"""doctor --checklist: each unmet check names its fix and owner. Synthetic temp campaigns only."""

import itertools
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool.cli import doctor_report  # noqa: E402
from campaign_tool.doctor_checklist import checklist, render  # noqa: E402

BOOLEANS = ("config_readable", "jurisdiction_verified", "reviewed_law_package", "public_deployment_checked",
            "newsletter_checked", "kit_built")
FORBIDDEN = re.compile(r"late|overdue|violat|illegal|compliant", re.I)


def done_report(**overrides):
    report = {key: True for key in BOOLEANS}
    report.update(law_package_status="reviewed", safe_to_send_automatically=False, production_ready=False,
                  next_actions=["generic"], agencies_selected=2, stale_comment_kits=[])
    report.update(overrides)
    return report


def by_key(rows):
    return {row["key"]: row for row in rows}


class ChecklistCLITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-checklist-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"

    def cli(self, *arguments, returncode=0, root=None):
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *arguments[:1], "--directory",
                                 str(root or self.root), *arguments[1:]],
                                cwd=REPO_ROOT, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, returncode, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        return result

    def init(self, root=None):
        self.cli("init", "--name", "Synthetic Campaign", "--county", "Synthetic County", "--state", "ca", root=root)

    def agencies(self, entries):
        (self.root / "kit").mkdir(exist_ok=True)
        (self.root / "kit" / "agencies.json").write_text(json.dumps(entries) if isinstance(entries, list) else entries)

    def test_fresh_init_lists_todos_and_info(self):
        self.init()
        rows = by_key(json.loads(self.cli("doctor", "--checklist", "--json").stdout))
        for key in ("jurisdiction_verified", "reviewed_law_package", "agencies_selected", "kit_built",
                    "public_deployment_checked", "newsletter_checked"):
            self.assertEqual(rows[key]["state"], "todo", key)
            self.assertTrue(rows[key]["next"] and rows[key]["owner"], key)
        self.assertEqual(rows["reviewed_law_package"]["owner"], "independent law reviewer")
        self.assertIn("docs/LAW-PACKAGES.md", rows["reviewed_law_package"]["next"])
        self.assertEqual(rows["safe_to_send_automatically"]["state"], "info")
        self.assertEqual(rows["production_ready"]["state"], "info")
        self.assertIn("not yet", rows["production_ready"]["label"])
        text = self.cli("doctor", "--checklist").stdout.splitlines()
        self.assertIn("[todo] Law package reviewed", text)
        self.assertEqual(text[-1], "6 to do · 2 ok · not a legal status")
        self.assertTrue(any(line.strip().startswith("→ ") and line.endswith("(organizer)") for line in text))

    def test_plain_doctor_is_unchanged_and_json_needs_checklist(self):
        self.init()
        plain = self.cli("doctor").stdout
        self.assertEqual(plain, json.dumps(doctor_report(self.root.resolve()), indent=2) + "\n")
        self.assertEqual(self.cli("doctor", "--contact-max-age-days", "30").stdout,
                         json.dumps(doctor_report(self.root.resolve(), 30), indent=2) + "\n")
        result = self.cli("doctor", "--json", returncode=2)
        self.assertIn("--json requires --checklist", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_unreadable_agencies_file_names_kit_error_not_zero_selected(self):
        self.init()
        self.agencies("{not json")
        rows = by_key(json.loads(self.cli("doctor", "--checklist", "--json").stdout))
        self.assertEqual(rows["kit_error"]["state"], "todo")
        self.assertIn("kit/agencies.json", rows["kit_error"]["next"])
        self.assertNotEqual(rows["agencies_selected"]["state"], "todo")
        self.assertNotIn("(0)", rows["agencies_selected"]["label"])
        self.assertIn("unknown", rows["agencies_selected"]["label"])

    def test_stale_contacts_named_with_count_and_command(self):
        self.init()
        old = {"selected": True, "verified": True, "verification": {"checked_on": "2001-01-01"}}
        fresh_unverified = {"agency_id": "c", "selected": True, "verified": False}
        self.agencies([dict(old, agency_id="a"), dict(old, agency_id="b"), fresh_unverified])
        rows = by_key(json.loads(self.cli("doctor", "--contact-max-age-days", "30", "--checklist", "--json").stdout))
        row = rows["stale_contacts"]
        self.assertEqual(row["state"], "todo")
        self.assertIn("Re-check 2 contact(s)", row["next"])
        self.assertIn("kit-verify-agency --directory", row["next"])
        self.assertIn("a, b", row["next"])
        self.assertEqual(rows["agencies_selected"]["state"], "ok")
        without_flag = by_key(json.loads(self.cli("doctor", "--checklist", "--json").stdout))
        self.assertNotIn("stale_contacts", without_flag)

    def test_directory_with_spaces_and_quotes_is_shell_quoted(self):
        root = self.root.parent / "my 'odd' campaign"
        self.init(root=root)
        given = str(root)
        rows = by_key(json.loads(self.cli("doctor", "--checklist", "--json", root=root).stdout))
        self.assertIn(f"kit --directory {shlex.quote(given)}`", rows["agencies_selected"]["next"])
        self.assertEqual(shlex.split(rows["kit_built"]["next"].split("`")[1])[-1], given)

    def test_unreadable_config_keeps_stopped_path(self):
        self.root.mkdir(parents=True)
        result = self.cli("doctor", "--checklist", returncode=1)
        self.assertIn("Stopped:", result.stderr)


class ChecklistRowTests(unittest.TestCase):
    def test_all_satisfied_is_zero_todo(self):
        rows = checklist(done_report(), "D")
        self.assertEqual({row["state"] for row in rows} - {"ok", "info"}, set())
        self.assertTrue(render(done_report(), "D").splitlines()[-1].startswith("0 to do · "))
        self.assertEqual(by_key(rows)["safe_to_send_automatically"]["state"], "info")
        self.assertEqual(rows[-1]["key"], "production_ready")

    def test_production_ready_never_claims_ready_while_a_todo_exists(self):
        for values in itertools.product((True, False), repeat=len(BOOLEANS)):
            for production in (True, False):
                report = done_report(production_ready=production, **dict(zip(BOOLEANS, values)))
                rows = checklist(report, "D")
                todos = sum(row["state"] == "todo" for row in rows)
                self.assertEqual(todos, values.count(False))
                label = by_key(rows)["production_ready"]["label"]
                self.assertEqual("not yet" in label, todos > 0, (values, label))
                self.assertNotIn("Production ready: yes", label)

    def test_safe_to_send_is_info_even_if_report_says_true(self):
        row = by_key(checklist(done_report(safe_to_send_automatically=True), "D"))["safe_to_send_automatically"]
        self.assertEqual((row["state"], row["next"]), ("info", None))
        self.assertIn("every send is approved by a person", row["label"])

    def test_law_package_error_adds_maintainer_row_and_keeps_review_row(self):
        report = done_report(reviewed_law_package=False, law_package_status="unreadable",
                             law_package_error="jurisdictions/us-ca/package.json: bad JSON")
        rows = by_key(checklist(report, "D"))
        self.assertEqual(rows["law_package_error"]["owner"], "maintainer")
        self.assertIn("bad JSON; report it to the maintainer", rows["law_package_error"]["next"])
        self.assertEqual(rows["reviewed_law_package"]["state"], "todo")

    def test_zero_selected_without_kit_error_is_todo(self):
        row = by_key(checklist(done_report(agencies_selected=0), "D"))["agencies_selected"]
        self.assertEqual((row["state"], row["owner"]), ("todo", "organizer"))
        self.assertEqual(row["next"], "Select agencies: `python3 -m campaign_tool kit --directory D`")

    def test_stale_contacts_more_is_counted(self):
        stale = [{"agency_id": f"a{i}", "checked_on": None, "age_days": None} for i in range(100)]
        row = by_key(checklist(done_report(stale_contacts=stale, stale_contacts_more=5), "D"))["stale_contacts"]
        self.assertIn("Re-check 105 contact(s)", row["next"])
        self.assertIn("a0, a1, a2, …", row["next"])
        self.assertEqual(by_key(checklist(done_report(stale_contacts=[]), "D"))["stale_contacts"]["state"], "ok")

    def test_stale_comment_kits_name_each_meeting_once(self):
        stale = [{"meeting": "m2", "finding": "f", "latest_correction": "2026-01-02"},
                 {"meeting": "m1", "finding": "f", "latest_correction": "2026-01-02"},
                 {"meeting": "m1", "finding": "g", "latest_correction": "2026-01-03"}]
        row = by_key(checklist(done_report(stale_comment_kits=stale), "D"))["stale_comment_kits"]
        self.assertEqual(row["next"].count("meetings-stage"), 2)
        self.assertIn("meetings-stage --directory D --id m1 --replace", row["next"])
        self.assertLess(row["next"].index("m1"), row["next"].index("m2"))

    def test_unknown_key_becomes_short_info_row(self):
        value = [{"agency": "x" * 50, "ends": "2027-01-01"}] * 5
        rows = by_key(checklist(done_report(contracts_ending=value, extra_flag=False, empty=[]), "D"))
        row = rows["contracts_ending"]
        self.assertEqual(row["state"], "info")
        self.assertTrue(row["label"].startswith("Other: contracts_ending = "))
        self.assertLessEqual(len(row["label"].split(" = ", 1)[1]), 80)
        self.assertEqual(rows["extra_flag"]["label"], "Other: extra_flag = false")
        self.assertEqual(rows["empty"]["label"], "Other: empty = []")
        self.assertNotIn("next_actions", rows)

    def test_no_row_uses_legal_conclusion_words(self):
        reports = [done_report(), done_report(
            **{key: False for key in BOOLEANS}, agencies_selected=0, kit_error="kit/agencies.json is not readable JSON",
            law_package_error="e", stale_contacts=[{"agency_id": "a"}], stale_comment_kits=[{"meeting": "m"}])]
        for report in reports:
            for row in checklist(report, "D"):
                self.assertIsNone(FORBIDDEN.search(" ".join(str(row[k] or "") for k in ("label", "next", "owner"))),
                                  row)
            self.assertIsNone(FORBIDDEN.search(render(report, "D")))


if __name__ == "__main__":
    unittest.main()
