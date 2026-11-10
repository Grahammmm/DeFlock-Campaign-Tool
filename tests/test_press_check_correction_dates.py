"""RF0645-1: press check compares correction dates as calendar days, not strings. Synthetic fixtures only.

A correction date the finding loader accepts may be unpadded ('2026-2-3'); it must neither make a draft stale when it is not
after the filename date, nor be reported twice or out of order. Run: python3 -B -m unittest tests.test_press_check_correction_dates -q
"""
import itertools
import json
import os
import shlex
import socket
import unittest
from datetime import date
from unittest import mock

from campaign_tool import findings as findings_mod
from campaign_tool import press, press_check, today
from tests.test_press_check import DAY, FIRST, MEETING, NOW, SECOND, SLUG, THIRD, Base, snapshot

HEADER = '<!-- DRAFT: not sent. Review before any use. findings %s -->\n\nbody\n'
CANARY = 'NOTE-CANARY'
AS_OF = date(2026, 5, 1)


class CorrectionDayBase(Base):
    def corrections(self, ident, *dates, **extra):
        slug = SLUG if ident == FIRST else ('cedar-second' if ident == SECOND else 'cedar-third')
        self.add(ident, slug, [{'date': d, 'note': CANARY} for d in dates], **extra)

    def draft_for(self, ids, day=DAY, folder='kit/press', tag='d'):
        rel = f'{folder}/{day}-{tag}.md'
        self.put(rel, HEADER % ', '.join(ids))
        return rel

    def reasons(self, rel, now=NOW):
        return self.rows(now)[rel]['reasons']

    def press_items(self):
        return [i for i in today.todo(self.root, AS_OF) if i['kind'] == 'outreach_stale' and i['what'].startswith('press:')]


class RepresentationTests(CorrectionDayBase):
    def test_padded_and_unpadded_older_or_equal_days_do_not_stale(self):
        self.corrections(FIRST, '2026-02-03', '2026-2-3', '2026-02-3', '2026-2-03', '2026-03-01', '2026-3-1', '2026-03-1', '2026-3-01')
        self.corrections(SECOND, '2026-3-2', '2026-03-2', '2026-3-02', '2026-03-02', '2026-03-10', '2026-3-10', '2026-2-3')
        self.corrections(THIRD, '2026-9-30', '2026-10-1', '2026-10-01', '2026-2-20')
        # one draft per finding; the ten-day and October filenames separate day-width from month-width spellings
        paths = [self.draft_for([FIRST]), self.draft_for([SECOND], day='2026-03-10', tag='wide'),
                 self.draft_for([THIRD], day='2026-10-01', tag='autumn')]
        before = snapshot(self.root)
        report = self.check()
        self.assertEqual([(r['path'], r['status'], r['reasons']) for r in report['drafts']], [(p, 'ok', []) for p in paths])
        self.assertEqual(report['problems'], 0)
        self.assertEqual(self.press_items(), [])
        for flags in ((), ('--json',)):
            done = self.cli(*flags)
            self.assertEqual((done.returncode, done.stderr), (0, ''))
        self.assertEqual(json.loads(self.cli('--json').stdout), {'drafts': [
            {'path': p, 'kind': 'draft', 'status': 'ok', 'findings': [i], 'reasons': []}
            for p, i in zip(paths, (FIRST, SECOND, THIRD))], 'problems': 0})
        self.assertEqual(snapshot(self.root), before)

    def test_later_days_are_chronological_unique_and_canonical(self):
        rel = self.draft_for([FIRST])
        spellings = ['2026-4-2', '2026-04-10', '2026-04-02']
        for order in itertools.permutations(spellings):
            self.corrections(FIRST, *order, '2026-2-20')
            row = self.rows()[rel]
            self.assertEqual(row['status'], 'stale', order)
            self.assertEqual(row['reasons'], [{'code': 'corrected', 'finding_id': FIRST, 'dates': ['2026-04-02', '2026-04-10']}], order)
        report = self.check()
        self.assertEqual(report['problems'], 1)
        self.assertIn(f'{rel}: stale (findings {FIRST}): corrected {FIRST} after-filename-date 2026-04-02,2026-04-10',
                      press_check.format_text(report))
        self.assertNotIn(CANARY, json.dumps(report) + press_check.format_text(report))

    def test_real_calendar_boundaries_and_year_endpoints(self):
        cases = (  # (published, filename day, corrections, expected later days)
            ('2028-01-01T00:00:00+00:00', '2028-02-29', ['2028-2-28', '2028-2-29', '2028-02-29'], []),
            ('2028-01-01T00:00:00+00:00', '2028-02-29', ['2028-2-28', '2028-3-1', '2028-03-01'], ['2028-03-01']),
            ('2028-01-01T00:00:00+00:00', '2028-02-28', ['2028-2-29', '2028-02-28'], ['2028-02-29']),
            ('2026-01-01T00:00:00+00:00', '2026-12-31', ['2026-12-31', '2026-12-1'], []),
            ('2026-01-01T00:00:00+00:00', '2026-12-31', ['2027-1-1', '2027-01-01'], ['2027-01-01']),
            ('2026-01-01T00:00:00+00:00', '2026-10-15', ['2026-10-6', '2026-9-30'], []),
            ('2026-01-01T00:00:00+00:00', '2026-02-03', ['2026-2-4', '2026-2-3'], ['2026-02-04']),
            ('2026-01-01T00:00:00+00:00', '9999-12-30', ['9999-12-30', '9999-12-1'], []),
            ('2026-01-01T00:00:00+00:00', '9999-12-30', ['9999-12-31', '9999-12-30'], ['9999-12-31']))
        for published, day, dates, expected in cases:
            with self.subTest(day=day, dates=dates):
                self.corrections(FIRST, *dates, published_at=published)
                rel = self.draft_for([FIRST], day=day, tag='edge')
                want = [{'code': 'corrected', 'finding_id': FIRST, 'dates': expected}] if expected else []
                self.assertEqual(self.reasons(rel), want)
                self.assertEqual(self.rows()[rel]['status'], 'stale' if expected else 'ok')
                (self.root / rel).unlink()


class PreservationTests(CorrectionDayBase):
    def test_canonical_fixture_keeps_exact_report_and_text(self):
        self.corrections(SECOND, '2026-04-02', '2026-03-01', '2026-04-01', '2026-04-02', '2026-02-20')
        self.corrections(THIRD, '2026-05-10', '2026-03-02', '2026-03-01')
        self.corrections(FIRST, '2026-02-03')
        rel = self.draft_for([FIRST, SECOND, THIRD, SECOND])
        report = self.check()
        self.assertEqual(report, {'drafts': [{'path': rel, 'kind': 'draft', 'status': 'stale', 'findings': [FIRST, SECOND, THIRD], 'reasons': [
            {'code': 'corrected', 'finding_id': SECOND, 'dates': ['2026-04-01', '2026-04-02']},
            {'code': 'corrected', 'finding_id': THIRD, 'dates': ['2026-03-02', '2026-05-10']}]}], 'problems': 1})
        self.assertEqual(press_check.format_text(report), f'{rel}: stale (findings {FIRST}, {SECOND}, {THIRD}): '
                         f'corrected {SECOND} after-filename-date 2026-04-01,2026-04-02; '
                         f'corrected {THIRD} after-filename-date 2026-03-02,2026-05-10')
        # a header that drops a corrected finding does not cite it
        dropped = self.draft_for([FIRST], tag='dropped')
        self.assertEqual(self.rows()[dropped]['status'], 'ok')

    def test_bad_correction_stays_unreadable_without_value_leak(self):
        rel = self.draft_for([FIRST])
        bad = ['2026-02-30', '2026-13-01', 'CANARY-DATE', '', '2026/03/05', 20260305, None, ['2026-04-01'], {'d': 'CANARY-DATE'}]
        for value in bad:
            with self.subTest(value=repr(value)):
                self.add(FIRST, SLUG, [{'date': value, 'note': CANARY}])
                report = self.check()
                by = {row['path']: row for row in report['drafts']}
                self.assertEqual(by[rel]['status'], 'not_checked')
                self.assertEqual(by[rel]['reasons'], [{'code': 'not_checked', 'message': 'finding file invalid or duplicated'}])
                self.assertEqual(by[f'content/findings/{SLUG}.json']['reasons'][0]['message'], 'finding fails the publish checks')
                shown = json.dumps(report) + press_check.format_text(report)
                for secret in (CANARY, 'CANARY-DATE', 'Traceback'):
                    self.assertNotIn(secret, shown)
        for stray in ({'date': '2026-4-2'}, {'note': CANARY}, {'date': '2026-4-2', 'note': CANARY, 'extra': 1}):
            self.add(FIRST, SLUG, [stray])
            self.assertEqual(self.reasons(rel), [{'code': 'not_checked', 'message': 'finding file invalid or duplicated'}])
        done = self.cli()
        self.assertEqual(done.returncode, 3)
        self.assertNotIn(CANARY, done.stdout + done.stderr)
        self.assertNotIn('Traceback', done.stderr)

    def test_independent_stale_and_not_checked_reasons_survive(self):
        old = ('2026-2-3', '2026-3-1', '2026-03-1')
        for ident in (FIRST, SECOND, THIRD):
            self.corrections(ident, *old)
        draft = self.draft_for([FIRST], tag='cited')
        suggested = self.draft_for([SECOND], folder='kit/press.suggested', tag='s')
        letter = self.letter()
        suggested_letter = self.put(f'kit/press/letters.suggested/{DAY}-t.md', (self.root / letter).read_text()).relative_to(self.root).as_posix()
        signup = self.put(f'kit/press/letters/{DAY}-signup.md', HEADER % THIRD + 'I encourage readers to visit the campaign signup page: /#participate.\n')
        signup = signup.relative_to(self.root).as_posix()
        self.assertEqual(self.check()['problems'], 0)
        # cancelled and past meetings stay independent stale reasons
        self.meeting(MEETING, '2026-06-10T18:00:00-07:00', status='cancelled')
        self.assertEqual([r['code'] for r in self.reasons(letter)], ['meeting_cancelled'])
        self.assertEqual([r['code'] for r in self.reasons(suggested_letter)], ['meeting_cancelled'])
        self.meeting(MEETING, '2026-04-30T12:00:00Z', status='cancelled')
        self.assertEqual([r['code'] for r in self.reasons(letter)], ['meeting_cancelled', 'meeting_past'])
        self.meeting(MEETING, '2026-06-10T18:00:00-07:00')
        # withdrawn wins over a now-harmless correction history; unpublished stays stale
        document = json.loads(self.findings.joinpath('cedar-second.json').read_text())
        self.findings.joinpath('cedar-second.json').write_text(json.dumps(dict(document, state='draft')))
        self.assertEqual(self.reasons(suggested), [{'code': 'unpublished', 'finding_id': SECOND}])
        self.assertEqual(self.rows()[signup]['status'], 'ok')
        self.root.joinpath('private').mkdir(exist_ok=True)
        findings_mod.withdraw(self.root, FIRST, 'synthetic reason')
        self.assertEqual(self.reasons(draft), [{'code': 'withdrawn', 'finding_id': FIRST}])
        self.assertEqual(self.rows()[draft]['status'], 'stale')
        # an unavailable index and an uncertain (invalid) finding file are not_checked, never ok
        self.findings.joinpath('broken.json').write_text('{not json')
        self.assertEqual(self.reasons(suggested), [{'code': 'not_checked', 'message': 'finding index unavailable'}])
        self.findings.joinpath('broken.json').unlink()
        bad = dict(document, id=THIRD, slug='cedar-third', state='published', corrections=[{'date': 'bad', 'note': 'n'}])
        self.findings.joinpath('cedar-third.json').write_text(json.dumps(bad))
        uncertain = self.draft_for([THIRD], tag='uncertain')
        self.assertEqual(self.reasons(uncertain), [{'code': 'not_checked', 'message': 'finding file invalid or duplicated'}])
        self.assertEqual(self.rows()[uncertain]['status'], 'not_checked')

    def test_invalid_filename_date_is_still_not_checked_and_ignores_corrections(self):
        self.corrections(FIRST, '2026-4-2')
        self.corrections(SECOND, '2026-2-3')
        bad = self.draft_for([FIRST], day='2026-02-30', tag='impossible')
        none = self.put('kit/press/nodate.md', HEADER % FIRST).relative_to(self.root).as_posix()
        both = self.draft_for([SECOND, THIRD], day='2026-02-30', tag='mixed')
        self.findings.joinpath('cedar-third.json').unlink()
        filename = {'code': 'not_checked', 'message': 'missing or invalid filename date'}
        self.assertEqual(self.reasons(bad), [filename])
        self.assertEqual(self.reasons(none), [filename])
        self.assertEqual(self.reasons(both), [filename, {'code': 'unpublished', 'finding_id': THIRD}])
        self.assertEqual(self.rows()[both]['status'], 'stale')


class ConsumerTests(CorrectionDayBase):
    def test_real_cli_and_today_count_the_calendar_result(self):
        press.build_press(self.root, [FIRST], DAY)
        letter = self.letter()
        self.meeting(MEETING, '2099-06-10T18:00:00-07:00')  # the CLI reads the real clock; keep the meeting in its future
        self.corrections(FIRST, '2026-2-3', '2026-3-1')
        done = self.cli()
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, f'kit/press/{DAY}-{SLUG}.md: ok\n{letter}: ok\n', ''))
        self.assertEqual(json.loads(self.cli('--json').stdout)['problems'], 0)
        self.assertEqual(self.press_items(), [])
        # two drafts hit by later days, one untouched: the item counts rows, not days or reasons
        self.corrections(SECOND, '2026-4-2', '2026-04-02', '2026-4-10')
        self.corrections(THIRD, '2026-5-1')
        first = self.draft_for([SECOND], tag='second')
        second = self.draft_for([THIRD], tag='third')
        text, data = self.cli(), self.cli('--json')
        self.assertEqual((text.returncode, data.returncode), (3, 3))
        self.assertIn(f'{first}: stale (findings {SECOND}): corrected {SECOND} after-filename-date 2026-04-02,2026-04-10', text.stdout)
        self.assertIn(f'{second}: stale (findings {THIRD}): corrected {THIRD} after-filename-date 2026-05-01', text.stdout)
        self.assertNotIn('Traceback', text.stderr + data.stderr)
        parsed = json.loads(data.stdout)
        self.assertEqual(parsed['problems'], 2)
        self.assertEqual(parsed, self.check())
        self.assertEqual([(i['what'], i['command']) for i in self.press_items()],
                         [('press: 2 draft check problem(s); inspect before use', f'press check --directory {shlex.quote(str(self.root))}')])

    def test_read_only_offline_and_note_privacy_for_every_outcome(self):
        self.draft(), self.letter()
        self.corrections(SECOND, '2026-4-2', '2026-04-10')
        self.corrections(THIRD, '2026-2-3')
        self.draft_for([SECOND], tag='later')
        self.draft_for([THIRD], tag='older')
        before = snapshot(self.root)
        guards = [mock.patch.object(target, name, side_effect=AssertionError(f'{name} called')) for target, name in
                  ((press, 'build_press'), (press, 'build_letter'), (press, '_write_text'), (press, '_write_json'),
                   (os, 'chmod'), (os, 'unlink'), (os, 'rename'), (os, 'mkdir'), (socket, 'create_connection'))]
        for guard in guards:
            guard.start()
            self.addCleanup(guard.stop)
        report = self.check()
        for guard in guards:
            guard.stop()
        self.assertEqual(report['problems'], 1)
        shown = json.dumps(report) + press_check.format_text(report)
        done = self.cli()
        self.assertEqual(done.returncode, 3)
        self.assertNotIn(CANARY, shown + done.stdout + done.stderr)
        self.assertEqual(snapshot(self.root), before)
        # accepted noncanonical spellings stay byte-for-byte on disk
        self.assertIn('"2026-4-2"', self.findings.joinpath('cedar-second.json').read_text())
        self.assertIn('"2026-2-3"', self.findings.joinpath('cedar-third.json').read_text())


if __name__ == '__main__':
    unittest.main()
