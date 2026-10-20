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
            with sqlite3.connect("file:%s?mode=ro" % target, uri=True) as db:
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
        with sqlite3.connect(f"file:{self.portal / 'portal.sqlite'}?mode=ro&immutable=1", uri=True) as db:
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
                            ("--document-timeout", "9"), ("--document-timeout", "601")):
            with self.subTest(flag=flag, value=value):
                code, parsed, raw = self.stage("extract", flag, value)
                self.assertEqual((code, parsed["error_code"]), (1, "STAGE_CONFIG_INVALID"))

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
        with sqlite3.connect(self.portal / "portal.sqlite") as pdb:
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


class TemplateTests(unittest.TestCase):
    def test_committed_plan_matches_generator(self):
        path = REPO / "deploy/records-runtime/targets/make_test_container.py"
        spec = importlib.util.spec_from_file_location("make_test_container", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        committed = (path.parent / "test-container.json.in").read_text(encoding="utf-8")
        self.assertEqual(committed, module.render_text())
        plan = json.loads(committed)
        self.assertEqual([s["name"] for s in plan["stages"]], list(run_stages.STAGES))
        for entry in plan["stages"]:
            argv = entry["argv"]
            self.assertIn("@CONTAINER_PYTHON@", argv)
            self.assertLess(int(argv[argv.index("--timeout") + 1]), entry["timeout_seconds"])

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
