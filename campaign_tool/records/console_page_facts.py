"""GET /facts[?agency=ID]: what the pinned run's records say about each agency (DF1117L-02).

Read-only. Reads only run-outputs/<pinned run>/agency-factsheets.json (outcomes/factsheets.py). Statements are exact page-cited
source quotes, never adjudicated facts. Only a capped excerpt, a count/label/code and a 12-hex document prefix with its page
are shown: never a full hash, unit id, filesystem path or document payload."""
import collections
import os
import re
import urllib.parse

from . import approval_queue as aq, console_export as ce, console_layout, console_snapshot as snap
from .outcomes import factsheets as fs
from .outcomes.reading import esc

PATHS = ("/facts",)
NAV = ("Agency fact sheets", 21)
CAP = 300
BASIS = "NOT FOUND means no supported page-cited statement, not absence of conduct"
LEAD = "Private; source statements, not adjudicated facts; review required"
FOUND, NOT_FOUND = "SOURCE STATEMENT \u2014 REVIEW REQUIRED", "NOT FOUND"
HEX64, TOKEN = re.compile(r"[0-9a-f]{64}"), re.compile(r"[a-z][a-z0-9_]{0,63}")
HIDDEN = re.compile(r"[0-9A-Fa-f]{64,}|(?<![\w/.])(?:~|/)(?:[\w.~-]+/)+[\w.~-]*|(?<!\w)[A-Za-z]:\\\S+|\brun-outputs/\S*")
UNAVAILABLE = {"missing": "The fact sheets have not been written for this run yet.", "unsafe_or_unreadable": "The fact-sheet file cannot be read safely.",
               "oversize": "The fact-sheet file is larger than the console will read.", "invalid_json": "The fact-sheet file is not valid JSON."}


def _unavailable(code):
    return {"state": "unavailable", "code": code}


def _clean(text):
    """Free text with full hashes (so unit ids too) and recognizable filesystem paths replaced; ordinary language is kept."""
    return HIDDEN.sub("[removed]", text)


def _s(v, blank=True):
    if not isinstance(v, str) or not (blank or v.strip()):
        return False
    try:
        v.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _statement_ok(x):
    c = x.get("citation") if isinstance(x, dict) else None
    return (isinstance(c, dict) and _s(x.get("quote")) and isinstance(c.get("document_sha256"), str) and HEX64.fullmatch(c["document_sha256"])
            and type(c.get("page")) is int and c["page"] > 0)


def _field_ok(f):
    st = f.get("statements") if isinstance(f, dict) else None
    return isinstance(st, list) and f.get("status") == (FOUND if st else NOT_FOUND) and all(map(_statement_ok, st))


def _agency_ok(a):
    gaps, fields = a.get("gaps"), a.get("fields")
    return (_s(a.get("agency_id"), False) and len(a["agency_id"]) <= 256 and a["agency_id"].isprintable() and _clean(a["agency_id"]) == a["agency_id"]
            and _s(a.get("agency")) and isinstance(gaps, list)
            and all(isinstance(g, dict) and isinstance(g.get("code"), str) and TOKEN.fullmatch(g["code"]) for g in gaps)
            and isinstance(fields, dict) and set(fields) == set(fs.FIELDS) and all(map(_field_ok, fields.values())))


def load_facts(project, run_id):
    """{'state': 'ok', 'report': doc} for the pinned run, else {'state': 'unavailable', 'code': ...}."""
    root = run = None
    try:
        if not isinstance(run_id, str) or not ce.RUN_ID.fullmatch(run_id) or run_id in (".", ".."):
            return _unavailable("unsafe_or_unreadable")
        root = aq._open_chain(project)
        run = ce._open_chain(root, ["run-outputs", run_id])
        raw = None if run is None else snap._read_file(run, "agency-factsheets.json", fs.MAX_OUTPUT_BYTES)
        if raw is None:
            return _unavailable("missing")
        doc = aq._strict_load(raw, "invalid_json")
        if not isinstance(doc, dict) or doc.get("schema") != fs.SCHEMA:
            return _unavailable("schema_invalid")
        if doc.get("run_id") != run_id:
            return _unavailable("run_mismatch")
        agencies, gaps = doc.get("agencies"), doc.get("gaps")
        if not (_s(doc.get("basis")) and BASIS in doc["basis"] and isinstance(gaps, list) and all(isinstance(g, str) and TOKEN.fullmatch(g) for g in gaps)
                and isinstance(agencies, list) and all(isinstance(a, dict) and _agency_ok(a) for a in agencies)
                and len({a["agency_id"] for a in agencies}) == len(agencies)):
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


def parse_agency(path):
    """(True, agency|None), or (False, None) when the query is malformed, duplicated, blank or has another key."""
    query = path.partition("?")[2]
    try:
        pairs = urllib.parse.parse_qsl(query, keep_blank_values=True, strict_parsing=bool(query), errors="strict")
    except (ValueError, UnicodeError):
        return False, None
    got = dict(pairs)
    if len(got) != len(pairs) or not set(got) <= {"agency"} or any(not v.strip() or v != v.strip() for v in got.values()):
        return False, None
    return True, got.get("agency")


def _head(doc=None):
    basis = esc(_clean(doc["basis"])) if doc else esc(BASIS) + "."
    return "<h1>Agency fact sheets</h1><p><strong>%s</strong></p><p>%s</p>" % (esc(LEAD), basis)


def _cell(a, key):
    count = len(a["fields"][key]["statements"])
    return '<td><a href="/facts?agency=%s">%s</a></td>' % (esc(urllib.parse.quote(a["agency_id"], safe="")), count or "not found")


def _matrix(agencies):
    heads = '<th scope="col">Agency</th>' + "".join('<th scope="col">%s</th>' % esc(label) for label, _ in fs.FIELDS.values())
    rows = ['<tr><th scope="row">%s (%s)</th>%s</tr>' % (esc(_clean(a["agency"])), esc(a["agency_id"]), "".join(_cell(a, k) for k in fs.FIELDS)) for a in agencies]
    return ('<div class="scroll" role="region" tabindex="0" aria-label="Supported statements per agency and field"><table style="min-width:60rem">'
            "<caption>Supported source statements per agency and field</caption><thead><tr>%s</tr></thead><tbody>%s</tbody></table></div>" % (heads, "".join(rows)))


def _detail(a):
    docs = esc("/documents?agency=" + urllib.parse.quote(a["agency_id"], safe=""))
    out = ["<h2>%s</h2>" % esc(_clean(a["agency"])), '<p>Agency id: %s. <a href="/facts">All agencies</a></p>' % esc(a["agency_id"])]
    for key, (label, _) in fs.FIELDS.items():
        items = []
        for s in a["fields"][key]["statements"]:
            text, c = _clean(s["quote"]), s["citation"]
            items.append('<li>%s%s <a href="%s">%s page %d</a></li>' % (esc(text[:CAP]), " (cut)" if len(text) > CAP else "", docs, c["document_sha256"][:12], c["page"]))
        out.append("<h3>%s</h3>%s" % (esc(label), "<ul>%s</ul>" % "".join(items) if items else "<p>not found</p>"))
    gaps = sorted(collections.Counter(g["code"] for g in a["gaps"]).items())
    out.append("<h3>Coverage gaps</h3>" + ("<ul>%s</ul>" % "".join("<li>%s: %d</li>" % (esc(c), n) for c, n in gaps) if gaps else "<p>none recorded</p>"))
    return "\n".join(out)


def render(loaded, agency=None):
    """(status, html). An unknown agency is a 404 with the same heading, never an empty detail."""
    if loaded["state"] != "ok":
        body = _head() + '<p role="status" class="diag">Agency fact sheets: unavailable: %s. %s</p>' % (
            esc(loaded["code"]), esc(UNAVAILABLE.get(loaded["code"]) or console_layout.reason(loaded["code"])))
        return 200, console_layout.shell("Agency fact sheets", body, active=NAV[0])
    doc = loaded["report"]
    body, status = _head(doc), 200
    if doc["gaps"]:
        body += "<p>Run-level coverage gaps: %s.</p>" % esc(", ".join(sorted(set(doc["gaps"]))))
    if agency is None:
        body += _matrix(doc["agencies"]) if doc["agencies"] else "<p>This run's fact sheets list no agencies.</p>"
    else:
        found = next((a for a in doc["agencies"] if a["agency_id"] == agency), None)
        if found:
            body += _detail(found)
        else:
            body, status = body + '<p>No such agency in this run\'s fact sheets. <a href="/facts">All agencies</a></p>', 404
    return status, console_layout.shell("Agency fact sheets", body, active=NAV[0])


def serve(handler):
    if handler.path.partition("?")[0] != "/facts":
        return False
    ok, agency = parse_agency(handler.path)
    if not ok:
        handler._fail(400, "Unknown filter", "The agency filter is not valid. Nothing was changed. Go back and pick a listed agency.")
    else:
        handler._send(*render(load_facts(handler.options["project"], handler.options["run_id"]), agency))
    return True
