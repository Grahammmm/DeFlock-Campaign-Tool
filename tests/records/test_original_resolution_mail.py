"""Contract B/C: immutable synthetic declarations, mail payloads and gated appends."""
import hashlib
import json
import sqlite3
import unittest

from campaign_tool.records.ledger import store, stages, original_resolution as resolution
from campaign_tool.records.ledger.migrations import original_resolution_v001 as migration
from tests.records import test_original_reconciliation as fixtures
from tests.records.test_mail_resolution_policy import approved, value


class OriginalResolutionMailTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.OriginalReconciliationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.database, self.sha = self.fixture.database, self.fixture.sha
        self.output = self.fixture.backend.output
        self.path = str(self.output / "blobs" / self.sha)
        self.raw = b'{"schema":"synthetic-verification-receipt"}'
        self.receipt = hashlib.sha256(self.raw).hexdigest()
        self.oid = hashlib.sha256(b"fictional occurrence").hexdigest()
        self.receipt_path = self.output / "verification-receipts" / self.receipt
        self.receipt_path.parent.mkdir(mode=0o700)
        self.receipt_path.write_bytes(self.raw)
        self.source = {"account": "synthetic-account", "folder": "INBOX", "uidvalidity": 1, "uid": 7, "mime_path": "1.2"}
        self.evidence = {"receipt_sha256": self.receipt, "verification_receipt_path": str(self.receipt_path)}
        with store.ledger(self.database) as con:
            resolution.ensure_schema(con)
            self.legacy = con.execute("SELECT payload_sha256 FROM legacy_rows WHERE source_table='docs'").fetchone()[0]
            con.execute("INSERT INTO occurrences VALUES(?,?,?,?,?,?,?,?)", (self.oid, self.sha, "attachment",
                        store.canonical(self.source), None, None, "verified_mail_export", store.canonical(self.evidence)))
            con.commit()
        self.policy_value = value(self.sha, self.fixture.source.stat().st_size, self.receipt, self.oid, self.legacy)
        self.policy = approved(self.policy_value)

    def append(self, con, **changes):
        args = dict(subject=self.sha, policy=self.policy, verification_receipt_sha256=self.receipt,
                    occurrence_id=self.oid, storage_path=self.path)
        args.update(changes)
        return resolution.append_mail_export(con, **args)

    def enroll(self):
        with store.ledger(self.database) as con:
            con.execute("BEGIN IMMEDIATE")
            result = self.append(con)
            con.commit()
            return result

    def test_happy_overlay_immutable_original_and_all_occurrences(self):
        before = self.fixture.rows("SELECT * FROM originals")
        occurrences = self.fixture.rows("SELECT * FROM occurrences")
        first, reused = self.enroll()
        self.assertFalse(reused)
        with store.ledger(self.database, readonly=True) as con:
            row = resolution.effective_original(con, self.sha)
            self.assertEqual((row["role"], row["scope"], row["storage_path"], row["preservation_status"]),
                             ("original", "in_scope", self.path, "mail_export_validation_pending"))
            self.assertEqual(row["resolution_sha256"], first)
        self.assertEqual(self.fixture.rows("SELECT * FROM originals"), before)
        self.assertEqual(self.fixture.rows("SELECT * FROM occurrences"), occurrences)
        self.assertEqual(self.fixture.rows("SELECT DISTINCT status FROM stage_state"), [("pending",)])
        self.assertEqual(self.enroll(), (first, True))
        with sqlite3.connect(self.database) as con:
            self.assertEqual(resolution.effective_original(con, self.sha)["resolution_sha256"], first)
            self.assertIsNone(con.row_factory)

    def test_same_original_multiple_genuine_occurrences_one_binding(self):
        with store.ledger(self.database) as con:
            con.execute("INSERT INTO occurrences SELECT ?,original_sha256,kind,source_ref,parent_occurrence_id,"
                        "acquired_at,acquisition_method,evidence FROM occurrences WHERE id=?", ("0" * 64, self.oid))
            con.commit()
        self.enroll()
        self.assertEqual(self.fixture.rows("SELECT capture_occurrence_id FROM original_resolutions"), [(self.oid,)])
        self.assertEqual(self.fixture.rows("SELECT count(*) FROM occurrences WHERE acquisition_method='verified_mail_export'"), [(2,)])


    def test_reader_rejects_every_payload_and_column_tamper(self):
        cases = [("extra", 1), ("schema", "unknown"), ("byte_length", 1234), ("byte_length", True),
                 ("role", "unresolved"), ("scope", "out_of_scope"), ("mail_identity_sha256", "0" * 64),
                 ("occurrence_kind", "mail"), ("occurrence_kind", None),
                 ("storage_path", "relative/blobs/" + self.sha),
                 ("storage_path", str(self.output / "other" / self.sha)),
                 ("storage_path", str(self.output.parent / "elsewhere" / "blobs" / self.sha)),
                 ("declaration_sha256", "0" * 64)]
        for key, val in cases:
            with self.subTest(key=key, val=val):
                def mutate(con, row, payload):
                    payload[key] = val
                    con.execute("UPDATE original_resolutions SET payload_json=?,binding_sha256=?",
                                (store.canonical(payload), resolution.digest(payload)))
                self._fresh_tamper(mutate)
        for column in ("binding_sha256", "declaration_sha256", "policy_sha256", "capture_receipt_sha256", "capture_occurrence_id"):
            with self.subTest(column=column):
                def mutate(con, row, payload):
                    con.execute("PRAGMA defer_foreign_keys=ON")
                    con.execute("UPDATE original_resolutions SET " + column + "=?", ("0" * 64,))
                self._fresh_tamper(mutate)
        self._fresh_tamper(lambda con, row, payload: con.execute("UPDATE originals SET bytes=bytes+1"), original=True)
        self._fresh_tamper(lambda con, row, payload: con.execute("UPDATE original_resolutions SET payload_json=?", ('{}',)))

    def _fresh_tamper(self, mutate, *, original=False):
        case = OriginalResolutionMailTests()
        case.setUp()
        try:
            case.enroll()
            with store.ledger(case.database) as con:
                con.execute("BEGIN IMMEDIATE")
                con.execute("PRAGMA defer_foreign_keys=ON")
                con.execute("DROP TRIGGER original_resolution_update")
                if original:
                    for row in con.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='originals'").fetchall():
                        con.execute('DROP TRIGGER "' + row[0] + '"')
                row = dict(con.execute("SELECT * FROM original_resolutions").fetchone())
                payload = json.loads(row["payload_json"])
                mutate(con, row, payload)
                with self.assertRaises(ValueError):
                    resolution.effective_original(con, case.sha)
                con.rollback()
        finally:
            case.doCleanups()

    def test_reader_occurrence_tampers(self):
        for column, val in (("acquisition_method", "captured_legacy_local"), ("kind", "mail"),
                            ("original_sha256", "0" * 64), ("source_ref", '{}'),
                            ("evidence", store.canonical(dict(self.evidence, receipt_sha256="0" * 64)))):
            with self.subTest(column=column):
                self._fresh_tamper(lambda con, row, payload, column=column, val=val:
                                  con.execute("UPDATE occurrences SET " + column + "=? WHERE id=?", (val, self.oid)))
        def missing(con, row, payload):
            con.execute("PRAGMA defer_foreign_keys=ON")
            con.execute("DELETE FROM occurrences WHERE id=?", (self.oid,))
        self._fresh_tamper(missing)

    def test_reader_receipt_missing_changed_symlink_oversize(self):
        self.enroll()
        for mode in ("missing", "changed", "symlink", "oversize", "parent_symlink"):
            with self.subTest(mode=mode):
                self.receipt_path.unlink(missing_ok=True)
                if mode == "changed": self.receipt_path.write_bytes(b"changed")
                if mode == "oversize": self.receipt_path.write_bytes(b"x" * (1024 * 1024 + 1))
                if mode == "symlink": self.receipt_path.symlink_to(self.fixture.source)
                if mode == "parent_symlink":
                    self.receipt_path.parent.rmdir()
                    self.receipt_path.parent.symlink_to(self.output / "blobs", target_is_directory=True)
                with store.ledger(self.database, readonly=True) as con:
                    with self.assertRaisesRegex(ValueError, "original_resolution_mail_receipt_changed"):
                        resolution.effective_original(con, self.sha)

    def test_old_reader_loudly_refuses_and_schema_pin_unchanged(self):
        self.enroll()
        with store.ledger(self.database, readonly=True) as con:
            p = json.loads(con.execute("SELECT payload_json FROM original_resolutions").fetchone()[0])
            with self.assertRaisesRegex(ValueError, "original_resolution_payload_mismatch"):
                resolution.require(set(p) == resolution.FIELDS and p["schema"] == migration.NAME,
                                   "original_resolution_payload_mismatch")
            self.assertEqual(con.execute("SELECT value FROM ledger_meta WHERE key=?", (resolution.KEY,)).fetchone()[0],
                             hashlib.sha256(migration.SQL.encode()).hexdigest())

    def test_transaction_required_and_gate_cannot_be_reused(self):
        with store.ledger(self.database) as con:
            with self.assertRaisesRegex(ValueError, "transaction_required"):
                self.append(con)
        self.enroll()
        with store.ledger(self.database) as con:
            with self.assertRaisesRegex(sqlite3.OperationalError, "no such function"):
                con.execute("SELECT original_resolution_append_allowed(1)")
            con.execute("BEGIN IMMEDIATE")
            self.append(con)
            for sql in ("UPDATE original_resolutions SET policy_sha256='wrong'", "DELETE FROM original_resolutions",
                        "INSERT OR REPLACE INTO original_resolutions SELECT * FROM original_resolutions"):
                with self.subTest(sql=sql), self.assertRaises(sqlite3.Error): con.execute(sql)
                con.rollback()

    def test_conflicting_evidence_and_both_cross_kind_orders_refuse(self):
        self.enroll()
        other = dict(self.policy_value, approval_sha256="0" * 64)
        with store.ledger(self.database) as con:
            con.execute("BEGIN IMMEDIATE")
            with self.assertRaisesRegex(ValueError, "original_resolution_conflict"):
                self.append(con, policy=approved(other))
            with self.assertRaisesRegex(ValueError, "original_resolution_conflict"):
                resolution.append_initial(con, subject=self.sha, policy_sha256=self.policy.sha256,
                    capture_receipt_sha256=self.receipt, capture_occurrence_id=self.oid, storage_path=self.path)
            con.rollback()
        case = OriginalResolutionMailTests(); case.setUp()
        try:
            case.fixture.enroll()
            with store.ledger(case.database) as con:
                con.execute("BEGIN IMMEDIATE")
                with self.assertRaisesRegex(ValueError, "original_resolution_conflict"): case.append(con)
                con.rollback()
        finally: case.doCleanups()

    def test_wrong_binding_receipt_or_occurrence_refuses_no_row(self):
        for args in ({"occurrence_id": "0" * 64}, {"verification_receipt_sha256": "0" * 64}):
            with store.ledger(self.database) as con:
                con.execute("BEGIN IMMEDIATE")
                with self.assertRaisesRegex(ValueError, "resolution_policy_receipt_mismatch"): self.append(con, **args)
                self.assertEqual(con.execute("SELECT count(*) FROM original_resolutions").fetchone()[0], 0)
                con.rollback()

    def test_append_negative_eligibility_checks_and_rollback(self):
        changes = [
            ("UPDATE originals SET scope='out_of_scope'", "not_unresolved"),
            ("UPDATE originals SET storage_path='elsewhere'", "not_unresolved"),
            ("UPDATE originals SET provenance_json='{}'", "import_evidence_missing"),
            ("UPDATE legacy_rows SET payload_sha256='wrong' WHERE source_table='docs'", "import_binding_mismatch"),
            ("UPDATE stage_state SET status='blocked' WHERE stage='extract'", "requires_unaccepted_original"),
            ("DELETE FROM stage_state WHERE stage='extract'", "requires_unaccepted_original"),
            ("UPDATE occurrences SET kind='mail' WHERE acquisition_method='verified_mail_export'", "import_binding_mismatch"),
            ("UPDATE occurrences SET acquisition_method='captured_legacy_local' WHERE acquisition_method='verified_mail_export'", "mail_occurrence_unbound"),
        ]
        for sql, code in changes:
            with self.subTest(sql=sql), store.ledger(self.database) as con:
                con.execute("BEGIN IMMEDIATE")
                for row in con.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name IN ('originals','legacy_rows','stage_state')").fetchall():
                    con.execute('DROP TRIGGER "' + row[0] + '"')
                con.execute(sql)
                with self.assertRaisesRegex(ValueError, code): self.append(con)
                self.assertEqual(con.execute("SELECT count(*) FROM original_resolutions").fetchone()[0], 0)
                con.rollback()
        with store.ledger(self.database) as con:
            con.execute("BEGIN IMMEDIATE")
            self.append(con)
            con.rollback()
        self.assertEqual(self.fixture.rows("SELECT count(*) FROM original_resolutions"), [(0,)])

    def test_cached_content_is_not_eligible(self):
        runner = stages.testing_runner(self.database, run_id=self.fixture.identity["run_id"],
            owner="synthetic", validators={}, version="synthetic-mail-cache",
            engine_version=self.fixture.identity["runtime"]["version"],
            config_sha256=self.fixture.identity["config_sha256"])
        runner.set_content(self.sha, "preserve", b"synthetic original", author_id="synthetic", tier="A")
        with store.ledger(self.database) as con:
            con.execute("BEGIN IMMEDIATE")
            with self.assertRaisesRegex(ValueError, "requires_uncached_original"): self.append(con)
            self.assertEqual(con.execute("SELECT count(*) FROM original_resolutions").fetchone()[0], 0)
            con.rollback()

    def test_proposal_subject_and_pending_receipt_are_not_eligible(self):
        for proposal in (False, True):
            with self.subTest(proposal=proposal), store.ledger(self.database) as con:
                con.execute("BEGIN IMMEDIATE")
                con.execute("INSERT INTO receipts(sha256,stage,subject_sha256,reviewer_id,role,verdict,"
                            "coverage,locators,rationale,model_or_tool,input_hashes,created_at_tz,path) "
                            "VALUES(?,'preserve',?,'synthetic','preserver','pass','{}','[]',"
                            "'synthetic','synthetic','{}','2000-01-01T00:00:00Z','synthetic')", ("0" * 64, self.sha))
                if proposal:
                    con.execute("INSERT INTO proposals VALUES(?,?,?,?,?,?,?,?,?,?)",
                        ("synthetic", "A", None, "1" * 64, "2" * 64, "0" * 64, None, "none", None, None))
                    code = "proposal_exists"
                else:
                    con.execute("UPDATE stage_state SET receipt_sha256=? WHERE stage='preserve'", ("0" * 64,))
                    code = "requires_unaccepted_original"
                with self.assertRaisesRegex(ValueError, code): self.append(con)
                self.assertEqual(con.execute("SELECT count(*) FROM original_resolutions").fetchone()[0], 0)
                con.rollback()

    def test_rejected_missing_policy_original_and_missing_occurrence(self):
        v = value("0" * 64, 18, self.receipt, self.oid, self.legacy)
        with store.ledger(self.database) as con:
            con.execute("BEGIN IMMEDIATE")
            with self.assertRaisesRegex(ValueError, "resolution_policy_invalid"):
                self.append(con, policy=approved(v))
            con.execute("DELETE FROM occurrences WHERE id=?", (self.oid,))
            with self.assertRaisesRegex(ValueError, "mail_occurrence_unbound"): self.append(con)
            self.assertEqual(con.execute("SELECT count(*) FROM original_resolutions").fetchone()[0], 0)
            con.rollback()

    def test_policy_size_and_legacy_pin_refused(self):
        for field, invalid, code in (("bytes", 1234, "resolution_policy_invalid"),
                                     ("legacy_binding_sha256", "0" * 64, "import_binding_mismatch")):
            v = json.loads(self.policy.raw); v["originals"][self.sha][field] = invalid
            with store.ledger(self.database) as con:
                con.execute("BEGIN IMMEDIATE")
                with self.assertRaisesRegex(ValueError, code): self.append(con, policy=approved(v))
                self.assertEqual(con.execute("SELECT count(*) FROM original_resolutions").fetchone()[0], 0)
                con.rollback()
