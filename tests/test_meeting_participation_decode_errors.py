"""RF0421-5: JSON decoder recursion in `meetings-stage --participation FILE` is the fixed reader refusal.

Synthetic temp copies of the fictional example only; no network, nothing is sent, built or published.
Run from the repository root: python3 -B -m unittest tests.test_meeting_participation_decode_errors -q
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from campaign_tool import meeting_stage
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
ARRAY = b"[" * 40000 + b"0"  # 40,001 bytes
OBJECT = b'{"a":' * 12000 + b"0"  # 60,001 bytes
REAL_FSTAT = os.fstat


def snapshot(folder):
    """bytes, mode and mtime (never atime) of every path below folder, plus membership."""
    result = {}
    for p in sorted(folder.rglob("*")):
        info = os.lstat(p)
        result[str(p.relative_to(folder))] = (p.read_bytes() if stat.S_ISREG(info.st_mode) else None,
                                              stat.S_IMODE(info.st_mode), info.st_mtime_ns)
    return result


class ParticipationDecodeErrorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-participation-decode-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name).resolve()
        self.root = self.tmp / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        (self.root / "kit").mkdir(exist_ok=True)
        (self.root / "kit" / "meetings.json").write_text(json.dumps({"schema_version": 1, "meetings": [
            {"id": MID, "body": "Example City Council", "starts_at": STARTS, "agenda_item": "Item 7: ALPR",
             "agenda_url": "https://example.invalid/agenda"}]}), encoding="utf-8")
        self.input = self.tmp / "participation.json"
        self.input.write_bytes(json.dumps(FULL).encode("utf-8"))

    def put(self, data):
        self.input.write_bytes(data)

    def stage(self, **kwargs):
        return stage(self.root, MID, participation=str(self.input), today=TODAY, **kwargs)

    def test_real_nested_array_and_object_refuse_at_reader_boundary(self):
        for name, raw in (("array", ARRAY), ("object", OBJECT)):
            with self.subTest(shape=name):
                self.assertLess(len(raw), LIMIT)
                with self.assertRaises(RecursionError):  # the fixture really exhausts the decoder
                    json.loads(raw.decode("utf-8"))
                self.put(raw)
                for call in (lambda: meeting_stage._read_participation(str(self.input)), self.stage):
                    with self.assertRaises(MeetingStageError) as caught:
                        call()
                    self.assertEqual(str(caught.exception), REFUSAL)
                    self.assertIsNone(caught.exception.__cause__)
                    self.assertTrue(caught.exception.__suppress_context__)

    def cli(self, *extra):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "meetings-stage", "--directory",
                               str(self.root), "--id", MID, "--participation", str(self.input), *extra],
                              cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=120)

    def test_cli_decoder_recursion_has_one_fixed_stopped_line_and_no_write(self):
        for replace in (False, True):
            if replace:
                self.put(json.dumps(FULL).encode("utf-8"))
                stage(self.root, MID, participation=str(self.input), today=TODAY)
            for name, raw in (("array", ARRAY), ("object", OBJECT)):
                with self.subTest(replace=replace, shape=name):
                    self.put(raw)
                    os.chmod(self.input, 0o600)
                    before = snapshot(self.root)
                    in_info = os.lstat(self.input)
                    done = self.cli(*(["--replace"] if replace else []))
                    self.assertEqual(done.returncode, 1)
                    self.assertEqual(done.stdout, "")
                    self.assertEqual(done.stderr, "Stopped: " + REFUSAL + "\n")
                    self.assertEqual(snapshot(self.root), before)
                    self.assertEqual(self.input.read_bytes(), raw)
                    self.assertEqual(os.lstat(self.input).st_mtime_ns, in_info.st_mtime_ns)

    def test_descriptor_closes_after_real_recursion(self):
        opened = []
        real_open, real_close = os.open, os.close
        live = set()

        def spy_open(path, *a, **k):
            fd = real_open(path, *a, **k)
            if os.fspath(path) == str(self.input):
                opened.append(fd)
                live.add(fd)
            return fd

        def spy_close(fd):
            live.discard(fd)
            return real_close(fd)
        sentinel = real_open(os.devnull, os.O_RDONLY)
        try:
            for raw in (ARRAY, OBJECT):
                self.put(raw)
                with mock.patch.object(os, "open", spy_open), mock.patch.object(os, "close", spy_close):
                    with self.assertRaises(MeetingStageError):
                        meeting_stage._read_participation(str(self.input))
                self.assertTrue(opened)
                self.assertEqual(live, set())
                for fd in opened:
                    with self.assertRaises(OSError):
                        REAL_FSTAT(fd)
                REAL_FSTAT(sentinel)  # unrelated descriptor still open
        finally:
            real_close(sentinel)

    def test_existing_precedence_is_unchanged(self):
        over = b"[" * (LIMIT + 1)
        cases = {"oversize": over, "bad_utf8": b"\xff\xfe", "syntax": b"{", "empty": b""}
        for name, raw in cases.items():
            with self.subTest(case=name):
                self.put(raw)
                with mock.patch.object(meeting_stage.json, "loads", wraps=json.loads) as loads:
                    with self.assertRaises(MeetingStageError) as caught:
                        self.stage()
                self.assertFalse(any(len(c.args[0]) > LIMIT for c in loads.call_args_list if isinstance(c.args[0], str)))
                self.assertEqual(str(caught.exception), REFUSAL)
        raw = json.dumps(FULL).encode("utf-8")
        self.put(raw + b" " * (LIMIT - len(raw)))  # exact cap still succeeds
        self.stage()
        self.assertTrue((self.root / "content" / "meetings" / f"{MID}.json").exists())

    def test_decodable_wrong_shapes_keep_semantic_refusals(self):
        for raw in (b"[[[0]]]", b"3", b'{"unknown": {"a": [1]}}', b'{"in_person": [[1]]}'):
            with self.subTest(raw=raw):
                self.put(raw)
                with self.assertRaises(ContentError):
                    self.stage()
        # a downstream RecursionError is not swallowed into the reader refusal
        self.put(json.dumps(FULL).encode("utf-8"))
        with mock.patch.object(meeting_stage, "load_participation", side_effect=RecursionError("deep")):
            with self.assertRaises(RecursionError):
                self.stage()


if __name__ == "__main__":
    unittest.main()
