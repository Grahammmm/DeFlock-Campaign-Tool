"""Record an organizer-transcribed agency timeline (a portal or letter history) from one CSV in one checked write.

Every row goes through ``kit_track.record_step(..., dry_run=True)`` in file order against the recorded steps plus the
earlier accepted rows, so the step rules are never copied here. Nothing is fetched or sent; a refusal writes nothing.
After each new row the whole sent store as it would be written is measured against ``kit_track.MAX_BYTES``, the
ceiling ``read_sent`` enforces, so no import (preview or apply) may replace a readable store with an unreadable one.
The selected request's existing history is replay-validated (``kit_track.validate_steps``) before import or replay.
See docs/KIT-RESPONSE-IMPORT.md for the replay identity, including the extension dispatch date.
"""
import csv
import io
import json
import os
import stat
import tempfile
from datetime import date
from pathlib import Path

from . import kit, kit_track

COLUMNS = ("date", "kind", "note", "amount", "expected_on", "partial")
MAX_BYTES, MAX_ROWS, SHOWN = 256 * 1024, 200, 20
SKIPPED = "already recorded, skipped"


class ImportRefused(ValueError):
    exit_code = 2


def _read_rows(csv_path):  # [(physical line, cells)] of the non-blank data rows; the file is checked before any row
    try:
        fd = os.open(csv_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:  # O_NOFOLLOW: a symlink fails here with ELOOP
        raise ImportRefused(f"csv cannot be opened as a regular file (symlinks are refused): {exc.strerror}") from None
    # Own the raw fd until wrapping succeeds; parse only after read AND close succeed.
    try:
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise ImportRefused("csv must be a regular file")
            if info.st_size > MAX_BYTES:
                raise ImportRefused("csv is larger than 256 KiB")
            handle = os.fdopen(fd, "rb")
        except BaseException:
            os.close(fd)  # still ours; never retry an uncertain close
            raise
        with handle:  # ownership transferred: only the stream closes this fd
            data = handle.read(MAX_BYTES + 1)
    except OSError:
        raise ImportRefused("csv cannot be read; nothing was written") from None
    if len(data) > MAX_BYTES:
        raise ImportRefused("csv is larger than 256 KiB")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ImportRefused("csv is not UTF-8") from None
    # strict: broken quoting is refused, never repaired; the whole file is parsed before any row is used
    reader, rows, start = csv.reader(io.StringIO(text, newline=""), strict=True), [], 1
    try:
        for cells in reader:
            if cells:
                rows.append((start, [cell.strip() for cell in cells]))
            start = reader.line_num + 1
    except csv.Error:  # start is the record's first physical line; the parser's message may quote the input
        raise ImportRefused(f"csv line {start}: malformed CSV (quotes or field size)") from None
    if rows:
        _check_header(rows[0][1])
    if len(rows) < 2:
        raise ImportRefused("csv has no rows")
    if len(rows) - 1 > MAX_ROWS:
        raise ImportRefused(f"csv has {len(rows) - 1} rows; at most {MAX_ROWS} rows")
    return rows[1:]


def _shown(cell):  # a header cell echoed safely: printable ASCII only, short
    return repr("".join(c if 32 <= ord(c) < 127 else "?" for c in cell[:40]))


def _check_header(header):
    if header == list(COLUMNS):
        return
    parts = [f"missing column {c}" for c in COLUMNS if c not in header]
    parts += [f"unexpected column {_shown(c)}" for c in header if c not in COLUMNS]
    if len(set(header)) != len(header):
        parts.append("a column is repeated")
    if not parts:
        parts = ["columns are out of order"]
    raise ImportRefused("csv header must be exactly " + ",".join(COLUMNS) + ": " + "; ".join(parts))


def _signature(on, kind, note, amount, expected_on, partial):  # a row or a recorded step in one comparable form
    try:
        note = kit_track._clean_note(note) if note else None
    except ValueError:
        pass  # kept raw: it equals no recorded step, and record_step refuses it with its own message
    if isinstance(amount, str):
        try:
            amount = kit_track._amount_cents(amount)
        except ValueError:
            pass
    return (on, kind, note, amount, expected_on or None, bool(partial))


def _step_signature(step):  # the CSV's expected_on cell is the dispatch date on an extension, a production date otherwise
    dated = step.get("extension_expected_on") if step.get("kind") == "extension" else step.get("expected_on")
    return _signature(step.get("on"), step.get("kind"), step.get("note"), step.get("amount_cents"),
                      dated, step.get("partial") is True)


def _row(cells):  # (date, kind, record_step keywords, signature); ValueError when the row is malformed
    if len(cells) != len(COLUMNS):
        raise ValueError(f"expected {len(COLUMNS)} cells, found {len(cells)}")
    on, kind, note, amount, expected_on, partial = cells
    if partial not in ("", "yes", "no"):
        raise ValueError("partial must be empty, yes or no")
    keywords = {"note": note or None, "amount": amount or None, "expected_on": expected_on or None,
                "partial": partial == "yes"}
    return on, kind, keywords, _signature(on, kind, note, amount or None, expected_on, partial == "yes")


def _document(requests):  # the complete document the writer persists
    return {"schema_version": 1, "requests": dict(sorted(requests.items()))}


def _check_capacity(requests):  # refuse a store the writer would produce but read_sent would refuse (pure, no I/O)
    try:
        size = len((json.dumps(_document(requests), indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    except UnicodeEncodeError:
        raise ImportRefused("kit/sent.json: import cannot be encoded as UTF-8; nothing was written") from None
    if size > kit_track.MAX_BYTES:
        raise ImportRefused("kit/sent.json: import would exceed 1 MiB; nothing was written")


def _write(path, requests):  # the existing atomic writer
    kit._write_json(path, _document(requests))


def import_steps(root, agency_id, csv_path, *, request="main", apply=False, today=None):
    """Check every CSV row with the kit-response rules in file order; with ``apply`` record them all in one write.

    Returns a report dict; raises ``ImportRefused`` (exit 2, nothing written) naming each refused line."""
    root = Path(root)
    rows = _read_rows(csv_path)
    today = today or date.today()  # one effective day for the history check and every row dry run
    try:
        request = kit_track._request(request)
        requests = kit_track.read_sent(root)
        key = kit_track._key(agency_id, request)
        if "#" in agency_id or key not in requests:  # no such sent request: record_step's own message
            kit_track.record_step(root, agency_id, "2000-01-01", "acknowledged", request=request, today=today,
                                  dry_run=True)
    except ValueError as exc:
        raise ImportRefused(str(exc)) from None
    try:
        kit_track.validate_steps(requests[key], kit_track._label(agency_id, request), today=today)
    except ValueError:
        raise ImportRefused("kit/sent.json: selected request history is invalid; nothing was written") from None
    recorded = {_step_signature(s) for s in requests[key]["steps"]}
    first_line, errors, added, skipped = {}, [], [], []
    with tempfile.TemporaryDirectory(prefix="kit-import-") as scratch:  # the working copy record_step dry-runs against
        working = Path(scratch) / "kit" / "sent.json"
        working.parent.mkdir()
        _write(working, requests)
        for line, cells in rows:
            try:
                on, kind, keywords, signature = _row(cells)
                if signature in first_line:
                    raise ValueError(f"same as line {first_line[signature]}")
                first_line[signature] = line
                if signature in recorded:
                    skipped.append(line)
                    continue
                step = kit_track.record_step(scratch, agency_id, on, kind, request=request, today=today,
                                             dry_run=True, **keywords)["steps"][-1]
            except ValueError as exc:
                errors.append(f"line {line}: {exc}")
                continue
            requests[key]["steps"].append(step)  # later rows see this one; a refused row is never added
            added.append((line, step))
            _check_capacity(requests)  # every new row, the last included, before anything is persisted
            _write(working, requests)
    if errors:
        more = f"\nand {len(errors) - SHOWN} more" if len(errors) > SHOWN else ""
        raise ImportRefused(f"{len(errors)} bad row(s); kit/sent.json was not changed:\n"
                            + "\n".join(errors[:SHOWN]) + more)
    if apply and added:
        _write(root / "kit" / "sent.json", requests)
    return {"agency_id": agency_id, "request": request, "applied": bool(apply and added), "rows": len(rows),
            "added": len(added), "skipped": len(skipped), "lines": [f"line {line}: {SKIPPED}" for line in skipped],
            "steps": [{"line": line, "on": step["on"], "kind": step["kind"]} for line, step in added]}


def render(report):
    return json.dumps(report, indent=2)
