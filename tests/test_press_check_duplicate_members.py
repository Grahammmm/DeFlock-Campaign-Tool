"""RF2311-2: press check refuses repeated JSON member names instead of keeping the last. Synthetic fixtures only.

Run: python3 -B -m unittest tests.test_press_check_duplicate_members -q
"""
import json
import unittest

from campaign_tool import press_check
from tests.test_press_check import FIRST, MEETING, NOW, SLUG, Base, snapshot

MESSAGE = 'duplicate JSON object member names'
CANARY = 'DUP-CANARY-VALUE'
FUTURE = '2099-06-10T18:00:00-07:00'
FINDING = f'content/findings/{SLUG}.json'
MEETING_PATH = f'content/meetings/{MEETING}.json'


def with_extra(text, extra):
    """Append raw `extra` members to a serialized JSON object, creating repeated names."""
    return text.rstrip()[:-1] + ', ' + extra + '}'


class DuplicateMembers(Base):
    def setUp(self):
        super().setUp()
        self.meeting(MEETING, FUTURE)
        self.draft_path = self.draft()
        self.letter_path = self.letter()

    def mutate(self, rel, extra):
        path = self.root / rel
        path.write_text(with_extra(path.read_text(encoding='utf-8'), extra), encoding='utf-8')

    def inputs(self):
        return [r for r in self.check()['drafts'] if r['kind'] == 'input']

    def refused(self, path):
        self.assertEqual(self.inputs(), [{'path': path, 'kind': 'input', 'status': 'unreadable', 'findings': [],
                                          'reasons': [{'code': 'invalid_input', 'message': MESSAGE}]}])

    def test_healthy_unique_members_stay_ok(self):
        report = self.check()
        self.assertEqual(report['problems'], 0)
        self.assertEqual({r['status'] for r in report['drafts']}, {'ok'})

    def test_duplicate_corrections_never_report_clean(self):
        for extra in ('"corrections": []', '"corrections": [{"date": "2026-04-01", "note": "x"}]'):
            self.mutate(FINDING, extra)
            report = self.check()
            self.assertEqual({r['status'] for r in report['drafts'] if r['kind'] != 'input'}, {'not_checked'})
            self.refused(FINDING)
            self.assertEqual(report['problems'], 3)
            self.add(FIRST, SLUG, [])  # restore a unique-member document
            self.assertEqual(self.check()['problems'], 0)

    def test_nested_correction_date_and_array_objects(self):
        self.add(FIRST, SLUG, [{'date': '2026-04-01', 'note': 'n'}])
        path = self.root / FINDING
        text = path.read_text(encoding='utf-8').replace('"date": "2026-04-01"', '"date": "2026-04-01", "date": "2026-02-01"')
        path.write_text(text, encoding='utf-8')
        self.refused(FINDING)

    def test_meeting_status_equal_values_and_escaped_key(self):
        for extra in ('"status": "cancelled", "status": "scheduled"', '"status": "scheduled", "status": "scheduled"',
                      '"status": "cancelled", "st\\u0061tus": "scheduled"'):
            self.meeting(MEETING, FUTURE)
            self.mutate(MEETING_PATH, extra)
            rows = {r['path']: r for r in self.check()['drafts']}
            self.assertEqual(rows[self.letter_path]['status'], 'not_checked', extra)
            self.assertEqual(rows[self.draft_path]['status'], 'ok')
            self.refused(MEETING_PATH)
            self.assertEqual(self.check()['problems'], 2)

    def test_withdrawal_id_duplicate_makes_index_unavailable(self):
        withdrawn = self.root / 'private/withdrawn'
        withdrawn.mkdir(parents=True, exist_ok=True)
        (withdrawn / 'w.json').write_text(f'{{"id": "fnd_0000000000000009", "id": "{FIRST}", "note": "{CANARY}"}}', encoding='utf-8')
        report = self.check()
        self.assertEqual({r['status'] for r in report['drafts'] if r['kind'] != 'input'}, {'not_checked'})
        self.refused('private/withdrawn/w.json')
        self.assertNotIn(CANARY, json.dumps(report))

    def test_scope_is_per_object_and_decoded_keys(self):
        withdrawn = self.root / 'private/withdrawn'
        withdrawn.mkdir(parents=True, exist_ok=True)
        path = withdrawn / 'w.json'
        path.write_text('{"id": "fnd_0000000000000009", "Id": 3, "meta": [{"a": 1}, {"a": 2}], "t": "\\"id\\": 1, \\"id\\": 2"}',
                        encoding='utf-8')
        self.assertEqual(self.inputs(), [])
        path.write_text('{"id": "fnd_0000000000000009", "meta": [{"a": 1, "a": 2}]}', encoding='utf-8')
        self.refused('private/withdrawn/w.json')

    def test_cli_exit_and_no_leak_or_write(self):
        self.mutate(FINDING, f'"corrections": [], "note": "{CANARY}", "note": "{CANARY}"')
        before = snapshot(self.top)
        for args in ((), ('--json',)):
            result = self.cli(*args)
            self.assertEqual(result.returncode, 3, result.stderr)
            self.assertIn(MESSAGE, result.stdout)
            self.assertNotIn(CANARY, result.stdout + result.stderr)
            self.assertNotIn('Traceback', result.stderr)
        self.assertEqual(snapshot(self.top), before)

    def test_other_parse_failures_keep_their_message(self):
        (self.root / FINDING).write_text('{"id": ', encoding='utf-8')
        rows = self.inputs()
        self.assertEqual(rows[0]['reasons'][0]['message'], 'file is not valid UTF-8 JSON')


if __name__ == '__main__':
    unittest.main()
