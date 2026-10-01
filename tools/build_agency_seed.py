"""Emit data/agencies/us-ca.json deterministically from the tables in this file.

Contributors edit the Python tables below, never the generated JSON. Run:

    python3 tools/build_agency_seed.py            # rewrite data/agencies/us-ca.json
    python3 tools/build_agency_seed.py --check    # exit 1 if the JSON is stale

Every entry is unverified contributor knowledge (``verified: false``). The
optional overlay ``data/agencies/us-ca.place-fips.json`` (written by
tools/refresh_agency_seed_online.py from the Census Gazetteer) supplies
``place_fips`` for cities; without it ``place_fips`` stays null. Nothing here
claims that any agency uses ALPR, and no records URL or email is stated unless
a contributor could state it with confidence (currently none are).

City policing values:
    "pd"       the city operates its own police department
    "sheriff"  the city contracts with the county sheriff for policing
    ("contract", "<agency name>")  the city contracts with another agency
    None       arrangement not recorded; organizer must verify
"""
import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT = REPO_ROOT / "data" / "agencies" / "us-ca.json"
OVERLAY = REPO_ROOT / "data" / "agencies" / "us-ca.place-fips.json"
GENERATED = "2026-09-30"
JURISDICTION = "us-ca"
STATE = "ca"

# Incorporated towns (as opposed to cities) whose council is a Town Council.
TOWNS = {
    "Apple Valley", "Atherton", "Colma", "Corte Madera", "Danville", "Fairfax",
    "Hillsborough", "Loomis", "Los Altos Hills", "Los Gatos", "Mammoth Lakes",
    "Moraga", "Paradise", "Portola Valley", "Ross", "San Anselmo", "Tiburon",
    "Truckee", "Windsor", "Woodside", "Yountville", "Yucca Valley", "Discovery Bay",
}

# Police department names that are not "<City> Police Department".
POLICE_NAMES = {
    "Sunnyvale": "Sunnyvale Department of Public Safety",
    "Rohnert Park": "Rohnert Park Department of Public Safety",
    "Los Gatos": "Los Gatos-Monte Sereno Police Department",
}

# Sheriff names that are not "<County> County Sheriff's Office".
SHERIFF_NAMES = {
    "Los Angeles": "Los Angeles County Sheriff's Department",
    "San Francisco": "San Francisco Sheriff's Office",
}
BOARD_NAMES = {"San Francisco": "San Francisco Board of Supervisors"}
COUNTY_JURISDICTION = {"San Francisco": "City and County of San Francisco"}

PD, SO = "pd", "sheriff"

# (county_fips, county name, county seat, [(city, policing), ...])
COUNTIES = [
    ("06001", "Alameda", "Oakland", [
        ("Alameda", PD), ("Albany", PD), ("Berkeley", PD), ("Dublin", SO),
        ("Emeryville", PD), ("Fremont", PD), ("Hayward", PD), ("Livermore", PD),
        ("Newark", PD), ("Oakland", PD), ("Piedmont", PD), ("Pleasanton", PD),
        ("San Leandro", PD), ("Union City", PD),
    ]),
    ("06003", "Alpine", "Markleeville", []),
    ("06005", "Amador", "Jackson", [
        ("Amador City", SO), ("Ione", PD), ("Jackson", PD), ("Plymouth", SO),
        ("Sutter Creek", PD),
    ]),
    ("06007", "Butte", "Oroville", [
        ("Biggs", SO), ("Chico", PD), ("Gridley", PD), ("Oroville", PD), ("Paradise", PD),
    ]),
    ("06009", "Calaveras", "San Andreas", [("Angels Camp", PD)]),
    ("06011", "Colusa", "Colusa", [("Colusa", PD), ("Williams", PD)]),
    ("06013", "Contra Costa", "Martinez", [
        ("Antioch", PD), ("Brentwood", PD), ("Clayton", PD), ("Concord", PD),
        ("Danville", SO), ("El Cerrito", PD), ("Hercules", PD), ("Lafayette", SO),
        ("Martinez", PD), ("Moraga", PD), ("Oakley", PD), ("Orinda", SO),
        ("Pinole", PD), ("Pittsburg", PD), ("Pleasant Hill", PD), ("Richmond", PD),
        ("San Pablo", PD), ("San Ramon", PD), ("Walnut Creek", PD),
    ]),
    ("06015", "Del Norte", "Crescent City", [("Crescent City", PD)]),
    ("06017", "El Dorado", "Placerville", [("Placerville", PD), ("South Lake Tahoe", PD)]),
    ("06019", "Fresno", "Fresno", [
        ("Clovis", PD), ("Coalinga", PD), ("Firebaugh", PD), ("Fowler", PD),
        ("Fresno", PD), ("Huron", PD), ("Kerman", PD), ("Kingsburg", PD),
        ("Mendota", PD), ("Orange Cove", PD), ("Parlier", PD), ("Reedley", PD),
        ("San Joaquin", PD), ("Sanger", PD), ("Selma", PD),
    ]),
    ("06021", "Glenn", "Willows", [("Orland", PD), ("Willows", PD)]),
    ("06023", "Humboldt", "Eureka", [
        ("Arcata", PD), ("Blue Lake", SO), ("Eureka", PD), ("Ferndale", PD),
        ("Fortuna", PD), ("Rio Dell", PD), ("Trinidad", SO),
    ]),
    ("06025", "Imperial", "El Centro", [
        ("Brawley", PD), ("Calexico", PD), ("Calipatria", PD), ("El Centro", PD),
        ("Holtville", PD), ("Imperial", PD), ("Westmorland", PD),
    ]),
    ("06027", "Inyo", "Independence", [("Bishop", PD)]),
    ("06029", "Kern", "Bakersfield", [
        ("Arvin", PD), ("Bakersfield", PD), ("California City", PD), ("Delano", PD),
        ("Maricopa", SO), ("McFarland", PD), ("Ridgecrest", PD), ("Shafter", PD),
        ("Taft", PD), ("Tehachapi", PD), ("Wasco", SO),
    ]),
    ("06031", "Kings", "Hanford", [
        ("Avenal", PD), ("Corcoran", PD), ("Hanford", PD), ("Lemoore", PD),
    ]),
    ("06033", "Lake", "Lakeport", [("Clearlake", PD), ("Lakeport", PD)]),
    ("06035", "Lassen", "Susanville", [("Susanville", PD)]),
    ("06037", "Los Angeles", "Los Angeles", [
        ("Agoura Hills", SO), ("Alhambra", PD), ("Arcadia", PD), ("Artesia", SO),
        ("Avalon", SO), ("Azusa", PD), ("Baldwin Park", PD), ("Bell", PD),
        ("Bell Gardens", PD), ("Bellflower", SO), ("Beverly Hills", PD),
        ("Bradbury", SO), ("Burbank", PD), ("Calabasas", SO), ("Carson", SO),
        ("Cerritos", SO), ("Claremont", PD), ("Commerce", SO), ("Compton", SO),
        ("Covina", PD), ("Cudahy", SO), ("Culver City", PD), ("Diamond Bar", SO),
        ("Downey", PD), ("Duarte", SO), ("El Monte", PD), ("El Segundo", PD),
        ("Gardena", PD), ("Glendale", PD), ("Glendora", PD), ("Hawaiian Gardens", SO),
        ("Hawthorne", PD), ("Hermosa Beach", PD), ("Hidden Hills", SO),
        ("Huntington Park", PD), ("Industry", SO), ("Inglewood", PD),
        ("Irwindale", PD), ("La Cañada Flintridge", SO), ("La Habra Heights", SO),
        ("La Mirada", SO), ("La Puente", SO), ("La Verne", PD), ("Lakewood", SO),
        ("Lancaster", SO), ("Lawndale", SO), ("Lomita", SO), ("Long Beach", PD),
        ("Los Angeles", PD), ("Lynwood", SO), ("Malibu", SO), ("Manhattan Beach", PD),
        ("Maywood", SO), ("Monrovia", PD), ("Montebello", PD), ("Monterey Park", PD),
        ("Norwalk", SO), ("Palmdale", SO), ("Palos Verdes Estates", PD),
        ("Paramount", SO), ("Pasadena", PD), ("Pico Rivera", SO), ("Pomona", PD),
        ("Rancho Palos Verdes", SO), ("Redondo Beach", PD), ("Rolling Hills", SO),
        ("Rolling Hills Estates", SO), ("Rosemead", SO), ("San Dimas", SO),
        ("San Fernando", PD), ("San Gabriel", PD), ("San Marino", PD),
        ("Santa Clarita", SO), ("Santa Fe Springs", ("contract", "Whittier Police Department")),
        ("Santa Monica", PD), ("Sierra Madre", PD), ("Signal Hill", PD),
        ("South El Monte", SO), ("South Gate", PD), ("South Pasadena", PD),
        ("Temple City", SO), ("Torrance", PD), ("Vernon", PD), ("Walnut", SO),
        ("West Covina", PD), ("West Hollywood", SO), ("Westlake Village", SO),
        ("Whittier", PD),
    ]),
    ("06039", "Madera", "Madera", [("Chowchilla", PD), ("Madera", PD)]),
    ("06041", "Marin", "San Rafael", [
        ("Belvedere", PD), ("Corte Madera", ("contract", "Central Marin Police Authority")),
        ("Fairfax", PD), ("Larkspur", ("contract", "Central Marin Police Authority")),
        ("Mill Valley", PD), ("Novato", PD), ("Ross", PD),
        ("San Anselmo", ("contract", "Central Marin Police Authority")),
        ("San Rafael", PD), ("Sausalito", PD), ("Tiburon", PD),
    ]),
    ("06043", "Mariposa", "Mariposa", []),
    ("06045", "Mendocino", "Ukiah", [
        ("Fort Bragg", PD), ("Point Arena", SO), ("Ukiah", PD), ("Willits", PD),
    ]),
    ("06047", "Merced", "Merced", [
        ("Atwater", PD), ("Dos Palos", PD), ("Gustine", PD), ("Livingston", PD),
        ("Los Banos", PD), ("Merced", PD),
    ]),
    ("06049", "Modoc", "Alturas", [("Alturas", PD)]),
    ("06051", "Mono", "Bridgeport", [("Mammoth Lakes", PD)]),
    ("06053", "Monterey", "Salinas", [
        ("Carmel-by-the-Sea", PD), ("Del Rey Oaks", PD), ("Gonzales", PD),
        ("Greenfield", PD), ("King City", PD), ("Marina", PD), ("Monterey", PD),
        ("Pacific Grove", PD), ("Salinas", PD), ("Sand City", PD), ("Seaside", PD),
        ("Soledad", PD),
    ]),
    ("06055", "Napa", "Napa", [
        ("American Canyon", SO), ("Calistoga", PD), ("Napa", PD), ("St. Helena", PD),
        ("Yountville", SO),
    ]),
    ("06057", "Nevada", "Nevada City", [
        ("Grass Valley", PD), ("Nevada City", PD), ("Truckee", PD),
    ]),
    ("06059", "Orange", "Santa Ana", [
        ("Aliso Viejo", SO), ("Anaheim", PD), ("Brea", PD), ("Buena Park", PD),
        ("Costa Mesa", PD), ("Cypress", PD), ("Dana Point", SO), ("Fountain Valley", PD),
        ("Fullerton", PD), ("Garden Grove", PD), ("Huntington Beach", PD), ("Irvine", PD),
        ("La Habra", PD), ("La Palma", PD), ("Laguna Beach", PD), ("Laguna Hills", SO),
        ("Laguna Niguel", SO), ("Laguna Woods", SO), ("Lake Forest", SO),
        ("Los Alamitos", PD), ("Mission Viejo", SO), ("Newport Beach", PD),
        ("Orange", PD), ("Placentia", PD), ("Rancho Santa Margarita", SO),
        ("San Clemente", SO), ("San Juan Capistrano", SO), ("Santa Ana", PD),
        ("Seal Beach", PD), ("Stanton", SO), ("Tustin", PD), ("Villa Park", SO),
        ("Westminster", PD), ("Yorba Linda", SO),
    ]),
    ("06061", "Placer", "Auburn", [
        ("Auburn", PD), ("Colfax", SO), ("Lincoln", PD), ("Loomis", SO),
        ("Rocklin", PD), ("Roseville", PD),
    ]),
    ("06063", "Plumas", "Quincy", [("Portola", SO)]),
    ("06065", "Riverside", "Riverside", [
        ("Banning", PD), ("Beaumont", PD), ("Blythe", PD), ("Calimesa", SO),
        ("Canyon Lake", SO), ("Cathedral City", PD), ("Coachella", SO), ("Corona", PD),
        ("Desert Hot Springs", PD), ("Eastvale", SO), ("Hemet", PD), ("Indian Wells", SO),
        ("Indio", PD), ("Jurupa Valley", SO), ("La Quinta", SO), ("Lake Elsinore", SO),
        ("Menifee", PD), ("Moreno Valley", SO), ("Murrieta", PD), ("Norco", SO),
        ("Palm Desert", SO), ("Palm Springs", PD), ("Perris", SO), ("Rancho Mirage", SO),
        ("Riverside", PD), ("San Jacinto", SO), ("Temecula", SO), ("Wildomar", SO),
    ]),
    ("06067", "Sacramento", "Sacramento", [
        ("Citrus Heights", PD), ("Elk Grove", PD), ("Folsom", PD), ("Galt", PD),
        ("Isleton", SO), ("Rancho Cordova", SO), ("Sacramento", PD),
    ]),
    ("06069", "San Benito", "Hollister", [("Hollister", PD), ("San Juan Bautista", SO)]),
    ("06071", "San Bernardino", "San Bernardino", [
        ("Adelanto", SO), ("Apple Valley", SO), ("Barstow", PD), ("Big Bear Lake", SO),
        ("Chino", PD), ("Chino Hills", SO), ("Colton", PD), ("Fontana", PD),
        ("Grand Terrace", SO), ("Hesperia", SO), ("Highland", SO), ("Loma Linda", SO),
        ("Montclair", PD), ("Needles", SO), ("Ontario", PD), ("Rancho Cucamonga", SO),
        ("Redlands", PD), ("Rialto", PD), ("San Bernardino", PD), ("Twentynine Palms", SO),
        ("Upland", PD), ("Victorville", SO), ("Yucaipa", SO), ("Yucca Valley", SO),
    ]),
    ("06073", "San Diego", "San Diego", [
        ("Carlsbad", PD), ("Chula Vista", PD), ("Coronado", PD), ("Del Mar", SO),
        ("El Cajon", PD), ("Encinitas", SO), ("Escondido", PD), ("Imperial Beach", SO),
        ("La Mesa", PD), ("Lemon Grove", SO), ("National City", PD), ("Oceanside", PD),
        ("Poway", SO), ("San Diego", PD), ("San Marcos", SO), ("Santee", SO),
        ("Solana Beach", SO), ("Vista", SO),
    ]),
    ("06075", "San Francisco", "San Francisco", [("San Francisco", PD)]),
    ("06077", "San Joaquin", "Stockton", [
        ("Escalon", PD), ("Lathrop", SO), ("Lodi", PD), ("Manteca", PD), ("Ripon", PD),
        ("Stockton", PD), ("Tracy", PD),
    ]),
    ("06079", "San Luis Obispo", "San Luis Obispo", [
        ("Arroyo Grande", PD), ("Atascadero", PD), ("Grover Beach", PD),
        ("Morro Bay", PD), ("Paso Robles", PD), ("Pismo Beach", PD),
        ("San Luis Obispo", PD),
    ]),
    ("06081", "San Mateo", "Redwood City", [
        ("Atherton", PD), ("Belmont", PD), ("Brisbane", PD), ("Burlingame", PD),
        ("Colma", PD), ("Daly City", PD), ("East Palo Alto", PD), ("Foster City", PD),
        ("Half Moon Bay", SO), ("Hillsborough", PD), ("Menlo Park", PD), ("Millbrae", SO),
        ("Pacifica", PD), ("Portola Valley", SO), ("Redwood City", PD), ("San Bruno", PD),
        ("San Carlos", SO), ("San Mateo", PD), ("South San Francisco", PD), ("Woodside", SO),
    ]),
    ("06083", "Santa Barbara", "Santa Barbara", [
        ("Buellton", SO), ("Carpinteria", SO), ("Goleta", SO), ("Guadalupe", PD),
        ("Lompoc", PD), ("Santa Barbara", PD), ("Santa Maria", PD), ("Solvang", SO),
    ]),
    ("06085", "Santa Clara", "San Jose", [
        ("Campbell", PD), ("Cupertino", SO), ("Gilroy", PD), ("Los Altos", PD),
        ("Los Altos Hills", SO), ("Los Gatos", PD), ("Milpitas", PD),
        ("Monte Sereno", ("contract", "Los Gatos-Monte Sereno Police Department")),
        ("Morgan Hill", PD), ("Mountain View", PD), ("Palo Alto", PD), ("San Jose", PD),
        ("Santa Clara", PD), ("Saratoga", SO), ("Sunnyvale", PD),
    ]),
    ("06087", "Santa Cruz", "Santa Cruz", [
        ("Capitola", PD), ("Santa Cruz", PD), ("Scotts Valley", PD), ("Watsonville", PD),
    ]),
    ("06089", "Shasta", "Redding", [("Anderson", PD), ("Redding", PD), ("Shasta Lake", SO)]),
    ("06091", "Sierra", "Downieville", [("Loyalton", SO)]),
    ("06093", "Siskiyou", "Yreka", [
        ("Dorris", None), ("Dunsmuir", None), ("Etna", None), ("Fort Jones", None),
        ("Montague", None), ("Mount Shasta", PD), ("Tulelake", None), ("Weed", PD),
        ("Yreka", PD),
    ]),
    ("06095", "Solano", "Fairfield", [
        ("Benicia", PD), ("Dixon", PD), ("Fairfield", PD), ("Rio Vista", PD),
        ("Suisun City", PD), ("Vacaville", PD), ("Vallejo", PD),
    ]),
    ("06097", "Sonoma", "Santa Rosa", [
        ("Cloverdale", PD), ("Cotati", PD), ("Healdsburg", PD), ("Petaluma", PD),
        ("Rohnert Park", PD), ("Santa Rosa", PD), ("Sebastopol", PD), ("Sonoma", SO),
        ("Windsor", SO),
    ]),
    ("06099", "Stanislaus", "Modesto", [
        ("Ceres", PD), ("Hughson", SO), ("Modesto", PD), ("Newman", PD), ("Oakdale", PD),
        ("Patterson", SO), ("Riverbank", SO), ("Turlock", PD), ("Waterford", SO),
    ]),
    ("06101", "Sutter", "Yuba City", [("Live Oak", SO), ("Yuba City", PD)]),
    ("06103", "Tehama", "Red Bluff", [("Corning", PD), ("Red Bluff", PD), ("Tehama", SO)]),
    ("06105", "Trinity", "Weaverville", []),
    ("06107", "Tulare", "Visalia", [
        ("Dinuba", PD), ("Exeter", PD), ("Farmersville", PD), ("Lindsay", PD),
        ("Porterville", PD), ("Tulare", PD), ("Visalia", PD), ("Woodlake", PD),
    ]),
    ("06109", "Tuolumne", "Sonora", [("Sonora", PD)]),
    ("06111", "Ventura", "Ventura", [
        ("Camarillo", SO), ("Fillmore", SO), ("Moorpark", SO), ("Ojai", SO),
        ("Oxnard", PD), ("Port Hueneme", PD), ("Santa Paula", PD), ("Simi Valley", PD),
        ("Thousand Oaks", SO), ("Ventura", PD),
    ]),
    ("06113", "Yolo", "Woodland", [
        ("Davis", PD), ("West Sacramento", PD), ("Winters", PD), ("Woodland", PD),
    ]),
    ("06115", "Yuba", "Marysville", [("Marysville", PD), ("Wheatland", PD)]),
]

# Counties whose city list is believed complete (every incorporated city listed).
COMPLETE_COUNTIES = {
    "06001", "06003", "06019", "06029", "06037", "06043", "06053", "06067", "06073",
    "06075", "06079", "06083", "06087", "06105", "06111",
}


def slugify(text):
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return re.sub(r"-{2,}", "-", text)


def source(title):
    return {"title": title, "url": None, "accessed": GENERATED}


def agency(agency_id, name, kind, jurisdiction_name, place_fips=None, sources=()):
    return {
        "agency_id": agency_id, "name": name, "kind": kind,
        "jurisdiction_name": jurisdiction_name, "place_fips": place_fips,
        "records_url": None, "records_email": None,
        "portal": {"vendor": "unknown", "url": None},
        "flock_transparency_slug": None, "muckrock_agency_id": None,
        "verified": False, "sources": list(sources),
    }


SEED_NOTE = "seeded from contributor knowledge; verify on the official agency page"


def county_agencies(fips, name, slug):
    county_name = COUNTY_JURISDICTION.get(name, name + " County")
    return [
        agency(f"{STATE}-{slug}-county-sheriff",
               SHERIFF_NAMES.get(name, name + " County Sheriff's Office"),
               "sheriff", county_name, sources=[source(SEED_NOTE)]),
        agency(f"{STATE}-{slug}-county-board-of-supervisors",
               BOARD_NAMES.get(name, name + " County Board of Supervisors"),
               "county_board", county_name, sources=[source(SEED_NOTE)]),
        agency(f"{STATE}-{slug}-county-district-attorney",
               name + " County District Attorney", "district_attorney", county_name,
               sources=[source(SEED_NOTE)]),
        agency(f"{STATE}-{slug}-county-chp",
               "California Highway Patrol (public records, headquarters) - " + name + " County",
               "chp", "California Highway Patrol",
               sources=[source("statewide agency; one entry per county so county kits include it; "
                               "submit to CHP headquarters public records, verify")]),
    ]


def city_agencies(city, policing, overlay):
    slug = slugify(city)
    body = "Town" if city in TOWNS else "City"
    jurisdiction_name = f"{body} of {city}"
    place_fips = overlay.get(city)
    entries = []
    if policing == PD:
        entries.append(agency(f"{STATE}-{slug}-police",
                              POLICE_NAMES.get(city, city + " Police Department"),
                              "police", jurisdiction_name, place_fips,
                              sources=[source(SEED_NOTE)]))
        council_note = SEED_NOTE
    elif policing == SO:
        council_note = "contracts with county sheriff for policing; verify"
    elif isinstance(policing, tuple) and policing[0] == "contract":
        council_note = f"contracts with {policing[1]} for policing; verify"
    elif policing is None:
        council_note = "policing arrangement not recorded in seed; verify whether the city has its own police department"
    else:
        raise ValueError(f"Unknown policing value for {city}: {policing!r}")
    entries.append(agency(f"{STATE}-{slug}-{body.lower()}-council", f"{city} {body} Council",
                          "city_council", jurisdiction_name, place_fips,
                          sources=[source(council_note)]))
    return entries


def build(overlay=None):
    overlay = overlay or {}
    counties = []
    for fips, name, seat, cities in COUNTIES:
        slug = slugify(name)
        agencies = county_agencies(fips, name, slug)
        for city, policing in cities:
            agencies.extend(city_agencies(city, policing, overlay))
        counties.append({"county_fips": fips, "name": name, "seat": seat, "agencies": agencies})
    return {"schema_version": 1, "jurisdiction": JURISDICTION, "generated": GENERATED,
            "counties": counties}


def check_tables():
    fips_seen = set()
    ids = set()
    expected = {f"06{n:03d}" for n in range(1, 116, 2)}
    for fips, name, _seat, cities in COUNTIES:
        if fips in fips_seen:
            raise ValueError("duplicate county fips " + fips)
        fips_seen.add(fips)
        names = [city for city, _ in cities]
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate city in {name}")
    if fips_seen != expected:
        raise ValueError(f"county fips mismatch: {sorted(expected ^ fips_seen)}")
    for county in build()["counties"]:
        for entry in county["agencies"]:
            if entry["agency_id"] in ids:
                raise ValueError("duplicate agency_id " + entry["agency_id"])
            ids.add(entry["agency_id"])


def render(document):
    return json.dumps(document, indent=2, ensure_ascii=False, sort_keys=False) + "\n"


def load_overlay(path=OVERLAY):
    if not Path(path).is_file():
        return {}
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return {k: v for k, v in data.get("places", {}).items() if isinstance(v, str)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="Exit 1 if the JSON is stale")
    parser.add_argument("--output", default=str(OUTPUT))
    args = parser.parse_args(argv)
    check_tables()
    text = render(build(load_overlay()))
    output = Path(args.output)
    if args.check:
        if not output.is_file() or output.read_text(encoding="utf-8") != text:
            print(f"{output} is stale; run python3 tools/build_agency_seed.py", file=sys.stderr)
            return 1
        print(f"{output} is current")
        return 0
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")
    counties = len(COUNTIES)
    cities = sum(len(c[3]) for c in COUNTIES)
    print(f"Wrote {output}: {counties} counties, {cities} cities")
    return 0


if __name__ == "__main__":
    sys.exit(main())
