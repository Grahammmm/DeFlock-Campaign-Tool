# Prepared pipeline connection (candidate, not activated)

`runner.pipeline.PipelineConnection` connects the existing concrete WP2 canonical
mail backend, WP4 enrollment and installed ExtractionStageAdapter, and WP5
CatalogStage. Trusted startup supplies these preconfigured objects on one canonical
ledger, distinct authorized stage runs/profiles, and immutable `PreparedItem`
plans. No callback, module name, authority, model or arbitrary path is selected by
record data. This module registers no validators and creates no production profile.

Call `advance(preserved_result)` after successful existing WP2 preservation. It
never starts/finalizes/reopens runs, advances mail checkpoints, changes leases, or
replaces uncertain lifecycle outcomes. The existing controller remains responsible
for stored lifecycle intents and recovery. All canonical stage writes occur inside
existing real adapters, under WP1 prerequisites and claims. Replay revalidates the
same domain evidence; accepted counts come from the canonical stage query, not a
hook return or schema-valid receipt. Partial downstream failure leaves the verified
intake intact and reports each item separately. Pending EML extraction stays pending.

PreparedItem binds original SHA/path, real extraction receipt/path/hash, private
catalog artifact/hash and trusted author ID. Actual extraction wrapper execution and
catalog drafting precede this bounded handoff; this module does not pretend to
supply either capability. It performs no extraction from mail text itself. Real
wrapper artifacts and card evidence are checked by the installed WP4/WP5 validators.
Unknown agency/dates and proposed low value semantics remain with the WP5 validator.

Bounds: at most 20 subjects/plans, at most 1 MiB per original, admission budget
1..60 seconds between adapter calls. This is **not** hard mid-call preemption.
A trusted host supervisor/hard adapter deadline is still required before unattended
activation. No additional independent work journal or competing finalizer exists.
Exceptions are isolated per subject and returned as class codes, without raw private
paths. Preserve/extract/catalog completion is distinct from downstream stages.
Detect/review/compare/privacy remain explicit missing capabilities. `release_ready`
and `publication_ready` always remain false. No fresh-mail coverage is inferred.

New synthetic connection test composes the pinned existing tiny mail + TXT fixture,
not inherited full-suite classes. It instruments real calls only to supply the
trusted connection objects; no validator returns an unconditional successful result.
Existing harness defects, workflows, and acceptance tests are unchanged. Parent
review and pinned CI remain required; this is not a full pipeline release.

## Superseding constructor and subject-safety repair

WP4 has no `ExtractionStageAdapter.database` field. Bind to the actual
`extraction.runner.database` AND `extraction.validator.database`, with exact
installed StageRunner and validator types, adapter ID/validator tuple, non-testing
installed authority, and WP1's own current run/profile check. WP5 separately checks
its real catalog runner/database/validator tuple and distinct run. No alias or
synthetic authority is installed to repair compatibility. Recheck these bindings
on each advance. Existing WP2 lifecycle intents/finalization remain untouched.

A missing catalog object is an explicit capability gap; this permits genuine
first pending-to-accepted extraction without requiring a fabricated card.
Catalog artifacts are checked through WP5's private bounded read and strict decode:
exact SHA, schema, and the trusted plan's subject must match BEFORE any process
call. There is no preparation-error fallback into process. A wrong-subject card,
even with broken support, cannot enter WP5's invalidation path. A correctly bound
card still goes to the real process, preserving WP5's verified-drift handling.

The new tiny test invokes the connection before the fixture's FIRST installed
extraction acceptance. It asserts preserved=2/extracted=0/catalog=0 becomes
2/1/0, then the real catalog handoff becomes 2/1/1; EML stays extraction-pending.
Replay retains exact receipts; wrong-subject, malformed-schema, changed card hash,
failed extraction receipt, mismatched database and missing installed authority
are checked. These remain synthetic tests, not actual corpus or mailbox work.

### Installed catalog identity (supersedes bound-method equality)

The catalog factory legitimately constructs one adapter for the stage object and
another equivalently configured adapter for registry installation. Their bound
methods need not compare equal. The connection requires the runner's exact adapter
ID, the exact registered callable object, an exact CatalogAdapter bound owner, the
CatalogAdapter.validate implementation, and matching canonical database/private
root. WP1's current installed run/profile checks still apply. No callable alias,
replacement validator, synthetic authority, or registration is introduced.
