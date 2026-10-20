"""Records runtime wrapper (plan T10 robustness + T11 orchestration).

Every test renders the real templates with render.py into a temporary
directory. The tests use a fake docker shim, the synthetic stub stage
backend and a loopback fake Telegram server only. No network, no records.
"""
import fcntl
import http.server
import importlib.util
import json
import os
import re
import shutil
import signal
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
sys.path.insert(0, str(ROOT))
from campaign_tool.records import run_report  # noqa: E402

FAKE_TOKEN = "123456:SYNTHETIC-TEST-TOKEN"
FAKE_IMAGE = "sha256:" + "0" * 64
STUB_TEMPLATE = RUNTIME / "targets" / "stub.json.in"
SYNTHETIC_FIXTURE = ROOT / "examples" / "run-report" / "stub-stages.synthetic.json"
STAGES = run_report.STAGE_ORDER

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
esac
exit 2
"""


class FakeTelegram(http.server.BaseHTTPRequestHandler):
    calls = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        FakeTelegram.calls.append((self.path, self.rfile.read(length).decode()))
        good = self.path == f"/bot{FAKE_TOKEN}/sendMessage"
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


class RuntimeHarness(unittest.TestCase):
    """Render the wrapper for a synthetic TEST target and run it."""

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
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = self.tmp / "state"
        self.state.mkdir()
        (self.state / "image").write_text(FAKE_IMAGE + "\n")
        (self.state / "up-after").write_text("1\n")
        docker = self.tmp / "docker"
        docker.write_text(FAKE_DOCKER.replace("@STATE@", str(self.state)))
        docker.chmod(stat.S_IRWXU)
        self.runtime = self.tmp / "runtime"
        self.runtime.mkdir()
        self.fixture_path = self.tmp / "stub-fixture.json"
        self.fixture = json.loads(SYNTHETIC_FIXTURE.read_text())
        self.write_fixture()
        self.env_file = self.tmp / "telegram.env"
        self.env_file.write_text(f"TELEGRAM_BOT_TOKEN={FAKE_TOKEN}\nTELEGRAM_HOME_CHANNEL=-100\n")
        self.values = {
            "RELEASE_COMMIT": "a" * 40, "ENGINE_DIR": str(ROOT), "DOCKER": str(docker),
            "TIMEOUT": shutil.which("timeout"), "FLOCK": shutil.which("flock"),
            "CONTAINER": "synthetic-records", "EXPECTED_IMAGE": FAKE_IMAGE,
            "RUN_UID": str(os.getuid()), "RUNTIME_DIR": str(self.runtime),
            "HOST_PYTHON": sys.executable, "NOTIFY_PY": str(RUNTIME / "notify.py"),
            "NOTIFY_ENV_FILE": str(self.env_file), "NOTIFY_LABEL": "Synthetic",
            "DOCKER_WAIT_ATTEMPTS": "3", "DOCKER_WAIT_DELAY": "0",
            "DOCKER_WAIT_MAX_DELAY": "0", "DOCKER_CMD_TIMEOUT": "1",
            "DOCKER_WAIT_DEADLINE": "30", "NOTIFY_TIMEOUT": "30",
            "RUN_BUDGET_SECONDS": "600", "DEFAULT_TARGET": "TEST",
            "SCHEDULED_TARGET": "TEST", "RUN_RECORDS_SH": str(self.runtime / "run-records.sh"),
            "STUB_FIXTURE": str(self.fixture_path),
            # 64 MiB floor: any CI or dev host has that much free scratch space.
            "MIN_FREE_BYTES": str(64 * 1024 ** 2),
        }
        self.render()

    def write_fixture(self):
        self.fixture_path.write_text(json.dumps(self.fixture))

    def stage_spec(self, name):
        return self.fixture["stages"].setdefault(name, {})

    def render(self, **overrides):
        values = {**self.values, **overrides}
        values_path = self.tmp / "values.json"
        values_path.write_text(json.dumps(values))
        out = self.tmp / "rendered"
        shutil.rmtree(out, ignore_errors=True)
        result = subprocess.run(
            [sys.executable, "-B", str(RUNTIME / "render.py"), "--values", str(values_path),
             "--out", str(out), "--target", f"TEST={STUB_TEMPLATE}"],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.script = out / "run-records.sh"
        (self.runtime / "targets").mkdir(exist_ok=True)
        shutil.copy(out / "targets" / "TEST.json", self.runtime / "targets" / "TEST.json")
        return out

    def fake_helper(self, body, **overrides):
        helper = self.tmp / "fake_notify.py"
        helper.write_text(body)
        self.render(NOTIFY_PY=str(helper), **overrides)

    def run_script(self, *args, api=None, timeout=90):
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
               "RECORDS_NOTIFY_API_BASE": api or self.api}
        return subprocess.run(["/bin/sh", str(self.script), *args], env=env,
                              capture_output=True, text=True, timeout=timeout)

    @property
    def target_dir(self):
        return self.runtime / "TEST"

    def report(self):
        report = json.loads((self.target_dir / "run-report.json").read_text())
        run_report.validate_run_report(report)
        [archived] = sorted((self.target_dir / "reports").glob("run_*.json"))
        self.assertEqual(json.loads(archived.read_text()), report)
        return report

    def stage(self, report, name):
        return next(stage for stage in report["stages"] if stage["name"] == name)

    def alerts(self, kind="notify-failed"):
        return sorted((self.target_dir / "alerts").glob(f"{kind}-*.json"))

    def calls(self):
        path = self.state / "calls"
        return path.read_text().splitlines() if path.exists() else []

    def log(self):
        return (self.target_dir / "run-records.log").read_text()

    def assert_hang_child_gone(self):
        pid = int((self.state / "hang-pid").read_text())
        for _ in range(30):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.1)
        self.fail("hung docker child survived the command deadline")

    def assert_fallback_receipts(self, reason, exit_code):
        report = self.report()
        self.assertTrue(report["notify_failed"])
        notify = self.stage(report, "notify")
        self.assertEqual((notify["status"], notify["error_code"]),
                         ("fail", "NOTIFY_" + reason.upper()))
        self.assertEqual(report["overall_status"], "fail")
        [alert] = self.alerts()
        data = json.loads(alert.read_text())
        self.assertEqual((data["kind"], data["reason"], data["run_exit_code"], data["run_id"]),
                         ("notify_failed", reason, exit_code, report["run_id"]))
        self.assertIn(f"notify: FAILED reason={reason}", self.log())


class OrchestratedRunTests(RuntimeHarness):
    # --- acceptance: dry-run lists every stage -----------------------------
    def test_dry_run_lists_all_stages_and_touches_nothing(self):
        result = self.run_script("--target", "TEST", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        for index, name in enumerate(STAGES, start=1):
            self.assertRegex(result.stdout, rf"stage {index}/8 {name}:")
        for index, name in enumerate(STAGES[:-1]):
            self.assertIn(f"stage {index + 1}/8 {name}: exit-on-fail={10 + index}", result.stdout)
        self.assertIn("preflight 1 lock", result.stdout)
        self.assertIn("preflight 2 disk", result.stdout)
        self.assertIn("preflight 3 docker", result.stdout)
        self.assertEqual(self.calls(), [])
        self.assertFalse(self.target_dir.exists())
        self.assertFalse((self.runtime / "run-records.lock").exists())
        self.assertEqual(FakeTelegram.calls, [])

    # --- acceptance: real TEST run writes a complete report ----------------
    def test_test_run_writes_complete_valid_report(self):
        result = self.run_script("--target", "TEST")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.report()
        self.assertEqual([stage["name"] for stage in report["stages"]], list(STAGES))
        self.assertEqual(report["mode"], "test")
        self.assertEqual(report["overall_status"], "warn")
        self.assertFalse(report["notify_failed"])
        statuses = {stage["name"]: stage["status"] for stage in report["stages"]}
        self.assertEqual(statuses, {"sync": "ok", "delta": "ok", "adoption": "ok",
                                    "portal": "warn", "extract": "ok", "digest_gates": "warn",
                                    "outputs": "ok", "notify": "ok"})
        for stage in report["stages"]:
            self.assertIsNotNone(stage["duration_seconds"], stage["name"])
        self.assertEqual(report["new_data"], {"documents": 4, "pages": 21,
                                              "extracted_units": 140, "portal_releases": 1,
                                              "request_status_changes": 0})
        self.assertEqual(report["review_required_count"], 2)
        needs = {(item["code"], item["stage"]): item for item in report["needs_g"]}
        # Phase 1: documents needing OCR are counted, never dropped.
        self.assertEqual(needs[("AWAITING_OCR", "extract")]["count"], 2)
        self.assertEqual(self.stage(report, "extract")["counts"]["skipped"], 2)
        # Every stub stage marks itself, so a stub run cannot pass as coverage.
        for name in STAGES[:-1]:
            self.assertEqual(needs[("STUB_STAGE_BACKEND", name)]["severity"], "info")
        self.assertEqual(len(FakeTelegram.calls), 1)
        message = FakeTelegram.calls[0][1]
        self.assertIn("completed+with+warnings", message)
        self.assertNotIn("failed", message)
        self.assertEqual(self.alerts(), [])
        self.assertIn("docker ready after 1/3 attempts", self.log())

    def test_scheduled_flag_on_test_target_still_reports_test_mode(self):
        result = self.run_script("--scheduled")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.report()["mode"], "test")

    # --- per-stage exit codes ----------------------------------------------
    def test_failed_stage_sets_its_exit_code_and_skips_downstream(self):
        self.stage_spec("adoption").update(exit_code=4, emit="none")
        self.write_fixture()
        result = self.run_script()
        self.assertEqual(result.returncode, 12, result.stderr)
        report = self.report()
        adoption = self.stage(report, "adoption")
        self.assertEqual((adoption["status"], adoption["error_code"]), ("fail", "STAGE_EXIT_4"))
        for name in ("portal", "extract", "digest_gates", "outputs"):
            stage = self.stage(report, name)
            self.assertEqual((stage["status"], stage["error_code"], stage["duration_seconds"]),
                             ("skipped", "UPSTREAM_FAILED", None))
        self.assertEqual(report["overall_status"], "fail")
        self.assertEqual(self.stage(report, "notify")["status"], "ok")
        self.assertIn("failed+at+stage+adoption", FakeTelegram.calls[0][1])
        self.assertIn("exit+12", FakeTelegram.calls[0][1])

    def test_continue_stage_failure_runs_downstream_and_keeps_exit_code(self):
        self.stage_spec("portal").update(
            exit_code=1, result={"status": "fail", "error_code": "PORTAL_HTTP_404",
                                 "counts": {"input": 1, "output": 0, "failed": 1, "skipped": 0}})
        self.write_fixture()
        result = self.run_script()
        self.assertEqual(result.returncode, 13, result.stderr)
        report = self.report()
        self.assertEqual(self.stage(report, "portal")["error_code"], "PORTAL_HTTP_404")
        self.assertEqual(self.stage(report, "extract")["status"], "ok")
        self.assertEqual(self.stage(report, "outputs")["status"], "ok")

    def test_each_work_stage_maps_to_its_exit_code(self):
        for index, name in enumerate(STAGES[:-1]):
            with self.subTest(stage=name):
                shutil.rmtree(self.target_dir, ignore_errors=True)
                self.fixture = json.loads(SYNTHETIC_FIXTURE.read_text())
                self.stage_spec(name).update(emit="invalid")
                self.write_fixture()
                result = self.run_script()
                self.assertEqual(result.returncode, 10 + index, result.stderr)
                stage = self.stage(self.report(), name)
                self.assertEqual(stage["error_code"], "STAGE_RESULT_INVALID")

    def test_stage_timeout_is_bounded_and_reported(self):
        self.stage_spec("extract").update(sleep_seconds=30)
        self.write_fixture()
        config_path = self.runtime / "targets" / "TEST.json"
        config = json.loads(config_path.read_text())
        config["stages"][4]["timeout_seconds"] = 1
        config_path.write_text(json.dumps(config))
        started = time.monotonic()
        result = self.run_script()
        self.assertLess(time.monotonic() - started, 20)
        self.assertEqual(result.returncode, 14, result.stderr)
        self.assertEqual(self.stage(self.report(), "extract")["error_code"], "STAGE_TIMEOUT")

    def test_run_budget_deadline_stops_work(self):
        self.render(RUN_BUDGET_SECONDS="40", DOCKER_WAIT_DEADLINE="2")
        self.stage_spec("delta").update(sleep_seconds=60)
        self.write_fixture()
        started = time.monotonic()
        result = self.run_script()
        self.assertLess(time.monotonic() - started, 60)
        self.assertEqual(result.returncode, 11, result.stderr)
        report = self.report()
        self.assertEqual(self.stage(report, "delta")["error_code"], "RUN_DEADLINE_REACHED")
        self.assertEqual(self.stage(report, "adoption")["status"], "skipped")

    # --- overlap lock --------------------------------------------------------
    def test_lock_held_skips_slot_without_any_work(self):
        with open(self.runtime / "run-records.lock", "a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            result = self.run_script()
        self.assertEqual(result.returncode, 73, result.stderr)
        self.assertEqual(self.calls(), [])
        self.assertFalse((self.target_dir / "run-report.json").exists())
        self.assertEqual(list((self.target_dir / "reports").iterdir()), [])
        [skip] = self.alerts("run-skipped")
        data = json.loads(skip.read_text())
        self.assertEqual((data["kind"], data["target"]), ("lock_held", "TEST"))
        self.assertRegex(data["run_id"], r"^run_[0-9]{8}T[0-9]{6}Z_[0-9a-f]{8}$")
        self.assertIn("skipped: another records run holds the lock", self.log())
        self.assertEqual(FakeTelegram.calls, [])

    def test_overlapping_runs_one_skips(self):
        self.stage_spec("sync").update(sleep_seconds=3)
        self.write_fixture()
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "RECORDS_NOTIFY_API_BASE": self.api}
        first = subprocess.Popen(["/bin/sh", str(self.script)], env=env,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(50):
                if (self.state / "calls").exists() and "info" in self.calls():
                    break
                time.sleep(0.1)
            second = self.run_script()
        finally:
            first_rc = first.wait(timeout=60)
        self.assertEqual(second.returncode, 73)
        self.assertEqual(first_rc, 0)
        self.assertEqual(len(self.alerts("run-skipped")), 1)
        self.assertEqual(len(list((self.target_dir / "reports").glob("run_*.json"))), 1)

    # --- usage and configuration ---------------------------------------------
    def test_usage_errors_exit_64(self):
        for args in (["--bogus"], ["--target"], ["--target", "test"], ["--target", "../X"]):
            with self.subTest(args=args):
                self.assertEqual(self.run_script(*args).returncode, 64)
        self.assertEqual(self.calls(), [])

    def test_unknown_target_refuses_78_with_receipt(self):
        result = self.run_script("--target", "NOPE")
        self.assertEqual(result.returncode, 78)
        self.assertIn("TARGET_CONFIG_MISSING", (self.runtime / "preflight-refusals.log").read_text())
        self.assertEqual(self.calls(), [])

    def test_invalid_stage_plan_writes_report_and_exits_78(self):
        (self.runtime / "targets" / "TEST.json").write_text('{"target_config_version": 1}')
        result = self.run_script()
        self.assertEqual(result.returncode, 78)
        report = self.report()
        self.assertEqual(self.stage(report, "sync")["error_code"], "TARGET_CONFIG_INVALID")
        self.assertEqual(self.calls(), [])
        self.assertIn("target+stage+plan+invalid", FakeTelegram.calls[0][1])


class RobustnessTests(RuntimeHarness):
    """Plan T10 regressions (Oct 16/17), on top of the T11 report."""

    def test_docker_down_retries_then_fails_cleanly(self):
        (self.state / "up-after").write_text("999\n")
        result = self.run_script()
        self.assertEqual(result.returncode, 75)
        self.assertIn("not ready after 3/3 attempts", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(sum(c == "info" for c in self.calls()), 3)
        report = self.report()
        sync = self.stage(report, "sync")
        self.assertEqual((sync["status"], sync["error_code"]), ("fail", "DOCKER_UNAVAILABLE"))
        self.assertTrue(all(self.stage(report, name)["status"] == "skipped"
                            for name in STAGES[1:-1]))
        self.assertEqual(report["needs_g"], [{"code": "DOCKER_UNAVAILABLE", "stage": "sync",
                                              "severity": "blocking", "count": 1}])
        self.assertIn("Docker", FakeTelegram.calls[0][1])
        self.assertEqual(self.log().count("retrying"), 2)

    def test_docker_comes_up_within_bound(self):
        (self.state / "up-after").write_text("3\n")
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("docker ready after 3/3 attempts", self.log())
        self.assertEqual(self.stage(self.report(), "outputs")["status"], "ok")

    def test_docker_down_and_telegram_down_still_exit_75(self):
        (self.state / "up-after").write_text("999\n")
        result = self.run_script(api=f"http://127.0.0.1:{free_port()}")
        self.assertEqual(result.returncode, 75)
        self.assertTrue(self.report()["notify_failed"])
        self.assertEqual(len(self.alerts()), 1)

    def test_bad_telegram_token_run_completes_and_flags_notify_failed(self):
        self.env_file.write_text("TELEGRAM_BOT_TOKEN=999:WRONG\nTELEGRAM_HOME_CHANNEL=-100\n")
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.report()
        self.assertTrue(report["notify_failed"])
        self.assertEqual(self.stage(report, "notify")["error_code"], "NOTIFY_HTTP_401")
        self.assertEqual(self.stage(report, "outputs")["status"], "ok")
        [alert] = self.alerts()
        data = json.loads(alert.read_text())
        self.assertEqual((data["kind"], data["reason"], data["run_exit_code"]),
                         ("notify_failed", "http_401", 0))
        self.assertIn("notify: FAILED reason=http_401", self.log())
        for path in [alert, self.target_dir / "run-report.json",
                     self.target_dir / "run-records.log"]:
            self.assertNotIn("WRONG", path.read_text())
        self.assertNotIn("WRONG", result.stderr)

    def test_telegram_unreachable_is_non_fatal(self):
        result = self.run_script(api=f"http://127.0.0.1:{free_port()}")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.stage(self.report(), "notify")["error_code"],
                         "NOTIFY_NETWORK_UNREACHABLE")
        self.assertEqual(len(self.alerts()), 1)

    def test_missing_credentials_is_non_fatal(self):
        self.env_file.unlink()
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.stage(self.report(), "notify")["error_code"],
                         "NOTIFY_MISSING_CREDENTIALS")
        self.assertEqual(FakeTelegram.calls, [])

    def test_notify_refuses_non_loopback_override(self):
        result = self.run_script(api="http://example.invalid")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.stage(self.report(), "notify")["error_code"],
                         "NOTIFY_API_BASE_NOT_ALLOWED")
        self.assertEqual(FakeTelegram.calls, [])

    def test_notify_helper_missing_writes_fallback_receipts(self):
        self.render(NOTIFY_PY="/nonexistent/notify.py")
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_fallback_receipts("helper_failed", 0)

    def test_notify_helper_crash_preserves_failing_stage_exit(self):
        self.stage_spec("sync").update(exit_code=3, emit="none")
        self.write_fixture()
        self.fake_helper("raise SystemExit(5)\n")
        result = self.run_script()
        self.assertEqual(result.returncode, 10, result.stderr)
        self.assert_fallback_receipts("helper_failed", 10)

    def test_notify_helper_exit_zero_without_outcome_is_detected(self):
        self.fake_helper("raise SystemExit(0)\n")
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_fallback_receipts("helper_no_outcome", 0)

    def test_notify_helper_hang_is_bounded(self):
        self.fake_helper("import time\ntime.sleep(60)\n", NOTIFY_TIMEOUT="1")
        started = time.monotonic()
        result = self.run_script()
        self.assertLess(time.monotonic() - started, 20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_fallback_receipts("helper_timeout", 0)

    def assert_clean_docker_unavailable(self, result, started):
        self.assertLess(time.monotonic() - started, 20)
        self.assertEqual(result.returncode, 75, result.stderr)
        self.assertEqual(self.stage(self.report(), "sync")["error_code"], "DOCKER_UNAVAILABLE")
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
        self.assert_clean_docker_unavailable(self.run_script(), started)

    def test_hung_image_inspect_reaches_docker_unavailable(self):
        (self.state / "hang-inspect-image").touch()
        started = time.monotonic()
        result = self.run_script()
        self.assert_clean_docker_unavailable(result, started)
        self.assertIn("timed out", result.stderr)

    def test_overall_docker_deadline_stops_retries_early(self):
        (self.state / "up-after").write_text("999\n")
        self.render(DOCKER_WAIT_ATTEMPTS="50", DOCKER_WAIT_DELAY="1",
                    DOCKER_WAIT_MAX_DELAY="1", DOCKER_WAIT_DEADLINE="3")
        started = time.monotonic()
        result = self.run_script()
        self.assertLess(time.monotonic() - started, 20)
        self.assertEqual(result.returncode, 75)
        self.assertLess(sum(c == "info" for c in self.calls()), 50)
        self.assertIn("readiness deadline 3s reached", result.stderr)

    def test_image_mismatch_refuses_with_78(self):
        (self.state / "image").write_text("sha256:" + "f" * 64 + "\n")
        result = self.run_script()
        self.assertEqual(result.returncode, 78)
        self.assertEqual(self.stage(self.report(), "sync")["error_code"], "IMAGE_MISMATCH")
        self.assertIn("refused+to+start", FakeTelegram.calls[0][1])


class OrchestratorSignalTests(unittest.TestCase):
    def test_sigterm_during_stage_kills_it_and_still_writes_report(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        fixture = tmp / "fixture.json"
        fixture.write_text(json.dumps({"stub_fixture_version": 1,
                                       "stages": {"delta": {"sleep_seconds": 60}}}))
        template = STUB_TEMPLATE.read_text()
        config = template.replace("@TARGET_NAME@", "TEST").replace(
            "@HOST_PYTHON@", sys.executable).replace("@STUB_FIXTURE@", str(fixture))
        (tmp / "TEST.json").write_text(config)
        report_path = tmp / "report.json"
        now = time.time()
        started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
        run_id = "run_" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now)) + "_0badc0de"
        proc = subprocess.Popen(
            [sys.executable, "-B", "-m", "campaign_tool.records.run_orchestrator", "run",
             "--target-config", str(tmp / "TEST.json"), "--run-id", run_id,
             "--started-at", started_at, "--deadline-epoch", str(int(now) + 300),
             "--report", str(report_path), "--work-dir", str(tmp)],
            env={**os.environ, "PYTHONPATH": str(ROOT)})
        time.sleep(2.5)
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(timeout=20), 11)
        report = json.loads(report_path.read_text())
        run_report.validate_run_report(report)
        delta = next(stage for stage in report["stages"] if stage["name"] == "delta")
        self.assertEqual(delta["error_code"], "RUN_INTERRUPTED")
        self.assertEqual(report["stages"][-1]["error_code"], "NOTIFY_PENDING")


# Synthetic stage: forks a same-group child that ignores SIGTERM and keeps
# appending to a marker file. The leader keeps default SIGTERM handling and
# either hangs ("hang") or exits 0 with a valid result ("exit").
TERM_IGNORING_STAGE = r"""
import json, os, signal, sys, time
mode, state = sys.argv[1], sys.argv[2]
pid = os.fork()
if pid == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    with open(os.path.join(state, "child.pid.tmp"), "w") as fh:
        fh.write(str(os.getpid()))
    os.replace(os.path.join(state, "child.pid.tmp"), os.path.join(state, "child.pid"))
    while True:
        with open(os.path.join(state, "marker"), "a") as fh:
            fh.write("x\n")
        time.sleep(0.05)
while not os.path.exists(os.path.join(state, "child.pid")):
    time.sleep(0.01)
if mode == "hang":
    time.sleep(60)
print(json.dumps({"stage_result_version": 1, "status": "ok",
                  "counts": {"input": 0, "output": 0, "failed": 0, "skipped": 0}}))
sys.stdout.flush()
"""


class StageProcessGroupTests(unittest.TestCase):
    """No stage descendant may outlive its stage (Sol review of d10c1b6)."""

    def setUp(self):
        from campaign_tool.records import run_orchestrator  # noqa: PLC0415
        self.orch = run_orchestrator
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = self.tmp / "state"
        self.state.mkdir()
        self.script = self.tmp / "term_ignoring_stage.py"
        self.script.write_text(TERM_IGNORING_STAGE)
        self.addCleanup(self.kill_child_if_alive)

    def child_pid(self):
        path = self.state / "child.pid"
        return int(path.read_text()) if path.exists() else None

    @staticmethod
    def pid_running(pid):
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
        except (FileNotFoundError, ProcessLookupError):
            return False
        return stat.rsplit(")", 1)[1].split()[0] != "Z"

    def kill_child_if_alive(self):
        pid = self.child_pid()
        if pid and self.pid_running(pid):
            os.kill(pid, signal.SIGKILL)

    def assert_no_survivor_and_no_late_write(self):
        pid = self.child_pid()
        self.assertIsNotNone(pid, "synthetic child never started")
        marker = self.state / "marker"
        size = marker.stat().st_size
        time.sleep(0.6)
        self.assertFalse(self.pid_running(pid), "TERM-ignoring descendant survived")
        self.assertEqual(marker.stat().st_size, size, "late write after stage returned")

    def stage_argv(self, mode):
        return [sys.executable, "-B", str(self.script), mode, str(self.state)]

    def test_timeout_kills_term_ignoring_descendant(self):
        schema = self.orch.load_stage_result_schema()
        status, counts, code, result, _ = self.orch.run_stage(
            self.stage_argv("hang"), 0.5, schema, self.tmp, dict(os.environ))
        self.assertEqual((status, code, result), ("fail", "STAGE_TIMEOUT", None))
        self.assertEqual(counts, self.orch.zero_counts())
        self.assert_no_survivor_and_no_late_write()

    def test_descendant_left_after_normal_exit_is_killed_and_fails_stage(self):
        schema = self.orch.load_stage_result_schema()
        status, _, code, result, _ = self.orch.run_stage(
            self.stage_argv("exit"), 30, schema, self.tmp, dict(os.environ))
        self.assertEqual((status, code, result), ("fail", "STAGE_LEFT_PROCESSES", None))
        self.assert_no_survivor_and_no_late_write()

    def test_sigterm_kills_term_ignoring_descendant_and_still_writes_report(self):
        config = json.loads(STUB_TEMPLATE.read_text().replace("@TARGET_NAME@", "TEST")
                            .replace("@HOST_PYTHON@", sys.executable)
                            .replace("@STUB_FIXTURE@", str(SYNTHETIC_FIXTURE)))
        config["stages"][1]["argv"] = self.stage_argv("hang")
        (self.tmp / "TEST.json").write_text(json.dumps(config))
        report_path = self.tmp / "report.json"
        now = time.time()
        started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
        run_id = "run_" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now)) + "_0badc0de"
        proc = subprocess.Popen(
            [sys.executable, "-B", "-m", "campaign_tool.records.run_orchestrator", "run",
             "--target-config", str(self.tmp / "TEST.json"), "--run-id", run_id,
             "--started-at", started_at, "--deadline-epoch", str(int(now) + 300),
             "--report", str(report_path), "--work-dir", str(self.tmp)],
            env={**os.environ, "PYTHONPATH": str(ROOT)})
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        deadline = time.monotonic() + 20
        while self.child_pid() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIsNotNone(self.child_pid(), "delta stage never started")
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(timeout=20), 11)
        self.assert_no_survivor_and_no_late_write()
        report = json.loads(report_path.read_text())
        run_report.validate_run_report(report)
        delta = next(stage for stage in report["stages"] if stage["name"] == "delta")
        self.assertEqual(delta["error_code"], "RUN_INTERRUPTED")
        self.assertEqual(report["stages"][-1]["error_code"], "NOTIFY_PENDING")


class RendererTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        spec = importlib.util.spec_from_file_location("records_runtime_render",
                                                      RUNTIME / "render.py")
        self.render = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.render)
        self.values = {
            "RELEASE_COMMIT": "b" * 40, "ENGINE_DIR": "/srv/engine", "DOCKER": "/usr/bin/docker",
            "TIMEOUT": "/usr/bin/timeout", "FLOCK": "/usr/bin/flock", "CONTAINER": "records",
            "EXPECTED_IMAGE": FAKE_IMAGE, "RUN_UID": "1000", "RUNTIME_DIR": "/srv/runtime",
            "HOST_PYTHON": "/usr/bin/python3", "NOTIFY_PY": "/srv/engine/notify.py",
            "NOTIFY_ENV_FILE": "/srv/notify.env", "NOTIFY_LABEL": "Records",
            "DOCKER_WAIT_ATTEMPTS": "12", "DOCKER_WAIT_DELAY": "5",
            "DOCKER_WAIT_MAX_DELAY": "60", "DOCKER_CMD_TIMEOUT": "20",
            "DOCKER_WAIT_DEADLINE": "600", "NOTIFY_TIMEOUT": "60",
            "RUN_BUDGET_SECONDS": "2640", "DEFAULT_TARGET": "TEST",
            "SCHEDULED_TARGET": "TEST", "RUN_RECORDS_SH": "/srv/runtime/run-records.sh",
            "STUB_FIXTURE": "/srv/fixture.json", "MIN_FREE_BYTES": "2147483648",
        }
        self.targets = [("TEST", str(STUB_TEMPLATE))]

    def do_render(self, values=None, targets=None, out=None):
        return self.render.render(values or self.values, targets or self.targets,
                                  out or self.tmp / "out")

    def test_suggested_values_render_three_pacific_slots(self):
        written = self.do_render()
        self.assertEqual(sorted(written), ["records-run.service", "records-run.timer",
                                           "run-records.sh", "targets/TEST.json"])
        out = self.tmp / "out"
        timer = (out / "records-run.timer").read_text()
        for slot in ("07:00:00", "13:00:00", "20:30:00"):
            self.assertIn(f"OnCalendar=*-*-* {slot} America/Los_Angeles", timer)
        service = (out / "records-run.service").read_text()
        self.assertIn("ExecStart=/bin/sh /srv/runtime/run-records.sh --target TEST --scheduled",
                      service)
        self.assertEqual(stat.S_IMODE((out / "run-records.sh").stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((out / "targets" / "TEST.json").stat().st_mode), 0o600)
        for path in out.rglob("*"):
            if path.is_file():
                self.assertIsNone(re.search(r"@[A-Z_]+@", path.read_text()), path.name)

    def test_render_test_and_live_container_targets(self):
        container_values = {
            **{k: v for k, v in self.values.items() if k != "STUB_FIXTURE"},
            "CONTAINER_USER": "0",
            "CONTAINER_ENGINE_DIR": "/opt/flock-engine",
            "CONTAINER_PYTHON": "/opt/campaign-tool/venv/bin/python",
            "CONTAINER_PROJECT_DIR": "/srv/flock-live",
            "CONTAINER_APPROVAL_FILE": "/srv/flock-live/approval.json",
            "ADOPTION_CONFIG": "/srv/flock-live/adoption.json",
            "SCHEDULED_TARGET": "LIVE",
        }
        targets = [("TEST", str(RUNTIME / "targets" / "test-container.json.in")),
                   ("LIVE", str(RUNTIME / "targets" / "live-container.json.in"))]
        written = self.do_render(values=container_values, targets=targets)
        self.assertIn("targets/TEST.json", written)
        self.assertIn("targets/LIVE.json", written)
        out = self.tmp / "out"
        test = json.loads((out / "targets" / "TEST.json").read_text())
        live = json.loads((out / "targets" / "LIVE.json").read_text())
        self.assertEqual((test["target"], test["mode"]), ("TEST", "test"))
        self.assertEqual((live["target"], live["mode"]), ("LIVE", "live"))
        self.assertEqual(test["stages"], live["stages"])
        for name in ("TEST", "LIVE"):
            self.assertIsNone(re.search(r"@[A-Z_]+@",
                                        (out / "targets" / f"{name}.json").read_text()), name)

    def assert_refused(self, fragment, **kwargs):
        with self.assertRaises(ValueError) as caught:
            self.do_render(**kwargs)
        self.assertIn(fragment, str(caught.exception))

    def test_refuses_missing_unknown_and_unsafe_values(self):
        values = dict(self.values)
        del values["FLOCK"]
        self.assert_refused("no value for ['FLOCK']", values=values)
        self.assert_refused("unknown placeholder", values={**self.values, "EXTRA": "x"})
        self.assert_refused("shell-safe", values={**self.values, "NOTIFY_LABEL": "a b"})
        self.assert_refused("shell-safe", values={**self.values, "CONTAINER": "x;rm"})
        self.assert_refused("absolute path", values={**self.values, "DOCKER": "docker"})
        self.assert_refused("reserved", values={**self.values, "TARGET_NAME": "LIVE"})

    def test_refuses_budget_beyond_unit_timeout(self):
        self.assert_refused("exceeds", values={**self.values, "RUN_BUDGET_SECONDS": "2760"})
        self.assert_refused("readiness", values={**self.values, "RUN_BUDGET_SECONDS": "650"})

    def test_refuses_output_inside_git_checkout_or_non_empty(self):
        self.assert_refused("Git checkout", out=ROOT / "rendered-should-not-exist")
        self.assertFalse((ROOT / "rendered-should-not-exist").exists())
        (self.tmp / "busy").mkdir()
        (self.tmp / "busy" / "x").write_text("x")
        self.assert_refused("not empty", out=self.tmp / "busy")

    def test_refuses_unrendered_scheduled_target_and_stub_in_live(self):
        self.assert_refused("no rendered stage plan",
                            values={**self.values, "SCHEDULED_TARGET": "LIVE"})
        live = self.tmp / "live.json.in"
        live.write_text(STUB_TEMPLATE.read_text().replace('"mode": "test"', '"mode": "live"'))
        self.assert_refused("refused in a live target",
                            targets=[("TEST", str(STUB_TEMPLATE)), ("LIVE", str(live))],
                            values={**self.values, "SCHEDULED_TARGET": "LIVE"})

    def test_timer_check_rejects_other_slots(self):
        with self.assertRaises(ValueError):
            self.render.check_timer("OnCalendar=*-*-* 00:00:00 America/Los_Angeles\n")


class StageContractTests(unittest.TestCase):
    def setUp(self):
        from campaign_tool.records import run_orchestrator  # noqa: PLC0415
        self.orch = run_orchestrator
        self.config = json.loads(STUB_TEMPLATE.read_text().replace("@TARGET_NAME@", "TEST")
                                 .replace("@HOST_PYTHON@", "/usr/bin/python3")
                                 .replace("@STUB_FIXTURE@", "/x/fixture.json"))

    def assert_invalid(self, mutate, fragment):
        config = json.loads(json.dumps(self.config))
        mutate(config)
        with self.assertRaises(self.orch.TargetConfigError) as caught:
            self.orch.validate_target_config(config)
        self.assertIn(fragment, str(caught.exception))

    def test_stub_plan_is_valid(self):
        self.orch.validate_target_config(self.config)

    def test_target_config_rejections(self):
        self.assert_invalid(lambda c: c["stages"].reverse(), "in order")
        self.assert_invalid(lambda c: c["stages"].pop(), "in order")
        self.assert_invalid(lambda c: c["stages"][0].update(argv=["python3"]), "absolute")
        self.assert_invalid(lambda c: c["stages"][0].update(timeout_seconds=0), "timeout")
        self.assert_invalid(lambda c: c["stages"][0].update(timeout_seconds=True), "timeout")
        self.assert_invalid(lambda c: c["stages"][0].update(on_fail="retry"), "on_fail")
        self.assert_invalid(lambda c: c["stages"][0].update(shell=True), "unexpected")
        self.assert_invalid(lambda c: c.update(mode="live"), "refused in a live target")
        self.assert_invalid(lambda c: c.update(target="test"), "upper-case")
        self.assert_invalid(lambda c: c.update(extra=1), "exactly fields")

    def test_report_mode_maps_test_and_live_plans(self):
        def load(template, target):
            text = template.read_text(encoding="utf-8")
            return json.loads(text.replace("@TARGET_NAME@", target)
                                  .replace("@DOCKER@", "/usr/bin/docker"))
        live = load(RUNTIME / "targets" / "live-container.json.in", "LIVE")
        test = load(RUNTIME / "targets" / "test-container.json.in", "TEST")
        self.orch.validate_target_config(live)
        self.orch.validate_target_config(test)
        self.assertEqual((test["mode"], live["mode"]), ("test", "live"))
        # A TEST plan always reports test, scheduled or not.
        self.assertEqual(self.orch.report_mode(test, False), "test")
        self.assertEqual(self.orch.report_mode(test, True), "test")
        # A LIVE plan reports manual for a manual run and scheduled for --scheduled.
        self.assertEqual(self.orch.report_mode(live, False), "manual")
        self.assertEqual(self.orch.report_mode(live, True), "scheduled")

    def test_disabled_stage_needs_no_argv(self):
        config = json.loads(json.dumps(self.config))
        config["stages"][3] = {"name": "portal", "enabled": False}
        self.orch.validate_target_config(config)

    def test_stage_result_parsing(self):
        schema = self.orch.load_stage_result_schema()
        ok = {"stage_result_version": 1, "status": "ok",
              "counts": {"input": 1, "output": 1, "failed": 0, "skipped": 0}}
        parse = self.orch.parse_stage_result
        self.assertEqual(parse(b"noise\n" + json.dumps(ok).encode() + b"\n\n", schema)["status"],
                         "ok")
        for bad in ({**ok, "status": "warn"}, {**ok, "error_code": "X_Y_Z"},
                    {**ok, "path": "/x"}, {**ok, "counts": {"input": -1}},
                    {**ok, "needs_g": [{"code": "ABC", "severity": "info", "count": 1}] * 2}):
            self.assertIsNone(parse(json.dumps(bad).encode(), schema), bad)
        self.assertIsNone(parse(b"", schema))
        self.assertIsNone(parse(b"[1]", schema))

    def test_notify_reason_codes_are_schema_codes(self):
        for reason in ("http_401", "network_unreachable", "URLError", "helper_timeout", "?"):
            self.assertRegex(self.orch.notify_reason_code(reason), r"^[A-Z][A-Z0-9_]{2,63}$")


class TemplateHygieneTests(unittest.TestCase):
    def test_templates_hold_no_host_specific_values(self):
        # Split literals so this test itself stays clean for the public scan.
        host_paths = re.compile("/home/" + r"user\b|/home/" + "coder")
        tracked = [p for p in RUNTIME.rglob("*") if p.is_file()
                   and "__pycache__" not in p.parts and p.suffix in (".in", ".py", ".md")]
        self.assertGreaterEqual(len(tracked), 7)
        for path in sorted(tracked):
            text = path.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"sha256:[0-9a-f]{64}", path.name)
            self.assertNotRegex(text, host_paths, path.name)
            self.assertNotRegex(text, re.compile(r"bot\d{6,}:"), path.name)

    def test_wrapper_is_posix_sh_syntax(self):
        result = subprocess.run(["/bin/sh", "-n", str(RUNTIME / "run-records.sh.in")],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
