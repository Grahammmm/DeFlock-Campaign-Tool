"""Admin console page to confirm or reject one OCR page (OCR-REVIEW-ROUTE-1).

``records console-export`` writes ``console-model/ocr-pages.json`` inside the container: every page the OCR stage flagged
for a person (``page_state.needs_visual_review``), its confidence, whether a person has already decided it and, for a page
still waiting, the current extraction text. The console serves ``GET /ocr-review`` (the list) and ``GET /ocr-review?id=``
(one page with its text) from that pinned export. ``POST /ocr-review/confirm`` and ``/ocr-review/reject`` decide ONE page
and are journaled like the console's other decisions (``console_journal``); the next ``records decisions-apply`` hands the
entry to the existing ``ocr_review.mark``. Nothing is written to the export or the live project here, nothing is signed,
sent or published, and a confirmed page only becomes readable text for the findings lane, which still needs its own review.

Review-required: the page shows OCR text, not the page image, so the person compares it with the original document.
"""
import hmac
import json
import os
import re
import sqlite3
import urllib.parse
from pathlib import Path

from . import console_layout, ocr_review, run_stages
from .outcomes import received_items as ri
from .outcomes.reading import esc

SCHEMA = "records-console-ocr-pages-v1"
PAGE_ID = re.compile(r"[0-9a-f]{64}-[1-9][0-9]{0,5}")
SHA = re.compile(r"[0-9a-f]{64}")
ROUTES = {"/ocr-review/confirm": "ocr_confirm", "/ocr-review/reject": "ocr_reject"}
FIELDS = {"ocr_confirm": {"csrf_token", "id", "derivative_sha256", "confidence", "read"},
          "ocr_reject": {"csrf_token", "id", "derivative_sha256", "confidence"}}
LABEL = "console-reviewer"  # the label the page writes to reviews.jsonl (a role, never a name; the journal holds the actor)
MAX_ROWS = 1000
MAX_PAGE_TEXT = 20000
MAX_TOTAL_TEXT = 32 * 1024 * 1024
REFUSED = "Refused: an OCR page is decided only from a pinned export with a decision journal. Nothing was saved or decided."
NOTE = ("A person compares this text with the page in the original document. Confirm records that it matches; Reject keeps "
        "the page out of use. Either is recorded here and applied at the next records run. Nothing is sent or published.")
BUCKET_WORDS = {"spot_check": "OCR confidence 80 or more: spot check", "hold": "held until a person confirms"}


class OcrPagesError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def page_id(sha, page):
    return "%s-%d" % (sha, page)


def _unavailable(code):
    return {"state": "unavailable", "code": code}


def _page_text(db, processing, digest, wanted):
    """-> ({page: text}, None) or ({}, code). The current extraction's text per page; never a partial answer."""
    try:
        units, diagnostics, _ = ri._current_units(db, processing, digest)
    except (ValueError, OSError):
        return {}, "extraction_unavailable"
    if diagnostics or not units:
        return {}, "extraction_unavailable"
    found = {}
    for unit in sorted(units, key=lambda u: (u["page"], u["ordinal"])):
        if unit["page"] in wanted:
            found.setdefault(unit["page"], []).append(unit["text"])
    return {page: "\n".join(texts) for page, texts in found.items()}, None


def _collect(db, processing, reviews):
    db.row_factory = sqlite3.Row
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='page_state'").fetchone():
        return {"rows": [], "counts": dict(pending=0, confirmed=0, rejected=0), "truncated": False}
    flagged = ocr_review.pages(db)
    decisions = {ocr_review.binding(r): r["decision"] for r in ocr_review.read_reviews(reviews)} if flagged else {}
    counts, rows, truncated, total = dict(pending=0, confirmed=0, rejected=0), [], False, 0
    waiting = 0  # waiting rows listed so far: the cap counts these only, never the pages already decided
    texts = {}
    for page in flagged:
        decision = decisions.get(ocr_review.binding(page))
        state = {"confirmed": "confirmed", "rejected": "rejected"}.get(decision, "pending")
        counts[state] += 1
        row = {"id": page_id(page["original_sha256"], page["page_no"]), "original_sha256": page["original_sha256"],
               "page_no": page["page_no"], "confidence": page["confidence"], "derivative_sha256": page["derivative_sha256"],
               "bucket": ocr_review.bucket(page["confidence"]), "state": state, "text": None, "text_code": None,
               "text_truncated": False}
        if state == "pending":
            if waiting >= MAX_ROWS or total >= MAX_TOTAL_TEXT:
                truncated = True
                continue
            waiting += 1
            if page["original_sha256"] not in texts:
                wanted = {p["page_no"] for p in flagged if p["original_sha256"] == page["original_sha256"]}
                texts[page["original_sha256"]] = _page_text(db, processing, page["original_sha256"], wanted)
            by_page, code = texts[page["original_sha256"]]
            text = by_page.get(page["page_no"])
            if text is None:
                row["text_code"] = code or "page_text_missing"
            else:
                row["text_truncated"] = len(text) > MAX_PAGE_TEXT
                row["text"] = text[:MAX_PAGE_TEXT]
                total += len(row["text"].encode("utf-8"))
        rows.append(row)
    return {"rows": rows, "counts": counts, "truncated": truncated}


def model(project):
    """The console-model body for the live project (read-only), or {"state": "unavailable", "code": ...}."""
    project = Path(os.path.abspath(project))
    processing = project / "processing-v1"
    try:
        with run_stages.ledger_ro(processing) as db:
            body = _collect(db, processing, project / "ocr-review-v1" / "reviews.jsonl")
    except run_stages.LedgerBusy:
        return _unavailable("ledger_busy")
    except ocr_review.ChainBroken:
        return _unavailable("ocr_review_chain_broken")
    except (sqlite3.Error, OSError, ValueError, KeyError, TypeError):
        return _unavailable("model_unreadable")
    return body


def _items(project, run_id):
    """{original sha256: received item} from this run's received-items.json; {} when it cannot be read."""
    from . import console_documents as cd
    try:
        got = cd._read(Path(os.path.abspath(project)) / "run-outputs" / run_id / "outcomes" / "received-items.json", ri.SCHEMA)
    except (ValueError, OSError):
        return {}
    items = (got.get("doc") or {}).get("items", []) if got["state"] == "ok" else []
    found = {}
    for item in items:
        if type(item) is dict and isinstance(item.get("original_sha256"), str):
            found.setdefault(item["original_sha256"], item)
    return found


def _confidence(row):
    value = row["confidence"]
    return "not recorded" if value is None else ("%g" % value)


def _label(row, items):
    item = items.get(row["original_sha256"]) or {}
    return str(item.get("item_id") or "document " + row["original_sha256"][:12])


def status_words(row):
    note = row.get("pending_apply") or ""
    if row["state"] == "confirmed":
        return "Confirmed"
    if row["state"] == "rejected":
        return "Rejected (stays not ready)"
    if note.startswith("pending apply: ocr_confirm"):
        return "Confirm recorded; applies at the next records run"
    if note.startswith("pending apply: ocr_reject"):
        return "Reject recorded; applies at the next records run"
    if note:
        return "Awaiting confirmation (%s)" % note
    return "Awaiting confirmation"


def _open(row):
    return row["state"] == "pending" and not (row.get("pending_apply") or "").startswith("pending apply")


def complete_text(row):
    """True only when the page's whole text was exported: a page whose text is missing or cut short is never confirmed."""
    return type(row.get("text")) is str and row.get("text_truncated") is False


def _form(path, token, row, button, extra=""):
    hidden = {"csrf_token": token, "id": row["id"], "derivative_sha256": row["derivative_sha256"] or "",
              "confidence": json.dumps(row["confidence"])}
    return ('<form method="post" action="%s">%s%s<button type="submit">%s</button></form>' % (
        path, "".join('<input type="hidden" name="%s" value="%s">' % (k, esc(v)) for k, v in hidden.items()), extra,
        esc(button)))


def render_list(doc, items, form_ok, error=None):
    out = ["<h1>OCR pages to confirm</h1>"]
    if error:
        out.append(console_layout.sources_banner([], [console_layout.unavailable_note("OCR pages", error)]))
        return console_layout.shell("OCR pages to confirm", "\n".join(out), active="OCR pages")
    counts = doc["counts"]
    out.append('<p role="status">%d awaiting confirmation, %d confirmed, %d rejected.</p>' % (
        counts["pending"], counts["confirmed"], counts["rejected"]))
    out.append("<p>%s</p>" % esc(NOTE))
    if not form_ok:
        out.append('<p class="diag" role="status">Read-only: a page can be decided only from the exported console with a '
                   "decision journal.</p>")
    if doc["truncated"]:
        out.append('<p class="diag" role="status">Only the first %d waiting pages are listed; the rest follow after these are '
                   "decided and the console is exported again.</p>" % MAX_ROWS)
    rows = sorted(doc["rows"], key=lambda r: (r["state"] != "pending", r["original_sha256"], r["page_no"]))
    if not rows:
        out.append("<p>No OCR pages need a person's confirmation.</p>")
    else:
        out.append('<div class="scroll"><table><thead><tr><th scope="col">Page</th><th scope="col">Confidence</th>'
                   '<th scope="col">Status</th></tr></thead><tbody>')
        for row in rows:
            out.append('<tr><td><a href="/ocr-review?id=%s">%s, page %d</a></td><td>%s</td><td>%s</td></tr>' % (
                esc(row["id"]), esc(_label(row, items)), row["page_no"], esc(_confidence(row)), esc(status_words(row))))
        out.append("</tbody></table></div>")
    return console_layout.shell("OCR pages to confirm", "\n".join(out), active="OCR pages")


def render_page(row, items, token):
    """token None: read-only (no form). One page: its source item, confidence, text and the two decisions."""
    item = items.get(row["original_sha256"]) or {}
    title = "OCR page %d of %s" % (row["page_no"], _label(row, items))
    out = ["<h1>%s</h1>" % esc(title), '<p role="status">%s</p>' % esc(status_words(row)), "<dl>"]
    for name, value in (("Item", item.get("item_id") or "not in this run's received items"), ("Agency", item.get("agency_id")),
                        ("Request", item.get("request_id")), ("Received", item.get("received_at")),
                        ("Document", row["original_sha256"][:12]), ("Page", row["page_no"]),
                        ("Confidence", _confidence(row)), ("Queue", BUCKET_WORDS[row["bucket"]])):
        if value is not None:
            out.append("<dt>%s</dt><dd>%s</dd>" % (esc(name), esc(value)))
    out.append("</dl><h2>OCR text</h2>")
    if row["text"] is not None:
        out.append("<pre>%s</pre>" % esc(row["text"]))
        if row["text_truncated"]:
            out.append('<p class="diag" role="alert">Text shortened after %d characters. Do not confirm this page: the whole '
                       "text is not shown. It can be rejected.</p>" % MAX_PAGE_TEXT)
    elif row["state"] == "pending":
        out.append('<p class="diag" role="alert">The text could not be shown (%s). Do not confirm this page.</p>' % esc(
            row["text_code"] or "unavailable"))
    else:
        out.append("<p>The text is shown only while a page is waiting.</p>")
    out.append("<p>%s</p>" % esc(NOTE))
    if _open(row) and token is not None:
        if complete_text(row):  # no Confirm form unless the whole text is on the page; Reject stays available
            out.append(_form("/ocr-review/confirm", token, row, "Confirm this page",
                             '<label><input type="checkbox" name="read" value="1" required> '
                             "I compared this text with the page and it matches.</label>"))
        out.append(_form("/ocr-review/reject", token, row, "Reject this page"))
    out.append('<p><a href="/ocr-review">Back to the OCR pages</a></p>')
    return console_layout.shell(title, "\n".join(out), active="OCR pages")


def serve(handler):
    path = urllib.parse.urlsplit(handler.path)
    if path.path != "/ocr-review":
        return False
    args = urllib.parse.parse_qs(path.query, keep_blank_values=True)
    ident = args.get("id", [None])[0]
    if set(args) - {"id"} or len(args.get("id", [])) > 1 or (ident is not None and not PAGE_ID.fullmatch(ident)):
        handler._fail(400)
        return True
    options = handler.options
    form_ok = handler.pin is not None and handler.server.decisions_dir is not None
    token = handler.csrf_token() if form_ok else None
    try:
        doc = options["source"].ocr_pages()
    except OcrPagesError as error:
        handler._send(200, render_list(None, {}, form_ok, error.code))
        return True
    except (OSError, ValueError, KeyError, TypeError):
        handler._fail(503)
        return True
    items = _items(options["project"], options["run_id"])
    if ident is None:
        handler._send(200, render_list(doc, items, form_ok))
        return True
    row = next((r for r in doc["rows"] if r["id"] == ident), None)
    if row is None:
        handler._fail(404)
        return True
    handler._send(200, render_page(row, items, token))
    return True


def post(handler):
    """POST /ocr-review/confirm|reject: one page. True when the path is ours (answered either way)."""
    action = ROUTES.get(handler.path)
    if action is None:
        return False
    from . import console_journal, console_origin
    if not console_origin.request_ok(handler, post=True):
        handler._fail(403)
        return True
    if handler.pin is None or handler.server.decisions_dir is None:  # no pinned export and journal: nothing is read or decided
        handler._fail(503, REFUSED)
        return True
    fields = handler._body()
    if isinstance(fields, int):
        handler._fail(fields)
        return True
    if set(fields) != FIELDS[action]:
        handler._fail(400)
        return True
    if not hmac.compare_digest(fields["csrf_token"].encode("utf-8"), handler.server.csrf_token.encode("ascii")):
        handler._fail(403)
        return True
    if not PAGE_ID.fullmatch(fields["id"]) or (action == "ocr_confirm" and fields["read"] != "1"):
        handler._fail(400)
        return True
    if console_journal.ocr(handler, action, fields["id"], fields["derivative_sha256"], fields["confidence"]):
        handler._send(303, "", location="/ocr-review")
    return True
