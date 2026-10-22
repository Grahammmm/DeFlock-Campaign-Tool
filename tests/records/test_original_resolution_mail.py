"""T4B slice A, groups B and C: mail-export resolution reader and append-only writer.

Synthetic ledger, fictional bytes and a fictional intake output; no mailbox, no network.
Tampering runs inside a transaction that is rolled back, so each case starts clean.
"""
import json
import sqlite3
import unittest
from unittest.mock import patch

from campaign_tool.records.intake import folder
from campaign_tool.records.ledger import original_resolution as module, store
from tests.records.mail_resolution_fixtures import MailResolutionCase, build_policy, sha

EML_OCC, ATT_OCC = sha("eml occurrence"), sha("attachment occurrence")


class Base(MailResolutionCase, unittest.TestCase):
    def setUp(self):
        self.setUpFixture()
        self.occurrence(EML_OCC, self.eml, "mail")
        self.occurrence(ATT_OCC, self.att, "attachment", uid=8, mime_path="1.2")
        self.policy = build_policy({self.eml: self.entry(self.eml, EML_OCC),
                                    self.att: self.entry(self.att, ATT_OCC)})

    def append(self, subject=None, occurrence=None, **changes):
        subject = subject or self.eml
        occurrence = occurrence or (EML_OCC if subject == self.eml else ATT_OCC)
        arguments = self.kwargs(subject, occurrence, self.policy)
        arguments.update(changes)
        with self.writer() as con:
            return module.append_mail_export(con, **arguments)

    def effective(self, subject=None):
        with store.ledger(self.database, readonly=True) as con:
            return module.effective_original(con, subject or self.eml)

    def resolutions(self):
        if not self.rows("SELECT 1 FROM sqlite_master WHERE name='original_resolutions'"):
            return []
        return self.rows("SELECT * FROM original_resolutions")


class ReaderTests(Base):
    def test_happy_path_overlay_and_untouched_declaration(self):  # 5
        before = self.rows("SELECT * FROM originals")
        declared = self.effective()
        self.assertEqual((declared["role"], declared["scope"], declared["storage_path"]),
                         ("unresolved", "unresolved", None))
        identity, reused = self.append()
        self.assertFalse(reused)
        original = self.effective()
        self.assertEqual((original["role"], original["scope"]), ("original", "in_scope"))
        self.assertEqual(original["storage_path"], self.storage(self.eml))
        self.assertEqual(original["resolution_sha256"], identity)
        self.assertEqual(original["preservation_status"], "mail_export_validation_pending")
        self.assertEqual(self.rows("SELECT * FROM originals"), before)

    def test_payload_is_exact_and_holds_no_raw_identity(self):
        self.append()
        row = self.rows("SELECT payload_json,capture_receipt_sha256,capture_occurrence_id,policy_sha256 "
                        "FROM original_resolutions")[0]
        payload = json.loads(row[0])
        self.assertEqual(set(payload), module.MAIL_FIELDS)
        self.assertEqual((row[1], row[2], row[3]), (self.receipt, EML_OCC, self.policy.sha256))
        self.assertEqual(payload["occurrence_kind"], "mail")
        self.assertEqual(payload["legacy_binding_sha256"], self.legacy_binding(self.eml))
        self.assertNotIn("synthetic-account", row[0])
        self.assertNotIn("INBOX", row[0])

    def tamper(self, mutate=None, *, columns=None, sql=(), rebind=True, code=None, subject=None):
        """Edit inside a transaction (triggers dropped), then expect the reader to refuse."""
        subject = subject or self.eml
        self.append(subject)
        with store.ledger(self.database) as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                for trigger in ("original_resolution_update", "immutable_originals_update",
                                "immutable_originals_insert"):
                    con.execute("DROP TRIGGER " + trigger)
                payload = json.loads(con.execute("SELECT payload_json FROM original_resolutions").fetchone()[0])
                if mutate:
                    mutate(payload)
                values = {"payload_json": store.canonical(payload)}
                if rebind:
                    values["binding_sha256"] = module.digest(payload)
                values.update(columns or {})
                for statement in sql:
                    con.execute(*statement)
                for key, value in values.items():
                    con.execute(f"UPDATE original_resolutions SET {key}=?", (value,))
                with self.assertRaisesRegex(ValueError, code):
                    module.effective_original(con, subject)
            finally:
                con.rollback()

    def test_tampered_records_fail_closed(self):  # 6
        e = self.eml
        cases = {
            "extra key": (lambda p: p.update(extra=1), dict(code="original_resolution_payload_mismatch")),
            "missing key": (lambda p: p.pop("role"), dict(code="original_resolution_payload_mismatch")),
            "other subject": (lambda p: p.update(original_sha256=self.att), dict(code="original_resolution_payload_mismatch")),
            "binding hash": (None, dict(columns={"binding_sha256": sha("other")}, rebind=False,
                                        code="original_resolution_declaration_mismatch")),
            "declaration digest": (lambda p: p.update(declaration_sha256=sha("x")),
                                   dict(code="original_resolution_declaration_mismatch")),
            "byte length": (lambda p: p.update(byte_length=1), dict(code="original_resolution_declaration_mismatch")),
            "role": (lambda p: p.update(role="reference"), dict(code="original_resolution_declaration_mismatch")),
            "declaration edited": (None, dict(sql=[("UPDATE originals SET legacy_format='txt' WHERE sha256=?", (e,))],
                                              code="original_resolution_declaration_mismatch")),
            "declaration resolved elsewhere": (None, dict(
                sql=[("UPDATE originals SET storage_path='/x' WHERE sha256=?", (e,))],
                code="original_resolution_declaration_mismatch")),
            "policy column": (None, dict(columns={"policy_sha256": sha("other")}, rebind=False,
                                         code="original_resolution_evidence_mismatch")),
            "receipt column": (lambda p: p.update(verification_receipt_sha256=sha("other")),
                               dict(code="original_resolution_evidence_mismatch")),
            "occurrence column": (lambda p: p.update(occurrence_id=sha("other")),
                                  dict(code="original_resolution_evidence_mismatch")),
            "occurrence missing": (lambda p: p.update(occurrence_id=sha("other")),
                                   dict(columns={"capture_occurrence_id": sha("other")}, sql=[("PRAGMA defer_foreign_keys=ON",)],
                                        code="original_resolution_mail_occurrence_unbound")),
            "wrong method": (None, dict(sql=[("UPDATE occurrences SET acquisition_method='captured_legacy_local' WHERE id=?", (EML_OCC,))],
                                        code="original_resolution_mail_occurrence_unbound")),
            "wrong kind": (None, dict(sql=[("UPDATE occurrences SET kind='attachment' WHERE id=?", (EML_OCC,))],
                                      code="original_resolution_mail_occurrence_unbound")),
            "payload kind": (lambda p: p.update(occurrence_kind="attachment"),
                             dict(code="original_resolution_mail_occurrence_unbound")),
            "wrong sha": (None, dict(sql=[("UPDATE occurrences SET original_sha256=? WHERE id=?", (self.att, EML_OCC))],
                                     code="original_resolution_mail_occurrence_unbound")),
            "mail identity": (lambda p: p.update(mail_identity_sha256=sha("other")),
                              dict(code="original_resolution_mail_occurrence_unbound")),
            "source ref edited": (None, dict(sql=[("UPDATE occurrences SET source_ref=json_set(source_ref,'$.uid',99) WHERE id=?", (EML_OCC,))],
                                             code="original_resolution_mail_occurrence_unbound")),
            "evidence receipt": (None, dict(sql=[("UPDATE occurrences SET evidence=json_set(evidence,'$.receipt_sha256',?) WHERE id=?",
                                                  (sha("other"), EML_OCC))], code="original_resolution_mail_occurrence_unbound")),
            "evidence receipt not a digest": (None, dict(sql=[("UPDATE occurrences SET evidence=json_set(evidence,'$.receipt_sha256','../x') WHERE id=?",
                                                               (EML_OCC,))], code="original_resolution_mail_occurrence_unbound")),
            "evidence unparsable": (None, dict(sql=[("UPDATE occurrences SET evidence='not json' WHERE id=?", (EML_OCC,))],
                                               code="original_resolution_mail_occurrence_unbound")),
            "evidence path": (None, dict(sql=[("UPDATE occurrences SET evidence=json_set(evidence,'$.verification_receipt_path','/elsewhere') WHERE id=?",
                                               (EML_OCC,))], code="original_resolution_mail_receipt_changed")),
            "storage name": (lambda p: p.update(storage_path=str(self.output / "blobs" / self.att)),
                             dict(code="original_resolution_storage_binding")),
            "storage not absolute": (lambda p: p.update(storage_path="blobs/" + e),
                                     dict(code="original_resolution_storage_binding")),
            "storage not under blobs": (lambda p: p.update(storage_path=str(self.output / "cas" / e)),
                                        dict(code="original_resolution_storage_binding")),
            "other output root": (lambda p: p.update(storage_path=str(self.base / "other-out" / "blobs" / e)),
                                  dict(code="original_resolution_mail_receipt_changed")),
        }
        for name, (mutate, options) in cases.items():
            with self.subTest(name):
                self.tamper(mutate, **options)

    def test_verification_receipt_must_still_exist_and_hash(self):  # 6
        self.append()
        path = self.output / "verification-receipts" / self.receipt
        content = path.read_bytes()
        try:
            path.chmod(0o600)
            path.write_bytes(content + b" ")
            with self.assertRaisesRegex(ValueError, "original_resolution_mail_receipt_changed"):
                self.effective()
            path.unlink()
            with self.assertRaisesRegex(ValueError, "original_resolution_mail_receipt_changed"):
                self.effective()
            elsewhere = self.base / "real-receipt"
            elsewhere.write_bytes(content)
            path.symlink_to(elsewhere)
            with self.assertRaisesRegex(ValueError, "original_resolution_mail_receipt_changed"):
                self.effective()
            path.unlink()
            path.write_bytes(b"x" * (module.MAX_RECEIPT_BYTES + 1))
            with self.assertRaisesRegex(ValueError, "original_resolution_mail_receipt_changed"):
                self.effective()
        finally:
            path.unlink(missing_ok=True)
            path.write_bytes(content)
            path.chmod(0o400)
        self.assertEqual(self.effective()["scope"], "in_scope")

    def test_unknown_payload_schema_is_refused(self):  # 7
        self.append()
        with store.ledger(self.database) as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                con.execute("DROP TRIGGER original_resolution_update")
                payload = json.loads(con.execute("SELECT payload_json FROM original_resolutions").fetchone()[0])
                payload["schema"] = "original_resolution_mail_export_v2"
                con.execute("UPDATE original_resolutions SET payload_json=?", (store.canonical(payload),))
                with self.assertRaisesRegex(ValueError, "original_resolution_payload_mismatch"):
                    module.effective_original(con, self.eml)
            finally:
                con.rollback()

    def test_pre_t4b_reader_refuses_a_mail_payload_loudly(self):  # 8
        # `_effective_v1` is the unchanged installed-extension reader; it is what an
        # engine that predates T4B runs for every row.
        self.append()
        with store.ledger(self.database, readonly=True) as con:
            row = con.execute("SELECT * FROM original_resolutions").fetchone()
            original = dict(con.execute("SELECT * FROM originals WHERE sha256=?", (self.eml,)).fetchone())
            payload = json.loads(row["payload_json"])
            self.assertNotEqual(set(payload), module.FIELDS)
            with self.assertRaisesRegex(ValueError, "original_resolution_payload_mismatch"):
                module._effective_v1(con, self.eml, original, row, payload)

    def test_v1_reader_is_unchanged_and_dispatch_is_by_schema(self):  # 9
        # v1 local-capture behaviour is exercised end to end, unchanged, by
        # tests/records/test_original_reconciliation.py. Here: mail rows dispatch to the
        # mail reader and an original without a row still reads as its declaration.
        self.assertEqual(self.effective(self.att)["scope"], "unresolved")
        self.append(self.att)
        self.assertEqual(self.effective(self.att)["preservation_status"], "mail_export_validation_pending")
        self.assertEqual(self.effective(self.eml)["scope"], "unresolved")


class AppendTests(Base):
    def refused(self, code, subject=None, **changes):
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, code):
            self.append(subject, **changes)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.resolutions(), [])

    def build(self):
        return build_policy({self.eml: self.entry(self.eml, EML_OCC), self.att: self.entry(self.att, ATT_OCC)})

    def refused_in_transaction(self, code, prepare, policy=None, subject=None, occurrence=None):
        """Run append after `prepare(con)` (trigger drops, edits); nothing survives the rollback."""
        subject = subject or self.eml
        occurrence = occurrence or (EML_OCC if subject == self.eml else ATT_OCC)
        with store.ledger(self.database) as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                prepare(con)
                with self.assertRaisesRegex(ValueError, code):
                    module.append_mail_export(con, **self.kwargs(subject, occurrence, policy or self.build()))
            finally:
                con.rollback()
        self.assertEqual(self.resolutions(), [])

    def test_check_1_original_must_be_unresolved(self):  # 10
        def resolve(con):
            con.execute("DROP TRIGGER immutable_originals_update")
            con.execute("UPDATE originals SET role='original',scope='in_scope' WHERE sha256=?", (self.eml,))
        self.refused_in_transaction("original_resolution_not_unresolved", resolve)

    def test_check_1_cas_must_exist_hash_and_have_the_declared_length(self):  # 10
        path = self.output / "blobs" / self.eml
        content = path.read_bytes()
        try:
            path.chmod(0o600)
            path.write_bytes(b"different bytes")
            self.refused("original_resolution_cas_unavailable")
            path.unlink()
            self.refused("original_resolution_cas_unavailable")
        finally:
            path.write_bytes(content)
            path.chmod(0o400)
        wrong = dict(sha256=self.eml, bytes=len(content) + 1, before_mode=0o400, after_mode=0o400)
        with patch.object(folder, "verify_blob", return_value=wrong):
            self.refused("original_resolution_cas_length_mismatch")

    def test_check_2_legacy_binding(self):  # 10
        self.policy = build_policy({self.eml: dict(self.entry(self.eml, EML_OCC), legacy_binding_sha256=sha("other"))})
        self.refused("resolution_policy_legacy_binding_mismatch")
        self.policy = self.build()

        def no_docs_row(con):
            con.execute("DROP TRIGGER immutable_originals_update")
            con.execute("UPDATE originals SET provenance_json='{}' WHERE sha256=?", (self.eml,))
        self.refused_in_transaction("original_resolution_import_evidence_missing", no_docs_row)

        def other_format(con):
            con.execute("DROP TRIGGER immutable_originals_update")
            con.execute("UPDATE originals SET legacy_format='pdf' WHERE sha256=?", (self.eml,))
        self.refused_in_transaction("original_resolution_import_binding_mismatch", other_format)

    def test_check_3_original_must_be_untouched(self):  # 10
        def accepted_stage(con):
            con.execute("UPDATE stage_state SET status='blocked' WHERE original_sha256=? AND stage='extract'", (self.eml,))
        self.refused_in_transaction("original_resolution_requires_unaccepted_original", accepted_stage)

    def test_check_5_policy_membership_and_bytes(self):  # 10
        self.policy = build_policy({self.att: self.entry(self.att, ATT_OCC)})
        self.refused("resolution_not_in_policy")
        self.policy = build_policy({self.eml: self.entry(self.eml, EML_OCC, size=1)})
        self.refused("resolution_policy_bytes_mismatch")

    def test_check_7_kind(self):  # 10
        # A non-eml original named by a `mail` occurrence is a kind mismatch.
        text_as_mail = self.occurrence(sha("txt as mail"), self.att, "mail", uid=9)
        self.policy = build_policy({self.att: self.entry(self.att, text_as_mail)})
        self.refused("original_resolution_mail_kind_mismatch", subject=self.att, occurrence=text_as_mail)

    def test_occurrence_checks_leave_no_row(self):  # 10, 6
        self.policy = build_policy({self.eml: self.entry(self.eml, sha("missing"))})
        self.refused("original_resolution_mail_occurrence_unbound", occurrence=sha("missing"))
        other = self.occurrence(sha("other subject occurrence"), self.att, "attachment", uid=12)
        self.policy = build_policy({self.eml: self.entry(self.eml, other)})
        self.refused("original_resolution_mail_occurrence_unbound", occurrence=other)
        self.policy = self.build()
        self.refused("original_resolution_storage_binding", storage_path="relative/blobs/" + self.eml)
        self.refused("original_resolution_storage_binding", storage_path=str(self.output / "cas" / self.eml))

    def test_receipt_changed_before_append_leaves_no_row(self):  # 6
        path = self.output / "verification-receipts" / self.receipt
        content = path.read_bytes()
        path.chmod(0o600)
        path.write_bytes(content + b" ")
        try:
            self.refused("original_resolution_mail_receipt_changed")
        finally:
            path.write_bytes(content)
            path.chmod(0o400)

    def test_gate_function_is_refused_outside_the_writer(self):  # 11
        self.append()
        values = self.rows("SELECT * FROM original_resolutions")[0]
        with store.ledger(self.database) as con:
            # No gate function registered on this connection, and the one-shot gate is
            # closed again after the backend call: a direct insert is refused.
            with self.assertRaises(sqlite3.Error):
                con.execute("INSERT INTO original_resolutions VALUES(?,?,?,?,?,?,?)", values)
            con.create_function("original_resolution_append_allowed", -1, lambda *row: 0)
            with self.assertRaises(sqlite3.Error):
                con.execute("INSERT INTO original_resolutions VALUES(?,?,?,?,?,?,?)", values)
            con.create_function("original_resolution_append_allowed", -1, lambda *row: 1)
            with self.assertRaises(sqlite3.Error):  # one resolution per original, even if a gate says yes
                con.execute("INSERT INTO original_resolutions VALUES(?,?,?,?,?,?,?)", values)
        self.assertEqual(len(self.resolutions()), 1)

    def test_update_and_delete_refused_and_second_evidence_conflicts(self):  # 12
        self.append()
        with store.ledger(self.database) as con:
            for statement in ("UPDATE original_resolutions SET policy_sha256='x'", "DELETE FROM original_resolutions"):
                with self.assertRaises(sqlite3.Error):
                    con.execute(statement)
        second = self.occurrence(sha("second eml occurrence"), self.eml, "mail", uid=11)
        other = build_policy({self.eml: self.entry(self.eml, second)})
        before = self.resolutions()
        with self.assertRaisesRegex(ValueError, "original_resolution_conflict"):
            self.append(policy=other, occurrence_id=second)
        self.assertEqual(self.resolutions(), before)

    def test_exact_replay_is_idempotent(self):  # 13
        first = self.append()
        before = self.snapshot()
        second = self.append()
        self.assertFalse(first[1])
        self.assertEqual(second, (first[0], True))
        self.assertEqual(self.snapshot(), before)

    def test_local_then_mail_and_mail_then_local_are_refused(self):  # 14
        # mail-export then local-capture
        self.append(self.att)
        with store.ledger(self.database) as con:
            con.execute("BEGIN IMMEDIATE")
            with self.assertRaisesRegex(ValueError, "original_resolution_conflict"):
                module.append_initial(con, subject=self.att, policy_sha256=sha("local policy"),
                                      capture_receipt_sha256=self.receipt, capture_occurrence_id=ATT_OCC,
                                      storage_path=self.storage(self.att))
            con.rollback()
        # local-capture then mail-export: a v1 row blocks the mail row for the same original
        with store.ledger(self.database) as con:
            con.execute("BEGIN IMMEDIATE")
            module.ensure_schema(con)
            con.execute("DROP TRIGGER original_resolution_delete")
            con.execute("DROP TRIGGER original_resolution_update")
            con.execute("DELETE FROM original_resolutions")
            con.create_function("original_resolution_append_allowed", -1, lambda *row: 1)
            con.execute("INSERT INTO original_resolutions VALUES(?,?,?,?,?,?,?)",
                        (self.eml, sha("v1 binding"), sha("declaration"), sha("local policy"), self.receipt, EML_OCC,
                         json.dumps({"schema": "original_resolution_v1"})))
            with self.assertRaisesRegex(ValueError, "original_resolution_conflict"):
                module.append_mail_export(con, **self.kwargs(self.eml, EML_OCC, self.policy))
            con.rollback()

    def test_policy_receipt_or_occurrence_mismatch_is_refused(self):  # 15
        self.refused("resolution_policy_receipt_mismatch", occurrence_id=ATT_OCC)
        self.refused("resolution_policy_receipt_mismatch", verification_receipt_sha256=sha("another receipt"))
        policy = build_policy({self.eml: self.entry(self.eml, EML_OCC, receipt=sha("another receipt"))})
        self.policy = policy
        self.refused("resolution_policy_receipt_mismatch")

    def test_writer_requires_policy_object_and_transaction(self):
        with self.writer() as con:
            with self.assertRaisesRegex(ValueError, "installed_mail_resolution_policy_required"):
                module.append_mail_export(con, **dict(self.kwargs(self.eml, EML_OCC, self.policy), policy=object()))
        with store.ledger(self.database) as con:
            with self.assertRaisesRegex(ValueError, "original_resolution_requires_transaction"):
                module.append_mail_export(con, **self.kwargs(self.eml, EML_OCC, self.policy))
        self.assertEqual(self.resolutions(), [])

    def test_failed_append_rolls_back_with_the_transaction(self):
        before = self.snapshot()
        with self.assertRaisesRegex(RuntimeError, "fault"):
            with self.writer() as con:
                module.append_mail_export(con, **self.kwargs(self.eml, EML_OCC, self.policy))
                raise RuntimeError("fault")
        self.assertEqual(self.snapshot(), before)

    def test_append_leaves_declarations_and_legacy_rows_untouched(self):
        tables = ("originals", "legacy_rows", "occurrence_projection_rows", "stage_state", "stage_events",
                  "receipts")
        before = {t: self.rows(f"SELECT * FROM {t}") for t in tables}
        legacy = self.rows("SELECT * FROM occurrences WHERE acquisition_method LIKE 'captured_legacy_%'")
        self.append()
        self.append(self.att)
        self.assertEqual({t: self.rows(f"SELECT * FROM {t}") for t in tables}, before)
        self.assertEqual(self.rows("SELECT * FROM occurrences WHERE acquisition_method LIKE 'captured_legacy_%'"), legacy)
        self.assertEqual(len(self.resolutions()), 2)


if __name__ == "__main__":
    unittest.main()
