"""Free-space preflight in the records run wrapper (Fable audit F4(a), FL-2).

The rendered ``run-records.sh`` must refuse to start a run when the runtime
filesystem has less than ``MIN_FREE_BYTES`` available: exit 78, a log line,
a schema-valid run report (``sync`` ``DISK_SPACE_LOW`` plus a blocking
needs-G item) and a notification, with no Docker call and no stage work.
``render.py`` must validate the placeholder.

Synthetic only: a fake ``df`` on PATH, the fake docker shim, the stub stage
backend and the loopback fake Telegram server from
``tests/test_records_runtime.py``. No network, no records.
"""
import importlib.util
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tests.test_records_runtime import (FAKE_IMAGE, ROOT, RUNTIME, STAGES, STUB_TEMPLATE,
                                        FakeTelegram, RuntimeHarness)

MIB = 1024 ** 2
FLOOR = 64 * MIB
REAL_DF = shutil.which("df")

FAKE_DF = r"""#!/bin/sh
# Synthetic df. Mode in $STATE/df-mode:
#   real | avail:<KiB> | fail | garbage | hang | okfail | okhang
# okfail/okhang print a valid, roomy reading and then fail or hang, so a
# wrapper that only parses stdout (and drops df's status) would admit the run.
STATE=@STATE@
echo "$*" >>"$STATE/df-calls"
mode=$(cat "$STATE/df-mode" 2>/dev/null || echo real)
case "$mode" in
  real) exec @REAL_DF@ "$@";;
  avail:*)
    # Device and mount point both contain spaces, as a hostile layout would.
    printf '%s\n' "Filesystem 1024-blocks Used Available Capacity Mounted on"
    printf 'synthetic dev 999999999 1 %s 50%% /synthetic mnt\n' "${mode#avail:}"
    exit 0;;
  fail) echo "df: synthetic failure" >&2; exit 1;;
  garbage) printf '%s\n' "Filesystem" "not a df line"; exit 0;;
  hang) exec sleep 60;;
  okfail|okhang)
    printf '%s\n' "Filesystem 1024-blocks Used Available Capacity Mounted on"
    printf '%s\n' "synthetic 999999999 1 999999998 1% /synthetic"
    [ "$mode" = okfail ] && exit 1
    exec sleep 60;;
esac
exit 2
"""


class DiskPreflightTests(RuntimeHarness):
    def setUp(self):
        super().setUp()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        df = self.bin / "df"
        df.write_text(FAKE_DF.replace("@STATE@", str(self.state)).replace("@REAL_DF@", REAL_DF))
        df.chmod(stat.S_IRWXU)

    def run_script(self, *args, api=None, timeout=90):
        # Same as the harness, with the fake df first on PATH.
        env = {"PATH": f"{self.bin}:" + os.environ.get("PATH", "/usr/bin:/bin"),
               "RECORDS_NOTIFY_API_BASE": api or self.api}
        return subprocess.run(["/bin/sh", str(self.script), *args], env=env,
                              capture_output=True, text=True, timeout=timeout)

    def df_mode(self, mode):
        (self.state / "df-mode").write_text(mode + "\n")

    def df_calls(self):
        path = self.state / "df-calls"
        return path.read_text().splitlines() if path.exists() else []

    def assert_refused(self, result, code):
        self.assertEqual(result.returncode, 78, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        report = self.report()
        sync = self.stage(report, "sync")
        self.assertEqual((sync["status"], sync["error_code"]), ("fail", code))
        self.assertTrue(all(self.stage(report, name)["status"] == "skipped"
                            for name in STAGES[1:-1]))
        self.assertEqual(report["needs_g"], [{"code": code, "stage": "sync",
                                              "severity": "blocking", "count": 1}])
        self.assertEqual(report["overall_status"], "fail")
        # No Docker call, so no stage work could have run.
        self.assertEqual(self.calls(), [])
        self.assertEqual(len(FakeTelegram.calls), 1)
        self.assertIn("refused+to+start+%28runtime+disk+space", FakeTelegram.calls[0][1])
        self.assertFalse(report["notify_failed"])
        return report

    # --- acceptance: below threshold refuses with 78 and a report line -------
    def test_below_threshold_refuses_78_with_report_and_log_line(self):
        self.df_mode(f"avail:{FLOOR // 1024 - 1}")
        result = self.run_script()
        self.assert_refused(result, "DISK_SPACE_LOW")
        line = (f"runtime filesystem has {FLOOR - 1024} bytes available, below "
                f"MIN_FREE_BYTES={FLOOR}; no records work was attempted")
        self.assertIn(line, self.log())
        self.assertIn(line, result.stderr)
        self.assertIn("finished exit=78", self.log())
        self.assertEqual(self.df_calls(), [f"-P -k {self.runtime}"])

    def test_exactly_at_threshold_runs(self):
        self.df_mode(f"avail:{FLOOR // 1024}")
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"disk ok: {FLOOR} bytes available (minimum {FLOOR})", self.log())
        self.assertEqual(self.stage(self.report(), "outputs")["status"], "ok")

    def test_real_df_below_a_huge_threshold_refuses(self):
        self.render(MIN_FREE_BYTES=str(1024 ** 5))  # 1 PiB ceiling; no test host has it free
        self.df_mode("real")
        self.assert_refused(self.run_script(), "DISK_SPACE_LOW")
        self.assertIn(f"below MIN_FREE_BYTES={1024 ** 5}", self.log())

    def test_real_df_above_floor_runs(self):
        self.df_mode("real")
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(self.log(), rf"disk ok: \d+ bytes available \(minimum {FLOOR}\)")

    # --- fail closed when free space cannot be measured ---------------------
    def test_df_failure_or_garbage_refuses_unknown(self):
        for mode in ("fail", "garbage"):
            with self.subTest(mode=mode):
                FakeTelegram.calls = []
                shutil.rmtree(self.target_dir, ignore_errors=True)
                self.df_mode(mode)
                self.assert_refused(self.run_script(), "DISK_SPACE_UNKNOWN")
                self.assertIn("could not measure free space", self.log())

    def test_hung_df_is_bounded_and_refuses_unknown(self):
        self.df_mode("hang")
        started = time.monotonic()
        result = self.run_script(timeout=60)
        elapsed = time.monotonic() - started
        self.assert_refused(result, "DISK_SPACE_UNKNOWN")
        # DF_TIMEOUT 10 s + kill grace 2 s, plus orchestrator/notify overhead.
        self.assertLess(elapsed, 40)

    def test_valid_reading_then_nonzero_exit_refuses_unknown(self):
        # Review finding: df's exit status must not be masked by the parser.
        self.df_mode("okfail")
        self.assert_refused(self.run_script(), "DISK_SPACE_UNKNOWN")
        self.assertIn("could not measure free space", self.log())
        self.assertNotIn("disk ok:", self.log())

    def test_valid_reading_then_timeout_refuses_unknown(self):
        self.df_mode("okhang")
        started = time.monotonic()
        result = self.run_script(timeout=60)
        elapsed = time.monotonic() - started
        self.assert_refused(result, "DISK_SPACE_UNKNOWN")
        self.assertIn("could not measure free space", self.log())
        self.assertNotIn("disk ok:", self.log())
        self.assertLess(elapsed, 40)

    # --- a really full disk still exits 78 ----------------------------------
    def test_unwritable_log_still_exits_78_and_reports(self):
        # A directory where the log file should be makes every log append
        # fail, as ENOSPC would. The wrapper must not die on that.
        (self.target_dir / "run-records.log").mkdir(parents=True)
        self.df_mode("avail:0")
        result = self.run_script()
        self.assertEqual(result.returncode, 78, result.stderr)
        self.assertIn("below MIN_FREE_BYTES", result.stderr)
        report = self.report()
        self.assertEqual(self.stage(report, "sync")["error_code"], "DISK_SPACE_LOW")
        self.assertEqual(self.calls(), [])

    # --- ordering, dry run and the wrapper's own value check ----------------
    def test_preflight_runs_after_lock_and_plan_check_before_docker(self):
        (self.runtime / "targets" / "TEST.json").write_text('{"target_config_version": 1}')
        self.df_mode("avail:0")
        result = self.run_script()
        self.assertEqual(result.returncode, 78)
        self.assertEqual(self.stage(self.report(), "sync")["error_code"], "TARGET_CONFIG_INVALID")
        self.assertEqual(self.df_calls(), [])

    def test_dry_run_lists_disk_preflight_and_calls_no_df(self):
        result = self.run_script("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"preflight 2 disk: {self.runtime} filesystem needs at least {FLOOR} "
                      f"bytes available (else exit 78)", result.stdout)
        self.assertEqual(self.df_calls(), [])
        self.assertFalse(self.target_dir.exists())

    def test_wrapper_refuses_zero_or_non_canonical_threshold(self):
        rendered = self.script.read_text()
        self.assertIn(f"MIN_FREE_BYTES={FLOOR}\n", rendered)
        for count, bad in enumerate(("0", "0123", "", "1e9"), start=1):
            with self.subTest(bad=bad):
                self.script.write_text(rendered.replace(f"MIN_FREE_BYTES={FLOOR}\n",
                                                        f"MIN_FREE_BYTES={bad}\n"))
                result = self.run_script()
                self.assertEqual(result.returncode, 78)
                refusals = (self.runtime / "preflight-refusals.log").read_text().splitlines()
                self.assertEqual(len(refusals), count)
                self.assertTrue(refusals[-1].endswith("refused=NUMBER_INVALID"), refusals)
                self.assertEqual(self.df_calls(), [])
                self.assertFalse(self.target_dir.exists())


class DiskPreflightRenderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        spec = importlib.util.spec_from_file_location("records_runtime_render_fl2",
                                                      RUNTIME / "render.py")
        self.render = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.render)
        self.values = {
            "RELEASE_COMMIT": "c" * 40, "ENGINE_DIR": "/srv/engine", "DOCKER": "/usr/bin/docker",
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
        self.out = 0

    def do_render(self, **overrides):
        self.out += 1
        values = {key: value for key, value in {**self.values, **overrides}.items()
                  if value is not None}
        out = self.tmp / f"out{self.out}"
        self.render.render(values, [("TEST", str(STUB_TEMPLATE))], out)
        return out

    def assert_refused(self, fragment, **overrides):
        with self.assertRaises(ValueError) as caught:
            self.do_render(**overrides)
        self.assertIn(fragment, str(caught.exception))
        return str(caught.exception)

    def test_rendered_wrapper_carries_the_threshold(self):
        out = self.do_render()
        script = (out / "run-records.sh").read_text()
        self.assertIn("MIN_FREE_BYTES=2147483648\n", script)
        self.assertNotIn("@MIN_FREE_BYTES@", script)

    def test_missing_threshold_is_refused(self):
        self.assert_refused("no value for ['MIN_FREE_BYTES']", MIN_FREE_BYTES=None)

    def test_non_canonical_or_non_numeric_threshold_is_refused(self):
        for bad in ("0", "0067108864", "-1", "1.5", "2G", "", " 67108864"):
            with self.subTest(bad=bad):
                message = self.assert_refused("MIN_FREE_BYTES", MIN_FREE_BYTES=bad)
                self.assertNotIn("Traceback", message)

    def test_threshold_bounds(self):
        self.assert_refused("64 MiB", MIN_FREE_BYTES=str(FLOOR - 1))
        self.assert_refused("bytes, not GiB", MIN_FREE_BYTES="2")
        self.assert_refused("1 PiB", MIN_FREE_BYTES=str(1024 ** 5 + 1))
        self.do_render(MIN_FREE_BYTES=str(FLOOR))
        self.do_render(MIN_FREE_BYTES=str(1024 ** 5))

    def test_template_without_placeholder_is_refused(self):
        copy = self.tmp / "runtime-copy"
        shutil.copytree(RUNTIME, copy, ignore=shutil.ignore_patterns("__pycache__"))
        wrapper = copy / "run-records.sh.in"
        text = wrapper.read_text()
        self.assertIn("MIN_FREE_BYTES=@MIN_FREE_BYTES@", text)
        wrapper.write_text(text.replace("MIN_FREE_BYTES=@MIN_FREE_BYTES@",
                                        "MIN_FREE_BYTES=67108864"))
        self.render.HERE = copy
        self.assert_refused("lost required placeholder(s) ['MIN_FREE_BYTES']")

    def test_budget_counts_the_df_probe(self):
        # 600 + 3 * (20 + 2) = 666 s of Docker readiness fit a 670 s budget on
        # their own; the 12 s df probe in front of them does not.
        message = self.assert_refused("readiness", RUN_BUDGET_SECONDS="670")
        self.assertIn("678s", message)
        self.do_render(RUN_BUDGET_SECONDS="679")


class DiskPreflightNotifyTests(unittest.TestCase):
    def test_notify_message_names_the_refusal(self):
        spec = importlib.util.spec_from_file_location("records_runtime_notify_fl2",
                                                      RUNTIME / "notify.py")
        notify = importlib.util.module_from_spec(spec)
        sys.path.insert(0, str(ROOT))
        spec.loader.exec_module(notify)
        from campaign_tool.records import run_orchestrator, run_report
        run_id = "run_20260101T000000Z_0123abcd"
        for code, words in (("DISK_SPACE_LOW", "below the configured minimum"),
                            ("DISK_SPACE_UNKNOWN", "could not be measured")):
            # The same builder `orch abort` uses for the wrapper's report.
            report = run_orchestrator.abort_report(run_id, "test", "2026-01-01T00:00:00Z", code)
            run_report.validate_run_report(report)
            text = notify.compose(report, "Synthetic", 78)
            self.assertIn(f"records run {run_id} (test) refused to start", text)
            self.assertIn(words, text)
            self.assertIn("No records work was attempted", text)
            self.assertIn("Needs G: 1 item(s).", text)
            self.assertNotIn("Coverage:", text)


if __name__ == "__main__":
    unittest.main()
