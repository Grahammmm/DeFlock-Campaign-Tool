#!/usr/bin/env python3
"""Collect exact reviewer receipts, without manufacturing or upgrading approvals."""
import argparse
import hashlib
import json
import os
from pathlib import Path

if __package__:
    from .safe_output import write_private
else:
    try:
        from safe_output import write_private
    except ModuleNotFoundError:
        from campaign_tool.records.gates.safe_output import write_private


def extract_receipts(data):
    values = data if isinstance(data, list) else data.get("reviews") if isinstance(data, dict) else None
    if not isinstance(values, list):
        return [], ["No canonical review array; preserve the source and request reviewer reissue."]
    receipts, errors = [], []
    if isinstance(data, dict) and "collection_errors" in data:
        prior = data["collection_errors"]
        if not isinstance(prior, list) or prior:
            errors.append("Source collection errors remain unresolved; no inferred approval.")
    for index, item in enumerate(values):
        required = ("finding_id", "finding_digest", "reviewer_agent", "role", "verdict", "rationale", "reviewed_at")
        if not isinstance(item, dict) or any(not isinstance(item.get(k), str) or not item[k].strip() for k in required):
            errors.append(f"Receipt {index}: missing canonical fields; no inferred approval.")
            continue
        receipts.append(item)
    return receipts, errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("batch_root", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    summaries = []
    for agency in sorted((args.batch_root / "agencies").iterdir()):
        if not agency.is_dir() or not (agency / "findings.json").exists():
            continue
        reviews, sources, errors, seen = [], [], [], set()
        for name in ("gate-reviews.json", "factual-review.json",
                     "legal-review.json", "privacy-review.json"):
            path = agency / name
            if not path.exists() and not path.is_symlink():
                continue
            raw = None
            try:
                raw = path.read_bytes()
                parsed, issues = extract_receipts(json.loads(raw))
                errors.extend({"path": str(path), "issue": issue} for issue in issues)
                for receipt in parsed:
                    # All fields participate; never deduplicate by reviewer/role
                    # identity alone or let a pass replace a distinct challenge.
                    identity = json.dumps(receipt, sort_keys=True, separators=(",", ":"),
                                          ensure_ascii=True, allow_nan=False)
                    if identity not in seen:
                        seen.add(identity)
                        reviews.append(receipt)
            except OSError:
                errors.append({"path": str(path), "issue": "Review source unreadable; no inferred approval."})
            except (ValueError, TypeError) as exc:
                errors.append({"path": str(path), "issue": str(exc)})
            sources.append({"path": str(path),
                            "sha256": hashlib.sha256(raw).hexdigest() if raw is not None else None})
        output = {
            "reviews": reviews,
            "receipt_sources": sources,
            "collection_errors": errors,
            "scope": "Unmodified review entries only. This collection grants no factual, legal, privacy or publication approval.",
        }
        target = agency / "independent-reviews.json"
        write_private(target, json.dumps(output, indent=2) + "\n")
        summaries.append({"agency": agency.name, "receipts": len(reviews), "source_files": len(sources), "errors": len(errors)})
    print(json.dumps(summaries, indent=2))


if __name__ == "__main__":
    main()
