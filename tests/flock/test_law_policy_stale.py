"""flock_law_policy stale STORE --max-age-days N [--today D]: LAW rows whose text was accessed too long ago, or in the future (RF0550-12)."""
import csv
from datetime import date, timedelta
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
TAIL = " | research view, not a legal conclusion"
DUTY, URL, SHA, ADDER = "Planted duty zq7", "https://oag.ca.gov/planted-url-zq7", "e" * 64, "planted-adder-zq7"
RULES = sorted(law.load_package("us-ca")["rules"], key=lambda r: r["rule_id"])  # derived at test time
TODAY = date(2026, 11, 2)


def tool_module():
    spec = importlib.util.spec_from_file_location("flock_law_policy_stale_under_test", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run(*args):
    done = subprocess.run([sys.executable, "-B", str(TOOL), *args], capture_output=True, text=True, check=False)
    return done.returncode, done.stdout, done.stderr


def ago(days, today=TODAY):
    return (today - timedelta(days=days)).isoformat()


def law_row(rule, accessed, status="unreviewed"):
    return {"rule_id": rule["rule_id"], "section": "Sec", "duty": DUTY, "official_url": URL, "text_sha256": SHA,
            "accessed": accessed, "effective_from": rule["effective_from"], "effective_to": "",
            "review": rule["review"], "added_by": ADDER, "added_at": "2026-10-30", "status": status}


def totals(n, stale=0, future=0, max_age=180, today=TODAY):
    return f"law={n} stale={stale} future={future} max_age_days={max_age} today={today.isoformat()}"


class StaleTest(unittest.TestCase):
    def setUp(self):
        self.assertGreaterEqual(len(RULES), 3)
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

    def stale(self, *extra, today="2026-11-02", max_age="180"):  # (exit, stdout lines, stderr); the store is never written
        before = self.snapshot()
        args = ["stale", str(self.store), "--max-age-days", max_age] + (["--today", today] if today else [])
        code, out, err = run(*args, *extra)
        self.assertEqual(self.snapshot(), before, "store changed")
        return code, out.splitlines(), err

    def test_one_stale_of_three_boundary_not_stale(self):
        a, b, c = RULES[:3]
        self.write([law_row(a, ago(10)), law_row(b, ago(180), "checked"), law_row(c, ago(400), "disputed")])
        want = f"stale | {c['rule_id']} | accessed {ago(400)} | 400 days | disputed{TAIL}"
        self.assertEqual(self.stale(), (3, [want, totals(3, stale=1)], ""))
        self.write([law_row(a, ago(10)), law_row(b, ago(181), "checked"), law_row(c, ago(180))])  # one day past: stale
        code, out, _ = self.stale()
        self.assertEqual((code, out), (3, [f"stale | {b['rule_id']} | accessed {ago(181)} | 181 days | checked{TAIL}",
                                            totals(3, stale=1)]))

    def test_future_access_flagged(self):
        a, b = RULES[:2]
        self.write([law_row(a, "2026-12-01"), law_row(b, ago(0))])  # accessed today: age 0, fresh
        want = f"future | {a['rule_id']} | accessed 2026-12-01 | -29 days | unreviewed{TAIL}"
        self.assertEqual(self.stale(), (3, [want, totals(2, future=1)], ""))
        self.write([law_row(a, ago(-1)), law_row(b, ago(500))])  # tomorrow is future; both kinds counted
        code, out, _ = self.stale(max_age="3650")
        self.assertEqual((code, out[0], out[-1]), (3, f"future | {a['rule_id']} | accessed {ago(-1)} | -1 days | unreviewed{TAIL}",
                                                   totals(2, future=1, max_age=3650)))
        self.assertEqual(self.stale(max_age="1")[1][-1], totals(2, stale=1, future=1, max_age=1))

    def test_all_fresh_only_totals(self):
        self.write([law_row(r, ago(i % 30)) for i, r in enumerate(RULES)])
        self.assertEqual(self.stale(), (0, [totals(len(RULES))], ""))

    def test_default_today_is_the_real_date(self):
        a, b = RULES[:2]
        real = date.today()
        self.write([law_row(a, ago(400, real)), law_row(b, ago(0, real))])
        code, out, err = self.stale(today=None)
        self.assertEqual((code, out[-1], err), (3, totals(2, stale=1, today=real), ""))
        self.assertTrue(out[0].startswith(f"stale | {a['rule_id']} | accessed {ago(400, real)} | 400 days"))

    def test_sorted_by_rule_id_not_csv_order(self):
        rows = [law_row(r, ago(400)) for r in reversed(RULES[:3])]
        self.write(rows)
        code, out, _ = self.stale()
        self.assertEqual(code, 3)
        self.assertEqual([x.split(" | ")[1] for x in out[:-1]], [r["rule_id"] for r in RULES[:3]])

    def test_same_rule_twice_loader_refuses_function_keeps_csv_order(self):
        a, b = RULES[:2]
        self.write([law_row(a, ago(400)), law_row(a, ago(500))])  # main's loader: one row per rule_id
        code, out, err = self.stale()
        self.assertEqual((code, out), (1, []))
        self.assertIn("row 3: rule_id duplicates an earlier row", err)
        rows = [(7, law_row(b, ago(300))), (5, law_row(a, ago(500))), (2, law_row(a, ago(400)))]
        self.assertEqual(tool_module().stale(rows, TODAY, 180), [
            f"stale | {a['rule_id']} | accessed {ago(400)} | 400 days | unreviewed{TAIL}",
            f"stale | {a['rule_id']} | accessed {ago(500)} | 500 days | unreviewed{TAIL}",
            f"stale | {b['rule_id']} | accessed {ago(300)} | 300 days | unreviewed{TAIL}", totals(3, stale=3)])

    def test_usage_errors_nothing_on_stdout(self):
        self.write([law_row(r, ago(400)) for r in RULES[:2]])
        s = str(self.store)
        cases = [[s, "--max-age-days", n] for n in ("0", "3651", "-5", "1e3", "ten", "", " 10", "10.0", "00000")]
        cases += [[s, "--max-age-days", "180", "--today", d] for d in ("2026-02-30", "26-11-02", "", "2026-11-2")]
        cases += [[s, "--max-age-days", "180", "x"], [s, "--max-age-days", "180", "--today", "2026-11-02", "x"],
                  [s, "--max-age-days"], [s], [], [s, "--max-age", "180"], [s, "--max-age-days", "180", "--on", "2026-11-02"],
                  [s, "--today", "2026-11-02", "--max-age-days", "180"], [str(self.store / "LAW.csv"), "--max-age-days", "180"],
                  [str(self.store / "missing"), "--max-age-days", "180"]]
        for args in cases:
            before = self.snapshot()
            code, out, err = run("stale", *args)
            self.assertEqual((code, out), (2, ""), args)
            self.assertTrue(err.startswith("usage:"), args)
            self.assertEqual(self.snapshot(), before)
        self.assertEqual(run("stale", s, "--max-age-days", "3650", "--today", "2026-11-02")[0], 0)  # both bounds valid
        self.assertEqual(run("stale", s, "--max-age-days", "1", "--today", "2026-11-02")[0], 3)

    def test_store_without_law_csv(self):
        self.assertEqual(self.stale(), (1, [], "store: no LAW.csv\n"))
        (self.store / "agencies" / "x").mkdir(parents=True)  # a POLICY-only store is still refused
        self.assertEqual(self.stale(), (1, [], "store: no LAW.csv\n"))

    def test_bad_row_error_on_stderr_only(self):
        a, b = RULES[:2]
        cases = [("row 3: accessed must be YYYY-MM-DD", [law_row(a, ago(400)), law_row(b, "2026-13-01")]),
                 ("row 2: rule_id not in law package", [dict(law_row(a, ago(400)), rule_id="ca-not-a-real-rule")])]
        for want, rows in cases:
            self.write(rows)
            code, out, err = self.stale()
            self.assertEqual((code, out), (1, []), want)
            self.assertIn(want, err)
            self.assertNotIn("stale |", err)
        self.write([law_row(a, ago(400))])  # a POLICY.csv error still fails closed
        (self.store / "agencies" / "x").mkdir(parents=True)
        (self.store / "agencies" / "x" / "POLICY.csv").write_text("not,a,header\n")
        code, out, err = self.stale()
        self.assertEqual((code, out), (1, []))
        self.assertIn("x: row 1: header does not match POLICY.csv", err)

    def test_output_has_no_row_text(self):
        self.write([law_row(r, ago(400) if i % 2 else "2027-01-01") for i, r in enumerate(RULES)])
        code, out, err = self.stale()
        self.assertEqual((code, err), (3, ""))
        text = "\n".join(out)
        for leak in (DUTY, URL, SHA, ADDER, "Sec", str(self.store), "LAW.csv"):
            self.assertNotIn(leak, text)

    def test_help_lists_stale(self):
        code, out, _ = run("--help")
        self.assertEqual(code, 0)
        self.assertIn("stale STORE --max-age-days N [--today YYYY-MM-DD]", out)


if __name__ == "__main__":
    unittest.main()
