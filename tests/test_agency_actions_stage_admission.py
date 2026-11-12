"""RF1801-1: the agency-action staging basename must still be the file this edit created before promotion (synthetic)."""
import contextlib
import io
import os
import sys
import unittest
from unittest import mock

from campaign_tool import agency_actions as actions
from campaign_tool.agency_actions import ActionError, add_action, remove_action
from tests import test_agency_actions as fx

POLICE, TODAY = fx.POLICE, fx.TODAY
ADD = dict(on="2026-09-02", kind="policy_adopted", as_stated="Another", source_url="https://example.invalid/x",
           checked_on="2026-10-01", today=TODAY)


class StageAdmissionTests(fx.Base):
    def stages(self):
        return sorted(p for p in self.path.parent.iterdir() if p.name.startswith(".agency-actions-"))

    def substitute(self, how):
        """Patch fsync so, after the real call, the staging entry is swapped by ``how(stage_path)``."""
        real_fsync, state = os.fsync, {}

        def hook(fd):
            result = real_fsync(fd)
            if not state:
                state["done"] = True
                how(self.stages()[0])
            return result

        return mock.patch("campaign_tool.agency_actions.os.fsync", side_effect=hook)

    def run_op(self, op):
        if op == "add":
            return add_action(self.root, POLICE, **ADD)
        return remove_action(self.root, POLICE, 1, today=TODAY)

    def refuse(self, how, op, expect_foreign=True):
        self.put(fx.document(fx.row()))
        before = self.path.read_bytes()
        inode = self.path.stat().st_ino
        with self.substitute(how), mock.patch("campaign_tool.agency_actions.os.replace") as replace:
            with self.assertRaisesRegex(ActionError, "changed while it was being edited; nothing was written"):
                self.run_op(op)
        replace.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.path.stat().st_ino, inode)
        if expect_foreign:
            self.assertEqual(len(self.stages()), 1)

    def test_add_remove_refuse_foreign_regular_stage_before_replace(self):
        def regular(stage):
            stage.unlink()
            stage.write_bytes(b"NOT JSON FOREIGN")

        def valid_json(stage):
            stage.unlink()
            stage.write_bytes(actions._dump([]))

        for op in ("add", "remove"):
            for how in (regular, valid_json):
                with self.subTest(op=op, how=how.__name__):
                    self.refuse(how, op)
                    self.assertTrue(self.stages()[0].read_bytes())
                    self.stages()[0].unlink()

    def test_symlink_fifo_directory_foreign_hardlink_refuse(self):
        sentinel = self.tmp / "sentinel"
        sentinel.write_bytes(b"SENTINEL")

        def symlink(stage):
            stage.unlink()
            stage.symlink_to(sentinel)

        def dangling(stage):
            stage.unlink()
            stage.symlink_to(self.tmp / "nowhere")

        def fifo(stage):
            stage.unlink()
            os.mkfifo(stage)

        def directory(stage):
            stage.unlink()
            stage.mkdir()

        def hardlink(stage):
            stage.unlink()
            os.link(sentinel, stage)

        for op in ("add", "remove"):
            for how in (symlink, dangling, fifo, directory, hardlink):
                with self.subTest(op=op, how=how.__name__):
                    self.refuse(how, op)
                    stage = self.stages()[0]
                    if stage.is_dir() and not stage.is_symlink():
                        stage.rmdir()
                    else:
                        stage.unlink()
                    self.assertEqual(sentinel.read_bytes(), b"SENTINEL")

    def test_missing_stage_and_owned_inode_extra_link_refuse(self):
        for op in ("add", "remove"):
            with self.subTest(op=op, case="missing"):
                self.refuse(lambda stage: stage.unlink(), op, expect_foreign=False)
                self.assertEqual(self.stages(), [])
        extra = self.tmp / "extra-link"
        for op in ("add", "remove"):
            with self.subTest(op=op, case="extra"):
                self.refuse(lambda stage: os.link(stage, extra), op, expect_foreign=False)
                self.assertTrue(extra.exists())  # the added link is retained
                self.assertEqual(self.stages(), [])  # our own staging name is cleaned
                extra.unlink()

    def test_first_add_and_last_remove_have_same_admission(self):
        self.assertFalse(self.path.exists())
        with self.substitute(lambda s: (s.unlink(), s.write_bytes(b"X"))), \
                self.assertRaisesRegex(ActionError, "changed while"):
            add_action(self.root, POLICE, **ADD)
        self.assertFalse(self.path.exists())
        add_action(self.root, POLICE, **ADD)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o644)
        remove_action(self.root, POLICE, 1, today=TODAY)
        self.assertEqual(self.path.read_bytes(), actions._dump([]))

    def test_final_stage_stat_fstat_errors_are_private(self):
        real_stat = os.stat
        for exc in (PermissionError(13, "SECRET-OS-MESSAGE"), OSError(5, "SECRET-OS-MESSAGE")):
            self.put(fx.document(fx.row()))
            before = self.path.read_bytes()
            seen = {"fsynced": False}
            real_fsync = os.fsync

            def fsync(fd):
                seen["fsynced"] = True
                return real_fsync(fd)

            def stat_(path, *a, **k):
                if seen["fsynced"] and isinstance(path, str) and path.startswith(".agency-actions-") \
                        and not seen.get("raised"):
                    seen["raised"] = True
                    raise exc
                return real_stat(path, *a, **k)

            with self.subTest(exc=type(exc).__name__), \
                    mock.patch("campaign_tool.agency_actions.os.fsync", side_effect=fsync), \
                    mock.patch("campaign_tool.agency_actions.os.stat", side_effect=stat_), \
                    mock.patch("campaign_tool.agency_actions.os.replace") as replace:
                with self.assertRaises(ActionError) as caught:
                    remove_action(self.root, POLICE, 1, today=TODAY)
            replace.assert_not_called()
            self.assertNotIn("SECRET", str(caught.exception))
            self.assertIn("write failed", str(caught.exception))
            self.assertEqual(self.path.read_bytes(), before)

    def test_real_cli_refusal_has_no_success_footer(self):
        from campaign_tool import cli
        self.put(fx.document(fx.row()))
        before = self.path.read_bytes()
        argv = ["campaign_tool", "content", "agency-action", "remove", "--agency", POLICE, "--index", "1",
                "--directory", str(self.root)]
        out, err = io.StringIO(), io.StringIO()
        with self.substitute(lambda s: (s.unlink(), s.write_bytes(b"FOREIGN"))), mock.patch.object(sys, "argv", argv), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as code:
                cli.main()
        self.assertEqual(code.exception.code, 1)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("changed while it was being edited", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())
        self.assertEqual(self.path.read_bytes(), before)

    def test_descriptors_close_and_healthy_edit_unchanged(self):
        self.put(fx.document(fx.row()))
        before = set(os.listdir("/proc/self/fd"))
        result = add_action(self.root, POLICE, **ADD)
        self.assertEqual(result["index"], 2)
        with self.assertRaises(ActionError):
            with self.substitute(lambda s: (s.unlink(), s.write_bytes(b"F"))):
                remove_action(self.root, POLICE, 1, today=TODAY)
        self.assertEqual(set(os.listdir("/proc/self/fd")) - before, set())
        self.stages()[0].unlink()


if __name__ == "__main__":
    unittest.main()
