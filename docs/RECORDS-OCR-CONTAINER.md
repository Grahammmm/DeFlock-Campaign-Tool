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

## Owner runbook: snapshot, rebuild, verify, rollback

Placeholders:

- `$CONTAINER`: the target container
- `$OLD_IMAGE_ID`: its current image ID
- `$NEW_TAG`: a new tag that no other container uses
- `$BUILD_PY`: an approved interpreter that already has setuptools==84.0.0 and
  wheel==0.48.0 (`docs/RECORDS-RELEASE.md`)
- `$PRIVATE`: a private directory outside every Git checkout

Keep the real values in the private runbook.

1. Snapshot, before anything else:

        docker inspect "$CONTAINER" > "$PRIVATE/inspect-before.json"
        docker inspect "$CONTAINER" --format '{{.Image}}'      # record as $OLD_IMAGE_ID

   Back up every named volume and bind directory the container mounts. For each
   one, record the file count and an owner/mode digest, for example
   `find <dir> | wc -l` and
   `find <dir> -printf '%u:%g %m %p\n' | sort | sha256sum`.
   Never retag or overwrite a tag that another container uses.

2. Build the wheel from the pinned commit, in a clean clone. This step installs
   nothing and makes no network calls.

        python3 container/flock-ocr/build_wheel.py --build-python "$BUILD_PY" \
          --provenance-out "$PRIVATE/wheel-provenance.json"

   It must end with `WHEEL_SHA256=<64 hex>`. Any FAIL means stop.

3. Run the preflight, then build. The build uses the network for apt and PyPI.

        python3 container/flock-ocr/preflight.py --base-image "$OLD_IMAGE_ID" \
          --engine-commit 0ab680433b7fe9a1593c19ad64ae2ed27dd045f3 \
          --wheel-sha256 "$WHEEL_SHA256" --tag "$NEW_TAG"

   Then run the `docker build --pull=false ...` command it prints. Without
   buildx, the legacy builder resolves `FROM <image ID>` locally. The build
   fails closed on any apt pin, Python hash, wheel hash, manifest or doctor
   mismatch.

4. Check the image before it touches the container:

        python3 container/flock-ocr/verify.py --image "$NEW_TAG"
        docker run --rm --entrypoint find "$NEW_TAG" /opt/campaign-tool -maxdepth 2

   `verify.py` must end with `verify: OK`. The `find` must list only
   `ENGINE_COMMIT`, `requirements.lock`, `venv/...` and `dist/<the one wheel>`.
   The context filter is a port of Docker's matcher, tested with synthetic
   files, so this `find` is the real check.

5. Recreate the container:

   - Stop it and rename it to `$CONTAINER-pre-ocr`. Do not remove it.
   - Create `$CONTAINER` from the new image ID with every setting in
     `inspect-before.json` unchanged: volumes, binds, user, capabilities,
     security options, tmpfs, limits, network, restart policy and labels.
   - Diff the new `docker inspect` against the saved one. Only image, ID,
     creation time and state fields may differ.

6. Accept:

        python3 container/flock-ocr/verify.py --container "$CONTAINER"

   Then recompute the step-1 counts and owner/mode digests for every volume and
   bind. They must match the snapshot.

7. Roll back if step 5 or 6 fails, or later on request:

        docker stop "$CONTAINER" && docker rename "$CONTAINER" "$CONTAINER-ocr-failed"
        docker rename "$CONTAINER-pre-ocr" "$CONTAINER" && docker start "$CONTAINER"
        docker inspect "$CONTAINER" --format '{{.Image}}'      # must equal $OLD_IMAGE_ID

   If the data checks failed, restore the volumes from the step-1 backup.
   Remove `$CONTAINER-pre-ocr` and the old image only after the owner signs off.

Passing `verify.py` means the tools are ready. It is not corpus, fidelity,
extraction or review acceptance, and it does not certify the base image.
