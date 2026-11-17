"""RF1911-4: retained ledger/index paths are filenames, never SQLite URI syntax. Synthetic data only."""
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool.records import retained_units
from tests.records.test_fc2_retained_scope import build_ledger, tree_state

NAMES = ["plain dir", "has#frag", "has?mode=rw", "pct%23lit", "pct%3Flit", "uni\u00e9\u4e2d%", "a#b?c%d e"]


class UriPathCase(unittest.TestCase):
    def private(self, name):
        holder = tempfile.TemporaryDirectory(prefix="rf1911-4-", dir=os.path.realpath(tempfile.gettempdir()))
        self.addCleanup(holder.cleanup)
        base = Path(holder.name)
        os.chmod(base, 0o700)
        d = base / name
        d.mkdir(mode=0o700)
        os.chmod(d, 0o700)
        return base, d


class ReaderTests(UriPathCase):
    def test_reader_accepts_literal_uri_characters(self):
        for name in NAMES:
            with self.subTest(name=name):
                _, d = self.private(name)
                db, root, snap, declared = build_ledger(d, [("uri:a", 2)])
                opened = []
                real = sqlite3.connect

                def spy(target, *a, **k):
                    opened.append(target)
                    return real(target, *a, **k)

                with mock.patch.object(retained_units.sqlite3, "connect", spy):
                    out = retained_units.read_current_units(str(db), str(root), snap, declared)
                self.assertEqual(out["unit_count"], 2)
                self.assertEqual(out["snapshot_sha256"], snap)
                self.assertEqual(opened, [db.as_uri() + "?mode=ro"] * 2)
                self.assertNotIn("?mode=ro?", "".join(opened))

    def test_percent_sequences_are_literal_not_decoded(self):
        base, d = self.private("pct%23lit")
        decoy = base / "pct#lit"
        decoy.mkdir(mode=0o700)
        db, root, snap, declared = build_ledger(d, [("uri:real", 1)])
        # A decoy at the decoded location holds a ledger with a different pin.
        ddb, _, dsnap, _ = build_ledger(decoy, [("uri:decoy", 3)])
        self.assertNotEqual(snap, dsnap)
        out = retained_units.read_current_units(str(db), str(root), snap, declared)
        self.assertEqual(out["unit_count"], 1)
        before = tree_state(decoy)
        with self.assertRaises(retained_units.RetainedUnitError) as cm:
            retained_units.read_current_units(str(db), str(root), dsnap, declared)
        self.assertEqual(cm.exception.code, "ledger_snapshot_stale")
        self.assertEqual(tree_state(decoy), before)

    def test_missing_literal_path_never_creates_decoy(self):
        for name in ("frag#x", "q?mode=rwc"):
            with self.subTest(name=name):
                base, d = self.private("dir")
                missing = d / (name + ".sqlite")
                before = sorted(os.listdir(d))
                with self.assertRaises(retained_units.RetainedUnitError) as cm:
                    retained_units.read_current_units(str(missing), str(d), "0" * 64, [])
                self.assertEqual(cm.exception.code, "database_unavailable")
                self.assertEqual(sorted(os.listdir(d)), before)
                # Even if the file vanishes after validation, readonly open must not create anything.
                with self.assertRaises(sqlite3.OperationalError):
                    sqlite3.connect(retained_units._ro_uri(missing), uri=True)
                self.assertEqual(sorted(os.listdir(d)), before)
                self.assertEqual(sorted(os.listdir(base)), ["dir"])

    def test_pin_and_permission_failures_remain_closed(self):
        _, d = self.private("has#frag?x")
        db, root, snap, declared = build_ledger(d, [("uri:a", 1)])
        with self.assertRaises(retained_units.RetainedUnitError) as cm:
            retained_units.read_current_units(str(db), str(root), "f" * 64, declared)
        self.assertEqual(cm.exception.code, "ledger_snapshot_stale")
        os.chmod(db, 0o644)
        with self.assertRaises(retained_units.RetainedUnitError) as cm:
            retained_units.read_current_units(str(db), str(root), snap, declared)
        self.assertEqual(cm.exception.code, "database_permissions_or_size")


class SearchTests(UriPathCase):
    def test_search_accepts_literal_uri_characters(self):
        for name in NAMES:
            with self.subTest(name=name):
                base, d = self.private(name)
                db, root, snap, declared = build_ledger(d, [("uri:s", 2)])
                read = retained_units.read_current_units(str(db), str(root), snap, declared)
                idx_dir = d / "idx#?%"
                idx_dir.mkdir(mode=0o700)
                built = retained_units.build_search_index(str(idx_dir), read)
                hits = retained_units.search_index(built["path"], snap, "synthetic",
                                                   expected_content_sha256=built["content_sha256"])
                self.assertEqual(len(hits), 2)
                self.assertEqual(sorted(os.listdir(idx_dir)), ["retained-search-" + snap + ".sqlite"])

    def test_search_pin_and_content_failures_remain_closed(self):
        _, d = self.private("has#frag")
        db, root, snap, declared = build_ledger(d, [("uri:s", 1)])
        read = retained_units.read_current_units(str(db), str(root), snap, declared)
        idx_dir = d / "idx?x"
        idx_dir.mkdir(mode=0o700)
        built = retained_units.build_search_index(str(idx_dir), read)
        with self.assertRaises(retained_units.RetainedUnitError) as cm:
            retained_units.search_index(built["path"], snap, "synthetic", expected_content_sha256="e" * 64)
        self.assertEqual(cm.exception.code, "index_content_pin_mismatch")
        gone = idx_dir / ("retained-search-" + snap + ".sqlite")
        os.chmod(gone, 0o644)
        with self.assertRaises(retained_units.RetainedUnitError) as cm:
            retained_units.search_index(str(gone), snap, "synthetic", expected_content_sha256=built["content_sha256"])
        self.assertEqual(cm.exception.code, "index_permissions_or_size")


if __name__ == "__main__":
    unittest.main()
