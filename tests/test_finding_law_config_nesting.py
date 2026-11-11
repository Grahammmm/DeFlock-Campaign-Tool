"""Finding-law check (RF2150-1): a campaign.json that exhausts the JSON parser's nesting limit is a controlled refusal.

Synthetic data only: a copy of the fictional example under a temporary directory, with an otherwise-unused ``extra``
value nested far past the interpreter's real decoder limit (the fixture's own parse is asserted to raise
``RecursionError`` before any production code runs). Run from the repository root:
python3 -B -m unittest tests.test_finding_law_config_nesting -v
"""
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

from campaign_tool import cli, content_check, finding_law, law
from campaign_tool.content import ContentError
from campaign_tool.content_check import content_problems
from tests import test_finding_law_config_paths as paths
from tests.test_local_law import rule

DEPTH = 100_000  # verified below to exceed the real decoder's limit
FIXED = "campaign.json: JSON nesting is too deep"
SENTINEL = "SYNTHETIC-DEEP-VALUE-SENTINEL"
BASE = {"schema_version": 1, "name": "Cedar County Records Project", "county": "Cedar County (fictional)",
        "state": "CA", "country": "US"}


def deep_text(shape="array", depth=DEPTH, leaf=SENTINEL):
    """A valid campaign.json whose unused ``extra`` value is nested ``depth`` levels deep."""
    head = json.dumps(BASE)[:-1] + ', "extra": '
    if shape == "array":
        return head + "[" * depth + json.dumps(leaf) + "]" * depth + "}"
    return head + '{"k": ' * depth + json.dumps(leaf) + "}" * depth + "}"


class NestingCase(paths.ConfigPathCase):
    def put(self, text):
        self.config.write_text(text, encoding="utf-8")

    def put_overlay(self):
        (self.root / "law").mkdir(exist_ok=True)
        (self.root / "law" / "local-rules.json").write_text(
            json.dumps({"schema_version": 1, "jurisdiction": "us-ca", "rules": [rule()]}), encoding="utf-8")

    def refused(self, call, *args):
        with self.assertRaises(ContentError) as caught:
            call(*args)
        self.assertEqual(str(caught.exception), FIXED)
        self.assertTrue(caught.exception.__suppress_context__)
        self.assertIsNone(caught.exception.__cause__)
        self.assertNotIn(SENTINEL, str(caught.exception))
        self.assertNotIn(str(self.tmp), str(caught.exception))
        return caught.exception

    def cli_run(self, *extra):
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "content-check", "--directory",
                               str(self.root), *extra], cwd=paths.REPO_ROOT, timeout=120, capture_output=True,
                              text=True, env=dict(os.environ, PYTHONPATH=str(paths.REPO_ROOT)))


class OverdeepConfigTests(NestingCase):
    def test_real_overdeep_regular_config_is_controlled(self):
        text = deep_text()
        with self.assertRaises(RecursionError):  # the fixture really exhausts the decoder
            json.loads(text)
        self.put(text)
        self.refused(finding_law.jurisdiction, self.root)

    def test_overdeep_object_and_array_payloads_share_fixed_refusal(self):
        for shape in ("array", "object"):
            with self.subTest(shape=shape):
                text = deep_text(shape)
                with self.assertRaises(RecursionError):
                    json.loads(text)
                self.put(text)
                self.refused(finding_law.jurisdiction, self.root)
                self.put(deep_text(shape, depth=50))  # shallow nested unused values stay accepted
                self.assertEqual(finding_law.jurisdiction(self.root), "us-ca")

    def test_cited_findings_and_unused_overlay_require_complete_jurisdiction(self):
        for kind in ("cited", "overlay"):
            with self.subTest(kind=kind):
                self.setUp()
                self.put(deep_text())
                if kind == "cited":
                    self.cite_unknown()
                else:
                    self.put_overlay()
                findings = [] if kind == "overlay" else None
                with mock.patch.object(law, "load_package", side_effect=AssertionError("package read")), \
                        mock.patch.object(law, "load_local_rules", side_effect=AssertionError("overlay read")):
                    if findings is None:
                        self.refused(finding_law.check_findings, self.root, [
                            {"slug": "s", "event_date": "2021-04-01", "sources": [], "rule_ids": ["ca-made-up"]}])
                    else:
                        self.refused(finding_law.check, self.root, findings)

    def test_content_check_text_json_and_load_parity(self):
        self.put(deep_text())
        self.put_overlay()
        self.assertEqual(self.load_error(), FIXED)
        self.assertEqual(content_problems(self.root), [FIXED])
        text = self.cli_run()
        self.assertEqual((text.returncode, text.stderr), (3, ""), text.stdout)
        self.assertEqual(text.stdout.splitlines(), ["content/: 1 problem(s)", FIXED])
        as_json = self.cli_run("--json")
        self.assertEqual((as_json.returncode, as_json.stderr), (3, ""), as_json.stdout)
        document = json.loads(as_json.stdout)
        self.assertEqual(document["count"], 1)
        self.assertIs(document["truncated"], False)
        self.assertIsNone(document["law_package"])
        self.assertEqual(document["problems"], [{"file": "campaign.json", "message": "JSON nesting is too deep"}])
        for output in (text.stdout, as_json.stdout, text.stderr, as_json.stderr):
            self.assertNotIn(SENTINEL, output)
            self.assertNotIn("Traceback", output)
            self.assertNotIn("RecursionError", output)
            self.assertNotIn("maximum recursion", output)


class FastPathAndCompatibilityTests(NestingCase):
    def test_no_citations_no_overlay_keeps_fast_path_and_optional_package_semantics(self):
        self.put(deep_text())
        with mock.patch.object(finding_law, "jurisdiction", side_effect=AssertionError("jurisdiction")), \
                mock.patch.object(cli, "read_config", side_effect=AssertionError("config read")), \
                mock.patch.object(law, "load_package", side_effect=AssertionError("package read")):
            self.assertEqual(finding_law.check(self.root, []), ([], []))
            self.assertEqual(finding_law.check_findings(self.root, []), [])
        self.assertIsNone(self.load_error())
        # The reporter's informational package lookup suppresses the controlled refusal (it never becomes a problem).
        self.assertIsNone(content_check._law_package(self.root))
        self.replace_config("absent")
        self.assertIsNone(finding_law.jurisdiction(self.root))
        self.assertIsNone(content_check._law_package(self.root))
        self.setUp()
        self.replace_config("directory")
        with self.assertRaises(ContentError) as caught:
            finding_law.jurisdiction(self.root)
        self.assertEqual(str(caught.exception), paths.REFUSAL)

    def test_existing_reader_errors_and_regular_symlink_compatibility(self):
        bad = {"malformed": "{", "schema": json.dumps(dict(BASE, schema_version=2)),
               "field": json.dumps({k: v for k, v in BASE.items() if k != "name"}),
               "state": json.dumps(dict(BASE, state="California"))}
        for name, text in bad.items():
            with self.subTest(name=name):
                self.put(text)
                with self.assertRaises(ContentError) as caught:
                    finding_law.jurisdiction(self.root)
                self.assertTrue(str(caught.exception).startswith("campaign.json: "), caught.exception)
                self.assertNotEqual(str(caught.exception), FIXED)
        self.config.write_bytes(b"\xff\xfe{")  # UTF-8 failure keeps the old wrapper
        with self.assertRaises(ContentError) as caught:
            finding_law.jurisdiction(self.root)
        self.assertTrue(str(caught.exception).startswith("campaign.json: "), caught.exception)
        self.assertNotEqual(str(caught.exception), FIXED)
        self.setUp()
        self.put(deep_text())
        self.replace_config("regular_link")  # the allowed regular-file link gets the same fixed refusal
        self.refused(finding_law.jurisdiction, self.root)
        for kind in ("directory", "dangling", "fifo"):
            with self.subTest(kind=kind):
                self.setUp()
                self.replace_config(kind)
                with self.assertRaises(ContentError) as caught:
                    finding_law.jurisdiction(self.root)
                self.assertEqual(str(caught.exception), paths.REFUSAL)


class BoundaryTests(NestingCase):
    def test_exception_boundary_is_narrow(self):
        self.cite_unknown()
        with mock.patch.object(cli, "read_config", side_effect=RecursionError(SENTINEL)):
            self.refused(finding_law.jurisdiction, self.root)
            self.assertEqual(self.load_error(), FIXED)
            self.assertEqual(content_problems(self.root), [FIXED])
        with mock.patch.object(cli, "read_config", side_effect=RuntimeError(SENTINEL)):
            with self.assertRaises(RuntimeError):
                finding_law.jurisdiction(self.root)
        with mock.patch.object(law, "load_package", side_effect=RecursionError(SENTINEL)):  # later stage: not relabeled
            with self.assertRaises(RecursionError):
                finding_law.check(self.root, [{"slug": "s", "event_date": "2021-04-01", "sources": [],
                                               "rule_ids": ["ca-made-up"]}])

    def test_repeated_calls_and_refusal_are_read_only(self):
        public = self.root / "public"
        public.mkdir()
        (public / "sentinel.txt").write_text("synthetic published sentinel", encoding="utf-8")
        self.cite_unknown()
        self.put_overlay()
        shallow = self.config.read_text(encoding="utf-8")
        for text in (shallow, deep_text(), deep_text("object"), shallow):
            self.put(text)
            deep = text is not shallow
            before = self.snapshot()
            for _ in range(2):
                if deep:
                    self.assertEqual(self.load_error(), FIXED)
                    self.assertEqual(content_problems(self.root), [FIXED])
                else:
                    self.assertEqual(finding_law.jurisdiction(self.root), "us-ca")
                    self.assertIn(paths.UNKNOWN, content_problems(self.root))
            self.assertEqual(self.snapshot(), before)
        self.assertEqual(sorted(p.name for p in public.iterdir()), ["sentinel.txt"])
        self.assertEqual((public / "sentinel.txt").read_text(encoding="utf-8"), "synthetic published sentinel")


if __name__ == "__main__":
    unittest.main()
