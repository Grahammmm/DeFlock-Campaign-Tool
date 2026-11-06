"""Metadata-only records run notification text (packaged so the installed engine can use it).

Moved verbatim from deploy/records-runtime/notify.py, which imports these names back.
The message is built only from enums, counts and upper-snake codes in the run report,
never from record content.
"""
from __future__ import annotations

from typing import Any

from campaign_tool.records import run_coverage

ARRIVAL_KINDS = ("response", "extension", "denial", "fee", "records", "other")

PREFLIGHT_MESSAGES = {
    "DOCKER_UNAVAILABLE": "{label}: records run {run_id} could not start because Docker or the "
                          "records container was not ready. No records work was attempted.",
    "IMAGE_MISMATCH": "{label}: records run {run_id} refused to start (configuration or image "
                      "check failed). No records work was attempted.",
    "TARGET_CONFIG_INVALID": "{label}: records run {run_id} refused to start (target stage plan "
                             "invalid). No records work was attempted.",
    "DISK_SPACE_LOW": "{label}: records run {run_id} refused to start (runtime disk space below "
                      "the configured minimum). No records work was attempted.",
    "DISK_SPACE_UNKNOWN": "{label}: records run {run_id} refused to start (runtime disk space "
                          "could not be measured). No records work was attempted.",
}


def kind_line(summary: dict[str, Any] | None) -> str:
    kinds = (summary or {}).get("received_by_kind")
    if not isinstance(kinds, dict):
        return "Came in: unavailable."
    total = sum(kinds[k] for k in ARRIVAL_KINDS)
    if not total:
        return "Came in: 0 new items."
    parts = ", ".join(f"{kinds[k]} {k}" for k in ARRIVAL_KINDS if kinds[k])
    return f"Came in: {total} new item(s) ({parts}). Items, not messages."


def compose_arrival(report: dict[str, Any], exit_code: int) -> str:
    """Fixed-section arrival message: Came in / Done / Needs G. Run id, enums, counts and codes only.

    The heading is a constant (never the arbitrary --label); no record, request, draft or agency
    identifier, filename, address, subject or text can enter. Coverage stays last, as in legacy text.
    """
    run_id = f"{report['run_id']} ({report['mode']})"
    work = [stage for stage in report["stages"] if stage["name"] != "notify"]
    first_failed = next((stage for stage in work if stage["status"] == "fail"), None)
    if first_failed is not None and first_failed["error_code"] in PREFLIGHT_MESSAGES:
        return (f"Records arrival run {run_id} refused to start ({first_failed['error_code']}). "
                f"No work attempted; stages not attempted.\n"
                f"Came in: unavailable.\nNeeds G: drafts pending unavailable.")
    summary = report.get("arrival_summary")
    pending = (summary or {}).get("pending_drafts")
    done = []
    for stage in work:
        text = f"{stage['name']} {stage['status']}"
        if stage["status"] in ("warn", "fail"):
            text += f" {stage['error_code']}"
        elif stage["status"] == "ok" and stage["counts"]["input"]:
            text += f" {stage['counts']['output']}/{stage['counts']['input']}"
        done.append(text)
    groups = report["needs_g"]
    holds = (f"{len(groups)} hold group(s), {sum(g['count'] for g in groups)} issue occurrence(s)"
             if groups else "no holds")
    drafts = "drafts waiting for approval: unavailable" if pending is None else \
        f"{pending} draft(s) waiting for approval"
    head = f"Records arrival run {run_id}" + (f" FAILED at {first_failed['name']} (exit {exit_code})"
                                              if first_failed is not None else "")
    coverage = run_coverage.format_coverage(run_coverage.coverage_counts(report))
    return "\n".join([head, kind_line(summary), "Done: " + ", ".join(done) + ".",
                      f"Needs G: {drafts}; {holds}.", coverage])


def compose(report: dict[str, Any], label: str, exit_code: int) -> str:
    """Build the message from report enums, counts and codes only.

    Every message names the report mode after the run id. Non-preflight
    messages end with the coverage line (run_coverage, contract v1). The T17
    cutover message is simply this message for the first scheduled live run.
    Arrival-triggered runs use compose_arrival instead.
    """
    if report.get("trigger") == "arrival":
        return compose_arrival(report, exit_code)
    run_id = f"{report['run_id']} ({report['mode']})"
    work = [stage for stage in report["stages"] if stage["name"] != "notify"]
    failed = [stage for stage in work if stage["status"] == "fail"]
    warned = [stage for stage in work if stage["status"] == "warn"]
    needs = len(report["needs_g"])
    tail = f" Needs G: {needs} item(s)." if needs else ""
    coverage = "\n" + run_coverage.format_coverage(run_coverage.coverage_counts(report))
    if failed:
        first = failed[0]
        if first["error_code"] in PREFLIGHT_MESSAGES:
            return PREFLIGHT_MESSAGES[first["error_code"]].format(label=label, run_id=run_id) + tail
        return (f"{label}: records run {run_id} failed at stage {first['name']} "
                f"({first['error_code']}, exit {exit_code}). The runtime needs attention. "
                f"No replies were sent.{tail}{coverage}")
    if warned:
        codes = ", ".join(f"{stage['name']} {stage['error_code']}" for stage in warned)
        return (f"{label}: records run {run_id} completed with warnings ({codes}). "
                f"Details are in the private run report.{tail}{coverage}")
    return (f"{label}: records run {run_id} completed. "
            f"Details are in the private run report.{tail}{coverage}")
