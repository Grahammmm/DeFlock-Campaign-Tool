"""Private, metadata-only document drill-down over the request's pinned inputs."""
import re
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl
from zoneinfo import ZoneInfo

from . import console_documents as cd, console_layout, console_ocr, finding_review, ocr_review
from .extraction_routes import OCR_METHOD, VISION_METHOD
from .outcomes import received_items as ri
from .run_stages import LedgerBusy, snapshot_ro

PATHS = ("/document",)
NAV = None
PREFIX = re.compile(r"[0-9a-f]{12}")
PACIFIC = ZoneInfo("America/Los_Angeles")
IDENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_:-]{0,127}")
TOPIC = re.compile(r"[A-Za-z0-9][A-Za-z0-9 &'(),_-]{0,119}")
FULL_HASH = re.compile(r"[0-9a-f]{64}", re.I)
OCR_METHODS = (OCR_METHOD, VISION_METHOD, "ocrmypdf", "tesseract")
NATIVE_METHODS = ("legacy-intake:pdf", "pypdf")
OCR_REVIEW = {"pending": "Awaiting review", "confirmed": "Confirmed", "rejected": "Rejected; not ready"}
FINDING_STATES = {"unreviewed": "Unreviewed", "approved": "Approved", "rejected": "Rejected", "deferred": "Deferred"}
TEXT_ROUTES = {"awaiting_ocr": "Awaiting OCR", "waiting": "Awaiting extraction", "could_not_be_read": "Read error",
               "ocr_unreviewed": "OCR — awaiting visual review", "ocr_flagged": "OCR — usable with a review flag"}


def _label(value, pattern=IDENT):
    # Admit only display identifiers/titles, not arbitrary paths, URLs, filenames or embedded digests.
    return value if isinstance(value, str) and pattern.fullmatch(value) and not FULL_HASH.search(value) else "Unknown"


def _received(value):
    try:
        stamp = datetime.fromisoformat(value)
        if stamp.tzinfo is not None:
            return stamp.astimezone(PACIFIC).strftime("%Y-%m-%d %H:%M %Z")
    except (TypeError, ValueError, OverflowError):
        pass
    return "Unknown"


def _ledger_pages(project, digest):
    """Use only page metadata, never the current extraction's evidence files."""
    path = Path(project) / "processing-v1" / "ledger.sqlite"
    try:
        if not path.is_symlink() and not path.parent.is_symlink():
            with snapshot_ro(path) as db:
                return db.execute("SELECT page_no, method, status, reason, needs_visual_review, confidence, derivative_sha256 "
                                  "FROM page_state WHERE original_sha256=? ORDER BY page_no", (digest,)).fetchall()
    except (OSError, sqlite3.Error, LedgerBusy):
        pass
    return []


def _ocr_text(method, status, reason):
    return method in OCR_METHODS and status == "partial" and reason != "ocr_skipped_machine_text"


def _live_ocr_rows(project, digest, pages):
    # LiveSource.ocr_pages() reads page text to build the review screen; this route must not call it.
    rows = [{"original_sha256": digest, "page_no": page, "confidence": confidence, "derivative_sha256": derivative}
            for page, method, status, reason, flagged, confidence, derivative in pages
            if _ocr_text(method, status, reason) and flagged == 1]
    if rows:
        done = {ocr_review.binding(r): r["decision"]
                for r in ocr_review.read_reviews(Path(project) / "ocr-review-v1" / "reviews.jsonl")
                if r["original_sha256"] == digest}
        for row in rows:
            row["state"] = done.get(ocr_review.binding(row), "pending")
    return {"rows": rows, "truncated": False}


def _ocr(options, digest, pages, state):
    source = options["source"]
    try:
        if source.snapshot:
            # Use the frozen review projection, including its truncation; do not fill gaps from live data.
            doc = source.ocr_pages()
        elif pages:
            doc = _live_ocr_rows(options["project"], digest, pages)
        else:
            return None, ""
    except (console_ocr.OcrPagesError, OSError, ValueError, TypeError, KeyError):
        return None, ""
    methods = {row[0]: row[1:4] for row in pages}
    flagged = {}
    for row in doc["rows"]:
        if not isinstance(row, dict) or row.get("original_sha256") != digest:
            continue
        n, confidence, review = row.get("page_no"), row.get("confidence"), row.get("state")
        if (type(n) is not int or not 1 <= n <= 100000 or not isinstance(review, str) or review not in OCR_REVIEW
                or "confidence" not in row or confidence is not None
                and (type(confidence) not in (int, float) or not 0 <= confidence <= 100)):
            continue
        if n in methods and not _ocr_text(*methods[n]):
            continue  # page_state also flags native and failed pages
        if n not in methods and confidence is None and state not in ("ocr_unreviewed", "ocr_flagged"):
            continue  # without a known method or confidence, the model alone is not OCR evidence
        band = "Not recorded" if confidence is None else ("Below 80" if ocr_review.bucket(confidence) == "hold" else "80 or above")
        flagged[n] = (n, band, OCR_REVIEW[review])
    note = "Only part of the OCR review queue is in this export." if doc["truncated"] else ""
    return [flagged[n] for n in sorted(flagged)], note


def _asks(model, items):
    tiers = model["tiers"].get("doc")
    if tiers is None:
        return None
    bindings = {}
    for item in items:
        if item.get("agency_id") and item.get("request_id") and item.get("item_id"):
            key = (cd._key(model, item["agency_id"]), item["request_id"])
            bindings.setdefault(key, set()).add(item["item_id"])
    rows = set()
    for req in tiers["requests"]:
        ids = bindings.get((cd._key(model, req.get("agency_key")), req.get("request_id")))
        if ids:
            rows.update((_label(req.get("request_id")), _label(ask["ask_id"]), ask["state"]) for ask in req["asks"]
                        if any(isinstance(ev, dict) and ev.get("item_id") in ids for ev in ask.get("evidence") or []))
    return sorted(rows)


def _expiries(model, digest, items):
    register = model["links"].get("doc")
    if register is None:
        return None
    ids = {i["item_id"] for i in items if i.get("item_id")}

    def cites(source):
        original = source.get("original_sha256")
        item = source.get("item_id")
        return original == digest or (original is None and isinstance(item, str) and item in ids)

    rows = set()
    for link in register.get("links", []):
        sources = link.get("sources", [])
        if not any(map(cites, sources)):
            continue
        expiry = link.get("expiry") or {}
        # A shared link's merged date belongs to the source cited in expiry, not to every notice.
        if not (cites(expiry) or (expiry.get("original_sha256") is None and expiry.get("item_id") is None
                                 and all(map(cites, sources)))):
            continue
        try:
            day = date.fromisoformat(expiry.get("date", "") or "").isoformat()
        except (TypeError, ValueError):
            continue
        status, _ = cd._status({"expiry": {"date": day}}, datetime.now(timezone.utc))
        rows.add((day, status.replace("_", " ").capitalize()))
    return sorted(rows)


def _findings(root, digest, items):
    try:
        agencies = sorted({i["agency_id"] for i in items if _label(i.get("agency_id")) != "Unknown"})
        return [(_label(agency), _label(row.get("topic"), TOPIC), FINDING_STATES.get(row["review_state"], "Unknown"))
                for agency in agencies for row in finding_review.list_findings(root, agency=agency)
                if row.get("agency") == agency and any(f.get("source_sha256") == digest for f in row.get("facts", []))]
    except (finding_review.FindingReviewError, OSError, ValueError, KeyError, TypeError):
        return None


def _section(title, columns, rows, empty):
    return (f"<h2>{title}</h2>" + (cd._table(columns, rows, title) if rows else
                                    f"<p>{title} unavailable.</p>" if rows is None else f"<p>{empty}</p>"))


def _render(options, model, digest, items, analysis):
    pages = _ledger_pages(options["project"], digest)
    state = next((r.get("state") for r in (model["textstate"].get("doc") or {}).get("records", [])
                  if r.get("sha256") == digest), None)
    ocr, note = _ocr(options, digest, pages, state)
    route = TEXT_ROUTES.get(state or "")
    if route is None:
        if ocr or any(_ocr_text(method, status, reason) for _, method, status, reason, *_ in pages):
            route = "OCR"
        elif state == "text_reviewed" and pages and all(method in NATIVE_METHODS and status == "ok"
                                                       for _, method, status, *_ in pages):
            route = "Native"
        else:
            route = "Unknown"
    metadata = dict.fromkeys((_label(i.get("agency_id")), _label(i.get("request_id")), _received(i.get("received_at")),
                              i["kind"] if i.get("kind") in ri.KINDS else "Unknown") for i in items)
    out = [f"<h1>Document {digest[:12]}</h1><p>{cd.LABEL}</p>", '<p><a href="/documents">Back to documents</a></p>']
    for fields in metadata:
        out.append("<dl>" + "".join(f"<dt>{name}</dt><dd>{console_layout.esc(value)}</dd>"
                                    for name, value in zip(("Agency", "Request", "Received (Pacific)", "Kind"), fields)) + "</dl>")
    out.append(f'<dl><dt>Pages</dt><dd>{len(pages) or "Unknown"}</dd><dt>Text route</dt><dd>{route}</dd></dl>')
    out.append(_section("Flagged OCR pages", ("Page", "OCR confidence", "Review"), ocr, "No flagged OCR pages shown."))
    if note:
        out.append(f"<p>{note}</p>")
    out.append('<p><a href="/ocr-review">Open OCR review</a></p>')
    out.append(_section("Linked asked items", ("Request", "Asked item", "Disposition"), _asks(model, items), "No asked items matched."))
    out.append(_section("Release-link expiry", ("Expiry", "Status"), _expiries(model, digest, items), "No attributable expiry recorded."))
    if analysis is not None:
        out.append(_section("Citing findings", ("Agency", "Topic", "Review state"), _findings(analysis, digest, items),
                            "No findings cite this document."))
    return console_layout.shell("Document details", "\n".join(out), active="Library")


def serve(handler):
    try:
        [(key, prefix)] = parse_qsl(handler.path.partition("?")[2], keep_blank_values=True,
                                    strict_parsing=True, max_num_fields=1)
        if key != "id" or not PREFIX.fullmatch(prefix):
            raise ValueError
    except ValueError:
        handler._fail(400, "Use one 12-character lowercase hex document ID")
        return True
    options = handler.options
    try:
        model = cd.load(options["project"], options["run_id"], pages=False)
    except ValueError:
        model = None
    if model is None or "items" not in (model["received"].get("doc") or {}):
        handler._fail(503, "Received documents unavailable for this run")
        return True
    matches = {}
    for item in cd._items(model):
        digest = item.get("original_sha256")
        if isinstance(digest, str) and ri.is_hash(digest) and digest.startswith(prefix):
            matches.setdefault(digest, []).append(item)
    if not matches:
        handler._fail(404, "Document not found")
    elif len(matches) > 1:
        handler._fail(409, "This ID matches more than one document")
    else:
        (digest, items), = matches.items()
        handler._send(200, _render(options, model, digest, items, handler.server.analysis_root))
    return True
