"""Guided setup refuses engine-local destinations, new or resumed; synthetic and offline."""
import io
import shutil
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from campaign_tool import cli, git_exposure

FLAGS = ["--name", "Synthetic Campaign", "--county", "San Luis Obispo", "--state", "CA", "--yes"]
ANSWERS = {"name": "Synthetic Campaign", "county": "San Luis Obispo", "state": "CA", "location": ""}
DOWNSTREAM = ("campaign_tool.cli.initialize", "campaign_tool.cli.read_config",
              "campaign_tool.kit.build_kit", "campaign_tool.site.build_site",
              "campaign_tool.cli.doctor_report")
MARKER = "private-sentinel-7f3a"


def invoke(root):
    out, err = io.StringIO(), io.StringIO()
    argv = ["campaign_tool", "setup", "--directory", str(root)] + FLAGS
    with patch("sys.argv", argv), patch("sys.stdin", io.StringIO()), redirect_stdout(out), redirect_stderr(err):
        try:
            cli.main()
            code = 0
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


def snapshot(root):
    result = {}
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        link = str(path.readlink()) if path.is_symlink() else None
        data = path.read_bytes() if path.is_file() and not path.is_symlink() else None
        result[str(path.relative_to(root))] = (data, link, info.st_mode, info.st_mtime_ns)
    return result


class EngineBoundaryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        self.engine = self.base / "engine"
        self.engine.mkdir()
        reference = self.base / "reference"
        self.assertEqual(invoke(reference)[0], 0)
        self.valid = (reference / "campaign.json").read_bytes()
        patcher = patch.object(git_exposure, "engine_checkout_root", return_value=self.engine)
        patcher.start()
        self.addCleanup(patcher.stop)

    def seed(self, root, data=None):
        root.mkdir(parents=True, exist_ok=True)
        (root / "campaign.json").write_bytes(self.valid if data is None else data)
        (root / "note.txt").write_text(MARKER)

    def assert_refused(self, root):
        before = snapshot(self.engine)
        code, out, err = invoke(root)
        self.assertEqual(code, 1)
        self.assertIn("outside the engine checkout", err)
        self.assertEqual(out.count("Step 1 of 4: Campaign file … stopped"), 1)
        for text in ("… kept", "Step 2", "Setup finished", MARKER, "Synthetic Campaign"):
            self.assertNotIn(text, out + err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(snapshot(self.engine), before)

    def test_existing_config_cannot_bypass_engine_guard(self):
        nested = self.engine / "a" / "camp"
        for root in (self.engine, nested):
            self.seed(root)
            self.assert_refused(root)
            self.assertFalse((root / "kit").exists() or (root / "public").exists())
            with self.assertRaises(ValueError) as caught:
                cli_run = __import__("campaign_tool.setup_cmd", fromlist=["run_setup"]).run_setup
                cli_run(root, ANSWERS, out=lambda *_: None)
            self.assertIn("outside the engine checkout", str(caught.exception))

    def test_alias_and_missing_suffix_refuse(self):
        alias = self.base / "alias"
        alias.symlink_to(self.engine)
        self.seed(self.engine / "real")
        self.assert_refused(alias / "real")
        self.assert_refused(alias / "missing" / "deeper")

    def test_refusal_precedes_config_admission(self):
        for data in (self.valid.replace(b"Synthetic Campaign", b"Other"), b"{not json " + MARKER.encode(), None):
            root = self.engine / "case"
            if data is not None:
                self.seed(root, data)
            with ExitStack() as stack:
                mocks = [stack.enter_context(patch(name)) for name in DOWNSTREAM]
                self.assert_refused(root)
            for mock in mocks:
                mock.assert_not_called()
            if data is not None:
                shutil.rmtree(root)

    def test_resolution_failure_refuses(self):
        self.seed(self.base / "elsewhere")
        from campaign_tool.setup_cmd import run_setup
        lines = []
        with patch.object(Path, "resolve", side_effect=OSError("denied")), \
                self.assertRaises(ValueError) as caught:
            run_setup(self.base / "elsewhere", ANSWERS, out=lines.append)
        self.assertIn("outside the engine checkout", str(caught.exception))
        self.assertEqual(lines, ["Step 1 of 4: Campaign file … stopped"])

    def test_sibling_lookalike_and_move_then_resume_allowed(self):
        sibling = self.base / "engine-sibling"
        self.seed(sibling)
        code, out, err = invoke(sibling)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Step 1 of 4: Campaign file … kept", out)
        self.assertIn("Setup finished", out)
        # Refused inside the engine, then moved outside and resumed with edits preserved.
        inside = self.engine / "camp"
        self.seed(inside)
        self.assert_refused(inside)
        moved = self.base / "moved"
        shutil.copytree(inside, moved)
        (moved / "note.txt").write_text("edited")
        code, out, err = invoke(moved)
        self.assertEqual(code, 0)
        self.assertEqual((moved / "note.txt").read_text(), "edited")


if __name__ == "__main__":
    unittest.main()
