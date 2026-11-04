"""DF1422-01: special-district police are seeded as kind district_police and reach a kit only via --include.

Synthetic temporary campaigns only; no network; nothing is sent.
"""
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tools"))
import build_agency_seed  # noqa: E402
from campaign_tool import discovery, kit, law  # noqa: E402

BART = "San Francisco Bay Area Rapid Transit District Police Department"
EBRPD = "East Bay Regional Park District Police Department"
SEED_FILES = (REPO_ROOT / "data" / "agencies" / "us-ca.json",
              REPO_ROOT / "campaign_tool" / "_resources" / "data" / "agencies" / "us-ca.json",
              REPO_ROOT / "workers" / "wizard" / "src" / "generated" / "agencies-us-ca.json")
# Departments that contract with a sheriff or a city, or belong to a city: never listed.
NOT_LISTED = ("Sacramento Regional Transit District", "Santa Clara Valley Transportation Authority",
              "Los Angeles County Metropolitan Transportation Authority", "Oxnard Harbor District",
              "Port San Luis Harbor District", "Golden Gate Bridge, Highway and Transportation District")


def init(root, county):
    args = [sys.executable, "-B", "-m", "campaign_tool", "init", "--directory", str(root),
            "--name", "Synthetic Campaign", "--county", county, "--state", "CA"]
    result = subprocess.run(args, cwd=REPO_ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return root


def kit_files(root):
    base = root / "kit"
    return {str(p.relative_to(base)): p.read_bytes() for p in sorted(base.rglob("*")) if p.is_file()}


def district(entries):
    return [a for a in entries if a["kind"] == "district_police"]


def county_names():
    return {fips: name for fips, name, _seat, _cities in build_agency_seed.COUNTIES}


class SeedTests(unittest.TestCase):
    def test_check_green_and_every_entry_unverified_with_one_note_naming_type_and_county(self):
        for script in ("tools/build_agency_seed.py", "tools/sync_campaign_resources.py"):
            args = [sys.executable, "-B", script] + (["--check"] if "build" in script else [])
            result = subprocess.run(args, cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, script + result.stdout + result.stderr)
        names = county_names()
        expected_notes = {
            (fips, d): build_agency_seed.DISTRICT_NOTE.format(type=kind, county=names[fips])
            for fips, d, _name, kind in build_agency_seed.DISTRICT_POLICE}
        for path in SEED_FILES:
            seed = json.loads(path.read_text(encoding="utf-8"))
            all_ids = [a["agency_id"] for c in seed["counties"] for a in c["agencies"]]
            self.assertEqual(len(all_ids), len(set(all_ids)), path)
            listed = []
            for county in seed["counties"]:
                for entry in district(county["agencies"]):
                    listed.append((county["county_fips"], entry["jurisdiction_name"]))
                    self.assertIs(entry["verified"], False)
                    self.assertIsNone(entry["records_url"])
                    self.assertIsNone(entry["records_email"])
                    self.assertIsNone(entry["place_fips"])
                    self.assertEqual(entry["portal"], {"vendor": "unknown", "url": None})
                    self.assertEqual(len(entry["sources"]), 1)
                    note = entry["sources"][0]["title"]
                    self.assertEqual(note, expected_notes[(county["county_fips"], entry["jurisdiction_name"])])
                    self.assertIn(f"serves {county['name']} County;", note)
                    self.assertIsNone(entry["sources"][0]["url"])
            self.assertEqual(sorted(listed), sorted(expected_notes), path)

    def test_every_table_row_has_a_known_type_and_note_names_it(self):
        for _fips, _d, _name, kind in build_agency_seed.DISTRICT_POLICE:
            self.assertIn(kind, build_agency_seed.DISTRICT_TYPES)
        self.assertEqual(set(build_agency_seed.DISTRICT_TYPES), {"transit", "port_harbor", "airport", "park"})
        self.assertEqual({kind for _f, _d, _n, kind in build_agency_seed.DISTRICT_POLICE},
                         {"transit", "port_harbor", "park"})
        for county in discovery.load_seed("us-ca")["counties"]:
            for entry in district(county["agencies"]):
                self.assertRegex(entry["sources"][0]["title"],
                                 r"^special-district police department \((transit|port_harbor|airport|park)\); ")

    def test_departments_that_contract_or_belong_to_a_city_are_not_listed(self):
        table = {d for _f, d, _n, _k in build_agency_seed.DISTRICT_POLICE}
        self.assertEqual(table & set(NOT_LISTED), set())
        for path in SEED_FILES:
            seed = json.loads(path.read_text(encoding="utf-8"))
            names = {a["jurisdiction_name"] for c in seed["counties"] for a in district(c["agencies"])}
            self.assertEqual(names & set(NOT_LISTED), set(), path)

    def test_multi_county_district_gets_one_entry_per_county_with_distinct_ids(self):
        names = county_names()
        bart_rows = [row for row in build_agency_seed.DISTRICT_POLICE if row[1].startswith("San Francisco Bay Area Rapid")]
        self.assertGreater(len(bart_rows), 1)
        seed = discovery.load_seed("us-ca")
        found = [(c["county_fips"], a) for c in seed["counties"] for a in district(c["agencies"])
                 if a["name"] == BART]
        self.assertEqual([fips for fips, _a in found], [row[0] for row in bart_rows])
        ids = [a["agency_id"] for _fips, a in found]
        self.assertEqual(len(set(ids)), len(ids))
        for fips, entry in found:
            slug = build_agency_seed.slugify(names[fips])
            self.assertEqual(entry["agency_id"], f"ca-san-francisco-bay-area-rapid-transit-district-police-{slug}")
            self.assertIn(f"serves {names[fips]} County;", entry["sources"][0]["title"])

    def test_multi_county_fixture_row_one_entry_per_county_matching_the_note(self):
        fixture = [("06001", "Example Transit District", "Example Transit District Police Department", "transit"),
                   ("06013", "Example Transit District", "Example Transit District Police Department", "transit"),
                   ("06075", "Example Transit District", "Example Transit District Police Department", "transit")]
        with mock.patch.object(build_agency_seed, "DISTRICT_POLICE", fixture):
            built = build_agency_seed.build(build_agency_seed.load_overlay())
            build_agency_seed.check_tables()
        found = [(c["county_fips"], c["name"], a) for c in built["counties"] for a in district(c["agencies"])]
        self.assertEqual([f for f, _n, _a in found], ["06001", "06013", "06075"])
        ids = [a["agency_id"] for _f, _n, a in found]
        self.assertEqual(ids, ["ca-example-transit-district-police-alameda", "ca-example-transit-district-police-contra-costa",
                               "ca-example-transit-district-police-san-francisco"])
        for _fips, name, entry in found:
            self.assertIn(f"serves {name} County;", entry["sources"][0]["title"])

    def test_single_county_district_keeps_the_plain_police_id(self):
        seed = discovery.load_seed("us-ca")
        san_diego = next(c for c in seed["counties"] if c["county_fips"] == "06073")
        self.assertEqual([a["agency_id"] for a in district(san_diego["agencies"])],
                         ["ca-san-diego-unified-port-district-police"])

    def test_district_slug_equal_to_a_city_police_slug_still_gets_a_distinct_id(self):
        # "Fresno" already has the city id ca-fresno-police; the two fixture rows must not repeat it.
        fixture = [("06019", "Fresno", "Fresno Example District Police Department", "park"),
                   ("06067", "Sacramento", "Sacramento Example District Police Department", "airport")]
        with mock.patch.object(build_agency_seed, "DISTRICT_POLICE", fixture):
            built = build_agency_seed.build(build_agency_seed.load_overlay())
            build_agency_seed.check_tables()  # no duplicate-id error
        by_id = {a["agency_id"]: a for c in built["counties"] for a in c["agencies"]}
        self.assertEqual(by_id["ca-fresno-police"]["kind"], "police")
        self.assertEqual(by_id["ca-fresno-police-fresno"]["kind"], "district_police")
        self.assertEqual(by_id["ca-sacramento-police"]["kind"], "police")
        self.assertEqual(by_id["ca-sacramento-police-sacramento"]["kind"], "district_police")
        all_ids = [a["agency_id"] for c in built["counties"] for a in c["agencies"]]
        self.assertEqual(len(all_ids), len(set(all_ids)))

    def test_schema_validates_and_rejects_unknown_kind(self):
        schema = json.loads((REPO_ROOT / "schemas" / "agency-seed.schema.json").read_text(encoding="utf-8"))
        seed = discovery.load_seed("us-ca")
        law.validate(seed, schema)
        broken = copy.deepcopy(seed)
        next(a for c in broken["counties"] for a in c["agencies"] if a["kind"] == "district_police")["kind"] = "district"
        with self.assertRaises(ValueError):
            law.validate(broken, schema)

    def test_order_is_school_then_district_in_table_order_per_county(self):
        seed = discovery.load_seed("us-ca")
        la = next(c for c in seed["counties"] if c["county_fips"] == "06073")
        kinds = [a["kind"] for a in la["agencies"]]
        last_school = len(kinds) - 1 - kinds[::-1].index("school_police")
        first_district = kinds.index("district_police")
        self.assertEqual(first_district, last_school + 1)
        alameda = next(c for c in seed["counties"] if c["county_fips"] == "06001")
        table = [name for fips, _d, name, _k in build_agency_seed.DISTRICT_POLICE if fips == "06001"]
        self.assertEqual([a["name"] for a in district(alameda["agencies"])], table)
        self.assertEqual(table, [BART, EBRPD])

    def test_check_tables_refuses_duplicate_row_unknown_county_and_unknown_type(self):
        row = build_agency_seed.DISTRICT_POLICE[0]
        bad_tables = {"duplicate special district row": build_agency_seed.DISTRICT_POLICE + [row],
                      "06002 is not a county fips": [("06002",) + row[1:]],
                      "unknown district type": [row[:3] + ("ferry",)]}
        for message, table in bad_tables.items():
            with self.subTest(message=message), mock.patch.object(build_agency_seed, "DISTRICT_POLICE", table):
                with self.assertRaisesRegex(ValueError, message):
                    build_agency_seed.check_tables()
        build_agency_seed.check_tables()

    def test_check_tables_refuses_a_duplicate_agency_id_across_the_seed(self):
        # A hand-built clash the id rule cannot avoid: refused by the existing duplicate-id check.
        clash = ("06001", "Alameda County Sheriff", "Clash District Police Department", "park")
        with mock.patch.object(build_agency_seed, "DISTRICT_POLICE", [clash]):
            with mock.patch.object(build_agency_seed, "district_agencies",
                                   lambda fips, reserved=(): [build_agency_seed.agency(
                                       "ca-alameda-county-sheriff", "x", "district_police", "x")] if fips == "06001" else []):
                with self.assertRaisesRegex(ValueError, "duplicate agency_id ca-alameda-county-sheriff"):
                    build_agency_seed.check_tables()

    def test_doc_names_the_count_opt_in_multi_county_and_contracting_rules(self):
        doc = (REPO_ROOT / "docs" / "AGENCY-DISCOVERY.md").read_text(encoding="utf-8")
        flat = " ".join(doc.split())
        districts = {d for _f, d, _n, _k in build_agency_seed.DISTRICT_POLICE}
        self.assertIn(f"({len(build_agency_seed.DISTRICT_POLICE)} entries for {len(districts)} districts", flat)
        self.assertIn("from the `DISTRICT_POLICE` table", flat)
        self.assertIn("`--include district_police`", doc)
        self.assertIn("one entry per county it serves", flat)
        self.assertIn("contracts with a sheriff or a city police department", flat)
        self.assertIn("Still unseeded:", doc)
        self.assertIn("`ccd_police`, `school_police` and `district_police`", flat)


class DiscoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.seed = discovery.load_seed("us-ca")

    def names(self, query, include):
        location = discovery.locate(query, "CA", self.seed)
        return [a["name"] for a in discovery.agencies_for(location, self.seed, include)]

    def test_default_include_never_lists_district_police(self):
        self.assertNotIn("district_police", discovery.DEFAULT_INCLUDE)
        self.assertEqual(discovery.OPT_IN_KINDS, ("campus_police", "ccd_police", "school_police", "district_police"))
        self.assertIn("district_police", discovery.LAW_ENFORCEMENT_KINDS)
        self.assertIn("district_police", discovery.COUNTY_KINDS)
        for query in ("Alameda County", "San Diego County", "Los Angeles County", "Oakland"):
            location = discovery.locate(query, "CA", self.seed)
            self.assertEqual(district(discovery.agencies_for(location, self.seed)), [], query)

    def test_opt_in_lists_after_school_and_all_four_together_in_order(self):
        self.assertEqual(self.names("Alameda County", ("sheriff", "district_police")),
                         ["Alameda County Sheriff's Office", BART, EBRPD])
        kinds = [a["kind"] for a in discovery.agencies_for(
            discovery.locate("San Diego County", "CA", self.seed), self.seed,
            ("campus_police", "ccd_police", "school_police", "district_police"))]
        self.assertEqual(kinds, sorted(kinds, key=("campus_police", "ccd_police", "school_police", "district_police").index))
        self.assertEqual(kinds[-1], "district_police")
        self.assertEqual(kinds.count("district_police"), 1)
        reversed_kinds = [a["kind"] for a in discovery.agencies_for(
            discovery.locate("San Diego County", "CA", self.seed), self.seed,
            ("district_police", "school_police", "ccd_police", "campus_police"))]
        self.assertEqual(reversed_kinds, sorted(reversed_kinds, key=("district_police", "school_police", "ccd_police",
                                                                      "campus_police").index))
        self.assertEqual(reversed_kinds[0], "district_police")
        city = discovery.agencies_for(discovery.locate("Oakland", "CA", self.seed), self.seed,
                                      ("police", "sheriff", "district_police"))
        self.assertEqual([a["kind"] for a in city][-2:], ["district_police", "district_police"])
        self.assertEqual(self.names("Alpine County", ("district_police",)), [])

    def test_each_county_of_a_multi_county_district_sees_only_its_own_entry(self):
        for county, suffix in (("Alameda County", "alameda"), ("Contra Costa County", "contra-costa"),
                               ("San Mateo County", "san-mateo"), ("Santa Clara County", "santa-clara")):
            location = discovery.locate(county, "CA", self.seed)
            bart = [a for a in discovery.agencies_for(location, self.seed, ("district_police",))
                    if a["name"] == BART]
            self.assertEqual([a["agency_id"] for a in bart],
                             [f"ca-san-francisco-bay-area-rapid-transit-district-police-{suffix}"], county)

    def test_unknown_kind_typo_names_allowed_kinds(self):
        location = discovery.locate("Alameda County", "CA", self.seed)
        for typo in ("district", "District_Police", "district-police"):
            with self.subTest(typo=typo), self.assertRaises(ValueError) as caught:
                discovery.agencies_for(location, self.seed, ("sheriff", typo))
            self.assertIn(f"Unknown agency kind(s) ['{typo}']", str(caught.exception))
            self.assertIn("'district_police'", str(caught.exception))


class KitTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-district-kit-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)

    def run_kit(self, root, *include):
        args = [sys.executable, "-B", "-m", "campaign_tool", "kit", "--directory", str(root)]
        for kind in include:
            args += ["--include", kind]
        return subprocess.run(args, cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)

    def test_default_kit_byte_identical_to_seed_without_district_rows(self):
        with mock.patch.object(build_agency_seed, "DISTRICT_POLICE", []):
            main_like = build_agency_seed.build(build_agency_seed.load_overlay())
        branch = discovery.load_seed("us-ca")
        stripped = json.loads(json.dumps(branch))
        for county in stripped["counties"]:
            county["agencies"] = [a for a in county["agencies"] if a["kind"] != "district_police"]
        self.assertEqual(stripped, main_like)  # district rows are the only seed change
        outputs = []
        for name, seed in (("main", main_like), ("branch", None)):
            root = init(self.tmp / name / "campaign", "Alameda")
            summary = kit.build_kit(root, seed=seed)
            outputs.append((summary, kit_files(root)))
        (main_summary, main_files), (summary, files) = outputs
        self.assertEqual(summary, main_summary)
        self.assertEqual(sorted(files), sorted(main_files))
        self.assertGreater(len(files), 5)
        self.assertEqual([p for p in files if files[p] != main_files[p]], [])
        joined = b"".join(files.values()).lower()
        self.assertNotIn(b"rapid transit district", joined)
        self.assertNotIn(b"east bay regional park district", joined)

    def test_cli_include_district_police_lists_unverified_after_other_opt_ins(self):
        root = init(self.tmp / "campaign", "San Diego")
        result = self.run_kit(root, "campus_police", "ccd_police", "school_police", "district_police")
        self.assertEqual(result.returncode, 0, result.stderr)
        agencies = json.loads((root / "kit" / "agencies.json").read_text(encoding="utf-8"))
        kinds = [a["kind"] for a in agencies]
        self.assertEqual(kinds, sorted(kinds, key=("campus_police", "ccd_police", "school_police", "district_police").index))
        self.assertEqual(kinds.count("district_police"), 1)
        for entry in agencies:
            self.assertIs(entry["verified"], False)
            self.assertIsNone(entry["records_email"])
        requests = {p.name for p in (root / "kit" / "requests").glob("*.md")}
        self.assertIn("ca-san-diego-unified-port-district-police.md", requests)

    def test_cli_include_only_district_police_and_unknown_kind_refused(self):
        root = init(self.tmp / "campaign", "Alameda")
        result = self.run_kit(root, "district_police")
        self.assertEqual(result.returncode, 0, result.stderr)
        agencies = json.loads((root / "kit" / "agencies.json").read_text(encoding="utf-8"))
        self.assertEqual([a["name"] for a in agencies], [BART, EBRPD])
        self.assertEqual([a["agency_id"] for a in agencies],
                         ["ca-san-francisco-bay-area-rapid-transit-district-police-alameda",
                          "ca-east-bay-regional-park-district-police-alameda"])
        refused = self.run_kit(root, "district")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("district_police", refused.stdout + refused.stderr)
        self.assertNotIn("Traceback", refused.stderr)
        helped = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "kit", "--help"], cwd=REPO_ROOT,
                                capture_output=True, text=True, timeout=30)
        self.assertRegex(" ".join(helped.stdout.split()), r"district_police \(opt-in\)")


if __name__ == "__main__":
    unittest.main()
