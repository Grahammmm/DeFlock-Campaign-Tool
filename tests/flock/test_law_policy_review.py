"""flock_law_policy review STORE [--agency SLUG] [--today D]: LAW/POLICY rows still unreviewed or disputed (RF0550-13)."""
import csv
from datetime import date, timedelta
import hashlib
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "flock_law_policy.py"
sys.path.insert(0, str(ROOT))
from campaign_tool import law  # noqa: E402

LAW_HEADER = ["rule_id", "section", "duty", "official_url", "text_sha256", "accessed", "effective_from",
              "effective_to", "review", "added_by", "added_at", "status"]
POLICY_HEADER = ["policy_row_id", "agency", "policy_id", "version_label", "doc_sha256", "footer_version_date",
                 "adoption_evidence", "adoption_sha256", "governs_from", "governs_to", "added_by", "added_at", "status"]
DUTY, URL, SHA, ADDER = "Planted duty rv9", "https://oag.ca.gov/planted-url-rv9", "e" * 64, "planted-adder-rv9"
EVIDENCE = "Planted minutes rv9"
RULES = sorted(law.load_package("us-ca")["rules"], key=lambda r: r["rule_id"])  # derived at test time
TODAY = date(2026, 11, 2)


def tool_module():
    spec = importlib.util.spec_from_file_location("flock_law_policy_review_under_test", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run(*args):
    done = subprocess.run([sys.executable, "-B", str(TOOL), *args], capture_output=True, text=True, check=False)
    return done.returncode, done.stdout, done.stderr


def ago(days, today=TODAY):
    return (today - timedelta(days=days)).isoformat()


def law_row(rule, status="unreviewed", added=ago(5)):
    return {"rule_id": rule["rule_id"], "section": "Sec", "duty": DUTY, "official_url": URL, "text_sha256": SHA,
            "accessed": ago(1), "effective_from": rule["effective_from"], "effective_to": "",
            "review": rule["review"], "added_by": ADDER, "added_at": added, "status": status}


def pol(agency, pid, label, status="unreviewed", added=ago(5), start="2020-01-01"):
    d = hashlib.sha256(f"{agency}{pid}{label}".encode()).hexdigest()
    key = (agency.lower(), pid, label, d)  # agencies here are single lowercase-able letters: slug == lower
    rid = "P-" + hashlib.sha256("\x1f".join(key).encode()).hexdigest()[:12]
    return {"policy_row_id": rid, "agency": agency, "policy_id": pid, "version_label": label, "doc_sha256": d,
            "footer_version_date": "2019-12-01", "adoption_evidence": EVIDENCE, "adoption_sha256": "f" * 64,
            "governs_from": start, "governs_to": "", "added_by": ADDER, "added_at": added, "status": status}


def totals(lu=0, ld=0, pu=0, pd=0):
    return f"law_unreviewed={lu} law_disputed={ld} policy_unreviewed={pu} policy_disputed={pd}"


def law_line(rule, status, added=ago(5), age="(5 days)"):
    return f"review | law | {rule['rule_id']} | {status} | added {added} {age}"


def pol_line(r, age="(5 days)", text=None):
    text = text or f"{r['policy_id']} {r['version_label']}"
    return f"review | policy | {r['agency'].lower()} | {r['policy_row_id']} | {text} | {r['status']} | added {r['added_at']} {age}"


class ReviewTest(unittest.TestCase):
    def setUp(self):
        self.assertGreaterEqual(len(RULES), 3)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Path(tmp.name) / "store"
        self.store.mkdir()

    def write(self, path, header, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", newline="") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(header)
            writer.writerows([[r[k] for k in header] for r in rows])

    def fill(self, law_rows, policy_rows):
        self.write(self.store / "LAW.csv", LAW_HEADER, law_rows)
        for a in sorted({r["agency"] for r in policy_rows}):
            self.write(self.store / "agencies" / a.lower() / "POLICY.csv", POLICY_HEADER,
                       [r for r in policy_rows if r["agency"] == a])

    def snapshot(self):
        return sorted((str(p), p.read_bytes() if p.is_file() else b"", p.stat().st_mtime_ns) for p in self.store.rglob("*"))

    def review(self, *extra, today="2026-11-02"):  # (exit, stdout lines, stderr); the store is never written
        before = self.snapshot()
        code, out, err = run("review", str(self.store), *extra, *(["--today", today] if today else []))
        self.assertEqual(self.snapshot(), before, "store changed")
        return code, out.splitlines(), err

    def mixed(self):
        a, b, c = RULES[:3]
        laws = [law_row(c, "disputed"), law_row(a, "checked"), law_row(b)]
        a1, a2 = pol("A", "P2", "v1"), dict(pol("A", "P1", "v1", "checked"), governs_to="2021-12-31")
        a3 = pol("A", "P1", "v2", "disputed", start="2022-01-01")
        b1, b2 = dict(pol("B", "P1", "v1", "checked"), governs_to="2021-12-31"), pol("B", "P1", "v2", start="2022-01-01")
        self.fill(laws, [a1, a2, a3, b1, b2])
        return (b, c), (a3, a1), b2

    def test_mixed_lists_only_non_checked_rows_sorted(self):
        (b, c), (a3, a1), b2 = self.mixed()
        want = [law_line(b, "unreviewed"), law_line(c, "disputed"), pol_line(a3), pol_line(a1), pol_line(b2),
                totals(1, 1, 2, 1)]
        self.assertEqual(self.review(), (3, want, ""))

    def test_all_checked_totals_only(self):
        self.fill([law_row(r, "checked") for r in RULES[:3]], [pol("A", "P1", "v1", "checked"), pol("B", "P1", "v1", "checked")])
        self.assertEqual(self.review(), (0, [totals()], ""))

    def test_agency_scopes_policy_rows_only(self):
        (b, c), (a3, a1), _ = self.mixed()
        laws = [law_line(b, "unreviewed"), law_line(c, "disputed")]
        self.assertEqual(self.review("--agency", "a"), (3, laws + [pol_line(a3), pol_line(a1), totals(1, 1, 1, 1)], ""))
        self.assertEqual(self.review("--agency", "c"), (3, laws + [totals(1, 1)], ""))
        code, out, _ = run("review", str(self.store), "--today", "2026-11-02", "--agency", "c")  # keys in any order
        self.assertEqual((code, out.splitlines()), (3, laws + [totals(1, 1)]))
        self.fill([law_row(r, "checked") for r in RULES[:2]], [pol("A", "P1", "v1")])
        self.assertEqual(self.review("--agency", "c"), (0, [totals()], ""))  # exit follows the LAW rows only
        self.assertEqual(self.review("--agency", "a")[0], 3)

    def test_usage_errors_nothing_on_stdout(self):
        self.mixed()
        s = str(self.store)
        cases = [[s, "--agency", "Bad Slug"], [s, "--agency", "A"], [s, "--agency", ""], [s, "--agency", "-a"],
                 [s, "--agency", "a", "--agency", "b"], [s, "--today", "2026-11-02", "--today", "2026-11-03"],
                 [s, "--today", "2026-13-01"], [s, "--today", "26-11-02"], [s, "--today", ""], [s, "--bogus", "x"],
                 [s, "--agency"], [s, "--agency", "a", "x"], [s, "x"], [], [str(self.store / "LAW.csv")],
                 [str(self.store / "missing")], [s, "--agency", "a", "--today", "2026-11-02", "--on", "2026-11-02"]]
        for args in cases:
            before = self.snapshot()
            code, out, err = run("review", *args)
            self.assertEqual((code, out), (2, ""), args)
            self.assertTrue(err.startswith("usage:"), args)
            self.assertEqual(self.snapshot(), before)

    def test_pipe_replaced_and_long_text_cut_to_60(self):
        self.fill([law_row(RULES[0], "checked")], [pol("A", "P|1", "v|2 draft"), pol("A", "Q", "L" * 80)])
        code, out, err = self.review()
        self.assertEqual((code, err, out[-1]), (3, "", totals(pu=2)))
        self.assertEqual([x.split(" | ")[4] for x in out[:-1]], ["P/1 v/2 draft", "Q " + "L" * 60])
        self.assertTrue(all(len(x.split(" | ")) == 7 for x in out[:-1]))
        p = pol("A", "x" * 70 + "|", "y")
        self.assertEqual(tool_module().review([], [(2, p)], None, TODAY)[0], pol_line(p, text="x" * 60 + " y"))

    def test_future_and_same_day_ages(self):
        a, b = RULES[:2]
        p = pol("A", "P1", "v1", "disputed", added="2026-11-03")
        self.fill([law_row(a, added="2026-11-02"), law_row(b, "disputed", added="2026-12-01")], [p])
        self.assertEqual(self.review(), (3, [law_line(a, "unreviewed", "2026-11-02", "(0 days)"),
                                             law_line(b, "disputed", "2026-12-01", "(future)"),
                                             pol_line(p, "(future)"), totals(1, 1, 0, 1)], ""))
        code, out, _ = self.review(today="2026-12-01")
        self.assertEqual((code, out[:3]), (3, [law_line(a, "unreviewed", "2026-11-02", "(29 days)"),
                                               law_line(b, "disputed", "2026-12-01", "(0 days)"),
                                               pol_line(p, "(28 days)")]))

    def test_default_today_is_the_real_date(self):
        real = date.today()
        self.fill([law_row(RULES[0], added=ago(3, real))], [])
        self.assertEqual(self.review(today=None), (3, [law_line(RULES[0], "unreviewed", ago(3, real), "(3 days)"),
                                                       totals(1)], ""))

    def test_bad_policy_row_stderr_only(self):
        self.mixed()
        bad = dict(pol("B", "P9", "v9"), status="maybe")
        self.write(self.store / "agencies" / "b" / "POLICY.csv", POLICY_HEADER, [bad])
        code, out, err = self.review()
        self.assertEqual((code, out), (1, []))
        self.assertIn("status must be unreviewed, checked or disputed", err)
        self.assertNotIn("review |", err)
        (self.store / "agencies" / "b" / "POLICY.csv").write_text("not,a,header\n")
        self.assertEqual(self.review("--agency", "a")[:2], (1, []))  # any store error fails closed, even out of scope
        self.assertEqual(run("review", str(Path(self.store).parent / "empty-dir-missing"))[0], 2)

    def test_output_has_no_row_text(self):
        self.mixed()
        code, out, err = self.review()
        self.assertEqual((code, err), (3, ""))
        text = "\n".join(out)
        for leak in (DUTY, URL, SHA, ADDER, EVIDENCE, "f" * 64, "Sec", str(self.store), "LAW.csv"):
            self.assertNotIn(leak, text)

    def test_help_lists_review(self):
        code, out, _ = run("--help")
        self.assertEqual(code, 0)
        self.assertIn("review STORE [--agency SLUG] [--today YYYY-MM-DD]", out)


if __name__ == "__main__":
    unittest.main()
