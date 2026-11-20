"""GET /changes: what is new in the selected run since the previous one (DF1117L-14).

Read-only. The comparison is DF1117L-13's outcomes.changes.diff (no second implementation). Hosted: the pinned export against
the newest older installed export under DIR/exports, chosen by install name. Live: the selected run against the previous
settled run-outputs directory. Identities only (ids, codes, 12-hex prefixes); never record text. A section the diff cannot
read says "unavailable", never zero.
"""
import os
import urllib.parse
from datetime import datetime, timezone

from . import approval_queue as aq, console_export as ce, console_layout, console_snapshot as snap
from .console_data_age import _pacific
from .outcomes import analysis_ready, changes
from .outcomes.reading import esc

PATHS = ("/changes",)
NAV = ("Changes", 40)
SCAN_CAP = 2000
FIRST = "first export: nothing to compare"
FIRST_LIVE = "first run: nothing to compare"
SECTIONS = (("facts", "Statements"), ("comparison", "Policy comparison"), ("register", "Requests"),
            ("received", "Received items"), ("proposals", "Gap proposals"))
DIAG = {"history_unavailable": "The installed exports could not be listed safely.",
        "previous_export_invalid": "The nearest older export is not valid, so nothing is compared.",
        "run_not_chronological": "This run's id or install time does not say when it started, so no earlier run can be named.",
        "run_unavailable": "The selected run folder cannot be read.",
        "run_unsettled": "The selected run has not finished writing its outputs yet.",
        "runs_unavailable": "The run folders could not be listed safely.",
        "run_directory_unavailable": "A run folder of the pair cannot be read.",
        "run_order_invalid": "The two runs are not in start order."}


class _Unavailable(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _install(name):
    try:
        return datetime.strptime(name[:16], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _names(root_fd, parts):
    """Entry names of one directory below root_fd (no-follow); more than SCAN_CAP entries is a failure."""
    fd = ce._open_chain(root_fd, parts)
    if fd is None:
        raise _Unavailable("history_unavailable")
    try:
        names = []
        with os.scandir(fd) as scan:
            for entry in scan:
                if len(names) >= SCAN_CAP:
                    raise _Unavailable("history_unavailable")
                names.append((entry.name, entry.is_dir(follow_symlinks=False)))
        return names
    finally:
        os.close(fd)


def _stamp(value):
    try:
        moment = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return "unknown export time"
    return "exported " + _pacific(moment)


def _hosted(handler):
    pin, pinner = handler.pin, handler.server.pinner
    installed, started = _install(pin.name), changes.run_start(pin.run_id)
    if installed is None or started is None:
        raise _Unavailable("run_not_chronological")
    root_fd = None
    try:
        root_fd = aq._open_chain(pinner.root)
        names = _names(root_fd, ["exports"])
    except (OSError, aq.ApprovalQueueError, ce.ExportError):
        raise _Unavailable("history_unavailable") from None
    finally:
        if root_fd is not None:
            os.close(root_fd)
    eligible = []
    for name, _ in names:
        if ce.NAME.fullmatch(name):
            when, begun = _install(name), changes.run_start(name[17:])
            if when is not None and begun is not None and when < installed and begun < started:
                eligible.append((when, name))
    if not eligible:
        return None
    try:
        before = pinner.historical(max(eligible)[1])
    except (snap.SnapshotError, ce.ExportError, aq.ApprovalQueueError, OSError, ValueError, KeyError, TypeError, AttributeError):
        raise _Unavailable("previous_export_invalid") from None
    if before.source_project_id != pin.source_project_id:
        raise _Unavailable("previous_export_invalid")
    sides = tuple((os.path.join(p.path, "run-outputs", p.run_id), p.run_id, _stamp(p.exported_at)) for p in (before, pin))
    return sides


def _settled(root_fd, run):
    fd = ce._open_chain(root_fd, ["run-outputs", run, "outcomes"])
    if fd is None:
        return False
    try:
        raw = snap._read_file(fd, "analysis-ready.json", analysis_ready.MAX_BYTES)
        doc = None if raw is None else aq._strict_load(raw, "invalid")
    finally:
        os.close(fd)
    return type(doc) is dict and doc.get("schema") == analysis_ready.SCHEMA and doc.get("run_id") == run


def _live(handler):
    project, run = handler.options.get("project"), handler.options.get("run_id")
    started = changes.run_start(run)
    if started is None or not project:
        raise _Unavailable("run_not_chronological")
    project, root_fd = os.path.abspath(project), None
    try:
        root_fd = aq._open_chain(project)
        found = {name: changes.run_start(name) for name, is_dir in _names(root_fd, ["run-outputs"])
                 if is_dir and ce.RUN_ID.fullmatch(name)}
        found = {name: begun for name, begun in found.items() if begun is not None}
        if run not in found:
            raise _Unavailable("run_unavailable")
        if (started, run) == max((begun, name) for name, begun in found.items()) and not _settled(root_fd, run):
            raise _Unavailable("run_unsettled")
    except _Unavailable as error:
        raise _Unavailable("runs_unavailable" if error.code == "history_unavailable" else error.code) from None
    except (OSError, snap.SnapshotError, aq.ApprovalQueueError, ce.ExportError):
        raise _Unavailable("runs_unavailable") from None
    finally:
        if root_fd is not None:
            os.close(root_fd)
    older = [(begun, name) for name, begun in found.items() if begun < started]
    if not older:
        return None
    before = max(older)[1]
    return tuple((os.path.join(project, "run-outputs", name), name, "started " + _pacific(found[name]) + "; live run, not exported")
                 for name in (before, run))


def _link(path, agency, text):
    return '<a href="%s?agency=%s">%s</a>' % (path, esc(urllib.parse.quote(agency, safe="")), esc(text))


def _who(path, agency):
    return "unassigned" if agency is None else _link(path, agency, agency) if path else esc(agency)


def _ba(row):
    return " ".join("%s: %s" % (k, "-" if v is None else v) for k, v in row.items())


def _rows(name, kind, row):
    """(html) of one change row; links only go to this run's own pages, never for a removed source."""
    agency, linked = row.get("agency_id"), kind != "removed"
    if name == "facts":
        what = "%s, document %s page %s" % (row["field"], row["document_prefix"], row["page"])
        return "%s: %s" % (_who("/facts" if linked else None, agency), esc(what))
    if name == "comparison":
        what = "%s%s: %s (%s) now %s (%s)" % (row["field"], "" if row["request_id"] is None else " request " + row["request_id"],
                                              row["before"]["status"], row["before"]["reason"], row["after"]["status"], row["after"]["reason"])
        return "%s: %s" % (_who("/comparison", agency), esc(what))
    if name == "register":
        return "%s: %s" % (_who("/agency", agency), esc("request %s: %s now %s" % (row["request_id"], _ba(row["before"]), _ba(row["after"]))))
    if name == "received":
        return "%s: %s" % (_who(None, agency), esc("item " + row["item_prefix"]))
    return "%s: %s" % (_who(None, agency), esc("request %s proposal %s" % (row["request_id"], row["proposal_prefix"])))


COUNTS = (("facts", "added", "New statements"), ("facts", "removed", "Statements no longer present"),
          ("comparison", "changed", "Comparison rows changed"), ("register", "changed", "Requests changed"),
          ("received", "added", "New received items"), ("proposals", "added", "Gap proposals added"),
          ("proposals", "removed", "Gap proposals removed"))


def render_pair(result, sides):
    """The page body for one admitted pair; sides = ((dir, run_id, time text) older, newer)."""
    (_, old_id, old_time), (_, new_id, new_time) = sides
    out = ["<h1>What changed</h1><p>Read-only. Private; identities only, compared by run id. A difference is a lead to check, not a finding.</p>",
           "<p>Current run %s (%s). Previous run %s (%s).</p>" % (esc(new_id), esc(new_time), esc(old_id), esc(old_time)),
           "<h2>Counts</h2><ul>"]
    for name, kind, label in COUNTS:
        section = result[name]
        out.append("<li>%s: %s</li>" % (esc(label), esc(section) if isinstance(section, str) else len(section[kind])))
    out.append("</ul>")
    for name, label in SECTIONS:
        section = result[name]
        out.append("<h2>%s</h2>" % esc(label))
        if isinstance(section, str):
            out.append('<p role="status" class="diag">%s</p>' % esc(section))
            continue
        # current-side additions and transitions first, then what only the previous run had
        items = [(kind, row) for kind in ("added", "changed", "removed") for row in section.get(kind, ())]
        out.append("<ul>%s</ul>" % "".join("<li>%s %s</li>" % (
            {"added": "New", "changed": "Changed", "removed": "Previous run only"}[kind], _rows(name, kind, row)) for kind, row in items)
            if items else "<p>no changes</p>")
    return "".join(out)


def build(handler):
    """(status, body html) for the page; never raises."""
    head = "<h1>What changed</h1>"
    try:
        sides = _hosted(handler) if handler.pin is not None else _live(handler)
        if sides is None:
            return 200, head + '<p role="status">%s.</p>' % esc(FIRST if handler.pin is not None else FIRST_LIVE)
        try:
            result = changes.diff(sides[0][0], sides[1][0])
        except changes.ChangesError as error:
            return 200, head + '<p role="status" class="diag">Changes: unavailable: %s. %s</p>' % (
                esc(error.code), esc(DIAG.get(error.code, "The two runs could not be compared.")))
        return 200, render_pair(result, sides)
    except _Unavailable as error:
        return 200, head + '<p role="status" class="diag">Changes: unavailable: %s. %s</p>' % (esc(error.code), esc(DIAG.get(error.code, "")))


def serve(handler):
    if handler.path != "/changes":
        return False
    status, body = build(handler)
    handler._send(status, console_layout.shell("What changed", body, active=NAV[0]))
    return True
