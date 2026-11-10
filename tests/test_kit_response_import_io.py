"""Real synthetic CSV acquisition faults; no campaign data or network."""
import contextlib
import errno
import json
import os
from datetime import date
import unittest
from unittest import mock

from campaign_tool import kit_import_steps as subject
from tests import test_kit_response_import as fixture

MESSAGE = "csv cannot be read; nothing was written"


class TimelineIOTests(unittest.TestCase):
    # Reuse setup/helpers through an alias, without discovering the original TestCase again.
    setUp = fixture.KitResponseImportTests.setUp
    write = fixture.KitResponseImportTests.write
    do = fixture.KitResponseImportTests.do
    tree = fixture.KitResponseImportTests.tree

    @contextlib.contextmanager
    def fault(self, path, seam, error):
        real_open, real_stat, real_close, real_fdopen = os.open, os.fstat, os.close, os.fdopen
        fds, handles, closes, bounds = [], [], [], []

        def opened(name, flags, *args, **kwargs):
            fd = real_open(name, flags, *args, **kwargs)
            if name == path:
                fds.append(fd)
            return fd

        def checked(fd):
            result = real_stat(fd)
            if fd in fds and seam in ("fstat", "cleanup"):
                raise error
            return result

        def closed(fd):
            if fd in fds:
                closes.append(fd)
            real_close(fd)
            if fd in fds and seam == "cleanup":
                raise OSError(errno.EIO, "PRIVATE-CLEANUP")

        class Stream:
            def __init__(self, raw):
                self.raw = raw

            def __enter__(self):
                return self

            def read(self, size):
                bounds.append(size)
                if seam == "read":
                    # Consume a complete valid row before the injected read error.
                    self.raw.read(size)
                    raise error
                if seam == "returned-size":
                    self.raw.read(size)
                    return b"x" * size
                return self.raw.read(size)

            def __exit__(self, *exc):
                self.raw.close()
                if seam == "close":
                    raise error

        def wrapped(fd, mode):
            if fd in fds and seam == "fdopen":
                raise error
            raw = real_fdopen(fd, mode)
            handles.append(raw)
            return Stream(raw)

        with mock.patch.object(os, "open", side_effect=opened), \
                mock.patch.object(os, "fstat", side_effect=checked), \
                mock.patch.object(os, "close", side_effect=closed), \
                mock.patch.object(os, "fdopen", side_effect=wrapped):
            try:
                yield fds, handles, closes, bounds
            finally:
                # Check genuine descriptor validity, then clean any RED-run leaks.
                leaked = []
                for fd in fds:
                    try:
                        real_stat(fd)
                    except OSError as exc:
                        self.assertEqual(exc.errno, errno.EBADF)
                    else:
                        leaked.append(fd)
                        real_close(fd)
                self.assertEqual(leaked, [], "reader leaked its acquired descriptor")

    def refuse_case(self, seam, error, *, absent=False):
        path = self.write("2026-03-05,acknowledged,PRIVATE-NOTE,,,", name="PRIVATE-PATH.csv")
        saved_sent = self.sent.read_bytes()
        if absent:
            self.sent.unlink()
        before = self.tree(), path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns
        # An unrelated live descriptor must never be closed, even during raw cleanup.
        with path.open("rb") as sentinel:
            for cli in (False, True):
                for apply in (False, True):
                    with self.subTest(seam=seam, cli=cli, apply=apply, absent=absent):
                        with contextlib.ExitStack() as stack:
                            for obj, name in ((subject, "_check_header"), (subject.csv, "reader"),
                                              (subject.kit_track, "read_sent"), (subject.kit_track, "_request"),
                                              (subject.tempfile, "TemporaryDirectory"),
                                              (subject.kit_track, "record_step"), (subject, "_check_capacity"),
                                              (subject, "_write")):
                                stack.enter_context(mock.patch.object(
                                    obj, name, side_effect=AssertionError("downstream work: " + name)))
                            with self.fault(path, seam, error) as state:
                                if cli:
                                    self.assertEqual(self.do(path, *(("--apply",) if apply else ())),
                                                     (2, "", "Stopped: " + MESSAGE + "\n"))
                                else:
                                    with self.assertRaises(subject.ImportRefused) as raised:
                                        subject.import_steps(self.root, self.agency, path, apply=apply)
                                    self.assertEqual(str(raised.exception), MESSAGE)
                                    self.assertEqual(raised.exception.exit_code, 2)
                                    self.assertIsNone(raised.exception.__cause__)
                                    self.assertTrue(raised.exception.__suppress_context__)
                            fds, handles, closes, bounds = state
                            self.assertEqual(len(fds), 1)
                            self.assertTrue(all(h.closed for h in handles))
                            self.assertEqual(closes, fds if seam in ("fstat", "fdopen", "cleanup") else [])
                            self.assertEqual(bounds, [subject.MAX_BYTES + 1] if seam in ("read", "close") else [])
                        os.fstat(sentinel.fileno())
                        self.assertEqual((self.tree(), path.read_bytes(), path.stat().st_mode,
                                          path.stat().st_mtime_ns), before)
        if absent:  # restore only the synthetic fixture for the next matrix case
            self.sent.write_bytes(saved_sent)

    def test_post_open_fault_matrix_is_private_and_precedes_all_campaign_work(self):
        for seam in ("fstat", "fdopen", "read", "close", "cleanup"):
            for absent in (False, True):
                self.refuse_case(seam, OSError(errno.EIO, "PRIVATE-ERROR\n\x1b[2J", "PRIVATE-FILE"), absent=absent)

    def test_oserror_variants_never_disclose_values(self):
        for error in (PermissionError(errno.EACCES, "PRIVATE-DENIED", "PRIVATE-FIRST", None, "PRIVATE-SECOND"),
                      FileNotFoundError(errno.ENOENT, "PRIVATE-MISSING", "PRIVATE-PATH"),
                      OSError("PRIVATE-BARE")):
            for seam in ("fstat", "fdopen", "read", "close"):
                self.refuse_case(seam, error)

    def test_non_oserror_construction_failure_propagates_after_raw_cleanup(self):
        path = self.write("2026-03-05,acknowledged,,,,")
        for error in (RuntimeError("programmer error"), KeyboardInterrupt(), SystemExit(7)):
            with self.subTest(error=type(error).__name__):
                with self.fault(path, "fdopen", error) as state:
                    with self.assertRaises(type(error)) as raised:
                        subject._read_rows(path)
                    self.assertIs(raised.exception, error)
                self.assertEqual(state[2], state[0])
                self.assertEqual(state[1], [])

    def test_non_oserror_read_failure_closes_only_stream_and_propagates(self):
        path = self.write("2026-03-05,acknowledged,,,,")
        error = RuntimeError("programmer error")
        with self.fault(path, "read", error) as state:
            with self.assertRaises(RuntimeError) as raised:
                subject._read_rows(path)
            self.assertIs(raised.exception, error)
        self.assertEqual(state[2], [])
        self.assertTrue(state[1][0].closed)

    def test_returned_byte_limit_still_refuses_after_stream_close(self):
        path = self.write("2026-03-05,acknowledged,,,,")
        with self.fault(path, "returned-size", OSError()) as state:
            with self.assertRaisesRegex(subject.ImportRefused, "^csv is larger than 256 KiB$"):
                subject._read_rows(path)
        self.assertEqual(state[3], [subject.MAX_BYTES + 1])
        self.assertTrue(state[1][0].closed)
        self.assertEqual(state[2], [])

    def test_fifo_admission_retains_its_diagnostic_and_closes_raw_fd(self):
        path = self.tmp / "input.fifo"
        os.mkfifo(path)
        with self.fault(path, "none", OSError()) as state:
            with self.assertRaisesRegex(subject.ImportRefused, "^csv must be a regular file$"):
                subject._read_rows(path)
        self.assertEqual(state[2], state[0])
        self.assertEqual(state[1], [])

    def test_success_closes_transferred_stream_without_raw_close(self):
        path = self.write("2026-03-05,acknowledged,,,,")
        with self.fault(path, "none", OSError()) as state:
            self.assertEqual(subject._read_rows(path), [(2, ["2026-03-05", "acknowledged", "", "", "", ""])])
        self.assertEqual(state[2], [])
        self.assertTrue(state[1][0].closed)
        self.assertEqual(state[3], [subject.MAX_BYTES + 1])

    def test_success_formats_reports_named_scope_single_write_and_replay(self):
        fixture.KitResponseImportTests.make_second(self)
        original = self.sent.read_bytes()
        row = '2026-03-05,acknowledged,"Portal, receipt",,,'
        for request in ("main", "second"):
            extra = () if request == "main" else ("--request", request)
            for data in (fixture.HEADER + "\n" + row,
                         fixture.HEADER + "\r\n" + row + "\r\n",
                         "\ufeff" + fixture.HEADER + "\n" + row + "\n"):
                with self.subTest(request=request, data=data):
                    self.sent.write_bytes(original)
                    path = self.tmp / "format.csv"
                    path.write_bytes(data.encode("utf-8"))
                    before = self.tree()
                    expected = {"agency_id": self.agency, "request": request, "applied": False,
                                "rows": 1, "added": 1, "skipped": 0, "lines": [],
                                "steps": [{"line": 2, "on": "2026-03-05", "kind": "acknowledged"}]}
                    self.assertEqual(subject.import_steps(self.root, self.agency, path, request=request,
                                                         today=date(2026, 3, 25)), expected)
                    self.assertEqual(self.do(path, *extra), (0, json.dumps(expected, indent=2) +
                                     "\nChecked only; nothing was written, fetched or sent.\n", ""))
                    self.assertEqual(self.tree(), before)
                    with mock.patch.object(subject, "_write", wraps=subject._write) as write:
                        result = self.do(path, "--apply", *extra)
                    expected["applied"] = True
                    self.assertEqual(result, (0, json.dumps(expected, indent=2) +
                                     "\nRecorded only. Nothing was fetched or sent.\n", ""))
                    self.assertEqual(len([c for c in write.call_args_list if c.args[0] == self.sent]), 1)
                    self.assertEqual(self.sent.stat().st_mode & 0o777, 0o600)
                    other = f"{self.agency}#second" if request == "main" else self.agency
                    self.assertEqual(subject.kit_track.read_sent(self.root)[other]["steps"], [])
                    after = self.tree()
                    with mock.patch.object(subject, "_write", wraps=subject._write) as write:
                        replay = self.do(path, "--apply", *extra)
                    expected.update(applied=False, added=0, skipped=1,
                                    lines=["line 2: already recorded, skipped"], steps=[])
                    self.assertEqual(replay, (0, json.dumps(expected, indent=2) +
                                     "\nChecked only; nothing was written, fetched or sent.\n", ""))
                    self.assertEqual([c for c in write.call_args_list if c.args[0] == self.sent], [])
                    self.assertEqual(self.tree(), after)


if __name__ == "__main__":
    unittest.main()
