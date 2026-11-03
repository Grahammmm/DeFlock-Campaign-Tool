"""DF1822-16: UC/CSU campus police are seeded as kind campus_police and reach a kit only via --include.

Synthetic temporary campaigns only; no network; nothing is sent.
"""
import json
import shutil
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
from campaign_tool import discovery, kit  # noqa: E402
from campaign_tool.content import ContentError  # noqa: E402
from campaign_tool.site import build_site  # noqa: E402

BERKELEY = "University of California, Berkeley Police Department"
EAST_BAY = "California State University, East Bay University Police Department"
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"


def init(root, county):
    args = [sys.executable, "-B", "-m", "campaign_tool", "init", "--directory", str(root),
            "--name", "Synthetic Campaign", "--county", county, "--state", "CA"]
    result = subprocess.run(args, cwd=REPO_ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return root


def kit_files(root):
    base = root / "kit"
    return {str(p.relative_to(base)): p.read_bytes() for p in sorted(base.rglob("*")) if p.is_file()}


def campus(entries):
    return [a for a in entries if a["kind"] == "campus_police"]


class SeedTests(unittest.TestCase):
    def test_checks_green_and_33_unverified_campus_entries(self):
        for script in ("tools/build_agency_seed.py", "tools/sync_campaign_resources.py"):
            args = [sys.executable, "-B", script] + (["--check"] if "build" in script else [])
            result = subprocess.run(args, cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, script + result.stdout + result.stderr)
        for path in (REPO_ROOT / "data" / "agencies" / "us-ca.json",
                     REPO_ROOT / "campaign_tool" / "_resources" / "data" / "agencies" / "us-ca.json"):
            seed = json.loads(path.read_text(encoding="utf-8"))
            entries = [a for county in seed["counties"] for a in campus(county["agencies"])]
            self.assertEqual(len(entries), 33, path)
            self.assertEqual(len({a["agency_id"] for a in entries}), 33)
            for entry in entries:
                self.assertIs(entry["verified"], False)
                self.assertIsNone(entry["records_url"])
                self.assertIsNone(entry["records_email"])
                self.assertEqual(len(entry["sources"]), 1)
                self.assertIn("campus records office", entry["sources"][0]["title"])
        alameda = next(c for c in seed["counties"] if c["county_fips"] == "06001")
        kinds = [a["kind"] for a in alameda["agencies"]][:6]
        self.assertEqual(kinds, ["sheriff", "county_board", "district_attorney", "chp",
                                 "campus_police", "campus_police"])

    def test_check_tables_refuses_duplicate_campus_and_unknown_county(self):
        row = build_agency_seed.CAMPUSES[0]
        bad_tables = {"duplicate campus row": build_agency_seed.CAMPUSES + [row],
                      "06002 is not a county fips": [("06002",) + row[1:]]}
        for message, table in bad_tables.items():
            with self.subTest(message=message), mock.patch.object(build_agency_seed, "CAMPUSES", table):
                with self.assertRaisesRegex(ValueError, message):
                    build_agency_seed.check_tables()
        build_agency_seed.check_tables()


class DiscoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.seed = discovery.load_seed("us-ca")

    def names(self, query, include):
        location = discovery.locate(query, "CA", self.seed)
        return [a["name"] for a in discovery.agencies_for(location, self.seed, include)]

    def test_default_include_never_lists_campus_police(self):
        self.assertNotIn("campus_police", discovery.DEFAULT_INCLUDE)
        self.assertEqual(discovery.OPT_IN_KINDS, ("campus_police",))
        for query in ("Alameda County", "Berkeley", "Los Angeles County"):
            location = discovery.locate(query, "CA", self.seed)
            self.assertEqual(campus(discovery.agencies_for(location, self.seed)), [], query)

    def test_opt_in_county_city_and_county_without_campus(self):
        names = self.names("Alameda County", ("sheriff", "campus_police"))
        self.assertEqual(names, ["Alameda County Sheriff's Office", BERKELEY, EAST_BAY])
        city = discovery.agencies_for(discovery.locate("Berkeley", "CA", self.seed), self.seed,
                                      ("police", "city_council", "sheriff", "campus_police"))
        self.assertEqual([a["kind"] for a in city[:2]], ["police", "city_council"])
        self.assertTrue(all(a["jurisdiction_name"] == "City of Berkeley" for a in city[:2]))
        self.assertEqual([a["name"] for a in city[3:]], [BERKELEY, EAST_BAY])
        self.assertEqual(len(city), 5)
        self.assertEqual(self.names("Alpine County", ("campus_police",)), [])
        self.assertEqual(len(self.names("Los Angeles County", ("campus_police",))), 6)

    def test_unknown_kind_typo_names_allowed_kinds(self):
        location = discovery.locate("Alameda County", "CA", self.seed)
        for typo in ("campus", "Campus_Police", "campus-police"):
            with self.subTest(typo=typo), self.assertRaises(ValueError) as caught:
                discovery.agencies_for(location, self.seed, ("sheriff", typo))
            self.assertIn(f"Unknown agency kind(s) ['{typo}']", str(caught.exception))
            self.assertIn("'campus_police'", str(caught.exception))
            self.assertIn("'chp'", str(caught.exception))


class KitTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-campus-kit-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)

    def test_default_kit_byte_identical_to_seed_without_campuses(self):
        with mock.patch.object(build_agency_seed, "CAMPUSES", []):
            main_like = build_agency_seed.build(build_agency_seed.load_overlay())
        branch = discovery.load_seed("us-ca")
        stripped = json.loads(json.dumps(branch))
        for county in stripped["counties"]:
            county["agencies"] = [a for a in county["agencies"] if a["kind"] != "campus_police"]
        self.assertEqual(stripped, main_like)  # campus rows are the only seed change
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
        self.assertNotIn(b"campus", b"".join(files.values()).lower())

    def test_cli_include_campus_police_drafts_both_unverified(self):
        root = init(self.tmp / "campaign", "Alameda")
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "kit", "--directory", str(root),
                                 "--include", "sheriff", "--include", "campus_police"],
                                cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        agencies = json.loads((root / "kit" / "agencies.json").read_text(encoding="utf-8"))
        self.assertEqual([a["name"] for a in agencies], ["Alameda County Sheriff's Office", BERKELEY, EAST_BAY])
        for entry in agencies[1:]:
            self.assertIs(entry["verified"], False)
            self.assertIsNone(entry["records_email"])
        requests = sorted(p.name for p in (root / "kit" / "requests").glob("*.md"))
        self.assertEqual(requests, ["ca-alameda-county-sheriff.md", "ca-csu-east-bay-campus-police.md",
                                    "ca-uc-berkeley-campus-police.md"])
        refused = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "kit", "--directory", str(root),
                                  "--include", "campus"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("campus_police", refused.stdout + refused.stderr)
        self.assertNotIn("Traceback", refused.stderr)


class SiteKindTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-campus-site-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))

    def write_agency(self, kind):
        path = self.root / "content" / "agencies.json"
        agencies = json.loads((EXAMPLE / "content" / "agencies.json").read_text(encoding="utf-8"))
        agencies.append({"agency_id": "ca-uc-example-campus-police", "slug": "uc-example",
                         "name": "Example Campus PD (fictional)", "kind": kind, "jurisdiction_name": "UC (Example)"})
        path.write_text(json.dumps(agencies), encoding="utf-8")

    def test_campus_police_label_and_unknown_kind_refused(self):
        self.write_agency("campus_police")
        build_site(self.root)
        page = (self.root / "public" / "agencies.html").read_text(encoding="utf-8")
        article = page.split('<article id="uc-example">', 1)[1].split("</article>", 1)[0]
        self.assertIn('<p class="eyebrow">Campus police</p>', article)
        for bad in ("campus", "Campus police"):
            with self.subTest(kind=bad):
                self.write_agency(bad)
                with self.assertRaisesRegex(ContentError, r"kind: choose one of .*'campus_police'"):
                    build_site(self.root)


if __name__ == "__main__":
    unittest.main()
