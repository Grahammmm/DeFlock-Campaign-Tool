"""WP0 source CLI acceptance; wheel installation is additionally exercised in CI."""
import json
from pathlib import Path
import subprocess
import sys
import unittest


class WP0Acceptance(unittest.TestCase):
    def test_wp0_acceptance(self):
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.records", "version", "--json"],
                                capture_output=True, text=True, check=True)
        value = json.loads(result.stdout)
        self.assertEqual(value["package_version"], "0.1.0")
        self.assertEqual(value["installation_kind"], "source_checkout")
        self.assertEqual(len(value["commit"]), 40)
        self.assertEqual(len(value["dependency_lock_sha256"]), 64)
        self.assertEqual(value["ledger_schema_version"], 1)
        self.assertIn("canonical_ledger", value["enabled_features"])
        self.assertIn("offline_finding_gates", value["enabled_features"])
        self.assertEqual(value["runtime_services_enabled"], [])
        self.assertNotIn("scheduled_runner", value["enabled_features"])

    def test_existing_gate_commands_remain_available(self):
        for command in ("validate-findings", "reconcile-coverage", "collect-reviews", "batch-report"):
            result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.records", command, "--help"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_strict_scan_is_a_public_cli_command(self):
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.records", "scan-public", "--help"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--strict", result.stdout)
        self.assertIn("--private-index", result.stdout)


if __name__ == "__main__":
    unittest.main()
