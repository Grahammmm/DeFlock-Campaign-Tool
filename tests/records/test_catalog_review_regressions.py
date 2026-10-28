"""Synthetic regressions for independently reproduced catalog review findings."""
import json
import unittest

from tests.records import test_catalog as fixtures


class CatalogReviewRegressions(unittest.TestCase):
    setUp = fixtures.CatalogTests.setUp
    run_import = fixtures.CatalogTests.run_import

    def test_changed_preservation_result_cannot_reuse_stale_catalog(self):
        first = self.run_import()
        (self.blobs / self.hashes[0]).unlink()
        second = self.run_import()
        saved = json.loads((self.output / second['snapshot_id'] / 'catalog.json').read_text())
        card = next(card for card in saved['cards'] if card['sha256'] == self.hashes[0])
        self.assertEqual(card['preservation'], 'missing',
                         'Persisted board must reflect the current verification result')
        self.assertEqual(saved['summary'], second['summary'],
                         'Returned and persisted summaries must agree')

    def test_reuse_rejects_corrupt_preserved_input_object(self):
        first = self.run_import()
        from campaign_tool.records.catalog import digest_file
        object_path = self.output / first['snapshot_id'] / 'objects' / digest_file(self.queue)
        object_path.write_bytes(b'synthetic damaged preserved input')
        with self.assertRaises((ValueError, OSError)):
            self.run_import()

    def test_output_cannot_contain_source_queue(self):
        self.output.mkdir(mode=0o700)
        self.queue = self.output / 'CURRENT.json'
        original = ''.join(json.dumps(row) + '\n' for row in self.queue_rows).encode()
        self.queue.write_bytes(original)
        with self.assertRaises((ValueError, OSError)):
            self.run_import()
        self.assertEqual(self.queue.read_bytes(), original,
                         'Overlap rejection must occur before source mutation')

    def test_board_does_not_call_all_stored_identities_originals(self):
        self.snapshot_rows[0]['effective_role'] = 'project_artifact'
        self.snapshot.write_text(''.join(json.dumps(row) + '\n' for row in self.snapshot_rows))
        result = self.run_import()
        board = (self.output / result['snapshot_id'] / 'board.html').read_text().lower()
        self.assertNotIn('unique originals', board,
                         'The total includes project artifacts, not just agency originals')


if __name__ == '__main__':
    unittest.main()
