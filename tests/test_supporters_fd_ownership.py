"""RF1025-4: real descriptor lifetimes, synthetic read-only supporter inputs."""
import contextlib
import errno
import os
import stat
from datetime import date
from pathlib import Path
from unittest import mock

from campaign_tool import supporters as s
from tests.test_supporters_list import ListBase, public_row, record, REFUSAL

TODAY = date(2026, 11, 10)
REAL_OPEN, REAL_CLOSE, REAL_FDOPEN, REAL_FSTAT = os.open, os.close, os.fdopen, os.fstat


class Stream:
    def __init__(self, owner, life, handle):
        self.owner, self.life, self.handle = owner, life, handle

    def __enter__(self):
        self.handle.__enter__()
        return self

    def fileno(self):
        return self.handle.fileno()

    def read(self, size):
        self.life['reads'].append(size)
        if self.owner.read_error and self.life['path'] == self.owner.target:
            raise self.owner.read_error
        return self.handle.read(size)

    def __exit__(self, *args):
        try:
            return self.handle.__exit__(*args)
        finally:
            self.owner.closed(self.life['fd'])
            if self.owner.reuse and self.life['path'] == self.owner.target:
                fd = REAL_OPEN(os.devnull, os.O_RDONLY)
                self.owner.reused.append(fd)
                self.owner.case.assertEqual(fd, self.life['fd'], 'sentinel must reuse the closed integer')


class Lifetimes:
    """Record each acquisition separately, even when the kernel reuses an fd."""
    def __init__(self, case, target, error=None, cleanup_error=False, reuse=False, read_error=None):
        self.case, self.target, self.error = case, target, error
        self.cleanup_error, self.reuse, self.read_error = cleanup_error, reuse, read_error
        self.lives, self.active, self.reused = [], {}, []

    def open(self, path, flags, *args, **kwargs):
        fd = REAL_OPEN(path, flags, *args, **kwargs)
        if Path(path).is_relative_to(self.case.root):
            life = dict(path=Path(path), fd=fd, closes=0, reads=[], flags=flags)
            self.lives.append(life)
            self.active[fd] = life
        return fd

    def closed(self, fd):
        life = self.active.pop(fd)
        life['closes'] += 1

    def close(self, fd):
        REAL_CLOSE(fd)
        if fd in self.active:
            self.closed(fd)
            if self.cleanup_error:
                raise OSError('synthetic cleanup failure after actual close')

    def fdopen(self, fd, *args, **kwargs):
        life = self.active.get(fd)
        if life and life['path'] == self.target and self.error is not None:
            raise self.error
        handle = REAL_FDOPEN(fd, *args, **kwargs)
        return Stream(self, life, handle) if life else handle

    @contextlib.contextmanager
    def installed(self):
        sentinel = REAL_OPEN(os.devnull, os.O_RDONLY)
        try:
            with mock.patch.object(s.os, 'open', self.open), mock.patch.object(s.os, 'close', self.close), \
                    mock.patch.object(s.os, 'fdopen', self.fdopen):
                yield self
            REAL_FSTAT(sentinel)
            for fd in self.reused:
                REAL_FSTAT(fd)
            self.case.assertTrue(any(life['path'] == self.target for life in self.lives), 'no target acquisition')
            for life in self.lives:
                self.case.assertEqual(life['closes'], 1, life)
                self.case.assertEqual(life['flags'], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                if life['fd'] not in self.reused:
                    with self.case.assertRaises(OSError) as caught:
                        REAL_FSTAT(life['fd'])
                    self.case.assertEqual(caught.exception.errno, errno.EBADF)
        finally:
            # Also clean up leaks on the RED implementation, without confusing reused lifetimes.
            for fd in list(self.active):
                REAL_CLOSE(fd)
            for fd in self.reused:
                REAL_CLOSE(fd)
            REAL_CLOSE(sentinel)


class SupporterFdOwnershipTests(ListBase):
    def setUp(self):
        super().setUp()
        self.row = public_row('Alpha Union')
        self.rec = record(self.row, cosign='any')
        self.write_public([self.row])
        self.write_private({'alpha-union': self.rec})
        self.meeting('a-good.json', ['alpha-union'])
        self.meeting('z-last.json', ['alpha-union'])
        self.meeting_file = self.root / 'content' / 'meetings' / 'z-last.json'
        for name in ('_install_pair', '_install_private', '_temporary', '_ensure_private_dir', '_correspondence'):
            patch = mock.patch.object(s, name, side_effect=AssertionError('reader reached writer'))
            patch.start()
            self.addCleanup(patch.stop)
        for name in ('socket.socket.connect', 'campaign_tool.site.build_site'):
            patch = mock.patch(name, side_effect=AssertionError('reader reached network or publisher'))
            patch.start()
            self.addCleanup(patch.stop)
        self.callers = {
            'public': (self.public_file, lambda: s.load_supporters(self.root, today=TODAY)),
            'private': (self.private_file, lambda: s.read_consent(self.root, today=TODAY)),
            'meeting': (self.meeting_file, lambda: s.list_supporters(self.root, today=TODAY)),
        }

    def snapshot(self):
        result = {}
        for path in (self.root, *sorted(self.root.rglob('*'))):
            info = path.lstat()
            result[str(path.relative_to(self.root))] = (
                stat.S_IMODE(info.st_mode), info.st_mtime_ns, path.read_bytes() if path.is_file() else None)
        return result

    def test_public_private_and_staged_construction_failures(self):
        for name, (path, call) in self.callers.items():
            for cls in (OSError, RuntimeError, KeyboardInterrupt, SystemExit):
                with self.subTest(reader=name, exception=cls.__name__):
                    before = self.snapshot()
                    error = cls('synthetic private construction canary')
                    with Lifetimes(self, path, error).installed() as seen:
                        with self.assertRaises(cls) as caught:
                            call()
                        self.assertIs(caught.exception, error)
                    if name == 'meeting':
                        self.assertEqual([life['path'] for life in seen.lives], [
                            self.public_file, self.private_file, path.with_name('a-good.json'), path])
                    self.assertEqual(self.snapshot(), before)

    def test_cleanup_error_preserves_primary_exception(self):
        for name, (path, call) in self.callers.items():
            with self.subTest(reader=name):
                before = self.snapshot()
                error = KeyboardInterrupt('original interruption')
                with Lifetimes(self, path, error, cleanup_error=True).installed():
                    with self.assertRaises(KeyboardInterrupt) as caught:
                        call()
                    self.assertIs(caught.exception, error)
                self.assertEqual(self.snapshot(), before)

    def test_success_and_read_fstat_validation_errors_have_only_stream_ownership(self):
        for name, (path, call) in self.callers.items():
            original = path.read_bytes()
            for failure in ('success', 'read', 'fstat', 'validation', 'oversize'):
                with self.subTest(reader=name, outcome=failure):
                    if failure == 'validation':
                        path.write_bytes(b'{broken')
                    elif failure == 'oversize':
                        cap = s.MAX_MEETING_BYTES if name == 'meeting' else s.MAX_BYTES
                        path.write_bytes(b' ' * (cap + 1))
                    else:
                        path.write_bytes(original)
                    before = self.snapshot()
                    error = OSError('synthetic stream failure')
                    tracker = Lifetimes(self, path, reuse=True, read_error=error if failure == 'read' else None)

                    def fstat(fd):
                        life = tracker.active.get(fd)
                        if failure == 'fstat' and life and life['path'] == path:
                            raise error
                        return REAL_FSTAT(fd)

                    with tracker.installed(), mock.patch.object(s.os, 'fstat', fstat):
                        if failure == 'success':
                            self.assertTrue(call())
                        else:
                            with self.assertRaises(OSError if failure in ('read', 'fstat') else s.SupporterError):
                                call()
                    target = next(life for life in tracker.lives if life['path'] == path)
                    if failure != 'fstat':
                        self.assertEqual(target['reads'], [
                            (s.MAX_MEETING_BYTES if name == 'meeting' else s.MAX_BYTES) + 1])
                    self.assertEqual(self.snapshot(), before)
            path.write_bytes(original)

    def test_private_permission_refusal_keeps_reused_sentinel_open(self):
        os.chmod(self.private_file, 0o644)
        before = self.snapshot()
        with Lifetimes(self, self.private_file, reuse=True).installed() as tracker:
            with self.assertRaises(s.SupporterError):
                s.read_consent(self.root, today=TODAY)
        self.assertEqual(tracker.lives[0]['reads'], [])
        self.assertEqual(self.snapshot(), before)

    def test_repeated_in_process_cli_failures_and_recovery(self):
        before = self.snapshot()
        for path, _ in self.callers.values():
            for extra in ((), ('--json',)):
                for attempt in range(3):
                    with self.subTest(path=path.name, extra=extra, attempt=attempt):
                        with Lifetimes(self, path, OSError('private exception canary')).installed():
                            self.assertEqual(self.run_list(*extra), (2, '', REFUSAL))
        self.assertEqual(self.ok('--json'), s.list_supporters(self.root, today=TODAY))
        self.assertEqual(self.snapshot(), before)

    def test_success_projections_and_consent_binding_remain_read_only(self):
        before = self.snapshot()
        self.assertEqual(s.load_supporters(self.root, today=TODAY), [self.row])
        self.assertEqual(s.read_consent(self.root, today=TODAY), {'alpha-union': self.rec})
        self.assertEqual(s.list_supporters(self.root, today=TODAY), [{
            'id': 'alpha-union', 'status': 'listed', 'kind': 'community', 'name': 'Alpha Union',
            'joined_on': '2026-09-01', 'consent_present': True, 'confirmed_on': '2026-09-01',
            'cosign': 'any', 'cosign_confirmed_on': '2026-09-05',
            'staged_meetings': ['a-good', 'z-last']}])
        self.assertEqual(s.resolve_cosigners(self.root, 'a-good', ['alpha-union'], today=TODAY), [self.row])
        self.assertEqual(self.snapshot(), before)
        # A stale binding still refuses; the repair never bypasses consent validation.
        self.write_private({'alpha-union': {**self.rec, 'public_sha256': '0' * 64}})
        before = self.snapshot()
        with self.assertRaises(s.SupporterError):
            s.load_supporters(self.root, today=TODAY)
        self.assertEqual(self.run_list(), (2, '', REFUSAL))
        self.assertEqual(self.snapshot(), before)
