"""Input freshness for ``doctor --stale-days N`` (read-only).

Names each campaign input whose date is more than N days before today, with the command that refreshes it.
A missing or unparseable date is ``unknown`` and an input that does not exist is ``absent``: never assumed fresh.
"""
from datetime import date, datetime

from . import discovery, law
from .content import ContentError, load_snapshot

MAP_REFRESH = "python3 tools/fetch_osm_alprs.py --directory {} --relation <county relation id> (or --bbox SOUTH WEST NORTH EAST)"
SEED_REFRESH = "update the shared agency seed (package release)"


def _entry(name, status, refresh, day=None, today=None):
    entry = {"input": name, "status": status, "date": day and day.isoformat(), "refresh": refresh}
    if day is not None:
        entry["age_days"] = (today - day).days
    return entry


def _dated(name, day, refresh, stale_days, today):
    """An entry when the date is unknown or strictly older than stale_days; None when fresh."""
    if day is None:
        return _entry(name, "unknown", refresh)
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
        _rows, stale = law.review_sheet(law.load_package(jurisdiction), today, stale_days)
    except (OSError, ValueError, TypeError, KeyError):
        return [_entry(f"law package {jurisdiction}", "unknown", refresh)]
    return [{"input": "law:" + s["rule_id"], "status": "stale", "title": s["title"], "accessed": s["accessed"],
             "date": s["accessed"], "age_days": s["age_days"], "refresh": refresh} for s in stale]


def stale_inputs(root, cfg, stale_days, today=None):
    """Stale, unknown and absent inputs sorted by input name; fresh inputs are not listed."""
    today = today or date.today()
    jurisdiction = str(cfg.get("country", "US")).lower() + "-" + cfg["state"].lower()
    found = [_meetings(root, stale_days, today), _map(root, stale_days, today), _seed(jurisdiction, stale_days, today)]
    found += _law(jurisdiction, stale_days, today)
    return sorted((e for e in found if e), key=lambda e: (e["input"], e.get("title", "")))
