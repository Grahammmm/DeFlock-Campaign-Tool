"""DF1537-11: `withdraw` / `correct` move an approved PUBLISH.json entry to withdrawn / superseded with a dated change-log row;
`--check --run` (and so `stage`) refuses an approved text that was withdrawn or superseded in any run of the same agency."""
import csv, importlib.util, io, json, os, shutil, contextlib, unittest
from unittest import mock
from tests.flock import test_publish as base

A, B = "2026-10-30", "2026-10-31"  # run A is the base fixture run; run B its copy one day later (same agency)
OTHER = "other-city-police-department"
APPROVED = {"status": "approved", "approved_by": "G", "approved_on": "2026-10-30", "approval_ref": "G-1"}
BY, REF = "Pat Organizer", "G-DEC-1537"
HEADER = "finding_id,action,on,by,approval_ref,replaced_by\n"


class PublishWithdrawTest(base.FlockPublishTest):  # setUp: verified run A exported, N1 and O1 approved
    def setUp(self):
        super().setUp()
        self.assertEqual(self.export(), (0, "exported=4 refused=0\n"))
        self.runs, self.log = self.run_dir.parent, self.run_dir / "PUBLISH-CHANGES.csv"
        doc = json.loads(self.out.read_text())
        for f in doc["findings"]:
            f["review"] = dict(APPROVED) if f["finding_id"] in ("N1", "O1") else f["review"]
        self.save(self.run_dir, doc)
        self.texts = [f["text"] for f in doc["findings"]]

    def save(self, run, doc):
        (run / "PUBLISH.json").write_text(json.dumps(doc, indent=2) + "\n")

    def doc(self, run=None):
        return json.loads(((run or self.run_dir) / "PUBLISH.json").read_text())

    def copy_run(self, day=B, agency=None):  # a copy of run A as another run of this (or another) agency
        target = (self.store / "agencies" / agency / "runs" / day) if agency else self.runs / day
        shutil.copytree(self.run_dir, target)
        doc = self.doc(target)
        doc["run_date"] = day
        self.save(target, doc)
        return target

    def pub(self, *argv):  # every run is checked for output hygiene (edge path 9)
        code, out = base.run("flock_publish.py", *argv)
        for secret in self.texts + [BY, REF, "G-1"]:
            self.assertNotIn(secret, out)
        return code, out

    def withdraw(self, fid, on=A, by=BY, ref=REF, run=None):
        return self.pub("withdraw", str(run or self.run_dir), "--finding", fid, "--on", on, "--by", by, "--approval-ref", ref)

    def correct(self, fid, new, on=A, by=BY, ref=REF):
        return self.pub("correct", self.rd, "--finding", fid, "--replaced-by", new, "--on", on, "--by", by, "--approval-ref", ref)

    def checked(self, run=None):
        run = run or self.run_dir
        return self.pub("--check", str(run / "PUBLISH.json"), "--run", str(run))

    def frozen(self):  # PUBLISH.json bytes and the change log (None = absent)
        return self.out.read_bytes(), self.log.read_bytes() if os.path.lexists(self.log) else None

    def assert_refused(self, result, line, before, verb="withdraw"):
        self.assertEqual(result[0], 1, result[1])
        self.assertIn(line + "\n", result[1])
        self.assertTrue(result[1].endswith("%s refused | nothing written\n" % verb), result[1])
        self.assertEqual(self.frozen(), before)

    def test_path1_withdraw_approved_entry(self):
        self.assertEqual(self.withdraw("N1"), (0, "withdrawn | N1 | 2026-10-30\n"))
        self.assertEqual(self.doc()["findings"][0]["review"], dict(APPROVED, status="withdrawn"))  # approval fields kept
        self.assertEqual(self.log.read_text(), HEADER + "N1,withdraw,2026-10-30,%s,%s,\n" % (BY, REF))
        self.assertEqual(self.log.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.out.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.checked(), (0, "findings=4 approved=1 errors=0 drift=0\n"))
        code, out = self.pub("status", str(self.store))
        self.assertIn("| %s | partly-approved | findings=4 pending=3 approved=1 errors=0\n" % A, out)
        self.assertEqual(code, 0)
        self.assertEqual(self.pub("recheck", self.rd, "--today", B),  # a logged terminal entry passes recheck's schema gate
                         (0, "recheck | approved=1 moved=0 | review-required; G decides any site use\n"))

    def test_path1_needs_review_entry_can_be_withdrawn_and_log_appends(self):
        doc = self.doc()
        doc["findings"][1]["review"]["status"] = "needs-review"
        self.save(self.run_dir, doc)
        self.assertEqual(self.withdraw("O1"), (0, "withdrawn | O1 | 2026-10-30\n"))
        self.assertEqual(self.withdraw("N1"), (0, "withdrawn | N1 | 2026-10-30\n"))
        self.assertEqual(self.log.read_text().count("\n"), 3)
        self.assertEqual(self.checked(), (0, "findings=4 approved=0 errors=0 drift=0\n"))

    def test_path2_correct_with_a_later_run_of_the_same_agency(self):
        later = self.copy_run()
        self.assertEqual(self.correct("N1", "%s:O1" % later), (0, "superseded | N1 | by 2026-10-31:O1 | 2026-10-30\n"))
        self.assertEqual(self.doc()["findings"][0]["review"], dict(APPROVED, status="superseded"))
        self.assertEqual(self.log.read_text(), HEADER + "N1,correct,2026-10-30,%s,%s,2026-10-31:O1\n" % (BY, REF))
        self.assertEqual(self.checked(), (0, "findings=4 approved=1 errors=0 drift=0\n"))
        self.assertFalse((later / "PUBLISH-CHANGES.csv").exists())  # the new run is never written

    def test_path3_correct_refusals_write_nothing(self):
        later, other = self.copy_run(), self.copy_run(agency=OTHER)
        stale = self.copy_run("2026-11-01")
        doc = self.doc(stale)
        doc["run_date"] = A  # this run's file fails its own --check (drift)
        self.save(stale, doc)
        before = self.frozen()
        cases = [("%s:O1" % other, "--replaced-by: not a run of the same agency"),
                 ("%s:O1" % base.ROOT, "--replaced-by: not a run of the same agency"),
                 (str(later), "--replaced-by: not a run of the same agency"),  # no :NEWID
                 ("%s:A1" % later, "--replaced-by: not an approved entry"),  # pending there
                 ("%s:Z9" % later, "--replaced-by: not an approved entry"),
                 ("%s:N1" % later, "--replaced-by: text equals the old text"),
                 ("%s:N1" % self.run_dir, "--replaced-by: is the same entry"),
                 ("%s:O1" % stale, "--replaced-by: file: run_date differs from the run")]
        for new, line in cases:
            with self.subTest(new=new):
                self.assert_refused(self.correct("N1", new), line, before, "correct")

    def test_path4_withdraw_refusals_write_nothing(self):
        before = self.frozen()
        cases = [(("A1",), "--finding: not an approved or needs-review entry"),  # pending
                 (("Z9",), "--finding: not an approved or needs-review entry"),
                 (("N1", "2026-10-29"), "--on: not a YYYY-MM-DD from approved_on to today"),
                 (("N1", "2999-01-01"), "--on: not a YYYY-MM-DD from approved_on to today"),
                 (("N1", "2026-02-30"), "--on: not a YYYY-MM-DD from approved_on to today"),
                 (("N1", "30/10/2026"), "--on: not a YYYY-MM-DD from approved_on to today"),
                 (("N1", A, " "), "--by: empty, over 80 characters or has a newline"),
                 (("N1", A, "Pat\nOrganizer"), "--by: empty, over 80 characters or has a newline"),
                 (("N1", A, "p" * 81), "--by: empty, over 80 characters or has a newline"),
                 (("N1", A, BY, ""), "--approval-ref: empty, over 80 characters or has a newline"),
                 (("N1", A, BY, "G-1\r"), "--approval-ref: empty, over 80 characters or has a newline")]
        for args, line in cases:
            with self.subTest(args=args[:2]):
                self.assert_refused(self.withdraw(*args), line, before)
        self.assertEqual(self.withdraw("N1")[0], 0)
        before = self.frozen()
        self.assert_refused(self.withdraw("N1"), "--finding: not an approved or needs-review entry", before)  # terminal
        later = self.copy_run("2026-11-01")
        self.assert_refused(self.correct("N1", "%s:O1" % later), "--finding: not an approved or needs-review entry", before, "correct")

    def test_path4_needs_review_without_approval_fields_refused(self):
        doc = self.doc()
        doc["findings"][1]["review"] = {"status": "needs-review", "approved_by": "", "approved_on": "", "approval_ref": ""}
        self.save(self.run_dir, doc)
        self.assert_refused(self.withdraw("O1"), "--finding: not an approved or needs-review entry", self.frozen())

    def test_path5_later_run_reapproving_withdrawn_text_refused_and_not_staged(self):
        later = self.copy_run()
        self.assertEqual(self.withdraw("N1")[0], 0)
        self.assertEqual(self.checked(later), (1, "finding N1: text was withdrawn in run 2026-10-30\n"
                                                "findings=4 approved=2 errors=1 drift=0\n"))
        doc = self.doc()
        doc["findings"][0]["text"] = "  " + doc["findings"][0]["text"].replace(" ", " \n\t ") + " "  # whitespace-only difference
        self.save(self.run_dir, doc)
        self.assertIn("finding N1: text was withdrawn in run 2026-10-30\n", self.checked(later)[1])
        (self.tmp / "copy.json").write_text("{}")
        code, out = self.pub("stage", str(later), "--copy", str(self.tmp / "copy.json"), "--out", str(self.tmp / "staging"))
        self.assertEqual(code, 1)
        self.assertIn("finding N1: text was withdrawn in run 2026-10-30\n", out)
        self.assertTrue(out.endswith("nothing written\n"), out)
        self.assertFalse((self.tmp / "staging").exists())
        self.assertIn("| %s | invalid |" % B, self.pub("status", str(self.store))[1])
        before = (later / "PUBLISH.json").read_bytes()
        code, out = self.withdraw("O1", run=later)  # the later run's own verbs refuse too
        self.assertEqual(code, 1)
        self.assertIn("finding N1: text was withdrawn in run 2026-10-30\n", out)
        self.assertEqual(((later / "PUBLISH.json").read_bytes(), (later / "PUBLISH-CHANGES.csv").exists()), (before, False))

    def test_path5_another_agency_withdrawal_does_not_block(self):
        other = self.copy_run(agency=OTHER)
        doc = self.doc(other)
        doc["findings"][0]["review"]["status"] = "withdrawn"
        self.save(other, doc)
        self.assertEqual(self.checked(), (0, "findings=4 approved=2 errors=0 drift=0\n"))

    def test_path6_change_log_mismatches_named(self):
        doc = self.doc()
        doc["findings"][0]["review"]["status"] = "withdrawn"  # hand edit, no change row
        self.save(self.run_dir, doc)
        self.assertEqual(self.checked(), (1, "finding N1: change log does not match\nfindings=4 approved=1 errors=1 drift=0\n"))
        rows = {"row for an approved entry": "N1,withdraw,%s,x,y,\nO1,withdraw,%s,x,y,\n" % (A, A),
                "duplicate row": "N1,withdraw,%s,x,y,\nN1,withdraw,%s,x,y,\n" % (A, A),
                "wrong action": "N1,correct,%s,x,y,%s:O1\n" % (A, B)}
        for why, body in rows.items():
            with self.subTest(why=why):
                self.log.write_text(HEADER + body)
                code, out = self.checked()
                self.assertEqual(code, 1)
                self.assertIn("finding %s: change log does not match\n" % ("O1" if "approved" in why else "N1"), out)
        for body in ("finding_id,action\nN1,withdraw\n", HEADER + "N1,withdraw,%s,x,y\n" % A, ""):
            self.log.write_text(body)
            self.assertIn("PUBLISH-CHANGES.csv: a link, or its header or row widths differ\n", self.checked()[1])

    def load(self):
        spec = importlib.util.spec_from_file_location("_test_withdraw", base.TOOLS / "flock_publish.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_path7_failed_publish_write_takes_the_log_row_back(self):
        tool = self.load()
        argv = ["withdraw", self.rd, "--finding", "N1", "--on", A, "--by", BY, "--approval-ref", REF]
        for existing in (False, True):
            if existing:
                self.assertEqual(self.withdraw("O1")[0], 0)
            before, buf = self.frozen(), io.StringIO()
            with mock.patch.object(tool.verify_tool, "write", side_effect=OSError("disk full")), contextlib.redirect_stdout(buf):
                self.assertEqual(tool.main(argv), 1)
            self.assertEqual(buf.getvalue(), "io: OSError\n")
            self.assertEqual(self.frozen(), before, existing)  # byte-identical log (or still absent), PUBLISH.json unchanged
        self.assertEqual(self.checked(), (0, "findings=4 approved=1 errors=0 drift=0\n"))

    def test_path8_symlinked_change_log_and_repo_rundir_refused(self):
        target = self.tmp / "elsewhere.csv"
        target.write_text(HEADER)
        self.log.symlink_to(target)
        before = self.frozen()
        self.assert_refused(self.withdraw("N1"), "PUBLISH-CHANGES.csv: is a symlink", before)
        self.assertEqual(target.read_text(), HEADER)
        self.assertIn("PUBLISH-CHANGES.csv: a link, or its header or row widths differ\n", self.checked()[1])
        self.log.unlink()
        self.assertEqual(self.withdraw("N1", run=base.ROOT / "tests"), (1, "flock-publish: RUNDIR is inside the repository\n"))

    def test_usage_errors(self):
        for argv in (["withdraw", self.rd, "--finding", "N1", "--on", A, "--by", BY],
                     ["withdraw", self.rd, "--finding", "N1", "--on", A, "--by", BY, "--approval-ref", REF, "--replaced-by", "x:y"],
                     ["correct", self.rd, "--finding", "N1", "--on", A, "--by", BY, "--approval-ref", REF],
                     ["export", self.rd, "--site-slug", "sample", "--finding", "N1"],
                     ["withdraw", self.rd, "--finding", "N1", "--on", A, "--by", BY, "--approval-ref", REF, "--run", self.rd]):
            with self.subTest(argv=argv[:1] + argv[-2:]):
                self.assertEqual(self.pub(*argv)[0], 2)
        self.assertFalse(os.path.lexists(self.log))


for _name in [n for n in dir(base.FlockPublishTest) if n.startswith("test_")]:
    setattr(PublishWithdrawTest, _name, None)  # base tests run once, in tests/flock/test_publish.py

if __name__ == "__main__":
    unittest.main()
