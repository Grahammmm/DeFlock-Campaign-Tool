"""Records runtime wrapper robustness (plan T10; Oct 16/17 regressions).

Uses a fake docker shim and a loopback fake Telegram server only.
"""
import http.server
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "deploy" / "records-runtime"
FAKE_TOKEN = "123456:SYNTHETIC-TEST-TOKEN"
FAKE_IMAGE = "sha256:" + "0" * 64

FAKE_DOCKER = r"""#!/bin/sh
# Synthetic docker shim. State lives in $STATE (rendered absolute path).
STATE=@STATE@
echo "$*" >>"$STATE/calls"
hang() {
  # Simulate an unresponsive daemon: a child that outlives a naive kill.
  [ -e "$STATE/hang-$1" ] || return 0
  sleep 30 & echo $! >"$STATE/hang-pid"; wait; exit 0
}
case "$1" in
  info)
    hang info
    n=$(cat "$STATE/info-count" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" >"$STATE/info-count"
    up_after=$(cat "$STATE/up-after" 2>/dev/null || echo 999999)
    [ "$n" -ge "$up_after" ] || { echo "Cannot connect to the Docker daemon" >&2; exit 1; }
    exit 0;;
  inspect)
    case "$*" in
      *State.Running*) hang inspect-running; echo true;;
      *Image*) hang inspect-image; cat "$STATE/image";;
    esac
    exit 0;;
  exec)
    exit "$(cat "$STATE/exec-rc")";;
esac
exit 2
"""


class FakeTelegram(http.server.BaseHTTPRequestHandler):
    calls = []
    mode = "ok"

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        FakeTelegram.calls.append((self.path, self.rfile.read(length).decode()))
        good = self.path == f"/bot{FAKE_TOKEN}/sendMessage" and FakeTelegram.mode == "ok"
        body = json.dumps({"ok": good} if good else {"ok": False, "error_code": 401}).encode()
        self.send_response(200 if good else 401)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002
        pass


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class RecordsRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeTelegram)
        cls.api = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        FakeTelegram.calls = []
        FakeTelegram.mode = "ok"
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.tmp)])
        self.state = self.tmp / "state"
        self.state.mkdir()
        (self.state / "image").write_text(FAKE_IMAGE + "\n")
        (self.state / "exec-rc").write_text("0\n")
        (self.state / "up-after").write_text("1\n")
        docker = self.tmp / "docker"
        docker.write_text(FAKE_DOCKER.replace("@STATE@", str(self.state)))
        docker.chmod(stat.S_IRWXU)
        self.runtime = self.tmp / "runtime"
        self.env_file = self.tmp / "telegram.env"
        self.env_file.write_text(f"TELEGRAM_BOT_TOKEN={FAKE_TOKEN}\nTELEGRAM_HOME_CHANNEL=-100\n")
        self.values = {
            "RELEASE_COMMIT": "a" * 40, "RELEASE_DIR": "/synthetic/release",
            "RECORDS_ROOT": "/synthetic/root", "PARSERS_DIR": "/synthetic/parsers",
            "PRIVATE_TMP": "/synthetic/tmp", "DOCKER": str(docker),
            "CONTAINER": "synthetic-records", "EXPECTED_IMAGE": FAKE_IMAGE,
            "RUN_UID": str(os.getuid()), "RUNTIME_DIR": str(self.runtime),
            "HOST_PYTHON": sys.executable, "NOTIFY_PY": str(RUNTIME / "notify.py"),
            "NOTIFY_ENV_FILE": str(self.env_file), "NOTIFY_LABEL": "Synthetic",
            "DOCKER_WAIT_ATTEMPTS": "3", "DOCKER_WAIT_DELAY": "0",
            "DOCKER_WAIT_MAX_DELAY": "0", "TIMEOUT": shutil.which("timeout"),
            "DOCKER_CMD_TIMEOUT": "1", "DOCKER_WAIT_DEADLINE": "30",
            "NOTIFY_TIMEOUT": "30",
        }
        self.script = self.tmp / "run-records.sh"
        self.render()

    def render(self, **overrides):
        text = (RUNTIME / "run-records.sh.in").read_text()
        for key, value in {**self.values, **overrides}.items():
            text = text.replace(f"@{key}@", value)
        self.assertIsNone(re.search(r"@[A-Z_]+@", text), "unrendered placeholder")
        self.script.write_text(text)

    def fake_helper(self, body, **overrides):
        helper = self.tmp / "fake_notify.py"
        helper.write_text(body)
        self.render(NOTIFY_PY=str(helper), **overrides)

    def assert_fallback_receipts(self, reason, status, code):
        report = self.report()
        self.assertEqual((report["status"], report["exit_code"]), (status, code))
        self.assertTrue(report["notify_failed"])
        self.assertEqual(report["notify"]["reason"], reason)
        [alert] = self.alerts()
        data = json.loads(alert.read_text())
        self.assertEqual(
            (data["kind"], data["reason"], data["run_status"], data["run_exit_code"]),
            ("notify_failed", reason, status, code))
        self.assertEqual(data["run_id"], report["run_id"])
        log = (self.runtime / "run-records.log").read_text()
        self.assertIn(f"notify: FAILED reason={reason}", log)

    def assert_hang_child_gone(self):
        pid = int((self.state / "hang-pid").read_text())
        for _ in range(30):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.1)
        self.fail("hung docker child survived the command deadline")

    def run_script(self, api=None):
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
               "RECORDS_NOTIFY_API_BASE": api or self.api}
        return subprocess.run(["/bin/sh", str(self.script)], env=env,
                              capture_output=True, text=True, timeout=60)

    def report(self):
        return json.loads((self.runtime / "run-report.json").read_text())

    def alerts(self):
        return sorted((self.runtime / "alerts").glob("notify-failed-*.json"))

    def calls(self):
        return (self.state / "calls").read_text().splitlines()

    # --- healthy baseline -------------------------------------------------
    def test_healthy_run_sends_completion_and_exits_zero(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.report()
        self.assertEqual((report["status"], report["exit_code"]), ("ok", 0))
        self.assertFalse(report["notify_failed"])
        self.assertEqual(report["notify"]["status"], "sent")
        self.assertEqual(self.alerts(), [])
        self.assertEqual(len(FakeTelegram.calls), 1)
        self.assertIn("completed", FakeTelegram.calls[0][1])
        self.assertNotIn("failed", FakeTelegram.calls[0][1])
        self.assertTrue(any(c.startswith("exec ") for c in self.calls()))

    # --- Oct 17: Docker not up yet after reboot ---------------------------
    def test_docker_down_retries_then_fails_cleanly(self):
        (self.state / "up-after").write_text("999\n")
        result = self.run_script()
        self.assertEqual(result.returncode, 75)
        self.assertIn("not ready after 3/3 attempts", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(sum(c == "info" for c in self.calls()), 3)
        self.assertFalse(any(c.startswith("exec ") for c in self.calls()))
        report = self.report()
        self.assertEqual(report["status"], "docker_unavailable")
        self.assertEqual(report["docker_wait"],
                         {"attempts": 3, "max_attempts": 3, "deadline_seconds": 30})
        self.assertIn("Docker", FakeTelegram.calls[0][1])
        log = (self.runtime / "run-records.log").read_text()
        self.assertEqual(log.count("retrying"), 2)

    def test_docker_comes_up_within_bound(self):
        (self.state / "up-after").write_text("3\n")
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.report()["docker_wait"]["attempts"], 3)
        self.assertTrue(any(c.startswith("exec ") for c in self.calls()))

    def test_docker_down_and_telegram_down_still_exit_75(self):
        (self.state / "up-after").write_text("999\n")
        result = self.run_script(api=f"http://127.0.0.1:{free_port()}")
        self.assertEqual(result.returncode, 75)
        self.assertTrue(self.report()["notify_failed"])
        self.assertEqual(len(self.alerts()), 1)

    # --- Oct 16: Telegram failure must not fail the run --------------------
    def test_bad_telegram_token_run_completes_and_flags_notify_failed(self):
        self.env_file.write_text("TELEGRAM_BOT_TOKEN=999:WRONG\nTELEGRAM_HOME_CHANNEL=-100\n")
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.report()
        self.assertEqual(report["status"], "ok")
        self.assertTrue(report["notify_failed"])
        self.assertEqual(report["notify"]["reason"], "http_401")
        [alert] = self.alerts()
        data = json.loads(alert.read_text())
        self.assertEqual((data["kind"], data["reason"], data["run_status"]),
                         ("notify_failed", "http_401", "ok"))
        log = (self.runtime / "run-records.log").read_text()
        self.assertIn("notify: FAILED reason=http_401", log)
        for path in [alert, self.runtime / "run-report.json", self.runtime / "run-records.log"]:
            self.assertNotIn("WRONG", path.read_text())
        self.assertNotIn("WRONG", result.stderr)

    def test_telegram_unreachable_is_non_fatal(self):
        result = self.run_script(api=f"http://127.0.0.1:{free_port()}")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.report()["notify"]["reason"], "network_unreachable")
        self.assertEqual(len(self.alerts()), 1)

    def test_missing_credentials_is_non_fatal(self):
        self.env_file.unlink()
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.report()["notify"]["reason"], "missing_credentials")
        self.assertEqual(FakeTelegram.calls, [])

    def test_notify_helper_missing_writes_fallback_receipts(self):
        self.render(NOTIFY_PY="/nonexistent/notify.py")
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_fallback_receipts("helper_failed", "ok", 0)

    def test_notify_helper_crash_preserves_failing_pipeline_exit(self):
        (self.state / "exec-rc").write_text("3\n")
        self.fake_helper("raise SystemExit(5)\n")
        result = self.run_script()
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assert_fallback_receipts("helper_failed", "pipeline_failed", 3)

    def test_notify_helper_exit_zero_without_outcome_is_detected(self):
        self.fake_helper("raise SystemExit(0)\n")
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_fallback_receipts("helper_no_outcome", "ok", 0)

    def test_notify_helper_hang_is_bounded(self):
        self.fake_helper("import time\ntime.sleep(60)\n", NOTIFY_TIMEOUT="1")
        started = time.monotonic()
        result = self.run_script()
        self.assertLess(time.monotonic() - started, 15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_fallback_receipts("helper_timeout", "ok", 0)

    # --- hung docker daemon: per-command and overall deadlines -------------
    def assert_clean_docker_unavailable(self, result, started):
        self.assertLess(time.monotonic() - started, 20)
        self.assertEqual(result.returncode, 75, result.stderr)
        self.assertEqual(self.report()["status"], "docker_unavailable")
        self.assertFalse(any(c.startswith("exec ") for c in self.calls()))
        self.assertEqual(len(FakeTelegram.calls), 1)
        self.assert_hang_child_gone()

    def test_hung_docker_info_reaches_docker_unavailable(self):
        (self.state / "hang-info").touch()
        started = time.monotonic()
        result = self.run_script()
        self.assert_clean_docker_unavailable(result, started)
        self.assertEqual(sum(c == "info" for c in self.calls()), 3)

    def test_hung_running_inspect_reaches_docker_unavailable(self):
        (self.state / "hang-inspect-running").touch()
        started = time.monotonic()
        result = self.run_script()
        self.assert_clean_docker_unavailable(result, started)

    def test_hung_image_inspect_reaches_docker_unavailable(self):
        (self.state / "hang-inspect-image").touch()
        started = time.monotonic()
        result = self.run_script()
        self.assert_clean_docker_unavailable(result, started)
        self.assertIn("timed out", result.stderr)

    def test_overall_deadline_stops_retries_early(self):
        (self.state / "up-after").write_text("999\n")
        self.render(DOCKER_WAIT_ATTEMPTS="50", DOCKER_WAIT_DELAY="1",
                    DOCKER_WAIT_MAX_DELAY="1", DOCKER_WAIT_DEADLINE="3")
        started = time.monotonic()
        result = self.run_script()
        self.assertLess(time.monotonic() - started, 15)
        self.assertEqual(result.returncode, 75)
        self.assertLess(self.report()["docker_wait"]["attempts"], 50)
        self.assertIn("readiness deadline 3s reached", result.stderr)

    # --- failure wording only on pipeline failure --------------------------
    def test_pipeline_failure_passes_exit_code_and_sends_failure(self):
        (self.state / "exec-rc").write_text("3\n")
        result = self.run_script()
        self.assertEqual(result.returncode, 3)
        report = self.report()
        self.assertEqual((report["status"], report["exit_code"]), ("pipeline_failed", 3))
        self.assertIn("failed", FakeTelegram.calls[0][1])

    def test_image_mismatch_refuses_with_78(self):
        (self.state / "image").write_text("sha256:" + "f" * 64 + "\n")
        result = self.run_script()
        self.assertEqual(result.returncode, 78)
        self.assertEqual(self.report()["status"], "config_error")
        self.assertFalse(any(c.startswith("exec ") for c in self.calls()))

    def test_notify_refuses_non_loopback_override(self):
        result = self.run_script(api="http://example.invalid")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.report()["notify"]["reason"], "api_base_not_allowed")
        self.assertEqual(FakeTelegram.calls, [])


class TemplateHygieneTests(unittest.TestCase):
    def test_templates_hold_no_host_specific_values(self):
        for path in sorted(p for p in RUNTIME.iterdir() if p.is_file()):
            text = path.read_text()
            self.assertNotRegex(text, r"sha256:[0-9a-f]{64}", path.name)
            self.assertNotRegex(text, r"/home/user\b|/home/coder/records", path.name)
            self.assertNotRegex(text, re.compile(r"bot\d{6,}:"), path.name)

    def test_wrapper_is_posix_sh_syntax(self):
        result = subprocess.run(["/bin/sh", "-n", str(RUNTIME / "run-records.sh.in")],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
