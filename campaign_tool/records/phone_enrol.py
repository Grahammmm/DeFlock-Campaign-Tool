"""Phone enrolment (A6 PR 3): the public record of one new passkey, and the trust line an operator may add, attended.

Only public data ever reaches the box. The console page (``console_phone_enrol``) asks the phone to create an ES256
passkey (``navigator.credentials.create``, attestation none) and stores the browser's public answer: credential id,
attestationObject, clientDataJSON, the server challenge and the receive time. ``records phone-key line`` turns that
record into one ``allowed_signers`` line for ``phone_approval`` (namespace ``deflock-phone-approval`` only, with a
``valid-before`` date) and prints it with its SHA256 fingerprint. Nothing here writes the trust file; adding the line
is the attended sitting step (docs/APPROVALS.md).

Both the page and the command run ``parse``: a minimal strict CBOR reader (maps, byte and text strings and small
integers in shortest form, nothing else, no trailing bytes) and the WebAuthn checks for this one shape: attestation
format ``none``; rpIdHash = sha256(rp id); user presence, user verification and attested credential data set and no
extensions; a COSE EC2 P-256 ES256 key whose point is on the curve; clientDataJSON of type ``webauthn.create`` with
the stored challenge, origin ``https://<rp id>`` and not from a framed page. Anything else refuses.
"""
import argparse
import base64
import hashlib
import json
import os
import re
import stat
import sys
import time

from .. import phone_approval
from . import console_origin

SCHEMA = "deflock-phone-enrol-v1"
RECORD_FIELDS = ("attestation_object", "challenge", "client_data", "credential_id", "received_at", "rp_id", "schema")
PREFIX = "phone-enrol-"
MAX_FIELD = 4096  # base64url characters per field; a real record is well under 1 KiB
MAX_RECORD = 64 * 1024
MAX_RECORDS = 20  # an admitted page cannot fill the disk; the operator clears old records
CHALLENGE_BYTES = 32
DEFAULT_VALID_DAYS, MAX_VALID_DAYS = 365, 400
STAMP = "%Y-%m-%dT%H:%M:%SZ"
PRINCIPAL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}")
_B64URL = re.compile(r"[A-Za-z0-9_-]*")
# NIST P-256 (the curve check refuses a point a broken or hostile page made up)
_P = 0xffffffff00000001000000000000000000000000ffffffffffffffffffffffff
_B = 0x5ac635d8aa3a93e7b3ebbd55769886bc651d06b0cc53b0f63bce3c3e27d2604b
UP, UV, AT, ED = phone_approval.UP, phone_approval.UV, phone_approval.AT, phone_approval.ED


class EnrolError(ValueError):
    """A content-free refusal code; never carries record bytes."""


# ---- minimal strict CBOR -------------------------------------------------------------------------------------------

class _Cbor:
    def __init__(self, data):
        self.data, self.at = data, 0

    def take(self, n):
        if self.at + n > len(self.data):
            raise EnrolError("cbor_truncated")
        out = self.data[self.at:self.at + n]
        self.at += n
        return out

    def _argument(self, info):
        if info < 24:
            return info
        if info not in (24, 25, 26):  # 8-byte arguments, reserved values and indefinite lengths are never needed
            raise EnrolError("cbor_unsupported")
        size = 1 << (info - 24)
        value = int.from_bytes(self.take(size), "big")
        if value < (24 if size == 1 else 1 << (4 * size)):
            raise EnrolError("cbor_not_shortest")
        return value

    def item(self, depth=0):
        if depth > 3:
            raise EnrolError("cbor_too_deep")
        head = self.take(1)[0]
        major, value = head >> 5, self._argument(head & 31)
        if major == 0:
            return value
        if major == 1:
            return -1 - value
        if major == 2:
            return bytes(self.take(value))
        if major == 3:
            try:
                return self.take(value).decode("utf-8")
            except UnicodeDecodeError:
                raise EnrolError("cbor_text_invalid") from None
        if major == 5:
            out = {}
            for _ in range(value):
                key = self.item(depth + 1)
                if type(key) not in (int, str) or key in out:
                    raise EnrolError("cbor_map_key_invalid")
                out[key] = self.item(depth + 1)
            return out
        raise EnrolError("cbor_unsupported")  # arrays, tags, floats, true/false/null


def cbor_one(data):
    """The one CBOR item that is exactly ``data`` (trailing bytes refuse)."""
    reader = _Cbor(data)
    value = reader.item()
    if reader.at != len(data):
        raise EnrolError("cbor_trailing_bytes")
    return value


# ---- WebAuthn registration (this one shape only) ------------------------------------------------------------------

def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def unb64url(text, limit=MAX_FIELD):
    if not isinstance(text, str) or not 0 < len(text) <= limit or not _B64URL.fullmatch(text) or len(text) % 4 == 1:
        raise EnrolError("field_invalid")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def check_rp_id(rp_id):
    try:
        return console_origin.check_public_origin("https://" + rp_id)[0] if isinstance(rp_id, str) else None
    except ValueError:
        return None


def _unique(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate")
        out[key] = value
    return out


def _client(client_data, rp_id, challenge):
    try:
        client = json.loads(client_data.decode("utf-8"), object_pairs_hook=_unique)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise EnrolError("client_data_invalid") from None
    if not isinstance(client, dict) or client.get("type") != "webauthn.create":
        raise EnrolError("client_data_type")
    if client.get("challenge") != b64url(challenge):
        raise EnrolError("client_data_challenge")
    if client.get("origin") != "https://" + rp_id or client.get("crossOrigin", False) is not False \
            or "topOrigin" in client:  # never from a page framed by another site
        raise EnrolError("client_data_origin")


def _cose_point(key):
    if not isinstance(key, dict) or set(key) != {1, 3, -1, -2, -3}:
        raise EnrolError("key_not_es256")
    x, y = key[-2], key[-3]
    if key[1] != 2 or key[3] != -7 or key[-1] != 1 or not isinstance(x, bytes) or not isinstance(y, bytes) \
            or len(x) != 32 or len(y) != 32:
        raise EnrolError("key_not_es256")
    xi, yi = int.from_bytes(x, "big"), int.from_bytes(y, "big")
    if xi >= _P or yi >= _P or (yi * yi - (xi * xi * xi - 3 * xi + _B)) % _P:
        raise EnrolError("key_not_on_curve")
    return b"\x04" + x + y


def parse(attestation_object, client_data, rp_id, challenge, credential_id):
    """{"blob", "fingerprint", "rp_id"} of a registration that meets every check, else EnrolError (a code only)."""
    if check_rp_id(rp_id) != rp_id:
        raise EnrolError("rp_id_invalid")
    if not all(isinstance(v, bytes) for v in (attestation_object, client_data, challenge, credential_id)) \
            or len(challenge) != CHALLENGE_BYTES:
        raise EnrolError("field_invalid")
    _client(client_data, rp_id, challenge)
    top = cbor_one(attestation_object)
    if not isinstance(top, dict) or set(top) != {"fmt", "attStmt", "authData"} or top["fmt"] != "none" \
            or top["attStmt"] != {} or not isinstance(top["authData"], bytes):
        raise EnrolError("attestation_not_none")
    auth = top["authData"]
    if len(auth) < 55:
        raise EnrolError("auth_data_short")
    if auth[:32] != hashlib.sha256(rp_id.encode("ascii")).digest():
        raise EnrolError("rp_id_mismatch")
    flags = auth[32]
    if flags & (UP | UV | AT) != UP | UV | AT:
        raise EnrolError("flags_missing")
    if flags & ED:
        raise EnrolError("extensions_unsupported")
    length = int.from_bytes(auth[53:55], "big")
    if not 16 <= length <= 1023 or auth[55:55 + length] != credential_id or len(credential_id) != length:
        raise EnrolError("credential_id_mismatch")
    point = _cose_point(cbor_one(auth[55 + length:]))  # no extensions: the key is the last item
    blob = (phone_approval._s(phone_approval.KEY_TYPE) + phone_approval._s("nistp256") + phone_approval._s(point)
            + phone_approval._s(rp_id))
    return {"blob": blob, "fingerprint": fingerprint(blob), "rp_id": rp_id}


def fingerprint(blob):
    """The OpenSSH SHA256 fingerprint (as ``ssh-keygen -l`` prints it) of a public key blob."""
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii").rstrip("=")


# ---- the public record --------------------------------------------------------------------------------------------

def check_dir(path, *others):
    """The absolute, real, owner-only 0700 enrol directory, disjoint from every other console location, else
    ValueError. Never creates or changes anything."""
    try:
        info = os.lstat(path)
        ok = (isinstance(path, str) and os.path.isabs(path) and os.path.realpath(path) == path
              and stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700)
        for other in others:
            if ok and other is not None:
                other = os.path.realpath(other)
                ok = os.path.commonpath((other, path)) not in (other, path)
    except (OSError, TypeError, ValueError):
        ok = False
    if not ok:
        raise ValueError("phone_enrol_dir_invalid")
    return path


def store(directory, rp_id, challenge, credential_id, attestation_object, client_data, now=None):
    """Parse, then write ONE public record as a new 0600 file; -> (file name, parsed). EnrolError or OSError/ValueError
    (directory unusable or full) leave nothing behind."""
    parsed = parse(attestation_object, client_data, rp_id, challenge, credential_id)
    check_dir(directory)
    if sum(1 for name in os.listdir(directory) if name.startswith(PREFIX)) >= MAX_RECORDS:
        raise ValueError("phone_enrol_full")
    now = int(time.time() if now is None else now)
    record = {"schema": SCHEMA, "rp_id": rp_id, "credential_id": b64url(credential_id),
              "attestation_object": b64url(attestation_object), "client_data": b64url(client_data),
              "challenge": b64url(challenge), "received_at": time.strftime(STAMP, time.gmtime(now))}
    name = "%s%s-%s.json" % (PREFIX, time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now)),
                             hashlib.sha256(parsed["blob"]).hexdigest()[:12])
    data = (json.dumps(record, sort_keys=True, indent=1) + "\n").encode("ascii")
    path = os.path.join(directory, name)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)  # created here: ours to remove
    try:
        try:
            view = memoryview(data)
            while view:  # os.write may be short: write until every byte is out
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("phone_enrol_short_write")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
    except BaseException:
        try:
            os.unlink(path)  # a failed write/fsync/close must not leave a partial record behind
        except OSError:
            pass
        raise
    return name, parsed


def load(path):
    """Parsed credential of one stored record file (re-checked in full), else EnrolError."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise EnrolError("record_unreadable") from None
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise EnrolError("record_unreadable")
        raw = stream.read(MAX_RECORD + 1)
    try:
        record = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise EnrolError("record_invalid") from None
    if len(raw) > MAX_RECORD or not isinstance(record, dict) or tuple(sorted(record)) != RECORD_FIELDS \
            or record["schema"] != SCHEMA or not all(isinstance(v, str) for v in record.values()):
        raise EnrolError("record_invalid")
    return parse(unb64url(record["attestation_object"]), unb64url(record["client_data"]), record["rp_id"],
                 unb64url(record["challenge"]), unb64url(record["credential_id"]))


def line(parsed, principal, valid_days=DEFAULT_VALID_DAYS, now=None):
    """The one allowed_signers line for a parsed credential: phone namespace only, expiring after ``valid_days``."""
    if not isinstance(principal, str) or not PRINCIPAL.fullmatch(principal):
        raise EnrolError("principal_invalid")
    if type(valid_days) is not int or not 1 <= valid_days <= MAX_VALID_DAYS:
        raise EnrolError("valid_days_invalid")
    before = time.strftime("%Y%m%d", time.gmtime(int(time.time() if now is None else now) + valid_days * 86400))
    return '%s namespaces="%s",valid-before="%s" %s %s' % (
        principal, phone_approval.NAMESPACE, before, phone_approval.KEY_TYPE,
        base64.b64encode(parsed["blob"]).decode("ascii"))


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="records phone-key", description="Print the trust line for an enrolled phone's PUBLIC key. "
        "It never writes the trust file; adding the line is the attended step in docs/APPROVALS.md.")
    commands = parser.add_subparsers(dest="command", required=True)
    one = commands.add_parser("line", help="print one allowed_signers line (stdout) and its fingerprint (stderr)")
    one.add_argument("--from", dest="source", required=True, metavar="FILE", help="record written by /phone/enrol")
    one.add_argument("--principal", required=True, metavar="NAME", help="the approver's existing principal name")
    one.add_argument("--valid-days", type=int, default=DEFAULT_VALID_DAYS, metavar="N",
                     help="days until the line expires (1..%d; default %%(default)s)" % MAX_VALID_DAYS)
    args = parser.parse_args(argv)
    try:
        parsed = load(args.source)
        text = line(parsed, args.principal, args.valid_days)
    except EnrolError as error:
        print("phone-key: refused (%s); nothing was printed or written" % error, file=sys.stderr)
        return 2
    print(text)
    print("fingerprint %s (application %s); compare it with the phone screen before adding the line"
          % (parsed["fingerprint"], parsed["rp_id"]), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
