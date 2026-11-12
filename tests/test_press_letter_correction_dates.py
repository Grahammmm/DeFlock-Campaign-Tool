"""Private letter correction markers name the latest calendar day, whatever the admitted spelling (RF1721-1)."""
import hashlib
import itertools
import json
import os
import stat
import unittest
from pathlib import Path

from campaign_tool.content import ContentError
from campaign_tool.press import build_press, letter_word_count
from tests import test_press_letter as base

FIRST, SECOND, THIRD, SLUG, DAY = base.FIRST, base.SECOND, base.THIRD, base.SLUG, base.DAY


def notes(*dates):
    return [{'date': d, 'note': f'Private correction note {n}.'} for n, d in enumerate(dates)]


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class CorrectionDayTests(base.LetterBase):
    def test_latest_calendar_day_beats_month_and_day_lexical_decoys(self):
        cases = (
            (['2026-2-3', '2026-03-09', '2026-02-10'], '2026-03-09'),   # month decoy: '2026-2-3' > '2026-03-09'
            (['2026-03-9', '2026-03-10'], '2026-03-10'),                # day decoy: '2026-03-9' > '2026-03-10'
            (['2026-3-9', '2026-3-10', '2026-03-8'], '2026-03-10'),
            (['2026-9-30', '2027-01-01'], '2027-01-01'),
        )
        for dates, expected in cases:
            for order in itertools.permutations(dates):
                with self.subTest(dates=order):
                    self.save(dict(self.first, corrections=notes(*order)))
                    text = self.read(self.letter())
                    self.assertIn(f'(finding {FIRST}) Corrected {expected}. https://', text)
                    self.assertEqual(text.count('Corrected'), 1)
                    # The real CLI agrees with the API.
                    result = self.cli('--finding', FIRST, '--date', DAY)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    installed = (self.root / f'kit/press/letters/{DAY}-{SLUG}.md').read_text(encoding='utf-8')
                    self.assertEqual(installed, text)

    def test_equivalent_spellings_and_duplicate_days_have_identical_marker_bytes(self):
        spellings = (['2026-03-09'], ['2026-3-09'], ['2026-03-9'], ['2026-3-9'],
                     ['2026-3-9', '2026-03-09', '2026-3-09'], ['2026-02-03', '2026-2-3', '2026-3-9'])
        outputs = set()
        for dates in spellings:
            with self.subTest(dates=dates):
                saved = notes(*dates)
                self.save(dict(self.first, corrections=saved))
                text = self.read(self.letter())
                outputs.add(text)
                self.assertEqual(text.count('Corrected'), 1)
                self.assertIn(f'(finding {FIRST}) Corrected 2026-03-09. https://', text)
                self.assertEqual(json.loads((self.findings / (SLUG + '.json')).read_text())['corrections'], saved)
                for private in ('Private correction note', self.first['author']):
                    self.assertNotIn(private, text)
        self.assertEqual(len(outputs), 1, 'equivalent spellings must generate identical letter bytes')
        self.save(dict(self.first, corrections=[]))
        self.assertNotIn('Corrected', self.read(self.letter()))

    def test_leap_month_year_and_four_digit_year_boundaries(self):
        cases = (
            (['2028-2-29', '2028-03-1'], '2028-03-01'),
            (['2028-3-1', '2028-02-29'], '2028-03-01'),
            (['2026-9-30', '2026-10-1'], '2026-10-01'),
            (['2026-12-31', '2027-1-1'], '2027-01-01'),
            (['2026-3-9', '9999-1-2', '9999-01-1'], '9999-01-02'),
        )
        for dates, expected in cases:
            with self.subTest(dates=dates):
                self.save(dict(self.first, corrections=notes(*dates)))
                self.assertIn(f'(finding {FIRST}) Corrected {expected}. ', self.read(self.letter()))
        # A low year keeps four digits.
        self.save(dict(self.first, published_at='0900-01-01T00:00:00+00:00', updated_at=None,
                       corrections=notes('0900-1-2', '0900-01-10', '0900-1-9')))
        self.assertIn(f'(finding {FIRST}) Corrected 0900-01-10. ', self.read(self.letter()))

    def test_each_retained_finding_uses_its_own_latest_day_with_or_without_base(self):
        self.save(dict(self.first, corrections=notes('2026-2-3', '2026-03-09')))
        self.save(dict(json.loads((self.findings / 'cedar-second.json').read_text()), corrections=[]))
        self.save(dict(json.loads((self.findings / 'cedar-third.json').read_text()),
                       corrections=notes('2026-03-9', '2026-03-10')))
        for with_base in (True, False):
            if not with_base:
                self.site(base_url=None)
            with self.subTest(with_base=with_base):
                result = self.letter([FIRST, SECOND, THIRD])
                text = self.read(result)
                self.assertEqual(result['findings'], [FIRST, SECOND, THIRD])
                self.assertIn(f'(finding {FIRST}) Corrected 2026-03-09.', text)
                self.assertNotIn('Corrected', text.split(f'(finding {SECOND})')[1].split(f'(finding {THIRD})')[0])
                self.assertIn(f'(finding {THIRD}) Corrected 2026-03-10.', text)
                self.assertEqual(text.count('Corrected'), 2)
                self.assertEqual(('http' in text), with_base)
        # A dropped finding leaves no marker and no ID.
        self.site(base_url='https://cedar-records.example.invalid')
        self.sized((100, 60, 60))
        limit = self.counts()[1]
        result = self.letter([FIRST, SECOND, THIRD], words=limit)
        text = self.read(result)
        self.assertEqual((result['findings'], result['dropped_findings']), ([FIRST, SECOND], [THIRD]))
        self.assertNotIn(THIRD, text)
        self.assertNotIn('2026-03-10', text)

    def test_calendar_marker_preserves_exact_word_budget_boundaries(self):
        self.sized((90, 60, 60))
        for first, second in ((['2026-03-09'], ['2026-03-10']), (['2026-2-3', '2026-03-09'], ['2026-03-9', '2026-03-10'])):
            self.save(dict(json.loads((self.findings / (SLUG + '.json')).read_text()), corrections=notes(*first)))
            self.save(dict(json.loads((self.findings / 'cedar-second.json').read_text()), corrections=notes(*second)))
            counts = self.counts()
            with self.subTest(first=first):
                self.assertTrue(150 <= counts[1] <= 400 and counts[0] < counts[1] < counts[2], counts)
                at_limit = self.letter([FIRST, SECOND, THIRD], words=counts[1])
                self.assertEqual((at_limit['findings'], at_limit['dropped_findings'], at_limit['words']),
                                 ([FIRST, SECOND], [THIRD], counts[1]))
                over = self.letter([FIRST, SECOND, THIRD], words=counts[1] - 1)
                self.assertEqual((over['findings'], over['dropped_findings']), ([FIRST], [SECOND, THIRD]))
                self.assertEqual(over['words'], counts[0])
        self.sized((900, 60, 60))
        tree_before = base.tree(self.root)
        with self.assertRaisesRegex(ValueError, 'the first finding .* needs'):
            self.letter([FIRST, SECOND], words=400)
        self.assertEqual(base.tree(self.root), tree_before, 'a first-only overflow writes nothing')

    def test_original_and_suggested_refresh_hash_the_corrected_bytes(self):
        self.save(dict(self.first, corrections=notes('2026-2-3', '2026-03-09')))
        result = self.letter()
        target = self.root / result['path']
        self.assertIn('Corrected 2026-03-09.', target.read_text(encoding='utf-8'))
        record_path = self.letters() / '.written.json'
        self.assertEqual(json.loads(record_path.read_text()), {target.name: sha(target)})
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        # An organizer edit keeps its bytes, mode and mtime; the corrected suggestion is hashed exactly.
        target.write_text('Organizer edit')
        os.utime(target, (1_700_000_000, 1_700_000_000))
        before = (target.read_bytes(), stat.S_IMODE(target.stat().st_mode), target.stat().st_mtime_ns)
        kept = self.letter()
        suggested = self.root / kept['path']
        self.assertTrue(kept['kept_edited'])
        self.assertEqual((target.read_bytes(), stat.S_IMODE(target.stat().st_mode), target.stat().st_mtime_ns), before)
        self.assertIn('Corrected 2026-03-09.', suggested.read_text(encoding='utf-8'))
        self.assertEqual(stat.S_IMODE(suggested.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(suggested.parent.stat().st_mode), 0o700)
        srecord = suggested.parent / '.written.json'
        self.assertEqual(json.loads(srecord.read_text()), {target.name: sha(suggested)})
        # A generated suggestion refreshes when the latest day changes; unrelated record entries stay.
        other = 'x-other.md'
        srecord.write_text(json.dumps({other: 'a' * 64, target.name: sha(suggested)}))
        self.save(dict(self.first, corrections=notes('2026-2-3', '2026-03-09', '2026-3-10')))
        refreshed = self.root / self.letter()['path']
        self.assertIn('Corrected 2026-03-10.', refreshed.read_text(encoding='utf-8'))
        self.assertEqual(json.loads(srecord.read_text()), {other: 'a' * 64, target.name: sha(refreshed)})
        # An edited suggestion is refused and left untouched.
        refreshed.write_text('Edited suggestion')
        state = (base.tree(self.root / 'kit'))
        with self.assertRaisesRegex(ValueError, 'letters.suggested.*edited'):
            self.letter()
        self.assertEqual(base.tree(self.root / 'kit'), state)

    def test_actual_cli_reports_count_and_never_sends_or_rewrites_content(self):
        self.save(dict(self.first, corrections=notes('2026-2-3', '2026-03-9', '2026-03-10', '2026-3-2')))
        outside = base.tree(self.root / 'content'), base.tree(self.root / 'public')
        result = self.cli('--finding', FIRST, '--date', DAY)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        summary = json.loads(result.stdout[:result.stdout.rindex('}') + 1])
        path = self.root / summary['path']
        text = path.read_text(encoding='utf-8')
        self.assertIn(f'(finding {FIRST}) Corrected 2026-03-10.', text)
        self.assertEqual((summary['words'], summary['sent']), (letter_word_count(text), False))
        self.assertEqual(result.stdout.rstrip().splitlines()[-2], f"Letter word count: {summary['words']}/250.")
        self.assertEqual(result.stdout.rstrip().splitlines()[-1], 'Private letter draft written. Nothing was sent.')
        self.assertEqual(json.loads((path.parent / '.written.json').read_text()), {path.name: sha(path)})
        self.assertEqual((base.tree(self.root / 'content'), base.tree(self.root / 'public')), outside)
        for private in ('Private correction note', self.first['author'], self.first['body_md']):
            self.assertNotIn(private, text + result.stdout + result.stderr)

    def test_invalid_correction_admission_and_canonical_release_are_unchanged(self):
        for bad in ('2026-02-30', '2026-13-01', '2026-3-32', '2026/03/09', '', 20260309, None):
            with self.subTest(date=bad):
                self.save(dict(self.first, corrections=[{'date': bad, 'note': 'Note.'}]))
                before = base.tree(self.root)
                with self.assertRaises(ContentError):
                    self.letter()
                self.assertEqual(base.tree(self.root), before, 'a refused letter must write nothing')
        # Canonical padded dates still give the same marker; the release keeps the stored spelling.
        self.save(dict(self.first, corrections=notes('2026-02-03', '2026-03-09')))
        self.assertIn(f'(finding {FIRST}) Corrected 2026-03-09. ', self.read(self.letter()))
        canonical = build_press(self.root, [FIRST], DAY)
        release = (self.root / canonical['path']).read_text(encoding='utf-8')
        self.assertIn('- 2026-02-03: Private correction note 0.', release)
        self.save(dict(self.first, corrections=notes('2026-2-3', '2026-3-9')))
        loose = build_press(self.root, [FIRST], DAY)
        self.assertIn('- 2026-2-3: Private correction note 0.', (self.root / loose['path']).read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
