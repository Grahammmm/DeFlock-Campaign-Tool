"""DFGL-06B: records analysis-config builds a complete, stable flock_review CONFIG from an operator profile."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from campaign_tool.records import analysis_config, cli
from tests.flock import test_review

ROOT = Path(__file__).resolve().parents[2]
SLUG = test_review.SLUG
PROFILE = ROOT / "tests" / "fixtures" / "records" / "analysis-profile.json"
SHA = ("a" * 64, "b" * 64, "c" * 64)


class AnalysisConfigTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.method = self.tmp / "method"
        for name in " ".join(test_review.SCRIPTS.values()).split():
            test_review.write_stub(self.method, name)
        self.root = self.fresh_root("root")

    def fresh_root(self, name, **edits):
        root = self.tmp / name
        (root / "profiles").mkdir(parents=True, mode=0o700)
        profile = json.loads(PROFILE.read_text().replace("@ROOT@", str(root)).replace("@METHOD@", str(self.method))
                             .replace("@PY@", sys.executable))
        profile.update(edits)
        (root / "profiles" / (SLUG + ".json")).write_text(json.dumps(profile))
        return root

    def add_text(self, root, sha):
        path = root / "agencies" / SLUG / "review" / "inputs" / "records" / (sha + ".txt")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[[PAGE 1]]\nfictional record text\n")
        return path

    def config_path(self, root):
        return root / "agencies" / SLUG / "review-config.json"

    def flock_check(self, root):
        code, out = test_review.tool("--check", self.config_path(root))
        self.assertEqual(code, 0, out)
        self.assertIn("errors=0", out)

    def test_config_written_passes_flock_check_with_record_sources(self):
        paths = [self.add_text(self.root, sha) for sha in SHA[:2]]
        self.assertEqual(analysis_config.write_config(self.root, SLUG, created_date="2026-11-11"), "CONFIG_WRITTEN")
        self.flock_check(self.root)
        cfg = json.loads(self.config_path(self.root).read_text())
        facts = cfg["facts_intake"]
        self.assertEqual(facts["extra_sources"], [
            {"id": "REC-" + sha[:12], "title": "record " + sha[:12], "path": str(path), "kind": "record_text",
             "page_marker_regex": r"^\[\[PAGE (\d+)\]\]\s*$"} for sha, path in zip(SHA, paths)])
        self.assertEqual(facts["source_manifest"], str(paths[0].parent / "MANIFEST.csv"))
        self.assertEqual(cfg["review"]["run_date"], "2026-11-11")
        self.assertEqual(self.config_path(self.root).stat().st_mode & 0o777, 0o600)

    def test_rerun_is_unchanged_and_byte_identical_with_a_stable_run_date(self):
        self.add_text(self.root, SHA[0])
        analysis_config.write_config(self.root, SLUG, created_date="2026-11-11")
        before = self.config_path(self.root).read_bytes()
        self.assertEqual(analysis_config.write_config(self.root, SLUG, created_date="2026-12-24"), "CONFIG_UNCHANGED")
        self.assertEqual(self.config_path(self.root).read_bytes(), before)

    def test_new_text_changes_only_facts_intake(self):
        self.add_text(self.root, SHA[0])
        analysis_config.write_config(self.root, SLUG, created_date="2026-11-11")
        before = json.loads(self.config_path(self.root).read_text())
        self.add_text(self.root, SHA[1])
        self.assertEqual(analysis_config.write_config(self.root, SLUG, created_date="2026-12-24"), "CONFIG_WRITTEN")
        after = json.loads(self.config_path(self.root).read_text())
        self.assertEqual([k for k in before if before[k] != after[k]], ["facts_intake"])
        self.assertEqual(before["review"], after["review"])
        self.assertEqual(after["review"]["run_date"], "2026-11-11")
        self.assertEqual(len(after["facts_intake"]["extra_sources"]), 2)
        self.flock_check(self.root)

    def test_missing_profile_and_unknown_keys_write_nothing(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        self.assertEqual(analysis_config.write_config(empty, SLUG, created_date="2026-11-11"), "PROFILE_MISSING")
        self.assertEqual(list(empty.iterdir()), [])
        for n, edits in enumerate(({"surprise": 1}, {"review": {"method_dir": "x", "method_python": "y", "added_by": "z", "extra": 1}})):
            root = self.fresh_root("bad-%d" % n, **edits)
            self.assertEqual(analysis_config.write_config(root, SLUG, created_date="2026-11-11"), "PROFILE_INVALID")
            self.assertFalse((root / "agencies").exists())

    def test_method_dir_without_a_script_keeps_the_previous_config(self):
        self.add_text(self.root, SHA[0])
        analysis_config.write_config(self.root, SLUG, created_date="2026-11-11")
        before = self.config_path(self.root).read_bytes()
        self.add_text(self.root, SHA[1])
        (self.method / "compare" / "scripts" / "summarize.py").unlink()
        self.assertEqual(analysis_config.write_config(self.root, SLUG, created_date="2026-11-11"), "CONFIG_INVALID review.method_dir")
        self.assertEqual(self.config_path(self.root).read_bytes(), before)

    def test_flock_store_and_repository_roots_are_refused(self):
        (self.root / "LAW.csv").write_text("x\n")
        self.assertEqual(analysis_config.write_config(self.root, SLUG, created_date="2026-11-11"), "ROOT_REFUSED")
        store = self.fresh_root("store")
        (store / "agencies" / "other").mkdir(parents=True)
        (store / "agencies" / "other" / "FACTS.csv").write_text("x\n")
        self.assertEqual(analysis_config.write_config(store, SLUG, created_date="2026-11-11"), "ROOT_REFUSED")
        self.assertEqual(analysis_config.write_config(ROOT / "tests", SLUG, created_date="2026-11-11"), "ROOT_REFUSED")
        self.assertFalse(self.config_path(self.root).exists())

    def test_cli_command_builds_the_same_config(self):
        self.assertEqual(cli.COMMANDS["analysis-config"], ("analysis_config", "main"))
        self.add_text(self.root, SHA[0])
        self.add_text(self.root, SHA[1])
        run = lambda *extra: subprocess.run(  # noqa: E731
            [sys.executable, "-B", "-m", "campaign_tool.records", "analysis-config", "--root", str(self.root), "--slug", SLUG, *extra],
            cwd=ROOT, capture_output=True, text=True, check=False)
        done = run("--check-only")
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "CONFIG_PENDING"), done.stderr)
        self.assertFalse(self.config_path(self.root).exists())
        done = run()
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "CONFIG_WRITTEN"), done.stderr)
        self.flock_check(self.root)
        self.assertEqual(len(json.loads(self.config_path(self.root).read_text())["facts_intake"]["extra_sources"]), 2)
        self.assertEqual(run().stdout.strip(), "CONFIG_UNCHANGED")


if __name__ == "__main__":
    unittest.main()
