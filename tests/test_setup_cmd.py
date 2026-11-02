"""Synthetic guided setup acceptance; no network or external effects."""
import ast
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from campaign_tool import cli, setup_cmd
from campaign_tool.kit import build_kit


class TTY(io.StringIO):
    def isatty(self):
        return True


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "campaign"
        self.flags = ["--name", "Synthetic Campaign", "--county", "San Luis Obispo", "--state", "CA"]

    def invoke(self, flags=None, stdin=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        argv = ["campaign_tool", "setup", "--directory", str(self.root)] + (self.flags + ["--yes"] if flags is None else flags)
        with patch("sys.argv", argv), patch("sys.stdin", stdin or io.StringIO()), redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                cli.main()
                code = 0
            except SystemExit as exc:
                code = exc.code
        return code, stdout.getvalue(), stderr.getvalue()

    def snapshot(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}

    def test_flags_full_offline_run(self):
        with patch("socket.socket", side_effect=AssertionError("network forbidden")), patch(
                "campaign_tool.kit.build_kit", wraps=build_kit) as kit:
            code, out, err = self.invoke()
        self.assertEqual(code, 0, err)
        kit.assert_called_once_with(self.root, online=False)
        cfg = json.loads((self.root / "campaign.json").read_text())
        self.assertEqual(cfg["external_sends"], "disabled")
        self.assertEqual(cfg["publication"], "manual")
        self.assertTrue((self.root / "public/index.html").is_file())
        drafts = sorted(str(p.relative_to(self.root)) for p in (self.root / "kit/requests").glob("*.md"))
        self.assertTrue(drafts)
        steps = [line for line in out.splitlines() if line.startswith("Step ")]
        self.assertEqual(len(steps), 4)
        for number, name in enumerate(("Campaign file", "Request drafts", "Starter site", "Readiness"), 1):
            self.assertIn(f"Step {number} of 4: {name} … done", steps)
        block = out.split("Drafted requests to review (nothing was sent):\n")[1].split("Open items:")[0]
        self.assertEqual(block.splitlines(), drafts)
        self.assertIn("Verify county/state", out)
        self.assertTrue(out.rstrip().endswith("sending is always your decision."))

    def test_interactive_bad_state_then_valid_and_optional_place(self):
        code, _, err = self.invoke([], TTY("Synthetic Campaign\nSan Luis Obispo\nCalif\nCA\nMorro Bay\n"))
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads((self.root / "campaign.json").read_text())["location_query"], "Morro Bay")

    def test_interactive_three_bad_states_writes_nothing(self):
        code, _, err = self.invoke([], TTY("Synthetic Campaign\nSan Luis Obispo\nCalif\nC\n123\n"))
        self.assertEqual(code, 1)
        self.assertIn("two-letter", err)
        self.assertFalse(self.root.exists())

    def test_missing_flags_non_tty_or_yes_never_prompt(self):
        for stdin, flags in ((io.StringIO(), []), (TTY(), ["--yes"]),
                             (io.StringIO(), self.flags[2:] + ["--yes"])):
            with self.subTest(flags=flags):
                code, out, err = self.invoke(flags, stdin)
                self.assertEqual(code, 2)
                self.assertIn("--name", err)
                self.assertNotIn("Campaign name:", out)
                self.assertFalse(self.root.exists())

    def test_partial_flags_prompt_only_missing_and_blank_place(self):
        code, out, err = self.invoke(["--name", "Synthetic Campaign"], TTY("San Luis Obispo\nCA\n\n"))
        self.assertEqual(code, 0, err)
        self.assertNotIn("Campaign name:", out)
        self.assertNotIn("location_query", json.loads((self.root / "campaign.json").read_text()))

    def test_cancel_and_eof_before_writes(self):
        for stdin in (TTY(""), TTY("Synthetic Campaign\n"), TTY("Synthetic Campaign\nSan Luis Obispo\nCA\n")):
            code, _, err = self.invoke([], stdin)
            self.assertEqual(code, 1)
            self.assertIn("cancelled", err)
            self.assertFalse(self.root.exists())
        with patch.object(TTY, "readline", side_effect=KeyboardInterrupt):
            code, _, err = self.invoke([], TTY())
        self.assertEqual(code, 1)
        self.assertIn("cancelled", err)
        self.assertFalse(self.root.exists())

    def test_rerun_keeps_campaign_and_organizer_draft(self):
        self.assertEqual(self.invoke()[0], 0)
        config = (self.root / "campaign.json").read_bytes()
        draft = next((self.root / "kit/requests").glob("*.md"))
        draft.write_text("Organizer edited draft.\n")
        code, out, err = self.invoke(self.flags[:-1] + ["ca", "--yes"])
        self.assertEqual(code, 0, err)
        self.assertIn("Step 1 of 4: Campaign file … kept", out)
        self.assertEqual((self.root / "campaign.json").read_bytes(), config)
        self.assertEqual(draft.read_text(), "Organizer edited draft.\n")
        self.assertTrue(list((self.root / "kit/requests.suggested").glob("*.md")))

    def test_mismatched_identity_changes_nothing(self):
        self.assertEqual(self.invoke()[0], 0)
        before = self.snapshot()
        for field, value in (("--county", "Monterey"), ("--name", "Other"), ("--state", "OR")):
            flags = self.flags.copy()
            flags[flags.index(field) + 1] = value
            code, out, err = self.invoke(flags + ["--yes"])
            self.assertEqual(code, 1)
            self.assertIn("Stopped at step 1", err)
            self.assertIn("nothing changed", err)
            self.assertNotIn("Step 2", out)
            self.assertEqual(self.snapshot(), before)

    def test_step_three_symlink_failure_then_resume(self):
        self.root.mkdir()
        target = Path(self.tmp.name) / "external"
        target.mkdir()
        (self.root / "public").symlink_to(target, target_is_directory=True)
        code, out, err = self.invoke()
        self.assertEqual(code, 1)
        self.assertIn("Stopped at step 3", err)
        self.assertIn("Step 2 of 4: Request drafts … done", out)
        self.assertNotIn("Step 4", out)
        self.assertEqual(list(target.iterdir()), [])
        (self.root / "public").unlink()
        code, out, err = self.invoke()
        self.assertEqual(code, 0, err)
        self.assertIn("Step 1 of 4: Campaign file … kept", out)

    def test_step_two_and_four_failures_stop_without_success(self):
        for module, step in (("campaign_tool.kit.build_kit", 2), ("campaign_tool.cli.doctor_report", 4)):
            with patch(module, side_effect=ValueError("synthetic failure")):
                code, out, err = self.invoke()
            self.assertEqual(code, 1)
            self.assertIn(f"Stopped at step {step}", err)
            self.assertNotIn("Setup finished", out)
        self.assertEqual(self.invoke()[0], 0)

    def test_blank_name_and_invalid_flag_state(self):
        for field, value in (("--name", "  "), ("--state", "Calif")):
            flags = self.flags.copy()
            flags[flags.index(field) + 1] = value
            code, _, err = self.invoke(flags + ["--yes"])
            self.assertEqual(code, 1)
            self.assertIn("Stopped at step 1", err)
            self.assertFalse((self.root / "campaign.json").exists())

    def test_return_contract_and_no_network_imports(self):
        output = []
        result = setup_cmd.run_setup(self.root, dict(name="Synthetic Campaign", county="San Luis Obispo", state="CA"), output.append)
        self.assertEqual(result["steps"], ["done"] * 4)
        self.assertTrue(result["requests"])
        self.assertEqual(result["open_items"], cli.doctor_report(self.root)["next_actions"])
        tree = ast.parse(Path(setup_cmd.__file__).read_text())
        imports = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        imports |= {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
        self.assertFalse(imports & {"smtplib", "imaplib", "urllib", "http", "requests", "subprocess"})
