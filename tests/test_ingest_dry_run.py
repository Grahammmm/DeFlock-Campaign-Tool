"""RF1500-4: `ingest --dry-run` previews a --file or --dir run and writes nothing.

Synthetic bytes in temporary directories only. Run from the repository root:
python3 -B -m unittest tests.test_ingest_dry_run -v
"""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sqlite3
import sys
import tempfile
import unittest

REPO_ROOT = Path(__file__).resolve().parents[1]
MAX_BYTES = 50 * 1024 * 1024


class IngestDryRunTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-ingest-dry-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "campaign"
        self.folder = self.base / "production"
        self.folder.mkdir()
        self.cli("init", "--name", "Synthetic Campaign", "--county", "Synthetic County", "--state", "ca")

    def cli(self, command, *arguments, returncode=0):
        result = subprocess.run(
            [sys.executable, "-B", "-m", "campaign_tool", command, "--directory", str(self.root), *map(str, arguments)],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, returncode, f"stdout={result.stdout!r}; stderr={result.stderr!r}")
        self.assertNotIn("Traceback", result.stderr)
        return result

    def snapshot(self):
        """Every path under the campaign: type, size, mtime and content hash (link target for links)."""
        tree = {}
        for folder, dirs, names in os.walk(self.root):
            for name in dirs + names:
                path = Path(folder) / name
                info = path.lstat()
                content = (os.readlink(path) if path.is_symlink() else
                           hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None)
                tree[str(path.relative_to(self.root))] = (info.st_mode, info.st_size, info.st_mtime_ns, content)
        return tree

    def dry(self, *arguments, returncode=0):
        """Run one dry run and assert the campaign tree is byte-identical afterwards."""
        before = self.snapshot()
        result = self.cli("ingest", *arguments, "--dry-run", returncode=returncode)
        self.assertEqual(self.snapshot(), before, "dry run changed the campaign tree")
        return result

    def dry_file(self, path, source_id):
        result = self.dry("--file", path, "--source-id", source_id)
        body, last = result.stdout.rstrip("\n").rsplit("\n", 1)
        self.assertEqual(last, "Dry run: nothing was written.")
        return json.loads(body)

    def write(self, relative, data):
        path = self.folder / relative
        path.write_bytes(data)
        return path

    def test_fresh_campaign_without_private_would_store_and_creates_nothing(self):
        (self.root / "private").rmdir()
        data = b"Synthetic fresh\n"
        report = self.dry_file(self.write("a.txt", data), "synthetic-1")
        digest = hashlib.sha256(data).hexdigest()
        self.assertEqual(report, {
            "sha256": digest,
            "receipt_id": hashlib.sha256(json.dumps(["synthetic-1", digest], separators=(",", ":")).encode()).hexdigest(),
            "new_receipt": True, "analysis_status": "not_started", "outcome": "would_store", "dry_run": True})
        self.assertFalse((self.root / "private").exists())

    def test_file_after_real_ingest_is_present_and_receipt_follows_source_id(self):
        source = self.write("a.txt", b"Synthetic stored\n")
        real = json.loads(self.cli("ingest", "--file", source, "--source-id", "synthetic-1").stdout)
        same = self.dry_file(source, "synthetic-1")
        self.assertEqual((same["outcome"], same["new_receipt"], same["receipt_id"]),
                         ("present", False, real["receipt_id"]))
        other = self.dry_file(source, "synthetic-2")
        self.assertEqual((other["outcome"], other["new_receipt"]), ("present", True))
        self.assertNotEqual(other["receipt_id"], real["receipt_id"])

    def test_dir_dry_run_reports_each_outcome_then_real_run_matches(self):
        old = self.write("old.txt", b"Synthetic already stored\n")
        self.cli("ingest", "--file", old, "--source-id", "synthetic-1")
        self.write("dup-a.txt", b"Synthetic twin\n")
        self.write("dup-b.txt", b"Synthetic twin\n")
        self.write("new.txt", b"Synthetic new\n")
        self.write(".hidden", b"Synthetic hidden\n")
        (self.folder / "link.txt").symlink_to(old)
        os.mkfifo(self.folder / "pipe")
        with (self.folder / "big.pdf").open("wb") as handle:
            handle.truncate(MAX_BYTES + 1)
        preview = json.loads(self.dry("--dir", self.folder, "--source-id", "synthetic-1").stdout)
        expected = [(".hidden", "hidden"), ("big.pdf", "too_large"), ("dup-a.txt", "would_store"),
                    ("dup-b.txt", "present"), ("link.txt", "symlink"), ("new.txt", "would_store"),
                    ("old.txt", "present"), ("pipe", "not_regular")]
        self.assertEqual([(f["path"], f["outcome"]) for f in preview["files"]], expected)
        self.assertEqual(preview["counts"], {"would_store": 2, "present": 2, "symlink": 1,
                                             "not_regular": 1, "too_large": 1, "hidden": 1})
        self.assertIs(preview["dry_run"], True)
        self.assertEqual(preview["dir"], "production")
        twin = hashlib.sha256(b"Synthetic twin\n").hexdigest()[:12]
        self.assertEqual([f["sha256"] for f in preview["files"]][2:4], [twin, twin])
        self.assertEqual([f["new_receipt"] for f in preview["files"]],
                         [False, False, True, False, False, True, False, False])
        real = json.loads(self.cli("ingest", "--dir", self.folder, "--source-id", "synthetic-1").stdout)
        self.assertNotIn("dry_run", real)
        renamed = [{**f, "outcome": "stored" if f["outcome"] == "would_store" else f["outcome"]}
                   for f in preview["files"]]
        self.assertEqual(real["files"], renamed)
        self.assertEqual(real["counts"]["stored"], preview["counts"]["would_store"])
        again = json.loads(self.dry("--dir", self.folder, "--source-id", "synthetic-1").stdout)
        self.assertEqual(again["counts"]["would_store"], 0)
        self.assertEqual(again["counts"]["present"], 4)

    def test_unreadable_ledger_stops_and_writes_nothing(self):
        source = self.write("a.txt", b"Synthetic a\n")
        self.cli("ingest", "--file", source, "--source-id", "synthetic-1")
        ledger = self.root / "private" / "ledger.sqlite"
        copy = self.base / "ledger-copy.sqlite"
        copy.write_bytes(ledger.read_bytes())
        ledger.unlink()
        ledger.symlink_to(copy)
        for replacement in ("symlink", "not sqlite"):
            if replacement == "not sqlite":
                ledger.unlink()
                ledger.write_bytes(b"Synthetic text, not a database\n" * 64)
            for arguments in (("--file", source), ("--dir", self.folder)):
                with self.subTest(replacement=replacement, mode=arguments[0]):
                    result = self.dry(*arguments, "--source-id", "synthetic-1", returncode=1)
                    self.assertEqual(result.stdout, "")
                    self.assertIn("Stopped: ledger is not readable; nothing was written", result.stderr)

    def test_wal_mode_ledger_is_read_without_creating_sidecars(self):
        source = self.write("a.txt", b"Synthetic wal\n")
        real = json.loads(self.cli("ingest", "--file", source, "--source-id", "synthetic-1").stdout)
        private = self.root / "private"
        ledger = private / "ledger.sqlite"
        db = sqlite3.connect(ledger)
        self.assertEqual(db.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
        db.close()
        self.assertEqual(ledger.read_bytes()[18:20], b"\2\2")
        self.assertEqual(sorted(p.name for p in private.iterdir()), ["ledger.sqlite", "objects"])
        same = self.dry_file(source, "synthetic-1")  # dry() asserts the tree is unchanged
        self.assertEqual((same["outcome"], same["new_receipt"], same["receipt_id"]),
                         ("present", False, real["receipt_id"]))
        preview = json.loads(self.dry("--dir", self.folder, "--source-id", "synthetic-1").stdout)
        self.assertEqual(preview["counts"]["present"], 1)
        self.assertEqual(sorted(p.name for p in private.iterdir()), ["ledger.sqlite", "objects"])

    def test_ledger_sidecar_refused_without_writing(self):
        source = self.write("a.txt", b"Synthetic sidecar\n")
        self.cli("ingest", "--file", source, "--source-id", "synthetic-1")
        for suffix in ("journal", "wal", "shm"):
            sidecar = self.root / "private" / f"ledger.sqlite-{suffix}"
            sidecar.write_bytes(b"")
            for arguments in (("--file", source), ("--dir", self.folder)):
                with self.subTest(sidecar=suffix, mode=arguments[0]):
                    result = self.dry(*arguments, "--source-id", "synthetic-1", returncode=1)
                    self.assertEqual(result.stdout, "")
                    self.assertIn("Stopped: ledger has a journal or WAL file beside it; nothing was written",
                                  result.stderr)
            sidecar.unlink()

    def test_more_than_max_dir_files_refused_like_a_real_run(self):
        (self.root / "private").rmdir()
        for index in range(2001):
            (self.folder / f"empty-{index:04d}.txt").write_bytes(b"")
        result = self.dry("--dir", self.folder, "--source-id", "synthetic-big", returncode=1)
        self.assertIn("more than 2000 files; split the folder", result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertFalse((self.root / "private").exists())

    def test_dry_run_keeps_input_refusals(self):
        target = self.write("a.txt", b"Synthetic a\n")
        link = self.base / "link.txt"
        link.symlink_to(target)
        result = self.dry("--file", link, "--source-id", "synthetic-1", returncode=1)
        self.assertIn("Stopped: Input must be a regular, non-symlink file", result.stderr)
        big = self.base / "big.pdf"
        with big.open("wb") as handle:
            handle.truncate(MAX_BYTES + 1)
        result = self.dry("--file", big, "--source-id", "synthetic-1", returncode=1)
        self.assertIn("Alpha limit: 50 MiB per file", result.stderr)


if __name__ == "__main__":
    unittest.main()
