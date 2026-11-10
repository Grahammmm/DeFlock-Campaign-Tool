"""`today`: one ranked starter to-do list with the exact command for each item (read-only).

Pure aggregator over existing read-only helpers. Every source is read separately, so a broken input becomes its own
``unreadable`` item ("fix <file>: ...") and never hides another source's items. Nothing is written, sent or published.

DF1109-03: existing private press, social and newsletter drafts are run through their own read-only checks (press
check, social --check, newsletter --check) at the UTC start of the as-of day; one ``outreach_stale`` item per tool
gives the problem count and the check to run. A newsletter draft without provenance is one ``outreach_not_checked``
info item, never a match claim. Absent drafts add nothing; no draft is generated, posted or sent.
"""
import argparse
import json
import os
import shlex
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import body_schedules, correction_requests, findings, kit, kit_assign, kit_track, meeting_comments, meeting_stage
from .body_schedules import read_body_schedules
from .content import (CONTINUATION_KEYS, _agency_response, _read_json, continuation_issues, continuation_targets,
                      load_agencies, load_meeting)
from .meetings_manual import ManualMeetingError, _read_scan

KINDS = ("comment_deadline", "request_past_due", "request_due_today", "request_waiting_on_you", "request_fee_unpaid",
         "request_stated_date_passed", "request_installment_passed",
         "request_installment_due", "request_stated_extension_date_passed", "request_download_pending", "request_download_expired",
         "finding_reply_due", "correction_request_open", "finding_ready",
         "finding_blocked", "outreach_stale", "outreach_not_checked",
         "contract_ending", "outcome_missing", "participation_unverified", "meeting_not_staged",
         "minutes_pending", "continued_pending", "direction_pending", "stale_comment_kit", "stale_contact", "review_waiting",
         "stale_body_schedule", "expected_regular_meeting", "backup_due", "unreadable")
WINDOW_DAYS, CONTACT_MAX_AGE_DAYS, ERROR_CHARS, REVIEW_DAYS = 7, 180, 120, 7
BACKUP_DAYS = 7
FINDING_REPLY_DAYS = 3  # DF1109-02: a pending reply window is listed from three days before its reply_by date
CORRECTION_LOG = "private/" + correction_requests.NAME
WITHHELD = "could not be read (reason withheld: it can quote the file)"
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


def backup_days(text):
    """argparse type for ``today --backup-days``: a whole number of days, 1-90."""
    if not (text.isascii() and text.isdigit()) or not 1 <= int(text) <= 90:
        raise argparse.ArgumentTypeError(f"must be a whole number of days from 1 to 90, got {text!r}")
    return int(text)


def _item(kind, what, command, due=None, key=""):
    return {"kind": kind, "what": what, "due": due, "command": command, "_key": key}


def _unreadable(label, exc):
    return _item("unreadable", f"fix {label}: {str(exc)[:ERROR_CHARS]}", f"open {label} and fix it", key=label)


def _meetings(root, as_of, selector):
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
        if (pending := _minutes_item(meeting, ident, as_of, selector)) is not None:
            items.append(pending)
        if label not in unreliable and (later := _continued_item(meeting, staged, as_of, selector)) is not None:
            items.append(later)
        if (report := _direction_item(meeting, staged, as_of, selector)) is not None:
            items.append(report)
        if sum(1 for other in staged if other["id"] == ident) == 1 and (
                pending := _meeting_work(meeting, ident, as_of, selector)) is not None:  # a duplicate id is already unreadable
            items.append(pending)
        if meeting["status"] != "scheduled" or day is None or not as_of <= day <= as_of + timedelta(WINDOW_DAYS):
            continue
        if ident in submitted:
            continue
        agenda = f" (agenda: {meeting['agenda_url']})" if meeting["agenda_url"] else ""
        items.append(_item("comment_deadline", f"Written comment due {due} for {meeting['body']} ({ident})",
                           f"submit written comment: {written['how']}{agenda}", due, ident))
    items.extend(_unstaged_meetings(root, as_of, selector, {entry[1]["id"] for entry in loaded}))
    return items


def _meeting_work(meeting, ident, as_of, selector):
    """DF1109B-02: a scheduled staged meeting whose day has passed with no outcome (a recording cue, not proof it was
    held), or one starting within 14 days with no organizer-verified participation. Read-only; nothing is invented."""
    if meeting["status"] != "scheduled":
        return None
    start, quoted = meeting["starts_at"][:10], shlex.quote(ident)
    if meeting["outcome"] is None and start < as_of.isoformat():
        return _item("outcome_missing", f"{meeting['body']} was scheduled for {start} ({ident}) with no outcome "
                     "recorded: verify what happened, then record it",
                     f"meetings-outcome {selector} --id {quoted} --result RESULT --on YYYY-MM-DD "
                     "--source OFFICIAL_URL --verified-by NAME", start, ident)
    if meeting["participation"] is None and as_of.isoformat() <= start <= (as_of + timedelta(14)).isoformat():
        return _item("participation_unverified", f"Verify how the public may take part in {meeting['body']} on {start} "
                     f"({ident})", f"verify how the public may take part, then meetings-stage {selector} --id {quoted} "
                     "--participation FILE --replace", start, ident)
    return None


def _unstaged_meetings(root, as_of, selector, staged_ids):
    """DF1109B-02: agenda matches in kit/meetings.json starting within 14 days that have no staged meeting. Reads only
    that scan file; a bad one is exactly one unreadable item and no partial rows."""
    label = "kit/meetings.json"
    try:
        path = root / "kit" / "meetings.json"
        if os.path.lexists(path) and not path.is_file() and not os.path.islink(path):
            raise ValueError(f"{label}: is not a regular file")
        rows = _read_scan(root)
        found, seen = [], set()
        for n, raw in enumerate(rows or []):
            if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
                raise ValueError(f"{label}: meetings[{n}] needs an id")
            meeting = load_meeting({k: raw[k] for k in ("id", "body", "starts_at", "status", "agenda_url", "agenda_item",
                                                        "location") if k in raw}, f"{label} meetings[{n}]", today=as_of)
            if meeting["id"] in seen:
                raise ValueError(f"{label}: duplicate meeting id")
            seen.add(meeting["id"])
            found.append(meeting)
    except (ValueError, OSError, ManualMeetingError) as exc:
        return [_unreadable(label, exc)]
    items, folder = [], root / "content" / "meetings"
    for meeting in found:
        ident, start = meeting["id"], meeting["starts_at"][:10]
        if (meeting["status"] != "scheduled" or not as_of.isoformat() <= start <= (as_of + timedelta(14)).isoformat()
                or ident in staged_ids or os.path.lexists(folder / f"{ident}.json")):
            continue
        items.append(_item("meeting_not_staged", f"Agenda match for {meeting['body']} on {start} ({ident}): verify the "
                           f"posted agenda and stage the meeting", f"meetings-stage {selector} --id {shlex.quote(ident)}",
                           start, ident))
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


def _continued_item(meeting, meetings, as_of, selector):
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
                 f"meetings-stage {selector} --id MEETING_ID", stated, meeting["id"])


def _direction_item(meeting, meetings, as_of, selector):
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
                 f"outcome with meetings-outcome {selector} --id MEETING_ID --result RESULT "
                 "--on YYYY-MM-DD --source OFFICIAL_URL --verified-by NAME", outcome["return_by"], meeting["id"])


def _minutes_item(meeting, ident, as_of, selector):
    """DF2207-05: a recorded outcome older than MINUTES_DAYS days with no approved minutes linked (never an allegation)."""
    outcome = meeting["outcome"]
    if not outcome or "minutes_url" in outcome or (as_of - date.fromisoformat(outcome["on"])).days <= MINUTES_DAYS:
        return None
    return _item("minutes_pending", f"Outcome of {meeting['body']} ({ident}) recorded for {outcome['on']} has no approved "
                 "minutes linked: check whether the body's official minutes are approved",
                 f"meetings-outcome {selector} --id {shlex.quote(ident)} "
                 "--minutes-url OFFICIAL_MINUTES_URL --minutes-approved-on YYYY-MM-DD", outcome["on"], ident)


def _request_name(row):
    return f"{row['name'] or row['agency_id']} ({kit_track._label(row['agency_id'], row['request'])})"


def _request_item(kind, row, what, command, due, key):
    """One request item: the owner suffix and the recorded-receipt note exactly as every request kind shows them;
    ``received_on``/``receipt_evidence`` are copied into the JSON only when the row carries them."""
    if "owner" in row:
        what += f" [owner: {row['owner']}]"
    extra = {k: row[k] for k in ("received_on", "receipt_evidence") if k in row}
    if extra:  # counted from the recorded agency receipt: say so, never as a legal deadline
        what += f" ({kit_track.receipt_note(row['received_on'], row['receipt_evidence'])})"
    return {**_item(kind, what, command, due, key), **extra}


def _reply_command(writer, selector, row):  # a private draft writer for this one request; guidance only, never run
    return (f"{writer} {selector} --agency {shlex.quote(row['agency_id'])}"
            + ("" if row["request"] == "main" else f" --request {shlex.quote(row['request'])}"))


def _request_key(row):  # agency and request, as the provisional-date items are keyed
    return row["agency_id"] + ("" if row["request"] == "main" else "#" + row["request"])


def _unpaid_fees(root, selector, law_base, owner):
    """DF1109-01: one ``request_fee_unpaid`` item per open (not closed or withdrawn) request whose latest fee estimate
    (by date, then recorded order) has no ``fee_paid``/``fee_waived`` step after it: kit-status's own amount and the
    estimate's own date. A zero-dollar estimate still counts. Nothing beyond kit/sent.json is read when no request is
    recorded; a malformed store raises, so the caller lists no partial request work."""
    sent = kit_track.read_sent(root)
    if not sent:
        return []
    steps = {kit_track._split(key): [s for s in entry["steps"] if isinstance(s, dict)] for key, entry in sent.items()}
    items = []
    for row in kit_track.status(root, law_base=law_base, owner=owner)["requests"]:
        if row["closed"] or "withdrawn_on" in row or row["fee_state"] != "unpaid":
            continue
        listed = steps.get((row["agency_id"], row["request"])) or []
        last = kit_track._fee(listed)[0]
        if last is None:  # the file changed between the two reads: list no partial request work
            raise ValueError("kit/sent.json unreadable: changed while it was read")
        on = listed[last]["on"]
        items.append(_request_item("request_fee_unpaid", row, f"{_request_name(row)}: unpaid fee estimate "
                                   f"{row['fee_estimate']} recorded {on}; review and reply",
                                   "kit " + _reply_command("fee-reply", selector, row), on, _request_key(row)))
    return items


def _requests(root, as_of, selector, law_base, owner=None):
    """Request work for the organizer, from kit_track's own readers (read-only; commands are guidance only).

    Besides the provisional determination items, DF1109-01 lists work waiting on the organizer: an open agency
    clarification request (``request_waiting_on_you``, due its own date; no claim that it pauses any legal period),
    an unpaid latest fee estimate (``request_fee_unpaid``) and an answered request whose agency-stated production date
    has passed (``request_stated_date_passed``: the agency's own date, provisional, not a legal deadline). Each is keyed
    by agency and request, keeps ``--owner`` and the received_on note, and is built in full or not at all."""
    items = []
    try:
        kit_assign.read_assignments(root)
    except ValueError as exc:  # one item naming the bad file; no unfiltered fallback, other sources still listed
        return [_unreadable(kit_assign.FILE, exc)]
    command = f"kit-status {selector}" + (f" --owner {shlex.quote(owner)}" if owner is not None else "")
    report = kit_track.due(root, as_of.isoformat(), law_base=law_base, owner=owner)
    for row in report["stated_passed"]:
        items.append(_request_item("request_stated_date_passed", row, f"{_request_name(row)}: stated date passed "
                                   f"{row['stated']} — the agency's own date; provisional, not a legal deadline; "
                                   "follow up", _reply_command("kit-followup", selector, row), row["stated"],
                                   _request_key(row)))
    items += _unpaid_fees(root, selector, law_base, owner)
    for row in report["open"]:
        name = _request_name(row)
        if row["state"] == "waiting_on_you":
            items.append(_request_item("request_waiting_on_you", row, f"{name}: the agency asked for clarification on "
                                       f"{row['since']}; reply", _reply_command("kit-clarify-reply", selector, row),
                                       row["since"], _request_key(row)))
            continue
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
        elif row["state"] == "stated_extension_date_passed":  # the agency's own notice date; not a legal finding
            later = f"; provisional extended date {row['with_extension']}" if row.get("with_extension") else ""
            what = (f"Check on {name}: {kit_track.EXTENSION_PASSED} {row['due']}{later}"
                    + (f" ({kit_track.EXTENSION_LATER})" if kit_track.EXTENSION_LATER in row["note"] else "")
                    + "; no determination, production or denial is recorded")
        elif row["state"] == "download_pending":  # an expiring link: its own agency-stated date, days left
            n = row["days"]
            what = f"Download records from {name} before {row['due']} ({n} day{'' if n == 1 else 's'} left)"
        elif row["state"] == "download_expired":
            what = f"{name}: link expired {row['due']}: {kit_track.DOWNLOAD_ASK}"
        else:
            continue
        items.append(_request_item("request_" + row["state"], row, what, command, row["due"],
                                   row["agency_id"] + row["request"] if row.get("kind") in ("download", "extension")
                                   else _request_key(row)))
    return items


def _reply_item(row, as_of, selector):
    """DF1109-02: one ``finding_reply_due`` for a private draft whose agency_response is pending with reply_by at most
    FINDING_REPLY_DAYS days away or already past (listed until a reply is recorded); None otherwise. A recorded window,
    never a legal deadline: preparing a request is not proof anyone asked, so no asked date or lateness is claimed. The
    command's STATUS is the organizer's own choice of received, declined or no_response (``finding reply`` keeps its
    guards: the real asked date, no_response only on or after reply_by, the text a received reply needs). A draft
    that cannot be reloaded or whose response is invalid raises ValueError/OSError for the caller to name."""
    draft = findings.load_draft(row["draft_path"])
    if "agency_response" not in draft:
        return None
    response = _agency_response(draft["agency_response"], "agency_response", findings._cited_hashes(draft))
    if response["status"] != "pending" or (date.fromisoformat(response["reply_by"]) - as_of).days > FINDING_REPLY_DAYS:
        return None
    by, ident = response["reply_by"], draft["id"]
    what = (f"reply window for {ident} ends {by}" if by >= as_of.isoformat() else
            f"reply window for {ident} ended {by}: record the reply or no_response")
    return _item("finding_reply_due", what, f"finding reply {shlex.quote(row['draft_path'])} {selector} "
                 "--status STATUS --on YYYY-MM-DD --asked-on YYYY-MM-DD", by, f"{ident} {row['draft_path']}")


def _findings(root, as_of, selector):
    """Draft work from status_rows' private draft rows: ready/blocked, and (DF1109-02) pending reply windows. A draft
    status_rows already reports unreadable is named once; one whose response cannot be checked is its own item."""
    items = []
    for row in findings.status_rows(root, draft_paths=True):
        if "draft_path" in row and row["state"] != "withdrawn":
            try:
                if (reply := _reply_item(row, as_of, selector)) is not None:
                    items.append(reply)
            except (ValueError, OSError):  # the reason can quote the file: withheld, the other drafts still listed
                items.append(_unreadable(row["draft_path"], f"agency_response {WITHHELD}"))
        if row["state"] == "unreadable":
            items.append(_unreadable(row["path"], row["error"]))  # status_rows' own path and withheld-reason text
        elif row["state"] in ("ready", "blocked"):  # only drafts reach these states; name the file actually read
            draft, key = shlex.quote(row["draft_path"]), f"{row['id']} {row['draft_path']}"
            items.append(_item("finding_ready", f"Finding {row['id']} is reviewed and ready to publish",
                               f"finding publish {draft} {selector} --approved-by NAME", key=key)
                         if row["state"] == "ready" else
                         _item("finding_blocked", f"Finding {row['id']} is blocked: {', '.join(row['blockers'])}",
                               f"finding review-status {draft} {selector}", key=key))
    return items


def _correction_requests(root, as_of, selector):
    """DF1109-02: one ``correction_request_open`` per correction request open more than 14 days (correction_requests'
    own open_overdue rule, as doctor uses it), with its integer days_open. No log: nothing, and nothing is created.
    Each id is rechecked in list_requests, so a decision recorded between the two reads lists no stale work. Never the
    summary, decision note or requester; the command's DECISION is the organizer's (accepted needs a recorded
    correction first). A broken log is one ``unreadable`` item with a fixed reason: its own text can quote the file."""
    try:
        overdue = correction_requests.open_overdue(root, as_of) or []
        rows = correction_requests.list_requests(root, today=as_of) if overdue else []
    except (ValueError, OSError):
        return [_unreadable(CORRECTION_LOG, f"the correction request log {WITHHELD}")]
    return [{**_item("correction_request_open", f"Correction request {row['id']} for finding {_show(row['finding_id'])} "
                     f"has been open {row['days_open']} days; record a decision",
                     f"correction-request decide DECISION {selector} --id {row['id']} --on YYYY-MM-DD "
                     "--note NOTE", row["received_on"], f"{row['id']:012d}"), "days_open": row["days_open"]}
            for row in rows if row["id"] in overdue and row["state"] == "open" and row["days_open"] > 14]


def _moment(as_of):  # DF1109-03: stored drafts are checked at the UTC start of the as-of day
    return datetime.combine(as_of, datetime.min.time(), tzinfo=timezone.utc)


# Split from one string: the publication-path scan reads an exact "social" constant as a kit/social writer, and this
# module only reads (it writes nothing), so it stays out of that registry rather than being added to it.
PRESS, SOCIAL, NEWSLETTER = "press social newsletter".split()


def _present(root, *names):
    """Whether any named kit/ draft exists. lexists: a link or special file is present (the check names it), never
    absent. A kit/ that is itself a link, a regular file or other non-folder is a hazard to report, not absence: the
    tool's own check runs and names it."""
    kit_dir = root / "kit"
    if os.path.islink(kit_dir) or (os.path.lexists(kit_dir) and not os.path.isdir(kit_dir)):
        return True
    return any(os.path.lexists(kit_dir / name) for name in names)


def _press(root, as_of, selector):
    """DF1109-03: press check's own problem count (stale, not-checked or unsafe rows) as one item; no folder, nothing."""
    if not _present(root, PRESS, f"{PRESS}.suggested"):
        return []
    from .press_check import check_press
    n = check_press(root, now=_moment(as_of))["problems"]
    return [_item("outreach_stale", f"press: {n} draft check problem(s); inspect before use",
                  f"press check {selector}", key=PRESS)] if n else []


def _social(root, as_of, selector):
    """DF1109-03: stale or outdated social drafts from social --check, kit/social/ and each folder block counted once.
    A missing draft or an edit-only problem is not counted; a folder or draft the check could not read is one
    ``unreadable`` item next to any count, never a fresh match. No social folder: nothing, content is not read."""
    if not _present(root, SOCIAL, f"{SOCIAL}.suggested"):
        return []
    from .social import check_social
    report = check_social(root, now=_moment(as_of))
    stale, hazards = 0, []
    for rel, block in [(f"kit/{SOCIAL}", report), *report.get("folders", {}).items()]:
        stale += len(block["stale"]) + sum(1 for row in block["drafts"]
                                           if row["state"] == "outdated" or "outdated" in row["problems"])
        if block.get("folder_problems") or any(row["state"] == "not_regular" for row in block["drafts"]):
            hazards.append(rel)
    items = [_item("outreach_stale", f"social: {stale} stale or outdated draft(s); inspect before use",
                   f"social {selector} --check", key=SOCIAL)] if stale else []
    if hazards:
        items.append(_unreadable(f"kit/{SOCIAL}/", f"social --check could not check {', '.join(hazards)}"))
    return items


def _newsletter(root, as_of, selector):
    """DF1109-03: newsletter --check's difference count; a draft without provenance is an info item, not a match."""
    if not _present(root, f"{NEWSLETTER}-draft.json"):
        return []
    from .newsletter.check import check_draft
    report = check_draft(root, now=_moment(as_of))
    # the newsletter module takes only --directory (no --campaign flag, by design), so its hint always names the path
    command = f"python3 -m campaign_tool.newsletter --directory {shlex.quote(str(root))}"
    if report["status"] == "not_checked":
        return [_item("outreach_not_checked", "newsletter draft not checked; rebuild with --from-content",
                      command + " --from-content", key=NEWSLETTER)]
    n = len(report["differences"])
    return [_item("outreach_stale", f"newsletter: {n} content difference(s); inspect before use",
                  command + " --check", key=NEWSLETTER)] if report["status"] == "different" else []


def _review_waiting(root, as_of, selector, days):
    """Assigned review roles with no current-content receipt after ``days`` days (assignments never count as review)."""
    items = []
    for row in findings.status_rows(root, draft_paths=True):
        if "draft_path" not in row or row["state"] == "withdrawn":  # unreadable rows are reported by _findings
            continue
        try:
            draft = findings.load_draft(row["draft_path"])
            assigned, states = findings.load_assignments(root, draft), None
            if assigned:
                states = findings.assignment_states(root, draft, as_of)
        except ValueError as exc:  # one bad assignment or receipt file never hides another draft's items
            items.append(_unreadable(f"private/review-assignments/{row['id']}.json", exc))
            continue
        for role, entry in sorted(assigned.items()):
            state = states[role]
            if state["state"] not in ("assigned", "needs_reassignment") \
                    or (as_of - date.fromisoformat(entry["on"])).days < days:
                continue
            if state["state"] == "assigned":
                items.append(_item("review_waiting", f"Review ({role}) of {row['id']} assigned to "
                                   f"{entry['reviewer']} since {entry['on']}",
                                   f"finding review-status {shlex.quote(row['draft_path'])} {selector}",
                                   entry["on"], f"{row['id']} {role}"))
            else:  # DF1109B-14: the assignee's role is retired; ask another person, never the retired one
                items.append(_item("review_waiting", f"Review ({role}) of {row['id']} assigned to "
                                   f"{entry['reviewer']} since {entry['on']} needs reassignment: their {role} role "
                                   f"was retired on {state['retired_on']}",
                                   f"finding assign {shlex.quote(row['draft_path'])} {selector} "
                                   f"--role {role} --reviewer 'ACTIVE REVIEWER' --on {as_of} --replace",
                                   entry["on"], f"{row['id']} {role}"))
    return items


def _stale_kits(root, selector):
    by_meeting = {}
    for entry in meeting_stage.stale_kits(root):
        by_meeting.setdefault(entry["meeting"], []).append(f"{entry['finding']} (corrected {entry['latest_correction']})")
    return [_item("stale_comment_kit", f"Comment kit for meeting {meeting} cites {', '.join(cited)} without the "
                  "latest correction", f"meetings-stage {selector} --id {shlex.quote(meeting)} --replace",
                  key=meeting) for meeting, cited in sorted(by_meeting.items())]


def _contacts(root, as_of, selector):
    stale = kit.stale_contacts(root, CONTACT_MAX_AGE_DAYS, as_of)
    if stale is None:
        raise ValueError("kit/agencies.json is not readable JSON")
    return [_item("stale_contact", f"Re-check the records contact for {entry['agency_id']} on its official site ("
                  + (f"last checked {entry['checked_on']}, {entry['age_days']} days ago)" if entry["checked_on"]
                     else "no valid checked_on date)"),
                  f"kit-verify-agency {selector} --agency-id {shlex.quote(entry['agency_id'])} "
                  "--source-url OFFICIAL_URL --checked-on YYYY-MM-DD --records-email ADDRESS",
                  entry["checked_on"], entry["agency_id"]) for entry in stale]


def _contracts(root, as_of, selector, days):
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
                           f"{term['checked_on']}){note}", f"meetings {selector} --client "
                           "LEGISTAR_CLIENT --online", term["end"], agency["agency_id"]))
    return items


def _body_schedules(root, as_of):
    """DF0022-10: for each organizer-verified regular body schedule, the next two expected dates with no staged meeting
    of that body ("watch for the agenda"; never a scheduled meeting, never a posting deadline), plus a stale warning.
    A broken schedule file raises (one ``unreadable`` item from ``todo``); a broken meeting file is already its own
    item in ``_meetings`` and simply suppresses nothing here."""
    schedules = read_body_schedules(root, today=as_of)
    if not schedules:
        return []
    staged, folder = [], root / "content" / "meetings"
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        try:
            staged.append(load_meeting(_read_json(path), "meetings/" + path.name, today=as_of))
        except (ValueError, OSError):
            continue
    items = []
    for schedule in schedules:
        body, source = schedule["body"], schedule["source_url"]
        if body_schedules.stale(schedule, as_of):
            items.append(_item("stale_body_schedule", f"Regular meeting schedule for {body} is stale (checked "
                               f"{schedule['checked_on']}); re-check {source}",
                               f"Re-check {source} and update content/body-schedules.json", schedule["checked_on"], body))
        for day in body_schedules.next_expected_dates(schedule, staged, as_of):
            items.append(_item("expected_regular_meeting", f"Expected {body} meeting {day.isoformat()}: watch for the agenda",
                               f"Check {source} for the agenda; stage only a verified meeting", day.isoformat(),
                               f"{body} {day.isoformat()}"))
    return items


def _show(text):
    """A filesystem name for display: control and non-printable characters become escaped text."""
    return "".join(c if c.isprintable() else c.encode("unicode_escape").decode("ascii") if ord(c) < 256
                   else f"\\u{ord(c):04x}" if ord(c) < 0x10000 else f"\\U{ord(c):08x}" for c in str(text))


def _backups(folder, as_of, selector, days):
    """DF0237-10: one ``backup_due`` item unless a verified archive no older than ``days`` exists in ``folder``."""
    from . import backup
    shown, quoted = _show(folder), shlex.quote(str(folder))
    command = f"backup {selector} --into {quoted}"
    inspect = f"backup-list --dir {quoted}"
    unreadable = lambda label, reason: _item("unreadable", f"fix {label}: {reason}", inspect, key=label)
    try:
        found = backup.list_archives(folder)
        rows = [(backup._created(row), row) for row in found["archives"] if row["verify"] == "ok"]
    except (backup.BackupError, OSError) as exc:
        return [unreadable(shown, "backup directory could not be read"),
                _item("backup_due", f"Could not establish a verified backup in {shown}.", command, key=str(folder))]
    items = [unreadable(f"{shown}/{_show(row['name'])}", "archive could not be verified; not counted as a backup")
             for row in found["archives"] if row["verify"] != "ok"]
    items += [unreadable(f"{shown}/{_show(row['name'])}", "symlink archive skipped; not counted as a backup")
              for row in found["skipped"]]
    if not rows:
        items.append(_item("backup_due", f"No verified backup found in {shown}.", command, key=str(folder)))
        return items
    when, row = max(rows, key=lambda pair: pair[0])  # first of equal instants keeps list_archives' name order
    age = (as_of - when.date()).days
    if age > days:
        day = when.date().isoformat()
        items.append(_item("backup_due", f"Last verified backup {_show(row['name'])} was created {day} "
                           f"({age} days ago; limit {days} days).", command, due=day, key=str(folder)))
    return items


def todo(root, as_of, *, law_base=None, contract_days=CONTRACT_DAYS, review_days=REVIEW_DAYS, owner=None,
         backups=None, backup_days=BACKUP_DAYS, campaign_name=None):
    """Ranked to-do items ``{rank, kind, what, due, command}``; reads only, never raises for a broken input.

    ``owner`` keeps only that teammate's request work; meetings, findings, contracts and contacts stay listed.
    ``campaign_name`` (``campaigns today`` only, DF1109-15) puts ``--campaign NAME`` at every campaign selector
    instead of ``--directory PATH``; path operands (draft files, ``--into``) and human instructions are unchanged."""
    root = Path(root)
    if campaign_name is None:
        selector = f"--directory {shlex.quote(str(root))}"
    else:
        from .registry import NAME
        if not isinstance(campaign_name, str) or not NAME.fullmatch(campaign_name):
            raise ValueError("campaign_name is not a valid campaign name")
        selector = f"--campaign {shlex.quote(campaign_name)}"
    owner = None if owner is None else kit_assign.owner_label(owner, "--owner")
    if backups is not None and (type(backup_days) is not int or not 1 <= backup_days <= 90):
        from .backup import BackupError
        raise BackupError("backup_days must be an integer 1-90")
    sources = ((lambda: _meetings(root, as_of, selector), "content/meetings/"),
               (lambda: _requests(root, as_of, selector, law_base, owner), "kit/sent.json"),
               (lambda: _findings(root, as_of, selector), "private/drafts/"),
               (lambda: _correction_requests(root, as_of, selector), CORRECTION_LOG),
               (lambda: _press(root, as_of, selector), "kit/press/"),  # DF1109-03: one source per outreach tool
               (lambda: _social(root, as_of, selector), f"kit/{SOCIAL}/"),
               (lambda: _newsletter(root, as_of, selector), "kit/newsletter-draft.json"),
               (lambda: _contracts(root, as_of, selector, contract_days), "content/agencies.json"),
               (lambda: _review_waiting(root, as_of, selector, review_days), "private/review-assignments/"),
               (lambda: _stale_kits(root, selector), "content/"),
               (lambda: _contacts(root, as_of, selector), "kit/agencies.json"),
               (lambda: _body_schedules(root, as_of), "content/body-schedules.json"))
    items = []
    for read, label in sources:
        try:
            items.extend(read())
        except (ValueError, OSError) as exc:  # one failing source never hides another's items
            items.append(_unreadable(label, exc))
    if backups is not None:
        items.extend(_backups(backups, as_of, selector, backup_days))
    items.sort(key=lambda item: (KINDS.index(item["kind"]), item["due"] or "", item["_key"]))
    return [{"rank": rank, **{k: v for k, v in item.items() if k != "_key"}} for rank, item in enumerate(items, 1)]


def run_cli(root, as_of=None, as_json=False, contract_days=CONTRACT_DAYS, review_days=REVIEW_DAYS, owner=None,
            *, backups=None, backup_days=BACKUP_DAYS):
    day = date.today() if as_of is None else kit_track._day(as_of)
    if day is None:
        raise ValueError("--as-of must be a real YYYY-MM-DD date")
    items = todo(root, day, contract_days=contract_days, review_days=review_days, owner=owner,
                 backups=backups, backup_days=backup_days)
    if as_json:
        return json.dumps(items, indent=2, ensure_ascii=False)
    if not items:
        return f"Nothing due on {day.isoformat()}."
    return "\n".join(f"{item['rank']}. [{item['kind']}] {item['what']} — {item['command']}" for item in items)
