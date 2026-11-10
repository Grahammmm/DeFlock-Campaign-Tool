"""DF1109B-09: list every dated agency-profile check and record one re-read date (``content agency-profile``).

``list_profile_checks`` reads ``content/agencies.json`` through the build's own ``content.load_agencies`` and prints each
recorded ``checked_on`` of the nine profile check families with its age, oldest first. ``recheck_profile`` sets exactly
one existing ``checked_on`` after the organizer re-read that source: never a future date or one before the stored date,
and only when the whole prospective file still passes ``load_agencies``; the write goes through the same admitted
directory descriptors and identity checks as ``content agency-sharing`` and replaces the file atomically. Nothing is
fetched, built or published, and no other field is created, moved or changed.

``--index`` is the 1-based position in the stored list; ``doctor --stale-days`` names the same entry zero-based in
brackets (``reports[0]`` is ``--field reports --index 1``).

DF1109B-17: ``write_policy`` and ``write_contract`` record a whole ``profile.alpr_policy`` / ``profile.contract`` document
through the same transaction. An existing document needs ``replace``; a reading of the previous document
(``policy_check`` / ``contract_check``) is never rebound to a different document URL: it is removed in the same atomic
write only with ``drop_check``. The whole prospective file passes the build's loader and the shared privacy lint first.
"""
import json
import os
import re
import secrets
import unicodedata
from datetime import date

from . import law
from .agency_cards import text as plain_text
from .content import (ARCHIVE_KEYS, CHECKLIST_JURISDICTION, MAX_JSON_BYTES, ContentError, _calendar_day, load_agencies,
                      load_contract_checklist)
from .content_sharing import TARGET, _install_problem, _open_content, _parse_target, _read_target
from .privacy_patterns import categories, refusal

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


def _agency(document, agency_id):
    """The position of the one selected agency ``agency_id`` in the raw file."""
    found = [n for n, a in enumerate(document) if isinstance(a, dict) and a.get("agency_id") == agency_id] \
        if isinstance(document, list) else []
    if len(found) != 1:
        raise ContentProfileError(f"--agency {agency_id}: not found in content/agencies.json")
    if document[found[0]].get("selected", True) is not True:
        raise ContentProfileError(f"--agency {agency_id}: is not selected")
    return found[0]


def _target_fact(document, agency_id, field, index):
    """-> (stored fact dict, display name, position of the agency in the file) of the one existing fact; refuses
    anything it would have to create."""
    position = _agency(document, agency_id)
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


def _install(root_fd, content_fd, content_stat, target_fd, target_stat, original, text, verb="recheck"):
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
            raise ContentProfileError(why.replace("the import", f"the {verb}") + "; nothing was written")
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


def _transaction(root, change, verb):
    """Read content/agencies.json through the admitted descriptors, let ``change(document)`` edit the parsed copy and
    return ``(result, changed)``, then install the whole rewritten file once, or refuse with nothing written."""
    root_fd, content_fd = _open_content(root)
    target_fd = None
    try:
        content_stat = os.fstat(content_fd)
        target_fd, target_stat, original = _read_target(content_fd)
        result, changed = change(_parse_target(original))
        if not changed:
            return result
        text = (json.dumps(result.pop("_document"), indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        if len(text) > MAX_JSON_BYTES:
            raise ContentProfileError(f"content/agencies.json: the rewritten file would exceed {MAX_JSON_BYTES} bytes; "
                                      "nothing was written")
        _install(root_fd, content_fd, content_stat, target_fd, target_stat, original, text, verb)
        return result
    finally:
        for fd in (target_fd, content_fd, root_fd):
            if fd is not None:
                os.close(fd)


def recheck_profile(root, agency_id, field, checked_on, index=None, today=None):
    """Set one existing profile ``checked_on``; -> ``{agency_id, field, index, old, new, changed}``."""
    today = today or date.today()
    if field not in FIELDS:
        raise ContentProfileError(f"--field: choose one of {list(FIELDS)}")
    if _calendar_day(checked_on, "--checked-on") > today:
        raise ContentProfileError("--checked-on: must not be after today")
    if isinstance(index, int) and not isinstance(index, bool):
        index = str(index)

    def change(document):
        fact, name, position = _target_fact(document, agency_id, field, index)
        old = fact["checked_on"]
        if checked_on < _calendar_day(old, f"{name}.checked_on").isoformat():
            raise ContentProfileError(f"--checked-on: must not be before stored checked_on {old}")
        fact["checked_on"] = checked_on  # the only change, in the parsed copy of the whole file
        _admit_prospective(document, position, field, index, checked_on, today)
        result = {"agency_id": agency_id, "field": field, "index": None if index is None else int(index),
                  "old": old, "new": checked_on, "changed": old != checked_on}
        return (result | {"_document": document}) if result["changed"] else result, result["changed"]
    return _transaction(root, change, "recheck")


def format_recheck(result):
    where = result["field"] + ("" if result["index"] is None else f"[{result['index']}]")
    head = (f"Rechecked {result['agency_id']} {where}: {result['old']} -> {result['new']}." if result["changed"]
            else f"Already checked on {result['new']}; nothing changed.")
    return f"{head}\n{NOTICE}\n"


# document field -> (its document URL key, the reading of it, the reading's URL key, the reading's command)
DOCUMENTS = {"alpr_policy": ("url", "policy_check", "policy_url", "policy-check"),
             "contract": ("source_url", "contract_check", "contract_url", "contract-check")}
WRITE_NOTICE = "Recorded only. Nothing was fetched, built, published or sent."


def _leaves(value, where):
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _leaves(item, f"{where}.{key}")
    elif isinstance(value, str):
        yield where, value


def _admit_document(document, position, field, today):
    """The new document's own text through the shared privacy lint, then the whole prospective file through the
    build's loader and the build's privacy gate over every admitted agency; a refusal names place and rule only."""
    where = f"content/agencies.json[{position}].profile.{field}"
    for place, text in _leaves(document[position]["profile"][field], where):
        if note := refusal(place, text):
            raise ContentProfileError(f"{note}; nothing was written")
    from .site import _privacy_gate  # local: the site module is large and only writers need its gate
    try:
        _privacy_gate({"agencies": load_agencies(document, "content/agencies.json", today)})
    except ContentError as exc:
        raise ContentProfileError(f"{exc}; nothing was written") from None


def _write_document(root, agency_id, field, new, replace, drop_check, today):
    """Record one whole document; -> ``{agency_id, field, checked_on, dropped, reading, verb, archive_removed}``."""
    today = today or date.today()
    url_key, reading, reading_url, verb = DOCUMENTS[field]
    if drop_check and not replace:
        raise ContentProfileError("--drop-check requires --replace; nothing was written")
    if _calendar_day(new["checked_on"], "--checked-on") > today:
        raise ContentProfileError("--checked-on: must not be after today; nothing was written")

    def change(document):
        position = _agency(document, agency_id)
        entry = document[position]
        if entry.get("profile") is None:
            entry["profile"] = {}
        profile = entry["profile"]
        if not isinstance(profile, dict):
            raise ContentProfileError(f"content/agencies.json[{position}].profile: must be an object; nothing was written")
        old = profile.get(field)
        if old is not None and not replace:
            raise ContentProfileError(f"profile.{field}: {agency_id} already has one; pass --replace to record the "
                                      "whole document again (unsupplied optional fields are removed); nothing was written")
        old = old if isinstance(old, dict) else {}
        same = url_key in old and old[url_key] == new[url_key]  # exact string, never canonicalized or fetched
        archive_removed = False
        if ARCHIVE_KEYS & set(old):
            if same:  # the archived copy is of this same document, so it stays with it
                new.update({key: old[key] for key in sorted(ARCHIVE_KEYS) if key in old})
            else:
                archive_removed = True
        check, dropped = profile.get(reading), None
        if check is not None:
            if not same and not drop_check:
                raise ContentProfileError(f"profile.{field}.{url_key} changes while {reading} exists; pass --replace "
                                          f"--drop-check, then record a new reading with {verb}; nothing was written")
            stored = check.get("checked_on") if isinstance(check, dict) else None
            if same and not drop_check and isinstance(stored, str) and new["checked_on"] > stored:
                raise ContentProfileError(f"profile.{reading}.checked_on would be before profile.{field}.checked_on; "
                                          f"pass --drop-check or redo {verb} first with --checked-on not before "
                                          f"{new['checked_on']}; nothing was written")
            if drop_check:  # removed, never rebound to the new document: the old reading is of the old one
                dropped = stored if isinstance(stored, str) else "unknown"
                del profile[reading]
        profile[field] = new
        _admit_document(document, position, field, today)
        return {"agency_id": agency_id, "field": field, "checked_on": new["checked_on"], "dropped": dropped,
                "reading": reading, "verb": verb, "archive_removed": archive_removed, "_document": document}, True
    return _transaction(root, change, "write")


def write_policy(root, agency_id, *, url, source_url, checked_on, replace=False, drop_check=False, today=None):
    """``profile.alpr_policy`` = the posted policy ``url``, the agency page ``source_url`` and the reading day."""
    new = {"url": url, "source_url": source_url, "checked_on": checked_on}
    return _write_document(root, agency_id, "alpr_policy", new, replace, drop_check, today)


def write_contract(root, agency_id, *, vendor, start, end, source_url, checked_on, renewal_note=None, cost_usd=None,
                   period=None, as_stated_in=None, replace=False, drop_check=False, today=None):
    """``profile.contract`` as the signed agreement states it; ``cost_usd`` stays the typed decimal string."""
    cost = (cost_usd, period, as_stated_in)
    if any(value is not None for value in cost) and not all(value is not None for value in cost):
        raise ContentProfileError("--cost-usd, --period and --as-stated-in go together (all three or none); "
                                  "nothing was written")
    new = {"vendor": vendor, "start": start, "end": end}
    if renewal_note is not None:
        new["renewal_note"] = renewal_note
    new.update(source_url=source_url, checked_on=checked_on)
    if cost_usd is not None:
        new["cost"] = {"amount_usd": cost_usd, "period": period, "as_stated_in": as_stated_in}
    return _write_document(root, agency_id, "contract", new, replace, drop_check, today)


def format_document(result):
    lines = [f"Recorded profile.{result['field']} for {result['agency_id']} (checked_on {result['checked_on']})."]
    if result["archive_removed"]:
        lines.append(f"Removed profile.{result['field']} archived_url and archived_on: they were a copy of the previous "
                     "document.")
    if result["dropped"] is not None:
        lines.append(f"Dropped profile.{result['reading']} (checked_on {result['dropped']}); record a new reading with "
                     f"content agency-profile {result['verb']}.")
    return "\n".join(lines + [WRITE_NOTICE]) + "\n"


# DF1109B-18: the organizer's reading of a stored document against the checklist (policy_check / contract_check).
READING_NOTICE = "This is the organizer's reading, not a legal review."
STATUSES = ("addressed", "not_found", "unclear")
PAGE = re.compile(r"[1-9][0-9]{0,3}")  # ASCII digits only, 1..9999, canonical
NOTE_LIMIT, ID_SHOWN, IDS_SHOWN = 200, 40, 20


def _checklist_ids(field):
    """The checklist element ids, in checklist order, read from the build's own resources."""
    try:
        if field == "alpr_policy":
            return [element["id"] for element in law.load_checklist(CHECKLIST_JURISDICTION)["elements"]]
        return [element["id"] for element in load_contract_checklist()["elements"]]
    except (ValueError, ContentError) as exc:
        raise ContentProfileError(f"the {'policy' if field == 'alpr_policy' else 'contract'} checklist could not be "
                                  f"loaded ({exc}); nothing was written") from None


def _shown(ids):
    """Ids for a diagnostic: bounded, escaped, and never a private-looking value."""
    shown = [("(withheld)" if categories(i) else ascii(i[:ID_SHOWN])) for i in list(ids)[:IDS_SHOWN]]
    return ", ".join(shown) + (f" (+{len(ids) - IDS_SHOWN} more)" if len(ids) > IDS_SHOWN else "") if ids else "(none)"


def _note_problem(text):
    if (not text.strip() or len(text) > NOTE_LIMIT or not plain_text(text)
            or any(unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") for c in text)):
        return f"plain text of 1 to {NOTE_LIMIT} characters (no angle brackets or control characters)"
    return None


def parse_reading_elements(field, element_args, note_args=()):
    """Raw ``{id: {status, page?, note?}}`` in checklist order from repeated ``ID=STATUS[:PAGE]`` and ``ID=TEXT`` tokens.

    Every id of the checklist must be given exactly once; one refusal names all missing, unknown and duplicate ids and
    each field problem. Neither a note nor a whole submitted token is echoed."""
    ids, problems, found, notes = _checklist_ids(field), [], {}, {}
    contract = field == "contract"
    duplicates, unknown = set(), set()
    for number, token in enumerate(element_args, 1):
        key, equals, value = token.partition("=")
        status, colon, page = value.partition(":")
        if not equals or not key or not status or ":" in page:
            problems.append(f"--element #{number}: expected ID=STATUS or ID=STATUS:PAGE")
            continue
        if key not in ids:
            unknown.add(key)
            continue
        if key in found:
            duplicates.add(key)
            continue
        entry = {}
        if status not in STATUSES:
            problems.append(f"--element {key}: status must be one of {list(STATUSES)}")
        else:
            entry["status"] = status
        if colon:
            if not PAGE.fullmatch(page):
                problems.append(f"--element {key}: page must be a whole number from 1 to 9999 without leading zeros")
            elif status != "addressed" and not contract:
                problems.append(f"--element {key}: page is only allowed when status is addressed")
            else:
                entry["page"] = int(page)
        found[key] = entry
    missing = [i for i in ids if i not in found]
    if missing or unknown or duplicates:
        problems.insert(0, f"--element: Missing element ids: {_shown(missing)}; unknown element ids: {_shown(sorted(unknown))}; "
                           f"duplicate element ids: {_shown(sorted(duplicates))}")
    for number, token in enumerate(note_args, 1):
        key, equals, text = token.partition("=")
        if not equals or not key:
            problems.append(f"--note #{number}: expected ID=TEXT")
        elif key not in found:
            problems.append(f"--note #{number}: names no element given with --element")
        elif key in notes:
            problems.append(f"--note {key}: given more than once")
        elif why := _note_problem(text):
            problems.append(f"--note {key}: {why}")
        elif place := refusal(f"--note {key}", text):
            problems.append(place)
        else:
            notes[key] = text
    if problems:
        raise ContentProfileError("; ".join(problems) + "; nothing was written")
    return {key: {**found[key], **({"note": notes[key]} if key in notes else {})} for key in ids}


def _write_reading(root, agency_id, field, checked_on, elements, replace, today):
    """Record ``profile.<reading>`` for the stored document; -> ``{agency_id, field, reading, checked_on, count}``."""
    today = today or date.today()
    url_key, reading, reading_url, verb = DOCUMENTS[field]
    command = "policy" if field == "alpr_policy" else "contract"
    given = _calendar_day(checked_on, "--checked-on")
    if given > today:
        raise ContentProfileError("--checked-on: must not be after today; nothing was written")

    def change(document):
        position = _agency(document, agency_id)
        profile = document[position].get("profile")
        stored = profile.get(field) if isinstance(profile, dict) else None
        if not isinstance(stored, dict) or not isinstance(stored.get(url_key), str):
            raise ContentProfileError(f"profile.{reading}: record profile.{field} with content agency-profile {command} "
                                      "first; nothing was written")
        if _calendar_day(stored.get("checked_on"), f"profile.{field}.checked_on") > given:
            raise ContentProfileError(f"--checked-on: must not be before profile.{field}.checked_on "
                                      f"{stored['checked_on']}; nothing was written")
        if profile.get(reading) is not None and not replace:
            raise ContentProfileError(f"profile.{reading}: {agency_id} already has one; pass --replace to record the "
                                      "whole reading again (omitted pages and notes are removed); nothing was written")
        profile[reading] = {"checked_on": checked_on, reading_url: stored[url_key], "elements": elements}
        _admit_document(document, position, reading, today)
        return {"agency_id": agency_id, "field": reading, "checked_on": checked_on, "count": len(elements),
                "_document": document}, True
    return _transaction(root, change, "write")


def write_policy_check(root, agency_id, *, checked_on, elements, replace=False, today=None):
    """``profile.policy_check`` from ``parse_reading_elements("alpr_policy", ...)``; ``policy_url`` is the stored one."""
    return _write_reading(root, agency_id, "alpr_policy", checked_on, elements, replace, today)


def write_contract_check(root, agency_id, *, checked_on, elements, replace=False, today=None):
    """``profile.contract_check`` from ``parse_reading_elements("contract", ...)``; ``contract_url`` is the stored one."""
    return _write_reading(root, agency_id, "contract", checked_on, elements, replace, today)


def format_reading(result):
    return (f"Recorded profile.{result['field']} for {result['agency_id']} (checked_on {result['checked_on']}; "
            f"{result['count']} elements).\n{READING_NOTICE}\n{WRITE_NOTICE}\n")
