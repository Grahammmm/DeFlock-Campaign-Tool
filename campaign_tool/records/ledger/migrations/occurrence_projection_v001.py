"""Additive occurrence extension; bootstrap migration and checksum stay unchanged."""
NAME = "occurrence_projection_v1"
VERSION = 1
SQL = """
CREATE TABLE occurrence_projection_runs(
 id TEXT PRIMARY KEY NOT NULL,
 import_id TEXT NOT NULL REFERENCES legacy_imports(id),
 projector_version TEXT NOT NULL,
 projector_sha256 TEXT NOT NULL,
 input_rows_sha256 TEXT NOT NULL,
 source_database_sha256 TEXT NOT NULL,
 source_manifest_sha256 TEXT NOT NULL,
 summary_json TEXT NOT NULL
);
CREATE TABLE occurrence_projection_rows(
 projection_id TEXT NOT NULL REFERENCES occurrence_projection_runs(id),
 import_id TEXT NOT NULL,
 source_table TEXT NOT NULL CHECK(source_table='occurrences'),
 source_rowid INTEGER NOT NULL,
 payload_sha256 TEXT NOT NULL,
 payload_json TEXT NOT NULL,
 occurrence_id TEXT REFERENCES occurrences(id),
 disposition TEXT NOT NULL CHECK(disposition IN ('projected','blocked','scope_excluded')),
 reason TEXT NOT NULL CHECK(length(reason)>0),
 PRIMARY KEY(projection_id,source_rowid),
 FOREIGN KEY(import_id,source_table,source_rowid)
   REFERENCES legacy_rows(import_id,source_table,source_rowid),
 CHECK((disposition='projected' AND occurrence_id IS NOT NULL)
    OR (disposition!='projected' AND occurrence_id IS NULL))
);
CREATE INDEX occurrence_projection_source
 ON occurrence_projection_rows(import_id,source_rowid);
"""
for table, key in (
    ("occurrence_projection_runs", "id=NEW.id"),
    ("occurrence_projection_rows",
     "projection_id=NEW.projection_id AND source_rowid=NEW.source_rowid"),
):
    for operation in ("UPDATE", "DELETE"):
        SQL += (
            f"CREATE TRIGGER immutable_{table}_{operation.lower()} BEFORE {operation} ON {table} "
            "BEGIN SELECT RAISE(ABORT,'immutable occurrence projection'); END;\n"
        )
    SQL += (
        f"CREATE TRIGGER no_replace_{table} BEFORE INSERT ON {table} "
        f"WHEN EXISTS(SELECT 1 FROM {table} WHERE {key}) "
        "BEGIN SELECT RAISE(ABORT,'occurrence projection identity already exists'); END;\n"
    )
