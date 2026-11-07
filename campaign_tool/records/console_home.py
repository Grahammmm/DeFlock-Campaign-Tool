"""Read-only console Home. Decisions remain on their existing signed action pages."""
import urllib.parse
from datetime import datetime, timedelta, timezone

from . import console_board as cb, console_oversight as co, console_outstanding, console_standing, mail_send, public_packets
from .outcomes.reading import BANNER, esc
from . import console_calendar, console_layout

GROUPS = ("request", "follow-up", "send", "site-update", "social-post", "status-line", "packet")
LABELS = ("Requests", "Follow-ups", "Sends", "Site updates", "Social posts", "Status lines", "Packets")
PLAIN_KINDS = {
    "first-request": "First request", "reminder-no-response": "Reminder: no reply",
    "reminder-promised-date": "Reminder: promised date passed",
    "formal-notice-no-response": "Formal notice: no reply", "appeal-denial": "Reply to a refusal",
    "reply-extension": "Reply to an extension", "fee-reply": "Reply about fees",
    "followup-missing-records": "Ask for missing records", "next-social-post": "Suggested post",
    "next-site-update": "Suggested site update", "next-followup-probe": "Suggested follow-up",
}
DISCONNECTED = (("agency-page", "CON-SITEPR-1"), ("redaction", "CON-REDACT-1"), ("finding", "CON-FIND-1"))
NEXT = {"next-site-update": ("site-update", "site"), "next-social-post": ("social-post", "social"),
        "next-followup-probe": ("follow-up", "followup")}


def load(project, run_id, run_report_path=None, now=None, *, send_config=None, meetings_dir=None):
    model = co.load(project, run_id, run_report_path, now)
    try:
        calendar = console_calendar.load(project, run_id, model["now"], send_config=send_config,
                                         meetings_dir=meetings_dir)
        if calendar.get("state") == "unavailable":
            model["calendar"] = co._unavailable(calendar.get("code", "calendar_unreadable"))
        else:
            today = model["now"].date()
            entries = console_calendar.entries(calendar, today, horizon_days=7)
            model["calendar"] = {"state": "ok", "rows": sorted(
                (e for e in entries if e["kind"] != "overdue" and today <= e["date"] <= today + timedelta(days=7)),
                key=lambda e: (e["date"], e["kind"]))}
    except Exception:
        model["calendar"] = co._unavailable("calendar_unreadable")
    try:
        board = cb.load(project, run_id)
        model["waiting"] = ({"state": "ok", "rows": waiting_rows(board, model["now"].date())}
                            if board["timeline"]["state"] == "ok" else dict(board["timeline"]))
    except Exception:
        model["waiting"] = co._unavailable("board_unreadable")
    try:
        model["packets"] = {"state": "ok", "rows": public_packets.pending(project, model["now"].timestamp())}
    except public_packets.PacketError as error:
        model["packets"] = co._unavailable(error.code)
    except (OSError, ValueError, KeyError, TypeError):
        model["packets"] = co._unavailable("packets_unreadable")
    model["sends"] = {"state": "ok", "rows": [], "configured": send_config is not None}
    if send_config is not None:
        try:
            model["sends"]["rows"] = mail_send.results(project, strict=True)
        except (mail_send.MailSendError, OSError, ValueError, KeyError, TypeError):
            model["sends"] = co._unavailable("sends_unreadable")
    return model


def _age(now, made):
    return None if made is None else max(0, int((now.timestamp() - made) // 3600))


def _overdue(model, agency, request, today):
    marks = []
    if request:
        for req in cb._requests(model):
            if req.get("agency_key") == agency and request in (req.get("request_id"), req.get("ledger_request_id")):
                for mark in req["overdue_marks"]:
                    day = cb._day(mark.get("date"))
                    if mark.get("kind") != "unknown" and day and day <= today:
                        marks.append(((today - day).days, mark["date"], mark.get("kind")))
    return max(marks) if marks else (None, None, None)


def decision_rows(model, today):
    rows, now = [], model["now"]

    def add(ident, group, kind, agency, request, why, age, link, source, hours=None):
        overdue, day, mark = _overdue(model, agency, request, today)
        rows.append({"id": ident, "kind_group": group, "kind": kind, "agency": agency, "request": request,
                     "what": {"public": "Public status line", "packets": "Update packet"}.get(source, PLAIN_KINDS.get(kind, kind)), "why": why, "age_hours": age,
                     "overdue_days": overdue, "overdue_date": day, "overdue_kind": mark,
                     "hours_left": hours, "link": link, "source": source})

    sends = model["sends"]
    latest = {(r["queue_id"], r.get("revision")): r for r in sends.get("rows", [])}
    for row in model["queue"].get("rows", []):
        kind, ident = row["kind"], row["id"]
        made = co._parse_ts(row.get("created_at"))
        age = _age(now, made.timestamp() if made else None)
        evidence = row.get("evidence") or {}
        why = evidence.get("why") or " ".join(str(evidence.get(k) or "") for k in ("clock_kind", "clock_date")).strip()
        if row["status"] == "pending":
            group, _ = NEXT.get(kind, ("request" if kind == "first-request" else "follow-up", None))
            link = "/review?id=" + urllib.parse.quote(ident, safe="")
            add(ident, group, kind, row["agency_id"], row["request_id"], why or "Draft awaiting decision", age, link, "queue")
        elif row["status"] == "approved" and kind not in NEXT and sends.get("configured") and sends["state"] == "ok":
            result = latest.get((ident, row["revision"]))
            if result and result.get("state") == "sent":
                continue
            note = "Approved; not sent"
            if result and result.get("state") in ("unknown", "attempt"):
                note = "Delivery unresolved; reconcile in the outbox before any retry"
            add(ident, "send", kind, row["agency_id"], row["request_id"], note, age,
                "/send?id=" + urllib.parse.quote(ident, safe=""), "sends")
    public = model["public"]
    for line in public.get("lines", []):
        if line["state"] != "holding" and not (line["state"] == "proposed" and public.get("switch") == "OFF"):
            continue
        hours = max(0, int((line["hold_until"] - now.timestamp()) // 3600)) if line["state"] == "holding" else None
        event = line.get("event") or {}
        why = "%s %s %s" % (line.get("template", ""), event.get("type", ""), event.get("date", ""))
        add(line["id"], "status-line", line["state"], line["agency_key"], line.get("request_id"), why,
            _age(now, line["proposed_at"]), "/public-updates", "public", hours)
    for packet in model["packets"].get("rows", []):
        if packet["status"] != "proposed":
            continue
        add(packet["id"], "site-update" if packet["kind"] == "site_update" else "packet", packet["kind"],
            packet["agency_key"], packet["request_id"], "%s %s" % (packet["source"], packet["request_id"]),
            packet["age_hours"], "/packets", "packets", packet["hours_to_expiry"])

    return sorted(rows, key=lambda row: (row["age_hours"] is None,
                                        row["age_hours"] if row["age_hours"] is not None else 0, row["id"]))


def waiting_rows(board, today):
    requests = {(r.get("agency_key"), r.get("request_id")): r for r in cb._requests(board)}
    rows, seen = [], set()
    for row in cb.board_rows(board, today):
        key = (row["agency_key"], row["request_id"])
        if key in seen or not (cb.keep(row, "waiting") or cb.keep(row, "overdue")):
            continue
        seen.add(key)
        request = requests[key]
        asked = [cb._day(e.get("date")) for e in cb._live(request) if e["type"] == "sent"]
        marks = [cb._day(m.get("date")) for m in request["overdue_marks"] if m.get("kind") != "unknown"]
        marks = [d for d in marks if d and d <= today]
        since = min(marks) if marks else None
        rows.append(row | {"asked": min((d for d in asked if d), default=None),
                           "due_day": cb._day(row["due"]), "since": since,
                           "days": (today - since).days if since else None})
    return sorted(rows, key=lambda r: (not bool(r["overdue"]), -(r["days"] or 0),
                                      r["due_day"] is None, r["due_day"] or today,
                                      r["agency_key"] or "", r["request_id"] or ""))


def render_waiting(state, today):
    out = ['<section id="waiting"><h2>Waiting on agencies</h2>']
    if state["state"] != "ok":
        return "\n".join(out + ['<p role="status">Waiting on agencies: unavailable (%s).</p></section>' % esc(state.get("code"))])
    rows = state["rows"]
    out.append('<table><thead><tr>' + ''.join('<th scope="col">%s</th>' % h for h in
               ("Agency", "Request", "Asked on", "Due (basis)", "Status")) + '</tr></thead><tbody>')
    for r in rows[:40]:
        if r["overdue"]:
            status = ('<span class="overdue">OVERDUE %d days</span> since %s' % (r["days"], esc(r["since"]))
                      if r["days"] is not None else '<span class="overdue">OVERDUE</span> since unknown')
            status += ' (on our records; not a legal finding)'
        else:
            days = (r["due_day"] - today).days if r["due_day"] else None
            status = "due date unknown" if days is None else ("due in %d days" % days if days >= 0 else "due date passed; no overdue mark recorded")
        cells = ['<a href="/agency?agency=%s">%s</a>' % (esc(urllib.parse.quote(r["agency_key"] or "", safe="")), esc(r["agency"])),
                 esc(r["request_id"]), esc(str(r["asked"]) + " (DATED)" if r["asked"] else "unknown (UNKNOWN)"),
                 esc('%s (%s; %s)' % (r["due_day"], cb.BASIS_WORD.get(r["basis"], "UNKNOWN"), r["basis"] or "unknown")
                     if r["due_day"] else "unknown (UNKNOWN)"), status]
        out.append('<tr>%s</tr>' % ''.join('<td>%s</td>' % c for c in cells))
    out.append('</tbody></table>')
    if not rows:
        out.append('<p>No requests waiting on agencies.</p>')
    if len(rows) > 40:
        out.append('<p>and <a href="/board?filter=waiting">%d more on the Agencies page</a></p>' % (len(rows) - 40))
    return "\n".join(out + ['</section>'])


def render_home(model, token):
    rows = decision_rows(model, model["now"].date())
    calendar = model["calendar"]
    week = calendar.get("rows", [])
    week_count = str(len(week)) if calendar["state"] == "ok" else "unknown"
    waiting = model["waiting"]
    waiting_count = ("%d waiting on agencies (%d overdue)" % (len(waiting["rows"]), sum(bool(r["overdue"]) for r in waiting["rows"]))
                     if waiting["state"] == "ok" else "waiting on agencies: unknown")
    out = ["<h1>Home</h1>", '<p role="status">%d need your decision · %s due in the next 7 days · %s</p>' % (len(rows), week_count, waiting_count),
           '<section id="decide"><h2>Needs your decision</h2>']
    counts = ["%s %d" % (label, count) for group, label in zip(GROUPS, LABELS)
              if (count := sum(r["kind_group"] == group for r in rows))]
    if counts:
        out.append("<p>" + " · ".join(counts) + "</p>")
    for source in ("timeline", "queue", "public", "packets", "sends"):
        state = model[source]
        if state["state"] != "ok":
            out.append('<p role="status">%s: unavailable (%s).</p>' % (esc(source), esc(state.get("code"))))
    if model["packets"]["state"] == "ok" and not model["packets"]["rows"]:
        out.append("<p>No packets pending.</p>")
    out.append('<table><thead><tr>' + "".join('<th scope="col">%s</th>' % title for title in
               ("Agency", "What", "Waiting since", "Open")) + '</tr></thead><tbody>')
    for row in rows:
        what = esc(row["what"])
        if row["overdue_days"] is not None:
            what += ' <span class="overdue">OVERDUE %d days</span> (on our records; not a legal finding)' % row["overdue_days"]
        agency = '<a href="/agency?agency=%s">%s</a>' % (
            esc(urllib.parse.quote(row["agency"] or "", safe="")), esc(row["agency"]))
        cells = [agency, what, esc(co._ago(row["age_hours"])),
                 '<a href="%s">%s</a>' % (esc(row["link"]), "Review" if row["source"] == "queue" else "Open")]
        out.append("<tr>%s</tr>" % "".join("<td>%s</td>" % cell for cell in cells))
    out.append('</tbody></table></section><section id="week"><h2>Next 7 days</h2>')
    out.append("<p>%s</p>" % esc(cb.COMPUTED_LEGEND))
    if calendar["state"] != "ok":
        out.append('<p role="status">Calendar: unavailable (%s).</p>' % esc(calendar.get("code")))
    elif not week:
        out.append('<p>Nothing due in the next 7 days.</p>')
    else:
        out.append('<ul>')
        for entry in week[:20]:
            text = " · ".join((entry["date"].strftime("%a %b %d"), console_calendar.KINDS[entry["kind"]],
                               entry["agency"], entry["request"], entry["basis"]))
            if entry["kind"] in ("statutory_due", "extension_limit"):
                text += " · " + entry["source"] + " · " + entry["label"]
            if entry["kind"] == "meeting":
                text += " · " + entry["label"]
            out.append('<li><a href="/calendar?month=%s">%s</a></li>' % (
                entry["date"].strftime("%Y-%m"), esc(text)))
        out.append('</ul>')
    out.append('</section>')
    out.append(render_waiting(waiting, model["now"].date()))
    out.append('<details id="more"><summary>More detail</summary>')
    out.append(console_standing.render_standing(model, model["now"].date()))
    out.append(console_outstanding.render_outstanding(model))
    out += ["<p>%s: source not connected yet (%s).</p>" % pair for pair in DISCONNECTED]
    out.append('</details>')
    return console_layout.shell("Home", "\n".join(out), "Home")



def serve(handler):
    if urllib.parse.urlsplit(handler.path).path != "/":
        return False
    options = handler.server.options
    try:
        model = load(options["project"], options["run_id"], options.get("run_report_path"),
                     send_config=options.get("send_config"), meetings_dir=getattr(handler.server, "meetings_dir", None))
    except ValueError:
        handler._fail(503)
        return True
    handler._send(200, render_home(model, handler.server.csrf_token))
    return True
