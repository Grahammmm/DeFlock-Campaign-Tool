# Comprehension wiring: proposed handoff contract v1

Status: PROPOSAL / REVIEW REQUIRED. F-046-contract, task t_4e304183, run1089.
Author: sol-reviewer, actual gpt-6.1-sol / openai-codex.
This freezes the proposed file-routing interface only, pending independent actual
Opus5.5 review and adoption by operator Claude Code on G's Mac. It grants no
consumer dispatch, live run, stage promotion, installation or semantic acceptance.
Remote freshness is UNVERIFIED. Original Fable scope and all holds remain intact.

## Source identity and compatibility

Baseline: 93995a1a615f6aaca7bfb0fbeb39a7f3aa367997. Source and local import-closure
hashes are frozen in tests/fixtures/comprehension/source-pins.json. They cover the
package initializers, detector core/pipeline/persistence/migration, ledger
store/migration, unchanged host stage source and unchanged run-report schema.
The new schema is schemas/comprehension-handoff-v1.json, JSON Schema 2020-12.
It is versioned independently of all existing records and receipts.

Source-backed interfaces, NOT new formats:

    evaluate_structured(*, subject_sha256, normalized=None,
                        source_bindings=None, joins=None, rules=None, config=None)
    canonical_joins(connection, source_bindings)
    CanonicalJoins(source_bindings_sha256, rows_json, revision_sha256)

CURRENT_CORE_UNIT is a descriptive placeholder in pipeline.py's docstring, not
an exported Python symbol. The actual existing core unit has original_sha256,
locator, record_type, event_id, event_at where required, and fields. No new unit
class/schema is introduced here. See docs/RECORDS-DETECTORS.md and
STRUCTURED-DETECTOR-CONTRACT.md; exact behavior remains the existing core API.

The coordinator inventory is the existing list of source_bindings, each with
exactly unit_id, original_sha256, artifact_sha256, locator. Here its artifact hash
identifies extracted bytes, whereas the new envelope's artifact_sha256 identifies
the ORIGINAL document (the inventory's original_sha256). This distinction must
not be normalized away. All support originals, including policy, belong in the
coordinator inventory. No inventory may be constructed from authored units.

Units files contain lists of the existing normalization items:
{record: existing core unit, source: {unit_id, artifact_sha256}}. A future host
assembles structured-normalization-v1 with explicit mapper version, completeness
and source_inventory_sha256 = core.digest(source_bindings). That digest is of
canonical JSON WITHOUT a trailing newline. File SHA256 bindings below hash the
EXACT bytes, including any newline. Never interchange these identities.

WP6 rules remain {version, entries, holiday_tables}; entries retain detector,
agency_id, rule_id, version, source_sha256, effective_from, exclusive effective_to,
parameters and existing version_verified/applicability gates. Those last gates
are supplied trusted policy decisions, never inferred or granted by this manifest.

The ledger identity is canonical-ledger, schema_version=1, tied to the actual
v001.VERSION and SHA256 of the COMPLETE v001.SQL including appended triggers.
It is not a serialized CanonicalJoins object. Obtain a fresh joins snapshot from
the admitted copy via canonical_joins, never deserialize a record-controlled flag.
Any later incompatible source is a same-owner admission issue, not permission to
invent a new adapter or reseal these pins.

## Envelope fields and routing bindings

The producer writes comprehension-handoff-v1.json under an admitted immutable
input root. All fields are required; unknown fields and versions are rejected.

    schema_version: integer 1 (not boolean)
    run_id: nonblank opaque producer ID, preserved verbatim downstream
    source_manifest: root-relative path to producer manifest copy
    source_manifest_sha256: full lowercase SHA256 of its exact bytes
    inventory: {path, sha256} of coordinator-supplied source_bindings copy
    canonical_copy: {path, sha256, schema: canonical-ledger, schema_version: 1}
    rules: {path, sha256, version}, version exactly equal to existing rules.version
    artifacts: nonempty list, sorted lexically by original artifact_sha256
    publication_ready: false
    stage_promotions: integer 0
    review_required: true
    qualified: false
    activation_allowed: false

Each artifact row has exactly:

    artifact_sha256: original identity, unique in this list
    derived_receipt_sha256: exact receipt-file hash
    derived_receipt: {path, sha256}, equal to derived_receipt_sha256
    kind_receipt: {path, sha256} or explicitly null
    units: {path, sha256} or explicitly null
    status: ready | blocked | inapplicable
    reason: one of the defined routing reasons below

These paths bind already produced files; they do NOT authorize creation in the
input root. Future consumer outputs go to a separately admitted exclusive run
root, retaining the producer run_id and input identities in their receipts.
No consumer may substitute a similarly named file, regenerate a run ID or
repoint the frozen envelope. No output-root CLI is added by this slice.

Routing status is NOT a detector status or a source qualification:

    ready / inputs_bound: both kind_receipt and units required and hash-bound.
      This means files are available, not mapped correctly or semantically true.
    blocked / missing_kind_receipt: kind_receipt must be null.
    blocked / missing_units: units must be null.
    blocked / preserve_pending, unresolved_mapping, upstream_blocked:
      retain the upstream hold, even if some files exist.
    inapplicable / classified_non_target: kind_receipt required; a later consumer
      must validate the accepted classifier's evidence before excluding anything.

A derived receipt is required for every row; sources without one cannot produce
a valid handoff and remain blocked at producer admission. Missing required
inventory/canonical/rules invalidates the whole envelope, not a successful empty
batch. A valid empty replay of an already processed run is a consumer outcome,
not a newly fabricated empty inventory: retain the original handoff and record
zero new work separately from missing/failed evidence. Current v1 is deliberately
nonempty. A blocked/non-target row must not be silently dropped from coverage.

Kind and derived receipt payload schemas are deliberately NOT defined here.
F-033 and the existing extraction producer own their actual contracts. Fixture
receipt bodies marked synthetic_only are opaque hash-routing examples, NOT
qualified producer receipts. They cannot unlock real consumer dispatch.

## Mandatory checks beyond JSON Schema

Schema validity alone checks no referenced byte, trust, identity, inventory
coverage, SQLite contents or factual claim. Consumer implementation must enforce
all of these before evaluation or output writes:

1. Obtain expected producer run ID and source-manifest hash from the existing
   owner, outside the record/envelope itself. A self-declared hash is not trust.
   Verify the manifest's exact bytes and its producer-supported inventory,
   canonical/rules and artifact routing bindings. The test's producer-manifest
   JSON is an explicitly SYNTHETIC identity-adapter seam, not a replacement
   coordinator manifest format. Real source/manifest adaptation remains with
   the coordinator and must be admitted before consumers.
2. Validate the entire envelope and ALL reference paths before referenced content
   reads. Portable relative paths use slash-separated ASCII alphanumeric/underscore/
   hyphen components with internal dot extensions; no absolute paths, dot/parent
   components, empty segments, backslashes, URL/percent encodings or control bytes.
   Reject root, ancestor, intermediate and leaf symlinks, even links staying inside
   the root; reject nonregular leaves. No recursive search or fallback path.
3. Require unique original artifact identities, lexical sorting and unique file
   reference paths. Require exact inventory-original population equality, including
   support originals. Verify every exact byte hash and receipt hash equality.
   Inconsistent repeated identity or path is an error, never last-row-wins.
4. Verify inventory using the existing source-binding contract; validate all unit
   source hashes, locators and original-to-row bindings through the real adapter.
   Verify extraction receipt freshness, full upstream preservation and inventory
   completeness with the coordinator BEFORE claiming normalization complete.
5. Verify canonical schema/version/checksum, trusted rules source and exact rules
   version; take joins under one fresh consistent read snapshot. Missing context
   stays blocked. The canonical copy is never modified. Production snapshot/WAL
   and read-only adapter admission remain with the existing owner; this slice
   qualifies only synthetic rollback-journal copies, NOT WAL/sidecar safety.
6. Call the existing evaluate_structured API, preserving its per-detector
   evaluated/blocked/inapplicable outcomes, typed reasons, suppressed hits and
   revisions. Unbound rule sources remain rule_source_unbound; unresolved review
   gates remain rule_version_or_applicability_unresolved. Missing source_bindings
   remains source_bindings_required at the adapter, and cannot pass envelope
   admission. No missing/blocked evidence becomes a clean zero-hit evaluation.

The test-only validate_handoff oracle in tests/test_comprehension_contract.py
expresses these routing/byte relationships and calls actual existing APIs. It is
NOT an installed validator, hostile-filesystem sandbox or complete production
receipt normalizer. Its ancestry checks assume an admitted immutable tree; future
host implementations must use the owner's descriptor/snapshot safety contract
for concurrent filesystem mutation rather than copying this test helper into
production. Synthetic rule gates certify no real policy or legal applicability.

## Determinism and unchanged host integration boundary

Serialize new envelope bytes as UTF-8 JSON with sorted keys, compact separators,
ensure_ascii=true, allow_nan=false, and ONE trailing LF. Producer ordering for
inventory/units is preserved because existing digests include list order. Sort
only artifact routing rows by original identity. No generated timestamp enters
the envelope. Identical run/content bindings give byte-identical serialization;
new producer instances keep their own IDs rather than pretending to be a replay.

Pinned host source: campaign_tool/records/run_stages.py,
SHA256 2a24a0da325a528bc19ba7c3971c62a85bfb521304fa8a87ff18c04a47f25e1a.
STAGES/HANDLERS remain sync, delta, adoption, portal, extract, digest_gates,
outputs. F-046 will add an explicitly enabled INTERNAL comprehension adapter
at stage_outputs(args), after extraction/gating, then digest generation. There
are no new top-level kind/units/detect stages, no stage-count change, and no
replacement of digest_gates or its human-review-required semantics.

Future proposed config comprehension.enabled, kind_enabled, units_enabled,
detect_enabled all default false; digest.enabled=false is separate. Proposed
outputs CLI --comprehension-handoff is absolute at the CLI boundary, pointing to
the manifest whose INTERNAL references are relative. No argument plus disabled
flags preserves current snapshot behavior. F-046 must implement and test real
parser/combination/path validation before reads/writes; none is implemented here.
Existing stage_outputs reads processing AND portal snapshots; later tests need
redirected synthetic copies of both. A config-only declaration is not support.

Coordinator ownership of acquisition, preservation, extraction/OCR, authoritative
inventory and canonical stores is unchanged. Bounded local Qwen retains its
admitted jobs/raw receipts. No new queue, ledger writer, OCR role or notification
transport. No real records, inbox, credentials, shared repository or service read
or write is needed for these tests. No redaction, independent semantic review,
substantive human/legal gate or publication hold is removed.

## Acceptance and owner handoff

Run in the candidate repo, using the installed python3 (stdlib only; no
dependency installation). The test-only schema oracle supports exactly the
keywords used by this schema and refuses unknown ones; it is not a general
JSON Schema engine or a production validator:

    python3 -m unittest tests.test_comprehension_contract

Tests compare frozen fixture bytes and pinned source closure, check actual API
signatures, execute the real adapter/joins against a fully synthetic v001 SQLite
copy, and assert exact positive/blocked identities. They reject missing required
bindings, duplicate/unsorted identities, changed bytes, unsafe paths/symlinks,
unsupported versions, changed run/source identities and receipt/rule mismatches.
The byte-hash negative deliberately disables only that gate on inert whitespace
and proves the otherwise-valid control proceeds. Mutation outputs are retained
under a new candidate out/contract-<uuid> each run, never under fixtures.

Applicable existing contract tests for this source boundary are
 tests.records.test_structured_detector_pipeline (safe in-memory local command)
 and tests.records.test_structured_context_boundary (operator-run with full
 discovery; imports the wider Pipeline/extraction fixture closure).
There is no tests/contract directory at the pinned baseline. Unrelated portal
contract tests are not this interface's scoped clearance. Local baseline command:

    python3 -m unittest tests.records.test_structured_detector_pipeline

Full discovery = operator-run: python3 -m unittest discover tests, exact frozen
candidate head, using /home/user/agent-work/tools/venv-fullsuite as admitted by
the operator's 2026-10-24 10:43 PDT comment. The worker does not run unsafe full
child discovery, push or obtain remote CI. Scoped success is not full-suite
clearance. Operator attaches the full result and creates exactly one existing-
route opus-coder actual Opus5.5 verification child; no worker self-approval or
second verifier. Complete test evidence and exact head are supplied separately.

Independent review and operator adoption remain required before any consumer
uses this proposed interface. Machine-valid means REVIEW REQUIRED only.
