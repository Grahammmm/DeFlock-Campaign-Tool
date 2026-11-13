"""``records holds``: per-item list of held extract documents, metadata only (HOLD-INSPECT-1).

Read-only: the ledger is opened only through ``run_stages.snapshot_ro``; no evidence payload,
original, unit text, mail or path is read or printed (12-hex hash prefixes and fixed codes only).
``--report`` compares counts with a run report's needs_g (``COUNT_MISMATCH``, a finding). Exit 0 on a
readable ledger, 2 with ``LEDGER_UNREADABLE <reason>`` otherwise. Contract: docs/RECORDS-HOLDS.md.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import sys

from . import native_extract as ne

SCHEMA = "records-holds-v1"
RUN_ID_RE = re.compile(r"^run_[0-9]{8}T[0-9]{6}Z_[0-9a-f]{8}$")
HEX64 = re.compile(r"[0-9a-f]{64}")
STAMP = re.compile(r"[0-9T:.+Z-]{10,40}")
ADOPTION = ("ADOPTION_CODED_GAPS",)
RETRY = (ne.ERROR, ne.TIMEOUT, ne.AWAITING_OCR, ne.DEFERRED, ne.PARSER_MISSING)  # S3 / next run / OCR / install
DISPOSITION = {**{code: "retryable" for code in RETRY}, ne.PRESERVE_PENDING: "custodian"}  # else review_required


class Unreadable(Exception):
    """The ledger cannot be read without writing; the argument is a fixed reason code."""


class _Snapshot:
    """The ``_query`` surface native_extract's read helpers use, over a read-only snapshot."""
    def __init__(self, db):
        self.db = db

    def _query(self, sql, values=()):
        return [dict(row) for row in self.db.execute(sql, values)]


def _code(value):
    return value if isinstance(value, str) and ne.REFUSAL_CODE_PATTERN.fullmatch(value) else ne.UNLISTED_CODE


def _stamp(value):
    return value if isinstance(value, str) and STAMP.fullmatch(value) else None


def _alert_hold(snapshot, subject):
    """``(outcome, alert row, holds)``: the alert holding under native_extract's rule, else the first open one."""
    first = (None, None, False)
    for row in ne.open_alerts(snapshot, subject):
        codes = ne.alert_codes(row["key"])
        outcome = ne.classify_codes(codes)
        if not ne.ocr_released(codes) and (outcome not in ne.RETRYABLE or row["count"] >= ne.MAX_ATTEMPTS):
            return outcome, row, True
        first = first if first[1] else (outcome, row, False)
    return first


def _alert_detail(outcome, key):
    if outcome == ne.NOT_ACCEPTED:
        return ne.held_refusal_detail(key)["refusal_code"]
    return ",".join(sorted({_code(code) for code in ne.alert_codes(key)}))


def _run_list(processing, run_id):
    """``{sha256: (outcome, detail_code)}`` from the run's private outcome list, or None."""
    path = Path(processing) / "runs" / f"extract-{ne.safe_run_name(run_id)}.json"
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
        if body.get("schema") != "records-extract-outcomes-v1" or body.get("run_id") != run_id:
            return None
        listed = {}
        for doc in body["documents"]:
            subject, outcome = doc["original_sha256"], doc["outcome"]
            if HEX64.fullmatch(subject) and outcome in ne.OUTCOMES:
                codes = doc.get("issue_codes") or ([doc["refusal_code"]] if "refusal_code" in doc else [])
                listed[subject] = (outcome, ",".join(sorted({_code(c) for c in codes})) or None)
        return listed
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def collect(project, run_id=None):
    """``(items, run_list_used)`` for every held extract document; raises ``Unreadable``."""
    from .run_stages import LedgerBusy, snapshot_ro

    processing = Path(project) / "processing-v1"
    listed = _run_list(processing, run_id) if run_id else None
    items = []
    try:
        with snapshot_ro(processing / "ledger.sqlite") as db:
            db.row_factory = sqlite3.Row
            snapshot = _Snapshot(db)
            for subject, preserved in ne.pending_subjects(snapshot):
                item = {"sha256_prefix": subject[:12], "attempts": 0, "first_run": None, "last_run": None,
                        "detail_code": None, "preserved": preserved}
                if not preserved:
                    seen = snapshot._query("SELECT first_seen_at FROM originals WHERE sha256=?", (subject,))
                    state = snapshot._query("SELECT status FROM stage_state WHERE original_sha256=? "
                                            "AND stage='preserve'", (subject,))
                    item.update(code=ne.PRESERVE_PENDING, first_run=_stamp(seen[0]["first_seen_at"]),
                                detail_code="preserve_" + _code(state[0]["status"] if state else "missing"))
                else:
                    outcome, row, holds = _alert_hold(snapshot, subject)
                    run = listed.get(subject) if listed else None
                    if run and not holds and run[0] != ne.EXTRACTED:
                        item.update(code=run[0], detail_code=run[1])  # what the latest run counted
                    elif outcome is not None:
                        item.update(code=outcome, detail_code=_alert_detail(outcome, row["key"]))
                    else:
                        continue  # pending, not held: parsed on the next run
                    if row is not None:
                        seen = snapshot._query("SELECT first_seen,last_seen FROM alerts WHERE key=?", (row["key"],))[0]
                        item.update(attempts=int(row["count"]), first_run=_stamp(seen["first_seen"]),
                                    last_run=_stamp(seen["last_seen"]))
                item["disposition"] = DISPOSITION.get(item["code"], "review_required")
                items.append(item)
    except LedgerBusy as error:
        raise Unreadable("busy") from error
    except (sqlite3.Error, OSError, ValueError, KeyError, TypeError, IndexError) as error:
        raise Unreadable("missing" if "database_missing" in str(error) else "error") from error
    order = {code: index for index, code in enumerate(ne.OUTCOMES)}
    return sorted(items, key=lambda i: (order.get(i["code"], 99), i["sha256_prefix"])), listed is not None


def load_report(path):
    """``(run_id, {(stage, code): count})`` from a run report, or None when unreadable or malformed."""
    try:
        report = json.loads(Path(path).read_text(encoding="utf-8"))
        run_id = report["run_id"]
        needs = {(row["stage"], row["code"]): row["count"] for row in report["needs_g"]}
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not (isinstance(run_id, str) and RUN_ID_RE.fullmatch(run_id)) or not all(
            isinstance(s, str) and isinstance(c, str) and type(n) is int and n >= 0 for (s, c), n in needs.items()):
        return None
    return run_id, needs


def compare(counts, needs):
    """``(mismatches, not_in_ledger)`` between ledger item counts and a report's needs_g."""
    from .run_stages import EXTRACT_NEEDS

    mismatches = []
    for outcome, code, _ in EXTRACT_NEEDS:
        ledger, report = counts.get(outcome, 0), needs.get(("extract", code), 0)
        if ledger != report:
            mismatches.append({"code": outcome, "ledger": ledger, "report": report})
    not_in_ledger = {code: n for (stage, code), n in sorted(needs.items()) if stage == "adoption" and code in ADOPTION}
    return mismatches, not_in_ledger


def build(project, report_path=None, code=None):
    report = load_report(report_path) if report_path else None
    items, run_list = collect(project, report[0] if report else None)
    counts = {}
    for item in items:
        counts[item["code"]] = counts.get(item["code"], 0) + 1
    doc = {"schema": SCHEMA, "counts": counts, "items": [i for i in items if code in (None, i["code"])],
           "report": None, "run_list_used": run_list, "note": "listing only; clears nothing; review-required"}
    if report_path and report is None:
        doc["report"] = {"readable": False}
    elif report:
        mismatches, not_in_ledger = compare(counts, report[1])
        doc["report"] = {"readable": True, "run_id": report[0], "mismatches": mismatches,
                         "not_in_ledger": not_in_ledger}
    return doc


def findings(doc):
    """Fixed finding lines for the report comparison."""
    report = doc["report"]
    if report is None or not report["readable"]:
        return [] if report is None else ["REPORT_UNREADABLE"]
    lines = [f"COUNT_MISMATCH {m['code']} {m['ledger']} {m['report']}" for m in report["mismatches"]]
    return lines + [f"NOT_IN_LEDGER {code} {n} (per-message view: adopt-mail dry run)"
                    for code, n in report["not_in_ledger"].items()]


def _age_days(stamp, now):
    try:
        then = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        then = then if then.tzinfo else then.replace(tzinfo=timezone.utc)
        return str(max(0, (now - then).days))
    except (AttributeError, ValueError):
        return "-"


def render(doc, now=None):
    now = now or datetime.now(timezone.utc)
    lines = ["sha256_prefix code attempts age_days last_run detail_code preserved disposition"]
    for i in doc["items"]:
        lines.append(" ".join(str(v) for v in (i["sha256_prefix"], i["code"], i["attempts"],
                                                _age_days(i["first_run"], now), i["last_run"] or "-",
                                                i["detail_code"] or "-", "yes" if i["preserved"] else "no",
                                                i["disposition"])))
    lines.append("counts " + (" ".join(f"{c}={n}" for c, n in doc["counts"].items()) or "none"))
    return lines + findings(doc) + [doc["note"]]


def main(argv=None):
    parser = argparse.ArgumentParser(prog="records holds", description="Read-only list of held extract documents.")
    parser.add_argument("--project", required=True, help="records project (reads processing-v1/ledger.sqlite)")
    parser.add_argument("--report", help="latest run report: compare counts with its needs_g")
    parser.add_argument("--code", help="list only items with this code (counts stay complete)")
    parser.add_argument("--json", action="store_true", help="print one records-holds-v1 document")
    args = parser.parse_args(argv)
    try:
        doc = build(args.project, args.report, args.code)
    except Unreadable as error:
        print(f"LEDGER_UNREADABLE {error.args[0]}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(doc, indent=1, sort_keys=True))
        for line in findings(doc):
            print(line, file=sys.stderr)
    else:
        print("\n".join(render(doc)))
    return 0
