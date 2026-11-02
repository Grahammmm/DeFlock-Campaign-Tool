"""Record an organizer-verified outcome on a past staged meeting (DF2237-11).

Offline only: the organizer reads the result on the agency's own record (minutes, video, vote page) and
types it in; nothing is fetched or published. The whole meeting document is validated by
``content.load_meeting`` before the atomic write.
"""
import os
from datetime import date

from .content import _read_json, load_meeting
from .kit import _write_json


class MeetingOutcomeError(ValueError):
    pass


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


def record_outcome(root, meeting_id, result, on, source_url, verified_by, vote=None, replace=False, today=None):
    today = today or date.today()
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
               "verified_at": today.isoformat(), **({"vote": vote} if vote is not None else {})}
    updated = {**document, "outcome": outcome}
    load_meeting(updated, label, today=today)
    if os.path.islink(path):  # checked again right before the write
        raise MeetingOutcomeError(f"content/meetings/{path.name} must not be a symlink")
    _write_json(path, updated)
    return outcome
