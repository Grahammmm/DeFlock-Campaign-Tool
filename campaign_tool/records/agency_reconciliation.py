"""Produce provenance-bound agency candidates without approving attribution."""
import argparse
from collections import Counter, defaultdict
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def valid_hash(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def label(value):
    if not isinstance(value, str):
        return ""
    value = " ".join(value.split())
    return "" if value.casefold() in {"unassigned", "unknown"} else value


def reconcile(cards, records, aliases=None):
    """Reconcile only unassigned originals/children; all results stay candidates."""
    if not isinstance(cards, list) or not isinstance(records, list):
        raise ValueError("cards and records must be lists")
    aliases = {} if aliases is None else aliases
    if not isinstance(aliases, dict):
        raise ValueError("aliases must be an explicit mapping")
    normalized_aliases = {}
    for source, target in aliases.items():
        source, target = label(source).casefold(), label(target).casefold()
        if not source or not target:
            raise ValueError("alias endpoints must name agencies")
        if source in normalized_aliases and normalized_aliases[source] != target:
            raise ValueError("conflicting aliases")
        normalized_aliases[source] = target
    for target in normalized_aliases.values():
        if target in normalized_aliases and normalized_aliases[target] != target:
            raise ValueError("alias chains must be explicitly flattened")

    def agency_key(value):
        value = label(value).casefold()
        return normalized_aliases.get(value, value)

    indexed = {}
    for card in cards:
        if not isinstance(card, dict) or not valid_hash(card.get("sha256")):
            raise ValueError("card requires a lowercase SHA-256")
        if card["sha256"] in indexed:
            raise ValueError("duplicate card identity")
        if card.get("agency_status") not in {"unassigned", "hint_present", "scope_excluded"}:
            raise ValueError("card requires explicit agency_status")
        if not isinstance(card.get("agency_hints"), list) or not isinstance(card.get("parents"), list):
            raise ValueError("card requires hint and parent lists")
        if any(not isinstance(v, str) for v in card["agency_hints"]):
            raise ValueError("agency hints must be strings")
        if any(not valid_hash(v) for v in card["parents"]):
            raise ValueError("parent references must be hashes")
        indexed[card["sha256"]] = card
    matched = defaultdict(list)
    unmatched, excluded = [], []
    for record in records:
        if not isinstance(record, dict) or not valid_hash(record.get("sha256")) or not valid_hash(record.get("object_sha256")):
            raise ValueError("digest metadata requires identity and object hashes")
        if type(record.get("line")) is not int or record["line"] < 1:
            raise ValueError("digest locator must be a positive physical line")
        if not isinstance(record.get("agency"), str):
            raise ValueError("digest agency must be a string")
        item = {"sha256": record["sha256"], "agency": record["agency"],
                "canonical_agency": agency_key(record["agency"]),
                "source": {"object_sha256": record["object_sha256"], "line": record["line"]}}
        card = indexed.get(record["sha256"])
        if card is None:
            unmatched.append(item)
        elif card["agency_status"] == "scope_excluded":
            excluded.append({"sha256": item["sha256"], "source": item["source"]})
        elif label(record["agency"]):
            matched[record["sha256"]].append(item)
    results = []
    for sha, card in sorted(indexed.items()):
        if card["agency_status"] != "unassigned" or card.get("role") not in {"agency_original", "container_child"}:
            continue
        exact = [{"agency": r["agency"], "canonical_agency": r["canonical_agency"], "source": r["source"]}
                 for r in matched[sha]]
        parents, unresolved = [], []
        for parent in sorted(set(card["parents"])):
            parent_card = indexed.get(parent)
            if parent_card is None or parent_card["agency_status"] == "scope_excluded":
                unresolved.append(parent)
                continue
            for hint in sorted(set(parent_card["agency_hints"])):
                if label(hint):
                    parents.append({"agency": hint, "canonical_agency": agency_key(hint), "parent_sha256": parent})
        candidates = sorted({v["canonical_agency"] for v in exact + parents})
        status = "no_evidence" if not candidates else "conflicting_candidates" if len(candidates) > 1 else "candidate_only"
        results.append({"sha256": sha, "role": card["role"], "exact_hash_candidates": exact,
                        "parent_hints": parents, "unresolved_parent_hashes": unresolved,
                        "canonical_candidates": candidates, "status": status,
                        "verified": False, "publication_ready": False})
    statuses = Counter(r["status"] for r in results)
    summary = {"target_count": len(results),
               "exact_hash_candidate_items": sum(bool(r["exact_hash_candidates"]) for r in results),
               "parent_hint_items": sum(bool(r["parent_hints"]) for r in results),
               "either_source_items": sum(bool(r["canonical_candidates"]) for r in results),
               "no_evidence_items": statuses["no_evidence"],
               "conflicting_items": statuses["conflicting_candidates"],
               "candidate_only_items": statuses["candidate_only"],
               "unresolved_parent_items": sum(bool(r["unresolved_parent_hashes"]) for r in results),
               "unmatched_digest_records": len(unmatched), "excluded_digest_records": len(excluded)}
    return {"cards": results, "summary": summary, "unmatched_digest_records": unmatched,
            "excluded_digest_records": excluded}


def checked(path, directory=False):
    path = Path(os.path.abspath(path))
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("symlink input/output is not permitted")
    if not (path.is_dir() if directory else path.is_file()):
        raise ValueError("required input is absent or wrong type")
    return path


def private_output(path, directory=False):
    path = checked(path, directory=directory)
    metadata = path.stat()
    if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
        raise ValueError("output must remain owner-only")
    return path


def run(snapshot, output, aliases_path=None):
    """Read an immutable catalog snapshot, save a separate owner-only result."""
    os.umask(0o077)
    snapshot = checked(snapshot, directory=True)
    catalog_bytes = checked(snapshot / "catalog.json").read_bytes()
    manifest_bytes = checked(snapshot / "input-manifest.json").read_bytes()
    catalog, manifest = json.loads(catalog_bytes), json.loads(manifest_bytes)
    if catalog.get("schema_version") != 1 or manifest.get("schema_version") != 1:
        raise ValueError("unsupported catalog/manifest schema")
    if sha256(canonical(manifest).encode()) != catalog.get("snapshot_id"):
        raise ValueError("catalog snapshot identity does not bind its manifest")
    artifacts = json.loads(checked(snapshot / "artifact-hashes.json").read_text())
    if artifacts.get("catalog.json") != sha256(catalog_bytes) or artifacts.get("input-manifest.json") != sha256(manifest_bytes):
        raise ValueError("catalog input integrity check failed")
    bindings = {"catalog_sha256": sha256(catalog_bytes), "manifest_sha256": sha256(manifest_bytes),
                "snapshot_id": catalog["snapshot_id"], "digest_objects": [],
                "implementation_sha256": sha256(Path(__file__).read_bytes())}
    records = []
    for entry in manifest["inputs"]:
        if entry.get("kind") != "document_digests":
            continue
        identity = entry.get("sha256")
        if not valid_hash(identity) or entry.get("object") != "objects/" + identity:
            raise ValueError("invalid digest object mapping")
        data = checked(snapshot / entry["object"]).read_bytes()
        if len(data) != entry.get("bytes") or sha256(data) != identity:
            raise ValueError("digest object integrity check failed")
        bindings["digest_objects"].append({"sha256": identity, "bytes": len(data)})
        for number, line in enumerate(data.splitlines(), 1):
            if line.strip():
                row = json.loads(line)
                records.append({"sha256": row["sha256"], "agency": row["agency"],
                                "object_sha256": identity, "line": number})
    aliases = None
    if aliases_path:
        aliases_path = checked(aliases_path)
        data = aliases_path.read_bytes()
        aliases = json.loads(data)
        bindings["explicit_aliases_sha256"] = sha256(data)
    result = reconcile(catalog["cards"], records, aliases)
    result_bytes = (canonical(result) + "\n").encode()
    run_id = sha256(canonical({"inputs": bindings, "result_sha256": sha256(result_bytes)}).encode())
    output = Path(os.path.abspath(output))
    if any(p.is_symlink() for p in (output, *output.parents)):
        raise ValueError("symlink output is not permitted")
    if output == snapshot or snapshot in output.parents or output in snapshot.parents:
        raise ValueError("output must not overlap snapshot")
    if aliases_path and (output == aliases_path or output in aliases_path.parents):
        raise ValueError("output must not contain alias input")
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    private_output(output, directory=True)
    fd = os.open(output / "writer.lock", os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as lock:
        private_output(output / "writer.lock")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        destination = output / run_id
        receipt = {"schema_version": 1, "run_id": run_id, "input_bindings": bindings,
                   "result_sha256": sha256(result_bytes), "summary": result["summary"],
                   "independent_check": "not_performed", "verified_attributions": 0,
                   "scope": "Metadata candidates only; no substantive review or publication approval."}
        receipt_bytes = (canonical(receipt) + "\n").encode()
        if destination.exists():
            private_output(destination, directory=True)
            if private_output(destination / "candidates.json").read_bytes() != result_bytes or private_output(destination / "receipt.json").read_bytes() != receipt_bytes:
                raise ValueError("existing reconciliation output changed")
            reused = True
        else:
            stage = Path(tempfile.mkdtemp(prefix=".agency-stage-", dir=output))
            try:
                for name, data in (("candidates.json", result_bytes), ("receipt.json", receipt_bytes)):
                    with (stage / name).open("xb") as file:
                        file.write(data)
                        file.flush()
                        os.fsync(file.fileno())
                stage.rename(destination)
            finally:
                if stage.exists():
                    shutil.rmtree(stage)
            reused = False
    return {"output": str(destination), "run_id": run_id, "reused": reused, "summary": result["summary"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--aliases", type=Path)
    args = parser.parse_args()
    print(json.dumps(run(args.snapshot, args.output, args.aliases), sort_keys=True))


if __name__ == "__main__":
    main()
