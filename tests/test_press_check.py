"""press check: read-only freshness check of private press drafts and letters, fictional content only (DF1622-06)."""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from campaign_tool import findings as findings_mod
from campaign_tool import press, press_check
from campaign_tool.press import build_letter, build_press

REPO = Path(__file__).resolve().parents[1]
FIRST, SECOND, THIRD = (f'fnd_000000000000000{n}' for n in '123')
SLUG = 'cedar-policy-posted'
DAY = '2026-03-01'
NOW = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
POLICE = 'ca-cedar-police'
MEETING = 'cedar-police-2026-06'


def snapshot(path):
    """{relative path: (kind, bytes or target, mode, mtime_ns)} of the whole tree."""
    out = {}
    for item in sorted(Path(path).rglob('*')):
        info = item.lstat()
        key = item.relative_to(path).as_posix()
        if item.is_symlink():
            out[key] = ('link', os.readlink(item), info.st_mode, info.st_mtime_ns)
        elif item.is_file():
            out[key] = ('file', item.read_bytes(), info.st_mode, info.st_mtime_ns)
        else:
            out[key] = ('other', None, info.st_mode, info.st_mtime_ns)
    return out


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='fictional-press-check-')
        self.addCleanup(tmp.cleanup)
        self.top = Path(tmp.name)
        self.root = self.top / 'campaign'
        shutil.copytree(REPO / 'examples/fictional-campaign', self.root, ignore=shutil.ignore_patterns('public'))
        self.findings = self.root / 'content/findings'
        self.first = json.loads((self.findings / (SLUG + '.json')).read_text())
        self.add(SECOND, 'cedar-second', [])
        self.add(THIRD, 'cedar-third', [])
        self.meeting(MEETING, '2026-06-10T18:00:00-07:00')
        patcher = mock.patch('socket.socket.connect', side_effect=AssertionError('network is blocked'))
        patcher.start()
        self.addCleanup(patcher.stop)

    def add(self, ident, slug, corrections, **extra):
        finding = dict(self.first, id=ident, slug=slug, title=slug.title(), summary=f'Summary of {slug}.',
                       limitations=[], corrections=corrections, **extra)
        (self.findings / (slug + '.json')).write_text(json.dumps(finding), encoding='utf-8')

    def meeting(self, ident, starts_at, **extra):
        folder = self.root / 'content/meetings'
        document = {'id': ident, 'body': 'Cedar Council (fictional)', 'starts_at': starts_at, 'agency_id': POLICE, **extra}
        (folder / (ident + '.json')).write_text(json.dumps(document), encoding='utf-8')

    def draft(self, ids=(FIRST,), day=DAY):
        return build_press(self.root, list(ids), day)['path']

    def letter(self, ids=(FIRST,), day=DAY):
        return build_letter(self.root, list(ids), day, now=NOW)['path']

    def put(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return path

    def check(self, now=NOW):
        return press_check.check_press(self.root, now=now)

    def rows(self, now=NOW):
        return {row['path']: row for row in self.check(now)['drafts']}

    def cli(self, *args, directory=None):
        cmd = [sys.executable, '-B', '-m', 'campaign_tool', 'press', 'check', '--directory', str(directory or self.root), *args]
        return subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, timeout=120)


class CheckTests(Base):
    def test_cli_ok_text_json_and_empty_missing_folders(self):
        before = snapshot(self.root)
        done = self.cli()
        self.assertEqual((done.returncode, done.stdout), (0, 'No private press drafts found.\n'))
        self.assertEqual(snapshot(self.root), before)
        self.assertFalse((self.root / 'kit').exists())
        done = self.cli('--json')
        self.assertEqual((done.returncode, json.loads(done.stdout)), (0, {'drafts': [], 'problems': 0}))
        draft, letter = self.draft(), self.letter()
        before = snapshot(self.root)
        # The example finding was corrected on 2026-02-03, before the 2026-03-01 draft; the meeting is after NOW.
        report = self.check()
        self.assertEqual(report, {'drafts': [
            {'path': draft, 'kind': 'draft', 'status': 'ok', 'findings': [FIRST], 'reasons': []},
            {'path': letter, 'kind': 'letter', 'status': 'ok', 'findings': [FIRST], 'reasons': []}], 'problems': 0})
        self.assertEqual(press_check.format_text(report), f'{draft}: ok\n{letter}: ok')
        self.assertEqual(snapshot(self.root), before)

    def test_all_four_folders_and_independent_suggested_headers(self):
        self.draft(), self.letter()
        for rel, ids in (('kit/press/%s-edited.md', (SECOND, THIRD)), ('kit/press.suggested/%s-s.md', (SECOND,)),
                         ('kit/press/letters/%s-l.md', (FIRST,)), ('kit/press/letters.suggested/%s-t.md', (THIRD,))):
            self.put(rel % DAY, f"<!-- DRAFT: not sent. Review before any use. findings {', '.join(ids)} -->\n\nbody\n"
                     + ('I encourage readers to visit the campaign signup page: /#participate.\n' if 'letters' in rel else ''))
        self.put('kit/press/handwritten.md', 'My own notes, no header.\n')
        (self.root / 'kit/press/.written.json').write_text('{}')
        (self.root / 'kit/press/notes.txt').write_text('ignored')
        self.findings.joinpath('cedar-third.json').unlink()
        report = self.check()
        paths = [row['path'] for row in report['drafts']]
        self.assertEqual(paths, [f'kit/press/{DAY}-cedar-policy-posted.md', f'kit/press/{DAY}-edited.md', 'kit/press/handwritten.md',
                                 f'kit/press.suggested/{DAY}-s.md', f'kit/press/letters/{DAY}-cedar-policy-posted.md',
                                 f'kit/press/letters/{DAY}-l.md', f'kit/press/letters.suggested/{DAY}-t.md'])
        by = {row['path']: row for row in report['drafts']}
        self.assertEqual(by[f'kit/press/{DAY}-edited.md']['findings'], [SECOND, THIRD])
        self.assertEqual([r['code'] for r in by[f'kit/press/{DAY}-edited.md']['reasons']], ['unpublished'])
        self.assertEqual(by[f'kit/press/{DAY}-edited.md']['reasons'][0]['finding_id'], THIRD)
        self.assertEqual(by[f'kit/press.suggested/{DAY}-s.md']['status'], 'ok')
        self.assertEqual(by['kit/press/handwritten.md']['status'], 'not_checked')
        self.assertEqual(by[f'kit/press/letters.suggested/{DAY}-t.md']['status'], 'stale')
        self.assertEqual(report['problems'], 3)

    def test_withdrawn_unpublished_missing_and_leftover_public(self):
        self.root.joinpath('private').mkdir(exist_ok=True)
        paths = {name: self.draft([ident], day) for name, ident, day in
                 (('w', FIRST, '2026-03-02'), ('d', SECOND, '2026-03-03'))}
        findings_mod.withdraw(self.root, FIRST, 'synthetic reason SECRET-CANARY')
        document = json.loads(self.findings.joinpath('cedar-second.json').read_text())
        self.findings.joinpath('cedar-second.json').write_text(json.dumps(dict(document, state='draft')))
        ghost = self.put('kit/press/2026-03-04-ghost.md', '<!-- DRAFT: not sent. Review before any use. findings fnd_gone -->\n')
        # leftover public copy of a withdrawn finding: withdrawal wins
        self.findings.joinpath(SLUG + '.json').write_text(json.dumps(self.first))
        rows = self.rows()
        self.assertEqual([r['code'] for r in rows[paths['w']]['reasons']], ['withdrawn'])
        self.assertEqual([r['code'] for r in rows[paths['d']]['reasons']], ['unpublished'])
        self.assertEqual(rows[ghost.relative_to(self.root).as_posix()]['reasons'], [{'code': 'unpublished', 'finding_id': 'fnd_gone'}])
        done = self.cli()
        self.assertEqual(done.returncode, 3)
        self.assertNotIn('SECRET-CANARY', done.stdout + done.stderr)
        self.assertIn(f'{paths["w"]}: stale (findings {FIRST}): withdrawn {FIRST}', done.stdout)

    def test_every_header_id_and_all_correction_dates(self):
        self.add(SECOND, 'cedar-second', [{'date': '2026-04-02', 'note': 'NOTE-CANARY-B'}, {'date': '2026-03-01', 'note': 'same day'},
                                          {'date': '2026-04-01', 'note': 'NOTE-CANARY-A'}, {'date': '2026-04-02', 'note': 'dup'},
                                          {'date': '2026-02-20', 'note': 'older'}])
        self.add(FIRST, SLUG, [{'date': '2026-02-03', 'note': 'before'}])
        name = self.draft([FIRST, SECOND, THIRD])
        row = self.rows()[name]
        self.assertEqual((row['status'], row['findings']), ('stale', [FIRST, SECOND, THIRD]))
        self.assertEqual(row['reasons'], [{'code': 'corrected', 'finding_id': SECOND, 'dates': ['2026-04-01', '2026-04-02']}])
        self.assertNotIn('CANARY', json.dumps(self.check()) + press_check.format_text(self.check()))
        self.assertIn('corrected fnd_0000000000000002 after-filename-date 2026-04-01,2026-04-02', press_check.format_text(self.check()))
        # a letter that dropped the corrected finding does not cite it
        self.add(THIRD, 'cedar-third', [{'date': '2026-04-05', 'note': 'x'}])
        self.add(SECOND, 'cedar-second', [{'date': '2026-04-05', 'note': 'x'}])
        letter = self.put('kit/press/letters/2026-03-01-dropped.md',
                          f'<!-- DRAFT: not sent. Review before any use. findings {FIRST} -->\n\nI encourage readers to visit the campaign signup page: /#participate.\n')
        self.assertEqual(self.rows()[letter.relative_to(self.root).as_posix()]['status'], 'ok')
        # multiple findings withdrawn and corrected accumulate
        self.findings.joinpath('cedar-third.json').unlink()
        self.assertEqual([r['code'] for r in self.rows()[name]['reasons']], ['corrected', 'unpublished'])

    def test_missing_malformed_later_header_and_filename_date(self):
        good = '<!-- DRAFT: not sent. Review before any use. findings %s -->\n\nbody\n'
        cases = {
            'kit/press/2026-03-01-empty.md': '<!-- DRAFT: not sent. Review before any use. findings  -->\n',
            'kit/press/2026-03-01-garbage.md': good % 'a,,b',
            'kit/press/2026-03-01-badid.md': good % 'not valid!',
            'kit/press/2026-03-01-later.md': 'intro\n' + good % FIRST,
            'kit/press/2026-03-01-none.md': 'plain text\n',
            'kit/press/nodate.md': good % FIRST,
            'kit/press/2026-02-30-impossible.md': good % FIRST,
        }
        for rel, text in cases.items():
            self.put(rel, text)
        (self.root / 'kit/press/2026-03-01-bad.md').write_bytes(b'\xff\xfe bad')
        (self.root / 'kit/press/2026-03-01-big.md').write_bytes(b'x' * (press_check.MAX_MARKDOWN_BYTES + 1))
        rows = self.rows()
        for rel in cases:
            row = rows[rel]
            self.assertEqual(row['status'], 'not_checked', rel)
        self.assertEqual(rows['kit/press/2026-03-01-empty.md']['reasons'][0]['message'], 'missing or invalid draft header')
        self.assertEqual(rows['kit/press/nodate.md']['reasons'], [{'code': 'not_checked', 'message': 'missing or invalid filename date'}])
        self.assertEqual(rows['kit/press/2026-02-30-impossible.md']['findings'], [FIRST])
        self.assertEqual(rows['kit/press/2026-03-01-bad.md']['status'], 'unreadable')
        self.assertEqual(rows['kit/press/2026-03-01-big.md']['status'], 'unreadable')
        self.assertNotIn('plain text', json.dumps(rows))
        text = press_check.format_text(self.check())
        self.assertIn('not checked: missing or invalid draft header', text)
        self.assertEqual(self.cli().returncode, 3)

    def test_letter_cancelled_past_boundary_offsets_and_signup(self):
        letter = self.letter()
        self.assertIn(f'/meetings/{MEETING}.html', (self.root / letter).read_text())
        self.assertEqual(self.rows()[letter]['status'], 'ok')
        # exactly now is not past; one second later is; offsets compare as instants
        self.meeting(MEETING, '2026-05-01T05:00:00-07:00')
        self.assertEqual(self.rows(NOW)[letter]['status'], 'ok')
        self.assertEqual(self.rows(NOW.replace(second=1))[letter]['reasons'], [{'code': 'meeting_past', 'meeting_id': MEETING}])
        self.meeting(MEETING, '2026-05-01T12:00:00Z')
        self.assertEqual(self.rows(NOW)[letter]['status'], 'ok')
        self.meeting(MEETING, '2026-04-30T12:00:00Z', status='cancelled')
        self.assertEqual([r['code'] for r in self.rows()[letter]['reasons']], ['meeting_cancelled', 'meeting_past'])
        self.meeting(MEETING, '2026-06-10T18:00:00-07:00', status='cancelled')
        self.assertEqual([r['code'] for r in self.rows()[letter]['reasons']], ['meeting_cancelled'])
        self.meeting(MEETING, '2026-06-10T18:00:00-07:00')
        # relative and HTTPS links; an unrelated body link is ignored; the edited closing is not checked
        text = (self.root / letter).read_text()
        edited = self.root / 'kit/press/letters/2026-03-02-edited.md'
        edited.write_text(text.replace('https://cedar-records.example.invalid', '').replace(
            'I am writing', 'See also /meetings/other-meeting.html. I am writing'))
        self.assertEqual(self.rows()['kit/press/letters/2026-03-02-edited.md']['status'], 'ok')
        self.meeting(MEETING, '2026-04-30T12:00:00Z')
        self.assertEqual(self.rows()['kit/press/letters/2026-03-02-edited.md']['reasons'],
                         [{'code': 'meeting_past', 'meeting_id': MEETING}])
        edited.write_text(text.replace('I encourage readers to review', 'Please look at'))
        self.assertEqual(self.rows()['kit/press/letters/2026-03-02-edited.md']['reasons'],
                         [{'code': 'not_checked', 'message': 'unrecognized letter closing'}])
        # signup fallback needs no meeting lookup, even with a broken meetings folder
        shutil.rmtree(self.root / 'content/meetings')
        shutil.rmtree(self.root / 'kit/press/letters')
        signup = self.letter_signup()
        with mock.patch.object(press_check._Index, 'meetings', side_effect=AssertionError('no lookup')):
            self.assertEqual(self.rows()[signup]['status'], 'ok')
        # a referenced meeting that no longer exists
        edited.write_text(text)
        self.assertEqual(self.rows()['kit/press/letters/2026-03-02-edited.md']['reasons'],
                         [{'code': 'meeting_missing', 'meeting_id': MEETING}])

    def letter_signup(self):
        return build_letter(self.root, [SECOND], '2026-03-05', now=NOW)['path']

    def test_unsafe_ancestors_entries_and_content_indexes(self):
        draft, letter = self.draft(), self.letter()
        outside = self.top / 'outside'
        outside.mkdir()
        (outside / 'x.md').write_text('<!-- DRAFT: not sent. Review before any use. findings fnd_x -->\n')
        press_dir = self.root / 'kit/press'
        (press_dir / 'link.md').symlink_to(outside / 'x.md')
        (press_dir / 'dangling.md').symlink_to(outside / 'missing.md')
        (press_dir / 'dir.md').mkdir()
        os.mkfifo(press_dir / 'fifo.md')
        rows = self.rows()
        for name in ('link', 'dangling', 'dir', 'fifo'):
            self.assertEqual((rows[f'kit/press/{name}.md']['status'], rows[f'kit/press/{name}.md']['kind']), ('not_regular', 'draft'))
        self.assertEqual(rows[draft]['status'], 'ok')
        # a regular name swapped for a FIFO between listing and open must neither block nor be read
        real = os.stat

        def lying(path, *args, **kwargs):
            if path == 'fifo.md':
                return real(self.root / draft, *args, **kwargs)
            return real(path, *args, **kwargs)
        with mock.patch('campaign_tool.press_check.os.stat', side_effect=lying):
            self.assertEqual(self.rows()['kit/press/fifo.md']['status'], 'not_regular')
        # unreadable open
        if os.geteuid() != 0:
            (press_dir / 'locked.md').write_text('x')
            (press_dir / 'locked.md').chmod(0)
            self.assertEqual(self.rows()['kit/press/locked.md']['status'], 'unreadable')
            (press_dir / 'locked.md').chmod(0o600)
        # folder and ancestor links: one row per link, nothing below it enumerated
        for rel in ('kit/press/letters', 'kit/press.suggested', 'kit/press/letters.suggested'):
            target = self.root / rel
            if target.exists():
                shutil.rmtree(target)
        (self.root / 'kit/press/letters').symlink_to(outside)
        (self.root / 'kit/press.suggested').symlink_to(outside)
        (self.root / 'kit/press/letters.suggested').write_text('a file')
        report = self.check()
        got = {r['path']: r['status'] for r in report['drafts']}
        for rel in ('kit/press/letters', 'kit/press.suggested', 'kit/press/letters.suggested'):
            self.assertEqual(got[rel], 'not_regular')
        self.assertNotIn(letter, got)
        shutil.rmtree(press_dir)
        press_dir.symlink_to(outside)
        (self.root / 'kit/press.suggested').unlink()
        rows = self.rows()
        self.assertEqual([p for p in rows if p.startswith('kit/')], ['kit/press'])
        # content indexes
        press_dir.unlink()
        self.draft()
        shutil.copy(self.findings / (SLUG + '.json'), self.findings / 'dup.json')
        (self.findings / 'broken.json').write_text('{not json')
        (self.findings / 'linked.json').symlink_to(outside / 'x.md')
        withdrawn = self.root / 'private/withdrawn'
        withdrawn.mkdir(parents=True)
        (withdrawn / 'odd.json').write_text('[]')
        report = self.check()
        by = {r['path']: r for r in report['drafts']}
        self.assertEqual(by[f'kit/press/{DAY}-{SLUG}.md']['reasons'], [{'code': 'not_checked', 'message': 'finding index unavailable'}])
        for rel in ('content/findings/dup.json', f'content/findings/{SLUG}.json', 'content/findings/broken.json',
                    'content/findings/linked.json', 'private/withdrawn/odd.json'):
            self.assertEqual(by[rel]['kind'], 'input', rel)
            self.assertNotEqual(by[rel]['status'], 'ok', rel)
        self.assertEqual(by['content/findings/linked.json']['status'], 'not_regular')
        self.assertEqual([r['path'] for r in report['drafts'][1:]], sorted(r['path'] for r in report['drafts'][1:]))
        # withdrawn folder and meetings folder links
        for rel in ('content/findings/dup.json', 'content/findings/broken.json', 'content/findings/linked.json'):
            (self.root / rel).unlink()
        shutil.rmtree(withdrawn)
        withdrawn.symlink_to(outside)
        self.assertEqual(self.rows()[f'kit/press/{DAY}-{SLUG}.md']['status'], 'not_checked')
        withdrawn.unlink()
        self.letter()
        (self.root / 'content/meetings').rename(self.root / 'content/meetings.real')
        (self.root / 'content/meetings').symlink_to(outside)
        rows = self.rows()
        self.assertEqual(rows['content/meetings']['status'], 'not_regular')
        self.assertEqual(rows[f'kit/press/letters/{DAY}-{SLUG}.md']['reasons'],
                         [{'code': 'not_checked', 'message': 'meeting file invalid or duplicated'}])

    def test_read_only_offline_and_generators_unchanged(self):
        self.draft(), self.letter()
        (self.root / 'kit/press/2026-03-01-stale.md').write_text('<!-- DRAFT: not sent. Review before any use. findings fnd_gone -->\n')
        before = snapshot(self.root)
        guards = [mock.patch.object(target, name, side_effect=AssertionError(f'{name} called')) for target, name in
                  ((press, 'build_press'), (press, 'build_letter'), (press, '_write_text'), (press, '_write_json'),
                   (press, '_kit_folder'), (os, 'chmod'), (os, 'unlink'), (os, 'rename'), (os, 'mkdir'),
                   (socket, 'create_connection'))]
        for guard in guards:
            guard.start()
            self.addCleanup(guard.stop)
        report = self.check()
        self.assertEqual(report['problems'], 1)
        for guard in guards:
            guard.stop()
        self.assertEqual(snapshot(self.root), before)
        self.assertEqual(self.cli().returncode, 3)
        self.assertEqual(snapshot(self.root), before)
        # generators keep their result shapes and bytes
        result = build_press(self.root, [FIRST], '2026-03-09')
        self.assertEqual(sorted(result), ['findings', 'kept_edited', 'path', 'sent'])
        self.assertTrue((self.root / result['path']).read_text().startswith(
            f'<!-- DRAFT: not sent. Review before any use. findings {FIRST} -->\n'))

    def test_cli_usage_and_exit_codes(self):
        self.assertEqual(subprocess.run([sys.executable, '-B', '-m', 'campaign_tool', 'press', 'check'], cwd=REPO,
                                        capture_output=True, text=True).returncode, 2)
        self.assertEqual(self.cli('--bogus').returncode, 2)
        missing = self.cli(directory=self.top / 'nope')
        self.assertEqual(missing.returncode, 1)
        self.assertFalse((self.top / 'nope').exists())
        self.assertEqual(self.cli(directory=self.top).returncode, 1)
        self.draft()
        self.put('kit/press/2026-03-02-odd.md', 'no header\n')
        json_run = self.cli('--json')
        self.assertEqual(json_run.returncode, 3)
        data = json.loads(json_run.stdout)
        self.assertEqual((data['problems'], [r['status'] for r in data['drafts']]), (1, ['ok', 'not_checked']))
        self.assertEqual(self.cli('--json').stdout, json_run.stdout)
        text_run = self.cli()
        self.assertNotIn('Nothing was sent', text_run.stdout)
        self.assertIn('kit/press/2026-03-02-odd.md: not checked', text_run.stdout)
        self.put('kit/press/2026-03-03-new\nline.md', 'x')
        self.assertIn('2026-03-03-new\\x0aline.md: not checked', self.cli().stdout)
        with self.assertRaises(ValueError):
            press_check.check_press(self.root, now=datetime(2026, 5, 1))
        with self.assertRaises(ValueError):
            press_check.check_press(self.root, now='2026-05-01')


if __name__ == '__main__':
    unittest.main()
