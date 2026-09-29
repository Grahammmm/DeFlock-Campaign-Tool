import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from campaign_tool.records.catalog import (
    STAGE_FIELDS, build_catalog, capture_file, checked_path,
    digest_file, import_catalog, index_hashes, verify_blob,
)


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.database = self.root / 'intake.sqlite'
        self.blobs = self.root / 'blobs'
        self.blobs.mkdir(mode=0o700)
        self.digests = self.root / 'agencies'
        self.digests.mkdir()
        self.support = self.root / 'support'
        self.support.mkdir()
        self.queue = self.root / 'queue.jsonl'
        self.snapshot = self.root / 'snapshot.jsonl'
        self.output = self.root / 'catalog-output'
        self.hashes = [hashlib.sha256(v).hexdigest() for v in (b'alpha', b'beta')]
        for sha, value in zip(self.hashes, (b'alpha', b'beta')):
            (self.blobs / sha).write_bytes(value)
        db = sqlite3.connect(self.database)
        db.executescript('''
CREATE TABLE docs(sha TEXT PRIMARY KEY, bytes INTEGER, format TEXT,
 stage TEXT, first_seen TEXT, review_status TEXT);
CREATE TABLE occurrences(sha TEXT, path TEXT, parent TEXT,
 first_seen TEXT, last_seen TEXT);
CREATE TABLE preservations(sha TEXT, path TEXT, verified_at TEXT, bytes INTEGER);
CREATE TABLE scope_exclusions(path TEXT, sha TEXT, reason TEXT, at TEXT);
''')
        for sha, size in zip(self.hashes, (5, 4)):
            db.execute('INSERT INTO docs VALUES (?,?,?,?,?,?)',
                       (sha, size, 'txt', 'complete', '2026-01-01T00:00:00Z', 'pending'))
            db.execute('INSERT INTO occurrences VALUES (?,?,?,?,?)',
                       (sha, '/synthetic/' + sha, None, '2026-01-01', '2026-01-02'))
        db.execute('INSERT INTO occurrences VALUES (?,?,?,?,?)',
                   (self.hashes[0], '/synthetic/duplicate', None, '2026-01-01', '2026-01-03'))
        db.commit()
        db.close()
        self.queue_rows = [{'sha256': self.hashes[0], 'agency_hints': ['Synthetic Agency'],
                            'classification': ['agency_original']}]
        self.snapshot_rows = [{'sha256': sha, 'effective_role': 'agency_original',
                               'complete_substantive_original_level_digestion': 'full',
                               'publication_readiness': 'not_ready'} for sha in self.hashes]
        self.queue.write_text(''.join(json.dumps(row) + '\n' for row in self.queue_rows))
        self.snapshot.write_text(''.join(json.dumps(row) + '\n' for row in self.snapshot_rows))
        (self.support / 'receipt.json').write_text('{"synthetic":true}\n')
        self.active = patch('campaign_tool.records.catalog.active',
                            return_value=({sha: 0 for sha in self.hashes}, 'synthetic-run'))
        self.active.start()
        self.addCleanup(self.active.stop)

    def run_import(self):
        return import_catalog(self.database, self.queue, self.snapshot, self.digests,
                              self.output, support_root=self.support, blob_root=self.blobs)

    def build(self):
        db = sqlite3.connect(self.database)
        db.row_factory = sqlite3.Row
        try:
            return build_catalog(db, list(enumerate(self.queue_rows, 1)),
                                 list(enumerate(self.snapshot_rows, 1)), [], blob_root=self.blobs)
        finally:
            db.close()

    def test_all_database_identities_survive_shorter_queue(self):
        catalog, inventory, coverage, details = self.build()
        self.assertEqual({c['sha256'] for c in catalog['cards']}, set(self.hashes))
        self.assertEqual(len(inventory), 2)
        self.assertEqual(len(details), 2)
        self.assertTrue(catalog['join_gaps'])
        self.assertEqual(sum(c['occurrence_count'] for c in catalog['cards']), 3)

    def test_declarations_are_not_approvals_or_closures(self):
        catalog, _, _, _ = self.build()
        for card in catalog['cards']:
            self.assertEqual(card['snapshot_declarations'][STAGE_FIELDS[0]], 'full')
            self.assertFalse(card['review_receipts_revalidated'])
            self.assertFalse(card['publication_ready'])
            self.assertFalse(card['closed'])
            self.assertIsNone(card['dates']['document_date'])

    def test_import_is_private_and_source_unchanged(self):
        before = digest_file(self.database)
        result = self.run_import()
        self.assertEqual(digest_file(self.database), before)
        destination = self.output / result['snapshot_id']
        catalog = json.loads((destination / 'catalog.json').read_text())
        self.assertEqual(len(catalog['cards']), 2)
        self.assertEqual(len(list((destination / 'objects').iterdir())), 3)
        self.assertTrue((destination / 'board.html').is_file())
        for file in self.output.rglob('*'):
            self.assertEqual(file.stat().st_mode & 0o077, 0, str(file))

    def test_identical_import_reuses_snapshot(self):
        first = self.run_import()
        original = (self.output / first['snapshot_id'] / 'catalog.json').read_bytes()
        second = self.run_import()
        self.assertEqual(first['snapshot_id'], second['snapshot_id'])
        self.assertTrue(second['reused'])
        self.assertEqual(original, (self.output / first['snapshot_id'] / 'catalog.json').read_bytes())

    def test_preserved_support_is_exact_bytes(self):
        result = self.run_import()
        original = (self.support / 'receipt.json').read_bytes()
        sha = hashlib.sha256(original).hexdigest()
        self.assertEqual((self.output / result['snapshot_id'] / 'objects' / sha).read_bytes(), original)

    def test_duplicate_identity_is_rejected(self):
        rows = [(1, self.queue_rows[0]), (2, self.queue_rows[0])]
        with self.assertRaises(ValueError):
            index_hashes(rows, 'synthetic')

    def test_corrupt_and_missing_originals_are_not_verified(self):
        sha = self.hashes[0]
        self.assertEqual(verify_blob(self.blobs, sha, 5), 'verified')
        (self.blobs / sha).write_bytes(b'wrong')
        self.assertNotEqual(verify_blob(self.blobs, sha, 5), 'verified')
        (self.blobs / sha).unlink()
        self.assertNotEqual(verify_blob(self.blobs, sha, 5), 'verified')

    def test_symlink_input_is_rejected(self):
        link = self.root / 'linked-queue'
        link.symlink_to(self.queue)
        with self.assertRaises((ValueError, OSError)):
            checked_path(link)

    def test_world_accessible_output_is_rejected(self):
        self.output.mkdir(mode=0o755)
        self.output.chmod(0o755)
        with self.assertRaises((ValueError, PermissionError)):
            self.run_import()

    def test_capture_rejects_corrupt_existing_object(self):
        objects = self.root / 'objects'
        objects.mkdir()
        source = self.support / 'receipt.json'
        sha = digest_file(source)
        (objects / sha).write_bytes(b'incorrect')
        with self.assertRaises((ValueError, OSError)):
            capture_file(source, objects)


if __name__ == '__main__':
    unittest.main()
