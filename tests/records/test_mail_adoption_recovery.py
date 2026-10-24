"""Synthetic regressions for adoption identity validation and recovery.

Covers the two blocking findings of the T4 review at 55080e6:

* an enrolled (account, folder, uidvalidity, uid) identity whose receipt now
  declares a different EML, attachment set or receipt evidence must be refused
  as an ``identity_*`` conflict, never counted as adopted, and never rewritten;
* a failure or crash after ``mail_messages`` enrollment but before preserve
  completes must surface as ``incomplete_messages`` (durable, every rerun) and be
  resumed by a later apply, including from a fresh process.

Plus dry-run non-mutation for a non-empty WAL with a missing ``-shm`` and with
a live writer connection. Everything is synthetic; no private inputs.
"""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from campaign_tool.records import mail_adoption
from campaign_tool.records.intake import eml_export
from campaign_tool.records.run import Pipeline
from campaign_tool.records.runner.canonical_mail import CanonicalMailBackend
from tests.child_fixtures import run_fixture
from tests.records.test_mail_adoption_regression import _blob, _write_private, synthetic_receipt

REPO = Path(__file__).resolve().parents[2]


def tree(base):
    """Every path under ``base`` with mode, size, mtime and content hash."""
    result = {}
    for parent, dirs, files in os.walk(base):
        for name in dirs + files:
            path = Path(parent) / name
            info = path.lstat()
            digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
            result[str(path.relative_to(base))] = (info.st_mode, info.st_size, info.st_mtime_ns, digest)
    return result


def ledger_rows(ledger):
    """Canonical mail rows + occurrence evidence, read without sidecar side effects."""
    con = sqlite3.connect(Path(ledger).as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        return (con.execute("SELECT * FROM mail_messages ORDER BY id").fetchall(),
                con.execute("SELECT id,original_sha256,kind,source_ref,evidence FROM occurrences "
                            "ORDER BY id").fetchall(),
                con.execute("SELECT original_sha256,status FROM stage_state WHERE stage='preserve' "
                            "ORDER BY original_sha256").fetchall())
    finally:
        con.close()


def run_python(fixture, *args):
    env = dict(os.environ, PYTHONPATH=str(REPO), PYTHONDONTWRITEBYTECODE="1")
    return run_fixture(fixture, *args, cwd=REPO, env=env,
                       capture_output=True, text=True, timeout=120)


CLI = "mail_adoption_cli"

CRASH_AFTER_ENROLLMENT = "mail_adoption_crash_after_enrollment"

CRASH_AFTER_CLAIM = "mail_adoption_crash_after_claim"

WAL_WRITER_CRASH = "mail_adoption_wal_writer_crash"


class AdoptionRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = Pipeline(self.base / "processing").root.path
        self.ledger = self.root / "ledger.sqlite"
        self.export = self.base / "mail-history"
        self.export.mkdir(mode=0o700)
        self.path11, self.receipt11 = synthetic_receipt(self.export, uid=11, attachments=1)
        synthetic_receipt(self.export, uid=12, attachments=2)
        self.cfg = self.base / "config.json"
        _write_private(self.cfg, json.dumps({"schema": mail_adoption.CONFIG_SCHEMA, "source": "mail-history",
                                             "adoption": "enabled", "disable_marker_sha256": None}).encode())

    def adopt(self, **kw):
        return mail_adoption.adopt(self.root, self.export, config_path=self.cfg, **kw)

    def cli(self, *extra):
        return run_python(CLI, "--root", str(self.root), "--mail-history", str(self.export),
                          "--config", str(self.cfg), *extra)

    def replace_receipt(self, receipt):
        os.unlink(self.path11)
        eml_export._ensure_private(self.path11, (json.dumps(receipt, sort_keys=True) + "\n").encode())

    def assert_refused_conflict(self, code):
        before_rows, before_tree = ledger_rows(self.ledger), tree(self.root)
        dry = self.adopt()
        self.assertEqual(tree(self.root), before_tree)
        self.assertEqual(dry["identity_conflicts"], {code: 1})
        self.assertEqual((dry["already_adopted"], dry["pending_messages"]), (1, 0))
        self.assertTrue(mail_adoption.blocking(dry))
        applied = self.adopt(apply=True)
        self.assertEqual(applied["identity_conflicts"], {code: 1})
        self.assertEqual(applied["after"]["identity_conflicts"], {code: 1})
        self.assertEqual((applied["adopted_messages"], applied["resumed_messages"]), (0, 0))
        self.assertNotIn("run_id", applied)
        self.assertTrue(mail_adoption.blocking(applied))
        # Earlier canonical evidence is never rewritten.
        self.assertEqual(ledger_rows(self.ledger), before_rows)
        result = self.cli("--apply")
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(ledger_rows(self.ledger), before_rows)

    # -- finding 1: identity-only classification ------------------------------
    def test_changed_eml_and_attachments_under_same_identity_is_refused(self):
        self.adopt(apply=True)
        os.unlink(self.path11)
        synthetic_receipt(self.export, uid=11, attachments=2, body="Changed synthetic body.\n")
        self.assert_refused_conflict("identity_eml_changed")

    def test_changed_eml_same_attachment_count_is_refused(self):
        self.adopt(apply=True)
        os.unlink(self.path11)
        synthetic_receipt(self.export, uid=11, attachments=1, body="Changed synthetic body.\n")
        self.assert_refused_conflict("identity_eml_changed")

    def test_removed_attachment_declaration_is_refused(self):
        self.adopt(apply=True)
        self.replace_receipt(dict(self.receipt11, attachments=[]))
        self.assert_refused_conflict("identity_attachment_set_changed")

    def test_added_attachment_declaration_is_refused(self):
        self.adopt(apply=True)
        extra = _blob(self.export, b"%PDF-1.4\nSynthetic extra attachment\n%%EOF\n")
        extra.update(part=99, filename="extra.pdf", original_filename="extra.pdf", content_type="application/pdf")
        self.replace_receipt(dict(self.receipt11, attachments=self.receipt11["attachments"] + [extra]))
        self.assert_refused_conflict("identity_attachment_set_changed")

    def test_changed_receipt_evidence_same_bytes_is_refused(self):
        self.adopt(apply=True)
        self.replace_receipt(dict(self.receipt11, internaldate="2026-02-03T04:05:06Z"))
        self.assert_refused_conflict("identity_receipt_changed")

    def test_unchanged_receipt_rewritten_identically_stays_adopted(self):
        self.adopt(apply=True)
        self.replace_receipt(self.receipt11)
        report = self.adopt()
        self.assertEqual((report["already_adopted"], report["identity_conflicts"]), (2, {}))
        self.assertFalse(mail_adoption.blocking(report))

    # -- finding 2: enrollment before preservation ----------------------------
    def test_post_enrollment_exception_is_durably_incomplete_then_resumed(self):
        failing = lambda *a: CanonicalMailBackend(*a, installed_preservation=False)
        first = self.adopt(apply=True, backend_factory=failing)
        self.assertEqual(first["failures"], {"wp1_installed_preservation_adapter_missing": 2})
        self.assertEqual(first["after"]["incomplete_messages"], 2)
        self.assertEqual(first["after"]["pending_messages"], 2)
        self.assertTrue(mail_adoption.blocking(first))
        # Durable: a separate dry run sees it and does not report success.
        dry = self.adopt()
        self.assertEqual((dry["incomplete_messages"], dry["already_adopted"], dry["new_messages"]), (2, 0, 0))
        self.assertTrue(mail_adoption.blocking(dry))
        # A failing rerun must keep reporting it rather than going quiet.
        again = self.adopt(apply=True, backend_factory=failing)
        self.assertEqual(again["after"]["incomplete_messages"], 2)
        self.assertTrue(mail_adoption.blocking(again))
        resumed = self.adopt(apply=True)
        self.assertEqual((resumed["resumed_messages"], resumed["adopted_messages"], resumed["failures"]), (2, 0, {}))
        self.assertEqual(resumed["after"], {"already_adopted": 2, "new_messages": 0, "incomplete_messages": 0,
                                            "pending_messages": 0, "coded_gap_messages": 0, "coded_gaps": {},
                                            "unaccepted_coded_gaps": {}, "identity_conflicts": {},
                                            "unfinalized_runs": 0})
        self.assertFalse(mail_adoption.blocking(resumed))
        self.assertTrue(all(status == "done" for _, status in ledger_rows(self.ledger)[2]))
        before = tree(self.root)
        final = self.adopt(apply=True)
        self.assertEqual((final["pending_messages"], final["adopted_messages"], final["resumed_messages"]), (0, 0, 0))
        self.assertNotIn("run_id", final)
        self.assertEqual(tree(self.root), before)

    def test_crash_after_enrollment_resumes_in_fresh_process(self):
        crashed = run_python(CRASH_AFTER_ENROLLMENT, str(self.root), str(self.export), str(self.cfg))
        self.assertEqual(crashed.returncode, 17, crashed.stderr)
        rows = ledger_rows(self.ledger)
        self.assertEqual(len(rows[0]), 1)                       # enrolled ...
        self.assertTrue(all(s == "pending" for _, s in rows[2]))  # ... not preserved
        dry = self.cli()
        self.assertEqual(dry.returncode, 3, dry.stderr)
        report = json.loads(dry.stdout)
        self.assertEqual((report["incomplete_messages"], report["new_messages"], report["unfinalized_runs"]), (1, 1, 1))
        applied = self.cli("--apply")
        self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
        report = json.loads(applied.stdout)
        self.assertEqual(report["recovered_runs"], {"finalized": 1})
        self.assertEqual((report["resumed_messages"], report["adopted_messages"]), (1, 1))
        self.assertEqual(report["after"]["pending_messages"], 0)
        self.assertEqual(report["after"]["unfinalized_runs"], 0)
        con = sqlite3.connect(self.ledger.as_uri() + "?mode=ro&immutable=1", uri=True)
        statuses = sorted(r[0] for r in con.execute("SELECT status FROM runs WHERE run_id LIKE 'mail-history-%'"))
        con.close()
        self.assertEqual(statuses, ["completed", "interrupted"])
        before = tree(self.root)
        rerun = self.cli("--apply")
        self.assertEqual(rerun.returncode, 0, rerun.stderr)
        self.assertEqual(json.loads(rerun.stdout)["pending_messages"], 0)
        self.assertEqual(tree(self.root), before)

    def test_crash_after_stage_claim_is_durably_incomplete_until_lease_expiry(self):
        crashed = run_python(CRASH_AFTER_CLAIM, str(self.root), str(self.export), str(self.cfg))
        self.assertEqual(crashed.returncode, 18, crashed.stderr)
        dry = self.cli()
        self.assertEqual(dry.returncode, 3, dry.stderr)
        self.assertEqual(json.loads(dry.stdout)["incomplete_messages"], 1)
        # The dead process's preserve lease is still live: the fresh-process resume
        # is refused by the stage engine and reported, never clean.
        applied = self.cli("--apply")
        self.assertEqual(applied.returncode, 3, applied.stderr)
        report = json.loads(applied.stdout)
        self.assertEqual(report["recovered_runs"], {"finalized": 1})
        self.assertEqual(sum(report["failures"].values()), 1)
        self.assertEqual((report["after"]["incomplete_messages"], report["after"]["pending_messages"]), (1, 1))
        self.assertIn("in_progress", {s for _, s in ledger_rows(self.ledger)[2]})
        # Synthetic time travel only: expire the lease the dead process held.
        con = sqlite3.connect(self.ledger)
        con.execute("UPDATE work_leases SET expires_at='2000-01-01T00:00:00+00:00'")
        con.commit()
        con.close()
        resumed = self.cli("--apply")
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        report = json.loads(resumed.stdout)
        self.assertEqual((report["resumed_messages"], report["failures"]), (1, {}))
        self.assertEqual((report["after"]["pending_messages"], report["after"]["incomplete_messages"]), (0, 0))
        self.assertTrue(all(s == "done" for _, s in ledger_rows(self.ledger)[2]))

    # -- dry-run non-mutation under WAL states --------------------------------
    def test_nonempty_wal_with_missing_shm_dry_run_fails_closed_without_mutation(self):
        self.adopt(apply=True)
        crashed = run_python(WAL_WRITER_CRASH, str(self.ledger))
        self.assertEqual(crashed.returncode, 0, crashed.stderr)
        self.assertGreater(Path(str(self.ledger) + "-wal").stat().st_size, 0)
        Path(str(self.ledger) + "-shm").unlink()
        before = tree(self.base)
        with self.assertRaisesRegex(mail_adoption.AdoptionError, "ledger_wal_not_checkpointed"):
            self.adopt()
        result = self.cli()
        self.assertEqual(result.returncode, 2)
        self.assertIn("ledger_wal_not_checkpointed", result.stdout)
        self.assertEqual(tree(self.base), before)
        self.assertFalse(Path(str(self.ledger) + "-shm").exists())
        # Writer-side recovery happens only under the root lock in apply.
        applied = self.adopt(apply=True)
        self.assertTrue(applied["ledger_wal_recovered"])
        self.assertEqual((applied["already_adopted"], applied["pending_messages"]), (2, 0))
        self.assertFalse(mail_adoption.blocking(applied))

    def test_active_writer_dry_run_fails_closed_or_reads_without_mutation(self):
        self.adopt(apply=True)
        writer = sqlite3.connect(self.ledger)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA wal_autocheckpoint=0")
        # Open write transaction, no frames yet: committed snapshot readable, nothing touched.
        writer.execute("BEGIN IMMEDIATE")
        before = tree(self.base)
        report = self.adopt()
        self.assertEqual(report["already_adopted"], 2)
        self.assertEqual(tree(self.base), before)
        # Committed but un-checkpointed frames from a live writer: fail closed.
        writer.execute("PRAGMA user_version=%d" % writer.execute("PRAGMA user_version").fetchone()[0])
        writer.commit()
        before = tree(self.base)
        with self.assertRaisesRegex(mail_adoption.AdoptionError, "ledger_wal_not_checkpointed"):
            self.adopt()
        self.assertEqual(tree(self.base), before)
        # Apply cannot settle a WAL held by a writer outside the lock: refuses.
        with self.assertRaisesRegex(mail_adoption.AdoptionError, "ledger_active_writer"):
            self.adopt(apply=True)


if __name__ == "__main__":
    unittest.main()
