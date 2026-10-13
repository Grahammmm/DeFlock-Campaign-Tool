"""Strict model transport regressions; all listeners and payloads are synthetic."""
import json
import os
import socket
import ssl
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch

from campaign_tool.digest.model import ModelConfig, ModelError, chat_json, check_config, is_local_or_tailscale


class LoopbackEndpoint:
    def __init__(self, status=200, location=None):
        self.requests = []
        endpoint = self

        class Handler(BaseHTTPRequestHandler):
            def respond(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                endpoint.requests.append((self.command, self.path, dict(self.headers), body))
                payload = json.dumps({"choices": [{"message": {"content": '{"ok": true}'}}]}).encode()
                self.send_response(status)
                if location:
                    self.send_header("Location", location)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_POST = do_GET = respond

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class StrictLocalTransportTests(unittest.TestCase):
    def endpoint(self, **kwargs):
        endpoint = LoopbackEndpoint(**kwargs)
        self.addCleanup(endpoint.close)
        return endpoint

    def config(self, url, **kwargs):
        canary = "synthetic-key"
        return ModelConfig(url, api_key=canary, privacy_tier="strict_local",
                           timeout=2, **kwargs)

    def test_environment_proxy_never_receives_payload_or_authorization(self):
        target, proxy = self.endpoint(), self.endpoint()
        for upper in (False, True):
            env = {"http_proxy": proxy.url, "https_proxy": proxy.url,
                   "all_proxy": proxy.url, "no_proxy": ""}
            if upper:
                env = {key.upper(): value for key, value in env.items()}
            with self.subTest(upper=upper), patch.dict(os.environ, env, clear=True), \
                    patch.object(urllib.request, "_opener", None):
                self.assertEqual(chat_json(self.config(target.url), [{"role": "user", "content": "synthetic data"}]),
                                 {"ok": True})
            self.assertEqual(proxy.requests, [], "environment proxy received private model request")
        self.assertEqual(len(target.requests), 2)
        self.assertEqual(target.requests[0][1], "/v1/chat/completions")
        self.assertEqual(target.requests[0][2]["Authorization"], "Bearer synthetic-key")

    def test_redirect_never_reaches_second_destination(self):
        sink = self.endpoint()
        source = self.endpoint(status=302, location=sink.url)
        with patch.dict(os.environ, {}, clear=True), patch.object(urllib.request, "_opener", None):
            with self.assertRaises(ModelError):
                chat_json(self.config(source.url), [{"role": "user", "content": "synthetic data"}])
        self.assertEqual(sink.requests, [], "redirect destination received model authorization")
        self.assertEqual(len(source.requests), 1)

    def test_every_redirect_status_fails_without_following_location(self):
        sink = self.endpoint()
        for status in (300, 301, 303, 307, 308):
            source = self.endpoint(status=status, location=sink.url)
            with self.subTest(status=status), self.assertRaisesRegex(ModelError, "redirects are forbidden"):
                chat_json(self.config(source.url), [])
        self.assertEqual(sink.requests, [])

    def test_global_opener_cannot_redirect_transport(self):
        target = self.endpoint()
        global_opener = Mock()
        global_opener.open.side_effect = AssertionError("global opener invoked")
        with patch.object(urllib.request, "_opener", global_opener), \
                patch.object(urllib.request, "urlopen", side_effect=AssertionError("urlopen invoked")):
            self.assertEqual(chat_json(self.config(target.url), []), {"ok": True})
        global_opener.open.assert_not_called()
        self.assertEqual(len(target.requests), 1)

    def test_custom_opener_is_rejected_before_resolution_or_connection(self):
        opener = Mock()
        with patch("socket.getaddrinfo", side_effect=AssertionError("DNS invoked")), \
                patch("socket.socket", side_effect=AssertionError("socket opened")), \
                self.assertRaisesRegex(ModelError, "custom model opener"):
            chat_json(self.config("http://localhost:1234/v1"), [], opener=opener)
        opener.assert_not_called()

    @staticmethod
    def answer(ip, port=1234):
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        address = (ip, port, 0, 0) if family == socket.AF_INET6 else (ip, port)
        return family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", address

    def test_named_host_is_resolved_once_then_pinned_to_verified_address(self):
        target = self.endpoint()
        for host in ("localhost", "fixture.localhost", "fixture.tail-test.ts.net"):
            answers = [self.answer("127.0.0.1", target.server.server_port)]
            # A second lookup would simulate a rebound hostname. None is allowed.
            with self.subTest(host=host), patch("socket.getaddrinfo", side_effect=[answers, [self.answer("192.0.2.9")]]) as resolve:
                self.assertEqual(chat_json(self.config(f"http://{host}:{target.server.server_port}/v1"), []), {"ok": True})
                resolve.assert_called_once_with(host, target.server.server_port,
                                                type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
            self.assertEqual(target.requests[-1][2]["Host"], f"{host}:{target.server.server_port}")

    def test_any_forbidden_dns_result_rejects_entire_destination_before_connecting(self):
        for host in ("localhost", "localhost.localdomain", "fixture.localhost", "fixture.ts.net"):
            for ips in (("192.0.2.9",), ("127.0.0.1", "192.0.2.9"), ("::1", "2001:db8::9")):
                with self.subTest(host=host, ips=ips), \
                        patch("socket.getaddrinfo", return_value=[self.answer(ip) for ip in ips]), \
                        patch("socket.socket", side_effect=AssertionError("socket opened")), \
                        self.assertRaisesRegex(ModelError, "forbidden address"):
                    chat_json(self.config(f"http://{host}:1234/v1"), [])

    def test_dns_error_is_sanitized_and_no_empty_answer_is_accepted(self):
        for result in ([], socket.gaierror("synthetic-private-error")):
            mock = Mock(side_effect=result) if isinstance(result, Exception) else Mock(return_value=result)
            with patch("socket.getaddrinfo", mock), patch("socket.socket", side_effect=AssertionError("socket opened")), \
                    self.assertRaises(ModelError) as caught:
                chat_json(self.config("http://localhost:1234/v1"), [])
            self.assertNotIn("synthetic-private-error", str(caught.exception))

    def test_numeric_addresses_skip_dns_and_use_only_verified_socket(self):
        target = self.endpoint()
        with patch("socket.getaddrinfo", side_effect=AssertionError("numeric address used DNS")):
            self.assertEqual(chat_json(self.config(target.url), []), {"ok": True})

    def test_unexpected_peer_address_fails_before_sending_any_request(self):
        sock = Mock()
        sock.getpeername.return_value = ("192.0.2.9", 1234)
        with patch("socket.socket", return_value=sock), self.assertRaisesRegex(ModelError, "peer did not match"):
            chat_json(self.config("http://127.0.0.1:1234/v1"), [])
        sock.sendall.assert_not_called()
        sock.close.assert_called()

    def test_tls_uses_default_verification_and_original_hostname_for_sni(self):
        target = self.endpoint()
        context = Mock()
        # This fixture checks TLS setup arguments; it never disables verification
        # in the implementation or contacts an actual TLS/model provider.
        context.wrap_socket.side_effect = lambda sock, **kwargs: sock
        with patch("socket.getaddrinfo", return_value=[self.answer("127.0.0.1", target.server.server_port)]), \
                patch("ssl.create_default_context", return_value=context) as create_context:
            result = chat_json(self.config(f"https://fixture.ts.net:{target.server.server_port}/v1"), [])
        self.assertEqual(result, {"ok": True})
        create_context.assert_called_once_with()
        self.assertEqual(context.wrap_socket.call_args.kwargs, {"server_hostname": "fixture.ts.net"})

    def test_tls_certificate_failure_sends_no_model_request_and_is_sanitized(self):
        target = self.endpoint()
        context = Mock()
        context.wrap_socket.side_effect = ssl.SSLCertVerificationError("synthetic-private-cert-text")
        with patch("ssl.create_default_context", return_value=context), self.assertRaises(ModelError) as caught:
            chat_json(self.config(target.url.replace("http:", "https:")), [])
        self.assertEqual(target.requests, [])
        self.assertNotIn("synthetic-private-cert-text", str(caught.exception))

    def test_error_status_reports_code_without_response_or_location_content(self):
        target = self.endpoint(status=401, location="https://example.invalid/private-token")
        with self.assertRaisesRegex(ModelError, "^model HTTP 401$"):
            chat_json(self.config(target.url), [])


class ModelConfigValidationTests(unittest.TestCase):
    def test_malformed_url_forms_are_rejected_without_io(self):
        from urllib.parse import urlunsplit
        userinfo_url = urlunsplit(("http", "user:synthetic-secret@localhost", "/v1", "", ""))
        urls = [None, 3, "", " http://localhost/v1", "http://localhost/v1\n", "http://local\thost/v1",
                userinfo_url, "http://localhost/v1?token=synthetic-secret",
                "http://localhost/v1#synthetic-secret", "http://localhost/v1?", "http://localhost/v1#",
                "ftp://localhost/v1", "http:///v1", "http://localhost:0/v1", "http://localhost:65536/v1",
                "http://localhost:/v1", "http://localhost:word/v1", "http://[::1/v1",
                "http://[::1]ignored/v1", "http://[::1%25lo0]/v1", "http://local_host/v1",
                "http://localhost\\@example.invalid/v1", "http://localhost/v1%", "http://localhost/café"]
        for url in urls:
            with self.subTest(url=url), patch("socket.getaddrinfo", side_effect=AssertionError("DNS invoked")), \
                    patch("socket.socket", side_effect=AssertionError("socket opened")), \
                    self.assertRaises(ModelError) as caught:
                chat_json(ModelConfig(url, privacy_tier="strict_local"), [])
            self.assertNotIn("synthetic-secret", str(caught.exception))
            self.assertFalse(is_local_or_tailscale(url))

    def test_ineligible_names_and_noncanonical_numeric_forms_are_rejected(self):
        for host in ("example.invalid", "localhost.example.invalid", "ts.net", "127.1", "2130706433",
                     "0x7f000001", "100.200.1.1", "[::ffff:127.0.0.1]", "192.168.0.1"):
            with self.subTest(host=host), self.assertRaisesRegex(ModelError, "strict_local"):
                check_config(ModelConfig(f"http://{host}/v1", privacy_tier="strict_local"))

    def test_bad_timeout_key_model_and_privacy_tier_are_sanitized(self):
        canary = "synthetic-secret"
        settings = [{"timeout": value} for value in (0, -1, True, None, "2", float("nan"), float("inf"),
                                                   3601, 1e100, 10 ** 400)]
        settings += [{"api_key": canary + "\n"}, {"api_key": None}, {"model_id": ""},
                     {"model_id": None}, {"privacy_tier": "synthetic-secret"}, {"privacy_tier": []}]
        for kwargs in settings:
            with self.subTest(kwargs=kwargs), self.assertRaises(ModelError) as caught:
                check_config(ModelConfig("http://localhost/v1", **kwargs))
            self.assertNotIn("synthetic-secret", str(caught.exception))

    def test_from_env_rejects_bad_timeout_without_reflecting_it(self):
        with self.assertRaises(ModelError) as caught:
            ModelConfig.from_env({"MODEL_BASE_URL": "http://localhost/v1", "MODEL_TIMEOUT": "synthetic-secret"})
        self.assertNotIn("synthetic-secret", str(caught.exception))
        self.assertIsNone(ModelConfig.from_env({}))


if __name__ == "__main__":
    unittest.main()
