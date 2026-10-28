"""Mail payload implementation; the installed v1 table and gate stay unchanged."""
import hashlib
import os
import sqlite3
import unicodedata
from pathlib import Path
from . import store
from .mail_resolution_policy import ApprovedMailResolutionPolicy, decode, read_regular, sha

MAIL_SCHEMA = "original_resolution_mail_export_v1"
MAIL_FIELDS = {"schema", "original_sha256", "declaration_sha256", "policy_sha256",
               "verification_receipt_sha256", "occurrence_id", "occurrence_kind",
               "mail_identity_sha256", "legacy_binding_sha256", "byte_length",
               "role", "scope", "storage_path"}


def _occurrence(con, subject, occurrence_id, receipt, storage_path, *, output_root, kind=None, identity=None):
    from .original_resolution import _named_row, require, digest
    code = "original_resolution_mail_occurrence_unbound"
    occurrence = _named_row(con, "SELECT * FROM occurrences WHERE id=?", (occurrence_id,))
    require(occurrence is not None and occurrence["original_sha256"] == subject
            and occurrence["acquisition_method"] == "verified_mail_export"
            and occurrence["kind"] in ("mail", "attachment")
            and (kind is None or occurrence["kind"] == kind), code)
    try:
        source, evidence = decode(occurrence["source_ref"]), decode(occurrence["evidence"])
        require(type(source) is dict and type(evidence) is dict, code)
        mail_identity = {key: source[key] for key in ("account", "folder", "uidvalidity", "uid")}
        require(all(type(mail_identity[key]) is str and 0 < len(mail_identity[key]) <= 1024
                        and not any(unicodedata.category(c).startswith("C") for c in mail_identity[key])
                        for key in ("account", "folder"))
                and all(type(mail_identity[key]) is int and 0 < mail_identity[key] < 2 ** 32
                        for key in ("uidvalidity", "uid")), code)
        identity_hash = digest(mail_identity)
        require(identity is None or identity_hash == identity, code)
        require(evidence.get("receipt_sha256") == receipt, code)
        require(type(storage_path) is str, "original_resolution_storage_binding")
        require(output_root is not None, "original_resolution_storage_binding")
        root = Path(output_root)
        require(root.is_absolute() and ".." not in root.parts, "original_resolution_storage_binding")
        storage = Path(storage_path)
        require(storage.is_absolute() and storage.name == subject and storage.parent.name == "blobs"
                and ".." not in storage.parts and str(storage) == storage_path
                and os.path.normpath(storage_path) == storage_path and storage.parent.parent == root, "original_resolution_storage_binding")
        receipt_path = storage.parent.parent / "verification-receipts" / receipt
        # Derive the path from the digest, never use an evidence-selected path.
        require(evidence.get("verification_receipt_path") == str(receipt_path),
                "original_resolution_storage_binding")
    except (KeyError, TypeError, ValueError) as exc:
        if str(exc).startswith("original_resolution_"):
            raise
        raise ValueError(code) from None
    try:
        raw = read_regular(receipt_path, 1024 * 1024)
        require(hashlib.sha256(raw).hexdigest() == receipt, "original_resolution_mail_receipt_changed")
    except (OSError, ValueError):
        raise ValueError("original_resolution_mail_receipt_changed") from None
    return occurrence["kind"], identity_hash


def _legacy_binding(con, subject, original, entry):
    from .original_resolution import _named_row, require
    try:
        provenance = decode(original["provenance_json"])
        require(type(provenance) is dict and provenance.get("table") == "docs"
                and type(provenance.get("import_id")) is str, "original_resolution_import_evidence_missing")
        source = _named_row(con, "SELECT payload_json,payload_sha256,identity_json FROM legacy_rows "
                            "WHERE import_id=? AND source_table='docs' AND source_rowid=?",
                            (provenance["import_id"], provenance.get("source_rowid")))
        require(source is not None, "original_resolution_import_evidence_missing")
        legacy = decode(source["payload_json"])
        require(type(legacy) is dict and hashlib.sha256(source["payload_json"].encode()).hexdigest()
                == source["payload_sha256"] == entry["legacy_binding_sha256"]
                and decode(source["identity_json"]) == {"sha": subject}
                and legacy.get("sha") == subject and legacy.get("bytes") == original["bytes"]
                and legacy.get("format") == original["legacy_format"], "original_resolution_import_binding_mismatch")
    except (KeyError, TypeError, ValueError) as exc:
        if str(exc).startswith("original_resolution_"):
            raise
        raise ValueError("original_resolution_import_binding_mismatch") from None


def effective(con, subject, original, row, payload, *, output_root, policy):
    from .original_resolution import require, digest
    require(set(payload) == MAIL_FIELDS and payload["original_sha256"] == subject
            and all(sha(payload[key]) for key in ("original_sha256", "declaration_sha256", "policy_sha256",
                    "verification_receipt_sha256", "occurrence_id", "mail_identity_sha256", "legacy_binding_sha256")),
            "original_resolution_payload_mismatch")
    require(row["payload_json"] == store.canonical(payload), "original_resolution_payload_mismatch")
    require(digest(payload) == row["binding_sha256"]
            and payload["declaration_sha256"] == digest(original) == row["declaration_sha256"]
            and type(payload["byte_length"]) is int and payload["byte_length"] == original["bytes"]
            and original["role"] == original["scope"] == "unresolved" and original["storage_path"] is None
            and payload["role"] == "original" and payload["scope"] == "in_scope",
            "original_resolution_declaration_mismatch")
    require(payload["policy_sha256"] == row["policy_sha256"]
            and payload["verification_receipt_sha256"] == row["capture_receipt_sha256"]
            and payload["occurrence_id"] == row["capture_occurrence_id"],
            "original_resolution_evidence_mismatch")
    require(type(policy) is ApprovedMailResolutionPolicy, "resolution_policy_invalid")
    require(policy.sha256 == payload["policy_sha256"], "resolution_policy_mismatch")
    entry = policy.admission(subject, original["bytes"])
    require(entry["verification_receipt_sha256"] == payload["verification_receipt_sha256"]
            and entry["binding_occurrence_id"] == payload["occurrence_id"]
            and entry["legacy_binding_sha256"] == payload["legacy_binding_sha256"],
            "resolution_policy_receipt_mismatch")
    _legacy_binding(con, subject, original, entry)
    require(payload["occurrence_kind"] in ("mail", "attachment"), "original_resolution_mail_occurrence_unbound")
    _occurrence(con, subject, payload["occurrence_id"], payload["verification_receipt_sha256"],
                payload["storage_path"], output_root=output_root, kind=payload["occurrence_kind"], identity=payload["mail_identity_sha256"])
    original.update(role="original", scope="in_scope", storage_path=payload["storage_path"],
                    preservation_status="mail_export_validation_pending", resolution_sha256=row["binding_sha256"])
    return original


def append(con, *, subject, policy, verification_receipt_sha256, occurrence_id, storage_path, output_root):
    """No commit or acquisition bypass: caller verifies MIME, CAS and receipt first."""
    from .original_resolution import _named_row, require, digest, ensure_schema, effective_original
    require(con.in_transaction, "original_resolution_transaction_required")
    con.execute("SAVEPOINT mail_resolution_append")
    pending = None
    writing = False
    try:
        require(type(policy) is ApprovedMailResolutionPolicy, "resolution_policy_invalid")
        ensure_schema(con)
        original = _named_row(con, "SELECT * FROM originals WHERE sha256=?", (subject,))
        require(original is not None, "original_resolution_not_unresolved")
        original = dict(original)
        entry = policy.admission(subject, original["bytes"])
        require(verification_receipt_sha256 == entry["verification_receipt_sha256"]
                and occurrence_id == entry["binding_occurrence_id"], "resolution_policy_receipt_mismatch")
        kind, identity_hash = _occurrence(con, subject, occurrence_id, verification_receipt_sha256, storage_path, output_root=output_root)
        payload = dict(schema=MAIL_SCHEMA, original_sha256=subject, declaration_sha256=digest(original),
                       policy_sha256=policy.sha256, verification_receipt_sha256=verification_receipt_sha256,
                       occurrence_id=occurrence_id, occurrence_kind=kind, mail_identity_sha256=identity_hash,
                       legacy_binding_sha256=entry["legacy_binding_sha256"], byte_length=original["bytes"],
                       role="original", scope="in_scope", storage_path=storage_path)
        identity = digest(payload)
        prior = con.execute("SELECT binding_sha256 FROM original_resolutions WHERE original_sha256=?", (subject,)).fetchone()
        if prior:
            require(prior[0] == identity, "original_resolution_conflict")
            effective_original(con, subject, output_root=output_root, mail_policy=policy)
            con.execute("RELEASE mail_resolution_append")
            return identity, True
        eligible(con, subject, original, entry, kind)
        values = (subject, identity, payload["declaration_sha256"], policy.sha256,
                  verification_receipt_sha256, occurrence_id, store.canonical(payload))
        writing = True
        pending = values
        con.create_function("original_resolution_append_allowed", -1, lambda *row: int(row == pending))
        try:
            con.execute("INSERT INTO original_resolutions VALUES(?,?,?,?,?,?,?)", values)
        finally:
            pending = None
            con.create_function("original_resolution_append_allowed", -1, lambda *row: 0)
        effective_original(con, subject, output_root=output_root, mail_policy=policy)
        con.execute("RELEASE mail_resolution_append")
    except BaseException as exc:
        con.execute("ROLLBACK TO mail_resolution_append")
        con.execute("RELEASE mail_resolution_append")
        if writing and isinstance(exc, sqlite3.Error):
            raise ValueError("original_resolution_write_failed") from None
        raise
    finally:
        pending = None
        con.create_function("original_resolution_append_allowed", -1, lambda *row: 0)
    return identity, False


def eligible(con, subject, original, entry, kind):
    """Read-only pre-occurrence eligibility; append rechecks before writing."""
    from .original_resolution import require
    require(original["role"] == original["scope"] == "unresolved" and original["storage_path"] is None,
            "original_resolution_not_unresolved")
    _legacy_binding(con, subject, original, entry)
    require(kind != "mail" or original["legacy_format"] == "eml", "original_resolution_import_binding_mismatch")
    states = con.execute("SELECT stage,status,receipt_sha256 FROM stage_state WHERE original_sha256=?", (subject,)).fetchall()
    require(len(states) == 7 and {row[0] for row in states} == set(store.STAGES)
            and all(row[1] == "pending" and row[2] is None for row in states),
            "original_resolution_requires_unaccepted_original")
    if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='stage_content'").fetchone():
        require(not con.execute("SELECT 1 FROM stage_content WHERE subject_sha256=? LIMIT 1", (subject,)).fetchone(),
                "original_resolution_requires_uncached_original")
    require(not con.execute("SELECT 1 FROM proposals p JOIN receipts r ON "
                            "r.sha256 IN (p.privacy_receipt_sha256,p.independent_receipt_sha256) "
                            "WHERE r.subject_sha256=? LIMIT 1", (subject,)).fetchone(), "original_resolution_proposal_exists")
