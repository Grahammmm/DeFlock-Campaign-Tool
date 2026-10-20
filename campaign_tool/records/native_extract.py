"""Phase 1 native-text extraction over one records root (T11k).

The ``extract`` stage of the records run (``run_stages``) calls ``run`` here.
It advances every original whose ledger ``extract`` stage is ``pending``
through the engine's existing extraction path, the same one ``records run``
uses (``run.Pipeline.stage_extract``):

1. ``extraction_routes.extract`` parses the pinned original in a bounded
   child process of the *current* interpreter. The Phase 1 stage plan runs
   this under the campaign-tool parser venv (``CONTAINER_PYTHON``), which is
   where pypdf/openpyxl are installed.
2. ``ExtractionLedgerAdapter.enroll`` binds the receipt and units to the
   canonical original.
3. ``ExtractionStageAdapter.accept`` is the installed functional extraction
   check that marks ``extract`` done. It is no visual, legal, privacy or
   review acceptance. Extracted units stay ``candidate_extracted`` and are
   review-required downstream.

This module adds no parser, no OCR and no ledger SQL writes of its own. Its
only writes are the private per-document list under ``<root>/runs`` and the
``awaiting-ocr-v1`` queue ``<root>/awaiting-ocr.jsonl`` (both 0600).

Phase 1 (G 2026-10-19) has no OCR. Scanned or no-text PDF pages and image
originals are counted as ``AWAITING_OCR`` and never dropped. They stay
``pending`` for Phase 2. The PDF ones are listed in the queue file in the OCR
stage's input shape (one line per document: ``document_id``, ``sha256`` and
the absolute ``path`` of the pinned original), rewritten in full on every run.
``Pipeline.stage_extract`` records one keyed open
alert per held document. A later run reads that alert and counts the
document again without parsing it again. A corrupt document is coded per
document and never stops the stage. The per-run ``max_documents`` cap counts
parse attempts only. Images, unsupported forms and missing-parser documents are
classified and counted on every run without using it, so they cannot starve the
parseable documents behind them.

Each document gets exactly one outcome code per run (``OUTCOMES``). The
stage result carries ``outcomes`` as ``[{code, count}]``, and the counts sum
to ``counts.input``. The per-document list (original hash, form and code) is
written to ``<root>/runs/extract-<run_id>.json`` (0600). It stays in private
storage and never goes into the report.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time

EXTRACTED = "EXTRACTED"
AWAITING_OCR = "AWAITING_OCR"
CORRUPT = "CORRUPT_DOCUMENT"
WORKBOOK_HELD = "WORKBOOK_POLICY_REQUIRED"
PARSER_MISSING = "PARSER_MISSING"
DECODER_NEEDED = "DECODER_NEEDED"
TIMEOUT = "EXTRACT_TIMEOUT"
INCOMPLETE = "EXTRACT_INCOMPLETE"
NOT_ACCEPTED = "EXTRACT_NOT_ACCEPTED"
ERROR = "EXTRACT_ERROR"
PRESERVE_PENDING = "PRESERVE_PENDING"
DEFERRED = "DEFERRED_RUN_BUDGET"
OUTCOMES = (EXTRACTED, AWAITING_OCR, CORRUPT, INCOMPLETE, WORKBOOK_HELD, PARSER_MISSING, DECODER_NEEDED,
            TIMEOUT, NOT_ACCEPTED, ERROR, PRESERVE_PENDING, DEFERRED)
FAILED = {CORRUPT, INCOMPLETE, TIMEOUT, NOT_ACCEPTED, ERROR}
# Retried on later runs until an alert has counted MAX_ATTEMPTS of them; then held.
RETRYABLE = {TIMEOUT, ERROR}
MAX_ATTEMPTS = 3
# Native parser each form needs beyond the stdlib (folder.worker / extraction_routes).
FORM_PARSER = {"pdf": "pypdf", "xlsx": "openpyxl", "msg": "extract_msg"}
# Fixed issue codes in a held document's alert key ("extract:<sha256>:<code>,<code>").
# Pipeline.stage_extract writes the parser ones; this module writes the last two.
OCR_CODES = {"ocr_runtime_unconfigured"}
WORKBOOK_CODES = {"workbook_cached_values_and_objects_not_reviewed", "workbook_policy_refused"}
WORKBOOK_REFUSED_CODE = "workbook_policy_refused"
TIMEOUT_CODES = {"extraction_timeout"}
DECODER_CODES = {"decoder_needed", "unsupported_format"}
CORRUPT_CODES = {"decoder_unavailable_or_failed", "invalid_or_bounded_parser_output", "extraction_error"}
REFUSED_CODE = "extraction_check_refused"
ERROR_CODE = "extraction_stage_error"
ALERT_PREFIX = "extract:"
DEFAULT_MAX_DOCUMENTS = 200
DEFAULT_DOCUMENT_TIMEOUT = 120
MIN_DOCUMENT_SECONDS = 10
DEADLINE_MARGIN = 15
# Queue file for the OCR stage (awaiting-ocr-v1 JSON Lines), rewritten every run.
AWAITING_OCR_QUEUE = "awaiting-ocr.jsonl"


class RootNotInitialized(Exception):
    """The records root has no ledger or intake database. Nothing is created."""


def missing_parsers():
    missing = []
    for module in ("pypdf", "openpyxl"):
        try:
            __import__(module)
        except ImportError:
            missing.append(module)
    return missing


def _importable(module):
    try:
        __import__(module)
    except ImportError:
        return False
    return True


def classify_codes(codes):
    """Outcome for a held/blocked document from its fixed extraction issue codes."""
    codes = set(codes)
    if codes & OCR_CODES:
        return AWAITING_OCR
    if codes & WORKBOOK_CODES:
        # Text was parsed but a workbook is only accepted under the pinned native workbook
        # policy, which this Phase 1 path does not load: held for an owner decision.
        return WORKBOOK_HELD
    if codes & TIMEOUT_CODES:
        return TIMEOUT
    if codes & DECODER_CODES:
        return DECODER_NEEDED
    if codes & CORRUPT_CODES:
        return CORRUPT
    if REFUSED_CODE in codes:
        return NOT_ACCEPTED
    if ERROR_CODE in codes:
        return ERROR
    return INCOMPLETE  # parsed, but with coded gaps the extraction check cannot accept


def _reason_codes(reason):
    # "extraction_<status>:<code>,<code>" from Pipeline.stage_extract.
    _, _, tail = str(reason or "").partition(":")
    return {code for code in tail.split(",") if code}


def pending_subjects(pipeline):
    """``(subject, preserve_done)`` for in-scope originals whose extract is pending."""
    rows = pipeline._query(
        "SELECT o.sha256 AS sha256, p.status AS preserve FROM originals o "
        "JOIN stage_state e ON e.original_sha256=o.sha256 AND e.stage='extract' "
        "LEFT JOIN stage_state p ON p.original_sha256=o.sha256 AND p.stage='preserve' "
        "WHERE o.scope!='out_of_scope' AND e.status='pending' ORDER BY o.first_seen_at,o.sha256")
    return [(row["sha256"], row["preserve"] == "done") for row in rows]


def ocr_released(codes):
    """True when an alert held the document only because no OCR runtime was configured.

    Its codes classify as ``AWAITING_OCR`` and, without the OCR runtime code, name no
    other hold (workbook, timeout, decoder, corrupt, refused, error). Such an alert
    stops holding once a valid OCR runtime is enabled; every other alert still holds.
    """
    codes = set(codes)
    return classify_codes(codes) == AWAITING_OCR and classify_codes(codes - OCR_CODES) == INCOMPLETE


def open_alerts(pipeline, subject):
    return pipeline._query("SELECT key,count FROM alerts WHERE state='open' AND substr(key,1,?)=?",
                           (len(ALERT_PREFIX) + len(subject) + 1, ALERT_PREFIX + subject + ":"))


def held_outcome(pipeline, subject, *, ocr_enabled=False):
    """Outcome of an earlier run's open extract alert that holds the document, else None.

    Retryable outcomes (timeouts, unexpected errors) hold only once their alert has
    counted ``MAX_ATTEMPTS`` runs. Resolving the alert (state != 'open') re-queues it.
    With ``ocr_enabled`` (Phase 2, T6), an alert that held the document only for the
    missing OCR runtime (``ocr_released``) no longer holds it, so a PDF queued
    ``AWAITING_OCR`` in Phase 1 is extracted with OCR on the first OCR-enabled run.
    """
    for row in open_alerts(pipeline, subject):
        codes = row["key"].rsplit(":", 1)[-1].split(",")
        if ocr_enabled and ocr_released(codes):
            continue
        outcome = classify_codes(codes)
        if outcome not in RETRYABLE or row["count"] >= MAX_ATTEMPTS:
            return outcome
    return None


def resolve_ocr_released(pipeline, subject):
    """Mark the subject's OCR-runtime-only alerts resolved after it is extracted with OCR."""
    keys = [row["key"] for row in open_alerts(pipeline, subject)
            if ocr_released(row["key"].rsplit(":", 1)[-1].split(","))]
    if keys and hasattr(pipeline, "_resolve_alert"):
        for key in keys:
            try:
                pipeline._resolve_alert(key)
            except Exception:  # noqa: BLE001 - the document is extracted; a stale alert only shows in status
                pass


def _hold(pipeline, subject, code):
    try:
        pipeline._alert(ALERT_PREFIX + subject + ":" + code, owner="runtime")
    except Exception:  # noqa: BLE001 - the outcome is still counted in this run's report
        pass


def _pages(pipeline, import_id):
    return pipeline._query("SELECT COUNT(*) AS n FROM extraction_adapter_pages WHERE import_id=?",
                           (import_id,))[0]["n"]


def classify_form(pipeline, subject, *, missing):
    """``(form, outcome)`` before any parse; ``outcome`` is None when the original needs parsing.

    This only resolves the declared form (a ledger read and a bounded head read). It
    never parses, so ``run`` does not charge it to the per-run parse budget: images,
    unsupported forms and missing parsers are counted on every run without using the
    slots that parseable documents need. ``PARSER_MISSING`` is not held, so the
    document is eligible again on the first run after the parser is installed.
    """
    from . import extraction_routes

    try:
        form = pipeline._form(subject, pipeline._original(subject))
    except Exception:  # noqa: BLE001 - unresolved declarations or unreadable original
        _hold(pipeline, subject, ERROR_CODE)  # retryable: held after MAX_ATTEMPTS runs
        return None, ERROR
    if form in extraction_routes.IMAGES:
        return form, AWAITING_OCR  # Phase 1: image originals wait for OCR/vision
    if form not in extraction_routes.SUPPORTED and form != "msg":
        return form, DECODER_NEEDED
    needed = FORM_PARSER.get(form)
    if needed and (needed in missing or (form == "msg" and not _importable(needed))):
        return form, PARSER_MISSING
    return form, None


def _workbook_refusal(form, error):
    # The installed check raises ExtractionBindingError("workbook_...") from the native
    # workbook policy (formula cells, gaps, package holds). Hold it as a workbook outcome.
    return form == "xlsx" and str(error).startswith("workbook_")


def extract_one(pipeline, stage, subject, *, form, timeout):
    """Return ``(outcome, form, units, pages)`` for one classified, parseable original."""
    from .extraction_ledger import ExtractionBindingError

    pipeline.extraction_timeout = timeout
    try:
        result = pipeline.stage_extract(subject, stage)
    except (ExtractionBindingError, ValueError) as error:
        # Parsed and possibly enrolled, but the installed extraction check refused it.
        # Held, so later runs do not parse and enroll the same document again.
        if _workbook_refusal(form, error):
            _hold(pipeline, subject, WORKBOOK_REFUSED_CODE)
            return WORKBOOK_HELD, form, 0, 0
        _hold(pipeline, subject, REFUSED_CODE)
        return NOT_ACCEPTED, form, 0, 0
    except Exception:  # noqa: BLE001 - one document never stops the stage
        _hold(pipeline, subject, ERROR_CODE)
        return ERROR, form, 0, 0
    status = result.get("status")
    if status == "done":
        units = int(result.get("units") or 0)
        return EXTRACTED, form, units, _pages(pipeline, result["import_id"]) if result.get("import_id") else 0
    if status == "blocked" and str(result.get("reason", "")).startswith("extraction_"):
        return classify_codes(_reason_codes(result["reason"])), form, 0, 0
    _hold(pipeline, subject, REFUSED_CODE)
    return NOT_ACCEPTED, form, 0, 0


def write_private_list(root, run_id, documents):
    directory = Path(root) / "runs"
    name = "".join(ch for ch in run_id if ch.isalnum() or ch in "-_.") or "run"
    body = {"schema": "records-extract-outcomes-v1", "run_id": run_id,
            "written_at": datetime.now(timezone.utc).isoformat(), "documents": documents}
    _write_private(directory / f"extract-{name}.json", json.dumps(body, sort_keys=True) + "\n")


def _write_private(target, text):
    temporary = target.with_name("." + target.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(text)
    os.replace(temporary, target)


def awaiting_ocr_queue(pipeline, documents):
    """``awaiting-ocr-v1`` lines for this run's ``AWAITING_OCR`` PDF originals.

    The shape is the OCR stage's queue input (``ocr_stage``, docs/RECORDS-OCR-STAGE.md):
    ``{"document_id", "sha256", "path"}`` with ``path`` the absolute pinned original
    under the records root. That stage takes PDFs only, so image originals stay
    counted as ``AWAITING_OCR`` but are not queued (``awaiting_ocr_images``).
    """
    lines, images = [], 0
    for document in documents:
        if document["outcome"] != AWAITING_OCR:
            continue
        subject = document["original_sha256"]
        try:
            original = pipeline._original(subject)
            form = document["form"] or pipeline._form(subject, original)
        except Exception:  # noqa: BLE001 - still counted; the queue only lists resolvable PDFs
            continue
        if form != "pdf":
            images += 1
            continue
        path = Path(original["storage_path"])
        if not path.is_absolute():
            path = Path(pipeline.root.path) / path
        lines.append({"document_id": "sha256:" + subject, "sha256": subject, "path": os.path.abspath(path)})
    lines.sort(key=lambda line: line["sha256"])
    return lines, images


def write_awaiting_ocr(root, lines):
    """Rewrite ``<root>/awaiting-ocr.jsonl`` (0600) with the current queue, even when empty."""
    _write_private(Path(root) / AWAITING_OCR_QUEUE,
                   "".join(json.dumps(line, sort_keys=True) + "\n" for line in lines))


def run(root, *, run_id, deadline, max_documents=DEFAULT_MAX_DOCUMENTS,
        document_timeout=DEFAULT_DOCUMENT_TIMEOUT, clock=time.monotonic, pipeline_factory=None,
        native_workbook_policy=None, ocr=None):
    """Advance pending extraction under the root lock; return the outcome summary.

    ``deadline`` is a ``clock()`` value. No document starts unless it can get at
    least ``MIN_DOCUMENT_SECONDS`` before it. ``max_documents`` caps parse attempts
    only: documents classified without a parse (images, unsupported forms, missing
    parsers) and held documents are counted but never use the budget, so they cannot
    starve parseable documents behind them. Parseable documents left unstarted when
    the budget or ``max_documents`` runs out are coded ``DEFERRED_RUN_BUDGET``.
    ``native_workbook_policy`` is a ``workbook_policy.NativeWorkbookPolicy`` from
    trusted startup (``load_native_workbook_policy``), else XLSX stays held.
    ``ocr`` (Phase 2, T6) is ``{"tools", "tool_signature", "vision"}`` from the operator stage
    plan: image-only PDF pages then go through per-page Tesseract and, when ``vision`` names a
    local adapter (``extract.vision``), empty or low-confidence pages through it. Without it
    those documents stay ``AWAITING_OCR`` exactly as in Phase 1.
    Raises ``RootNotInitialized`` (nothing created) and ``run.RunError``
    (``root_locked``).
    """
    from . import run as records_run

    root = Path(os.path.abspath(root))
    if not (root / "ledger.sqlite").is_file() or not (root / "intake" / "intake.sqlite").is_file():
        raise RootNotInitialized()
    if pipeline_factory is None:
        ocr = ocr or {}
        pipeline = records_run.Pipeline(root, native_workbook_policy=native_workbook_policy,
                                        ocr_tools=ocr.get("tools"), ocr_tool_signature=ocr.get("tool_signature"),
                                        ocr_vision=ocr.get("vision"))
    else:
        pipeline = pipeline_factory(root)
    ocr_enabled = bool(getattr(pipeline, "ocr_tools", None))
    missing = missing_parsers()
    outcomes = {code: 0 for code in OUTCOMES}
    documents, new_units, new_pages = [], 0, 0
    with records_run.root_lock(pipeline.root.path):
        pipeline.recover()
        subjects = pending_subjects(pipeline)
        stage = None
        attempted = unexpected = 0
        try:
            for subject, preserved in subjects:
                form = None
                remaining = deadline - clock()
                held = held_outcome(pipeline, subject, ocr_enabled=ocr_enabled) if preserved else None
                if not preserved:
                    outcome = PRESERVE_PENDING
                elif held is not None:
                    outcome = held  # counted every run, never parsed again
                elif remaining < MIN_DOCUMENT_SECONDS:
                    outcome = DEFERRED
                else:
                    form, outcome = classify_form(pipeline, subject, missing=missing)
                    if outcome is None and attempted >= max_documents:
                        outcome = DEFERRED
                    elif outcome is None:
                        attempted += 1
                        if stage is None:
                            stage = pipeline._open_stage_run()
                        timeout = int(max(1, min(document_timeout, remaining - 5)))
                        outcome, form, units, pages = extract_one(pipeline, stage, subject,
                                                                  form=form, timeout=timeout)
                        unexpected += outcome in (ERROR, NOT_ACCEPTED)
                        if ocr_enabled and outcome == EXTRACTED:
                            resolve_ocr_released(pipeline, subject)
                        new_units += units
                        new_pages += pages
                outcomes[outcome] += 1
                documents.append({"original_sha256": subject, "form": form, "outcome": outcome})
        finally:
            if stage is not None:
                pipeline._close_stage_run({"stage": "extract", "outcomes": outcomes})
        write_private_list(pipeline.root.path, run_id, documents)
        queue, images = awaiting_ocr_queue(pipeline, documents)
        write_awaiting_ocr(pipeline.root.path, queue)
    return {"documents": len(subjects), "outcomes": outcomes, "missing_parsers": missing,
            "attempted": attempted, "unexpected_failures": unexpected,
            "new_units": new_units, "new_pages": new_pages,
            "awaiting_ocr_queued": len(queue), "awaiting_ocr_images": images}
