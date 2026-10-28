"""Validate Flock records run reports against schemas/records-run-report-v1.json.

The JSON Schema covers shape and value formats using the stdlib-only subset in
``campaign_tool.law.validate``. This module adds the cross-field rules that
schema subset cannot express. A valid report is machine-valid only; it is not
semantic acceptance of the run.
"""

import json
import math
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


def check_outcomes(outcomes, counts, where="outcomes"):
    """Per-document outcome codes: unique, and exactly one per counted input unit."""
    codes = [item["code"] for item in outcomes]
    if len(set(codes)) != len(codes):
        raise ValueError(f"{where}: duplicate outcome code")
    total = sum(item["count"] for item in outcomes)
    if total != counts["input"]:
        raise ValueError(f"{where}: outcome counts sum to {total}, counts.input is {counts['input']}")


def check_finite_durations(report):
    """Reject NaN and infinite durations; diagnostics never echo the value.

    The shared ``law`` subset checks number type and minimum but not
    finiteness. Only float values can be non-finite; anything else is left to
    ``law.validate`` so its existing type/minimum/required errors are unchanged.
    """
    if not isinstance(report, dict):
        return
    if _nonfinite(report.get("duration_seconds")):
        raise ValueError("$.duration_seconds: must be a finite number")
    stages = report.get("stages")
    if isinstance(stages, list):
        for index, stage in enumerate(stages):
            if isinstance(stage, dict) and _nonfinite(stage.get("duration_seconds")):
                raise ValueError(f"$.stages[{index}].duration_seconds: must be a finite number")


def _nonfinite(value):
    return isinstance(value, float) and not math.isfinite(value)


def validate_run_report(report, schema=None):
    """Raise ValueError when ``report`` does not satisfy run report v1."""
    schema = schema if schema is not None else load_schema()
    check_finite_durations(report)
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
        if "outcomes" in stage:
            check_outcomes(stage["outcomes"], stage["counts"], f"{where}.outcomes")

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
