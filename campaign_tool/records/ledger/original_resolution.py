"""Initial resolution only; no replacement, scope override or stage promotion.

Trusted application startup supplies the approved local-capture policy. Evidence
submissions cannot choose a policy or register a resolution writer. All readers
use effective_original(), retaining the original immutable declaration.
"""
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from . import store
from .migrations import original_resolution_v001 as migration

KEY = "extension:" + migration.NAME
SCHEMA_HASH = hashlib.sha256(migration.SQL.encode()).hexdigest()
FIELDS = {"schema", "original_sha256", "declaration_sha256", "policy_sha256",
          "capture_receipt_sha256", "capture_occurrence_id", "byte_length",
          "role", "scope", "storage_path"}


MAIL_NAME = "original_resolution_mail_export_v1"
MAIL_FIELDS = {"schema", "original_sha256", "declaration_sha256", "policy_sha256",
               "verification_receipt_sha256", "occurrence_id", "occurrence_kind",
               "mail_identity_sha256", "legacy_binding_sha256", "byte_length", "role",
               "scope", "storage_path"}
MAX_RECEIPT_BYTES = 1024 * 1024


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


def _legacy_source(con, subject, original):
    """Legacy `docs` binding of an unresolved import (eligibility check 2); returns its row."""
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
    return source


def _require_untouched(con, subject):
    """Seven pending stages, no cached stage content, no proposal receipt (check 3)."""
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
    if type(payload) is dict and payload.get("schema") == MAIL_NAME:
        return _effective_mail(con, subject, original, row, payload)
    return _effective_v1(con, subject, original, row, payload)


def _effective_v1(con, subject, original, row, payload):
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
    _legacy_source(con, subject, original)
    _require_untouched(con, subject)
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


def _mail_occurrence(con, subject, occurrence_id, kind=None):
    """The `verified_mail_export` occurrence a mail resolution names (never trusted blindly)."""
    occurrence = _named_row(con, "SELECT * FROM occurrences WHERE id=?", (occurrence_id,))
    require(occurrence is not None and occurrence["original_sha256"] == subject
            and occurrence["acquisition_method"] == "verified_mail_export"
            and occurrence["kind"] in ("mail", "attachment")
            and (kind is None or occurrence["kind"] == kind), "original_resolution_mail_occurrence_unbound")
    try:
        source, evidence = json.loads(occurrence["source_ref"]), json.loads(occurrence["evidence"])
        identity = digest({key: source[key] for key in ("account", "folder", "uidvalidity", "uid")})
        receipt = evidence["receipt_sha256"]
        require(type(receipt) is str and re.fullmatch(r"[0-9a-f]{64}", receipt), "digest")
    except (ValueError, KeyError, TypeError):
        raise ValueError("original_resolution_mail_occurrence_unbound") from None
    return occurrence, identity, receipt, evidence


def _mail_receipt(evidence, output, receipt):
    """Re-hash the content-addressed verification receipt; path comes from the digest only."""
    from ..intake import folder
    path = Path(output) / "verification-receipts" / receipt
    try:
        require(evidence.get("verification_receipt_path") == str(path), "path")
        with folder.secure_open(path) as stream:
            raw = stream.read(MAX_RECEIPT_BYTES + 1)
        require(len(raw) <= MAX_RECEIPT_BYTES and hashlib.sha256(raw).hexdigest() == receipt, "hash")
    except (OSError, ValueError):
        raise ValueError("original_resolution_mail_receipt_changed") from None


def _mail_storage(storage_path, subject):
    require(type(storage_path) is str, "original_resolution_storage_binding")
    storage = Path(storage_path)
    require(storage.is_absolute() and storage.name == subject and storage.parent.name == "blobs",
            "original_resolution_storage_binding")
    return storage.parent.parent


def _effective_mail(con, subject, original, row, payload):
    require(set(payload) == MAIL_FIELDS and payload["original_sha256"] == subject,
            "original_resolution_payload_mismatch")
    require(digest(payload) == row["binding_sha256"]
            and payload["declaration_sha256"] == digest(original) == row["declaration_sha256"]
            and payload["byte_length"] == original["bytes"]
            and original["role"] == original["scope"] == "unresolved"
            and original["storage_path"] is None
            and payload["role"] == "original" and payload["scope"] == "in_scope",
            "original_resolution_declaration_mismatch")
    require(payload["policy_sha256"] == row["policy_sha256"]
            and payload["verification_receipt_sha256"] == row["capture_receipt_sha256"]
            and payload["occurrence_id"] == row["capture_occurrence_id"],
            "original_resolution_evidence_mismatch")
    output = _mail_storage(payload["storage_path"], subject)
    _, identity, receipt, evidence = _mail_occurrence(con, subject, payload["occurrence_id"],
                                                      payload["occurrence_kind"])
    require(identity == payload["mail_identity_sha256"] and receipt == payload["verification_receipt_sha256"],
            "original_resolution_mail_occurrence_unbound")
    _mail_receipt(evidence, output, receipt)
    original.update(role=payload["role"], scope=payload["scope"],
                    storage_path=payload["storage_path"], preservation_status="mail_export_validation_pending",
                    resolution_sha256=row["binding_sha256"])
    return original


def append_mail_export(con, *, subject, policy, verification_receipt_sha256, occurrence_id, storage_path):
    """Initial mail-export resolution; called only by the canonical mail backend.

    It runs inside the backend's root-locked write transaction, after the
    `verified_mail_export` occurrence rows exist. The approved policy, not the
    evidence, decides role and scope. No rebinding exists: an original that already
    has any resolution row is only ever replayed with the exact same payload.
    """
    from ..intake import folder
    from ..intake.mail_resolution_policy import ApprovedMailResolutionPolicy
    require(type(policy) is ApprovedMailResolutionPolicy, "installed_mail_resolution_policy_required")
    require(con.in_transaction, "original_resolution_requires_transaction")
    ensure_schema(con)
    row = con.execute("SELECT * FROM originals WHERE sha256=?", (subject,)).fetchone()
    require(row is not None, "original_resolution_original_missing")
    original = dict(row)
    entry = policy.entry(subject)
    require(entry is not None, "resolution_not_in_policy")
    require(entry["bytes"] == original["bytes"], "resolution_policy_bytes_mismatch")
    require(entry["verification_receipt_sha256"] == verification_receipt_sha256
            and entry["binding_occurrence_id"] == occurrence_id, "resolution_policy_receipt_mismatch")
    output = _mail_storage(storage_path, subject)
    require(original["role"] == original["scope"] == "unresolved"
            and original["storage_path"] is None, "original_resolution_not_unresolved")
    occurrence, mail_identity, receipt, evidence = _mail_occurrence(con, subject, occurrence_id)
    require(receipt == verification_receipt_sha256, "original_resolution_mail_occurrence_unbound")
    source = _legacy_source(con, subject, original)
    require(source["payload_sha256"] == entry["legacy_binding_sha256"], "resolution_policy_legacy_binding_mismatch")
    payload = dict(schema=MAIL_NAME, original_sha256=subject, declaration_sha256=digest(original),
                   policy_sha256=policy.sha256, verification_receipt_sha256=verification_receipt_sha256,
                   occurrence_id=occurrence_id, occurrence_kind=occurrence["kind"],
                   mail_identity_sha256=mail_identity, legacy_binding_sha256=source["payload_sha256"],
                   byte_length=original["bytes"], role="original", scope="in_scope", storage_path=storage_path)
    identity = digest(payload)
    prior = con.execute("SELECT binding_sha256 FROM original_resolutions WHERE original_sha256=?",
                        (subject,)).fetchone()
    if prior:
        require(prior[0] == identity, "original_resolution_conflict")
        effective_original(con, subject)
        return identity, True
    _require_untouched(con, subject)
    require(occurrence["kind"] != "mail" or original["legacy_format"] == "eml",
            "original_resolution_mail_kind_mismatch")
    try:
        proof = folder.verify_blob(subject, output)
    except (OSError, ValueError):
        raise ValueError("original_resolution_cas_unavailable") from None
    require(proof["bytes"] == original["bytes"], "original_resolution_cas_length_mismatch")
    _mail_receipt(evidence, output, receipt)
    values = (subject, identity, payload["declaration_sha256"], policy.sha256,
              verification_receipt_sha256, occurrence_id, store.canonical(payload))
    pending = values
    con.create_function("original_resolution_append_allowed", -1, lambda *args: int(args == pending))
    try:
        con.execute("INSERT INTO original_resolutions VALUES(?,?,?,?,?,?,?)", values)
    finally:
        pending = None
        con.create_function("original_resolution_append_allowed", -1, lambda *args: 0)
    effective_original(con, subject)
    return identity, False
