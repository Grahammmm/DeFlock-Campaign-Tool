"""Read-only console Home. Decisions remain on their existing signed action pages."""
import urllib.parse
from datetime import datetime, timezone

from . import console_board as cb, console_oversight as co, mail_send, public_packets
from .outcomes.reading import BANNER, esc

GROUPS = ("request", "follow-up", "send", "site-update", "social-post", "status-line", "packet")
LABELS = ("Requests", "Follow-ups", "Sends", "Site updates", "Social posts", "Status lines", "Packets")
DISCONNECTED = (("agency-page", "CON-SITEPR-1"), ("redaction", "CON-REDACT-1"), ("finding", "CON-FIND-1"))
NEXT = {"next-site-update": ("site-update", "site"), "next-social-post": ("social-post", "social"),
        "next-followup-probe": ("follow-up", "followup")}


def load(project, run_id, run_report_path=None, now=None, *, send_config=None):
    model = co.load(project, run_id, run_report_path, now)
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
                     "what": "%s %s" % (kind, request or "agency-wide"), "why": why, "age_hours": age,
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
            group, anchor = NEXT.get(kind, ("request" if kind == "first-request" else "follow-up", None))
            link = "/next-steps#" + anchor if anchor else "/dashboard#drafts"
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

    def urgency(row):
        overdue, hours, age = row["overdue_days"], row["hours_left"], row["age_hours"]
        closing = hours is not None and hours <= (12 if row["source"] == "public" else 48)
        band = 0 if overdue is not None else 1 if closing else 2
        return (band, -(overdue or 0), hours if hours is not None else float("inf"),
                -(age if age is not None else -1), GROUPS.index(row["kind_group"]), row["id"])

    return sorted(rows, key=urgency)


def render_home(model, token):
    rows = decision_rows(model, model["now"].date())
    out = ['<!doctype html><html lang="en"><head><meta charset="utf-8">',
           '<meta name="robots" content="noindex,nofollow"><title>Home</title></head><body>',
           '<p class="notice">%s</p>' % esc(BANNER), co.NAV, "<h1>Home</h1>",
           '<h2>Needs your decision</h2><p role="status">' + " · ".join(
               "%s %d" % (label, sum(r["kind_group"] == group for r in rows))
               for group, label in zip(GROUPS, LABELS)) + "</p>"]
    for source in ("timeline", "queue", "public", "packets", "sends"):
        state = model[source]
        if state["state"] != "ok":
            out.append('<p role="status">%s: unavailable (%s).</p>' % (esc(source), esc(state.get("code"))))
    if model["packets"]["state"] == "ok" and not model["packets"]["rows"]:
        out.append("<p>No packets pending.</p>")
    out.append('<table><thead><tr>' + "".join('<th scope="col">%s</th>' % title for title in
               ("Agency", "What is being approved", "Why", "Age", "Overdue", "Open")) + '</tr></thead><tbody>')
    for row in rows:
        overdue = ""
        if row["overdue_days"] is not None:
            overdue = "overdue %d days since %s (%s), on our records; not a legal finding" % (
                row["overdue_days"], row["overdue_date"],
                "promised" if row["overdue_kind"] == "promised_date_passed" else "statutory")
        why = str(row["why"])
        if row["hours_left"] is not None:
            why += "; window closes in %d h" % row["hours_left"]
        agency = '<a href="/agency?agency=%s">%s</a>' % (
            esc(urllib.parse.quote(row["agency"] or "", safe="")), esc(row["agency"]))
        cells = [agency, esc(row["what"]), esc(why), esc(co._ago(row["age_hours"])), esc(overdue),
                 '<a href="%s">Open</a>' % esc(row["link"])]
        out.append("<tr>%s</tr>" % "".join("<td>%s</td>" % cell for cell in cells))
    out.append("</tbody></table>")
    out += ["<p>%s: source not connected yet (%s).</p>" % pair for pair in DISCONNECTED]
    out += ['<h2 id="standing">Where each agency stands</h2><p>Coming.</p>',
            '<h2 id="outstanding">Outstanding information</h2><p>Coming.</p>', '</body></html>']
    return "\n".join(out) + "\n"


def serve(handler):
    if urllib.parse.urlsplit(handler.path).path != "/":
        return False
    options = handler.server.options
    try:
        model = load(options["project"], options["run_id"], options.get("run_report_path"),
                     send_config=options.get("send_config"))
    except ValueError:
        handler._fail(503)
        return True
    handler._send(200, render_home(model, handler.server.csrf_token))
    return True
