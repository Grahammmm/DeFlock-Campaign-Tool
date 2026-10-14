# Structured detector prerequisites on the PR96 stack

Base: PR96 b4c2fa2fe04c2b8ace540d5345b8877656dbb3ab. This branch reuses
PR86's four source files unchanged and adds only synthetic boundary tests and
this handoff. It duplicates no contextual runner implementation and leaves
PR96/97 heads unchanged.

## Exact implementation dependency

PR96 records source_date_adapter_unavailable, empty applicability bases and
unknown conduct/retention roles. It has no trusted normalizer API, verified
historical policy provider or complete structured support-source resolver.
PR86 requires all of those inputs before its wiring proposal is safe:

1. An established normalizer must map every authoritative extraction unit and
   preserve unknown field states; source text or worksheet labels cannot confer
   a record type, event timestamp, agency, policy or completeness.
2. Primary plus supporting original inventories must be current, accepted and
   hash/locator verified. A capped selected-text view is insufficient.
3. canonical_joins must run fresh in the host's canonical read snapshot against
   those verified bindings; a typed declaration is not independent review.
4. Reviewed rule versions and source-bound applicability/date decisions must
   come from a trusted current adapter, not draft law-package flags or mail dates.
5. Normalization, support inventory, join and policy revisions must enter PR96's
   contextual identity before reuse, with approval revocation under its shared lock.
6. All seven results must bind to protected detect content/receipts; blocked
   inputs must stop advancement without converting missing evidence to no hits.

The existing unapplied ARCH10 patch's stage["structured_inputs"] dictionary is
a proposed handoff, not an installed or authenticated provider. This branch
does not activate it, manufacture version_verified/applicability decisions,
or claim the seven detectors are wired into the runtime. ARCH10 remains open.

## Bounded evidence delivered instead

Six new tests use real synthetic canonical intake/current extraction on PR96.
They verify read-only source-bound joins, request/agency refusal, join revision
invalidation and retained original identity, unknown date/applicability inputs,
all seven blocked results without a normalizer, and refusal to treat capped
selection as complete support. PR86's existing adapter tests retain positive
and zero-hit triage, unresolved rules, source/locator drift, support gaps and
partial-population suppression. None proves semantic or legal review.

Trusted source dates and unattended contextual preselection/resume remain
deployment holds from PR96. Queue fencing remains separate in PR97. No private
corpus, credentials, live migration, model call, service change or deployment
is part of this source-contract/test handoff.
