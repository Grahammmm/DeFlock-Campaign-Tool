"""No-response follow-ups: when a request clock passes, queue a DRAFT reminder. Nothing here sends.

Inputs are one run's ``CURRENT-REQUEST-REGISTER.json`` (the register already computed each request's
statutory determination reminder date and the agency's promised date), an optional per-agency settings
file and the approval queue. A past clock yields a pending draft in the queue; approval stays with a
human. Autonomy is ``off`` (default) or ``draft``; there is no sending value. The campaign project
directory, agency keys and the register's own law wording are inputs, never constants here.
Every day here is on the register's Pacific calendar: a reminder approved at 00:30Z counts from the
previous Pacific day, and the formal-notice wait adds ``notice_after_days`` calendar days to that day.
"""
import argparse
import hashlib
import json
import os
import re
import stat
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

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
REGISTER_ZONE = ZoneInfo("America/Los_Angeles")  # the register's own calendar (outcomes.register)
_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_ISO_STAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}(?::[0-9]{2}(?:\.[0-9]{1,6})?)?(?:Z|[+-][0-9]{2}:[0-9]{2})")


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


def _entry(value, *, allow_signature=False):
    allowed = {"autonomy", "notice_after_days", "silence_after_days"} | ({"signature"} if allow_signature else set())
    if not isinstance(value, dict) or set(value) - allowed:
        raise SettingsError("entry")
    out = {}
    if "signature" in value:
        signature = value["signature"]
        if (not isinstance(signature, str) or not signature.strip() or len(signature) > 200
                or signature.count("\n") > 2 or re.search(r"[\x00-\x09\x0b-\x1f\x7f-\x9f]", signature)):
            raise SettingsError("signature")
        out["signature"] = signature
    if "autonomy" in value:
        if value["autonomy"] not in AUTONOMY or not isinstance(value["autonomy"], str):
            raise SettingsError("autonomy")
        out["autonomy"] = value["autonomy"]
    if "notice_after_days" in value:
        days = value["notice_after_days"]
        if type(days) is not int or not 1 <= days <= 60:
            raise SettingsError("notice_after_days")
        out["notice_after_days"] = days
    if "silence_after_days" in value:
        days = value["silence_after_days"]
        if type(days) is not int or not 0 <= days <= 30:
            raise SettingsError("silence_after_days")
        out["silence_after_days"] = days
    return out


def _off():
    return {"invalid": False, "default": {"autonomy": "off", "notice_after_days": DEFAULT_NOTICE_DAYS,
                                         "silence_after_days": 0}, "agencies": {}}


def _read_bounded(path, limit, *, owner_only=False):
    """At most ``limit`` bytes of one regular file. Read-only; no symlink is followed at any level.

    The leaf opens with O_NONBLOCK, so a FIFO or device is refused at fstat instead of waiting for a writer.
    """
    parts = Path(os.path.abspath(path)).parts
    parent, leaf = os.open("/", os.O_RDONLY | os.O_DIRECTORY), None
    try:
        for part in parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = child
        leaf = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        info = os.fstat(leaf)
        if not stat.S_ISREG(info.st_mode) or (owner_only and (
                info.st_uid != os.geteuid() or info.st_mode & 0o077 or info.st_nlink != 1)):
            raise ValueError("input_file")
        if info.st_size > limit:
            raise ValueError("input_size")
        chunks, size = [], 0
        while size <= limit:
            chunk = os.read(leaf, min(1 << 16, limit + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        if size > limit:
            raise ValueError("input_size")
        return b"".join(chunks)
    finally:
        if leaf is not None:
            os.close(leaf)
        os.close(parent)


def load_settings(project):
    """Per-agency settings. Absent file: all agencies off. Any defect: ``invalid`` and all agencies off."""
    invalid = dict(_off(), invalid=True)
    try:
        path = checked_path(os.path.join(project, SETTINGS_FILE), existing=False)
        try:
            raw = _read_bounded(path, MAX_SETTINGS, owner_only=True)
        except FileNotFoundError:
            return _off()
        doc = _strict(raw)
        if not isinstance(doc, dict) or doc.get("schema") != SETTINGS_SCHEMA or set(doc) - {"schema", "default", "agencies"}:
            return invalid
        default = _entry(doc.get("default", {}), allow_signature=True)
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
    """A day on the register's Pacific calendar, or None when unavailable.

    A canonical ``YYYY-MM-DD`` is that recorded date. A complete timezone-aware ISO timestamp (``Z`` or
    ``±HH:MM``, as the approval queue writes ``updated_at``) is the America/Los_Angeles date of that
    instant, the same day the register uses for ``as_of``. Anything else is unavailable: no prefix fallback.
    """
    if not isinstance(value, str):
        return None
    try:
        if _ISO_DATE.fullmatch(value):
            return date.fromisoformat(value)
        if _ISO_STAMP.fullmatch(value):
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(REGISTER_ZONE).date()
    except (ValueError, OverflowError):
        pass
    return None


def followup_due(row, setting):
    """Pure clock dates, including future ones; closed requests have no follow-up due."""
    if str(row.get("status", "")).strip().casefold() in CLOSED:
        return []
    deadline, promise = row.get("deadline") or {}, row.get("promised") or {}
    clocks = []
    if deadline.get("state") not in ("determined", "unknown") and deadline.get("due"):
        clocks.append(("determination", deadline["due"]))
    if promise.get("state") in ("upcoming", "due_today", "passed") and promise.get("date"):
        clocks.append(("promised", promise["date"]))
    return [(kind, value, (day + timedelta(days=1 + setting.get("silence_after_days", 0))).isoformat())
            for kind, value in clocks if (day := _day(value)) is not None]


def _clocks(row, setting, today):
    """Passed clocks of one register row: [(clock_kind, date, reminder_kind)]."""
    out = []
    for kind, clock_date, due in followup_due(row, setting):
        state = (row.get("deadline" if kind == "determination" else "promised") or {}).get("state")
        if state in ("past_due_reminder", "passed") and today and today >= _day(due):
            out.append((kind, clock_date, REMINDERS[0 if kind == "determination" else 1]))
    return out


def _plan(register, settings, queue_rows, as_of):
    today = _day(as_of)
    approved = {(r["item_id"], r["kind"]): r for r in queue_rows if r["status"] == "approved"}
    counts = {"skipped_off": 0, "skipped_issue": 0, "not_due": 0}
    found = []
    for row in register.get("requests", []):
        setting = _setting(settings, row["agency_key"])
        clocks = _clocks(row, setting, today)
        if not clocks or str(row.get("status", "")).strip().casefold() in CLOSED:
            counts["not_due"] += 1
            continue
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
                              "clock_kind": clock_kind, "clock_date": clock_date, "item_id": ident, "row": row,
                              "signature": setting.get("signature"),
                              "reminder_date": changed.isoformat() if changed else None})
    return found, counts


def plan(register, settings, queue_rows, *, as_of):
    """Pure planner: the drafts this register calls for. No IO."""
    return _plan(register, settings, queue_rows, as_of)[0]


def reminder_status(register, settings, queue_rows, *, as_of):
    """Read-only status per passed clock; only planner identities can match a draft."""
    today = _day(as_of)
    if today is None:
        raise ValueError("as_of_invalid")
    candidates = {(c["item_id"], c["kind"]) for c in plan(register, settings, queue_rows, as_of=as_of)}
    queued = {(r["item_id"], r["kind"]): r for r in queue_rows}
    out, seen = [], set()
    for row in register.get("requests", []):
        setting = _setting(settings, row["agency_key"])
        closed = str(row.get("status", "")).strip().casefold() in CLOSED
        for kind, clock_date, due in followup_due(dict(row, status="open"), setting):
            source = row.get("deadline" if kind == "determination" else "promised") or {}
            key = (row["agency_key"], row["request_id"], kind, clock_date)
            if key in seen or source.get("state") not in ("past_due_reminder", "passed") or _day(clock_date) >= today:
                continue
            seen.add(key)
            ident = item_id(*key)
            pair = (ident, REMINDERS[0 if kind == "determination" else 1])
            match = queued.get(pair) if pair in candidates else None
            state, reason = None, "No reminder draft queued"
            if closed:
                state, reason = "closed", "Request closed"
            elif setting["autonomy"] != "draft":
                state, reason = "off", "Reminders are off for this agency (see Settings)"
            elif today < _day(due):
                state, reason = "not_yet", "Reminder due " + due
            elif row.get("issues"):
                reason = "Request needs checking before a reminder can be drafted"
            elif match:
                state = {"pending": "drafted", "approved": "approved", "rejected": "rejected"}.get(match["status"])
                reason = {"drafted": "Draft ready", "approved": "Approved", "rejected": "Rejected"}.get(
                    state, "Reminder draft " + match["status"])
            out.append(dict(zip(("agency_key", "request_id", "clock_kind", "clock_date"), key),
                            state=state, due=due, reason=reason, **({"queue_id": match["id"]} if match else {})))
    return out


def clock_table(register, settings, queue_rows, timeline=None, *, as_of):
    """All request clocks, on our records; not a legal finding. Pure, with no IO."""
    today = _day(as_of)
    if today is None:
        raise ValueError("as_of_invalid")
    chains = {(r["agency_key"], r["request_id"]): r.get("extension_chain") or {}
              for r in (timeline or {}).get("requests", [])}
    approved = {(r["item_id"], r["kind"]): r for r in queue_rows if r["status"] == "approved"}
    out = []
    for row in register.get("requests", []):
        setting = _setting(settings, row["agency_key"])
        deadline, promise = row.get("deadline") or {}, row.get("promised") or {}
        followups = followup_due(row, setting)
        notices = []
        for kind, clock_date, _ in followups:
            ident = item_id(row["agency_key"], row["request_id"], kind, clock_date)
            prior = approved.get((ident, REMINDERS[0 if kind == "determination" else 1]))
            changed = _day(prior["updated_at"]) if prior else None
            if changed:
                notices.append((changed + timedelta(days=setting["notice_after_days"])).isoformat())
        entry = {"agency_key": row["agency_key"], "request_id": row["request_id"],
                 **{k: deadline.get(k) for k in ("basis", "base_due", "extended_due", "due", "state")},
                 "promised": dict(promise),
                 "followup_due": [{"clock_kind": k, "clock_date": c, "due_date": d} for k, c, d in followups],
                 "notice_due": min(notices) if notices else None,
                 "extension_limit_exceeded": chains.get((row["agency_key"], row["request_id"]), {}).get("exceeds_14_days"),
                 "autonomy": setting["autonomy"], "issues": list(row.get("issues") or []),
                 "closed": str(row.get("status", "")).strip().casefold() in CLOSED}
        out.append(entry)

    def order(entry):
        dates = [entry.get(k) for k in ("base_due", "extended_due", "due", "notice_due")]
        dates += [entry["promised"].get("date")] + [f["due_date"] for f in entry["followup_due"]]
        upcoming = [d for d in dates if _day(d) and _day(d) >= today]
        return (min(upcoming, default="9999-12-31"), entry["agency_key"], entry["request_id"])
    return sorted(out, key=order)


def _render(candidate, register, run_id):
    row = candidate["row"]
    body = draft_templates.render_followup(
        candidate["kind"], agency=row["agency"], request_id=row["request_id"], asked=row.get("asked") or "",
        request_date=row.get("request_date"), clock_kind=candidate["clock_kind"], clock_date=candidate["clock_date"],
        deadline=row.get("deadline") or {}, limits=register.get("limits") or [], as_of=register["as_of"],
        signature=candidate.get("signature"), reminder_date=candidate.get("reminder_date"))
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
        raw = _read_bounded(path, MAX_REGISTER)
    except FileNotFoundError:
        return None
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
                            ("rows", "print Deadlines and Overdue rows"),
                            ("clocks", "print all request clocks and follow-up due dates")):
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
    elif args.action == "clocks":
        from .outcomes.timeline import SCHEMA
        timeline = None
        try:
            path = checked_path(os.path.join(args.project, "run-outputs", args.run_id, "outcomes", "timeline.json"), existing=False)
            try:
                with secure_open(path) as stream:
                    raw = stream.read(MAX_REGISTER + 1)
            except FileNotFoundError:
                raw = None
            if raw is not None:
                if len(raw) > MAX_REGISTER:
                    raise ValueError("timeline_size")
                timeline = _strict(raw)
                if not isinstance(timeline, dict) or timeline.get("schema") != SCHEMA or timeline.get("run_id") != args.run_id \
                        or not isinstance(timeline.get("requests"), list):
                    raise ValueError("timeline_shape")
            result = clock_table(register, settings, queue_rows, timeline, as_of=register["as_of"])
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            print("timeline_unreadable", file=sys.stderr)
            return 1
    else:
        result = dashboard(register, settings, queue_rows)
    print(json.dumps(result, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
