"""Test CI scan-result enforcement without real credentials or scanner installs."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "tools/check_secret_report.py"


class SecretReportTests(unittest.TestCase):
    def run_report(self, data):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "report.json"
            path.write_text(json.dumps(data))
            return subprocess.run([sys.executable, str(SCRIPT), str(path)],
                                  capture_output=True, text=True, timeout=10)

    def test_empty_scan_passes(self):
        result = self.run_report({"results": {}})
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_candidate_blocks_without_printing_secret_hash(self):
        marker = "synthetic-sensitive-marker"
        result = self.run_report({"results": {"synthetic.py": [
            {"line_number": 2, "type": "SyntheticDetector", "hashed_secret": marker}]}})
        self.assertEqual(result.returncode, 1)
        self.assertIn("synthetic.py:2: SyntheticDetector", result.stdout)
        self.assertNotIn(marker, result.stdout + result.stderr)

    def test_malformed_scan_fails_closed(self):
        for data in ({}, {"results": []}, {"results": {"synthetic.py": None}}):
            with self.subTest(data=data):
                self.assertNotEqual(self.run_report(data).returncode, 0)


if __name__ == "__main__":
    unittest.main()
