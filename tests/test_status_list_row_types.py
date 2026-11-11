"""`status --list` refuses malformed receipt scalar storage types (synthetic campaign only)."""
import hashlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import ledger_list

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
FILES = {"a.txt": b"synthetic RF0540-1 record A\n", "b.txt": b"synthetic RF0540-1 record B\n"}
SHA_A = hashlib.sha256(FILES["a.txt"]).hexdigest()
FIXED = "private/ledger.sqlite is not a readable ledger"


def cli(*args):
    return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *args], cwd=REPO_ROOT,
                          capture_output=True, text=True, timeout=120)


class RowTypeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-row-types-")
        cls.addClassCleanup(temporary.cleanup)
        cls.tmp = Path(temporary.name)
        cls.base = cls.tmp / "base"
        shutil.copytree(EXAMPLE, cls.base, ignore=shutil.ignore_patterns("public"))
        for name, data in FILES.items():
            (cls.tmp / name).write_bytes(data)
            result = cli("ingest", "--directory", str(cls.base), "--file", str(cls.tmp / name),
                         "--source-id", "source-one")
            assert result.returncode == 0, result.stderr

    def setUp(self):
        self.root = self.tmp / ("c-" + self.id().rsplit(".", 1)[-1])
        shutil.copytree(self.base, self.root)
        self.ledger = self.root / "private" / "ledger.sqlite"

    def mutate(self, *statements):
        db = sqlite3.connect(self.ledger)
        for sql, params in statements:
            db.execute(sql, params)
        db.commit()
        db.close()

    def test_healthy_listing_is_unchanged(self):
        report = ledger_list.list_receipts(self.root)
        self.assertEqual(report["count"], 2)
        self.assertEqual({r["sha256_prefix"] for r in report["receipts"]},
                         {hashlib.sha256(d).hexdigest()[:12] for d in FILES.values()})

    def test_bad_scalar_types_refuse_the_whole_listing(self):
        cases = {
            "blob_sha": [("UPDATE objects SET sha256 = ? WHERE sha256 = ?", (b"\x00\xff", SHA_A)),
                         ("UPDATE receipts SET object_sha256 = ? WHERE object_sha256 = ?", (b"\x00\xff", SHA_A))],
            "blob_size": [("UPDATE objects SET byte_count = ? WHERE sha256 = ?", (b"\x01", SHA_A))],
            "real_size": [("UPDATE objects SET byte_count = ? WHERE sha256 = ?", (1.5, SHA_A))],
            "negative_size": [("UPDATE objects SET byte_count = ? WHERE sha256 = ?", (-1, SHA_A))],
            "blob_source": [("UPDATE receipts SET source_id = ? WHERE object_sha256 = ?", (b"\x02", SHA_A))],
            "blob_received": [("UPDATE receipts SET received_at = ? WHERE object_sha256 = ?", (b"\x03", SHA_A))],
        }
        for label, statements in cases.items():
            with self.subTest(label):
                shutil.copyfile(self.base / "private" / "ledger.sqlite", self.ledger)
                self.mutate(*statements)
                before = self.ledger.read_bytes()
                with self.assertRaises(ValueError) as caught:
                    ledger_list.list_receipts(self.root)
                self.assertEqual(str(caught.exception), FIXED)
                self.assertIsNone(caught.exception.__cause__)
                result = cli("status", "--directory", str(self.root), "--list")
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertIn("Stopped: " + FIXED, result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertEqual(self.ledger.read_bytes(), before)

    def test_bad_row_refuses_even_when_registered_or_over_cap(self):
        self.mutate(("UPDATE receipts SET received_at = ? WHERE object_sha256 = ?", (b"\x03", SHA_A)))
        with mock.patch.object(ledger_list, "_registered", return_value={SHA_A}):
            with self.assertRaises(ValueError):
                ledger_list.list_receipts(self.root, not_in_sources=True)
        with mock.patch.object(ledger_list, "MAX_ROWS", 1):
            with self.assertRaises(ValueError):
                ledger_list.list_receipts(self.root)

    def test_rows_excluded_by_sql_are_not_validated(self):
        self.mutate(("UPDATE receipts SET source_id = ? WHERE object_sha256 = ?", (b"\x02", SHA_A)),
                    ("UPDATE objects SET byte_count = ? WHERE sha256 = ?", (-1, SHA_A)))
        self.mutate(("UPDATE receipts SET source_id = ? WHERE object_sha256 != ?", ("source-two", SHA_A)))
        report = ledger_list.list_receipts(self.root, source_id="source-two")
        self.assertEqual(report["count"], 1)


if __name__ == "__main__":
    unittest.main()
