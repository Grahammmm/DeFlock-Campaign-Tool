"""Saved-request leaf admission: the JSON is consumed through one non-following descriptor, so a pathname swapped to a
symlink after the preliminary checks is refused before any payload read, history access or step. Offline, synthetic."""
import errno
import json
import re
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from campaign_tool import kit_muckrock, kit_track
from tests import test_kit_muckrock as base
from tests import test_kit_muckrock_bounded_read as bounded

PLAIN = "not a regular file (symlinks are refused)"
REFUSED = re.escape(PLAIN)
CANARY = "SYNTHETIC-OUTSIDE-CANARY"


class LeafAdmissionTests(base.KitMuckrockTests):
    for _name in [n for n in dir(base.KitMuckrockTests) if n.startswith("test_")]:
        locals()[_name] = None
    del _name

    def outside(self):  # a separate document, outside the campaign, that would be acknowledged if consumed
        target = self.tmp / "outside.json"
        document = {**json.loads((base.FIXTURES / "request-90001.json").read_text(encoding="utf-8")), "status": "processed"}
        target.write_text(json.dumps({**document, "canary": CANARY}), encoding="utf-8")
        return target

    def swap(self, path, replacement):  # replace the pathname immediately before the real os.open
        real, done = os.open, []

        def opener(name, *args, **kwargs):
            if not done and os.fspath(name) == os.fspath(path):
                done.append(1)
                os.rename(path, self.tmp / "original-backup")
                if replacement == "FIFO":
                    os.mkfifo(path)
                elif replacement == "DIR":
                    os.mkdir(path)
                else:
                    os.symlink(replacement, path)
            return real(name, *args, **kwargs)
        return mock.patch.object(os, "open", opener)

    def traps(self):
        return (mock.patch.object(kit_muckrock.json, "loads", side_effect=AssertionError("parsed")),
                mock.patch.object(kit_track, "read_sent", side_effect=AssertionError("history")),
                mock.patch.object(kit_track, "record_step", side_effect=AssertionError("recorded")))

    def test_leaf_symlink_swap_refuses_before_target_payload_read(self):
        outside = self.outside()
        for label, link in (("existing", outside), ("dangling", self.tmp / "nowhere.json")):
            path = self.saved(status="started")
            with self.subTest(label), self.swap(path, link), self.traps()[0]:
                with self.assertRaises(ValueError) as caught:
                    kit_muckrock._load(path)
            self.assertEqual(str(caught.exception), f"{path}: {PLAIN}")
        link = self.tmp / "static.json"
        link.symlink_to(outside)
        for target in (link, self.tmp / "missing.json"):
            with self.subTest(target=target.name), self.assertRaisesRegex(ValueError, "not a regular file"):
                kit_muckrock._load(target)

    def test_open_inode_stays_bound_after_path_replacement(self):
        path = self.saved(status="started")
        outside = self.outside()
        real_fstat = os.fstat

        def fstat(fd):
            info = real_fstat(fd)
            os.rename(path, self.tmp / "moved.json")
            os.symlink(outside, path)
            return info
        with mock.patch.object(os, "fstat", fstat):
            document = kit_muckrock._load(path)
        self.assertEqual(document["status"], "started")
        self.assertNotIn("canary", document)

    def test_replaced_fifo_directory_and_oversize_regular_refuse_without_payload(self):
        for replacement in ("FIFO", "DIR"):
            path = self.saved(status="started")
            with self.subTest(replacement), self.swap(path, replacement), self.traps()[0]:
                with self.assertRaisesRegex(ValueError, REFUSED):
                    kit_muckrock._load(path)
            path.unlink(missing_ok=True) if replacement == "FIFO" else path.rmdir()
        path = self.saved(status="started")
        stream, real_fstat = bounded.Growing(), os.fstat

        def big(fd):  # a regular file whose descriptor size is over the cap although the earlier pathname check passed
            info = real_fstat(fd)
            return mock.Mock(st_mode=info.st_mode, st_size=kit_muckrock.MAX_BYTES + 1)
        with stream.patching(), mock.patch.object(os, "fstat", big):
            with self.assertRaisesRegex(ValueError, "larger than 1 MiB"):
                kit_muckrock._load(path)
        self.assertEqual((stream.opens, stream.requests), (1, []))

    def test_raw_descriptor_to_stream_ownership_on_all_unwinds(self):
        path = self.saved(status="started")
        opened = []
        real_open = os.open

        def spy(*args, **kwargs):
            opened.append(real_open(*args, **kwargs))
            return opened[-1]

        def closed(fd):
            with self.assertRaises(OSError) as caught:
                os.fstat(fd)
            self.assertEqual(caught.exception.errno, errno.EBADF)
        for label, patches, expected in (
                ("ok", [], None),
                ("fstat", [mock.patch.object(os, "fstat", side_effect=OSError("fstat"))], OSError),
                ("fdopen oserror", [mock.patch.object(os, "fdopen", side_effect=OSError("fdopen"))], OSError),
                ("fdopen memory", [mock.patch.object(os, "fdopen", side_effect=MemoryError())], MemoryError)):
            opened.clear()
            with self.subTest(label), mock.patch.object(os, "open", spy):
                for patcher in patches:
                    patcher.start()
                    self.addCleanup(patcher.stop)
                try:
                    if expected:
                        with self.assertRaises(expected):
                            kit_muckrock._load(path)
                    else:
                        kit_muckrock._load(path)
                finally:
                    mock.patch.stopall()
                self.assertEqual(len(opened), 1)
                closed(opened[0])
        for label, body in (("malformed", b"{"), ("deep", None)):
            bad = self.tmp / "bad.json"
            bad.write_bytes(body if body else b"[" * 20000 + b"]" * 20000)
            opened.clear()
            with self.subTest(label), mock.patch.object(os, "open", spy), self.assertRaises(ValueError):
                kit_muckrock._load(bad)
            closed(opened[0])
        stream = bounded.Growing(read_error=OSError("read"))  # a read error keeps its identity and closes once
        with stream.patching(), self.assertRaisesRegex(OSError, "^read$"):
            kit_muckrock._load(path)
        self.assertEqual(stream.closed, 1)
        with mock.patch.object(os, "O_NOFOLLOW", None, create=True), mock.patch.object(kit_muckrock.os, "O_NOFOLLOW",
                                                                                      None, create=True):
            with self.assertRaisesRegex(ValueError, "safe non-symlink file reads are unavailable"):
                kit_muckrock._load(path)

    def test_growth_after_real_descriptor_stat_still_has_actual_byte_guard(self):
        path = self.saved(status="processed")
        stream = bounded.Growing(kit_muckrock.MAX_BYTES + 1)
        with stream.patching(), self.traps()[0]:
            with self.assertRaisesRegex(ValueError, "larger than 1 MiB"):
                kit_muckrock._load(path)
        self.assertEqual((stream.opens, stream.requests, stream.returned, stream.closed),
                         (1, [kit_muckrock.MAX_BYTES + 1], [kit_muckrock.MAX_BYTES + 1], 1))

    def test_cli_modes_and_shortcuts_do_not_bypass_leaf_admission(self):
        outside = self.outside()
        for stored in (True, False):
            for status in ("processed", "started"):
                for apply in (False, True):
                    for as_json in (False, True):
                        with self.subTest(stored=stored, status=status, apply=apply, json=as_json):
                            if stored and not self.sent.exists():
                                self.filed()
                            elif self.sent.exists():
                                self.sent.unlink()
                            path = self.saved(status=status)
                            if (self.tmp / "original-backup").exists():
                                (self.tmp / "original-backup").unlink()
                            sent_before = self.sent.read_bytes() if self.sent.exists() else None
                            target_before = outside.read_bytes()
                            with self.swap(path, outside), self.traps()[1], self.traps()[2]:
                                code, out, err = self.imp(path, *(["--apply"] * apply), *(["--json"] * as_json))
                            self.assertEqual((code, out), (1, ""))
                            self.assertEqual(err.count("Stopped:"), 1)
                            self.assertIn(f"Stopped: {path}: {PLAIN}", err)
                            for text in ("Traceback", CANARY, str(outside), "Recorded"):
                                self.assertNotIn(text, err)
                            self.assertEqual(outside.read_bytes(), target_before)
                            self.assertEqual(self.sent.read_bytes() if self.sent.exists() else None, sent_before)
                            path.unlink()

    def test_healthy_mapping_apply_replay_and_recovery_remain_compatible(self):
        self.filed()
        path = self.saved(status="processed")
        outside = self.outside()
        with self.swap(path, outside):
            self.assertEqual(self.imp(path, "--apply")[0], 1)
        path.unlink()
        (self.tmp / "original-backup").rename(path)  # restore the synthetic source: ordinary behaviour again
        code, out, _ = self.imp(path)
        self.assertEqual(code, 0)
        self.assertIn("Dry run: nothing written", out)
        self.assertEqual(self.steps(), [])
        self.assertEqual(self.imp(path, "--apply")[0], 0)
        self.assertEqual([s["kind"] for s in self.steps()], ["acknowledged"])
        code, out, _ = self.imp(path, "--apply")
        self.assertEqual(code, 0)
        self.assertIn("already recorded", out)
        self.assertEqual(len(self.steps()), 1)


if __name__ == "__main__":
    unittest.main()
