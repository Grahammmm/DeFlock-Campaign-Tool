"""Synthetic saved-selection checks, not race-proof filesystem confinement.

Application imports are lazy and inside scoped offline guards. No pipeline or
real queue is constructed. Ordinary unittest loading needs no staging manifest.
"""
from contextlib import contextmanager, ExitStack
import builtins
import importlib
import io
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.request

REPO = Path(__file__).resolve().parents[2]


@contextmanager
def offline(fixture):
    """Scoped sentinels for this known import/call closure, not a sandbox.

    The closure has explicit caller-supplied data paths, no live data constants
    or bound data-path defaults. Repository/stdlib imports are read-only; all
    data access is fixture-owned. Receipt bytes are never needed at this layer.
    """
    fixture = Path(fixture).absolute()
    read_roots = (REPO, Path(sys.base_prefix).resolve(), Path(sys.prefix).resolve())
    original_open, original_io, original_os = builtins.open, io.open, os.open

    def deny(*args, **kwargs):
        raise AssertionError("FA1702 forbidden network/model/process/database access")

    def check(file, writing=False):
        if isinstance(file, int):
            return
        path = Path(os.path.abspath(file))
        # Reject receipt content even inside fictional directories.
        if path.suffix == ".json" and path.is_relative_to(fixture):
            raise AssertionError("FA1702 receipt-content read/write forbidden during open")
        if path.is_relative_to(fixture):
            return
        if not writing and any(path.is_relative_to(root) for root in read_roots):
            return
        raise AssertionError("FA1702 nonfixture data access")

    def wrapped(original):
        def opening(file, mode="r", *args, **kwargs):
            check(file, any(flag in mode for flag in "wax+"))
            return original(file, mode, *args, **kwargs)
        return opening

    def os_open(file, flags, *args, **kwargs):
        check(file, bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)))
        return original_os(file, flags, *args, **kwargs)

    with ExitStack() as stack:
        for obj, name, replacement in (
            (builtins, "open", wrapped(original_open)), (io, "open", wrapped(original_io)),
            (os, "open", os_open), (socket, "getaddrinfo", deny),
            (socket, "create_connection", deny), (socket.socket, "connect", deny),
            (socket.socket, "connect_ex", deny), (urllib.request, "urlopen", deny),
            (subprocess, "Popen", deny), (os, "system", deny),
            (sqlite3, "connect", deny),
        ):
            stack.enter_context(patch.object(obj, name, replacement))
        yield


@contextmanager
def faulty_open(selected, canonical):
    """Deliberately omit only use-boundary reconstruction, entirely test-local."""
    from campaign_tool.records import export_selection as selection, run
    canonical = Path(os.path.abspath(canonical))
    if canonical != selected.canonical_root:
        raise ValueError("export_selection_root_mismatch")
    run.require_root_lock(canonical)
    queue = selection.Queue(canonical / "portal")
    try:
        inventory = selection.CollectionInventory(queue, selected.scope)
        yield {"root": selected.root, "receipts": selected.receipts, "inventory": inventory}
    finally:
        queue.close()


class SelectionFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(
            prefix="fa1702-fixture-", dir=os.environ.get("TMPDIR") or tempfile.gettempdir())
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.guards = self.enterContext(offline(self.base))
        self.selection = importlib.import_module("campaign_tool.records.export_selection")
        self.run = importlib.import_module("campaign_tool.records.run")
        self.trace = []
        self.inventory = object()
        outer = self

        class QueueDouble:
            def __init__(self, path):
                outer.trace.append(("queue", path))
                self.closed = 0

            def close(self):
                self.closed += 1
                outer.trace.append(("close", self.closed))

        def inventory(queue, scope):
            self.trace.append(("inventory", queue, scope))
            return self.inventory

        def locked(root):
            self.trace.append(("lock", root))

        self.enterContext(patch.object(self.run, "require_root_lock", locked))
        self.enterContext(patch.object(self.selection, "Queue", QueueDouble))
        self.enterContext(patch.object(self.selection, "CollectionInventory", inventory))

    def make_selection(self, label):
        base = self.base / label
        exports = base / "export-parent" / "exports"
        canonical = base / "canonical-parent" / "canonical"
        (exports / "receipts" / "middle").mkdir(parents=True)
        canonical.mkdir(parents=True)
        # Intentionally absent receipt files: path admission is metadata-only.
        scope = self.selection.Scope(frozenset({"archive.example.invalid"}))
        selected = self.selection.ExportSelection(
            str(exports), ("receipts/middle/one.json", "receipts/middle/two.json"),
            scope, str(canonical))
        expected = (exports / "receipts/middle/one.json", exports / "receipts/middle/two.json")
        self.assertEqual(selected.receipts, expected)
        return selected, expected

    def positive(self, label):
        selected, expected = self.make_selection(label)
        self.trace.clear()
        with selected.open(str(selected.canonical_root)) as opened:
            self.assertEqual(opened, {"root": selected.root, "receipts": expected,
                                      "inventory": self.inventory})
            self.assertEqual([row[0] for row in self.trace], ["lock", "queue", "inventory"])
            queue = self.trace[2][1]
            self.assertEqual(queue.closed, 0)
            self.assertEqual(self.trace[1][1], selected.canonical_root / "portal")
            self.assertIs(self.trace[2][2], selected.scope)
        self.assertEqual(queue.closed, 1)
        self.assertEqual([row[0] for row in self.trace], ["lock", "queue", "inventory", "close"])
        self.assertFalse((selected.canonical_root / "portal").exists())

    def changed_ancestor(self, kind):
        selected, expected = self.make_selection("changed-" + kind)
        if kind == "export":
            ancestor = selected.root.parent
        elif kind == "receipt":
            ancestor = selected.root / "receipts" / "middle"
        elif kind == "canonical":
            ancestor = selected.canonical_root.parent
        else:
            self.fail("unknown fixture kind")
        moved = ancestor.with_name(ancestor.name + "-saved")
        alternate = self.base / (kind + "-alternate")
        alternate.mkdir()
        ancestor.rename(moved)
        ancestor.symlink_to(alternate, target_is_directory=True)
        self.assertTrue(ancestor.is_symlink())
        self.assertEqual(selected.receipts, expected)
        self.assertEqual(selected.scope.hosts, frozenset({"archive.example.invalid"}))
        return selected

    def snapshot(self):
        # Metadata only, no traversal through symlink replacements.
        return sorted((str(path.relative_to(self.base)), path.lstat().st_mode,
                       path.lstat().st_size, path.lstat().st_mtime_ns)
                      for path in self.base.rglob("*"))

    def assert_no_queue(self):
        self.assertEqual([row for row in self.trace if row[0] == "queue"], [],
                         "USE_BOUNDARY_NO_QUEUE")
        self.assertEqual([row for row in self.trace if row[0] == "inventory"], [])

    def boundary(self, kind, adapter=None):
        selected = self.changed_ancestor(kind)
        before = self.snapshot()
        self.trace.clear()
        caught = None
        try:
            with (adapter(selected, str(selected.canonical_root)) if adapter
                  else selected.open(str(selected.canonical_root))):
                pass
        except self.selection.PortalError as error:
            caught = error
        # This exact oracle is shared by the real and faulty paths. It runs
        # before the error assertion, so the control must fail on queue access.
        self.assert_no_queue()
        self.assertIsNotNone(caught)
        self.assertEqual(str(caught), "symlink_path")
        self.assertEqual(self.trace, [("lock", selected.canonical_root)])
        self.assertEqual(self.snapshot(), before)
        self.positive("fresh-after-" + kind)


class SelectionUseBoundaryTests(SelectionFixture, unittest.TestCase):
    def test_unchanged_selection_normalizes_yields_and_closes_once(self):
        self.positive("unchanged")

    def test_export_root_ancestor_changed_after_construction(self):
        self.boundary("export")

    def test_receipt_intermediate_ancestor_changed_after_construction(self):
        self.boundary("receipt")

    def test_canonical_root_ancestor_changed_after_construction(self):
        self.boundary("canonical")


def negative_suite():
    # Not discovered by ordinary unittest. The launcher alone expects this one
    # retained, exact assertion failure, then reruns every real behavior gate.
    class MissingRecheckProbe(SelectionFixture, unittest.TestCase):
        def runTest(self):
            self.boundary("export", adapter=faulty_open)
    return unittest.TestSuite([MissingRecheckProbe()])


if __name__ == "__main__":
    unittest.main()
