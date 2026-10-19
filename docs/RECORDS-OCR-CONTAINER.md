# Pinned OCR container layer (candidate)

`container/flock-ocr/Dockerfile` layers a pinned OCR toolchain and the
`campaign_tool` engine wheel onto an operator-chosen Debian 12 (bookworm) base
image. It is a reviewed recipe only. This repository builds nothing, starts or
replaces no container, and holds no deployment names, image IDs, mounts or
account values; those stay in the operator's private runbook.

## What is pinned

| Component | Pin | Why |
| --- | --- | --- |
| tesseract-ocr / -eng / -osd | 5.3.0-2 / 1:4.1.0-2 | Per-page OCR (`docs/RECORDS-OCR.md`) |
| poppler-utils | 22.12.0-2+deb12u3 | `pdftoppm` rasterising, `pdftotext` |
| qpdf, libqpdf29 | 11.3.0-1+deb12u1 | Structural PDF checks (`qpdf --version`) |
| ocrmypdf, python3-pikepdf, ghostscript | 14.0.1+dfsg1-1, 6.0.0+dfsg-1+b2, 10.0.0~dfsg-11+deb12u8 | Operator acceptance check only; the engine no longer calls OCRmyPDF |
| python3-venv | 3.11.2-1+b1 | Isolated engine venv |
| pypdf, openpyxl, et_xmlfile | `container/flock-ocr/requirements.lock` (sha256) | Parser bundle; versions must equal `requirements-runner.txt` |
| campaign_tool wheel | `WHEEL_SHA256` + `ENGINE_COMMIT` build args | Built from a clean accepted commit per `docs/RECORDS-RELEASE.md` |

Apt versions were read from an existing bookworm OCR candidate's dpkg database
and checked against packages.debian.org on 2026-10-19. When Debian publishes a
newer `+deb12uN` security update the old version leaves the mirror and the build
fails closed; update the pin in a reviewed PR. Installed pins are `apt-mark hold`.

Known gap: pypdf 6.10.0 (the repository-wide pin) has published PDF-parsing
denial-of-service advisories fixed in later 6.x releases. This layer keeps the
repository pin so the container matches CI; raising it is a separate reviewed
change to every requirements file.

## Build (operator action)

1. Snapshot the target container's writable volumes and owner-only files first.
   Replacing a container keeps named volumes only if the same volumes are reused.
2. From a clean checkout of the accepted commit, build the wheel
   (`docs/RECORDS-RELEASE.md`) into `container/flock-ocr/dist/` and record its
   sha256. Exactly one `deflock_campaign_tool-*.whl` may be present.
3. Build a **new** tag; never overwrite a tag that other containers use:

        docker build -f container/flock-ocr/Dockerfile \
          --build-arg BASE_IMAGE="$BASE_IMAGE" \
          --build-arg ENGINE_COMMIT="$ENGINE_COMMIT" \
          --build-arg WHEEL_SHA256="$WHEEL_SHA256" \
          -t "$NEW_TAG" container/flock-ocr

   The build fails unless the base is Debian 12, every apt pin resolves, every
   Python hash matches, the wheel hash matches, `records version --json` reports
   an installed clean wheel for `ENGINE_COMMIT`, and the OCR doctor is ready.
4. Recreate the container from the new image ID with its existing run settings
   unchanged. USER, ENTRYPOINT and CMD are inherited from the base image.

## Acceptance

    docker exec "$CONTAINER" ocrmypdf --version
    docker exec "$CONTAINER" pdftotext -v
    docker exec "$CONTAINER" qpdf --version
    docker exec "$CONTAINER" /opt/campaign-tool/venv/bin/python -I -m campaign_tool.records.extract.ocr --doctor

Passing these is tool readiness. It is not corpus, fidelity, extraction or
review acceptance, and it does not certify the base image.

Rollback: recreate the container from the previous image ID with the same
settings and volumes.
