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


def stale_inputs(root, cfg, stale_days, today=None):
    """Stale, future-dated, unknown and absent inputs sorted by input name; fresh inputs are not listed."""
    today = today or date.today()
    jurisdiction = str(cfg.get("country", "US")).lower() + "-" + cfg["state"].lower()
    found = [_meetings(root, stale_days, today), _map(root, stale_days, today), _seed(jurisdiction, stale_days, today)]
    found += _law(jurisdiction, stale_days, today)
    return sorted((e for e in found if e), key=lambda e: (e["input"], e.get("title", "")))
