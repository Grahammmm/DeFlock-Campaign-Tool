"""Resolve a county or city to a location and the agencies that serve it.

Works offline from data/agencies/<country>-<state>.json (schema
schemas/agency-seed.schema.json). Everything the seed says is unverified
contributor knowledge: ``agencies_for`` returns suggestions (``suggested:
true``) that an organizer must review before any request is sent. Nothing in
this module claims that an agency operates ALPR.

``locate_online`` is the only function that touches the network, and only
when a caller explicitly asks for it (``campaign_tool kit --online``).
"""
import json
import re
import unicodedata
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

from . import law

REPO_ROOT = Path(__file__).resolve().parents[1]
SEED_DIR = REPO_ROOT / "data" / "agencies"
SEED_SCHEMA = REPO_ROOT / "schemas" / "agency-seed.schema.json"
GEOCODER = "https://geocoding.geo.census.gov/geocoder/geographies/onelineaddress"
COUNTY_KINDS = ("sheriff", "county_board", "district_attorney", "chp")
CITY_KINDS = ("police", "city_council")
LAW_ENFORCEMENT_KINDS = ("sheriff", "police", "chp")
DEFAULT_INCLUDE = ("sheriff", "police", "county_board", "city_council",
                   "district_attorney", "chp")
STATE_FIPS = {"CA": "06"}


@dataclass(frozen=True)
class Location:
    county_fips: str
    county_name: str
    state: str
    place_fips: str | None
    place_name: str | None
    match_kind: str  # "county" or "city"
    query: str = ""

    def to_dict(self):
        return asdict(self)


def normalize(text):
    text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()
    text = text.lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def seed_path(jurisdiction, base=None):
    if not law.JURISDICTION_PATTERN.match(str(jurisdiction)):
        raise ValueError(f"Jurisdiction must look like 'us-ca', got {jurisdiction!r}")
    return Path(base or SEED_DIR) / (jurisdiction + ".json")


def load_seed(jurisdiction, base=None):
    """Read and schema-validate the agency seed for ``jurisdiction``."""
    path = seed_path(jurisdiction, base)
    if not path.is_file():
        raise ValueError(f"No agency seed at {path}")
    try:
        seed = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path}: invalid JSON: {exc}") from None
    law.validate(seed, json.loads(SEED_SCHEMA.read_text(encoding="utf-8")))
    if seed["jurisdiction"] != jurisdiction:
        raise ValueError(f"{path}: jurisdiction {seed['jurisdiction']!r} != {jurisdiction!r}")
    return seed


def seed_state(seed):
    return seed["jurisdiction"].split("-")[1].upper()


def place_name_of(agency):
    """City or town name implied by a city-level agency's jurisdiction_name."""
    match = re.match(r"^(?:City|Town) of (.+)$", agency["jurisdiction_name"])
    return match.group(1) if match else None


def cities_in(county):
    """Ordered unique (place_name, place_fips) pairs for a seed county."""
    seen = {}
    for entry in county["agencies"]:
        if entry["kind"] in CITY_KINDS:
            name = place_name_of(entry)
            if name and name not in seen:
                seen[name] = entry["place_fips"]
    return list(seen.items())


def _candidates(seed):
    for county in seed["counties"]:
        yield ("county", normalize(county["name"]), county, None, None)
        for name, fips in cities_in(county):
            yield ("city", normalize(name), county, name, fips)


def _split_hint(query):
    text = normalize(query)
    hint = None
    if text.startswith("city of ") or text.startswith("town of "):
        hint, text = "city", text.split(" ", 2)[2]
    elif text.endswith(" county"):
        hint, text = "county", text[: -len(" county")]
    return hint, text


def _find(seed, hint, text):
    return [c for c in _candidates(seed) if c[1] == text and (hint is None or c[0] == hint)]


def locate(query, state, seed):
    """Resolve ``query`` (county or city name) against ``seed`` offline.

    "X County" or a bare county name resolves to the county; "City of X",
    "Town of X" or a bare city name resolves to the city. A bare name that is
    both a county and a city (for example "San Luis Obispo") raises
    ValueError listing the candidates so the organizer can disambiguate.
    """
    if not isinstance(query, str) or not query.strip():
        raise ValueError("Location query must be a non-empty string")
    state = str(state).upper()
    if state != seed_state(seed):
        raise ValueError(f"Seed covers {seed_state(seed)}, not {state}")
    hint, text = _split_hint(query)
    if not text:
        raise ValueError(f"Could not parse location query {query!r}")
    matches = _find(seed, hint, text)
    if not matches and hint is None and text.endswith(" city"):
        # "Morro Bay city" (Census style) but not "Yuba City", which matched above.
        matches = _find(seed, "city", text[: -len(" city")])
    if not matches:
        raise ValueError(f"No county or city named {query!r} in the {state} agency seed; "
                         "check spelling, add it to tools/build_agency_seed.py, or use --online")
    if len(matches) > 1:
        labels = sorted({f"{c[2]['name']} County" if c[0] == "county"
                         else f"City of {c[3]} ({c[2]['name']} County)" for c in matches})
        raise ValueError(f"Ambiguous location {query!r}; candidates: {labels}")
    kind, _text, county, place, fips = matches[0]
    return Location(county["county_fips"], county["name"], state, fips, place, kind, query)


def locate_online(query, state="CA", timeout=10, opener=None):
    """Census geocoder lookup (network). Returns a Location or raises ValueError.

    Uses the onelineaddress endpoint with returntype=geographies. The result
    carries county and incorporated-place FIPS only; agency suggestions still
    come from the offline seed via ``county_fips``.
    """
    params = urllib.parse.urlencode({
        "address": f"{query}, {state}", "benchmark": "Public_AR_Current",
        "vintage": "Current_Current", "format": "json", "layers": "Counties,Incorporated Places"})
    url = GEOCODER + "?" + params
    opener = opener or urllib.request.urlopen
    try:
        with opener(url, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"Census geocoder lookup failed: {exc}") from None
    matches = payload.get("result", {}).get("addressMatches") or []
    if not matches:
        raise ValueError(f"Census geocoder found no match for {query!r}")
    geographies = matches[0].get("geographies", {})
    counties = geographies.get("Counties") or []
    if not counties:
        raise ValueError("Census geocoder returned no county for " + repr(query))
    county = counties[0]
    places = geographies.get("Incorporated Places") or []
    place = places[0] if places else None
    county_name = re.sub(r" County$", "", county.get("NAME", "")) or county.get("BASENAME", "")
    place_name = place and (place.get("BASENAME") or re.sub(r" (city|town)$", "", place.get("NAME", "")))
    return Location(str(county.get("GEOID")), county_name, state.upper(),
                    str(place.get("GEOID")) if place else None, place_name or None,
                    "city" if place else "county", query)


def county_for(location, seed):
    for county in seed["counties"]:
        if county["county_fips"] == location.county_fips:
            return county
    raise ValueError(f"County FIPS {location.county_fips} is not in the agency seed")


def agencies_for(location, seed, include=DEFAULT_INCLUDE):
    """Suggested agencies for ``location`` from the seed, each with ``suggested: True``.

    Ordering: for a city match, that city's agencies first, then county-wide
    agencies; for a county match, county-wide agencies then every city's
    agencies in seed order. Within a group, ``include`` order is kept.
    """
    include = tuple(include)
    unknown = sorted(set(include) - set(DEFAULT_INCLUDE) - {"other"})
    if unknown:
        raise ValueError(f"Unknown agency kind(s) {unknown}; choose from {list(DEFAULT_INCLUDE)}")
    county = county_for(location, seed)
    rank = {kind: index for index, kind in enumerate(include)}

    def pick(entries):
        chosen = [e for e in entries if e["kind"] in rank]
        return sorted(chosen, key=lambda e: rank[e["kind"]])

    county_wide = [e for e in county["agencies"] if e["kind"] in COUNTY_KINDS]
    by_city = {}
    for entry in county["agencies"]:
        if entry["kind"] in CITY_KINDS:
            by_city.setdefault(place_name_of(entry), []).append(entry)
    ordered = []
    if location.match_kind == "city":
        if location.place_name not in by_city:
            raise ValueError(f"City {location.place_name!r} is not in the seed for "
                             f"{county['name']} County")
        ordered += pick(by_city[location.place_name])
        ordered += pick(county_wide)
    else:
        ordered += pick(county_wide)
        for name in by_city:
            ordered += pick(by_city[name])
    return [{**json.loads(json.dumps(entry)), "suggested": True} for entry in ordered]
