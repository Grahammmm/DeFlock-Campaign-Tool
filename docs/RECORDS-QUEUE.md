# WP7 bounded queue and WP1 lease integration

This is generic private infrastructure, not a corpus run or active service.
`campaign_tool.records.queue.from_ledger(database, now=aware_timestamp)` reads one
consistent SQLite snapshot. It reads every canonical original and its seven stage
slots, fails closed above 10,000 originals/70,000 slots or leases, and returns an
explained deterministic top 20. Missing slots fail rather than silently disappearing.
No digest-existence predicate, file-extension filter or low-value auto-closing exists.

Priority terms: +40 new production/open request, +30 important type, +20 per
severity-3 hit (maximum +40), +10 live agency, -50 proposed low-value. Scores clamp
to 0-100. Ties sort by original SHA-256, then stage order. Caller-supplied priority
facts must come from the versioned catalog/detector adapter, never filenames. Every
score includes all terms and exact fact hash; packets carry config_version and
planner_version. Missing facts score zero. Results never assert corpus review.

Selection checks preserve/extract/catalog/detect/review/compare/privacy in order and
chooses the first eligible unfinished stage. Live leases and future retries hold
that stage. Blocked stages without a scheduled retry remain explicit holds. Catalog
can follow blocked extraction, matching WP1. Independent branches can proceed;
a blocked portal document never holds unrelated local records. Privacy holds remain
open until explicitly retried/reopened. Settled candidate slots are not full review.

## Claim API

Trusted installed startup supplies a configured WP1 StageRunner to:

    result = claim_next(runner, now=aware_timestamp, facts=catalog_priority_facts)

This delegates `runner.claim(subject, stage, ttl_seconds=2700)`. WP1 owns atomic
claims, owner/run/profile validation, content binding and expired-lease attempts.
The queue never writes stages/leases, registers validators, creates runs or promotes
receipts. Bounded contention, prerequisite and unprepared-content skips are reported;
authority/configuration/corruption errors propagate. At most 20 attempts per call.
The plan is a snapshot, not a lock; WP1 claim remains authoritative. The packet's
expected_attempt is advisory; actual counts remain in WP1. WP1 requires prepared
stage content; installed domain adapters supply it, never this planner.

StageRunner is imported only for claims. Production composition installs reconciled
packages together, never private data or developer worktree paths in public code.

## Packets and return path

`packet(claimed, denominator=..., derived_text=..., page_images=...,
receipt_template=...)` requires an explicit denominator and subject/stage-bound WP1
receipt template. At most 1,000 private path references; none is read or executed.
The caller writes owner-only private packets, then returns full or blocked receipts
through runner.promote with claim_id. No automatic completion/low-value closure.

Remaining integration: versioned catalog/detector facts adapter and freshness;
derived-path/denominator assembly; owner-only atomic packet storage; trusted blocked
receipt/retry adapter; runner continue-with-next loop; root `records queue next
--stage-any --owner` CLI; independent review and CI. This slice supplies contracts,
not all those adapters.

Read-only preview (no claim or files written):

    python -m campaign_tool.records.queue /private/ledger.sqlite --as-of 2026-01-01T12:00:00+00:00

Synthetic tests: `python -B -m unittest tests.records.test_queue -v`.
WP1 composition evidence is separate and uses only a synthetic test-only runner.
No timer, install, portal access, external send, activation, deployment or publication.

Scope-excluded originals remain in the snapshot inventory and an explicit `excluded_from_claims` list, but never become queue claims. Their seven slots must still reconcile. A concurrent out-of-scope refusal is treated as an item-specific skip, not an abort of unrelated candidates.
