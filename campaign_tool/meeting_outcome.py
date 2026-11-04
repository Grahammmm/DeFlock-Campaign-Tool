"""Record an organizer-verified outcome on a past staged meeting (DF2237-11).

Offline only: the organizer reads the result on the agency's own record (minutes, video, vote page) and
types it in; nothing is fetched or published. The whole meeting document is validated by
``content.load_meeting`` before the atomic write.
"""
import csv
import os
import stat
import unicodedata
from datetime import date

from .content import VOTE_VALUES, _read_json, default_agencies, load_agencies, load_meeting, load_officials
from .kit import _write_json


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


def _find(folder, meeting_id):
    """The staged ``content/meetings/<file>`` whose id (or filename stem) is ``meeting_id``; never follows a symlink."""
    paths = sorted(folder.glob("*.json")) if folder.is_dir() and not os.path.islink(folder) else []
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
                   votes_file=None, recording_url=None, recording_start=None):
    today = today or date.today()
    if recording_start is not None and recording_url is None:  # DF1422-10: a start time is part of a recording
        raise MeetingOutcomeError("recording_start needs recording_url")
    rows = read_votes(votes_file) if votes_file is not None else None  # before any read of the meeting
    path, document = _find(root / "content" / "meetings", meeting_id)
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
               **({"recording_start": recording_start} if recording_start is not None else {})}
    updated = {**document, "outcome": outcome}
    load_meeting(updated, label, today=today)
    if os.path.islink(path):  # checked again right before the write
        raise MeetingOutcomeError(f"content/meetings/{path.name} must not be a symlink")
    _write_json(path, updated)
    return outcome
