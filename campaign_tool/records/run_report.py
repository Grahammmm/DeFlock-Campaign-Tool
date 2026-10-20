"""Validate Flock records run reports against schemas/records-run-report-v1.json.

The JSON Schema covers shape and value formats using the stdlib-only subset in
``campaign_tool.law.validate``. This module adds the cross-field rules that
schema subset cannot express. A valid report is machine-valid only; it is not
semantic acceptance of the run.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from campaign_tool import law

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "schemas" / "records-run-report-v1.json"
STAGE_ORDER = ("sync", "delta", "adoption", "portal", "extract",
               "digest_gates", "outputs", "notify")


def load_schema(path=None):
    return json.loads(Path(path or SCHEMA_PATH).read_text(encoding="utf-8"))


def _parse_utc(text):
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def validate_run_report(report, schema=None):
    """Raise ValueError when ``report`` does not satisfy run report v1."""
    schema = schema if schema is not None else load_schema()
    law.validate(report, schema)

    names = tuple(stage["name"] for stage in report["stages"])
    if names != STAGE_ORDER:
        raise ValueError(f"$.stages: expected stage order {list(STAGE_ORDER)}, got {list(names)}")

    started, finished = _parse_utc(report["started_at"]), _parse_utc(report["finished_at"])
    if finished < started:
        raise ValueError("$.finished_at: earlier than started_at")
    elapsed = (finished - started).total_seconds()
    if abs(report["duration_seconds"] - elapsed) > 1:
        raise ValueError("$.duration_seconds: disagrees with started_at/finished_at")

    for index, stage in enumerate(report["stages"]):
        where = f"$.stages[{index}]"
        status, code = stage["status"], stage["error_code"]
        if status in ("warn", "fail") and code is None:
            raise ValueError(f"{where}.error_code: required when status is {status}")
        if status == "ok" and code is not None:
            raise ValueError(f"{where}.error_code: must be null when status is ok")
        if stage["duration_seconds"] is None and status != "skipped":
            raise ValueError(f"{where}.duration_seconds: null only allowed for skipped stages")

    statuses = {stage["status"] for stage in report["stages"]}
    expected = "fail" if "fail" in statuses else "warn" if "warn" in statuses else "ok"
    if report["overall_status"] != expected:
        raise ValueError(f"$.overall_status: expected {expected!r} from stage statuses, "
                         f"got {report['overall_status']!r}")

    notify = report["stages"][-1]
    if report["notify_failed"] and notify["status"] != "fail":
        raise ValueError("$.notify_failed: true requires the notify stage status to be fail")
    if notify["status"] == "fail" and not report["notify_failed"]:
        raise ValueError("$.notify_failed: must be true when the notify stage failed")

    seen = set()
    for index, item in enumerate(report["needs_g"]):
        key = (item["code"], item["stage"])
        if key in seen:
            raise ValueError(f"$.needs_g[{index}]: duplicate code/stage {list(key)}")
        seen.add(key)
    return report
