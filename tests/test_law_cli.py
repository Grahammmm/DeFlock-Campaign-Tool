"""Offline tests for the ``campaign_tool.law`` ``rules`` and ``deadline`` commands.

Synthetic packages only, written under a temporary ``--base``. Outputs are
provisional reminders, never legal due dates. Run from the repository root:
python3 -B -m unittest tests.test_law_cli -v
"""

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import law  # noqa: E402

RULE_KEYS = ["rule_id", "citation", "effective_from", "effective_to", "review"]


def synthetic_package(status="reviewed", day_type="calendar", days=10, extension=14):
    package = copy.deepcopy(law.load_package("us-ca"))
    template = package["rules"][0]
    rules = []
    for rule_id, start, end, review in (("ca-a", "2020-01-01", None, "verified"),
                                        ("ca-b", "2020-01-01", "2024-12-31", "likely"),
                                        ("ca-c", "2025-01-01", None, "needs_attorney_review")):
        rule = copy.deepcopy(template)
        rule.update(rule_id=rule_id, citation="Synthetic " + rule_id,
                    effective_from=start, effective_to=end, review=review)
        rules.append(rule)
    package["rules"] = rules
    for scope in package["request_scopes"]:
        scope["rule_ids"] = ["ca-a"]
    package["status"] = status
    package["reviewed_by"] = ["Synthetic Reviewer"] if status == "reviewed" else []
    package["reviewed_at"] = "2026-01-01" if status == "reviewed" else None
    package["records_law"].update(day_type=day_type, determination_days=days,
                                  determination_extension_days=extension)
    return package


class LawRulesDeadlineCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-law-cli-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)

    def write(self, **options):
        folder = self.base / "us-ca"
        folder.mkdir(exist_ok=True)
        (folder / "package.json").write_text(json.dumps(synthetic_package(**options)), encoding="utf-8")

    def run_law(self, *arguments, returncode=0, base=True):
        extra = ["--base", str(self.base)] if base else []
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.law", *arguments, *extra],
                                cwd=REPO_ROOT, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, returncode, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        return result

    def output(self, *arguments):
        result = json.loads(self.run_law(*arguments).stdout)
        self.assertEqual(result["note"], law.HOLIDAY_NOTE)
        return result

    def stopped(self, *arguments, message="Stopped:"):
        result = self.run_law(*arguments, returncode=1)
        self.assertTrue(result.stderr.startswith(message), result.stderr)
        self.assertEqual(result.stdout, "")

    def rule_ids(self, *arguments):
        result = self.output("rules", "us-ca", *arguments)
        self.assertEqual(result["rule_count"], len(result["rules"]))
        for rule in result["rules"]:
            self.assertEqual(list(rule), RULE_KEYS)
        return [rule["rule_id"] for rule in result["rules"]]

    def test_rules_in_force_both_interval_ends_inclusive(self):
        self.write()
        result = self.output("rules", "us-ca", "--on", "2024-12-31")
        self.assertEqual((result["jurisdiction"], result["status"], result["on"], result["rule_count"]),
                         ("us-ca", "reviewed", "2024-12-31", 2))
        self.assertEqual([r["rule_id"] for r in result["rules"]], ["ca-a", "ca-b"])
        self.assertEqual(result["rules"][1], {"rule_id": "ca-b", "citation": "Synthetic ca-b",
                                              "effective_from": "2020-01-01",
                                              "effective_to": "2024-12-31", "review": "likely"})
        self.assertEqual(self.rule_ids("--on", "2025-01-01"), ["ca-a", "ca-c"])
        self.assertEqual(self.rule_ids("--on", "2020-01-01"), ["ca-a", "ca-b"])

    def test_date_before_every_rule_lists_none(self):
        self.write()
        result = self.output("rules", "us-ca", "--on", "2019-12-31")
        self.assertEqual((result["rule_count"], result["rules"]), (0, []))

    def test_review_filter_keeps_only_that_class(self):
        self.write()
        self.assertEqual(self.rule_ids("--on", "2025-01-01", "--review", "needs_attorney_review"), ["ca-c"])
        self.assertEqual(self.rule_ids("--on", "2025-01-01", "--review", "verified"), ["ca-a"])
        self.assertEqual(self.rule_ids("--on", "2025-01-01", "--review", "likely"), [])
        self.assertEqual(self.rule_ids("--on", "2024-12-31", "--review", "likely"), ["ca-b"])
        for bad in ("unknown", "Verified", "needs-attorney-review"):
            refused = self.run_law("rules", "us-ca", "--on", "2025-01-01", "--review", bad, returncode=2)
            self.assertEqual(refused.stdout, "")

    def test_bad_dates_stop_without_output(self):
        self.write()
        for bad in ("2025-02-30", "2025-1-5", "2025-01-15T00:00", "20250115"):
            with self.subTest(date=bad):
                self.stopped("rules", "us-ca", "--on", bad, message="Stopped: --on:")
                self.stopped("deadline", "us-ca", "--sent", bad, message="Stopped: --sent:")

    def test_missing_date_flag_is_usage_error(self):
        self.write()
        for arguments in (("rules", "us-ca"), ("deadline", "us-ca"), ("deadline", "us-ca", "--extension")):
            with self.subTest(arguments=arguments):
                result = self.run_law(*arguments, returncode=2)
                self.assertIn("usage:", result.stderr)
                self.assertEqual(result.stdout, "")

    def test_calendar_deadline_with_and_without_extension(self):
        self.write()
        plain = self.output("deadline", "us-ca", "--sent", "2025-01-15")
        self.assertEqual({k: plain[k] for k in ("jurisdiction", "status", "sent", "extension",
                                                 "day_type", "days", "reminder")},
                         {"jurisdiction": "us-ca", "status": "reviewed", "sent": "2025-01-15",
                          "extension": False, "day_type": "calendar", "days": 10,
                          "reminder": "2025-01-25"})
        extended = self.output("deadline", "us-ca", "--sent", "2025-01-15", "--extension")
        self.assertEqual((extended["extension"], extended["days"], extended["reminder"]),
                         (True, 24, "2025-02-08"))

    def test_business_deadline_weekend_and_friday_start(self):
        self.write(day_type="business")
        for sent in ("2025-01-18", "2025-01-19", "2025-01-17"):
            with self.subTest(sent=sent):
                result = self.output("deadline", "us-ca", "--sent", sent)
                self.assertEqual((result["day_type"], result["days"], result["reminder"]),
                                 ("business", 10, "2025-01-31"))

    def test_out_of_range_deadline_stops(self):
        self.write()
        self.stopped("deadline", "us-ca", "--sent", "9999-12-30",
                     message="Stopped: law_deadline_out_of_range")
        self.write(day_type="business")
        self.stopped("deadline", "us-ca", "--sent", "9999-12-20", "--extension",
                     message="Stopped: law_deadline_out_of_range")

    def test_draft_package_is_shown_with_status(self):
        self.write(status="draft")
        self.assertEqual(self.output("rules", "us-ca", "--on", "2025-01-01")["status"], "draft")
        self.assertEqual(self.output("deadline", "us-ca", "--sent", "2025-01-15")["status"], "draft")

    def test_missing_package_and_bad_jurisdiction_stop(self):
        for command in (("rules", "--on", "2025-01-01"), ("deadline", "--sent", "2025-01-15")):
            with self.subTest(command=command[0]):
                self.stopped(command[0], "us-ca", *command[1:], message="Stopped: No law package")
                self.write()
                self.stopped(command[0], "US-CA", *command[1:], message="Stopped: Jurisdiction must")
                self.stopped(command[0], "us-zz", *command[1:], message="Stopped: No law package")
                (self.base / "us-ca" / "package.json").unlink()

    def test_show_unchanged_and_help_lists_commands(self):
        shown = self.run_law("show", "us-ca", base=False)
        expected = json.dumps(law.summary(law.load_package("us-ca")), indent=2, ensure_ascii=False)
        self.assertEqual(shown.stdout, expected + "\n")
        usage = self.run_law("--help", base=False).stdout
        for command in ("show", "rules", "deadline"):
            self.assertIn(command, usage)


if __name__ == "__main__":
    unittest.main()
