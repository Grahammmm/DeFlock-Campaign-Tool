"""law impact refuses a package file that repeats a JSON member name (synthetic data only)."""
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from campaign_tool import law, law_impact  # noqa: E402

BASE_TEXT = (REPO_ROOT / "tests" / "fixtures" / "law_impact" / "base-package.json").read_text(encoding="utf-8")
TOP = '"jurisdiction": "us-ca",'
RULE = '"rule_id": "ca-alpha",'


class DuplicateMemberTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.old = self.tmp / "old.json"
        self.new = self.tmp / "new.json"
        self.new.write_text(BASE_TEXT, encoding="utf-8")

    def write(self, text):
        self.old.write_text(text, encoding="utf-8")
        return self.old

    def test_package_with_repeated_top_level_member_is_refused(self):
        self.assertIn(TOP, BASE_TEXT)
        path = self.write(BASE_TEXT.replace(TOP, '"jurisdiction": "zz-bad",\n  ' + TOP, 1))
        with self.assertRaisesRegex(law_impact.ImpactError, "repeated JSON member name"):
            law_impact.load_file(path)

    def test_package_with_repeated_nested_member_is_refused(self):
        self.assertIn(RULE, BASE_TEXT)
        path = self.write(BASE_TEXT.replace(RULE, RULE + "\n      " + RULE, 1))
        with self.assertRaisesRegex(law_impact.ImpactError, "repeated JSON member name"):
            law_impact.load_file(path)

    def test_escaped_equivalent_name_counts_as_repeat(self):
        path = self.write(BASE_TEXT.replace(TOP, '"jurisdiction": "us-ca",\n  "\\u006aurisdiction": "us-ca",', 1))
        with self.assertRaisesRegex(law_impact.ImpactError, "repeated JSON member name"):
            law_impact.load_file(path)

    def test_refusal_names_no_member_value(self):
        path = self.write(BASE_TEXT.replace(TOP, TOP + '\n  "SECRET-MEMBER-NAME": 1,\n  "SECRET-MEMBER-NAME": 2,', 1))
        with self.assertRaises(law_impact.ImpactError) as ctx:
            law_impact.load_file(path)
        self.assertIn("repeated JSON member name", str(ctx.exception))
        self.assertNotIn("SECRET-MEMBER-NAME", str(ctx.exception))

    def test_run_cli_reports_repeated_member_package_as_input_error(self):
        self.write(BASE_TEXT.replace(TOP, '"jurisdiction": "zz-bad",\n  ' + TOP, 1))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = law.main(["impact", str(self.old), str(self.new), "--directory", str(self.tmp / "campaign")])
        self.assertEqual((code, out.getvalue()), (2, ""))
        self.assertIn("repeated JSON member name", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())

    def test_unchanged_base_package_still_loads(self):
        package = law_impact.load_file(self.write(BASE_TEXT))
        self.assertEqual(package["jurisdiction"], "us-ca")


if __name__ == "__main__":
    unittest.main()
