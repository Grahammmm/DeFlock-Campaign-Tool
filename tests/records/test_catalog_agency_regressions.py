"""Synthetic guard for agency placeholders; never interpret them as attribution."""
import json
import unittest
from tests.records import test_catalog as fixtures


class AgencyPlaceholderRegression(unittest.TestCase):
    setUp = fixtures.CatalogTests.setUp
    run_import = fixtures.CatalogTests.run_import

    def test_literal_unassigned_is_flagged_as_unknown(self):
        self.queue_rows[0]['agency_hints'] = ['UNASSIGNED']
        self.queue.write_text(''.join(json.dumps(row) + '\n' for row in self.queue_rows))
        result = self.run_import()
        catalog = json.loads((self.output / result['snapshot_id'] / 'catalog.json').read_text())
        card = next(c for c in catalog['cards'] if c['sha256'] == self.hashes[0])
        self.assertIn('agency_unassigned', card['flags'])

    def test_named_agency_hint_is_not_flagged_as_empty(self):
        result = self.run_import()
        catalog = json.loads((self.output / result['snapshot_id'] / 'catalog.json').read_text())
        card = next(c for c in catalog['cards'] if c['sha256'] == self.hashes[0])
        self.assertNotIn('agency_unassigned', card['flags'])
        self.assertFalse(card['review_receipts_revalidated'])


if __name__ == '__main__':
    unittest.main()

class AgencyStatusCases(unittest.TestCase):
    setUp = fixtures.CatalogTests.setUp
    run_import = fixtures.CatalogTests.run_import

    def test_placeholder_variants_and_named_hints(self):
        cases = [([], 'unassigned'), (['UNASSIGNED'], 'unassigned'),
                 ([' unAssigned ', ' '], 'unassigned'),
                 (['UNASSIGNED', 'Synthetic Agency'], 'hint_present')]
        for hints, expected in cases:
            with self.subTest(hints=hints):
                self.queue_rows[0]['agency_hints'] = hints
                self.queue.write_text(''.join(json.dumps(row) + '\n' for row in self.queue_rows))
                result = self.run_import()
                catalog = json.loads((self.output / result['snapshot_id'] / 'catalog.json').read_text())
                card = next(c for c in catalog['cards'] if c['sha256'] == self.hashes[0])
                self.assertEqual(card['agency_status'], expected)
                self.assertEqual(card['agency_hints'], hints)
                self.assertEqual('agency_unassigned' in card['flags'], expected == 'unassigned')
                self.assertEqual(sum(catalog['summary']['agency_attribution_counts'].values()), 2)
                self.assertFalse(card['publication_ready'])

    def test_excluded_identity_is_not_an_agency_assignment_gap(self):
        import sqlite3
        with sqlite3.connect(self.database) as db:
            db.execute('UPDATE docs SET stage=? WHERE sha=?', ('out_of_scope', self.hashes[0]))
        result = self.run_import()
        catalog = json.loads((self.output / result['snapshot_id'] / 'catalog.json').read_text())
        card = next(c for c in catalog['cards'] if c['sha256'] == self.hashes[0])
        self.assertEqual(card['agency_status'], 'scope_excluded')
        self.assertEqual(card['agency_hints'], [])
        self.assertNotIn('agency_unassigned', card['flags'])
        self.assertEqual(card['preservation'], 'scope_excluded_not_opened')
