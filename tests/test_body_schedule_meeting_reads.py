"""RF0330-3: `body-schedule list` admits each staged meeting leaf safely before printing agenda-watch dates.

Synthetic campaign only; no network. Run from the repository root:
python3 -B -W error::ResourceWarning -m unittest tests.test_body_schedule_meeting_reads -q
"""
import contextlib
import errno
import io
import json
import os
import shutil
import signal
import stat
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from campaign_tool import body_schedule_cmd as cmd, cli
from campaign_tool.content import MAX_JSON_BYTES

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
DAY = date(2026, 11, 9)
BODY = "Synthetic Council"
URL = "https://example.invalid/calendar"
SAFE = "meetings/{}: cannot be read safely"
# 2026-11-17 18:30 America/Los_Angeles, the first expected Tuesday: a staged meeting there suppresses that date.
NOV17 = "2026-11-18T02:30:00Z"
CONTROL = "agenda-watch=2026-11-17,2026-12-01,2026-12-15\n"
SUPPRESSED = "agenda-watch=2026-12-01,2026-12-15,2027-01-05\n"


def meeting(name, starts_at=NOV17, body=BODY):
    return {"id": name, "body": body, "starts_at": starts_at}


def padded(document, size):
    """UTF-8 bytes of valid JSON for ``document``, padded with trailing whitespace to exactly ``size`` bytes."""
    data = json.dumps(document).encode("utf-8")
    return data + b" " * (size - len(data))


def snapshot(root):
    result = {}
    for path in [root, *sorted(root.rglob("*"))]:
        info = path.lstat()
        value = os.readlink(path) if path.is_symlink() else path.read_bytes() if stat.S_ISREG(info.st_mode) else None
        result[str(path.relative_to(root))] = (info.st_mode, None if path.is_dir() else info.st_mtime_ns, value)
    return result


class Deadline:
    """Fail (instead of hanging the run) if the block waits, e.g. on a writerless FIFO."""
    def __enter__(self):
        def expired(*_):
            raise AssertionError("blocked on a writerless FIFO")
        self.previous = signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, 10)

    def __exit__(self, *_):
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, self.previous)


class RecordingStream:
    def __init__(self, stream, requests):
        self.stream, self.requests = stream, requests

    def read(self, *args):
        self.requests.append(args)
        return self.stream.read(*args)

    def fileno(self):
        return self.stream.fileno()

    def __enter__(self):
        self.stream.__enter__()
        return self

    def __exit__(self, *exc):
        return self.stream.__exit__(*exc)


class Base(unittest.TestCase):
    def setUp(self):
        offline = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used"))
        offline.start()
        self.addCleanup(offline.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-meeting-reads-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name).resolve()
        self.root = self.tmp / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.content = self.root / "content"
        self.folder = self.content / "meetings"
        shutil.rmtree(self.folder, ignore_errors=True)
        self.folder.mkdir()
        self.schedules(checked_on="2026-10-01")

    def schedules(self, **changes):
        value = {"body": BODY, "weekday": "tuesday", "ordinals": [1, 3], "local_start_time": "18:30",
                 "time_zone": "America/Los_Angeles", "source_url": URL, "checked_on": "2026-10-01", **changes}
        (self.content / "body-schedules.json").write_text(
            json.dumps({"schema_version": 1, "schedules": [value]}), encoding="utf-8")

    def stage(self, name, starts_at="2026-12-29T02:30:00Z"):
        path = self.folder / name
        path.write_text(json.dumps(meeting(Path(name).stem.lstrip("."), starts_at)), encoding="utf-8")
        return path

    def outside(self):
        path = self.tmp / "outside.json"
        path.write_text(json.dumps(meeting("outside")), encoding="utf-8")
        return path

    def listing(self):
        return cmd.list_schedules(self.root, today=DAY)

    def refused(self, needle):
        with self.assertRaises(cmd.BodyScheduleError) as caught:
            self.listing()
        self.assertEqual(str(caught.exception), needle)

    @contextlib.contextmanager
    def swapped(self, victim, replace):
        """After real path admission, ``replace(victim)`` runs once just before the actual open of ``victim``."""
        real_open, done = os.open, []

        def hook(path, *args, **kwargs):
            if Path(path) == victim and not done:
                done.append(1)
                replace(victim)
            return real_open(path, *args, **kwargs)
        with mock.patch.object(cmd.os, "open", side_effect=hook), Deadline():
            yield
        self.assertEqual(done, [1])

    @staticmethod
    def to_symlink(target):
        def replace(victim):
            victim.rename(victim.with_name(victim.name + ".saved"))
            victim.symlink_to(target)
        return replace

    @staticmethod
    def to_fifo(victim):
        victim.rename(victim.with_name(victim.name + ".saved"))
        os.mkfifo(victim)

    @contextlib.contextmanager
    def tracked(self):
        """Record opened descriptors and every stream read size; yields (fds, requests)."""
        real_open, real_fdopen = os.open, os.fdopen
        fds, requests = [], []

        def opening(path, *args, **kwargs):
            fd = real_open(path, *args, **kwargs)
            if Path(path).parent == self.folder:
                fds.append(fd)
            return fd

        def streaming(fd, *args, **kwargs):
            stream = real_fdopen(fd, *args, **kwargs)
            return RecordingStream(stream, requests) if fd in fds else stream
        with mock.patch.object(cmd.os, "open", side_effect=opening), \
                mock.patch.object(cmd.os, "fdopen", side_effect=streaming):
            yield fds, requests

    def closed(self, fd):
        with self.assertRaises(OSError) as caught:
            os.fstat(fd)
        self.assertEqual(caught.exception.errno, errno.EBADF)


class LeafReplacementTests(Base):
    def test_control_lists_dates(self):
        self.stage("a.json")
        self.assertIn(CONTROL, self.listing() + "\n")

    def test_meeting_swapped_to_writerless_fifo_refuses_without_waiting(self):
        path = self.stage("a.json")
        with self.tracked() as (_, requests), self.swapped(path, self.to_fifo):
            self.refused(SAFE.format("a.json"))
        self.assertEqual(requests, [])

    def test_meeting_symlink_and_same_size_inode_substitutions_refuse_before_payload(self):
        outside = self.outside()
        path = self.stage("a.json")
        with self.tracked() as (_, requests), self.swapped(path, self.to_symlink(outside)):
            self.refused(SAFE.format("a.json"))
        self.assertNotIn(str(outside), SAFE.format("a.json"))
        path.unlink()
        path.with_name("a.json.saved").rename(path)
        # symlink to a writerless FIFO never waits
        fifo = self.tmp / "outside.fifo"
        os.mkfifo(fifo)
        with self.tracked() as (_, requests), self.swapped(path, self.to_symlink(fifo)):
            self.refused(SAFE.format("a.json"))
        path.unlink()
        path.with_name("a.json.saved").rename(path)

        def other_inode(victim):
            replacement = self.tmp / "same-size.json"
            replacement.write_bytes(victim.read_bytes().replace(b"2026-12-29", b"2026-11-18"))
            os.replace(replacement, victim)
        before = path.stat().st_size
        with self.tracked() as (fds, requests), self.swapped(path, other_inode):
            self.refused(SAFE.format("a.json"))
        self.assertEqual(path.stat().st_size, before)
        self.assertEqual(requests, [])
        for fd in fds:
            self.closed(fd)

    def test_static_links_and_nonregular_keep_old_wording(self):
        old = "meetings/a.json: must be a regular non-symlink file"
        path = self.folder / "a.json"
        path.symlink_to(self.outside())
        self.refused(old)
        path.unlink()
        path.symlink_to(self.tmp / "absent")
        self.refused(old)
        path.unlink()
        os.mkfifo(path)
        with Deadline():
            self.refused(old)
        path.unlink()
        path.mkdir()
        self.refused(old)

    def test_disappearing_leaf_refuses_but_missing_folder_is_allowed(self):
        path = self.stage("a.json")
        with self.swapped(path, lambda victim: victim.unlink()):
            self.refused(SAFE.format("a.json"))
        shutil.rmtree(self.folder)
        self.assertIn(CONTROL, self.listing() + "\n")


class ByteBoundTests(Base):
    def test_meeting_bytes_are_admitted_before_decode(self):
        path = self.folder / "a.json"
        for size, expect_ok in ((100, True), (MAX_JSON_BYTES, True), (MAX_JSON_BYTES + 1, False)):
            with self.subTest(size=size):
                path.write_bytes(padded(meeting("a", "2026-12-29T02:30:00Z"), size))
                with self.tracked() as (_, requests):
                    if expect_ok:
                        self.assertIn("agenda-watch=", self.listing())
                        self.assertEqual(requests, [(MAX_JSON_BYTES + 1,)])
                    else:
                        self.refused("meetings/a.json: a.json: file too large")
                        self.assertEqual(requests, [])  # static oversize: no payload read

    def test_utf8_bytes_not_characters_are_counted(self):
        pad = "\u00e9" * (MAX_JSON_BYTES // 2)
        data = ('{"id":"a","body":"' + BODY + '","starts_at":"' + NOV17 + '","pad":"' + pad + '"}').encode()
        self.assertLess(len(data.decode()), MAX_JSON_BYTES)
        self.assertGreater(len(data), MAX_JSON_BYTES)
        (self.folder / "a.json").write_bytes(data)
        self.refused("meetings/a.json: a.json: file too large")

    def test_growth_after_fstat_is_caught_by_the_bounded_read(self):
        path = self.stage("a.json")
        real_fstat, grown = os.fstat, []

        def growing(fd):
            info = real_fstat(fd)
            if not grown and stat.S_ISREG(info.st_mode) and os.path.samestat(info, path.lstat()):
                grown.append(1)
                path.write_bytes(padded(meeting("a"), MAX_JSON_BYTES + 5000))
            return info
        real_loads, decoded = json.loads, []

        def loads(text, *args, **kwargs):
            decoded.append(len(text))
            return real_loads(text, *args, **kwargs)
        with self.tracked() as (fds, requests), mock.patch.object(cmd.os, "fstat", side_effect=growing), \
                mock.patch.object(cmd, "load_meeting", side_effect=AssertionError("parsed")), \
                mock.patch.object(cmd.json, "loads", side_effect=loads):
            self.refused("meetings/a.json: a.json: file too large")
        self.assertLessEqual(max(decoded), MAX_JSON_BYTES)  # only the site file was decoded, never the grown meeting
        self.assertEqual(requests, [(MAX_JSON_BYTES + 1,)])
        self.closed(fds[0])

    def test_decode_and_validation_errors_keep_existing_wording(self):
        path = self.folder / "a.json"
        path.write_bytes(b"\xff")
        with self.assertRaises(cmd.BodyScheduleError) as caught:
            self.listing()
        self.assertTrue(str(caught.exception).startswith("meetings/a.json: a.json: not valid UTF-8 JSON ("))
        path.write_text("{}")
        with self.assertRaises(cmd.BodyScheduleError) as caught:
            self.listing()
        self.assertTrue(str(caught.exception).startswith("meetings/a.json: "))
        self.assertNotIn("cannot be read safely", str(caught.exception))


class DescriptorLifetimeTests(Base):
    def setUp(self):
        super().setUp()
        self.path = self.stage("a.json")
        self.admitted = self.path.lstat()
        self.sentinel = os.open(self.path, os.O_RDONLY)
        self.addCleanup(self.close_sentinel)

    def close_sentinel(self):
        try:
            os.close(self.sentinel)
        except OSError:
            pass

    def read(self):
        return cmd._read_meeting(self.path, self.admitted)

    def check_refusal(self, fds):
        self.assertEqual(len(fds), 1)
        self.closed(fds[0])
        os.fstat(self.sentinel)  # an unrelated live descriptor is untouched

    def test_io_failures_close_the_descriptor_and_hide_os_detail(self):
        real_fdopen = os.fdopen

        class Broken(RecordingStream):
            def read(self, *args):
                raise OSError(errno.EIO, "secret os detail /private/target")
        cases = {
            "fstat": mock.patch.object(cmd.os, "fstat", side_effect=OSError(errno.EIO, "secret os detail")),
            "fdopen": mock.patch.object(cmd.os, "fdopen", side_effect=OSError(errno.EMFILE, "secret os detail")),
            "read": mock.patch.object(cmd.os, "fdopen", side_effect=lambda *a, **k: Broken(real_fdopen(*a, **k), [])),
        }
        for label, patch in cases.items():
            with self.subTest(label), self.tracked() as (fds, _), patch:
                with self.assertRaises(cmd.BodyScheduleError) as caught:
                    self.read()
                self.assertEqual(str(caught.exception), SAFE.format("a.json"))
                self.assertIsNone(caught.exception.__cause__)
                self.assertNotIn("secret", str(caught.exception))
            self.check_refusal(fds)

    def test_admission_mismatch_and_nonregular_descriptors_close_the_descriptor(self):
        mismatched = os.stat_result((self.admitted.st_mode, self.admitted.st_ino + 1, self.admitted.st_dev,
                                     1, 0, 0, 1, 0, 0, 0))
        with self.tracked() as (fds, _):
            with self.assertRaises(cmd.BodyScheduleError):
                cmd._read_meeting(self.path, mismatched)
        self.check_refusal(fds)
        os.mkfifo(self.tmp / "p")
        fifo = self.folder / "p.json"
        os.rename(self.tmp / "p", fifo)
        with self.tracked() as (fds, _), Deadline():
            with self.assertRaises(cmd.BodyScheduleError):
                cmd._read_meeting(fifo, self.admitted)
        self.check_refusal(fds)

    def test_unexpected_exceptions_propagate_with_cleanup(self):
        with self.tracked() as (fds, _), mock.patch.object(cmd.os, "fstat", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.read()
        self.check_refusal(fds)

    def test_success_closes_the_descriptor(self):
        with self.tracked() as (fds, _):
            self.assertEqual(self.read()["id"], "a")
        self.check_refusal(fds)

    def test_unavailable_capability_refuses_without_opening(self):
        for name in ("O_NOFOLLOW", "O_NONBLOCK"):
            with self.subTest(name), mock.patch.object(cmd.os, name, None), \
                    mock.patch.object(cmd.os, "open", side_effect=AssertionError("opened")):
                with self.assertRaises(cmd.BodyScheduleError) as caught:
                    self.read()
                self.assertEqual(str(caught.exception), SAFE.format("a.json"))


class CliAndCompatibilityTests(Base):
    def setUp(self):
        super().setUp()
        clock = mock.patch.object(cmd, "date", wraps=date)
        clock.start().today.return_value = DAY
        self.addCleanup(clock.stop)

    def invoke(self, code=0):
        out, err = io.StringIO(), io.StringIO()
        argv = ["campaign_tool", "body-schedule", "list", "--directory", str(self.root)]
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                cli.main()
                actual = 0
            except SystemExit as exc:
                actual = exc.code
        self.assertEqual(actual, code, err.getvalue())
        return out.getvalue(), err.getvalue()

    def test_real_cli_refuses_entire_nonempty_listing(self):
        self.stage("a.json")
        (self.folder / "z.json").write_text("{")
        before = snapshot(self.root)
        out, err = self.invoke(code=2)
        self.assertEqual(out, "")
        self.assertIn("meetings/z.json: z.json: not valid UTF-8 JSON", err)
        self.assertNotIn("agenda-watch", err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(snapshot(self.root), before)

    def test_real_cli_swapped_leaf_exits_2_without_listing(self):
        outside = self.outside()
        path = self.stage("a.json")
        with self.swapped(path, self.to_symlink(outside)):
            out, err = self.invoke(code=2)
        self.assertEqual(out, "")
        self.assertIn(SAFE.format("a.json"), err)
        self.assertNotIn(str(outside), err)
        self.assertNotIn("Traceback", err)

    def test_healthy_agenda_watch_bytes_and_membership_are_unchanged(self):
        self.stage(".dot.json")  # a dot-prefixed match is still read
        (self.folder / "notes.txt").write_text("{")  # non-json entries are ignored
        expected = (f"{BODY} | weekday=tuesday; ordinals=1,3; local_start_time=18:30; "
                    f"time_zone=America/Los_Angeles | source_url={URL} | "
                    "checked_on=2026-10-01; age=39 days; limit=365 days; fresh | "
                    "agenda-watch=2026-11-17,2026-12-01,2026-12-15\n"
                    "Agenda-watch dates only; check the official source and stage only a verified meeting.\n")
        self.assertEqual(self.invoke()[0], expected)
        (self.folder / ".dot.json").write_text(json.dumps(meeting("dot", NOV17)))
        # a different body does not suppress
        (self.folder / "other.json").write_text(json.dumps(meeting("other", NOV17, "Another Body")))
        self.assertEqual(self.invoke()[0], expected.replace(CONTROL.strip(), SUPPRESSED.strip()))
        self.schedules(checked_on="2020-01-01")
        self.assertIn("stale | agenda-watch=2026-12-01", self.invoke()[0])

    def test_dangling_folder_and_empty_store_do_not_open_a_meeting(self):
        self.stage("a.json")
        (self.content / "body-schedules.json").write_text(json.dumps({"schema_version": 1, "schedules": []}))
        with mock.patch.object(cmd, "_read_meeting", side_effect=AssertionError("meeting opened")):
            self.assertEqual(self.invoke()[0], "No body schedules recorded.\n")
        self.schedules()
        shutil.rmtree(self.folder)
        self.folder.symlink_to(self.content, target_is_directory=True)
        self.assertEqual(self.invoke(code=2)[0], "")

    def test_listing_is_read_only(self):
        self.stage("a.json")
        write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | getattr(os, "O_DIRECTORY", 0)
        real_open = os.open

        def guard(path, flags, *args, **kwargs):
            if flags & write_flags:
                raise AssertionError("writer reached")
            return real_open(path, flags, *args, **kwargs)
        before = snapshot(self.root)
        with mock.patch.object(cmd.os, "open", side_effect=guard), \
                mock.patch.object(cmd.os, "fsync", side_effect=AssertionError("writer reached")):
            self.invoke()
            self.invoke()
            (self.folder / "bad.json").write_text("{")
            before = snapshot(self.root)
            self.invoke(code=2)
            self.invoke(code=2)
        self.assertEqual(snapshot(self.root), before)


if __name__ == "__main__":
    unittest.main()
