"""Initial resolution only; no replacement, scope override or stage promotion.

Trusted application startup supplies the approved local-capture policy. Evidence
submissions cannot choose a policy or register a resolution writer. All readers
use effective_original(), retaining the original immutable declaration.
"""
import hashlib
import json
import sqlite3
from pathlib import Path
from . import store
from .migrations import original_resolution_v001 as migration
from .mail_resolution_policy import ApprovedMailResolutionPolicy
from . import original_resolution_mail as mail

KEY = "extension:" + migration.NAME
SCHEMA_HASH = hashlib.sha256(migration.SQL.encode()).hexdigest()
FIELDS = {"schema", "original_sha256", "declaration_sha256", "policy_sha256",
          "capture_receipt_sha256", "capture_occurrence_id", "byte_length",
          "role", "scope", "storage_path"}
MAIL_FIELDS = mail.MAIL_FIELDS
MAIL_SCHEMA = mail.MAIL_SCHEMA


def digest(value):
    return hashlib.sha256(store.canonical(value).encode()).hexdigest()


def require(value, reason):
    if not value:
        raise ValueError(reason)


def installed(con):
    present = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                          "AND name='original_resolutions'").fetchone()
    metadata = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ledger_meta'").fetchone()
    if metadata is None:
        require(present is None, "unbound_original_resolution_schema")
        return False  # Existing read-only queue fixtures may be minimal snapshots.
    marker = con.execute("SELECT value FROM ledger_meta WHERE key=?", (KEY,)).fetchone()
    require(bool(marker) == bool(present), "unbound_original_resolution_schema")
    if marker:
        require(marker[0] == SCHEMA_HASH, "original_resolution_schema_mismatch")
    return bool(marker)


def ensure_schema(con):
    if installed(con):
        return
    statement = ""
    for line in migration.SQL.splitlines(True):
        statement += line
        if sqlite3.complete_statement(statement):
            con.execute(statement)
            statement = ""
    require(not statement.strip(), "incomplete_original_resolution_schema")
    con.execute("INSERT INTO ledger_meta VALUES(?,?)", (KEY, SCHEMA_HASH))


def _named_row(con, sql, parameters):
    # Detector adapters accept caller-owned plain sqlite connections. Keep the
    # connection's row factory unchanged and name only this cursor's columns.
    cursor = con.cursor()
    try:
        cursor.row_factory = sqlite3.Row
        return cursor.execute(sql, parameters).fetchone()
    finally:
        cursor.close()


def effective_original(con, subject):
    declaration = _named_row(con, "SELECT * FROM originals WHERE sha256=?", (subject,))
    if declaration is None:
        return None
    original = dict(declaration)
    if not installed(con):
        return original
    row = _named_row(con, "SELECT * FROM original_resolutions WHERE original_sha256=?", (subject,))
    if row is None:
        return original
    payload = json.loads(row["payload_json"])
    if type(payload) is dict and payload.get("schema") == MAIL_SCHEMA:
        return mail.effective(con, subject, original, row, payload)
    require(type(payload) is dict and set(payload) == FIELDS
            and payload["schema"] == migration.NAME and payload["original_sha256"] == subject,
            "original_resolution_payload_mismatch")
    require(digest(payload) == row["binding_sha256"]
            and payload["declaration_sha256"] == digest(original) == row["declaration_sha256"]
            and payload["byte_length"] == original["bytes"]
            and original["role"] == original["scope"] == "unresolved"
            and original["storage_path"] is None
            and payload["role"] == "original" and payload["scope"] == "in_scope",
            "original_resolution_declaration_mismatch")
    require(all(payload[key] == row[key] for key in
                ("policy_sha256", "capture_receipt_sha256", "capture_occurrence_id")),
            "original_resolution_evidence_mismatch")
    occurrence = _named_row(con, "SELECT * FROM occurrences WHERE id=?",
                            (payload["capture_occurrence_id"],))
    require(occurrence is not None and occurrence["original_sha256"] == subject
            and occurrence["acquisition_method"] == "verified_local_capture",
            "original_resolution_capture_unbound")
    from ..intake import local_capture
    storage = Path(payload["storage_path"])
    require(storage.is_absolute() and storage.name == subject and storage.parent.name == "blobs",
            "original_resolution_storage_binding")
    require(local_capture.verify_occurrence(occurrence, storage.parent.parent, subject,
                expected_policy=payload["policy_sha256"], verify_cas=False) == payload["capture_receipt_sha256"],
            "original_resolution_capture_receipt_changed")
    original.update(role=payload["role"], scope=payload["scope"],
                    storage_path=payload["storage_path"], preservation_status="capture_validation_pending",
                    resolution_sha256=row["binding_sha256"])
    return original


def append_mail_export(con, *, subject, policy, verification_receipt_sha256, occurrence_id, storage_path):
    """Backend-only, inside its root-locked write transaction; never a CLI route."""
    return mail.append(con, subject=subject, policy=policy,
                       verification_receipt_sha256=verification_receipt_sha256,
                       occurrence_id=occurrence_id, storage_path=storage_path)


def append_initial(con, *, subject, policy_sha256, capture_receipt_sha256,
                   capture_occurrence_id, storage_path):
    """Called only by the installed reconciler inside its root/write transaction.

    No rebinding is implemented. This prevents receipt/cache reinterpretation;
    any future correction needs a separately reviewed supersession contract.
    """
    ensure_schema(con)
    original = dict(con.execute("SELECT * FROM originals WHERE sha256=?", (subject,)).fetchone())
    payload = dict(schema=migration.NAME, original_sha256=subject,
                   declaration_sha256=digest(original), policy_sha256=policy_sha256,
                   capture_receipt_sha256=capture_receipt_sha256,
                   capture_occurrence_id=capture_occurrence_id, byte_length=original["bytes"],
                   role="original", scope="in_scope", storage_path=storage_path)
    identity = digest(payload)
    prior = con.execute("SELECT binding_sha256 FROM original_resolutions WHERE original_sha256=?",
                        (subject,)).fetchone()
    if prior:
        require(prior[0] == identity, "original_resolution_conflict")
        effective_original(con, subject)
        return identity, True
    require(original["role"] == original["scope"] == "unresolved"
            and original["storage_path"] is None, "original_resolution_not_unresolved")
    provenance = json.loads(original["provenance_json"])
    require(type(provenance) is dict and provenance.get("table") == "docs"
            and type(provenance.get("import_id")) is str
            and con.execute("SELECT 1 FROM legacy_rows WHERE import_id=? AND source_table='docs' "
                            "AND source_rowid=?", (provenance["import_id"], provenance.get("source_rowid"))).fetchone(),
            "original_resolution_import_evidence_missing")
    source = con.execute("SELECT payload_json,payload_sha256,identity_json FROM legacy_rows WHERE import_id=? "
                         "AND source_table='docs' AND source_rowid=?",
                         (provenance["import_id"],provenance["source_rowid"])).fetchone()
    legacy = json.loads(source["payload_json"])
    require(hashlib.sha256(source["payload_json"].encode()).hexdigest() == source["payload_sha256"]
            and json.loads(source["identity_json"]) == {"sha": subject}
            and legacy.get("sha") == subject and legacy.get("bytes") == original["bytes"]
            and legacy.get("format") == original["legacy_format"], "original_resolution_import_binding_mismatch")
    states = list(con.execute("SELECT stage,status,receipt_sha256 FROM stage_state WHERE original_sha256=?", (subject,)))
    require(len(states) == 7 and {row["stage"] for row in states} == set(store.STAGES)
            and all(row["status"] == "pending" and row["receipt_sha256"] is None for row in states),
            "original_resolution_requires_unaccepted_original")
    if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='stage_content'").fetchone():
        require(not con.execute("SELECT 1 FROM stage_content WHERE subject_sha256=? LIMIT 1", (subject,)).fetchone(),
                "original_resolution_requires_uncached_original")
    require(not con.execute("SELECT 1 FROM proposals p JOIN receipts r ON "
                            "r.sha256 IN (p.privacy_receipt_sha256,p.independent_receipt_sha256) "
                            "WHERE r.subject_sha256=? LIMIT 1", (subject,)).fetchone(),
            "original_resolution_proposal_exists")
    values = (subject, identity, payload["declaration_sha256"], policy_sha256,
              capture_receipt_sha256, capture_occurrence_id, store.canonical(payload))
    pending = values
    con.create_function("original_resolution_append_allowed", -1, lambda *row: int(row == pending))
    try:
        con.execute("INSERT INTO original_resolutions VALUES(?,?,?,?,?,?,?)", values)
    finally:
        pending = None
        con.create_function("original_resolution_append_allowed", -1, lambda *row: 0)
    effective_original(con, subject)
    return identity, False
