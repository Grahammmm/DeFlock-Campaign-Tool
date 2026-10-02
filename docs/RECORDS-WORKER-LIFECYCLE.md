# Linux worker lifecycle sidecar

This generic standard-library engine is an inert module, not an enabled service.
It does not change the records CLI, run safety, unattended gates, scheduler,
images, templates, provider configuration, or any running container. It requires
Python 3.11+, unprivileged Linux, mounted procfs, pidfd_open/pidfd_send_signal,
SO_PEERCRED, and PR_SET_CHILD_SUBREAPER. Unsupported primitives fail closed.

## Launcher and stop contract

Run the supervisor **inside the worker container**, in a dedicated interpreter
with one thread, default SIGCHLD handling, and no preexisting children. Supply a
fresh, absolute state directory under an owner-controlled private parent. All
path components must be directories, not symlinks. Never reuse a run directory.

```sh
python -m campaign_tool.records.worker_lifecycle supervise \
  --state-dir /path/to/runtime/unique-lifecycle --wall-seconds 2700 \
  --term-seconds 5 --kill-seconds 5 -- \
  python -m campaign_tool.records run --root /path/to/records \
  --mail-config /path/to/mail-config.json --unattended \
  --max-originals-per-run 200 --ocr --json
```

The command above is a future integration interface, not deployment approval.
The lifecycle engine does not implement or authorize the records command flags.
There are no service changes in this slice. In particular, killing a host Docker
exec client is not worker cancellation, and killing a whole container is not the
stop contract. A future host ExecStop must execute the following **in the same
container/PID namespace as the supervisor**, as its unprivileged UID, and wait
for its exit code and fixed JSON response:

```sh
python -m campaign_tool.records.worker_lifecycle cancel \
  --state-dir /path/to/runtime/unique-lifecycle --wait-seconds 65
```

For example, the host orchestration layer may run that command through an
already authorized exec transport. It must not merely signal the exec client.
No transport, container name, or deployment configuration is embedded here.
A zero cancel exit code means an authenticated **live supervisor** acknowledged
that all its worker descendants were reaped and its bound receipt was persisted.
A missing, stale, invalid, or timed-out ACK must leave the stop operation failed.
Reading an old receipt alone never authorizes a successful stop.

## Public Python API and exit codes

`supervise(command, state_dir, *, wall_seconds=2700, term_seconds=5,
kill_seconds=5, max_owned=256)` returns fixed metadata. It must own the dedicated
interpreter's child tree. It is not a helper to embed in a threaded application.
`cancel(state_dir, *, wait_seconds=65)` returns the authenticated result.
`exit_code(result, cancelling=False)` maps results to CLI codes. API preflight
errors raise `Rejected` or `Unsupported`; CLI exceptions emit fixed metadata,
never arbitrary exception strings, arguments, paths, environment, or tracebacks.
Worker stdin/stdout/stderr are DEVNULL; private worker output is not published.

| Status | Supervisor exit | Cancel exit |
| --- | --- | --- |
| completed | 0 | 0 only with quiescent live ACK |
| worker_failed | 10 | 0 only with quiescent live ACK |
| cancelled | 20 | 0 only with quiescent live ACK |
| timed_out | 21 | 0 only with quiescent live ACK |
| invalid_state | 30 | 30 |
| unsupported | 31 | 31 |
| not_quiescent | 32 | 32 |
| capacity_exceeded | 33 | 33 |

Output has exactly `schema`, `status`, `quiescent`, `worker_exit`, `escalated`.
`worker_exit` is the leader's numeric exit code or signal, not its output. A
completed leader is not enough: leftover descendants are terminated before the
engine can report completed. Failure statuses are never successful cancel ACKs,
even when emergency cleanup managed to reap the entire tree.

## Identity, ownership and bounded cleanup

The worker starts in a new session/process group. The supervisor sets itself as
a Linux child subreaper **before** starting it. Double-forked daemons, session
escapes, leader exit, and descendants forked by worker threads are handled by
kernel adoption, not an assumption that the original process group is intact.
Cleanup signals only currently direct children; after parents exit, descendants
are adopted and cleanup continues. ECHILD from waitpid, not an empty snapshot or
leader exit, is the quiescence proof. The Linux contracts are documented in
[PR_SET_CHILD_SUBREAPER](https://man7.org/linux/man-pages/man2/PR_SET_CHILD_SUBREAPER.2const.html),
[pidfd_send_signal](https://man7.org/linux/man-pages/man2/pidfd_send_signal.2.html),
and [wait](https://man7.org/linux/man-pages/man2/waitpid.2.html).

Every signal target is obtained from this supervisor's kernel child list, with
UID, parent and process-start identity checked before and after pidfd_open.
Signals use pidfd_send_signal exclusively, never kill(pid), killpg or a PID from
control state. A changed identity fails closed without signaling that target.

The fresh directory is 0700 and state, socket and receipt are 0600, owned by the
current UID. File operations use anchored directory FDs, no-follow opens,
regular-file/owner/mode/one-link/size checks. The immutable state is pinned by
bytes plus device/inode/size/mtime/ctime. Its boot ID, PID/start/UID, random run
identity and random cancellation capability bind the socket exchange. The
client pins supervisor identity with a pidfd and verifies socket peer credentials.
The server verifies UID and capability; malformed or partial clients cannot
extend the worker budget indefinitely. Tokens never appear in CLI output.
Receipts bind boot, process and run identities to the fixed result, without
storing the worker command, environment, or cancellation capability.

Wall time is capped at 2700 seconds, measured monotonically starting before
launch. TERM and KILL grace intervals are each positive and at most 30 seconds.
The defaults bound shutdown to approximately 10 seconds beyond the work budget;
maximum configured grace bounds it to approximately 60 seconds. Polling and
bounded handshakes add small scheduling latency. Cancellation waits at most 65
seconds. There is at most one 25ms control handshake per tick and eight waiting
ACK clients. More than `max_owned` (1..256) currently direct children triggers
capacity failure and cleanup. Reads and reap loops have hard size/count caps.
This is detection, **not a kernel-enforced fork/memory/CPU quota**: nested live
children are counted when adopted. Fork bombs exceeding the 4096-child hard
inspection/reap cap cannot be certified, and need a separately approved cgroup
resource policy. Kernel stalls and uninterruptible tasks can outlive KILL; the
receipt must then say `not_quiescent`, never success.

## Boundaries and operational gaps

This is for non-hostile workers and a private, trusted same-UID runtime. It is
not a sandbox against a worker deliberately attacking the supervisor, modifying
its memory/files, changing UID, escaping the PID namespace, arranging external
ptrace/reparenting, or spawning through an unrelated external service. The
same UID has access to the cancellation capability; restrictive files do not
isolate malicious same-UID code. Privilege changes are not authorized kill
targets. Cgroup containment or privilege separation needs separate work.

Supervisor SIGTERM/SIGINT/SIGHUP request bounded cleanup. Supervisor SIGKILL,
container teardown, interpreter crash, or exceptional kernel I/O can prevent
cleanup; no successful receipt may be inferred. A supervisor crash does not
provide an independent watchdog. This module does not restart Docker, services,
or workers. External reconciliation after an unacknowledged stop remains an
operator responsibility. Do not activate unattended work until independently
reviewing the exact integration tree and its containment assumptions.

## Synthetic tests and CI

```sh
python -B -m unittest tests.records.test_worker_lifecycle -v
python -B tools/check_public_tree.py
node scripts/scan-secrets.mjs
```

Existing offline/intake/release Linux CI runs unittest discovery and therefore
collects these tests without new workflows. Tests use private temporary
synthetic workers: real signals, TERM-ignore/KILL, wall timeout, fork nesting,
double-fork/session escape, thread-created descendants, unrelated-process
survival, live authenticated cancellation, malformed/partial clients, stale and
mutated state, symlinks/FIFOs/hardlinks/modes/size, capacity and budget caps.
A mocked process-start race checks the pidfd guard without attempting actual
PID reuse. There is no claim of exhaustive kernel-race, hostile-worker, namespace,
D-state or fork-bomb coverage. Linux tests intentionally fail for root or missing
required primitives rather than silently claiming success. Non-Linux skips are
not a Linux acceptance receipt. Only synthetic inputs are used; no records,
mail, credentials, models or provider calls are needed.

Independent exact-head/tree review and remote CI are separate acceptance gates.
No merge or service activation is authorized by passing local tests.
