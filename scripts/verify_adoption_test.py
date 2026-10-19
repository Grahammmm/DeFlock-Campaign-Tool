#!/usr/bin/env python3
"""Verify `records adopt-mail` on a frozen TEST fixture; emit a numbers-only receipt.

The local-only acceptance worker runs this, and only against a TEST copy. It
automates the documented acceptance runbook (docs/RECORDS-MAIL-ADOPTION.md)
around the reviewed engine command and decides no policy itself:

  0. guards: the fixture directory name must end in ``-TEST``. The processing
     root and export are fixed children of it (``processing-v1``,
     ``mail-history``) and cannot be overridden. No symlinks are allowed. The
     effective uid must own the ledger, so no root-owned files appear in the
     copy. The config must already exist, be owner-only, and not be a symlink.
     The receipt must be written outside the fixture.
  1. read-only baseline: the ``mail_messages`` and ``runs`` counts, read with
     an immutable SQLite URI, and only when the WAL is empty; plus digests of
     the whole fixture tree and of the export tree.
  2. ``adopt-mail`` dry run. It must exit 0 and leave the fixture tree
     unchanged. Every runbook precondition must hold: inventory equal to the
     expected totals (154 messages / 82 attachments by default), no gaps,
     conflicts, incomplete or predicted binding conflicts, an open gate,
     already_adopted + new = total, and ledger rows equal to already_adopted.
     If any check fails, the script stops here and never applies.
  3. ``adopt-mail --apply``. It must exit 0 with no failures, adopt exactly the
     new messages, and leave ``mail_messages`` equal to the expected total. A
     run row is added only if there was something to adopt.
  4. ``adopt-mail --apply`` again. It must adopt 0, start no run, and leave the
     tree byte-identical.
  5. the export tree and the disable marker are unchanged throughout.

The receipt (``--receipt``, also printed to stdout) holds only integers,
booleans and fixed-vocabulary codes: check names, gate state, and safe error
codes matching ``[a-z][a-z0-9_:]*``. It has no paths, headers, subjects,
addresses, hashes of records or other record text. The engine's own output is
parsed in memory and never echoed. Its stderr is discarded; the receipt only
records whether stderr was empty.

The configuration is never written or changed by this script. Enabling or
disabling adoption stays the operator's documented, G-approved step. Exit
codes: 0 all checks passed; 1 a check failed (receipt says where it stopped);
2 refused before anything was run.

Standard library only (Python >= 3.11), so it runs inside the Flock container
from an exported engine checkout:

    python3 -B <engine>/scripts/verify_adoption_test.py \
        --test-root <frozen copy whose name ends in -TEST> \
        --config <owner-only mail-history-adoption-v1 config> \
        --receipt <new file outside the fixture> [--dry-run-only]
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import sys

SCHEMA = "flock-adoption-test-verification-v1"
PROCESSING_DIR = "processing-v1"
EXPORT_DIR = "mail-history"
MARKER = ".adoption-disabled-v1.json"
RUN_PREFIX = "mail-history-"
SAFE_CODE = re.compile(r"[a-z][a-z0-9_:]{0,95}")
GATE_STATES = frozenset({"disabled_no_config", "disabled_by_config", "disabled_by_marker",
                         "marker_binding_mismatch", "disable_marker_unreadable", "enabled",
                         "enabled_marker_superseded_by_config"})
REPORT_INTS = ("receipts", "attachments", "already_adopted", "new_messages", "new_attachments",
               "eligible_new_messages", "incomplete_messages", "pending_messages", "pending_attachments",
               "unfinalized_runs", "adopted_messages", "adopted_attachments", "resumed_messages")
REPORT_MAPS = ("identity_conflicts", "predicted_binding_conflicts", "gaps", "failures", "recovered_runs")
AFTER_INTS = ("already_adopted", "new_messages", "incomplete_messages", "pending_messages", "unfinalized_runs")


class Refused(Exception):
    """Guard failure before any engine invocation; the message is a safe code."""


def safe_code(value):
    value = str(value)
    return value if SAFE_CODE.fullmatch(value) else "unrecognized_code"


def safe_counts(value):
    if not isinstance(value, dict):
        return {"unrecognized_shape": 1}
    out = {}
    for key, count in value.items():
        code = safe_code(key)
        out[code] = out.get(code, 0) + (count if isinstance(count, int) and not isinstance(count, bool) else 1)
    return dict(sorted(out.items()))


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def tree_digest(top):
    """Digest of every entry's path, type, mode, size, mtime and content (atime excluded).

    Only the digest and counts leave this function; paths never do.
    """
    digest = hashlib.sha256()
    files = entries = 0
    top = str(top)
    for base, dirs, names in os.walk(top, followlinks=False):
        dirs.sort()
        for name in sorted(dirs + names):
            path = os.path.join(base, name)
            info = os.lstat(path)
            rel = os.path.relpath(path, top)
            entries += 1
            if stat.S_ISLNK(info.st_mode):
                content = "link:" + hashlib.sha256(os.fsencode(os.readlink(path))).hexdigest()
            elif stat.S_ISREG(info.st_mode):
                files += 1
                content = file_sha256(path)
            else:
                content = "-"
            digest.update(json.dumps([rel, stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode),
                                      info.st_size if not stat.S_ISDIR(info.st_mode) else 0,
                                      info.st_mtime_ns, content]).encode() + b"\n")
    return {"sha256": digest.hexdigest(), "files": files, "entries": entries}


def marker_state(root):
    path = root / MARKER
    if not os.path.lexists(path):
        return "absent"
    if path.is_symlink() or not path.is_file():
        return "unsafe"
    return file_sha256(path)


def _wal_quiet(ledger):
    wal = Path(str(ledger) + "-wal")
    journal = Path(str(ledger) + "-journal")
    return not (wal.exists() and wal.stat().st_size) and not journal.exists()


def ledger_counts(ledger):
    """Read-only counts via an immutable URI; refuses (None) unless the WAL is empty."""
    if not _wal_quiet(ledger):
        return None
    con = sqlite3.connect("file:%s?mode=ro&immutable=1" % ledger, uri=True)
    try:
        one = lambda sql: con.execute(sql).fetchone()[0]
        by_status = {safe_code(status): count for status, count in con.execute(
            "SELECT status, count(*) FROM runs WHERE run_id LIKE ? GROUP BY status", (RUN_PREFIX + "%",))}
        return {
            "mail_messages": one("SELECT count(*) FROM mail_messages"),
            "runs": one("SELECT count(*) FROM runs"),
            "mail_history_runs_by_status": dict(sorted(by_status.items())),
            "mail_history_runs_open": one("SELECT count(*) FROM runs WHERE run_id LIKE 'mail-history-%' "
                                          "AND (status='running' OR ended_at IS NULL)"),
            "mail_export_preserve_not_done": one(
                "SELECT count(*) FROM stage_state WHERE stage='preserve' AND status!='done' AND original_sha256 IN "
                "(SELECT original_sha256 FROM occurrences WHERE acquisition_method='verified_mail_export')"),
            "quick_check_ok": one("PRAGMA quick_check") == "ok",
            "foreign_key_violations": len(con.execute("PRAGMA foreign_key_check").fetchall()),
        }
    finally:
        con.close()


def sanitize_report(report):
    """Keep only integers, booleans and safe codes from an adopt-mail report."""
    if not isinstance(report, dict):
        return {"parsed": False}
    if report.get("status") == "refused":
        return {"parsed": True, "refused": True, "code": safe_code(report.get("code"))}
    out = {"parsed": True, "refused": False,
           "mode_apply": report.get("mode") == "apply",
           "source_mail_history": report.get("source") == "mail-history",
           "started_run": "run_id" in report,
           "ledger_wal_recovered": bool(report.get("ledger_wal_recovered"))}
    for key in REPORT_INTS:
        value = report.get(key)
        out[key] = value if isinstance(value, int) and not isinstance(value, bool) else None
    for key in REPORT_MAPS:
        if key in report:
            out[key] = safe_counts(report[key])
    gate = report.get("gate") if isinstance(report.get("gate"), dict) else {}
    out["gate_state"] = gate.get("state") if gate.get("state") in GATE_STATES else "unrecognized_code"
    out["gate_open"] = gate.get("open") is True
    out["gate_marker_present"] = gate.get("marker_present") is True
    after = report.get("after")
    if isinstance(after, dict):
        out["after"] = {}
        for key in AFTER_INTS:
            value = after.get(key)
            out["after"][key] = value if isinstance(value, int) and not isinstance(value, bool) else None
        out["after"]["identity_conflicts"] = safe_counts(after.get("identity_conflicts", {}))
    return out


def run_adopt(engine, root, export, config, *, apply, timeout):
    command = [sys.executable, "-B", "-m", "campaign_tool.records", "adopt-mail",
               "--root", str(root), "--mail-history", str(export), "--config", str(config)]
    if apply:
        command.append("--apply")
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8",
           "PYTHONPATH": str(engine), "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        done = subprocess.run(command, cwd=str(engine), env=env, stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return {"exit": None, "timed_out": True, "stderr_empty": None, "report": {"parsed": False}}
    try:
        parsed = json.loads(done.stdout.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        parsed = None
    return {"exit": done.returncode, "timed_out": False, "stderr_empty": not done.stderr.strip(),
            "report": sanitize_report(parsed)}


def guard(args, *, euid=None):
    """Resolve and validate inputs; raise Refused(code). Touches nothing."""
    euid = os.geteuid() if euid is None else euid
    raw = Path(os.path.abspath(args.test_root))
    if not raw.name.endswith("-TEST"):
        raise Refused("test_root_not_test")
    if raw.is_symlink() or raw.resolve() != raw or not raw.is_dir():
        raise Refused("test_root_unsafe")
    root, export = raw / PROCESSING_DIR, raw / EXPORT_DIR
    for path in (root, export):
        if path.is_symlink() or not path.is_dir():
            raise Refused("fixture_layout_unexpected")
    ledger = root / "ledger.sqlite"
    if ledger.is_symlink() or not ledger.is_file():
        raise Refused("ledger_missing")
    if ledger.stat().st_uid != euid:
        raise Refused("uid_not_ledger_owner")
    config = Path(os.path.abspath(args.config))
    if config.is_symlink() or not config.is_file():
        raise Refused("config_missing")
    info = config.stat()
    if info.st_uid != euid or stat.S_IMODE(info.st_mode) & 0o077:
        raise Refused("config_not_owner_only")
    receipt = Path(os.path.abspath(args.receipt))
    if os.path.commonpath((str(receipt), str(raw))) == str(raw) or \
            os.path.commonpath((str(receipt.resolve()), str(raw))) == str(raw):
        raise Refused("receipt_inside_test_root")
    if os.path.lexists(receipt):
        raise Refused("receipt_exists")
    if not receipt.parent.is_dir():
        raise Refused("receipt_dir_missing")
    engine = Path(os.path.abspath(args.engine)) if args.engine else Path(__file__).resolve().parents[1]
    module = engine / "campaign_tool" / "records" / "mail_adoption.py"
    if not module.is_file():
        raise Refused("engine_missing_adopt_mail")
    if os.path.commonpath((str(engine.resolve()), str(raw))) == str(raw):
        raise Refused("engine_inside_test_root")
    if args.expect_messages < 0 or args.expect_attachments < 0:
        raise Refused("expected_counts_invalid")
    return {"raw": raw, "root": root, "export": export, "ledger": ledger, "config": config,
            "receipt": receipt, "engine": engine, "module": module}


def _check(checks, name, ok):
    checks[name] = bool(ok)
    return bool(ok)


def dry_run_checks(checks, dry, baseline, tree_same, total, attachments):
    rep = dry["report"]
    _check(checks, "dry_exit_0", dry["exit"] == 0)
    _check(checks, "dry_parsed", rep.get("parsed") and not rep.get("refused"))
    _check(checks, "dry_tree_unchanged", tree_same)
    _check(checks, "dry_inventory_messages", rep.get("receipts") == total)
    _check(checks, "dry_inventory_attachments", rep.get("attachments") == attachments)
    for key in ("gaps", "identity_conflicts", "predicted_binding_conflicts"):
        _check(checks, "dry_no_" + key, rep.get(key) == {})
    _check(checks, "dry_no_incomplete", rep.get("incomplete_messages") == 0)
    _check(checks, "dry_no_unfinalized_runs", rep.get("unfinalized_runs") == 0)
    _check(checks, "dry_gate_open", rep.get("gate_open"))
    a0, n0 = rep.get("already_adopted"), rep.get("new_messages")
    _check(checks, "dry_adopted_plus_new_is_total", isinstance(a0, int) and isinstance(n0, int) and a0 + n0 == total)
    _check(checks, "dry_eligible_equals_new", rep.get("eligible_new_messages") == n0)
    _check(checks, "dry_pending_equals_new", rep.get("pending_messages") == n0)
    _check(checks, "baseline_ledger_readable", baseline is not None)
    _check(checks, "baseline_rows_equal_adopted", baseline is not None and baseline["mail_messages"] == a0)
    _check(checks, "baseline_no_open_runs", baseline is not None and baseline["mail_history_runs_open"] == 0)


def after_clean(rep, total):
    after = rep.get("after") or {}
    return (after.get("already_adopted") == total and after.get("new_messages") == 0
            and after.get("incomplete_messages") == 0 and after.get("pending_messages") == 0
            and after.get("unfinalized_runs") == 0 and after.get("identity_conflicts") == {})


def apply_checks(checks, app, n0, baseline, counts, total):
    rep = app["report"]
    _check(checks, "apply_exit_0", app["exit"] == 0)
    _check(checks, "apply_parsed", rep.get("parsed") and not rep.get("refused") and rep.get("mode_apply"))
    _check(checks, "apply_no_failures", rep.get("failures") == {})
    _check(checks, "apply_adopted_equals_new", rep.get("adopted_messages") == n0)
    _check(checks, "apply_resumed_zero", rep.get("resumed_messages") == 0)
    _check(checks, "apply_no_recovery", "recovered_runs" not in rep and not rep.get("ledger_wal_recovered"))
    _check(checks, "apply_after_clean", after_clean(rep, total))
    _check(checks, "apply_run_started_iff_new", rep.get("started_run") == (n0 > 0))
    _check(checks, "apply_ledger_readable", counts is not None)
    if counts is None or baseline is None:
        return
    _check(checks, "apply_mail_messages_is_total", counts["mail_messages"] == total)
    _check(checks, "apply_mail_messages_is_baseline_plus_new", counts["mail_messages"] == baseline["mail_messages"] + n0)
    _check(checks, "apply_runs_delta", counts["runs"] == baseline["runs"] + (1 if n0 > 0 else 0))
    expected = dict(baseline["mail_history_runs_by_status"])
    if n0 > 0:
        expected["completed"] = expected.get("completed", 0) + 1
    _check(checks, "apply_mail_history_run_statuses", counts["mail_history_runs_by_status"] == expected)
    _check(checks, "apply_no_open_runs", counts["mail_history_runs_open"] == 0)
    _check(checks, "apply_preserve_all_done", counts["mail_export_preserve_not_done"] == 0)
    _check(checks, "apply_quick_check_ok", counts["quick_check_ok"])
    _check(checks, "apply_no_foreign_key_violations", counts["foreign_key_violations"] == 0)


def rerun_checks(checks, again, counts_before, counts, tree_same, total):
    rep = again["report"]
    _check(checks, "rerun_exit_0", again["exit"] == 0)
    _check(checks, "rerun_parsed", rep.get("parsed") and not rep.get("refused") and rep.get("mode_apply"))
    _check(checks, "rerun_adopted_zero", rep.get("adopted_messages") == 0 and rep.get("resumed_messages") == 0)
    _check(checks, "rerun_no_run_started", rep.get("started_run") is False)
    _check(checks, "rerun_nothing_pending", rep.get("pending_messages") == 0 and rep.get("incomplete_messages") == 0)
    _check(checks, "rerun_already_adopted_is_total", rep.get("already_adopted") == total)
    for key in ("failures", "gaps", "identity_conflicts"):
        _check(checks, "rerun_no_" + key, rep.get(key) == {})
    _check(checks, "rerun_after_clean", after_clean(rep, total))
    _check(checks, "rerun_tree_unchanged", tree_same)
    _check(checks, "rerun_counts_unchanged", counts is not None and counts == counts_before)


def verify(paths, args):
    total, attachments = args.expect_messages, args.expect_attachments
    raw, root, export, ledger, config = (paths[k] for k in ("raw", "root", "export", "ledger", "config"))
    engine = paths["engine"]
    receipt = {"schema": SCHEMA, "status": None, "stopped_at": None,
               "expected": {"messages": total, "attachments": attachments},
               "engine": {"mail_adoption_sha256": file_sha256(paths["module"]),
                          "script_sha256": file_sha256(Path(__file__).resolve())},
               "dry_run_only": bool(args.dry_run_only), "checks": {}}
    checks = receipt["checks"]
    marker0 = marker_state(root)
    export0 = tree_digest(export)
    tree0 = tree_digest(raw)
    baseline = ledger_counts(ledger)
    receipt["baseline"] = baseline
    receipt["fixture"] = {"files": tree0["files"], "entries": tree0["entries"],
                          "export_files": export0["files"], "marker_present": marker0 != "absent"}

    def run(apply):
        return run_adopt(engine, root, export, config, apply=apply, timeout=args.timeout)

    def finish(stage):
        receipt["stopped_at"] = stage
        _check(checks, "export_unchanged", tree_digest(export) == export0)
        _check(checks, "marker_unchanged", marker_state(root) == marker0)
        failed = sorted(name for name, ok in checks.items() if not ok)
        receipt["failed_checks"] = failed
        if failed:
            receipt["status"] = "fail_" + stage if stage != "done" else "fail"
        else:
            receipt["status"] = "pass_dry_run_only" if args.dry_run_only else "pass"
        return receipt

    dry = run(False)
    tree1 = tree_digest(raw)
    receipt["dry_run"] = {"exit": dry["exit"], "timed_out": dry["timed_out"],
                          "stderr_empty": dry["stderr_empty"], **dry["report"]}
    dry_run_checks(checks, dry, baseline, tree1 == tree0, total, attachments)
    if not all(checks.values()):
        return finish("dry_run")
    if args.dry_run_only:
        return finish("done")

    n0 = dry["report"]["new_messages"]
    app = run(True)
    tree2 = tree_digest(raw)
    counts = ledger_counts(ledger)
    receipt["apply"] = {"exit": app["exit"], "timed_out": app["timed_out"],
                        "stderr_empty": app["stderr_empty"], **app["report"]}
    receipt["after_apply"] = counts
    apply_checks(checks, app, n0, baseline, counts, total)
    if n0 == 0:
        _check(checks, "apply_tree_unchanged_when_nothing_new", tree2 == tree1)
    if not all(checks.values()):
        return finish("apply")

    again = run(True)
    tree3 = tree_digest(raw)
    counts_again = ledger_counts(ledger)
    receipt["rerun"] = {"exit": again["exit"], "timed_out": again["timed_out"],
                        "stderr_empty": again["stderr_empty"], **again["report"]}
    receipt["after_rerun"] = counts_again
    rerun_checks(checks, again, counts, counts_again, tree3 == tree2, total)
    receipt["new_on_rerun"] = again["report"].get("adopted_messages")
    return finish("done")


def write_receipt(path, receipt):
    data = (json.dumps(receipt, sort_keys=True, indent=1) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
    return data


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Verify records adopt-mail on a frozen TEST fixture; emit a numbers-only receipt.")
    parser.add_argument("--test-root", required=True, help="frozen TEST copy; its name must end in -TEST")
    parser.add_argument("--config", required=True, help="existing owner-only mail-history-adoption-v1 config")
    parser.add_argument("--receipt", required=True, help="new numbers-only JSON receipt, outside the TEST copy")
    parser.add_argument("--engine", help="engine checkout containing campaign_tool (default: this script's repo)")
    parser.add_argument("--expect-messages", type=int, default=154)
    parser.add_argument("--expect-attachments", type=int, default=82)
    parser.add_argument("--dry-run-only", action="store_true", help="stop after the dry-run checks (no apply)")
    parser.add_argument("--timeout", type=int, default=3600, help="seconds per adopt-mail invocation")
    return parser.parse_args(argv)


def main(argv=None):
    os.umask(0o077)
    args = parse_args(argv)
    try:
        paths = guard(args)
    except Refused as error:
        refused = {"schema": SCHEMA, "status": "refused", "code": safe_code(error)}
        print(json.dumps(refused, sort_keys=True))
        return 2
    receipt = verify(paths, args)
    sys.stdout.write(write_receipt(paths["receipt"], receipt).decode())
    return 0 if receipt["status"].startswith("pass") else 1


if __name__ == "__main__":
    sys.exit(main())
