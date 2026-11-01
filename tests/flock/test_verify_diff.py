"""RF1710-10: `flock_verify.py diff OLD NEW` names verifier outcome and public-ok changes between two runs of one agency."""
import contextlib, copy, csv, importlib.util, io, json, shutil, sys, unittest
from pathlib import Path
from unittest import mock
from tests.flock import test_verify as base

FIX, AGENCY, GOLDEN = base.FIXTURES, base.AGENCY, base.GOLDEN
RULE = "ca-civ-1798.90.52-access-records"
TAIL = " | ids only, not a review verdict"
MISSING = "diff: NEW PUBLIC-OK.csv on disk is missing; diff uses a fresh finish"
DOWN = {"verdict": "downgrade", "new_rating": "medium", "records_sought": "Existing search log field list"}
_spec = importlib.util.spec_from_file_location("_rf1710_verify", base.TOOLS / "flock_verify.py")
verify = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(verify)


def table(name):
    with open(FIX / name, newline="") as handle:
        return list(csv.reader(handle))


def write(path, rows):
    with open(path, "w", newline="") as handle:
        csv.writer(handle, lineterminator="\n").writerows(rows)


_F, _V = table("findings-sample.csv"), table("verify-sample.csv")
LEAK = ([r[i] for r in _V[1:] for i in (5, 6) if r[i]]  # records_sought and reason
        + sorted({r[1] for r in _F[1:]}) + [r[i] for r in _F[1:] for i in (4, 8, 10) if r[i]])  # topic, claim, quote, next_step


def summary(old=7, new=7, added=0, removed=0, changed=0, lost=0, gained=0):
    return "diff | old=%d new=%d added=%d removed=%d changed=%d public_lost=%d public_gained=%d" % (
        old, new, added, removed, changed, lost, gained) + TAIL


class VerifyDiffTest(unittest.TestCase):
    setUp_base = base.FlockVerifyTest.setUp

    def setUp(self):
        self.setUp_base()
        self.old = self.run_dir
        self.verified(self.old)
        self.new = self.scaffold("2026-10-31")

    def scaffold(self, date, agency=AGENCY):
        code, _ = base.run("flock_compare.py", "scaffold", "--store", str(self.store), "--agency", agency, "--date", date,
                           "--provider", "anthropic", "--model", "claude-opus-5-5",
                           "--applicability", "Sample city police; local ALPR policy applies")
        self.assertEqual(code, 0)
        return self.store / "agencies" / agency / "runs" / date

    def verified(self, run, find=None, ver=None, **edits):  # edits: finding id -> VERIFY.csv field changes
        find, ver = find or copy.deepcopy(_F), ver or copy.deepcopy(_V)
        for row in ver[1:]:
            for column, value in edits.get(row[0], {}).items():
                row[ver[0].index(column)] = value
        write(run / "FINDINGS.csv", find)
        self.assertEqual(base.run("flock_verify.py", "start", str(run), "--provider", "openai", "--model", "gpt-5-codex")[0], 0)
        write(run / "VERIFY.csv", ver)
        self.assertEqual(base.run("flock_verify.py", "--check", str(run))[0], 0)

    def snap(self):
        return sorted((str(p.relative_to(self.store)), p.lstat().st_size, p.lstat().st_mtime_ns,
                       p.read_bytes() if p.is_file() and not p.is_symlink() else b"") for p in self.store.rglob("*"))

    def diff(self, old=None, new=None, *extra):  # every call: read-only, and no reason/records_sought/topic/claim text
        before = self.snap()
        code, out = base.run("flock_verify.py", "diff", str(old or self.old), str(new or self.new), *extra)
        self.assertEqual(self.snap(), before)
        for text in LEAK:
            self.assertNotIn(text, out)
        return code, out

    def test_identical_runs_all_zero(self):
        self.verified(self.new)
        self.assertEqual(self.diff(), (0, MISSING + "\n" + summary() + "\n"))
        self.assertEqual(self.diff(self.old, self.old), (0, "diff: NEW PUBLIC-OK.csv on disk is missing; diff uses a fresh "
                                                             "finish\n" + summary() + "\n"))

    def test_strong_downgraded_loses_public_ok(self):
        self.verified(self.new, N1=DOWN)
        self.assertEqual(self.diff(), (3, "\n".join([MISSING, "diff | changed N1 confirm/strong -> downgrade/medium",
                                                     "diff | public-ok lost N1", summary(changed=1, lost=1)]) + "\n"))
        # out of order (runs swapped): the same change reads as a gain, which is not exit 3
        self.assertEqual(self.diff(self.new, self.old)[1].splitlines()[1:],
                         ["diff | changed N1 downgrade/medium -> confirm/strong", "diff | public-ok gained N1",
                          summary(changed=1, gained=1)])
        self.assertEqual(self.diff(self.new, self.old)[0], 0)

    def test_rejected_strong_loses_public_ok(self):
        self.verified(self.new, A1={"verdict": "reject", "new_rating": ""})
        code, out = self.diff()
        self.assertEqual((code, out.splitlines()[1:]), (3, ["diff | changed A1 confirm/strong -> reject/strong",
                                                            "diff | public-ok lost A1", summary(changed=1, lost=1)]))

    def test_removed_public_ok_finding(self):
        find, ver = [r for r in _F if r[0] != "T1"], [r for r in _V if r[0] != "T1"]
        self.verified(self.new, find, ver)
        self.assertEqual(self.diff(), (3, "\n".join([MISSING, "diff | removed T1", "diff | public-ok lost T1",
                                                     summary(new=6, removed=1, lost=1)]) + "\n"))
        ver = [r for r in _V if r[0] != "O2"]  # a removed finding that was never public-ok: no lost line, exit 0
        self.verified(self.scaffold("2026-11-01"), [r for r in _F if r[0] != "O2"], ver)
        self.assertEqual(self.diff(self.old, self.store / "agencies" / AGENCY / "runs" / "2026-11-01")[1].splitlines()[1:],
                         ["diff | removed O2", summary(new=6, removed=1)])

    def test_added_confirmed_strong_gains_public_ok(self):
        find = copy.deepcopy(_F) + [["N3", "na-search-purposes", "record-gap", "strong", "The sample log has no case number field.",
                                     "F-090c3d39b556", RULE, "", "", "", "Ask how the case number is recorded."]]
        ver = copy.deepcopy(_V) + [["N3", "confirm", "F-090c3d39b556", "2", "strong", "", "Page 2 lists no case number field."]]
        self.verified(self.new, find, ver)
        self.assertEqual(self.diff(), (0, "\n".join([MISSING, "diff | added N3", "diff | public-ok gained N3",
                                                     summary(new=8, added=1, gained=1)]) + "\n"))

    def test_rule_now_needs_attorney_review(self):  # package fixture: the rule reads needs_attorney_review for NEW's outputs
        self.verified(self.new)
        real, new = verify.compare.law.load_package, self.new.resolve()

        def package(name):
            pkg, caller = copy.deepcopy(real(name)), sys._getframe(1)
            if caller.f_code.co_name == "outputs" and caller.f_locals.get("run") == new:
                for rule in pkg["rules"]:
                    rule["review"] = "needs_attorney_review" if rule["rule_id"] == RULE else rule["review"]
            return pkg
        out = io.StringIO()
        with mock.patch.object(verify.compare.law, "load_package", package), contextlib.redirect_stdout(out):
            code = verify.main(["diff", str(self.old), str(self.new)])
        self.assertEqual((code, out.getvalue().splitlines()), (3, [MISSING, "diff | public-ok lost N1", "diff | public-ok lost O1",
                                                                   summary(lost=2)]))

    def test_errors_refuse(self):
        self.verified(self.new)
        verify_csv = self.old / "VERIFY.csv"
        good = verify_csv.read_bytes()
        bad = copy.deepcopy(_V)
        bad[1][3] = "3"  # N1 reread_page differs from the fact's page
        write(verify_csv, bad)
        self.assertEqual(self.diff(), (1, "diff: OLD has 1 error(s); run --check first\n"))
        verify_csv.write_bytes(good)
        write(self.new / "VERIFY.csv", bad)
        self.assertEqual(self.diff(), (1, "diff: NEW has 1 error(s); run --check first\n"))
        (self.new / "VERIFY.csv").unlink()
        self.assertEqual(self.diff(), (1, "diff: NEW has 1 error(s); run --check first\n"))

    def test_other_agency_or_link_refused(self):
        self.verified(self.new)
        other = self.store / "agencies" / "other-agency" / "runs" / "2026-10-31"
        shutil.copytree(self.new, other)
        loose = shutil.copytree(self.new, self.store / "loose")
        for old, new in ((self.old, other), (other, self.new), (self.old, loose), (loose, self.new)):
            with self.subTest(old=old, new=new):
                self.assertEqual(self.diff(old, new), (1, "diff: runs belong to different agencies\n"))
        link = self.store.parent / "link"
        link.symlink_to(self.new)  # even a link to a run of the same agency
        for old, new in ((self.old, link), (link, self.new)):
            self.assertEqual(self.diff(old, new), (1, "diff: link refused\n"))

    def test_family_refusal(self):
        self.verified(self.new)
        (self.new / "VERIFY.json").write_text(json.dumps({"schema": 1, "verifier_provider": "Anthropic"}))
        self.assertEqual(self.diff(), (1, "diff: NEW refused: verifier family equals author family\n"))
        (self.old / "VERIFY.json").unlink()
        self.assertEqual(self.diff(), (1, "diff: OLD refused: family unknown\n"))

    def test_finished_new_current_or_stale(self):
        self.verified(self.new)
        self.assertEqual(base.run("flock_verify.py", "finish", str(self.new), "--golden", GOLDEN)[0], 0)
        self.assertEqual(self.diff(), (0, summary() + "\n"))  # current on disk: no note line
        self.verified(self.scaffold("2026-11-01"), N1=DOWN)  # rebuild NEW's VERIFY.csv as a downgrade after finish
        shutil.copy(self.store / "agencies" / AGENCY / "runs" / "2026-11-01" / "VERIFY.csv", self.new / "VERIFY.csv")
        self.assertEqual(self.diff(), (3, "\n".join(["diff: NEW PUBLIC-OK.csv on disk is stale; diff uses a fresh finish",
                                                     "diff | changed N1 confirm/strong -> downgrade/medium",
                                                     "diff | public-ok lost N1", summary(changed=1, lost=1)]) + "\n"))
        self.assertIn("N1", (self.new / "PUBLIC-OK.csv").read_text(encoding="utf-8"))  # disk untouched, diff used fresh outputs

    def test_usage(self):
        self.verified(self.new)
        for extra in ((), (str(self.old),), (str(self.old), str(self.new), str(self.new)),
                      (str(self.old), str(self.new), "--golden", GOLDEN), ("--check", str(self.old), str(self.new))):
            with self.subTest(extra=extra):
                self.assertEqual(base.run("flock_verify.py", "diff", *extra)[0], 2)
        self.assertEqual(base.run("flock_verify.py", "--check", str(self.old)), (0, "verified=7 errors=0\n"))
        self.assertEqual(base.run("flock_verify.py", "current", str(self.old), str(self.new))[0], 2)


if __name__ == "__main__":
    unittest.main()
