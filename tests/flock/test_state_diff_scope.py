"""RF0511-1: flock_state.py diff accepts `state --json --agency` snapshots and refuses incompatible comparison scopes."""
import contextlib
import io
import json
import os
import unittest

from tests.flock import test_state as base
from tests.flock.test_state import MOD, SLUG, row

OTHER = "other-town"
REFUSED = "diff: snapshot agency filters differ (OLD/NEW)\n"
SET = {"setUp": base.StateTests.setUp, "requests": base.StateTests.requests, "email": base.StateTests.email,
       "state": base.StateTests.state, "snap": base.StateTests.snap}


class ScopeTests(unittest.TestCase):
    locals().update(SET)  # borrowed, not inherited, so test_state's own tests are not collected twice

    def build(self, state="open", other="open"):
        self.requests(row("R-1", state))
        self.requests(row("R-2", other, agency="Other Town"), slug=OTHER)

    def save(self, name, *agencies, raw=None):
        extra = [w for a in agencies for w in ("--agency", a)]
        code, out, err = self.state("--json", *extra)
        self.assertEqual(err, "")
        path = self.tmp / name
        path.write_text(out if raw is None else raw, encoding="utf-8")
        return path

    def edit(self, name, fn, src):
        data = json.loads(self.save(src, SLUG).read_text(encoding="utf-8"))
        fn(data)
        path = self.tmp / name
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def diff(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = MOD.main(["diff", *map(str, argv)])
        return code, out.getvalue(), err.getvalue()

    def test_real_filtered_snapshot_round_trip_text_and_json(self):
        self.build()
        self.email("2026-10-02", field=False)
        old = self.save("old.json", SLUG)
        self.assertIn("agency_filter", old.read_text(encoding="utf-8"))
        self.assertEqual(self.diff(old, self.save("same.json", SLUG))[::2], (0, ""))
        self.requests(row("R-1", "closed"))
        self.email("2026-10-02")
        new = self.save("new.json", SLUG)
        code, out, err = self.diff(old, new)
        self.assertEqual((code, err), (3, ""))
        self.assertEqual(out.splitlines()[:2], [f"diff | request | {SLUG} | R-1 | open-receipt-unknown -> closed",
                                                f"diff | run | {SLUG} | 2026-10-02 | email-drafted -> approved-not-sent"])
        jcode, raw, _ = self.diff(old, new, "--json")
        data = json.loads(raw)
        self.assertEqual((jcode, data["schema"], len(data["changes"])), (3, "flock-state-diff-v1", 2))

    def test_equal_selection_is_order_independent_and_empty_results_valid(self):
        self.build()
        a, b = self.save("a.json", SLUG, OTHER), self.save("b.json", OTHER, SLUG)
        data = json.loads(b.read_text(encoding="utf-8"))
        data["agency_filter"].reverse()
        b.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(self.diff(a, b)[::2], (0, ""))
        empty = self.edit("empty.json", lambda d: d.update(requests=[], runs=[], agency_filter=[SLUG]), "e0.json")
        self.assertEqual(self.diff(empty, empty)[::2], (0, ""))
        run_only = self.edit("runs.json", lambda d: d.update(requests=[], runs=[{"agency": SLUG, "run": "2026-10-02", "state": "email-drafted"}]), "r0.json")
        self.assertEqual(self.diff(run_only, run_only)[::2], (0, ""))

    def test_incompatible_scopes_refused_before_any_diff_output(self):
        self.build()
        full, a, b, ab = (self.save("full.json"), self.save("a.json", SLUG), self.save("b.json", OTHER), self.save("ab.json", SLUG, OTHER))
        empty = self.edit("empty.json", lambda d: d.update(requests=[], runs=[], agency_filter=[SLUG]), "e0.json")
        empty_full = self.edit("ef.json", lambda d: (d.update(requests=[], runs=[]), d.pop("agency_filter")), "e1.json")
        for old, new in ((full, a), (a, full), (a, b), (a, ab), (ab, a), (empty, empty_full), (empty_full, empty)):
            for extra in ((), ("--json",)):
                with self.subTest(old=old.name, new=new.name, extra=extra):
                    self.assertEqual(self.diff(old, new, *extra), (1, "", REFUSED))

    def test_invalid_scope_metadata_names_only_field_and_side(self):
        self.build()
        canary = "private-canary-zq9@x.example"
        bad = [None, [], SLUG, {"a": 1}, [True], [1], [SLUG, SLUG], [f"t{i}" for i in range(21)], ["../x"], ["a b"], ["late-town"], [canary]]
        good = self.save("good.json", SLUG)
        for value in bad:
            path = self.edit("bad.json", lambda d, v=value: d.update(agency_filter=v), "b0.json")
            for old, new, side in ((path, good, "OLD"), (good, path, "NEW")):
                for extra in ((), ("--json",)):
                    with self.subTest(value=repr(value)[:20], side=side, extra=extra):
                        self.assertEqual(self.diff(old, new, *extra), (1, "", f"diff: agency_filter is not 1-20 distinct plain slugs ({side})\n"))
        twenty = self.edit("twenty.json", lambda d: d.update(requests=[], runs=[], agency_filter=[f"t{i}" for i in range(20)]), "t0.json")
        self.assertEqual(self.diff(twenty, twenty)[::2], (0, ""))

    def test_selected_rows_cannot_claim_other_agencies(self):
        self.build()
        good = self.save("good.json", SLUG)
        cases = {
            "requests[0].agency": lambda d: d["requests"][0].update(agency=OTHER),
            "masked": lambda d: d["requests"][0].update(agency=OTHER, request_id="row 4"),
            "runs[0].agency": lambda d: d.update(runs=[{"agency": OTHER, "run": "2026-10-02", "state": "email-drafted"}]),
        }
        for name, fn in cases.items():
            path = self.edit("bad.json", fn, "b0.json")
            for old, new, side in ((path, good, "OLD"), (good, path, "NEW")):
                with self.subTest(name=name, side=side):
                    field = "runs[0].agency" if name.startswith("runs") else "requests[0].agency"
                    code, out, err = self.diff(old, new, "--json")
                    self.assertEqual((code, out, err), (1, "", f"diff: {field} is outside agency_filter ({side})\n"))
                    self.assertNotIn(OTHER, err)

    def test_legacy_loader_and_full_diff_are_unchanged(self):
        self.build()
        full, a = self.save("full.json"), self.save("a.json", SLUG)
        rows = MOD.load_snapshot(str(a), "OLD")
        self.assertIsInstance(rows, list)
        self.assertEqual(rows, [(("request"), SLUG, "R-1", "open-receipt-unknown", True)])
        self.assertEqual(self.diff(full, full)[::2], (0, ""))
        for key in ("extra", "agency"):
            path = self.edit("x.json", lambda d, k=key: d.update({k: [SLUG]}), "x0.json")
            self.assertEqual(self.diff(path, path), (1, "", "diff: snapshot keys are not flock-state-v1 (OLD)\n"))
        path = self.edit("missing.json", lambda d: d.pop("errors"), "m0.json")
        self.assertEqual(self.diff(path, path)[::2], (1, "diff: snapshot keys are not flock-state-v1 (OLD)\n"))
        self.requests(row("R-1", sha=base.SHA, ev="synthetic ack", rdate="2026-10-02"))
        changed = self.save("changed.json", SLUG)
        self.assertEqual(self.diff(a, changed)[0], 3)  # same scope, state moved: ordinary diff

    def test_read_only_and_snapshot_admission_controls(self):
        self.build()
        good = self.save("good.json", SLUG)
        other = self.save("other.json", OTHER)
        bad = self.edit("bad.json", lambda d: d.update(agency_filter=[]), "b0.json")
        link = self.tmp / "link.json"
        link.symlink_to(good)
        fifo = self.tmp / "fifo.json"
        os.mkfifo(fifo)
        (self.tmp / "junk.json").write_bytes(b"\xff\xfe")
        before = self.snap()
        for pair in ((good, other), (bad, good), (link, good), (fifo, good), (self.tmp / "junk.json", good)):
            code, out, err = self.diff(*pair)
            self.assertEqual((code, out), (1, ""))
            self.assertEqual(err.count("\n"), 1)
            self.assertNotIn("Traceback", err)
        self.assertEqual(self.snap(), before)


if __name__ == "__main__":
    unittest.main()
