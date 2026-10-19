#!/usr/bin/env python3
"""Non-fatal run notification for the records runtime (stdlib only).

Reads the run report written by run-records.sh, sends one short,
metadata-only Telegram message and records the outcome in the report.
It always exits 0 for delivery problems: a failed send is logged, an alert
file is written and the report gets ``"notify_failed": true``. The failure
wording is used only when the pipeline itself did not succeed.

Credentials come from the process environment or a KEY=VALUE env file:
TELEGRAM_BOT_TOKEN and TELEGRAM_HOME_CHANNEL. They are never logged,
written to the report or the alert file.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_API_BASE = "https://api.telegram.org"
LOOPBACK_PREFIXES = ("http://127.0.0.1:", "http://[::1]:", "http://localhost:")
TIMEOUT_SECONDS = 20

MESSAGES = {
    "ok": "{label}: records run completed. Details are in the private run report.",
    "pipeline_failed": "{label}: records run failed (exit {exit_code}). The runtime needs attention. No replies were sent.",
    "docker_unavailable": "{label}: records run could not start because Docker or the records container was not ready. No records work was attempted.",
    "config_error": "{label}: records run refused to start (configuration or image check failed). No records work was attempted.",
}
FALLBACK_MESSAGE = "{label}: records run ended with status {status} (exit {exit_code}). Check the private run report."


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def read_env_file(path: str | None) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path:
        return values
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return values
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def api_base_allowed(base: str) -> bool:
    return base == DEFAULT_API_BASE or base.startswith(LOOPBACK_PREFIXES)


def compose(report: dict[str, Any], label: str) -> str:
    status = str(report.get("status", "unknown"))
    template = MESSAGES.get(status, FALLBACK_MESSAGE)
    return template.format(label=label, status=status, exit_code=report.get("exit_code", "?"))


def send(api_base: str, token: str, chat_id: str, text: str) -> None:
    body = urllib.parse.urlencode(
        {"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"}
    ).encode()
    request = urllib.request.Request(f"{api_base}/bot{token}/sendMessage", data=body)
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        payload = json.load(response)
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise RuntimeError("telegram_not_ok")


def classify(exc: BaseException) -> str:
    """Return a token-free reason for logs and alert files."""
    if isinstance(exc, urllib.error.HTTPError):
        return f"http_{exc.code}"
    if isinstance(exc, urllib.error.URLError):
        return "network_unreachable"
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, RuntimeError) and str(exc) == "telegram_not_ok":
        return "telegram_not_ok"
    if isinstance(exc, (ValueError, json.JSONDecodeError)):
        return "bad_response"
    return type(exc).__name__


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def append_log(path: str | None, run_id: str, message: str) -> None:
    line = f"{utc_now()} run={run_id} notify: {message}"
    print(line, file=sys.stderr)
    if path:
        try:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Non-fatal run notification for the records runtime.")
    parser.add_argument("--report", required=True)
    parser.add_argument("--alerts-dir", required=True)
    parser.add_argument("--log")
    parser.add_argument("--env-file")
    parser.add_argument("--label", default="Records runtime")
    parser.add_argument("--api-base", default=os.environ.get("RECORDS_NOTIFY_API_BASE", DEFAULT_API_BASE))
    args = parser.parse_args(argv)

    report_path = Path(args.report)
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if not isinstance(report, dict):
            raise ValueError("report is not an object")
    except (OSError, ValueError):
        report = {"status": "unknown", "exit_code": None, "report_unreadable": True}
    run_id = str(report.get("run_id", "unknown"))

    env = read_env_file(args.env_file)
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or env.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_HOME_CHANNEL") or env.get("TELEGRAM_HOME_CHANNEL", "")

    reason = None
    if not api_base_allowed(args.api_base):
        reason = "api_base_not_allowed"
    elif not token or not chat_id:
        reason = "missing_credentials"
    else:
        try:
            send(args.api_base.rstrip("/"), token, chat_id, compose(report, args.label))
        except Exception as exc:  # noqa: BLE001 - delivery must never be fatal
            reason = classify(exc)

    attempted_at = utc_now()
    if reason is None:
        report["notify"] = {"status": "sent", "at": attempted_at}
        report["notify_failed"] = False
        append_log(args.log, run_id, "sent")
    else:
        report["notify"] = {"status": "failed", "at": attempted_at, "reason": reason}
        report["notify_failed"] = True
        append_log(args.log, run_id, f"FAILED reason={reason}; run status unchanged")
        alert = {
            "schema": "records-notify-alert-v1",
            "kind": "notify_failed",
            "run_id": run_id,
            "run_status": report.get("status"),
            "run_exit_code": report.get("exit_code"),
            "reason": reason,
            "at": attempted_at,
        }
        try:
            alerts = Path(args.alerts_dir)
            alerts.mkdir(parents=True, exist_ok=True)
            safe_id = "".join(ch for ch in run_id if ch.isalnum() or ch in "-_") or "unknown"
            atomic_write_json(alerts / f"notify-failed-{safe_id}.json", alert)
        except OSError:
            append_log(args.log, run_id, "could not write alert file")

    if not report.get("report_unreadable"):
        try:
            atomic_write_json(report_path, report)
        except OSError:
            append_log(args.log, run_id, "could not update run report")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException:  # noqa: BLE001 - last-resort guard, still non-fatal
        print("notify: unexpected error; run status unchanged", file=sys.stderr)
        raise SystemExit(0)
