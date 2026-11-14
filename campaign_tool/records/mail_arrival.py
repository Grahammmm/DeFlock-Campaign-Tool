"""Non-fatal, metadata-only bridge from a host mail job to records arrivals.

Input is a dedicated sidecar, never a mailbox or the mailer's detailed report.
Only run_id, new_messages and failures are admitted. A failed/partial poll may
stamp downloaded messages but never refreshes the last successful poll time.
"""
import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import arrival

MAX_STATUS_BYTES = 4096


def read_status(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError("status")
        raw = handle.read(MAX_STATUS_BYTES + 1)
    if len(raw) > MAX_STATUS_BYTES:
        raise ValueError("status")
    data = json.loads(raw)
    if not isinstance(data, dict) or set(data) != {"run_id", "new_messages", "failures"}:
        raise ValueError("status")
    if not isinstance(data["run_id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", data["run_id"]):
        raise ValueError("status")
    if any(type(data[key]) is not int or not 0 <= data[key] <= 2**31 - 1
           for key in ("new_messages", "failures")):
        raise ValueError("status")
    return data


def write_state(runtime):
    root, _, _ = arrival._paths(runtime)
    if not arrival._check_dir(root, False):
        raise ValueError("runtime")
    directory = root / "mail-poll"
    arrival._check_dir(directory, True)
    fd, name = tempfile.mkstemp(prefix=".state-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            json.dump({"updated_at": datetime.now(timezone.utc).isoformat()}, handle)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, directory / "state.json")
        arrival._fsync_dir(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Stamp arrivals from a metadata-only mail status sidecar.")
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("status")
    args = parser.parse_args(argv)
    try:
        data = read_status(args.status)
    except (OSError, ValueError):
        print("MAIL_STATUS_UNREADABLE", file=sys.stderr)
        return 0
    if data["failures"]:
        print("MAIL_POLL_FAILED", file=sys.stderr)
    else:
        try:
            write_state(args.runtime_dir)
        except (OSError, ValueError, arrival.ArrivalError):
            print("MAIL_STATE_UNWRITABLE", file=sys.stderr)
    if data["new_messages"] > 0:
        ref = hashlib.sha256(f"{data['run_id']}\0{data['new_messages']}".encode("ascii")).hexdigest()
        try:
            result = subprocess.run(
                [sys.executable, "-B", "-m", "campaign_tool.records", "arrival", "stamp",
                 "--runtime-dir", args.runtime_dir, "--source", "mail", "--ref", ref],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=False)
            if result.returncode:
                print("MAIL_STAMP_FAILED", file=sys.stderr)
        except (OSError, subprocess.SubprocessError):
            print("MAIL_STAMP_FAILED", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
