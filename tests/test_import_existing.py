"""Synthetic acceptance tests for offline import-existing."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from campaign_tool import cli, import_existing as importer


class ImportExistingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="synthetic-import-")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.root, self.folder = self.base / "campaign", self.base / "records"
        self.root.mkdir()
        self.folder.mkdir()
        (self.root / "campaign.json").write_text(json.dumps(
            {"schema_version": 1, "name": "Synthetic", "county": "Synthetic", "state": "CA"}))
        self.instant = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def write(self, name, data=None):
        path = self.folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if data is not None else ("Synthetic " + name).encode())
        return path

    def run_import(self):
        return importer.run(self.root, self.folder, "l", now=self.instant)

    def receipt(self, summary):
        return json.loads((self.root / summary["receipt"]).read_text())

    def ledger(self):
        with contextlib.closing(sqlite3.connect(self.root / "private/ledger.sqlite")) as db:
            return tuple(db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                         for table in ("objects", "receipts"))

    def run_cli(self, code):
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "import-existing",
                                 "--directory", str(self.root), "--from", str(self.folder), "--label", "l"],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, code, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        return json.loads(result.stdout)

    def test_sources_receipt_kinds_and_idempotent_same_second_rerun(self):
        names = ["City of A/one.pdf", "City of A/two.PDF", "City of A/mail.eml",
                 "county-b/policy.pdf", "notes.txt"]
        for name in names:
            self.write(name)
        first = self.run_import()
        receipt = self.receipt(first)
        self.assertEqual(first["counts"]["stored"], 5)
        self.assertEqual(first["sources"], {"import:l:city-of-a": 3, "import:l:county-b": 1, "import:l:_top": 1})
        self.assertEqual([row["path"] for row in receipt["files"]], sorted(names))
        self.assertEqual([row["kind"] for row in receipt["files"]], ["email", "pdf", "pdf", "pdf", "document"])
        self.assertEqual(receipt["schema"], "import-receipt-v1")
        self.assertEqual(receipt["folder_name"], "records")
        self.assertEqual(receipt["started_at"], self.instant.isoformat())
        self.assertFalse(receipt["stopped"])
        self.assertNotIn("files", first)
        for row in receipt["files"]:
            self.assertEqual(row["sha256_prefix"], hashlib.sha256((self.folder / row["path"]).read_bytes()).hexdigest()[:12])
            self.assertTrue(row["new_receipt"])
        text = (self.root / first["receipt"]).read_text()
        self.assertNotIn(str(self.base), text)
        self.assertNotRegex(text, r"[a-fA-F0-9]{40,}")
        self.assertEqual(stat.S_IMODE((self.root / first["receipt"]).stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.root / "private/imports").stat().st_mode), 0o700)
        second = self.run_import()
        self.assertEqual(second["counts"]["present"], 5)
        self.assertEqual(second["counts"]["stored"], 0)
        self.assertEqual(second["receipt"], "private/imports/20260101T000000Z-l-2.json")
        self.assertFalse(any(row["new_receipt"] for row in self.receipt(second)["files"]))
        self.assertEqual(self.ledger(), (5, 5))
        self.assertEqual(self.receipt(first), receipt)  # Exclusive receipt creation never overwrites.

    def test_same_bytes_different_sources_and_slug_collisions(self):
        for name in ("A B/one.pdf", "a-b/two.pdf", "a-b-2/three.pdf", "!!!/four.pdf"):
            self.write(name, b"same synthetic bytes")
        receipt = self.receipt(self.run_import())
        self.assertEqual({r["path"]: r["source_id"] for r in receipt["files"]},
                         {"!!!/four.pdf": "import:l:_unnamed", "A B/one.pdf": "import:l:a-b",
                          "a-b/two.pdf": "import:l:a-b-2", "a-b-2/three.pdf": "import:l:a-b-2-2"})
        self.assertEqual([r["outcome"] for r in receipt["files"]], ["stored", "present", "present", "present"])
        self.assertTrue(all(r["new_receipt"] for r in receipt["files"]))
        self.assertEqual(self.ledger(), (1, 4))

    def test_skips_are_not_errors_and_no_symlinks_followed(self):
        self.write("visible.pdf")
        self.write(".hidden/secret.pdf")
        self.write(".file")
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "other.txt").write_bytes(b"outside synthetic")
        (self.folder / "linked-dir").symlink_to(outside)
        (self.folder / "linked-file").symlink_to(outside / "other.txt")
        os.mkfifo(self.folder / "pipe")
        with (self.folder / "large.pdf").open("wb") as handle:
            handle.truncate(cli.MAX_BYTES + 1)
        receipt = self.receipt(self.run_cli(0))
        self.assertEqual({r["path"]: r["outcome"] for r in receipt["files"]},
                         {".hidden": "hidden", ".file": "hidden", "linked-dir": "symlink",
                          "linked-file": "symlink", "pipe": "not_regular", "large.pdf": "too_large", "visible.pdf": "stored"})
        self.assertEqual(self.ledger(), (1, 1))
        for row in receipt["files"]:
            if row["outcome"] != "stored":
                self.assertIsNone(row["sha256_prefix"])
                self.assertFalse(row["new_receipt"])

    def test_vanishing_file_records_error_continues_and_cli_exits_three(self):
        gone = self.write("a.pdf")
        self.write("z.pdf")
        original = cli._ingest_one

        def vanish(db, root, name, source, dir_fd=None):
            if name == "a.pdf":
                gone.unlink()
            return original(db, root, name, source, dir_fd=dir_fd)

        args = ["campaign_tool", "import-existing", "--directory", str(self.root),
                "--from", str(self.folder), "--label", "l"]
        output = io.StringIO()
        with patch.object(sys, "argv", args), patch.object(cli, "_ingest_one", side_effect=vanish), contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as caught:
                cli.main()
        self.assertEqual(caught.exception.code, 3)
        receipt = self.receipt(json.loads(output.getvalue()))
        self.assertEqual([r["outcome"] for r in receipt["files"]], ["error", "stored"])
        self.assertIn("Input changed", receipt["files"][0]["error"])
        self.assertFalse(receipt["stopped"])
        self.assertNotIn(str(self.base), json.dumps(receipt))

    def test_integrity_mismatch_writes_stopped_receipt_and_never_attempts_later_files(self):
        path = self.write("a.pdf")
        self.write("z.pdf")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        objects = self.root / "private/objects"
        objects.mkdir(parents=True)
        (objects / digest).write_bytes(b"corrupt synthetic")
        receipt = self.receipt(self.run_cli(3))
        self.assertTrue(receipt["stopped"])
        self.assertEqual(len(receipt["files"]), 1)
        self.assertEqual(receipt["files"][0]["outcome"], "error")
        self.assertIn("Stored object integrity mismatch", receipt["files"][0]["error"])
        self.assertEqual(self.ledger(), (0, 0))
        self.assertEqual((objects / digest).read_bytes(), b"corrupt synthetic")

    def test_file_cap_refuses_before_any_store_or_receipt_write(self):
        for index in range(2001):
            self.write(f"{index:04}.txt", b"")
        with self.assertRaisesRegex(ValueError, "2000 files"):
            self.run_import()
        self.assertFalse((self.root / "private").exists())

    def test_invalid_labels_refuse_before_any_write(self):
        for label in ("../x", "UPPER", "a" * 41, "", "-a", "a\n"):
            with self.subTest(label=label), self.assertRaises(ValueError):
                importer.run(self.root, self.folder, label)
        self.assertFalse((self.root / "private").exists())

    def test_invalid_folders_and_missing_campaign_refuse_without_writes(self):
        link = self.base / "link"
        link.symlink_to(self.folder)
        file = self.write("one.pdf")
        nested = self.root / "nested"
        nested.mkdir()
        for folder in (link, str(link) + "/.", file, nested, self.root, self.base / "absent"):
            with self.subTest(folder=str(folder)), self.assertRaises((ValueError, OSError)):
                importer.run(self.root, folder, "l")
            self.assertFalse((self.root / "private").exists())
        (self.root / "campaign.json").unlink()
        with self.assertRaises(OSError):
            self.run_import()
        self.assertFalse((self.root / "private").exists())

    def test_subfolder_swapped_after_inventory_does_not_follow_link(self):
        inside = self.write("a/one.pdf")
        self.write("z.pdf")
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "one.pdf").write_bytes(b"outside")
        inventory = importer._entries

        def swap(fd):
            entries = inventory(fd)
            inside.unlink()
            inside.parent.rmdir()
            inside.parent.symlink_to(outside)
            return entries

        with patch.object(importer, "_entries", side_effect=swap):
            receipt = self.receipt(self.run_import())
        self.assertEqual([r["outcome"] for r in receipt["files"]], ["error", "stored"])
        self.assertEqual(self.ledger(), (1, 1))
        self.assertFalse((self.root / "private/objects" / hashlib.sha256(b"outside").hexdigest()).exists())

    def test_kind_hints_are_extensions_only_and_empty_import_is_valid(self):
        self.assertEqual(self.run_import()["sources"], {})
        for ext, kind in ((".MSG", "email"), (".mbox", "email"), (".jpg", "image"),
                          (".xlsx", "spreadsheet"), (".docx", "document"), (".zip", "other")):
            self.write("synthetic" + ext, b"not parsed")
            self.assertEqual(importer._kind("synthetic" + ext), kind)
        self.assertEqual(self.run_import()["counts"]["error"], 0)

    def snapshot(self, top):
        return {str(p.relative_to(top)): (p.stat().st_mode, p.stat().st_mtime_ns,
                                          p.read_bytes() if p.is_file() else None)
                for p in sorted(top.rglob("*"))}

    def cli_refusal(self, source, root=None):
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "import-existing",
                                 "--directory", str(root or self.root), "--from", str(source), "--label", "l"],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr.count("Stopped:"), 1)
        self.assertIn("must be disjoint", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn("owner-note", result.stderr)

    def test_ancestor_source_refused_before_inventory_or_store(self):
        (self.root / "private").mkdir()
        (self.root / "private/owner-note.txt").write_bytes(b"synthetic private")
        (self.base / "agency-policy.txt").write_bytes(b"synthetic agency")
        before = self.snapshot(self.base)
        with patch.object(importer, "_entries") as entries, patch.object(cli, "database") as database, \
                self.assertRaisesRegex(ValueError, "must be disjoint"):
            importer.run(self.root, self.base, "l", now=self.instant)
        entries.assert_not_called()
        database.assert_not_called()
        self.assertEqual(self.snapshot(self.base), before)
        self.cli_refusal(self.base)
        self.assertEqual(self.snapshot(self.base), before)

    def test_overlap_matrix_and_cli_refusal(self):
        deep = self.root / "a" / "b"
        deep.mkdir(parents=True)
        (self.root / ".hidden").mkdir()
        alias = self.base / "alias"
        alias.symlink_to(self.base)
        cases = (self.root, deep, self.base, self.base / ".", self.root / "a" / "..",
                 self.root / ".." / "campaign", alias / "campaign", alias, self.base.parent)
        for case in cases:
            with self.subTest(case=str(case)):
                before = self.snapshot(self.base)
                with self.assertRaisesRegex(ValueError, "must be disjoint"):
                    importer.run(self.root, case, "l", now=self.instant)
                self.assertEqual(self.snapshot(self.base), before)
        for case in (self.root, deep, self.base):
            with self.subTest(cli=str(case)):
                self.cli_refusal(case)

    def test_overlap_does_not_depend_on_discoverable_private_files(self):
        with patch.object(importer, "_entries") as entries, patch.object(cli, "database") as database, \
                patch.object(importer, "_write_receipt") as receipt:
            with self.assertRaises(ValueError):
                importer.run(self.root, self.base, "l", now=self.instant)
        for spy in (entries, database, receipt):
            spy.assert_not_called()
        self.assertFalse((self.root / "private").exists())

    def test_disjoint_siblings_and_prefix_names_repeat_safely(self):
        for name in ("campaign-records", "records é 2"):
            with self.subTest(name=name):
                folder = self.base / name
                folder.mkdir()
                (folder / "one.txt").write_bytes(b"synthetic " + name.encode())
                first = importer.run(self.root, folder, "l", now=self.instant)
                receipt_bytes = (self.root / first["receipt"]).read_bytes()
                counts = self.ledger()
                second = importer.run(self.root, folder, "l", now=self.instant)
                self.assertEqual(self.ledger(), counts)
                self.assertNotEqual(second["receipt"], first["receipt"])
                self.assertEqual((self.root / first["receipt"]).read_bytes(), receipt_bytes)
        self.assertEqual(self.run_import()["counts"]["error"], 0)

    def test_source_final_symlink_refusal_is_not_resolved_away(self):
        link = self.base / "link"
        link.symlink_to(self.folder)
        for spelling in (link, str(link) + "/", str(link) + "/."):
            with self.subTest(spelling=str(spelling)), self.assertRaisesRegex(ValueError, "real directory"):
                importer.run(self.root, spelling, "l", now=self.instant)
        self.assertFalse((self.root / "private").exists())


if __name__ == "__main__":
    unittest.main()
