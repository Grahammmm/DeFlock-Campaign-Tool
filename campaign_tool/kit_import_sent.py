"""Record requests the campaign sent before it used the tool, from one private CSV, in one checked write.

Each row becomes an ``imported: true`` entry in kit/sent.json with no draft and no draft hash: the original letter is a
historical assertion by the organizer, not something this tool drafted or checked. Rows are checked in file order
against kit/agencies.json, the request-name and reference rules and the entries already recorded (plus the rows
accepted before them); one refused row means nothing is written. A letter prefix binds an ingested original as
correspondence (read-only ledger lookup). Nothing is fetched or sent, and no input value is echoed in a message.
"""
import copy
import csv
import io
import json
import os
import re
import sqlite3
import stat
from datetime import date, datetime, timezone
from pathlib import Path

from . import kit, kit_track

COLUMNS = ("agency_id", "request", "sent_on", "channel", "reference", "letter_record")
CHANNELS = ("email", "portal", "mail")  # muckrock entries come from kit-muckrock-import, never from this file
MAX_BYTES, MAX_ROWS, SHOWN = 512 * 1024, 500, 20
ISO_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")  # ASCII digits only, zero padded


class SentImportError(ValueError):
    exit_code = 2


def _read_rows(csv_path):  # [(physical start line, trimmed cells)] of the non-empty rows, header first
    try:
        fd = os.open(csv_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:  # O_NOFOLLOW: a symlink fails here with ELOOP
        raise SentImportError("csv cannot be opened as a regular file (symlinks are refused)") from None
    try:  # after open, the raw fd is ours until fdopen succeeds; then the stream owns it
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise SentImportError("csv must be a regular file")
            with os.fdopen(fd, "rb") as handle:
                fd = None
                data = handle.read(MAX_BYTES + 1)
        finally:
            if fd is not None:
                os.close(fd)
    except OSError:  # fixed, value-free refusal: no path, errno or detail of a private input
        raise SentImportError("csv cannot be read; nothing was written") from None
    if len(data) > MAX_BYTES:
        raise SentImportError("csv is larger than 512 KiB")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise SentImportError("csv is not UTF-8") from None
    reader, rows, start = csv.reader(io.StringIO(text, newline=""), strict=True), [], 1
    try:
        for cells in reader:
            cells = [cell.strip() for cell in cells]
            if cells and cells != [""]:  # only a truly empty line is skipped
                rows.append((start, cells))
            start = reader.line_num + 1
    except csv.Error:
        raise SentImportError(f"csv line {start}: malformed CSV (quotes or field size)") from None
    if not rows:
        raise SentImportError("csv is empty")
    if rows[0][1] != list(COLUMNS):
        raise SentImportError("csv header must be exactly " + ",".join(COLUMNS) + " on its first line")
    if len(rows) == 1:
        raise SentImportError("csv has no rows")
    if len(rows) - 1 > MAX_ROWS:
        raise SentImportError(f"csv has {len(rows) - 1} rows; at most {MAX_ROWS} rows")
    return rows[1:]


def _agencies(root):  # {agency_id: number of kit/agencies.json entries with that id}
    path = Path(root) / "kit" / "agencies.json"
    if (Path(root) / "kit").is_symlink() or not (Path(root) / "kit").is_dir():
        raise SentImportError("run kit first: kit/ is missing or not a real folder")
    if path.is_symlink() or not path.is_file():
        raise SentImportError("run kit first: kit/agencies.json is missing")
    try:
        listed = kit._load_existing(path)
    except OSError:
        raise SentImportError("kit/agencies.json cannot be read") from None
    except ValueError:
        # Shared loader diagnostics can contain private paths and agency values.
        raise SentImportError("kit/agencies.json unreadable: invalid agency data") from None
    counts = {}
    for entry in listed:
        counts[entry["agency_id"]] = counts.get(entry["agency_id"], 0) + 1
    return counts


def _letter(root, prefix, cache):  # the one full hash a ledger prefix names; ValueError(reason) with no input echoed
    if prefix not in cache:
        try:
            cache[prefix] = kit_track._resolve_correspondence(root, [prefix], {})[0]
        except ValueError as exc:
            cache[prefix] = str(exc).removeprefix("--correspondence #1: ")
        except (sqlite3.Error, OSError):
            cache[prefix] = "the ledger cannot be read"
    if not re.fullmatch(r"[0-9a-f]{64}", cache[prefix]):
        raise ValueError(cache[prefix])
    return cache[prefix]


def _check_row(root, cells, agencies, requests, first_line, today, letters):
    """(key, entry-without-timestamp, [(field, reason)]) for one row; the key is None when it cannot be formed."""
    if len(cells) != len(COLUMNS):
        return None, None, [("row", f"expected {len(COLUMNS)} cells, found {len(cells)}")]
    agency_id, request, sent_on, channel, reference, letter = cells
    problems, key = [], None
    if not kit.AGENCY_ID.fullmatch(agency_id):
        problems.append(("agency_id", "must look like ca-example-police"))
    elif agencies.get(agency_id, 0) != 1:
        problems.append(("agency_id", "not in kit/agencies.json" if agency_id not in agencies
                         else "appears more than once in kit/agencies.json"))
    request = request or "main"
    if not kit_track.REQUEST.fullmatch(request):
        problems.append(("request", "must be 1-40 characters a-z, 0-9 or -, starting with a letter or digit"))
    elif not problems:
        stem = kit_track._stem(agency_id, request)
        if request != "main" and stem in agencies:
            problems.append(("request", "its draft name is another agency's id; choose another request name"))
        else:
            key = kit_track._key(agency_id, request)
    day = kit_track._day(sent_on, ISO_DAY)
    if day is None:
        problems.append(("sent_on", "must be a real YYYY-MM-DD date"))
    elif day > today or day < kit_track.EARLIEST:
        problems.append(("sent_on", "must be between 2000-01-01 and today"))
    if channel not in CHANNELS:
        problems.append(("channel", "must be one of " + ", ".join(CHANNELS)))
    if reference and not kit_track.REFERENCE.fullmatch(reference):
        problems.append(("reference", "must be 1-40 letters, digits, - _ # or ."))
    correspondence = None
    if letter:
        try:
            correspondence = _letter(root, letter, letters)
        except ValueError as exc:
            problems.append(("letter_record", str(exc)))
    if key in first_line:
        problems.append(("request", f"same request as line {first_line[key]}"))
    elif key is not None and key in requests:
        problems.append(("request", "already recorded for this agency"))
    if problems:
        return key, None, problems
    entry = {"imported": True, "sent_on": sent_on, "channel": channel}
    if reference:
        entry["reference"] = reference
    if correspondence:
        entry["correspondence"] = [correspondence]
    return key, entry, []


def import_sent(root, csv_path, *, apply=False, today=None):
    """Check every CSV row in file order; with ``apply`` record them all in one write.

    Returns ``{"checked": N, "imported": N if apply else 0, "written": bool(apply)}``; raises ``SentImportError``
    (exit 2, nothing written) naming each refused line and field without echoing any input value."""
    root, today = Path(root), today or date.today()
    rows = _read_rows(csv_path)
    agencies = _agencies(root)
    try:
        stored = kit_track.read_sent(root)
    except ValueError:  # reader text can embed stored request keys: never forward or inspect it
        raise SentImportError("kit/sent.json unreadable: invalid request data") from None
    except OSError:
        raise SentImportError("kit/sent.json cannot be read") from None
    requests = copy.deepcopy(stored)
    first_line, errors, letters, accepted = {}, [], {}, []
    for line, cells in rows:
        key, entry, problems = _check_row(root, cells, agencies, requests, first_line, today, letters)
        if problems:
            errors.extend(f"line {line}: {field}: {reason}" for field, reason in problems)
            continue
        first_line[key] = line
        requests[key] = entry
        accepted.append(key)
    if errors:
        more = f"\nand {len(errors) - SHOWN} more" if len(errors) > SHOWN else ""
        raise SentImportError(f"{len(errors)} problem(s); kit/sent.json was not changed:\n"
                              + "\n".join(errors[:SHOWN]) + more)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for key in accepted:
        requests[key] = {**requests[key], "recorded_at": stamp, "steps": []}
    document = {"schema_version": 1, "requests": dict(sorted(requests.items()))}
    text = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    try:  # a retained extension field may hold a lone surrogate the reader admitted from an ASCII escape
        encoded = text.encode("utf-8")
    except UnicodeEncodeError:
        raise SentImportError("kit/sent.json: import cannot be encoded as UTF-8; nothing was written") from None
    if len(encoded) > kit_track.MAX_BYTES:
        raise SentImportError("kit/sent.json would be larger than 1 MiB; nothing was written")
    if apply:
        try:
            kit._write_json(root / "kit" / "sent.json", document)
        except OSError:
            raise SentImportError("kit/sent.json could not be written; nothing was changed") from None
    return {"checked": len(rows), "imported": len(rows) if apply else 0, "written": bool(apply)}
