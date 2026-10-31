"""status --check: private/objects re-hashed against the ledger (RF1230-4).

Synthetic bytes in a temporary campaign only. Run from the repository root:
python3 -m unittest tests.test_store_check -v
"""
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import store_check  # noqa: E402

FIRST = b"synthetic first object\n"
SECOND = b"synthetic second object, a bit longer\n"


def sha(data):
    return hashlib.sha256(data).hexdigest()


class StoreCheckTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-store-check-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "campaign"
        self.objects = self.root / "private" / "objects"
        self.cli("init", "--name", "Synthetic Campaign", "--county", "Synthetic", "--state", "CA")

    def cli(self, command, *arguments, code=0):
        result = subprocess.run(
            [sys.executable, "-B", "-m", "campaign_tool", command, "--directory", str(self.root),
             *map(str, arguments)], cwd=REPO_ROOT, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        return result

    def ingest(self, data, name, source="synthetic-source"):
        path = self.base / name
        path.write_bytes(data)
        self.cli("ingest", "--file", path, "--source-id", source)

    def ingest_two(self):
        self.ingest(FIRST, "first.txt")
        self.ingest(SECOND, "second.txt")

    def check(self, code):
        result = self.cli("status", "--check", code=code)
        decoder = json.JSONDecoder()
        counts, end = decoder.raw_decode(result.stdout)
        block, _ = decoder.raw_decode(result.stdout[end:].lstrip())
        return counts, block["check"], result

    def problems(self, code=3):
        return self.check(code)[1]["problems"]

    def snapshot(self):
        found = {}
        for path in sorted(self.root.rglob("*")):
            info = os.lstat(path)
            found[str(path.relative_to(self.root))] = (info.st_mode, info.st_size, info.st_mtime_ns)
        return found

    def test_clean_store_exit_zero_and_counts_unchanged(self):
        self.ingest_two()
        plain = self.cli("status").stdout
        counts, check, result = self.check(0)
        self.assertEqual(check, {"checked_objects": 2, "problems": []})
        self.assertTrue(result.stdout.startswith(plain))
        self.assertEqual(counts, json.loads(plain))
        self.assertEqual(counts["stored_objects"], 2)

    def test_missing_file(self):
        self.ingest_two()
        (self.objects / sha(FIRST)).unlink()
        self.assertEqual(self.problems(), [{"sha256": sha(FIRST), "problem": "missing_file"}])

    def test_edited_in_place_same_size_is_hash_mismatch(self):
        self.ingest_two()
        target = self.objects / sha(SECOND)
        data = bytearray(target.read_bytes())
        data[0] ^= 1
        target.write_bytes(bytes(data))
        self.assertEqual(self.problems(), [{"sha256": sha(SECOND), "problem": "hash_mismatch"}])

    def test_truncated_is_size_mismatch_without_hashing(self):
        self.ingest_two()
        (self.objects / sha(FIRST)).write_bytes(FIRST[:5])
        hashed = []
        real = store_check._sha256

        def counting(path):
            hashed.append(Path(path).name)
            return real(path)
        with mock.patch.object(store_check, "_sha256", counting):
            result = store_check.check_store(self.root)
        self.assertEqual(result["problems"], [{"sha256": sha(FIRST), "problem": "size_mismatch",
                                               "expected": len(FIRST), "actual": 5}])
        self.assertEqual(hashed, [sha(SECOND)])
        self.assertEqual(self.problems()[0]["problem"], "size_mismatch")

    def test_orphan_temp_and_unexpected_entries(self):
        self.ingest_two()
        orphan = b"synthetic orphan copied in by hand\n"
        (self.objects / sha(orphan)).write_bytes(orphan)
        (self.objects / ".intake-abc").write_bytes(FIRST)
        (self.objects / "notes.txt").write_text("synthetic", encoding="utf-8")
        (self.objects / "subdir").mkdir()
        (self.objects / sha(FIRST).upper()).write_bytes(FIRST)
        expected = sorted([
            {"name": sha(orphan), "problem": "no_ledger_row"},
            {"name": ".intake-abc", "problem": "leftover_temp"},
            {"name": "notes.txt", "problem": "unexpected_entry"},
            {"name": "subdir", "problem": "unexpected_entry"},
            {"name": sha(FIRST).upper(), "problem": "unexpected_entry"},
        ], key=lambda item: item["name"])
        self.assertEqual(self.problems(), expected)

    def test_symlinked_object_is_never_followed(self):
        self.ingest_two()
        outside = self.base / "outside-copy"
        outside.write_bytes(FIRST)
        target = self.objects / sha(FIRST)
        target.unlink()
        target.symlink_to(outside)
        real_open = open

        def guarded(path, *args, **kwargs):
            if Path(path).resolve() == outside.resolve():
                raise AssertionError("symlink target opened")
            return real_open(path, *args, **kwargs)
        with mock.patch("builtins.open", guarded):
            result = store_check.check_store(self.root)
        self.assertEqual(result["problems"], [{"sha256": sha(FIRST), "problem": "not_a_regular_file"}])
        self.assertEqual(self.problems(), result["problems"])

    def test_directory_in_place_of_object_is_not_a_regular_file(self):
        self.ingest_two()
        target = self.objects / sha(SECOND)
        target.unlink()
        target.mkdir()
        self.assertEqual(self.problems(), [{"sha256": sha(SECOND), "problem": "not_a_regular_file"}])

    def test_symlinked_objects_folder_stops(self):
        self.ingest_two()
        moved = self.base / "objects-elsewhere"
        os.rename(self.objects, moved)
        self.objects.symlink_to(moved, target_is_directory=True)
        result = self.cli("status", "--check", code=1)
        self.assertIn("Stopped: private/objects must not be a symlink", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_symlinked_private_folder_stops(self):
        self.ingest_two()
        moved = self.base / "private-elsewhere"
        os.rename(self.root / "private", moved)
        (self.root / "private").symlink_to(moved, target_is_directory=True)
        result = self.cli("status", "--check", code=1)
        self.assertIn("Stopped: private/objects must not be a symlink", result.stderr)

    def test_fresh_campaign_has_no_problems_and_stays_without_ledger(self):
        counts, check, _ = self.check(0)
        self.assertEqual(check, {"checked_objects": 0, "problems": []})
        self.assertEqual(counts["stored_objects"], 0)
        self.assertFalse((self.root / "private" / "ledger.sqlite").exists())
        self.assertFalse(self.objects.exists())

    def test_run_with_problems_changes_nothing(self):
        self.ingest_two()
        (self.objects / sha(FIRST)).write_bytes(b"x")
        (self.objects / ".intake-left").write_bytes(b"partial")
        ledger = self.root / "private" / "ledger.sqlite"
        before, ledger_bytes = self.snapshot(), ledger.read_bytes()
        self.assertEqual(len(self.problems()), 2)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(ledger.read_bytes(), ledger_bytes)

    def test_no_source_id_or_original_name_in_output(self):
        self.ingest(FIRST, "synthetic-name.pdf", source="synthetic-source-xyz")
        (self.objects / ".intake-zz").write_bytes(b"partial")
        (self.objects / sha(FIRST)).write_bytes(FIRST[:3])
        _, _, result = self.check(3)
        for text in ("synthetic-source-xyz", "synthetic-name"):
            self.assertNotIn(text, result.stdout + result.stderr)

    def test_corrupt_ledger_stops(self):
        self.ingest_two()
        (self.root / "private" / "ledger.sqlite").write_bytes(b"synthetic corrupt ledger " * 200)
        result = self.cli("status", "--check", code=1)
        self.assertIn("Stopped: ledger unreadable: ", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_ledger_without_objects_table_is_unreadable(self):
        private = self.root / "private"
        sqlite3.connect(private / "ledger.sqlite").close()
        with self.assertRaisesRegex(ValueError, "^ledger unreadable: OperationalError$"):
            store_check.check_store(self.root)

    def test_invalid_ledger_row_builds_no_path(self):
        self.ingest_two()
        db = sqlite3.connect(self.root / "private" / "ledger.sqlite")
        with db:
            db.execute("INSERT INTO objects VALUES (?, ?, ?)", ("../../campaign.json", 1, "t"))
        db.close()
        with mock.patch.object(store_check.os, "lstat", wraps=os.lstat) as lstat:
            result = store_check.check_store(self.root)
        self.assertEqual(result["problems"],
                         [{"sha256": "../../campaign.json", "problem": "invalid_ledger_row"}])
        self.assertEqual(result["checked_objects"], 3)
        self.assertEqual(lstat.call_count, 2)


if __name__ == "__main__":
    unittest.main()
