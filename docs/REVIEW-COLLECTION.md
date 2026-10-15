# C01: collection failures block readiness

The audit's two failure modes were filename shadowing and evaluating only the
valid receipt subset after collection errors. Collection now reads every existing
supported source: gate-reviews.json, factual-review.json, legal-review.json and
privacy-review.json. Source paths/hashes remain recorded. A source that cannot
be read has an explicit error and unavailable hash, replacing stale passing
collection output rather than leaving it usable without a failure marker.

Only canonically identical complete JSON receipt entries are deduplicated.
All fields participate. Distinct challenges, changed rationales/verdicts and
conflicting reviews remain visible; filenames do not assign or upgrade roles.
Malformed entries retain a source/index error; no receipt fields are inferred.
A source envelope's existing collection errors also survive recollection as
an unresolved failure, including malformed error-envelope types.

validate-findings and batch-report share review-envelope decoding. Nonempty or
malformed collection_errors block every affected finding with
review_collection_failed. The CLI --require-ready exits1; batch publication-ready
counts for that agency are0 and its errors include the collection failure.
Clean review arrays/envelopes and existing gate checks remain supported.
Collector/batch-report retain their reporting exit conventions; collector exit0
means a report was written, not that collection or readiness succeeded.

Regression fixtures use existing synthetic source-bound finding/review builders.
They test both filename orders, malformed challenges in all four files, malformed
JSON/envelopes, unreadable sources after prior passing output, prior collection
errors, exact deduplication and distinct challenges. Healthy controls still pass.
This changes neither review authentication/independence nor substantive/legal/
privacy approval. It grants no publication, live-data or runtime authority.

## Independent review repair cycle1

batch-report now treats external review read/decode/shape failures as collection
failures even when a finding has valid inline receipts. The affected findings
receive review_collection_failed and readiness0. The reviewer's three malformed
inputs are reproduced in a new test alongside unreadable-file handling and a
healthy inline-only control. Reporting exit conventions are unchanged.

Only the three changed gate-module engine_sha256 entries in the reviewed
records-gates import manifest are refreshed; historical source hashes and other
entries stay unchanged. The existing exact manifest approval pin is updated
using its same adjacent-literal annotations. Scanner logic, permitted paths,
detector rules, workflow checks and provenance validation are unchanged.
