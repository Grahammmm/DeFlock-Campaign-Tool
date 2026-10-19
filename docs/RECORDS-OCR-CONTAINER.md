# Pinned OCR container layer (candidate)

`container/flock-ocr/Dockerfile` layers a pinned OCR toolchain and the
`campaign_tool` engine wheel onto an operator-chosen Debian 12 (bookworm) base
image. It is a reviewed recipe only. This repository builds nothing, starts or
replaces no container, and holds no deployment names, image IDs, mounts or
account values; those stay in the owner's private runbook. Every build, install,
container recreation and rollback below is an owner action.

Files in `container/flock-ocr/`:

| File | Role |
| --- | --- |
| `pins.json` | Single source of reviewed pins: engine commit and manifest hash, wheel build tools, apt versions, expected tool versions |
| `Dockerfile` | Layer recipe; its apt pins must equal `pins.json` (tested) |
| `requirements.lock` | Hash-locked parser bundle (pip `--require-hashes`) |
| `build_wheel.py` | Builds the engine wheel twice from the pinned commit and checks it against `pins.json` |
| `preflight.py` | Validates inputs, the pinned commit and the build context; prints the `docker build` command |
| `verify.py` | Read-only acceptance check of a built image or running container |
| `runbook.json` | Ordered owner runbook steps and rollback paths (IDs match the runbook below) |
| `check_runbook.py` | Static ordering check of `runbook.json`; runs no lifecycle command |
| `runbook-rootless.json` | The same steps for rootless Docker (`docs/RECORDS-OCR-CONTAINER-ROOTLESS.md`); passes `check_runbook.py` |
| `rootless_swap.py` | Owner-run driver for the rootless variant: `--dry-run`, `--apply`, `--resume`, `--rollback` |
| `.dockerignore` | Deny-by-default context: only `requirements.lock` and the one wheel |

## What is pinned

| Component | Pin | Why |
| --- | --- | --- |
| tesseract-ocr / -eng / -osd | 5.3.0-2 / 1:4.1.0-2 | Per-page OCR (`docs/RECORDS-OCR.md`) |
| poppler-utils | 22.12.0-2+deb12u3 | `pdftoppm` rasterising, `pdftotext` |
| qpdf, libqpdf29 | 11.3.0-1+deb12u1 | Structural PDF checks (`qpdf --version`) |
| ocrmypdf, python3-pikepdf, ghostscript | 14.0.1+dfsg1-1, 6.0.0+dfsg-1+b2, 10.0.0~dfsg-11+deb12u8 | Operator acceptance check only; the engine no longer calls OCRmyPDF |
| python3-venv | 3.11.2-1+b1 | Isolated engine venv |
| pypdf, openpyxl, et_xmlfile | `requirements.lock` (sha256) | Parser bundle; versions must equal `requirements-runner.txt` |
| campaign_tool wheel | `pins.json` `engine.commit` 0ab680433b7fe9a1593c19ad64ae2ed27dd045f3 and `engine.manifest_sha256` 302d0f31...e59e | Built by `build_wheel.py` with setuptools==84.0.0 and wheel==0.48.0 (`requirements-build.txt`) |

Apt versions were read from an existing bookworm OCR candidate's dpkg database
and checked against packages.debian.org on 2026-10-19. poppler-utils deb12u3
comes from the security archive, so the base must keep its `bookworm-security`
apt source. When Debian publishes a newer `+deb12uN` update, the old version
leaves the mirror and the build fails closed. Update `pins.json` and the
Dockerfile together in a reviewed PR; never drop a pin. Installed pins are
`apt-mark hold`.

### Wheel provenance

The engine pin is main commit `0ab680433b7fe9a1593c19ad64ae2ed27dd045f3`
(tree `7271fe67...`, merge of PR #136).

`engine.manifest_sha256` came from running
`python3 -B -m campaign_tool.records version --json` in a clean detached
checkout of that commit. That manifest binds the sha256 of all 173 package
files and of the dependency lock. `setup.py` embeds the same manifest in the
wheel, and the installed `records version --json` re-verifies it on the target.

So five things must agree with the reviewed pin:

- the source checkout
- the wheel
- `build_wheel.py --check-wheel`
- the Dockerfile smoke test, which gets `ENGINE_MANIFEST_SHA256` from preflight
- `verify.py`

The wheel's byte sha256 depends on the build tools and zip metadata, so this
change does not record it. `build_wheel.py` produces it:

- it builds two wheels from two fresh detached checkouts, with
  `SOURCE_DATE_EPOCH` set to the commit time and `pip --no-index`;
- it stops unless both wheels are byte-identical and both embedded manifests
  hash to `engine.manifest_sha256`;
- only then does it print `WHEEL_SHA256`.

Preflight passes that value to the build, and the Dockerfile checks it with
`sha256sum -c`. Record it, together with the `--provenance-out` JSON, in the
private runbook. If the two builds differ, stop: the wheel is not reproducible
with this toolchain, and that needs review rather than a workaround.

### Compatibility assumptions

- The base is Debian 12 (the Dockerfile checks `/etc/debian_version`) on amd64.
  At build time its apt sources must reach the Debian main and security
  archives. Nothing is fetched at run time.
- The base is passed as an immutable local image ID (`sha256:<64 hex>`) or a
  digest reference. Preflight and the Dockerfile reject tags, and preflight
  never pulls. USER, ENTRYPOINT and CMD are inherited from the base unchanged,
  so a moved tag could change more than the OCR packages.
- The engine needs Python >= 3.11. The venv uses bookworm's python3 3.11.2. The
  wheel is pure Python (`py3-none-any`) with no runtime dependencies.
- `pdftotext` has no `--version` flag; use `pdftotext -v`, which prints to
  stderr.
- Known gap: pypdf 6.10.0 (the repository-wide pin) has published PDF-parsing
  denial-of-service advisories fixed in later 6.x releases. This layer keeps
  the repository pin so the container matches CI. Raising it is a separate
  reviewed change to every requirements file.

## Expected version output

`verify.py` checks that the first output line of each command starts with the
value in `pins.json` `expected_versions`:

    ocrmypdf --version     14.0.1
    pdftotext -v           pdftotext version 22.12.0
    pdftoppm -v            pdftoppm version 22.12.0
    qpdf --version         qpdf version 11.3.0
    tesseract --version    tesseract 5.3.0

It also checks that:

- each `dpkg-query` version equals `pins.json` `apt` and every pinned package
  is held;
- `ENGINE_COMMIT` equals the pin;
- `records version --json` reports `installed_wheel`, the pinned commit,
  `manifest_sha256` and dependency-lock hash, and `source_dirty: false`;
- the OCR doctor reports `"ready": true`.

## Owner runbook: quiesce, snapshot, rebuild, verify, rollback

On a rootless Docker host, use `docs/RECORDS-OCR-CONTAINER-ROOTLESS.md`
instead. It runs these same steps without sudo, in namespace-root helper
containers, through one driver script.

The ordered, machine-checked form of this runbook is
`container/flock-ocr/runbook.json`. Each step below carries its ID in square
brackets. `tests/test_flock_ocr_container.py` checks that this document lists the
same IDs in the same order and that the ordering rules hold (for example: all
mount users stopped before the snapshot and before any restore, the snapshot
gate passed before any build or recreate, data validated before any container
start, writers re-enabled last). Nothing in this repository runs these steps.

Placeholders:

- `$CONTAINER`: the target container
- `$OLD_IMAGE_ID`: its current image ID
- `$NEW_TAG`: a new tag that no other container uses
- `$BUILD_PY`: an approved interpreter that already has setuptools==84.0.0 and
  wheel==0.48.0 (`docs/RECORDS-RELEASE.md`)
- `$PRIVATE`: a private directory outside every Git checkout, on a different
  filesystem from the mounts, writable only by the owner
- `$MOUNTS`: every named volume and bind source `$CONTAINER` mounts
- `$MOUNT_USERS`: every container and host process that reads or writes any of
  `$MOUNTS`, including the Flock coordinator and the local Qwen queue

Keep the real values, receipts and manifests in `$PRIVATE` and the private
runbook. None of them belong in Git, in a fixture or in a cloud task.

The rule for the whole run: after [Q2], all mount users remain stopped through
the snapshot, build and recreate steps. [C3] is the sole exception: start only
the replacement while every other user remains stopped. For acceptance, stop
and confirm all users again at [A1], capture evidence at [A2], restart only the
replacement at [A3], and verify it at [A4]. Release other users and writers only
at [A5]. A rollback follows its own validated-data and restart order. If any
step fails, stop and take the rollback path named for that step.

### Record (read only)

1. [R1] Record the current state:

        docker inspect "$CONTAINER" > "$PRIVATE/inspect-before.json"
        docker inspect "$CONTAINER" --format '{{.Image}}'      # record as $OLD_IMAGE_ID

   From `inspect-before.json`, list every mount source. Then find every other
   user of each one: `docker ps -a --filter volume=<name>` for named volumes, the
   `Mounts` of every other container for bind sources, and the owner's private
   inventory of host jobs. Record the result as `$MOUNT_USERS`. An unknown user
   is a failure: stop here. Nothing has changed yet.

### Quiesce and snapshot gate

2. [Q1] Pause the writers with the existing owner-controlled pause/lock that the
   Flock coordinator and the local Qwen queue already honor. Do not disable,
   edit or replace timers or schedules, and do not cut any job over to a new
   runner. Wait until the coordinator reports no in-flight job. Record the pause
   time in `$PRIVATE/snapshot-receipt.json`.

3. [Q2] Stop `$CONTAINER` and every other container in `$MOUNT_USERS` with
   `docker stop` (never `rm`). Confirm that none is running and that no host
   process has a file open under any mount (for example `fuser -vm <source>`).
   Keep every user stopped through [A2]. Only the replacement may start at [C3];
   other users remain stopped until [A5] or a rollback's validated restart.

4. [S1] Take a raw, owner-preserving copy of every mount, as root, without
   opening any database. Never run `sqlite3`, Python `sqlite3` or any other
   SQLite client against a live mount, not even read-only: a SQLite connection
   to a WAL database can checkpoint it and delete its `-wal` and `-shm` files
   when the last connection closes, which changes the live bytes even though
   every writer is stopped. For each mount:

        E=container/flock-ocr/snapshot_evidence.py
        python3 "$E" manifest <source> > "$PRIVATE/<mount>.baseline.json"
        tar --numeric-owner --acls --xattrs -cpf "$PRIVATE/<mount>.tar" -C <source> .
        python3 "$E" manifest <source> > "$PRIVATE/<mount>.after-tar.json"
        python3 "$E" compare "$PRIVATE/<mount>.baseline.json" "$PRIVATE/<mount>.after-tar.json"

   The baseline manifest is the comparison baseline for every later check. It
   is a content manifest (SHA-256 of every regular file, including each
   database and its `-wal` and `-shm` files exactly as they are) and an
   owner/mode manifest (numeric `uid:gid`, so a renamed user cannot hide a
   change, plus mode and type). `manifest` only reads bytes and `lstat`s; it
   never opens SQLite. The `compare` must print `manifests: equal`.

5. [S2] Record SQLite evidence from an isolated copy, never from the live mount.
   Extract each `.tar` into an empty, disposable directory in `$PRIVATE`, then
   run the backup API and `PRAGMA integrity_check` there:

        mkdir "$PRIVATE/inspect/<mount>"
        tar --numeric-owner --acls --xattrs -xpf "$PRIVATE/<mount>.tar" -C "$PRIVATE/inspect/<mount>"
        python3 "$E" sqlite-evidence "$PRIVATE/inspect/<mount>" \
          --out "$PRIVATE/sqlite/<mount>" --live <every mount source> \
          > "$PRIVATE/<mount>.sqlite-evidence.json"

   `sqlite-evidence` refuses to run if the copy or output overlaps any `--live`
   source. It writes a `.backup` copy of each database with the WAL folded in,
   and `PRAGMA integrity_check` on each `.backup` must print `ok`. The inspect
   copy may be checkpointed by this step; that is why it is disposable and is
   never used as a restore source or comparison baseline. Record the SHA-256 of
   each `.tar` and `.backup` file. File counts and owner/mode digests alone do
   not prove byte integrity; the content manifest does. `sqlite-evidence` must
   exit 0 with `"ok": true`; it fails closed (exit 2) on a missing or
   non-directory copy, an unreadable directory or file, or a SQLite error. An
   empty `"databases"` map is valid only for a mount the inventory lists as
   database-free, and `"files_inspected"` must match the regular-file count of
   the baseline manifest.

6. [S3] Prove the backup restores and that the live data never changed. Extract
   each `.tar` into a second, empty scratch directory in `$PRIVATE` and, before
   anything opens it, compare its manifest with the baseline. Then recompute
   the live manifest and compare it with the baseline again:

        mkdir "$PRIVATE/restore-test/<mount>"
        tar --numeric-owner --acls --xattrs -xpf "$PRIVATE/<mount>.tar" -C "$PRIVATE/restore-test/<mount>"
        python3 "$E" manifest "$PRIVATE/restore-test/<mount>" > "$PRIVATE/<mount>.restored.json"
        python3 "$E" compare "$PRIVATE/<mount>.baseline.json" "$PRIVATE/<mount>.restored.json"
        python3 "$E" manifest <source> > "$PRIVATE/<mount>.live-after.json"
        python3 "$E" compare "$PRIVATE/<mount>.baseline.json" "$PRIVATE/<mount>.live-after.json"

   Both must print `manifests: equal`: the archive restores every byte, owner
   and mode, including the `-wal` and `-shm` files, and nothing (including
   [S2]) wrote to the live mounts.

7. [G1] Snapshot gate. Write `"snapshot_ok": true`, the backup references and
   their SHA-256 values, and the completion time into
   `$PRIVATE/snapshot-receipt.json` only if [S1] to [S3] all passed. No wheel
   build, image build, rename or create may start unless this receipt says
   `"snapshot_ok": true`. If the gate fails, take rollback path [RB-A].

### Build and verify (mounts stay quiescent)

8. [B1] Build the wheel from the pinned commit, in a clean clone. This step
   installs nothing and makes no network calls.

        python3 container/flock-ocr/build_wheel.py --build-python "$BUILD_PY" \
          --provenance-out "$PRIVATE/wheel-provenance.json"

   It must end with `WHEEL_SHA256=<64 hex>`. Any FAIL: rollback path [RB-A].

9. [B2] Run the preflight, then build. The build uses the network for apt and
   PyPI.

        python3 container/flock-ocr/preflight.py --base-image "$OLD_IMAGE_ID" \
          --engine-commit 0ab680433b7fe9a1593c19ad64ae2ed27dd045f3 \
          --wheel-sha256 "$WHEEL_SHA256" --tag "$NEW_TAG"

   Then run the `docker build --pull=false ...` command it prints. Without
   buildx, the legacy builder resolves `FROM <image ID>` locally. The build
   fails closed on any apt pin, Python hash, wheel hash, manifest or doctor
   mismatch. Any failure: rollback path [RB-A].

10. [V1] Check the image before it touches the container:

        python3 container/flock-ocr/verify.py --image "$NEW_TAG"
        docker run --rm --network none --entrypoint find "$NEW_TAG" /opt/campaign-tool -maxdepth 2

    `verify.py` must end with `verify: OK`. The `find` must list only
    `ENGINE_COMMIT`, `requirements.lock`, `venv/...` and `dist/<the one wheel>`.
    The context filter is a port of Docker's matcher, tested with synthetic
    files, so this `find` is the real check. These runs mount no data. Any
    failure: rollback path [RB-A].

### Recreate

11. [C1] Rename the stopped `$CONTAINER` to `$CONTAINER-pre-ocr`. Do not remove
    it. If the rename fails: rollback path [RB-B].

12. [C2] Create (do not start) `$CONTAINER` from the new image ID with every
    setting in `inspect-before.json` unchanged: volumes, binds, user,
    capabilities, security options, tmpfs, limits, network, restart policy and
    labels. Diff its `docker inspect` against the saved one. Only image, ID,
    creation time and state fields may differ. If create fails, or the diff
    shows anything else: rollback path [RB-B].

13. [C3] Start the new `$CONTAINER`. Other mount users stay stopped. If it does
    not start: rollback path [RB-C].

### Accept

14. [A1] Stop the replacement `$CONTAINER` again. Confirm that ALL users in
    `$MOUNT_USERS`, including the replacement, are stopped and no process can
    access any mount. Do not take acceptance evidence until this quiescence is
    confirmed. Any stop or confirmation failure: rollback path [RB-C].

15. [A2] While all mount users remain stopped, take the current-state baseline
    and raw `tar` archive for each mount. Extract each archive into an isolated
    directory and run `sqlite-evidence` only on that copy. Compare manifests
    and record exact archive/evidence references. The isolated SQLite integrity
    check is necessary but not sufficient: `integrity_check=ok` does not prove
    that a raw sequential DB/WAL copy contains all committed rows. A checkpoint
    between reading the DB and WAL members can produce an integrity-valid but
    stale backup. Therefore accept current-state differences only after this
    renewed all-user quiescence, archive/evidence sequence, and owner review
    recorded in the private runbook. Any mismatch, evidence failure, or
    uncertain mount user: rollback path [RB-C].

16. [A3] Restart the replacement while every other mount user remains stopped
    and the [Q1] pause/lock is held. If start fails: rollback path [RB-C].

17. [A4] Re-run runtime verification of the restarted replacement:

        python3 container/flock-ocr/verify.py --container "$CONTAINER"

    If verification fails: rollback path [RB-C]. Do not release any other mount
    user or writer before this succeeds.

18. [A5] Only after [A4] succeeds, restart the other containers in
    `$MOUNT_USERS`, then release the [Q1] pause/lock. Watch the coordinator's
    next job complete.

### Rollback paths

Every path keeps all mount users stopped until the data is validated, starts
the retained old container before anything else, and re-enables writers last.
Never start a container while a restore is in progress.

[RB-A]: failure at or before [V1]. `$CONTAINER` is unchanged and still stopped.

1. [RB-A1] Confirm that every user in `$MOUNT_USERS` is still stopped.
2. [RB-A2] Validate the data: recompute each mount's manifest with
   `snapshot_evidence.py manifest` and `compare` it with the [S1] baseline.
   If no baseline was written, [S1] never started and nothing was copied,
   opened or restored, so there is nothing to compare. If they differ, restore
   as in [RB-C4] and validate as in [RB-C5] first.
3. [RB-A3] `docker start "$CONTAINER"`; its image must equal `$OLD_IMAGE_ID`.
4. [RB-A4] Restart the other mount users, then release the [Q1] pause/lock.

[RB-B]: failure in [C1] or [C2], before the replacement has ever started.

1. [RB-B1] Confirm that every user in `$MOUNT_USERS` is stopped. If a new
   `$CONTAINER` was created, it is stopped; rename it to
   `$CONTAINER-ocr-failed`. Do not start it.
2. [RB-B2] If the rename in [C1] happened, rename `$CONTAINER-pre-ocr` back to
   `$CONTAINER`. `docker inspect "$CONTAINER" --format '{{.Image}}'` must equal
   `$OLD_IMAGE_ID`.
3. [RB-B3] Validate the data as in [RB-A2]; restore and validate first if it
   differs.
4. [RB-B4] `docker start "$CONTAINER"`.
5. [RB-B5] Restart the other mount users, then release the [Q1] pause/lock.

[RB-C]: failure in [C3] or any acceptance step [A1]-[A5], or a later rollback
on the owner's request.

1. [RB-C1] Pause writers again with the [Q1] pause/lock if it was released,
   wait for no in-flight job, then stop every user in `$MOUNT_USERS`, including
   the new `$CONTAINER`. Confirm none is running and no host process has a file
   open under a mount.
2. [RB-C2] Rename the new `$CONTAINER` to `$CONTAINER-ocr-failed`. Do not
   start it again.
3. [RB-C3] Recompute each mount's manifest and `compare` it with the [S1]
   baseline. On a later rollback the difference includes real work done since
   [A5]; the owner decides in writing whether to keep it, and an [S1]-style
   `tar` and manifest of the current state are taken before any restore.
4. [RB-C4] If the data checks failed or the owner chose to restore: for each
   mount, move the current contents aside into `$PRIVATE`, then extract the
   [S1] `.tar` with `tar --numeric-owner --acls --xattrs -xpf` into the empty
   source.
5. [RB-C5] Validate before any start, without opening the live databases: each
   mount's manifest must `compare` equal to the [S1] baseline, whose isolated
   copy already passed [S2] `sqlite-evidence`. When the owner chose to keep the
   current state instead, its manifest must equal the one taken in [RB-C3],
   and that state's archive, extracted to an isolated directory, must pass
   `sqlite-evidence` (`PRAGMA integrity_check` prints `ok`). If this fails,
   leave everything stopped and escalate to the owner. Do not start any
   container.
6. [RB-C6] Rename `$CONTAINER-pre-ocr` back to `$CONTAINER`. Its image must
   equal `$OLD_IMAGE_ID`.
7. [RB-C7] `docker start "$CONTAINER"`. Check that it stays running and that
   its own health check, if it has one, reports healthy.
8. [RB-C8] Restart the other mount users, then release the [Q1] pause/lock.

Remove `$CONTAINER-pre-ocr`, `$CONTAINER-ocr-failed`, the old image and the
`$PRIVATE` backups only after the owner signs off.

Passing `verify.py` means the tools are ready. It is not corpus, fidelity,
extraction or review acceptance, and it does not certify the base image.
