"""DFGL-12A: records backup / backup-verify through the real CLI on a project produced by the real pipeline."""
import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from campaign_tool.records import approval_queue, cli, e2e_proof, public_packets
from campaign_tool.records.intake import imap_intake
from campaign_tool.records.recovery import backup_v2 as v2
from tests.records.test_run_slice import synthetic_email

ROOT = Path(__file__).resolve().parents[2]
PACKET = {"kind": "document", "agency_key": "ex-pd", "request_id": "r1", "source": "doc-1", "title": "Policy released",
          "text": "Example PD released its ALPR policy.",
          "documents": [{"sha256": "a" * 64, "date_received": "2026-11-03", "redaction_ids": ["red-1"]}]}


def tree_state(root):
    out = []
    for path in sorted(Path(root).rglob("*")):
        info = path.lstat()
        out.append((str(path), info.st_mode, info.st_size, info.st_mtime_ns,
                    hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""))
    return out


class RecordsBackupCli(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="records-backup-")
        cls.addClassCleanup(cls.tmp.cleanup)
        base = Path(cls.tmp.name)
        content = base / "campaign" / "content"
        content.mkdir(parents=True)
        (content / "agencies.json").write_text(json.dumps(
            [{"agency_id": "a-1", "name": "Agency", "profile": {"status": "Records requested", "summary": "s"}}]))
        message = base / "m.eml"
        message.write_bytes(synthetic_email())
        report = e2e_proof.replay(message, base / "out", base / "campaign")
        assert report["result"] == "PASS"
        cls.project = base / "out" / "replay" / "project"
        with sqlite3.connect(cls.project / "processing-v1" / "ledger.sqlite") as con:  # what a polled mailbox adds
            con.executescript(imap_intake.CHECKPOINTS_SQL)
            con.execute("INSERT INTO mail_checkpoints VALUES('local','inbox',1,7,NULL,'2026-11-01T00:00:00+00:00')")
        con.close()
        approval_queue.add_draft(str(cls.project), item_id="1" * 64, kind="reply-extension", agency_id="a-1",
                                 request_id="req-1", body="Please extend.", evidence={"findings": [], "deadlines": []},
                                 source_run_id="run_20260101T060000Z_0a1b2c3d")
        public_packets.init(str(cls.project))
        public_packets.propose(str(cls.project), PACKET)
        cls.counter = 0

    def setUp(self):
        self.before = tree_state(self.project)
        self.addCleanup(lambda: self.assertEqual(tree_state(self.project), self.before, "source tree changed"))
        type(self).counter += 1
        self.work = Path(self.tmp.name) / ("w%d" % self.counter)
        self.work.mkdir(mode=0o700)

    def run_cli(self, *args):
        done = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.records", *args], cwd=ROOT,
                              capture_output=True, text=True, timeout=300)
        self.assertEqual(done.stderr, "")
        line = json.loads(done.stdout)
        self.assertNotIn(str(self.project), done.stdout)  # codes and counts only
        return done.returncode, line

    def evidence(self, name="quiescence.json", **changes):
        doc = {"schema": "records-quiescence-v1", "basis": "independent_all_writer_audit", "roots": [str(self.project)],
               "observed_utc": datetime.now(timezone.utc).isoformat(), "all_writers_stopped": True,
               "checks": {"units": {}, "open_files": {}, "run_lock": "free", "arrival_pending": False},
               "auditor": "records quiescence-audit", **changes}
        path = self.work / name
        path.write_text(json.dumps(doc))
        return str(path)

    def backup(self, evidence=None, *extra):
        return self.run_cli("backup", "--project", str(self.project), "--into", str(self.work / "sets"),
                            "--quiescence", evidence or self.evidence(), *extra)

    def test_commands_registered(self):
        self.assertEqual(cli.COMMANDS["backup"], ("records_backup", "backup_main"))
        self.assertEqual(cli.COMMANDS["backup-verify"], ("records_backup", "verify_main"))

    def test_check_profile_classifies_every_file(self):
        code, line = self.run_cli("backup", "--check-profile", "--project", str(self.project))
        self.assertEqual((code, line["code"], line["unclassified"]), (0, "OK", 0))
        self.assertTrue(v2.REQUIRED_ROLES <= set(line["roles"]), line["roles"])
        self.assertEqual(sum(1 for p in self.project.rglob("*") if p.is_file()),
                         line["files"])
        self.assertTrue({"rebuildable", "process_state"} <= set(line["excluded"]))

    def test_unknown_file_makes_profile_incomplete(self):
        parent = self.project / "processing-v1"
        saved = parent.stat()
        extra = parent / "stray.bin"
        extra.write_bytes(b"x")

        def restore():
            extra.unlink()
            os.utime(parent, ns=(saved.st_atime_ns, saved.st_mtime_ns))
        self.addCleanup(restore)
        code, line = self.run_cli("backup", "--check-profile", "--project", str(self.project))
        self.assertEqual((code, line["code"], line["unclassified"]), (1, "PROFILE_INCOMPLETE", 1))
        code, line = self.backup()
        self.assertEqual((code, line["code"]), (1, "PROFILE_INCOMPLETE"))
        self.assertFalse((self.work / "sets").exists())

    def test_entry_limit_reported(self):
        from campaign_tool.records import records_backup
        out = io.StringIO()
        with mock.patch("campaign_tool.records.recovery.records_profile.MAX_ENTRIES", 3), redirect_stdout(out):
            self.assertEqual(records_backup.backup_main(["--check-profile", "--project", str(self.project)]), 1)
        self.assertEqual(json.loads(out.getvalue())["code"], "LIMIT_ENTRIES")

    def test_backup_writes_set_and_trusted_digest_verifies(self):
        evidence = self.evidence()
        code, line = self.backup(evidence)
        self.assertEqual((code, line["code"]), (0, "OK"))
        sets = self.work / "sets"
        (target,) = [p for p in sets.iterdir() if p.name.startswith("set-")]
        digests = sets / "backup-digests.jsonl"
        self.assertEqual(oct(digests.stat().st_mode & 0o777), "0o600")
        (entry,) = [json.loads(l) for l in digests.read_text().splitlines()]
        self.assertEqual(entry["set"], str(target))
        self.assertEqual(entry["evidence_sha256"], hashlib.sha256(Path(evidence).read_bytes()).hexdigest())
        self.assertEqual(v2.verify_v2(str(target), manifest_sha256=entry["manifest_sha256"])["files"], line["files"])
        code, line = self.run_cli("backup-verify", "--set", str(target), "--digests", str(digests))
        self.assertEqual((code, line["code"]), (0, "OK"))
        code, line = self.run_cli("backup-verify", "--set", str(target), "--digests", str(self.work / "missing.jsonl"))
        self.assertEqual((code, line["code"]), (1, "DIGESTS_UNREADABLE"))
        other = self.work / "other.jsonl"
        other.write_text("")
        self.assertEqual(self.run_cli("backup-verify", "--set", str(target), "--digests", str(other))[1]["code"],
                         "DIGEST_NOT_TRUSTED")

    def test_stale_wrong_or_tampered_evidence_is_refused(self):
        stale = (datetime.now(timezone.utc) - timedelta(seconds=300)).isoformat()
        good = self.evidence()
        trusted = hashlib.sha256(Path(good).read_bytes()).hexdigest()
        cases = [(self.evidence("a.json", observed_utc=stale), ()),
                 (self.evidence("b.json", roots=[str(self.project) + "/x"]), ()),
                 (self.evidence("c.json", all_writers_stopped=False), ()), (self.evidence("d.json", schema="other"), ()),
                 (self.evidence("e.json", auditor="someone"), ())]
        tampered = self.evidence("good.json", checks={"units": {}, "open_files": {}, "run_lock": "stale",
                                                      "arrival_pending": False})
        cases.append((tampered, ("--quiescence-sha256", trusted)))
        for evidence, extra in cases:
            code, line = self.backup(evidence, *extra)
            self.assertEqual((code, line["code"]), (1, "QUIESCENCE_REFUSED"), evidence)
        self.assertFalse((self.work / "sets").exists())

    def test_flipped_byte_fails_verification(self):
        self.assertEqual(self.backup()[0], 0)
        sets = self.work / "sets"
        (target,) = [p for p in sets.iterdir() if p.name.startswith("set-")]
        victim = max((target / "payload").iterdir(), key=lambda p: p.stat().st_size)
        victim.chmod(0o600)
        raw = bytearray(victim.read_bytes())
        raw[len(raw) // 2] ^= 1
        victim.write_bytes(bytes(raw))
        code, line = self.run_cli("backup-verify", "--set", str(target), "--digests", str(sets / "backup-digests.jsonl"))
        self.assertEqual((code, line["code"]), (1, "VERIFY_FAILED"))

    def test_destination_inside_project_is_refused(self):
        code, line = self.run_cli("backup", "--project", str(self.project), "--into", str(self.project / "sets"),
                                  "--quiescence", self.evidence())
        self.assertEqual((code, line["code"]), (1, "BACKUP_REFUSED"))


if __name__ == "__main__":
    unittest.main()
