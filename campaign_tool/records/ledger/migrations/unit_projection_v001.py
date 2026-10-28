"""Additive private unit-capture evidence and append-only batch checkpoints."""

NAME = "unit_projection_v1"
SQL = """
CREATE TABLE unit_projection_runs(
 id TEXT PRIMARY KEY NOT NULL,
 import_id TEXT NOT NULL REFERENCES legacy_imports(id),
 adapter_version TEXT NOT NULL,
 adapter_sha256 TEXT NOT NULL,
 source_database_sha256 TEXT NOT NULL,
 source_manifest_sha256 TEXT NOT NULL,
 expected_rows INTEGER NOT NULL CHECK(expected_rows>=0));
CREATE TABLE unit_projection_rows(
 projection_id TEXT NOT NULL REFERENCES unit_projection_runs(id),
 import_id TEXT NOT NULL,
 source_table TEXT NOT NULL CHECK(source_table='units'),
 source_rowid INTEGER NOT NULL,
 payload_sha256 TEXT NOT NULL,
 unit_id TEXT REFERENCES units(id),
 disposition TEXT NOT NULL CHECK(disposition IN ('projected','blocked','scope_excluded')),
 reason TEXT NOT NULL,
 observed_text TEXT,
 observed_text_sha256 TEXT,
 observed_locator TEXT,
 integrity_checked INTEGER NOT NULL CHECK(integrity_checked IN (0,1)),
 PRIMARY KEY(projection_id,source_rowid),
 FOREIGN KEY(import_id,source_table,source_rowid)
  REFERENCES legacy_rows(import_id,source_table,source_rowid),
 CHECK((disposition='projected' AND unit_id IS NOT NULL AND
        observed_text IS NOT NULL AND observed_text_sha256 IS NOT NULL AND
        observed_locator IS NOT NULL AND integrity_checked=1)
       OR (disposition!='projected' AND unit_id IS NULL)));
CREATE TABLE unit_projection_checkpoints(
 projection_id TEXT NOT NULL REFERENCES unit_projection_runs(id),
 batch_number INTEGER NOT NULL CHECK(batch_number>0),
 last_source_rowid INTEGER,
 previous_sha256 TEXT,
 checkpoint_sha256 TEXT NOT NULL,
 summary_json TEXT NOT NULL,
 PRIMARY KEY(projection_id,batch_number));
"""
for _table, _condition in (
    ("unit_projection_runs", "id=NEW.id"),
    ("unit_projection_rows",
     "projection_id=NEW.projection_id AND source_rowid=NEW.source_rowid"),
    ("unit_projection_checkpoints",
     "projection_id=NEW.projection_id AND batch_number=NEW.batch_number"),
):
    for _operation in ("UPDATE", "DELETE"):
        SQL += (
            f"CREATE TRIGGER {_table}_no_{_operation.lower()} BEFORE {_operation} "
            f"ON {_table} BEGIN SELECT RAISE(ABORT,'immutable unit capture'); END;\n"
        )
    SQL += (
        f"CREATE TRIGGER {_table}_no_replace BEFORE INSERT ON {_table} "
        f"WHEN EXISTS(SELECT 1 FROM {_table} WHERE {_condition}) "
        "BEGIN SELECT RAISE(ABORT,'immutable unit capture'); END;\n"
    )
