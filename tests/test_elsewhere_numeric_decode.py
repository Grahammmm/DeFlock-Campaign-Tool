"""RF0755-4: an integer token the interpreter refuses to convert in content/elsewhere.json is a named content problem.

Synthetic only: temp copies of examples/fictional-campaign, reserved .test hosts, no network. Inputs are raw JSON text
written independently of the code under test; the oversized number tokens stay far below the real byte cap.
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from campaign_tool import content_elsewhere
from campaign_tool.content import ContentError
from campaign_tool.content_check import content_problems, run_cli
from campaign_tool.content_elsewhere import read_elsewhere
from campaign_tool.meeting_stage import MeetingStageError, stage
from campaign_tool.site import build_site
from tests import test_elsewhere_duplicate_keys as _dup  # module alias: no TestCase is imported into this namespace

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
DAY = date(2026, 10, 1)
MID = "legistar-examplecity-3001"
NAMED = "elsewhere.json: not valid UTF-8 JSON (value cannot be decoded)"
REFUSAL = r"^elsewhere\.json: not valid UTF-8 JSON \(value cannot be decoded\)$"
BIG = "7" * 5000  # over the default 4300-digit conversion ceiling, about 5 KB
members, obj, doc, VALID = _dup.members, _dup.obj, _dup.doc, _dup.VALID

CASES = {
    "root": BIG,
    "negative root": "-" + BIG,
    "array element": f"[{BIG}]",
    "negative array element": f"[-{BIG}]",
    "required field value": doc(obj(members(1, place=BIG))),
    "nested unknown value": doc(obj(members() + [f'"extra": {{"deep": [{BIG}]}}'])),
    "negative nested unknown value": doc(obj(members() + [f'"extra": -{BIG}'])),
    "later entry": doc(obj(members(1)), obj(members(2, decided_on='"2026-04-11"') + [f'"extra": {BIG}'])),
}


class Case(_dup.Case):
    def stage_ready(self):
        (self.root / "kit").mkdir(exist_ok=True)
        (self.root / "kit" / "meetings.json").write_text(json.dumps({"schema_version": 1, "meetings": [
            {"id": MID, "body": "Example City Council", "starts_at": "2026-11-18T18:00:00-08:00"}]}), encoding="utf-8")
        return self.root / "content" / "meetings" / f"{MID}.json"


class ReaderTests(Case):
    def test_real_oversized_decimal_tokens_become_named_content_errors(self):
        for label, text in CASES.items():
            with self.subTest(case=label):
                self.assertLess(len(text), 20000)
                self.put(text)
                with mock.patch.object(content_elsewhere, "load_elsewhere", side_effect=AssertionError("semantic load")):
                    with self.assertRaisesRegex(ContentError, REFUSAL) as caught:
                        read_elsewhere(self.root, DAY)
                self.assertNotIn("digits", str(caught.exception))
                self.assertNotIn("set_int_max_str_digits", str(caught.exception))
                self.assertNotIn("7777", str(caught.exception))

    def test_collecting_reader_preserves_prior_problems_and_adds_one_file_problem(self):
        for label in ("root", "later entry"):
            with self.subTest(case=label):
                self.put(CASES[label])
                collected = ["earlier: independent problem"]
                self.assertIsNone(read_elsewhere(self.root, DAY, collected))
                self.assertEqual(collected, ["earlier: independent problem", NAMED])
                with self.assertRaisesRegex(ContentError, REFUSAL):
                    read_elsewhere(self.root, DAY)

    def test_parser_valueerror_does_not_echo_exception_canaries(self):
        self.put(VALID)
        canary = "/secret/CANARY-PATH value=CANARY-VALUE 123456"
        with mock.patch.object(content_elsewhere.json, "loads", side_effect=ValueError(canary)):
            with self.assertRaises(ContentError) as caught:
                read_elsewhere(self.root, DAY)
        self.assertEqual(str(caught.exception), NAMED)
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)
        self.assertNotIn("CANARY", str(caught.exception))
        # a downstream programming/validation error is not turned into the parser category
        self.put(VALID)
        with mock.patch.object(content_elsewhere, "load_elsewhere", side_effect=ValueError("downstream")):
            with self.assertRaisesRegex(ValueError, "^downstream$") as caught:
                read_elsewhere(self.root, DAY)
        self.assertNotIsInstance(caught.exception, ContentError)

    def test_configured_integer_limit_is_respected_without_global_changes(self):
        script = Path(self.root).parent / "limit_child.py"
        script.write_text(textwrap.dedent("""
            import json, sys
            from datetime import date
            from pathlib import Path
            root, digits = Path(sys.argv[1]), int(sys.argv[2])
            sys.path.insert(0, sys.argv[3])
            from campaign_tool.content import ContentError
            from campaign_tool.content_elsewhere import read_elsewhere
            out = {}
            for label, sign in (("positive", ""), ("negative", "-")):
                (root / "content" / "elsewhere.json").write_text("[" + sign + "7" * digits + "]", encoding="utf-8")
                try:
                    read_elsewhere(root, date(2026, 10, 1))
                    out[label] = "accepted"
                except ContentError as exc:
                    out[label] = str(exc)
            print(json.dumps(out))
        """), encoding="utf-8")

        def child(limit, digits):
            result = subprocess.run([sys.executable, "-B", "-X", f"int_max_str_digits={limit}", str(script), str(self.root), str(digits), str(REPO_ROOT)],
                                    cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)

        below = child(640, 640)  # at the configured limit: parsed, then the existing schema refusal
        above = child(640, 641)  # one over: the new parser category
        disabled = child(0, 5000)  # limit disabled: parsed, existing schema refusal, no production-side cap
        for label in ("positive", "negative"):
            self.assertEqual(above[label], NAMED)
            for seen in (below, disabled):
                self.assertTrue(seen[label].startswith("elsewhere.json[0]: expected an object"), seen[label])
        self.assertNotEqual(sys.get_int_max_str_digits(), 641)  # the parent interpreter setting is untouched


class ConsumerTests(Case):
    def test_checker_text_json_and_independent_errors_survive_numeric_failure(self):
        self.put(CASES["array element"])
        collected = []
        self.assertIsNone(read_elsewhere(self.root, DAY, collected))
        self.assertEqual(collected, [NAMED])
        self.assertEqual([p for p in content_problems(self.root) if p.startswith("elsewhere.json")], collected)
        text = self.cli("content-check")
        self.assertEqual(text.returncode, 3, text.stderr)
        self.assertIn(NAMED, text.stdout)
        self.assertNotIn("content/: 0 problem", text.stdout)
        for shown in (text.stdout, text.stderr):
            self.assertNotIn("set_int_max_str_digits", shown)
            self.assertNotIn("7777", shown)
        as_json = self.cli("content-check", "--json")
        self.assertEqual(as_json.returncode, 3, as_json.stderr)
        record = json.loads(as_json.stdout)
        self.assertEqual(record["schema"], "content-check-v1")
        rows = [r for r in record["problems"] if r["file"] == "elsewhere.json"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["message"], NAMED.split(": ", 1)[1])
        # an independent malformed file is reported alongside, in text and JSON
        (self.root / "content" / "findings").mkdir(parents=True, exist_ok=True)
        (self.root / "content" / "findings" / "zz-bad.json").write_text("{not json", encoding="utf-8")
        both = [p for p in content_problems(self.root)]
        self.assertIn(NAMED, both)
        self.assertTrue(any(p.startswith("findings/zz-bad.json") or "zz-bad" in p for p in both), both)
        out, code = io.StringIO(), 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            try:
                run_cli(self.root, as_json=True)
            except SystemExit as exc:
                code = exc.code
        self.assertEqual(code, 3)
        record = json.loads(out.getvalue())
        files = [r["file"] for r in record["problems"]]
        self.assertIn("elsewhere.json", files)
        self.assertTrue(any("zz-bad" in name for name in files), files)
        self.assertEqual(record["count"], len(record["problems"]))

    def test_build_numeric_refusal_preserves_absent_and_last_good_public_trees(self):
        self.put(VALID)
        build_site(self.root, today=DAY)
        public_before = self.snapshot(self.public)
        self.put(CASES["nested unknown value"])
        source_before = (self.path.read_bytes(), self.path.stat().st_mode)
        with self.assertRaisesRegex(ContentError, REFUSAL):
            build_site(self.root, today=DAY)
        self.assertEqual(self.snapshot(self.public), public_before)
        self.assertEqual((self.path.read_bytes(), self.path.stat().st_mode), source_before)
        self.assertEqual(list(self.root.glob(".public.*")), [])
        shutil.rmtree(self.public)
        with self.assertRaisesRegex(ContentError, REFUSAL):
            build_site(self.root, today=DAY)
        self.assertFalse(self.public.exists())
        self.assertEqual(list(self.root.glob(".public.*")), [])
        result = self.cli("build")
        self.assertEqual((result.returncode, result.stdout), (1, ""))
        self.assertEqual(result.stderr.strip().splitlines()[-1], f"Stopped: {NAMED}")
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn("set_int_max_str_digits", result.stderr)
        self.assertFalse(self.public.exists())

    def test_stage_numeric_refusal_preserves_first_and_replacement_outputs(self):
        target = self.stage_ready()
        self.put(CASES["array element"])
        before = self.snapshot(self.root)
        with self.assertRaisesRegex((ContentError, MeetingStageError), REFUSAL):
            stage(self.root, MID, elsewhere_indices=[1])
        self.assertFalse(target.exists())
        self.assertEqual(self.snapshot(self.root), before)
        result = self.cli("meetings-stage", "--id", MID, "--elsewhere", "1")
        self.assertEqual((result.returncode, result.stdout), (1, ""))
        self.assertEqual(result.stderr.strip().splitlines()[-1], f"Stopped: {NAMED}")
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse(target.exists())
        self.assertEqual(self.snapshot(self.root), before)
        self.put(VALID)
        stage(self.root, MID, elsewhere_indices=[1])
        staged = (target.read_bytes(), target.stat().st_mode, target.stat().st_mtime_ns)
        self.put(CASES["array element"])
        with self.assertRaisesRegex((ContentError, MeetingStageError), REFUSAL):
            stage(self.root, MID, replace=True, elsewhere_indices=[1])
        self.assertEqual((target.read_bytes(), target.stat().st_mode, target.stat().st_mtime_ns), staged)


class CompatibilityTests(Case):
    def test_existing_specific_refusals_and_bounded_read_closure_are_unchanged(self):
        expected = {
            "syntax": ("[1,", r"^elsewhere\.json: not valid UTF-8 JSON \(Expecting value: line 1 column 4 \(char 3\)\)$"),
            "duplicate": ('[{"a": 1, "a": 2}]', r"^elsewhere\.json: a JSON object repeats a key$"),
            "nesting": ("[" * 100000, r"^elsewhere\.json: not valid UTF-8 JSON \(nested too deeply\)$"),
        }
        for label, (text, pattern) in expected.items():
            with self.subTest(case=label):
                self.put(text)
                with self.assertRaisesRegex(ContentError, pattern):
                    read_elsewhere(self.root, DAY)
        self.path.write_bytes(b"[\xff]")
        with self.assertRaisesRegex(ContentError, r"^elsewhere\.json: not valid UTF-8 JSON \(.*utf-8.*\)$"):
            read_elsewhere(self.root, DAY)
        # an over-cap numeric payload is refused by size before any parser entry
        self.put("[" + "7" * (content_elsewhere.MAX_JSON_BYTES + 10) + "]")
        with mock.patch.object(content_elsewhere.json, "loads", side_effect=AssertionError("parser entered")):
            with self.assertRaisesRegex(ContentError, r"^elsewhere\.json: file too large$"):
                read_elsewhere(self.root, DAY)
        # the cap+1 binary read and handle closure also hold on a numeric parse failure
        self.put(CASES["root"])
        real_fdopen, handles = os.fdopen, []
        target_path = self.path

        def tracking_fdopen(descriptor, *args, **kwargs):
            same = os.path.samestat(os.fstat(descriptor), os.stat(target_path))
            handle = real_fdopen(descriptor, *args, **kwargs)
            if same:
                handles.append(handle)
            return handle

        with mock.patch.object(os, "fdopen", tracking_fdopen):
            with self.assertRaisesRegex(ContentError, REFUSAL):
                read_elsewhere(self.root, DAY)
        self.assertEqual(len(handles), 1)
        self.assertTrue(handles[0].closed)

    def test_valid_string_schema_absent_feature_and_small_numbers_are_unchanged(self):
        self.put(doc(obj(members(1, place='"Z\\u00fcrich 12345678901234567890"')),
                     obj(members(2, decided_on='"2026-04-11"'))))
        accepted = read_elsewhere(self.root, DAY)
        self.assertEqual([e["place"] for e in accepted], ["Z\u00fcrich 12345678901234567890", "Springfield 2"])
        self.put("[12345]")
        with self.assertRaises(ContentError) as caught:
            read_elsewhere(self.root, DAY)
        self.assertNotEqual(str(caught.exception), NAMED)
        self.assertIn("elsewhere.json", str(caught.exception))
        self.put(doc(obj(members(1, place="12345"))))
        with self.assertRaises(ContentError) as caught:
            read_elsewhere(self.root, DAY)
        self.assertNotEqual(str(caught.exception), NAMED)
        self.path.unlink()
        self.assertIsNone(read_elsewhere(self.root, DAY))
        self.assertEqual(read_elsewhere(self.root, DAY, []), None)
        self.assertNotIn(NAMED, content_problems(self.root))


if __name__ == "__main__":
    unittest.main()
