"""Record a newsletter issue the organizer sent by hand (DF1822-05). Nothing is sent here.

``kit/newsletter-sent.json`` lists each recorded issue in date order::

    {"schema_version": 1,
     "issues": [{"sent_on": "YYYY-MM-DD", "draft_sha256": "<hex>", "subject": "...", "recorded_at": "<ISO>"}]}

``draft_sha256`` is the sha256 of ``kit/newsletter-draft.json``, which holds the
subject, HTML and text of the draft that went out. ``last_sent`` gives the next
``--from-content`` run its window when ``--since`` is not passed.
"""
import contextlib
import hashlib
import json
import os
import re
import stat
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

SENT_NAME = "newsletter-sent.json"
DRAFT_NAME = "newsletter-draft.json"
MAX_DRAFT_BYTES = 4 * 1024 * 1024
MAX_SENT_BYTES = 1024 * 1024
MAX_ISSUES = 1000
EARLIEST = date(2000, 1, 1)
_SHA256 = re.compile(r"[0-9a-f]{64}")


def _read_regular(path, limit, label):
    """Bytes of a regular, non-link file of at most ``limit`` bytes; ``None`` when it does not exist."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode):
        raise ValueError(f"{label} must not be a symlink")
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"{label} is not a regular file")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError(f"{label} is not a regular file")
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"{label} is larger than {limit} bytes")
    return data


def _valid_day(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.isoformat() == value else None


def _load(root):
    """Recorded issues from kit/newsletter-sent.json; ``[]`` when there is none yet."""
    kit = Path(root) / "kit"
    try:
        if os.path.islink(kit):
            raise ValueError("kit/ must not be a symlink")
        data = _read_regular(kit / SENT_NAME, MAX_SENT_BYTES, "the file")
        if data is None:
            return []
        document = json.loads(data.decode("utf-8"))
        if not isinstance(document, dict) or document.get("schema_version") != 1:
            raise ValueError("schema_version must be 1")
        issues = document.get("issues")
        if not isinstance(issues, list) or len(issues) > MAX_ISSUES:
            raise ValueError(f"issues must be a list of at most {MAX_ISSUES}")
        previous = None
        for index, issue in enumerate(issues):
            day = _valid_day(issue.get("sent_on")) if isinstance(issue, dict) else None
            if (day is None or not isinstance(issue.get("draft_sha256"), str)
                    or not _SHA256.fullmatch(issue["draft_sha256"]) or not isinstance(issue.get("subject"), str)
                    or not isinstance(issue.get("recorded_at"), str)):
                raise ValueError(f"issues[{index}] is malformed")
            if previous and day < previous:
                raise ValueError(f"issues[{index}] is out of date order")
            previous = day
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"kit/{SENT_NAME} unreadable: {exc}") from None
    return issues


def last_sent(root):
    """Latest recorded ``sent_on`` (``YYYY-MM-DD``) or ``None`` when nothing is recorded."""
    issues = _load(root)
    return issues[-1]["sent_on"] if issues else None


def record_sent(root, sent_on, today=None):
    """Append ``{"sent_on", "draft_sha256", "subject", "recorded_at"}`` for the current draft; returns it."""
    today = today or date.today()
    day = _valid_day(sent_on)
    if day is None:
        raise ValueError(f"sent date {sent_on!r} is not a real YYYY-MM-DD day")
    if day > today:
        raise ValueError(f"sent date {sent_on} is after today ({today.isoformat()})")
    if day < EARLIEST:
        raise ValueError(f"sent date {sent_on} is before {EARLIEST.isoformat()}")
    kit = Path(root) / "kit"
    issues = _load(root)  # refuses a symlinked kit/ before anything under it is read
    data = _read_regular(kit / DRAFT_NAME, MAX_DRAFT_BYTES, f"kit/{DRAFT_NAME}")
    if data is None:
        raise ValueError(f"no draft to record: kit/{DRAFT_NAME} is missing")
    try:
        subject = json.loads(data.decode("utf-8")).get("subject")
    except (UnicodeDecodeError, ValueError, AttributeError):
        subject = None
    if not isinstance(subject, str):
        raise ValueError(f"kit/{DRAFT_NAME} is not a newsletter draft (no subject)")
    digest = hashlib.sha256(data).hexdigest()
    for issue in issues:
        if issue["draft_sha256"] == digest:
            raise ValueError(f"this draft was already recorded as sent on {issue['sent_on']}")
    if issues and sent_on < issues[-1]["sent_on"]:
        raise ValueError(f"sent date {sent_on} is before the last recorded send ({issues[-1]['sent_on']})")
    if len(issues) >= MAX_ISSUES:
        raise ValueError(f"kit/{SENT_NAME} already holds {MAX_ISSUES} issues")
    record = {"sent_on": sent_on, "draft_sha256": digest, "subject": subject,
              "recorded_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat()}
    text = json.dumps({"schema_version": 1, "issues": issues + [record]}, indent=2, ensure_ascii=False) + "\n"
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_SENT_BYTES:  # _load would refuse the result; keep the existing archive
        raise ValueError(f"kit/{SENT_NAME} would exceed {MAX_SENT_BYTES} bytes; nothing recorded")
    fd, temp = tempfile.mkstemp(dir=kit, prefix="." + SENT_NAME + ".", suffix=".tmp")  # mode 0600
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
        os.replace(temp, kit / SENT_NAME)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp)
        raise
    return record
