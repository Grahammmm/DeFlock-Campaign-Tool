"""``records holds``: per-item list of held extract documents, metadata only (HOLD-INSPECT-1).

The listing is read-only: the ledger is opened only through ``run_stages.snapshot_ro``; no evidence payload,
original, unit text, mail or path is read or printed (12-hex hash prefixes and fixed codes only). Counts are
reconciled with the newest run outcome list, or with a run report's needs_g under ``--report``
(``COUNT_MISMATCH`` is a finding). Exit 0 on a readable ledger, 2 with ``LEDGER_UNREADABLE <reason>``.
``records holds release`` (HOLD-RELEASE-1) is the one writer: it resets the attempt counter of retryable
holds, dry run unless ``--apply``. Contract: docs/RECORDS-HOLDS.md.
"""
import argparse
from collections import Counter
import contextlib
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
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
        return parse_report(Path(path).read_bytes())
    except OSError:
        return None


def parse_report(data):
    """``load_report`` over the report's bytes (the release receipt hashes the same bytes it parsed)."""
    try:
        report = json.loads(data.decode("utf-8"))
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


# --- release (HOLD-RELEASE-1): reset the attempt counter of retryable holds; dry run unless --apply ----------

RELEASE_SCHEMA = "records-holds-release-v1"
RELEASABLE = (ne.ERROR, ne.TIMEOUT)  # native_extract.RETRYABLE: the only holds a later attempt can clear
MAX_PER_CALL = 25
REPORT_MAX_AGE, REPORT_SKEW = 24 * 3600, 600  # a release with no run after it retries nothing


class Refused(Exception):
    """``(exit status, fixed code)``; raised before the ledger is changed, except RECEIPT_NOT_PUBLISHED."""


def candidates(snapshot, code):
    """``(chosen, other)``: ``[(subject, [(alert key, count)])]`` for held documents whose every holding
    alert (native_extract's rule, as ``_alert_hold``) classifies as ``code``; ``other`` counts documents
    a different code holds as well (resetting would not release them)."""
    chosen, other = [], 0
    for subject, preserved in ne.pending_subjects(snapshot):
        holding = []
        for row in ne.open_alerts(snapshot, subject) if preserved else ():
            codes = ne.alert_codes(row["key"])
            outcome = ne.classify_codes(codes)
            if not ne.ocr_released(codes) and (outcome not in ne.RETRYABLE or row["count"] >= ne.MAX_ATTEMPTS):
                holding.append((outcome, row["key"], int(row["count"])))
        mine = [(key, count) for outcome, key, count in holding if outcome == code]
        other += bool(mine) and len(mine) < len(holding)
        chosen += [(subject, mine)] if mine and len(mine) == len(holding) else []
    return chosen, other


def _report(path, processing, now):
    """``(run_id, sha256)`` of the newest run's report, no older than ``REPORT_MAX_AGE``."""
    try:
        data = Path(path).read_bytes()
        run_id = parse_report(data)[0]
        stamp = datetime.strptime(run_id[4:19], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
    except (OSError, TypeError, ValueError):
        raise Refused(2, "REPORT_UNREADABLE") from None
    if not -REPORT_SKEW <= (now - stamp).total_seconds() <= REPORT_MAX_AGE:
        raise Refused(2, "REPORT_STALE")
    if latest_run_id(processing) != run_id:
        raise Refused(2, "REPORT_NOT_LATEST")
    return run_id, hashlib.sha256(data).hexdigest()


def _receipt_dir(value, project):
    """An owner-only directory outside the project, created 0700 when absent."""
    path = Path(os.path.abspath(value))
    real, inside = Path(os.path.realpath(path)), Path(os.path.realpath(project))
    if real == inside or inside in real.parents:
        raise Refused(2, "RECEIPT_UNSAFE")
    try:
        if not os.path.lexists(path):
            path.mkdir(mode=0o700)
        info = os.lstat(path)
    except OSError:
        raise Refused(2, "RECEIPT_UNSAFE") from None
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.geteuid():
        raise Refused(2, "RECEIPT_UNSAFE")
    return path


@contextlib.contextmanager
def _locked(processing, runtime_lock):
    """The wrapper's ``run-records.lock`` (required; opened, never created), then the extract stage's root
    lock. The wrapper holds the first across preflight and every stage, the extract stage only the second,
    so the first is what keeps an apply out of an active run; without it the apply fails closed."""
    from .run import RunError, root_lock
    if runtime_lock is None:
        raise Refused(2, "RUNTIME_LOCK_REQUIRED")
    try:
        fd = os.open(runtime_lock, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        raise Refused(2, "RUNTIME_LOCK_UNREADABLE") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Refused(2, "RUNTIME_LOCK_UNREADABLE")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:  # BlockingIOError when held; any other error cannot prove it free
            raise Refused(3, "LEDGER_BUSY") from None
        if not (processing / "ledger.sqlite").is_file():
            raise Refused(2, "LEDGER_UNREADABLE missing")
        try:
            with root_lock(processing):
                yield "held"
        except RunError:
            raise Refused(3, "LEDGER_BUSY") from None
    finally:
        os.close(fd)


def _write_receipt(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())


def _commit(processing, selected, code, directory, report_sha, now):
    """Receipt staged and fsynced before the single ledger transaction; published by rename after commit."""
    from .ledger import store
    lines = "".join(json.dumps({"schema": RELEASE_SCHEMA, "sha256_prefix": subject[:12], "code": code,
                                "attempts_before": max(n for _, n in alerts), "released_at": now.isoformat(),
                                "run_report_sha256": report_sha}, sort_keys=True) + "\n" for subject, alerts in selected)
    name = f"holds-release-{now:%Y%m%dT%H%M%SZ}-{os.urandom(4).hex()}.jsonl"
    staged = directory / ("." + name + ".tmp")
    try:
        _write_receipt(staged, lines)
    except OSError:
        raise Refused(2, "RECEIPT_UNSAFE") from None
    try:
        with store.ledger(processing / "ledger.sqlite") as con:
            con.execute("BEGIN IMMEDIATE")
            for key, count in (alert for _, alerts in selected for alert in alerts):
                # Only the attempt counter: the alert stays open under its key, so a new failure counts again.
                if con.execute("UPDATE alerts SET count=0 WHERE key=? AND state='open' AND count=?",
                               (key, count)).rowcount != 1:
                    raise Refused(3, "LEDGER_CHANGED")
            con.commit()
    except BaseException as error:
        os.unlink(staged)
        if isinstance(error, (Refused, KeyboardInterrupt, SystemExit)):
            raise
        busy = isinstance(error, sqlite3.OperationalError) and "locked" in str(error)
        raise Refused(3 if busy else 1, "LEDGER_BUSY" if busy else "RELEASE_FAILED") from None
    try:
        os.replace(staged, directory / name)
    except OSError:  # committed: the staged file is the receipt
        raise Refused(1, "RECEIPT_NOT_PUBLISHED ." + name + ".tmp") from None
    return name


def release(project, code, *, apply=False, limit=None, receipt=None, report=None, runtime_lock=None, now=None):
    """The ``records-holds-release-v1`` document. Never deletes, moves or re-extracts anything."""
    from .run_stages import LedgerBusy, snapshot_ro
    if code in (ne.PRESERVE_PENDING, "EXTRACT_PRESERVE_PENDING"):
        raise Refused(2, "CODE_CUSTODIAN")
    if code not in RELEASABLE:
        raise Refused(2, "CODE_NOT_RETRYABLE")
    if limit is not None and not 1 <= limit <= MAX_PER_CALL:
        raise Refused(2, "MAX_OUT_OF_RANGE")
    processing, now = Path(project) / "processing-v1", now or datetime.now(timezone.utc)
    doc = {"schema": RELEASE_SCHEMA, "mode": "apply" if apply else "dry_run", "code": code, "released": 0,
           "receipt_file": None, "run_id": None, "runtime_lock": "not_checked"}
    for value, missing in ((limit, "MAX_REQUIRED"), (receipt, "RECEIPT_REQUIRED"), (report, "REPORT_REQUIRED"),
                           (runtime_lock, "RUNTIME_LOCK_REQUIRED")):
        if apply and value is None:
            raise Refused(2, missing)
    # Both locks are held from the report check through the receipt's publication: no run starts in between.
    with _locked(processing, runtime_lock) if apply else contextlib.nullcontext("not_checked") as lock:
        if apply:
            doc["run_id"], report_sha = _report(report, processing, now)
            directory = _receipt_dir(receipt, project)
        try:
            with snapshot_ro(processing / "ledger.sqlite") as db:
                db.row_factory = sqlite3.Row
                chosen, other = candidates(_Snapshot(db), code)
        except LedgerBusy:
            raise Refused(3, "LEDGER_BUSY") from None
        except (sqlite3.Error, OSError, ValueError, KeyError, TypeError) as error:
            raise Refused(2, "LEDGER_UNREADABLE " + ("missing" if "database_missing" in str(error) else "error")) from None
        selected = chosen[:limit] if limit else chosen
        doc.update(eligible=len(chosen), held_by_other_code=other, runtime_lock=lock,
                   items=[{"sha256_prefix": s[:12], "attempts": max(n for _, n in a)} for s, a in selected])
        if apply and selected:
            doc["receipt_file"] = _commit(processing, selected, code, directory, report_sha, now)
            doc["released"] = len(selected)
    return doc


def release_main(argv):
    parser = argparse.ArgumentParser(prog="records holds release", description=(
        "Reset the attempt counter of held EXTRACT_ERROR or EXTRACT_TIMEOUT documents so the next run retries "
        "them. Dry run unless --apply. Never deletes, moves or re-extracts."))
    parser.add_argument("--project", required=True, help="records project (processing-v1/ledger.sqlite)")
    parser.add_argument("--code", required=True, help="EXTRACT_ERROR or EXTRACT_TIMEOUT")
    parser.add_argument("--apply", action="store_true",
                        help="write; needs --max, --receipt, --report and --runtime-lock")
    parser.add_argument("--max", type=int, help=f"release at most N documents (1..{MAX_PER_CALL})")
    parser.add_argument("--receipt", help="owner-only receipt directory outside the project")
    parser.add_argument("--report", help="the newest run's run report, younger than 24 h")
    parser.add_argument("--runtime-lock", help="the wrapper's RUNTIME_DIR/run-records.lock (required with "
                        "--apply; held from the report check to the receipt)")
    parser.add_argument("--json", action="store_true", help="print one records-holds-release-v1 document")
    args = parser.parse_args(argv)
    try:
        doc = release(args.project, args.code, apply=args.apply, limit=args.max, receipt=args.receipt,
                      report=args.report, runtime_lock=args.runtime_lock)
    except Refused as error:
        print(error.args[1], file=sys.stderr)
        return error.args[0]
    lines = ["sha256_prefix code attempts"] + [f"{i['sha256_prefix']} {args.code} {i['attempts']}" for i in doc["items"]]
    lines.append(f"eligible {doc['eligible']} selected {len(doc['items'])} held_by_other_code {doc['held_by_other_code']}")
    lines.append(f"RELEASED {doc['released']} receipt {doc['receipt_file']} run {doc['run_id']} runtime_lock "
                 f"{doc['runtime_lock']}; the next run retries them" if args.apply else
                 "DRY_RUN nothing changed (apply: --apply --max N --receipt DIR --report RUN-REPORT "
                 "--runtime-lock RUNTIME_DIR/run-records.lock)")
    print(json.dumps(doc, indent=1, sort_keys=True) if args.json else "\n".join(lines))
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["release"]:
        return release_main(argv[1:])
    parser = argparse.ArgumentParser(prog="records holds", description=(
        "Read-only list of held extract documents. `records holds release --help`: reset retryable holds."))
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
