# Rootless OCR container swap (owner runbook variant)

This is the rootless-Docker form of the owner runbook in
`docs/RECORDS-OCR-CONTAINER.md`. It uses the same step IDs, kinds, order and
rollback paths. `container/flock-ocr/runbook-rootless.json` passes
`check_runbook.py`, and `tests/test_flock_ocr_rootless_swap.py` checks that the
IDs match this document and the original runbook.

The difference is how the steps run. No step uses sudo. Under rootless Docker
the daemon runs inside a user namespace. Container root maps to the host user,
and container IDs 1 and up map to that user's subuid range. Those are the IDs
that already own the mounted files. Every data step therefore runs in a
throwaway helper container (`docker run --rm`) as root of that namespace. In
there it can read and restore every file with its numeric owner, which the host
user alone cannot do for subuid-owned files.

`container/flock-ocr/rootless_swap.py` is the only driver. It runs the steps
in order and takes the named rollback path on any failure. Nothing in this
repository or its CI runs it against Docker. The tests use a fake Docker and
systemd that run the real `tar` and `snapshot_evidence.py` on a synthetic WAL
database.

Passing the driver means the tools are ready. It is not corpus, fidelity,
extraction or review acceptance.

## Inputs (arguments, never stored in Git)

- `--container NAME`: the target container.
- `--pause-unit BASE`: a systemd user unit base name. [Q1] stops `BASE.timer`,
  waits for `BASE.service` to finish any run in progress, then stops
  `BASE.service`. Repeat the flag for more units.
- `--private DIR`: an owner-private directory outside every Git checkout.
  The driver creates it if needed, sets it to `chmod 700`, and writes each run
  into a new `run-<UTC time>` subdirectory, also mode 0700. Archives,
  manifests, SQLite evidence, the saved `docker inspect` and the driver state
  go only there.
- `--new-tag NAME:TAG`: a new image tag. It must not exist yet, unless
  `--reuse-image` names the run that built it.
- `--build-python PY`: an interpreter that already has the pinned wheel build
  tools (see `docs/RECORDS-RELEASE.md`). Not needed with `--reuse-image`.
- `--reuse-image PREV_RUN_DIR`: optional. Reuse the `--new-tag` image that an
  earlier run in the same `--private` directory built and passed [V1] with,
  instead of building it again. See "Reusing a built and verified image"
  below.
- `--helper-image REF`: optional image ID or digest for the helpers. The
  default is the old image ID. It must provide `tar` with ACL/xattr support,
  `python3` and the `sqlite3` module. [R1] probes for all three.
- `--accept-malformed-sqlite RELPATH=SHA256`: optional and repeatable. It
  exempts one known, intentionally corrupt database from the SQLite
  integrity requirement only, for example a mutation-test fixture.
  `RELPATH` is relative to the mount root, with no leading `/`, `.` or `..`.
  `SHA256` pins the file's exact bytes. Without the flag the gate is
  unchanged. See "Hash-pinned malformed SQLite fixtures" below.

The driver derives mounts, mount users and image IDs at run time from
`docker inspect` and `docker ps`. It never takes them from a file in Git.

## How to run

    S=container/flock-ocr/rootless_swap.py
    python3 "$S" --dry-run --container "$CONTAINER" --pause-unit "$MAIL_UNIT" \
      --private "$PRIVATE" --new-tag "$NEW_TAG" --build-python "$BUILD_PY"
    python3 "$S" --apply   ...same arguments...

`--dry-run` runs only the read-only [R1] discovery: `docker info`,
`docker inspect`, `docker ps`, `docker image inspect`, `docker top` and
`systemctl --user show`. It prints every later command, including each helper
`docker run --rm ...`, without running it. It creates no directory and writes
no file. Read its mount list, mount users and excluded mounts before running
`--apply`.

Exit codes:

- 0: done.
- 1: a step failed and its rollback path completed.
- 2: refused before any change.
- 3: held at [A2] for owner review.
- 4: a rollback step failed. Everything that was stopped stays stopped, and the
  owner decides what happens next.

A run held at [A2] continues with
`--resume "$PRIVATE/run-..." --accept-a2 <sha256 of a2/diff.json>` after the
owner has reviewed that diff. To roll it back instead, use
`--rollback "$PRIVATE/run-..."`. `--rollback` also recovers an interrupted
run: it takes the rollback path of the step recorded in `state.json`. Only one
run at a time can hold the lock in `$PRIVATE`.

## Pruning superseded runs (separate operator action)

Pruning is never automatic on rollback. First review the dry-run listing:

    python3 "$S" prune --private "$PRIVATE"
    python3 "$S" prune --private "$PRIVATE" --reuse-image "$KEEP_FOR_NEXT_RUN"

Only after confirming the listed evidence is no longer needed, repeat with
`--apply` (and the same `--reuse-image` protections). Explicit `--dry-run` is
also accepted. `--reuse-image` is repeatable here and protects a run planned
for a future swap; existing reference targets in every run's `state.json` are
protected automatically, even if the referring run is also a prune candidate.
A persisted reference to an already absent, correctly named direct run under
`--private` is logged as `reference target already absent` and ignored. Missing
explicit `--reuse-image` targets and references outside `--private` still refuse.

The newest timestamp-named run is always kept. The newest `complete` run is also
always kept as the `latest completed swap rollback point`, even when a later run
was refused. Other older runs whose phase is `complete`, `rolled_back` or
`refused` are eligible. Held, running, escalated, interrupted rollback and unknown
phases stay untouched. Invalid state, foreign identities, symlinked runs/state,
unknown run names, or invalid reuse references refuse the whole selection rather
than guessing. The private path
must already be a real, owner-owned mode-0700 directory outside Git.

A correctly named run without `state.json` is listed as an `orphan remnant` and
kept by default. This covers early failed attempts that left only `s2/` or `s3/`
contents. To opt in, preview with both flags before repeating with `--apply`:

    python3 "$S" prune --private "$PRIVATE" --include-orphans --helper-image "$HELPER_IMAGE"

`$HELPER_IMAGE` must be an immutable image ID or digest reference. An orphan is
eligible only if it is not newest, has no reuse references, and its top level
contains only real directories named `s1`, `s2`, `s3`, `a2`, `b1` or `rollback`
(an empty run is also eligible). Unexpected entries, files or symlinks at that
level keep the orphan protected. No state or helper identity is inferred.

Apply holds the same lock as swap/resume/rollback and refuses a non-rootless
daemon. Before deleting anything it checks all helper images locally (no pull),
all containers including stopped ones for overlapping mounts, and own-user
process use. Each eligible run's recorded immutable helper image is used;
`--helper-image <image ID or digest>` can override it if the old image is gone.
The helper needs Python 3.11+ with fd-safe `shutil.rmtree`.

Each cleanup helper has the existing namespace-root user, capabilities and
network isolation. It mounts only the selected run writable at `/private` and
the recipe readonly: no live mount and no writable private parent or sibling.
It rejects nested mounts and unlinks symlinks without following them, removes
the run's contents including subuid-owned remnants, and leaves `state.json`
until last. The host then removes the empty directory and verifies absence.
It never removes an image/container or changes a service/timer. Dry-run starts
no helper and creates no file; it checks an existing lock read-only.

Deletion is irreversible and not transactional across runs. A helper failure
stops further deletion with exit 2; earlier successfully pruned runs stay gone.
If interrupted after state removal but before directory removal, the next
preview lists the empty orphan; the same explicit orphan opt-in can remove it.
Ordinary host-root files outside the daemon's namespace may still fail cleanup; do not
add privileges or bypass that denial. Exit 0 means preview/apply finished,
not that any retained evidence received semantic review. Never prune while an
operator's live attempt or evidence review still needs these directories.

## Helper containers

Every helper is
`docker run --rm --network none --user 0:0 --security-opt no-new-privileges
--cap-drop ALL` plus only CHOWN, DAC_OVERRIDE, FOWNER, FSETID and SETFCAP. Each
carries the label `deflock.flock-swap.helper=1`. It mounts:

- `/private`: this run's directory;
- `/recipe`: `container/flock-ocr`, readonly;
- `/live/mNN`: each snapshotted mount, by volume name
  (`volume-nocopy,readonly`) or by bind source (`readonly`).

Live mounts are `readonly` in every helper except the two restore commands of
a rollback (`find /live/mNN -mindepth 1 -delete`, then `tar -xpf` of the S1
archive). The tests assert this. SQLite is opened only on extracted copies
under `/private`. `snapshot_evidence.py sqlite-evidence` still refuses any copy
that overlaps a `--live` path.

Archives use `tar --numeric-owner --acls --xattrs`. Manifests record numeric
`uid:gid` as seen inside the namespace. The archive and its restore run in the
same namespace, so the same numbers map back to the same host IDs.

## Rootless-specific refusals

[R1] refuses, with nothing changed, when:

- the daemon is not rootless;
- the private path is relative, inside a Git checkout, or not a directory
  owned by this user;
- `<name>-pre-ocr` or `<name>-ocr-failed` already exists;
- the new tag already exists and `--reuse-image` is not given;
- `--reuse-image` is given and any of its checks fails (see below);
- a pause unit is not a loaded user unit (there is no sudo path);
- the target has an anonymous volume, a volume subpath, a mount type other
  than volume, bind or tmpfs, a bind source that is a regular file, or two
  mount sources that overlap;
- the helper image lacks `tar` with ACL/xattr support, `python3` or `sqlite3`.

tmpfs mounts and socket binds are listed as not snapshotted.

[S1] and [A2] also refuse a mount in which any entry is owned by uid or gid
65534. Inside the namespace, that is the overflow ID that an unmapped host ID
appears as (for example, a file created by host root). A rootless archive
cannot restore such a file to its real owner, so the run stops before any
archive is taken.

Before writing the first archive, [S1] budgets the whole run's private-storage
peak, not just the initial snapshot: seven estimated copies plus 512 MiB free.
The copies cover S1 archive + SQLite backups (2), A2 archive + SQLite backups
(2), one evidence/restore-test extract that can remain after a failure (1),
and rollback current-state preservation + verification extract (2). Each copy
estimate rounds entry sizes up to 4 KiB and adds 4 KiB per entry for tar/PAX
and filesystem metadata. [A2] remeasures the current mounts and requires five
copies plus 512 MiB of *remaining free space*: S1 is already on disk and must
not be charged twice. Insufficient space fails before that phase's archive.
Both checks write `$RUN/{s1,a2}/space-budget.json` with the components, required
bytes and available bytes. Dry-run prints the policy without scanning mounts.

This is a conservative data-storage estimate, not a reservation or a promise
against unlimited growth, unusually large xattrs, another disk user or repeated
interrupted rollbacks. Wheel/image builds also need their own space (possibly on
another filesystem); the 512 MiB margin does not bound Docker's build cache.
Do not remove S1 to make A2 fit. Preview pruning old runs instead.

The [R1] preflight (below) checks free space for the whole run up front
with a tar-aware estimate. [S1] and [A2] then require the larger of that
budget and the per-copy budget above, so neither check is weakened.

## [R1] preflight

Swap attempts 1 to 6 (2026-10-19/20) each failed at a new gate after the
snapshot had been taken: [S2] on an intentionally corrupt test fixture, [S3] on
sockets, [C2] on API defaults in the inspect diff, [B1] on a wheel an earlier
attempt left in `dist/`, twice on a checkout that was not main, and [A2] 146 MB
short of free space. Each cost a ~130 GB snapshot and a rollback. [R1] now ends
with a preflight that checks each of these before anything is stopped. Any
finding refuses the run (exit 2, nothing changed). The findings go to
`$RUN/preflight.json` and `$RUN/preflight/`.

| Earlier failure | Preflight check |
|---|---|
| off-main or stale checkout | The checkout running the driver must be on `main`, `HEAD` must equal `refs/remotes/origin/main` (run `git fetch origin` first), tracked files must be unmodified and the recipe directory must have no untracked files. The commit goes into `state.json` as `recipe_commit`. |
| [B1] leftover `dist/` | Reported, not refused. [B1]/[B2] build in `$RUN/b1/context`, so a wheel left in `container/flock-ocr/dist` cannot reach or block a build; it is left untouched and listed in `$RUN/preflight.json`. |
| [S2] malformed fixture | A read-only `snapshot_evidence.py survey` of each live mount, in a helper with the mount read-only. Every SQLite database is opened only as `file:...?mode=ro&immutable=1`: no lock, no WAL replay, no sidecar. An unpinned database that fails `integrity_check`, or an `--accept-malformed-sqlite` pin that no mount holds at its hash, refuses the run. A database being written at that moment can read as malformed; that is a false refusal before any change, and [S2] still checks the isolated copy as before. |
| overflow owners | The same survey refuses entries owned by uid or gid 65534 (formerly found only at [S1], after the stop). |
| [S3] sockets | The survey names every socket, fifo and device per mount. They are excluded from every equality check (see "Sockets and other special files"). |
| [C2] settings diff | A create-only probe: `POST /containers/create` of `<name>-swap-probe` with the saved settings and the old image, without `Binds`, `Mounts` and `VolumesFrom` (it never mounts live data), never started. Its inspect is diffed with the same rules as [C2] (name and mounts left out), then it is removed with `docker rm -v`. Any difference refuses the run. |
| [A2] free space | The survey sizes give one archive set (tar headers, pax reserve, long names, padding, record rounding) `T`, the largest extracted mount `L` and the SQLite data `D`. The preflight budget is `3T + 2D + L + 512 MiB` free (the driver takes the larger of this and the per-copy budget at [S1]/[A2]): S1 archives, S2 backups, A2 archives and backups, and the preserved current state plus its verify extract of an [RB-C] restore after [A2]. Extracts run one mount at a time and are deleted, so only the largest counts. |

Freeing space is the operator's job. Superseded run directories under
`--private` are kept or deleted under the owner's retention rule, never by the
driver.

## Rootless owner runbook

The rule for the whole run is unchanged. After [Q2], every mount user stays
stopped through the snapshot, build and recreate. [C3] starts only the
replacement. Acceptance stops and confirms all users again at [A1], captures
evidence at [A2], restarts only the replacement at [A3] and verifies it at
[A4]. Other users and the pause timers are released only at [A5].

"Confirm quiescence" means all three of these hold:

- no container whose mounts share a volume name or an overlapping source path
  with a snapshotted mount is running;
- each pause timer and service is inactive;
- no process of this user has a working directory or open file under a mount
  source.

Processes of other host users are not visible without privilege. The
container check is the primary control.

### Record (read only)

1. [R1] Check the rootless daemon, the private directory, the names, the tag
   and the pause units. Save `docker inspect` of the target as
   `inspect-before.json`, record its ID and image ID, and derive the mount
   list from its `Mounts`. List the mount users: every container whose mounts
   overlap one of those, in any state. Record which users were running and
   whether each pause timer was active. Save `docker top` of the target (the
   app-server inside it). Probe the helper image. Run the preflight (see
   "[R1] preflight"); its only change is the create-only probe, which is
   removed before [R1] ends. Nothing else has changed yet.

### Quiesce and snapshot gate

2. [Q1] `systemctl --user stop BASE.timer`. Wait up to `--mail-wait` seconds
   for `BASE.service` to finish a run in progress. A running service is never
   interrupted: if it is still active, the step fails. Then
   `systemctl --user stop BASE.service` and confirm both are inactive. Timers
   are stopped, never disabled or edited.

3. [Q2] `docker stop` (never `docker rm`) every running mount user, including
   the target and therefore the app-server inside it. Confirm quiescence.

4. [S1] For each mount, in helpers with the live mount read-only: write a
   baseline manifest, take a `--numeric-owner` tar into
   `$RUN/s1/mNN.tar`, take a second manifest and compare it with the baseline.
   No database is opened. Special files (sockets, fifos, devices) are
   recorded in the manifest's `non_archivable` map and named in the log (see
   "Sockets and other special files" below).

5. [S2] Extract each S1 archive into `$RUN/s2/inspect/mNN` and run
   `sqlite-evidence` on that copy only. It must exit 0 with `"ok": true`, and
   `files_inspected` must equal the baseline's regular-file count. The driver
   re-hashes every `.backup` on the host, then deletes the disposable copy.
   A failing run tries every database first and names each failure on its own
   `FAIL` line, saved in `$RUN/s2/mNN.sqlite-evidence-failure.json`. A
   database pinned with `--accept-malformed-sqlite` is hash-checked instead
   of opened (see below).

6. [S3] Extract each S1 archive into an empty `$RUN/s3/restore-test/mNN`.
   Its manifest must equal the baseline, and a fresh live manifest must still
   equal it too. Every regular file, directory and symlink is compared;
   special files are excluded from these checks and listed by path.

7. [G1] Write `snapshot-receipt.json` with `"snapshot_ok": true`, the
   archive and backup SHA-256 values, any hash-pinned malformed databases
   that were exempted (`sqlite_accepted_malformed_sha256`), the special files
   per mount (`non_archivable`, path to kind) and the completion
   time, only if [S1] to [S3] all passed. Every build, rename, create and
   start step re-reads this receipt and refuses without it.

### Build and verify (mounts stay quiescent)

8. [B1] Create `$RUN/b1/context` with just the recipe's `.dockerignore` and
   `requirements.lock`. Run `build_wheel.py --build-python "$BUILD_PY" --dist
   "$RUN/b1/context/dist" --provenance-out "$RUN/wheel-provenance.json"`.
   The driver takes `WHEEL_SHA256` from its last line. Every run owns its output,
   including a failed or interrupted build; no new wheel goes into the shared
   `container/flock-ocr/dist`, and any legacy contents there are left untouched.
   The next run uses a fresh context and cannot be blocked by an old wheel.
   With `--reuse-image` it creates no context and takes the wheel SHA-256
   recorded by the earlier run.

9. [B2] `preflight.py --context "$RUN/b1/context"` against the old image ID.
   Context validation still requires exactly the lock plus one hashed wheel.
   The Dockerfile stays in the recipe; only the isolated context is sent. Then the
   `docker build --pull=false` command that `preflight.build_command` produces.
   Then resolve the new image ID. With `--reuse-image` it builds nothing: the
   tag must still resolve to the image ID checked at [R1].

10. [V1] `verify.py --image <new image ID>` must report `verify: OK`. The
    `/opt/campaign-tool` listing must hold only `ENGINE_COMMIT`,
    `requirements.lock`, `venv/...` and `dist/<the pinned wheel>`. A reused
    image is verified again here, against the current `pins.json`.

### Recreate

11. [C1] `docker rename` the stopped target to `<name>-pre-ocr`. Its ID must
    equal the recorded one.

12. [C2] Create the replacement without starting it: Engine API
    `POST /containers/create` over the rootless socket. The body is the saved
    `Config`, `HostConfig` and network endpoints, with only `Image` changed.
    Diff its `docker inspect` against `inspect-before.json`. Only image, ID,
    creation, state, per-container paths and the image's manifest descriptor
    may differ, after the API defaults are applied (see "[C2] settings diff"
    below). Env and labels may gain only keys that the new image itself adds.

13. [C3] `docker start` the replacement only. It must stay running and, if it
    has a health check, report healthy.

### Accept

14. [A1] `docker stop` the replacement. Confirm quiescence for ALL users,
    including the replacement.

15. [A2] While all users stay stopped, repeat [S1] to [S3] for the current
    state into `$RUN/a2`: archive, isolated `sqlite-evidence` and restore test.
    Then compare each current baseline with the S1 baseline. If anything
    differs, the run holds (exit 3) with everything stopped and writes
    `a2/diff.json`. `integrity_check=ok` alone does not prove that the
    replacement's writes are complete or wanted, so the owner reviews the diff
    and accepts it by its SHA-256. On `--resume`, the driver confirms
    quiescence again and checks that the live state still equals the reviewed
    [A2] baseline.

16. [A3] `docker start` the replacement while every other user stays stopped.

17. [A4] `verify.py --container <name>` must report `verify: OK`.

18. [A5] `docker start` the other mount users that were running at [R1].
    Then `systemctl --user start BASE.timer` for each timer that was active at
    [R1]. The service is never started directly. Watch the next coordinator
    and mail job complete.

### Rollback paths

Each path keeps every mount user stopped until the data is validated. It
starts the retained old container before anything else and releases writers
last. A rollback never starts the replacement and never uses `docker rm`.

[RB-A]: failure at or before [V1].

1. [RB-A1] If [S1] never started, nothing was copied or opened, and the path
   just returns everything to its [R1] state. Otherwise confirm quiescence.
2. [RB-A2] Compare each mount that has an S1 baseline with that baseline. If
   a mount differs and [G1] passed, restore it as in [RB-C4]. If a mount
   differs before [G1] passed, no verified archive exists: escalate and leave
   everything stopped. Live data changed while every user was stopped, so
   something unknown is writing.
3. [RB-A3] `docker start` the target if it was running at [R1]. Its ID and
   image must equal the recorded ones.
4. [RB-A4] Start the other users that were running at [R1], then the pause
   timers that were active at [R1].

[RB-B]: failure in [C1] or [C2], before the replacement ever started.

1. [RB-B1] Rename a created replacement to `<name>-ocr-failed`, then confirm
   quiescence.
2. [RB-B2] Rename `<name>-pre-ocr` back. Its ID must equal the recorded one.
3. [RB-B3] Validate the data as in [RB-A2].
4. [RB-B4] `docker start` the old container.
5. [RB-B5] Release as in [RB-A4].

[RB-C]: failure in [C3] or [A1]-[A5], or `--rollback` of a run held at [A2].

1. [RB-C1] Stop the pause timers again (waiting for a service run to finish),
   `docker stop` every running mount user including the replacement, and
   confirm quiescence.
2. [RB-C2] Rename the replacement to `<name>-ocr-failed`. It is not started
   again.
3. [RB-C3] Compare each live mount with its S1 baseline.
4. [RB-C4] For each mount that differs, first check the S1 archive hash
   against the receipt, then validate every earlier preservation of that
   mount against its receipt (stop if any differs). Unless one of them already
   holds exactly the live state, archive the current state into a new attempt
   directory `$RUN/rollback/mNN/attempt-NNN/` (`current.json`, `current.tar`).
   Extract it to an isolated directory and check it against the current
   manifest. Write `receipt.json` (hashes and the `non_archivable` special
   files of the captured state) last and make the three files
   read-only. Only then clear the mount and extract the S1 archive into it.
   Clearing removes the mount's sockets too; the S1 archive does not bring
   them back, and the log names each one.
   Attempt directories are created exclusively and never rewritten. A retried
   or interrupted rollback adds `attempt-002`, and so on; it never truncates an
   earlier capture, so the first one keeps the replacement's work even if a
   restore fails part-way. An attempt without a receipt was interrupted before
   verification; it is kept untouched and never used. `state.json` records
   each receipt and which attempt covered the clear.
5. [RB-C5] Each mount must equal its S1 baseline before any start. Otherwise
   escalate.
6. [RB-C6] Rename `<name>-pre-ocr` back. Its ID must equal the recorded one.
7. [RB-C7] `docker start` the old container. It must stay running and
   healthy.
8. [RB-C8] Release as in [RB-A4].

A rollback requested after a completed run (real work done since [A5]) is
outside the driver. Follow the manual [RB-C] in `docs/RECORDS-OCR-CONTAINER.md`,
where the owner decides in writing whether to keep that work.

Remove `<name>-pre-ocr`, `<name>-ocr-failed`, the old image and the run
directory only after the owner signs off. Files under `$RUN` that came from a
restore test or an inspect copy can carry subuid owners. Remove them with a
helper (`rm -rf` as namespace root), not as the host user.

## Hash-pinned malformed SQLite fixtures

Some review trees keep deliberately corrupted databases as regression
fixtures (for example `.../db-mutation/ledger.sqlite` next to its uncorrupted
siblings). [S2] would correctly reject them as `database disk image is
malformed`. Do not delete or repair such a file to get past the gate. First
confirm read-only that it is an intentional fixture and not damage. Then
rerun with one `--accept-malformed-sqlite RELPATH=SHA256` per file, using the
path and `sha256` recorded in the failed run's `$RUN/s1/mNN.baseline.json`.

An exempted file loses only the SQLite requirement. It is still:

- archived in `$RUN/s1/mNN.tar` and `$RUN/a2/mNN.tar`;
- hash-matched: before [S2] runs, the pin must equal the file's `sha256` in
  that mount's baseline manifest, or the step fails. `sqlite-evidence`
  re-hashes the extracted copy and fails on any other value;
- restore-tested: [S3] and [A2] compare full manifests, its `sha256`
  included.

It is never opened with SQLite and gets no `.backup`. The report marks it
`"accepted_malformed": true`. A pin for a path that is missing, is not a
SQLite file, has another hash, or matches no snapshotted mount fails [S2].
So does an unpinned malformed database. Pins are fixed at `--apply`, kept in
`state.json` and reused by [A2]. `--resume` and `--rollback` do not take
them.

## Sockets and other special files

A mount can hold Unix sockets left by a stopped process (for example an
app-server control socket or a lifecycle socket under a rehearsal's `tmp/`).
GNU tar skips sockets ("socket ignored", exit 0), so a fresh restore never
has them. Before this rule, [S3] failed with "fresh restore vs baseline:
N difference(s)" for every such socket.

The manifest records every entry that is not a regular file, directory or
symlink as `"type": "other"` with a `"special"` kind (`socket`, `fifo`,
`char_device`, `block_device` or `unknown`), and lists them all in its
top-level `non_archivable` map (path to kind; `{}` when there are none). The
driver then:

- names each one by path in the [S1] and [A2] log for its mount, and writes
  them per mount into `snapshot-receipt.json` (`non_archivable`) and into
  each rollback attempt's `receipt.json`;
- leaves them out of every equality check ([S1] after-tar, [S3] restore
  and live, [A2] against S1, `--resume`, [RB-A2]/[RB-C3]-[RB-C5]) and logs
  the excluded paths each time. They are never silently dropped;
- still fails on any difference in a regular file, directory or symlink,
  including a path that is a special file on one side and a regular file,
  directory or symlink on the other.

A socket that appears or disappears between [S1] and [A2] therefore does not
hold the run; it is listed in the [A2] log. fifos are archived and restored
by tar but are excluded the same way. A device node would be archived but
the helper cannot recreate it (no `CAP_MKNOD`), so its extract fails and the
step stops safely. `snapshot_evidence.py compare` (the CLI) stays strict and
still reports special files.

## [C2] settings diff

[C2] creates the replacement from the saved inspect and fails on any setting
that comes back different. Swap attempt 3 (2026-10-19) stopped there safely,
with RB-B clean, on two benign differences:

- `ImageManifestDescriptor`: the digest and size of the image's manifest.
  The daemon derives it from the image, and the swap changes the image on
  purpose.
- `HostConfig.OomKillDisable`: `null` in the saved inspect (stored by an
  older daemon), `false` after the create.

Grahammmm/DeFlock-Campaign-Tool#153 fixed exactly those two:
`ImageManifestDescriptor` joins the ignored top-level fields (like `Image`
and `Config.Image`), and `HOSTCONFIG_NULL_IS_FALSE` treats a null or absent
`OomKillDisable` as `false`. The top-level `Platform` is still compared. Env
and labels keep the existing rule: only keys that the new image adds.

To find what else the create endpoint rewrites, a create-only probe ran
against rootless Docker 29.6.2 (API 1.55): throwaway containers from the old
image, no mounts, `NetworkMode=none`, never started, removed right after
their inspect. It sent the saved `HostConfig` and variants with fields set to
null, empty or zero. The diff maps both sides through the pairs it found
(`HOSTCONFIG_NULL_IS_FALSE` plus `HOSTCONFIG_EQUIVALENT`) before comparing.
Each pair is one setting written two ways; the create body is not changed.

| Field | Treated as equal |
|---|---|
| `OomKillDisable` | `null` = absent = `false` (#153) |
| `MemorySwappiness` | `-1` = `null` |
| `PidsLimit` | `0` = `-1` = `null` (unlimited) |
| `ConsoleSize` | `null` = `[0, 0]` |
| `Mounts`, `Links` | `[]` = `null` = absent |
| `Tmpfs`, `Sysctls`, `StorageOpt`, `Annotations` | `{}` = `null` = absent |

[C2] logs each pair it applied as `API default, treated as equal: ...`.

Everything else is compared exactly as stored. Some values are filled in by
the daemon when unset: `MaskedPaths` and `ReadonlyPaths` (`null` means the
default masks; `[]` means none), `IpcMode`, `CgroupnsMode`, `Runtime`,
`ShmSize`, `LogConfig` and `RestartPolicy`. The saved inspect always carries
their explicit values, and the probe returned those values unchanged. So a
difference in one of them is a real change, or a changed daemon default, and
still fails. The same holds for mounts, binds, env, networks and aliases,
user, entrypoint and command, volumes, capabilities, security options,
read-only root, resource limits other than the table above, and restart
policy. The tests cover each case
(`tests/test_flock_ocr_rootless_swap.py`, `PROBED_EQUAL` and
`PROBED_STRICT`).

## Reusing a built and verified image

A run that fails after [V1] has already built and verified the image. Take
`--reuse-image "$PRIVATE/run-..."` with the same `--new-tag` and the next
attempt does not build it again. [R1] accepts the existing tag only if all of
these hold, and otherwise refuses with nothing changed:

- the directory is a run directly under `--private`, its `state.json`
  belongs to it, and it is not the current run;
- that run built the same `--new-tag` and recorded `V1` as passed, a valid
  image ID and the wheel SHA-256;
- it was built on the target's current image (its recorded `old_image`);
- the tag still resolves to the recorded image ID;
- the image's layers extend the target image's layers.

[B1] and [B2] then build nothing. [B2] resolves the tag again and fails
(RB-A) if it has moved. [V1] always runs again on the image ID, against the
current `pins.json`. Every snapshot step runs as usual. `state.json` and the
[G1] `snapshot-receipt.json` record the reuse (`reuse` / `reused_image`: run
directory, image ID, wheel SHA-256).

Before the next attempt the owner deals with what the failed run left.
RB-B renames a created, never-started replacement to `<name>-ocr-failed`,
and [R1] refuses while that name exists. After the owner signs off, remove
it (it was never started and holds no data), then run `--dry-run` with
`--reuse-image` and read its `reusing ...` line before `--apply`.

Alternatively, choose a new `--new-tag` and let the run build again.

## Known limits

- File binds are refused, not snapshotted. Socket binds and tmpfs are not data
  and are listed as excluded. Sockets inside a mount are recorded and listed
  but not archived or compared (see above).
- Open-file detection sees only this user's processes.
- The private directory is usually on the same filesystem as the Docker data
  root. The archives protect against a bad swap, not against disk loss.
- The fake in the tests covers every step and rollback path. `--rehearse`
  (below) runs the real sequence against real rootless Docker, but on a
  throwaway target with synthetic data and stand-in engine build steps. Run
  `--dry-run` first and review its output before `--apply`.

## Rehearsal (`--rehearse`)

```
python3 container/flock-ocr/rootless_swap.py --rehearse --private "$SCRATCH" \
  [--rehearse-image python:3.12-slim-bookworm] [--rehearse-mib 1024] \
  [--rehearse-fault unpinned-fixture] [--rehearse-keep]
```

`--private` is the rehearsal's own scratch root, outside Git. The rehearsal
never takes a container, unit, tag, pin or reuse argument; it makes its own.
Each invocation has its own owner identity `<stamp>`, the UTC second plus 32
random bits (`20261020t160000z-0a1b2c3d`), so two rehearsals started in the
same second on one daemon never share a name, label, tag or directory:

1. Its own private directory `flock-swap-rehearsal-<stamp>-private` (the
   driver's `--private`, holding `$RUN`), a volume
   `flock-swap-rehearsal-<stamp>-data` and a bind directory
   `flock-swap-rehearsal-<stamp>-bind` under the scratch root. Each is created
   exclusively: an existing directory or volume of that name is refused, never
   adopted, and the new volume must carry this invocation's label (`docker
   volume create` silently reuses an existing name). The volume and bind are
   filled by `rehearsal_fixture.py` in a helper: ~1 GiB of bulk
   files (`--rehearse-mib`), a WAL database with its sidecars, a hash-pinned
   corrupt test fixture, a left-behind Unix socket, a symlink, 0600/0640 modes
   and a file owned by namespace uid 1000.
2. A throwaway target `flock-swap-rehearsal-<stamp>` (labelled
   `deflock.flock-swap.rehearsal=<stamp>`) on the rehearsal image: the volume,
   the bind, a tmpfs, `--init`, a restart policy, a health check, no network.
3. The real driver, `--apply` code, from the [R1] preflight through [A5]. Only
   four steps are stand-ins, because they need the pinned engine build: [B1]
   writes a synthetic wheel into `$RUN/b1/context/dist`, [B2] layers it onto the
   target's image with `docker build`, [V1] checks the image's layers and the
   wheel hash in the image, [A4] checks the wheel hash in the running
   replacement. The pause unit `<name>-mail` is simulated in memory; no real
   systemd unit is touched. The checkout check reports instead of refusing
   (CI rehearses pull-request heads).
4. A check that the replacement runs the new image under the name, the old
   container is kept stopped as `<name>-pre-ocr`, the timer was released and
   the run completed at [A5].
5. Cleanup, of only what this invocation proved it created: once its volume
   exists, every container carrying its own label value (`docker rm -f -v`);
   the tag only if the image's `deflock.flock-swap.rehearsal.image` label names
   this target (built by its [B2]); its volume; then its bind and private
   directories, removed as namespace root by a helper confined to the scratch
   root. A setup failure on a name collision removes nothing it did not
   create. `--rehearse-keep` keeps the data and private directory.

`--rehearse-fault unpinned-fixture` withholds the fixture's pin. The rehearsal
passes only if the driver refuses inside [R1] (exit 2, no step done, the
simulated pause unit untouched, the target still running unchanged) and the
preflight survey's own findings name exactly the fixture's withheld paths and
nothing else. Any other refusal (not rootless, target missing, a leftover
`dist/`, an unrelated survey finding) fails the fault rehearsal.

The workflow `.github/workflows/flock-swap-rehearsal.yml` runs both on rootless
Docker in CI. Never run `--rehearse` on a host while a live swap is running on
the same daemon: a rehearsal container that disappears between a live run's
`docker ps` and its `docker inspect` would fail that run's mount-user check.
