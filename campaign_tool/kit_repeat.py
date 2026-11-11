"""kit-repeat (DF1952-11): draft a follow-on request that starts the day after an earlier sent request's coverage ended.

The follow-on is a private, unsent named draft kit/requests/<agency>--<NEW>.md with the earlier draft's scope sections
and an explicit date range; kit/repeat-drafts.json (0600) binds it to its predecessor until kit-sent records it.
Nothing is sent or published from this module.
"""
import errno
import hashlib
import json
import os
import re
import stat
import tempfile
from datetime import date, timedelta
from pathlib import Path

from . import kit, kit_track

FILE = "kit/repeat-drafts.json"
ENTRY_KEYS = {"follows", "date_from", "date_to", "source_sha256", "draft_sha256"}
SUBJECT_RANGE = re.compile(r", (?:\d{4}-\d\d-\d\d through (?:\d{4}-\d\d-\d\d|today))\Z", re.ASCII)
CONTEXT = "## Statutory context (for the requester, not for sending)"


def _pairs(pairs):
    keys = [k for k, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate key")
    return dict(pairs)


def _kit_dir(root):
    kit_dir = Path(root) / "kit"
    if kit_dir.is_symlink():
        raise ValueError("kit/ must not be a symlink")
    return kit_dir


def _entry_ok(key, entry):
    parts = kit_track._split(key) if isinstance(key, str) else None
    if not (parts and parts[1] != "main" and kit.AGENCY_ID.match(parts[0]) and key == kit_track._key(*parts)
            and isinstance(entry, dict) and set(entry) == ENTRY_KEYS):
        return False
    start, end = kit_track._day(entry["date_from"]), kit_track._day(entry["date_to"])
    return (isinstance(entry["follows"], str) and bool(kit_track.REQUEST.fullmatch(entry["follows"]))
            and entry["follows"] != parts[1] and start is not None and end is not None
            and kit_track.EARLIEST <= start <= end
            and all(isinstance(entry[k], str) and kit_track.SHA256.fullmatch(entry[k])
                    for k in ("source_sha256", "draft_sha256")))


def read_repeats(root):
    """{request key: {follows, date_from, date_to, source_sha256, draft_sha256}} of kit/repeat-drafts.json; {} when absent."""
    path = _kit_dir(root) / "repeat-drafts.json"
    if not os.path.lexists(path):
        return {}

    def bad(reason):
        return ValueError("kit/repeat-drafts.json unreadable: " + reason)
    if path.is_symlink() or not path.is_file():
        raise bad("not a regular file")
    if path.stat().st_size > kit_track.MAX_BYTES:
        raise bad("larger than 1 MiB")
    # The pathname checks above are only an early refusal. The object actually read is the one opened here without
    # following a final-component symlink (leaf only; ancestors are not anchored) and re-checked on its descriptor.
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise bad("not a regular file") from None
        raise
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise bad("not a regular file")
        if opened.st_size > kit_track.MAX_BYTES:
            raise bad("larger than 1 MiB")
        handle = os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise
    with handle:
        data = handle.read(kit_track.MAX_BYTES + 1)
    if len(data) > kit_track.MAX_BYTES:
        raise bad("larger than 1 MiB")
    try:
        document = json.loads(data.decode("utf-8"), object_pairs_hook=_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise bad("not JSON without duplicate keys") from None
    if not isinstance(document, dict) or set(document) != {"schema_version", "requests"} \
            or document["schema_version"] != 1 or type(document["schema_version"]) is not int \
            or not isinstance(document["requests"], dict):
        raise bad("expected schema_version 1 with a requests object")
    for key, entry in document["requests"].items():
        if not _entry_ok(key, entry):
            raise bad(f"malformed entry {key!r}")
    return document["requests"]


def bound_parent(root, agency_id, request, digest, requests):
    """The ``follows`` name for ``request`` when it is a repeat draft, else None; refuses a draft that no longer
    matches its recorded hash, or a predecessor that is no longer the recorded one."""
    meta = read_repeats(root).get(kit_track._key(agency_id, request))
    if meta is None:
        return None
    label = kit_track._label(agency_id, request)
    if meta["draft_sha256"] != digest:
        raise ValueError(f"{label} is a follow-on draft whose text changed since kit-repeat; restore the bound draft "
                         "(kit/repeat-drafts.json) so the chain is not lost, or start again with kit-repeat")
    parent = requests.get(kit_track._key(agency_id, meta["follows"]))
    if parent is None or parent.get("draft_sha256") != meta["source_sha256"]:
        raise ValueError(f"{label} follows {meta['follows']!r}, which is no longer the recorded sent request it was drafted from")
    return meta["follows"]


def _day_arg(value, flag):
    day = kit_track._day(value)
    if day is None or day < kit_track.EARLIEST:
        raise ValueError(f"{flag} must be a real YYYY-MM-DD date from 2000-01-01")
    return day


def _regular_folder(path, label):
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f"{label} must be a regular folder (not a symlink)")


def _follow_on_text(text, start, end):
    """The source draft with an explicit range: the sole range sentence replaced, or one appended when it is missing."""
    lines = text.split("\n")
    hits = kit.range_line_indexes(lines)
    if len(hits) > 1:
        raise ValueError("the earlier draft has more than one 'The date range for this request is' line; "
                         "edit it to one line before repeating it")
    span = f"{start.isoformat()} through {end.isoformat()}"
    sentence = f"{kit.RANGE_PREFIX}{span}."
    subjects = [i for i, line in enumerate(lines) if line.startswith("Subject: ")]
    old = None
    if hits:
        old = lines[hits[0]][len(kit.RANGE_PREFIX):].rstrip(".")
        lines[hits[0]] = sentence
    else:
        at = lines.index(CONTEXT) if CONTEXT in lines else len(lines)
        while at and lines[at - 1] == "":
            at -= 1
        lines[at:at] = ["", sentence]
    if len(subjects) == 1:
        subject = lines[subjects[0]]
        if SUBJECT_RANGE.search(subject):
            lines[subjects[0]] = SUBJECT_RANGE.sub(", " + span, subject)
        elif old and subject.endswith(", " + old):
            lines[subjects[0]] = subject[:-len(old)] + span
    return "\n".join(lines)


def repeat_request(root, agency_id, request, new_request, *, through=None, date_from=None, today=None):
    """Draft kit/requests/<agency>--<new_request>.md from the sent ``request``; returns a summary. Sends nothing."""
    root = Path(root)
    request, new_request = kit_track._request(request), kit_track._request(new_request)
    if new_request == "main":
        raise ValueError("--as must be a new request name, not main")
    if not isinstance(agency_id, str) or not kit.AGENCY_ID.match(agency_id):
        raise ValueError("--agency must be an agency ID such as ca-example-county")
    today = today or date.today()
    start_arg = None if date_from is None else _day_arg(date_from, "--from")
    end = today if through is None else _day_arg(through, "--through")
    if end > today:
        raise ValueError(f"--through {end.isoformat()} is after today ({today.isoformat()})")
    kit_dir = _kit_dir(root)
    agencies_path = kit_dir / "agencies.json"
    if not agencies_path.is_file() or agencies_path.is_symlink():
        raise ValueError("run kit first: kit/agencies.json is missing")
    agencies = kit._load_existing(agencies_path)
    if not any(entry["agency_id"] == agency_id for entry in agencies):
        raise ValueError(f"agency_id {agency_id!r} is not in kit/agencies.json")
    requests = kit_track.read_sent(root)
    label, new_label = kit_track._label(agency_id, request), kit_track._label(agency_id, new_request)
    entry = requests.get(kit_track._key(agency_id, request))
    if entry is None:
        raise ValueError(f"{label} is not recorded as sent; record it with kit-sent first")
    new_key, stem = kit_track._key(agency_id, new_request), kit_track._stem(agency_id, new_request)
    repeats = read_repeats(root)
    if new_key in requests or new_key in repeats:
        raise ValueError(f"{new_label} is already used; choose another --as name")
    if any(item["agency_id"] == stem for item in agencies):
        raise ValueError(f"kit/requests/{stem}.md is another agency's draft; choose another --as name")
    if start_arg is not None:
        start = start_arg
    elif "covers_through" in entry:
        try:
            start = kit_track._day(entry["covers_through"]) + timedelta(days=1)
        except OverflowError:
            raise ValueError(f"{label} covers through the last supported day; give --from") from None
    else:
        raise ValueError(f"{label} has no recorded coverage end (sent before kit-repeat, imported or unknown); "
                         "give --from YYYY-MM-DD, the first day the follow-on should cover")
    if start > end:
        raise ValueError(f"the follow-on would start {start.isoformat()}, after its end {end.isoformat()}; "
                         "give an earlier --from or a later --through")
    if entry.get("imported") is True or "draft_sha256" not in entry:
        raise ValueError(f"{label} was imported: its original draft is not on record, so its scopes cannot be repeated")
    requests_dir = kit_dir / "requests"
    _regular_folder(requests_dir, "kit/requests")
    source = requests_dir / (kit_track._stem(agency_id, request) + ".md")
    if source.is_symlink() or not source.is_file():
        raise ValueError(f"the original draft kit/requests/{source.name} must be a regular file (not a symlink)")
    data = source.read_bytes()
    source_hash = hashlib.sha256(data).hexdigest()
    if source_hash != entry["draft_sha256"]:
        raise ValueError(f"kit/requests/{source.name} changed since it was recorded as sent; restore it to repeat it")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError(f"kit/requests/{source.name} is not UTF-8") from None
    target = requests_dir / (stem + ".md")
    if os.path.lexists(target):
        raise ValueError(f"kit/requests/{stem}.md already exists; it is never replaced")
    body = _follow_on_text(text, start, end).encode("utf-8")
    meta = {"follows": request, "date_from": start.isoformat(), "date_to": end.isoformat(),
            "source_sha256": source_hash, "draft_sha256": hashlib.sha256(body).hexdigest()}
    store = _store_bytes({"schema_version": 1, "requests": dict(sorted({**repeats, new_key: meta}.items()))})
    if len(store) > kit_track.MAX_BYTES:  # checked before any output, so read_repeats can always read what we write
        raise ValueError("kit/repeat-drafts.json would be larger than 1 MiB; no follow-on draft was written; "
                         "existing requests were kept")
    store_path = kit_dir / "repeat-drafts.json"
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        raise ValueError(f"kit/requests/{stem}.md already exists; it is never replaced") from None
    owned = fd  # raw descriptor owned here until fdopen hands it to a stream
    try:
        handle = os.fdopen(fd, "wb")
        owned = None  # the stream owns it now; never close it a second time
        with handle:
            handle.write(body)
        _replace_store(store_path, store)
    except BaseException:
        try:
            if owned is not None:
                _close_unowned(owned)  # closed before its pathname is removed
        finally:
            os.unlink(target)  # only the draft this call created; the store was replaced atomically or not at all
        raise
    return {"agency_id": agency_id, "request": new_request, "follows": request, "draft": f"requests/{stem}.md",
            "date_from": meta["date_from"], "date_to": meta["date_to"], "draft_sha256": meta["draft_sha256"]}


def _close_unowned(fd):
    """Close a raw descriptor whose fdopen did not hand it over, never replacing the exception being unwound.

    fdopen may fail before it touched the descriptor (still open here) or after FileIO accepted it (CPython's
    io.open already closed it); a second close then raises EBADF. Either way the original error must escape.
    """
    try:
        os.close(fd)
    except OSError:
        pass  # already closed by io.open, or nothing more can be done; the caller re-raises the original error


def _store_bytes(document):
    """The exact bytes kit/repeat-drafts.json is written as (measured against the reader's 1 MiB limit)."""
    return (json.dumps(document, indent=2) + "\n").encode("utf-8")


def _replace_store(path, payload):
    """Atomically install ``payload``, the already size-checked bytes from _store_bytes, as a 0600 file."""
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".repeat-drafts.", suffix=".tmp")
    owned = fd  # raw descriptor owned here until fdopen hands it to a stream
    try:
        os.fchmod(fd, 0o600)
        handle = os.fdopen(fd, "wb")
        owned = None  # the stream owns it now; never close it a second time
        with handle:
            handle.write(payload)
        os.replace(temp, path)
    except BaseException:
        try:
            if owned is not None:
                _close_unowned(owned)  # closed before its temporary pathname is removed
        finally:
            os.unlink(temp)
        raise
