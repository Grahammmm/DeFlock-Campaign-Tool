"""RF2115-4: speaker-plan inputs enforce their byte limits on the bytes actually read. Synthetic, offline.

Run from the repository root: python3 -B -m unittest tests.test_meeting_speakers_io -q
"""
import hashlib
import json
import os
import stat
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest import mock

from campaign_tool import meeting_speakers as ms
from campaign_tool import meetings_status
from campaign_tool.meeting_stage import stage
from tests.test_meeting_speakers import run
from tests.test_meetings_manual import MID, ManualBase

NOW = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)
BAD = ms.MeetingSpeakersError


class IoBase(ManualBase):
    def setUp(self):
        super().setUp()
        self.add(starts_at="2099-01-20T18:30:00-08:00")
        stage(self.root, MID)
        self.write_scan([{"id": "other-meeting", "status": "scheduled"}])
        ms.update_plan(self.root, MID, add="Ann", point="ask:1", now=NOW)
        self.paths = {
            "staged": (self.root / "content" / "meetings" / f"{MID}.json", ms.MAX_STAGED_BYTES, None),
            "scan": (self.scan, ms.MAX_STAGED_BYTES, "kit/meetings.json is not a regular UTF-8 JSON file up to 4 MiB"),
            "manual": (self.manual, ms.MAX_STAGED_BYTES, ms.BAD_MANUAL),
            "store": (self.root / "kit" / "meetings" / "speakers" / f"{MID}.json", ms.MAX_STORE_BYTES, ms.BAD_STORE),
        }

    def digest_others(self, skip):
        return {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(self.root.rglob("*")) if p.is_file() and p != skip}

    @contextmanager
    def grow_after_fstat(self, name):
        """Real descriptor stat is captured, the real file is padded to cap+1, and the old stat is returned (once)."""
        path, limit, _ = self.paths[name]
        target = path.stat()
        real = os.fstat
        state = {"fired": False}

        def hook(fd):
            info = real(fd)
            if not state["fired"] and (info.st_dev, info.st_ino) == (target.st_dev, target.st_ino):
                state["fired"] = True
                with open(path, "ab") as handle:
                    handle.write(b" " * (limit + 1 - info.st_size))
            return info

        with mock.patch.object(ms.os, "fstat", hook):
            yield state
        self.assertTrue(state["fired"])
        self.assertEqual(path.stat().st_size, limit + 1)

    def message(self, name):
        return self.paths[name][2] or f"content/meetings/{MID}.json is not a regular UTF-8 JSON file up to 4 MiB"

    def actions(self):
        return [lambda: ms.list_plan(self.root, MID, now=NOW),
                lambda: ms.update_plan(self.root, MID, add="Bo", point="ask:1", now=NOW),
                lambda: ms.update_plan(self.root, MID, add="Ann", point="ask:1", now=NOW),
                lambda: ms.update_plan(self.root, MID, remove="Ann", now=NOW),
                lambda: ms.update_plan(self.root, MID, remove="Nobody", now=NOW)]


class ReaderTests(IoBase):
    def test_actual_byte_limits_below_exact_and_plus_one(self):
        for name, (path, limit, _) in self.paths.items():
            original = path.read_bytes()
            for size, ok in ((len(original), True), (limit, True), (limit + 1, False)):
                path.write_bytes(original + b" " * (size - len(original)))
                if ok:
                    self.assertEqual(len(ms._read_regular(path, limit, False, BAD("bad")) or b""), size, name)
                else:
                    with self.assertRaises(BAD), mock.patch.object(ms, "_json") as parse:
                        ms._read_regular(path, limit, False, BAD("bad"))
                    parse.assert_not_called()
            path.write_bytes(original)
        # exact-cap padded private plan still lists through the public API
        path, limit, _ = self.paths["store"]
        path.write_bytes(path.read_bytes() + b" " * (limit - path.stat().st_size))
        listed = ms.list_plan(self.root, MID, now=NOW)["points"]
        self.assertEqual([s for p in listed for s in p["speakers"]], ["Ann"])
        path.write_bytes(path.read_bytes() + b" ")
        with self.assertRaises(BAD) as caught:
            ms.list_plan(self.root, MID, now=NOW)
        self.assertEqual(str(caught.exception), ms.BAD_STORE)

    def test_growth_after_real_fstat_refuses_all_four_sources(self):
        for name in self.paths:
            with self.subTest(name), mock.patch.object(ms, "_json", wraps=ms._json) as parse:
                with self.grow_after_fstat(name):
                    with self.assertRaises(BAD) as caught:
                        ms.list_plan(self.root, MID, now=NOW)
                self.assertEqual(str(caught.exception), self.message(name))
                for call in parse.call_args_list:
                    self.assertLessEqual(len(call.args[0]), self.paths[name][1])
                self.paths[name][0].write_bytes(self.paths[name][0].read_bytes().rstrip(b" "))

    def test_growing_source_blocks_add_remove_repeat_and_cli(self):
        for name in self.paths:
            path = self.paths[name][0]
            for number, action in enumerate(self.actions()):
                with self.subTest(name=name, action=number):
                    before = self.digest_others(path)
                    with mock.patch.object(ms, "_write") as write, self.grow_after_fstat(name):
                        with self.assertRaises(BAD) as caught:
                            action()
                    write.assert_not_called()
                    self.assertEqual(str(caught.exception), self.message(name))
                    self.assertEqual(self.digest_others(path), before)
                    path.write_bytes(path.read_bytes().rstrip(b" "))
            for cli_args in (("--list",), ("--add", "Bo", "--point", "ask:1"), ("--remove", "Ann")):
                with self.subTest(name=name, cli=cli_args):
                    before = self.digest_others(path)
                    with self.grow_after_fstat(name):
                        code, out, err = run("meetings-speakers", "--directory", self.root, "--id", MID, *cli_args)
                    self.assertEqual((code, out, err.strip()), (1, "", "Stopped: " + self.message(name)))
                    self.assertEqual(self.digest_others(path), before)
                    path.write_bytes(path.read_bytes().rstrip(b" "))

    def test_descriptor_revalidates_regular_single_link_before_read(self):
        path = self.paths["store"][0]
        sentinel = self.tmp / "sentinel.json"
        sentinel.write_bytes(b"{}")
        real_lstat = os.lstat

        def swap(kind):
            def hook(target, *args, **kwargs):
                info = real_lstat(target, *args, **kwargs)
                if os.fspath(target) == os.fspath(path) and not os.path.lexists(path.with_name("swapped")):
                    path.with_name("swapped").write_bytes(b"")
                    path.unlink()
                    if kind == "fifo":
                        os.mkfifo(path)
                    elif kind == "dir":
                        path.mkdir()
                    else:
                        os.link(sentinel, path)
                return info
            return hook

        for kind in ("fifo", "dir", "hardlink"):
            with self.subTest(kind):
                original = path.read_bytes()
                path.with_name("swapped").unlink(missing_ok=True)
                with mock.patch.object(ms.os, "lstat", swap(kind)), mock.patch.object(ms, "_json") as parse:
                    with self.assertRaises(BAD) as caught:
                        ms.read_plan(self.root, MID)
                parse.assert_not_called()
                self.assertEqual(str(caught.exception), ms.BAD_STORE)
                self.assertEqual(sentinel.read_bytes(), b"{}")
                if kind == "dir":
                    path.rmdir()
                else:
                    path.unlink()
                path.write_bytes(original)
        path.with_name("swapped").unlink(missing_ok=True)
        self.assertTrue(stat.S_ISREG(path.lstat().st_mode))

    def test_reader_closes_on_length_refusal_and_read_error(self):
        path, limit, _ = self.paths["store"]
        opened, closed = [], []
        real_open, real_close = os.open, os.close

        def spy_open(*args, **kwargs):
            fd = real_open(*args, **kwargs)
            opened.append(fd)
            return fd

        def spy_close(fd):
            closed.append(fd)
            real_close(fd)

        original = path.read_bytes()
        with mock.patch.object(ms.os, "open", spy_open), mock.patch.object(ms.os, "close", spy_close):
            with self.grow_after_fstat("store"), self.assertRaises(BAD):
                ms._read_regular(path, limit, False, BAD("bad"))  # refused on the returned length
            path.write_bytes(original)
            with self.assertRaises(BAD) as caught, mock.patch.object(ms.os, "fdopen", side_effect=PermissionError):
                ms._read_regular(path, limit, False, BAD("bad"))
            self.assertEqual(str(caught.exception), "bad")
        self.assertEqual(len(opened), 2)
        self.assertEqual(opened, closed)

    def test_valid_inputs_and_absent_optional_sources_keep_outputs(self):
        before = ms.list_plan(self.root, MID, now=NOW)
        self.scan.unlink()
        self.assertIsNone(ms._read_regular(self.scan, ms.MAX_STAGED_BYTES, True, BAD("bad")))
        self.assertEqual(ms.list_plan(self.root, MID, now=NOW), before)
        store = self.paths["store"][0]
        self.assertEqual(stat.S_IMODE(store.stat().st_mode), 0o600)
        self.assertEqual(ms.update_plan(self.root, MID, remove="Ann", now=NOW)["changed"], True)
        self.assertEqual(ms.read_plan(self.root, MID)[1], [])

    def test_meetings_status_marks_oversized_growing_plan_unknown(self):
        store = self.paths["store"][0]
        with self.grow_after_fstat("store"):
            report = meetings_status.inventory(self.root, "2026-11-10", directory="D", today=NOW.date())
        rows = [r for r in report["rows"] if r.get("id") == MID]
        self.assertEqual([r["speakers_plan"] for r in rows], ["unknown"])
        self.assertIn(f"kit/meetings/speakers/{MID}.json", json.dumps(report))
        self.assertNotIn("Ann", json.dumps(report))
        self.assertEqual(store.stat().st_size, ms.MAX_STORE_BYTES + 1)


if __name__ == "__main__":
    unittest.main()
