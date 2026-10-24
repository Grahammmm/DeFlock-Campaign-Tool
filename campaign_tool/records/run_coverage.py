"""Coverage line projected from a Flock records run report v1 (coverage contract v1).

A pure, read-only consumer of a run report dict. It reads only the report's
stage statuses, stage counts and needs-G items, writes nothing, and imports no
pipeline stage code. The numbers are metadata (counts, enums, codes); a
machine-valid line is review-required, not semantic acceptance of the run.

Contract v1 (see deploy/records-runtime/README.md, "Coverage line"):

- adopted = S(adoption).counts.output + S(adoption).counts.skipped
  - n(adoption, ADOPTION_CODED_GAPS); adopted_this_run = S(adoption).counts.output
- coded_gap = n(adoption, ADOPTION_CODED_GAPS)
- awaiting_ocr = n(extract, AWAITING_OCR)
- portal_held = n(portal, PORTAL_FETCH_DRY_RUN_ONLY) + n(portal, PORTAL_HTTP_404)
  + n(portal, PORTAL_FETCH_BLOCKED)
- A count is None ("unknown") unless its source stage status is ok or warn;
  None is never rendered as 0. adopted is also None when the subtraction is
  negative.
- stub = any needs-G item with code STUB_STAGE_BACKEND; mode = report["mode"].

CLI: ``python3 -m campaign_tool.records.run_coverage --report PATH`` validates
the report with ``run_report.validate_run_report`` and prints the line (exit 0),
or exits 2 when the report is unreadable or invalid.
"""

import argparse
import json
import sys
from pathlib import Path

AVAILABLE = ("ok", "warn")
CODED_GAPS = "ADOPTION_CODED_GAPS"
AWAITING_OCR = "AWAITING_OCR"
PORTAL_HELD_CODES = ("PORTAL_FETCH_DRY_RUN_ONLY", "PORTAL_HTTP_404", "PORTAL_FETCH_BLOCKED",
                     "PORTAL_DOWNLOAD_PENDING", "PORTAL_LEDGER_PENDING")
STUB_CODE = "STUB_STAGE_BACKEND"


def _stage(report, name):
    for stage in report["stages"]:
        if stage["name"] == name:
            return stage
    return None


def _available(report, name):
    stage = _stage(report, name)
    return stage if stage is not None and stage["status"] in AVAILABLE else None


def _n(report, stage, code):
    """``count`` of the needs-G item with this stage and code, 0 when absent."""
    return sum(item["count"] for item in report["needs_g"]
               if item["stage"] == stage and item["code"] == code)


def coverage_counts(report):
    """Return the coverage counts (int or None), ``stub`` and ``mode`` for a v1 report."""
    adopted = adopted_this_run = coded_gap = awaiting_ocr = portal_held = None
    adoption = _available(report, "adoption")
    if adoption is not None:
        coded_gap = _n(report, "adoption", CODED_GAPS)
        adopted_this_run = adoption["counts"]["output"]
        total = adoption["counts"]["output"] + adoption["counts"]["skipped"] - coded_gap
        adopted = total if total >= 0 else None
    if _available(report, "extract") is not None:
        awaiting_ocr = _n(report, "extract", AWAITING_OCR)
    if _available(report, "portal") is not None:
        portal_held = sum(_n(report, "portal", code) for code in PORTAL_HELD_CODES)
    return {
        "adopted": adopted,
        "adopted_this_run": adopted_this_run,
        "coded_gap": coded_gap,
        "awaiting_ocr": awaiting_ocr,
        "portal_held": portal_held,
        "stub": any(item["code"] == STUB_CODE for item in report["needs_g"]),
        "mode": report["mode"],
    }


def _show(value):
    return "unknown" if value is None else str(value)


def format_coverage(counts):
    """``Coverage: adopted A (+a this run), coded-gap C, awaiting_ocr O, portal-held P``."""
    adopted = _show(counts["adopted"])
    if counts["adopted"] is not None and counts["adopted_this_run"] is not None:
        adopted += f" (+{counts['adopted_this_run']} this run)"
    line = (f"Coverage: adopted {adopted}, coded-gap {_show(counts['coded_gap'])}, "
            f"awaiting_ocr {_show(counts['awaiting_ocr'])}, "
            f"portal-held {_show(counts['portal_held'])}")
    return line + " [stub]" if counts["stub"] else line


def main(argv=None):
    # Validation is a CLI dependency, not a dependency of the pure projection.
    from campaign_tool.records import run_report
    parser = argparse.ArgumentParser(
        prog="python3 -m campaign_tool.records.run_coverage",
        description="Print the coverage line of a records run report v1.")
    parser.add_argument("--report", required=True, help="path to run-report.json")
    args = parser.parse_args(argv)
    try:
        report = json.loads(Path(args.report).read_text(encoding="utf-8"))
        run_report.validate_run_report(report)
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        reason = "unreadable" if isinstance(exc, OSError) else "invalid"
        print(f"run_coverage: run report {reason}; no coverage line", file=sys.stderr)
        return 2
    print(format_coverage(coverage_counts(report)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
