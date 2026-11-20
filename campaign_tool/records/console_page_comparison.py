"""GET /comparison[?agency=ID&status=S]: the pinned run's documentary policy comparison (DF1117L-01).

Read-only. Reads only run-outputs/<pinned run>/comparison.json, written by outcomes/comparison.py. Every row is a
documentary screening that needs attorney review, never a legal conclusion; an unreadable file is a diagnostic, never an
empty table."""
import os
import re
import urllib.parse

from . import approval_queue as aq, console_export as ce, console_layout, console_snapshot as snap
from .outcomes import comparison as cmp
from .outcomes.reading import esc
from .outcomes.response_items import MAX_OUTPUT_BYTES

PATHS = ("/comparison",)
NAV = ("Policy comparison", 20)
STATUSES = ("meets", "gap", "unknown")
NO_INPUT = "No reviewed comparison input is installed; every row is unknown"
REVIEW = "needs attorney review; not a legal conclusion"
LEGINFO = re.compile(re.escape(cmp.BASE_URL) + r"\?lawCode=(?:CIV|GOV)&sectionNum=[0-9]+(?:\.[0-9]+)*\.")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
FIELDS = {"cpra_response_deadline": "CPRA response deadline", "usage_policy_published": "Usage policy published",
          "authorized_users_training": "Authorized users and training", "access_logs": "Access logs"}
REASONS = {
    "missing_or_unqualified_evidence": "No qualified cited evidence was supplied for this row.",
    "missing_redacted_or_unresolved_fact": "The cited fact is missing, redacted or unresolved.",
    "practice_scope_policy_or_exceptions_unresolved": "The policy version, wording strength, native-record coverage or exceptions are unresolved.",
    "configured_access_is_not_actual_disclosure": "The cited event is configured access, not an actual disclosure.",
    "retention_bounds_or_data_layer_unresolved": "The retention bounds or data layer are unresolved.",
    "retention_data_layer_unresolved": "The retention data layer is unresolved.",
    "incomplete_or_contradictory_evidence": "The evidence is incomplete or contradicted.",
    "documentary_criteria_match": "The supplied cited documents match every documentary criterion.",
    "documentary_criterion_discrepancy": "The supplied cited documents show a discrepancy from at least one criterion.",
    "supplied_policy_shall_match": "Observed practice matches the supplied policy's required (shall) bounds.",
    "supplied_policy_shall_discrepancy": "Observed practice differs from the supplied policy's required (shall) bounds.",
    "supplied_policy_should_match": "Observed practice matches the supplied policy's recommended (should) bounds.",
    "supplied_policy_should_discrepancy": "Observed practice differs from the supplied policy's recommended (should) bounds.",
    "documentary_timing_match": "The documented determination date is within the computed due date.",
    "documentary_timing_discrepancy": "The documented timing differs from the computed due date.",
    "determination_still_pending": "The determination is still pending and not yet past the computed due date.",
    "receipt_calendar_extension_or_determination_unresolved": "Receipt, calendar, extension or determination facts are unresolved.",
    "unmapped_fact_sheet_field": "The fact sheet has a field with no documentary comparison mapping.",
}
UNAVAILABLE = {"missing": "The comparison has not been written for this run yet.", "unsafe_or_unreadable": "The comparison file cannot be read safely.",
               "oversize": "The comparison file is larger than the console will read.", "invalid_json": "The comparison file is not valid JSON."}


def input_status_text(status):
    """Plain words (unescaped) for the artifact's input_status; never implies the input was approved."""
    if status == "fact_sheets_missing":
        return NO_INPUT
    if status == "fact_sheets_supplied":
        return "Fact sheets were supplied to this run; their contents are not reviewed or approved here."
    return "Input status %s is not recognised; nothing here implies the input was reviewed." % status


def _unavailable(code):
    return {"state": "unavailable", "code": code}


def _s(v, blank=True):
    if not isinstance(v, str) or not (blank or v.strip()):
        return False
    try:
        v.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _clean(node):
    if isinstance(node, str):
        return _s(node)
    if isinstance(node, dict):
        return all(_s(k) and _clean(v) for k, v in node.items())
    return all(map(_clean, node)) if isinstance(node, list) else True


def _row_ok(r):
    c, e = r.get("citation"), r.get("evidence")
    return (_s(r.get("agency_id"), False) and _s(r.get("field"), False) and r.get("status") in STATUSES
            and _s(r.get("expected")) and _s(r.get("reason")) and (r.get("request_id") is None or _s(r["request_id"]))
            and isinstance(c, dict) and _s(c.get("label")) and _s(c.get("url")) and _clean(r.get("observed"))
            and isinstance(e, list) and all(isinstance(x, dict) and _s(x.get("source_id")) and _s(x.get("locator")) for x in e))


def load_comparison(project, run_id):
    """{'state': 'ok', 'report': doc} for the pinned run, else {'state': 'unavailable', 'code': ...}."""
    root = run = None
    try:
        if not isinstance(run_id, str) or not ce.RUN_ID.fullmatch(run_id) or run_id in (".", ".."):
            return _unavailable("unsafe_or_unreadable")
        root = aq._open_chain(project)
        run = ce._open_chain(root, ["run-outputs", run_id])
        raw = None if run is None else snap._read_file(run, "comparison.json", MAX_OUTPUT_BYTES)
        if raw is None:
            return _unavailable("missing")
        doc = aq._strict_load(raw, "invalid_json")
        if not isinstance(doc, dict) or doc.get("schema") != cmp.SCHEMA:
            return _unavailable("schema_invalid")
        if doc.get("run_id") != run_id:
            return _unavailable("run_mismatch")
        gaps, rows = doc.get("inventory_gaps"), doc.get("rows")
        if not (_s(doc.get("disclaimer")) and _s(doc.get("input_status")) and _s(doc.get("as_of")) and DATE.fullmatch(doc["as_of"])
                and isinstance(gaps, list) and all(_s(g) for g in gaps) and isinstance(rows, list)
                and all(isinstance(r, dict) and _row_ok(r) for r in rows)):
            return _unavailable("shape_invalid")
        return {"state": "ok", "report": doc}
    except (snap.SnapshotError, aq.ApprovalQueueError) as error:
        return _unavailable(error.code if error.code in ("oversize", "invalid_json") else
                            "oversize" if error.code == "export_oversize" else "unsafe_or_unreadable")
    except (OSError, ce.ExportError):
        return _unavailable("unsafe_or_unreadable")
    except RecursionError:
        return _unavailable("shape_invalid")
    finally:
        for fd in (root, run):
            if fd is not None:
                os.close(fd)


def counts(rows):
    """{agency_id: {status: n}} over all given rows (every status key present)."""
    out = {}
    for r in rows:
        out.setdefault(r["agency_id"], dict.fromkeys(STATUSES, 0))[r["status"]] += 1
    return out


def reason_text(code):
    return REASONS.get(code) or "%s (no description yet)" % code


def parse_filters(path):
    """(agency|None, status|None) from the query, or None when it is malformed, duplicated, blank or unknown."""
    query = path.partition("?")[2]
    try:
        pairs = urllib.parse.parse_qsl(query, keep_blank_values=True, strict_parsing=bool(query), errors="strict")
    except (ValueError, UnicodeError):
        return None
    got = dict(pairs)
    if len(got) != len(pairs) or not set(got) <= {"agency", "status"} or any(not v.strip() or v != v.strip() for v in got.values()):
        return None
    status = got.get("status")
    return None if status is not None and status not in STATUSES else (got.get("agency"), status)


def _citation(c):
    label = esc(c["label"])
    return '<a href="%s" rel="noreferrer">%s</a>' % (esc(c["url"]), label) if LEGINFO.fullmatch(c["url"]) else label


def _observed(value):
    if not isinstance(value, dict):
        return "none" if value is None else "not shown (not key/value pairs)"
    pairs = ["%s: %s" % (esc(k), esc(v)) for k, v in value.items() if v is None or isinstance(v, (str, int, float, bool))]
    return "<br>".join(pairs) or "none"


def _tr(r):
    field = FIELDS.get(r["field"]) or r["field"].replace("_", " ").capitalize()
    if r["field"] == "cpra_response_deadline":
        field += " (request %s)" % (r["request_id"] or "none recorded")
    evidence = "<br>".join("<code>%s</code> %s" % (esc(e["source_id"]), esc(e["locator"])) for e in r["evidence"]) or "not supplied"
    cells = (esc(field), esc(r["status"]), esc(r["expected"]), _observed(r.get("observed")), esc(reason_text(r["reason"])),
             _citation(r["citation"]), evidence, REVIEW)
    return "<tr>%s</tr>" % "".join("<td>%s</td>" % c for c in cells)


def _table(label, caption, heads, rows):
    return ('<div class="scroll" role="region" tabindex="0" aria-label="%s"><table style="min-width:60rem"><caption>%s</caption><thead><tr>%s</tr></thead>'
            "<tbody>%s</tbody></table></div>" % (esc(label), esc(caption), "".join('<th scope="col">%s</th>' % h for h in heads), "".join(rows)))


def render(loaded, agency=None, status=None):
    body = ["<h1>Policy comparison</h1>"]
    if loaded["state"] != "ok":
        body.append('<p role="status" class="diag">Policy comparison: unavailable: %s. %s</p>' % (
            esc(loaded["code"]), esc(UNAVAILABLE.get(loaded["code"]) or console_layout.reason(loaded["code"]))))
        return console_layout.shell("Policy comparison", "\n".join(body), active=NAV[0])
    doc = loaded["report"]
    body += ["<p>%s</p>" % esc(doc["disclaimer"]), '<p role="status">Input: %s</p>' % esc(input_status_text(doc["input_status"])),
             "<p>Missing inventory tables: %s. As of %s.</p>" % (esc(", ".join(doc["inventory_gaps"]) or "none"), esc(doc["as_of"]))]
    every = counts(doc["rows"])
    shown = [a for a in sorted(every) if agency in (None, a)]
    if not every:
        body.append("<p>This comparison lists no agencies or requests.</p>")
    else:
        link = lambda a: '<a href="/comparison?agency=%s">%s</a>' % (esc(urllib.parse.quote(a, safe="")), esc(a))
        body.append(_table("Counts per agency", "Rows per status", ("Agency",) + STATUSES,
                           ["<tr><td>%s</td>%s</tr>" % (link(a), "".join("<td>%d</td>" % every[a][s] for s in STATUSES)) for a in shown]))
        keep = [("agency", agency)] if agency else []
        links = ['<a href="%s">%s</a>' % (esc("/comparison?" + urllib.parse.urlencode(keep + [("status", s)])), s) for s in STATUSES]
        body.append('<p>Show only: %s <a href="%s">all statuses</a></p>' % (" ".join(links), esc("/comparison" + ("?" + urllib.parse.urlencode(keep) if keep else ""))))
    for a in shown:
        rows = [r for r in doc["rows"] if r["agency_id"] == a and status in (None, r["status"])]
        body.append("<h2>%s</h2>" % esc(a))
        body.append(_table("Rows for " + a, "%d rows" % len(rows), ("Field or request", "Status", "Expected", "Observed", "Reason", "Citation", "Evidence", "Review"),
                           map(_tr, rows)) if rows else "<p>No rows with status %s for this agency.</p>" % esc(status))
    return console_layout.shell("Policy comparison", "\n".join(body), active=NAV[0])


def serve(handler):
    if handler.path.partition("?")[0] != "/comparison":
        return False
    filters = parse_filters(handler.path)
    loaded = None if filters is None else load_comparison(handler.options["project"], handler.options["run_id"])
    if filters is None or (loaded["state"] == "ok" and filters[0] is not None
                           and filters[0] not in {r["agency_id"] for r in loaded["report"]["rows"]}):
        handler._fail(400, "Unknown filter", "The agency or status filter is not one this comparison has. Nothing was changed. Go back and pick a listed one.")
    else:
        handler._send(200, render(loaded, *filters))
    return True
