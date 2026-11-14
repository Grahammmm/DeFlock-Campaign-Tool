"""RF1036-2: a secondary close() failure never replaces the primary letter/receipt failure. Synthetic only.

Run from the repository root: python3 -B -m unittest tests.test_meeting_comment_close_errors -q
"""
import contextlib
import errno
import hashlib
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import types
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from campaign_tool import cli, meeting_comments as mc  # noqa: E402
from campaign_tool.content import MAX_JSON_BYTES, ContentError  # noqa: E402

TODAY = date.today()
MEETING_DAY = TODAY + timedelta(days=14)
EMPTY = b'{"schema_version": 1, "comments": {}}'
GOOD = {"on": "2026-03-12", "channel": "email", "recorded_at": "2026-03-12T00:00:00Z"}
REAL_OPEN, REAL_CLOSE, REAL_READ, REAL_FSTAT = os.open, os.close, os.read, os.fstat


def secondary():
    return OSError(errno.EIO, "SYNTHETIC secondary close failure")


def primary_read_error():
    return OSError(errno.ENOSPC, "SYNTHETIC primary read failure")


class Fds:
    """Scoped, real-syscall hooks for ONE path: every descriptor the code opens on it is an ownership generation.

    os.close really closes the descriptor first, then raises ``close_exc`` (if set). Calls on any other
    descriptor, and on this path's already-released numbers, are recorded as strays but still forwarded."""

    def __init__(self, case, path, close_exc=None, fstat_exc=None, read_exc=None, read_after=0, read_cap=None,
                 fake_size=None, after_close=None):
        self.path, self.close_exc, self.fstat_exc, self.read_exc = str(path), close_exc, fstat_exc, read_exc
        self.read_after, self.read_cap, self.fake_size, self.after_close = read_after, read_cap, fake_size, after_close
        self.owned, self.ever, self.opens, self.closes, self.strays = {}, set(), 0, [], []
        self.patchers = []
        case.made.append(self)
        self.requested, self.returned, self.reads, self.events = [], 0, 0, []
        for name, hook in (("open", self._open), ("close", self._close), ("read", self._read), ("fstat", self._fstat)):
            patcher = mock.patch(f"os.{name}", hook)
            patcher.start()
            self.patchers.append(patcher)
            case.addCleanup(patcher.stop)

    def _open(self, path, flags, *args, **kwargs):
        fd = REAL_OPEN(path, flags, *args, **kwargs)
        if str(path) == self.path:
            self.opens += 1
            self.owned[fd] = self.opens
            self.ever.add(fd)
            self.events.append("open")
        return fd

    def _close(self, fd):
        if fd in self.owned:
            self.closes.append((fd, self.owned.pop(fd)))
            self.events.append("close")
            REAL_CLOSE(fd)
            if self.after_close:
                self.after_close(fd)
            if self.close_exc is not None:
                raise self.close_exc
            return None
        if fd in self.ever:
            self.strays.append(fd)
        return REAL_CLOSE(fd)

    def _fstat(self, fd, *args, **kwargs):
        if fd in self.owned:
            if self.fstat_exc is not None:
                raise self.fstat_exc
            info = REAL_FSTAT(fd, *args, **kwargs)
            if self.fake_size is not None:
                return types.SimpleNamespace(st_mode=info.st_mode, st_size=self.fake_size)
            return info
        return REAL_FSTAT(fd, *args, **kwargs)

    def _read(self, fd, n):
        if fd not in self.owned:
            return REAL_READ(fd, n)
        self.reads += 1
        if self.read_exc is not None and self.reads > self.read_after:
            raise self.read_exc
        n = min(n, self.read_cap) if self.read_cap else n
        self.requested.append(n)
        data = REAL_READ(fd, n)
        self.returned += len(data)
        return data

    def stop(self):
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.patchers = []

    def assert_single_close(self, case):
        case.assertEqual(len(self.closes), self.opens, "every acquired descriptor generation closes exactly once")
        case.assertEqual(len({gen for _, gen in self.closes}), len(self.closes))
        case.assertEqual(self.strays, [], "a released descriptor number was closed again")
        case.assertEqual(self.owned, {}, "a descriptor was left open")


def meeting(ident):
    starts = f"{MEETING_DAY.isoformat()}T18:00:00-07:00"
    deadline = f"{(MEETING_DAY - timedelta(days=1)).isoformat()}T12:00:00-07:00"
    return {"id": ident, "body": f"Synthetic Council {ident}", "starts_at": starts, "status": "scheduled",
            "agenda_url": f"https://agenda.synthetic.example/{ident}", "comment_kit_md": f"Hello {ident}.\n",
            "participation": {"in_person": True, "remote": None,
                              "written_comment": {"how": "email the synthetic clerk", "deadline": deadline},
                              "source_url": "https://agenda.synthetic.example/rules", "verified_by": "Pat Organizer",
                              "verified_at": TODAY.isoformat()}}


def cli_run(*arguments):
    out, err, code = io.StringIO(), io.StringIO(), 0
    with mock.patch.object(sys, "argv", ["campaign_tool", *map(str, arguments)]), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            cli.main()
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


class CloseErrorBase(unittest.TestCase):
    def unpatch(self):
        """Stop every Fds hook made so far (newest first) so the next call runs the real syscalls."""
        while self.made:
            self.made.pop().stop()

    def setUp(self):
        self.made = []
        for target in ("urllib.request.urlopen", "socket.socket.connect"):
            patcher = mock.patch(target, side_effect=AssertionError("network used"))
            patcher.start()
            self.addCleanup(patcher.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-close-errors-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name).resolve()
        self.root = self.tmp / "campaign"
        shutil.copytree(REPO / "examples" / "fictional-campaign", self.root)
        folder = self.root / "content" / "meetings"
        shutil.rmtree(folder, ignore_errors=True)
        folder.mkdir()
        (folder / "m-a.json").write_text(json.dumps(meeting("m-a")), encoding="utf-8")
        self.store = self.root / "kit" / "comments-sent.json"
        self.letter_path = self.tmp / "letter-as-sent.txt"

    def oversize_letter(self):
        self.letter_path.write_bytes(b"x" * (mc.MAX_LETTER_BYTES + 1))
        return self.letter_path

    def snapshot(self):
        """bytes, mode, inode, mtime_ns and membership of every file and folder (atime excluded)."""
        state = {}
        for base in (self.root, self.letter_path):
            paths = [base] + (sorted(base.rglob("*")) if base.is_dir() else [])
            for path in paths:
                if not path.exists():
                    continue
                info = path.lstat()
                body = path.read_bytes() if stat.S_ISREG(info.st_mode) else None
                state[str(path)] = (body, info.st_mode, info.st_ino, info.st_mtime_ns)
        return state

    def record(self, **kwargs):
        return mc.record(self.root, "m-a", TODAY.isoformat(), "email", today=TODAY, **kwargs)

    def cli_record(self, *extra):
        return cli_run("meetings-comment-sent", "--directory", self.root, "--id", "m-a", "--on", TODAY.isoformat(),
                       "--channel", "email", *extra)


class LetterPrimaryTests(CloseErrorBase):
    def test_oversize_letter_primary_survives_real_close_error(self):
        letter = self.oversize_letter()
        for replace in (False, True):
            with self.subTest(replace=replace):
                fds = Fds(self, letter, close_exc=secondary())
                with self.assertRaises(mc.MeetingCommentError) as caught:
                    mc._letter(letter)
                self.assertEqual(str(caught.exception), "--file: must be at most 5 MiB")
                fds.assert_single_close(self)
                fds = Fds(self, letter, close_exc=secondary())
                before = self.snapshot()
                with self.assertRaises(mc.MeetingCommentError) as caught:
                    self.record(file=letter, replace=replace)
                self.assertEqual(str(caught.exception), "--file: must be at most 5 MiB")
                fds.assert_single_close(self)
                self.assertEqual(self.snapshot(), before)
                self.assertFalse(self.store.exists())
                fds = Fds(self, letter, close_exc=secondary())
                code, out, err = self.cli_record(*(["--replace"] if replace else []), "--file", letter)
                self.assertEqual((code, out), (1, ""))
                self.assertIn("--file: must be at most 5 MiB", err)
                self.assertNotIn("Recorded only", out + err)
                self.assertNotIn("Traceback", err)
                self.assertNotIn("SYNTHETIC", out + err)
                fds.assert_single_close(self)
                self.assertFalse(self.store.exists())

    def test_nonregular_letter_primary_survives_close_error(self):
        folder = self.tmp / "letter-folder"
        folder.mkdir()
        fifo = self.tmp / "letter-fifo"
        os.mkfifo(fifo)
        for name, path in (("directory", folder), ("fifo", fifo)):
            with self.subTest(kind=name):
                fds = Fds(self, path, close_exc=secondary())
                with self.assertRaises(mc.MeetingCommentError) as caught:
                    mc._letter(path)
                self.assertEqual(str(caught.exception), "--file: must be a regular file, not a symlink or folder")
                self.assertEqual(fds.returned, 0)
                fds.assert_single_close(self)
        link = self.tmp / "letter-link"
        link.symlink_to(self.oversize_letter())
        fds = Fds(self, link, close_exc=secondary())
        with self.assertRaises(mc.MeetingCommentError) as caught:
            mc._letter(link)
        self.assertIn("--file: cannot read it as a regular file", str(caught.exception))
        self.assertEqual((fds.opens, fds.closes), (0, []))

    def test_close_only_error_refuses_without_return_or_write(self):
        self.letter_path.write_bytes("synthetic ünïcode letter\n".encode("utf-8"))
        fds = Fds(self, self.letter_path, close_exc=secondary())
        with self.assertRaises(OSError) as caught:
            mc._letter(self.letter_path)
        self.assertEqual((caught.exception.errno, caught.exception.strerror), (errno.EIO, secondary().strerror))
        fds.assert_single_close(self)
        fds = Fds(self, self.letter_path, close_exc=secondary())
        before = self.snapshot()
        with self.assertRaises(OSError) as caught:
            self.record(file=self.letter_path)
        self.assertNotIsInstance(caught.exception, mc.MeetingCommentError)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.store.exists())
        code, out, err = self.cli_record("--file", self.letter_path)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("SYNTHETIC secondary close failure", err)
        self.assertNotIn("Recorded only", out + err)
        self.assertFalse(self.store.exists())
        self.unpatch()
        entry = self.record(file=self.letter_path)  # unpatched retry succeeds
        self.assertEqual(entry["letter_bytes"], self.letter_path.stat().st_size)
        self.assertTrue(self.store.exists())


class ReceiptPrimaryTests(CloseErrorBase):
    def setUp(self):
        super().setUp()
        self.store.parent.mkdir(exist_ok=True)

    def test_receipt_admission_and_read_primaries_survive_cleanup(self):
        self.store.write_bytes(EMPTY)
        fds = Fds(self, self.store, close_exc=secondary())
        with self.assertRaises(ContentError) as caught:
            mc._read_receipts(self.store, limit=1)
        self.assertEqual(str(caught.exception), f"{mc.LABEL}: file too large")
        self.assertEqual(fds.returned, 0)
        fds.assert_single_close(self)
        for label, kind in (("folder", "dir"), ("fifo", "fifo")):
            odd = self.tmp / f"receipts-{label}"
            odd.mkdir() if kind == "dir" else os.mkfifo(odd)
            fds = Fds(self, odd, close_exc=secondary())
            with self.assertRaises(ContentError) as caught:
                mc._read_receipts(odd)
            self.assertEqual(str(caught.exception), f"{mc.LABEL}: must be a regular file")
            fds.assert_single_close(self)
        for read_after in (0, 1):  # failure on the first read, and after a partial read
            self.store.write_bytes(EMPTY + b" " * 200000)
            primary = primary_read_error()
            fds = Fds(self, self.store, close_exc=secondary(), read_exc=primary, read_after=read_after)
            with mock.patch("json.loads", side_effect=AssertionError("parsed")):
                with self.assertRaises(OSError) as caught:
                    mc._read_receipts(self.store)
            self.assertIs(caught.exception, primary)
            fds.assert_single_close(self)
        fstat_primary = OSError(errno.EIO, "SYNTHETIC primary fstat failure")
        fds = Fds(self, self.store, close_exc=secondary(), fstat_exc=fstat_primary)
        with self.assertRaises(OSError) as caught:
            mc._read_receipts(self.store)
        self.assertIs(caught.exception, fstat_primary)
        fds.assert_single_close(self)
        # Growth after the size check: the post-read overflow refusal is a primary failure too.
        self.store.write_bytes(EMPTY + b" " * 100)
        fds = Fds(self, self.store, close_exc=secondary(), fake_size=0)
        with mock.patch("json.loads", side_effect=AssertionError("parsed")):
            with self.assertRaises(ContentError) as caught:
                mc._read_receipts(self.store, limit=len(EMPTY))
        self.assertEqual(str(caught.exception), f"{mc.LABEL}: file too large")
        self.assertEqual(fds.returned, len(EMPTY) + 1)
        fds.assert_single_close(self)
        # The public reader keeps its class-tagged diagnostic: ContentError vs OSError.
        self.store.write_bytes(EMPTY + b" " * 200000)
        for exc, tag in ((primary_read_error(), "OSError"),):
            fds = Fds(self, self.store, close_exc=secondary(), read_exc=exc)
            with self.assertRaises(mc.MeetingCommentError) as caught:
                mc.read(self.root)
            self.assertIn(f"({tag})", str(caught.exception))
            self.assertNotIn("SYNTHETIC", str(caught.exception))
        self.store.write_bytes(padded_envelope(MAX_JSON_BYTES + 1))
        fds = Fds(self, self.store, close_exc=secondary())
        with self.assertRaises(mc.MeetingCommentError) as caught:
            mc.read(self.root)
        self.assertIn("(ContentError)", str(caught.exception))

    def test_close_only_error_on_receipts_refuses_without_parsing(self):
        self.store.write_bytes(EMPTY)
        fds = Fds(self, self.store, close_exc=secondary())
        with mock.patch("json.loads", side_effect=AssertionError("parsed")):
            with self.assertRaises(OSError) as caught:
                mc._read_receipts(self.store)
        self.assertNotIsInstance(caught.exception, ValueError)
        fds.assert_single_close(self)
        with self.assertRaises(mc.MeetingCommentError) as caught:
            mc.read(self.root)
        self.assertIn("(OSError)", str(caught.exception))
        before = self.snapshot()
        with self.assertRaises(mc.MeetingCommentError):
            self.record()
        self.assertEqual(self.snapshot(), before)
        code, out, err = self.cli_record()
        self.assertEqual((code, out), (1, ""))
        self.assertNotIn("Recorded only", out + err)
        self.assertEqual(self.snapshot(), before)
        self.unpatch()
        self.assertEqual(mc._read_receipts(self.store), {"schema_version": 1, "comments": {}})


def padded_envelope(size):
    return EMPTY + b" " * (size - len(EMPTY))


class InterruptionAndFreshTests(CloseErrorBase):
    def seams(self):
        """(helper call, path, kwargs that fault the fstat seam, kwargs that fault the read seam)."""
        self.letter_path.write_bytes(b"synthetic letter\n")
        self.store.parent.mkdir(exist_ok=True)
        self.store.write_bytes(EMPTY + b" " * 10)
        return ((lambda: mc._letter(self.letter_path), self.letter_path),
                (lambda: mc._read_receipts(self.store), self.store))

    def test_unexpected_and_interruption_primaries_keep_identity(self):
        factories = (lambda: RuntimeError("SYNTHETIC primary"), lambda: MemoryError("SYNTHETIC primary"),
                     lambda: KeyboardInterrupt("SYNTHETIC primary"), lambda: SystemExit(7),
                     lambda: OSError(errno.EIO, "SYNTHETIC primary"), lambda: ValueError("SYNTHETIC primary"))
        for call, path in self.seams():
            for make in factories:
                for seam in ("fstat_exc", "read_exc"):
                    for close_exc in (None, secondary()):
                        with self.subTest(path=path.name, primary=type(make()).__name__, seam=seam,
                                          closing=bool(close_exc)):
                            primary = make()
                            fds = Fds(self, path, close_exc=close_exc, **{seam: primary})
                            with self.assertRaises(type(primary)) as caught:
                                call()
                            self.assertIs(caught.exception, primary)
                            fds.assert_single_close(self)
                            self.unpatch()

    def test_fresh_non_oserror_cleanup_exception_is_not_swallowed(self):
        for call, path in self.seams():
            for seam_kwargs in ({}, {"fstat_exc": OSError(errno.EIO, "SYNTHETIC primary")}):
                with self.subTest(path=path.name, primary=bool(seam_kwargs)):
                    fresh = RuntimeError("SYNTHETIC fresh cleanup failure")
                    fds = Fds(self, path, close_exc=fresh, **seam_kwargs)
                    with self.assertRaises(RuntimeError) as caught:
                        call()
                    self.assertIs(caught.exception, fresh)
                    fds.assert_single_close(self)
                    self.unpatch()

    def test_failed_open_acquires_nothing_to_close(self):
        absent = self.tmp / "absent"
        fds = Fds(self, absent, close_exc=secondary())
        with self.assertRaises(mc.MeetingCommentError) as caught:
            mc._letter(absent)
        self.assertIn("cannot read it as a regular file", str(caught.exception))
        with self.assertRaises(FileNotFoundError):
            mc._read_receipts(absent)
        self.assertEqual((fds.opens, fds.closes, fds.strays), (0, [], []))


class RecycledDescriptorTests(CloseErrorBase):
    def test_recycled_descriptor_is_never_closed_twice(self):
        sentinels = []
        self.addCleanup(lambda: [REAL_CLOSE(fd) for fd in sentinels if _is_open(fd)])

        def recycle(fd):
            sentinel = REAL_OPEN(os.devnull, os.O_RDONLY)  # lowest free number: the one just released
            sentinels.append(sentinel)
            self.assertEqual(sentinel, fd)

        letter = self.oversize_letter()
        self.store.parent.mkdir(exist_ok=True)
        self.store.write_bytes(EMPTY)
        for label, call, path, expected in (
                ("letter", lambda: mc._letter(letter), letter, mc.MeetingCommentError),
                ("receipts", lambda: mc._read_receipts(self.store, limit=1), self.store, ContentError)):
            with self.subTest(helper=label):
                fds = Fds(self, path, close_exc=secondary(), after_close=recycle)
                with self.assertRaises(expected):
                    call()
                fds.assert_single_close(self)
                self.assertTrue(_is_open(sentinels[-1]), "the unrelated descriptor in the recycled slot was closed")
                REAL_FSTAT(sentinels[-1])
                self.unpatch()


def _is_open(fd):
    try:
        REAL_FSTAT(fd)
    except OSError:
        return False
    return True


class StateAndCompatibilityTests(CloseErrorBase):
    def test_failures_preserve_all_campaign_state(self):
        letter = self.oversize_letter()
        mixed = {"schema_version": 1, "comments": {"m-b": {**GOOD}, "m-c": {**GOOD, "letter_sha256": "a" * 64,
                                                                         "letter_bytes": 3}}}
        for label, receipts in (("absent", None), ("existing", json.dumps(mixed).encode("utf-8"))):
            with self.subTest(store=label):
                if receipts is not None:
                    self.store.parent.mkdir(exist_ok=True)
                    self.store.write_bytes(receipts)
                    os.chmod(self.store, 0o600)
                for replace in (False, True):
                    before = self.snapshot()
                    fds = Fds(self, letter, close_exc=secondary())
                    with self.assertRaises(mc.MeetingCommentError):
                        self.record(file=letter, replace=replace)
                    fds.assert_single_close(self)
                    self.assertEqual(self.snapshot(), before)
                    self.unpatch()
        # Earlier date, channel and duplicate refusals still precede letter acquisition.
        self.store.write_bytes(json.dumps({"schema_version": 1, "comments": {"m-a": {**GOOD}}}).encode("utf-8"))
        fds = Fds(self, letter, close_exc=secondary())
        for kwargs, fragment in (({"on": (TODAY + timedelta(days=1)).isoformat()}, "future"),
                                 ({"channel": "fax"}, "--channel"), ({"on": "2026-3-5"}, "YYYY-MM-DD"),
                                 ({}, "already has a comment recorded")):
            arguments = {"on": TODAY.isoformat(), "channel": "email", **kwargs}
            with self.assertRaises(mc.MeetingCommentError) as caught:
                mc.record(self.root, "m-a", arguments["on"], arguments["channel"], file=letter, today=TODAY)
            self.assertIn(fragment, str(caught.exception))
        self.assertEqual(fds.opens, 0)

    def test_normal_outputs_bounds_and_fallbacks_are_unchanged(self):
        cap = b"y" * mc.MAX_LETTER_BYTES
        for body in (b"", cap, "synthetic ünïcode ✓ letter".encode("utf-8"), b"\x00\xff binary\n"):
            self.letter_path.write_bytes(body)
            fds = Fds(self, self.letter_path)
            self.assertEqual(mc._letter(self.letter_path), (hashlib.sha256(body).hexdigest(), len(body)))
            fds.assert_single_close(self)
            self.unpatch()
        self.letter_path.write_bytes(b"synthetic letter body")
        entry = self.record(file=self.letter_path, now=datetime(2026, 3, 15, tzinfo=timezone.utc))
        self.assertEqual(entry, {"on": TODAY.isoformat(), "channel": "email",
                                 "letter_sha256": hashlib.sha256(b"synthetic letter body").hexdigest(),
                                 "letter_bytes": 21, "recorded_at": "2026-03-15T00:00:00.000000+00:00"})
        self.assertEqual(json.loads(self.store.read_text(encoding="utf-8"))["comments"]["m-a"], entry)
        self.assertEqual(self.store.stat().st_mode & 0o777, 0o600)
        self.assertEqual(mc.read(self.root), {"m-a": entry})
        code, out, err = self.cli_record("--replace")
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(out.rstrip().endswith("Recorded only. Nothing was sent."))
        # Receipt reader: closure before decode, short reads, the cap+1 budget, duplicate/depth refusals.
        self.store.write_bytes(EMPTY)
        fds = Fds(self, self.store, read_cap=7)
        real_loads = json.loads

        def loads(*args, **kwargs):
            fds.events.append("decode")
            return real_loads(*args, **kwargs)

        with mock.patch("json.loads", loads):
            self.assertEqual(mc._read_receipts(self.store), {"schema_version": 1, "comments": {}})
        self.assertEqual(fds.events, ["open", "close", "decode"])
        self.assertEqual(fds.returned, len(EMPTY))
        self.unpatch()
        for size in (MAX_JSON_BYTES - 1, MAX_JSON_BYTES):
            self.store.write_bytes(padded_envelope(size))
            self.assertEqual(mc.read(self.root), {})
        self.store.write_bytes(padded_envelope(MAX_JSON_BYTES + 1))
        with self.assertRaises(ContentError):
            mc._read_receipts(self.store)
        fds = Fds(self, self.store, fake_size=0)
        self.store.write_bytes(padded_envelope(MAX_JSON_BYTES + 10))
        with self.assertRaises(ContentError):
            mc._read_receipts(self.store)
        self.assertEqual(fds.returned, MAX_JSON_BYTES + 1)
        self.unpatch()
        self.store.write_bytes(b'{"schema_version": 1, "comments": {}, "comments": {}}')
        with self.assertRaisesRegex(ContentError, "repeats a key"):
            mc._read_receipts(self.store)
        self.store.write_bytes(b"[" * 100000)
        with self.assertRaisesRegex(ContentError, "not valid UTF-8 JSON"):
            mc._read_receipts(self.store)


if __name__ == "__main__":
    unittest.main()
