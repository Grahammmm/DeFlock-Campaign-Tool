"""Real descriptor ownership at the officials reader/writer stream handoff."""
import contextlib
import errno
import io
import os
import stat
import subprocess
import sys
import unittest
from datetime import date
from unittest import mock

from campaign_tool import cli, officials_cmd as oc, statements, today
from tests import test_officials_commands as fixtures


class Cancelled(BaseException):
    pass


class OfficialsDescriptorTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.OfficialsCommandTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root

    def assert_closed(self, fd):
        with self.assertRaises(OSError) as caught:
            os.fstat(fd)
        self.assertEqual(caught.exception.errno, errno.EBADF)

    @contextlib.contextmanager
    def construction_fault(self, target, error=None, close_error=False, open_error=False):
        real_open, real_fdopen, real_close = os.open, os.fdopen, os.close
        captured, directories, close_attempts = [], [], []
        error = error if error is not None else OSError("private constructor payload")

        def opening(path, flags, *args, **kwargs):
            selected = (str(path).startswith(".officials-") if target == "write" else str(path) == target)
            if selected and open_error:
                raise OSError("private open payload")
            fd = real_open(path, flags, *args, **kwargs)
            if selected:
                captured.append(fd)
            if flags & os.O_DIRECTORY:
                directories.append(fd)
            return fd

        def wrapping(fd, *args, **kwargs):
            if fd in captured:
                raise error
            return real_fdopen(fd, *args, **kwargs)

        def closing(fd):
            if fd in captured:
                close_attempts.append(fd)
                if close_error:
                    raise OSError("private close payload")
            return real_close(fd)

        try:
            with mock.patch.object(oc.os, "open", side_effect=opening), \
                    mock.patch.object(oc.os, "fdopen", side_effect=wrapping), \
                    mock.patch.object(oc.os, "close", side_effect=closing):
                yield captured, directories, close_attempts
        finally:
            # Also clean leaked descriptors on the deliberately RED implementation.
            for fd in set(captured):
                try:
                    real_close(fd)
                except OSError:
                    pass

    def snapshot(self):
        return self.fixture.snap() if self.fixture.file.exists() else None

    def unchanged(self, before):
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.fixture.temps(), [])

    def test_reader_fdopen_failure_closes_actual_raw_fd(self):
        folder = oc._content_fd(self.root)
        sentinel = os.open(os.devnull, os.O_RDONLY)
        self.addCleanup(os.close, sentinel)
        self.addCleanup(os.close, folder)
        with self.construction_fault("site.json") as (fds, _, attempts):
            for _ in range(3):
                with self.assertRaises(oc.OfficialsError) as caught:
                    oc._read(folder, "site.json", "content/site.json")
                self.assertEqual(str(caught.exception), "content/site.json: cannot be read")
                self.assert_closed(fds[-1])
                os.fstat(folder)
                os.fstat(sentinel)
            self.assertEqual(len(fds), 3)
            self.assertEqual(attempts, fds)

    def test_staged_fdopen_failure_closes_fd_and_preserves_target(self):
        # A foreign staging-looking file must never be removed by cleanup.
        foreign = self.fixture.content / ".officials-foreign.tmp"
        foreign.write_bytes(b"unrelated")
        for existing in (False, True):
            if existing:
                self.fixture.add("Ash Example")
                self.fixture.add("Birch Example")
                os.chmod(self.fixture.file, 0o640)
            calls = [lambda: self.fixture.add("Cedar Example")]
            if existing:
                calls += [lambda: oc.recheck_official(self.root, agency_id=fixtures.COUNCIL,
                           name="Ash Example", checked_on="2026-10-01", today=fixtures.TODAY),
                          lambda: self.fixture.remove("Birch Example")]
            before = self.snapshot()
            for call in calls:
                for _ in range(3):
                    with self.construction_fault("write") as (fds, dirs, attempts), \
                            mock.patch.object(oc.os, "link") as link, mock.patch.object(oc.os, "replace") as replace:
                        with self.assertRaises(oc.OfficialsError) as caught:
                            call()
                        self.assertEqual(str(caught.exception),
                                         "content/officials.json: write failed; the previous file was kept")
                        self.assertEqual(len(fds), 1)
                        self.assert_closed(fds[0])
                        self.assertEqual(attempts, fds)
                        for fd in dirs:
                            self.assert_closed(fd)
                        link.assert_not_called()
                        replace.assert_not_called()
                    self.assertEqual(self.snapshot(), before)
                    self.assertEqual(self.fixture.temps(), [foreign.name])
                    self.assertEqual(foreign.read_bytes(), b"unrelated")
        foreign.unlink()

    def test_read_failures_cover_state_meetings_and_shared_consumers(self):
        self.fixture.add("Ash Example")
        import json
        agencies = [{"agency_id": fixtures.COUNCIL, "name": "Cedar council"}]
        (self.fixture.content / "agencies.json").write_text(json.dumps(agencies), encoding="utf-8")
        (self.fixture.content / "statements.json").write_text("[]", encoding="utf-8")
        meeting = "2026-10-cedar-council.json"
        cases = [(name, lambda: oc.list_officials(self.root, today=fixtures.TODAY), oc.OfficialsError,
                  "content/" + name + ": cannot be read")
                 for name in ("site.json", "agencies.json", "officials.json")]
        cases += [(meeting, lambda: self.fixture.remove("Ash Example"), oc.OfficialsError,
                   "content/meetings/" + meeting + ": cannot be read"),
                  ("statements.json", lambda: statements.list_statements(self.root, today=fixtures.TODAY),
                   statements.StatementError, "content/statements.json: cannot be read; nothing was written"),
                  ("officials.json", lambda: today._official_rechecks(self.root, fixtures.TODAY, "--directory X"),
                   oc.OfficialsError, "content/officials.json: cannot be read")]
        before, statement_bytes = self.snapshot(), (self.fixture.content / "statements.json").read_bytes()
        for target, call, kind, message in cases:
            with self.subTest(target=target, consumer=message), self.construction_fault(target) as (fds, dirs, _):
                with self.assertRaises(kind) as caught:
                    call()
                self.assertEqual(str(caught.exception), message)
                self.assertEqual(len(fds), 1)
                self.assert_closed(fds[0])
                for fd in dirs:
                    self.assert_closed(fd)
            self.unchanged(before)
            self.assertEqual((self.fixture.content / "statements.json").read_bytes(), statement_bytes)

    def test_raw_cleanup_runs_without_changing_exception_taxonomy(self):
        for target in ("site.json", "write"):
            call = (lambda: oc.list_officials(self.root, today=fixtures.TODAY)) if target != "write" else \
                   (lambda: self.fixture.add("Ash Example"))
            for error in (RuntimeError("programming failure"), Cancelled("cancelled")):
                with self.subTest(target=target, kind=type(error)), \
                        self.construction_fault(target, error) as (fds, _, attempts):
                    with self.assertRaises(type(error)) as caught:
                        call()
                    self.assertIs(caught.exception, error)
                    self.assert_closed(fds[0])
                    self.assertEqual(attempts, fds)
                self.unchanged(None)
            with self.construction_fault(target, close_error=True) as (fds, _, attempts):
                with self.assertRaises(oc.OfficialsError) as caught:
                    call()
                expected = ("content/site.json: cannot be read" if target != "write" else
                            "content/officials.json: write failed; the previous file was kept")
                self.assertEqual(str(caught.exception), expected)
                self.assertEqual(attempts, fds)  # no retry of an uncertain close
                os.fstat(fds[0])  # harness, not production, closes this deliberately failed close
            self.unchanged(None)

    def test_open_failure_has_no_unacquired_close(self):
        sentinel = os.open(os.devnull, os.O_RDONLY)
        self.addCleanup(os.close, sentinel)
        for target in ("site.json", "write"):
            with self.construction_fault(target, open_error=True) as (fds, _, attempts):
                with self.assertRaises(oc.OfficialsError):
                    oc.list_officials(self.root, today=fixtures.TODAY) if target != "write" else \
                        self.fixture.add("Ash Example")
                self.assertEqual((fds, attempts), ([], []))
                os.fstat(sentinel)
            self.unchanged(None)

    def test_fd_zero_is_owned_in_isolated_child(self):
        for target in ("site.json", "write"):
            child = subprocess.run([sys.executable, "-B", "-m", "tests.test_officials_fd_ownership",
                                    "--fd-zero", target], capture_output=True, text=True, timeout=30)
            self.assertEqual(child.returncode, 0, child.stdout + child.stderr)

    def test_successful_transfer_has_one_owner_on_later_failure(self):
        real_fdopen, real_open = os.fdopen, os.open
        # Reuse the closed stream's fd immediately: a raw double-close would destroy this sentinel.
        for operation in ("success-read", "fstat", "read", "success-write", "fchmod", "write", "flush", "fsync"):
            recycled, streams = [], []
            writing = operation in ("success-write", "fchmod", "write", "flush", "fsync")
            before = self.snapshot()

            class Stream:
                def __init__(self, handle):
                    self.handle = handle
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    fd = self.handle.fileno()
                    self.handle.close()
                    sentinel = real_open(os.devnull, os.O_RDONLY)
                    recycled.append((fd, sentinel))
                def fileno(self):
                    return self.handle.fileno()
                def read(self, limit):
                    if operation == "read":
                        raise OSError("private read payload")
                    return self.handle.read(limit)
                def write(self, data):
                    if operation == "write":
                        raise OSError("private write payload")
                    return self.handle.write(data)
                def flush(self):
                    if operation == "flush":
                        raise OSError("private flush payload")
                    return self.handle.flush()

            def wrapping(fd, mode):
                handle = real_fdopen(fd, mode)
                if mode == ("wb" if writing else "rb"):
                    streams.append(handle)
                    return Stream(handle)
                return handle

            try:
                with contextlib.ExitStack() as stack:
                    stack.enter_context(mock.patch.object(oc.os, "fdopen", side_effect=wrapping))
                    if operation in ("fstat", "fchmod", "fsync"):
                        real = getattr(os, operation)
                        def fail(fd, *args):
                            if any(not h.closed and h.fileno() == fd for h in streams):
                                raise OSError("private later payload")
                            return real(fd, *args)
                        stack.enter_context(mock.patch.object(oc.os, operation, side_effect=fail))
                    if operation.startswith("success"):
                        if writing:
                            self.assertTrue(self.fixture.add("Ash Example")["created"])
                        else:
                            folder = oc._content_fd(self.root)
                            try:
                                self.assertEqual(oc._read(folder, "site.json", "content/site.json")[0],
                                                 (self.fixture.content / "site.json").read_bytes())
                            finally:
                                os.close(folder)
                    else:
                        with self.assertRaises(oc.OfficialsError) as caught:
                            if writing:
                                self.fixture.add("Birch Example")
                            else:
                                oc.list_officials(self.root, today=fixtures.TODAY)
                        self.assertEqual(str(caught.exception),
                                         "content/officials.json: write failed; the previous file was kept" if writing
                                         else "content/site.json: cannot be read")
                self.assertEqual(len(recycled), 1)
                for old, sentinel in recycled:
                    self.assertEqual(old, sentinel)
                    os.fstat(sentinel)
                self.assertTrue(all(h.closed for h in streams))
                if operation == "success-write":
                    expected = ('{\n  "schema_version": 1,\n  "bodies": [\n    {\n'
                                '      "agency_id": "ca-cedar-city-council",\n      "officials": [\n        {\n'
                                '          "name": "Ash Example",\n          "role": "Council member",\n'
                                '          "source_url": "https://cedar.example/council",\n'
                                '          "checked_on": "2026-09-01"\n        }\n      ]\n    }\n  ]\n}\n')
                    self.assertEqual(self.fixture.file.read_bytes(), expected.encode())
                    self.assertEqual(stat.S_IMODE(self.fixture.file.stat().st_mode), 0o600)
                else:
                    self.unchanged(before)
            finally:
                for _, fd in recycled:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

    def test_actual_officials_cli_preserves_refusal_contract(self):
        for target in ("site.json", "write"):
            args = ["campaign_tool", "officials", "add", "--agency", fixtures.COUNCIL,
                    "--name", "Ash Example", "--role", "Mayor", "--source-url", fixtures.SRC,
                    "--checked-on", date.today().isoformat(), "--directory", str(self.root)]
            out, err = io.StringIO(), io.StringIO()
            with self.construction_fault(target) as (fds, _, _), mock.patch.object(sys, "argv", args), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                    self.assertRaises(SystemExit) as caught:
                cli.main()
            self.assertEqual(caught.exception.code, 2)
            self.assertEqual(out.getvalue(), "")
            expected = ("content/site.json: cannot be read" if target != "write" else
                        "content/officials.json: write failed; the previous file was kept")
            self.assertEqual(err.getvalue(), "Stopped: " + expected + "\n")
            self.assertEqual(len(fds), 1)
            self.assert_closed(fds[0])
            self.unchanged(None)


def fd_zero_child(target):
    case = OfficialsDescriptorTests()
    case.setUp()
    real_open = os.open
    saved = os.dup(0)

    def opening(path, flags, *args, **kwargs):
        selected = str(path).startswith(".officials-") if target == "write" else str(path) == target
        if selected:
            os.close(0)
        return real_open(path, flags, *args, **kwargs)

    try:
        with mock.patch.object(oc.os, "open", side_effect=opening):
            with case.construction_fault(target) as (fds, _, _):
                with case.assertRaises(oc.OfficialsError):
                    case.fixture.add("Ash Example") if target == "write" else \
                        oc.list_officials(case.root, today=fixtures.TODAY)
                case.assertEqual(fds, [0])
                case.assert_closed(0)
    finally:
        os.dup2(saved, 0)
        os.close(saved)
        case.doCleanups()


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--fd-zero":
        fd_zero_child(sys.argv[2])
    else:
        unittest.main()
