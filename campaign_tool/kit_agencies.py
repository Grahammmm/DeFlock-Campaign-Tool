"""kit-agency: add, select and deselect agencies in kit/agencies.json without hand-editing JSON.

Validates the whole list first (duplicate ids, non-boolean ``selected``), writes atomically through
``kit._write_json`` and keeps the file mode. ``kit`` keeps an existing agencies.json, so changes survive a re-run.
Offline; nothing is fetched or sent.
"""
import csv
import io
import os
import stat
from datetime import date
from pathlib import Path

from . import kit

KINDS = tuple(kit.LAW_ENFORCEMENT) + kit.GOVERNING + ("other",)


def _path(root):
    path = Path(root) / "kit" / "agencies.json"
    if path.is_symlink() or not path.is_file():
        raise ValueError("run kit first: kit/agencies.json is missing")
    return path


def load_agencies(root):
    """Return the raw agency list after kit's own checks plus duplicate and ``selected`` checks. Read-only."""
    path = _path(root)
    kit._load_existing(path)
    agencies = kit._read_json(path)
    seen = set()
    for entry in agencies:
        if entry["agency_id"] in seen:
            raise ValueError(f"duplicate agency_id {entry['agency_id']} in kit/agencies.json")
        seen.add(entry["agency_id"])
    for entry in agencies:
        if "selected" in entry and not isinstance(entry["selected"], bool):
            raise ValueError(f"{entry['agency_id']}: selected must be true or false")
    return agencies


def _save(root, agencies):
    path = _path(root)
    mode = stat.S_IMODE(path.stat().st_mode)
    kit._write_json(path, agencies)
    os.chmod(path, mode)


def set_selected(root, agency_id, value):
    """Set ``selected`` on the one entry with ``agency_id``; no write when it already has that value."""
    agencies = load_agencies(root)
    matches = [entry for entry in agencies if entry["agency_id"] == agency_id]
    if len(matches) != 1:
        raise ValueError(f"agency_id {agency_id!r} must appear exactly once in kit/agencies.json (found {len(matches)})")
    entry, value = matches[0], bool(value)
    changed = entry.get("selected", True) is not value
    if changed:
        entry["selected"] = value
        _save(root, agencies)
    return {"agency_id": agency_id, "selected": value, "changed": changed}


def _has_control(text):
    return any(ord(c) < 32 or 127 <= ord(c) < 160 or c in "\u2028\u2029" for c in text)


def _check_new(agency_id, name, kind, note, state, existing=(), today=None):
    """The ``add`` field rules for one new agency: its entry, or ValueError naming the first bad field."""
    if not isinstance(agency_id, str) or not kit.AGENCY_ID.match(agency_id):
        raise ValueError("agency_id must be lowercase like 'ca-example-police'")
    if agency_id[:2] != state:
        raise ValueError(f"agency_id must start with '{state}-' for this campaign's state")
    if agency_id in existing:
        raise ValueError(f"agency_id {agency_id} already exists in kit/agencies.json")
    if not isinstance(name, str) or not 3 <= len(name.strip()) <= 120 or _has_control(name):
        raise ValueError("name must be 3-120 characters with no control characters")
    try:  # same plain-text rule the request renderer applies, so the next kit run can draft this agency
        kit._clean_agency_name(name)
    except ValueError:
        raise ValueError("name must be plain text without angle brackets ('<' or '>')") from None
    if kind not in KINDS:
        raise ValueError("kind must be one of " + ", ".join(KINDS))
    if note is not None and (not isinstance(note, str) or len(note) > 300):
        raise ValueError("note must be at most 300 characters")
    entry = {"agency_id": agency_id, "name": name.strip(), "kind": kind, "selected": True, "verified": False,
             "added_by": "organizer", "added_on": (today or date.today()).isoformat()}
    if note is not None:
        entry["note"] = note
    return entry


def add_agency(root, agency_id, name, kind, note=None, today=None):
    """Append an organizer-added agency (selected, unverified). Refuses bad fields by name; file unchanged then."""
    from .cli import read_config  # local import: cli imports this module lazily
    agencies = load_agencies(root)
    state = read_config(Path(root))["state"].lower()
    entry = _check_new(agency_id, name, kind, note, state, {a["agency_id"] for a in agencies}, today)
    agencies.append(entry)
    _save(root, agencies)
    return entry


CSV_MAX_BYTES, CSV_MAX_ROWS, CSV_SHOWN = 256 * 1024, 500, 20


def _read_csv(csv_path):
    """[(line, cells)] for the non-blank data rows of a small regular UTF-8 CSV with the import header."""
    try:
        fd = os.open(csv_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:  # O_NOFOLLOW: a symlink fails here with ELOOP
        raise ValueError(f"csv cannot be opened as a regular file (symlinks are refused): {exc.strerror}") from None
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValueError("csv must be a regular file")
    with os.fdopen(fd, "rb") as handle:
        data = handle.read(CSV_MAX_BYTES + 1)
    if len(data) > CSV_MAX_BYTES:
        raise ValueError("csv is larger than 256 KiB")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("csv is not UTF-8") from None
    reader, rows, start = csv.reader(io.StringIO(text, newline="")), [], 1
    try:
        for cells in reader:
            if cells:
                rows.append((start, cells))
            start = reader.line_num + 1
    except csv.Error as exc:
        raise ValueError(f"csv line {start}: {exc}") from None
    if not rows or rows[0][1] not in (["agency_id", "name", "kind"], ["agency_id", "name", "kind", "note"]):
        raise ValueError("csv header must be exactly agency_id,name,kind or agency_id,name,kind,note")
    width, data_rows = len(rows[0][1]), rows[1:]
    if not 1 <= len(data_rows) <= CSV_MAX_ROWS:
        raise ValueError(f"csv must have 1-{CSV_MAX_ROWS} data rows (found {len(data_rows)})")
    _refuse([f"line {n}: expected {width} cells, found {len(c)}" for n, c in data_rows if len(c) != width])
    return data_rows


def _refuse(errors):
    if errors:
        more = f"\n(+{len(errors) - CSV_SHOWN} more)" if len(errors) > CSV_SHOWN else ""
        raise ValueError(f"{len(errors)} bad row(s); kit/agencies.json was not changed:\n"
                         + "\n".join(errors[:CSV_SHOWN]) + more)


def import_csv(root, csv_path, *, check=False, today=None):
    """Validate every CSV row with the ``add`` rules, then append them all in one write, or write nothing."""
    from .cli import read_config
    agencies = load_agencies(root)
    existing, state = {a["agency_id"] for a in agencies}, read_config(Path(root))["state"].lower()
    rows, errors, first_line, entries = _read_csv(csv_path), [], {}, []
    for line, cells in rows:
        agency_id, name, kind = cells[:3]
        note = cells[3] if len(cells) == 4 and cells[3] != "" else None
        try:
            entry = _check_new(agency_id, name, kind, note, state, today=today)
            if agency_id in existing:
                raise ValueError("already in kit/agencies.json")
            if agency_id in first_line:
                raise ValueError(f"repeats line {first_line[agency_id]} of this file")
            entries.append(entry)
        except ValueError as exc:
            errors.append(f"line {line}: {exc}")
        first_line.setdefault(agency_id, line)
    _refuse(errors)
    if not check:
        _save(root, agencies + entries)
    return {"added": [entry["agency_id"] for entry in entries], "count": len(entries), "written": not check}
