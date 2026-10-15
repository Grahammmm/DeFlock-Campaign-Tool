"""Offline, declared-scope diagnostics for records-unattended recovery.

No CLI, capture, restore, writer control or activation. Call only on explicitly
selected offline copies. This is a partial dependency reader, never an operational
closure certificate. In particular, opaque JSON/path dependencies remain held.
Caller SQL is deliberately unsupported (including constant SELECT placeholders).
Reports contain counts and fixed reason codes, never paths, payloads or row IDs.

Readers cover originals.storage_path, intake preservations.path, receipts.path
(including ledger-stage-artifact:), stage_content -> stage_artifacts and the
canonical mail_checkpoints plus unattended resume file. "Verified" means hash
and reference consistency, not receipt authenticity or policy applicability.
Capture/resolution JSON, context/configuration pointers, extraction artifacts,
queue envelopes and publication history require further readers; this version
always reports those opaque dependencies unqualified. No v2 Profile is emitted.
Limits: 1,000 files, 16 MiB/file, 64 MiB total and 10,000 rows/query. Do not raise
these limits merely to fit an operational tree. Never point this at live stores.
"""
from contextlib import closing
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import sqlite3
import time

from .backup import RecoveryError, canonical, fingerprint, require, sha, source_fd
from .backup_v2 import _absolute, _credential, _digest, _inventory, _private

VERSION = "records-unattended-dependency-assessor-v1"
KINDS = frozenset(("canonical", "intake", "original_blob", "receipt", "resume", "rules", "configuration"))
MAX_BYTES = 16 * 1024 * 1024
MAX_TOTAL = 64 * 1024 * 1024
MAX_ROWS = 10000


@dataclass(frozen=True)
class Dependency:
    """Pinned offline file and its historical source identity; no inferred paths."""
    source: str
    historical_source: str
    kind: str
    sha256: str
    schema_sha256: str = ""
    reference_sql: tuple = ()


def _rows(con, sql):
    rows = con.execute(sql).fetchmany(MAX_ROWS + 1)
    require(len(rows) <= MAX_ROWS, "dependency_row_bound")
    return rows


def _schema(con):
    return sha(canonical(_rows(con, "SELECT type,name,tbl_name,sql FROM sqlite_master "
                              "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name")))


def _resume(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "resume_invalid")
            result[key] = value
        return result
    require(len(raw) <= 4096, "resume_invalid")
    try:
        value = json.loads(raw, object_pairs_hook=pairs)
    except RecursionError as error:
        raise RecoveryError("resume_invalid") from error
    require(type(value) is dict and set(value) == {"schema", "cursor", "frontier"} and
            value["schema"] == "records-advance-resume-v1", "resume_invalid")
    for key in ("cursor", "frontier"):
        item = value[key]
        require(item is None or (type(item) is list and len(item) == 2 and
                type(item[0]) is str and 0 < len(item[0]) <= 128 and _digest(item[1])), "resume_invalid")
    require(value["cursor"] is None or (value["frontier"] is not None and
            value["cursor"] <= value["frontier"]), "resume_invalid")


def assess(*, offline_root, historical_root, dependencies, code_commit):
    """Return a held diagnostic; rejected inputs never produce partial coverage.

    code_commit is a caller declaration, NOT an attestation of installed code.
    Module byte hashes separately bind the implementation actually inspected.
    Inputs must enumerate every file in the private offline root. SQLite copies
    must be standalone: sidecars and unknown files reject before any content read.
    Only pinned bytes are deserialized into private in-memory SQLite connections.
    """
    records = Path(__file__).parent.parent
    module_paths = [Path(__file__), Path(__file__).with_name("backup.py"),
                    Path(__file__).with_name("backup_v2.py"), records / "publish.py"]
    report = {"schema": VERSION, "operation": "records-unattended", "status": "held",
              "activation": False, "capture_eligible": False,
              "code_identity": {"declared_commit": None, "module_sha256":
                                {p.name: sha(p.read_bytes()) for p in module_paths}},
              "coverage": {}, "schema_sha256": {}, "reasons": []}
    try:
        require(type(code_commit) is str and re.fullmatch(r"[0-9a-f]{40}", code_commit), "code_identity_invalid")
        report["code_identity"]["declared_commit"] = code_commit
        root, historical = _absolute(offline_root), _absolute(historical_root)
        require(root != historical, "offline_copy_required")
        require(type(dependencies) is tuple and 2 <= len(dependencies) <= 1000, "dependency_bound")
        paths, historical_paths, kinds = {}, {}, {}
        for item in dependencies:
            require(type(item) is Dependency and item.kind in KINDS, "unknown_dependency")
            source, old = _absolute(item.source), _absolute(item.historical_source)
            require(root in source.parents and historical in old.parents, "dependency_outside_scope")
            require(not _credential(source) and not _credential(old), "credential_dependency_forbidden")
            require(not source.name.endswith(("-wal", "-shm", "-journal")) and
                    not old.name.endswith(("-wal", "-shm", "-journal")), "sqlite_sidecar_forbidden")
            require(source not in paths and old not in historical_paths, "duplicate_dependency")
            require(not item.reference_sql, "caller_reference_sql_unsupported")
            require(_digest(item.sha256), "dependency_hash_required")
            require(_digest(item.schema_sha256) if item.kind in ("canonical", "intake") else
                    item.schema_sha256 == "", "dependency_schema_invalid")
            paths[source] = item
            historical_paths[str(old)] = item
            kinds.setdefault(item.kind, []).append(item)
        require(len(kinds.get("canonical", [])) == len(kinds.get("intake", [])) == 1,
                "two_ledgers_required")
        require(len(kinds.get("resume", [])) == 1, "unattended_cursor_required")
        resume = kinds["resume"][0]
        require(resume.historical_source == str(historical / "reports/unattended-advance-resume.json"),
                "unattended_cursor_location_invalid")
        before = _inventory(root)
        require({Path(p) for p, (kind, _) in before.items() if kind == "file"} == set(paths),
                "undeclared_or_missing_file")
        payloads, total = {}, 0
        for source, item in paths.items():
            with source_fd(source) as (fd, info):
                _private(info)
                require(info.st_size <= MAX_BYTES, "dependency_size_bound")
                total += info.st_size
                require(total <= MAX_TOTAL, "dependency_total_bound")
                require(fingerprint(info) == before[str(source)][1], "dependency_changed")
                raw = bytearray()
                while len(raw) <= info.st_size:
                    chunk = os.read(fd, min(65536, info.st_size + 1 - len(raw)))
                    if not chunk:
                        break
                    raw.extend(chunk)
                require(len(raw) == info.st_size and fingerprint(os.fstat(fd)) == fingerprint(info),
                        "dependency_changed")
                require(sha(raw) == item.sha256, "dependency_hash_mismatch")
                require(raw, "empty_dependency_placeholder")
                if item.kind in ("canonical", "intake", "resume"):
                    payloads[item.kind] = bytes(raw)
        _resume(payloads["resume"])
        coverage = {"files_verified": len(paths), "embedded_artifacts_verified": 0,
                    "embedded_receipts_verified": 0, "embedded_review_objects_verified": 0,
                    "mail_checkpoint_rows": 0, "unattended_cursor_files": 1,
                    "external_references_verified": 0}
        schemas, embedded, referenced_files = {}, {}, set()
        deadline = time.monotonic() + 10
        for kind in ("canonical", "intake"):
            with closing(sqlite3.connect(":memory:")) as con:
                con.deserialize(payloads[kind])
                con.execute("PRAGMA trusted_schema=OFF")
                con.execute("PRAGMA query_only=ON")
                # Restrict queries to reads and bounded schema introspection.
                # Generated columns are rejected separately before selecting data.
                con.set_authorizer(lambda action, a, b, db, origin:
                    sqlite3.SQLITE_OK if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ) or
                    (action == sqlite3.SQLITE_FUNCTION and b == "like") or
                    (action == sqlite3.SQLITE_PRAGMA and a == "table_xinfo") else sqlite3.SQLITE_DENY)
                con.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
                schemas[kind] = _schema(con)
                require(schemas[kind] == kinds[kind][0].schema_sha256, "schema_identity_mismatch")
                tables = {name for name, sql in _rows(con, "SELECT name,sql FROM sqlite_master WHERE type='table'")
                          if sql and not re.match(r"CREATE\s+VIRTUAL\b", sql, re.I)}
                def table(name):
                    require(name in tables, "required_table_missing")
                    # name is a fixed reader constant, never caller SQL.
                    require(all(row[-1] == 0 for row in _rows(con, 'PRAGMA table_xinfo("' + name + '")')),
                            "generated_columns_unsupported")
                def reference(path, digest, expected_kind):
                    item = historical_paths.get(path)
                    require(item is not None and item.kind == expected_kind and item.sha256 == digest,
                            "external_reference_missing_or_mismatched")
                    referenced_files.add(item.source)
                    coverage["external_references_verified"] += 1
                if kind == "intake":
                    table("preservations")
                    for digest, path in _rows(con, "SELECT sha,path FROM preservations"):
                        reference(path, digest, "original_blob")
                    continue
                for name in ("originals", "receipts", "stage_artifacts", "stage_content", "mail_checkpoints", "ledger_meta"):
                    table(name)
                for digest, path in _rows(con, "SELECT sha256,storage_path FROM originals"):
                    reference(path, digest, "original_blob")
                for digest, payload in _rows(con, "SELECT sha256,payload FROM stage_artifacts"):
                    require(type(payload) is bytes and _digest(digest) and sha(payload) == digest,
                            "embedded_artifact_invalid")
                    embedded[digest] = payload
                coverage["embedded_artifacts_verified"] = len(embedded)
                for digest, path in _rows(con, "SELECT sha256,path FROM receipts"):
                    if path == "ledger-stage-artifact:" + digest:
                        require(digest in embedded, "embedded_receipt_missing")
                        coverage["embedded_receipts_verified"] += 1
                    else:
                        reference(path, digest, "receipt")
                for stage, digest in _rows(con, "SELECT stage,content_sha256 FROM stage_content"):
                    require(digest in embedded, "embedded_stage_content_missing")
                    if stage == "review":
                        coverage["embedded_review_objects_verified"] += 1
                for validity, uid in _rows(con, "SELECT uidvalidity,highest_uid FROM mail_checkpoints"):
                    require(type(validity) is int and validity > 0 and type(uid) is int and uid >= 0,
                            "mail_checkpoint_invalid")
                    coverage["mail_checkpoint_rows"] += 1
                bindings = _rows(con, "SELECT value FROM ledger_meta WHERE key='publication-recovery-root-v1'")
                require(not bindings or bindings == [(historical_root,)], "publication_binding_mismatch")
        require(all(item.source in referenced_files for item in dependencies
                    if item.kind in ("original_blob", "receipt")), "unreferenced_evidence_placeholder")
        require(_inventory(root) == before, "dependency_changed")
        report["coverage"], report["schema_sha256"] = coverage, schemas
        report["reasons"] = ["operation_closure_unqualified", "opaque_dependency_fields_unqualified",
                             "all_writer_quiescence_unproved", "read_only_source_view_unproved",
                             "operational_restore_unproved"]
        if embedded:
            report["reasons"].append("embedded_objects_have_no_v2_object_reference_adapter")
        if kinds.get("rules") or kinds.get("configuration"):
            report["reasons"].append("declared_configuration_binding_unqualified")
        if coverage["embedded_review_objects_verified"] == 0:
            report["reasons"].append("review_evidence_not_observed")
        report["reasons"].append("relocated_publication_requires_separate_authority_adapter")
    except RecoveryError as error:
        report["status"] = "rejected"
        report["reasons"] = [str(error)]
    except (OSError, ValueError, TypeError, AttributeError, sqlite3.Error):
        report["status"] = "rejected"
        report["reasons"] = ["dependency_unreadable_or_invalid"]
    return report
