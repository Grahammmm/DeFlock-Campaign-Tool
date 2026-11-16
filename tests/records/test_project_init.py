"""DFGL-18: records project-init creates a private project for an existing campaign and refuses anything else."""
import argparse
import contextlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import cli as campaign_cli
from campaign_tool.records import agency_retention, campaign_config, cli, followup_clock, project_init, run_stages

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

    def starter_names(self):
        return sorted(p.name for p in self.project.iterdir())

    def test_fresh_project_exact_safe_starters(self):
        self.assertEqual(self.run_cli().returncode, 0)
        self.assertEqual(self.starter_names(), [".campaign-root", "agency-asks", "agency-retention.csv", "campaign.json",
                                                "followup-coverage", "followup-settings.json", "portal-queue-v1",
                                                "processing-v1"])
        for name in ("followup-settings.json", "agency-retention.csv", "agency-asks/README.md", "followup-coverage/README.md"):
            self.assertEqual(mode(self.project / name), 0o600, name)
            self.assertTrue((self.project / name).is_file())
        for name in ("agency-asks", "followup-coverage"):
            self.assertEqual(mode(self.project / name), 0o700)
            self.assertEqual([p.name for p in (self.project / name).iterdir()], ["README.md"])
        self.assertEqual(json.loads((self.project / "followup-settings.json").read_text()),
                         {"schema": "records-followup-settings-v1", "default": {"autonomy": "off"}, "agencies": {}})
        self.assertNotIn("signature", (self.project / "followup-settings.json").read_text())
        self.assertEqual((self.project / "agency-retention.csv").read_text(), ",".join(agency_retention.HEADER) + "\n")
        self.assertEqual(agency_retention.load(str(self.project / "agency-retention.csv")), ([], []))

    def test_starters_read_as_valid_and_off_for_any_agency(self):
        self.assertEqual(self.run_cli().returncode, 0)
        settings = followup_clock.load_settings(str(self.project))
        self.assertFalse(settings["invalid"])
        self.assertEqual((settings["default"]["autonomy"], settings["default"]["notice_after_days"],
                          settings["default"]["silence_after_days"]), ("off", 10, 0))
        for agency in ("ca-fict-a", "never-seen-before"):
            self.assertEqual(followup_clock._setting(settings, agency)["autonomy"], "off")
            self.assertNotIn("signature", followup_clock._setting(settings, agency))

    def test_empty_existing_project_supported(self):
        self.project.mkdir(mode=0o700)
        self.assertEqual(self.run_cli().returncode, 0)
        self.assertTrue((self.project / "followup-settings.json").is_file())

    def test_existing_project_with_one_starter_refused_untouched(self):
        self.project.mkdir(mode=0o700)
        (self.project / "followup-settings.json").write_text("sentinel")
        done = self.run_cli()
        self.assertEqual((done.returncode, self.starter_names()), (2, ["followup-settings.json"]))
        self.assertEqual((self.project / "followup-settings.json").read_text(), "sentinel")
        self.assertIn("project_exists", done.stderr)

    def test_second_init_leaves_edited_starters_untouched(self):
        self.assertEqual(self.run_cli().returncode, 0)
        path = self.project / "followup-settings.json"
        path.write_text('{"schema": "records-followup-settings-v1", "default": {"autonomy": "off"}, "agencies": {}} ')
        before = (path.read_bytes(), mode(path), self.starter_names())
        self.assertEqual(self.run_cli().returncode, 2)
        self.assertEqual((path.read_bytes(), mode(path), self.starter_names()), before)

    def test_private_modes_across_umasks(self):
        old = os.umask(0o022)
        self.addCleanup(os.umask, old)
        for index, mask in enumerate((0o000, 0o022, 0o077)):
            os.umask(mask)
            project = self.tmp / ("umask-%d" % index)
            project_init.init_project(str(project), str(self.campaign))
            for name in ("followup-settings.json", "agency-retention.csv", "agency-asks/README.md"):
                self.assertEqual(mode(project / name), 0o600, (oct(mask), name))
            for name in ("agency-asks", "followup-coverage"):
                self.assertEqual(mode(project / name), 0o700, (oct(mask), name))
            self.assertFalse(followup_clock.load_settings(str(project))["invalid"])

    def test_starter_io_failure_is_fixed_refusal_and_never_overwrites(self):
        outside = self.tmp / "outside.txt"
        outside.write_text("keep")
        real = os.mkdir

        def failing(path, *args, **kw):
            if Path(path).name == "agency-asks":
                raise PermissionError("secret path")
            return real(path, *args, **kw)
        with mock.patch("campaign_tool.records.project_init.os.mkdir", failing):
            with contextlib.redirect_stderr(io.StringIO()) as err, contextlib.redirect_stdout(io.StringIO()) as out:
                code = project_init.main(["--project", str(self.project), "--campaign-root", str(self.campaign)])
        self.assertEqual(code, 2)
        self.assertIn("project_starter_unavailable", err.getvalue())
        self.assertNotIn("secret path", err.getvalue())
        self.assertNotIn("Project ready", out.getvalue())
        self.assertEqual(outside.read_text(), "keep")
        self.assertEqual(self.run_cli().returncode, 2)

    def test_symlink_collision_is_not_followed(self):
        outside = self.tmp / "outside.txt"
        outside.write_text("keep")
        real = project_init._write

        def planted(path, text):
            if Path(path).name == "agency-retention.csv":
                os.symlink(outside, path)
            return real(path, text)
        with mock.patch("campaign_tool.records.project_init._write", planted):
            with self.assertRaises(project_init.Refused):
                project_init.init_project(str(self.project), str(self.campaign))
        self.assertEqual(outside.read_text(), "keep")


if __name__ == "__main__":
    unittest.main()
