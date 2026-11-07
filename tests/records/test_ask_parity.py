"""Synthetic rule-7 acceptance: no records, model, or service calls."""
import contextlib
import copy
import csv
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from campaign_tool.records import ask_parity as parity

COMPOUND = ('Provide the configured sharing relationships in the 2022 report and '
            'identify the agencies listed in that report.')


class AskParityTests(unittest.TestCase):
    def test_seven_lost_asks_and_explanation(self):
        pairs = [
            ('Assess each field separately.', 'Assess each field.', 'separately'),
            ('State the grounds for denial.', 'State the grounds.', 'denial'),
            ('List administrative-change events.', 'List events.', 'administrative'),
            ('Provide search records if entries are not held.', 'Provide search records.', 'held'),
            ('Confirm whether systems can export audit records.', 'Confirm whether systems can export.', 'audit'),
            ('Provide configured sharing relationships.', 'Provide sharing relationships.', 'configured'),
            ('Provide records listed or identified in those documents.', 'Provide records listed.', 'identified'),
        ]
        long = '\n'.join(p[0] for p in pairs) + '\nThis explanation can be deleted.'
        rows = parity.check(parity.propose(long), '\n'.join(p[1] for p in pairs))
        self.assertEqual(len(rows), 7)
        self.assertEqual([r['status'] for r in rows], ['missing'] * 7)
        for row, (_, _, term) in zip(rows, pairs):
            self.assertIn(term, row['missing'])
        self.assertEqual(parity.exit_code(rows), 1)

    def test_atomic_qualifier_year_and_compound_controls(self):
        proposed = parity.propose(COMPOUND)
        self.assertEqual(len(proposed), 2)
        for removed in ('configured ', '2022 '):
            rows = parity.check(proposed, COMPOUND.replace(removed, ''))
            self.assertEqual(rows[0]['status'], 'missing')
            self.assertIn(removed.strip(), rows[0]['missing'])
        rows = parity.check(proposed, COMPOUND.split(' and identify')[0] + '.')
        self.assertEqual([r['status'] for r in rows], ['candidate', 'missing'])
        rows = parity.check(proposed, COMPOUND)
        self.assertEqual([r['status'] for r in rows], ['candidate'] * 2)
        self.assertEqual(parity.exit_code(rows), 2)

    def test_list_markers_please_and_each_coordination(self):
        for join in (', and ', ' and ', '; ', '; and '):
            with self.subTest(join=join):
                rows = parity.propose('1. Please provide reports' + join + 'identify contracts.\n'
                                      '- Include invoices.\n(b) State deadlines.')
                self.assertEqual(len(rows), 4)
                self.assertTrue(rows[1]['long_text'].startswith('identify'))

    def test_alternative_condition_quotes_and_whole_words(self):
        for long, short, absent in [
            ('Provide the search records, or identify them in those documents.',
             'Provide the search records.', 'or identify them in those documents'),
            ('Provide entries if entries are not held.', 'Provide entries if entries are held.',
             'if entries are not held'),
            ('Provide the "go live" date for 2022.', 'Provide the date for 2022.', 'go live'),
            ('List audit logs.', 'List auditor logs.', 'audit'),
        ]:
            with self.subTest(long=long):
                rows = parity.check(parity.propose(long), short)
                self.assertEqual(rows[0]['status'], 'missing')
                self.assertIn(absent, rows[0]['missing'])
        rows = parity.check(parity.propose('Provide audit logs.'), 'PROVIDE AUDIT LOG.')
        self.assertEqual(rows[0]['status'], 'candidate')
        for quote in ("'", '‘'):
            end = '’' if quote == '‘' else quote
            self.assertIn('go live', parity.required(f'Provide {quote}go live{end} dates.'))
        rows = parity.check(parity.propose('Provide audit logs.'), 'Provide audit. List logs.')
        self.assertEqual(rows[0]['status'], 'missing')  # No cross-unit word union.

    def test_merged_survival_and_human_confirmation(self):
        table = parity.propose('Provide audit logs. Identify sharing partners.')
        short = 'Provide audit logs and identify sharing partners.'
        rows = parity.check(table, short)
        self.assertEqual([r['short_unit'] for r in rows], [1, 1])
        self.assertEqual([r['status'] for r in rows], ['candidate', 'candidate'])
        for row in table:
            row.update(short_unit=1, confirmed_by='reviewer')
        self.assertEqual(parity.exit_code(parity.check(table, short)), 0)
        table[1]['confirmed_by'] = ''
        self.assertEqual(parity.exit_code(parity.check(table, short)), 2)
        table[1]['status'] = 'confirmed'  # Serialized statuses never supply confirmation.
        self.assertEqual(parity.exit_code(parity.check(table, short)), 2)
        table[0]['short_unit'] = ''
        self.assertEqual(parity.check(table, short)[0]['status'], 'candidate')

    def test_relaxation_requires_note_and_rewording_can_be_confirmed(self):
        table = parity.propose('Provide configured sharing relationships.')
        table[0].update(required=['sharing', 'partners'], short_unit='1', confirmed_by='G')
        with self.assertRaisesRegex(ValueError, 'relaxed_without_note'):
            parity.check(table, 'Provide sharing partners.')
        table[0]['note'] = "Reworded: sharing partners means configured sharing relationships."
        self.assertEqual(parity.check(table, 'Provide sharing partners.')[0]['status'], 'confirmed')

    def test_invalid_duplicate_stale_or_withdrawn_confirmations(self):
        table = parity.propose('Provide audit logs.')
        for changes, code in [({'required': []}, 'invalid_required'),
                              ({'short_unit': '-1'}, 'invalid_short_unit'),
                              ({'confirmed_by': 'Private Person'}, 'confirmed_by_requires_role')]:
            changed = copy.deepcopy(table)
            changed[0].update(changes)
            with self.subTest(code=code), self.assertRaisesRegex(ValueError, code):
                parity.check(changed, 'Provide audit logs.')
        with self.assertRaisesRegex(ValueError, 'duplicate_ask_id'):
            parity.check(table * 2, 'Provide audit logs.')
        with self.assertRaisesRegex(ValueError, 'empty_table'):
            parity.check([], 'Provide audit logs.')
        table[0].update(short_unit='2', confirmed_by='reviewer')
        self.assertEqual(parity.exit_code(parity.check(table, 'Provide audit logs.')), 1)
        table[0]['short_unit'] = '1'
        self.assertEqual(parity.exit_code(parity.check(table, 'Provide logs.')), 1)
        table[0]['confirmed_by'] = ''  # withdrawn confirmation cannot survive
        self.assertEqual(parity.exit_code(parity.check(table, 'Provide audit logs.')), 2)

    def test_cli_roundtrip_exit_codes_and_no_text_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            long, short, table = (root / name for name in ('long.txt', 'short.txt', 'table.tsv'))
            long.write_text(COMPOUND, encoding='utf-8')
            short.write_text(COMPOUND, encoding='utf-8')
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(parity.main(['propose', str(long)]), 0)
            table.write_text(output.getvalue(), encoding='utf-8')
            with table.open(encoding='utf-8', newline='') as handle:
                rows = list(csv.DictReader(handle, delimiter='\t'))
            def run(extra=()):
                return subprocess.run([sys.executable, '-B', '-m', 'campaign_tool.records',
                    'ask-parity', 'check', str(table), str(short), *extra],
                    capture_output=True, text=True, check=False)
            def save():
                with table.open('w', encoding='utf-8', newline='') as handle:
                    writer = csv.DictWriter(handle, parity.FIELDS, delimiter='\t')
                    writer.writeheader()
                    writer.writerows(rows)
            candidate = run()
            self.assertEqual(candidate.returncode, 2, candidate.stderr)
            self.assertNotIn(COMPOUND, candidate.stdout + candidate.stderr)
            self.assertNotIn('long_text', candidate.stdout)
            self.assertIn(rows[0]['long_text'], run(['--show-text']).stdout)
            for row in rows:
                row.update(short_unit='1', confirmed_by='reviewer')
            save()
            self.assertEqual(run().returncode, 0)
            short.write_text(COMPOUND.replace('configured ', ''), encoding='utf-8')
            missing = run()
            self.assertEqual(missing.returncode, 1)
            self.assertIn('configured', json.loads(missing.stdout.splitlines()[0])['missing'])
            rows[0]['required'] = 'invalid private text'
            save()
            invalid = run()
            self.assertEqual(invalid.returncode, 1)
            self.assertNotIn('invalid private text', invalid.stdout + invalid.stderr)


if __name__ == '__main__':
    unittest.main()
