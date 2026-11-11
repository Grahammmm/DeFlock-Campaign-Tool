"""DFGL-18: records project-init creates a private project for an existing campaign and refuses anything else."""
import argparse
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from campaign_tool import cli as campaign_cli
from campaign_tool.records import campaign_config, cli, run_stages

ROOT = Path(__file__).resolve().parents[2]


def mode(path):
    return os.stat(path).st_mode & 0o777


class ProjectInitTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="project-init-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.campaign = self.tmp / "campaign"
        campaign_cli.initialize(argparse.Namespace(name="Fictland Records", county="Fictland", state="ca"), self.campaign)
        self.project = self.tmp / "project"

    def run_cli(self, *extra, project=None, campaign=None):
        return subprocess.run(
            [sys.executable, "-B", "-m", "campaign_tool.records", "project-init", "--project", str(project or self.project),
             "--campaign-root", str(campaign or self.campaign), *extra],
            cwd=ROOT, capture_output=True, text=True, timeout=120)

    def test_registered(self):
        self.assertIn("project-init", cli.COMMANDS)

    def test_init_creates_private_project_through_cli(self):
        done = self.run_cli()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(mode(self.project), 0o700)
        for name in ("campaign.json", ".campaign-root"):
            self.assertEqual(mode(self.project / name), 0o600)
        self.assertEqual((self.project / ".campaign-root").read_text(), str(self.campaign) + "\n")
        config = campaign_config.load(self.project)
        self.assertEqual((config.campaign_id, config.display_name, config.law_package),
                         ("fictland-ca", "Fictland Records", "us-ca"))
        ledger = self.project / "processing-v1" / "ledger.sqlite"
        self.assertEqual(mode(ledger.parent), 0o700)
        con = sqlite3.connect("file:%s?mode=ro" % ledger, uri=True)
        self.addCleanup(con.close)
        self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], 1)
        self.assertTrue((self.project / "portal-queue-v1" / "portal.sqlite").is_file())

    def test_campaign_id_flag_and_pointer_resolves(self):
        self.assertEqual(self.run_cli("--campaign-id", "test-one").returncode, 0)
        self.assertEqual(campaign_config.load(self.project).campaign_id, "test-one")
        args = argparse.Namespace(project=str(self.project), campaign_root=None)
        self.assertEqual(run_stages.next_steps_campaign_root(args), str(self.campaign))

    def test_bad_campaign_id_refused(self):
        done = self.run_cli("--campaign-id", "Bad_ID")
        self.assertEqual(done.returncode, 2)
        self.assertFalse(self.project.exists())

    def test_existing_nonempty_project_refused_untouched(self):
        self.project.mkdir(mode=0o700)
        (self.project / "keep.txt").write_text("mine")
        done = self.run_cli()
        self.assertEqual(done.returncode, 2)
        self.assertIn("project_exists", done.stderr)
        self.assertEqual(sorted(p.name for p in self.project.iterdir()), ["keep.txt"])

    def test_second_init_refused(self):
        self.assertEqual(self.run_cli().returncode, 0)
        before = (self.project / "campaign.json").read_bytes()
        self.assertEqual(self.run_cli("--campaign-id", "other").returncode, 2)
        self.assertEqual((self.project / "campaign.json").read_bytes(), before)

    def test_not_a_campaign_refused(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        done = self.run_cli(campaign=empty)
        self.assertEqual(done.returncode, 2)
        self.assertIn("not_a_campaign", done.stderr)
        self.assertFalse(self.project.exists())


if __name__ == "__main__":
    unittest.main()
