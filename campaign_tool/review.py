"""Content-binding checks, NOT authenticated authorization or legal validation."""
import hashlib
import json
import re

ROLES = frozenset(("factual", "legal", "privacy"))
CLASSIFICATIONS = frozenset(("documented_fact", "apparent_conflict",
                             "confirmed_conflict", "information_gap",
                             "redaction", "agency_assertion", "no_conflict"))


def content_hash(finding):
    return hashlib.sha256(json.dumps(finding, sort_keys=True,
                                     separators=(",", ":"), ensure_ascii=True,
                                     allow_nan=False).encode()).hexdigest()


def review_blockers(finding, receipts):
    """Caller must separately authenticate receipt issuers and validate artifacts."""
    blockers = []
    if finding.get("classification") not in CLASSIFICATIONS:
        blockers.append("unknown_classification")
    for field in ("id", "author", "summary", "sources", "limitations", "counterevidence"):
        if field not in finding:
            blockers.append("missing_" + field)
    if not finding.get("sources"):
        blockers.append("missing_evidence")
    if not isinstance(finding.get("summary"), str) or not finding["summary"].strip():
        blockers.append("missing_summary")
    for source in finding.get("sources", []):
        if not isinstance(source, dict) or not source.get("locator") or not source.get("sha256"):
            blockers.append("source_missing_locator_or_hash")
        elif not isinstance(source["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", source["sha256"]):
            blockers.append("invalid_source_hash")
    if finding.get("classification") in ("apparent_conflict", "confirmed_conflict"):
        for field in ("event_date", "rule_version", "duty", "exceptions"):
            if not finding.get(field):
                blockers.append("missing_" + field)
    digest = content_hash(finding)
    valid = [r for r in receipts if r.get("content_sha256") == digest
             and r.get("decision") == "approve" and r.get("reviewer")
             and r["reviewer"] != finding.get("author")
             and r.get("rationale") and r.get("reviewed_at")]
    if any(r.get("content_sha256") == digest and r.get("decision") in ("reject", "changes_requested")
           for r in receipts):
        blockers.append("unresolved_challenge")
    roles = {r.get("role") for r in valid}
    blockers.extend("missing_" + role + "_review" for role in sorted(ROLES - roles))
    if len({r["reviewer"] for r in valid if r.get("role") in ROLES}) < 2:
        blockers.append("need_two_independent_reviewers")
    return sorted(set(blockers))
