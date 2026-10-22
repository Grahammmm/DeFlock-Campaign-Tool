"""M2: preserve every gated slot in the denominator, never bless unknowns."""
import argparse
from pathlib import Path
import sqlite3
import tempfile
import unittest

from campaign_tool.records import run_stages
from campaign_tool.records.ledger import store
from campaign_tool.records.ledger.migrations import v001


class DigestGateAccountingTests(unittest.TestCase):
    def call(self, statuses, *, proposals=0, extra=()):
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp) / 'processing-v1'
            proc.mkdir()
            with sqlite3.connect(proc / 'ledger.sqlite') as db:
                # Actual stage vocabulary and query shape; deliberately nullable status
                # also exercises old/damaged schemas without hiding a denominator row.
                db.execute('CREATE TABLE stage_state(original_sha256 TEXT, stage TEXT, status TEXT)')
                db.execute('CREATE TABLE proposals(id TEXT)')
                for i, status in enumerate(statuses):
                    for stage in run_stages.GATED:
                        db.execute('INSERT INTO stage_state VALUES(?,?,?)', ('%064x' % i, stage, status))
                db.executemany('INSERT INTO stage_state VALUES(?,?,?)', extra)
                db.executemany('INSERT INTO proposals VALUES(?)', [(str(i),) for i in range(proposals)])
            return run_stages.stage_digest_gates(argparse.Namespace(project=tmp))

    def test_done_pending_and_empty_positive_controls(self):
        for statuses in ([], ['done'], ['done', 'pending']):
            with self.subTest(statuses=statuses):
                line, code = self.call(statuses, extra=[('x', 'extract', 'unexpected')])
                self.assertEqual((line['status'], code, line['error_code']), ('ok', 0, None))
                self.assertEqual(line['counts']['input'], len(statuses) * 5)
                self.assertEqual(line['counts']['output'], statuses.count('done') * 5)
                self.assertEqual(line['counts']['failed'], 0)
                self.assertEqual(line['counts']['skipped'], statuses.count('pending') * 5)

    def test_inapplicable_only_and_mixed_are_known_skipped_slots(self):
        for statuses in (['inapplicable'], ['done', 'pending', 'inapplicable']):
            with self.subTest(statuses=statuses):
                line, code = self.call(statuses)
                self.assertEqual((line['status'], code, line['error_code']), ('ok', 0, None))
                self.assertEqual(line['counts'], {'input': len(statuses) * 5,
                                 'output': statuses.count('done') * 5, 'failed': 0,
                                 'skipped': (statuses.count('pending') + 1) * 5})
                self.assertEqual(line.get('needs_g', []), [])

    def test_every_schema_status_has_an_explicit_non_unknown_classification(self):
        expected = {
            'done': ('ok', None, 5, 0, 0),
            'pending': ('ok', None, 0, 0, 5),
            'inapplicable': ('ok', None, 0, 0, 5),
            'in_progress': ('warn', 'GATE_IN_PROGRESS', 0, 0, 5),
            'blocked': ('warn', 'GATE_REVIEW_REQUIRED', 0, 5, 0),
        }
        # Schema additions require an intentional classification, not a silent default.
        self.assertEqual(set(expected), set(store.STATUSES))
        for status in store.STATUSES:
            with self.subTest(status=status):
                line, code = self.call([status])
                self.assertEqual(code, 0)
                self.assertEqual((line['status'], line['error_code'],
                                  line['counts']['output'], line['counts']['failed'],
                                  line['counts']['skipped']), expected[status])
                self.assertEqual(line['counts']['input'], 5)
                self.assertNotIn('GATE_UNKNOWN_STATUS', [n['code'] for n in line.get('needs_g', [])])

    def test_inapplicable_on_actual_v001_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp) / 'processing-v1'
            proc.mkdir()
            with sqlite3.connect(proc / 'ledger.sqlite') as db:
                db.execute('PRAGMA foreign_keys=ON')
                db.executescript(v001.SQL)
                db.execute("INSERT INTO originals(sha256,bytes,role,scope,preservation_status,provenance_json) "
                           "VALUES(?,0,'document','included','pending','{}')", ('a' * 64,))
                db.execute("INSERT INTO runs(run_id,kind,started_at,engine_version,status,summary) "
                           "VALUES('synthetic','test','2026-01-01T00:00:00+00:00','test','ok','{}')")
                for stage in run_stages.GATED:
                    db.execute("INSERT INTO stage_state(original_sha256,stage,status,owner,updated_at,run_id,reason) "
                               "VALUES(?,?,'inapplicable','test','2026-01-01T00:00:00+00:00','synthetic','empty document')",
                               ('a' * 64, stage))
                self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
            before = (proc / 'ledger.sqlite').read_bytes()
            line, code = run_stages.stage_digest_gates(argparse.Namespace(project=tmp))
            self.assertEqual((line['status'], code, line['error_code']), ('ok', 0, None))
            self.assertEqual(line['counts'], {'input': 5, 'output': 0, 'failed': 0, 'skipped': 5})
            self.assertEqual(line.get('needs_g', []), [])
            self.assertEqual((proc / 'ledger.sqlite').read_bytes(), before)

    def test_in_progress_warns_even_without_other_slots(self):
        for statuses in (['in_progress'], ['done', 'pending', 'in_progress']):
            with self.subTest(statuses=statuses):
                line, code = self.call(statuses)
                self.assertEqual((line['status'], code, line['error_code']), ('warn', 0, 'GATE_IN_PROGRESS'))
                self.assertEqual(line['counts'], {'input': len(statuses) * 5,
                                 'output': statuses.count('done') * 5, 'failed': 0,
                                 'skipped': (statuses.count('pending') + 1) * 5})
                self.assertIn({'code': 'GATE_IN_PROGRESS', 'severity': 'info', 'count': 5}, line.get('needs_g', []))

    def test_unknown_and_null_statuses_fail_closed_and_remain_counted(self):
        for status in ('future_status', '', None):
            with self.subTest(status=status):
                line, code = self.call(['done', status, 'in_progress'])
                self.assertEqual((line['status'], code, line['error_code']), ('warn', 0, 'GATE_UNKNOWN_STATUS'))
                self.assertEqual(line['counts'], {'input': 15, 'output': 5, 'failed': 5, 'skipped': 5})
                self.assertIn({'code': 'GATE_UNKNOWN_STATUS', 'severity': 'action', 'count': 5}, line.get('needs_g', []))

    def test_review_required_keeps_priority_without_losing_other_states(self):
        for statuses, proposals in ((['done', 'in_progress', 'unexpected'], 1),
                                    (['blocked', 'in_progress', 'unexpected'], 0)):
            with self.subTest(statuses=statuses):
                line, code = self.call(statuses, proposals=proposals)
                self.assertEqual((line['status'], code, line['error_code']), ('warn', 0, 'GATE_REVIEW_REQUIRED'))
                self.assertEqual(line.get('review_required', 0), proposals)
                self.assertEqual(line['counts']['input'], 15)
                self.assertEqual(sum(line['counts'][k] for k in ('output', 'failed', 'skipped')), 15)
                self.assertIn('GATE_UNKNOWN_STATUS', [n['code'] for n in line.get('needs_g', [])])


if __name__ == '__main__':
    unittest.main()
