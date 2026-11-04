"""DF1207-08: import the organizer-transcribed list of agencies the vendor transparency portal names as receiving data.

Offline only: the organizer reads the portal and types the names into a CSV; the file is read as a plain file, never
fetched or followed, and nothing in it is read as a finding. The whole ``content/agencies.json`` document is validated
by ``content.load_agencies`` before the one atomic write; any bad row stops everything and nothing is written.
"""
import csv
import io
import os
import stat
from datetime import date
from pathlib import Path

from .content import (ContentError, SHARING_HEADER, SHARING_MAX_ROWS, _calendar_day, _read_json, _sharing, load_agencies,
                      sharing_row_problems, sharing_source_problem)
from .kit import _write_json

MAX_CSV_BYTES = 1024 * 1024
LISTED_PROBLEMS = 20


class SharingImportError(ContentError):
    pass


def _read_csv(path):
    """``[(first line number, cells)]`` of a regular, non-symlink UTF-8 CSV (a BOM is allowed), blank lines kept."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise SharingImportError(f"csv: cannot read {path} as a regular file ({exc.strerror})") from None
    try:
        data = os.read(fd, MAX_CSV_BYTES + 1) if stat.S_ISREG(os.fstat(fd).st_mode) else None
        if data is not None and len(data) <= MAX_CSV_BYTES:  # os.read may return less than the file: read to the end
            while chunk := os.read(fd, MAX_CSV_BYTES + 1 - len(data)):
                data += chunk
    finally:
        os.close(fd)
    if data is None or len(data) > MAX_CSV_BYTES:
        raise SharingImportError(f"csv: must be a regular file of at most {MAX_CSV_BYTES} bytes")
    try:
        reader, rows, start = csv.reader(io.StringIO(data.decode("utf-8-sig"), newline="")), [], 1
        for cells in reader:
            rows.append((start, cells))
            start = reader.line_num + 1
        return rows
    except (UnicodeDecodeError, csv.Error) as exc:
        raise SharingImportError(f"csv: not a valid UTF-8 CSV ({exc})") from None


def check_rows(rows):
    """-> (recipients, problems): each problem names its file line; all of them are collected."""
    if not rows or rows[0][1] != SHARING_HEADER:
        raise SharingImportError("csv: header must be exactly " + ",".join(SHARING_HEADER))
    body = rows[1:]
    if not body:
        raise SharingImportError(f"csv: must list 1 to {SHARING_MAX_ROWS} agencies, found none")
    if len(body) > SHARING_MAX_ROWS:
        raise SharingImportError(f"csv: must list 1 to {SHARING_MAX_ROWS} agencies, found {len(body)}")
    recipients, problems, seen = [], [], {}
    for line, cells in body:
        if len(cells) != len(SHARING_HEADER):
            problems.append(f"line {line}: expected {len(SHARING_HEADER)} columns, found {len(cells)}" if cells else f"line {line}: blank line")
            continue
        name, state, kind = cells[0].strip(), cells[1], cells[2]
        found = [f"line {line}: {field}: {message}" for field, message in sharing_row_problems(name, state, kind)]
        if not found:
            key = (name.casefold(), state)
            if key in seen:
                found.append(f"line {line}: duplicate of line {seen[key]} (same name and state)")
            else:
                seen[key] = line
                recipients.append({"name": name, "state": state, "kind": kind})
        problems += found
    return recipients, problems


def import_sharing(root, agency_id, csv_path, checked_on, source_url, replace=False, today=None):
    """Record ``profile.sharing`` for ``agency_id`` in content/agencies.json from the CSV; -> a summary dict."""
    today = today or date.today()
    if _calendar_day(checked_on, "--checked-on") > today:
        raise SharingImportError("--checked-on: must not be after today")
    if (problem := sharing_source_problem(source_url)) is not None:
        raise SharingImportError(f"--source-url: {problem}")
    recipients, problems = check_rows(_read_csv(csv_path))
    if problems:
        shown = problems[:LISTED_PROBLEMS] + ([f"and {len(problems) - LISTED_PROBLEMS} more"] if len(problems) > LISTED_PROBLEMS else [])
        raise SharingImportError("; ".join(shown) + "; nothing was recorded")
    path = Path(root) / "content" / "agencies.json"
    if not path.is_file() and not path.is_symlink():
        raise SharingImportError("content/agencies.json: not found")
    document = _read_json(path)
    entries = [a for a in document if isinstance(a, dict) and a.get("agency_id") == agency_id] if isinstance(document, list) else []
    if len(entries) != 1:
        raise SharingImportError(f"agency {agency_id}: not found in content/agencies.json")
    entry = entries[0]
    if entry.get("selected", True) is not True:
        raise SharingImportError(f"agency {agency_id}: is not selected")
    profile = entry.setdefault("profile", {})
    if not isinstance(profile, dict):
        raise SharingImportError(f"agency {agency_id}: profile must be an object")
    if profile.get("sharing") is not None and not replace:
        raise SharingImportError(f"agency {agency_id} already has a sharing list; pass --replace to overwrite it")
    profile["sharing"] = {"checked_on": checked_on, "source_url": source_url, "recipients": recipients}
    profile["sharing"] = _sharing(profile["sharing"], "profile.sharing", today)  # sorted as stored
    load_agencies(document, "content/agencies.json", today)  # the whole document, as build reads it
    if os.path.islink(path):  # checked again right before the write
        raise SharingImportError("content/agencies.json must not be a symlink")
    _write_json(path, document)
    return {"agency_id": agency_id, "recipients": len(recipients), "checked_on": checked_on, "source_url": source_url}
