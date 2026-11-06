import http.client
import http.server
import io
import socket
import threading
import unittest
from unittest import mock

from campaign_tool.records import console_edge_smoke as smoke

HOST = "admin.example.test"
URL = "https://" + HOST
ACCESS = "https://team-x.cloudflareaccess.com/cdn-cgi/access/login"
BODY_MARKER = b"SECRET-BODY-MARKER"
CSP = "default-src 'none'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"


def redirect_all(method, path, headers):
    return 302, {"Location": ACCESS}, b""


class Stub:
    def __init__(self, behave):
        self.log = []
        stub = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args):
                pass

            def handle_any(self):
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    self.rfile.read(n)
                stub.log.append((self.command, self.path, dict(self.headers)))
                status, extra, body = behave(self.command, self.path, dict(self.headers))
                self.send_response(status)
                for k, v in extra.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            do_GET = do_HEAD = do_POST = do_OPTIONS = handle_any

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def factory(self, scheme, host, timeout):
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)


def run(factory, *args):
    out = io.StringIO()
    with mock.patch.object(smoke, "_connect", factory):
        code = smoke.main([URL, *args], out)
    return code, out.getvalue()


def run_stub(behave, *args):
    with Stub(behave) as stub:
        code, text = run(stub.factory, *args)
    return code, text, stub


class AccessSweepTests(unittest.TestCase):
    def test_all_redirect_to_access_passes(self):
        code, text, stub = run_stub(redirect_all)
        self.assertEqual(code, 0, text)
        self.assertTrue(text.rstrip().endswith("PASS"))

    def test_one_200_fails_naming_method_path(self):
        def behave(m, p, h):
            return (200, {}, b"") if (m, p) == ("GET", "/documents") else redirect_all(m, p, h)
        code, text, _ = run_stub(behave)
        self.assertEqual(code, 1)
        self.assertIn("GET /documents 200 - FAIL", text)
        self.assertIn("FAIL 2 probes", text)

    def test_fingerprints_fail(self):
        for header in ({"Content-Security-Policy": CSP}, {"Server": "RecordsPanel/1"},
                       {"x-deflock-write-fence": "1"}):
            with self.subTest(header=header):
                code, text, _ = run_stub(lambda m, p, h: (302, {"Location": ACCESS, **header}, b""))
                self.assertEqual(code, 1)
                self.assertIn("FAIL", text)

    def test_foreign_redirect_hosts_fail(self):
        for target in ("https://evil.example.test/", "https://cloudflareaccess.com.evil.test/"):
            with self.subTest(target=target):
                code, text, _ = run_stub(lambda m, p, h: (302, {"Location": target}, b""))
                self.assertEqual(code, 1)

    def test_404_fails_in_access_mode(self):
        code, text, _ = run_stub(lambda m, p, h: (404, {}, b""))
        self.assertEqual(code, 1)

    def test_401_403_pass(self):
        for status in (401, 403):
            code, text, _ = run_stub(lambda m, p, h: (status, {}, b""))
            self.assertEqual(code, 0, text)

    def test_http_probe_may_redirect_to_same_path_on_https(self):
        def behave(m, p, h):
            return 301, {"Location": URL + "/board"}, b""
        with Stub(behave) as stub:
            with mock.patch.object(smoke, "_connect", stub.factory):
                status, headers = smoke._send("http", HOST, "GET", "/board", None, {}, 5)
        self.assertEqual(smoke.judge("access", HOST, "http", "/board", status, headers), "ok")
        self.assertEqual(smoke.judge("access", HOST, "http", "/", status, headers), "FAIL")

    def test_connection_error_fails(self):
        def refuse(scheme, host, timeout):
            raise ConnectionRefusedError()
        code, text = run(refuse)
        self.assertEqual(code, 1)
        self.assertIn("ERR:ConnectionRefusedError", text)

    def test_no_redirect_followed_and_junk_round_sent(self):
        code, text, stub = run_stub(redirect_all)
        probes = smoke._probes()
        self.assertEqual(len(stub.log), len(probes))
        junk = [e for e in stub.log if e[2].get("Cookie") == "CF_Authorization=x.y.z"]
        self.assertEqual(len(junk), (len(probes) - 2) // 2)
        self.assertTrue(all(e[2].get("Cf-Access-Jwt-Assertion") == "x.y.z" for e in junk))
        anon = [e for e in stub.log if "Cookie" not in e[2]]
        self.assertEqual(len(anon), len(probes) - len(junk))

    def test_probe_list_matches_plan(self):
        probes = smoke._probes()
        self.assertEqual(len([p for p in probes if p[1] == "POST"]), 14)
        self.assertEqual([p[3] for p in probes if p[1] == "POST"][0], "a=b")
        self.assertEqual([(p[0], p[2]) for p in probes[-2:]], [("http", "/"), ("http", "/board")])

    def test_output_never_contains_body(self):
        code, text, _ = run_stub(lambda m, p, h: (200, {}, BODY_MARKER))
        self.assertEqual(code, 1)
        self.assertNotIn(BODY_MARKER.decode(), text)


class IsolationTests(unittest.TestCase):
    def test_refused_is_unreachable_and_passes(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()

        def factory(scheme, host, timeout):
            return http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        code, text = run(factory, "--mode", "isolation")
        self.assertEqual(code, 0, text)
        self.assertTrue(text.startswith("ISOLATION CHECK (not an Access check)\n"))
        self.assertIn("unreachable", text)

    def test_cloudflare_5xx_counts_as_isolated(self):
        code, text, _ = run_stub(lambda m, p, h: (530, {}, b"error 1033"), "--mode", "isolation")
        self.assertEqual(code, 0, text)

    def test_fingerprint_200_fails(self):
        code, text, _ = run_stub(lambda m, p, h: (200, {"Server": "RecordsPanel"}, b""), "--mode", "isolation")
        self.assertEqual(code, 1)

    def test_plain_200_fails(self):
        code, text, _ = run_stub(lambda m, p, h: (200, {}, b""), "--mode", "isolation")
        self.assertEqual(code, 1)


class CliTests(unittest.TestCase):
    def check(self, *argv):
        out = io.StringIO()
        with mock.patch.object(smoke, "_connect", side_effect=AssertionError("no network on bad usage")):
            self.assertEqual(smoke.main(list(argv), out), 2, out.getvalue())

    def test_bad_usage_exits_2(self):
        self.check("http://" + HOST)
        self.check(URL + "/path")
        self.check(URL + ":8443")
        self.check("https://Admin.Example.test")
        self.check(URL, "--cookie", "a=b")
        self.check(URL, "--header", "X: y")
        self.check(URL, "--mode", "bogus")
        self.check(URL, "extra")
        self.check()


if __name__ == "__main__":
    unittest.main()
