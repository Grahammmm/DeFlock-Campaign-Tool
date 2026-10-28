"""Offline tests for campaign_tool.discovery and the California agency seed.

No network. The seed is contributor knowledge with verified: false throughout;
these tests check structure and resolution, not that any contact is correct.
"""
import json
import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tools"))
from campaign_tool import discovery, law  # noqa: E402
import build_agency_seed  # noqa: E402

SEED_PATH = REPO_ROOT / "data" / "agencies" / "us-ca.json"
AGENCY_ID = re.compile(r"^[a-z]{2}-[a-z0-9-]+$")


class SeedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.seed = discovery.load_seed("us-ca")

    def test_seed_validates_against_schema(self):
        schema = json.loads((REPO_ROOT / "schemas" / "agency-seed.schema.json").read_text())
        law.validate(self.seed, schema)
        self.assertEqual(self.seed["jurisdiction"], "us-ca")

    def test_seed_is_generated_from_the_script(self):
        build_agency_seed.check_tables()
        expected = build_agency_seed.render(build_agency_seed.build(build_agency_seed.load_overlay()))
        self.assertEqual(SEED_PATH.read_text(encoding="utf-8"), expected,
                         "run python3 tools/build_agency_seed.py")

    def test_all_58_counties_with_required_agencies(self):
        fips = sorted(c["county_fips"] for c in self.seed["counties"])
        self.assertEqual(fips, [f"06{n:03d}" for n in range(1, 116, 2)])
        for county in self.seed["counties"]:
            kinds = {a["kind"] for a in county["agencies"]}
            with self.subTest(county=county["name"]):
                self.assertTrue({"sheriff", "county_board", "district_attorney", "chp"} <= kinds)
                self.assertTrue(county["seat"])
                chp = [a for a in county["agencies"] if a["kind"] == "chp"]
                self.assertEqual(len(chp), 1)
                self.assertEqual(chp[0]["jurisdiction_name"], "California Highway Patrol")

    def test_agency_ids_unique_and_well_formed(self):
        ids = [a["agency_id"] for c in self.seed["counties"] for a in c["agencies"]]
        self.assertEqual(len(ids), len(set(ids)))
        for agency_id in ids:
            self.assertRegex(agency_id, AGENCY_ID)
            self.assertTrue(agency_id.startswith("ca-"))

    def test_nothing_is_marked_verified_and_no_alpr_claims(self):
        for county in self.seed["counties"]:
            for agency in county["agencies"]:
                self.assertIs(agency["verified"], False, agency["agency_id"])
                self.assertIsNone(agency["flock_transparency_slug"])
                for source in agency["sources"]:
                    self.assertNotRegex(source["title"].lower(), r"\buses? alpr\b")

    def test_fully_enumerated_counties_have_expected_city_counts(self):
        expected = {"San Luis Obispo": 7, "Santa Barbara": 8, "Monterey": 12, "Santa Cruz": 4,
                    "Ventura": 10, "Kern": 11, "Fresno": 15, "Sacramento": 7, "San Diego": 18,
                    "Alameda": 14, "Los Angeles": 88}
        by_name = {c["name"]: c for c in self.seed["counties"]}
        for name, count in expected.items():
            with self.subTest(county=name):
                self.assertEqual(len(discovery.cities_in(by_name[name])), count)

    def test_contract_cities_have_council_but_no_police_entry(self):
        by_name = {c["name"]: c for c in self.seed["counties"]}
        kinds = {a["jurisdiction_name"]: a["kind"] for a in by_name["Ventura"]["agencies"]
                 if a["kind"] == "police"}
        self.assertNotIn("City of Camarillo", kinds)
        self.assertIn("City of Oxnard", kinds)
        council = next(a for a in by_name["Ventura"]["agencies"]
                       if a["agency_id"] == "ca-camarillo-city-council")
        self.assertIn("contracts with county sheriff", council["sources"][0]["title"])


class LocateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.seed = discovery.load_seed("us-ca")

    def test_locate_county(self):
        loc = discovery.locate("San Luis Obispo County", "CA", self.seed)
        self.assertEqual((loc.county_fips, loc.match_kind, loc.place_name), ("06079", "county", None))
        self.assertEqual(discovery.locate("  kern county ", "ca", self.seed).county_fips, "06029")

    def test_locate_city(self):
        loc = discovery.locate("Morro Bay", "CA", self.seed)
        self.assertEqual((loc.county_fips, loc.match_kind, loc.place_name),
                         ("06079", "city", "Morro Bay"))
        self.assertEqual(discovery.locate("City of San Luis Obispo", "CA", self.seed).match_kind, "city")
        self.assertEqual(discovery.locate("Yuba City", "CA", self.seed).place_name, "Yuba City")
        self.assertEqual(discovery.locate("La Canada Flintridge", "CA", self.seed).county_fips, "06037")

    def test_ambiguous_and_unknown(self):
        with self.assertRaises(ValueError) as caught:
            discovery.locate("San Luis Obispo", "CA", self.seed)
        self.assertIn("Ambiguous", str(caught.exception))
        self.assertIn("San Luis Obispo County", str(caught.exception))
        self.assertIn("City of San Luis Obispo", str(caught.exception))
        with self.assertRaises(ValueError):
            discovery.locate("SLO", "CA", self.seed)
        with self.assertRaises(ValueError):
            discovery.locate("Morro Bay", "NV", self.seed)
        with self.assertRaises(ValueError):
            discovery.locate("", "CA", self.seed)

    def test_agencies_for_city_orders_city_first(self):
        loc = discovery.locate("Morro Bay", "CA", self.seed)
        ids = [a["agency_id"] for a in discovery.agencies_for(loc, self.seed)]
        self.assertEqual(ids[:2], ["ca-morro-bay-police", "ca-morro-bay-city-council"])
        self.assertEqual(ids[2], "ca-san-luis-obispo-county-sheriff")
        self.assertEqual(len(ids), 6)
        for agency in discovery.agencies_for(loc, self.seed):
            self.assertIs(agency["suggested"], True)

    def test_agencies_for_county_and_include_filter(self):
        loc = discovery.locate("San Luis Obispo County", "CA", self.seed)
        everything = discovery.agencies_for(loc, self.seed)
        self.assertEqual(len(everything), 18)
        self.assertEqual(everything[0]["kind"], "sheriff")
        only = discovery.agencies_for(loc, self.seed, include=("police", "sheriff"))
        self.assertEqual([a["kind"] for a in only][:2], ["sheriff", "police"])
        self.assertEqual(len(only), 8)
        with self.assertRaises(ValueError):
            discovery.agencies_for(loc, self.seed, include=("fbi",))

    def test_locate_online_parses_geocoder_payload_without_network(self):
        payload = {"result": {"addressMatches": [{"geographies": {
            "Counties": [{"GEOID": "06079", "NAME": "San Luis Obispo County"}],
            "Incorporated Places": [{"GEOID": "0648945", "NAME": "Morro Bay city", "BASENAME": "Morro Bay"}],
        }}]}}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps(payload).encode()

        seen = {}

        def opener(url, timeout):
            seen["url"] = url
            seen["timeout"] = timeout
            return Response()

        loc = discovery.locate_online("Morro Bay", "CA", opener=opener)
        self.assertEqual((loc.county_fips, loc.place_fips, loc.place_name, loc.match_kind),
                         ("06079", "0648945", "Morro Bay", "city"))
        self.assertEqual(seen["timeout"], 10)
        self.assertIn("geographies/onelineaddress", seen["url"])
        self.assertIn("benchmark=Public_AR_Current", seen["url"])


if __name__ == "__main__":
    unittest.main()
