"""ledger_list._snapshot is anchored to the admitted private directory (synthetic only)."""
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import ledger_list

REFUSAL = "private/ledger.sqlite is not a readable ledger"


def make_ledger(directory, source):
    directory.mkdir()
    db = sqlite3.connect(directory / "ledger.sqlite")
    db.execute("CREATE TABLE objects (sha256 TEXT, byte_count INTEGER)")
    db.execute("CREATE TABLE receipts (receipt_id INTEGER, object_sha256 TEXT, source_id TEXT, "
               "original_name TEXT, received_at TEXT)")
    db.execute("INSERT INTO objects VALUES (?, 1)", ("a" * 64,))
    db.execute("INSERT INTO receipts VALUES (1, ?, ?, 'n', '2026-01-01T00:00:00+00:00')", ("a" * 64, source))
    db.commit()
    db.close()


class ParentBindingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-parent-binding-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.private = self.root / "private"
        make_ledger(self.private, "local-synthetic")
        self.foreign = self.root / "foreign"
        make_ledger(self.foreign, "FOREIGN-SYNTHETIC-CANARY")
        self.ledger = self.private / "ledger.sqlite"

    def swap_parent(self):
        os.rename(self.private, self.root / "moved")
        os.symlink(self.foreign, self.private)

    def hooked(self, name, hook):
        real = getattr(os, name)
        state = {"done": False}

        def wrapper(*args, **kwargs):
            if not state["done"]:
                state["done"] = True
                hook()
            return real(*args, **kwargs)
        return mock.patch(f"os.{name}", wrapper)

    def test_control_lists_local_ledger(self):
        self.assertEqual(ledger_list.list_receipts(self.root)["receipts"][0]["source_id"], "local-synthetic")

    def test_private_symlink_swap_before_ledger_open_never_reads_foreign_payload(self):
        real_open = os.open
        seen = []

        def forwarding(path, flags, *args, **kwargs):
            if os.fspath(path) == "ledger.sqlite" and not seen:
                seen.append(1)
                self.swap_parent()
            return real_open(path, flags, *args, **kwargs)
        with mock.patch("os.open", forwarding):
            try:
                report = ledger_list.list_receipts(self.root)
            except ValueError as error:
                self.assertEqual(str(error), REFUSAL)
                return
        # The held descriptor read the original directory; the final identity check must still refuse.
        self.fail(f"snapshot escaped after substitution: {report}")

    def test_parent_replacement_before_directory_acquisition_is_refused(self):
        real_open = os.open
        state = []

        def forwarding(path, flags, *args, **kwargs):
            if os.fspath(path) == str(self.private) and not state:
                state.append(1)
                self.swap_parent()
            return real_open(path, flags, *args, **kwargs)
        with mock.patch("os.open", forwarding):
            with self.assertRaises(ValueError) as caught:
                ledger_list._snapshot(self.ledger)
        self.assertEqual(str(caught.exception), REFUSAL)

    def test_replacement_by_distinct_real_directory_is_refused(self):
        def replace():
            os.rename(self.private, self.root / "moved")
            os.rename(self.foreign, self.private)
        with self.hooked("open", replace):
            with self.assertRaises(ValueError) as caught:
                ledger_list._snapshot(self.ledger)
        self.assertEqual(str(caught.exception), REFUSAL)

    def test_parent_swap_during_read_refuses_before_snapshot_escapes(self):
        with self.hooked("dup", self.swap_parent):
            with self.assertRaises(ValueError) as caught:
                ledger_list._snapshot(self.ledger)
        self.assertEqual(str(caught.exception), REFUSAL)

    def test_parent_disappearance_during_read_is_refused(self):
        with self.hooked("dup", lambda: os.rename(self.private, self.root / "moved")):
            with self.assertRaises(ValueError) as caught:
                ledger_list._snapshot(self.ledger)
        self.assertEqual(str(caught.exception), REFUSAL)

    def test_missing_and_non_directory_parent_refused(self):
        with self.assertRaises(ValueError):
            ledger_list._snapshot(self.root / "nope" / "ledger.sqlite")
        (self.root / "file").write_text("x")
        with self.assertRaises(ValueError):
            ledger_list._snapshot(self.root / "file" / "ledger.sqlite")

    def test_sidecar_checks_follow_the_admitted_directory_not_replacement(self):
        # sidecar only in the foreign directory, parent swapped before the sidecar check: still no foreign read
        (self.foreign / "ledger.sqlite-wal").write_bytes(b"")
        with self.hooked("stat", self.swap_parent):
            with self.assertRaises(ValueError):
                ledger_list._snapshot(self.ledger)
        self.assertTrue((self.foreign / "ledger.sqlite-wal").exists())

    def test_sidecars_in_admitted_directory_keep_sidecar_error(self):
        (self.private / "ledger.sqlite-journal").write_bytes(b"")
        with self.assertRaises(ValueError) as caught:
            ledger_list._snapshot(self.ledger)
        self.assertEqual(str(caught.exception), ledger_list.SIDECAR_ERROR)
        (self.private / "ledger.sqlite-journal").unlink()
        os.symlink("dangling-target", self.private / "ledger.sqlite-shm")
        with self.assertRaises(ValueError) as caught:
            ledger_list._snapshot(self.ledger)
        self.assertEqual(str(caught.exception), ledger_list.SIDECAR_ERROR)
        self.assertTrue(os.path.lexists(self.private / "ledger.sqlite-shm"))

    def test_sidecar_appearing_during_read_is_refused(self):
        with self.hooked("dup", lambda: (self.private / "ledger.sqlite-wal").write_bytes(b"")):
            with self.assertRaises(ValueError) as caught:
                ledger_list._snapshot(self.ledger)
        self.assertEqual(str(caught.exception), ledger_list.SIDECAR_ERROR)

    def test_cli_refuses_substitution_with_fixed_message(self):
        real_open = os.open
        seen = []

        def forwarding(path, flags, *args, **kwargs):
            if os.fspath(path) == "ledger.sqlite" and not seen:
                seen.append(1)
                self.swap_parent()
            return real_open(path, flags, *args, **kwargs)
        with mock.patch("os.open", forwarding):
            with self.assertRaises(ValueError) as caught:
                ledger_list.list_receipts(self.root)
        self.assertNotIn("FOREIGN", str(caught.exception))
        self.assertEqual(str(caught.exception), REFUSAL)

    def test_directory_descriptor_closed_on_every_path(self):
        real_open, real_close = os.open, os.close
        opened, closed = [], []

        def tracking_open(*args, **kwargs):
            fd = real_open(*args, **kwargs)
            opened.append(fd)
            return fd

        def tracking_close(fd):
            closed.append(fd)
            return real_close(fd)
        with mock.patch("os.open", tracking_open), mock.patch("os.close", tracking_close):
            ledger_list._snapshot(self.ledger).close()
            (self.private / "ledger.sqlite-wal").write_bytes(b"")
            with self.assertRaises(ValueError):
                ledger_list._snapshot(self.ledger)
        self.assertEqual(sorted(opened), sorted(closed))
        self.assertEqual(len(opened), 3)  # dir+ledger, then dir only


if __name__ == "__main__":
    unittest.main()
