"""Fail-closed binding guards and authorized relational append validation."""

NAME = "unit_projection_guards_v2"
MARKER = "extension:" + NAME
SQL = """
CREATE INDEX unit_projection_bound_unit ON unit_projection_rows(unit_id);
CREATE INDEX unit_projection_bound_source
 ON unit_projection_rows(import_id,source_table,source_rowid);
"""
for _table, _predicate in (
    ("units", "EXISTS(SELECT 1 FROM unit_projection_rows WHERE unit_id={row}.id)"),
    ("legacy_rows",
     "({row}.source_table='units' AND EXISTS(SELECT 1 FROM unit_projection_runs "
     "WHERE import_id={row}.import_id))"),
    ("ledger_meta",
     "{row}.key IN ('extension:unit_projection_v1','extension:unit_projection_guards_v1',"
     "'extension:unit_projection_guards_v2')"),
):
    for _operation in ("UPDATE", "DELETE", "INSERT"):
        _row = "NEW" if _operation == "INSERT" else "OLD"
        _condition = _predicate.format(row=_row)
        if _operation == "UPDATE":
            _condition = "(" + _condition + ") OR (" + _predicate.format(row="NEW") + ")"
        if _table == "ledger_meta" and _operation == "INSERT":
            _condition += " AND EXISTS(SELECT 1 FROM ledger_meta WHERE key=NEW.key)"
        SQL += (
            f"CREATE TRIGGER unit_capture_guard_{_table}_{_operation.lower()} "
            f"BEFORE {_operation} ON {_table} WHEN {_condition} "
            "BEGIN SELECT RAISE(ABORT,'immutable bound unit capture'); END;\n"
        )

SQL += """
CREATE INDEX unit_projection_run_import ON unit_projection_runs(import_id);
CREATE TRIGGER unit_capture_append_run BEFORE INSERT ON unit_projection_runs
BEGIN
 SELECT CASE WHEN unit_capture_append_allowed('run',NEW.id,NEW.import_id,
  NEW.adapter_version,NEW.adapter_sha256,NEW.source_database_sha256,
  NEW.source_manifest_sha256,NEW.expected_rows)!=1
  THEN RAISE(ABORT,'unauthorized unit run') END;
 SELECT CASE WHEN NOT EXISTS(
  SELECT 1 FROM legacy_imports WHERE id=NEW.import_id AND status='captured'
   AND source_database_sha256=NEW.source_database_sha256
   AND source_manifest_sha256=NEW.source_manifest_sha256
   AND json_extract(declared_table_counts,'$.units')=NEW.expected_rows)
  THEN RAISE(ABORT,'unit run capture mismatch') END;
END;
CREATE TRIGGER unit_capture_append_row BEFORE INSERT ON unit_projection_rows
BEGIN
 SELECT CASE WHEN unit_capture_append_allowed('row',NEW.projection_id,NEW.import_id,
  NEW.source_rowid,NEW.payload_sha256,NEW.unit_id,NEW.disposition,NEW.reason,
  NEW.observed_text,NEW.observed_text_sha256,NEW.observed_locator,NEW.integrity_checked)!=1
  THEN RAISE(ABORT,'unauthorized unit association') END;
 SELECT CASE WHEN NEW.source_table!='units' OR unit_capture_association_valid(
  NEW.projection_id,NEW.import_id,NEW.source_rowid,NEW.payload_sha256,NEW.unit_id,
  NEW.disposition,NEW.reason,NEW.observed_text,NEW.observed_text_sha256,
  NEW.observed_locator,NEW.integrity_checked)!=1
  THEN RAISE(ABORT,'unit association binding mismatch') END;
END;
CREATE TRIGGER unit_capture_append_checkpoint BEFORE INSERT ON unit_projection_checkpoints
BEGIN
 SELECT CASE WHEN unit_capture_append_allowed('checkpoint',NEW.projection_id,
  NEW.batch_number,NEW.last_source_rowid,NEW.previous_sha256,
  NEW.checkpoint_sha256,NEW.summary_json)!=1
  THEN RAISE(ABORT,'unauthorized unit checkpoint') END;
 SELECT CASE WHEN unit_capture_checkpoint_valid(NEW.projection_id,NEW.batch_number,
  NEW.last_source_rowid,NEW.previous_sha256,NEW.checkpoint_sha256,NEW.summary_json)!=1
  THEN RAISE(ABORT,'unit checkpoint correspondence mismatch') END;
END;
"""
