# Read-only structured source resolution checkpoint

## Scope

`campaign_tool.records.detectors.sources.resolve_sources` is a trusted local
library boundary, not a CLI, normalizer or stage adapter. The host supplies its
configured canonical ledger, original root, derived root and explicit primary/
support SHA-256 IDs. No paths, callback, authority flags or source text are taken
from submitted detector JSON.

The resolver uses one SQLite read-only transaction for accepted preserve/extract
receipt CAS, latest accepted content, current extraction import/manifest, every
declared unit and fresh `canonical_joins`. It verifies original bytes and size,
installed extraction code identity, preservation-input binding, manifest hashes,
unit identity/ordinal/payload/text/locator/kind provenance and configured-root
confinement. It reads all units in each declared inventory; it does not use the
runner's selected-text limit or silently fall back to historical extraction.

Source artifacts are checked with secure no-symlink opens, private ownership,
byte bounds and before/after file metadata. No models, parser execution, ledger
writes, registration, promotion, invalidation or publication occur here.
The connection is application read-only; this is not proof of a genuinely
read-only filesystem or complete all-writer quiescence.

Limits are explicit: at most16 originals,10000 units,8MiB per read artifact
and32MiB aggregate payload/metadata reads and serialized result. Larger scope
fails closed and needs a separately reviewed operation-specific design; no caps
are raised automatically. The snapshot returns no original/derived filesystem
paths. Returned unit payloads are private local data and must never be exported
to cloud models or public receipts.

## Meaning of the result

A frozen SourceSnapshot binds declared original inventories, receipt/import
identities, exact source bindings, payloads, fresh join rows/revision and adapter
code hashes. Its JSON properties return independent copies. Re-resolve before
use; hashes establish bytes at read time and do not prevent later filesystem
changes. A single ledger snapshot does not atomically freeze the filesystem.

`support_scope=explicit_originals_only` means exhaustive over declared accepted
extractions, NOT proof that every required supporting document was enumerated.
Multiple typed agencies remain present; the resolver does not select an agency
or repair inconsistent request/agency evidence. Typed ledger associations remain
source declarations, not independent factual review.

No record type, event kind, conduct/retention date, policy adoption or legal
applicability is inferred from headers, labels, acquisition/production dates,
CLI arguments or today's date. No normalized units or detector policy inputs
are emitted. Without a normalizer all seven pure detector outcomes remain
blocked; ARCH10 runtime integration remains unfinished.

## Required trusted date/applicability handoff, still unavailable

An installed, authenticated provider must independently establish:

- Date role (conduct/receipt/disclosure/retention), event kind and jurisdiction-
  resolved calendar interval; source SHA, accepted unit/artifact and exact locator.
  Missing endpoints or competing dates remain unknown; retention needs both ends.
- Exact historical rule/policy bytes and revision; effective interval and adoption;
  source-bound jurisdiction, regulated role and policy-adoption scope decisions.
  Draft package flags and source-controlled `version_verified` booleans cannot
  establish provider authentication.
- Exact source snapshot/context revision and decision receipt identity. Changed
  support, join, normalization, date, rule or scope inputs must enter PR96's
  context identity and durably revoke approvals before protected invalidation.

Existing `context_revision.event_context/resolve_applicability` supplies conservative
structural date/scope checks; it does not authenticate an assertion's issuer.
This PR creates no new trust registry or substitute provider. Full normalizer,
support-dependency closure, trusted dates/applicability, protected detect receipts,
unattended selection/resume/budget wiring and publication review remain holds.

## Synthetic evidence

Base main5cdff835, Linux Python3.12.3. No corpus, credentials or network calls.
Fifteen new actual-canonical intake cases plus existing structured boundary/
adapter cases:39 passed, zero skips. Positive explicit primary/support resolution,
missing/changed support, omitted current import, tampered unit bytes/locator,
symlink/outside-root refusal, bounds, fresh join changes and ambiguous agencies
are covered. A concurrent WAL writer changes an agency between source and join
reads: the current read snapshot stays consistent and the next resolution changes
its revision. Missing/ambiguous source date assertions remain unresolved.

The first fixture run reused a single-run Pipeline while adding support; three
setup cases failed. Constructing a fresh Pipeline for the second run corrected
the fixture; the focused case and subsequent full39-case suite passed. Final code-identity
hardening initially bound a test wrapper module instead of the installed join
adapter; the isolation test caught that revision-only mismatch. Pinning the
installed module directly corrected it; final affected checks are in the PR.

## Recovery/rollback checkpoint reusing PR92/99

The existing release-manifest/schema-pin, backup-publication compatibility,
selected v2 recovery and publication/provenance cases passed again on this base.
Thirty-one existing cases passed, including two historical-byte rollback
refusal checks. Exact selections are recorded in the PR validation receipt.
No recovery implementation or replacement collector was added.

Covered: synthetic two-database roundtrip/replay, quiescent WAL isolated restore,
missing quiescence/root-lock-only/unknown-store/reference-omission rejection,
target-inside-source rejection, trusted manifest pin, corrupt archive/interruption
failure, correction rollback, tampered/missing manifest and symlink history,
interrupted rollback recovery, retained proposal provenance.

These temporary synthetic stores do not qualify the active runtime rollback,
establish operational dependency closure, stop all writers or authorize activation.
The existing release plan's live gates remain unchanged.

## Independent review repair cycle 1

Only the two source-review findings are changed. Primary and support originals
now use the shared ledger_catalog.scope_excluded policy, rejecting both excluded
scope labels and excluded_unrelated_personal before opening their bytes.
The resolver's local descriptor walk retains O_NOFOLLOW on each directory and
the final component, adds O_NONBLOCK at final open, and validates a regular
descriptor before buffered reads. Shared extraction/intake code is unchanged.

Three new regressions cover six primary/support exclusion variants with a
never-open assertion, plus substituted original/unit FIFOs in subprocesses
with three-second deadlines. Both FIFO variants timed out on the reviewed head;
the repaired resolver suite passes18 tests, including existing symlink refusal.
Initial exclusion setup attempted immutable metadata updates and was corrected
to insert fresh unaccepted synthetic declarations; no immutability guard bypass.
No new semantic, date, applicability, recovery or activation authority.
