"""Private, read-only response-state leads. Machine classification needs human review."""
import argparse
import json
import re
import sys

from . import item_tiers, playbook
from .outcomes import received_items, response_items, timeline

SCHEMA = "records-followup-state-v1"
_BOOK = playbook.load()  # Immutable shipped rules; classify itself performs no IO.
_AGENCY = frozenset({"acknowledged", "extension notice", "determination", "production",
                     "denial", "no records", "fee estimate", "clarification requested", "received"})
_ANSWERS = _AGENCY - {"extension notice"}
_NEEDS = ("withheld", "audit_no_records", "other_no_records", "not_addressed", "produced")


def _number(ask):
    match = re.fullmatch(r"ask-([1-9][0-9]*)", str(ask.get("ask_id", "")))
    if not match:
        raise ValueError("followup_state_ask_id")
    return int(match[1])


def classify(request, items, as_of):
    """Pure: timeline request + matching response-items asks (or None) + ISO day.

    When ratings are absent, request.asked/request.asks supplies the original scope.
    Summary labels never replace the exhaustive item_needs partition.
    """
    today = timeline._day(as_of)
    if today is None:
        raise ValueError("followup_state_as_of")
    asks = items if items else request.get("asks")
    if asks is None:
        asks = response_items.split_asked(request.get("asked"))
    needs = {key: [] for key in _NEEDS}
    events = [e for e in request.get("events", []) if not e.get("superseded")
              and timeline._day(e.get("date"))
              and (timeline._day(e["date"]) <= today or e.get("type") == "promised date")]
    events.sort(key=lambda e: (e["date"], e["type"]))
    no_records = any(e["type"] == "no records" for e in events)
    rated = any(a.get("rating") in response_items.RATINGS for a in asks)
    open_items, audits_none, tier1, seen = [], False, False, set()
    for ask in asks:
        number = _number(ask)
        if number in seen:
            raise ValueError("followup_state_duplicate_ask")
        seen.add(number)
        text, rating = ask.get("text") or "", ask.get("rating")
        tier = playbook.tier_for(_BOOK, text)
        audits = bool(item_tiers.logs_audits([{"tier": tier, "text": text,
                                             "state": item_tiers.state_of(rating)}])["audits"]["asked"])
        if rating in ("withheld", "exemption_cited"):
            key = "withheld"
        elif rating == "no_records_stated" or (no_records and not rated):
            key = "audit_no_records" if audits else "other_no_records"
        elif rating == "answered":
            key = "produced"
        else:
            key = "not_addressed"
        needs[key].append(number)
        audits_none |= audits and (rating == "no_records_stated" or no_records)
        if key != "produced":
            open_items.append(number)
            tier1 |= tier == 1
    for values in needs.values():
        values.sort()
    flags = []
    for flag, active in (("open_tier1", tier1), ("audit_items_no_records", audits_none),
                         ("extension_limit_exceeded", (request.get("extension_chain") or {}).get("exceeds_14_days")),
                         ("items_not_rated", items is None), ("register_issues", request.get("issues"))):
        if active:
            flags.append(flag)
    agency = [e for e in events if e["type"] in _AGENCY]
    sent = max((e["date"] for e in events if e["type"] in ("sent", "our follow-up sent")), default="")
    last_answer = max((e["date"] for e in agency if e["type"] in _ANSWERS), default="")
    silence = any(m.get("kind") in ("statutory_date_passed", "promised_date_passed")
                  and timeline._day(m.get("date")) and timeline._day(m["date"]) < today
                  for m in request.get("overdue_marks", [])) and not any(e["date"] > sent for e in agency)
    extension = any(e["type"] in ("extension notice", "promised date") and e["date"] > last_answer
                    and any(timeline._day(d) and timeline._day(d) >= today for d in e.get("new_dates", []))
                    for e in events)
    clock = request.get("clocks") or {}
    if request.get("closed"):
        state = "closed"
    elif (agency and agency[-1]["type"] == "clarification requested") or request.get("status") in (
            "clarification requested", "needs clarification", "clarify"):
        state = "clarification"
    elif request.get("issues"):
        state = "unknown"
    elif silence:
        state = "silence"
    elif extension:
        state = "extension"
    elif needs["withheld"]:
        state = "withheld"
    elif needs["produced"] and needs["not_addressed"]:
        state = "partial"  # Explicit mixed-answer acceptance, including other no-records items.
    elif needs["audit_no_records"] or needs["other_no_records"] or no_records:
        state = "no_records"
    elif rated and not open_items:
        state = "answered"
    elif not agency and clock.get("state") not in (None, "unknown", "satisfied") and timeline._day(clock.get("due")):
        state = "waiting"
    else:
        state = "unknown"
    evidence = []
    for event in events:
        source = event.get("source") or {}
        ids = {k: source[k] for k in ("event_id", "item_id", "key", "draft_id", "request") if k in source}
        entry = {"type": event["type"], "date": event["date"], "ids": ids}
        if entry not in evidence:
            evidence.append(entry)
    return {"state": state, "flags": flags, "open_items": sorted(open_items),
            "item_needs": needs, "evidence": {"events": evidence, "item_numbers": sorted(seen)}}


def _read(path):
    return response_items._read_json(path)


def _index(rows):
    result = {}
    for row in rows:
        key = (row.get("agency_key"), row.get("request_id"))
        if not all(isinstance(k, str) and k for k in key) or key in result:
            raise ValueError("followup_state_duplicate_or_invalid_request")
        result[key] = row
    return result


def classify_run(project, run_id):
    """Read only the selected run's timeline, ratings and original register scope."""
    if not isinstance(run_id, str) or timeline._RUN_ID.fullmatch(run_id) is None:
        raise ValueError("followup_state_run_id")
    root = received_items._safe_dir(project) / "run-outputs" / run_id
    outcomes = received_items._safe_dir(root / "outcomes")
    report = _read(outcomes / "timeline.json")
    if report.get("schema") != timeline.SCHEMA or report.get("run_id") != run_id:
        raise ValueError("followup_state_timeline")
    if not isinstance(report.get("requests"), list) or timeline._day(report.get("as_of")) is None:
        raise ValueError("followup_state_timeline")
    try:
        ratings = _read(outcomes / "response-items.json")
    except FileNotFoundError:
        ratings = {"schema": response_items.SCHEMA, "run_id": run_id, "requests": []}
    if ratings.get("schema") != response_items.SCHEMA or ratings.get("run_id") != run_id:
        raise ValueError("followup_state_ratings")
    by_items = _index(ratings["requests"])
    # Timeline omits asked/status on main; the register is their authoritative same-run source.
    try:
        register = _read(root / "CURRENT-REQUEST-REGISTER.json")
    except FileNotFoundError:
        register = {"requests": []}
    by_register = _index(register["requests"])
    _index(report["requests"])
    rows = []
    for req in report["requests"]:
        key = (req["agency_key"], req["request_id"])
        original = by_register.get(key, {})
        merged = dict(req, asked=original.get("asked"), status=original.get("status"))
        row = by_items.get(key)
        result = classify(merged, row["asks"] if row is not None else None, report["as_of"])
        rows.append({"agency_key": key[0], "request_id": key[1], **result})
    return {"schema": SCHEMA, "run_id": run_id, "as_of": report["as_of"], "review_required": True,
            "visibility": "private", "requests": rows,
            "limits": ["Machine leads only; review required. On our records; not a legal finding."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    try:
        result = classify_run(args.project, args.run_id)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        print("followup_state_invalid_input", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0
