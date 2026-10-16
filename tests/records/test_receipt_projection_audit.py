"""Synthetic regressions for audit C14–16."""
import json
import unittest
from unittest.mock import patch
from campaign_tool.records.ledger import receipt_projection as rp, store
from tests.records import test_ledger_receipts as fixtures

class ProjectionAuditTests(unittest.TestCase):
    setUp = fixtures.ReceiptProjectionTests.setUp
    tearDown = fixtures.ReceiptProjectionTests.tearDown
    write = fixtures.ReceiptProjectionTests.write
    run_project = fixtures.ReceiptProjectionTests.run_project
    query = fixtures.ReceiptProjectionTests.query

    def test_resume_after_resolution(self):
        missing = rp.digest(b'later fictional original')
        self.write([dict(self.row, original=missing), self.row])
        self.assertEqual(self.run_project(batch_size=1)['remaining'], 1)
        before = self.query('SELECT * FROM rp_items')
        gaps = self.query('SELECT * FROM rp_gaps')
        with store.ledger(self.db) as con:
            con.execute('INSERT INTO originals VALUES(?,?,?,?,?,?,?,?,?,?)',
                        (missing, 23, 'text/plain', None, 'original', 'in_scope', None, 'captured', None, '{}'))
            con.commit()
        result = self.run_project(batch_size=1)
        self.assertEqual(result['processed'], 2)
        self.assertEqual(result['binding_verified'], 1)
        self.assertEqual(self.query('SELECT * FROM rp_items ORDER BY line')[0], before[0])
        self.assertEqual(self.query('SELECT * FROM rp_gaps'), gaps)
        self.assertEqual(self.run_project()['added'], 0)
        # A new capture can bind the resolved original without replacing history.
        self.write([dict(self.row, original=missing)])
        self.assertEqual(self.run_project()['binding_verified'], 1)
        self.assertEqual(self.query('SELECT * FROM rp_gaps'), gaps)

    def test_malformed_scalars_continue(self):
        raw = (b'{"reviewer":1e400}\n{"reviewer":"\\ud800"}\n'
               b'{"ignored":{"\\udfff":0}}\n' + json.dumps(self.row).encode() + b'\n')
        self.write([], raw)
        result = self.run_project()
        self.assertEqual(result['processed'], 4)
        self.assertEqual(result['binding_verified'], 1)
        self.assertEqual(result['gaps'], 3)
        self.assertEqual(self.query('SELECT reason FROM rp_gaps'), [('malformed_record',)] * 3)
        self.assertEqual(b''.join(r[0] for r in self.query('SELECT raw FROM rp_items ORDER BY line')), raw)
        self.assertEqual(self.run_project()['added'], 0)

    def test_capacity_deduplicates_first(self):
        other = b'new fictional artifact'
        (self.root / 'other.json').write_bytes(other)
        self.write([self.row, self.row, dict(self.row, path='other.json', receipt=rp.digest(other))])
        with patch.object(rp, 'MAX_JOB_ARTIFACTS', len(self.art)):
            self.run_project(batch_size=1)
            result = self.run_project()
            self.assertEqual(result['binding_verified'], 2)
            self.assertEqual(result['gaps'], 1)
            self.assertEqual(self.query('SELECT count(*),sum(size) FROM rp_artifacts'), [(1, len(self.art))])
            self.assertEqual(self.query('SELECT capture_error FROM rp_items ORDER BY line'), [(None,), (None,), ('oversize',)])
            self.assertEqual(self.run_project()['added'], 0)

    def test_33_bindings_store_32_artifacts(self):
        rows = []
        for number in range(32):
            body = bytes([number]) * 64
            path = f'artifact-{number}.json'
            (self.root / path).write_bytes(body)
            rows.append(dict(self.row, path=path, receipt=rp.digest(body)))
        self.write(rows + [rows[0]])
        with patch.object(rp, 'MAX_JOB_ARTIFACTS', 32 * 64):
            result = self.run_project()
            self.assertEqual(result['binding_verified'], 33)
            self.assertEqual(result['gaps'], 0)
            self.assertEqual(self.query('SELECT count(*),sum(size) FROM rp_artifacts'), [(32, 32 * 64)])

    def test_legacy_unmapped_invalid_scalars_replay_without_rewriting(self):
        # The old planner copied mapped fields only. These invalid, unmapped
        # scalars therefore never entered its stored canonical plan.
        expected_plan = rp.plan(json.dumps(self.row).encode(), rp.canonical({
            'kind': 'reviews', 'fields': self.fields,
            'declarations': {k: k for k in ('reviewer', 'coverage', 'holds', 'supersedes', 'challenges')},
            'artifact_paths': {}}))
        old_raw = [json.dumps(self.row).encode()[:-1] + suffix + b'\n'
                   for suffix in (b',"ignored":1e400}', b',"ignored":"\\ud800"}')]
        self.write([], b''.join(old_raw + [json.dumps(self.row).encode() + b'\n']))
        current_plan = rp.plan
        def historical_plan(raw, mapping):
            return expected_plan if raw in old_raw else current_plan(raw, mapping)
        with patch.object(rp, 'plan', side_effect=historical_plan):
            self.assertEqual(self.run_project(batch_size=2)['binding_verified'], 2)
        before = {table: self.query(f'SELECT * FROM {table}') for table in rp.migration.KEYS}
        result = self.run_project()
        self.assertEqual(result['processed'], 3)
        self.assertEqual(result['binding_verified'], 3)
        self.assertEqual(result['gaps'], 0)
        for table, rows in before.items():
            self.assertEqual(self.query(f'SELECT * FROM {table}')[:len(rows)], rows)
        self.assertEqual(self.run_project()['added'], 0)
        # The same invalid source in a new manifest gets a current explicit hold.
        self.write([], old_raw[0])
        self.assertEqual(self.run_project()['binding_verified'], 0)
        self.assertEqual(self.query('SELECT reason FROM rp_gaps'), [('malformed_record',)])
