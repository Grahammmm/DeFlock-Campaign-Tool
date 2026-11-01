#!/usr/bin/env python3
"""Fetch community-mapped ALPR points from OpenStreetMap for a campaign map.

Standard library only. --bbox and --relation use the network and are never run
by tests; --from-file reads a saved Overpass answer the organizer reviewed, offline.

    python3 tools/fetch_osm_alprs.py --directory my-campaign --bbox SOUTH WEST NORTH EAST
    python3 tools/fetch_osm_alprs.py --directory my-campaign --relation 396490
    python3 tools/fetch_osm_alprs.py --directory my-campaign --from-file SAVED.json
    python3 tools/fetch_osm_alprs.py --directory my-campaign --relation 396490 --check   # writes nothing

It queries the Overpass API for nodes tagged ``man_made=surveillance`` and
``surveillance:type=ALPR`` inside a bounding box or an OSM relation (a county
boundary relation id), and writes:

    <directory>/content/map/cameras.geojson   Point features with properties
                                              manufacturer, operator, url
    <directory>/content/map/attribution.txt   the required attribution text

Data: (c) OpenStreetMap contributors, Open Database License (ODbL) 1.0,
https://www.openstreetmap.org/copyright. Most ALPR nodes are added by the
DeFlock community (https://deflock.me); DeFlock is the community source of
these points, not an agency record. A mapped point says a camera was observed,
not which agency owns it or what it records. Verify with records requests
before publishing any statement about an agency. Review the output before
building the site; ``campaign_tool build`` validates it but cannot judge it.
"""
import argparse
import contextlib
import json
import math
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

OVERPASS_ENDPOINTS = ("https://overpass-api.de/api/interpreter",
                      "https://overpass.kumi.systems/api/interpreter")
USER_AGENT = "deflock-campaign-tool/0.1 (+https://github.com/Grahammmm/deflock-campaign-tool)"
ATTRIBUTION = ("Map data (c) OpenStreetMap contributors, ODbL 1.0 "
               "(https://www.openstreetmap.org/copyright). Community ALPR points via DeFlock "
               "(https://deflock.me). Points show observed cameras, not confirmed agency ownership.")
TEXT_LIMIT = 200
MAX_ANSWER_BYTES = 50 * 1024 * 1024
NOT_CAMPAIGN = "fetch_osm_alprs: --directory is not a campaign directory (no campaign.json)"
EMPTY_KEPT = "fetch_osm_alprs: answer has no points; current map kept (use --allow-empty to replace)"
NODE_URL = re.compile(r"https://www\.openstreetmap\.org/node/[1-9][0-9]{0,18}")
CHANGE_KINDS = ("added", "removed", "moved", "retagged")
LIST_LIMIT = 20


class WriteRefused(Exception):
    """A refused or failed write: the message is printed; exit 3 for EMPTY_KEPT, else 2."""


def query_for(args):
    filters = '["man_made"="surveillance"]["surveillance:type"="ALPR"]'
    if args.relation:
        area = 3600000000 + args.relation
        return f"[out:json][timeout:90];area({area})->.a;node{filters}(area.a);out body;"
    south, west, north, east = args.bbox
    return f"[out:json][timeout:90];node{filters}({south},{west},{north},{east});out body;"


def fetch(query, endpoints=OVERPASS_ENDPOINTS, attempts=3):
    body = urllib.parse.urlencode({"data": query}).encode()
    last = None
    for attempt in range(attempts):
        for endpoint in endpoints:
            request = urllib.request.Request(endpoint, data=body, headers={"User-Agent": USER_AGENT})
            try:
                with urllib.request.urlopen(request, timeout=120) as response:
                    if response.status != 200:
                        raise urllib.error.HTTPError(endpoint, response.status, "Overpass error", None, None)
                    return json.load(response)
            except (urllib.error.URLError, TimeoutError, ValueError) as exc:
                last = exc
                print(f"Overpass request failed ({endpoint}): {exc}", file=sys.stderr)
        time.sleep(5 * (attempt + 1))
    raise SystemExit(f"Could not fetch from Overpass after {attempts} attempts: {last}")


def clean(value):
    value = str(value or "").strip()
    return value.replace("<", "").replace(">", "")[:TEXT_LIMIT]


def _refuse_constant(name):
    raise ValueError(f"non-finite constant {name}")


def load_saved(path):
    """Return the saved answer's elements list, or None when the file is not valid Overpass JSON."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_ANSWER_BYTES + 1)
        answer = json.loads(raw.decode("utf-8"), parse_constant=_refuse_constant) \
            if len(raw) <= MAX_ANSWER_BYTES else None
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(answer, dict) or not isinstance(answer.get("elements"), list):
        return None
    return answer["elements"]


def _coordinate(value, limit):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and -limit <= value <= limit)


def _skip_reason(element, seen):
    if not isinstance(element, dict) or element.get("type") != "node":
        return "not-node"
    if "lat" not in element or "lon" not in element:
        return "no-coordinates"
    if not (_coordinate(element["lat"], 90) and _coordinate(element["lon"], 180)):
        return "bad-coordinates"
    node_id = element.get("id")
    if not isinstance(node_id, int) or isinstance(node_id, bool) or node_id <= 0:
        return "bad-id"
    if node_id in seen:
        return "duplicate"
    return None


def to_geojson(elements):
    """Return (FeatureCollection, {skip reason: count}); bad and duplicate points are never written."""
    features, seen, skipped = [], set(), {}
    for element in elements:
        reason = _skip_reason(element, seen)
        if reason:
            skipped[reason] = skipped.get(reason, 0) + 1
            continue
        seen.add(element["id"])
        tags = element.get("tags") if isinstance(element.get("tags"), dict) else {}
        features.append({
            "type": "Feature",
            "properties": {
                "manufacturer": clean(tags.get("manufacturer")),
                "operator": clean(tags.get("operator")),
                "url": f"https://www.openstreetmap.org/node/{int(element['id'])}",
            },
            "geometry": {"type": "Point", "coordinates": [float(element["lon"]), float(element["lat"])]},
        })
    features.sort(key=lambda f: f["properties"]["url"])
    return {"type": "FeatureCollection", "features": features}, skipped


def _current_points(path):
    """Feature count of the current map: 0 when absent, None when unreadable or not a regular file (it is
    already broken; a FIFO or device is never opened). The caller has already refused a symlink."""
    if not path.exists():
        return 0
    if not path.is_file():
        return None
    try:
        features = json.loads(path.read_text(encoding="utf-8"))["features"]
        return len(features) if isinstance(features, list) else None
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        return None


def _write_atomic(final, text, after=""):
    """Replace final through a fresh temp file in the same directory; never follow a symlinked final."""
    if final.is_symlink():
        raise WriteRefused(f"fetch_osm_alprs: {final.name} is a symlink; not written{after}")
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=final.parent, prefix="." + final.name + ".")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, final)
    except Exception:
        if tmp is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
        raise WriteRefused(f"fetch_osm_alprs: could not write {final.name}; it was left unchanged{after}") from None


def write_map(campaign, collection, allow_empty):
    """Create content/ then content/map one level at a time (never a symlink); write cameras, then attribution."""
    map_dir = campaign
    for name in ("content", "map"):
        map_dir = map_dir / name
        if map_dir.is_symlink() or (map_dir.exists() and not map_dir.is_dir()):
            raise WriteRefused(f"fetch_osm_alprs: {map_dir} is a symlink or not a directory; nothing written")
        try:
            map_dir.mkdir(exist_ok=True)
        except OSError:  # read-only filesystem, no permission, or a racing non-directory
            raise WriteRefused(f"fetch_osm_alprs: could not create {name}/ under the campaign directory; "
                               "nothing written") from None
    cameras = map_dir / "cameras.geojson"
    if cameras.is_symlink():  # refused before the empty guard reads the current map: a link is never followed
        raise WriteRefused("fetch_osm_alprs: cameras.geojson is a symlink; not written")
    if not collection["features"] and not allow_empty:
        current = _current_points(cameras)
        if current is None:
            print("fetch_osm_alprs: warning: current cameras.geojson is unreadable; replacing it", file=sys.stderr)
        elif current:
            raise WriteRefused(EMPTY_KEPT)
    _write_atomic(cameras, json.dumps(collection, indent=1) + "\n")
    _write_atomic(map_dir / "attribution.txt", ATTRIBUTION + "\n", " (the new cameras.geojson was written)")
    return map_dir


def load_current(path):
    """The current FeatureCollection (no features when absent), or None when it is not readable GeoJSON."""
    try:
        current = json.loads(Path(path).read_bytes().decode("utf-8"), parse_constant=_refuse_constant)
    except FileNotFoundError:
        return {"type": "FeatureCollection", "features": []}
    except (OSError, ValueError, RecursionError):
        return None
    valid = isinstance(current, dict) and current.get("type") == "FeatureCollection"
    return current if valid and isinstance(current.get("features"), list) else None


def _keyed(collection):
    """Node URL -> (tags, [lon, lat] or None); features without a valid first-seen node URL are only counted."""
    keyed, unkeyed = {}, 0
    for feature in collection["features"]:
        props = feature.get("properties") if isinstance(feature, dict) else None
        url = props.get("url") if isinstance(props, dict) else None
        if not isinstance(url, str) or not NODE_URL.fullmatch(url) or url in keyed:
            unkeyed += 1
            continue
        point = (feature.get("geometry") if isinstance(feature.get("geometry"), dict) else {}).get("coordinates")
        valid = isinstance(point, list) and len(point) == 2 and _coordinate(point[0], 180) \
            and _coordinate(point[1], 90)
        keyed[url] = (tuple(str(props.get(k) or "") for k in ("manufacturer", "operator")), point if valid else None)
    return keyed, unkeyed


def _metres(a, b):
    """Haversine distance in metres between two [lon, lat] points."""
    lon1, lat1, lon2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371008.8 * math.asin(math.sqrt(min(1.0, h)))


def diff(old_collection, new_collection, move_m=10.0):
    """Compare by node URL; a point can be moved and retagged at once; an unusable old point counts as moved."""
    old, unkeyed_old = _keyed(old_collection)
    new = _keyed(new_collection)[0]
    result = {"added": sorted(new.keys() - old.keys()), "removed": sorted(old.keys() - new.keys()),
              "moved": [], "retagged": [], "unchanged": [], "unkeyed_old": unkeyed_old}
    for url in sorted(old.keys() & new.keys()):
        (old_tags, old_point), (new_tags, new_point) = old[url], new[url]
        moved = old_point is None or new_point is None or _metres(old_point, new_point) > move_m
        for kind in ["moved"] * moved + ["retagged"] * (old_tags != new_tags) or ["unchanged"]:
            result[kind].append(url)
    return result


def print_check(changes):
    """Counts, then up to LIST_LIMIT sorted URLs per change kind; never tag text or coordinates. 3 on removals."""
    counts = (f"{k}={len(changes[k])}" for k in (*CHANGE_KINDS, "unchanged"))
    print(*counts, f"unkeyed_old={changes['unkeyed_old']}")
    for kind in (k for k in CHANGE_KINDS if changes[k]):
        print(f"{kind}:", *(f"  {url}" for url in changes[kind][:LIST_LIMIT]), sep="\n")
        if len(changes[kind]) > LIST_LIMIT:
            print(f"  … +{len(changes[kind]) - LIST_LIMIT} more")
    print("Check only: nothing was written.")
    return 3 if changes["removed"] else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--directory", required=True, help="Campaign directory (contains campaign.json)")
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--bbox", nargs=4, type=float, metavar=("SOUTH", "WEST", "NORTH", "EAST"))
    scope.add_argument("--relation", type=int, help="OSM relation id of the county boundary")
    scope.add_argument("--from-file", metavar="SAVED.json",
                       help="saved Overpass JSON answer the organizer reviewed (offline, no network)")
    parser.add_argument("--allow-empty", action="store_true",
                        help="let an answer with no points replace a map that has points")
    parser.add_argument("--check", action="store_true", help="compare with the current cameras.geojson by node "
                        "URL and write nothing; exit 3 when any point would be removed")
    parser.add_argument("--move-metres", type=float, metavar="M", help="with --check: moved beyond M metres (10)")
    args = parser.parse_args(argv)
    if args.move_metres is not None and not (args.check and math.isfinite(args.move_metres)
                                             and args.move_metres >= 0):
        parser.error("--move-metres needs --check and a finite number of metres >= 0")
    if args.bbox:
        south, west, north, east = args.bbox
        if not (-90 <= south < north <= 90 and -180 <= west < east <= 180):
            parser.error("bbox must be SOUTH WEST NORTH EAST within valid ranges")
    # --check (RF1730-10) only reads and writes nothing, so it keeps its own gate; the campaign.json gate
    # (RF1730-11) guards every write and runs before any fetch or read of the answer.
    current = None
    if args.check:
        current = load_current(Path(args.directory) / "content" / "map" / "cameras.geojson")
        if current is None:
            print("fetch_osm_alprs: current cameras.geojson is not readable GeoJSON", file=sys.stderr)
            return 2
    else:
        marker = Path(args.directory) / "campaign.json"
        if marker.is_symlink() or not marker.is_file():
            print(NOT_CAMPAIGN, file=sys.stderr)
            return 2
    if args.from_file:
        elements = load_saved(args.from_file)
        if elements is None:
            print("fetch_osm_alprs: saved answer is not valid Overpass JSON", file=sys.stderr)
            return 2
    else:
        elements = fetch(query_for(args)).get("elements", [])
    collection, skipped = to_geojson(elements if isinstance(elements, list) else [])
    if args.check:
        return print_check(diff(current, collection, 10.0 if args.move_metres is None else args.move_metres))
    try:
        map_dir = write_map(Path(args.directory), collection, args.allow_empty)
    except WriteRefused as exc:
        print(exc, file=sys.stderr)
        return 3 if str(exc) == EMPTY_KEPT else 2
    print(f"Wrote {len(collection['features'])} points to {map_dir / 'cameras.geojson'}")
    print(f"points={len(collection['features'])} skipped={sum(skipped.values())}")
    for reason in sorted(skipped):
        print(f"skipped {reason}={skipped[reason]}")
    print("ATTRIBUTION REQUIRED: " + ATTRIBUTION)
    print("Review the points before building. A mapped camera is not evidence of agency ownership.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
