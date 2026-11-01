#!/usr/bin/env python3
"""Fetch community-mapped ALPR points from OpenStreetMap for a campaign map.

Standard library only. --bbox and --relation use the network and are never run
by tests; --from-file reads a saved Overpass answer the organizer reviewed, offline.

    python3 tools/fetch_osm_alprs.py --directory my-campaign --bbox SOUTH WEST NORTH EAST
    python3 tools/fetch_osm_alprs.py --directory my-campaign --relation 396490
    python3 tools/fetch_osm_alprs.py --directory my-campaign --from-file SAVED.json

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
import json
import math
import sys
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--directory", required=True, help="Campaign directory (contains campaign.json)")
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--bbox", nargs=4, type=float, metavar=("SOUTH", "WEST", "NORTH", "EAST"))
    scope.add_argument("--relation", type=int, help="OSM relation id of the county boundary")
    scope.add_argument("--from-file", metavar="SAVED.json",
                       help="saved Overpass JSON answer the organizer reviewed (offline, no network)")
    args = parser.parse_args(argv)
    if args.bbox:
        south, west, north, east = args.bbox
        if not (-90 <= south < north <= 90 and -180 <= west < east <= 180):
            parser.error("bbox must be SOUTH WEST NORTH EAST within valid ranges")
    if args.from_file:
        elements = load_saved(args.from_file)
        if elements is None:
            print("fetch_osm_alprs: saved answer is not valid Overpass JSON", file=sys.stderr)
            return 2
    else:
        elements = fetch(query_for(args)).get("elements", [])
    collection, skipped = to_geojson(elements if isinstance(elements, list) else [])
    map_dir = Path(args.directory) / "content" / "map"
    map_dir.mkdir(parents=True, exist_ok=True)
    (map_dir / "cameras.geojson").write_text(json.dumps(collection, indent=1) + "\n", encoding="utf-8")
    (map_dir / "attribution.txt").write_text(ATTRIBUTION + "\n", encoding="utf-8")
    print(f"Wrote {len(collection['features'])} points to {map_dir / 'cameras.geojson'}")
    print(f"points={len(collection['features'])} skipped={sum(skipped.values())}")
    for reason in sorted(skipped):
        print(f"skipped {reason}={skipped[reason]}")
    print("ATTRIBUTION REQUIRED: " + ATTRIBUTION)
    print("Review the points before building. A mapped camera is not evidence of agency ownership.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
