"""flock_requests.py summary PATH --as-of D: oldest_open_days per agency and in totals; sent_after_as_of counted, never
aged; calendar days since the recorded sent date, not a legal status (RF0620-12)."""
import json
import tempfile
import unittest
from pathlib import Path

from tests.flock.test_requests_inventory import GOLDEN, INV_HEADER, SHA, doc
from tests.flock.test_requests_summary import BANNED, FIXTURE, HEADER, TAIL, row, run


class RequestsSummaryAsOfTests(unittest.TestCase):
    def write(self, name, *lines):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        path = Path(t.name) / name
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return str(path)

    def ok(self, *args):
        done = run("summary", *args)
        self.assertEqual((done.returncode, done.stderr), (0, ""), done.stderr)
        return done.stdout

    def both(self, path, *args):
        out = self.ok(path, *args)
        return out.splitlines(), json.loads(self.ok(path, *args, "--json"))

    def store(self):
        return self.write("REQUESTS.csv", HEADER, row("1", "Alpha Police", "2026-10-01"),
                          row("2", "Alpha Police", "2026-09-10", receipt=True),
                          row("3", "Alpha Police", "2026-08-01", "closed"),
                          row("4", "Beta Sheriff", "2026-11-01"), row("5", "Beta Sheriff", "2026-10-20", "produced"),
                          row("6", "Gamma Office", "2026-11-05"), row("7", "Delta Clerk", "2026-01-02", "withdrawn"))

    def test_oldest_open_days_per_agency_totals_max_and_sent_after_counted(self):
        lines, data = self.both(self.store(), "--as-of", "2026-11-01")
        days = {a["slug"]: a["oldest_open_days"] for a in data["agencies"]}
        # 2026-09-10 is the oldest open (the closed 2026-08-01 is not aged); same-day sent = 0; only-after = null
        self.assertEqual(days, {"alpha-police": 52, "beta-sheriff": 0, "delta-clerk": None, "gamma-office": None})
        self.assertEqual({k: data["totals"][k] for k in ("oldest_open_days", "sent_after_as_of", "as_of")},
                         {"oldest_open_days": 52, "sent_after_as_of": 1, "as_of": "2026-11-01"})
        self.assertEqual([ln.rsplit(" | ", 1)[1] for ln in lines[:-1]],
                         ["oldest_open_days 52", "oldest_open_days 0", "oldest_open_days -", "oldest_open_days -"])
        self.assertTrue(lines[0].endswith("| oldest_open_sent 2026-09-10 | oldest_open_days 52"))
        self.assertEqual(lines[-1], "request status | agencies=4 requests=7 open=4 receipt_unknown=3"
                                    " oldest_open_days=52 sent_after_as_of=1 as_of=2026-11-01" + TAIL)
        self.assertTrue(all(v >= 0 for v in days.values() if v is not None))  # never negative
        self.assertFalse(BANNED.search("\n".join(lines)))

    def test_totals_null_when_no_agency_has_an_age(self):
        path = self.write("REQUESTS.csv", HEADER, row("1", "Alpha Police", "2026-12-01"),
                          row("2", "Alpha Police", "2026-12-02"), row("3", "Beta Sheriff", "2026-01-02", "closed"))
        lines, data = self.both(path, "--as-of", "2026-11-01")
        self.assertEqual([a["oldest_open_days"] for a in data["agencies"]], [None, None])
        self.assertEqual((data["totals"]["oldest_open_days"], data["totals"]["sent_after_as_of"]), (None, 2))
        self.assertIn(" oldest_open_days=- sent_after_as_of=2 as_of=2026-11-01 |", lines[-1])
        self.assertTrue(all(ln.endswith(" | oldest_open_days -") for ln in lines[:-1]))

    def test_open_mix_before_and_after_only_before_is_aged(self):
        path = self.write("REQUESTS.csv", HEADER, row("1", "Alpha Police", "2026-11-03"),
                          row("2", "Alpha Police", "2026-10-30"))
        _, data = self.both(path, "--as-of", "2026-11-01")
        self.assertEqual((data["agencies"][0]["oldest_open_days"], data["totals"]["sent_after_as_of"]), (2, 1))
        self.assertEqual(data["agencies"][0]["oldest_open_sent"], "2026-10-30")

    def test_invalid_repeated_or_missing_value_exit_2_stdout_empty(self):
        path = self.store()
        msg = "summary: --as-of must be YYYY-MM-DD\n"
        for args, err in ((["--as-of", "2026-02-30"], msg), (["--as-of", "2026-1-01"], msg), (["--as-of", ""], msg),
                          (["--json", "--as-of", "yesterday"], msg),
                          (["--as-of", "2026-11-01", "--as-of", "2026-11-01"], None), (["--as-of"], None),
                          (["--as-of", "--json"], None), (["--json", "--as-of"], None),
                          (["--as-of", "2026-11-01", "--today", "2026-11-01"], None)):
            done = run("summary", path, *args)
            self.assertEqual((done.returncode, done.stdout), (2, ""), args)
            if err:
                self.assertEqual(done.stderr, err, args)
            else:
                self.assertTrue(done.stderr.startswith("usage: "), args)

    def test_with_inventory_and_json_any_order_identical_doc_counts_unchanged(self):
        inv = self.write("INVENTORY.csv", INV_HEADER, doc("1", "1", SHA["a"]), doc("1", "2", SHA["b"], "yes"),
                         doc("4", "3", SHA["c"]))
        path, d = self.store(), ["--as-of", "2026-11-01"]
        outs = {self.ok(path, *a) for a in ([*d, "--inventory", inv, "--json"], ["--json", "--inventory", inv, *d],
                                            ["--inventory", inv, "--json", *d], [d[0], d[1], "--json", "--inventory", inv])}
        self.assertEqual(len(outs), 1)
        data, plain = json.loads(outs.pop()), json.loads(self.ok(path, "--inventory", inv, "--json"))
        for a, b in zip(data["agencies"], plain["agencies"]):
            self.assertEqual(a.pop("requests"), b.pop("requests"))
            self.assertEqual(set(a) - set(b), {"oldest_open_days"})
        self.assertEqual({k: data["totals"][k] for k in plain["totals"]}, plain["totals"])
        text = self.ok(path, "--inventory", inv, *d).splitlines()
        self.assertIn("request docs | Alpha Police | 1 | status=open | posted=1 withheld=1", text)
        self.assertTrue(text[-1].endswith("unknown_rows=0 oldest_open_days=52 sent_after_as_of=1 as_of=2026-11-01" + TAIL))

    def test_without_as_of_text_and_json_byte_identical_and_no_new_keys(self):
        self.assertEqual(self.ok(str(FIXTURE)), GOLDEN)  # RF0620-11 text, byte for byte
        for path in (str(FIXTURE), self.store()):
            text, data = self.ok(path), json.loads(self.ok(path, "--json"))
            self.assertNotIn("oldest_open_days", text + json.dumps(data))
            self.assertNotIn("sent_after_as_of", json.dumps(data))
            aged = self.ok(path, "--as-of", "2026-11-01").splitlines()
            stripped = [ln.replace(ln[ln.find(" | oldest_open_days"):], "") if " | oldest_open_days" in ln else ln
                        for ln in aged[:-1]]
            self.assertEqual(stripped, text.splitlines()[:-1])
            self.assertEqual(self.ok(path), text)  # stable across runs


if __name__ == "__main__":
    unittest.main()
