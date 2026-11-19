"""Check the loopback records console and freshness of its current export."""
import argparse
import http.client
import io
import math
import os
import stat
import sys
from contextlib import redirect_stderr
from datetime import datetime, timezone
from pathlib import Path

from . import admin_panel, console_snapshot, pipeline_health


PAGES = ("/", "/board", "/documents", "/calendar", "/settings", "/next-steps")
POST_PATH = "/approvals/reject"
CSP = dict(admin_panel.SECURITY)["Content-Security-Policy"]
FAILURES = ("FINGERPRINT_MISSING", "SEND_EXPOSED", "POST_ACCEPTED", "EXPORT_STALE", "EXPORT_BROKEN",
            "HTTP_UNAVAILABLE")
# http.client admits three-digit statuses in this range. Include all of them so
# recovery doesn't need a second directory inventory or a persisted index.
ALERT_CODES = FAILURES + tuple(f"HTTP_{status}" for status in range(100, 1000))
DIGITS = "zero one two three four five six seven eight nine".split()


def _http_check(port, method, path):
    # A direct client cannot pick up a proxy or follow an HTTP redirect.
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        connection.request(method, path)
        with connection.getresponse() as response:
            status = response.status
            if method == "POST":
                if status in (403, 503):
                    return "PASS"
                if 200 <= status < 400:
                    return "POST_ACCEPTED"
            elif path == "/send":
                if status == 404:
                    return "PASS"
                if 200 <= status < 300:
                    return "SEND_EXPOSED"
            elif status == 200:
                return "PASS" if response.headers.get_all("Content-Security-Policy") == [CSP] else "FINGERPRINT_MISSING"
            return f"HTTP_{status}"
    except (OSError, http.client.HTTPException):
        return "HTTP_UNAVAILABLE"
    finally:
        connection.close()


def _export_check(root, max_age):
    try:
        # A new pinner can't fall back to a previously good export. Suppress
        # its panel-oriented log; this command reports only the fixed code.
        with redirect_stderr(io.StringIO()):
            pin = console_snapshot.Pinner(root).pin()
        exported = datetime.fromisoformat(pin.exported_at)
        if exported.utcoffset() is None:
            return "EXPORT_BROKEN"
        age = (datetime.now(timezone.utc) - exported).total_seconds() / 3600
    except (console_snapshot.SnapshotError, ValueError, OverflowError):
        return "EXPORT_BROKEN"
    if age < 0:
        return "EXPORT_BROKEN"
    return "EXPORT_STALE" if age >= max_age else "PASS"


def _absolute_dir(raw):
    path = Path(raw)
    if not path.is_absolute() or ".." in path.parts:
        raise argparse.ArgumentTypeError("expected an absolute directory without '..'")
    return Path("/" + raw.lstrip("/"))


def _check_alert_location(directory, export_root):
    # Validate top-down without following links. Sticky shared parents such as
    # /tmp are safe when each child is owned by root or this user.
    uid = os.getuid()
    for parent in reversed(directory.parents):
        info = parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, uid)
                or (info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX)):
            raise pipeline_health.HealthError("alerts_unsafe")
    for root in (export_root, export_root.resolve()):
        if directory.is_relative_to(root) or root.is_relative_to(directory):
            raise pipeline_health.HealthError("alerts_unsafe")


def _record_alerts(directory, failures):
    checks = []
    for code in ALERT_CODES:
        if code.startswith("HTTP_") and code[5:].isdigit():
            reason = "http_" + "_".join(DIGITS[int(digit)] for digit in code[5:])
        else:
            reason = code.lower()
        checks.append({"check": "console_check_" + reason, "reason_code": reason,
                       "state": "warn" if code in failures else "ok"})
    # Delegate to the already-classified pipeline_health alert writer; this module opens no file itself.
    refresh = pipeline_health.write_alerts
    refresh({"now": datetime.now(timezone.utc).isoformat(), "checks": checks}, directory)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="records console-check", description=__doc__, allow_abbrev=False)
    parser.add_argument("--port", required=True, type=int, help="console port on 127.0.0.1")
    parser.add_argument("--export-root", required=True, type=_absolute_dir)
    parser.add_argument("--max-age-hours", type=float, default=36)
    parser.add_argument("--alerts-dir", type=_absolute_dir, help="separate private directory for health alerts")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if not math.isfinite(args.max_age_hours) or args.max_age_hours <= 0:
        parser.error("--max-age-hours must be finite and positive")
    results = [(f"GET {path}", _http_check(args.port, "GET", path)) for path in (*PAGES, "/send")]
    results.append((f"POST {POST_PATH}", _http_check(args.port, "POST", POST_PATH)))
    results.append(("export", _export_check(args.export_root, args.max_age_hours)))
    failures = {code for _, code in results} - {"PASS"}
    if args.alerts_dir is not None:
        try:
            _check_alert_location(args.alerts_dir, args.export_root)
            _record_alerts(args.alerts_dir, failures)
        except (pipeline_health.HealthError, OSError, RuntimeError, ValueError, TypeError):
            print("records console-check: refused alerts_unsafe", file=sys.stderr)
            return 2
    for label, code in results:
        print(f"{label} {code}")
    return int(bool(failures))
