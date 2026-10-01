"""Synthetic-only ledger migration and checkpoint regressions."""
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from campaign_tool.records.ledger.store import STAGES, counts, import_legacy, initialize, ledger


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.root.chmod(0o700)
        self.snapshot = self.root / "source"
        self.snapshot.mkdir(mode=0o700)
        self.output = self.root / "candidate" / "ledger.sqlite"
        self.sha = hashlib.sha256(b"synthetic original").hexdigest()
        con = sqlite3.connect(self.snapshot / "intake.sqlite")
        con.executescript('''
        CREATE TABLE docs(sha TEXT PRIMARY KEY,bytes INTEGER,format TEXT,stage TEXT,first_seen TEXT,review_status TEXT,version TEXT,digest TEXT);
        CREATE TABLE occurrences(oid TEXT,sha TEXT,path TEXT,root TEXT,parent TEXT,locator TEXT,class TEXT,receipt TEXT,first_seen TEXT,last_seen TEXT,PRIMARY KEY(oid,sha));
        CREATE TABLE units(sha TEXT,ordinal INTEGER,kind TEXT,locator TEXT,text TEXT,data TEXT,PRIMARY KEY(sha,ordinal));
        CREATE TABLE preservations(sha TEXT PRIMARY KEY,path TEXT,verified_at TEXT,bytes INTEGER);
        CREATE TABLE extraction_attempts(id TEXT PRIMARY KEY,sha TEXT);
        CREATE TABLE edges(parent TEXT,locator TEXT,child TEXT,name TEXT,PRIMARY KEY(parent,locator));
        CREATE TABLE edge_history(parent TEXT,locator TEXT,child TEXT,name TEXT);
        CREATE TABLE document_history(sha TEXT,record TEXT);
        CREATE TABLE events(run TEXT,stage TEXT,code TEXT);
        CREATE TABLE runs(id TEXT PRIMARY KEY,kind TEXT);
        CREATE TABLE scope_exclusions(path TEXT,sha TEXT,reason TEXT,PRIMARY KEY(path,sha));
        CREATE TABLE seen(run TEXT,oid TEXT,sha TEXT,PRIMARY KEY(run,oid));
        CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);
        ''')
        con.execute("INSERT INTO docs VALUES(?,?,?,?,?,?,?,?)", (self.sha,18,"txt","complete","2000-01-01T00:00:00Z","full","legacy-v1","declared"))
        con.execute("INSERT INTO occurrences VALUES(?,?,?,?,?,?,?,?,?,?)", ("delivery",self.sha,"synthetic.txt","fixture",None,"file","test",None,None,None))
        con.execute("INSERT INTO units VALUES(?,?,?,?,?,?)", (self.sha,0,"text",'{"line":1}',"synthetic text",'{}'))
        con.execute("INSERT INTO meta VALUES('synthetic','true')")
        con.commit()
        con.close()
        self.refresh_manifest()

    def tearDown(self):
        self.tmp.cleanup()

    def refresh_manifest(self):
        digest = hashlib.sha256((self.snapshot / "intake.sqlite").read_bytes()).hexdigest()
        (self.snapshot / "input-manifest.json").write_text(json.dumps({"database_snapshot_sha256": digest}))

    def test_empty_ledger_is_candidate_not_accepted(self):
        initialize(self.output)
        result = counts(self.output)
        self.assertEqual(result["originals"],0)
        self.assertIsNone(result["end_to_end_complete"])
        self.assertEqual(result["acceptance"],"candidate_unreconciled")

    def test_import_keeps_declarations_without_stage_promotion(self):
        result = import_legacy(self.snapshot,self.output)
        self.assertEqual(result["status"],"captured")
        observed = counts(self.output)
        self.assertEqual(observed["originals"],1)
        self.assertEqual(observed["stage_slots_observed"],7)
        for stage in STAGES:
            self.assertEqual(observed["stages"][stage]["pending"],1)
            self.assertEqual(observed["stages"][stage]["done"],0)
        self.assertEqual(observed["legacy_rows"]["units"],1)
        self.assertEqual(observed["canonical_units"],0)
        with ledger(self.output,readonly=True) as con:
            declaration = json.loads(con.execute("SELECT payload_json FROM legacy_rows WHERE source_table='docs'").fetchone()[0])
            self.assertEqual(declaration["review_status"],"full")

    def test_replay_is_idempotent(self):
        import_legacy(self.snapshot,self.output)
        first = counts(self.output)
        result = import_legacy(self.snapshot,self.output)
        self.assertTrue(result["reused"])
        self.assertEqual(counts(self.output),first)

    def test_checkpoint_resumes_without_duplicate_rows(self):
        result = import_legacy(self.snapshot,self.output,batch_size=1,max_batches=1)
        self.assertEqual(result["status"],"checkpointed")
        self.assertEqual(counts(self.output)["incomplete_imports"],1)
        result = import_legacy(self.snapshot,self.output,batch_size=1)
        self.assertEqual(result["status"],"captured")
        self.assertEqual(counts(self.output)["legacy_rows"],{"docs":1,"occurrences":1,"units":1,"meta":1})

    def test_changed_manifest_cannot_resume_active_run(self):
        import_legacy(self.snapshot,self.output,max_batches=1)
        path = self.snapshot / "input-manifest.json"
        manifest = json.loads(path.read_text())
        manifest["changed"] = True
        path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError,"different unfinished"):
            import_legacy(self.snapshot,self.output)

    def test_incorrect_snapshot_hash_refused(self):
        (self.snapshot / "input-manifest.json").write_text(json.dumps({"database_snapshot_sha256":"0"*64}))
        with self.assertRaisesRegex(ValueError,"hash mismatch"):
            import_legacy(self.snapshot,self.output)
        self.assertFalse(self.output.exists())

    def test_immutable_evidence_rejects_updates(self):
        import_legacy(self.snapshot,self.output)
        with ledger(self.output) as con:
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute("UPDATE originals SET bytes=1")
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute("DELETE FROM legacy_rows")

    def test_done_requires_bound_receipt(self):
        import_legacy(self.snapshot,self.output)
        with ledger(self.output) as con:
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute("UPDATE stage_state SET status='done' WHERE stage='review'")
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute("UPDATE stage_state SET status='inapplicable',reason=NULL WHERE stage='review'")

    def test_stage_changes_append_history(self):
        import_legacy(self.snapshot,self.output)
        with ledger(self.output) as con:
            con.execute("UPDATE stage_state SET status='blocked',reason='synthetic gap' WHERE stage='review'")
            self.assertEqual(con.execute("SELECT COUNT(*) FROM stage_events").fetchone()[0],8)
            con.commit()
        self.assertEqual(counts(self.output)["stages"]["review"]["blocked"],1)

    def test_symlink_output_refused(self):
        self.output.parent.mkdir(mode=0o700)
        self.output.symlink_to(self.root / "not-created")
        with self.assertRaisesRegex(ValueError,"symlink"):
            import_legacy(self.snapshot,self.output)

    def test_group_readable_output_parent_refused(self):
        self.output.parent.mkdir(mode=0o755)
        self.output.parent.chmod(0o755)
        with self.assertRaisesRegex(ValueError,"owner-only"):
            initialize(self.output)

    def test_existing_ledger_never_overwritten(self):
        initialize(self.output)
        with self.assertRaises(FileExistsError):
            initialize(self.output)

    def test_unclassified_source_table_refused(self):
        with sqlite3.connect(self.snapshot / "intake.sqlite") as con:
            con.execute("CREATE TABLE unexpected(value TEXT)")
        self.refresh_manifest()
        with self.assertRaisesRegex(ValueError,"unexpected legacy schema"):
            import_legacy(self.snapshot,self.output)

    def test_composite_occurrence_identity_retained(self):
        other = hashlib.sha256(b"different synthetic original").hexdigest()
        with sqlite3.connect(self.snapshot / "intake.sqlite") as con:
            con.execute("INSERT INTO occurrences VALUES(?,?,?,?,?,?,?,?,?,?)", ("delivery",other,"synthetic-v2.txt","fixture",None,"file","test",None,None,None))
        self.refresh_manifest()
        import_legacy(self.snapshot,self.output)
        with ledger(self.output,readonly=True) as con:
            identities = [json.loads(row[0]) for row in con.execute("SELECT identity_json FROM legacy_rows WHERE source_table='occurrences'")]
            self.assertEqual(len(identities),2)
            self.assertEqual({row["sha"] for row in identities},{self.sha,other})

    def test_invalid_batch_size_refused(self):
        for value in (0,True,10001):
            with self.assertRaises(ValueError):
                import_legacy(self.snapshot,self.output,batch_size=value)


    def test_original_replace_refused_even_without_recursive_triggers(self):
        import_legacy(self.snapshot,self.output)
        with ledger(self.output) as con:
            con.execute("PRAGMA recursive_triggers=OFF")
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute("INSERT OR REPLACE INTO originals SELECT sha256,1,mime_detected,first_seen_at,role,scope,storage_path,preservation_status,legacy_format,provenance_json FROM originals")
            self.assertEqual(con.execute("SELECT bytes FROM originals").fetchone()[0],18)

    def test_legacy_row_and_history_replacement_refused(self):
        import_legacy(self.snapshot,self.output)
        with ledger(self.output) as con:
            con.execute("PRAGMA recursive_triggers=OFF")
            for table in ("legacy_rows", "stage_events"):
                with self.assertRaises(sqlite3.IntegrityError):
                    con.execute("INSERT OR REPLACE INTO " + table + " SELECT * FROM " + table + " LIMIT 1")

    def test_preservation_cannot_be_replaced(self):
        import_legacy(self.snapshot,self.output)
        receipt_sha = hashlib.sha256(b"synthetic preservation receipt").hexdigest()
        receipt = {"sha256":receipt_sha,"stage":"preserve","subject_sha256":self.sha,
                   "reviewer_id":"fixture","role":"preservation","verdict":"pass",
                   "coverage":"{}","locators":"[]","rationale":"synthetic constraint test",
                   "model_or_tool":"synthetic","input_hashes":"[]","created_at_tz":"2000-01-01T00:00:00Z",
                   "supersedes":None,"path":"synthetic-receipt.json"}
        with ledger(self.output) as con:
            con.execute("INSERT INTO receipts(" + ",".join(receipt) + ") VALUES(" + ",".join("?" for _ in receipt) + ")",tuple(receipt.values()))
            con.execute("UPDATE stage_state SET status='done',receipt_sha256=? WHERE stage='preserve'",(receipt_sha,))
            con.execute("PRAGMA recursive_triggers=OFF")
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute("INSERT OR REPLACE INTO stage_state SELECT original_sha256,stage,'pending',NULL,owner,updated_at,run_id,reason FROM stage_state WHERE stage='preserve'")
            self.assertEqual(con.execute("SELECT status FROM stage_state WHERE stage='preserve'").fetchone()[0],"done")
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute("INSERT OR REPLACE INTO receipts SELECT * FROM receipts")

    def test_failed_initialization_leaves_no_final_database(self):
        with patch("campaign_tool.records.ledger.store.os.link",side_effect=RuntimeError("synthetic interruption")):
            with self.assertRaises(RuntimeError):
                initialize(self.output)
        self.assertFalse(self.output.exists())
        initialize(self.output)
        self.assertEqual(counts(self.output)["originals"],0)

    def test_schema_failure_leaves_no_final_database(self):
        with patch("campaign_tool.records.ledger.store.v001.SQL","CREATE TABLE partial(value TEXT); INVALID SQL;"):
            with self.assertRaises(sqlite3.OperationalError):
                initialize(self.output)
        self.assertFalse(self.output.exists())
        initialize(self.output)
        self.assertEqual(counts(self.output)["originals"],0)

    def test_approved_proposal_requires_exact_nonnull_hash(self):
        initialize(self.output)
        with ledger(self.output) as con:
            sql = "INSERT INTO proposals(id,tier,public_content_sha256,manifest_sha256,owner_approval,approved_content_sha256) VALUES(?,?,?,?,?,?)"
            for approval in (None,hashlib.sha256(b"different public content").hexdigest()):
                with self.assertRaises(sqlite3.IntegrityError):
                    con.execute(sql,("synthetic","A",self.sha,self.sha,"approved",approval))
            con.execute(sql,("synthetic","A",self.sha,self.sha,"approved",self.sha))
            self.assertEqual(con.execute("SELECT COUNT(*) FROM proposals").fetchone()[0],1)

    def test_writer_connections_enable_replacement_protection(self):
        initialize(self.output)
        with ledger(self.output) as con:
            self.assertEqual(con.execute("PRAGMA recursive_triggers").fetchone()[0],1)
            self.assertEqual(con.execute("PRAGMA journal_mode").fetchone()[0],"wal")


if __name__ == "__main__":
    unittest.main()
