"""RF0845-1: refuse control-bearing campaign directory operands before any checklist advice is built."""
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from campaign_tool.doctor_checklist import checklist, render

REPO = Path(__file__).resolve().parents[1]
MESSAGE = "checklist directory contains a control character; use a control-free path to this campaign"
REPORT = {"agencies_selected": 0, "kit_built": False,
          "stale_contacts": [{"agency_id": "synthetic-agency"}],
          "stale_comment_kits": [{"meeting": "m2"}, {"meeting": "m1"}, {"meeting": "m1"}],
          "safe_to_send_automatically": False}
ESC_LF = "synthetic-campaign\x1b[2J\n[ok] Forged readiness row"
CONTROLS = [chr(c) for c in (*range(0, 32), *range(127, 160), 0x2028, 0x2029)]


def snapshot(root):
    return {str(p.relative_to(root)): (p.stat().st_mode, p.stat().st_mtime_ns, p.read_bytes() if p.is_file() else None)
            for p in [root, *sorted(root.rglob("*"))]}


def commands(rows):
    return [shlex.split(command) for row in rows for command in re.findall(
        r"`(python3 -m campaign_tool [^`]+)`", row["next"] or "")]


class DirectoryControlTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-checklist-controls-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.enterContext(mock.patch("socket.socket.connect", side_effect=AssertionError("network used")))
        self.enterContext(mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used")))

    def campaign(self, name):
        """A valid synthetic campaign whose real directory name is `name`."""
        self.count = getattr(self, "count", 0) + 1
        plain = self.base / f"plain-campaign-{self.count}"
        self.assertEqual(self.cli("init", "--directory", str(plain), "--name", "Synthetic Campaign",
                                  "--county", "Synthetic County", "--state", "ca").returncode, 0)
        root = self.base / name
        plain.rename(root)
        return root

    def cli(self, *args):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", TMPDIR=str(self.base))
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *args], cwd=REPO, env=env,
                              capture_output=True, text=True, errors="surrogateescape", timeout=120)

    def assert_refused(self, call):
        with self.assertRaises(ValueError) as caught:
            call()
        self.assertEqual(str(caught.exception), MESSAGE)

    def test_directory_control_matrix_refuses_all_entry_points(self):
        git = self.enterContext(mock.patch("campaign_tool.git_exposure.git_exposure_row", return_value=None))
        for control in CONTROLS:
            for directory in (control + "campaign", "cam" + control + "paign", "campaign" + control):
                for form in (directory, Path(directory)):
                    with self.subTest(code=ord(control), position=directory.index(control), form=type(form).__name__):
                        self.assert_refused(lambda: checklist(REPORT, form))
                        self.assert_refused(lambda: render(REPORT, form))
                        self.assert_refused(lambda: render(REPORT, form, as_json=True))
        git.assert_not_called()

    def test_real_control_directory_cli_has_no_success_output(self):
        root = self.campaign(ESC_LF)
        before = snapshot(self.base)
        for extra in ((), ("--json",)):
            with self.subTest(extra=extra):
                done = self.cli("doctor", "--directory", str(root), "--checklist", *extra)
                self.assertEqual(done.returncode, 1)
                self.assertEqual(done.stdout, "")
                self.assertEqual(done.stderr, "Stopped: " + MESSAGE + "\n")
        self.assertEqual(snapshot(self.base), before)

    def test_other_real_control_names_are_refused(self):
        for name in ("lf\ncampaign", "cr\rcampaign", "tab\tcampaign", "c1\u0085campaign"):
            with self.subTest(name=ascii(name)):
                root = self.campaign(name)
                done = self.cli("doctor", "--directory", str(root), "--checklist")
                self.assertEqual((done.returncode, done.stdout, done.stderr), (1, "", "Stopped: " + MESSAGE + "\n"))

    def test_preflight_does_not_depend_on_visible_todos(self):
        git = self.enterContext(mock.patch("campaign_tool.git_exposure.git_exposure_row", return_value=None))
        reports = ({}, {"safe_to_send_automatically": False}, {"kit_error": "x", "agencies_selected": 0},
                   {"config_readable": True, "jurisdiction_verified": True, "kit_built": True,
                    "stale_contacts": [], "stale_comment_kits": []}, {"surprise_key": 1})
        for report in reports:
            for as_json in (False, True):
                with self.subTest(report=sorted(report), as_json=as_json):
                    self.assert_refused(lambda: checklist(report, ESC_LF))
                    self.assert_refused(lambda: render(report, ESC_LF, as_json=as_json))
        git.assert_not_called()

    def test_invalid_directory_refuses_before_git_row_lookup_and_accepted_keeps_input(self):
        with mock.patch("campaign_tool.git_exposure.git_exposure_row", return_value=None) as git:
            for as_json in (False, True):
                self.assert_refused(lambda: render(REPORT, "bad\ndir", as_json=as_json))
            git.assert_not_called()
            render(REPORT, "-ok campaign", as_json=True)
            render(REPORT, "-ok campaign")
            self.assertEqual(git.call_args_list, [mock.call("-ok campaign")] * 2)

    def test_all_advice_branches_keep_accepted_operand_identity(self):
        for name in ("D", "a b", "a'b", "a;$HOME", "a\\b", "-campaign", "--directory", "é 🙂", "~/campaign"):
            for exists, count in ((False, 5), (True, 7)):
                with self.subTest(name=name, agency_list_exists=exists):
                    emitted = commands(checklist(REPORT, name, exists))
                    self.assertEqual(len(emitted), count)
                    expected = "./" + name if name.startswith("-") else name
                    for command in emitted:
                        self.assertEqual(command[command.index("--directory") + 1], expected)
                    self.assertEqual([c[c.index("--id") + 1] for c in emitted if c[3] == "meetings-stage"],
                                     ["m1", "m2"])

    def test_accepted_path_output_is_byte_identical(self):
        operands = {"D": "D", "/tmp/campaign": "/tmp/campaign", "../campaign": "../campaign",
                    "./-campaign": "./-campaign", "-campaign": "./-campaign", "--campaign": "./--campaign",
                    "~/campaign": "'~/campaign'", "caf\u00e9 \U0001f642": "'caf\u00e9 \U0001f642'",
                    "a'b": "'a'\"'\"'b'", "a\\b": "'a\\b'"}
        with mock.patch("campaign_tool.git_exposure.git_exposure_row", return_value=None):
            for directory, operand in operands.items():
                with self.subTest(directory=directory):
                    expected = "Build the kit: `python3 -m campaign_tool kit --directory " + operand + "`"
                    rows = checklist({"kit_built": False}, directory)
                    self.assertEqual(rows[0]["next"], expected)
                    self.assertEqual(checklist({"kit_built": False}, directory), rows)  # no accumulating prefix
                    text = render({"kit_built": False}, directory)
                    self.assertEqual(text, "[todo] Kit built\n       \u2192 " + expected + " (organizer)\n"
                                           "[info] Production ready: not yet, 1 to do; ready only when every todo "
                                           "above is done\n1 to do \u00b7 0 ok \u00b7 not a legal status")
                    self.assertEqual(json.loads(render({"kit_built": False}, directory, as_json=True)), rows)

    def test_refusal_and_success_are_read_only(self):
        root = self.campaign("synthetic-campaign")
        neighbor = self.base / "neighbor"
        neighbor.mkdir()
        (neighbor / "sentinel").write_bytes(b"synthetic neighbor must not change\n")
        before = (snapshot(root), snapshot(neighbor))
        ok = self.cli("doctor", "--directory", str(root), "--checklist")
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertIn("[todo]", ok.stdout)
        self.assertNotIn("\x1b", ok.stdout)
        bad = self.base / "bad\ncampaign"
        os.symlink(root, bad)  # a control-bearing alias of the same campaign is refused ...
        refused = self.cli("doctor", "--directory", str(bad), "--checklist", "--json")
        self.assertEqual((refused.returncode, refused.stdout), (1, ""))
        bad.unlink()
        again = self.cli("doctor", "--directory", str(root), "--checklist")  # ... the safe path stays usable
        self.assertEqual((again.returncode, again.stdout), (0, ok.stdout))
        self.assertEqual((snapshot(root), snapshot(neighbor)), before)
        self.assertFalse((root / "public").exists())

    def test_plain_doctor_is_not_restricted(self):
        root = self.campaign(ESC_LF)
        done = self.cli("doctor", "--directory", str(root))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn("\x1b", done.stdout)
        self.assertIn("jurisdiction_verified", done.stdout)


if __name__ == "__main__":
    unittest.main()
