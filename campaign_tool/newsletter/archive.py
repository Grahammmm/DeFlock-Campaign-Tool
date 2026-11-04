"""Local public archive of hand-sent newsletter issues (DF1952-09). Nothing is sent, hosted or deployed.

``publish_issue(root, day, publish=True)`` copies one kept issue's frozen public projection (the
``public_archive`` the draft carried when it was made with ``--from-content``) into
``content/newsletters/<day>.json``::

    {"schema_version": 1, "sent_on": "YYYY-MM-DD", "title": "...", "body_md": "..."}

Only a regular, non-link ``kit/newsletter-issues/<day>.json`` whose sha256 equals the recorded
``draft_sha256`` is used; the current draft, current content and the saved HTML or text are never
consulted. Hash equality proves the bytes are the ones recorded, not that anyone reviewed the text.
``load_public_issues`` is the one validator the site build and ``content-check`` both use.
"""
import contextlib
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

from .draft import _EMAIL, _PHONE
from .sent import ISSUES_DIR, _load, _read_regular, _valid_day, read_kept_copy

FOLDER = "newsletters"
KEYS = {"schema_version", "sent_on", "title", "body_md"}
MAX_TITLE = 200
MAX_BODY_BYTES = 256 * 1024
MAX_FILE_BYTES = 512 * 1024
MAX_PUBLIC_ISSUES = 1000
_HASH_RUN = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{40,}(?![0-9A-Fa-f])")
_LINK_TARGET = re.compile(r"\]\(([^)]*)\)")
_BAD_SCHEME = re.compile(r"(?i)\b(?:javascript|vbscript|mailto|tel|file|ftp|http):|\bdata:[a-z]+/")
_MARKERS = ("{{", "}}", "unsubscribe", "template consent footer")
NO_PROJECTION = ("this draft has no public archive projection; only drafts made with --from-content carry one, so "
                 "draft again with --from-content and record that issue; an older copy is never reconstructed")


def _pairs(items):
    keys = [key for key, _ in items]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate JSON keys")
    return dict(items)


def _json(data, label):
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_pairs)
    except (UnicodeDecodeError, ValueError) as exc:
        reason = "duplicate JSON keys" if "duplicate" in str(exc) else "not valid UTF-8 JSON"
        raise ValueError(f"{label}: {reason}") from None


def _plain(value, field, limit_bytes, single_line):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field}: must be non-blank text")
    if len(value.encode("utf-8")) > limit_bytes:
        raise ValueError(f"{field}: larger than {limit_bytes} bytes")
    allowed = "" if single_line else "\n\t"
    if any((ord(c) < 32 or 127 <= ord(c) < 160) and c not in allowed for c in value):
        raise ValueError(f"{field}: control characters are not allowed")
    if "<" in value or ">" in value:
        raise ValueError(f"{field}: raw HTML is not allowed")
    if _EMAIL.search(value) or _PHONE.search(value):
        raise ValueError(f"{field}: contact details are not allowed")
    if _HASH_RUN.search(value):
        raise ValueError(f"{field}: full hash values are not allowed")
    lowered = value.lower()
    if any(marker in lowered for marker in _MARKERS):
        raise ValueError(f"{field}: unsubscribe or template markers are not allowed")
    if _BAD_SCHEME.search(value) or any(not target.startswith("https://") for target in _LINK_TARGET.findall(value)):
        raise ValueError(f"{field}: only https links are allowed")


def validate_public_issue(document, day=None):
    """The checked public issue ``{"schema_version", "sent_on", "title", "body_md"}``; ``ValueError`` names the field.

    ``day`` (the file name's day) must equal ``sent_on`` when given. Nothing in the document is echoed."""
    if not isinstance(document, dict):
        raise ValueError("expected an object")
    unknown, missing = sorted(set(document) - KEYS), sorted(KEYS - set(document))
    if unknown or missing:
        raise ValueError(f"unknown field(s) {unknown} or missing field(s) {missing}")
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise ValueError("schema_version: must be 1")
    sent_on = document["sent_on"]
    if _valid_day(sent_on) is None:
        raise ValueError("sent_on: must be a real YYYY-MM-DD day")
    if day is not None and sent_on != day:
        raise ValueError("sent_on: must equal the day in the file name")
    _plain(document["title"], "title", MAX_TITLE * 4, True)
    if len(document["title"]) > MAX_TITLE:
        raise ValueError(f"title: longer than {MAX_TITLE} characters")
    _plain(document["body_md"], "body_md", MAX_BODY_BYTES, False)
    return {key: document[key] for key in ("schema_version", "sent_on", "title", "body_md")}


def issue_paths(content_dir):
    """Sorted ``content/newsletters/*.json`` paths; ``[]`` without the folder. Refuses a linked folder or a bad name."""
    folder = Path(content_dir) / FOLDER
    if folder.is_symlink():
        raise ValueError("the folder must not be a symlink")
    if not folder.is_dir():
        return []
    paths = sorted(path for path in folder.iterdir() if path.name.endswith(".json"))
    if len(paths) > MAX_PUBLIC_ISSUES:
        raise ValueError(f"at most {MAX_PUBLIC_ISSUES} issue files")
    return paths


def load_public_issue(path):
    """One validated issue from ``content/newsletters/<day>.json``; ``ValueError`` without the file name."""
    path = Path(path)
    day = path.name[:-len(".json")]
    if _valid_day(day) is None:
        raise ValueError("the file name must be a real YYYY-MM-DD day")
    try:
        data = _read_regular(path, MAX_FILE_BYTES, "the file")
    except OSError:
        raise ValueError("the file is not readable") from None
    if data is None:
        raise ValueError("the file is not readable")
    return validate_public_issue(_json(data, "the file"), day)


def load_public_issues(content_dir):
    """All public issues, oldest first; the first refusal is ``ValueError("newsletters/<name>: reason")``."""
    issues = []
    try:
        paths = issue_paths(content_dir)
    except ValueError as exc:
        raise ValueError(f"{FOLDER}: {exc}") from None
    for path in paths:
        try:
            issues.append(load_public_issue(path))
        except ValueError as exc:
            raise ValueError(f"{FOLDER}/{path.name}: {exc}") from None
    return sorted(issues, key=lambda issue: issue["sent_on"])


def _kept_projection(root, day):
    """The recorded issue's ``(record, public_archive)`` after the one-record, kept-copy and hash checks."""
    records = [issue for issue in _load(root) if issue["sent_on"] == day]
    if not records:
        raise ValueError(f"no sent record for {day}; record it with: newsletter sent --date {day}")
    if len(records) > 1:
        raise ValueError(f"{day} is recorded more than once in kit/newsletter-sent.json; it cannot be archived "
                         "unambiguously")
    data = read_kept_copy(root, day)
    if data is None:
        raise ValueError(f"no kept copy kit/{ISSUES_DIR}/{day}.json; issues recorded before copies were kept cannot "
                         "be archived")
    if hashlib.sha256(data).hexdigest() != records[0]["draft_sha256"]:
        raise ValueError(f"kit/{ISSUES_DIR}/{day}.json does not match the recorded draft_sha256; refusing")
    draft = _json(data, f"kit/{ISSUES_DIR}/{day}.json")
    projection = draft.get("public_archive") if isinstance(draft, dict) else None
    if projection is None:
        raise ValueError(NO_PROJECTION)
    if not isinstance(projection, dict) or set(projection) != {"schema_version", "title", "body_md"}:
        raise ValueError("the kept draft's public_archive is malformed")
    return {"schema_version": projection["schema_version"], "sent_on": day,
            "title": projection["title"], "body_md": projection["body_md"]}


def publish_issue(root, day, *, publish=False):
    """Check that ``day``'s kept issue can be archived; with ``publish`` also write ``content/newsletters/<day>.json``.

    Returns ``{"date", "title", "target", "published"}``. Refuses (``ValueError``) without a single sent record, a
    kept copy of the recorded hash and a valid projection, and never replaces an existing target. Without
    ``publish`` nothing is written."""
    if _valid_day(day) is None:
        raise ValueError(f"date {day!r} is not a real YYYY-MM-DD day")
    root = Path(root)
    issue = validate_public_issue(_kept_projection(root, day), day)
    content = root / "content"
    if content.is_symlink() or not content.is_dir():
        raise ValueError("content/ must be an existing folder, not a link")
    folder = content / FOLDER
    if folder.is_symlink() or (folder.exists() and not folder.is_dir()):
        raise ValueError(f"content/{FOLDER} must be a real folder")
    target = folder / (day + ".json")
    result = {"date": day, "title": issue["title"], "target": f"content/{FOLDER}/{day}.json", "published": False}
    try:
        os.lstat(target)
    except FileNotFoundError:
        pass
    else:
        raise ValueError(f"content/{FOLDER}/{day}.json already exists; it is never replaced")
    if len(issue_paths(content)) >= MAX_PUBLIC_ISSUES:
        raise ValueError(f"content/{FOLDER} already holds {MAX_PUBLIC_ISSUES} issues")
    encoded = (json.dumps(issue, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    if len(encoded) > MAX_FILE_BYTES:
        raise ValueError(f"the public issue would exceed {MAX_FILE_BYTES} bytes")
    if not publish:
        return result
    folder.mkdir(exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=folder, prefix=".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), 0o644)
        os.link(temp, target)  # exclusive: fails if the target appeared meanwhile
    except FileExistsError:
        raise ValueError(f"content/{FOLDER}/{day}.json already exists; it is never replaced") from None
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp)
    result["published"] = True
    return result
