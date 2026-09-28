#!/usr/bin/env python3
"""Fail-closed structural gate for source-bound Flock finding reviews.

This does not decide legal merits or publish anything. Reviewers must inspect
the sources and the exact content whose digest they approve.
"""
import argparse
import hashlib
import json
import re
from datetime import date, datetime
from pathlib import Path

CLASSES = {
    "NOT_ASSESSED", "NOT_APPLICABLE", "VERSION_OR_APPLICABILITY_UNRESOLVED",
    "NO_CONFLICT_OBSERVED", "APPARENT_RULE_CONFLICT", "CONFIRMED_RULE_CONFLICT",
    "CONFIRMED_DOCUMENT_CONTRADICTION", "DOCUMENTATION_GAP",
    "PRODUCTION_SCOPE_GAP", "REDACTION_OR_EXPORT_LIMIT",
    "AGENCY_ASSERTION_UNCORROBORATED", "LEGAL_REVIEW_REQUIRED",
    "CONFIRMED_DOCUMENTARY_FACT", "MISSING_DOCUMENT", "REDACTION",
    "APPARENT_CONFLICT", "INSUFFICIENT_EVIDENCE",
}
INCOMPLETE = {"NOT_ASSESSED", "VERSION_OR_APPLICABILITY_UNRESOLVED", "LEGAL_REVIEW_REQUIRED", "INSUFFICIENT_EVIDENCE"}
ROLES = {"factual", "legal", "privacy"}
COVERAGE = {"selected", "full_text", "full_visual", "all_rows"}


def agent_identity(value):
    """Reject aliases created by whitespace; compare stable ASCII IDs caselessly."""
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:@-]*", value):
        return None
    return value.casefold()


def valid_hash(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def reviewed_timestamp(value):
    if not isinstance(value, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", value):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.utcoffset() is not None
    except ValueError:
        return False


def checked_locators(value):
    if not isinstance(value, list) or not value:
        return False
    for item in value:
        if text(item):
            continue
        if not (isinstance(item, dict) and text(item.get("locator"))
                and text(item.get("path")) and Path(item["path"]).is_absolute()
                and valid_hash(item.get("sha256"))):
            return False
    return True


def verify_bytes(item, label, blocks):
    """Check the bound bytes; reject symlinks anywhere in the supplied path."""
    try:
        path = Path(item.get("path", ""))
        if (not path.is_absolute() or not path.is_file()
                or any(part.is_symlink() for part in (path, *path.parents))):
            raise ValueError("not an absolute regular original")
        if sha256(path) != item["sha256"]:
            blocks.append(label + "source bytes changed")
    except (OSError, ValueError, TypeError):
        blocks.append(label + "original unavailable")


def digest(finding):
    body = {k: v for k, v in finding.items()
            if k not in {"reviews", "publication_status", "gate_report"}}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"),
                                   ensure_ascii=False).encode()).hexdigest()


def text(value):
    return isinstance(value, str) and bool(value.strip())


def sha256(path):
    h = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def gate(finding, external_reviews=(), check_files=False):
    blocks = []
    if not isinstance(finding, dict):
        return {"ready": False, "blockers": ["finding is not an object"]}
    identity = finding.get("finding_id")
    content_hash = digest(finding)
    for field in ("finding_id", "agency", "claim", "author_agent", "next_action"):
        if not text(finding.get(field)):
            blocks.append("missing " + field)
    author = agent_identity(finding.get("author_agent"))
    if author is None:
        blocks.append("invalid author identity")
    if not check_files:
        blocks.append("source byte verification required for readiness")
    classification = finding.get("classification")
    if not isinstance(classification, str) or classification not in CLASSES:
        blocks.append("unrecognized classification")
    if isinstance(classification, str) and classification in INCOMPLETE:
        blocks.append("unresolved legal/review classification")
    for field in ("counterevidence", "missing_evidence"):
        if not isinstance(finding.get(field), list):
            blocks.append(field + " must be an explicit list")
    evidence = finding.get("primary_evidence")
    if not isinstance(evidence, list) or not evidence:
        blocks.append("primary evidence required")
        evidence = []
    for i, item in enumerate(evidence):
        label = "evidence[%s] " % i
        if not isinstance(item, dict):
            blocks.append(label + "must be an object")
            continue
        for field in ("path", "locator", "observation", "coverage"):
            if not text(item.get(field)):
                blocks.append(label + "missing " + field)
        if not isinstance(item.get("coverage"), str) or item["coverage"] not in COVERAGE:
            blocks.append(label + "invalid coverage")
        expected = item.get("sha256", "")
        if not valid_hash(expected):
            blocks.append(label + "invalid source hash")
        elif check_files:
            verify_bytes(item, label, blocks)
    artifacts = finding.get("public_artifacts", [])
    if not isinstance(artifacts, list):
        blocks.append("public_artifacts must be an array")
        artifacts = []
    artifact_bindings = set()
    for i, item in enumerate(artifacts):
        label = "public_artifact[%s] " % i
        if not isinstance(item, dict) or not text(item.get("path")) or not valid_hash(item.get("sha256")):
            blocks.append(label + "requires path and source hash")
            continue
        binding = (item["path"], item["sha256"])
        if binding in artifact_bindings:
            blocks.append(label + "duplicate artifact")
        artifact_bindings.add(binding)
        if check_files:
            verify_bytes(item, label, blocks)
    rules = finding.get("rules")
    if not isinstance(rules, list):
        rules = []
        blocks.append("rules must be an explicit list")
    confirmed = classification == "CONFIRMED_RULE_CONFLICT"
    if confirmed and not rules:
        blocks.append("confirmed conflict requires applicable rules")
    for i, rule in enumerate(rules):
        if not isinstance(rule, dict):
            blocks.append("rule must be an object")
            continue
        for field in ("rule_id", "clause", "source_url"):
            if not text(rule.get(field)):
                blocks.append("rule[%s] missing %s" % (i, field))
        if confirmed:
            if rule.get("applicability") != "yes" or rule.get("version_verified") is not True:
                blocks.append("confirmed conflict has unresolved rule version/applicability")
            try:
                event = date.fromisoformat(rule.get("applicability_event_date") or finding["event_date"])
                start = date.fromisoformat(rule["effective_from"])
                end = date.fromisoformat(rule["effective_to"]) if rule.get("effective_to") else None
                if event < start or (end and event > end):
                    blocks.append("rule does not cover event date")
            except (ValueError, KeyError, TypeError):
                blocks.append("confirmed conflict needs verified effective/event dates")
    if confirmed:
        if finding.get("exceptions_checked") is not True:
            blocks.append("exceptions not checked")
        if finding.get("missing_evidence"):
            blocks.append("confirmed conflict still lists unresolved evidence")
    if finding.get("privacy_status") != "cleared":
        blocks.append("privacy clearance pending")
    reviews = finding.get("reviews", [])
    if not isinstance(reviews, list):
        blocks.append("reviews must be an array")
        reviews = []
    reviews = reviews + [r for r in external_reviews
                         if isinstance(r, dict) and r.get("finding_id") == identity]
    roles, reviewers = set(), set()
    for review in reviews:
        if not isinstance(review, dict):
            blocks.append("review must be an object")
            continue
        if review.get("finding_id") != identity or review.get("finding_digest") != content_hash:
            blocks.append("stale or mismatched review")
            continue
        if review.get("verdict") != "pass":
            blocks.append("unresolved reviewer challenge or block")
            continue
        reviewer = agent_identity(review.get("reviewer_agent"))
        if reviewer is None or reviewer == author:
            blocks.append("review must be independent of author")
            continue
        role = review.get("role")
        if (not isinstance(role, str) or role not in ROLES
                or not text(review.get("rationale")) or not reviewed_timestamp(review.get("reviewed_at"))):
            blocks.append("incomplete reviewer receipt")
            continue
        if not checked_locators(review.get("checked_locators")) or review.get("reviewed_primary") is not True:
            blocks.append("review did not record primary-source checks")
            continue
        if role == "privacy" and artifact_bindings:
            checked = review.get("checked_public_artifacts")
            if (not isinstance(checked, list)
                    or any(not isinstance(a, dict) or not text(a.get("path"))
                           or not valid_hash(a.get("sha256")) for a in checked)
                    or {(a["path"], a["sha256"]) for a in checked} != artifact_bindings):
                blocks.append("privacy review did not bind exact public artifacts")
                continue
        roles.add(role)
        reviewers.add(reviewer)
    if roles != ROLES:
        blocks.append("missing independent roles: " + ", ".join(sorted(ROLES - roles)))
    if len(reviewers) < 2:
        blocks.append("at least two distinct independent reviewers required")
    return {"finding_id": identity, "finding_digest": content_hash,
            "ready": not blocks, "blockers": sorted(set(blocks)),
            "ready_scope": "exact finding and declared public artifact bytes only",
            "source_bytes_checked": bool(check_files)}


def objects(path, key):
    data = json.loads(Path(path).read_text())
    values = data.get(key) if isinstance(data, dict) else data
    if not isinstance(values, list):
        raise ValueError("input must be an array or object with " + key)
    return values


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("findings")
    p.add_argument("--reviews")
    p.add_argument("--check-files", action="store_true")
    p.add_argument("--require-ready", action="store_true")
    p.add_argument("--output")
    args = p.parse_args()
    findings = objects(args.findings, "findings")
    reviews = objects(args.reviews, "reviews") if args.reviews else []
    report = [gate(f, reviews, args.check_files) for f in findings]
    ids = [r.get("finding_id") for r in report]
    for item in report:
        if ids.count(item.get("finding_id")) > 1:
            item["ready"] = False
            item["blockers"].append("duplicate finding_id")
    output = {"total": len(report), "ready": sum(r["ready"] for r in report),
              "gate_scope": "structural evidence/review checks; no legal merits decision",
              "findings": report}
    rendered = json.dumps(output, indent=2) + "\n"
    if args.output:
        Path(args.output).write_text(rendered)
    else:
        print(rendered, end="")
    return 1 if args.require_ready and (not report or any(not r["ready"] for r in report)) else 0


if __name__ == "__main__":
    raise SystemExit(main())
