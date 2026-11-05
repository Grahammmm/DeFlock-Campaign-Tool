"""DF0237-06: officially sourced California fusion centers are seeded as kind fusion_center, opt-in only.

Synthetic temporary campaigns only; no network; nothing is sent.
"""
import datetime
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
import build_agency_seed as seedtool  # noqa: E402
from campaign_tool import discovery  # noqa: E402

ALAMEDA, ORANGE, SAN_DIEGO = "06001", "06059", "06073"


def synthetic(**changes):
    row = {"name": "Synthetic Regional Center", "host": "Synthetic County Sheriff",
           "county_fips": (ALAMEDA, SAN_DIEGO),
           "county_source_url": "https://example.gov/counties",
           "host_source_url": "https://example.gov/about", "checked_on": "2026-11-04"}
    row.update(changes)
    return row


def init(root, county):
    args = [sys.executable, "-B", "-m", "campaign_tool", "init", "--directory", str(root),
            "--name", "Synthetic Campaign", "--county", county, "--state", "CA"]
    result = subprocess.run(args, cwd=REPO_ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return root


def cli(*args):
    return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *map(str, args)], cwd=REPO_ROOT,
                          capture_output=True, text=True, timeout=60)


def seed_entries(seed, kind="fusion_center"):
    return [a for c in seed["counties"] for a in c["agencies"] if a["kind"] == kind]


class SeedRowTests(unittest.TestCase):
    def test_sources_and_null_contacts(self):
        entries = seed_entries(discovery.load_seed("us-ca"))
        self.assertEqual(len(entries), sum(len(r["county_fips"]) for r in seedtool.FUSION_CENTERS))
        today = datetime.date.today()
        for entry in entries:
            self.assertIs(entry["verified"], False)
            for field in ("records_url", "records_email", "flock_transparency_slug", "muckrock_agency_id"):
                self.assertIsNone(entry[field])
            self.assertEqual(entry["portal"], {"vendor": "unknown", "url": None})
            self.assertIsNone(entry["place_fips"])
            self.assertEqual(len(entry["sources"]), 2)
            for src in entry["sources"]:
                self.assertTrue(src["url"].startswith("https://"))
                self.assertEqual(src["accessed"], src["checked_on"])
                self.assertLessEqual(datetime.date.fromisoformat(src["checked_on"]), today)
            self.assertIn("serves", entry["sources"][0]["title"])
            self.assertIn("host or governing agency", entry["sources"][1]["title"])
        orange = next(e for e in entries if e["agency_id"] == "ca-orange-county-intelligence-assessment-center-fusion-center-orange")
        self.assertIn("Orange County Sheriff's Department", orange["sources"][1]["title"])
        self.assertIn("Orange County", orange["sources"][0]["title"])
        self.assertNotEqual(orange["sources"][0]["url"], orange["sources"][1]["url"])

    def test_statewide_and_unsourced_centers_are_not_seeded(self):
        names = " ".join(e["name"] for e in seed_entries(discovery.load_seed("us-ca"))).lower()
        for omitted in ("state threat assessment", "joint regional intelligence", "law enforcement coordination",
                        "northern california regional", "central california intelligence"):
            self.assertNotIn(omitted, names)
        doc = (REPO_ROOT / "docs" / "AGENCY-DISCOVERY.md").read_text(encoding="utf-8")
        flat = " ".join(doc.split())
        self.assertIn("Fusion-center suggestions are unverified; a listing is not evidence of ALPR use or data sharing.", flat)
        for omitted in ("JRIC", "SD-LECC", "CCIC/SacRTAC", "NCRIC", "STAC"):
            self.assertIn(omitted, doc)

    def test_table_validation_and_collision(self):
        check = seedtool.check_fusion_centers
        today = datetime.date(2026, 11, 4)
        check([synthetic()], today=today)
        bad = [
            synthetic(county_fips=()), synthetic(county_fips=(ALAMEDA, ALAMEDA)), synthetic(county_fips=("06999",)),
            synthetic(county_fips=[ALAMEDA]), synthetic(name=" "), synthetic(name="x\nname"), synthetic(host=""),
            synthetic(county_source_url="http://example.gov/a"), synthetic(host_source_url="https://u:p@example.gov/a"),
            synthetic(host_source_url=None), synthetic(checked_on="2026-02-30"), synthetic(checked_on="2026-1-4"),
            synthetic(checked_on="2026-11-05"), synthetic(checked_on="GENERATED"), {"name": "Only a name"},
            dict(synthetic(), extra="x"),
        ]
        for row in bad:
            with self.assertRaises(ValueError, msg=str(row)):
                check([row], today=today)
        with self.assertRaises(ValueError):
            check([synthetic(), synthetic(name="synthetic  regional center!")], today=today)
        check([synthetic(checked_on="2024-02-29")], today=today)
        # a fusion id equal to an existing entry id is refused rather than suffixed or overwritten
        clash = synthetic(name="Alameda County", county_fips=(ALAMEDA,))
        with mock.patch.object(seedtool, "FUSION_CENTERS", [clash, dict(clash, name="Alameda County!")]):
            with self.assertRaises(ValueError):
                seedtool.check_tables()
        # two centers whose slugs differ only after the county suffix still collide on the resulting id
        with mock.patch.object(seedtool, "FUSION_CENTERS", [synthetic()]), \
                mock.patch.object(seedtool, "fusion_id", lambda row, county: "ca-alameda-county-sheriff"):
            with self.assertRaises(ValueError):
                seedtool.check_tables()

    def test_repeat_generation_is_identical_and_non_fusion_entries_unchanged(self):
        first = seedtool.render(seedtool.build(seedtool.load_overlay()))
        self.assertEqual(first, seedtool.render(seedtool.build(seedtool.load_overlay())))
        with mock.patch.object(seedtool, "FUSION_CENTERS", []):
            plain = seedtool.build(seedtool.load_overlay())
        built = json.loads(first)
        for county in built["counties"]:
            county["agencies"] = [a for a in county["agencies"] if a["kind"] != "fusion_center"]
        self.assertEqual(built, plain)


class ResolutionTests(unittest.TestCase):
    def test_multicounty_and_city_resolution(self):
        centers = [synthetic()]
        names = {}
        for fips in (ALAMEDA, SAN_DIEGO, ORANGE):
            names[fips] = [a["agency_id"] for a in seedtool.fusion_agencies(fips, centers)]
        self.assertEqual(names[ALAMEDA], ["ca-synthetic-regional-center-fusion-center-alameda"])
        self.assertEqual(names[SAN_DIEGO], ["ca-synthetic-regional-center-fusion-center-san-diego"])
        self.assertEqual(names[ORANGE], [])  # headquarters is never inferred; an unserved county gets none
        self.assertEqual(seedtool.fusion_agencies(ALAMEDA, []), [])
        with mock.patch.object(seedtool, "FUSION_CENTERS", centers):
            seed = seedtool.build(seedtool.load_overlay())
        oakland = discovery.agencies_for(discovery.locate("Oakland", "CA", seed), seed, ("police", "fusion_center"))
        self.assertEqual([a["kind"] for a in oakland][-1], "fusion_center")
        default = discovery.agencies_for(discovery.locate("Oakland", "CA", seed), seed)
        self.assertNotIn("fusion_center", [a["kind"] for a in default])
        self.assertEqual(discovery.agencies_for(discovery.locate("Orange County", "CA", seed), seed, ("fusion_center",)), [])
        order = [a["kind"] for a in discovery.agencies_for(
            discovery.locate("Alameda County", "CA", seed), seed, ("fusion_center", "sheriff"))]
        self.assertEqual(order, ["fusion_center", "sheriff"])

    def test_kind_wiring(self):
        self.assertNotIn("fusion_center", discovery.DEFAULT_INCLUDE)
        self.assertNotIn("fusion_center", discovery.CITY_KINDS)
        self.assertEqual(discovery.OPT_IN_KINDS[-1], "fusion_center")
        self.assertIn("fusion_center", discovery.LAW_ENFORCEMENT_KINDS)
        self.assertIn("fusion_center", discovery.COUNTY_KINDS)
        schema = json.loads((REPO_ROOT / "schemas" / "agency-seed.schema.json").read_text(encoding="utf-8"))
        text = json.dumps(schema)
        self.assertIn('"fusion_center"', text)
        self.assertIn("checked_on", text)


class KitTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-fusion-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)

    def kit(self, root, *include, scope=None):
        args = ["kit", "--directory", root]
        for kind in include:
            args += ["--include", kind]
        if scope:
            args += ["--scope", scope]
        return cli(*args)

    def agencies(self, root):
        return json.loads((root / "kit" / "agencies.json").read_text(encoding="utf-8"))

    def test_default_kit_unchanged_and_fusion_only_include(self):
        default = init(self.tmp / "default", "Orange")
        self.assertEqual(self.kit(default).returncode, 0)
        self.assertNotIn("fusion_center", [a["kind"] for a in self.agencies(default)])
        only = init(self.tmp / "only", "Orange")
        result = self.kit(only, "fusion_center")
        self.assertEqual(result.returncode, 0, result.stderr)
        listed = self.agencies(only)
        self.assertEqual([a["agency_id"] for a in listed],
                         ["ca-orange-county-intelligence-assessment-center-fusion-center-orange"])
        self.assertIs(listed[0]["verified"], False)
        self.assertIsNone(listed[0]["records_email"])
        drafts = {p.name for p in (only / "kit" / "requests").glob("*.md")}
        self.assertEqual(drafts, {"ca-orange-county-intelligence-assessment-center-fusion-center-orange.md"})
        # an existing agencies.json is kept byte for byte when kit is rerun with another include
        before = (only / "kit" / "agencies.json").read_bytes()
        self.assertEqual(self.kit(only, "sheriff", "fusion_center").returncode, 0)
        self.assertEqual((only / "kit" / "agencies.json").read_bytes(), before)
        # a county without a listed center gets nothing invented
        elsewhere = init(self.tmp / "elsewhere", "Alpine")
        self.assertEqual(self.kit(elsewhere, "fusion_center").returncode, 0)
        self.assertEqual(self.agencies(elsewhere), [])
        helped = cli("kit", "--help")
        self.assertIn("fusion_center (opt-in)", " ".join(helped.stdout.split()))

    def test_select_deselect_and_sharing_scope(self):
        root = init(self.tmp / "campaign", "Orange")
        self.assertEqual(self.kit(root, "fusion_center", scope="sharing").returncode, 0)
        agency_id = "ca-orange-county-intelligence-assessment-center-fusion-center-orange"
        draft = root / "kit" / "requests" / f"{agency_id}.md"
        self.assertTrue(draft.exists())
        text = draft.read_text(encoding="utf-8")
        self.assertEqual(self.kit(root, "fusion_center", scope="sharing").returncode, 0)
        self.assertEqual(draft.read_text(encoding="utf-8"), text)
        self.assertEqual(cli("kit-agency", "deselect", "--directory", root, "--id", agency_id).returncode, 0)
        self.assertEqual(self.kit(root, "fusion_center", scope="sharing").returncode, 0)
        self.assertFalse(draft.exists(), "an unedited draft for a deselected center is removed")
        self.assertEqual(cli("kit-agency", "select", "--directory", root, "--id", agency_id).returncode, 0)
        self.assertEqual(self.kit(root, "fusion_center", scope="sharing").returncode, 0)
        self.assertEqual(draft.read_text(encoding="utf-8"), text)
        package = json.loads((REPO_ROOT / "jurisdictions" / "us-ca" / "package.json").read_text(encoding="utf-8"))
        self.assertIn("sharing", json.dumps(package))
        unknown = cli("kit-agency", "select", "--directory", root, "--id", "ca-no-such-fusion-center-orange")
        self.assertNotEqual(unknown.returncode, 0)
        added = cli("kit-agency", "add", "--directory", root, "--id", "ca-synthetic-fusion-center-orange",
                    "--name", "Synthetic Fusion Center", "--kind", "fusion_center")
        self.assertEqual(added.returncode, 0, added.stderr)
        again = cli("kit-agency", "add", "--directory", root, "--id", "ca-synthetic-fusion-center-orange",
                    "--name", "Synthetic Fusion Center", "--kind", "fusion_center")
        self.assertNotEqual(again.returncode, 0)


class ResourceTests(unittest.TestCase):
    def test_resource_sync_and_frozen_snapshot(self):
        canonical = (REPO_ROOT / "data" / "agencies" / "us-ca.json").read_bytes()
        bundled = (REPO_ROOT / "campaign_tool" / "_resources" / "data" / "agencies" / "us-ca.json").read_bytes()
        self.assertEqual(canonical, bundled)
        self.assertEqual((REPO_ROOT / "schemas" / "agency-seed.schema.json").read_bytes(),
                         (REPO_ROOT / "campaign_tool" / "_resources" / "schemas" / "agency-seed.schema.json").read_bytes())
        # The Workers snapshot is generated from the seed (sync-data.mjs --check enforces it), so it carries the centers too.
        self.assertEqual(canonical, (REPO_ROOT / "workers" / "wizard" / "src" / "generated" / "agencies-us-ca.json").read_bytes())


if __name__ == "__main__":
    unittest.main()
