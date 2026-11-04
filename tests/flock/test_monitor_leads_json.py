"""RF1240-12: `flock_monitor.py leads ... --json` (flock-monitor-leads-v1). Synthetic LEADS.csv only, no network."""
import contextlib
import csv
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "flock_monitor.py"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


monitor = load("_test_monitor_leads_json", TOOL)
_builders = load("_test_monitor_leads_json_builders", ROOT / "tests" / "flock" / "test_monitor_leads_list.py")
TODAY, FIVE, LINE = _builders.TODAY, _builders.FIVE, _builders.LINE
A, B, C, D, E = (row[0] for row in FIVE)
# Text stdout of origin/main (88edf506) captured on FIVE at --date 2026-11-02, byte for byte.
GOLDEN = {(): "\n".join([LINE[B], LINE[C], LINE[E], LINE[A], LINE[D], "leads=5 shown=5 new=3 ignored=1 requested=1"]),
          ("--state", "requested"): LINE[B] + "\nleads=5 shown=1 new=0 ignored=0 requested=1",
          ("--agency", "sample-city"): "\n".join([LINE[C], LINE[E], LINE[A], "leads=5 shown=3 new=2 ignored=1 requested=0"]),
          ("--state", "new", "--agency", "sample-city"):
              "\n".join([LINE[E], LINE[A], "leads=5 shown=2 new=2 ignored=0 requested=0"])}
ZERO = {"leads": 0, "shown": 0, "new": 0, "ignored": 0, "requested": 0}


class LeadsJsonTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Path(tmp.name) / "store"
        self.store.mkdir(mode=0o700)
        self.leads = self.store / "LEADS.csv"

    def write(self, rows):
        with open(self.leads, "w", encoding="utf-8", newline="") as out:
            csv.writer(out, lineterminator="\n").writerows([monitor.LEADS_HEADER] + rows)

    def run_main(self, *args, store=None, date=TODAY):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            try:
                code = monitor.main(["leads", "--store", str(store or self.store), "--date", date, *args])
            except SystemExit as exit_:
                code = exit_.code
        return code, out.getvalue()

    def doc(self, *args, **kw):
        code, out = self.run_main(*args, "--json", **kw)
        return code, json.loads(out)

    def text_rows(self, *args):  # (item_id, agency, state, first_seen, age_days) and counts parsed from the text form
        code, out = self.run_main(*args)
        lines = out.splitlines()
        rows = [tuple(p.split("=")[-1] for p in line.split(" | ")[1:]) for line in lines[:-1]]
        rows = [(i, a, s, f, int(age[:-1])) for i, a, s, f, age in rows]
        return code, rows, {k: int(v) for k, v in (p.split("=") for p in lines[-1].split())}

    def test_five_leads_in_text_order_with_age_and_counts(self):
        self.write(FIVE)
        code, doc = self.doc()
        tcode, rows, counts = self.text_rows()
        self.assertEqual((code, tcode), (0, 0))
        self.assertEqual([(r["item_id"], r["agency"], r["state"], r["first_seen"], r["age_days"]) for r in doc["leads"]],
                         rows)
        self.assertEqual([r["item_id"] for r in doc["leads"]], [B, C, E, A, D])
        self.assertEqual(doc["counts"], counts)
        self.assertEqual(doc["counts"], {"leads": 5, "shown": 5, "new": 3, "ignored": 1, "requested": 1})
        self.assertEqual({k: doc[k] for k in ("schema", "review_required", "date", "filters", "errors")},
                         {"schema": "flock-monitor-leads-v1", "review_required": True, "date": TODAY,
                          "filters": {"state": None, "agency": None}, "errors": []})
        self.assertEqual(sorted(doc), ["counts", "date", "errors", "filters", "leads", "review_required", "schema"])
        self.assertTrue(all(sorted(r) == ["age_days", "agency", "first_seen", "item_id", "state"] for r in doc["leads"]))

    def test_filters_give_the_text_set_and_are_echoed(self):
        self.write(FIVE)
        for args, state, agency in ((("--state", "requested"), "requested", None),
                                    (("--agency", "sample-city"), None, "sample-city"),
                                    (("--state", "new", "--agency", "sample-city"), "new", "sample-city"),
                                    (("--state", "requested", "--agency", "sample-city"), "requested", "sample-city")):
            with self.subTest(args=args):
                code, doc = self.doc(*args)
                tcode, rows, counts = self.text_rows(*args)
                self.assertEqual((code, tcode), (0, 0))
                self.assertEqual([tuple(r[k] for k in ("item_id", "agency", "state", "first_seen", "age_days"))
                                  for r in doc["leads"]], rows)
                self.assertEqual((doc["counts"], doc["counts"]["leads"]), (counts, 5))
                self.assertEqual(doc["filters"], {"state": state, "agency": agency})
                self.assertTrue(all(r["state"] == (state or r["state"]) and r["agency"] == (agency or r["agency"])
                                    for r in doc["leads"]))

    def test_missing_leads_csv_is_empty_with_zero_counts_and_creates_nothing(self):
        code, doc = self.doc()
        self.assertEqual((code, doc["leads"], doc["counts"], doc["errors"]), (0, [], ZERO, []))
        self.assertEqual(sorted(p.name for p in self.store.iterdir()), [])
        absent = self.store / "absent"
        code, doc = self.doc(store=absent)
        self.assertEqual((code, doc["leads"], doc["counts"], doc["errors"]), (0, [], ZERO, []))
        self.assertFalse(absent.exists())

    def assert_refused(self, *args, **kw):
        tcode, text = self.run_main(*args, **kw)
        code, doc = self.doc(*args, **kw)
        self.assertEqual((tcode, code), (1, 1))
        self.assertEqual((doc["errors"], doc["leads"], doc["counts"]), (text.splitlines(), [], None))
        self.assertEqual((doc["schema"], doc["review_required"]), ("flock-monitor-leads-v1", True))
        return doc["errors"]

    def test_bad_rows_and_linked_file_are_one_error_each_equal_to_text(self):
        late = [list(r) for r in FIVE]
        late[3][2] = "2026-11-03"
        bad_id = [list(r) for r in FIVE]
        bad_id[1][0] = "M-XYZ"
        for rows, line in ((late, "LEADS.csv row 5: first_seen is invalid"), (bad_id, "LEADS.csv row 3: item_id is invalid")):
            with self.subTest(line):
                self.write(rows)
                self.assertEqual(self.assert_refused("--state", "new"), [line])  # a row outside the filter still refuses
        self.write(FIVE)
        os.replace(self.leads, self.store / "real.csv")
        os.symlink(self.store / "real.csv", self.leads)
        self.assertEqual(self.assert_refused(), ["LEADS.csv is a symlink"])

    def test_other_refusals_are_errors_exit_1(self):
        self.assertEqual(self.assert_refused(store=ROOT / "tests" / "flock"), ["store inside repository"])
        self.write(FIVE)
        self.assertEqual(self.assert_refused(date="2026-10-29"), ["LEADS.csv row 2: first_seen is invalid"])
        self.leads.write_bytes(b"\xff\xfe not utf-8\n")
        self.assertEqual(self.assert_refused(), ["io error: UnicodeDecodeError"])
        with mock.patch.object(monitor, "leads_rows", side_effect=PermissionError("denied")):
            code, doc = self.doc()
        self.assertEqual((code, doc["errors"], doc["leads"], doc["counts"]), (1, ["io error: PermissionError"], [], None))

    def test_usage_errors_stay_exit_2_with_no_json(self):
        self.write(FIVE)
        for args in (("--state", "done"), ("--state", "NEW"), ("--agency", "Sample City"), ("--agency", "../x")):
            with self.subTest(args=args):
                self.assertEqual(self.run_main(*args, "--json"), (2, ""))
        store = str(self.store)
        for argv in (["digest", "--store", store, "--date", TODAY, "--dry-run", "--json"],
                     ["lead", "--store", store, "--item", A, "--state", "requested", "--by", "x", "--json"]):
            with self.subTest(argv=argv[0]), contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit) as caught:
                monitor.main(argv)  # --json is a leads (and check) option only
            self.assertEqual(caught.exception.code, 2)
        self.assertEqual(self.leads.read_bytes().count(b"\n"), 6)

    def test_canary_title_url_and_state_by_never_printed(self):
        rows = [list(r) for r in FIVE]
        rows[0][5], rows[0][4] = "CANARY-by Organizer", "2026-10-31"
        self.write(rows)
        with open(self.store / "MONITOR.csv", "w", encoding="utf-8", newline="") as out:
            csv.writer(out, lineterminator="\n").writerows([monitor.MONITOR_HEADER, [
                A, "Sample City", "agenda", "https://www.city.example/CANARY-url.pdf", "CANARY-title Flock contract",
                "0" * 64, "2026-10-30", "public", "unreviewed"]])
        code, out = self.run_main("--json")
        self.assertEqual(code, 0)
        for absent in ("CANARY", "Organizer", "Quietname", "https://", "city.example", "2026-10-31", "2026-10-21",
                       "Sample City", "Zed Town", "state_by", "state_at", "title", "url"):
            self.assertNotIn(absent, out)

    def test_json_takes_no_lock_and_writes_nothing(self):
        self.write(FIVE)
        before = self.leads.read_bytes()
        with mock.patch.object(monitor, "store_lock", side_effect=AssertionError("lock taken")), \
                mock.patch.object(monitor, "write_atomic", side_effect=AssertionError("write")):
            self.assertEqual(self.doc()[0], 0)
        self.assertEqual((sorted(p.name for p in self.store.iterdir()), self.leads.read_bytes()), (["LEADS.csv"], before))

    def test_text_stdout_is_golden_and_json_stdout_is_one_document(self):
        self.write(FIVE)
        run = lambda *args: subprocess.run([sys.executable, "-B", str(TOOL), "leads", "--store", str(self.store),
                                            "--date", TODAY, *args], capture_output=True, timeout=60)
        for args, golden in GOLDEN.items():
            with self.subTest(args=args):
                text, js = run(*args), run(*args, "--json")
                self.assertEqual((text.returncode, text.stdout), (0, (golden + "\n").encode()))
                self.assertEqual(js.returncode, 0)
                body = js.stdout.decode("utf-8")
                doc, end = json.JSONDecoder().raw_decode(body)
                self.assertEqual((body[end:], doc["schema"]), ("\n", "flock-monitor-leads-v1"))
                self.assertEqual(body, json.dumps(doc, sort_keys=True, indent=1, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    unittest.main()
