"""DFGL-12: records backup-check through the real CLI on sets made by records backup (DFGL-12A)."""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from campaign_tool.records import approval_queue, cli, e2e_proof, public_packets
from campaign_tool.records.intake import imap_intake
from tests.records.test_records_backup_profile import tree_state
from tests.records.test_run_slice import synthetic_email

ROOT = Path(__file__).resolve().parents[2]
PACKET = {"kind": "document", "agency_key": "ex-pd", "request_id": "r1", "source": "doc-1", "title": "Policy released",
          "text": "Example PD released its ALPR policy.",
          "documents": [{"sha256": "a" * 64, "date_received": "2026-11-03", "redaction_ids": ["red-1"]}]}


class BackupCheckCli(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="records-backup-check-")
        cls.addClassCleanup(cls.tmp.cleanup)
        base = Path(cls.tmp.name)
        content = base / "campaign" / "content"
        content.mkdir(parents=True)
        (content / "agencies.json").write_text(json.dumps(
            [{"agency_id": "ca-agency", "name": "Agency", "profile": {"status": "Records requested", "summary": "s"}}]))
        message = base / "m.eml"
        message.write_bytes(synthetic_email())
        assert e2e_proof.replay(message, base / "out", base / "campaign")["result"] == "PASS"
        cls.project = base / "out" / "replay" / "project"
        with sqlite3.connect(cls.project / "processing-v1" / "ledger.sqlite") as con:  # what a polled mailbox adds
            con.executescript(imap_intake.CHECKPOINTS_SQL)
            con.execute("INSERT INTO mail_checkpoints VALUES('local','inbox',1,7,NULL,'2026-11-01T00:00:00+00:00')")
        con.close()
        approval_queue.add_draft(str(cls.project), item_id="1" * 64, kind="reply-extension", agency_id="ca-agency",
                                 request_id="req-1", body="Please extend.", evidence={"findings": [], "deadlines": []},
                                 source_run_id="run_20260101T060000Z_0a1b2c3d")
        public_packets.init(str(cls.project))
        public_packets.propose(str(cls.project), PACKET)
        evidence = base / "quiescence.json"
        evidence.write_text(json.dumps({
            "schema": "records-quiescence-v1", "basis": "independent_all_writer_audit", "roots": [str(cls.project)],
            "observed_utc": datetime.now(timezone.utc).isoformat(), "all_writers_stopped": True,
            "checks": {"units": {}, "open_files": {}, "run_lock": "free", "arrival_pending": False},
            "auditor": "records quiescence-audit"}))
        cls.sets = base / "sets"
        done = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.records", "backup", "--project", str(cls.project),
                               "--into", str(cls.sets), "--quiescence", str(evidence)], cwd=ROOT, capture_output=True, text=True)
        assert done.returncode == 0, done.stdout + done.stderr
        cls.master = base / "master"
        shutil.copytree(cls.sets, cls.master)

    def setUp(self):
        self.into = Path(self.tmp.name) / ("into-%s" % self.id().rsplit(".", 1)[1])
        self.alerts = self.into.with_name(self.into.name + "-alerts")
        shutil.copytree(self.master, self.into)
        # the digest list names the copied set, as the operator's copy of the list would
        self.set = sorted(self.into.glob("set-*"))[0]
        entry = json.loads((self.into / "backup-digests.jsonl").read_text().splitlines()[-1])
        entry["set"] = str(self.set)
        self.entry = entry
        self.write_digests(entry)
        self.project_before = tree_state(self.project)

    def write_digests(self, *entries):
        (self.into / "backup-digests.jsonl").write_text("".join(json.dumps(e) + "\n" for e in entries))

    def check(self, *extra):
        done = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.records", "backup-check", "--into", str(self.into),
                               *extra], cwd=ROOT, capture_output=True, text=True, timeout=300)
        self.assertEqual(done.stderr, "")
        self.assertEqual(tree_state(self.project), self.project_before, "project changed")
        return done.returncode, json.loads(done.stdout)["code"]

    def alert_names(self):
        return sorted(p.name for p in self.alerts.glob("*.json")) if self.alerts.exists() else []

    def test_registered(self):
        self.assertEqual(cli.COMMANDS["backup-check"], ("records_backup", "check_main"))

    def test_fresh_set_is_ok_and_nothing_is_written(self):
        before = tree_state(self.into)
        self.assertEqual(self.check(), (0, "BACKUP_OK"))
        self.assertEqual(tree_state(self.into), before)
        self.assertFalse(self.alerts.exists())

    def test_flipped_byte_fails_and_alerts(self):
        victim = next(p for p in sorted((self.set / "payload").rglob("*")) if p.is_file() and p.stat().st_size)
        data = bytearray(victim.read_bytes())
        data[0] ^= 1
        victim.chmod(0o600)
        victim.write_bytes(bytes(data))
        before = tree_state(self.into)
        self.assertEqual(self.check(), (1, "BACKUP_VERIFY_FAILED"))
        self.assertEqual(tree_state(self.into), before)
        self.assertEqual(self.alert_names(), ["health-backup-backup_verify_failed.json"])

    def test_old_digest_is_stale(self):
        self.entry["at"] = (datetime.now(timezone.utc) - timedelta(days=9)).isoformat()
        self.write_digests(self.entry)
        before = tree_state(self.into)
        self.assertEqual(self.check(), (1, "BACKUP_STALE"))
        self.assertEqual(tree_state(self.into), before)
        self.assertEqual(self.alert_names(), ["health-backup-backup_stale.json"])
        self.assertEqual(self.check("--max-age-days", "30"), (0, "BACKUP_OK"))  # a good run clears the alert
        self.assertEqual(self.alert_names(), ["health-backup-backup_stale.cleared.json"])

    def test_empty_dir_is_none(self):
        shutil.rmtree(self.into)
        self.into.mkdir()
        self.assertEqual(self.check(), (1, "BACKUP_NONE"))
        self.assertEqual(list(self.into.iterdir()), [])
        self.assertEqual(self.alert_names(), ["health-backup-backup_none.json"])

    def test_unreadable_digest_list_is_invalid(self):
        (self.into / "backup-digests.jsonl").write_text("not json\n")
        self.assertEqual(self.check(), (1, "DIGESTS_INVALID"))

    def test_digest_of_other_manifest_fails_verify(self):
        self.entry["manifest_sha256"] = "0" * 64
        self.write_digests(self.entry)
        self.assertEqual(self.check(), (1, "BACKUP_VERIFY_FAILED"))


if __name__ == "__main__":
    unittest.main()
