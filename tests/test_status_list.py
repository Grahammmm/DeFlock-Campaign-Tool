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

    def fd_count(self):
        return len(os.listdir("/proc/self/fd"))

    def test_snapshot_size_budget_exact_and_plus_one(self):
        self.populate()
        size = self.ledger.stat().st_size
        with mock.patch.object(ledger_list, "MAX_LEDGER_BYTES", size):
            self.assertEqual(ledger_list.list_receipts(self.root)["count"], 3)  # exactly at the limit
        with open(self.ledger, "ab") as handle:
            handle.write(b"\0")  # one physical trailing byte past the limit
        before = (self.ledger.read_bytes(), self.ledger.stat().st_mtime_ns, sorted(os.listdir(self.ledger.parent)))
        with mock.patch.object(ledger_list, "MAX_LEDGER_BYTES", size), \
                mock.patch.object(ledger_list.os, "fdopen") as opened, \
                mock.patch.object(ledger_list.sqlite3, "connect") as connect:
            with self.assertRaises(ValueError) as caught:
                ledger_list.list_receipts(self.root)
        self.assertEqual(str(caught.exception), ledger_list.LEDGER_SIZE_ERROR)
        opened.assert_not_called()
        connect.assert_not_called()
        self.assertEqual(before, (self.ledger.read_bytes(), self.ledger.stat().st_mtime_ns,
                                  sorted(os.listdir(self.ledger.parent))))
        # under the limit, empty and malformed files keep the generic refusal
        for content in (b"", b"not a synthetic sqlite database"):
            self.ledger.write_bytes(content)
            with mock.patch.object(ledger_list, "MAX_LEDGER_BYTES", 1024):
                with self.assertRaises(ValueError) as caught:
                    ledger_list.list_receipts(self.root)
            self.assertEqual(str(caught.exception), "private/ledger.sqlite is not a readable ledger")

    def test_snapshot_growth_after_fstat_is_bounded(self):
        self.populate()
        size = self.ledger.stat().st_size
        inode = self.ledger.stat().st_ino
        real_fstat, real_fdopen = os.fstat, os.fdopen
        reads, handles, grown = [], [], []

        def fstat(fd):
            info = real_fstat(fd)  # the genuine descriptor size, then the file grows
            if info.st_ino == inode and not grown:
                with open(self.ledger, "ab") as handle:
                    handle.write(b"\0" * 4096)
                grown.append(True)
            return info

        class Spy:
            def __init__(self, handle):
                self.handle = handle
                handles.append(handle)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.handle.close()

            def read(self, *arguments):
                data = self.handle.read(*arguments)
                reads.append((arguments, len(data)))
                return data

        descriptors = self.fd_count()
        with mock.patch.object(ledger_list, "MAX_LEDGER_BYTES", size), \
                mock.patch.object(ledger_list.os, "fstat", fstat), \
                mock.patch.object(ledger_list.os, "fdopen", lambda *a, **k: Spy(real_fdopen(*a, **k))), \
                mock.patch.object(ledger_list.sqlite3, "connect") as connect:
            with self.assertRaises(ValueError) as caught:
                ledger_list.list_receipts(self.root)
        self.assertEqual(str(caught.exception), ledger_list.LEDGER_SIZE_ERROR)
        self.assertEqual(grown, [True])
        self.assertEqual(reads, [((size + 1,), size + 1)])  # one finite read, stopped at limit + 1
        self.assertTrue(all(handle.closed for handle in handles))
        self.assertEqual(self.fd_count(), descriptors)
        connect.assert_not_called()

    def test_oversized_status_cli_has_no_output_or_side_effects(self):
        self.ledger.parent.mkdir(exist_ok=True)
        with open(self.ledger, "wb") as handle:
            handle.truncate(ledger_list.MAX_LEDGER_BYTES + 1)  # sparse: production limit plus one byte
        before = (self.ledger.stat().st_size, self.ledger.stat().st_mode, self.ledger.stat().st_mtime_ns,
                  sorted(os.listdir(self.ledger.parent)))
        self.assertEqual(before[0], 64 * 1024 * 1024 + 1)
        self.assertStopped("--list", message="private/ledger.sqlite exceeds the 64 MiB snapshot limit")
        self.assertStopped("--list", "--source-id", "source-one", message=ledger_list.LEDGER_SIZE_ERROR)
        after = self.ledger.stat()
        self.assertEqual(before, (after.st_size, after.st_mode, after.st_mtime_ns, sorted(os.listdir(self.ledger.parent))))

    def test_snapshot_size_gate_preserves_sidecar_and_file_type_refusals(self):
        self.populate()
        size = self.ledger.stat().st_size
        with mock.patch.object(ledger_list, "MAX_LEDGER_BYTES", size - 1):
            Path(str(self.ledger) + "-wal").write_bytes(b"")
            with self.assertRaises(ValueError) as caught:
                ledger_list.list_receipts(self.root)
            self.assertEqual(str(caught.exception), ledger_list.SIDECAR_ERROR)  # the sidecar check still comes first
            Path(str(self.ledger) + "-wal").unlink()
            with self.assertRaises(ValueError) as caught:
                ledger_list.list_receipts(self.root)
            self.assertEqual(str(caught.exception), ledger_list.LEDGER_SIZE_ERROR)
        real = self.tmp / "elsewhere.sqlite"
        self.ledger.rename(real)
        self.ledger.symlink_to(real)
        with mock.patch.object(ledger_list, "MAX_LEDGER_BYTES", size - 1):
            with self.assertRaises(ValueError) as caught:
                ledger_list.list_receipts(self.root)
        self.assertEqual(str(caught.exception), "private/ledger.sqlite must be a regular file, not a symlink")

    def test_cap_reports_more(self):
        self.populate()
        with mock.patch.object(ledger_list, "MAX_ROWS", 2):
            report = ledger_list.list_receipts(self.root)
        self.assertEqual((report["count"], report["more"], len(report["receipts"])), (2, 1, 2))

    # --- RF0325-4: --since values whose UTC instant is outside years 0001-9999 are a controlled refusal ---
    UNDERFLOW = ("0001-01-01T00:00:00+00:01", "0001-01-01T00:00:00.999999+00:01", "0001-01-01T00:00:00+23:59",
                 "0001-01-01T00:00:00.000000+00:00:01")
    OVERFLOW = ("9999-12-31T23:59:59-00:01", "9999-12-31T23:59:59.999999-00:01", "9999-12-31T23:59:59-23:59")
    ACCEPTED = {
        "0001-01-01T00:00:00+00:00": "0001-01-01T00:00:00+00:00",
        "0001-01-01T00:00:00Z": "0001-01-01T00:00:00+00:00",
        "0001-01-01T00:00:00.000001+00:00": "0001-01-01T00:00:00.000001+00:00",
        "0001-01-01T00:01:00+00:01": "0001-01-01T00:00:00+00:00",
        "0001-01-01T00:00:00-00:01": "0001-01-01T00:01:00+00:00",
        "0001-01-01T23:59:59-23:59": "0001-01-02T23:58:59+00:00",
        "9999-12-31T23:59:59+00:00": "9999-12-31T23:59:59+00:00",
        "9999-12-31T23:59:59.999999Z": "9999-12-31T23:59:59.999999+00:00",
        "9999-12-31T23:58:59-00:01": "9999-12-31T23:59:59+00:00",
        "9999-12-31T23:59:59+00:01": "9999-12-31T23:58:59+00:00",
        "9999-12-31T23:59:59+23:59": "9999-12-31T00:00:59+00:00",
        "2026-12-31T20:00:00-08:00": "2027-01-01T04:00:00+00:00",
        "0001-01-01": "0001-01-01",
        "9999-12-31": "9999-12-31",
    }

    def test_since_utc_underflow_and_overflow_are_fixed_value_errors(self):
        for value in self.UNDERFLOW + self.OVERFLOW:
            with self.subTest(value=value):
                for call in (lambda: ledger_list._since(value), lambda: ledger_list.list_receipts(self.root, since=value)):
                    with self.assertRaises(ValueError) as caught:
                        call()
                    self.assertEqual(str(caught.exception), ledger_list.SINCE_ERROR)
                    self.assertIsNone(caught.exception.__cause__)
                    self.assertTrue(caught.exception.__suppress_context__)

    def test_since_representable_utc_endpoints_and_date_only_remain_accepted(self):
        for value, expected in self.ACCEPTED.items():
            with self.subTest(value=value):
                self.assertEqual(ledger_list._since(value), expected)
                self.assertEqual(ledger_list.list_receipts(self.root, since=value), {"receipts": [], "count": 0})

    def assertOverflowStopped(self, value, *extra):
        result = cli("status", "--directory", str(self.root), "--list", "--since", value, *extra)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr.strip(), "Stopped: " + ledger_list.SINCE_ERROR)
        for text in ("Traceback", "OverflowError", "out of range", value):
            self.assertNotIn(text, result.stderr)

    def snapshot_tree(self):
        tree = {}
        for path in sorted(self.root.rglob("*")):
            info = path.lstat()
            tree[str(path.relative_to(self.root))] = (info.st_mode, info.st_mtime_ns,
                                                      path.read_bytes() if path.is_file() else None)
        return tree

    def test_since_endpoint_cli_refusal_is_controlled_with_and_without_ledger_and_never_mutates(self):
        for populated in (False, True):
            if populated:
                self.populate()
            before = self.snapshot_tree()
            for value in ("0001-01-01T00:00:00+00:01", "9999-12-31T23:59:59-00:01"):
                with self.subTest(populated=populated, value=value):
                    self.assertOverflowStopped(value)
                    self.assertOverflowStopped(value, "--source-id", "source-one", "--not-in-sources")
            self.assertEqual(self.snapshot_tree(), before)
            for suffix in ("-wal", "-journal", "-shm"):
                self.assertFalse(os.path.lexists(str(self.ledger) + suffix), suffix)
        # the adjacent representable endpoints stay successful through the real CLI
        before = self.snapshot_tree()
        self.assertEqual(self.listing("--since", "0001-01-01T00:01:00+00:01")["count"], 3)
        self.assertEqual(self.listing("--since", "9999-12-31T23:58:59-00:01")["count"], 0)
        self.assertEqual(self.snapshot_tree(), before)

    def test_since_refusal_precedes_source_library_and_snapshot_reads(self):
        for variant in ("absent", "malformed", "sidecar"):
            with self.subTest(variant=variant):
                if variant != "absent":
                    self.ledger.parent.mkdir(exist_ok=True)
                    self.ledger.write_bytes(b"not a synthetic sqlite database")
                if variant == "sidecar":
                    Path(str(self.ledger) + "-wal").write_bytes(b"")
                with mock.patch.object(ledger_list, "_registered") as registered, \
                        mock.patch.object(ledger_list, "_snapshot") as snapshot:
                    with self.assertRaises(ValueError) as caught:
                        ledger_list.list_receipts(self.root, "source-one", "9999-12-31T23:59:59-00:01", True)
                self.assertEqual(str(caught.exception), ledger_list.SINCE_ERROR)
                registered.assert_not_called()
                snapshot.assert_not_called()
        # a bad sources.json is only reached after a valid since
        self.sources.write_text("{", encoding="utf-8")
        self.assertOverflowStopped("0001-01-01T00:00:00+00:01", "--not-in-sources")
        self.assertStopped("--list", "--since", "0001-01-01T00:00:00+00:00", "--not-in-sources",
                           message="sources.json: not valid UTF-8 JSON")

    def test_since_mixed_mode_and_source_id_precedence_is_unchanged(self):
        value = "0001-01-01T00:00:00+00:01"
        self.assertStopped("--since", value, message="--source-id/--since/--not-in-sources need --list")
        for flag in ("--check", "--fix-modes"):
            self.assertStopped("--list", flag, "--since", value, message="--list is read-only; run --check separately")
        self.assertStopped("--list", "--source-id", "", "--since", value, message="--source-id: use 1-200 characters")
        self.assertStopped("--list", "--source-id", "x" * 201, "--since", value,
                           message="--source-id: use 1-200 characters")

    def test_since_offsets_filter_inclusively_without_wall_clock_dependency(self):
        self.populate()
        fixed = ["2026-12-31T23:59:59.999999+00:00", "2027-01-01T00:00:00+00:00", "2027-01-01T00:00:00.000001+00:00"]
        with sqlite3.connect(self.ledger) as db:
            for name, stamp in zip(("a.txt", "b.txt", "a-again.txt"), fixed):
                db.execute("UPDATE receipts SET received_at = ? WHERE original_name = ?", (stamp, name))
        db.close()
        names = lambda *extra: [r["original_name"] for r in self.listing(*extra)["receipts"]]
        for spelling in ("2027-01-01T00:00:00+00:00", "2027-01-01T00:00:00Z", "2026-12-31T16:00:00-08:00",
                         "2027-01-01T09:00:00+09:00"):
            with self.subTest(spelling=spelling):
                self.assertEqual(names("--since", spelling), ["b.txt", "a-again.txt"])
        self.assertEqual(names("--since", "2027-01-01T00:00:00.000001+00:00"), ["a-again.txt"])
        self.assertEqual(names("--since", "2027-01-01T00:00:00.000002+00:00"), [])
        self.assertEqual(names("--since", "2026-12-31T23:59:59.999999Z"), ["a.txt", "b.txt", "a-again.txt"])
        self.assertEqual(names("--since", "2027-01-01T00:00:00Z", "--source-id", "source-one"), ["b.txt"])
        self.assertEqual(names("--since", "0001-01-01T00:01:00+00:01"), ["a.txt", "b.txt", "a-again.txt"])
        self.assertEqual(self.listing("--since", "9999-12-31T23:58:59-00:01"), {"receipts": [], "count": 0})


if __name__ == "__main__":
    unittest.main()
