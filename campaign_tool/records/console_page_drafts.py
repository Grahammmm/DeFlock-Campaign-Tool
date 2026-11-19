"""GET /drafts: every approval-queue item the console queue reader returns, not only those awaiting a decision.

Read-only metadata view: state, kind, agency, request, times (Pacific), revision and a /review link. It never
shows a body, evidence, actor, reason, approval record or pending-apply overlay, and has no decision control.
Rows carry whatever status the reader returns; superseded/sent appear only if a compatible snapshot supplies them.
"""
import urllib.parse
from zoneinfo import ZoneInfo

from . import approval_queue, console_layout, draft_templates, mail_send
from .outcomes.reading import esc

PATHS = ("/drafts",)
NAV = ("Draft history", 30)
STATES = ("pending", "approved", "rejected", "superseded", "sent")  # the last two: reserved, shown only when supplied
KINDS = frozenset(draft_templates.KINDS) | {draft_templates.MESSAGE_LINK}
_TEXT = ("id", "status", "kind", "agency_id", "request_id")


# console_oversight and console_home build the nav at import, which loads this registry: import them lazily.
def _time(value):
    """(epoch or None, Pacific display text)."""
    from . import console_oversight as co
    moment = co._parse_ts(value)
    if moment is None:
        return None, "unknown"
    return moment.timestamp(), moment.astimezone(ZoneInfo(mail_send.TZ)).strftime("%Y-%m-%d %H:%M %Z")


def _project(rows):
    from .console_home import PLAIN_KINDS
    if not isinstance(rows, list):
        raise TypeError("queue rows")
    out = []
    for row in rows:
        if not isinstance(row, dict) or not all(isinstance(row.get(k), str) for k in _TEXT) or type(row.get("revision")) is not int:
            raise TypeError("queue row")
        _, made = _time(row.get("created_at"))
        changed_at, changed = _time(row.get("updated_at"))
        out.append({"id": row["id"], "state": row["status"], "kind": row["kind"], "what": PLAIN_KINDS.get(row["kind"], row["kind"]),
                    "agency": row["agency_id"], "request": row["request_id"], "revision": row["revision"],
                    "created": made, "changed": changed, "changed_at": changed_at})
    return sorted(out, key=lambda r: (r["changed_at"] is None, -(r["changed_at"] or 0), r["id"]))


def _queue(options):
    """(projected rows, None) or (None, unavailable code), through the same reader and codes as Home."""
    from . import console_oversight as co
    try:
        state = co._load_queue(options["project"], options["source"])
        if state["state"] != "ok":
            return None, state.get("code")
        return _project(state["rows"]), None
    except (approval_queue.ApprovalQueueError, OSError, ValueError, KeyError, TypeError, AttributeError):
        return None, "queue_invalid"


def _filters(query):
    """{name: value} for at most one each of state, agency, kind; None when the query is refused."""
    args = urllib.parse.parse_qs(query, keep_blank_values=True)
    if not set(args) <= {"state", "agency", "kind"} or any(len(v) != 1 for v in args.values()):
        return None
    found = {k: v[0] for k, v in args.items()}
    return found if found.get("state", STATES[0]) in STATES else None


def _unknown(rows, found):
    """True when an agency or kind filter names nothing the queue knows (a kind may also be any queue contract kind)."""
    return (("agency" in found and found["agency"] not in {r["agency"] for r in rows})
            or ("kind" in found and found["kind"] not in KINDS | {r["kind"] for r in rows}))


def _link(**query):
    return esc("/drafts?" + urllib.parse.urlencode(query))


def render(rows, code, found):
    out = ["<h1>Draft history</h1>", '<section id="drafts">']
    if rows is None:
        out.append('<p role="status">%s</p>' % esc(console_layout.unavailable_note("Drafts", code)))
        return console_layout.shell("Draft history", "\n".join(out + ["</section>"]), "Draft history")
    seen = {}
    for row in rows:
        seen[row["state"]] = seen.get(row["state"], 0) + 1
    counts = [(s, seen.get(s, 0)) for s in STATES] + sorted((s, n) for s, n in seen.items() if s not in STATES)
    out.append("<p>All %d: %s</p>" % (len(rows), " · ".join('<a href="%s">%s %d</a>' % (_link(state=s), esc(s), n) for s, n in counts)))
    shown = [r for r in rows if all(r[k] == found[k] for k in ("state", "agency", "kind") if k in found)]
    if found:
        out.append('<p>Showing %d matching %s. <a href="/drafts">Show all</a></p>' % (
            len(shown), esc(", ".join("%s=%s" % pair for pair in found.items()))))
    if not shown:
        out.append("<p>No drafts match this filter.</p>" if rows else "<p>No drafts in the approval queue.</p>")
    else:
        out.append('<table><thead><tr>' + "".join('<th scope="col">%s</th>' % h for h in (
            "State", "What", "Agency", "Request", "Created (Pacific)", "Last changed (Pacific)", "Revision", "Open")) + "</tr></thead><tbody>")
        for r in shown:
            cells = [esc(r["state"]), esc(r["what"]), esc(r["agency"]), esc(r["request"]), esc(r["created"]), esc(r["changed"]),
                     str(r["revision"]), '<a href="%s">Review</a>' % esc("/review?id=" + urllib.parse.quote(r["id"], safe=""))]
            out.append("<tr>%s</tr>" % "".join("<td>%s</td>" % c for c in cells))
        out.append("</tbody></table>")
    return console_layout.shell("Draft history", "\n".join(out + ["</section>"]), "Draft history")


def serve(handler):
    found = _filters(urllib.parse.urlsplit(handler.path).query)
    rows, code = (None, None) if found is None else _queue(handler.options)
    if found is None or (rows is not None and _unknown(rows, found)):
        handler._fail(400)
        return True
    handler._send(200, render(rows, code, found))
    return True
