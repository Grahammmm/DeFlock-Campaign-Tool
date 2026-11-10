"""RF2315-3: `finding diff` reports a changed ``sources[].page`` and refuses malformed raw page values.
Synthetic files only.

Run from the repository root: python3 -B -m unittest tests.test_finding_diff_pages -q
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
from tests.test_finding_diff import SHA_A, SHA_B, finding, published  # noqa: E402

QUOTE_CANARY = "SYNTHETIC-QUOTE-CANARY-4417"
PAGE_CANARY = "private-looking-page-sentinel-9921"
PAGE_ONLY_TEXT = f"sources:\n~ {'a' * 12} page\n1 field(s) differ\n"
PAGE_ONLY_JSON = [{"field": "sources", "removed": [], "added": [],
                   "changed": [{"sha256": "a" * 12, "keys": ["page"]}]}]


def source(sha=SHA_A, locator="page:1", **extra):
    return {"sha256": sha, "locator": locator, **extra}


class FindingDiffPageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-finding-diff-pages-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "campaign"
        (self.root / "content" / "findings").mkdir(parents=True)
        self.published_path = self.root / "content" / "findings" / "one.json"
        (self.base / "unrelated.txt").write_text("sentinel", encoding="utf-8")

    def write(self, name, document):
        path = self.base / name
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def snapshot(self):
        tree = {}
        for folder, dirs, files in os.walk(self.base):
            for name in dirs + files:
                full = os.path.join(folder, name)
                info = os.lstat(full)
                content = Path(full).read_bytes() if os.path.isfile(full) and not os.path.islink(full) else None
                tree[os.path.relpath(full, self.base)] = (info.st_mode, info.st_size, info.st_mtime_ns, content)
        return tree

    def cli(self, *arguments, returncode, as_json=False):
        before = self.snapshot()
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "finding", "diff",
                                 "--directory", str(self.root), *map(str, arguments), *(["--json"] if as_json else [])],
                                cwd=REPO_ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, returncode, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(self.snapshot(), before, "finding diff must not write anything")
        return result

    def both_forms(self, old, new, returncode, as_json=False):
        """Run OLD NEW and published-vs-draft for the same pair; return the two results."""
        old_path, new_path = self.write("old.json", old), self.write("new.json", new)
        self.write("campaign/content/findings/one.json", published(**{k: v for k, v in old.items()
                                                                       if k not in ("id", "state")}))
        return (self.cli(old_path, new_path, returncode=returncode, as_json=as_json),
                self.cli(old["id"], "--from", new_path, returncode=returncode, as_json=as_json))

    def test_page_only_change_both_cli_forms_and_formats(self):
        old = finding(sources=[source(page=1, quote=QUOTE_CANARY), source(SHA_B, "page:2")])
        new = finding(sources=[source(page=2, quote=QUOTE_CANARY), source(SHA_B, "page:2")])
        self.assertEqual(finding_diff.diff(old, new), PAGE_ONLY_JSON)
        for result in self.both_forms(old, new, 3):
            self.assertEqual(result.stdout, PAGE_ONLY_TEXT)
            self.assertNotIn(SHA_A, result.stdout)
            self.assertNotIn(QUOTE_CANARY, result.stdout)
        for result in self.both_forms(old, new, 3, as_json=True):
            lines = result.stdout.splitlines()
            self.assertEqual(lines[-1], "1 field(s) differ")
            self.assertEqual(json.loads("\n".join(lines[:-1])), PAGE_ONLY_JSON)
            self.assertNotIn(QUOTE_CANARY, result.stdout)

    def test_page_add_remove_equal_and_boundaries(self):
        cases = ((None, 1, True), (100000, None, True), (1, 100000, True), (7, 7, False), (None, None, False))
        for before, after, differs in cases:
            with self.subTest(before=before, after=after):
                a = source(**({} if before is None else {"page": before}))
                b = source(**({} if after is None else {"page": after}))
                result = self.cli(self.write("old.json", finding(sources=[a])),
                                  self.write("new.json", finding(sources=[b])), returncode=3 if differs else 0)
                self.assertEqual(result.stdout, PAGE_ONLY_TEXT if differs else "no differences\n")
        # the pure API treats a normalized None like an absent key
        self.assertEqual(finding_diff.diff(finding(sources=[source(page=None)]), finding(sources=[source()])), [])

    def test_page_on_second_repeated_hash_is_compared(self):
        old = [source(locator="page:1", page=1), source(locator="page:2", page=2)]
        new = [source(locator="page:1", page=1), source(locator="page:2", page=3)]
        [entry] = finding_diff.diff(finding(sources=old), finding(sources=new))
        self.assertEqual(entry["changed"], [{"sha256": "a" * 12, "keys": ["page"]}])
        both = [dict(new[0], quote="x"), dict(new[1], quote="y")]
        [entry] = finding_diff.diff(finding(sources=old), finding(sources=both))
        self.assertEqual(entry["changed"][0]["keys"], ["page", "quote"])
        reordered = list(reversed(old))
        self.assertEqual(finding_diff.diff(finding(sources=old), finding(sources=reordered))[0]["changed"][0]["keys"],
                         ["locator", "page"])  # positional metadata semantics are unchanged

    def test_legacy_duplicate_counts_do_not_invent_optional_keys(self):
        old, new = [source()], [source(), source(locator="page:4")]
        for with_quote in (False, True):
            extra = {"quote": "Q"} if with_quote else {}
            a, b = [dict(s, **extra) for s in old], [dict(s, **extra) for s in new]
            [entry] = finding_diff.diff(finding(sources=a), finding(sources=b))
            self.assertEqual(entry["changed"][0]["keys"],
                             ["count", "locator", "title", "rule_id"] + (["quote"] if with_quote else []))
        legacy_old = [source()]
        legacy_new = [source(), source(locator="page:4", title="T", rule_id="R")]
        self.assertEqual(finding_diff.diff(finding(sources=legacy_old), finding(sources=legacy_new))[0]["changed"],
                         [{"sha256": "a" * 12, "keys": ["count", "locator", "title", "rule_id"]}])
        with_page = [source(), source(locator="page:4", page=3)]
        self.assertEqual(finding_diff.diff(finding(sources=legacy_old), finding(sources=with_page))[0]["changed"],
                         [{"sha256": "a" * 12, "keys": ["count", "locator", "title", "rule_id", "page"]}])

    def test_added_removed_hashes_and_distinct_hash_reorder_keep_old_shape(self):
        old = finding(sources=[source(page=1)])
        new = finding(sources=[source(page=1), source(SHA_B, "page:2", page=5)])
        self.assertEqual(finding_diff.diff(old, new),
                         [{"field": "sources", "removed": [], "added": ["b" * 12], "changed": []}])
        self.assertEqual(finding_diff.diff(new, old),
                         [{"field": "sources", "removed": ["b" * 12], "added": [], "changed": []}])
        swapped = finding(sources=list(reversed(new["sources"])))
        self.assertEqual(finding_diff.diff(new, swapped), [])
        result = self.cli(self.write("old.json", finding()), self.write("new.json", finding(summary="New.")),
                          returncode=3)
        self.assertEqual(result.stdout, 'summary: "Old summary." -> "New."\n1 field(s) differ\n')

    def test_malformed_raw_page_refuses_before_output(self):
        bad = [False, True, 0, -1, 100001, 1.0, "2", None, [1], {"n": 1}, PAGE_CANARY]
        self.write("campaign/content/findings/one.json", published())
        good = self.write("good.json", finding(sources=[source(page=1)]))
        for value in bad:
            malformed = finding(sources=[source(page=1), source(SHA_B, "page:2", page=value)])
            bad_path = self.write("bad.json", malformed)
            for label, arguments in (("old", (bad_path, good)), ("new", (good, bad_path)),
                                     ("from", ("syn-diff-1", "--from", bad_path))):
                for as_json in (False, True):
                    with self.subTest(value=repr(value)[:20], position=label, json=as_json):
                        result = self.cli(*arguments, returncode=1, as_json=as_json)
                        self.assertEqual(result.stdout, "")
                        self.assertTrue(result.stderr.startswith("Stopped: "), result.stderr)
                        self.assertIn("bad.json: sources[1].page: must be a whole number from 1 to 100000",
                                      result.stderr)
                        self.assertNotIn(PAGE_CANARY, result.stderr)
        # a malformed raw page in the published file is refused too (the stderr names its path, not its value)
        self.write("campaign/content/findings/one.json", published(sources=[source(page=0)]))
        result = self.cli("syn-diff-1", "--from", good, returncode=1)
        self.assertEqual(result.stdout, "")
        self.assertIn("one.json: sources[0].page: must be a whole number from 1 to 100000", result.stderr)
        # only real integers 1 and 100000, or an absent key, are accepted; partial drafts still load
        for page in (1, 100000):
            self.assertEqual(finding_diff.load(self.write("ok.json", {"id": "p", "sources": [source(page=page)]}))
                             ["sources"][0]["page"], page)
        self.assertNotIn("page", finding_diff.load(self.write("ok.json", {"id": "p", "sources": [source()]}))
                         ["sources"][0])

    def test_every_page_path_is_read_only(self):
        self.write("campaign/content/findings/one.json", published(sources=[source(page=1)]))
        same = self.write("same.json", finding(sources=[source(page=1)]))
        other = self.write("other.json", finding(sources=[source(page=2)]))
        broken = self.write("broken.json", finding(sources=[source(page=0)]))
        for arguments, code in (((same, same), 0), ((same, other), 3), (("syn-diff-1", "--from", other), 3),
                                (("syn-diff-1", "--from", same), 0), ((same, broken), 1)):
            with self.subTest(arguments=[str(a) for a in arguments]):
                self.cli(*arguments, returncode=code)  # cli() asserts the whole tree is byte/mode/mtime identical


if __name__ == "__main__":
    unittest.main()
