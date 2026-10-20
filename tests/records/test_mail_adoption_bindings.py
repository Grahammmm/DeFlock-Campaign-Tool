"""Synthetic regressions: full canonical binding validation of enrolled identities.

Round-1 review at 834de0c showed that an enrolled identity whose canonical
occurrence id, MIME locator or verification receipt path had drifted was still
counted as ``already_adopted``. Every case here mutates exactly one canonical
binding after a successful adoption and requires dry-run *and* apply to refuse
the identity as an ``identity_*`` conflict, write nothing, and exit 3 from the
CLI. Exact-wire (schema'd, nested) and native MSG receipts must still validate
as complete. Everything is synthetic; no private inputs.
"""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import unittest

from campaign_tool.records import mail_adoption
from campaign_tool.records.intake import eml_export
from tests.records import native_msg_fixtures as native
from tests.records import test_mail_adoption_recovery as recovery
from tests.records.test_mail_adoption_recovery import WAL_WRITER_CRASH, ledger_rows, run_python, tree
from tests.records.test_mail_wire_intake import binary, chain, leaf, mixed


class _Base(recovery.AdoptionRecoveryTests):
    """Reuse the recovery fixture (uid 11 with 1 attachment, uid 12 with 2)."""

    def mutate(self, sql, args=()):
        con = sqlite3.connect(self.ledger)
        try:
            con.execute(sql, args)
            con.commit()
        finally:
            con.close()
        self.assertFalse(Path(str(self.ledger) + "-wal").exists())

    def attachment(self, uid=11):
        con = sqlite3.connect(self.ledger.as_uri() + "?mode=ro&immutable=1", uri=True)
        con.row_factory = sqlite3.Row
        try:
            for row in con.execute("SELECT * FROM occurrences WHERE kind='attachment' ORDER BY id"):
                if json.loads(row["source_ref"])["uid"] == uid:
                    return dict(row)
        finally:
            con.close()
        raise AssertionError("fixture attachment missing")

    def update_evidence(self, change):
        row = self.attachment()
        evidence = json.loads(row["evidence"])
        change(evidence)
        self.mutate("UPDATE occurrences SET evidence=? WHERE id=?", (json.dumps(evidence), row["id"]))


# The recovery suite's own tests are not re-run through this subclass.
for _name in [n for n in vars(recovery.AdoptionRecoveryTests) if n.startswith("test_")]:
    setattr(_Base, _name, None)


class CanonicalBindingDriftTests(_Base):
    def setUp(self):
        super().setUp()
        first = self.adopt(apply=True)
        self.assertEqual((first["adopted_messages"], first["failures"]), (2, {}))

    # -- occurrence identity, locator, parent, multiplicity --------------------
    def test_changed_attachment_occurrence_id_is_refused(self):
        self.mutate("UPDATE occurrences SET id=? WHERE id=?", ("f" * 64, self.attachment()["id"]))
        self.assert_refused_conflict("identity_occurrence_mismatch")

    def test_changed_mime_locator_is_refused(self):
        row = self.attachment()
        source = json.loads(row["source_ref"])
        source["mime_path"] = "999"
        self.mutate("UPDATE occurrences SET source_ref=? WHERE id=?", (json.dumps(source), row["id"]))
        self.assert_refused_conflict("identity_locator_changed")

    def test_changed_parent_occurrence_is_refused(self):
        self.mutate("UPDATE occurrences SET parent_occurrence_id=? WHERE id=?", ("e" * 64, self.attachment()["id"]))
        self.assert_refused_conflict("identity_parent_changed")

    def test_changed_acquisition_method_is_refused(self):
        self.mutate("UPDATE occurrences SET acquisition_method='captured_legacy_mail' WHERE id=?",
                    (self.attachment()["id"],))
        self.assert_refused_conflict("identity_occurrence_mismatch")

    def test_duplicate_attachment_occurrence_is_refused(self):
        row = self.attachment()
        row["id"] = "d" * 64
        self.mutate("INSERT INTO occurrences(%s) VALUES(%s)" % (",".join(row), ",".join("?" * len(row))),
                    tuple(row.values()))
        self.assert_refused_conflict("identity_attachment_set_changed")

    def test_duplicate_mail_occurrence_is_refused(self):
        con = sqlite3.connect(self.ledger.as_uri() + "?mode=ro&immutable=1", uri=True)
        con.row_factory = sqlite3.Row
        mid = mail_adoption.message_id(("synthetic-adopt-account", "Adoption", "900", "11"))
        row = dict(con.execute("SELECT * FROM occurrences WHERE id=?", (mid,)).fetchone())
        con.close()
        row["id"] = "c" * 64
        self.mutate("INSERT INTO occurrences(%s) VALUES(%s)" % (",".join(row), ",".join("?" * len(row))),
                    tuple(row.values()))
        self.assert_refused_conflict("identity_occurrence_mismatch")

    def test_missing_attachment_occurrence_is_refused(self):
        self.mutate("DELETE FROM occurrences WHERE id=?", (self.attachment()["id"],))
        self.assert_refused_conflict("identity_attachment_set_changed")

    def test_missing_mail_occurrence_is_refused(self):
        mid = mail_adoption.message_id(("synthetic-adopt-account", "Adoption", "900", "11"))
        self.mutate("DELETE FROM occurrences WHERE id=?", (mid,))
        self.assert_refused_conflict("identity_occurrence_mismatch")

    def test_occurrence_moved_to_other_identity_is_refused(self):
        row = self.attachment()
        source = json.loads(row["source_ref"])
        source["uid"] = 999
        self.mutate("UPDATE occurrences SET source_ref=? WHERE id=?", (json.dumps(source), row["id"]))
        self.assert_refused_conflict("identity_attachment_set_changed")

    def test_intake_catalog_locator_drift_is_refused(self):
        from campaign_tool.records.intake import folder
        intake = self.root / "intake" / "intake.sqlite"
        part = self.receipt11["attachments"][0]["part"]
        oid = folder.hid(folder.js(["mail-receipt-v1", "synthetic-adopt-account", "Adoption", "900", "11", str(part)]))
        con = sqlite3.connect(intake)
        try:
            value = json.loads(con.execute("SELECT locator FROM occurrences WHERE oid=?", (oid,)).fetchone()[0])
            value["mime"] = "42"
            con.execute("UPDATE occurrences SET locator=? WHERE oid=?", (json.dumps(value), oid))
            con.commit()
        finally:
            con.close()
        # The locator feeds the canonical occurrence id, so the ledger no longer
        # holds the expected id for that attachment.
        self.assert_refused_conflict("identity_occurrence_mismatch")

    # -- evidence and verification receipt ------------------------------------
    def test_changed_verification_receipt_path_is_refused(self):
        def change(evidence):
            evidence["verification_receipt_path"] = str(self.root / "intake" / "verification-receipts" / ("0" * 64))
        self.update_evidence(change)
        self.assert_refused_conflict("identity_verification_path_changed")

    def test_extra_evidence_field_is_refused(self):
        self.update_evidence(lambda evidence: evidence.update(note="synthetic"))
        self.assert_refused_conflict("identity_evidence_mismatch")

    def test_changed_evidence_bytes_is_refused(self):
        self.update_evidence(lambda evidence: evidence.update(bytes=evidence["bytes"] + 1))
        self.assert_refused_conflict("identity_evidence_mismatch")

    def test_changed_evidence_cas_is_refused(self):
        self.update_evidence(lambda evidence: evidence.update(cas_sha256="a" * 64))
        self.assert_refused_conflict("identity_evidence_mismatch")

    def _verification_file(self):
        evidence = json.loads(self.attachment()["evidence"])
        path = self.root / "intake" / "verification-receipts" / evidence["receipt_sha256"]
        self.assertEqual(str(path), evidence["verification_receipt_path"])
        return path

    def test_changed_stored_verification_receipt_bytes_is_refused(self):
        path = self._verification_file()
        os.chmod(path, 0o600)
        path.write_bytes(path.read_bytes() + b" ")
        os.chmod(path, 0o400)
        self.assert_refused_conflict("identity_verification_receipt_changed")

    def test_missing_stored_verification_receipt_is_refused(self):
        self._verification_file().unlink()
        self.assert_refused_conflict("identity_verification_receipt_missing")

    def test_symlinked_stored_verification_receipt_is_refused(self):
        path = self._verification_file()
        moved = self.base / "moved-receipt"
        shutil.move(path, moved)
        os.symlink(moved, path)
        self.assert_refused_conflict("identity_verification_receipt_missing")

    # -- mail_messages row ----------------------------------------------------
    def test_changed_mail_header_projection_is_refused(self):
        mid = mail_adoption.message_id(("synthetic-adopt-account", "Adoption", "900", "11"))
        self.mutate("UPDATE mail_messages SET received_at=? WHERE id=?", ("1999-01-01T00:00:00+00:00", mid))
        self.assert_refused_conflict("identity_mail_row_changed")

    # -- intake catalog WAL: dry-run must not touch it ------------------------
    def test_intake_wal_dry_run_fails_closed_without_mutation(self):
        intake = self.root / "intake" / "intake.sqlite"
        crashed = run_python("import sqlite3, sys\nsqlite3.connect(sys.argv[1]).execute('PRAGMA journal_mode=WAL').fetchone()\n"
                             + WAL_WRITER_CRASH, str(intake))
        self.assertEqual(crashed.returncode, 0, crashed.stderr)
        self.assertGreater(Path(str(intake) + "-wal").stat().st_size, 0)
        Path(str(intake) + "-shm").unlink()
        before = tree(self.base)
        with self.assertRaisesRegex(mail_adoption.AdoptionError, "intake_wal_not_checkpointed"):
            self.adopt()
        self.assertEqual(tree(self.base), before)
        applied = self.adopt(apply=True)
        self.assertEqual((applied["already_adopted"], applied["pending_messages"]), (2, 0))
        self.assertFalse(mail_adoption.blocking(applied))
        self.assertFalse(Path(str(intake) + "-wal").exists() and Path(str(intake) + "-wal").stat().st_size)

    def test_unchanged_bindings_stay_complete_and_byte_identical(self):
        before_rows, before_tree = ledger_rows(self.ledger), tree(self.base)
        for apply in (False, True):
            report = self.adopt(apply=apply)
            self.assertEqual((report["already_adopted"], report["identity_conflicts"]), (2, {}))
            self.assertFalse(mail_adoption.blocking(report))
        self.assertEqual(ledger_rows(self.ledger), before_rows)
        self.assertEqual(tree(self.base), before_tree)


class _BytesBase(_Base):
    def blob(self, sha):
        path = self.root / "intake" / "blobs" / sha
        self.assertTrue(path.is_file() and not path.is_symlink())
        return path

    def eml_sha(self, uid=11):
        mid = mail_adoption.message_id(("synthetic-adopt-account", "Adoption", "900", str(uid)))
        con = sqlite3.connect(self.ledger.as_uri() + "?mode=ro&immutable=1", uri=True)
        try:
            return con.execute("SELECT eml_sha256 FROM mail_messages WHERE id=?", (mid,)).fetchone()[0]
        finally:
            con.close()

    def rewrite(self, path, data):
        mode = os.stat(path).st_mode & 0o777
        os.chmod(path, 0o600)
        path.write_bytes(data)
        os.chmod(path, mode)

    def assert_bytes_untouched(self, path, data):
        # The damaged file is reported, never repaired, re-sealed or re-chmod-ed.
        self.assertEqual(path.read_bytes(), data)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o400)


class CanonicalOriginalBytesTests(_BytesBase):
    """Round-2 finding: completed identities must re-verify the stored CAS bytes.

    Each case damages exactly one canonical original under ``intake/blobs``
    after a successful adoption, leaving the ledger and every receipt
    untouched. Dry-run and apply must refuse the identity with a safe
    ``identity_original_blob_*`` code, never write, never repair the bytes, and
    the CLI must exit 3.
    """

    def setUp(self):
        super().setUp()
        first = self.adopt(apply=True)
        self.assertEqual((first["adopted_messages"], first["failures"]), (2, {}))

    def test_same_length_attachment_corruption_is_refused(self):
        path = self.blob(self.attachment()["original_sha256"])
        raw = path.read_bytes()
        middle = len(raw) // 2
        damaged = raw[:middle] + bytes([raw[middle] ^ 1]) + raw[middle + 1:]
        self.assertEqual(len(damaged), len(raw))
        self.rewrite(path, damaged)
        self.assert_refused_conflict("identity_original_blob_changed")
        self.assert_bytes_untouched(path, damaged)

    def test_first_byte_attachment_corruption_is_refused(self):
        # The reviewer's review_cas_probe.py case.
        path = self.blob(self.attachment()["original_sha256"])
        raw = path.read_bytes()
        damaged = bytes([raw[0] ^ 1]) + raw[1:]
        self.rewrite(path, damaged)
        self.assert_refused_conflict("identity_original_blob_changed")
        self.assert_bytes_untouched(path, damaged)

    def test_truncated_attachment_is_refused(self):
        path = self.blob(self.attachment()["original_sha256"])
        damaged = path.read_bytes()[:-1]
        self.rewrite(path, damaged)
        self.assert_refused_conflict("identity_original_blob_changed")
        self.assert_bytes_untouched(path, damaged)

    def test_extended_attachment_is_refused(self):
        path = self.blob(self.attachment()["original_sha256"])
        damaged = path.read_bytes() + b"\n"
        self.rewrite(path, damaged)
        self.assert_refused_conflict("identity_original_blob_changed")
        self.assert_bytes_untouched(path, damaged)

    def test_missing_attachment_blob_is_refused(self):
        path = self.blob(self.attachment()["original_sha256"])
        path.unlink()
        self.assert_refused_conflict("identity_original_blob_missing")
        self.assertFalse(os.path.lexists(path))

    def test_symlinked_attachment_blob_is_refused(self):
        # The link target holds the correct bytes; following it would hide the swap.
        path = self.blob(self.attachment()["original_sha256"])
        moved = self.base / "moved-blob"
        shutil.move(path, moved)
        os.symlink(moved, path)
        self.assert_refused_conflict("identity_original_blob_unsafe")
        self.assertTrue(path.is_symlink())

    def test_attachment_blob_replaced_by_directory_is_refused(self):
        path = self.blob(self.attachment()["original_sha256"])
        path.unlink()
        path.mkdir(mode=0o700)
        self.assert_refused_conflict("identity_original_blob_unsafe")

    def test_eml_body_only_corruption_is_refused(self):
        path = self.blob(self.eml_sha())
        raw = path.read_bytes()
        marker = b"Synthetic adoption body for uid 11."
        at = raw.index(marker) + len(b"Synthetic ")
        self.assertGreater(at, raw.index(b"\r\n\r\n"))   # inside the body, headers untouched
        damaged = raw[:at] + (b"B" if raw[at:at + 1] != b"B" else b"C") + raw[at + 1:]
        self.assertEqual(len(damaged), len(raw))
        self.rewrite(path, damaged)
        self.assert_refused_conflict("identity_original_blob_changed")
        self.assert_bytes_untouched(path, damaged)

    def test_missing_eml_blob_is_refused(self):
        self.blob(self.eml_sha()).unlink()
        self.assert_refused_conflict("identity_original_blob_missing")

    def test_unchanged_bytes_stay_complete_without_mutation(self):
        before_rows, before_tree = ledger_rows(self.ledger), tree(self.base)
        for apply in (False, True):
            report = self.adopt(apply=apply)
            self.assertEqual((report["already_adopted"], report["identity_conflicts"],
                              report["pending_messages"]), (2, {}, 0))
            self.assertFalse(mail_adoption.blocking(report))
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.cli("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(ledger_rows(self.ledger), before_rows)
        self.assertEqual(tree(self.base), before_tree)


class IncompleteIdentityBytesTests(_BytesBase):
    """Enrollment committed, preservation unfinished, then the stored bytes changed."""

    def test_corrupt_blob_of_incomplete_identity_is_refused_not_resumed(self):
        from campaign_tool.records.runner.canonical_mail import CanonicalMailBackend
        failing = lambda *a: CanonicalMailBackend(*a, installed_preservation=False)
        first = self.adopt(apply=True, backend_factory=failing)
        self.assertEqual(first["after"]["incomplete_messages"], 2)
        damaged_sha = self.attachment()["original_sha256"]
        uid11_shas = {damaged_sha, self.eml_sha(11)}
        path = self.blob(damaged_sha)
        raw = path.read_bytes()
        damaged = raw[:-2] + bytes([raw[-2] ^ 1]) + raw[-1:]
        self.rewrite(path, damaged)
        before_rows, before_tree = ledger_rows(self.ledger), tree(self.root)
        dry = self.adopt()
        self.assertEqual(tree(self.root), before_tree)
        self.assertEqual(dry["identity_conflicts"], {"identity_original_blob_changed": 1})
        self.assertEqual((dry["incomplete_messages"], dry["already_adopted"]), (1, 0))
        self.assertTrue(mail_adoption.blocking(dry))
        # Apply resumes only the intact identity (uid 12). The damaged one is
        # never handed to the backend and keeps its rows and unfinished stages.
        applied = self.adopt(apply=True)
        self.assertEqual(applied["identity_conflicts"], {"identity_original_blob_changed": 1})
        self.assertEqual((applied["resumed_messages"], applied["failures"]), (1, {}))
        self.assertEqual(applied["after"]["identity_conflicts"], {"identity_original_blob_changed": 1})
        self.assertEqual((applied["after"]["already_adopted"], applied["after"]["incomplete_messages"]), (1, 0))
        self.assertTrue(mail_adoption.blocking(applied))
        after_rows = ledger_rows(self.ledger)
        self.assertEqual(after_rows[:2], before_rows[:2])
        stages = lambda rows: sorted(tuple(r) for r in rows[2] if r[0] in uid11_shas)
        self.assertEqual(stages(after_rows), stages(before_rows))
        self.assertNotIn((damaged_sha, "done"), stages(after_rows))
        self.assert_bytes_untouched(path, damaged)
        result = self.cli("--apply")
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(ledger_rows(self.ledger), after_rows)


class VerifyOriginalBytesUnitTests(unittest.TestCase):
    def test_rejects_untrusted_digest_and_size_without_opening(self):
        blobs = Path("/nonexistent-cas")
        for sha, size in (("../" + "a" * 61, 1), ("A" * 64, 1), ("a" * 63, 1), (None, 1),
                          ("a" * 64, -1), ("a" * 64, True), ("a" * 64, "1")):
            self.assertEqual(mail_adoption.verify_original_bytes(blobs, sha, size),
                             "identity_original_blob_invalid")


class ExactWireReceiptBindingTests(_Base):
    """Schema'd nested receipts (non-trivial parents) must validate as complete."""

    def setUp(self):
        super().setUp()
        for path in (self.export / "receipts").iterdir():
            path.unlink()

    def export_raw(self, raw, uid):
        path = eml_export.export_message(raw, mail_root=self.export, account="synthetic-adopt-account",
                                         mailbox="Adoption", uidvalidity=900, uid=uid)
        target = self.export / "receipts" / ("wire-%d.json" % uid)
        target.write_bytes(Path(path).read_bytes())
        os.chmod(target, 0o600)
        return target

    def nested_parent_row(self):
        con = sqlite3.connect(self.ledger.as_uri() + "?mode=ro&immutable=1", uri=True)
        try:
            mids = {row[0] for row in con.execute("SELECT id FROM mail_messages")}
            for oid, parent in con.execute("SELECT id,parent_occurrence_id FROM occurrences WHERE kind='attachment'"):
                if parent not in mids:
                    return oid
        finally:
            con.close()
        raise AssertionError("fixture has no nested occurrence")

    def test_nested_wire_receipt_validates_then_parent_drift_is_refused(self):
        raw, _, _, _ = chain()
        self.path11 = self.export_raw(raw, 11)
        first = self.adopt(apply=True)
        self.assertEqual((first["adopted_messages"], first["failures"]), (1, {}), first)
        again = self.adopt()
        self.assertEqual((again["already_adopted"], again["identity_conflicts"]), (1, {}), again)
        self.assertFalse(mail_adoption.blocking(again))
        self.mutate("UPDATE occurrences SET parent_occurrence_id=? WHERE id=?",
                    (mail_adoption.message_id(("synthetic-adopt-account", "Adoption", "900", "11")),
                     self.nested_parent_row()))
        before_rows = ledger_rows(self.ledger)
        for apply in (False, True):
            report = self.adopt(apply=apply)
            self.assertEqual(report["identity_conflicts"], {"identity_parent_changed": 1})
            self.assertEqual(report["already_adopted"], 0)
            self.assertTrue(mail_adoption.blocking(report))
        self.assertEqual(ledger_rows(self.ledger), before_rows)

    def test_nested_wire_receipt_corrupt_nested_blob_is_refused(self):
        raw, _, _, _ = chain()
        self.export_raw(raw, 11)
        first = self.adopt(apply=True)
        self.assertEqual((first["adopted_messages"], first["failures"]), (1, {}), first)
        con = sqlite3.connect(self.ledger.as_uri() + "?mode=ro&immutable=1", uri=True)
        sha = con.execute("SELECT original_sha256 FROM occurrences WHERE id=?",
                          (self.nested_parent_row(),)).fetchone()[0]
        con.close()
        path = self.root / "intake" / "blobs" / sha
        data = path.read_bytes()
        os.chmod(path, 0o600)
        path.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
        os.chmod(path, 0o400)
        before_rows, before_tree = ledger_rows(self.ledger), tree(self.root)
        for apply in (False, True):
            report = self.adopt(apply=apply)
            self.assertEqual(report["identity_conflicts"], {"identity_original_blob_changed": 1})
            self.assertEqual(report["already_adopted"], 0)
            self.assertTrue(mail_adoption.blocking(report))
            self.assertEqual(tree(self.root), before_tree)
        self.assertEqual(ledger_rows(self.ledger), before_rows)

    @unittest.skipUnless(native.available(), "optional native MSG parser unavailable")
    def test_native_msg_item_receipt_validates_then_native_item_drift_is_refused(self):
        _, scalar, _, _ = chain()
        msg = native.message(embedded=True, attachment=scalar)
        raw = mixed([leaf(), binary(msg, b"outer.msg", b"application/vnd.ms-outlook")])
        self.export_raw(raw, 11)
        first = self.adopt(apply=True)
        self.assertEqual((first["adopted_messages"], first["failures"]), (1, {}), first)
        again = self.adopt()
        self.assertEqual((again["already_adopted"], again["identity_conflicts"]), (1, {}), again)
        con = sqlite3.connect(self.ledger.as_uri() + "?mode=ro&immutable=1", uri=True)
        oid, evidence = con.execute("SELECT id,evidence FROM occurrences WHERE "
                                    "json_extract(evidence,'$.native_item') IS NOT NULL").fetchone()
        con.close()
        value = json.loads(evidence)
        value["native_item"]["representation"] = "synthetic_drift"
        self.mutate("UPDATE occurrences SET evidence=? WHERE id=?", (json.dumps(value), oid))
        report = self.adopt()
        self.assertEqual(report["identity_conflicts"], {"identity_evidence_mismatch": 1})

    @unittest.skipUnless(native.available(), "optional native MSG parser unavailable")
    def test_native_msg_item_corrupt_native_blob_is_refused(self):
        _, scalar, _, _ = chain()
        msg = native.message(embedded=True, attachment=scalar)
        self.export_raw(mixed([leaf(), binary(msg, b"outer.msg", b"application/vnd.ms-outlook")]), 11)
        first = self.adopt(apply=True)
        self.assertEqual((first["adopted_messages"], first["failures"]), (1, {}), first)
        con = sqlite3.connect(self.ledger.as_uri() + "?mode=ro&immutable=1", uri=True)
        sha = con.execute("SELECT original_sha256 FROM occurrences WHERE "
                          "json_extract(evidence,'$.native_item') IS NOT NULL").fetchone()[0]
        con.close()
        path = self.root / "intake" / "blobs" / sha
        data = path.read_bytes()
        os.chmod(path, 0o600)
        path.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
        os.chmod(path, 0o400)
        for apply in (False, True):
            report = self.adopt(apply=apply)
            self.assertEqual(report["identity_conflicts"], {"identity_original_blob_changed": 1})


del _Base, _BytesBase

if __name__ == "__main__":
    unittest.main()
