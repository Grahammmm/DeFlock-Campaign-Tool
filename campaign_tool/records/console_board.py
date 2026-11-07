"""Admin console Screens A and B: the Board across agencies and one agency's timeline (REC-CON-1).

Read-only view over one run's ``outcomes/timeline.json`` (REC-TL-1), with the tier-1 line from the response
items (REC-ITEM-1) and release-link expiry (REC-LINK-1). Each source fails alone and is named unavailable;
nothing is created, fetched, sent, approved or published. Agencies, requests and clocks are data read from
those files; nothing here is specific to one campaign. Overdue marks are the labelled arithmetic from the
timeline, never a legal finding.

  records panel  ->  GET /board[?filter=overdue|waiting|ours|closed]   (Screen A)
                     GET /agency?agency=ID                              (Screen B)
"""
import hashlib
import json
import urllib.parse
from datetime import datetime, timezone

from .. import law
from . import deadline_calc, followup_clock
from . import console_documents as cd
from .outcomes import release_links as _rl
from .outcomes import timeline as _tl
from .outcomes.reading import BANNER, esc
from . import console_layout

FILTERS = {"all": "All", "overdue": "Overdue", "waiting": "Waiting on agency", "ours": "Ball in our court",
           "closed": "Closed"}
OURS = ("G", "coordinator")
CONTACT_SOURCES = ("received_item", "outbox_receipt", "manual")
# Drafts are ours; a promised date is a date the agency stated, not a contact on that day.
NOT_CONTACT = ("draft", "promised date", "hand note")
EXPIRING_DAYS = 14
# CON-HOME-2's clock-basis vocabulary: a recorded receipt is dated, a submission proxy is inferred.
BASIS_WORD = {"recorded_receipt": "DATED", "submission_proxy": "INFERRED"}
LABEL = "Private; review required. Overdue marks are arithmetic on our records, not a legal finding."
COMPUTED_LEGEND = ("COMPUTED = date arithmetic from the law package (Gov. Code § 7922.535), not a legal finding. "
                   "UNVERIFIED = dates could not be verified against the law package and register.")


def _str_or_none(value):
    return value is None or type(value) is str


def _event_ok(e):
    if type(e) is not dict or type(e.get("type")) is not str:
        return False
    if not all(_str_or_none(e.get(k)) for k in ("date", "summary", "draft_status")):
        return False
    if not (e.get("source") is None or type(e.get("source")) is dict):
        return False
    dates = e.get("new_dates")
    return dates is None or (type(dates) is list and all(type(d) is str for d in dates))


def _mark_ok(m):
    return (type(m) is dict and all(_str_or_none(m.get(k)) for k in ("kind", "date", "basis"))
            and type(m.get("label")) is str)


def _request_ok(r):
    if type(r) is not dict or not all(_str_or_none(r.get(k)) for k in (
            "agency_key", "agency", "request_id", "ledger_request_id", "stage")):
        return False
    for key in ("clocks", "extension_chain"):
        if not (r.get(key) is None or type(r.get(key)) is dict):
            return False
    dates = (r.get("extension_chain") or {}).get("new_dates")
    if not (dates is None or (type(dates) is list and all(type(d) is str for d in dates))):
        return False
    if not (r.get("issues") is None or type(r.get("issues")) is list):
        return False
    action = r.get("next_action")
    return (type(r.get("events")) is list and all(_event_ok(e) for e in r["events"])
            and type(r.get("overdue_marks")) is list and all(_mark_ok(m) for m in r["overdue_marks"])
            and type(action) is dict and _str_or_none(action.get("owner")) and _str_or_none(action.get("action")))


def _timeline_ok(doc):
    """The fields Screens A and B consume have the expected types; a bad timeline degrades alone."""
    return type(doc.get("requests")) is list and all(_request_ok(r) for r in doc["requests"])


def load(project, run_id):
    """Timeline plus the sources Screen A summarises. Each key is {'state','doc'|'code'}."""
    run_dir = cd._check_run(project, run_id)
    timeline = cd._read(run_dir / "outcomes" / "timeline.json", _tl.SCHEMA)
    doc = timeline.get("doc")
    if doc is not None and (doc.get("run_id") != run_id or not _timeline_ok(doc)):
        timeline = {"state": "unavailable", "code": "run_mismatch" if doc.get("run_id") != run_id else "shape_invalid"}
    model = cd.load(project, run_id, pages=False)
    return {"run_id": run_id, "timeline": timeline, "links": model["links"], "tiers": model["tiers"],
            "deadlines": computed_dates(project, run_id),
            "received": model["received"], "aliases": model.get("aliases", {"state": "absent"})}


def computed_dates(project, run_id):
    """Read the same run's register through the cited calculator; never infer verification."""
    try:
        register = followup_clock.read_register(project, run_id)
        package = law.load_package("us-ca")
        digest = hashlib.sha256(law.package_path("us-ca").read_bytes()).hexdigest()
        rows = deadline_calc.deadlines(register, package, digest)
        return {(r["agency_key"], r["request_id"]): r for r in rows}
    except (OSError, ValueError, KeyError, TypeError, AttributeError, StopIteration, OverflowError):
        return {}


def computed_clock(model, row, name, value):
    calculated = model.get("deadlines", {}).get((row.get("agency_key"), row.get("request_id")), {})
    entry = calculated.get(name, {})
    basis = entry.get("label", "UNVERIFIED") if entry.get("date") == value else "UNVERIFIED"
    source = ("Gov. Code § 7922.535 via law package " + entry["package"].rsplit(" ", 1)[-1][:12]
              if entry.get("package") else "Gov. Code § 7922.535; law package unavailable")
    received = calculated.get("received", {})
    own = (row.get("clocks") or {}).get("basis")  # no calculator row: keep how the register's own clock was started
    start = ("from recorded receipt" if received.get("note") == "recorded receipt" or (not received and own == "recorded_receipt") else
             "from send date; receipt not recorded" if received.get("date") or (not received and own == "submission_proxy")
             else "receipt/send date not recorded")
    return basis, source, start


def sources(model):
    keys = ("timeline", "links", "tiers", "received") + (("aliases",) if model["aliases"]["state"] != "absent" else ())
    return {k: ({"state": "ok"} if model[k]["state"] == "ok" else dict(model[k])) for k in keys}


def _requests(model):
    return (model["timeline"].get("doc") or {}).get("requests", [])


def _day(value):
    return _tl._day(value)


def _tier1(model, row):
    for req in (model["tiers"].get("doc") or {}).get("requests", []):
        if req.get("agency_key") == row.get("agency_key") and req.get("request_id") in (
                row.get("request_id"), row.get("ledger_request_id")):
            return req["logs_and_audits"]["line"]
    return "not available"


def _live(row):
    return [e for e in row["events"] if not e.get("superseded")]


def _last_contact(row, today):
    days = [_day(e.get("date")) for e in _live(row)
            if (e.get("source") or {}).get("kind") in CONTACT_SOURCES and e.get("type") not in NOT_CONTACT]
    days = [d for d in days if d and d <= today]
    return (today - max(days)).days if days else None


def _promised(row):
    dates = sorted({d for e in _live(row) if e["type"] == "promised date" for d in e.get("new_dates") or [] if d})
    return dates[-1] if dates else None


def _pending(row):
    return sum(1 for e in _live(row) if e["type"] == "draft" and e.get("draft_status") == "pending")


def _received(model, row):
    """Received items for this request, agencies matched through the alias map; None when the list is unavailable."""
    if model.get("received", {}).get("state") != "ok":
        return None
    return sum(1 for i in cd._items(model)
               if cd._key(model, i.get("agency_id")) == row.get("agency_key")
               and i.get("request_id") in (row.get("request_id"), row.get("ledger_request_id")))


def board_rows(model, today):
    rows = []
    for r in _requests(model):
        owner = r["next_action"].get("owner")
        rows.append({
            "agency_key": r.get("agency_key"), "agency": r.get("agency") or r.get("agency_key"),
            "request_id": r.get("request_id"), "tier1": _tier1(model, r), "stage": r.get("stage"),
            "ledger_request_id": r.get("ledger_request_id"), "received": _received(model, r),
            "last_contact_days": _last_contact(r, today), "due": (r.get("clocks") or {}).get("due"),
            "basis": (r.get("clocks") or {}).get("basis"), "promised": _promised(r),
            "overdue": [m["label"] for m in r["overdue_marks"] if m.get("kind") != "unknown"],
            "clock_unknown": any(m.get("kind") == "unknown" for m in r["overdue_marks"]),
            "next_action": r["next_action"].get("action"), "owner": owner, "closed": bool(r.get("closed")),
            "drafts_pending": _pending(r)})
    # needs action first: overdue, then ball in our court, then waiting on agency, then closed
    rows.sort(key=lambda x: (x["closed"], not x["overdue"], x["owner"] not in OURS, x["agency_key"] or "",
                             x["request_id"] or ""))
    return rows


def keep(row, which):
    if which == "overdue":
        return bool(row["overdue"]) and not row["closed"]
    if which == "waiting":
        return row["owner"] == "agency" and not row["closed"]
    if which == "ours":
        return row["owner"] in OURS and not row["closed"]
    if which == "closed":
        return row["closed"]
    return True


def links_expiring(model, today):
    out = 0
    for link in (model["links"].get("doc") or {}).get("links", []):
        try:
            _, days = _rl.status(link, datetime(today.year, today.month, today.day, tzinfo=timezone.utc),
                                 warn_days=EXPIRING_DAYS)
        except (ValueError, KeyError, TypeError):
            continue
        if days is not None and 0 <= days <= EXPIRING_DAYS:
            out += 1
    return out


def counters(model, rows, today):
    return {"requests": len(rows), "overdue": sum(1 for r in rows if r["overdue"] and not r["closed"]),
            "drafts_pending": sum(r["drafts_pending"] for r in rows),
            "links_expiring_14d": links_expiring(model, today) if model["links"]["state"] == "ok" else None}


# ---- Screen B -----------------------------------------------------------------------------------------

def agency_view(model, agency):
    """Requests of one agency with clocks, stage ladder, marks, next action and events oldest first."""
    out = []
    for r in _requests(model):
        if r.get("agency_key") != agency:
            continue
        clocks = r.get("clocks") or {}
        live = _live(r)
        promised = sorted({d for e in live if e["type"] == "promised date" for d in e.get("new_dates") or [] if d})
        asks = []
        for req in (model["tiers"].get("doc") or {}).get("requests", []):
            if req.get("agency_key") == agency and req.get("request_id") in (r.get("request_id"), r.get("ledger_request_id")):
                asks = [{k: a.get(k) for k in ("ask_id", "tier", "tier_name", "state", "text")} for a in req["asks"]]
        out.append({"request_id": r.get("request_id"), "agency": r.get("agency") or agency, "stage": r.get("stage"),
                    "closed": bool(r.get("closed")), "clocks": clocks, "promised": promised,
                    "extension_chain": r.get("extension_chain") or {}, "overdue_marks": r["overdue_marks"],
                    "next_action": r["next_action"], "events": r["events"], "asks": asks,
                    "issues": r.get("issues") or []})
    return out


def agencies(model):
    return sorted({r.get("agency_key") for r in _requests(model) if isinstance(r.get("agency_key"), str)})


# ---- rendering ---------------------------------------------------------------------------------------

def _head(title, model):
    notes = ['<p class="diag" role="status">%s: unavailable (%s).</p>' % (esc(k), esc(s.get("code")))
             for k, s in sources(model).items() if s["state"] != "ok"]
    return [console_layout.start(title, "Agencies"),
            "<h1>%s</h1>" % esc(title), "<p>Run %s. %s Nothing is fetched, sent or published.</p>" % (
                esc(model["run_id"]), esc(LABEL))] + notes


def _documents_href(agency):
    """Screen C accepts only plain ids; tracker keys with spaces or slashes link to its all-agencies page."""
    if type(agency) is str and cd.AGENCY.fullmatch(agency):
        return "/documents?agency=" + urllib.parse.quote(agency, safe="")
    return "/documents"


def _received_cell(r):
    if r["received"] is None:
        return "unavailable"
    if not r["received"]:
        return "0"
    return '<a href="%s">%d files</a>' % (esc(_documents_href(r["agency_key"])), r["received"])


def _ago(days):
    return "no contact recorded" if days is None else ("today" if days == 0 else "%d days ago" % days)


def render_board(model, today, which="all"):
    out = _head("Board: every request, every agency", model)
    if model["timeline"]["state"] != "ok":
        return "\n".join(out + ["<p>No timeline for this run, so no requests can be listed.</p></main></body></html>"]) + "\n"
    rows = board_rows(model, today)
    c = counters(model, rows, today)
    out.append('<p role="status">Requests: %d. Overdue: %d. Drafts pending: %d. Links expiring within %d days: %s.</p>' % (
        c["requests"], c["overdue"], c["drafts_pending"], EXPIRING_DAYS,
        "unavailable" if c["links_expiring_14d"] is None else c["links_expiring_14d"]))
    out.append('<nav aria-label="Filters">%s</nav>' % " ".join(
        ("<strong>%s</strong>" % esc(t)) if k == which else '<a href="/board?filter=%s">%s</a>' % (k, esc(t))
        for k, t in FILTERS.items()))
    shown = [r for r in rows if keep(r, which)]
    if not shown:
        out.append("<p>No requests match this filter.</p>")
    else:
        head = ("Agency", "Request", "Tier 1 (logs / audits)", "Stage", "Received", "Last contact", "Statutory due", "Promised",
                "Overdue", "Next action", "Owner")
        out.append("<table><caption>Requests, needs action first</caption><thead><tr>%s</tr></thead><tbody>" % "".join(
            '<th scope="col">%s</th>' % esc(h) for h in head))
        for r in shown:
            overdue = "; ".join(r["overdue"]) or ("clock unknown" if r["clock_unknown"] else "")
            cells = ['<a href="/agency?agency=%s">%s</a>' % (esc(urllib.parse.quote(r["agency_key"] or "", safe="")),
                                                               esc(r["agency"])),
                     esc(r["request_id"]), esc(r["tier1"]), esc(r["stage"]), _received_cell(r),
                     esc(_ago(r["last_contact_days"])),
                     esc("%s (%s)" % (r["due"], BASIS_WORD.get(r["basis"], "UNKNOWN")) if r["due"] else "unknown"),
                     esc(r["promised"] or ""), esc(overdue), esc(r["next_action"]), esc(r["owner"])]
            out.append("<tr>%s</tr>" % "".join("<td>%s</td>" % c for c in cells))
        out.append("</tbody></table>")
    return "\n".join(out + ["</main></body></html>"]) + "\n"


def render_agency(model, agency):
    views = agency_view(model, agency)
    out = _head("Agency timeline: %s" % (views[0]["agency"] if views else agency), model)
    out.append("<p>%s</p>" % esc(COMPUTED_LEGEND))
    docs_href = _documents_href(agency)
    out.append('<p><a href="%s">Documents and findings</a> <a href="/next-steps">Drafts</a></p>' % esc(docs_href))
    if model["timeline"]["state"] != "ok":
        out.append("<p>No timeline for this run.</p>")
    elif not views:
        out.append("<p>No requests for this agency in this run.</p>")
    for v in views:
        c = v["clocks"]
        out.append("<section aria-label=\"%s\"><h2>Request %s</h2>" % (esc(v["request_id"]), esc(v["request_id"])))
        out.append("<h3>Clocks (basis: %s)</h3><ol><li>Received to determination date: %s</li>"
                   "<li>Extension date (from the notice, else the recorded extension): %s</li><li>Promised date(s): %s</li><li>Current statutory due: %s</li></ol>" % (
                       esc(c.get("basis") or "unknown"), esc(c.get("base_due") or "unknown"),
                       esc(", ".join(v["extension_chain"].get("new_dates") or []) or c.get("extended_due") or "none recorded"), esc(", ".join(v["promised"]) or "none"),
                       esc(c.get("due") or "unknown")))
        for name, key, title in (("determination_due", "base_due", "Determination due"),
                                 ("extension_limit", "extended_due", "Extension limit")):
            basis, source, start = computed_clock(model, {**v, "agency_key": agency}, name, c.get(key))
            out.append("<p>%s: %s (%s; %s). %s.</p>" % (
                title, esc(c.get(key) or "unknown"), basis, esc(start), esc(source)))
        calculated = model.get("deadlines", {}).get((agency, v["request_id"]), {}).get("determination_due", {})
        digest = calculated.get("package", "unavailable").rsplit(" ", 1)[-1][:12]
        out.append("<p>Dates computed from Gov. Code § 7922.535 (law package %s); provisional, not a legal finding.</p>" % esc(digest))
        for m in v["overdue_marks"]:
            if m.get("kind") == "unknown":
                out.append('<p class="clock-unknown">Clock unknown: %s</p>' % esc(m.get("label") or "no statutory date is recorded"))
                continue
            out.append('<p class="overdue"><strong>OVERDUE</strong> %s: %s%s</p>' % (
                esc(m.get("kind")), esc(m.get("label")), esc(" (%s, basis %s)" % (m["date"], m.get("basis")) if m.get("date") else "")))
        chain = v["extension_chain"]
        if chain.get("exceeds_14_days"):
            out.append('<p class="overdue"><strong>%s</strong></p>' % esc(chain.get("label") or _tl.EXTENSION_LABEL))
        ladder = " &rarr; ".join(("<strong>%s</strong>" % esc(s)) if s == v["stage"] else esc(s) for s in _tl.STAGES)
        out.append("<h3>Stage</h3><p>%s%s</p>" % (ladder, " (closed)" if v["closed"] else ""))
        out.append("<h3>Next action</h3><p>%s: %s</p>" % (esc(v["next_action"].get("owner")), esc(v["next_action"].get("action"))))
        if v["asks"]:
            out.append("<h3>Asked items</h3><ul>%s</ul>" % "".join(
                "<li>tier %s %s &mdash; %s: %s</li>" % (esc(a["tier"] or "-"), esc(a["tier_name"] or ""), esc(a["state"]),
                                                       esc((a["text"] or "")[:200])) for a in v["asks"]))
        out.append('<p><a href="/manual-event?%s">Add manual event</a></p>' % esc(urllib.parse.urlencode({"agency": agency, "request": v["request_id"]})))
        out.append("<h3>Events, oldest first</h3>")
        if not v["events"]:
            out.append("<p>None recorded.</p>")
        else:
            out.append("<table><caption>Events</caption><thead><tr><th scope=\"col\">Date</th><th scope=\"col\">Type</th>"
                       "<th scope=\"col\">Summary</th><th scope=\"col\">Source</th></tr></thead><tbody>")
            for e in v["events"]:
                summary = e.get("summary") or ""
                if e.get("superseded"):
                    summary += " (superseded by a correction)"
                out.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
                    esc(e.get("date") or "undated"), esc(e["type"]), esc(summary), _source(e.get("source") or {}, docs_href)))
            out.append("</tbody></table>")
        out.append("</section>")
    return "\n".join(out + ["</main></body></html>"]) + "\n"


def _source(src, docs_href):
    kind = src.get("kind")
    if kind == "outbox_receipt" and src.get("message_id"):
        mid = str(src["message_id"])
        return 'message <a href="mid:%s">%s</a>' % (esc(urllib.parse.quote(mid.strip("<>"), safe="@")), esc(mid))
    if kind == "received_item":
        return 'received item <a href="%s">%s</a>' % (esc(docs_href), esc(src.get("item_id")))
    if kind == "manual":
        return "manual entry %s by %s" % (esc(src.get("event_id")), esc(src.get("actor")))
    if kind == "approval_journal":
        return "approval journal %s" % esc(src.get("draft_id"))
    if kind == "register":
        return "tracker row %s" % esc(src.get("request"))
    return esc(json.dumps(src, sort_keys=True))


# ---- panel seam --------------------------------------------------------------------------------------

def serve(handler):
    """Called by admin_panel's GET handler. Returns True when it answered /board or /agency."""
    parts = urllib.parse.urlsplit(handler.path)
    if parts.path not in ("/board", "/agency"):
        return False
    query = urllib.parse.parse_qs(parts.query)
    which = query.get("filter", ["all"])[0]
    agency = query.get("agency", [None])[0]
    # Agency keys are data (tracker names may hold spaces and slashes): bounded here, then matched exactly
    # against the timeline's own agencies below, so an unknown key is a 404 and never reaches a path or query.
    if which not in FILTERS or (parts.path == "/agency" and (
            agency is None or not 0 < len(agency) <= 256 or not agency.isprintable())):
        handler._fail(400)
        return True
    options = handler.server.options
    try:
        model = load(options["project"], options["run_id"])
    except ValueError:
        handler._fail(503)
        return True
    if parts.path == "/agency" and model["timeline"]["state"] == "ok" and agency not in agencies(model):
        handler._fail(404)
        return True
    today = datetime.now(timezone.utc).date()
    handler._send(200, render_board(model, today, which) if parts.path == "/board" else render_agency(model, agency))
    return True
