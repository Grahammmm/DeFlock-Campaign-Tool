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


# UC and CSU campus police (kind campus_police, opt-in only): (county_fips, system, campus, display name).
# Contributor knowledge, unverified; campus records offices differ from city/county ones.
_UC = ("Berkeley", "06001"), ("Davis", "06113"), ("Irvine", "06059"), ("Los Angeles", "06037"), \
    ("Merced", "06047"), ("Riverside", "06065"), ("San Diego", "06073"), ("San Francisco", "06075"), \
    ("Santa Barbara", "06083"), ("Santa Cruz", "06087")
_CSU = ("Bakersfield", "06029"), ("Channel Islands", "06111"), ("Chico", "06007"), ("Dominguez Hills", "06037"), \
    ("East Bay", "06001"), ("Fresno", "06019"), ("Fullerton", "06059"), ("Humboldt", "06023"), \
    ("Long Beach", "06037"), ("Los Angeles", "06037"), ("Maritime Academy", "06095"), ("Monterey Bay", "06053"), \
    ("Northridge", "06037"), ("Pomona", "06037"), ("Sacramento", "06067"), ("San Bernardino", "06071"), \
    ("San Diego", "06073"), ("San Francisco", "06075"), ("San José", "06085"), ("San Luis Obispo", "06079"), \
    ("San Marcos", "06073"), ("Sonoma", "06097"), ("Stanislaus", "06099")
CAL_POLY = {"Humboldt": "Cal Poly Humboldt", "Pomona": "Cal Poly Pomona", "San Luis Obispo": "Cal Poly"}
CAMPUSES = [(fips, "UC", campus, f"University of California, {campus} Police Department") for campus, fips in _UC] + [
    (fips, "CSU", campus, CAL_POLY.get(campus, "California State University, " + campus)
     + " University Police Department") for campus, fips in _CSU]
CAMPUS_NOTE = ("UC/CSU campus police department; seeded from contributor knowledge; "
               "submit to the campus records office, verify")

# California community college district police (kind ccd_police, opt-in only):
# (county_fips of the district office, district, display name). Only districts that operate their OWN
# police department (Education Code 72330) are listed; a district that contracts a city police or sheriff
# is not. Each row was read from an official district or member-college page naming the department; those
# page titles and URLs are recorded in the PR that added the row, not in the seed (url stays null).
# Districts the CCCCO directory (https://www.cccco.edu/students/find-a-college/community-college-districts/,
# 72 districts) lists but this table omits were not confirmed from an official source: add one with its page.
CCD_POLICE = [
    ("06083", "Allan Hancock Joint Community College District", "Allan Hancock Joint Community College District Police Department"),
    ("06007", "Butte-Glenn Community College District", "Butte-Glenn Community College District Police Department"),
    ("06037", "Cerritos Community College District", "Cerritos Community College District Police Department"),
    ("06071", "Chaffey Community College District", "Chaffey Community College District Police Department"),
    ("06037", "Compton Community College District", "Compton Community College District Police Department"),
    ("06013", "Contra Costa Community College District", "Contra Costa Community College District Police Department"),
    ("06037", "El Camino Community College District", "El Camino Community College District Police Department"),
    ("06085", "Foothill-De Anza Community College District", "Foothill-De Anza Community College District Police Department"),
    ("06037", "Glendale Community College District", "Glendale Community College District Police Department"),
    ("06067", "Los Rios Community College District", "Los Rios Community College District Police Department"),
    ("06041", "Marin Community College District", "Marin Community College District Police Department"),
    ("06047", "Merced Community College District", "Merced Community College District Police Department"),
    ("06073", "MiraCosta Community College District", "MiraCosta Community College District Police Department"),
    ("06037", "Mt. San Antonio Community College District", "Mt. San Antonio Community College District Police Department"),
    ("06055", "Napa Valley Community College District", "Napa Valley Community College District Police Department"),
    ("06001", "Ohlone Community College District", "Ohlone Community College District Police Department"),
    ("06073", "Palomar Community College District", "Palomar Community College District Police Department"),
    ("06037", "Pasadena Area Community College District", "Pasadena Area Community College District Police Department"),
    ("06037", "Rio Hondo Community College District", "Rio Hondo Community College District Police Department"),
    ("06065", "Riverside Community College District", "Riverside Community College District Police Department"),
    ("06071", "San Bernardino Community College District", "San Bernardino Community College District Police Department"),
    ("06073", "San Diego Community College District", "San Diego Community College District Police Department"),
    ("06075", "San Francisco Community College District", "San Francisco Community College District Police Department"),
    ("06077", "San Joaquin Delta Community College District", "San Joaquin Delta Community College District Police Department"),
    ("06085", "San Jose-Evergreen Community College District", "San Jose-Evergreen Community College District Police Department"),
    ("06079", "San Luis Obispo County Community College District", "San Luis Obispo County Community College District Police Department"),
    ("06037", "Santa Monica Community College District", "Santa Monica Community College District Police Department"),
    ("06107", "Sequoias Community College District", "Sequoias Community College District Police Department"),
    ("06095", "Solano County Community College District", "Solano County Community College District Department of Public Safety"),
    ("06097", "Sonoma County Junior College District", "Sonoma County Junior College District Police Department"),
    ("06059", "South Orange County Community College District", "South Orange County Community College District Campus Police Departments"),
    ("06073", "Southwestern Community College District", "Southwestern Community College District Police Department"),
    ("06019", "State Center Community College District", "State Center Community College District Police Department"),
    ("06111", "Ventura County Community College District", "Ventura County Community College District Police Department"),
    ("06071", "Victor Valley Community College District", "Victor Valley Community College District Police Department"),
    ("06085", "West Valley-Mission Community College District", "West Valley-Mission Community College District Police Department"),
]
CCD_NOTE = ("community college district police department; seeded from the district's official page; "
            "submit to the district records office, verify")

# California K-12 school district police (kind school_police, opt-in only):
# (county_fips of the district office, district, display name). Only districts that operate their OWN
# police department (Education Code 38000(b)) are listed; a district whose campuses are served by a city
# police department or the county sheriff through school resource officers is NOT listed, because the
# ALPR records sit with that agency. Each row was read from an official district or department page
# naming the department; those page titles and URLs are recorded in the PR that added the row, not in
# the seed (url stays null). Districts that POST or the press list but this table omits were not
# confirmed as a district police department from an official source: add one with its page.
SCHOOL_POLICE = [
    ("06071", "Apple Valley Unified School District", "Apple Valley Unified School District Police Department"),
    ("06019", "Clovis Unified School District", "Clovis Unified School District Police Department"),
    ("06037", "Compton Unified School District", "Compton Unified School District Police Department"),
    ("06037", "El Rancho Unified School District", "El Rancho Unified School District Police Department"),
    ("06071", "Fontana Unified School District", "Fontana Unified School District Police Department"),
    ("06037", "Hacienda La Puente Unified School District", "Hacienda La Puente Unified School District Police Department"),
    ("06071", "Hesperia Unified School District", "Hesperia Unified School District Police Department"),
    ("06037", "Inglewood Unified School District", "Inglewood Unified School District Police Department"),
    ("06029", "Kern High School District", "Kern High School District Police Department"),
    ("06037", "Los Angeles Unified School District", "Los Angeles School Police Department"),
    ("06037", "Montebello Unified School District", "Montebello Unified School District Police Department"),
    ("06071", "San Bernardino City Unified School District", "San Bernardino City Unified School District Police Department"),
    ("06073", "San Diego Unified School District", "San Diego Unified School District Police Department"),
    ("06059", "Santa Ana Unified School District", "Santa Ana Unified School District Police Department"),
    ("06071", "Snowline Joint Unified School District", "Snowline Joint Unified School District Police Department"),
    ("06077", "Stockton Unified School District", "Stockton Unified School District Police Department"),
    ("06067", "Twin Rivers Unified School District", "Twin Rivers Unified School District Police Department"),
    ("06065", "Val Verde Unified School District", "Val Verde Unified School District Police Department"),
    ("06071", "Victor Valley Union High School District", "Victor Valley Union High School District Police Department"),
]
SCHOOL_NOTE = ("school district police department; seeded from the district's official page; "
               "submit to the district records office, verify")

# California special-district police departments (kind district_police, opt-in only):
# (county_fips served, district, display name, district_type) with district_type one of DISTRICT_TYPES.
# Only a special district or joint powers authority that employs its OWN sworn police department
# (Penal Code 830.33 and similar) is listed; a department that contracts with a sheriff or city police
# (for example SacRT, VTA, Oxnard Harbor District) is not, and neither is a harbor, airport or
# port department that is a city department (Los Angeles, Long Beach, Oakland, San Francisco, San Jose).
# A district serving several counties has ONE ROW PER COUNTY (same district and name), so a county kit
# finds it. Each row was read from an official district page; those page titles and URLs are recorded
# in the PR that added the row, not in the seed (url stays null). Districts this table omits were not
# confirmed from an official source: add one with its page.
DISTRICT_TYPES = ("transit", "port_harbor", "airport", "park")
DISTRICT_POLICE = [
    ("06001", "San Francisco Bay Area Rapid Transit District", "San Francisco Bay Area Rapid Transit District Police Department", "transit"),
    ("06013", "San Francisco Bay Area Rapid Transit District", "San Francisco Bay Area Rapid Transit District Police Department", "transit"),
    ("06075", "San Francisco Bay Area Rapid Transit District", "San Francisco Bay Area Rapid Transit District Police Department", "transit"),
    ("06081", "San Francisco Bay Area Rapid Transit District", "San Francisco Bay Area Rapid Transit District Police Department", "transit"),
    ("06085", "San Francisco Bay Area Rapid Transit District", "San Francisco Bay Area Rapid Transit District Police Department", "transit"),
    ("06001", "East Bay Regional Park District", "East Bay Regional Park District Police Department", "park"),
    ("06013", "East Bay Regional Park District", "East Bay Regional Park District Police Department", "park"),
    ("06037", "Burbank-Glendale-Pasadena Airport Authority", "Burbank-Glendale-Pasadena Airport Authority Police Department", "airport"),
    ("06067", "Fulton-El Camino Recreation and Park District", "Fulton-El Camino Park Police Department", "park"),
    ("06073", "San Diego Unified Port District", "San Diego Unified Port District Harbor Police Department", "port_harbor"),
    ("06077", "Stockton Port District", "Port of Stockton Police Department", "port_harbor"),
]
DISTRICT_NOTE = ("special-district police department ({type}); serves {county} County; seeded from the "
                 "district's official page; submit to the district records office, verify")


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


def campus_agencies(fips, slug):
    """Campus police for county ``fips`` in CAMPUSES order (``slug`` is the county slug, kept for symmetry)."""
    return [agency(f"{STATE}-{slugify(system + ' ' + campus)}-campus-police", name, "campus_police",
                   f"{system} ({campus})", sources=[source(CAMPUS_NOTE)])
            for row_fips, system, campus, name in CAMPUSES if row_fips == fips]


def ccd_agencies(fips):
    """Community college district police for county ``fips`` in CCD_POLICE order."""
    return [agency(f"{STATE}-{slugify(district)}-ccd-police", name, "ccd_police", district,
                   sources=[source(CCD_NOTE)])
            for row_fips, district, name in CCD_POLICE if row_fips == fips]


def school_agencies(fips):
    """School district police for county ``fips`` in SCHOOL_POLICE order."""
    return [agency(f"{STATE}-{slugify(district)}-school-police", name, "school_police", district,
                   sources=[source(SCHOOL_NOTE)])
            for row_fips, district, name in SCHOOL_POLICE if row_fips == fips]


def district_agencies(fips, reserved=()):
    """Special-district police for county ``fips`` in DISTRICT_POLICE order.

    A district with rows in several counties gets one entry per county, id ``<slug>-police-<county slug>``.
    A single-county district gets ``<slug>-police``, or the ``-<county slug>`` form when that id is in
    ``reserved`` (an id some other entry already uses, such as a city police department).
    """
    county_names = {row[0]: row[1] for row in COUNTIES}
    rows_per_district = {}
    for _fips, district, _name, _type in DISTRICT_POLICE:
        rows_per_district[district] = rows_per_district.get(district, 0) + 1
    entries = []
    for row_fips, district, name, district_type in DISTRICT_POLICE:
        if row_fips != fips:
            continue
        county = county_names[row_fips]
        agency_id = f"{STATE}-{slugify(district)}-police"
        if rows_per_district[district] > 1 or agency_id in reserved:
            agency_id += "-" + slugify(county)
        note = DISTRICT_NOTE.format(type=district_type, county=county)
        entries.append(agency(agency_id, name, "district_police", district, sources=[source(note)]))
    return entries


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
    reserved = set()  # ids of every non-district entry, so a district id never repeats one
    for fips, name, _seat, cities in COUNTIES:
        slug = slugify(name)
        for entry in (county_agencies(fips, name, slug) + campus_agencies(fips, slug) + ccd_agencies(fips)
                      + school_agencies(fips)
                      + [e for city, policing in cities for e in city_agencies(city, policing, overlay)]):
            reserved.add(entry["agency_id"])
    counties = []
    for fips, name, seat, cities in COUNTIES:
        slug = slugify(name)
        agencies = county_agencies(fips, name, slug)
        agencies.extend(campus_agencies(fips, slug))
        agencies.extend(ccd_agencies(fips))
        agencies.extend(school_agencies(fips))
        agencies.extend(district_agencies(fips, reserved))
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
    campuses = [(system, campus) for _fips, system, campus, _name in CAMPUSES]
    if len(set(campuses)) != len(campuses):
        raise ValueError("duplicate campus row")
    for fips, system, campus, _name in CAMPUSES:
        if fips not in fips_seen:
            raise ValueError(f"campus {system} {campus}: {fips} is not a county fips")
    districts = [district for _fips, district, _name in CCD_POLICE]
    if len(set(districts)) != len(districts):
        raise ValueError("duplicate ccd district row")
    for fips, district, _name in CCD_POLICE:
        if fips not in fips_seen:
            raise ValueError(f"ccd district {district}: {fips} is not a county fips")
    schools = [district for _fips, district, _name in SCHOOL_POLICE]
    if len(set(schools)) != len(schools):
        raise ValueError("duplicate school district row")
    for fips, district, _name in SCHOOL_POLICE:
        if fips not in fips_seen:
            raise ValueError(f"school district {district}: {fips} is not a county fips")
    district_rows = [(fips, district) for fips, district, _name, _type in DISTRICT_POLICE]
    if len(set(district_rows)) != len(district_rows):
        raise ValueError("duplicate special district row")
    for fips, district, _name, district_type in DISTRICT_POLICE:
        if fips not in fips_seen:
            raise ValueError(f"special district {district}: {fips} is not a county fips")
        if district_type not in DISTRICT_TYPES:
            raise ValueError(f"special district {district}: unknown district type {district_type!r}")
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
