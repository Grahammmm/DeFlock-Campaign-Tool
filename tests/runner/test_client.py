"""HTTP client: bearer header, retries, 204, hash checks and site-path safety, all over a fake opener."""
import hashlib
import io
import json
import unittest
import urllib.error

from runner.client import FakeWorkspace, Workspace, WorkspaceError, safe_site_path


class FakeResponse:
    def __init__(self, status, body=b"", headers=None):
        self.status = status
        self._body = body
        self.headers = headers or {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Opener:
    """Scripted responses; records every request (method, path, headers, body)."""

    def __init__(self, script):
        self.script = list(script)
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append({"method": request.get_method(), "url": request.full_url,
                              "headers": {k.lower(): v for k, v in request.header_items()}, "body": request.data})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def http_error(code, body=b"{}"):
    return urllib.error.HTTPError("https://workspace.example.invalid/x", code, "err", {}, io.BytesIO(body))


def client(opener, **kw):
    sleeps = []
    ws = Workspace("https://workspace.example.invalid", "tok-synthetic", sleep=sleeps.append, opener=opener, backoff_base=0.5, **kw)
    return ws, sleeps


class ClientTests(unittest.TestCase):
    def test_constructor_validation(self):
        with self.assertRaises(ValueError):
            Workspace("ftp://x", "tok")
        with self.assertRaises(ValueError):
            Workspace("https://x", "")

    def test_bearer_header_and_204_idle(self):
        opener = Opener([FakeResponse(204)])
        ws, _ = client(opener)
        self.assertIsNone(ws.lease_job(120))
        req = opener.requests[0]
        self.assertEqual(req["method"], "GET")
        self.assertTrue(req["url"].endswith("/api/runner/jobs?lease=120"))
        self.assertEqual(req["headers"]["authorization"], "Bearer tok-synthetic")

    def test_5xx_retries_with_backoff_then_raises(self):
        opener = Opener([http_error(503), http_error(502), FakeResponse(200, b'{"job_id": "job_1"}')])
        ws, sleeps = client(opener)
        self.assertEqual(ws.lease_job()["job_id"], "job_1")
        self.assertEqual(sleeps, [0.5, 1.0])
        opener = Opener([http_error(500)] * 3)
        ws, sleeps = client(opener, max_retries=2)
        with self.assertRaises(WorkspaceError) as ctx:
            ws.lease_job()
        self.assertEqual(ctx.exception.status, 500)
        self.assertTrue(ctx.exception.retryable)
        self.assertEqual(len(opener.requests), 3)

    def test_4xx_is_not_retried(self):
        opener = Opener([http_error(409, b'{"error":"job is done, not leased"}')])
        ws, sleeps = client(opener)
        with self.assertRaises(WorkspaceError) as ctx:
            ws.post_result("job_1", "done", {})
        self.assertEqual(ctx.exception.status, 409)
        self.assertFalse(ctx.exception.retryable)
        self.assertEqual(sleeps, [])
        with self.assertRaises(ValueError):
            ws.post_result("job_1", "weird")

    def test_connection_errors_retry_then_unreachable(self):
        opener = Opener([urllib.error.URLError("refused")] * 2 + [FakeResponse(204)])
        ws, sleeps = client(opener)
        self.assertIsNone(ws.lease_job())
        self.assertEqual(len(sleeps), 2)
        opener = Opener([TimeoutError()] * 5)
        ws, _ = client(opener, max_retries=1)
        with self.assertRaises(WorkspaceError) as ctx:
            ws.lease_job()
        self.assertEqual(ctx.exception.status, 0)
        self.assertTrue(ctx.exception.retryable)

    def test_get_original_verifies_hash(self):
        data = b"synthetic original"
        sha = hashlib.sha256(data).hexdigest()
        opener = Opener([FakeResponse(200, data, {"Content-Type": "text/plain"}), FakeResponse(200, b"tampered", {})])
        ws, _ = client(opener)
        payload, media = ws.get_original(sha)
        self.assertEqual((payload, media), (data, "text/plain"))
        with self.assertRaises(WorkspaceError):
            ws.get_original(sha)
        with self.assertRaises(ValueError):
            ws.get_original("nothex")

    def test_put_original_posts_bytes_by_hash(self):
        data = b"%PDF-1.4 synthetic"
        sha = hashlib.sha256(data).hexdigest()
        opener = Opener([FakeResponse(201, json.dumps({"sha256": sha, "byte_count": len(data), "created": True}).encode())])
        ws, _ = client(opener)
        result = ws.put_original(data, "application/pdf")
        self.assertEqual(result["sha256"], sha)
        req = opener.requests[0]
        self.assertEqual(req["method"], "PUT")
        self.assertTrue(req["url"].endswith("/api/runner/originals/" + sha))
        self.assertEqual(req["headers"]["content-type"], "application/pdf")
        self.assertEqual(req["body"], data)

    def test_put_site_file_sends_hash_header_and_refuses_unsafe_paths(self):
        html = b"<!doctype html><title>synthetic</title>"
        sha = hashlib.sha256(html).hexdigest()
        opener = Opener([FakeResponse(201, json.dumps({"key": "sites/v1/index.html", "sha256": sha, "bytes": len(html)}).encode()),
                         FakeResponse(201, json.dumps({"key": "sites/v1/x.css", "sha256": "0" * 64, "bytes": 1}).encode())])
        ws, _ = client(opener)
        result = ws.put_site_file("v1", "index.html", html, "text/html; charset=utf-8")
        self.assertEqual(result["sha256"], sha)
        req = opener.requests[0]
        self.assertTrue(req["url"].endswith("/api/runner/site/v1/index.html"))
        self.assertEqual(req["headers"]["x-object-sha256"], sha)
        self.assertEqual(req["headers"]["content-type"], "text/html; charset=utf-8")
        # a workspace that reports a different hash is an error, not silently accepted
        with self.assertRaises(WorkspaceError):
            ws.put_site_file("v1", "x.css", b"a{}", "text/css")
        for bad_version in ("", "V1", "v 1", "../v1", "v" * 65):
            with self.assertRaises(ValueError):
                ws.put_site_file(bad_version, "index.html", html, "text/html")
        for bad in ("", "/index.html", "../index.html", "findings/../x.html", ".hidden", "run.sh", "notes", "x.php", "a\\b.html"):
            with self.assertRaises(ValueError, msg=bad):
                ws.put_site_file("v1", bad, html, "text/html")
        with self.assertRaises(ValueError):
            ws.put_site_file("v1", "big.js", b"x" * (16 * 1024 * 1024 + 1), "text/javascript")
        # none of the refused calls reached the network
        self.assertEqual(len(opener.requests), 2)

    def test_safe_site_path_mirrors_worker_rules(self):
        for ok in ("index.html", "findings/example.html", "map/cameras.geojson", "vendor/maplibre-gl.js", "_headers", "assets/Logo.PNG", "feed.xml", "robots.txt"):
            self.assertEqual(safe_site_path(ok), ok)
        for bad in ("", "/index.html", "a//b.html", "../index.html", "./index.html", ".hidden", "index.html/", "run.sh", "setup.exe",
                    "notes", "dir/noext", "x.htm.bak", "x.sqlite", "secrets.key", "a" * 130 + ".html", "/".join(["d"] * 17) + "/x.html", None, 3):
            self.assertIsNone(safe_site_path(bad), bad)

    def test_fake_workspace_rejects_unsafe_site_paths_like_the_worker(self):
        ws = FakeWorkspace()
        with self.assertRaises(WorkspaceError):
            ws.put_site_file("v1", "run.sh", b"x", "text/plain")
        with self.assertRaises(WorkspaceError):
            ws.put_site_file("v1", "../x.html", b"x", "text/html")
        self.assertEqual(ws.put_site_file("v1", "_headers", b"/*\n", "text/plain")["key"], "sites/v1/_headers")


if __name__ == "__main__":
    unittest.main()
