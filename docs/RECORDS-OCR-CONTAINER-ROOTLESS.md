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
- `--new-tag NAME:TAG`: a new image tag. It must not exist yet.
- `--build-python PY`: an interpreter that already has the pinned wheel build
  tools (see `docs/RECORDS-RELEASE.md`).
- `--helper-image REF`: optional image ID or digest for the helpers. The
  default is the old image ID. It must provide `tar` with ACL/xattr support,
  `python3` and the `sqlite3` module. [R1] probes for all three.

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
- the new tag already exists;
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

The disk-space check needs three times the mount data plus 512 MiB free in the
private directory.

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
   app-server inside it). Probe the helper image. Nothing has changed yet.

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
   No database is opened.

5. [S2] Extract each S1 archive into `$RUN/s2/inspect/mNN` and run
   `sqlite-evidence` on that copy only. It must exit 0 with `"ok": true`, and
   `files_inspected` must equal the baseline's regular-file count. The driver
   re-hashes every `.backup` on the host, then deletes the disposable copy.

6. [S3] Extract each S1 archive into an empty `$RUN/s3/restore-test/mNN`.
   Its manifest must equal the baseline, and a fresh live manifest must still
   equal it too.

7. [G1] Write `snapshot-receipt.json` with `"snapshot_ok": true`, the
   archive and backup SHA-256 values and the completion time, only if [S1] to
   [S3] all passed. Every build, rename, create and start step re-reads this
   receipt and refuses without it.

### Build and verify (mounts stay quiescent)

8. [B1] `build_wheel.py --build-python "$BUILD_PY" --provenance-out
   "$RUN/wheel-provenance.json"`. The driver takes `WHEEL_SHA256` from its
   last line.

9. [B2] `preflight.py` against the old image ID. Then the
   `docker build --pull=false` command that `preflight.build_command` produces.
   Then resolve the new image ID.

10. [V1] `verify.py --image <new image ID>` must report `verify: OK`. The
    `/opt/campaign-tool` listing must hold only `ENGINE_COMMIT`,
    `requirements.lock`, `venv/...` and `dist/<the pinned wheel>`.

### Recreate

11. [C1] `docker rename` the stopped target to `<name>-pre-ocr`. Its ID must
    equal the recorded one.

12. [C2] Create the replacement without starting it: Engine API
    `POST /containers/create` over the rootless socket. The body is the saved
    `Config`, `HostConfig` and network endpoints, with only `Image` changed.
    Diff its `docker inspect` against `inspect-before.json`. Only image, ID,
    creation, state and per-container paths may differ. Env and labels may gain
    only keys that the new image itself adds.

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
   against the receipt. Archive the current state into
   `$RUN/rollback/mNN.current.tar`, extract it to an isolated directory and
   check that it matches the current manifest. Only then clear the mount and
   extract the S1 archive into it. The current state is kept, not discarded.
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

## Known limits

- File binds are refused, not snapshotted. Socket binds and tmpfs are not data
  and are listed as excluded.
- Open-file detection sees only this user's processes.
- The private directory is usually on the same filesystem as the Docker data
  root. The archives protect against a bad swap, not against disk loss.
- The driver has been exercised only against the fake in the tests. Run
  `--dry-run` first and review its output before `--apply`.
