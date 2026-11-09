"""Synthetic-only replay immutability of the per-run comparison pair (RF0854-3)."""
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch

from campaign_tool.records.outcomes import comparison as c
from tests.records import test_outcome_comparison as base

RUN = "synthetic-run"


class FixedDay(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 5, 1, 12, 0)


class ReplayImmutability(unittest.TestCase):
    def setUp(self):
        helper = base.OutputsStage("test_absent_fact_sheets_emit_unknown_not_empty_success")
        self.helper = helper
        self.project = helper.project()
        self.addCleanup(shutil.rmtree, self.project, True)
        self.out = self.project / "run-outputs" / RUN
        self.json, self.md = self.out / "comparison.json", self.out / "comparison.md"

    def state(self, *paths):
        return {p.name: (p.lstat().st_ino, p.lstat().st_mode, p.lstat().st_mtime_ns, p.read_bytes()) for p in paths}

    def write(self, run=RUN):
        return c.write_run_comparison(self.project, run)

    def change_input(self):
        path = self.project / c.INPUT_NAME
        data = json.loads(path.read_text())
        row = data["agencies"][0]["facts"]["usage_policy_published"]["value"]
        row["written_public"] = not row["written_public"]
        path.write_text(json.dumps(data))

    def test_identical_replay_preserves_both_inodes_modes_and_bytes(self):
        self.write()
        sibling = self.out / "other-report.txt"
        sibling.write_text("sibling sentinel")
        before = self.state(self.json, self.md, sibling)
        with patch.object(c.os, "link", side_effect=AssertionError("no install on replay")), \
                patch.object(c.tempfile, "mkdtemp", side_effect=AssertionError("no staging on replay")):
            self.assertEqual(self.write()["run_id"], RUN)
        self.assertEqual(self.state(self.json, self.md, sibling), before)
        self.assertEqual(sorted(p.name for p in self.out.iterdir()),
                         ["comparison.json", "comparison.md", "other-report.txt"])
        self.assertEqual(self.json.stat().st_mode & 0o777, 0o600)

    def test_changed_rating_input_or_asof_refuses_whole_existing_pair(self):
        self.write()
        sibling = self.out / "other-report.txt"
        sibling.write_text("sibling sentinel")
        before = self.state(self.json, self.md, sibling)
        self.change_input()
        with self.assertRaisesRegex(OSError, "comparison_run_already_exists"):
            self.write()
        self.assertEqual(self.state(self.json, self.md, sibling), before)
        # A changed captured as-of day under the same run identity is also a conflict.
        (self.project / c.INPUT_NAME).write_bytes(base.FIXTURE.read_bytes())
        with patch.object(c, "datetime", FixedDay), self.assertRaisesRegex(OSError, "comparison_run_already_exists"):
            self.write()
        self.assertEqual(self.state(self.json, self.md, sibling), before)
        # Changed canonical inventory: a new ledger agency.
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(self.project / "processing-v1/ledger.sqlite")) as db, db:
            db.execute("INSERT INTO agencies(id,name,jurisdiction) VALUES(?,?,?)", ("synthetic-new", "Invented New Agency", "us-ca"))
        with self.assertRaisesRegex(OSError, "comparison_run_already_exists"):
            self.write()
        self.assertEqual(self.state(self.json, self.md, sibling), before)
        # A fresh run id represents the changed data and succeeds beside the old run.
        self.assertEqual(self.write("synthetic-run-2")["run_id"], "synthetic-run-2")
        self.assertEqual(self.state(self.json, self.md, sibling), before)

    def test_partial_or_unsafe_pair_is_not_repaired(self):
        victim = self.project / "untouched.txt"
        victim.write_text("do not read or modify")
        for case in ("json-only", "md-only", "directory", "fifo", "symlink-leaf", "symlink-md"):
            with self.subTest(case=case):
                shutil.rmtree(self.out, ignore_errors=True)
                self.out.mkdir(parents=True)
                if case == "json-only":
                    self.json.write_text("{}")
                elif case == "md-only":
                    self.md.write_text("old")
                elif case == "directory":
                    self.json.mkdir()
                elif case == "fifo":
                    os.mkfifo(self.json)
                elif case == "symlink-leaf":
                    self.json.symlink_to(victim)
                else:
                    self.md.symlink_to(victim)
                names = sorted(p.name for p in self.out.iterdir())
                with patch.object(Path, "open", autospec=True, side_effect=Path.open) as opened:
                    with self.assertRaises(OSError):
                        self.write()
                    self.assertFalse(any(call.args[0] == victim for call in opened.call_args_list))
                self.assertEqual(sorted(p.name for p in self.out.iterdir()), names)
                self.assertEqual(victim.read_text(), "do not read or modify")
        self.assertEqual(self.json.is_symlink() or self.md.is_symlink(), True)

    def test_symlinked_run_directory_refused(self):
        (self.project / "run-outputs").mkdir()
        self.out.symlink_to(self.project / "processing-v1", target_is_directory=True)
        before = sorted(p.name for p in (self.project / "processing-v1").iterdir())
        with self.assertRaises(OSError):
            self.write()
        self.assertEqual(sorted(p.name for p in (self.project / "processing-v1").iterdir()), before)

    def test_second_install_failure_rolls_back_only_own_first_file(self):
        self.out.mkdir(parents=True)
        sibling = self.out / "other-report.txt"
        sibling.write_text("sibling sentinel")
        real = os.link
        calls = []

        def failing(src, dst, *args, **kwargs):
            calls.append(Path(dst).name)
            if Path(dst).name == "comparison.md":
                raise OSError("synthetic-second-install-failure")
            return real(src, dst, *args, **kwargs)
        with patch.object(c.os, "link", side_effect=failing), self.assertRaisesRegex(OSError, "synthetic-second"):
            self.write()
        self.assertEqual(calls, ["comparison.json", "comparison.md"])
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ["other-report.txt"])
        self.assertEqual(sibling.read_text(), "sibling sentinel")
        # The failed fresh run left no completed pair, so a rerun publishes normally.
        self.write()
        self.assertTrue(self.json.is_file() and self.md.is_file())

    def test_rollback_never_removes_a_competing_writers_replacement(self):
        self.out.mkdir(parents=True)
        real = os.link

        def failing(src, dst, *args, **kwargs):
            if Path(dst).name == "comparison.md":
                self.json.unlink()
                self.json.write_text("foreign replacement")
                raise OSError("synthetic-second-install-failure")
            return real(src, dst, *args, **kwargs)
        with patch.object(c.os, "link", side_effect=failing), self.assertRaises(OSError):
            self.write()
        self.assertEqual(self.json.read_text(), "foreign replacement")
        self.assertFalse(self.md.exists())

    def test_occupied_target_and_competing_writer_are_never_clobbered(self):
        self.out.mkdir(parents=True)
        residue = self.out / ".comparison.json.tmp"
        residue.write_text("unrelated residue")
        real = os.link

        def arriving(src, dst, *args, **kwargs):
            if Path(dst).name == "comparison.json":
                Path(dst).write_text("competing completed pair member")
            return real(src, dst, *args, **kwargs)
        with patch.object(c.os, "link", side_effect=arriving), self.assertRaisesRegex(OSError, "comparison_run_already_exists"):
            self.write()
        self.assertEqual(self.json.read_text(), "competing completed pair member")
        self.assertFalse(self.md.exists())
        self.assertEqual(residue.read_text(), "unrelated residue")
        # Arrival of the second member after the first install: our first file is withdrawn, theirs kept.
        shutil.rmtree(self.out)
        self.out.mkdir()

        def arriving_md(src, dst, *args, **kwargs):
            if Path(dst).name == "comparison.md":
                Path(dst).write_text("competing markdown")
            return real(src, dst, *args, **kwargs)
        with patch.object(c.os, "link", side_effect=arriving_md), self.assertRaisesRegex(OSError, "comparison_run_already_exists"):
            self.write()
        self.assertFalse(self.json.exists())
        self.assertEqual(self.md.read_text(), "competing markdown")

    def test_real_outputs_stage_reports_refusal_and_keeps_derived_bindings(self):
        self.assertEqual(self.helper.invoke(self.project)[1], 0)
        derived = self.out / "outcomes" / "received-items.json"
        frozen = {p.name: (p.stat().st_ino, hashlib.sha256(p.read_bytes()).hexdigest())
                  for p in (self.json, self.md, derived) if p.exists()}
        self.assertIn("received-items.json", frozen)
        self.assertEqual(self.helper.invoke(self.project)[1], 0)
        self.assertEqual({p.name: (p.stat().st_ino, hashlib.sha256(p.read_bytes()).hexdigest())
                          for p in (self.json, self.md, derived) if p.exists()}, frozen)
        self.change_input()
        result, code = self.helper.invoke(self.project)
        self.assertEqual((code, result["error_code"]), (1, "OUTPUTS_WRITE_FAILED"))
        self.assertEqual({p.name: (p.stat().st_ino, hashlib.sha256(p.read_bytes()).hexdigest())
                          for p in (self.json, self.md, derived) if p.exists()}, frozen)
        # A fresh run id accepts the changed input.
        self.assertEqual(self.helper.invoke(self.project, "synthetic-run-2")[1], 0)
        self.assertTrue((self.project / "run-outputs/synthetic-run-2/comparison.json").is_file())


if __name__ == "__main__":
    unittest.main()
