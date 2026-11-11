"""Synthetic tests: import-existing refuses a non-directory/linked private root."""
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from campaign_tool import cli, import_existing as importer

MSG = "private/ must be a real directory, not a symlink or file; nothing was imported"


class PrivateRootTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="synthetic-privroot-")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.root, self.folder = self.base / "campaign", self.base / "records"
        self.outside = self.base / "outside"
        for d in (self.root, self.folder, self.outside):
            d.mkdir()
        self.outside.chmod(0o755)
        (self.outside / "sentinel.txt").write_text("keep")
        (self.root / "campaign.json").write_text(json.dumps(
            {"schema_version": 1, "name": "Synthetic", "county": "Synthetic", "state": "CA"}))
        self.instant = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def populate(self):
        (self.folder / "a.txt").write_text("synthetic a")

    def run_import(self):
        return importer.run(self.root, self.folder, "l", now=self.instant)

    def assert_refused(self, populated=True):
        if populated:
            self.populate()
        before = (sorted(os.listdir(self.outside)), self.outside.stat().st_mode,
                  self.outside.stat().st_mtime_ns)
        with patch.object(importer, "_entries") as entries, \
                patch.object(cli, "database") as database, \
                patch.object(importer, "_write_receipt") as receipt:
            with self.assertRaises(ValueError) as caught:
                self.run_import()
        self.assertEqual(str(caught.exception), MSG)
        for tripwire in (entries, database, receipt):
            tripwire.assert_not_called()
        self.assertEqual(before, (sorted(os.listdir(self.outside)), self.outside.stat().st_mode,
                                  self.outside.stat().st_mtime_ns))

    def test_directory_symlink_refused_api_and_cli(self):
        (self.root / "private").symlink_to(self.outside)
        self.assert_refused()
        result = subprocess.run(
            [sys.executable, "-B", "-m", "campaign_tool", "import-existing", "--directory",
             str(self.root), "--from", str(self.folder), "--label", "l"],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn(MSG, result.stderr)
        self.assertNotIn(str(self.outside), result.stderr)
        self.assertEqual(sorted(os.listdir(self.outside)), ["sentinel.txt"])
        self.assertEqual(self.outside.stat().st_mode & 0o777, 0o755)

    def test_empty_source_still_refuses_linked_private_root(self):
        (self.root / "private").symlink_to(self.outside)
        self.assert_refused(populated=False)

    def test_dangling_file_and_multihop_links_refused(self):
        private = self.root / "private"
        private.symlink_to(self.base / "missing")
        self.assert_refused()
        self.assertFalse((self.base / "missing").exists())
        private.unlink()
        (self.base / "f").write_text("x")
        private.symlink_to(self.base / "f")
        self.assert_refused()
        private.unlink()
        hop = self.base / "hop"
        hop.symlink_to(self.outside)
        private.symlink_to(hop)
        self.assert_refused()

    def test_regular_file_fifo_and_socket_refused(self):
        private = self.root / "private"
        private.write_text("not a dir")
        self.assert_refused()
        self.assertEqual(private.read_text(), "not a dir")
        private.unlink()
        os.mkfifo(private)
        self.assert_refused()
        private.unlink()
        sock = socket.socket(socket.AF_UNIX)
        self.addCleanup(sock.close)
        try:
            sock.bind(str(private))
        except OSError:
            self.skipTest("unix sockets unavailable or path too long")
        self.assert_refused()

    def test_missing_and_real_private_still_import(self):
        self.populate()
        self.assertEqual(self.run_import()["counts"]["stored"], 1)
        self.assertTrue((self.root / "private").is_dir())
        self.assertEqual(importer.run(self.root, self.folder, "l", now=self.instant)["counts"]["error"], 0)
        empty = self.base / "c2"
        empty.mkdir()
        (empty / "campaign.json").write_text((self.root / "campaign.json").read_text())
        (empty / "private").mkdir()
        self.assertEqual(importer.run(empty, self.folder, "l", now=self.instant)["counts"]["stored"], 1)

    def test_lstat_error_is_not_treated_as_missing(self):
        self.populate()
        real = os.lstat

        def fake(path, *a, **k):
            if str(path).endswith("private"):
                raise PermissionError("denied")
            return real(path, *a, **k)

        with patch.object(importer.os, "lstat", fake), \
                patch.object(cli, "database") as database:
            with self.assertRaises(PermissionError):
                self.run_import()
        database.assert_not_called()

    def test_invalid_label_precedes_private_check(self):
        (self.root / "private").symlink_to(self.outside)
        with self.assertRaisesRegex(ValueError, "Label must be"):
            importer.run(self.root, self.folder, "Bad Label", now=self.instant)

    def test_manual_fix_allows_retry(self):
        self.populate()
        private = self.root / "private"
        private.symlink_to(self.outside)
        with self.assertRaises(ValueError):
            self.run_import()
        private.unlink()
        self.assertEqual(self.run_import()["counts"]["stored"], 1)
        self.assertEqual(sorted(os.listdir(self.outside)), ["sentinel.txt"])

    def test_doc_paragraph(self):
        text = (Path(__file__).resolve().parent.parent / "docs" / "RECORDS-INTAKE.md").read_text()
        self.assertIn("refuses before import", text)
        self.assertIn("not a guarantee against concurrent changes", text)


if __name__ == "__main__":
    unittest.main()
