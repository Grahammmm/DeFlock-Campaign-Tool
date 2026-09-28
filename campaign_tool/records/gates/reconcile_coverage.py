#!/usr/bin/env python3
"""Reconcile inventory with analyst digests without turning extraction into review."""
import argparse
import collections
import datetime
import json
import pathlib
import re


def rows(path):
    with path.open() as stream:
        for number, line in enumerate(stream, 1):
            if line.strip():
                item = json.loads(line)
                if not isinstance(item, dict):
                    raise ValueError(f"{path}:{number}: expected object")
                yield item


def reconcile(intake, digests):
    inventory = {}
    for item in intake:
        sha = item.get("sha256", "")
        if not re.fullmatch(r"[a-f0-9]{64}", sha):
            raise ValueError("Inventory document requires a valid SHA-256")
        if sha in inventory:
            raise ValueError("Inventory queue contains duplicate document identities")
        inventory[sha] = item
    by_hash = collections.defaultdict(list)
    for item in digests:
        sha = item.get("sha256", "")
        if not re.fullmatch(r"[a-f0-9]{64}", sha):
            raise ValueError("Analyst digest requires a valid SHA-256")
        by_hash[sha].append(item)
    counts = collections.Counter()
    partitions = collections.defaultdict(collections.Counter)
    details = []
    for sha, original in sorted(inventory.items()):
        analyses = by_hash.get(sha, [])
        statuses = {x.get("review_status", "not_reviewed") for x in analyses}
        malformed = any(not x.get("coverage") or not x.get("key_points") for x in analyses)
        if not analyses:
            state = "no_analyst_digest"
        elif malformed:
            state = "incomplete_digest"
        elif len(statuses) != 1:
            state = "conflicting_coverage_declarations"
        elif statuses == {"full"}:
            state = "author_declared_full"
        elif statuses == {"partial"}:
            state = "author_declared_partial"
        elif statuses == {"blocked"}:
            state = "author_declared_blocked"
        else:
            state = "unrecognized_review_status"
        classes = original.get("classification", ["unknown"])
        if isinstance(classes, str):
            classes = [classes]
        partition = "original_hint" if "original" in classes else (
            "unknown_hint" if "unknown" in classes or not classes else "generated_hint")
        counts[state] += 1
        partitions[partition][state] += 1
        details.append({
            "sha256": sha,
            "classification_hint": partition,
            "classification_is_not_authenticity_proof": True,
            "agency_hints": original.get("agency_hints", []),
            "source_paths": original.get("source_paths", []),
            "parents": original.get("parents", []),
            "extraction_stage": original.get("stage", "unknown"),
            "analysis_state": state,
            "digest_count": len(analyses),
            "digest_paths": [x.get("_digest_file") for x in analyses],
            "authors": sorted({x.get("author_agent", "unrecorded") for x in analyses}),
            "gaps": [g for x in analyses for g in x.get("gaps", [])],
            "issues": original.get("issues", []),
            "independent_legal_or_publication_approval": "not_established_by_this_report",
        })
    outside = [{"sha256": sha, "digests": len(items),
                "paths": [x.get("path", x.get("source_path")) for x in items]}
               for sha, items in sorted(by_hash.items()) if sha not in inventory]
    summary = {
        "schema_version": 1,
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "intake_unique_hashes": len(inventory),
        "analyst_digest_entries": sum(len(x) for x in by_hash.values()),
        "analyst_unique_hashes": len(by_hash),
        "matched_analyst_unique_hashes": len(set(by_hash) & set(inventory)),
        "coverage_states": dict(counts),
        "classification_hint_partitions": {k: dict(v) for k, v in partitions.items()},
        "analyst_hashes_outside_intake": outside,
        "limits": [
            "Author-declared full is not independent acceptance or legal certification.",
            "Classification hints never exempt a document from evidence triage.",
            "Generated and unknown files remain visible; confirm identity before excluding them.",
            "Outside-intake hashes may be supplemental sources or a provenance error; resolve individually.",
            "Findings publication checks are separate from document coverage.",
        ],
    }
    return summary, details


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("batch_root", type=pathlib.Path)
    parser.add_argument("--output", type=pathlib.Path)
    args = parser.parse_args()
    intake = list(rows(args.batch_root / "intake/all-agency-document-queue.jsonl"))
    digests = []
    for path in sorted((args.batch_root / "agencies").glob("*/document-digests.jsonl")):
        for item in rows(path):
            item["_digest_file"] = str(path)
            digests.append(item)
    summary, details = reconcile(intake, digests)
    target = args.output or args.batch_root / "COVERAGE-RECONCILIATION.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(summary, indent=2) + "\n")
    temporary.replace(target)
    queue = target.with_name("DOCUMENT-REVIEW-QUEUE.jsonl")
    temporary = queue.with_suffix(queue.suffix + ".tmp")
    with temporary.open("w") as stream:
        for item in details:
            stream.write(json.dumps(item, sort_keys=True) + "\n")
    temporary.replace(queue)
    print(json.dumps({k: v for k, v in summary.items() if k != "analyst_hashes_outside_intake"}, indent=2))


if __name__ == "__main__":
    main()
