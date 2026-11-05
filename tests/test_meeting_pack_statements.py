"""DF1407-07: 'What members have said' in the private meeting pack (synthetic, offline)."""
import csv
import html
import io
import json
import unittest
from datetime import datetime, timezone

from campaign_tool.meeting_pack import build
from tests import test_meeting_pack as _fixture

AGENCY = 'ca-cedar-police'
NAMES = ('Ann Alpha', 'Bo Beta', 'Cy Gamma')
NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)


def statement(name, day, quote, agency=AGENCY, **extra):
    return {'agency_id': agency, 'official': name, 'said_on': day, 'quote': quote, 'context': 'meeting',
            'source_url': f'https://synthetic.invalid/{name[:2]}/{day}', 'checked_on': '2026-10-30', **extra}


class StatementPackTests(_fixture.MeetingPackTests):
    # Reuses the pack fixture; the inherited tests are not re-run here (see the discovery guard below).
    def setUp(self):
        super().setUp()
        people = lambda: [{'name': n, 'role': 'Member', 'profile_url': None,
                           'source_url': 'https://synthetic.invalid/roster', 'checked_on': '2026-10-01'} for n in NAMES]
        self.write('officials.json', {'schema_version': 1, 'bodies': [{'agency_id': AGENCY, 'officials': people()},
                                                                      {'agency_id': 'ca-cedar-city-council', 'officials': people()}]})
        self.before = self.read_pack()

    def read_pack(self):
        self.pack(replace=True)
        return {n: (self.folder / n).read_bytes() for n in ('handout.html', 'comment.md', 'sources.csv')}

    def put(self, rows):
        self.write('statements.json', rows)

    def sheet(self):
        return list(csv.reader(io.StringIO((self.folder / 'sources.csv').read_text(), newline='')))

    def test_two_newest_per_member_in_roster_order_with_csv_rows(self):
        rows = [statement('Cy Gamma', '2026-05-01', 'c1'), statement('Ann Alpha', '2026-01-01', 'a-old'),
                statement('Ann Alpha', '2026-03-01', 'a-mid'), statement('Ann Alpha', '2026-04-01', 'a-new'),
                statement('Ann Alpha', '2026-04-01', 'a-new-same-day'),
                statement('Ann Alpha', '2026-04-01', 'a-new', agency='ca-cedar-city-council'),
                statement('Cy Gamma', '2026-06-01', 'c2')]
        self.put(rows)
        pack = self.read_pack()
        page = pack['handout.html'].decode()
        self.assertIn('<h2>What members have said</h2>', page)
        self.assertIn("these selections do not summarize a member's views.", page)
        self.assertEqual(page.count('class="quote"'), 4)
        order = [page.index(x) for x in ('\u201ca-new\u201d', 'a-new-same-day', 'c2', 'c1')]
        self.assertEqual(order, sorted(order))
        self.assertNotIn('a-mid', page)
        self.assertNotIn('a-old', page)
        self.assertNotIn('Bo Beta', page.split('What members have said')[1])
        self.assertLess(page.index('What we found'), page.index('What members have said'))
        table = self.sheet()
        self.assertEqual(table[0], ['title', 'record_prefix', 'received', 'locator', 'public_copy', 'cited_by',
                                    'kind', 'said_on', 'source_url'])
        kinds = [r[6] for r in table[1:]]
        self.assertEqual(kinds, ['source', 'source', 'statement', 'statement', 'statement', 'statement'])
        self.assertEqual([r[7:] for r in table[3:]], [['2026-04-01', 'https://synthetic.invalid/An/2026-04-01'],
                                                      ['2026-04-01', 'https://synthetic.invalid/An/2026-04-01'],
                                                      ['2026-06-01', 'https://synthetic.invalid/Cy/2026-06-01'],
                                                      ['2026-05-01', 'https://synthetic.invalid/Cy/2026-05-01']])
        self.assertEqual(table[3][:7], [''] * 6 + ['statement'])
        self.assertEqual(table[1][6:], ['source', '', ''])
        self.assertEqual(pack['comment.md'], self.before['comment.md'])

    def test_exact_long_quote_with_markup_whitespace_and_query_url(self):
        quote = ('  Yes & no <b>"x"</b> {count}  {quote}   \u00e9\u4e2d ' + 'z' * 400)[:400]
        self.put([statement('Bo Beta', '2026-02-02', quote, source_url='https://synthetic.invalid/ab?x=1&y=2')])
        page = self.read_pack()['handout.html'].decode()
        self.assertIn('\u201c' + html.escape(quote, quote=True) + '\u201d', page)
        self.assertIn('.quote { white-space: pre-wrap }', page)
        self.assertIn('href="https://synthetic.invalid/ab?x=1&amp;y=2"', page)
        self.assertEqual(self.sheet()[-1][8], 'https://synthetic.invalid/ab?x=1&y=2')

    def test_no_file_empty_other_body_or_no_agency_leave_all_three_files_identical(self):
        cases = [[], [statement('Ann Alpha', '2026-03-01', 'q', agency='ca-cedar-city-council')]]
        for rows in cases:
            self.put(rows)
            self.assertEqual(self.read_pack(), self.before)
        self.put([statement('Ann Alpha', '2026-03-01', 'q')])
        self.meeting['agency_id'] = None
        self.stage()
        null_pack = self.read_pack()
        self.assertNotIn(b'What members have said', null_pack['handout.html'])
        self.assertEqual(null_pack['sources.csv'].decode().splitlines()[0].count(','), 5)

    def test_labels_override_text_only_and_comment_unchanged(self):
        self.put([statement('Ann Alpha', '2026-03-01', 'Keep <this>')])
        self.write('labels.json', {'schema_version': 1, 'labels': {
            'handout.statements_heading': 'Dicho', 'handout.statements_note': 'Nota', 'statements.context_meeting': 'reunion',
            'statements.quote': '[{quote}]'}})
        pack = self.read_pack()
        page = pack['handout.html'].decode()
        for text in ('<h2>Dicho</h2><p>Nota</p>', '[Keep &lt;this&gt;]', '(reunion)'):
            self.assertIn(text, page)
        self.assertEqual(pack['comment.md'], self.before['comment.md'])

    def test_refusals_leave_existing_pack_untouched(self):
        bad = [('not json{', None), ('{}', None), ([statement('Nobody Here', '2026-03-01', 'q')], None),
               ([statement('Ann Alpha', '2027-03-01', 'q')], None),
               ([statement('Ann Alpha', '2026-03-01', 'q', source_url='http://synthetic.invalid/x')], None),
               ([statement('Ann Alpha', '2026-03-01', '')], None)]
        for value, _ in bad:
            path = self.content / 'statements.json'
            path.write_text(value if isinstance(value, str) else json.dumps(value))
            with self.assertRaises(ValueError):
                build(self.root, 'm', today=NOW, replace=True)
            self.assertEqual({n: (self.folder / n).read_bytes() for n in self.before}, self.before)

    def test_missing_roster_and_symlink_refuse_and_full_hash_in_quote_refused(self):
        self.put([statement('Ann Alpha', '2026-03-01', 'q')])
        (self.content / 'officials.json').unlink()
        with self.assertRaises(ValueError):
            build(self.root, 'm', today=NOW, replace=True)
        self.write('officials.json', {'schema_version': 1, 'bodies': [{'agency_id': AGENCY, 'officials': [
            {'name': 'Ann Alpha', 'role': 'Member', 'profile_url': None, 'source_url': 'https://synthetic.invalid/r',
             'checked_on': '2026-10-01'}]}]})
        self.put([statement('Ann Alpha', '2026-03-01', 'h' + 'ab12' * 16)])
        with self.assertRaises(ValueError):
            build(self.root, 'm', today=NOW, replace=True)
        self.assertEqual({n: (self.folder / n).read_bytes() for n in self.before}, self.before)
        (self.content / 'statements.json').unlink()
        (self.content / 'real.json').write_text('[]')
        (self.content / 'statements.json').symlink_to('real.json')
        with self.assertRaises(ValueError):
            build(self.root, 'm', today=NOW, replace=True)

    def test_two_builds_identical_and_upcoming_uses_the_section(self):
        self.put([statement('Ann Alpha', '2026-03-01', 'same')])
        self.assertEqual(self.read_pack(), self.read_pack())
        from campaign_tool.meeting_pack import build_upcoming
        self.meeting['starts_at'] = '2026-11-10T18:00:00-08:00'
        self.stage()
        report = build_upcoming(self.root, 30, NOW, replace=True)
        self.assertEqual([b['id'] for b in report['built']], ['m'])
        self.assertIn(b'What members have said', (self.folder / 'handout.html').read_bytes())


# Only this module's tests run here; the inherited fixture methods are removed from the collected class.
for _name in [n for n in vars(_fixture.MeetingPackTests) if n.startswith('test_')]:
    if _name not in vars(StatementPackTests):
        setattr(StatementPackTests, _name, None)

if __name__ == '__main__':
    unittest.main()
