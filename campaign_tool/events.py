"""``event add|remove|list --directory D`` (DF1109-14): edit ``content/events.json`` (DF1107-08) without typing offsets.

``add`` takes local wall-clock times (``YYYY-MM-DDTHH:MM`` or ``YYYY-MM-DDTHH:MM:SS``) and an explicit IANA zone, and stores
each endpoint in the existing ``YYYY-MM-DDTHH:MM:SS±HH:MM`` form with the offset that zone has at that moment. A local time
the zone skips (a daylight-saving gap) or passes twice (an overlap) is refused, never normalized or guessed; there is no
fold override. The zone name is conversion input only: the file schema is unchanged and stored rows are never re-converted.

Every operation needs a real ``content/`` with a valid ``site.json`` and validates the whole current file with the build's
own ``load_events`` first (a malformed file refuses every operation, including ``remove``), then the whole prospective list,
then writes once: same-directory temporary file, fsync, a recheck that the file is still the one read (or still absent),
one rename. Any ordinary failure leaves the old bytes and mode. One writer at a time; there is no stronger guarantee.
``list`` writes nothing. Nothing is fetched, built or published; rebuild the site after a change.
"""
import json
import os
import re
import stat
import tempfile
import zoneinfo
from datetime import date, datetime, timezone
from pathlib import Path

from .content import MAX_JSON_BYTES, ContentError, _read_json, load_site
from .content_events import build_cutoff, load_events, partition, read_events

PARTS = ("content", "events.json")
LABEL = "/".join(PARTS)
LOCAL = re.compile(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?", re.ASCII)
DAY = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)
ZONE_KEY = re.compile(r"[A-Za-z0-9_+-]{1,64}(?:/[A-Za-z0-9_+-]{1,64}){0,3}", re.ASCII)
MAX_DESCRIPTION_BYTES = 1200 * 4  # load_events caps description_md at 1200 characters


class EventError(ContentError):
    """An event request or the events file is refused; nothing was written. The CLI exits 2."""
    exit_code = 2


def _zone(key):
    if not isinstance(key, str) or not ZONE_KEY.fullmatch(key):
        raise EventError("--tz: must be an IANA time zone name such as America/Los_Angeles")
    try:
        return zoneinfo.ZoneInfo(key)
    except (KeyError, ValueError, OSError):  # ZoneInfoNotFoundError is a KeyError
        pass
    if not zoneinfo.available_timezones():
        raise EventError("--tz: no IANA time zone database is installed (install the tzdata package; nothing is fetched)")
    raise EventError("--tz: unknown time zone; use an IANA name such as America/Los_Angeles")


def local_moment(text, zone_key, field):
    """The aware ``datetime`` (in the zone) for one local wall-clock time; EventError when it is malformed, skipped by
    the zone (gap) or occurs twice (overlap). ``field`` (``--starts``/``--ends``) names the refusal; text is never echoed."""
    match = LOCAL.fullmatch(text) if isinstance(text, str) else None
    if not match:
        raise EventError(f"{field}: must be a local time YYYY-MM-DDTHH:MM or YYYY-MM-DDTHH:MM:SS, without offset or Z")
    try:
        wall = datetime(*(int(part or 0) for part in match.groups()))
    except ValueError:
        raise EventError(f"{field}: not a real calendar date and time") from None
    zone = _zone(zone_key)
    instants = {}
    for fold in (0, 1):
        try:
            instant = wall.replace(tzinfo=zone, fold=fold).astimezone(timezone.utc)
            back = instant.astimezone(zone)
        except (OverflowError, ValueError):
            raise EventError(f"{field}: outside the range of representable times") from None
        if back.replace(tzinfo=None) == wall:  # naive comparison: the wall fields round-trip exactly
            instants[instant] = back
    if not instants:
        raise EventError(f"{field}: nonexistent local time in --tz (skipped by a daylight-saving change)")
    if len(instants) > 1:
        raise EventError(f"{field}: ambiguous local time in --tz (occurs twice at a daylight-saving change)")
    return next(iter(instants.values()))


def stored_moment(moment):
    """``YYYY-MM-DDTHH:MM:SS±HH:MM`` in the moment's own offset; an offset with seconds keeps them (load_events refuses)."""
    total = int(moment.utcoffset().total_seconds())
    sign, total = ("-" if total < 0 else "+"), abs(total)
    offset = f"{sign}{total // 3600:02d}:{total % 3600 // 60:02d}" + (f":{total % 60:02d}" if total % 60 else "")
    return moment.replace(tzinfo=None).isoformat(timespec="seconds") + offset


def _content(root):
    content = Path(root) / "content"
    if content.is_symlink() or not content.is_dir():
        raise EventError("content/ does not exist (or is a symbolic link); run content init first")
    site = content / "site.json"
    if site.is_symlink() or not site.is_file():
        raise EventError("content/site.json is required")
    load_site(_read_json(site), content / "public-assets")
    return content


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_nlink)


def _load(content):
    """-> (bytes, identity, mode) of content/events.json, or None when absent. Links and non-regular files are refused."""
    target = content / PARTS[1]
    try:
        info = os.lstat(target)
    except FileNotFoundError:
        return None
    except OSError:
        raise EventError(f"{LABEL}: cannot be inspected") from None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise EventError(f"{LABEL}: must be a regular file with a single link (not a symbolic link or folder)")
    try:
        fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise EventError(f"{LABEL}: could not be read") from None
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise EventError(f"{LABEL}: must be a regular file with a single link (not a symbolic link or folder)")
        data = handle.read(MAX_JSON_BYTES + 1)
    if len(data) > MAX_JSON_BYTES:
        raise EventError(f"{LABEL}: file too large")
    return data, _identity(info), stat.S_IMODE(info.st_mode)


def _validate(rows):
    try:
        return load_events(rows)
    except ContentError as exc:
        raise EventError(f"{exc}; nothing was written") from None


def _state(root):
    """-> (content dir, current validated rows in stored order, loaded or None)."""
    try:
        content = _content(root)
    except EventError:
        raise
    except ContentError as exc:
        raise EventError(str(exc)) from None
    loaded = _load(content)
    if loaded is None:
        return content, [], None
    try:
        document = json.loads(loaded[0].decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise EventError("events.json: not valid UTF-8 JSON; nothing was written") from None
    return content, _validate(document), loaded


def _install(content, rows, loaded):
    """Validate ``rows`` as the whole file, then write them beside the target and rename once over it."""
    _validate(rows)
    data = (json.dumps(rows, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    if len(data) > MAX_JSON_BYTES:
        raise EventError(f"{LABEL}: would be larger than {MAX_JSON_BYTES} bytes; nothing was written")
    target = content / PARTS[1]
    name = None
    try:
        fd, name = tempfile.mkstemp(dir=content, prefix=".events-", suffix=".tmp")
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), 0o644 if loaded is None else loaded[2])
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        current = _load(content)  # still the very file read (or still absent) just before the swap
        if (current is None) != (loaded is None) or (current is not None and current[:2] != loaded[:2]):
            raise EventError(f"{LABEL}: changed while it was being edited; nothing was written")
        os.replace(name, target)
        name = None
    except OSError:
        raise EventError(f"{LABEL}: write failed; the previous file was kept") from None
    finally:
        if name is not None:
            try:
                os.unlink(name)
            except OSError:
                pass


def _description(path):
    """The exact text of a regular, non-link UTF-8 file (no newline translation, nothing trimmed)."""
    try:
        fd = os.open(Path(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise EventError("--description-file: could not be read (it must be a regular file, not a symbolic link)") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise EventError("--description-file: must be a regular file")
        data = os.read(fd, MAX_DESCRIPTION_BYTES + 1)
    except OSError:
        raise EventError("--description-file: could not be read") from None
    finally:
        os.close(fd)
    if len(data) > MAX_DESCRIPTION_BYTES:
        raise EventError("--description-file: longer than 1200 characters")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise EventError("--description-file: not valid UTF-8") from None


def add_event(root, *, event_id, title, starts, ends, tz, description_file, venue=None, online=False, url=None,
              accessibility_note=None):
    """Append one event; -> ``{"action": "added", "id": event_id}``. A duplicate id is refused (no upsert)."""
    if (venue is None) == (not online):
        raise EventError("give exactly one of --venue or --online")
    content, rows, loaded = _state(root)
    start, end = local_moment(starts, tz, "--starts"), local_moment(ends, tz, "--ends")
    if end.astimezone(timezone.utc) < start.astimezone(timezone.utc):
        raise EventError("--ends: is before --starts; nothing was written")
    if any(row.get("id") == event_id for row in rows):
        raise EventError("--id: an event with this id already exists; nothing was written")
    row = {"id": event_id, "title": title, "starts_at": stored_moment(start), "ends_at": stored_moment(end)}
    row.update({"venue": venue} if venue is not None else {"online": True})
    row["description_md"] = _description(description_file)
    for key, value in (("url", url), ("accessibility_note", accessibility_note)):
        if value is not None:
            row[key] = value
    _install(content, rows + [row], loaded)
    return {"action": "added", "id": event_id}


def remove_event(root, *, event_id):
    """Drop the one event whose id is exactly ``event_id``; -> ``{"action": "removed", "id": event_id}``.
    The last removal leaves ``[]`` (the file is never deleted)."""
    content, rows, loaded = _state(root)
    kept = [row for row in rows if row["id"] != event_id]
    if len(kept) == len(rows):
        raise EventError("--id: no event with this id; nothing was written")
    _install(content, kept, loaded)
    return {"action": "removed", "id": event_id}


def parse_day(text):
    if not isinstance(text, str) or not DAY.fullmatch(text):
        raise EventError("--as-of: must be a real YYYY-MM-DD date")
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise EventError("--as-of: must be a real YYYY-MM-DD date") from None


def list_events(root, as_of=None):
    """-> ``{"as_of", "upcoming", "past"}`` split exactly as the site build splits them (start at or after ``as_of``
    00:00:00 UTC is upcoming). Reads with the build's reader; writes nothing."""
    as_of = as_of or date.today()
    try:
        _load(_content(root))  # the same regular, single-link store check add and remove make (a hardlink is refused)
        rows = read_events(root) or []
    except EventError:
        raise
    except ContentError as exc:
        raise EventError(str(exc)) from None
    upcoming, past = partition(rows, build_cutoff(as_of))
    return {"as_of": as_of.isoformat(), "upcoming": upcoming, "past": past}


def format_events(listing):
    return json.dumps(listing, indent=2, ensure_ascii=False)
