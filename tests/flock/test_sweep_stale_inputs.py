"""RF1239-1: flock_sweep names a latest run whose recorded inputs changed since scaffold `stale-inputs` (next: a new dated
run), never `compare-open` or `finished`, and never a matrix cell. Temp store outside the repo; synthetic fixtures only."""
import contextlib, csv, datetime, importlib.util, io, json, shlex, unittest
from unittest import mock
from tests.flock import test_compare_inputs as inputs_test, test_sweep as base

AGENCY, TOOLS, CI = base.AGENCY, base.TOOLS, inputs_test.CompareInputsTest
_spec = importlib.util.spec_from_file_location("_rf1239_sweep", TOOLS / "flock_sweep.py")
sweep = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sweep)
RESUME, SAME = "resume compare card (REMAINING.md)", " (pick a later --date; the folder exists)"
SUMMARY = "agencies=5 finished=%d ready=%d blocked=%d unmapped=1 unwatched=0"
LATER = "2026-11-02"


class SweepStaleInputs(unittest.TestCase):
    sweep = base.FlockSweepTest.sweep
    finish = base.FlockSweepTest.finish
    edit_facts, edit_law, edit_policy, facts_import = CI.edit_facts, CI.edit_law, CI.edit_policy, CI.facts_import

    def setUp(self):
        base.FlockSweepTest.setUp(self)
        self.agency = self.store / "agencies" / AGENCY
        self.paths = {"FACTS.csv": self.agency / "FACTS.csv", "LAW.csv": self.store / "LAW.csv",
                      "POLICY.csv": self.agency / "POLICY.csv"}

    def status(self, *args):
        code, out = self.sweep("status", *args)
        self.assertEqual(code, 0, out)
        lines = out.splitlines()
        return {r["slug"] or r["watch_agency"]: r for r in csv.DictReader(lines[:-1])}[AGENCY], lines[-1], out

    def scaffold(self, date):  # the ready-compare command flock_sweep builds for this agency
        return ("python3 %s scaffold --store %s --agency %s --date %s --provider <author family> --model <model> "
                "--applicability \"Sample city police; local ALPR policy applies\"; compare card"
                % (shlex.quote(str(TOOLS / "flock_compare.py")), shlex.quote(str(self.store.resolve())), AGENCY, date))

    def meta(self, inputs):
        path = self.run_dir / "RUN.json"
        meta = json.loads(path.read_text(encoding="utf-8"))
        if inputs is None:
            del meta["inputs"]
        else:
            meta["inputs"] = inputs(meta["inputs"])
        path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")

    def test_fresh_run_unchanged_is_compare_open(self):  # 1
        self.assertIn("inputs", json.loads((self.run_dir / "RUN.json").read_text()))
        row, last, _ = self.status()
        self.assertEqual((row["stage"], row["next"], last), ("compare-open", RESUME, SUMMARY % (0, 0, 4)))
        base.shutil.copy(base.FIXTURES / "findings-sample.csv", self.run_dir / "FINDINGS.csv")
        self.assertEqual(self.status()[0]["stage"], "ready-verify")

    def test_facts_changed(self):  # 2: explicit --date and the default (today)
        self.edit_facts()
        row, last, _ = self.status("--date", LATER)
        self.assertEqual((row["stage"], last), ("stale-inputs", SUMMARY % (0, 0, 4)))
        self.assertEqual(row["next"], "inputs changed since 2026-10-30 (FACTS.csv); " + self.scaffold(LATER))
        today = datetime.date.today().isoformat()
        self.assertEqual(self.status()[0]["next"], "inputs changed since 2026-10-30 (FACTS.csv); " + self.scaffold(today))

    def test_two_inputs_sorted_one_row(self):  # 3: POLICY edited first; names still sorted
        self.edit_policy(), self.edit_law()
        row, _, out = self.status("--date", LATER)
        self.assertEqual((row["stage"], out.count(AGENCY + ","), out.count("stale-inputs")), ("stale-inputs", 1, 1))
        self.assertTrue(row["next"].startswith("inputs changed since 2026-10-30 (LAW.csv,POLICY.csv); python3 "), row["next"])

    def test_changed_after_verify_not_finished(self):  # 4
        self.finish()
        row, last, _ = self.status()
        self.assertEqual((row["stage"], last), ("finished", SUMMARY % (1, 0, 3)))
        self.edit_policy()
        row, last, _ = self.status("--date", LATER)
        self.assertEqual((row["stage"], last), ("stale-inputs", SUMMARY % (0, 0, 4)))
        self.assertEqual(row["next"], "inputs changed since 2026-10-30 (POLICY.csv); " + self.scaffold(LATER))
        self.assertEqual((row["public_ok"], row["questions"]), ("4", "2"))  # the verified files are still there
        out = self.tmp / "m"
        self.assertEqual(self.sweep("matrix", "--out", str(out)), (0, "matrix rows=0 agencies=0\n"))
        self.assertEqual((out / "COMPARE-MATRIX.csv").read_text(), "topic,claim_class\n")
        self.assertEqual((out / "COMPARE.md").read_text().splitlines()[-1],
                         "- not finished: %s;second-city-police;campus-police;parks" % AGENCY)

    def test_same_date_flagged(self):  # 5
        self.edit_law()
        row = self.status("--date", "2026-10-30")[0]
        self.assertEqual(row["next"], "inputs changed since 2026-10-30 (LAW.csv); " + self.scaffold("2026-10-30") + SAME)
        self.assertFalse(self.status("--date", LATER)[0]["next"].endswith(SAME))

    def test_legacy_run_output_unchanged(self):  # 6: no inputs record -> the bytes a run with unchanged inputs gives
        fresh = self.sweep("status", "--date", LATER)
        self.meta(None)
        self.assertEqual(self.sweep("status", "--date", LATER), fresh)
        self.edit_law()  # a legacy run has nothing to compare against
        self.assertEqual(self.sweep("status", "--date", LATER), fresh)
        base.shutil.copy(base.FIXTURES / "findings-sample.csv", self.run_dir / "FINDINGS.csv")
        self.assertEqual(self.status()[0]["stage"], "ready-verify")

    def test_malformed_record_stays_compare_open(self):  # 7: even when an input also changed
        for label, inputs in (("list", lambda good: list(good.values())), ("null", lambda good: None),
                              ("short", lambda good: dict(good, **{"LAW.csv": "abc"}))):
            with self.subTest(label):
                self.setUp()
                self.meta(inputs)
                self.edit_facts()
                row = self.status("--date", LATER)[0]
                self.assertEqual((row["stage"], row["next"]), ("compare-open", RESUME))

    def test_remaining_and_changed(self):  # 8: stale-inputs wins; without a change REMAINING.md stays compare-open
        base.shutil.copy(base.FIXTURES / "findings-sample.csv", self.run_dir / "FINDINGS.csv")
        (self.run_dir / "REMAINING.md").write_text("- doc 3\n")
        self.assertEqual(self.status()[0]["stage"], "compare-open")
        self.edit_facts()
        self.assertEqual(self.status("--date", LATER)[0]["stage"], "stale-inputs")

    def in_process(self, *argv, roster=base.ROSTER, assess=None):
        calls, real = {}, sweep.compare.check
        def counting(run, *a, **kw):
            calls[str(run)] = calls.get(str(run), 0) + 1
            return real(run, *a, **kw)
        buf, patch = io.StringIO(), mock.patch.object(sweep, "assess", assess) if assess else contextlib.nullcontext()
        with mock.patch.object(sweep.compare, "check", counting), patch, contextlib.redirect_stdout(buf):
            code = sweep.main([*argv, "--watchlist", base.WATCH, "--roster", roster, "--store", str(self.store)])
        return code, buf.getvalue(), calls

    def test_one_check_per_agency(self):  # 9
        cases = (("compare-open", lambda: None), ("stale-inputs", self.edit_policy),
                 ("stale-inputs", lambda: (self.run_dir / "REMAINING.md").write_text("- doc 3\n")))
        for stage, step in cases:
            with self.subTest(stage):
                step()
                code, out, calls = self.in_process("status")
                self.assertEqual((code, f",{stage}," in out, list(calls.values())), (0, True, [1]), out)
        self.setUp()
        self.finish(), self.edit_facts()  # a verified run that went stale stops before verify re-checks it
        code, out, calls = self.in_process("status")
        self.assertEqual((",stale-inputs," in out, list(calls.values())), (True, [1]), out)

    def test_morro_bay_gate(self):  # 10: the gate row's next is the re-scaffold step; no [RUN] block
        roster = self.tmp / "r.csv"
        roster.write_text((base.FIXTURES / "roster-sample.csv").read_text()
                          + "Golden Town,morro-bay,yes,Sample city police; local ALPR policy applies,\n")
        real = sweep.assess
        def assess(store, slug, *rest):  # the morro-bay roster row is assessed against the sample agency's store
            return dict(real(store, AGENCY if slug == "morro-bay" else slug, *rest), slug=slug)
        self.edit_facts()
        base.shutil.copy(base.FIXTURES / "findings-sample.csv", self.run_dir / "FINDINGS.csv")
        code, out, _ = self.in_process("plan", "--date", LATER, roster=str(roster), assess=assess)
        self.assertEqual(code, 0, out)
        self.assertEqual(out.splitlines(), ["CARDS", "- morro-bay stale-inputs: inputs changed since 2026-10-30 "
                                            "(FACTS.csv); " + self.scaffold(LATER), base.GATE])
        self.assertNotIn("## [RUN]", out)


if __name__ == "__main__":
    unittest.main()
