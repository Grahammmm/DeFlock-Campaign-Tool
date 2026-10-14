# Bounded contextual runtime integration

This integrates PR84's unchanged pure context/applicability helper and synthetic
tests into the manual canonical records pipeline. It is source-only and does
not close ARCH04/05 for deployed or unattended operation.

## Implemented behavior

Before manual catalog/detect/review/compare/privacy reuse, the runner reconstructs
a context snapshot from current accepted extraction, its authoritative unit
inventory, canonical original/occurrence ancestry, request/agency joins and
recorded receipt declarations. Full ordered rule/package content, actual prompt
and adapter source hashes, redaction options and declared model configuration
bind the revision. Model aliases are explicitly unresolved, not immutable model
revision evidence.

The existing stage_artifacts table stores immutable snapshot bytes; ledger_meta
stores their current hash. This adds no schema or parallel request registry.
Catalog and analytical outer envelopes bind stage-specific identities. Original
extraction is reused only when its current acceptance and installed-code identity
match; context changes do not duplicate preserved originals or extracted bytes.

Under the same root lock used by publication, affected proposal approvals are
revoked and committed before the first invalidation. Every affected analytical
stage is invalidated through the protected controller. The pointer updates only
after invalidation completes. Crashes retain the prior pointer and revoked
approval, so the next attempt can reconcile again. Receipts, content and event
history remain intact. Invalid snapshot pointers also revoke affected approvals.

Receipt timestamps remain source declarations. Conduct, receipt-calendar,
disclosure and retention date roles remain unknown until a trusted source-date
adapter resolves them. The CLI event-date value is retained as a declaration,
never proof of conduct. Unknown conduct dates suppress dated model narration;
detector-only drafts still require independent factual review. Empty matched
duties and unresolved applicability produce denied comparison receipts rather
than inapplicability. Unresolved comparison content cannot pass its validator.

## Remaining holds

- The unattended runner overrides stage selection. Its bounded preselection,
  resume and shared-budget contextual reconciliation are not wired by this patch.
  This is a manual-runner integration, not full ARCH04/05 closure.
- No trusted source-date/applicability provider or authenticated acceptance seam
  is introduced. Historical/multiple-duty dates remain blocked, even when the
  pure helper can structurally describe them.
- Canonical joins are bound declarations, not independently verified truth.
  Multiple delivery contexts are composed into one revision; simultaneous
  context-specific completions and reviewed date bases are not implemented.
- Exact module/prompt hashes are recorded; configured model aliases remain
  unpinned and cannot establish verified model identity.
- Recovery profiles still need reviewed mapping of these existing ledger
  artifact/metadata dependencies. No live restore or activation is authorized.
- Independent source review and CI are required before any merge. PR87 queue
  fencing and PR86 normalization/detector integration remain separate work.

## Synthetic validation

The affected context, run-slice, catalog, ledger, runtime, safety and retirement
suite passed 202 tests with zero skips on Linux Python 3.12.3. New runtime cases
cover rule text and prompt changes, repeated-byte contexts, unknown dates,
shared-lock enforcement, immutable snapshot reuse, corrupt-pointer revocation,
and revocation-before-crash/replay. Privacy tests retain real synthetic loopback
model calls behind a test-only preverified-date precondition; production has no
date-verification override.
