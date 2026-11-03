"""RF0250-9: flock_state.py state STORE --agency SLUG (repeatable) states only the chosen agencies; an unknown slug is refused
before any folder is read, a malformed or repeated one is usage, and another agency's broken file no longer hides them."""
import contextlib
import io
import json
import unittest
from unittest import mock

from tests.flock import test_state as base
from tests.flock.test_state import HEAD, HEADER, MOD, row

A, B, C = "alpha-town", "bravo-town", "charlie-town"
NAMES = {A: "Alpha Town", B: "Bravo Town", C: "Charlie Town"}


class StateAgencyTests(unittest.TestCase):
    # test_state's store builders and state() helper, borrowed (not inherited) so its own tests are not collected twice
    setUp, requests, state, snap = base.StateTests.setUp, base.StateTests.requests, base.StateTests.state, base.StateTests.snap

    def build(self, broken_b=True):
        self.requests(row("R-A2", agency=NAMES[A]), row("R-A1", "closed", agency=NAMES[A]), slug=A)
        self.requests(text="a,b\n1,2\n" if broken_b else HEADER + row("R-B1", agency=NAMES[B]), slug=B)
        self.requests(row("R-C1", "withdrawn", agency=NAMES[C]), slug=C)
        run = self.store / "agencies" / C / "runs" / "2026-10-02"
        run.mkdir(parents=True)
        (run / "FOLLOWUP-DRAFT.md").write_text(HEAD + "\n", encoding="utf-8")

    def loads(self, *extra):
        calls = []
        real = MOD.requests_tool.load
        with mock.patch.object(MOD.requests_tool, "load", lambda f, folder=None: calls.append(folder) or real(f, folder)):
            got = self.state(*extra)
        return got, calls

    def test_broken_other_agency_no_longer_hides_the_chosen_one(self):
        self.build()
        self.assertEqual(self.state(), (1, "", "state: REQUESTS.csv unreadable or not v1 (bravo-town)\n"))
        code, out, err = self.state("--agency", A)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual([ln.split(" | ")[2:5] for ln in out.splitlines()[:-1]],
                         [[A, "R-A1", "state=closed"], [A, "R-A2", "state=open-receipt-unknown"]])
        self.assertTrue(out.splitlines()[-1].startswith("state | requests=2 runs=0 agencies=1 open-receipt-unknown=1 "), out)
        self.assertIn(" closed=1 ", out.splitlines()[-1])
        self.assertEqual(self.state("--agency", B), self.state())  # naming the broken agency gives the full run's refusal
        self.assertEqual(self.state("--agency", A, "--agency", B)[:2], (1, ""))

    def test_two_agencies_sorted_as_the_full_run_and_totals_summed(self):
        self.build(broken_b=False)
        full = self.state()[1].splitlines()
        code, out, _ = self.state("--agency", C, "--agency", A)  # argv order does not change the output order
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[:-1], [ln for ln in full[:-1] if f" | {B} | " not in ln])
        self.assertEqual(out.splitlines()[:-1], self.state("--agency", A, "--agency", C)[1].splitlines()[:-1])
        tot = out.splitlines()[-1]
        self.assertTrue(tot.startswith("state | requests=3 runs=1 agencies=2 open-receipt-unknown=1 "), tot)
        for part in (" closed=1 ", " withdrawn=1 ", " follow-up-drafted=1 "):
            self.assertIn(part, tot)
        one = [json.loads(self.state("--json", "--agency", s)[1])["totals"] for s in (A, C)]
        both = json.loads(self.state("--agency", A, "--json", "--agency", C)[1])
        self.assertEqual(both["totals"], {k: one[0][k] + one[1][k] for k in one[0]})
        self.assertEqual((both["errors"], both["agency_filter"]), (0, [A, C]))

    def test_unknown_slug_is_refused_before_any_folder_is_read(self):
        self.build()
        for extra in (("--agency", "zulu-town"), ("--agency", A, "--agency", "zulu-town"), ("--json", "--agency", "zulu-town")):
            with self.subTest(extra=extra):
                (code, out, err), calls = self.loads(*extra)
                self.assertEqual((code, out, err, calls), (1, "", "state: unknown agency (zulu-town)\n", []))
        (self.store / "agencies" / "file-town").write_text("not a folder", encoding="utf-8")
        self.assertEqual(self.loads("--agency", "file-town"), ((1, "", "state: unknown agency (file-town)\n"), []))
        with mock.patch.object(MOD, "REPO", self.tmp):  # store checks still come first
            self.assertEqual(self.state("--agency", "zulu-town"), (1, "", "state: store is inside the repository\n"))

    def test_malformed_repeated_or_extra_words_are_usage_and_nothing_is_read(self):
        self.build()
        bad = [["--agency", s] for s in ("../" + A, "a b", "x" * 41, "late-agency", "Overdue", "-x", "", ".hidden", "a/b")]
        bad += [["--agency", A, "--agency", A], ["--agency"], [A, "--agency"], ["--json", "--json", "--agency", A],
                ["--agency", A, "--text"], ["--agency", "--json"], [s for i in range(21) for s in ("--agency", f"t{i}")]]
        for extra in bad:
            with self.subTest(extra=extra):
                (code, out, err), calls = self.loads(*extra)
                self.assertEqual((code, out, calls), (2, "", []))
                self.assertTrue(err.startswith("usage: flock_state.py state STORE [--json] [--agency SLUG]..."), err)
        for argv in ([], ["state"], ["state", "a", "b"], ["state", "a", "--json", "x"], ["status", "a"], ["state", "a", "--text"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(MOD.main(argv), 2)
        self.assertEqual(self.state("--agency", "x" * 40)[2], "state: unknown agency (%s)\n" % ("x" * 40))  # 40 is the limit
        twenty = [s for i in range(20) for s in ("--agency", f"t{i}")]
        self.assertEqual(self.state(*twenty), (1, "", "state: unknown agency (t0)\n"))  # 20 is accepted, then checked

    def test_named_symlinked_folder_gives_link_refused_and_is_never_entered(self):
        self.build()
        outside = self.tmp / "outside"
        (outside / "runs" / "2026-10-02").mkdir(parents=True)
        (outside / "REQUESTS.csv").write_text(HEADER + row("R-OUT", agency="Linked Town"), encoding="utf-8")
        (outside / "runs" / "2026-10-02" / "FOLLOWUP-DRAFT.md").write_text(HEAD + "\n", encoding="utf-8")
        (self.store / "agencies" / "linked-town").symlink_to(outside, target_is_directory=True)
        (code, out, err), calls = self.loads("--agency", "linked-town")
        self.assertEqual((code, err, calls), (1, "", []))
        self.assertEqual(out.splitlines()[0], "state | run | linked-town | - | state=link-refused | basis=email-stage")
        self.assertTrue(out.splitlines()[-1].startswith("state | requests=0 runs=1 agencies=1 "), out)
        self.assertNotIn("R-OUT", out)
        self.assertNotIn("follow-up-drafted=1", out)

    def test_json_filter_key_only_with_the_flag(self):
        self.build(broken_b=False)
        raw = self.state("--json")[1]
        full = json.loads(raw)
        self.assertEqual(list(full), ["schema", "review_required", "requests", "runs", "totals", "errors"])  # no agency_filter
        self.assertEqual(raw, json.dumps(full, ensure_ascii=True) + "\n")
        code, raw_a, _ = self.state("--agency", A, "--json")
        data = json.loads(raw_a)
        self.assertEqual((code, data["agency_filter"], data["schema"], data["review_required"]), (0, [A], "flock-state-v1", True))
        self.assertEqual(data["requests"], [r for r in full["requests"] if r["agency"] == A])
        self.assertEqual(data["runs"], [r for r in full["runs"] if r["agency"] == A])
        self.assertEqual(json.loads(self.state("--json", "--agency", C)[1])["runs"], full["runs"])
        self.assertNotIn("agencies=", self.state()[1])

    def test_nothing_is_written_with_agency(self):
        self.build()
        before = self.snap()
        for extra in (("--agency", A), ("--agency", B, "--json"), ("--agency", "zulu-town"), ("--agency", "../x")):
            self.state(*extra)
            self.state(*extra, verified=False)
        self.assertEqual(self.snap(), before)

if __name__ == "__main__":
    unittest.main()
