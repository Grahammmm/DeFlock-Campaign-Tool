# Runner image: pulls jobs from the workspace Worker, runs the engine, posts results.
# Build from the repository root:  docker build -f runner/Dockerfile -t deflock-runner .
# See docs/RUNNER.md. The image carries no credentials; configuration is environment only.
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Non-root user; /work is the only writable location (RUNNER_WORKDIR, 0700).
RUN groupadd --system --gid 10001 runner \
 && useradd --system --uid 10001 --gid runner --home-dir /work --shell /usr/sbin/nologin runner \
 && mkdir -p /work /app && chown runner:runner /work && chmod 0700 /work

WORKDIR /app
COPY requirements-runner.txt ./
RUN pip install -r requirements-runner.txt

# Only what the handlers import: engine, runner, law packages, templates, schemas, public-tree checker.
COPY campaign_tool/ ./campaign_tool/
COPY runner/__init__.py runner/__main__.py runner/client.py runner/loop.py ./runner/
COPY runner/handlers/ ./runner/handlers/
COPY jurisdictions/ ./jurisdictions/
COPY templates/ ./templates/
COPY schemas/ ./schemas/
COPY tools/check_public_tree.py ./tools/check_public_tree.py

# Build-time smoke test: the fake workspace has no jobs, so --once exits 3 (idle).
RUN python3 -B -m runner --once --fake || test "$?" -eq 3

USER runner
ENV RUNNER_WORKDIR=/work \
    JURISDICTIONS_DIR=/app/jurisdictions \
    PRIVACY_TIER=redacted_cloud \
    POLL_INTERVAL=15 \
    LEASE_SECONDS=300
VOLUME ["/work"]

# WORKSPACE_URL and RUNNER_TOKEN are required at run time (never baked in).
ENTRYPOINT ["python3", "-B", "-m", "runner"]
