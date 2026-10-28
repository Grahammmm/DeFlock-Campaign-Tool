"""Isolated append-only detector manifest and run-hit linkage tables."""
import hashlib
import re

VERSION = "detector-persistence-v1"
TABLES = {
    "wp6_persistence_meta": ("singleton",),
    "wp6_manifests": ("run_id",),
    "wp6_hit_identities": ("dedupe_key",),
    "wp6_run_hits": ("run_id", "dedupe_key"),
    "wp6_input_originals": ("run_id", "original_sha256"),
}
STATEMENTS = [
    """CREATE TABLE wp6_persistence_meta(
        singleton INTEGER PRIMARY KEY CHECK(singleton=1), version TEXT NOT NULL, checksum TEXT NOT NULL)""",
    """CREATE TABLE wp6_manifests(
        run_id TEXT PRIMARY KEY REFERENCES detector_runs(run_id),
        adapter_version TEXT NOT NULL, manifest_sha256 TEXT NOT NULL,
        input_sha256 TEXT NOT NULL, units_sha256 TEXT NOT NULL, joins_sha256 TEXT NOT NULL,
        rules_sha256 TEXT NOT NULL, config_sha256 TEXT NOT NULL,
        run_provenance_json TEXT NOT NULL, manifest_json TEXT NOT NULL,
        result_sha256 TEXT NOT NULL, hit_count INTEGER NOT NULL CHECK(hit_count>=0),
        FOREIGN KEY(run_id) REFERENCES runs(run_id))""",
    """CREATE TABLE wp6_hit_identities(
        dedupe_key TEXT PRIMARY KEY REFERENCES detector_hits(dedupe_key),
        semantic_json TEXT NOT NULL, semantic_sha256 TEXT NOT NULL,
        canonical_row_sha256 TEXT NOT NULL)""",
    """CREATE TABLE wp6_run_hits(
        run_id TEXT NOT NULL REFERENCES wp6_manifests(run_id),
        dedupe_key TEXT NOT NULL REFERENCES wp6_hit_identities(dedupe_key),
        hit_json TEXT NOT NULL, hit_sha256 TEXT NOT NULL,
        PRIMARY KEY(run_id,dedupe_key))""",
    """CREATE TABLE wp6_input_originals(
        run_id TEXT NOT NULL REFERENCES wp6_manifests(run_id),
        original_sha256 TEXT NOT NULL REFERENCES originals(sha256),
        metadata_sha256 TEXT NOT NULL, PRIMARY KEY(run_id,original_sha256))""",
]
for table, keys in TABLES.items():
    predicate = " AND ".join(f"{key}=NEW.{key}" for key in keys)
    STATEMENTS.append(f"CREATE TRIGGER {table}_no_replace BEFORE INSERT ON {table} "
                      f"WHEN EXISTS(SELECT 1 FROM {table} WHERE {predicate}) "
                      "BEGIN SELECT RAISE(ABORT,'immutable detector evidence'); END")
    for operation in ("UPDATE", "DELETE"):
        STATEMENTS.append(f"CREATE TRIGGER {table}_no_{operation.lower()} BEFORE {operation} ON {table} "
                          "BEGIN SELECT RAISE(ABORT,'immutable detector evidence'); END")
CHECKSUM = hashlib.sha256("\n".join(STATEMENTS).encode()).hexdigest()
DEFINITIONS = {}
for statement in STATEMENTS:
    kind, name = re.match(r"CREATE (TABLE|TRIGGER) ([a-z0-9_]+)", statement).groups()
    DEFINITIONS[(kind.lower(), name)] = statement.strip()


def apply(connection):
    """Caller owns the transaction. Refuse partial or unversioned adoption."""
    if not connection.in_transaction:
        raise ValueError("migration_transaction_required")
    placeholders = ",".join("?" for _ in TABLES)
    existing = {row[0] for row in connection.execute(
        f"SELECT name FROM sqlite_master WHERE type='table' AND name IN ({placeholders})", tuple(TABLES))}
    if existing:
        if existing != set(TABLES):
            raise ValueError("partial_detector_extension_refused")
        rows = list(connection.execute("SELECT singleton,version,checksum FROM wp6_persistence_meta"))
        if [tuple(row) for row in rows] != [(1, VERSION, CHECKSUM)]:
            raise ValueError("detector_extension_version_mismatch")
        names = tuple(name for _, name in DEFINITIONS)
        placeholders = ",".join("?" for _ in names)
        actual = {(row[0], row[1]): row[2].strip() if row[2] is not None else None
                  for row in connection.execute(
                      f"SELECT type,name,sql FROM sqlite_master WHERE name IN ({placeholders})", names)}
        # A same-named no-op trigger or changed table is not this migration.
        # Compare complete SQLite-stored definitions, not just presence or the
        # metadata checksum. Unknown formatting also requires explicit migration.
        if actual != DEFINITIONS:
            raise ValueError("detector_extension_definition_mismatch")
        return
    for statement in STATEMENTS:
        connection.execute(statement)
    connection.execute("INSERT INTO wp6_persistence_meta VALUES(1,?,?)", (VERSION, CHECKSUM))
