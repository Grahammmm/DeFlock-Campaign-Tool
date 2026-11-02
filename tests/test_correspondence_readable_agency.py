"""Synthetic acceptance for correspondence_readable --agency (chosen agencies only)."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tools import correspondence_readable as readable
from tests.records import test_correspondence as canonical_tests

TITLE = '# Private correspondence\n\n'
GOLDEN = (  # main's output for dict_report() before --agency existed
    '# Private correspondence\n\n## Fictional agency-a\n\n### synthetic-r-a\n\nSubmitted 3 Feb 2026.\n'
    'Determination was due 13 Feb 2026; none is recorded.\nProduction: none is recorded.\n'
    'Whether this request was fully answered has not been assessed.\n\n\n## Fictional agency-b\n\n'
    '### synthetic-r-b\n\nSubmitted 3 Feb 2026.\nDetermination was due 13 Feb 2026; none is recorded.\n'
    'Production: none is recorded.\nWhether this request was fully answered has not been assessed.\n\n\n'
    '## Still open\n\n- Request r-b: Suggested follow-up on 1 Mar 2026 (proposal only; the owner decides).\n\n'
    '## What this report cannot tell you\n\n### Gaps in words\n\n'
    '- Outstanding items and follow-up have not been reviewed.\n'
    '- The recorded determination date needs date or time zone clarification.\n'
    '- A request association uses evidence excluded from this report.\n'
    '- Document associations disagree about which request they belong to.\n'
    '- A document cannot be associated with just one request.\n'
    '- A portal item is listed but its document bytes have not been received.\n\n'
    '### Report limits\n\nSynthetic snapshot only.\nSecond synthetic limit.\n\n'
    'As of 1 Mar 2026. Snapshot aaaaaaaaaaaa.\n')


def shown(n, m):
    return f'Shown: {n} of {m} agencies (filtered with --agency).\n\n'


def request(identity, agency, ref):
    return {'id': identity, 'agency_id': agency, 'external_ref': ref,
            'submitted_at': '2026-02-03T12:00:00Z', 'determination_due_at': '2026-02-13T12:00:00Z',
            'determination_at': None, 'production_at': None, 'fulfillment': 'not_assessed', 'occurrences': []}


def dict_report():
    return {'schema': 'correspondence-report-v1', 'visibility': 'private',
            'agencies': [{'id': 'agency-a', 'name': 'Fictional agency-a'},
                         {'id': 'agency-b', 'name': 'Fictional agency-b'}],
            'requests': [request('r-a', 'agency-a', 'synthetic-r-a'), request('r-b', 'agency-b', 'synthetic-r-b')],
            'mail': [], 'thread_candidates': [],
            'reminder_proposals': [{'request_id': 'r-b', 'at': '2026-03-01T00:00:00Z', 'status': 'proposal_only'}],
            'gaps': [{'code': 'outstanding_items_and_follow_up_unreviewed', 'request_id': 'r-b'},
                     {'code': 'determination_due_timezone_or_format_unresolved', 'request_id': 'r-a'},
                     {'code': 'request_join_scope_excluded'},
                     {'code': 'request_binding_conflict', 'occurrence_id': 'o', 'request_ids': ['r-a', 'r-b']},
                     {'code': 'request_binding_ambiguous', 'occurrence_id': 'p', 'request_ids': ['r-b']},
                     {'code': 'portal_bytes_not_received', 'agency_id': 'agency-b'}],
            'as_of': '2026-03-01T12:00:00Z', 'snapshot_sha256': 'a' * 64,
            'limits': ['Synthetic snapshot only.', 'Second synthetic limit.']}


class AgencyFilterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.source = self.base / 'report.json'
        self.count = 0

    def run_report(self, report, *agencies, output=None):
        self.source.write_text(json.dumps(report))
        if output is None:
            self.count += 1
            output = self.base / ('readable-' + str(self.count))
        argv = ['--report', str(self.source), '--output-dir', str(output)]
        for agency in agencies:
            argv += ['--agency', agency]
        capture = io.StringIO()
        with redirect_stdout(capture):
            code = readable.main(argv)
        md = (output / 'correspondence.md').read_text() if code == 0 else None
        return code, json.loads(capture.getvalue()), md, output

    def test_one_agency_from_real_two_agency_report(self):
        fixture = canonical_tests.CorrespondenceTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.agency('agency-a')
        fixture.agency('agency-b')
        fixture.request('r-a')
        fixture.request('r-b', 'agency-b')
        digest = fixture.original('message-b')
        fixture.occurrence('message-b', digest)
        fixture.join('join-b', digest, request='r-b', agency='agency-b')
        fixture.mail('message-b', digest)
        result = fixture.report()
        code, summary, md, _ = self.run_report(result, 'agency-a')
        self.assertEqual(code, 0)
        self.assertTrue(md.startswith(TITLE + shown(1, 2) + '## Fictional agency-a\n'))
        self.assertIn('### synthetic-r-a', md)
        for absent in ('Fictional agency-b', 'synthetic-r-b', 'r-b', digest[:12], 'Received '):
            self.assertNotIn(absent, md)
        kept = [g for g in result['gaps'] if g.get('request_id', 'r-a') == 'r-a']
        self.assertEqual(summary, {'ok': True, 'agencies': 1, 'requests': 1, 'gaps': len(kept), 'shown_agencies': 1})
        self.assertEqual(md.count('Outstanding items and follow-up have not been reviewed.'), 1)
        _, _, other, _ = self.run_report(result, 'agency-b')
        self.assertIn('Received ', other)
        self.assertNotIn('Fictional agency-a', other)

    def test_reminders_follow_their_requests_agency(self):
        report = dict_report()
        report['reminder_proposals'].append({'request_id': 'r-ghost', 'at': '2026-03-02T00:00:00Z'})
        _, _, md, _ = self.run_report(report, 'agency-a')
        self.assertIn('## Still open\n\nNo follow-up proposals are recorded.\n', md)
        self.assertNotIn('Request r-b', md)
        self.assertNotIn('r-ghost', md)
        _, _, md, _ = self.run_report(report, 'agency-b')
        self.assertIn('## Still open\n\n- Request r-b: Suggested follow-up on 1 Mar 2026', md)
        self.assertNotIn('r-ghost', md)
        self.assertNotIn('No follow-up proposals are recorded.', md)

    def test_gaps_bound_outside_the_choice_are_left_out(self):
        code, summary, md, _ = self.run_report(dict_report(), 'agency-a')
        self.assertEqual(code, 0)
        self.assertNotIn(readable.GAP_TEXT['outstanding_items_and_follow_up_unreviewed'], md)
        self.assertNotIn(readable.GAP_TEXT['request_binding_ambiguous'], md)
        self.assertNotIn(readable.GAP_TEXT['portal_bytes_not_received'], md)
        for code_name in ('determination_due_timezone_or_format_unresolved', 'request_join_scope_excluded',
                          'request_binding_conflict'):
            self.assertIn(readable.GAP_TEXT[code_name], md)
        self.assertEqual(summary, {'ok': True, 'agencies': 1, 'requests': 1, 'gaps': 3, 'shown_agencies': 1})
        self.assertIn('### Report limits\n\nSynthetic snapshot only.\nSecond synthetic limit.\n', md)
        _, summary, md, _ = self.run_report(dict_report(), 'agency-b')
        self.assertNotIn(readable.GAP_TEXT['determination_due_timezone_or_format_unresolved'], md)
        self.assertIn(readable.GAP_TEXT['portal_bytes_not_received'], md)
        self.assertEqual(summary['gaps'], 5)

    def test_every_agency_chosen_matches_unfiltered_plus_shown_line(self):
        _, plain_summary, plain, _ = self.run_report(dict_report())
        code, summary, both, _ = self.run_report(dict_report(), 'agency-b', 'agency-a')
        self.assertEqual(code, 0)
        self.assertEqual(both, TITLE + shown(2, 2) + plain[len(TITLE):])
        self.assertEqual(summary, {**plain_summary, 'agencies': 2, 'shown_agencies': 2})

    def test_unknown_or_repeated_agency_refused_before_any_folder(self):
        cases = [(['agency-z'], 'agency_unknown'), (['Agency-A'], 'agency_unknown'),
                 (['agency-a '], 'agency_unknown'), ([''], 'agency_unknown'),
                 (['agency-a', 'agency-z'], 'agency_unknown'),
                 (['agency-a', 'agency-a'], 'agency_repeated'), (['agency-b', 'agency-a', 'agency-b'], 'agency_repeated')]
        output = self.base / 'never'
        for agencies, expected in cases:
            with self.subTest(agencies=agencies):
                code, summary, _, _ = self.run_report(dict_report(), *agencies, output=output)
                self.assertEqual((code, summary), (2, {'error': expected}))
                self.assertFalse(output.exists())
        result = subprocess.run([sys.executable, '-B', 'tools/correspondence_readable.py', '--report', str(self.source),
                                 '--output-dir', str(output), '--agency', 'agency-z'],
                                capture_output=True, text=True, check=False)
        self.assertEqual((result.returncode, json.loads(result.stdout)), (2, {'error': 'agency_unknown'}))
        self.assertFalse(output.exists())

    def test_without_agency_output_is_byte_identical_golden(self):
        code, summary, _, output = self.run_report(dict_report())
        self.assertEqual(code, 0)
        self.assertEqual((output / 'correspondence.md').read_bytes(), GOLDEN.encode())
        self.assertEqual(summary, {'ok': True, 'agencies': 2, 'requests': 2, 'gaps': 6})

    def test_filtered_output_is_private_and_existing_folder_kept(self):
        code, _, _, output = self.run_report(dict_report(), 'agency-a')
        self.assertEqual(code, 0)
        self.assertEqual(output.stat().st_mode & 0o777, 0o700)
        self.assertEqual((output / 'correspondence.md').stat().st_mode & 0o777, 0o600)
        sentinel = output / 'keep'
        sentinel.write_text('unchanged')
        self.assertEqual(self.run_report(dict_report(), 'agency-a', output=output)[1], {'error': 'output_dir_exists'})
        self.assertEqual(sentinel.read_text(), 'unchanged')
        self.assertEqual((output / 'correspondence.md').read_text().count('Fictional agency-b'), 0)

    def test_hostile_agency_name_escaped_as_without_filter(self):
        report = dict_report()
        report['agencies'][0]['name'] = '# Heading [x](http://e) | `code` \\ * _ <tag> \u202e\x00'
        _, _, plain, _ = self.run_report(report)
        _, _, chosen, _ = self.run_report(report, 'agency-a')
        heading = next(line for line in plain.splitlines() if line.startswith('## \\#'))
        self.assertIn('\n' + heading + '\n', chosen)
        self.assertIn('\\[x\\](http://e)', chosen)
        self.assertNotIn('\u202e', chosen)
        self.assertNotIn('\x00', chosen)
        self.assertNotRegex(chosen, r'(?<!\\)\[x\]\(')


if __name__ == '__main__':
    unittest.main()
