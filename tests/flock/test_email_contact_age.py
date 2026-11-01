"""flock_email --max-age-days: check names contacts checked more than N days ago as stale; draft refuses them (DF1537-08)."""
import shutil, subprocess, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOL, FIXTURES = ROOT / "tools" / "flock_email.py", ROOT / "tests" / "fixtures" / "flock"
HEADER = "agency,role,email,source_url,source_sha256,checked_at,added_by"
TAIL = " | draft only; G approval required before any send"
LOCAL = "zz-local-part"
TODAY = "2026-10-31"


def row(agency, checked, email=f"{LOCAL}@example.org"):
    return f"{agency},records,{email},https://www.example.org/records,{'c' * 64},{checked},sample"


def run(*argv):
    return subprocess.run([sys.executable, "-B", str(TOOL), *map(str, argv)], capture_output=True, text=True)


class ContactAgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = self.tmp / "store"
        (self.store / "agencies").mkdir(parents=True)

    def agency(self, slug, checked):
        folder = self.store / "agencies" / slug
        folder.mkdir()
        (folder / "CONTACT.csv").write_text(f"{HEADER}\n{row(slug.replace('-', ' ').title(), checked)}\n", encoding="utf-8")
        return folder

    def rundir(self, slug, checked):
        run_dir = self.agency(slug, checked) / "runs" / "2026-10-31"
        run_dir.mkdir(parents=True)
        shutil.copy(FIXTURES / "followup-draft-sample.md", run_dir / "FOLLOWUP-DRAFT.md")
        return run_dir

    def assertNoLeak(self, done):
        self.assertNotIn(LOCAL, done.stdout + done.stderr)

    def test_boundary_n_days_is_ready_and_n_plus_one_is_stale_with_day_count(self):
        self.agency("a-boundary", "2026-10-01")  # exactly 30 days before TODAY
        self.agency("b-over", "2026-09-30")      # 31 days
        done = run("check", self.store, "--max-age-days", 30, "--today", TODAY)
        self.assertEqual((done.returncode, done.stderr), (1, ""))
        self.assertEqual(done.stdout.splitlines(), [
            "contact | a-boundary | ready | domain=example.org",
            "contact | b-over | stale | checked 2026-09-30, 31 days old",
            f"contact | agencies=2 ready=1 not_ready=0 stale=1{TAIL}"])
        self.assertNoLeak(done)

    def test_all_fresh_exits_0_with_stale_zero(self):
        self.agency("a-fresh", "2026-10-30")
        done = run("check", self.store, "--today", TODAY, "--max-age-days", 90)
        self.assertEqual((done.returncode, done.stdout.splitlines()[-1]), (0, f"contact | agencies=1 ready=1 not_ready=0 stale=0{TAIL}"))

    def test_future_checked_at_is_named_for_check_and_draft(self):
        run_dir = self.rundir("a-future", "2026-11-01")
        done = run("check", self.store, "--max-age-days", 30, "--today", TODAY)
        self.assertEqual(done.returncode, 1)
        self.assertEqual(done.stdout.splitlines()[0], "contact | a-future | not ready | contact: checked_at is in the future")
        drafts = self.tmp / "drafts"
        done = run("draft", run_dir, "--drafts", drafts, "--max-age-days", 30, "--today", TODAY)
        self.assertEqual((done.returncode, done.stdout, done.stderr), (1, "", "contact: checked_at is in the future\n"))
        self.assertEqual([(run_dir / "EMAIL-DRAFT.md").exists(), drafts.exists()], [False, False])

    def test_fresh_stale_and_invalid_store_three_lines_exit_1(self):
        self.agency("a-fresh", "2026-10-20")
        self.agency("b-stale", "2026-01-01")
        bad = self.store / "agencies" / "c-invalid"
        bad.mkdir()
        (bad / "CONTACT.csv").write_text(f"{HEADER}\n{row('C Invalid', '2026-10-20', 'not-an-address')}\n", encoding="utf-8")
        done = run("check", self.store, "--max-age-days", 90, "--today", TODAY)
        self.assertEqual(done.returncode, 1)
        self.assertEqual(done.stdout.splitlines(), [
            "contact | a-fresh | ready | domain=example.org",
            "contact | b-stale | stale | checked 2026-01-01, 303 days old",
            "contact | c-invalid | not ready | row 2: email is invalid",
            f"contact | agencies=3 ready=1 not_ready=1 stale=1{TAIL}"])
        self.assertNoLeak(done)

    def test_impossible_checked_at_date_is_not_ready_with_flag(self):
        self.agency("a-bad-date", "2026-02-30")
        done = run("check", self.store, "--max-age-days", 30, "--today", TODAY)
        self.assertEqual(done.stdout.splitlines()[0], "contact | a-bad-date | not ready | contact: checked_at is not a real date")

    def test_draft_refuses_stale_contact_and_writes_nothing(self):
        run_dir, drafts = self.rundir("a-stale", "2026-09-30"), self.tmp / "drafts"
        before = sorted(p.name for p in run_dir.iterdir())
        done = run("draft", run_dir, "--drafts", drafts, "--max-age-days", 30, "--today", TODAY)
        self.assertEqual((done.returncode, done.stdout), (1, ""))
        self.assertEqual(done.stderr, "contact: checked_at 2026-09-30 is 31 days old (max 30)\n")
        self.assertEqual(sorted(p.name for p in run_dir.iterdir()), before)
        self.assertFalse(drafts.exists())
        self.assertNoLeak(done)

    def test_draft_fresh_contact_output_identical_with_and_without_flag(self):
        outputs = []
        for slug, extra in (("a-plain", ()), ("b-flag", ("--max-age-days", 30, "--today", TODAY))):
            run_dir, drafts = self.rundir(slug, "2026-10-01"), self.tmp / f"drafts-{slug}"
            done = run("draft", run_dir, "--drafts", drafts, *extra)
            self.assertEqual((done.returncode, done.stdout, done.stderr), (0, "written=2 to_domain=example.org\n", ""))
            outputs.append([(run_dir / "EMAIL-DRAFT.md").read_bytes(), (drafts / f"{slug}-2026-10-31-EMAIL-DRAFT.md").read_bytes()])
        self.assertEqual(outputs[0], outputs[1])

    def test_without_flags_a_stale_contact_is_ready_as_on_main(self):
        run_dir = self.rundir("a-old", "2020-01-01")
        done = run("check", self.store)
        self.assertEqual((done.returncode, done.stdout), (0, "contact | a-old | ready | domain=example.org\n"
                                                             f"contact | agencies=1 ready=1 not_ready=0{TAIL}\n"))
        done = run("draft", run_dir, "--drafts", self.tmp / "drafts")
        self.assertEqual((done.returncode, done.stdout), (0, "written=2 to_domain=example.org\n"))

    def test_bad_flag_values_are_usage_exit_2(self):
        run_dir = self.rundir("a-fresh", "2026-10-30")
        cases = [("--max-age-days", "0"), ("--max-age-days", "-1"), ("--max-age-days", "abc"), ("--max-age-days", "3651"),
                 ("--max-age-days", "+5"), ("--max-age-days", "30", "--today", "2026-13-01"),
                 ("--max-age-days", "30", "--today", "2026-02-30"), ("--max-age-days", "30", "--today", "20261031"),
                 ("--today", TODAY), ("--max-age-days", "30", "--max-age-days", "40"), ("--max-age-days",),
                 ("--max-age-days", "30", "--other", "1"), ("--max-age-days", "30", "extra")]
        for extra in cases:
            for argv in (("check", self.store, *extra), ("draft", run_dir, "--drafts", self.tmp / "drafts", *extra)):
                with self.subTest(argv=argv[0], extra=extra):
                    done = run(*argv)
                    self.assertEqual((done.returncode, done.stdout), (2, ""))
                    self.assertTrue(done.stderr.startswith("usage: flock_email.py"))
        self.assertFalse((self.tmp / "drafts").exists())
        self.assertEqual(run("status", self.store, "--max-age-days", "30").returncode, 2)  # status is unchanged

    def test_max_bounds_1_and_3650_are_accepted(self):
        self.agency("a-today", TODAY)
        for n in ("1", "3650"):
            self.assertEqual(run("check", self.store, "--max-age-days", n, "--today", TODAY).returncode, 0)

    def test_usage_lists_the_optional_flags(self):
        done = run("--help")
        self.assertEqual(done.returncode, 0)
        self.assertIn("check and draft also take [--max-age-days N [--today YYYY-MM-DD]]", done.stdout)


if __name__ == "__main__":
    unittest.main()
