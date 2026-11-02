"""DF1507-05: `finding diff ID --directory D --from DRAFT` / `finding diff --directory D OLD NEW`. Synthetic files only.

Run from the repository root: python3 -B -m unittest tests.test_finding_diff -v
"""
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

SHA_A, SHA_B, SHA_C = "a" * 64, "b" * 64, "c" * 64
BODY = "\n".join(f"line {n}" for n in range(1, 41))


def finding(**changes):
    base = {"id": "syn-diff-1", "title": "Synthetic title", "slug": "synthetic-title", "summary": "Old summary.",
            "body_md": BODY, "classification": "documented_fact", "confidence": "verified",
            "event_date": "2026-01-02", "author": "Synthetic Author",
            "sources": [{"sha256": SHA_A, "locator": "page:1"}, {"sha256": SHA_B, "locator": "page:2"}],
            "limitations": ["Synthetic limits."], "counterevidence": ["None found."]}
    base.update(changes)
    return base


def published(**changes):
    return finding(**{"state": "published", "published_at": "2026-01-05T00:00:00Z", "corrections": [], **changes})


class FindingDiffTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-finding-diff-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "campaign"
        (self.root / "content" / "findings").mkdir(parents=True)
        self.write(self.root / "content" / "findings" / "free-form-name.json", published())

    def write(self, path, document):
        path.write_text(json.dumps(document) if isinstance(document, dict) else document, encoding="utf-8")
        return path

    def snapshot(self):
        tree = {}
        for folder, dirs, files in os.walk(self.base):
            for name in dirs + files:
                info = os.lstat(os.path.join(folder, name))
                tree[os.path.relpath(os.path.join(folder, name), self.base)] = (info.st_mtime_ns, info.st_size)
        return tree

    def cli(self, *arguments, returncode):
        before = self.snapshot()
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "finding", "diff",
                                 "--directory", str(self.root), *map(str, arguments)],
                                cwd=REPO_ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, returncode, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(self.snapshot(), before, "finding diff must not write anything")
        if returncode == 1:
            self.assertTrue(result.stderr.startswith("Stopped: "), result.stderr)
        return result

    def draft(self, name="draft.json", **changes):
        return self.write(self.base / name, finding(**changes))

    def as_json(self, *arguments):
        lines = self.cli(*arguments, "--json", returncode=3).stdout.splitlines()
        self.assertRegex(lines[-1], r"^\d+ field\(s\) differ$")
        return json.loads("\n".join(lines[:-1]))

    def test_identical_published_and_draft_report_no_differences(self):
        result = self.cli("syn-diff-1", "--from", self.draft(), returncode=0)
        self.assertEqual(result.stdout, "no differences\n")
        self.assertEqual(self.cli("syn-diff-1", "--from", self.draft(), "--json", returncode=0).stdout,
                         "[]\nno differences\n")

    def test_only_summary_changed(self):
        path = self.draft(summary="New summary.")
        self.assertEqual(self.cli("syn-diff-1", "--from", path, returncode=3).stdout,
                         'summary: "Old summary." -> "New summary."\n1 field(s) differ\n')
        self.assertEqual(self.as_json("syn-diff-1", "--from", path),
                         [{"field": "summary", "old": "Old summary.", "new": "New summary."}])

    def test_body_line_edit_shows_one_context_line_and_no_file_headers(self):
        path = self.draft(body_md=BODY.replace("line 20\n", "line twenty\n"))
        expected = ["@@ -19,3 +19,3 @@", " line 19", "-line 20", "+line twenty", " line 21"]
        self.assertEqual(self.as_json("syn-diff-1", "--from", path), [{"field": "body_md", "lines": expected}])
        text = self.cli("syn-diff-1", "--from", path, returncode=3).stdout.splitlines()
        self.assertEqual(text, ["body_md:", *expected, "1 field(s) differ"])

    def test_sources_added_removed_and_locator_changed(self):
        sources = [{"sha256": SHA_A, "locator": "page:9"}, {"sha256": SHA_C, "locator": "page:3"}]
        path = self.draft(sources=sources)
        self.assertEqual(self.as_json("syn-diff-1", "--from", path), [{
            "field": "sources", "removed": ["b" * 12], "added": ["c" * 12],
            "changed": [{"sha256": "a" * 12, "keys": ["locator"]}]}])
        text = self.cli("syn-diff-1", "--from", path, returncode=3).stdout
        self.assertEqual(text, f"sources:\n- {'b' * 12}\n+ {'c' * 12}\n~ {'a' * 12} locator\n1 field(s) differ\n")
        self.assertNotIn(SHA_A, text)

    def test_source_title_rule_and_repeat_count_changes(self):
        old = finding(sources=[{"sha256": SHA_A, "locator": "page:1"}])
        new = finding(sources=[{"sha256": SHA_A, "locator": "page:1", "title": "T", "rule_id": "R"},
                               {"sha256": SHA_A, "locator": "page:4"}])
        self.assertEqual(finding_diff.diff(old, new)[0]["changed"],
                         [{"sha256": "a" * 12, "keys": ["count", "locator", "title", "rule_id"]}])
        reordered = finding(sources=list(reversed(finding()["sources"])))
        self.assertEqual(finding_diff.diff(finding(), reordered), [])

    def test_lists_multiset_and_missing_fields(self):
        old = finding(limitations=["A", "B", "A"], counterevidence=["None found."])
        new = finding(limitations=["A", "C"])
        del new["counterevidence"], new["title"]
        self.assertEqual(finding_diff.diff(old, new), [
            {"field": "title", "old": "Synthetic title", "new": None},
            {"field": "limitations", "removed": ["B", "A"], "added": ["C"]},
            {"field": "counterevidence", "removed": ["None found."], "added": []}])

    def test_every_public_field_in_order_via_old_new(self):
        old = self.draft("old.json")
        new = self.draft("new.json", title="T2", summary="S2", body_md="x", sources=[], classification="opinion",
                         confidence="likely", limitations=[], counterevidence=["Other."])
        fields = [change["field"] for change in self.as_json(old, new)]
        self.assertEqual(fields, list(finding_diff.FIELDS))
        text = self.cli(old, new, returncode=3).stdout.splitlines()
        self.assertIn('limitations:\n- "Synthetic limits."\ncounterevidence:\n- "None found."\n+ "Other."',
                      "\n".join(text))
        self.assertEqual(text[-1], "8 field(s) differ")

    def test_publication_only_fields_are_not_a_public_difference(self):
        old = self.write(self.base / "old.json", published())
        new = self.write(self.base / "new.json", published(
            state="draft", published_at="2026-02-01T00:00:00Z", updated_at="2026-02-02T00:00:00Z",
            corrections=[{"date": "2026-02-02", "note": "Fixed."}], event_date="2026-01-03"))
        self.assertEqual(self.cli(old, new, returncode=0).stdout, "no differences\n")

    def test_unreadable_inputs_stop_naming_the_file(self):
        self.assertIn("no published finding with id 'missing'",
                      self.cli("missing", "--from", self.draft(), returncode=1).stderr)
        broken = self.write(self.base / "broken.json", "{not json")
        self.assertIn(f"{broken}: draft is not valid JSON", self.cli(broken, self.draft(), returncode=1).stderr)
        link = self.base / "link.json"
        link.symlink_to(self.draft())
        self.assertIn(f"{link}: draft is a symlink", self.cli("syn-diff-1", "--from", link, returncode=1).stderr)
        shaped = self.write(self.base / "shaped.json", finding(limitations="not a list"))
        self.assertIn(f"{shaped}: key 'limitations'", self.cli(shaped, self.draft(), returncode=1).stderr)
        self.write(self.root / "content" / "findings" / "second.json", published())
        stderr = self.cli("syn-diff-1", "--from", self.draft(), returncode=1).stderr
        self.assertIn("content/findings/free-form-name.json and content/findings/second.json", stderr)
        self.assertNotIn(str(self.base), stderr)

    def test_mixing_the_two_forms_is_refused(self):
        draft = self.draft()
        for arguments in (("syn-diff-1", draft, "--from", draft), (draft,), (draft, draft, draft)):
            self.assertIn("use either ID --from DRAFT, or OLD NEW", self.cli(*arguments, returncode=2).stderr)


if __name__ == "__main__":
    unittest.main()
