"""Additive immutable runner, lease and validation-authority bindings."""
NAME = "stage_authority_v1"
TABLES = {
 "stage_runner_bindings": ("run_id","profile_id","profile_sha256","engine_version","config_sha256","test_only","created_at"),
 "stage_claims": ("claim_id","subject_sha256","stage","revision","content_sha256","input_receipts","owner","run_id","profile_sha256","leased_at","expires_at"),
 "stage_validation_authority": ("receipt_sha256","claim_id","run_id","owner","profile_sha256","test_only","validator_id"),
}
SQL = """
CREATE TABLE stage_runner_bindings(
 run_id TEXT PRIMARY KEY NOT NULL REFERENCES runs(run_id), profile_id TEXT NOT NULL,
 profile_sha256 TEXT NOT NULL, engine_version TEXT NOT NULL, config_sha256 TEXT NOT NULL,
 test_only INTEGER NOT NULL CHECK(test_only IN (0,1)), created_at TEXT NOT NULL);
CREATE TABLE stage_claims(
 claim_id TEXT PRIMARY KEY NOT NULL, subject_sha256 TEXT NOT NULL, stage TEXT NOT NULL,
 revision INTEGER NOT NULL, content_sha256 TEXT NOT NULL REFERENCES stage_artifacts(sha256),
 input_receipts TEXT NOT NULL, owner TEXT NOT NULL,
 run_id TEXT NOT NULL REFERENCES stage_runner_bindings(run_id), profile_sha256 TEXT NOT NULL,
 leased_at TEXT NOT NULL, expires_at TEXT NOT NULL,
 FOREIGN KEY(subject_sha256,stage,revision) REFERENCES stage_content(subject_sha256,stage,revision));
CREATE INDEX stage_claim_scope ON stage_claims(subject_sha256,stage,leased_at);
CREATE TABLE stage_validation_authority(
 receipt_sha256 TEXT PRIMARY KEY NOT NULL REFERENCES stage_transitions(receipt_sha256),
 claim_id TEXT NOT NULL REFERENCES stage_claims(claim_id),
 run_id TEXT NOT NULL REFERENCES stage_runner_bindings(run_id), owner TEXT NOT NULL,
 profile_sha256 TEXT NOT NULL, test_only INTEGER NOT NULL CHECK(test_only IN (0,1)),
 validator_id TEXT NOT NULL);
"""
for table, columns in TABLES.items():
 for operation in ("UPDATE","DELETE"):
  SQL += (f"CREATE TRIGGER authority_{table}_{operation.lower()} BEFORE {operation} ON {table} "
          "BEGIN SELECT RAISE(ABORT,'immutable stage authority'); END;\n")
 SQL += (f"CREATE TRIGGER authority_{table}_insert BEFORE INSERT ON {table} BEGIN "
         f"SELECT CASE WHEN EXISTS(SELECT 1 FROM {table} WHERE {columns[0]}=NEW.{columns[0]}) "
         "THEN RAISE(ABORT,'authority replacement') END; "
         f"SELECT CASE WHEN stage_append_allowed('{table}',"+
         ",".join("NEW."+key for key in columns)+
         ")!=1 THEN RAISE(ABORT,'unauthorized authority evidence') END; END;\n")
SQL += """
CREATE TRIGGER stage_authority_marker_update BEFORE UPDATE ON ledger_meta
 WHEN OLD.key='extension:stage_authority_v1' OR NEW.key='extension:stage_authority_v1'
 BEGIN SELECT RAISE(ABORT,'immutable authority marker'); END;
CREATE TRIGGER stage_authority_marker_delete BEFORE DELETE ON ledger_meta
 WHEN OLD.key='extension:stage_authority_v1'
 BEGIN SELECT RAISE(ABORT,'immutable authority marker'); END;
CREATE TRIGGER stage_authority_marker_insert BEFORE INSERT ON ledger_meta
 WHEN NEW.key='extension:stage_authority_v1' AND EXISTS(SELECT 1 FROM ledger_meta WHERE key=NEW.key)
 BEGIN SELECT RAISE(ABORT,'immutable authority marker'); END;
"""
