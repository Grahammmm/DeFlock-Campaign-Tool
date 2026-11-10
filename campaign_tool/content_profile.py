"""DF1109B-09: list every dated agency-profile check and record one re-read date (``content agency-profile``).

``list_profile_checks`` reads ``content/agencies.json`` through the build's own ``content.load_agencies`` and prints each
recorded ``checked_on`` of the nine profile check families with its age, oldest first. ``recheck_profile`` sets exactly
one existing ``checked_on`` after the organizer re-read that source: never a future date or one before the stored date,
and only when the whole prospective file still passes ``load_agencies``; the write goes through the same admitted
directory descriptors and identity checks as ``content agency-sharing`` and replaces the file atomically. Nothing is
fetched, built or published, and no other field is created, moved or changed.

``--index`` is the 1-based position in the stored list; ``doctor --stale-days`` names the same entry zero-based in
brackets (``reports[0]`` is ``--field reports --index 1``).
"""
import json
import os
import re
import secrets
from datetime import date

from .content import MAX_JSON_BYTES, ContentError, _calendar_day, load_agencies
from .content_sharing import TARGET, _install_problem, _open_content, _parse_target, _read_target

SINGLE_FIELDS = ("alpr_policy", "policy_check", "contract", "contract_check", "pre_implementation_meeting", "sharing")
LIST_FIELDS = ("funding", "transparency", "reports")
FIELDS = SINGLE_FIELDS + LIST_FIELDS
DEPENDENT = {"alpr_policy": "policy_check", "contract": "contract_check"}  # document -> the reading that may not be older
INDEX = re.compile(r"[1-9][0-9]{0,5}")
NOTICE = "Recorded only. Nothing was fetched, built or published."


class ContentProfileError(ContentError):
    pass


def _selected(document):
    """The raw selected agency entries of an already loader-validated document."""
    return [entry for entry in document if entry.get("selected", True) is True]


def _read_document(root):
    """The parsed content/agencies.json, or None when content/ or the file is absent (never through a link)."""
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        raise ContentProfileError(f"content/: the selected campaign directory cannot be opened ({exc.strerror})") from None
    content_fd = target_fd = None
    try:
        try:
            content_fd = os.open("content", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ContentProfileError(f"content/: must be a real directory inside the selected campaign, not a link "
                                      f"({exc.strerror})") from None
        try:
            os.stat(TARGET, dir_fd=content_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        target_fd, _, data = _read_target(content_fd)
        return _parse_target(data)
    finally:
        for fd in (target_fd, content_fd, root_fd):
            if fd is not None:
                os.close(fd)


def _facts(entry):
    """``[(field, 1-based index or None, stored fact)]`` for one raw agency entry, in field order then stored order."""
    profile, found = entry.get("profile") or {}, []
    for field in SINGLE_FIELDS:
        if isinstance(profile.get(field), dict) and "checked_on" in profile[field]:
            found.append((field, None, profile[field]))
    for field in LIST_FIELDS:
        found += [(field, n, item) for n, item in enumerate(profile.get(field) or [], 1)]
    return found


def list_profile_checks(root, agency_id=None, today=None):
    """``[{agency_id, field, index, checked_on, age_days}]`` oldest first; [] when nothing is recorded."""
    today = today or date.today()
    document = _read_document(root)
    if document is None:
        document = []
    load_agencies(document, "content/agencies.json", today)  # the whole file, as build reads it; refuses before listing
    entries = _selected(document)
    if agency_id is not None:
        entries = [entry for entry in entries if entry["agency_id"] == agency_id]
        if not entries:
            raise ContentProfileError(f"--agency {agency_id}: not a selected agency in content/agencies.json")
    rows = [{"agency_id": entry["agency_id"], "field": field, "index": index, "checked_on": fact["checked_on"],
             "age_days": (today - _calendar_day(fact["checked_on"], "checked_on")).days}
            for entry in entries for field, index, fact in _facts(entry)]
    return sorted(rows, key=lambda r: (r["checked_on"], r["agency_id"], r["field"], r["index"] or 0))


def format_profile_checks(rows, as_json=False):
    if as_json:
        return json.dumps(rows, indent=2, ensure_ascii=False) + "\n"
    if not rows:
        return "No recorded profile checks.\n"
    return "".join(f"{r['agency_id']} | {r['field']}{'' if r['index'] is None else '[%d]' % r['index']} | "
                   f"checked_on={r['checked_on']} | age_days={r['age_days']}\n" for r in rows)


def _target_fact(document, agency_id, field, index):
    """-> (stored fact dict, display name, position of the agency in the file) of the one existing fact; refuses
    anything it would have to create."""
    found = [n for n, a in enumerate(document) if isinstance(a, dict) and a.get("agency_id") == agency_id] \
        if isinstance(document, list) else []
    if len(found) != 1:
        raise ContentProfileError(f"--agency {agency_id}: not found in content/agencies.json")
    position = found[0]
    if document[position].get("selected", True) is not True:
        raise ContentProfileError(f"--agency {agency_id}: is not selected")
    profile = document[position].get("profile")
    value = profile.get(field) if isinstance(profile, dict) else None
    if field in SINGLE_FIELDS:
        if index is not None:
            raise ContentProfileError(f"--index: {field} is a single record; leave --index out")
        if not isinstance(value, dict) or "checked_on" not in value:
            raise ContentProfileError(f"--field {field}: {agency_id} has no recorded {field}; nothing to recheck")
        return value, field, position
    count = len(value) if isinstance(value, list) else 0
    if count == 0:
        raise ContentProfileError(f"--field {field}: {agency_id} has no recorded {field} entries; nothing to recheck")
    if index is None:
        raise ContentProfileError(f"--index: required for {field}, 1 to {count} (the 1-based stored position)")
    if not (isinstance(index, str) and INDEX.fullmatch(index)) or int(index) > count:
        raise ContentProfileError(f"--index: {field} has entries 1 to {count} (the 1-based stored position)")
    item = value[int(index) - 1]
    if not isinstance(item, dict) or "checked_on" not in item:
        raise ContentProfileError(f"--field {field} --index {index}: entry has no checked_on")
    return item, f"{field}[{index}]", position


def _admit_prospective(document, position, field, index, new, today):
    """The build's loader over the whole prospective document; a refusal names what to recheck first."""
    if field == "transparency" and int(index) > 1:
        newer = document[position]["profile"]["transparency"][int(index) - 2]
        newer = newer.get("checked_on") if isinstance(newer, dict) else None
        if isinstance(newer, str) and new >= newer:
            raise ContentProfileError(f"--checked-on: transparency[{index}] must stay older than transparency[{int(index) - 1}] "
                                      f"({newer}); snapshots are kept newest first, so record a fresh reading as a new "
                                      "snapshot at the top instead; nothing was written")
    try:
        load_agencies(document, "content/agencies.json", today)
    except ContentError as exc:
        hint = ""
        reading = DEPENDENT.get(field)
        if reading and f"[{position}].profile.{reading}.checked_on: must not be before" in str(exc):
            hint = f" Recheck {reading} first with --checked-on {new} (or later), then retry {field}."
        raise ContentProfileError(f"{exc}; nothing was written.{hint}") from None


def _before_install():
    """Seam between the last write and the identity check; does nothing (tests place a deterministic swap here)."""


def _install(root_fd, content_fd, content_stat, target_fd, target_stat, original, text):
    """Write ``text`` to a fresh 0600 temporary file in content/ and replace agencies.json only if nothing changed."""
    temp_fd = temp = None
    try:
        name = f".{TARGET}.{secrets.token_hex(8)}.tmp"
        temp_fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=content_fd)
        temp = name
        view = memoryview(text)
        while view:
            view = view[os.write(temp_fd, view):]
        os.fsync(temp_fd)
        os.close(temp_fd)
        temp_fd = None
        _before_install()
        if (why := _install_problem(root_fd, content_fd, content_stat, target_fd, target_stat, original)) is not None:
            raise ContentProfileError(why.replace("the import", "the recheck") + "; nothing was written")
        os.replace(temp, TARGET, src_dir_fd=content_fd, dst_dir_fd=content_fd)
        temp = None
    finally:
        if temp_fd is not None:
            os.close(temp_fd)
        if temp is not None:  # only this command's own temporary file, from the admitted directory
            try:
                os.unlink(temp, dir_fd=content_fd)
            except OSError:
                pass


def recheck_profile(root, agency_id, field, checked_on, index=None, today=None):
    """Set one existing profile ``checked_on``; -> ``{agency_id, field, index, old, new, changed}``."""
    today = today or date.today()
    if field not in FIELDS:
        raise ContentProfileError(f"--field: choose one of {list(FIELDS)}")
    if _calendar_day(checked_on, "--checked-on") > today:
        raise ContentProfileError("--checked-on: must not be after today")
    if isinstance(index, int) and not isinstance(index, bool):
        index = str(index)
    root_fd, content_fd = _open_content(root)
    target_fd = None
    try:
        content_stat = os.fstat(content_fd)
        target_fd, target_stat, original = _read_target(content_fd)
        document = _parse_target(original)
        fact, name, position = _target_fact(document, agency_id, field, index)
        old = fact["checked_on"]
        if checked_on < _calendar_day(old, f"{name}.checked_on").isoformat():
            raise ContentProfileError(f"--checked-on: must not be before stored checked_on {old}")
        fact["checked_on"] = checked_on  # the only change, in the parsed copy of the whole file
        _admit_prospective(document, position, field, index, checked_on, today)
        result = {"agency_id": agency_id, "field": field, "index": None if index is None else int(index),
                  "old": old, "new": checked_on, "changed": old != checked_on}
        if not result["changed"]:
            return result
        text = (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        if len(text) > MAX_JSON_BYTES:
            raise ContentProfileError(f"content/agencies.json: the rewritten file would exceed {MAX_JSON_BYTES} bytes; "
                                      "nothing was written")
        _install(root_fd, content_fd, content_stat, target_fd, target_stat, original, text)
        return result
    finally:
        for fd in (target_fd, content_fd, root_fd):
            if fd is not None:
                os.close(fd)


def format_recheck(result):
    where = result["field"] + ("" if result["index"] is None else f"[{result['index']}]")
    head = (f"Rechecked {result['agency_id']} {where}: {result['old']} -> {result['new']}." if result["changed"]
            else f"Already checked on {result['new']}; nothing changed.")
    return f"{head}\n{NOTICE}\n"
