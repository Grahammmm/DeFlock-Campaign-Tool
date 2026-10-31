import contextlib, io, subprocess, unittest
from types import SimpleNamespace
from unittest import mock

from tests.flock import test_review_batch as base
from tests.flock.test_review import ROOT, SLUG
from tests.flock.test_review_lock import load

batch = load("_rf1105_8_batch", ROOT / "tools" / "flock_review_batch.py")
FAIL = b"stage compare-build failed rc=2 log=/tmp/x/compare.log\n"
# Captured from the same call on main 862f362 with the same canned child ({rc} = 0 or 3; the stub agency never really moves).
MAIN = ("golden ok keys=10\nrun %s rc={rc} state=ready\n%s ready 2/28 next=inventory\n"
        "agencies=1 done=0 waiting=0 in_flight=0 failed=0 invalid=0 held=0 locked=0\n") % (SLUG, SLUG)


class ReviewBatchReasonTest(unittest.TestCase):
    def setUp(self):  # one ready stub agency and the G5 sample golden
        base.FlockReviewBatchTest.setUp(self)
        base.FlockReviewBatchTest.world(self)
        self.list.write_text(str(self.configs[0]) + "\n")

    def run_child(self, stdout=b"", rc=1, stderr=b""):  # flock_review run is a canned child, so no stage runs
        calls, shown, errors = [], io.StringIO(), io.StringIO()
        def child(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, rc, stdout, stderr)
        fake = SimpleNamespace(run=child, DEVNULL=subprocess.DEVNULL)
        with mock.patch.object(batch, "subprocess", fake), contextlib.redirect_stdout(shown), contextlib.redirect_stderr(errors):
            code = batch.main(["status", str(self.list), "--run", *map(str, self.gate)])
        self.assertEqual((code, len(calls), calls[0][-2:]), (0, 1, ["run", str(self.configs[0].resolve())]))
        self.assertEqual(errors.getvalue(), "")
        return shown.getvalue()

    def reason(self, out, rc=1):  # the reason line, which must come right after the run line
        lines = out.splitlines()
        at = lines.index("run %s rc=%d state=failed" % (SLUG, rc))
        self.assertTrue(lines[at + 1].startswith("run %s reason: " % SLUG), out)
        self.assertEqual(sum(" reason: " in x for x in lines), 1)
        self.assertIn(" failed=1 ", out)
        return lines[at + 1][len("run %s reason: " % SLUG):]

    def unknown(self):
        return "unknown (see flock_review status %s)" % self.configs[0].resolve()

    def test_failed_stage_named_with_its_log(self):
        out = self.run_child(b"stage inventory done rc=0 log=/tmp/x/inv.log\nstage manifest done rc=0 log=/tmp/x/man.log\n" + FAIL
                             + b"stages=28 done=2 cached=0 skip=0 waiting=None failed=compare-build\n")
        self.assertEqual(self.reason(out), "stage compare-build failed rc=2 log=/tmp/x/compare.log")
        # the last matching line wins; any other non-0/3 exit code is explained the same way
        out = self.run_child(b"stage manifest failed rc=1 log=/tmp/x/a.log\n" + FAIL, rc=2)
        self.assertEqual(self.reason(out, rc=2), "stage compare-build failed rc=2 log=/tmp/x/compare.log")
        out = self.run_child(b"io: OSError\n" + FAIL + b"stage compare-build done rc=0 log=/tmp/x/c.log\n", rc=-9)
        self.assertEqual(self.reason(out, rc=-9), "stage compare-build failed rc=2 log=/tmp/x/compare.log")

    def test_io_and_lock_relayed(self):
        self.assertEqual(self.reason(self.run_child(b"io: PermissionError\n")), "io: PermissionError")
        self.assertEqual(self.reason(self.run_child(b"locked: /tmp/x/out/.review/LOCK\n")), "locked: /tmp/x/out/.review/LOCK")

    def test_free_text_never_printed(self):
        text = [b"Traceback (most recent call last):", b"ValueError: Officer Jane Roe, plate 7ABC123",
                b"io: Permission denied reading /records/secret.pdf", b"see stage compare-build failed rc=2 log=/x",
                b"stage compare-build failed rc=2 log=/x\x1b[2Jtrailing", b"locked: /x\x00trailing", b"stage Compare Build failed rc=2 log=/x",
                b"stage compare-build failed rc=two log=/x", b"stage compare-build failed rc=2 log=", b"locked:", b"io: Permission\xffError"]
        out = self.run_child(b"\n".join(text) + b"\n")
        self.assertEqual(self.reason(out), self.unknown())
        for word in ("Traceback", "Jane", "7ABC123", "secret", "trailing", "Compare Build", "two", "Permission"):
            self.assertNotIn(word, out)
        self.assertEqual(self.reason(self.run_child(b"")), self.unknown())

    def test_stderr_never_printed(self):
        out = self.run_child(b"", stderr=b"CANARY-7f3 stage compare-build failed rc=2 log=/tmp/canary.log\nio: PermissionError\n")
        self.assertEqual(self.reason(out), self.unknown())
        self.assertNotIn("CANARY", out)
        self.assertNotIn("canary.log", out)

    def test_non_utf8_output_does_not_crash(self):
        out = self.run_child(b"\xff\xfe\x80junk\nstage compare-build failed rc=2 log=/tmp/x/\xffc.log\n\xc3\n")
        self.assertEqual(self.reason(out), "stage compare-build failed rc=2 log=/tmp/x/\ufffdc.log")
        self.assertEqual(self.reason(self.run_child(b"\xff\xfe\x80\n\xc3(\n")), self.unknown())

    def test_overlong_line_capped_at_300(self):
        line = "stage compare-build failed rc=2 log=/tmp/" + "a" * 400
        self.assertEqual(self.reason(self.run_child(line.encode() + b"\n")), line[:300])
        self.assertEqual(self.reason(self.run_child(b"locked: " + b"b" * 292 + b"\n")), "locked: " + "b" * 292)

    def test_success_and_wait_unchanged(self):
        for rc in (0, 3):
            for stdout in (b"", b"stage inventory done rc=0 log=/tmp/x/inv.log\n" + FAIL + b"io: PermissionError\n"):
                self.assertEqual(self.run_child(stdout, rc=rc, stderr=b"CANARY"), MAIN.format(rc=rc))


if __name__ == "__main__":
    unittest.main()
