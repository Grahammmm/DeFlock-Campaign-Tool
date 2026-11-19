"""Approval by a tap on G's phone (A6): a passkey (WebAuthn) assertion checked as an OpenSSH signature.

Trust. The phone's passkey public key is enrolled, attended, as one ``sk-ecdsa-sha2-nistp256@openssh.com`` line in the
same root-owned trust file as the typed path (``approval.TRUSTED_SIGNERS``), limited to
``namespaces="deflock-phone-approval"``. The key's application is the console's WebAuthn relying-party id. The private
key never leaves the phone; nothing here creates, holds or accepts one.

What is signed. The phone signs an envelope (``envelope``): the decision id, the sha256 of the exact
``approval.payload`` bytes the typed path would sign (scope, id, revision, content hash and full text, so an edit or
another tie choice is another hash), the issue and expiry times and a nonce. The WebAuthn challenge is the SSHSIG
signed-data blob of that envelope (``challenge``).

Verification. The browser's assertion is repacked, format only, as an SSHSIG of type
``webauthn-sk-ecdsa-sha2-nistp256@openssh.com`` (``to_sshsig``) and verified by ``approval.verify`` in the phone
namespace: the same root-owned ssh-keygen and trust file as the typed path, so an unknown or removed device fails there.
ssh-keygen does not enforce the WebAuthn flags, so ``check`` also requires user presence and user verification, a
``webauthn.get`` client data and the exact ``https://<rp id>`` origin, all taken from the signature it verified.
Expiry is enforced when an approval is recorded (``verify_tap``); a stored approval is re-verified without it. One
nonce approves once: the approval queue, which owns the record, refuses a nonce it has already stored.
"""
import base64
import calendar
import hashlib
import json
import re
import struct
import time

from . import approval

NAMESPACE = approval.PHONE_NAMESPACE
HEADER = "deflock-phone-approval-v1"
KEY_TYPE = "sk-ecdsa-sha2-nistp256@openssh.com"
SIG_TYPE = "webauthn-sk-ecdsa-sha2-nistp256@openssh.com"
DEFAULT_TTL = 24 * 3600  # the apply runs a few times a day; a tap must survive until the next one
MAX_TTL = 36 * 3600
SKEW = 300  # seconds an envelope may be issued ahead of the verifying clock
UP, UV, AT, ED = 0x01, 0x04, 0x40, 0x80
TAP_FIELDS = ("authenticator_data", "client_data", "envelope", "signature")
MAX_FIELD = 4096
STAMP = "%Y-%m-%dT%H:%M:%SZ"
_ID = r"[A-Za-z0-9._:-]{1,128}"
_TS = r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z"
_ENVELOPE = re.compile(HEADER + r"\ndecision: (%s)\npayload-sha256: ([0-9a-f]{64})\nissued: (%s)\nexpires: (%s)\n"
                       r"nonce: ([0-9a-f]{32})\n" % (_ID, _TS, _TS))
_B64URL = re.compile(r"[A-Za-z0-9_-]*")


class PhoneExpired(approval.ApprovalError):
    """The envelope is outside its validity window; a new tap is needed."""


def _stamp(epoch):
    return time.strftime(STAMP, time.gmtime(int(epoch)))


def _epoch(text):
    try:
        return calendar.timegm(time.strptime(text, STAMP))
    except ValueError:
        raise approval.ApprovalError("phone envelope time is invalid") from None


def envelope(decision, payload, issued, nonce, ttl=DEFAULT_TTL):
    """The exact text the phone signs for one decision over ``payload`` (the bytes the typed path would sign)."""
    if not isinstance(payload, bytes) or type(ttl) is not int or not 0 < ttl <= MAX_TTL:
        raise approval.ApprovalError("invalid phone envelope")
    text = "%s\ndecision: %s\npayload-sha256: %s\nissued: %s\nexpires: %s\nnonce: %s\n" % (
        HEADER, decision, hashlib.sha256(payload).hexdigest(), _stamp(issued), _stamp(int(issued) + ttl), nonce)
    parse_envelope(text)
    return text


def parse_envelope(text):
    match = _ENVELOPE.fullmatch(text) if isinstance(text, str) else None
    if match is None:
        raise approval.ApprovalError("invalid phone envelope")
    decision, digest, issued, expires, nonce = match.groups()
    return {"decision": decision, "payload_sha256": digest, "issued": _epoch(issued), "expires": _epoch(expires),
            "nonce": nonce}


def challenge(text):
    """The bytes the passkey signs (the WebAuthn challenge): the SSHSIG signed-data blob of the envelope."""
    return b"SSHSIG" + _s(NAMESPACE) + _s(b"") + _s(b"sha512") + _s(hashlib.sha512(text.encode()).digest())


# ---- SSH wire format (format conversion only; the verdict is ssh-keygen's) -----------------------------------------

def _s(value):
    value = value.encode() if isinstance(value, str) else value
    return struct.pack(">I", len(value)) + value


def _mpint(n):
    raw = n.to_bytes((n.bit_length() + 8) // 8, "big") if n else b""
    return _s(raw)


class _Reader:
    def __init__(self, data):
        self.data, self.at = data, 0

    def take(self, n):
        if n < 0 or self.at + n > len(self.data):
            raise approval.ApprovalError("malformed phone signature")
        out = self.data[self.at:self.at + n]
        self.at += n
        return out

    def string(self):
        return self.take(struct.unpack(">I", self.take(4))[0])

    def end(self):
        if self.at != len(self.data):
            raise approval.ApprovalError("malformed phone signature")


def _b64url(text):
    if not isinstance(text, str) or len(text) > MAX_FIELD or not _B64URL.fullmatch(text):
        raise approval.ApprovalError("malformed phone assertion")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _der_rs(der):
    """(r, s) of a DER ECDSA-Sig-Value; anything else refuses."""
    def integer(at):
        if at + 2 > len(der) or der[at] != 0x02 or not 0 < der[at + 1] <= 33 or at + 2 + der[at + 1] > len(der):
            raise approval.ApprovalError("malformed phone signature")
        return int.from_bytes(der[at + 2:at + 2 + der[at + 1]], "big"), at + 2 + der[at + 1]
    if len(der) < 8 or der[0] != 0x30 or der[1] != len(der) - 2:
        raise approval.ApprovalError("malformed phone signature")
    r, at = integer(2)
    s, at = integer(at)
    if at != len(der) or not r or not s:
        raise approval.ApprovalError("malformed phone signature")
    return r, s


def _pubkey(blob):
    """application (the WebAuthn rp id) of an sk-ecdsa public key blob."""
    reader = _Reader(blob)
    if reader.string() != KEY_TYPE.encode() or reader.string() != b"nistp256" or len(reader.string()) != 65:
        raise approval.ApprovalError("not a phone key")
    application = reader.string()
    reader.end()
    try:
        return application.decode("ascii")
    except UnicodeDecodeError:
        raise approval.ApprovalError("not a phone key") from None


def phone_keys():
    """[(public key blob, rp id)] of every phone line in the protected trust file (metadata checked first)."""
    approval.anchor_status()
    try:
        with open(approval.TRUSTED_SIGNERS, encoding="utf-8") as stream:
            lines = stream.read().splitlines()
    except (OSError, UnicodeError) as error:
        raise approval.TrustUnavailable("trust file unreadable") from error
    keys = []
    for line in lines:
        tokens = line.split()
        if not tokens or tokens[0].startswith("#") or KEY_TYPE not in tokens[1:3]:
            continue
        at = tokens.index(KEY_TYPE, 1)
        spaces = re.search(r'(?:^|,)namespaces="([^"]*)"', tokens[1]) if at == 2 else None
        if spaces is None or NAMESPACE not in spaces.group(1).split(",") or at + 1 >= len(tokens):
            continue
        try:
            blob = base64.b64decode(tokens[at + 1], validate=True)
            keys.append((blob, _pubkey(blob)))
        except (ValueError, approval.ApprovalError):
            continue  # ssh-keygen stays the judge of the file; an unusable line simply matches nothing
    return keys


def to_sshsig(pub, rp_id, tap):
    """The armored SSHSIG for a browser assertion under this key, or None when the assertion is for another rp id."""
    auth = _b64url(tap["authenticator_data"])
    if len(auth) < 37:
        raise approval.ApprovalError("malformed phone assertion")
    flags, counter, extensions = auth[32], auth[33:37], auth[37:]
    if auth[:32] != hashlib.sha256(rp_id.encode()).digest():
        return None
    if flags & AT or (extensions and not flags & ED):
        raise approval.ApprovalError("malformed phone assertion")
    client = _b64url(tap["client_data"])
    try:
        origin = json.loads(client.decode("utf-8"))["origin"]
    except (ValueError, UnicodeDecodeError, KeyError, TypeError):
        raise approval.ApprovalError("malformed phone assertion") from None
    if not isinstance(origin, str):
        raise approval.ApprovalError("malformed phone assertion")
    r, s = _der_rs(_b64url(tap["signature"]))
    sig = (_s(SIG_TYPE) + _s(_mpint(r) + _mpint(s)) + bytes([flags]) + counter + _s(origin) + _s(client)
           + _s(extensions))
    blob = b"SSHSIG" + struct.pack(">I", 1) + _s(pub) + _s(NAMESPACE) + _s(b"") + _s(b"sha512") + _s(sig)
    text = base64.b64encode(blob).decode()
    return ("-----BEGIN SSH SIGNATURE-----\n" + "\n".join(text[i:i + 70] for i in range(0, len(text), 70))
            + "\n-----END SSH SIGNATURE-----\n")


def _webauthn(armored):
    """(rp id, flags, client data dict, origin) of an armored phone SSHSIG; refuses any other shape."""
    try:
        body = "".join(armored.strip().splitlines()[1:-1])
        reader = _Reader(base64.b64decode(body, validate=True))
    except (ValueError, AttributeError):
        raise approval.ApprovalError("malformed phone signature") from None
    if reader.take(6) != b"SSHSIG" or reader.take(4) != struct.pack(">I", 1):
        raise approval.ApprovalError("malformed phone signature")
    rp_id = _pubkey(reader.string())
    if reader.string() != NAMESPACE.encode() or reader.string() != b"" or reader.string() != b"sha512":
        raise approval.ApprovalError("malformed phone signature")
    sig = _Reader(reader.string())
    reader.end()
    if sig.string() != SIG_TYPE.encode():
        raise approval.ApprovalError("not a phone signature")
    sig.string()
    flags = sig.take(1)[0]
    sig.take(4)
    origin, client = sig.string(), sig.string()
    sig.string()
    sig.end()
    try:
        data = json.loads(client.decode("utf-8"))
        origin = origin.decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        raise approval.ApprovalError("malformed phone signature") from None
    return rp_id, flags, data, origin


def _bound(text, decision, payload):
    env = parse_envelope(text)
    if env["decision"] != decision or env["payload_sha256"] != hashlib.sha256(payload).hexdigest():
        raise approval.ApprovalError("phone approval names another decision or text")
    return env


def check(text, armored, decision, payload):
    """The verified principal of a stored phone approval (no clock check), or ApprovalError / TrustUnavailable."""
    _bound(text, decision, payload)
    rp_id, flags, client, origin = _webauthn(armored)
    if (flags & (UP | UV)) != UP | UV or not isinstance(client, dict) or client.get("type") != "webauthn.get" \
            or origin != "https://" + rp_id or client.get("origin") != origin \
            or client.get("crossOrigin", False) is not False or "topOrigin" in client:  # never from a framed page
        raise approval.ApprovalError("phone approval lacks presence, verification or the console origin")
    return approval.verify(text.encode(), armored, namespace=NAMESPACE)


def tap_envelope(tap):
    """The parsed envelope of a well-shaped tap (exactly the four string fields), else ApprovalError."""
    if not isinstance(tap, dict) or tuple(sorted(tap)) != TAP_FIELDS \
            or not all(isinstance(tap[k], str) and len(tap[k]) <= MAX_FIELD for k in TAP_FIELDS):
        raise approval.ApprovalError("malformed phone tap")
    return parse_envelope(tap["envelope"])


def verify_tap(tap, decision, payload, now=None):
    """(principal, armored signature, envelope text) for a fresh tap approving ``decision`` over ``payload``.

    PhoneExpired outside the validity window; ApprovalError for anything unproven (an unknown or removed device, another
    text, missing presence or verification); TrustUnavailable when the anchor or verifier cannot be used."""
    env = tap_envelope(tap)
    _bound(tap["envelope"], decision, payload)
    now = time.time() if now is None else now
    if not (env["issued"] < env["expires"] <= env["issued"] + MAX_TTL and env["issued"] - SKEW <= now <= env["expires"]):
        raise PhoneExpired("phone approval expired or not yet valid")
    for pub, rp_id in phone_keys():
        armored = to_sshsig(pub, rp_id, tap)
        if armored is None:
            continue
        try:
            return check(tap["envelope"], armored, decision, payload), armored, tap["envelope"]
        except approval.TrustUnavailable:
            raise
        except approval.ApprovalError:
            continue  # another enrolled key may share the rp id
    raise approval.ApprovalError("phone approval is not from an enrolled device")
