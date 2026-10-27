"""Conservative document understanding over receipt-bound derived units."""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from . import evidence

SCHEMA = "records-understanding-document-v1"
MAX_CLASSIFICATIONS = 500
MAX_ENTITIES = 500
MAX_DATES = 1000
MAX_SUMMARY_FACTS = 20
MAX_KEY_GROUPS = 512
MAX_CROSS_RECORD_KEYS = 200
# Upper bound for the legacy integer page-inventory form; larger values are
# reported as an invalid inventory instead of allocating a page range.
MAX_PAGE_INVENTORY = 100_000
_PAGE_COUNT_KEYS = ("declared_page_count", "page_count", "expected_page_count")
_DATE = re.compile(
    r"(?<![0-9])(?:[0-9]{4}-[0-9]{2}-[0-9]{2}"
    r"(?:[Tt ][0-9]{2}:[0-9]{2}(?::[0-9]{2}(?:\.[0-9]+)?)?(?:Z|z|[+-][0-9]{2}:?[0-9]{2})?)?)"
    r"(?![0-9])"
)
_KEY = re.compile(r"(?i)\b(?:request|policy|agreement|contract|report|invoice|case|incident|RFP)\s*(?:(?:no\.?|number|id)\b\s*[:#-]?|[#:]|(?=[0-9]))\s*([A-Z0-9][A-Z0-9./_-]{2,30})\b")
_ENTITY = re.compile(r"(?im)\b(agency|department|vendor|system|requester|recipient|author)\s*:\s*([^\n;]{2,160})")
_CLASSIFIERS = (
    ("policy", re.compile(r"\b(policy|procedure|standard|guideline)\b", re.I)),
    ("agreement", re.compile(r"\b(agreement|memorandum of understanding|MOU|contract)\b", re.I)),
    ("audit_or_search_log", re.compile(r"\b(audit log|search log|query log|access log)\b", re.I)),
    ("records_request_or_response", re.compile(r"\b(public records request|records request|responsive records|records search)\b", re.I)),
    ("invoice_or_fee_record", re.compile(r"\b(invoice|fee estimate|cost estimate|amount due)\b", re.I)),
)


def _citation(bundle: dict, unit: dict, start: int, end: int) -> dict:
    raw = unit["text"].encode("utf-8")
    cite = evidence.citation(unit, start, end)
    # The caller validates the bundle once before scanning. span_support checks
    # this exact normalized unit; revalidating the full bundle per span is quadratic.
    if cite.get("quote") != raw[start:end].decode("utf-8"):
        raise ValueError("citation_quote_mismatch")
    return cite


def _utf8_offset(text: str, character_offset: int) -> int:
    return len(text[:character_offset].encode("utf-8"))


_NUMERIC_OFFSET = re.compile(r"(?<=[0-9])[+-]([0-9]{2}):?([0-9]{2})$")


def _offset_components_in_range(raw: str) -> bool:
    """Check a supplied compact/colon numeric offset before parser normalization.

    The parser would otherwise carry +01:60 into +02:00; such source text stays unresolved.
    """
    match = _NUMERIC_OFFSET.search(raw)
    return match is None or (int(match.group(1)) <= 23 and int(match.group(2)) <= 59)


def _date_value(raw: str) -> dict[str, Any]:
    candidate = {"raw": raw, "value": None, "time_basis": "unknown"}
    if "T" not in raw and "t" not in raw and " " not in raw:
        try:
            candidate.update(value=date.fromisoformat(raw).isoformat(), time_basis="local_date_only")
        except ValueError:
            candidate["parse_status"] = "invalid_date"
        return candidate
    if not _offset_components_in_range(raw):
        candidate["parse_status"] = "unparsed_date_time"
        return candidate
    normalized = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
    # ISO permits compact offsets; normalize only for Python's parser.
    normalized = re.sub(r"([+-][0-9]{2})([0-9]{2})$", r"\1:\2", normalized)
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        candidate["parse_status"] = "unparsed_date_time"
        return candidate
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        candidate.update(value=parsed.isoformat(), time_basis="timezone_unknown")
    else:
        candidate.update(value=parsed.isoformat(), time_basis="explicit_offset")
    return candidate


def _positive_int(value: Any) -> bool:
    return type(value) is int and value > 0


def _declared_page_count(context: Any) -> tuple[int | None, str]:
    """Return (declared total, status) where status is declared, absent or invalid.

    Every present count key must be a positive integer and all must agree;
    otherwise the total is unknown and reported as invalid metadata.
    """
    if not isinstance(context, dict):
        return None, "absent"
    present = [context[k] for k in _PAGE_COUNT_KEYS if context.get(k) is not None]
    if not present:
        return None, "absent"
    if not all(_positive_int(v) for v in present) or len(set(present)) != 1:
        return None, "invalid"
    return present[0], "declared"


def _missing_page_count(declared: int | None, in_range: int) -> int | None:
    """Count-versus-inventory predicate: shortfall against a declared total.

    Pure arithmetic; a large untrusted total never allocates a page range.
    """
    return None if declared is None else max(0, declared - in_range)


_PAGE_ID_KEYS = ("page", "page_no")


def _page_entry_id(entry: Any) -> Any:
    """Resolve one inventory entry to its page identifier, or None when untrusted.

    A dict entry may carry either supported alias or both. Every alias that is
    present must be a positive integer and all present aliases must agree;
    a contradictory or malformed entry is rejected rather than silently
    preferring one number. Inventory counting and page metadata lookup both
    use this single resolver.
    """
    if isinstance(entry, dict):
        present = [entry[k] for k in _PAGE_ID_KEYS if k in entry]
        if not present or not all(_positive_int(v) for v in present) or len(set(present)) != 1:
            return None
        return present[0]
    return entry if _positive_int(entry) else None


def _page_inventory(context: Any) -> tuple[list[int], dict, set[str]]:
    """Validate the supplied page inventory against any declared total.

    Returns unique valid page numbers (only pages actually supplied), a count
    summary and inventory gap codes. Missing pages are counted, never invented.
    """
    raw_pages = context.get("pages") if isinstance(context, dict) else None
    declared, count_status = _declared_page_count(context)
    gaps: set[str] = set()
    invalid = 0
    if raw_pages is None:
        shape, identifiers = "absent", []
    elif type(raw_pages) is int and 0 < raw_pages <= MAX_PAGE_INVENTORY:
        shape, identifiers = "count", list(range(1, raw_pages + 1))
    elif isinstance(raw_pages, list):
        shape = "list"
        identifiers = [_page_entry_id(p) for p in raw_pages]
    else:
        # Unsupported shape, bool, nonpositive or over-bound integer: no range is allocated.
        shape, identifiers, invalid = "invalid", [], 1
    valid = [p for p in identifiers if _positive_int(p)]
    invalid += len(identifiers) - len(valid)
    unique = sorted(set(valid))
    duplicate = len(valid) - len(unique)
    out_of_range = sum(1 for p in unique if declared is not None and p > declared)
    # Without a valid declared total there is no range to be inside of.
    in_range = None if declared is None else len(unique) - out_of_range
    missing = None if in_range is None else _missing_page_count(declared, in_range)
    if count_status == "invalid":
        gaps.add("page_count_invalid")
    if invalid or duplicate or out_of_range:
        gaps.add("page_inventory_invalid")
    if declared is not None and not unique:
        gaps.add("page_inventory_missing")
    elif missing:
        gaps.add("page_inventory_incomplete")
    if gaps - {"page_inventory_incomplete", "page_inventory_missing"}:
        status = "inconsistent"
    elif declared is None:
        status = "unknown_total"
    elif not unique:
        status = "missing"
    elif missing:
        status = "incomplete"
    else:
        status = "complete"
    summary = {"declared_page_count": declared, "count_status": count_status, "inventory_shape": shape,
               "supplied_page_entry_count": len(identifiers), "inventoried_page_count": len(unique),
               "in_range_page_count": in_range, "missing_page_count": missing,
               "duplicate_page_count": duplicate, "invalid_page_identifier_count": invalid,
               "out_of_range_page_count": out_of_range, "status": status}
    return unique, summary, gaps


def _coverage(bundle: dict) -> tuple[list[dict], list[str]]:
    units = bundle.get("units", [])
    contexts = bundle.get("source_context", {})
    by_original: dict[str, list[dict]] = {}
    for unit in units:
        by_original.setdefault(unit.get("original_sha256", ""), []).append(unit)
    rows: list[dict] = []
    gaps: set[str] = set()
    for original, context in sorted(contexts.items()):
        source_units = by_original.get(original, [])
        raw_pages = context.get("pages", []) if isinstance(context, dict) else []
        page_numbers, inventory, inventory_gaps = _page_inventory(context)
        gaps.update(inventory_gaps)
        declared_count = inventory["declared_page_count"]
        page_rows = []
        source_visual_claim = isinstance(context, dict) and context.get("visual_review_complete") is True
        page_visual_claim = any(isinstance(p, dict) and p.get("visual_review_complete") is True
                                for p in raw_pages) if isinstance(raw_pages, list) else False
        if source_visual_claim or page_visual_claim:
            gaps.add("claimed_visual_review_unverified")
        for page in page_numbers:
            page_units = [u for u in source_units if page in (u.get("locator", {}).get("page"), u.get("locator", {}).get("page_no"))]
            text_units = [u for u in page_units if isinstance(u.get("text"), str) and bool(u["text"].strip())]
            ocr = any(_is_ocr(u) for u in text_units)
            state = "text_unit_present" if text_units else "no_text_unit"
            if not text_units:
                gaps.add("page_without_text_unit")
            page_meta = next((p for p in raw_pages if isinstance(p, dict) and _page_entry_id(p) == page), {}) if isinstance(raw_pages, list) else {}
            page_rows.append({"page": page, "text_unit_count": len(text_units), "text_coverage": state,
                              "text_sources": sorted({_source_kind(u) for u in text_units if _source_kind(u) != "visual"}),
                              "evidence_attempts": sorted({_source_kind(u) for u in page_units}),
                              "visual_attempt_count": sum(1 for u in page_units if _source_kind(u) == "visual"),
                              "extraction_status": page_meta.get("status"), "extraction_method": page_meta.get("method"),
                              "visual_check_required": page_meta.get("needs_visual_review") is True,
                              "visual_review": "not_established",
                              "ocr_visual_review": "required" if ocr else "not_established"})
        # The declared total and inventory-missing gap now come from _page_inventory.
        # No existing adapter authenticates visual-review claims, so flags are
        # reported as unverified gaps and can never establish completion.
        visual = "not_established"
        context_gaps = []
        if isinstance(context, dict):
            context_gaps = list(context.get("quality_gaps", [])) + list(context.get("normalization_gaps", []))
        if source_visual_claim or page_visual_claim:
            context_gaps.append("claimed_visual_review_unverified")
        for item in context_gaps:
            if isinstance(item, dict):
                gaps.add(str(item.get("code", "source_quality_gap")))
            elif isinstance(item, (str, int, float)):
                gaps.add(str(item))
        if any(_is_ocr(u) for u in source_units):
            gaps.add("ocr_visual_review_required")
        rows.append({"original_sha256": original, "format": context.get("format") if isinstance(context, dict) else None,
                     "native_status": context.get("native_status") if isinstance(context, dict) else None,
                     "unit_count": len(source_units), "declared_unit_count": context.get("declared_unit_count") if isinstance(context, dict) else None,
                     "normalized_unit_count": context.get("normalized_unit_count") if isinstance(context, dict) else len(source_units),
                     "pages": page_rows, "declared_page_count": declared_count, "page_inventory": inventory,
                     "visual_review": visual,
                     "quality_gaps": sorted(str(g.get("code", "source_quality_gap")) if isinstance(g, dict) else str(g) for g in context_gaps),
                     "quality_gap_details": [g for g in context_gaps if isinstance(g, dict)]})
    # Include unit-bearing originals if the context inventory is incomplete.
    for original in sorted(set(by_original) - set(contexts)):
        rows.append({"original_sha256": original, "format": None, "native_status": "unknown",
                     "unit_count": len(by_original[original]), "declared_unit_count": None,
                     "normalized_unit_count": len(by_original[original]), "pages": [], "declared_page_count": None,
                     "page_inventory": _page_inventory(None)[1],
                     "visual_review": "not_established", "quality_gaps": ["source_context_missing"],
                     "quality_gap_details": []})
        gaps.add("source_context_missing")
    return rows, sorted(gaps)


def _source_kind(unit: dict) -> str:
    kind = str(unit.get("kind", "")).lower()
    data = unit.get("data") if isinstance(unit.get("data"), dict) else {}
    if _is_ocr(unit):
        return "ocr"
    if kind in {"visual", "image", "visual_page"} or data.get("visual_review") is True:
        return "visual"
    return "native"


def _is_ocr(unit: dict) -> bool:
    data = unit.get("data") if isinstance(unit.get("data"), dict) else {}
    return bool("ocr" in str(unit.get("kind", "")).lower()
                or "ocr" in str(unit.get("attempt_kind", "")).lower()
                or data.get("ocr_receipt_id") or data.get("ocr") is True or unit.get("ocr") is True)


def _extract(bundle: dict) -> dict:
    bundle = evidence.validate_bundle(bundle)
    units = bundle.get("units", [])
    coverage, gaps = _coverage(bundle)
    classifications: list[dict] = []
    date_mentions: list[dict] = []
    entity_candidates: list[dict] = []
    key_mentions: dict[str, dict] = {}
    facts: list[dict] = []
    context_sha256 = evidence.sha(bundle["source_context"])
    totals = {"classifications": 0, "entity_candidates": 0, "date_mentions": 0,
              "summary_facts": 0, "cross_record_key_candidates": 0}
    untracked_key_occurrences = 0
    for unit in units:
        text = unit.get("text")
        if not isinstance(text, str):
            continue
        for label, pattern in _CLASSIFIERS:
            match = pattern.search(text)
            if match:
                totals["classifications"] += 1
                if len(classifications) < MAX_CLASSIFICATIONS:
                    cite = _citation(bundle, unit, _utf8_offset(text, match.start()), _utf8_offset(text, match.end()))
                    classifications.append({"unit_id": unit["unit_id"], "candidate": label,
                                            "basis": "deterministic_keyword_match", "citation": cite,
                                            "semantic_assessment": "NOT_ASSESSED"})
                break
        for match in _ENTITY.finditer(text):
            totals["entity_candidates"] += 1
            if len(entity_candidates) < MAX_ENTITIES:
                label, value = match.group(1).casefold(), match.group(2).strip()
                if value:
                    value_start = match.start(2) + len(match.group(2)) - len(match.group(2).lstrip())
                    value_end = value_start + len(value)
                    cite = _citation(bundle, unit, _utf8_offset(text, value_start), _utf8_offset(text, value_end))
                    entity_candidates.append({"type": label, "value": value, "unit_id": unit["unit_id"],
                                               "citation": cite, "semantic_assessment": "NOT_ASSESSED"})
        for match in _DATE.finditer(text):
            totals["date_mentions"] += 1
            if len(date_mentions) < MAX_DATES:
                raw = match.group(0)
                cite = _citation(bundle, unit, _utf8_offset(text, match.start()), _utf8_offset(text, match.end()))
                date_mentions.append({**_date_value(raw), "unit_id": unit["unit_id"], "citation": cite,
                                      "role": "date_mention_role_unknown", "semantic_assessment": "NOT_ASSESSED"})
        for match in _KEY.finditer(text):
            key = match.group(1)
            folded = key.casefold()
            item = key_mentions.get(folded)
            if item is None:
                if len(key_mentions) >= MAX_KEY_GROUPS:
                    untracked_key_occurrences += 1
                    continue
                item = {"key_candidate": key, "first_original": None, "second_original": None,
                        "occurrence_count": 0, "citations": []}
                key_mentions[folded] = item
            item["occurrence_count"] += 1
            original = unit["original_sha256"]
            if item["first_original"] is None:
                item["first_original"] = original
            elif original != item["first_original"] and item["second_original"] is None:
                item["second_original"] = original
            if len(item["citations"]) < 2 and original in (item["first_original"], item["second_original"]) and all(
                    cite["original_sha256"] != original for cite in item["citations"]):
                start, end = match.span(1)
                cite = _citation(bundle, unit, _utf8_offset(text, start), _utf8_offset(text, end))
                item["citations"].append({"key_candidate": key, "unit_id": unit["unit_id"],
                                          "original_sha256": original, "citation": cite})
        for match in re.finditer(r"[^\n.!?]{12,512}(?:[.!?](?=\s|$)|(?=\n|$))", text):
            quote = match.group(0).strip()
            if len(quote) < 12:
                continue
            totals["summary_facts"] += 1
            if len(facts) >= MAX_SUMMARY_FACTS:
                continue
            left = match.start() + (len(match.group(0)) - len(match.group(0).lstrip()))
            right = left + len(quote)
            cite = _citation(bundle, unit, _utf8_offset(text, left), _utf8_offset(text, right))
            claim_text = "Source text states: " + quote
            # Bundle integrity was validated once above; preserve the shared claim
            # contract without rehashing every unit for each summary fact.
            derived = evidence.grounded_evidence.derived_claim(bundle, context_sha256, claim_text, [cite])
            facts.append({"claim": derived, "unit_id": unit["unit_id"], "kind": "verbatim_summary_candidate"})
    cross_record_keys = []
    for item in key_mentions.values():
        if item["second_original"] is not None:
            totals["cross_record_key_candidates"] += 1
            if len(cross_record_keys) < MAX_CROSS_RECORD_KEYS:
                cross_record_keys.append({"key_candidate": item["key_candidate"], "occurrences": item["citations"],
                                          "occurrence_count": item["occurrence_count"],
                                          "omitted_occurrence_count": item["occurrence_count"] - len(item["citations"]),
                                          "distinct_originals": "at_least_two", "semantic_assessment": "NOT_ASSESSED"})
    omissions = {"classifications": max(0, totals["classifications"] - len(classifications)),
                 "entity_candidates": max(0, totals["entity_candidates"] - len(entity_candidates)),
                 "date_mentions": max(0, totals["date_mentions"] - len(date_mentions)),
                 "summary_facts": max(0, totals["summary_facts"] - len(facts)),
                 "cross_record_key_candidates": max(0, totals["cross_record_key_candidates"] - len(cross_record_keys)),
                 "key_occurrences_not_tracked": untracked_key_occurrences}
    partial = any(omissions.values()) or bool(gaps)
    return {"schema": SCHEMA, "snapshot_sha256": bundle["snapshot_sha256"], "coverage": coverage,
            "classifications": classifications, "entity_candidates": entity_candidates,
            "date_mentions": date_mentions, "summary_facts": facts,
            "cross_record_key_candidates": sorted(cross_record_keys, key=lambda item: item["key_candidate"].casefold()),
            "candidate_totals": totals, "candidate_omissions": omissions,
            "partial_understanding": partial, "gaps": gaps, "review_acceptance": False,
            "semantic_assessment": "NOT_ASSESSED"}


def analyze(bundle: dict) -> dict:
    """Return bounded, source-backed candidates; this never accepts a review."""
    return _extract(bundle)


def apply_model(bundle: dict, proposal: dict) -> dict:
    """Bind model-proposed claims to current citations or return an explicit abstention."""
    validated = evidence.validate_bundle(bundle)
    context_sha256 = evidence.sha(validated["source_context"])
    result = {"schema": SCHEMA, "snapshot_sha256": validated["snapshot_sha256"],
              "claims": [], "status": "abstained", "review_acceptance": False,
              "semantic_assessment": "NOT_ASSESSED", "gaps": []}
    if not isinstance(proposal, dict):
        result["gaps"].append("model_proposal_shape_invalid")
        return result
    if proposal.get("abstain") is True:
        reason = proposal.get("reason")
        result["abstention_reason"] = reason[:500] if isinstance(reason, str) else "model_explicit_abstention"
        result["gaps"].append("model_abstained")
        return result
    proposed = proposal.get("claims")
    if not isinstance(proposed, list) or not proposed:
        result["gaps"].append("model_claims_missing_or_empty")
        return result
    if len(proposed) > 50:
        result["gaps"].append("model_claim_count_bound")
        return result
    try:
        for item in proposed:
            if not isinstance(item, dict) or not isinstance(item.get("text"), str):
                raise ValueError("model_claim_shape_invalid")
            cites = item.get("citations")
            if not isinstance(cites, list) or not cites:
                raise ValueError("model_claim_uncited")
            for cite in cites:
                # validate_bundle above pins this snapshot. Validate each support against
                # that single validated view rather than hashing every unit per citation.
                evidence.grounded_evidence.validate_support(cite, validated)
            result["claims"].append(evidence.grounded_evidence.derived_claim(validated, context_sha256, item["text"], cites))
    except (KeyError, TypeError, ValueError) as exc:
        # Never return a partially accepted model response.
        result["claims"] = []
        code = str(exc)
        result["gaps"].append(code if code and len(code) <= 96 else "model_claim_validation_failed")
        return result
    result["status"] = "structurally_cited_not_semantically_assessed"
    return result
