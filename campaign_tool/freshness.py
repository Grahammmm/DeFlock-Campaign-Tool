"""Input freshness for ``doctor --stale-days N`` (read-only).

Names each campaign input whose date is more than N days before today, with the command that refreshes it.
A missing or unparseable date is ``unknown`` and an input that does not exist is ``absent``: never assumed fresh.
A date after today is ``future`` (whatever N is): a clock or source-date anomaly, reported with a signed negative
``age_days`` and a refresh text that starts by asking the organizer to check the source date and the local clock.
It is a diagnostic, not a legal conclusion and not an automatic refresh. Law sources are aged one entry per source:
``records_law`` first, then each rule's sources in package order.
"""
from datetime import date, datetime

from . import discovery, law
from .content import ContentError, load_snapshot

MAP_REFRESH = "python3 tools/fetch_osm_alprs.py --directory {} --relation <county relation id> (or --bbox SOUTH WEST NORTH EAST)"
SEED_REFRESH = "update the shared agency seed (package release)"
FUTURE_PREFIX = "Check the source date and local clock; then "


def _entry(name, status, refresh, day=None, today=None):
    entry = {"input": name, "status": status, "date": day and day.isoformat(), "refresh": refresh}
    if day is not None:
        entry["age_days"] = (today - day).days
    return entry


def _dated(name, day, refresh, stale_days, today):
    """An entry when the date is unknown, after today (``future``) or strictly older than stale_days; None when fresh."""
    if day is None:
        return _entry(name, "unknown", refresh)
    if day > today:  # calendar days on the same injected today; independent of stale_days
        return _entry(name, "future", FUTURE_PREFIX + refresh, day, today)
    return _entry(name, "stale", refresh, day, today) if (today - day).days > stale_days else None


def _meetings(root, stale_days, today):
    from .cli import _read_previous  # no-follow, <= 4 MiB; None when absent
    document = _read_previous(root / "kit" / "meetings.json")
    fields = document if isinstance(document, dict) else {}  # "unreadable": symlink, too large or not JSON
    client = fields.get("client")
    refresh = (f"python3 -m campaign_tool meetings --directory {root} --online --client "
               + (client if isinstance(client, str) and client.strip() else "<legistar client>"))
    if document is None:
        return _entry("kit/meetings.json", "absent", refresh)
    day, generated = None, fields.get("generated")
    try:
        day = datetime.fromisoformat(generated).date() if isinstance(generated, str) else None
    except ValueError:
        pass
    return _dated("kit/meetings.json", day, refresh, stale_days, today)


def _map(root, stale_days, today):
    map_dir = root / "content" / "map"
    refresh = MAP_REFRESH.format(root)
    snapshot = map_dir / "snapshot.json"
    if not snapshot.exists() and not snapshot.is_symlink():
        exists = (map_dir / "cameras.geojson").exists()  # a pre-snapshot map has an unknown data date
        return _entry("content/map/snapshot.json", "unknown" if exists else "absent", refresh)
    try:
        day = load_snapshot(snapshot)
        day = date.fromisoformat(day) if day else None
    except (ContentError, OSError, ValueError):
        day = None
    return _dated("content/map/snapshot.json", day, refresh, stale_days, today)


def _seed(jurisdiction, stale_days, today):
    name = f"agency seed {jurisdiction}"
    try:
        path = discovery.seed_path(jurisdiction)
        if not path.is_file():
            return _entry(name, "absent", SEED_REFRESH)
        day = date.fromisoformat(discovery.load_seed(jurisdiction)["generated"])
    except (OSError, ValueError, TypeError, KeyError):
        day = None
    return _dated(name, day, SEED_REFRESH, stale_days, today)


def _law(jurisdiction, stale_days, today):
    refresh = (f"python3 -m campaign_tool.law review-sheet {jurisdiction} --as-of {today.isoformat()} "
               "--out FILE.csv")
    try:
        if not law.package_path(jurisdiction).is_file():
            return [_entry(f"law package {jurisdiction}", "absent", refresh)]
        package = law.load_package(jurisdiction)
        rules = [("records_law", package["records_law"]["sources"])]
        rules += [(rule["rule_id"], rule["sources"]) for rule in package["rules"]]
        dated = [(rule_id, source, date.fromisoformat(source["accessed"]))
                 for rule_id, sources in rules for source in sources]
    except (OSError, ValueError, TypeError, KeyError):
        return [_entry(f"law package {jurisdiction}", "unknown", refresh)]
    found = []
    for rule_id, source, day in dated:  # one entry per source, so a future date never hides behind an older one
        entry = _dated("law:" + rule_id, day, refresh, stale_days, today)
        if entry:
            found.append({**entry, "title": source["title"], "accessed": source["accessed"]})
    return found


PROFILE_FILE = "content/agencies.json"
_MISSING = object()  # no such public file at all (distinct from a file holding JSON null)
_BAD = object()  # present but unreadable: never echoed
PROFILE_REFRESH = "recheck the source and update checked_on"
PROFILE_SINGLETONS = ("alpr_policy", "contract", "policy_check", "contract_check", "pre_implementation_meeting",
                      "sharing")
OFFICIALS_FILE, OFFICIAL_REFRESH = "content/officials.json", "re-check the official's page and update checked_on"
SCHEDULES_FILE, SCHEDULE_REFRESH = "content/body-schedules.json", "re-check the body's calendar page and update checked_on"
SOURCES_FILE, COPY_REFRESH = "content/sources.json", "re-download the public copy and run sources verify-copy"


def _public_document(root, name):
    """The parsed public content/<name>: _MISSING when absent (a JSON null file is a document), _BAD when unreadable."""
    import json
    import os
    import stat
    from .content import MAX_JSON_BYTES
    path = root / "content" / name
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):  # symlink (even dangling), directory or FIFO: never opened
            return _BAD
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                return _BAD
            data = handle.read(MAX_JSON_BYTES + 1)
        return json.loads(data.decode("utf-8")) if len(data) <= MAX_JSON_BYTES else _BAD
    except FileNotFoundError:
        return _MISSING
    except (OSError, ValueError, RecursionError):
        return _BAD


def _profile_day(value):
    """A strict round-tripping YYYY-MM-DD string as a date; None for anything else."""
    if isinstance(value, str) and len(value) == 10:
        try:
            day = date.fromisoformat(value)
        except ValueError:
            return None
        return day if day.isoformat() == value else None
    return None


def _check(found, name, obj, refresh, stale_days, today):
    """Append the entry for one dated object; False (nothing appended) when the object is not a JSON object."""
    if not isinstance(obj, dict):
        return False
    entry = _dated(name, _profile_day(obj.get("checked_on")), refresh, stale_days, today)
    if entry:
        found.append(entry)
    return True


def _profiles(root, stale_days, today):
    """One entry per dated profile object in content/agencies.json (public file only; no private fallback)."""
    from .content import AGENCY_ID
    document = _public_document(root, "agencies.json")
    if document is _MISSING:
        return []
    found, bad = [], not isinstance(document, list)

    def check(name, obj):
        nonlocal bad
        bad |= not _check(found, name, obj, PROFILE_REFRESH, stale_days, today)

    for entry in [] if bad else document:
        if not isinstance(entry, dict) or not isinstance(entry.get("agency_id"), str) \
                or not AGENCY_ID.fullmatch(entry["agency_id"]):
            bad = True
            continue
        profile = entry.get("profile")
        if entry.get("selected", True) is not True or profile is None:
            continue
        if not isinstance(profile, dict):
            bad = True
            continue
        base = f"{PROFILE_FILE}:{entry['agency_id']}."
        for field in PROFILE_SINGLETONS:
            if profile.get(field) is not None:
                check(base + field, profile[field])
        for field in ("funding", "reports", "transparency"):
            items = profile.get(field)
            if items is None:
                continue
            if not isinstance(items, list):
                bad = True
                continue
            if field != "transparency":  # each funding source and each report's check is its own clock
                for index, item in enumerate(items):
                    check(f"{base}{field}[{index}]", item)
                continue
            newest = None  # the newest valid snapshot (first wins a tie); invalid ones are unknown
            for index, item in enumerate(items):
                day = _profile_day(item.get("checked_on")) if isinstance(item, dict) else None
                if not isinstance(item, dict):
                    bad = True
                elif day is None:
                    check(f"{base}transparency[{index}]", item)
                elif newest is None or day > newest[0]:
                    newest = (day, index, item)
            if newest:
                check(f"{base}transparency[{newest[1]}]", newest[2])
    if bad:
        found.append(_entry(PROFILE_FILE, "unknown", PROFILE_REFRESH))
    return found


def _rows(root, name, refresh, rows_of, stale_days, today):
    """One entry per dated object ``rows_of(document)`` yields as (input id, object) from public content/<name>.

    A missing file adds nothing; an unreadable file or a bad envelope or row shape adds one file-level ``unknown``
    (valid sibling rows are still dated). Ids use stored zero-based positions, never the row's own text."""
    document = _public_document(root, name)
    if document is _MISSING:
        return []
    found, label, bad = [], "content/" + name, False
    try:
        for input_id, obj in rows_of(document):
            bad |= obj is _BAD or not _check(found, f"{label}:{input_id}", obj, refresh, stale_days, today)
    except (TypeError, KeyError, AttributeError):  # wrong envelope (including _BAD): nothing more to iterate
        bad = True
    return found + [_entry(label, "unknown", refresh)] * bad


def _official_rows(document):
    if document["schema_version"] != 1 or not isinstance(document["bodies"], list):
        raise TypeError
    for b, body in enumerate(document["bodies"]):
        people = body.get("officials") if isinstance(body, dict) else None
        if not isinstance(people, list):
            yield f"bodies[{b}]", _BAD
            continue
        yield from ((f"bodies[{b}].officials[{i}]", person) for i, person in enumerate(people))


def _schedule_rows(document):
    if type(document["schema_version"]) is not int or document["schema_version"] != 1 \
            or not isinstance(document["schedules"], list):
        raise TypeError
    yield from ((f"schedules[{i}]", row) for i, row in enumerate(document["schedules"]))


def _copy_rows(document):
    if not isinstance(document, list):
        raise TypeError
    for i, source in enumerate(document):  # each present public_copy is its own check, even for a repeated digest
        if not isinstance(source, dict):
            yield f"[{i}]", _BAD
        elif source.get("public_copy") is not None:
            yield f"[{i}].public_copy", source["public_copy"]


def stale_inputs(root, cfg, stale_days, today=None):
    """Stale, future-dated, unknown and absent inputs sorted by input name; fresh inputs are not listed."""
    today = today or date.today()
    jurisdiction = str(cfg.get("country", "US")).lower() + "-" + cfg["state"].lower()
    found = [_meetings(root, stale_days, today), _map(root, stale_days, today), _seed(jurisdiction, stale_days, today)]
    found += _law(jurisdiction, stale_days, today)
    found += _profiles(root, stale_days, today)
    found += _rows(root, "officials.json", OFFICIAL_REFRESH, _official_rows, stale_days, today)
    found += _rows(root, "body-schedules.json", SCHEDULE_REFRESH, _schedule_rows, stale_days, today)
    found += _rows(root, "sources.json", COPY_REFRESH, _copy_rows, stale_days, today)
    return sorted((e for e in found if e), key=lambda e: (e["input"], e.get("title", "")))
