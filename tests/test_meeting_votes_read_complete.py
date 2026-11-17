"""Bounded vote reads consume synthetic files through EOF before any outcome write."""
import io
import json
import os
import socket
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from campaign_tool import cli, meeting_outcome as mo
from tests import test_meeting_votes as fixtures

MID, SOURCE = fixtures.MID, fixtures.SOURCE


class CompleteVoteReads(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.MeetingVotesTests('runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.network = patch.object(socket.socket, 'connect', side_effect=AssertionError('offline'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def path(self, data):
        return self.fixture.csv(data)

    def short(self, sizes=(26, 3, 1)):
        real_read = os.read
        calls = []
        def read(fd, count):
            chunk = real_read(fd, min(count, sizes[len(calls) % len(sizes)]))
            calls.append((count, len(chunk)))
            return chunk
        return patch.object(mo.os, 'read', side_effect=read), calls

    def cli_run(self, path, replace=False):
        args = ['meetings-outcome', '--directory', str(self.fixture.root), '--id', MID,
                '--result', 'approved', '--on', '2026-10-14', '--source', SOURCE,
                '--verified-by', 'Synthetic Organizer', '--votes', str(path)]
        if replace:
            args.append('--replace')
        out, err = io.StringIO(), io.StringIO()
        old_mask = os.umask(0o077)
        os.umask(old_mask)
        try:
            with patch('sys.argv', ['campaign_tool', *args]), redirect_stdout(out), redirect_stderr(err):
                try:
                    code = cli.main()
                except SystemExit as exc:
                    code = exc.code
        finally:
            os.umask(old_mask)
        return code, out.getvalue(), err.getvalue()

    def test_short_nonempty_reads_continue_until_eof(self):
        path = self.path('name,vote\nAsh Example,yes\nBirch Example,no\n')
        expected = [(1, ['name', 'vote']), (2, ['Ash Example', 'yes']), (3, ['Birch Example', 'no'])]
        for schedule in ((1,), (26, 3, 1)):
            reader, calls = self.short(schedule)
            with self.subTest(schedule=schedule), reader:
                self.assertEqual(mo.read_votes(path), expected)
            self.assertGreater(len(calls), 2)
            self.assertEqual(calls[-1][1], 0)

    def test_chunk_boundaries_preserve_utf8_and_csv(self):
        data = 'name,vote\r\n"Zoë ""Z""\r\nExample",yes\r\nBirch Example,no'
        reader, calls = self.short((1,))
        with reader:
            self.assertEqual(mo.read_votes(self.path(data)), [(1, ['name', 'vote']),
                (2, ['Zoë "Z"\r\nExample', 'yes']), (3, ['Birch Example', 'no'])])
        self.assertEqual(calls[-1][1], 0)
        for sep in ('\n', '\r\n', '\r'):
            for end in ('', sep):
                values = ['yes', 'no', 'abstain', 'absent', 'recused']
                data = sep.join(['name,vote'] + [f'Name,{v}' for v in values]) + end
                self.assertEqual([r[1][1] for r in mo.read_votes(self.path(data))[1:]], values)

    def test_read_budget_covers_exact_limit_and_oversized_suffix(self):
        for size in (65536, 65537):
            for schedule in ((65537,), (26, 2048)):
                data = b'name,vote\nAsh Example,yes\n' + b' ' * (size - 26)
                self.assertEqual(len(data), size)
                reader, calls = self.short(schedule)
                with self.subTest(size=size, schedule=schedule), reader:
                    if size == 65536:
                        self.assertEqual(mo.read_votes(self.path(data))[1][1], ['Ash Example', 'yes'])
                        self.assertEqual(calls[-1][1], 0)
                    else:
                        with self.assertRaisesRegex(mo.MeetingOutcomeError, 'at most 65536 bytes'):
                            mo.read_votes(self.path(data))
                consumed = 0
                for requested, received in calls:
                    self.assertGreater(requested, 0)
                    self.assertLessEqual(requested, 65537 - consumed)
                    consumed += received
                self.assertLessEqual(consumed, 65537)
        self.assertEqual(mo.read_votes(self.path('')), [])
        self.assertEqual(mo.read_votes(self.path('name,vote\n')), [(1, ['name', 'vote'])])
        with self.assertRaises(mo.MeetingOutcomeError):
            self.fixture.record(self.path('name,vote\n'))

    def test_growth_after_fstat_still_obeys_actual_byte_cap(self):
        path = self.path(b'name,vote\nAsh Example,yes\n')
        real_read = os.read
        count = []
        def grow(fd, requested):
            if not count:
                with path.open('ab') as f:
                    f.write(b' ' * 65537)
            chunk = real_read(fd, min(requested, 1024))
            count.append((requested, len(chunk)))
            return chunk
        before = self.fixture.meeting.read_bytes()
        with patch.object(mo.os, 'read', side_effect=grow):
            with self.assertRaisesRegex(mo.MeetingOutcomeError, 'at most 65536 bytes'):
                self.fixture.record(path)
        self.assertEqual(sum(n for _, n in count), 65537)
        self.assertEqual(self.fixture.meeting.read_bytes(), before)

    def test_valid_short_reads_record_all_votes_in_order(self):
        path = self.path('name,vote\n Ash  Example ,YES\nBirch Example,no\n')
        original = path.read_bytes()
        doc = json.loads(self.fixture.meeting.read_text())
        expected = [{'name': 'Ash Example', 'vote': 'yes'}, {'name': 'Birch Example', 'vote': 'no'}]
        for replace in (False, True):
            reader, calls = self.short()
            with reader:
                result = self.fixture.record(path, replace=replace)
            self.assertEqual(result['votes'], expected)
            self.assertEqual(json.loads(self.fixture.meeting.read_text()), {**doc, 'outcome': result})
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(calls[-1][1], 0)
        self.fixture.record(replace=True)
        self.assertNotIn('votes', self.fixture.stored())
        self.assertFalse((self.fixture.root / 'public').exists())

    def test_late_bad_suffix_prevents_new_and_replacement_writes(self):
        prefix = b'name,vote\nAsh Example,yes\n'
        cases = [(b'"unclosed', True), (b'Birch \xff,no\n', True),
                 (b'Nobody Example,yes\n', False), (b'Birch Example,maybe\n', False),
                 (b' ' * 65537, True)]
        for replace in (False, True):
            if replace:
                self.fixture.record()
            for suffix, before_find in cases:
                path = self.path(prefix + suffix)
                files = [p for p in self.fixture.root.rglob('*') if p.is_file()] + [path]
                snapshot = {p: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode) for p in files}
                for via_cli in (False, True):
                    reader, calls = self.short((len(prefix), 4096))
                    with self.subTest(replace=replace, suffix=suffix[:20], cli=via_cli), reader, patch.object(mo, '_find', wraps=mo._find) as find:
                        if via_cli:
                            code, out, err = self.cli_run(path, replace)
                            self.assertEqual(code, 1, err)
                            self.assertEqual(out, '')
                            self.assertNotIn('Traceback', err)
                        else:
                            with self.assertRaises(mo.MeetingOutcomeError):
                                self.fixture.record(path, replace=replace)
                        if before_find:
                            find.assert_not_called()
                        self.assertGreater(len(calls), 1)
                    self.assertEqual(snapshot, {p: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode) for p in files})
                    self.assertFalse((self.fixture.root / 'public').exists())

    def test_descriptor_closed_on_every_post_open_exit(self):
        real_open, real_read = os.open, os.read
        for case in ('success', 'size', 'decode', 'csv', 'directory', 'io', 'interrupt'):
            path = self.fixture.tmp if case == 'directory' else self.path({
                'success': b'name,vote\n', 'size': b' ' * 65537,
                'decode': b'\xff', 'csv': b'"unclosed', 'io': b'abcd', 'interrupt': b'abcd'
            }.get(case, b''))
            fds, reads = [], []
            def opened(*args, **kwargs):
                fd = real_open(*args, **kwargs)
                fds.append(fd)
                return fd
            def reading(fd, count):
                if reads and case in ('io', 'interrupt'):
                    raise OSError('sentinel') if case == 'io' else KeyboardInterrupt()
                reads.append(True)
                return real_read(fd, min(count, 1))
            with self.subTest(case=case), patch.object(mo.os, 'open', side_effect=opened), patch.object(mo.os, 'read', side_effect=reading):
                if case == 'success':
                    mo.read_votes(path)
                else:
                    error = OSError if case == 'io' else KeyboardInterrupt if case == 'interrupt' else mo.MeetingOutcomeError
                    with self.assertRaises(error):
                        mo.read_votes(path)
                self.assertEqual(len(fds), 1)
                with self.assertRaises(OSError):
                    os.fstat(fds[0])
        with self.assertRaises(mo.MeetingOutcomeError):
            mo.read_votes(self.fixture.tmp / 'absent.csv')

    def test_late_io_error_never_records_a_prefix(self):
        path = self.path('name,vote\nAsh Example,yes\nBirch Example,no\n')
        real_read = os.read
        for replace in (False, True):
            if replace:
                self.fixture.record()
            before = self.fixture.meeting.read_bytes()
            for via_cli in (False, True):
                calls = []
                def broken(fd, count):
                    if calls:
                        raise OSError('sentinel')
                    calls.append(True)
                    return real_read(fd, min(count, 26))
                with patch.object(mo.os, 'read', side_effect=broken):
                    if via_cli:
                        code, out, err = self.cli_run(path, replace)
                        self.assertEqual(code, 1, err)
                        self.assertEqual(out, '')
                        self.assertNotIn('Traceback', err)
                    else:
                        with self.assertRaisesRegex(OSError, 'sentinel'):
                            self.fixture.record(path, replace=replace)
                self.assertTrue(calls)
                self.assertEqual(self.fixture.meeting.read_bytes(), before)
        result = self.fixture.record(path, replace=True)
        self.assertEqual(len(result['votes']), 2)
