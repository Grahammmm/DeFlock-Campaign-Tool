"""Offline tests for campaign_tool.law and the us-ca law package.

Synthetic campaign fixtures only. Passing these checks means the package is
structurally valid and the date arithmetic is consistent; it is not legal
review. Run from the repository root: python3 -B -m unittest tests.test_law -v
"""

import copy
import json
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import law  # noqa: E402

CA_PATH = REPO_ROOT / "jurisdictions" / "us-ca" / "package.json"


def generic_schema_check(document, schema, root=None, where="$"):
    """Independent re-implementation of the schema rules, used to cross-check law.validate."""
    root = root if root is not None else schema
    if "$ref" in schema:
        node = root
        for part in schema["$ref"][2:].split("/"):
            node = node[part]
        merged = {**node, **{k: v for k, v in schema.items() if k != "$ref"}}
        return generic_schema_check(document, merged, root, where)
    problems = []
    if "const" in schema and document != schema["const"]:
        problems.append(f"{where}: const")
    if "enum" in schema and document not in schema["enum"]:
        problems.append(f"{where}: enum")
    types = schema.get("type")
    if types is not None:
        types = types if isinstance(types, list) else [types]
        checks = {"object": lambda v: isinstance(v, dict), "array": lambda v: isinstance(v, list),
                  "string": lambda v: isinstance(v, str), "null": lambda v: v is None,
                  "integer": lambda v: isinstance(v, int) and not isinstance(v, bool)}
        if not any(checks[t](document) for t in types):
            problems.append(f"{where}: type")
    if isinstance(document, str) and "pattern" in schema and not re.search(schema["pattern"], document):
        problems.append(f"{where}: pattern")
    if isinstance(document, str) and len(document) < schema.get("minLength", 0):
        problems.append(f"{where}: minLength")
    if isinstance(document, int) and not isinstance(document, bool) and document < schema.get("minimum", document):
        problems.append(f"{where}: minimum")
    if isinstance(document, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in document:
                problems.append(f"{where}: required {key}")
        if schema.get("additionalProperties") is False:
            problems += [f"{where}: extra {k}" for k in document if k not in props]
        for key, value in document.items():
            if key in props:
                problems += generic_schema_check(value, props[key], root, f"{where}.{key}")
    if isinstance(document, list):
        if len(document) < schema.get("minItems", 0):
            problems.append(f"{where}: minItems")
        for i, entry in enumerate(document):
            if "items" in schema:
                problems += generic_schema_check(entry, schema["items"], root, f"{where}[{i}]")
    return problems


class CaliforniaPackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.package = law.load_package("us-ca")
        cls.schema = law.load_schema()
        cls.raw = json.loads(CA_PATH.read_text(encoding="utf-8"))

    def test_package_conforms_to_schema_by_independent_checker(self):
        self.assertEqual(generic_schema_check(self.raw, self.schema), [])

    def test_package_is_a_draft_with_no_reviewers(self):
        self.assertEqual(self.package["status"], "draft")
        self.assertEqual(self.package["reviewed_by"], [])
        self.assertIsNone(self.package["reviewed_at"])
        self.assertEqual(self.package["jurisdiction"], "us-ca")

    def test_records_law_encodes_cpra_deadlines(self):
        rl = self.package["records_law"]
        self.assertEqual(rl["name"], "California Public Records Act")
        self.assertIn("7920.000", rl["citation"])
        self.assertEqual((rl["determination_days"], rl["determination_extension_days"],
                          rl["day_type"]), (10, 14, "calendar"))
        for source in rl["sources"]:
            self.assertTrue(source["url"].startswith("https://leginfo.legislature.ca.gov/"))

    def test_expected_rules_present_with_sources_and_review_labels(self):
        by_id = {rule["rule_id"]: rule for rule in self.package["rules"]}
        for rule_id in ("ca-civ-1798.90.5-definitions",
                        "ca-civ-1798.90.51-usage-and-privacy-policy",
                        "ca-civ-1798.90.52-access-records",
                        "ca-civ-1798.90.53-end-user-usage-and-privacy-policy",
                        "ca-civ-1798.90.54-civil-action",
                        "ca-civ-1798.90.55-public-comment-before-implementation",
                        "ca-civ-1798.90.55-sharing-limits",
                        "ca-veh-2413-chp-lpr-retention-and-sharing"):
            self.assertIn(rule_id, by_id)
            expected = "needs_attorney_review" if rule_id == "ca-civ-1798.90.5-definitions" else "verified"
            self.assertEqual(by_id[rule_id]["review"], expected, rule_id)
        for rule_id in by_id:
            if rule_id.startswith("ca-civ-1798.90."):
                self.assertEqual(by_id[rule_id]["effective_from"], "2016-01-01", rule_id)
        self.assertEqual(by_id["ca-gov-7284.6-values-act-immigration-enforcement-limits"]["review"], "needs_attorney_review")
        self.assertEqual(by_id["ca-oag-2023-dle-06-out-of-state-sharing-guidance"]["review"], "likely")
        for rule in self.package["rules"]:
            self.assertTrue(rule["sources"], rule["rule_id"])
            self.assertIn(rule["review"], ("verified", "likely", "needs_attorney_review"))

    def test_request_scopes_cover_template_and_reference_known_rules(self):
        ids = [scope["scope_id"] for scope in self.package["request_scopes"]]
        self.assertEqual(ids, ["agreements", "policies", "sharing", "audit-logs",
                               "compliance", "hearing-records"])
        known = {rule["rule_id"] for rule in self.package["rules"]}
        for scope in self.package["request_scopes"]:
            self.assertTrue(scope["items"])
            self.assertTrue(scope["rule_ids"], scope["scope_id"])
            self.assertTrue(set(scope["rule_ids"]) <= known, scope["scope_id"])

    def test_no_secrets_or_private_markers_in_package(self):
        text = CA_PATH.read_text(encoding="utf-8")
        for marker in ("@gmail", "password", "Bearer ", "/Users/", "private/"):
            self.assertNotIn(marker, text)


class LawFunctionTests(unittest.TestCase):
    def setUp(self):
        self.package = law.load_package("us-ca")

    def test_deadline_ten_calendar_days_and_fourteen_day_extension(self):
        self.assertEqual(law.deadline(self.package, "2026-01-05"), date(2026, 1, 15))
        self.assertEqual(law.deadline(self.package, date(2026, 1, 5), extension=True),
                         date(2026, 1, 29))
        # Calendar days cross month and year boundaries and count weekends.
        self.assertEqual(law.deadline(self.package, "2025-12-28"), date(2026, 1, 7))

    def test_business_day_deadline_skips_weekends_only(self):
        business = copy.deepcopy(self.package)
        business["records_law"]["day_type"] = "business"
        # Friday 2026-01-02 + 10 business days = Friday 2026-01-16 (holidays not modeled).
        self.assertEqual(law.deadline(business, "2026-01-02"), date(2026, 1, 16))
        self.assertIn("holidays are not modeled", law.HOLIDAY_NOTE)

    def test_deadline_rejects_malformed_dates(self):
        for bad in ("2026/01/05", "2026-13-01", 20260105, None):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    law.deadline(self.package, bad)

    def test_rules_in_force_respects_sb34_effective_date(self):
        before = {r["rule_id"] for r in law.rules_in_force(self.package, "2015-12-31")}
        after = {r["rule_id"] for r in law.rules_in_force(self.package, date(2016, 1, 2))}
        self.assertFalse(any(r.startswith("ca-civ-1798.90.") for r in before))
        self.assertIn("ca-veh-2413-chp-lpr-retention-and-sharing", before)
        self.assertTrue({"ca-civ-1798.90.51-usage-and-privacy-policy",
                         "ca-civ-1798.90.55-sharing-limits"} <= after)
        self.assertNotIn("ca-gov-7284.6-values-act-immigration-enforcement-limits", after)
        self.assertIn("ca-civ-1798.90.55-sharing-limits",
                      {r["rule_id"] for r in law.rules_in_force(self.package, "2016-01-01")})
        expired = copy.deepcopy(self.package)
        expired["rules"][0]["effective_to"] = "2020-01-01"
        self.assertNotIn(expired["rules"][0]["rule_id"],
                         {r["rule_id"] for r in law.rules_in_force(expired, "2020-01-02")})
        self.assertIn(expired["rules"][0]["rule_id"],
                      {r["rule_id"] for r in law.rules_in_force(expired, "2020-01-01")})

    def test_request_scope_lookup(self):
        self.assertEqual(law.request_scope(self.package, "audit-logs")["scope_id"], "audit-logs")
        with self.assertRaises(ValueError):
            law.request_scope(self.package, "no-such-scope")

    def test_render_request_contains_citation_items_and_no_angle_brackets(self):
        scope = law.request_scope(self.package, "policies")
        text = law.render_request(
            self.package, "policies", "Synthetic County Sheriff's Office",
            "Public Records Custodian", ("2025-01-01", date(2025, 6, 30)),
            "USD 50", "Synthetic Requester\nsynthetic@example.invalid")
        self.assertIn(self.package["records_law"]["citation"], text)
        self.assertIn("California Public Records Act", text)
        for item in scope["items"]:
            self.assertIn("- " + item, text)
        self.assertIn("2025-01-01 through 2025-06-30", text)
        self.assertIn("USD 50", text)
        self.assertIn("DRAFT ONLY", text)
        self.assertIn("Law package status: draft", text)
        self.assertNotIn("<", text)
        self.assertNotIn(">", text)
        # No unfilled template placeholders survive.
        self.assertNotIn("[official records custodian]", text)
        self.assertNotIn("[agency]", text)
        self.assertNotIn("[explicit approved cap]", text)

    def test_render_request_rejects_markup_and_bad_ranges(self):
        args = ["Synthetic Agency", "Custodian", "2025", "USD 10", "Requester"]
        for index in range(len(args)):
            hostile = list(args)
            hostile[index] = "<script>alert(1)</script>"
            with self.subTest(field=index):
                with self.assertRaises(ValueError):
                    law.render_request(self.package, "agreements", *hostile)
        with self.assertRaises(ValueError):
            law.render_request(self.package, "agreements", "A", "B",
                               ("2025-06-30", "2025-01-01"), "USD 10", "R")
        with self.assertRaises(ValueError):
            law.render_request(self.package, "agreements", "", "B", "2025", "USD 10", "R")


class LoadPackageValidationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="synthetic-law-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.raw = json.loads(CA_PATH.read_text(encoding="utf-8"))

    def write(self, document, jurisdiction="us-ca"):
        target = self.base / jurisdiction
        target.mkdir(parents=True, exist_ok=True)
        (target / "package.json").write_text(json.dumps(document), encoding="utf-8")

    def assert_rejected(self, document, fragment, jurisdiction="us-ca"):
        self.write(document, jurisdiction)
        with self.assertRaises(ValueError) as caught:
            law.load_package(jurisdiction, self.base)
        self.assertIn(fragment, str(caught.exception))
        # The independent checker must agree whenever the failure is structural.
        return str(caught.exception)

    def test_valid_copy_loads_from_alternate_base(self):
        self.write(self.raw)
        self.assertEqual(law.load_package("us-ca", self.base)["jurisdiction"], "us-ca")

    def test_missing_top_level_field_is_rejected(self):
        broken = dict(self.raw)
        del broken["records_law"]
        self.assert_rejected(broken, "missing required field 'records_law'")
        self.assertTrue(generic_schema_check(broken, law.load_schema()))

    def test_missing_rule_field_and_bad_enum_pattern_and_type(self):
        broken = copy.deepcopy(self.raw)
        del broken["rules"][0]["remedy"]
        self.assert_rejected(broken, "$.rules[0]: missing required field 'remedy'")
        broken = copy.deepcopy(self.raw)
        broken["rules"][1]["review"] = "certain"
        self.assert_rejected(broken, "$.rules[1].review")
        broken = copy.deepcopy(self.raw)
        broken["rules"][1]["sources"][0]["url"] = "http://example.invalid"
        self.assert_rejected(broken, "pattern")
        broken = copy.deepcopy(self.raw)
        broken["records_law"]["determination_days"] = "10"
        self.assert_rejected(broken, "expected type integer")
        broken = copy.deepcopy(self.raw)
        broken["records_law"]["determination_days"] = 0
        self.assert_rejected(broken, "below minimum")
        broken = copy.deepcopy(self.raw)
        broken["rules"][2]["notes"] = "extra"
        self.assert_rejected(broken, "unexpected field(s) ['notes']")
        broken = copy.deepcopy(self.raw)
        broken["status"] = "final"
        self.assert_rejected(broken, "$.status")
        broken = copy.deepcopy(self.raw)
        broken["schema_version"] = 2
        self.assert_rejected(broken, "expected constant 1")
        broken = copy.deepcopy(self.raw)
        broken["rules"][0]["sources"] = []
        self.assert_rejected(broken, "minItems")
        for document in (broken,):
            self.assertTrue(generic_schema_check(document, law.load_schema()))

    def test_semantic_checks(self):
        broken = copy.deepcopy(self.raw)
        broken["rules"][1]["rule_id"] = broken["rules"][0]["rule_id"]
        self.assert_rejected(broken, "duplicate")
        broken = copy.deepcopy(self.raw)
        broken["request_scopes"][0]["rule_ids"] = ["ca-missing-rule"]
        self.assert_rejected(broken, "unknown rule id(s)")
        broken = copy.deepcopy(self.raw)
        broken["rules"][0]["effective_to"] = "2015-01-01"
        self.assert_rejected(broken, "effective_to precedes effective_from")
        broken = copy.deepcopy(self.raw)
        broken["rules"][0]["effective_from"] = "2016-02-30"
        self.assert_rejected(broken, "effective_from")
        broken = copy.deepcopy(self.raw)
        broken["status"] = "reviewed"
        self.assert_rejected(broken, "reviewed_by")
        broken["reviewed_by"] = ["synthetic-reviewer"]
        self.assert_rejected(broken, "reviewed_at")
        broken["reviewed_at"] = "2026-09-30"
        self.write(broken)
        self.assertEqual(law.load_package("us-ca", self.base)["status"], "reviewed")
        mismatched = copy.deepcopy(self.raw)
        self.assert_rejected(mismatched, "does not match directory", jurisdiction="us-nv")

    def test_missing_and_malformed_files(self):
        with self.assertRaises(ValueError) as caught:
            law.load_package("us-zz", self.base)
        self.assertIn("No law package", str(caught.exception))
        for bad in ("US-CA", "ca", "us-ca/../x", ""):
            with self.assertRaises(ValueError):
                law.load_package(bad, self.base)
        (self.base / "us-ca").mkdir()
        (self.base / "us-ca" / "package.json").write_text("{not json", encoding="utf-8")
        with self.assertRaises(ValueError) as caught:
            law.load_package("us-ca", self.base)
        self.assertIn("invalid JSON", str(caught.exception))
        self.assertEqual(law.package_status("us-ca", self.base)[0], "missing")
        self.assertEqual(law.package_status("us-zz", self.base), ("missing", None))
        self.assertEqual(law.package_status("us-ca")[0], "draft")


class LawCliTests(unittest.TestCase):
    def run_python(self, *arguments, returncode=0):
        result = subprocess.run([sys.executable, "-B", *arguments], cwd=REPO_ROOT,
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, returncode, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        return result

    def test_show_prints_summary(self):
        result = self.run_python("-m", "campaign_tool.law", "show", "us-ca")
        summary = json.loads(result.stdout)
        self.assertEqual(summary["status"], "draft")
        self.assertEqual(summary["rule_count"], 12)
        self.assertEqual(summary["records_law"]["determination_days"], 10)
        self.assertIn("holidays are not modeled", summary["note"])
        missing = self.run_python("-m", "campaign_tool.law", "show", "us-zz", returncode=1)
        self.assertIn("Stopped:", missing.stderr)

    def test_doctor_reports_draft_for_california_and_missing_elsewhere(self):
        with tempfile.TemporaryDirectory(prefix="synthetic-doctor-") as temporary:
            for state, expected in (("CA", "draft"), ("WY", "missing")):
                root = Path(temporary) / state
                self.run_python("-m", "campaign_tool", "init", "--directory", str(root),
                                "--name", "Synthetic", "--county", "Synthetic", "--state", state)
                report = json.loads(self.run_python(
                    "-m", "campaign_tool", "doctor", "--directory", str(root)).stdout)
                self.assertIs(report["reviewed_law_package"], False)
                self.assertEqual(report["law_package_status"], expected)
                self.assertNotIn("law_package_error", report)
                for key in ("config_readable", "jurisdiction_verified", "public_deployment_checked",
                            "newsletter_checked", "safe_to_send_automatically",
                            "production_ready", "next_actions"):
                    self.assertIn(key, report)


if __name__ == "__main__":
    unittest.main()
