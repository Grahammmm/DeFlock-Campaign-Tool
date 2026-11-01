"""Name every content/ problem in one pass, with exactly the rules ``build`` enforces (read-only).

``load_content`` stops at the first refusal; this calls the same loaders file by file and keeps going. Parity
invariant: ``content_problems(root) == []`` iff ``load_content(root)`` returns. Cross-file rules use loaded files.
"""
from pathlib import Path

from .content import (CITY_KINDS, LOCAL_ASSET, MAX_GEOJSON_BYTES, ContentError, _bounds, _read_json,
                      _require_keys, _slug, _text, _validate_cameras, default_agencies, load_agencies,
                      load_finding, load_map, load_meeting, load_site, load_sources, plain_text)

LIMIT = 200
CAMERAS_MISSING = "map/cameras.geojson is missing; run tools/fetch_osm_alprs.py or supply reviewed data"


def _attempt(problems, label, call):
    """Return ``call()`` or record its refusal (named after ``label``'s file) and return None."""
    try:
        return call()
    except ContentError as exc:
        text, head, folder = str(exc), str(exc).split(":", 1)[0], label.rpartition("/")[0]
        if folder and "/" not in head and head.endswith((".json", ".geojson")):  # _read_json names the bare file
            text = folder + "/" + text
    except ValueError as exc:  # map_controller.render refuses with a plain ValueError naming the field
        text = f"{label}: {exc}"
    problems.append(text)
    return None


def _cities(path, _name):  # load_map's map/cities.json expressions
    raw = _read_json(path)
    if not isinstance(raw, dict) or len(raw) > 200:
        raise ContentError("map/cities.json: expected {slug: {bounds: [[w,s],[e,n]]}}")
    for slug, entry in raw.items():
        _slug(slug, "map/cities.json key")
        _require_keys(entry, {"bounds", "name"}, {"bounds"}, f"map/cities.json[{slug}]")
        _bounds(entry["bounds"], f"map/cities.json[{slug}].bounds")
        _text(entry, "name", f"map/cities.json[{slug}]", required=False, default=None)
    return set(raw)


def _boundaries(path, name):  # load_map's city-boundary-file expressions
    document = _read_json(path, MAX_GEOJSON_BYTES)
    if not isinstance(document, dict) or document.get("type") != "FeatureCollection":
        raise ContentError(f"map/{name}: expected a GeoJSON FeatureCollection")


def _attribution(document, map_dir):  # load_map's attribution expressions
    attribution, path = document.get("attribution"), map_dir / "attribution.txt"
    if attribution is None and path.is_file():
        attribution = path.read_text(encoding="utf-8").strip()
    if attribution is not None and (not plain_text(attribution) or len(attribution) > 500):
        raise ContentError("map attribution must be plain text under 500 characters")


def _config_tail(document, cities):  # load_map's map-config.json checks that run after its sibling files
    for slug in document["bounding_box_fallbacks"] if cities is not None else []:  # cities None: its file is named
        if slug not in cities:
            raise ContentError(f"map/map-config.json.bounding_box_fallbacks: {slug!r} has no bounds in "
                               "map/cities.json")
    _bounds(document["county_bounds"], "map/map-config.json.county_bounds")
    _text(document, "group_label", "map/map-config.json", default="Unincorporated area (ownership unconfirmed)")


def _map(problems, content_dir):
    """load_map stops at its first bad file; once it has refused (so parity holds), name the other files' too."""
    before, map_dir = len(problems), content_dir / "map"
    config = _attempt(problems, "map/map-config.json", lambda: load_map(content_dir))
    if len(problems) == before:
        return config
    config_named = problems[-1].startswith("map/map-config.json")  # else load_map passed the config's head
    checks = {"cameras.geojson": lambda p, n: _validate_cameras(_read_json(p, MAX_GEOJSON_BYTES), "map/" + n),
              "cities.json": _cities}
    missing = {"cameras.geojson": CAMERAS_MISSING}
    document = None
    try:
        document = _read_json(map_dir / "map-config.json")
        url = document["city_boundaries_url"]
        if LOCAL_ASSET.fullmatch(url):
            checks.setdefault(Path(url).name, _boundaries)
            missing.setdefault(Path(url).name, f"map/{Path(url).name}: city boundary file is missing")
    except (ValueError, KeyError, TypeError):
        pass  # map-config.json's own refusal is already listed
    cities = set()  # an absent cities.json means no city bounds, as in load_map
    for name, check in checks.items():
        if (map_dir / name).is_file():
            result = _attempt(problems, "map/" + name, lambda: check(map_dir / name, name))
            if name == "cities.json":
                cities = result
        elif name in missing:
            problems.append(missing[name])
    if not config_named:
        _attempt(problems, "map/attribution.txt", lambda: _attribution(document, map_dir))
        _attempt(problems, "map/map-config.json", lambda: _config_tail(document, cities))
    return config


def _files(content_dir, sub):
    folder = content_dir / sub
    return sorted(folder.glob("*.json")) if folder.is_dir() else []


def _meeting(path, label):
    meeting = load_meeting(_read_json(path), label)
    meeting["id"] = meeting["id"] or _slug(path.stem, label + " filename")
    return meeting


def _cap(problems, limit):
    if limit is None or len(problems) <= limit:
        return problems
    return problems[:limit] + [f"\u2026 and {len(problems) - limit} more"]


def content_problems(root, limit=LIMIT):
    """Sorted, de-duplicated problem lines for ``root/content``; None when there is no content/."""
    root = Path(root)
    content_dir = root / "content"
    if not content_dir.is_dir():
        return None
    problems = []
    site_path = content_dir / "site.json"
    if not site_path.is_file():
        problems.append("content/site.json is required when content/ exists")
    else:
        _attempt(problems, "site.json", lambda: load_site(_read_json(site_path)))

    slugs, ids = set(), set()
    for path in _files(content_dir, "findings"):
        label = "findings/" + path.name
        finding = _attempt(problems, label, lambda: load_finding(_read_json(path), label))
        if finding is None:
            continue
        if finding["slug"] in slugs or finding["id"] in ids:
            problems.append(f"findings/{path.name}: duplicate finding slug or id")
        slugs.add(finding["slug"])
        ids.add(finding["id"])

    for path in _files(content_dir, "meetings"):
        label = "meetings/" + path.name
        _attempt(problems, label, lambda: _meeting(path, label))

    sources_path = content_dir / "sources.json"
    if sources_path.is_file():
        _attempt(problems, "sources.json", lambda: load_sources(_read_json(sources_path)))

    agencies_path = content_dir / "agencies.json"
    if agencies_path.is_file():
        agencies = _attempt(problems, "content/agencies.json",
                            lambda: load_agencies(_read_json(agencies_path), "content/agencies.json"))
    else:
        agencies = _attempt(problems, "kit/agencies.json", lambda: default_agencies(root))

    map_config = _map(problems, content_dir)
    if map_config and agencies is not None:  # a broken agency list would only add false lines
        known = {agency["slug"] for agency in agencies if agency["kind"] in CITY_KINDS}
        for slug in map_config["controller"]["bounding_box_fallbacks"]:
            if slug not in known:
                problems.append(f"map/map-config.json.bounding_box_fallbacks: {slug!r} is not a "
                                "police/city_council agency slug")
    return _cap(sorted(set(problems)), limit)


def run_cli(root):
    """Print ``content/: N problem(s)`` and one line each; exit 3 when any problem is listed."""
    problems = content_problems(root, limit=None)
    if problems is None:
        print("no content/ directory")
        return
    print(f"content/: {len(problems)} problem(s)")
    for line in _cap(problems, LIMIT):
        print(line)
    if problems:
        raise SystemExit(3)
