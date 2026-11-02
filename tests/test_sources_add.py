"""`sources add`: register one ingested record in content/sources.json (synthetic campaign only)."""
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool.content import ContentError, load_content
from campaign_tool.site import build_site
from campaign_tool.sources_cmd import add_source

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
RECORD = b"synthetic RF2010-4 record bytes; not a real document\n"
DIGEST = hashlib.sha256(RECORD).hexdigest()


def cli(*args):
    return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *args], cwd=REPO_ROOT,
                          capture_output=True, text=True, timeout=120)


class SourcesAddTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-sources-add-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.sources = self.root / "content" / "sources.json"
        self.ledger = self.root / "private" / "ledger.sqlite"

    def ingest(self, data=RECORD):
        record = self.tmp / "record.bin"
        record.write_bytes(data)
        result = cli("ingest", "--directory", str(self.root), "--file", str(record), "--source-id", "synthetic-1")
        self.assertEqual(result.returncode, 0, result.stderr)
        return hashlib.sha256(data).hexdigest()

    def add(self, sha=DIGEST[:12], **overrides):
        fields = {"title": "Synthetic Cedar record", "agency_id": "ca-cedar-police",
                  "received_at": "2026-02-01T10:00:00+00:00", "fragment": "cedar-new-record"}
        fields.update(overrides)
        return add_source(self.root, sha=sha, **fields)

    def assertRefused(self, message, sha=DIGEST[:12], **overrides):
        before = self.sources.read_bytes() if self.sources.exists() else None
        with self.assertRaises(ValueError) as caught:
            self.add(sha, **overrides)
        self.assertIn(message, str(caught.exception))
        self.assertEqual(self.sources.read_bytes() if self.sources.exists() else None, before)
        self.assertEqual([p.name for p in self.sources.parent.glob(".sources-*")], [])

    def test_cli_twelve_char_prefix_appends_full_hash_and_links_the_finding(self):
        self.ingest()
        original = self.sources.read_text(encoding="utf-8")
        result = cli("sources", "add", "--directory", str(self.root), "--sha256", DIGEST[:12],
                     "--title", "Synthetic Cedar record", "--agency-id", "ca-cedar-police",
                     "--received-at", "2026-02-01T10:00:00+00:00", "--fragment", "cedar-new-record",
                     "--pages", "3", "--note", "Fixture bytes.")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout),
                         {"added": DIGEST[:12], "fragment": "cedar-new-record", "entries": 3})
        written = self.sources.read_text(encoding="utf-8")
        self.assertTrue(written.startswith(original.rstrip()[:-1].rstrip()), "existing entry bytes changed")
        self.assertTrue(written.endswith("}\n]\n"))
        document = json.loads(written)
        self.assertEqual(document[:2], json.loads(original))
        self.assertEqual(document[2]["sha256"], DIGEST)
        self.assertEqual((document[2]["pages"], document[2]["note"]), (3, "Fixture bytes."))
        model = load_content(self.root)
        self.assertEqual(model["sources"][2]["sha256"], DIGEST)
        finding_path = self.root / "content" / "findings" / "cedar-policy-posted.json"
        finding = json.loads(finding_path.read_text(encoding="utf-8"))
        finding["sources"][0]["sha256"] = DIGEST
        finding_path.write_text(json.dumps(finding), encoding="utf-8")
        build_site(self.root)
        page = (self.root / "public" / "findings" / "cedar-policy-posted.html").read_text(encoding="utf-8")
        self.assertIn('href="/sources.html#cedar-new-record"', page)

    def test_full_hash_accepted_and_absent_file_created_when_content_exists(self):
        self.ingest()
        self.sources.unlink()
        self.assertEqual(self.add(DIGEST), {"added": DIGEST[:12], "fragment": "cedar-new-record", "entries": 1})
        self.assertEqual([e["sha256"] for e in json.loads(self.sources.read_text(encoding="utf-8"))], [DIGEST])

    def test_short_or_non_hex_prefix_refused(self):
        self.ingest()
        self.assertRefused("at least 12", sha=DIGEST[:11])
        for bad in ("g" * 12, DIGEST[:12].upper() + "G", DIGEST[:12] + " ", "", DIGEST + "0"):
            self.assertRefused("at least 12", sha=bad)

    def test_never_ingested_or_no_ledger_refused_and_file_unchanged(self):
        self.assertRefused("no ledger: run ingest first")
        self.ingest()
        other = hashlib.sha256(b"never ingested synthetic bytes").hexdigest()
        self.assertRefused("not ingested: run ingest first", sha=other[:12])
        self.assertRefused("not ingested: run ingest first", sha=other)
        result = cli("sources", "add", "--directory", str(self.root), "--sha256", other[:12], "--title", "T",
                     "--agency-id", "ca-cedar-police", "--received-at", "2026-02-01T10:00:00+00:00",
                     "--fragment", "cedar-new-record")
        self.assertEqual((result.returncode, result.stdout), (1, ""))
        self.assertIn("Stopped: not ingested", result.stderr)

    def test_deeply_nested_sources_file_refused_by_cli(self):
        self.ingest()
        self.sources.write_bytes(b"[" * 200000 + b"]" * 200000)
        before = self.sources.read_bytes()
        result = cli("sources", "add", "--directory", str(self.root), "--sha256", DIGEST[:12], "--title", "T",
                     "--agency-id", "ca-cedar-police", "--received-at", "2026-02-01T10:00:00+00:00",
                     "--fragment", "cedar-new-record")
        self.assertEqual((result.returncode, result.stdout), (1, ""))
        self.assertIn("Stopped: sources.json: not valid UTF-8 JSON", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(self.sources.read_bytes(), before)
        self.assertEqual([p.name for p in self.sources.parent.glob(".sources-*")], [])

    def test_ambiguous_prefix_refused_longer_prefix_resolves(self):
        self.ingest()
        twin = DIGEST[:12] + ("0" if DIGEST[12] != "0" else "1") * 52
        with sqlite3.connect(self.ledger) as db:
            db.execute("INSERT INTO objects VALUES (?, ?, ?)", (twin, 1, "2026-02-01T00:00:00+00:00"))
        db.close()
        self.assertRefused("ambiguous prefix (2 matches)", sha=DIGEST[:12])
        self.assertEqual(self.add(DIGEST[:13])["entries"], 3)

    def test_duplicate_in_fragment_refused_other_fragment_accepted(self):
        self.ingest()
        self.add()
        self.assertRefused("duplicate sha256 within fragment 'cedar-new-record'")
        self.assertEqual(self.add(fragment="cedar-second-home")["entries"], 4)

    def test_invalid_fields_refused_with_loader_message_file_unchanged(self):
        self.ingest()
        self.assertRefused("'ca-nowhere-police' is not one of the campaign's agencies", agency_id="ca-nowhere-police")
        self.assertRefused("invalid agency identifier", agency_id="Cedar")
        self.assertRefused("'library' is the page anchor", fragment="library")
        self.assertRefused("must be a lowercase slug", fragment="Cedar Records")
        self.assertRefused("received_at: must include a time zone offset", received_at="2026-02-01T10:00:00")
        self.assertRefused("received_at: invalid ISO-8601 value", received_at="yesterday")
        self.assertRefused("public_url: must be an https:// URL", public_url="http://example.invalid/x")
        self.assertRefused("title: must not be empty", title="  ")
        self.assertRefused("plain text without angle brackets", title="<b>x</b>")
        self.assertRefused("--pages", pages=-1)

    def test_unselected_kit_agency_is_unknown_without_content_agencies(self):
        self.ingest()
        (self.root / "content" / "agencies.json").unlink()
        kit = self.root / "kit"
        kit.mkdir()
        (kit / "agencies.json").write_text(json.dumps([
            {"agency_id": "ca-cedar-police", "name": "Cedar Police", "kind": "police"},
            {"agency_id": "ca-pine-police", "name": "Pine Police", "kind": "police", "selected": False}]))
        self.assertRefused("'ca-pine-police' is not one of", agency_id="ca-pine-police")
        self.assertEqual(self.add()["entries"], 3)

    def test_symlinked_or_invalid_sources_file_refused(self):
        self.ingest()
        target = self.tmp / "elsewhere.json"
        target.write_bytes(self.sources.read_bytes())
        self.sources.unlink()
        self.sources.symlink_to(target)
        self.assertRefused("symlinks are not read or replaced")
        self.assertTrue(self.sources.is_symlink())
        self.assertEqual(json.loads(target.read_text(encoding="utf-8"))[-1]["fragment"], "cedar-police-records")
        self.sources.unlink()
        self.sources.symlink_to(self.tmp / "missing.json")
        self.assertRefused("symlinks are not read or replaced")
        self.assertFalse((self.tmp / "missing.json").exists())
        self.sources.unlink()
        self.sources.write_text("[{", encoding="utf-8")
        self.assertRefused("not valid UTF-8 JSON")
        self.sources.write_text("{}", encoding="utf-8")
        self.assertRefused("must be a list of library entries")

    def test_missing_content_directory_refused(self):
        self.ingest()
        shutil.rmtree(self.root / "content")
        with self.assertRaises(ContentError) as caught:
            self.add()
        self.assertIn("content/ is required", str(caught.exception))
        self.assertFalse((self.root / "content").exists())

    def test_five_thousand_entries_refuses_the_next(self):
        self.ingest()
        bulk = [{"sha256": hashlib.sha256(str(i).encode()).hexdigest(), "title": "t", "agency_id": "ca-cedar-police",
                 "received_at": "2026-01-01T00:00:00+00:00", "public_url": None, "fragment": "bulk"}
                for i in range(5000)]
        self.sources.write_text(json.dumps(bulk, separators=(",", ":")), encoding="utf-8")
        self.assertRefused("must be a list of library entries")

    def test_crash_before_replace_keeps_old_file_and_no_temp(self):
        self.ingest()
        before = self.sources.read_bytes()
        with mock.patch("campaign_tool.sources_cmd.os.replace", side_effect=OSError("simulated crash")):
            with self.assertRaises(OSError):
                self.add()
        self.assertEqual(self.sources.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.sources.parent.iterdir() if p.name.startswith(".")), [])

    def test_ledger_opened_read_only(self):
        self.ingest()
        before = (self.ledger.read_bytes(), self.ledger.stat().st_mtime_ns)
        self.add()
        self.assertRefused("duplicate sha256 within fragment")
        self.assertEqual((self.ledger.read_bytes(), self.ledger.stat().st_mtime_ns), before)
        self.assertEqual(sorted(p.name for p in self.ledger.parent.iterdir() if p.name.startswith("ledger")),
                         ["ledger.sqlite"])


if __name__ == "__main__":
    unittest.main()
