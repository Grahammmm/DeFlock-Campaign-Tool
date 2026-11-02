"""Two independent synthetic campaigns using real offline county seed suggestions."""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from campaign_tool.cli import check_public_tree

REPO = Path(__file__).resolve().parents[1]
WRAPPER = REPO / "tests" / "fixtures" / "no_network_run.py"
EXAMPLE = REPO / "examples" / "fictional-campaign" / "content"
SEED = REPO / "campaign_tool" / "_resources" / "data" / "agencies" / "us-ca.json"
LAW_ENFORCEMENT = {"sheriff", "police", "chp"}


class SecondCountyE2ETests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-second-county-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        seed = json.loads(SEED.read_text(encoding="utf-8"))
        self.counties = {county["name"]: county for county in seed["counties"]}

    def cli(self, *args, code=0):
        result = subprocess.run(
            [sys.executable, "-B", "-W", "error::ResourceWarning", str(WRAPPER),
             *map(str, args)], cwd=REPO, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        self.assertNotIn("NETWORK_FORBIDDEN", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        return result

    def initialize(self, folder, county, state="CA", location=None):
        args = ["init", "--directory", folder, "--name", "Synthetic " + county,
                "--county", county, "--state", state]
        if location is not None:
            args += ["--location", location]
        self.cli(*args)
        return folder

    def read(self, folder, relative):
        return json.loads((folder / relative).read_text(encoding="utf-8"))

    def seed_agencies(self, county):
        return {agency["agency_id"]: agency for agency in self.counties[county]["agencies"]}

    def assert_kit(self, folder, county):
        actual = self.read(folder, "kit/agencies.json")
        expected = self.seed_agencies(county)
        self.assertEqual(len(actual), len(expected))
        self.assertEqual({agency["agency_id"] for agency in actual}, set(expected))
        for agency in actual:
            with self.subTest(agency=agency["agency_id"]):
                original = expected[agency["agency_id"]]
                self.assertEqual(agency["jurisdiction_name"], original["jurisdiction_name"])
                self.assertEqual(agency["name"], original["name"])
                self.assertIs(agency["selected"], True)
                self.assertIs(agency["verified"], False)
        drafts = {path.stem: path for path in (folder / "kit/requests").glob("*.md")}
        self.assertEqual(set(drafts), {key for key, agency in expected.items()
                                     if agency["kind"] in LAW_ENFORCEMENT})
        for key, path in drafts.items():
            text = path.read_text(encoding="utf-8")
            agency = expected[key]
            self.assertIn(f"Agency: {agency['name']} ({agency['jurisdiction_name']})", text)
            self.assertIn("DRAFT ONLY", text)
            self.assertIn("does not assert that this agency operates ALPR", text)
            # Reject any request addressed to an agency outside this county's seed.
            self.assertEqual([line for line in text.splitlines() if line.startswith("Agency: ")],
                             [f"Agency: {agency['name']} ({agency['jurisdiction_name']})"])
        summary = self.read(folder, "kit/summary.json")
        self.assertEqual(summary["location"]["county_name"], county)
        self.assertIs(summary["generated_offline"], True)
        self.assertIs(summary["sent"], False)
        return set(expected)

    def test_two_counties_end_to_end_without_cross_campaign_leakage(self):
        outputs = {}
        snapshots = {}
        for county in ("Humboldt", "Fresno"):
            with self.subTest(county=county):
                folder = self.initialize(self.root / county, county)
                self.cli("kit", "--directory", folder)
                self.assertEqual(self.read(folder, "campaign.json")["county"], county)
                outputs[county] = self.assert_kit(folder, county)
                doctor = json.loads(self.cli("doctor", "--directory", folder).stdout)
                self.assertIs(doctor["kit_built"], True)
                self.assertEqual(doctor["agencies_selected"], len(outputs[county]))
                self.assertIs(doctor["safe_to_send_automatically"], False)
                self.assertIs(doctor["production_ready"], False)
                shutil.copytree(EXAMPLE, folder / "content")
                site_path = folder / "content/site.json"
                # Only campaign-facing county text changes; evidence stays synthetic.
                site_path.write_text(site_path.read_text(encoding="utf-8").replace(
                    "Cedar County", county + " County"), encoding="utf-8")
                result = self.cli("build", "--directory", folder, "--check")
                self.assertIn("Public tree check passed", result.stdout)
                public = folder / "public"
                self.assertTrue((public / "index.html").is_file())
                self.assertIn(county, (public / "index.html").read_text(encoding="utf-8"))
                self.assertEqual(check_public_tree(public), [])
                other = "Fresno" if county == "Humboldt" else "Humboldt"
                for tree in (folder / "kit", public):
                    for path in tree.rglob("*"):
                        if path.is_file():
                            text = path.read_bytes().decode("utf-8", errors="replace")
                            self.assertNotIn("San Luis Obispo", text, str(path))
                            self.assertNotIn(other, text, str(path))
                snapshots[county] = {path.relative_to(folder): path.read_bytes()
                                     for path in folder.rglob("*") if path.is_file()}
        self.assertEqual(outputs["Humboldt"] & outputs["Fresno"],
                         set(self.seed_agencies("Humboldt")) & set(self.seed_agencies("Fresno")))
        for county, snapshot in snapshots.items():
            # Building the second campaign must not mutate the first's files.
            self.assertTrue(snapshot == {path.relative_to(self.root / county): path.read_bytes()
                                        for path in (self.root / county).rglob("*") if path.is_file()},
                            "campaign changed while building its neighbor")

    def test_case_and_space_variants_keep_same_agencies(self):
        expected = set(self.seed_agencies("Humboldt"))
        for index, variant in enumerate(("Humboldt", "humboldt ", "HUMBOLDT")):
            with self.subTest(variant=variant):
                folder = self.initialize(self.root / str(index), variant)
                self.cli("kit", "--directory", folder)
                self.assertEqual(self.read(folder, "campaign.json")["county"], variant)
                self.assertEqual(self.assert_kit(folder, "Humboldt"), expected)

    def test_unknown_county_and_unseeded_state_refuse_without_partial_kit(self):
        for county, state, message in (("Atlantis", "CA", "No county or city named"),
                                       ("Washoe", "NV", "No agency seed")):
            with self.subTest(county=county, state=state):
                folder = self.initialize(self.root / county, county, state)
                before = (folder / "campaign.json").read_bytes()
                result = self.cli("kit", "--directory", folder, code=1)
                self.assertIn(message, result.stderr)
                self.assertIn("Atlantis County" if state == "CA" else "us-nv.json", result.stderr)
                self.assertFalse((folder / "kit/agencies.json").exists())
                self.assertFalse((folder / "kit").exists())
                self.assertEqual((folder / "campaign.json").read_bytes(), before)

    def test_city_county_ambiguity_refuses_then_explicit_county_resolves(self):
        folder = self.initialize(self.root / "ambiguous", "Fresno", location="Fresno")
        before = (folder / "campaign.json").read_bytes()
        result = self.cli("kit", "--directory", folder, code=1)
        for label in ("Ambiguous location", "Fresno County", "City of Fresno"):
            self.assertIn(label, result.stderr)
        self.assertFalse((folder / "kit/agencies.json").exists())
        self.assertEqual((folder / "campaign.json").read_bytes(), before)
        explicit = self.initialize(self.root / "explicit", "Fresno", location="Fresno County")
        self.cli("kit", "--directory", explicit)
        self.assert_kit(explicit, "Fresno")

    def test_duplicate_init_preserves_existing_campaign_and_kit(self):
        folder = self.initialize(self.root / "existing", "Humboldt")
        self.cli("kit", "--directory", folder)
        snapshot = {path.relative_to(folder): path.read_bytes()
                    for path in folder.rglob("*") if path.is_file()}
        result = self.cli("init", "--directory", folder, "--name", "Replacement",
                          "--county", "Fresno", "--state", "CA", code=1)
        self.assertIn("File exists", result.stderr)
        self.assertTrue(snapshot == {path.relative_to(folder): path.read_bytes()
                                    for path in folder.rglob("*") if path.is_file()},
                        "duplicate init changed campaign files")

    def test_network_guard_negative_control(self):
        result = subprocess.run([sys.executable, "-B", str(WRAPPER), "--guard-negative-control"],
                                cwd=REPO, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 97, result.stderr)
        self.assertIn("NETWORK_FORBIDDEN", result.stderr)


if __name__ == "__main__":
    unittest.main()
