# Host-owned detector preparation boundary

`detectors.preparation.prepare(snapshot, provider=None)` is a library contract.
It installs no semantic provider, performs no IO, evaluates no detector and
changes no stage, context, approval or publication state. Missing provider is
blocked. Runtime integration remains held.

The host must pass a fresh `sources.resolve_sources` result directly, never
deserialize a `SourceSnapshot` from submitted record JSON. The host must select
and authenticate the installed provider out of band. `PreparationProvider` is
only a Python protocol; type membership, a method named `provide`, a receipt hash
or any descriptor is not an authentication mechanism. No provider is implemented
or registered here. Synthetic test providers establish no real authority.

## Provider responsibility

Before returning `ProviderInputs(normalized, rules, config, descriptors)`, the
installed provider must resolve and authenticate its receipts and decision
artifacts against the exact source snapshot. It must independently verify:

- Every extraction unit's mapping and observed/missing/blank/redacted states;
  stable event identity; unknown types must remain unresolved, never `other`.
- Complete required support dependencies, including training and historical
  policies. The resolver's explicit-original inventory is not dependency closure.
- Controlling date roles, event kinds, jurisdiction-local intervals and source
  evidence. Receipt/acquisition/CLI dates cannot supply conduct dates. Retention
  needs both interval endpoints. Timezone conversion must preserve the intended
  date used by WP6 rule selection. The date decision artifact must cover all
  date-valued normalized fields, not just the event timestamps fingerprinted here.
- Historical rule bytes, adoption, jurisdiction and regulated-role scope;
  configuration such as holiday coverage and baselines. WP6 rule intervals end
  exclusively. Any ARCH04/05 translation must explicitly preserve this meaning.
- Immutable model/prompt revision when a model participates. A model proposal
  cannot itself authenticate a decision. `disabled` means no model contributed
  to these prepared inputs; downstream narrative revisions remain separate.

Those semantic/authentication checks are deliberately not reimplemented by this
structural boundary. A `prepared` result means the host-supplied inputs are bound,
not that source facts, legal applicability or review were independently accepted.

## Descriptor contract

All five descriptors require nonempty `revision`, `code_sha256`, `receipt_sha256`
and `source_revision_sha256`. Every source revision must match the supplied
snapshot. Hashes are lowercase SHA-256. Extra fields, including authority flags,
are refused. Decision hashes identify host-verified private artifacts; they are
not a new receipt store and must never be populated with invented placeholders.

| Descriptor | Additional required fields |
|---|---|
| normalization | `payload_sha256 = digest(normalized)` |
| support | `originals_sha256`, `bindings_sha256`, complete `required_original_sha256` list matching resolved originals, empty `unresolved` list |
| dates | `normalization_sha256`, `events_sha256`, `decision_sha256`, nonempty source-bound `evidence_unit_ids` |
| policy | `payload_sha256 = digest(rules)`, `decision_sha256`, nonempty source-bound `evidence_unit_ids`, `interval_end: exclusive` |
| provider | `model: {mode: disabled}` or `{mode: pinned, revision_sha256, prompt_sha256}` |

`events_sha256` fingerprints a mapping from target source unit ID to ordered
`{event_id, event_at}` records. The normalization digest additionally binds all
support/training/date-valued fields. Multiple records may map to a source unit;
every source unit must be classified. Policy and training support types may come
from other originals; unrelated target events cannot enter the primary scope.
Existing WP6 `version_verified/applicability` fields are accepted only inside a
host provider result with the complete descriptor set; they confer no authority.

## Result and limits

`Preparation(status, reason, prepared)` has a payload only for `prepared`.
Blockers are stable codes and never echo provider errors, paths or record values.
`PreparedInputs` contains immutable JSON strings and fresh canonical joins;
`kwargs` returns independent private copies compatible with `evaluate_structured`.
No detector is run during preparation. Later evaluation may still block on
detector-specific context, ambiguous joins or malformed individual fields.

The preparation revision binds normalized data, full bindings, rules, config,
every descriptor, source revision and canonical join revision/rows. Changing any
dependency changes the revision. Future integration must also bind installed
preparation code identity and feed these dependencies through existing ARCH04/05
invalidation; this PR does not add or call another reconciler.

Core limits apply: 5,000 units, 10,000 join rows, 256 rules, 8 MiB, depth32 and
100,000 native JSON nodes. Resolver limits cannot relax these limits. Serialized
escaped size is checked as well as input character counts; nothing is truncated.
Host provider execution remains responsible for its own resource/time bounds.

No raw payload or prepared JSON belongs in public output. No runtime stage hook,
protected detect receipt, model call, ledger mutation, intake/recovery work or
live activation is included. Independent source review and normal Linux CI remain
required before merging; Mac unit tests do not qualify the full Linux runtime.
