---
name: records-extraction
description: Extract Flock documents and OCR into provenance-bound candidates, validate functional extraction through the installed callback, and retain coverage and visual-review holds.
---

# Records Extraction

Read the common contract and **Extraction** stage. Use the pinned local parser environment and bounded routes. Account for every page, sheet, row, attachment and unsupported part; keep raw text, images and OCR private. Bind original bytes, units, artifacts, parser components and OCR receipts exactly.

Use `ExtractionLedgerAdapter.enroll(...)`, then the installed `ExtractionStageAdapter.accept(import_id)` under its trusted run/profile binding. Enrollment or parser success does not imply acceptance. OCR text and visual-check queues are not substantive review. Image digest repair does not authorize Pillow acceptance: retain `unsupported_complete_parser` until a separately reviewed installed validation policy supports it.

Preserve scope and gaps in the handoff to `records-content-review`; do not relabel an extracted batch as understood.

Shared versioned inputs, outputs, retry/failure rules and Hermes release gates:
[Records stage handoff](../../docs/RECORDS-STAGE-HANDOFF.md). Read it for this stage;
use the next skill only after its eligibility conditions are satisfied. These
repository skills require that shared document to travel with the skill bundle.
