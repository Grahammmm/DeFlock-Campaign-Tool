"""Reviewed backup_v2 profile for the records project layout (DFGL-12A, BACKUP-FIT-1).

Scope: the pipeline stores. Top-level entries the records pipeline writes (``PIPELINE``, plus credential-named entries,
which stay in scope so they are classified ``credential``) are walked; every other top-level entry is the legacy tree,
declared out of scope, counted (stat only) and recorded in the set as ``out_of_scope_legacy``; it is covered by the
operator's archive, not by this backup. Every file and directory in scope is classified by path rule; anything the
rules do not name makes the profile incomplete rather than guessed. A path a ledger row references that is not an
included store (off the project, in the legacy tree or missing) is counted and refused (REFERENCED_OUTSIDE_SET).
Reads only metadata, SQLite (immutable read-only) and the bytes of small receipt/journal files; never writes, never
touches credentials or sidecar contents.
"""
import hashlib
import os
import re
import sqlite3
import stat
from contextlib import closing
from functools import lru_cache
from pathlib import Path

from . import backup_v2 as v2
from .backup import MAX_ENTRIES, MAX_FILE, MAX_TOTAL
from .. import extraction_ledger
from ..intake import folder, imap_intake
from ..ledger.migrations import (occurrence_projection_v001, original_resolution_v001, stages_authority_v001,
                                 stages_v001, unit_projection_guards_v001, unit_projection_v001, v001)
from ..portal import collections as portal_collections, store as portal_store

HEX = re.compile(r"[0-9a-f]{64}")
LEDGER, INTAKE, PORTAL = ("processing-v1/ledger.sqlite", "processing-v1/intake/intake.sqlite",
                          "portal-queue-v1/portal.sqlite")
JOURNALS = ("approvals/queue.jsonl", "public-packets.jsonl")
TABLES = {LEDGER: ("originals", "receipts", "occurrences", "mail_messages", "stage_state", "stage_artifacts",
                   "extraction_adapter_imports", "ledger_meta", "schema_migrations"),
          INTAKE: ("docs", "occurrences", "preservations", "meta"),
          PORTAL: ("portal_items", "portal_versions", "portal_meta")}
DB_ROLES = {LEDGER: ("canonical_ledger",), INTAKE: ("intake_ledger",), PORTAL: ("queue_store",)}
PROCESS = {".lock", "run.lock", "writer.lock", ".page.lock", "public-packets.lock", "public-packets.KILL",
           ".t12-sandbox-v1"}
SCAN_LIMIT = MAX_ENTRIES * 4
LEGACY_LIMIT = 2000000  # out-of-scope entries counted (lstat only) before the count stops
PIPELINE = frozenset(("processing-v1", "portal-queue-v1", "run-outputs", "approvals", "site-changes",
                      "public-packets.json", "public-packets.jsonl", "public-packets.lock", "public-packets.KILL",
                      "run-report.json", "run.lock", ".t12-sandbox-v1", ".adoption-config-live-v1.json"))
# Prior per-run copies of the two included databases (run_stages.backup_adoption_dbs: backups/<run>/ holds
# ledger.sqlite, intake.sqlite, their SQLite sidecars and a manifest; rotated to the newest BACKUP_KEEP runs).
PRIOR_BACKUP = re.compile(r"processing-v1/backups/[^/]+/((ledger|intake)\.sqlite(-wal|-shm|-journal)?|manifest\.json)")
# extraction_routes.extract makes a fresh mkdtemp ``extract-*`` work dir per call and nothing opens it after that call
# (the validator never opens run_path): enrollment has already copied an accepted extraction's receipt and derivatives
# into artifacts/evidence, and a blocked one is redone by the next extract stage in a new work dir from the original
# blob and the OCR page receipts (both included).
EXTRACT_WORK = re.compile(r"processing-v1/extract/extract-[^/]+/.+")
# Reviewed schemas, rebuilt from the migration modules (never from the database being backed up): per store an ordered
# (group, required, scripts) list. A group is every object its scripts add to a private in-memory reference; a database
# is supported only when it holds each required group exactly, any optional group exactly or not at all, and nothing else.
SCHEMAS = {LEDGER: (("base", True, (v001.SQL,)), ("stages", True, (stages_v001.SQL,)),
                    ("authority", True, (stages_authority_v001.SQL,)),
                    ("occurrence_projection", False, (occurrence_projection_v001.SQL,)),
                    ("original_resolution", False, (original_resolution_v001.SQL,)),
                    ("unit_projection", False, (unit_projection_v001.SQL, unit_projection_guards_v001.SQL)),
                    ("extraction_adapter", True, (extraction_ledger.SCHEMA,)),
                    ("mail_checkpoints", False, (imap_intake.CHECKPOINTS_SQL,))),
           INTAKE: (("intake", True, (folder.SCHEMA,)),),
           PORTAL: (("portal_queue", True, (portal_store.SCHEMA,)),
                    ("export_inventory", False, (portal_collections.SCHEMA,)))}


class ProfileIncomplete(Exception):
    def __init__(self, code, counts=None, reason=None):
        super().__init__(code)
        self.code, self.counts, self.reason = code, counts or {}, reason


def _rule(rel):
    """(included, roles, exclusion) for one project-relative file path, or None when no rule names it."""
    parts, name = rel.split("/"), rel.rsplit("/", 1)[-1]
    if v2._credential(Path(rel)):
        return False, (), "credential"
    if name.endswith(("-wal", "-shm", "-journal")) and rel.rsplit("-", 1)[0] in DB_ROLES:
        return False, (), "sqlite_sidecar"
    if name in PROCESS:
        return False, (), "process_state"
    if PRIOR_BACKUP.fullmatch(rel):
        return False, (), "prior_backup"
    if (rel == "run-report.json" or parts[0] == "run-outputs" or rel.startswith("processing-v1/runs/")
            or EXTRACT_WORK.fullmatch(rel)):
        return False, (), "rebuildable"
    if rel in DB_ROLES:
        return True, DB_ROLES[rel], ""
    named = len(parts) > 1 and HEX.fullmatch(name) is not None
    for prefix, role in (("processing-v1/intake/blobs/", "original_blob"), ("portal-queue-v1/objects/", "original_blob"),
                         ("processing-v1/intake/verification-receipts/", "receipt"),
                         ("processing-v1/intake/local-capture-receipts/", "receipt"),
                         ("processing-v1/intake/document-receipts/", "receipt"),
                         ("processing-v1/intake/resolution-policies/", "resolution_policy")):
        if rel.startswith(prefix) and rel.count("/") == prefix.count("/"):
            return (True, (role,), "") if named else None
    if rel in JOURNALS:
        return True, ("review_bundle",), ""
    if re.fullmatch(r"processing-v1/mail/[0-9a-f]{64}/receipt\.json", rel):
        return True, ("receipt",), ""
    for prefix, role in (("processing-v1/mail/", "derived_resume"), ("processing-v1/extract/", "derived_resume"),
                         ("processing-v1/ocr/", "derived_resume"), ("processing-v1/artifacts/evidence/", "derived_resume"),
                         ("processing-v1/context-v1/", "derived_resume"),
                         ("processing-v1/outbox/", "queue_store"), ("processing-v1/proposals/", "publication_store"),
                         ("site-changes/", "publication_store")):
        if rel.startswith(prefix):
            return True, (role,), ""
    if re.fullmatch(r"processing-v1/captured-inputs/[0-9a-f]{64}\.sqlite", rel):  # ledger.store.import_legacy input
        return True, ("derived_resume",), ""
    if rel in ("processing-v1/.adoption-disabled-v1.json", ".adoption-config-live-v1.json"):  # mail_adoption controls
        return True, ("configuration",), ""
    if rel in ("processing-v1/.activation-intent-v1.json", "processing-v1/.bound-trial-v1.json",
               "portal-queue-v1/.canonical-binding-v1.json"):  # markers with no writer in this package: kept
        return True, ("derived_resume",), ""
    if rel in ("processing-v1/awaiting-ocr.jsonl",):
        return True, ("queue_store",), ""
    if rel == "public-packets.json":
        return True, ("publication_store",), ""
    return None


def _in_scope(name):
    """A top-level project entry the pipeline owns (credential-named entries stay in scope to be classified)."""
    return name in PIPELINE or v2._credential(Path(name))


def _survey(root):
    files, dirs, outside, unreadable = [], [], [], 0
    stack = [""]
    while stack and len(files) + len(dirs) <= SCAN_LIMIT:
        base = stack.pop()
        with os.scandir(root / base if base else root) as entries:
            for entry in sorted(entries, key=lambda e: e.name):
                rel = base + "/" + entry.name if base else entry.name
                if not base and not _in_scope(entry.name):
                    outside.append(rel)
                elif entry.is_symlink():
                    unreadable += 1
                elif entry.is_dir(follow_symlinks=False):
                    dirs.append(rel); stack.append(rel)
                elif entry.is_file(follow_symlinks=False):
                    files.append((rel, entry.stat(follow_symlinks=False).st_size))
                else:
                    unreadable += 1
    return files, dirs, unreadable, outside


def _legacy(root, names):
    """Counts (lstat only, no symlink followed, nothing opened) of the out-of-scope legacy tree."""
    out = {"entries": len(names), "files": 0, "bytes": 0}
    stack, seen = [str(root / n) for n in names], 0
    while stack and seen < LEGACY_LIMIT:
        path = stack.pop()
        seen += 1
        try:
            info = os.lstat(path)
            if stat.S_ISDIR(info.st_mode):
                with os.scandir(path) as entries:
                    stack.extend(entry.path for entry in entries)
            elif stat.S_ISREG(info.st_mode):
                out["files"] += 1
                out["bytes"] += info.st_size
        except OSError:
            out["unreadable"] = out.get("unreadable", 0) + 1
    if stack:
        out["count_stopped_at_bound"] = True
    return out


def _collapse(part):
    """One path component with hash-like, run, temporary and numeric parts collapsed (names only, never contents)."""
    m = re.fullmatch(r"[0-9a-f]{64}(\..+)?", part)
    if m:
        return "<sha>" + (m.group(1) or "")
    if re.fullmatch(r"[0-9a-f]{2}", part):
        return "<hh>"
    part = re.sub(r"run_\d{8}T\d{6}Z_[0-9a-f]+", "<run>", part)
    part = re.sub(r"^(extract-|\.[a-z]+-)[a-z0-9_]{8}$", r"\1<tmp>", part)
    part = re.sub(r"[0-9a-f]{16,}", "<sha>", part)
    return re.sub(r"\d{2,}", "<n>", part)


def pattern(rel):
    return "/".join(_collapse(part) for part in rel.split("/"))


def _lit(text):
    return "'" + text.replace("'", "''") + "'"


def _objects(con):
    return {(r[0], r[1]): (r[2], r[3]) for r in con.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}


@lru_cache(maxsize=None)
def _reference(rel):
    """{group: {(type, name): (table, sql)}} for one store, built only from the reviewed migration scripts."""
    groups = {}
    with closing(sqlite3.connect(":memory:")) as ref:
        for group, _required, scripts in SCHEMAS[rel]:
            before = _objects(ref)
            for script in scripts:
                ref.executescript(script)
            groups[group] = {key: value for key, value in _objects(ref).items() if key not in before}
    return groups


def _pinned_schema(rel, actual):
    """The reviewed schema rows of a supported database; drift (any missing, changed or extra object) is refused."""
    reference, schema = _reference(rel), {}
    if set(actual) - {key for objects in reference.values() for key in objects}:
        raise ProfileIncomplete("PROFILE_INCOMPLETE", reason="schema_drift")
    for group, required, _scripts in SCHEMAS[rel]:
        expected = reference[group]
        if any(key in actual for key in expected):
            if any(actual.get(key) != value for key, value in expected.items()):
                raise ProfileIncomplete("PROFILE_INCOMPLETE", reason="schema_drift")
        elif required:
            raise ProfileIncomplete("PROFILE_INCOMPLETE", reason="schema_drift")
        else:
            continue
        schema.update(expected)
    return [[kind, name, table, sql] for (kind, name), (table, sql) in sorted(schema.items())]


def _database(path, rel, tables, queries):
    con = None
    try:
        con = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=1)
        con.execute("PRAGMA query_only=ON")
        schema = _pinned_schema(rel, _objects(con))
        have = {row[1] for row in schema if row[0] == "table"}
        if not set(tables) <= have:
            raise ProfileIncomplete("PROFILE_INCOMPLETE", reason="schema_drift")
        refs = [tuple(r) for sql in queries for r in con.execute(sql)]
        if rel == LEDGER:  # the library's fixed resolution reader adds these at backup time; count them here too
            refs.extend(v2._resolution_refs(con, have))
        return have, v2.sha(v2.canonical(schema)), refs
    except sqlite3.Error:  # corrupt or unreadable store: a fixed code, never the driver's message or any path
        raise ProfileIncomplete("PROFILE_INCOMPLETE", reason="database_unreadable") from None
    finally:
        if con is not None:
            con.close()


def counts(files, rules, extra=None):
    out = {"files": len(files), "roles": {}, "excluded": {}, "unclassified": 0, "limits": {
        "entries": MAX_ENTRIES, "scan_entries": SCAN_LIMIT, "total_bytes": MAX_TOTAL, "member_bytes": MAX_FILE},
        **(extra or {})}
    total = 0
    for (rel, size), rule in zip(files, rules):
        if rule is None:
            out["unclassified"] += 1
        elif rule[0]:
            total += size
            for role in rule[1]:
                out["roles"][role] = out["roles"].get(role, 0) + 1
        else:
            out["excluded"][rule[2]] = out["excluded"].get(rule[2], 0) + 1
    out["included_bytes"] = total
    return out


def build_profile(project, report=None, detail=None):
    """The Profile for a records project; ProfileIncomplete(code, counts) when coverage or limits fail.

    ``detail`` (optional dict) receives ``unclassified`` {pattern: [files, bytes]} and ``referenced_outside``
    {"role pattern": count}: collapsed names and counts only, for ``--explain``.
    """
    root = Path(os.path.abspath(project))
    if root.is_symlink() or not root.is_dir() or ";" in str(root):
        raise ProfileIncomplete("PROFILE_INCOMPLETE")
    files, dirs, unreadable, outside = _survey(root)
    rules = [_rule(rel) for rel, _ in files]
    found = counts(files, rules, {"directories": len(dirs), "unreadable": unreadable,
                                  "out_of_scope_legacy": _legacy(root, outside)})
    if detail is not None:
        families = detail.setdefault("unclassified", {})
        for (rel, size), rule in zip(files, rules):
            if rule is None:
                entry = families.setdefault(pattern(rel), [0, 0])
                entry[0] += 1
                entry[1] += size
    if report is not None:
        report.update(found)
    if len(files) + len(dirs) > SCAN_LIMIT or len(files) + len(outside) > MAX_ENTRIES:
        raise ProfileIncomplete("LIMIT_ENTRIES", found)
    if found["unclassified"] or unreadable:
        raise ProfileIncomplete("PROFILE_INCOMPLETE", found)
    if found["included_bytes"] > MAX_TOTAL or any(size > MAX_FILE for _, size in files):
        raise ProfileIncomplete("LIMIT_BYTES", found)
    base = str(root)
    roles = {rel: list(rule[1]) for (rel, _), rule in zip(files, rules) if rule[0]}
    digests = {}
    for rel in JOURNALS:  # journals are not database rows; the profile binds each by a pinned closure row
        if rel in roles:
            digests[rel] = hashlib.sha256((root / rel).read_bytes()).hexdigest()
    # Originals with no stored bytes (NULL storage_path: legacy declarations) have no file to close over.
    queries = {LEDGER: ["SELECT 'original_blob', storage_path, sha256 FROM originals WHERE storage_path IS NOT NULL",
                        "SELECT 'receipt', %s || receipt_sha256, receipt_sha256 FROM extraction_adapter_imports"
                        % _lit(base + "/processing-v1/artifacts/evidence/"),
                        *("SELECT 'review_bundle', %s, %s" % (_lit(base + "/" + rel), _lit(d)) for rel, d in digests.items())],
               INTAKE: ["SELECT 'original_blob', path, sha FROM preservations"],
               PORTAL: ["SELECT 'original_blob', %s || sha256, sha256 FROM portal_versions"
                        % _lit(base + "/portal-queue-v1/objects/")]}
    meta, outside_refs = {}, {}
    for rel in (LEDGER, INTAKE, PORTAL):
        if rel in roles:
            try:
                have, schema, refs = _database(root / rel, rel, TABLES[rel], queries[rel])
            except ProfileIncomplete as error:
                raise ProfileIncomplete(error.code, found, error.reason) from None
            meta[rel] = (have, schema)
            for role, source, _digest in refs:
                key = source[len(base) + 1:] if type(source) is str and source.startswith(base + "/") else None
                if key in roles:
                    if role not in roles[key]:
                        roles[key].append(role)
                    continue
                if key is None:  # off the project: never print the path, only its collapsed tail
                    where = "<off-project>/" + pattern("/".join(str(source).split("/")[-3:]))
                else:
                    where = pattern(key) + ("" if os.path.lexists(root / key) else " (missing)")
                outside_refs[role + " " + where] = outside_refs.get(role + " " + where, 0) + 1
    if LEDGER in roles and "mail_checkpoints" in meta[LEDGER][0]:
        roles[LEDGER].append("cursor")
    found["roles"] = {}
    for assigned in roles.values():
        for role in assigned:
            found["roles"][role] = found["roles"].get(role, 0) + 1
    found["referenced_outside_set"] = sum(outside_refs.values())
    missing = sorted(v2.REQUIRED_ROLES - set(found["roles"]))
    if missing:
        found["missing_roles"] = missing
    if detail is not None:
        detail["referenced_outside"] = outside_refs
    if report is not None:
        report.update(found)
    if outside_refs:  # a referenced path the set would not hold: the backup refuses rather than leave it behind
        raise ProfileIncomplete("REFERENCED_OUTSIDE_SET", found)
    if missing:
        raise ProfileIncomplete("PROFILE_INCOMPLETE", found, "required_role_missing")
    stores = []
    for index, ((rel, _), rule) in enumerate(zip(files, rules)):
        name, source = "f%04d" % index, base + "/" + rel
        if not rule[0]:
            stores.append(v2.Store(name, source, included=False, exclusion=rule[2]))
        elif rel in meta:
            tables = TABLES[rel] + (("mail_checkpoints",) if "cursor" in roles[rel] else ())
            stores.append(v2.Store(name, source, tuple(roles[rel]), "sqlite", required_tables=tables,
                                   schema_sha256=meta[rel][1], reference_sql=tuple(queries[rel])))
        else:
            wanted = set(roles[rel]) & {"receipt", "review_bundle", "resolution_policy"}
            digest = digests.get(rel) or (hashlib.sha256((root / rel).read_bytes()).hexdigest() if wanted else "")
            stores.append(v2.Store(name, source, tuple(roles[rel]), expected_sha256=digest))
    return v2.Profile((base,), tuple(stores), tuple(base + "/" + d for d in dirs), tuple(base + "/" + o for o in outside))
