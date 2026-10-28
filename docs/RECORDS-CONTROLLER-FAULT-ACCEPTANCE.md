# Controller kernel-fault acceptance

This is a synthetic Linux acceptance layer for the accepted host and systemd
controller, not a production change or deployment acceptance. It uses the existing
private-profile and injected Docker fixtures. No Docker executable, systemd unit,
mail, record, provider, model, or external service is invoked.

## Covered boundaries

- A real child calls `os._exit(73)` immediately after operation-intent write
  returns, following both file and directory fsync. Run and stop intent survive;
  a fresh process rejects ordinary host admission and managed new invocation.
- Secondary HOLD file-fsync and directory-fsync faults exercise actual
  `os.fsync` interception, not a mocked write exception. The earlier durable
  operation remains the deny-first authority. A visible HOLD name after a failed
  directory fsync is expressly not treated as durable.
- Initial intent file-fsync failure permits an independently successful fallback.
  Initial directory-fsync failure is fenced by a separately successful HOLD.
- When every initial intent and HOLD file fsync fails, the current operation
  returns held with zero injected Docker calls and unchanged job identity.
  **No durable restart-denial claim is made for total storage failure.**
  A controller cannot reconstruct an intent that never became persistent;
  operators need an independent durable admission interlock/reconciliation
  before permitting another writer. These tests do not simulate power loss,
  remount, privileged journal deletion, or a storage device lying about fsync.
- Actual Linux flock contention blocks stop behind cancel/admit and blocks a
  late allocation behind an in-progress stop intent. Pipe handshakes observe
  a real BlockingIOError and a completed intent write, rather than infer
  serialization from arbitrary sleeps. The stop seals no-allocation before the
  allocator proceeds; the same reservation is rejected again after restart.
- Corrupt binding after real controller exit still rejects fresh admission.

Every child is registered for bounded terminate/kill/wait cleanup immediately
after Popen, before assertions or handshakes. Signals target only owned, unreaped
Popen children, never journal PIDs. All waits are bounded. Tests check fixed
outcomes and injected call counts, not arbitrary worker arguments or exceptions.
Child environments are explicitly constructed without ambient model settings.
Record content cannot choose a callback, child action, executable, or script.

## Run and evidence

```sh
python3 -B -m unittest tests.records.test_controller_kernel_faults -v
```

Use a private trusted TMPDIR or the test user's trusted home. The dedicated CI
workflow runs this slice on Python 3.11, 3.12, and 3.13 on Linux and rejects any
skip. Existing workflows retain full offline suites and credential scanners;
the new workflow also runs the public-tree and repository secret checks.
Keep the exact commit/tree, command, terminal count/status, scanner results,
and CI head binding in the author's private receipt. Failed evidence is retained,
not overwritten or presented as acceptance.

## What this does not certify

Fake Docker is still injected. This slice does not prove container or cgroup
quiescence, systemd service wiring, independent host watchdog behavior, OCR,
intake completeness, board exports, paid-service-free end-to-end operation,
or live scheduling. Prior source acceptance is preserved, not silently expanded.
Real-host containment and full combined-release independent acceptance remain
separate gates. Nothing here activates a unit, timer, image, or pipeline.
