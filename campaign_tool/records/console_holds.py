"""Read-only console view of the documents the pipeline is holding (DF2222-07).

``records console-export`` writes ``console-model/holds.json`` inside the container from the existing ``records holds``
listing (``holds.build``, routes on, mail history at ``PROJECT/mail-history``): per outcome and fixed detail code the count,
and per item only the 12-hex prefix, outcome, detail code, attempt count and preservation route. No path, file name, text
or full hash is kept. ``GET /holds`` shows it; the live console computes the same rows. Nothing here releases, retries or
writes: the attended ``records holds release`` and the coordinator own the documents. An export without the file
reads ``holds: unavailable``, never an empty list.
"""
import json
import re
import urllib.parse
from collections import Counter
from pathlib import Path

from . import console_layout, holds, native_extract as ne, preserve_routes
from .extraction_failures import fixed_code
from .outcomes.reading import esc

NAME = "holds.json"
PREFIX = re.compile(r"[0-9a-f]{12}")
HELD = tuple(code for code in ne.OUTCOMES if code != ne.EXTRACTED)
PRESERVE_ATOMS = frozenset("preserve_" + s for s in ("pending", "in_progress", "done", "blocked", "inapplicable", "missing"))
LABEL = "read only; the coordinator owns these documents"
KEYS = ("sha256_prefix", "code", "detail_code", "attempts", "route")
RETRY, WAIT = "retry with records holds release (attended)", "wait for the next run"
SCOPE = "G scope decision"
STEPS = {ne.AWAITING_OCR: "review OCR readiness", ne.PARSER_MISSING: "operator checks parser availability",
         ne.DEFERRED: WAIT}
ROUTE_STEPS = {"R1": "coordinator reviews mail-copy adoption", "R2": "coordinator reviews legacy source",
               "R2s": "prepare approved supplement source", "R3": "custodian trusted validation required",
               "R4": SCOPE, "R5": SCOPE, "unavailable": "route evidence unavailable"}


class HoldsError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _detail(raw, preserve):
    """Sorted distinct fixed atoms of a (comma-separated) detail code; anything else is ``unlisted``."""
    atoms = set()
    for atom in raw.split(",") if isinstance(raw, str) else ():
        atoms.add(atom if (preserve and atom in PRESERVE_ATOMS) or atom == "accept_status_rejected"
                  else fixed_code(atom))
    return ",".join(sorted(atoms)) or "unlisted"


def _clean(item):
    """The exact five-field row of a listing item; ValueError on bad identity, outcome or attempt data."""
    prefix, code, attempts = item.get("sha256_prefix"), item.get("code"), item.get("attempts")
    if not (isinstance(prefix, str) and PREFIX.fullmatch(prefix) and code in HELD and type(attempts) is int
            and attempts >= 0):
        raise ValueError("holds_item_invalid")
    preserve = code == ne.PRESERVE_PENDING
    route = item.get("route")
    return {"sha256_prefix": prefix, "code": code, "detail_code": _detail(item.get("detail_code"), preserve),
            "attempts": attempts, "route": (route if route in preserve_routes.ROUTES else "unavailable") if preserve
            else None}


def _tally(items):
    counts, details = Counter(i["code"] for i in items), {}
    for i in items:
        details.setdefault(i["code"], Counter()).update(i["detail_code"].split(","))
    return ({c: counts[c] for c in HELD if c in counts}, {c: dict(sorted(details[c].items())) for c in HELD if c in counts})


def _body(items):
    order = {code: n for n, code in enumerate(HELD)}
    items = sorted(items, key=lambda i: (order[i["code"]], i["sha256_prefix"], i["detail_code"], i["attempts"],
                                         i["route"] or ""))
    counts, details = _tally(items)
    return {"state": "ok", "counts": counts, "detail_counts": details, "items": items}


def model(project):
    """The console-model body for the live project (read-only), or {"state": "unavailable", "code": ...}."""
    try:
        doc = holds.build(project, routes=True, mail_history=Path(project) / "mail-history")
        return _body([_clean(i) for i in doc["items"]])
    except holds.Unreadable:
        return {"state": "unavailable", "code": "holds_unreadable"}
    except (ValueError, KeyError, TypeError):
        return {"state": "unavailable", "code": "holds_invalid"}


def _dump(value):
    return json.dumps(value, sort_keys=True)


def validate(doc):
    """The pinned export's ok body (state, counts, detail_counts, items) or HoldsError; shares ``_clean`` and ``_tally``."""
    items = doc.get("items") if type(doc) is dict else None
    if type(items) is not list or set(doc) != {"schema", "source_project_id", "state", "counts", "detail_counts", "items"}:
        raise HoldsError("console_model_invalid")
    try:
        if any(type(i) is not dict or set(i) != set(KEYS) or _clean(i) != i for i in items):
            raise ValueError
        want = _body(items)
    except (ValueError, KeyError, TypeError):
        raise HoldsError("console_model_invalid") from None
    if want["items"] != items or any(_dump(doc[k]) != _dump(want[k]) for k in ("counts", "detail_counts")):
        raise HoldsError("console_model_invalid")  # canonical JSON: an exact int, never True or 1.0, and totals that match the rows
    return want


def step(item):
    """The fixed next-step wording (guidance only, not clearance)."""
    code = item["code"]
    if code == ne.PRESERVE_PENDING:
        return ROUTE_STEPS[item["route"]]
    if code in ne.RETRYABLE:
        return RETRY if item["attempts"] >= ne.MAX_ATTEMPTS else WAIT
    return STEPS.get(code, "review required")


def _table(title, head, rows):
    cells = "".join("<tr>%s</tr>" % "".join("<td>%s</td>" % esc(c) for c in row) for row in rows)
    return ('<h2>%s</h2><div class="scroll"><table><thead><tr>%s</tr></thead><tbody>%s</tbody></table></div>'
            % (esc(title), "".join('<th scope="col">%s</th>' % esc(h) for h in head), cells))


def render(doc):
    out = ["<h1>Held documents</h1>", "<p>%s. Counts are the listing captured at export time (the current listing on a "
           "live console); the release itself is an attended step outside this console.</p>" % esc(LABEL)]
    if doc.get("state") != "ok":
        out.append('<p role="status">holds: unavailable</p>')
    elif not doc["items"]:
        out.append('<p role="status">No held documents.</p>')
    else:
        out.append(_table("Totals", ("Outcome", "Held", "Detail codes"), [
            (c, n, ", ".join("%s %d" % kv for kv in doc["detail_counts"][c].items())) for c, n in doc["counts"].items()]))
        for code in doc["counts"]:
            out.append(_table(code, ("Document", "Detail code", "Attempts", "Route", "Next step"),
                              [(i["sha256_prefix"], i["detail_code"], i["attempts"], i["route"] or "-", step(i))
                               for i in doc["items"] if i["code"] == code]))
    out.append('<p><a href="/documents">Documents and findings</a> <a href="/oversight">Oversight</a></p>')
    return console_layout.shell("Held documents", "\n".join(out))


def serve(handler):
    """GET /holds (no query). True when the path is ours; there is no POST route."""
    parts = urllib.parse.urlsplit(handler.path)
    if parts.path != "/holds":
        return False
    if parts.query:
        handler._fail(400)
        return True
    handler._send(200, render(handler.options["source"].holds()))
    return True
