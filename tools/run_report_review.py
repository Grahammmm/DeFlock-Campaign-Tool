"""Read-only run summary: machine-valid, work outcome and human review owed, kept apart.

``summary REPORT.json`` / ``summary --dir DIR`` print to stdout and write nothing. No verb records,
signs or marks a review; output names the review owed per human gate and is never acceptance.
Exit 1: invalid report, failed work stage, finished after --now, or a run_id with differing
contents. Exit 2: refused input. Otherwise 0."""
import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from campaign_tool.records.run_coverage import STUB_CODE, coverage_counts  # noqa: E402
from campaign_tool.records.run_report import validate_run_report  # noqa: E402
from tools.run_report_check import KNOWN_OUTCOMES  # noqa: E402

MAX_BYTES, MAX_FILES = 1024 * 1024, 500
RUN_ID = re.compile(r"^run_[0-9]{8}T[0-9]{6}Z_[0-9a-f]{8}$")
STAMP = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
BANNED = re.compile(r"\b(late|overdue)\b|violat|unlawful|illegal|non-?complian|breach", re.I)
GATE_2, GATE_3 = "gate 2 approve what gets published", "gate 3 approve the law and any legal claim"
# Literal gate map. No send-side needs-G code exists on main today, so nothing maps to "gate 1 approve
# what goes out". A code not listed prints "unmapped -> operator": never dropped, never read as reviewed.
GATES = {"GATE_REVIEW_REQUIRED": GATE_2, "LEGAL_REVIEW_REQUIRED": GATE_3, **dict.fromkeys(f"""{STUB_CODE}
    AWAITING_OCR ADOPTION_CODED_GAPS ADOPTION_IDENTITY_CONFLICT ADOPTION_NOT_CONFIGURED ADOPTION_RECEIPT_GAPS
    ADOPTION_UNACCEPTED_GAP EXTRACT_ROOT_LOCKED GATE_BLOCKED LEDGER_WAL_PENDING OCR_BACKEND_TRANSIENT OCR_BACKLOG
    OCR_CACHE_DAMAGED OCR_DOCUMENT_FAILED OCR_PAGES_UNRESOLVED OCR_QUEUE_ITEM_INVALID OCR_SOURCE_INVALID
    OCR_VISION_PAGES PORTAL_FETCH_BLOCKED PORTAL_FETCH_DRY_RUN_ONLY PORTAL_HOST_NOT_ALLOWLISTED PORTAL_HTTP_404
    PORTAL_DOWNLOAD_PENDING PORTAL_LEDGER_PENDING PORTAL_NOTICE_UNREADABLE PORTAL_PRESERVE_REVIEW_REQUIRED
    PORTAL_RETRY_EXHAUSTED SYNC_OFFLINE_SNAPSHOT RUN_INTERRUPTED RUN_DEADLINE_REACHED STAGE_TIMEOUT
    STAGE_SPAWN_FAILED STAGE_RESULT_INVALID STAGE_LEFT_PROCESSES UPSTREAM_FAILED""".split(), "operator (no gate)")}
REVIEWED = ("REVIEWED | none by this tool | machine-valid is review-required, not semantic "
            "acceptance; extracted and OCR text stay unreviewed; not a legal status")


class Refused(Exception):  # the reason never echoes a path
    pass


def _code(value):  # codes are schema-checked; a code carrying banned wording is withheld
    return "WITHHELD_CODE" if BANNED.search(value) else value


def _field(report, key, pattern):
    value = report.get(key) if isinstance(report, dict) else None
    return value if isinstance(value, str) and pattern.match(value) else None


def _parse(stamp):
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _diagnostic(exc):
    """Keep the JSON location; keep the message tail only when it is plain fixed wording."""
    match = re.match(r"^(\$[A-Za-z0-9_.\[\]]*)(?:: (.*))?$", str(exc))
    if not match:
        return "cross-field check failed"
    tail = match.group(2) or ""
    return match.group(1) + (f": {tail}" if re.match(r"^[a-z][a-z /_]{0,80}$", tail) else "")


def read_file(path):
    if path.is_symlink() or not path.is_file():
        raise Refused("symlink or not a regular file")
    if path.stat().st_size > MAX_BYTES:
        raise Refused("file over 1 MiB")
    return path.read_bytes()


def load(data):
    """Return (report, diagnostic); diagnostic is None only for a machine-valid report."""
    try:
        report = json.loads(data.decode("utf-8"))
    except ValueError:
        return None, "unreadable JSON"
    try:
        validate_run_report(report)
    except (ValueError, TypeError, KeyError, AttributeError, IndexError) as exc:
        return report, _diagnostic(exc)
    return report, None


def summarise(report, diagnostic, now=None):
    """Return (lines, exit status) for one report."""
    run_id = _field(report, "run_id", RUN_ID) or "unknown"
    out = []
    def emit(*parts):
        out.append(" | ".join(("review", run_id) + parts))
    if diagnostic is not None:  # an invalid report is never summarised as ok
        for parts in (("VALID", "no", diagnostic), ("WORK", "not interpreted"), ("OWED", "not interpreted"), (REVIEWED,)):
            emit(*parts)
        return out, 1
    emit("VALID", "yes")
    emit("MODE", report["mode"], *(["TEST evidence only; not live"] if report["mode"] == "test" else []))
    stubbed = {item["stage"] for item in report["needs_g"] if item["code"] == STUB_CODE}
    if stubbed:
        emit("STUB", "yes", "output of this run is not real data")
    work = report["stages"][:-1]
    known = {s["name"]: s["status"] in ("ok", "warn") and s["name"] not in stubbed for s in work}
    emit("WORK", " ".join(f"{s['name']}={s['status']} out={s['counts']['output'] if known[s['name']] else 'unknown'}"
                          for s in work))
    outcomes = work[4].get("outcomes", [])
    unmapped = sum(1 for item in outcomes if item["code"] not in KNOWN_OUTCOMES)
    text = " ".join(f"{_code(item['code'])}={item['count']}" for item in outcomes) or "none recorded"
    emit("WORK", "counts", f"{text} unmapped={unmapped}" if known["extract"] else "unknown")
    work_failed = any(stage["status"] == "fail" for stage in work)
    notify = report["stages"][-1]["status"]
    only = " (notify only)" if notify == "fail" and not work_failed else ""
    emit("NOTIFY", "failed" if notify == "fail" else notify, "does not affect WORK",
         f"rollup={report['overall_status']}{only}")
    emit("OWED", f"review_required={report['review_required_count']} -> {GATE_2}")
    ocr = report.get("ocr")
    emit("OWED", *([f"ocr pages_reviewed={ocr['pages_reviewed']}/{ocr['pages_ocr']}",
                    f"pages unreviewed={ocr['pages_ocr'] - ocr['pages_reviewed']}"] if ocr else ["ocr unknown"]))
    awaiting = coverage_counts(report)["awaiting_ocr"]
    emit("OWED", f"AWAITING_OCR={'unknown' if awaiting is None or 'extract' in stubbed else awaiting}")
    for item in report["needs_g"]:
        code = _code(item["code"])
        gate = f"-> {GATES[code]}" if code in GATES else "unmapped -> operator"
        emit("OWED", f"{code} x{item['count']} [{item['severity']}] {gate}")
    seconds = (now - _parse(report["finished_at"])).total_seconds() if now is not None else 0
    future = seconds < 0
    if now is not None:
        emit("AGE", *(["in the future", "do not trust"] if future else [f"{int(seconds // 3600)}h since finished_at"]))
    emit(REVIEWED)
    return out, 1 if work_failed or future else 0


def list_dir(directory):
    if directory.is_symlink() or not directory.is_dir():
        raise Refused("not a plain directory")
    paths = [path for path in directory.iterdir() if path.name.endswith(".json")]
    if len(paths) > MAX_FILES:
        raise Refused("more than 500 files")
    return [read_file(path) for path in paths]


def summarise_dir(blobs, now):
    loaded = [(data, *load(data)) for data in blobs]
    loaded.sort(key=lambda x: (_field(x[1], "started_at", STAMP) or "", _field(x[1], "run_id", RUN_ID) or "", x[0]))
    variants = {}
    for data, report, diag in loaded:
        if diag is None:
            variants.setdefault(report["run_id"], set()).add(data)
    lines, status, valid, invalid, dupes, seen = [], 0, 0, 0, 0, set()
    for data, report, diag in loaded:
        run_id = report["run_id"] if diag is None else None
        if run_id is not None and (run_id, data) in seen:
            lines.append(f"review | {run_id} | DUPLICATE | identical")
            dupes += 1
            continue
        seen.add((run_id, data))
        block, code = summarise(report, diag, now)
        if run_id is not None and len(variants[run_id]) > 1:
            block.insert(1, f"review | {run_id} | DUPLICATE | run_id | contents differ")
            dupes, code = dupes + 1, 1
        lines += block
        valid, invalid = (valid + 1, invalid) if diag is None else (valid, invalid + 1)
        status = max(status, code)
    lines.append(f"review | reports={valid + invalid} valid={valid} invalid={invalid} "
                 f"duplicates={dupes} | machine-valid is not reviewed")
    return lines, status


def main(argv=None):
    parser = argparse.ArgumentParser(prog="run_report_review.py", description="Read-only run report summary.")
    summary = parser.add_subparsers(dest="verb", required=True).add_parser("summary")
    summary.add_argument("report", nargs="?")
    summary.add_argument("--dir")
    summary.add_argument("--now", help="YYYY-MM-DDTHH:MM:SSZ; adds an AGE line only")
    args = parser.parse_args(argv)
    try:
        if (args.report is None) == (args.dir is None):
            raise Refused("give exactly one of REPORT.json or --dir")
        if args.now is not None and not STAMP.match(args.now):
            raise Refused("--now must be YYYY-MM-DDTHH:MM:SSZ")
        try:  # the shape check passes 2026-02-30 or 25:00:00; strptime rejects them
            now = _parse(args.now) if args.now is not None else None
        except ValueError:
            raise Refused("--now is not a real UTC date and time") from None
        lines, status = (summarise_dir(list_dir(Path(args.dir)), now) if args.dir is not None
                         else summarise(*load(read_file(Path(args.report))), now))
    except (Refused, OSError) as exc:
        print(f"review | refused | {exc if isinstance(exc, Refused) else 'unreadable'}", file=sys.stderr)
        return 2
    print("\n".join(lines))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
