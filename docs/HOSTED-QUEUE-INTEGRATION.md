# Hosted queue integration and compatibility

This integrates PR87's hosted queue repair on main 28e9d198. It is independent
of PR96's manual contextual-analysis contract: no records pipeline or context
files are changed. Neither patch is activated by this source integration.

## Protocol and migration

- Apply only additive 0002_job_claims.sql through the existing migration system;
  0001_init.sql remains byte-identical. Existing job evidence becomes a legacy
  claim snapshot, not a submitted or replayable new result.
- Leased job payloads issue an immutable claim_token. Renewals and first results
  must supply that exact live claim; wrong/expired/replaced claims have no effects.
- Accepted result identity includes status, outputs, receipt and error. Exact
  replay returns the original acknowledgment even after a later attempt starts.
  Changed result replay returns a conflict.
- One D1 batch owns claim acceptance, parent transition, downstream jobs,
  classification update, exhaustion incident and response receipt.
- Old tokenless clients fail closed. Worker, migration and runner adoption must
  be coordinated; old live leases expire normally before reclaim/exhaustion.
  Do not invent tokens for old workers or replay legacy snapshots as new results.

## Runner storage and recovery

The new runner renews within an absolute runtime budget and checks ownership on
each transport attempt. It persists a private result envelope before delivery,
then removes only its unique scratch directory. Restart delivers that envelope
before leasing another job and does not rerun the handler. Preserve the separate
persistent result directory across container replacement; ephemeral scratch is
insufficient for restart recovery. Retained conflicting results require operator
reconciliation, including during runner token rotation.

This fences subsequent operations, not computation already running or nonqueue
writes already in flight. Keep runner/Worker clocks synchronized. Coordinated
rollout and these operational requirements remain release gates; this patch
performs no live migration, volume/service change, credentials or deployment.

## Integration fixture changes

The previously merged website-build seam now supplies the issued claim token
on its result calls. Its outage assertion expects the runner's fixed error text
and still checks partial upload, normal queue retry and unchanged job/files/card.
Exact completion replay is asserted idempotent; a changed replay still conflicts.
No build handler, manifest version/hash contract or deployment gate is weakened.

## Validation scope

Use synthetic Python fixtures and ephemeral Miniflare D1/R2/KV. Targeted tests
cover stale workers, cross-campaign refusal, competing completion, every atomic
write boundary, second followup failure, acknowledgment loss, bounded expiry,
migration evidence retention, heartbeat/transport fencing and durable restart.
Container image construction remains a CI gate; no deployed applicability is
claimed. Independent source review and exact-head CI precede any merge.
