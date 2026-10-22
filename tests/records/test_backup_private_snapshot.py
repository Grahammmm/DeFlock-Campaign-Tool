"""M6: synthetic WAL backup never opens the source via SQLite."""
from contextlib import closing
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch
from tests import test_records_backup_v2 as fixtures
from campaign_tool.records.recovery import backup as v1, backup_v2 as v2


class PrivateWalTests(unittest.TestCase):
    def setUp(self):
        self.h = fixtures.RecoveryV2('runTest')
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.profile = self.h.quiescent_wal_profile()

    def test_sqlite_opens_private_copy_and_preserves_source_fingerprints(self):
        before = v2._inventory(self.h.source)
        original = v2.snapshot_ledger
        calls = []
        def root_like_open(source, destination):
            calls.append(Path(source))
            # Model the ownership/ctime side effect documented for root SQLite.
            wal = Path(str(source) + '-wal')
            wal.chmod(wal.stat().st_mode)
            return original(source, destination)
        with patch.object(v2, 'snapshot_ledger', side_effect=root_like_open):
            try:
                self.h.backup(profile=self.profile)
            except v1.RecoveryError as error:
                self.fail('unchanged quiescent WAL source must back up: ' + str(error))
        self.assertEqual(before, v2._inventory(self.h.source))
        self.assertEqual(len(calls), 1)
        self.assertNotEqual(calls[0], self.h.intake)
        with closing(sqlite3.connect(self.h.base / 'backup/payload/intake')) as con:
            self.assertIn(('wal-committed',), con.execute('SELECT source FROM occurrences').fetchall())

    def test_real_source_metadata_change_still_refuses_completion(self):
        original = v2.snapshot_ledger
        def changed(source, destination):
            result = original(source, destination)
            self.h.blob.chmod(0o600)
            return result
        with patch.object(v2, 'snapshot_ledger', side_effect=changed):
            with self.assertRaisesRegex(v1.RecoveryError, 'scope_changed_during_backup'):
                self.h.backup(profile=self.profile)
        self.assertFalse((self.h.base / 'backup/manifest.json').exists())
