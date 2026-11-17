"""Duplicate JSON member refusal in model replies; every payload is synthetic and in-memory/loopback."""
import io
import json
import threading
import traceback
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from campaign_tool import law
from campaign_tool.digest import build_digest
from campaign_tool.digest.detectors import units_from_text
from campaign_tool.digest.model import ModelConfig, ModelError, chat_json

FIXED = "model response contains duplicate JSON members"
CANARY = "CANARY_VALUE_XYZ"
MESSAGES = [{"role": "user", "content": "{}"}]
PACKAGE = law.load_package("us-ca")
HEALTHY = {
    "scope": "Synthetic record", "actors": [], "dates": [], "statements": [], "omissions": [],
    "counterevidence": [],
    "conclusions": [{"text": "A rule-free finding.", "confidence": "likely",
                     "sources": [{"locator": {"unit": 0}, "rule_id": None}]}],
}
UNITS = [{"text": "Synthetic page text.", "locator": {"page": 1}}]


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class Opener:
    def __init__(self, body):
        self.body, self.calls, self.responses = body, 0, []

    def __call__(self, request, timeout=None):
        self.calls += 1
        response = Response(self.body)
        self.responses.append(response)
        return response


def envelope(content):
    return json.dumps({"choices": [{"message": {"content": content}}]}).encode()


def cloud(body):
    opener = Opener(body)
    return chat_json(ModelConfig("https://api.example.invalid/v1"), MESSAGES, opener=opener), opener


def refused(test, body):
    opener = Opener(body)
    with test.assertRaises(ModelError) as caught:
        chat_json(ModelConfig("https://api.example.invalid/v1"), MESSAGES, opener=opener)
    test.assertEqual(str(caught.exception), FIXED)
    test.assertTrue(caught.exception.__suppress_context__)
    test.assertNotIn(CANARY, "".join(traceback.format_exception(caught.exception)))
    test.assertEqual(opener.calls, 1)
    test.assertTrue(all(r.closed for r in opener.responses))


class OuterEnvelopeTests(unittest.TestCase):
    def test_outer_duplicate_members_refuse_before_choice_selection(self):
        good = '{"role": "assistant", "content": "{}"}'
        cases = [
            '{"choices": [{"message": {"content": "{}"}}], "choices": [{"message": {"content": "{\\"a\\": \\"%s\\"}"}}]}' % CANARY,
            '{"choices": [{"message": {"content": "{}", "content": "{\\"a\\": 1}"}}]}',
            '{"choices": [{"message": {"content": "{}", "cont\\u0065nt": "{}"}}]}',
            '{"unused": 1, "unused": 1, "choices": [{"message": %s}]}' % good,
            '{"choices": [{"message": %s}, {"message": %s, "message": %s}]}' % (good, good, good),
            '{"choices": [{"message": %s}], "meta": [{"k": 1, "k": 2}]}' % good,
        ]
        for body in cases:
            with self.subTest(body=body):
                refused(self, body.encode())

    def test_unique_names_and_legacy_failures_remain_compatible(self):
        document, _ = cloud(json.dumps({"choices": [{"message": {"content": "{\"a\": {\"a\": 1}, \"A\": [{\"a\": 2}], \"s\\u0063ope\": \"k\\\\u0070\"}"}}],
                                        "usage": {"a": 1}}).encode())
        self.assertEqual(document, {"a": {"a": 1}, "A": [{"a": 2}], "scope": "k\\u0070"})
        for body, text in [(b"not json", "model reply is not a chat completion"),
                           (json.dumps({"choices": []}).encode(), "model reply is not a chat completion"),
                           (envelope(5), "model reply content is not text"),
                           (envelope("[1]"), "model reply is not a JSON object"),
                           (envelope("prose"), "model response is not JSON")]:
            with self.subTest(text=text), self.assertRaises(ModelError) as caught:
                cloud(body)
            self.assertEqual(str(caught.exception), text)


class InnerDocumentTests(unittest.TestCase):
    def test_inner_duplicate_members_refuse_at_every_nesting_level(self):
        cases = [
            '{"scope": "A", "scope": "A"}',
            '{"scope": "A", "sco\\u0070e": "%s"}' % CANARY,
            '{"conclusions": [{"text": "t", "confidence": "needs_attorney_review", "confidence": "verified"}]}',
            '{"conclusions": [{"text": "t", "text": "u"}]}',
            '{"statements": [{"locator": {"unit": 0, "unit": 1}}]}',
            '{"x": {"y": [{"z": 1, "z": 2}]}}',
        ]
        for content in cases:
            with self.subTest(content=content):
                refused(self, envelope(content))

    def test_fenced_and_prose_duplicate_members_cannot_bypass_refusal(self):
        nested = '{"a": "{\\"b\\": 1}", "a": "{\\"b\\": 2}"}'
        for content in ['{"a": 1, "a": 2}', '```json\n{"a": 1, "a": 2}\n```', 'Here: {"a": 1, "a": 2} done',
                        nested, "Sure! " + nested]:
            with self.subTest(content=content):
                refused(self, envelope(content))
        for content in ['{"a": 1}', '```json\n{"a": 1}\n```', 'Here: {"a": 1} done']:
            with self.subTest(content=content):
                self.assertEqual(cloud(envelope(content))[0], {"a": 1})

    def test_challenge_shaped_chat_reply_cannot_discard_disputes(self):
        refused(self, envelope('{"disputes": [{"id": 1}], "disputes": []}'))
        document, _ = cloud(envelope('{"disputes": [], "notes": "Synthetic"}'))
        self.assertEqual(document, {"disputes": [], "notes": "Synthetic"})


class RawServer:
    def __init__(self, body):
        server = self
        server.body, server.requests = body, 0

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("content-length", 0)))
                server.requests += 1
                self.send_response(200)
                self.send_header("content-length", str(len(server.body)))
                self.end_headers()
                self.wfile.write(server.body)

            def log_message(self, *args):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/v1"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join()


class TransportAndDigestTests(unittest.TestCase):
    def test_duplicate_refusal_is_shared_by_both_response_transports(self):
        refused(self, envelope('{"a": 1, "a": 2}'))
        for body in (envelope('{"a": 1, "a": 2}'), b'{"choices": [], "choices": []}'):
            server = RawServer(body)
            try:
                with self.assertRaises(ModelError) as caught:
                    chat_json(ModelConfig(server.url, privacy_tier="strict_local", timeout=2), MESSAGES)
            finally:
                server.close()
            self.assertEqual(str(caught.exception), FIXED)
            self.assertEqual(server.requests, 1)
        server = RawServer(envelope('{"a": 1}'))
        try:
            self.assertEqual(chat_json(ModelConfig(server.url, privacy_tier="strict_local", timeout=2), MESSAGES), {"a": 1})
        finally:
            server.close()
        with self.assertRaises(ModelError):
            chat_json(ModelConfig("http://127.0.0.1:1/v1", privacy_tier="strict_local"), MESSAGES, opener=Opener(b""))

    def test_build_digest_refuses_ambiguous_narrative_without_mutation(self):
        def digest(content):
            opener = Opener(envelope(content))
            units = json.loads(json.dumps(UNITS))
            try:
                return build_digest("a" * 64, units, PACKAGE, "us-ca", "us-ca:draft", "redacted_cloud",
                                    model_config=ModelConfig("https://api.example.invalid/v1"), opener=opener), opener
            finally:
                self.assertEqual(units, UNITS)
        result, opener = digest(json.dumps(HEALTHY))
        self.assertEqual((result["scope"], result["conclusions"][0]["confidence"], opener.calls), ("Synthetic record", "likely", 1))
        healthy = json.dumps(HEALTHY)
        variants = [
            healthy.replace('"confidence": "likely"', '"confidence": "needs_attorney_review", "confidence": "verified"'),
            healthy.replace('"unit": 0', '"unit": 0, "unit": 0'),
            healthy.replace('"scope": "Synthetic record"', '"scope": "FIRST", "sco\\u0070e": "LAST"'),
        ]
        for content in variants:
            with self.subTest(content=content):
                with self.assertRaises(ModelError) as caught:
                    try:
                        digest(content)
                    except TypeError:
                        self.fail("build_digest does not accept opener")
                self.assertEqual(str(caught.exception), FIXED)


if __name__ == "__main__":
    unittest.main()
