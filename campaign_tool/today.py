"""`today`: one ranked starter to-do list with the exact command for each item (read-only).

Pure aggregator over existing read-only helpers. Every source is read separately, so a broken input becomes its own
``unreadable`` item ("fix <file>: ...") and never hides another source's items. Nothing is written, sent or published.
"""
import argparse
import json
import shlex
from datetime import date, datetime, timedelta
from pathlib import Path

from . import findings, kit, kit_assign, kit_track, meeting_comments, meeting_stage
from .content import CONTINUATION_KEYS, _read_json, continuation_issues, continuation_targets, load_agencies, load_meeting

KINDS = ("comment_deadline", "request_past_due", "request_due_today", "request_installment_passed",
         "request_installment_due", "request_download_pending", "request_download_expired", "finding_ready",
         "finding_blocked",
         "contract_ending", "minutes_pending", "continued_pending", "direction_pending", "stale_comment_kit", "stale_contact", "review_waiting",
         "unreadable")
WINDOW_DAYS, CONTACT_MAX_AGE_DAYS, ERROR_CHARS, REVIEW_DAYS = 7, 180, 120, 7
CONTRACT_DAYS, CONTRACT_ENDED_DAYS, MINUTES_DAYS, DIRECTION_DAYS = 90, 30, 45, 14


def contract_days(text):
    """argparse type for ``today --contract-days``: a whole number of days, 1-365."""
    if not (text.isascii() and text.isdigit()) or not 1 <= int(text) <= 365:
        raise argparse.ArgumentTypeError(f"must be a whole number of days from 1 to 365, got {text!r}")
    return int(text)


def review_days(text):
    """argparse type for ``today --review-days``: a whole number of days, 1-90."""
    if not (text.isascii() and text.isdigit()) or not 1 <= int(text) <= 90:
        raise argparse.ArgumentTypeError(f"must be a whole number of days from 1 to 90, got {text!r}")
    return int(text)


def _item(kind, what, command, due=None, key=""):
    return {"kind": kind, "what": what, "due": due, "command": command, "_key": key}


def _unreadable(label, exc):
    return _item("unreadable", f"fix {label}: {str(exc)[:ERROR_CHARS]}", f"open {label} and fix it", key=label)


def _meetings(root, as_of, directory):
    folder, items = root / "content" / "meetings", []
    try:
        submitted = meeting_comments.read(root)  # a comment already sent by hand is not due again (DF1207-09)
    except ValueError as exc:  # bad file: say so once, keep listing every deadline
        items.append(_unreadable(meeting_comments.LABEL, exc))
        submitted = {}
    loaded = []  # DF0022-07, pass 1: one tolerant read of every file; a broken one is its own item and nothing more
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        label = "content/meetings/" + path.name
        try:
            meeting = load_meeting(_read_json(path), "meetings/" + path.name, today=as_of)
            written = (meeting["participation"] or {}).get("written_comment") or {}
            due = written.get("deadline")
            day = datetime.fromisoformat(due.replace("Z", "+00:00")).date() if due else None
        except (ValueError, OSError) as exc:
            items.append(_unreadable(label, exc))
            continue
        loaded.append((label, {**meeting, "id": meeting["id"] or path.stem}, written, due, day))
    staged = [entry[1] for entry in loaded]
    unreliable = _continuation_unreadable(loaded, items)  # pass 2: relationships between the files read
    for label, meeting, written, due, day in loaded:  # pass 3: the reminders of each readable meeting
        ident = meeting["id"]
        if (pending := _minutes_item(meeting, ident, as_of, directory)) is not None:
            items.append(pending)
        if label not in unreliable and (later := _continued_item(meeting, staged, as_of, directory)) is not None:
            items.append(later)
        if (report := _direction_item(meeting, staged, as_of, directory)) is not None:
            items.append(report)
        if meeting["status"] != "scheduled" or day is None or not as_of <= day <= as_of + timedelta(WINDOW_DAYS):
            continue
        if ident in submitted:
            continue
        agenda = f" (agenda: {meeting['agenda_url']})" if meeting["agenda_url"] else ""
        items.append(_item("comment_deadline", f"Written comment due {due} for {meeting['body']} ({ident})",
                           f"submit written comment: {written['how']}{agenda}", due, ident))
    return items


def _continuation_unreadable(loaded, items):
    """DF0022-07: one ``unreadable`` item per meeting file whose continuation cannot be trusted (a duplicate id, or an
    explicit ``continued_to`` that names no valid later meeting of the same body); returns those files' labels. Nothing
    changes while no meeting carries a continuation, and no other meeting's reminder is ever suppressed."""
    staged = [entry[1] for entry in loaded]
    if not any(key in (meeting["outcome"] or {}) for meeting in staged for key in CONTINUATION_KEYS):
        return set()
    labels, flagged = {id(entry[1]): entry[0] for entry in loaded}, {}
    for meeting in staged:
        if sum(1 for other in staged if other["id"] == meeting["id"]) > 1:
            flagged.setdefault(labels[id(meeting)], f"duplicate meeting ID {meeting['id']}")
    for meeting, problem in continuation_issues(staged):
        flagged.setdefault(labels[id(meeting)], f"outcome.continued_to: {problem}")
    items.extend(_unreadable(label, reason) for label, reason in flagged.items())
    return set(flagged)


def _continued_item(meeting, meetings, as_of, directory):
    """DF0022-07: a continued item whose stated date has passed with no meeting of that body staged on it. A follow-up
    cue only: a later meeting on that date is not evidence the item was heard or resolved. An explicit ``continued_to``
    never yields one (it is checked as a reference instead)."""
    outcome = meeting["outcome"] or {}
    stated = outcome.get("continued_to_date")
    if stated is None or stated >= as_of.isoformat() or continuation_targets(meeting, meetings):
        return None
    return _item("continued_pending", f"Continued item for {meeting['body']} ({meeting['id']}): stated date {stated} has "
                 "passed with no matching meeting staged; check the official agenda",
                 f"open {shlex.quote(outcome['source_url'])} and stage the verified follow-up meeting with "
                 f"meetings-stage --directory {directory} --id MEETING_ID", stated, meeting["id"])


def _direction_item(meeting, meetings, as_of, directory):
    """DF0022-08: a staff direction whose source-stated return-by date is within DIRECTION_DAYS days (or already past)
    and that no later outcome of the same body follows. A workflow cue only: a later outcome is not proof the direction
    was met, and a missing one is not a claim that staff failed to report."""
    outcome = meeting["outcome"] or {}
    if "direction" not in outcome or (date.fromisoformat(outcome["return_by"]) - as_of).days > DIRECTION_DAYS:
        return None
    day = meeting["starts_at"][:10]
    if any(other["body"] == meeting["body"] and other["status"] != "cancelled" and other["starts_at"][:10] > day
           and other["outcome"] is not None and other["outcome"]["on"] <= as_of.isoformat() for other in meetings):
        return None
    return _item("direction_pending", f"Staff direction for {meeting['body']} ({meeting['id']}), return-by "
                 f"{outcome['return_by']}: check the official record for a report back",
                 f"open {shlex.quote(outcome['direction_source'])} and check for a report back; record a verified later "
                 f"outcome with meetings-outcome --directory {directory} --id MEETING_ID --result RESULT "
                 "--on YYYY-MM-DD --source OFFICIAL_URL --verified-by NAME", outcome["return_by"], meeting["id"])


def _minutes_item(meeting, ident, as_of, directory):
    """DF2207-05: a recorded outcome older than MINUTES_DAYS days with no approved minutes linked (never an allegation)."""
    outcome = meeting["outcome"]
    if not outcome or "minutes_url" in outcome or (as_of - date.fromisoformat(outcome["on"])).days <= MINUTES_DAYS:
        return None
    return _item("minutes_pending", f"Outcome of {meeting['body']} ({ident}) recorded for {outcome['on']} has no approved "
                 "minutes linked: check whether the body's official minutes are approved",
                 f"meetings-outcome --directory {directory} --id {shlex.quote(ident)} "
                 "--minutes-url OFFICIAL_MINUTES_URL --minutes-approved-on YYYY-MM-DD", outcome["on"], ident)


def _requests(root, as_of, directory, law_base, owner=None):
    items = []
    try:
        kit_assign.read_assignments(root)
    except ValueError as exc:  # one item naming the bad file; no unfiltered fallback, other sources still listed
        return [_unreadable(kit_assign.FILE, exc)]
    command = f"kit-status --directory {directory}" + (f" --owner {shlex.quote(owner)}" if owner is not None else "")
    for row in kit_track.due(root, as_of.isoformat(), law_base=law_base, owner=owner)["open"]:
        name = f"{row['name'] or row['agency_id']} ({kit_track._label(row['agency_id'], row['request'])})"
        if row["state"] == "past_due":
            what = (f"Follow up with {name}: request sent {row['sent_on']} passed its provisional determination "
                    f"date {row['due']} {-row['days']} day(s) ago")
        elif row["state"] == "due_today":
            what = f"Check on {name}: provisional determination date for the request sent {row['sent_on']} is today"
        elif row["state"] == "installment_passed":
            what = f"Check on {name}: the next installment the agency stated for {row['due']} is not recorded yet"
        elif row["state"] == "producing" and row["days"] == 0:
            what, row = (f"Check on {name}: the next installment the agency stated for {row['due']} is due today",
                         {**row, "state": "installment_due"})
        elif row["state"] == "download_pending":  # an expiring link: its own agency-stated date, days left
            n = row["days"]
            what = f"Download records from {name} before {row['due']} ({n} day{'' if n == 1 else 's'} left)"
        elif row["state"] == "download_expired":
            what = f"{name}: link expired {row['due']}: {kit_track.DOWNLOAD_ASK}"
        else:
            continue
        if "owner" in row:
            what += f" [owner: {row['owner']}]"
        items.append(_item("request_" + row["state"], what, command, row["due"],
                           row["agency_id"] + (row["request"] if row.get("kind") == "download"
                                               else "" if row["request"] == "main" else "#" + row["request"])))
    return items


def _findings(root, directory):
    items = []
    for row in findings.status_rows(root, draft_paths=True):
        if row["state"] == "unreadable":
            items.append(_unreadable(row["path"], row["error"]))  # status_rows' own path and withheld-reason text
        elif row["state"] in ("ready", "blocked"):  # only drafts reach these states; name the file actually read
            draft, key = shlex.quote(row["draft_path"]), f"{row['id']} {row['draft_path']}"
            items.append(_item("finding_ready", f"Finding {row['id']} is reviewed and ready to publish",
                               f"finding publish {draft} --directory {directory} --approved-by NAME", key=key)
                         if row["state"] == "ready" else
                         _item("finding_blocked", f"Finding {row['id']} is blocked: {', '.join(row['blockers'])}",
                               f"finding review-status {draft} --directory {directory}", key=key))
    return items


def _review_waiting(root, as_of, directory, days):
    """Assigned review roles with no current-content receipt after ``days`` days (assignments never count as review)."""
    items = []
    for row in findings.status_rows(root, draft_paths=True):
        if "draft_path" not in row or row["state"] == "withdrawn":  # unreadable rows are reported by _findings
            continue
        try:
            draft = findings.load_draft(row["draft_path"])
            assigned, states = findings.load_assignments(root, draft), None
            if assigned:
                states = findings.assignment_states(root, draft)
        except ValueError as exc:  # one bad assignment or receipt file never hides another draft's items
            items.append(_unreadable(f"private/review-assignments/{row['id']}.json", exc))
            continue
        for role, entry in sorted(assigned.items()):
            if states[role]["state"] == "assigned" and (as_of - date.fromisoformat(entry["on"])).days >= days:
                items.append(_item("review_waiting", f"Review ({role}) of {row['id']} assigned to "
                                   f"{entry['reviewer']} since {entry['on']}",
                                   f"finding review-status {shlex.quote(row['draft_path'])} --directory {directory}",
                                   entry["on"], f"{row['id']} {role}"))
    return items


def _stale_kits(root, directory):
    by_meeting = {}
    for entry in meeting_stage.stale_kits(root):
        by_meeting.setdefault(entry["meeting"], []).append(f"{entry['finding']} (corrected {entry['latest_correction']})")
    return [_item("stale_comment_kit", f"Comment kit for meeting {meeting} cites {', '.join(cited)} without the "
                  "latest correction", f"meetings-stage --directory {directory} --id {shlex.quote(meeting)} --replace",
                  key=meeting) for meeting, cited in sorted(by_meeting.items())]


def _contacts(root, as_of, directory):
    stale = kit.stale_contacts(root, CONTACT_MAX_AGE_DAYS, as_of)
    if stale is None:
        raise ValueError("kit/agencies.json is not readable JSON")
    return [_item("stale_contact", f"Re-check the records contact for {entry['agency_id']} on its official site ("
                  + (f"last checked {entry['checked_on']}, {entry['age_days']} days ago)" if entry["checked_on"]
                     else "no valid checked_on date)"),
                  f"kit-verify-agency --directory {directory} --agency-id {shlex.quote(entry['agency_id'])} "
                  "--source-url OFFICIAL_URL --checked-on YYYY-MM-DD --records-email ADDRESS",
                  entry["checked_on"], entry["agency_id"]) for entry in stale]


def _contracts(root, as_of, directory, days):
    path = root / "content" / "agencies.json"
    if not path.exists() and not path.is_symlink():  # no agency list: nothing to watch
        return []
    items = []
    for agency in load_agencies(_read_json(path), "content/agencies.json", today=as_of):  # the build's validation
        if (term := agency["contract"]) is None:
            continue
        left = (date.fromisoformat(term["end"]) - as_of).days
        head = f"Contract with {term['vendor']} for {agency['name']} ({agency['agency_id']})"
        if 0 <= left <= days:
            step = f"ends {term['end']} (in {left} days): ask for the renewal staff report and watch the agenda"
        elif -CONTRACT_ENDED_DAYS <= left < 0:
            step = f"ended {term['end']} ({-left} days ago): ask whether it was renewed and for the staff report"
        else:
            continue
        note = f" Note: {term['renewal_note']}" if term["renewal_note"] else ""
        items.append(_item("contract_ending", f"{head} {step}. Source: {term['source_url']} (checked "
                           f"{term['checked_on']}){note}", f"meetings --directory {directory} --client "
                           "LEGISTAR_CLIENT --online", term["end"], agency["agency_id"]))
    return items


def todo(root, as_of, *, law_base=None, contract_days=CONTRACT_DAYS, review_days=REVIEW_DAYS, owner=None):
    """Ranked to-do items ``{rank, kind, what, due, command}``; reads only, never raises for a broken input.

    ``owner`` keeps only that teammate's request work; meetings, findings, contracts and contacts stay listed."""
    root = Path(root)
    directory = shlex.quote(str(root))
    owner = None if owner is None else kit_assign.owner_label(owner, "--owner")
    sources = ((lambda: _meetings(root, as_of, directory), "content/meetings/"),
               (lambda: _requests(root, as_of, directory, law_base, owner), "kit/sent.json"),
               (lambda: _findings(root, directory), "private/drafts/"),
               (lambda: _contracts(root, as_of, directory, contract_days), "content/agencies.json"),
               (lambda: _review_waiting(root, as_of, directory, review_days), "private/review-assignments/"),
               (lambda: _stale_kits(root, directory), "content/"),
               (lambda: _contacts(root, as_of, directory), "kit/agencies.json"))
    items = []
    for read, label in sources:
        try:
            items.extend(read())
        except (ValueError, OSError) as exc:  # one failing source never hides another's items
            items.append(_unreadable(label, exc))
    items.sort(key=lambda item: (KINDS.index(item["kind"]), item["due"] or "", item["_key"]))
    return [{"rank": rank, **{k: v for k, v in item.items() if k != "_key"}} for rank, item in enumerate(items, 1)]


def run_cli(root, as_of=None, as_json=False, contract_days=CONTRACT_DAYS, review_days=REVIEW_DAYS, owner=None):
    day = date.today() if as_of is None else kit_track._day(as_of)
    if day is None:
        raise ValueError("--as-of must be a real YYYY-MM-DD date")
    items = todo(root, day, contract_days=contract_days, review_days=review_days, owner=owner)
    if as_json:
        return json.dumps(items, indent=2, ensure_ascii=False)
    if not items:
        return f"Nothing due on {day.isoformat()}."
    return "\n".join(f"{item['rank']}. [{item['kind']}] {item['what']} — {item['command']}" for item in items)
