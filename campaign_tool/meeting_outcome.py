"""Record an organizer-verified outcome on a past staged meeting (DF2237-11).

Offline only: the organizer reads the result on the agency's own record (minutes, video, vote page) and
types it in; nothing is fetched or published. The whole meeting document is validated by
``content.load_meeting`` before the atomic write.
"""
import csv
import json
import os
import re
import stat
import sys
import unicodedata
from datetime import date

from .content import (COMMENT_KEYS, DIRECTION_KEYS, VOTE_VALUES, _read_json, continuation_issues, default_agencies, load_agencies,
                      load_meeting, load_officials)
from .kit import _write_json
from .legal_claims import legal_notes


class MeetingOutcomeError(ValueError):
    pass


def read_votes(path):
    """``[(file row number, cells)]`` of a regular, non-symlink UTF-8 CSV of at most 64 KiB (DF0137-07)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise MeetingOutcomeError(f"votes: cannot read {path} as a regular file ({exc.strerror})") from None
    try:
        data = os.read(fd, 65537) if stat.S_ISREG(os.fstat(fd).st_mode) else None
    finally:
        os.close(fd)
    if data is None or len(data) > 65536:
        raise MeetingOutcomeError("votes: must be a regular file of at most 65536 bytes")
    try:
        return list(enumerate(csv.reader(data.decode("utf-8").splitlines()), 1))
    except (UnicodeDecodeError, csv.Error) as exc:
        raise MeetingOutcomeError(f"votes: not a valid UTF-8 CSV ({exc})") from None


def check_votes(rows, root, agency_id, today):
    """Each row names one of the body's officials exactly (NFC, whitespace runs collapsed, case-sensitive) and gives
    one of VOTE_VALUES; every problem is listed by file row (the header is row 1). Nothing is inferred."""
    path, agencies_path = root / "content" / "officials.json", root / "content" / "agencies.json"
    officials = None
    if agency_id is not None and (path.is_file() or path.is_symlink()):
        agencies = (load_agencies(_read_json(agencies_path), "content/agencies.json", today) if agencies_path.is_file()
                    else default_agencies(root))
        officials = load_officials(_read_json(path), agencies, today).get(agency_id)
    if not officials:
        raise MeetingOutcomeError("--votes needs the meeting's agency_id to have a content/officials.json body; "
                                  "nothing was recorded")
    same = lambda name: " ".join(unicodedata.normalize("NFC", name).split())  # noqa: E731
    names, votes = {same(person["name"]): person["name"] for person in officials}, []
    problems = (([] if rows[:1] == [(1, ["name", "vote"])] else ["votes: header must be name,vote"])
                + ([] if 1 <= len(rows[1:]) <= 30 else ["votes: must list 1 to 30 rows"]))
    for number, row in rows[1:]:
        if len(row) != 2:
            problems.append(f"votes row {number}: must have exactly two columns, name and vote")
            continue
        name, vote = same(row[0]), row[1].strip().lower()
        if name not in names:
            problems.append(f'votes row {number}: unknown name "{name}" (not in officials.json for {agency_id})')
        elif any(names[name] == other["name"] for other in votes):
            problems.append(f"votes row {number}: name listed twice")
        if vote not in VOTE_VALUES:
            problems.append(f"votes row {number}: vote must be one of {', '.join(VOTE_VALUES)}")
        votes.append({"name": names.get(name), "vote": vote})
    if problems:
        raise MeetingOutcomeError("; ".join(problems))
    return votes


def _check_parents(root):
    """RF0844-1: ``content`` then ``content/meetings`` (campaign-relative, checked lexically and in that order) must not
    be symlinks, whatever they point at, and an existing one must be a directory. A missing one is left as it is. Never
    resolves or reads a link target; the caller's campaign root remains the trust boundary."""
    for relative in ("content", "content/meetings"):
        path = root / relative
        if os.path.islink(path):
            raise MeetingOutcomeError(f"{relative} must not be a symlink; no staged meeting is read or written through it")
        if os.path.lexists(path) and not os.path.isdir(path):
            raise MeetingOutcomeError(f"{relative} must be a directory")


def _check_writable(root, path):
    """Checked again right before every write: the parents (RF0844-1) and the final file must not be symlinks."""
    _check_parents(root)
    if os.path.islink(path):
        raise MeetingOutcomeError(f"content/meetings/{path.name} must not be a symlink")


def _find(root, meeting_id):
    """The staged ``content/meetings/<file>`` whose id (or filename stem) is ``meeting_id``; never follows a symlink."""
    _check_parents(root)
    folder = root / "content" / "meetings"
    paths = sorted(folder.glob("*.json")) if folder.is_dir() else []
    known = []
    for path in paths:
        if os.path.islink(path):
            if path.stem == meeting_id:
                raise MeetingOutcomeError(f"content/meetings/{path.name} must not be a symlink")
            continue
        document = _read_json(path)
        found = document.get("id", path.stem) if isinstance(document, dict) else path.stem
        if found == meeting_id:
            return path, document
        known.append(str(found))
    raise MeetingOutcomeError(f"no staged meeting {meeting_id} in content/meetings/; "
                              f"known: {', '.join(known[:5]) or 'none'}")


def record_outcome(root, meeting_id, result, on, source_url, verified_by, vote=None, replace=False, today=None,
                   votes_file=None, recording_url=None, recording_start=None, continued_to=None, continued_to_date=None, direction=None, return_by=None,
                   direction_source=None, spoke=None, written=None, comment_source_url=None):
    today = today or date.today()
    _check_direction_pair(direction, return_by, direction_source)  # DF0022-08
    _check_comment_pair(spoke, written, comment_source_url)  # DF0022-09
    if recording_start is not None and recording_url is None:  # DF1422-10: a start time is part of a recording
        raise MeetingOutcomeError("recording_start needs recording_url")
    if continued_to is not None and continued_to_date is not None:  # DF0022-07: one stated destination, not two
        raise MeetingOutcomeError("--continued-to and --continued-to-date are mutually exclusive")
    for name, value in (("continued_to", continued_to), ("continued_to_date", continued_to_date)):
        if value is not None and result != "continued":
            raise MeetingOutcomeError(f"{name}: requires result continued")
    rows = read_votes(votes_file) if votes_file is not None else None  # before any read of the meeting
    path, document = _find(root, meeting_id)
    label = f"meetings/{path.name}"
    meeting = load_meeting(document, label, today=today)
    if meeting["status"] == "cancelled":
        raise MeetingOutcomeError(f"meeting {meeting_id} is cancelled; no outcome is recorded")
    if meeting["starts_at"][:10] > today.isoformat():
        raise MeetingOutcomeError(f"meeting {meeting_id} has not happened yet ({meeting['starts_at'][:10]})")
    if meeting["outcome"] is not None and not replace:
        raise MeetingOutcomeError(f"meeting {meeting_id} already has an outcome ({meeting['outcome']['result']}); "
                                  "pass --replace to overwrite it")
    outcome = {"result": result, "on": on, "source_url": source_url, "verified_by": verified_by,
               "verified_at": today.isoformat(), **({"vote": vote} if vote is not None else {}),
               **({"votes": check_votes(rows, root, meeting["agency_id"], today)} if rows is not None else {}),
               **({"recording_url": recording_url} if recording_url is not None else {}),
               **({"recording_start": recording_start} if recording_start is not None else {}),
               **({"continued_to": continued_to} if continued_to is not None else {}),
               **({"continued_to_date": continued_to_date} if continued_to_date is not None else {}),
               **({"direction": direction, "return_by": return_by, "direction_source": source_url if direction_source is None else direction_source}
                  if direction is not None else {}),
               # DF0022-09: the source is this outcome's own minutes (none is carried by a fresh outcome) or source_url
               **_comment_group(spoke, written, comment_source_url, source_url)}
    updated = {**document, "outcome": outcome}
    prospective = load_meeting(updated, label, today=today)
    if continued_to is not None:  # DF0022-07: the staged meetings with only this outcome replaced
        _check_continuation(root, path, prospective, today)
    _check_writable(root, path)  # checked again right before the write
    _write_json(path, updated)
    return outcome


def _check_direction_pair(direction, return_by, direction_source):
    """DF0022-08: a direction and its return-by date are given together; a source alone selects nothing."""
    if direction is None and return_by is None:
        if direction_source is not None:
            raise MeetingOutcomeError("direction_source: needs direction and return_by")
    elif direction is None:
        raise MeetingOutcomeError("direction: needed together with return_by")
    elif return_by is None:
        raise MeetingOutcomeError("return_by: needed together with direction")


def _check_comment_pair(spoke, written, comment_source_url):
    """DF0022-09: a comment source alone selects nothing."""
    if spoke is None and written is None and comment_source_url is not None:
        raise MeetingOutcomeError("--comment-source needs --spoke or --written")


def _comment_group(spoke, written, comment_source_url, default_source):
    """The count keys to store, or ``{}``; the source is the one given, else ``default_source``. Values are validated by
    ``content.load_comment_counts`` when the prospective document is loaded."""
    if spoke is None and written is None:
        return {}
    return {**({"spoke": spoke} if spoke is not None else {}), **({"written": written} if written is not None else {}),
            "comment_source_url": default_source if comment_source_url is None else comment_source_url}


def comment_count(text, flag):
    """DF0022-09: a CLI count: ASCII decimal digits only, 0 to 999 (leading zeros allowed); anything else is refused by flag."""
    if isinstance(text, str) and re.fullmatch(r"[0-9]+", text) and len(text.lstrip("0")) <= 3:
        return int(text)
    raise MeetingOutcomeError(f"{flag}: must be a whole number from 0 to 999")


def _check_continuation(root, path, meeting, today):
    """DF0022-07: ``meeting``'s explicit ``continued_to`` must name one scheduled, later meeting of the same body among
    the staged ones. Only this meeting's outcome is replaced in the set, so a broken reference elsewhere (or the one
    being replaced) never blocks it; reads only, no symlink is followed, nothing is written."""
    _check_parents(root)  # RF0844-1: before the staged meetings are enumerated
    source = {**meeting, "id": meeting["id"] or path.stem}
    staged, unreadable = [source], {}
    for other in sorted((root / "content" / "meetings").glob("*.json")):
        if other.name == path.name:
            continue
        if os.path.islink(other):
            unreadable[other.stem] = f"content/meetings/{other.name} must not be a symlink"
            continue
        try:
            loaded = load_meeting(_read_json(other), f"meetings/{other.name}", today=today)
        except (ValueError, OSError) as exc:
            unreadable[other.stem] = str(exc)
            continue
        staged.append({**loaded, "id": loaded["id"] or other.stem})
    target = source["outcome"]["continued_to"]
    where = f"meetings/{source['id']}.outcome.continued_to"
    if target in unreadable:
        raise MeetingOutcomeError(f"{where}: meeting {target} cannot be read ({unreadable[target]})")
    if sum(1 for m in staged if m["id"] == source["id"]) > 1:
        raise MeetingOutcomeError(f"{where}: duplicate meeting ID {source['id']}")
    problems = [problem for found, problem in continuation_issues(staged) if found is source]
    if problems:
        raise MeetingOutcomeError(f"{where}: {problems[0]}")


def record_minutes(root, meeting_id, minutes_url, minutes_approved_on, *, replace=False, today=None):
    """Attach the body's approved official minutes to an already recorded outcome (DF2207-05).

    Only ``minutes_url`` and ``minutes_approved_on`` are merged into the stored outcome; every other outcome and
    meeting field stays exactly as stored. Nothing is fetched; the whole document is validated before the write."""
    today = today or date.today()
    path, document = _find(root, meeting_id)
    label = f"meetings/{path.name}"
    meeting = load_meeting(document, label, today=today)
    if meeting["status"] == "cancelled":
        raise MeetingOutcomeError(f"minutes_url: meeting {meeting_id} is cancelled; it has no outcome")
    if meeting["starts_at"][:10] > today.isoformat():
        raise MeetingOutcomeError(f"minutes_url: meeting {meeting_id} has not happened yet ({meeting['starts_at'][:10]})")
    if meeting["outcome"] is None:
        raise MeetingOutcomeError(f"minutes_url: needs a recorded outcome (meeting {meeting_id} has none)")
    if "minutes_url" in meeting["outcome"] and not replace:
        raise MeetingOutcomeError(f"minutes_url: meeting {meeting_id} already has minutes approved on "
                                  f"{meeting['outcome']['minutes_approved_on']}; pass --replace to overwrite them")
    stored = {key: value for key, value in document["outcome"].items() if key not in MINUTES_KEYS}
    updated = {**document, "outcome": {**stored, "minutes_url": minutes_url, "minutes_approved_on": minutes_approved_on}}
    load_meeting(updated, label, today=today)
    _check_writable(root, path)  # checked again right before the write
    _write_json(path, updated)
    return updated["outcome"]


def record_direction(root, meeting_id, direction, return_by, *, direction_source=None, replace=False, today=None):
    """Attach the staff direction an official source states to an already recorded outcome (DF0022-08).

    Only ``direction``, ``return_by`` and ``direction_source`` are merged into the stored outcome (the source defaults
    to the outcome's own ``source_url``); every other outcome and meeting key stays exactly as stored. Nothing is
    fetched; the whole prospective meeting and its continuation references are validated before the write."""
    today = today or date.today()
    _check_direction_pair(direction, return_by, direction_source)
    if direction is None:
        raise MeetingOutcomeError("direction: required")
    path, document = _find(root, meeting_id)
    label = f"meetings/{path.name}"
    meeting = load_meeting(document, label, today=today)
    if meeting["status"] == "cancelled":
        raise MeetingOutcomeError(f"direction: meeting {meeting_id} is cancelled; it has no outcome")
    if meeting["starts_at"][:10] > today.isoformat():
        raise MeetingOutcomeError(f"direction: meeting {meeting_id} has not happened yet ({meeting['starts_at'][:10]})")
    if meeting["outcome"] is None:
        raise MeetingOutcomeError(f"direction: needs a recorded outcome (meeting {meeting_id} has none)")
    if "direction" in meeting["outcome"] and not replace:
        raise MeetingOutcomeError(f"direction already recorded; pass --replace to overwrite it (meeting {meeting_id})")
    stored = {key: value for key, value in document["outcome"].items() if key not in DIRECTION_KEYS}
    updated = {**document, "outcome": {**stored, "direction": direction, "return_by": return_by,
                                       "direction_source": meeting["outcome"]["source_url"] if direction_source is None else direction_source}}
    prospective = load_meeting(updated, label, today=today)
    if "continued_to" in prospective["outcome"]:  # the staged meetings with only this outcome replaced
        _check_continuation(root, path, prospective, today)
    _check_writable(root, path)  # checked again right before the write
    _write_json(path, updated)
    return updated["outcome"]


MINUTES_KEYS = ("minutes_url", "minutes_approved_on")


def record_comment_counts(root, meeting_id, *, spoke=None, written=None, comment_source_url=None, replace=False, today=None):
    """Attach the public-comment totals an official record states to an already recorded outcome (DF0022-09).

    Only ``spoke``, ``written`` and ``comment_source_url`` are merged (the source defaults to the outcome's current
    ``minutes_url``, else its ``source_url``); every other outcome and meeting key stays exactly as stored. With
    ``replace`` the whole count group is replaced, so a count not supplied is removed. Nothing is fetched; the whole
    prospective meeting and its continuation references are validated before the write."""
    today = today or date.today()
    _check_comment_pair(spoke, written, comment_source_url)
    if spoke is None and written is None:
        raise MeetingOutcomeError("public comment counts: needs --spoke or --written")
    path, document = _find(root, meeting_id)
    label = f"meetings/{path.name}"
    meeting = load_meeting(document, label, today=today)
    if meeting["status"] == "cancelled":
        raise MeetingOutcomeError(f"public comment counts: meeting {meeting_id} is cancelled; it has no outcome")
    if meeting["starts_at"][:10] > today.isoformat():
        raise MeetingOutcomeError(f"public comment counts: meeting {meeting_id} has not happened yet ({meeting['starts_at'][:10]})")
    if meeting["outcome"] is None:
        raise MeetingOutcomeError(f"public comment counts: needs a recorded outcome (meeting {meeting_id} has none)")
    current = meeting["outcome"]
    if ("spoke" in current or "written" in current) and not replace:
        raise MeetingOutcomeError(f"public comment counts already recorded; pass --replace to overwrite them (meeting {meeting_id})")
    stored = {key: value for key, value in document["outcome"].items() if key not in COMMENT_KEYS}
    group = _comment_group(spoke, written, comment_source_url, current.get("minutes_url", current["source_url"]))
    updated = {**document, "outcome": {**stored, **group}}
    prospective = load_meeting(updated, label, today=today)
    if "continued_to" in prospective["outcome"]:  # the staged meetings with only this outcome replaced
        _check_continuation(root, path, prospective, today)
    _check_writable(root, path)  # checked again right before the write
    _write_json(path, updated)
    return updated["outcome"]


CREATION_FLAGS = (("result", "--result"), ("on", "--on"), ("source", "--source"), ("verified_by", "--verified-by"))
MINUTES_ONLY_REFUSES = (("vote", "--vote"), ("votes", "--votes"), ("recording_url", "--recording-url"),
                        ("recording_start", "--recording-start"), ("continued_to", "--continued-to"),
                        ("continued_to_date", "--continued-to-date"))


DIRECTION_ONLY_REFUSES = (("vote", "--vote"), ("votes", "--votes"), ("recording_url", "--recording-url"),
                          ("recording_start", "--recording-start"), ("continued_to", "--continued-to"),
                          ("continued_to_date", "--continued-to-date"))


def _direction_notes(meeting_id, outcome):
    """DF0022-08: the alert-only legal-claim guard over the raw direction text; notes go to stderr, never change the result."""
    if outcome.get("direction") is not None:
        for note in legal_notes([(f"meetings/{meeting_id}.outcome.direction", outcome["direction"])]):
            print(note, file=sys.stderr)


def run_outcome_command(root, args):
    """The ``meetings-outcome`` command: creation, minutes-only or direction-only mode; returns the lines to print."""
    lines = _run_outcome_mode(root, args)
    _direction_notes(args.id, json.loads(lines[0]))
    return lines


COMMENT_FLAGS = (("spoke", "--spoke"), ("written", "--written"), ("comment_source", "--comment-source"))
COUNTS_ONLY_REFUSES = MINUTES_ONLY_REFUSES + (("direction", "--direction"), ("return_by", "--return-by"),
                                               ("direction_source", "--direction-source"))


def _run_outcome_mode(root, args):
    minutes_url, minutes_on = args.minutes_url, args.minutes_approved_on
    direction, return_by, source = args.direction, args.return_by, args.direction_source
    comments = {name: getattr(args, name, None) for name, _ in COMMENT_FLAGS}  # DF0022-09
    wanted = [flag for name, flag in COMMENT_FLAGS if comments[name] is not None]
    spoke, written = (None if comments[name] is None else comment_count(comments[name], flag)
                      for name, flag in COMMENT_FLAGS[:2])
    if wanted and (minutes_url is not None or minutes_on is not None):
        raise MeetingOutcomeError(f"minutes mode cannot be combined with {', '.join(wanted)}")
    if wanted:
        _check_comment_pair(spoke, written, comments["comment_source"])
    if wanted and all(getattr(args, name) is None for name, _ in CREATION_FLAGS):  # counts-only mode
        mixed = [flag for name, flag in COUNTS_ONLY_REFUSES if getattr(args, name) is not None]
        if mixed:
            raise MeetingOutcomeError(f"counts-only mode cannot be combined with {', '.join(mixed)}")
        outcome = record_comment_counts(root, args.id, spoke=spoke, written=written,
                                        comment_source_url=comments["comment_source"], replace=args.replace)
        return [json.dumps(outcome, indent=2), "Recorded only. Nothing was published."]
    wants_direction = any(value is not None for value in (direction, return_by, source))
    if wants_direction and (minutes_url is not None or minutes_on is not None):
        raise MeetingOutcomeError("minutes mode cannot be combined with direction flags")
    if (wants_direction and all(getattr(args, name) is None for name, _ in CREATION_FLAGS)
            and minutes_url is None and minutes_on is None):  # direction-only mode
        _check_direction_pair(direction, return_by, source)
        mixed = [flag for name, flag in DIRECTION_ONLY_REFUSES if getattr(args, name) is not None]
        if mixed:
            raise MeetingOutcomeError(f"direction-only mode cannot be combined with {', '.join(mixed)}")
        outcome = record_direction(root, args.id, direction, return_by, direction_source=source, replace=args.replace)
        return [json.dumps(outcome, indent=2), "Recorded only. Nothing was published."]
    if minutes_url is None and minutes_on is None:
        missing = [flag for name, flag in CREATION_FLAGS if getattr(args, name) is None]
        if missing:
            raise MeetingOutcomeError(f"{', '.join(missing)}: required to record an outcome "
                                      "(or give --minutes-url and --minutes-approved-on to confirm an existing one)")
        outcome = record_outcome(root, args.id, args.result, args.on, args.source, args.verified_by, args.vote,
                                 args.replace, votes_file=args.votes, recording_url=args.recording_url,
                                 recording_start=args.recording_start, continued_to=args.continued_to,
                                 continued_to_date=args.continued_to_date, direction=direction, return_by=return_by,
                                 direction_source=source, spoke=spoke, written=written,
                                 comment_source_url=comments["comment_source"])
        return [json.dumps(outcome, indent=2)] + (["votes: none"] if args.replace and args.votes is None else []) \
            + ["Recorded only. Nothing was published."]
    for name, flag in (("minutes_url", "--minutes-url"), ("minutes_approved_on", "--minutes-approved-on")):
        if getattr(args, name) is None:
            raise MeetingOutcomeError(f"{flag}: needed together with "
                                      f"{'--minutes-approved-on' if name == 'minutes_url' else '--minutes-url'}")
    mixed = [flag for name, flag in CREATION_FLAGS + MINUTES_ONLY_REFUSES if getattr(args, name) is not None]
    if mixed:
        raise MeetingOutcomeError(f"{', '.join(mixed)}: cannot be combined with --minutes-url/--minutes-approved-on; "
                                  "minutes only confirm an outcome already recorded")
    outcome = record_minutes(root, args.id, minutes_url, minutes_on, replace=args.replace)
    return [json.dumps(outcome, indent=2), "Recorded only. Nothing was published."]
