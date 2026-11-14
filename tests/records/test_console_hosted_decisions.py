"""DFB-11: decisions on the hosted console (public origin + decision journal) need a signature-verified Access token.

Synthetic only: throwaway RSA keys generated here (never a real Access key, AUD, team or email). The known-answer test uses
the public example of RFC 7515 Appendix A.2, so the verifier is checked against a signature it did not make itself.
"""
import base64
import email.message
import hashlib
import http.client
import io
import json
import os
import random
import tempfile
import time
import unittest
import urllib.parse
from contextlib import redirect_stderr
from pathlib import Path

from campaign_tool.records import admin_panel as ap
from campaign_tool.records import console_access as ca
from campaign_tool.records import public_packets as pp
from tests.records.test_console_access import AUD, EMAIL, HDR, TEAM, make_token, write_config
from tests.records.test_console_apply import Apply
from tests.records.test_console_model import RUN

PUBLIC, HOST = "https://admin.example.test", "admin.example.test"
NOW = int(time.time())
KID = "kid-1"
_SMALL = [p for p in range(3, 2000, 2) if all(p % q for q in range(3, int(p ** 0.5) + 1, 2))]

# RFC 7515 Appendix A.2 (JWS using RS256): signing input octets, public modulus (e = 65537) and signature octets.
RFC_INPUT = bytes([
    101, 121, 74, 104, 98, 71, 99, 105, 79, 105, 74, 83, 85, 122, 73, 49, 78, 105, 74, 57, 46, 101, 121, 74, 112, 99, 51,
    77, 105, 79, 105, 74, 113, 98, 50, 85, 105, 76, 65, 48, 75, 73, 67, 74, 108, 101, 72, 65, 105, 79, 106, 69, 122, 77,
    68, 65, 52, 77, 84, 107, 122, 79, 68, 65, 115, 68, 81, 111, 103, 73, 109, 104, 48, 100, 72, 65, 54, 76, 121, 57, 108,
    101, 71, 70, 116, 99, 71, 120, 108, 76, 109, 78, 118, 98, 83, 57, 112, 99, 49, 57, 121, 98, 50, 57, 48, 73, 106, 112,
    48, 99, 110, 86, 108, 102, 81])
RFC_N = int.from_bytes(bytes([
    161, 248, 22, 10, 226, 227, 201, 180, 101, 206, 141, 45, 101, 98, 99, 54, 43, 146, 125, 190, 41, 225, 240, 36,
    119, 252, 22, 37, 204, 144, 161, 54, 227, 139, 217, 52, 151, 197, 182, 234, 99, 221, 119, 17, 230, 124, 116, 41,
    249, 86, 176, 251, 138, 143, 8, 154, 220, 75, 105, 137, 60, 193, 51, 63, 83, 237, 208, 25, 184, 119, 132, 37,
    47, 236, 145, 79, 228, 133, 119, 105, 89, 75, 234, 66, 128, 211, 44, 15, 85, 191, 98, 148, 79, 19, 3, 150, 188,
    110, 155, 223, 110, 189, 210, 189, 163, 103, 142, 236, 160, 198, 104, 247, 1, 179, 141, 191, 251, 56, 200, 52,
    44, 226, 254, 109, 39, 250, 222, 74, 90, 72, 116, 151, 157, 212, 185, 207, 154, 222, 196, 199, 91, 5, 133, 44,
    44, 15, 94, 248, 165, 193, 117, 3, 146, 249, 68, 232, 237, 100, 193, 16, 198, 182, 71, 96, 154, 164, 120, 58,
    235, 156, 108, 154, 215, 85, 49, 48, 80, 99, 139, 131, 102, 92, 111, 111, 122, 130, 163, 150, 112, 42, 31, 100,
    27, 130, 211, 235, 242, 57, 34, 25, 73, 31, 182, 134, 135, 44, 87, 22, 245, 10, 248, 53, 141, 154, 139, 157, 23,
    195, 64, 114, 143, 127, 135, 216, 154, 24, 216, 252, 171, 103, 173, 132, 89, 12, 46, 207, 117, 147, 57, 54, 60,
    7, 3, 77, 111, 96, 111, 158, 33, 224, 84, 86, 202, 229, 233, 161]), "big")
RFC_SIG = bytes([
    112, 46, 33, 137, 67, 232, 143, 209, 30, 181, 216, 45, 191, 120, 69, 243, 65, 6, 174, 27, 129, 255, 247, 115, 17, 22,
    173, 209, 113, 125, 131, 101, 109, 66, 10, 253, 60, 150, 238, 221, 115, 162, 102, 62, 81, 102, 104, 123, 0, 11, 135,
    34, 110, 1, 135, 237, 16, 115, 249, 69, 229, 130, 173, 252, 239, 22, 216, 90, 121, 142, 232, 198, 109, 219, 61, 184,
    151, 91, 23, 208, 148, 2, 190, 237, 213, 217, 217, 112, 7, 16, 141, 178, 129, 96, 213, 248, 4, 12, 167, 68, 87, 98,
    184, 31, 190, 127, 249, 217, 46, 10, 231, 111, 36, 242, 91, 51, 187, 230, 244, 74, 230, 30, 177, 4, 10, 203, 32, 4, 77,
    62, 249, 18, 142, 212, 1, 48, 121, 91, 212, 189, 59, 65, 238, 202, 208, 102, 171, 101, 25, 129, 253, 228, 141, 247,
    127, 55, 45, 195, 139, 159, 175, 221, 59, 239, 177, 139, 93, 163, 204, 60, 46, 176, 47, 158, 58, 65, 214, 18, 202, 173,
    21, 145, 18, 115, 160, 95, 35, 185, 232, 56, 250, 175, 132, 157, 105, 132, 41, 239, 90, 30, 136, 121, 130, 54, 195, 212,
    14, 96, 69, 34, 165, 68, 200, 242, 122, 122, 45, 184, 6, 99, 209, 108, 247, 202, 234, 86, 222, 64, 92, 178, 33, 90, 69,
    178, 194, 85, 102, 181, 90, 193, 167, 72, 160, 112, 223, 200, 163, 42, 70, 149, 67, 208, 25, 238, 251, 71])


def _probable_prime(rng, bits):
    while True:
        c = rng.getrandbits(bits) | (3 << (bits - 2)) | 1
        if any(c % p == 0 for p in _SMALL):
            continue
        d, r = c - 1, 0
        while d % 2 == 0:
            d, r = d // 2, r + 1
        for _ in range(24):
            x = pow(rng.randrange(2, c - 2), d, c)
            if x in (1, c - 1):
                continue
            for _ in range(r - 1):
                x = pow(x, 2, c)
                if x == c - 1:
                    break
            else:
                break
        else:
            return c


class TestKey:
    """A throwaway, deterministic RSA key (test only) that signs RS256 JWTs like Cloudflare Access."""
    _cache = {}

    def __init__(self, seed, bits=2048):
        if (seed, bits) not in TestKey._cache:
            rng, e = random.Random(seed), 65537
            while True:
                p, q = _probable_prime(rng, bits // 2), _probable_prime(rng, bits // 2)
                phi = (p - 1) * (q - 1)
                if p != q and phi % e and (p * q).bit_length() == bits:
                    break
            TestKey._cache[(seed, bits)] = (p * q, e, pow(e, -1, phi))
        self.n, self.e, self.d = TestKey._cache[(seed, bits)]

    def jwk(self, kid=KID):
        size = (self.n.bit_length() + 7) // 8
        return {"kid": kid, "kty": "RSA", "alg": "RS256", "use": "sig", "e": _b64(self.e.to_bytes(3, "big")),
                "n": _b64(self.n.to_bytes(size, "big"))}

    def sign(self, data):
        size = (self.n.bit_length() + 7) // 8
        digest = ca._SHA256_INFO + hashlib.sha256(data).digest()
        block = b"\x00\x01" + b"\xff" * (size - 3 - len(digest)) + b"\x00" + digest
        return pow(int.from_bytes(block, "big"), self.d, self.n).to_bytes(size, "big")

    def token(self, kid=KID, header=None, **claims):
        payload = {"aud": [AUD], "iss": "https://" + TEAM, "exp": NOW + 3600, "iat": NOW - 10, "email": EMAIL,
                   "sub": "synthetic", **claims}
        head = {"alg": "RS256", "kid": kid, "typ": "JWT"} if header is None else header
        signing_input = _b64(json.dumps(head).encode()) + "." + _b64(json.dumps(payload).encode())
        return signing_input + "." + _b64(self.sign(signing_input.encode()))


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def write_keys(directory, *jwks, mode=0o600, extra=None, name="access-certs.json"):
    path = Path(directory) / name
    path.write_text(json.dumps({"keys": list(jwks), **(extra or {})}))
    os.chmod(path, mode)
    return path


def headers(*tokens):
    msg = email.message.Message()
    for token in tokens:
        msg[HDR] = token
    return msg


CFG = {"aud": AUD, "email": EMAIL, "team_domain": TEAM}


class Verifier(unittest.TestCase):
    def setUp(self):
        self.key, self.other = TestKey(1), TestKey(2)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.keys = write_keys(self.dir, self.key.jwk())

    def who(self, *tokens, keys=None):
        return ca.identity(headers(*tokens), CFG, keys or self.keys, NOW)

    def test_rfc7515_a2_known_answer(self):
        self.assertTrue(ca.rs256_valid(RFC_INPUT, RFC_SIG, (RFC_N, 65537)))
        self.assertFalse(ca.rs256_valid(RFC_INPUT[:-1] + b"R", RFC_SIG, (RFC_N, 65537)))
        self.assertFalse(ca.rs256_valid(RFC_INPUT, RFC_SIG[:-1] + bytes([RFC_SIG[-1] ^ 1]), (RFC_N, 65537)))
        self.assertFalse(ca.rs256_valid(RFC_INPUT, RFC_SIG[1:], (RFC_N, 65537)))
        self.assertFalse(ca.rs256_valid(RFC_INPUT, RFC_N.to_bytes(256, "big"), (RFC_N, 65537)))  # s >= n

    def test_only_a_signed_token_with_matching_claims_and_a_known_kid_is_an_identity(self):
        good = self.key.token()
        self.assertEqual(self.who(good), EMAIL)
        head, payload, sig = good.split(".")
        forged = json.loads(base64.urlsafe_b64decode(payload + "=="))
        forged["exp"] += 1
        self.assertTrue(ca.claims_ok(headers(make_token(now=NOW)), CFG, NOW))  # the unsigned tripwire token passes the claims...
        for token in (make_token(now=NOW),  # ...but is never an identity
                      head + "." + _b64(json.dumps(forged).encode()) + "." + sig,  # payload changed after signing
                      self.other.token(),  # same kid, another key
                      self.key.token(kid="kid-unknown"),
                      self.key.token(header={"alg": "RS256", "typ": "JWT"}),  # no kid
                      self.key.token(header={"alg": "HS256", "kid": KID}),
                      self.key.token(header={"alg": "none", "kid": KID}),
                      head + "." + payload + ".",
                      head + "." + payload + "." + sig[:-4],
                      self.key.token(exp=NOW - 1),
                      self.key.token(email="someone@example.test"),
                      self.key.token(aud=["f" * 64]),
                      self.key.token(iss="https://" + "other.cloudflareaccess.com")):
            with self.subTest(token=token[-12:]):
                self.assertIsNone(self.who(token))
        self.assertIsNone(self.who(good, good))  # exactly one header
        self.assertIsNone(self.who())

    def test_rotated_keys_are_picked_up_without_a_restart(self):
        token = self.other.token(kid="kid-2")
        self.assertIsNone(self.who(token))
        write_keys(self.dir, self.key.jwk(), self.other.jwk("kid-2"))
        self.assertEqual(self.who(token), EMAIL)

    def test_the_keys_file_is_owner_only_bounded_and_strict(self):
        ec = {"kid": "ec", "kty": "EC", "crv": "P-256", "x": "AA", "y": "AA"}
        ok = write_keys(self.dir, ec, self.key.jwk(), extra={"public_cert": {"kid": KID, "cert": "x"}}, name="ok.json")
        self.assertEqual(list(ca.load_keys(ok)), [KID])  # Cloudflare's extra members and non-RSA keys are ignored
        link = self.dir / "link.json"
        link.symlink_to(self.keys)
        small = TestKey(3, bits=1024).jwk()
        bad = [write_keys(self.dir, self.key.jwk(), mode=0o640, name="mode.json"),
               write_keys(self.dir, small, name="small.json"),
               write_keys(self.dir, self.key.jwk(), self.other.jwk(), name="dupe.json"),
               write_keys(self.dir, ec, name="ec.json"),
               write_keys(self.dir, name="none.json"),
               write_keys(self.dir, {**self.key.jwk(), "n": "not base64!"}, name="n.json"),
               write_keys(self.dir, self.key.jwk(), extra={"pad": "x" * ca.MAX_KEYS_FILE}, name="big.json"),
               link, self.dir / "missing.json"]
        for path in bad:
            with self.subTest(path=path.name), self.assertRaisesRegex(ValueError, "access_keys_invalid"):
                ca.load_keys(path)
        with self.assertRaisesRegex(ValueError, "access_keys_invalid"):
            self.who(self.key.token(), keys=self.dir / "missing.json")


class HostedDecisions(Apply):
    """records panel --export-root DIR --run-id auto --decisions-dir J --public-origin URL --access-config C --access-keys K"""

    def setUp(self):
        super().setUp()
        self.key, self.other = TestKey(1), TestKey(2)
        self.cfg = write_config(self.tmp)
        self.keys = write_keys(self.tmp, self.key.jwk())

    def up(self, **kw):
        super().up(public_origin=PUBLIC, access_config=str(self.cfg), access_clock=lambda: NOW,
                   access_keys=str(self.keys), **kw)

    def public(self, method, path, token=None, host=HOST, origin=PUBLIC, **fields):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        head = {"Host": host, "Content-Type": "application/x-www-form-urlencoded"}
        head.update({"Origin": origin} if origin else {})
        head.update({HDR: token} if token else {})
        body = urllib.parse.urlencode({"csrf_token": self.server.csrf_token, **fields}) if method == "POST" else None
        try:
            connection.request(method, path, body, head)
            response = connection.getresponse()
            return response.status, response.getheader("Location"), response.read().decode()
        finally:
            connection.close()

    def test_a_signed_access_decision_is_journaled_with_its_identity_and_applied_by_the_next_run(self):
        self.make_export()
        self.up()
        token = self.key.token()
        status, location, _ = self.public("POST", "/approvals/reject", token, id=self.pending_id, reason="not ours",
                                          **{"return": "review"})
        self.assertEqual((status, location), (303, "/review?id=" + self.pending_id))
        revision = self.packet_revision(self.proposed)
        self.assertEqual(self.public("POST", "/packets/reject", token, id=self.proposed, revision=revision,
                                     reason="not yet")[0], 303)
        reject, packet = self.lines()
        actor = ap.access_actor(EMAIL)
        self.assertEqual([(e["action"], e["actor"]) for e in (reject, packet)],
                         [("reject", actor), ("packet_reject", actor)])
        self.assertIn("pending apply: reject", self.public("GET", "/review?id=" + self.pending_id, token)[2])
        self.assertEqual(self.row(self.pending_id)["status"], "pending")  # nothing reaches the live project yet
        self.assertEqual(self.apply(), ["APPLIED", "APPLIED"])  # the next records run
        row = self.row(self.pending_id)
        self.assertEqual((row["status"], row["reason"]), ("rejected", "not ours"))
        rejected = [json.loads(line) for line in (self.live / "approvals" / "queue.jsonl").read_text().splitlines()][-1]
        self.assertEqual((rejected["event"], rejected["actor"]), ("reject", actor))
        self.assertEqual(next(r for r in pp.status(self.live)["packets"] if r["id"] == self.proposed)["status"], "rejected")
        self.make_export(RUN + "-2")
        page = self.public("GET", "/review?id=" + self.pending_id, token)[2]
        self.assertIn("Rejected: not ours", page)
        self.assertNotIn("pending apply", page)

    def test_an_approve_still_needs_g_signature_which_the_apply_verifies(self):
        self.make_export()
        self.up()
        token = self.key.token()
        self.assertEqual(self.public("POST", "/approvals/approve", token, id=self.pending_id, signature="junk")[0], 303)
        payload = self.exported("queue.json", self.pending_id)
        self.assertEqual(self.public("POST", "/approvals/approve", token, id=self.pending_id,
                                     signature=self.sign(payload))[0], 303)
        self.assertEqual(self.apply(), ["SIGNATURE_REFUSED", "APPLIED"])  # an Access login alone never approves
        self.assertEqual(self.row(self.pending_id)["status"], "approved")

    def test_anonymous_forged_and_loopback_posts_are_refused_and_journal_nothing(self):
        self.make_export()
        self.up()
        loop = "127.0.0.1:%d" % self.server.server_port
        fields = {"id": self.pending_id, "reason": "forged"}
        unsigned = make_token(now=NOW)
        self.assertEqual(self.public("GET", "/board", unsigned)[0], 200)  # the read-only tripwire is unchanged
        for label, token, host, origin, status in (
                ("anonymous", None, HOST, PUBLIC, 403),
                ("unsigned claims", unsigned, HOST, PUBLIC, 403),
                ("another key", self.other.token(), HOST, PUBLIC, 403),
                ("expired", self.key.token(exp=NOW - 1), HOST, PUBLIC, 403),
                ("loopback", None, loop, "http://" + loop, 403),
                ("loopback with a signed token", self.key.token(), loop, "http://" + loop, 403),
                ("no origin", self.key.token(), HOST, None, 403)):
            for path, extra in (("/approvals/reject", fields), ("/packets/reject", {"id": self.proposed, "revision": "0",
                                                                                    "reason": "x"}),
                                ("/public-updates/kill", {})):
                with self.subTest(label=label, path=path):
                    self.assertEqual(self.public("POST", path, token, host, origin, **extra)[0], status)
        for token, host, origin in ((unsigned, HOST, PUBLIC), (None, loop, "http://" + loop)):
            self.assertIn("need the Access sign-in", self.public("POST", "/approvals/reject", token, host, origin, **fields)[2])
        self.assertEqual(self.lines(), [])
        self.assertEqual(self.row(self.pending_id)["status"], "pending")

    def test_send_stays_404(self):
        self.make_export()
        self.up()
        token = self.key.token()
        self.assertEqual(self.public("GET", "/send?id=" + self.approved_id, token)[0], 404)
        self.assertEqual(self.public("POST", "/send", token, id=self.approved_id, fingerprint="x", signature="x", to="x",
                                     confirm="x")[0], 404)
        self.assertEqual(self.lines(), [])

    def test_keys_are_required_checked_at_start_and_re_read_per_post(self):
        self.make_export()
        cases = ((dict(access_keys=None), "access_keys_required"),
                 (dict(access_keys=str(write_keys(self.tmp, self.key.jwk(), mode=0o644, name="open.json"))),
                  "access_keys_invalid"))
        for kw, code in cases:
            with self.subTest(code=code), self.assertRaises(ap.PanelError) as raised:
                ap.build_server(export_root=str(self.root), run_id="auto", actor="operator", system=False,
                                decisions_dir=str(self.j), public_origin=PUBLIC, access_config=str(self.cfg),
                                **{"access_keys": str(self.keys), **kw})
            self.assertEqual(raised.exception.code, code)
        with self.assertRaises(ap.PanelError) as raised:
            ap.build_server(export_root=str(self.root), run_id="auto", actor="operator", system=False,
                            access_keys=str(self.keys))
        self.assertEqual(raised.exception.code, "access_keys_unused")
        self.up()
        os.chmod(self.keys, 0o644)  # a damaged keys file refuses every decision until it is fixed
        status, _, page = self.public("POST", "/approvals/reject", self.key.token(), id=self.pending_id, reason="x")
        self.assertEqual(status, 503)
        self.assertIn("nothing was saved", page)
        write_keys(self.tmp, self.other.jwk("kid-2"))  # a rotation: no restart needed
        self.assertEqual(self.public("POST", "/approvals/reject", self.key.token(), id=self.pending_id, reason="x")[0], 403)
        self.assertEqual(self.public("POST", "/approvals/reject", self.other.token(kid="kid-2"), id=self.pending_id,
                                     reason="x")[0], 303)
        self.assertEqual(len(self.lines()), 1)

    def test_the_command_line_takes_the_keys_file(self):
        err = io.StringIO()
        with redirect_stderr(err):
            code = ap.main(["--export-root", str(self.root), "--run-id", "auto", "--decisions-dir", str(self.j),
                            "--actor", "G", "--public-origin", PUBLIC, "--access-config", str(self.cfg), "--no-system"])
        self.assertEqual((code, err.getvalue()), (2, "access_keys_required\n"))


if __name__ == "__main__":
    unittest.main()
