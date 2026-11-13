"""Offline mail-arrival acceptance: rendered hook, real stamps, real health CLI."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool.records import arrival, mail_arrival, run_orchestrator, run_report

ROOT = Path(__file__).resolve().parents[2]
KIT = ROOT / "deploy" / "records-runtime"
SPEC = importlib.util.spec_from_file_location("mail_runtime_render", KIT / "render.py")
assert SPEC is not None and SPEC.loader is not None
render = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(render)
RUN_ID = "run_20261113T180000Z_0123abcd"


class MailArrivalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mail-arrival-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.runtime = self.base / "runtime"
        self.runtime.mkdir(mode=0o700)
        self.status = self.base / "status.json"
        self.state = self.runtime / "mail-poll" / "state.json"
        self.values = {
            "RELEASE_COMMIT": "a" * 40, "ENGINE_DIR": str(ROOT),
            "DOCKER": "/usr/bin/docker", "TIMEOUT": shutil.which("timeout"),
            "FLOCK": shutil.which("flock"), "CONTAINER": "synthetic-records",
            "EXPECTED_IMAGE": "sha256:" + "0" * 64, "RUN_UID": str(os.getuid()),
            "RUNTIME_DIR": str(self.runtime), "HOST_PYTHON": sys.executable,
            "NOTIFY_PY": str(KIT / "notify.py"), "NOTIFY_ENV_FILE": str(self.base / "unused"),
            "NOTIFY_LABEL": "Synthetic", "DOCKER_WAIT_ATTEMPTS": "3", "DOCKER_WAIT_DELAY": "0",
            "DOCKER_WAIT_MAX_DELAY": "0", "DOCKER_CMD_TIMEOUT": "1", "DOCKER_WAIT_DEADLINE": "30",
            "NOTIFY_TIMEOUT": "30", "RUN_BUDGET_SECONDS": "600", "DEFAULT_TARGET": "TEST",
            "SCHEDULED_TARGET": "TEST", "RUN_RECORDS_SH": str(self.runtime / "run-records.sh"),
            "RECORDS_DROP_DIR": str(self.base / "drop"), "STUB_FIXTURE": str(self.base / "unused.json"),
            "MIN_FREE_BYTES": "67108864", "RECORDS_ROOT": str(self.base / "synthetic-records"),
        }
        self.out = self.base / "rendered"

    def render(self, **overrides):
        render.render({**self.values, **overrides}, [("TEST", KIT / "targets" / "stub.json.in")], self.out)
        return self.out / "mail-arrival-stamp.sh"

    def status_fixture(self, count=2, failures=0):
        self.status.write_text(json.dumps({"run_id": RUN_ID, "new_messages": count, "failures": failures}))

    def hook(self, script=None):
        result = subprocess.run(["/bin/sh", str(script or self.render()), str(self.status)],
                                cwd=self.base, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        return result

    def test_render_private_substituted_and_absent_without_drop(self):
        script = self.render()
        self.assertEqual(script.stat().st_mode & 0o777, 0o700)
        self.assertNotRegex(script.read_text(), r"@[A-Z_]+@")
        self.assertLessEqual(len((KIT / "mail-arrival-stamp.sh.in").read_text().splitlines()), 60)
        self.assertIn(str(self.runtime), script.read_text())
        self.assertIn(str(ROOT), script.read_text())
        values = dict(self.values)
        del values["RECORDS_DROP_DIR"]
        out = self.base / "no-drop"
        # Preserve the existing required-drop guard; a refused render writes nothing.
        with self.assertRaisesRegex(render.RenderError, "RECORDS_DROP_DIR"):
            render.render(values, [("TEST", KIT / "targets" / "stub.json.in")], out)
        self.assertFalse((out / "mail-arrival-stamp.sh").exists())
        with self.assertRaisesRegex(render.RenderError, "absolute path"):
            render.check_values({**self.values, "RUNTIME_DIR": "relative"})

    def test_positive_count_consumed_and_report_validates(self):
        self.status_fixture()
        self.assertEqual(self.hook().stderr, "")
        stamps = list((self.runtime / "arrival").glob("*-mail.json"))
        self.assertEqual(len(stamps), 1)
        data = json.loads(stamps[0].read_text())
        self.assertEqual(set(data), {"source", "ref", "seen_at"})
        self.assertEqual(data["source"], "mail")
        self.assertEqual(data["ref"], hashlib.sha256(f"{RUN_ID}\0{2}".encode("ascii")).hexdigest())
        self.assertEqual(stamps[0].stat().st_mode & 0o777, 0o600)
        self.assertEqual(arrival.pending(self.runtime), [data])
        self.assertTrue(arrival.has_work(self.runtime))
        self.assertEqual(arrival.claim(self.runtime, RUN_ID), [data])
        self.assertEqual(arrival.pending(self.runtime), [])
        report = run_orchestrator.abort_report(RUN_ID, "arrival", "2026-11-13T18:00:00Z",
                                              "DISK_SPACE_LOW", trigger="arrival", arrivals=[data])
        run_report.validate_run_report(report)
        report["arrivals"][0]["source"] = "unrecognized"
        with self.assertRaises(ValueError):
            run_report.validate_run_report(report)

    def test_zero_count_writes_health_state_and_rendered_health_uses_it(self):
        self.status_fixture(count=0)
        self.assertEqual(self.hook().stderr, "")
        self.assertFalse((self.runtime / "arrival").exists())
        self.assertEqual(set(json.loads(self.state.read_text())), {"updated_at"})
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.state.parent.stat().st_mode & 0o777, 0o700)
        unit = (self.out / "records-health.service").read_text()
        command = shlex.split(next(line.removeprefix("ExecStart=") for line in unit.splitlines()
                                   if line.startswith("ExecStart=")))
        command.remove("--record-alerts")
        command.append("--json")
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.stderr, "")
        report = json.loads(result.stdout)
        poll = next(check for check in report["checks"] if check["check"] == "mail_poll")
        self.assertEqual((poll["state"], poll["reason_code"]), ("ok", "mail_poll_recent"))
        # Other health checks stay unknown in this empty synthetic runtime.
        self.assertEqual(result.returncode, report["exit_code"])

    def test_unreadable_or_invalid_status_is_one_fixed_line_without_writes(self):
        script = self.render()
        good = {"run_id": RUN_ID, "new_messages": 2, "failures": 0}
        for raw in (None, "{", "[]", "x" * 4097, json.dumps({**good, "new_messages": True}),
                    json.dumps({**good, "new_messages": -1}), json.dumps({**good, "failures": "error"}),
                    json.dumps({**good, "subject": "synthetic-private"}),
                    json.dumps({**good, "run_id": "synthetic@example.invalid"})):
            with self.subTest(raw=raw):
                if raw is not None:
                    self.status.write_text(raw)
                self.assertEqual(self.hook(script).stderr, "MAIL_STATUS_UNREADABLE\n")
                self.assertFalse(self.state.exists())
                self.assertFalse((self.runtime / "arrival").exists())

    def test_symlink_status_is_not_followed(self):
        target = self.base / "other.json"
        target.write_text(json.dumps({"run_id": RUN_ID, "new_messages": 2, "failures": 0}))
        self.status.symlink_to(target)
        self.assertEqual(self.hook().stderr, "MAIL_STATUS_UNREADABLE\n")
        self.assertFalse(self.state.exists())

    def test_failed_poll_preserves_success_clock_but_stamps_partial_arrivals(self):
        self.status_fixture(count=0)
        script = self.render()
        self.hook(script)
        before = self.state.read_bytes()
        self.status_fixture(count=2, failures=1)
        self.assertEqual(self.hook(script).stderr, "MAIL_POLL_FAILED\n")
        self.assertEqual(self.state.read_bytes(), before)
        self.assertEqual(len(arrival.pending(self.runtime)), 1)

    def test_stamp_and_state_failures_are_nonfatal_and_independent(self):
        self.status_fixture()
        (self.runtime / "arrival").write_text("not a directory")
        self.assertEqual(self.hook().stderr, "MAIL_STAMP_FAILED\n")
        self.assertTrue(self.state.exists())
        (self.runtime / "arrival").unlink()
        self.state.unlink()
        self.state.parent.rmdir()
        self.state.parent.symlink_to(self.base, target_is_directory=True)
        self.assertEqual(self.hook(self.out / "mail-arrival-stamp.sh").stderr, "MAIL_STATE_UNWRITABLE\n")
        self.assertEqual(len(arrival.pending(self.runtime)), 1)
        self.assertFalse((self.base / "state.json").exists())

    def test_stamp_timeout_is_fixed_code_and_nonfatal(self):
        self.status_fixture()
        stderr = io.StringIO()
        with mock.patch.object(mail_arrival.subprocess, "run", side_effect=subprocess.TimeoutExpired("stamp", 15)), \
                contextlib.redirect_stderr(stderr):
            self.assertEqual(mail_arrival.main(["--runtime-dir", str(self.runtime), str(self.status)]), 0)
        self.assertEqual(stderr.getvalue(), "MAIL_STAMP_FAILED\n")
        self.assertTrue(self.state.exists())


if __name__ == "__main__":
    unittest.main()
