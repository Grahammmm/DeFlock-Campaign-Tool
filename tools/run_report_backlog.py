"""Read-only held-item list for one records run report (REC-BACKLOG-1).

``REPORT.json`` (one machine-valid records-run-report-v1) prints one line per held class:
stage, code, count, whether the extract outcome count agrees with the needs_g count,
whether clearing writes live data, and the clearing path. Codes and counts only: no
document, message, path, hash or record content is read or printed. Nothing is written.

The held classes are the four named by the DeFlock go-live plan: EXTRACT_ERROR,
EXTRACT_NOT_ACCEPTED, EXTRACT_PRESERVE_PENDING (extract stage) and ADOPTION_CODED_GAPS
(adoption stage). A class missing from the report prints count 0. Where main has no
clearing verb, the line says so and names the owning card; no command is invented.
A listing is not acceptance and clears nothing. Exit 1: invalid report. Exit 2: refused input.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.run_report_review import RUN_ID, Refused, _field, load, read_file  # noqa: E402

SCHEMA = "run-report-backlog-v1"
NOTE = "listing only; clears nothing; machine-valid is review-required, not semantic acceptance"
# (stage, needs_g code, extract outcome code or None, writes live data, clearing path)
HELD = (
    ("extract", "EXTRACT_ERROR", "EXTRACT_ERROR", "yes",
     "none on main: an open extract alert holds the document after repeated runs; resolving it is a "
     "live ledger write by the attended operator after card BACKLOG-EXTRACT-1 names the cause"),
    ("extract", "EXTRACT_NOT_ACCEPTED", "EXTRACT_NOT_ACCEPTED", "yes",
     "none on main: the extracted text failed the acceptance check; clear only through card "
     "BACKLOG-EXTRACT-1 (new engine reports internal type/where); no model may clear it"),
    ("extract", "EXTRACT_PRESERVE_PENDING", "PRESERVE_PENDING", "yes",
     "none on main: originals not yet preserved; the attended T4B apply (docs/runbooks/T4B-TEST-APPLY.md) "
     "or card BACKLOG-PRESERVE-1 decides; a clear is a live-data write"),
    ("adoption", "ADOPTION_CODED_GAPS", None, "yes",
     "read-only view: python3 -m campaign_tool.records adopt-mail --root ROOT --mail-history MAIL (no --apply); "
     "clearing is the T4B resolution policy apply (docs/RECORDS-MAIL-ADOPTION.md), attended, G approval"),
)


def facts(report, diagnostic):
    """Return (facts, exit status)."""
    run_id = _field(report, "run_id", RUN_ID) or "unknown"  # strict shape, else never echoed
    if diagnostic is not None:
        return {"schema": SCHEMA, "run_id": run_id, "valid": False, "diagnostic": diagnostic, "held": []}, 1
    owed = {(i["stage"], i["code"]): i["count"] for i in report["needs_g"]}
    extract = next(s for s in report["stages"] if s["name"] == "extract")
    outcomes = {o["code"]: o["count"] for o in extract.get("outcomes", [])}
    held = []
    for stage, code, outcome, writes, path in HELD:
        count = owed.get((stage, code), 0)
        check = "n/a" if outcome is None else "match" if outcomes.get(outcome, 0) == count else "differs"
        held.append({"stage": stage, "code": code, "count": count, "outcome_check": check,
                     "clear_writes_live_data": writes, "clearing": path})
    return {"schema": SCHEMA, "run_id": run_id, "valid": True, "diagnostic": None, "held": held,
            "total": sum(h["count"] for h in held), "note": NOTE}, 0


def render(f):
    if not f["valid"]:
        return [f"backlog | {f['run_id']} | VALID | no | {f['diagnostic']}", f"backlog | {NOTE}"]
    lines = [f"backlog | {f['run_id']} | VALID | yes"]
    for h in f["held"]:
        lines.append(f"backlog | {f['run_id']} | {h['stage']} | {h['code']} x{h['count']} | "
                     f"outcome {h['outcome_check']} | writes live data: {h['clear_writes_live_data']} | "
                     f"clear: {h['clearing']}")
    lines.append(f"backlog | held total={f['total']} | {NOTE}")
    return lines


def main(argv=None):
    parser = argparse.ArgumentParser(prog="run_report_backlog.py", description="Read-only held-item list.")
    parser.add_argument("report")
    parser.add_argument("--json", action="store_true", help="print one run-report-backlog-v1 document")
    args = parser.parse_args(argv)
    try:
        f, status = facts(*load(read_file(Path(args.report))))
    except (Refused, OSError) as exc:
        print(f"backlog | refused | {exc if isinstance(exc, Refused) else 'unreadable'}", file=sys.stderr)
        return 2
    print(json.dumps(f, indent=1, sort_keys=True) if args.json else "\n".join(render(f)))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
