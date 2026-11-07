"""Admin console Calendar (CON-CAL-1): month grid, agenda and a content-limited .ics export.

  records panel     ->  GET /calendar[?month=YYYY-MM]   month grid plus agenda
                        GET /calendar.ics                RFC 5545 all-day events, no bodies, URLs or addresses
  records calendar  ->  --project P --run-id R [--ics OUT]

Read-only. Every entry is DATED (a recorded or agency-stated date) or INFERRED (arithmetic or a projection) and
carries its source; a source that cannot be read is named unavailable and the others still list. Follow-up
drafts are projected only by calling ``followup_clock.plan`` on copies of register rows; nothing is queued,
sent, approved or published here. Overdue is arithmetic on our records, not a legal finding.
"""
import argparse
import calendar
import copy
import hashlib
import json
import os
import re
import sys
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from ..meetings_manual import _read_bytes, ManualMeetingError

from . import console_board as cb, console_oversight as co, followup_clock as fc, mail_send, public_packets
from .outcomes.reading import BANNER, esc

KINDS = {"statutory_due": "Statutory due", "extension_limit": "Extension limit", "expected_window": "Agency-stated date",
         "followup_draft_due": "Follow-up draft due", "publication_pending": "Publication step",
         "link_expiry": "Link expiry", "overdue": "Overdue", "followup_planned_send": "Follow-up planned send", "meeting": "Meeting"}
BASIS = {"recorded_receipt": "DATED", "submission_proxy": "INFERRED", "agency_promised": "DATED"}
CLOCK_SOURCE = "register clock (Gov. Code 7922.535 arithmetic on our records)"
LABEL = "Private; review required. Overdue is on our records; not a legal finding. Nothing is sent from this page."
MONTH = re.compile(r"(\d{4})-(0[1-9]|1[0-2])")
MONTH_YEARS = (1900, 2200)  # outside this range the grid arithmetic would run into date.min/date.max
UID_HOST = "@calendar.deflock.invalid"


def _row_ok(r):
    """True when a register row has the shape the projection reads; a bad row degrades only the follow-up source."""
    if not isinstance(r, dict):
        return False
    if not all(isinstance(r.get(k), str) and r.get(k) for k in ("agency_key", "request_id")):
        return False
    if r.get("agency") is not None and not isinstance(r.get("agency"), str):
        return False
    if not isinstance(r.get("status", ""), str):
        return False
    return all(r.get(k) is None or isinstance(r.get(k), dict) for k in ("deadline", "promised"))


def load(project, run_id, now=None, *, send_config=None, meetings_dir=None):
    model = co.load(project, run_id, None, now)
    model["meetings"] = _load_meetings(meetings_dir)
    model["sends"] = {"state": "ok", "rows": [], "configured": send_config is not None}
    if send_config is not None:
        try:
            model["sends"]["rows"] = mail_send.results(project, strict=True)
        except (mail_send.MailSendError, OSError, ValueError, KeyError, TypeError):
            model["sends"] = co._unavailable("sends_unreadable")
    try:
        model["packets"] = {"state": "ok", "rows": public_packets.pending(project, model["now"].timestamp())}
    except public_packets.PacketError as error:
        model["packets"] = co._unavailable(error.code)
    except (OSError, ValueError, KeyError, TypeError):
        model["packets"] = co._unavailable("packets_unreadable")
    try:
        register, settings = fc.read_register(project, run_id), fc.load_settings(project)
        if register is None:
            model["followup"] = co._unavailable("register_missing")
        elif not isinstance(register.get("requests"), list) or not isinstance(register.get("as_of"), str) \
                or not all(_row_ok(r) for r in register["requests"]):
            model["followup"] = co._unavailable("register_invalid")
        elif settings["invalid"]:
            model["followup"] = co._unavailable("settings_invalid")
        elif model["queue"]["state"] != "ok":
            model["followup"] = co._unavailable("queue_unavailable")
        else:
            model["followup"] = {"state": "ok", "register": register, "settings": settings}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        model["followup"] = co._unavailable("register_unreadable")
    return model


def _load_meetings(directory):
    rows, notes, seen = [], [], set()
    if directory is None:
        return rows, notes
    root = Path(directory)
    for filename in ("meetings.json", "meetings-manual.json"):
        try:
            if (root / "kit").is_symlink():
                raise ValueError("symlink kit")
            data = _read_bytes(root / "kit" / filename, filename)
            if data is None:
                continue
            doc = json.loads(data.decode("utf-8"))
            if not isinstance(doc, dict) or doc.get("schema_version") != 1 or not isinstance(doc.get("meetings"), list):
                raise ValueError("invalid meetings")
            staged, local_seen, undated = [], set(), 0
            for m in doc["meetings"]:
                if not isinstance(m, dict) or not all(isinstance(m.get(k), str) for k in ("id", "body", "agenda_item")):
                    raise ValueError("invalid meeting")
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", m["id"]):
                    raise ValueError("invalid meeting id")
                day = cb._day(m.get("starts_at"))
                if day is None:
                    undated += 1
                    continue
                if m.get("status", "scheduled") not in ("scheduled", "cancelled") or m.get("not_scanned"):
                    continue
                if m["id"] in seen or m["id"] in local_seen:
                    continue
                local_seen.add(m["id"])
                label = "%s: %s - keyword lead, verify on the official agenda" % (m["body"], m["agenda_item"])
                for flag, text in ((m.get("status") == "cancelled", "cancelled"),
                                   (not m.get("time_known"), "time unknown"), (m.get("stale"), "stale source"),
                                   ((root / "content" / "meetings" / (m["id"] + ".json")).is_file(), "comment kit staged")):
                    if flag:
                        label += " (%s)" % text
                generated = cb._day(doc.get("generated"))
                source = "%s agenda via kit/%s (generated %s)" % (m.get("source", "unknown"), filename, generated or "unknown")
                staged.append((day, "meeting", m["id"], m["body"], "", label, "DATED", source))
            rows.extend(staged)
            seen.update(local_seen)
            if undated:
                notes.append("undated meetings skipped: %d (%s)" % (undated, filename))
        except (OSError, ValueError, TypeError, RecursionError, ManualMeetingError):
            notes.append("unavailable (meetings): %s" % filename)
    return rows, notes


def _iso(day):
    return day.isoformat()


def _projection(model, today, add, notes):
    reg, settings = model["followup"]["register"], model["followup"]["settings"]
    rows, as_of = model["queue"]["rows"], reg["as_of"]
    pending = {(r["item_id"], r["kind"]) for r in rows if r["status"] == "pending"}
    queued = {(r["item_id"], r["kind"]) for r in rows}
    off, refused = set(), 0
    src = "follow-up planner projection (register run %s)" % model["run_id"]

    def probe(row, day, only=None):
        found = fc.plan({**reg, "requests": [row]}, settings, rows, as_of=_iso(day))
        return [c for c in found if only is None or c["kind"] == only]

    for r in reg["requests"]:
        if str(r.get("status", "")).strip().casefold() in fc.CLOSED:
            continue
        setting = fc._setting(settings, r["agency_key"])
        if setting["autonomy"] != "draft":
            off.add(r.get("agency") or r["agency_key"])
        wait = timedelta(days=1 + setting.get("silence_after_days", 0))
        deadline, promised = r.get("deadline") or {}, r.get("promised") or {}
        hits = []  # (candidate, date)
        now_day = fc._day(as_of)
        hits += [(c, now_day) for c in probe(r, now_day)]
        due = fc._day(deadline.get("due"))
        if deadline.get("state") in ("running", "due_today", "not_started") and due:
            p = copy.deepcopy(r)
            p["deadline"]["state"], p["promised"] = "past_due_reminder", {"state": "none", "date": None}
            hits += [(c, due + wait) for c in probe(p, due + wait)]
        pdate = fc._day(promised.get("date"))
        if promised.get("state") in ("upcoming", "due_today") and pdate:
            p = copy.deepcopy(r)
            p["promised"]["state"], p["deadline"]["state"] = "passed", "running"
            hits += [(c, pdate + wait) for c in probe(p, pdate + wait)]
        for q in rows:
            changed = fc._day(q.get("updated_at"))
            if q["status"] == "approved" and q["kind"] in fc.REMINDERS and changed and q["agency_id"] == r["agency_key"] \
                    and q["request_id"] == r["request_id"]:
                d = changed + timedelta(days=setting["notice_after_days"])
                if d > now_day:
                    hits += [(c, d) for c in probe(r, d, fc.NOTICE)]
        refused += 0 if hits else 1
        for c, day in hits:
            key = (c["item_id"], c["kind"])
            if day == now_day and key in queued and key not in pending:
                continue  # already drafted and decided; the planner adds nothing new
            state = "awaiting your approval (Home)" if key in pending else "draft only, sends after your approval"
            add(day, "followup_draft_due", r["agency_key"], r.get("agency"), r["request_id"],
                "%s (pattern silence) — %s" % (c["kind"], state), "INFERRED", src)
    if refused:
        notes.append("no follow-up projected for %d requests (closed, unresolved issues, determined, no clock, or drafting off)" % refused)
    notes.extend("follow-up drafting is off for this agency: %s" % name for name in sorted(off))


def build(model, today, horizon_days=90, since=None):
    """(entries, notes). Entries are sorted by date; ``since`` widens the window backwards for the month view."""
    out, notes, seen = [], [], set()
    low, high = since or today, today + timedelta(days=horizon_days)
    names = {}

    def add(day, kind, agency_key, agency, request, label, basis, source, force=False):
        if day is None or not (force or low <= day <= high):
            return
        key = (kind, agency_key, request or "", day, label)
        if key in seen:
            return
        seen.add(key)
        names.setdefault(agency_key, agency or agency_key)
        out.append({"date": day, "kind": kind, "agency_key": agency_key or "", "agency": agency or agency_key or "",
                    "request": request or "", "label": label, "basis": basis, "source": source})

    tl = model["timeline"]
    if tl["state"] != "ok":
        notes.append("timeline: unavailable (%s)" % tl.get("code"))
    else:
        no_clock = 0
        for r in cb._requests(model):
            k, n, req = r.get("agency_key"), r.get("agency"), r.get("request_id")
            clock = r.get("clocks") or {}
            if r.get("closed"):
                continue
            basis = BASIS.get(clock.get("basis"))
            if basis is None or not clock.get("due"):
                no_clock += clock.get("state") != "determined"
            elif clock.get("state") != "determined":
                add(cb._day(clock["due"]), "statutory_due", k, n, req, "statutory determination due", basis, CLOCK_SOURCE)
                ext = cb._day(clock.get("extended_due"))
                if ext != cb._day(clock["due"]):
                    add(ext, "extension_limit", k, n, req, "latest date if the 14-day extension applies", basis, CLOCK_SOURCE)
            for e in r["events"]:
                if e["type"] in ("promised date", "extension notice") and not e.get("superseded"):
                    for d in e.get("new_dates") or []:
                        add(cb._day(d), "expected_window", k, n, req, "%s: agency-stated date" % e["type"], "DATED",
                            "%s (source %s)" % (e["type"], (e.get("source") or {}).get("kind") or "unknown"))
            for m in r["overdue_marks"]:
                if m.get("kind") != "unknown" and cb._day(m.get("date")):
                    add(cb._day(m["date"]), "overdue", k, n, req, "%s: %s" % (m["kind"], m["label"]),
                        BASIS.get(m.get("basis"), "INFERRED"), "timeline overdue mark", force=True)
        if no_clock:
            notes.append("no clock: %d open requests have no usable statutory date" % no_clock)
    links = model["links"]
    if links["state"] != "ok":
        notes.append("release links: unavailable (%s)" % links.get("code"))
    else:
        unstated = 0
        for link in (links.get("doc") or {}).get("links", []):
            expiry = link.get("expiry") or {}
            day = cb._day(expiry.get("date"))
            if day is None:
                unstated += 1
                continue
            dated = expiry.get("basis") == "stated_date" and not expiry.get("year_inferred")
            ids = link.get("agency_ids") or [""]
            add(day, "link_expiry", ids[0], ids[0], "", "release link on %s expires" % link.get("host"),
                "DATED" if dated else "INFERRED", "release-link register")
        if unstated:
            notes.append("no stated expiry for %d release links" % unstated)
    if model["public"]["state"] != "ok":
        notes.append("public updates: unavailable (%s)" % model["public"].get("code"))
    else:
        for line in model["public"].get("lines") or []:
            if line.get("state") == "holding" and isinstance(line.get("hold_until"), int):
                day = datetime.fromtimestamp(line["hold_until"], timezone.utc).date()
                add(day, "publication_pending", line["agency_key"], None, "", "status line hold ends (veto window)",
                    "DATED", "public-updates journal (hold_until)")
    if model["packets"]["state"] != "ok":
        notes.append("public packets: unavailable (%s)" % model["packets"].get("code"))
    else:
        for p in model["packets"]["rows"]:
            add(cb._day(p.get("expires_at")), "publication_pending", p["agency_key"], None, p.get("request_id"),
                "%s packet (%s) expires unless decided" % (p["kind"], p["status"]), "INFERRED",
                "public packet journal (proposal time + expiry)")
    if model["followup"]["state"] != "ok":
        notes.append("follow-up projection: unavailable (%s)" % model["followup"].get("code"))
    else:
        staged, extra = [], []
        try:
            _projection(model, today, lambda *a, **k: staged.append((a, k)), extra)
        except (KeyError, TypeError, ValueError, AttributeError, IndexError, OverflowError):
            notes.append("follow-up projection: unavailable (register_invalid)")
        else:
            for a, k in staged:
                add(*a, **k)
            notes.extend(extra)
    queue, sends = model["queue"], model.get("sends", {})
    if queue["state"] != "ok":
        notes.append("follow-up planned sends: unavailable (queue_unavailable)")
    if sends.get("state") == "unavailable":
        notes.append("follow-up planned sends: unavailable (sends_unreadable)")
    latest = {(r["queue_id"], r.get("revision")): r for r in sends.get("rows", [])}
    for q in queue.get("rows", []) if queue["state"] == "ok" else []:
        if q["kind"] != "followup-playbook" or q["status"] not in ("pending", "approved"):
            continue
        evidence = q.get("evidence") or {}
        day = cb._day(evidence.get("planned_send_date"))
        result = latest.get((q["id"], q.get("revision")), {})
        if result.get("state") == "sent":
            continue
        state = "awaiting your approval (Home)"
        if q["status"] == "approved":
            if sends.get("state") == "unavailable":
                continue  # cannot establish whether this revision was sent
            state = "approved; sending is yours"
            if not sends.get("configured"):
                state += " (send not tracked)"
            elif result.get("state") in ("unknown", "attempt"):
                state += " (delivery unresolved; reconcile in the outbox before any retry)"
        add(day, "followup_planned_send", q["agency_id"], None, q["request_id"],
            "%s follow-up: %s" % (evidence.get("pattern", "unknown"), state), "INFERRED",
            "follow-up engine route (run %s)" % q["source_run_id"])
    meetings, meeting_notes = model.get("meetings", ([], []))
    for meeting in meetings:
        add(*meeting)
    notes.extend(meeting_notes)
    for e in out:
        e["agency"] = names.get(e["agency_key"]) or e["agency"]
    out.sort(key=lambda e: (e["date"], e["kind"], e["agency_key"], e["request"], e["label"]))
    return out, notes


def entries(model, today, horizon_days=90, since=None):
    return build(model, today, horizon_days, since)[0]


# ---- .ics --------------------------------------------------------------------------------------------

PATHS = re.compile(r"(?<![\w])(?:[A-Za-z]:[\\/]|~[\\/]?|\.{1,2}[\\/]|[\\/])[^\s)\]'\",;]*"
                   r"|(?<![\w.-])[\w.-]+(?:[\\/][\w.-]+){2,}|(?<![\w.-])[\w.-]+[\\/][\w.-]*\.\w+\b")


def _scrub(text):
    text = re.sub(r"https?\S*|file:\S*", "", text)
    text = PATHS.sub("", text)
    text = re.sub(r"[0-9a-fA-F]{32,}", "", text).replace("@", "")
    return re.sub(r"\s+", " ", text).strip()


def _esc(text):
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")


def _fold(line):
    raw, parts = line.encode("utf-8"), []
    while len(raw) > (75 if not parts else 74):  # continuation lines carry a leading space
        cut = 75 if not parts else 74
        while cut > 0 and (raw[cut] & 0xC0) == 0x80:
            cut -= 1
        parts.append(raw[:cut])
        raw = raw[cut:]
    parts.append(raw)
    return "\r\n ".join(p.decode("utf-8") for p in parts)


def uid(e):
    blob = "|".join((e["kind"], e["agency_key"], e["request"], _iso(e["date"])))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32] + UID_HOST


def to_ics(items, now):
    stamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//DeFlock Campaign Tool//records console//EN", "CALSCALE:GREGORIAN"]
    done = set()
    for e in items:
        if uid(e) in done:
            continue
        done.add(uid(e))
        summary = "%s: %s %s (%s)" % (KINDS[e["kind"]], _scrub(e["agency"]), _scrub(e["request"]), e["basis"])
        if e["kind"] == "meeting":
            summary = "Meeting: %s (DATED)" % _scrub(e["agency"])
        lines += ["BEGIN:VEVENT", "UID:" + uid(e), "DTSTAMP:" + stamp, "DTSTART;VALUE=DATE:" + e["date"].strftime("%Y%m%d"),
                  "SUMMARY:" + _esc(re.sub(r"\s+", " ", summary)), "DESCRIPTION:" + _esc(_scrub(e["source"])), "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "".join(_fold(x) + "\r\n" for x in lines)


# ---- rendering ---------------------------------------------------------------------------------------

def _month(text, today):
    m = MONTH.fullmatch(text) if text else None
    if m and not MONTH_YEARS[0] <= int(m.group(1)) <= MONTH_YEARS[1]:
        return None
    return date(int(m.group(1)), int(m.group(2)), 1) if m else (None if text else today.replace(day=1))


def render(model, today, first):
    last = date(first.year + (first.month == 12), first.month % 12 + 1, 1) - timedelta(days=1)
    horizon = max(90, (last - today).days)
    items, notes = build(model, today, horizon, since=min(today, first))
    prev, nxt = (first - timedelta(days=1)).replace(day=1), last + timedelta(days=1)
    out = ['<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="robots" content="noindex,nofollow">'
           "<title>Calendar</title></head><body>", '<p class="notice">%s</p>' % esc(BANNER),
           '<nav aria-label="Pages"><a href="/">Home</a> <a href="/board">Board</a> <a href="/oversight">Oversight</a> '
           '<a href="/next-steps">Drafts and next steps</a> <a href="/calendar.ics">Download .ics</a></nav>',
           "<h1>Calendar: %s</h1>" % esc(first.strftime("%B %Y")),
           "<p>Run %s. %s DATED means a recorded or agency-stated date; INFERRED means arithmetic or a projection. "
           "Subscribing from a phone needs hosting, which is not built.</p>" % (esc(model["run_id"]), esc(LABEL)),
           '<p><a href="/calendar?month=%s">&larr; %s</a> <a href="/calendar?month=%s">%s &rarr;</a></p>' % (
               prev.strftime("%Y-%m"), prev.strftime("%b %Y"), nxt.strftime("%Y-%m"), nxt.strftime("%b %Y"))]
    out += ['<p class="diag" role="status">%s</p>' % esc(n) for n in notes]
    out.append("<table><caption>%s</caption><thead><tr>%s</tr></thead><tbody>" % (
        esc(first.strftime("%B %Y")), "".join('<th scope="col">%s</th>' % d for d in calendar.day_abbr)))
    for week in calendar.Calendar().monthdayscalendar(first.year, first.month):
        cells = []
        for n in week:
            day = date(first.year, first.month, n) if n else None
            labels = "".join("<br>%s: %s (%s)" % (esc(KINDS[e["kind"]]), esc(e["agency"]), e["basis"])
                             for e in items if e["date"] == day)
            cells.append("<td>%s%s</td>" % ("<strong>%d</strong>" % n if day == today else (n or ""), labels))
        out.append("<tr>%s</tr>" % "".join(cells))
    out.append("</tbody></table><h2>Agenda</h2>")
    ordered = sorted(items, key=lambda e: (not (e["kind"] == "overdue" or e["date"] < today), e["date"] != today, e["date"],
                                           e["kind"], e["agency_key"]))
    out.append("<ul>")
    for e in ordered:
        flag = "<strong>OVERDUE</strong> " if e["kind"] == "overdue" else ("<strong>TODAY</strong> " if e["date"] == today else "")
        out.append("<li>%s%s &mdash; %s: %s %s &mdash; %s (%s; source: %s)</li>" % (
            flag, e["date"], esc(KINDS[e["kind"]]), esc(e["agency"]), esc(e["request"]), esc(e["label"]), e["basis"],
            esc(e["source"])))
    out.append("</ul>" if ordered else "<li>Nothing dated in this window.</li></ul>")
    return "\n".join(out + ["</body></html>"]) + "\n"


# ---- panel seam and CLI ------------------------------------------------------------------------------

def serve(handler):
    from .admin_panel import SECURITY
    parts = urllib.parse.urlsplit(handler.path)
    if parts.path not in ("/calendar", "/calendar.ics"):
        return False
    now = datetime.now(timezone.utc)
    first = _month(urllib.parse.parse_qs(parts.query).get("month", [None])[0], now.date())
    if first is None:
        handler._fail(400)
        return True
    options = handler.server.options
    try:
        model = load(options["project"], options["run_id"], now, send_config=options.get("send_config"),
                     meetings_dir=getattr(handler.server, "meetings_dir", None))
    except ValueError:
        handler._fail(503)
        return True
    if parts.path == "/calendar":
        handler._send(200, render(model, now.date(), first))
        return True
    data = to_ics(entries(model, now.date()), now).encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "text/calendar; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    for k, v in SECURITY:
        handler.send_header(k, v)
    handler.end_headers()
    if handler.command != "HEAD":
        handler.wfile.write(data)
    return True


def main():
    parser = argparse.ArgumentParser(prog="records calendar", description="Read-only calendar; sends nothing.")
    parser.add_argument("--project", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--ics", help="write the content-limited .ics file here")
    args = parser.parse_args()
    now = datetime.now(timezone.utc)
    try:
        model = load(args.project, args.run_id, now)
    except (ValueError, OSError):
        print("calendar_unreadable", file=sys.stderr)
        return 1
    items, notes = build(model, now.date())
    if args.ics:
        fd = os.open(args.ics, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(to_ics(items, now))
    for e in items:
        print("%s  %s  %s %s  %s (%s; %s)" % (e["date"], KINDS[e["kind"]], e["agency"], e["request"], e["label"], e["basis"], e["source"]))
    for n in notes:
        print("note:", n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
