"""RF1500-23: press check binds each freshness read to the regular file it inspected, fictional content only.

A different regular file renamed over a draft, letter or JSON index between the no-follow stat and the descriptor
open is refused before any byte is read: one unreadable row, never the replacement's findings, ids or status.

Run from the repository root: python3 -B -m unittest tests.test_press_check_file_identity -q
"""
import contextlib
import errno
import io
import json
import os
import stat
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from campaign_tool import cli, press_check, today
from tests import test_press_check as base_mod

FIRST, SECOND, NOW, DAY, MEETING = base_mod.FIRST, base_mod.SECOND, base_mod.NOW, base_mod.DAY, base_mod.MEETING
CHANGED = [{'code': 'unreadable', 'message': 'file changed while being opened'}]
REAL_OPEN, REAL_READ, REAL_CLOSE, REAL_FSTAT = os.open, os.read, os.close, os.fstat
HEADER = '<!-- DRAFT: not sent. Review before any use. findings {} -->\n\nbody\n'
SIGNUP = 'I encourage readers to visit the campaign signup page: /#participate.\n'
WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND


def closed(fd):
    try:
        REAL_FSTAT(fd)
    except OSError as exc:
        return exc.errno == errno.EBADF
    return False


class Swap:
    """Wrap the checker's os.open/os.read: rename a prepared file over ``name`` just before its real open."""

    def __init__(self, name, replacement):
        self.name, self.replacement = name, replacement
        self.swapped, self.captured, self.read_fds, self.closed_fds, self.writes = 0, [], [], [], []
        self.live, self.captured_reads = set(), []  # reads while a captured descriptor is still open

    def open(self, path, flags, *args, dir_fd=None, **kwargs):
        if flags & WRITE_FLAGS:
            self.writes.append(path)
            raise AssertionError('press check opened a file for writing')
        hit = path == self.name and not flags & os.O_DIRECTORY and dir_fd is not None and self.replacement
        if hit:
            os.replace(self.replacement, path, dst_dir_fd=dir_fd)
            self.swapped += 1
            self.replacement = None
        fd = REAL_OPEN(path, flags, *args, dir_fd=dir_fd, **kwargs)
        if hit:
            self.captured.append(fd)
            self.live.add(fd)
        return fd

    def read(self, fd, size):
        self.read_fds.append(fd)
        if fd in self.live:
            self.captured_reads.append(fd)
        return REAL_READ(fd, size)

    def close(self, fd):
        self.closed_fds.append(fd)
        self.live.discard(fd)
        return REAL_CLOSE(fd)

    @contextlib.contextmanager
    def active(self):
        with mock.patch('campaign_tool.press_check.os.open', side_effect=self.open), \
                mock.patch('campaign_tool.press_check.os.read', side_effect=self.read), \
                mock.patch('campaign_tool.press_check.os.close', side_effect=self.close):
            yield self


class IdentityBase(base_mod.Base):
    def setUp(self):
        super().setUp()
        self.staging = self.top / 'staging'
        self.staging.mkdir()

    def prepare(self, text, name='replacement'):
        path = self.staging / name
        path.write_text(text, encoding='utf-8')
        return path

    def swapped_check(self, rel, replacement_text):
        """check_press while a prepared regular file replaces ``rel`` at its open seam; returns (report, swap)."""
        target = self.root / rel
        replacement = self.prepare(replacement_text)
        self.assertNotEqual((target.stat().st_dev, target.stat().st_ino),
                            (replacement.stat().st_dev, replacement.stat().st_ino))
        swap = Swap(target.name, replacement)
        sentinel = REAL_OPEN(os.devnull, os.O_RDONLY)
        self.addCleanup(REAL_CLOSE, sentinel)
        with swap.active():
            report = self.check()
        self.assertEqual(swap.swapped, 1)
        fd = swap.captured[0]
        self.assertIn(fd, swap.closed_fds)
        self.assertEqual(swap.captured_reads, [], 'the replacement descriptor was read')
        self.assertEqual(swap.live, set())
        self.assertEqual(swap.writes, [])
        self.assertFalse(closed(sentinel))
        self.assertEqual(target.read_text(encoding='utf-8'), replacement_text)  # the fixture's swap happened
        return report, swap

    def stale_and_healthy(self, folder, letter):
        """Original stale header (cites an unpublished id) and a healthy replacement for one dated file."""
        ending = SIGNUP if letter else ''
        stale = HEADER.format('fnd_gone') + ending
        healthy = HEADER.format(FIRST) + ending
        rel = f'{folder}/{DAY}-swapped.md'
        self.put(rel, stale)
        return rel, healthy


class MarkdownTests(IdentityBase):
    def test_regular_markdown_replacement_is_refused_before_body_read(self):
        draft, letter = self.draft(), self.letter()
        for folder, kind, is_letter in (('kit/press', 'draft', False), ('kit/press.suggested', 'draft', False),
                                        ('kit/press/letters', 'letter', True),
                                        ('kit/press/letters.suggested', 'letter', True)):
            with self.subTest(folder=folder):
                rel, healthy = self.stale_and_healthy(folder, is_letter)
                rows = self.rows()
                self.assertEqual(rows[rel]['status'], 'stale')  # stable: the stale original is reported
                report, _swap = self.swapped_check(rel, healthy)
                rows = {row['path']: row for row in report['drafts']}
                self.assertEqual(rows[rel], {'path': rel, 'kind': kind, 'status': 'unreadable', 'findings': [],
                                             'reasons': CHANGED})
                self.assertEqual(rows[draft]['status'], 'ok')
                self.assertEqual(rows[letter]['status'], 'ok')
                self.assertEqual(report['problems'], 1)
                self.assertNotIn(FIRST, json.dumps(rows[rel]))
                # settled namespace: an ordinary later run inspects the replacement normally
                self.assertEqual(self.rows()[rel]['status'], 'ok')
                (self.root / rel).unlink()


class JsonIndexTests(IdentityBase):
    def test_each_json_index_replacement_marks_input_unavailable(self):
        draft = self.draft((FIRST, SECOND))
        # content/findings: the replacement would withdraw nothing but claims a correction; never parsed
        rel = 'content/findings/cedar-policy-posted.json'
        replacement = dict(self.first, corrections=self.first['corrections'] + [{'date': '2026-04-01', 'text': 'x'}])
        report, _ = self.swapped_check(rel, json.dumps(replacement))
        rows = {row['path']: row for row in report['drafts']}
        self.assertEqual(rows[rel], {'path': rel, 'kind': 'input', 'status': 'unreadable', 'findings': [],
                                     'reasons': CHANGED})
        self.assertEqual(rows[draft]['status'], 'not_checked')
        self.assertEqual(rows[draft]['reasons'], [{'code': 'not_checked', 'message': 'finding index unavailable'}] * 2)
        self.assertEqual(report['problems'], 2)
        self.assertNotIn('2026-04-01', json.dumps(report))

    def test_withdrawal_replacement_keeps_safe_stale_precedence(self):
        draft = self.draft((FIRST, SECOND))
        self.put('private/withdrawn/a.json', json.dumps({'id': SECOND}))
        self.put('private/withdrawn/b.json', json.dumps({'id': FIRST}))
        self.assertEqual(self.rows()[draft]['reasons'], [{'code': 'withdrawn', 'finding_id': FIRST},
                                                         {'code': 'withdrawn', 'finding_id': SECOND}])
        report, _ = self.swapped_check('private/withdrawn/b.json', json.dumps({'id': 'fnd_0000000000000009'}))
        rows = {row['path']: row for row in report['drafts']}
        self.assertEqual(rows['private/withdrawn/b.json']['reasons'], CHANGED)
        self.assertEqual(rows['private/withdrawn/b.json']['status'], 'unreadable')
        # SECOND's readable withdrawal still makes the draft stale; FIRST is not derived from the replacement
        self.assertEqual(rows[draft]['status'], 'stale')
        self.assertEqual(rows[draft]['reasons'], [{'code': 'not_checked', 'message': 'finding index unavailable'},
                                                  {'code': 'withdrawn', 'finding_id': SECOND}])
        self.assertNotIn('fnd_0000000000000009', json.dumps(report))

    def test_cancelled_meeting_replaced_by_scheduled_is_not_checked(self):
        letter = self.letter()
        self.meeting(MEETING, '2026-06-10T18:00:00-07:00', status='cancelled')
        self.assertIn({'code': 'meeting_cancelled', 'meeting_id': MEETING}, self.rows()[letter]['reasons'])
        rel = f'content/meetings/{MEETING}.json'
        scheduled = json.loads((self.root / rel).read_text())
        scheduled.pop('status')
        report, _ = self.swapped_check(rel, json.dumps(scheduled))
        rows = {row['path']: row for row in report['drafts']}
        self.assertEqual(rows[rel], {'path': rel, 'kind': 'input', 'status': 'unreadable', 'findings': [],
                                     'reasons': CHANGED})
        self.assertEqual(rows[letter]['status'], 'not_checked')
        self.assertEqual(rows[letter]['reasons'], [{'code': 'not_checked', 'message': 'meeting file invalid or duplicated'}])


class PrimitiveTests(IdentityBase):
    def folder(self):
        fd = REAL_OPEN(self.staging, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(REAL_CLOSE, fd)
        return fd

    def test_primitive_refuses_replacement_and_closes_only_its_descriptor(self):
        dir_fd = self.folder()
        sentinel = REAL_OPEN(os.devnull, os.O_RDONLY)
        self.addCleanup(REAL_CLOSE, sentinel)
        for attempt in range(20):  # repeated faults retain no descriptor
            (self.staging / 'target').write_bytes(b'{"id":"old"}')
            new = self.staging / 'new'
            new.write_bytes(b'{"id":"new"}')
            swap = Swap('target', new)
            with swap.active(), self.assertRaises(press_check._Hazard) as caught:
                press_check._read(dir_fd, 'target', 100)
            self.assertEqual((caught.exception.status, caught.exception.message),
                             ('unreadable', 'file changed while being opened'))
            fd = swap.captured[0]
            self.assertEqual(swap.read_fds, [])
            self.assertEqual(swap.closed_fds, [fd])
            self.assertTrue(closed(fd))
            self.assertFalse(closed(dir_fd))
            self.assertFalse(closed(sentinel))
        self.assertEqual(press_check._read(dir_fd, 'target', 100), b'{"id":"new"}')  # settled: read normally

    def test_open_refusal_closes_nothing(self):
        dir_fd = self.folder()
        (self.staging / 'target').write_bytes(b'x')
        sentinel = REAL_OPEN(os.devnull, os.O_RDONLY)
        self.addCleanup(REAL_CLOSE, sentinel)
        swap = Swap('target', None)
        with swap.active(), mock.patch('campaign_tool.press_check.os.open', side_effect=PermissionError(errno.EACCES, 'x')), \
                self.assertRaises(press_check._Hazard) as caught:
            press_check._read(dir_fd, 'target', 100)
        self.assertEqual(caught.exception.message, 'cannot be read')
        self.assertEqual(swap.closed_fds, [])
        self.assertFalse(closed(sentinel))
        self.assertFalse(closed(dir_fd))

    def test_identity_uses_both_device_and_inode_and_preserves_type_precedence(self):
        dir_fd = self.folder()
        (self.staging / 'target').write_bytes(b'same')

        def faked(change):
            def fstat(fd):
                real = list(REAL_FSTAT(fd)[:10])
                for index, value in change(real).items():
                    real[index] = value
                return os.stat_result(real)
            return mock.patch('campaign_tool.press_check.os.fstat', side_effect=fstat)

        for label, change in (('inode', lambda s: {stat.ST_INO: s[stat.ST_INO] + 1}),
                              ('device', lambda s: {stat.ST_DEV: s[stat.ST_DEV] + 1})):
            with self.subTest(label), faked(change):
                with self.assertRaises(press_check._Hazard) as caught:
                    press_check._read(dir_fd, 'target', 100)
                self.assertEqual((caught.exception.status, caught.exception.message),
                                 ('unreadable', 'file changed while being opened'))
        self.assertEqual(press_check._read(dir_fd, 'target', 100), b'same')  # equal identity accepted
        # a descriptor of the wrong type stays not_regular even when its identity also differs
        for mode in (stat.S_IFIFO | 0o600, stat.S_IFDIR | 0o700):
            with self.subTest(mode=oct(mode)), \
                    faked(lambda s, m=mode: {stat.ST_MODE: m, stat.ST_INO: s[stat.ST_INO] + 1}):
                with self.assertRaises(press_check._Hazard) as caught:
                    press_check._read(dir_fd, 'target', 100)
                self.assertEqual(caught.exception.status, 'not_regular')
        # real late FIFO and late symlink: neither blocks, neither is followed or read
        os.mkfifo(self.staging / 'fifo')
        swap = Swap('target', self.staging / 'fifo')
        with swap.active(), self.assertRaises(press_check._Hazard) as caught:
            press_check._read(dir_fd, 'target', 100)
        self.assertEqual(caught.exception.status, 'not_regular')
        self.assertEqual(swap.read_fds, [])
        (self.staging / 'target').unlink()
        (self.staging / 'target').write_bytes(b'same')
        (self.staging / 'outside').write_bytes(b'secret')
        (self.staging / 'link').symlink_to(self.staging / 'outside')
        swap = Swap('target', self.staging / 'link')
        with swap.active(), self.assertRaises(press_check._Hazard) as caught:
            press_check._read(dir_fd, 'target', 100)
        self.assertEqual(caught.exception.status, 'not_regular')
        self.assertEqual(swap.captured, [])

    def test_limits_and_short_reads_survive(self):
        dir_fd = self.folder()
        (self.staging / 'target').write_bytes(b'abcdef')
        self.assertEqual(press_check._read(dir_fd, 'target', 6), b'abcdef')
        with self.assertRaises(press_check._Hazard) as caught:
            press_check._read(dir_fd, 'target', 5)
        self.assertEqual(caught.exception.message, 'file is too large')
        with mock.patch('campaign_tool.press_check.os.read', side_effect=lambda fd, n: REAL_READ(fd, min(n, 1))):
            self.assertEqual(press_check._read(dir_fd, 'target', 6), b'abcdef')


class ConsumerTests(IdentityBase):
    def run_cli(self, *args):
        argv = ['campaign_tool', 'press', 'check', '--directory', str(self.root), *args]
        out, err = io.StringIO(), io.StringIO()
        old = os.umask(0o077)
        os.umask(old)  # cli.main sets its own umask; restore the test process's afterwards
        try:
            with mock.patch.object(sys, 'argv', argv), contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as done:
                cli.main()
        finally:
            os.umask(old)
        return done.exception.code, out.getvalue(), err.getvalue()

    def test_cli_text_json_and_today_report_the_real_refusal(self):
        draft = self.draft()
        rel, healthy = self.stale_and_healthy('kit/press', False)
        # stable control first: a real subprocess sees the stale original and exits 3
        done = self.cli('--json')
        self.assertEqual(done.returncode, 3)
        self.assertEqual({row['path']: row['status'] for row in json.loads(done.stdout)['drafts']},
                         {draft: 'ok', rel: 'stale'})
        (self.root / rel).unlink()
        done = self.cli()
        self.assertEqual((done.returncode, done.stdout), (0, f'{draft}: ok\n'))
        for as_json in (False, True):
            with self.subTest(as_json=as_json):
                self.put(rel, HEADER.format('fnd_gone'))
                swap = Swap(Path(rel).name, self.prepare(healthy))
                with swap.active():
                    code, out, err = self.run_cli(*(['--json'] if as_json else []))
                self.assertEqual(code, 3)
                self.assertEqual(swap.captured_reads, [])
                if as_json:
                    self.assertEqual(json.loads(out), {'drafts': [
                        {'path': draft, 'kind': 'draft', 'status': 'ok', 'findings': [FIRST], 'reasons': []},
                        {'path': rel, 'kind': 'draft', 'status': 'unreadable', 'findings': [], 'reasons': CHANGED}],
                        'problems': 1})
                else:
                    self.assertEqual(out, f'{draft}: ok\n'
                                          f'{rel}: unreadable: unreadable: file changed while being opened\n')
                for canary in ('Traceback', str(self.top), 'fnd_gone', FIRST + ' -->', 'Errno', 'body'):
                    self.assertNotIn(canary, out + err)
                (self.root / rel).unlink()
        # today's press adapter counts the repaired refusal and emits its existing warning
        self.put(rel, HEADER.format('fnd_gone'))
        swap = Swap(Path(rel).name, self.prepare(healthy))
        with swap.active():
            items = today._press(self.root, date(2026, 5, 1), '--directory x')
        self.assertEqual([item['what'] for item in items], ['press: 1 draft check problem(s); inspect before use'])
        self.assertEqual(swap.captured_reads, [])
        self.assertEqual(today._press(self.root, date(2026, 5, 1), '--directory x'), [])  # settled and healthy


class ReadOnlyTests(IdentityBase):
    def test_stable_retry_limits_and_read_only_contracts_survive(self):
        draft, letter = self.draft(), self.letter()  # the letter makes the lazy meeting index load
        self.put(f'kit/press/{DAY}-bad-utf8.md', 'x')
        (self.root / f'kit/press/{DAY}-bad-utf8.md').write_bytes(b'\xff\xfe')
        self.put('content/meetings/broken.json', '{')
        self.put('content/meetings/repeat.json', '{"id": "a", "id": "b"}')
        rel, healthy = self.stale_and_healthy('kit/press', False)
        expected_other = {
            f'kit/press/{DAY}-bad-utf8.md': [{'code': 'unreadable', 'message': 'file is not valid UTF-8'}],
            'content/meetings/broken.json': [{'code': 'invalid_input', 'message': 'file is not valid UTF-8 JSON'}],
            'content/meetings/repeat.json': [{'code': 'invalid_input', 'message': 'duplicate JSON object member names'}]}
        before = base_mod.snapshot(self.root)
        replacement = self.prepare(healthy)
        replacement_entry = base_mod.snapshot(self.staging)['replacement']
        swap = Swap(Path(rel).name, replacement)
        with swap.active():
            report = self.check()
        rows = {row['path']: row for row in report['drafts']}
        self.assertEqual(rows[rel]['reasons'], CHANGED)
        for path, reasons in expected_other.items():
            self.assertEqual(rows[path]['reasons'], reasons)
        self.assertEqual(rows[draft]['status'], 'ok')
        self.assertEqual(rows[letter]['status'], 'ok')  # its own meeting is still found; existing precedence
        after = base_mod.snapshot(self.root)
        # only the fixture-performed rename differs: the target entry and its folder's mtime
        self.assertEqual(after.pop(rel), replacement_entry)
        before.pop(rel)
        self.assertEqual(after.pop('kit/press')[:3], before.pop('kit/press')[:3])
        self.assertEqual(after, before)
        self.assertEqual(swap.writes, [])
        self.assertEqual(self.rows()[rel]['status'], 'ok')  # settled: ordinary later run reads the replacement


if __name__ == '__main__':
    unittest.main()
