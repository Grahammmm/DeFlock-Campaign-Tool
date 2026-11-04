"""Private log of correction requests: private/correction-requests.json (0600). Records what was asked about which
published finding and the decision, never who asked; nothing is sent, published or read by the site build."""
import json
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path

from .content import load_content
from .findings import _write_private
from .privacy_patterns import EMAIL  # the shared pattern: everything the old local copy matched, plus the spelled-out forms

NAME = "correction-requests.json"
STATES = ("open", "accepted", "declined")
REQUIRED = ("id", "finding_id", "received_on", "summary", "state")
DECIDED = ("decided_on", "note")
DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _today(today):
    return today or datetime.now(timezone.utc).date()


def _date(value):
    try:
        return date.fromisoformat(value) if isinstance(value, str) and DAY.match(value) else None
    except ValueError:
        return None


def _path(root):
    private = Path(root) / "private"
    if os.path.islink(private) or not private.is_dir():
        raise ValueError("private/ must be an existing directory, not a symlink")
    path = private / NAME
    if os.path.islink(path) or (os.path.lexists(path) and not path.is_file()):
        raise ValueError(f"private/{NAME} must be a regular file, not a symlink")
    return path


def _unreadable(why):
    return ValueError(f"private/{NAME} unreadable: {why}; nothing was changed")


def _check_request(index, item):
    where = f"requests[{index}]"
    if not isinstance(item, dict):
        raise _unreadable(f"{where} is not an object")
    if unknown := [key for key in item if key not in REQUIRED + DECIDED]:
        raise _unreadable(f"unknown key {unknown[0]!r} in {where}")
    decided = item.get("state") in STATES[1:]
    if missing := [key for key in REQUIRED + DECIDED * decided if key not in item]:
        raise _unreadable(f"missing key {missing[0]!r} in {where}")
    if not decided and any(key in item for key in DECIDED):
        raise _unreadable(f"open {where} has decided_on or note")
    if type(item["id"]) is not int or item["state"] not in STATES:
        raise _unreadable(f"bad id or state in {where}")
    if bad := [key for key in ("finding_id", "summary") + DECIDED * decided if not isinstance(item[key], str)]:
        raise _unreadable(f"{bad[0]!r} in {where} is not text")
    if bad := [key for key in ("received_on",) + ("decided_on",) * decided if _date(item[key]) is None]:
        raise _unreadable(f"{bad[0]!r} in {where} is not a YYYY-MM-DD date")
    return item["id"]


def _load(path):
    if not path.exists():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _unreadable(f"not valid JSON ({exc.__class__.__name__})") from None
    except OSError as exc:
        raise _unreadable(f"cannot read it ({exc.__class__.__name__})") from None
    if not isinstance(document, dict):
        raise _unreadable("top level is not an object")
    if unknown := [key for key in document if key not in ("schema_version", "requests")]:
        raise _unreadable(f"unknown key {unknown[0]!r}")
    if document.get("schema_version") != 1 or not isinstance(document.get("requests"), list):
        raise _unreadable("needs schema_version 1 and a requests list")
    ids = [_check_request(index, item) for index, item in enumerate(document["requests"])]
    if ids != sorted(set(ids)) or (ids and ids[0] < 1):
        raise _unreadable("ids must be unique, ascending and start at 1 or above")
    return document


def _day(value, flag, today):
    if (day := _date(value)) is None:
        raise ValueError(f"{flag} {value}: not a real calendar date (YYYY-MM-DD)")
    if day > today:
        raise ValueError(f"{flag} {value} is in the future")
    return day


def _text(value, flag):
    text = " ".join(str(value).split())
    if not 1 <= len(text) <= 500:
        raise ValueError(f"{flag} must be 1 to 500 characters on one line")
    if EMAIL.search(text):
        raise ValueError(f"{flag} contains an email address: requester contact stays out of this log")
    return text


def _published(root, finding_id):
    model = load_content(root)
    for finding in model["findings"] if model else []:
        if finding["id"] == finding_id:
            return finding
    raise ValueError(f"no published finding {finding_id}")


def add(root, finding_id, received, summary, today=None):
    """Log one request against a published finding; returns the new open request."""
    path, today = _path(root), _today(today)
    document = _load(path) or {"schema_version": 1, "requests": []}
    finding = _published(root, finding_id)
    day = _day(received, "--received", today)
    published = datetime.fromisoformat(finding["published_at"].replace("Z", "+00:00")).date()
    if day < published:
        raise ValueError(f"--received {received} is before the finding's publication date {published}")
    request = {"id": max([r["id"] for r in document["requests"]], default=0) + 1, "finding_id": finding_id,
               "received_on": day.isoformat(), "summary": _text(summary, "--summary"), "state": "open"}
    document["requests"].append(request)
    _write_private(path, document, ".correction-requests-")
    return request


def decide(root, request_id, decision, on, note, today=None):
    """Record accepted or declined for an open request; decided requests are final. ``accepted`` needs a
    correction on the finding dated on or after the request (record it first with ``finding correct``)."""
    if decision not in STATES[1:]:
        raise ValueError("decision must be accepted or declined")
    path, today = _path(root), _today(today)
    document = _load(path) or {"requests": []}
    request = next((r for r in document["requests"] if r["id"] == request_id), None)
    if request is None:
        raise ValueError(f"no correction request {request_id}")
    if request["state"] != "open":
        raise ValueError(f"correction request {request_id} is already {request['state']}; decisions are final")
    day, note = _day(on, "--on", today), _text(note, "--note")
    if day.isoformat() < request["received_on"]:
        raise ValueError(f"--on {on} is before the request was received ({request['received_on']})")
    if decision == "accepted":
        finding = _published(root, request["finding_id"])
        if not any(c["date"] >= request["received_on"] for c in finding["corrections"]):
            raise ValueError("accept only after recording the correction: finding correct "
                             f"{request['finding_id']} --date D --note T (dated {request['received_on']} or later)")
    request.update(state=decision, decided_on=day.isoformat(), note=note)
    _write_private(path, document, ".correction-requests-")
    return request


def list_requests(root, today=None):
    """Open requests first (with days_open), then the rest, each group by id."""
    document, today = _load(_path(root)) or {"requests": []}, _today(today)
    rows = sorted(document["requests"], key=lambda r: (r["state"] != "open", r["id"]))
    return [dict(r, days_open=(today - date.fromisoformat(r["received_on"])).days) if r["state"] == "open"
            else r for r in rows]


def open_overdue(root, today, days=14):
    """Ids of requests open more than ``days`` days, or None when there is no log."""
    if not os.path.lexists(Path(root) / "private" / NAME):
        return None
    return [r["id"] for r in list_requests(root, today) if r["state"] == "open" and r["days_open"] > days]
