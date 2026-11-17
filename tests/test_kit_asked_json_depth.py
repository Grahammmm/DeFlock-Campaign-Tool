"""RF1501-5: kit-asked refuses an agencies.json whose JSON decoder exhausts recursion (synthetic only)."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import kit_asked, kit_track  # noqa: E402
from campaign_tool.content import ContentError  # noqa: E402
from tests.test_kit_asked import EXAMPLE, POLICE, A, B  # noqa: E402

MESSAGE = "agencies.json: JSON nesting is too deep"
CANARY = "PAYLOAD-CANARY-5501"
DEPTH = 20 * sys.getrecursionlimit()  # the C scanner's own budget exceeds the Python limit; stay far above both
SHAPES = {
    "array": "[" * DEPTH + "]" * DEPTH,
    "object": '{"a":' * DEPTH + f'"{CANARY}"' + "}" * DEPTH,
    "mixed": '[{"a":' * DEPTH + f'"{CANARY}"' + "}]" * DEPTH,
}


def snapshot(root):
    result = {}
    for path in sorted(Path(root).rglob("*")):
        info = path.lstat()
        result[str(path.relative_to(root))] = (
            path.read_bytes() if path.is_file() else None, info.st_mode, info.st_ino, info.st_mtime_ns)
    return result


class JsonDepthTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-kit-asked-depth-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root)
        self.path = self.root / "content" / "agencies.json"
        self.healthy = self.path.read_bytes()
        sent = self.root / "kit" / "sent.json"
        sent.parent.mkdir(exist_ok=True)
        sent.write_text(json.dumps({"schema_version": 1, "requests": {POLICE: {
            "sent_on": "2026-03-02", "channel": "email", "draft": f"requests/{POLICE}.md", "draft_sha256": "a" * 64,
            "recorded_at": "2026-03-02T10:00:00+00:00", "steps": [], "scopes": [A, B]}}}), encoding="utf-8")
        (self.root / "unrelated.txt").write_text("keep me\n", encoding="utf-8")

    def deep(self, shape="array"):
        self.path.write_text(SHAPES[shape], encoding="utf-8")

    def cli(self, *flags):
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "kit-asked", "--directory", str(self.root),
                               *flags], cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)

    def test_real_array_object_and_mixed_decoder_exhaustion_has_fixed_content_error(self):
        for shape in SHAPES:
            with self.subTest(shape=shape):
                self.deep(shape)
                self.assertLess(self.path.stat().st_size, 2 * 1024 * 1024)
                for call in (lambda: kit_asked._read_agencies(self.path), lambda: kit_asked.propose(self.root)):
                    with self.assertRaises(ContentError) as caught:
                        call()
                    self.assertEqual(str(caught.exception), MESSAGE)
                    self.assertTrue(caught.exception.__suppress_context__)
                    self.assertIsNone(caught.exception.__cause__)
                    self.assertNotIn(CANARY, str(caught.exception))

    def test_depth_refusal_precedes_history_titles_projection_and_writes(self):
        self.deep()
        (self.root / "kit" / "sent.json").write_text("{not json", encoding="utf-8")  # malformed history
        seams = [mock.patch.object(kit_track, "read_sent", side_effect=AssertionError("history")),
                 mock.patch.object(kit_asked, "_titles", side_effect=AssertionError("titles")),
                 mock.patch.object(kit_asked.copy, "deepcopy", side_effect=AssertionError("deepcopy")),
                 mock.patch.object(kit_asked.kit, "_write_json", side_effect=AssertionError("write")),
                 mock.patch.object(os, "chmod", side_effect=AssertionError("chmod"))]
        with contextlib.ExitStack() as stack:
            for seam in seams:
                stack.enter_context(seam)
            with self.assertRaises(ContentError) as caught:
                kit_asked.propose(self.root)
            self.assertEqual(str(caught.exception), MESSAGE)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(kit_asked.run_cli(self.root, apply=True), 3)

    def test_dry_run_apply_and_outcomes_cli_have_same_clean_refusal(self):
        self.deep("object")
        for apply in (False, True):
            for outcomes in (False, True):
                flags = (["--apply"] if apply else []) + (["--outcomes"] if outcomes else [])
                with self.subTest(flags=flags):
                    done = self.cli(*flags)
                    self.assertEqual((done.returncode, done.stdout, done.stderr), (3, "", f"Stopped: {MESSAGE}\n"))
                    err, out = io.StringIO(), io.StringIO()
                    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                        code = kit_asked.run_cli(self.root, apply=apply, outcomes=outcomes)
                    self.assertEqual((code, out.getvalue(), err.getvalue()), (3, "", f"Stopped: {MESSAGE}\n"))
                    for text in (done.stdout, done.stderr):
                        for banned in ("Traceback", "RecursionError", CANARY, "Fix", "Written", "Dry run"):
                            self.assertNotIn(banned, text)

    def test_depth_refusal_preserves_campaign_and_repair_allows_retry(self):
        self.deep("mixed")
        before = snapshot(self.root)
        for flags in ((), ("--apply",)):
            self.assertEqual(self.cli(*flags).returncode, 3)
        with self.assertRaises(ContentError):
            kit_asked.propose(self.root)
        self.assertEqual(snapshot(self.root), before)
        self.path.write_bytes(self.healthy)
        mode = self.path.stat().st_mode & 0o777
        done = self.cli("--apply")
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        self.assertIn("Written to content/agencies.json", done.stdout)
        applied = self.path.read_bytes()
        self.assertNotEqual(applied, self.healthy)
        document = json.loads(applied)
        police = next(e for e in document if e["agency_id"] == POLICE)
        self.assertEqual(police["asked"][-1]["state"], "awaiting_response")
        self.assertEqual(self.path.stat().st_mode & 0o777, mode)
        self.assertFalse(list(self.path.parent.glob("*.tmp")))
        again = self.cli("--apply")
        self.assertIn("Nothing written.", again.stdout)
        self.assertEqual(self.path.read_bytes(), applied)

    def test_decode_recursion_releases_descriptor_and_preserves_unrelated_fd(self):
        self.deep()
        opened = []
        real_open = os.open

        def spy(*args, **kwargs):
            fd = real_open(*args, **kwargs)
            if str(args[0]).endswith("agencies.json"):
                opened.append(fd)
            return fd

        unrelated = os.open(self.root / "unrelated.txt", os.O_RDONLY)
        try:
            with mock.patch.object(os, "open", spy):
                with self.assertRaises(ContentError):
                    kit_asked._read_agencies(self.path)
            self.assertEqual(len(opened), 1)
            with self.assertRaises(OSError):
                os.fstat(opened[0])
            self.assertEqual(os.read(unrelated, 4), b"keep")
            self.path.write_bytes(self.healthy)
            self.assertIsInstance(kit_asked._read_agencies(self.path), list)
        finally:
            os.close(unrelated)

    def test_recursion_translation_is_decode_local_and_value_free(self):
        self.path.write_bytes(self.healthy)
        with mock.patch.object(kit_asked.json, "loads", side_effect=RecursionError(CANARY)):
            with self.assertRaises(ContentError) as caught:
                kit_asked._read_agencies(self.path)
        self.assertEqual(str(caught.exception), MESSAGE)
        with mock.patch.object(kit_asked.json, "loads", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                kit_asked._read_agencies(self.path)
        for seam in (mock.patch.object(kit_track, "read_sent", side_effect=RecursionError(CANARY)),
                     mock.patch.object(kit_asked, "_titles", side_effect=RecursionError(CANARY))):
            with seam, self.assertRaises(RecursionError):
                kit_asked.propose(self.root)

    def test_healthy_input_and_existing_admission_errors_remain_compatible(self):
        self.path.write_bytes(self.healthy)
        result = kit_asked.propose(self.root)
        self.assertTrue(result["changed"])
        self.assertIsNone(result["error"])
        self.assertEqual(kit_asked.run_cli(self.root, apply=True), 0)
        self.assertEqual(kit_asked.run_cli(self.root, apply=True), 0)
        cases = {
            "shallow": (b'{"a": 1}', "content/agencies.json: must be a list of agencies"),
            "syntax": (b"[1,", "agencies.json: not valid UTF-8 JSON ("),
            "utf8": (b"[\xff]", "agencies.json: not valid UTF-8 JSON ("),
            "large": (b"[" + b" " * (2 * 1024 * 1024) + b"]", "agencies.json: file too large"),
        }
        for name, (data, expected) in cases.items():
            with self.subTest(name=name):
                self.path.write_bytes(data)
                with self.assertRaises(ContentError) as caught:
                    kit_asked.propose(self.root)
                self.assertTrue(str(caught.exception).startswith(expected), str(caught.exception))
                self.assertNotIn("nesting", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
