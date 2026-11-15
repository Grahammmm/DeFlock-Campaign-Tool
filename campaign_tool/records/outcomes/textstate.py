"""Private per-record text state for the organizer (DOC-2).

Reads the extract stage's own private list (processing-v1/runs/extract-<run>.json,
schema records-extract-outcomes-v1) and writes one plain-language state per
record to run-outputs/<run>/text-state.json and text-state.md (0700 dir, 0600
files). Records this run's extract did not list (extracted by an earlier run)
are added from the ledger when their extract stage is done and they have a
current extraction: their state comes from the current OCR review state, so a
reviewed PDF is text_reviewed in every later run. An unreviewed or rejected OCR
page is never text_reviewed. A record whose OCR pages are all confirmed or
unreviewed at confidence 80 or more (ocr_review.page_use_allowed: read_with_flag),
with at least one of the latter, is ocr_flagged: usable for analysis with a flag,
never a review verdict and never public. Output carries the state, fixed words and the
record sha256 only: never a path, form, written_at, an awaiting-ocr.jsonl entry
or document text. Review-required; a state is not a review of what the text
says. The extract list, the OCR review file and the ledger are read, never
written.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sqlite3
import stat

from ..understanding.evidence import read_pinned, sha

SCHEMA = "records-text-state-v1"
LIST_SCHEMA = "records-extract-outcomes-v1"
MAX_LIST_BYTES = 64 * 1024 * 1024
MAX_OUTPUT_BYTES = 64 * 1024 * 1024
FILES = ("text-state.json", "text-state.md")
HASH = re.compile(r"[0-9a-f]{64}")
RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
UNREADABLE_RETRY = "Could not be read this time: the reader failed or ran out of time (it retries; a reader that is not installed retries when it is)."
WAITING = ("waiting", "Waiting its turn: not processed yet.")
STATES = {
    "AWAITING_OCR": ("awaiting_ocr", "A scan with no text yet; waiting for OCR."),
    "CORRUPT_DOCUMENT": ("could_not_be_read", "Could not be read: the file looks damaged."),
    "DECODER_NEEDED": ("could_not_be_read", "Could not be read: this file format needs a reader we do not have yet."),
    "WORKBOOK_POLICY_REQUIRED": ("could_not_be_read", "Could not be read: a spreadsheet needs an owner decision before it is opened."),
    "EXTRACT_NOT_ACCEPTED": ("could_not_be_read", "Could not be read: the extracted text failed our checks and was refused."),
    "EXTRACT_INCOMPLETE": ("could_not_be_read", UNREADABLE_RETRY),
    "EXTRACT_TIMEOUT": ("could_not_be_read", UNREADABLE_RETRY),
    "EXTRACT_ERROR": ("could_not_be_read", UNREADABLE_RETRY),
    "PARSER_MISSING": ("could_not_be_read", UNREADABLE_RETRY),
    "PRESERVE_PENDING": WAITING,
    "DEFERRED_RUN_BUDGET": WAITING,
}
REVIEWED = ("text_reviewed", "Text extracted and passed the extraction checks.")
OCR_UNREVIEWED = ("ocr_unreviewed", "Text came from scanning (OCR) and has not been visually checked yet.")
OCR_FLAGGED = ("ocr_flagged", "Text came from scanning (OCR); usable for analysis with a flag, not yet visually confirmed.")
UNKNOWN = ("unknown_state", "State not recognised")
ORDER = ("text_reviewed", "ocr_flagged", "ocr_unreviewed", "awaiting_ocr", "could_not_be_read", "waiting", "unknown_state")


def _need(condition, code):
    if not condition:
        raise ValueError(code)


def _path(value):
    path = Path(os.path.abspath(value))
    _need(not any(p.is_symlink() for p in (path, *path.parents)), "textstate_symlink_path")
    return path


def load_list(path):
    """No-symlink, regular-file, bounded read of the extract list; schema checked."""
    path = _path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as handle:
        _need(stat.S_ISREG(os.fstat(handle.fileno()).st_mode), "textstate_list_not_regular")
        raw = handle.read(MAX_LIST_BYTES + 1)
    _need(len(raw) <= MAX_LIST_BYTES, "textstate_list_too_large")
    data = json.loads(raw)
    _need(isinstance(data, dict) and data.get("schema") == LIST_SCHEMA, "textstate_list_schema")
    documents = data.get("documents")
    _need(isinstance(documents, list) and all(isinstance(d, dict) for d in documents), "textstate_list_documents")
    hashes = [d.get("original_sha256") for d in documents]
    _need(all(isinstance(h, str) and HASH.fullmatch(h) for h in hashes), "textstate_record_hash")
    _need(len(set(hashes)) == len(hashes), "textstate_duplicate_record")
    return documents


def classify(outcome, ocr_pending, ocr_rejected=False, ocr_flagged=False):
    if outcome == "EXTRACTED":
        if ocr_pending:
            return OCR_UNREVIEWED
        if ocr_rejected:
            return STATES["EXTRACT_NOT_ACCEPTED"]
        return OCR_FLAGGED if ocr_flagged else REVIEWED
    return STATES.get(outcome, UNKNOWN) if isinstance(outcome, str) else UNKNOWN


def build(run_id, documents, pending, rejected=frozenset(), flagged=frozenset()):
    records = sorted(({"sha256": d["original_sha256"], "state": state, "words": words}
                      for d in documents
                      for state, words in [classify(d.get("outcome"), d["original_sha256"] in pending,
                                                    d["original_sha256"] in rejected, d["original_sha256"] in flagged)]),
                     key=lambda r: r["sha256"])
    counts = dict.fromkeys(ORDER, 0)
    for record in records:
        counts[record["state"]] += 1
    return {"schema": SCHEMA, "run_id": run_id, "counts": counts, "records": records}


def markdown(report):
    prefixes = [r["sha256"][:12] for r in report["records"]]
    lines = [f"# Record text state: run {report['run_id']}", "",
             "Private and review-required: one state per received record; not a review of what the text says.",
             "", "| State | Records |", "| --- | --- |"]
    lines += [f"| {state} | {report['counts'][state]} |" for state in ORDER]
    lines.append("")
    for record, prefix in zip(report["records"], prefixes):
        short = record["sha256"] if prefixes.count(prefix) > 1 else prefix
        lines.append(f"{short} - {record['words']}")
    return "\n".join(lines) + "\n"


def _extracted_earlier(db, listed):
    """Originals whose extract stage is done, with a current extraction, that this run did not list.

    Same admission as the received-items inventory (effective original, in scope, admitted role);
    an original whose admission cannot be shown is left out, never guessed.
    """
    from ..ledger import store
    from ..ledger_catalog import scope_excluded
    from .received_items import ADMITTED_ROLES
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"originals", "stage_state", "extraction_adapter_current"} <= tables:
        return set()
    db.row_factory = sqlite3.Row
    found = set()
    for row in db.execute("SELECT o.sha256 AS sha256 FROM originals o "
                          "JOIN stage_state e ON e.original_sha256=o.sha256 AND e.stage='extract' AND e.status='done' "
                          "JOIN extraction_adapter_current c ON c.original_sha256=o.sha256 ORDER BY o.sha256").fetchall():
        digest = row["sha256"]
        if digest in listed or not HASH.fullmatch(digest):
            continue
        try:
            original = store.effective_original(db, digest)
        except ValueError:
            continue
        if original is not None and not scope_excluded(original) and original["role"] in ADMITTED_ROLES:
            found.add(digest)
    return found


def _ledger_facts(project, processing, listed):
    """-> (records with an OCR page held, with one rejected, with one read-with-flag, records extracted by earlier runs).

    Each current review-required page goes through the admitted policy ocr_review.page_use_allowed (confirmed: use;
    rejected: never; unreviewed at confidence 80 or more: read with a flag; anything else: held for a person). A page
    without a confidence, or under 80, is held exactly as before; a broken review chain raises (never read as reviewed).
    """
    from ..ocr_review import page_use_allowed, pages
    from ..run_stages import ledger_ro
    reviews = project / "ocr-review-v1" / "reviews.jsonl"
    with ledger_ro(processing) as db:
        has_pages = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='page_state'").fetchone()
        review_pages = pages(db) if has_pages else []  # an older ledger without page rows has nothing to review
        pending, rejected, flagged = set(), set(), set()
        for page in review_pages:
            allowed, reason = page_use_allowed(page, reviews)
            if allowed:
                if reason == "read_with_flag":
                    flagged.add(page["original_sha256"])
            elif reason == "review_rejected":
                rejected.add(page["original_sha256"])
            else:
                pending.add(page["original_sha256"])
        return pending, rejected, flagged, _extracted_earlier(db, listed)


def write_run_textstate(project, run_id):
    """Write this writer's own pair under run-outputs/<run_id>/; raise OSError on any refusal.

    A missing, symlinked, oversize or wrong-schema list writes nothing and
    raises, so it can never read as "all fine". An identical replay is a
    verified no-op; a changed or partial existing pair is refused.
    """
    from ..run_stages import LedgerBusy
    try:
        _need(isinstance(run_id, str) and RUN_ID.fullmatch(run_id) is not None, "textstate_run_id")
        project = _path(project)
        processing = project / "processing-v1"
        name = "".join(ch for ch in run_id if ch.isalnum() or ch in "-_.") or "run"  # as write_private_list
        documents = load_list(processing / "runs" / f"extract-{name}.json")
        pending, rejected, flagged, earlier = _ledger_facts(project, processing, {d["original_sha256"] for d in documents})
        documents = documents + [{"original_sha256": digest, "outcome": "EXTRACTED"} for digest in sorted(earlier)]
        report = build(run_id, documents, pending, rejected, flagged)
        files = ((FILES[0], (json.dumps(report, ensure_ascii=False, sort_keys=True) + "\n").encode()),
                 (FILES[1], markdown(report).encode()))
        _need(sum(len(raw) for _, raw in files) <= MAX_OUTPUT_BYTES, "textstate_output_bound")
        directory = _path(project / "run-outputs")
        directory.mkdir(mode=0o700, exist_ok=True)
        output = _path(directory / run_id)
        if output.exists():
            # Shared with fact sheets, comparison and register: only this pair is verified.
            _need(output.is_dir(), "textstate_existing_output")
            present = set(FILES) & {p.name for p in output.iterdir()}
            if present == set(FILES):
                for filename, raw in files:
                    read_pinned(output / filename, sha(raw), MAX_OUTPUT_BYTES)
                return report
            _need(not present, "textstate_partial_output")  # A partial pair is never repaired.
        else:
            output.mkdir(mode=0o700)
        for filename, raw in files:
            fd = os.open(output / filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
        return report
    except (ValueError, sqlite3.Error, LedgerBusy) as error:
        raise OSError("TEXTSTATE_UNAVAILABLE") from error
