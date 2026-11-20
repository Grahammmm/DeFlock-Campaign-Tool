"""Text of every page a run's citations point to, for the private console export (DF2307-07).

``cited-pages.json`` holds, for each (original sha256, page) cited by one run's agency-factsheets.json, gap-proposals.json and
received-items.json, the CURRENT extraction's page text read through the checked unit reader of received_items (manifest hash,
ordered membership, derived CAS path, payload digest, locator and text hash), its text route, text_sha256 and the citing ids.
A run citation says what pointed at the page; the text is the current extraction, not proof that an old quote still matches.
Read-only: no original bytes, no ledger write, no OCR decision. Anything over a bound is counted in ``not_exported``.
"""
import hashlib
import json
import os
import re
import sqlite3
from datetime import date
from pathlib import Path

from . import approval_queue as aq, console_export as ce, console_ocr as co, ocr_review
from .extraction_routes import OCR_METHOD, VISION_METHOD
from .outcomes import received_items as ri
from .run_stages import LedgerBusy, snapshot_ro

NAME, SCHEMA = ce.CITED, ce.CITED_SCHEMA
MAX_PAGES, MAX_PAGE_BYTES, MAX_TOTAL_BYTES = 2000, 64 * 1024, 32 * 1024 * 1024
REASONS = ("page_bytes", "page_count", "total_bytes")
ROUTES, BANDS = ("native", "ocr"), ("below_80", "at_least_80", "not_recorded", "not_applicable")
REVIEWS, KINDS = ("pending", "confirmed", "rejected", "not_applicable"), ("fact", "gap", "deadline")
KEYS = {"schema", "source_project_id", "state", "run_id", "pages", "not_exported"}
ROW = {"original_sha256", "page", "agency", "received_at", "route", "confidence_band", "review_state", "text", "text_sha256",
       "citations"}
TYPE, CODE = re.compile(r"[a-z][a-z_]{0,39}"), re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")


def encode(doc):
    """The one serialization of this file (compact, UTF-8), so the byte budget is exact."""
    return (json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")


def unavailable(code):
    return {"state": "unavailable", "code": code}


def _need(ok, code):
    if not ok:
        raise ValueError(code)


def _cited(root, run_id):
    """-> ({(sha256, page): {"cites": {(kind, id, label)}, "agency": fallback}}, {original sha256: received item}) from the
    run's three fixed files below `root`. Raises on any missing, mismatched or broken input; empty lists are a real answer."""
    _need(ce.RUN_ID.fullmatch(run_id) and run_id not in (".", ".."), "cited_pages_run_id")
    fds = [os.open(os.path.abspath(root), os.O_RDONLY | os.O_DIRECTORY)]
    try:
        for parts in (["run-outputs", run_id], ["run-outputs", run_id, "outcomes"]):
            fd = ce._open_chain(fds[0], parts)
            if fd is None:
                raise ValueError("cited_pages_input_missing")
            fds.append(fd)
        sheets, _ = co._read_json(fds[1], "agency-factsheets.json", "records-agency-factsheets-v1", run_id)
        gaps, _ = co._read_json(fds[2], "gap-proposals.json", "records-gap-proposals-v1", run_id)
        received, raw = co._read_json(fds[2], "received-items.json", ri.SCHEMA, run_id)
    finally:
        for fd in fds:
            os.close(fd)
    items = {}
    for item in co._list(received.get("items")):
        _need(type(item.get("item_id")) is str and type(item.get("original_sha256")) is str, "cited_pages_item")
        _need(item["item_id"] not in items, "cited_pages_item")
        items[item["item_id"]] = item
    cited = {}

    def add(sha, page, kind, ident, label, agency=None):
        _need(type(sha) is str and co.SHA.fullmatch(sha) and type(ident) is str and ident, "cited_pages_citation")
        entry = cited.setdefault((sha, co._page(page)), {"cites": set(), "agency": None})
        entry["cites"].add((kind, ident, label))
        entry["agency"] = entry["agency"] or agency

    for agency in co._list(sheets.get("agencies")):
        _need(type(agency.get("agency_id")) is str and type(agency.get("fields")) is dict, "cited_pages_citation")
        for field, value in agency["fields"].items():
            _need(type(value) is dict, "cited_pages_citation")
            for number, statement in enumerate(co._list(value.get("statements")), 1):
                cite = statement.get("citation")
                _need(type(cite) is dict, "cited_pages_citation")
                add(cite.get("document_sha256"), cite.get("page"), "fact", "%s/%s/%d" % (agency["agency_id"], field, number),
                    field, agency["agency_id"])
    seen, documents = set(), []
    for agency in co._list(gaps.get("agencies")):
        for request in co._list(agency.get("requests")):
            for proposal in co._list(request.get("proposals")):
                ident = proposal.get("proposal_id")
                _need(type(ident) is str and ident, "cited_pages_citation")
                if ident in seen:
                    continue
                seen.add(ident)
                if proposal.get("evidence_kind") == "document":  # a ledger-field proposal cites no page
                    documents.append((ident, proposal.get("item_id"), proposal.get("page"), proposal.get("type")))
                else:
                    _need(proposal.get("evidence_kind") == "ledger_field", "cited_pages_citation")
    if documents:
        _need(gaps.get("received_items_sha256") == hashlib.sha256(raw).hexdigest(), "cited_pages_binding")  # made from these items
    for ident, item_id, page, kind in documents:
        _need(type(item_id) is str and item_id in items, "cited_pages_item")
        add(items[item_id]["original_sha256"], page, "gap", ident, kind if type(kind) is str and TYPE.fullmatch(kind) else "gap")
    for item in items.values():
        for number, deadline in enumerate(co._list(item.get("deadlines")), 1):
            if deadline.get("page") is None:  # a deadline with no page cites no page
                continue
            stated = deadline.get("date")
            try:
                label = date.fromisoformat(stated).isoformat() if type(stated) is str and len(stated) == 10 else None
            except ValueError:
                label = None
            add(item["original_sha256"], deadline["page"], "deadline", "%s/%d" % (item["item_id"], number),
                label or "Stated deadline")
    return cited, {item["original_sha256"]: item for item in items.values()}


def _ocr_state(row, sha, page, decisions):
    """-> (route, confidence band, review state) of one checked manifest page row; confidence never implies a review."""
    if row.get("method") not in (OCR_METHOD, VISION_METHOD) or row.get("reason") == "ocr_skipped_machine_text":
        return "native", "not_applicable", "not_applicable"  # an OCR skip carries the machine text
    confidence = row.get("confidence")
    band = "not_recorded" if type(confidence) not in (int, float) else "at_least_80" if confidence >= 80 else "below_80"
    return "ocr", band, decisions().get((sha, page, confidence, row.get("derivative_sha256")), "pending")


def _build(project, run_id, root):
    project = os.path.abspath(project)
    pid = aq._project_id(project)
    cited, owners = _cited(root or project, run_id)
    head = {"schema": SCHEMA, "source_project_id": pid, "state": "ok", "run_id": run_id}
    omit = lambda sha, page, reason: {"original_sha256": sha, "page": page, "reason": reason}
    keys = sorted(cited)
    used = len(encode({**head, "pages": [], "not_exported": []}))
    rest = sum(len(encode(omit(s, p, REASONS[-1]))) for s, p in keys)  # worst case: every page omitted (item + its comma)
    _need(used + rest <= MAX_TOTAL_BYTES, "cited_pages_metadata_bound")
    pages, skipped, loaded = [], [], {}

    def decisions():  # read once, and only for a project that cites an OCR page
        if "d" not in loaded:
            loaded["d"] = {ocr_review.binding(r): r["decision"]
                           for r in ocr_review.read_reviews(Path(project) / "ocr-review-v1" / "reviews.jsonl")}
        return loaded["d"]
    if keys:
        processing = ri._safe_dir(Path(project) / "processing-v1")
        _need(not (processing / "ledger.sqlite").is_symlink(), "cited_pages_ledger_path")
        with snapshot_ro(processing / "ledger.sqlite") as db:
            db.row_factory = sqlite3.Row
            for sha in sorted({s for s, _ in keys}):
                units, found, _ = ri._current_units(db, processing, sha, page_rows=True)
                _need("current_extraction_unavailable" not in found, "cited_pages_extraction")
                for key in (k for k in keys if k[0] == sha):
                    page, here = key[1], [u for u in units if u["page"] == key[1]]
                    _need(len(here) == 1, "cited_pages_unit")  # none or several current units: never pick one
                    rest -= len(encode(omit(sha, page, REASONS[-1])))
                    text = here[0]["text"].encode("utf-8")
                    route, band, review = _ocr_state(here[0]["page_row"], sha, page, decisions)
                    item = owners.get(sha) or {}
                    when, agency = item.get("received_at"), item.get("agency_id")
                    row = {"original_sha256": sha, "page": page, "agency": agency if type(agency) is str and agency else cited[key]["agency"],
                           "received_at": when if ri._utc(when) is not None else None, "route": route,
                           "confidence_band": band, "review_state": review, "text": here[0]["text"],
                           "text_sha256": here[0]["text_sha256"],
                           "citations": [dict(zip(("kind", "id", "label"), c)) for c in sorted(cited[key]["cites"])]}
                    size = len(encode(row)) - (0 if pages else 1) if len(text) <= MAX_PAGE_BYTES else 0  # encode() ends in "\n"; the item adds a comma after the first
                    reason = ("page_bytes" if len(text) > MAX_PAGE_BYTES else "page_count" if len(pages) >= MAX_PAGES
                              else "total_bytes" if used + size + rest > MAX_TOTAL_BYTES else None)
                    if reason is None:
                        pages.append(row)
                        used += size
                    else:
                        used += len(encode(omit(sha, page, reason))) - (0 if skipped else 1)
                        skipped.append(omit(sha, page, reason))
    doc = {**head, "pages": pages, "not_exported": skipped}
    _need(len(encode(doc)) <= MAX_TOTAL_BYTES, "cited_pages_metadata_bound")
    return doc


def read(project, run_id, root=None):
    """The ok model of the run's cited pages, or {'state': 'unavailable', 'code': code}. `root` holds run-outputs/<run_id>/
    (an export staging copy; the live project by default); the evidence is read from the live project. Never writes."""
    try:
        return _build(project, run_id, root)
    except LedgerBusy:
        return unavailable("ledger_busy")
    except (aq.ApprovalQueueError, ce.ExportError) as error:
        return unavailable(getattr(error, "code", None) or str(error))
    except ValueError as error:  # fixed codes (a decoder message is not one)
        return unavailable(str(error) if CODE.fullmatch(str(error)) else "cited_pages_unreadable")
    except (OSError, sqlite3.Error, KeyError, TypeError, RecursionError):
        return unavailable("cited_pages_unreadable")


def _hash(value):
    return type(value) is str and co.SHA.fullmatch(value) is not None


def _page_ok(row):
    text = row["text"].encode("utf-8")
    return (set(row) == ROW and _hash(row["original_sha256"]) and type(row["page"]) is int and row["page"] >= 1
            and all(row[k] is None or type(row[k]) is str for k in ("agency", "received_at"))
            and row["route"] in ROUTES and row["confidence_band"] in BANDS and row["review_state"] in REVIEWS
            and (row["route"] == "ocr") == (row["review_state"] != "not_applicable")
            and len(text) <= MAX_PAGE_BYTES and hashlib.sha256(text).hexdigest() == row["text_sha256"]
            and type(row["citations"]) is list and row["citations"]
            and all(type(c) is dict and set(c) == {"kind", "id", "label"} and c["kind"] in KINDS
                    and type(c["id"]) is str and type(c["label"]) is str for c in row["citations"]))


def validate(doc, run_id):
    """`doc` when it is a well-formed ok model of run_id within every bound (unique, disjoint page keys), else None."""
    try:
        pages, skipped = doc["pages"], doc["not_exported"]
        found = [(r["original_sha256"], r["page"]) for r in pages] + [(r["original_sha256"], r["page"]) for r in skipped]
        ok = (set(doc) == KEYS and doc["schema"] == SCHEMA and doc["state"] == "ok" and doc["run_id"] == run_id
              and type(pages) is list and type(skipped) is list and len(pages) <= MAX_PAGES
              and all(type(r) is dict for r in pages + skipped) and all(_page_ok(r) for r in pages)
              and all(set(r) == {"original_sha256", "page", "reason"} and _hash(r["original_sha256"])
                      and type(r["page"]) is int and r["page"] >= 1 and r["reason"] in REASONS for r in skipped)
              and len(set(found)) == len(found) and len(encode(doc)) <= MAX_TOTAL_BYTES)
    except (KeyError, TypeError, AttributeError, ValueError):
        return None
    return doc if ok else None
