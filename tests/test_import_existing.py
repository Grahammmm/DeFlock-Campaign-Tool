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

    # --- Atomic, no-clobber receipt publication (RF1925-8) ---

    CHILD = '''import os, sys
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from campaign_tool import import_existing as importer
mode, root, folder = sys.argv[2:5]
if mode == "serialize":
    def dump(obj, fp, **kwargs):
        fp.write('{"schema":')
        fp.flush()
        os.fsync(fp.fileno())
        os._exit(73)
    importer.json.dump = dump
else:
    real_link = os.link
    def link(*args, **kwargs):
        real_link(*args, **kwargs)
        os._exit(73)
    importer.os.link = link
importer.run(Path(root), Path(folder), "l", now=datetime(2026, 1, 1, tzinfo=timezone.utc))
'''

    def imports(self):
        directory = self.root / "private/imports"
        return directory, sorted(p.name for p in directory.iterdir()) if directory.is_dir() else []

    def finals(self):
        return [n for n in self.imports()[1] if n.endswith(".json")]

    def stages(self):
        return [n for n in self.imports()[1] if not n.endswith(".json")]

    def child_exit(self, mode):
        script = self.base / "child.py"
        script.write_text(self.CHILD)
        result = subprocess.run([sys.executable, "-B", str(script), str(Path(cli.__file__).resolve().parent.parent),
                                 mode, str(self.root), str(self.folder)], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 73, result.stderr)

    def expected_bytes(self, receipt):
        return (json.dumps(receipt, indent=2) + "\n").encode("utf-8")

    def test_process_exit_during_receipt_write_never_exposes_final_json(self):
        self.write("one.pdf")
        self.child_exit("serialize")
        directory, names = self.imports()
        self.assertEqual(self.finals(), [])
        self.assertEqual(len(names), 1)
        stage = directory / names[0]
        self.assertTrue(names[0].startswith("."))
        self.assertEqual(stat.S_IMODE(stage.stat().st_mode), 0o600)
        torn = stage.read_bytes()
        self.assertEqual(torn, b'{"schema":')
        self.assertEqual(self.ledger(), (1, 1))  # Preserved object and ledger survive the interruption.
        retry = self.run_import()
        self.assertEqual(retry["receipt"], "private/imports/20260101T000000Z-l.json")
        self.assertEqual(retry["counts"]["present"], 1)
        self.assertEqual(retry["counts"]["stored"], 0)
        self.assertEqual(self.ledger(), (1, 1))
        for name in self.finals():
            json.loads((directory / name).read_text())
        self.assertEqual(stage.read_bytes(), torn)  # Never trusted, resumed or deleted.

    def test_ordinary_serialization_flush_and_file_fsync_errors_leave_no_final(self):
        self.write("one.pdf")
        first = self.run_import()
        directory = self.root / "private/imports"
        before = (directory / "20260101T000000Z-l.json").read_bytes()
        receipt = {"schema": "import-receipt-v1", "files": []}
        closed = []
        real_fdopen, real_fsync = os.fdopen, os.fsync

        class FlushFails:
            def __init__(self, handle):
                self.handle = handle
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                self.handle.close()
                closed.append(self.handle.closed)
            def __getattr__(self, name):
                return getattr(self.handle, name)
            def flush(self):
                raise OSError("synthetic flush failure")

        def file_fsync_fails(fd):
            if stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError("synthetic fsync failure")
            return real_fsync(fd)

        cases = {
            "serialize": patch.object(importer.json, "dump", side_effect=OSError("synthetic dump failure")),
            "flush": patch.object(importer.os, "fdopen", lambda *a, **k: FlushFails(real_fdopen(*a, **k))),
            "fsync": patch.object(importer.os, "fsync", side_effect=file_fsync_fails),
        }
        for label, context in cases.items():
            with self.subTest(case=label), context, self.assertRaises(OSError):
                importer._write_receipt(self.root, "20260101T000000Z", "l", receipt)
            self.assertEqual(self.finals(), ["20260101T000000Z-l.json"])
            self.assertEqual(self.stages(), [])  # Owned stage cleaned on ordinary failure.
            self.assertEqual((directory / "20260101T000000Z-l.json").read_bytes(), before)
        self.assertEqual(closed, [True])
        self.assertEqual(self.receipt(first)["schema"], "import-receipt-v1")

    def test_publish_collision_preserves_existing_file_symlink_and_other_stage(self):
        directory = self.root / "private/imports"
        directory.mkdir(parents=True)
        other = directory / ".receipt-other-run.tmp"
        other.write_bytes(b"{partial")
        other.chmod(0o600)
        outside = self.base / "outside.txt"
        outside.write_bytes(b"outside sentinel")
        sentinel = directory / "20260101T000000Z-l.json"
        real_link = os.link
        arrivals = [lambda: sentinel.write_bytes(b"concurrent sentinel"),
                    lambda: (directory / "20260101T000000Z-l-2.json").symlink_to(outside)]

        def racing_link(*args, **kwargs):
            if arrivals:
                arrivals.pop(0)()
            return real_link(*args, **kwargs)

        other_mtime = other.lstat().st_mtime_ns
        receipt = {"schema": "import-receipt-v1", "n": 1}
        with patch.object(importer.os, "link", side_effect=racing_link):
            name = importer._write_receipt(self.root, "20260101T000000Z", "l", receipt)
        self.assertEqual(name, "private/imports/20260101T000000Z-l-3.json")
        self.assertEqual(sentinel.read_bytes(), b"concurrent sentinel")
        self.assertEqual(outside.read_bytes(), b"outside sentinel")
        self.assertTrue((directory / "20260101T000000Z-l-2.json").is_symlink())
        self.assertEqual((self.root / name).read_bytes(), self.expected_bytes(receipt))
        self.assertEqual(other.read_bytes(), b"{partial")
        self.assertEqual(other.lstat().st_mtime_ns, other_mtime)
        self.assertEqual(self.stages(), [".receipt-other-run.tmp"])

    def test_two_writers_in_the_same_second_publish_distinct_valid_receipts(self):
        import threading
        (self.root / "private").mkdir()
        barrier, results, real_link = threading.Barrier(2, timeout=20), {}, os.link
        waited = set()

        def synchronized_link(*args, **kwargs):
            if threading.get_ident() not in waited:  # Meet only before each writer's first attempt.
                waited.add(threading.get_ident())
                barrier.wait()
            return real_link(*args, **kwargs)

        def writer(number):
            results[number] = importer._write_receipt(self.root, "20260101T000000Z", "l", {"n": number})

        with patch.object(importer.os, "link", side_effect=synchronized_link):
            threads = [threading.Thread(target=writer, args=(n,)) for n in (1, 2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(30)
        self.assertEqual(sorted(results), [1, 2])
        self.assertEqual(len(set(results.values())), 2)
        for number, name in results.items():
            self.assertEqual((self.root / name).read_bytes(), self.expected_bytes({"n": number}))
        self.assertEqual(self.stages(), [])

    def test_crash_after_publication_keeps_complete_receipt(self):
        self.write("one.pdf")
        self.child_exit("link")
        directory, _ = self.imports()
        self.assertEqual(self.finals(), ["20260101T000000Z-l.json"])
        published = (directory / "20260101T000000Z-l.json").read_bytes()
        self.assertTrue(published.endswith(b"}\n"))
        self.assertEqual(json.loads(published)["schema"], "import-receipt-v1")
        self.assertEqual(len(self.stages()), 1)  # Both names may remain after a crash.
        retry = self.run_import()
        self.assertEqual(retry["receipt"], "private/imports/20260101T000000Z-l-2.json")
        self.assertEqual((directory / "20260101T000000Z-l.json").read_bytes(), published)

    def test_directory_fsync_and_cleanup_errors_after_publication_keep_final(self):
        receipt, real_fsync = {"schema": "import-receipt-v1", "n": 1}, os.fsync
        (self.root / "private").mkdir()

        def directory_fsync_fails(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("synthetic directory fsync failure")
            return real_fsync(fd)

        with patch.object(importer.os, "fsync", side_effect=directory_fsync_fails), self.assertRaises(OSError):
            importer._write_receipt(self.root, "20260101T000000Z", "l", receipt)
        self.assertEqual(self.finals(), ["20260101T000000Z-l.json"])
        self.assertEqual((self.root / "private/imports/20260101T000000Z-l.json").read_bytes(), self.expected_bytes(receipt))
        self.assertEqual(self.stages(), [])
        with patch.object(importer.os, "unlink", side_effect=OSError("synthetic cleanup failure")), \
                self.assertRaises(OSError):
            importer._write_receipt(self.root, "20260101T000000Z", "l", receipt)
        self.assertEqual(self.finals(), ["20260101T000000Z-l-2.json", "20260101T000000Z-l.json"])
        self.assertEqual((self.root / "private/imports/20260101T000000Z-l-2.json").read_bytes(), self.expected_bytes(receipt))
        self.assertEqual(len(self.stages()), 1)

    def test_full_import_receipt_failure_retains_ledger_and_retry_is_present(self):
        path = self.write("one.pdf")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        with patch.object(importer.json, "dump", side_effect=OSError("synthetic dump failure")), \
                self.assertRaises(OSError):
            self.run_import()
        self.assertEqual((self.root / "private/objects" / digest).read_bytes(), path.read_bytes())
        self.assertEqual(self.ledger(), (1, 1))
        self.assertEqual(self.finals(), [])
        self.assertEqual(self.stages(), [])
        retry = self.run_import()
        self.assertEqual((retry["counts"]["present"], retry["counts"]["stored"]), (1, 0))
        self.assertEqual(self.ledger(), (1, 1))
        self.assertEqual(self.finals(), ["20260101T000000Z-l.json"])

    def test_receipt_exact_bytes_modes_suffixes_and_empty_import_preserved(self):
        old = os.umask(0)
        self.addCleanup(os.umask, old)
        directory = self.root / "private/imports"
        directory.mkdir(parents=True)
        directory.chmod(0o755)
        receipt = {"schema": "import-receipt-v1", "label": "l", "note": "caf\u00e9", "files": []}
        names = [importer._write_receipt(self.root, "20260101T000000Z", "l", receipt) for _ in range(3)]
        self.assertEqual(names, [f"private/imports/20260101T000000Z-l{s}.json" for s in ("", "-2", "-3")])
        for name in names:
            self.assertEqual((self.root / name).read_bytes(), self.expected_bytes(receipt))
            self.assertEqual(stat.S_IMODE((self.root / name).stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        self.assertEqual(self.stages(), [])
        empty = self.run_import()
        self.assertEqual(empty["receipt"], "private/imports/20260101T000000Z-l-4.json")
        text = (self.root / empty["receipt"]).read_bytes()
        self.assertEqual(text, self.expected_bytes(json.loads(text)))
        self.assertEqual(self.receipt(empty)["files"], [])
        self.assertEqual(self.stages(), [])


    def raw(self, name_bytes, data):
        path = os.path.join(os.fsencode(self.folder), name_bytes)
        with open(path, "wb") as handle:
            handle.write(data)

    def objects_on_disk(self):
        directory = self.root / "private/objects"
        return sorted(directory.iterdir()) if directory.is_dir() else []

    def test_undecodable_basename_errors_before_preservation_and_continues(self):
        from campaign_tool import store_check
        self.write("a.txt")
        self.raw(b"m-invalid-\xff.txt", b"bad bytes")
        self.write("z.txt")
        seen = []
        real = cli._ingest_one
        def watch(db, root, name, *args, **kwargs):
            seen.append(name)
            return real(db, root, name, *args, **kwargs)
        with patch.object(cli, "_ingest_one", watch):
            summary = self.run_import()
        self.assertEqual(seen, ["a.txt", "z.txt"])
        self.assertEqual((summary["counts"]["stored"], summary["counts"]["error"], summary["stopped"]), (2, 1, False))
        files = self.receipt(summary)["files"]
        self.assertEqual([row["outcome"] for row in files], ["stored", "error", "stored"])
        bad = files[1]
        self.assertEqual(bad["error"], "Filename cannot be represented as UTF-8; original was not imported")
        self.assertIsNone(bad["sha256_prefix"])
        self.assertFalse(bad["new_receipt"])
        self.assertEqual(os.fsencode(bad["path"]), b"m-invalid-\xff.txt")
        self.assertEqual(len(self.objects_on_disk()), 2)
        self.assertEqual(self.ledger(), (2, 2))
        self.assertEqual(store_check.check_store(self.root)["problems"], [])

    def test_cli_bad_basename_exit_three_rerun_and_manual_rename(self):
        self.write("a.txt")
        self.raw(b"bad-\xff\xfe.txt", b"bad bytes")
        first = self.run_cli(3)
        files = self.receipt(first)["files"]
        self.assertEqual([row["outcome"] for row in files], ["stored", "error"])
        self.assertNotIn("codec", json.dumps(files))
        again = self.run_cli(3)
        self.assertEqual([row["outcome"] for row in self.receipt(again)["files"]], ["present", "error"])
        self.assertEqual(len(self.objects_on_disk()), 1)
        self.assertEqual(self.ledger(), (1, 1))
        os.rename(os.path.join(os.fsencode(self.folder), b"bad-\xff\xfe.txt"),
                  os.path.join(os.fsencode(self.folder), b"good.txt"))
        third = self.run_cli(0)
        self.assertEqual([row["outcome"] for row in self.receipt(third)["files"]], ["present", "stored"])
        self.assertEqual(self.ledger(), (2, 2))

    def test_all_bad_names_read_nothing_and_publish_receipt(self):
        self.raw(b"\xc3.txt", b"one")
        self.raw(b"\xe2\x82.txt", b"two")
        with patch.object(cli, "_ingest_one", side_effect=AssertionError("must not ingest")):
            summary = self.run_import()
        self.assertEqual(summary["counts"]["error"], 2)
        self.assertFalse(summary["stopped"])
        self.assertEqual(self.objects_on_disk(), [])
        self.assertEqual(self.ledger(), (0, 0))
        self.assertEqual(stat.S_IMODE((self.root / summary["receipt"]).stat().st_mode), 0o600)

    def test_bad_name_leaves_preexisting_same_digest_untouched(self):
        self.write("ok.txt", b"same")
        self.run_import()
        before = [(p.name, p.read_bytes()) for p in self.objects_on_disk()]
        self.raw(b"x-\xff.txt", b"same")
        summary = self.run_import()
        self.assertEqual([r["outcome"] for r in self.receipt(summary)["files"]], ["present", "error"])
        self.assertEqual(summary["counts"]["error"], 1)
        self.assertEqual([(p.name, p.read_bytes()) for p in self.objects_on_disk()], before)
        self.assertEqual(self.ledger(), (1, 1))

    def test_skip_precedence_and_unicode_names_unchanged(self):
        from campaign_tool import store_check
        self.raw(b".hidden-\xff", b"h")
        os.symlink("nowhere", os.path.join(os.fsencode(self.folder), b"link-\xff"))
        for name in ("caf\u00e9.txt", "cafe\u0301.txt", "\u6587\u4ef6.txt", "\U0001f600.txt"):
            self.write(name)
        summary = self.run_import()
        self.assertEqual(summary["counts"]["hidden"], 1)
        self.assertEqual(summary["counts"]["symlink"], 1)
        self.assertEqual(summary["counts"]["error"], 0)
        self.assertEqual(summary["counts"]["stored"], 4)
        self.assertEqual(store_check.check_store(self.root)["problems"], [])

    def test_valid_leaf_under_raw_directory_remains_eligible(self):
        directory = os.path.join(os.fsencode(self.folder), b"dir-\xff")
        os.mkdir(directory)
        with open(os.path.join(directory, b"ok.txt"), "wb") as handle:
            handle.write(b"ok")
        summary = self.run_import()
        self.assertEqual(summary["counts"]["stored"], 1)
        self.assertEqual(summary["counts"]["error"], 0)


if __name__ == "__main__":
    unittest.main()
