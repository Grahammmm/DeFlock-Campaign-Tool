# Pinned OCR toolchain + campaign_tool wheel layered onto an operator-chosen
# Debian 12 (bookworm) base image. Candidate only: building or deploying it is an
# operator action (see docs/RECORDS-OCR-CONTAINER.md). Nothing is fetched at run
# time and the image carries no credentials, campaign data or settings.
#
# The build context is this directory. The engine wheel is built beforehand from
# a clean checkout of the accepted commit (docs/RECORDS-RELEASE.md) and placed in
# ./dist/ (gitignored). Its sha256 and commit are required build arguments:
#
#   docker build -f container/flock-ocr/Dockerfile \
#     --build-arg BASE_IMAGE=<exact local bookworm image> \
#     --build-arg ENGINE_COMMIT=<40-hex commit> \
#     --build-arg WHEEL_SHA256=<64-hex wheel sha256> \
#     -t <new tag> container/flock-ocr
#
# Apt pins are exact Debian 12 versions. If the mirror has moved on (for example
# a new +deb12uN security update), the build fails closed; update the pin in a
# reviewed PR rather than dropping it.

ARG BASE_IMAGE
FROM ${BASE_IMAGE}
ARG ENGINE_COMMIT
ARG WHEEL_SHA256
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONDONTWRITEBYTECODE=1

RUN printf '%s' "${ENGINE_COMMIT}" | grep -Eqx '[0-9a-f]{40}' \
 && printf '%s' "${WHEEL_SHA256}" | grep -Eqx '[0-9a-f]{64}' \
 && grep -Eqx '12\.[0-9]+' /etc/debian_version

# Exact Debian 12 versions (resolved 2026-10-19 from the 2026-09-30 OCR candidate
# image's dpkg database and packages.debian.org). qpdf is the CLI; libqpdf29 is
# pinned to the same source version. ocrmypdf pulls pikepdf and Ghostscript.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      tesseract-ocr=5.3.0-2 \
      tesseract-ocr-eng=1:4.1.0-2 \
      tesseract-ocr-osd=1:4.1.0-2 \
      poppler-utils=22.12.0-2+deb12u3 \
      qpdf=11.3.0-1+deb12u1 \
      libqpdf29=11.3.0-1+deb12u1 \
      ocrmypdf=14.0.1+dfsg1-1 \
      python3-pikepdf=6.0.0+dfsg-1+b2 \
      ghostscript=10.0.0~dfsg-11+deb12u8 \
      python3-venv=3.11.2-1+b1 \
 && apt-mark hold tesseract-ocr tesseract-ocr-eng tesseract-ocr-osd poppler-utils \
      qpdf libqpdf29 ocrmypdf python3-pikepdf ghostscript python3-venv \
 && apt-get clean && rm -rf /var/lib/apt/lists/*

# Engine in its own root-owned venv, read-only to the runtime user. The wheel has
# no dependencies; pypdf/openpyxl are the hash-locked parser bundle.
COPY requirements.lock /opt/campaign-tool/requirements.lock
COPY dist/ /opt/campaign-tool/dist/
RUN cd /opt/campaign-tool/dist \
 && test "$(ls deflock_campaign_tool-*-py3-none-any.whl | wc -l)" -eq 1 \
 && printf '%s  %s\n' "${WHEEL_SHA256}" deflock_campaign_tool-*-py3-none-any.whl | sha256sum -c - \
 && python3 -m venv /opt/campaign-tool/venv \
 && /opt/campaign-tool/venv/bin/pip install --require-hashes --only-binary=:all: \
      -r /opt/campaign-tool/requirements.lock \
 && /opt/campaign-tool/venv/bin/pip install --no-deps --no-index \
      /opt/campaign-tool/dist/deflock_campaign_tool-*-py3-none-any.whl \
 && printf '%s\n' "${ENGINE_COMMIT}" > /opt/campaign-tool/ENGINE_COMMIT \
 && chmod -R a+rX,go-w /opt/campaign-tool

# Build-time smoke: the acceptance commands, the installed-wheel manifest bound to
# ENGINE_COMMIT, and the engine's own OCR doctor. Dependency readiness only; not
# corpus, fidelity or review acceptance.
RUN ocrmypdf --version \
 && pdftotext -v \
 && qpdf --version \
 && tesseract --version \
 && tesseract --list-langs | grep -qx eng \
 && cd / \
 && /opt/campaign-tool/venv/bin/records version --json > /tmp/engine-version.json \
 && /opt/campaign-tool/venv/bin/python -I -c 'import json,sys; r=json.load(open("/tmp/engine-version.json")); sys.exit(0 if r["installation_kind"]=="installed_wheel" and r["commit"]==sys.argv[1] and r["source_dirty"] is False else "engine manifest does not match ENGINE_COMMIT or is dirty")' "${ENGINE_COMMIT}" \
 && rm /tmp/engine-version.json \
 && /opt/campaign-tool/venv/bin/python -I -m campaign_tool.records.extract.ocr --doctor

LABEL org.opencontainers.image.title="deflock-flock-ocr" \
      org.opencontainers.image.revision="${ENGINE_COMMIT}" \
      org.opencontainers.image.description="Pinned OCR toolchain and campaign_tool engine layer"
# USER, ENTRYPOINT and CMD are inherited from BASE_IMAGE unchanged on purpose.
