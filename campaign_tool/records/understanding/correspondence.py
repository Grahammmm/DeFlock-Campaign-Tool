"""Conservative, portable tracking for cited correspondence candidates."""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import re
from typing import Any, Iterable

from . import evidence

SCHEMA = "records-understanding-correspondence-v1"
TYPES = {"request", "acknowledgment", "determination", "clarification", "promise",
         "delivery", "partial_delivery", "withdrawal", "other"}
MARKERS = (
    ("withdrawal", re.compile(r"\b(withdraw|withdrawn|cancel(?:led|ed)? the request)\b", re.I)),
    ("partial_delivery", re.compile(r"\b(partial(?:ly)?|some of|portion of|remaining records|will follow)\b", re.I)),
    ("delivery", re.compile(r"\b(attached|enclosed|provided|produced|responsive records|records are available)\b", re.I)),
    ("promise", re.compile(r"\b(will provide|will send|expect to provide|will respond by)\b", re.I)),
    ("determination", re.compile(r"\b(determination|estimated response date)\b", re.I)),
    ("clarification", re.compile(r"\b(please clarify|clarification|narrow your request)\b", re.I)),
    ("acknowledgment", re.compile(r"\b(received your request|acknowledge receipt|request has been received|we received)\b", re.I)),
    ("request", re.compile(r"\b(public records request|records request|request the following|please provide)\b", re.I)),
)


def _s(value: Any) -> str:
    return " ".join(value.strip().split()) if isinstance(value, str) else ""


def _id(agency: str, number: str) -> str:
    def escape(component: str) -> str:
        return component.casefold().replace("%", "%25").replace(":", "%3a")

    return escape(agency) + "::" + escape(number)


_NUMERIC_OFFSET = re.compile(r"(?<=[0-9])[+-]([0-9]{2}):?([0-9]{2})$")


def _offset_components_in_range(raw: str) -> bool:
    """Check a supplied compact/colon numeric offset before parser normalization."""
    match = _NUMERIC_OFFSET.search(raw)
    return match is None or (int(match.group(1)) <= 23 and int(match.group(2)) <= 59)


def _to_utc(parsed: datetime) -> datetime | None:
    """Bounded UTC conversion: an unrepresentable instant is unresolved, never an escaping error."""
    try:
        return parsed.astimezone(timezone.utc)
    except OverflowError:
        return None


def _time(value: Any) -> tuple[datetime | None, str]:
    if not isinstance(value, str) or not value.strip():
        return None, "time_unknown"
    raw = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        try:
            date.fromisoformat(raw)
            return None, "date_only_timezone_unknown"
        except ValueError:
            return None, "invalid_time"
    if not _offset_components_in_range(raw):
        return None, "invalid_time"
    raw = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
    raw = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", raw)
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None, "invalid_time"
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None, "timezone_unknown"
    utc = _to_utc(parsed)
    return (utc, "explicit_offset") if utc is not None else (None, "invalid_time")


def _asof(value: str | datetime) -> tuple[datetime, str, str]:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            return datetime.min.replace(tzinfo=timezone.utc), "timezone_unknown", value.isoformat()
        utc = _to_utc(value)
        if utc is None:
            return datetime.min.replace(tzinfo=timezone.utc), "invalid_time", value.isoformat()
        return utc, "explicit_offset", utc.isoformat().replace("+00:00", "Z")
    parsed, status = _time(value)
    return (parsed or datetime.min.replace(tzinfo=timezone.utc), status, value if isinstance(value, str) else "")


def _hash(obj: Any) -> str:
    blob = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(blob).hexdigest()


def _classify(event: dict) -> str:
    kind = _s(event.get("event_type") or event.get("kind")).casefold()
    if kind in TYPES:
        return kind
    text = event.get("text")
    if isinstance(text, str):
        for label, pattern in MARKERS:
            if pattern.search(text):
                return label
    return "other"


def _items(event: dict, name: str) -> list[str]:
    values = event.get(name, [])
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        return []
    return sorted({_s(item) for item in values if _s(item)}, key=str.casefold)


def _event_key(event: dict) -> str:
    message_id = _s(event.get("message_id") or event.get("event_id"))
    if message_id:
        return "message:" + message_id.casefold()
    fields = ("agency_id", "request_number", "request_numbers", "occurred_at", "event_type",
              "kind", "text", "items_requested", "items_delivered", "due_at", "due_kind")
    return "sha256:" + _hash({key: event.get(key) for key in fields if key in event})


def _validate_citations(event: dict, bundle: dict | None, event_type: str) -> list[dict]:
    citations = event.get("citations", [])
    if not isinstance(citations, list):
        raise ValueError("event_citations_invalid")
    if bundle is None:
        return citations
    if not citations:
        raise ValueError("event_citation_required")
    for citation in citations:
        evidence.validate_citation(citation, bundle)
    claim_text = _s(event.get("claim_text")) or f"Correspondence candidate classified as {event_type}."
    evidence.claim(bundle, claim_text, citations)
    return citations


def _draft(request_id: str, missing: list[str], status: str) -> dict:
    if status == "withdrawn":
        text = "This request is recorded as withdrawn. Review the correspondence timeline before taking further action."
    elif missing:
        text = "Please confirm the status of these requested item categories: " + "; ".join(missing[:12]) + "."
    else:
        text = "Please confirm the current status of this public records request and any remaining steps."
    return {"text": text, "purpose": "private_review_draft", "send": False,
            "requires_human_review": True, "request_id": request_id}


def _deadline(rule: dict, field: str, kind: str, request_id: str, asof: datetime, asof_status: str,
              bundle: dict | None) -> tuple[dict | None, str | None]:
    value = rule.get(field)
    if value is None:
        return None, None
    from . import policy
    policy_versions = rule.get("policy_versions")
    version_id = _s(rule.get("version_id"))
    requirement_id = _s(rule.get("requirement_id"))
    basis = _s(rule.get("basis"))
    if not isinstance(policy_versions, list) or not version_id or not requirement_id or not basis:
        return None, kind + "_deadline_policy_basis_incomplete"
    validation = policy.validate_versions(policy_versions)
    if not validation.get("valid"):
        return None, kind + "_deadline_policy_versions_invalid"
    selection = policy.select_version(
        policy_versions, agency_id=_s(rule.get("agency_id")),
        jurisdiction=_s(rule.get("jurisdiction")), regulated_role=_s(rule.get("regulated_role")),
        event_at=rule.get("event_at"))
    if selection.get("status") != "selected" or selection.get("version", {}).get("version_id") != version_id:
        return None, kind + "_deadline_policy_version_unresolved"
    requirements = policy.requirements_for(
        policy_versions, agency_id=_s(rule.get("agency_id")),
        jurisdiction=_s(rule.get("jurisdiction")), regulated_role=_s(rule.get("regulated_role")),
        event_at=rule.get("event_at"))
    if requirements.get("status") != "selected":
        return None, kind + "_deadline_requirement_unresolved"
    requirement = next((item for item in requirements.get("requirements", [])
                        if item.get("requirement_id") == requirement_id), None)
    if requirement is None:
        return None, kind + "_deadline_requirement_unknown"
    citations = rule.get("citations", [])
    if bundle is not None:
        if not isinstance(citations, list) or not citations:
            return None, kind + "_deadline_citation_required"
        try:
            for cite in citations:
                evidence.validate_citation(cite, bundle)
            evidence.claim(bundle, basis, citations)
        except (KeyError, TypeError, ValueError):
            return None, kind + "_deadline_citation_invalid"
    parsed, status = _time(value)
    if parsed is None:
        return None, kind + "_deadline_" + status
    return ({"request_id": request_id, "action": "track_deadline", "deadline_kind": kind,
             "due_at": parsed.isoformat().replace("+00:00", "Z"),
             "status": ("undetermined_as_of_timezone" if asof_status != "explicit_offset" else ("overdue" if parsed < asof else "upcoming")),
             "basis": {"version_id": version_id, "requirement_id": requirement_id,
                       "description": basis, "requirement": requirement,
                       "event_at": rule.get("event_at"), "citations": citations},
             "source": "explicit_validated_policy_version"}, None)


def _decode_source_quote(citation: dict, bundle: dict) -> tuple[str | None, str]:
    try:
        _validate_support(citation, bundle)
    except (KeyError, TypeError, ValueError):
        return None, "unverified"
    quote = citation.get("quote")
    if not isinstance(quote, str):
        return None, "invalid_quote"
    if citation.get("schema") == "records-grounded-data-evidence-v1":
        try:
            import json
            quote = json.loads(quote)
        except (TypeError, ValueError):
            return None, "invalid_quote"
    return (quote, "verified") if isinstance(quote, str) else (None, "invalid_quote")


def _verified_metadata_source(event: dict, bundle: dict | None) -> tuple[str | None, str]:
    if bundle is None:
        return None, "unchecked"
    citations = event.get("citations") if isinstance(event.get("citations"), list) else []
    date_cite = event.get("message_date_citation")
    id_cite = event.get("message_id_citation")
    primary = [cite for cite in citations if isinstance(cite, dict)
               and cite != date_cite and cite != id_cite]
    originals = {cite.get("original_sha256") for cite in primary
                 if isinstance(cite.get("original_sha256"), str)}
    if len(originals) != 1:
        return None, "source_ambiguous" if originals else "source_missing"
    original = next(iter(originals))
    if event.get("source_original_sha256") != original:
        return None, "source_flag_mismatch"
    for citation in (date_cite, id_cite):
        if isinstance(citation, dict) and citation.get("original_sha256") != original:
            return None, "metadata_source_mismatch"
    return original, "verified"


def _verified_message_date(event: dict, bundle: dict | None) -> tuple[str | None, str, dict | None]:
    """Derive message chronology from a source-bound exact citation, never caller text."""
    citation = event.get("message_date_citation")
    if not isinstance(citation, dict) or bundle is None:
        return None, "unverified" if event.get("message_date") else "unknown", None
    _, source_status = _verified_metadata_source(event, bundle)
    if source_status != "verified":
        return None, source_status, citation
    quote, quote_status = _decode_source_quote(citation, bundle)
    if quote_status != "verified" or not quote.strip():
        return None, "unverified", citation
    try:
        parsed = parsedate_to_datetime(quote)
    except (TypeError, ValueError, OverflowError):
        return None, "invalid_date", citation
    if parsed is None or parsed.tzinfo is None or parsed.utcoffset() is None:
        return None, "timezone_unknown", citation
    utc = _to_utc(parsed)
    if utc is None:
        return None, "invalid_date", citation
    normalized = utc.isoformat().replace("+00:00", "Z")
    supplied = event.get("message_date")
    if supplied is not None and supplied != normalized:
        return None, "source_value_mismatch", citation
    return normalized, "explicit_offset", citation


def _verified_message_id(event: dict, bundle: dict | None) -> tuple[str | None, str, dict | None]:
    supplied = event.get("message_id")
    citation = event.get("message_id_citation")
    if bundle is None:
        return (_s(supplied) or None, "unchecked", citation if isinstance(citation, dict) else None)
    if not supplied and not isinstance(citation, dict):
        return None, "unknown", None
    if not isinstance(citation, dict):
        return None, "unverified", None
    _, source_status = _verified_metadata_source(event, bundle)
    if source_status != "verified":
        return None, source_status, citation
    quote, quote_status = _decode_source_quote(citation, bundle)
    if quote_status != "verified" or quote != supplied:
        return None, "source_value_mismatch", citation
    return quote, "verified", citation

def _validate_support(citation: dict, bundle: dict) -> None:
    if citation.get("schema") == "records-grounded-data-evidence-v1":
        evidence.grounded_evidence.validate_support(citation, bundle)
    else:
        evidence.validate_citation(citation, bundle)


def track(events: Iterable[dict], *, as_of: str | datetime, deadline_rules: Iterable[dict] | None = None,
          bundle: dict | None = None) -> dict:
    """Create a review-only tracker from explicit events; never sends follow-ups."""
    asof, asof_status, asof_out = _asof(as_of)
    snapshot = evidence.validate_bundle(bundle) if bundle is not None else None
    grouped, holds = {}, []
    for index, raw in enumerate(events):
        if not isinstance(raw, dict):
            holds.append({"index": index, "reason": "event_shape_invalid"})
            continue
        agency = _s(raw.get("agency_id"))
        many = raw.get("request_numbers")
        if isinstance(many, list):
            many = sorted({_s(item) for item in many if _s(item)}, key=str.casefold)
            if len(many) != 1:
                holds.append({"index": index, "agency_id": agency or None, "request_numbers": many,
                              "reason": "request_number_ambiguous" if many else "request_number_missing"})
                continue
            number = many[0]
        else:
            number = _s(raw.get("request_number"))
        if not agency or not number:
            holds.append({"index": index, "reason": "agency_or_request_number_missing"})
            continue
        event = dict(raw, agency_id=agency, request_number=number)
        event_type = _classify(event)
        for metadata_hold in raw.get("metadata_holds", []) if isinstance(raw.get("metadata_holds"), list) else []:
            if isinstance(metadata_hold, dict):
                holds.append({"index": index, "agency_id": agency, "request_number": number,
                              "reason": _s(metadata_hold.get("reason")) or "message_header_metadata_held"})
        try:
            cites = _validate_citations(event, snapshot, event_type)
        except (KeyError, TypeError, ValueError) as exc:
            holds.append({"index": index, "agency_id": agency, "request_number": number,
                          "reason": str(exc)[:96] or "event_citation_invalid"})
            continue
        timestamp, time_status = _time(event.get("occurred_at"))
        time_source = "event_time"
        message_date, message_date_status, message_date_citation = _verified_message_date(event, snapshot)
        message_id, message_id_status, message_id_citation = _verified_message_id(event, snapshot)
        if snapshot is not None and event.get("message_id") and message_id is None:
            holds.append({"index": index, "agency_id": agency, "request_number": number,
                          "reason": "message_id_" + message_id_status})
        if timestamp is None and message_date is not None:
            timestamp, message_status = _time(message_date)
            if timestamp is not None:
                time_status = "message_date_explicit_offset"
                time_source = "message_date"
            else:
                time_status = "message_date_" + message_status
                time_source = "message_date"
        elif timestamp is None and message_date_status not in (None, "unknown", "explicit_offset"):
            time_status = "message_date_" + str(message_date_status)
            time_source = "message_date"
        if event.get("message_date") and message_date is None:
            holds.append({"index": index, "agency_id": agency, "request_number": number,
                          "reason": "message_date_unverified" if message_date_status == "unverified" else "message_date_" + str(message_date_status)})
        request_id = _id(agency, number)
        event_for_key = dict(event)
        if snapshot is not None and event.get("message_id") and message_id is None:
            event_for_key.pop("message_id", None)
            event_for_key.pop("event_id", None)
        event_key = _event_key(event_for_key)
        group_key = (request_id, event_key)
        occurrence = {"occurrence_id": _s(event.get("occurrence_id")) or f"input:{index}",
                      "occurred_at": event.get("occurred_at") if isinstance(event.get("occurred_at"), str) else None,
                      "time_status": time_status, "source_id": _s(event.get("source_id")) or None}
        fact = {"event_type": event_type, "claim_text": _s(event.get("claim_text")) or None,
                "citations": cites, "items_requested": _items(event, "items_requested"),
                "items_delivered": _items(event, "items_delivered"),
                "due_at": event.get("due_at") if isinstance(event.get("due_at"), str) else None,
                "due_kind": event.get("due_kind") if event.get("due_kind") in {"promised_delivery", "determination"} else None,
                "withdrawal_review_status": "unreviewed" if event_type == "withdrawal" else None,
                "message_date": message_date,
                "message_date_status": message_date_status,
                "message_date_citation": message_date_citation,
                "message_id_citation": message_id_citation,
                "time_sort": timestamp.isoformat() if timestamp else None, "time_status": time_status,
                "time_source": time_source, "deduplication_key": event_key}
        if group_key in grouped:
            prior = grouped[group_key]["event"]
            keys = ("event_type", "claim_text", "items_requested", "items_delivered", "due_at", "due_kind",
                    "withdrawal_review_status", "message_date", "message_id_citation", "message_date_citation")
            if any(prior.get(key) != fact.get(key) for key in keys):
                holds.append({"index": index, "request_id": request_id, "reason": "duplicate_message_content_conflict"})
                continue
        else:
            grouped[group_key] = {"request_id": request_id, "agency_id": agency,
                                  "request_number": number, "event": fact, "occurrences": []}
        grouped[group_key]["occurrences"].append(occurrence)

    requests = {}
    for group in grouped.values():
        request = requests.setdefault(group["request_id"], {
            "request_id": group["request_id"], "agency_id": group["agency_id"],
            "request_number": group["request_number"], "timeline": [], "occurrence_count": 0})
        row = dict(group["event"])
        row["occurrences"] = sorted(group["occurrences"], key=lambda x: (x["occurred_at"] or "", x["occurrence_id"]))
        row["occurrence_count"] = len(group["occurrences"])
        request["timeline"].append(row)
        request["occurrence_count"] += len(group["occurrences"])

    due_actions = []
    for request in requests.values():
        request["timeline"].sort(key=lambda row: (row["time_sort"] is None, row["time_sort"] or "", row["deduplication_key"]))
        requested, delivered = set(), set()
        for row in request["timeline"]:
            requested.update(row["items_requested"])
            delivered.update(row["items_delivered"])
            if row["due_at"] and row["due_kind"] == "promised_delivery":
                moment, status = _time(row["due_at"])
                basis = {"event_deduplication_key": row["deduplication_key"],
                         "citations": row["citations"]}
                if status == "date_only_timezone_unknown":
                    due_actions.append({"request_id": request["request_id"], "action": "follow_up_on_promised_delivery",
                                        "deadline_kind": "promised_delivery", "due_day": row["due_at"],
                                        "status": status, "basis": basis, "draft_only": True})
                elif moment is None:
                    holds.append({"request_id": request["request_id"], "reason": "promise_due_date_" + status})
                else:
                    due_actions.append({"request_id": request["request_id"], "action": "follow_up_on_promised_delivery",
                                        "deadline_kind": "promised_delivery",
                                        "due_at": moment.isoformat().replace("+00:00", "Z"),
                                        "status": ("undetermined_as_of_timezone" if asof_status != "explicit_offset" else ("overdue" if moment < asof else "upcoming")),
                                        "basis": basis, "draft_only": True})
        missing = sorted(requested - delivered, key=str.casefold)
        timeline = request["timeline"]
        kinds = [row["event_type"] for row in timeline]
        order_uncertain = len(timeline) > 1 and any(row["time_sort"] is None for row in timeline)
        # No trusted review adapter exists in this portable module. Caller-provided
        # confirmation flags cannot turn a phrase candidate into a terminal status.
        candidate_withdrawals = [row for row in timeline if row["event_type"] == "withdrawal"]
        if candidate_withdrawals:
            holds.append({"request_id": request["request_id"], "reason": "withdrawal_candidate_requires_review"})
        if order_uncertain:
            holds.append({"request_id": request["request_id"], "reason": "event_order_uncertain"})
            status = "withdrawal_candidate_requires_review" if candidate_withdrawals else "event_order_uncertain"
        elif candidate_withdrawals:
            status = "withdrawal_candidate_requires_review"
        elif missing:
            status = "partially_delivered" if delivered else "awaiting_items"
        elif "delivery" in kinds or "partial_delivery" in kinds:
            status = "delivery_recorded_scope_unverified"
        elif "promise" in kinds:
            status = "awaiting_promised_delivery"
        elif "acknowledgment" in kinds:
            status = "acknowledged"
        else:
            status = "open"
        promises = {(row["due_at"], row["claim_text"]) for row in timeline if row["event_type"] == "promise"}
        request.update({"status": status, "requested_items": sorted(requested, key=str.casefold),
                        "delivered_items": sorted(delivered, key=str.casefold),
                        "outstanding_items": missing if requested else [],
                        "outstanding_scope": "explicit_item_list" if requested else "not_structured_or_unknown",
                        "contradictions": ["multiple_distinct_promises_require_review"] if len(promises) > 1 else [],
                        "semantic_assessment": "NOT_ASSESSED",
                        "private_followup_draft": _draft(request["request_id"], missing, status)})

    for index, rule in enumerate(deadline_rules or []):
        if not isinstance(rule, dict):
            holds.append({"index": index, "reason": "deadline_rule_shape_invalid"})
            continue
        agency, number = _s(rule.get("agency_id")), _s(rule.get("request_number"))
        request_id = _id(agency, number) if agency and number else ""
        if not request_id or request_id not in requests:
            holds.append({"index": index, "request_id": request_id or None, "reason": "deadline_rule_unknown_request"})
            continue
        for field, kind in (("determination_due_at", "determination"), ("delivery_due_at", "delivery")):
            action, reason = _deadline(rule, field, kind, request_id, asof, asof_status, snapshot)
            if action:
                due_actions.append(action)
            if reason:
                holds.append({"index": index, "request_id": request_id, "reason": reason})
    # Result payloads may borrow nested citations/requirements while building.
    # Detach once at the public boundary, preserving values and internal sharing.
    return deepcopy({"schema": SCHEMA, "as_of": asof_out, "as_of_status": asof_status,
            "snapshot_sha256": snapshot.get("snapshot_sha256") if snapshot else None,
            "requests": [requests[key] for key in sorted(requests)],
            "due_actions": sorted(due_actions, key=lambda row: (row.get("due_at", row.get("due_day", "")), row["request_id"], row["deadline_kind"])),
            "holds": sorted(holds, key=lambda row: (row.get("index", -1), row.get("request_id", ""), row["reason"])),
            "semantic_assessment": "NOT_ASSESSED", "review_acceptance": False})


def _header_pairs(unit: dict) -> list[tuple[int, str, str]]:
    data = unit.get("data") if isinstance(unit.get("data"), dict) else {}
    headers = data.get("headers")
    rows: list[tuple[int, str, str]] = []
    if isinstance(headers, dict):
        for index, (key, value) in enumerate(headers.items()):
            values = value if isinstance(value, list) else [value]
            for item in values:
                if isinstance(item, str):
                    rows.append((index, str(key).strip(), item.strip()))
                elif isinstance(item, dict):
                    header_value = item.get("value") or item.get("raw")
                    if isinstance(header_value, str):
                        rows.append((index, str(key).strip(), header_value.strip()))
    elif isinstance(headers, list):
        for index, item in enumerate(headers):
            if isinstance(item, (list, tuple)) and len(item) == 2 and all(isinstance(part, str) for part in item):
                rows.append((index, item[0].strip(), item[1].strip()))
            elif isinstance(item, str) and ":" in item:
                key, value = item.split(":", 1)
                rows.append((index, key.strip(), value.strip()))
            elif isinstance(item, dict):
                key = item.get("name") or item.get("key")
                value = item.get("value") or item.get("raw")
                if isinstance(key, str) and isinstance(value, str):
                    rows.append((index, key.strip(), value.strip()))
    return rows


def _exact_header_citation(unit: dict, value: str, bundle: dict,
                           pointer: str | None = None) -> dict | None:
    text = unit.get("text")
    if isinstance(text, str) and value:
        start = text.find(value)
        if start >= 0:
            cite = evidence.citation(unit, len(text[:start].encode("utf-8")),
                                     len(text[:start + len(value)].encode("utf-8")))
            evidence.validate_citation(cite, bundle)
            return cite
    if pointer:
        cite = evidence.grounded_evidence.data_pointer_support(unit, pointer)
        evidence.grounded_evidence.validate_support(cite, bundle)
        return cite
    return None

def _message_metadata(bundle: dict) -> tuple[dict[str, dict], list[dict]]:
    by_original: dict[str, list[dict]] = {}
    holds: list[dict] = []
    for unit in bundle.get("units", []):
        headers = _header_pairs(unit)
        if not headers:
            continue
        original = unit.get("original_sha256")
        row = {"message_ids": [], "dates": [], "unit_id": unit.get("unit_id")}
        for header_index, key, value in headers:
            folded = key.casefold()
            if folded not in {"message-id", "date"}:
                continue
            citation = _exact_header_citation(
                unit, value, bundle, pointer=f"/headers/{header_index}/1")
            if citation is None:
                holds.append({"original_sha256": original, "unit_id": unit.get("unit_id"),
                               "reason": f"{folded.replace('-', '_')}_header_unlocatable"})
                continue
            if folded == "message-id":
                row["message_ids"].append({"value": value, "citation": citation})
            else:
                parsed = None
                try:
                    parsed = parsedate_to_datetime(value)
                except (TypeError, ValueError, OverflowError):
                    pass
                utc = None
                if parsed is not None and parsed.tzinfo is not None and parsed.utcoffset() is not None:
                    utc = _to_utc(parsed)
                    if utc is None:
                        parsed = None  # unrepresentable UTC instant: hold as invalid_date
                if utc is None:
                    status = "timezone_unknown" if parsed is not None else "invalid_date"
                    row["dates"].append({"value": value, "citation": citation, "normalized": None,
                                         "status": status})
                    holds.append({"original_sha256": original, "reason": "message_date_" + status})
                else:
                    row["dates"].append({"value": value, "citation": citation,
                                         "normalized": utc.isoformat().replace("+00:00", "Z"),
                                         "status": "explicit_offset"})
        by_original.setdefault(original, []).append(row)
    result = {}
    for original, rows in by_original.items():
        ids = [item for row in rows for item in row["message_ids"]]
        dates = [item for row in rows for item in row["dates"]]
        unique_ids = {item["value"].casefold(): item for item in ids}
        unique_dates = {item["normalized"] or ("raw:" + item["value"]): item for item in dates}
        meta = {}
        if len(unique_ids) == 1:
            meta["message_id"] = next(iter(unique_ids.values()))
        elif len(unique_ids) > 1:
            holds.append({"original_sha256": original, "reason": "message_id_header_conflict"})
        if len(unique_dates) == 1:
            meta["message_date"] = next(iter(unique_dates.values()))
        elif len(unique_dates) > 1:
            holds.append({"original_sha256": original, "reason": "message_date_header_conflict"})
        result[original] = meta
    return result, holds


def events_from_bundle(bundle: dict, agency_id: str, request_numbers: Iterable[str]) -> list[dict]:
    """Return cited candidates scoped to a request and same-original native headers.

    RFC Date is message chronology metadata only. It is never assigned as an
    event's request-received, agency-received, determination, or audit time.
    """
    validated = evidence.validate_bundle(bundle)
    agency = _s(agency_id)
    requested = sorted({_s(item) for item in request_numbers if _s(item)}, key=str.casefold)
    if not agency or not requested:
        raise ValueError("agency_and_request_numbers_required")
    metadata, header_holds = _message_metadata(validated)
    holds_by_original: dict[str, list[dict]] = {}
    for hold in header_holds:
        holds_by_original.setdefault(hold.get("original_sha256") or "", []).append(hold)
    result = []
    for unit in validated.get("units", []):
        text = unit.get("text")
        if not isinstance(text, str):
            continue
        matches = []
        for number in requested:
            matches.extend((number, hit.start(), hit.end()) for hit in re.finditer(
                r"(?<![A-Za-z0-9])" + re.escape(number) + r"(?![A-Za-z0-9])", text, re.I))
        if not matches:
            continue
        numbers = sorted({item[0] for item in matches}, key=str.casefold)
        found = next(((label, pattern.search(text)) for label, pattern in MARKERS if pattern.search(text)), None)
        if found:
            kind, hit = found
            start, end = hit.span()
        else:
            kind, (number, start, end) = "other", matches[0]
        start_byte, end_byte = len(text[:start].encode()), len(text[:end].encode())
        event_citation = evidence.citation(unit, start_byte, end_byte)
        evidence.validate_citation(event_citation, validated)
        number, number_start, number_end = matches[0]
        number_citation = evidence.citation(unit, len(text[:number_start].encode()),
                                             len(text[:number_end].encode()))
        evidence.validate_citation(number_citation, validated)
        citations = [event_citation]
        if number_citation != event_citation:
            citations.append(number_citation)
        meta = metadata.get(unit.get("original_sha256"), {})
        message = meta.get("message_id")
        date_header = meta.get("message_date")
        message_id = message.get("value") if message else None
        message_date = date_header.get("normalized") if date_header else None
        message_id_citation = message.get("citation") if message else None
        message_date_citation = date_header.get("citation") if date_header else None
        for header_cite in (message_id_citation, message_date_citation):
            if header_cite and header_cite not in citations:
                citations.append(header_cite)
        candidate_status = "NOT_ASSESSED"
        if kind == "withdrawal":
            candidate_status = "withdrawal_requires_review"
        result.append({"agency_id": agency, "request_numbers": numbers, "event_type": kind,
                       "occurred_at": None, "message_date": message_date,
                       "message_date_status": date_header.get("status") if date_header else "unknown",
                       "message_date_citation": message_date_citation,
                       "message_id_citation": message_id_citation,
                       "occurrence_id": unit.get("unit_id"), "source_id": unit.get("import_id"),
                       "source_original_sha256": unit.get("original_sha256"),
                       "message_id": message_id, "claim_text": "Correspondence text contains a candidate event phrase.",
                       "citations": citations, "candidate_status": candidate_status,
                       "metadata_holds": holds_by_original.get(unit.get("original_sha256"), []),
                       "withdrawal_review_status": "unreviewed" if kind == "withdrawal" else None})
    return result
