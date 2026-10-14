# Structured detector integration contract (ARCH10)

Base: `39b9242d7f19f3acbb87cae4e021c6be810eb836`.
This change adds an offline adapter and synthetic regressions. It does not wire
or enable the main pipeline. The single integrator owns `run.py`, stage-content
validation, contextual invalidation and extraction coverage. The unapplied
[wiring proposal](integration/ARCH10-stage-detect.patch) targets that exact base.

`detectors.pipeline.evaluate_structured` calls all seven existing WP6 detectors.
Each entry is `evaluated` (including zero hits), `blocked`, or `inapplicable`.
Private detector hits remain triage signals; they do not satisfy factual review,
legal review, independent review, acceptance or publication gates.

## Caller contract

Use keyword inputs `subject_sha256`, `normalized`, `source_bindings`, `joins`,
`rules`, `config`. The host must construct these from its trusted extraction and
context adapters, under one consistent snapshot. Do not load record-supplied JSON
as trusted configuration. The normalized envelope is not an attestation of truth.

1. `source_bindings` is the authoritative extraction inventory for the primary
   original and all relevant support originals. Each item has `unit_id`,
   `original_sha256`, `artifact_sha256` and exact `locator`. Before calling this
   adapter, verify original identity, derived bytes/hash, current extraction
   receipt, complete coverage and unit locators against the canonical ledger.
   Missing pages, partial workbooks or unavailable support inventories cannot be
   admitted as complete. The current truncated `_units()` view is insufficient.
2. `normalized` has `schema: structured-normalization-v1`, a nonempty mapper
   `version`, `complete`, `source_inventory_sha256: digest(source_bindings)` and
   `units`. Each unit is `{record: WP6_UNIT, source: {unit_id, artifact_sha256}}`.
   WP6_UNIT is the existing core shape: original hash, locator, stable event ID,
   source-supported event timestamp, record type and observed field states.
   Every source unit, including policy and training support, must be mapped or
   explicitly classified. Unknown worksheets/columns/types remain blocked.
   Only an established normalizer may classify a unit `other`; text hints are
   not a basis for excluding it. Mapping metadata does not enter event dedupe.
3. Obtain `joins` with `canonical_joins(connection, source_bindings)` fresh from
   the canonical read snapshot. It checks typed join evidence against verified
   source locators, the agency row and request/agency consistency, and fingerprints
   the read rows. It neither creates nor infers an entity join. Bare `typed` JSON,
   hints, conflicting agencies and stale source snapshots cannot evaluate.
   The returned Python snapshot is a trusted host value, not a credential or
   authenticated human review. Never deserialize it from record content or cache
   it across ledger revisions. Malformed canonical join evidence raises
   `ContractError`; the integrator must record that input-preparation blocker.
4. `rules` keeps the current WP6 `{version, entries, holiday_tables}` interface.
   Entries retain detector/agency scope, rule/version IDs, source hash, parameters,
   effective dates and the existing review-gate fields `version_verified: true`
   and `applicability: yes`. These decisions must come from the trusted current
   policy-version interface; this adapter does not grant or authenticate them.
   An unresolved version/applicability or unbound source blocks the check. No
   bundled draft law package is silently converted into an agency policy.
   `effective_to` remains **exclusive**, as in the existing structured core.
5. `config` is explicit, including `as_of` for deadlines. No wall-clock conduct
   date, holiday table, baseline, identity, missing value or legal duty is invented.

Rule hashes resolve to bound source originals; source locators and derived hashes
are retained in the inventory digest. Source-binding checks establish identity,
not semantic validity of a normalization or rule. Unknown semantic inputs must
remain blocked upstream. Support target events from unrelated originals are
refused; training and other non-target support types are allowed.

## Outcomes and dependency revisions

Every result carries normalization, complete source inventory, canonical join,
rule and config hashes, plus a result hash. Each evaluated detector retains its
original core manifest, counts and exact references even with zero hits.
Individual malformed rows retain blocked outcomes. If any relevant row blocks,
the adapter withholds all hits for that detector and records their suppressed
count; it never presents a rate computed over only the unblocked subset.

Inapplicability means either no matching input type in the complete classified
inventory or an event outside the supplied rule intervals. It does not establish
that no other legal duty applies. Missing normalization/rules/joins is blocked.
Malformed envelopes return seven blocked entries with stable reasons.

## Integration handoff

The patch reads trusted `stage["structured_inputs"][subject]`, runs the adapter,
and saves all seven entries plus their revision metadata in detect content.
Missing input becomes blocked and stops advancement. It retains textual leads
as a separate field. A blocked result registers content and returns blocked;
the integrator must bind this to the shared blocked-state/receipt mechanism.
The validator status change is additive for historical `skipped` content.

Before applying the proposal, the integrator must install the source/context
provider and include all structured revisions in contextual invalidation, so
changed extraction, normalization, joins or rules reopen detect and downstream
review. Replace the legacy `_inapplicable(..., "detect", ...)` skipped-content
path with the same evidence-backed contract if any caller uses it. Validate the
new content provenance through the shared semantic helper. Do not use WP6
`persist_evaluation` with the outer stage-runner ID: it requires one detector per
compatible run and a matching detector-config digest.

The adapter performs no writes, network calls, production reads or promotions.
The join helper only reads the supplied SQLite connection. Tests use fictional
in-memory ledgers and synthetic policy state; no real policy is certified.
