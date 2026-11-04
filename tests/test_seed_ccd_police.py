"""DF1207-17: community college district police are seeded as kind ccd_police and reach a kit only via --include.

Synthetic temporary campaigns only; no network; nothing is sent.
"""
import copy
import json
import re
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

OHLONE = "Ohlone Community College District Police Department"
BERKELEY = "University of California, Berkeley Police Department"
EAST_BAY = "California State University, East Bay University Police Department"
SEED_FILES = (REPO_ROOT / "data" / "agencies" / "us-ca.json",
              REPO_ROOT / "campaign_tool" / "_resources" / "data" / "agencies" / "us-ca.json")


def init(root, county):
    args = [sys.executable, "-B", "-m", "campaign_tool", "init", "--directory", str(root),
            "--name", "Synthetic Campaign", "--county", county, "--state", "CA"]
    result = subprocess.run(args, cwd=REPO_ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return root


def kit_files(root):
    base = root / "kit"
    return {str(p.relative_to(base)): p.read_bytes() for p in sorted(base.rglob("*")) if p.is_file()}


def ccd(entries):
    return [a for a in entries if a["kind"] == "ccd_police"]


class SeedTests(unittest.TestCase):
    def test_check_green_and_every_entry_unverified_with_one_note(self):
        for script in ("tools/build_agency_seed.py", "tools/sync_campaign_resources.py"):
            args = [sys.executable, "-B", script] + (["--check"] if "build" in script else [])
            result = subprocess.run(args, cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, script + result.stdout + result.stderr)
        expected_ids = {f"ca-{build_agency_seed.slugify(district)}-ccd-police"
                        for _fips, district, _name in build_agency_seed.CCD_POLICE}
        self.assertEqual(len(expected_ids), len(build_agency_seed.CCD_POLICE))
        for path in SEED_FILES:
            seed = json.loads(path.read_text(encoding="utf-8"))
            all_ids = [a["agency_id"] for c in seed["counties"] for a in c["agencies"]]
            self.assertEqual(len(all_ids), len(set(all_ids)), path)
            county_fips = {c["county_fips"] for c in seed["counties"]}
            listed = []
            for county in seed["counties"]:
                for entry in ccd(county["agencies"]):
                    listed.append(entry["agency_id"])
                    self.assertIn(county["county_fips"], county_fips)
                    self.assertIs(entry["verified"], False)
                    self.assertIsNone(entry["records_url"])
                    self.assertIsNone(entry["records_email"])
                    self.assertIsNone(entry["place_fips"])
                    self.assertEqual(entry["portal"], {"vendor": "unknown", "url": None})
                    self.assertEqual(len(entry["sources"]), 1)
                    self.assertEqual(entry["sources"][0]["title"], build_agency_seed.CCD_NOTE)
                    self.assertIsNone(entry["sources"][0]["url"])
                    self.assertRegex(entry["jurisdiction_name"], r"(Community|Junior) College District$")
            self.assertEqual(set(listed), expected_ids, path)
            self.assertEqual(len(listed), len(build_agency_seed.CCD_POLICE), path)

    def test_coast_contract_policing_is_not_seeded(self):
        # The district's 2025 ASFSR supersedes its 2019 police protocol manual:
        # unsworn Public Safety staff rely on local police under MOUs.
        for path in SEED_FILES:
            seed = json.loads(path.read_text(encoding="utf-8"))
            entries = [a for c in seed["counties"] for a in c["agencies"]]
            self.assertNotIn("ca-coast-community-college-district-ccd-police",
                             [a["agency_id"] for a in entries], path)
            self.assertNotIn("Coast Community College District",
                             [a["jurisdiction_name"] for a in entries], path)

    def test_schema_validates_and_rejects_unknown_kind(self):
        schema = json.loads((REPO_ROOT / "schemas" / "agency-seed.schema.json").read_text(encoding="utf-8"))
        seed = discovery.load_seed("us-ca")
        law.validate(seed, schema)
        broken = copy.deepcopy(seed)
        next(a for c in broken["counties"] for a in c["agencies"] if a["kind"] == "ccd_police")["kind"] = "ccd"
        with self.assertRaises(ValueError):
            law.validate(broken, schema)

    def test_order_is_campus_then_ccd_in_table_order_per_county(self):
        seed = discovery.load_seed("us-ca")
        alameda = next(c for c in seed["counties"] if c["county_fips"] == "06001")
        kinds = [a["kind"] for a in alameda["agencies"]][:7]
        self.assertEqual(kinds, ["sheriff", "county_board", "district_attorney", "chp",
                                 "campus_police", "campus_police", "ccd_police"])
        la = next(c for c in seed["counties"] if c["county_fips"] == "06037")
        table = [name for fips, _district, name in build_agency_seed.CCD_POLICE if fips == "06037"]
        self.assertGreater(len(table), 1)
        self.assertEqual([a["name"] for a in ccd(la["agencies"])], table)
        self.assertEqual([a["kind"] for a in la["agencies"]].index("ccd_police"),
                         [a["kind"] for a in la["agencies"]].index("campus_police") + 6)  # 6 LA campuses

    def test_check_tables_refuses_duplicate_district_and_unknown_county(self):
        row = build_agency_seed.CCD_POLICE[0]
        bad_tables = {"duplicate ccd district row": build_agency_seed.CCD_POLICE + [row],
                      "06002 is not a county fips": [("06002",) + row[1:]]}
        for message, table in bad_tables.items():
            with self.subTest(message=message), mock.patch.object(build_agency_seed, "CCD_POLICE", table):
                with self.assertRaisesRegex(ValueError, message):
                    build_agency_seed.check_tables()
        build_agency_seed.check_tables()

    def test_doc_says_all_482_cities_are_seeded(self):
        seed = discovery.load_seed("us-ca")
        councils = [a for c in seed["counties"] for a in c["agencies"] if a["kind"] == "city_council"]
        self.assertEqual(len(councils), 482)
        doc = (REPO_ROOT / "docs" / "AGENCY-DISCOVERY.md").read_text(encoding="utf-8")
        self.assertIn("all 482 incorporated cities", doc)
        self.assertNotIn("believed complete", doc)
        self.assertIn(f"({len(build_agency_seed.CCD_POLICE)}, from the\n`CCD_POLICE` table)", doc)
        self.assertRegex(doc, r"(?i)private universities and school police are not\s+seeded")
        self.assertIsNone(re.search(r"[Cc]ommunity colleges[^.]*not\s+seeded", doc))


class DiscoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.seed = discovery.load_seed("us-ca")

    def names(self, query, include):
        location = discovery.locate(query, "CA", self.seed)
        return [a["name"] for a in discovery.agencies_for(location, self.seed, include)]

    def test_default_include_never_lists_ccd_police(self):
        self.assertNotIn("ccd_police", discovery.DEFAULT_INCLUDE)
        self.assertEqual(discovery.OPT_IN_KINDS, ("campus_police", "ccd_police"))
        self.assertIn("ccd_police", discovery.LAW_ENFORCEMENT_KINDS)
        for query in ("Alameda County", "Fremont", "Los Angeles County", "Pasadena"):
            location = discovery.locate(query, "CA", self.seed)
            self.assertEqual(ccd(discovery.agencies_for(location, self.seed)), [], query)

    def test_opt_in_county_city_and_county_without_entries(self):
        self.assertEqual(self.names("Alameda County", ("sheriff", "ccd_police")),
                         ["Alameda County Sheriff's Office", OHLONE])
        self.assertEqual(self.names("Alameda County", ("sheriff", "campus_police", "ccd_police")),
                         ["Alameda County Sheriff's Office", BERKELEY, EAST_BAY, OHLONE])
        self.assertEqual(self.names("Alameda County", ("ccd_police", "campus_police")),
                         [OHLONE, BERKELEY, EAST_BAY])
        city = discovery.agencies_for(discovery.locate("Fremont", "CA", self.seed), self.seed,
                                      ("police", "city_council", "sheriff", "ccd_police"))
        self.assertEqual([a["kind"] for a in city], ["police", "city_council", "sheriff", "ccd_police"])
        self.assertEqual(self.names("Alpine County", ("ccd_police",)), [])
        table = [n for fips, _d, n in build_agency_seed.CCD_POLICE if fips == "06037"]
        self.assertEqual(self.names("Los Angeles County", ("ccd_police",)), table)

    def test_coast_is_not_suggested_by_include_ccd_police(self):
        orange = self.names("Orange County", ("ccd_police",))
        self.assertEqual(orange, ["South Orange County Community College District Campus Police Departments"])
        self.assertEqual([n for n in orange if "Coast" in n], [])

    def test_unknown_kind_typo_names_allowed_kinds(self):
        location = discovery.locate("Alameda County", "CA", self.seed)
        for typo in ("ccd", "CCD_Police", "ccd-police"):
            with self.subTest(typo=typo), self.assertRaises(ValueError) as caught:
                discovery.agencies_for(location, self.seed, ("sheriff", typo))
            self.assertIn(f"Unknown agency kind(s) ['{typo}']", str(caught.exception))
            self.assertIn("'ccd_police'", str(caught.exception))
            self.assertIn("'campus_police'", str(caught.exception))


class KitTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-ccd-kit-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)

    def run_kit(self, root, *include):
        args = [sys.executable, "-B", "-m", "campaign_tool", "kit", "--directory", str(root)]
        for kind in include:
            args += ["--include", kind]
        return subprocess.run(args, cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)

    def test_default_kit_byte_identical_to_seed_without_ccd_rows(self):
        with mock.patch.object(build_agency_seed, "CCD_POLICE", []):
            main_like = build_agency_seed.build(build_agency_seed.load_overlay())
        branch = discovery.load_seed("us-ca")
        stripped = json.loads(json.dumps(branch))
        for county in stripped["counties"]:
            county["agencies"] = [a for a in county["agencies"] if a["kind"] != "ccd_police"]
        self.assertEqual(stripped, main_like)  # ccd rows are the only seed change
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
        self.assertNotIn(b"community college district", joined)

    def test_cli_include_ccd_police_lists_after_campus_police_unverified(self):
        root = init(self.tmp / "campaign", "Alameda")
        result = self.run_kit(root, "sheriff", "campus_police", "ccd_police")
        self.assertEqual(result.returncode, 0, result.stderr)
        agencies = json.loads((root / "kit" / "agencies.json").read_text(encoding="utf-8"))
        self.assertEqual([a["name"] for a in agencies],
                         ["Alameda County Sheriff's Office", BERKELEY, EAST_BAY, OHLONE])
        for entry in agencies[1:]:
            self.assertIs(entry["verified"], False)
            self.assertIsNone(entry["records_email"])
        requests = sorted(p.name for p in (root / "kit" / "requests").glob("*.md"))
        self.assertEqual(requests, ["ca-alameda-county-sheriff.md", "ca-csu-east-bay-campus-police.md",
                                    "ca-ohlone-community-college-district-ccd-police.md",
                                    "ca-uc-berkeley-campus-police.md"])

    def test_cli_include_only_ccd_police_and_unknown_kind_refused(self):
        root = init(self.tmp / "campaign", "Alameda")
        result = self.run_kit(root, "ccd_police")
        self.assertEqual(result.returncode, 0, result.stderr)
        agencies = json.loads((root / "kit" / "agencies.json").read_text(encoding="utf-8"))
        self.assertEqual([a["name"] for a in agencies], [OHLONE])
        refused = self.run_kit(root, "ccd")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("ccd_police", refused.stdout + refused.stderr)
        self.assertNotIn("Traceback", refused.stderr)
        helped = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "kit", "--help"], cwd=REPO_ROOT,
                                capture_output=True, text=True, timeout=30)
        self.assertRegex(" ".join(helped.stdout.split()), r"ccd_police \(opt-in\)")


if __name__ == "__main__":
    unittest.main()
