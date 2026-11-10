"""DF1107-04: organizer-verified decisions made by other governments (``content/elsewhere.json`` -> ``/elsewhere.html``).

An optional JSON array of 1-100 entries. The organizer reads each decision off the deciding body's own record and attests
the date; this module checks syntax only. It never fetches a URL, infers a place, ranks, counts or reconciles anything.
Build raises the first problem; content-check collects every independent problem with the same messages.
"""
import json
import os
import re
import unicodedata
from datetime import date
from pathlib import Path

from .content import MAX_JSON_BYTES, ContentError, _https, plain_text

FILE = "elsewhere.json"
KINDS = ("ended_contract", "paused", "declined", "adopted_ordinance", "restricted_sharing", "audit_ordered", "other")
REQUIRED = ("place", "state", "body", "decided_on", "kind", "as_stated", "source_url", "checked_on")
OPTIONAL = ("news_url",)
MAX_ENTRIES, MAX_TEXT = 100, 200
DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
STATE = re.compile(r"[A-Z]{2}")


def _line(value):
    """The trimmed single-line plain text, or None when it is not 1-200 plain characters."""
    if not isinstance(value, str) or not plain_text(value):
        return None
    value = value.strip()
    if not 1 <= len(value) <= MAX_TEXT or any(unicodedata.category(char) in ("Cc", "Cf", "Zl", "Zp") for char in value):
        return None
    return value


def _day(value):
    if not isinstance(value, str) or not DAY.fullmatch(value):
        return None
    try:
        return date(int(value[:4]), int(value[5:7]), int(value[8:]))
    except ValueError:
        return None


def _entry(entry, index, today, refuse):
    """Validate one entry; ``refuse(where, message)`` for every independent problem. Returns the entry or None."""
    label = f"{FILE}[{index}]"
    failed = []

    def bad(field, message):
        failed.append(field)
        refuse(f"{label}.{field}" if field else label, message)

    if not isinstance(entry, dict):
        bad("", "expected an object")
        return None
    unknown = sorted(str(key) for key in set(entry) - set(REQUIRED) - set(OPTIONAL))
    if unknown:
        bad("", f"unknown field(s) {unknown}")
    missing = [key for key in REQUIRED if key not in entry]
    if missing:
        bad("", f"missing field(s) {missing}")
    out = {}
    for key in ("place", "body", "as_stated"):
        if key in entry:
            out[key] = _line(entry[key])
            if out[key] is None:
                bad(key, f"must be trimmed plain single-line text of 1-{MAX_TEXT} characters without angle brackets or control characters")
    if "state" in entry:
        if not isinstance(entry["state"], str) or not STATE.fullmatch(entry["state"]):
            bad("state", "must be a two-letter uppercase state code such as CA")
        out["state"] = entry["state"]
    if "kind" in entry:
        if not isinstance(entry["kind"], str) or entry["kind"] not in KINDS:
            bad("kind", "must be one of " + ", ".join(KINDS))
        out["kind"] = entry["kind"]
    days = {}
    for key in ("decided_on", "checked_on"):
        if key in entry:
            days[key] = _day(entry[key])
            if days[key] is None:
                bad(key, "must be a real YYYY-MM-DD date")
            out[key] = entry[key]
    if days.get("checked_on") is not None and days["checked_on"] > today:
        bad("checked_on", f"{entry['checked_on']} is in the future")
    if days.get("decided_on") is not None and days.get("checked_on") is not None and days["decided_on"] > days["checked_on"]:
        bad("decided_on", "must not be after checked_on")
    for key in ("source_url", "news_url"):
        if key in entry:
            try:
                out[key] = _https(entry[key], f"{label}.{key}")
            except ContentError as exc:
                failed.append(key)
                refuse(f"{label}.{key}", str(exc).split(": ", 1)[-1])
    return None if failed else {key: out[key] for key in (*REQUIRED, *OPTIONAL) if key in out}


def load_elsewhere(document, *, today=None, problems=None):
    """Validate a parsed ``elsewhere.json``: a list of 1-100 entries, kept in stored order.

    Refuses, never skips. With ``problems`` (a list) every independent problem is appended and None returned; without it
    the first raises ``ContentError``. Duplicates (place + body + decided_on + kind, trimmed and literal) name both indices."""
    today = today or date.today()
    found = []

    def refuse(where, message):
        found.append(f"{where}: {message}")

    if not isinstance(document, list) or not 1 <= len(document) <= MAX_ENTRIES:
        refuse(FILE, f"must be a list of 1 to {MAX_ENTRIES} entries")
    else:
        accepted, seen = [], {}
        for index, raw in enumerate(document):
            entry = _entry(raw, index, today, refuse)
            if entry is None:
                continue
            identity = (entry["place"], entry["body"], entry["decided_on"], entry["kind"])
            if identity in seen:
                refuse(f"{FILE}[{index}]", f"duplicate of {FILE}[{seen[identity]}] (same place, body, decided_on and kind)")
                continue
            seen[identity] = index
            accepted.append(entry)
        if not found:
            return accepted
    if problems is None:
        raise ContentError(found[0])
    problems.extend(found)
    return None


class _RepeatedMember(Exception):
    """A JSON object repeated a member name (private; translated to ``ContentError`` without the name)."""


def _no_repeats(pairs):
    found = {}
    for key, value in pairs:
        if key in found:
            raise _RepeatedMember
        found[key] = value
    return found


def _read_strict(path):
    """``content._read_json`` (same size cap, UTF-8 and symlink rules) that refuses a repeated member name at any depth.

    The payload is one binary read of at most ``MAX_JSON_BYTES + 1`` bytes from a handle closed before decoding; the extra
    byte only proves the file is over the cap, so a larger file is refused without being read further or parsed.

    The decoder would otherwise keep the last value, so the file would have two readings: the maintenance commands
    refuse it already. The refusal names neither the repeated key nor either value."""
    if path.is_symlink():
        raise ContentError(f"{path.name}: symlinks are not read")
    with path.open("rb") as handle:
        data = handle.read(MAX_JSON_BYTES + 1)
    if len(data) > MAX_JSON_BYTES:
        raise ContentError(f"{path.name}: file too large")
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_no_repeats)
    except _RepeatedMember:
        raise ContentError(f"{path.name}: a JSON object repeats a key") from None
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContentError(f"{path.name}: not valid UTF-8 JSON ({exc})") from None
    except ValueError:
        # Parser-conversion boundary: the decoder's own refusal to convert a number (e.g. an integer token over the
        # interpreter's digit ceiling). Fixed text only: no exception text, number, digit count or tuning advice.
        raise ContentError(f"{path.name}: not valid UTF-8 JSON (value cannot be decoded)") from None


def read_elsewhere(root, today=None, problems=None):
    """``load_elsewhere`` of ``<root>/content/elsewhere.json``; None when the file is absent.

    A symlink (even dangling), folder or other non-regular path is refused, never skipped.
    A JSON object that repeats a member name (identical or conflicting values) is refused as one file-level problem."""
    path = Path(root) / "content" / FILE
    try:
        if path.is_symlink():
            raise ContentError(f"{FILE}: symlinks are not read")
        if not os.path.lexists(path):
            return None
        if not path.is_file():
            raise ContentError(f"{FILE}: must be a regular file, not a symlink or folder")
        try:
            document = _read_strict(path)
        except OSError:
            raise ContentError(f"{FILE}: could not be read") from None
        except RecursionError:
            raise ContentError(f"{FILE}: not valid UTF-8 JSON (nested too deeply)") from None
    except ContentError as exc:
        if problems is None:
            raise
        problems.append(str(exc))
        return None
    return load_elsewhere(document, today=today, problems=problems)


def sorted_for_page(entries):
    """A copy grouped by kind in KINDS order: ``[(kind, [entries newest decided_on first])]``, empty kinds omitted.

    The tie break is literal and deterministic (place, body, state, source_url); the stored order is not touched."""
    groups = []
    for kind in KINDS:
        rows = [entry for entry in entries if entry["kind"] == kind]
        rows.sort(key=lambda item: (item["place"], item["body"], item["state"], item["source_url"]))
        rows.sort(key=lambda item: item["decided_on"], reverse=True)
        if rows:
            groups.append((kind, rows))
    return groups
