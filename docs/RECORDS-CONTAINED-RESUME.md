# Contained one-item resume composition

`contained_resume` composes two existing engine components: the opt-in
`bounded_resume` request/output/deadline boundary and the Linux
`worker_lifecycle` pidfd/subreaper supervisor. No installed mail adapter,
credential supplier, service, schedule or deployment is included.

The public CLI remains inert plan mode. `--execute` without an explicit reviewed
binding fails closed; there is no worker CLI switch or adapter discovery flag.
The Python API is intended for a **fresh dedicated interpreter**, under the
unprivileged Linux/runtime prerequisites in
[the lifecycle contract](RECORDS-WORKER-LIFECYCLE.md).

```python
from campaign_tool.records.contained_resume import run_once

# Explicitly reviewed code and a fresh private state path supplied by the owner.
# Values here are illustrative, not an installed configuration or approval.
result = run_once(
    "/private/runtime/reviewed_adapter.py",
    sha256="<exact reviewed source SHA-256>",
    state_dir="/private/runtime/fresh-run-state",
    execute=True,
    seconds=120,
)
```

The standalone adapter exposes `run(request)`. Its exact source bytes are
captured through no-follow descriptor walks, owner/mode/type/link/size checks and
SHA-256 verification. Those captured bytes are compiled; the pathname is not
reopened for import. Import and execution both happen inside the inner
deadline/output fence. Import-time behavior, dependencies, credentials, writes,
and recovery effects must be reviewed as part of the specific adapter; a source
hash is byte identity, not permission or a transitive dependency lock.

The immutable request still requires one item, no notifications, no pruning and
no child processes. This wrapper does **not** admit a subprocess-using adapter.
The outer supervisor provides an additional recovery boundary for accidental
descendants. It adopts orphans and session escapes and signals only its pinned
kernel-owned children, without numerical process-group kills. Leader exit alone
cannot establish completion. A successful result requires the outer quiescence
receipt and a successful inner worker exit.

An internal worker entry checks the fresh lifecycle state against its actual
parent identity and fixed budget/cleanup/ownership parameters before importing
adapter source. This binds a trusted same-UID invocation; it is not authentication
against a malicious same-UID process able to forge state or alter dependencies.
The module does not discover or load mail configuration and provides no fallback
credential path.

The total configured budget accepts 6–120 seconds. Two seconds are reserved for
outer TERM/KILL cleanup, leaving the outer supervisor a wall budget of at most
118 seconds at the default. The inner `bounded_resume` deadline is a further
three seconds shorter (115 seconds at the default), so the inner fence, its own
up-to-two-second worker cleanup and interpreter startup finish strictly before
the outer wall timer. An inner timeout exits the leader with a dedicated code
that is reported as `timed_out` with no completion claim.

The inner fence waits on the worker process id (`waitpid`), not on the
multiprocessing sentinel pipe. Every forked descendant inherits that pipe, so
waiting on it let a surviving accidental descendant hold the inner fence open
until its deadline after the worker had already exited.

Polling, kernel scheduling and uninterruptible I/O can exceed nominal timing; this is not
a hard real-time deadline. Unproven cleanup reports failure. A fresh lifecycle
directory retains its own private state/socket/receipt; canonical sync status,
failure markers and production checkpoints are not written by this module.

## Qualification and remaining limits

The new focused contract is:

```sh
python3 -B -m unittest tests.records.test_contained_resume -v
```

It has ten admission/source tests and five Linux composition tests: exact
request and import/raw-output suppression; leader-exit cleanup of a pinned
session-escaped descendant; timeout cleanup with no completion claim; changed
source refusal before effects; and unrelated-process survival. The admission
tests also pin the inner/outer budget relationship and the inner-timeout
mapping. `tests.records.test_bounded_resume` adds a regression case showing that
a surviving forked descendant no longer holds the inner fence open after the
worker exits.

Failure history: at head `7d83f2531fa83a4e55c6adf15c66483334fe1310` the
leader-exit composition case failed deterministically in every hosted job
(Python 3.11/3.12/3.13), returning a quiescent `timed_out` instead of
`completed`. The cause was the inherited-sentinel wait combined with equal
inner and outer deadlines, not the test fixture. That failure is preserved here
and in the PR history; it is not reset by the later fix.

Forking in the
negative fixtures deliberately violates the no-child request to exercise the
outer fence. It does not permit real adapters to fork. Descendant tests pin the
original identity with a pidfd and require exit plus a stopped synthetic writer.
Root/non-Linux execution cannot be called qualified Linux acceptance.

Seal and independently review the exact source/test/docs before admitting a new
qualified Linux integration invocation. Reuse existing component receipts rather
than manually rerunning their passing suites. Preserve prior failed attempts;
this new composition does not reset any installed acceptance budget.

Supervisor SIGKILL/crash, hostile same-UID code, privilege/namespace escape,
external-service spawning and fork/resource exhaustion retain the lifecycle
deployment holds. A separately reviewed per-job runtime kill domain and live
reconciliation are still required for unattended adoption. Passing this isolated
composition does not clear that P1 hold, qualify a private mail credential
supplier, prove a real export durable, or authorize installation or production
execution. Rollback is to stop using the new wrapper and retain all resulting
immutable evidence. Existing entrypoints and schedules remain unchanged.
