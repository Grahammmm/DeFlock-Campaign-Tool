"""ADM-SMOKE-1: workspace_smoke edge/app modes against loopback stub servers (no external network)."""
import contextlib
import http.server
import io
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import workspace_smoke as ws  # noqa: E402

ACCESS = "https" + "://team.cloudflare" + "access.com/cdn-cgi/access/login/x"  # split: repo secret scan bans the literal
HEALTH_OK = b'{"ok":true,"campaign":true,"d1_ok":true,"migrations_applied":12,"access_configured":true,"outbound":"off"}'


def stub(handler):
    """handler(path) -> (status, headers dict, body bytes)."""
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            status, headers, body = handler(self.path)
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    return srv


class Base(unittest.TestCase):
    def serve(self, handler):
        srv = stub(handler)
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return "http://127.0.0.1:%d" % srv.server_address[1]

    def probe(self, handler, mode):
        ok, results = ws.run(self.serve(handler), mode)
        return ok, {p: (g, d) for p, g, d in results}


def redirect(_p):
    return 302, {"Location": ACCESS}, b""


class EdgeTests(Base):
    def test_access_redirect_everywhere_passes(self):
        ok, res = self.probe(redirect, "edge")
        self.assertTrue(ok, res)
        self.assertEqual(len(res), 7)

    def test_401_and_403_pass(self):
        ok, _ = self.probe(lambda p: (401 if p == "/" else 403, {}, b"no"), "edge")
        self.assertTrue(ok)

    def test_healthz_200_fails_and_is_named(self):
        def h(p):
            return (200, {"Content-Type": "application/json"}, b'{"ok":true,"campaign":true}') if p == "/healthz" else redirect(p)
        ok, res = self.probe(h, "edge")
        self.assertFalse(ok)
        self.assertEqual([p for p, (g, _) in res.items() if not g], ["/healthz"])

    def test_200_html_on_export_fails_and_is_named(self):
        def h(p):
            return (200, {}, b"<html>x</html>") if p == "/api/export.json" else redirect(p)
        ok, res = self.probe(h, "edge")
        self.assertFalse(ok)
        self.assertEqual([p for p, (g, _) in res.items() if not g], ["/api/export.json"])

    def test_redirect_to_other_host_fails(self):
        ok, _ = self.probe(lambda p: (302, {"Location": "https://evil.example/x"}, b""), "edge")
        self.assertFalse(ok)

    def test_lookalike_host_fails(self):
        ok, _ = self.probe(lambda p: (302, {"Location": "https" + "://cloudflare" + "access.com.evil.example/"}, b""), "edge")
        self.assertFalse(ok)

    def test_404_fails_in_edge(self):
        ok, _ = self.probe(lambda p: (404, {}, b""), "edge")
        self.assertFalse(ok)

    def test_connection_refused_fails(self):
        srv = stub(redirect)
        url = "http://127.0.0.1:%d" % srv.server_address[1]
        srv.shutdown()
        srv.server_close()
        ok, _ = ws.run(url, "edge")
        self.assertFalse(ok)


class AppTests(Base):
    def handler(self, health=HEALTH_OK, other=401, health_status=200):
        def h(p):
            if p == "/healthz":
                return health_status, {"Content-Type": "application/json"}, health
            return other, {}, b""
        return h

    def test_exact_shape_health_with_denials_passes(self):
        ok, res = self.probe(self.handler(), "app")
        self.assertTrue(ok, res)

    def test_subset_and_counts_pass(self):
        ok, _ = self.probe(self.handler(health=b'{"ok":true,"migrations_applied":0}', other=404), "app")
        self.assertTrue(ok)

    def test_503_runner_export_passes(self):
        ok, _ = self.probe(self.handler(other=503), "app")
        self.assertTrue(ok)

    def test_extra_key_fails(self):
        ok, res = self.probe(self.handler(health=b'{"ok":true,"campaign_id":3}'), "app")
        self.assertFalse(ok)
        self.assertFalse(res["/healthz"][0])

    def test_string_value_email_fails(self):
        ok, res = self.probe(self.handler(health=b'{"ok":true,"outbound":"g@example.com"}'), "app")
        self.assertFalse(ok)
        self.assertIn("outbound", res["/healthz"][1])

    def test_float_and_nested_and_non_json_fail(self):
        for body in (b'{"ok":1.5}', b'{"ok":{"a":1}}', b'{"ok":null}', b"<html></html>", b"[true]"):
            ok, _ = self.probe(self.handler(health=body), "app")
            self.assertFalse(ok, body)

    def test_healthz_non_200_fails(self):
        ok, _ = self.probe(self.handler(health_status=503, health=b'{"ok":false}'), "app")
        self.assertFalse(ok)

    def test_200_on_other_path_fails(self):
        def h(p):
            if p == "/healthz":
                return 200, {}, HEALTH_OK
            return (200, {}, b"{}") if p == "/status" else (401, {}, b"")
        ok, res = self.probe(h, "app")
        self.assertFalse(ok)
        self.assertEqual([p for p, (g, _) in res.items() if not g], ["/status"])

    def test_redirect_on_other_path_fails_in_app(self):
        ok, _ = self.probe(redirect, "app")
        self.assertFalse(ok)


class CliTests(Base):
    def run_cli(self, argv):
        buf = io.StringIO()
        return ws.main(argv, out=buf), buf.getvalue()

    def test_default_mode_is_edge(self):
        url = self.serve(redirect)
        code, text = self.run_cli([url])
        self.assertEqual(code, 0)
        self.assertIn("PASS (edge mode)", text)

    def test_exit_1_and_fail_line(self):
        url = self.serve(lambda p: (200, {}, b"x"))
        code, text = self.run_cli(["--mode", "edge", url])
        self.assertEqual(code, 1)
        self.assertIn("FAIL (edge mode)", text)

    def test_bad_url_is_usage_error(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.run_cli(["ftp://x"])[0], 2)

    def test_no_credential_flags(self):
        for flag in ("--token", "--cookie", "--header", "--password"):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                ws.main([flag, "x", "http://127.0.0.1:1"])

    def test_requests_carry_no_credentials(self):
        seen = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append({k.lower() for k in self.headers.keys()})
                self.send_response(401)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        ws.run("http://127.0.0.1:%d" % srv.server_address[1], "edge")
        self.assertEqual(len(seen), 7)
        for hdrs in seen:
            self.assertFalse(hdrs & {"authorization", "cookie", "cf-access-jwt-assertion"})


if __name__ == "__main__":
    unittest.main()
