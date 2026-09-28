#!/usr/bin/env python3
"""Collect exact reviewer receipts, without manufacturing or upgrading approvals."""
import argparse
import hashlib
import json
import os
from pathlib import Path


def extract_receipts(data):
    values = data if isinstance(data, list) else data.get("reviews") if isinstance(data, dict) else None
    if not isinstance(values, list):
        return [], ["No canonical review array; preserve the source and request reviewer reissue."]
    receipts, errors = [], []
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
        factual = agency / "gate-reviews.json"
        if not factual.exists():
            factual = agency / "factual-review.json"
        reviews, sources, errors = [], [], []
        for path in (factual, agency / "legal-review.json", agency / "privacy-review.json"):
            if not path.exists():
                continue
            raw = path.read_bytes()
            try:
                parsed, issues = extract_receipts(json.loads(raw))
                reviews.extend(parsed)
                errors.extend({"path": str(path), "issue": issue} for issue in issues)
            except (ValueError, TypeError) as exc:
                errors.append({"path": str(path), "issue": str(exc)})
            sources.append({"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()})
        output = {
            "reviews": reviews,
            "receipt_sources": sources,
            "collection_errors": errors,
            "scope": "Unmodified review entries only. This collection grants no factual, legal, privacy or publication approval.",
        }
        target = agency / "independent-reviews.json"
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(output, indent=2) + "\n")
        temporary.replace(target)
        summaries.append({"agency": agency.name, "receipts": len(reviews), "source_files": len(sources), "errors": len(errors)})
    print(json.dumps(summaries, indent=2))


if __name__ == "__main__":
    main()
