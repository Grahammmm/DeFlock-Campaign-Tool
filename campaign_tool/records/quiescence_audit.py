"""records quiescence-audit: one-moment check of every known records writer (DFGL-12B).

Writes ``records-quiescence-v1`` evidence for ``records backup``. Unit states come from a file the operator captured
(``systemctl --user show -p Id,ActiveState,SubState <units>``); this module never calls systemctl. The run lock is one
check among several and never decides alone. The evidence is an attestation the operator makes; nothing authenticates it.
"""
import argparse
import errno
import fcntl
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone

PROC_ROOT = "/proc"
MAX_AGE = 120
MAX_UNITS = 65536
WRITER_UNITS = (
    "records-run.service", "records-run.timer", "records-arrival.path", "records-arrival.service",
    "deflockslo-daily-mail.service", "deflockslo-daily-mail.timer", "records-console.service",
    "records-analysis.service", "records-analysis.timer")


def _units(path):
    """(check, codes). The capture is stale unless its ``Captured=<epoch>`` line is at most MAX_AGE old."""
    check, codes = {"listed": len(WRITER_UNITS), "missing": 0, "active": 0, "stale": True}, []
    if path == "-":  # a pipe (docker exec -i) carries no file age, so the capture time travels in the data
        raw = sys.stdin.buffer.read(MAX_UNITS + 1)
    else:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            raw = os.read(fd, MAX_UNITS + 1)
        finally:
            os.close(fd)
    states = {}
    for block in raw[:MAX_UNITS].decode("utf-8", "replace").split("\n\n"):
        pairs = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        if pairs.get("Captured", "").isdigit():
            check["stale"] = not 0 <= time.time() - int(pairs["Captured"]) <= MAX_AGE
        if "Id" in pairs:
            states[pairs["Id"]] = pairs.get("ActiveState")
    check["missing"] = sum(1 for unit in WRITER_UNITS if unit not in states)
    check["active"] = sum(1 for unit in WRITER_UNITS if unit in states and states[unit] not in ("inactive", "failed"))
    for failed, code in ((check["stale"], "UNITS_STALE"), (check["missing"], "UNITS_INCOMPLETE"),
                         (check["active"], "WRITER_ACTIVE")):
        if failed:
            codes.append(code)
    return check, codes


def _open_files(root):
    held = unreadable = 0
    roots = {root, os.path.realpath(root)}  # /proc reports resolved paths
    for pid in os.listdir(PROC_ROOT):
        if not pid.isdigit() or int(pid) == os.getpid():
            continue
        base = os.path.join(PROC_ROOT, pid)
        targets = []
        try:
            targets.append(os.readlink(os.path.join(base, "cwd")))
        except OSError as error:
            unreadable += error.errno not in (errno.ENOENT, errno.ESRCH)
        try:
            for name in os.listdir(os.path.join(base, "fd")):
                try:
                    targets.append(os.readlink(os.path.join(base, "fd", name)))
                except FileNotFoundError:  # that descriptor closed meanwhile
                    pass
        except OSError as error:
            unreadable += error.errno not in (errno.ENOENT, errno.ESRCH)
        held += sum(1 for target in targets for r in roots if target == r or target.startswith(r + "/"))
    return {"count": held, "proc_unreadable": unreadable}


def _lock(runtime):
    try:
        fd = os.open(os.path.join(runtime, "run-records.lock"), os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return "absent" if os.path.isdir(runtime) else "unreadable"
    except OSError:
        return "unreadable"
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return "free"
    except BlockingIOError:
        return "held"
    except OSError:
        return "unreadable"
    finally:
        os.close(fd)  # closing releases the momentary lock


def audit(project, runtime, units_file):
    checks, codes = {}, []
    checks["units"], found = _units(units_file)
    codes += found
    checks["open_files"] = _open_files(project)
    if checks["open_files"]["count"]:
        codes.append("OPEN_FILE")
    if checks["open_files"]["proc_unreadable"]:
        codes.append("PROC_UNREADABLE")
    checks["run_lock"] = _lock(runtime)
    if checks["run_lock"] in ("held", "unreadable"):
        codes.append("RUN_LOCK_" + checks["run_lock"].upper())
    checks["arrival_pending"] = os.path.lexists(os.path.join(runtime, "arrival.pending"))
    if checks["arrival_pending"]:
        codes.append("ARRIVAL_PENDING")
    checks["codes"] = codes
    return {"schema": "records-quiescence-v1", "basis": "independent_all_writer_audit", "roots": [project],
            "observed_utc": datetime.now(timezone.utc).isoformat(), "all_writers_stopped": not codes,
            "checks": checks, "auditor": "records quiescence-audit"}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="records quiescence-audit", description="Check every known records writer at one moment; write quiescence evidence.")
    parser.add_argument("--project", required=True)
    parser.add_argument("--runtime", required=True)
    parser.add_argument("--units-file", required=True, help="captured units (first line Captured=<epoch>), or - for stdin")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    project, out = os.path.abspath(args.project), os.path.realpath(args.out)  # out resolved through symlinks
    if out.startswith(os.path.realpath(project) + "/"):
        print("OUT_INSIDE_PROJECT")
        return 2
    try:
        doc = audit(project, os.path.abspath(args.runtime), args.units_file)
        raw = (json.dumps(doc, sort_keys=True) + "\n").encode()
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, raw)
        finally:
            os.close(fd)
    except OSError:
        print("AUDIT_UNAVAILABLE")
        return 2
    for code in doc["checks"]["codes"]:
        print(code)
    print("evidence_sha256=" + hashlib.sha256(raw).hexdigest())
    return 0 if doc["all_writers_stopped"] else 1
