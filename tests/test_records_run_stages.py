"""Synthetic unit tests for the container-side records stage adapters (T11i/T11j).

``campaign_tool.records.run_stages`` is what a records run executes inside the
Flock container. These tests pin, with synthetic fixtures only (no container,
no network, no real records):

* every stage's result line, validated by the orchestrator's own parser;
* the stdout contract: exactly one JSON line, no paths or exception text;
* the fail-closed portal allowlist path (nothing enrolled, queue not created);
* count-only ledger reads that never create or change a sidecar file, and that
  report a busy ledger (non-empty WAL) instead of reading it;
* the container-side deadline and child process-group kill on timeout;
* the committed TEST stage plan template matching its generator.
"""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import signal
import sqlite3
import stat
import sys
import tempfile
import time
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from campaign_tool.records import native_extract, run_orchestrator, run_stages
from campaign_tool.records import run as records_run
from campaign_tool.records.ledger import store
from campaign_tool.records.portal.policy import PortalError, parse_approval
from tests.test_records_portal import NOW, policy_data
from contextlib import closing

REPO = Path(__file__).resolve().parents[1]
SCHEMA = run_orchestrator.load_stage_result_schema()
HOST = "portal.example.test"


def run_main(argv):
    """Run the adapter's main(); return (exit, parsed result, raw stdout)."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = run_stages.main(argv)
    raw = out.getvalue()
    return code, run_orchestrator.parse_stage_result(raw.encode(), SCHEMA), raw


def eml(body_html):
    return ("From: clerk@example.test\r\nTo: requester@example.test\r\nSubject: Records\r\n"
            "MIME-Version: 1.0\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
            + body_html + "\r\n").encode()


def synthetic_ledger(path, *, extract=None, gated=None, proposals=0, mail_messages=2):
    """Only the tables/columns the count-only stages read (real schema: see smoke test)."""
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE stage_state(original_sha256 TEXT, stage TEXT, status TEXT);
        CREATE TABLE proposals(id TEXT);
        CREATE TABLE mail_messages(id TEXT);
        CREATE TABLE originals(sha256 TEXT);
    """)
    n = 0
    for stage, states in (("extract", extract or {}), *((s, (gated or {}).get(s, {})) for s in run_stages.GATED)):
        for status, count in states.items():
            for _ in range(count):
                n += 1
                con.execute("INSERT INTO stage_state VALUES(?,?,?)", (f"sha{n}", stage, status))
    con.executemany("INSERT INTO proposals VALUES(?)", [(f"p{i}",) for i in range(proposals)])
    con.executemany("INSERT INTO mail_messages VALUES(?)", [(f"m{i}",) for i in range(mail_messages)])
    con.executemany("INSERT INTO originals VALUES(?)", [(f"o{i}",) for i in range(3)])
    con.commit()
    con.close()
    path.chmod(0o600)


class StageTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name) / "records-project"
        self.mail = self.project / "mail-history"
        self.processing = self.project / "processing-v1"
        self.portal = self.project / "portal-queue-v1"
        for directory in (self.mail / "receipts", self.processing, self.portal):
            directory.mkdir(parents=True, mode=0o700)
        (self.mail / "SYNC-STATUS.json").write_text(json.dumps({"status": "ok"}), encoding="utf-8")
        self.ledger = self.processing / "ledger.sqlite"

    def tearDown(self):
        self.temp.cleanup()

    def stage(self, stage, *extra):
        return run_main([stage, "--project", str(self.project), "--timeout", "60", "--run-id", "run-1", *extra])

    def assertContract(self, code, parsed, raw):
        self.assertIsNotNone(parsed, "stage line must validate as records-stage-result-v1")
        self.assertEqual(len(raw.splitlines()), 1, "exactly one stdout line")
        self.assertNotIn(self.temp.name, raw, "no paths in the result line")
        self.assertEqual(code, 1 if parsed["status"] == "fail" else 0)

    def add_receipt(self, name, raw, *, sha=None):
        (self.mail / "messages").mkdir(exist_ok=True)
        (self.mail / "messages" / f"{name}.eml").write_bytes(raw)
        receipt = {"original_eml": {"path": f"messages/{name}.eml",
                                    "sha256": sha or hashlib.sha256(raw).hexdigest()}}
        (self.mail / "receipts" / f"{name}.json").write_text(json.dumps(receipt), encoding="utf-8")

    def adopt_child(self, dry_report, apply_report, dry_code=0, apply_code=0):
        """A fake ``run_child`` returning a dry-run report then an apply report."""
        calls = []

        def child(argv, timeout):
            calls.append(argv)
            apply = "--apply" in argv
            report = apply_report if apply else dry_report
            code = apply_code if apply else dry_code
            noise = b"progress line /private/x\n"
            return code, noise + (json.dumps(report).encode() if report is not None else b"")
        return child, calls


class SyncStageTests(StageTestCase):
    def test_offline_snapshot_counts_receipts(self):
        for i in range(3):
            (self.mail / "receipts" / f"r{i}.json").write_text("{}", encoding="utf-8")
        (self.mail / "receipts" / "note.txt").write_text("x", encoding="utf-8")
        code, parsed, raw = self.stage("sync")
        self.assertContract(code, parsed, raw)
        self.assertEqual((parsed["status"], parsed["counts"]["input"]), ("ok", 3))
        self.assertEqual([i["code"] for i in parsed["needs_g"]], ["SYNC_OFFLINE_SNAPSHOT"])

    def test_missing_export_fails(self):
        (self.mail / "receipts").rmdir()
        code, parsed, raw = self.stage("sync")
        self.assertContract(code, parsed, raw)
        self.assertEqual(parsed["error_code"], "SYNC_EXPORT_MISSING")

    def test_unreadable_status_warns(self):
        (self.mail / "SYNC-STATUS.json").write_text("{not json", encoding="utf-8")
        code, parsed, raw = self.stage("sync")
        self.assertContract(code, parsed, raw)
        self.assertEqual((parsed["status"], parsed["error_code"]), ("warn", "SYNC_STATUS_UNREADABLE"))

    def test_sync_script_outcomes(self):
        script = Path(self.temp.name) / "sync.py"
        script.write_text("import sys; sys.exit(0)\n", encoding="utf-8")
        code, parsed, raw = self.stage("sync", "--sync-script", str(script))
        self.assertContract(code, parsed, raw)
        self.assertEqual(parsed["status"], "ok")
        self.assertNotIn("needs_g", parsed)
        script.write_text("import sys; print('/private/path'); sys.exit(4)\n", encoding="utf-8")
        code, parsed, raw = self.stage("sync", "--sync-script", str(script))
        self.assertContract(code, parsed, raw)
        self.assertEqual(parsed["error_code"], "SYNC_FAILED")
        self.assertNotIn("/private/path", raw)


ADOPT_REPORT = {"receipts": 154, "eligible_new_messages": 36, "already_adopted": 3,
                "coded_gap_messages": 115, "identity_conflicts": {}, "unaccepted_coded_gaps": {},
                "adopted_messages": 36, "resumed_messages": 0, "failures": {}}

# A dry-run report with the owner config present: gate open, adoption work pending.
ADOPT_DRY_REPORT = {"receipts": 154, "eligible_new_messages": 36, "already_adopted": 3,
                    "coded_gap_messages": 115, "identity_conflicts": {},
                    "unaccepted_coded_gaps": {}, "pending_messages": 36,
                    "gate": {"open": True, "state": "enabled"}}


class AdoptionStageTests(StageTestCase):
    def fake_child(self, code, report):
        calls = []

        def child(argv, timeout):
            calls.append(argv)
            noise = b"progress line /private/x\n"
            return code, noise + (json.dumps(report).encode() if report is not None else b"")
        return child, calls

    def test_delta_dry_run(self):
        child, calls = self.fake_child(0, {**ADOPT_REPORT, "coded_gap_messages": 0})
        with mock.patch.object(run_stages, "run_child", child):
            code, parsed, raw = self.stage("delta")
        self.assertContract(code, parsed, raw)
        self.assertNotIn("--apply", calls[0])
        self.assertEqual(parsed["counts"], {"input": 154, "output": 36, "skipped": 3, "failed": 0})

    def test_delta_conflicts_warn_with_blocking_item(self):
        report = {**ADOPT_REPORT, "identity_conflicts": {"identity_verification_path_changed": 3}}
        child, _ = self.fake_child(3, report)
        with mock.patch.object(run_stages, "run_child", child):
            code, parsed, raw = self.stage("delta")
        self.assertContract(code, parsed, raw)
        self.assertEqual((parsed["status"], parsed["error_code"]), ("warn", "DELTA_BLOCKING_CONFLICTS"))
        self.assertIn({"code": "ADOPTION_IDENTITY_CONFLICT", "severity": "blocking", "count": 3}, parsed["needs_g"])
        self.assertEqual(parsed["counts"]["failed"], 3)

    def test_delta_refused_or_failed(self):
        for code_in, report, expected in ((2, {"status": "refused", "code": "x"}, "DELTA_REFUSED"),
                                          (0, None, "DELTA_REFUSED"),
                                          (1, ADOPT_REPORT, "DELTA_FAILED")):
            child, _ = self.fake_child(code_in, report)
            with self.subTest(expected=expected), mock.patch.object(run_stages, "run_child", child):
                code, parsed, raw = self.stage("delta")
                self.assertContract(code, parsed, raw)
                self.assertEqual(parsed["error_code"], expected)

    def test_adoption_not_configured_is_skipped(self):
        code, parsed, raw = self.stage("adoption")
        self.assertContract(code, parsed, raw)
        self.assertEqual((parsed["status"], parsed["error_code"]), ("skipped", "ADOPTION_NOT_CONFIGURED"))

    def test_adoption_apply_counts_coded_gaps(self):
        child, calls = self.adopt_child(ADOPT_DRY_REPORT, ADOPT_REPORT)
        with mock.patch.object(run_stages, "backup_adoption_dbs", return_value=4096), \
                mock.patch.object(run_stages, "run_child", child):
            code, parsed, raw = self.stage("adoption", "--config", "/cfg.json")
        self.assertContract(code, parsed, raw)
        self.assertEqual(len(calls), 2, "one dry-run then one apply")
        self.assertNotIn("--apply", calls[0])
        self.assertIn("--apply", calls[1])
        self.assertEqual(calls[1][calls[1].index("--config") + 1], "/cfg.json")
        self.assertEqual((parsed["status"], parsed["error_code"]), ("warn", "ADOPTION_CODED_GAPS"))
        self.assertEqual(parsed["new_data"], {"documents": 36, "backup_bytes": 4096})
        self.assertEqual(parsed["counts"], {"input": 154, "output": 36, "skipped": 118, "failed": 0})
        self.assertIn({"code": "ADOPTION_CODED_GAPS", "severity": "info", "count": 115}, parsed["needs_g"])

    def test_adoption_rerun_adopts_zero(self):
        dry = {**ADOPT_DRY_REPORT, "pending_messages": 0, "eligible_new_messages": 0}
        apply = {**ADOPT_REPORT, "adopted_messages": 0, "already_adopted": 39}
        child, _ = self.adopt_child(dry, apply)
        with mock.patch.object(run_stages, "run_child", child):
            code, parsed, raw = self.stage("adoption", "--config", "/cfg.json")
        self.assertContract(code, parsed, raw)
        self.assertEqual(parsed["counts"]["output"], 0)
        self.assertNotIn("new_data", parsed)
        self.assertIn({"code": "ADOPT_BACKUP_SKIPPED", "severity": "info", "count": 1}, parsed["needs_g"])

    def test_adoption_blocked_fails(self):
        report = {**ADOPT_REPORT, "unaccepted_coded_gaps": {"other_code": 2}}
        child, _ = self.adopt_child(ADOPT_DRY_REPORT, report, apply_code=3)
        with mock.patch.object(run_stages, "backup_adoption_dbs", return_value=4096), \
                mock.patch.object(run_stages, "run_child", child):
            code, parsed, raw = self.stage("adoption", "--config", "/cfg.json")
        self.assertContract(code, parsed, raw)
        self.assertEqual(parsed["error_code"], "ADOPTION_BLOCKED")
        self.assertIn({"code": "ADOPTION_UNACCEPTED_GAP", "severity": "blocking", "count": 2}, parsed["needs_g"])

    def test_receipt_gap_block_has_a_visible_reason(self):
        report = {**ADOPT_REPORT, "adopted_messages": 0, "gaps": {"duplicate_receipt_identity": 1}}
        # Only the apply is blocked; a read-only dry-run before it (T17-prep's
        # pre-adoption backup decision, #158) sees a clean report. The backup
        # helper is mocked with create=True so this test holds with and without
        # that stage composed in.
        dry = {**report, "gaps": {}, "pending_messages": 1, "gate": {"open": True, "state": "enabled"}}

        def child(argv, timeout):
            apply = "--apply" in argv
            return (3 if apply else 0), json.dumps(report if apply else dry).encode()

        with mock.patch.object(run_stages, "backup_adoption_dbs", return_value=4096, create=True), \
                mock.patch.object(run_stages, "run_child", child):
            code, parsed, raw = self.stage("adoption", "--config", "/cfg.json")
        self.assertContract(code, parsed, raw)
        self.assertEqual((parsed["status"], parsed["error_code"]), ("fail", "ADOPTION_BLOCKED"))
        self.assertIn({"code": "ADOPTION_RECEIPT_GAPS", "severity": "blocking", "count": 1}, parsed["needs_g"])


class AdoptionBackupTests(StageTestCase):
    """Synthetic tests for the per-run pre-adoption ledger/intake backup."""

    def synthetic_intake(self):
        path = self.processing / "intake" / "intake.sqlite"
        path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE runs(id TEXT)")
        con.execute("INSERT INTO runs VALUES('baseline')")
        con.commit()
        con.close()
        path.chmod(0o600)
        return path

    def backups_dir(self):
        return self.processing / "backups"

    def run_dir(self, run_id="run-1"):
        return self.backups_dir() / run_id

    def read_manifest(self, run_id="run-1"):
        return json.loads((self.run_dir(run_id) / run_stages.BACKUP_MANIFEST).read_text(encoding="utf-8"))

    def test_backup_taken_and_integrity_ok_before_apply(self):
        synthetic_ledger(self.ledger, mail_messages=5)
        self.synthetic_intake()
        seen = {}

        def child(argv, timeout):
            apply = "--apply" in argv
            if apply:
                # The backup must already exist before the apply child runs.
                seen["backup_exists_before_apply"] = (self.run_dir() / "ledger.sqlite").is_file()
                report, code = ADOPT_REPORT, 0
            else:
                report, code = ADOPT_DRY_REPORT, 0
            noise = b"progress /private/x\n"
            return code, noise + json.dumps(report).encode()

        with mock.patch.object(run_stages, "run_child", child):
            code, parsed, raw = self.stage("adoption", "--config", "/cfg.json")
        self.assertContract(code, parsed, raw)
        self.assertTrue(seen["backup_exists_before_apply"], "backup precedes apply")
        self.assertGreater(parsed["new_data"]["backup_bytes"], 0)
        # Both copies are present, integrity-check ok, and hash to their manifest.
        manifest = self.read_manifest()
        self.assertEqual(manifest["run_id"], "run-1")
        self.assertEqual({entry["name"] for entry in manifest["files"]},
                         {"ledger.sqlite", "intake.sqlite"})
        for entry in manifest["files"]:
            target = self.run_dir() / entry["name"]
            self.assertEqual(target.stat().st_size, entry["bytes"])
            self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), entry["sha256"])
            with closing(sqlite3.connect("file:%s?mode=ro" % target, uri=True)) as db, db:
                self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_skip_when_nothing_pending(self):
        dry = {**ADOPT_DRY_REPORT, "pending_messages": 0, "eligible_new_messages": 0}
        apply = {**ADOPT_REPORT, "adopted_messages": 0, "already_adopted": 39}
        child, _ = self.adopt_child(dry, apply)
        with mock.patch.object(run_stages, "run_child", child):
            code, parsed, raw = self.stage("adoption", "--config", "/cfg.json")
        self.assertContract(code, parsed, raw)
        self.assertIn({"code": "ADOPT_BACKUP_SKIPPED", "severity": "info", "count": 1}, parsed["needs_g"])
        self.assertFalse(self.backups_dir().exists(), "no backup written when nothing pending")

    def test_skip_when_adoption_disabled(self):
        dry = {**ADOPT_DRY_REPORT, "gate": {"open": False, "state": "disabled_by_config"}}
        refused = {"status": "refused", "code": "adoption_gate_closed:disabled_by_config"}
        child, _ = self.adopt_child(dry, refused, apply_code=2)
        with mock.patch.object(run_stages, "run_child", child):
            code, parsed, raw = self.stage("adoption", "--config", "/cfg.json")
        self.assertContract(code, parsed, raw)
        self.assertEqual(parsed["error_code"], "ADOPTION_REFUSED")
        self.assertIn({"code": "ADOPT_BACKUP_SKIPPED", "severity": "info", "count": 1}, parsed["needs_g"])
        self.assertFalse(self.backups_dir().exists())

    def test_backup_failure_blocks_apply(self):
        synthetic_ledger(self.ledger, mail_messages=3)
        self.synthetic_intake()
        before = self.ledger.read_bytes()
        child, calls = self.adopt_child(ADOPT_DRY_REPORT, ADOPT_REPORT)
        with mock.patch.object(run_stages, "backup_adoption_dbs",
                               side_effect=run_stages.BackupFailure("integrity_check_failed")), \
                mock.patch.object(run_stages, "run_child", child):
            code, parsed, raw = self.stage("adoption", "--config", "/cfg.json")
        self.assertContract(code, parsed, raw)
        self.assertEqual((parsed["status"], parsed["error_code"]), ("fail", "ADOPT_BACKUP_FAILED"))
        self.assertEqual(len(calls), 1, "only the dry-run ran; apply never attempted")
        self.assertNotIn("--apply", calls[0])
        self.assertEqual(self.ledger.read_bytes(), before, "ledger unchanged on backup failure")

    def test_permissions(self):
        synthetic_ledger(self.ledger, mail_messages=1)
        self.synthetic_intake()
        run_stages.backup_adoption_dbs(self.processing, "run-1")
        self.assertEqual(stat.S_IMODE(self.backups_dir().stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.run_dir().stat().st_mode), 0o700)
        for name in ("ledger.sqlite", "intake.sqlite", run_stages.BACKUP_MANIFEST):
            self.assertEqual(stat.S_IMODE((self.run_dir() / name).stat().st_mode), 0o600)

    def test_rotation_keeps_newest_nine(self):
        synthetic_ledger(self.ledger, mail_messages=1)
        self.synthetic_intake()
        for index in range(11):
            run_stages.backup_adoption_dbs(self.processing, "run-%d" % index)
            # Pin a deterministic, increasing mtime so rotation order is exact.
            os.utime(self.run_dir("run-%d" % index), ns=(index, index))
        remaining = sorted((entry.name for entry in self.backups_dir().iterdir()),
                           key=lambda name: int(name.split("-")[1]))
        self.assertEqual(remaining, ["run-2", "run-3", "run-4", "run-5", "run-6",
                                     "run-7", "run-8", "run-9", "run-10"])

    def test_rotation_never_removes_outside_backups(self):
        synthetic_ledger(self.ledger, mail_messages=1)
        self.synthetic_intake()
        sentinel = self.processing / "sentinel.sqlite"
        sentinel.write_bytes(b"not-a-backup")
        for index in range(11):
            run_stages.backup_adoption_dbs(self.processing, "run-%d" % index)
            os.utime(self.run_dir("run-%d" % index), ns=(index, index))
        self.assertTrue(sentinel.exists(), "rotation only touches backups/")

    def test_rerun_idempotent(self):
        synthetic_ledger(self.ledger, mail_messages=2)
        self.synthetic_intake()
        first = run_stages.backup_adoption_dbs(self.processing, "run-1")
        first_manifest = self.read_manifest()
        second = run_stages.backup_adoption_dbs(self.processing, "run-1")
        self.assertEqual(first, second)
        self.assertEqual(self.read_manifest()["run_id"], "run-1")
        self.assertEqual([entry["sha256"] for entry in self.read_manifest()["files"]],
                         [entry["sha256"] for entry in first_manifest["files"]])
        self.assertEqual([entry.name for entry in self.backups_dir().iterdir()], ["run-1"])

    def test_missing_source_fails_closed(self):
        synthetic_ledger(self.ledger, mail_messages=1)
        # No intake.sqlite: the backup must fail rather than silently omit it.
        with self.assertRaises(run_stages.BackupFailure):
            run_stages.backup_adoption_dbs(self.processing, "run-1")
        self.assertEqual(list(self.backups_dir().iterdir()), [], "failed backup leaves no directory")

    # --- review round 1 regressions (fail-closed on indeterminate dry-run,
    #     corrupt source, path-traversal run-id, and writer-lock) ------------

    def test_dry_run_none_still_takes_backup(self):
        synthetic_ledger(self.ledger, mail_messages=2)
        self.synthetic_intake()
        child, calls = self.adopt_child(None, ADOPT_REPORT)
        with mock.patch.object(run_stages, "run_child", child):
            code, parsed, raw = self.stage("adoption", "--config", "/cfg.json")
        self.assertContract(code, parsed, raw)
        self.assertTrue(self.run_dir().is_dir(), "indeterminate dry-run must still back up")
        self.assertNotIn({"code": "ADOPT_BACKUP_SKIPPED", "severity": "info", "count": 1}, parsed["needs_g"])
        self.assertTrue(any("--apply" in c for c in calls), "apply still runs after the backup")
        self.assertIn("backup_bytes", parsed["new_data"])

    def test_dry_run_refused_still_takes_backup(self):
        synthetic_ledger(self.ledger, mail_messages=2)
        self.synthetic_intake()
        refused = {"source": "x", "status": "refused", "code": "ledger_wal_not_checkpointed"}
        child, calls = self.adopt_child(refused, ADOPT_REPORT, dry_code=2)
        with mock.patch.object(run_stages, "run_child", child):
            code, parsed, raw = self.stage("adoption", "--config", "/cfg.json")
        self.assertContract(code, parsed, raw)
        self.assertTrue(self.run_dir().is_dir(), "refused dry-run must still back up")
        self.assertIn("backup_bytes", parsed["new_data"])

    def test_unfinalized_runs_still_takes_backup(self):
        synthetic_ledger(self.ledger, mail_messages=2)
        self.synthetic_intake()
        dry = {**ADOPT_DRY_REPORT, "pending_messages": 0, "eligible_new_messages": 0,
               "unfinalized_runs": 1}
        apply = {**ADOPT_REPORT, "adopted_messages": 0, "recovered_runs": {"interrupted": 1}}
        child, calls = self.adopt_child(dry, apply)
        with mock.patch.object(run_stages, "run_child", child):
            code, parsed, raw = self.stage("adoption", "--config", "/cfg.json")
        self.assertContract(code, parsed, raw)
        self.assertTrue(self.run_dir().is_dir(), "unfinalized runs write to the ledger, so back up")
        self.assertIn("backup_bytes", parsed["new_data"])

    def test_incomplete_messages_still_takes_backup(self):
        synthetic_ledger(self.ledger, mail_messages=2)
        self.synthetic_intake()
        dry = {**ADOPT_DRY_REPORT, "pending_messages": 0, "eligible_new_messages": 0,
               "incomplete_messages": 1}
        apply = {**ADOPT_REPORT, "adopted_messages": 0, "resumed_messages": 1}
        child, calls = self.adopt_child(dry, apply)
        with mock.patch.object(run_stages, "run_child", child):
            code, parsed, raw = self.stage("adoption", "--config", "/cfg.json")
        self.assertContract(code, parsed, raw)
        self.assertTrue(self.run_dir().is_dir(), "incomplete messages resume and write, so back up")
        self.assertIn("backup_bytes", parsed["new_data"])

    def test_corrupt_source_maps_to_backup_failed(self):
        synthetic_ledger(self.ledger, mail_messages=2)
        intake = self.processing / "intake" / "intake.sqlite"
        intake.parent.mkdir(parents=True, exist_ok=True)
        intake.write_bytes(b"not a sqlite database at all" * 200)
        intake.chmod(0o600)
        before = self.ledger.read_bytes()
        child, calls = self.adopt_child(ADOPT_DRY_REPORT, ADOPT_REPORT)
        with mock.patch.object(run_stages, "run_child", child):
            code, parsed, raw = self.stage("adoption", "--config", "/cfg.json")
        self.assertContract(code, parsed, raw)
        self.assertEqual((parsed["status"], parsed["error_code"]), ("fail", "ADOPT_BACKUP_FAILED"))
        self.assertEqual(len(calls), 1, "only the dry-run ran; apply never attempted")
        self.assertNotIn("--apply", calls[0])
        self.assertEqual(self.ledger.read_bytes(), before, "ledger unchanged on corrupt source")
        self.assertFalse(self.run_dir().exists(), "failed backup leaves no run directory")

    def test_invalid_run_id_rejected_ledger_untouched(self):
        synthetic_ledger(self.ledger, mail_messages=2)
        self.synthetic_intake()
        sentinel = self.processing / "sentinel.sqlite"
        sentinel.write_bytes(b"keep-me")
        for run_id in ("..", ".", "", ".hidden", "a/b"):
            with self.subTest(run_id=run_id):
                with self.assertRaises(run_stages.BackupFailure):
                    run_stages.backup_adoption_dbs(self.processing, run_id)
                self.assertTrue(self.ledger.exists(), "ledger must survive a bad run-id")
                self.assertTrue(sentinel.exists(), "sentinel must survive a bad run-id")

    def test_stage_run_id_dotdot_fails_closed(self):
        synthetic_ledger(self.ledger, mail_messages=2)
        self.synthetic_intake()
        sentinel = self.processing / "sentinel.sqlite"
        sentinel.write_bytes(b"keep-me")
        child, calls = self.adopt_child(ADOPT_DRY_REPORT, ADOPT_REPORT)
        with mock.patch.object(run_stages, "run_child", child):
            code, parsed, raw = run_main(["adoption", "--project", str(self.project),
                                          "--timeout", "60", "--run-id", "..",
                                          "--config", "/cfg.json"])
        self.assertContract(code, parsed, raw)
        self.assertEqual((parsed["status"], parsed["error_code"]), ("fail", "ADOPT_BACKUP_FAILED"))
        self.assertEqual(len(calls), 1, "apply never attempted for a bad run-id")
        self.assertTrue(self.ledger.exists(), "ledger untouched")
        self.assertTrue(sentinel.exists(), "sentinel untouched")

    def test_backup_requires_root_lock(self):
        synthetic_ledger(self.ledger, mail_messages=2)
        self.synthetic_intake()
        with records_run.root_lock(self.processing):
            with self.assertRaises(run_stages.BackupFailure) as ctx:
                run_stages._backup_under_lock(self.processing, "run-1")
            self.assertEqual(ctx.exception.args, ("root_locked",))
        run_stages._backup_under_lock(self.processing, "run-1")
        self.assertTrue(self.run_dir("run-1").is_dir())

    def test_rotate_backups_refuses_symlinked_dir(self):
        # A symlinked backups/ must never be followed by rotation: the target
        # directory and its files are left exactly as they were.
        target = self.project / "elsewhere"
        target.mkdir()
        victim = target / "victim_00"
        victim.mkdir()
        keep = victim / "precious.txt"
        keep.write_bytes(b"keep-me")
        os.symlink(target, self.backups_dir())
        run_stages._rotate_backups(self.backups_dir())
        self.assertTrue(keep.exists(), "rotation must refuse a symlinked backups dir")
        self.assertEqual(keep.read_bytes(), b"keep-me")

    def test_symlinked_backups_dir_fails_closed(self):
        synthetic_ledger(self.ledger, mail_messages=2)
        self.synthetic_intake()
        # A sibling directory holding more than BACKUP_KEEP run dirs, each with a
        # precious file that neither backup nor rotation may touch through the link.
        target = self.project / "elsewhere"
        target.mkdir()
        precious = []
        for index in range(12):
            victim = target / ("victim_%02d" % index)
            victim.mkdir()
            keep = victim / "precious.txt"
            keep.write_bytes(b"keep-me")
            precious.append(keep)
        os.symlink(target, self.backups_dir())
        before = sorted(p.read_bytes() for p in precious)
        child, calls = self.adopt_child(ADOPT_DRY_REPORT, ADOPT_REPORT)
        with mock.patch.object(run_stages, "run_child", child):
            code, parsed, raw = self.stage("adoption", "--config", "/cfg.json")
        self.assertContract(code, parsed, raw)
        self.assertEqual((parsed["status"], parsed["error_code"]), ("fail", "ADOPT_BACKUP_FAILED"))
        self.assertEqual(len(calls), 1, "apply never attempted for a symlinked backups dir")
        self.assertNotIn("--apply", calls[0])
        self.assertEqual(sorted(p.read_bytes() for p in precious), before,
                         "symlink target must be untouched")
        self.assertTrue(self.ledger.exists(), "ledger untouched")


def synthetic_approval(*_args, **_kwargs):
    return parse_approval(json.dumps(policy_data()), uid=0, mode=stat.S_IFREG | 0o644,
                          now=NOW - timedelta(days=3650 * 4))


class PortalStageTests(StageTestCase):
    def test_allowlist_argument_required(self):
        code, parsed, raw = self.stage("portal")
        self.assertContract(code, parsed, raw)
        self.assertEqual(parsed["error_code"], "PORTAL_ALLOWLIST_MISSING")

    def test_unloadable_allowlist_fails_closed(self):
        self.add_receipt("a", eml(f'<a href="https://{HOST}/documents/42/download?token=t">d</a>'))
        for reason, expected in (("approval_invalid", "PORTAL_ALLOWLIST_INVALID"),
                                 ("approval_expired", "PORTAL_ALLOWLIST_EXPIRED"),
                                 ("approval_missing_or_unsafe", "PORTAL_ALLOWLIST_UNSAFE")):
            def refuse(*_a, reason=reason, **_k):
                raise PortalError(reason)
            with self.subTest(reason), mock.patch("campaign_tool.records.portal.policy.load_approval", refuse):
                code, parsed, raw = self.stage("portal", "--approval-file", "/nonexistent/approval.json")
                self.assertContract(code, parsed, raw)
                self.assertEqual(parsed["error_code"], expected)
                self.assertEqual(parsed["needs_g"], [{"code": expected, "severity": "blocking", "count": 1}])
                self.assertEqual(parsed["counts"]["output"], 0)
                self.assertEqual(list(self.portal.iterdir()), [], "no queue created, nothing enrolled")

    def test_real_missing_allowlist_file_fails_closed(self):
        code, parsed, raw = self.stage("portal", "--approval-file", str(Path(self.temp.name) / "absent.json"))
        self.assertContract(code, parsed, raw)
        self.assertEqual(parsed["error_code"], "PORTAL_ALLOWLIST_UNSAFE")
        self.assertEqual(list(self.portal.iterdir()), [])

    def test_intake_enrolls_and_dry_runs(self):
        self.add_receipt("a", eml(f'<a href="https://{HOST}/documents/42/download?token=t">d</a>'))
        self.add_receipt("b", eml('<a href="https://unlisted.example.test/documents/7">d</a>'))
        self.add_receipt("c", eml("<p>no portal link</p>"))
        self.add_receipt("d", eml(f'<a href="https://{HOST}/documents/43">d</a>'), sha="0" * 64)
        with mock.patch("campaign_tool.records.portal.policy.load_approval", synthetic_approval):
            code, parsed, raw = self.stage("portal", "--approval-file", "/approval.json")
        self.assertContract(code, parsed, raw)
        self.assertEqual((parsed["status"], parsed["error_code"]), ("warn", "PORTAL_HOST_REFUSED"))
        self.assertEqual(parsed["counts"], {"input": 2, "output": 1, "skipped": 1, "failed": 1})
        codes = {item["code"]: item["count"] for item in parsed["needs_g"]}
        self.assertEqual(codes, {"PORTAL_HOST_NOT_ALLOWLISTED": 1, "PORTAL_NOTICE_UNREADABLE": 1,
                                 "PORTAL_FETCH_DRY_RUN_ONLY": 1})
        # Rerun: the same link is not enrolled twice.
        with mock.patch("campaign_tool.records.portal.policy.load_approval", synthetic_approval):
            code, parsed, raw = self.stage("portal", "--approval-file", "/approval.json")
        with closing(sqlite3.connect(f"file:{self.portal / 'portal.sqlite'}?mode=ro&immutable=1", uri=True)) as db, db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM portal_items").fetchone()[0], 1)

    def test_notice_path_escape_is_unresolved(self):
        outside = Path(self.temp.name) / "outside.eml"
        outside.write_bytes(eml("x"))
        receipt = {"original_eml": {"path": str(outside), "sha256": hashlib.sha256(outside.read_bytes()).hexdigest()}}
        (self.mail / "receipts" / "x.json").write_text(json.dumps(receipt), encoding="utf-8")
        self.assertEqual([e for _, e in run_stages.iter_notices(self.mail)], ["notice_source_unresolved"])


class CountStageTests(StageTestCase):
    def sidecars(self, path):
        return sorted(p.name for p in path.parent.iterdir() if p.name.startswith(path.name + "-"))

    # The extract stage is a writer (native_extract, under the root lock), not a
    # count-only reader. Real extraction on synthetic roots is covered in
    # tests/records/test_native_extract.py; here only the result mapping.
    def summary(self, attempted=0, unexpected=0, units=0, pages=0, **outcomes):
        counts = {code: 0 for code in native_extract.OUTCOMES}
        counts.update(outcomes)
        return {"documents": sum(counts.values()), "outcomes": counts, "missing_parsers": [],
                "attempted": attempted, "unexpected_failures": unexpected, "new_units": units, "new_pages": pages}

    def extract_with(self, summary=None, error=None):
        with mock.patch.object(native_extract, "run", return_value=summary, side_effect=error) as fake:
            code, parsed, raw = self.stage("extract", "--max-documents", "7", "--document-timeout", "30")
        self.assertContract(code, parsed, raw)
        if summary is not None or error is not None:
            kwargs = fake.call_args.kwargs
            self.assertEqual((kwargs["run_id"], kwargs["max_documents"], kwargs["document_timeout"]),
                             ("run-1", 7, 30))
        return parsed

    def test_extract_reports_outcomes_and_awaiting_ocr(self):
        parsed = self.extract_with(self.summary(attempted=3, units=5, pages=4, EXTRACTED=2, AWAITING_OCR=1,
                                                PRESERVE_PENDING=0))
        self.assertEqual((parsed["status"], parsed["error_code"]), ("ok", None))
        self.assertEqual(parsed["counts"], {"input": 3, "output": 2, "failed": 0, "skipped": 1})
        self.assertEqual(parsed["outcomes"], [{"code": "EXTRACTED", "count": 2}, {"code": "AWAITING_OCR", "count": 1}])
        self.assertEqual(parsed["needs_g"], [{"code": "AWAITING_OCR", "severity": "info", "count": 1}])
        self.assertEqual(parsed["new_data"], {"extracted_units": 5, "pages": 4})

    def test_extract_parser_missing_and_corrupt_warn(self):
        parsed = self.extract_with(self.summary(attempted=2, EXTRACTED=1, CORRUPT_DOCUMENT=1, PARSER_MISSING=4))
        self.assertEqual((parsed["status"], parsed["error_code"]), ("warn", "EXTRACT_NATIVE_PARSER_MISSING"))
        self.assertEqual(parsed["counts"], {"input": 6, "output": 1, "failed": 1, "skipped": 4})
        codes = {item["code"]: item["count"] for item in parsed["needs_g"]}
        self.assertEqual(codes, {"EXTRACT_NATIVE_PARSER_MISSING": 4, "EXTRACT_CORRUPT_DOCUMENT": 1})
        parsed = self.extract_with(self.summary(attempted=2, EXTRACTED=1, CORRUPT_DOCUMENT=1))
        self.assertEqual((parsed["status"], parsed["error_code"]), ("warn", "EXTRACT_DOCUMENT_FAILURES"))

    def test_extract_backlog_and_workbook_warn(self):
        parsed = self.extract_with(self.summary(attempted=1, EXTRACTED=1, DEFERRED_RUN_BUDGET=3))
        self.assertEqual((parsed["status"], parsed["error_code"]), ("warn", "EXTRACT_BACKLOG_DEFERRED"))
        parsed = self.extract_with(self.summary(attempted=1, WORKBOOK_POLICY_REQUIRED=1))
        self.assertEqual((parsed["status"], parsed["error_code"]), ("warn", "EXTRACT_WORKBOOK_POLICY_REQUIRED"))

    def test_extract_systemic_failure_fails(self):
        parsed = self.extract_with(self.summary(attempted=3, unexpected=3, EXTRACT_ERROR=2, EXTRACT_NOT_ACCEPTED=1))
        self.assertEqual((parsed["status"], parsed["error_code"]), ("fail", "EXTRACT_ALL_ATTEMPTS_FAILED"))
        # Two failed attempts are not yet systemic.
        parsed = self.extract_with(self.summary(attempted=2, unexpected=2, EXTRACT_ERROR=2))
        self.assertEqual((parsed["status"], parsed["error_code"]), ("warn", "EXTRACT_DOCUMENT_FAILURES"))

    def test_extract_nothing_pending_is_ok(self):
        parsed = self.extract_with(self.summary())
        self.assertEqual((parsed["status"], parsed["counts"]["input"]), ("ok", 0))
        self.assertNotIn("outcomes", parsed)

    def test_extract_root_locked_warns(self):
        parsed = self.extract_with(error=records_run.RunError("root_locked"))
        self.assertEqual((parsed["status"], parsed["error_code"]), ("warn", "EXTRACT_ROOT_LOCKED"))
        parsed = self.extract_with(error=records_run.RunError("other"))
        self.assertEqual((parsed["status"], parsed["error_code"]), ("fail", "EXTRACT_RUN_FAILED"))

    def test_extract_invalid_limits_refused(self):
        for flag, value in (("--max-documents", "0"), ("--max-documents", "5001"),
                            ("--document-timeout", "9"), ("--document-timeout", "1401"),
                            # the stage deadline (--timeout 60) minus the 15 s margin cannot honour it
                            ("--document-timeout", "46")):
            with self.subTest(flag=flag, value=value):
                code, parsed, raw = self.stage("extract", flag, value)
                self.assertEqual((code, parsed["error_code"]), (1, "STAGE_CONFIG_INVALID"))

    def test_extract_limit_matrix(self):
        """T6-timeout D1/D3: document budget vs stage deadline, OCR child limits only with their flags."""
        def invalid(*argv, timeout="1490"):
            return run_stages.config_invalid(run_stages.parser().parse_args(
                ["extract", "--project", "/p", "--timeout", timeout, *argv]))
        vision = ("--ocr", "--ocr-vision-config", "/v.json")
        accepted = ((), ("--document-timeout", "10"), ("--document-timeout", "1400"),
                    ("--document-timeout", "1200", "--ocr", "--ocr-page-timeout", "180"),
                    ("--ocr", "--ocr-page-timeout", "10"), ("--ocr", "--ocr-page-timeout", "600"),
                    (*vision, "--ocr-vision-timeout", "10"), (*vision, "--ocr-vision-timeout", "600"),
                    ("--document-timeout", "1200", *vision, "--ocr-page-timeout", "180", "--ocr-vision-timeout", "300"))
        refused = (("--document-timeout", "1401"), ("--document-timeout", "1476"),
                   ("--ocr-page-timeout", "180"), ("--ocr", "--ocr-page-timeout", "9"),
                   ("--ocr", "--ocr-page-timeout", "601"), ("--ocr", "--ocr-vision-timeout", "300"),
                   ("--ocr-vision-timeout", "300"), (*vision, "--ocr-vision-timeout", "9"),
                   (*vision, "--ocr-vision-timeout", "601"))
        for argv in accepted:
            with self.subTest(accepted=argv):
                self.assertFalse(invalid(*argv))
        for argv in refused:
            with self.subTest(refused=argv):
                self.assertTrue(invalid(*argv))
        # --document-timeout <= --timeout - DEADLINE_MARGIN (15 s); the unpinned default is clamped instead.
        self.assertFalse(invalid("--document-timeout", "1400", timeout="1415"))
        self.assertTrue(invalid("--document-timeout", "1400", timeout="1414"))
        self.assertFalse(invalid("--document-timeout", "45", timeout="60"))
        self.assertTrue(invalid("--document-timeout", "46", timeout="60"))
        self.assertFalse(invalid(timeout="60"))

    def test_extract_unpinned_document_timeout_is_the_default_budget(self):
        with mock.patch.object(native_extract, "run", return_value=self.summary()) as fake:
            code, parsed, raw = self.stage("extract")
        self.assertEqual((code, fake.call_args.kwargs["document_timeout"]), (0, native_extract.DEFAULT_DOCUMENT_TIMEOUT))

    def test_missing_ledger_is_unreadable_and_not_created(self):
        for stage, expected in (("extract", "EXTRACT_ROOT_NOT_INITIALIZED"),
                                ("digest_gates", "GATES_LEDGER_UNREADABLE"),
                                ("outputs", "OUTPUTS_LEDGER_UNREADABLE")):
            with self.subTest(stage):
                code, parsed, raw = self.stage(stage)
                self.assertContract(code, parsed, raw)
                self.assertEqual(parsed["error_code"], expected)
                self.assertEqual(list(self.processing.iterdir()), [], "no ledger or sidecar created")

    def test_busy_ledger_is_not_read(self):
        synthetic_ledger(self.ledger, extract={"pending": 1})
        wal = Path(str(self.ledger) + "-wal")
        wal.write_bytes(b"x" * 32)
        before = (self.ledger.read_bytes(), wal.read_bytes(), self.sidecars(self.ledger))
        # extract is a writer under the root lock (no snapshot read); see test_native_extract.
        for stage, expected in (("digest_gates", "GATES_LEDGER_BUSY"), ("outputs", "OUTPUTS_LEDGER_BUSY")):
            with self.subTest(stage):
                code, parsed, raw = self.stage(stage)
                self.assertContract(code, parsed, raw)
                self.assertEqual((parsed["status"], parsed["error_code"]), ("warn", expected))
                self.assertEqual(parsed["needs_g"], [{"code": "LEDGER_WAL_PENDING", "severity": "info", "count": 1}])
        self.assertEqual((self.ledger.read_bytes(), wal.read_bytes(), self.sidecars(self.ledger)), before)
        self.assertFalse((self.project / "run-outputs").exists())

    def test_rollback_journal_is_busy(self):
        synthetic_ledger(self.ledger)
        Path(str(self.ledger) + "-journal").write_bytes(b"")
        with self.assertRaises(run_stages.LedgerBusy), run_stages.snapshot_ro(self.ledger):
            pass

    def test_change_during_read_is_busy(self):
        synthetic_ledger(self.ledger)
        with self.assertRaises(run_stages.LedgerBusy):
            with run_stages.snapshot_ro(self.ledger) as db:
                db.execute("SELECT COUNT(*) FROM originals").fetchone()
                Path(str(self.ledger) + "-wal").write_bytes(b"late writer")

    def test_snapshot_is_query_only(self):
        synthetic_ledger(self.ledger)
        with run_stages.snapshot_ro(self.ledger) as db, self.assertRaises(sqlite3.Error):
            db.execute("INSERT INTO originals VALUES('x')")

    def test_digest_gates_review_required(self):
        synthetic_ledger(self.ledger, gated={"catalog": {"done": 2, "pending": 1}, "privacy": {"blocked": 1}},
                         proposals=2)
        code, parsed, raw = self.stage("digest_gates")
        self.assertContract(code, parsed, raw)
        self.assertEqual((parsed["status"], parsed["error_code"]), ("warn", "GATE_REVIEW_REQUIRED"))
        self.assertEqual(parsed["review_required"], 2)
        self.assertEqual(parsed["counts"], {"input": 4, "output": 2, "failed": 1, "skipped": 1})
        self.assertEqual(self.sidecars(self.ledger), [])

    def test_outputs_snapshot_counts_only(self):
        synthetic_ledger(self.ledger, extract={"pending": 1}, mail_messages=39)
        with closing(sqlite3.connect(self.portal / "portal.sqlite")) as pdb, pdb:
            pdb.execute("CREATE TABLE portal_items(id TEXT)")
            pdb.execute("INSERT INTO portal_items VALUES('a')")
        code, parsed, raw = self.stage("outputs")
        self.assertContract(code, parsed, raw)
        self.assertEqual(parsed["status"], "ok")
        target = self.project / "run-outputs" / "run-1.json"
        snapshot = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual((snapshot["mail_messages"], snapshot["originals"], snapshot["portal_items"],
                          snapshot["portal_refusals"]), (39, 3, 1, 0))
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(self.sidecars(self.ledger), [])
        self.assertEqual(self.sidecars(self.portal / "portal.sqlite"), [])

    def test_real_schema_ledger(self):
        # A ledger created by the engine itself: queries match the real schema.
        # (extract over a real engine root: tests/records/test_native_extract.py)
        store.initialize(self.ledger)
        code, parsed, raw = self.stage("digest_gates")
        self.assertContract(code, parsed, raw)
        self.assertEqual(parsed["status"], "ok")
        self.assertEqual(self.sidecars(self.ledger), [])


class DeadlineTests(unittest.TestCase):
    def test_child_group_killed_on_timeout(self):
        with tempfile.TemporaryDirectory() as temp:
            pidfile = Path(temp) / "grandchild.pid"
            argv = ["/bin/sh", "-c", f"sleep 30 & echo $! > '{pidfile}'; wait"]
            started = time.monotonic()
            with self.assertRaises(run_stages.StageTimeout):
                run_stages.run_child(argv, 1)
            self.assertLess(time.monotonic() - started, 10)
            grandchild = int(pidfile.read_text())
            for _ in range(50):
                try:
                    os.kill(grandchild, 0)
                except ProcessLookupError:
                    break
                with contextlib.suppress(ChildProcessError):
                    os.waitpid(grandchild, os.WNOHANG)
                time.sleep(0.1)
            else:
                self.fail("grandchild outlived the stage deadline")

    def test_child_descendants_killed_after_normal_exit(self):
        with tempfile.TemporaryDirectory() as temp:
            pidfile = Path(temp) / "orphan.pid"
            argv = ["/bin/sh", "-c", f"sleep 30 & echo $! > '{pidfile}'; echo '{{\"ok\": 1}}'"]
            code, out = run_stages.run_child(argv, 10)
            self.assertEqual((code, run_stages.last_json(out)), (0, {"ok": 1}))
            orphan = int(pidfile.read_text())
            for _ in range(50):
                try:
                    os.kill(orphan, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.1)
            else:
                self.fail("descendant outlived the stage")

    def test_container_deadline_alarm(self):
        def slow(args):
            time.sleep(5)
            return run_stages.result("ok", {}), 0
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(run_stages.HANDLERS, {"sync": slow}):
            started = time.monotonic()
            code, parsed, raw = run_main(["sync", "--project", temp, "--timeout", "1"])
        self.assertLess(time.monotonic() - started, 4)
        self.assertEqual((code, parsed["error_code"]), (1, "STAGE_CONTAINER_TIMEOUT"))
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL)[0], 0.0, "alarm cleared")

    def test_internal_error_prints_no_exception_text(self):
        def boom(args):
            raise RuntimeError("/private/records/secret-name.pdf")
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(run_stages.HANDLERS, {"sync": boom}):
            code, parsed, raw = run_main(["sync", "--project", temp])
        self.assertEqual((code, parsed["error_code"]), (1, "STAGE_INTERNAL_ERROR"))
        self.assertNotIn("secret", raw)

    def test_invalid_timeout_refused(self):
        for value in ("0", "2701"):
            with self.subTest(value), tempfile.TemporaryDirectory() as temp:
                code, parsed, raw = run_main(["sync", "--project", temp, "--timeout", value])
                self.assertEqual((code, parsed["error_code"]), (1, "STAGE_CONFIG_INVALID"))

    def test_last_json(self):
        self.assertEqual(run_stages.last_json(b'{"a": 1}'), {"a": 1})
        self.assertEqual(run_stages.last_json(b'noise\n{"a": 2}\n'), {"a": 2})
        self.assertIsNone(run_stages.last_json(b"\xff\xfe"))
        self.assertIsNone(run_stages.last_json(b"[1, 2]"))
        self.assertIsNone(run_stages.last_json(b"no json"))


class SandboxGuardTests(StageTestCase):
    """AUD-DF1: --require-sandbox refuses a project without a safe .t12-sandbox-v1 marker."""

    def guarded(self, *extra):
        calls = []

        def handler(args):
            calls.append(args.project)
            return run_stages.result("ok", {"input": 1, "output": 1}), 0
        with mock.patch.dict(run_stages.HANDLERS, {"digest_gates": handler}):
            code, parsed, raw = self.stage("digest_gates", *extra)
        self.assertContract(code, parsed, raw)
        return code, parsed, calls

    def marker(self, mode=0o600):
        path = self.project / run_stages.SANDBOX_MARKER
        path.write_bytes(b'{"sandbox_marker_version":1}\n')
        path.chmod(mode)

    def assertRefused(self, code_name):
        before = sorted(p.name for p in self.project.iterdir())
        code, parsed, calls = self.guarded("--require-sandbox")
        self.assertEqual((code, parsed["status"], parsed["error_code"]), (1, "fail", code_name))
        self.assertEqual(calls, [], "no handler runs after a refusal")
        self.assertEqual(sorted(p.name for p in self.project.iterdir()), before)

    def test_missing_marker_refused(self):
        self.assertRefused("SANDBOX_MARKER_MISSING")

    def test_unsafe_markers_refused(self):
        outside = Path(self.temp.name) / "outside-marker"
        outside.write_bytes(b"x")
        outside.chmod(0o600)
        (self.project / run_stages.SANDBOX_MARKER).symlink_to(outside)
        self.assertRefused("SANDBOX_MARKER_UNSAFE")
        (self.project / run_stages.SANDBOX_MARKER).unlink()
        for mode in (0o644, 0o640, 0o602):
            with self.subTest(mode=oct(mode)):
                self.marker(mode)
                self.assertRefused("SANDBOX_MARKER_UNSAFE")
        (self.project / run_stages.SANDBOX_MARKER).unlink()
        (self.project / run_stages.SANDBOX_MARKER).mkdir(mode=0o700)
        self.assertRefused("SANDBOX_MARKER_UNSAFE")

    def test_marker_of_another_owner_refused(self):
        self.marker()
        with mock.patch.object(run_stages.os, "geteuid", return_value=os.geteuid() + 1):
            self.assertRefused("SANDBOX_MARKER_UNSAFE")

    def test_owner_only_marker_passes_guard(self):
        self.marker()
        code, parsed, calls = self.guarded("--require-sandbox")
        self.assertEqual((code, parsed["status"], parsed["error_code"]), (0, "ok", None))
        self.assertEqual(calls, [str(self.project)])

    def test_without_flag_no_marker_needed(self):
        code, parsed, calls = self.guarded()
        self.assertEqual((code, parsed["status"]), (0, "ok"))
        self.assertEqual(len(calls), 1)


class TemplateTests(unittest.TestCase):
    def _load_generator(self):
        path = REPO / "deploy/records-runtime/targets/make_test_container.py"
        spec = importlib.util.spec_from_file_location("make_test_container", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return path.parent, module

    def _check_template(self, module, directory, filename, mode):
        committed = (directory / filename).read_text(encoding="utf-8")
        self.assertEqual(committed, module.render_text(mode))
        plan = json.loads(committed)
        self.assertEqual(plan["mode"], mode)
        self.assertEqual([s["name"] for s in plan["stages"]], list(run_stages.STAGES))
        for entry in plan["stages"]:
            argv = entry["argv"]
            self.assertIn("@CONTAINER_PYTHON@", argv)
            self.assertLess(int(argv[argv.index("--timeout") + 1]), entry["timeout_seconds"])
        return plan

    def test_committed_plan_matches_generator(self):
        directory, module = self._load_generator()
        self._check_template(module, directory, "test-container.json.in", "test")
        self._check_template(module, directory, "live-container.json.in", "live")

    def test_test_and_live_plans_differ_only_in_mode(self):
        # AUD-DF1: TEST has its own project/adoption/approval placeholders; nothing else differs.
        directory, module = self._load_generator()
        test_plan = self._check_template(module, directory, "test-container.json.in", "test")
        live_plan = self._check_template(module, directory, "live-container.json.in", "live")
        live_names = {"@CONTAINER_PROJECT_DIR@", "@ADOPTION_CONFIG@", "@CONTAINER_APPROVAL_FILE@"}
        for entry in test_plan["stages"]:
            argv = entry["argv"]
            self.assertFalse(live_names & set(argv), entry["name"])
            self.assertEqual(argv[argv.index("--project") + 1], "@CONTAINER_PROJECT_DIR_TEST@")
        by_name = {entry["name"]: entry["argv"] for entry in test_plan["stages"]}
        self.assertIn("@ADOPTION_CONFIG_TEST@", by_name["adoption"])
        self.assertIn("@CONTAINER_APPROVAL_FILE_TEST@", by_name["portal"])
        mapped = json.loads(json.dumps(test_plan["stages"]).replace("_TEST@", "@"))
        self.assertNotEqual(test_plan["stages"], live_plan["stages"])
        self.assertEqual(mapped, live_plan["stages"])

    def test_committed_ocr_plans_match_generator_and_differ_only_in_extract(self):
        # T6-plan: Phase 2 LIVE plans. Only the extract stage differs from the Phase 1 LIVE plan.
        directory, module = self._load_generator()
        self.assertEqual(module.TEMPLATES, {
            "test-container.json.in": ("test", None), "live-container.json.in": ("live", None),
            "live-ocr-container.json.in": ("live", "ocr"),
            "live-ocr-vision-container.json.in": ("live", "ocr-vision")})
        live_plan = self._check_template(module, directory, "live-container.json.in", "live")
        # T6-timeout B: per-document budget 1200 s, page children 180 s, vision children 300 s.
        limits = ["--max-documents", "20", "--document-timeout", "1200", "--ocr-page-timeout", "180"]
        tail = {"ocr": [*limits, "--ocr"],
                "ocr-vision": [*limits, "--ocr-vision-timeout", "300", "--ocr",
                               "--ocr-vision-config", "@CONTAINER_OCR_VISION_CONFIG@"]}
        for filename, variant in (("live-ocr-container.json.in", "ocr"),
                                  ("live-ocr-vision-container.json.in", "ocr-vision")):
            with self.subTest(filename):
                committed = (directory / filename).read_text(encoding="utf-8")
                self.assertEqual(committed, module.render_text("live", variant))
                plan = json.loads(committed)
                self.assertEqual(plan["mode"], "live")
                self.assertEqual([s["name"] for s in plan["stages"]], list(run_stages.STAGES))
                for ocr_stage, phase1 in zip(plan["stages"], live_plan["stages"]):
                    if ocr_stage["name"] != "extract":
                        self.assertEqual(ocr_stage, phase1)
                        continue
                    argv = ocr_stage["argv"]
                    self.assertEqual(argv[:argv.index("--max-documents")],
                                     phase1["argv"][:phase1["argv"].index("--timeout")]
                                     + ["--timeout", "1490", "--run-id", "{run_id}"])
                    self.assertEqual(argv[argv.index("--max-documents"):], tail[variant])
                    self.assertEqual((ocr_stage["timeout_seconds"], ocr_stage["on_fail"]), (1500, "stop"))
                    # The adapter accepts these limits (its own bounds) and sees the OCR flags.
                    args = run_stages.parser().parse_args(argv[argv.index(run_stages.__name__) + 1:])
                    self.assertFalse(run_stages.config_invalid(args))
                    self.assertTrue(1 <= args.timeout <= 2700 and 1 <= args.max_documents <= 5000
                                    and 10 <= args.document_timeout <= 1400)
                    self.assertTrue(args.ocr)
                    self.assertEqual((args.document_timeout, args.ocr_page_timeout, args.ocr_vision_timeout),
                                     (1200, 180, 300 if variant == "ocr-vision" else None))
                    # The pinned budget fits the stage deadline and admits one uncached page.
                    self.assertLessEqual(args.document_timeout,
                                         args.timeout - native_extract.DEADLINE_MARGIN)
                    self.assertGreaterEqual(args.document_timeout, 2 * args.ocr_page_timeout
                                            + (args.ocr_vision_timeout or 0))
                    self.assertEqual(args.ocr_vision_config,
                                     "@CONTAINER_OCR_VISION_CONFIG@" if variant == "ocr-vision" else None)
        for bad in (("test", "ocr"), ("live", "tesseract")):
            with self.subTest(bad), self.assertRaises(ValueError):
                module.plan(*bad)

    def test_live_mail_plan_refreshes_only_live_sync(self):
        directory, module = self._load_generator()
        value = json.loads((directory / module.LIVE_MAIL_TEMPLATE).read_text())
        self.assertEqual(value, module.live_mail_plan())
        original = module.plan("live")
        sync = value["stages"][0]
        self.assertEqual(sync["argv"][-2:],
                         ["--sync-script", "@CONTAINER_SYNC_SCRIPT@"])
        args = run_stages.parser().parse_args(
            sync["argv"][sync["argv"].index(run_stages.__name__) + 1:])
        self.assertEqual(args.sync_script, "@CONTAINER_SYNC_SCRIPT@")
        self.assertEqual(args.timeout, 110)
        self.assertEqual((sync["timeout_seconds"], sync["on_fail"]), (120, "stop"))
        sync["argv"] = sync["argv"][:-2]
        self.assertEqual(value, original)
        self.assertNotIn("--sync-script", module.plan("test")["stages"][0]["argv"])

    def test_render_accepts_container_python(self):
        sys.path.insert(0, str(REPO / "deploy/records-runtime"))
        try:
            spec = importlib.util.spec_from_file_location("records_render",
                                                          REPO / "deploy/records-runtime/render.py")
            render = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(render)
        finally:
            sys.path.pop(0)
        self.assertIn("CONTAINER_PYTHON", render.ABSOLUTE)


if __name__ == "__main__":
    unittest.main()
