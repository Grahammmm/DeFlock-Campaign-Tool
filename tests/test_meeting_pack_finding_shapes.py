"""Real-disk finding-root admission, private pack preservation and CLI contracts."""
import json
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from campaign_tool import meeting_pack
from tests import test_meeting_pack as fixtures


ROOTS = (None, [], [1], '', 'synthetic-payload-canary', 7, 1.25, True, False)
REASON = 'findings/bad.json: expected an object'


class FindingShapeTests(unittest.TestCase):
    def fixture(self):
        # Composition reuses synthetic setup without collecting its historical tests.
        fixture = fixtures.MeetingPackTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        return fixture

    def state(self, root):
        """Entire synthetic campaign: names, modes, mtimes and exact file bytes."""
        return {str(path.relative_to(root)): (
            path.stat().st_mode, path.stat().st_mtime_ns,
            path.read_bytes() if path.is_file() else None)
            for path in (root, *sorted(root.rglob('*')))}

    def members(self, folder):
        return {p.name: p.read_bytes() for p in folder.iterdir()}

    def refuse(self, fixture, name='bad.json', **kwargs):
        before = self.state(fixture.root)
        with patch.object(meeting_pack, '_commit_pack') as commit:
            with self.assertRaises(ValueError) as caught:
                fixture.pack(**kwargs)
            self.assertEqual(str(caught.exception), f'findings/{name}: expected an object')
            commit.assert_not_called()
        self.assertEqual(self.state(fixture.root), before)

    def test_non_object_roots_refuse_before_state_filter_and_truncation(self):
        for value in ROOTS:
            for name in ('a-bad.json', 'z-bad.json'):
                with self.subTest(value=value, name=name):
                    fixture = self.fixture()
                    fixture.write('findings/' + name, value)
                    self.refuse(fixture, name, max_findings=1)

    def test_first_and_replacement_refusals_preserve_all_campaign_state(self):
        for existing in (False, True):
            for value in ROOTS:
                with self.subTest(existing=existing, value=value):
                    fixture = self.fixture()
                    if existing:
                        fixture.pack()
                        sentinel = fixture.folder.parent / 'unrelated'
                        sentinel.mkdir(mode=0o700)
                        (sentinel / 'sentinel.txt').write_text('unrelated synthetic pack')
                        fixture.folder.parent.chmod(0o750)
                    else:
                        self.assertFalse((fixture.root / 'kit').exists())
                    fixture.write('findings/bad.json', value)
                    self.refuse(fixture, replace=existing)
                    if not existing:
                        self.assertFalse((fixture.root / 'kit').exists())

    def test_hidden_json_and_unmatched_agency_refuse_but_non_json_is_ignored(self):
        for name in ('.bad.json', 'bad.json'):
            with self.subTest(name=name):
                fixture = self.fixture()
                fixture.meeting['agency_id'] = 'ca-other-police'
                fixture.stage()
                fixture.write('findings/' + name, None)
                self.refuse(fixture, name)
                (fixture.content / 'findings' / name).unlink()
                (fixture.content / 'findings/editor.tmp').write_text('not even JSON')
                report = fixture.pack()
                self.assertEqual(report['findings'], 0)

    def test_nonpublished_objects_keep_skip_semantics_and_identical_output(self):
        fixture = self.fixture()
        expected_report = fixture.pack()
        expected_bytes = self.members(fixture.folder)
        objects = ({}, {'unknown': 'synthetic'}, {'state': None},
                   {'state': 'draft', 'unknown': True}, {'state': 'withdrawn'},
                   {'state': 'unknown'})
        for index, value in enumerate(objects):
            fixture.write(f'findings/skip-{index}.json', value)
        self.assertEqual(fixture.pack(replace=True), expected_report)
        self.assertEqual(self.members(fixture.folder), expected_bytes)
        self.assertLess(fixture.html().index('Finding 2'), fixture.html().index('Finding 1'))
        self.assertEqual((fixture.folder / 'comment.md').read_bytes(),
                         fixture.meeting['comment_kit_md'].encode())

    def test_published_objects_still_require_loader_validation(self):
        fixture = self.fixture()
        fixture.write('findings/bad.json', {'state': 'published'})
        before = self.state(fixture.root)
        with patch.object(meeting_pack, '_commit_pack') as commit:
            with self.assertRaises(ValueError) as caught:
                fixture.pack()
            self.assertNotIn('expected an object', str(caught.exception))
            self.assertIn('bad.json', str(caught.exception))
            commit.assert_not_called()
        self.assertEqual(self.state(fixture.root), before)

    def test_single_cli_controlled_refusal_and_repair_retry(self):
        for existing in (False, True):
            for value in (None, [], 'synthetic-payload-canary'):
                with self.subTest(existing=existing, value=value):
                    fixture = self.fixture()
                    if existing:
                        fixture.pack()
                    fixture.write('findings/bad.json', value)
                    before = self.state(fixture.root)
                    args = ('--replace',) if existing else ()
                    result = fixture.cli(*args)
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertEqual(result.stdout, '')
                    self.assertIn('Stopped:', result.stderr)
                    self.assertIn(REASON, result.stderr)
                    for forbidden in ('Traceback', 'AttributeError', 'synthetic-payload-canary'):
                        self.assertNotIn(forbidden, result.stderr)
                    self.assertEqual(self.state(fixture.root), before)
                    fixture.write('findings/bad.json', {})
                    retry = fixture.cli(*args)
                    self.assertEqual(retry.returncode, 0, retry.stderr)
                    self.assertEqual(json.loads(retry.stdout)['findings'], 2)
                    self.assertEqual(set(self.members(fixture.folder)),
                                     {'handout.html', 'sources.csv', 'comment.md'})

    def stage_batch(self, fixture, now):
        (fixture.content / 'meetings/m.json').unlink()
        for name, days, extra in (('z-early', 1, {}), ('a-late', 3, {}),
                                  ('cancelled', 2, {'status': 'cancelled'}),
                                  ('outside', 20, {})):
            fixture.write(f'meetings/{name}.json', {
                **fixture.meeting, 'id': name,
                'starts_at': (now + timedelta(days=days)).isoformat(), **extra})

    def expected_skips(self):
        return [{'id': 'z-early', 'reason': REASON},
                {'id': 'cancelled', 'reason': 'cancelled'},
                {'id': 'a-late', 'reason': REASON}]

    def assert_batch_success(self, fixture, report):
        self.assertEqual(set(report), {'window', 'built', 'skipped'})
        self.assertEqual([row['id'] for row in report['built']], ['z-early', 'a-late'])
        self.assertEqual(report['skipped'], [{'id': 'cancelled', 'reason': 'cancelled'}])
        for row in report['built']:
            name = row['id']
            self.assertEqual(set(row), {'id', 'starts_at', 'written', 'findings',
                                        'truncated', 'comment_sent'})
            self.assertEqual((row['findings'], row['truncated'], row['comment_sent']), (2, 0, None))
            self.assertEqual(row['written'], [f'kit/meeting-packs/{name}/{member}'
                                             for member in ('handout.html', 'comment.md', 'sources.csv')])
            folder = fixture.folder.parent / name
            self.assertEqual((folder / 'comment.md').read_bytes(), fixture.meeting['comment_kit_md'].encode())
            for path in (fixture.root / 'kit', folder.parent, folder):
                self.assertEqual(path.stat().st_mode & 0o777, 0o700)
            for path in folder.iterdir():
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_upcoming_skips_every_affected_meeting_and_preserves_old_packs(self):
        now = datetime(2099, 1, 1, tzinfo=timezone.utc)
        for existing in (False, True):
            for value in (None, [], 'synthetic-payload-canary'):
                with self.subTest(existing=existing, value=value):
                    fixture = self.fixture()
                    self.stage_batch(fixture, now)
                    if existing:
                        self.assert_batch_success(fixture, meeting_pack.build_upcoming(fixture.root, today=now))
                    fixture.write('findings/bad.json', value)
                    before = self.state(fixture.root)
                    with patch.object(meeting_pack, '_commit_pack') as commit:
                        report = meeting_pack.build_upcoming(fixture.root, today=now, replace=existing)
                        commit.assert_not_called()
                    self.assertEqual(report, {
                        'window': {'from': now.isoformat(), 'to': (now + timedelta(days=14)).isoformat(), 'days': 14},
                        'built': [], 'skipped': self.expected_skips()})
                    self.assertEqual(self.state(fixture.root), before)
                    fixture.write('findings/bad.json', {})
                    self.assert_batch_success(fixture, meeting_pack.build_upcoming(
                        fixture.root, today=now, replace=existing))

    def test_upcoming_cli_exit_three_and_repair(self):
        fixture = self.fixture()
        self.stage_batch(fixture, datetime.now(timezone.utc))
        for existing in (False, True):
            with self.subTest(existing=existing):
                fixture.write('findings/bad.json', 'synthetic-payload-canary')
                before = self.state(fixture.root)
                command = [
                    sys.executable, '-B', '-m', 'campaign_tool', 'meeting-pack',
                    '--directory', str(fixture.root), '--upcoming', '--replace']
                result = subprocess.run(command,
                    cwd=fixtures.REPO, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 3, result.stderr)
                self.assertEqual(result.stderr, '')
                report = json.loads(result.stdout)
                self.assertEqual(set(report), {'window', 'built', 'skipped'})
                self.assertEqual(report['built'], [])
                self.assertEqual(report['skipped'], self.expected_skips())
                self.assertNotIn('synthetic-payload-canary', result.stdout)
                self.assertEqual(self.state(fixture.root), before)
                fixture.write('findings/bad.json', {})
                retry = subprocess.run(command, cwd=fixtures.REPO,
                                       capture_output=True, text=True, timeout=30)
                self.assertEqual(retry.returncode, 3, retry.stderr)  # cancelled meeting still skips
                self.assertEqual(retry.stderr, '')
                self.assert_batch_success(fixture, json.loads(retry.stdout))

    def test_empty_window_does_not_scan_malformed_findings(self):
        fixture = self.fixture()
        fixture.write('findings/bad.json', None)
        before = self.state(fixture.root)
        with patch.object(meeting_pack, 'build') as build:
            report = meeting_pack.build_upcoming(fixture.root, today=fixtures.NOW)
            build.assert_not_called()
        self.assertEqual((report['built'], report['skipped']), ([], []))
        self.assertEqual(self.state(fixture.root), before)


if __name__ == '__main__':
    unittest.main()
