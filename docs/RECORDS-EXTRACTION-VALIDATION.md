# WP4 installed functional extraction acceptance

Stable installed adapter ID: `records-extract-complete-v1`.
Content schema: `canonical-extraction-content-v1`, exact canonical UTF-8 JSON,
maximum 4 MiB. This is a functional extraction gate, never original-level visual
review, factual challenge, legal comparison, privacy clearance, owner readiness,
or publication. Existing `ExtractionLedgerAdapter.enroll()` remains pending by
default. No production records are promoted by the tests.

## Combined runner contract (WP1 / WP2 handoff)

Trusted startup imports the fixed installed module. Receipt data and job payloads
must never be used as startup configuration or dynamic Python import paths.

```python
from campaign_tool.records.extraction_validation import (
    ADAPTER_ID, ExtractionStageAdapter, install_extraction_validator,
)
from campaign_tool.records.ledger import stages

install_extraction_validator(
    database=trusted_database,
    evidence_root=trusted_extraction_cas,
    original_root=trusted_original_storage_root,
)
stages.configure_installed_profile(
    trusted_profile_id,
    engine_version=trusted_engine_version,
    config_sha256=trusted_config_sha256,
    validators={
        "preserve": already_installed_preservation_adapter_id,
        "extract": ADAPTER_ID,
    },
)
# An existing running stage-runner/records-runner row must bind this exact
# engine/config. No implicit run, owner, profile, or preservation acceptance.
extract = ExtractionStageAdapter(
    database=trusted_database,
    evidence_root=trusted_extraction_cas,
    original_root=trusted_original_storage_root,
    run_id=trusted_run_id,
    owner=authenticated_owner_id,
    profile_id=trusted_profile_id,
)
result = extract.accept(canonical_extraction_import_id)
```

No arbitrary callback, receipt path, executable, registry ID, owner/run/profile
or stage override is accepted by `accept`. It loads the canonical current import,
constructs a content envelope, uses the configured runner to register content,
claims `extract`, and submits its exact receipt through WP1. The original must
already have trusted accepted preservation. WP1 rejects synthetic prerequisite
acceptance for installed production runners. Missing preservation is not repaired
or forged. The bridge does not invent a preservation validator.

WP1 currently lets installed application modules populate `_INSTALLED_VALIDATORS`.
This module is that fixed application code and registers exactly the ID above.
Different roots/database or a different implementation under that ID are rejected;
one database/root binding per process is intentional. Registry installation and
profile configuration must not be exposed as untrusted submission CLI operations.
No WP1 or WP2 source is copied or modified. Parent/Bacon should bind this ID in
the combined runner's trusted startup profile rather than passing a callback in a
stage receipt. Missing registry support fails closed.

## What is verified

- Current canonical import identity, source receipt hash, immutable manifest hash,
  enrollment run and occurrence binding; not a historical candidate selected by a
  submitter.
- Canonical original storage path constrained to the installed private root;
  actual source size and SHA-256, private ownership/modes and symlink checks.
- Manifest and every accepted derivative opened only at installed CAS root plus
  a validated hash. Receipt `run_path` and other submitted paths are never opened.
- Source receipt bytes, parser metadata and units JSONL hashes and sizes. The
  source receipt, metadata and manifest must all describe complete extraction,
  without issues, children or OCR ambiguity.
- Every unit's exact line payload hash, ordinal, text hash, parser/version,
  canonical locator, provenance, source units hash and canonical unit row.
- Exact parser-reported unit count, source-derived denominator, parser provenance,
  historical page receipts and current page-state bindings.
- The content envelope binds installed adapter/enrollment/parser source code
  hashes. Changed code or evidence cannot silently reuse approval.
- Only a `pass` with exact `full_text` coverage, the registered tool identifier and
  canonical import locator is accepted. A visual review claim is not accepted.

## Deliberately narrow supported complete paths

UTF-8 TXT, Markdown, log and reStructuredText: compare every emitted line and
line locator to canonical source bytes, including blank lines. Decode replacement,
empty/unknown coverage, dropped/reordered/changed lines and parser-version drift
are rejected.

Text-bearing PDF: re-run the installed existing extraction route in a private
scratch directory, with its subprocess resource limits and a fixed 20-second
bound. Independently compare all pages/units, exact parser/runtime identity, page
hashes and count. Missing/blank/OCR/error pages, partial output, changed content
and unknown runtime version are rejected. Text extraction does not establish
render fidelity; `needs_visual_review` remains unchanged even after acceptance.

Limits: 8 MiB original, 64 MiB aggregate accepted evidence, existing per-unit and
10,000-unit bounds, maximum 256 declared PDF pages, 4 MiB canonical envelope,
WP1's 64 KiB stage receipt cap. Sources outside this slice stay pending/blocked for
a future validator rather than being falsely accepted. CSV, spreadsheets, images,
MSG/EML, archives, OCR and nested children are not accepted by this validator.

The host's vendored PDF module is importable but its distribution version metadata
is currently unavailable. The real PDF acceptance test explicitly checks the
fail-closed enrollment and skips success in that environment. No package install,
fabricated version metadata or downgrade of provenance is performed. A normal
installed parser environment is required to exercise PDF success. This is a host
readiness limitation, not a claim that PDFs have passed production validation.

## Replay, concurrency and default pending behavior

The hardened acceptance path compares the complete current canonical unit set,
not just the units named by a submitted manifest. Unexpected extra current units
are rejected; historical-unit inspection remains bounded. The content envelope
also binds `parser_components` and `parser_runtime_sha256` to the installed
parser implementation, rather than accepting a receipt's parser labels as proof.

A competing live extraction lease is rejected before content registration can
change attribution or invalidate that lease. This requires the actual WP1
transactional stage-controller helpers from the pinned dependency; a copied or
older partial stage implementation is not an equivalent dependency.

Enrollment remains a candidate operation. When WP1's stage controller is present,
enrollment no longer directly writes even the pending-stage reason: authority is
left to WP1 and the pending reason remains explicit in its result/manifest. It
still does not promote any stage.

The bridge revalidates original/CAS/units before attempting exact accepted-receipt
replay, even though WP1 can skip its domain callback for exact unchanged replay.
WP1 ties new acceptance to a content revision, claimed lease, run/owner/profile,
and prerequisite hashes in a single transaction. Failures do not create accepted
receipts. Historical candidates cannot be chosen for current acceptance. Existing
superseded receipt/active revision conflicts need the controller's explicit
invalidation/supersession path, not an automatic overwrite by this bridge.

Any PDF reparse is bounded, but occurs during domain acceptance; a dedicated
runner is appropriate. This small-sample implementation is not a throughput
benchmark. Retain a later queue/performance acceptance gate before bulk use.

## Synthetic acceptance evidence

Tests use the actual WP1 overlay and installed validator implementation. Successful
stage transitions are made with `testing_runner`, retain `test_only` authority and
assert zero verified seven-stage/end-to-end completions. Separate installed-profile
checks prove fixed registration, explicit startup identity, callback rejection,
immutable binding and refusal to accept a synthetic preservation prerequisite.
They do not claim a production installed preservation-to-extraction run passed.

No real corpus promotions, migrations, schedules, external requests, package
installs, deployments or website changes occur. The parent integration must add
its actual installed preservation adapter and run the combined acceptance suite.
