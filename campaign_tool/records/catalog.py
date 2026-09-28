"""Snapshot an existing private intake ledger into a hash-keyed catalog.

No extraction, mail access, review approval, installation, scheduling or publishing.
All operational paths are explicit private arguments, never campaign defaults.
"""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import tempfile

from .gates.reconcile_coverage import reconcile
from .gates.safe_output import write_private
from .intake.folder import active

SCHEMA_VERSION = 1
STAGE_FIELDS = (
    "complete_substantive_original_level_digestion",
    "independent_source_level_factual_challenge",
    "finding_bound_independent_challenge", "event_date_legal_local_policy",
    "privacy_exact_public_artifact", "publication_readiness", "end_to_end_completion",
)
FORMAT_SCORES = {"pdf": 60, "xlsx": 75, "xls": 75, "csv": 75, "tsv": 75,
                 "docx": 60, "doc": 60, "eml": 45, "zip": 40, "txt": 40,
                 "png": 35, "jpg": 35, "jpeg": 35, "html": 30}


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest_file(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def checked_path(path, *, directory=False):
    path = Path(os.path.abspath(path))
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("symlink input/output is not supported")
    if directory and not path.is_dir() or not directory and not path.is_file():
        raise ValueError("required input path is unavailable")
    return path


def valid_sha(value):
    return isinstance(value, str) and re.fullmatch("[a-f0-9]{64}", value) is not None


def capture_file(path, objects):
    """Capture exact bytes once, refusing mutation during the read."""
    path = checked_path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    target_fd, target_name = tempfile.mkstemp(prefix="capture-", dir=objects)
    temporary = Path(target_name)
    try:
        with os.fdopen(fd, "rb") as source, os.fdopen(target_fd, "wb") as output:
            before = os.fstat(source.fileno())
            h = hashlib.sha256()
            size = 0
            for block in iter(lambda: source.read(1024 * 1024), b""):
                h.update(block)
                size += len(block)
                output.write(block)
            output.flush()
            os.fsync(output.fileno())
            after = os.fstat(source.fileno())
        identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        if identity(before) != identity(after) or size != before.st_size:
            raise ValueError("source changed during capture")
        sha = h.hexdigest()
        target = objects / sha
        if target.exists():
            if digest_file(target) != sha:
                raise ValueError("captured object hash mismatch")
            temporary.unlink()
        else:
            temporary.rename(target)
        return {"source_path": str(path), "sha256": sha, "bytes": size,
                "source_mtime_ns": before.st_mtime_ns, "object": "objects/" + sha}
    finally:
        if temporary.exists():
            temporary.unlink()


def jsonl(path):
    result = []
    with path.open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("JSONL rows must be objects")
            result.append((number, row))
    return result


def index_hashes(rows, label):
    indexed = {}
    for number, row in rows:
        sha = row.get("sha256")
        if not valid_sha(sha) or sha in indexed:
            raise ValueError(label + " has invalid or duplicate hash identities")
        indexed[sha] = (number, row)
    return indexed


def strings(value):
    return [x for x in value if isinstance(x, str)] if isinstance(value, list) else []


def verify_blob(root, sha, expected_size):
    if root is None:
        return "not_checked"
    path = root / sha
    if path.is_symlink():
        return "symlink_rejected"
    if not path.is_file():
        return "missing"
    if path.stat().st_size != expected_size or digest_file(path) != sha:
        return "hash_or_size_mismatch"
    return "verified"


def build_catalog(db, queue_rows, snapshot_rows, digests, *, blob_root=None):
    queue = index_hashes(queue_rows, "queue")
    snapshot = index_hashes(snapshot_rows, "review snapshot")
    docs = list(db.execute("SELECT sha,bytes,format,stage,first_seen,review_status FROM docs ORDER BY sha"))
    if any(not valid_sha(d["sha"]) for d in docs):
        raise ValueError("database has invalid hash identities")
    levels, inventory_run = active(db)
    occurrences = defaultdict(list)
    for row in db.execute("SELECT sha,path,parent,first_seen,last_seen FROM occurrences"):
        occurrences[row["sha"]].append(dict(row))
    preserved = {row[0] for row in db.execute("SELECT sha FROM preservations")}
    exclusions = {row[0] for row in db.execute("SELECT sha FROM scope_exclusions WHERE sha!=''")}
    inventory = []
    for doc in docs:
        q = queue.get(doc["sha"], (None, {}))[1]
        inventory.append({"sha256": doc["sha"], "stage": doc["stage"],
                          "classification": q.get("classification", []),
                          "agency_hints": strings(q.get("agency_hints")),
                          "source_paths": sorted({r["path"] for r in occurrences[doc["sha"]]}),
                          "parents": sorted({r["parent"] for r in occurrences[doc["sha"]] if r["parent"]})})
    coverage, details = reconcile(inventory, digests)
    by_hash = {d["sha256"]: d for d in details}
    cards = []
    for doc, original in zip(docs, inventory):
        sha = doc["sha"]
        q = queue.get(sha, (None, {}))[1]
        snap_line, prior = snapshot.get(sha, (None, {}))
        scoped_out = sha in exclusions or doc["stage"] == "out_of_scope"
        role = prior.get("effective_role", "unresolved")
        role = role if isinstance(role, str) else "unresolved"
        evidence = by_hash[sha]
        flags = []
        if sha not in queue:
            flags.append("absent_from_prior_export_queue")
        if sha not in snapshot:
            flags.append("absent_from_review_snapshot")
        if sha not in levels:
            flags.append("not_in_latest_active_inventory")
        if not original["agency_hints"]:
            flags.append("agency_unassigned")
        flags.append("document_date_unknown")
        if scoped_out:
            flags.append("scope_excluded_identity_only")
        preservation = "scope_excluded_not_opened" if scoped_out else verify_blob(blob_root, sha, doc["bytes"])
        if preservation not in {"verified", "scope_excluded_not_opened"}:
            flags.append("preservation_" + preservation)
        dates = [r["last_seen"] for r in occurrences[sha] if r["last_seen"]]
        score = 0 if scoped_out else 10 if role == "project_artifact" else FORMAT_SCORES.get(doc["format"], 20)
        cards.append({"sha256": sha, "agency_hints": original["agency_hints"] if not scoped_out else [],
            "format": doc["format"], "bytes": doc["bytes"], "role": role,
            "catalog_status": "cataloged", "extraction_stage": doc["stage"],
            "analysis_state": evidence["analysis_state"], "digest_count": evidence["digest_count"],
            "dates": {"first_seen": doc["first_seen"], "last_seen": max(dates) if dates else None,
                      "document_date": None},
            "value_score": score, "value_score_basis": ["provisional_format_and_declared_role_only", "not_a_substantive_value_assessment"],
            "source_paths": [] if scoped_out else original["source_paths"],
            "parents": [] if scoped_out else original["parents"],
            "occurrence_count": len(occurrences[sha]), "flags": flags,
            "preservation": preservation, "preservation_record_present": sha in preserved,
            "snapshot_declarations": {key: prior.get(key, "unknown") if isinstance(prior.get(key, "unknown"), str) else "invalid_declaration" for key in STAGE_FIELDS},
            "snapshot_line": snap_line, "prior_review_status": doc["review_status"],
            "publication_ready": False, "review_receipts_revalidated": False,
            "proposed_low_value": role == "project_artifact", "closed": False})
    hashes = {c["sha256"] for c in cards}
    summary = {"catalog_cards": len(cards), "database_unique_hashes": len(docs),
        "prior_queue_rows": len(queue), "review_snapshot_rows": len(snapshot),
        "active_inventory_hashes": len(levels), "occurrences": sum(c["occurrence_count"] for c in cards),
        "role_counts": dict(Counter(c["role"] for c in cards)),
        "extraction_counts": dict(Counter(c["extraction_stage"] for c in cards)),
        "analysis_counts": dict(Counter(c["analysis_state"] for c in cards)),
        "preservation_counts": dict(Counter(c["preservation"] for c in cards)),
        "snapshot_declared_digestion": dict(Counter(c["snapshot_declarations"][STAGE_FIELDS[0]] for c in cards)),
        "snapshot_declared_independent_source_review": dict(Counter(c["snapshot_declarations"][STAGE_FIELDS[1]] for c in cards)),
        "publication_ready": 0, "closed": 0,
        "reconciled_same_scope": coverage["intake_unique_hashes"] == len(cards),
        "missing_from_prior_queue": len(hashes - set(queue)),
        "prior_queue_outside_database": len(set(queue) - hashes),
        "missing_from_review_snapshot": len(hashes - set(snapshot)),
        "review_snapshot_outside_database": len(set(snapshot) - hashes)}
    return {"schema_version": SCHEMA_VERSION, "inventory_run": inventory_run,
        "summary": summary, "cards": cards,
        "join_gaps": {"missing_from_prior_queue": sorted(hashes - set(queue)),
                      "prior_queue_outside_database": sorted(set(queue) - hashes),
                      "review_snapshot_outside_database": sorted(set(snapshot) - hashes)},
        "limits": ["Cataloged metadata is not substantive review or publication approval.",
                   "Snapshot declarations are preserved, not upgraded or newly verified.",
                   "Later receipts are preserved separately and require explicit content-bound reconciliation.",
                   "First/last seen are intake observations, not document or receipt dates.",
                   "Proposed low-value items are never automatically closed."]}, inventory, coverage, details


def import_catalog(database, queue, snapshot, digests_root, output, *, support_root=None, blob_root=None):
    os.umask(0o077)
    database, queue, snapshot = [checked_path(x) for x in (database, queue, snapshot)]
    digests_root = checked_path(digests_root, directory=True)
    support_root = checked_path(support_root, directory=True) if support_root else None
    blob_root = checked_path(blob_root, directory=True) if blob_root else None
    output = Path(os.path.abspath(output))
    if any(part.is_symlink() for part in (output, *output.parents)):
        raise ValueError("output symlink rejected")
    if any(root and (output == root or root in output.parents) for root in (digests_root, support_root, blob_root)):
        raise ValueError("output cannot be within a captured source directory")
    if any(path == output or output in path.parents for path in (database, queue, snapshot, digests_root, support_root, blob_root) if path):
        raise ValueError("output cannot contain source inputs")
    output.mkdir(parents=True, mode=0o700, exist_ok=True)
    if output.stat().st_mode & 0o077:
        raise ValueError("output directory must be owner-only")
    lock_fd = os.open(output / "writer.lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        stage = Path(tempfile.mkdtemp(prefix=".catalog-stage-", dir=output))
        try:
            objects = stage / "objects"
            objects.mkdir(mode=0o700)
            backup_path = stage / "intake.sqlite"
            backup_path.touch(mode=0o600, exist_ok=False)
            with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as source:
                with sqlite3.connect(backup_path) as target:
                    source.backup(target)
            manifest = {"schema_version": 1, "database_source": str(database),
                        "database_snapshot_sha256": digest_file(backup_path), "inputs": []}
            def capture(path, kind):
                entry = {"kind": kind, **capture_file(path, objects)}
                manifest["inputs"].append(entry)
                return stage / entry["object"]
            queue_copy = capture(queue, "prior_queue")
            snapshot_copy = capture(snapshot, "review_snapshot")
            digests = []
            for path in sorted(digests_root.glob("*/document-digests.jsonl")):
                saved = capture(path, "document_digests")
                for number, row in jsonl(saved):
                    row["_digest_file"] = str(path)
                    row["_digest_line"] = number
                    digests.append(row)
            support_files = sorted(p for p in support_root.rglob("*") if p.is_file() or p.is_symlink()) if support_root else []
            for path in support_files:
                capture(path, "preserved_support_not_adjudicated")
            with sqlite3.connect(backup_path.as_uri() + "?mode=ro", uri=True) as db:
                db.row_factory = sqlite3.Row
                if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("snapshot database integrity check failed")
                catalog, inventory, coverage, details = build_catalog(db, jsonl(queue_copy), jsonl(snapshot_copy), digests, blob_root=blob_root)
            manifest["preservation_results"] = [
                {"sha256": card["sha256"], "state": card["preservation"]}
                for card in catalog["cards"]]
            manifest["implementation_hashes"] = {
                name: digest_file(Path(__file__).with_name(name))
                for name in ("catalog.py", "catalog_board.py")}
            snapshot_id = hashlib.sha256(canonical(manifest).encode()).hexdigest()
            catalog["snapshot_id"] = snapshot_id
            catalog["summary"]["preserved_support_files"] = len(support_files)
            catalog["summary"]["digest_sources"] = sum(i["kind"] == "document_digests" for i in manifest["inputs"])
            catalog["summary"]["support_receipts_awaiting_reconciliation"] = len(support_files)
            write_private(stage / "input-manifest.json", json.dumps(manifest, indent=2) + "\n")
            write_private(stage / "catalog.json", json.dumps(catalog, indent=2) + "\n")
            write_private(stage / "inventory.jsonl", "".join(canonical(row) + "\n" for row in inventory))
            write_private(stage / "COVERAGE-RECONCILIATION.json", json.dumps(coverage, indent=2) + "\n")
            write_private(stage / "DOCUMENT-REVIEW-QUEUE.jsonl", "".join(canonical(row) + "\n" for row in details))
            from .catalog_board import render_board
            write_private(stage / "board.html", render_board(catalog))
            artifacts = {p.name: digest_file(p) for p in stage.iterdir() if p.is_file()}
            write_private(stage / "artifact-hashes.json", json.dumps(artifacts, indent=2) + "\n")
            destination = output / snapshot_id
            reused = destination.exists()
            if reused:
                checked_path(destination, directory=True)
                for name, sha in json.loads((destination / "artifact-hashes.json").read_text()).items():
                    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name) or digest_file(checked_path(destination / name)) != sha:
                        raise ValueError("existing catalog snapshot artifact changed")
                if (destination / "input-manifest.json").read_bytes() != (stage / "input-manifest.json").read_bytes():
                    raise ValueError("snapshot identity collision")
                for entry in manifest["inputs"]:
                    sha = entry["sha256"]
                    if not valid_sha(sha) or entry["object"] != "objects/" + sha:
                        raise ValueError("invalid preserved object identity")
                    preserved = checked_path(destination / entry["object"])
                    if preserved.stat().st_size != entry["bytes"] or digest_file(preserved) != sha:
                        raise ValueError("existing preserved input object changed")
                catalog = json.loads((destination / "catalog.json").read_text())
                shutil.rmtree(stage)
            else:
                stage.rename(destination)
            pointer = {"schema_version": 1, "snapshot_id": snapshot_id,
                       "relative_directory": snapshot_id, "catalog_sha256": digest_file(destination / "catalog.json")}
            write_private(output / "CURRENT.json", json.dumps(pointer, indent=2) + "\n")
            return {"snapshot_id": snapshot_id, "reused": reused, "output": str(destination),
                    "summary": catalog["summary"]}
        finally:
            if stage.exists():
                shutil.rmtree(stage)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ("database", "queue", "snapshot", "digests-root", "output"):
        parser.add_argument("--" + arg, required=True, type=Path)
    parser.add_argument("--support-root", type=Path)
    parser.add_argument("--blob-root", type=Path)
    args = parser.parse_args()
    result = import_catalog(args.database, args.queue, args.snapshot, args.digests_root,
                            args.output, support_root=args.support_root, blob_root=args.blob_root)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
