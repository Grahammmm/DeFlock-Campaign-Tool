"""RF0111-4: finding diff shows exact old/new body when line splitting erases the hunk. Synthetic data only.

Run from the repository root: python3 -B -m unittest tests.test_finding_diff_body_endings -q
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import finding_diff  # noqa: E402

PAIRS = [("alpha\nbeta", "alpha\nbeta\n"), ("alpha\nbeta\n", "alpha\nbeta"),
         ("alpha\r\nbeta\r\n", "alpha\nbeta\n"), ("alpha\nbeta", "alpha\rbeta"),
         ("alpha\nbeta", "alpha\u2028beta"), ("alpha\nbeta\n", "alpha\nbeta\r\n")]


def base(body):
    return {"id": "syn-diff-1", "title": "Synthetic", "slug": "synthetic", "summary": "S.", "body_md": body,
            "classification": "documented_fact", "confidence": "verified", "event_date": "2026-01-02",
            "author": "Synthetic Author", "sources": [], "limitations": [], "counterevidence": []}


class BodyEndingTests(unittest.TestCase):
    def test_exact_old_new_shape_and_ascii_render(self):
        for old, new in PAIRS:
            changes = finding_diff.diff(base(old), base(new))
            self.assertEqual(changes, [{"field": "body_md", "old": old, "new": new}], (old, new))
            self.assertEqual(list(changes[0]), ["field", "old", "new"])
            (line,) = finding_diff.render(changes)
            self.assertEqual(line, f"body_md: {json.dumps(old)} -> {json.dumps(new)}")
            self.assertTrue(line.isascii() and line.isprintable())

    def test_literal_backslash_n_differs_from_newline(self):
        (line,) = finding_diff.render(finding_diff.diff(base("a\nb"), base("a\nb\n")))
        self.assertEqual(line, 'body_md: "a\\nb" -> "a\\nb\\n"')
        (line,) = finding_diff.render([{"field": "body_md", "old": "a\\nb", "new": "\u00e9\t\u2029\"x"}])
        self.assertEqual(line, 'body_md: "a\\\\nb" -> "\\u00e9\\t\\u2029\\"x"')

    def test_equality_and_ordinary_hunks_unchanged(self):
        for empty in (None, ""):
            self.assertEqual(finding_diff.diff(base(empty), base("")), [])
        self.assertEqual(finding_diff.diff(base("alpha\nbeta"), base("alpha\nbeta")), [])
        self.assertEqual(finding_diff.diff(base("alpha\nbeta"), base("alpha\ngamma")),
                         [{"field": "body_md", "lines": ["@@ -1,2 +1,2 @@", " alpha", "-beta", "+gamma"]}])
        self.assertIn("lines", finding_diff.diff(base(""), base("\n"))[0])
        changes = finding_diff.diff(base("-- old detail"), base("++ new detail\n"))
        self.assertIn("lines", changes[0])
        self.assertIn("--- old detail", changes[0]["lines"])

    def test_other_fields_keep_order_and_single_body_change(self):
        new = base("alpha\nbeta\n")
        new["title"] = "Other"
        new["limitations"] = ["x"]
        changes = finding_diff.diff(base("alpha\nbeta"), new)
        self.assertEqual([c["field"] for c in changes], ["title", "body_md", "limitations"])

    def test_cli_forms_formats_exit_3_and_read_only(self):
        with tempfile.TemporaryDirectory(prefix="synthetic-rf0111-4-") as tmp:
            root = Path(tmp) / "campaign"
            (root / "content" / "findings").mkdir(parents=True)
            published = dict(base("alpha\nbeta"), state="published", published_at="2026-01-05T00:00:00Z",
                             corrections=[])
            paths = {}
            for name, doc in (("pub", published), ("old", base("alpha\nbeta")), ("new", base("alpha\nbeta\n"))):
                paths[name] = Path(tmp) / f"{name}.json"
                paths[name].write_text(json.dumps(doc), encoding="utf-8")
            (root / "content" / "findings" / "x.json").write_text(json.dumps(published), encoding="utf-8")
            before = {p: p.read_bytes() for p in list(paths.values()) + [root / "content" / "findings" / "x.json"]}
            forms = [[str(paths["old"]), str(paths["new"])], ["syn-diff-1", "--from", str(paths["new"])]]
            for form in forms:
                for as_json in (False, True):
                    cmd = [sys.executable, "-B", "-m", "campaign_tool", "finding", "diff", "--directory", str(root),
                           *form, *(["--json"] if as_json else [])]
                    r = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
                    self.assertEqual((r.returncode, r.stderr), (3, ""), cmd)
                    self.assertTrue(r.stdout.endswith("1 field(s) differ\n"))
                    if as_json:
                        data = json.loads(r.stdout[:-len("1 field(s) differ\n")])
                        self.assertEqual(data, [{"field": "body_md", "old": "alpha\nbeta", "new": "alpha\nbeta\n"}])
                    else:
                        self.assertEqual(r.stdout, 'body_md: "alpha\\nbeta" -> "alpha\\nbeta\\n"\n1 field(s) differ\n')
            self.assertEqual(before, {p: p.read_bytes() for p in before})


if __name__ == "__main__":
    unittest.main()
