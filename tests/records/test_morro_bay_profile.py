"""PROFILE-MB-1: the candidate profiles/morro-bay.json (records-review-profile-v1) builds a flock_review-valid CONFIG in a sandbox."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from campaign_tool.records import analysis_config
from tests.flock import test_review

ROOT = Path(__file__).resolve().parents[2]
PROFILE = ROOT / "profiles" / "morro-bay.json"
SLUG = "morro-bay"
ANNOTATION = '"scan_note": "# pragma: allowlist secret - public document hash, not a credential'


class MorroBayProfileTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        # the profile names its method as ~/agent-work/skills/agency-review: the sandbox HOME holds stub scripts there
        self.home = self.tmp / "home"
        method = self.home / "agent-work" / "skills" / "agency-review"
        for name in " ".join(test_review.SCRIPTS.values()).split():
            test_review.write_stub(method, name)
        self.root = self.tmp / "analysis-root"
        (self.root / "profiles").mkdir(parents=True, mode=0o700)
        shutil.copyfile(PROFILE, self.root / "profiles" / (SLUG + ".json"))
        self.env = dict(os.environ, HOME=str(self.home))

    def cli(self, *args, slug=SLUG):
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool.records", "analysis-config", "--root", str(self.root),
                               "--slug", slug, *args], cwd=ROOT, env=self.env, capture_output=True, text=True, check=False)

    def config(self):
        return json.loads((self.root / "agencies" / SLUG / "review-config.json").read_text())

    def test_check_prints_config_written_and_the_config_passes_flock_review_check(self):
        done = self.cli("--check")
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "CONFIG_WRITTEN"), done.stderr)
        path = self.root / "agencies" / SLUG / "review-config.json"
        check = subprocess.run([sys.executable, "-B", str(ROOT / "tools" / "flock_review.py"), "--check", str(path)],
                               env=self.env, capture_output=True, text=True, check=False)
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
        self.assertIn("errors=0", check.stdout)
        self.assertEqual(self.cli("--check").stdout.strip(), "CONFIG_UNCHANGED")

    def test_check_only_still_writes_nothing_and_the_two_flags_exclude_each_other(self):
        done = self.cli("--check-only")
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "CONFIG_PENDING"), done.stderr)
        self.assertFalse((self.root / "agencies").exists())
        both = self.cli("--check", "--check-only")
        self.assertEqual(both.returncode, 2)
        self.assertFalse((self.root / "agencies").exists())

    def test_a_slug_without_a_profile_prints_profile_missing(self):
        done = self.cli("--check", slug="no-method-file-city")
        self.assertEqual((done.returncode, done.stdout.strip()), (1, "PROFILE_MISSING"), done.stderr)
        self.assertFalse((self.root / "agencies").exists())

    def test_config_carries_the_method_file_values(self):
        self.assertEqual(self.cli("--check").stdout.strip(), "CONFIG_WRITTEN")
        cfg, profile = self.config(), json.loads(PROFILE.read_text())
        self.assertEqual(cfg["review"]["slug"], SLUG)
        self.assertEqual(cfg["review"]["method_dir"], "~/agent-work/skills/agency-review")
        self.assertEqual(cfg["facts_intake"]["fact_agency"], "Morro Bay")
        self.assertEqual(cfg["facts_intake"]["portal"], profile["portal"])  # the real portal host is never committed (public-scan portal_host)
        self.assertEqual(cfg["not_posted_analysis_only_ids"], profile["not_posted_analysis_only_ids"])
        self.assertEqual(len(cfg["not_posted_analysis_only_ids"]), 8)
        self.assertEqual(cfg["compare"]["f3"]["slug"], SLUG)
        rules = cfg["rules_intake"]
        self.assertEqual((rules["added_by"], rules["added_at"], rules["agency_name"]), ("opus-coder-bopus", "2026-10-30", "Morro Bay"))
        self.assertEqual([s["id"] for s in rules["sources"]], ["civ", "gov", "doj", "mb-435-2020", "mb-435-2025", "mb-435-2026", "slo-503"])
        comparison = [s["id"] for s in rules["sources"] if s.get("comparison_only")]
        self.assertEqual(comparison, ["slo-503"])

    def test_profile_is_portable_and_adopts_nothing(self):
        text = PROFILE.read_text()
        profile = json.loads(text)
        analysis_config._validate(profile, SLUG)  # frozen shape: no field outside records-review-profile-v1
        self.assertNotIn("/home/", text)
        # the only sanctioned use of the word is the inline scanner annotation on a provenance-hash line (detect-secrets has no JSON comments)
        bare = []
        for line in text.splitlines():
            if ANNOTATION in line:
                self.assertRegex(line, r'^\s*"orig_sha256": "[0-9a-f]{64}", ' + re.escape(ANNOTATION) + r'",$')
                line = ""
            bare.append(line)
        self.assertNotRegex("\n".join(bare), r"(?i)\b(?:password|token|secret|api[_-]?key)\b")
        self.assertEqual(profile["schema"], "records-review-profile-v1")
        for source in profile["rules_intake"]["sources"]:
            if "orig_sha256" in source:
                self.assertRegex(source["orig_sha256"], r"^[0-9a-f]{64}$")
            if source["kind"] == "policy":
                self.assertEqual(source["adoption_evidence"], "unverified")  # adoption is never claimed here
        for key in ("roster_doc_id", "training_join", "ocr_log_doc_ids", "region_config"):
            self.assertNotIn(key, profile["review"])  # the lane config carries no record roots or roster file for them

    def test_committed_portal_is_a_placeholder_and_the_operator_copy_carries_the_real_host(self):
        profile = json.loads(PROFILE.read_text())
        self.assertTrue(profile["portal"].endswith(".example.invalid"), profile["portal"])
        profile["portal"] = "records.example.invalid"  # stands for the host the operator sets in the copy under <root>/profiles/
        (self.root / "profiles" / (SLUG + ".json")).write_text(json.dumps(profile))
        self.assertEqual(self.cli("--check").stdout.strip(), "CONFIG_WRITTEN")
        self.assertEqual(self.config()["facts_intake"]["portal"], "records.example.invalid")

    def test_every_provenance_hash_line_carries_the_scanner_annotation(self):
        lines = [line for line in PROFILE.read_text().splitlines() if '"orig_sha256"' in line]
        self.assertEqual(len(lines), 5)
        for line in lines:
            self.assertIn(ANNOTATION, line)  # without it detect-secrets fails the wp5 credential scan

    def test_rules_intake_defaults_added_by_and_date_for_a_profile_without_them(self):
        profile = json.loads(PROFILE.read_text())
        for key in ("added_by", "added_at"):
            del profile["rules_intake"][key]
        config = analysis_config.build_config(self.root, profile, created_date="2026-11-14")
        self.assertEqual((config["rules_intake"]["added_by"], config["rules_intake"]["added_at"]), ("sonnet-coder", "2026-11-14"))
        self.assertTrue(re.fullmatch(r"\d{4}-\d{2}-\d{2}", config["review"]["run_date"]))


if __name__ == "__main__":
    unittest.main()
