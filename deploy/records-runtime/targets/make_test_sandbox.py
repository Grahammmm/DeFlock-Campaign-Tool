#!/usr/bin/env python3
"""Write the TEST sandbox stage plan (T11i) from one argv table.

Generates ``deploy/records-runtime/targets/test-sandbox.json.in``. Run it after
changing the table; ``tests/test_records_run_stages.py`` checks the committed
template is current.
"""
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = "/workspace/flockbot/work/records-project"
SANDBOX = ["@DOCKER@", "run", "--rm", "--network", "none", "--user", "@SANDBOX_USER@",
           "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
           "-v", "@SANDBOX_PROJECT_DIR@:" + PROJECT,
           "-v", "@SANDBOX_ENGINE_DIR@:/engine:ro",
           "-v", "@PORTAL_APPROVAL_FILE@:/run/flock-portal-approval.json:ro",
           "-e", "PYTHONPATH=/engine", "@EXPECTED_IMAGE@",
           "python3", "-B", "-m", "campaign_tool.records.run_stages"]
STAGES = (
    # name, timeout, on_fail, extra adapter arguments
    ("sync", 120, "stop", []),
    ("delta", 600, "stop", []),
    ("adoption", 900, "continue", ["--config", PROJECT + "/.t4-adoption-config-v1.json"]),
    ("portal", 300, "continue", ["--approval-file", "/run/flock-portal-approval.json"]),
    ("extract", 300, "stop", []),
    ("digest_gates", 120, "stop", []),
    ("outputs", 120, "stop", []),
)


def plan():
    stages = []
    for name, timeout, on_fail, extra in STAGES:
        # The adapter's own deadline is 10 s inside the host timeout so the
        # container-side work stops before the docker client is killed.
        argv = SANDBOX + [name, "--project", PROJECT, "--timeout", str(timeout - 10),
                          "--run-id", "{run_id}", *extra]
        stages.append({"name": name, "argv": argv, "timeout_seconds": timeout, "on_fail": on_fail})
    return {"target_config_version": 1, "target": "@TARGET_NAME@", "mode": "test", "stages": stages}


def render_text():
    return json.dumps(plan(), indent=1) + "\n"


if __name__ == "__main__":
    (HERE / "test-sandbox.json.in").write_text(render_text(), encoding="utf-8")
