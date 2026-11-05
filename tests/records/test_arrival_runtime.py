"""Rendered arrival wrapper, path/service units and drain races, offline (REC-AUTO-1)."""
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tests import test_records_runtime as harness  # noqa: E402
from campaign_tool.records import arrival, run_report  # noqa: E402

REF_A = hashlib.sha256(b"a").hexdigest()
REF_B = hashlib.sha256(b"b").hexdigest()
REF_C = hashlib.sha256(b"c").hexdigest()


class ArrivalRuntime(harness.RuntimeHarness):
    def setUp(self):
        super().setUp()
        os.chmod(self.runtime, 0o700)
        self.drop = self.tmp / "drop"

    def run_wrapper(self, *args, env=None, timeout=120):
        base = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "RECORDS_NOTIFY_API_BASE": self.api,
                "ARRIVAL_QUIET_SECONDS": "0"}
        return subprocess.run(["/bin/sh", str(self.script), *args], env={**base, **(env or {})},
                              capture_output=True, text=True, timeout=timeout)

    def stamp(self, source="inbox-watch", ref=REF_A):
        return arrival.stamp(self.runtime, source, ref)

    def reports(self):
        paths = sorted((self.target_dir / "reports").glob("run_*.json")) if self.target_dir.exists() else []
        out = [json.loads(path.read_text()) for path in paths]
        for report in out:
            run_report.validate_run_report(report)
        return out

    def docker_calls(self):
        return self.calls()

    def wait_for(self, predicate, seconds=30):
        end = time.time() + seconds
        while time.time() < end:
            if predicate():
                return True
            time.sleep(0.05)
        return False


class WrapperInterfaceTests(ArrivalRuntime):
    def test_usage_errors_and_dry_run_touch_nothing(self):
        for args in (["--arrival", "--scheduled"], ["--arrival", "--target", "OTHER"], ["--arrival", "--bogus"]):
            self.assertEqual(self.run_wrapper(*args).returncode, 64, args)
        before = sorted(os.listdir(self.runtime))
        result = self.run_wrapper("--arrival", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("arrival: target TEST (SCHEDULED_TARGET)", result.stdout)
        self.assertIn(str(self.drop), result.stdout)
        self.assertIn("arrival.pending", result.stdout)
        self.assertIn("at most one follow-up", result.stdout)
        self.assertEqual(sorted(os.listdir(self.runtime)), before)
        self.assertEqual(self.docker_calls(), [])
        same = self.run_wrapper("--arrival", "--target", "TEST", "--dry-run")
        self.assertEqual(same.returncode, 0, same.stderr)

    def test_idle_wake_does_nothing(self):
        result = self.run_wrapper("--arrival")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.docker_calls(), [])
        self.assertEqual(self.reports(), [])
        self.assertEqual(harness.FakeTelegram.calls, [])
        self.assertFalse((self.runtime / "arrival").exists())


class ArrivalRunTests(ArrivalRuntime):
    def test_stamp_starts_one_incremental_run_with_ref_only_batch(self):
        name = self.stamp()
        seen = json.loads((self.runtime / "arrival" / name).read_text())["seen_at"]
        result = self.run_wrapper("--arrival")
        self.assertEqual(result.returncode, 0, result.stderr)
        [report] = self.reports()
        self.assertEqual((report["mode"], report["trigger"]), ("arrival", "arrival"))
        self.assertEqual(report["arrivals"], [{"source": "inbox-watch", "ref": REF_A, "seen_at": seen}])
        self.assertEqual([s["name"] for s in report["stages"]], list(harness.STAGES))
        self.assertEqual(self.stage(report, "sync")["status"], "ok")
        self.assertTrue(report["run_id"].startswith("run_"))
        self.assertEqual(arrival.pending(self.runtime), [])
        self.assertTrue((self.runtime / "arrival" / "done" / report["run_id"] / name).exists())
        self.assertNotIn("DOCKER_RUN", " ".join(self.docker_calls()))
        for call in harness.FakeTelegram.calls:  # notify stays generic: never renders opaque refs
            self.assertNotIn(REF_A, harness.sent_text(call))
        self.assertEqual(self.run_wrapper("--arrival").returncode, 0)  # nothing new: no second run
        self.assertEqual(len(self.reports()), 1)

    def test_completed_drop_file_starts_a_run_and_retained_folder_does_not(self):
        self.drop.mkdir()
        scan = self.drop / "scan.pdf"
        scan.write_bytes(b"%PDF-1.4 synthetic")
        os.utime(scan, (time.time() - 60,) * 2)
        (self.drop / "in-progress.pdf.part").write_bytes(b"partial")
        self.assertEqual(self.run_wrapper("--arrival").returncode, 0)
        [report] = self.reports()
        self.assertEqual([a["source"] for a in report["arrivals"]], ["drop-folder"])
        self.assertNotIn("scan", json.dumps(report))
        self.assertEqual(self.run_wrapper("--arrival").returncode, 0)  # retained originals: quiet
        self.assertEqual(len(self.reports()), 1)
        scan.write_bytes(b"%PDF-1.4 synthetic, changed")
        os.utime(scan, (time.time() - 30,) * 2)
        self.assertEqual(self.run_wrapper("--arrival").returncode, 0)
        self.assertEqual(len(self.reports()), 2)

    def test_fresh_renamed_drop_file_still_starts_a_run_without_backdating(self):
        self.drop.mkdir()
        staging = self.drop / "scan.pdf.part"
        staging.write_bytes(b"%PDF-1.4 synthetic")
        os.rename(staging, self.drop / "scan.pdf")  # mtime is now: inside the settle window
        self.assertEqual(self.run_wrapper("--arrival").returncode, 0)
        [report] = self.reports()
        self.assertEqual([a["source"] for a in report["arrivals"]], ["drop-folder"])

    def test_fresh_earlier_page_rename_after_a_full_retained_prefix_is_still_stamped(self):
        self.drop.mkdir()
        old = time.time() - 600
        inventory = {}
        for n in range(arrival.MAX_DROP_FILES + 1):
            name = f"m{n:05d}.pdf"
            path = self.drop / name
            path.write_bytes(b"x")
            os.utime(path, (old, old))
            st = os.lstat(path)
            inventory[hashlib.sha256(name.encode()).hexdigest()] = arrival.drop_ref(name, st.st_size, st.st_mtime_ns)
        # The retained prefix is already acknowledged (seeded directly: 5,001 fsynced stamps are slow).
        inv = self.runtime / arrival.INVENTORY_NAME
        inv.write_text(json.dumps(inventory))
        os.chmod(inv, 0o600)
        self.assertEqual(self.run_wrapper("--arrival").returncode, 0)
        self.assertEqual(self.reports(), [])  # nothing new: quiet
        staging = self.drop / "a-new.pdf.part"
        staging.write_bytes(b"%PDF-1.4 synthetic")
        os.rename(staging, self.drop / "a-new.pdf")  # fresh mtime, lexically on page one
        result = self.run_wrapper("--arrival", env={"ARRIVAL_MAX_WAIT_SECONDS": "20"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.reports()), 1)

    def test_burst_is_one_batch_and_cannot_extend_the_wait_indefinitely(self):
        stop = threading.Event()
        written = []

        def burst():
            n = 0
            while not stop.is_set() and n < 40:
                written.append(self.stamp("drop-folder", hashlib.sha256(bytes([n])).hexdigest()))
                n += 1
                time.sleep(0.3)

        self.stamp()
        thread = threading.Thread(target=burst)
        thread.start()
        started = time.monotonic()
        try:
            result = self.run_wrapper("--arrival", env={"ARRIVAL_QUIET_SECONDS": "1", "ARRIVAL_MAX_WAIT_SECONDS": "2"})
        finally:
            stop.set()
            thread.join()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(time.monotonic() - started, 60)
        first = self.reports()[0]
        # The run began after ~2 s although stamps kept arriving every 0.3 s.
        self.assertGreaterEqual(len(first["arrivals"]), 2)
        total = len(first["arrivals"]) + len(arrival.pending(self.runtime)) + sum(
            len(r["arrivals"]) for r in self.reports()[1:])
        self.assertEqual(total, len(written) + 1)
        self.assertLess(len(first["arrivals"]), len(written) + 1 if len(written) > 12 else 99)

    def test_stage_failure_keeps_the_attempted_batch_on_its_report(self):
        self.stage_spec("extract").update(result={"status": "fail", "error_code": "EXTRACT_ERROR",
                                                  "counts": {"input": 1, "output": 0, "failed": 1, "skipped": 0}})
        self.write_fixture()
        self.stamp()
        self.assertEqual(self.run_wrapper("--arrival").returncode, 14)
        [report] = self.reports()
        self.assertEqual(len(report["arrivals"]), 1)
        self.assertEqual(self.stage(report, "extract")["status"], "fail")
        self.assertEqual(arrival.pending(self.runtime), [])


class PreflightAbortTests(ArrivalRuntime):
    def test_disk_low_aborts_with_trigger_arrival_and_keeps_every_stamp(self):
        self.render(MIN_FREE_BYTES=str(1024 ** 5))
        self.stamp()
        self.assertEqual(self.run_wrapper("--arrival").returncode, 78)
        [report] = self.reports()
        self.assertEqual((report["mode"], report["trigger"], report["arrivals"]), ("arrival", "arrival", []))
        self.assertEqual(self.stage(report, "sync")["error_code"], "DISK_SPACE_LOW")
        self.assertEqual(len(arrival.pending(self.runtime)), 1)
        self.assertTrue((self.runtime / "arrival.pending").exists())  # queued wake stays visible
        self.assertEqual(self.docker_calls(), [])

    def test_docker_unavailable_keeps_stamps_pending_and_does_not_retry_in_a_loop(self):
        (self.state / "up-after").write_text("999999\n")
        self.stamp()
        self.assertEqual(self.run_wrapper("--arrival").returncode, 75)
        [report] = self.reports()
        self.assertEqual(report["arrivals"], [])
        self.assertEqual(len(arrival.pending(self.runtime)), 1)
        self.assertEqual(len(self.reports()), 1)  # no immediate failure retry


class LockContentionTests(ArrivalRuntime):
    def hold_lock(self):
        fd = os.open(self.runtime / "run-records.lock", os.O_WRONLY | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.addCleanup(os.close, fd)
        return fd

    def test_busy_arrival_queues_exit_zero_without_work_or_consumption(self):
        self.hold_lock()
        self.stamp()
        self.stamp("drop-folder", REF_B)
        for _ in range(3):
            result = self.run_wrapper("--arrival")
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.runtime / "arrival.pending").exists())
        self.assertEqual(stat.S_IMODE((self.runtime / "arrival.pending").stat().st_mode), 0o600)
        self.assertEqual(len(arrival.pending(self.runtime)), 2)
        self.assertEqual(self.docker_calls(), [])
        self.assertEqual(self.reports(), [])
        self.assertEqual(harness.FakeTelegram.calls, [])

    def test_busy_scheduled_and_manual_still_skip_with_73(self):
        self.hold_lock()
        for args in (["--scheduled"], []):
            self.assertEqual(self.run_wrapper(*args).returncode, 73)
        self.assertEqual(len(list((self.target_dir / "alerts").glob("run-skipped-*.json"))), 2)
        self.assertFalse((self.runtime / "arrival.pending").exists())

    def test_marker_write_failure_is_a_visible_refusal(self):
        self.hold_lock()
        self.stamp()
        (self.runtime / "arrival.pending").mkdir()
        result = self.run_wrapper("--arrival")
        self.assertEqual(result.returncode, 78)
        self.assertEqual(len(arrival.pending(self.runtime)), 1)


class DrainTests(ArrivalRuntime):
    def start_holder(self, *args):
        self.stage_spec("sync").update(sleep_seconds=3)
        self.write_fixture()
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "RECORDS_NOTIFY_API_BASE": self.api,
               "ARRIVAL_QUIET_SECONDS": "0"}
        holder = subprocess.Popen(["/bin/sh", str(self.script), *args], env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(holder.kill)
        self.assertTrue(self.wait_for(lambda: (self.target_dir / "run-records.log").exists()
                                      and "docker ready" in self.log()), "holder never reached its stages")
        if args == ("--arrival",):  # an arrival parent claims its batch after "docker ready"
            done = self.runtime / "arrival" / "done"
            self.assertTrue(self.wait_for(lambda: any(done.glob("run_*/*"))), "parent never claimed")
        return holder

    def test_finishing_holder_drains_exactly_one_follow_up(self):
        for parent_args in ([], ["--scheduled"], ["--arrival"]):
            with self.subTest(parent=parent_args):
                shutil.rmtree(self.target_dir, ignore_errors=True)
                if parent_args == ["--arrival"]:
                    self.stamp("inbox-watch", REF_C)
                holder = self.start_holder(*parent_args)
                self.stamp("inbox-watch", REF_A)
                self.stamp("drop-folder", REF_B)
                self.assertEqual(self.run_wrapper("--arrival").returncode, 0)  # busy: queued
                self.assertTrue((self.runtime / "arrival.pending").exists())
                self.assertEqual(holder.wait(timeout=120), 0, holder.stderr.read())
                reports = self.reports()
                self.assertEqual(len(reports), 2, [r["mode"] for r in reports])
                parent, follow = sorted(reports, key=lambda r: r["started_at"] + r["run_id"])
                self.assertNotEqual(parent["run_id"], follow["run_id"])
                self.assertEqual((follow["mode"], follow["trigger"]), ("arrival", "arrival"))
                self.assertEqual(sorted(a["ref"] for a in follow["arrivals"]), sorted([REF_A, REF_B]))
                self.assertEqual(parent["arrivals"], [] if parent_args != ["--arrival"] else parent["arrivals"])
                self.assertEqual([s["name"] for s in follow["stages"]], list(harness.STAGES))
                self.assertEqual(arrival.pending(self.runtime), [])
                self.assertFalse((self.runtime / "arrival.pending").exists())
                self.assertIn("starting one arrival follow-up run", self.log())
                shutil.rmtree(self.runtime / "arrival", ignore_errors=True)

    def test_events_during_the_follow_up_wait_for_a_fresh_wake_and_never_chain(self):
        holder = self.start_holder()
        first = self.stamp("inbox-watch", REF_A)
        self.assertEqual(self.run_wrapper("--arrival").returncode, 0)
        done = self.runtime / "arrival" / "done"
        self.assertTrue(self.wait_for(lambda: any(done.glob(f"run_*/{first}"))), "follow-up never claimed")
        late = self.stamp("drop-folder", REF_B)
        self.assertEqual(holder.wait(timeout=120), 0, holder.stderr.read())
        self.assertEqual(len(self.reports()), 2)  # no third run, no recursive chain
        self.assertEqual([s["ref"] for s in arrival.pending(self.runtime)], [REF_B])
        self.assertTrue((self.runtime / "arrival" / late).exists())
        # A fresh path activation then runs the next bounded batch.
        self.stage_spec("sync").update(sleep_seconds=0)
        self.write_fixture()
        self.assertEqual(self.run_wrapper("--arrival").returncode, 0)
        self.assertEqual(len(self.reports()), 3)
        self.assertEqual(arrival.pending(self.runtime), [])

    def test_no_follow_up_after_a_preflight_failure_and_stamps_survive(self):
        (self.state / "up-after").write_text("999999\n")
        self.stamp()
        self.assertEqual(self.run_wrapper().returncode, 75)  # manual holder, docker down
        self.assertEqual(len(self.reports()), 1)
        self.assertEqual(len(arrival.pending(self.runtime)), 1)

    def test_follow_up_that_does_not_fit_the_invocation_deadline_is_deferred(self):
        self.render(RUN_BUDGET_SECONDS="100")
        self.stamp()
        self.assertEqual(self.run_wrapper().returncode, 0)
        self.assertEqual(len(self.reports()), 1)
        self.assertIn("ARRIVAL_FOLLOWUP_DEFERRED", self.log())
        self.assertEqual(len(arrival.pending(self.runtime)), 1)  # retained for the next wake

    def test_follow_up_guard_cannot_start_its_own_fresh_deadline_or_chain(self):
        result = self.run_wrapper("--arrival", env={"RECORDS_ARRIVAL_FOLLOWUP": "1"})
        self.assertEqual(result.returncode, 78)  # no inherited deadline: refused
        self.assertEqual(self.run_wrapper(env={"RECORDS_ARRIVAL_FOLLOWUP": "1",
                                               "RECORDS_INVOCATION_DEADLINE": "9999999999"}).returncode, 64)
        self.assertEqual(self.docker_calls(), [])

    def test_lock_fd_does_not_outlive_the_parent(self):
        self.stamp()
        self.assertEqual(self.run_wrapper().returncode, 0)
        fd = os.open(self.runtime / "run-records.lock", os.O_WRONLY)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # raises if any child kept the lock


class RenderArrivalTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        sys.path.insert(0, str(harness.RUNTIME))
        import render  # noqa: PLC0415
        self.render = render
        self.values = {
            "RELEASE_COMMIT": "a" * 40, "ENGINE_DIR": "/srv/engine", "DOCKER": "/usr/bin/docker",
            "TIMEOUT": "/usr/bin/timeout", "FLOCK": "/usr/bin/flock", "CONTAINER": "records",
            "EXPECTED_IMAGE": harness.FAKE_IMAGE, "RUN_UID": "1000", "RUNTIME_DIR": "/srv/runtime",
            "HOST_PYTHON": "/usr/bin/python3", "NOTIFY_PY": "/srv/engine/notify.py",
            "NOTIFY_ENV_FILE": "/srv/notify.env", "NOTIFY_LABEL": "Records",
            "DOCKER_WAIT_ATTEMPTS": "12", "DOCKER_WAIT_DELAY": "5", "DOCKER_WAIT_MAX_DELAY": "60",
            "DOCKER_CMD_TIMEOUT": "20", "DOCKER_WAIT_DEADLINE": "600", "NOTIFY_TIMEOUT": "60",
            "RUN_BUDGET_SECONDS": "2640", "DEFAULT_TARGET": "TEST", "SCHEDULED_TARGET": "TEST",
            "RUN_RECORDS_SH": "/srv/runtime/run-records.sh", "STUB_FIXTURE": "/srv/fixture.json",
            "MIN_FREE_BYTES": "2147483648", "RECORDS_DROP_DIR": "/srv/records-drop",
        }
        self.targets = [("TEST", str(harness.STUB_TEMPLATE))]
        self.n = 0

    def do_render(self, **overrides):
        self.n += 1
        values = {k: v for k, v in {**self.values, **overrides}.items() if v is not None}
        return self.render.render(values, self.targets, self.tmp / f"out{self.n}")

    def test_units_are_rendered_complete_private_and_fully_substituted(self):
        self.do_render()
        out = self.tmp / "out1"
        for name in ("records-arrival.path", "records-arrival.service"):
            self.assertEqual(stat.S_IMODE((out / name).stat().st_mode), 0o600)
        path_unit = (out / "records-arrival.path").read_text()
        self.assertIn("PathChanged=/srv/records-drop\n", path_unit)
        self.assertIn("PathChanged=/srv/runtime/arrival\n", path_unit)
        self.assertIn("Unit=records-arrival.service", path_unit)
        self.assertNotIn("DirectoryNotEmpty", path_unit)
        self.assertNotIn("PathExists", path_unit)
        service = (out / "records-arrival.service").read_text()
        self.assertIn("ExecStart=/bin/sh /srv/runtime/run-records.sh --arrival\n", service)
        self.assertNotIn("--target", service)
        self.assertIn("Type=oneshot", service)
        self.assertIn("UMask=0077", service)
        self.assertIn("TimeoutStartSec=47min", service)
        wrapper = (out / "run-records.sh").read_text()
        self.assertIn("SCHEDULED_TARGET=TEST\n", wrapper)
        self.assertIn("RECORDS_DROP_DIR=/srv/records-drop\n", wrapper)
        for path in out.rglob("*"):
            if path.is_file():
                self.assertIsNone(re.search(r"@[A-Z_]+@", path.read_text()), path.name)
        scheduled = (out / "records-run.service").read_text()
        self.assertIn("--target TEST --scheduled", scheduled)  # existing unit unchanged

    def test_bad_drop_dirs_fail_closed_before_any_output(self):
        for bad in ("relative/drop", "/srv/runtime", "/srv/runtime/arrival", "/srv/runtime/done/in",
                    "/srv", "/", "/srv/records-drop/../x", "/srv//drop", "/srv/drop/"):
            with self.assertRaises(self.render.RenderError, msg=bad):
                self.do_render(RECORDS_DROP_DIR=bad)
        with self.assertRaises(self.render.RenderError):
            self.do_render(RECORDS_DROP_DIR="/srv/drop; reboot")
        with self.assertRaises(self.render.RenderError):
            self.do_render(RECORDS_DROP_DIR=None)
        self.assertFalse(any(p.exists() and any(p.iterdir()) for p in self.tmp.glob("out*")))

    def test_existing_guards_are_untouched(self):
        with self.assertRaises(self.render.RenderError):
            self.do_render(MIN_FREE_BYTES="1")
        with self.assertRaises(self.render.RenderError):
            self.do_render(RUN_BUDGET_SECONDS="2800")


if __name__ == "__main__":
    unittest.main()
