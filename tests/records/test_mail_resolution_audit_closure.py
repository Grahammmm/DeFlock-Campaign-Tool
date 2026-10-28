"""Additional S1 audit closure controls, on the real synthetic legacy-ledger schema.

The Claude repros remain in test_t4b_*; these isolate each strengthened gate so
missing-trigger refusal cannot accidentally stand in for policy/root validation.
"""
import unittest
from unittest.mock import patch

from campaign_tool.records.ledger import original_resolution as resolution, store
from tests.records import test_original_resolution_mail as fixtures


class AuditClosureTests(unittest.TestCase):
    def setUp(self):
        self.w = fixtures.OriginalResolutionMailTests()
        self.w.setUp()
        self.addCleanup(self.w.doCleanups)

    def test_mail_reader_requires_explicit_trusted_context(self):
        self.w.enroll()
        with store.ledger(self.w.database, readonly=True) as con:
            self.assertEqual(self.w.read(con)["role"], "original")
            for kwargs, code in (({}, "resolution_policy_invalid"),
                                 ({"mail_policy": self.w.policy}, "original_resolution_storage_binding"),
                                 ({"output_root": self.w.output}, "resolution_policy_invalid")):
                with self.subTest(kwargs=tuple(kwargs)), self.assertRaisesRegex(ValueError, '^' + code + '$'):
                    resolution.effective_original(con, self.w.sha, **kwargs)

    def test_reader_refuses_external_root_even_with_valid_copied_receipt(self):
        from tests.records.test_t4b_resolution_adversarial import World
        case = World()
        case.setUp()
        self.addCleanup(case.doCleanups)
        external = case.stage_root('other-complete-root')
        external_path = str(external / 'blobs' / case.sha)
        # All payload hashes, policy, legacy row and receipt bytes are valid.
        case.raw_row(payload={'policy_sha256': case.w.policy.sha256, 'storage_path': external_path})
        case.set_evidence(verification_receipt_path=str(external / 'verification-receipts' / case.receipt))
        with self.assertRaisesRegex(ValueError, '^original_resolution_storage_binding$'):
            case.effective()

    def test_reader_proves_each_policy_entry_binding(self):
        from tests.records.test_t4b_resolution_adversarial import World
        for field, code in (('verification_receipt_sha256', 'resolution_policy_receipt_mismatch'),
                            ('occurrence_id', 'resolution_policy_receipt_mismatch'),
                            ('legacy_binding_sha256', 'resolution_policy_receipt_mismatch')):
            with self.subTest(field=field):
                case = World(); case.setUp(); self.addCleanup(case.doCleanups)
                # Corrupt both payload and columns consistently with valid digests;
                # occurrence FK is deferred only during this synthetic construction.
                if field == 'occurrence_id':
                    with store.ledger(case.db) as con:
                        con.execute("INSERT INTO occurrences SELECT ?,original_sha256,kind,source_ref,"
                                    "parent_occurrence_id,acquired_at,acquisition_method,evidence FROM occurrences WHERE id=?",
                                    ('0' * 64, case.oid))
                        con.commit()
                case.raw_row(payload={'policy_sha256': case.w.policy.sha256, field: '0' * 64})
                with self.assertRaisesRegex(ValueError, '^' + code + '$'):
                    case.effective()

    def test_reader_rechecks_legacy_row_after_successful_append(self):
        self.w.enroll()
        with store.ledger(self.w.database) as con:
            con.execute('BEGIN IMMEDIATE')
            triggers = con.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name='legacy_rows'").fetchall()
            for name, _ in triggers:
                con.execute('DROP TRIGGER "' + name + '"')
            con.execute("UPDATE legacy_rows SET payload_sha256=? WHERE source_table='docs'", ('0' * 64,))
            for _, sql in triggers:
                con.execute(sql)
            with self.assertRaisesRegex(ValueError, '^original_resolution_import_binding_mismatch$'):
                self.w.read(con)
            con.rollback()

    def test_every_resolution_trigger_missing_or_replaced_is_loud(self):
        with store.ledger(self.w.database) as con:
            names = [row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'original_resolution_%'")]
        self.assertEqual(len(names), 6)
        for name in names:
            for replacement in (False, True):
                with self.subTest(name=name, replacement=replacement), store.ledger(self.w.database) as con:
                    con.execute('BEGIN IMMEDIATE')
                    con.execute('DROP TRIGGER "' + name + '"')
                    if replacement:
                        table = 'ledger_meta' if 'marker' in name else 'original_resolutions'
                        con.execute('CREATE TRIGGER "' + name + '" BEFORE UPDATE ON ' + table + ' BEGIN SELECT 1; END')
                    for check in (resolution.installed, resolution.ensure_schema):
                        with self.assertRaisesRegex(ValueError, '^original_resolution_schema_mismatch$'):
                            check(con)
                    con.rollback()

    def test_policy_all_state_is_frozen_and_reinitialisation_refused(self):
        p = self.w.policy
        raw, sha = p.raw, p.sha256
        for name in ('raw', 'sha256', '_raw', '_sha256', '_entries'):
            with self.subTest(name=name):
                with self.assertRaises(AttributeError):
                    setattr(p, name, None)
                with self.assertRaises(AttributeError):
                    delattr(p, name)
        with self.assertRaises(TypeError):
            p._entries[self.w.sha]['bytes'] = 100
        with self.assertRaisesRegex(ValueError, '^resolution_policy_invalid$'):
            p.__init__(raw, expected_sha256=sha)
        self.assertEqual((p.raw, p.sha256), (raw, sha))
        self.assertFalse(self.w.enroll()[1])

    def test_append_failure_is_not_committable_and_outer_work_survives(self):
        with store.ledger(self.w.database) as con:
            con.execute('BEGIN IMMEDIATE')
            con.execute('CREATE TABLE synthetic_outer_work(value)')
            con.execute('INSERT INTO synthetic_outer_work VALUES(1)')
            with patch.object(resolution, 'effective_original', side_effect=ValueError('synthetic_self_check')):
                with self.assertRaisesRegex(ValueError, '^synthetic_self_check$'):
                    self.w.append(con)
            self.assertEqual(con.execute('SELECT count(*) FROM original_resolutions').fetchone()[0], 0)
            self.assertEqual(con.execute('SELECT value FROM synthetic_outer_work').fetchone()[0], 1)
            self.assertEqual(con.execute('SELECT original_resolution_append_allowed(1)').fetchone()[0], 0)
            con.commit()  # Catch-and-commit cannot persist the rejected row.
        self.assertEqual(self.w.fixture.rows('SELECT count(*) FROM original_resolutions'), [(0,)])
        self.assertFalse(self.w.enroll()[1])

    def test_identity_text_length_boundary_preserves_valid_unicode(self):
        for length, valid in ((1, True), (1024, True), (1025, False)):
            with self.subTest(length=length), store.ledger(self.w.database) as con:
                con.execute('BEGIN IMMEDIATE')
                source = dict(self.w.source, folder='é' * length)
                con.execute('UPDATE occurrences SET source_ref=? WHERE id=?', (store.canonical(source), self.w.oid))
                if valid:
                    self.assertFalse(self.w.append(con)[1])
                else:
                    with self.assertRaisesRegex(ValueError, '^original_resolution_mail_occurrence_unbound$'):
                        self.w.append(con)
                con.rollback()

    def test_canonical_stored_payload_whitespace_refused_without_trigger_masking(self):
        self.w.enroll()
        with store.ledger(self.w.database) as con:
            con.execute('BEGIN IMMEDIATE')
            sql = con.execute("SELECT sql FROM sqlite_master WHERE name='original_resolution_update'").fetchone()[0]
            con.execute('DROP TRIGGER original_resolution_update')
            con.execute("UPDATE original_resolutions SET payload_json=' ' || payload_json")
            con.execute(sql)
            with self.assertRaisesRegex(ValueError, '^original_resolution_payload_mismatch$'):
                self.w.read(con)
            con.rollback()
