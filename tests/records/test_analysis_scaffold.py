"""FLK-CMP-2: records analysis-config --scaffold writes only a candidate profile; --check-only lists what the operator must fill."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from campaign_tool.records import analysis_config

ROOT = Path(__file__).resolve().parents[2]
MISSING = ("compare_f3.applicability,portal,review.added_by,review.method_dir,review.method_python,"
           "rules_intake.f2_law_map,rules_intake.law_specs,rules_intake.policy_specs")
AGENCIES = [{"agency_id": "ca-cedar-police", "name": "Cedar Police Department (fictional)", "records_email": "sentinel-contact@example.invalid",
             "portal": {"url": "https://sentinel.example.invalid/portal"}},
            {"agency_id": "ca-birch-sheriff", "name": "Birch Sheriff (fictional)"}]


def cli(*args):
    done = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.records", "analysis-config", *args],
                          cwd=ROOT, capture_output=True, text=True, check=False)
    return done.returncode, done.stdout.strip()


def snapshot(path):
    return sorted((str(p.relative_to(path)), p.read_bytes() if p.is_file() else None) for p in Path(path).rglob("*"))


class ScaffoldTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name).resolve()
        self.root, self.campaign = base / "analysis", base / "campaign"
        self.root.mkdir()
        self.put_agencies(AGENCIES)

    def put_agencies(self, data, raw=None):
        (self.campaign / "content").mkdir(parents=True, exist_ok=True)
        (self.campaign / "content" / "agencies.json").write_text(raw if raw is not None else json.dumps(data))

    def scaffold(self, agency="ca-cedar-police", slug="cedar-police", campaign=True):
        extra = ["--campaign-root", str(self.campaign)] if campaign else []
        return cli("--scaffold", "--root", str(self.root), "--slug", slug, "--records-agency-id", agency, *extra)

    @property
    def candidate(self):
        return self.root / "profiles" / "cedar-police.json.candidate"

    def test_scaffold_then_check_only_names_every_missing_field(self):
        before = snapshot(self.campaign)
        self.assertEqual(self.scaffold(), (0, "PROFILE_SCAFFOLDED"))
        self.assertEqual(sorted(p.name for p in (self.root / "profiles").iterdir()), ["cedar-police.json.candidate"])
        self.assertEqual(self.candidate.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.root / "profiles").stat().st_mode & 0o777, 0o700)
        data = json.loads(self.candidate.read_text())
        self.assertEqual((data["schema"], data["slug"], data["records_agency_id"]), ("records-review-profile-v1", "cedar-police", "ca-cedar-police"))
        self.assertEqual((data["agency_name"], data["fact_agency"]), ("Cedar Police Department (fictional)",) * 2)
        self.assertIsNone(data["review"]["method_dir"])
        self.assertNotIn("sentinel", self.candidate.read_text())
        self.assertEqual(snapshot(self.campaign), before)
        out = cli("--check-only", "--root", str(self.root), "--slug", "cedar-police")
        self.assertEqual(out, (1, "PROFILE_INCOMPLETE " + MISSING))
        self.assertEqual(cli("--root", str(self.root), "--slug", "cedar-police"), (1, "PROFILE_MISSING"))
        self.assertEqual([p.name for p in self.root.iterdir()], ["profiles"])

    def test_campaign_root_pointer_and_missing_campaign(self):
        self.assertEqual(self.scaffold(campaign=False), (1, "SCAFFOLD_CAMPAIGN_REQUIRED"))
        self.assertFalse((self.root / "profiles").exists())
        (self.root / ".campaign-root").write_text(str(self.campaign) + "\n")
        self.assertEqual(self.scaffold(campaign=False), (0, "PROFILE_SCAFFOLDED"))

    def test_existing_candidate_live_profile_or_dangling_link_is_never_overwritten(self):
        profiles = self.root / "profiles"
        profiles.mkdir(mode=0o700)
        (profiles / "cedar-police.json").write_text("live")
        self.assertEqual(self.scaffold(), (1, "SCAFFOLD_EXISTS"))
        self.assertEqual(sorted(os.listdir(profiles)), ["cedar-police.json"])
        (profiles / "cedar-police.json").unlink()
        (profiles / "cedar-police.json.candidate").write_text("mine")
        self.assertEqual(self.scaffold(), (1, "SCAFFOLD_EXISTS"))
        self.assertEqual((profiles / "cedar-police.json.candidate").read_text(), "mine")
        (profiles / "cedar-police.json.candidate").unlink()
        os.symlink(str(self.root / "nowhere"), profiles / "cedar-police.json.candidate")
        self.assertEqual(self.scaffold(), (1, "SCAFFOLD_EXISTS"))
        self.assertFalse((self.root / "nowhere").exists())
        self.assertEqual(sorted(os.listdir(profiles)), ["cedar-police.json.candidate"])

    def test_two_simultaneous_scaffolds_create_one_complete_candidate(self):
        results = []
        run = lambda: results.append(analysis_config.scaffold_profile(self.root, "cedar-police", "ca-cedar-police", campaign_root=self.campaign))  # noqa: E731
        threads = [threading.Thread(target=run) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(results), ["PROFILE_SCAFFOLDED", "SCAFFOLD_EXISTS"])
        self.assertEqual(sorted(os.listdir(self.root / "profiles")), ["cedar-police.json.candidate"])
        json.loads(self.candidate.read_text())

    def test_bad_agency_source_and_arguments_create_nothing(self):
        cases = [("ca-nobody", None, "SCAFFOLD_AGENCY_MISSING"),
                 ("ca-cedar-police", json.dumps(AGENCIES + AGENCIES[:1]), "SCAFFOLD_AGENCIES_INVALID"),
                 ("ca-cedar-police", '[{"agency_id": "ca-cedar-police", "name": "A", "name": "B"}]', "SCAFFOLD_AGENCIES_INVALID"),
                 ("ca-cedar-police", '[{"agency_id": "ca-cedar-police", "name": NaN}]', "SCAFFOLD_AGENCIES_INVALID"),
                 ("ca-cedar-police", "[" * 5000, "SCAFFOLD_AGENCIES_INVALID"),
                 ("ca-cedar-police", json.dumps([{"agency_id": "ca-cedar-police", "name": 5}]), "SCAFFOLD_AGENCIES_INVALID"),
                 ("ca-cedar-police", " " * (2 << 20), "SCAFFOLD_AGENCIES_INVALID"),
                 ("../x", None, "SCAFFOLD_AGENCY_INVALID")]
        for agency, raw, want in cases:
            if raw is not None:
                self.put_agencies(None, raw=raw)
            self.assertEqual(self.scaffold(agency), (1, want), agency)
            self.assertEqual(list(self.root.iterdir()), [], want)
        self.assertEqual(self.scaffold(slug="Bad/Slug"), (1, "SCAFFOLD_SLUG_INVALID"))
        self.assertEqual(cli("--scaffold", "--root", str(self.root), "--slug", "cedar-police")[0], 2)
        self.assertEqual(cli("--check-only", "--root", str(self.root), "--slug", "cedar-police", "--records-agency-id", "x")[0], 2)
        (self.campaign / "content" / "agencies.json").unlink()
        self.assertEqual(self.scaffold(), (1, "SCAFFOLD_SOURCE_UNAVAILABLE"))

    def test_symlinked_root_and_repository_root_are_refused(self):
        link = self.root.parent / "link"
        os.symlink(self.root, link)
        self.assertEqual(analysis_config.scaffold_profile(link, "cedar-police", "ca-cedar-police", campaign_root=self.campaign), "SCAFFOLD_ROOT_INVALID")
        self.assertEqual(analysis_config.scaffold_profile(ROOT / "tests", "cedar-police", "ca-cedar-police", campaign_root=self.campaign), "ROOT_REFUSED")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_failed_write_leaves_no_debris(self):
        real = os.write

        def fail(fd, data):
            raise OSError(28, "full")
        os.write = fail
        try:
            out = analysis_config.scaffold_profile(self.root, "cedar-police", "ca-cedar-police", campaign_root=self.campaign)
        finally:
            os.write = real
        self.assertEqual(out, "SCAFFOLD_WRITE_FAILED")
        self.assertEqual(os.listdir(self.root / "profiles"), [])

    def test_check_only_candidate_states_and_live_precedence(self):
        self.scaffold()
        root = str(self.root)
        data = json.loads(self.candidate.read_text())
        data["portal"] = "records.example.invalid"
        self.candidate.write_text(json.dumps(data))
        out = cli("--check-only", "--root", root, "--slug", "cedar-police")
        self.assertEqual(out, (1, "PROFILE_INCOMPLETE " + MISSING.replace("portal,", "")))
        data.update(review={"method_dir": "m", "method_python": "p", "added_by": "t"},
                    rules_intake={"law_specs": [], "policy_specs": [], "extra_specs": [], "f2_law_map": "f", "sources": []},
                    compare_f3={"applicability": "a"})
        self.candidate.write_text(json.dumps(data))
        self.assertEqual(cli("--check-only", "--root", root, "--slug", "cedar-police"), (1, "PROFILE_CANDIDATE"))
        self.assertEqual(cli("--root", root, "--slug", "cedar-police"), (1, "PROFILE_MISSING"))
        self.candidate.write_text("{not json")
        self.assertEqual(cli("--check-only", "--root", root, "--slug", "cedar-police"), (1, "PROFILE_INVALID"))
        (self.root / "profiles" / "cedar-police.json").write_text('{"schema": "x"}')
        self.assertEqual(cli("--check-only", "--root", root, "--slug", "cedar-police"), (1, "PROFILE_INVALID"))
        self.assertFalse((self.root / "agencies").exists())

    def test_morro_bay_profile_still_checks_config_states(self):
        morro = json.loads((ROOT / "profiles" / "morro-bay.json").read_text())
        self.assertEqual(analysis_config._incomplete_fields(morro), [])


if __name__ == "__main__":
    unittest.main()
