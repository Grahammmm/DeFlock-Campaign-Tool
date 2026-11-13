"""``records holds``: per-item list of held extract documents, metadata only (HOLD-INSPECT-1).

Read-only: the ledger is opened only through ``run_stages.snapshot_ro``; no evidence payload, original,
unit text, mail or path is read or printed (12-hex hash prefixes and fixed codes only). Counts are reconciled
with the newest run outcome list, or with a run report's needs_g under ``--report`` (``COUNT_MISMATCH`` is a
finding). Exit 0 on a readable ledger, 2 with ``LEDGER_UNREADABLE <reason>``. Contract: docs/RECORDS-HOLDS.md.
"""
import argparse
from collections import Counter
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


def latest_run_id(processing):
    """Newest run ID with an extract outcome list in ``processing-v1/runs`` (file names only), or None."""
    try:
        names = [p.name[len("extract-"):-len(".json")] for p in (processing / "runs").glob("extract-run_*.json")]
    except OSError:
        return None
    return max((name for name in names if RUN_ID_RE.fullmatch(name)), default=None)


def collect(processing, listed):
    """Every held extract document, coded with the run outcome list ``listed`` when given; raises ``Unreadable``."""
    from .run_stages import LedgerBusy, snapshot_ro
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
                        item.update(code=outcome, detail_code=ne.held_refusal_detail(row["key"])["refusal_code"]
                                    if outcome == ne.NOT_ACCEPTED else
                                    ",".join(sorted({_code(code) for code in ne.alert_codes(row["key"])})))
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
    return sorted(items, key=lambda i: (order.get(i["code"], 99), i["sha256_prefix"]))


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


def compare(counts, needs, extract_needs):
    """``(mismatches, not_in_ledger)`` between ledger item counts and needs_g counts."""
    mismatches = [{"code": o, "ledger": counts.get(o, 0), "report": needs.get(("extract", c), 0)}
                  for o, c, _ in extract_needs if counts.get(o, 0) != needs.get(("extract", c), 0)]
    return mismatches, {c: n for (s, c), n in sorted(needs.items()) if s == "adoption" and c in ADOPTION}


def build(project, report_path=None, code=None):
    """The document; baseline: ``--report``'s needs_g, else the newest run outcome list (its needs_g source)."""
    from .run_stages import EXTRACT_NEEDS
    processing = Path(project) / "processing-v1"
    report = load_report(report_path) if report_path else None
    run_id = report[0] if report else latest_run_id(processing)
    listed = _run_list(processing, run_id) if run_id else None
    items = collect(processing, listed)
    counts = dict(Counter(item["code"] for item in items))
    tally = Counter(outcome for outcome, _ in (listed or {}).values())
    needs = report[1] if report else None if listed is None else {("extract", c): tally[o] for o, c, _ in EXTRACT_NEEDS}
    source = "report" if report else "run_list" if needs is not None else None
    mismatches, gaps = compare(counts, needs, EXTRACT_NEEDS) if needs is not None else ([], {})
    baseline = dict(source=source, run_id=run_id if source else None, mismatches=mismatches, not_in_ledger=gaps,
                    report_file="none" if not report_path else "read" if report else "unreadable")
    return {"schema": SCHEMA, "counts": counts, "items": [i for i in items if code in (None, i["code"])],
            "report": baseline, "run_list_used": listed is not None,
            "note": "listing only; clears nothing; review-required"}


def findings(doc):
    """Fixed finding lines; a missing baseline is printed, never a silent zero."""
    report, lines = doc["report"], []
    lines += ["REPORT_UNREADABLE"] if report["report_file"] == "unreadable" else []
    lines += [] if doc["run_list_used"] else ["RUN_LIST_MISSING (unalerted pending documents are not coded)"]
    lines += [] if report["source"] else ["NOT_RECONCILED (no run report and no run outcome list)"]
    lines += [f"COUNT_MISMATCH {m['code']} {m['ledger']} {m['report']}" for m in report["mismatches"]]
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
        lines.append(" ".join(str(v) for v in (
            i["sha256_prefix"], i["code"], i["attempts"], _age_days(i["first_run"], now), i["last_run"] or "-",
            i["detail_code"] or "-", "yes" if i["preserved"] else "no", i["disposition"])))
    lines.append("counts " + (" ".join(f"{c}={n}" for c, n in doc["counts"].items()) or "none"))
    return lines + findings(doc) + [doc["note"]]


def main(argv=None):
    parser = argparse.ArgumentParser(prog="records holds", description="Read-only list of held extract documents.")
    parser.add_argument("--project", required=True, help="records project (reads processing-v1/ledger.sqlite)")
    parser.add_argument("--report", help="run report: compare with its needs_g (default: newest run outcome list)")
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
