#!/usr/bin/env python3
"""Non-fatal run notification for the records runtime.

Reads the run report v1 written by the stage orchestrator and sends one
short, metadata-only Telegram message. It then records the outcome as the
report's ``notify`` stage. It always exits 0 for delivery problems. A failed
send is logged, an alert file is written, the report gets
``"notify_failed": true`` and the notify stage becomes ``fail`` with a
``NOTIFY_<REASON>`` code. The run's own exit status is untouched. The
failure wording is used only when the run itself did not succeed.

The message is built only from enums, counts and upper-snake codes in the
report, never from record content. Credentials come from the process
environment or a KEY=VALUE env file: TELEGRAM_BOT_TOKEN and
TELEGRAM_HOME_CHANNEL. They are never logged or written to the report or
the alert file.

Run with PYTHONPATH set to the engine source tree. Report updates go through
campaign_tool.records.run_orchestrator so the report stays schema-valid.
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

from campaign_tool.records import run_orchestrator, run_report

DEFAULT_API_BASE = "https://api.telegram.org"
LOOPBACK_PREFIXES = ("http://127.0.0.1:", "http://[::1]:", "http://localhost:")
TIMEOUT_SECONDS = 20

PREFLIGHT_MESSAGES = {
    "DOCKER_UNAVAILABLE": "{label}: records run {run_id} could not start because Docker or the "
                          "records container was not ready. No records work was attempted.",
    "IMAGE_MISMATCH": "{label}: records run {run_id} refused to start (configuration or image "
                      "check failed). No records work was attempted.",
    "TARGET_CONFIG_INVALID": "{label}: records run {run_id} refused to start (target stage plan "
                             "invalid). No records work was attempted.",
    "DISK_SPACE_LOW": "{label}: records run {run_id} refused to start (runtime disk space below "
                      "the configured minimum). No records work was attempted.",
    "DISK_SPACE_UNKNOWN": "{label}: records run {run_id} refused to start (runtime disk space "
                          "could not be measured). No records work was attempted.",
}


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


def compose(report: dict[str, Any], label: str, exit_code: int) -> str:
    """Build the message from report enums, counts and codes only."""
    run_id = report["run_id"]
    work = [stage for stage in report["stages"] if stage["name"] != "notify"]
    failed = [stage for stage in work if stage["status"] == "fail"]
    warned = [stage for stage in work if stage["status"] == "warn"]
    needs = len(report["needs_g"])
    tail = f" Needs G: {needs} item(s)." if needs else ""
    if failed:
        first = failed[0]
        if first["error_code"] in PREFLIGHT_MESSAGES:
            return PREFLIGHT_MESSAGES[first["error_code"]].format(label=label, run_id=run_id) + tail
        return (f"{label}: records run {run_id} failed at stage {first['name']} "
                f"({first['error_code']}, exit {exit_code}). The runtime needs attention. "
                f"No replies were sent.{tail}")
    if warned:
        codes = ", ".join(f"{stage['name']} {stage['error_code']}" for stage in warned)
        return (f"{label}: records run {run_id} completed with warnings ({codes}). "
                f"Details are in the private run report.{tail}")
    return f"{label}: records run {run_id} completed. Details are in the private run report.{tail}"


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
    parser.add_argument("--exit-code", type=int, required=True)
    parser.add_argument("--log")
    parser.add_argument("--env-file")
    parser.add_argument("--label", default="Records runtime")
    parser.add_argument("--api-base", default=os.environ.get("RECORDS_NOTIFY_API_BASE", DEFAULT_API_BASE))
    args = parser.parse_args(argv)

    report_path = Path(args.report)
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        run_report.validate_run_report(report)
    except (OSError, ValueError):
        # Nothing trustworthy to send or update. The wrapper sees no outcome
        # and records helper_no_outcome itself.
        append_log(args.log, "unknown", "run report unreadable or invalid; nothing sent")
        return 0
    run_id = report["run_id"]

    env = read_env_file(args.env_file)
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or env.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_HOME_CHANNEL") or env.get("TELEGRAM_HOME_CHANNEL", "")

    started = time.monotonic()
    reason = None
    if not api_base_allowed(args.api_base):
        reason = "api_base_not_allowed"
    elif not token or not chat_id:
        reason = "missing_credentials"
    else:
        try:
            send(args.api_base.rstrip("/"), token, chat_id,
                 compose(report, args.label, args.exit_code))
        except Exception as exc:  # noqa: BLE001 - delivery must never be fatal
            reason = classify(exc)

    run_orchestrator.record_notify_outcome(report, sent=reason is None, reason=reason,
                                           seconds=time.monotonic() - started)
    if reason is None:
        append_log(args.log, run_id, "sent")
    else:
        append_log(args.log, run_id, f"FAILED reason={reason}; run status unchanged")
        try:
            run_orchestrator.write_notify_alert(args.alerts_dir, report, reason, args.exit_code)
        except OSError:
            append_log(args.log, run_id, "could not write alert file")
    try:
        run_orchestrator.atomic_write_json(report_path, report)
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
