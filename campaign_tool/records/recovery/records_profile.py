"""Reviewed backup_v2 profile for the records project layout (DFGL-12A, BACKUP-FIT-1).

Scope: the pipeline stores. Top-level entries the records pipeline writes (``PIPELINE``, plus credential-named entries,
which stay in scope so they are classified ``credential``) are walked; every other top-level entry is the legacy tree,
declared out of scope, counted (stat only) and recorded in the set as ``out_of_scope_legacy``; it is covered by the
operator's archive, not by this backup. Every file and directory in scope is classified by path rule; anything the
rules do not name makes the profile incomplete rather than guessed.

Off-project stores: ``EXTRA_ROOTS`` is a fixed, reviewed list of store directories outside the project that ledger
rows reference (the retained CAS and evidence, the preservation-extraction evidence). Each one that exists is a
further backup root; only the files a ledger row names inside it are included, everything else there is
``out_of_scope_legacy``. A referenced path that is not an included store (off the project and every declared root, in
the legacy tree, excluded or missing) is counted and refused (REFERENCED_OUTSIDE_SET).
Reads only metadata, SQLite (immutable read-only) and the bytes of small receipt/journal files; never writes, never
touches credentials or sidecar contents.
"""
import hashlib
import os
import pwd
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
# Declared off-project store roots, relative to the caller's home directory (from the password database, never from
# the environment or a ledger value). Fixed and reviewed: a new location is a profile change, not a run-time choice.
EXTRA_ROOTS = ("deflock-private-artifacts/records-project-retained-v1/cas",
               "deflock-private-artifacts/records-project-retained-v1/evidence",
               "deflock-preservation-extraction-20261013-v1/live-evidence-v1",
               "deflock-preservation-extraction-20261013-v1/live-remaining-evidence-v1")
# Ledger values that name derived files an extraction import depends on (its manifest's artifacts, the units it
# enrolled). Not closure-bound by the library; the profile includes them from a declared root and counts any that
# fall outside the set.
DERIVED_SQL = ("SELECT 'derived_resume', json_extract(a.value, '$.path'), NULL "
               "FROM extraction_adapter_imports, json_each(extraction_adapter_imports.manifest_json, '$.artifacts') a",
               "SELECT 'derived_resume', derived_path, NULL FROM units WHERE derived_path IS NOT NULL")
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


def _home():
    return pwd.getpwuid(os.getuid()).pw_dir


def _inside(path, root):
    return path == root or path.startswith(root.rstrip("/") + "/")


def backup_roots(project):
    """The roots a backup of ``project`` reads, in order: the project, then each declared ``EXTRA_ROOTS`` entry that
    exists. Path safety as for the project: absolute, resolved (no symlink anywhere), no ``..``, a real directory,
    neither inside nor around another root. The quiescence evidence must name exactly these roots."""
    root = Path(os.path.abspath(project))
    if root.is_symlink() or not root.is_dir() or ";" in str(root):
        raise ProfileIncomplete("PROFILE_INCOMPLETE")
    roots = [str(root)]
    for rel in EXTRA_ROOTS:
        path = os.path.join(_home(), rel)
        if not os.path.lexists(path):
            continue  # a declared store that does not exist here is not read; a row naming it is still refused
        if (not os.path.isabs(path) or os.path.realpath(path) != path or ".." in Path(path).parts or ";" in path
                or not stat.S_ISDIR(os.lstat(path).st_mode)
                or any(_inside(path, other) or _inside(other, path) for other in roots)):
            raise ProfileIncomplete("PROFILE_INCOMPLETE", reason="extra_root_unsafe")
        roots.append(path)
    return tuple(roots)


def _survey(root, wanted=None):
    """(files, dirs, unreadable, outside) under ``root``, relative paths.

    Project root (``wanted`` None): top-level entries that are not pipeline stores are ``outside``. Declared extra
    root: ``wanted`` is the set of relative file paths ledger rows name there; the walk enters only directories that
    lead to one, and every other entry is ``outside`` (counted, never opened).
    """
    files, dirs, outside, unreadable = [], [], [], 0
    lead = set() if wanted is None else {str(p) for rel in wanted for p in Path(rel).parents if str(p) != "."}
    stack = [""]
    while stack and len(files) + len(dirs) <= SCAN_LIMIT:
        base = stack.pop()
        with os.scandir(root / base if base else root) as entries:
            for entry in sorted(entries, key=lambda e: e.name):
                rel = base + "/" + entry.name if base else entry.name
                if wanted is None and not base and not _in_scope(entry.name):
                    outside.append(rel)
                elif wanted is not None and rel not in wanted and rel not in lead:
                    outside.append(rel)
                elif entry.is_symlink():
                    unreadable += 1
                elif entry.is_dir(follow_symlinks=False) and (wanted is None or rel in lead):
                    dirs.append(rel); stack.append(rel)
                elif entry.is_file(follow_symlinks=False) and (wanted is None or rel in wanted):
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


def _database(path, rel, tables, queries, derived=()):
    con = None
    try:
        con = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=1)
        con.execute("PRAGMA query_only=ON")
        schema = _pinned_schema(rel, _objects(con))
        have = {row[1] for row in schema if row[0] == "table"}
        if not set(tables) <= have:
            raise ProfileIncomplete("PROFILE_INCOMPLETE", reason="schema_drift")
        refs = [tuple(r) + (True,) for sql in queries for r in con.execute(sql)]
        if rel == LEDGER:  # the library's fixed resolution reader adds these at backup time; count them here too
            refs.extend(tuple(r) + (True,) for r in v2._resolution_refs(con, have))
        refs.extend(tuple(r) + (False,) for sql in derived for r in con.execute(sql))
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
    roots = backup_roots(project)
    root, extras = Path(roots[0]), roots[1:]
    files, dirs, unreadable, outside = _survey(root)
    rules = [_rule(rel) for rel, _ in files]
    found = counts(files, rules, {"directories": len(dirs), "unreadable": unreadable,
                                  "out_of_scope_legacy": _legacy(root, outside),
                                  "root_owner": "caller" if os.lstat(root).st_uid == os.getuid() else "other"})
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
    # Originals with no stored bytes (NULL storage_path: legacy declarations) have no file to close over. An
    # extraction import's receipt is its manifest's source_receipt artifact, wherever the import stored it.
    queries = {LEDGER: ["SELECT 'original_blob', storage_path, sha256 FROM originals WHERE storage_path IS NOT NULL",
                        "SELECT 'receipt', json_extract(manifest_json, '$.artifacts.source_receipt.path'), "
                        "receipt_sha256 FROM extraction_adapter_imports",
                        *("SELECT 'review_bundle', %s, %s" % (_lit(base + "/" + rel), _lit(d)) for rel, d in digests.items())],
               INTAKE: ["SELECT 'original_blob', path, sha FROM preservations"],
               PORTAL: ["SELECT 'original_blob', %s || sha256, sha256 FROM portal_versions"
                        % _lit(base + "/portal-queue-v1/objects/")]}
    meta, outside_refs, wanted = {}, {}, {x: {} for x in extras}

    def refuse_ref(role, where):
        outside_refs[role + " " + where] = outside_refs.get(role + " " + where, 0) + 1

    for rel in (LEDGER, INTAKE, PORTAL):
        if rel in roles:
            try:
                have, schema, refs = _database(root / rel, rel, TABLES[rel], queries[rel],
                                               DERIVED_SQL if rel == LEDGER else ())
            except ProfileIncomplete as error:
                raise ProfileIncomplete(error.code, found, error.reason) from None
            meta[rel] = (have, schema)
            for role, source, _digest, bound in refs:
                if type(source) is not str or not os.path.isabs(source) or os.path.normpath(source) != source:
                    refuse_ref(role, "<unbound>")
                    continue
                if source.startswith(base + "/"):
                    key = source[len(base) + 1:]
                    if key in roles:
                        if bound and role not in roles[key]:
                            roles[key].append(role)
                        continue
                    refuse_ref(role, pattern(key) + ("" if os.path.lexists(root / key) else " (missing)"))
                    continue
                home = next((x for x in extras if source.startswith(x + "/")), None)
                if home is None:  # off the project and every declared root: never print the path, only its tail
                    refuse_ref(role, "<off-project>/" + pattern("/".join(source.split("/")[-3:])))
                    continue
                assigned = wanted[home].setdefault(source[len(home) + 1:], [])
                if role not in assigned:
                    assigned.append(role)
    # Declared extra roots: only the files ledger rows name are included; the rest is counted as legacy.
    xfiles, xdirs, xoutside, legacy = [], [], [], dict(found["out_of_scope_legacy"])
    summary = {"roots": len(extras), "owner_other": 0, "files": 0, "bytes": 0}
    for index, x in enumerate(extras):
        names = wanted[x]
        got, got_dirs, bad, out = _survey(Path(x), set(names))
        summary["owner_other"] += os.lstat(x).st_uid != os.getuid()
        unreadable += bad
        present = {rel for rel, _ in got}
        for rel, assigned in sorted(names.items()):
            if rel not in present:
                for role in assigned:
                    refuse_ref(role, "<extra-root-%d>/%s (missing)" % (index + 1, pattern(rel)))
        for rel, size in got:
            xfiles.append((x, rel, size, names[rel]))
            summary["files"] += 1
            summary["bytes"] += size
        xdirs.extend(x + "/" + d for d in got_dirs)
        xoutside.extend(x + "/" + o for o in out)
        for key, value in _legacy(Path(x), out).items():
            legacy[key] = (legacy.get(key) or 0) + value if type(value) is int else value
    found["out_of_scope_legacy"] = legacy
    if extras:
        found["extra_roots"] = summary
    found["included_bytes"] += summary["bytes"]
    found["unreadable"] = unreadable
    # Conditional roles: present when their source is, declared absent only when the source does not exist at all.
    absent = []
    if LEDGER in roles:
        if "mail_checkpoints" in meta[LEDGER][0]:
            roles[LEDGER].append("cursor")
        else:
            absent.append("cursor")
    if not any(rel in roles for rel in JOURNALS) and not any(os.path.lexists(root / rel) for rel in JOURNALS):
        absent.append("review_bundle")
    found["roles"] = {}
    for assigned in (*roles.values(), *(entry[3] for entry in xfiles)):
        for role in assigned:
            found["roles"][role] = found["roles"].get(role, 0) + 1
    found["absent_roles"] = {role: v2.ABSENT_REASON for role in absent}
    found["referenced_outside_set"] = sum(outside_refs.values())
    missing = sorted(v2.REQUIRED_ROLES - set(found["roles"]) - set(absent))
    if missing:
        found["missing_roles"] = missing
    if detail is not None:
        detail["referenced_outside"] = outside_refs
    if report is not None:
        report.update(found)
    if (len(files) + len(dirs) + len(xfiles) + len(xdirs) > SCAN_LIMIT
            or len(files) + len(outside) + len(xfiles) + len(xoutside) > MAX_ENTRIES):
        raise ProfileIncomplete("LIMIT_ENTRIES", found)
    if unreadable:
        raise ProfileIncomplete("PROFILE_INCOMPLETE", found)
    if found["included_bytes"] > MAX_TOTAL or any(entry[2] > MAX_FILE for entry in xfiles):
        raise ProfileIncomplete("LIMIT_BYTES", found)
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
            hashed = set(roles[rel]) & {"receipt", "review_bundle", "resolution_policy"}
            digest = digests.get(rel) or (hashlib.sha256((root / rel).read_bytes()).hexdigest() if hashed else "")
            stores.append(v2.Store(name, source, tuple(roles[rel]), expected_sha256=digest))
    for index, (x, rel, _size, assigned) in enumerate(xfiles):
        source = x + "/" + rel
        digest = hashlib.sha256(Path(source).read_bytes()).hexdigest() if "receipt" in assigned else ""
        stores.append(v2.Store("x%05d" % index, source, tuple(assigned), expected_sha256=digest))
    return v2.Profile(roots, tuple(stores), (*(base + "/" + d for d in dirs), *xdirs),
                      (*(base + "/" + o for o in outside), *xoutside), tuple(sorted(absent)))
