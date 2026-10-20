#!/usr/bin/env python3
"""Write the TEST and LIVE container stage plans (T11i, T16) from one argv table.

Generates ``deploy/records-runtime/targets/test-container.json.in`` (mode
``test``) and ``deploy/records-runtime/targets/live-container.json.in`` (mode
``live``). The two plans are identical apart from ``mode``: the same argv
table, placeholders and stage timeouts drive a sandbox copy of TEST and the
real records project alike. Each stage is
``docker exec -u CONTAINER_USER CONTAINER env PYTHONPATH=ENGINE
CONTAINER_PYTHON -m campaign_tool.records.run_stages STAGE --project PROJECT
...``.
``CONTAINER_PYTHON`` is the image's parser environment
(``/opt/campaign-tool/venv/bin/python`` in the pinned flock-ocr image, which
carries the hash-locked pypdf/openpyxl bundle) when present, else the system
``python3``; without pypdf/openpyxl the extract stage codes PDF/XLSX documents
``PARSER_MISSING`` and does not attempt them. The
adapter enforces its own
container-side deadline (``--timeout``, 10 s inside the host timeout) because
killing the ``docker exec`` client does not stop work in the container.
``tests/test_records_run_stages.py`` checks both committed templates are
current.
"""
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXEC = ["@DOCKER@", "exec", "-u", "@CONTAINER_USER@", "@CONTAINER@",
        "env", "PYTHONPATH=@CONTAINER_ENGINE_DIR@",
        "@CONTAINER_PYTHON@", "-B", "-m", "campaign_tool.records.run_stages"]
STAGES = (
    # name, timeout, on_fail, extra adapter arguments
    ("sync", 120, "stop", []),
    ("delta", 600, "stop", []),
    # Adoption is fail-closed per identity; other stages work on already-ledgered data.
    ("adoption", 900, "continue", ["--config", "@ADOPTION_CONFIG@"]),
    ("portal", 300, "continue", ["--approval-file", "@CONTAINER_APPROVAL_FILE@"]),
    # Native-text extraction (T11k) works through the backlog under its own
    # deadline and per-run document cap; the rest is counted as deferred.
    ("extract", 600, "stop", ["--max-documents", "200", "--document-timeout", "120"]),
    ("digest_gates", 120, "stop", []),
    ("outputs", 120, "stop", []),
)

MODES = {"test", "live"}


def plan(mode="test"):
    if mode not in MODES:
        raise ValueError(f"mode: expected one of {sorted(MODES)}")
    stages = []
    for name, timeout, on_fail, extra in STAGES:
        argv = EXEC + [name, "--project", "@CONTAINER_PROJECT_DIR@", "--timeout", str(timeout - 10),
                       "--run-id", "{run_id}", *extra]
        stages.append({"name": name, "argv": argv, "timeout_seconds": timeout, "on_fail": on_fail})
    return {"target_config_version": 1, "target": "@TARGET_NAME@", "mode": mode, "stages": stages}


def render_text(mode="test"):
    return json.dumps(plan(mode), indent=1) + "\n"


if __name__ == "__main__":
    (HERE / "test-container.json.in").write_text(render_text("test"), encoding="utf-8")
    (HERE / "live-container.json.in").write_text(render_text("live"), encoding="utf-8")
