# Explicit CSV/text row mappings (F-040a)

Status: REVIEW REQUIRED. No installed provider, extraction qualification, canonical
writer, stage acceptance, semantic/legal acceptance or live execution is provided.
Native CSV extraction availability is UNVERIFIED. The coordinator continues to
own extraction/OCR, preservation, authoritative inventory and canonical stores.
The local Qwen queue retains its existing export jobs and raw receipts.

## Source pin and unchanged consumers

Implementation baseline: 93995a1a615f6aaca7bfb0fbeb39a7f3aa367997.
The existing CURRENT_CORE_UNIT is the WP6 record described by
`docs/RECORDS-DETECTORS.md:17-35`, `detectors/core.py:74-166` and
`docs/STRUCTURED-DETECTOR-CONTRACT.md:21-36`, not a new schema.
The output envelope is exactly `structured-normalization-v1`, consumed by
`campaign_tool.records.detectors.pipeline.evaluate_structured`.

Native synthetic CSV unit shapes follow `records/intake/folder.py:292-322`:
`csv_header` text is a JSON string array; `csv_row` text is a JSON string array,
with `data.values`, `presence`, `column_mapping=positional` and `row_sha256`.
The latter hash uses the producer's JSON serialization (sorted keys, ASCII,
ordinary separators), not core canonical JSON. The producer's presence vocabulary
is `missing_field`, `empty`, `whitespace`, `redacted_marker`, `present`.
Ragged native rows are ambiguous; they are never silently realigned.
This is static interface compatibility, NOT a new CSV extraction claim.

Upstream F-033 kind receipt was independently reviewed at
2c27b80efd84a35bf92951d0e158637daa9fd3ba. It is a routing candidate, never mapping
authority. This candidate does not copy/import F-033 code or infer mappings from
its labels, MIME, filenames or sheet names. The host must separately preserve
its kind-receipt hash in the existing comprehension handoff; this pure row API
does not introduce a competing handoff envelope.

## Pure API

`campaign_tool.records.understanding.rows.normalize` takes keyword-only inputs:

- `run_id`: unchanged producer run identity, nonempty and at most 256 characters.
- `receipts`: list of `{artifact_sha256, derived_receipt_sha256,
  derived_receipt: bytes}`. Each exact `format-extraction-v1` receipt is pinned by
  independently admitted original/receipt identities. There is one receipt per
  original in this call. Receipt bytes are not opened from a path by this API.
- `source_bindings`: the independently supplied coordinator extraction inventory,
  in its original order, with `{unit_id, original_sha256, artifact_sha256, locator}`.
  Every supplied receipt unit must match exactly, and no inventory unit may be
  omitted. The mapper DOES NOT manufacture the authoritative inventory from its
  own output. Unit IDs and raw-unit hashes use the existing `understanding.evidence`
  receipt-bound derivative contract. Original/receipt hashes are not proof of
  trusted acquisition, preservation or semantic truth; those remain host gates.
- `mappings`: admitted per-agency mapping objects, one per source original:

      {
        "schema": "agency-row-mapping-v1",
        "version": "synthetic-example-v1",
        "artifact_sha256": "<independently pinned original SHA256>",
        "agency_id": "<owner-admitted agency identity>",
        "record_type": "search-log",
        "unit_kind": "csv_row",
        "delimiter": "|",
        "columns": [
          {"name": "ID", "target": "event_id", "type": "str"},
          {"name": "When", "target": "event_at", "type": "str"},
          {"name": "Reason", "target": "purpose", "type": "str"},
          {"name": "User", "target": "user_id", "type": "str"}
        ]
      }

The hash placeholder above is explanatory, not a runnable fixture. Durable
synthetic input constructors live in `tests/test_understanding_rows.py`; they use
independent producer-shaped inventory/identity construction and no real records.
Actual mapping authorship/semantic qualification remains with the admission
owner. The mapping agency is diagnostic/dedupe scope only; it is NEVER inserted
as a typed canonical join. Downstream must still supply freshly admitted
`CanonicalJoins`, authoritative inventory, source-bound rules and explicit config.

Mapping columns are positional, unique and exact. A CSV header must match the
names in order, without case folding, trimming, substring recognition or guessed
aliases. Only that bound structural header is classified `other`. A missing or
mismatched header prevents successful CSV row mapping. A null target denotes an
unknown column, not permission to ignore it: the entire row stays unknown and
incomplete. Extra columns likewise stay unknown. Unknown target names/types and
ambiguous/duplicate mapping declarations are refused, not coerced into fields.
Supported target field names are explicitly frozen from the existing WP6
contract in `rows.FIELD_NAMES`. New fields require a reviewed mapping extension.

`unit_kind` is `csv_row`, `text_line` or `pdf_page`. The last two consume ONE
explicitly delimited row from already-admitted extracted text per cited unit,
using the declared delimiter and strict CSV quoting. They do not discover tables,
select a header, split an ambiguous multiline page into guessed rows or run OCR.
A quoted newline within one field is supported by the CSV parser. Distinct
physical rows in one text unit are `text_table_row_ambiguous`, not partial success.
Page/line and CSV record/line-start/line-end locators are preserved exactly.
Sheet/row workbook mapping is intentionally deferred to F-040b; workbook inputs
are unresolved, with all original holds retained. No raw workbook is opened.

`str` copies observed text verbatim. `int` accepts only signed decimal integer
syntax without leading zeros; `bool` accepts exactly `true`/`false`; `json` accepts
an explicit JSON array (for existing array-valued fields), never arbitrary objects.
No timezone, identity, boolean, date or number is inferred. Event IDs/timestamps
must map from string columns. Event timestamps require full ISO date/time with
seconds and a valid explicit `Z` or `+/-HH:MM` offset (hours <=23, minutes <=59),
and must pass the existing core timestamp parser. Naive dates/times, invalid
calendar dates and invalid offsets are typed unresolved. The timestamp text is
not normalized or converted to a guessed timezone.

Missing trailing cells in an explicitly mapped TEXT row become `{state: missing}`.
Native CSV ragged rows are instead unresolved because the producer cannot
establish alignment. Empty/whitespace values are `{state: blank}`. Producer redaction
markers (`[redacted]`, `[withheld]`, unbracketed equivalents, repeated x or block
characters) become `{state: redacted}` and never acquire a value. Observed cells
become `{state: observed, value: ...}`. States for event identity/time columns are
retained separately in diagnostics, while core identity/time are null if unobserved.
Missing critical fields stay blocked by the existing detectors, not converted to
an adverse finding. Mapping completeness does not mean detector completeness.

## Output and failure semantics

The result contains `normalized` and unchanged `source_bindings`, ready for the
EXISTING adapter keyword inputs. `normalized.units` entries have exactly
`{record, source: {unit_id, artifact_sha256}}`; no mapping metadata is injected
into core event identity. `normalized.source_inventory_sha256` uses the core
digest of the exact supplied inventory, not the worker's observed subset.

Separate `diagnostics`, `events`, `upstream`, `source_receipts`, `mapping_sha256`
and `run_id` retain provenance, cell states and stable typed reasons. These are
not a replacement unit/rule/join schema. No raw receipt or original is written.
All output values are detached copies, not aliases into caller input.

Logical events group by `[agency_id, record_type, event_id]`. Identical duplicates
produce one event summary with `duplicate_rows` and ALL cited source references.
Every occurrence also remains in normalized.units so the core retains every
source binding and can independently perform its existing dedupe. Conflicting
observations are all retained, each is diagnosed `conflicting_duplicate_event`,
and the normalization is incomplete; no first/last-wins policy discards evidence.
The unchanged core provides a second conflict check when actually evaluated.

Unknown mapping/unit kind/columns are never converted to a known usage type.
Missing identities, invalid timestamps, invalid conversions and conflicting
duplicates produce explicit unresolved diagnostics and `complete=false`.
Upstream partial/blocked receipts or ANY extraction issue likewise keep
`complete=false`, with `upstream_not_clear`. No accepted subset is advertised.
Integrity, pin, inventory, malformed JSON, bounds and mapping-contract violations
raise `RowsError` with stable source-free codes and return no partial result.
A missing/extra source inventory entry is a refusal, not a repaired inventory.

Identical inputs yield byte-stable canonical results. Receipt and mapping list
order is immaterial; inventory order remains identity-bearing, as required by
the existing core digest. A changed receipt hash/ordinal legitimately changes
unit identity. Do not label a changed receipt as an identical replay.

Every result preserves `review_required=true`, `qualified=false`,
`activation_allowed=false`, `publication_ready=false`, `stage_promotions=0`.
Structural validity is not semantic/legal acceptance or permission to activate.
There are no filesystem, database, network, model, acquisition or persistence
operations in this module. Existing stores, services, profile configuration,
raw receipts and jobs remain outside its ownership.

## Frozen acceptance population

Run separately from the candidate repository:

    python3 -m unittest tests.test_understanding_rows
    python3 -m unittest tests.records.test_structured_detector_pipeline

There is no `tests/contract` directory at baseline. The second command is the
applicable existing detector-contract population, not a substitute for the full
suite. Tests cover native synthetic CSV shape, exact identities/hash bindings,
text/page locators, missing/blank/redacted states, typed conversions, unknown
mappings/columns, invalid timezone class, duplicate evidence/conflicts, disjoint
agency/type identities, header and workbook controls, upstream holds, tampering,
bounds, deterministic replay and the real evaluate_structured consumer. An inert
mock disables only the source-binding gate to prove the negative control depends
on that deciding gate; no prohibited operation is executed.

Full discovery (`python3 -m unittest discover tests`) is OPERATOR-RUN at the frozen
candidate head under the 2026-10-24 11:01 PDT card admission; pending full discovery
is NOT full-suite clearance. Worker no-push/no-merge/no-publication remains intact.
Author: sol-reviewer, actual gpt-6.1-sol / openai-codex, task t_2185944d, run 1115.
Independent review must be actual Opus5.5, never the Sol author reviewing itself.
