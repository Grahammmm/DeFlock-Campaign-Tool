"""Offline maintenance of organizer-attested regular schedules; single writer only.

Only staged content is replaced. A later explicit build retains all publication gates.
"""
import functools
import json
import os
import stat
import tempfile
from datetime import date
from pathlib import Path

from . import body_schedules as schedules
from .content import MAX_JSON_BYTES, ContentError, _read_json, load_meeting, load_site


class BodyScheduleError(ContentError):
    """A refused schedule command; the CLI exits 2."""
    exit_code = 2


def _domain_errors(function):
    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except BodyScheduleError:
            raise
        except (ContentError, OSError, UnicodeError) as exc:
            raise BodyScheduleError(str(exc)) from None
    return wrapper


def _content_dir(root):
    content = Path(root) / "content"
    if content.is_symlink() or not content.is_dir():
        raise BodyScheduleError("content/ must be an existing real directory")
    site = content / "site.json"
    if site.is_symlink() or not site.is_file():
        raise BodyScheduleError("content/site.json is required as a regular non-symlink file")
    load_site(_read_json(site), content / "public-assets")
    return content


def _target_mode(content):
    if content.is_symlink() or not content.is_dir():
        raise BodyScheduleError("content/ must be an existing real directory")
    target = content / schedules.FILE
    if not os.path.lexists(target):
        return 0o644
    info = target.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise BodyScheduleError("body-schedules.json: must be a regular non-symlink file")
    return stat.S_IMODE(info.st_mode)


def _write(content, rows, day):
    rows = schedules.load_body_schedules({"schema_version": 1, "schedules": rows}, today=day)
    try:
        data = (json.dumps({"schema_version": 1, "schedules": rows}, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    except UnicodeEncodeError:
        raise BodyScheduleError("body-schedules.json: text is not valid UTF-8") from None
    if len(data) > MAX_JSON_BYTES:
        raise BodyScheduleError(f"body-schedules.json: would exceed {MAX_JSON_BYTES} bytes; nothing was changed")
    mode = _target_mode(content)
    name = None
    try:
        handle, name = tempfile.mkstemp(dir=content, prefix=".body-schedules-", suffix=".tmp")
        with os.fdopen(handle, "wb") as out:
            os.fchmod(out.fileno(), mode)
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        _target_mode(content)  # refuse a changed entry type before replacement; not a writer lock
        os.replace(name, content / schedules.FILE)
    except OSError:
        raise BodyScheduleError("body-schedules.json: could not be written; the previous file was kept") from None
    finally:
        if name is not None:
            try:
                os.unlink(name)
            except OSError:
                pass


@_domain_errors
def add_schedule(root, *, body, weekday, ordinals, local_start_time, time_zone, source_url, checked_on, today=None):
    day = today or date.today()
    content = _content_dir(root)
    rows = schedules.read_body_schedules(root, today=day)
    rows.append({"body": body, "weekday": weekday, "ordinals": ordinals, "local_start_time": local_start_time,
                 "time_zone": time_zone, "source_url": source_url, "checked_on": checked_on})
    _write(content, rows, day)
    return f"Added body schedule: {body}."


def _find(rows, body):
    for row in rows:
        if row["body"] == body:
            return row
    raise BodyScheduleError(f"body-schedules.json: body not found: {body}")


@_domain_errors
def recheck_schedule(root, *, body, checked_on, today=None):
    day = today or date.today()
    content = _content_dir(root)
    rows = schedules.read_body_schedules(root, today=day)
    row = _find(rows, body)
    previous = row["checked_on"]
    row["checked_on"] = checked_on
    schedules.load_body_schedules({"schema_version": 1, "schedules": rows}, today=day)
    if checked_on < previous:
        raise BodyScheduleError(f"body-schedules.json: checked_on must not be before {previous} for {body}")
    if checked_on != previous:
        _write(content, rows, day)
    return f"Rechecked body schedule: {body} (checked {checked_on})."


@_domain_errors
def remove_schedule(root, *, body, today=None):
    day = today or date.today()
    content = _content_dir(root)
    rows = schedules.read_body_schedules(root, today=day)
    _find(rows, body)
    _write(content, [row for row in rows if row["body"] != body], day)
    return f"Removed body schedule: {body}."


def _meetings(content, day):
    folder = content / "meetings"
    if not os.path.lexists(folder):
        return []
    if folder.is_symlink() or not folder.is_dir():
        raise BodyScheduleError("meetings/: must be a real directory")
    meetings = []
    for path in sorted(folder.glob("*.json")):
        if path.is_symlink() or not path.is_file():
            raise BodyScheduleError(f"meetings/{path.name}: must be a regular non-symlink file")
        try:
            meetings.append(load_meeting(_read_json(path), "meetings/" + path.name, today=day))
        except (ContentError, OSError) as exc:
            raise BodyScheduleError(f"meetings/{path.name}: {exc}") from None
    return meetings


@_domain_errors
def list_schedules(root, *, today=None):
    day = today or date.today()
    content = _content_dir(root)
    rows = schedules.read_body_schedules(root, today=day)
    if not rows:
        return "No body schedules recorded."
    staged = _meetings(content, day)
    lines = []
    for row in rows:
        dates = schedules.next_expected_dates(row, staged, day, limit=3)
        watch = ",".join(item.isoformat() for item in dates) or "none in supported calendar range"
        age = (day - date.fromisoformat(row["checked_on"])).days
        state = "stale" if schedules.stale(row, day) else "fresh"
        lines.append(f"{row['body']} | weekday={row['weekday']}; ordinals={','.join(map(str, row['ordinals']))}; "
                     f"local_start_time={row['local_start_time']}; time_zone={row['time_zone']} | "
                     f"source_url={row['source_url']} | checked_on={row['checked_on']}; age={age} days; "
                     f"limit=365 days; {state} | agenda-watch={watch}")
    lines.append("Agenda-watch dates only; check the official source and stage only a verified meeting.")
    return "\n".join(lines)
