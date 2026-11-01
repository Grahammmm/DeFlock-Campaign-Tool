"""RF2010-2: `ingest --dir DIR` ingests one local production folder per run.

Synthetic bytes in temporary directories only. Run from the repository root:
python3 -B -m unittest tests.test_ingest_dir -v
"""

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = Path(__file__).resolve().parents[1]
MAX_BYTES = 50 * 1024 * 1024


class IngestDirTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-ingest-dir-")
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

    def ingest_dir(self, source_id="synthetic-production-1", folder=None):
        result = self.cli("ingest", "--dir", folder or self.folder, "--source-id", source_id)
        self.assertEqual(result.stderr, "")
        return json.loads(result.stdout)

    def write(self, relative, data):
        path = self.folder / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def ledger(self):
        path = self.root / "private" / "ledger.sqlite"
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        try:
            return (sorted(r[0] for r in db.execute("SELECT sha256 FROM objects")),
                    sorted(db.execute("SELECT source_id, object_sha256 FROM receipts")))
        finally:
            db.close()

    def stored(self, data):
        return (self.root / "private" / "objects" / hashlib.sha256(data).hexdigest()).exists()

    def outcomes(self, report):
        return [(f["path"], f["outcome"]) for f in report["files"]]

    def test_folder_with_subfolder_stores_each_file_sorted(self):
        files = {"b.txt": b"Synthetic b\n", "sub/a.txt": b"Synthetic sub a\n", "a.txt": b"Synthetic a\n"}
        for relative, data in files.items():
            self.write(relative, data)
        report = self.ingest_dir()
        self.assertEqual(report["dir"], "production")
        self.assertEqual(self.outcomes(report), [("a.txt", "stored"), ("b.txt", "stored"), ("sub/a.txt", "stored")])
        for entry in report["files"]:
            self.assertEqual(entry["sha256"], hashlib.sha256(files[entry["path"]]).hexdigest()[:12])
            self.assertTrue(entry["new_receipt"])
        self.assertEqual(report["counts"], {"stored": 3, "present": 0, "symlink": 0, "not_regular": 0,
                                            "too_large": 0, "hidden": 0})
        objects, receipts = self.ledger()
        self.assertEqual(len(objects), 3)
        self.assertEqual(len(receipts), 3)
        self.assertEqual({r[0] for r in receipts}, {"synthetic-production-1"})
        for data in files.values():
            self.assertTrue(self.stored(data))

    def test_rerun_same_source_is_present_without_new_receipts(self):
        self.write("a.txt", b"Synthetic a\n")
        self.write("sub/b.txt", b"Synthetic b\n")
        self.ingest_dir()
        before = self.ledger()
        again = self.ingest_dir()
        self.assertEqual(self.outcomes(again), [("a.txt", "present"), ("sub/b.txt", "present")])
        self.assertFalse(any(f["new_receipt"] for f in again["files"]))
        self.assertEqual(again["counts"]["present"], 2)
        self.assertEqual(self.ledger(), before)
        other = self.ingest_dir("synthetic-production-2")
        self.assertEqual(self.outcomes(other), [("a.txt", "present"), ("sub/b.txt", "present")])
        self.assertTrue(all(f["new_receipt"] for f in other["files"]))
        self.assertEqual(len(self.ledger()[0]), 2)
        self.assertEqual(len(self.ledger()[1]), 4)

    def test_identical_bytes_store_one_object_then_present(self):
        self.write("one.txt", b"Synthetic duplicate\n")
        self.write("two.txt", b"Synthetic duplicate\n")
        report = self.ingest_dir()
        self.assertEqual(self.outcomes(report), [("one.txt", "stored"), ("two.txt", "present")])
        self.assertEqual(report["files"][0]["sha256"], report["files"][1]["sha256"])
        self.assertEqual([f["new_receipt"] for f in report["files"]], [True, False])
        objects, receipts = self.ledger()
        self.assertEqual((len(objects), len(receipts)), (1, 1))

    def test_symlinks_are_reported_and_never_read(self):
        locked = self.base / "locked"
        locked.mkdir()
        (locked / "secret.txt").write_bytes(b"Synthetic outside file\n")
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "inner.txt").write_bytes(b"Synthetic outside folder file\n")
        locked.chmod(0o000)
        self.addCleanup(locked.chmod, 0o700)
        (self.folder / "link.txt").symlink_to(locked / "secret.txt")
        (self.folder / "linked-folder").symlink_to(outside, target_is_directory=True)
        (self.folder / "dangling.txt").symlink_to(self.base / "absent")
        self.write("real.txt", b"Synthetic real\n")
        report = self.ingest_dir()
        self.assertEqual(self.outcomes(report), [("dangling.txt", "symlink"), ("link.txt", "symlink"),
                                                 ("linked-folder", "symlink"), ("real.txt", "stored")])
        self.assertEqual([f["sha256"] for f in report["files"][:3]], [None, None, None])
        self.assertEqual(report["counts"]["symlink"], 3)
        self.assertFalse(self.stored(b"Synthetic outside file\n"))
        self.assertFalse(self.stored(b"Synthetic outside folder file\n"))
        self.assertEqual(len(self.ledger()[0]), 1)

    def test_fifo_is_not_regular_and_hidden_entries_are_skipped(self):
        os.mkfifo(self.folder / "pipe")
        self.write(".DS_Store", b"Synthetic finder data\n")
        self.write(".hidden/inner.txt", b"Synthetic hidden inner\n")
        self.write("sub/.secret.txt", b"Synthetic nested hidden\n")
        self.write("visible.txt", b"Synthetic visible\n")
        report = self.ingest_dir()
        self.assertEqual(self.outcomes(report), [(".DS_Store", "hidden"), (".hidden", "hidden"), ("pipe", "not_regular"),
                                                 ("sub/.secret.txt", "hidden"), ("visible.txt", "stored")])
        self.assertEqual(report["counts"]["hidden"], 3)
        self.assertEqual(report["counts"]["not_regular"], 1)
        for data in (b"Synthetic finder data\n", b"Synthetic hidden inner\n", b"Synthetic nested hidden\n"):
            self.assertFalse(self.stored(data))
        self.assertEqual(len(self.ledger()[0]), 1)

    def test_oversized_sparse_file_is_skipped_and_others_ingested(self):
        big = self.folder / "big.pdf"
        with big.open("wb") as handle:
            handle.truncate(MAX_BYTES + 1)
        self.write("small.txt", b"Synthetic small\n")
        report = self.ingest_dir()
        self.assertEqual(self.outcomes(report), [("big.pdf", "too_large"), ("small.txt", "stored")])
        self.assertIsNone(report["files"][0]["sha256"])
        self.assertFalse(report["files"][0]["new_receipt"])
        self.assertEqual(len(self.ledger()[0]), 1)
        self.assertEqual(len(list((self.root / "private" / "objects").iterdir())), 1)

    def test_oversized_single_file_still_refused_before_ledger(self):
        big = self.base / "big.pdf"
        with big.open("wb") as handle:
            handle.truncate(MAX_BYTES + 1)
        result = self.cli("ingest", "--file", big, "--source-id", "synthetic-big", returncode=1)
        self.assertIn("Alpha limit: 50 MiB per file", result.stderr)
        self.assertFalse((self.root / "private" / "ledger.sqlite").exists())

    def test_more_than_2000_files_refused_before_any_write(self):
        for index in range(2001):
            (self.folder / f"empty-{index:04d}.txt").write_bytes(b"")
        result = self.cli("ingest", "--dir", self.folder, "--source-id", "synthetic-big-folder", returncode=1)
        self.assertIn("more than 2000 files; split the folder", result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertFalse((self.root / "private" / "ledger.sqlite").exists())
        self.assertFalse((self.root / "private" / "objects").exists())
        (self.folder / "empty-2000.txt").unlink()
        report = self.ingest_dir()
        self.assertEqual(report["counts"]["stored"], 1)
        self.assertEqual(report["counts"]["present"], 1999)

    def test_dir_must_be_real_directory_and_file_dir_are_exclusive(self):
        self.write("a.txt", b"Synthetic a\n")
        link = self.base / "production-link"
        link.symlink_to(self.folder, target_is_directory=True)
        for target in (link, self.folder / "a.txt", self.base / "missing"):
            with self.subTest(target=target.name):
                result = self.cli("ingest", "--dir", target, "--source-id", "synthetic-bad", returncode=1)
                self.assertIn("Stopped: --dir must be a real directory", result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertFalse((self.root / "private" / "ledger.sqlite").exists())
        both = self.cli("ingest", "--file", self.folder / "a.txt", "--dir", self.folder,
                        "--source-id", "synthetic-bad", returncode=2)
        self.assertIn("not allowed with argument", both.stderr)
        neither = self.cli("ingest", "--source-id", "synthetic-bad", returncode=2)
        self.assertIn("one of the arguments --file --dir is required", neither.stderr)
        self.assertFalse((self.root / "private" / "ledger.sqlite").exists())

    def test_file_output_is_unchanged(self):
        data = b"Synthetic policy only.\n"
        source = self.write("policy.txt", data)
        digest = hashlib.sha256(data).hexdigest()
        receipt = hashlib.sha256(json.dumps(["synthetic-delivery-a", digest], separators=(",", ":")).encode()).hexdigest()
        for new_receipt in (True, False):
            result = self.cli("ingest", "--file", source, "--source-id", "synthetic-delivery-a")
            expected = {"sha256": digest, "receipt_id": receipt, "new_receipt": new_receipt,
                        "analysis_status": "not_started"}
            self.assertEqual(result.stdout, json.dumps(expected, indent=2) + "\n")
            self.assertEqual(result.stderr, "")

    def test_integrity_mismatch_stops_run_naming_path_and_keeps_earlier_files(self):
        self.write("a.txt", b"Synthetic a\n")
        b_file = self.write("b.txt", b"Synthetic b\n")
        self.write("c.txt", b"Synthetic c\n")
        self.cli("ingest", "--file", b_file, "--source-id", "synthetic-earlier")
        corrupt = self.root / "private" / "objects" / hashlib.sha256(b"Synthetic b\n").hexdigest()
        corrupt.write_bytes(b"Synthetic deliberate corruption\n")
        result = self.cli("ingest", "--dir", self.folder, "--source-id", "synthetic-production-1", returncode=1)
        self.assertIn("Stopped: Stored object integrity mismatch; intake stopped: b.txt", result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(corrupt.read_bytes(), b"Synthetic deliberate corruption\n")
        self.assertTrue(self.stored(b"Synthetic a\n"))
        self.assertFalse(self.stored(b"Synthetic c\n"))
        objects, receipts = self.ledger()
        self.assertEqual(len(objects), 2)
        self.assertEqual(sorted(r[0] for r in receipts), ["synthetic-earlier", "synthetic-production-1"])


if __name__ == "__main__":
    unittest.main()
