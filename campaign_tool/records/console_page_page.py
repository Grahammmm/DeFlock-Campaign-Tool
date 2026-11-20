"""Private page viewer: one cited page's checked source text at GET /page?doc=<12 hex>&p=N (DF2307-08)."""
import re
from urllib.parse import parse_qsl

from . import console_documents as cd, console_layout
from .console_page_document import FULL_HASH, PREFIX, _label, _received

PATHS = ("/page",)
NAV = None
BANNER = "Private; source text, review required; not for publication"
PAGE = re.compile(r"[1-9][0-9]{0,8}")
SHORT = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _.:-]{0,59}")
PATH_LIKE = re.compile(r"(?<![\w/.<])(?:~|\.{1,2}|[A-Za-z]:)?(?:[/\\][\w.~-]+){2,}[/\\]?|[A-Za-z]:\\\S*")
OMITTED = "[omitted]"
ROUTES = {"native": "Native", "ocr": "OCR"}
BANDS = {"below_80": "Below 80", "at_least_80": "80 or above", "not_recorded": "Not recorded", "not_applicable": "Not applicable"}
REVIEWS = {"pending": "Awaiting review", "confirmed": "Confirmed", "rejected": "Rejected; not ready", "not_applicable": "Not applicable"}
REASONS = {"page_bytes": "the page is larger than the export's per-page limit",
           "page_count": "the export reached its page-count limit",
           "total_bytes": "the export reached its total size limit"}
KINDS = {"fact": "Fact statement", "gap": "Gap proposal", "deadline": "Deadline"}


def _clean(text):
    """Source text with full digests and file paths replaced by a marker (before escaping); everything else is kept."""
    return PATH_LIKE.sub(OMITTED, FULL_HASH.sub(OMITTED, text))


def _query(path):
    try:
        found = parse_qsl(path.partition("?")[2], keep_blank_values=True, strict_parsing=True, max_num_fields=2)
        values = dict(found)
        if len(found) != 2 or set(values) != {"doc", "p"} or not PREFIX.fullmatch(values["doc"]) or not PAGE.fullmatch(values["p"]):
            raise ValueError
        return values["doc"], int(values["p"])
    except ValueError:
        return None


def _render(prefix, page, row, omitted):
    esc = console_layout.esc
    out = [f"<h1>{BANNER}</h1>", f"<h2>Document {prefix}, page {page}</h2>", '<p><a href="/documents">Back to documents</a></p>']
    if row is None:
        out.append(f"<p>This page's text is not in the console export: {REASONS[omitted]}.</p>")
    else:
        meta = (("Agency", _label(row["agency"])), ("Received (Pacific)", _received(row["received_at"])),
                ("Text route", ROUTES[row["route"]]), ("OCR confidence", BANDS[row["confidence_band"]]),
                ("Review state", REVIEWS[row["review_state"]]))
        out.append("<dl>" + "".join(f"<dt>{name}</dt><dd>{esc(value)}</dd>" for name, value in meta) + "</dl>")
        out.append("<h3>Page text</h3><pre>%s</pre>" % esc(_clean(row["text"])))
        out.append("<h3>Cited by</h3><ul>" + "".join(
            "<li>%s: %s</li>" % (KINDS[c["kind"]], esc(_label(c["label"], SHORT))) for c in row["citations"]) + "</ul>")
    return console_layout.shell("Cited page", "\n".join(out), active="Library")


def serve(handler):
    query = _query(handler.path)
    if query is None:
        handler._fail(400, "Use one 12-character lowercase hex document and a page number")
        return True
    prefix, page = query
    options = handler.options
    try:
        model = options["source"].cited_pages(options["run_id"])
        received = cd.load(options["project"], options["run_id"], pages=False)
    except (ValueError, OSError, KeyError, AttributeError):
        model = received = None
    if not model or model.get("state") != "ok" or received is None or "items" not in (received["received"].get("doc") or {}):
        handler._fail(503, "Cited pages unavailable for this run")
        return True
    digests = {r["original_sha256"] for r in model["pages"] + model["not_exported"]}
    digests |= {i["original_sha256"] for i in cd._items(received) if isinstance(i.get("original_sha256"), str)}
    matches = {d for d in digests if d.startswith(prefix)}
    if len(matches) > 1:
        handler._fail(409, "This ID matches more than one document")
        return True
    digest = next(iter(matches), None)
    row = next((r for r in model["pages"] if r["original_sha256"] == digest and r["page"] == page), None)
    skipped = next((r for r in model["not_exported"] if r["original_sha256"] == digest and r["page"] == page), None)
    if row is None and skipped is None:
        handler._fail(404, "Page not found among the cited pages")
    else:
        handler._send(200, _render(prefix, page, row, skipped and skipped["reason"]))
    return True
