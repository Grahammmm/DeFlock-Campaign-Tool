"""RF0010-2: content/elsewhere.json is admitted through one no-follow, nonblocking, fstat-checked descriptor.

Synthetic only: temp copies of examples/fictional-campaign, reserved .test hosts, no network. The leaf is swapped for a
symlink/FIFO/directory exactly between the reader's pathname checks and its acquisition, using real filesystem calls.
"""
import contextlib
import json
import os
import shutil
import signal
import stat
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from campaign_tool import content_elsewhere
from campaign_tool.content import ContentError, load_content
from campaign_tool.content_check import content_problems
from campaign_tool.content_elsewhere import read_elsewhere

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
DAY = date(2026, 10, 1)
CANARY = "SUBSTITUTED-EXTERNAL-CANARY"
UNREADABLE = r"^elsewhere\.json: could not be read$"
NONREGULAR = r"^elsewhere\.json: must be a regular file, not a symlink or folder$"


def row(place):
    return {"place": place, "state": "OR", "body": "Springfield City Council", "decided_on": "2026-03-10",
            "kind": "paused", "as_stated": "The council continued the contract.",
            "source_url": "https://records.example.test/minutes/1", "checked_on": "2026-09-15"}


class Case(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-elsewhere-admission-")
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        self.root = base / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.path = self.root / "content" / "elsewhere.json"
        self.path.write_text(json.dumps([row("Springfield")]), encoding="utf-8")
        self.external = base / "external.json"
        self.external.write_text(json.dumps([row(CANARY)]), encoding="utf-8")
        self.external_bytes = self.external.read_bytes()
        for target in ("socket.socket", "urllib.request.urlopen"):
            guard = mock.patch(target, side_effect=AssertionError("network access attempted"))
            guard.start()
            self.addCleanup(guard.stop)

    @contextlib.contextmanager
    def swap_before_open(self, replace):
        """Run ``replace()`` once, just before the reader's real ``os.open`` of the leaf; yields the requested flags."""
        real_open, seen = os.open, []

        def opening(target, flag, *args, **kwargs):
            if Path(target) == self.path and not seen:
                seen.append(flag)
                replace()
            return real_open(target, flag, *args, **kwargs)

        with mock.patch.object(os, "open", opening):
            yield seen

    def link_to_external(self):
        self.path.unlink()
        self.path.symlink_to(self.external)

    def open_fds(self):
        return set(os.listdir("/proc/self/fd"))


class LateSubstitution(Case):
    def test_late_symlink_is_refused_without_reading_target(self):
        for label in ("valid", "malformed", "dangling"):
            with self.subTest(case=label):
                self.path.unlink(missing_ok=True)
                self.path.write_text(json.dumps([row("Springfield")]), encoding="utf-8")
                if label == "malformed":
                    self.external.write_bytes(b"{not json")
                before = self.external.read_bytes()
                dangling = self.external.parent / "missing.json"

                def replace():
                    self.path.unlink()
                    self.path.symlink_to(dangling if label == "dangling" else self.external)

                with self.swap_before_open(replace) as flags:
                    with self.assertRaisesRegex(ContentError, UNREADABLE) as caught:
                        read_elsewhere(self.root, DAY)
                self.assertTrue(flags[0] & os.O_NOFOLLOW and flags[0] & os.O_NONBLOCK)
                self.assertNotIn(CANARY, str(caught.exception))
                self.assertNotIn(str(self.root), str(caught.exception))
                self.assertTrue(self.path.is_symlink())
                self.assertEqual(self.external.read_bytes(), before)

    def test_collecting_form_appends_one_safe_problem(self):
        problems = []
        with self.swap_before_open(self.link_to_external):
            self.assertIsNone(read_elsewhere(self.root, DAY, problems=problems))
        self.assertEqual(problems, ["elsewhere.json: could not be read"])
        self.assertEqual(self.external.read_bytes(), self.external_bytes)

    def test_model_and_checker_use_safe_admission(self):
        with self.swap_before_open(self.link_to_external):
            with self.assertRaisesRegex(ContentError, UNREADABLE):
                load_content(self.root, today=DAY)
        self.path.unlink()
        self.path.write_text(json.dumps([row("Springfield")]), encoding="utf-8")
        with self.swap_before_open(self.link_to_external):
            problems = content_problems(self.root, today=DAY)
        self.assertEqual([p for p in problems if "elsewhere.json" in p], ["elsewhere.json: could not be read"])
        self.assertEqual(self.external.read_bytes(), self.external_bytes)

    def test_late_fifo_and_directory_refuse_actual_descriptor_type(self):
        def timeout(*_):
            raise AssertionError("open blocked on a substituted FIFO")

        previous = signal.signal(signal.SIGALRM, timeout)
        self.addCleanup(signal.signal, signal.SIGALRM, previous)
        for label in ("fifo", "directory"):
            with self.subTest(case=label):
                self.path.unlink(missing_ok=True)
                if self.path.is_dir():
                    self.path.rmdir()
                self.path.write_text("[]", encoding="utf-8")

                def replace():
                    self.path.unlink()
                    os.mkfifo(self.path) if label == "fifo" else self.path.mkdir()

                before = self.open_fds()
                signal.alarm(10)
                try:
                    with self.swap_before_open(replace):
                        with self.assertRaisesRegex(ContentError, NONREGULAR):
                            read_elsewhere(self.root, DAY)
                finally:
                    signal.alarm(0)
                self.assertEqual(self.open_fds(), before)
                if label == "fifo":
                    self.path.unlink()
                else:
                    self.path.rmdir()

    def test_static_refusals_keep_their_categories(self):
        self.path.unlink()
        self.assertIsNone(read_elsewhere(self.root, DAY))
        self.path.symlink_to(self.external)
        with self.assertRaisesRegex(ContentError, "symlinks are not read"):
            read_elsewhere(self.root, DAY)
        self.path.unlink()
        self.path.mkdir()
        with self.assertRaisesRegex(ContentError, "must be a regular file"):
            read_elsewhere(self.root, DAY)

    def test_opened_regular_descriptor_is_the_only_payload_source(self):
        real_fstat, replaced = os.fstat, []

        def fstat(descriptor):
            if not replaced:
                replaced.append(True)
                self.path.rename(self.path.with_name("moved.json"))
                self.path.symlink_to(self.external)
            return real_fstat(descriptor)

        with mock.patch.object(os, "fstat", fstat):
            entries = read_elsewhere(self.root, DAY)
        self.assertEqual([e["place"] for e in entries or []], ["Springfield"])
        self.assertEqual(self.external.read_bytes(), self.external_bytes)


class Descriptors(Case):
    def test_raw_descriptor_closes_on_fstat_fdopen_and_nonregular_failures(self):
        sentinel = os.open(os.devnull, os.O_RDONLY)
        self.addCleanup(os.close, sentinel)
        before = self.open_fds()
        with mock.patch.object(os, "fstat", side_effect=OSError("boom")):
            with self.assertRaisesRegex(ContentError, UNREADABLE):
                read_elsewhere(self.root, DAY)
        self.assertEqual(self.open_fds(), before)
        with mock.patch.object(os, "fdopen", side_effect=OSError("boom")):
            with self.assertRaisesRegex(ContentError, UNREADABLE):
                read_elsewhere(self.root, DAY)
        self.assertEqual(self.open_fds(), before)
        with mock.patch.object(os, "fdopen", side_effect=RuntimeError("bug")):
            with self.assertRaises(RuntimeError):
                read_elsewhere(self.root, DAY)
        self.assertEqual(self.open_fds(), before)
        with mock.patch.object(content_elsewhere.stat, "S_ISREG", return_value=False):
            with self.assertRaisesRegex(ContentError, NONREGULAR):
                read_elsewhere(self.root, DAY)
        self.assertEqual(self.open_fds(), before)
        os.fstat(sentinel)

    def test_stream_closes_for_decode_and_size_failures(self):
        before = self.open_fds()
        for payload in (b"{not json", b"[\xff]", b'[{"a": 1, "a": 1}]', b"[" * 100000,
                        b" " * (content_elsewhere.MAX_JSON_BYTES + 1)):
            self.path.write_bytes(payload)
            with self.assertRaises(ContentError):
                read_elsewhere(self.root, DAY)
            self.assertEqual(self.open_fds(), before)

    def test_regular_file_is_still_accepted_with_original_order(self):
        self.path.write_text(json.dumps([row("B"), row("A")]), encoding="utf-8")
        self.assertEqual([e["place"] for e in read_elsewhere(self.root, DAY) or []], ["B", "A"])
        self.assertTrue(stat.S_ISREG(self.path.stat().st_mode))


if __name__ == "__main__":
    unittest.main()
