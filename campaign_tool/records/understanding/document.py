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


def _date_value(raw: str) -> dict[str, Any]:
    candidate = {"raw": raw, "value": None, "time_basis": "unknown"}
    if "T" not in raw and "t" not in raw and " " not in raw:
        try:
            candidate.update(value=date.fromisoformat(raw).isoformat(), time_basis="local_date_only")
        except ValueError:
            candidate["parse_status"] = "invalid_date"
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
        if isinstance(raw_pages, int):
            page_numbers = list(range(1, raw_pages + 1))
        elif isinstance(raw_pages, list):
            page_numbers = [p.get("page", p.get("page_no")) if isinstance(p, dict) else p for p in raw_pages]
        else:
            page_numbers = []
        page_numbers = sorted({p for p in page_numbers if type(p) is int and p > 0})
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
            page_meta = next((p for p in raw_pages if isinstance(p, dict) and p.get("page_no", p.get("page")) == page), {}) if isinstance(raw_pages, list) else {}
            page_rows.append({"page": page, "text_unit_count": len(text_units), "text_coverage": state,
                              "text_sources": sorted({_source_kind(u) for u in text_units if _source_kind(u) != "visual"}),
                              "evidence_attempts": sorted({_source_kind(u) for u in page_units}),
                              "visual_attempt_count": sum(1 for u in page_units if _source_kind(u) == "visual"),
                              "extraction_status": page_meta.get("status"), "extraction_method": page_meta.get("method"),
                              "visual_check_required": page_meta.get("needs_visual_review") is True,
                              "visual_review": "not_established",
                              "ocr_visual_review": "required" if ocr else "not_established"})
        declared_count = context.get("page_count", context.get("expected_page_count")) if isinstance(context, dict) else None
        if type(declared_count) is int and declared_count > 0 and not page_numbers:
            gaps.add("page_inventory_missing")
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
                     "pages": page_rows, "declared_page_count": declared_count,
                     "visual_review": visual,
                     "quality_gaps": sorted(str(g.get("code", "source_quality_gap")) if isinstance(g, dict) else str(g) for g in context_gaps),
                     "quality_gap_details": [g for g in context_gaps if isinstance(g, dict)]})
    # Include unit-bearing originals if the context inventory is incomplete.
    for original in sorted(set(by_original) - set(contexts)):
        rows.append({"original_sha256": original, "format": None, "native_status": "unknown",
                     "unit_count": len(by_original[original]), "declared_unit_count": None,
                     "normalized_unit_count": len(by_original[original]), "pages": [], "declared_page_count": None,
                     "visual_review": "not_established", "quality_gaps": ["source_context_missing"],
                     "quality_gap_details": []})
        gaps.add("source_context_missing")
    return rows, sorted(gaps)


def _source_kind(unit: dict) -> str:
    kind = str(unit.get("kind", "")).lower()
    attempt = str(unit.get("attempt_kind", "")).lower()
    data = unit.get("data") if isinstance(unit.get("data"), dict) else {}
    if _is_ocr(unit) or "ocr" in kind or "ocr" in attempt:
        return "ocr"
    if kind in {"visual", "image", "visual_page"} or data.get("visual_review") is True:
        return "visual"
    return "native"


def _is_ocr(unit: dict) -> bool:
    data = unit.get("data") if isinstance(unit.get("data"), dict) else {}
    return bool("ocr" in str(unit.get("attempt_kind", "")).lower() or data.get("ocr_receipt_id") or data.get("ocr") is True or unit.get("ocr") is True)


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
