"""Private letters to the editor, using fictional content only (DF2207-09)."""
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from campaign_tool import press
from campaign_tool.content import load_content
from campaign_tool.press import build_letter, build_press, letter_text, letter_word_count

REPO = Path(__file__).resolve().parents[1]
FIRST, SECOND, THIRD, FOURTH = (f'fnd_000000000000000{n}' for n in '1234')
SLUG = 'cedar-policy-posted'
DAY = '2026-03-01'
NOW = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
POLICE, OTHER = 'ca-cedar-police', 'ca-cedar-county-sheriff'
BASE = 'https://cedar-records.example.invalid'


def words(count, tag):
    return ' '.join(f'{tag}{n}' for n in range(count))


def tree(path):
    """{relative path: bytes or link target} of everything under path (empty when absent)."""
    out = {}
    for item in sorted(Path(path).rglob('*')) if Path(path).exists() else []:
        key = item.relative_to(path).as_posix()
        out[key] = os.readlink(item) if item.is_symlink() else (item.read_bytes() if item.is_file() else None)
    return out


class LetterBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='fictional-letter-')
        self.addCleanup(self.tmp.cleanup)
        self.top = Path(self.tmp.name)
        self.root = self.top / 'campaign'
        shutil.copytree(REPO / 'examples/fictional-campaign', self.root, ignore=shutil.ignore_patterns('public'))
        self.findings = self.root / 'content/findings'
        self.first = json.loads((self.findings / (SLUG + '.json')).read_text())
        self.add(SECOND, 'cedar-second', 'Second fictional finding', 'Second summary.', [])
        self.add(THIRD, 'cedar-third', 'Third fictional finding', 'Third summary.', [])
        self.add(FOURTH, 'cedar-fourth', 'Fourth fictional finding', 'Fourth summary.', [])
        for target in ('socket.socket.connect', 'socket.create_connection'):
            patcher = mock.patch(target, side_effect=AssertionError('network is blocked'))
            patcher.start()
            self.addCleanup(patcher.stop)

    def add(self, ident, slug, title, summary, corrections, **extra):
        finding = dict(self.first, id=ident, slug=slug, title=title, summary=summary, limitations=[],
                       corrections=corrections, **extra)
        self.save(finding)
        return finding

    def save(self, finding):
        (self.findings / (finding['slug'] + '.json')).write_text(json.dumps(finding), encoding='utf-8')

    def site(self, **kw):
        path = self.root / 'content/site.json'
        data = json.loads(path.read_text())
        data.update(kw)
        if 'base_url' in kw and kw['base_url'] is None:
            data.pop('base_url')
        path.write_text(json.dumps(data))

    def meeting(self, ident, starts_at, **extra):
        folder = self.root / 'content/meetings'
        folder.mkdir(exist_ok=True)
        document = {'id': ident, 'body': 'Cedar Council (fictional)', 'starts_at': starts_at, 'agency_id': POLICE, **extra}
        (folder / (ident + '.json')).write_text(json.dumps(document), encoding='utf-8')

    def letter(self, ids=(FIRST,), **kw):
        kw.setdefault('now', NOW)
        return build_letter(self.root, list(ids), DAY, **kw)

    def read(self, result):
        return (self.root / result['path']).read_text(encoding='utf-8')

    def letters(self):
        return self.root / 'kit/press/letters'

    def cli(self, *args):
        return subprocess.run([sys.executable, '-B', '-m', 'campaign_tool', 'press', 'letter',
                               '--directory', str(self.root), *args], cwd=REPO, capture_output=True, text=True, timeout=60)

    def refuses(self, pattern, *args, **kw):
        before = tree(self.root)
        with self.assertRaisesRegex(ValueError, pattern):
            self.letter(*args, **kw)
        self.assertEqual(tree(self.root), before, 'a refused letter must write nothing')

    def sized(self, counts):
        """Three findings whose summaries have the given word counts (first, second, third)."""
        for ident, slug, count in zip((FIRST, SECOND, THIRD), (SLUG, 'cedar-second', 'cedar-third'), counts):
            self.save(dict(json.loads((self.findings / (slug + '.json')).read_text()), summary=words(count, slug[-3:])))

    def counts(self, outlet=None):
        """Word counts of the letter keeping the first one, two and three findings."""
        model = load_content(self.root)
        by_id = {f['id']: f for f in model['findings']}
        ordered = [by_id[i] for i in (FIRST, SECOND, THIRD)]
        meeting = press._next_letter_meeting(model, ordered, NOW)
        return [letter_word_count(letter_text(ordered[:n], model['site'], meeting, outlet)) for n in (1, 2, 3)]


class LetterCliTests(LetterBase):
    def test_first_person_cli_default_and_outlet(self):
        outside = tree(self.root / 'content'), tree(self.root / 'public')
        result = self.cli('--finding', FIRST, '--outlet', '  Cedar Courier  ', '--date', DAY)
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout[:result.stdout.rindex('}') + 1])
        path = self.root / summary['path']
        self.assertEqual(summary['path'], f'kit/press/letters/{DAY}-{SLUG}.md')
        text = path.read_text(encoding='utf-8')
        self.assertEqual(summary, {'path': summary['path'], 'findings': [FIRST], 'dropped_findings': [],
                                   'words': letter_word_count(text), 'word_limit': 250, 'kept_edited': False,
                                   'sent': False, 'review_flags': []})
        lines = result.stdout.rstrip().splitlines()
        self.assertEqual(lines[-2], f"Letter word count: {summary['words']}/250.")
        self.assertEqual(lines[-1], 'Private letter draft written. Nothing was sent.')
        self.assertTrue(text.startswith('<!-- DRAFT: not sent. Review before any use.'))
        for item in ('To the editor of Cedar Courier:', 'I am writing to ask readers to consider these published records.',
                     self.first['summary'], f'(finding {FIRST})', 'Corrected 2026-02-03.', f'{BASE}/findings/{SLUG}.html',
                     f'I encourage readers to visit the campaign signup page: {BASE}/#participate.',
                     '[YOUR NAME]', '[YOUR CITY]'):
            self.assertIn(item, text)
        for private in (self.first['body_md'], self.first['sources'][0]['sha256'], self.first['author'],
                        self.first['corrections'][0]['note']):
            self.assertNotIn(private, text)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((path.parent / '.written.json').stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()),
                         sorted(['agency-cards.json', 'campaign.json', 'content', 'kit', 'map-config.json', 'README.md']))
        self.assertEqual((tree(self.root / 'content'), tree(self.root / 'public')), outside)

    def test_flags_go_to_stderr_and_cli_refusals(self):
        self.save(dict(self.first, summary='The sample department is non-compliant with the access-record rule.'))
        result = self.cli('--finding', FIRST, '--date', DAY)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stderr, r'(?m)^review-required: press/letters/.*legal-claim wording \(non-compliant\)$')
        self.assertNotIn('sample department', result.stderr + result.stdout.split('"review_flags"')[1].lower())
        for args, code in ((['--finding', FIRST, '--words', '149'], 1), (['--finding', FIRST, '--words', '401'], 1),
                           (['--finding', FIRST, '--words', 'many'], 2), ([], 1),
                           (['--finding', FIRST, '--outlet', ' '], 1)):
            shutil.rmtree(self.root / 'kit', ignore_errors=True)
            with self.subTest(args=args):
                bad = self.cli(*args, '--date', DAY)
                self.assertEqual(bad.returncode, code, bad.stderr)
                self.assertFalse((self.root / 'kit').exists())

    def test_outlet_is_plain_trimmed_text(self):
        for bad in ('', '   ', 'Two\nlines', 'tab\there', 'bell\x07', 'c1\x85control', 'sep\u2028arator', 'x' * 121, 5):
            with self.subTest(outlet=bad):
                self.refuses('--outlet', outlet=bad)
        result = self.letter(outlet='x' * 120)
        self.assertIn('To the editor of ' + 'x' * 120 + ':', self.read(result))
        result = self.letter(outlet='../../etc/passwd')
        self.assertEqual(result['path'], f'kit/press/letters/{DAY}-{SLUG}.md')
        self.assertIn('To the editor of ../../etc/passwd:', self.read(result))
        self.assertIn('To the editor:', self.read(self.letter(outlet=None)))


class LetterSelectionTests(LetterBase):
    def test_order_duplicates_and_published_gate(self):
        result = self.letter([SECOND, FIRST, SECOND])
        text = self.read(result)
        self.assertEqual(result['findings'], [SECOND, FIRST])
        self.assertTrue(result['path'].endswith('-cedar-second.md'))
        self.assertLess(text.index('Second summary.'), text.index(self.first['summary']))
        self.assertEqual(text.count('Second summary.'), 1)
        self.assertEqual(len(self.letter([FIRST, SECOND, THIRD])['findings']), 3)
        self.assertEqual(len(self.letter([FIRST, SECOND, THIRD, FIRST, SECOND])['findings']), 3)
        shutil.rmtree(self.root / 'kit')
        self.refuses('at least one', [])
        self.refuses('at most 3', [FIRST, SECOND, THIRD, FOURTH])
        self.refuses('finding id text', [FIRST, 7])
        with self.assertRaisesRegex(ValueError, 'finding id text'):
            build_letter(self.root, FIRST, DAY, now=NOW)

    def test_every_bad_id_is_named_and_nothing_is_written(self):
        draft = dict(self.first, id='draft-id', slug='draft-finding', state='draft')
        self.save(draft)
        self.refuses('draft-id is not a published finding.*missing-id is not a published finding',
                     ['draft-id', 'missing-id'])
        (self.findings / 'draft-finding.json').unlink()
        self.refuses('missing-id is not a published finding', [FIRST, 'missing-id'])
        self.save(dict(self.first, id='gone-id', slug='gone-finding', state='withdrawn'))
        self.refuses("state is 'withdrawn'; only published findings", ['gone-id'])
        (self.findings / 'gone-finding.json').unlink()
        bad = {k: v for k, v in self.first.items() if k != 'summary'}
        (self.findings / (SLUG + '.json')).write_text(json.dumps(bad))
        self.refuses('summary', [SECOND])
        shutil.rmtree(self.root / 'content')
        self.refuses('content/ directory', [FIRST])

    def test_word_values_and_dates_refused_before_anything_is_written(self):
        for bad in (149, 401, 0, -250, True, False, 250.0, '250', None, [250]):
            with self.subTest(words=bad):
                self.refuses('--words', words=bad)
        before = tree(self.root)
        for bad_day in ('2026-02-30', '26-3-1', 20260301, ''):
            with self.assertRaisesRegex(ValueError, '--date'):
                build_letter(self.root, [FIRST], bad_day, now=NOW)
        for bad_now in (datetime(2026, 5, 1), '2026-05-01', 5):
            with self.assertRaisesRegex(ValueError, 'timezone-aware'):
                build_letter(self.root, [FIRST], DAY, now=bad_now)
        self.assertEqual(tree(self.root), before)
        for ok in (150, 400):
            self.assertEqual(self.letter(words=ok)['word_limit'], ok)


class LetterBudgetTests(LetterBase):
    def test_whole_paragraph_budget_boundary(self):
        self.sized((135, 100, 100))
        one, two, three = self.counts()
        self.assertTrue(151 <= one < two < three <= 400, (one, two, three))
        full = self.letter([FIRST, SECOND, THIRD], words=three)
        self.assertEqual((full['findings'], full['dropped_findings'], full['words']), ([FIRST, SECOND, THIRD], [], three))
        drop_one = self.letter([FIRST, SECOND, THIRD], words=three - 1)
        self.assertEqual((drop_one['findings'], drop_one['dropped_findings'], drop_one['words']),
                         ([FIRST, SECOND], [THIRD], two))
        text = self.read(drop_one)
        self.assertNotIn('thi0', text)
        self.assertIn(f'findings {FIRST}, {SECOND} -->', text)
        self.assertNotIn(THIRD, text)
        self.assertEqual(letter_word_count(text), two)
        drop_two = self.letter([FIRST, SECOND, THIRD], words=two - 1)
        self.assertEqual((drop_two['findings'], drop_two['dropped_findings'], drop_two['words']),
                         ([FIRST], [SECOND, THIRD], one))
        self.refuses(f'{FIRST}.*needs {one} words.*limit is {one - 1}', [FIRST, SECOND, THIRD], words=one - 1)

    def test_no_middle_skipping_and_no_truncation(self):
        self.sized((135, 200, 5))
        result = self.letter([FIRST, SECOND, THIRD], words=250)
        self.assertEqual((result['findings'], result['dropped_findings']), ([FIRST], [SECOND, THIRD]))
        text = self.read(result)
        self.assertIn(self.first_summary(), text)
        self.assertNotIn('thi0', text)
        self.assertNotIn('sec0', text)

    def first_summary(self):
        return json.loads((self.findings / (SLUG + '.json')).read_text())['summary']

    def test_closing_and_salutation_are_recounted_each_time(self):
        self.sized((135, 100, 100))
        self.meeting('cedar-next', '2026-06-01T18:00:00-07:00')
        outlet = 'A Very Long Outlet Name Indeed'
        for kwargs in ({}, {'outlet': outlet}):
            one, two, three = self.counts(kwargs.get('outlet'))
            self.assertTrue(150 <= one - 1 and three <= 400, (one, two, three))
            kept = self.letter([FIRST, SECOND, THIRD], words=three, **kwargs)
            self.assertEqual((kept['findings'], kept['words']), ([FIRST, SECOND, THIRD], three))
            self.assertIn('cedar-next.html', self.read(kept))
            dropped = self.letter([FIRST, SECOND, THIRD], words=three - 1, **kwargs)
            self.assertEqual((dropped['findings'], dropped['words']), ([FIRST, SECOND], two))
            self.assertIn('cedar-next.html', self.read(dropped))
        self.assertGreater(self.counts(outlet)[0], self.counts()[0])

    def test_placeholder_and_unicode_count(self):
        comment = '<!-- DRAFT: not sent. Review before any use. findings a, b -->\n\n'
        self.assertEqual(letter_word_count(comment + 'one [YOUR NAME] two\n[YOUR CITY]\n'), 2)
        self.assertEqual(letter_word_count('[YOUR NAME][YOUR CITY]'), 0)
        self.assertEqual(letter_word_count('[YOUR NAME] is [YOUR NAME]'), 1)
        self.assertEqual(letter_word_count(comment + '-- — ... (finding) ! 12'), 2)
        self.assertEqual(letter_word_count('café naïve 日本語 ١٢٣ a—b'), 5)
        self.assertEqual(letter_word_count('a\u00a0b\tc\nd'), 4)
        self.assertEqual(letter_word_count('https://x.invalid/a?b=1 (finding fnd_01) 2026-01-02.'), 4)
        # Only the generated INITIAL comment is excluded; a comment-like line later in the text is body.
        self.assertEqual(letter_word_count('before it\n' + comment.strip()), 2 + 10)
        self.assertEqual(letter_word_count(''), 0)
        result = self.letter()
        self.assertEqual(result['words'], letter_word_count(self.read(result)))
        self.assertEqual(self.letter()['words'], result['words'])

    def test_unicode_summary_is_counted_and_kept_verbatim(self):
        self.save(dict(self.first, summary='Le département a publié  une  politique — « café » 日本語.'))
        text = self.read(self.letter())
        self.assertIn('Le département a publié une politique — « café » 日本語. (finding', text)


class LetterCorrectionTests(LetterBase):
    def test_latest_correction_and_no_base_url(self):
        notes = [{'date': '2026-02-03', 'note': 'Older note.'}, {'date': '2026-03-09', 'note': 'Newest note.'},
                 {'date': '2026-02-10', 'note': 'Middle note.'}]
        self.save(dict(self.first, corrections=notes))
        text = self.read(self.letter([FIRST, SECOND]))
        self.assertIn('(finding fnd_0000000000000001) Corrected 2026-03-09. https://', text)
        self.assertNotIn('Corrected 2026-02-03', text)
        self.assertNotIn('Newest note', text)
        self.assertEqual(text.count('Corrected'), 1)
        self.assertNotIn('Corrected', text.split('Second summary.')[1])
        self.site(base_url=None)
        text = self.read(self.letter([FIRST, SECOND]))
        self.assertNotIn('http', text)
        self.assertIn('I encourage readers to visit the campaign signup page: /#participate.', text)
        self.assertIn('(finding fnd_0000000000000001) Corrected 2026-03-09.\n', text)
        with self.assertRaisesRegex(ValueError, 'base_url'):
            build_press(self.root, [FIRST], DAY)
        self.assertTrue(self.letters().is_dir())
        self.assertFalse((self.root / 'kit/press' / f'{DAY}-{SLUG}.md').exists())

    def test_no_correction_has_no_marker_and_invented_text_is_absent(self):
        self.save(dict(self.first, corrections=[]))
        text = self.read(self.letter())
        for banned in ('Corrected', 'I am a resident', 'I attended', 'speak', 'register', 'deadline', 'endorse'):
            self.assertNotIn(banned, text)


class LetterMeetingTests(LetterBase):
    def closing(self, **kw):
        return self.read(self.letter(**kw)).rstrip().splitlines()[-4]

    def test_primary_agency_upcoming_selection(self):
        self.meeting('m-past', '2026-01-01T09:00:00-08:00')
        self.meeting('m-cancelled', '2026-05-02T09:00:00-07:00', status='cancelled')
        self.meeting('m-other', '2026-05-03T09:00:00-07:00', agency_id=OTHER)
        self.meeting('m-none', '2026-05-03T09:00:00-07:00', agency_id=None)
        self.assertIn(f'visit the campaign signup page: {BASE}/#participate.', self.closing())
        # Textual order disagrees with instant order: 05:00+14:00 is 15:00Z the day before 20:00-07:00.
        self.meeting('m-late-text', '2026-07-01T05:00:00+14:00')
        self.meeting('m-later', '2026-06-30T20:00:00-07:00')
        expected = ('I encourage readers to review the next Cedar Council (fictional) meeting on '
                    f'2026-07-01T05:00:00+14:00: {BASE}/meetings/m-late-text.html.')
        self.assertEqual(self.closing(), expected)
        exact = datetime(2026, 6, 30, 15, 0, tzinfo=timezone.utc)
        self.assertEqual(self.closing(now=exact), expected)
        after = exact + timedelta(seconds=1)
        self.assertIn('meetings/m-later.html', self.closing(now=after))
        self.assertIn('2026-06-30T20:00:00-07:00', self.closing(now=after))
        local = datetime(2026, 6, 30, 8, 0, tzinfo=timezone(timedelta(hours=-7)))
        self.assertEqual(self.closing(now=local), expected)
        self.assertIn('visit the campaign signup page', self.closing(now=datetime(2027, 1, 1, tzinfo=timezone.utc)))

    def test_ties_by_id_and_filename_date_does_not_choose(self):
        self.meeting('t-b', '2026-07-01T10:00:00-07:00')
        self.meeting('t-a', '2026-07-01T17:00:00+00:00')
        for day in ('2020-01-01', '2099-12-31'):
            result = build_letter(self.root, [FIRST], day, now=NOW)
            self.assertIn('meetings/t-a.html', result_text(self.root, result))
        text = result_text(self.root, self.letter())
        for banned in ('speak', 'register', 'sign up to', 'deadline', 'attend'):
            self.assertNotIn(banned, text)

    def test_first_finding_agency_decides_even_when_later_findings_differ(self):
        self.add(SECOND, 'cedar-second', 'Second', 'Second summary.', [], agency_id=OTHER)
        self.meeting('police-meet', '2026-06-01T10:00:00-07:00')
        self.meeting('sheriff-meet', '2026-05-15T10:00:00-07:00', agency_id=OTHER)
        self.assertIn('police-meet', self.read(self.letter([FIRST, SECOND])))
        self.assertIn('sheriff-meet', self.read(self.letter([SECOND, FIRST])))

    def test_no_meetings_null_agency_and_relative_link(self):
        self.assertIn('/#participate', self.closing())
        self.save(dict(self.first, agency_id=None))
        self.meeting('any-meet', '2026-06-01T10:00:00-07:00', agency_id=None)
        self.assertIn(f'{BASE}/#participate.', self.closing())
        self.save(dict(self.first, agency_id=POLICE))
        self.site(base_url=None)
        self.meeting('police-meet', '2026-06-02T10:00:00-07:00')
        self.assertEqual(self.closing(), 'I encourage readers to review the next Cedar Council (fictional) meeting on '
                                         '2026-06-02T10:00:00-07:00: /meetings/police-meet.html.')
        model = {'meetings': [{'id': 'x', 'agency_id': None, 'starts_at': '2027-01-01T00:00:00+00:00', 'status': 'scheduled'}]}
        self.assertIsNone(press._next_letter_meeting(model, [{'agency_id': None}], NOW))
        self.assertIsNone(press._next_letter_meeting(model, [], NOW))
        with self.assertRaisesRegex(ValueError, 'timezone-aware'):
            press._next_letter_meeting(model, [{'agency_id': 'a'}], datetime(2026, 1, 1))

    def test_meeting_closing_words_are_counted(self):
        plain = self.letter()['words']
        self.meeting('cedar-next', '2026-06-01T18:00:00-07:00')
        with_meeting = self.letter()
        self.assertEqual(with_meeting['words'], letter_word_count(self.read(with_meeting)))
        self.assertNotEqual(with_meeting['words'], plain)


def result_text(root, result):
    return (Path(root) / result['path']).read_text(encoding='utf-8')


class LetterKeptEditTests(LetterBase):
    def test_deterministic_reruns_edits_and_suggestions(self):
        first = self.letter()
        target = self.root / first['path']
        original = target.read_bytes()
        again = self.letter()
        self.assertEqual((again['kept_edited'], again['path']), (False, first['path']))
        self.assertEqual(target.read_bytes(), original)
        record = json.loads((self.letters() / '.written.json').read_text())
        self.assertEqual(list(record), [target.name])
        self.assertRegex(record[target.name], r'^[0-9a-f]{64}$')
        target.write_text('Organizer edit')
        suggested = self.root / 'kit/press/letters.suggested' / target.name
        for _ in range(2):
            kept = self.letter()
            self.assertTrue(kept['kept_edited'])
            self.assertEqual(kept['path'], f'kit/press/letters.suggested/{target.name}')
            self.assertEqual(target.read_text(), 'Organizer edit')
            self.assertEqual(suggested.read_bytes(), original)
        self.assertEqual(stat.S_IMODE(suggested.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(suggested.parent.stat().st_mode), 0o700)
        self.assertEqual(list(json.loads((suggested.parent / '.written.json').read_text())), [target.name])
        # A known-unedited suggestion refreshes when the finding changes.
        self.save(dict(self.first, summary='A changed published summary.'))
        refreshed = self.letter()
        self.assertIn('A changed published summary.', suggested.read_text())
        self.assertEqual(target.read_text(), 'Organizer edit')
        self.assertTrue(refreshed['kept_edited'])
        # An edited suggestion is never replaced.
        suggested.write_text('Edited suggestion')
        with self.assertRaisesRegex(ValueError, 'letters.suggested.*edited'):
            self.letter()
        self.assertEqual(suggested.read_text(), 'Edited suggestion')
        self.assertEqual(target.read_text(), 'Organizer edit')
        # An unedited original (hash matches) is refreshed in place.
        target.unlink()
        suggested.unlink()
        self.assertFalse(self.letter()['kept_edited'])

    def test_unknown_hash_is_treated_as_edited_and_release_files_are_untouched(self):
        release = build_press(self.root, [FIRST], DAY)
        release_state = tree(self.root / 'kit/press')
        target = self.letters() / f'{DAY}-{SLUG}.md'
        self.letters().mkdir(parents=True)
        target.write_text('Hand-written, no record')
        result = self.letter()
        self.assertTrue(result['kept_edited'])
        self.assertEqual(target.read_text(), 'Hand-written, no record')
        after = tree(self.root / 'kit/press')
        for key, value in release_state.items():
            self.assertEqual(after[key], value, key)
        self.assertEqual(release['path'], f'kit/press/{DAY}-{SLUG}.md')
        self.assertIn(f'{DAY}-{SLUG}.md', json.loads((self.root / 'kit/press/.written.json').read_text()))

    def test_other_records_are_kept_and_sorted(self):
        self.letter()
        later = build_letter(self.root, [SECOND], '2026-02-01', now=NOW)
        record = json.loads((self.letters() / '.written.json').read_text())
        self.assertEqual(list(record), sorted(record))
        self.assertEqual(len(record), 2)
        self.assertTrue(later['path'].endswith('2026-02-01-cedar-second.md'))

    def test_malformed_metadata_is_refused_and_safe(self):
        self.letter()
        record = self.letters() / '.written.json'
        name = f'{DAY}-{SLUG}.md'
        good = 'a' * 64
        cases = {
            'not json': b'{nope',
            'not utf8': b'\xff\xfe',
            'list': b'[]',
            'hash type': json.dumps({name: 5}).encode(),
            'uppercase hash': json.dumps({name: 'A' * 64}).encode(),
            'short hash': json.dumps({name: 'a' * 63}).encode(),
            'path name': json.dumps({'../x.md': good}).encode(),
            'slash name': json.dumps({'a/b.md': good}).encode(),
            'not md': json.dumps({'x.txt': good}).encode(),
            'duplicate key': ('{"%s": "%s", "%s": "%s"}' % (name, good, name, good)).encode(),
            'oversized': b'{"x.md": "' + b'a' * (1 << 20) + b'"}',
            'deep': b'[' * 50000,
        }
        draft = (self.root / f'kit/press/letters/{name}').read_bytes()
        for label, data in cases.items():
            with self.subTest(label):
                record.write_bytes(data)
                with self.assertRaisesRegex(ValueError, r'letters/\.written\.json'):
                    self.letter()
                self.assertEqual(record.read_bytes(), data)
                self.assertEqual((self.root / f'kit/press/letters/{name}').read_bytes(), draft)
        record.unlink()
        self.assertTrue(self.letter()['kept_edited'])  # missing record: no known generated hash, so the file is kept

    def test_malformed_suggestion_metadata_refuses(self):
        first = self.letter()
        (self.root / first['path']).write_text('Organizer edit')
        self.letter()
        record = self.root / 'kit/press/letters.suggested/.written.json'
        record.write_text('[]')
        before = tree(self.root)
        with self.assertRaisesRegex(ValueError, r'letters\.suggested/\.written\.json'):
            self.letter()
        self.assertEqual(tree(self.root), before)


class LetterSafetyTests(LetterBase):
    def setUp(self):
        super().setUp()
        self.outside = self.top / 'outside'
        self.outside.mkdir()
        (self.outside / 'sentinel').write_text('untouched')
        self.name = f'{DAY}-{SLUG}.md'

    def assertOutsideUntouched(self):
        self.assertEqual(tree(self.outside), {'sentinel': b'untouched'})

    def link(self, path, target=None):
        path.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target or self.outside, path)

    def test_symlinked_ancestors_refuse(self):
        for ancestor in ('kit', 'kit/press', 'kit/press/letters'):
            with self.subTest(ancestor=ancestor):
                shutil.rmtree(self.root / 'kit', ignore_errors=True)
                self.link(self.root / ancestor)
                before = tree(self.root)
                with self.assertRaisesRegex(ValueError, 'symlink'):
                    self.letter()
                self.assertEqual(tree(self.root), before)
                self.assertOutsideUntouched()
                (self.root / ancestor).unlink()

    def test_symlinked_or_special_suggestion_ancestor_refuses_when_needed(self):
        first = self.letter()
        (self.root / first['path']).write_text('Organizer edit')
        self.link(self.root / 'kit/press/letters.suggested')
        before = tree(self.root)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.letter()
        self.assertEqual(tree(self.root), before)
        self.assertOutsideUntouched()
        (self.root / 'kit/press/letters.suggested').unlink()
        (self.root / 'kit/press/letters.suggested').write_text('a file')
        with self.assertRaisesRegex(ValueError, 'not a folder'):
            self.letter()

    def test_file_where_a_folder_belongs_refuses(self):
        (self.root / 'kit').mkdir()
        (self.root / 'kit/press').write_text('a file')
        self.refuses('not a folder')

    def test_symlink_directory_and_fifo_targets_are_never_followed(self):
        (self.letters()).mkdir(parents=True)
        target = self.letters() / self.name
        suggested = self.root / 'kit/press/letters.suggested' / self.name
        for label, make in (('symlink', lambda: os.symlink(self.outside / 'sentinel', target)),
                            ('dangling', lambda: os.symlink(self.outside / 'missing', target)),
                            ('directory', lambda: target.mkdir()),
                            ('fifo', lambda: os.mkfifo(target))):
            with self.subTest(label):
                if os.path.lexists(target):
                    target.rmdir() if target.is_dir() and not target.is_symlink() else target.unlink()
                shutil.rmtree(suggested.parent, ignore_errors=True)
                make()
                result = self.letter()
                self.assertTrue(result['kept_edited'])
                self.assertEqual(result['path'], f'kit/press/letters.suggested/{self.name}')
                self.assertTrue(suggested.is_file())
                self.assertTrue(os.path.lexists(target))
                self.assertOutsideUntouched()

    def test_symlink_suggestion_and_record_links_refuse(self):
        self.letters().mkdir(parents=True)
        (self.letters() / self.name).write_text('edit')
        self.link(self.root / 'kit/press/letters.suggested' / self.name, self.outside / 'sentinel')
        before = tree(self.root)
        with self.assertRaisesRegex(ValueError, 'letters.suggested'):
            self.letter()
        self.assertEqual(tree(self.root), before)
        self.assertOutsideUntouched()
        (self.root / 'kit/press/letters.suggested' / self.name).unlink()
        for label, make in (('symlink', lambda p: os.symlink(self.outside / 'sentinel', p)),
                            ('directory', lambda p: p.mkdir()), ('fifo', os.mkfifo)):
            for folder in ('letters', 'letters.suggested'):
                with self.subTest(label=label, folder=folder):
                    record = self.root / 'kit/press' / folder / '.written.json'
                    record.parent.mkdir(parents=True, exist_ok=True)
                    if os.path.lexists(record):
                        record.rmdir() if record.is_dir() and not record.is_symlink() else record.unlink()
                    make(record)
                    before = tree(self.root)
                    with self.assertRaisesRegex(ValueError, r'\.written\.json'):
                        self.letter()
                    self.assertEqual(tree(self.root), before)
                    self.assertOutsideUntouched()
                    record.rmdir() if record.is_dir() and not record.is_symlink() else record.unlink()

    def test_write_failures_leave_the_old_state(self):
        with mock.patch.object(press, '_write_text', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.letter()
        self.assertFalse(any(p.is_file() for p in self.root.joinpath('kit').rglob('*')))
        first = self.letter()
        record = self.letters() / '.written.json'
        before = record.read_bytes()
        with mock.patch.object(press, '_write_json', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                build_letter(self.root, [SECOND], DAY, now=NOW)
        self.assertEqual(record.read_bytes(), before)
        orphan = self.letters() / f'{DAY}-cedar-second.md'
        self.assertTrue(orphan.is_file())  # written but untracked, so a rerun treats it as edited and keeps it
        self.assertTrue(build_letter(self.root, [SECOND], DAY, now=NOW)['kept_edited'])
        self.assertEqual(first['path'], f'kit/press/letters/{self.name}')
        leftovers = [p.name for p in self.letters().iterdir() if p.name.endswith('.tmp')]
        self.assertEqual(leftovers, [])


class LetterLegalGuardTests(LetterBase):
    def test_legal_guard_is_alert_only_and_offline(self):
        clean = self.letter()
        self.assertEqual(clean['review_flags'], [])
        accusation = 'The sample department is non-compliant with the access-record rule.'
        self.save(dict(self.first, summary=accusation))
        flagged = self.letter()
        self.assertEqual(flagged['review_flags'],
                         [f"review-required: press/letters/{DAY}-{SLUG}.md: legal-claim wording (non-compliant)"])
        self.assertIn(accusation, self.read(flagged))  # alert-only: the text is kept, only named by rule
        self.assertNotIn('sample department', json.dumps(flagged['review_flags']).lower())
        # The outlet and the closing are part of the scanned visible body.
        self.save(self.first)
        self.assertTrue(self.letter(outlet='The sample department violated the access law')['review_flags'])
        # Only the retained body is scanned: the dropped paragraph's wording is not flagged.
        self.sized((135, 200, 5))
        self.save(dict(json.loads((self.findings / 'cedar-second.json').read_text()), summary=accusation + ' ' + words(190, 'sec')))
        dropped = self.letter([FIRST, SECOND], words=250)
        self.assertEqual((dropped['dropped_findings'], dropped['review_flags']), ([SECOND], []))

    def test_guard_failure_is_a_review_flag_not_a_silent_success(self):
        with mock.patch.object(press, 'legal_notes', side_effect=RuntimeError('broken')):
            result = self.letter()
        self.assertEqual(result['review_flags'], [f'review-required: press/letters/{DAY}-{SLUG}.md: '
                                                  'legal-claim wording (guard-error)'])
        self.assertTrue((self.root / result['path']).is_file())
        with mock.patch('campaign_tool.legal_claims.SENTENCE_SPLIT', None):
            flags = self.letter()['review_flags']
        self.assertEqual(len(flags), 1)
        self.assertIn('guard-error', flags[0])

    def test_cap_is_enforced_and_content_is_unchanged(self):
        self.save(dict(self.first, summary=words(500, 'big')))
        before = tree(self.root)
        with self.assertRaisesRegex(ValueError, f'{FIRST} needs [0-9]+ words.*limit is 400'):
            self.letter(words=400)
        self.assertEqual(tree(self.root), before)

    def test_nothing_is_sent_or_published(self):
        source = (REPO / 'campaign_tool/press.py').read_text()
        for banned in ('smtplib', 'socket', 'urllib', 'requests', 'http.client', 'subprocess'):
            self.assertNotIn('import ' + banned, source)
        content, public = tree(self.root / 'content'), tree(self.root / 'public')
        self.assertIs(self.letter()['sent'], False)
        self.assertEqual((tree(self.root / 'content'), tree(self.root / 'public')), (content, public))
        self.assertFalse((self.root / 'public').exists())


if __name__ == '__main__':
    unittest.main()
