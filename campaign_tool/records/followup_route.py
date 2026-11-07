"""Private read-only follow-up routing leads; never drafts or sends a letter."""
import argparse
import csv
import io
import json
import re
import sys

from . import approval_queue, followup_clock, followup_state
from .intake.folder import secure_open
from .ledger.store import checked_path
from .outcomes import timeline

CANDIDATES = (("blanket_denial", "withheld"), ("audit_no_documents", "audit_no_records"),
              ("no_records", "other_no_records"), ("partial_production", "not_addressed"))
COVERERS = {"appeal-denial": "withheld", "followup-missing-records": "not_addressed"}
READY = "ready now; no statutory wait"
CLOCK = "on our records; not a legal finding"
EXTENSION = "the extension exceeds the 14-day limit on our records; not a legal finding"


def _rule(state, flags, kinds, opened):
    conditions = (state == "closed" and "open_tier1" in flags, state == "closed",
                  state == "clarification", "fee-reply" in kinds, state == "silence",
                  "extension_limit_exceeded" in flags, state == "extension",
                  state in ("withheld", "no_records", "partial") and bool(opened),
                  state in ("answered", "waiting") or not opened, state == "unknown")
    return next((n for n, match in enumerate(conditions, 1) if match), 10)


def _mapped(q, asks, need):
    evidence = q.get("evidence") or {}
    ident = evidence.get("item_id")
    if not ident:
        return None
    candidates = [a for a in asks if followup_state._number(a) in need]
    for ask in candidates:
        sources = {e.get("item_id") for e in ask.get("evidence", [])}
        if ident in sources and sources != {ident}:
            return None
    if q["kind"] == "appeal-denial":
        mapped = []
        for ask in candidates:
            entries = ask.get("evidence") or []
            sources = {e.get("item_id") for e in entries}
            if ident in sources:
                if sources != {ident} or ask.get("rating") not in ("withheld", "exemption_cited"):
                    return None
                mapped.append(followup_state._number(ask))
        return mapped or None
    gaps = [f for f in evidence.get("findings", []) if f.get("rating") == "gap"]
    mapped = set()
    for gap in gaps:
        if gap.get("page") is None or not gap.get("quote"):
            return None
        matches = [a for a in candidates if any(
            (e.get("item_id"), e.get("page"), e.get("quote")) ==
            (ident, gap["page"], gap["quote"]) for e in a.get("evidence", []))]
        if not matches or any(any(e.get("item_id") != ident for e in a.get("evidence", [])) for a in matches):
            return None
        mapped.update(followup_state._number(a) for a in matches)
    return sorted(mapped) if gaps else None


def _coverage(needs, queue, items, table):
    by_id = {q["id"]: q for q in queue}
    overrides = {}
    for line in table:
        qid = line.get("queue_id")
        q = by_id.get(qid)
        raw = line.get("ask_numbers", "")
        if (qid in overrides or q is None or q.get("kind") not in COVERERS
                or not str(line.get("confirmed_by") or "").strip()):
            return [], [], "coverage_table_invalid"
        if raw == "none":
            numbers = []
        elif isinstance(raw, str) and re.fullmatch(r"[1-9][0-9]*(?:,[1-9][0-9]*)*", raw):
            numbers = [int(n) for n in raw.split(",")]
        else:
            return [], [], "coverage_table_invalid"
        if len(numbers) != len(set(numbers)) or not set(numbers) <= set(needs.get(COVERERS[q["kind"]], [])):
            return [], [], "coverage_table_invalid"
        overrides[qid] = numbers
    covered, unknown, used = [], [], set()
    patterns = dict((key, pattern) for pattern, key in CANDIDATES)
    for qid, q in sorted(by_id.items()):
        key = COVERERS.get(q.get("kind"))
        if key is None:
            continue
        numbers = overrides[qid] if qid in overrides else _mapped(q, (items or {}).get("asks", []), needs.get(key, []))
        if numbers is None or used.intersection(numbers):
            unknown.append(qid)  # Overlapping coverers cannot satisfy the exactly-once invariant.
            continue
        used.update(numbers)
        covered.append({"pattern": patterns[key], "queue_id": qid, "items": sorted(numbers),
                        "basis": "operator_table" if qid in overrides else "item_evidence"})
    return covered, unknown, "coverage_unreconciled" if unknown else None


def route(state_row, clock_row, queue_rows, as_of, *, items=None, coverage_rows=()):
    """Pure first-match routing. Inputs and private evidence text are never modified."""
    if timeline._day(as_of) is None:
        raise ValueError("followup_route_as_of")
    needs = state_row.get("item_needs") or {}
    opened = sorted({n for _, key in CANDIDATES for n in needs.get(key, [])})
    queue = [q for q in queue_rows if q.get("status") in ("pending", "approved")]
    if len({q["id"] for q in queue}) != len(queue):
        raise ValueError("followup_route_duplicate_queue")
    kinds = {q.get("kind") for q in queue}
    number = _rule(state_row["state"], state_row.get("flags", []), kinds, opened)
    out = dict(pattern=None, extra_patterns=[], asks_for=[], covered=[], unreconciled=[],
               vehicle=None, handled_by="none", planned_date=None, date_basis=None,
               reason=f"rule {number}; records_contradict_policy and vague_answer require a reviewed finding")
    if number == 1:
        out.update(pattern="closed_new_request", vehicle="new_request", handled_by="engine", planned_date=as_of)
        if needs.get("audit_no_records"):
            out["extra_patterns"] = ["audit_no_documents"]
    elif number == 3:
        out.update(handled_by="manual", planned_date=as_of)
    elif number == 4:
        out.update(handled_by="received_items")
    elif number == 5:
        dates = [f["due_date"] for f in clock_row.get("followup_due", [])]
        out.update(pattern="silence", vehicle="reply_in_thread", handled_by="followup_clock",
                   planned_date=clock_row.get("notice_due") or min(dates, default=None), date_basis=CLOCK)
    elif number == 6:
        out.update(pattern="extension", vehicle="reply_in_thread", planned_date=as_of, date_basis=EXTENSION,
                   handled_by="received_items" if "reply-extension" in kinds else "engine")
    elif number == 7:
        out["planned_date"] = (clock_row.get("promised") or {}).get("date") or clock_row.get("extended_due")
    elif number == 8:
        covered, unknown, error = _coverage(needs, queue, items, coverage_rows)
        if error:
            out.update(handled_by="manual", reason=error, unreconciled=unknown)
            return out
        out["covered"] = covered
        done = {n for row in covered for n in row["items"]}
        residual = [p for p, key in CANDIDATES if set(needs.get(key, [])) - done]
        out.update(vehicle="reply_in_thread", handled_by="engine" if residual else "received_items", planned_date=as_of)
        if residual:
            out.update(pattern=residual[0], extra_patterns=residual[1:], asks_for=sorted(set(opened) - done))
    elif number == 10:
        out.update(handled_by="manual")
    if number == 1:
        out["asks_for"] = opened or sorted((state_row.get("evidence") or {}).get("item_numbers", []))
    elif number == 6:
        out["asks_for"] = opened  # every ask produced: nothing to re-ask, so []
    if out["handled_by"] == "engine" and out["date_basis"] is None:
        out["date_basis"] = READY
    return out


def _table(project, agency):
    if not isinstance(agency, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", agency):
        raise ValueError("followup_route_agency")
    path = checked_path(str(project / "followup-coverage" / (agency + ".tsv")), existing=False)
    try:
        with secure_open(path) as stream:
            raw = stream.read(1024 * 1024 + 1)
    except FileNotFoundError:
        return []
    if len(raw) > 1024 * 1024:
        raise ValueError("followup_route_table_size")
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8")), delimiter="\t")
    if reader.fieldnames != ["queue_id", "ask_numbers", "confirmed_by", "note"]:
        raise ValueError("followup_route_table_columns")
    rows = list(reader)
    if any(None in r or any(v is None for v in r.values()) for r in rows):
        raise ValueError("followup_route_table_shape")
    return rows


def route_run(project, run_id):
    states = followup_state.classify_run(project, run_id)
    project = followup_state.received_items._safe_dir(project)
    root = project / "run-outputs" / run_id
    register = followup_clock.read_register(project, run_id)
    if register is None or register["as_of"] != states["as_of"]:
        raise ValueError("followup_route_register")
    queue = approval_queue.load_queue(project)
    report = followup_state._read(root / "outcomes" / "timeline.json")
    clocks = followup_state._index(followup_clock.clock_table(
        register, followup_clock.load_settings(project), queue, report, as_of=states["as_of"]))
    try:
        ratings = followup_state._read(root / "outcomes" / "response-items.json")
    except FileNotFoundError:
        ratings = {"requests": []}
    by_items = followup_state._index(ratings["requests"])
    tables = {r["agency_key"]: _table(project, r["agency_key"]) for r in states["requests"]}
    rows = []
    for state in states["requests"]:
        key = (state["agency_key"], state["request_id"])
        matching = [q for q in queue if (q["agency_id"], q["request_id"]) == key]
        other_ids = {q["id"] for q in queue if q["agency_id"] == key[0] and q["request_id"] != key[1]}
        table = [r for r in tables[key[0]] if r["queue_id"] not in other_ids]
        rows.append({"agency_key": key[0], "request_id": key[1], "review_required": True,
                     **route(state, clocks.get(key, {}), matching, states["as_of"],
                             items=by_items.get(key), coverage_rows=table)})
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    try:
        rows = route_run(args.project, args.run_id)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, approval_queue.ApprovalQueueError):
        print("followup_route_invalid_input", file=sys.stderr)
        return 2
    for row in rows:
        print(json.dumps(row, sort_keys=True))
    return 0
