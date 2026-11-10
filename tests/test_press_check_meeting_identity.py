"""press check: a malformed meeting document still claims the identity it names (RF0035-2); fictional content only."""
import json
import unittest

from campaign_tool import press_check
from tests.test_press_check import MEETING, NOW, Base

NOT_CHECKED = [{'code': 'not_checked', 'message': 'meeting file invalid or duplicated'}]
STAGING = 'meeting fails the staging checks'
FUTURE = '2099-06-10T18:00:00-07:00'


class MeetingIdentity(Base):
    def setUp(self):
        super().setUp()
        self.meeting(MEETING, FUTURE)
        self.letter_path = self.letter()

    def bad(self, name, ident=MEETING, **extra):
        document = {'id': ident, 'body': 'Cedar Council (fictional)', 'starts_at': FUTURE, 'agency_id': 'ca-cedar-police'}
        document.update(extra)
        if ident is None:
            del document['id']
        (self.root / 'content/meetings' / (name + '.json')).write_text(json.dumps(document), encoding='utf-8')

    def report(self):
        return press_check.check_press(self.root, now=NOW)

    def inputs(self):
        return {r['path']: r for r in self.report()['drafts'] if r['kind'] == 'input'}

    def row(self):
        return {r['path']: r for r in self.report()['drafts']}[self.letter_path]

    def test_malformed_duplicate_claim_blocks_healthy_letter_in_both_orders(self):
        for name in ('aa-copy', 'zz-copy'):
            for extra in ({'status': 'bogus'}, {'starts_at': 'not-a-time'}, {'extra_key': 1}):
                self.bad(name, **extra)
                self.assertEqual(self.row()['reasons'], NOT_CHECKED, (name, extra))
                self.assertEqual(self.row()['status'], 'not_checked')
                (self.root / 'content/meetings' / (name + '.json')).unlink()
        self.assertEqual(self.row()['status'], 'ok')

    def test_sole_invalid_explicit_id_is_uncertain_not_missing(self):
        (self.root / 'content/meetings' / (MEETING + '.json')).unlink()
        self.bad('zz-copy', status='bogus')
        self.assertEqual(self.row()['reasons'], NOT_CHECKED)
        report = self.report()
        self.assertEqual(report['problems'], 2)
        self.assertEqual(list(self.inputs()), ['content/meetings/zz-copy.json'])
        self.assertEqual(self.inputs()['content/meetings/zz-copy.json']['reasons'][0]['message'], STAGING)

    def test_mixed_collision_diagnostics_count_paths_once(self):
        self.bad('aa-copy', status='bogus')
        rows = self.inputs()
        self.assertEqual(set(rows), {'content/meetings/aa-copy.json', f'content/meetings/{MEETING}.json'})
        self.assertEqual(rows['content/meetings/aa-copy.json']['reasons'][0]['message'], STAGING)
        self.assertEqual(rows[f'content/meetings/{MEETING}.json']['reasons'][0]['message'], 'duplicate id')
        self.assertEqual(self.report()['problems'], 3)
        self.bad('zz-copy', status='bogus')
        self.bad('mm-copy', status='bogus')
        self.assertEqual(len(self.inputs()), 4)
        self.assertEqual(self.report()['problems'], 5)

    def test_two_invalid_claimants_each_report_once(self):
        (self.root / 'content/meetings' / (MEETING + '.json')).unlink()
        self.bad('aa-copy', status='bogus')
        self.bad('zz-copy', status='bogus')
        self.assertEqual(len(self.inputs()), 2)
        self.assertEqual(self.row()['reasons'], NOT_CHECKED)

    def test_rejected_id_shapes_are_not_claims(self):
        for value in (None, '', [], {}, True, 'Bad_Slug'):
            self.bad('zz-copy', ident=value, status='bogus')
            self.assertEqual(self.row()['status'], 'ok', value)
            (self.root / 'content/meetings/zz-copy.json').unlink()

    def test_absent_id_uses_filename_stem(self):
        self.bad('cedar-police-2026-06-x', ident=None, status='bogus')
        self.assertEqual(self.row()['status'], 'ok')
        (self.root / 'content/meetings' / (MEETING + '.json')).unlink()
        (self.root / 'content/meetings/cedar-police-2026-06-x.json').rename(self.root / 'content/meetings' / (MEETING + '.json'))
        self.assertEqual(self.row()['reasons'], NOT_CHECKED)

    def test_unrelated_invalid_identity_does_not_poison_healthy_meeting(self):
        self.bad('other-meeting', ident='other-meeting', status='bogus')
        self.assertEqual(self.row()['status'], 'ok')
        self.assertEqual(self.report()['problems'], 1)

    def test_valid_duplicates_and_missing_controls_unchanged(self):
        folder = self.root / 'content/meetings'
        (folder / 'zz-copy.json').write_text((folder / (MEETING + '.json')).read_text(), encoding='utf-8')
        self.assertEqual(self.row()['reasons'], NOT_CHECKED)
        self.assertEqual(len(self.inputs()), 2)
        (folder / 'zz-copy.json').unlink()
        (folder / (MEETING + '.json')).unlink()
        self.assertEqual(self.row()['reasons'], [{'code': 'meeting_missing', 'meeting_id': MEETING}])

    def test_read_only(self):
        from tests.test_press_check import snapshot
        self.bad('zz-copy', status='bogus')
        before = snapshot(self.root)
        self.report()
        self.assertEqual(before, snapshot(self.root))


if __name__ == '__main__':
    unittest.main()
