# Runner processing coverage

This integrates PR81's unchanged deterministic range/checkpoint API with the
current canonical extraction snapshot. The runner selects only the accepted
extraction import's ordered unit IDs, verifies the manifest hash and derivative
bindings, and retains missing, unreadable and capped units in the denominator.
Historical import rows cannot silently become current text.

Detect receipts use selected coverage over the authoritative inventory rather
than full_text over a capped selection. Review content records a redacted range
manifest and an initial checkpoint in the existing stage-content artifacts.
Automated narratives and challenges accept zero semantic-review ranges. Model
prompt omission remains an explicit limitation; a planned chunk is not an
emission receipt. The runner does not dispatch these chunks or authenticate
caller acceptance receipts. No automatic semantic resume is enabled.

The reusable API supports idempotent attempt receipts, interrupted emission,
JSON checkpoint restoration, stale source/context rejection and union counting.
Any future caller that accepts ranges must authenticate receipts and persist
updates atomically through the existing authority. A self-hash is not approval.
Factual/legal/privacy review and publication authority remain separate.

Denied challenge receipts may record zero coverage. Passing review, compare and
privacy receipts still require positive coverage, exact locators and all existing
independent review gates. This change creates no schema, registry, schedule,
model endpoint, corpus enrollment or runtime activation.

Mechanical emission resume is available through the checkpoint API: only completed
attempts cover emitted ranges, and interrupted chunks remain queued for emission.
Emission is not acceptance or semantic understanding, and output remains
review-required. Stale progress is rejected visibly rather than partially reused.
`run.py` still records `planned_only` until the W15B persistence integration;
this API neither stores checkpoints nor dispatches model calls.

Validation uses synthetic fixtures only. Current-extraction tests cover selection
caps, historical rows, missing/changed derivatives, redaction context drift and
zero-coverage denial. PR81's 32 tests retain oversized Unicode, hidden/empty units,
partial/interrupted attempts and stale/replayed checkpoint coverage. This is
processing bookkeeping, not semantic understanding or independent review.
