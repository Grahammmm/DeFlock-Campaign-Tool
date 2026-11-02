"""Synthetic private JSON-to-Markdown acceptance, including real report production."""
from contextlib import redirect_stdout
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

from tools import correspondence_readable as readable
from tests.records import test_correspondence as canonical_tests


class ReadableTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.source = self.base / 'report.json'
        self.output = self.base / 'readable'
        self.report = {'schema': 'correspondence-report-v1', 'visibility': 'private',
                       'agencies': [], 'requests': [], 'mail': [], 'gaps': [],
                       'thread_candidates': [], 'reminder_proposals': [],
                       'as_of': '2026-03-01T12:00:00Z', 'snapshot_sha256': 'a' * 64,
                       'limits': ['Synthetic snapshot only.']}

    def request(self, **values):
        row = {'id': 'r', 'agency_id': 'a', 'external_ref': 'Synthetic request',
               'submitted_at': '2026-02-03T12:00:00Z',
               'determination_due_at': '2026-02-13T12:00:00Z',
               'determination_at': None, 'production_at': None,
               'fulfillment': 'not_assessed', 'occurrences': []}
        row.update(values)
        self.report['agencies'] = [{'id': 'a', 'name': 'Synthetic agency'}]
        self.report['requests'].append(row)
        return row

    def run_report(self, report=None, **args):
        self.source.write_text(json.dumps(self.report if report is None else report))
        out = io.StringIO()
        with redirect_stdout(out):
            status = readable.main(['--report', str(args.get('source', self.source)),
                                    '--output-dir', str(args.get('output', self.output))])
        return status, json.loads(out.getvalue()), out.getvalue()

    def markdown(self):
        return (self.output / 'correspondence.md').read_text()

    def test_real_report_two_agencies_three_requests_five_mail_reply_chain(self):
        fixture = canonical_tests.CorrespondenceTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.agency('agency-a')
        fixture.agency('agency-b')
        fixture.request('r-later', due='2026-10-01T12:00:00Z')
        fixture.request('r-earlier')
        fixture.request('r-other', 'agency-b')
        fixture.execute("UPDATE requests SET submitted_at='2026-08-01T12:00:00Z' WHERE id='r-earlier'")
        for i in range(5):
            identity = 'message-' + str(i)
            digest = fixture.original(identity)
            fixture.occurrence(identity, digest)
            fixture.join('join-' + str(i), digest, request='r-earlier')
            fields = [] if not i else [['In-Reply-To', '<message-' + str(i - 1) + '@synthetic.invalid>']]
            fixture.mail(identity, digest, uid=i + 1, fields=fields)
        result = fixture.report()
        code, summary, _ = self.run_report(result)
        self.assertEqual(code, 0)
        self.assertEqual(summary, {'ok': True, 'agencies': 2, 'requests': 3, 'gaps': len(result['gaps'])})
        md = self.markdown()
        self.assertLess(md.index('Fictional agency-a'), md.index('Fictional agency-b'))
        self.assertLess(md.index('### synthetic-r-earlier'), md.index('### synthetic-r-later'))
        self.assertEqual(md.count('Received '), 5)
        self.assertIn('reply to 13 Oct 2026 message (from reply headers; not verified)', md)

    def test_dates_no_inferences_and_missing_values(self):
        self.request(status='ignored illegal refused overdue late violated')
        self.assertEqual(self.run_report()[0], 0)
        md = self.markdown()
        self.assertIn('Submitted 3 Feb 2026.', md)
        self.assertIn('Determination was due 13 Feb 2026; none is recorded.', md)
        self.assertIn('Production: none is recorded.', md)
        self.assertIn('Whether this request was fully answered has not been assessed.', md)
        self.assertIsNone(re.search(r'\b(late|overdue|refused|violated|illegal|ignored)\b', md, re.I))

    def test_naive_timezone_and_offset_are_not_converted(self):
        self.request(submitted_at='2026-02-03T23:55:00-08:00',
                     determination_due_at='2026-02-13T12:00:00',
                     determination_at='2026-02-14T00:01:00+14:00')
        self.assertEqual(self.run_report()[0], 0)
        md = self.markdown()
        self.assertIn('Submitted 3 Feb 2026.', md)
        self.assertIn('2026-02-13T12:00:00', md)
        self.assertIn('(time zone not recorded)', md)
        self.assertIn('determination recorded 14 Feb 2026', md)

    def test_all_known_gaps_unknown_and_owner_proposals(self):
        self.request()
        self.report['gaps'] = [{'code': code} for code in readable.GAP_TEXT] + [{'code': 'new_gap'}]
        self.report['reminder_proposals'] = [{'request_id': 'r', 'at': '2026-03-01T00:00:00Z',
                                             'status': 'proposal_only', 'requires_current_owner_review': True}]
        code, summary, _ = self.run_report()
        self.assertEqual(code, 0)
        self.assertEqual(summary['gaps'], len(self.report['gaps']))
        for sentence in readable.GAP_TEXT.values():
            self.assertIn(sentence, self.markdown())
        self.assertIn('Other gap: `new\\_gap`', self.markdown())
        self.assertIn('Suggested follow-up on 1 Mar 2026 (proposal only; the owner decides)', self.markdown())
        self.assertIn('Synthetic snapshot only.', self.markdown())

    def test_hostile_text_is_not_markdown_or_control_characters(self):
        hostile = '# Heading [x](http://e) | `code` \\ * _ <tag> \u202e\x00'
        self.request(external_ref='- ' + hostile)
        self.report['agencies'][0]['name'] = hostile
        self.report['limits'].append('b' * 64)
        self.assertEqual(self.run_report()[0], 0)
        md = self.markdown()
        self.assertIn('\\# Heading', md)
        self.assertIn('\\[x\\](http://e)', md)
        self.assertIn('\\| \\`code\\`', md)
        self.assertNotIn('\u202e', md)
        self.assertNotIn('\x00', md)
        self.assertNotRegex(md, r'(?<!\\)\[x\]\(')
        self.assertNotRegex(md, r'[0-9a-fA-F]{40,}')
        for value in ('- a', '+ b', '12. c'):
            self.assertTrue(readable.text(value).startswith('\\'))

    def test_duplicate_mail_and_attachment_chronology(self):
        row = self.request()
        first, reply, attachment = '1' * 64, '2' * 64, '3' * 64
        for i, digest in enumerate((reply, first, first, first, attachment)):
            row['occurrences'].append({'id': str(i), 'original_sha256': digest,
                'acquired_at': '2026-02-05T12:00:00Z' if digest == reply else '2026-02-04T12:00:00Z',
                'kind': 'attachment' if digest == attachment else 'mail',
                'parent_occurrence_id': '1' if digest == attachment else None})
        self.report['mail'] = [
            {'id': 'reply', 'eml_sha256': reply, 'received_at': '2026-02-05T12:00:00Z', 'folder': 'INBOX'},
            *[{'id': 'parent-' + str(i), 'eml_sha256': first, 'received_at': '2026-02-04T12:00:00Z',
               'folder': 'INBOX'} for i in range(3)],
            {'id': 'unrelated', 'eml_sha256': '4' * 64, 'received_at': '2026-01-01T00:00:00Z', 'folder': 'Other'}]
        self.report['thread_candidates'] = [{'mail_id': 'reply', 'parent_mail_ids': ['parent-0']}]
        self.assertEqual(self.run_report()[0], 0)
        md = self.markdown()
        self.assertEqual(md.count('Received '), 2)
        self.assertEqual(md.count('received 3 times'), 1)
        self.assertLess(md.index('Received 4 Feb'), md.index('Received 5 Feb'))
        self.assertIn('  - Document acquired 4 Feb 2026, attachment, file 333333333333', md)
        self.assertNotIn('444444444444', md)
        self.assertNotRegex(md, r'[0-9a-fA-F]{40,}')

    def test_chronology_offsets_missing_dates_and_parent_placement(self):
        row = self.request()
        self.request(id='earlier', external_ref='Earlier request', submitted_at='2026-02-03T23:00:00+14:00')
        row['occurrences'] = [
            {'id': 'parent', 'original_sha256': 'a' * 64, 'kind': 'mail',
             'acquired_at': '2026-02-05T00:00:00Z', 'parent_occurrence_id': None},
            {'id': 'child', 'original_sha256': 'b' * 64, 'kind': 'attachment',
             'acquired_at': '2026-02-01T00:00:00Z', 'parent_occurrence_id': 'parent'}]
        self.assertEqual(self.run_report()[0], 0)
        md = self.markdown()
        self.assertLess(md.index('### Earlier request'), md.index('### Synthetic request'))
        self.assertIn('file aaaaaaaaaaaa.\n  - Document acquired 1 Feb', md)
        self.assertEqual(readable.date(None), 'none is recorded')
        self.assertIn('date format unresolved', readable.date('synthetic-unknown'))

    def test_duplicate_json_members_are_not_selected_by_wording(self):
        self.source.write_text('{"schema":"other","schema":"correspondence-report-v1","visibility":"private"}')
        capture = io.StringIO()
        with redirect_stdout(capture):
            code = readable.main(['--report', str(self.source), '--output-dir', str(self.output)])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(capture.getvalue()), {'error': 'report_not_json'})
        self.assertFalse(self.output.exists())

    def test_empty_cli_permissions_and_source_unchanged(self):
        self.source.write_text(json.dumps(self.report))
        before = hashlib.sha256(self.source.read_bytes()).hexdigest()
        result = subprocess.run([sys.executable, '-B', 'tools/correspondence_readable.py',
                                 '--report', str(self.source), '--output-dir', str(self.output)],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {'ok': True, 'agencies': 0, 'requests': 0, 'gaps': 0})
        self.assertIn('No requests are recorded.', self.markdown())
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.output / 'correspondence.md').stat().st_mode & 0o777, 0o600)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), before)

    def test_report_refusals_create_nothing(self):
        cases = [('schema_unsupported', {'schema': 'other'}),
                 ('report_not_private', {'visibility': 'public'}),
                 ('report_has_error', {'error': 'private secret marker'}),
                 ('report_not_json', None), ('report_unreadable', 'oversize')]
        for expected, changes in cases:
            with self.subTest(expected=expected):
                report = copy.deepcopy(self.report)
                if isinstance(changes, dict):
                    report.update(changes)
                self.source.write_text(json.dumps(report))
                if changes is None:
                    self.source.write_text('{not json')
                elif changes == 'oversize':
                    with self.source.open('wb') as stream:
                        stream.truncate(33 * 1024 * 1024)
                capture = io.StringIO()
                with redirect_stdout(capture):
                    code = readable.main(['--report', str(self.source), '--output-dir', str(self.output)])
                self.assertEqual(code, 2)
                self.assertEqual(json.loads(capture.getvalue()), {'error': expected})
                self.assertNotIn(str(self.base), capture.getvalue())
                self.assertFalse(self.output.exists())

    def test_path_refusals_and_existing_output_preserved(self):
        self.output.mkdir()
        sentinel = self.output / 'keep'
        sentinel.write_text('unchanged')
        self.assertEqual(self.run_report()[1], {'error': 'output_dir_exists'})
        self.assertEqual(sentinel.read_text(), 'unchanged')
        link = self.base / 'parent-link'
        link.symlink_to(self.base, target_is_directory=True)
        for source, output, expected in [
            ('relative.json', self.base / 'new', 'path_not_absolute'),
            (self.source, 'relative-dir', 'path_not_absolute'),
            (self.source, str(self.base) + '/sub/../new', 'path_not_absolute'),
            (self.source, link / 'new', 'output_parent_unsafe'),
            (self.base / 'missing.json', self.base / 'new', 'report_unreadable')]:
            with self.subTest(expected=expected):
                self.assertEqual(self.run_report(source=source, output=output)[1], {'error': expected})
                self.assertFalse((self.base / 'new').exists())
        source_link = self.base / 'source-link'
        source_link.symlink_to(self.source)
        self.assertEqual(self.run_report(source=source_link)[1], {'error': 'report_unreadable'})


if __name__ == '__main__':
    unittest.main()
