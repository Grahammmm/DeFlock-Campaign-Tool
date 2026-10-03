# Receipt-aware systemd records control

Generic stdlib module: `campaign_tool.records.systemd_control`. No units, timers,
images, credentials or deployments are installed. Synthetic tests call injected Docker
and real private filesystem/flock, never providers or systemd.

## Commands and identity

```sh
python -B -m campaign_tool.records.systemd_control run --profile /private/profile.json --invocation-id 0123456789abcdef0123456789abcdef
python -B -m campaign_tool.records.systemd_control stop-post --profile /private/profile.json --invocation-id 0123456789abcdef0123456789abcdef
```

Use the same systemd manager supplied INVOCATION_ID for ExecStart and ExecStopPost;
it must be exactly 32 lowercase hex characters. Never derive authority from timestamps,
PID, a container name, records content, a command string or an untrusted callback.
The caller must bind the exact same immutable private profile and fixed host journal.
No ID/hash/command/environment/error text is printed. Output has only the host adapter's
schema/status/quiescent/healthy/cards fixed fields. Python APIs accept explicit
invoke/containment injection for trusted tests only, never from JSON or record inputs.

Host extensions: `run(..., job_id=None)`, `Journal.allocate(job_id=None)`,
`stop(..., expected_job_id=None)`; CLI `run --job-id` and `stop --expected-job-id`.
Normal unbound calls retain their behavior outside a managed journal. Do not switch
journals/profiles to bypass HOLD. A preassigned ID cannot reopen a closed reservation.

## Durable binding and admission

Under launch.lock and journal.lock, control chooses a unique random32hex job. Before
ANY Docker operation it writes a managed-mode marker, pending invocation pointer,
immutable invocation-ID mapping and immutable job reservation; all owner0600, one-link,
no symlinks, under the existing host owner0700 trusted private directory with fsync.
Mappings contain only schema, invocation ID, job ID and exact profile SHA256.

Metadata is separate from the existing host receipt schema; no implicit schema upgrade,
private inputs or cancellation capabilities are added to host receipts. Partial metadata
fails closed, including a mode marker with a missing pointer or a pointer lacking seals.
Duplicate invocation run is HOLD, not replayed worker admission. Previous reservations
must have a finalized quiescent job or sealed not-allocated closure before a new prepare.

Accepted allocation checks this metadata under journal.lock; worker admission checks
again under cancel-admit.lock. Unbound allocation cannot bypass a pending reservation.
An independent durable systemd-hold.json fence is serialized with journal/cancel-admit,
and blocks every next allocation/admission. Broken/mutated metadata also blocks admission.
No automatic fence deletion or override is provided; an owner must independently
reconcile corruption/uncertainty. Existing host profile trust and privileged-owner
limits apply; another privileged operator can defeat application fencing.

## Stop-post outcomes

Stop-post loads the exact immutable invocation and job seals, not whatever active job
happens to exist. Accepted stop rechecks expected job identity inside stop.lock, during
the atomic cancellation change and again after admission.lock before Docker mutation.
A pointer race cannot cause cancellation of a subsequent job.

A finalized quiescent success for this invocation returns completed without any Docker
call, cancellation commit or receipt rewrite. A finalized failure, cancelled job or
failed post-run remains postrun_failed/nonhealthy, also without rewriting. Repeated
stop is idempotent; an older finalized invocation does not cancel a subsequent writer.
That outcome belongs only to the invocation, not proof that a subsequent job is quiet.

A complete sealed binding with NO job directory is closed under journal/cancel-admit
locks as not_allocated. It returns nonzero, quiescent=false, healthy=false: no fake
worker ACK, no adoption/cancellation of another active job. Late allocation of that
reservation is denied. A job directory without a receipt, receipt without active
pointer, allocated job without CID, missing mapping, stale profile, broken seals or
unknown runtime is uncertainty/HOLD, never guessed no-allocation or success.

An unfinished matching job uses independent accepted stop: live supervisor cancellation,
bounded exact-CID escalation, terminal plus observed empty containment, removal,
then private board/health. No old disk success replaces live cleanup proof. Cleanup
failure keeps the job held and permits a matching safe retry; identity uncertainty
sets the separate durable admission fence. A healthy board/global health slot cannot
overwrite a failed/cancelled/unproven worker outcome.

## Exit codes

| Code | Fixed status | Meaning |
| --- | --- | --- |
| 0 | completed | This invocation finalized naturally, quiescent and healthy |
| 10 | postrun_failed | Quiescent finalized failure/cancellation/post-run gap, never healthy |
| 11 | not_allocated | Sealed invocation closed before allocation, no quiescence claim |
| 30 | invalid_state | Invalid CLI/profile/ID, no arbitrary exception output |
| 32 | held | Uncertain identity/cleanup; next writer denied |

Do not prefix ExecStopPost with '-' or translate every nonzero into success.
Do not blindly invoke the old stop after natural completion. Do not combine status0
with service-manager failure to claim the pipeline healthy.

## No-paid and containment contract

The accepted host adapter still uses env -i for workers/reporters/probes, empties primary
and challenge MODEL_BASE_URL and sets strict_local. No ambient model/API-key forwarding,
analysis-config flag, paid service, new package or model is added. Operational extraction,
OCR, catalog, detectors and board remain separate from human/model review and publication.
The existing approved worker command/flags are unchanged. Combined accepted runner and
intake interfaces are an independent release gate, not an optional cloud-analysis dependency.

Per-run private PID namespace/init/security/network policy and exact-CID+empty cgroup
proof remain in the host adapter, not this module. Systemd RuntimeMaxSec/independent
stop-post wiring are still real-host acceptance tasks. This module installs no watchdog,
does not restart Docker/shared containers, and cannot promise bounded killing while
the trusted daemon/kernel is unavailable. Host/controller/manager/reboot recovery and
actual deadline/resource enforcement require isolated host acceptance before activation.

## Synthetic coverage

```sh
python -B -m unittest tests.records.test_systemd_control tests.records.test_container_host tests.records.test_worker_lifecycle -v
python -B tools/check_public_tree.py --patterns-only
node scripts/scan-secrets.mjs
```

New regressions cover binding before every Docker call; exact success/failure no-op;
no allocation/nonzero/late-start fence; controller loss during metadata and both
allocation boundaries; unfinished cleanup; already-finished cancellation; older
completed invocation versus newer writer; mutated seals/profile/deleted pointer;
two active-pointer race points; actual threaded journal flock serialization;
cleanup failure/retry; unknown Docker; duplicate invocation; strict ID/sanitized
output; distinct CLI codes; no-paid clean environment and no JSON callback surface.
Tests create no containers and cannot substitute for real host/systemd acceptance.
