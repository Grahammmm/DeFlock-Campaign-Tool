"""RF0535-4: admit only exact source digests before a read-only finding diff."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from campaign_tool import finding_diff
from campaign_tool.findings import MAX_DRAFT_BYTES
from tests.test_finding_diff import SHA_A, SHA_B, finding, published

REPO = Path(__file__).resolve().parents[1]
BAD = "SYNPRIVATE!\n+"
REASON = "must be 64 lowercase hex characters"
SHAPE = "every entry of 'sources' must be an object with a 'sha256' string"


def source(sha: object = SHA_A, **extra):
    return {"sha256": sha, **extra}


class FindingDiffHashTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-diff-hashes-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "campaign"
        self.folder = self.root / "content" / "findings"
        self.folder.mkdir(parents=True)
        self.write("unrelated.json", {"synthetic": "untouched"})

    def write(self, name, document):
        path = self.base / name
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def publish(self, document):
        return self.write("campaign/content/findings/one.json", published(**document))

    def snapshot(self):
        result = {}
        for folder, dirs, files in os.walk(self.base):
            for name in dirs + files:
                path = Path(folder) / name
                info = path.lstat()
                payload = os.readlink(path) if path.is_symlink() else path.read_bytes() if path.is_file() else None
                result[str(path.relative_to(self.base))] = (info.st_mode, info.st_mtime_ns, info.st_size, payload)
        return result

    def cli(self, arguments, code, output=None, error=None, as_json=False):
        before = self.snapshot()
        result = subprocess.run(
            [sys.executable, "-B", "-m", "campaign_tool", "finding", "diff", "--directory", str(self.root),
             *map(str, arguments), *(["--json"] if as_json else [])],
            cwd=REPO, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        self.assertEqual(self.snapshot(), before, "all input bytes, types, modes, mtimes and membership must survive")
        if output is not None:
            self.assertEqual(result.stdout, output)
        self.assertEqual(result.stderr, "" if error is None else f"Stopped: {error}\n")
        for canary in ("SYNPRIVATE!", "LOCATOR-CANARY", "TITLE-CANARY", "QUOTE-CANARY", "Traceback"):
            self.assertNotIn(canary, result.stdout + result.stderr)
        return result

    def refuses_load(self, document, message):
        path = self.write("input.json", document)
        before = self.snapshot()
        with self.assertRaises(ValueError) as caught:
            finding_diff.load(path)
        self.assertEqual(str(caught.exception), f"{path}: {message}")
        self.assertEqual(self.snapshot(), before)

    def test_raw_source_digest_requires_exact_lowercase_hex(self):
        for digest in (SHA_A, "0123456789abcdef" * 4):
            document = {"id": "partial", "sources": [source(digest)]}
            self.assertEqual(finding_diff.load(self.write("valid.json", document)), document)
        bad = ("", "a", "a" * 12, "a" * 63, "a" * 65, "A" * 64, "a" * 63 + "B", "z" * 64,
               "ａ" * 64, "٠" * 64, " " + SHA_A, SHA_A + " ", SHA_A + "\n", "a" * 31 + "\n" + "a" * 32,
               SHA_A + "\r", "\t" + SHA_A, "\x1b" + SHA_A, "\x00" + SHA_A, BAD)
        for digest in bad:
            for index in (0, 2):
                with self.subTest(digest=repr(digest), index=index):
                    self.refuses_load({"id": "partial", "sources": [source()] * index + [source(digest)]},
                                      f"sources[{index}].sha256: {REASON}")

    def test_structural_and_page_error_precedence_is_stable(self):
        for value in (None, False, 1, [], {}):
            self.refuses_load({"id": "partial", "sources": [source(value)]}, SHAPE)
        for sources in (None, {}, [None], [{}], [source(BAD, page=0), {}]):
            self.refuses_load({"id": "partial", "sources": sources}, SHAPE)
        self.refuses_load({"id": "partial", "sources": [source(BAD, page=0)]}, f"sources[0].sha256: {REASON}")
        self.refuses_load({"id": "partial", "sources": [source(page=0)]},
                          "sources[0].page: must be a whole number from 1 to 100000")

    def test_bad_digest_refuses_both_cli_forms_formats_and_sides(self):
        good_doc = finding(sources=[source()])
        bad_doc = finding(sources=[source(), source(BAD, locator="LOCATOR-CANARY", title="TITLE-CANARY",
                                                       quote="QUOTE-CANARY")])
        good, bad = self.write("good.json", good_doc), self.write("bad.json", bad_doc)
        for as_json in (False, True):
            self.publish(good_doc)
            for arguments in ((bad, good), (good, bad), (bad, bad), ("syn-diff-1", "--from", bad)):
                with self.subTest(arguments=arguments, json=as_json):
                    self.cli(arguments, 1, "", f"{bad}: sources[1].sha256: {REASON}", as_json)
            published_path = self.publish(bad_doc)
            self.cli(("syn-diff-1", "--from", good), 1, "",
                     f"{published_path}: sources[1].sha256: {REASON}", as_json)
            self.cli(("syn-diff-1", "--from", bad), 1, "",
                     f"{published_path}: sources[1].sha256: {REASON}", as_json)

    def test_added_removed_and_shared_bad_hashes_never_reach_output(self):
        pairs = (([source()], [source(), source(BAD)]),
                 ([source(), source(BAD)], [source()]),
                 ([source(), source(BAD, locator="old")], [source(), source(BAD, locator="new")]))
        for old_sources, new_sources in pairs:
            old_doc, new_doc = finding(sources=old_sources), finding(sources=new_sources)
            old, new = self.write("old.json", old_doc), self.write("new.json", new_doc)
            published_path = self.publish(old_doc)
            for as_json in (False, True):
                path = old if len(old_sources) > 1 else new
                self.cli((old, new), 1, "", f"{path}: sources[1].sha256: {REASON}", as_json)
                path = published_path if len(old_sources) > 1 else new
                self.cli(("syn-diff-1", "--from", new), 1, "", f"{path}: sources[1].sha256: {REASON}", as_json)

    def test_all_published_candidates_are_validated_before_id_result(self):
        good = self.write("good.json", finding())
        self.publish(finding())
        for name in ("aaa.json", "zzz.json"):
            sibling = self.write(f"campaign/content/findings/{name}", published(id="other", sources=[source(BAD)]))
            for as_json in (False, True):
                self.cli(("syn-diff-1", "--from", good), 1, "",
                         f"{sibling}: sources[0].sha256: {REASON}", as_json)
            sibling.unlink()

    def test_valid_hash_comparisons_keep_exact_text_json_and_prefix_identity(self):
        # The removed and added full digests deliberately have the same display prefix.
        colliding = "a" * 12 + "b" * 52
        old_doc = finding(sources=[source(), source(SHA_B, locator="old", page=1, quote="old")])
        new_doc = finding(sources=[source(colliding), source(SHA_B, locator="new", page=2, quote="new"),
                                   source(SHA_B)])
        expected_text = ("sources:\n- aaaaaaaaaaaa\n+ aaaaaaaaaaaa\n"
                         "~ bbbbbbbbbbbb count, locator, title, rule_id, page, quote\n1 field(s) differ\n")
        expected_json = ('[\n  {\n    "field": "sources",\n    "removed": [\n      "aaaaaaaaaaaa"\n    ],\n'
                         '    "added": [\n      "aaaaaaaaaaaa"\n    ],\n    "changed": [\n      {\n'
                         '        "sha256": "bbbbbbbbbbbb",\n        "keys": [\n          "count",\n'
                         '          "locator",\n          "title",\n          "rule_id",\n          "page",\n'
                         '          "quote"\n        ]\n      }\n    ]\n  }\n]\n1 field(s) differ\n')
        old, new = self.write("old.json", old_doc), self.write("new.json", new_doc)
        self.publish(old_doc)
        for arguments in ((old, new), ("syn-diff-1", "--from", new)):
            self.cli(arguments, 3, expected_text)
            self.cli(arguments, 3, expected_json, as_json=True)
        # Distinct hashes reorder without a change; repeated hashes compare metadata positionally.
        reordered = self.write("reordered.json", finding(sources=list(reversed(old_doc["sources"]))))
        self.cli((old, reordered), 0, "no differences\n")
        repeated = self.write("repeated.json", {"id": "partial", "sources": [source(locator="x"), source(locator="y")]})
        swapped = self.write("swapped.json", {"id": "partial", "sources": [source(locator="y"), source(locator="x")]})
        self.cli((repeated, swapped), 3, "sources:\n~ aaaaaaaaaaaa locator\n1 field(s) differ\n")
        self.assertEqual(finding_diff.diff({"sources": [source(page=None)]}, {"sources": [source()]}), [])

    def test_digest_success_and_refusal_leave_all_inputs_untouched(self):
        for document in ({"id": "partial"}, {"id": "partial", "sources": []}):
            path = self.write("partial.json", document)
            self.assertEqual(finding_diff.load(path), document)
            for as_json in (False, True):
                self.cli((path, path), 0, "[]\nno differences\n" if as_json else "no differences\n", as_json=as_json)
        good = self.write("good.json", {"id": "partial"})
        link = self.base / "link.json"
        link.symlink_to(good)
        invalid = self.base / "invalid.json"
        invalid.write_text("{not json", encoding="utf-8")
        oversized = self.base / "oversized.json"
        oversized.write_bytes(b" " * (MAX_DRAFT_BYTES + 1))
        for path, reason in ((link, "draft is a symlink; refusing to follow it"), (invalid, "draft is not valid JSON"),
                             (oversized, "draft is larger than 1048576 bytes")):
            for as_json in (False, True):
                self.cli((good, path), 1, "", f"{path}: {reason}", as_json)


if __name__ == "__main__":
    unittest.main()
