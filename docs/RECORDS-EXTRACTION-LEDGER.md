# WP4 canonical extraction adapter

Interface version: `extraction-ledger-v1`. This optional module imports the actual
WP1 store as a dependency. It does not copy its schema, change the extraction
routes, edit the stage registry, run OCR, or process the corpus.

```python
from campaign_tool.records.extraction_ledger import ExtractionLedgerAdapter

adapter = ExtractionLedgerAdapter(
    database=private_canonical_ledger,
    evidence_root=private_immutable_evidence_directory,
)
result = adapter.enroll(
    original_path=canonical_original_path,
    receipt_path=extraction_run_directory / "extraction.json",
    receipt_sha256=expected_extraction_receipt_hash,
)
```

The expected hash comes from the trusted extraction operation's returned receipt,
not from an untrusted record choosing its own authorization. The receipt file is
hashed exactly as written; the extraction route adds the receipt hash only to its
returned dictionary, so do not rewrite the file to include it.

## Verified bindings

The canonical original must already exist with a storage path and at least one
occurrence. Its file and the extraction run's pinned `input` are streamed and
checked against canonical hash/size. Files must be regular, current-actor-owned,
private and free of symlink aliases. The receipt must name that original/run,
use the known route adapter version and retain `review_status: not_reviewed`.

The adapter preserves exact receipt, parser metadata and units JSONL bytes in a
private SHA-256 evidence store. Every unit must match its receipt representation;
its exact JSONL bytes and UTF-8 text have separate hashes. Parser name/version/
components must agree between the receipt and derivative metadata; unknown parser
versions fail rather than becoming verified provenance. These are bindings to
parser metadata, not independent attestation of the executing runtime image.

Page states are recomputed with the existing route's page-manifest function and
must match all receipt page fields: method/version, text hash, denominator,
confidence, visual hold, missing pages and partial/blocked reasons. PDF receipts
need an explicit page denominator. OCR derivatives are hashed, bound to their
selected units and compared to the original page denominator; unknown OCR tool
version and visual-review holds remain explicit gaps. Child blobs are checked
against their declared hash/size. The adapter does not enroll those children as
independent originals or guess their native source relationship.

## Canonical transaction and history

Evidence files are fsynced and stored without overwrite before one canonical
transaction inserts the import, delivery run, versioned units and historical page
rows. A crash may leave reusable content-addressed files, but never a partially
committed unit/page import. Replay verifies exact manifests, original metadata,
canonical units, page history and current page projection before acknowledging.

Each receipt has a distinct extraction version. A parser or content change creates
new unit IDs tied to that version; old units and page receipts remain preserved.
Replaying an old receipt does not make it current. Reusing one receipt after any
bound artifact changes is rejected. Adapter code identity is included in the
manifest; code upgrades need explicit reconciliation instead of quietly accepting
old runs under changed behavior.

New tables are additive and namespaced: `extraction_adapter_schema`,
`extraction_adapter_imports`, `extraction_adapter_pages` and
`extraction_adapter_current`. History rows reject update/delete/replacement. The
main WP1 schema is accessed only through its checked store context.

A candidate can become the current pending extraction view only while extract
and downstream stages are all pending. Active/accepted stages hold the candidate
for explicit invalidation through the trusted stage controller. Existing page
state without this adapter's history binding is also held and preserved. The
current page projection is replaceable only because every prior projection is
already retained in immutable version history. Canonical units carry status
`candidate_extracted`; consumers must use the selected import's manifest/unit IDs,
not concatenate units from every historical version.

## Stage authority and runner result

This section describes enrollment alone. The separate, fixed installed adapter
in `extraction_validation.py` now implements the deliberately narrow functional
acceptance paths documented in `RECORDS-EXTRACTION-VALIDATION.md`. Enrollment
itself still grants no acceptance, and the additional validator must be configured
by trusted startup with the pinned WP1 implementation.

WP1's stage contract provides a trusted installed validator registry but ships no
production extraction validator. This adapter therefore grants **zero stage
promotions**. Pending extraction receives reason
`trusted_extraction_stage_adapter_unconfigured`. Existing active/accepted states
are not reset, and no review, comparison or privacy state is completed.

`validate_and_promote()` explicitly refuses. A future installed extraction
validator must verify these artifacts plus accepted preservation, runtime identity,
coverage and holds, then use the WP1 installed runner's set-content/claim/promote
API with its live lease and exact prerequisite hashes. No submitted receipt may
register a callable or select validator authority.

`enroll()` returns `interface_version`, `import_id`, `run_id`, original/receipt/
manifest hashes, unit/page counts, `reused`, selection state, gaps, and zero stage/
review promotions. `extract_acceptance: pending_trusted_validator` states this
adapter's acceptance limit; it does not overwrite any older accepted stage.
Selection is `current_pending_candidate`, `historical_candidate`,
`held_existing_stage` or `held_unbound_existing_pages`.

The private manifest is stored under its hash in the evidence directory and in
the canonical import row. It contains the exact artifact paths/hashes, versioned
unit IDs and page records. The runner can queue those inputs for the trusted
extraction gate, then catalog only after canonical prerequisites permit it.
Run IDs are `extraction-enroll:<import-id>` child operations, not independently
attested parent-run identities. Unknown agency/policy/join metadata is not filled.

## Bounds and tests

Defaults in this slice: 1 GiB original streaming limit, 16 MiB receipt/metadata,
64 MiB derivative file, 1 MiB unit line, 10,000 units/pages/children, 128 MiB total
captured artifact/unit bytes. Every child is bounded by the remaining aggregate
budget before reading. These are adapter limits, not parser sandbox/disk quotas.
A larger production requires an explicit batching/limit decision, not silent
truncation or a false complete count.

```sh
RECORDS_WP1_SOURCE_ROOT=/trusted/wp1-candidate \
  python3 -B -m unittest tests.records.test_extraction_ledger -v
```

The overlay only extends the test import path; WP1 files are not copied. Tests
use tiny synthetic originals, including one real text-route invocation, partial
PDF/OCR metadata fixtures, replay/mutation checks and interruption at every commit
boundary. No real corpus, OCR activation, stage acceptance, external requests,
commits, pushes or deployment are performed. Parent review and combined runner
integration remain required before activation.
