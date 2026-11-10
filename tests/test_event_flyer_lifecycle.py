"""RF1005-12: build_event closes the raw logo descriptor when stream construction fails (synthetic, offline)."""
import contextlib
import errno
import io
import os
import stat
import sys
import tempfile
import unittest
from unittest import mock

from campaign_tool import cli, flyer
from tests import test_event_flyer as base  # fixture helpers only; its TestCase is composed, never re-exported

SVG = base.SVG
CONSTRUCTOR_FAILURE = OSError(errno.EMFILE, 'synthetic stream construction failure')


class Sentinel(BaseException):
    """Deterministic non-Exception failure; never escapes the harness."""


def is_open(descriptor):
    try:
        os.fstat(descriptor)
    except OSError as exc:
        if exc.errno == errno.EBADF:
            return False
        raise
    return True


class Seams:
    """Records real mkstemp allocations and raises at the fdopen seam, restoring both on exit."""

    def __init__(self, fail=None, wrap=None):
        self.allocated = []  # (descriptor, name)
        self.fail = fail
        self.wrap = wrap
        self.raw_closes = []
        self._stack = contextlib.ExitStack()

    def __enter__(self):
        real_mkstemp, real_fdopen, real_close = tempfile.mkstemp, os.fdopen, os.close

        def mkstemp(*args, **kw):
            descriptor, name = real_mkstemp(*args, **kw)
            self.allocated.append((descriptor, name))
            return descriptor, name

        def fdopen(descriptor, *args, **kw):
            if self.fail is not None:
                raise self.fail
            stream = real_fdopen(descriptor, *args, **kw)
            return self.wrap(stream) if self.wrap else stream

        def close(descriptor):
            self.raw_closes.append(descriptor)
            return real_close(descriptor)

        self._stack.enter_context(mock.patch.object(flyer.tempfile, 'mkstemp', mkstemp))
        self._stack.enter_context(mock.patch.object(flyer.os, 'fdopen', fdopen))
        self._stack.enter_context(mock.patch.object(flyer.os, 'close', close))
        return self

    def __exit__(self, *exc):
        self._stack.close()
        for descriptor, _ in self.allocated:  # a regression must not leak into the test process
            if is_open(descriptor):
                self.leaked = True
                os.close(descriptor)
        return False

    leaked = False


class FailingStream:
    """Real stream wrapper whose chosen operation fails after the real stream is really closed."""

    def __init__(self, stream, write=False, exit_=False):
        self.stream, self.fail_write, self.fail_exit = stream, write, exit_

    def __enter__(self):
        return self

    def write(self, data):
        if self.fail_write:
            self.stream.write(data[:4])
            raise OSError(errno.ENOSPC, 'synthetic partial write')
        return self.stream.write(data)

    def __exit__(self, *exc):
        self.stream.close()
        if self.fail_exit:
            raise OSError(errno.EIO, 'synthetic stream exit failure')
        return False


class EventLogoLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.fx = base.EventFlyerTests('test_fields_in_both_twins')
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.fx.logo()
        self.folder = self.fx.folder
        self.logo_path = self.folder / 'event-ev1-logo.svg'

    def staging(self):
        return sorted(p.name for p in self.folder.glob('.event-ev1-logo.*')) if self.folder.exists() else []

    def finals(self):
        return sorted(p.name for p in self.folder.iterdir() if not p.name.startswith('.')) if self.folder.exists() else []

    def assert_closed_and_clean(self, seams):
        self.assertFalse(seams.leaked, 'descriptor left open')
        self.assertTrue(seams.allocated)
        for descriptor, name in seams.allocated:
            self.assertFalse(is_open(descriptor), descriptor)
            self.assertFalse(os.path.lexists(name), name)
        self.assertEqual(self.staging(), [])

    def test_event_logo_fdopen_oserror_closes_real_raw_descriptor(self):
        sentinel_fd = os.open(os.devnull, os.O_RDONLY)
        self.addCleanup(os.close, sentinel_fd)
        with Seams(fail=CONSTRUCTOR_FAILURE) as seams:
            with self.assertRaises(OSError) as caught:
                self.fx.make()
        self.assertIs(caught.exception, CONSTRUCTOR_FAILURE)
        self.assert_closed_and_clean(seams)
        self.assertEqual(len(seams.allocated), 1)
        self.assertEqual(seams.raw_closes, [seams.allocated[0][0]])
        self.assertEqual(self.finals(), [])
        self.assertTrue(is_open(sentinel_fd))

    def test_repeated_event_logo_constructor_failures_retain_no_descriptors(self):
        with Seams(fail=CONSTRUCTOR_FAILURE) as seams:
            for _ in range(3):
                with self.assertRaises(OSError):
                    self.fx.make(replace=True)
            states = [is_open(descriptor) for descriptor, _ in seams.allocated]
        self.assertEqual(len(seams.allocated), 3)
        self.assertEqual(states, [False, False, False])
        self.assertFalse(seams.leaked)
        self.assertEqual(self.staging(), [])
        report = self.fx.make(replace=True)
        self.assertEqual(report['written'], ['kit/flyer/event-ev1.html', 'kit/flyer/event-ev1.txt', 'kit/flyer/event-ev1-logo.svg'])
        self.assertEqual(self.logo_path.read_bytes(), SVG)

    def test_event_logo_constructor_baseexception_closes_raw_descriptor(self):
        failure = Sentinel('synthetic')
        with Seams(fail=failure) as seams:
            with self.assertRaises(Sentinel) as caught:
                self.fx.make()
        self.assertIs(caught.exception, failure)
        self.assert_closed_and_clean(seams)
        self.assertEqual(self.finals(), [])

    def test_same_suffix_replacement_constructor_failure_preserves_outputs(self):
        self.fx.make()
        names = ['event-ev1.html', 'event-ev1.txt', 'event-ev1-logo.svg']
        before = {n: ((self.folder / n).read_bytes(), (self.folder / n).lstat().st_mode, (self.folder / n).lstat().st_mtime_ns)
                  for n in names}
        unrelated = self.folder / '.unrelated.tmp'
        unrelated.write_bytes(b'keep')
        with Seams(fail=CONSTRUCTOR_FAILURE) as seams, \
                mock.patch.object(flyer, '_write_text', side_effect=AssertionError('twin written')), \
                mock.patch.object(flyer.os, 'replace', side_effect=AssertionError('logo published')):
            with self.assertRaises(OSError):
                self.fx.make(replace=True)
        self.assertFalse(seams.leaked)
        after = {n: ((self.folder / n).read_bytes(), (self.folder / n).lstat().st_mode, (self.folder / n).lstat().st_mtime_ns)
                 for n in names}
        self.assertEqual(after, before)
        self.assertEqual(unrelated.read_bytes(), b'keep')
        report = self.fx.make(replace=True)
        self.assertEqual(report['event'], 'ev1')
        self.assertEqual(stat.S_IMODE(self.logo_path.stat().st_mode), 0o600)

    def test_stream_ownership_transfers_exactly_once(self):
        self.fx.make()
        before = self.logo_path.read_bytes()
        sentinel_fd = os.open(os.devnull, os.O_RDONLY)
        self.addCleanup(os.close, sentinel_fd)
        cases = {
            'write': dict(wrap=lambda s: FailingStream(s, write=True)),
            'exit': dict(wrap=lambda s: FailingStream(s, exit_=True)),
        }
        for label, kw in cases.items():
            with self.subTest(label), Seams(**kw) as seams:
                with self.assertRaises(OSError):
                    self.fx.make(replace=True)
                self.assert_closed_and_clean(seams)
                self.assertEqual(seams.raw_closes, [])  # no second close after the stream owns the descriptor
        real_read = flyer.Path.read_bytes
        with self.subTest('read'), Seams(wrap=lambda s: s) as seams:
            armed = []
            real_fdopen = flyer.os.fdopen

            def tripwire_fdopen(*a, **k):
                stream = real_fdopen(*a, **k)
                armed.append(True)
                return stream

            def read_bytes(path):
                if armed:
                    raise OSError(errno.EIO, 'synthetic source read failure')
                return real_read(path)

            with mock.patch.object(flyer.os, 'fdopen', tripwire_fdopen), mock.patch.object(flyer.Path, 'read_bytes', read_bytes):
                with self.assertRaises(OSError):
                    self.fx.make(replace=True)
            self.assert_closed_and_clean(seams)
            self.assertEqual(seams.raw_closes, [])
        with self.subTest('replace'), Seams() as seams:
            with mock.patch.object(flyer.os, 'replace', side_effect=OSError(errno.EXDEV, 'synthetic replace failure')):
                with self.assertRaises(OSError):
                    self.fx.make(replace=True)
            self.assert_closed_and_clean(seams)
            self.assertEqual(seams.raw_closes, [])
        self.assertEqual(self.logo_path.read_bytes(), before)
        self.assertTrue(is_open(sentinel_fd))

    def test_allocation_failure_has_no_owned_descriptor_or_temporary(self):
        sentinel_fd = os.open(os.devnull, os.O_RDONLY)
        self.addCleanup(os.close, sentinel_fd)
        closes = []
        real_close = os.close
        with mock.patch.object(flyer.tempfile, 'mkstemp', side_effect=OSError(errno.ENOSPC, 'synthetic allocation failure')), \
                mock.patch.object(flyer.os, 'close', lambda d: (closes.append(d), real_close(d))[1]), \
                mock.patch.object(flyer, '_write_text', side_effect=AssertionError('twin written')):
            with self.assertRaises(OSError):
                self.fx.make()
        self.assertEqual(closes, [])
        self.assertTrue(is_open(sentinel_fd))
        self.assertEqual(self.finals(), [])
        self.assertEqual(self.staging(), [])
        self.assertEqual(self.fx.make()['event'], 'ev1')

    def test_success_no_brand_and_campaign_controls_keep_contracts(self):
        with Seams() as seams:
            report = self.fx.make()
        self.assertEqual(report, {'written': ['kit/flyer/event-ev1.html', 'kit/flyer/event-ev1.txt', 'kit/flyer/event-ev1-logo.svg'],
                                  'event': 'ev1'})
        logos = [a for a in seams.allocated if os.path.basename(a[1]).startswith(".event-ev1-logo.")]
        self.assertEqual(len(logos), 1)
        self.assertFalse(is_open(logos[0][0]))
        self.assertEqual(seams.raw_closes, [])
        self.assertEqual(self.logo_path.read_bytes(), SVG)
        self.assertEqual(stat.S_IMODE(self.logo_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.folder.stat().st_mode), 0o700)
        with Seams() as seams:  # replacement run
            self.fx.make(replace=True)
        self.assertEqual(self.logo_path.read_bytes(), SVG)
        self.fx.site({'brand': {}})
        with Seams() as seams:
            report = self.fx.make(replace=True)
        self.assertEqual([a for a in seams.allocated if os.path.basename(a[1]).startswith(".event-ev1-logo.")], [])
        self.assertEqual(report['written'], ['kit/flyer/event-ev1.html', 'kit/flyer/event-ev1.txt'])
        self.assertFalse(self.logo_path.exists())

    def test_real_event_cli_remains_private_and_dispatches_unchanged(self):
        self.fx.events([base.event(1, starts_at='2999-01-01T10:00:00Z', ends_at='2999-01-01T11:00:00Z')])
        for branded in (True, False):
            if not branded:
                self.fx.site({'brand': {}})
            result = self.fx.cli('--event', 'ev1', '--replace')
            self.assertEqual(result.returncode, 0, result.stderr)
            lines = result.stdout.splitlines()
            self.assertEqual(lines[1], 'Private file; nothing was printed, sent or published.')
            self.assertEqual(('kit/flyer/event-ev1-logo.svg' in lines[0]), branded)
        self.fx.logo()
        out, err = io.StringIO(), io.StringIO()
        argv = ['campaign_tool', 'flyer', '--directory', str(self.fx.root), '--event', 'ev1', '--replace']
        with Seams(fail=CONSTRUCTOR_FAILURE) as seams, mock.patch.object(sys, 'argv', argv), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as caught:
                cli.main()
        self.assertEqual(caught.exception.code, 1)
        self.assertEqual(out.getvalue(), '')
        self.assertNotIn('Traceback', err.getvalue())
        self.assert_closed_and_clean(seams)


if __name__ == '__main__':
    unittest.main()
