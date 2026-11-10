"""RF1025-3: `meetings-stage --participation FILE` admits and reads one regular-file descriptor, at most 64 KB.

Synthetic temp copies of the fictional example only; no network, nothing is sent, built or published.
Run from the repository root: python3 -B -m unittest tests.test_meeting_participation_read_safety -q
"""
import contextlib
import copy
import errno
import io
import json
import os
import shutil
import socket
import stat
import sys
import tempfile
import threading
import unittest
from contextlib import ExitStack
from datetime import date
from pathlib import Path
from unittest import mock

from campaign_tool import cli, meeting_stage
from campaign_tool.content import ContentError
from campaign_tool.meeting_stage import MeetingStageError, stage

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
MID = "legistar-examplecity-1001"
STARTS = "2026-10-14T18:00:00-07:00"
TODAY = date(2026, 10, 12)
LIMIT = 64 * 1024
REFUSAL = "--participation: not a regular UTF-8 JSON file up to 64 KB"
FULL = {"in_person": True,
        "remote": {"available": True, "how": "Zoom link on the posted agenda"},
        "written_comment": {"how": "Email the city clerk", "deadline": "2026-10-14T12:00:00-07:00"},
        "source_url": "https://example.invalid/council/agenda-2026-10-14",
        "verified_by": "Synthetic Organizer", "verified_at": "2026-10-10"}
SOURCE_ONLY = {"in_person": None, "remote": None, "written_comment": None,
               "source_url": "https://example.invalid/council/agenda", "verified_by": "Synthetic Organizer",
               "verified_at": "2026-10-10"}
REPLACEMENT = dict(FULL, verified_by="Replacement Canary")
REAL = {name: getattr(os, name) for name in ("lstat", "open", "fstat", "read", "close")}


def snapshot(folder):
    """bytes, mode and mtime (never atime) of every file below folder, plus directory membership."""
    result = {}
    for p in sorted(folder.rglob("*")):
        info = REAL["lstat"](p)
        result[str(p.relative_to(folder))] = (p.read_bytes() if stat.S_ISREG(info.st_mode) else None,
                                              stat.S_IMODE(info.st_mode), info.st_mtime_ns)
    return result


def append(path, data):
    fd = REAL["open"](path, os.O_WRONLY | os.O_APPEND)
    try:
        os.write(fd, data)
    finally:
        REAL["close"](fd)


class Spy:
    """Real lstat/open/fstat/read/close wrappers that act only on the one participation path and its descriptors."""

    def __init__(self, path):
        self.path = str(path)
        self.fds, self.live, self.closed, self.reads, self.opens = [], set(), [], [], 0
        self.after_lstat = self.after_open = self.after_fstat = None
        self.fstat_override = self.fstat_error = self.read_error = self.read_cap = None

    def _lstat(self, path, *args, **kwargs):
        result = REAL["lstat"](path, *args, **kwargs)
        if isinstance(path, (str, os.PathLike)) and os.fspath(path) == self.path and self.after_lstat:
            hook, self.after_lstat = self.after_lstat, None  # once
            hook()
        return result

    def _open(self, path, *args, **kwargs):
        fd = REAL["open"](path, *args, **kwargs)
        if isinstance(path, (str, os.PathLike)) and os.fspath(path) == self.path:
            self.opens += 1
            self.fds.append(fd)
            self.live.add(fd)
            if self.after_open:
                self.after_open()
        return fd

    def _fstat(self, fd, *args, **kwargs):
        result = REAL["fstat"](fd, *args, **kwargs)
        if fd in self.live:
            if self.fstat_error:
                raise self.fstat_error
            if self.after_fstat:
                hook, self.after_fstat = self.after_fstat, None
                hook()
            if self.fstat_override:
                return self.fstat_override(result)
        return result

    def _read(self, fd, size, *args, **kwargs):
        if fd not in self.live:
            return REAL["read"](fd, size, *args, **kwargs)
        if self.read_error:
            raise self.read_error
        data = REAL["read"](fd, min(size, self.read_cap) if self.read_cap else size, *args, **kwargs)
        self.reads.append((size, len(data)))
        return data

    def _close(self, fd):
        if fd in self.live:
            self.live.discard(fd)
            self.closed.append(fd)
        return REAL["close"](fd)

    @contextlib.contextmanager
    def patched(self):
        with ExitStack() as stack:
            for name in ("lstat", "open", "fstat", "read", "close"):
                stack.enter_context(mock.patch.object(os, name, getattr(self, "_" + name)))
            yield self

    @property
    def returned(self):
        return sum(n for _, n in self.reads)


def special_mode(mode):
    def override(result):
        return os.stat_result((mode,) + tuple(result)[1:10])
    return override


def padded(total, text=None):
    """A valid participation document of exactly `total` UTF-8 bytes (trailing JSON whitespace)."""
    raw = json.dumps(dict(FULL, verified_by=text or FULL["verified_by"]), ensure_ascii=False).encode("utf-8")
    assert len(raw) <= total
    return raw + b" " * (total - len(raw))


class ParticipationReadSafetyTests(unittest.TestCase):
    def setUp(self):
        for patcher in (mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used")),
                        mock.patch.object(socket.socket, "connect", side_effect=AssertionError("socket used")),
                        mock.patch("campaign_tool.site.build_site", side_effect=AssertionError("build used"))):
            patcher.start()
            self.addCleanup(patcher.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-participation-read-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name).resolve()
        self.root = self.tmp / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.meetings = self.root / "content" / "meetings"
        self.target = self.meetings / f"{MID}.json"
        (self.root / "kit").mkdir(exist_ok=True)
        (self.root / "kit" / "meetings.json").write_text(json.dumps({"schema_version": 1, "meetings": [
            {"id": MID, "body": "Example City Council", "starts_at": STARTS, "agenda_item": "Item 7: ALPR",
             "agenda_url": "https://example.invalid/agenda"}]}), encoding="utf-8")
        self.input = self.tmp / "participation.json"
        self.input.write_bytes(json.dumps(FULL).encode("utf-8"))

    def stage(self, **kwargs):
        return stage(self.root, MID, participation=str(self.input), today=TODAY, **kwargs)

    def put(self, data, name="participation.json"):
        path = self.tmp / name
        path.write_bytes(data)
        return path

    def assert_refused_without_write(self, spy=None, **kwargs):
        before = snapshot(self.root)
        with mock.patch.object(meeting_stage, "load_participation", side_effect=AssertionError("validated")):
            with self.assertRaises(MeetingStageError) as caught:
                self.stage(**kwargs)
        self.assertEqual(str(caught.exception), REFUSAL)
        self.assertIsNone(caught.exception.__cause__)
        self.assertEqual(snapshot(self.root), before)
        return caught.exception

    def swap_to_symlink(self, destination):
        def hook():
            os.unlink(self.input)
            os.symlink(destination, self.input)
        return hook

    def test_growing_participation_after_real_fstat_refuses_before_stage_write(self):
        spy = Spy(self.input)
        spy.after_fstat = lambda: append(self.input, b" " * LIMIT)  # genuine small-file metadata, then growth
        with spy.patched():
            self.assert_refused_without_write()
        self.assertTrue(spy.reads)
        self.assertTrue(all(requested <= LIMIT + 1 for requested, _ in spy.reads))
        self.assertEqual(spy.returned, LIMIT + 1)  # stopped at limit+1, not the whole grown file
        self.assertFalse(self.target.exists())

    def test_last_component_swap_to_symlink_or_different_inode_refuses(self):
        replacement = self.put(json.dumps(REPLACEMENT).encode("utf-8"), "replacement.json")
        replacement_bytes = replacement.read_bytes()

        def other_inode():
            temp = self.put(replacement_bytes, "other-inode.json")
            os.replace(temp, self.input)
        cases = {"symlink": self.swap_to_symlink(replacement),
                 "dangling": self.swap_to_symlink(self.tmp / "absent.json"),
                 "inode": other_inode}
        for name, hook in cases.items():
            with self.subTest(case=name):
                self.fresh_input()
                spy = Spy(self.input)
                spy.after_lstat = hook
                with spy.patched():
                    error = self.assert_refused_without_write()
                self.assertNotIn("Replacement Canary", str(error))
                self.assertEqual(spy.returned, 0)  # nothing from the replacement was ever read
                self.assertEqual(replacement.read_bytes(), replacement_bytes)
                self.assertFalse(self.target.exists())

    def fresh_input(self, data=None):
        """A regular input file again, never written through a symlink left by a previous case."""
        if os.path.lexists(self.input):
            if self.input.is_dir() and not self.input.is_symlink():
                self.input.rmdir()
            else:
                os.unlink(self.input)
        self.input.write_bytes(data if data is not None else json.dumps(FULL).encode("utf-8"))

    def run_bounded(self, call, seconds=10):
        outcome = []

        def target():
            try:
                call()
            except BaseException as exc:  # noqa: BLE001 - reported to the test thread
                outcome.append(exc)
        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        thread.join(seconds)
        self.assertFalse(thread.is_alive(), "reader blocked on a special file")
        return outcome

    def test_special_file_refused_without_payload_read_or_wait(self):
        os.unlink(self.input)
        os.mkfifo(self.input)  # static FIFO with no producer
        spy = Spy(self.input)
        with spy.patched():
            outcome = self.run_bounded(self.stage)
        self.assertEqual([str(e) for e in outcome], [REFUSAL])
        self.assertEqual(spy.reads, [])
        os.unlink(self.input)

        def to_fifo():
            os.unlink(self.input)
            os.mkfifo(self.input)

        def to_directory():
            os.unlink(self.input)
            os.mkdir(self.input)
        for name, hook in (("fifo", to_fifo), ("directory", to_directory)):  # substituted after admission
            with self.subTest(substituted=name):
                self.fresh_input()
                spy = Spy(self.input)
                spy.after_lstat = hook
                with spy.patched():
                    outcome = self.run_bounded(self.stage)
                self.assertEqual([str(e) for e in outcome], [REFUSAL])
                self.assertEqual(spy.reads, [])
                self.assertEqual(spy.opens, 1)
                self.assertEqual(spy.closed, spy.fds)
                self.assertFalse(self.target.exists())
        self.fresh_input()
        for mode in (stat.S_IFSOCK, stat.S_IFCHR, stat.S_IFBLK, stat.S_IFIFO, stat.S_IFDIR):
            with self.subTest(fstat_mode=oct(mode)):
                spy = Spy(self.input)
                spy.fstat_override = special_mode(mode | 0o600)
                with spy.patched():
                    self.assert_refused_without_write()
                self.assertEqual(spy.reads, [])
        with self.assertRaises(MeetingStageError):  # a static directory is refused before any open
            stage(self.root, MID, participation=str(self.tmp), today=TODAY)

    def test_exact_cap_and_cap_plus_one_use_actual_utf8_bytes(self):
        multibyte = "Synth\u00e9tic Organizer"
        for total, ok in ((LIMIT - 1, True), (LIMIT, True), (LIMIT + 1, False)):
            with self.subTest(total=total):
                data = padded(total, multibyte)
                self.assertLess(len(data.decode("utf-8")), len(data))  # characters are not bytes
                self.input.write_bytes(data)
                if ok:
                    self.stage(replace=True)
                    document = json.loads(self.target.read_text(encoding="utf-8"))
                    self.assertEqual(document["participation"]["verified_by"], multibyte)
                else:
                    spy = Spy(self.input)
                    with spy.patched():
                        self.assert_refused_without_write(replace=True)
                    self.assertEqual(spy.reads, [])  # known oversized: refused before any payload read
        self.input.write_bytes(padded(LIMIT))  # ordinary short reads run to EOF
        spy = Spy(self.input)
        spy.read_cap = 1000
        with spy.patched():
            self.stage(replace=True)
        self.assertGreater(len(spy.reads), LIMIT // 1000)
        self.assertEqual(spy.returned, LIMIT)
        self.assertEqual(spy.reads[-1][1], 0)  # terminated at EOF
        self.input.write_bytes(json.dumps(FULL).encode("utf-8"))  # real-file control
        self.stage(replace=True)
        self.assertEqual(json.loads(self.target.read_text(encoding="utf-8"))["participation"], FULL)

    def test_opened_input_never_reopens_payload(self):
        replacement = self.put(json.dumps(REPLACEMENT).encode("utf-8"), "replacement.json")
        spy = Spy(self.input)
        spy.after_fstat = self.swap_to_symlink(replacement)  # pathname redirected after the descriptor was admitted
        with spy.patched():
            self.stage()
        self.assertEqual(spy.opens, 1)
        self.assertEqual(json.loads(self.target.read_text(encoding="utf-8"))["participation"]["verified_by"],
                         "Synthetic Organizer")

    def test_descriptors_close_on_every_acquired_path(self):
        sentinel = REAL["open"](os.devnull, os.O_RDONLY)
        self.addCleanup(self.close_quietly, sentinel)
        scenarios = {
            "success": (json.dumps(FULL).encode("utf-8"), lambda spy: None, False),
            "short-reads": (json.dumps(FULL).encode("utf-8"), lambda spy: setattr(spy, "read_cap", 7), False),
            "fstat": (json.dumps(FULL).encode("utf-8"), lambda spy: setattr(spy, "fstat_error", OSError(errno.EIO, "x")), True),
            "read": (json.dumps(FULL).encode("utf-8"), lambda spy: setattr(spy, "read_error", OSError(errno.EIO, "x")), True),
            "utf8": (b"\xff\xfe not utf-8", lambda spy: None, True),
            "json": (b"{not json", lambda spy: None, True),
            "oversize": (padded(LIMIT + 1), lambda spy: None, True),
        }
        for name, (data, configure, refused) in scenarios.items():
            for attempt in (1, 2):
                with self.subTest(case=name, attempt=attempt):
                    self.fresh_input(data)
                    spy = Spy(self.input)
                    configure(spy)
                    with spy.patched():
                        if refused:
                            with self.assertRaises(MeetingStageError) as caught:
                                self.stage(replace=True)
                            self.assertEqual(str(caught.exception), REFUSAL)
                        else:
                            self.stage(replace=True)
                    self.assertEqual(len(spy.fds), 1)
                    self.assertEqual(spy.closed, spy.fds)
                    with self.assertRaises(OSError):
                        os.fstat(spy.fds[0])
                    os.fstat(sentinel)  # a descriptor the reader never acquired stays open
        with self.subTest(case="open-failure"):  # no descriptor was acquired, so none is closed
            self.fresh_input()
            spy = Spy(self.input)
            spy.after_lstat = self.swap_to_symlink(self.tmp / "absent.json")
            with spy.patched():
                with self.assertRaises(MeetingStageError):
                    self.stage(replace=True)
            self.assertEqual((spy.fds, spy.closed), ([], []))
            os.fstat(sentinel)

    @staticmethod
    def close_quietly(fd):
        with contextlib.suppress(OSError):
            REAL["close"](fd)

    def cli(self, *extra, participation=None):
        argv = ["campaign_tool", "meetings-stage", "--directory", str(self.root), "--id", MID,
                "--participation", str(participation or self.input), *extra]
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                cli.main()
            except SystemExit as exit_:
                return exit_.code, out.getvalue(), err.getvalue()
        return 0, out.getvalue(), err.getvalue()

    def test_refusal_preserves_first_and_existing_output_and_cli_privacy(self):
        canary = dict(FULL, verified_by="Canary Verifier", remote={"available": True, "how": "canary-how-text"})
        self.input = self.put(json.dumps(canary).encode("utf-8"), "canary-private-name.json")
        for replace in (False, True):
            with self.subTest(replace=replace):
                if replace:
                    stage(self.root, MID, today=TODAY)  # an existing staged meeting to protect
                    stage_args = ("--replace",)
                else:
                    stage_args = ()
                before = snapshot(self.root)
                before_input = self.input.read_bytes(), stat.S_IMODE(self.input.stat().st_mode)
                spy = Spy(self.input)
                spy.after_fstat = lambda: append(self.input, b" " * LIMIT)
                with spy.patched():
                    code, out, err = self.cli(*stage_args)
                self.assertEqual((code, out), (1, ""))
                self.assertEqual(err, f"Stopped: {REFUSAL}\n")
                for private in (str(self.tmp), "canary", "Canary", "Traceback", "example.invalid"):
                    self.assertNotIn(private, err)
                self.assertEqual(snapshot(self.root), before)
                self.assertEqual(self.target.exists(), replace)
                self.assertEqual(self.input.read_bytes()[:len(before_input[0])], before_input[0])
                self.assertEqual(stat.S_IMODE(self.input.stat().st_mode), before_input[1])
                self.input.write_bytes(before_input[0])  # restore for the next case
        self.assertEqual(self.cli(participation=self.tmp)[0], 1)
        # ordinary post-decode schema refusals keep their own field-specific diagnostics
        bad = self.put(json.dumps(dict(FULL, surprise=1)).encode("utf-8"), "unknown-key.json")
        with self.assertRaises(ContentError):
            stage(self.root, MID, participation=str(bad), today=TODAY, replace=True)

    def test_successful_legacy_bytes_and_no_flag_policy_unchanged(self):
        spaced = copy.deepcopy(FULL)
        spaced["remote"]["how"] = "  Zoom link on the posted agenda \n"
        spaced["written_comment"]["how"] = "\tEmail the city clerk  "
        expected = {"full": (FULL, FULL), "spaced": (spaced, FULL), "source-only": (SOURCE_ONLY, SOURCE_ONLY)}
        for name, (given, wanted) in expected.items():
            with self.subTest(channel=name):
                self.input.write_bytes(json.dumps(given).encode("utf-8"))
                report = self.stage(replace=True)
                document = json.loads(self.target.read_text(encoding="utf-8"))
                self.assertEqual(document["participation"], wanted)
                self.assertEqual(report["written"], f"content/meetings/{MID}.json")
                section = document["comment_kit_md"].split("## How to take part\n", 1)[1].split("\n\n## Sources", 1)[0]
                self.assertIn("Source: " + wanted["source_url"], section)
                self.assertEqual("In person" in section, wanted["in_person"] is not None)
        stage(self.root, MID, today=TODAY, replace=True)  # no flag: participation is omitted and the old one dropped
        document = json.loads(self.target.read_text(encoding="utf-8"))
        self.assertNotIn("participation", document)
        self.assertIn("participation details were not verified", document["comment_kit_md"])


if __name__ == "__main__":
    unittest.main()
