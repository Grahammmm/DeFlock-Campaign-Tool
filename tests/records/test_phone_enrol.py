"""A6 phone enrolment: /phone/enrol keeps one PUBLIC passkey record; `records phone-key line` prints its trust line.

Contract: only a registration that is exactly ES256 P-256, with user presence and verification, for the console's rp id,
the stored challenge and the https://<rp id> origin, in strict CBOR, is recorded or turned into a line; that line is
limited to the phone namespace with an expiry, is accepted by ssh-keygen and makes phone_approval.verify_tap accept a
tap from the same key. The page stores nothing without its option or CSRF token. Synthetic keys only; no browser.
"""
import base64
import hashlib
import http.client
import io
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from campaign_tool import approval, phone_approval
from campaign_tool.records import admin_panel as ap, console_phone_enrol as cpe, phone_enrol as pe
from tests.approval_signing import KEYGEN, Signer
from tests.phone_signing import RP_ID, Phone
from tests.records.test_console_export import tree_state
from tests.records.test_console_journal import Journal

CHALLENGE, CRED_ID = bytes(range(32)), b"\x07" * 20
FLAGS = pe.UP | pe.UV | pe.AT | 0x18  # backup eligible and backed up, as an iCloud passkey reports


def cbor(value):
    def head(major, n):
        if n < 24:
            return bytes([major << 5 | n])
        size, info = ((1, 24) if n < 256 else (2, 25) if n < 65536 else (4, 26))
        return bytes([major << 5 | info]) + n.to_bytes(size, "big")
    if type(value) is int:
        return head(0, value) if value >= 0 else head(1, -1 - value)
    if isinstance(value, bytes):
        return head(2, len(value)) + value
    if isinstance(value, str):
        return head(3, len(value.encode())) + value.encode()
    return head(5, len(value)) + b"".join(cbor(k) + cbor(v) for k, v in value.items())


def registration(phone, rp_id=RP_ID, flags=FLAGS, key=None, challenge=CHALLENGE, origin=None, kind="webauthn.create",
                 trailing=b""):
    """(attestationObject, clientDataJSON) as navigator.credentials.create() returns them for ``phone`` (fmt none)."""
    point = phone_approval._Reader(phone.blob)
    point.string(), point.string()
    point = point.string()
    cose = {1: 2, 3: -7, -1: 1, -2: point[1:33], -3: point[33:]} if key is None else key(point)
    auth = (hashlib.sha256(rp_id.encode()).digest() + bytes([flags]) + b"\0" * 4 + b"\0" * 16
            + len(CRED_ID).to_bytes(2, "big") + CRED_ID + cbor(cose))
    client = json.dumps({"type": kind, "challenge": pe.b64url(challenge), "origin": origin or "https://" + RP_ID,
                         "crossOrigin": False}).encode()
    return cbor({"fmt": "none", "attStmt": {}, "authData": auth}) + trailing, client


class Parse(unittest.TestCase):
    # Bug caught: accepting a registration for another site, without Face ID/passcode, with a non-ES256 or off-curve
    # key, for a challenge the console never issued, from another or framed origin, or with smuggled trailing bytes.
    def test_anything_but_the_one_shape_is_refused(self):
        phone = Phone()
        cases = {
            "rp_id_mismatch": registration(phone, rp_id="evil.example.invalid"),
            "flags_missing": registration(phone, flags=pe.UP | pe.AT),
            "extensions_unsupported": registration(phone, flags=FLAGS | pe.ED),
            "key_not_es256": registration(phone, key=lambda p: {1: 2, 3: -8, -1: 1, -2: p[1:33], -3: p[33:]}),
            "key_not_es256 ": registration(phone, key=lambda p: {1: 2, 3: -7, -1: 2, -2: p[1:33], -3: p[33:]}),
            "key_not_on_curve": registration(phone, key=lambda p: {1: 2, 3: -7, -1: 1, -2: p[1:33], -3: b"\1" * 32}),
            "client_data_challenge": registration(phone, challenge=b"\1" * 32),
            "client_data_origin": registration(phone, origin="https://evil.example.invalid"),
            "client_data_type": registration(phone, kind="webauthn.get"),
            "cbor_trailing_bytes": registration(phone, trailing=b"\0"),
        }
        framed = json.loads(registration(phone)[1])
        cases["client_data_origin  "] = (registration(phone)[0], json.dumps(dict(framed, crossOrigin=True)).encode())
        for code, (att, client) in cases.items():
            with self.subTest(code):
                with self.assertRaises(pe.EnrolError) as caught:
                    pe.parse(att, client, RP_ID, CHALLENGE, CRED_ID)
                self.assertEqual(str(caught.exception), code.strip())
        with self.assertRaises(pe.EnrolError):  # a non-shortest CBOR length is not canonical
            pe.cbor_one(b"\x58\x01\x00")
        good = pe.parse(*registration(phone), RP_ID, CHALLENGE, CRED_ID)
        self.assertEqual(good["blob"], phone.blob)


class Line(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.enrol = self.tmp / "enrol"
        self.enrol.mkdir(mode=0o700)

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = pe.main(list(args))
        return code, out.getvalue(), err.getvalue()

    # Bug caught: a printed line ssh-keygen cannot read, a fingerprint that is not the key's, a line that carries the
    # typed-path namespace or no expiry, or a key blob that phone_approval cannot match to a real tap.
    def test_the_line_from_a_stored_record_verifies_a_tap_end_to_end(self):
        phone = Phone()
        name, parsed = pe.store(str(self.enrol), RP_ID, CHALLENGE, CRED_ID, *registration(phone))
        record = self.enrol / name
        self.assertEqual((stat.S_IMODE(os.stat(record).st_mode), stat.S_IMODE(os.stat(self.enrol).st_mode)),
                         (0o600, 0o700))
        self.assertEqual(sorted(json.loads(record.read_text())), sorted(pe.RECORD_FIELDS))
        code, out, err = self.cli("line", "--from", str(record), "--principal", phone.principal)
        self.assertEqual(code, 0, err)
        (text,) = out.splitlines()
        before = time.strftime("%Y%m%d", time.gmtime(time.time() + 365 * 86400))
        self.assertEqual(text.split(" ")[:3], [phone.principal, 'namespaces="deflock-phone-approval",valid-before="%s"'
                                               % before, phone_approval.KEY_TYPE])
        self.assertNotIn('"%s"' % approval.NAMESPACE, text)
        self.assertNotIn(approval.NAMESPACE + ",", text)
        self.assertIn(parsed["fingerprint"], err)
        pub = self.tmp / "key.pub"
        pub.write_text(" ".join(text.split(" ")[2:]) + "\n")
        listed = subprocess.run([KEYGEN, "-l", "-f", str(pub)], capture_output=True, text=True, check=True).stdout
        self.assertEqual(listed.split()[1], parsed["fingerprint"])
        computer = Signer("g@example.invalid")
        self.addCleanup(computer.close)
        trust = self.tmp / "allowed_signers"
        trust.write_text(computer.signers.read_text() + text + "\n")
        payload = b"synthetic approval payload"
        tap = phone.tap(phone_approval.envelope("draft-1", payload, int(time.time()), "ab" * 16))
        with computer.trust(trust):
            self.assertEqual(phone_approval.verify_tap(tap, "draft-1", payload)[0], phone.principal)
            with self.assertRaises(approval.ApprovalError):  # another passkey is not this line's key
                phone_approval.verify_tap(Phone().tap(tap["envelope"]), "draft-1", payload)

    # Bug caught: a principal that injects options or a second key, an unbounded expiry, or a tampered record.
    def test_bad_principal_validity_or_record_prints_nothing(self):
        name, _ = pe.store(str(self.enrol), RP_ID, CHALLENGE, CRED_ID, *registration(Phone()))
        record = str(self.enrol / name)
        for args in (("--principal", "g x"), ("--principal", 'g,cert-authority'), ("--principal", "p", "--valid-days", "401"),
                     ("--principal", "p", "--valid-days", "0")):
            code, out, _ = self.cli("line", "--from", record, *args)
            self.assertEqual((code, out), (2, ""), args)
        doc = json.loads(Path(record).read_text())
        doc["rp_id"] = "evil.example.invalid"
        forged = self.enrol / "forged.json"
        forged.write_text(json.dumps(doc))
        self.assertEqual(self.cli("line", "--from", str(forged), "--principal", "p")[:2], (2, ""))


class Page(Journal):
    def setUp(self):
        super().setUp()
        self.enrol = self.tmp / "enrol"
        self.enrol.mkdir(mode=0o700)
        self.make_export()

    def fields(self, page):
        return dict(re.findall(r'<input type="hidden" name="([a-z_]+)" value="([^"]*)">', page))

    # Bug caught: the enrol POST storing a record (or journaling anything) without its option, without the CSRF
    # token, off the public host, or with a challenge this console did not issue.
    def test_without_the_option_there_is_no_page_and_nothing_is_stored(self):
        self.up()
        self.get(cpe.PATH, 404)
        self.get(cpe.SCRIPT_PATH, 404)
        self.assertEqual(self.decide(cpe.PATH, challenge="x")[0], 404)
        self.assertEqual((os.listdir(self.enrol), self.lines()), ([], []))

    def test_without_csrf_host_or_issued_challenge_nothing_is_stored(self):
        self.up(phone_enrol_dir=str(self.enrol))
        self.assertIn("only on the console's public address", self.get(cpe.PATH))
        with mock.patch.object(cpe, "_rp_id", return_value=RP_ID):
            fields = self.fields(self.get(cpe.PATH))
            att, client = registration(Phone(), challenge=pe.unb64url(fields["challenge"]))
            full = dict(fields, credential_id=pe.b64url(CRED_ID), attestation_object=pe.b64url(att),
                        client_data=pe.b64url(client))
            self.assertEqual(self.decide(cpe.PATH, **dict(full, csrf_token="wrong"))[0], 403)
            self.assertEqual(self.decide(cpe.PATH, **dict(full, tag="0" * 64))[0], 403)
        self.assertEqual(self.decide(cpe.PATH, **full)[0], 403)  # loopback host: no rp id
        self.assertEqual((os.listdir(self.enrol), self.lines()), ([], []))

    # Bug caught: the page not asking for ES256 with user verification, or the POST writing outside its own 0700
    # directory (export, journal) or keeping more than the public record.
    def test_page_asks_for_es256_and_post_stores_one_public_record(self):
        self.up(phone_enrol_dir=str(self.enrol))
        script = self.get(cpe.SCRIPT_PATH)
        for needle in ("alg: -7", 'userVerification: "required"', 'attestation: "none"', 'residentKey: "preferred"'):
            self.assertIn(needle, script)
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        self.addCleanup(connection.close)
        connection.request("GET", cpe.PATH)
        response = connection.getresponse()
        response.read()
        self.assertEqual(response.getheader("Content-Security-Policy"), cpe.PAGE_CSP)  # only this page runs a script
        root_before = tree_state(self.root)
        phone = Phone()
        with mock.patch.object(cpe, "_rp_id", return_value=RP_ID):
            page = self.get(cpe.PATH)
            self.assertIn('data-rp-id="%s"' % RP_ID, page)
            fields = self.fields(page)
            att, client = registration(phone, challenge=pe.unb64url(fields["challenge"]))
            status, _, done = self.decide(cpe.PATH, **dict(fields, credential_id=pe.b64url(CRED_ID),
                                                           attestation_object=pe.b64url(att),
                                                           client_data=pe.b64url(client)))
        self.assertEqual(status, 200, done[:300])
        self.assertIn(pe.fingerprint(phone.blob), done)
        (name,) = os.listdir(self.enrol)
        self.assertEqual(stat.S_IMODE(os.stat(self.enrol / name).st_mode), 0o600)
        self.assertEqual(pe.load(str(self.enrol / name))["blob"], phone.blob)
        self.assertEqual((tree_state(self.root), self.lines()), (root_before, []))

    def test_the_option_needs_the_journal_and_its_own_private_directory(self):
        loose = self.tmp / "loose"
        loose.mkdir(mode=0o755)
        os.chmod(loose, 0o755)
        for kw in ({"phone_enrol_dir": str(self.enrol)},
                   {"phone_enrol_dir": str(loose), "decisions_dir": str(self.j)},
                   {"phone_enrol_dir": str(self.j), "decisions_dir": str(self.j)}):
            with self.assertRaises(ap.PanelError) as caught:
                ap.build_server(run_id="auto", actor="operator", system=False, export_root=str(self.root), **kw)
            self.assertEqual(caught.exception.code, "phone_enrol_dir_invalid")


if __name__ == "__main__":
    unittest.main()
