"""Additive, receipt-driven stage controller; no legacy status adoption."""

NAME = "stage_controller_v1"
TABLES = {
 "stage_artifacts": ("sha256", "payload"),
 "stage_content": ("subject_sha256", "stage", "revision", "content_sha256",
                   "author_id", "tier", "run_id", "created_at"),
 "stage_transitions": ("receipt_sha256", "subject_sha256", "stage", "revision",
                       "status", "input_receipts", "run_id", "event_sequence", "validator_id"),
 "stage_attempts": ("id", "subject_sha256", "stage", "receipt_sha256", "outcome",
                   "reason", "run_id", "created_at"),
}
SQL = """
CREATE TABLE stage_artifacts(
 sha256 TEXT PRIMARY KEY NOT NULL, payload BLOB NOT NULL);
CREATE TABLE stage_content(
 subject_sha256 TEXT NOT NULL REFERENCES originals(sha256), stage TEXT NOT NULL,
 revision INTEGER NOT NULL CHECK(revision>0),
 content_sha256 TEXT NOT NULL REFERENCES stage_artifacts(sha256),
 author_id TEXT NOT NULL, tier TEXT NOT NULL CHECK(tier IN ('A','B')),
 run_id TEXT NOT NULL REFERENCES runs(run_id), created_at TEXT NOT NULL,
 PRIMARY KEY(subject_sha256,stage,revision));
CREATE TABLE stage_transitions(
 receipt_sha256 TEXT PRIMARY KEY NOT NULL REFERENCES receipts(sha256),
 subject_sha256 TEXT NOT NULL REFERENCES originals(sha256), stage TEXT NOT NULL,
 revision INTEGER NOT NULL, status TEXT NOT NULL,
 input_receipts TEXT NOT NULL, run_id TEXT NOT NULL REFERENCES runs(run_id),
 event_sequence INTEGER NOT NULL REFERENCES stage_events(sequence),
 validator_id TEXT NOT NULL,
 FOREIGN KEY(subject_sha256,stage,revision) REFERENCES stage_content(subject_sha256,stage,revision));
CREATE TABLE stage_attempts(
 id TEXT PRIMARY KEY NOT NULL, subject_sha256 TEXT NOT NULL REFERENCES originals(sha256),
 stage TEXT NOT NULL, receipt_sha256 TEXT NOT NULL, outcome TEXT NOT NULL,
 reason TEXT NOT NULL, run_id TEXT NOT NULL REFERENCES runs(run_id), created_at TEXT NOT NULL);
CREATE INDEX stage_transition_subject ON stage_transitions(subject_sha256,stage);
CREATE INDEX stage_event_subject ON stage_events(original_sha256,stage,sequence);
"""
for _table, _columns in TABLES.items():
    for _operation in ("UPDATE", "DELETE"):
        SQL += (
            f"CREATE TRIGGER controller_{_table}_{_operation.lower()} BEFORE {_operation} ON {_table} "
            "BEGIN SELECT RAISE(ABORT,'immutable stage evidence'); END;\n")
    _key = (
        "subject_sha256=NEW.subject_sha256 AND stage=NEW.stage AND revision=NEW.revision"
        if _table == "stage_content" else f"{_columns[0]}=NEW.{_columns[0]}"
    )
    SQL += (
        f"CREATE TRIGGER controller_{_table}_insert BEFORE INSERT ON {_table} BEGIN "
        f"SELECT CASE WHEN EXISTS(SELECT 1 FROM {_table} WHERE {_key}) "
        "THEN RAISE(ABORT,'stage evidence replacement') END; "
        f"SELECT CASE WHEN stage_append_allowed('{_table}'," +
        ",".join("NEW." + column for column in _columns) +
        ")!=1 THEN RAISE(ABORT,'unauthorized stage evidence') END; END;\n")
SQL += """
CREATE TRIGGER controller_stage_update BEFORE UPDATE ON stage_state BEGIN
 SELECT CASE WHEN stage_append_allowed('stage_state',NEW.original_sha256,NEW.stage,
  NEW.status,NEW.receipt_sha256,NEW.owner,NEW.updated_at,NEW.run_id,NEW.reason)!=1
  THEN RAISE(ABORT,'stage state requires controller') END;
END;
CREATE TRIGGER controller_initial_slot BEFORE INSERT ON stage_state
 WHEN NEW.status!='pending' OR NEW.receipt_sha256 IS NOT NULL
 BEGIN SELECT RAISE(ABORT,'new stage slot must be pending'); END;
CREATE TRIGGER controller_history_binding BEFORE INSERT ON stage_events
 WHEN NOT EXISTS(SELECT 1 FROM stage_state WHERE original_sha256=NEW.original_sha256
  AND stage=NEW.stage AND status=NEW.status AND receipt_sha256 IS NEW.receipt_sha256
  AND owner=NEW.owner AND updated_at=NEW.updated_at AND run_id=NEW.run_id AND reason IS NEW.reason)
 BEGIN SELECT RAISE(ABORT,'stage history must match state'); END;
CREATE TRIGGER controller_marker_update BEFORE UPDATE ON ledger_meta
 WHEN OLD.key='extension:stage_controller_v1' OR NEW.key='extension:stage_controller_v1'
 BEGIN SELECT RAISE(ABORT,'immutable stage controller marker'); END;
CREATE TRIGGER controller_marker_delete BEFORE DELETE ON ledger_meta
 WHEN OLD.key='extension:stage_controller_v1'
 BEGIN SELECT RAISE(ABORT,'immutable stage controller marker'); END;
CREATE TRIGGER controller_marker_insert BEFORE INSERT ON ledger_meta
 WHEN NEW.key='extension:stage_controller_v1' AND EXISTS(
  SELECT 1 FROM ledger_meta WHERE key=NEW.key)
 BEGIN SELECT RAISE(ABORT,'immutable stage controller marker'); END;
"""
