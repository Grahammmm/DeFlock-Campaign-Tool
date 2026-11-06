"""Private per-run request timeline (REC-TL-1).

Merges, for every request in the same run's register, the register clocks, the run's received
items, the approval journal, the outbox receipts and operator-entered manual events into dated
events, a stage, a next action with an owner, labelled overdue marks and an extension-chain flag.

Read only: it sends, posts, approves and publishes nothing, writes no ledger or journal, and
reads no draft or message body (only ids, kinds, states and dates). Everything is data driven;
no agency, campaign or jurisdiction is named here. Overdue marks are arithmetic on our records,
never a legal finding. Output: run-outputs/<run_id>/outcomes/timeline.json (0700/0600), a derived
view recomputed per run and replaced atomically; nobody edits it by hand.

Manual events (append-only, read here, written by the console): ``<project>/manual-events.jsonl``,
one JSON object per line: ``{"event_id", "agency_id", "request_id", "type", "date", "summary",
"actor"}`` plus optional ``"new_date"`` (extension notices and promised dates) and ``"corrects"``
(an earlier event_id; both stay listed, the earlier one is marked superseded).
"""
from __future__ import annotations

from datetime import date
import json
import os
import re
import sqlite3
import stat

SCHEMA = "records-timeline-v1"
MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_OUTPUT_BYTES = 64 * 1024 * 1024
MAX_MANUAL = 20000
OVERDUE_LABEL = "past the statutory date on our records; not a legal finding"
EXTENSION_LABEL = "extension total exceeds 14 days on our records"
EVENT_TYPES = ("sent", "acknowledged", "extension notice", "promised date", "determination", "production",
               "denial", "no records", "fee estimate", "our follow-up sent", "call", "portal check", "closed",
               "received", "draft")
# Stage ladder, lowest to highest; the stage is the highest one the evidence reaches.
STAGES = ("not_started", "drafted", "sent", "acknowledged", "extended", "determined", "produced", "denied",
          "no_records", "analysed", "follow_up_drafted", "follow_up_sent")
EVENT_STAGE = {"sent": "sent", "acknowledged": "acknowledged", "extension notice": "extended",
               "determination": "determined", "production": "produced", "denial": "denied",
               "no records": "no_records", "our follow-up sent": "follow_up_sent"}
ITEM_EVENT = {"response": "acknowledged", "extension": "extension notice", "denial": "denial",
              "fee": "fee estimate", "records": "production", "other": "received"}
FOLLOW_UP_KINDS = frozenset({"followup-missing-records", "appeal-denial", "reply-extension", "fee-reply"})
LIMITS = (
    "Private; REVIEW REQUIRED. Arithmetic on our records, not a legal finding.",
    "Overdue marks use the register clock (basis recorded_receipt or submission_proxy); receipt may be "
    "unverified and portal-only replies may be unseen.",
    "Stage is the furthest step the recorded events reach; it does not assess whether a response was complete.",
    "Nothing here sends, posts or approves anything; sending stays manual.",
)
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


def _need(condition, code):
    if not condition:
        raise ValueError(code)


def _day(value):
    """Return a date from an ISO date or datetime string, else None."""
    if not isinstance(value, str) or not _DAY.match(value.strip()):
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def _iso(day):
    return day.isoformat() if day else None


def _canon(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)


# ---------------------------------------------------------------- pure merge

def _event(day, kind, summary, source, **extra):
    return {"date": _iso(day), "type": kind, "summary": summary, "source": source, **extra}


def _matches(row, agency, request):
    return agency == row.get("agency_key") and request in (row.get("ledger_request_id"), row.get("request_id"))


def _extension_chain(clock, events):
    """Extension notices with their stated new dates, against the 14-day limit on our records."""
    notices = [e for e in events if e["type"] == "extension notice" and not e.get("superseded")]
    dates = sorted({d for e in notices for d in e.get("new_dates", []) if d})
    limit = _day(clock.get("extended_due"))
    latest = _day(dates[-1]) if dates else None
    exceeds = bool(limit and latest and latest > limit)
    return {"count": len(notices), "new_dates": dates, "latest": _iso(latest), "limit_14_day": _iso(limit),
            "exceeds_14_days": exceeds, "label": EXTENSION_LABEL if exceeds else None}


def _marks(row, today, events, closed):
    clock = row.get("deadline") or {}
    marks = []
    if closed:
        return marks
    state = clock.get("state")
    if state == "past_due_reminder":
        marks.append({"kind": "statutory_date_passed", "date": clock.get("due"), "basis": clock.get("basis"),
                      "label": OVERDUE_LABEL})
    elif state == "unknown":
        marks.append({"kind": "unknown", "date": None, "basis": clock.get("basis"),
                      "label": "no usable clock on our records; overdue status unknown"})
    finished = any(e["type"] in ("production", "denial", "no records") and not e.get("superseded") for e in events)
    if not finished:
        promised = sorted({d for e in events if e["type"] in ("promised date", "extension notice")
                           and not e.get("superseded") for d in e.get("new_dates", [])})
        passed = [d for d in promised if _day(d) and _day(d) < today]
        if passed and not [d for d in promised if _day(d) and _day(d) >= today]:
            marks.append({"kind": "promised_date_passed", "date": passed[-1], "basis": "agency_promised",
                          "label": OVERDUE_LABEL})
    return marks


def _next_action(row, stage, drafts, closed, marks):
    pending = sorted(d for d in drafts if d["status"] == "pending")
    approved = sorted(d for d in drafts if d["status"] == "approved")
    if pending:
        return {"owner": "G", "action": f"approve, edit or reject the pending {pending[0]['draft_kind']} draft"}
    if approved:
        return {"owner": "G", "action": f"send the approved {approved[0]['draft_kind']} draft yourself "
                                        "(sending stays manual)"}
    if closed:
        return {"owner": "none", "action": "nothing: recorded closed"}
    action = row.get("next_action")
    if action == "clarify":
        return {"owner": "coordinator", "action": "resolve missing or conflicting scope, identity or dates"}
    if action == "follow up":
        return {"owner": "coordinator", "action": "draft a cited follow-up for review"}
    if action == "nothing":
        return {"owner": "none", "action": "nothing: recorded complete"}
    if any(m["kind"] == "unknown" for m in marks):
        return {"owner": "coordinator", "action": "establish the clock for this request"}
    return {"owner": "agency", "action": "wait for the agency; the determination period is running"}


def build_timeline(register, received, drafts, outbox, manual, *, run_id):
    """Pure merge. Inputs are already-parsed documents; unmatched events are counted, never dropped."""
    today = _day(register.get("as_of"))
    _need(today is not None, "timeline_register_as_of")
    requests, matched = [], set()
    items = list((received or {}).get("items", []))
    superseded = {m.get("corrects") for m in manual if m.get("corrects")}
    for row in register.get("requests", []):
        agency, request = row.get("agency_key"), row.get("ledger_request_id")
        events, draft_rows = [], []
        clock = row.get("deadline") or {}
        if clock.get("determination_at"):
            events.append(_event(_day(clock["determination_at"]), "determination",
                                 "determination recorded in the register", {"kind": "register", "request": row.get("request_id")}))
        if row.get("production_at"):
            events.append(_event(_day(row["production_at"]), "production", "production recorded in the register",
                                 {"kind": "register", "request": row.get("request_id")}))
        if clock.get("extension_recorded"):
            events.append(_event(_day(clock.get("extended_due")), "extension notice",
                                 "extension recorded in the register (no notice date held)",
                                 {"kind": "register", "request": row.get("request_id")},
                                 new_dates=[], undated=True))
        for it in items:
            if not _matches(row, it.get("agency_id"), it.get("request_id")):
                continue
            matched.add(("item", it.get("item_id")))
            kind = ITEM_EVENT.get(it.get("kind"), "received")
            stated = sorted({d["date"] for d in it.get("deadlines", []) if isinstance(d, dict) and d.get("date")})
            extra = {"new_dates": stated} if kind == "extension notice" else {}
            events.append(_event(_day(it.get("received_at")), kind, f"received {it.get('kind')} item",
                                 {"kind": "received_item", "item_id": it.get("item_id")},
                                 analysed=it.get("comparison_status") not in (None, "unresolved"), **extra))
            if kind != "extension notice":
                for d in stated:
                    events.append(_event(_day(d), "promised date", "date stated by the agency in a received item",
                                         {"kind": "received_item", "item_id": it.get("item_id")}, new_dates=[d]))
        for d in drafts:
            if not _matches(row, d.get("agency_id"), d.get("request_id")):
                continue
            matched.add(("draft", d.get("id")))
            draft_rows.append({"draft_id": d.get("id"), "draft_kind": d.get("kind"), "status": d.get("status")})
            events.append(_event(_day(d.get("created_at")), "draft", f"{d.get('kind')} draft {d.get('status')}",
                                 {"kind": "approval_journal", "draft_id": d.get("id")},
                                 draft_kind=d.get("kind"), draft_status=d.get("status")))
        for o in outbox:
            if not _matches(row, o.get("agency_id"), o.get("request_id")) or o.get("state") != "sent":
                continue
            matched.add(("outbox", o.get("idempotency_key")))
            followup = o.get("kind") == "send_followup"
            events.append(_event(_day(o.get("sent_at")), "our follow-up sent" if followup else "sent",
                                 "follow-up sent" if followup else "request sent",
                                 {"kind": "outbox_receipt", "key": o.get("idempotency_key"),
                                  "message_id": o.get("message_id")}))
        for m in manual:
            if not _matches(row, m.get("agency_id"), m.get("request_id")):
                continue
            matched.add(("manual", m.get("event_id")))
            new = [m["new_date"]] if _day(m.get("new_date")) else []
            events.append(_event(_day(m.get("date")), m["type"], m.get("summary") or m["type"],
                                 {"kind": "manual", "event_id": m.get("event_id"), "actor": m.get("actor")},
                                 new_dates=new, superseded=m.get("event_id") in superseded,
                                 corrects=m.get("corrects")))
        events = [e for e in events if e["date"] or e["type"] == "extension notice"]
        events.sort(key=lambda e: (e["date"] or "9999", e["type"], json.dumps(e["source"], sort_keys=True)))
        live = [e for e in events if not e.get("superseded")]
        closed = any(e["type"] == "closed" for e in live) or str(row.get("status", "")).casefold() in (
            "complete", "completed", "closed", "withdrawn", "fulfilled")
        reached = {"not_started"}
        for e in live:
            if e["type"] in EVENT_STAGE:
                reached.add(EVENT_STAGE[e["type"]])
            if e["type"] == "draft" and e.get("draft_status") != "rejected":
                reached.add("follow_up_drafted" if e.get("draft_kind") in FOLLOW_UP_KINDS else "drafted")
            if e.get("analysed"):
                reached.add("analysed")
        stage = max(reached, key=STAGES.index)
        chain = _extension_chain(clock, live)
        marks = _marks(row, today, live, closed)
        requests.append({
            "agency_key": agency, "agency": row.get("agency"), "request_id": row.get("request_id"),
            "ledger_request_id": request, "stage": stage, "closed": closed,
            "clocks": {k: clock.get(k) for k in ("basis", "state", "base_due", "extended_due", "due",
                                                  "recorded_due", "determination_at")},
            "extension_chain": chain,
            "overdue_marks": marks,
            "next_action": _next_action(row, stage, draft_rows, closed, marks),
            "issues": list(row.get("issues", [])),
            "events": [{k: v for k, v in e.items() if k not in ("analysed", "undated")} for e in events]})
    unmatched = {kind: sum(1 for t, key in (
        [("item", i.get("item_id")) for i in items] + [("draft", d.get("id")) for d in drafts]
        + [("outbox", o.get("idempotency_key")) for o in outbox if o.get("state") == "sent"]
        + [("manual", m.get("event_id")) for m in manual]) if t == kind and (t, key) not in matched)
                 for kind in ("item", "draft", "outbox", "manual")}
    return {"schema": SCHEMA, "run_id": run_id, "as_of": register["as_of"], "visibility": "private",
            "review_required": True, "requests": requests, "request_count": len(requests),
            "unmatched": unmatched, "limits": list(LIMITS)}


# ---------------------------------------------------------------- IO

def _read_bytes(path):
    info = path.lstat()
    _need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= MAX_INPUT_BYTES,
          "timeline_input_file")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        raw = stream.read(MAX_INPUT_BYTES + 1)
    _need(len(raw) <= MAX_INPUT_BYTES, "timeline_input_file")
    return raw


def _loads(raw):
    def unique(pairs):
        out = {}
        for key, value in pairs:
            _need(key not in out, "timeline_duplicate_key")
            out[key] = value
        return out
    try:
        return json.loads(raw, object_pairs_hook=unique)
    except (json.JSONDecodeError, UnicodeError, RecursionError):
        raise ValueError("timeline_input_json") from None


def read_manual_events(path):
    """Valid manual events in file order. A malformed or unknown-type line refuses the whole file."""
    try:
        raw = _read_bytes(path)
    except FileNotFoundError:
        return []
    out, seen = [], set()
    for line in raw.decode("utf-8").splitlines():
        if not line.strip():
            continue
        event = _loads(line.encode("utf-8"))
        _need(type(event) is dict and event.get("type") in EVENT_TYPES and _day(event.get("date"))
              and isinstance(event.get("actor"), str) and event["actor"].strip()
              and isinstance(event.get("event_id"), str) and event["event_id"] not in seen
              and isinstance(event.get("agency_id"), str) and isinstance(event.get("request_id"), str),
              "timeline_manual_event")
        seen.add(event["event_id"])
        out.append({k: event[k] for k in ("event_id", "agency_id", "request_id", "type", "date", "actor")}
                    | {k: event[k] for k in ("summary", "new_date", "corrects") if isinstance(event.get(k), str)})
        _need(len(out) <= MAX_MANUAL, "timeline_manual_event")
    return out


def read_outbox_receipts(path):
    """Sent rows only, metadata only (no draft body): a missing journal means no receipts."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return []
    _need(stat.S_ISREG(info.st_mode) and not path.is_symlink(), "timeline_outbox_file")
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = db.execute("SELECT idempotency_key, kind, request_id, agency_id, sent_at, provider_receipt "
                          "FROM outbox WHERE state='sent' ORDER BY idempotency_key").fetchall()
    finally:
        db.close()
    out = []
    for key, kind, request, agency, sent_at, receipt in rows:
        try:
            message_id = json.loads(receipt or "{}").get("provider_message_id")
        except (ValueError, AttributeError):
            message_id = None
        out.append({"idempotency_key": key, "kind": kind, "request_id": request, "agency_id": agency,
                    "sent_at": sent_at, "state": "sent",
                    "message_id": message_id if isinstance(message_id, str) else None})
    return out


def write_run_timeline(project, run_id):
    """Outputs-stage hook, after the register and received items exist.

    Missing register: nothing written. A missing received-items file or approval journal counts as
    empty. The file is replaced atomically because it is a recomputed view of append-only inputs.
    """
    from .. import approval_queue
    from . import received_items as _ri
    try:
        _need(_RUN_ID.fullmatch(run_id) is not None, "timeline_run_id")
        project = _ri._safe_dir(project)
        run_dir = _ri._safe_dir(project / "run-outputs" / run_id)
        outcomes = _ri._safe_dir(run_dir / "outcomes")
        try:
            register = _loads(_read_bytes(run_dir / "CURRENT-REQUEST-REGISTER.json"))
        except FileNotFoundError:
            return {"requests": [], "skipped": "inputs_missing"}
        try:
            received = _loads(_read_bytes(outcomes / "received-items.json"))
        except FileNotFoundError:
            received = {"items": []}
        _need(type(register) is dict and type(register.get("requests")) is list and type(received) is dict
              and type(received.get("items")) is list, "timeline_input_shape")
        drafts = approval_queue.load_queue(project)
        outbox = read_outbox_receipts(project / "outbox" / "outbox.sqlite")
        manual = read_manual_events(project / "manual-events.jsonl")
        report = build_timeline(register, received, drafts, outbox, manual, run_id=run_id)
        encoded = (_canon(report) + "\n").encode("utf-8")
        _need(len(encoded) <= MAX_OUTPUT_BYTES, "timeline_output_bound")
        outcomes.mkdir(mode=0o700, exist_ok=True)
        os.chmod(outcomes, 0o700)
        target = outcomes / "timeline.json"
        if os.path.lexists(target):
            _need(stat.S_ISREG(target.lstat().st_mode) and not target.is_symlink(), "timeline_target_invalid")
        temporary = outcomes / ".timeline.json.tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        return report
    except (ValueError, sqlite3.Error, approval_queue.ApprovalQueueError, UnicodeError) as error:
        raise OSError("TIMELINE_UNAVAILABLE") from error
