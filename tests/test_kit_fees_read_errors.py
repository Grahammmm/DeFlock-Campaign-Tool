"""kit-fees: an OSError escaping the report's two input reads is one fixed, path-free refusal. Offline, synthetic."""
import errno
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import kit_export, kit_fees, kit_track  # noqa: E402
from tests.test_kit_check import ready_drafts  # noqa: E402
from tests.test_kit_status_csv import run  # noqa: E402

FIXED = "kit-fees: unable to read campaign inputs"
STOPPED = f"Stopped: {FIXED}\n"
PRIVATE = "SYNTHETIC-PRIVATE-MESSAGE-\u00e9"
PRIVATE_FILE = "/SYNTHETIC-PRIVATE-DIR/sent.json"
REAL_READ_SENT = kit_track.read_sent


def oserror(cls: type[OSError] = PermissionError, code=errno.EACCES):
    error = cls(code, PRIVATE + "\nSECOND-LINE")
    error.filename, error.filename2 = PRIVATE_FILE, PRIVATE_FILE + ".2"
    return error


class KitFeesReadErrorTests(unittest.TestCase):
    def setUp(self):
        offline = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used"))
        offline.start()
        self.addCleanup(offline.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-kit-fees-read-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        self.cmd("init", "--directory", self.root, "--name", "Synthetic", "--county", "San Luis Obispo", "--state", "CA")
        self.cmd("kit", "--directory", self.root)
        self.a, self.b, self.c = (p.stem for p in sorted((self.root / "kit" / "requests").glob("*.md"))[:3])
        ready_drafts(self.root)
        self.sent = self.root / "kit" / "sent.json"
        self.private = self.tmp / "private"
        self.private.mkdir()
        self.canary = self.private / "canary.csv"
        self.canary.write_text("keep me\n", encoding="utf-8")

    def cmd(self, *arguments):
        code, out, err = run(*arguments)
        self.assertEqual(code, 0, err)
        return out

    def populate(self):
        for agency, amount in ((self.a, "100.05"), (self.b, "40"), (self.c, "0.10")):
            self.cmd("kit-sent", "--directory", self.root, "--agency", agency, "--date", "2026-03-02", "--channel", "email")
            self.cmd("kit-response", "--directory", self.root, "--agency", agency, "--date", "2026-03-05",
                     "--kind", "fee_estimate", "--amount", amount)
        self.cmd("kit-response", "--directory", self.root, "--agency", self.a, "--date", "2026-03-06",
                 "--kind", "fee_paid", "--amount", "99.99")
        self.cmd("kit-response", "--directory", self.root, "--agency", self.b, "--date", "2026-03-06", "--kind", "fee_paid")

    def subprocess_cli(self, *extra):
        done = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "kit-fees", "--directory", str(self.root),
                               *map(str, extra)], cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)
        return done.returncode, done.stdout, done.stderr

    def snapshot(self):
        """Bytes, mode and mtime of every file plus every directory (atime excluded)."""
        seen = {}
        for path in sorted(self.tmp.rglob("*")):
            info = path.lstat()
            try:
                data = None if path.is_dir() else path.read_bytes()
            except PermissionError:  # the mode-000 fixture: mode and mtime still compared
                data = None
            seen[str(path)] = (path.is_dir(), data, info.st_mode, info.st_mtime_ns)
        return seen

    def assert_refusal(self, code, out, err):
        self.assertEqual((code, out, err), (1, "", STOPPED))
        for marker in (str(self.tmp), "Errno", "Permission", "Traceback", "SYNTHETIC", "Fix:"):
            self.assertNotIn(marker, err)

    def assert_value_error(self, call):
        with self.assertRaises(ValueError) as caught:
            call()
        self.assertEqual(str(caught.exception), FIXED)
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)
        self.assertEqual(caught.exception.args, (FIXED,))

    def test_real_sent_permission_refusal_api_and_actual_cli(self):
        self.populate()
        expected = kit_fees.fees(self.root)
        self.sent.chmod(0)
        try:
            try:
                self.sent.read_bytes()
            except PermissionError:
                pass
            else:
                self.skipTest("runtime can still read mode-000 data")
            before = self.snapshot()
            self.assert_value_error(lambda: kit_fees.fees(self.root))
            self.assert_refusal(*self.subprocess_cli())
            self.assert_refusal(*self.subprocess_cli("--csv", self.private / "out.csv"))
            self.assertFalse((self.private / "out.csv").exists())
            self.assertEqual(self.snapshot(), before)
        finally:
            self.sent.chmod(0o600)
        self.assertEqual(kit_fees.fees(self.root), expected)

    def test_status_read_oserror_matrix_is_value_free(self):
        self.populate()
        before = self.snapshot()
        for cls, code in ((PermissionError, errno.EACCES), (OSError, errno.EIO), (FileNotFoundError, errno.ENOENT),
                          (IsADirectoryError, errno.EISDIR)):
            with self.subTest(cls=cls.__name__), \
                    mock.patch.object(kit_track, "status", side_effect=oserror(cls, code)) as status, \
                    mock.patch.object(kit_track, "read_sent") as read_sent:
                self.assert_value_error(lambda: kit_fees.fees(self.root))
                status.assert_called_once_with(self.root, None, None)
                read_sent.assert_not_called()
        self.assertEqual(self.snapshot(), before)

    def test_second_read_oserror_after_real_status_is_contained(self):
        self.populate()
        calls = []

        def second_fails(root):
            calls.append(root)
            if len(calls) == 2:
                raise oserror()
            return REAL_READ_SENT(root)

        before = self.snapshot()
        with mock.patch.object(kit_track, "read_sent", side_effect=second_fails):
            self.assert_value_error(lambda: kit_fees.fees(self.root))
        self.assertEqual(len(calls), 2)
        for extra in ((), ("--csv", self.private / "out.csv")):
            calls.clear()
            with mock.patch.object(kit_track, "read_sent", side_effect=second_fails):
                self.assert_refusal(*run("kit-fees", "--directory", self.root, *extra))
            self.assertEqual(len(calls), 2)
        self.assertFalse((self.private / "out.csv").exists())
        self.assertEqual(self.snapshot(), before)

    def test_missing_is_empty_but_post_admission_disappearance_refuses(self):
        self.assertFalse(self.sent.exists())
        report = kit_fees.fees(self.root)
        self.assertEqual((report["schema"], report["requests"]), ("kit-fees-v1", []))
        self.assertEqual(report["totals"]["quoted"], "$0.00")
        self.assertIn("No fee estimates recorded.", report["note"])
        self.assertFalse(self.sent.exists())
        gone = FileNotFoundError(errno.ENOENT, "No such file", str(self.sent))
        with mock.patch.object(kit_track, "read_sent", side_effect=gone) as read_sent:
            self.assert_value_error(lambda: kit_fees.fees(self.root))
            self.assertEqual(read_sent.call_count, 1)  # no retry
        self.assertFalse(self.sent.exists())

    def test_existing_valueerrors_and_non_oserrors_keep_identity(self):
        self.sent.write_text("{not json", encoding="utf-8")
        with self.assertRaises(ValueError) as caught:
            kit_fees.fees(self.root)
        self.assertIn("kit/sent.json unreadable", str(caught.exception))
        self.assertNotEqual(str(caught.exception), FIXED)
        code, out, err = self.subprocess_cli()
        self.assertEqual((code, out), (1, ""))
        self.assertIn("kit/sent.json unreadable: not JSON", err)
        self.sent.unlink()
        for seam in ("status", "read_sent"):
            for make in (lambda: ValueError("kept"), lambda: RuntimeError("kept"), lambda: RecursionError("kept"),
                         lambda: KeyboardInterrupt()):
                raised = make()
                patches = [mock.patch.object(kit_track, seam, side_effect=raised)]
                if seam == "read_sent":  # let the real status pass: only fees' own read raises
                    calls = []

                    def second(root, raised=raised, calls=calls):
                        calls.append(root)
                        if len(calls) == 2:
                            raise raised
                        return REAL_READ_SENT(root)
                    patches = [mock.patch.object(kit_track, "read_sent", side_effect=second)]
                with self.subTest(seam=seam, kind=type(raised).__name__), patches[0]:
                    with self.assertRaises(type(raised)) as caught:
                        kit_fees.fees(self.root)
                    self.assertIs(caught.exception, raised)

    def test_aggregation_and_csv_errors_are_outside_read_translation(self):
        self.populate()
        late = oserror()
        reads, real_fee = [], kit_track._fee

        def fee_after_reads(steps):  # status uses _fee too: fail only once fees' own second read is done
            if len(reads) >= 2:
                raise late
            return real_fee(steps)

        def counted(root):
            reads.append(root)
            return REAL_READ_SENT(root)
        with mock.patch.object(kit_track, "read_sent", side_effect=counted), \
                mock.patch.object(kit_track, "_fee", side_effect=fee_after_reads):
            with self.assertRaises(PermissionError) as caught:
                kit_fees.fees(self.root)
        self.assertIs(caught.exception, late)
        self.assertEqual(len(reads), 2)
        report = kit_fees.fees(self.root)
        write_failure = oserror(OSError, errno.EIO)
        with mock.patch.object(kit_export, "write_rows", side_effect=write_failure):
            with self.assertRaises(OSError) as caught:
                kit_fees.write_csv(self.root, self.private / "w.csv", report)
        self.assertIs(caught.exception, write_failure)
        existing = self.private / "exists.csv"
        existing.write_text("keep\n", encoding="utf-8")
        code, out, err = run("kit-fees", "--directory", self.root, "--csv", existing)
        self.assertEqual(code, 1)
        self.assertNotIn(FIXED, err)
        self.assertEqual(existing.read_text(encoding="utf-8"), "keep\n")

    def test_repeated_refusal_and_recovery_are_read_only(self):
        self.populate()
        expected = kit_fees.fees(self.root)
        self.sent.chmod(0)
        try:
            try:
                self.sent.read_bytes()
            except PermissionError:
                pass
            else:
                self.skipTest("runtime can still read mode-000 data")
            before = self.snapshot()
            for _ in range(2):
                self.assert_value_error(lambda: kit_fees.fees(self.root))
                self.assert_refusal(*self.subprocess_cli("--csv", self.private / "out.csv"))
                self.assertEqual(self.snapshot(), before)
            self.assertEqual(self.canary.read_text(encoding="utf-8"), "keep me\n")
            self.assertEqual(os.stat(self.sent).st_mode & 0o777, 0)  # production never repairs the mode
        finally:
            self.sent.chmod(0o600)
        self.assertEqual(kit_fees.fees(self.root), expected)

    def test_healthy_report_and_csv_complete_bytes_unchanged(self):
        self.assertEqual(kit_fees.fees(self.root)["totals"]["counts"]["requests_with_fee"], 0)
        self.cmd("kit-sent", "--directory", self.root, "--agency", self.a, "--date", "2026-03-02", "--channel", "email")
        self.cmd("kit-response", "--directory", self.root, "--agency", self.a, "--date", "2026-03-05",
                 "--kind", "fee_estimate", "--amount", "50")
        self.cmd("kit-response", "--directory", self.root, "--agency", self.a, "--date", "2026-03-06",
                 "--kind", "fee_estimate", "--amount", "100.05")
        self.cmd("kit-response", "--directory", self.root, "--agency", self.a, "--date", "2026-03-07",
                 "--kind", "fee_paid", "--amount", "99.99")
        for agency, amount in ((self.b, "40"), (self.c, "0.10")):
            self.cmd("kit-sent", "--directory", self.root, "--agency", agency, "--date", "2026-03-02", "--channel", "email")
            self.cmd("kit-response", "--directory", self.root, "--agency", agency, "--date", "2026-03-05",
                     "--kind", "fee_estimate", "--amount", amount)
        self.cmd("kit-response", "--directory", self.root, "--agency", self.b, "--date", "2026-03-06", "--kind", "fee_paid")
        names = {self.a: "Arroyo Grande Police Department", self.b: "Atascadero Police Department",
                 self.c: "Grover Beach Police Department"}

        def row(agency, estimate, state, paid):
            return {"agency_id": agency, "request": "main", "name": names[agency], "reference": None,
                    "fee_estimate": estimate, "fee_state": state, "fee_paid": paid, "over_fee_cap": None}
        counts = {"requests_with_fee": 3, "unpaid": 1, "paid": 2, "waived": 0, "over_cap": 0}
        totals = {"quoted": "$140.15", "paid": "$99.99", "unpaid": "$0.10", "counts": counts}
        report = kit_fees.fees(self.root)
        self.assertEqual(list(report), ["schema", "requests", "totals", "note"])
        self.assertEqual(report["requests"], [row(self.c, "$0.10", "unpaid", None),
                                              row(self.a, "$100.05", "paid", "$99.99"),
                                              row(self.b, "$40.00", "paid", None)])
        self.assertEqual(report["totals"], totals)
        self.assertEqual(report["note"], kit_fees.NOTE)
        out = self.private / "fees.csv"
        summary = kit_fees.write_csv(self.root, out, report)
        self.assertEqual(summary["totals"], totals)
        header = ",".join(kit_fees.HEADER)
        self.assertEqual(out.read_bytes(), (
            f"{header}\r\n{self.c},main,{names[self.c]},,$0.10,unpaid,,\r\n"
            f"{self.a},main,{names[self.a]},,$100.05,paid,$99.99,\r\n"
            f"{self.b},main,{names[self.b]},,$40.00,paid,,\r\nTOTAL,,unpaid $0.10,,$140.15,,$99.99,\r\n").encode())
        self.assertEqual(os.stat(out).st_mode & 0o777, 0o600)
        code, stdout, err = run("kit-fees", "--directory", self.root)
        self.assertEqual((code, json.loads(stdout), err), (0, report, "Read-only. Nothing was sent.\n"))


if __name__ == "__main__":
    unittest.main()
