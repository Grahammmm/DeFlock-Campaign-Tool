"""Synthetic regressions for coded-gap reporting in records adopt-mail.

G decision 2026-10-19 (T4 option c): adopt everything adoptable; a receipt the
canonical backend cannot adopt under its current contract (its EML already
declared as an unresolved legacy original, or an unsupported message/rfc822
part) is a *coded gap*: classified read-only, reported by stable code in every
dry-run, apply and run summary, counted and never dropped, and never passed to
the backend. Only the accepted classes are non-blocking; any other coded gap
still exits 3.

Everything is synthetic; no private inputs, mailbox or network.
"""
from email.message import EmailMessage
from email.policy import SMTP
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from campaign_tool.records import mail_adoption
from campaign_tool.records.intake import eml_export, mail_delta
from campaign_tool.records.ledger import store
from campaign_tool.records.run import Pipeline
from tests.records.test_mail_adoption import write_private
from tests.records.test_mail_adoption_recovery import CLI, run_python, tree
from tests.records.test_mail_adoption_regression import _blob, synthetic_receipt


def rfc822_receipt(mail_root, *, uid, validity=900):
    """Legacy-shape receipt whose EML forwards a message as a message/rfc822 part."""
    inner = EmailMessage()
    inner["From"] = "clerk@agency.example.invalid"
    inner["To"] = "records@agency.example.invalid"
    inner["Subject"] = "Synthetic forwarded message %d" % uid
    inner.set_content("Forwarded synthetic body.\n")
    outer = EmailMessage()
    outer["From"] = "records@adopt.example.invalid"
    outer["To"] = "organizer@campaign.example.invalid"
    outer["Subject"] = "Synthetic forward %d" % uid
    outer["Message-ID"] = "<adopt-forward-%d@adopt.example.invalid>" % uid
    outer.set_content("See the forwarded message.\n")
    outer.add_attachment(inner)
    for index, part in enumerate(outer.walk()):
        if part.is_multipart():
            part.set_boundary("synthetic-forward-%d-boundary-%d" % (uid, index))
    raw = outer.as_bytes(policy=SMTP)
    receipt = {"complete": True, "account_id": "synthetic-adopt-account", "folder": "Adoption",
               "uidvalidity": validity, "uid": uid, "bytes": len(raw),
               "internaldate": "2026-01-02T03:04:05Z", "original_eml": _blob(mail_root, raw),
               "attachments": []}
    key = hashlib.sha256(json.dumps(
        [receipt[k] for k in ("account_id", "folder", "uidvalidity", "uid")]).encode()).hexdigest()
    path = mail_root / "receipts" / (key + ".json")
    eml_export._ensure_private(path, (json.dumps(receipt, sort_keys=True) + "\n").encode())
    return path, receipt


class CodedGapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = Pipeline(self.base / "processing").root.path
        self.ledger = self.root / "ledger.sqlite"
        self.export = self.base / "mail-history"
        self.export.mkdir(mode=0o700)
        self.cfg = self.base / "adoption.json"
        write_private(self.cfg, json.dumps({"schema": mail_adoption.CONFIG_SCHEMA, "source": "mail-history",
                                            "adoption": "enabled", "disable_marker_sha256": None}).encode())

    def adopt(self, **kw):
        return mail_adoption.adopt(self.root, self.export, **kw)

    def apply(self):
        return self.adopt(apply=True, config_path=self.cfg)

    def cli(self, *extra):
        done = run_python(CLI, "--root", str(self.root), "--mail-history", str(self.export), *extra)
        return done.returncode, json.loads(done.stdout)

    def declare_legacy(self, receipt, *, scope="unresolved", delta=0):
        """Declare the receipt's EML as an existing legacy original (as on the frozen copy)."""
        original = receipt["original_eml"]
        with store.ledger(self.ledger) as con:
            con.execute("INSERT INTO originals VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (original["sha256"], original["bytes"] + delta, None, "2026-01-01T00:00:00+00:00",
                         "unresolved", scope, None, "captured", "eml", "{}"))
            con.commit()

    def query(self, sql, *args):
        # immutable: a plain read-only open would touch the -shm sidecar.
        con = sqlite3.connect(self.ledger.as_uri() + "?mode=ro&immutable=1", uri=True)
        try:
            return con.execute(sql, args).fetchall()
        finally:
            con.close()

    def original_row(self, sha):
        return self.query("SELECT * FROM originals WHERE sha256=?", sha)

    def assert_not_passed_to_backend(self, path, receipt):
        """No CAS blob, verification receipt or mail row was created for a coded gap."""
        sha = receipt["original_eml"]["sha256"]
        self.assertFalse((self.root / "intake" / "blobs" / sha).exists())
        receipt_sha = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        self.assertFalse((self.root / "intake" / "verification-receipts" / receipt_sha).exists())
        self.assertEqual(self.query("SELECT count(*) FROM mail_messages WHERE uid=?", receipt["uid"]), [(0,)])
        self.assertEqual(self.query("SELECT count(*) FROM occurrences WHERE original_sha256=? AND "
                                    "acquisition_method='verified_mail_export'", sha), [(0,)])

    def assert_invariants(self, report):
        self.assertEqual(report["new_messages"], report["eligible_new_messages"] + report["coded_gap_messages"])
        self.assertEqual(report["coded_gap_messages"], sum(report["coded_gaps"].values()))
        self.assertEqual(report["receipts"], report["already_adopted"] + report["pending_messages"]
                         + report["coded_gap_messages"] + sum(report["identity_conflicts"].values()))

    def mail_history_runs(self):
        return self.query("SELECT status, summary FROM runs WHERE run_id LIKE 'mail-history-%' ORDER BY started_at")

    def test_acceptance_shape_adopts_all_adoptable_and_reports_accepted_gaps(self):
        # 3 adopted earlier, then 4 adoptable new + 2 unresolved legacy EMLs + 1 rfc822
        # receipt whose EML is also unresolved (MIME is checked first, like the backend).
        for uid in (1, 2, 3):
            synthetic_receipt(self.export, uid=uid, attachments=1)
        first = self.apply()
        self.assertEqual((first["adopted_messages"], first["coded_gap_messages"]), (3, 0))
        for uid in (4, 5, 6, 7):
            synthetic_receipt(self.export, uid=uid, attachments=uid % 2)
        gaps = [synthetic_receipt(self.export, uid=uid, attachments=1) for uid in (8, 9)]
        forward = rfc822_receipt(self.export, uid=10)
        for _, receipt in gaps + [forward]:
            self.declare_legacy(receipt)
        before_originals = {r["original_eml"]["sha256"]: self.original_row(r["original_eml"]["sha256"])
                            for _, r in gaps + [forward]}

        snapshot = tree(self.root)
        dry = self.adopt(config_path=self.cfg)
        self.assertEqual(tree(self.root), snapshot)
        self.assertEqual((dry["receipts"], dry["already_adopted"], dry["new_messages"]), (10, 3, 7))
        self.assertEqual((dry["eligible_new_messages"], dry["pending_messages"]), (4, 4))
        self.assertEqual(dry["coded_gaps"], {"original_scope_unresolved": 2, "unsupported_rfc822_part": 1})
        self.assertEqual((dry["coded_gap_messages"], dry["coded_gap_attachments"]), (3, 2))
        self.assertEqual(dry["unaccepted_coded_gaps"], {})
        self.assertNotIn("predicted_binding_conflicts", dry)
        self.assert_invariants(dry)
        self.assertFalse(mail_adoption.blocking(dry))

        report = self.apply()
        self.assertEqual((report["adopted_messages"], report["failures"]), (4, {}))
        # adopted + coded gaps == every new receipt at the start of the run.
        self.assertEqual(report["adopted_messages"] + report["coded_gap_messages"], report["new_messages"])
        self.assertEqual(report["coded_gaps"], dry["coded_gaps"])
        self.assertEqual(report["after"], {"already_adopted": 7, "new_messages": 3, "incomplete_messages": 0,
                                           "pending_messages": 0, "coded_gap_messages": 3,
                                           "coded_gaps": dry["coded_gaps"], "unaccepted_coded_gaps": {},
                                           "identity_conflicts": {}, "unfinalized_runs": 0})
        self.assertFalse(mail_adoption.blocking(report))
        self.assertEqual(self.query("SELECT count(*) FROM mail_messages"), [(7,)])
        for path, receipt in gaps + [forward]:
            self.assert_not_passed_to_backend(path, receipt)
        # Legacy declarations are left untouched for their own resolution contract (T4B).
        for sha, row in before_originals.items():
            self.assertEqual(self.original_row(sha), row)
        runs = self.mail_history_runs()
        self.assertEqual([status for status, _ in runs], ["completed", "completed_with_gaps"])
        summary = json.loads(runs[1][1])["runner_summary"]
        self.assertEqual((summary["adopted"], summary["failures"], summary["coded_gap_messages"]), (4, 0, 3))
        self.assertEqual(summary["coded_gaps"], dry["coded_gaps"])

        # Rerun: adopts 0, starts no run, writes nothing, still reports the gaps.
        snapshot = tree(self.root)
        again = self.apply()
        self.assertEqual((again["adopted_messages"], again["resumed_messages"], again["pending_messages"]), (0, 0, 0))
        self.assertNotIn("run_id", again)
        self.assertEqual((again["already_adopted"], again["coded_gaps"]), (7, dry["coded_gaps"]))
        self.assert_invariants(again)
        self.assertEqual(tree(self.root), snapshot)
        self.assertEqual(len(self.mail_history_runs()), 2)
        code, cli = self.cli("--config", str(self.cfg), "--apply")
        self.assertEqual((code, cli["adopted_messages"], cli["coded_gap_messages"]), (0, 0, 3))
        self.assertEqual(tree(self.root), snapshot)
        self.assertEqual(self.query("PRAGMA integrity_check"), [("ok",)])

    def test_rfc822_code_matches_the_backend_refusal(self):
        path, receipt = rfc822_receipt(self.export, uid=20)
        dry = self.adopt()
        self.assertEqual(dry["coded_gaps"], {"unsupported_rfc822_part": 1})
        # Parity: the backend's own legacy intake refuses the same receipt with the same code.
        scratch = Pipeline(self.base / "scratch").root.path / "intake"
        with self.assertRaisesRegex(mail_delta.Rejected, "^unsupported_rfc822_part$"):
            mail_delta.import_receipt(path, self.export, scratch)
        report = self.apply()
        self.assertEqual((report["adopted_messages"], report["failures"]), (0, {}))
        self.assertNotIn("run_id", report)  # nothing adoptable: no run, no write
        self.assert_not_passed_to_backend(path, receipt)
        self.assertFalse(mail_adoption.blocking(report))

    def test_unresolved_code_matches_the_backend_refusal(self):
        path, receipt = synthetic_receipt(self.export, uid=21)
        self.declare_legacy(receipt)
        self.assertEqual(self.adopt()["coded_gaps"], {"original_scope_unresolved": 1})
        # Without the classification the backend refuses it (after side effects):
        # the code is the read-only prediction of canonical_original_binding_conflict.
        from campaign_tool.records.runner.canonical_mail import CanonicalMailBackend
        from campaign_tool.records.runner.contracts import Folder
        scratch = Pipeline(self.base / "scratch-root").root.path
        scratch_ledger = scratch / "ledger.sqlite"
        original = receipt["original_eml"]
        with store.ledger(scratch_ledger) as con:
            con.execute("INSERT INTO originals VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (original["sha256"], original["bytes"], None, "2026-01-01T00:00:00+00:00",
                         "unresolved", "unresolved", None, "captured", "eml", "{}"))
            con.commit()
        backend = CanonicalMailBackend(self.export, scratch / "intake", scratch_ledger)
        backend.start_run(mail_adoption._identity(None))
        with self.assertRaisesRegex(ValueError, "^canonical_original_binding_conflict$"):
            backend.preserve(path, "synthetic-adopt-account", Folder("Adoption", 900), 21)

    def test_unaccepted_binding_gap_is_reported_and_blocks(self):
        synthetic_receipt(self.export, uid=30)
        path, receipt = synthetic_receipt(self.export, uid=31)
        self.declare_legacy(receipt, scope="in_scope", delta=1)
        dry = self.adopt()
        self.assertEqual(dry["coded_gaps"], {"original_bytes_conflict": 1})
        self.assertEqual(dry["unaccepted_coded_gaps"], {"original_bytes_conflict": 1})
        self.assertTrue(mail_adoption.blocking(dry))
        code, cli = self.cli()
        self.assertEqual((code, cli["unaccepted_coded_gaps"]), (3, {"original_bytes_conflict": 1}))
        code, applied = self.cli("--config", str(self.cfg), "--apply")
        self.assertEqual((code, applied["adopted_messages"], applied["failures"]), (3, 1, {}))
        self.assertEqual(applied["after"]["unaccepted_coded_gaps"], {"original_bytes_conflict": 1})
        self.assert_not_passed_to_backend(path, receipt)
        self.assertEqual(self.mail_history_runs()[0][0], "completed_with_gaps")

    def test_changed_export_eml_is_a_blocking_gap_not_a_crash(self):
        synthetic_receipt(self.export, uid=40)
        path, receipt = synthetic_receipt(self.export, uid=41)
        eml = self.export / receipt["original_eml"]["path"]
        data = bytearray(eml.read_bytes())
        data[-2] ^= 0x01  # same length, different bytes
        os.chmod(eml, 0o600)
        eml.write_bytes(bytes(data))
        snapshot = tree(self.root)
        dry = self.adopt()
        self.assertEqual(tree(self.root), snapshot)
        self.assertEqual(dry["unaccepted_coded_gaps"], {"source_hash_mismatch": 1})
        self.assertTrue(mail_adoption.blocking(dry))
        report = self.apply()
        self.assertEqual((report["adopted_messages"], report["failures"]), (1, {}))
        self.assertTrue(mail_adoption.blocking(report))
        self.assert_not_passed_to_backend(path, receipt)

    def test_coded_gap_receipt_becomes_adoptable_once_resolved(self):
        # Gaps are classified fresh each run: nothing is remembered or dropped.
        path, receipt = rfc822_receipt(self.export, uid=50)
        self.assertEqual(self.apply()["coded_gap_messages"], 1)
        os.unlink(path)
        synthetic_receipt(self.export, uid=50)
        report = self.apply()
        self.assertEqual((report["adopted_messages"], report["coded_gap_messages"]), (1, 0))


if __name__ == "__main__":
    unittest.main()
