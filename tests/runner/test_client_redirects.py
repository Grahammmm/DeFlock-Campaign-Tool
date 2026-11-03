"""Runner transport never follows redirects, so RUNNER_TOKEN cannot reach another origin.

All listeners are loopback and all payloads synthetic. The HTTPS leg is a stub
https_open so no DNS lookup, TLS handshake or outside connection happens.
"""
import email.message
import hashlib
import io
import os
import threading
import unittest
import urllib.parse
import urllib.request
import urllib.response
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from runner.client import Workspace, WorkspaceError

TOKEN = "synthetic-runner-token"
SHA = hashlib.sha256(b"synthetic original\n").hexdigest()


class Loopback:
    """Records every request; answers `status` with an optional Location header."""

    def __init__(self, status=200, location=None):
        self.requests = []
        endpoint = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                endpoint.requests.append((self.path, self.headers.get("Authorization")))
                body = b"synthetic original\n"
                self.send_response(status)
                if location:
                    self.send_header("Location", location + self.path)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


def fetches(client):
    """The three token-bearing reads the hosted export and handlers use."""
    return {"export": lambda: client.get("/api/runner/export.json"),
            "original_via_get": lambda: client.get("/api/runner/originals/" + SHA),
            "get_original": lambda: client.get_original(SHA)}


class RedirectTests(unittest.TestCase):
    def setUp(self):
        # No environment proxy may stand in for the loopback listeners.
        env = patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def endpoint(self, **kwargs):
        endpoint = Loopback(**kwargs)
        self.addCleanup(endpoint.close)
        return endpoint

    def client(self, url):
        return Workspace(url, TOKEN, timeout=5, sleep=lambda _s: None)

    def test_cross_origin_redirect_is_refused_and_token_never_reaches_other_origin(self):
        sink = self.endpoint()  # another origin: same host, different port
        for status in (301, 302, 303, 307, 308):
            source = self.endpoint(status=status, location=sink.url)
            for name, fetch in fetches(self.client(source.url)).items():
                with self.subTest(status=status, fetch=name):
                    with self.assertRaises(WorkspaceError) as caught:
                        fetch()
                    self.assertEqual(caught.exception.status, status)
                    self.assertFalse(caught.exception.retryable)
            # The workspace got exactly one request per fetch (no retry) and it carried the token.
            self.assertEqual([auth for _, auth in source.requests], ["Bearer " + TOKEN] * 3)
        self.assertEqual(sink.requests, [], "redirect target received a runner request")

    def test_same_origin_redirect_is_not_followed_either(self):
        source = self.endpoint(status=302, location="")  # relative Location back to the same path
        with self.assertRaises(WorkspaceError) as caught:
            self.client(source.url).get("/api/runner/export.json")
        self.assertEqual(caught.exception.status, 302)
        self.assertEqual(len(source.requests), 1)

    def test_https_to_http_downgrade_is_refused(self):
        sink = self.endpoint()

        def https_open(handler, request):
            # Stub HTTPS workspace: redirect to plaintext http on another origin.
            headers = email.message.Message()
            headers["Location"] = sink.url + urllib.parse.urlsplit(request.full_url).path
            response = urllib.response.addinfourl(io.BytesIO(b""), headers, request.full_url, 302)
            response.msg = "Found"
            seen.append(request.get_header("Authorization"))
            return response

        for name in ("export", "original_via_get", "get_original"):
            seen = []
            with self.subTest(fetch=name), patch.object(urllib.request.HTTPSHandler, "https_open", https_open):
                client = self.client("https://workspace.example.invalid")
                with self.assertRaises(WorkspaceError) as caught:
                    fetches(client)[name]()
                self.assertEqual(caught.exception.status, 302)
                self.assertEqual(seen, ["Bearer " + TOKEN])
        self.assertEqual(sink.requests, [], "plaintext redirect target received a runner request")

    def test_plain_success_still_works_over_default_transport(self):
        source = self.endpoint()
        payload, _media_type = self.client(source.url).get_original(SHA)
        self.assertEqual(payload, b"synthetic original\n")
        self.assertEqual(source.requests, [("/api/runner/originals/" + SHA, "Bearer " + TOKEN)])


if __name__ == "__main__":
    unittest.main()
