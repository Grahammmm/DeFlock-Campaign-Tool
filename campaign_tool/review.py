"""Content-binding checks, NOT authenticated authorization or legal validation."""
import hashlib
import json
import re
import unicodedata

ROLES = frozenset(("factual", "legal", "privacy"))
CLASSIFICATIONS = frozenset(("documented_fact", "apparent_conflict",
                             "confirmed_conflict", "information_gap",
                             "redaction", "agency_assertion", "no_conflict"))


def content_hash(finding):
    return hashlib.sha256(json.dumps(finding, sort_keys=True,
                                     separators=(",", ":"), ensure_ascii=True,
                                     allow_nan=False).encode()).hexdigest()


EDIT_MARKER = re.compile(r"\[\s*EDIT\s+ME\s*:", re.IGNORECASE)  # "[EDIT ME: ...]" placeholders, any case/spacing


def _marked(value):
    if isinstance(value, str):
        return bool(EDIT_MARKER.search(value))
    if isinstance(value, dict):
        return any(_marked(item) for item in value.values())
    return isinstance(value, list) and any(_marked(item) for item in value)


def edit_markers(finding):
    """One 'edit_marker:<field>' per field still holding a placeholder; sources as 'sources[i].<key>'."""
    found = []
    for key, value in finding.items():
        if key == "sources" and isinstance(value, list):
            found.extend(f"edit_marker:sources[{index}]" + (f".{name}" if name is not None else "")
                         for index, source in enumerate(value)
                         for name, item in (source.items() if isinstance(source, dict) else [(None, source)])
                         if _marked(item))
        elif _marked(value):
            found.append("edit_marker:" + str(key))
    return found


LEGAL_TERMS = re.compile(r"\b(violat\w*|illegal\w*|unlawful\w*|broke\s+the\s+law|criminal\w*)\b", re.IGNORECASE)


def _plain(text):
    """NFKC with format characters (zero-width space, soft hyphen, ...) removed, so they cannot split a term."""
    return "".join(ch for ch in unicodedata.normalize("NFKC", text) if unicodedata.category(ch) != "Cf")


def legal_conclusion_fields(finding):
    """Sorted text fields stating a legal conclusion; field names only, never the matched words."""
    return [f for f in ("body_md", "summary", "title")
            if isinstance(finding.get(f), str) and LEGAL_TERMS.search(_plain(finding[f]))]


def _who(value):
    """Comparable identity: casefolded, whitespace collapsed; non-string or empty -> None."""
    return " ".join(value.split()).casefold() or None if isinstance(value, str) else None


def review_blockers(finding, receipts, campaign_id=None):
    """Caller must separately authenticate receipt issuers and validate artifacts.

    ``campaign_id`` (findings passes the campaign's own id, or "" before it has one): only approvals carrying
    exactly that id count, and a current approval with no id or another campaign's id is named as a blocker
    instead of being skipped silently. A challenge blocks whatever its binding. None keeps the unbound check."""
    if not isinstance(finding, dict):
        return ["invalid_finding"]
    published_view = receipts == []  # content.load_finding: see the wording gate below
    receipts, blockers = (receipts, []) if isinstance(receipts, list) else ([], ["invalid_receipts"])
    if not all(isinstance(r, dict) for r in receipts):
        blockers.append("invalid_receipt")
        receipts = [r for r in receipts if isinstance(r, dict)]
    if not isinstance(finding.get("classification"), str) or finding["classification"] not in CLASSIFICATIONS:
        blockers.append("unknown_classification")
    blockers.extend(edit_markers(finding))
    for field in ("id", "author", "summary", "sources", "limitations", "counterevidence"):
        if field not in finding:
            blockers.append("missing_" + field)
    sources = finding.get("sources")
    if not isinstance(sources, list) or not sources:
        blockers.append("missing_evidence")
        sources = []
    if not isinstance(finding.get("summary"), str) or not finding["summary"].strip():
        blockers.append("missing_summary")
    for source in sources:
        if not isinstance(source, dict) or not source.get("locator") or not source.get("sha256"):
            blockers.append("source_missing_locator_or_hash")
        elif not isinstance(source["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", source["sha256"]):
            blockers.append("invalid_source_hash")
    if finding.get("classification") in ("apparent_conflict", "confirmed_conflict"):
        for field in ("event_date", "rule_version", "duty", "exceptions"):
            if not finding.get(field):
                blockers.append("missing_" + field)
    digest = object()  # equals no receipt hash when the finding cannot be hashed
    try:
        digest = content_hash(finding)
    except (ValueError, TypeError):
        blockers.append("unhashable_finding")
    valid = [r for r in receipts if r.get("content_sha256") == digest
             and r.get("decision") == "approve" and isinstance(r.get("role"), str)
             and _who(r.get("reviewer")) not in (None, _who(finding.get("author")))
             and r.get("rationale") and r.get("reviewed_at")
             and (campaign_id is None or (bool(campaign_id) and r.get("campaign_id") == campaign_id))]
    if campaign_id is not None:
        approvals = [r for r in receipts if r.get("content_sha256") == digest and r.get("decision") == "approve"]
        if any("campaign_id" not in r for r in approvals):
            blockers.append("unbound_legacy_receipt")
        if any("campaign_id" in r and (not campaign_id or r["campaign_id"] != campaign_id) for r in approvals):
            blockers.append("foreign_campaign_receipt")
    if any(r.get("content_sha256") == digest and r.get("decision") in ("reject", "changes_requested")
           for r in receipts):
        blockers.append("unresolved_challenge")
    # Wording gate, not legal validation. Allowed only in a confirmed_conflict with a current legal approve
    # receipt, or with receipts == [] (content.load_finding on an already-published file, whose publish
    # step required that receipt); a confirmed_conflict draft with no receipts still shows missing_legal_review.
    if not (finding.get("classification") == "confirmed_conflict"
            and (published_view or any(r.get("role") == "legal" for r in valid))):
        blockers.extend("legal_conclusion_wording_in_" + field for field in legal_conclusion_fields(finding))
    roles = {r.get("role") for r in valid}
    blockers.extend("missing_" + role + "_review" for role in sorted(ROLES - roles))
    if len({_who(r["reviewer"]) for r in valid if r.get("role") in ROLES}) < 2:
        blockers.append("need_two_independent_reviewers")
    return sorted(set(blockers))
