"""Reviewed backup_v2 profile for the records project layout (DFGL-12A).

Every file and directory under the project is classified by path rule; anything the rules do not name makes the
profile incomplete rather than guessed. Reads only metadata, SQLite (immutable read-only) and the bytes of small
receipt/journal files; never writes, never touches credentials or sidecar contents.
"""
import hashlib
import os
import re
import sqlite3
from pathlib import Path

from . import backup_v2 as v2
from .backup import MAX_ENTRIES, MAX_FILE, MAX_TOTAL

HEX = re.compile(r"[0-9a-f]{64}")
LEDGER, INTAKE, PORTAL = ("processing-v1/ledger.sqlite", "processing-v1/intake/intake.sqlite",
                          "portal-queue-v1/portal.sqlite")
JOURNALS = ("approvals/queue.jsonl", "public-packets.jsonl")
TABLES = {LEDGER: ("originals", "receipts", "occurrences", "mail_messages", "stage_state", "stage_artifacts",
                   "extraction_adapter_imports", "ledger_meta", "schema_migrations"),
          INTAKE: ("docs", "occurrences", "preservations", "meta"),
          PORTAL: ("portal_items", "portal_versions", "portal_meta")}
DB_ROLES = {LEDGER: ("canonical_ledger",), INTAKE: ("intake_ledger",), PORTAL: ("queue_store",)}
PROCESS = {".lock", "run.lock", "writer.lock", "public-packets.lock", "public-packets.KILL", ".t12-sandbox-v1"}
SCAN_LIMIT = MAX_ENTRIES * 4


class ProfileIncomplete(Exception):
    def __init__(self, code, counts=None):
        super().__init__(code)
        self.code, self.counts = code, counts or {}


def _rule(rel):
    """(included, roles, exclusion) for one project-relative file path, or None when no rule names it."""
    parts, name = rel.split("/"), rel.rsplit("/", 1)[-1]
    if v2._credential(Path(rel)):
        return False, (), "credential"
    if name.endswith(("-wal", "-shm", "-journal")) and rel.rsplit("-", 1)[0] in DB_ROLES:
        return False, (), "sqlite_sidecar"
    if name in PROCESS:
        return False, (), "process_state"
    if rel == "run-report.json" or parts[0] == "run-outputs" or rel.startswith("processing-v1/runs/"):
        return False, (), "rebuildable"
    if rel in DB_ROLES:
        return True, DB_ROLES[rel], ""
    named = len(parts) > 1 and HEX.fullmatch(name) is not None
    for prefix, role in (("processing-v1/intake/blobs/", "original_blob"), ("portal-queue-v1/objects/", "original_blob"),
                         ("processing-v1/intake/verification-receipts/", "receipt"),
                         ("processing-v1/intake/local-capture-receipts/", "receipt"),
                         ("processing-v1/intake/resolution-policies/", "resolution_policy")):
        if rel.startswith(prefix) and rel.count("/") == prefix.count("/"):
            return (True, (role,), "") if named else None
    if rel in JOURNALS:
        return True, ("review_bundle",), ""
    if re.fullmatch(r"processing-v1/mail/[0-9a-f]{64}/receipt\.json", rel):
        return True, ("receipt",), ""
    for prefix, role in (("processing-v1/mail/", "derived_resume"), ("processing-v1/extract/", "derived_resume"),
                         ("processing-v1/ocr/", "derived_resume"), ("processing-v1/artifacts/evidence/", "derived_resume"),
                         ("processing-v1/outbox/", "queue_store"), ("processing-v1/proposals/", "publication_store"),
                         ("site-changes/", "publication_store")):
        if rel.startswith(prefix):
            return True, (role,), ""
    if rel in ("processing-v1/awaiting-ocr.jsonl",):
        return True, ("queue_store",), ""
    if rel == "public-packets.json":
        return True, ("publication_store",), ""
    return None


def _survey(root):
    files, dirs, unreadable = [], [], 0
    stack = [""]
    while stack and len(files) + len(dirs) <= SCAN_LIMIT:
        base = stack.pop()
        with os.scandir(root / base if base else root) as entries:
            for entry in sorted(entries, key=lambda e: e.name):
                rel = base + "/" + entry.name if base else entry.name
                if entry.is_symlink():
                    unreadable += 1
                elif entry.is_dir(follow_symlinks=False):
                    dirs.append(rel); stack.append(rel)
                elif entry.is_file(follow_symlinks=False):
                    files.append((rel, entry.stat(follow_symlinks=False).st_size))
                else:
                    unreadable += 1
    return files, dirs, unreadable


def _lit(text):
    return "'" + text.replace("'", "''") + "'"


def _database(path, tables, queries):
    con = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=1)
    try:
        con.execute("PRAGMA query_only=ON")
        schema = [list(r) for r in con.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name")]
        have = {r[1] for r in schema if r[0] == "table"}
        if not set(tables) <= have:
            raise ProfileIncomplete("PROFILE_INCOMPLETE")
        refs = [tuple(r) for sql in queries for r in con.execute(sql)]
        return have, v2.sha(v2.canonical(schema)), refs
    finally:
        con.close()


def counts(files, rules, extra=None):
    out = {"files": len(files), "roles": {}, "excluded": {}, "unclassified": 0, "limits": {
        "entries": MAX_ENTRIES, "total_bytes": MAX_TOTAL, "member_bytes": MAX_FILE}, **(extra or {})}
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


def build_profile(project, report=None):
    """The Profile for a records project; ProfileIncomplete(code, counts) when coverage or limits fail."""
    root = Path(os.path.abspath(project))
    if root.is_symlink() or not root.is_dir() or ";" in str(root):
        raise ProfileIncomplete("PROFILE_INCOMPLETE")
    files, dirs, unreadable = _survey(root)
    rules = [_rule(rel) for rel, _ in files]
    found = counts(files, rules, {"directories": len(dirs), "unreadable": unreadable})
    if report is not None:
        report.update(found)
    if len(files) + len(dirs) > SCAN_LIMIT or len(files) > MAX_ENTRIES:
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
    queries = {LEDGER: ["SELECT 'original_blob', storage_path, sha256 FROM originals",
                        "SELECT 'receipt', %s || receipt_sha256, receipt_sha256 FROM extraction_adapter_imports"
                        % _lit(base + "/processing-v1/artifacts/evidence/"),
                        *("SELECT 'review_bundle', %s, %s" % (_lit(base + "/" + rel), _lit(d)) for rel, d in digests.items())],
               INTAKE: ["SELECT 'original_blob', path, sha FROM preservations"],
               PORTAL: ["SELECT 'original_blob', %s || sha256, sha256 FROM portal_versions"
                        % _lit(base + "/portal-queue-v1/objects/")]}
    meta = {}
    for rel in (LEDGER, INTAKE, PORTAL):
        if rel in roles:
            have, schema, refs = _database(root / rel, TABLES[rel], queries[rel])
            meta[rel] = (have, schema)
            for role, source, _digest in refs:
                key = source[len(base) + 1:] if source.startswith(base + "/") else None
                if key in roles and role not in roles[key]:
                    roles[key].append(role)
    if LEDGER in roles and "mail_checkpoints" in meta[LEDGER][0]:
        roles[LEDGER].append("cursor")
    if LEDGER not in roles or INTAKE not in roles or not v2.REQUIRED_ROLES <= {r for v in roles.values() for r in v}:
        raise ProfileIncomplete("PROFILE_INCOMPLETE", found)
    found["roles"] = {}
    for assigned in roles.values():
        for role in assigned:
            found["roles"][role] = found["roles"].get(role, 0) + 1
    if report is not None:
        report.update(found)
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
    return v2.Profile((base,), tuple(stores), tuple(base + "/" + d for d in dirs))
