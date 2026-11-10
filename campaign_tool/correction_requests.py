"""Private log of correction requests: private/correction-requests.json (0600). Records what was asked about which
published finding and the decision, never who asked; nothing is sent or published. The site build does not read it,
with one explicit off-by-default exception: site.json corrections.show_request_counts true makes the build call
``quarterly_counts``, which publishes per-quarter counts only (never text, ids, dates or anything about who asked)."""
import json
import os
import re
import stat
from datetime import date, datetime, timezone
from pathlib import Path

from .content import ContentError, load_content
from .findings import _locked, _read_json, _write_private
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


def _validate_document(document, today=None):
    """Pure whole-document check shared by the log commands and the counts. ``today`` (counts only) adds the strict
    checks: integer schema_version, no date after today, decided_on not before received_on."""
    if not isinstance(document, dict):
        raise _unreadable("top level is not an object")
    if unknown := [key for key in document if key not in ("schema_version", "requests")]:
        raise _unreadable(f"unknown key {unknown[0]!r}")
    if document.get("schema_version") != 1 or not isinstance(document.get("requests"), list):
        raise _unreadable("needs schema_version 1 and a requests list")
    if today is not None and type(document["schema_version"]) is not int:
        raise _unreadable("needs schema_version 1 and a requests list")
    ids = [_check_request(index, item) for index, item in enumerate(document["requests"])]
    if ids != sorted(set(ids)) or (ids and ids[0] < 1):
        raise _unreadable("ids must be unique, ascending and start at 1 or above")
    if today is not None:
        for index, item in enumerate(document["requests"]):
            received = _date(item["received_on"])
            decided = _date(item["decided_on"]) if "decided_on" in item else received
            if received > today or decided > today or decided < received:
                raise _unreadable(f"requests[{index}] has a future date or a decision before its receipt")
    return document


class _DuplicateKey(ValueError):
    """A JSON object repeated a key; fixed text only, never the key or a value."""


def _no_repeats(pairs):
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):  # decoded strings: a literal and an escaped spelling collide
        raise _DuplicateKey("duplicate object key")
    return dict(pairs)


def _no_constant(name):
    raise ValueError("non-finite number")


def _strict_loads(text, finite=False):
    """json.loads that refuses a repeated key at any depth; ``finite`` also refuses NaN and Infinity."""
    return json.loads(text, object_pairs_hook=_no_repeats, parse_constant=_no_constant if finite else None)


def _load(path):
    if not path.exists():
        return None
    try:
        document = _strict_loads(path.read_text(encoding="utf-8"))
    except _DuplicateKey:
        raise _unreadable("duplicate object key") from None
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _unreadable(f"not valid JSON ({exc.__class__.__name__})") from None
    except OSError as exc:
        raise _unreadable(f"cannot read it ({exc.__class__.__name__})") from None
    return _validate_document(document)


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
    with _locked(path.parent, ".correction-requests.lock"):
        path = _path(root)
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
    with _locked(path.parent, ".correction-requests.lock"):
        path = _path(root)
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


COUNTS_REFUSAL = f"private/{NAME}: cannot publish correction request counts; check the private log"


def _exists(path):
    """False only when the path is genuinely absent (ENOENT); permission and other I/O errors propagate."""
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    return True


def _counts_path(root):
    """The log path, or None when the private directory or the log is absent; links and non-directories are refused."""
    private = Path(root) / "private"
    if not _exists(private):
        return None
    if os.path.islink(private) or not private.is_dir():
        raise ValueError("private")
    path = private / NAME
    if not _exists(path):
        return None
    if os.path.islink(path):
        raise ValueError("log")
    return path


def quarterly_counts(root, today=None):
    """DF1103-14: ``[{quarter, received, accepted, declined, still_open}]``, newest quarter first, grouped by the quarter the
    request was received; accepted, declined and still_open split that cohort by the CURRENT stored state. Nothing else
    leaves this function. Missing storage is zero rows and is never created; any other failure is one fixed ContentError
    that carries no private value, filename or cause."""
    today = _today(today)
    try:
        path = _counts_path(root)
        if path is None:
            return []
        text = _read_json(path, 4 * 1024 * 1024, "correction request log", raw=True)
        document = _validate_document(_strict_loads(text, finite=True), today)
        cohorts = {}
        for item in document["requests"]:
            day = date.fromisoformat(item["received_on"])
            row = cohorts.setdefault(f"{day.year}-Q{(day.month - 1) // 3 + 1}",
                                     {"received": 0, "accepted": 0, "declined": 0, "open": 0})
            row["received"] += 1
            row[item["state"]] += 1
    except Exception:  # every failure, including a bug, must not echo private text
        raise ContentError(COUNTS_REFUSAL) from None
    return [{"quarter": quarter, "received": row["received"], "accepted": row["accepted"],
             "declined": row["declined"], "still_open": row["open"]}
            for quarter, row in sorted(cohorts.items(), reverse=True)]


# DF1952-13: private, never-sent reply drafts from the recorded disposition.
REPLY_DIR = ("kit", "corrections", "replies")
REPLY_BANNER = "DRAFT — NOT SENT. Review and edit before sending it yourself."
_LOG_REFUSAL = f"private/{NAME} is missing, unreadable or invalid; no draft was written"
_CONTENT_REFUSAL = "public content is unreadable; no draft was written (run content-check)"


def _reply_request(root, request_id):
    if type(request_id) is not int or request_id < 1:
        raise ValueError("--id must be a positive whole number")
    private = Path(root) / "private"
    try:
        path = private / NAME
        if os.path.islink(private) or not private.is_dir() or not _exists(path):
            raise ValueError
        text = _read_json(path, 4 * 1024 * 1024, "correction request log", raw=True)
        document = _validate_document(_strict_loads(text, finite=True))
    except Exception:  # fixed text only: no path, key or private value from the log
        raise ValueError(_LOG_REFUSAL) from None
    for item in document["requests"]:
        if item["id"] == request_id:
            return item
    raise ValueError(f"no correction request {request_id}")


def _reply_finding(root, finding_id):
    """(finding or None, base_url or None). Missing content is absence; present but invalid content refuses."""
    try:
        try:
            mode = os.lstat(Path(root) / "content").st_mode
        except FileNotFoundError:
            return None, None  # genuinely absent
        if not stat.S_ISDIR(mode):  # a file, link or dangling link is present but invalid
            raise ValueError
        model = load_content(root)
    except Exception:
        raise ValueError(_CONTENT_REFUSAL) from None
    if not model:
        return None, None
    found = next((f for f in model["findings"] if f["id"] == finding_id), None)
    return found, model["site"].get("base_url")


def _quote(note):
    return "\n".join("> " + line for line in (note.splitlines() or [""]))


def _render_reply(request, finding, base):
    state, subject = request["state"], request["finding_id"]
    url = (base.rstrip("/") + "/findings/" + finding["slug"] + ".html") if finding and base else None
    lines = [f"# Draft reply to correction request {request['id']}", "", REPLY_BANNER, "",
             "[EDIT ME: add greeting outside this repository]", "", f"Subject: finding {subject}", ""]
    if state == "open":
        lines += [f"We received your correction request about this finding on {request['received_on']}. "
                  "It is being reviewed, and we will write again when a decision has been recorded."]
    elif state == "accepted":
        matches = [c["date"] for c in finding["corrections"]
                   if request["received_on"] <= c["date"] <= request["decided_on"]] if finding else []
        lines += ["We accepted your correction request about this finding.", "",
                  f"Received on: {request['received_on']}", f"Decision date: {request['decided_on']}",
                  "Correction date: " + (matches[0] if len(matches) == 1 else "[EDIT ME: confirm the correction date]"),
                  "", "Recorded decision note:", _quote(request["note"]), "",
                  "[EDIT ME: confirm the correction addressed this request]"]
    else:
        lines += ["We reviewed your correction request about this finding and did not accept it.", "",
                  f"Received on: {request['received_on']}", f"Decision date: {request['decided_on']}", "",
                  "Reason recorded:", _quote(request["note"])]
    lines += ["", "Finding: " + (url or "[EDIT ME: add the finding URL]"), "", "[EDIT ME: sign-off]", ""]
    return "\n".join(lines)


def _reply_target(root, request_id):
    """Preflight every component without creating anything; returns the target path."""
    current, missing = Path(root), 0
    for part in REPLY_DIR + (f"{request_id}.md",):
        current = current / part
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            missing += 1
            continue
        except OSError:
            raise ValueError(f"kit/corrections/replies/{request_id}.md: path could not be checked safely; "
                             "nothing was written") from None
        if missing or stat.S_ISLNK(mode) or (part != f"{request_id}.md" and not stat.S_ISDIR(mode)):
            raise ValueError(f"kit/corrections/replies/{request_id}.md: path has a link or non-directory, or the draft already exists; "
                             "nothing was written")
        if part == f"{request_id}.md":
            raise ValueError(f"kit/corrections/replies/{request_id}.md already exists; edit it by hand "
                             "(an existing draft is never replaced)")
    return current


def reply(root, request_id):
    """Write kit/corrections/replies/<id>.md from the recorded disposition; never overwrites, never sends."""
    request = _reply_request(root, request_id)
    finding, base = _reply_finding(root, request["finding_id"])
    text = _render_reply(request, finding, base)
    target = _reply_target(root, request_id)
    rel = f"kit/corrections/replies/{request_id}.md"
    root = Path(root)
    made = []
    try:
        current = root
        for part in REPLY_DIR:
            current = current / part
            if not _exists(current):
                os.mkdir(current, 0o700)
                os.chmod(current, 0o700)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        made.append(target)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as out:
            os.fchmod(out.fileno(), 0o600)
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        made.clear()
    except FileExistsError:
        raise ValueError(f"{rel} already exists; edit it by hand (an existing draft is never replaced)") from None
    except OSError:
        raise ValueError(f"{rel} could not be written safely; nothing was sent") from None
    finally:
        for partial in made:
            try:
                os.unlink(partial)
            except OSError:
                pass
    return {"written": rel, "id": request_id, "state": request["state"]}
