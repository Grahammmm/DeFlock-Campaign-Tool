# WP2 reliability repair: fairness and lifecycle outbox

This additive repair addresses WP2-R1/R2/R3. Existing review reproduction files
and failure evidence are retained separately. No live exporter, schedules, host
configuration, canonical evidence, frozen WP4 files or composition tests change.

## Bounded fairness

Stage selection now evaluates required preceding-stage acceptance before LIMIT.
Missing, blocked, backoff-delayed or exhausted predecessors do not allow their
pending descendants to consume the bounded selection. Eligible untried tasks
precede retries. The existing per-task retry limit/backoff and total max_work
attempt bound remain. This is not a promise of optimal scheduling under infinite
incoming work; no failed document can permanently hide unrelated eligible tasks
behind an ineligible bounded prefix.

An account-scoped persisted folder cursor rotates the next run to the folder
after the last serviced folder. Repeated failed UIDs and continuously busy early
folders cannot consume every later run before other folders are attempted. Total
message attempts never exceed max_messages, including failed attempts. Failed
UID checkpoints remain unchanged. Empty folders and UIDVALIDITY changes keep
native identity semantics. No receipt is marked received based on rotation.

Provider inventory order is never used as the scheduling order: the available
configured scopes are first normalized to the trusted profile's explicit folder
list, then rotated using the persisted cursor. A provider changing its order
between runs cannot repeatedly move a healthy folder ahead of the cursor to
starve it. Unconfigured/missing-folder alerts and provider UIDVALIDITY values are
preserved. No public interface, schema, workflow, launcher or profile file changes.
Synthetic adversarial reordering regressions exercise max_messages=1 and 2 with
three repeatedly failing folders, a healthy fourth folder, new provider objects
on every run, strict per-run attempt bounds, and unchanged failed checkpoints.

## Durable canonical lifecycle requests

A private runner_lifecycle outbox records exact identity, operation, outcome and
summary before calling the canonical backend. These payload fields cannot be
updated/replaced/deleted. Recovery and finish acknowledgment retries select this
outbox independently of the originating control run's status. An interrupted
control row does not discard its pending canonical work.

A failed finalization is retried with the SAME intended outcome and summary;
it is not replaced with a contradictory failed outcome. A crash after canonical
commit but before acknowledgment safely replays the idempotent canonical call.
Finish acknowledgment and the control result reconciliation are one sidecar
transaction. No distributed atomicity with the canonical database is claimed.
A backend startup exception after commit also leaves a recovery request.

Lifecycle retries are bounded separately to min(max_work, 100) requests at run
startup, plus at most one current-run finalization/recovery request. Backoff starts
at retry_delay and increases exponentially, capped at one day. Unlike domain
work, these durable I/O intents are not silently abandoned at retry_limit; they
remain visible pending until a recognized acknowledgment. Failure narratives stay
redacted. A single failed lifecycle request does not prevent unrelated requests
or normal work from progressing. Pending lifecycle work means completed_with_gaps,
never release readiness.

## Schema and API compatibility

Base runner schema version/checksum remain unchanged. journal() transactionally
installs a separately checksummed reliability extension with a folder cursor and
lifecycle outbox. Existing v1 histories/checkpoints/work survive. Unknown base or
extension checksums fail closed. Up to 100 historical interrupted or explicit
canonical_finalization_pending runs without intents are enrolled for recovery per
run. Exact finalization details already lost by old code are not fabricated:
recovery closes still-running old canonical entries as interrupted; already-final
canonical outcomes remain unchanged.

No public run(), Exporter, Preserved, or backend method signatures change.
Backend recover_run acknowledgment is finalized/already_finalized/absent;
finish_run acknowledgment is finalized/already_finalized. Existing CanonicalMailBackend
already implements these outcomes. Identity/outcome/summary must be idempotent.

Added summary fields: canonical_lifecycle_retried (prior intents acknowledged in
this run), canonical_lifecycle_pending (remaining durable intents), and
canonical_run_finalization='pending' while the current finalization lacks an ack.
Existing counts retain their meanings; release_ready remains intentionally false.

Modified engine code changes the runtime fingerprint. Existing profile files are
not edited: trusted startup must prepare newly pinned profile bytes before use.
No production activation is performed. The old code does not consume the new
outbox; do not revert an activated runtime without reconciling its pending intents.

## Synthetic acceptance

Tests exercise max_work/max_messages 1 and 2, blocked ancestors, continuously busy
and failed early folders, retry limits, unchanged failed-UID checkpoints, v1
extension migration, checksum rejection, recovery failure, finalization retry,
crash-after-canonical-commit, startup response loss, immutable intents/backoff,
and recovery of older pending-finalization events. WP1 overlay databases and mail
are synthetic. Passing tests do not certify a real unattended pipeline or website.
