"""Initial canonical schema and immutable legacy-import evidence."""
VERSION = 1
SQL = r"""
CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, checksum TEXT NOT NULL);
CREATE TABLE ledger_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE originals(
 sha256 TEXT PRIMARY KEY CHECK(length(sha256)=64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
 bytes INTEGER NOT NULL CHECK(bytes>=0), mime_detected TEXT, first_seen_at TEXT,
 role TEXT NOT NULL, scope TEXT NOT NULL, storage_path TEXT,
 preservation_status TEXT NOT NULL, legacy_format TEXT, provenance_json TEXT NOT NULL);
CREATE TABLE occurrences(
 id TEXT PRIMARY KEY, original_sha256 TEXT NOT NULL REFERENCES originals(sha256),
 kind TEXT NOT NULL CHECK(kind IN ('mail','attachment','portal','archive','local')),
 source_ref TEXT NOT NULL, parent_occurrence_id TEXT REFERENCES occurrences(id),
 acquired_at TEXT, acquisition_method TEXT NOT NULL, evidence TEXT NOT NULL);
CREATE TABLE mail_messages(
 id TEXT PRIMARY KEY, account TEXT NOT NULL, folder TEXT NOT NULL,
 uidvalidity INTEGER NOT NULL CHECK(uidvalidity>0), uid INTEGER NOT NULL CHECK(uid>0),
 message_id TEXT, eml_sha256 TEXT NOT NULL REFERENCES originals(sha256),
 headers TEXT NOT NULL, received_at TEXT, account_provenance_status TEXT NOT NULL,
 UNIQUE(account,folder,uidvalidity,uid));
CREATE TABLE portal_items(
 id TEXT PRIMARY KEY, portal_host TEXT NOT NULL, request_id TEXT NOT NULL,
 item_id TEXT NOT NULL, agency_id TEXT, title TEXT, notice_occurrence_id TEXT REFERENCES occurrences(id),
 retrieval_status TEXT NOT NULL, last_attempt_at TEXT, attempts INTEGER NOT NULL DEFAULT 0,
 last_error TEXT, original_sha256 TEXT REFERENCES originals(sha256),
 UNIQUE(portal_host,request_id,item_id));
CREATE TABLE units(
 id TEXT PRIMARY KEY, original_sha256 TEXT NOT NULL REFERENCES originals(sha256),
 parser TEXT, parser_version TEXT, locator TEXT NOT NULL, unit_type TEXT,
 text_sha256 TEXT, derived_path TEXT NOT NULL, status TEXT NOT NULL,
 legacy_ordinal INTEGER, provenance_json TEXT NOT NULL);
CREATE TABLE page_state(
 original_sha256 TEXT NOT NULL REFERENCES originals(sha256), page_no INTEGER NOT NULL CHECK(page_no>0),
 method TEXT, method_version TEXT, derivative_sha256 TEXT, confidence REAL,
 needs_visual_review INTEGER NOT NULL CHECK(needs_visual_review IN (0,1)),
 status TEXT NOT NULL CHECK(status IN ('ok','partial','blocked','inapplicable')), reason TEXT,
 PRIMARY KEY(original_sha256,page_no));
CREATE TABLE agencies(id TEXT PRIMARY KEY, name TEXT NOT NULL, jurisdiction TEXT, portal_host TEXT);
CREATE TABLE requests(
 id TEXT PRIMARY KEY, agency_id TEXT NOT NULL REFERENCES agencies(id), external_ref TEXT,
 submitted_at TEXT, determination_due_at TEXT, determination_at TEXT, production_at TEXT,
 status TEXT NOT NULL, UNIQUE(agency_id,external_ref));
CREATE TABLE joins(
 id TEXT PRIMARY KEY, original_sha256 TEXT NOT NULL REFERENCES originals(sha256),
 agency_id TEXT REFERENCES agencies(id), request_id TEXT REFERENCES requests(id),
 join_type TEXT NOT NULL, evidence TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('typed','hinted','blocked')), blocked_reason TEXT);
CREATE TABLE receipts(
 sha256 TEXT PRIMARY KEY, stage TEXT NOT NULL, subject_sha256 TEXT NOT NULL REFERENCES originals(sha256),
 reviewer_id TEXT NOT NULL, role TEXT NOT NULL, verdict TEXT NOT NULL,
 coverage TEXT NOT NULL, locators TEXT NOT NULL, rationale TEXT NOT NULL,
 model_or_tool TEXT NOT NULL, input_hashes TEXT NOT NULL, created_at_tz TEXT NOT NULL,
 supersedes TEXT REFERENCES receipts(sha256), path TEXT NOT NULL,
 UNIQUE(sha256,stage,subject_sha256));
CREATE TABLE runs(
 run_id TEXT PRIMARY KEY, kind TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT,
 engine_version TEXT NOT NULL, image_digest TEXT, config_sha256 TEXT,
 coverage_cutoff TEXT, status TEXT NOT NULL, summary TEXT NOT NULL);
CREATE TABLE stage_state(
 original_sha256 TEXT NOT NULL REFERENCES originals(sha256),
 stage TEXT NOT NULL CHECK(stage IN ('preserve','extract','catalog','detect','review','compare','privacy')),
 status TEXT NOT NULL CHECK(status IN ('pending','in_progress','done','blocked','inapplicable')),
 receipt_sha256 TEXT, owner TEXT NOT NULL, updated_at TEXT NOT NULL,
 run_id TEXT NOT NULL REFERENCES runs(run_id), reason TEXT,
 PRIMARY KEY(original_sha256,stage),
 FOREIGN KEY(receipt_sha256,stage,original_sha256) REFERENCES receipts(sha256,stage,subject_sha256),
 CHECK(status!='done' OR receipt_sha256 IS NOT NULL),
 CHECK(status!='inapplicable' OR (reason IS NOT NULL AND length(reason)>0)));
CREATE TABLE stage_events(
 sequence INTEGER PRIMARY KEY AUTOINCREMENT, original_sha256 TEXT NOT NULL,
 stage TEXT NOT NULL, status TEXT NOT NULL, receipt_sha256 TEXT, owner TEXT NOT NULL,
 updated_at TEXT NOT NULL, run_id TEXT NOT NULL REFERENCES runs(run_id), reason TEXT);
CREATE TRIGGER stage_insert_history AFTER INSERT ON stage_state BEGIN
 INSERT INTO stage_events(original_sha256,stage,status,receipt_sha256,owner,updated_at,run_id,reason)
 VALUES(NEW.original_sha256,NEW.stage,NEW.status,NEW.receipt_sha256,NEW.owner,NEW.updated_at,NEW.run_id,NEW.reason);
END;
CREATE TRIGGER stage_update_history AFTER UPDATE ON stage_state BEGIN
 INSERT INTO stage_events(original_sha256,stage,status,receipt_sha256,owner,updated_at,run_id,reason)
 VALUES(NEW.original_sha256,NEW.stage,NEW.status,NEW.receipt_sha256,NEW.owner,NEW.updated_at,NEW.run_id,NEW.reason);
END;
CREATE TRIGGER preserve_never_reopened BEFORE UPDATE ON stage_state
 WHEN OLD.stage='preserve' AND OLD.status='done'
 BEGIN SELECT RAISE(ABORT,'accepted preservation is immutable'); END;
CREATE TRIGGER stage_slot_immutable BEFORE UPDATE ON stage_state
 WHEN OLD.original_sha256!=NEW.original_sha256 OR OLD.stage!=NEW.stage
 BEGIN SELECT RAISE(ABORT,'stage identity is immutable'); END;
CREATE TRIGGER stage_no_replace BEFORE INSERT ON stage_state
 WHEN EXISTS(SELECT 1 FROM stage_state WHERE original_sha256=NEW.original_sha256 AND stage=NEW.stage)
 BEGIN SELECT RAISE(ABORT,'stage slots cannot be replaced'); END;
CREATE TRIGGER stage_no_delete BEFORE DELETE ON stage_state
 BEGIN SELECT RAISE(ABORT,'stage history cannot be deleted'); END;
CREATE TABLE digests(
 id TEXT PRIMARY KEY, original_sha256 TEXT, author_id TEXT, coverage_declared TEXT,
 denominator TEXT NOT NULL, path TEXT NOT NULL, sha256 TEXT NOT NULL,
 supersedes TEXT REFERENCES digests(id), reference_status TEXT NOT NULL);
CREATE TABLE detector_runs(
 run_id TEXT PRIMARY KEY, detector TEXT NOT NULL, detector_version TEXT NOT NULL,
 rules_version TEXT NOT NULL, started_at TEXT NOT NULL, eligible INTEGER NOT NULL,
 evaluated INTEGER NOT NULL, skipped INTEGER NOT NULL, blocked INTEGER NOT NULL, manifest_sha256 TEXT NOT NULL);
CREATE TABLE detector_hits(
 id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES detector_runs(run_id),
 original_sha256 TEXT NOT NULL REFERENCES originals(sha256), locators TEXT NOT NULL,
 severity INTEGER NOT NULL, observed_value_private TEXT NOT NULL,
 applicability TEXT NOT NULL, dedupe_key TEXT NOT NULL UNIQUE);
CREATE TABLE rules(
 id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('statute','policy','contract')),
 authority TEXT NOT NULL, citation TEXT NOT NULL, effective_from TEXT, effective_to TEXT,
 source_sha256 TEXT NOT NULL, clause TEXT NOT NULL, applicability TEXT NOT NULL,
 version TEXT NOT NULL);
CREATE TABLE comparisons(
 id TEXT PRIMARY KEY, original_sha256 TEXT NOT NULL REFERENCES originals(sha256),
 rule_id TEXT NOT NULL REFERENCES rules(id), rule_version TEXT NOT NULL,
 observation TEXT NOT NULL, expected TEXT NOT NULL, classification TEXT NOT NULL,
 evidence TEXT NOT NULL, counterevidence TEXT NOT NULL, next_action TEXT, reviewer_id TEXT NOT NULL);
CREATE TABLE proposals(
 id TEXT PRIMARY KEY, tier TEXT NOT NULL CHECK(tier IN ('A','B')), agency_id TEXT REFERENCES agencies(id),
 public_content_sha256 TEXT NOT NULL, manifest_sha256 TEXT NOT NULL,
 privacy_receipt_sha256 TEXT REFERENCES receipts(sha256), independent_receipt_sha256 TEXT REFERENCES receipts(sha256),
 owner_approval TEXT NOT NULL CHECK(owner_approval IN ('none','approved','rejected')),
 approved_content_sha256 TEXT, supersedes TEXT REFERENCES proposals(id),
 CHECK(owner_approval!='approved' OR (approved_content_sha256 IS NOT NULL AND approved_content_sha256=public_content_sha256)));
CREATE TABLE publications(
 id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL REFERENCES proposals(id), site_pr_ref TEXT,
 deployed_version TEXT, published_at TEXT, rollback_ref TEXT);
CREATE TABLE work_leases(
 item_key TEXT PRIMARY KEY, stage TEXT NOT NULL, owner TEXT NOT NULL,
 leased_at TEXT NOT NULL, expires_at TEXT NOT NULL, attempts INTEGER NOT NULL,
 last_error TEXT, next_eligible_at TEXT);
CREATE TABLE alerts(
 id TEXT PRIMARY KEY, key TEXT NOT NULL UNIQUE, first_seen TEXT NOT NULL,
 last_seen TEXT NOT NULL, count INTEGER NOT NULL, owner TEXT NOT NULL, state TEXT NOT NULL);
CREATE TABLE legacy_imports(
 id TEXT PRIMARY KEY, source_database_sha256 TEXT NOT NULL, source_manifest_sha256 TEXT NOT NULL,
 importer_version TEXT NOT NULL, importer_sha256 TEXT NOT NULL, captured_database TEXT NOT NULL,
 status TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
 declared_table_counts TEXT NOT NULL, config_sha256 TEXT NOT NULL,
 UNIQUE(source_database_sha256,source_manifest_sha256,importer_version,importer_sha256,config_sha256));
CREATE TABLE legacy_rows(
 import_id TEXT NOT NULL REFERENCES legacy_imports(id), source_table TEXT NOT NULL,
 source_rowid INTEGER NOT NULL, identity_json TEXT NOT NULL,
 payload_json TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
 PRIMARY KEY(import_id,source_table,source_rowid));
CREATE TABLE legacy_checkpoints(
 import_id TEXT NOT NULL REFERENCES legacy_imports(id), source_table TEXT NOT NULL,
 last_rowid INTEGER NOT NULL, copied_rows INTEGER NOT NULL,
 PRIMARY KEY(import_id,source_table));
CREATE TABLE import_gaps(
 id TEXT PRIMARY KEY, import_id TEXT NOT NULL REFERENCES legacy_imports(id),
 subject_sha256 TEXT, category TEXT NOT NULL, reason TEXT NOT NULL,
 owner TEXT NOT NULL, status TEXT NOT NULL);
CREATE INDEX units_original ON units(original_sha256);
CREATE INDEX stage_status ON stage_state(stage,status);
CREATE INDEX occurrences_original ON occurrences(original_sha256);
CREATE INDEX legacy_source ON legacy_rows(import_id,source_table);
"""
IMMUTABLE_KEYS = {
    "originals": ("sha256",),
    "receipts": ("sha256",),
    "stage_events": ("sequence",),
    "legacy_rows": ("import_id", "source_table", "source_rowid"),
}
for table, keys in IMMUTABLE_KEYS.items():
    predicate = " AND ".join(f"{key}=NEW.{key}" for key in keys)
    SQL += (f"CREATE TRIGGER immutable_{table}_insert BEFORE INSERT ON {table} "
            f"WHEN EXISTS(SELECT 1 FROM {table} WHERE {predicate}) "
            "BEGIN SELECT RAISE(ABORT,'immutable evidence cannot be replaced'); END;\n")
for table in IMMUTABLE_KEYS:
    for operation in ("UPDATE", "DELETE"):
        SQL += (f"CREATE TRIGGER immutable_{table}_{operation.lower()} BEFORE {operation} ON {table} "
                "BEGIN SELECT RAISE(ABORT,'immutable evidence'); END;\n")
