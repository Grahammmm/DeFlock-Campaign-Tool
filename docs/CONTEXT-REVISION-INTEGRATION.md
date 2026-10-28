# Contextual revisions and applicability (ARCH04/05)

Baseline: `39b9242d7f19f3acbb87cae4e021c6be810eb836`.

`campaign_tool.records.context_revision` is a pure, source-only contract. It
does not write the ledger, revoke approvals, choose production policy, call a
model, or change `records/run.py`. Runtime integration remains with the shared
runner integrator. **The baseline runtime defects are not closed by importing
this module.**

## Reproduced baseline behavior

Synthetic calls to `Pipeline` with patched package loading and temporary roots
confirmed the following:

| Probe | Baseline observation |
| --- | --- |
| Same rule IDs, amended duty text | Identical `config_sha256` |
| Explicit event changed from 2020 to 2026 | Identical `config_sha256` |
| No conduct date, mail header dated 2026-09-15 | `_event_date()` returned 2026-09-15 |
| Empty digest duties | Comparison requested `inapplicable`, reason `no_rule_in_force_matched_digest_duties` |

No corpus, service, credential, model, or GPU was used. These probes describe
source behavior, not a finding about deployed data or an actual legal breach.

## Public API and private input contract

- `event_context(evidence_context, dates)` freezes a JSON snapshot. Pass existing
  canonical occurrence/request/agency IDs and the selected canonical rows,
  ancestry, joins, exclusion status and provenance evidence in `evidence_context`.
  Its completeness must be checked by the trusted adapter. This is not a second
  correspondence registry or schema migration.
- `rule_revision(rule)` hashes the entire rule, including its text, sources,
  effective interval, exceptions and review metadata. Stable IDs are labels.
- `build_revision(...)` binds the original, extraction receipt and exact selected
  units/coverage, context, ordered rule candidates, package/overlay metadata,
  applicability bases, and required version strings. It returns a content hash
  and a hash for every analytical stage.
- `invalidation_plan(previous, current)` compares independently rederived stage
  hashes. Missing, corrupt or legacy manifests require re-evaluation. It lists
  **every** affected stage and an approval-revocation requirement; it performs
  neither action. Different original hashes cannot be compared.
- `resolve_applicability(rule_id, rules, context, bases)` selects a source-bound
  historical revision or returns `VERSION_OR_APPLICABILITY_UNRESOLVED`.
- `compare_applicability(duty_ids, rules, context, bases)` leaves empty duties,
  absent versions and unresolved context blocked. Even resolved rows still need
  legal review; no output certifies compliance or independent review.

Supply the complete ordered candidate-rule universe consumed by analysis. The
legacy detectors choose the first citation match, so changing order changes
identity. Put package status, jurisdiction, sources, adoption and incorporated
policy/overlay revisions in `rule_context`. Missing historical coverage stays
unresolved. Same-ID historical revisions are accepted here, but cannot simply
be passed to the existing package loader, which rejects duplicate IDs.

The extraction manifest must identify the selected extraction import/receipt,
parser and OCR versions, every selected unit's content hash and locator, coverage
and selection/chunk manifest. Do not use only the current `_units()` list: it
truncates at 2,000 units and does not establish complete extraction coverage.
The coverage worker owns that adapter. Hash all supplied metadata, and retain
the actual source snapshot privately so a reviewer can reproduce each hash.

Version keys are `extraction`, `catalog`, `detector`, `digest`, `model`, `prompt`,
`challenge_model`, `challenge_prompt`, `redaction`, `context`, `applicability`
and `privacy`. Use pinned model/adapter revisions and hashes of actual prompts,
output schemas and relevant options, not just aliases or schema version `1`.
Use explicit `disabled` for absent models. Store no secrets in these fields.
Additional version keys invalidate all analysis conservatively.

## Dates and applicability bases

Each date role is distinct: `conduct`, `receipt`, `disclosure`, `retention`.
Each is a list of periods with `event_kind`, `start`, `end`, and `evidence`.
Evidence contains `source_sha256` and an exact nonblank `locator`; include the
canonical occurrence ID where relevant. Empty, conflicting or ungrounded dates
remain unresolved. Calendar dates must be resolved using the source's jurisdiction;
the helper never truncates a UTC timestamp or substitutes today.

`event_kind` differentiates a search from implementation, an agency's receipt
of a request from an organizer's receipt of a production, and a withholding
decision from production. A basis must name the exact date role and event kind.
Retention requires both endpoints and one controlling version for the whole
interval; crossing versions requires separately assessed periods.

Every applicability basis binds `rule_revision_sha256`, `context_sha256`,
`event_role`, `event_kind`, `version_verified`, `version_evidence`, `rationale`,
`scope`, and `scope_checks`. Scope checks explicitly cover `jurisdiction`,
`regulated_role` and `policy_adoption`, each with a status and source evidence.
Unknown checks block resolution. `NOT_APPLICABLE` requires a consistent,
source-bound exclusion; no matched duty is never an exclusion.

These are structural requirements, not verification that a source exists,
proves the assertion, or defeats all exceptions. The adapter must verify cited
bytes, exact locators, request membership, scope and version/adoption evidence.
Applicability bases themselves are hash-bound; correcting a basis invalidates
comparison and privacy even when the public wording is identical.

## Correspondence coordination

The correspondence worker adds no conduct, retention or applicability fields.
Consume the existing canonical ledger IDs and its selected-row snapshot rather
than raw SQLite/WAL bytes. `occurrences.acquired_at`, `mail_messages.received_at`
and request `production_at` are recorded receipt/declaration provenance. They
must not become conduct, request-receipt, disclosure or retention dates by
default. Typed joins are recorded assertions, not independently verified facts.
Ambiguous ancestry/request membership and excluded sources remain gaps.

One original hash may have multiple occurrence/request/agency contexts. Reuse
the same extraction, but create separate private contextual artifacts and
bindings. The current ledger has one stage head per original/stage, so it cannot
claim simultaneous completion of all contexts. The integrator must either bind
an aggregate of all reviewed contexts to that head or explicitly supersede it
when switching context; the module does not create duplicate originals.

## Shared runner integration

The accompanying [guard patch](integration/context-revision-run-guards.patch)
is an **unapplied, partial** diff against the pinned baseline. It removes date
fallbacks and the empty-duty inapplicability shortcut, and strengthens global
configuration identity. It is not the complete contextual reuse/invalidation
implementation and must not be represented as closing ARCH04/05 by itself.

The integrator also needs these steps under the publication lane's serialized
invalidation/approval authority:

1. Resolve and verify each subject's canonical contexts, selected extraction,
   rules and applicability bases; build the current revision before deciding
   whether accepted or blocked work may be skipped.
2. Load the prior manifest bound to accepted stage content, not a mutable
   unverified sidecar. Compute `invalidation_plan(previous, current)`.
3. Revoke affected proposal approvals first; invalidate every returned stage
   through `runner.invalidate(subject, stage, reason=plan['reason'])`. The ledger
   does not invalidate review when detect alone changes, so do not collapse the
   plan to only its earliest item. Preserve all originals, receipts and content
   revisions. A crash must leave old approvals unusable before work resumes.
4. Bind each stage-specific identity in its **outer content envelope** and
   require it during reuse. Use only that stage's identity for cache decisions;
   attaching the overall hash as a mandatory cache key to extraction would
   defeat reuse across request contexts. Keep the overall manifest separately
   content-bound for audit. A plan is not a receipt that invalidation occurred.
5. Keep `receipt.input_hashes` exactly as defined by the ledger; its validator
   rejects extra context fields. The digest schema also rejects new fields.
   Envelope/validator changes belong to the integrator, not this lane.
6. Never call `build_digest(..., event_date=None)` with a model: it independently
   substitutes today. Mixed duty-specific dates/historical revisions require a
   coordinated digest input change. Until then hold the legacy model path where
   one verified date cannot represent the selected scope.
7. Preserve unresolved applicability decisions as private artifacts and blocked
   work. Existing compare/challenge adapters require a date and an in-force rule;
   do not fabricate either to fit their schema. Coordinate that contract before
   promoting resolved historical/multi-event rows. Exceptions still need review.
8. Bind proposal provenance and renewed approval to the current contextual
   revision even when public bytes did not change (publication lane ARCH08).

The suggested guard patch removes catalog dates derived from mail headers;
source-supported dates can be added by the integrator. Context date changes
still invalidate catalog conservatively so existing historical metadata is not
silently reused.

## Validation

Synthetic regressions cover changed rule text/sources/exceptions/effective dates,
same bytes in distinct contexts, all version inputs, selective invalidation,
corrupt/rehashed manifests, missing/ambiguous dates, date kinds and sources,
historical rule boundaries/gaps/overlaps, retention intervals, explicit scope
exclusion, empty duties, and unchanged immutable ledger history after applying
an invalidation plan using test-only authority.

Run with Python 3.11+ and a real, writable temporary directory on macOS:

```sh
python3 -B -m unittest tests.records.test_context_revision tests.records.test_ledger_stages tests.records.test_catalog_dependency_guard tests.records.test_review_regressions tests.test_law -v
python3 -B -m unittest tests.test_ca_law_corrections tests.runner.test_digest -v
git apply --check --unidiff-zero docs/integration/context-revision-run-guards.patch
python3 -B tools/check_public_tree.py
node scripts/scan-secrets.mjs
```

The helper uses only the standard library. Linux parser/OCR suites and a fully
wired runtime journey remain integrator verification requirements. Tests do not
grant publication approval, validate law, or establish source completeness.
