"""`sources list | edit | remove`: look after content/sources.json (synthetic campaign only)."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool.sources_cmd import add_source, edit_source, list_sources, remove_source

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_CONTENT = REPO_ROOT / "examples" / "fictional-campaign" / "content"
RECORD = b"synthetic RF1500-1 record bytes; not a real document\n"
DIGEST = hashlib.sha256(RECORD).hexdigest()
CITING = "cedar-policy-posted"


def cli(*args):
    return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *args], cwd=REPO_ROOT,
                          capture_output=True, text=True, timeout=120)


def sha_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class SourcesManageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-sources-manage-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        result = cli("init", "--directory", str(self.root), "--name", "Synthetic", "--county", "Cedar", "--state", "CA")
        self.assertEqual(result.returncode, 0, result.stderr)
        shutil.copytree(EXAMPLE_CONTENT, self.root / "content")
        self.sources = self.root / "content" / "sources.json"
        record = self.tmp / "record.bin"
        record.write_bytes(RECORD)
        result = cli("ingest", "--directory", str(self.root), "--file", str(record), "--source-id", "synthetic-1")
        self.assertEqual(result.returncode, 0, result.stderr)
        add_source(self.root, sha=DIGEST[:12], title="Synthetic new record", agency_id="ca-cedar-police",
                   received_at="2026-02-01T10:00:00+00:00", fragment="cedar-new-record")
        self.cited, self.other = (entry["sha256"] for entry in self.library()[:2])

    def library(self):
        return json.loads(self.sources.read_text(encoding="utf-8"))

    def write_library(self, document):
        self.sources.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")

    def write_malformed_finding(self):
        """A published, verified finding with every required key but classification=[] (TypeError in the loader)."""
        findings = self.root / "content" / "findings"
        finding = json.loads((findings / f"{CITING}.json").read_text(encoding="utf-8"))
        self.assertEqual((finding["state"], finding["confidence"]), ("published", "verified"))
        finding.update(classification=[], slug="synthetic-malformed", id="synthetic-malformed")
        (findings / "malformed.json").write_text(json.dumps(finding), encoding="utf-8")

    def run_cli(self, command, *args):
        return cli("sources", command, "--directory", str(self.root), *args)

    def assertStopped(self, result, message):
        self.assertEqual((result.returncode, result.stdout), (1, ""), result.stderr)
        self.assertTrue(result.stderr.startswith("Stopped: "), result.stderr)
        self.assertIn(message, result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def assertUnchanged(self, before):
        self.assertEqual(self.sources.read_bytes() if self.sources.exists() else None, before)
        self.assertEqual([p.name for p in self.sources.parent.glob(".sources-*")], [])

    def test_list_absent_empty_and_three_entries_with_cited_by(self):
        result = self.run_cli("list")
        self.assertEqual(result.returncode, 0, result.stderr)
        listed = json.loads(result.stdout)
        self.assertEqual(listed["count"], 3)
        self.assertEqual([(e["sha256"], e["fragment"], e["cited_by"]) for e in listed["entries"]],
                         [(self.cited[:12], "cedar-police-records", [CITING]),
                          (self.other[:12], "cedar-police-records", []), (DIGEST[:12], "cedar-new-record", [])])
        self.assertEqual(listed["entries"][2], {
            "sha256": DIGEST[:12], "fragment": "cedar-new-record", "title": "Synthetic new record",
            "agency_id": "ca-cedar-police", "received_at": "2026-02-01T10:00:00+00:00", "public_url": None,
            "pages": None, "cited_by": []})
        self.assertEqual(listed["entries"][1]["public_url"], "https://example.invalid/fixtures/agreement-excerpt")
        (self.root / "content" / "findings" / "broken.json").write_text("{", encoding="utf-8")
        self.assertEqual(list_sources(self.root), listed)  # unreadable finding skipped by list
        self.write_malformed_finding()
        result = self.run_cli("list")
        self.assertEqual((result.returncode, json.loads(result.stdout)), (0, listed), result.stderr)
        self.write_library([])
        self.assertEqual(list_sources(self.root), {"entries": [], "count": 0})
        self.sources.unlink()
        self.assertEqual(list_sources(self.root), {"entries": [], "count": 0})
        self.assertFalse(self.sources.exists())

    def test_edit_title_and_clear_public_url_change_only_those_keys(self):
        os.chmod(self.sources, 0o640)
        before = self.library()
        result = self.run_cli("edit", "--sha256", self.other[:12], "--title", "Corrected title", "--clear-public-url")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(json.loads(lines[0]), {"edited": self.other[:12], "fragment": "cedar-police-records",
                                                "changed": ["public_url", "title"]})
        self.assertEqual(lines[1:], ["Nothing was published."])
        after = self.library()
        self.assertEqual(after[1], {**before[1], "title": "Corrected title", "public_url": None})
        self.assertEqual(list(after[1]), list(before[1]))
        self.assertEqual((after[0], after[2]), (before[0], before[2]))
        self.assertEqual(oct(self.sources.stat().st_mode & 0o777), oct(0o640))
        self.assertEqual(edit_source(self.root, DIGEST[:12], pages=0, note="Fixture.", public_url="https://example.invalid/r"),
                         {"edited": DIGEST[:12], "fragment": "cedar-new-record", "changed": ["note", "pages", "public_url"]})
        self.assertEqual(edit_source(self.root, DIGEST[:12], note="")["changed"], ["note"])
        self.assertNotIn("note", self.library()[2])
        self.assertEqual(self.library()[:2], after[:2])

    def test_edit_refusals_leave_file_byte_identical(self):
        before = self.sources.read_bytes()
        cases = [((), "nothing to change"),
                 (("--title", "Synthetic Cedar Police ALPR policy"), "nothing to change"),
                 (("--public-url", "http://example.invalid/x"), "public_url: must be an https:// URL"),
                 (("--pages", "-1"), "--pages: must be a whole number"),
                 (("--public-url", "https://example.invalid/x", "--clear-public-url"), "cannot be combined"),
                 (("--title", "<b>x</b>"), "plain text without angle brackets"),
                 (("--title", "  "), "title: must not be empty")]
        for extra, message in cases:
            with self.subTest(extra=extra):
                self.assertStopped(self.run_cli("edit", "--sha256", self.cited[:12], *extra), message)
                self.assertUnchanged(before)

    def test_prefix_refusals_and_fragment_narrowing(self):
        before = self.sources.read_bytes()
        for prefix, message in ((DIGEST[:11], "at least 12 lowercase hex"), (DIGEST[:12].upper(), "at least 12 lowercase hex"),
                                (hashlib.sha256(b"unknown synthetic").hexdigest()[:12], "no library entry for")):
            with self.subTest(prefix=prefix):
                self.assertStopped(self.run_cli("edit", "--sha256", prefix, "--title", "T"), message)
                self.assertStopped(self.run_cli("remove", "--sha256", prefix), message)
                self.assertUnchanged(before)
        self.assertStopped(self.run_cli("remove", "--sha256", DIGEST[:12], "--fragment", "cedar-police-records"),
                           "no library entry for " + DIGEST[:12] + " in fragment 'cedar-police-records'")
        add_source(self.root, sha=DIGEST[:12], title="Second home", agency_id="ca-cedar-police",
                   received_at="2026-02-02T10:00:00+00:00", fragment="cedar-second-home")
        before = self.sources.read_bytes()
        message = "ambiguous: 2 entries; add --fragment (one of: cedar-new-record, cedar-second-home)"
        self.assertStopped(self.run_cli("edit", "--sha256", DIGEST[:12], "--title", "T"), message)
        self.assertStopped(self.run_cli("remove", "--sha256", DIGEST), message)
        self.assertUnchanged(before)
        self.assertEqual(edit_source(self.root, DIGEST[:12], "cedar-second-home", title="T")["fragment"], "cedar-second-home")
        self.assertEqual([e["title"] for e in self.library()[2:]], ["Synthetic new record", "T"])
        twin = DIGEST[:12] + ("0" if DIGEST[12] != "0" else "1") * 52
        self.write_library(self.library()[:3] + [{**self.library()[2], "sha256": twin}])
        with self.assertRaisesRegex(ValueError, r"ambiguous prefix \(2 entries\); give more hex characters"):
            remove_source(self.root, DIGEST[:12], "cedar-new-record")
        self.assertEqual(remove_source(self.root, DIGEST[:13], "cedar-new-record")["entries"], 3)

    def test_remove_cited_record_refused_until_another_fragment_keeps_it(self):
        before = self.sources.read_bytes()
        refusal = f"cited by published finding(s): {CITING}; correct or withdraw them first"
        self.assertStopped(self.run_cli("remove", "--sha256", self.cited[:12]), refusal)
        self.assertUnchanged(before)
        self.write_library(self.library() + [{**self.library()[0], "fragment": "cedar-second-home"}])
        self.assertEqual(remove_source(self.root, self.cited[:12], "cedar-police-records"),
                         {"removed": self.cited[:12], "fragment": "cedar-police-records", "entries": 3})
        before = self.sources.read_bytes()
        with self.assertRaisesRegex(ValueError, "cited by published finding"):
            remove_source(self.root, self.cited[:12])
        self.assertUnchanged(before)
        self.assertEqual([e["fragment"] for e in self.library() if e["sha256"] == self.cited], ["cedar-second-home"])

    def test_remove_refused_when_a_finding_cannot_be_read(self):
        (self.root / "content" / "findings" / "broken.json").write_text("{", encoding="utf-8")
        before = self.sources.read_bytes()
        self.assertStopped(self.run_cli("remove", "--sha256", DIGEST[:12]), "findings/broken.json cannot be read")
        self.assertUnchanged(before)

    def test_remove_refused_when_a_finding_has_a_malformed_field(self):
        self.write_malformed_finding()
        before = self.sources.read_bytes()
        self.assertStopped(self.run_cli("remove", "--sha256", DIGEST[:12]), "findings/malformed.json cannot be read")
        self.assertUnchanged(before)

    def test_remove_uncited_entry_keeps_ledger_and_objects_and_build_passes(self):
        ledger, blob = self.root / "private" / "ledger.sqlite", self.root / "private" / "objects" / DIGEST
        hashes = (sha_file(ledger), sha_file(blob))
        result = self.run_cli("remove", "--sha256", DIGEST[:12])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [
            json.dumps({"removed": DIGEST[:12], "fragment": "cedar-new-record", "entries": 2}), "Nothing was published."])
        self.assertNotIn(DIGEST, [e["sha256"] for e in self.library()])
        self.assertEqual(remove_source(self.root, self.other[:12])["entries"], 1)
        self.assertEqual(self.sources.read_text(encoding="utf-8"), json.dumps(self.library(), indent=2) + "\n")
        build = cli("build", "--directory", str(self.root), "--check")
        self.assertEqual(build.returncode, 0, build.stderr)
        self.assertIn("Public tree check passed", build.stdout)
        self.assertEqual((sha_file(ledger), sha_file(blob)), hashes)

    def test_symlink_invalid_json_and_missing_content_refused_nothing_created(self):
        target = self.tmp / "elsewhere.json"
        target.write_bytes(self.sources.read_bytes())
        self.sources.unlink()
        self.sources.symlink_to(target)
        target_before = target.read_bytes()
        writers = (("list",), ("edit", "--sha256", DIGEST[:12], "--title", "T"), ("remove", "--sha256", DIGEST[:12]))
        for args in writers:
            with self.subTest(args=args, case="symlink"):
                self.assertStopped(self.run_cli(*args), "symlinks are not read or replaced")
                self.assertTrue(self.sources.is_symlink())
                self.assertEqual(target.read_bytes(), target_before)
        self.sources.unlink()
        self.sources.write_text("[{", encoding="utf-8")
        for args in writers:
            with self.subTest(args=args, case="invalid"):
                self.assertStopped(self.run_cli(*args), "sources.json: not valid UTF-8 JSON")
                self.assertUnchanged(b"[{")
        shutil.rmtree(self.root / "content")
        for args in writers:
            with self.subTest(args=args, case="no content"):
                self.assertStopped(self.run_cli(*args), "content/ is required")
                self.assertFalse((self.root / "content").exists())

    def test_replace_failure_keeps_old_file_and_no_temp(self):
        before = self.sources.read_bytes()
        with mock.patch("campaign_tool.sources_cmd.os.replace", side_effect=OSError("simulated crash")):
            with self.assertRaises(OSError):
                edit_source(self.root, DIGEST[:12], title="T")
            with self.assertRaises(OSError):
                remove_source(self.root, DIGEST[:12])
        self.assertUnchanged(before)
        self.assertEqual(sorted(p.name for p in self.sources.parent.iterdir() if p.name.startswith(".")), [])


if __name__ == "__main__":
    unittest.main()
