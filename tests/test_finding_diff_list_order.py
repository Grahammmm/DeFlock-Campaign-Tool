"""RF2100-5: finding diff shows ordered old/new lists for a pure limitations/counterevidence permutation.

Synthetic files only. Run from the repository root: python3 -B -m unittest tests.test_finding_diff_list_order -q
"""
import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import finding_diff  # noqa: E402
from tests.test_finding_diff import finding, published  # noqa: E402

A, B, C = "Synthetic A", "Synthetic B", "Synthetic C"
FIELDS = ("limitations", "counterevidence")


class ListOrderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-diff-order-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "campaign"
        (self.root / "content" / "findings").mkdir(parents=True)

    def snapshot(self):
        tree = {}
        for folder, dirs, files in os.walk(self.base):
            for name in dirs + files:
                path = os.path.join(folder, name)
                info = os.lstat(path)
                data = b"" if os.path.isdir(path) else Path(path).read_bytes()
                tree[os.path.relpath(path, self.base)] = (info.st_mode, info.st_mtime_ns, data)
        return tree

    def cli(self, *arguments):
        before = self.snapshot()
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "finding", "diff",
                                 "--directory", str(self.root), *arguments],
                                cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(result.stderr, "")
        return result

    def files(self, field, old, new):
        (self.root / "content" / "findings" / "p.json").write_text(json.dumps(published(**{field: old})))
        old_path, new_path = self.base / "old.json", self.base / "new.json"
        old_path.write_text(json.dumps(finding(**{field: old})))
        new_path.write_text(json.dumps(finding(**{field: new})))
        return old_path, new_path

    def test_pure_permutations_keep_every_occurrence(self):
        for field in FIELDS:
            for old, new in (([A, B], [B, A]), ([A, B, C], [C, A, B]), ([A, B, A], [A, A, B]), ([A, A, B], [A, B, A])):
                changes = finding_diff.diff(finding(**{field: old}), finding(**{field: new}))
                self.assertEqual(changes, [{"field": field, "old": old, "new": new}])
        self.assertEqual(finding_diff.diff(finding(limitations=[A, A]), finding(limitations=[A, A])), [])
        self.assertEqual(finding_diff.diff(finding(limitations=[A, A]), finding(limitations=[A])),
                         [{"field": "limitations", "removed": [A], "added": []}])

    def test_membership_changes_keep_legacy_deltas(self):
        self.assertEqual(finding_diff.diff(finding(limitations=[A, B, A]), finding(limitations=[A, C])),
                         [{"field": "limitations", "removed": [B, A], "added": [C]}])
        self.assertEqual(finding_diff.diff(finding(counterevidence=[A, B]), finding(counterevidence=[C, B, A, A])),
                         [{"field": "counterevidence", "removed": [], "added": [C, A]}])

    def test_absent_and_empty_lists(self):
        base = finding()
        del base["limitations"]
        self.assertEqual(finding_diff.diff(base, finding(limitations=[])), [])
        self.assertEqual(finding_diff.diff(base, finding(limitations=[A])),
                         [{"field": "limitations", "removed": [], "added": [A]}])

    def test_fields_order_summary_and_agency_shape(self):
        old = finding(limitations=[A, B], counterevidence=[A, B], summary="x", also_agency_ids=["ca-a", "ca-b"])
        new = finding(limitations=[B, A], counterevidence=[B, A], summary="y", also_agency_ids=["ca-b", "ca-a"])
        self.assertEqual(finding_diff.diff(old, new), [
            {"field": "summary", "old": "x", "new": "y"},
            {"field": "limitations", "old": [A, B], "new": [B, A]},
            {"field": "counterevidence", "old": [A, B], "new": [B, A]},
            {"field": "also_agency_ids", "old": ["ca-a", "ca-b"], "new": ["ca-b", "ca-a"]}])

    def test_results_are_detached_from_inputs(self):
        old, new = finding(limitations=[A, B]), finding(limitations=[B, A])
        keep = copy.deepcopy((old, new))
        changes = finding_diff.diff(old, new)
        changes[0]["old"].append("mutated")
        changes[0]["new"].clear()
        self.assertEqual((old, new), keep)

    def test_escaping_is_json_quoted_on_one_line(self):
        odd = ['-lead "q" \\ back', "caf\u00e9\nsecond line"]
        changes = finding_diff.diff(finding(limitations=odd), finding(limitations=odd[::-1]))
        self.assertEqual(finding_diff.render(changes), [
            "limitations: " + json.dumps(odd, ensure_ascii=False) + " -> " + json.dumps(odd[::-1], ensure_ascii=False)])
        self.assertNotIn("\n", finding_diff.render(changes)[0])

    def test_both_cli_forms_and_formats(self):
        for field in FIELDS:
            old, new = [A, B, A], [A, A, B]
            old_path, new_path = self.files(field, old, new)
            line = f"{field}: {json.dumps(old)} -> {json.dumps(new)}\n1 field(s) differ\n"
            entry = json.dumps([{"field": field, "old": old, "new": new}], indent=2, ensure_ascii=False)
            for args, as_json in (((str(old_path), str(new_path)), False), ((str(old_path), str(new_path), "--json"), True),
                                  (("syn-diff-1", "--from", str(new_path)), False),
                                  (("syn-diff-1", "--from", str(new_path), "--json"), True)):
                result = self.cli(*args)
                self.assertEqual(result.returncode, 3)
                self.assertEqual(result.stdout, entry + "\n1 field(s) differ\n" if as_json else line)
            same = self.cli(str(old_path), str(old_path))
            self.assertEqual((same.returncode, same.stdout), (0, "no differences\n"))

    def test_malformed_list_is_still_refused(self):
        old_path, new_path = self.files("limitations", [A], [A])
        new_path.write_text(json.dumps(finding(limitations=[A, 5])))
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "finding", "diff", "--directory",
                                 str(self.root), str(old_path), str(new_path)],
                                cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("must be a list of strings", result.stderr)


if __name__ == "__main__":
    unittest.main()
