"""Draft engine-routed follow-ups into the signed approval queue; never sends."""
import argparse
from collections import Counter
import hashlib
import json
import re
import sys

from . import approval_queue as aq, case_builder, followup_clock, followup_drafter
from . import followup_route, followup_state as fs, mail_drafts, playbook
from .outcomes import received_items

KIND = "followup-playbook"


def _thread(project, route, items):
    vehicle = route["vehicle"]
    if vehicle == "new_request":
        return None, "new request: no thread; recipient filled by G", None
    if vehicle != "reply_in_thread":
        raise ValueError("vehicle_unknown")
    candidates = [r for r in items if (r.get("agency_id"), r.get("request_id")) ==
                  (route["agency_key"], route["request_id"]) and r.get("received_at") is not None]
    if not candidates:
        return None, None, "no_source_message"
    latest = max(r["received_at"] for r in candidates)
    sources = [r for r in candidates if r["received_at"] == latest]
    if len(sources) != 1:
        return None, None, "ambiguous_source_message"
    ident = sources[0].get("item_id")
    if not isinstance(ident, str) or not re.fullmatch(r"[0-9a-f]{64}", ident):
        return None, None, "thread_not_found"
    thread, reason = mail_drafts.find_thread(project, ident)
    if thread is None or reason:
        return None, None, reason or "thread_not_found"
    return ident, "replies to the latest agency message on this request received " + latest, None


def _identity(route, state, ratings, thread_id):
    # Queue lifecycle events are effects, not new agency evidence (avoid self-triggering).
    ids = {k + ":" + str(v) for e in state["evidence"]["events"]
           for k, v in e["ids"].items() if k != "draft_id"}
    for ask in ratings.get("asks", []):
        for e in ask.get("evidence", []):
            ids.update(str(e[k]) for k in ("item_id", "finding_id", "event_id") if e.get(k))
    identity = [route[k] for k in ("agency_key", "request_id", "pattern", "extra_patterns", "asks_for")]
    identity += [thread_id, sorted(ids), state["state"], sorted(state["flags"])]
    raw = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "route:" + hashlib.sha256(raw.encode()).hexdigest()[:32]


def run(project, run_id, *, apply=False, as_of=None):
    routes = followup_route.route_run(project, run_id)
    states = fs.classify_run(project, run_id)
    if as_of is not None and as_of != states["as_of"]:
        raise ValueError("followup_engine_as_of_stale")
    project = received_items._safe_dir(project)
    root = project / "run-outputs" / run_id / "outcomes"
    requests = fs._index(fs._read(root / "timeline.json")["requests"])
    ratings = fs._read(root / "response-items.json")
    if ratings.get("schema") != fs.response_items.SCHEMA or ratings.get("run_id") != run_id:
        raise ValueError("followup_engine_ratings_stale")
    by_items, by_state = fs._index(ratings["requests"]), fs._index(states["requests"])
    try:
        received = fs._read(root / "received-items.json")
    except FileNotFoundError:
        received = {"schema": received_items.SCHEMA, "run_id": run_id, "items": []}
    if received.get("schema") != received_items.SCHEMA or received.get("run_id") != run_id:
        raise ValueError("followup_engine_received_stale")
    settings = followup_clock.load_settings(project)
    existing = {r["id"] for r in aq.load_queue(project)}
    summary = dict(queued=0, would_queue=0, autonomy_off=0, duplicate=0, no_open_asks=0,
                   provenance_unresolved=0, by_pattern=Counter(), by_handled_by=Counter(),
                   manual_reasons=Counter(), manual=[])
    planned = []  # Validate all inputs and render before the first queue write.
    book = playbook.load()
    for route in routes:
        summary["by_pattern"][route["pattern"] or "none"] += 1
        summary["by_handled_by"][route["handled_by"]] += 1
        key = (route["agency_key"], route["request_id"])
        if route["handled_by"] == "manual":
            summary["manual_reasons"][route["reason"]] += 1
            summary["manual"].append(dict(agency_id=key[0], request_id=key[1],
                                          reason=route["reason"], unreconciled=route["unreconciled"]))
        if route["handled_by"] != "engine":
            continue
        if followup_clock._setting(settings, key[0])["autonomy"] != "draft":
            summary["autonomy_off"] += 1
            continue
        thread_id, basis, reason = _thread(project, route, received["items"])
        if reason:
            summary["provenance_unresolved"] += 1
            summary["manual_reasons"][reason] += 1
            summary["manual"].append(dict(agency_id=key[0], request_id=key[1], reason=reason, unreconciled=[]))
            continue
        state, items = by_state[key], by_items[key]
        ident = _identity(route, state, items, thread_id)
        qid = aq.draft_id(ident, KIND)
        if qid in existing:
            summary["duplicate"] += 1
            continue
        if not route["asks_for"]:
            summary["no_open_asks"] += 1
            continue
        case = case_builder.build(route, requests[key], items, asks_rows=case_builder._asks(project, key[0]))
        draft = followup_drafter.draft(book, case, ())  # No policy section is selected implicitly.
        if not draft["open_asks"]:
            summary["no_open_asks"] += 1
            continue
        evidence = {k: route[k] for k in ("pattern", "extra_patterns", "asks_for", "covered", "vehicle", "date_basis")}
        evidence.update(source="followup_engine", state=state["state"], flags=state["flags"],
                        thread_item_id=thread_id, thread_basis=basis, planned_send_date=route["planned_date"],
                        draft_status=draft["status"], lint_hits=len(draft["banned_phrases"]["hits"]),
                        unfilled=len(draft["unfilled"]))
        planned.append(dict(item_id=ident, kind=KIND, agency_id=key[0], request_id=key[1],
                            body=draft["body"], evidence=evidence, source_run_id=run_id))
        existing.add(qid)
    for draft in planned:
        if apply:
            aq.add_draft(project, **draft)
            summary["queued"] += 1
        else:
            summary["would_queue"] += 1
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        summary = run(args.project, args.run_id, apply=args.apply)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, aq.ApprovalQueueError, playbook.PlaybookError):
        print("followup_engine_invalid_input", file=sys.stderr)
        return 2
    print(json.dumps(summary, sort_keys=True))
    return 0
