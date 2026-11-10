"""Source-linked agency actions taken outside a recorded meeting outcome (DF0237-01).

``content/agency-actions.json`` is ``{"schema_version": 1, "actions": [ROW, ...]}``. An organizer transcribes what an
agency's own official source says changed (a policy adopted, sharing ended, a contract signed, ...) with the date it took
effect, the words as the source states them, the https address of that source and the day the organizer checked it.
The software never fetches or attests the source, never infers an action, and the site credits nobody with a change.

Every refusal names ``content/agency-actions.json.actions[N].FIELD`` (N counts from 0 in file order, as the other content
diagnostics do) and never repeats a supplied statement, address or unknown key. The one file is edited by ``add_action`` /
``remove_action`` only: the whole store is validated before and after the change, the new bytes are written to a temporary
file beside it and swapped in with one rename, and any ordinary failure leaves the old bytes. One organizer writes at a time;
there is no concurrent-writer promise.
"""
import json
import os
import re
import stat
import tempfile
import unicodedata
from contextlib import ExitStack
from datetime import date
from pathlib import Path

from .content import AGENCY_ID, ContentError, _calendar_day, _https

ACTION_KINDS = ("policy_adopted", "policy_amended", "policy_posted", "sharing_started", "sharing_ended",
                "retention_changed", "contract_signed", "contract_renewed", "contract_ended", "contract_paused",
                "cameras_added", "cameras_removed", "other")
ROW_KEYS = ("agency_id", "on", "kind", "as_stated", "source_url", "checked_on")
PARTS = ("content", "agency-actions.json")
LABEL = "/".join(PARTS)
MAX_BYTES = 2 * 1024 * 1024
MAX_PER_AGENCY = 50
MAX_STATEMENT = 200
MAX_URL = 500
DATE_SHAPE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", re.ASCII)
INDEX_SHAPE = re.compile(r"[1-9][0-9]{0,8}", re.ASCII)
BAD_CATEGORIES = ("Cc", "Cf", "Cs", "Zl", "Zp")  # controls, format/bidi, lone surrogates, line and paragraph separators


class ActionError(ContentError):
    """The action store, or a requested change to it, is not acceptable; the message never repeats a supplied value."""


class _Duplicate(ValueError):
    pass


# --- validation -----------------------------------------------------------------------------------------------

def _day(value):
    """The date for a canonical real YYYY-MM-DD string, else None (the value is never repeated)."""
    if not (isinstance(value, str) and value.isascii() and DATE_SHAPE.fullmatch(value)):
        return None
    try:
        return _calendar_day(value, "day")
    except (ContentError, ValueError):
        return None


def _statement_problem(value):
    if not isinstance(value, str):
        return "must be text"
    if not 1 <= len(value) <= MAX_STATEMENT:
        return f"must be 1 to {MAX_STATEMENT} characters"
    if value != value.strip():
        return "must not start or end with whitespace"
    if "<" in value or ">" in value or any(unicodedata.category(c) in BAD_CATEGORIES for c in value):
        return "must be plain text without angle brackets, control, invisible or line-separator characters"
    return None


def _url_problem(value):
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_URL:
        return f"must be an https:// URL of 1 to {MAX_URL} characters"
    try:
        _https(value, "url")
    except ContentError:
        return "must be an https:// URL"
    return None


def _row_problems(raw, where, known, today):
    """-> (problem lines, canonical row or None) for one stored row."""
    if not isinstance(raw, dict):
        return [f"{where}: must be an object"], None
    found = [f"{where}.{key}: missing" for key in ROW_KEYS if key not in raw]
    found += [f"{where}: unknown field not allowed" for key in raw if key not in ROW_KEYS]
    if found:
        return found, None
    problems = []
    agency = raw["agency_id"]
    if not isinstance(agency, str) or not AGENCY_ID.fullmatch(agency):
        problems.append(f"{where}.agency_id: invalid agency identifier")
    elif known is not None and agency not in known:
        problems.append(f"{where}.agency_id: unknown agency {agency}")
    if not isinstance(raw["kind"], str) or raw["kind"] not in ACTION_KINDS:
        problems.append(f"{where}.kind: choose one of {list(ACTION_KINDS)}")
    on, checked = _day(raw["on"]), _day(raw["checked_on"])
    if on is None:
        problems.append(f"{where}.on: must be a real YYYY-MM-DD date")
    if checked is None:
        problems.append(f"{where}.checked_on: must be a real YYYY-MM-DD date")
    if on is not None and checked is not None:
        if on > checked:
            problems.append(f"{where}.on: must not be after checked_on")
        if checked > today:
            problems.append(f"{where}.checked_on: must not be in the future")
    for key, check in (("as_stated", _statement_problem), ("source_url", _url_problem)):
        message = check(raw[key])
        if message:
            problems.append(f"{where}.{key}: {message}")
    if problems:
        return problems, None
    return [], {key: raw[key] for key in ROW_KEYS}


def check_actions(document, agencies, *, today=None):
    """-> (validated rows in stored order, problem lines). ``agencies`` None skips only the known-agency check."""
    today = today or date.today()
    if not isinstance(document, dict) or set(document) != {"schema_version", "actions"}:
        return [], [f"{LABEL}: must be an object with exactly schema_version and actions"]
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        return [], [f"{LABEL}.schema_version: must be 1"]
    items = document["actions"]
    if not isinstance(items, list):
        return [], [f"{LABEL}.actions: must be a list"]
    known = None if agencies is None else {agency["agency_id"] for agency in agencies}
    rows, problems, seen, counts = [], [], set(), {}
    for index, raw in enumerate(items):
        where = f"{LABEL}.actions[{index}]"
        found, row = _row_problems(raw, where, known, today)
        agency = raw.get("agency_id") if isinstance(raw, dict) else None
        if isinstance(agency, str) and AGENCY_ID.fullmatch(agency):
            counts[agency] = counts.get(agency, 0) + 1
            if counts[agency] == MAX_PER_AGENCY + 1:
                found.append(f"{where}.agency_id: more than {MAX_PER_AGENCY} actions for {agency}")
            if row is not None:
                key = (agency, row["on"], row["kind"], row["source_url"])
                if key in seen:
                    found.append(f"{where}.agency_id: duplicate agency, date, kind and source for {agency}")
                seen.add(key)
        problems += found
        if row is not None and not found:
            rows.append(row)
    return rows, problems


def validate_actions(document, agencies, *, today=None):
    """A fresh list of validated rows (stored order), or ActionError naming the first problem."""
    rows, problems = check_actions(document, agencies, today=today)
    if problems:
        raise ActionError(problems[0])
    return rows


# --- reading --------------------------------------------------------------------------------------------------

def _hook(pairs):
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise _Duplicate("duplicate key")
        seen[key] = value
    return seen


def _refuse_constant(_name):
    raise ValueError("not a JSON number")


def _parse(data):
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_hook, parse_constant=_refuse_constant)
    except _Duplicate:
        raise ActionError(f"{LABEL}: a JSON object repeats a key") from None
    except (ValueError, RecursionError):
        raise ActionError(f"{LABEL}: not valid UTF-8 JSON") from None


def _entry(directory, name, where):
    """No-follow metadata admission; an absent entry is not a dangling link."""
    try:
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError:
        raise ActionError(f"{where}: cannot be inspected") from None
    if stat.S_ISLNK(info.st_mode):
        raise ActionError(f"{where}: is a symbolic link; links are never followed")
    if name == "content":
        if not stat.S_ISDIR(info.st_mode):
            raise ActionError(f"{where}: must be a folder")
    elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ActionError(f"{where}: must be a regular file with a single link")
    return info


def _directory_identity(info):
    return None if info is None else (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode))


def _check_content(root_fd, admitted):
    """Recheck only the entry in the pinned root, never a substituted parent's target."""
    try:
        current = os.stat("content", dir_fd=root_fd, follow_symlinks=False)
    except FileNotFoundError:
        current = None
    except OSError:
        raise ActionError(f"{LABEL}: changed while it was being read") from None
    if _directory_identity(current) != _directory_identity(admitted):
        raise ActionError(f"{LABEL}: changed while it was being read")


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_nlink)


def _load(root):
    """Bounded bytes, identity and mode, or honest absence under stable parents.

    The supplied root is the trust anchor (its own symlinks are allowed). All
    subsequent opens are descriptor-relative and no-follow. This confines reads,
    not the writer's separate pathname-based staging/replacement operations.
    """
    with ExitStack() as stack:
        try:
            try:
                root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            except FileNotFoundError:
                return None
            stack.callback(os.close, root_fd)
            admitted = _entry(root_fd, "content", "content")
            if admitted is None:
                _check_content(root_fd, None)
                return None
            try:
                content_fd = os.open("content", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                     dir_fd=root_fd)
            except OSError:
                _check_content(root_fd, admitted)
                raise ActionError(f"{LABEL}: cannot be read") from None
            stack.callback(os.close, content_fd)
            if _directory_identity(os.fstat(content_fd)) != _directory_identity(admitted):
                raise ActionError(f"{LABEL}: changed while it was being read")
            _check_content(root_fd, admitted)
            if _entry(content_fd, PARTS[1], LABEL) is None:
                _check_content(root_fd, admitted)
                return None
            fd = os.open(PARTS[1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=content_fd)
            stack.callback(os.close, fd)
            with os.fdopen(fd, "rb", closefd=False) as handle:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ActionError(f"{LABEL}: must be a regular file with a single link")
                data = handle.read(MAX_BYTES + 1)
            _check_content(root_fd, admitted)
            if len(data) > MAX_BYTES:
                raise ActionError(f"{LABEL}: file too large")
            return data, _identity(info), stat.S_IMODE(info.st_mode)
        except OSError:
            raise ActionError(f"{LABEL}: cannot be read") from None


def read_actions(root, agencies, *, today=None):
    """The validated rows of ``root``'s store in stored order, or None when there is no file."""
    loaded = _load(root)
    if loaded is None:
        return None
    return validate_actions(_parse(loaded[0]), agencies, today=today)


def action_problems(root, agencies, *, today=None):
    """Every problem line of the store (content-check); [] when absent or valid. ``agencies`` None skips the id check."""
    try:
        loaded = _load(root)
        if loaded is None:
            return []
        return check_actions(_parse(loaded[0]), agencies, today=today)[1]
    except ActionError as exc:
        return [str(exc)]


def by_agency(rows):
    """{agency_id: rows without agency_id in stored order} for the loaded content model."""
    grouped = {}
    for row in rows or []:
        grouped.setdefault(row["agency_id"], []).append({key: row[key] for key in ROW_KEYS if key != "agency_id"})
    return grouped


class SelectorError(ActionError):
    """``list --agency`` names no loaded agency; the supplied value is never repeated."""


def list_actions(root, agency_id=None, *, today=None):
    """DF1109B-05, read only: every stored action as a flat dict, agencies in order of first appearance in the file, each
    agency's rows in stored order with ``index`` the 1-based position ``remove --index`` takes (counted before the
    ``agency_id`` filter). The whole store is validated by the build's reader first; [] when there is no file."""
    from .content import load_campaign_agencies  # local: content owns the agency loaders
    from .labels import DEFAULTS
    today = today or date.today()
    agencies = load_campaign_agencies(root, today)
    grouped = by_agency(read_actions(root, agencies, today=today))
    if agency_id is not None and (not isinstance(agency_id, str) or not AGENCY_ID.fullmatch(agency_id)
                                  or agency_id not in {agency["agency_id"] for agency in agencies}):
        raise SelectorError("--agency: unknown agency")
    return [{"agency_id": agency, "index": index, "on": row["on"], "kind": row["kind"],
             "kind_label": DEFAULTS["agency.action_kind_" + row["kind"]], "as_stated": row["as_stated"],
             "source_url": row["source_url"], "checked_on": row["checked_on"]}
            for agency, rows in grouped.items() if agency_id in (None, agency)
            for index, row in enumerate(rows, 1)]


# --- writing --------------------------------------------------------------------------------------------------

def _dump(rows):
    document = {"schema_version": 1, "actions": [{key: row[key] for key in ROW_KEYS} for row in rows]}
    data = (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    if len(data) > MAX_BYTES:
        raise ActionError(f"{LABEL}: would be larger than {MAX_BYTES // 1024} KiB; nothing was written")
    return data


def _state(root, today):
    """-> (agencies, rows, loaded) for a whole, valid store; ActionError otherwise."""
    from .content import load_campaign_agencies  # local: content owns the agency loaders
    root = Path(root)
    content = root / "content"
    if not content.is_dir() or content.is_symlink():
        raise ActionError("content/ must be a real folder of this campaign; nothing was written")
    agencies = load_campaign_agencies(root, today)
    loaded = _load(root)
    rows = [] if loaded is None else validate_actions(_parse(loaded[0]), agencies, today=today)
    return agencies, rows, loaded


def _install(root, data, loaded):
    """Write ``data`` beside the target and rename it over; the old bytes survive every ordinary failure."""
    target = Path(root).joinpath(*PARTS)
    mode = 0o644 if loaded is None else loaded[2]
    name = None
    try:
        fd, name = tempfile.mkstemp(dir=target.parent, prefix=".agency-actions-", suffix=".tmp")
        with os.fdopen(fd, "wb") as handle:  # closes the descriptor on every path
            os.fchmod(handle.fileno(), mode)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        # Recheck the target is still the very file read (or still absent) just before it is replaced.
        current = _load(root)
        if (current is None) != (loaded is None) or (current is not None and current[1] != loaded[1]):
            raise ActionError(f"{LABEL}: changed while it was being edited; nothing was written")
        os.replace(name, target)
        name = None
    except ActionError:
        raise
    except OSError:
        raise ActionError(f"{LABEL}: write failed; the previous file was kept") from None
    finally:
        if name is not None:
            try:
                os.unlink(name)
            except OSError:
                pass


def add_action(root, agency_id, *, on, kind, as_stated, source_url, checked_on, today=None):
    """Append one organizer-transcribed action. -> {"agency_id", "index" (1-based among that agency's rows), "notes"}."""
    from .legal_claims import legal_notes
    root, today = Path(root), today or date.today()
    agencies, rows, loaded = _state(root, today)
    new = {"agency_id": agency_id, "on": on, "kind": kind, "as_stated": as_stated, "source_url": source_url,
           "checked_on": checked_on}
    where = f"{LABEL}.actions[{len(rows)}]"
    prospective = rows + [new]
    found = check_actions({"schema_version": 1, "actions": prospective}, agencies, today=today)[1]
    if found:
        raise ActionError(found[0])
    _install(root, _dump(prospective), loaded)
    return {"agency_id": agency_id, "index": sum(1 for row in prospective if row["agency_id"] == agency_id),
            "notes": legal_notes([(f"{where}.as_stated", as_stated)])}


def parse_index(text):
    """The 1-based index typed on the command line, or ActionError (ASCII digits only; no sign, space or zero)."""
    if not isinstance(text, str) or not INDEX_SHAPE.fullmatch(text):
        raise ActionError("--index: must be a whole number of 1 or more (the agency's stored order)")
    return int(text)


def remove_action(root, agency_id, index, *, today=None):
    """Drop the ``index``-th (1-based, STORED order) action of one agency. -> {"agency_id", "index"}."""
    root, today = Path(root), today or date.today()
    if type(index) is not int or index < 1:
        raise ActionError("--index: must be a whole number of 1 or more (the agency's stored order)")
    agencies, rows, loaded = _state(root, today)
    if loaded is None:
        raise ActionError(f"{LABEL}: does not exist; there is nothing to remove")
    if not isinstance(agency_id, str) or not AGENCY_ID.fullmatch(agency_id) or agency_id not in {
            a["agency_id"] for a in agencies}:
        raise ActionError("--agency: unknown agency")
    positions = [n for n, row in enumerate(rows) if row["agency_id"] == agency_id]
    if not positions:
        raise ActionError(f"--agency: no actions on record for {agency_id}")
    if index > len(positions):
        raise ActionError(f"--index: {agency_id} has {len(positions)} action(s) on record; choose 1 to {len(positions)}")
    del rows[positions[index - 1]]
    _install(root, _dump(rows), loaded)
    return {"agency_id": agency_id, "index": index}
