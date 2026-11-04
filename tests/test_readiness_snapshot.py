"""Synthetic-only projection and fail-closed contract; no network or live campaign."""
import contextlib
import io
import json
import re
import socket
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from campaign_tool import cli
from tools.flock_privacy import categories

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "schemas/readiness-snapshot-v1.json").read_text())
PLANTED = ["Synthetic Secret Agency", "person@example.invalid", "805-555-1234", "123 Synthetic Avenue", "7ABC123"]


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="synthetic-readiness-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "campaign"
        result = self.command("init", "--name", PLANTED[0], "--county", "Example", "--state", "NV")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.config = self.root / "campaign.json"
        self.cfg = cli.read_config(self.root)
        self.cfg.update(campaign_id="synthetic-campaign", synthetic_private=PLANTED)
        self.config.write_text(json.dumps(self.cfg))
        (self.root / "kit").mkdir(exist_ok=True)
        (self.root / "kit" / "private-fixture.json").write_text(json.dumps(PLANTED))
        (self.root / "content").mkdir()
        (self.root / "content" / "private-fixture.json").write_text(json.dumps(PLANTED))
        self.out = self.root / "snapshot.json"

    def command(self, command, *args):
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", command,
                               "--directory", str(self.root), *args], cwd=ROOT,
                              capture_output=True, text=True, timeout=30)

    def emit(self, **kwargs):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), patch.object(socket, "socket", side_effect=AssertionError("no network")):
            cli.readiness(self.root, **kwargs)
        return buf.getvalue()

    def assert_schema(self, value, schema=SCHEMA):
        # Validate every keyword used by this dependency-free repository's schema.
        if "const" in schema:
            self.assertEqual(value, schema["const"])
        if "enum" in schema:
            self.assertIn(value, schema["enum"])
        if "type" in schema:
            types = {"object": dict, "array": list, "string": str, "boolean": bool, "integer": int}
            self.assertIs(type(value), types[schema["type"]])
        if isinstance(value, dict):
            self.assertTrue(set(schema["required"]) <= value.keys())
            if schema.get("additionalProperties") is False:
                self.assertTrue(value.keys() <= schema["properties"].keys())
            for key, item in value.items():
                self.assert_schema(item, schema["properties"][key])
        if isinstance(value, list):
            self.assertLessEqual(len(value), schema["maxItems"])
            for item in value:
                self.assert_schema(item, schema["items"])
        if isinstance(value, str):
            self.assertGreaterEqual(len(value), schema.get("minLength", 0))
            self.assertLessEqual(len(value), schema.get("maxLength", len(value)))
            if "pattern" in schema:
                self.assertRegex(value, schema["pattern"])
            if schema.get("format") == "date-time":
                self.assertEqual(datetime.fromisoformat(value).utcoffset().total_seconds(), 0)
        if type(value) is int and "minimum" in schema:
            self.assertGreaterEqual(value, schema["minimum"])

    def test_projection_matches_doctor_and_schema_without_private_values(self):
        before = self.command("doctor")
        self.assertEqual(before.returncode, 0, before.stderr)
        with patch.object(cli, "doctor_report", wraps=cli.doctor_report) as doctor:
            text = self.emit()
        doctor.assert_called_once_with(self.root, None, None)
        snapshot = json.loads(text)
        self.assert_schema(snapshot)
        self.assertEqual(snapshot["next_actions"], json.loads(before.stdout)["next_actions"])
        for planted in PLANTED:
            self.assertNotIn(planted, text)
        for sentence in snapshot["next_actions"]:
            self.assertEqual(categories(sentence), [])
            self.assertNotRegex(sentence, r"(?i)violat|unlawful|illegal|non-?complian|breach")
        self.assertEqual(self.command("doctor").stdout, before.stdout)
        result = self.command("readiness", "--out", str(self.out))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assert_schema(json.loads(self.out.read_text()))
        result = self.command("readiness", "--campaign-id", "explicit-id")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["campaign_id"], "explicit-id")

    def test_age_flags_project_real_doctor_staleness(self):
        old = (date.today() - timedelta(days=200)).isoformat()
        contacts = [{"agency_id": f"synthetic-{i}", "name": PLANTED[0], "kind": "police",
                     "selected": True, "verified": True,
                     "verification": {"checked_on": old}} for i in range(3)]
        (self.root / "kit" / "agencies.json").write_text(json.dumps(contacts))
        (self.root / "kit" / "meetings.json").write_text(json.dumps({
            "schema_version": 1, "generated": old + "T00:00:00+00:00", "meetings": []}))
        for days, expected_contacts, meeting_stale in ((180, 3, True), (3650, 0, False)):
            with self.subTest(days=days):
                flags = ("--contact-max-age-days", str(days), "--stale-days", str(days))
                doctor = self.command("doctor", *flags)
                self.assertEqual(doctor.returncode, 0, doctor.stderr)
                report = json.loads(doctor.stdout)
                self.assertEqual(len(report["stale_contacts"]), expected_contacts)
                self.assertEqual("kit/meetings.json" in [e["input"] for e in report["stale_inputs"]],
                                 meeting_stale)
                result = self.command("readiness", *flags)
                self.assertEqual(result.returncode, 0, result.stderr)
                snapshot = json.loads(result.stdout)
                self.assert_schema(snapshot)
                for key in ("stale_contacts", "stale_inputs"):
                    self.assertEqual(snapshot["counts"][key], len(report[key]) + report.get(key + "_more", 0))
                self.assertEqual(snapshot["next_actions"], report["next_actions"])
                for planted in PLANTED:
                    self.assertNotIn(planted, result.stdout)

    def test_invalid_age_flags_refuse_without_output(self):
        for flag, values in (("--contact-max-age-days", ("0", "-1", "abc")),
                             ("--stale-days", ("0", "-1", "3651", "abc"))):
            for value in values:
                with self.subTest(flag=flag, value=value):
                    result = self.command("readiness", flag, value, "--out", str(self.out))
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn(flag, result.stderr)
                    self.assertEqual(result.stdout, "")
                    self.assertFalse(self.out.exists())

    def test_large_lists_and_doctor_truncation_become_counts_only(self):
        report = cli.doctor_report(self.root)
        report.update(stale_contacts=[PLANTED] * 150, stale_inputs=[PLANTED] * 120,
                      stale_comment_kits=[PLANTED], contracts_ending=[PLANTED] * 2,
                      correction_requests_open_over_14_days=3)
        with patch.object(cli, "doctor_report", return_value=report):
            text = self.emit()
        snapshot = json.loads(text)
        self.assertEqual(snapshot["counts"], dict(stale_contacts=150, stale_inputs=120,
                         stale_comment_kits=1, contracts_ending=2, correction_requests_open_over_14_days=3))
        self.assert_schema(snapshot)
        for key in snapshot["counts"]:
            self.assertNotIn(key, {k for k in snapshot if k != "counts"})
        for planted in PLANTED:
            self.assertNotIn(planted, text)
        report.update(stale_contacts=[PLANTED] * 100, stale_contacts_more=50,
                      stale_inputs=[PLANTED] * 100, stale_inputs_more=20)
        with patch.object(cli, "doctor_report", return_value=report):
            self.assertEqual(json.loads(self.emit())["counts"], snapshot["counts"])

    def assert_refuses_without_output_or_post(self, reason, **kwargs):
        stdout = io.StringIO()
        original = self.out.read_bytes() if self.out.exists() else None
        with contextlib.redirect_stdout(stdout), patch.object(socket, "socket") as network:
            with self.assertRaisesRegex(ValueError, reason) as caught:
                cli.readiness(self.root, out=self.out, **kwargs)
        self.assertEqual(stdout.getvalue(), "")
        network.assert_not_called()
        self.assertEqual(self.out.read_bytes() if self.out.exists() else None, original)
        for planted in PLANTED:
            self.assertNotIn(planted, str(caught.exception))

    def test_missing_campaign_id_refuses_before_write_or_post(self):
        del self.cfg["campaign_id"]
        self.config.write_text(json.dumps(self.cfg))
        self.assert_refuses_without_output_or_post("readiness_campaign_id_absent")
        result = self.command("readiness", "--out", str(self.out))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("readiness_campaign_id_absent", result.stderr)
        self.assertFalse(self.out.exists())
        for invalid in ("", " "):
            self.assert_refuses_without_output_or_post("readiness_campaign_id_absent", campaign_id=invalid)
        self.assertEqual(json.loads(self.emit(campaign_id="explicit-id"))["campaign_id"], "explicit-id")

    def test_unsafe_next_actions_refuse_before_write_or_post(self):
        report = cli.doctor_report(self.root)
        for text, category in [(PLANTED[1], "email"), (PLANTED[2], "phone"), (PLANTED[4], "plate"),
                               ("person [at] example [dot] invalid", "email")]:
            with self.subTest(category=category), patch.object(cli, "doctor_report", return_value={**report, "next_actions": ["Review the packet.", text]}):
                self.assert_refuses_without_output_or_post(f"index 1, category {category}")
                self.out.write_text("existing-snapshot")
                self.assert_refuses_without_output_or_post(f"index 1, category {category}")
                self.out.unlink()

    def test_real_doctor_unsafe_meeting_id_refuses_without_output(self):
        with patch("campaign_tool.meeting_stage.stale_kits", return_value=[{"meeting": PLANTED[4]}]):
            self.assertIn(PLANTED[4], json.dumps(cli.doctor_report(self.root)))
            self.assert_refuses_without_output_or_post("readiness_unsafe_next_action: index 4, category plate")

    def test_snapshot_limits_refuse_without_truncating_or_rewording(self):
        report = cli.doctor_report(self.root)
        for actions, reason in [(["x" * 241], "readiness_next_action_length"), (["Review."] * 41, "readiness_next_actions_count")]:
            with patch.object(cli, "doctor_report", return_value={**report, "next_actions": actions}):
                self.assert_refuses_without_output_or_post(reason)
        with patch.object(cli, "doctor_report", return_value={**report, "next_actions": ["x" * 240] * 40}):
            snapshot = json.loads(self.emit())
            self.assertEqual(snapshot["next_actions"], ["x" * 240] * 40)
            self.assert_schema(snapshot)

    def test_static_transport_and_view_boundaries(self):
        source = (ROOT / "workers/workspace/src/readiness.ts").read_text()
        self.assertNotRegex(source, r"views/|<html|<div")
        self.assertIn('from tools.flock_privacy import categories', (ROOT / "campaign_tool/cli.py").read_text())


if __name__ == "__main__":
    unittest.main()
