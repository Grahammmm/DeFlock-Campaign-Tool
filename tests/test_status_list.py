"""`status --list`: ingested ledger receipts with 12-char prefixes, read-only (synthetic campaign only)."""
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from campaign_tool import ledger_list
from campaign_tool.sources_cmd import add_source

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
FILE_A = b"synthetic DF2237-17 record A; not a real document\n"
FILE_B = b"synthetic DF2237-17 record B; not a real document\n"
SHA_A, SHA_B = (hashlib.sha256(data).hexdigest() for data in (FILE_A, FILE_B))
FULL_HASH = re.compile(r"[0-9a-f]{64}")


def cli(*args):
    return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *args], cwd=REPO_ROOT,
                          capture_output=True, text=True, timeout=120)


class StatusListTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-status-list-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.sources = self.root / "content" / "sources.json"
        self.ledger = self.root / "private" / "ledger.sqlite"

    def ingest(self, name, data, source_id):
        path = self.tmp / name
        path.write_bytes(data)
        result = cli("ingest", "--directory", str(self.root), "--file", str(path), "--source-id", source_id)
        self.assertEqual(result.returncode, 0, result.stderr)

    def populate(self):
        """Two sources, three ingests: A and B under source-one, A again under source-two."""
        self.ingest("a.txt", FILE_A, "source-one")
        self.ingest("b.txt", FILE_B, "source-one")
        self.ingest("a-again.txt", FILE_A, "source-two")

    def listing(self, *extra):
        result = cli("status", "--directory", str(self.root), "--list", *extra)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(FULL_HASH.search(result.stdout), "a full 64-hex hash was printed")
        return json.loads(result.stdout)

    def assertStopped(self, *args, message):
        result = cli("status", "--directory", str(self.root), *args)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("Stopped: " + message, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_three_receipts_in_received_order_with_prefixes_and_no_full_hash(self):
        self.populate()
        report = self.listing()
        self.assertEqual(report["count"], 3)
        self.assertNotIn("more", report)
        rows = report["receipts"]
        self.assertEqual([(r["sha256_prefix"], r["source_id"], r["original_name"]) for r in rows],
                         [(SHA_A[:12], "source-one", "a.txt"), (SHA_B[:12], "source-one", "b.txt"),
                          (SHA_A[:12], "source-two", "a-again.txt")])
        self.assertEqual([r["bytes"] for r in rows], [len(FILE_A), len(FILE_B), len(FILE_A)])
        self.assertEqual([r["received_at"] for r in rows], sorted(r["received_at"] for r in rows))
        self.assertEqual(set(rows[0]), {"sha256_prefix", "bytes", "source_id", "original_name", "received_at"})
        self.assertNotIn("stored_objects", json.dumps(report))

    def test_source_id_and_since_filters(self):
        self.populate()
        rows = self.listing()["receipts"]
        self.assertEqual([r["original_name"] for r in self.listing("--source-id", "source-two")["receipts"]],
                         ["a-again.txt"])
        self.assertEqual(self.listing("--source-id", "source-thr")["count"], 0)
        self.assertEqual(self.listing("--since", "2000-01-01")["count"], 3)
        self.assertEqual(self.listing("--since", "2999-01-01")["count"], 0)
        third = datetime.fromisoformat(rows[2]["received_at"])
        shifted = third.astimezone(timezone(timedelta(hours=-8))).isoformat()
        self.assertEqual([r["original_name"] for r in self.listing("--since", shifted)["receipts"]],
                         ["a-again.txt"])
        self.assertEqual(self.listing("--since", shifted, "--source-id", "source-one")["count"], 0)
        for bad in ("yesterday", "2026-13-01", "2026-11-01T10:00:00", "2026-11-01T25:00:00+00:00", "1"):
            self.assertStopped("--list", "--since", bad,
                               message="--since: use YYYY-MM-DD or an ISO timestamp with offset")
        self.assertStopped("--list", "--source-id", "x" * 201, message="--source-id: use 1-200 characters")
        self.assertStopped("--list", "--source-id", "", message="--source-id: use 1-200 characters")

    def test_not_in_sources_drops_registered_objects(self):
        self.populate()
        add_source(self.root, sha=SHA_A[:12], title="Synthetic record A", agency_id="ca-cedar-police",
                   received_at="2026-02-01T10:00:00+00:00", fragment="synthetic-record-a")
        self.assertEqual(self.listing()["count"], 3)
        kept = self.listing("--not-in-sources")
        self.assertEqual([(r["sha256_prefix"], r["original_name"]) for r in kept["receipts"]], [(SHA_B[:12], "b.txt")])
        self.sources.unlink()
        self.assertEqual(self.listing("--not-in-sources")["count"], 3)
        self.sources.write_text("{", encoding="utf-8")
        self.assertStopped("--list", "--not-in-sources", message="sources.json: not valid UTF-8 JSON")
        self.sources.write_text('[{"sha256": "short"}]', encoding="utf-8")
        result = cli("status", "--directory", str(self.root), "--list", "--not-in-sources")
        self.assertEqual((result.returncode, result.stdout), (1, ""))
        self.assertIn("Stopped: sources.json[0]", result.stderr)

    def test_no_ledger_lists_nothing_and_symlinked_ledger_is_refused(self):
        self.assertFalse(os.path.lexists(self.ledger))
        self.assertEqual(self.listing(), {"receipts": [], "count": 0})
        self.populate()
        real = self.tmp / "elsewhere.sqlite"
        self.ledger.rename(real)
        self.ledger.symlink_to(real)
        self.assertStopped("--list", message="private/ledger.sqlite must be a regular file, not a symlink")
        self.ledger.unlink()
        self.ledger.mkdir()
        self.assertStopped("--list", message="private/ledger.sqlite must be a regular file, not a symlink")

    def test_original_name_control_characters_replaced_and_cut(self):
        self.ingest("bell\x07name\nline.txt", FILE_A, "source-one")
        self.assertEqual(self.listing()["receipts"][0]["original_name"], "bell?name?line.txt")
        # Real file names stop at 255 bytes, so a longer stored name is set directly in the synthetic ledger.
        with sqlite3.connect(self.ledger) as db:
            db.execute("UPDATE receipts SET original_name = ?", ("\x1b[31m" + "n" * 300,))
        db.close()
        name = self.listing()["receipts"][0]["original_name"]
        self.assertEqual(name, "?[31m" + "n" * 195)
        self.assertEqual(len(name), 200)

    def test_listing_is_read_only(self):
        self.populate()
        add_source(self.root, sha=SHA_B[:12], title="Synthetic record B", agency_id="ca-cedar-police",
                   received_at="2026-02-01T10:00:00+00:00", fragment="synthetic-record-b")
        before = (self.ledger.read_bytes(), self.ledger.stat().st_mtime_ns, sorted(os.listdir(self.ledger.parent)))
        self.listing("--source-id", "source-one", "--since", "2000-01-01", "--not-in-sources")
        self.listing()
        after = (self.ledger.read_bytes(), self.ledger.stat().st_mtime_ns, sorted(os.listdir(self.ledger.parent)))
        self.assertEqual(before, after)
        for suffix in ("-wal", "-journal", "-shm"):
            self.assertFalse(os.path.lexists(str(self.ledger) + suffix), suffix)

    def test_checkpointed_wal_ledger_listed_without_creating_sidecars(self):
        self.populate()
        db = sqlite3.connect(self.ledger)
        self.assertEqual(db.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
        db.close()  # last connection closed: WAL checkpointed and its -wal/-shm files removed
        self.assertEqual(self.ledger.read_bytes()[18:20], b"\2\2")
        before = (self.ledger.read_bytes(), self.ledger.stat().st_mtime_ns, sorted(os.listdir(self.ledger.parent)))
        self.assertFalse(any(name.startswith("ledger.sqlite-") for name in before[2]), before[2])
        self.assertEqual(self.listing()["count"], 3)
        self.assertEqual(self.listing("--source-id", "source-two")["count"], 1)
        after = (self.ledger.read_bytes(), self.ledger.stat().st_mtime_ns, sorted(os.listdir(self.ledger.parent)))
        self.assertEqual(before, after)

    def test_journal_or_wal_sidecar_refused_and_left_alone(self):
        self.populate()
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(str(self.ledger) + suffix)
            sidecar.write_bytes(b"")
            before = sorted(os.listdir(self.ledger.parent)), self.ledger.read_bytes()
            self.assertStopped("--list", message=ledger_list.SIDECAR_ERROR)
            self.assertEqual((sorted(os.listdir(self.ledger.parent)), self.ledger.read_bytes()), before)
            sidecar.unlink()

    def test_unreadable_ledger_refused(self):
        self.ledger.parent.mkdir(exist_ok=True)
        self.ledger.write_bytes(b"not a synthetic sqlite database")
        self.assertStopped("--list", message="private/ledger.sqlite is not a readable ledger")

    def test_filters_need_list_and_list_refuses_check(self):
        self.populate()
        for args in (("--source-id", "source-one"), ("--since", "2000-01-01"), ("--not-in-sources",)):
            self.assertStopped(*args, message="--source-id/--since/--not-in-sources need --list")
        for args in (("--list", "--check"), ("--list", "--fix-modes"), ("--list", "--check", "--source-id", "s")):
            self.assertStopped(*args, message="--list is read-only; run --check separately")
        counts = cli("status", "--directory", str(self.root))
        self.assertEqual(counts.returncode, 0, counts.stderr)
        self.assertEqual(json.loads(counts.stdout)["receipt_occurrences"], 3)

    def test_cap_reports_more(self):
        self.populate()
        with mock.patch.object(ledger_list, "MAX_ROWS", 2):
            report = ledger_list.list_receipts(self.root)
        self.assertEqual((report["count"], report["more"], len(report["receipts"])), (2, 1, 2))


if __name__ == "__main__":
    unittest.main()
