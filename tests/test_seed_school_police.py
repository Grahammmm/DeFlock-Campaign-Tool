"""DF1207-18: K-12 school district police are seeded as kind school_police and reach a kit only via --include.

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

KERN_HIGH = "Kern High School District Police Department"
SEED_FILES = (REPO_ROOT / "data" / "agencies" / "us-ca.json",
              REPO_ROOT / "campaign_tool" / "_resources" / "data" / "agencies" / "us-ca.json",
              REPO_ROOT / "workers" / "wizard" / "src" / "generated" / "agencies-us-ca.json")
# Districts that rely on city or sheriff school resource officers, or have only a security/safety
# division, or whose department an official page did not confirm: never listed.
NOT_LISTED = ("San Jose Unified School District", "Oakland Unified School District",
              "Long Beach Unified School District", "Pomona Unified School District",
              "Sweetwater Union High School District", "Perris Union High School District",
              "West Contra Costa Unified School District", "Bakersfield City School District",
              "Colton Joint Unified School District")


def init(root, county):
    args = [sys.executable, "-B", "-m", "campaign_tool", "init", "--directory", str(root),
            "--name", "Synthetic Campaign", "--county", county, "--state", "CA"]
    result = subprocess.run(args, cwd=REPO_ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return root


def kit_files(root):
    base = root / "kit"
    return {str(p.relative_to(base)): p.read_bytes() for p in sorted(base.rglob("*")) if p.is_file()}


def school(entries):
    return [a for a in entries if a["kind"] == "school_police"]


class SeedTests(unittest.TestCase):
    def test_check_green_and_every_entry_unverified_with_one_note(self):
        for script in ("tools/build_agency_seed.py", "tools/sync_campaign_resources.py"):
            args = [sys.executable, "-B", script] + (["--check"] if "build" in script else [])
            result = subprocess.run(args, cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, script + result.stdout + result.stderr)
        expected_ids = {f"ca-{build_agency_seed.slugify(district)}-school-police"
                        for _fips, district, _name in build_agency_seed.SCHOOL_POLICE}
        self.assertEqual(len(expected_ids), len(build_agency_seed.SCHOOL_POLICE))
        for path in SEED_FILES:
            seed = json.loads(path.read_text(encoding="utf-8"))
            all_ids = [a["agency_id"] for c in seed["counties"] for a in c["agencies"]]
            self.assertEqual(len(all_ids), len(set(all_ids)), path)
            county_fips = {c["county_fips"] for c in seed["counties"]}
            listed = []
            for county in seed["counties"]:
                for entry in school(county["agencies"]):
                    listed.append(entry["agency_id"])
                    self.assertIn(county["county_fips"], county_fips)
                    self.assertIs(entry["verified"], False)
                    self.assertIsNone(entry["records_url"])
                    self.assertIsNone(entry["records_email"])
                    self.assertIsNone(entry["place_fips"])
                    self.assertEqual(entry["portal"], {"vendor": "unknown", "url": None})
                    self.assertEqual(len(entry["sources"]), 1)
                    self.assertEqual(entry["sources"][0]["title"], build_agency_seed.SCHOOL_NOTE)
                    self.assertIsNone(entry["sources"][0]["url"])
                    self.assertRegex(entry["jurisdiction_name"], r"District$")
            self.assertEqual(set(listed), expected_ids, path)
            self.assertEqual(len(listed), len(build_agency_seed.SCHOOL_POLICE), path)

    def test_districts_served_by_city_or_sheriff_officers_are_not_listed(self):
        for path in SEED_FILES:
            seed = json.loads(path.read_text(encoding="utf-8"))
            names = {a["jurisdiction_name"] for c in seed["counties"] for a in school(c["agencies"])}
            for district in NOT_LISTED:
                self.assertNotIn(district, names, path)
        table = {district for _fips, district, _name in build_agency_seed.SCHOOL_POLICE}
        self.assertEqual(table & set(NOT_LISTED), set())

    def test_schema_validates_and_rejects_unknown_kind(self):
        schema = json.loads((REPO_ROOT / "schemas" / "agency-seed.schema.json").read_text(encoding="utf-8"))
        seed = discovery.load_seed("us-ca")
        law.validate(seed, schema)
        broken = copy.deepcopy(seed)
        next(a for c in broken["counties"] for a in c["agencies"] if a["kind"] == "school_police")["kind"] = "school"
        with self.assertRaises(ValueError):
            law.validate(broken, schema)

    def test_order_is_campus_then_ccd_then_school_in_table_order_per_county(self):
        seed = discovery.load_seed("us-ca")
        san_bernardino = next(c for c in seed["counties"] if c["county_fips"] == "06071")
        kinds = [a["kind"] for a in san_bernardino["agencies"]]
        last_ccd = len(kinds) - 1 - kinds[::-1].index("ccd_police")
        first_school = kinds.index("school_police")
        self.assertEqual(first_school, last_ccd + 1)
        table = [name for fips, _district, name in build_agency_seed.SCHOOL_POLICE if fips == "06071"]
        self.assertGreater(len(table), 1)
        self.assertEqual([a["name"] for a in school(san_bernardino["agencies"])], table)
        self.assertEqual(kinds[first_school:first_school + len(table)], ["school_police"] * len(table))

    def test_check_tables_refuses_duplicate_district_and_unknown_county(self):
        row = build_agency_seed.SCHOOL_POLICE[0]
        bad_tables = {"duplicate school district row": build_agency_seed.SCHOOL_POLICE + [row],
                      "06002 is not a county fips": [("06002",) + row[1:]]}
        for message, table in bad_tables.items():
            with self.subTest(message=message), mock.patch.object(build_agency_seed, "SCHOOL_POLICE", table):
                with self.assertRaisesRegex(ValueError, message):
                    build_agency_seed.check_tables()
        build_agency_seed.check_tables()

    def test_slug_equal_to_a_ccd_district_still_gets_a_distinct_id(self):
        ccd_district = build_agency_seed.CCD_POLICE[0]
        clash = (ccd_district[0], ccd_district[1], ccd_district[2].replace("Community College", "School"))
        with mock.patch.object(build_agency_seed, "SCHOOL_POLICE", [clash]):
            built = build_agency_seed.build(build_agency_seed.load_overlay())
            build_agency_seed.check_tables()  # no duplicate-id error
        county = next(c for c in built["counties"] if c["county_fips"] == ccd_district[0])
        slug = build_agency_seed.slugify(ccd_district[1])
        self.assertEqual([a["agency_id"] for a in county["agencies"] if a["agency_id"].startswith(f"ca-{slug}-")],
                         [f"ca-{slug}-ccd-police", f"ca-{slug}-school-police"])

    def test_doc_names_the_sro_rule_count_and_opt_in(self):
        doc = (REPO_ROOT / "docs" / "AGENCY-DISCOVERY.md").read_text(encoding="utf-8")
        self.assertIn(f"({len(build_agency_seed.SCHOOL_POLICE)}, from the\n`SCHOOL_POLICE` table)", doc)
        self.assertIn("`--include school_police`", doc)
        self.assertIn("school resource officers from a city police department or\nthe county sheriff is not listed", doc)
        self.assertNotIn("school police are not seeded", " ".join(doc.split()).lower())


class DiscoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.seed = discovery.load_seed("us-ca")

    def names(self, query, include):
        location = discovery.locate(query, "CA", self.seed)
        return [a["name"] for a in discovery.agencies_for(location, self.seed, include)]

    def test_default_include_never_lists_school_police(self):
        self.assertNotIn("school_police", discovery.DEFAULT_INCLUDE)
        self.assertEqual(discovery.OPT_IN_KINDS, ("campus_police", "ccd_police", "school_police", "district_police", "fusion_center"))
        self.assertIn("school_police", discovery.LAW_ENFORCEMENT_KINDS)
        for query in ("Kern County", "San Bernardino County", "Los Angeles County", "Fontana"):
            location = discovery.locate(query, "CA", self.seed)
            self.assertEqual(school(discovery.agencies_for(location, self.seed)), [], query)

    def test_opt_in_lists_after_ccd_and_all_three_together(self):
        self.assertEqual(self.names("Kern County", ("sheriff", "school_police")),
                         ["Kern County Sheriff's Office", KERN_HIGH])
        sb = self.names("San Bernardino County", ("campus_police", "ccd_police", "school_police"))
        table = [n for fips, _d, n in build_agency_seed.SCHOOL_POLICE if fips == "06071"]
        self.assertEqual(sb[-len(table):], table)
        kinds = [a["kind"] for a in discovery.agencies_for(
            discovery.locate("San Bernardino County", "CA", self.seed), self.seed,
            ("school_police", "campus_police", "ccd_police"))]
        self.assertEqual(kinds, sorted(kinds, key=("school_police", "campus_police", "ccd_police").index))
        city = discovery.agencies_for(discovery.locate("Fontana", "CA", self.seed), self.seed,
                                      ("police", "sheriff", "school_police"))
        self.assertEqual([a["kind"] for a in city][-1], "school_police")
        self.assertEqual(self.names("Alpine County", ("school_police",)), [])

    def test_unknown_kind_typo_names_allowed_kinds(self):
        location = discovery.locate("Kern County", "CA", self.seed)
        for typo in ("school", "School_Police", "school-police"):
            with self.subTest(typo=typo), self.assertRaises(ValueError) as caught:
                discovery.agencies_for(location, self.seed, ("sheriff", typo))
            self.assertIn(f"Unknown agency kind(s) ['{typo}']", str(caught.exception))
            self.assertIn("'school_police'", str(caught.exception))


class KitTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-school-kit-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)

    def run_kit(self, root, *include):
        args = [sys.executable, "-B", "-m", "campaign_tool", "kit", "--directory", str(root)]
        for kind in include:
            args += ["--include", kind]
        return subprocess.run(args, cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)

    def test_default_kit_byte_identical_to_seed_without_school_rows(self):
        with mock.patch.object(build_agency_seed, "SCHOOL_POLICE", []):
            main_like = build_agency_seed.build(build_agency_seed.load_overlay())
        branch = discovery.load_seed("us-ca")
        stripped = json.loads(json.dumps(branch))
        for county in stripped["counties"]:
            county["agencies"] = [a for a in county["agencies"] if a["kind"] != "school_police"]
        self.assertEqual(stripped, main_like)  # school rows are the only seed change
        outputs = []
        for name, seed in (("main", main_like), ("branch", None)):
            root = init(self.tmp / name / "campaign", "Kern")
            summary = kit.build_kit(root, seed=seed)
            outputs.append((summary, kit_files(root)))
        (main_summary, main_files), (summary, files) = outputs
        self.assertEqual(summary, main_summary)
        self.assertEqual(sorted(files), sorted(main_files))
        self.assertGreater(len(files), 5)
        self.assertEqual([p for p in files if files[p] != main_files[p]], [])
        self.assertNotIn(b"kern high school district", b"".join(files.values()).lower())

    def test_cli_include_school_police_lists_unverified_after_other_opt_ins(self):
        root = init(self.tmp / "campaign", "San Bernardino")
        result = self.run_kit(root, "campus_police", "ccd_police", "school_police")
        self.assertEqual(result.returncode, 0, result.stderr)
        agencies = json.loads((root / "kit" / "agencies.json").read_text(encoding="utf-8"))
        kinds = [a["kind"] for a in agencies]
        self.assertEqual(kinds, sorted(kinds, key=("campus_police", "ccd_police", "school_police").index))
        self.assertEqual(kinds.count("school_police"), 6)
        for entry in agencies:
            self.assertIs(entry["verified"], False)
            self.assertIsNone(entry["records_email"])
        requests = {p.name for p in (root / "kit" / "requests").glob("*.md")}
        self.assertIn("ca-fontana-unified-school-district-school-police.md", requests)

    def test_cli_include_only_school_police_and_unknown_kind_refused(self):
        root = init(self.tmp / "campaign", "Kern")
        result = self.run_kit(root, "school_police")
        self.assertEqual(result.returncode, 0, result.stderr)
        agencies = json.loads((root / "kit" / "agencies.json").read_text(encoding="utf-8"))
        self.assertEqual([a["name"] for a in agencies], [KERN_HIGH])
        refused = self.run_kit(root, "school")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("school_police", refused.stdout + refused.stderr)
        self.assertNotIn("Traceback", refused.stderr)
        helped = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "kit", "--help"], cwd=REPO_ROOT,
                                capture_output=True, text=True, timeout=30)
        self.assertRegex(" ".join(helped.stdout.split()), r"school_police \(opt-in\)")


if __name__ == "__main__":
    unittest.main()
