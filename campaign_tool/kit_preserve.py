"""kit-preserve: a private, never-sent draft asking an agency to keep the records one recorded request covers.

Offline. Reads the recorded sent request, verifies the actual draft bytes against the recorded hash, and writes
``kit/preservation/<agency>--<request>.md`` exclusively (0600, in a private 0700 folder). It asks; it states no legal
duty, computes no deadline or deletion date, and sends nothing. The retention period appears only as the organizer
recorded it in the newest transparency snapshot, else an [EDIT ME] marker.
"""
import hashlib
import os
import re
import stat
import sys
import unicodedata
from datetime import date
from pathlib import Path

from . import content, kit, kit_agencies, kit_track
from .legal_claims import legal_notes

NOTE = "Draft only. Nothing was sent."
MARKER = "[EDIT ME: agency retention period and date checked]"
MAX_DRAFT = 1 << 20
BANNED = (r"required\W+to", r"must\W+preserve", r"obligated", r"spoliation")  # the narrow preservation policy

ASK = ("For this request only, please keep until it is answered any search audit logs, hot-list alert logs, sharing "
       "and access logs, and system settings that the request already covers and that your schedule may otherwise delete.")
LIMITS = ("This asks for no additional record categories or date range. It does not ask you to keep raw plate reads "
          "or images.")
REPLY = "Please let me know whether you can do this."
TEMPLATE = """# Preservation request draft: {name}

DRAFT — NOT SENT. Review before sending it yourself.

To: {name}, records custodian

{first}This concerns the records request {request_line}sent to {name} on {sent_on}.

{ask}

{limits}

{retention}

{reply}

[YOUR NAME]
"""


def validate_preservation_wording(text):
    """Refuse (ValueError) a letter that states a duty; case-insensitive, whitespace-normalized, quoted or negated too."""
    flat = " ".join(str(text).split()).casefold()
    for pattern in BANNED:
        if re.search(r"\b" + pattern + r"\b", flat):
            raise ValueError("preservation wording must ask, not state a legal duty; remove the phrase matching "
                             f"{pattern.replace(chr(92) + 'W+', ' ')!r}")


def _plain(value, label):  # one line of printable text; controls and newlines refused, never silently cleaned
    if not isinstance(value, str) or not value.strip() or any(unicodedata.category(c).startswith("C") for c in value):
        raise ValueError(f"{label} must be one line of plain text with no control characters")
    return value.strip()


def _escape(text):  # dynamic text cannot open a heading, list, link, emphasis or code span
    return re.sub(r"([\\`*_{}\[\]<>()#+\-.!|~&])", r"\\\1", text)


def _folder_ok(path, label):  # a real directory, not a link; absent is fine
    if os.path.islink(path) or (os.path.lexists(path) and not os.path.isdir(path)):
        raise ValueError(f"{label} must be a real folder, not a symlink or file")


def _regular(path, label):
    if os.path.islink(path) or not os.path.isfile(path):
        raise ValueError(f"{label} must be a regular file, not a symlink")


def _sent_entry(root, agency_id, request):
    entry = kit_track.read_sent(root).get(kit_track._key(agency_id, request))
    label = kit_track._label(agency_id, request)
    if entry is None:
        raise ValueError(f"no hand-sent request recorded for {label}; record it with kit-sent first")
    steps = [s for s in entry["steps"] if isinstance(s, dict)]
    if any(s.get("kind") == "closed" for s in steps):
        raise ValueError(f"request for {label} is closed")
    if any(s.get("kind") == "withdrawn" for s in steps):
        raise ValueError(f"request for {label} was withdrawn; no preservation request drafted")
    answer = next((s for s in steps if s.get("kind") in kit_track.ANSWERED), None)
    if answer and not kit_track._producing(steps):
        raise ValueError(f"request for {label} was answered ({answer['kind']} on {answer.get('on')}); "
                         "no preservation request drafted")
    return entry, label


def _verify_draft(root, agency_id, request, entry, label):  # the recorded draft's own bytes, read once
    stem = kit_track._stem(agency_id, request)
    if "imported" in entry or "draft_sha256" not in entry:
        raise ValueError(f"{label} has no recorded draft to check (imported); restore the recorded draft "
                         "and record it as sent before drafting a preservation request")
    path = Path(root) / "kit" / "requests" / f"{stem}.md"
    _regular(path, f"kit/requests/{stem}.md")
    if path.stat().st_size > MAX_DRAFT:
        raise ValueError(f"kit/requests/{stem}.md is larger than 1 MiB")
    data = path.read_bytes()
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError(f"kit/requests/{stem}.md is not UTF-8") from None
    if hashlib.sha256(data).hexdigest() != entry["draft_sha256"]:
        raise ValueError(f"kit/requests/{stem}.md differs from the draft recorded as sent; restore the recorded "
                         "draft (or record the sent version again) before drafting a preservation request")


def _agency(root, agency_id):
    agencies = kit_agencies.load_agencies(root)  # duplicate ids and a non-boolean selected are refused
    entry = next((a for a in agencies if a["agency_id"] == agency_id), None)
    if entry is None or entry.get("selected", True) is not True:
        raise ValueError(f"agency_id {agency_id!r} is not a selected agency in kit/agencies.json")
    return entry


def _retention(root, agency_id, today):  # (days, checked_on, source_url) of the newest snapshot, else None
    path = Path(root) / "content" / "agencies.json"
    if os.path.lexists(path):
        _regular(path, "content/agencies.json")
        loaded = content.load_agencies(content._read_json(path), "content/agencies.json", today)
    else:
        loaded = content.load_agencies(content._read_json(Path(root) / "kit" / "agencies.json"),
                                       "kit/agencies.json", today)
    match = next((a for a in loaded if a["agency_id"] == agency_id), None)
    newest = (match["transparency"] or [None])[0] if match else None
    if newest is None or newest.get("retention_days") is None:
        return None
    return newest["retention_days"], newest["checked_on"], newest["source_url"]


def _retention_text(found):
    if found is None:
        return f"Retention period: {MARKER}"
    days, checked, url = found
    if any(c.isspace() or unicodedata.category(c).startswith("C") for c in url):
        raise ValueError("transparency source URL is not plain text")
    return (f"Organizer-recorded transparency snapshot: retention_days = {days}; checked on {checked}; {url}. "
            "This reported value does not establish the schedule for the requested logs or settings.")


def preserve_request(root, agency_id, *, request="main", today=None):
    """Write the private preservation draft for one sent, still-open request; return its relative path and a note."""
    root, today = Path(root), today or date.today()
    request = kit_track._request(request)
    if not isinstance(agency_id, str) or not kit.AGENCY_ID.fullmatch(agency_id):
        raise ValueError("--agency must look like ca-example-police")
    for part in ("kit", "kit/requests", "content", "kit/preservation"):  # no link or non-folder anywhere on the path
        _folder_ok(root / part, part)
    agency = _agency(root, agency_id)
    entry, label = _sent_entry(root, agency_id, request)
    _verify_draft(root, agency_id, request, entry, label)
    name = _escape(_plain(agency.get("name"), "the agency name"))
    first = ""
    if entry.get("reference") is not None:
        first = kit_track.reference_lines({"reference": _escape(_plain(entry["reference"], "the reference"))})[1] + " "
    text = TEMPLATE.format(name=name, first=first, sent_on=entry["sent_on"], ask=ASK, limits=LIMITS, reply=REPLY,
                           request_line="" if request == "main" else f"named `{request}` ",
                           retention=_retention_text(_retention(root, agency_id, today)))
    validate_preservation_wording(text)
    for note in legal_notes([("kit/preservation draft", text)]):  # alert-only, like every other draft
        print(note, file=sys.stderr)
    folder = root / "kit" / "preservation"
    target = folder / f"{kit_track._stem(agency_id, request)}.md"
    relative = f"kit/preservation/{target.name}"
    if os.path.lexists(folder):
        if stat.S_IMODE(folder.stat().st_mode) & 0o077:
            raise ValueError("kit/preservation/ must be private (no group or other access); fix it yourself")
    else:
        os.mkdir(folder, 0o700)
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        raise ValueError(f"{relative} already exists (or is a link); nothing was overwritten") from None
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
    except BaseException:
        os.unlink(target)  # only the file this call just created
        raise
    return {"written": relative, "agency_id": agency_id, "request": request, "note": NOTE}
