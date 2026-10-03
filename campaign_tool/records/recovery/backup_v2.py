"""Bounded declared-scope recovery library. No CLI or live-writer discovery."""
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time

from campaign_tool import __version__
from .backup import (RecoveryError, canonical, sha, require, parent_fd,
                     source_fd, new_directory, write_new, copy_exact,
                     snapshot_ledger, fingerprint, MAX_FILE, MAX_TOTAL,
                     MAX_ENTRIES, MAX_MANIFEST)

SCHEMA = "records-backup-v2"
REQUIRED_ROLES = frozenset(("canonical_ledger", "intake_ledger", "original_blob",
                            "receipt", "cursor", "review_bundle"))
ROLES = REQUIRED_ROLES | frozenset(("rules", "configuration", "derived_resume",
                                   "queue_store", "publication_store"))
EXCLUSIONS = frozenset(("credential", "sqlite_sidecar", "process_state", "rebuildable"))
NAME = re.compile(r"[a-z][a-z0-9_-]{0,79}")
HASH = re.compile(r"[0-9a-f]{64}")
MAX_REFS = 10000
BOUNDARY_SECONDS = 120
OPERATION_SECONDS = 60


@dataclass(frozen=True)
class Store:
    name: str
    source: str
    roles: tuple = ()
    kind: str = "file"
    included: bool = True
    exclusion: str = ""
    expected_sha256: str = ""
    required_tables: tuple = ()
    schema_sha256: str = ""
    reference_sql: tuple = ()


@dataclass(frozen=True)
class Profile:
    roots: tuple
    stores: tuple


@dataclass(frozen=True)
class CallerQuiescence:
    roots: tuple
    observed_utc: str
    evidence_sha256: str
    basis: str = "independent_all_writer_audit"
    all_writers_stopped: bool = False


def _absolute(value):
    require(type(value) is str and value and "\0" not in value, "invalid_path")
    p = Path(value)
    require(p.is_absolute() and str(p) == value and ".." not in p.parts, "absolute_canonical_path_required")
    return p


def _inside(p, root):
    return p == root or root in p.parents


def _digest(value):
    return type(value) is str and HASH.fullmatch(value) is not None


def _private(info, directory=False):
    require(info.st_uid == os.getuid(), "wrong_owner")
    require(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode),
            "directory_required" if directory else "regular_file_required")
    require(stat.S_IMODE(info.st_mode) == 0o700 if directory else
            stat.S_IMODE(info.st_mode) in (0o400, 0o600), "owner_only_permissions_required")
    if not directory:
        require(info.st_nlink == 1, "hardlink_rejected")


def _inventory(root):
    """FD-anchored metadata walk; never opens excluded file contents."""
    found = {}
    def visit(fd, p):
        _private(os.fstat(fd), True)
        found[str(p)] = ("directory", fingerprint(os.fstat(fd)))
        with os.scandir(fd) as entries:
            names = sorted(entry.name for entry in entries)
        require(len(found) + len(names) <= MAX_ENTRIES * 4, "scope_entry_bound")
        for name in names:
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            require(not stat.S_ISLNK(info.st_mode), "symlink_rejected")
            target = p / name
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    require((info.st_dev, info.st_ino) == (os.fstat(child).st_dev, os.fstat(child).st_ino),
                            "directory_changed")
                    visit(child, target)
                finally:
                    os.close(child)
            else:
                _private(info)
                require(info.st_size <= MAX_FILE, "file_size_bound")
                found[str(target)] = ("file", fingerprint(info))
    with parent_fd(root) as (parent, name):
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            visit(fd, root)
        finally:
            os.close(fd)
    return found


def _credential(path):
    for part in path.parts:
        lower = part.lower()
        if lower in (".runtime", ".env", "credentials", "secrets", "signing_key", "signing-key",
                     "password", "passwords", "token", "tokens") or lower.startswith(".env.") or lower.endswith((".key", ".pem")):
            return True
    return False


def _queries(value):
    require(type(value) in (tuple, list) and len(value) <= 20, "reference_query_bound")
    for sql in value:
        require(type(sql) is str and 1 <= len(sql) <= 4096 and
                re.match(r"^\s*SELECT\b", sql, re.I) and ";" not in sql, "read_only_reference_query_required")


def _tables(value):
    require(type(value) in (tuple, list) and value and len(value) <= 100 and
            all(type(t) is str and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,99}", t) for t in value) and
            len(set(value)) == len(value), "required_tables_invalid")


def _profile(profile):
    require(type(profile) is Profile and type(profile.roots) is tuple and
            1 <= len(profile.roots) <= 16 and type(profile.stores) is tuple and
            1 <= len(profile.stores) <= MAX_ENTRIES, "profile_required")
    roots = tuple(_absolute(p) for p in profile.roots)
    require(len(set(roots)) == len(roots) and not any(_inside(a, b) for a in roots for b in roots if a != b),
            "duplicate_or_nested_roots")
    names, sources, roles, databases = set(), set(), set(), []
    for item in profile.stores:
        require(type(item) is Store and type(item.name) is str and NAME.fullmatch(item.name),
                "store_identity_invalid")
        p = _absolute(item.source)
        require(item.name not in names and p not in sources, "duplicate_store")
        names.add(item.name); sources.add(p)
        require(sum(_inside(p, r) for r in roots) == 1 and p not in roots, "store_outside_scope")
        require(type(item.included) is bool and item.kind in ("file", "sqlite") and type(item.roles) is tuple,
                "store_shape_invalid")
        if not item.included:
            require(not item.roles and item.kind == "file" and item.exclusion in EXCLUSIONS and
                    not item.expected_sha256 and not item.required_tables and not item.schema_sha256 and
                    not item.reference_sql, "excluded_store_invalid")
            if _credential(p):
                require(item.exclusion == "credential", "credential_classification_required")
            continue
        require(item.roles and len(set(item.roles)) == len(item.roles) and set(item.roles) <= ROLES and
                not item.exclusion, "included_store_roles_invalid")
        require(not _credential(p), "credential_inclusion_forbidden")
        require(not item.expected_sha256 or _digest(item.expected_sha256), "expected_hash_invalid")
        if set(item.roles) & {"receipt", "review_bundle"}:
            require(_digest(item.expected_sha256), "review_or_receipt_hash_required")
        roles.update(item.roles)
        if item.kind == "sqlite":
            _tables(item.required_tables); _queries(item.reference_sql)
            require(_digest(item.schema_sha256) and item.reference_sql, "sqlite_schema_and_closure_queries_required")
            if "cursor" in item.roles:
                require("mail_checkpoints" in item.required_tables, "cursor_table_required")
            databases.append(p)
        else:
            require(not item.required_tables and not item.schema_sha256 and not item.reference_sql,
                    "file_cannot_declare_database_schema")
    require(REQUIRED_ROLES <= roles and len(databases) >= 2, "required_roles_and_two_databases")
    for item in profile.stores:
        if not item.included and item.exclusion == "sqlite_sidecar":
            require(any(item.source == str(db) + suffix for db in databases
                        for suffix in ("-wal", "-shm", "-journal")), "unpaired_sqlite_sidecar")
    inventories = {}
    for r in roots:
        inventories.update(_inventory(r))
    expected = set(str(p) for p in sources)
    expected_dirs = set(str(r) for r in roots)
    for p in sources:
        r = next(r for r in roots if _inside(p, r))
        expected_dirs.update(str(q) for q in p.parents if _inside(q, r))
    require(set(inventories) == expected | expected_dirs, "unknown_or_missing_scope_store")
    require(all(inventories[str(p)][0] == "file" for p in sources), "store_must_be_regular_file")
    return roots, inventories


def _boundary(q, roots):
    require(type(q) is CallerQuiescence and q.all_writers_stopped is True and
            q.basis == "independent_all_writer_audit" and _digest(q.evidence_sha256) and
            q.roots == tuple(str(r) for r in roots), "independent_caller_quiescence_required")
    require(type(q.observed_utc) is str and q.observed_utc.endswith("+00:00"), "utc_boundary_required")
    try:
        observed = datetime.fromisoformat(q.observed_utc)
    except ValueError:
        raise RecoveryError("utc_boundary_required") from None
    require(observed.tzinfo is not None and 0 <= (datetime.now(timezone.utc) - observed).total_seconds() <= BOUNDARY_SECONDS,
            "quiescence_attestation_expired")
    return {"basis": q.basis, "observed_utc": q.observed_utc, "evidence_sha256": q.evidence_sha256,
            "caller_confirmed_all_writers_stopped": True, "independently_verified_by_library": False}


def _db_check(path, row, deadline):
    with source_fd(path):
        con = sqlite3.connect(Path(path).as_uri() + "?mode=ro&immutable=1", uri=True, timeout=1)
        try:
            con.execute("PRAGMA query_only=ON")
            con.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            require([r[0] for r in con.execute("PRAGMA integrity_check")] == ["ok"], "database_integrity_failed")
            require(con.execute("PRAGMA foreign_key_check").fetchone() is None, "database_foreign_key_failed")
            schema = [list(r) for r in con.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name")]
            schema_hash = sha(canonical(schema))
            require(schema_hash == row["schema_sha256"], "database_schema_identity_mismatch")
            tables = {r[1] for r in schema if r[0] == "table"}
            require(set(row["required_tables"]) <= tables, "database_required_table_missing")
            refs = []
            for sql in row["reference_sql"]:
                cursor = con.execute(sql)
                require(len(cursor.description or ()) == 3, "reference_query_columns")
                for ref in cursor:
                    require(len(refs) < MAX_REFS, "reference_count_bound")
                    role, source, digest = tuple(ref)
                    require(type(role) is str and role in ROLES and type(source) is str and _digest(digest),
                            "reference_row_invalid")
                    _absolute(source)
                    refs.append((role, source, digest))
            return refs
        finally:
            con.close()


def _closure(rows, references):
    by_source = {row["source"]: row for row in rows}
    for role, source, digest in references:
        require(source in by_source and role in by_source[source]["roles"] and
                digest == by_source[source]["sha256"], "database_reference_not_closed")
    # Every required evidence category must be bound by a database closure query,
    # not merely an arbitrary unreferenced file carrying the right label.
    bound_roles = {r[0] for r in references}
    require({"original_blob", "receipt", "review_bundle"} <= bound_roles, "required_evidence_not_database_bound")


def _separate(destination, roots):
    target = _absolute(destination)
    require(not any(_inside(target, r) or _inside(r, target) for r in roots), "destination_must_be_isolated")
    with parent_fd(target) as (fd, name):
        _private(os.fstat(fd), True)
        try:
            os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            return target
        raise RecoveryError("destination_exists_no_overwrite")


def _deadline(deadline):
    require(time.monotonic() <= deadline, "operation_timeout")


def _snapshot_quiescent(source, destination, deadline):
    """No-sidecar source: immutable SQLite read, never create source WAL/SHM.

    Immutable mode is safe only under the required independent caller boundary.
    Existing sidecars retain the WAL-aware v1 snapshot path, not immutable mode.
    """
    _deadline(deadline)
    if any(os.path.lexists(str(source) + suffix) for suffix in ("-wal", "-shm", "-journal")):
        result = snapshot_ledger(source, destination)
        _deadline(deadline)
        return result
    with source_fd(source) as (source_file, before), parent_fd(destination) as (parent, name):
        out = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        os.close(out)
        snapshot_deadline = min(deadline, time.monotonic() + 30)
        live = sqlite3.connect(Path(source).as_uri() + "?mode=ro&immutable=1", uri=True, timeout=1)
        try:
            live.execute("PRAGMA query_only=ON")
            page_size = live.execute("PRAGMA page_size").fetchone()[0]
            require(page_size * live.execute("PRAGMA page_count").fetchone()[0] <= MAX_FILE, "ledger_size_bound")
            def progress(status, remaining, total):
                _deadline(snapshot_deadline)
                require(total * page_size <= MAX_FILE, "ledger_snapshot_bound")
            target = sqlite3.connect(destination, timeout=1)
            try:
                live.backup(target, pages=128, progress=progress, sleep=0.01)
            finally:
                target.close()
        finally:
            live.close()
        _deadline(deadline)
        with parent_fd(source) as (src_parent, src_name):
            after = os.stat(src_name, dir_fd=src_parent, follow_symlinks=False)
        require(fingerprint(before) == fingerprint(os.fstat(source_file)) == fingerprint(after),
                "ledger_path_changed")
        with source_fd(destination) as (fd, info):
            digest = hashlib.sha256()
            while block := os.read(fd, 1024 * 1024):
                _deadline(deadline)
                digest.update(block)
            require(info.st_size <= MAX_FILE, "ledger_snapshot_bound")
            os.fsync(fd); os.fchmod(fd, 0o400)
        os.fsync(parent)
        _deadline(deadline)
        return {"sha256": digest.hexdigest(), "bytes": info.st_size}


def backup_v2(destination, profile, *, quiescence):
    """Caller must establish quiescence independently before calling this library."""
    started = time.monotonic()
    try:
        roots, before = _profile(profile)
        _deadline(started + OPERATION_SECONDS)
        boundary = _boundary(quiescence, roots)
        target = _separate(destination, roots)
        new_directory(target); new_directory(target / "payload")
        rows, excluded, references, total = [], [], [], 0
        for item in sorted(profile.stores, key=lambda s: s.name):
            require(time.monotonic() - started <= OPERATION_SECONDS, "operation_timeout")
            if not item.included:
                excluded.append({"name": item.name, "source": item.source, "reason": item.exclusion})
                continue
            copied = (_snapshot_quiescent(item.source, target / "payload" / item.name, started + OPERATION_SECONDS)
                      if item.kind == "sqlite" else copy_exact(item.source, target / "payload" / item.name))
            _deadline(started + OPERATION_SECONDS)
            total += copied["bytes"]
            require(total <= MAX_TOTAL, "total_size_bound")
            require(not item.expected_sha256 or copied["sha256"] == item.expected_sha256, "expected_hash_mismatch")
            if "original_blob" in item.roles:
                require(Path(item.source).name == copied["sha256"], "original_content_address_mismatch")
            row = {"name": item.name, "path": "payload/" + item.name, "source": item.source,
                   "roles": list(item.roles), "kind": item.kind, **copied,
                   "required_tables": list(item.required_tables), "schema_sha256": item.schema_sha256,
                   "reference_sql": list(item.reference_sql)}
            rows.append(row)
            if item.kind == "sqlite":
                references.extend(_db_check(target / row["path"], row, started + OPERATION_SECONDS))
            _deadline(started + OPERATION_SECONDS)
        _deadline(started + OPERATION_SECONDS)
        _closure(rows, references)
        _deadline(started + OPERATION_SECONDS)
        after = {}
        for r in roots:
            _deadline(started + OPERATION_SECONDS)
            after.update(_inventory(r))
            _deadline(started + OPERATION_SECONDS)
        require(before == after, "scope_changed_during_backup")
        _boundary(quiescence, roots)
        _deadline(started + OPERATION_SECONDS)
        manifest = {"schema": SCHEMA, "engine_version": __version__, "scope": "declared_profile_closed",
                    "roots": [str(r) for r in roots], "required_roles": sorted(REQUIRED_ROLES),
                    "quiescence": boundary, "files": rows, "excluded": excluded, "total_bytes": total,
                    "activation": False, "sends": False, "replay": False, "live_pipeline_recovery_proven": False}
        raw = canonical(manifest)
        _deadline(started + OPERATION_SECONDS)
        require(len(raw) <= MAX_MANIFEST, "manifest_size_bound")
        manifest_digest = sha(raw)
        _deadline(started + OPERATION_SECONDS)
        write_new(target / "manifest.json", raw)
        _deadline(started + OPERATION_SECONDS)
        return {"schema": SCHEMA, "manifest_sha256": manifest_digest, "files": len(rows), "sqlite_snapshots":
                sum(r["kind"] == "sqlite" for r in rows), "declared_scope_closed": True,
                "live_pipeline_recovery_proven": False}
    except RecoveryError:
        raise
    except (OSError, ValueError, TypeError, sqlite3.Error):
        raise RecoveryError("backup_v2_refused") from None


def _manifest(source, digest):
    require(_digest(digest), "trusted_manifest_digest_required")
    with source_fd(source / "manifest.json") as (fd, info):
        require(info.st_size <= MAX_MANIFEST, "manifest_size_bound")
        raw = os.read(fd, MAX_MANIFEST + 1)
    require(sha(raw) == digest, "trusted_manifest_digest_mismatch")
    def unique(pairs):
        out = {}
        for k, v in pairs:
            require(k not in out, "duplicate_json_key")
            out[k] = v
        return out
    m = json.loads(raw, object_pairs_hook=unique, parse_constant=lambda _: require(False, "json_constant_rejected"))
    expected = {"schema", "engine_version", "scope", "roots", "required_roles", "quiescence", "files",
                "excluded", "total_bytes", "activation", "sends", "replay", "live_pipeline_recovery_proven"}
    require(type(m) is dict and set(m) == expected and m["schema"] == SCHEMA and
            m["scope"] == "declared_profile_closed" and m["engine_version"] == __version__ and
            all(m[k] is False for k in ("activation", "sends", "replay", "live_pipeline_recovery_proven")) and
            m["required_roles"] == sorted(REQUIRED_ROLES), "manifest_schema_invalid")
    require(type(m["roots"]) is list and 1 <= len(m["roots"]) <= 16, "manifest_roots_invalid")
    roots = [_absolute(r) for r in m["roots"]]
    require(len(set(roots)) == len(roots) and not any(_inside(a,b) for a in roots for b in roots if a != b),
            "manifest_roots_invalid")
    q = m["quiescence"]
    require(type(q) is dict and set(q) == {"basis","observed_utc","evidence_sha256",
            "caller_confirmed_all_writers_stopped","independently_verified_by_library"} and
            q["basis"] == "independent_all_writer_audit" and _digest(q["evidence_sha256"]) and
            type(q["observed_utc"]) is str and q["observed_utc"].endswith("+00:00") and
            q["caller_confirmed_all_writers_stopped"] is True and q["independently_verified_by_library"] is False,
            "manifest_quiescence_invalid")
    datetime.fromisoformat(q["observed_utc"])
    require(type(m["files"]) is list and type(m["excluded"]) is list and
            1 <= len(m["files"]) + len(m["excluded"]) <= MAX_ENTRIES, "manifest_entry_bound")
    names, sources, roles, total, databases = set(), set(), set(), 0, []
    for row in m["files"]:
        fields = {"name","path","source","roles","kind","sha256","bytes","required_tables","schema_sha256","reference_sql"}
        require(type(row) is dict and set(row) == fields and type(row["name"]) is str and
                NAME.fullmatch(row["name"]) and row["path"] == "payload/" + row["name"], "manifest_member_invalid")
        p = _absolute(row["source"])
        require(row["name"] not in names and p not in sources and
                sum(_inside(p,r) for r in roots) == 1 and p not in roots, "manifest_duplicate_or_outside_store")
        names.add(row["name"]); sources.add(p)
        require(not _credential(p) and type(row["roles"]) is list and row["roles"] and
                len(set(row["roles"])) == len(row["roles"]) and set(row["roles"]) <= ROLES and
                row["kind"] in ("file","sqlite") and _digest(row["sha256"]) and
                type(row["bytes"]) is int and 0 <= row["bytes"] <= MAX_FILE, "manifest_member_invalid")
        roles.update(row["roles"]); total += row["bytes"]
        if "original_blob" in row["roles"]:
            require(p.name == row["sha256"], "original_content_address_mismatch")
        if row["kind"] == "sqlite":
            _tables(row["required_tables"]); _queries(row["reference_sql"])
            require(_digest(row["schema_sha256"]) and row["reference_sql"], "manifest_database_invalid")
            if "cursor" in row["roles"]:
                require("mail_checkpoints" in row["required_tables"], "cursor_table_required")
            databases.append(p)
        else:
            require(row["required_tables"] == [] and row["reference_sql"] == [] and row["schema_sha256"] == "",
                    "manifest_file_schema_invalid")
    require(REQUIRED_ROLES <= roles and len(databases) >= 2 and total <= MAX_TOTAL and
            type(m["total_bytes"]) is int and total == m["total_bytes"], "manifest_scope_or_totals_invalid")
    for row in m["excluded"]:
        require(type(row) is dict and set(row) == {"name","source","reason"} and type(row["name"]) is str and
                NAME.fullmatch(row["name"]) and row["reason"] in EXCLUSIONS, "manifest_exclusion_invalid")
        p = _absolute(row["source"])
        require(row["name"] not in names and p not in sources and sum(_inside(p,r) for r in roots) == 1 and p not in roots,
                "manifest_duplicate_or_outside_store")
        names.add(row["name"]); sources.add(p)
        require(not _credential(p) or row["reason"] == "credential", "credential_classification_required")
        if row["reason"] == "sqlite_sidecar":
            require(any(str(p) == str(db) + suffix for db in databases for suffix in ("-wal","-shm","-journal")),
                    "unpaired_sqlite_sidecar")
    return m


def verify_v2(source, *, manifest_sha256):
    """No historical source path is opened; exact trusted digest is mandatory."""
    started = time.monotonic()
    try:
        source = _absolute(source)
        inventory = _inventory(source)
        m = _manifest(source, manifest_sha256)
        expected = {str(source), str(source / "payload"), str(source / "manifest.json")}
        expected.update(str(source / row["path"]) for row in m["files"])
        require(set(inventory) == expected, "unclassified_backup_member")
        references = []
        for row in m["files"]:
            require(time.monotonic() - started <= OPERATION_SECONDS, "operation_timeout")
            target = source / row["path"]
            with source_fd(target) as (fd, info):
                h = hashlib.sha256()
                while block := os.read(fd, 1024 * 1024):
                    require(time.monotonic() - started <= OPERATION_SECONDS, "operation_timeout")
                    h.update(block)
                require(info.st_size == row["bytes"] and h.hexdigest() == row["sha256"], "member_hash_or_size_mismatch")
            if row["kind"] == "sqlite":
                references.extend(_db_check(target, row, started + OPERATION_SECONDS))
        _closure(m["files"], references)
        require(inventory == _inventory(source), "backup_changed_during_verification")
        return {"schema": SCHEMA, "manifest_sha256": manifest_sha256, "files": len(m["files"]),
                "sqlite_snapshots": sum(r["kind"] == "sqlite" for r in m["files"]),
                "declared_scope_closed": True, "live_pipeline_recovery_proven": False}
    except RecoveryError:
        raise
    except (OSError, ValueError, TypeError, sqlite3.Error):
        raise RecoveryError("verify_v2_refused") from None


def restore_v2(source, destination, *, manifest_sha256):
    """Fresh target only. Preserves bytes, absolute references and signatures."""
    deadline = time.monotonic() + OPERATION_SECONDS
    try:
        source = _absolute(source)
        # Prevent restoring into an ancestor/descendant of the recovery set.
        target = _separate(destination, (source,))
        report = verify_v2(str(source), manifest_sha256=manifest_sha256)
        _deadline(deadline)
        m = _manifest(source, manifest_sha256)
        target = _separate(destination, (source, *(_absolute(r) for r in m["roots"])))
        _deadline(deadline)
        new_directory(target); new_directory(target / "payload")
        for row in m["files"]:
            _deadline(deadline)
            copy_exact(source / row["path"], target / row["path"],
                       expected={"sha256": row["sha256"], "bytes": row["bytes"]})
            _deadline(deadline)
        # Keep original manifest bytes. No regenerated signatures or rebased paths.
        with source_fd(source / "manifest.json") as (fd, info):
            raw = os.read(fd, MAX_MANIFEST + 1)
        require(sha(raw) == manifest_sha256, "manifest_changed_during_restore")
        _deadline(deadline)
        write_new(target / "manifest.json", raw)
        _deadline(deadline)
        verified = verify_v2(str(target), manifest_sha256=manifest_sha256)
        _deadline(deadline)
        # Completion is signalled only by return, not by activating a runnable tree.
        return {**verified, "isolated": True, "activation": False, "sends": 0, "replay": 0,
                "runnable_reconciliation_proven": False}
    except RecoveryError:
        raise
    except (OSError, ValueError, TypeError, sqlite3.Error):
        raise RecoveryError("restore_v2_refused") from None

