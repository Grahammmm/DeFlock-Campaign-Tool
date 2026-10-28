"""Fill place_fips for the California agency seed from the Census Gazetteer.

NETWORK REQUIRED. Never run from tests or CI. Usage:

    python3 tools/refresh_agency_seed_online.py          # download, write overlay, rebuild JSON
    python3 tools/refresh_agency_seed_online.py --file 2024_Gaz_place_national.txt

Downloads the national Gazetteer "place" file (or reads a local copy), keeps
California incorporated places (LSAD city/town, not CDPs), and writes
data/agencies/us-ca.place-fips.json mapping seed city names to 7-digit GEOIDs.
tools/build_agency_seed.py merges that overlay on its next run, so the JSON
seed stays a deterministic function of the tables plus the overlay. Only
place_fips is touched; contacts, portals and verification flags are left to
organizers. Unmatched seed cities are printed so a contributor can check the
spelling in the tables.
"""
import argparse
import io
import json
import sys
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_agency_seed as seed  # noqa: E402

DEFAULT_URL = ("https://www2.census.gov/geo/docs/maps-data/data/gazetteer/"
               "2024_Gazetteers/2024_Gaz_place_national.zip")
INCORPORATED_SUFFIXES = (" city", " town")
NAME_FIXES = {"Ventura": "San Buenaventura (Ventura)", "Paso Robles": "El Paso de Robles (Paso Robles)"}


def fetch(url, timeout=30):
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 (organizer-run)
        data = response.read()
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        name = next(n for n in archive.namelist() if n.endswith(".txt"))
        return archive.read(name).decode("utf-8", errors="replace")


def parse(text, state="CA"):
    places = {}
    lines = text.splitlines()
    header = [h.strip() for h in lines[0].split("\t")]
    usps, geoid, name = header.index("USPS"), header.index("GEOID"), header.index("NAME")
    for line in lines[1:]:
        cells = [c.strip() for c in line.split("\t")]
        if len(cells) <= max(usps, geoid, name) or cells[usps] != state:
            continue
        full = cells[name]
        for suffix in INCORPORATED_SUFFIXES:
            if full.endswith(suffix):
                places[full[: -len(suffix)]] = cells[geoid]
                break
    return places


def overlay_for_seed(places):
    mapping, missing = {}, []
    for _fips, _county, _seat, cities in seed.COUNTIES:
        for city, _policing in cities:
            key = NAME_FIXES.get(city, city)
            code = places.get(key) or places.get(city)
            if code and len(code) == 7 and code.isdigit():
                mapping[city] = code
            else:
                missing.append(city)
    return mapping, missing


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--file", help="Local Gazetteer place .txt instead of downloading")
    parser.add_argument("--overlay", default=str(seed.OVERLAY))
    args = parser.parse_args(argv)
    text = Path(args.file).read_text(encoding="utf-8") if args.file else fetch(args.url)
    mapping, missing = overlay_for_seed(parse(text))
    overlay = {"source": args.file or args.url, "state": "CA",
               "note": "Census Gazetteer incorporated-place GEOIDs; input to tools/build_agency_seed.py",
               "places": dict(sorted(mapping.items()))}
    Path(args.overlay).write_text(json.dumps(overlay, indent=2, ensure_ascii=False) + "\n",
                                  encoding="utf-8")
    print(f"Matched {len(mapping)} cities; unmatched: {missing or 'none'}")
    seed.OUTPUT.write_text(seed.render(seed.build(seed.load_overlay(args.overlay))), encoding="utf-8")
    print(f"Rebuilt {seed.OUTPUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
