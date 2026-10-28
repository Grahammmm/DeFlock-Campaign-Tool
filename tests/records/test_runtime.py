"""Runtime preflight: no partial intake and real Linux publication invariants."""
import contextlib
import io
import json
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from campaign_tool.records import cli, runtime


class RuntimeTests(unittest.TestCase):
    def test_unsupported_platform_never_imports_intake_or_creates_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "missing" / "records"
            output = io.StringIO()
            with patch.object(runtime.platform, "system", return_value="Windows"), \
                    patch.object(sys, "argv", ["records", "run", "--root", str(root)]), \
                    patch.object(cli.importlib, "import_module") as load, \
                    contextlib.redirect_stdout(output):
                self.assertEqual(cli.main(), 2)
            load.assert_not_called()
            self.assertFalse(root.parent.exists())
            self.assertEqual(json.loads(output.getvalue())["failure_code"], "unsupported_records_runtime")
            self.assertNotIn(directory, output.getvalue())

    def test_manual_and_unattended_fail_before_pipeline_construction(self):
        if platform.system() == "Windows":
            self.skipTest("Linux modules cannot import fcntl on Windows; dispatcher test covers this")
        from campaign_tool.records import run, unattended
        for module, constructor in ((run, "build_pipeline"), (unattended, "UnattendedPipeline")):
            with self.subTest(module=module.__name__), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "not-created"
                with patch.object(runtime, "require", return_value=False), \
                        patch.object(module, constructor) as build:
                    self.assertEqual(module.main(["--root", str(root)]), 2)
                build.assert_not_called()
                self.assertFalse(root.exists())

    def test_filesystem_error_is_stable_and_does_not_leak_paths(self):
        if platform.system() == "Windows":
            self.skipTest("Linux module import unavailable")
        from campaign_tool.records.extract import ocr
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "not-created"
            with patch.object(runtime.platform, "system", return_value="Linux"), \
                    patch.object(ocr, "_publish_directory", side_effect=OSError("private details")):
                report = runtime.check(root)
            self.assertFalse(report["supported"])
            self.assertFalse(report["atomic_no_replace"])
            self.assertEqual(report["failure_code"], "records_filesystem_primitives_unavailable")
            self.assertNotIn("private details", json.dumps(report))
            self.assertFalse(root.exists())
            self.assertEqual(list(Path(directory).iterdir()), [])

    @unittest.skipUnless(platform.system() == "Linux", "real Linux filesystem probe runs in CI")
    def test_real_no_replace_probe_cleans_up_and_preserves_missing_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "missing" / "records"
            report = runtime.check(root)
            self.assertTrue(report["supported"], report)
            self.assertTrue(report["atomic_no_replace"])
            self.assertTrue(report["filesystem_checked"])
            self.assertEqual(list(Path(directory).iterdir()), [])

    @unittest.skipUnless(platform.system() == "Linux", "real Linux filesystem probe runs in CI")
    def test_symlink_ancestor_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "real").mkdir()
            (base / "alias").symlink_to(base / "real", target_is_directory=True)
            report = runtime.check(base / "alias" / "missing")
            self.assertFalse(report["supported"])
            self.assertEqual(list((base / "real").iterdir()), [])

    def test_doctor_command_actual_host(self):
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.records", "doctor"],
                                capture_output=True, text=True)
        report = json.loads(result.stdout)
        self.assertEqual(result.returncode, 0 if report["supported"] else 2)
        self.assertEqual(set(report["parser_versions"]), {"pypdf", "Pillow", "openpyxl", "extract-msg"})
        if platform.system() != "Linux":
            self.assertEqual(report["failure_code"], "unsupported_records_runtime")


if __name__ == "__main__":
    unittest.main()
