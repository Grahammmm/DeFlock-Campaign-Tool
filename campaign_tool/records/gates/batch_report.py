#!/usr/bin/env python3
"""Combine agency digests and evidence-review gates without inflating coverage."""
import argparse
import json
from collections import Counter
from pathlib import Path
if __package__:
    from .validate_findings import gate
else:
    from validate_findings import gate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("batch_root", type=Path)
    parser.add_argument("--check-files", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--require-agency", action="append", default=[],
                        help="Expected private agency package; repeat for each agency")
    args = parser.parse_args()
    results, errors = [], []
    unique = set()
    for agency in sorted((args.batch_root / "agencies").glob("*/")):
        digests = []
        digest_file = agency / "document-digests.jsonl"
        if digest_file.exists():
            for number, line in enumerate(digest_file.read_text().splitlines(), 1):
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                    if not isinstance(item, dict):
                        raise ValueError("not an object")
                    digests.append(item)
                    if item.get("sha256"):
                        unique.add(item["sha256"])
                except (ValueError, TypeError):
                    errors.append({"path": str(digest_file), "line": number, "error": "invalid digest"})
        else:
            errors.append({"path": str(digest_file), "error": "missing document digests"})
        findings, reviews = [], []
        for filename, key, dest in [("findings.json", "findings", findings),
                                    ("independent-reviews.json", "reviews", reviews)]:
            path = agency / filename
            if not path.exists():
                if key == "findings":
                    errors.append({"path": str(path), "error": "missing findings"})
                continue
            try:
                data = json.loads(path.read_text())
                items = data.get(key) if isinstance(data, dict) else data
                if not isinstance(items, list):
                    raise ValueError("not an array")
                dest.extend(items)
            except (ValueError, TypeError):
                errors.append({"path": str(path), "error": "invalid " + key})
        checks = [gate(f, reviews, args.check_files) for f in findings]
        results.append({"agency_package": agency.name, "document_digest_entries": len(digests),
                        "distinct_digest_hashes": len({d.get("sha256") for d in digests if d.get("sha256")}),
                        "declared_review_statuses": dict(Counter(str(d.get("review_status", "unspecified")) for d in digests)),
                        "findings": len(findings), "publication_ready": sum(c["ready"] for c in checks),
                        "classification_counts": dict(Counter(f.get("classification", "unspecified") for f in findings if isinstance(f, dict))),
                        "gates": checks})
    required = set(args.require_agency)
    missing = sorted(required - {r["agency_package"] for r in results})
    output = {
        "scope": "Agency digest declarations and structural finding gates; consult intake report for full corpus denominator",
        "distinct_digest_hashes": len(unique), "missing_agency_packages": missing,
        "findings": sum(r["findings"] for r in results),
        "publication_ready": sum(r["publication_ready"] for r in results),
        "errors": errors, "agencies": results,
        "exhaustive_legal_review_claimed": False,
    }
    args.output.write_text(json.dumps(output, indent=2) + "\n")
    print(json.dumps({k: v for k, v in output.items() if k != "agencies"}))


if __name__ == "__main__":
    main()
