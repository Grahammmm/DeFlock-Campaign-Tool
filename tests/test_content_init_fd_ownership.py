"""Real synthetic descriptor ownership at content-init's four stream handoffs."""
import contextlib
import errno
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import cli, content_init as subject


class ConstructionAbort(BaseException):
    pass


class UnexpectedConstructionError(Exception):
    pass


def snapshot(root):
    return {str(p.relative_to(root)): (p.stat().st_mode, p.stat().st_mtime_ns, p.read_bytes())
            for p in root.rglob('*') if p.is_file()}


class DescriptorOwnership(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='synthetic-content-fds-')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.source, self.dest = self.base / 'source', self.base / 'dest'
        for root in (self.source, self.dest):
            with mock.patch.object(sys, 'argv', ['campaign-tool', 'init', '--directory', str(root),
                    '--name', 'Synthetic Cedar', '--county', 'Cedar', '--state', 'CA']), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertIsNone(cli.main())
        subject.init_content(self.source)
        (self.source / 'content' / 'labels.json').write_text(json.dumps(
            {'schema_version': 1, 'labels': {'nav.home': 'Synthetic home'}}))
        self.unrelated = os.open(self.base / 'unrelated', os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, self.unrelated)

    def assert_closed(self, fd):
        with self.assertRaises(OSError) as caught:
            os.fstat(fd)
        self.assertEqual(caught.exception.errno, errno.EBADF)

    def assert_no_output(self):
        self.assertFalse((self.dest / 'content').exists())
        self.assertEqual(list(self.dest.glob('.content.new-*')), [])

    def exercise_fault(self, action, select, expected, primary=None, secondary=False, on_fault=None):
        """Inject before real fdopen; record real opens, closes and failure-time identity."""
        primary = primary if primary is not None else OSError(errno.EIO, 'synthetic constructor failure')
        real_open, real_close, real_fdopen = os.open, os.close, os.fdopen
        acquired, closed, failed, identity = [], [], [], []

        def opening(*args, **kwargs):
            fd = real_open(*args, **kwargs)
            acquired.append(fd)
            return fd

        def closing(fd):
            info = os.fstat(fd)
            current = (fd, info.st_dev, info.st_ino)
            closed.append(current)
            real_close(fd)
            if secondary and identity and current == identity[0]:
                raise OSError(errno.EIO, 'synthetic secondary close failure')

        def constructing(fd, mode='r', *args, **kwargs):
            path = Path(os.readlink(f'/proc/self/fd/{fd}'))
            if select(path, mode):
                failed.append(fd)
                info = os.fstat(fd)
                identity.append((fd, info.st_dev, info.st_ino))
                if on_fault:
                    on_fault(path)
                raise primary
            return real_fdopen(fd, mode, *args, **kwargs)

        try:
            with mock.patch.object(subject.os, 'open', side_effect=opening), \
                    mock.patch.object(subject.os, 'close', side_effect=closing), \
                    mock.patch.object(subject.os, 'fdopen', side_effect=constructing):
                with self.assertRaises(expected) as caught:
                    action()
            self.assertEqual(len(failed), 1)
            self.assert_closed(failed[0])
            self.assertEqual(closed.count(identity[0]), 1)
            if expected is type(primary):
                self.assertIs(caught.exception, primary)
            for fd in acquired:
                self.assert_closed(fd)
            os.fstat(self.unrelated)
            return str(caught.exception)
        finally:
            # RED runs must not leak the intentionally exposed descriptors.
            for fd in set(acquired):
                try:
                    real_close(fd)
                except OSError:
                    pass

    def test_fresh_first_and_later_write_failure_roll_back(self):
        before = snapshot(self.dest)
        for name in ('site.json', 'pages/background.json', 'sources.json'):
            with self.subTest(name=name):
                self.exercise_fault(lambda: subject.init_content(self.dest),
                    lambda p, m: m == 'w' and str(p).endswith('/' + name), OSError)
                self.assert_no_output()
                self.assertEqual(snapshot(self.dest), before)

    def test_status_and_all_add_names_site_failure_are_private_and_read_only(self):
        before = snapshot(self.source)
        actions = [lambda: subject.starter_status(self.source)] + [
            lambda name=name: subject.add_starter(self.source, name) for name, _ in subject.STARTERS]
        for repeat in range(2):
            for index, action in enumerate(actions):
                with self.subTest(repeat=repeat, action=index), \
                        mock.patch.object(subject, 'starter_text', side_effect=AssertionError('must not generate')):
                    message = self.exercise_fault(action,
                        lambda p, m: m == 'rb' and p == self.source / 'content/site.json', subject.StarterError)
                    self.assertEqual(message, 'content/site.json is missing or not valid (run content-check); nothing was written')
                    self.assertEqual(snapshot(self.source), before)

    def test_temp_failure_closes_before_discard_and_preserves_foreign_data(self):
        for name, pages in (('faq', 'existing'), ('help-map', 'absent'),
                            ('background', 'existing'), ('help-map', 'foreign')):
            with self.subTest(name=name, pages=pages):
                content = self.source / 'content'
                for starter, parts in subject.STARTERS:
                    (content.joinpath(*parts)).unlink(missing_ok=True)
                if pages != 'existing' and (content / 'pages').exists():
                    (content / 'pages').rmdir()
                if pages == 'existing':
                    (content / 'pages').mkdir(exist_ok=True)
                before = snapshot(self.source)
                real_discard = subject._discard

                def discard(temp, created, fds):
                    self.assert_closed(captured[0])
                    return real_discard(temp, created, fds)

                captured = []
                def foreign(path):
                    captured.append(int(next(p.name for p in Path('/proc/self/fd').iterdir()
                        if p.is_symlink() and os.readlink(p) == str(path))))
                    if pages == 'foreign':
                        (content / 'pages' / 'foreign.txt').write_bytes(b'keep foreign bytes')

                with mock.patch.object(subject, '_discard', side_effect=discard):
                    self.exercise_fault(lambda: subject.add_starter(self.source, name),
                        lambda p, m: m == 'wb', subject.StarterError, on_fault=foreign)
                self.assertEqual(list(content.rglob('*.new-*')), [])
                self.assertFalse(content.joinpath(*dict(subject.STARTERS)[name]).exists())
                if pages == 'absent':
                    self.assertFalse((content / 'pages').exists())
                else:
                    self.assertTrue((content / 'pages').is_dir())
                if pages == 'foreign':
                    self.assertEqual((content / 'pages/foreign.txt').read_bytes(), b'keep foreign bytes')
                    (content / 'pages/foreign.txt').unlink()
                self.assertEqual(snapshot(self.source), before)

    def test_copy_each_input_and_output_constructor_failure(self):
        before, dest_before = snapshot(self.source), snapshot(self.dest)
        for entry in ('campaign.json', 'content/site.json', 'content/labels.json',
                      'content/pages/background.json', 'output'):
            for wrapper in (False, True):
                with self.subTest(entry=entry, wrapper=wrapper):
                    def select(path, mode):
                        return (mode == 'w' and self.dest in path.parents) if entry == 'output' else (
                            mode == 'rb' and path == self.source / entry)
                    action = (lambda: subject.init_content(self.dest, source_root=self.source)) if wrapper else (
                        lambda: subject.init_from(self.dest, self.source))
                    self.exercise_fault(action, select, OSError)
                    self.assert_no_output()
                    self.assertEqual(snapshot(self.source), before)
                    self.assertEqual(snapshot(self.dest), dest_before)

    def test_primary_exception_and_secondary_close_for_all_handoffs(self):
        (self.source / 'content/faq.json').unlink()
        cases = [
            (lambda: subject.init_content(self.dest), lambda p, m: m == 'w', False),
            (lambda: subject.starter_status(self.source), lambda p, m: m == 'rb', True),
            (lambda: subject.add_starter(self.source, 'faq'), lambda p, m: m == 'wb', True),
            (lambda: subject.init_from(self.dest, self.source), lambda p, m: m == 'rb', False),
        ]
        for index, (action, select, converts) in enumerate(cases):
            for error in (OSError(errno.EIO, 'primary'), UnexpectedConstructionError('primary'), ConstructionAbort('primary')):
                for secondary in (False, True):
                    with self.subTest(site=index, error=type(error).__name__, secondary=secondary):
                        expected = subject.StarterError if converts and isinstance(error, OSError) else type(error)
                        self.exercise_fault(action, select, expected, error, secondary)
                        self.assert_no_output()

    def test_open_failure_does_not_close_unacquired_descriptor(self):
        with mock.patch.object(subject.os, 'open', side_effect=OSError('open failed')), \
                mock.patch.object(subject.os, 'close') as close:
            with self.assertRaisesRegex(OSError, 'open failed'):
                subject._write(self.dest / 'unused', 'text')
        close.assert_not_called()
        os.fstat(self.unrelated)

    def test_inprocess_cli_keeps_exit_codes_and_fixed_starter_messages(self):
        (self.source / 'content/faq.json').unlink()
        for args, select, code, message in (
            (['add-starter', '--list'], lambda p, m: m == 'rb', 2,
             'content/site.json is missing or not valid (run content-check); nothing was written'),
            (['add-starter', 'faq'], lambda p, m: m == 'wb', 2,
             'content/faq.json could not be written; nothing was written'),
            (['init'], lambda p, m: m == 'w', 1, 'synthetic constructor failure'),
            (['init', '--from', str(self.source)], lambda p, m: m == 'rb', 1, 'synthetic constructor failure'),
        ):
            with self.subTest(args=args):
                root = self.source if args[0] == 'add-starter' else self.dest
                stdout, stderr, failed = io.StringIO(), io.StringIO(), []
                real_fdopen = os.fdopen
                error = OSError(errno.EIO, 'synthetic constructor failure')

                def constructing(fd, mode='r', *a, **kw):
                    path = Path(os.readlink(f'/proc/self/fd/{fd}'))
                    if select(path, mode):
                        failed.append(fd)
                        raise error
                    return real_fdopen(fd, mode, *a, **kw)

                try:
                    with mock.patch.object(sys, 'argv', ['campaign-tool', 'content', *args, '--directory', str(root)]), \
                            mock.patch.object(subject.os, 'fdopen', side_effect=constructing), \
                            contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                        with self.assertRaises(SystemExit) as exited:
                            cli.main()
                    self.assertEqual(exited.exception.code, code)
                    self.assertEqual(stdout.getvalue(), '')
                    self.assertIn(message, stderr.getvalue())
                    self.assertNotIn('Traceback', stderr.getvalue())
                    self.assertEqual(len(failed), 1)
                    self.assert_closed(failed[0])
                    self.assert_no_output()
                finally:
                    for fd in failed:
                        try:
                            os.close(fd)
                        except OSError:
                            pass

    def test_successful_transfer_io_errors_never_raw_close_reused_fd(self):
        (self.source / 'content/faq.json').unlink()
        cases = [
            (lambda: subject.init_content(self.dest), 'w', 'write', OSError),
            (lambda: subject.starter_status(self.source), 'rb', 'read', subject.StarterError),
            (lambda: subject.add_starter(self.source, 'faq'), 'wb', 'flush', subject.StarterError),
            (lambda: subject.init_from(self.dest, self.source), 'rb', 'read', OSError),
        ]
        for action, selected_mode, operation, expected in cases:
            with self.subTest(mode=selected_mode, operation=operation):
                real_fdopen, streams, sentinels = os.fdopen, [], []

                class FailingStream:
                    def __init__(inner, stream):
                        inner.stream = stream
                    def __enter__(inner):
                        return inner
                    def __exit__(inner, *exc):
                        number = inner.stream.fileno()
                        inner.stream.close()
                        fresh = os.open(self.base / 'unrelated', os.O_RDONLY)
                        if fresh != number:
                            os.dup2(fresh, number)
                            os.close(fresh)
                        sentinels.append(number)
                    def __getattr__(inner, name):
                        if name == operation:
                            def fail(*a, **kw):
                                raise OSError('post-transfer I/O failure')
                            return fail
                        return getattr(inner.stream, name)

                def constructing(fd, mode='r', *a, **kw):
                    stream = real_fdopen(fd, mode, *a, **kw)
                    if mode == selected_mode:
                        streams.append(stream)
                        return FailingStream(stream)
                    return stream

                try:
                    with mock.patch.object(subject.os, 'fdopen', side_effect=constructing):
                        with self.assertRaises(expected):
                            action()
                    self.assertEqual(len(streams), 1)
                    self.assertTrue(streams[0].closed)
                    for fd in sentinels:
                        os.fstat(fd)
                    self.assert_no_output()
                finally:
                    for fd in sentinels:
                        os.close(fd)

    def test_success_bytes_for_ca_or_starters_and_public_branding_copy(self):
        resources = Path(subject.__file__).parent / '_resources/examples'
        for state in ('CA', 'OR'):
            with self.subTest(state=state):
                config = json.loads((self.dest / 'campaign.json').read_text())
                config['state'] = state
                (self.dest / 'campaign.json').write_text(json.dumps(config))
                result = subject.init_content(self.dest)
                content = self.dest / 'content'
                expected_site = {
                    'schema_version': 1,
                    'tagline': '[EDIT ME: one-line campaign tagline]',
                    'mission_md': '[EDIT ME: two or three sentences on what the campaign asks for and why]',
                    'about_md': '## Who we are\n\n[EDIT ME: who runs Synthetic Cedar in Cedar, ' + state + ']\n\n'
                                '## How findings are reviewed\n\n[EDIT ME: who checks each finding against its cited records before it is published]',
                    'contact_email': None, 'signup': {'mode': 'none'}, 'language': 'en',
                }
                expected = {'site.json': json.dumps(expected_site, indent=2, ensure_ascii=False) + '\n',
                            'sources.json': '[]\n'}
                for name, parts in subject.STARTERS:
                    relative = Path(*parts)
                    document = json.loads((resources / relative).read_text())
                    if name == 'faq' and state == 'OR':
                        for entry in document['entries']:
                            if entry.pop('rule_id', None) is not None:
                                entry['answer_md'] = ('[EDIT ME: verify for this campaign] Check the applicable law package and official guidance for response timing. '
                                    'A reminder is not a legal determination or a promise that records will be produced by that date.')
                    expected[str(relative)] = json.dumps(document, indent=2, ensure_ascii=False) + '\n'
                for relative, text in expected.items():
                    self.assertEqual((content / relative).read_bytes(), text.encode('utf-8'))
                    self.assertEqual((content / relative).stat().st_mode & 0o777, 0o600)
                self.assertEqual(result['placeholders'], sum(t.count('[EDIT ME: ') for t in expected.values()))
                for name, parts in subject.STARTERS:
                    target = content.joinpath(*parts)
                    target.unlink()
                    added = subject.add_starter(self.dest, name)
                    self.assertEqual(target.read_bytes(), expected[str(Path(*parts))].encode('utf-8'))
                    self.assertEqual(added, {'written': 'content/' + str(Path(*parts)), 'created_pages': False,
                        'placeholders': expected[str(Path(*parts))].count('[EDIT ME: ')})
                shutil.rmtree(content)
        before = snapshot(self.source)
        copied = subject.init_from(self.dest, self.source)
        self.assertEqual(snapshot(self.source), before)
        self.assertEqual(set(copied['copied']), {'content/site.json', 'content/labels.json',
            'content/pages/background.json', 'content/pages/help-map.json'})
        for relative in ('labels.json', 'pages/background.json', 'pages/help-map.json'):
            self.assertEqual((self.dest / 'content' / relative).read_bytes(),
                             (json.dumps(json.loads((self.source / 'content' / relative).read_text()),
                                         indent=2, ensure_ascii=False) + '\n').encode('utf-8'))

    def test_fd_zero_is_closed_in_isolated_child(self):
        result = subprocess.run([sys.executable, '-B', '-m', 'tests.test_content_init_fd_ownership',
                                 '--fd-zero', str(self.base / 'zero')], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'fd-zero closed')


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--fd-zero':
        os.close(0)
        primary = ConstructionAbort('zero')
        with mock.patch.object(subject.os, 'fdopen', side_effect=primary) as constructor:
            try:
                subject._write(Path(sys.argv[2]), 'synthetic')
            except ConstructionAbort as caught:
                assert caught is primary
        assert constructor.call_args.args[0] == 0
        try:
            os.fstat(0)
        except OSError as exc:
            assert exc.errno == errno.EBADF
        else:
            raise AssertionError('fd zero remained open')
        print('fd-zero closed')
    else:
        unittest.main()
