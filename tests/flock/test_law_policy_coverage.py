"""flock_law_policy coverage STORE [--on D]: us-ca package rules with no LAW.csv row, none in effect, only disputed (DF1537-04)."""
import contextlib
import csv
from datetime import date, timedelta
import importlib.util
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "flock_law_policy.py"
sys.path.insert(0, str(ROOT))
from campaign_tool import law  # noqa: E402

LAW_HEADER = ["rule_id", "section", "duty", "official_url", "text_sha256", "accessed", "effective_from",
              "effective_to", "review", "added_by", "added_at", "status"]
TAIL = " | research view, not a legal conclusion"
DUTY, URL, SHA = "Synthetic duty text", "https://leginfo.legislature.ca.gov/synthetic", "d" * 64
RULES = sorted(law.load_package("us-ca")["rules"], key=lambda r: r["rule_id"])  # derived at test time, no fixed count
IDS = [r["rule_id"] for r in RULES]


def tool_module():
    spec = importlib.util.spec_from_file_location("flock_law_policy_coverage_under_test", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run(*args):
    done = subprocess.run([sys.executable, "-B", str(TOOL), *args], capture_output=True, text=True, check=False)
    return done.returncode, done.stdout, done.stderr


def law_row(rule, to="", status="unreviewed"):
    return {"rule_id": rule["rule_id"], "section": "Sec", "duty": DUTY, "official_url": URL, "text_sha256": SHA,
            "accessed": "2026-10-01", "effective_from": rule["effective_from"], "effective_to": to,
            "review": rule["review"], "added_by": "tester", "added_at": "2026-10-30", "status": status}


def counts(covered, missing=0, not_in_effect=0, disputed_only=0):
    return (f"rules={len(IDS)} covered={covered} missing={missing} not_in_effect={not_in_effect}"
            f" disputed_only={disputed_only}{TAIL}")


def late(on):  # package rules whose effective_from is after `on`: never in effect that day
    return [r["rule_id"] for r in RULES if r["effective_from"] > on]


class CoverageTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Path(tmp.name) / "store"
        self.store.mkdir()

    def write(self, rows):
        with open(self.store / "LAW.csv", "w", newline="") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(LAW_HEADER)
            writer.writerows([[r[k] for k in LAW_HEADER] for r in rows])

    def snapshot(self):
        return sorted((str(p), p.read_bytes() if p.is_file() else b"", p.stat().st_mtime_ns) for p in self.store.rglob("*"))

    def coverage(self, *extra, store=None):  # (exit, stdout lines, stderr); the store is never written
        before = self.snapshot()
        code, out, err = run("coverage", str(store or self.store), *extra)
        self.assertEqual(self.snapshot(), before, "store changed")
        return code, out.splitlines(), err

    def test_every_rule_has_a_row(self):
        self.write([law_row(r) for r in RULES])
        self.assertEqual(self.coverage(), (0, [counts(len(IDS))], ""))
        self.assertEqual(self.coverage("--on", "2099-01-01"), (0, [counts(len(IDS))], ""))  # open-ended rows: covered

    def test_omitted_rules_named_with_review_label_sorted(self):
        gone = [RULES[-1], RULES[0]]
        self.write([law_row(r) for r in RULES if r not in gone])
        code, out, err = self.coverage()
        want = [f"coverage | {r['rule_id']} | no LAW row | review {r['review']}" for r in (RULES[0], RULES[-1])]
        self.assertEqual((code, out, err), (3, want + [counts(len(IDS) - 2, missing=2)], ""))
        for leak in (DUTY, URL, SHA, str(self.store), "LAW.csv"):
            self.assertNotIn(leak, "\n".join(out))
        attorney = [r for r in RULES if r["review"] == "needs_attorney_review"][:1]
        if attorney:  # reported like any other rule, label printed for prioritising
            self.write([law_row(r) for r in RULES if r not in attorney])
            self.assertIn(f"coverage | {attorney[0]['rule_id']} | no LAW row | review needs_attorney_review", self.coverage()[1])

    def test_on_before_from_after_to_and_inclusive_ends(self):
        target = RULES[0]
        start = date.fromisoformat(target["effective_from"])
        end = (start + timedelta(days=30)).isoformat()
        self.write([law_row(r, to=end if r is target else "") for r in RULES])
        miss = f"coverage | {target['rule_id']} | not in effect on %s (1 rows)"
        for on in ((start - timedelta(days=1)).isoformat(), (start + timedelta(days=31)).isoformat()):
            code, out, _ = self.coverage("--on", on)
            flagged = sorted(set(late(on)) | {target["rule_id"]})
            self.assertEqual(code, 3, on)
            self.assertIn(miss % on, out)
            self.assertEqual(out[-1], counts(len(IDS) - len(flagged), not_in_effect=len(flagged)), on)
        for on in (target["effective_from"], end):  # both ends inclusive
            self.assertNotIn(miss % on, self.coverage("--on", on)[1])
        self.assertEqual(self.coverage()[:2], (0, [counts(len(IDS))]))  # without --on dates are not checked

    def test_disputed_only_with_and_without_on(self):
        target = RULES[0]
        self.write([law_row(r, status="disputed" if r is target else "checked") for r in RULES])
        line = f"coverage | {target['rule_id']} | only disputed rows (1)"
        self.assertEqual(self.coverage(), (3, [line, counts(len(IDS) - 1, disputed_only=1)], ""))
        on = (date.fromisoformat(target["effective_from"]) - timedelta(days=1)).isoformat()
        code, out, _ = self.coverage("--on", on)  # one rule, two findings: counted once in covered
        flagged = sorted(set(late(on)) | {target["rule_id"]})
        self.assertEqual(out[:2], [f"coverage | {target['rule_id']} | not in effect on {on} (1 rows)", line])
        self.assertEqual(out[-1], counts(len(IDS) - len(flagged), not_in_effect=len(flagged), disputed_only=1))

    def test_multiple_rows_per_rule_in_function(self):  # a clean LAW.csv has one row per rule_id; the function takes any
        mod, rule = tool_module(), RULES[0]
        rows = [(n, law_row(r)) for n, r in enumerate(RULES[1:], 3)]
        mixed = [(2, law_row(rule, status="checked")), (99, law_row(rule, status="disputed"))]
        self.assertEqual(mod.coverage(mixed + rows, RULES), [counts(len(IDS))])
        both = [(2, law_row(rule, status="disputed")), (99, law_row(rule, status="disputed"))]
        self.assertEqual(mod.coverage(both + rows, RULES)[0], f"coverage | {rule['rule_id']} | only disputed rows (2)")

    def test_load_errors_print_nothing_on_stdout(self):
        rows = [law_row(r) for r in RULES]
        cases = [("row 3: rule_id not in law package", rows[:1] + [dict(rows[0], rule_id="ca-not-a-real-rule")]),
                 ("duplicates an earlier row", rows + [dict(rows[0], status="disputed")]),
                 ("row 2: status must be", [dict(rows[0], status="DISPUTED")] + rows[1:])]
        for want, table in cases:
            self.write(table)
            code, out, err = self.coverage()
            self.assertEqual((code, out), (1, []), want)
            self.assertIn(want, err)
        self.write(rows)  # a POLICY.csv error still fails closed
        (self.store / "agencies" / "x").mkdir(parents=True)
        (self.store / "agencies" / "x" / "POLICY.csv").write_text("not,a,header\n")
        code, out, err = self.coverage()
        self.assertEqual((code, out), (1, []))
        self.assertIn("x: row 1: header does not match POLICY.csv", err)

    def test_store_without_law_csv(self):
        self.assertEqual(self.coverage(), (1, [], "store: no LAW.csv\n"))

    def test_usage_errors(self):
        self.write([law_row(r) for r in RULES])
        s = str(self.store)
        for args in (["coverage", str(self.store / "LAW.csv")], ["coverage", s, "--on", "2024-02-30"],
                     ["coverage", s, "--on", "24-01-01"], ["coverage", s, "--on"], ["coverage", s, "--at", "2024-01-01"],
                     ["coverage", s, "--on", "2024-01-01", "x"], ["coverage", str(self.store / "missing")], ["coverage"]):
            before = self.snapshot()
            code, out, err = run(*args)
            self.assertEqual((code, out), (2, ""), args)
            self.assertTrue(err.startswith("usage:"), args)
            self.assertEqual(self.snapshot(), before)

    def test_package_unreadable_no_partial_report(self):
        self.write([law_row(r) for r in RULES])
        mod, real = tool_module(), law.load_package("us-ca")
        for effect in ([ValueError("boom")], [real, ValueError("boom")]):  # fails in load_all, or in coverage itself
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(mod.law, "load_package", side_effect=effect), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = mod.main(["coverage", str(self.store)])
            self.assertEqual((code, out.getvalue()), (1, ""))
            self.assertIn("row 1: law package unreadable", err.getvalue())

    def test_help_lists_coverage(self):
        code, out, _ = run("--help")
        self.assertEqual(code, 0)
        self.assertIn("coverage STORE [--on YYYY-MM-DD]", out)


if __name__ == "__main__":
    unittest.main()
