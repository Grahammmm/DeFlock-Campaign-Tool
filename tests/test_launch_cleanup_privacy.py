"""Cleanup of the launch checker's temporary rehearsal tree must fail with a fixed, value-free refusal."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import call, patch

from campaign_tool import cli, launch_check
from tests import test_doctor_launch as base

MESSAGE = ("Temporary launch rehearsal could not be removed (filesystem error); "
           "temporary files may remain")
CANARY = "SYNTHETIC-CLEANUP-PAYLOAD"


class Unrenderable:
    def __str__(self):
        raise AssertionError("cleanup payload must not be rendered")

    __repr__ = __str__


class LaunchCleanupPrivacyTests(unittest.TestCase):
    def setUp(self):
        self.fx = base.DoctorLaunchTests("test_complete_campaign_and_no_public_tree")
        self.fx.setUp()
        self.addCleanup(self.fx.temp.cleanup)
        self.root = self.fx.root
        self.trees = []
        real_mkdtemp = tempfile.mkdtemp

        def mkdtemp(*args, **kwargs):
            path = real_mkdtemp(*args, **kwargs)
            if kwargs.get("prefix") == "campaign-launch-":
                self.trees.append(Path(path))
            return path

        patcher = patch.object(launch_check.tempfile, "mkdtemp", mkdtemp)
        patcher.start()
        self.addCleanup(self.sweep)
        self.addCleanup(patcher.stop)

    def sweep(self):  # test-owned residue only
        for tree in self.trees:
            shutil.rmtree(tree, ignore_errors=True)

    def state(self):
        out = {}
        for p in sorted(self.root.rglob("*")):
            st = p.lstat()
            out[str(p.relative_to(self.root))] = (
                p.read_bytes() if p.is_file() and not p.is_symlink() else None, st.st_mode, st.st_mtime_ns,
                os.readlink(p) if p.is_symlink() else None)
        return out

    def assert_fixed(self, exc):
        self.assertEqual(str(exc), MESSAGE)
        self.assertTrue(exc.__suppress_context__)
        self.assertIsNone(exc.__cause__)

    @contextlib.contextmanager
    def unlink_fault(self):
        real = os.unlink
        hits = []

        def unlink(path, *args, **kwargs):
            if path == "campaign.json" and "dir_fd" in kwargs:
                hits.append(path)
                raise PermissionError(13, CANARY, str(self.trees[-1] / "SYNTHETIC-PRIVATE-NAME"))
            return real(path, *args, **kwargs)

        with patch("os.unlink", unlink):
            yield hits

    def test_real_cleanup_unlink_failure_refuses_without_payload(self):
        before = self.state()
        with self.unlink_fault() as hits, self.assertRaises(ValueError) as ctx:
            launch_check.launch_checks(self.root, today=base.TODAY)
        self.assertTrue(hits)
        self.assert_fixed(ctx.exception)
        self.assertEqual(len(self.trees), 1)
        self.assertTrue(self.trees[0].exists())  # honest residue
        self.assertEqual(self.state(), before)

    def test_cleanup_refusal_after_build_or_scan_failure_is_private(self):
        for target, exc in (("launch_check.build_site", ValueError("earlier build problem")),
                            ("cli.check_public_tree", OSError(13, "earlier", "/earlier/path")),
                            ("launch_check.check_links", ValueError("earlier link problem"))):
            with self.subTest(target=target):
                with patch("campaign_tool." + target, side_effect=exc), \
                        patch.object(launch_check.shutil, "rmtree", side_effect=PermissionError(13, CANARY, "/x")):
                    with self.assertRaises(ValueError) as ctx:
                        launch_check.launch_checks(self.root, today=base.TODAY)
                self.assert_fixed(ctx.exception)

    def test_real_cli_dispatch_cleanup_failure_has_exact_stderr(self):
        out, err = io.StringIO(), io.StringIO()
        argv = ["campaign_tool", "doctor", "--launch", "--directory", str(self.root)]
        with self.unlink_fault(), patch.object(sys, "argv", argv), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as ctx:
                cli.main()
        self.assertEqual(ctx.exception.code, 1)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue(), f"Stopped: {MESSAGE}\n")

    def test_healthy_subprocess_keeps_exit_zero_json(self):
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "doctor", "--launch",
                                 "--directory", str(self.root)], cwd=base.REPO, capture_output=True,
                                text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(json.loads(result.stdout)["launch"]["checks"]), 9)

    def test_cleanup_oserror_subclasses_and_aggregate_details_are_value_free(self):
        errors = [FileNotFoundError(2, CANARY, "/p/SYNTHETIC-PRIVATE-NAME"),
                  PermissionError(13, CANARY, "/p/a", None, "/p/b"),
                  OSError(Unrenderable()),
                  shutil.Error([("/src/" + CANARY, "/dst/" + CANARY, Unrenderable())])]
        for err in errors:
            with self.subTest(error=type(err).__name__):
                with patch.object(launch_check.shutil, "rmtree", side_effect=err):
                    with self.assertRaises(ValueError) as ctx:
                        launch_check.launch_checks(self.root, today=base.TODAY)
                self.assert_fixed(ctx.exception)

    def test_cleanup_scope_allocation_and_validation_guards(self):
        with patch.object(launch_check.shutil, "rmtree", wraps=shutil.rmtree) as rm:
            launch_check.launch_checks(self.root, today=base.TODAY)
        self.assertEqual([c for c in rm.call_args_list if c.args == (self.trees[0],)], [call(self.trees[0])])
        self.assertFalse(self.trees[0].exists())
        with patch.object(launch_check.shutil, "rmtree") as rm, \
                patch.object(launch_check.tempfile, "mkdtemp", side_effect=OSError(13, CANARY)):
            report = launch_check.launch_checks(self.root, today=base.TODAY)
        rm.assert_not_called()
        self.assertEqual(len(report["checks"]), 9)
        shutil.rmtree(self.root / "content")
        with patch.object(launch_check.shutil, "rmtree") as rm:
            launch_check.launch_checks(self.root, today=base.TODAY)
        rm.assert_not_called()

    def test_cleanup_non_oserror_and_unrelated_errors_are_not_reclassified(self):
        for exc in (RuntimeError("x"), KeyboardInterrupt(), SystemExit(5)):
            with self.subTest(error=type(exc).__name__):
                with patch.object(launch_check.shutil, "rmtree", side_effect=exc):
                    with self.assertRaises(type(exc)) as ctx:
                        launch_check.launch_checks(self.root, today=base.TODAY)
                self.assertIs(ctx.exception, exc)

    def test_normal_and_failed_rehearsals_keep_existing_contracts(self):
        report = launch_check.launch_checks(self.root, today=base.TODAY)
        self.assertEqual([r["check"] for r in report["checks"]], base.ORDER)
        self.assertFalse(self.trees[0].exists())
        with patch.object(launch_check, "build_site", side_effect=OSError(13, CANARY, "/x")):
            report = launch_check.launch_checks(self.root, today=base.TODAY)
        rows = {r["check"]: r for r in report["checks"]}
        self.assertEqual(rows["build_check"]["state"], "fail")
        self.assertNotIn(CANARY, json.dumps(report))
        self.assertFalse(self.trees[1].exists())


if __name__ == "__main__":
    unittest.main()
