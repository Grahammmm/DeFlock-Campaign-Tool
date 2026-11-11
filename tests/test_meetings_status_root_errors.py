"""meetings-status: an OS failure while reading the campaign root's metadata is the same fixed, value-free refusal."""

import contextlib
import io
import os
import tempfile
import traceback
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from campaign_tool import cli as cli_module
from campaign_tool import meetings_status
from campaign_tool.meetings_status import MeetingsStatusError, inventory
from tests.test_meetings_status import AS_OF, TODAY, build, cli, snapshot

FIXED = "Stopped: campaign directory does not exist or is not a directory\n"
SECRET = "SYNTHETIC-PRIVATE-ROOT-DETAIL"
FAULTS = (FileNotFoundError, NotADirectoryError, PermissionError, OSError)


def fail_second_lstat(root, make_error):
    """Real lstat; the call on ``root`` after the first one (past lexists) raises."""
    real = os.lstat
    seen = []

    def fake(path, *a, **k):
        if os.fspath(path) == root:
            seen.append(1)
            if len(seen) >= 2:
                raise make_error()
        return real(path, *a, **k)

    return mock.patch("os.lstat", fake), seen


def run_main(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), mock.patch(
            "sys.argv", ["campaign_tool", *argv]):
        try:
            cli_module.main()
            code = 0
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


class RootErrorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "campaign"
        self.root.mkdir()
        build(self.root)
        (self.base / "sibling.txt").write_text("keep")

    def test_real_root_disappearance_becomes_fixed_inventory_error(self):
        root, moved = str(self.root), self.base / "moved"
        before = snapshot(self.root)
        real, seen = os.lstat, []

        def fake(path, *a, **k):
            if os.fspath(path) == root:
                seen.append(1)
                if len(seen) == 2:
                    os.rename(root, moved)  # the real directory vanishes; real lstat raises FileNotFoundError
            return real(path, *a, **k)

        with mock.patch("os.lstat", fake), mock.patch.object(
                meetings_status, "_staged", side_effect=AssertionError("children scanned")):
            with self.assertRaises(MeetingsStatusError) as ctx:
                inventory(root, AS_OF, today=TODAY)
        self.assertEqual(str(ctx.exception), meetings_status.BAD_ROOT)
        self.assertIsNone(ctx.exception.__cause__)
        self.assertTrue(ctx.exception.__suppress_context__)
        self.assertEqual(snapshot(moved), before)
        self.assertEqual((self.base / "sibling.txt").read_text(), "keep")

    def test_root_metadata_oserror_matrix_is_value_free(self):
        for kind in FAULTS:
            with self.subTest(kind=kind.__name__):
                patch, seen = fail_second_lstat(str(self.root), lambda: kind(13, SECRET, str(self.root)))
                with patch, mock.patch.object(meetings_status, "_staged", side_effect=AssertionError("scan")):
                    with self.assertRaises(MeetingsStatusError) as ctx:
                        inventory(str(self.root), AS_OF, today=TODAY)
                self.assertEqual(len(seen), 2)
                self.assertEqual(str(ctx.exception), meetings_status.BAD_ROOT)
                self.assertNotIn(SECRET, "".join(traceback.format_exception(ctx.exception)))

    def test_real_cli_text_and_json_root_faults_have_empty_stdout(self):
        before = snapshot(self.root)
        for kind in FAULTS:
            for extra in ((), ("--json",)):
                with self.subTest(kind=kind.__name__, extra=extra):
                    patch, _ = fail_second_lstat(str(self.root), lambda: kind(13, SECRET, str(self.root)))
                    with patch:
                        code, out, err = run_main("meetings-status", "--directory", str(self.root),
                                                  "--as-of", AS_OF, *extra)
                    self.assertEqual((code, out, err), (1, "", FIXED))
        self.assertEqual(snapshot(self.root), before)

    def test_invalid_as_of_wins_before_any_root_observation(self):
        for extra in ((), ("--json",)):
            patch, seen = fail_second_lstat(str(self.root), lambda: PermissionError(SECRET))
            with patch:
                code, out, err = run_main("meetings-status", "--directory", str(self.root),
                                          "--as-of", "2026-13-40", *extra)
            self.assertEqual((code, out, seen), (1, "", []))
            self.assertIn("--as-of must be a real YYYY-MM-DD date", err)

    def test_static_roots_and_healthy_output_unchanged(self):
        absent = self.base / "absent"
        plain = self.base / "plain.txt"
        plain.write_text("x")
        link = self.base / "link"
        os.symlink(self.root, link)
        for bad in (absent, plain, link):
            with self.subTest(root=bad.name):
                self.assertEqual(run_main("meetings-status", "--directory", str(bad)), (1, "", FIXED))
        empty = self.base / "empty"
        empty.mkdir()
        code, out, err = run_main("meetings-status", "--directory", str(empty), "--as-of", AS_OF)
        self.assertEqual((code, out, err), (0, "No staged meetings or unstaged matches. | private; review-required\n", ""))
        self.assertEqual(cli("meetings-status", "--directory", str(absent)).stderr, FIXED)

    def test_programming_errors_not_mislabeled_and_recovery_not_sticky(self):
        patch, _ = fail_second_lstat(str(self.root), lambda: RuntimeError("sentinel"))
        with patch, self.assertRaises(RuntimeError):
            inventory(str(self.root), AS_OF, today=TODAY)
        report = inventory(str(self.root), AS_OF, today=TODAY)
        self.assertTrue(report["private"])
        self.assertEqual(run_main("meetings-status", "--directory", str(self.root), "--as-of", AS_OF)[2], "")


if __name__ == "__main__":
    unittest.main()
