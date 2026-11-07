"""Synthetic register -> cited, read-only deadline labels (DL-CALC-1)."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from campaign_tool import law
from campaign_tool.records import deadline_calc as dc
from campaign_tool.records.outcomes import register


def report(**values):
    data = {table: [] for table in register.correspondence.TABLES}
    data["agencies"] = [{"id": "alpha-pd", "name": "Alpha Police"}]
    data["requests"] = [{"id": "r1", "agency_id": "alpha-pd", "external_ref": "A-1",
                         "received_at": "2026-11-06", "submitted_at": "2026-11-05",
                         "determination_at": None, "production_at": None, "status": "open", **values}]
    return register.build_register(data, as_of="2026-12-01")


class Deadlines(unittest.TestCase):
    def setUp(self):
        self.package = law.load_package("us-ca")
        self.digest = hashlib.sha256(law.package_path("us-ca").read_bytes()).hexdigest()

    def calc(self, doc):
        return dc.deadlines(doc, self.package, self.digest)[0]

    def test_receipt_weekend_citation_extension_and_promise(self):
        # Friday +10 is Monday; Thursday +10 is Sunday (the spec's Friday/Sunday example is impossible).
        for start, due, rolled in (("2026-11-06", "2026-11-16", False),
                                   ("2026-11-05", "2026-11-16", True)):
            with self.subTest(start=start):
                result = self.calc(report(received_at=start, extension="yes", promised_at="2026-12-15"))
                self.assertEqual(result["received"], {"date": start, "label": "DATED", "note": "recorded receipt"})
                self.assertEqual(result["determination_due"]["date"], due)
                self.assertEqual("weekend-rolled" in result["determination_due"]["note"], rolled)
                self.assertEqual(result["extension_limit"]["date"], "2026-11-30")
                self.assertIn("recorded: yes", result["extension_limit"]["note"])
                self.assertEqual(result["promised"], {"date": "2026-12-15", "label": "DATED", "note": "agency-stated"})
                rule = next(r for r in self.package["rules"] if r["rule_id"] == dc.RULE)
                for name in ("determination_due", "extension_limit"):
                    entry = result[name]
                    self.assertEqual(entry["label"], "COMPUTED")
                    self.assertEqual(entry["citation"], rule["citation"])
                    self.assertTrue(entry["source_url"].startswith("https://leginfo.legislature.ca.gov/"))
                    self.assertEqual(entry["package"], "jurisdictions/us-ca/package.json sha256 " + self.digest)
                    self.assertEqual(entry["caveat"], dc.CAVEAT)

    def test_proxy_unknown_invalid_and_closed_requests_remain_visible(self):
        result = self.calc(report(received_at=None))
        self.assertEqual(result["received"]["label"], "INFERRED")
        self.assertEqual(result["determination_due"]["label"], "COMPUTED")
        self.assertIn("recorded: no", result["extension_limit"]["note"])
        for values in ({"received_at": None, "submitted_at": None},
                       {"received_at": "invalid", "submitted_at": None}):
            result = self.calc(report(**values))
            self.assertEqual([result[k]["label"] for k in ("received", "determination_due", "extension_limit", "promised")],
                             ["UNKNOWN"] * 4)
        for status in ("closed", "withdrawn", "cancelled"):
            self.assertEqual(self.calc(report(status=status))["determination_due"]["date"], "2026-11-16")

    def test_package_and_stale_register_mismatches(self):
        original = report()
        for field, value in (("determination_days", 5), ("determination_extension_days", 15), ("day_type", "business")):
            package = copy.deepcopy(self.package)
            package["records_law"][field] = value
            result = dc.deadlines(original, package, self.digest)[0]
            self.assertIn("package_register_mismatch", result["issues"])
            self.assertEqual([result[k]["label"] for k in ("determination_due", "extension_limit")], ["UNVERIFIED"] * 2)
        for field, name in (("base_due", "determination_due"), ("extended_due", "extension_limit")):
            for bad in ("2026-11-17", None, "invalid"):
                doc = copy.deepcopy(original)
                doc["requests"][0]["deadline"][field] = bad
                result = self.calc(doc)
                self.assertIn("computed_mismatch", result["issues"])
                self.assertEqual(result[name]["label"], "UNVERIFIED")
        self.assertEqual(self.calc(original)["issues"], original["requests"][0]["issues"])

    def test_cli_json_table_and_failure_paths_are_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory = root / "run-outputs" / "run-1"
            directory.mkdir(parents=True)
            path = directory / "CURRENT-REQUEST-REGISTER.json"
            doc = report(extension="yes", promised_at="2026-12-15")
            path.write_text(json.dumps(doc), encoding="utf-8")
            def snapshot():
                return {str(p.relative_to(root)): (p.stat().st_mode, p.read_bytes() if p.is_file() else None)
                        for p in root.rglob("*")}
            def cli(run="run-1", *extra):
                return subprocess.run([sys.executable, "-B", "-m", "campaign_tool.records", "deadlines",
                                       "--project", tmp, "--run-id", run, *extra], capture_output=True, text=True)
            before = snapshot()
            result = cli("run-1", "--json")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), dc.deadlines(doc, self.package, self.digest))
            table = cli()
            self.assertEqual(table.returncode, 0, table.stderr)
            for text in ("COMPUTED", "DATED", self.digest, "Gov. Code § 7922.535", dc.CAVEAT):
                self.assertIn(text, table.stdout)
            for run in ("missing", "../run-1"):
                self.assertEqual(cli(run).returncode, 1)
            self.assertEqual(snapshot(), before)
            for raw in ('{"schema":"records-request-register-v1","requests":[],"requests":[]}', "not json"):
                path.write_text(raw)
                before = snapshot()
                failed = cli("run-1", "--json")
                self.assertEqual(failed.returncode, 1)
                self.assertEqual(failed.stdout, "")
                self.assertEqual(snapshot(), before)
