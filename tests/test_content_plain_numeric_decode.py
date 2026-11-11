"""RF1400-2: a decoder ValueError (integer-conversion guard) is a fixed, file-labelled refusal."""
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

from tests import test_content_plain as legacy  # alias only: old TestCases must not be rediscovered here

content_plain = legacy.content_plain
REPO_ROOT = legacy.REPO_ROOT
LIMIT = 4300
LABELS = {"page": "content/pages/probe.json", "faq": "content/faq.json", "glossary": "content/glossary.json"}
DECODE = "{}: JSON value could not be decoded"


class NumericDecodeTests(legacy.Case):
    target, document, put = legacy.BoundedReadTests.target, legacy.BoundedReadTests.document, legacy.BoundedReadTests.put

    def setUp(self):
        super().setUp()
        previous = sys.get_int_max_str_digits()
        sys.set_int_max_str_digits(LIMIT)
        self.addCleanup(sys.set_int_max_str_digits, previous)

    def raw(self, kind, digits, sign=""):
        base = json.dumps(self.document(kind), ensure_ascii=False)
        raw = (base[:-1] + ', "extra": ' + sign + "7" * digits + "}").encode("utf-8")
        self.assertLess(len(raw), content_plain.MAX_JSON_BYTES)
        return raw

    def clear(self):
        for kind in legacy.SURFACES:
            self.target(kind).unlink(missing_ok=True)

    def run_cli(self, *mode):
        env = dict(os.environ, PYTHONINTMAXSTRDIGITS=str(LIMIT))
        before = self.snapshot()
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "content", "check-plain", *mode,
                                 "--directory", str(self.root)], cwd=REPO_ROOT, capture_output=True, text=True,
                                timeout=60, env=env)
        self.assertEqual(self.snapshot(), before, "a refusal writes nothing")
        return result

    def test_real_numeric_decoder_refusal_labels_page_faq_and_glossary(self):
        for kind in legacy.SURFACES:
            for sign in ("", "-"):
                with self.subTest(kind=kind, sign=sign):
                    self.clear()
                    self.write(self.target(kind), self.raw(kind, 5000, sign))
                    with self.assertRaises(content_plain.ContentError) as caught:
                        content_plain.check_content(self.root)
                    self.assertEqual(str(caught.exception), DECODE.format(LABELS[kind]))
                    for mode in ((), ("--json",)):
                        result = self.run_cli(*mode)
                        self.assertEqual(result.returncode, 1)
                        self.assertEqual(result.stdout, "")
                        self.assertEqual(result.stderr, "Stopped: " + DECODE.format(LABELS[kind]) + "\n")

    def test_decoder_valueerror_message_and_chaining_are_private(self):
        for kind in legacy.SURFACES:
            with self.subTest(kind=kind):
                self.clear()
                self.put(kind)
                canary = ValueError("PRIVATE-CANARY /home/secret/path\n12345678")
                with mock.patch.object(content_plain.json, "loads", side_effect=canary):
                    with self.assertRaises(content_plain.ContentError) as caught:
                        content_plain.check_content(self.root)
                self.assertEqual(str(caught.exception), DECODE.format(LABELS[kind]))
                self.assertIsNone(caught.exception.__cause__)
                self.assertTrue(caught.exception.__suppress_context__)

    def test_late_numeric_failure_emits_no_partial_report(self):
        self.page("a", body="The ALPR.")  # would warn
        self.write(self.root / "content" / "faq.json", self.raw("faq", 5000))
        with mock.patch.object(content_plain.plain_language, "check_fields") as fields:
            with self.assertRaises(content_plain.ContentError):
                content_plain.check_content(self.root)
        fields.assert_not_called()
        for mode in ((), ("--json",)):
            result = self.run_cli(*mode)
            self.assertEqual((result.returncode, result.stdout), (1, ""))
            self.assertEqual(result.stderr, "Stopped: " + DECODE.format("content/faq.json") + "\n")

    def test_existing_decode_categories_survive_and_other_valueerrors_pass_through(self):
        self.write(self.target("page"), b'{"title": ')
        with self.assertRaises(content_plain.ContentError) as caught:
            content_plain.check_content(self.root)
        self.assertIn("not valid UTF-8 JSON", str(caught.exception))
        self.write(self.target("page"), b"\xff\xfe")
        with self.assertRaises(content_plain.ContentError) as caught:
            content_plain.check_content(self.root)
        self.assertIn("not valid UTF-8 JSON", str(caught.exception))
        self.put("page")
        with mock.patch.object(content_plain.plain_language, "check_fields", side_effect=ValueError("engine")):
            with self.assertRaises(ValueError) as caught:
                content_plain.check_content(self.root)
        self.assertNotIsInstance(caught.exception, content_plain.ContentError)
        with mock.patch.object(content_plain.json, "loads", side_effect=RuntimeError("decoder")):
            with self.assertRaises(RuntimeError):
                content_plain.check_content(self.root)

    def test_accepted_numeric_token_is_unchanged_and_one_digit_more_refuses(self):
        self.put("page")
        control = content_plain.check_content(self.root)
        self.write(self.target("page"), self.raw("page", LIMIT))
        self.assertEqual(content_plain.check_content(self.root), control)
        self.write(self.target("page"), self.raw("page", LIMIT + 1))
        with self.assertRaises(content_plain.ContentError) as caught:
            content_plain.check_content(self.root)
        self.assertEqual(str(caught.exception), DECODE.format(LABELS["page"]))
        self.put("page")
        self.assertEqual(content_plain.check_content(self.root), control)


if __name__ == "__main__":
    unittest.main()
