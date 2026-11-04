"""RF1050-9: `flock_verify.py diff OLD NEW --json` prints one flock-verify-diff-v1 document; text, refusals and exits unchanged."""
import contextlib, copy, io, json, shutil, unittest
from unittest import mock
from tests.flock import test_verify as base
from tests.flock import test_verify_diff as dv

AGENCY, GOLDEN, DOWN, MISSING, summary = dv.AGENCY, dv.GOLDEN, dv.DOWN, dv.MISSING, dv.summary
CANARY = ("CANARYCLAIM", "CANARYNEXT", "CANARYREASON")
N3 = [["N3", "na-search-purposes", "record-gap", "strong", "The sample log has no case number field CANARYCLAIM.",
       "F-090c3d39b556", dv.RULE, "", "", "", "Ask how the case number is recorded CANARYNEXT."]]
N3V = [["N3", "confirm", "F-090c3d39b556", "2", "strong", "", "Page 2 lists no case number field CANARYREASON."]]
STRONG, ZERO = {"verdict": "confirm", "rating": "strong"}, dict.fromkeys(("added", "removed", "changed", "public_lost", "public_gained"), 0)


def doc(state="missing", findings=(), lost=(), gained=(), **counts):
    return {"schema": "flock-verify-diff-v1", "review_required": True, "new_public_ok_on_disk": state,
            "counts": {**ZERO, "old": 7, "new": 7, **counts}, "findings": list(findings),
            "public_ok": {"lost": list(lost), "gained": list(gained)}}


class VerifyDiffJsonTest(unittest.TestCase):
    setUp_base = base.FlockVerifyTest.setUp
    setUp, scaffold, verified, snap = dv.VerifyDiffTest.setUp, dv.VerifyDiffTest.scaffold, dv.VerifyDiffTest.verified, dv.VerifyDiffTest.snap

    def both(self, old=None, new=None):  # (code, text out, code, json out); read-only, no free text and no path in either
        before, args = self.snap(), (str(old or self.old), str(new or self.new))
        (code, text), (jcode, out) = base.run("flock_verify.py", "diff", *args), base.run("flock_verify.py", "diff", *args, "--json")
        self.assertEqual(self.snap(), before)
        for leak in dv.LEAK + list(CANARY):
            self.assertNotIn(leak, out)
            self.assertNotIn(leak, text)
        self.assertNotIn(str(self.store.parent), out)
        return code, text, jcode, out

    def json(self, old=None, new=None):  # -> (exit code, parsed document); exit equals the text form's
        code, _, jcode, out = self.both(old, new)
        self.assertEqual(jcode, code)
        self.assertNotIn("/", out)  # no path of any kind
        self.assertEqual(out, json.dumps(json.loads(out), sort_keys=True, indent=1, ensure_ascii=False) + "\n")
        return jcode, json.loads(out)

    def test_identical_runs_all_zero(self):
        self.verified(self.new)
        self.assertEqual(self.json(), (0, doc()))
        self.assertEqual(self.both()[:2], (0, MISSING + "\n" + summary() + "\n"))  # golden: main's text

    def test_added_removed_changed_and_lost(self):
        find = [r for r in copy.deepcopy(dv._F) if r[0] != "T1"] + N3
        ver = [r for r in copy.deepcopy(dv._V) if r[0] != "T1"] + N3V
        self.verified(self.new, find, ver, N1=DOWN)
        found = [{"id": "N1", "kind": "changed", "old": STRONG, "new": {"verdict": "downgrade", "rating": "medium"}},
                 {"id": "N3", "kind": "added", "old": None, "new": STRONG},
                 {"id": "T1", "kind": "removed", "old": STRONG, "new": None}]
        self.assertEqual(self.json(), (3, doc(findings=found, lost=["N1", "T1"], gained=["N3"],
                                              added=1, removed=1, changed=1, public_lost=2, public_gained=1)))
        self.assertEqual(self.both()[:2], (3, "\n".join([  # golden: main's text, ids interleaved in id order
            MISSING, "diff | changed N1 confirm/strong -> downgrade/medium", "diff | public-ok lost N1", "diff | added N3",
            "diff | public-ok gained N3", "diff | removed T1", "diff | public-ok lost T1",
            summary(added=1, removed=1, changed=1, lost=2, gained=1)]) + "\n"))
        # out of order (runs swapped): lost and gained trade places, added and removed too; N3 now lost, so still exit 3
        code, got = self.json(self.new, self.old)
        self.assertEqual((code, got["public_ok"], [f["kind"] for f in got["findings"]]),
                         (3, {"lost": ["N3"], "gained": ["N1", "T1"]}, ["changed", "removed", "added"]))

    def test_gained_only_exit_zero(self):
        self.verified(self.new, copy.deepcopy(dv._F) + N3, copy.deepcopy(dv._V) + N3V)
        self.assertEqual(self.json(), (0, doc(findings=[{"id": "N3", "kind": "added", "old": None, "new": STRONG}],
                                              gained=["N3"], new=8, added=1, public_gained=1)))
        self.assertEqual(self.both()[:2], (0, "\n".join([MISSING, "diff | added N3", "diff | public-ok gained N3",
                                                         summary(new=8, added=1, gained=1)]) + "\n"))

    def test_changed_without_public_change(self):  # a weak finding is never public-ok: no lost/gained, exit 0
        self.verified(self.new, O2={"verdict": "confirm", "new_rating": "weak"})
        code, got = self.json()
        self.assertEqual((code, got["findings"], got["public_ok"]), (0, [{"id": "O2", "kind": "changed",
            "old": {"verdict": "reject", "rating": "weak"}, "new": {"verdict": "confirm", "rating": "weak"}}],
            {"lost": [], "gained": []}))

    def test_new_public_ok_current_stale_missing(self):
        self.verified(self.new)
        self.assertEqual(base.run("flock_verify.py", "finish", str(self.new), "--golden", GOLDEN)[0], 0)
        self.assertEqual(self.json(), (0, doc("current")))
        later = self.scaffold("2026-11-01")
        self.verified(later, N1=DOWN)
        shutil.copy(later / "VERIFY.csv", self.new / "VERIFY.csv")
        code, got = self.json()
        self.assertEqual((code, got["new_public_ok_on_disk"], got["public_ok"]["lost"]), (3, "stale", ["N1"]))
        self.assertTrue(self.both()[1].startswith("diff: NEW PUBLIC-OK.csv on disk is stale; diff uses a fresh finish\n"))
        (self.new / "PUBLIC-OK.csv").unlink()
        self.assertEqual(self.json()[1]["new_public_ok_on_disk"], "missing")
        (self.new / "PUBLIC-OK.csv").symlink_to(later / "VERIFY.csv")  # a link is never followed: missing
        self.assertEqual(self.json()[1]["new_public_ok_on_disk"], "missing")

    def refused(self, old, new, line, code=1):  # the same one text line and exit under --json, never JSON
        got = self.both(old, new)
        self.assertEqual(got, (code, line + "\n", code, line + "\n"))

    def test_refusals_stay_text(self):
        self.verified(self.new)
        other = self.store / "agencies" / "other-agency" / "runs" / "2026-10-31"
        shutil.copytree(self.new, other)
        self.refused(self.old, other, "diff: runs belong to different agencies")
        self.refused(shutil.copytree(self.new, self.store / "loose"), self.new, "diff: runs belong to different agencies")
        link = self.store.parent / "link"
        link.symlink_to(self.new)
        self.refused(self.old, link, "diff: link refused")
        self.refused(link, self.new, "diff: link refused")
        good = (self.new / "VERIFY.json").read_bytes()
        (self.new / "VERIFY.json").write_text(json.dumps({"schema": 1, "verifier_provider": "Anthropic"}))
        self.refused(self.old, self.new, "diff: NEW refused: verifier family equals author family")
        (self.new / "VERIFY.json").write_bytes(good)
        (self.old / "VERIFY.json").unlink()
        self.refused(self.old, self.new, "diff: OLD refused: family unknown")

    def test_run_with_errors_stays_text(self):
        self.verified(self.new)
        bad = copy.deepcopy(dv._V)
        bad[1][3] = "3"  # N1 reread_page differs from the fact's page
        dv.write(self.new / "VERIFY.csv", bad)
        self.refused(self.old, self.new, "diff: NEW has 1 error(s); run --check first")
        dv.write(self.old / "VERIFY.csv", bad)
        self.refused(self.old, self.new, "diff: OLD has 1 error(s); run --check first")

    def test_io_error_unchanged(self):  # an unreadable CSV: class name only, exit 1, in both forms
        self.verified(self.new)
        for extra in ((), ("--json",)):
            out = io.StringIO()
            with self.subTest(extra=extra), contextlib.redirect_stdout(out), \
                    mock.patch.object(dv.verify.compare, "read_csv", side_effect=PermissionError(str(self.new))):
                code = dv.verify.main(["diff", str(self.old), str(self.new), *extra])
            self.assertEqual((code, out.getvalue()), (1, "io: PermissionError\n"))

    def test_usage(self):
        self.verified(self.new)
        for extra in (("--json",), (str(self.old), "--json"), (str(self.old), str(self.new), "--json", "--golden", GOLDEN)):
            with self.subTest(extra=extra):
                self.assertEqual(base.run("flock_verify.py", "diff", *extra), (2, ""))
        self.assertEqual(base.run("flock_verify.py", "--check", str(self.old), "--json")[0], 2)  # --json is diff-only


if __name__ == "__main__":
    unittest.main()
