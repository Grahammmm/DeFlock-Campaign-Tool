"""`law impact OLD NEW --directory D` (DF1422-08): rules two law-package versions change and the findings they touch.

Synthetic jurisdiction rules and findings only (tests/fixtures/law_impact/base-package.json). Run from the repository
root: python3 -B -m unittest tests.test_law_impact -v
"""
import contextlib
import copy
import io
import json
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import law  # noqa: E402

BASE = json.loads((REPO_ROOT / "tests" / "fixtures" / "law_impact" / "base-package.json").read_text(encoding="utf-8"))


def rule_of(package, rule_id):
    return next(r for r in package["rules"] if r["rule_id"] == rule_id)


def snapshot(folder):
    return {str(p.relative_to(folder)): p.read_bytes() for p in sorted(Path(folder).rglob("*")) if p.is_file()}


class LawImpactTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-law-impact-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        for sub in ("content/findings", "private/drafts", "private/withdrawn"):
            (self.root / sub).mkdir(parents=True)
        self.old, self.new = copy.deepcopy(BASE), copy.deepcopy(BASE)

    def finding(self, ident, event_date="2024-05-01", rule_ids=("ca-alpha",), where="content/findings",
                source_rules=(), **extra):
        document = {"id": ident, "slug": "slug-" + ident.replace("_", "-"), "title": "Synthetic " + ident,
                    "event_date": event_date, "rule_ids": list(rule_ids),
                    "sources": [{"sha256": "ab" * 32, "locator": f"page:{i + 1}", "title": "Synthetic record",
                                 "rule_id": rule} for i, rule in enumerate(source_rules)]}
        document.update(extra)
        path = self.root / where / (ident + ".json")
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def write(self, name, package):
        path = self.tmp / name
        path.write_text(json.dumps(package) if not isinstance(package, str) else package, encoding="utf-8")
        return path

    def run_impact(self, *extra, expect=None, write=True):
        old, new = (self.write("old.json", self.old), self.write("new.json", self.new)) if write else (
            self.tmp / "old.json", self.tmp / "new.json")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = law.main(["impact", str(old), str(new), "--directory", str(self.root), *extra])
        if expect is not None:
            self.assertEqual(code, expect, out.getvalue() + err.getvalue())
        return code, out.getvalue(), err.getvalue()

    def test_duty_change_lists_only_the_finding_citing_that_rule(self):
        rule_of(self.new, "ca-alpha")["duty"] = "Synthetic duty, amended."
        self.finding("fnd_a", "2024-05-01", ["ca-alpha"])
        self.finding("fnd_b", "2024-05-01", ["ca-beta"])
        _, out, _ = self.run_impact(expect=3)
        self.assertIn("changed ca-alpha: duty; window 2016-01-01..open", out)
        self.assertIn("fnd_a published 2024-05-01 cites ca-alpha (changed)", out)
        self.assertNotIn("fnd_b", out)
        self.assertIn(f"campaign_tool finding diff fnd_a --directory {self.root} --from DRAFT", out)
        self.assertIn("campaign_tool finding correct fnd_a --directory", out)

    def test_changed_rule_with_event_before_its_window_is_not_affected(self):
        rule_of(self.old, "ca-gamma")["effective_from"] = "2020-01-01"
        rule_of(self.new, "ca-gamma")["duty"] = "Synthetic duty, amended."
        self.finding("fnd_old", "2019-12-31", ["ca-gamma"])
        self.finding("fnd_in", "2020-01-01", ["ca-gamma"])
        _, out, _ = self.run_impact(expect=3)
        self.assertIn("fnd_in published", out)
        self.assertNotIn("fnd_old", out)

    def test_date_only_change_flags_only_days_whose_answer_changed(self):
        rule_of(self.new, "ca-alpha")["effective_to"] = "2025-01-01"
        for ident, day in (("fnd_before", "2024-06-01"), ("fnd_last", "2025-01-01"),
                           ("fnd_next", "2025-01-02"), ("fnd_after", "2025-06-01")):
            self.finding(ident, day)
        _, out, _ = self.run_impact(expect=3)
        self.assertIn("changed ca-alpha: effective_to; window 2025-01-02..open", out)
        self.assertEqual([i for i in ("fnd_before", "fnd_last", "fnd_next", "fnd_after") if i in out],
                         ["fnd_next", "fnd_after"])

    def test_date_only_change_below_the_window_is_not_affected(self):
        rule_of(self.new, "ca-alpha")["effective_to"] = "2025-01-01"
        self.finding("fnd_before", "2024-06-01")
        self.run_impact(expect=0)

    def test_date_only_start_moved_later_flags_the_days_that_left_the_rule(self):
        rule_of(self.new, "ca-alpha")["effective_from"] = "2017-06-01"
        self.finding("fnd_gap", "2016-12-31")
        self.finding("fnd_inside", "2018-01-01")
        _, out, _ = self.run_impact(expect=3)
        self.assertIn("window 2016-01-01..2017-05-31", out)
        self.assertIn("fnd_gap", out)
        self.assertNotIn("fnd_inside", out)

    def test_content_change_uses_the_union_of_both_intervals(self):
        rule_of(self.new, "ca-gamma")["duty"] = "Synthetic duty, amended."
        rule_of(self.new, "ca-gamma")["effective_to"] = "2025-12-31"
        self.finding("fnd_late", "2025-06-01", ["ca-gamma"])
        self.finding("fnd_after", "2026-01-01", ["ca-gamma"])
        _, out, _ = self.run_impact(expect=3)
        self.assertIn("window 2020-01-01..2025-12-31", out)
        self.assertIn("fnd_late", out)
        self.assertNotIn("fnd_after", out)

    def test_added_rule_cited_by_a_draft_through_sources_is_affected(self):
        rule = copy.deepcopy(rule_of(self.new, "ca-alpha"))
        rule.update(rule_id="ca-delta", effective_from="2024-01-01")
        self.new["rules"].append(rule)
        self.finding("fnd_d", "2024-05-01", [], where="private/drafts", source_rules=(None, "ca-delta"))
        self.finding("fnd_early", "2023-05-01", [], where="private/drafts", source_rules=("ca-delta",))
        draft = self.root / "private" / "drafts" / "fnd_d.json"
        _, out, _ = self.run_impact(expect=3)
        self.assertIn("added ca-delta; window 2024-01-01..open", out)
        self.assertIn("fnd_d draft 2024-05-01 cites ca-delta (added)", out)
        command = f"campaign_tool finding review-status {draft} --directory {self.root}"
        self.assertIn(command, out)
        self.assertIn("edit the unpublished draft", out)
        self.assertNotIn("fnd_early", out)
        self.assertNotIn("finding correct fnd_d", out)
        self.assertNotIn("finding diff fnd_d", out)
        data = json.loads(self.run_impact("--json", expect=3)[1])
        self.assertEqual(data["affected"][0]["next"][0], command)
        # A real campaign config, but deliberately no published counterpart.
        initialized = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "init", "--directory",
                                      str(self.root), "--county", "Synthetic", "--state", "CA",
                                      "--name", "Synthetic campaign"], cwd=REPO_ROOT, capture_output=True, text=True,
                                     timeout=30)
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        before = snapshot(self.root)
        result = subprocess.run([sys.executable, "-B", "-m", *shlex.split(command)], cwd=REPO_ROOT,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("blockers", result.stdout)
        self.assertEqual(snapshot(self.root), before)

    def test_pending_correction_is_distinct_and_emits_a_working_diff(self):
        rule_of(self.new, "ca-alpha")["duty"] = "Synthetic duty, amended."
        self.finding("fnd_c")
        draft = self.finding("fnd_c", where="private/drafts", title="Synthetic amended title")
        _, out, _ = self.run_impact(expect=3)
        self.assertIn("fnd_c pending correction 2024-05-01", out)
        data = json.loads(self.run_impact("--json", expect=3)[1])
        entry = next(e for e in data["affected"] if e["state"] == "pending correction")
        command = f"campaign_tool finding diff fnd_c --directory {self.root} --from {draft}"
        self.assertEqual(entry["next"][0], command)
        self.assertIn("if a correction is needed", entry["next"][1])
        before = snapshot(self.root)
        result = subprocess.run([sys.executable, "-B", "-m", *shlex.split(command)], cwd=REPO_ROOT,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn('title: "Synthetic fnd_c" -> "Synthetic amended title"', result.stdout)
        self.assertEqual(snapshot(self.root), before)

    def test_removed_rule_cited_by_a_published_finding_is_named_removed(self):
        self.new["rules"] = [r for r in self.new["rules"] if r["rule_id"] != "ca-gamma"]
        self.finding("fnd_r", "2021-03-01", ["ca-gamma"])
        self.finding("fnd_out", "2024-03-01", ["ca-gamma"])
        _, out, _ = self.run_impact(expect=3)
        self.assertIn("removed ca-gamma; window 2020-01-01..2023-12-31", out)
        self.assertIn("fnd_r published 2021-03-01 cites ca-gamma (removed)", out)
        self.assertNotIn("fnd_out", out)

    def test_corrected_published_finding_is_listed_as_published(self):
        rule_of(self.new, "ca-alpha")["citation"] = "Synthetic Code, renumbered"
        self.finding("fnd_c", corrections=[{"date": "2026-01-01", "note": "Synthetic note."}])
        _, out, _ = self.run_impact(expect=3)
        self.assertIn("fnd_c published 2024-05-01 cites ca-alpha (changed)", out)
        self.assertIn("changed ca-alpha: citation;", out)

    def test_withdrawn_finding_is_not_listed_and_unreadable_draft_is_counted(self):
        rule_of(self.new, "ca-alpha")["duty"] = "Synthetic duty, amended."
        self.finding("fnd_w", where="private/withdrawn", withdrawn={"date": "2026-01-01", "reason": "Synthetic."})
        (self.root / "private" / "drafts" / "broken.json").write_text("{not json", encoding="utf-8")
        code, out, _ = self.run_impact(expect=0)
        self.assertNotIn("fnd_w", out)
        self.assertIn("unreadable files not checked: 1", out)
        self.assertIn(f"unreadable {self.root}/private/drafts/broken.json", out)
        data = json.loads(self.run_impact("--json", expect=0)[1])
        self.assertEqual(data["counts"]["unreadable"], 1)
        self.assertEqual(data["unreadable"], [f"{self.root}/private/drafts/broken.json"])

    def test_draft_with_no_event_date_is_listed_and_alone_does_not_fail(self):
        rule_of(self.new, "ca-alpha")["duty"] = "Synthetic duty, amended."
        self.finding("fnd_e", "[EDIT ME: event date]", where="private/drafts")
        self.finding("fnd_m", where="private/drafts")
        path = self.root / "private" / "drafts" / "fnd_m.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        del document["event_date"]
        path.write_text(json.dumps(document), encoding="utf-8")
        _, out, _ = self.run_impact(expect=0)
        self.assertIn("findings affected: 0", out)
        self.assertIn("drafts with no event date: 2", out)
        self.assertIn("fnd_e draft [EDIT ME: event date] cites ca-alpha", out)
        self.assertIn("fnd_m draft no event_date cites ca-alpha", out)
        self.finding("fnd_a")  # a dated published finding in the window still sets exit 3
        self.run_impact(expect=3)

    def test_identical_packages_report_no_rule_changed(self):
        self.finding("fnd_a")
        _, out, _ = self.run_impact(expect=0)
        self.assertIn("no rule changed", out)
        self.assertIn("findings affected: 0", out)

    def test_other_rule_fields_are_ignored(self):
        rule_of(self.new, "ca-alpha")["remedy"] = "Synthetic remedy, amended."
        rule_of(self.new, "ca-alpha")["review"] = "verified"
        self.finding("fnd_a")
        _, out, _ = self.run_impact(expect=0)
        self.assertIn("no rule changed", out)

    def test_different_jurisdictions_exit_2(self):
        self.new["jurisdiction"] = "us-wa"
        for rule in self.new["rules"]:
            rule["rule_id"] = "wa" + rule["rule_id"][2:]
        for scope in self.new["request_scopes"]:
            scope["rule_ids"] = ["wa-alpha"]
        _, out, err = self.run_impact(expect=2)
        self.assertEqual(out, "")
        self.assertIn("us-ca", err)
        self.assertIn("us-wa", err)

    def test_invalid_package_exit_2_names_the_file(self):
        del self.new["rules"][0]["duty"]
        _, out, err = self.run_impact(expect=2)
        self.assertEqual(out, "")
        self.assertIn(str(self.tmp / "new.json"), err)
        self.assertNotIn(str(self.tmp / "old.json"), err)
        self.write("old.json", "{not json")
        _, _, err = self.run_impact(expect=2, write=False)
        self.assertIn(str(self.tmp / "old.json"), err)

    def test_missing_package_file_and_missing_directory_exit_2(self):
        self.write("old.json", self.old)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = law.main(["impact", str(self.tmp / "old.json"), str(self.tmp / "absent.json"),
                             "--directory", str(self.root)])
        self.assertEqual(code, 2)
        self.assertIn("absent.json", err.getvalue())
        self.write("new.json", self.new)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = law.main(["impact", str(self.tmp / "old.json"), str(self.tmp / "new.json"),
                             "--directory", str(self.tmp / "no-such-folder")])
        self.assertEqual(code, 2)
        self.assertIn("no-such-folder", err.getvalue())

    def test_exceptions_reordered_counts_as_changed(self):
        rule_of(self.new, "ca-alpha")["exceptions"].reverse()
        _, out, _ = self.run_impact(expect=0)
        self.assertIn("changed ca-alpha: exceptions; window 2016-01-01..open", out)

    def test_read_only_directory_bytes_and_package_status_unchanged(self):
        rule_of(self.new, "ca-alpha")["duty"] = "Synthetic duty, amended."
        self.new["status"] = "reviewed"
        self.new["reviewed_by"], self.new["reviewed_at"] = ["Synthetic Reviewer"], "2026-01-01"
        self.finding("fnd_a")
        self.finding("fnd_d", where="private/drafts")
        (self.root / "private" / "drafts" / "broken.json").write_text("{not json", encoding="utf-8")
        self.write("old.json", self.old)
        self.write("new.json", self.new)
        before, packages = snapshot(self.root), snapshot(self.tmp)
        _, out, _ = self.run_impact(expect=3, write=False)
        self.run_impact("--json", expect=3, write=False)
        self.assertEqual(snapshot(self.root), before)
        self.assertEqual(snapshot(self.tmp).keys(), packages.keys())
        self.assertEqual({k: v for k, v in snapshot(self.tmp).items() if k.endswith(".json") and "/" not in k},
                         {k: v for k, v in packages.items() if "/" not in k})
        self.assertIn("old.json (draft)", out)
        self.assertIn("new.json (reviewed)", out)

    def test_json_matches_the_text_counts(self):
        rule_of(self.new, "ca-alpha")["duty"] = "Synthetic duty, amended."
        self.new["rules"] = [r for r in self.new["rules"] if r["rule_id"] != "ca-gamma"]
        added = copy.deepcopy(rule_of(self.new, "ca-alpha"))
        added.update(rule_id="ca-delta")
        self.new["rules"].append(added)
        self.finding("fnd_a")
        self.finding("fnd_g", "2021-01-01", ["ca-gamma"], where="private/drafts")
        self.finding("fnd_e", "[EDIT ME: date]", ["ca-delta"], where="private/drafts")
        _, text, _ = self.run_impact(expect=3)
        _, raw, _ = self.run_impact("--json", expect=3)
        data = json.loads(raw)
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(data["counts"], {"changed": 1, "added": 1, "removed": 1, "affected": 2, "undated": 1,
                                          "unreadable": 0})
        self.assertIn("findings affected: 2", text)
        self.assertIn("drafts with no event date: 1", text)
        self.assertEqual([(a["id"], a["state"]) for a in data["affected"]], [("fnd_a", "published"), ("fnd_g", "draft")])
        self.assertEqual([r["kind"] for r in data["rules"]], ["changed", "added", "removed"])
        self.assertEqual(data["rules"][0]["window"], [{"from": "2016-01-01", "to": None}])
        self.assertEqual(data["affected"][0]["next"][0], f"campaign_tool finding diff fnd_a --directory {self.root} --from DRAFT")

    def test_module_entry_point_exit_code(self):
        rule_of(self.new, "ca-alpha")["duty"] = "Synthetic duty, amended."
        self.finding("fnd_a")
        old, new = self.write("old.json", self.old), self.write("new.json", self.new)
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.law", "impact", str(old), str(new),
                                 "--directory", str(self.root)], cwd=REPO_ROOT, capture_output=True, text=True,
                                timeout=30)
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("fnd_a published", result.stdout)


if __name__ == "__main__":
    unittest.main()
