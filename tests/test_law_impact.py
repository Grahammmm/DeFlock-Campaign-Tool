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
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import law, law_impact  # noqa: E402

MAX_DAY = date.max.isoformat()
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

    # RF1635-1: an inclusive end of 9999-12-31 ends at the last representable day, like no end at all.
    def test_maximum_end_and_open_have_empty_representable_difference(self):
        self.finding("fnd_boundary", MAX_DAY)
        for old_end, new_end in ((None, MAX_DAY), (MAX_DAY, None)):
            with self.subTest(old=old_end, new=new_end):
                rule_of(self.old, "ca-alpha")["effective_to"] = old_end
                rule_of(self.new, "ca-alpha")["effective_to"] = new_end
                self.assertEqual(law_impact.diff_rules(self.old, self.new),
                                 [dict(rule_id="ca-alpha", kind="changed", fields=["effective_to"], window=[])])
                _, out, err = self.run_impact(expect=0)
                self.assertEqual(err, "")
                self.assertIn("changed ca-alpha: effective_to; window none (no representable event dates)\n", out)
                self.assertNotIn("no rule changed", out)
                self.assertIn("findings affected: 0", out)
                self.assertNotIn("10000", out)
                _, raw, err = self.run_impact("--json", expect=0)
                self.assertEqual(err, "")
                data = json.loads(raw)
                self.assertEqual(data["rules"], [dict(rule_id="ca-alpha", kind="changed", fields=["effective_to"],
                                                      window=[])])
                self.assertEqual(data["counts"], {"changed": 1, "added": 0, "removed": 0, "affected": 0,
                                                  "undated": 0, "unreadable": 0})
                self.assertEqual(data["affected"], [])

    def test_last_representable_day_is_not_clamped_away_or_invented(self):
        self.finding("fnd_last", MAX_DAY)
        self.finding("fnd_earlier", "9999-12-30")
        cases = (((None, "9999-12-30"), "9999-12-31..open", None),
                 ((MAX_DAY, "9999-12-30"), "9999-12-31..9999-12-31", MAX_DAY))
        for (wide, narrow), text, to in cases:
            for old_end, new_end in ((wide, narrow), (narrow, wide)):
                with self.subTest(old=old_end, new=new_end):
                    rule_of(self.old, "ca-alpha")["effective_to"] = old_end
                    rule_of(self.new, "ca-alpha")["effective_to"] = new_end
                    _, out, err = self.run_impact(expect=3)
                    self.assertEqual(err, "")
                    self.assertIn(f"changed ca-alpha: effective_to; window {text}\n", out)
                    self.assertIn("fnd_last published 9999-12-31 cites ca-alpha (changed)", out)
                    self.assertNotIn("fnd_earlier", out)
                    data = json.loads(self.run_impact("--json", expect=3)[1])
                    self.assertEqual(data["rules"][0]["window"], [{"from": MAX_DAY, "to": to}])
                    self.assertEqual([a["id"] for a in data["affected"]], ["fnd_last"])

    def test_maximum_date_content_union_added_and_removed_rules(self):
        rule_of(self.new, "ca-alpha")["duty"] = "Synthetic duty, amended."
        rule_of(self.new, "ca-alpha")["effective_to"] = MAX_DAY
        last = copy.deepcopy(rule_of(self.new, "ca-beta"))
        last.update(rule_id="ca-delta", effective_from=MAX_DAY, effective_to=MAX_DAY)
        self.new["rules"].append(last)
        gone = copy.deepcopy(rule_of(self.old, "ca-beta"))
        gone.update(rule_id="ca-epsilon", effective_from=MAX_DAY, effective_to=None)
        self.old["rules"].append(gone)
        self.finding("fnd_a", MAX_DAY, ["ca-alpha"])
        self.finding("fnd_d", MAX_DAY, ["ca-delta"])
        self.finding("fnd_e", MAX_DAY, ["ca-epsilon"])
        self.finding("fnd_early", "9999-12-30", ["ca-delta", "ca-epsilon"])
        _, out, err = self.run_impact(expect=3)
        self.assertEqual(err, "")
        self.assertIn("changed ca-alpha: duty, effective_to; window 2016-01-01..open\n"
                      "added ca-delta; window 9999-12-31..9999-12-31\n"
                      "removed ca-epsilon; window 9999-12-31..open\n", out)
        data = json.loads(self.run_impact("--json", expect=3)[1])
        self.assertEqual([r["window"] for r in data["rules"]], [[{"from": "2016-01-01", "to": None}],
                                                                [{"from": MAX_DAY, "to": MAX_DAY}],
                                                                [{"from": MAX_DAY, "to": None}]])
        self.assertEqual([(a["id"], a["kinds"]) for a in data["affected"]],
                         [("fnd_a", {"ca-alpha": "changed"}), ("fnd_d", {"ca-delta": "added"}),
                          ("fnd_e", {"ca-epsilon": "removed"})])

    def test_window_membership_matches_independent_calendar_oracle(self):
        spans = [("0001-01-01", "0001-01-01"), ("0001-01-01", None), ("0001-01-02", "2016-01-01"),
                 ("2024-02-29", "2024-02-29"), ("2024-02-28", "2024-03-01"), ("2020-01-01", "2023-12-31"),
                 ("2023-06-01", "2025-01-01"), ("2016-01-01", None), ("2016-01-01", "9999-12-30"),
                 ("2016-01-01", MAX_DAY), ("9999-12-30", None), (MAX_DAY, MAX_DAY), (MAX_DAY, None)]
        last = date.max.toordinal()
        edges = {date.fromisoformat(e).toordinal() for span in spans for e in span if e} | {1, last}
        days = {date.fromordinal(n) for edge in edges for n in (edge - 1, edge, edge + 1) if 1 <= n <= last}

        def rule(start, end, duty="Synthetic duty"):
            return dict(rule_id="ca-x", citation="Synthetic", duty=duty, exceptions=[], effective_from=start,
                        effective_to=end)

        def member(span, day):
            return date.fromisoformat(span[0]) <= day and (span[1] is None or day <= date.fromisoformat(span[1]))

        def check(window, expected):
            self.assertEqual(window, sorted(window))
            for lo, hi in window:
                self.assertTrue(1 <= lo <= date.max.toordinal() and lo < hi, (lo, hi))
            for previous, following in zip(window, window[1:]):
                self.assertLess(previous[1], following[0])
            got = {day for day in days if any(lo <= day.toordinal() < hi for lo, hi in window)}
            self.assertEqual(got, {day for day in days if expected(day)})

        def rows(old, new):
            return law_impact.diff_rules({"rules": old}, {"rules": new})

        for old in spans:
            check(rows([], [rule(*old)])[0]["window"], lambda day: member(old, day))
            check(rows([rule(*old)], [])[0]["window"], lambda day: member(old, day))
            for new in spans:
                with self.subTest(old=old, new=new):
                    dated = rows([rule(*old)], [rule(*new)])
                    check(dated[0]["window"] if dated else [], lambda day: member(old, day) != member(new, day))
                    content = rows([rule(*old)], [rule(*new, duty="Synthetic duty, amended.")])
                    check(content[0]["window"], lambda day: member(old, day) or member(new, day))

    def test_empty_window_keeps_undated_and_other_rule_reporting(self):
        rule_of(self.new, "ca-alpha")["effective_to"] = MAX_DAY
        rule_of(self.new, "ca-beta")["duty"] = "Synthetic duty, amended."
        self.finding("fnd_u", "[EDIT ME: event date]", ["ca-alpha"], where="private/drafts")
        self.finding("fnd_a", MAX_DAY, ["ca-alpha"])
        self.finding("fnd_b", "2024-05-01", ["ca-beta"])
        _, out, err = self.run_impact(expect=3)
        self.assertEqual(err, "")
        self.assertIn("changed ca-alpha: effective_to; window none (no representable event dates)\n"
                      "changed ca-beta: duty; window 2016-01-01..open\n"
                      "findings affected: 1\n"
                      "fnd_b published 2024-05-01 cites ca-beta (changed)\n", out)
        self.assertIn("drafts with no event date: 1\nfnd_u draft [EDIT ME: event date] cites ca-alpha", out)
        data = json.loads(self.run_impact("--json", expect=3)[1])
        self.assertEqual(data["counts"], {"changed": 2, "added": 0, "removed": 0, "affected": 1, "undated": 1,
                                          "unreadable": 0})
        self.assertEqual([(r["rule_id"], r["window"]) for r in data["rules"]],
                         [("ca-alpha", []), ("ca-beta", [{"from": "2016-01-01", "to": None}])])
        self.assertEqual([u["id"] for u in data["undated"]], ["fnd_u"])
        rule_of(self.new, "ca-beta")["duty"] = rule_of(self.old, "ca-beta")["duty"]
        self.run_impact(expect=0)  # the undated draft alone still does not fail

    def test_boundary_reports_preserve_bytes_and_legacy_outputs(self):
        rule_of(self.new, "ca-alpha")["effective_to"] = MAX_DAY
        self.finding("fnd_a", MAX_DAY)
        self.finding("fnd_d", MAX_DAY, where="private/drafts")
        old, new = self.write("old.json", self.old), self.write("new.json", self.new)
        before, packages = snapshot(self.root), snapshot(self.tmp)
        modes = {p: p.stat().st_mode for p in (old, new, *self.root.rglob("*"))}
        self.run_impact(expect=0, write=False)
        self.run_impact("--json", expect=0, write=False)
        self.assertEqual(snapshot(self.root), before)
        self.assertEqual(snapshot(self.tmp), packages)
        self.assertEqual({p: p.stat().st_mode for p in (old, new, *self.root.rglob("*"))}, modes)
        self.assertEqual(json.loads(new.read_text(encoding="utf-8"))["status"], "draft")
        # An ordinary amendment prints exactly what it printed before the repair.
        rule_of(self.new, "ca-alpha").update(effective_to=None, duty="Synthetic duty, amended.")
        self.finding("fnd_a")
        (self.root / "private" / "drafts" / "fnd_d.json").unlink()
        _, out, _ = self.run_impact(expect=3)
        d = self.root
        self.assertEqual(out, (
            f"law impact us-ca: old {self.tmp}/old.json (draft), new {self.tmp}/new.json (draft)\n"
            "changed ca-alpha: duty; window 2016-01-01..open\n"
            "findings affected: 1\n"
            "fnd_a published 2024-05-01 cites ca-alpha (changed)\n"
            f"  next: campaign_tool finding diff fnd_a --directory {d} --from DRAFT\n"
            f"  then if a correction is needed: campaign_tool finding correct fnd_a --directory {d} "
            "--date YYYY-MM-DD --note TEXT --approved-by NAME [--from DRAFT]\n"
            "drafts with no event date: 0\n" + law_impact.NOTE + "\n"))
        rule_of(self.new, "ca-alpha")["effective_to"] = "9999-12-32"  # still not a date: input error
        _, out, err = self.run_impact(expect=2)
        self.assertEqual(out, "")
        self.assertIn(str(self.tmp / "new.json"), err)

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
