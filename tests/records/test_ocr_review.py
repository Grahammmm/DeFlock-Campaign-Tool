"""Synthetic-only D-D OCR review acceptance."""
import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool.records import native_extract as ne, run_stages
from campaign_tool.records.ledger.migrations import v001


class OCRReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        processing = self.project / 'processing-v1'
        processing.mkdir()
        self.ledger = processing / 'ledger.sqlite'
        with contextlib.closing(sqlite3.connect(self.ledger)) as db:
            db.executescript(v001.SQL)
            db.execute('INSERT INTO originals(sha256, bytes, role, scope, preservation_status, provenance_json) '
                       'VALUES(?, 0, ?, ?, ?, ?)',
                       ('a' * 64, 'record', 'synthetic', 'preserved', '{}'))
            for page, confidence in enumerate((91.0, 80.0, 79.99, None, 99.0), 1):
                db.execute('INSERT INTO page_state(original_sha256,page_no,method,method_version,'
                           'derivative_sha256,confidence,needs_visual_review,status,reason) '
                           'VALUES(?,?,?,?,?,?,?,?,?)',
                           ('a' * 64, page, 'ocr', 'v1', 'b' * 64, confidence,
                            int(page != 5), 'ok', 'synthetic private text'))
            db.commit()
        self.original = self.ledger.read_bytes()
        self.reviews = self.project / 'ocr-review-v1' / 'reviews.jsonl'

    def stage(self, expected_code=0):
        summary = dict(outcomes=dict.fromkeys(ne.OUTCOMES, 0), new_units=0,
                       new_pages=0, attempted=0, unexpected_failures=0)
        with mock.patch.object(ne, 'run', return_value=summary), contextlib.redirect_stdout(io.StringIO()) as out:
            code = run_stages.main(['extract', '--project', str(self.project), '--timeout', '60', '--run-id', 'test'])
        self.assertEqual(code, expected_code)
        return json.loads(out.getvalue())

    def cli(self, *args):
        from campaign_tool.records import ocr_review as review
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = review.main([args[0], '--project', str(self.project), *args[1:]])
        return code, out.getvalue()

    def test_export_boundaries_and_privacy(self):
        from campaign_tool.records import ocr_review as review
        out = self.project / 'queue.json'
        self.assertEqual(self.cli('export', '--out', str(out))[0], 0)
        data = json.loads(out.read_bytes())
        self.assertEqual(data['schema'], 'ocr-review-queue-v1')
        self.assertEqual(data['counts'], dict(pending_spot_check=2, pending_hold=2,
                                              reviewed_confirmed=0, reviewed_rejected=0))
        self.assertEqual(data['rows'], [dict(original_sha256='a' * 64, page_no=n,
                                           bucket='spot_check' if n < 3 else 'hold')
                                        for n in range(1, 5)])
        for forbidden in (b'confidence', b'synthetic', str(self.project).encode(), b'.pdf', b'text'):
            self.assertNotIn(forbidden, out.read_bytes())
        self.assertEqual(out.stat().st_mode & 0o777, 0o600)
        self.assertEqual([review.bucket(x) for x in (91.0, 80.0, 79.99, None)],
                         ['spot_check', 'spot_check', 'hold', 'hold'])
        self.assertEqual(self.ledger.read_bytes(), self.original)

    def mark(self, page, decision='confirmed', label='human-reviewer'):
        return self.cli('mark', '--original-sha256', 'a' * 64, '--page', str(page),
                        '--decision', decision, '--reviewer-label', label)

    def page(self, page):
        with run_stages.ledger_ro(self.ledger.parent) as db:
            db.row_factory = sqlite3.Row
            return dict(db.execute('SELECT * FROM page_state WHERE page_no=?', (page,)).fetchone())

    def test_mark_and_pure_use_policy(self):
        from campaign_tool.records import ocr_review as review
        self.assertEqual(review.page_use_allowed(self.page(3), self.reviews),
                         (False, 'review_required_lt80'))
        self.assertEqual(review.page_use_allowed(self.page(4), self.reviews),
                         (False, 'review_required_lt80'))
        self.assertEqual(review.page_use_allowed(self.page(2), self.reviews), (True, 'read_with_flag'))
        self.assertEqual(self.mark(3)[0], 0)
        self.assertEqual(self.stage()['review_required'], 3)
        self.assertEqual(review.page_use_allowed(self.page(3), self.reviews), (True, 'review_confirmed'))
        self.assertEqual(self.mark(1, 'rejected')[0], 0)
        self.assertEqual(review.page_use_allowed(self.page(1), self.reviews), (False, 'review_rejected'))
        out = self.project / 'reviewed-queue.json'
        self.assertEqual(self.cli('export', '--out', str(out))[0], 0)
        self.assertEqual(json.loads(out.read_bytes())['counts'],
                         dict(pending_spot_check=1, pending_hold=1, reviewed_confirmed=1, reviewed_rejected=1))
        self.assertEqual(self.reviews.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.reviews.parent.stat().st_mode & 0o777, 0o700)
        before = self.reviews.read_bytes()
        for args in ((3,), (5,), (99,), (2, 'unknown'), (2, 'confirmed', 'A Person')):
            self.assertNotEqual(self.mark(*args)[0], 0)
            self.assertEqual(self.reviews.read_bytes(), before)
        record = json.loads(before.splitlines()[0])
        self.assertEqual(record['schema'], 'ocr-review-v1')
        self.assertEqual(record['confidence'], 79.99)
        self.assertEqual(record['derivative_sha256'], 'b' * 64)
        self.assertEqual(record['reviewer_label'], 'human-reviewer')
        self.assertTrue(record['reviewed_at'].endswith('Z'))
        self.assertEqual(self.ledger.read_bytes(), self.original)

    def test_extract_counts_unreviewed_pages(self):
        self.assertEqual(self.stage().get('review_required', 0), 4)
        self.assertEqual(self.ledger.read_bytes(), self.original)

    def test_stale_review_requeues_each_binding_change(self):
        from campaign_tool.records import ocr_review as review
        self.assertEqual(self.mark(3)[0], 0)
        for column, value in (('derivative_sha256', 'c' * 64), ('confidence', 79.0)):
            with self.subTest(column=column):
                # Simulate re-enrollment only in this synthetic fixture.
                with contextlib.closing(sqlite3.connect(self.ledger)) as db:
                    db.execute(f'UPDATE page_state SET {column}=? WHERE page_no=3', (value,))
                    db.commit()
                snapshot = self.ledger.read_bytes()
                self.assertEqual(self.stage()['review_required'], 4)
                self.assertEqual(review.page_use_allowed(self.page(3), self.reviews),
                                 (False, 'review_required_lt80'))
                self.assertEqual(self.mark(3)[0], 0)
                self.assertEqual(self.stage()['review_required'], 3)
                self.assertEqual(self.ledger.read_bytes(), snapshot)

    def test_middle_line_tamper_refuses_all_callers(self):
        from campaign_tool.records import ocr_review as review
        for page in (1, 2, 3):
            self.assertEqual(self.mark(page)[0], 0)
        lines = self.reviews.read_bytes().splitlines(keepends=True)
        for middle in (lines[1].replace(b'confirmed', b'rejected'), b''):
            with self.subTest(middle=middle):
                damaged = lines[0] + middle + lines[2]
                self.reviews.write_bytes(damaged)
                out = self.project / 'tampered-queue.json'
                self.assertEqual(self.cli('export', '--out', str(out)),
                                 (1, 'ocr_review_chain_broken\n'))
                self.assertFalse(out.exists())
                self.assertEqual(self.mark(4), (1, 'ocr_review_chain_broken\n'))
                stage = self.stage(expected_code=1)
                self.assertEqual(stage['error_code'], 'OCR_REVIEW_CHAIN_BROKEN')
                with self.assertRaisesRegex(review.ChainBroken, 'ocr_review_chain_broken'):
                    review.page_use_allowed(self.page(1), self.reviews)
                self.assertEqual(self.reviews.read_bytes(), damaged)
                self.assertEqual(self.ledger.read_bytes(), self.original)

    def test_busy_snapshot_omits_count(self):
        from campaign_tool.records import ocr_review  # Import before patching the snapshot provider.
        with mock.patch.object(run_stages, 'ledger_ro', side_effect=run_stages.LedgerBusy):
            self.assertNotIn('review_required', self.stage())
        self.assertEqual(self.ledger.read_bytes(), self.original)

    def test_export_never_overwrites_evidence(self):
        for out in (self.ledger, self.project / 'processing-v1' / 'new.json'):
            self.assertNotEqual(self.cli('export', '--out', str(out))[0], 0)
        self.assertEqual(self.ledger.read_bytes(), self.original)

    def test_cli_busy_refuses_without_creating_output(self):
        from campaign_tool.records import ocr_review as review
        out = self.project / 'busy-queue.json'
        with mock.patch.object(review, 'ledger_ro', side_effect=run_stages.LedgerBusy):
            self.assertEqual(self.cli('export', '--out', str(out)), (1, 'ocr_review_refused\n'))
        self.assertFalse(out.exists())

    def test_mark_refuses_nonprivate_existing_directory(self):
        self.reviews.parent.mkdir(mode=0o755)
        self.reviews.parent.chmod(0o755)
        self.assertNotEqual(self.mark(1)[0], 0)
        self.assertFalse(self.reviews.exists())
        self.assertEqual(self.ledger.read_bytes(), self.original)

    def test_invalid_mark_does_not_create_file(self):
        for args in ((5,), (99,), (1, 'unknown'), (1, 'confirmed', 'A Person')):
            self.assertNotEqual(self.mark(*args)[0], 0)
            self.assertFalse(self.reviews.exists())
        self.assertEqual(self.ledger.read_bytes(), self.original)
