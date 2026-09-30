# M2 local OCR slice

`campaign_tool.records.extract.ocr` is an offline, page-scoped helper for
already preserved PDF blobs. It does not activate scheduled intake, alter the
intake ledger, run the private corpus, or certify a page reviewed. The caller
supplies an exact source SHA-256, private intake/output roots, and an
operator-pinned tool signature. A safe 1-64 character attempt ID defaults to
`initial`; a new ID allows a later attempt without changing the actual tool
signature. Selected pages and sticky fidelity holds are explicit.

Read-only readiness check (no installation or evidence processing):

    PYTHONPATH="$PINNED_PARSER_BUNDLE:$PWD" python3 -m campaign_tool.records.extract.ocr --doctor

The doctor imports `pypdf`, finds `ocrmypdf`, `tesseract`, and `pdftoppm`,
and reads `ocrmypdf --version`, `tesseract --version`, and `pdftoppm -v`
labels only when present. It reports exact missing and version-unverified names.
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
`pdftoppm`, creates a searchable derivative and sidecar via OCRmyPDF, and
independently probes Tesseract word TSV confidence. Sidecar text and TSV
confidence come from separate OCR passes; confidence is a triage estimate,
not a calibrated probability or word-by-word attestation. Page receipts bind
source SHA, 1-based page locator, method/tool/attempt identity, confidence,
derivative hashes, and explicit extraction, fidelity and review states.

A confidence below threshold is `visual_check_queued`; a higher score remains
`ocr_text_unreviewed`. Empty text, absent confidence, local tool failures,
page text-probe errors, writer errors, and cleanup errors have blocked
dispositions. The CLI reports page counts, statuses, receipt IDs and readiness,
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

Synthetic injected tests in `tests/records/test_ocr.py` never call real OCR
executables. They cover retry history, index reconciliation, concurrent
holds, page-failure continuation, tamper/symlink rejection, doctor and owner
permissions. No private records are fixtures.
