"""RF1550-13: a failed metadata inspection of content/elsewhere.json is a fixed read refusal, never a raw OSError.

Synthetic only: temp copies of examples/fictional-campaign, no network. The fault is injected at the real
``Path.lstat`` boundary, scoped to the exact leaf; real chmod denial is added where it is effective.
"""
import contextlib
import errno
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from campaign_tool import cli, content_elsewhere
from campaign_tool.content import ContentError, load_content
from campaign_tool.content_check import content_problems
from campaign_tool.content_elsewhere import read_elsewhere

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "fictional-campaign"
DAY = date(2026, 10, 1)
CANARY = "SYNTHETIC-PRIVATE-PATH-MARKER\n\x1b[31m\u00e9"
UNREADABLE = "elsewhere.json: could not be read"


def row(place="Springfield"):
    return {"place": place, "state": "OR", "body": "Springfield City Council", "decided_on": "2026-03-10",
            "kind": "paused", "as_stated": "The council continued the contract.",
            "source_url": "https://records.example.test/minutes/1", "checked_on": "2026-09-15"}


class Case(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-elsewhere-inspect-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.path = self.root / "content" / "elsewhere.json"
        self.path.write_text(json.dumps([row()]), encoding="utf-8")
        guard = mock.patch("socket.socket", side_effect=AssertionError("network access attempted"))
        guard.start()
        self.addCleanup(guard.stop)

    @contextlib.contextmanager
    def lstat_fault(self, exc):
        real = Path.lstat

        def lstat(target, *args, **kwargs):
            if Path(target) == self.path:
                raise exc
            return real(target, *args, **kwargs)

        with mock.patch.object(Path, "lstat", lstat):
            yield

    def denied(self):
        return PermissionError(errno.EACCES, CANARY, str(self.path), None, str(self.path))

    def run_cli(self, *flags):
        out, err = io.StringIO(), io.StringIO()
        argv = ["campaign_tool", "content-check", *flags, "--directory", str(self.root)]
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                cli.main()
                code = 0
            except SystemExit as exc:
                code = exc.code or 0
        return code, out.getvalue(), err.getvalue()

    def snapshot(self):
        result = {}
        for item in sorted(self.root.rglob("*")):
            info = item.lstat()
            result[str(item)] = (info.st_mode, info.st_mtime_ns, item.read_bytes() if item.is_file() else None)
        return result

    def assert_clean(self, text):
        for leaked in (str(self.root), "SYNTHETIC-PRIVATE", "Errno", "\x1b", "Traceback", "Fix"):
            self.assertNotIn(leaked, text)


class InspectionTests(Case):
    def test_real_search_permission_denial_is_safe_in_both_reader_modes(self):
        directory = self.path.parent
        original = directory.stat().st_mode
        directory.chmod(0)
        try:
            try:
                self.path.lstat()
            except PermissionError:
                effective = True
            except OSError:
                effective = False
            else:
                effective = False
            if effective:
                with self.assertRaises(ContentError) as raised:
                    read_elsewhere(self.root, DAY)
                self.assertEqual(str(raised.exception), UNREADABLE)
                problems = ["earlier"]
                self.assertIsNone(read_elsewhere(self.root, DAY, problems))
                self.assertEqual(problems, ["earlier", UNREADABLE])
        finally:
            directory.chmod(original)
        self.assertEqual(len(read_elsewhere(self.root, DAY)), 1)  # recovery

    def test_inspection_oserror_matrix_never_echoes_payload(self):
        for code in (errno.EACCES, errno.EIO, errno.ELOOP):
            exc = OSError(code, CANARY, str(self.path), None, str(self.path))
            with self.subTest(code=code), self.lstat_fault(exc), \
                    mock.patch.object(content_elsewhere, "_read_strict") as acquire, \
                    mock.patch.object(content_elsewhere, "load_elsewhere") as semantic:
                with self.assertRaises(ContentError) as raised:
                    read_elsewhere(self.root, DAY)
                self.assertEqual(str(raised.exception), UNREADABLE)
                self.assertTrue(raised.exception.__suppress_context__)
                self.assertIsNone(raised.exception.__cause__)
                problems = ["earlier"]
                self.assertIsNone(read_elsewhere(self.root, DAY, problems))
                self.assertEqual(problems, ["earlier", UNREADABLE])
                acquire.assert_not_called()
                semantic.assert_not_called()
        for exc in (RuntimeError("boom"), KeyboardInterrupt()):
            with self.subTest(exc=type(exc).__name__), self.lstat_fault(exc):
                with self.assertRaises(type(exc)):
                    read_elsewhere(self.root, DAY, [])

    def test_absence_and_static_leaf_types_keep_existing_meaning(self):
        self.assertEqual([entry["place"] for entry in read_elsewhere(self.root, DAY)], ["Springfield"])
        with self.lstat_fault(FileNotFoundError(errno.ENOENT, "x")):
            self.assertIsNone(read_elsewhere(self.root, DAY, []))
        with self.lstat_fault(NotADirectoryError(errno.ENOTDIR, "x")):
            self.assertIsNone(read_elsewhere(self.root, DAY))
        self.path.unlink()
        self.assertIsNone(read_elsewhere(self.root, DAY))
        shutil.rmtree(self.root / "content")
        self.assertIsNone(read_elsewhere(self.root, DAY))
        (self.root / "content").mkdir()
        self.path.symlink_to(self.root / "missing-target.json")  # dangling
        with mock.patch.object(content_elsewhere, "_read_strict") as acquire:
            with self.assertRaisesRegex(ContentError, r"^elsewhere\.json: symlinks are not read$"):
                read_elsewhere(self.root, DAY)
            self.path.unlink()
            self.path.mkdir()
            with self.assertRaisesRegex(ContentError, r"^elsewhere\.json: must be a regular file, not a symlink or folder$"):
                read_elsewhere(self.root, DAY)
            acquire.assert_not_called()

    def test_post_admission_disappearance_is_read_failure_not_absence(self):
        real_open = os.open

        def opening(target, *args, **kwargs):
            if Path(target) == self.path:
                raise FileNotFoundError(errno.ENOENT, CANARY, str(self.path))
            return real_open(target, *args, **kwargs)

        with mock.patch.object(os, "open", opening):
            with self.assertRaisesRegex(ContentError, "^elsewhere.json: could not be read$"):
                read_elsewhere(self.root, DAY)
            problems = []
            self.assertIsNone(read_elsewhere(self.root, DAY, problems))
            self.assertEqual(problems, [UNREADABLE])

    def test_existing_parser_categories_are_unchanged(self):
        self.path.write_bytes(b"\xff\xfe")
        with self.assertRaisesRegex(ContentError, r"^elsewhere\.json: not valid UTF-8 JSON"):
            read_elsewhere(self.root, DAY)
        self.path.write_text('[{"a": 1, "a": 2}]', encoding="utf-8")
        with self.assertRaisesRegex(ContentError, "repeats a key"):
            read_elsewhere(self.root, DAY)
        self.path.write_bytes(b"[" + b" " * (content_elsewhere.MAX_JSON_BYTES + 1) + b"]")
        with self.assertRaisesRegex(ContentError, "file too large"):
            read_elsewhere(self.root, DAY)


class ConsumerTests(Case):
    def test_model_checker_and_actual_cli_keep_one_attributed_problem(self):
        (self.root / "content" / "findings" / "zz-bad.json").write_text("{not json", encoding="utf-8")
        with self.lstat_fault(self.denied()):
            with self.assertRaises(ContentError) as raised:
                load_content(self.root, DAY)
            self.assertNotIn("SYNTHETIC", str(raised.exception))
            problems = content_problems(self.root)
            self.assertEqual(sum(1 for item in problems if item.startswith("elsewhere.json")), 1)
            self.assertIn(UNREADABLE, problems)
            self.assertTrue(any("zz-bad.json" in item for item in problems))
            code, out, err = self.run_cli()
            self.assertEqual(code, 3)
            self.assertIn(UNREADABLE, out)
            self.assertIn("zz-bad.json", out)
            self.assert_clean(out + err)
            code, out, err = self.run_cli("--json")
            self.assertEqual(code, 3)
            self.assert_clean(out + err)
            document = json.loads(out)
            self.assertEqual(document["schema"], "content-check-v1")
            self.assertIn({"file": "elsewhere.json", "message": "could not be read"}, document["problems"])
            self.assertTrue(any(item["file"].endswith("zz-bad.json") for item in document["problems"]))

    def test_repeated_refusal_and_recovery_do_not_mutate_campaign(self):
        before = self.snapshot()
        with self.lstat_fault(self.denied()):
            for _ in range(2):
                self.assertEqual(self.run_cli()[0], 3)
                self.assertEqual(self.run_cli("--json")[0], 3)
                with self.assertRaises(ContentError):
                    read_elsewhere(self.root, DAY)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(len(read_elsewhere(self.root, DAY)), 1)
        self.assertEqual(self.run_cli()[0], 0)


if __name__ == "__main__":
    unittest.main()
