"""Private, read-only diff of two runs' outcome files (records-outcome-changes-v1).

``diff(previous_run_dir, current_run_dir)`` compares five fixed files of two ``run-outputs/<run_id>`` folders and
returns identities only (ids, codes, 12-hex prefixes): never a quote, name, status prose, path or full digest.
A section whose file is missing, invalid or of another schema on either side is ``"unavailable: <code>"``,
never an empty "no changes". The previous run must have started strictly earlier (by its engine run id, not
mtime). Directories and files are opened without following any symlink. Nothing is written.
"""
import argparse
import contextlib
import json
import os
import re
import stat
import sys
from datetime import date
from pathlib import Path

from .. import approval_queue as aq
from ..console_data_age import _run_start
from .factsheets import FIELDS

SCHEMA = "records-outcome-changes-v1"
MAX_FILE = 64 * 1024 * 1024
HEX64 = re.compile(r"[0-9a-f]{64}")
CODE = re.compile(r"[A-Za-z0-9_.:-]{1,80}")
STATUSES = ("meets", "gap", "unknown")


class ChangesError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class _Bad(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _need(ok):
    if not ok:
        raise _Bad("invalid")


def run_start(run_id):
    """Aware UTC start encoded in an engine run id, or None (legacy, malformed or impossible calendar)."""
    return _run_start(run_id) if isinstance(run_id, str) else None


def _id(value, nullable=False, cap=128):
    if value is None and nullable:
        return None
    _need(type(value) is str and 0 < len(value) <= cap and value.isprintable())
    return value


def _hex(value):
    _need(type(value) is str and HEX64.fullmatch(value) is not None)
    return value


def _out(value):
    return value[:12] if value is not None and HEX64.fullmatch(value) else value


def _sort(key):
    return tuple((v is not None, "" if v is None else v) for v in key)


def _dicts(value):
    _need(type(value) is list and all(type(row) is dict for row in value))
    return value


def _facts(data):
    """Set of (agency, field, document sha256, page); statements sharing a source position count once."""
    found, seen = set(), set()
    for agency in _dicts(data.get("agencies")):
        agency_id, fields = _id(agency.get("agency_id")), agency.get("fields")
        _need(agency_id not in seen and type(fields) is dict and set(fields) <= set(FIELDS))
        seen.add(agency_id)
        for field, entry in fields.items():
            _need(type(entry) is dict)
            for statement in _dicts(entry.get("statements")):
                cite = statement.get("citation")
                _need(type(cite) is dict)
                page = cite.get("page")
                _need(type(page) is int and page >= 1)
                found.add((agency_id, field, _hex(cite.get("document_sha256")), page))
    return found


def _comparison(data):
    rows = {}
    for row in _dicts(data.get("rows")):
        key = (_id(row.get("agency_id")), _id(row.get("field")), _id(row.get("request_id"), True))
        _need(key not in rows and row.get("status") in STATUSES and type(row.get("reason")) is str
              and CODE.fullmatch(row["reason"]) is not None)
        rows[key] = (row["status"], row["reason"])
    return rows


def _day(value):
    _need(value is None or (type(value) is str and len(value) == 10 and date.fromisoformat(value).isoformat() == value))
    return value


def _register(data):
    rows = {}
    for row in _dicts(data.get("requests")):
        agency = _id(row.get("agency_key"), cap=512 if str(row.get("agency_key")).startswith("tracker:") else 128)
        key = (agency, _id(row.get("request_id")))
        clock = row.get("deadline")
        _need(key not in rows and type(clock) is dict and type(clock.get("state")) is str
              and CODE.fullmatch(clock["state"]) is not None)
        rows[key] = (clock["state"], _day(clock.get("due")), _day(clock.get("determination_at")))
    return rows


def _received(data):
    found = set()
    for item in _dicts(data.get("items")):
        key = (_id(item.get("agency_id"), True), _hex(item.get("item_id")))
        _need(key not in found)
        found.add(key)
    return found


def _proposals(data):
    found = set()
    for agency in _dicts(data.get("agencies")):
        for request in _dicts(agency.get("requests")):
            for row in _dicts(request.get("proposals")):
                key = (_id(agency.get("agency_id")), _id(request.get("request_id"), True), _hex(row.get("proposal_id")))
                _need(key not in found and (row.get("agency_id"), row.get("request_id")) == key[:2]
                      and "agency_id" in row and "request_id" in row)
                found.add(key)
    return found


def _added_removed(row):
    return lambda old, new: {"added": [row(k) for k in sorted(new - old, key=_sort)],
                             "removed": [row(k) for k in sorted(old - new, key=_sort)]}


def _changed(row, snapshot):
    def compare(old, new):
        return {"changed": [dict(row(k), before=snapshot(old[k]), after=snapshot(new[k]))
                            for k in sorted(old.keys() & new.keys(), key=_sort) if old[k] != new[k]]}
    return compare


def _tracker(key):
    return None if key.startswith("tracker:") else _out(key)


SECTIONS = (
    ("facts", ("agency-factsheets.json",), "records-agency-factsheets-v1", True, _facts, _added_removed(
        lambda k: {"agency_id": _out(k[0]), "field": k[1], "document_prefix": k[2][:12], "page": k[3]})),
    ("comparison", ("comparison.json",), "records-agency-comparison-v1", True, _comparison, _changed(
        lambda k: {"agency_id": _out(k[0]), "field": _out(k[1]), "request_id": _out(k[2])},
        lambda v: {"status": v[0], "reason": v[1]})),
    ("register", ("CURRENT-REQUEST-REGISTER.json",), "records-request-register-v1", False, _register, _changed(
        lambda k: {"agency_id": _tracker(k[0]), "request_id": _out(k[1])},
        lambda v: {"state": v[0], "due": v[1], "determination_at": v[2]})),
    ("received", ("outcomes", "received-items.json"), "records-received-item-v1", True, _received,
     lambda old, new: {"added": [{"agency_id": _out(k[0]), "item_prefix": k[1][:12]} for k in sorted(new - old, key=_sort)]}),
    ("proposals", ("outcomes", "gap-proposals.json"), "records-gap-proposals-v1", True, _proposals, _added_removed(
        lambda k: {"agency_id": _out(k[0]), "request_id": _out(k[1]), "proposal_prefix": k[2][:12]})),
)


def _parts(value):
    path = Path(os.fspath(value))
    parts = (path if path.is_absolute() else Path(os.getcwd()) / path).parts
    if ".." in parts or len(parts) < 3 or parts[-2] != "run-outputs" or run_start(parts[-1]) is None:
        raise ChangesError("run_id_invalid")
    return parts


def _open_dir(parts):
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except OSError:
        os.close(fd)
        raise ChangesError("run_directory_unavailable") from None


def _load(run_fd, names, schema, run_id, project):
    """Projection of one file; _Bad(code) with missing, invalid, schema_mismatch or run_mismatch."""
    fds = []
    try:
        fd = run_fd
        for number, name in enumerate(names, 1):
            flags = os.O_RDONLY | os.O_NOFOLLOW | (os.O_NONBLOCK if number == len(names) else os.O_DIRECTORY)
            try:
                fd = os.open(name, flags, dir_fd=fd)
            except FileNotFoundError:
                raise _Bad("missing") from None
            except OSError:
                raise _Bad("invalid") from None
            fds.append(fd)
        _need(stat.S_ISREG(os.fstat(fd).st_mode))
        raw = aq._read_capped(fd, MAX_FILE)
        _need(len(raw) <= MAX_FILE)
        try:
            data = aq._strict_load(raw, "invalid")
        except aq.ApprovalQueueError:
            raise _Bad("invalid") from None
        _need(type(data) is dict and type(data.get("schema")) is str)
        if data["schema"] != schema:
            raise _Bad("schema_mismatch")
        if run_id is not None:
            _need(type(data.get("run_id")) is str)
            if data["run_id"] != run_id:
                raise _Bad("run_mismatch")
        try:
            return project(data)
        except (KeyError, TypeError, ValueError, AttributeError):
            raise _Bad("invalid") from None
    except OSError:
        raise _Bad("invalid") from None
    finally:
        for fd in fds:
            os.close(fd)


def diff(previous_run_dir, current_run_dir):
    previous, current = _parts(previous_run_dir), _parts(current_run_dir)
    if run_start(previous[-1]) >= run_start(current[-1]):  # equal is not older; the suffix is never chronology
        raise ChangesError("run_order_invalid")
    result = {"schema": SCHEMA, "previous_run_id": previous[-1], "current_run_id": current[-1]}
    with contextlib.ExitStack() as stack:
        sides = []
        for side, parts in (("previous", previous), ("current", current)):
            fd = _open_dir(parts)
            stack.callback(os.close, fd)
            sides.append((side, fd, parts[-1]))
        for name, files, schema, bound, project, compare in SECTIONS:
            try:
                loaded = []
                for side, fd, run_id in sides:
                    try:
                        loaded.append(_load(fd, files, schema, run_id if bound else None, project))
                    except _Bad as error:
                        raise _Bad(side + "_" + error.code) from None
                result[name] = compare(*loaded)
            except _Bad as error:
                result[name] = "unavailable: " + error.code
    return result


def _cell(value):
    return "-" if value is None else str(value)


def render(result):
    lines = [SCHEMA, "previous: " + result["previous_run_id"], "current: " + result["current_run_id"]]
    for name, *_ in SECTIONS:
        section = result[name]
        if isinstance(section, str):
            lines.append(name + ": " + section)
            continue
        counts = ", ".join(f"{len(rows)} {kind}" for kind, rows in section.items() if rows)
        lines.append(name + ": " + (counts or "no changes"))
        for kind, rows in section.items():
            for row in rows:
                ids = " ".join(_cell(v) for v in row.values() if type(v) is not dict)
                states = " -> ".join("/".join(_cell(v) for v in row[k].values()) for k in ("before", "after") if k in row)
                lines.append("  " + {"added": "+", "removed": "-", "changed": "~"}[kind] + " " + ids + (" " + states if states else ""))
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="records outcomes-diff", description="Diff two runs' outcome files (identities only; read-only).")
    parser.add_argument("--previous", required=True, metavar="DIR", help="older run-outputs/<run_id> folder")
    parser.add_argument("--current", required=True, metavar="DIR", help="newer run-outputs/<run_id> folder")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = diff(args.previous, args.current)
    except ChangesError as error:
        print(json.dumps({"error": error.code}) if args.json else "refused: " + error.code)
        return 1
    print(json.dumps(result, sort_keys=True) if args.json else render(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
