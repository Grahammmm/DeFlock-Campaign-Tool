"""RF0844-1: the four meeting-outcome writers (creation, minutes-only, direction-only, counts-only) refuse a symlink at
campaign-relative ``content`` or ``content/meetings`` before reading staged meetings or writing a replacement.

Synthetic content only (temp copies of the fictional example, injected ``today``); nothing is fetched or published.
Run from the repository root: python3 -B -m unittest tests.test_meeting_outcome_parent_links -v
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from campaign_tool import meeting_outcome
from campaign_tool.meeting_outcome import (MeetingOutcomeError, record_comment_counts, record_direction, record_minutes,
                                           record_outcome)

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
MID = "cedar-council-2026-10"  # starts_at 2026-10-14T18:00:00-07:00
STEM = "2026-10-cedar-council"
TODAY = date(2026, 10, 20)
SOURCE = "https://example.invalid/cedar-council/minutes"
MINUTES = "https://example.invalid/cedar-council/approved-minutes"
DIRECTION = "Staff to return with a fictional report."
CREATE = ("--result", "continued", "--on", "2026-10-14", "--source", SOURCE, "--verified-by", "Synthetic Organizer")

OPERATIONS = {  # mode: (starts with a recorded outcome, library call, CLI flags)
    "outcome": (False, lambda root, **kw: record_outcome(root, MID, "continued", "2026-10-14", SOURCE, "Synthetic Organizer",
                                                          "4-1", today=TODAY, **kw), CREATE),
    "minutes": (True, lambda root, **kw: record_minutes(root, MID, MINUTES, "2026-10-20", today=TODAY, **kw),
                ("--minutes-url", MINUTES, "--minutes-approved-on", "2026-10-20")),
    "direction": (True, lambda root, **kw: record_direction(root, MID, DIRECTION, "2026-11-01", today=TODAY, **kw),
                  ("--direction", DIRECTION, "--return-by", "2026-11-01")),
    "counts": (True, lambda root, **kw: record_comment_counts(root, MID, spoke=3, written=2, today=TODAY, **kw),
               ("--spoke", "3", "--written", "2")),
}
COMMENT_TEXT = "Two minutes, one ask.\n"
SUBMITTED = {"on": "2026-10-10", "channel": "email", "text": COMMENT_TEXT,
             "sha256": hashlib.sha256(COMMENT_TEXT.encode("utf-8")).hexdigest()}


def snapshot(base):
    """Every entry under ``base`` (links not followed): kind, mode, and bytes digest or link text."""
    seen = {}

    def walk(directory):
        for entry in sorted(os.scandir(directory), key=lambda e: e.name):
            info = entry.stat(follow_symlinks=False)
            relative = os.path.relpath(entry.path, base)
            if entry.is_symlink():
                seen[relative] = ("link", os.readlink(entry.path))
            elif entry.is_dir(follow_symlinks=False):
                seen[relative] = ("dir", info.st_mode)
                walk(entry.path)
            else:
                seen[relative] = ("file", info.st_mode, hashlib.sha256(Path(entry.path).read_bytes()).hexdigest())

    walk(base)
    return seen


def run_cli(root, *flags):
    return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "meetings-outcome", "--directory", str(root),
                           "--id", MID, *flags], cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.templates = tempfile.TemporaryDirectory(prefix="synthetic-outcome-templates-")
        base = Path(cls.templates.name)
        for recorded in (False, True):
            root = base / ("recorded" if recorded else "fresh")
            shutil.copytree(EXAMPLE, root, ignore=shutil.ignore_patterns("public"))
            document = root / "content" / "meetings" / f"{STEM}.json"
            stored = json.loads(document.read_text(encoding="utf-8"))
            document.write_text(json.dumps({**stored, "submitted_comment": SUBMITTED}), encoding="utf-8")
            if recorded:
                record_outcome(root, MID, "continued", "2026-10-14", SOURCE, "Synthetic Organizer", "4-1", today=TODAY)
        cls.base = base

    @classmethod
    def tearDownClass(cls):
        cls.templates.cleanup()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-outcome-links-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)

    def campaign(self, mode, name=None):
        """A fresh private copy of the fictional campaign for ``mode``; returns its root."""
        root = self.tmp / (name or mode) / "campaign"
        shutil.copytree(self.base / ("recorded" if OPERATIONS[mode][0] else "fresh"), root)
        (root / "content" / "meetings" / f"{STEM}.json").chmod(0o644)
        return root

    def link_content(self, root, kind="external"):
        """Replace campaign ``content`` with a symlink; returns the tree a link to a real directory would write into."""
        content, outside = root / "content", root.parent / "outside-content"
        if kind == "internal":
            outside = root / "real-content"
        content.rename(outside)
        if kind == "missing":
            shutil.rmtree(outside)
            content.symlink_to(root.parent / "nowhere")
        elif kind == "file":
            shutil.rmtree(outside)
            outside = root.parent / "outside-file"
            outside.write_text("{}", encoding="utf-8")
            content.symlink_to(outside)
        else:
            content.symlink_to(outside)
        return outside

    def link_meetings(self, root, kind="external"):
        meetings, outside = root / "content" / "meetings", root.parent / "outside-meetings"
        if kind == "internal":
            outside = root / "content" / "real-meetings"
        meetings.rename(outside)
        if kind == "missing":
            shutil.rmtree(outside)
            meetings.symlink_to(root.parent / "nowhere")
        elif kind == "file":
            shutil.rmtree(outside)
            outside = root.parent / "outside-file"
            outside.write_text("{}", encoding="utf-8")
            meetings.symlink_to(outside)
        else:
            meetings.symlink_to(outside)
        return outside

    def refused(self, name, mode, root, needle):
        """The library call raises ``needle`` and changes nothing under ``tmp/<name>`` (the campaign and any link target)."""
        before = snapshot(self.tmp / name)
        with self.assertRaises(MeetingOutcomeError) as caught:
            OPERATIONS[mode][1](root)
        self.assertIn(needle, str(caught.exception))
        self.assertNotIn(str(self.tmp), str(caught.exception))
        self.assertEqual(snapshot(self.tmp / name), before, f"{name}: a refusal changed files")
        return str(caught.exception)


class ParentLinkTests(Base):
    def test_all_outcome_modes_refuse_linked_content_before_read(self):
        for mode in OPERATIONS:
            with self.subTest(mode=mode):
                root = self.campaign(mode)
                outside = self.link_content(root)
                external = outside / "meetings" / f"{STEM}.json"
                self.assertEqual(external.stat().st_mode & 0o777, 0o644)
                with mock.patch.object(meeting_outcome, "_read_json", side_effect=AssertionError("read")) as read, \
                        mock.patch.object(meeting_outcome, "load_meeting", side_effect=AssertionError("load")), \
                        mock.patch.object(meeting_outcome, "_write_json", side_effect=AssertionError("write")) as write, \
                        mock.patch.object(Path, "glob", side_effect=AssertionError("listed")):
                    self.refused(mode, mode, root, "content must not be a symlink")
                read.assert_not_called()
                write.assert_not_called()
                self.assertEqual(external.stat().st_mode & 0o777, 0o644)
                self.assertFalse((root / "public").exists())

    def test_linked_meetings_and_dangling_or_internal_aliases_are_refused(self):
        for mode in OPERATIONS:
            for position, link, needle in (("content", self.link_content, "content must not be a symlink"),
                                           ("meetings", self.link_meetings, "content/meetings must not be a symlink")):
                for kind in ("external", "internal", "missing", "file"):
                    with self.subTest(mode=mode, position=position, kind=kind):
                        root = self.campaign(mode, f"{mode}-{position}-{kind}")
                        link(root, kind)
                        with mock.patch.object(meeting_outcome, "_read_json", side_effect=AssertionError("read")), \
                                mock.patch.object(Path, "glob", side_effect=AssertionError("listed")):
                            self.refused(f"{mode}-{position}-{kind}", mode, root, needle)

    def test_continuation_check_refuses_linked_parents_before_listing(self):
        root = self.campaign("outcome")
        meeting = {"id": MID, "outcome": {"continued_to": "nowhere"}}
        path = root / "content" / "meetings" / f"{STEM}.json"
        outside = self.link_meetings(root)
        before = snapshot(outside)
        with mock.patch.object(Path, "glob", side_effect=AssertionError("listed")):
            with self.assertRaisesRegex(MeetingOutcomeError, "content/meetings must not be a symlink"):
                meeting_outcome._check_continuation(root, path, meeting, TODAY)
        self.assertEqual(snapshot(outside), before)

    def test_nonfolder_and_missing_parent_behavior_is_controlled(self):
        for mode in OPERATIONS:
            with self.subTest(mode=mode, case="content is a file"):
                root = self.campaign(mode, f"{mode}-contentfile")
                shutil.rmtree(root / "content")
                (root / "content").write_text("not a folder", encoding="utf-8")
                self.refused(f"{mode}-contentfile", mode, root, "content must be a directory")
            with self.subTest(mode=mode, case="meetings is a file"):
                root = self.campaign(mode, f"{mode}-meetingsfile")
                shutil.rmtree(root / "content" / "meetings")
                (root / "content" / "meetings").write_text("not a folder", encoding="utf-8")
                self.refused(f"{mode}-meetingsfile", mode, root, "content/meetings must be a directory")
            with self.subTest(mode=mode, case="meetings missing"):
                root = self.campaign(mode, f"{mode}-nomeetings")
                shutil.rmtree(root / "content" / "meetings")
                self.refused(f"{mode}-nomeetings", mode, root, f"no staged meeting {MID}")
                self.assertFalse((root / "content" / "meetings").exists())
            with self.subTest(mode=mode, case="content missing"):
                root = self.campaign(mode, f"{mode}-nocontent")
                shutil.rmtree(root / "content")
                self.refused(f"{mode}-nocontent", mode, root, f"no staged meeting {MID}")
                self.assertFalse((root / "content").exists())
            with self.subTest(mode=mode, case="final file is a link"):
                root = self.campaign(mode, f"{mode}-finallink")
                final = root / "content" / "meetings" / f"{STEM}.json"
                real = root.parent / "outside-final.json"
                final.rename(real)
                final.symlink_to(real)
                with self.assertRaisesRegex(MeetingOutcomeError, "must not be a symlink"):
                    {"outcome": lambda: record_outcome(root, STEM, "continued", "2026-10-14", SOURCE, "Synthetic Organizer",
                                                       "4-1", today=TODAY),
                     "minutes": lambda: record_minutes(root, STEM, MINUTES, "2026-10-20", today=TODAY),
                     "direction": lambda: record_direction(root, STEM, DIRECTION, "2026-11-01", today=TODAY),
                     "counts": lambda: record_comment_counts(root, STEM, spoke=3, today=TODAY)}[mode]()
        root = self.campaign("outcome", "unknown")
        before = snapshot(root)
        with self.assertRaisesRegex(MeetingOutcomeError, "no staged meeting nope in content/meetings/"):
            record_outcome(root, "nope", "continued", "2026-10-14", SOURCE, "Synthetic Organizer", today=TODAY)
        self.assertEqual(snapshot(root), before)

    def test_parent_substituted_during_validation_blocks_every_writer(self):
        for mode in OPERATIONS:
            for position in ("content", "meetings"):
                with self.subTest(mode=mode, position=position):
                    root = self.campaign(mode, f"{mode}-{position}-swap")
                    parent = root / ("content" if position == "content" else "content/meetings")
                    moved = parent.parent / f"moved-{position}"
                    sentinel = root.parent / "sentinel" / position
                    shutil.copytree(parent, sentinel, symlinks=True)
                    original, sentinel_before = snapshot(parent), snapshot(sentinel)
                    calls, real_load = [], meeting_outcome.load_meeting

                    def swapping_load(*args, **kwargs):
                        calls.append(args[1])
                        if len(calls) == 2:  # after the initial real load, during prospective-document validation
                            parent.rename(moved)
                            parent.symlink_to(sentinel)
                        return real_load(*args, **kwargs)

                    with mock.patch.object(meeting_outcome, "load_meeting", swapping_load), \
                            mock.patch.object(meeting_outcome, "_write_json", side_effect=AssertionError("written")) as write:
                        with self.assertRaisesRegex(MeetingOutcomeError, "must not be a symlink"):
                            OPERATIONS[mode][1](root)
                    write.assert_not_called()
                    self.assertGreaterEqual(len(calls), 2)
                    self.assertTrue(parent.is_symlink())
                    self.assertEqual(snapshot(moved), original, "the original tree changed")
                    self.assertEqual(snapshot(sentinel), sentinel_before, "the sentinel tree changed")

    def test_real_cli_modes_refuse_parent_link_without_traceback(self):
        for mode, (_, _, flags) in OPERATIONS.items():
            for position, link, needle in (("content", self.link_content, "Stopped: content must not be a symlink"),
                                           ("meetings", self.link_meetings, "Stopped: content/meetings must not be a symlink")):
                with self.subTest(mode=mode, position=position):
                    root = self.campaign(mode, f"{mode}-{position}-cli")
                    outside = link(root)
                    before = [snapshot(self.tmp / f"{mode}-{position}-cli"), snapshot(outside)]
                    done = run_cli(root, *flags)
                    self.assertEqual(done.returncode, 1, done.stderr)
                    self.assertIn(needle, done.stderr)
                    self.assertNotIn("Traceback", done.stderr)
                    self.assertEqual(done.stdout, "")
                    self.assertEqual([snapshot(self.tmp / f"{mode}-{position}-cli"), snapshot(outside)], before)


class ValidCampaignTests(Base):
    def test_real_directories_preserve_success_and_refusal_contracts(self):
        meeting = lambda root: root / "content" / "meetings" / f"{STEM}.json"  # noqa: E731
        root = self.campaign("outcome", "valid")
        outcome = record_outcome(root, MID, "continued", "2026-10-14", SOURCE, "Synthetic Organizer", "4-1", today=TODAY)
        stored = json.loads(meeting(root).read_text(encoding="utf-8"))
        self.assertEqual(stored["outcome"], outcome)
        self.assertEqual(stored["submitted_comment"], SUBMITTED)  # unrelated raw fields kept
        self.assertEqual(stored["comment_kit_md"], json.loads((self.base / "fresh" / "content" / "meetings" / f"{STEM}.json")
                                                              .read_text(encoding="utf-8"))["comment_kit_md"])
        # the stored id (cedar-council-2026-10), not the file name, selects the meeting
        self.assertEqual(record_minutes(root, MID, MINUTES, "2026-10-20", today=TODAY, replace=True)["minutes_url"], MINUTES)
        before = meeting(root).read_bytes()
        with self.assertRaisesRegex(MeetingOutcomeError, "already has an outcome"):  # no-replace refusal
            record_outcome(root, MID, "continued", "2026-10-14", SOURCE, "Synthetic Organizer", "4-1", today=TODAY)
        with self.assertRaisesRegex(MeetingOutcomeError, "already has minutes"):
            record_minutes(root, MID, MINUTES, "2026-10-20", today=TODAY)
        self.assertEqual(meeting(root).read_bytes(), before)
        replaced = record_outcome(root, MID, "continued", "2026-10-14", SOURCE, "Synthetic Organizer", "3-2", replace=True,
                                  today=TODAY)  # explicit replacement drops the minutes
        self.assertEqual((replaced["vote"], "minutes_url" in replaced), ("3-2", False))
        self.assertEqual(record_direction(root, MID, DIRECTION, "2026-11-01", today=TODAY)["direction"], DIRECTION)
        self.assertEqual(record_comment_counts(root, MID, spoke=3, written=2, today=TODAY)["spoke"], 3)
        final = json.loads(meeting(root).read_text(encoding="utf-8"))
        self.assertEqual((final["outcome"]["direction"], final["outcome"]["written"]), (DIRECTION, 2))
        self.assertEqual(final["submitted_comment"], SUBMITTED)
        self.assertEqual(os.stat(meeting(root)).st_mode & 0o777, 0o600)  # the existing atomic writer's mode
        self.assertFalse((root / "public").exists())

    def test_real_directories_keep_status_refusals_and_cli_success(self):
        root = self.campaign("outcome", "refusals")
        path = root / "content" / "meetings" / f"{STEM}.json"
        original = json.loads(path.read_text(encoding="utf-8"))
        path.write_text(json.dumps({**original, "status": "cancelled"}), encoding="utf-8")
        with self.assertRaisesRegex(MeetingOutcomeError, "is cancelled"):
            record_outcome(root, MID, "continued", "2026-10-14", SOURCE, "Synthetic Organizer", today=TODAY)
        path.write_text(json.dumps({**original, "starts_at": "2026-12-14T18:00:00-08:00"}), encoding="utf-8")
        with self.assertRaisesRegex(MeetingOutcomeError, "has not happened yet"):
            record_outcome(root, MID, "continued", "2026-10-14", SOURCE, "Synthetic Organizer", today=TODAY)
        path.write_text(json.dumps(original), encoding="utf-8")
        for call in (lambda: record_minutes(root, MID, MINUTES, "2026-10-20", today=TODAY),
                     lambda: record_direction(root, MID, DIRECTION, "2026-11-01", today=TODAY),
                     lambda: record_comment_counts(root, MID, spoke=1, today=TODAY)):
            with self.assertRaisesRegex(MeetingOutcomeError, "needs a recorded outcome"):
                call()
        done = run_cli(root, *CREATE, "--vote", "4-1")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("Recorded only. Nothing was published.", done.stdout)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["outcome"]["result"], "continued")
        for mode in ("minutes", "direction", "counts"):
            done = run_cli(root, *OPERATIONS[mode][2])
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertIn("Recorded only. Nothing was published.", done.stdout)


if __name__ == "__main__":
    unittest.main()
