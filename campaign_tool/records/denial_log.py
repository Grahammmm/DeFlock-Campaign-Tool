"""Private read-only refusal log; machine reading, never a legal finding."""
import argparse
from datetime import datetime
import hashlib
import json
from zoneinfo import ZoneInfo

from . import approval_queue, followup_route, followup_state, mail_send
from .outcomes import received_items, response_items

LABEL = "Machine reading of the response; review required; not proof of a lawful or unlawful denial."
CITATION = "agency's citation; UNVERIFIED by us"


def _day(value):
    if not value:
        return None
    if len(value) == 10:
        return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError("denial_log_date_timezone")
    return moment.astimezone(ZoneInfo("America/Los_Angeles")).date().isoformat()


def entries(states, response_items_doc, queue_rows):
    """Pure, one row per request/ask; optional sources carry same-run receipt metadata."""
    if states["run_id"] != response_items_doc["run_id"]:
        raise ValueError("denial_log_stale_response_items")
    items = followup_state._index(response_items_doc["requests"])
    sources = {s["item_id"]: s for s in response_items_doc.get("sources", [])}
    if len(sources) != len(response_items_doc.get("sources", [])):
        raise ValueError("denial_log_duplicate_source")
    followup_state._index(states["requests"])
    if len({q["id"] for q in queue_rows}) != len(queue_rows):
        raise ValueError("denial_log_duplicate_queue")
    out = []
    for state in sorted(states["requests"], key=lambda s: (s["agency_key"], s["request_id"])):
        key = (state["agency_key"], state["request_id"])
        req = items.get(key, {})
        asks = req.get("asks", [])
        numbers = [followup_state._number(a) for a in asks]
        if len(numbers) != len(set(numbers)):
            raise ValueError("denial_log_duplicate_ask")
        needs = state.get("item_needs", {}).get("withheld", [])
        events = state.get("evidence", {}).get("events", [])
        denied = [e for e in events if e["type"] == "denial"]
        selected = [(n, a) for n, a in zip(numbers, asks)
                    if n in needs or a.get("rating") in ("withheld", "exemption_cited")]
        if not asks and denied:
            selected = [(None, {})]
        for number, ask in selected:
            evidence = sorted(ask.get("evidence", []), key=response_items.canon)
            quote = next((e for e in evidence if e.get("quote")), {})
            source_id = quote.get("item_id") or (denied[-1].get("ids", {}).get("item_id") if number is None else None)
            source = sources.get(source_id, {})
            dates = [e["date"] for e in events if e.get("ids", {}).get("item_id") == source_id and source_id]
            day = _day(source.get("received_at") or max(dates, default=None)
                       or (denied[-1]["date"] if number is None else None))
            matches = []
            for q in queue_rows:
                if q.get("agency_id") != key[0] or q.get("request_id") not in (key[1], req.get("ledger_request_id") or key[1]):
                    continue
                if followup_route.COVERERS.get(q.get("kind")) != "withheld":
                    continue
                if q.get("status") not in ("pending", "approved", "rejected", "sent"):
                    continue
                mapped = followup_route._mapped(q, asks, sorted(set(needs + numbers)))
                if (number is not None and mapped and number in mapped) or (number is None and source_id and
                        (q.get("evidence") or {}).get("item_id") == source_id):
                    matches.append(q)
            reply, qid = "not_drafted", None
            if len(matches) == 1:
                qid = matches[0]["id"]
                reply = {"pending": "drafted"}.get(matches[0]["status"], matches[0]["status"])
            identity_source = source_id or (denied[-1].get("ids", {}) if number is None else sorted(
                {e["item_id"] for e in evidence if e.get("item_id")}))
            words = {"quote": quote["quote"][:400], **{k: quote[k] for k in ("page", "doc_sha256") if k in quote}} if quote else None
            if words is not None and source.get("original_sha256"):
                words["doc_sha256"] = source["original_sha256"]
            citations = [e["quote"] for e in evidence if e.get("quote") and response_items.CITE.search(e["quote"])]
            out.append({"id": hashlib.sha256(response_items.canon([*key, number, identity_source]).encode()).hexdigest(),
                        "agency_key": key[0], "request_id": key[1], "date": day,
                        "date_basis": "DATED (recorded receipt; Pacific day)" if day else "UNKNOWN",
                        "item_no": number, "item_text": (ask.get("text") or "")[:160],
                        "kind": "denied_whole" if number is None else "exemption_cited" if ask.get("rating") == "exemption_cited" else "withheld",
                        "agency_words": words, "agency_words_note": None if words else "no quote extracted",
                        "agency_citation": {"text": citations, "label": CITATION}, "reply": reply, "queue_id": qid})
    return out


def load(project, run_id):
    """Read existing snapshots and review/send journals, never refresh or create them."""
    try:
        states = followup_state.classify_run(project, run_id)
        root = received_items._safe_dir(project) / "run-outputs" / run_id / "outcomes"
        doc = followup_state._read(root / "response-items.json")
        if doc.get("schema") != response_items.SCHEMA:
            raise ValueError("denial_log_response_items")
        try:
            received = followup_state._read(root / "received-items.json")
        except FileNotFoundError:
            received = {"schema": received_items.SCHEMA, "run_id": run_id, "items": []}
        if received.get("schema") != received_items.SCHEMA or received.get("run_id") != run_id:
            raise ValueError("denial_log_received_items")
        doc = dict(doc, sources=received["items"])
        queue = approval_queue.load_queue(project)
        sent = mail_send.results(project, strict=True) if queue else []
        queue = [dict(q, status="sent") if any(s.get("queue_id") == q["id"] and
                 s.get("revision") == q["revision"] and s.get("state") == "sent" for s in sent) else q for q in queue]
        return {"state": "available", "schema": "records-denial-log-v1", "run_id": run_id,
                "visibility": "private", "review_required": True, "label": LABEL,
                "entries": entries(states, doc, queue)}
    except FileNotFoundError:
        return {"state": "unavailable", "code": "denial_log_source_missing"}
    except (OSError, ValueError, KeyError, TypeError, AttributeError, approval_queue.ApprovalQueueError, mail_send.MailSendError):
        return {"state": "unavailable", "code": "denial_log_invalid_source"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = load(args.project, args.run_id)
    if args.json or result["state"] == "unavailable":
        print(json.dumps(result, sort_keys=True))
    else:
        print(LABEL)
        for row in result["entries"]:
            print(json.dumps(row, sort_keys=True))
    return 0 if result["state"] == "available" else 2
