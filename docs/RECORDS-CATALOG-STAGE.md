# Trusted, opt-in catalog-stage adapter

This new module is separate from the board, catalog exporter and private
snapshot-pointer implementation. Importing it registers nothing. Ordinary
build/export still promotes no stage. A catalog completion records a structured,
evidence-bound inventory entry, not independently reviewed facts or publication
approval. All metadata remains candidate; unknowns and proposed low-value remain
explicit. No agency is inferred from a filename, keyword, or language model.

## Startup contract

Call catalog_stage.register_catalog_adapter(database, private_artifact_root)
during trusted application startup. It returns a versioned installed adapter ID
bound to that database and artifact root. The callback is fixed engine code;
neither input cards nor receipts choose a validator, profile, path root or run.
The adapter uses WP1's trusted installed-validator registry because WP1 currently
has no separate public registration function. Existing entries are never replaced.

Configure the WP1 profile once with all required installed adapter IDs before
installed_runner binds a run. Catalog can be included with preservation and
extraction at that point. A preservation-only bound run cannot be expanded
mid-run: profile substitution is rejected. Do not mutate WP2's active profile.

catalog_run_factory(database, private_artifact_root, run_id=..., owner=...,
profile_id=..., engine_version=..., config_sha256=...,
additional_adapters=None) provides a convenience factory. It registers catalog,
combines other explicitly installed adapter IDs, configures the immutable
profile, and returns CatalogStage backed by installed_runner. It never creates
or finalizes a run row. The runner must already be a live matching records-runner
or stage-runner. A separate new catalog-only run can use previously accepted
production extraction. Production cannot consume synthetic prerequisite receipts.

For application composition, use CatalogStage(CatalogAdapter(...), runner) with
a runner already configured with this adapter. Testing callbacks remain WP1
test-only and do not certify production. WP1 or WP2 files are not modified.

## Input and exact evidence

process(private_card_artifact, expected_sha256, author_id=..., tier="A") is the
explicit mutating operation. Its strict JSON input contains only:
schema="catalog-card-evidence-v1", subject_sha256, metadata, supports, field_support.

Each support has unit_id, source_sha256, locator, artifact_sha256 and a nonempty
literal quote. It must match an existing canonical unit, source and locator;
status must be ok with parser/version and text hash. The private artifact bytes
and text hash must match, and the quote must occur in the text. A primary-original
support is mandatory. Schema-only cards or invented quotations do not pass.

Supported artifacts are UTF-8 text units (unit_type=text), or a structured
unit_type=catalog-proof-json with schema=records-unit-v1, original_sha256,
locator and text. Arbitrary historical JSON is not guessed into this format.
A captured/unverified unit remains blocked until an extraction adapter supplies
verified parser provenance and an exact supported artifact.

Metadata uses the existing strict candidate-field validator, including date
and score bounds. Existing display-evidence entries remain required for known
titles/types/dates/summaries. Every nonempty metadata field must also map in
field_support to verified support unit IDs. This binds candidate interpretations
to actual evidence; it does not prove the interpretation or replace independent
review. Low-value classification never closes a record.

Typed canonical agency/request joins must have verified support locators and
consistent canonical agency/request identities. Hinted joins are not attribution.
A cross-agency document retains each supported typed relationship.

## Stable per-original classification

The canonical content envelope contains only normalized per-original metadata,
original identity/length, typed relationships and sorted exact support hashes,
quotes and parser versions. No global counts, run IDs, card/stage status,
timestamps or artifact location enters this classification hash. Reformatting
or moving the incoming card does not reopen every record. Changed classification
or supporting evidence creates a new per-original revision through WP1.

Before promotion, exact input-card bytes are preserved privately in
catalog-card-artifacts/<sha256>.json, never overwritten. The promotion receipt
points to these bytes. The installed validator rereads them, verifies the unit
artifacts again and recomputes the envelope under WP1's claim transaction.
Filesystem evidence and ledger primitives remain responsible for underlying
original immutability. Originals themselves are not recopied or reclassified.

A successful path registers content, claims catalog and submits a bound receipt.
It advances only catalog. Existing done content is reused only after domain,
current prerequisite, authority, author/tier and revision checks. Missing evidence
or accepted extraction returns blocked with canonical_stage_changed=false; it
does not invent a canonical blocked receipt. Runner/API errors remain explicit.

## Bounds and private storage

Private artifact root must be owner-only, outside every repository, with no
symlink/parent traversal. Paths come from trusted startup and canonical units,
not records. Card limit 256 KiB; at most 64 supports, 2 MiB per support and 16 MiB
total. Byte identity is checked before/after reading. No external network,
models, installs, timer, server, message, original deletion or publication.

For the pilot, the parent's corrected artifact location is outside the project
repositories; do not relax the outside-repository guard. The metadata board
location correction is separate from this adapter's evidence-root configuration.
No real promotion or sample import is performed by these tests.

## Acceptance limits

Synthetic tests exercise real WP1 profiles/claims and catalog-only advancement
using test-only preservation/extraction. Factory tests confirm installed adapter
binding and rejection of synthetic prerequisites in production. Independent
parent review, integration with WP2's combined startup profile and actual
extraction artifacts are still required. No broad backlog processing is needed
to test this interface.

Card capture uses a fully written/fsynced temporary file and non-replacing hard
link into its hash location. An interruption before publication leaves no partial
published card. Existing foreign live catalog leases are rejected before content
registration; WP1 still verifies the execution claim transactionally at promotion.
The orchestrator must retain its single-runner control lock. This adapter does
not replace WP1's underlying transaction or multi-process run coordination.

## Accepted-evidence drift and exact positions

Before retrying catalog work for a byte-bound submitted original, the adapter
revalidates its current accepted catalog receipt and preserved supporting card.
Confirmed hash, source, locator or classification drift invalidates catalog and
its dependent stages through WP1, preserving immutable prior receipts. A malformed
new submission cannot itself invalidate a healthy accepted revision. Permission,
I/O and unstable-read failures block the attempt without declaring proven drift.
This is a runner-time guard, not continuous monitoring or a replacement for the
application's single-writer control lock.

Locators must be bounded typed positions: page/line/item/paragraph/row/column
with a positive index, or explicit part/sheet/cell positions, in prefix or JSON
object form. Blank, null, whitespace-only or unsupported positions are rejected
before equality checks. Unknown positions must remain unresolved, not inferred.

## Strict WP4 TXT handoff

The separate catalog_wp4_support adapter accepts WP4 text_line units whose
canonical status remains candidate_extracted, but only under a current accepted
extract receipt with non-test WP1 production authority and the fixed installed
WP4 validator. It verifies current content/revision, claim/profile and preservation
receipt binding, current import membership (including extra-unit rejection),
exact bounded JSONL line hash, ordinal, locator, text hash and parser identity.
It then invokes WP4's fixed domain validator to recheck the entire source and
import denominator. Roots come exclusively from trusted startup registration.
No path or callback supplied in a receipt is selected for execution.

Unknown metadata stays unknown. No canonical unit is renamed or relabeled;
EML extraction, PDF support through this catalog reader, review and later stages
are not certified by a successful TXT composition. Missing installed validators
or acceptance are gaps, not inferred success. Code dependency changes require a
new reviewed pin manifest and acceptance run. The caller retains the canonical
single-writer lock throughout composed processing.
