"""Go-live stage evidence from stored v1 reports, not semantic acceptance.

Reads only top-level .json reports and the shipped validation schema. Test and
stub runs do not count as live evidence. V1 has no engine identity or independent
substage receipts: proof here means the mapped metadata qualifies, not that an
artifact was accepted, a draft was queued for a specific item, or mail was sent.
"""

import argparse
import json
import os
import sys
from dataclasses import dataclass

from . import run_report
from .console_runs_model import _read_at

EXTERNAL = "external: not provable from run reports"
# stage_outputs keeps the first additive warning, in this producer order.
# DIGEST_UNRESOLVED is special: it overwrites any earlier additive warning.
OUTPUT_WARNINGS = (
    "OUTPUTS_APPROVAL_QUEUE_UNAVAILABLE",
    "OUTPUTS_UNMATCHED_UNAVAILABLE",
    "OUTPUTS_MODEL_ADVICE_UNAVAILABLE",
    "OUTPUTS_REGISTER_UNAVAILABLE",
    "OUTPUTS_FOLLOWUP_SETTINGS_INVALID",
    "OUTPUTS_FOLLOWUP_CLOCK_UNAVAILABLE",
    "OUTPUTS_GAP_PROPOSALS_UNAVAILABLE",
    "OUTPUTS_NEXT_STEPS_UNAVAILABLE",
    "OUTPUTS_TEXTSTATE_UNAVAILABLE",
    "OUTPUTS_RESPONSE_ITEMS_UNAVAILABLE",
    "OUTPUTS_TIMELINE_UNAVAILABLE",
    "OUTPUTS_FOLLOWUP_ENGINE_UNAVAILABLE",
    "OUTPUTS_ANALYSIS_READY_UNAVAILABLE",
)
OVERWRITE = ("OUTPUTS_DIGEST_UNRESOLVED",)
DOCUMENT_WARNINGS = ("EXTRACT_DOCUMENT_FAILURES", "EXTRACT_BACKLOG_DEFERRED")


def before(code):
    return OUTPUT_WARNINGS[:OUTPUT_WARNINGS.index(code)]


def after(code):
    return OUTPUT_WARNINGS[OUTPUT_WARNINGS.index(code) + 1:]


@dataclass(frozen=True)
class Stage:
    label: str
    source: str | None = None
    nonblocking: tuple[str, ...] = ()
    masks: tuple[str, ...] = ()


# GOLIVE-STATUS-2026-11-18 §1. Insertion order is the display order.
STAGES = {
    "1": Stage("Request sent"),
    "2": Stage("Email intake / sync", "sync"),
    "3": Stage("Arrival trigger"),
    "4": Stage("Portal downloads"),
    "5": Stage("Save + extract text", "extract", DOCUMENT_WARNINGS),
    "6": Stage("OCR", "extract", DOCUMENT_WARNINGS),
    "7": Stage("Catalogue / adoption", "adoption"),
    "8": Stage("Digest gates", "digest_gates", ("GATE_REVIEW_REQUIRED",)),
    "9": Stage("Facts", "outputs", OUTPUT_WARNINGS + OVERWRITE),
    "10": Stage("Policy comparison", "outputs", OUTPUT_WARNINGS + OVERWRITE),
    "11": Stage("Gap proposals", "outputs", after("OUTPUTS_GAP_PROPOSALS_UNAVAILABLE"),
                before("OUTPUTS_GAP_PROPOSALS_UNAVAILABLE") + OVERWRITE),
    "12": Stage("Received items, timeline, register", "outputs", after("OUTPUTS_TIMELINE_UNAVAILABLE"),
                tuple(code for code in before("OUTPUTS_TIMELINE_UNAVAILABLE")
                      if code != "OUTPUTS_REGISTER_UNAVAILABLE") + OVERWRITE),
    "13": Stage("Reply drafts to approval queue", "outputs", after("OUTPUTS_APPROVAL_QUEUE_UNAVAILABLE"), OVERWRITE),
    # A register failure skips the follow-up clock entirely; that's a blocker, not masking.
    "14": Stage("No-response follow-up drafts", "outputs", after("OUTPUTS_FOLLOWUP_CLOCK_UNAVAILABLE"),
                before("OUTPUTS_REGISTER_UNAVAILABLE") + OVERWRITE),
    "15": Stage("Engine-owned follow-ups", "outputs", after("OUTPUTS_FOLLOWUP_ENGINE_UNAVAILABLE"),
                before("OUTPUTS_FOLLOWUP_ENGINE_UNAVAILABLE") + OVERWRITE),
    # V1 has no findings stage or attendance receipt; analysis-ready is not a findings run.
    "16": Stage("Findings lane", "findings"),
    "17": Stage("Packets to website staging"),
    "18": Stage("Timers + monitoring"),
    "19": Stage("Backups"),
    "20": Stage("Approval trust anchor"),
    "A1": Stage("Console on the box"),
    "A2": Stage("Console export"),
    "A3": Stage("New console"),
    "A4": Stage("Console access"),
    "A5": Stage("Decisions applied"),
    "A6": Stage("Phone signing"),
    "A7": Stage("Workers console / shim"),
}


def load_reports(directory):
    schema = run_report.load_schema()
    reports = {}
    try:
        dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        raise ValueError(f"{directory!r}: reports directory unreadable") from None
    try:
        with os.scandir(dir_fd) as entries:
            names = sorted(entry.name for entry in entries if entry.name.endswith(".json"))
        for name in names:
            try:
                report = _read_at(dir_fd, name)
                run_report.validate_run_report(report, schema)
            except OSError:
                raise ValueError(f"{name!r}: unreadable report") from None
            except (ValueError, TypeError, KeyError, OverflowError):
                raise ValueError(f"{name!r}: invalid report") from None
            run_id = report["run_id"]
            if run_id in reports:
                raise ValueError(f"{name!r}: duplicate run ID {run_id}")
            reports[run_id] = report
    except OSError:
        raise ValueError(f"{directory!r}: reports directory unreadable") from None
    finally:
        os.close(dir_fd)
    return sorted(reports.values(), key=lambda report: (report["finished_at"], report["run_id"]))


def _observation(number, rule, stage, report, codes):
    if stage is None:
        return "not reported", None, None
    status = stage["status"]
    warning = stage["error_code"] if status == "warn" else None
    if warning in rule.masks:
        return "masked", warning, warning
    qualifies = status == "ok" or warning in rule.nonblocking
    if number == "2" and "SYNC_OFFLINE_SNAPSHOT" in codes:
        qualifies = False
    if number == "5" and warning and not stage["counts"]["output"]:
        qualifies = False  # Partial extraction only qualifies if some extraction succeeded.
    if number == "6" and not report.get("ocr", {}).get("pages_ocr"):
        qualifies = False  # Native extraction (or a zero OCR tally) isn't OCR evidence.
    return "proven" if qualifies else "not proven", warning, None


def summarize(reports, since):
    run_ids = [report["run_id"] for report in reports]
    if since is not None and since not in run_ids:
        raise ValueError(f"--since {since!r}: run ID not found")
    start = 0 if since is None else run_ids.index(since) + 1
    live_runs = []
    for index, report in enumerate(reports):
        codes = {item["code"] for item in report["needs_g"]}
        if report["mode"] != "test" and "STUB_STAGE_BACKEND" not in codes:
            live_runs.append((index, report, {stage["name"]: stage for stage in report["stages"]}, codes))

    rows = []
    for number, rule in STAGES.items():
        state = EXTERNAL if rule.source is None else "not reported"
        masked_by = proof = first_warning = last_warning = runs_after = None
        if rule.source is not None:
            for index, report, stages, codes in live_runs:
                state, warning, masked_by = _observation(number, rule, stages.get(rule.source), report, codes)
                if state == "proven":
                    proof, runs_after = report["run_id"], 0
                elif runs_after is not None:
                    runs_after += 1
                if warning is not None and index >= start:
                    first_warning = first_warning or warning
                    last_warning = warning
        rows.append(dict(stage=number, name=rule.label, state=state, last_proven_run=proof,
                         first_warning=first_warning, last_warning=last_warning,
                         runs_since_last_proof=runs_after, masked_by=masked_by))
    return dict(since=since, latest_run=live_runs[-1][1]["run_id"] if live_runs else None, stages=rows)


def format_table(summary):
    def cells(row):
        return " | ".join("-" if value is None else str(value) for value in row)

    lines = [f"Latest eligible run: {summary['latest_run'] or '-'}; warnings after: {summary['since'] or '(all)'}",
             "Stage | Name | Latest evidence | Last proven run | First warning | Last warning | Runs since proof | Masked by"]
    lines.extend(cells(row.values()) for row in summary["stages"])
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reports", required=True, help="directory containing only run report .json files")
    parser.add_argument("--since", metavar="RUN_ID",
                        help="show warnings after this stored run; keep proof and age from all eligible reports")
    parser.add_argument("--json", action="store_true", help="emit the same evidence as JSON")
    args = parser.parse_args(argv)
    try:
        reports = load_reports(args.reports)
        summary = summarize(reports, args.since)
    except ValueError as exc:
        print(f"golive_stages: {exc}", file=sys.stderr)
        return 2
    except OSError:
        print("golive_stages: bundled run-report schema unreadable", file=sys.stderr)
        return 2
    print(json.dumps(summary, indent=2) if args.json else format_table(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
