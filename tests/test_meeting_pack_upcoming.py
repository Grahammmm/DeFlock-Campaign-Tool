"""Synthetic offline `meeting-pack --upcoming` acceptance and refusal paths."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from campaign_tool.meeting_pack import build_upcoming

REPO = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)
EXISTS = 'meeting pack exists; use --replace to overwrite'


def at(days, now=NOW):
    return (now + timedelta(days=days)).isoformat()


class MeetingPackUpcomingTests(unittest.TestCase):
    def setUp(self):  # same synthetic campaign as tests/test_meeting_pack.py MeetingPackTests.setUp
        temporary = tempfile.TemporaryDirectory(prefix='synthetic-meeting-pack-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / 'campaign'
        shutil.copytree(REPO / 'examples/fictional-campaign', self.root)
        self.content = self.root / 'content'
        for folder in ('findings', 'meetings'):
            shutil.rmtree(self.content / folder)
            (self.content / folder).mkdir()
        self.site = json.loads((self.content / 'site.json').read_text())
        self.site['base_url'] = 'https://synthetic.invalid'
        self.write('site.json', self.site)
        self.template = json.loads((REPO / 'examples/fictional-campaign/content/findings/cedar-policy-posted.json').read_text())
        self.meeting = {'body': 'Synthetic Council', 'starts_at': '2099-11-02T18:00:00-08:00',
                        'agenda_item': 'Item 7', 'location': 'Synthetic Hall', 'agency_id': 'ca-cedar-police',
                        'agenda_url': 'https://synthetic.invalid/agenda', 'comment_kit_md': 'Hello.\nThank you.\n'}
        self.library = []
        self.finding(1)
        self.finding(2)
        self.packs = self.root / 'kit/meeting-packs'

    def write(self, relative, value):
        (self.content / relative).write_text(json.dumps(value), encoding='utf-8')

    def stage(self, meeting_id, starts_at, **extra):
        self.write(f'meetings/{meeting_id}.json', {**self.meeting, 'id': meeting_id, 'starts_at': starts_at, **extra})

    def finding(self, number, **extra):
        digest = hashlib.sha256(f'synthetic {number}'.encode()).hexdigest()
        finding = {**self.template, 'id': f'f{number}', 'slug': f'finding-{number}', 'title': f'Finding {number}',
                   'event_date': f'2026-01-{number:02d}', 'corrections': [],
                   'sources': [{'sha256': digest, 'title': f'Source {number}', 'locator': f'page:{number}'}], **extra}
        self.write(f'findings/f{number}.json', finding)
        self.library = [entry for entry in self.library if entry['sha256'] != digest]
        self.library.append({'sha256': digest, 'title': f'Source {number}', 'agency_id': 'ca-cedar-police',
                             'received_at': f'2026-02-{number:02d}T12:00:00Z', 'public_url': None, 'fragment': f'source-{number}'})
        self.write('sources.json', self.library)
        return finding

    def up(self, *args, **kwargs):
        return build_upcoming(self.root, *args, today=NOW, **kwargs)

    def ids(self, report):
        return [item['id'] for item in report['built']]

    def cli(self, *extra):
        return subprocess.run([sys.executable, '-B', '-m', 'campaign_tool', 'meeting-pack', '--directory',
                               str(self.root), *extra], cwd=REPO, capture_output=True, text=True, timeout=30)

    def test_window_builds_only_upcoming_and_wider_window_builds_two(self):
        self.stage('soon', at(2))
        self.stage('later', at(20))
        self.stage('past', at(-3))
        report = self.up()
        self.assertEqual(report['window'], {'from': NOW.isoformat(), 'to': at(14), 'days': 14})
        self.assertEqual(report['skipped'], [])
        self.assertEqual(report['built'], [{'id': 'soon', 'starts_at': at(2), 'findings': 2, 'truncated': 0, 'written': [
            f'kit/meeting-packs/soon/{name}' for name in ('handout.html', 'comment.md', 'sources.csv')]}])
        self.assertEqual(sorted(p.name for p in self.packs.iterdir()), ['soon'])
        report = self.up(30, replace=True)
        self.assertEqual((self.ids(report), report['skipped']), (['soon', 'later'], []))
        self.assertFalse((self.packs / 'past').exists())
        for path in (self.root / 'kit', self.packs, self.packs / 'soon', self.packs / 'later'):
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)

    def test_built_in_start_order_not_name_order(self):
        self.stage('a-late', at(5))
        self.stage('z-early', '2026-11-02T00:00:00-08:00')
        self.assertEqual(self.ids(self.up()), ['z-early', 'a-late'])

    def test_cancelled_and_stale_kit_skipped_with_reasons_others_built(self):
        self.finding(1, corrections=[{'date': '2026-02-03', 'note': 'Synthetic correction'}])
        self.stage('ok', at(1))
        self.stage('gone', at(2), status='cancelled')
        self.stage('stale', at(3), comment_kit_md='/findings/finding-1.html')
        report = self.up()
        self.assertEqual(self.ids(report), ['ok'])
        self.assertEqual(report['skipped'], [
            {'id': 'gone', 'reason': 'cancelled'},
            {'id': 'stale', 'reason': 'stale kit; re-stage with `meetings-stage --id stale --replace`'}])
        self.assertEqual(sorted(p.name for p in self.packs.iterdir()), ['ok'])

    def test_existing_pack_skipped_then_rebuilt_with_replace(self):
        self.stage('m', at(2))
        self.assertEqual(self.ids(self.up()), ['m'])
        handout = self.packs / 'm/handout.html'
        old = handout.read_bytes()
        self.finding(2, title='Replacement title')
        report = self.up()
        self.assertEqual((report['built'], report['skipped']), ([], [{'id': 'm', 'reason': EXISTS}]))
        self.assertEqual(handout.read_bytes(), old)
        self.assertEqual(self.ids(self.up(replace=True)), ['m'])
        self.assertIn('Replacement title', handout.read_text())

    def test_window_edges_start_included_end_excluded(self):
        self.stage('start', NOW.isoformat())
        self.stage('start-offset', '2026-10-31T17:00:00-07:00')  # the same instant as NOW
        self.stage('end', at(14))
        self.stage('just-before', (NOW + timedelta(days=14, seconds=-1)).isoformat())
        self.stage('just-past', (NOW - timedelta(seconds=1)).isoformat())
        report = self.up()
        self.assertEqual(sorted(self.ids(report)), ['just-before', 'start', 'start-offset'])
        self.assertEqual(report['skipped'], [])

    def test_symlink_bad_stem_invalid_json_and_mismatched_id_skipped_others_built(self):
        self.stage('good', at(2))
        outside = self.root.parent / 'outside.json'
        outside.write_text(json.dumps({**self.meeting, 'starts_at': at(3)}))
        (self.content / 'meetings/linked.json').symlink_to(outside)
        self.stage('has space', at(3))
        (self.content / 'meetings/broken.json').write_text('{not json')
        (self.content / 'meetings/folder.json').mkdir()
        self.write('meetings/renamed.json', {**self.meeting, 'id': 'other', 'starts_at': at(4)})
        report = self.up()
        self.assertEqual(self.ids(report), ['good'])
        self.assertEqual(report['skipped'], [
            {'id': 'broken', 'reason': 'unreadable meeting file'},
            {'id': 'folder', 'reason': 'unreadable meeting file'},
            {'id': 'has space', 'reason': 'invalid meeting id'},
            {'id': 'linked', 'reason': 'symlink on meeting-pack path; use real directories and files'},
            {'id': 'renamed', 'reason': 'meeting id differs from filename; re-stage the requested meeting'}])
        self.assertEqual(sorted(p.name for p in self.packs.iterdir()), ['good'])

    def test_symlinked_meetings_folder_refused(self):
        real = self.root.parent / 'real-meetings'
        (self.content / 'meetings').rename(real)
        (self.content / 'meetings').symlink_to(real, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.up()
        self.assertFalse(self.packs.exists())

    def test_no_meetings_or_no_folder_is_empty_success(self):
        empty = {'built': [], 'skipped': []}
        report = self.up()
        self.assertEqual({k: report[k] for k in empty}, empty)
        shutil.rmtree(self.content / 'meetings')
        report = self.up()
        self.assertEqual({k: report[k] for k in empty}, empty)
        result = self.cli('--upcoming')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['built'], [])
        self.assertFalse(self.packs.exists())

    def test_bad_arguments_refused(self):
        for days in (0, 91, True, '14', 14.0):
            with self.subTest(days=days), self.assertRaisesRegex(ValueError, '--within-days must be 1..90'):
                self.up(days)
        with self.assertRaisesRegex(ValueError, 'time zone'):
            build_upcoming(self.root, today=datetime(2026, 11, 1))

    def test_other_errors_propagate_and_earlier_packs_stay(self):
        self.stage('first', at(1))
        self.stage('second', at(2))
        real = os.replace

        def failing(source, target):
            if '/second/' in str(target):
                raise OSError('synthetic disk failure')
            return real(source, target)
        with patch('campaign_tool.meeting_pack.os.replace', side_effect=failing):
            with self.assertRaisesRegex(OSError, 'synthetic disk failure'):
                self.up()
        self.assertTrue((self.packs / 'first/handout.html').is_file())
        self.assertFalse((self.packs / 'second/handout.html').exists())

    def test_cli_exit_codes(self):
        now = datetime.now(timezone.utc)
        self.stage('soon', at(2, now))
        result = self.cli('--upcoming')
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual((self.ids(report), report['skipped'], report['window']['days']), (['soon'], [], 14))
        self.stage('gone', at(3, now), status='cancelled')
        result = self.cli('--upcoming', '--within-days', '30', '--replace')
        self.assertEqual(result.returncode, 3, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual((self.ids(report), report['skipped']), (['soon'], [{'id': 'gone', 'reason': 'cancelled'}]))
        self.assertEqual(report['window']['days'], 30)

    def test_cli_usage_errors_before_anything_is_read(self):
        self.stage('soon', at(2, datetime.now(timezone.utc)))
        for extra in (('--id', 'soon', '--upcoming'), ('--upcoming', '--within-days', '0'),
                      ('--upcoming', '--within-days', '91'), ('--id', 'soon', '--within-days', '5'),
                      ('--upcoming', '--allow-past'), ()):
            with self.subTest(extra=extra):
                result = self.cli(*extra)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn('usage:', result.stderr)
                self.assertEqual(result.stdout, '')
        self.assertFalse(self.packs.exists())


    # DF0137-17: build_upcoming follows site.json.language and content/labels.json for every pack it builds.
    def spanish(self):
        self.site['language'] = 'es'
        self.write('site.json', self.site)
        self.write('labels.json', {'schema_version': 1, 'labels': {
            'handout.title': 'Hoja informativa', 'handout.what_we_found': 'Lo que encontramos',
            'handout.source_line': 'Fuente: {title}, recibida {date}, registro {record}'}})

    def test_labels_and_language_apply_to_every_pack_in_the_window(self):
        self.stage('soon', at(2))
        self.stage('later', at(5))
        self.spanish()
        report = self.up()
        self.assertEqual((self.ids(report), report['skipped']), (['soon', 'later'], []))
        for meeting_id in ('soon', 'later'):
            page = (self.packs / meeting_id / 'handout.html').read_text()
            self.assertIn('<html lang="es"><meta charset="utf-8"><title>Hoja informativa</title>', page)
            self.assertIn('<h2>Lo que encontramos</h2>', page)
            self.assertIn('Fuente: Source 1, recibida 2026-02-01T12:00:00Z, registro ', page)
            self.assertNotIn('Source:', page)
            self.assertEqual((self.packs / meeting_id / 'comment.md').read_text(), 'Hello.\nThank you.\n')

    def test_bad_labels_skip_every_meeting_naming_the_key_and_write_nothing(self):
        self.stage('soon', at(2))
        self.stage('later', at(5))
        self.write('labels.json', {'schema_version': 1, 'labels': {'handout.corrected': 'corregido {title}'}})
        report = self.up()
        self.assertEqual(report['built'], [])
        self.assertEqual([item['id'] for item in report['skipped']], ['soon', 'later'])
        for item in report['skipped']:
            self.assertIn('handout.corrected', item['reason'])
            self.assertIn('{title}', item['reason'])
        self.assertFalse(self.packs.exists())


if __name__ == '__main__':
    unittest.main()
