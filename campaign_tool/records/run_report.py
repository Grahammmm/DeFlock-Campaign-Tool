"""Validate Flock records run reports against schemas/records-run-report-v1.json.

The JSON Schema covers shape and value formats using the stdlib-only subset in
``campaign_tool.law.validate``. This module adds the cross-field rules that
schema subset cannot express. A valid report is machine-valid only; it is not
semantic acceptance of the run.
"""

import json
import math
import re
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


# DF1117L-05: delta/adoption stages record which coded adoption gaps held messages back in
# ``outcomes`` (report v1 shape unchanged). Rows are GA_<CODE> (accepted gap), GB_<CODE>
# (blocking gap), GA_OTHER/GB_OTHER (overflow beyond MAX_GAP_CODES) and GAP_NONE (receipts
# without a coded gap). A recorded [] is a measured zero-input stage; absent means unavailable.
GAP_STAGES = ("delta", "adoption")
GAP_NONE, GAP_OTHER, MAX_GAP_CODES = "GAP_NONE", "OTHER", 32
_WIRE = re.compile(r"[A-Z][A-Z0-9_]{2,63}")
_SOURCE = re.compile(r"[a-z][a-z0-9_]*")
_SUFFIX = re.compile(r"[A-Z][A-Z0-9_]*")


def _count(value):
    return type(value) is int and value >= 0


def gap_partition(report, accepted):
    """Encode an adopt-mail report's coded gaps as a stage outcomes partition, or None.

    None (nothing recorded) when the gap data is missing, malformed, negative, or its sums
    disagree with the report's own totals; rejected text is never echoed.
    """
    if not isinstance(report, dict):
        return None
    gaps, total = report.get("coded_gaps"), report.get("coded_gap_messages")
    blocked, receipts = report.get("unaccepted_coded_gaps") or {}, report.get("receipts")
    if not (isinstance(gaps, dict) and isinstance(blocked, dict) and _count(total) and _count(receipts)):
        return None
    if not all(type(key) is str and _SOURCE.fullmatch(key) and _count(value) for key, value in gaps.items()):
        return None
    if sum(gaps.values()) != total or total > receipts or any(not _count(v) for v in blocked.values()):
        return None
    if sum(blocked.values()) != sum(v for k, v in gaps.items() if k not in accepted):
        return None
    named, other = [], {"GA": 0, "GB": 0}
    for key, value in gaps.items():
        prefix = "GA" if key in accepted else "GB"
        wire = f"{prefix}_{key.upper()}"
        if value and (key.upper() == GAP_OTHER or not _WIRE.fullmatch(wire)):
            other[prefix] += value
        elif value:
            named.append((wire, value))
    named.sort(key=lambda row: (-row[1], row[0]))
    for wire, value in named[MAX_GAP_CODES:]:
        other[wire[:2]] += value
    rows = [{"code": wire, "count": value} for wire, value in named[:MAX_GAP_CODES]]
    rows += [{"code": f"{prefix}_{GAP_OTHER}", "count": value} for prefix, value in other.items() if value]
    if receipts > total:
        rows.append({"code": GAP_NONE, "count": receipts - total})
    return rows


def _gap_rows(outcomes):
    """Decode a partition into ``{code, count, accepted}`` rows; ValueError when malformed."""
    rows, named = [], set()
    for item in outcomes:
        code, count = item["code"], item["count"]
        if not (type(count) is int and count >= 1):
            raise ValueError("gap partition: count must be a positive integer")
        if code == GAP_NONE:
            continue
        if code[:3] not in ("GA_", "GB_") or not _SUFFIX.fullmatch(code[3:]) or not _WIRE.fullmatch(code):
            raise ValueError("gap partition: not a gap code")
        suffix = code[3:]
        if suffix != GAP_OTHER:
            if suffix in named:
                raise ValueError("gap partition: code recorded as both accepted and blocking")
            named.add(suffix)
        rows.append({"code": suffix, "count": count, "accepted": code[:2] == "GA"})
    if len(named) > MAX_GAP_CODES:
        raise ValueError("gap partition: more than 32 named codes")
    return rows


def adoption_gap_breakdown(stage):
    """``[{code, count, accepted}]`` recorded by a delta/adoption stage; None when not recorded.

    ``code`` is the upper-case gap code (``OTHER`` for the overflow bucket); a stage that
    recorded no gap returns ``[]`` and one without the field (older report) returns None.
    """
    if not isinstance(stage, dict) or stage.get("name") not in GAP_STAGES or "outcomes" not in stage:
        return None
    try:
        return _gap_rows(stage["outcomes"])
    except (ValueError, KeyError, TypeError):
        return None


def check_gap_partition(stage, needs_g, where):
    """Delta/adoption outcomes must be a gap partition agreeing with the stage's needs-G totals."""
    rows = _gap_rows(stage["outcomes"])
    totals = {item["code"]: item["count"] for item in needs_g if item["stage"] == stage["name"]}
    blocking = sum(row["count"] for row in rows if not row["accepted"])
    if (sum(row["count"] for row in rows) != totals.get("ADOPTION_CODED_GAPS", 0)
            or blocking != totals.get("ADOPTION_UNACCEPTED_GAP", 0)):
        raise ValueError(f"{where}: gap partition disagrees with the stage's coded-gap totals")


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


def check_ocr(block):
    if sum(block[key] for key in ("pages_conf_ge80", "pages_conf_lt80", "pages_conf_unknown")) != block["pages_ocr"]:
        raise ValueError("OCR bucket counts disagree")
    if block["pages_reviewed"] > block["pages_ocr"]:
        raise ValueError("OCR reviewed count exceeds pages")


def check_refusals(block, extract_stage):
    refused = sum(o["count"] for o in extract_stage.get("outcomes", []) if o["code"] == "EXTRACT_NOT_ACCEPTED")
    if block["not_accepted_coded"] > refused:
        raise ValueError("Coded refusal count exceeds refusals")
    if block["unlisted"] > extract_stage["counts"]["input"]:
        raise ValueError("Unlisted refusal count exceeds input")


def check_arrival_metadata(report):
    """Calendar-validate arrival stamps and tie arrival mode to the arrival trigger."""
    trigger = report.get("trigger")
    if (report["mode"] == "arrival" and trigger not in (None, "arrival")) or (
            trigger == "arrival" and report["mode"] != "arrival"):
        raise ValueError("$.trigger: arrival mode and arrival trigger must agree")
    for index, item in enumerate(report.get("arrivals", [])):
        try:
            datetime.strptime(item["seen_at"], "%Y-%m-%dT%H:%M:%S.%fZ" if "." in item["seen_at"]
                              else "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            raise ValueError(f"$.arrivals[{index}].seen_at: not a calendar date") from None


def validate_run_report(report, schema=None):
    """Raise ValueError when ``report`` does not satisfy run report v1."""
    schema = schema if schema is not None else load_schema()
    check_finite_durations(report)
    law.validate(report, schema)

    names = tuple(stage["name"] for stage in report["stages"])
    if names != STAGE_ORDER:
        raise ValueError(f"$.stages: expected stage order {list(STAGE_ORDER)}, got {list(names)}")
    if "ocr" in report:
        check_ocr(report["ocr"])
    if "refusals" in report:
        check_refusals(report["refusals"], report["stages"][4])

    check_arrival_metadata(report)
    if "arrival_summary" in report and report.get("trigger") != "arrival":
        raise ValueError("$.arrival_summary: only an arrival-triggered run carries an arrival summary")
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
            if stage["name"] in GAP_STAGES:
                check_gap_partition(stage, report["needs_g"], f"{where}.outcomes")

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
