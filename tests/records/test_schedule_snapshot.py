"""Host schedule evidence from synthetic run reports; no project access."""
from contextlib import redirect_stdout
from datetime import timedelta
import io
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from campaign_tool.records import pipeline_health as ph, schedule, schedule_snapshot as ss
from tests.records.test_pipeline_health import Base, NOW, REPO, report_for
from tests import test_records_runtime as rt


class SnapshotTests(Base):
    def snapshot(self):
        path = self.target_dir / "schedule-snapshot.json"
        path.write_text(json.dumps(ss.export(self.target_dir / "reports", NOW)))
        return path

    def test_readable_snapshot_without_access_to_project_and_missed_slot_cli_alert(self):
        path = self.snapshot()
        with patch.object(schedule, "_runs", side_effect=PermissionError):
            current = self.assess(records_root=self.root / "inaccessible", schedule_snapshot_path=path)
        self.assertEqual(self.check(current, "schedule")["state"], "ok")
        self.assertEqual([c for c in current["checks"] if c["check"] not in ("schedule", "disk", "release_links")],
                         [c for c in self.assess()["checks"] if c["check"] not in ("schedule", "disk", "release_links")])
        self.assertEqual(self.check(current, "release_links")["reason_code"], "release_links_unavailable")  # the register is not readable here
        # The inaccessible records root cannot be measured; that is unknown, never ok (and the roomy runtime volume does not hide it).
        disk = self.check(current, "disk")
        self.assertEqual((disk["state"], disk["reason_code"], disk["path_role"]), ("unknown", "disk_unreadable", "records_root"))
        out = io.StringIO()
        with redirect_stdout(out):
            code = ph.main(["--runtime-dir", str(self.runtime), "--target", "t", "--root", str(self.root / "inaccessible"),
                            "--schedule-snapshot", str(path), "--now", (NOW + timedelta(hours=2)).isoformat(),
                            "--record-alerts", "--json"])
        self.assertEqual(code, 3)
        self.assertEqual(self.check(json.loads(out.getvalue()), "schedule")["reason_code"], "missed_slot")
        alert = json.loads((self.target_dir / "alerts/health-schedule-missed_slot.json").read_text())
        self.assertEqual((alert["state"], alert["reason_code"]), ("stop", "missed_slot"))

    def test_missing_unreadable_invalid_and_future_snapshot_never_fall_back_to_ok_ledger(self):
        path = self.target_dir / "schedule-snapshot.json"
        def check_unknown():
            item = self.check(self.assess(schedule_snapshot_path=path), "schedule")
            self.assertEqual((item["state"], item["reason_code"]), ("unknown", "schedule_unreadable"))
        check_unknown()
        original_read = ph._read_json
        def denied(p):
            if Path(p) == path:
                raise PermissionError
            return original_read(p)
        with patch.object(ph, "_read_json", side_effect=denied):
            check_unknown()
        path.write_text('{"schema":')
        check_unknown()
        data = ss.export(self.target_dir / "reports", NOW)
        data["generated_at"] = (NOW + timedelta(seconds=1)).isoformat()
        path.write_text(json.dumps(data))
        check_unknown()
        data = ss.export(self.target_dir / "reports", NOW)
        data["runs"][0]["ended_at"] = (NOW + timedelta(days=1)).isoformat()
        path.write_text(json.dumps(data))
        check_unknown()

    def test_report_status_mapping_custom_calendar_and_test_mode_exclusion(self):
        spec = schedule.Schedule("daily", "07:00,13:00,20:30")
        for status, expected in (("ok", "ok"), ("warn", "gaps"), ("fail", "failed")):
            with self.subTest(status=status):
                report = report_for(NOW - timedelta(minutes=10), "fail" if status == "fail" else "ok")
                if status == "warn":
                    report["overall_status"] = "warn"
                    report["stages"][0]["status"] = "warn"
                    report["stages"][0]["error_code"] = "SYNTHETIC_GAP"
                source = self.target_dir / "reports" / (report["run_id"] + ".json")
                source.write_text(json.dumps(report))
                data = ss.export(self.target_dir / "reports", NOW)
                self.assertEqual(ss.health(data, NOW, spec)["status"], expected)
                self.assertEqual(ss.health(data, NOW + timedelta(days=3), spec)["status"], "missed")
                report["mode"] = "test"
                source.write_text(json.dumps(report))
                self.assertEqual(len(ss.export(self.target_dir / "reports", NOW)["runs"]), 1)

    def test_export_and_atomic_import_cli_allow_only_metadata_and_reject_bad_input(self):
        exported = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.records.schedule_snapshot", "--reports",
                                   str(self.target_dir / "reports")], cwd=REPO, capture_output=True, text=True)
        self.assertEqual(exported.returncode, 0, exported.stderr)
        data = json.loads(exported.stdout)
        self.assertEqual(set(data["runs"][0]), {"started_at", "ended_at", "status"})
        self.assertNotIn(str(self.root), exported.stdout)
        self.assertNotIn("run_id", exported.stdout)
        path = self.target_dir / "schedule-snapshot.json"
        command = [sys.executable, "-B", "-m", "campaign_tool.records.schedule_snapshot", "--out", str(path)]
        installed = subprocess.run(command, input=exported.stdout, cwd=REPO, capture_output=True, text=True)
        self.assertEqual(installed.returncode, 0, installed.stderr)
        self.assertEqual(json.loads(path.read_text()), data)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        before = path.read_bytes()
        for bad in ("", "{}", exported.stdout + "truncated", "x" * (ss.MAX_BYTES + 1)):
            result = subprocess.run(command, input=bad, cwd=REPO, capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(path.read_bytes(), before)
        data["private_text"] = "synthetic forbidden field"
        with self.assertRaises(ValueError):
            ss.validate(data, NOW + timedelta(days=1))


class SnapshotWrapperTests(rt.RuntimeHarness):
    def test_wrapper_installs_host_evidence_without_container_exec_and_preserves_it_on_failure(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        path = self.target_dir / "schedule-snapshot.json"
        self.assertEqual(json.loads(path.read_text())["runs"], [])  # TEST runs cannot satisfy LIVE's calendar
        self.assertIn("schedule snapshot installed", self.log())
        self.assertFalse(any(call.startswith("exec ") for call in self.calls()))
        # Exercise the rendered post-step on the same target's synthetic non-test report.
        archived = next((self.target_dir / "reports").glob("run_*.json"))
        report = json.loads(archived.read_text())
        report["mode"] = "manual"
        archived.write_text(json.dumps(report))
        wrapper = self.script.read_text()
        function = wrapper.split("# SCHEDULE-SNAPSHOT-BEGIN", 1)[1].split("\n", 1)[1].split("# SCHEDULE-SNAPSHOT-END", 1)[0]
        script = self.tmp / "snapshot-step.sh"
        prefix = (f'#!/bin/sh\nset -eu\nENGINE_DIR={REPO}\nHOST_PYTHON={sys.executable}\n'
                  f'TIMEOUT={self.values["TIMEOUT"]}\nTARGET_DIR={self.target_dir}\n'
                  'SCHEDULED_TARGET=TEST\nTARGET=${1:-TEST}\nKILL_AFTER=2\nDOCKER_CMD_TIMEOUT=1\n'
                  'INVOCATION_DEADLINE_USE=$(( $(date +%s) + 10 ))\nlog() { printf "%s\\n" "$*"; }\n')
        script.write_text(prefix + function + '\nschedule_snapshot_step\n')
        subprocess.run(["sh", str(script)], check=True, capture_output=True)
        self.assertEqual(len(json.loads(path.read_text())["runs"]), 1)
        before = path.read_bytes()
        archived.write_text("truncated")
        result = subprocess.run(["sh", str(script)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn("schedule snapshot FAILED", result.stdout)
        self.assertEqual(path.read_bytes(), before)
        result = subprocess.run(["sh", str(script), "OTHER"], check=True, capture_output=True)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(path.read_bytes(), before)
