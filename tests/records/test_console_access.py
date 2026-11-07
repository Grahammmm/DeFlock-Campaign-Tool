"""Synthetic acceptance for the public-Host access-claims tripwire (never real AUD, email or team)."""
import base64
import contextlib
import io
import json
import os
import socket
import tempfile
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

from campaign_tool.records import admin_panel as ap
from tests.records.test_admin_panel import ServerCase, RUN, journal

PUBLIC = "https://admin.example.test"
HOST = "admin.example.test"
AUD = "ab12" * 16
EMAIL = "Operator@Example.test"
TEAM = "team-x.cloudflareaccess.com"
NOW = 1_800_000_000
HDR = "Cf-Access-Jwt-Assertion"


def b64(data):
    return base64.urlsafe_b64encode(data if isinstance(data, bytes) else json.dumps(data).encode()).rstrip(b"=").decode()


def write_config(tmpdir, aud=AUD, email=EMAIL, team=TEAM, mode=0o600, name="access.json"):
    path = Path(tmpdir) / name
    path.write_text(json.dumps({"aud": aud, "email": email, "team_domain": team}))
    os.chmod(path, mode)
    return path


def make_token(aud=AUD, email=EMAIL, team=TEAM, now=NOW, header=None, drop=(), raw_payload=None, **overrides):
    payload = {"aud": aud, "iss": "https://" + team, "exp": now + 3600, "iat": now - 10, "email": email}
    payload.update(overrides)
    for key in drop:
        payload.pop(key, None)
    head = {"alg": "RS256", "typ": "JWT"} if header is None else header
    return b64(head) + "." + (b64(raw_payload) if raw_payload is not None else b64(payload)) + ".c2ln"


def raw_request(port, method, path, host, origin, tokens=(), fields=None, extra=()):
    body = urlencode(fields or {}).encode()
    lines = [f"Host: {host}"] + ([f"Origin: {origin}"] if origin else []) + [f"{HDR}: {t}" for t in tokens]
    lines += list(extra) + ["Content-Type: application/x-www-form-urlencoded", f"Content-Length: {len(body)}"]
    data = (f"{method} {path} HTTP/1.1\r\n" + "\r\n".join(lines) + "\r\n\r\n").encode("utf-8") + body
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(data)
        s.shutdown(socket.SHUT_WR)
        out = b""
        while chunk := s.recv(65536):
            out += chunk
    head, _, text = out.partition(b"\r\n\r\n")
    return int(head.split()[1]), text.decode("utf-8", "replace")


class AccessTests(ServerCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.cfg = write_config(self.tmp)
        self.server = self.start(public_origin=PUBLIC, access_config=self.cfg, access_clock=lambda: NOW)
        self.port = self.http.port
        self.good = make_token()

    def req(self, method, path="/board", tokens=None, host=HOST, origin=PUBLIC, csrf=None, extra=()):
        fields = {"csrf_token": csrf or self.tok, "id": self.draft_row["id"], "reason": "not needed"} if method == "POST" else None
        return raw_request(self.port, method, path, host, origin, [self.good] if tokens is None else tokens, fields, extra)

    def both_refused(self, tokens, **kw):
        before = journal(self.project)
        self.assertEqual(self.req("GET", tokens=tokens, **kw)[0], 403)
        self.assertEqual(self.req("POST", "/approvals/reject", tokens=tokens, **kw)[0], 403)
        self.assertEqual(journal(self.project), before)
        self.assertEqual(self.row()["status"], "pending")

    def test_good_and_loopback(self):
        loop = f"127.0.0.1:{self.port}"
        self.assertEqual(self.req("GET")[0], 200)
        for tokens in ([], ["garbage"]):
            self.assertEqual(raw_request(self.port, "GET", "/board", loop, None, tokens)[0], 200)
        self.assertEqual(self.req("POST", "/approvals/reject")[0], 303)
        self.assertEqual(self.row()["status"], "rejected")

    def test_good_variants(self):
        for token in (make_token(aud=[AUD, "x"]), make_token(email=EMAIL.upper()), make_token(email=EMAIL.lower())):
            self.assertEqual(self.req("GET", tokens=[token])[0], 200)

    def test_each_bad_token_refused(self):
        parts = self.good.split(".")
        other = "cd34" * 16
        cases = {
            "none": [], "two": [self.good, self.good], "two parts": [".".join(parts[:2])],
            "four parts": [self.good + ".c2ln"], "empty part": [parts[0] + ".." + parts[2]],
            "plus": [self.good + "+"], "equals": [self.good + "="], "non-ascii": [self.good + "\u00e9"],
            "too long": ["a" * 8200 + ".YQ.YQ"], "not json": [parts[0] + "." + b64(b"nope") + ".c2ln"],
            "json list": [parts[0] + "." + b64(b"[1]") + ".c2ln"],
            "dup aud": [make_token(raw_payload=('{"aud":"x","aud":"%s","iss":"https://%s","exp":%d,"iat":1,"email":"%s"}'
                                                % (AUD, TEAM, NOW + 9, EMAIL)).encode())],
            "alg none": [make_token(header={"alg": "none"})], "alg HS256": [make_token(header={"alg": "HS256"})],
            "alg missing": [make_token(header={"typ": "JWT"})], "other aud": [make_token(aud=other)],
            "aud list without": [make_token(aud=[other, "x"])], "aud list too long": [make_token(aud=[AUD] + ["x"] * 16)],
            "iss http": [make_token(iss="http://" + TEAM)], "iss other": [make_token(iss="https://" + TEAM.replace("team-x", "t2"))],
            "exp now": [make_token(exp=NOW)], "exp past": [make_token(exp=NOW - 1)], "exp true": [make_token(exp=True)],
            "exp string": [make_token(exp="9999999999")], "iat future": [make_token(iat=NOW + 61)],
            "nbf future": [make_token(nbf=NOW + 61)], "email other": [make_token(email="o@example.test")],
            "email space": [make_token(email=EMAIL + " ")], "email missing": [make_token(drop=("email",))],
            "nan": [make_token(raw_payload=b'{"aud":"%s","exp":NaN}' % AUD.encode())],
        }
        for name, tokens in cases.items():
            with self.subTest(name):
                self.both_refused(tokens)

    def test_identity_header_never_trusted(self):
        self.both_refused([], extra=[f"Cf-Access-Authenticated-User-Email: {EMAIL}"])

    def test_refusal_echoes_nothing(self):
        bad = make_token(aud="cd34" * 16)
        status, body = self.req("GET", tokens=[bad])
        self.assertEqual(status, 403)
        for secret in bad.split(".") + [EMAIL, AUD, TEAM, "cd34" * 16]:
            self.assertNotIn(secret, body)

    def test_gate_order(self):
        before = journal(self.project)
        for kw in ({"origin": "https://other.example.test"}, {"csrf": "wrong"}, {"tokens": [make_token(aud="cd34" * 16)]}):
            with self.subTest(kw):
                self.assertEqual(self.req("POST", "/approvals/reject", **kw)[0], 403)
                self.assertEqual(journal(self.project), before)
                self.assertEqual(self.row()["status"], "pending")


class StartupTests(ServerCase):
    def run_main(self, origin, config):
        args = ["--project", str(self.project), "--run-id", RUN, "--actor", "op"]
        args += ["--public-origin", origin] if origin else []
        args += ["--access-config", str(config)] if config else []
        with mock.patch.object(ap, "build_server") as build, contextlib.redirect_stderr(io.StringIO()) as err:
            return ap.main(args), err.getvalue(), build

    def test_startup_refusals_build_nothing(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        good = write_config(tmp.name)
        link = Path(tmp.name) / "link.json"
        link.symlink_to(good)
        big = Path(tmp.name) / "big.json"
        big.write_text(json.dumps({"aud": AUD, "email": EMAIL, "team_domain": TEAM}).ljust(4097))
        os.chmod(big, 0o600)
        bad = [write_config(tmp.name, mode=0o640, name="m640"), write_config(tmp.name, mode=0o604, name="m604"), link, big,
               write_config(tmp.name, aud="ab12" * 15 + "ab1", name="a63"), write_config(tmp.name, aud=AUD.upper(), name="up"),
               write_config(tmp.name, team="team-x.example.test", name="team"),
               write_config(tmp.name, email="a@b@c", name="e1"), write_config(tmp.name, email="a<b@c", name="e2")]
        deep = Path(tmp.name) / "deep.json"
        deep.write_text("[" * 1500 + "]" * 1500)
        os.chmod(deep, 0o600)
        fifo = Path(tmp.name) / "fifo.json"
        os.mkfifo(fifo, 0o600)
        bad += [deep, fifo]
        extra = Path(tmp.name) / "extra.json"
        extra.write_text(json.dumps({"aud": AUD, "email": EMAIL, "team_domain": TEAM, "x": 1}))
        missing = Path(tmp.name) / "missing.json"
        missing.write_text(json.dumps({"aud": AUD, "email": EMAIL}))
        for path in (extra, missing):
            os.chmod(path, 0o600)
        for path in bad + [extra, missing]:
            with self.subTest(path=path.name):
                result, err, build = self.run_main(PUBLIC, path)
                self.assertEqual((result, err), (2, "access_config_invalid\n"))
                build.assert_not_called()
        self.assertEqual(self.run_main(PUBLIC, None)[:2], (2, "access_config_required\n"))
        result, err, build = self.run_main(None, good)
        self.assertEqual((result, err), (2, "access_config_unused\n"))
        build.assert_not_called()
        self.assertEqual(self.run_main("http://x.example.test", good)[:2], (2, "public_origin_invalid\n"))
        with mock.patch.object(ap.http.server, "HTTPServer") as listener:
            with self.assertRaises(ap.PanelError) as caught:
                ap.build_server(self.project, RUN, actor="op", public_origin=PUBLIC)
            self.assertEqual(caught.exception.code, "access_config_required")
            listener.assert_not_called()
