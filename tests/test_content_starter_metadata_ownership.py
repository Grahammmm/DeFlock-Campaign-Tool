"""RF0600-3: add_starter owns its raw staging descriptor through the first identity lookup (synthetic files only)."""
import contextlib
import errno
import io
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import cli, content_init as subject


class Abort(BaseException):
    pass


class Unexpected(Exception):
    pass


@contextlib.contextmanager
def instrumented(fstat_fail=None, fdopen_fail=None, secondary=False, open_fail=False, on_fstat=None, stream=None,
                 sweep=True):
    """Record real staging acquisitions and raw closes; inject faults only for the staging output descriptor."""
    real_open, real_fstat, real_close, real_fdopen = os.open, os.fstat, os.close, os.fdopen
    rec = types.SimpleNamespace(outputs=[], opened=[], closes=[], live=set(), faulted=set())

    def opening(path, *args, **kwargs):
        staging = isinstance(path, str) and ".new-" in path
        if staging and open_fail:
            raise OSError(errno.EIO, "synthetic open failure")
        fd = real_open(path, *args, **kwargs)
        rec.opened.append(fd)
        if staging:
            rec.outputs.append(fd)
            rec.live.add(fd)
        return fd

    def statting(fd, *args, **kwargs):
        if fstat_fail is not None and fd in rec.live and fd not in rec.faulted:
            rec.faulted.add(fd)
            if on_fstat:
                on_fstat(fd)
            raise fstat_fail
        return real_fstat(fd, *args, **kwargs)

    def closing(fd):
        if fd in rec.live:
            rec.live.discard(fd)
            rec.closes.append(fd)
            real_close(fd)
            if secondary:
                raise OSError(errno.EIO, "synthetic secondary close failure")
            return
        real_close(fd)

    def constructing(fd, *args, **kwargs):
        if fdopen_fail is not None and fd in rec.live:
            raise fdopen_fail
        built = real_fdopen(fd, *args, **kwargs)
        return stream(built) if stream and fd in rec.live else built

    try:
        with mock.patch.object(subject.os, "open", side_effect=opening), \
                mock.patch.object(subject.os, "fstat", side_effect=statting), \
                mock.patch.object(subject.os, "close", side_effect=closing), \
                mock.patch.object(subject.os, "fdopen", side_effect=constructing):
            yield rec
    finally:
        for fd in rec.outputs if sweep else ():  # RED runs must not leak the deliberately exposed descriptors
            try:
                real_close(fd)
            except OSError:
                pass


class StarterMetadataOwnership(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-starter-metadata-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "campaign"
        with mock.patch.object(sys, "argv", ["campaign-tool", "init", "--directory", str(self.root), "--name",
                "Synthetic Cedar", "--county", "Cedar", "--state", "CA"]), contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(cli.main())
        subject.init_content(self.root)
        self.content = self.root / "content"
        self.reference = {}
        for name, parts in subject.STARTERS:
            path = self.content.joinpath(*parts)
            self.reference[name] = path.read_bytes()
            path.unlink()
        self.unrelated = os.open(self.base / "unrelated", os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, self.unrelated)

    def target(self, name):
        return self.content.joinpath(*dict(subject.STARTERS)[name])

    def message(self, name):
        return "content/" + "/".join(dict(subject.STARTERS)[name]) + " could not be written; nothing was written"

    def assert_closed(self, fd):
        with self.assertRaises(OSError) as caught:
            os.fstat(fd)
        self.assertEqual(caught.exception.errno, errno.EBADF)

    def test_initial_staging_fstat_failure_closes_all_three_starter_fds(self):
        for name, _ in subject.STARTERS:
            for pages in ("existing", "absent"):
                with self.subTest(name=name, pages=pages):
                    shutil.rmtree(self.content / "pages", ignore_errors=True)  # drop earlier kept staging files
                    if pages == "existing":
                        (self.content / "pages").mkdir()
                    real_discard, at_discard = subject._discard, []

                    def discard(temp, created, fds):
                        at_discard.extend(rec.outputs)
                        for fd in rec.outputs:
                            self.assert_closed(fd)  # closed before the outer cleanup runs
                        self.assertIsNone(temp)
                        return real_discard(temp, created, fds)

                    with instrumented(fstat_fail=OSError(errno.EIO, "synthetic fstat failure")) as rec, \
                            mock.patch.object(subject, "_discard", side_effect=discard):
                        with self.assertRaises(subject.StarterError) as caught:
                            subject.add_starter(self.root, name)
                    self.assertEqual(str(caught.exception), self.message(name))
                    self.assertEqual(len(rec.outputs), 1)
                    self.assertEqual(at_discard, rec.outputs)
                    self.assertEqual(rec.closes, rec.outputs)
                    self.assertFalse(self.target(name).exists())
                    os.fstat(self.unrelated)

    def test_repeated_metadata_refusals_do_not_accumulate_live_descriptors(self):
        for name, _ in subject.STARTERS:
            acquired = []
            for attempt in range(3):
                with self.subTest(name=name, attempt=attempt):
                    with instrumented(fstat_fail=OSError(errno.EIO, "synthetic fstat failure")) as rec:
                        with self.assertRaises(subject.StarterError):
                            subject.add_starter(self.root, name)
                        for fd in rec.outputs:
                            self.assert_closed(fd)
                    self.assertEqual(len(rec.outputs), 1)
                    acquired += rec.outputs
            self.assertEqual(len(acquired), 3)  # unidentifiable staging files are kept by design; descriptors are not

    def test_real_inprocess_cli_closes_before_exit_two(self):
        for name, _ in subject.STARTERS:
            with self.subTest(name=name):
                stdout, stderr = io.StringIO(), io.StringIO()
                with instrumented(fstat_fail=OSError(errno.EIO, "synthetic fstat failure")) as rec, \
                        mock.patch.object(sys, "argv", ["campaign-tool", "content", "add-starter", name,
                                                         "--directory", str(self.root)]), \
                        contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    with self.assertRaises(SystemExit) as exited:
                        cli.main()
                    for fd in rec.outputs:
                        self.assert_closed(fd)  # already closed when the SystemExit is observed
                self.assertEqual(len(rec.outputs), 1)
                self.assertEqual(exited.exception.code, 2)
                self.assertEqual(stdout.getvalue(), "")
                self.assertEqual(stderr.getvalue(), "Stopped: " + self.message(name) + "\n")

    def test_metadata_primary_survives_secondary_close_error(self):
        for error in (OSError(errno.EIO, "primary"), Unexpected("primary"), Abort("primary")):
            for secondary in (False, True):
                with self.subTest(error=type(error).__name__, secondary=secondary):
                    expected = subject.StarterError if isinstance(error, OSError) else type(error)
                    with instrumented(fstat_fail=error, secondary=secondary) as rec:
                        with self.assertRaises(expected) as caught:
                            subject.add_starter(self.root, "faq")
                    if not isinstance(error, OSError):
                        self.assertIs(caught.exception, error)
                    else:
                        self.assertEqual(str(caught.exception), self.message("faq"))
                    self.assertEqual(rec.closes, rec.outputs)  # exactly one close attempt, no retry
                    self.assertEqual(len(rec.outputs), 1)
                    self.assert_closed(rec.outputs[0])

    def test_constructor_failure_after_successful_identity_still_cleans_owned_temp(self):
        for name, _ in subject.STARTERS:
            with self.subTest(name=name):
                real_discard = subject._discard

                def discard(temp, created, fds):
                    self.assertIsNotNone(temp)  # the real identity was recorded
                    self.assert_closed(rec.outputs[0])
                    return real_discard(temp, created, fds)

                with instrumented(fdopen_fail=OSError(errno.EIO, "synthetic constructor failure")) as rec, \
                        mock.patch.object(subject, "_discard", side_effect=discard):
                    with self.assertRaises(subject.StarterError):
                        subject.add_starter(self.root, name)
                self.assertEqual(rec.closes, rec.outputs)
                self.assertEqual(list(self.content.rglob("*.new-*")), [])
                self.assertFalse(self.target(name).exists())

    def test_successful_stream_transfer_never_closes_reused_number(self):
        sentinels = []

        class Failing:
            def __init__(inner, stream):
                inner.stream = stream

            def __enter__(inner):
                return inner

            def __exit__(inner, *exc):
                number = inner.stream.fileno()
                inner.stream.close()
                fresh = os.open(self.base / "unrelated", os.O_RDONLY)
                if fresh != number:
                    os.dup2(fresh, number)
                    os.close(fresh)
                sentinels.append(number)

            def __getattr__(inner, name):
                if name == "flush":
                    def fail(*args, **kwargs):
                        raise OSError(errno.EIO, "post-transfer I/O failure")
                    return fail
                return getattr(inner.stream, name)

        try:
            with instrumented(stream=Failing, sweep=False) as rec:
                with self.assertRaises(subject.StarterError):
                    subject.add_starter(self.root, "faq")
            self.assertEqual(rec.closes, [])  # no raw close of the released, reused number
            self.assertEqual(sentinels, rec.outputs)
            os.fstat(sentinels[0])
            self.assertEqual(list(self.content.rglob("*.new-*")), [])
        finally:
            for fd in sentinels:
                os.close(fd)

    def test_unidentified_temp_is_not_deleted_by_name_or_late_path_metadata(self):
        def substitute(fd):
            path = Path(os.readlink(f"/proc/self/fd/{fd}"))
            self.moved = path.with_name(path.name + ".orig")
            os.rename(path, self.moved)
            path.write_bytes(b"foreign bytes")
            self.foreign = path

        with instrumented(fstat_fail=OSError(errno.EIO, "synthetic fstat failure"), on_fstat=substitute) as rec:
            with self.assertRaises(subject.StarterError):
                subject.add_starter(self.root, "faq")
        self.assertEqual(self.foreign.read_bytes(), b"foreign bytes")
        self.assertTrue(self.moved.exists())
        self.assertFalse(self.target("faq").exists())
        self.assert_closed(rec.outputs[0])

    def test_open_failure_closes_no_unacquired_descriptor(self):
        with instrumented(open_fail=True) as rec:
            with self.assertRaises(subject.StarterError):
                subject.add_starter(self.root, "faq")
        self.assertEqual(rec.outputs, [])
        self.assertTrue(set(rec.closes) <= set(rec.opened))  # only handles this run acquired
        os.fstat(self.unrelated)

    def test_descriptor_zero_is_closed_in_isolated_child(self):
        result = subprocess.run([sys.executable, "-B", "-m", "tests.test_content_starter_metadata_ownership",
                                 "--fd-zero", str(self.root)], capture_output=True, text=True, timeout=60,
                                stdin=subprocess.DEVNULL)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "fd-zero closed")

    def test_healthy_starters_keep_legacy_bytes_modes_and_results(self):
        for name, parts in subject.STARTERS:
            with self.subTest(name=name):
                result = subject.add_starter(self.root, name)
                path = self.target(name)
                self.assertEqual(path.read_bytes(), self.reference[name])
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(result, {"written": "content/" + "/".join(parts), "created_pages": False,
                                          "placeholders": self.reference[name].decode("utf-8").count(subject.PLACEHOLDER)})
        self.assertEqual(list(self.content.rglob("*.new-*")), [])


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--fd-zero":
        os.close(0)
        real_open = os.open
        primary = OSError(errno.EIO, "synthetic fstat failure")
        seen = {}

        def keep_zero_free(path, *args, **kwargs):
            fd = real_open(path, *args, **kwargs)
            if fd == 0 and not (isinstance(path, str) and ".new-" in path):
                moved = os.dup(0)
                real_close = os.close
                real_close(0)
                return moved
            if isinstance(path, str) and ".new-" in path:
                seen["staging"] = fd
            return fd

        real_fstat = os.fstat

        def failing(fd, *args, **kwargs):
            if fd == seen.get("staging") and "failed" not in seen:
                seen["failed"] = True
                raise primary
            return real_fstat(fd, *args, **kwargs)

        with mock.patch.object(subject.os, "open", side_effect=keep_zero_free), \
                mock.patch.object(subject.os, "fstat", side_effect=failing):
            try:
                subject.add_starter(Path(sys.argv[2]), "faq")
            except subject.StarterError:
                pass
        assert seen.get("staging") == 0, seen
        try:
            real_fstat(0)
        except OSError as exc:
            assert exc.errno == errno.EBADF
        else:
            raise AssertionError("fd zero remained open")
        print("fd-zero closed")
    else:
        unittest.main()
