"""Additive, immutable canonical binding; imported originals remain unchanged."""
NAME = "original_resolution_v1"
SQL = """
CREATE TABLE original_resolutions(
 original_sha256 TEXT PRIMARY KEY NOT NULL REFERENCES originals(sha256),
 binding_sha256 TEXT UNIQUE NOT NULL,
 declaration_sha256 TEXT NOT NULL,
 policy_sha256 TEXT NOT NULL,
 capture_receipt_sha256 TEXT NOT NULL,
 capture_occurrence_id TEXT NOT NULL REFERENCES occurrences(id),
 payload_json TEXT NOT NULL
);
CREATE TRIGGER original_resolution_insert BEFORE INSERT ON original_resolutions BEGIN
 SELECT CASE WHEN original_resolution_append_allowed(NEW.original_sha256,
 NEW.binding_sha256,NEW.declaration_sha256,NEW.policy_sha256,
 NEW.capture_receipt_sha256,NEW.capture_occurrence_id,NEW.payload_json)!=1
 THEN RAISE(ABORT,'unauthorized original resolution') END;
 SELECT CASE WHEN EXISTS(SELECT 1 FROM original_resolutions
 WHERE original_sha256=NEW.original_sha256)
 THEN RAISE(ABORT,'original resolution cannot be replaced') END;
END;
CREATE TRIGGER original_resolution_update BEFORE UPDATE ON original_resolutions
 BEGIN SELECT RAISE(ABORT,'immutable original resolution'); END;
CREATE TRIGGER original_resolution_delete BEFORE DELETE ON original_resolutions
 BEGIN SELECT RAISE(ABORT,'immutable original resolution'); END;
CREATE TRIGGER original_resolution_marker_update BEFORE UPDATE ON ledger_meta
 WHEN OLD.key='extension:original_resolution_v1' OR NEW.key='extension:original_resolution_v1'
 BEGIN SELECT RAISE(ABORT,'immutable original resolution marker'); END;
CREATE TRIGGER original_resolution_marker_delete BEFORE DELETE ON ledger_meta
 WHEN OLD.key='extension:original_resolution_v1'
 BEGIN SELECT RAISE(ABORT,'immutable original resolution marker'); END;
CREATE TRIGGER original_resolution_marker_insert BEFORE INSERT ON ledger_meta
 WHEN NEW.key='extension:original_resolution_v1'
 AND EXISTS(SELECT 1 FROM ledger_meta WHERE key=NEW.key)
 BEGIN SELECT RAISE(ABORT,'original resolution marker cannot be replaced'); END;
"""
