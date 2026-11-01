"""Synthetic C02 controls: unavailable sources remain denominator gaps."""
import contextlib
import hashlib
import io
import json
import os
import pathlib
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from campaign_tool.records.intake import folder
from contextlib import closing


class InventoryDenominatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = pathlib.Path(self.tmp.name).resolve()
        self.source = self.base / 'received'
        self.source.mkdir()
        self.out = self.base / 'output'
        self.out.mkdir()
        self.db = sqlite3.connect(self.out / 'intake.sqlite')
        self.db.row_factory = sqlite3.Row
        self.db.executescript(folder.SCHEMA)
        self.addCleanup(self.db.close)
        self.config = folder.load_config(roots=[str(self.source)], output=str(self.out))
        self.a = self.source / 'a.txt'
        self.b = self.source / 'b.txt'
        self.a.write_text('first synthetic source')
        self.b.write_text('second synthetic source')
        self.scan()
        self.initial = set(folder.active(self.db)[0])
        self.assertEqual(len(self.initial), 2)
        # Extraction completion must not conceal inventory gaps.
        self.db.execute("UPDATE docs SET stage='complete',digest=?", (folder.js({'issues': []}),))
        self.db.commit()

    def scan(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return folder.inventory(self.db, self.out, [str(self.source)], self.config)

    def coverage(self):
        folder.report(self.db, self.out, self.config)
        return json.loads((self.out / 'coverage.json').read_text())

    def assert_gap(self, reason):
        self.assertEqual(set(folder.active(self.db)[0]), self.initial)
        report = self.coverage()
        self.assertFalse(report['inventory_complete'])
        self.assertIn(reason, report['issues'])
        self.assertTrue(report['inventory_gaps'])
        for sha in self.initial:
            self.assertEqual(hashlib.sha256((self.out / 'blobs' / sha).read_bytes()).hexdigest(), sha)

    def test_unreadable_file_retains_prior_source_and_exact_gap_path(self):
        real = folder.secure_open
        def unreadable(path):
            if str(path) == str(self.b):
                raise PermissionError('synthetic read failure')
            return real(path)
        with patch.object(folder, 'secure_open', side_effect=unreadable):
            self.scan()
        self.assert_gap('scan_error')
        with patch.object(folder, 'secure_open', side_effect=unreadable):
            self.scan()
        self.assert_gap('scan_error')
        self.assertEqual(self.coverage()['inventory_gaps'][0]['path'], str(self.b))
        self.assertEqual(self.db.execute('SELECT count(*) FROM occurrences').fetchone()[0], 2)
        self.scan()
        self.assertTrue(self.coverage()['inventory_complete'])
        self.assertEqual(self.coverage()['inventory_gaps'], [])

    def test_unreadable_directory_keeps_unseen_descendants(self):
        def unreadable(root, **kwargs):
            kwargs['onerror'](PermissionError(13, 'synthetic directory denial', str(self.source)))
            return iter(())
        with patch.object(folder.os, 'walk', side_effect=unreadable):
            self.scan()
        self.assert_gap('scan_error')

    def test_unavailable_root_keeps_prior_sources(self):
        moved = self.base / 'offline'
        self.source.rename(moved)
        self.scan()
        self.assert_gap('root_error')
        moved.rename(self.source)
        self.scan()
        self.assertTrue(self.coverage()['inventory_complete'])

    def test_interrupted_scan_survives_reopen_with_prior_denominator(self):
        def interrupted(*args, **kwargs):
            raise KeyboardInterrupt('synthetic interruption')
        with patch.object(folder.os, 'walk', side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.scan()
        # Publication of the conservative snapshot is committed before traversal.
        with closing(sqlite3.connect(self.out / 'intake.sqlite')) as reopened, reopened:
            reopened.row_factory = sqlite3.Row
            self.assertEqual(set(folder.active(reopened)[0]), self.initial)
        self.assert_gap('inventory_interrupted_or_running')

    def test_complete_scan_can_retire_deleted_source_without_deleting_history(self):
        self.b.unlink()
        self.scan()
        self.assertEqual(len(folder.active(self.db)[0]), 1)
        self.assertTrue(self.coverage()['inventory_complete'])
        self.assertEqual(self.coverage()['inventory_gaps'], [])
        self.assertEqual(self.db.execute('SELECT count(*) FROM occurrences').fetchone()[0], 2)
        self.assertEqual(self.db.execute('SELECT count(*) FROM docs').fetchone()[0], 2)
        self.assertEqual(len(list((self.out / 'blobs').iterdir())), 2)

    def test_failed_first_scan_exposes_unknown_denominator(self):
        self.config['roots'] = [str(self.base / 'absent')]
        with contextlib.redirect_stdout(io.StringIO()):
            folder.inventory(self.db, self.out, self.config['roots'], self.config)
        report = self.coverage()
        self.assertEqual(report['active_unique_documents'], 0)
        self.assertFalse(report['inventory_complete'])
        self.assertEqual(report['inventory_gaps'][0]['reason'], 'root_error')

    def test_changed_file_replaces_same_occurrence_on_partial_scan(self):
        old_a = hashlib.sha256(self.a.read_bytes()).hexdigest()
        self.a.write_text('changed synthetic source')
        real = folder.secure_open
        def unreadable(path):
            if str(path) == str(self.b):
                raise PermissionError('synthetic read failure')
            return real(path)
        with patch.object(folder, 'secure_open', side_effect=unreadable):
            self.scan()
        self.assertEqual(len(folder.active(self.db)[0]), 2)
        self.assertNotIn(old_a, folder.active(self.db)[0])
        self.assertTrue((self.out / 'blobs' / old_a).exists())
        self.assertEqual(self.db.execute('SELECT count(*) FROM occurrences').fetchone()[0], 3)
