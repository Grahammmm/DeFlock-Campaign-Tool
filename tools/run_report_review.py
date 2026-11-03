"""Read-only run summary: machine-valid, work outcome and human review owed, kept apart.

``summary REPORT.json`` / ``summary --dir DIR`` print to stdout and write nothing. No verb records,
signs or marks a review; output names the review owed per human gate and is never acceptance.
``summary … --json`` prints the same values as one run-report-review-v1 document; exit codes are unchanged.
``--dir … --since S [--until U]`` keeps invalid reports and valid ones started in [S, U); the rest are counted as excluded.
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
UNMAPPED = "unmapped -> operator"


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


def facts(report, diagnostic, now=None):
    """Return (facts, exit status) for one report; the text and the JSON are both rendered from these facts."""
    run_id = _field(report, "run_id", RUN_ID) or "unknown"
    if diagnostic is not None:  # an invalid report is never summarised as ok
        return {"run_id": run_id, "valid": False, "diagnostic": diagnostic, "status": 1}, 1
    stubbed = {item["stage"] for item in report["needs_g"] if item["code"] == STUB_CODE}
    work = report["stages"][:-1]
    known = {s["name"]: s["status"] in ("ok", "warn") and s["name"] not in stubbed for s in work}
    outcomes = work[4].get("outcomes", [])
    notify, ocr = report["stages"][-1]["status"], report.get("ocr")
    awaiting = coverage_counts(report)["awaiting_ocr"]
    seconds = (now - _parse(report["finished_at"])).total_seconds() if now is not None else 0
    status = 1 if any(s["status"] == "fail" for s in work) or seconds < 0 else 0
    return {"run_id": run_id, "valid": True, "diagnostic": None, "mode": report["mode"],
            "test_only": report["mode"] == "test", "stub": bool(stubbed),
            "work": [{"stage": s["name"], "status": s["status"],
                      "output": s["counts"]["output"] if known[s["name"]] else None} for s in work],
            "outcomes": [{"code": _code(i["code"]), "count": i["count"]} for i in outcomes] if known["extract"] else None,
            "unmapped": sum(i["code"] not in KNOWN_OUTCOMES for i in outcomes) if known["extract"] else None,
            "notify": "failed" if notify == "fail" else notify, "rollup": report["overall_status"],
            "review_required": report["review_required_count"],
            "ocr": {"pages_ocr": ocr["pages_ocr"], "pages_reviewed": ocr["pages_reviewed"],
                    "pages_unreviewed": ocr["pages_ocr"] - ocr["pages_reviewed"]} if ocr else None,
            "awaiting_ocr": None if awaiting is None or "extract" in stubbed else awaiting,
            "owed": [{"code": c, "count": i["count"], "severity": i["severity"], "gate": GATES.get(c, UNMAPPED)}
                     for i in report["needs_g"] for c in (_code(i["code"]),)],
            "age_hours": None if now is None else "future" if seconds < 0 else int(seconds // 3600),
            "status": status}, status


def render(f):
    """Text lines for one facts dict."""
    out = []
    def emit(*parts):
        out.append(" | ".join(("review", f["run_id"]) + parts))
    if not f["valid"]:
        for parts in (("VALID", "no", f["diagnostic"]), ("WORK", "not interpreted"), ("OWED", "not interpreted"), (REVIEWED,)):
            emit(*parts)
        return out
    emit("VALID", "yes")
    emit("MODE", f["mode"], *(["TEST evidence only; not live"] if f["test_only"] else []))
    if f["stub"]:
        emit("STUB", "yes", "output of this run is not real data")
    emit("WORK", " ".join(f"{w['stage']}={w['status']} out={'unknown' if w['output'] is None else w['output']}"
                          for w in f["work"]))
    text = " ".join(f"{o['code']}={o['count']}" for o in f["outcomes"] or []) or "none recorded"
    emit("WORK", "counts", "unknown" if f["outcomes"] is None else f"{text} unmapped={f['unmapped']}")
    only = " (notify only)" if f["notify"] == "failed" and all(w["status"] != "fail" for w in f["work"]) else ""
    emit("NOTIFY", f["notify"], "does not affect WORK", f"rollup={f['rollup']}{only}")
    emit("OWED", f"review_required={f['review_required']} -> {GATE_2}")
    ocr = f["ocr"]
    emit("OWED", *([f"ocr pages_reviewed={ocr['pages_reviewed']}/{ocr['pages_ocr']}",
                    f"pages unreviewed={ocr['pages_unreviewed']}"] if ocr else ["ocr unknown"]))
    emit("OWED", f"AWAITING_OCR={'unknown' if f['awaiting_ocr'] is None else f['awaiting_ocr']}")
    for o in f["owed"]:
        emit("OWED", f"{o['code']} x{o['count']} [{o['severity']}] {o['gate'] if o['gate'] == UNMAPPED else '-> ' + o['gate']}")
    if f["age_hours"] is not None:
        emit("AGE", *(["in the future", "do not trust"] if f["age_hours"] == "future" else [f"{f['age_hours']}h since finished_at"]))
    emit(REVIEWED)
    return out


def list_dir(directory):
    if directory.is_symlink() or not directory.is_dir():
        raise Refused("not a plain directory")
    paths = [path for path in directory.iterdir() if path.name.endswith(".json")]
    if len(paths) > MAX_FILES:
        raise Refused("more than 500 files")
    return [read_file(path) for path in paths]


def summarise_dir(blobs, now, window=None):
    """Return (entries, counts, status); an entry is (duplicate or None, facts or None) in text order.
    With window (since, until or None): invalid reports and valid ones started in [since, until) are kept."""
    loaded = [(data, *load(data)) for data in blobs]
    loaded.sort(key=lambda x: (_field(x[1], "started_at", STAMP) or "", _field(x[1], "run_id", RUN_ID) or "", x[0]))
    total = len(loaded)
    if window is not None:  # before duplicate detection: an excluded copy is never compared
        since, until = window  # fixed-width UTC stamps, already strptime-checked: string order is time order
        loaded = [x for x in loaded if x[2] is not None or since <= x[1]["started_at"] and (
            until is None or x[1]["started_at"] < until)]
    variants = {}
    for data, report, diag in loaded:
        if diag is None:
            variants.setdefault(report["run_id"], set()).add(data)
    entries, status, valid, invalid, dupes, seen = [], 0, 0, 0, 0, set()
    for data, report, diag in loaded:
        run_id = report["run_id"] if diag is None else None
        if run_id is not None and (run_id, data) in seen:
            entries.append(({"run_id": run_id, "kind": "identical"}, None))
            dupes += 1
            continue
        seen.add((run_id, data))
        f, code = facts(report, diag, now)
        differ = run_id is not None and len(variants[run_id]) > 1
        if differ:
            dupes, code = dupes + 1, 1
            f["status"] = code
        entries.append(({"run_id": run_id, "kind": "contents differ"} if differ else None, f))
        valid, invalid = (valid + 1, invalid) if diag is None else (valid, invalid + 1)
        status = max(status, code)
    counts = {"reports": valid + invalid, "valid": valid, "invalid": invalid, "duplicates": dupes}
    return entries, counts if window is None else {**counts, "excluded": total - len(loaded)}, status


def dir_lines(entries, counts, window=None):
    lines = []
    for dup, f in entries:
        if f is None:
            lines.append(f"review | {dup['run_id']} | DUPLICATE | identical")
            continue
        block = render(f)
        if dup is not None:
            block.insert(1, f"review | {dup['run_id']} | DUPLICATE | run_id | contents differ")
        lines += block
    span = "" if window is None else f" excluded={counts['excluded']} window={window[0]}..{window[1] or 'open'}"
    lines.append(f"review | reports={counts['reports']} valid={counts['valid']} invalid={counts['invalid']} "
                 f"duplicates={counts['duplicates']}{span} | machine-valid is not reviewed")
    return lines


def document(entries, counts, window=None):
    return {"schema": "run-report-review-v1", "review_required": True, "reviewed": "none by this tool",
            "reports": [f for _, f in entries if f is not None], "duplicates": [d for d, _ in entries if d is not None],
            "counts": counts, **({} if window is None else {"window": {"since": window[0], "until": window[1]}})}


def _stamp(name, value):  # the shape check passes 2026-02-30 or 25:00:00; strptime rejects them
    if value is not None and not STAMP.match(value):
        raise Refused(f"{name} must be YYYY-MM-DDTHH:MM:SSZ")
    try:
        return _parse(value) if value is not None else None
    except ValueError:
        raise Refused(f"{name} is not a real UTC date and time") from None


def main(argv=None):
    parser = argparse.ArgumentParser(prog="run_report_review.py", description="Read-only run report summary.")
    summary = parser.add_subparsers(dest="verb", required=True).add_parser("summary")
    summary.add_argument("report", nargs="?")
    summary.add_argument("--dir")
    summary.add_argument("--now", help="YYYY-MM-DDTHH:MM:SSZ; adds an AGE line only")
    summary.add_argument("--since", help="with --dir: only valid reports with started_at >= SINCE (invalid always shown)")
    summary.add_argument("--until", help="with --since: only started_at < UNTIL")
    summary.add_argument("--json", action="store_true", help="print one run-report-review-v1 document instead")
    args = parser.parse_args(argv)
    try:
        if (args.report is None) == (args.dir is None):
            raise Refused("give exactly one of REPORT.json or --dir")
        now = _stamp("--now", args.now)
        window = None if args.since is None and args.until is None else (args.since, args.until)
        if window is not None and args.dir is None:
            raise Refused("--since and --until need --dir")
        since, until = _stamp("--since", args.since), _stamp("--until", args.until)
        if window is not None and since is None:
            raise Refused("--until needs --since")
        if until is not None and until <= since:
            raise Refused("--until must be after --since")
        if args.dir is not None:
            entries, counts, status = summarise_dir(list_dir(Path(args.dir)), now, window)
            lines = dir_lines(entries, counts, window)
        else:
            f, status = facts(*load(read_file(Path(args.report))), now)
            entries, counts, lines = [(None, f)], {"reports": 1, "valid": int(f["valid"]),
                                                   "invalid": int(not f["valid"]), "duplicates": 0}, render(f)
    except (Refused, OSError) as exc:
        print(f"review | refused | {exc if isinstance(exc, Refused) else 'unreadable'}", file=sys.stderr)
        return 2
    print(json.dumps(document(entries, counts, window), indent=1, sort_keys=True) if args.json else "\n".join(lines))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
