"""DF1207-08: import the organizer-transcribed list of agencies the vendor transparency portal names as receiving data.

Offline only: the organizer reads the portal and types the names into a CSV; the file is read as a plain file, never
fetched or followed, and nothing in it is read as a finding. The whole ``content/agencies.json`` document is validated
by ``content.load_agencies`` before the one atomic write; any bad row stops everything and nothing is written.

RF0804-4: the selected campaign root is opened once; ``content`` and ``agencies.json`` are admitted relative to it without
following links, and the read, the temporary file and the replacement all go through those descriptors, so a linked
``content`` directory (or one swapped in later) can never send the import to another campaign.

RF1835-2: the existing file is read only up to ``MAX_JSON_BYTES``, and the complete new document is held to the same
limit in the exact UTF-8 bytes that would be written (indentation and non-ASCII text included). A larger document, or
one that cannot be encoded as UTF-8, is refused before any temporary file is created, so a successful import always
leaves a file the content reader accepts.

RF0540-4: after the replacement the admitted ``content`` directory is flushed (one ``fsync`` on the retained descriptor)
before success is reported. If that flush fails the complete new file stays installed (never rolled back) and the
import stops with a fixed message saying the data was written but crash durability is unconfirmed. A directory fsync
attempt is not a promise of power-loss survival on every filesystem. Failures before the replacement still leave the
previous file untouched.
"""
import csv
import io
import json
import os
import secrets
import stat
from datetime import date

from .content import (ContentError, MAX_JSON_BYTES, SHARING_HEADER, SHARING_MAX_ROWS, _calendar_day, _sharing, load_agencies,
                      sharing_row_problems, sharing_source_problem)

MAX_CSV_BYTES = 1024 * 1024
LISTED_PROBLEMS = 20
TARGET = "agencies.json"


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


def _open_content(root):
    """-> (root fd, content fd): ``content`` admitted relative to the root as a real directory, never through a link."""
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        raise SharingImportError(f"content/: the selected campaign directory cannot be opened ({exc.strerror})") from None
    try:
        return root_fd, os.open("content", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
    except OSError as exc:
        os.close(root_fd)
        raise SharingImportError(f"content/: must be a real directory inside the selected campaign, not a link ({exc.strerror})") from None


def _read_all(fd, limit, offset=0):
    """Up to ``limit`` + 1 bytes of ``fd`` from ``offset`` (so a file over the limit is detected before decoding)."""
    data = b""
    while len(data) <= limit and (chunk := os.pread(fd, limit + 1 - len(data), offset + len(data))):
        data += chunk
    return data


def _read_target(content_fd):
    """-> (fd, fstat, bytes) of content/agencies.json opened relative to the admitted directory; the fd stays open."""
    try:
        fd = os.open(TARGET, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=content_fd)
    except FileNotFoundError:
        raise SharingImportError("content/agencies.json: not found") from None
    except OSError as exc:
        raise SharingImportError(f"content/agencies.json: cannot be read as a regular file ({exc.strerror})") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise SharingImportError("content/agencies.json: must be a regular file")
        data = _read_all(fd, MAX_JSON_BYTES)
        if len(data) > MAX_JSON_BYTES:
            raise SharingImportError("content/agencies.json: file too large")
        return fd, info, data
    except BaseException:
        os.close(fd)
        raise


def _parse_target(data):
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SharingImportError(f"content/agencies.json: not valid UTF-8 JSON ({exc})") from None


def _same(a, b):
    return all(getattr(a, f) == getattr(b, f) for f in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns"))


def _install_problem(root_fd, content_fd, content_stat, target_fd, target_stat, original):
    """Why the replacement must not go ahead (the directory or the file is no longer what was read), else None."""
    try:
        now = os.stat("content", dir_fd=root_fd, follow_symlinks=False)
        if not stat.S_ISDIR(now.st_mode) or (now.st_dev, now.st_ino) != (content_stat.st_dev, content_stat.st_ino):
            return "content/ changed while the import was running"
        named = os.stat(TARGET, dir_fd=content_fd, follow_symlinks=False)
        if not stat.S_ISREG(named.st_mode) or not _same(named, target_stat) or not _same(os.fstat(target_fd), target_stat):
            return "content/agencies.json changed while the import was running"
        if _read_all(target_fd, MAX_JSON_BYTES) != original:
            return "content/agencies.json changed while the import was running"
    except OSError:
        return "content/ or content/agencies.json disappeared or changed while the import was running"
    return None


def _before_install():
    """Seam between the last write and the identity check; does nothing (tests place a deterministic swap here)."""


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
    root_fd, content_fd = _open_content(root)
    target_fd = temp_fd = temp = None
    try:
        content_stat = os.fstat(content_fd)
        target_fd, target_stat, original = _read_target(content_fd)
        try:  # RF1005-8: only the decoder's own recursion failure is a refusal; nothing has been staged yet
            document = _parse_target(original)
        except RecursionError:
            raise SharingImportError("content/agencies.json: JSON nesting is too deep; nothing was written") from None
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
        try:  # the exact buffer written below, measured before any temporary file exists
            text = (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        except UnicodeEncodeError:
            raise SharingImportError("content/agencies.json: import cannot be encoded as UTF-8; nothing was written") from None
        if len(text) > MAX_JSON_BYTES:
            raise SharingImportError(f"content/agencies.json: import would exceed {MAX_JSON_BYTES} bytes; nothing was written")
        name = f".{TARGET}.{secrets.token_hex(8)}.tmp"
        temp_fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=content_fd)
        temp = name
        view = memoryview(text)
        while view:
            view = view[os.write(temp_fd, view):]
        os.fsync(temp_fd)
        os.close(temp_fd)
        temp_fd = None
        _before_install()
        if (why := _install_problem(root_fd, content_fd, content_stat, target_fd, target_stat, original)) is not None:
            raise SharingImportError(f"{why}; nothing was written")
        os.replace(temp, TARGET, src_dir_fd=content_fd, dst_dir_fd=content_fd)
        temp = None  # committed: from here on the new document is installed and must never be rolled back
        try:  # RF0540-4: flush the already-admitted content directory so the rename itself is made durable
            os.fsync(content_fd)
        except OSError:
            raise SharingImportError("content/agencies.json: the new sharing data was written, but content/ could not be flushed to disk; "
                                     "crash durability is unconfirmed. Inspect the file before retrying.") from None
    finally:
        if temp_fd is not None:
            os.close(temp_fd)
        if temp is not None:  # only this command's own temporary file, from the admitted directory
            try:
                os.unlink(temp, dir_fd=content_fd)
            except OSError:
                pass
        for fd in (target_fd, content_fd, root_fd):
            if fd is not None:
                os.close(fd)
    return {"agency_id": agency_id, "recipients": len(recipients), "checked_on": checked_on, "source_url": source_url}
