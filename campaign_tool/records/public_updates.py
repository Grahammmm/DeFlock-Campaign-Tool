"""Public status-line generator, journal and switches (REC-PUB-1). Nothing here publishes anything.

Renders the six fixed low-risk wordings (L1..L6) from timeline events, keeps an append-only private journal
``<project>/public-updates.jsonl`` and decides, with the switches, hold, caps and undo below, which lines count as
"posted". "Posted" is a journal state plus a derived file ``public-status-feed.json`` for a later reviewed site change;
there is no transport, no network and no credential in this module.

Controls (all checked again immediately before every post):
* switch file ``public-autopost.json`` ships ``{"enabled": false}``. It is ON only when the file says so AND the
  journal holds a G SSH-signed ON approval (``campaign_tool.approval``, the REC-AUTO-4 verifier) for the current round;
  a hand-edited file stays OFF. Any user can turn it OFF.
* kill file ``public-autopost.KILL``: nothing posts, lines still holding are retracted. Code never removes it.
* hold: a line waits ``HOLD_SECONDS`` (12 h) in ``holding``; a human can veto it (retract) meanwhile.
* caps: 1 line per event, 3 per agency per day, 10 per day. A breach is a refusal plus an alert record, never a queue.
* undo: retracting a posted line writes a retraction record; the feed drops it, the journal keeps history.
Agencies, campaign and jurisdiction are data; wording fields are validated and no free text is ever rendered.
"""
import argparse
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .. import approval

SCHEMA = "records-public-updates-v1"
FEED_SCHEMA = "records-public-status-feed-v1"
SWITCH_SCOPE = "public-autopost-switch-v1"
JOURNAL, SWITCH, KILL, LOCK, FEED = ("public-updates.jsonl", "public-autopost.json", "public-autopost.KILL",
                                     "public-updates.lock", "public-status-feed.json")
HOLD_SECONDS = 12 * 3600
CAP_PER_AGENCY_DAY, CAP_PER_DAY = 3, 10
MAX_JOURNAL = 16 * 1024 * 1024
MAX_INPUT = 64 * 1024 * 1024
TZ = ZoneInfo("America/Los_Angeles")
TEMPLATES = {
    "L1": "{date}: We sent a public records request to {agency}{ref}.",
    "L2": "{date}: {agency} confirmed it received our request{ref}.",
    "L3": "{date}: {agency} said it needs more time and expects to respond by {agency_date}.",
    "L4": "{date}: {agency} responded to our request. We are reviewing the response.",
    "L5": "{date}: {agency} released records. We review records before publishing any of them.",
    "L6": "{date}: We sent {agency} a follow-up on our request.",
}
# timeline event type -> (template, source kinds that count as a source for it)
TRIGGERS = {
    "sent": ("L1", {"outbox_receipt", "tracker"}),
    "acknowledged": ("L2", {"received_item", "manual"}),
    "extension notice": ("L3", {"received_item", "manual"}),
    "determination": ("L4", {"register", "received_item", "manual"}),
    "production": ("L5", {"register", "received_item", "manual"}),
    "our follow-up sent": ("L6", {"outbox_receipt"}),
}
# events that must never be auto-posted; they also veto an L4/L5 line from the same day
BLOCKING = frozenset({"denial", "no records", "fee estimate", "closed"})
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
_AGENCY = re.compile(r"[A-Za-z0-9][A-Za-z0-9 .,&'()/-]{0,99}")
_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ./_-]{0,63}")
_ACTOR = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._@-]{0,63}")
_LINE_ID = re.compile(r"[0-9a-f]{16}")


class PublicUpdatesError(Exception):
    """Content-free refusal; ``code`` is stable."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _need(condition, code):
    if not condition:
        raise PublicUpdatesError(code)


def _now(now=None):
    return now if now is not None else time.time()


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _local_day(ts):
    return datetime.fromtimestamp(ts, TZ).date().isoformat()


def _actor(actor):
    _need(isinstance(actor, str) and _ACTOR.fullmatch(actor) is not None, "actor_invalid")
    return actor


def project_id(project):
    return hashlib.sha256(os.path.realpath(project).encode()).hexdigest()[:16]


# ---- files -------------------------------------------------------------------------------------

def _dir(project, create=False):
    if create:
        os.makedirs(project, mode=0o700, exist_ok=True)
    _need(os.path.isdir(project) and not os.path.islink(project), "project_invalid")
    return os.path.abspath(project)


def _read(path, limit, missing=None):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return missing
    except OSError:
        raise PublicUpdatesError("file_invalid") from None
    with os.fdopen(fd, "rb") as stream:
        _need(stat.S_ISREG(os.fstat(stream.fileno()).st_mode), "file_invalid")
        data = stream.read(limit + 1)
    _need(len(data) <= limit, "file_too_large")
    return data


def _replace(path, data):
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)


def _json(data, code):
    try:
        return json.loads(data.decode("utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError):
        raise PublicUpdatesError(code) from None


class _Locked:
    def __init__(self, project, create=False):
        self.project = _dir(project, create)

    def __enter__(self):
        self.fd = os.open(os.path.join(self.project, LOCK), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        deadline = time.monotonic() + 5.0
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.monotonic() > deadline:
                    os.close(self.fd)
                    raise PublicUpdatesError("journal_busy") from None
                time.sleep(0.05)

    def __exit__(self, *exc):
        os.close(self.fd)

    def path(self, name):
        return os.path.join(self.project, name)

    def events(self):
        raw = _read(self.path(JOURNAL), MAX_JOURNAL, b"")
        _need(raw == b"" or raw.endswith(b"\n"), "journal_invalid")
        out = [_json(line, "journal_invalid") for line in raw.split(b"\n")[:-1]]
        _need(all(isinstance(e, dict) and e.get("schema") == SCHEMA for e in out), "journal_invalid")
        return out

    def append(self, kind, now, **fields):
        seq = len(self.events())
        line = (json.dumps({"schema": SCHEMA, "seq": seq, "at": _iso(now), "ts": int(now), "event": kind, **fields},
                           sort_keys=True, allow_nan=False) + "\n").encode()
        fd = os.open(self.path(JOURNAL), os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, line)
            os.fsync(fd)
        finally:
            os.close(fd)


def fold(events):
    """Current line rows (by id), last switch event and alerts, from the append-only journal."""
    lines, switch, alerts = {}, None, []
    for ev in events:
        kind, lid = ev["event"], ev.get("id")
        if kind == "line":
            lines.setdefault(lid, {"id": lid, **ev["line"], "state": "proposed", "proposed_at": ev["ts"]})
        elif kind == "switch":
            switch = ev
        elif kind == "alert":
            alerts.append(ev)
        elif lid in lines:
            row = lines[lid]
            if kind == "hold":
                row.update(state="holding", hold_until=ev["hold_until"])
            elif kind == "post":
                row.update(state="posted", posted_at=ev["ts"])
            elif kind == "retract":
                row.update(state="retracted", retracted_at=ev["ts"], retract_reason=ev["reason"])
            elif kind == "refused":
                row.update(state="refused", refusal=ev["code"])
    return lines, switch, alerts


# ---- generator ---------------------------------------------------------------------------------

def render(template, fields):
    """Fixed wording; every field is validated, so no free text can reach the line."""
    _need(template in TEMPLATES, "template_unknown")
    need = {"date", "agency"} | ({"agency_date"} if template == "L3" else set())
    _need(need <= set(fields) and set(fields) <= need | {"ref"}, "fields_invalid")
    _need(all(isinstance(fields[k], str) and _DAY.fullmatch(fields[k]) for k in ("date", "agency_date") if k in fields),
          "date_invalid")
    for k in ("date", "agency_date"):
        if k in fields:
            try:
                date.fromisoformat(fields[k])
            except ValueError:
                raise PublicUpdatesError("date_invalid") from None
    _need(isinstance(fields["agency"], str) and _AGENCY.fullmatch(fields["agency"]) is not None, "agency_invalid")
    ref = fields.get("ref")
    _need(ref is None or (isinstance(ref, str) and _REF.fullmatch(ref) is not None), "ref_invalid")
    suffix = f" (reference {ref})" if ref and template in ("L1", "L2") else ""
    return TEMPLATES[template].format(date=fields["date"], agency=fields["agency"], ref=suffix,
                                      agency_date=fields.get("agency_date", ""))


def line_id(agency_key, request_id, event):
    basis = json.dumps([agency_key, request_id, event.get("type"), event.get("date"), event.get("source")],
                       sort_keys=True, default=str)
    return hashlib.sha256(basis.encode()).hexdigest()[:16]


def generate(timeline, today=None):
    """Pure. Returns (candidate lines, skipped[{reason,...}]) from a ``records-timeline-v1`` document."""
    _need(isinstance(timeline, dict) and timeline.get("schema") == "records-timeline-v1"
          and isinstance(timeline.get("requests"), list), "timeline_invalid")
    today = today or datetime.now(TZ).date().isoformat()
    out, skipped = [], []
    for req in timeline["requests"]:
        events = [e for e in req.get("events", []) if isinstance(e, dict) and not e.get("superseded")]
        blocked_days = {e.get("date") for e in events if e.get("type") in BLOCKING}
        for ev in events:
            spec = TRIGGERS.get(ev.get("type"))
            if spec is None:
                if ev.get("type") in BLOCKING:
                    skipped.append({"reason": "never_auto", "type": ev["type"]})
                continue
            tid, kinds = spec
            why = None
            source = ev.get("source")
            if req.get("issues"):
                why = "issue_flag"
            elif ev.get("corrects"):
                why = "correction"
            elif not isinstance(source, dict) or source.get("kind") not in kinds:
                why = "no_source"
            elif not (isinstance(ev.get("date"), str) and _DAY.fullmatch(ev["date"]) and ev["date"] <= today):
                why = "bad_date"
            elif tid in ("L4", "L5") and ev["date"] in blocked_days:
                why = "never_auto_same_day"
            fields = {"date": ev.get("date"), "agency": req.get("agency")}
            if tid == "L3" and why is None:
                new = ev.get("new_dates") or []
                if len(new) != 1:
                    why = "no_agency_date"
                else:
                    fields["agency_date"] = new[0]
            ref = req.get("agency_reference")
            if ref:
                fields["ref"] = ref
            if why is None:
                try:
                    text = render(tid, fields)
                except PublicUpdatesError as error:
                    why, text = error.code, None
            if why:
                skipped.append({"reason": why, "type": ev.get("type"), "date": ev.get("date")})
                continue
            out.append({"id": line_id(req.get("agency_key"), req.get("request_id"), ev), "template": tid,
                        "fields": fields, "text": text, "agency_key": str(req.get("agency_key")),
                        "event": {"type": ev["type"], "date": ev["date"], "source": source}})
    return out, skipped


# ---- switch ------------------------------------------------------------------------------------

def _switch_file_enabled(lock):
    raw = _read(lock.path(SWITCH), 4096, None)
    if raw is None:
        return False
    data = _json(raw, "switch_invalid")
    _need(isinstance(data, dict), "switch_invalid")
    return data.get("enabled") is True


def switch_content(project, round_):
    return (f"project: {project_id(project)}\naction: turn ON public status-line auto-posting (templates L1-L6 only)\n"
            f"hold: {HOLD_SECONDS // 3600} hours; caps: {CAP_PER_AGENCY_DAY} per agency per day, {CAP_PER_DAY} per day\n"
            f"round: {round_}")


def switch_payload(project):
    """The exact bytes G signs to turn the switch ON; each ON needs a fresh signature (the round counts switch events)."""
    with _Locked(project) as lock:
        events = lock.events()
    return approval.payload(SWITCH_SCOPE, project_id(project), switch_content(project, _round(events)),
                            round_=_round(events))


def _round(events):
    return sum(1 for e in events if e["event"] == "switch")


def _switch_state(lock, events):
    if os.path.lexists(lock.path(KILL)):
        return "KILLED"
    _, last, _ = fold(events)
    if not _switch_file_enabled(lock) or last is None or last.get("state") != "on":
        return "OFF"
    try:
        content = switch_content(lock.project, last["round"])
        who = approval.verify(approval.payload(SWITCH_SCOPE, project_id(lock.project), content, round_=last["round"]),
                              last["signature"])
    except (approval.ApprovalError, KeyError, TypeError):
        return "OFF"
    return "ON" if who == last.get("principal") else "OFF"


def switch_state(project):
    with _Locked(project) as lock:
        return _switch_state(lock, lock.events())


def init(project, now=None):
    """Ship OFF: writes ``{"enabled": false}`` (never overwrites an existing switch file)."""
    with _Locked(project, create=True) as lock:
        if not os.path.lexists(lock.path(SWITCH)):
            _replace(lock.path(SWITCH), b'{"enabled": false}\n')
    return "OFF"


def enable(project, signature, now=None):
    now = _now(now)
    _need(isinstance(signature, str) and signature, "approval_required")
    with _Locked(project) as lock:
        _need(not os.path.lexists(lock.path(KILL)), "killed")
        events = lock.events()
        rnd = _round(events)
        try:
            who = approval.verify(approval.payload(SWITCH_SCOPE, project_id(project), switch_content(project, rnd),
                                                   round_=rnd), signature)
        except approval.ApprovalError:
            raise PublicUpdatesError("approval_required") from None
        lock.append("switch", now, state="on", actor=who, principal=who, round=rnd, signature=signature)
        _replace(lock.path(SWITCH), b'{"enabled": true}\n')
        return "ON"


def turn_off(project, actor, now=None):
    now = _now(now)
    with _Locked(project, create=True) as lock:
        lock.append("switch", now, state="off", actor=_actor(actor))
        _replace(lock.path(SWITCH), b'{"enabled": false}\n')
    return "OFF"


def kill(project, actor, now=None):
    """Kill switch: create the kill file, switch OFF, retract every line still holding (or proposed)."""
    now = _now(now)
    with _Locked(project, create=True) as lock:
        actor = _actor(actor)
        fd = os.open(lock.path(KILL), os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        lock.append("switch", now, state="killed", actor=actor)
        _replace(lock.path(SWITCH), b'{"enabled": false}\n')
        return _retract_waiting(lock, now, "kill", actor)


def _observe_kill(lock, now):
    """A kill file seen by ``advance`` (made by hand): persist a killed switch event and disable the switch file,
    so deleting the kill file later leaves the switch OFF until a fresh signed ON."""
    if (fold(lock.events())[1] or {}).get("state") != "killed":
        lock.append("switch", now, state="killed", actor="system")
    _replace(lock.path(SWITCH), b'{"enabled": false}\n')
    return _retract_waiting(lock, now, "kill", "system")


def _retract_waiting(lock, now, reason, actor):
    lines, _, _ = fold(lock.events())
    gone = [row["id"] for row in lines.values() if row["state"] in ("proposed", "holding")]
    for lid in gone:
        lock.append("retract", now, id=lid, reason=reason, actor=actor)
    return gone


# ---- lines -------------------------------------------------------------------------------------

def propose(project, candidates, now=None):
    """Journal candidate lines as ``proposed`` (duplicates by event id are ignored). Returns the new ids."""
    now = _now(now)
    new = []
    with _Locked(project, create=True) as lock:
        known = fold(lock.events())[0]
        for cand in candidates:
            _need(render(cand["template"], cand["fields"]) == cand["text"], "text_mismatch")
            if cand["id"] in known:
                continue
            lock.append("line", now, id=cand["id"], line={k: cand[k] for k in
                                                           ("template", "fields", "text", "agency_key", "event")})
            known[cand["id"]] = True
            new.append(cand["id"])
    return new


def retract(project, lid, actor, reason="undo", now=None):
    """Undo a posted line, or veto one still holding. History stays in the journal; the feed drops the line."""
    now = _now(now)
    _need(isinstance(lid, str) and _LINE_ID.fullmatch(lid), "line_unknown")
    _need(isinstance(reason, str) and re.fullmatch(r"[a-z_ ]{1,40}", reason), "reason_invalid")
    with _Locked(project) as lock:
        row = fold(lock.events())[0].get(lid)
        _need(row is not None, "line_unknown")
        _need(row["state"] in ("proposed", "holding", "posted"), "invalid_transition")
        lock.append("retract", now, id=lid, reason=reason, actor=_actor(actor))
        _write_feed(lock)
    return "retracted"


def _refuse(lock, row, code, now):
    lock.append("refused", now, id=row["id"], code=code)
    lock.append("alert", now, id=row["id"], code=code, agency_key=row["agency_key"])


def advance(project, now=None):
    """One pass: proposed -> holding (switch ON), holding past the hold -> posted if every control still allows it."""
    now = _now(now)
    result = {"state": None, "held": [], "posted": [], "refused": [], "retracted": []}
    with _Locked(project) as lock:
        state = result["state"] = _switch_state(lock, lock.events())
        if state == "KILLED":
            result["retracted"] = _observe_kill(lock, now)
            return result
        if state != "ON":
            return result
        for row in sorted(fold(lock.events())[0].values(), key=lambda r: r["proposed_at"]):
            if row["state"] == "proposed":
                lock.append("hold", now, id=row["id"], hold_until=int(now) + HOLD_SECONDS)
                result["held"].append(row["id"])
        for row in sorted(fold(lock.events())[0].values(), key=lambda r: (r.get("hold_until", 0), r["id"])):
            if row["state"] != "holding" or row["hold_until"] > now:
                continue
            if os.path.lexists(lock.path(KILL)):  # checked before every post
                result["retracted"] += _observe_kill(lock, now)
                result["state"] = "KILLED"
                break
            posted = [r for r in fold(lock.events())[0].values()
                      if r["state"] in ("posted", "retracted") and r.get("posted_at") and _local_day(r["posted_at"]) == _local_day(now)]
            if len(posted) >= CAP_PER_DAY:
                code = "cap_day"
            elif sum(1 for r in posted if r["agency_key"] == row["agency_key"]) >= CAP_PER_AGENCY_DAY:
                code = "cap_agency_day"
            elif render(row["template"], row["fields"]) != row["text"]:
                code = "text_mismatch"
            else:
                code = None
            if code:
                _refuse(lock, row, code, now)
                result["refused"].append([row["id"], code])
            else:
                lock.append("post", now, id=row["id"])
                result["posted"].append(row["id"])
        _write_feed(lock)
    return result


def _write_feed(lock):
    lines = [r for r in fold(lock.events())[0].values() if r["state"] == "posted"]
    feed = {"schema": FEED_SCHEMA, "private_until_reviewed": True, "lines": [
        {"id": r["id"], "template": r["template"], "agency_key": r["agency_key"], "date": r["fields"]["date"],
         "text": r["text"], "posted_at": _iso(r["posted_at"])} for r in sorted(lines, key=lambda r: r["posted_at"])]}
    _replace(lock.path(FEED), (json.dumps(feed, sort_keys=True, indent=1) + "\n").encode())


def status(project):
    with _Locked(project) as lock:
        events = lock.events()
        lines, _, alerts = fold(events)
        counts = {}
        for row in lines.values():
            counts[row["state"]] = counts.get(row["state"], 0) + 1
        return {"switch": _switch_state(lock, events), "lines": counts, "alerts": len(alerts)}


# ---- command line ------------------------------------------------------------------------------

def _load_timeline(project, run_id):
    _need(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", run_id or "") is not None, "run_id_invalid")
    raw = _read(os.path.join(project, "run-outputs", run_id, "outcomes", "timeline.json"), MAX_INPUT, None)
    _need(raw is not None, "timeline_missing")
    return _json(raw, "timeline_invalid")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="records public-updates", description=(
        "Status-line generator, journal and switches. Nothing is posted anywhere; ships OFF."))
    parser.add_argument("action", choices=("init", "status", "propose", "advance", "payload", "enable", "off", "kill",
                                           "retract"))
    parser.add_argument("--project", required=True)
    parser.add_argument("--run-id")
    parser.add_argument("--signature-file")
    parser.add_argument("--actor", default="console")
    parser.add_argument("--line")
    args = parser.parse_args(argv)
    try:
        if args.action == "init":
            out = {"switch": init(args.project)}
        elif args.action == "status":
            out = status(args.project)
        elif args.action == "propose":
            lines, skipped = generate(_load_timeline(args.project, args.run_id))
            out = {"proposed": propose(args.project, lines), "candidates": len(lines), "skipped": len(skipped)}
        elif args.action == "advance":
            out = advance(args.project)
        elif args.action == "payload":
            sys.stdout.buffer.write(switch_payload(args.project))
            return 0
        elif args.action == "enable":
            raw = _read(args.signature_file or "", 8192, None)
            _need(raw is not None, "approval_required")
            out = {"switch": enable(args.project, raw.decode("utf-8", "replace"))}
        elif args.action == "off":
            out = {"switch": turn_off(args.project, args.actor)}
        elif args.action == "kill":
            out = {"switch": "KILLED", "retracted": kill(args.project, args.actor)}
        else:
            out = {"result": retract(args.project, args.line, args.actor)}
        print(json.dumps(out, sort_keys=True))
        return 0
    except PublicUpdatesError as error:
        print(error.code, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
