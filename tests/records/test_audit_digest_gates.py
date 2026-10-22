"""M2: preserve every gated slot in the denominator, never bless unknowns."""
import argparse
from pathlib import Path
import sqlite3
import tempfile
import unittest

from campaign_tool.records import run_stages


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

    def test_in_progress_warns_even_without_other_slots(self):
        for statuses in (['in_progress'], ['done', 'pending', 'in_progress']):
            with self.subTest(statuses=statuses):
                line, code = self.call(statuses)
                self.assertEqual((line['status'], code, line['error_code']), ('warn', 0, 'GATE_IN_PROGRESS'))
                self.assertEqual(line['counts'], {'input': len(statuses) * 5,
                                 'output': statuses.count('done') * 5, 'failed': 0,
                                 'skipped': (statuses.count('pending') + 1) * 5})
                self.assertIn({'code': 'GATE_IN_PROGRESS', 'severity': 'info', 'count': 5}, line['needs_g'])

    def test_unknown_and_null_statuses_fail_closed_and_remain_counted(self):
        for status in ('future_status', '', None):
            with self.subTest(status=status):
                line, code = self.call(['done', status, 'in_progress'])
                self.assertEqual((line['status'], code, line['error_code']), ('warn', 0, 'GATE_UNKNOWN_STATUS'))
                self.assertEqual(line['counts'], {'input': 15, 'output': 5, 'failed': 5, 'skipped': 5})
                self.assertIn({'code': 'GATE_UNKNOWN_STATUS', 'severity': 'action', 'count': 5}, line['needs_g'])

    def test_review_required_keeps_priority_without_losing_other_states(self):
        for statuses, proposals in ((['done', 'in_progress', 'unexpected'], 1),
                                    (['blocked', 'in_progress', 'unexpected'], 0)):
            with self.subTest(statuses=statuses):
                line, code = self.call(statuses, proposals=proposals)
                self.assertEqual((line['status'], code, line['error_code']), ('warn', 0, 'GATE_REVIEW_REQUIRED'))
                self.assertEqual(line.get('review_required', 0), proposals)
                self.assertEqual(line['counts']['input'], 15)
                self.assertEqual(sum(line['counts'][k] for k in ('output', 'failed', 'skipped')), 15)
                self.assertIn('GATE_UNKNOWN_STATUS', [n['code'] for n in line['needs_g']])


if __name__ == '__main__':
    unittest.main()
