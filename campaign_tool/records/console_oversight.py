"""Admin console Screens E (project oversight) and F (public updates) (REC-CON-3).

  records panel  ->  GET  /oversight                  Screen E: Now / Stuck / Needs G
                     GET  /public-updates             Screen F: status lines by state, switch, packets awaiting G
                     POST /public-updates/retract     Undo one posted line, or veto one still holding
                     POST /public-updates/off         turn the auto-post switch OFF (any console user)
                     POST /public-updates/kill        kill switch

Read-only views over files that other units own: the timeline (REC-TL-1), the release-link register
(REC-LINK-1), the run reports and the health watch (REC-HEALTH-1), the approval queue (REC-AUTO-4) and the
public-updates journal (REC-PUB-1). Each source fails alone and is named unavailable. The only writes are the
three Screen F buttons, which call the REC-PUB-1 functions that only ever make things safer (retract, OFF,
kill). Nothing here turns the switch ON, approves, fetches, sends or publishes. Agencies, requests and
campaign are data. Overdue marks and "stuck" are arithmetic on our records, not a legal finding.
"""
import hmac
import json
import os
import re
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import approval_queue, pipeline_health, public_packets
from . import public_updates as pu
from . import console_board as cb
from .outcomes import release_links as _rl
from .outcomes.reading import BANNER, esc
from . import console_layout, console_snapshot

STUCK_HOURS = 72
EXPIRING_DAYS = cb.EXPIRING_DAYS
RUN_WINDOW_HOURS = 24
STATES = ("proposed", "holding", "posted", "retracted")
LABEL = "Private; review required. Stuck and overdue are arithmetic on our records, not a legal finding."
ROUTES = {"/public-updates/retract": ("csrf_token", "id"), "/public-updates/off": ("csrf_token",),
          "/public-updates/kill": ("csrf_token",)}


def _unavailable(code):
    return {"state": "unavailable", "code": code}


# ---- sources -----------------------------------------------------------------------------------------

def _parse_ts(value):
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _target(run_report_path):
    """The run report lives at <runtime>/<target>/run-report.json (the health watch's layout)."""
    if not run_report_path:
        return None
    path = Path(os.path.abspath(run_report_path))
    return path.parent if path.name == "run-report.json" else None


def _load_runs(target_dir, now):
    """Valid reports finished inside the window (newest first) and the latest valid report."""
    if target_dir is None:
        return _unavailable("run_report_not_configured")
    try:
        reports, _bad = pipeline_health._recent_reports(target_dir, pipeline_health.LIMITS, now=now)
        try:
            latest = pipeline_health._read_json(target_dir / "run-report.json")
        except (ValueError, OSError):
            latest = None
        if latest is not None and pipeline_health._valid_report(latest):
            if pipeline_health._future(latest, now):
                return _unavailable("run_report_future")  # never fall back to an older "latest"
            reports.append(latest)
    except (OSError, ValueError, KeyError, TypeError):
        return _unavailable("runs_unreadable")
    seen, runs = set(), []
    for r in reports:
        finished = _parse_ts(r.get("finished_at"))
        if r["run_id"] in seen or finished is None:
            continue
        seen.add(r["run_id"])
        extract = next((s for s in r["stages"] if s["name"] == "extract"), None)
        runs.append({"run_id": r["run_id"], "finished": finished, "status": r["overall_status"],
                     "trigger": r.get("trigger") or r.get("mode"),
                     "items_in": (extract or {})["counts"]["input"] if extract else None,
                     "items_processed": (extract or {})["counts"]["output"] if extract else None,
                     "preserve_pending": sum(o["count"] for o in (extract or {}).get("outcomes", [])
                                             if o.get("code") == "PRESERVE_PENDING") if extract else None})
    runs.sort(key=lambda x: x["finished"], reverse=True)
    window = [x for x in runs if timedelta(0) <= now - x["finished"] <= timedelta(hours=RUN_WINDOW_HOURS)]
    return {"state": "ok", "runs": window, "latest": runs[0] if runs else None}


def _load_health(project, target_dir, now):
    if target_dir is None:
        return _unavailable("health_not_configured")
    try:
        report = pipeline_health.assess(runtime_dir=str(target_dir.parent), target=target_dir.name,
                                        records_root=Path(project) / "processing-v1", now=now)
        return {"state": "ok", "line": pipeline_health.status_line(report), "report": report}
    except (pipeline_health.HealthError, OSError, ValueError, KeyError, TypeError):
        return _unavailable("health_unreadable")


def _load_queue(project, source):
    try:
        return {"state": "ok", "rows": source.queue_rows()}
    except approval_queue.ApprovalQueueError as error:
        return _unavailable(error.code if error.code.startswith("console_model_") else "queue_invalid")
    except OSError:
        return _unavailable("queue_io")


class _Files:
    """The two things the REC-PUB-1 switch check needs from its lock object, without taking the lock."""

    def __init__(self, project):
        self.project = project

    def path(self, name):
        return os.path.join(self.project, name)


def _load_public(project, source=None):
    """Read-only fold of the public-updates journal. A missing journal is an empty one, not an error."""
    if source is not None and source.snapshot:
        return source.public_updates()
    try:
        project = pu._dir(project)
        files = _Files(project)
        raw = pu._read(files.path(pu.JOURNAL), pu.MAX_JOURNAL, b"")
        if raw and not raw.endswith(b"\n"):  # a writer is mid-append: ignore the unfinished tail
            raw = raw[:raw.rfind(b"\n") + 1]
        events = [pu._json(line, "journal_invalid") for line in raw.split(b"\n")[:-1]]
        pu._need(all(isinstance(e, dict) and e.get("schema") == pu.SCHEMA for e in events), "journal_invalid")
        lines, _switch, alerts = pu.fold(events)
        switch = pu._switch_state(files, events)
        return {"state": "ok", "lines": sorted(lines.values(), key=lambda r: (r["proposed_at"], r["id"])),
                "switch": switch, "alerts": [{k: a.get(k) for k in ("code", "agency_key", "at")} for a in alerts]}
    except pu.PublicUpdatesError as error:
        return _unavailable(error.code)
    except (OSError, KeyError, TypeError, ValueError):
        return _unavailable("journal_unreadable")


def load(project, run_id, run_report_path=None, now=None, *, source=None):
    now = now or datetime.now(timezone.utc)
    source = source or console_snapshot.LiveSource(project)
    board = cb.load(project, run_id)
    target = _target(run_report_path)
    return {"run_id": run_id, "now": now, "timeline": board["timeline"], "links": board["links"], "tiers": board["tiers"],
            "runs": _load_runs(target, now), "health": _load_health(project, target, now),
            "queue": _load_queue(project, source), "public": _load_public(project, source), "project": project,
            "source": source}


def sources(model):
    keys = ("timeline", "links", "runs", "health", "queue", "public")
    return {k: ({"state": "ok"} if model[k]["state"] == "ok" else dict(model[k])) for k in keys}


# ---- derived rows ------------------------------------------------------------------------------------

def stuck_requests(model):
    """Open requests where the ball is ours and nothing dated has happened for STUCK_HOURS."""
    today = model["now"].date()
    out = []
    for r in cb._requests(model):
        if r.get("closed") or r["next_action"].get("owner") not in cb.OURS:
            continue
        days = [d for d in (cb._day(e.get("date")) for e in cb._live(r)) if d and d <= today]
        if not days:
            continue
        idle = model["now"] - datetime.combine(max(days), datetime.min.time(), tzinfo=timezone.utc)
        if idle > timedelta(hours=STUCK_HOURS):
            out.append({"agency": r.get("agency") or r.get("agency_key"), "request_id": r.get("request_id"),
                        "stage": r.get("stage"), "idle_days": idle.days, "owner": r["next_action"]["owner"],
                        "action": r["next_action"].get("action")})
    return sorted(out, key=lambda x: (-x["idle_days"], x["agency"] or "", x["request_id"] or ""))


def overdue_requests(model):
    rows = cb.board_rows(model, model["now"].date())
    return [r for r in rows if r["overdue"] and not r["closed"]]


def expiring_links(model):
    out = []
    now = model["now"]
    for link in (model["links"].get("doc") or {}).get("links", []):
        try:
            _, days = _rl.status(link, now, warn_days=EXPIRING_DAYS)
        except (ValueError, KeyError, TypeError):
            continue
        if days is not None and 0 <= days <= EXPIRING_DAYS:
            out.append({"host": link.get("host"), "agencies": link.get("agency_ids") or [],
                        "expiry": (link.get("expiry") or {}).get("date"), "days": days})
    return sorted(out, key=lambda x: (x["days"], x["host"] or ""))


def portal_logins(model):
    """Portals we know links on, by host and platform: a login may be needed there. Nothing is fetched."""
    seen = {}
    for link in (model["links"].get("doc") or {}).get("links", []):
        key = (link.get("host"), link.get("platform"))
        seen.setdefault(key, set()).update(a for a in link.get("agency_ids") or [] if isinstance(a, str))
    return [{"host": h, "platform": p, "agencies": sorted(a)} for (h, p), a in sorted(
        seen.items(), key=lambda kv: (str(kv[0][0]), str(kv[0][1])))]


def pending_approvals(model):
    now = model["now"]
    out = []
    for row in (model["queue"].get("rows") or []):
        if row.get("status") != "pending":
            continue
        made = _parse_ts(row.get("created_at"))
        out.append({"id": row["id"], "kind": row.get("kind"), "agency": row.get("agency_id"),
                    "request": row.get("request_id"),
                    "age_hours": None if made is None else max(0, int((now - made).total_seconds() // 3600))})
    return sorted(out, key=lambda x: (-(x["age_hours"] if x["age_hours"] is not None else -1), x["id"]))


def g_decisions(model):
    return [{"agency": r.get("agency") or r.get("agency_key"), "request_id": r.get("request_id"),
             "action": r["next_action"].get("action")}
            for r in cb._requests(model) if r["next_action"].get("owner") == "G" and not r.get("closed")]


def lines_in(model, state):
    return [r for r in (model["public"].get("lines") or []) if r["state"] == state]


# ---- rendering ---------------------------------------------------------------------------------------

NAV = console_layout.nav()


def _head(title, model):
    notes = ['<p class="diag" role="status">%s</p>' % esc(console_layout.unavailable_note(*console_layout.plain([k]), s.get("code")))
             for k, s in sources(model).items() if s["state"] != "ok"]
    return [console_layout.start(title),
            "<h1>%s</h1>" % esc(title),
            "<p>Run %s. %s Nothing is fetched, sent or published.</p>" % (esc(model["run_id"]), esc(LABEL))] + notes


def _table(caption, headers, rows):
    if not rows:
        return "<p>None.</p>"
    return "<table><caption>%s</caption><thead><tr>%s</tr></thead><tbody>%s</tbody></table>" % (
        esc(caption), "".join('<th scope="col">%s</th>' % esc(h) for h in headers),
        "".join("<tr>%s</tr>" % "".join("<td>%s</td>" % esc(c) for c in row) for row in rows))


def _ago(hours):
    return "unknown" if hours is None else ("%d h" % hours if hours < 48 else "%d days" % (hours // 24))


def render_oversight(model):
    out = _head("Project oversight", model)
    out.append("<h2>Now</h2>")
    runs = model["runs"]
    if runs["state"] != "ok":
        out.append("<p>Runs: unavailable.</p>")
    else:
        out.append(_table("Runs in the last %d hours" % RUN_WINDOW_HOURS, ("Run", "Finished (UTC)", "Trigger", "Status",
                                                                         "Items in", "Items processed"),
                          [(x["run_id"], x["finished"].strftime("%Y-%m-%d %H:%M"), x["trigger"], x["status"],
                            x["items_in"], x["items_processed"]) for x in runs["runs"]]))
    health = model["health"]
    if health["state"] == "ok":
        rep = health["report"]
        out.append('<p role="status">Arrivals waiting: %s%s.</p>' % (
            esc(rep.get("waiting")), esc("; oldest %s min" % rep["oldest_waiting_minutes"]
                                         if rep.get("oldest_waiting_minutes") is not None else "")))
    out.append("<h2>Stuck</h2>")
    if health["state"] == "ok":
        out.append('<p role="status">%s</p>' % esc(health["line"]))
        bad = [c for c in health["report"]["checks"] if c["state"] != "ok"]
        if bad:
            out.append("<ul>%s</ul>" % "".join("<li>%s: %s (%s)</li>" % (esc(c["check"]), esc(c["reason_code"]), esc(c["state"]))
                                                for c in bad))
    else:
        out.append("<p>Pipeline health: unavailable.</p>")
    latest = runs.get("latest") if runs["state"] == "ok" else None
    out.append("<p>PRESERVE_PENDING documents (read only; the coordinator owns them) in the latest run: %s.</p>" % (
        esc("unknown" if not latest or latest["preserve_pending"] is None else latest["preserve_pending"])))
    if model["timeline"]["state"] == "ok":
        out.append(_table("Overdue requests (labelled arithmetic, not a legal finding)", ("Agency", "Request", "Overdue", "Owner"),
                          [(r["agency"], r["request_id"], "; ".join(r["overdue"]), r["owner"]) for r in overdue_requests(model)]))
        out.append(_table("Open requests idle for more than %d hours with the ball in our court" % STUCK_HOURS,
                          ("Agency", "Request", "Stage", "Days idle", "Owner", "Next action"),
                          [(s["agency"], s["request_id"], s["stage"], s["idle_days"], s["owner"], s["action"])
                           for s in stuck_requests(model)]))
    else:
        out.append("<p>Overdue and idle requests: unavailable without a timeline.</p>")
    if model["links"]["state"] == "ok":
        out.append(_table("Release links expiring within %d days (not fetched)" % EXPIRING_DAYS, ("Host", "Agencies", "Expires", "Days left"),
                          [(x["host"], ", ".join(x["agencies"]), x["expiry"], x["days"]) for x in expiring_links(model)]))
    out.append("<h2>Needs G</h2>")
    if model["queue"]["state"] == "ok":
        out.append(_table("Pending approvals, oldest first", ("Draft", "Kind", "Agency", "Request", "Waiting"),
                          [(p["id"][:12], p["kind"], p["agency"], p["request"], _ago(p["age_hours"]))
                           for p in pending_approvals(model)]) + '<p><a href="/next-steps">Open the approval queue</a></p>')
    if model["timeline"]["state"] == "ok":
        out.append(_table("Open G decisions from the timeline", ("Agency", "Request", "Decision needed"),
                          [(d["agency"], d["request_id"], d["action"]) for d in g_decisions(model)]))
    if model["links"]["state"] == "ok":
        out.append(_table("Portals with release links (a login may be needed; nothing is fetched)", ("Host", "Platform", "Agencies"),
                          [(p["host"], p["platform"], ", ".join(p["agencies"])) for p in portal_logins(model)]))
    pub = model["public"]
    if pub["state"] == "ok":
        holding = lines_in(model, "holding")
        out.append('<p role="status">Auto-post switch: <strong>%s</strong>. Lines holding: %d. <a href="/public-updates">Public updates and kill switch</a></p>' % (
            esc(pub["switch"]), len(holding)))
        out.append(_table("Public-update hold queue", ("Line", "Agency", "Text", "Hold ends (UTC)"),
                          [(r["id"], r["agency_key"], r["text"],
                            datetime.fromtimestamp(r["hold_until"], timezone.utc).strftime("%Y-%m-%d %H:%M")) for r in holding]))
    return "\n".join(out + ["</main></body></html>"]) + "\n"


def _form(path, token, label, fields=()):
    if token is None:  # a read-only export renders no form
        return ""
    hidden = "".join('<input type="hidden" name="%s" value="%s">' % (esc(k), esc(v)) for k, v in fields)
    return ('<form method="post" action="%s"><input type="hidden" name="csrf_token" value="%s">%s'
            '<button type="submit">%s</button></form>' % (path, esc(token), hidden, esc(label)))


def render_public(model, token):
    out = _head("Public updates", model)
    pub = model["public"]
    if pub["state"] != "ok":
        return "\n".join(out + ["<p>No journal can be read, so no lines are listed.</p></main></body></html>"]) + "\n"
    out.append('<p role="status">Auto-post switch: <strong>%s</strong> (OFF, ON when signed by G, or KILLED). '
               "It can only be turned ON by G's signature through the command line; this page can only make it safer.</p>" % esc(pub["switch"]))
    out.append(_form("/public-updates/off", token, "Turn auto-post OFF") + _form("/public-updates/kill", token, "Kill switch: stop and retract held lines"))
    for state in STATES:
        rows = lines_in(model, state)
        out.append("<h2>%s (%d)</h2>" % (esc(state.capitalize()), len(rows)))
        if not rows:
            out.append("<p>None.</p>")
            continue
        out.append("<table><thead><tr><th scope=\"col\">Line</th><th scope=\"col\">Agency</th><th scope=\"col\">Text</th>"
                   "<th scope=\"col\">Timing</th><th scope=\"col\">Action</th></tr></thead><tbody>")
        for r in rows:
            if state == "holding":
                when = "hold ends " + datetime.fromtimestamp(r["hold_until"], timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            elif state == "posted":
                when = "posted " + datetime.fromtimestamp(r["posted_at"], timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            elif state == "retracted":
                when = "retracted (%s)" % r.get("retract_reason")
            else:
                when = "proposed"
            action = _form("/public-updates/retract", token, "Undo" if state == "posted" else "Veto",
                           (("id", r["id"]),)) if state in ("proposed", "holding", "posted") else ""
            out.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
                esc(r["id"]), esc(r["agency_key"]), esc(r["text"]), esc(when), action))
        out.append("</tbody></table>")
    refused = [r for r in pub["lines"] if r["state"] == "refused"]
    if refused:
        out.append(_table("Refused lines (cap or check; never queued)", ("Line", "Agency", "Reason"),
                          [(r["id"], r["agency_key"], r.get("refusal")) for r in refused]))
    if pub["alerts"]:
        out.append(_table("Oversight alerts", ("Code", "Agency", "When"), [(a["code"], a["agency_key"], a["at"]) for a in pub["alerts"]]))
    out.append("<h2>Approval packets awaiting G</h2>")
    try:
        packets = (model.get("source") or console_snapshot.LiveSource(model["project"])).packets_pending(
            model["now"].timestamp())
        out.append(_table("Pending update packets", ("Packet", "Kind", "Agency", "Waiting"),
                          [(p["id"][:12], p["kind"], p["agency_key"], _ago(p["age_hours"])) for p in packets])
                   + '<p><a href="/packets">Review packets with G\'s signature</a></p>')
    except (public_packets.PacketError, OSError, ValueError, KeyError, TypeError):
        out.append("<p>Packets: unavailable.</p>")
    return "\n".join(out + ["</main></body></html>"]) + "\n"


# ---- panel seams -------------------------------------------------------------------------------------

def _model(handler):
    options = handler.options
    return load(options["project"], options["run_id"], options.get("run_report_path"), source=options.get("source"))


def serve(handler):
    """Called by admin_panel's GET handler. Returns True when it answered /oversight or /public-updates."""
    path = urllib.parse.urlsplit(handler.path)
    if path.path not in ("/oversight", "/public-updates") or path.query:
        return False
    try:
        model = _model(handler)
    except ValueError:
        handler._fail(503)
        return True
    handler._send(200, render_oversight(model) if path.path == "/oversight" else render_public(model, handler.csrf_token()))
    return True


def _journal_actor(label):
    """Fit any panel-accepted actor label to the journal's stricter actor syntax.

    The panel allows any printable label up to 80 characters (e.g. "Graham (operator)");
    the journal allows [A-Za-z0-9 ._@-], 64 characters. Disallowed characters become "-"
    so the label stays recognisable and Undo/OFF/kill never fail on the label alone.
    """
    text = re.sub(r"[^A-Za-z0-9 ._@-]", "-", str(label)).strip()
    text = re.sub(r"^[^A-Za-z0-9]+", "", text)[:64]
    return text or "console"


def post(handler):
    """Called by admin_panel's POST handler. Returns True when the path is one of ours (answered either way)."""
    if handler.path not in ROUTES:
        return False
    from . import console_origin
    if not console_origin.request_ok(handler, post=True):
        handler._fail(403)
        return True
    fields = handler._body()
    if isinstance(fields, int):
        handler._fail(fields)
        return True
    if set(fields) != set(ROUTES[handler.path]):
        handler._fail(400)
        return True
    if not hmac.compare_digest(fields["csrf_token"].encode("utf-8"), handler.server.csrf_token.encode("ascii")):
        handler._fail(403)
        return True
    project, actor = handler.server.options["project"], _journal_actor(handler.server.actor)
    try:
        if handler.path == "/public-updates/retract":
            if not re.fullmatch(r"[0-9a-f]{16}", fields["id"]):
                handler._fail(400)
                return True
            pu.retract(project, fields["id"], actor)
        elif handler.path == "/public-updates/off":
            pu.turn_off(project, actor)
        else:
            pu.kill(project, actor)
    except pu.PublicUpdatesError as error:
        handler._fail(409 if error.code in ("invalid_transition", "line_unknown") else 400 if error.code in (
            "actor_invalid", "reason_invalid") else 503)
        return True
    except OSError:
        handler._fail(503)
        return True
    handler._send(303, "", location="/public-updates")
    return True
