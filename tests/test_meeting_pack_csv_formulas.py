"""RF0300-4: private meeting-pack sources.csv cells pass the shared spreadsheet formula guard (synthetic, offline)."""
import csv
import hashlib
import html
import io
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from campaign_tool.kit_export import formula_safe
from campaign_tool.meeting_pack import build_upcoming
from tests import test_meeting_pack as _fixture

NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)
HEADER = ['title', 'record_prefix', 'received', 'locator', 'public_copy', 'cited_by']
AGENCY = 'ca-cedar-police'
# Admitted by content validation; a carriage-return start is already refused there and stays refused.
UNSAFE = ('=1+1', '+1+1', '-1+1', '@SUM(1,1)', '\t=1+1')
SAFE = ('ordinary source', '\u00e9\u4e2d unicode', 'source, "quoted"\nnext', ' =leading space', "'=1+1", 'a=b')


def prefix(number):
    return hashlib.sha256(f'synthetic {number}'.encode()).hexdigest()[:12]


class MeetingPackCsvFormulaTests(unittest.TestCase):
    # Borrow the synthetic pack fixture's plain functions; no discoverable TestCase is inherited or re-run.
    _base = _fixture.MeetingPackTests
    setUp = _base.setUp
    write, stage, finding, pack, html, snapshot = (_base.write, _base.stage, _base.finding, _base.pack, _base.html,
                                                   _base.snapshot)
    modes, siblings, fail_replace, cli = _base.modes, _base.siblings, _base.fail_replace, _base.cli

    def titled(self, number, title, **extra):
        source = {**self.template['sources'][0], 'sha256': hashlib.sha256(f'synthetic {number}'.encode()).hexdigest(),
                  'title': title, 'locator': f'page:{number}', **extra}
        self.finding(number, sources=[source])

    def rows(self):
        return list(csv.reader(io.StringIO((self.folder / 'sources.csv').read_text(), newline='')))

    def raw(self):
        return (self.folder / 'sources.csv').read_bytes()

    def pack_bytes(self):
        return {n: (self.folder / n).read_bytes() for n in ('handout.html', 'comment.md', 'sources.csv')}

    def test_admitted_formula_source_titles_guarded_once(self):
        for title in UNSAFE:
            with self.subTest(title=title):
                self.titled(1, title)
                self.pack(replace=self.folder.exists())
                rows = self.rows()
                self.assertEqual(rows[0], HEADER)
                self.assertEqual(rows[2][0], "'" + title)  # finding 1 is the older finding, so its row is second
                self.assertEqual(rows[2][1], prefix(1))
                self.assertFalse(rows[2][0].startswith("''"))
                self.assertIn(html.escape(title), self.html())  # the handout keeps the original text
                self.assertEqual((self.folder / 'comment.md').read_bytes(), b'Hello.\nThank you.\n')

    def test_safe_and_already_guarded_titles_byte_identical(self):
        self.pack()
        expected = ('title,record_prefix,received,locator,public_copy,cited_by\r\n'
                    f'Source 2,{prefix(2)},2026-02-02T12:00:00Z,page:2,held privately,finding-2\r\n'
                    f'Source 1,{prefix(1)},2026-02-01T12:00:00Z,page:1,held privately,finding-1\r\n').encode()
        self.assertEqual(self.raw(), expected)
        before = self.pack_bytes()
        for title in SAFE:
            with self.subTest(title=title):
                self.titled(1, title)
                self.pack(replace=True)
                self.assertEqual(self.rows()[2][0], title)
                self.assertEqual(self.raw(), expected.replace(b'Source 1', self.encoded(title), 1))
                self.assertEqual((self.folder / 'comment.md').read_bytes(), before['comment.md'])
        self.assertEqual(self.rows()[2][0], 'a=b')

    @staticmethod
    def encoded(title):
        out = io.StringIO(newline='')
        csv.writer(out).writerow([title])
        return out.getvalue().rstrip('\r\n').encode()

    def test_guard_after_locator_and_citation_aggregation(self):
        # Findings run newest first (finding 2, then 1); the first title and first locator open the joined cells.
        digest = hashlib.sha256(b'synthetic 1').hexdigest()
        shared = {**self.template['sources'][0], 'sha256': digest}
        self.finding(2, sources=[{**shared, 'title': 'Later title', 'locator': 'page:2'}])
        self.finding(1, sources=[{**shared, 'title': '=Older title', 'locator': '=cell-two'}])
        self.write('sources.json', self.library[:1])
        self.pack()
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1][0], 'Later title')
        self.assertEqual(rows[1][3], 'page:2; =cell-two')  # a later element is not prefixed on its own
        self.assertEqual(rows[1][5], 'finding-2; finding-1')
        self.finding(2, sources=[{**shared, 'title': '=First title', 'locator': '@first'}])
        self.pack(replace=True)
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1][0], "'=First title")
        self.assertEqual(rows[1][3], "'@first; =cell-two")  # one prefix for the whole joined cell
        self.assertEqual(rows[1][5], 'finding-2; finding-1')

    def test_guard_runs_once_per_completed_data_cell(self):
        self.titled(1, '=1+1')
        with patch('campaign_tool.meeting_pack.formula_safe', side_effect=formula_safe) as spy:
            self.pack()
        seen = [call.args[0] for call in spy.call_args_list]
        self.assertEqual(len(seen), 12)  # two data rows of six cells; the header is not guarded
        self.assertIn('=1+1', seen)
        self.assertNotIn("'=1+1", seen)
        self.assertNotIn('title', seen)

    def statements(self):
        people = [{'name': 'Ann Alpha', 'role': 'Member', 'profile_url': None,
                   'source_url': 'https://synthetic.invalid/roster', 'checked_on': '2026-10-01'}]
        self.write('officials.json', {'schema_version': 1, 'bodies': [{'agency_id': AGENCY, 'officials': people}]})
        self.write('statements.json', [{'agency_id': AGENCY, 'official': 'Ann Alpha', 'said_on': '2026-03-01',
                                        'quote': '=not a cell', 'context': 'meeting', 'checked_on': '2026-10-30',
                                        'source_url': 'https://synthetic.invalid/An/2026-03-01'}])

    def test_statement_schema_and_empty_placeholders_unchanged(self):
        self.titled(1, '@SUM(1,1)')
        self.pack()
        no_statements = self.raw()
        self.assertEqual(len(self.rows()[0]), 6)
        self.statements()
        self.pack(replace=True)
        rows = self.rows()
        self.assertEqual(rows[0], HEADER + ['kind', 'said_on', 'source_url'])
        self.assertEqual(rows[2], ["'@SUM(1,1)", prefix(1), '2026-02-01T12:00:00Z', 'page:1', 'held privately',
                                   'finding-1', 'source', '', ''])
        self.assertEqual(rows[3], [''] * 6 + ['statement', '2026-03-01', 'https://synthetic.invalid/An/2026-03-01'])
        self.assertEqual(len(rows), 4)
        self.assertIn(html.escape('=not a cell'), self.html())  # the exact quote stays in the handout
        self.assertNotIn(b'not a cell', self.raw())
        self.assertTrue(no_statements.startswith(b'title,record_prefix,received,locator,public_copy,cited_by\r\n'))

    def test_single_cli_and_upcoming_share_formula_projection(self):
        self.titled(1, '=1+1')
        for extra in ((), ('--replace',)):
            result = self.cli(*extra)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, '')
            self.assertEqual(json.loads(result.stdout)['findings'], 2)
            self.assertEqual(self.rows()[2][0], "'=1+1")
        self.assertEqual(self.cli().returncode, 1)  # the existing refusal without --replace is unchanged
        for meeting_id, starts in (('m', '2026-11-10T18:00:00-08:00'), ('m2', '2026-11-12T18:00:00-08:00'),
                                   ('m3', '2026-11-13T18:00:00-08:00')):
            self.meeting = {**self.meeting, 'id': meeting_id, 'starts_at': starts}
            if meeting_id == 'm3':
                self.meeting['status'] = 'cancelled'
            self.write(f'meetings/{meeting_id}.json', self.meeting)
        report = build_upcoming(self.root, 30, NOW, replace=True)
        self.assertEqual([item['id'] for item in report['built']], ['m', 'm2'])
        self.assertEqual(report['skipped'], [{'id': 'm3', 'reason': 'cancelled'}])
        for meeting_id in ('m', 'm2'):
            sheet = list(csv.reader(io.StringIO((self.root / 'kit/meeting-packs' / meeting_id / 'sources.csv')
                                                .read_text(), newline='')))
            self.assertEqual(sheet[2][0], "'=1+1")

    def test_csv_guard_does_not_mutate_sources_or_expand_sinks(self):
        self.titled(1, '-1+1')
        before = self.snapshot()
        first = self.pack()
        pack = self.pack_bytes()
        self.assertEqual(self.pack(replace=True), first)
        self.assertEqual(self.pack_bytes(), pack)
        self.assertEqual(self.snapshot(), before)  # content/ and public/ are untouched
        self.assertEqual(json.loads((self.content / 'findings/f1.json').read_text())['sources'][0]['title'], '-1+1')
        self.assertEqual(sorted(self.modes()), ['comment.md', 'handout.html', 'sources.csv'])
        self.assertEqual({mode for _, mode in self.modes().values()}, {0o600})
        self.assertEqual(self.folder.stat().st_mode & 0o777, 0o700)
        self.assertNotIn('warnings', first)

    def test_replacement_failure_preserves_complete_previous_pack(self):
        self.pack()
        old = self.modes()
        self.titled(1, '=1+1')
        self.meeting['comment_kit_md'] = 'Changed comment.\n'
        self.stage()
        failing, _calls = self.fail_replace(lambda source, target: source.name.startswith('.m.new-') and target == self.folder)
        with failing, self.assertRaisesRegex(OSError, 'synthetic promotion failure'):
            self.pack(replace=True)
        self.assertEqual(self.modes(), old)  # one complete previous generation, no mixed files
        self.assertEqual(self.siblings(), ['m'])
        self.pack(replace=True)
        rows = self.rows()
        self.assertEqual(rows[2][0], "'=1+1")
        self.assertEqual((self.folder / 'comment.md').read_text(), 'Changed comment.\n')
        self.assertEqual(self.modes()['sources.csv'][0], old['sources.csv'][0].replace(b'Source 1,', b"'=1+1,", 1))


if __name__ == '__main__':
    unittest.main()
