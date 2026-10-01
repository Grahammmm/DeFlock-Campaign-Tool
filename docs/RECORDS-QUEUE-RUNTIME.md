# WP7 additive durable queue continuation

queue.py is unchanged. queue_runtime.QueueRuntime adds private continuation and
packet persistence around its first-eligible-stage and scoring semantics. No CLI,
timer, cloud/model call, portal operation, promotion or publication is added.

## Trusted API and dependencies

At installed startup create QueueRuntime(stage_runner, private_root,
prepare=installed_local_adapter, adapter_id=..., adapter_version=...,
prepare_timeout_seconds=30). The runner must be an actual WP1 StageRunner, whose
owner/run/profile/config and live run remain authoritative. Call advance with
optional priority facts, max_attempts (1..20) and budget_seconds (at most 2700).
read_packet(attempt_id) verifies the persisted bytes and current WP1 claim.
close() releases the control database connection.

Only trusted installed startup selects the adapter callable. No record, priority
fact or preparation result selects a callback or executable. The fixed local
adapter receives metadata only and returns either:
- denominator: {kind: bytes/pages/rows/sheets/items, total: nonnegative integer},
  derived_text: absolute private paths, page_images: absolute private paths.
- blocked_reason: a bounded stable lower-case code.

It has no runner argument and must not mutate evidence, invoke models or acquire
publication authority. It assembles metadata, not stage content. Stage content
must already be registered through the installed WP1 domain adapter: unprepared
content is journaled as current_content_required, not fabricated by this runtime.

WP1 is a composed dependency, never copied into WP7 or modified. Claim creation
uses StageRunner.claim. Packet binding/replay reuses WP1's read-only _subject,
_head, _inputs and _check_claim checks together with the runner's live authority
check. This narrow internal API dependency must be pinned/tested when releases
compose; a future public read-only claim-validation API can replace it.

## Fairness without planner weakening

Each advance reads a consistent bounded canonical metadata snapshot. It validates
the entire inventory through queue.plan, then asks the unchanged planner for slices
of at most twenty originals so every eligible item can participate in continuation.
Eligibility, first unfinished eligible stage and priority scores are unchanged.

A private monotonic visit cursor prioritizes never-attempted eligible work, then
least-recently-attempted work, using original queue priority/hash/stage order as
ties. Visits are committed before WP1 claim attempts. Unprepared or contended top
twenty cannot hide eligible item twenty-one on the next call. Resumable packet
writes share the same fairness cursor rather than always running ahead of new work.
This is attempt fairness, not a promise of deadline scheduling or corpus completion.

Snapshot-original count remains full, excluded originals stay in that denominator,
and scope exclusions never become new claims. Actual WP1 rechecks concurrent
scope/lease/prerequisite changes. Ordinary per-item failures are journaled; authority
configuration failures fail closed rather than being misrepresented as contention.

## Durable control and packet recovery

private_root must already be owner-only, absolute, non-symlink and outside Git.
The owner-only SQLite control journal uses FULL synchronous mode and a private
nonblocking writer lock. It is separate from the canonical ledger. It stores
attempts, reasons, original selection metadata, claim token, exact packet bytes
and hashes, and a fairness cursor. It never writes WP1 tables directly.

An intent is committed before the claim. A process failure after WP1 commits but
before the sidecar acknowledges it is recovered by the same configured runner
reusing WP1's existing live claim. Foreign-owner/run attempts are not adopted.
A new run can select work normally once WP1 permits a new claim. Expired original
attempt deadlines never silently extend during recovery.

Packet bytes are journaled before being written into a fsynced private temporary.
A no-overwrite hard link exposes only complete final bytes; the packet directory is
synced before marking packet_ready. Existing files must match exact journal bytes.
Failure before exposure leaves no partial final artifact; failure after exposure
replays the same bytes without another prepare call. Abandoned temporary directories
after hard process death are not packets and may be removed by a separate private
maintenance process; no automatic broad deletion is implemented.

Packets contain only metadata/path references, the explicit supplied denominator,
WP1 content revision/hash/prerequisite receipts and claim, and an unfilled receipt
template. verdict, reviewer, coverage covered count and substantive review fields
remain null. Path reference stat identities are recorded and rechecked on read;
reference_bytes_verified is false because source bytes are not read or hashed.
Declared denominators are bound, not independently proven exhaustive. Canonical
unit/locator/coverage assembly remains the installed metadata adapter's job.

## Time and resource scope

An attempt has a durable maximum 45-minute deadline. WP1 lease requests never
exceed 2700 seconds and are shortened on intent recovery. Each advance also has a
bounded wall-time budget checked between operations. SQLite operations retain
their short lock timeout; this is not a hard real-time bound over kernel I/O.

The adapter runs in a POSIX fork child with a separate process group, redirected
diagnostics, an alarm and parent timeout/kill/reap cleanup. Its default timeout is
30 seconds, capped by remaining attempt and call budgets. No arbitrary external
executable is launched. A bounded preparation failure is stored privately.
The installed callback is trusted code, not a hostile-code sandbox. An orphaned
child from parent process death has its own alarm; real-host orphan/crash cleanup
still needs deployment acceptance testing.

Limits: planner inventory 10,000 originals and 70,000 slots/leases, 20 attempts per
advance, 1 MiB packet/adapter output, 1000 combined path references and 256 MiB
control database admission bound. Control history retention/compaction is deferred.
Blocked/failed packet preparation leaves the WP1 claim to expire normally; it
does not rewrite operational leases or promote a blocked/accepted stage itself.

## Synthetic acceptance and remaining integration

Tests compose the actual WP1 StageRunner using test-only profiles. Cases cover
unprepared top twenty plus healthy twenty-one, fairness against persistent packet
write retries, lost claim acknowledgement, lost packet acknowledgement, partial
writes, another live owner, excluded scope, timeouts, stale claims and references,
exact packet bindings, private paths and no completion promotion.

Run the new and existing queue suites with WP1 present in package composition.
Dependency absence is an explicit skip and is not a passing integration result.
No production callbacks or real corpus have been exercised. Parent owns PR24,
independent review, release composition and CI. Existing WP7 preview/claim APIs
remain unchanged; root CLI activation is not part of this addition.
