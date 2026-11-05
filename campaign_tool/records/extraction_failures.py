"""Fixed-vocabulary projection of one extract run's per-document outcome list (REC-AUTO-2).

``build_manifest(body)`` reads the body ``native_extract.write_private_list`` already wrote
(``records-extract-outcomes-v1``) and returns ``records-extract-failures-v1``: every
non-EXTRACTED row with a fixed ``reason_code`` and a ``disposition``. It is pure: no SQL,
network, model or file access, and it never copies an arbitrary key, an exception string,
a filename, a header value or a path. Any code outside the allowlists below becomes
``unlisted``. The original SHA is private identity and stays in the private file only.
It is a diagnostic handoff, not text input and not approval authority.
"""
import re

SCHEMA = "records-extract-failures-v1"
OUTCOMES_SCHEMA = "records-extract-outcomes-v1"
EXTRACTED = "EXTRACTED"
UNLISTED = "unlisted"
LEGACY = "legacy_failure_detail_unavailable"
SHA_PATTERN = re.compile(r"[0-9a-f]{64}")

OUTCOME_CODES = ("EXTRACTED", "AWAITING_OCR", "CORRUPT_DOCUMENT", "EXTRACT_INCOMPLETE", "WORKBOOK_POLICY_REQUIRED",
                 "PARSER_MISSING", "DECODER_NEEDED", "EXTRACT_TIMEOUT", "EXTRACT_NOT_ACCEPTED", "EXTRACT_ERROR",
                 "PRESERVE_PENDING", "DEFERRED_RUN_BUDGET")
# Producer codes (extraction_routes, extract.ocr, run.Pipeline, native_extract). Anything else is "unlisted".
REASON_CODES = frozenset({
    "ocr_runtime_unconfigured", "page_eligibility_unresolved", "unresolved_extraction_evidence",
    "aggregate_evidence_bound", "ocr_receipt_unverified", "extraction_timeout", "extraction_stage_error",
    "extraction_check_refused", "local_ocr_timeout", "local_ocr_dependency_or_output_missing",
    "no_text_after_ocr", "confidence_unavailable", "page_raster_bound", "fidelity_hold_requires_visual_comparison",
    "ocr_needed_or_blank_page", "ocr_visual_review_required", "decoder_needed", "unsupported_format",
    "decoder_unavailable_or_failed", "invalid_or_bounded_parser_output", "extraction_error",
    "workbook_cached_values_and_objects_not_reviewed", "workbook_policy_refused", "image_resource_limit",
    "preserve_pending", "deferred_run_budget", "parser_missing", LEGACY, UNLISTED,
})
ACCEPT_STATUSES = frozenset({"blocked", "partial", "missing", "refused", "failed", "error", "pending",
                             "incomplete", "unlisted"})
ACCEPT_PREFIX = "accept_status_"
REFUSAL_SOURCES = ("binding_error", "value_error", "exception", "accept_status", "form_resolution", "held_alert")
DISPOSITIONS = ("retry", "deferred", "hold")
MAX_ISSUE_CODES = 16
MAX_PENDING_PAGES = 1000
_NO_DETAIL_REASONS = {"PRESERVE_PENDING": "preserve_pending", "DEFERRED_RUN_BUDGET": "deferred_run_budget",
                      "PARSER_MISSING": "parser_missing", "AWAITING_OCR": "ocr_runtime_unconfigured"}


def fixed_code(value):
    """The value when it is in the fixed vocabulary (or a known accept_status_*), else ``unlisted``."""
    if not isinstance(value, str):
        return UNLISTED
    if value in REASON_CODES:
        return value
    if value.startswith(ACCEPT_PREFIX) and value[len(ACCEPT_PREFIX):] in ACCEPT_STATUSES:
        return value
    return UNLISTED


def _count(value):
    return value if type(value) is int and 0 <= value <= MAX_PENDING_PAGES else None


def _detail(row):
    """The safe existing detail fields of one row, validated; nothing else is copied."""
    detail = {}
    refusal = row.get("refusal_code")
    if "refusal_code" in row:
        detail["refusal_code"] = fixed_code(refusal)
        status = row.get("accept_status")
        detail["accept_status"] = status if isinstance(status, str) and status in ACCEPT_STATUSES else None
        source = row.get("refusal_source")
        detail["refusal_source"] = source if source in REFUSAL_SOURCES else UNLISTED
    issues = row.get("issue_codes")
    if isinstance(issues, list):
        detail["issue_codes"] = sorted({fixed_code(code) for code in issues[:MAX_ISSUE_CODES]})
    done = _count(row.get("ocr_pages_done"))
    pending = row.get("ocr_pages_pending")
    if done is not None and isinstance(pending, list):
        pages = [page for page in pending[:MAX_PENDING_PAGES] if type(page) is int and 0 < page <= 100000]
        detail["ocr_pages_done"] = done
        detail["ocr_pages_pending"] = pages
    return detail


def reason_code(outcome, detail):
    if outcome in _NO_DETAIL_REASONS and not detail.get("issue_codes"):
        return _NO_DETAIL_REASONS[outcome]
    code = detail.get("refusal_code")
    if code and code != UNLISTED:
        return code
    issues = [c for c in detail.get("issue_codes", ()) if c != UNLISTED]
    if issues:
        return issues[0]
    if outcome in ("EXTRACT_ERROR", "EXTRACT_NOT_ACCEPTED") and not code:
        return LEGACY  # never guess a cause from a bare top-level count
    if outcome in _NO_DETAIL_REASONS:
        return _NO_DETAIL_REASONS[outcome]
    return code or UNLISTED


def disposition(outcome, detail):
    if outcome in ("DEFERRED_RUN_BUDGET", "PRESERVE_PENDING", "PARSER_MISSING"):
        return "deferred"
    if outcome == "EXTRACT_TIMEOUT" or (outcome == "EXTRACT_ERROR" and detail.get("refusal_source") == "exception"):
        return "retry"
    return "hold"


def build_manifest(body):
    """Project ``body`` (a ``records-extract-outcomes-v1`` dict); raise ``ValueError`` when malformed."""
    if not isinstance(body, dict) or not isinstance(body.get("documents"), list):
        raise ValueError("extract_list_invalid")
    run_id = body.get("run_id")
    rows, by_outcome, by_reason = [], {}, {}
    for row in body["documents"]:
        if not isinstance(row, dict):
            raise ValueError("extract_list_invalid")
        outcome = row.get("outcome")
        if outcome not in OUTCOME_CODES:
            outcome = UNLISTED
        if outcome == EXTRACTED:
            continue
        sha = row.get("original_sha256")
        form = row.get("form")
        detail = _detail(row)
        item = {"original_sha256": sha if isinstance(sha, str) and SHA_PATTERN.fullmatch(sha) else None,
                "form": form if isinstance(form, str) and re.fullmatch(r"[a-z0-9_]{1,16}", form) else None,
                "outcome": outcome, "reason_code": reason_code(outcome, detail),
                "disposition": disposition(outcome, detail), **detail}
        rows.append(item)
        by_outcome[outcome] = by_outcome.get(outcome, 0) + 1
        by_reason[item["reason_code"]] = by_reason.get(item["reason_code"], 0) + 1
    return {"schema": SCHEMA, "run_id": run_id if isinstance(run_id, str) and len(run_id) <= 128 else None,
            "documents_listed": len(body["documents"]), "by_outcome": dict(sorted(by_outcome.items())),
            "by_reason": dict(sorted(by_reason.items())), "documents": rows}
