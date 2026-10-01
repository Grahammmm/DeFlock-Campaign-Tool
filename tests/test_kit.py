"""End-to-end offline tests for campaign_tool.kit and the `kit` CLI subcommand.

Synthetic temporary campaigns only; no network; nothing is sent.
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import kit  # noqa: E402


def init(root, county="San Luis Obispo", location=None):
    args = [sys.executable, "-B", "-m", "campaign_tool", "init", "--directory", str(root),
            "--name", "Synthetic Campaign", "--county", county, "--state", "CA"]
    if location:
        args += ["--location", location]
    result = subprocess.run(args, cwd=REPO_ROOT, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    return root


class KitBuildTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="synthetic-kit-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = init(Path(self.temporary.name) / "campaign")

    def read(self, name):
        return json.loads((self.root / "kit" / name).read_text(encoding="utf-8"))

    def test_county_kit_files_counts_and_safety(self):
        summary = kit.build_kit(self.root)
        kit_dir = self.root / "kit"
        for name in ("agencies.json", "governing-bodies.json", "law.json", "summary.json", "README.md"):
            self.assertTrue((kit_dir / name).is_file(), name)
        self.assertEqual(summary["location"]["county_fips"], "06079")
        self.assertEqual(summary["location"]["match_kind"], "county")
        self.assertEqual(summary["agencies_total"], 18)
        self.assertEqual(summary["agencies_selected"], 18)
        self.assertEqual(summary["requests_drafted"], 9)
        self.assertEqual(summary["governing_bodies"], 9)
        self.assertIs(summary["sent"], False)
        self.assertEqual(summary["law_package_status"], "draft")
        requests = sorted(p.name for p in (kit_dir / "requests").glob("*.md"))
        self.assertEqual(len(requests), 9)
        self.assertIn("ca-san-luis-obispo-county-sheriff.md", requests)
        self.assertIn("ca-san-luis-obispo-county-chp.md", requests)
        for path in (kit_dir / "requests").glob("*.md"):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("<", text, path.name)
            self.assertIn("DRAFT ONLY", text)
            self.assertIn("## ALPR vendor agreements and funding", text)
            self.assertIn("does not assert that this agency operates ALPR", text)
        self.assertTrue(all(a["selected"] is True and a["suggested"] is True
                            for a in self.read("agencies.json")))
        self.assertTrue(all(a["kind"] in ("county_board", "city_council", "district_attorney")
                            for a in self.read("governing-bodies.json")))
        self.assertEqual(self.read("law.json")["status"], "draft")
        self.assertIn("Nothing in this folder has been sent", (kit_dir / "README.md").read_text())
        cfg = json.loads((self.root / "campaign.json").read_text())
        self.assertEqual(cfg["jurisdiction"], "us-ca")
        self.assertEqual(cfg["location"], {"county_fips": "06079", "county_name": "San Luis Obispo",
                                           "place_fips": None, "place_name": None,
                                           "match_kind": "county"})
        self.assertEqual(cfg["county"], "San Luis Obispo")

    def test_rerun_preserves_organizer_edits_and_reports_diff(self):
        kit.build_kit(self.root)
        path = self.root / "kit" / "agencies.json"
        agencies = json.loads(path.read_text())
        for entry in agencies:
            if entry["agency_id"] == "ca-morro-bay-police":
                entry["selected"] = False
                entry["records_email"] = "records@example.invalid"
        agencies.append({"agency_id": "ca-synthetic-transit-police", "name": "Synthetic Transit Police",
                         "kind": "police", "selected": True})
        before = json.dumps(agencies, indent=2) + "\n"
        path.write_text(before)
        summary = kit.build_kit(self.root)
        self.assertEqual(path.read_text(), before)
        self.assertIs(summary["agencies_preserved"], True)
        self.assertEqual(summary["suggestion_diff_count"], 1)
        self.assertTrue((self.root / "kit" / "agencies.suggested.json").is_file())
        requests = {p.name for p in (self.root / "kit" / "requests").glob("*.md")}
        self.assertNotIn("ca-morro-bay-police.md", requests)
        self.assertIn("ca-synthetic-transit-police.md", requests)
        self.assertEqual(summary["agencies_selected"], 18)

    def test_city_location_query_and_include_filter(self):
        root = init(Path(self.temporary.name) / "city", location="Morro Bay")
        cfg = json.loads((root / "campaign.json").read_text())
        self.assertEqual(cfg["location_query"], "Morro Bay")
        summary = kit.build_kit(root, include=("police", "sheriff", "city_council"))
        self.assertEqual(summary["location"]["place_name"], "Morro Bay")
        self.assertEqual(summary["location"]["match_kind"], "city")
        ids = [a["agency_id"] for a in json.loads((root / "kit" / "agencies.json").read_text())]
        self.assertEqual(ids, ["ca-morro-bay-police", "ca-morro-bay-city-council",
                               "ca-san-luis-obispo-county-sheriff"])
        self.assertEqual(summary["requests_drafted"], 2)

    def test_missing_law_package_still_builds_with_banner(self):
        empty = Path(self.temporary.name) / "no-law"
        empty.mkdir()
        summary = kit.build_kit(self.root, law_base=empty)
        self.assertEqual(summary["law_package_status"], "missing")
        self.assertEqual(self.read("law.json")["status"], "missing")
        text = (self.root / "kit" / "requests" / "ca-morro-bay-police.md").read_text()
        self.assertIn("NO REVIEWED LAW PACKAGE", text)
        self.assertIn("Morro Bay Police Department", text)
        self.assertNotIn("<", text)

    def test_ambiguous_county_field_fails_clearly(self):
        cfg_path = self.root / "campaign.json"
        cfg = json.loads(cfg_path.read_text())
        cfg["location_query"] = "San Luis Obispo"
        cfg_path.write_text(json.dumps(cfg))
        with self.assertRaises(ValueError) as caught:
            kit.build_kit(self.root)
        self.assertIn("Ambiguous", str(caught.exception))
        self.assertFalse((self.root / "kit" / "summary.json").exists())

    def test_rejects_angle_brackets_in_organizer_agency_names(self):
        kit.build_kit(self.root)
        path = self.root / "kit" / "agencies.json"
        agencies = json.loads(path.read_text())
        agencies[0]["name"] = "<script>alert(1)</script>"
        path.write_text(json.dumps(agencies))
        with self.assertRaises(ValueError):
            kit.build_kit(self.root)


class KitCLITests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="synthetic-kit-cli-")
        self.addCleanup(self.temporary.cleanup)
        self.root = init(Path(self.temporary.name) / "campaign")

    def run_cli(self, *arguments, returncode=0):
        result = subprocess.run(
            [sys.executable, "-B", "-m", "campaign_tool", *map(str, arguments)],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, returncode, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        return result

    def test_kit_then_doctor(self):
        before = json.loads(self.run_cli("doctor", "--directory", self.root).stdout)
        self.assertEqual(before["agencies_selected"], 0)
        self.assertIs(before["kit_built"], False)
        result = self.run_cli("kit", "--directory", self.root, "--include", "sheriff",
                              "--include", "police")
        self.assertIn("Nothing was sent", result.stdout)
        self.assertEqual(result.stderr, "")
        summary = json.loads(result.stdout.rsplit("}\n", 1)[0] + "}")
        self.assertEqual(summary["agencies_total"], 8)
        self.assertEqual(summary["include"], ["sheriff", "police"])
        after = json.loads(self.run_cli("doctor", "--directory", self.root).stdout)
        self.assertEqual(after["agencies_selected"], 8)
        self.assertIs(after["kit_built"], True)
        self.assertIs(after["safe_to_send_automatically"], False)

    def test_kit_unknown_location_stops_without_traceback(self):
        root = init(Path(self.temporary.name) / "unknown", county="Nowhere")
        result = self.run_cli("kit", "--directory", root, returncode=1)
        self.assertIn("Stopped:", result.stderr)
        self.assertIn("Nowhere County", result.stderr)


if __name__ == "__main__":
    unittest.main()
