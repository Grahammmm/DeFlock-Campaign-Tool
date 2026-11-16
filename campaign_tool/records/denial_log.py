"""Private read-only refusal log; machine reading, never a legal finding."""
import argparse
from datetime import datetime
import hashlib
import json
import os
import re
import sqlite3
from zoneinfo import ZoneInfo

from . import approval_queue, followup_route, followup_state, mail_send
from .outcomes import received_items, response_items
from .campaign_config import DEFAULT_TIMEZONE, load_timezone

LABEL = "Machine reading of the response; review required; not proof of a lawful or unlawful denial."
CITATION = "agency's citation; UNVERIFIED by us"
_DENIAL_CUE = re.compile(response_items.WITHHELD.pattern + r"|\bexempt(?:ed|ion)?\b", re.I)
_HEADING = re.compile(r"^\s*(?:(?:request\s+)?item\s+\d{1,3}|\(\d{1,3}\)|\d{1,3}[.):])\s*[:.]?\s*$", re.I)


def _whole_quote(source, pages):
    """FLK-REC-4: one complete cited denial sentence of a whole denial from hash-bound current page text.

    -> (agency_words, note). Nothing qualifies without a positive page, a reviewed extraction, a section-numbered
    citation and no item reference by the agency; two distinct candidates stay manual, never first-match.
    """
    sha = source.get("original_sha256")
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
        return None, None
    if response_items._unreviewed(source, pages):
        return None, None
    found = {}
    for page, text in pages:
        if type(page) is not int or page < 1 or not isinstance(text, str):
            continue
        lines = [ln for ln in text.splitlines()]
        for sentence in response_items.sentences(text):
            if not (response_items._hit(_DENIAL_CUE, sentence) and followup_route.has_numbered_citation(sentence)):
                continue
            where = next((i for i, ln in enumerate(lines) if sentence in ln), None)
            before = next((ln for ln in reversed(lines[:where or 0]) if ln.strip()), "") if where is not None else ""
            if (followup_route.explicit_item_reference(sentence) or _HEADING.match(before)
                    or (where is not None and followup_route.explicit_item_reference(lines[where]))):
                return None, None  # item-level reply: keeps the existing denial routing
            if len(sentence) < 400:
                found[(page, sentence)] = None
    if len(found) > 1:
        return None, "multiple candidate quotes; manual"
    if not found:
        return None, None
    (page, sentence), = found
    return {"quote": sentence, "page": page, "doc_sha256": sha}, None


def _day(value, *, time_zone=DEFAULT_TIMEZONE):
    if not value:
        return None
    if len(value) == 10:
        return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError("denial_log_date_timezone")
    return moment.astimezone(ZoneInfo(time_zone)).date().isoformat()


def entries(states, response_items_doc, queue_rows, *, time_zone=DEFAULT_TIMEZONE, page_text=None):
    """Pure, one row per request/ask; optional sources carry same-run receipt metadata.

    `page_text` (optional): {item_id: [(page, text)]} or a callable item_id -> that list, current hash-bound
    extraction text used only to quote a whole denial; omitted means the previous behaviour exactly.
    """
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
        if not selected and denied:
            selected = [(None, {})]
        for number, ask in selected:
            evidence = sorted(ask.get("evidence", []), key=response_items.canon)
            quote = next((e for e in evidence if e.get("quote")), {})
            source_id = quote.get("item_id") or (denied[-1].get("ids", {}).get("item_id") if number is None else None)
            source = sources.get(source_id, {})
            dates = [e["date"] for e in events if e.get("ids", {}).get("item_id") == source_id and source_id]
            day = _day(source.get("received_at") or max(dates, default=None)
                       or (denied[-1]["date"] if number is None else None), time_zone=time_zone)
            identity_source = source_id or (denied[-1].get("ids", {}) if number is None else sorted(
                {e["item_id"] for e in evidence if e.get("item_id")}))
            row_id = hashlib.sha256(response_items.canon([*key, number, identity_source]).encode()).hexdigest()
            words = {"quote": quote["quote"][:400], **{k: quote[k] for k in ("page", "doc_sha256") if k in quote}} if quote else None
            if words is not None and source.get("original_sha256"):
                words["doc_sha256"] = source["original_sha256"]
            note = None
            if words is None and number is None and page_text is not None and source_id and source:
                pages = page_text(source_id) if callable(page_text) else page_text.get(source_id)
                if pages:
                    words, note = _whole_quote(source, pages)
            citations = [e["quote"] for e in evidence if e.get("quote") and response_items.CITE.search(e["quote"])]
            if words is not None and number is None:
                citations = [words["quote"]]
            matches = []
            for q in queue_rows:
                if q.get("agency_id") != key[0] or q.get("request_id") not in (key[1], req.get("ledger_request_id") or key[1]):
                    continue
                engine = q.get("kind") == "followup-playbook"
                engine_evidence = q.get("evidence") or {}
                if engine:
                    patterns = [engine_evidence.get("pattern"), *engine_evidence.get("extra_patterns", [])]
                    if q.get("source_run_id") != states["run_id"]:
                        continue
                    if "vague_answer" in patterns:  # exact stored denial id and locator, never the kind or asks alone
                        proofs = (engine_evidence.get("vague_answer") or {}).get("entries") or []
                        if words is not None and any(
                                p.get("denial_id") == row_id and p.get("page") == words.get("page")
                                and p.get("doc_sha256") == words.get("doc_sha256") for p in proofs if isinstance(p, dict)):
                            matches.append(q)
                        continue
                    if "blanket_denial" not in patterns:
                        continue
                elif followup_route.COVERERS.get(q.get("kind")) != "withheld":
                    continue
                if q.get("status") not in ("pending", "approved", "rejected", "sent"):
                    continue
                mapped = engine_evidence.get("asks_for", []) if engine else followup_route._mapped(q, asks, sorted(set(needs + numbers)))
                reply_source = (q.get("evidence") or {}).get("thread_item_id" if engine else "item_id")
                if (number is not None and mapped and number in mapped) or (number is None and source_id and
                        reply_source == source_id):
                    matches.append(q)
            reply, qid = "not_drafted", None
            if len(matches) == 1:
                qid = matches[0]["id"]
                reply = {"pending": "drafted"}.get(matches[0]["status"], matches[0]["status"])
            out.append({"id": row_id,
                        "agency_key": key[0], "request_id": key[1], "date": day,
                        "date_basis": (f"DATED (recorded receipt; {'Pacific' if time_zone == DEFAULT_TIMEZONE else time_zone} day)"
                                       if day else "UNKNOWN"),
                        "item_no": number, "item_text": (ask.get("text") or "")[:160],
                        "kind": "denied_whole" if number is None else "exemption_cited" if ask.get("rating") == "exemption_cited" else "withheld",
                        "agency_words": words, "agency_words_note": None if words else (note or "no quote extracted"),
                        "agency_citation": {"text": citations, "label": CITATION}, "reply": reply, "queue_id": qid})
    return out


def _extraction_loader(project, sources):
    """Lazy item_id -> [(page, text)] from the current hash-bound extraction; read-only, never extracts or creates."""
    by_id = {s.get("item_id"): s for s in sources if isinstance(s, dict)}

    def pages(item_id):
        sha = (by_id.get(item_id) or {}).get("original_sha256")
        processing = received_items._safe_dir(project) / "processing-v1"
        ledger = processing / "ledger.sqlite"
        if not isinstance(sha, str) or not os.path.lexists(ledger) or ledger.is_symlink():
            return []
        from .run_stages import LedgerBusy, snapshot_ro
        try:
            with snapshot_ro(ledger) as db:
                db.row_factory = sqlite3.Row
                units, _, _ = received_items._current_units(db, processing, sha)
        except (sqlite3.Error, LedgerBusy):
            raise ValueError("denial_log_extraction") from None
        return [(u["page"], u["text"]) for u in units]
    return pages


def load(project, run_id):
    """Read existing snapshots and review/send journals, never refresh or create them."""
    try:
        time_zone = load_timezone(project)
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
                "entries": entries(states, doc, queue, time_zone=time_zone,
                                   page_text=_extraction_loader(received_items._safe_dir(project), received["items"]))}
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
