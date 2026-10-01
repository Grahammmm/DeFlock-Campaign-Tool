"""Project captured occurrence facts without reopening sources or advancing stages."""
import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys

from .store import canonical, file_hash, ledger
from .migrations import occurrence_projection_v001 as migration

VERSION = "captured-occurrences-2"
MAX_ROWS = 20000
MAX_PAYLOAD_BYTES = 64 * 1024 * 1024
SCHEMA_HASH = hashlib.sha256(migration.SQL.encode()).hexdigest()
MIME_PATH = re.compile(r"1(?:\.[1-9][0-9]*)*\Z")


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def valid_sha(value):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate_json_key")
        value[key] = item
    return value


def decode(value):
    return json.loads(value, object_pairs_hook=_pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("invalid_json")))


def _text(value):
    return type(value) is str and bool(value.strip()) and "\x00" not in value


def _timestamp(value):
    if value is None:
        return None
    if not _text(value):
        raise ValueError("invalid_timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp_timezone_missing")
    return value


def mime_path(locator, receipt):
    """Never convert a flat traversal ordinal into a hierarchical MIME path."""
    if type(locator) is not dict:
        return None, "mime_locator_missing"
    indexes = {"mime_part_index", "traversal_index"} & set(locator)
    if indexes:
        if len(locator) != 1 or len(indexes) != 1:
            return None, "ambiguous_mime_conventions"
        index = locator[next(iter(indexes))]
        if type(index) is not int or index < 0:
            return None, "invalid_mime_traversal_index"
        return None, "mime_traversal_index_not_hierarchical"
    if receipt.get("mime_convention") not in (None, "hierarchical_root_1"):
        return None, "unsupported_mime_convention"
    if set(locator) != {"mime"}:
        return None, "unsupported_mime_locator"
    value = locator["mime"]
    if type(value) is not str or MIME_PATH.fullmatch(value) is None:
        return None, "invalid_mime_hierarchical_path"
    return value, None


def decode_source(source):
    raw = source["payload_json"]
    if type(raw) is not str or digest(raw) != source["payload_sha256"]:
        raise ValueError("captured_payload_hash_mismatch")
    row = decode(raw)
    identity = decode(source["identity_json"])
    if type(row) is not dict:
        raise ValueError("invalid_captured_occurrence_payload")
    if type(identity) is not dict or identity != {"oid": row.get("oid"), "sha": row.get("sha")}:
        raise ValueError("captured_occurrence_identity_mismatch")
    return row


def _candidate(kind, identity, sha, parent=None, acquired=None):
    return {
        "id": "occurrence:" + digest(canonical([kind, identity])),
        "original_sha256": sha, "kind": kind, "source_ref": canonical(identity),
        "parent_occurrence_id": parent, "acquired_at": acquired,
        "acquisition_method": "captured_legacy_" + kind,
    }


def _base_plan(row, original):
    if not _text(row.get("oid")) or not valid_sha(row.get("sha")):
        return None, "invalid_legacy_occurrence_identity", None, None
    if original is None:
        return None, "original_not_in_canonical_ledger", None, None
    if original["scope"] == "out_of_scope":
        return None, "source_scope_excluded", None, None
    try:
        receipt = decode(row["receipt"]) if row.get("receipt") is not None else {}
        locator = decode(row["locator"]) if row.get("locator") is not None else None
    except (TypeError, ValueError):
        return None, "invalid_legacy_locator_or_receipt", None, None
    if type(receipt) is not dict:
        return None, "invalid_legacy_receipt", None, None
    if row.get("parent") not in (None, ""):
        return None, "container_pending", receipt, locator
    if locator is not None or receipt.get("parent_sha256") is not None:
        return None, "unbound_root_locator_or_parent", receipt, locator
    try:
        acquired = _timestamp(receipt.get("received_at"))
    except ValueError:
        return None, "invalid_received_at", receipt, locator
    if "mail_identity" in receipt:
        identity = receipt["mail_identity"]
        if (type(identity) is not dict or set(identity) != {"account", "folder", "uidvalidity", "uid"}
                or not all(_text(identity.get(key)) for key in ("account", "folder"))
                or any(type(identity.get(key)) is not int or identity[key] <= 0 for key in ("uidvalidity", "uid"))
                or receipt.get("eml_sha256") != row["sha"] or original["legacy_format"] != "eml"):
            return None, "invalid_or_unbound_mail_identity", receipt, locator
        return _candidate("mail", {"scheme": "mail_account_folder_uid_v1", **identity},
                          row["sha"], acquired=acquired), "explicit_mail_identity", receipt, locator
    if "portal_identity" in receipt:
        return None, "portal_identity_adapter_not_supported", receipt, locator
    if (not _text(row.get("root")) or not _text(row.get("path"))
            or any(type(receipt.get(key)) is not int for key in ("mtime_ns", "ctime_ns", "device", "inode"))
            or receipt["device"] < 0 or receipt["inode"] < 0):
        return None, "filesystem_observation_receipt_missing", receipt, locator
    try:
        if _timestamp(receipt.get("observed_at")) is None:
            raise ValueError("missing_observation_time")
    except ValueError:
        return None, "filesystem_observation_time_invalid", receipt, locator
    identity = {"scheme": "legacy_filesystem_observation_v1", "legacy_oid": row["oid"],
                "original_sha256": row["sha"], "root": row["root"], "path": row["path"]}
    return _candidate("local", identity, row["sha"], acquired=acquired), "filesystem_observation_only", receipt, locator


def _container_plan(row, receipt, locator, plans):
    if not valid_sha(row.get("parent")) or receipt.get("parent_sha256") != row["parent"]:
        return None, "container_parent_hash_unbound"
    relation = receipt.get("relation")
    if relation == "reserialized_rfc822":
        return None, "reserialized_message_not_original_attachment_octets"
    if relation != "decoded_mime_payload":
        return None, "container_relation_adapter_not_supported"
    path, reason = mime_path(locator, receipt)
    if reason:
        return None, reason
    parent_oid = receipt.get("parent_legacy_oid")
    if not _text(parent_oid):
        return None, "parent_occurrence_identity_missing"
    parent = plans.get((parent_oid, row["parent"]))
    if parent is None or parent["candidate"] is None or parent["candidate"]["kind"] != "mail":
        return None, "typed_mail_parent_not_established"
    parent_id = parent["candidate"]["id"]
    identity = {"scheme": "mail_attachment_hierarchical_root_1_v1",
                "message_occurrence_id": parent_id, "mime_part_path": path}
    return _candidate("attachment", identity, row["sha"], parent=parent_id), "explicit_parent_and_hierarchical_mime"


def _ensure_schema(con):
    key = "extension:" + migration.NAME
    saved = con.execute("SELECT value FROM ledger_meta WHERE key=?", (key,)).fetchone()
    required = {"occurrence_projection_runs", "occurrence_projection_rows"}
    present = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")} & required
    if saved:
        if saved[0] != SCHEMA_HASH or present != required:
            raise ValueError("occurrence_extension_schema_mismatch")
        return
    if present:
        raise ValueError("unbound_occurrence_extension_schema")
    statement = ""
    for line in migration.SQL.splitlines(True):
        statement += line
        if sqlite3.complete_statement(statement):
            con.execute(statement)
            statement = ""
    if statement.strip():
        raise ValueError("incomplete_occurrence_extension_sql")
    con.execute("INSERT INTO ledger_meta(key,value) VALUES(?,?)", (key, SCHEMA_HASH))


def _load_rows(con, import_id, max_rows):
    size = con.execute(
        "SELECT COUNT(*),coalesce(SUM(length(CAST(payload_json AS BLOB))),0) FROM legacy_rows "
        "WHERE import_id=? AND source_table='occurrences'", (import_id,)).fetchone()
    if size[0] > max_rows or size[1] > MAX_PAYLOAD_BYTES:
        raise ValueError("occurrence_projection_resource_limit")
    return [dict(row) for row in con.execute(
        "SELECT source_rowid,identity_json,payload_json,payload_sha256 FROM legacy_rows "
        "WHERE import_id=? AND source_table='occurrences' ORDER BY source_rowid", (import_id,))]


def _insert_exact(con, table, key, fields):
    existing = con.execute("SELECT * FROM " + table + " WHERE id=?", (key,)).fetchone()
    if existing:
        if any(existing[name] != value for name, value in fields.items()):
            raise ValueError("existing_" + table + "_identity_conflict")
        return
    names = tuple(fields)
    con.execute("INSERT INTO " + table + "(id," + ",".join(names) + ") VALUES(" +
                ",".join("?" for _ in range(len(names) + 1)) + ")", (key, *(fields[name] for name in names)))


def _verify_canonical_occurrence(con, candidate, *, required):
    saved = con.execute("SELECT * FROM occurrences WHERE id=?", (candidate["id"],)).fetchone()
    if saved is None:
        if required:
            raise ValueError("canonical_occurrence_missing")
        return None
    if any(saved[name] != value for name, value in candidate.items()):
        raise ValueError("canonical_occurrence_binding_mismatch")
    evidence = decode(saved["evidence"])
    keys = {"projection_id", "import_id", "source_table", "source_rowid",
            "payload_sha256", "verification", "agency_request_join"}
    if (type(evidence) is not dict or set(evidence) != keys
            or evidence["source_table"] != "occurrences"
            or evidence["verification"] != "captured_declaration_only"
            or evidence["agency_request_join"] != "blocked"):
        raise ValueError("canonical_occurrence_evidence_mismatch")
    binding = con.execute(
        "SELECT 1 FROM occurrence_projection_rows r JOIN occurrence_projection_runs p "
        "ON p.id=r.projection_id AND p.import_id=r.import_id "
        "WHERE r.projection_id=? AND r.import_id=? AND r.source_table='occurrences' "
        "AND r.source_rowid=? AND r.payload_sha256=? AND r.occurrence_id=? "
        "AND r.disposition='projected'",
        (evidence["projection_id"], evidence["import_id"], evidence["source_rowid"],
         evidence["payload_sha256"], candidate["id"])).fetchone()
    if binding is None:
        raise ValueError("canonical_occurrence_evidence_unbound")
    return saved


def project_occurrences(database, import_id, *, max_rows=MAX_ROWS):
    """One atomic, bounded projection. Captures and stage state stay intact."""
    if not valid_sha(import_id):
        raise ValueError("invalid_import_id")
    if type(max_rows) is not int or not 1 <= max_rows <= MAX_ROWS:
        raise ValueError("invalid_projection_row_limit")
    implementation = file_hash(Path(__file__))
    with ledger(database) as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            capture = con.execute("SELECT * FROM legacy_imports WHERE id=?", (import_id,)).fetchone()
            if capture is None or capture["status"] != "captured":
                raise ValueError("completed_capture_required")
            sources = _load_rows(con, import_id, max_rows)
            declared = decode(capture["declared_table_counts"])
            if type(declared) is not dict or declared.get("occurrences") != len(sources):
                raise ValueError("captured_occurrence_count_mismatch")
            rows = [decode_source(source) for source in sources]
            inputs = digest(canonical([[s["source_rowid"], s["identity_json"], s["payload_sha256"]] for s in sources]))
            projection_id = digest(canonical([VERSION, implementation, SCHEMA_HASH, import_id, inputs]))
            _ensure_schema(con)
            prior = con.execute("SELECT summary_json FROM occurrence_projection_runs WHERE id=?", (projection_id,)).fetchone()
            if prior:
                saved = list(con.execute(
                    "SELECT source_rowid,payload_sha256,payload_json FROM occurrence_projection_rows "
                    "WHERE projection_id=? ORDER BY source_rowid", (projection_id,)))
                expected = [(s["source_rowid"], s["payload_sha256"], s["payload_json"]) for s in sources]
                if [tuple(row) for row in saved] != expected:
                    raise ValueError("existing_projection_source_mismatch")
            originals = {row["sha256"]: dict(row) for row in con.execute(
                "SELECT sha256,scope,legacy_format FROM originals")}
            plans, ordered = {}, []
            for source, row in zip(sources, rows):
                key = (row.get("oid"), row.get("sha"))
                if not all(type(value) in (str, type(None)) for value in key):
                    raise ValueError("invalid_captured_identity_type")
                if key in plans:
                    raise ValueError("duplicate_captured_occurrence_identity")
                original = originals.get(row.get("sha"))
                candidate, reason, receipt, locator = _base_plan(row, original)
                plan = {"source": source, "row": row, "candidate": candidate, "reason": reason,
                        "receipt": receipt, "locator": locator, "original": original}
                plans[key] = plan
                ordered.append(plan)
            for plan in ordered:
                if plan["reason"] == "container_pending":
                    plan["candidate"], plan["reason"] = _container_plan(
                        plan["row"], plan["receipt"], plan["locator"], plans)
            candidates = {}
            for plan in ordered:
                candidate = plan["candidate"]
                if candidate:
                    existing = candidates.get(candidate["id"])
                    if existing and existing != candidate:
                        raise ValueError("conflicting_native_occurrence_identity")
                    candidates[candidate["id"]] = candidate
            dispositions = Counter("projected" if p["candidate"] else
                                   "scope_excluded" if p["reason"] == "source_scope_excluded" else "blocked"
                                   for p in ordered)
            summary = {"source_rows": len(sources), "projected_source_rows": dispositions["projected"],
                       "blocked_source_rows": dispositions["blocked"], "scope_excluded_source_rows": dispositions["scope_excluded"],
                       "canonical_occurrences_in_projection": len(candidates),
                       "canonical_kinds": dict(Counter(c["kind"] for c in candidates.values())),
                       "unresolved_reasons": dict(Counter(p["reason"] for p in ordered if not p["candidate"])),
                       "stage_promotions": 0, "verified_agency_request_joins": 0,
                       "publication_ready": False, "end_to_end_complete": None}
            if prior:
                retained = [tuple(row) for row in con.execute(
                    "SELECT source_rowid,occurrence_id,disposition,reason "
                    "FROM occurrence_projection_rows WHERE projection_id=? ORDER BY source_rowid",
                    (projection_id,))]
                expected_bindings = [
                    (p["source"]["source_rowid"], p["candidate"]["id"] if p["candidate"] else None,
                     "projected" if p["candidate"] else
                     "scope_excluded" if p["reason"] == "source_scope_excluded" else "blocked",
                     p["reason"]) for p in ordered]
                if retained != expected_bindings or decode(prior[0]) != summary:
                    raise ValueError("existing_projection_binding_mismatch")
                for candidate in candidates.values():
                    _verify_canonical_occurrence(con, candidate, required=True)
                con.rollback()
                return {"projection_id": projection_id, "reused": True, **summary}
            con.execute("INSERT INTO occurrence_projection_runs VALUES(?,?,?,?,?,?,?,?)",
                        (projection_id, import_id, VERSION, implementation, inputs,
                         capture["source_database_sha256"], capture["source_manifest_sha256"], canonical(summary)))
            for kind in ("local", "mail", "attachment"):
                for candidate in candidates.values():
                    if candidate["kind"] != kind:
                        continue
                    saved = _verify_canonical_occurrence(con, candidate, required=False)
                    if saved:
                        continue
                    first = next(p for p in ordered if p["candidate"] and p["candidate"]["id"] == candidate["id"])
                    source = first["source"]
                    evidence = canonical({"projection_id": projection_id, "import_id": import_id,
                                          "source_table": "occurrences", "source_rowid": source["source_rowid"],
                                          "payload_sha256": source["payload_sha256"],
                                          "verification": "captured_declaration_only", "agency_request_join": "blocked"})
                    con.execute(
                        "INSERT INTO occurrences(id,original_sha256,kind,source_ref,parent_occurrence_id,acquired_at,acquisition_method,evidence) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (candidate["id"], candidate["original_sha256"], kind, candidate["source_ref"],
                         candidate["parent_occurrence_id"], candidate["acquired_at"], candidate["acquisition_method"], evidence))
            for plan in ordered:
                source, row, candidate = plan["source"], plan["row"], plan["candidate"]
                disposition = "projected" if candidate else "scope_excluded" if plan["reason"] == "source_scope_excluded" else "blocked"
                con.execute("INSERT INTO occurrence_projection_rows VALUES(?,?,?,?,?,?,?,?,?)",
                            (projection_id, import_id, "occurrences", source["source_rowid"], source["payload_sha256"],
                             source["payload_json"], candidate["id"] if candidate else None, disposition, plan["reason"]))
                binding = canonical({"import_id": import_id, "source_table": "occurrences",
                                     "source_rowid": source["source_rowid"], "payload_sha256": source["payload_sha256"]})
                if not candidate:
                    gap_id = digest(canonical([projection_id, source["source_rowid"], "occurrence_gap"]))
                    _insert_exact(con, "import_gaps", gap_id, {
                        "import_id": import_id, "subject_sha256": row.get("sha") if valid_sha(row.get("sha")) else None,
                        "category": "occurrence_projection", "reason": canonical({"code": plan["reason"], "source": decode(binding)}),
                        "owner": "occurrence_reconciliation", "status": "blocked"})
                if plan["original"] is not None and disposition != "scope_excluded":
                    join_id = digest(canonical([import_id, source["source_rowid"], "agency_request_unresolved"]))
                    _insert_exact(con, "joins", join_id, {
                        "original_sha256": row["sha"], "agency_id": None, "request_id": None,
                        "join_type": "captured_occurrence_context", "evidence": binding, "status": "blocked",
                        "blocked_reason": "agency_request_not_established_by_occurrence_projection"})
            con.commit()
            return {"projection_id": projection_id, "reused": False, **summary}
        except Exception:
            con.rollback()
            raise


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--import-id", required=True)
    parser.add_argument("--max-rows", type=int, default=MAX_ROWS)
    args = parser.parse_args(arguments)
    try:
        result = project_occurrences(args.database, args.import_id, max_rows=args.max_rows)
    except (ValueError, OSError, sqlite3.Error, TypeError, KeyError):
        print(json.dumps({"status": "blocked", "code": "occurrence_projection_failed"}), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
