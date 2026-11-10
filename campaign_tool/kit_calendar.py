"""Private iCalendar (RFC 5545) of open hand-sent requests' dates and staged meetings' written-comment deadlines.

Request dates: the provisional determination date, the agency's stated production date, each still-pending download
link expiry, the next stated installment of a rolling production and an unanswered stated extension date.
The file is for the organizer's own calendar: 0600, never overwritten, refused inside the campaign's public/ or
content/. Events carry ids, dates, agency or body names and the official source link only; never recipient emails,
request text, download links, record hashes, notes or reviewer names.
"""
import os
from datetime import date, datetime, timezone
from pathlib import Path

from . import kit_track

PRODID = "-//DeFlock Campaign Tool//kit calendar//EN"
DOMAIN = "campaign.invalid"
NO_LAW = ("No law package: provisional determination dates cannot be computed; "
          "only agency-stated dates are included.")
STATED = "agency-stated; provisional, not a legal deadline"


def escape_text(value):
    """RFC 5545 TEXT escaping: backslash, semicolon, comma and line breaks (as \\n)."""
    value = str(value).replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
    return value.replace("\r\n", "\\n").replace("\r", "\\n").replace("\n", "\\n")


def fold(line):
    """Fold one content line at 75 octets with CRLF + one space, never splitting a UTF-8 sequence."""
    parts, current, size, limit = [], [], 0, 75
    for char in line:
        width = len(char.encode("utf-8"))
        if size + width > limit:
            parts.append("".join(current))
            current, size, limit = [], 0, 74  # the leading space of a continuation line is one of its 75 octets
        current.append(char)
        size += width
    parts.append("".join(current))
    return "\r\n ".join(parts)


def _utc(moment):
    return moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _label(row):
    name = row["name"] or row["agency_id"]
    return name if row["request"] == "main" else f"{name} ({row['request']})"


def _uid(kind, row):
    # "#" is in neither the agency-id nor the request-name grammar, so it marks the boundary; main keeps its bare id
    suffix = "" if row["request"] == "main" else "#" + row["request"]
    return f"{kind}-{row['agency_id']}{suffix}@{DOMAIN}"


def _request_events(root, law_base):
    report = kit_track.status(root, law_base=law_base)
    stored = {kit_track._split(key): [(i, s) for i, s in enumerate(entry["steps"]) if isinstance(s, dict)]
              for key, entry in kit_track.read_sent(root).items()}
    steps = {key: [s for _, s in pairs] for key, pairs in stored.items()}
    events = []
    for row in report["requests"]:
        if row["closed"] or "withdrawn_on" in row:
            continue
        counted = f" ({row['note']})" if "received_on" in row else ""  # provenance; the dates are unchanged
        answered = {s.get("kind") for s in steps.get((row["agency_id"], row["request"]), [])} & kit_track.ANSWERED
        if row["determination_due"] and not answered:
            events.append({"uid": _uid("determination", row), "day": row["determination_due"],
                           "summary": "Records request: provisional determination date — " + _label(row) + counted})
        if row["stated_production_date"]:
            events.append({"uid": _uid("stated-production", row), "day": row["stated_production_date"],
                           "summary": "Records request: agency's stated production date — " + _label(row) + counted})
        for index, step in _pending_downloads(stored.get((row["agency_id"], row["request"]), [])):
            events.append(_stated(_uid("download-expiry", row).replace("@", f"-step{index}@", 1),
                                  step["download_expires_on"], "download link expiry", row))
        if row["producing"] and row["next_installment"]:
            events.append(_stated(_uid("next-installment", row), row["next_installment"], "next installment date", row))
        if row.get("extension_expected_on") and not answered:
            events.append(_stated(_uid("stated-extension", row), row["extension_expected_on"], "stated extension date",
                                  row))
    return events, report["law_package_status"]


def _stated(uid, day, what, row):  # an agency-stated reminder: the date and label only, never a link, note or hash
    return {"uid": uid, "day": day, "summary": f"Records request: {what} — {_label(row)} ({STATED})"}


def _pending_downloads(steps):
    """[(stored step index, step)] still pending, as kit_track._downloads: a later produced step that links records
    clears the latest earlier pending one. The index is the step's place in the stored history, so it never moves."""
    pending = []
    for index, step in steps:
        if step.get("kind") != "produced":
            continue
        if kit_track._step_records(step) and pending:
            pending.pop()
        if kit_track._download_pending(step):
            pending.append((index, step))
    return pending


def _meeting_events(root, as_of):
    from .content import load_content
    model = load_content(root)
    events = []
    for meeting in model["meetings"] if model else []:
        part = meeting["participation"]
        written = part and part["written_comment"]
        if meeting["status"] == "cancelled" or not written or not written["deadline"]:
            continue
        deadline = datetime.fromisoformat(written["deadline"].replace("Z", "+00:00"))
        if deadline.date() < as_of:
            continue
        events.append({"uid": f"meeting-comment-{meeting['id']}@{DOMAIN}", "moment": deadline,
                       "day": deadline.astimezone(timezone.utc).date().isoformat(),
                       "summary": "Written comment deadline — " + meeting["body"], "url": part["source_url"]})
    return events


def render(events, now):
    """The VCALENDAR text (CRLF line ends, folded) for ``events`` sorted by date then UID."""
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:" + PRODID, "CALSCALE:GREGORIAN"]
    for event in sorted(events, key=lambda e: (e["day"], e["uid"])):
        lines += ["BEGIN:VEVENT", "UID:" + event["uid"], "DTSTAMP:" + _utc(now)]
        if "moment" in event:
            lines += ["DTSTART:" + _utc(event["moment"]), "DURATION:PT0M"]
        else:
            lines.append("DTSTART;VALUE=DATE:" + event["day"].replace("-", ""))
        lines.append("SUMMARY:" + escape_text(event["summary"]))
        if event.get("url"):
            lines.append("URL:" + event["url"])
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "".join(fold(line) + "\r\n" for line in lines)


def _inside(path, folder):
    return os.path.commonpath([path, folder]) == folder


def _target(root, out):
    out = Path(os.path.abspath(out))
    if out.suffix != ".ics":
        raise ValueError(f"{out}: the calendar file name must end in .ics")
    parent = os.path.realpath(out.parent)
    campaign = os.path.realpath(root)
    for name in ("public", "content"):
        if _inside(parent, os.path.join(campaign, name)):
            raise ValueError(f"{out} is inside the campaign's {name}/ folder; write the private calendar elsewhere")
    if os.path.islink(out.parent) or not os.path.isdir(out.parent):
        raise ValueError(f"{out.parent}: the parent must be an existing directory, not a symbolic link")
    if os.path.lexists(out):
        raise ValueError(f"{out} exists; nothing was overwritten")
    return out


def calendar(root, out, as_of=None, *, now=None, law_base=None, today=None):
    """Write the private calendar to ``out`` (new file, 0600); return ``{"written", "events"}``."""
    from .cli import read_config  # local import: cli imports this module lazily
    root = Path(root)
    read_config(root)
    day = kit_track._day(as_of) if as_of is not None else (today or date.today())
    if day is None:
        raise ValueError("--as-of must be a real YYYY-MM-DD date")
    target = _target(root, out)
    requests, package_status = _request_events(root, law_base)
    events = requests + _meeting_events(root, day)
    data = render(events, now or datetime.now(timezone.utc)).encode("utf-8")
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(data)
    report = {"written": str(target), "events": len(events)}
    return {**report, "note": NO_LAW} if package_status == "missing" else report
