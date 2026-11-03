"""`today`: one ranked starter to-do list with the exact command for each item (read-only).

Pure aggregator over existing read-only helpers. Every source is read separately, so a broken input becomes its own
``unreadable`` item ("fix <file>: ...") and never hides another source's items. Nothing is written, sent or published.
"""
import json
import shlex
from datetime import date, datetime, timedelta
from pathlib import Path

from . import findings, kit, kit_track, meeting_stage
from .content import _read_json, load_meeting

KINDS = ("comment_deadline", "request_past_due", "request_due_today", "request_installment_passed",
         "request_installment_due", "finding_ready", "finding_blocked",
         "stale_comment_kit", "stale_contact", "unreadable")
WINDOW_DAYS, CONTACT_MAX_AGE_DAYS, ERROR_CHARS = 7, 180, 120


def _item(kind, what, command, due=None, key=""):
    return {"kind": kind, "what": what, "due": due, "command": command, "_key": key}


def _unreadable(label, exc):
    return _item("unreadable", f"fix {label}: {str(exc)[:ERROR_CHARS]}", f"open {label} and fix it", key=label)


def _meetings(root, as_of):
    folder, items = root / "content" / "meetings", []
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        label = "content/meetings/" + path.name
        try:
            meeting = load_meeting(_read_json(path), "meetings/" + path.name)
            written = (meeting["participation"] or {}).get("written_comment") or {}
            due = written.get("deadline")
            day = datetime.fromisoformat(due.replace("Z", "+00:00")).date() if due else None
        except (ValueError, OSError) as exc:
            items.append(_unreadable(label, exc))
            continue
        if meeting["status"] != "scheduled" or day is None or not as_of <= day <= as_of + timedelta(WINDOW_DAYS):
            continue
        ident = meeting["id"] or path.stem
        agenda = f" (agenda: {meeting['agenda_url']})" if meeting["agenda_url"] else ""
        items.append(_item("comment_deadline", f"Written comment due {due} for {meeting['body']} ({ident})",
                           f"submit written comment: {written['how']}{agenda}", due, ident))
    return items


def _requests(root, as_of, directory, law_base):
    items = []
    for row in kit_track.due(root, as_of.isoformat(), law_base=law_base)["open"]:
        name = f"{row['name'] or row['agency_id']} ({row['agency_id']})"
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
        else:
            continue
        items.append(_item("request_" + row["state"], what, f"kit-status --directory {directory}", row["due"],
                           row["agency_id"]))
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


def todo(root, as_of, *, law_base=None):
    """Ranked to-do items ``{rank, kind, what, due, command}``; reads only, never raises for a broken input."""
    root = Path(root)
    directory = shlex.quote(str(root))
    sources = ((lambda: _meetings(root, as_of), "content/meetings/"),
               (lambda: _requests(root, as_of, directory, law_base), "kit/sent.json"),
               (lambda: _findings(root, directory), "private/drafts/"),
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


def run_cli(root, as_of=None, as_json=False):
    day = date.today() if as_of is None else kit_track._day(as_of)
    if day is None:
        raise ValueError("--as-of must be a real YYYY-MM-DD date")
    items = todo(root, day)
    if as_json:
        return json.dumps(items, indent=2, ensure_ascii=False)
    if not items:
        return f"Nothing due on {day.isoformat()}."
    return "\n".join(f"{item['rank']}. [{item['kind']}] {item['what']} — {item['command']}" for item in items)
