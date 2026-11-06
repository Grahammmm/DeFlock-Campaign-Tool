"""No-response follow-ups: when a request clock passes, queue a DRAFT reminder. Nothing here sends.

Inputs are one run's ``CURRENT-REQUEST-REGISTER.json`` (the register already computed each request's
statutory determination reminder date and the agency's promised date), an optional per-agency settings
file and the approval queue. A past clock yields a pending draft in the queue; approval stays with a
human. Autonomy is ``off`` (default) or ``draft``; there is no sending value. The campaign project
directory, agency keys and the register's own law wording are inputs, never constants here.
"""
import argparse
import hashlib
import json
import os
import re
import stat
import sys
from datetime import date, timedelta

from . import approval_queue, draft_templates
from .intake.folder import secure_open
from .ledger.store import checked_path

SETTINGS_FILE = "followup-settings.json"
SETTINGS_SCHEMA = "records-followup-settings-v1"
MAX_SETTINGS = 64 * 1024
MAX_REGISTER = 32 * 1024 * 1024
AUTONOMY = ("off", "draft")  # sending is deliberately not a value
DEFAULT_NOTICE_DAYS = 10
CLOSED = ("complete", "completed", "closed", "withdrawn", "fulfilled")  # the register's own set
REMINDERS = ("reminder-no-response", "reminder-promised-date")
NOTICE = "formal-notice-no-response"
RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
REGISTER_NAME = "CURRENT-REQUEST-REGISTER.json"
REGISTER_SCHEMA = "records-request-register-v1"


class SettingsError(ValueError):
    pass


def _strict(raw):
    def pairs(items):
        keys = [k for k, _ in items]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate key")
        return dict(items)

    def refuse(_):
        raise ValueError("nonfinite")
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=refuse)
    except (ValueError, UnicodeDecodeError, RecursionError) as error:
        raise SettingsError("json") from error


def _entry(value, allowed_autonomy=True):
    if not isinstance(value, dict) or set(value) - {"autonomy", "notice_after_days"}:
        raise SettingsError("entry")
    out = {}
    if "autonomy" in value:
        if value["autonomy"] not in AUTONOMY or not isinstance(value["autonomy"], str):
            raise SettingsError("autonomy")
        out["autonomy"] = value["autonomy"]
    if "notice_after_days" in value:
        days = value["notice_after_days"]
        if type(days) is not int or not 1 <= days <= 60:
            raise SettingsError("notice_after_days")
        out["notice_after_days"] = days
    return out


def _off():
    return {"invalid": False, "default": {"autonomy": "off", "notice_after_days": DEFAULT_NOTICE_DAYS}, "agencies": {}}


def load_settings(project):
    """Per-agency settings. Absent file: all agencies off. Any defect: ``invalid`` and all agencies off."""
    invalid = dict(_off(), invalid=True)
    try:
        path = checked_path(os.path.join(project, SETTINGS_FILE), existing=False)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return _off()
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not (stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o077
                    and info.st_nlink == 1 and info.st_size <= MAX_SETTINGS):
                return invalid
            doc = _strict(stream.read(MAX_SETTINGS + 1))
        if not isinstance(doc, dict) or doc.get("schema") != SETTINGS_SCHEMA or set(doc) - {"schema", "default", "agencies"}:
            return invalid
        default = _entry(doc.get("default", {}))
        agencies = doc.get("agencies", {})
        if not isinstance(agencies, dict):
            return invalid
        result = _off()
        result["default"].update(default)
        result["agencies"] = {str(k): _entry(v) for k, v in agencies.items()}
        return result
    except (SettingsError, OSError, ValueError):
        return invalid


def _setting(settings, agency_key):
    merged = dict(settings["default"])
    merged.update(settings["agencies"].get(agency_key, {}))
    return merged


def item_id(agency_key, request_id, clock_kind, clock_date):
    blob = json.dumps([agency_key, request_id, clock_kind, clock_date], separators=(",", ":"), ensure_ascii=False)
    return "clock:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def _day(value):
    try:
        return date.fromisoformat(value[:10])
    except (TypeError, ValueError):
        return None


def _clocks(row):
    """Passed clocks of one register row: [(clock_kind, date, reminder_kind)]."""
    out = []
    deadline, promise = row.get("deadline") or {}, row.get("promised") or {}
    if deadline.get("state") == "past_due_reminder" and deadline.get("due"):
        out.append(("determination", deadline["due"], "reminder-no-response"))
    if promise.get("state") == "passed" and promise.get("date"):
        out.append(("promised", promise["date"], "reminder-promised-date"))
    return out


def _plan(register, settings, queue_rows, as_of):
    today = _day(as_of)
    approved = {(r["item_id"], r["kind"]): r for r in queue_rows if r["status"] == "approved"}
    counts = {"skipped_off": 0, "skipped_issue": 0, "not_due": 0}
    found = []
    for row in register.get("requests", []):
        clocks = _clocks(row)
        if not clocks or str(row.get("status", "")).strip().casefold() in CLOSED:
            counts["not_due"] += 1
            continue
        setting = _setting(settings, row["agency_key"])
        if setting["autonomy"] != "draft":
            counts["skipped_off"] += 1
            continue
        if row.get("issues"):
            counts["skipped_issue"] += 1
            continue
        for clock_kind, clock_date, reminder in clocks:
            ident = item_id(row["agency_key"], row["request_id"], clock_kind, clock_date)
            kinds = [reminder]
            prior = approved.get((ident, reminder))
            changed = _day(prior["updated_at"]) if prior else None
            if changed and today and changed + timedelta(days=setting["notice_after_days"]) <= today:
                kinds.append(NOTICE)
            for kind in kinds:
                found.append({"kind": kind, "agency_key": row["agency_key"], "request_id": row["request_id"],
                              "clock_kind": clock_kind, "clock_date": clock_date, "item_id": ident, "row": row})
    return found, counts


def plan(register, settings, queue_rows, *, as_of):
    """Pure planner: the drafts this register calls for. No IO."""
    return _plan(register, settings, queue_rows, as_of)[0]


def _render(candidate, register, run_id):
    row = candidate["row"]
    body = draft_templates.render_followup(
        candidate["kind"], agency=row["agency"], request_id=row["request_id"], asked=row.get("asked") or "",
        request_date=row.get("request_date"), clock_kind=candidate["clock_kind"], clock_date=candidate["clock_date"],
        deadline=row.get("deadline") or {}, limits=register.get("limits") or [], as_of=register["as_of"])
    evidence = {"source": "register", "run_id": run_id, "agency_key": candidate["agency_key"],
                "request_id": candidate["request_id"], "clock_kind": candidate["clock_kind"],
                "clock_date": candidate["clock_date"], "register_sources": row.get("sources", [])}
    return body, evidence


def read_register(project, run_id):
    """This run's register only, or None when it is absent. Never falls back to another run."""
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
        raise ValueError("run_id")
    path = checked_path(os.path.join(project, "run-outputs", run_id, REGISTER_NAME), existing=False)
    try:
        with secure_open(path) as source:
            raw = source.read(MAX_REGISTER + 1)
    except FileNotFoundError:
        return None
    if len(raw) > MAX_REGISTER:
        raise ValueError("register_size")
    try:
        doc = _strict(raw)
    except SettingsError as error:
        raise ValueError("register_json") from error
    if not isinstance(doc, dict) or doc.get("schema") != REGISTER_SCHEMA or not isinstance(doc.get("requests"), list) \
            or _day(doc.get("as_of")) is None:
        raise ValueError("register_shape")
    return doc


def queue_followups(project, run_id):
    """Queue pending drafts for this run's register. Returns counts only (no bodies, names or dates)."""
    summary = {"created": 0, "existing": 0, "skipped_off": 0, "skipped_issue": 0, "not_due": 0,
               "settings_invalid": False}
    register = read_register(project, run_id)
    if register is None:
        return summary
    settings = load_settings(project)
    summary["settings_invalid"] = settings["invalid"]
    queue_rows = approval_queue.load_queue(project)
    candidates, counts = _plan(register, settings, queue_rows, register["as_of"])
    summary.update(counts)
    have = {r["id"] for r in queue_rows}
    for candidate in candidates:
        body, evidence = _render(candidate, register, run_id)
        existed = approval_queue.draft_id(candidate["item_id"], candidate["kind"]) in have
        approval_queue.add_draft(project, item_id=candidate["item_id"], kind=candidate["kind"],
                                 agency_id=candidate["agency_key"], request_id=candidate["request_id"], body=body,
                                 evidence=evidence, source_run_id=run_id)
        summary["existing" if existed else "created"] += 1
    return summary


def dashboard(register, settings, queue_rows):
    """Deadlines (still running) and Overdue (passed) rows for the admin panel. Pure."""
    status = {}
    for r in queue_rows:
        status.setdefault(r["item_id"], []).append(f"{r['kind']}:{r['status']}")
    out = {"as_of": register["as_of"], "deadlines": [], "overdue": []}
    for row in register.get("requests", []):
        if str(row.get("status", "")).strip().casefold() in CLOSED:
            continue
        deadline, promise = row.get("deadline") or {}, row.get("promised") or {}
        items = []
        if deadline.get("state") in ("running", "due_today", "past_due_reminder") and deadline.get("due"):
            items.append(("determination", deadline["due"], deadline["state"]))
        if promise.get("state") in ("upcoming", "due_today", "passed") and promise.get("date"):
            items.append(("promised", promise["date"], promise["state"]))
        for clock_kind, clock_date, state in items:
            ident = item_id(row["agency_key"], row["request_id"], clock_kind, clock_date)
            entry = {"agency": row["agency"], "agency_key": row["agency_key"], "request_id": row["request_id"],
                     "clock_kind": clock_kind, "date": clock_date, "state": state,
                     "autonomy": _setting(settings, row["agency_key"])["autonomy"],
                     "followups": sorted(status.get(ident, [])), "check": bool(row.get("issues"))}
            out["overdue" if state in ("past_due_reminder", "passed") else "deadlines"].append(entry)
    for key in ("deadlines", "overdue"):
        out[key].sort(key=lambda e: (e["date"], e["agency_key"], e["request_id"], e["clock_kind"]))
    return out


def main():
    parser = argparse.ArgumentParser(prog="records followups",
                                     description="No-response follow-up clocks. Read-only; sends nothing.")
    sub = parser.add_subparsers(dest="action", required=True)
    for name, help_text in (("plan", "print the drafts this run's register calls for"),
                            ("rows", "print Deadlines and Overdue rows")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--project", required=True)
        p.add_argument("--run-id", required=True)
    args = parser.parse_args()
    try:
        register = read_register(args.project, args.run_id)
    except (ValueError, OSError):
        print("register_unreadable", file=sys.stderr)
        return 1
    if register is None:
        print("register_missing", file=sys.stderr)
        return 1
    settings = load_settings(args.project)
    try:
        queue_rows = approval_queue.load_queue(args.project)
    except approval_queue.ApprovalQueueError as error:
        print(error.code, file=sys.stderr)
        return 1
    if args.action == "plan":
        found, _ = _plan(register, settings, queue_rows, register["as_of"])
        have = {r["id"] for r in queue_rows}
        result = [{**{k: c[k] for k in ("kind", "agency_key", "request_id", "clock_kind", "clock_date", "item_id")},
                   "queued": approval_queue.draft_id(c["item_id"], c["kind"]) in have} for c in found]
        result = {"settings_invalid": settings["invalid"], "candidates": result}
    else:
        result = dashboard(register, settings, queue_rows)
    print(json.dumps(result, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
