"""Synthetic register -> cited, read-only deadline labels (DL-CALC-1)."""
import copy
from datetime import date
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


def report(holiday=("absent", frozenset(), None), **values):
    data = {table: [] for table in register.correspondence.TABLES}
    data["agencies"] = [{"id": "alpha-pd", "name": "Alpha Police"}]
    data["requests"] = [{"id": "r1", "agency_id": "alpha-pd", "external_ref": "A-1",
                         "received_at": "2026-11-06", "submitted_at": "2026-11-05",
                         "determination_at": None, "production_at": None, "status": "open", **values}]
    return register.build_register(data, as_of="2026-12-01", holiday=holiday)


def listed(*days):
    return "used", frozenset(date.fromisoformat(d) for d in days), "0123456789ab"


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

    def test_custodian_holiday_labels_and_dishonest_dates(self):
        # Contract: a date rolled past a listed holiday is COMPUTED with the custodian note; the weekend-only
        # note stays for weekend moves; anything else (shifted date, missing or doctored snapshot) is UNVERIFIED.
        note = "holiday-rolled (custodian list)"
        cases = (("2026-11-16", (), "2026-11-26", ""),                     # no file: Thursday, unchanged
                 ("2026-11-16", ("2026-11-26",), "2026-11-27", note),     # Thursday holiday
                 ("2026-12-15", ("2026-12-25",), "2026-12-28", note),     # Friday holiday, then the weekend
                 ("2026-11-16", ("2026-11-26", "2026-11-27"), "2026-11-30", note),  # two in a row
                 ("2026-11-05", ("2026-11-26",), "2026-11-16", "weekend-rolled"))  # unrelated list, weekend only
        for start, days, due, expect in cases:
            with self.subTest(start=start, days=days):
                doc = report(listed(*days) if days else ("absent", frozenset(), None), received_at=start)
                entry = self.calc(doc)["determination_due"]
                self.assertEqual((entry["date"], entry["label"]), (due, "COMPUTED"))
                self.assertTrue(entry["note"].endswith(expect), entry["note"])
                self.assertEqual("holiday-rolled" in entry["note"], expect == note)
                self.assertEqual(entry["caveat"], dc.CUSTODIAN_CAVEAT if days else dc.CAVEAT)
                self.assertNotIn("computed_mismatch", self.calc(doc)["issues"])
        self.assertIn("custodian's list, not the law package", dc.CUSTODIAN_CAVEAT)
        good = report(listed("2026-11-26"), received_at="2026-11-16")
        self.assertNotIn("holiday", self.calc(good)["extension_limit"]["note"])  # 2026-12-10 is not listed
        tampered = []
        for edit in (lambda d: d["requests"][0]["deadline"].update(base_due="2026-11-28"),  # shifted date
                     lambda d: d.pop("holiday_list"),                                       # missing snapshot
                     lambda d: d["holiday_list"]["dates"].append("2026-11-27"),             # doctored list
                     lambda d: d["requests"][0].update(holiday_list_sha256_prefix="f" * 12)):
            doc = copy.deepcopy(good)
            edit(doc)
            tampered.append(doc)
        legacy = report(received_at="2026-11-16")
        legacy["requests"][0]["deadline"]["base_due"] = "2026-11-27"                        # rolled with no list at all
        for doc in tampered + [legacy]:
            result = self.calc(doc)
            self.assertIn("computed_mismatch", result["issues"])
            self.assertEqual(result["determination_due"]["label"], "UNVERIFIED")

    def test_invalid_holiday_list_uses_weekends_only_and_keeps_its_issue(self):
        doc = report(("invalid", frozenset(), None), received_at="2026-11-06")
        result = self.calc(doc)
        self.assertEqual(result["determination_due"]["label"], "COMPUTED")
        self.assertIn("holiday_list_invalid", result["issues"])
        self.assertEqual(result["determination_due"]["caveat"], dc.INVALID_CAVEAT)
        doc["requests"][0]["holiday_list"] = "used"   # the row cannot claim a list the snapshot says is invalid
        self.assertIn("computed_mismatch", self.calc(doc)["issues"])

    def test_cli_table_banner_names_the_custodian_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "run-outputs" / "run-1"
            directory.mkdir(parents=True)
            (directory / "CURRENT-REQUEST-REGISTER.json").write_text(
                json.dumps(report(listed("2026-11-26"), received_at="2026-11-16")), encoding="utf-8")
            table = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.records", "deadlines",
                                    "--project", tmp, "--run-id", "run-1"], capture_output=True, text=True)
            self.assertEqual(table.returncode, 0, table.stderr)
            self.assertIn("Private; REVIEW REQUIRED. " + dc.CUSTODIAN_CAVEAT, table.stdout)
            self.assertIn("2026-11-27\tCOMPUTED", table.stdout)

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
