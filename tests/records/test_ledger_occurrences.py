"""Synthetic source-bound projection; no original bytes or live services."""
import hashlib
import json
import sqlite3
import unittest

from campaign_tool.records.ledger.occurrence_projection import decode_source, mime_path, project_occurrences
from campaign_tool.records.ledger.store import counts, import_legacy, ledger
from tests.records import test_ledger


class OccurrenceProjectionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_ledger.LedgerTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.snapshot = self.fixture.snapshot
        self.database = self.fixture.output
        self.sha = self.fixture.sha
        self.fs_receipt = {"mtime_ns": 1, "ctime_ns": 1, "device": 1, "inode": 2,
                           "observed_at": "2000-01-01T00:00:00+00:00", "received_at": None}
        with sqlite3.connect(self.snapshot / "intake.sqlite") as con:
            con.execute("UPDATE occurrences SET locator=?,receipt=?",
                        (json.dumps(None), json.dumps(self.fs_receipt)))

    def capture(self, **kwargs):
        self.fixture.refresh_manifest()
        return import_legacy(self.snapshot, self.database, **kwargs)["import_id"]

    def add_doc(self, label, form="txt"):
        sha = hashlib.sha256(label.encode()).hexdigest()
        with sqlite3.connect(self.snapshot / "intake.sqlite") as con:
            con.execute("INSERT INTO docs(sha,bytes,format,stage,first_seen,review_status,version,digest) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (sha, len(label), form, "complete", "2000-01-01T00:00:00Z", "full", "synthetic", "{}"))
        return sha

    def add_occurrence(self, oid, sha, *, receipt=None, parent=None, locator=None):
        with sqlite3.connect(self.snapshot / "intake.sqlite") as con:
            con.execute("INSERT INTO occurrences(oid,sha,path,root,parent,locator,class,receipt,first_seen,last_seen) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (oid, sha, oid + ".txt", "synthetic-root", parent, json.dumps(locator), "hint-only",
                         json.dumps(receipt if receipt is not None else self.fs_receipt),
                         "2000-01-01T00:00:00Z", "2000-01-01T00:00:00Z"))

    def mail(self, label="synthetic message", oid="message", uid=7):
        sha = self.add_doc(label, "eml")
        receipt = {"mail_identity": {"account": "synthetic-account", "folder": "INBOX", "uidvalidity": 1, "uid": uid},
                   "eml_sha256": sha}
        self.add_occurrence(oid, sha, receipt=receipt)
        return sha

    def attachment(self, parent, locator, *, parent_oid="message", relation="decoded_mime_payload"):
        sha = self.add_doc("synthetic attachment")
        receipt = {"relation": relation, "parent_sha256": parent}
        if parent_oid is not None:
            receipt["parent_legacy_oid"] = parent_oid
        self.add_occurrence("attachment", sha, receipt=receipt, parent=parent, locator=locator)
        return sha

    def test_local_projection_preserves_payload_and_stage_holds(self):
        import_id = self.capture()
        before = counts(self.database)
        with ledger(self.database, readonly=True) as con:
            events = con.execute("SELECT COUNT(*) FROM stage_events").fetchone()[0]
            source = tuple(con.execute("SELECT payload_sha256,payload_json FROM legacy_rows "
                                       "WHERE import_id=? AND source_table='occurrences'", (import_id,)).fetchone())
        result = project_occurrences(self.database, import_id)
        self.assertEqual(result["canonical_kinds"], {"local": 1})
        self.assertEqual(result["stage_promotions"], 0)
        self.assertFalse(result["publication_ready"])
        with ledger(self.database, readonly=True) as con:
            saved = tuple(con.execute("SELECT payload_sha256,payload_json FROM occurrence_projection_rows").fetchone())
            self.assertEqual(saved, source)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM stage_events").fetchone()[0], events)
            self.assertIsNone(con.execute("SELECT acquired_at FROM occurrences").fetchone()[0])
            self.assertEqual(tuple(con.execute("SELECT agency_id,request_id,status FROM joins").fetchone()), (None, None, "blocked"))
        after = counts(self.database)
        self.assertEqual(before["stages"], after["stages"])
        self.assertEqual(before["acceptance"], after["acceptance"])
        self.assertIsNone(after["end_to_end_complete"])

    def test_replay_adds_no_occurrences_rows_or_gaps(self):
        import_id = self.capture()
        first = project_occurrences(self.database, import_id)
        before = counts(self.database)
        second = project_occurrences(self.database, import_id)
        self.assertTrue(second["reused"])
        self.assertEqual(first["projection_id"], second["projection_id"])
        self.assertEqual(before, counts(self.database))
        with ledger(self.database, readonly=True) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM occurrence_projection_rows").fetchone()[0], 1)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM joins").fetchone()[0], 1)

    def test_new_capture_reuses_identity_and_retains_both_source_payloads(self):
        first_import = self.capture()
        first = project_occurrences(self.database, first_import)
        with sqlite3.connect(self.snapshot / "intake.sqlite") as con:
            con.execute("UPDATE occurrences SET last_seen='2000-01-02T00:00:00Z'")
        second_import = self.capture()
        second = project_occurrences(self.database, second_import)
        self.assertNotEqual(first["projection_id"], second["projection_id"])
        with ledger(self.database, readonly=True) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM occurrences").fetchone()[0], 1)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM occurrence_projection_rows").fetchone()[0], 2)
            self.assertEqual(con.execute("SELECT COUNT(DISTINCT payload_sha256) FROM occurrence_projection_rows").fetchone()[0], 2)

    def test_explicit_mail_parent_and_hierarchical_attachment(self):
        parent = self.mail()
        self.attachment(parent, {"mime": "1.2"})
        result = project_occurrences(self.database, self.capture())
        self.assertEqual(result["canonical_kinds"], {"local": 1, "mail": 1, "attachment": 1})
        with ledger(self.database, readonly=True) as con:
            child = con.execute("SELECT source_ref,parent_occurrence_id FROM occurrences WHERE kind='attachment'").fetchone()
            self.assertEqual(json.loads(child[0])["mime_part_path"], "1.2")
            self.assertEqual(con.execute("SELECT kind FROM occurrences WHERE id=?", (child[1],)).fetchone()[0], "mail")

    def test_traversal_index_is_not_coerced_to_hierarchical_path(self):
        parent = self.mail()
        self.attachment(parent, {"mime_part_index": 2})
        result = project_occurrences(self.database, self.capture())
        self.assertNotIn("attachment", result["canonical_kinds"])
        self.assertEqual(result["unresolved_reasons"]["mime_traversal_index_not_hierarchical"], 1)

    def test_parent_hash_alone_does_not_select_a_message_occurrence(self):
        parent = self.mail()
        self.attachment(parent, {"mime": "1.2"}, parent_oid=None)
        result = project_occurrences(self.database, self.capture())
        self.assertEqual(result["unresolved_reasons"]["parent_occurrence_identity_missing"], 1)

    def test_filesystem_eml_is_not_invented_as_native_mail(self):
        parent = self.add_doc("filesystem email", "eml")
        self.add_occurrence("message", parent)
        self.attachment(parent, {"mime": "1.2"})
        result = project_occurrences(self.database, self.capture())
        self.assertEqual(result["canonical_kinds"], {"local": 2})
        self.assertEqual(result["unresolved_reasons"]["typed_mail_parent_not_established"], 1)

    def test_reserialized_message_remains_unresolved(self):
        parent = self.mail()
        self.attachment(parent, {"mime": "1.2", "message": 0}, relation="reserialized_rfc822")
        result = project_occurrences(self.database, self.capture())
        self.assertEqual(result["unresolved_reasons"]["reserialized_message_not_original_attachment_octets"], 1)

    def test_invalid_mime_paths_and_mixed_conventions(self):
        for value in (0, True, "0", "2", "1.0", "1.01", "1..2", "1.2 ", "1/2"):
            with self.subTest(value=value):
                self.assertIsNotNone(mime_path({"mime": value}, {})[1])
        self.assertEqual(mime_path({"mime": "1.2", "mime_part_index": 3}, {})[1], "ambiguous_mime_conventions")
        self.assertEqual(mime_path({"mime": "1.2"}, {"mime_convention": "traversal_index"})[1], "unsupported_mime_convention")
        self.assertEqual(mime_path({"mime": "1.10.2"}, {}), ("1.10.2", None))

    def test_invalid_mail_uid_does_not_create_mail(self):
        sha = self.add_doc("invalid mail", "eml")
        self.add_occurrence("invalid-mail", sha, receipt={
            "mail_identity": {"account": "synthetic", "folder": "INBOX", "uidvalidity": 1, "uid": True},
            "eml_sha256": sha})
        result = project_occurrences(self.database, self.capture())
        self.assertNotIn("mail", result["canonical_kinds"])
        self.assertEqual(result["unresolved_reasons"]["invalid_or_unbound_mail_identity"], 1)

    def test_conflicting_native_identity_rolls_back_projection(self):
        self.mail("first message", oid="first")
        self.mail("different message", oid="second")
        import_id = self.capture()
        with self.assertRaisesRegex(ValueError, "conflicting_native"):
            project_occurrences(self.database, import_id)
        with ledger(self.database, readonly=True) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM occurrences").fetchone()[0], 0)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM joins").fetchone()[0], 0)

    def test_missing_original_is_a_bound_gap_not_a_fabricated_original(self):
        unknown = hashlib.sha256(b"unregistered synthetic original").hexdigest()
        self.add_occurrence("unregistered", unknown)
        result = project_occurrences(self.database, self.capture())
        self.assertEqual(result["unresolved_reasons"]["original_not_in_canonical_ledger"], 1)
        self.assertEqual(counts(self.database)["originals"], 1)

    def test_scope_excluded_source_is_retained_without_canonical_occurrence(self):
        with sqlite3.connect(self.snapshot / "intake.sqlite") as con:
            con.execute("INSERT INTO scope_exclusions(path,sha,reason) VALUES(?,?,?)",
                        ("synthetic.txt", self.sha, "synthetic scope exclusion"))
        result = project_occurrences(self.database, self.capture())
        self.assertEqual(result["scope_excluded_source_rows"], 1)
        self.assertEqual(result["canonical_occurrences_in_projection"], 0)
        with ledger(self.database, readonly=True) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM occurrence_projection_rows").fetchone()[0], 1)

    def test_incomplete_capture_is_not_projected(self):
        import_id = self.capture(max_batches=1)
        with self.assertRaisesRegex(ValueError, "completed_capture"):
            project_occurrences(self.database, import_id)

    def test_payload_hash_and_source_identity_are_checked(self):
        payload = json.dumps({"oid": "synthetic", "sha": self.sha})
        source = {"payload_json": payload, "payload_sha256": "0" * 64,
                  "identity_json": json.dumps({"oid": "synthetic", "sha": self.sha})}
        with self.assertRaisesRegex(ValueError, "payload_hash"):
            decode_source(source)
        source["payload_sha256"] = hashlib.sha256(payload.encode()).hexdigest()
        source["identity_json"] = json.dumps({"oid": "other", "sha": self.sha})
        with self.assertRaisesRegex(ValueError, "identity_mismatch"):
            decode_source(source)

    def test_invalid_projection_limits_and_import_id(self):
        import_id = self.capture()
        for value in (0, True, 20001):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "row_limit"):
                project_occurrences(self.database, import_id, max_rows=value)
        with self.assertRaisesRegex(ValueError, "import_id"):
            project_occurrences(self.database, "missing")

    def test_projection_source_records_are_immutable_including_replace(self):
        project_occurrences(self.database, self.capture())
        with ledger(self.database) as con:
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute("UPDATE occurrence_projection_rows SET reason='changed'")
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute("INSERT OR REPLACE INTO occurrence_projection_rows SELECT * FROM occurrence_projection_rows")
