#!/usr/bin/env python3
"""Fetch community-mapped ALPR points from OpenStreetMap for a campaign map.

Standard library only. This tool uses the network and is never run by tests.

    python3 tools/fetch_osm_alprs.py --directory my-campaign --bbox SOUTH WEST NORTH EAST
    python3 tools/fetch_osm_alprs.py --directory my-campaign --relation 396490

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


def to_geojson(elements):
    features = []
    for element in elements:
        if element.get("type") != "node" or "lat" not in element or "lon" not in element:
            continue
        tags = element.get("tags") or {}
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
    return {"type": "FeatureCollection", "features": features}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--directory", required=True, help="Campaign directory (contains campaign.json)")
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--bbox", nargs=4, type=float, metavar=("SOUTH", "WEST", "NORTH", "EAST"))
    scope.add_argument("--relation", type=int, help="OSM relation id of the county boundary")
    args = parser.parse_args(argv)
    if args.bbox:
        south, west, north, east = args.bbox
        if not (-90 <= south < north <= 90 and -180 <= west < east <= 180):
            parser.error("bbox must be SOUTH WEST NORTH EAST within valid ranges")
    map_dir = Path(args.directory) / "content" / "map"
    map_dir.mkdir(parents=True, exist_ok=True)
    collection = to_geojson(fetch(query_for(args)).get("elements", []))
    (map_dir / "cameras.geojson").write_text(json.dumps(collection, indent=1) + "\n", encoding="utf-8")
    (map_dir / "attribution.txt").write_text(ATTRIBUTION + "\n", encoding="utf-8")
    print(f"Wrote {len(collection['features'])} points to {map_dir / 'cameras.geojson'}")
    print("ATTRIBUTION REQUIRED: " + ATTRIBUTION)
    print("Review the points before building. A mapped camera is not evidence of agency ownership.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
