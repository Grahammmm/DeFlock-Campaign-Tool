"""Private, read-only, text-free "machine reading" of one run's follow-up state and route (FLK-REC-8).

``read`` calls the existing classifier and router for one exact run and keeps only ask NUMBERS, the display state,
flags, route pattern and a reason from the router's fixed vocabulary. It never copies ask text, response text, quotes,
evidence, queue ids or caught error text. ``render`` is a pure, form-free section for the agency and review pages.
Machine leads only: review required, not a legal finding, not an approval.
"""
import os
import re
import stat

from . import followup_clock, followup_route, followup_state
from .outcomes import timeline
from .outcomes.reading import esc

LABEL = "machine reading, review required"
CODE = "followup_reading_unavailable"
NOT_IN_EXPORT = "not in this export"
GROUPS = ("produced", "withheld", "audit_no_records", "other_no_records", "not_addressed")
STATES = frozenset({"closed", "clarification", "unknown", "silence", "extension", "withheld", "partial", "no_records",
                    "answered", "waiting"})
FLAGS = frozenset({"open_tier1", "audit_items_no_records", "extension_limit_exceeded", "items_not_rated",
                   "register_issues"})
PATTERNS = frozenset({"blanket_denial", "audit_no_documents", "no_records", "partial_production", "closed_new_request",
                      "silence", "extension", "records_contradict_policy", "vague_answer"})
REASONS = frozenset({"coverage_table_invalid", "coverage_unreconciled", "contradiction_evidence_unavailable",
                     "contradiction_findings_ambiguous", "reviewed_contradiction", "vague_answer_evidence_ambiguous",
                     "vague_answer; cited denial not tied to items (machine reading, not a finding)"})
RULE = re.compile(r"rule (10|[1-9]); records_contradict_policy and vague_answer require a reviewed finding")
MAX_REQUESTS = 5000
MAX_ASKS = 10000
MAX_TEXT = 256
ENVELOPE = {"schema", "source_project_id"}
DOC_KEYS = {"state", "run_id", "as_of", "requests"}
ROW_KEYS = {"agency_key", "request_id", "ledger_request_id", "state_source", "display_state", "flags", "item_needs",
            "route_source", "pattern", "reason"}
_SOURCES = ("timeline.json", "response-items.json")


def unavailable(run_id=None):
    return {"state": "unavailable", "run_id": run_id if isinstance(run_id, str) else None, "code": CODE}


def _text(value, allow_none=False):
    if value is None:
        return allow_none
    return type(value) is str and 0 < len(value) <= MAX_TEXT


def _number(value):
    return type(value) is int and value > 0


def _needs_ok(needs):
    if type(needs) is not dict or set(needs) != set(GROUPS):
        return False
    seen = set()
    for key in GROUPS:
        values = needs[key]
        if type(values) is not list or not all(_number(n) for n in values) or values != sorted(set(values)):
            return False
        if seen.intersection(values):
            return False
        seen.update(values)
    return len(seen) <= MAX_ASKS


def _row_ok(row):
    if type(row) is not dict or set(row) != ROW_KEYS:
        return False
    if not (_text(row["agency_key"]) and _text(row["request_id"]) and _text(row["ledger_request_id"], True)):
        return False
    if row["state_source"] == "ok":
        flags = row["flags"]
        if (row["display_state"] not in STATES or type(flags) is not list or len(set(flags)) != len(flags)
                or not set(flags) <= FLAGS or not _needs_ok(row["item_needs"])):
            return False
    elif row["state_source"] == "unavailable":
        if (row["display_state"], row["flags"], row["item_needs"]) != (None, None, None):
            return False
    else:
        return False
    if row["route_source"] == "ok":
        pattern, reason = row["pattern"], row["reason"]
        if pattern is not None and (type(pattern) is not str or pattern not in PATTERNS):
            return False
        return type(reason) is str and (reason in REASONS or RULE.fullmatch(reason) is not None)
    return row["route_source"] == "unavailable" and (row["pattern"], row["reason"]) == (None, None)


def validate(doc, run_id=None):
    """The ok reading document when it has exactly the bounded text-free shape (and run), else None."""
    if type(doc) is not dict or not set(doc) <= DOC_KEYS | ENVELOPE or not DOC_KEYS <= set(doc):
        return None
    if doc["state"] != "ok" or not _text(doc["run_id"]) or (run_id is not None and doc["run_id"] != run_id):
        return None
    if not _text(doc["as_of"]) or type(doc["requests"]) is not list or len(doc["requests"]) > MAX_REQUESTS:
        return None
    keys = set()
    for row in doc["requests"]:
        if not _row_ok(row):
            return None
        key = (row["agency_key"], row["request_id"])
        if key in keys:
            return None
        keys.add(key)
    return doc


def _regular(path):
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("followup_reading_source")


def _state_rows(project, run_id):
    """(states, ratings keys, register) for the exact run, or raise: required sources must exist and be regular."""
    root = followup_state.received_items._safe_dir(project)
    outcomes = root / "run-outputs" / run_id / "outcomes"
    for name in _SOURCES:
        _regular(outcomes / name)
    _regular(root / "run-outputs" / run_id / followup_clock.REGISTER_NAME)
    states = followup_state.classify_run(root, run_id)
    ratings = followup_state._index(followup_state._read(outcomes / "response-items.json")["requests"])
    register = followup_clock.read_register(str(root), run_id)
    if register is None or register["as_of"] != states["as_of"]:
        raise ValueError("followup_reading_register")
    return states, ratings, followup_state._index(register["requests"])


def _routes(project, run_id):
    try:
        return {(r["agency_key"], r["request_id"]): r for r in followup_route.route_run(project, run_id)}
    except Exception:  # route-only failure: the partition stays visible, pattern and reason become unknown
        return None


def read(project, run_id):
    """Project the selected run's machine reading. Never raises; global source failure is a content-free unavailable."""
    try:
        if not isinstance(run_id, str) or timeline._RUN_ID.fullmatch(run_id) is None:
            return unavailable()
        states, ratings, register = _state_rows(project, run_id)
        routes = _routes(project, run_id)
        rows = []
        for state in states["requests"]:
            key = (state["agency_key"], state["request_id"])
            ledger = (register.get(key) or {}).get("ledger_request_id")
            row = {"agency_key": key[0], "request_id": key[1], "ledger_request_id": ledger if isinstance(ledger, str) and ledger else None,
                   "state_source": "ok", "display_state": state["state"], "flags": list(state["flags"]),
                   "item_needs": {g: list(state["item_needs"][g]) for g in GROUPS},
                   "route_source": "unavailable", "pattern": None, "reason": None}
            route = (routes or {}).get(key)
            if key not in ratings:  # not rated: never guessed from an empty or asked scope
                row.update(state_source="unavailable", display_state=None, flags=None, item_needs=None)
            elif route is not None:
                row.update(route_source="ok", pattern=route.get("pattern"), reason=route.get("reason"))
            rows.append(row)
        doc = {"state": "ok", "run_id": run_id, "as_of": states["as_of"], "requests": rows}
        return doc if validate(doc, run_id) is not None else unavailable(run_id)
    except Exception:  # no caught text is ever copied
        return unavailable(run_id)


# ---- lookup and rendering ---------------------------------------------------------------------------------

def find(reading, agency_key, request_id, ledger_request_id=None):
    """The one row for this agency and request (exact id or explicit same-agency ledger alias), else None."""
    if type(reading) is not dict or reading.get("state") != "ok" or type(reading.get("requests")) is not list:
        return None
    ids = {request_id} | ({ledger_request_id} if ledger_request_id else set())
    found = [r for r in reading["requests"] if r["agency_key"] == agency_key
             and (r["request_id"] in ids or (r["ledger_request_id"] is not None and r["ledger_request_id"] in ids))]
    return found[0] if len(found) == 1 else None


def _list(values):
    return ", ".join(str(n) for n in values) if values else "none"


def render(reading, agency_key, request_id, *, ledger_request_id=None, draft_run_id=None):
    """One labelled, read-only section. No form, button or hidden input."""
    out = ['<section class="machine-reading" aria-label="%s"><h3>%s</h3>' % (esc(LABEL), esc(LABEL)),
           "<p>Private machine lead for the selected run; our records, not a legal finding. Nothing is approved or sent.</p>"]
    state = reading.get("state") if type(reading) is dict else None
    if state == "not_in_export":
        out.append("<p>%s</p>" % esc(NOT_IN_EXPORT))
        return "".join(out + ["</section>"])
    row = find(reading, agency_key, request_id, ledger_request_id) if state == "ok" else None
    if row is None:
        out.append("<p>Reading: unknown</p>")
        return "".join(out + ["</section>"])
    out.append("<p>Selected run %s, as of %s.%s</p>" % (esc(reading["run_id"]), esc(reading["as_of"]), (
        " This draft was created from run %s." % esc(draft_run_id)) if draft_run_id and draft_run_id != reading["run_id"] else ""))
    ok = row["state_source"] == "ok"
    out.append("<dl><dt>State</dt><dd>%s</dd><dt>Flags</dt><dd>%s</dd>" % (
        esc(row["display_state"] if ok else "unknown"), esc(_list(row["flags"]) if ok else "unknown")))
    for group in GROUPS:
        out.append("<dt>Asks %s</dt><dd>%s</dd>" % (esc(group.replace("_", " ")), esc(_list(row["item_needs"][group]) if ok else "unknown")))
    routed = row["route_source"] == "ok"
    out.append("<dt>Route pattern</dt><dd>%s</dd><dt>Route reason</dt><dd>%s</dd></dl>" % (
        esc((row["pattern"] or "none") if routed else "unknown"), esc(row["reason"] if routed else "unknown")))
    return "".join(out + ["</section>"])
