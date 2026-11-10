"""`recent`: private campaign-wide "what changed" report for a period (read-only).

Lists requests sent, agency steps and agency actions recorded, records added to the library, findings reviewed,
published, updated, corrected or withdrawn, correction requests received and decided, meetings held, their recorded
outcomes and comments submitted by hand, and (DF1109-05, in an ``Outreach`` area printed only when it has something)
official statements, press coverage and campaign events, each with its stored date and the command that shows more.
Only dates that are stored in a campaign file are used (never file times); an event without one is counted, not listed.
Shows agency names, finding titles, official names, outlets, event titles, meeting ids and 12-character record
prefixes only: never request text, addresses, notes, quotes, summaries, requesters, reviewer names or full hashes.
Each optional store is read on its own, without following links, opening special files or reading past its size
limit; one that cannot be read or validated is named ``<file> unreadable`` (exit 3) and hides nothing else.
The only write is an explicit new ``--md`` file. Nothing is sent or published.
A linked or non-directory input folder (``INPUT_DIRECTORIES``) is refused before anything is collected or written;
a missing folder stays an empty input.
"""
import json
import os
import re
import shlex
import stat
from datetime import date, timedelta
from pathlib import Path

from . import (agency_actions, content_events, content_statements, correction_requests, finding_updates, findings, kit,
               kit_track, labels, meeting_comments)
from .content import MAX_JSON_BYTES, _read_json, coverage_entries, load_agencies, load_meeting, load_sources

AREAS = ("Requests", "Records", "Findings", "Meetings")
OUTREACH = "Outreach"  # listed after AREAS only when it has an event or a problem, so older reports keep their bytes
_BAD = (ValueError, TypeError, KeyError, AttributeError, OSError, RecursionError)  # never echoed: only the file is named
MAX_DAYS, PREFIX_CHARS, PREFIXES_SHOWN, TEXT_CHARS = 366, 12, 8, 120
INPUT_DIRECTORIES = ("kit", "content", "private", "content/findings", "content/meetings", "private/drafts",
                     "private/withdrawn", "private/reviews")
_DAY = re.compile(r"\d{4}-\d\d-\d\d")
_MARKDOWN = re.compile(r"([\\`*_{}\[\]<>()#+!|~])")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")


class UsageError(ValueError):
    """A bad flag value; the message starts with the flag. ``cli`` exits with ``exit_code`` (2)."""
    exit_code = 2


def _today():
    return date.today()


def _day_of(value):
    """The calendar day of a stored ISO date or timestamp, else None."""
    if not isinstance(value, str) or not _DAY.fullmatch(value[:10]):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _flag_day(value, flag):
    if not isinstance(value, str) or not _DAY.fullmatch(value):
        raise UsageError(f"{flag} must be a real YYYY-MM-DD date, got {ascii(value)}")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise UsageError(f"{flag} must be a real YYYY-MM-DD date, got {ascii(value)}") from None


def window(since, until, today):
    """(first, last) days of the inclusive window; ``until`` defaults to ``today``."""
    first = _flag_day(since, "--since")
    last = today if until is None else _flag_day(until, "--until")
    if last > today:
        raise UsageError(f"--until {last} is in the future (today is {today})")
    if first > last:
        raise UsageError(f"--since {first} is after --until {last}" if until is not None
                         else f"--since {first} is in the future (today is {today})")
    if (last - first).days + 1 > MAX_DAYS:
        raise UsageError(f"--since {first} to {last} is {(last - first).days + 1} days; at most {MAX_DAYS}")
    return first, last


def _clean(text, limit=TEXT_CHARS):
    """One printable line of at most ``limit`` characters."""
    text = _CONTROL.sub(" ", text if isinstance(text, str) else "").strip()
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


class _Sink:
    def __init__(self, first, last):
        self.first, self.last, self.events, self.problems, self.undated = first, last, [], [], 0

    def add(self, area, day, what, command):
        if self.first <= day <= self.last:
            self.events.append({"on": day.isoformat(), "area": area, "what": what, "command": command})

    def problem(self, area, text):
        self.problems.append((area, text))

    def unreadable(self, area, label):
        """Name an unreadable store once (a new collector never repeats a line already given for the same file)."""
        if (area, f"{label} unreadable") not in self.problems:
            self.problem(area, f"{label} unreadable")


ABSENT = object()  # no file; a present file holding JSON ``null`` parses to None and is validated like any document


def _store(root, relative, limit=MAX_JSON_BYTES):
    """The parsed JSON of an optional campaign file (``null`` included), or ``ABSENT`` when there is no file.

    The leaf is inspected without following it: a link (even dangling), folder, FIFO or other special file, a file
    over ``limit``, bad UTF-8 or bad JSON raises without opening a link target or blocking. Its folders are already
    checked by ``_validate_input_directories``."""
    path = Path(root, relative)
    try:
        info = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return ABSENT
    if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
        raise ValueError(relative)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError(relative)
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError(relative)
    return json.loads(data.decode("utf-8"))


def _names(root, sink):
    path = root / "kit" / "agencies.json"
    if not path.is_file() or path.is_symlink():
        return {}
    try:
        return {entry["agency_id"]: _clean(entry["name"]) for entry in kit._load_existing(path)}
    except (ValueError, OSError, KeyError, TypeError):
        sink.problem("Requests", "kit/agencies.json unreadable")
        return {}


def _requests(root, sink, directory):
    try:
        sent = kit_track.read_sent(root)
    except (ValueError, OSError):
        sink.problem("Requests", "kit/sent.json unreadable")
        return
    names = _names(root, sink)
    for key, entry in sorted(sent.items()):
        agency_id, request = kit_track._split(key)
        label = names.get(agency_id) or agency_id
        label += "" if request == "main" else f" ({request} request)"
        command = f"kit-status --directory {directory}" + ("" if request == "main" else f" --request {request}")
        sink.add("Requests", date.fromisoformat(entry["sent_on"]), f"Request sent to {label}", command)
        for step in entry["steps"]:
            day = kit_track._day(step.get("on")) if isinstance(step, dict) else None
            if day is None or step.get("kind") not in kit_track.KINDS:
                sink.undated += 1
                continue
            kind = step["kind"].replace("_", " ") + (" (partial)" if step.get("partial") is True else "")
            sink.add("Requests", day, f"Agency step recorded for {label}: {kind}", command)


def _records(root, sink, directory):
    path = root / "content" / "sources.json"
    if not os.path.lexists(path):
        return
    try:
        entries = load_sources(_read_json(path))
    except (ValueError, OSError):
        sink.problem("Records", "content/sources.json unreadable")
        return
    by_day = {}
    for entry in entries:
        day = _day_of(entry["received_at"])
        if day is None:
            sink.undated += 1
        else:
            by_day.setdefault(day, set()).add(entry["sha256"])
    for day, hashes in by_day.items():
        prefixes = sorted({h[:PREFIX_CHARS] for h in hashes})
        shown = ", ".join(prefixes[:PREFIXES_SHOWN]) + (f" (+{len(prefixes) - PREFIXES_SHOWN} more)"
                                                       if len(prefixes) > PREFIXES_SHOWN else "")
        sink.add("Records", day, f"{len(prefixes)} record{'' if len(prefixes) == 1 else 's'} added to the library: "
                 + shown, f"sources list --directory {directory}")


def _documents(root, sub, what, limit=findings.MAX_DRAFT_BYTES):
    """(path, parsed JSON) of each readable ``root/sub/*.json``; an unreadable file is left to status_rows."""
    folder = root / sub
    if folder.is_symlink() or not folder.is_dir():
        return []
    found = []
    for path in sorted(folder.glob("*.json")):
        try:
            found.append((path, findings._read_json(path, limit, what)))
        except (ValueError, OSError):
            continue
    return found


def _findings(root, sink, directory, today):
    command = f"finding status --directory {directory}"
    try:
        rows = findings.status_rows(root, draft_paths=True)
    except (ValueError, OSError):
        sink.problem("Findings", "private/ unreadable")
        rows = []
    for row in rows:
        if row["state"] == "unreadable":
            where = Path(row.get("path") or "private")
            sink.problem("Findings", (where.relative_to(root).as_posix() if where.is_relative_to(root) else where.name)
                         + " unreadable")
        elif row.get("draft_path"):  # a draft stores no creation date, so "drafted" can never be dated
            sink.undated += 1
    titles, seen = {}, set()
    # A withdrawn document retains the publication history and takes precedence
    # over any public copy left behind by an interrupted withdrawal.
    for sub, what in (("private/drafts", "draft"), ("private/withdrawn", "withdrawn finding"),
                      ("content/findings", "published finding")):
        for path, document in _documents(root, sub, what):
            if not isinstance(document, dict) or not isinstance(document.get("id"), str):
                continue
            if document["id"] in seen:
                continue
            title = _clean(document.get("title")) or document["id"]
            titles[document["id"]] = title
            if sub != "private/drafts":
                seen.add(document["id"])
                day = _day_of(document.get("published_at"))
                if day is None:
                    sink.undated += 1
                else:
                    sink.add("Findings", day, f"Finding published: {title}", command)
                for correction in document.get("corrections") if isinstance(document.get("corrections"), list) else []:
                    day = _day_of(correction.get("date")) if isinstance(correction, dict) else None
                    if day is None:
                        sink.undated += 1
                    else:
                        sink.add("Findings", day, f"Finding corrected: {title}", command)
                _updates(sink, document, f"{sub}/{path.name}", title, command, today)
            if sub == "private/withdrawn":
                withdrawn = document.get("withdrawn")
                day = _day_of(withdrawn.get("at")) if isinstance(withdrawn, dict) else None
                if day is None:
                    sink.undated += 1
                else:
                    sink.add("Findings", day, f"Finding withdrawn: {title}", command)
    folder = root / "private" / "reviews"
    for path in sorted(folder.glob("*.json")) if folder.is_dir() and not folder.is_symlink() else []:
        try:
            receipts = findings._read_json(path, findings.MAX_RECEIPTS_BYTES, "receipt file")
            if not isinstance(receipts, list):
                raise ValueError(path.name)
        except (ValueError, OSError):
            sink.problem("Findings", f"private/reviews/{path.name} unreadable")
            continue
        title = titles.get(path.stem) or _clean(path.stem)
        for receipt in receipts:
            day = _day_of(receipt.get("reviewed_at")) if isinstance(receipt, dict) else None
            if day is None:
                sink.undated += 1
                continue
            role, decision = receipt.get("role"), receipt.get("decision")
            detail = f" ({role}, {decision})" if role in findings.ROLES and decision in findings.DECISIONS else ""
            sink.add("Findings", day, f"Finding reviewed: {title}{detail}", command)


def _meetings(root, sink, today):
    folder = root / "content" / "meetings"
    for path in sorted(folder.glob("*.json")) if folder.is_dir() and not folder.is_symlink() else []:
        label = "content/meetings/" + path.name
        try:
            meeting = load_meeting(_read_json(path), "meetings/" + path.name, today)
        except (ValueError, OSError):
            sink.problem("Meetings", f"{label} unreadable")
            continue
        body, ident, command = _clean(meeting["body"]), meeting["id"] or path.stem, "open " + shlex.quote(label)
        held = _day_of(meeting["starts_at"])
        if held is None:
            sink.undated += 1
        else:
            sink.add("Meetings", held, f"Meeting on {held}: {body} ({ident})"
                     + (" (cancelled)" if meeting["status"] == "cancelled" else ""), command)
        outcome = meeting["outcome"]
        if outcome:
            day = _day_of(outcome["verified_at"])
            if day is None:
                sink.undated += 1
            else:
                sink.add("Meetings", day, f"Outcome recorded for {body} ({ident}): {outcome['result']}", command)


# --- DF1109-05: the dated stores added after `recent` shipped. Each is read on its own and validated by its canonical
# checker; only the stored day and the fields named in docs/RECENT.md are projected.

def _updates(sink, document, label, title, command, today):
    """``Finding updated: <title>`` per stored update day (repeats kept); never the note."""
    if "updates" not in document:
        return
    try:
        updates = finding_updates.load_updates(document["updates"], document.get("published_at"), today=today,
                                               label="updates")
    except _BAD:
        sink.unreadable("Findings", label)
        return
    for update in updates:
        sink.add("Findings", date.fromisoformat(update["date"]), f"Finding updated: {title}", command)


def _public_names(root, sink, area, today, cache):
    """{agency_id: name} from content/agencies.json, read once and only when needed; {} (ids shown) when absent or bad."""
    if "names" not in cache:
        cache["names"] = {}
        try:
            document = _store(root, "content/agencies.json")
            if document is not ABSENT:
                cache["names"] = {agency["agency_id"]: _clean(agency["name"])
                                  for agency in load_agencies(document, "content/agencies.json", today)}
        except _BAD:
            sink.unreadable(area, "content/agencies.json")
    return cache["names"]


def _actions(root, sink, today, cache):
    """``Agency action: <kind label> — <agency>`` on ``on``; never as_stated, source_url or checked_on."""
    try:
        rows = agency_actions.read_actions(root, None, today=today) or []
    except _BAD:
        sink.unreadable("Requests", agency_actions.LABEL)
        return
    for row in rows:
        names = _public_names(root, sink, "Requests", today, cache)
        kind = labels.DEFAULTS[f"agency.action_kind_{row['kind']}"]
        sink.add("Requests", date.fromisoformat(row["on"]), f"Agency action: {kind} — "
                 + (names.get(row["agency_id"]) or row["agency_id"]), "open " + shlex.quote(agency_actions.LABEL))


def _statements(root, sink, today, cache):
    """``Official statement: <official> — <body>`` on ``said_on``; never the quote, context note, time or source."""
    label = "content/" + content_statements.FILE
    try:
        rows = content_statements.read_statements(root, None, today) or []
    except _BAD:
        sink.unreadable(OUTREACH, label)
        return
    for row in rows:
        names = _public_names(root, sink, OUTREACH, today, cache)
        sink.add(OUTREACH, date.fromisoformat(row["said_on"]), f"Official statement: {_clean(row['official'])} — "
                 + (names.get(row["agency_id"]) or row["agency_id"]), "open " + shlex.quote(label))


def _coverage(root, sink, today):
    """``Press coverage: <outlet>`` on ``published_on``; never the headline, URL or linked findings."""
    label = "content/coverage.json"
    try:
        document = _store(root, label)
        entries = [] if document is ABSENT else coverage_entries(document, today=today)
    except _BAD:
        sink.unreadable(OUTREACH, label)
        return
    for entry in entries:
        sink.add(OUTREACH, date.fromisoformat(entry["published_on"]), f"Press coverage: {_clean(entry['outlet'])}",
                 "open " + shlex.quote(label))


def _campaign_events(root, sink):
    """``Campaign event: <title>`` on the written day of ``starts_at`` (no stored announcement date exists)."""
    label = "content/events.json"
    try:
        document = _store(root, label)
        events = [] if document is ABSENT else content_events.load_events(document)
    except _BAD:
        sink.unreadable(OUTREACH, label)
        return
    for event in events:
        sink.add(OUTREACH, _day_of(event["starts_at"]), f"Campaign event: {_clean(event['title'])}",
                 "open " + shlex.quote(label))


def _comments(root, sink):
    """``Comment submitted by hand: <meeting id>`` on ``on``; never the channel, letter hash or bytes, or recorded_at."""
    label = meeting_comments.LABEL
    try:
        document = _store(root, label)
        if document is ABSENT:
            return
        if (not isinstance(document, dict) or set(document) != {"schema_version", "comments"}
                or document["schema_version"] != 1 or not isinstance(document["comments"], dict)):
            raise ValueError(label)
        entries = [(key, meeting_comments._entry(value, key)) for key, value in document["comments"].items()]
        if not all(meeting_comments.MEETING_ID.fullmatch(key) for key, _ in entries):
            raise ValueError(label)
    except _BAD:
        sink.unreadable("Meetings", label)
        return
    for key, entry in entries:
        sink.add("Meetings", date.fromisoformat(entry["on"]), f"Comment submitted by hand: {key}",
                 "open " + shlex.quote(label))


def _correction_requests(root, sink, directory):
    """Received (initial state ``open``) and decided days only; never the id, finding, summary, note or requester."""
    label = "private/" + correction_requests.NAME
    try:
        document = _store(root, label)
        requests = [] if document is ABSENT else correction_requests._validate_document(document)["requests"]
    except _BAD:
        sink.unreadable("Findings", label)
        return
    command = f"correction-request list --directory {directory}"
    for request in requests:
        sink.add("Findings", date.fromisoformat(request["received_on"]), "Correction request received (open)", command)
        if request["state"] in correction_requests.STATES[1:]:
            sink.add("Findings", date.fromisoformat(request["decided_on"]),
                     f"Correction request decided ({request['state']})", command)


def _validate_input_directories(root):
    """Refuse a linked or non-directory input folder before any event is collected (ValueError, exit 1).

    Only the ``INPUT_DIRECTORIES`` allowlist is inspected, parents before children, without following links; a missing
    folder is a valid empty input. Nothing below a refused folder is listed or opened, and the message never carries
    a link target, a path outside the campaign or an OS error text.
    """
    for relative in INPUT_DIRECTORIES:
        try:
            info = os.lstat(Path(root) / relative)
        except FileNotFoundError:
            continue
        except NotADirectoryError:  # a parent that is not a directory hides this entry
            continue
        except OSError:
            raise ValueError(f"recent: {relative}/ cannot be inspected") from None
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"recent: {relative}/ must be a real directory, not a symlink or file")


def collect(root, since, until, today=None):
    """(events, undated, problems) for the inclusive window; reads only. Raises ``UsageError`` for a bad window,
    ``ValueError`` for a linked or non-directory input folder."""
    root, today = Path(root), _today() if today is None else today
    first, last = window(since, until, today)
    _validate_input_directories(root)
    sink = _Sink(first, last)
    directory, cache = shlex.quote(str(root)), {}
    for read in (lambda: _requests(root, sink, directory), lambda: _actions(root, sink, today, cache),
                 lambda: _records(root, sink, directory), lambda: _findings(root, sink, directory, today),
                 lambda: _correction_requests(root, sink, directory), lambda: _meetings(root, sink, today),
                 lambda: _comments(root, sink), lambda: _statements(root, sink, today, cache),
                 lambda: _coverage(root, sink, today), lambda: _campaign_events(root, sink)):
        read()
    sink.events.sort(key=lambda event: event["what"])
    sink.events.sort(key=lambda event: event["on"], reverse=True)
    return sink.events, sink.undated, sink.problems


def report(root, since, until=None, today=None):
    """The events ``{"on": YYYY-MM-DD, "area", "what", "command"}``, newest first inside each area."""
    return collect(root, since, until, today)[0]


def _undated_line(count):
    return f"{count} undated item{'' if count == 1 else 's'} skipped"


def render(events, problems, undated, first, last, markdown=False):
    escape = (lambda text: _MARKDOWN.sub(r"\\\1", text)) if markdown else (lambda text: text)
    head = f"Recent changes, {first} to {last}"
    note = "Private report for the organizing team. Nothing was sent or published."
    lines = [f"# {head}", "", note] if markdown else [head + " (private; nothing was sent or published)"]
    outreach = any(event["area"] == OUTREACH for event in events) or any(where == OUTREACH for where, _ in problems)
    for area in AREAS + ((OUTREACH,) if outreach else ()):
        lines += ["", f"## {area}" if markdown else area, ""] if markdown else ["", area]
        mine = [event for event in events if event["area"] == area]
        notes = [text for where, text in problems if where == area]
        for event in mine:
            if markdown:
                fence = "``" if "`" in event["command"] else "`"
                lines.append(f"- {event['on']} — {escape(event['what'])} → {fence}{event['command']}{fence}")
            else:
                lines.append(f"{event['on']}  {event['what']}  → {event['command']}")
        lines += [("- " if markdown else "") + escape(text) for text in notes]
        if not mine and not notes:
            lines.append(("- " if markdown else "") + "nothing recorded")
    if undated:
        lines += ["", _undated_line(undated)]
    return "\n".join(lines) + "\n"


def _md_target(root, md):
    """The new file's resolved path; refused (exit 2) inside public/ or content/, when it exists or has no folder."""
    path = Path(md)
    if not path.name or path.name in (".", ".."):
        raise UsageError(f"--md {ascii(str(md))} must name a file")
    if not path.parent.is_dir():
        raise UsageError(f"--md folder {ascii(str(path.parent))} does not exist")
    target = path.parent.resolve() / path.name
    if os.path.lexists(target):
        raise UsageError(f"--md {ascii(str(md))} already exists; it is never overwritten")
    for zone in ("public", "content"):
        for base in (Path(root).absolute(), Path(root).resolve()):
            inside = base / zone
            for candidate in (inside, inside.resolve()):
                if target == candidate or candidate in target.parents:
                    raise UsageError(f"--md {ascii(str(md))} is inside {zone}/; write the report outside the "
                                     "published folders")
    return target


def _write_new(target, text):
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except FileExistsError:
        raise UsageError(f"--md {ascii(str(target))} already exists; it is never overwritten") from None
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        if hasattr(os, "fchmod"):
            os.fchmod(out.fileno(), 0o600)
        out.write(text)


def run_cli(root, since, until=None, md=None, as_json=False, today=None, out=None, err=None):
    """Print the report; return the exit code (0, or 3 when a file was unreadable). A bad flag raises UsageError."""
    import sys
    out, err, root = out or sys.stdout, err or sys.stderr, Path(root)
    today = _today() if today is None else today
    first, last = window(since, until, today)
    target = _md_target(root, md) if md is not None else None  # refused before anything is read
    events, undated, problems = collect(root, since, until, today)
    if target is not None:
        _write_new(target, render(events, problems, undated, first, last, markdown=True))
    if as_json:
        print(json.dumps(events, indent=2, ensure_ascii=False), file=out)
        for _, text in problems:
            print(text, file=err)
        if undated:
            print(_undated_line(undated), file=err)
    else:
        out.write(render(events, problems, undated, first, last))
    if target is not None:
        print(f"Wrote {target}", file=err)
    return 3 if problems else 0
