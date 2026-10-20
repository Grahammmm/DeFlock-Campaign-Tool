#!/usr/bin/env python3
"""Write the TEST container stage plan (T11i) from one argv table.

Generates ``deploy/records-runtime/targets/test-container.json.in``. Each stage
is ``docker exec -u CONTAINER_USER CONTAINER env PYTHONPATH=ENGINE python3 -m
campaign_tool.records.run_stages STAGE --project PROJECT ...`` against a
sandbox copy of TEST inside the Flock container. The adapter enforces its own
container-side deadline (``--timeout``, 10 s inside the host timeout) because
killing the ``docker exec`` client does not stop work in the container.
``tests/test_records_run_stages.py`` checks the committed template is current.
"""
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXEC = ["@DOCKER@", "exec", "-u", "@CONTAINER_USER@", "@CONTAINER@",
        "env", "PYTHONPATH=@CONTAINER_ENGINE_DIR@",
        "python3", "-B", "-m", "campaign_tool.records.run_stages"]
STAGES = (
    # name, timeout, on_fail, extra adapter arguments
    ("sync", 120, "stop", []),
    ("delta", 600, "stop", []),
    # Adoption is fail-closed per identity; other stages work on already-ledgered data.
    ("adoption", 900, "continue", ["--config", "@ADOPTION_CONFIG@"]),
    ("portal", 300, "continue", ["--approval-file", "@CONTAINER_APPROVAL_FILE@"]),
    ("extract", 300, "stop", []),
    ("digest_gates", 120, "stop", []),
    ("outputs", 120, "stop", []),
)


def plan():
    stages = []
    for name, timeout, on_fail, extra in STAGES:
        argv = EXEC + [name, "--project", "@CONTAINER_PROJECT_DIR@", "--timeout", str(timeout - 10),
                       "--run-id", "{run_id}", *extra]
        stages.append({"name": name, "argv": argv, "timeout_seconds": timeout, "on_fail": on_fail})
    return {"target_config_version": 1, "target": "@TARGET_NAME@", "mode": "test", "stages": stages}


def render_text():
    return json.dumps(plan(), indent=1) + "\n"


if __name__ == "__main__":
    (HERE / "test-container.json.in").write_text(render_text(), encoding="utf-8")
