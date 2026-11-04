"""RF0910-10: `withdraw|correct ... --dry-run` runs every withdraw/correct check, prints one `would ...` line and
`dry run | nothing written`, writes neither PUBLISH-CHANGES.csv nor PUBLISH.json, and exits as the real verb would."""
import contextlib, io, os, unittest
from unittest import mock
from tests.flock import test_publish as base
from tests.flock import test_publish_withdraw as tw
from tests.flock.test_publish_withdraw import A, B, BY, REF, HEADER, OTHER

DRY = "dry run | nothing written\n"


class PublishChangeDryRunTest(tw.PublishWithdrawTest):  # setUp: verified run A exported, N1 and O1 approved
    def dry_withdraw(self, fid, on=A, by=BY, ref=REF, run=None):
        return self.pub("withdraw", str(run or self.run_dir), "--finding", fid, "--on", on, "--by", by, "--approval-ref", ref, "--dry-run")

    def dry_correct(self, fid, new, on=A, by=BY, ref=REF):
        return self.pub("correct", self.rd, "--finding", fid, "--replaced-by", new, "--on", on, "--by", by, "--approval-ref", ref, "--dry-run")

    def assert_dry_refused(self, result, line, before, verb="withdraw"):
        self.assertEqual(result[0], 1, result[1])
        self.assertIn(line + "\n", result[1])
        self.assertTrue(result[1].endswith("%s refused | nothing written (dry run)\n" % verb), result[1])
        self.assertEqual(self.frozen(), before)

    def test_edge1_withdraw_dry_run_writes_nothing_then_real_run_succeeds(self):
        before = self.frozen()
        self.assertIsNone(before[1])  # change log absent
        self.assertEqual(self.dry_withdraw("N1"), (0, "would withdrawn | N1 | 2026-10-30\n" + DRY))
        self.assertEqual(self.frozen(), before)
        self.assertFalse(os.path.lexists(self.log))  # edge 6: never created
        self.assertEqual(self.withdraw("N1"), (0, "withdrawn | N1 | 2026-10-30\n"))  # same command, real
        self.assertEqual(self.log.read_text(), HEADER + "N1,withdraw,2026-10-30,%s,%s,\n" % (BY, REF))
        before = self.frozen()  # an existing change log stays byte-identical too
        self.assertEqual(self.dry_withdraw("O1"), (0, "would withdrawn | O1 | 2026-10-30\n" + DRY))
        self.assertEqual(self.frozen(), before)
        self.assert_dry_refused(self.dry_withdraw("N1"), "--finding: not an approved or needs-review entry", before)  # terminal now

    def test_edge1_needs_review_entry_dry_run(self):
        doc = self.doc()
        doc["findings"][1]["review"]["status"] = "needs-review"
        self.save(self.run_dir, doc)
        before = self.frozen()
        self.assertEqual(self.dry_withdraw("O1"), (0, "would withdrawn | O1 | 2026-10-30\n" + DRY))
        self.assertEqual(self.frozen(), before)

    def test_edge2_correct_dry_run_writes_nothing_in_either_run(self):
        later = self.copy_run()
        before, later_before = self.frozen(), (later / "PUBLISH.json").read_bytes()
        self.assertEqual(self.dry_correct("N1", "%s:O1" % later), (0, "would superseded | N1 | by 2026-10-31:O1 | 2026-10-30\n" + DRY))
        self.assertEqual(self.frozen(), before)
        self.assertFalse(os.path.lexists(self.log))
        self.assertEqual((later / "PUBLISH.json").read_bytes(), later_before)
        self.assertFalse(os.path.lexists(later / "PUBLISH-CHANGES.csv"))
        self.assertEqual(self.correct("N1", "%s:O1" % later), (0, "superseded | N1 | by 2026-10-31:O1 | 2026-10-30\n"))

    def test_edge3_correct_refusals_with_dry_run(self):  # test_publish_withdraw path 3
        later, other = self.copy_run(), self.copy_run(agency=OTHER)
        stale = self.copy_run("2026-11-01")
        doc = self.doc(stale)
        doc["run_date"] = A
        self.save(stale, doc)
        before = self.frozen()
        cases = [("%s:O1" % other, "--replaced-by: not a run of the same agency"),
                 ("%s:O1" % base.ROOT, "--replaced-by: not a run of the same agency"),
                 (str(later), "--replaced-by: not a run of the same agency"),
                 ("%s:A1" % later, "--replaced-by: not an approved entry"),
                 ("%s:Z9" % later, "--replaced-by: not an approved entry"),
                 ("%s:N1" % later, "--replaced-by: text equals the old text"),
                 ("%s:N1" % self.run_dir, "--replaced-by: is the same entry"),
                 ("%s:O1" % stale, "--replaced-by: file: run_date differs from the run")]
        for new, line in cases:
            with self.subTest(new=new):
                real, dry = self.correct("N1", new), self.dry_correct("N1", new)
                self.assert_dry_refused(dry, line, before, "correct")
                self.assertEqual(dry[1], real[1].replace("nothing written\n", "nothing written (dry run)\n"))  # same error lines
        self.assertFalse(os.path.lexists(later / "PUBLISH-CHANGES.csv"))

    def test_edge3_withdraw_refusals_with_dry_run(self):  # test_publish_withdraw path 4
        before = self.frozen()
        cases = [(("A1",), "--finding: not an approved or needs-review entry"),
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
                real, dry = self.withdraw(*args), self.dry_withdraw(*args)
                self.assert_dry_refused(dry, line, before)
                self.assertEqual(dry[1], real[1].replace("nothing written\n", "nothing written (dry run)\n"))
        self.assertFalse(os.path.lexists(self.log))

    def test_edge3_gate_failure_with_dry_run(self):  # a drifted run refuses before any entry check
        doc = self.doc()
        doc["run_date"] = B
        self.save(self.run_dir, doc)
        before = self.frozen()
        code, out = self.dry_withdraw("N1")
        self.assertEqual(code, 1)
        self.assertTrue(out.endswith("withdraw refused | nothing written (dry run)\n"), out)
        self.assertNotIn("would ", out)
        self.assertEqual(self.frozen(), before)

    def test_edge4_symlinked_change_log_with_dry_run(self):  # test_publish_withdraw path 8
        target = self.tmp / "elsewhere.csv"
        target.write_text(HEADER)
        self.log.symlink_to(target)
        before = self.frozen()
        self.assert_dry_refused(self.dry_withdraw("N1"), "PUBLISH-CHANGES.csv: is a symlink", before)
        later = self.copy_run("2026-11-01")
        self.assert_dry_refused(self.dry_correct("N1", "%s:O1" % later), "PUBLISH-CHANGES.csv: is a symlink", before, "correct")
        self.assertEqual(target.read_text(), HEADER)
        self.assertTrue(self.log.is_symlink())
        self.log.unlink()
        self.assertEqual(self.dry_withdraw("N1", run=base.ROOT / "tests"), (1, "flock-publish: RUNDIR is inside the repository\n"))

    def test_edge5_dry_run_usage_errors(self):
        copy = self.tmp / "copy.json"
        copy.write_text("{}")
        for argv in (["export", self.rd, "--site-slug", "sample", "--dry-run"],
                     ["status", str(self.store), "--dry-run"],
                     ["stage", self.rd, "--copy", str(copy), "--out", str(self.tmp / "staging"), "--dry-run"],
                     ["brief", str(self.store), "--agency", "x", "--out", str(self.tmp / "b.md"), "--dry-run"],
                     ["withdraw", self.rd, "--finding", "N1", "--on", A, "--by", BY, "--approval-ref", REF, "--today", A, "--dry-run"],
                     ["correct", self.rd, "--finding", "N1", "--on", A, "--by", BY, "--approval-ref", REF, "--dry-run"],
                     ["withdraw", self.rd, "--finding", "N1", "--on", A, "--by", BY, "--dry-run"]):
            with self.subTest(argv=argv[:1] + argv[-2:]):
                self.assertEqual(self.pub(*argv)[0], 2)
        self.assertFalse(os.path.lexists(self.log))
        self.assertFalse((self.tmp / "staging").exists())
        self.assertFalse((self.tmp / "b.md").exists())

    def test_dry_run_never_reaches_a_write(self):  # no open/write/truncate/unlink on the dry path (in-process)
        tool = self.load()
        argv = ["withdraw", self.rd, "--finding", "N1", "--on", A, "--by", BY, "--approval-ref", REF, "--dry-run"]
        boom = AssertionError("write reached")
        buf = io.StringIO()
        real_open = os.open
        def read_only_open(path, flags, *args, **kwargs):
            if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
                raise boom
            return real_open(path, flags, *args, **kwargs)
        with mock.patch.object(tool.verify_tool, "write", side_effect=boom), mock.patch.object(tool.os, "open", side_effect=read_only_open), \
                mock.patch.object(tool.os, "truncate", side_effect=boom), contextlib.redirect_stdout(buf):
            self.assertEqual(tool.main(argv), 0)
        self.assertEqual(buf.getvalue(), "would withdrawn | N1 | 2026-10-30\n" + DRY)
        self.assertFalse(os.path.lexists(self.log))


for _name in [n for n in dir(tw.PublishWithdrawTest) if n.startswith("test_")]:
    setattr(PublishChangeDryRunTest, _name, None)  # the withdraw/base tests run once, in their own modules

if __name__ == "__main__":
    unittest.main()
