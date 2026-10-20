# M2 local OCR slice

Per-page OCR in this module is the **only OCR path** in the engine's WP4
extraction (`extraction_routes`). The separate Phase 2 queue stage for
documents held as `awaiting_ocr` (OCRmyPDF first, local Qwen vision fallback)
is described in `docs/RECORDS-OCR-STAGE.md`; it writes only its own output
root and is not wired into WP4. WP4 has its own optional local vision fallback
after Tesseract (see "Local vision fallback (T6)" below). WP4's
`extraction_routes.extract()` no longer runs whole-document OCRmyPDF or writes
an `ocr.pdf` derivative; for PDF pages that are not natively `ok` it calls
`extract_image_only_pages` and binds each page row to that page's immutable
receipt. Page-level receipts keep exact 1-based page locators, per-page
confidence and per-page failures, so native-text pages are never replaced by
OCR output and every OCR result stays traceable to one page of the preserved
original. See `docs/RECORDS-EXTRACTION.md` for how WP4 records the receipts.

`campaign_tool.records.extract.ocr` is an offline, page-scoped helper for
already preserved PDF blobs. It does not activate scheduled intake, alter the
intake ledger, run the private corpus, or certify a page reviewed. The caller
supplies an exact source SHA-256, private intake/output roots, and an
operator-pinned tool signature. A safe 1-64 character attempt ID defaults to
`initial`; a new ID allows a later attempt without changing the actual tool
signature. Selected pages and sticky fidelity holds are explicit.

Read-only readiness check (no installation or evidence processing):

    PYTHONPATH="$PINNED_PARSER_BUNDLE:$PWD" python3 -m campaign_tool.records.extract.ocr --doctor

The doctor imports `pypdf`, finds `tesseract` and `pdftoppm`, and reads
`tesseract --version` and `pdftoppm -v` labels only when present. OCRmyPDF and
Ghostscript are no longer required: nothing consumed the searchable PDF they
produced. The doctor reports exact missing and version-unverified names and
absolute executable paths. Extraction invokes those doctor-resolved absolute
paths (placing their directories first on the child `PATH`) and records the
probed version labels in every page receipt's `tool_versions`.
The operator-provided `--tool-signature` is a declared pin, not independent
binary attestation. A passing doctor check is dependency readiness, not
full-pipeline, corpus, fidelity, or review acceptance. Propagate the private
parser bundle in `PYTHONPATH` for
both doctor and extraction. Doctor installs nothing and processes no evidence.

A manual batch invocation is available after local tools and permissions are
approved. Use private paths only. Omitted `--page` scans every PDF page;
`--hold-page N` records a sticky fidelity hold. A published failed attempt
cannot be rerun under the same ID. Use `--attempt-id retry-1` for a retry:

    PYTHONPATH="$PINNED_PARSER_BUNDLE:$PWD" python3 -m campaign_tool.records.extract.ocr \
      --intake-root "$PRIVATE_INTAKE" --sha "$SOURCE_SHA256" \
      --output-root "$PRIVATE_OCR" --tool-signature "$PINNED_OCR_VERSIONS" \
      --attempt-id retry-1

The helper reads the original by SHA-256 and never rewrites it. For an
image-only page it makes a single-page PDF derivative, renders it locally with
`pdftoppm`, and runs Tesseract **once** per page
(`tesseract page.png <base> -l <lang> txt tsv`). `sidecar.txt` is that run's
text output and `confidence.tsv` is the same run's word TSV, so the recorded
confidence describes exactly the recorded text. Confidence is a triage
estimate, not a calibrated probability or word-by-word attestation. Receipts
use schema `local-ocr-3`; receipts of any other version (including
`local-ocr-2`, whose text and confidence came from separate passes) are
rejected rather than reused. Page receipts bind
source SHA, 1-based page locator, method/tool/attempt identity, confidence,
derivative hashes, and explicit extraction, fidelity and review states.

Before rendering, a page whose MediaBox would exceed 40,000,000 pixels at
300 dpi is blocked as `page_raster_bound`. OCR children run with `RLIMIT_AS`
and `RLIMIT_FSIZE` bounds matching the intake worker.

A confidence below threshold is `visual_check_queued`; a higher score remains
`ocr_text_unreviewed`. Empty text, absent confidence, local tool failures,
page text-probe errors, writer errors, page-local publication rejections and
cleanup errors have blocked dispositions; later pages still receive receipts. The CLI reports page counts, statuses, receipt IDs and readiness,
never raw text. Symlinks in the source or any managed derivative path are
rejected. Existing output root, source, page and index directories must be
owned by the process and inaccessible to group and others; intake ancestors
need not be owner-only.

Published receipts, hash-bound manifests and separate `indexes/blocked/`
and `indexes/visual-check/` entries are immutable attempt history. Receipt
directory publication uses Linux `renameat2(RENAME_NOREPLACE)` and fails closed
where that no-replace primitive is unavailable. A new
attempt preserves the old receipt and index. The per-page mutable `current.json`
is the **local active disposition** and lists superseded receipt IDs. It is not
a campaign ledger or board adapter. Per-page locking serializes hold and
publication decisions. OCR rechecks for a hold immediately before publication;
a hold arriving during OCR takes priority over the unheld result. Hold markers
are atomically published and never released automatically.

If a crash occurs after a receipt is published but before its index or current
pointer is written, reconcile from published receipts without rerunning OCR:

    python3 -m campaign_tool.records.extract.ocr --reconcile \
      --output-root "$PRIVATE_OCR" --sha "$SOURCE_SHA256"

Reconciliation validates receipts, manifests and derivatives, recreates missing
history indexes, and refreshes the current disposition. It does not read
originals or prove review. A failed cleanup is recorded as a blocked recovery
receipt; leftover temporary artifacts remain private for operator inspection.
The current pointer is derived local state, not an independent review gate.

Every OCR page remains `not_reviewed` and fidelity `not_checked`. Sheriff
fidelity holds require documented visual comparison and a separate
owner-authorized release path. A blank produced field is not proof of a blank
native field. Do not publish derived PDFs or text directly. Owner-controlled
private storage, approved local tools, a bounded real-corpus run, and
downstream ledger/board wiring remain separate host work.

`load_page_receipt(output_root, source_sha256, page, receipt_id)` is the
read-only verified loader used by WP4 extraction and enrollment: it re-checks
the receipt, manifest and every artifact hash and returns the exact
`sidecar.txt` bytes.

## Local vision fallback (T6)

Tesseract always runs first. When the operator configures a local vision
adapter, each page whose verified `local-ocr-3` receipt is
`visual_check_queued` (low confidence) or blocked as
`no_text_after_ocr`/`confidence_unavailable` is sent to it **once**.
Fidelity-held pages and pages without a recorded raster are never sent.
`campaign_tool.records.extract.vision` passes the hash-verified `page.png` of
that Tesseract receipt to `argv + [page.png]` and reads UTF-8 text from stdout.
The engine never names a provider, URL or model endpoint. The Flock image's
command is `ocr_stage transcribe-png` (see `docs/RECORDS-OCR-STAGE.md`), which
talks to the loopback llama.cpp server. Output is an immutable
`local-vision-1` receipt under `<ocr_root>/vision/<sha>/page-N/<id>/` that names
the exact Tesseract receipt (id and SHA-256) it supersedes. A timeout, a failed
command, empty output, oversize output or non-UTF-8 output each becomes a
blocked receipt. A rerun under the same identity reuses the receipt and makes
no call.

Vision is configured by an owner-only, non-symlink `local-vision-config-v1`
file with exactly these keys:

    {"schema": "local-vision-config-v1",
     "argv": ["/opt/campaign-tool/venv/bin/python", "-I", "-m", "campaign_tool.records.ocr_stage",
              "transcribe-png", "--config", "<owner-only ocr-stage config>"],
     "engine": "qwen", "model": "qwen3.8:27b-thinkingcap-q6", "signature": "<operator pin>"}

Use it with `records run --ocr --ocr-vision-config FILE`, or in a stage plan
with `run_stages extract --ocr --ocr-vision-config FILE`. Both are off by
default, and Phase 1 stage plans keep native-only extraction. In the stage,
`--ocr` without installed tools fails as `EXTRACT_OCR_UNAVAILABLE`, a bad
vision file fails as `EXTRACT_OCR_VISION_CONFIG_INVALID`, and a vision file
without `--ocr` fails as `STAGE_CONFIG_INVALID`. None of these falls back
silently.

Vision text replaces the page's Tesseract text only when the vision receipt
produced text. Every OCR page row and unit records
`engine=tesseract|<vision engine>`. When a vision receipt exists, the row also
records `vision_receipt_id`, `vision_receipt_sha256` and `vision_status`, and
`method` is `local-vision-1` when vision text is used. Enrollment captures
each vision receipt as a closed CAS closure (`vision_receipt:<id>`,
`vision_asset:<id>:<name>`) and re-verifies it from CAS bytes, checking it
against the Tesseract receipt of the same page. Acceptance does the same: a
Tesseract-blocked page is acceptable only when bound vision text exists. The
accepted envelope lists `ocr.engines` per page and `ocr.vision_receipt_ids`.
The visual-review hold is unchanged. Vision text is machine output and
`not_reviewed`, like Tesseract text.

Synthetic injected tests in `tests/records/test_ocr.py` never call real OCR
executables. They cover retry history, index reconciliation, concurrent
holds, page-failure continuation, tamper/symlink rejection, doctor and owner
permissions. `tests/records/test_ocr_vision.py` covers the fallback with
injected OCR and vision runners: 10 clean scans make no vision call, empty and
low-confidence pages go to vision and the ledger records `qwen`, failure
dispositions, reuse, tamper and binding refusals, config loading and stage
wiring. No private records are fixtures.
