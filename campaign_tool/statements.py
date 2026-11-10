"""DF1109-13: ``statement add|remove|list`` maintain ``content/statements.json`` without hand-editing.

The store is the one the site build reads (DF1107-07, ``content_statements``): a JSON array of at most 200 rows, each an
organizer-verified public statement of an official already listed in ``content/officials.json``. Every change is
validated as a whole prospective file with the build's own ``check_statements`` (its shared privacy lint included)
before one atomic write in ``content/``; a refusal leaves the old bytes, and nothing creates the file but a first add.

* add appends one row. The quote is read from ``--quote-file`` exactly as stored on disk (UTF-8, no newline
  translation, nothing stripped or normalized), so a final line break is part of the quote and refused by the quote
  rule. The official must be on the current roster (the build's 365-day rule applies); an exact duplicate row
  (same keys and values, key order ignored) is refused.
* remove drops the row at its 1-based position in stored order (the ``index`` that list prints); the last removal
  leaves ``[]``.
* list prints the rows newest first (``said_on`` descending, equal days in stored order), each with its stored index.
  It writes nothing.

Nothing is fetched: a source address is the organizer's record of where they checked. Refusals name the file, row,
flag or field and a fixed reason, never a supplied quote, note or address. One organizer writes at a time; the target
is re-checked just before the rename, with no further concurrent-writer promise.
"""
import json
import os
import re
import stat
from datetime import date
from pathlib import Path
from typing import NoReturn

from .content import ContentError, load_officials
from .content_statements import FILE, MAX_BYTES, ROW_KEYS, check_statements, newest_first
from .officials_cmd import _content_fd, _folder_identity, _identity, _parse, _read, _state

LABEL = "content/" + FILE
UNTOUCHED = "nothing was written"
FOLDER_CHANGED = "content/: changed while it was being edited; " + UNTOUCHED
MAX_QUOTE_FILE = 4096  # 400 characters of at most 4 UTF-8 bytes each, with room to spare
FLAGS = {key: "--" + key.replace("_", "-") for key in ROW_KEYS} | {"quote": "--quote-file"}
POSITION = re.compile(r"[1-9][0-9]*", re.ASCII)


class StatementError(ContentError):
    """The store, or a requested change to it, is not acceptable; the message never repeats a supplied value."""
    exit_code = 2


def _refuse(message) -> NoReturn:
    raise StatementError(f"{message}; {UNTOUCHED}")


def _first(problems, rename=None) -> NoReturn:
    """One refusal line from the loader's problem list, optionally naming flags instead of the prospective row."""
    message = problems[0]
    for key in sorted(rename or (), key=len, reverse=True):  # context_note before context
        message = message.replace(f"{FILE}[{rename[key][0]}].{key}", rename[key][1])
    more = f" (and {len(problems) - 1} more problem(s))" if len(problems) > 1 else ""
    _refuse(message + more)


# --- reading -----------------------------------------------------------------------------------------------------

def _roster(root, today, *, fresh, with_folder=False, expected_folder=None):
    """The validated ``{agency_id: [person, ...]}`` roster the build would use; refused when there is none.

    ``with_folder`` returns ``(roster, folder identity)``: the (device, inode) of the very content/ folder the site,
    agencies and roster were read from. ``expected_folder`` refuses a roster read from any other folder."""
    try:
        agencies, document, loaded, folder = _state(root, today, with_folder_identity=True)
        if expected_folder is not None and folder != expected_folder:
            raise StatementError(FOLDER_CHANGED)
        if loaded is None:
            _refuse("content/officials.json: does not exist; list the official with officials add first")
        roster = load_officials(document, agencies, today, require_fresh=fresh)
        return (roster, folder) if with_folder else roster
    except StatementError:
        raise
    except ContentError as exc:
        raise StatementError(f"{exc}; {UNTOUCHED}") from None


def _store(root, *, with_folder=False, expected_folder=None):
    """-> (parsed document, (identity, mode, bytes)) or (None, None) when there is no store file. Never creates it.

    ``with_folder`` appends the (device, inode) of the content/ folder descriptor the store was read from, also when
    there is no store file. ``expected_folder`` refuses, before reading, a folder other than the one validated."""
    try:
        folder = _content_fd(root)
        try:
            identity = _folder_identity(folder)
            if expected_folder is not None and identity != expected_folder:
                raise StatementError(FOLDER_CHANGED)
            found = _read(folder, FILE, LABEL)
        finally:
            os.close(folder)
        if found is None:
            result = (None, None)
        else:
            result = (_parse(found[0], LABEL), (_identity(found[1]), stat.S_IMODE(found[1].st_mode), found[0]))
        return (*result, identity) if with_folder else result
    except StatementError:
        raise
    except ContentError as exc:
        raise StatementError(f"{exc}; {UNTOUCHED}") from None


def _validated(document, roster, today):
    rows, problems = check_statements(document, roster, today)
    if problems:
        _first(problems)
    return rows


def _quote_file(path):
    """The exact text of one small regular UTF-8 file; a link, FIFO, folder or oversized file is refused unread."""
    try:
        info = os.lstat(path)
    except OSError:
        _refuse("--quote-file: cannot be read")
    if stat.S_ISLNK(info.st_mode):
        _refuse("--quote-file: is a symbolic link; links are never followed")
    if not stat.S_ISREG(info.st_mode):
        _refuse("--quote-file: must be a regular file")
    if info.st_size > MAX_QUOTE_FILE:
        _refuse("--quote-file: file too large for a quote of at most 400 characters")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            opened = os.fstat(handle.fileno())
            data = handle.read(MAX_QUOTE_FILE + 1)
    except OSError:
        _refuse("--quote-file: cannot be read")
    if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
        _refuse("--quote-file: changed while it was being read")
    if len(data) > MAX_QUOTE_FILE:
        _refuse("--quote-file: file too large for a quote of at most 400 characters")
    try:
        return data.decode("utf-8")  # bytes -> text only: no newline translation, strip or normalization
    except UnicodeDecodeError:
        _refuse("--quote-file: not valid UTF-8 text")


# --- writing -----------------------------------------------------------------------------------------------------

def _dump(document):
    data = (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    if len(data) > MAX_BYTES:
        _refuse(f"{LABEL}: would be larger than {MAX_BYTES // (1024 * 1024)} MiB")
    return data


def _folder_changed(root, expected_folder):
    """Refuse unless root/content is still, as a real folder, the one whose files were read (never follows a link)."""
    try:
        check = os.open(Path(root) / "content", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        raise StatementError(FOLDER_CHANGED) from None
    try:
        same = _folder_identity(check) == expected_folder
    except OSError:
        same = False
    finally:
        os.close(check)
    if not same:
        raise StatementError(FOLDER_CHANGED)


def _install(root, data, loaded, *, expected_folder):
    """Stage ``data`` beside the store, fsync, then publish it with one rename (or a no-replace link for the first
    file). The folder must still be the very content/ folder whose roster and store were validated (device and inode
    only, checked before staging and again before publishing), and the target must still be the very file read, with
    the same bytes, or still absent; every ordinary failure keeps the old bytes and mode and removes the staged copy.
    Not a promise against a namespace change after the final check."""
    try:
        folder = _content_fd(root)
    except ContentError:
        raise StatementError(FOLDER_CHANGED) from None
    temp = f".statements-{os.getpid()}-{os.urandom(4).hex()}.tmp"
    staged = committed = False
    try:
        try:
            moved = _folder_identity(folder) != expected_folder
        except OSError:
            moved = True
        if moved:
            raise StatementError(FOLDER_CHANGED)
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=folder)
        staged = True
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), 0o644 if loaded is None else loaded[1])
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _folder_changed(root, expected_folder)
        try:
            current = _read(folder, FILE, LABEL)
        except ContentError:
            raise FileExistsError from None
        if loaded is None:
            if current is not None:
                raise FileExistsError
            os.link(temp, FILE, src_dir_fd=folder, dst_dir_fd=folder, follow_symlinks=False)  # never replaces
        else:
            if current is None or (_identity(current[1]), current[0]) != (loaded[0], loaded[2]):
                raise FileExistsError
            os.replace(temp, FILE, src_dir_fd=folder, dst_dir_fd=folder)
            staged = False
        committed = True
        os.fsync(folder)
    except FileExistsError:
        raise StatementError(f"{LABEL}: changed while it was being edited; {UNTOUCHED}") from None
    except OSError:
        if committed:
            raise StatementError(f"{LABEL}: the new file was written but the folder could not be flushed to disk; "
                                 "check it with statement list before relying on it") from None
        raise StatementError(f"{LABEL}: write failed; the previous file was kept") from None
    finally:
        if staged:
            try:
                os.unlink(temp, dir_fd=folder)
            except OSError:
                pass
        os.close(folder)


# --- commands ----------------------------------------------------------------------------------------------------

def add_statement(root, *, agency_id, official, said_on, quote_file, context, source_url, checked_on,
                  recording_time=None, context_note=None, today=None):
    """Append one organizer-verified statement. -> {"action": "added", "index": its 1-based stored position}."""
    today = today or date.today()
    roster, folder = _roster(root, today, fresh=True, with_folder=True)
    document, loaded = _store(root, expected_folder=folder)
    if loaded is None:
        document = []
    else:
        _validated(document, roster, today)
    row = {"agency_id": agency_id, "official": official, "said_on": said_on, "quote": _quote_file(quote_file),
           "context": context, "source_url": source_url, "checked_on": checked_on}
    row |= {key: value for key, value in (("recording_time", recording_time), ("context_note", context_note))
            if value is not None}
    _, problems = check_statements([row], roster, today)
    if problems:
        _first(problems, {key: (0, FLAGS[key]) for key in ROW_KEYS})
    for position, stored in enumerate(document, 1):
        if stored == row:
            _refuse(f"{LABEL}: the same statement is already stored at index {position}")
    document = document + [row]
    _validated(document, roster, today)
    _install(root, _dump(document), loaded, expected_folder=folder)
    return {"action": "added", "index": len(document)}


def _position(index):
    if isinstance(index, bool) or not isinstance(index, (int, str)):
        _refuse("--index: must be a whole number from 1 (see statement list)")
    if isinstance(index, int):
        index = str(index)
    if not POSITION.fullmatch(index):
        _refuse("--index: must be a whole number from 1 (see statement list)")
    return int(index) if len(index) <= 6 else 10 ** 6


def remove_statement(root, *, index, today=None):
    """Drop the row at a 1-based stored position. -> {"action": "removed", "index": that position}."""
    today = today or date.today()
    position = _position(index)
    document, loaded, folder = _store(root, with_folder=True)
    if loaded is None:
        _refuse(f"{LABEL}: does not exist; there is nothing to remove")
    if isinstance(document, list) and document:
        roster = _roster(root, today, fresh=False, expected_folder=folder)
    else:
        roster = None
    _validated(document, roster, today)
    if position > len(document):
        _refuse(f"--index: out of range; {LABEL} has {len(document)} statement(s)")
    document = document[:position - 1] + document[position:]
    _validated(document, roster, today)
    _install(root, _dump(document), loaded, expected_folder=folder)
    return {"action": "removed", "index": position}


def list_statements(root, *, today=None):
    """Validated rows newest first, each as ``{"index": stored 1-based position, **row}``; [] without a file."""
    today = today or date.today()
    document, loaded = _store(root)
    if loaded is None:
        return []
    roster = _roster(root, today, fresh=False) if isinstance(document, list) and document else None
    rows = _validated(document, roster, today)
    numbered = [{"index": position, **row} for position, row in enumerate(rows, 1)]
    return newest_first(numbered)


def format_statements(rows):
    return json.dumps(rows, indent=2, ensure_ascii=False) + "\n"


def run_cli(root, args):
    """-> the text the CLI prints for one ``statement`` verb (date.today(); no override)."""
    if args.statement_command == "list":
        return format_statements(list_statements(root))
    if args.statement_command == "add":
        done = add_statement(root, agency_id=args.agency_id, official=args.official, said_on=args.said_on,
                             quote_file=Path(args.quote_file).expanduser(), context=args.context,
                             source_url=args.source_url, checked_on=args.checked_on,
                             recording_time=args.recording_time, context_note=args.context_note)
    else:
        done = remove_statement(root, index=args.index)
    return json.dumps(done, separators=(",", ":")) + "\n"
