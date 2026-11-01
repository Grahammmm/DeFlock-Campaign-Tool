"""flock_requests --check STORE: a run whose email is recorded sent needs a request with that sent date (NB-DF-05)."""
import shutil, subprocess, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOL, FIXTURE = ROOT / "tools" / "flock_requests.py", ROOT / "tests" / "fixtures" / "flock" / "requests-sample.csv"
HEADER = "request_id,agency,sent,receipt_evidence,receipt_date,determination_date,extension_notice_date,status,source_sha256\n"
HEAD = "DRAFT - not sent - G approval required"
NAME, ADDR, SUBJ = "Opname Zedward", "planted-local@city.example", "Subject: planted subject words"
LEAKS = (NAME, "Opname", ADDR, "planted-local", "city.example", SUBJ, "planted subject", "To:", "@")
A, B = "sample-city", "second-town"


def run(*argv):
    return subprocess.run([sys.executable, "-B", str(TOOL), *map(str, argv)], capture_output=True, text=True)


def snapshot(base):
    return sorted((str(p.relative_to(base)), p.lstat().st_mtime_ns) for p in Path(base).rglob("*"))


class SentRunLinkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = self.tmp / "store"

    def requests(self, slug, *sent):
        folder = self.store / "agencies" / slug
        folder.mkdir(parents=True, exist_ok=True)
        label = slug.replace("-", " ").title()
        rows = "".join(f"R-{n},{label},{d},,,,,open,\n" for n, d in enumerate(sent, 1))
        (folder / "REQUESTS.csv").write_text(HEADER + rows, encoding="utf-8")

    def email(self, slug, date, sent="", approve="2026-02-01 ref G", base=None):
        """A run folder holding a synthetic EMAIL-DRAFT.md; sent=None leaves only a FOLLOWUP-DRAFT.md."""
        rundir = (base or self.store / "agencies" / slug / "runs") / date
        rundir.mkdir(parents=True, exist_ok=True)
        (rundir / "FOLLOWUP-DRAFT.md").write_text(HEAD + "\n", encoding="utf-8")
        if sent is not None:
            text = [HEAD, f"To: {ADDR}", SUBJ, "---", "body", "---", f"G APPROVED: {approve or '____'}",
                    f"Sent by: {sent or '____ (operator, by hand, after G' + chr(39) + 's reply)'}"]
            (rundir / "EMAIL-DRAFT.md").write_text("\n".join(text) + "\n", encoding="utf-8")
        return rundir

    def check(self, path=None):
        done = run("--check", path or self.store)
        for leaked in LEAKS:
            self.assertNotIn(leaked, done.stdout + done.stderr)
        return done.returncode, done.stdout.splitlines()

    def test_sent_run_with_matching_request_passes_and_writes_nothing(self):
        self.requests(A, "2026-02-03")
        self.email(A, "2026-02-03", f"{NAME} 2026-02-03")
        self.email(A, "2026-01-20", f"{NAME} {ADDR} 2026-02-03 by hand")  # sent on a later day than the run
        before = snapshot(self.store)
        self.assertEqual(self.check(), (0, ["requests=1 errors=0 sent_runs=2 matched=2"]))
        self.assertEqual(snapshot(self.store), before)

    def test_store_without_runs_counts_zero(self):
        self.requests(A, "2026-02-03")
        self.assertEqual(self.check(), (0, ["requests=1 errors=0 sent_runs=0 matched=0"]))

    def test_missing_or_unusable_sent_date_is_named(self):
        self.requests(A, "2026-02-03")
        for n, value in enumerate((NAME, f"{NAME} on 3 Feb 2026", f"{NAME} 2026-02-30", f"{NAME} 12026-02-033",
                                   f"{NAME} 2026/02/03"), 1):
            with self.subTest(value=value):
                self.email(A, f"2026-01-0{n}", value)
                code, lines = self.check()
                self.assertEqual(code, 1)
                self.assertIn(f"{A}: run 2026-01-0{n}: sent date missing on the Sent by line", lines)
                self.assertEqual(lines[-1], f"requests=1 errors={n} sent_runs={n} matched=0")

    def test_first_date_token_is_the_sent_date(self):
        self.requests(A, "2026-02-03")
        self.email(A, "2026-02-01", f"{NAME} 2026-02-03 (draft of 2026-02-04)")
        self.assertEqual(self.check(), (0, ["requests=1 errors=0 sent_runs=1 matched=1"]))

    def test_sent_date_before_run_date_is_named(self):
        self.requests(A, "2026-02-03")
        self.email(A, "2026-02-05", f"{NAME} 2026-02-03")
        self.assertEqual(self.check(), (1, [f"{A}: run 2026-02-05: sent date 2026-02-03 is before the run date",
                                            "requests=1 errors=1 sent_runs=1 matched=0"]))

    def test_no_request_with_that_sent_date_is_named(self):
        self.requests(A, "2026-02-02", "2026-02-04")  # one a day off either side
        self.requests(B, "2026-02-03")  # another agency's request never satisfies this one
        self.email(A, "2026-02-01", f"{NAME} 2026-02-03")
        self.assertEqual(self.check(), (1, [f"{A}: run 2026-02-01: no request with sent 2026-02-03",
                                            "requests=3 errors=1 sent_runs=1 matched=0"]))

    def test_agency_without_requests_file_is_named(self):
        self.requests(B, "2026-02-03")
        self.email(A, "2026-02-01", f"{NAME} 2026-02-03")
        code, lines = self.check()
        self.assertEqual((code, lines[-1]), (1, "requests=1 errors=1 sent_runs=1 matched=0"))
        self.assertIn(f"{A}: run 2026-02-01: no request with sent 2026-02-03", lines)

    def test_sent_without_approval_is_named_and_not_counted(self):
        self.requests(A, "2026-02-03")
        for approve in ("", "   "):
            with self.subTest(approve=repr(approve)):
                self.email(A, "2026-02-01", f"{NAME} 2026-02-03", approve=approve)
                self.assertEqual(self.check(), (1, [f"{A}: run 2026-02-01: sent without approval",
                                                    "requests=1 errors=1 sent_runs=0 matched=0"]))

    def test_earlier_stages_and_non_date_folders_are_ignored(self):
        self.requests(A, "2026-02-03")
        self.email(A, "2026-02-01", None)  # follow-up drafted
        self.email(A, "2026-02-02", "")  # email drafted
        self.email(A, "2026-02-04", "", approve="2026-02-04 ref G")  # approval recorded
        runs = self.store / "agencies" / A / "runs"
        for name in ("notes", "2026-02-30", "2026-02-03-old", "2026-2-3"):  # sent drafts no request matches
            self.email(A, name, f"{NAME} 2026-03-09")
        (runs / "2026-03-01").write_text("a file, not a run folder\n", encoding="utf-8")
        self.assertEqual(self.check(), (0, ["requests=1 errors=0 sent_runs=0 matched=0"]))

    def test_errors_add_to_requests_errors(self):
        self.requests(A, "2026-02-03")
        bad = self.store / "agencies" / A / "REQUESTS.csv"
        bad.write_text(bad.read_text(encoding="utf-8") + "R-1,Sample City,2026-02-03,,,,,open,\n", encoding="utf-8")
        self.email(A, "2026-02-01", NAME)
        code, lines = self.check()
        self.assertEqual((code, lines[-1]), (1, "requests=2 errors=2 sent_runs=1 matched=0"))
        self.assertEqual(lines[0], f"{A}: row 3: request_id is empty or duplicates an earlier row")
        self.assertEqual(lines[1], f"{A}: run 2026-02-01: sent date missing on the Sent by line")

    def test_symlinked_agency_runs_and_run_folders_are_never_read(self):
        self.requests(A, "2026-02-03")
        outside = self.tmp / "outside"
        self.email(None, "2026-02-01", f"{NAME} 2026-03-09", base=outside / "agency" / "runs")  # would fail if read
        self.email(None, "2026-02-02", f"{NAME} 2026-03-09", base=outside / "runs")
        (self.store / "agencies" / "linked-town").symlink_to(outside / "agency", target_is_directory=True)
        (self.store / "agencies" / A / "runs").mkdir()
        (self.store / "agencies" / A / "runs" / "2026-02-02").symlink_to(outside / "runs" / "2026-02-02")
        self.requests(B, "2026-02-03")
        (self.store / "agencies" / B / "runs").symlink_to(outside / "agency" / "runs", target_is_directory=True)
        real = self.email(A, "2026-02-05", None)
        (real / "EMAIL-DRAFT.md").symlink_to(outside / "runs" / "2026-02-02" / "EMAIL-DRAFT.md")
        locked = [outside / "agency" / "runs" / "2026-02-01" / "EMAIL-DRAFT.md",
                  outside / "runs" / "2026-02-02" / "EMAIL-DRAFT.md"]
        for path in locked:
            path.chmod(0o000)
            self.addCleanup(path.chmod, 0o600)
        self.assertEqual(self.check(), (0, ["requests=2 errors=0 sent_runs=0 matched=0"]))
        for path in locked:  # readable again and still skipped: the link, not the mode, keeps them out
            path.chmod(0o600)
        self.assertEqual(self.check(), (0, ["requests=2 errors=0 sent_runs=0 matched=0"]))

    def test_file_path_output_is_unchanged(self):
        self.requests(A, "2026-02-02")
        self.email(A, "2026-02-01", f"{NAME} 2026-02-03")  # would fail the store check
        req = self.store / "agencies" / A / "REQUESTS.csv"
        self.assertEqual(run("--check", req).stdout, "requests=1 errors=0\n")
        self.assertEqual(self.check(), (1, [f"{A}: run 2026-02-01: no request with sent 2026-02-03",
                                            "requests=1 errors=1 sent_runs=1 matched=0"]))
        done = run("--check", FIXTURE)
        self.assertEqual((done.returncode, done.stdout), (0, "requests=5 errors=0\n"))


if __name__ == "__main__":
    unittest.main()
