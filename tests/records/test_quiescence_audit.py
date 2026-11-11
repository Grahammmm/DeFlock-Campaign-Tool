"""DFGL-12B: records quiescence-audit, one failing check at a time, and its evidence accepted by records backup."""
import fcntl
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from campaign_tool.records import cli, quiescence_audit as qa
from tests.records.test_records_backup_profile import ROOT, RecordsBackupCli


def units_text(**states):
    return "\n\n".join("Id=%s\nActiveState=%s\nSubState=dead" % (u, states.get(u, "inactive")) for u in qa.WRITER_UNITS
                       if states.get(u) != "missing") + "\n"


class QuiescenceAudit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        RecordsBackupCli.setUpClass.__func__(cls)  # a project produced by the real pipeline
        cls.runtime = Path(cls.tmp.name) / "runtime"
        cls.runtime.mkdir()

    run_cli = RecordsBackupCli.run_cli

    def setUp(self):
        RecordsBackupCli.setUp(self)  # snapshots the project tree: the audit must not change it
        self.lock = self.runtime / "run-records.lock"
        self.lock.touch()
        self.pending = self.runtime / "arrival.pending"
        self.addCleanup(lambda: self.pending.exists() and self.pending.unlink())
        self.proc = self.work / "proc"
        self.proc.mkdir()
        patch = mock.patch.object(qa, "PROC_ROOT", str(self.proc))
        patch.start()
        self.addCleanup(patch.stop)

    def units(self, age=0, **states):
        path = self.work / "units.txt"
        path.write_text("Captured=%d\n\n" % (time.time() - age) + units_text(**states))
        return str(path)

    def audit(self, units=None):
        out = self.work / "e.json"
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = qa.main(["--project", str(self.project), "--runtime", str(self.runtime),
                            "--units-file", units or self.units(), "--out", str(out)])
        return code, stdout.getvalue().split(), json.loads(out.read_text())

    def assert_fails(self, code_name, **kw):
        code, lines, doc = self.audit(**kw)
        self.assertEqual(code, 1)
        self.assertIn(code_name, lines)
        self.assertIs(doc["all_writers_stopped"], False)
        return doc

    def test_all_quiet_passes_and_backup_accepts_it(self):
        own = self.proc / str(os.getpid()) / "fd"  # the auditor's own pid is excluded
        own.mkdir(parents=True)
        (own / "3").symlink_to(self.project / "processing-v1" / "ledger.sqlite")
        code, lines, doc = self.audit()
        self.assertEqual((code, doc["all_writers_stopped"], doc["roots"]), (0, True, [str(self.project)]))
        self.assertEqual(doc["checks"]["run_lock"], "free")
        self.assertEqual(oct((self.work / "e.json").stat().st_mode & 0o777), "0o600")
        digest = lines[-1].split("=")[1]
        code, line = self.run_cli("backup", "--project", str(self.project), "--into", str(self.work / "sets"),
                                  "--quiescence", str(self.work / "e.json"), "--quiescence-sha256", digest)
        self.assertEqual((code, line["code"]), (0, "OK"))

    def test_active_unit(self):
        self.assert_fails("WRITER_ACTIVE", units=self.units(**{"records-console.service": "active"}))

    def test_missing_unit(self):
        self.assert_fails("UNITS_INCOMPLETE", units=self.units(**{"records-arrival.path": "missing"}))

    def test_stale_or_undated_units_file(self):
        self.assert_fails("UNITS_STALE", units=self.units(age=300))
        undated = self.work / "undated.txt"
        undated.write_text(units_text())
        self.assert_fails("UNITS_STALE", units=str(undated))

    def test_out_path_through_symlinked_parent_into_project_is_refused(self):
        (self.work / "link").symlink_to(self.project)
        out = self.work / "link" / "e.json"
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = qa.main(["--project", str(self.project), "--runtime", str(self.runtime),
                            "--units-file", self.units(), "--out", str(out)])
        self.assertEqual((code, stdout.getvalue().split()), (2, ["OUT_INSIDE_PROJECT"]))
        self.assertFalse((self.project / "e.json").exists())

    def test_symlinked_project_still_sees_open_file(self):
        (self.work / "plink").symlink_to(self.project)
        (self.proc / "4242" / "fd").mkdir(parents=True)
        (self.proc / "4242" / "fd" / "3").symlink_to(self.project / "processing-v1" / "ledger.sqlite")  # as /proc shows it
        out = self.work / "e.json"
        with redirect_stdout(io.StringIO()) as stdout:
            code = qa.main(["--project", str(self.work / "plink"), "--runtime", str(self.runtime),
                            "--units-file", self.units(), "--out", str(out)])
        self.assertEqual(code, 1)
        self.assertIn("OPEN_FILE", stdout.getvalue().split())

    def test_open_file_under_project(self):
        for target, name in ((self.project / "processing-v1" / "ledger.sqlite", "fd/3"), (self.project, "cwd")):
            with self.subTest(name):
                pid = self.proc / "4242"
                (pid / "fd").mkdir(parents=True, exist_ok=True)
                (pid / name).symlink_to(target)
                doc = self.assert_fails("OPEN_FILE")
                self.assertEqual(doc["checks"]["open_files"]["count"], 1)
                (pid / name).unlink()

    def test_unreadable_pid_fails(self):
        (self.proc / "77").mkdir()
        (self.proc / "77" / "fd").write_text("")  # a descriptor directory that cannot be listed
        doc = self.assert_fails("PROC_UNREADABLE")
        self.assertEqual(doc["checks"]["open_files"]["proc_unreadable"], 1)

    def test_held_lock_fails_alone_and_free_lock_does_not_pass_alone(self):
        fd = os.open(self.lock, os.O_RDONLY)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        doc = self.assert_fails("RUN_LOCK_HELD")  # every other check passes
        self.assertEqual(doc["checks"]["run_lock"], "held")
        fcntl.flock(fd, fcntl.LOCK_UN)
        self.assertEqual(self.audit()[0], 0)
        self.assert_fails("WRITER_ACTIVE", units=self.units(**{"records-run.timer": "active"}))  # free lock, still fails

    def test_arrival_pending(self):
        self.pending.touch()
        self.assert_fails("ARRIVAL_PENDING")

    def test_never_calls_systemctl(self):
        with mock.patch("subprocess.Popen", side_effect=AssertionError("no subprocess")), \
                mock.patch("os.system", side_effect=AssertionError("no os.system")):
            self.assertEqual(self.audit()[0], 0)

    def test_registered_and_runs_through_module_entry(self):
        self.assertEqual(cli.COMMANDS["quiescence-audit"], ("quiescence_audit", "main"))
        out = self.work / "cli.json"
        text = Path(self.units(**{"records-run.service": "active"})).read_text()  # fed as a pipe, as docker exec -i does
        done = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.records", "quiescence-audit",
                               "--project", str(self.project), "--runtime", str(self.runtime),
                               "--units-file", "-", "--out", str(out)], cwd=ROOT, capture_output=True, text=True,
                              timeout=120, input=text)
        self.assertEqual((done.returncode, done.stderr), (1, ""))
        self.assertIn("WRITER_ACTIVE", done.stdout)
        self.assertIn("evidence_sha256=", done.stdout)
        self.assertIs(json.loads(out.read_text())["all_writers_stopped"], False)


if __name__ == "__main__":
    unittest.main()
