# Disposable container host lifecycle candidate

This reusable stdlib module is an inert candidate, not a deployed service. No
live profile, image, schedule, timer, board server, provider call or publication
is added. Tests create no containers. Main owns runtime provisioning and real
rootless/kernel/systemd acceptance. Worker-lifecycle PR52 remains separate.

## Containment and admission

Each run uses a disposable rootless Docker container, exact accepted image,
UID/GID1000, private PID/cgroup namespaces, --init, --restart=no, --pull never,
network none or the explicitly pinned existing network namespace, read-only root, cap-drop ALL, no-new-privileges, pinned custom
seccomp, bounded memory/CPU/PIDs and bounded json-file logs. Image health checks
are disabled. No host PID namespace, privileged mode, Docker socket or writable
cgroup mount belongs in a worker. All Docker operations use argv, never shell.

Init's direct child waits at a bounded gate. Host persists full CID BEFORE
start, captures kernel cgroup identity and validates accepted release and actual
namespace access before admission. The gate uses os.execve, not fork/Popen, to
replace itself with the supervisor. There is no exec into a persistent container
to run a supervisor. Supervisor death causes init/PID-namespace teardown under
the accepted kernel mechanism; host-client death alone does not imply stop.

Host fsyncs a fresh 0600 HOLD receipt and active pointer before ANY Docker call.
Random job identity/profile digest/role are bound in labels. Every inspect,
stop/kill/remove uses full CID and validates image, labels, runtime/PID/exit.
launch.lock serializes jobs; admission.lock serializes create/start/admit/remove.
Stop durably fences cancellation BEFORE waiting for admission. A delayed start
cannot admit after the fence, and exact-CID removal prevents revival by late
start calls. No stop/restart of a shared original container is provided.

Terminal runtime alone is insufficient. The host binds inspect PID to its
/proc start-time and a unified cgroup-v2 full-CID leaf, persisting path/dev/inode
and boot. Terminal runtime PLUS populated=0 is required, or removal of that
previously observed CID leaf with its surviving parent checked. Replaced inode,
boot drift, populated/unobservable group, daemon failure or missing initial
proof retains HOLD. A never-admitted container still created can be removed
under the fence without running proof. Running containers can be re-observed
if controller loss occurred before capture. Missing host-visible rootless PID
refuses admission, never substitutes an unrelated process.

Ambiguous create/no CID or remove/receipt interruption stays HOLD. No name lookup,
receipt deletion or automatic override guesses that it is safe. Stop can clean
up independently after supervisor/controller death: it attempts live in-container
cancel, then bounded exact-container TERM/KILL, terminal+empty proof and removal.
A missing supervisor ACK does not invalidate independently proven containment,
but it can NEVER substitute for that proof. All calls/waits/output are bounded.
The daemon/host kernel/Docker owner are trusted; another privileged operator can
defeat application fencing. No external watchdog is implemented for daemon/kernel
hang or a living supervisor failing its own wall budget. Those are separate
acceptance/service responsibilities and do not become success receipts.

Sources: [Docker create](https://docs.docker.com/reference/cli/docker/container/create/),
[rootless Docker](https://docs.docker.com/engine/security/rootless/),
[kernel cgroup-v2](https://docs.kernel.org/admin-guide/cgroup-v2.html).
Main has reported exact-image synthetic kernel acceptance for supervisor death
and independent cleanup after client death. This candidate's fake-Docker suite
is not that acceptance. Host/systemd races and independent review remain gates.

## Private profile and public API

Profile: exact-schema JSON regular one-link owner0600 file, bounded16KiB, in an
owner0700 directory, trusted root/current-owner non-writable parents, no symlinks.
Duplicate keys, unknown fields, nonfinite/Boolean budgets, traversal and
inode/byte mutation fail. One fixed host journal must be reused for one root;
do not move it to bypass HOLD. Required fields:

| Field | Contract |
| --- | --- |
| schema | Integer1 |
| name_prefix | Bounded portable prefix, never cancellation authority |
| image_id | Exact immutable sha256 Docker image ID |
| release_commit | Independently accepted commit |
| package_version, dependency_lock_sha256 | Accepted release provenance |
| docker_path, python | Trusted absolute host CLI/container interpreter |
| release_root, parser_path | Read-only accepted imports/work paths |
| root, tmp_dir, worker_state_parent, board_dir | Existing writable private UID1000 paths |
| mail_config | Existing read-only owner0600 file |
| host_receipts | Host owner0700 control directory, never worker-mounted |
| wall_seconds | Finite positive maximum2700 |
| owner_launch_approved | Boolean gate, not acceptance by itself |
| network | Exact mode/container_id object: none/null, or container/full CID |
| mounts | 1..16 kind/source/target/readonly objects, optional subpath; bind or trusted volume |
| seccomp_path, seccomp_sha256 | Trusted host security file and exact digest |
| memory_bytes | 16MiB..32GiB |
| pids_limit | 32..4096 |
| cpus | Finite0.1..16 |

Mounts are trusted provisioned data, never sockets/control data. Bind traversal
is checked; profile, seccomp and host journal cannot be exposed via a bind
ancestor. System/control targets are rejected. Release/parser/mail paths must
be RO-mounted, data/TMP/lifecycle/board RW-mounted. Named volumes are an operator
trust boundary and must be provisioned without sockets/control surfaces.
Optional volume-subpath is a bounded relative path with no traversal, empty
components, control characters or CSV delimiters. It supports minimal directory
and single-file mounts, including a readonly mail configuration at its original
absolute path; no copying/recovery/read of credentials is needed. Bind mounts
cannot have a subpath. A mail file must itself be owner0600 and regular; trusted
non-writable parents (including root) need not be private0700. Data finals do.
Actual UID1000 namespace access and modes are checked in-container; host0700
alone is NOT proof under rootless UID maps. Production workers may reuse ONLY the authorized existing network namespace
via --network container:FULL_CID. Target full ID/running state is checked before
create, start and admission; runtime NetworkMode must match. No bridge/host/name
fallback, new route, firewall edit, portal permission or target stop is provided.
Reporters and synthetic fixtures always use network none. Private per-run PID
namespace/init/restart policy is validated separately in runtime inspection.
Main provisions the target CID and must verify shared-network compatibility and
existing egress restrictions under the actual rootless image/mount profile.
No chown, permission widening,
directory provisioning, image pull/build or data repair is performed. Existing
named volumes are required via inspect; volume-nocopy disables implicit content
initialization during create.

```sh
python -m campaign_tool.records.container_host run --profile /private/profile.json
python -m campaign_tool.records.container_host stop --profile /private/profile.json
```

Python APIs: run(profile_path, invoke=call_argv, containment=None), stop(...).
Injection is for tests, never JSON-configured. env -i selects fixed PATH/imports/
TMPDIR/Python isolation, disables primary/challenge model endpoints and sets
strict_local privacy. No inherited host/container credentials. Version proof
uses accepted records version --json: exact commit/package/dependency lock,
source_dirtyfalse, ledger schema1 and known installation kind.

The future accepted worker command is PR52's documented interface:

```text
python -m campaign_tool.records.worker_lifecycle supervise
  --state-dir UNIQUE_PRIVATE_JOB_DIR --wall-seconds 2700 --term-seconds 5 --kill-seconds 5 --
  python -m campaign_tool.records run --root PRIVATE_ROOT --mail-config PRIVATE_CONFIG
  --unattended --max-originals-per-run 200 --ocr --json
```

This module does not add/change run flags. Missing accepted interfaces fail
closed. Natural completion requires fresh fixed completed/worker-exit0/
quiescenttrue supervisor output, runtime exit0, terminal+empty proof and removal.
Old disk receipts or stopped Docker clients cannot clear HOLD. Reporter cleanup
uses the same independent containment contract. The next writer is denied until
worker AND reporter are finalized, or the owner independently reconciles HOLD.

CLI output contains only fixed schema/status/quiescent/healthy/cards. Host exits:
0 completed/quiescent;10 postrun_failed;30 invalid_state;32 held. Internal gate/
report transport uses exit0 for a fixed report, even if report says postrun_failed;
that never means healthy. Raw stdout/errors/args/env/credentials/cancellation tokens
are never printed or saved in host receipts. Worker PR52 retains its own codes.
Client timeout signals only its owned Popen handle, never arbitrary host PIDs.
Transport cap256KiB; admission wait65s; individual calls/TERM/KILL/observation/
report wait/health are bounded. Stop is independently executable by Docker owner.

## Private post-run board and health

Only AFTER worker terminal+empty+removal, a second disposable accepted-release
reporter passes the same preflight and admission. It takes existing ROOT run.lock,
reads public ledger.store.ledger(..., readonly=True) in one SQLite snapshot and
uses public catalog_board.render_board. Its authority comes from host containment,
not stale supervisor state. Standalone metadata still requires a bound quiescent
worker receipt. No records/stages/leases/score/original mutation occurs.

Every original gets one canonical hash card, including out-of-scope and all
preserved states. Seven-stage counts reconcile. Type/score/dates come only from
current receipt-bound/content-hash-verified catalog classification; missing means
Unknown, corruption fails. Typed agency labels are allowlisted. Model/tool review
is distinct from declared/unverified human review. Titles/body/quotes/signed URLs/
paths/credentials/low-value rationale/raw mail are never rendered. No re-digest,
fabricated score, low-value closure or publication. Public HTML renderer escapes.

Fresh owner0700 per-job exports contain owner0600 catalog.json/index.html/receipt.json
with original/card/receipt counts, stage counters and hashes. No overwrite;
partial exports have no successful receipt. A separate fixed-field health.json
records fresh sanitized health status/exit/counts without raw CLI output. Caps100000 cards/64MiB fail, not
truncate. Board receipt never says healthy. Host healthy requires reconciled
board AND fresh accepted records health --root ... --json statusok/exit0, without
--record. Failed health/board/reporter is non-healthy. Reporter terminal+empty+
removal is required before admitting another writer.

## Tests and remaining acceptance

```sh
python -B -m unittest tests.records.test_container_host -v
python -B tools/check_public_tree.py --patterns-only
node scripts/scan-secrets.mjs
```

Tests inject fake Docker, synthetic private profiles/cgroups and SQLite roots;
cover CID-before-start, security argv, no exec-supervisor, cancellation races,
stale identity, TERM-resistant escalation, supervisor/client loss, populated or
missing proof, daemon/cleanup failure, serialization/no overwrite, inventory and
sanitization. Real synthetic local subprocesses exercise timeout/output caps;
one board test uses real public readonly-ledger API. No Docker/provider calls.
Existing CI discovery picks up the tests without workflow edits.

Pending: completed CI, exact head/tree independent review, main's host/systemd
race integration, production mount UID access and resource enforcement under
the accepted OCR/seccomp profile. Kernel acceptance alone is not deployment
acceptance. No schedules/live images/service activation before these gates.
