"""Real-file regressions for the schedule writer's post-acquisition boundary."""
import contextlib
import io
import json
import os
import shutil
import stat
import sys
from unittest import mock

from campaign_tool import body_schedule_cmd as cmd, body_schedules, cli
from tests.test_body_schedules import Base, BODY, document, row
from tests.test_body_schedule_commands import DAY, snapshot


def identity(info):
    return info.st_dev, info.st_ino


class DirectoryAnchorTests(Base):
    def fixture(self):
        root = self.tmp / ('case-' + str(len(list(self.tmp.iterdir()))))
        shutil.copytree(self.root, root)
        content = root / 'content'
        store = content / body_schedules.FILE
        store.write_text(json.dumps(document(row())))
        store.chmod(0o640)
        other = root / 'replacement'
        other.mkdir()
        (other / body_schedules.FILE).write_text(json.dumps(document(row(body='Other Council'))))
        (other / 'sentinel').write_bytes(b'unrelated')
        return root, content, root / 'parked', other

    def swap(self, content, parked, other, kind):
        content.rename(parked)
        if kind == 'directory':
            other.rename(content)
        elif kind in ('symlink', 'dangling'):
            content.symlink_to(other if kind == 'symlink' else other / 'absent', target_is_directory=True)
        elif kind == 'file':
            content.write_bytes(b'not a directory')

    def loaded(self, content):
        return body_schedules.load_body_schedules(
            json.loads((content / body_schedules.FILE).read_text()), today=DAY)

    def assert_closed(self, fds):
        for fd in fds:
            with self.assertRaises(OSError):
                os.fstat(fd)

    def test_directory_replaced_after_acquisition_refuses_before_temp(self):
        for kind in ('directory', 'symlink', 'dangling', 'missing', 'file'):
            with self.subTest(kind=kind):
                root, content, parked, other = self.fixture()
                original, replacement = snapshot(content), snapshot(other)
                real_open = cmd._open_directory
                fds, created = [], []
                real_os_open = os.open

                def acquire(path):
                    fd = real_open(path)
                    fds.append(fd)
                    self.swap(content, parked, other, kind)
                    return fd

                def opening(path, flags, *a, **kw):
                    if flags & os.O_CREAT:
                        created.append(path)
                    return real_os_open(path, flags, *a, **kw)

                with mock.patch.object(cmd, '_open_directory', acquire), mock.patch.object(cmd.os, 'open', opening):
                    with self.assertRaisesRegex(cmd.BodyScheduleError, '^' + cmd._KEPT + '$'):
                        cmd.add_schedule(root, **row(body='New Council'), today=DAY)
                self.assertEqual(created, [])
                self.assertEqual(snapshot(parked), original)
                self.assertEqual(snapshot(content if kind == 'directory' else other), replacement)
                self.assert_closed(fds)

    def test_late_retarget_cannot_redirect_replace_or_sync(self):
        for fail_sync in (False, True):
            for kind in ('directory', 'symlink'):
                with self.subTest(kind=kind, fail_sync=fail_sync):
                    root, content, parked, other = self.fixture()
                    expected_parent = identity(content.stat())
                    foreign = snapshot(other)
                    real_replace, real_sync = os.replace, os.fsync
                    synced, installed = [], []

                    def replace(src, dst, **kw):
                        self.swap(content, parked, other, kind)
                        real_replace(src, dst, **kw)
                        installed.append(identity((parked / body_schedules.FILE).parent.stat()))

                    def sync(fd):
                        info = os.fstat(fd)
                        real_sync(fd)
                        if stat.S_ISDIR(info.st_mode):
                            synced.append(identity(info))
                            if fail_sync:
                                raise OSError('injected')

                    with mock.patch.object(cmd.os, 'replace', replace), mock.patch.object(cmd.os, 'fsync', sync):
                        if fail_sync:
                            with self.assertRaisesRegex(cmd.BodyScheduleError, '^' + cmd._UNCONFIRMED + '$'):
                                cmd.add_schedule(root, **row(body='É Council'), today=DAY)
                        else:
                            self.assertEqual(cmd.add_schedule(root, **row(body='É Council'), today=DAY),
                                             'Added body schedule: É Council.')
                    self.assertEqual(installed, [expected_parent])
                    self.assertEqual(synced, [expected_parent])
                    self.assertEqual([r['body'] for r in self.loaded(parked)], [BODY, 'É Council'])
                    self.assertEqual(stat.S_IMODE((parked / body_schedules.FILE).stat().st_mode), 0o640)
                    self.assertEqual(snapshot(content if kind == 'directory' else other), foreign)
                    self.assertFalse(list(parked.glob('.body-schedules-*.tmp')))

    def test_retarget_during_file_sync_cleans_only_anchored_temp(self):
        for kind in ('directory', 'symlink'):
            with self.subTest(kind=kind):
                root, content, parked, other = self.fixture()
                original = snapshot(content)
                real_sync = os.fsync
                temp = '.body-schedules-owned.tmp'
                (other / temp).write_bytes(b'foreign same-name sentinel')
                foreign = snapshot(other)

                def sync(fd):
                    real_sync(fd)
                    self.swap(content, parked, other, kind)

                with mock.patch.object(cmd, '_temp_name', return_value=temp), mock.patch.object(cmd.os, 'fsync', sync):
                    with self.assertRaisesRegex(cmd.BodyScheduleError, '^' + cmd._KEPT + '$'):
                        cmd.add_schedule(root, **row(body='New'), today=DAY)
                self.assertEqual(snapshot(parked), original)
                self.assertEqual(snapshot(content if kind == 'directory' else other), foreign)

    def test_exclusive_temp_collisions_preserve_foreign_entries(self):
        for exhausted in (False, True):
            with self.subTest(exhausted=exhausted):
                root, content, parked, other = self.fixture()
                regular, link, free = ['.body-schedules-' + n + '.tmp' for n in ('regular', 'link', 'free')]
                (content / regular).write_bytes(b'foreign regular')
                (content / regular).chmod(0o400)
                (content / link).symlink_to(other / 'sentinel')
                original, foreign = snapshot(content), snapshot(other)
                names = [regular, link] * 5 if exhausted else [regular, link, free]
                with mock.patch.object(cmd, '_temp_name', side_effect=names) as generate:
                    if exhausted:
                        with self.assertRaisesRegex(cmd.BodyScheduleError, '^' + cmd._KEPT + '$'):
                            cmd.add_schedule(root, **row(body='New'), today=DAY)
                        self.assertEqual(snapshot(content), original)
                        self.assertEqual(generate.call_count, 10)
                    else:
                        cmd.add_schedule(root, **row(body='New'), today=DAY)
                        after = snapshot(content)
                        after[body_schedules.FILE] = original[body_schedules.FILE]
                        self.assertEqual(after, original)
                self.assertEqual(snapshot(other), foreign)

    def test_preinstall_faults_close_descriptors_and_keep_primary_error(self):
        for fault in ('create', 'fchmod', 'fdopen', 'write', 'flush', 'fsync', 'replace'):
            with self.subTest(fault=fault):
                root, content, parked, other = self.fixture()
                original, foreign = snapshot(content), snapshot(other)
                opened = []
                real_open, real_fdopen, real_close = os.open, os.fdopen, os.close
                unrelated = real_open(other / 'sentinel', os.O_RDONLY)
                self.addCleanup(os.close, unrelated)

                def opening(path, flags, *a, **kw):
                    if flags & os.O_CREAT and fault == 'create':
                        self.swap(content, parked, other, 'directory')
                        raise OSError('primary')
                    fd = real_open(path, flags, *a, **kw)
                    if flags & (os.O_CREAT | os.O_DIRECTORY):
                        opened.append(fd)
                    return fd

                def fail(*a, **kw):
                    self.swap(content, parked, other, 'directory')
                    raise OSError('primary')

                def stream(fd, *a, **kw):
                    if a and a[0] == 'rb':
                        return real_fdopen(fd, *a, **kw)
                    if fault == 'fdopen':
                        return fail()
                    out = real_fdopen(fd, *a, **kw)
                    proxy = mock.MagicMock(wraps=out)
                    proxy.__enter__.return_value = proxy
                    proxy.__exit__.side_effect = lambda *a: out.close()
                    getattr(proxy, fault).side_effect = fail
                    return proxy

                def close(fd):
                    real_close(fd)
                    raise OSError('secondary close')

                with contextlib.ExitStack() as stack:
                    stack.enter_context(mock.patch.object(cmd.os, 'open', opening))
                    stack.enter_context(mock.patch.object(cmd.os, 'close', close))
                    if fault in ('fdopen', 'write', 'flush'):
                        stack.enter_context(mock.patch.object(cmd.os, 'fdopen', stream))
                    elif fault != 'create':
                        stack.enter_context(mock.patch.object(cmd.os, fault, fail))
                    with self.assertRaisesRegex(cmd.BodyScheduleError, '^' + cmd._KEPT + '$'):
                        cmd.add_schedule(root, **row(body='New'), today=DAY)
                self.assertEqual(snapshot(parked), original)
                self.assertEqual(snapshot(content), foreign)
                self.assert_closed(opened)
                self.assertTrue(stat.S_ISREG(os.fstat(unrelated).st_mode))

    def test_uncertain_identity_refuses_before_output(self):
        root, content, parked, other = self.fixture()
        before = snapshot(root)
        real_stat = os.stat

        def uncertain(path, *a, **kw):
            if path == content and kw.get('follow_symlinks') is False:
                raise OSError('identity unavailable')
            return real_stat(path, *a, **kw)

        # Invoke the writer directly so initial API path admission is not the failing seam.
        with mock.patch.object(cmd.os, 'stat', uncertain):
            with self.assertRaisesRegex(cmd.BodyScheduleError, '^' + cmd._KEPT + '$'):
                cmd._write(content, [row(body='New')], DAY)
        self.assertEqual(snapshot(root), before)

    def test_cleanup_failure_preserves_primary_diagnostic_and_foreign_entry(self):
        root, content, parked, other = self.fixture()
        temp = '.body-schedules-owned.tmp'
        (other / temp).write_bytes(b'foreign sentinel')
        foreign = snapshot(other)
        before = (content / body_schedules.FILE).read_bytes()
        real_unlink = os.unlink
        attempts = []

        def fail_sync(fd):
            self.swap(content, parked, other, 'directory')
            raise OSError('primary write failure')

        def unlink(name, **kw):
            attempts.append(identity(os.fstat(kw['dir_fd'])))
            raise OSError('secondary cleanup failure')

        expected = identity(content.stat())
        with mock.patch.object(cmd, '_temp_name', return_value=temp), \
                mock.patch.object(cmd.os, 'fsync', fail_sync), mock.patch.object(cmd.os, 'unlink', unlink):
            with self.assertRaisesRegex(cmd.BodyScheduleError, '^' + cmd._KEPT + '$'):
                cmd.add_schedule(root, **row(body='New'), today=DAY)
        self.assertEqual(attempts, [expected])
        self.assertEqual(snapshot(content), foreign)
        self.assertEqual((parked / body_schedules.FILE).read_bytes(), before)
        self.assertTrue((parked / temp).is_file())
        real_unlink(parked / temp)  # injected cleanup failure deliberately retained this owned temp

    def test_cli_observed_retarget_refuses_without_success(self):
        root, content, parked, other = self.fixture()
        real_open = cmd._open_directory

        def acquire(path):
            fd = real_open(path)
            self.swap(content, parked, other, 'directory')
            return fd

        args = ['campaign_tool', 'body-schedule', 'remove', '--directory', str(root), '--body', BODY]
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cmd, '_open_directory', acquire), mock.patch.object(sys, 'argv', args), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as caught:
                cli.main()
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(out.getvalue(), '')
        self.assertIn(cmd._KEPT, err.getvalue())

    def test_all_mutators_use_same_directory_and_no_unrelated_writes(self):
        root, content, parked, other = self.fixture()
        before = snapshot(root)
        real_replace, real_sync = os.replace, os.fsync
        expected = identity(content.stat())
        events = []

        def replace(src, dst, **kw):
            real_replace(src, dst, **kw)
            events.append(('replace', identity((content / body_schedules.FILE).parent.stat())))

        def sync(fd):
            real_sync(fd)
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                events.append(('sync', identity(os.fstat(fd))))

        with mock.patch('socket.socket', side_effect=AssertionError('network')), \
                mock.patch('campaign_tool.site.build_site', side_effect=AssertionError('build')), \
                mock.patch('campaign_tool.cli.database', side_effect=AssertionError('ledger')), \
                mock.patch.object(cmd.os, 'replace', replace), mock.patch.object(cmd.os, 'fsync', sync):
            cmd.add_schedule(root, **row(body='É Council'), today=DAY)
            self.loaded(content)
            cmd.recheck_schedule(root, body=BODY, checked_on=DAY.isoformat(), today=DAY)
            self.loaded(content)
            cmd.remove_schedule(root, body='É Council', today=DAY)
            cmd.remove_schedule(root, body=BODY, today=DAY)
        self.assertEqual(events, [('replace', expected), ('sync', expected)] * 4)
        self.assertEqual(self.loaded(content), [])
        after = snapshot(root)
        after['content/' + body_schedules.FILE] = before['content/' + body_schedules.FILE]
        self.assertEqual(after, before)
