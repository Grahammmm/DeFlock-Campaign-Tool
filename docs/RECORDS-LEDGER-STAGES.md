# Stage controller authority contract

This WP1 component enforces generic stage/subject/content/prerequisite/independence
and transaction invariants, not substantive extraction or WP8 legal analysis.
It ships NO production domain adapters and no blanket-true production validator.
Missing adapters produce an operational blocked stage without accepting a receipt.

## Trusted startup and untrusted submission APIs

Trusted installed startup calls configure_installed_profile(profile_id,
engine_version=..., config_sha256=..., validators={stage: installed_adapter_id}).
IDs must exist in the installed application registry; callables and invented IDs
are rejected. Installed code owns that registry. Receipt data and submission CLI
arguments must never be routed to registry/profile configuration.

Startup obtains installed_runner(database, run_id=..., owner=..., profile_id=...).
The run must have kind stage-runner or records-runner, status running, no ended_at,
a started timestamp, and exactly matching engine_version/config_sha256. Run/profile/
adapter identity is bound immutably, and checked transactionally on each operation.

    runner.set_content(subject, stage, exact_bytes, author_id=..., tier="A")
    claim = runner.claim(subject, stage, ttl_seconds=300)
    runner.promote(subject, stage, receipt_bytes, claim_id=claim["claim_id"])
    runner.invalidate(subject, stage, reason=...)

Module wrappers take the configured runner first. Promotion accepts NO callback,
validator identity, owner, run or profile override. Those overrides in submitted
receipt JSON are also rejected. Actual reviewer identities remain separate from
the authenticated runner owner. Trusted installed code must authenticate them.

Hostile replacement of installed Python or full database-administrator control is
outside the boundary; the boundary is untrusted receipts versus installed runner
configuration. No production adapter is silently synthesized in this generic module.

## Claims, replay and revision authority

A claim binds subject/stage, content revision/hash, current prerequisite receipt
hashes, owner, run/profile and issue/expiry timestamps. Promotion verifies it inside
BEGIN IMMEDIATE, including the matching live work lease and in_progress owner/run.
Absent, foreign, wrong-run, expired, superseded and stale-content/input claims fail.
Live lease reuse checks run as well as owner. Success consumes the live work lease.

Duration is 1-3,600 seconds. Expired reclamation creates a new claim. Claiming an
already accepted non-preserve stage requires supersedes=current_receipt_sha256;
the new receipt must name it too. Preservation cannot be reopened. Exact unchanged
replay may omit claim_id only for the same still-authorized run/owner/profile and
current content/accepted receipt. It creates no extra stage/history transition.

## Receipt and adapter contract

Receipt exact JSON bytes are capped at 64 KiB; registered content at 4 MiB.
Required fields: schema=ledger-stage-receipt-v1, subject_sha256, stage,
content_sha256, author_id, tier=A/B, reviewer_id, role, verdict, coverage, locators,
rationale, model_or_tool, created_at_tz and input_hashes.
Roles by stage: preserver/extractor/cataloger/detector/factual/legal/privacy.
input_hashes contains original plus exact current prerequisite receipt hashes.

Coverage: denominator {kind: bytes/pages/rows/sheets/items, total: integer},
covered: integer, scope: selected/full_text/full_visual/all_rows. Review requires
nonzero known coverage; booleans are not counts. Locators are nonblank strings.
Selected scope never asserts full-original coverage.

Review entries bind content_sha256, reviewed_primary_sha256, reviewer_id, role,
verdict=pass, checked_locators, rationale and timezone-bearing reviewed_at.
Factual review requires blind_first_pass=true. Stable ASCII IDs are compared
case-insensitively. Self-review, whitespace aliases and filename-based independence
are not accepted. Tier A needs an independent factual check and final privacy role.
Tier B final privacy additionally needs legal review and at least two reviewers
beyond the author across factual/legal/privacy; one qualified reviewer may cover
two roles. The domain adapter verifies actual identities, evidence and scope.

A registered adapter receives one context dict: receipt (parsed dict), content
(exact bytes), original (canonical row dict), stage and subject_sha256. It must
return exactly True after real validation; exceptions/other results reject.
All new outcomes need a configured adapter. Done rejects unresolved holds/challenges;
blocked/inapplicable require reasons. Preserve cannot be inapplicable.
The original sample hash/length and an occurrence are checked additionally, but a
preservation adapter must verify the actual stored path and acquisition evidence.

## Synthetic profiles and truthful counts

testing_runner accepts injected callables ONLY under explicit test-only authority.
It has no test_only=false override. Even seven lambda-true synthetic transitions
produce end_to_end_complete=0 and verified_seven_stage_complete=0. Production
transitions cannot depend on synthetic prerequisite acceptance.

query_counts(database), also counts(database), includes all seven slots/statuses:
candidate_seven_stage_complete counts all settled candidate subjects;
synthetic_seven_stage_complete identifies settled subjects with test-only acceptance;
verified_seven_stage_complete/end_to_end_complete exclude all synthetic subjects.
All-pending candidates return numeric zero, not null. candidate=true,
acceptance=candidate_with_explicit_validation_authority, publication_ready=false,
owner_approval=false. Per-stage totals describe candidate state, not production
verification. The query is bounded to 10,000 original metadata records.

## Prerequisites, history and limits

Preserve precedes extract. Catalog can follow done/blocked/inapplicable extraction.
Detect/review depend on catalog; compare on review; privacy on review and done or
explicitly inapplicable compare. Exact prerequisite hashes are bound.
Content/parser/join/rule changes reopen affected downstream stages; preserve never
reopens. Old content/approvals never silently carry forward.

Receipts, content revisions, claim/validation authority and history are private
immutable evidence. Acceptance/history/authority append atomically; interruption
rolls them back. Failures leave a rejected attempt, not accepted evidence.
The authority migration is additive; earlier pre-authority accepted histories fail
closed instead of being silently grandfathered.

No real promotions, corpus run, deployment, publishing, owner approval or schedules.
Domain adapters, reviewer authentication and CLI/runner integration remain work for
the installed application. Unit, receipt and reconciliation files are untouched.

## Metadata-only preservation, including large originals

runner.set_preservation_evidence(subject, evidence, author_id=..., tier="A") registers
a small explicit evidence envelope, not raw original content. Its schema is
preservation-evidence-v1 with original_sha256, byte_length, storage_ref,
verification_receipt_sha256 and occurrence_ids. Hash/length must match the canonical
original; every occurrence ID must bind that original. The envelope is capped at
64 KiB regardless of original size; no 4 MiB original-size ceiling applies to this
metadata path. stage content_sha256 hashes the envelope, separately from original_sha256
and verification_receipt_sha256. Raw sample bytes remain an optional small-input path.

Adapter context additionally provides content_kind (original_bytes,
preservation_evidence or stage_content) and preservation_evidence (parsed envelope
or None). An installed preservation adapter must verify the actual storage reference,
stream/hash the original, validate its verification receipt and acquisition evidence.
Structural metadata checks alone never verify external bytes. The controller performs
no original filesystem read here. The large-original test is a synthetic size declaration,
not a real 9 MiB preservation/throughput claim.
