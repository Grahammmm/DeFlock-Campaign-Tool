"""Read-only, private follow-up cases from routed residual scope. Never sends."""
import copy
import csv
import io
import re

from . import followup_drafter as drafter, followup_route, followup_state
from .intake.folder import secure_open
from .ledger.store import checked_path
from .outcomes import timeline

ASK_COLUMNS = ["ask_id", "message", "item_no", "item_text", "period", "response", "state", "held_doc_ids"]
PRIOR_NOTE = "prior-asks ledger not staged; check repeated asks by hand (R1, R7)"


class CaseError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _need(condition, code):
    if not condition:
        raise CaseError(code)


def build(route_row, timeline_request, items, *, asks_rows=None, policy_rows=(), policy_ref=None):
    """Build a review-required case; only route.asks_for becomes draftable asks.

    policy_rows is accepted for the caller's matching drafter invocation; a section
    is never selected implicitly. Missing contact information stays missing.
    """
    pattern = route_row.get("pattern")
    extras = route_row.get("extra_patterns", [])
    _need(isinstance(extras, list), "route_pattern_unmapped")
    _need(all(isinstance(p, str) and drafter.RESPONSE_TYPES.get(p) == p
              for p in [pattern, *extras]), "route_pattern_unmapped")
    _need(len(set([pattern, *extras])) == len(extras) + 1, "route_pattern_duplicate")
    _need(route_row.get("handled_by", "engine") == "engine", "route_not_actionable")
    agency, request_id = timeline_request.get("agency_key"), timeline_request.get("request_id")
    _need(all(isinstance(v, str) and v.strip() for v in
              (agency, request_id, timeline_request.get("agency"))), "case_identity_missing")
    for row in (route_row, items):
        _need(all(row.get(k, v) == v for k, v in (("agency_key", agency), ("request_id", request_id))),
              "case_request_mismatch")
    numbers = route_row.get("asks_for")
    _need(isinstance(numbers, list) and numbers and all(type(n) is int and n > 0 for n in numbers)
          and len(set(numbers)) == len(numbers), "route_asks_invalid")
    by_number = {}
    for ask in items.get("asks", []):
        number = followup_state._number(ask)
        _need(number not in by_number, "case_duplicate_ask")
        by_number[number] = ask
    _need(set(numbers) <= by_number.keys(), "route_asks_missing")
    case = {"schema": drafter.SCHEMA, "agency": {"id": agency, "name": timeline_request["agency"]},
            "request_id": request_id, "response_type": pattern, "extra_patterns": list(extras),
            "asks": [dict({k: copy.deepcopy(v) for k, v in by_number[n].items()
                           if k in ("ask_id", "text", "rating", "produced")}, number=n) for n in numbers],
            "prior_asks": [], "slots": {}, "review_required": True}
    case["received_asks"] = [{"number": n, "text": a["text"]} for n, a in by_number.items()
                             if n not in numbers and a.get("rating") == "answered"]
    if asks_rows is None:
        case["slots"]["prior_asks_note"] = PRIOR_NOTE
    else:
        for row in asks_rows:
            if row.get("state") in ("produced", "pending"):
                _need(bool(row.get("item_text")), "prior_ask_text_missing")
                # Ledger ask ids need not share the response-items numbering scheme.
                case["prior_asks"].append({"text": row["item_text"], "status": row["state"]})
    events = [e for e in timeline_request.get("events", [])
              if not e.get("superseded") and timeline._day(e.get("date"))]
    sent = [e["date"] for e in events if e.get("type") == "sent"]
    replies = [e["date"] for e in events if e.get("type") in followup_state._AGENCY]
    if sent:
        case["request_date"] = min(sent)
    if replies:
        case["response_date"] = max(replies)
    if pattern == "closed_new_request":
        case["slots"]["old_request_id"] = request_id
        closed = [e["date"] for e in events if e.get("type") == "closed"]
        if closed:
            case["slots"]["closure_date"] = max(closed)
    if policy_ref is not None:
        case["policy"] = copy.deepcopy(policy_ref)
    drafter._validate(case)
    return case


def _asks(project, agency_key):
    _need(isinstance(agency_key, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", agency_key),
          "case_agency_invalid")
    path = checked_path(str(project / "agency-asks" / (agency_key + ".tsv")), existing=False)
    try:
        with secure_open(path) as stream:
            raw = stream.read(1024 * 1024 + 1)
    except FileNotFoundError:
        return None
    _need(len(raw) <= 1024 * 1024, "case_asks_too_large")
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")), delimiter="\t", strict=True)
    _need(reader.fieldnames == ASK_COLUMNS, "case_asks_columns")
    rows = list(reader)
    _need(all(None not in r and all(v is not None for v in r.values()) for r in rows), "case_asks_shape")
    return rows


def build_for_run(project, run_id, agency_key, request_id):
    """Read same-run outputs and optional operator-staged ledger, without writes."""
    routes = followup_route.route_run(project, run_id)  # validates run id and same-run schemas
    project = followup_state.received_items._safe_dir(project)
    root = project / "run-outputs" / run_id / "outcomes"
    key = (agency_key, request_id)
    by_route = followup_state._index(routes)
    requests = followup_state._index(followup_state._read(root / "timeline.json")["requests"])
    ratings = followup_state._read(root / "response-items.json")
    _need(ratings.get("schema") == followup_state.response_items.SCHEMA and ratings.get("run_id") == run_id,
          "case_ratings_stale")
    by_items = followup_state._index(ratings["requests"])
    _need(key in by_route and key in requests and key in by_items, "case_request_missing")
    return build(by_route[key], requests[key], by_items[key], asks_rows=_asks(project, agency_key))
