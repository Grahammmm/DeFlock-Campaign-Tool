"""GET /timeline[?agency=KEY&month=YYYY-MM&kind=TYPE&page=N]: every agency's events, newest first (DFCON17-07).

Read-only projection of the pinned run's validated ``outcomes/timeline.json``, read through
``console_board.load`` (live or pinned export alike). Filters are checked against what the timeline
holds (unknown, duplicate or extra parameters are 400), applied before paging at 100 rows. Nothing is
fetched, written, sent or published; record text is limited to the existing event sentence.
"""
import math
import re
import urllib.parse
from datetime import datetime
from zoneinfo import ZoneInfo

from . import console_board as cb, console_layout
from .outcomes import timeline as _tl
from .outcomes.reading import esc

PATHS = ("/timeline",)
NAV = None  # the primary nav owns the Timeline link (console_layout.LINKS)
PAGE_SIZE = 100
PARAMS = ("agency", "month", "kind", "page")
PACIFIC = ZoneInfo("America/Los_Angeles")
_MONTH = re.compile(r"\d{4}-\d{2}")
_PAGE = re.compile(r"[1-9][0-9]{0,5}")
UNAVAILABLE = "Timeline unavailable: no events can be listed, and this is not an empty timeline."


def _day(value):
    """Pacific calendar day of an aware timestamp; a date-only or naive value keeps its own day (no clock invented)."""
    text = value.strip() if isinstance(value, str) else ""
    if len(text) > 10 and _tl._day(text):
        try:
            stamp = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text)
        except ValueError:
            stamp = None
        if stamp is not None and stamp.tzinfo is not None:
            return stamp.astimezone(PACIFIC).date()
    return _tl._day(text)


def _basis(event, day):
    """DATED: the day the event is recorded on. INFERRED: a date somebody stated (promised), not an event day.
    COMPUTED: the register's extension limit, date arithmetic. UNKNOWN: no date held."""
    if day is None:
        return "UNKNOWN"
    if event["type"] == "extension notice" and (event.get("source") or {}).get("kind") == "register":
        return "COMPUTED"
    return "INFERRED" if event["type"] == "promised date" else "DATED"


def rows(model):
    """Every event of every request, newest first; undated last; ties by agency, request, kind, input order."""
    out = []
    for request in cb._requests(model):
        for event in request["events"]:
            day = _day(event.get("date"))
            out.append({"day": day, "agency_key": request.get("agency_key"),
                        "agency": request.get("agency") or request.get("agency_key") or "unknown",
                        "request": request.get("request_id") or "", "kind": event["type"],
                        "summary": event.get("summary") or "", "superseded": bool(event.get("superseded")),
                        "basis": _basis(event, day)})
    order = sorted(range(len(out)), key=lambda i: (out[i]["day"] is None, -(out[i]["day"].toordinal() if out[i]["day"] else 0),
                                                   str(out[i]["agency_key"]), out[i]["request"], out[i]["kind"], i))
    return [out[i] for i in order]


def parse(query, agencies):
    """Validated {'agency','month','kind','page'} from a query string, or None when it must be refused (400).
    A blank filter means no filter; agencies is the timeline's own key list (None: the timeline is unavailable)."""
    try:
        pairs = urllib.parse.parse_qsl(query, keep_blank_values=True, strict_parsing=bool(query), max_num_fields=8)
    except ValueError:
        return None
    names = [name for name, _ in pairs]
    if len(set(names)) != len(names) or not set(names) <= set(PARAMS):
        return None
    got = dict(pairs)
    agency, month, kind, page = (got.get(name) or None for name in PARAMS[:3] + ("page",))
    if "page" in got and not _PAGE.fullmatch(got["page"]):
        return None
    if month is not None and not (_MONTH.fullmatch(month) and 1 <= int(month[5:]) <= 12 and int(month[:4]) >= 1):
        return None
    if kind is not None and kind not in _tl.EVENT_TYPES:
        return None
    if agency is not None and (agencies is None and not (0 < len(agency) <= 256 and agency.isprintable())
                               or agencies is not None and agency not in agencies):
        return None
    return {"agency": agency, "month": month, "kind": kind, "page": int(page or 1)}


def _href(filters, page=None):
    query = {k: v for k, v in filters.items() if k != "page" and v}
    if page and page > 1:
        query["page"] = page
    return "/timeline" + ("?" + urllib.parse.urlencode(query) if query else "")


def _controls(model, filters):
    def select(name, label, options):
        return '<label>%s <select name="%s"><option value="">All</option>%s</select></label>' % (esc(label), name, "".join(
            '<option value="%s"%s>%s</option>' % (esc(v), " selected" if v == filters[name] else "", esc(t)) for v, t in options))
    keys = cb.agencies(model)
    names = {r.get("agency_key"): r.get("agency") or r.get("agency_key") for r in cb._requests(model)}
    return ('<form method="get" action="/timeline" aria-label="Timeline filters">%s%s <label>Month <input type="month" name="month" '
            'value="%s"></label> <button type="submit">Show</button> <a href="/timeline">Clear filters</a></form>' % (
                select("agency", "Agency", [(k, names[k]) for k in keys]),
                select("kind", "Kind", [(k, k) for k in _tl.EVENT_TYPES]), esc(filters["month"] or "")))


def _marks(model, agency):
    """Overdue marks of the requests in scope, each with the 'arithmetic, not a legal finding' wording from the timeline."""
    out = []
    for r in cb._requests(model):
        if agency is None or r.get("agency_key") == agency:
            for m in r["overdue_marks"]:
                text = ("clock unknown: " if m.get("kind") == "unknown" else "OVERDUE (COMPUTED, basis %s): " % (
                    cb.BASIS_WORD.get(m.get("basis"), "UNKNOWN"))) + (m.get("label") or "")
                out.append('<li class="%s">%s %s: %s</li>' % ("muted" if m.get("kind") == "unknown" else "overdue",
                                                              esc(r.get("agency") or r.get("agency_key")), esc(r.get("request_id")), esc(text)))
    return ["<h2>Overdue marks</h2><ul>%s</ul>" % "".join(out)] if out else []


def render(model, filters, page_rows=(), total=0):
    out = [console_layout.start("Timeline: every agency", "Timeline"), "<h1>Timeline: every agency</h1>",
           "<p>Run %s. %s Nothing is fetched, sent or published.</p>" % (esc(model["run_id"]), esc(cb.LABEL))]
    if model["timeline"]["state"] != "ok":
        out.append('<p class="diag" role="status">%s</p>' % esc(UNAVAILABLE + " " + console_layout.unavailable_note(
            "Timeline", model["timeline"].get("code"))))
        return "\n".join(out + [console_layout.END])
    pages = max(1, math.ceil(total / PAGE_SIZE))
    out += ["<p>%s</p>" % esc(cb._legend(model)), _controls(model, filters)] + _marks(model, filters["agency"])
    if not total:
        out.append("<p>%s</p>" % ("No events match these filters." if any(filters[k] for k in PARAMS[:3])
                                  else "No events are recorded in this run's timeline."))
        return "\n".join(out + [console_layout.END])
    out.append('<p role="status">%d events, newest first (dates in Pacific time). Page %d of %d.</p>' % (total, filters["page"], pages))
    out.append("<table><caption>Events across agencies</caption><thead><tr>%s</tr></thead><tbody>" % "".join(
        '<th scope="col">%s</th>' % esc(h) for h in ("Date (Pacific)", "Agency", "Request", "Kind", "What happened", "Basis")))
    for r in page_rows:
        agency = esc(r["agency"])
        if isinstance(r["agency_key"], str) and r["agency_key"]:
            agency = '<a href="/agency?agency=%s">%s</a>' % (esc(urllib.parse.quote(r["agency_key"], safe="")), agency)
        summary = r["summary"] + (" (superseded by a correction)" if r["superseded"] else "")
        out.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
            esc(r["day"].isoformat() if r["day"] else "undated"), agency, esc(r["request"]), esc(r["kind"]), esc(summary), r["basis"]))
    out.append("</tbody></table>")
    links = [(n, t) for n, t in ((filters["page"] - 1, "Previous page"), (filters["page"] + 1, "Next page")) if 1 <= n <= pages]
    out.append('<nav aria-label="Pages">%s</nav>' % " ".join('<a href="%s">%s</a>' % (esc(_href(filters, n)), t) for n, t in links))
    return "\n".join(out + [console_layout.END])


def serve(handler):
    options = handler.options
    try:
        model = cb.load(options["project"], options["run_id"])
    except ValueError:
        handler._fail(503)
        return True
    ok = model["timeline"]["state"] == "ok"
    filters = parse(urllib.parse.urlsplit(handler.path).query, cb.agencies(model) if ok else None)
    if filters is None:
        handler._fail(400)
        return True
    chosen = [r for r in rows(model) if not (
        (filters["agency"] and r["agency_key"] != filters["agency"]) or (filters["kind"] and r["kind"] != filters["kind"])
        or (filters["month"] and not (r["day"] and r["day"].isoformat().startswith(filters["month"]))))] if ok else []
    if filters["page"] > max(1, math.ceil(len(chosen) / PAGE_SIZE)):
        handler._fail(400)
        return True
    start = (filters["page"] - 1) * PAGE_SIZE
    handler._send(200, render(model, filters, chosen[start:start + PAGE_SIZE], len(chosen)))
    return True
