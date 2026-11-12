"""Offline maintenance of organizer-attested regular schedules; single writer only.

Only staged content is replaced. A later explicit build retains all publication gates.
"""
import functools
import json
import os
import stat
import secrets
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


def _target_mode(dir_fd):
    try:
        info = os.stat(schedules.FILE, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return 0o644
    if not stat.S_ISREG(info.st_mode):
        raise BodyScheduleError("body-schedules.json: must be a regular non-symlink file")
    return stat.S_IMODE(info.st_mode)


_KEPT = "body-schedules.json: could not be written; the previous file was kept"
_UNCONFIRMED = ("body-schedules.json: the change was installed, but directory sync failed; "
                "durability is not confirmed; inspect the store before retrying")


def _open_directory(content):
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(content, flags)
    try:
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("not a directory")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _check_directory(content, dir_fd):
    current = content.lstat()
    held = os.fstat(dir_fd)
    if (not stat.S_ISDIR(current.st_mode)
            or (current.st_dev, current.st_ino) != (held.st_dev, held.st_ino)):
        raise OSError("content directory changed")


def _temp_name():
    return f".body-schedules-{secrets.token_hex(16)}.tmp"


def _write(content, rows, day):
    """Persist rows as: file fsync -> atomic replace -> content/ directory fsync -> return.

    Before the replacement succeeds, any OSError means the previous file was kept. After it,
    a directory-sync failure means the new store IS installed but its durability is unconfirmed;
    that state is never rolled back and never reported as ordinary success.
    Namespace operations stay relative to the acquired directory, even if it is renamed
    after the final identity observation. These observations are not a writer lock.
    """
    rows = schedules.load_body_schedules({"schema_version": 1, "schedules": rows}, today=day)
    try:
        data = (json.dumps({"schema_version": 1, "schedules": rows}, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    except UnicodeEncodeError:
        raise BodyScheduleError("body-schedules.json: text is not valid UTF-8") from None
    if len(data) > MAX_JSON_BYTES:
        raise BodyScheduleError(f"body-schedules.json: would exceed {MAX_JSON_BYTES} bytes; nothing was changed")
    name = None
    handle = None
    dir_fd = None
    installed = False
    try:
        try:
            dir_fd = _open_directory(content)
            _check_directory(content, dir_fd)
            mode = _target_mode(dir_fd)
            for _ in range(10):
                candidate = _temp_name()
                try:
                    handle = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                     0o600, dir_fd=dir_fd)
                except FileExistsError:
                    continue
                name = candidate  # ownership begins only after exclusive creation succeeds
                break
            else:
                raise OSError("temporary name collisions")
            out = os.fdopen(handle, "wb")
            handle = None  # the stream now owns the descriptor
            with out:
                os.fchmod(out.fileno(), mode)
                out.write(data)
                out.flush()
                os.fsync(out.fileno())
            _target_mode(dir_fd)  # refuse a changed store type; not a writer lock
            _check_directory(content, dir_fd)
            os.replace(name, schedules.FILE, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            installed = True
        except OSError:
            raise BodyScheduleError(_KEPT) from None
        try:
            os.fsync(dir_fd)
        except OSError:
            raise BodyScheduleError(_UNCONFIRMED) from None
    finally:
        if handle is not None:
            try:
                os.close(handle)
            except OSError:
                pass
        if name is not None and not installed:
            try:
                os.unlink(name, dir_fd=dir_fd)
            except OSError:
                pass
        if dir_fd is not None:
            try:
                os.close(dir_fd)
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


def _read_meeting(path, admitted):
    """RF0330-3: the parsed JSON of one staged meeting leaf, read only through the admitted file itself.

    ``admitted`` is the leaf's lstat result from the path check. The leaf is opened read-only, no-follow and
    nonblocking (a missing capability refuses rather than falling back), and its descriptor must be a regular file
    with the admitted device/inode before any payload read. A descriptor size over ``MAX_JSON_BYTES`` is refused
    unread; otherwise at most ``MAX_JSON_BYTES + 1`` bytes are read, so growth after the size check is caught by the
    returned length before decoding. Acquisition and I/O failures give one fixed diagnostic with no OS detail.
    This guards the leaf only: it is not a directory-descriptor walk, a snapshot or a writer lock."""
    name = path.name
    unsafe = f"meetings/{name}: cannot be read safely"
    nofollow, nonblock = getattr(os, "O_NOFOLLOW", None), getattr(os, "O_NONBLOCK", None)
    if not nofollow or not nonblock:
        raise BodyScheduleError(unsafe)
    try:
        fd = os.open(path, os.O_RDONLY | nofollow | nonblock)
    except OSError:
        raise BodyScheduleError(unsafe) from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != (admitted.st_dev, admitted.st_ino):
            raise BodyScheduleError(unsafe)
        if info.st_size > MAX_JSON_BYTES:
            raise ContentError(f"{name}: file too large")
        handle = os.fdopen(fd, "rb")  # the stream owns the descriptor from here
    except OSError:
        os.close(fd)
        raise BodyScheduleError(unsafe) from None
    except BaseException:
        os.close(fd)
        raise
    try:
        with handle:
            data = handle.read(MAX_JSON_BYTES + 1)
    except OSError:
        raise BodyScheduleError(unsafe) from None
    if len(data) > MAX_JSON_BYTES:
        raise ContentError(f"{name}: file too large")
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContentError(f"{name}: not valid UTF-8 JSON ({exc})") from None
    except RecursionError:
        # RF1741-3: fixed category text only; never echo decoder resource details or payload.
        raise ContentError(f"{name}: JSON nesting is too deep") from None
    except ValueError:  # e.g. the interpreter's integer-conversion limit; JSONDecodeError was handled above
        raise ContentError(f"{name}: JSON number could not be decoded") from None


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
            admitted = path.lstat()
        except OSError:
            raise BodyScheduleError(f"meetings/{path.name}: cannot be read safely") from None
        if not stat.S_ISREG(admitted.st_mode):
            raise BodyScheduleError(f"meetings/{path.name}: must be a regular non-symlink file")
        try:
            meetings.append(load_meeting(_read_meeting(path, admitted), "meetings/" + path.name, today=day))
        except BodyScheduleError:
            raise
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
