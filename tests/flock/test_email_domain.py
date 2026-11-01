"""flock_email check/draft warn when the records address domain is not the source page's site; totals mismatch=N (RF1720-6)."""
import importlib.util, os, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOL, FIXTURES = ROOT / "tools" / "flock_email.py", ROOT / "tests" / "fixtures" / "flock"
HEADER = "agency,role,email,source_url,source_sha256,checked_at,added_by"
TAIL = " | draft only; G approval required before any send"
LOCAL = "zz-sentinel-local"
AGENCY_URL = "https://www.city.example.gov/records"
WARN = " | warn: domain differs from source host "


def row(agency, domain, url=AGENCY_URL, local=LOCAL, checked="2026-10-30"):
    return f"{agency},records,{local}@{domain},{url},{'c' * 64},{checked},sample"


def run(*argv):
    return subprocess.run([sys.executable, "-B", str(TOOL), *map(str, argv)], capture_output=True, text=True)


def load():
    spec = importlib.util.spec_from_file_location("_email_rf1720_6", TOOL)
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    return tool


class SameSiteTests(unittest.TestCase):
    def test_rule_www_dot_suffix_case_and_lookalike(self):
        tool = load()
        cases = [("example.gov", "example.gov", True), ("example.gov", "www.example.gov", True),
                 ("www.example.gov", "example.gov", True), ("city.example.gov", "www.police.city.example.gov", True),
                 ("records.city.example.gov", "city.example.gov", True), ("City.Example.GOV", "WWW.CITY.EXAMPLE.gov", True),
                 ("city.example.org", "badcity.example.org", False), ("badcity.example.org", "city.example.org", False),
                 ("freemail.example.org", "www.city.example.gov", False), ("example.gov", "(none)", False)]
        for domain, page, want in cases:
            with self.subTest(domain=domain, page=page):
                self.assertIs(tool.same_site(domain, page), want)

    def test_host_drops_userinfo_and_port_and_lowercases(self):
        tool = load()
        for url, want in (("https://WWW.Example.GOV/a?b#c", "www.example.gov"), (f"https://{LOCAL}@www.example.org:8443/x", "www.example.org"),
                          ("https://example.org", "example.org"), ("https:///path", "(none)"), (None, "(none)")):
            with self.subTest(url=url):
                self.assertEqual(tool.host(url), want)


class CheckDomainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = self.tmp / "store"
        (self.store / "agencies").mkdir(parents=True)

    def agency(self, slug, *rows):
        folder = self.store / "agencies" / slug
        folder.mkdir()
        (folder / "CONTACT.csv").write_text("\n".join([HEADER, *rows]) + "\n", encoding="utf-8")
        return folder

    def check(self, *extra):
        done = run("check", self.store, *extra)
        self.assertNotIn(LOCAL, done.stdout + done.stderr)
        return done

    def test_same_host_www_and_subdomains_give_no_warning_and_mismatch_0(self):
        self.agency("a-same", row("A Same", "city.example.gov", "https://city.example.gov/records"))
        self.agency("b-www", row("B Www", "city.example.gov"))
        self.agency("c-sub-host", row("C Sub Host", "city.example.gov", "https://www.police.city.example.gov/x"))
        self.agency("d-sub-mail", row("D Sub Mail", "records.city.example.gov"))
        done = self.check()
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        self.assertNotIn("warn", done.stdout)
        self.assertEqual(done.stdout.splitlines()[-1], f"contact | agencies=4 ready=4 not_ready=0 mismatch=0{TAIL}")

    def test_free_mail_address_warns_naming_host_but_stays_ready_exit_0(self):
        self.agency("a-free", row("A Free", "freemail.example.org"))
        done = self.check()
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        self.assertEqual(done.stdout.splitlines(), [f"contact | a-free | ready | domain=freemail.example.org{WARN}www.city.example.gov",
                                                    f"contact | agencies=1 ready=1 not_ready=0 mismatch=1{TAIL}"])

    def test_lookalike_suffix_warns_and_userinfo_in_source_url_never_printed(self):
        self.agency("a-look", row("A Look", "city.example.org", "https://badcity.example.org/records"))
        self.agency("b-info", row("B Info", "city.example.org", f"https://{LOCAL}@www.other.example.org:8443/x"))
        done = self.check()
        self.assertEqual(done.stdout.splitlines(), [f"contact | a-look | ready | domain=city.example.org{WARN}badcity.example.org",
                                                    f"contact | b-info | ready | domain=city.example.org{WARN}www.other.example.org",
                                                    f"contact | agencies=2 ready=2 not_ready=0 mismatch=2{TAIL}"])

    def test_upper_case_domain_and_host_compare_case_insensitively(self):
        self.agency("a-upper", row("A Upper", "City.Example.GOV", "https://WWW.CITY.EXAMPLE.gov/Records"))
        self.agency("b-upper-odd", row("B Upper Odd", "FREEMAIL.example.org", "https://WWW.CITY.EXAMPLE.gov/Records"))
        done = self.check()
        self.assertEqual(done.stdout.splitlines(), ["contact | a-upper | ready | domain=City.Example.GOV",
                                                    f"contact | b-upper-odd | ready | domain=FREEMAIL.example.org{WARN}www.city.example.gov",
                                                    f"contact | agencies=2 ready=2 not_ready=0 mismatch=1{TAIL}"])

    def test_not_ready_and_stale_agencies_get_no_warning_and_are_not_counted(self):
        self.agency("a-bad", row("A Bad", "freemail.example.org", "http://www.city.example.gov/"))  # source_url not https
        self.agency("b-two", row("B Two", "freemail.example.org"), row("B Two", "other.example.org"))
        self.agency("c-stale", row("C Stale", "freemail.example.org", checked="2026-01-01"))
        done = self.check("--max-age-days", 30, "--today", "2026-10-31")
        self.assertEqual(done.returncode, 1)
        self.assertNotIn("warn", done.stdout)
        self.assertEqual(done.stdout.splitlines(), ["contact | a-bad | not ready | row 2: source_url is invalid",
                                                    "contact | b-two | not ready | contact: more than one records row",
                                                    "contact | c-stale | stale | checked 2026-01-01, 303 days old",
                                                    f"contact | agencies=3 ready=0 not_ready=2 stale=1 mismatch=0{TAIL}"])

    def test_symlinked_agency_folder_still_link_refused_and_not_counted(self):
        target = self.tmp / "elsewhere" / "a-link"
        target.mkdir(parents=True)
        (target / "CONTACT.csv").write_text(f"{HEADER}\n{row('A Link', 'freemail.example.org')}\n", encoding="utf-8")
        os.symlink(target, self.store / "agencies" / "a-link")
        done = self.check()
        self.assertEqual(done.returncode, 1)
        self.assertEqual(done.stdout.splitlines(), ["contact | a-link | not ready | link refused",
                                                    f"contact | agencies=1 ready=0 not_ready=1 mismatch=0{TAIL}"])

    def test_two_agencies_one_mismatched_totals(self):
        self.agency("a-good", row("A Good", "city.example.gov"))
        self.agency("b-odd", row("B Odd", "freemail.example.org"))
        done = self.check()
        self.assertEqual((done.returncode, done.stdout.splitlines()[0]), (0, "contact | a-good | ready | domain=city.example.gov"))
        self.assertEqual(done.stdout.splitlines()[-1], f"contact | agencies=2 ready=2 not_ready=0 mismatch=1{TAIL}")


class DraftDomainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def rundir(self, slug, contact_row):
        agency = self.tmp / "store" / "agencies" / slug
        run_dir = agency / "runs" / "2026-10-31"
        run_dir.mkdir(parents=True)
        shutil.copy(FIXTURES / "followup-draft-sample.md", run_dir / "FOLLOWUP-DRAFT.md")
        (agency / "CONTACT.csv").write_text(f"{HEADER}\n{contact_row}\n", encoding="utf-8")
        return run_dir

    def test_mismatched_contact_warns_on_stderr_and_still_writes_both_files(self):
        run_dir, drafts = self.rundir("a-odd", row("A Odd", "freemail.example.org")), self.tmp / "drafts"
        done = run("draft", run_dir, "--drafts", drafts)
        self.assertEqual((done.returncode, done.stdout, done.stderr),
                         (0, "written=2 to_domain=freemail.example.org\n", "draft: warn: domain differs from source host www.city.example.gov\n"))
        self.assertNotIn(LOCAL, done.stdout + done.stderr)
        first, second = run_dir / "EMAIL-DRAFT.md", drafts / "a-odd-2026-10-31-EMAIL-DRAFT.md"
        self.assertEqual(first.read_bytes(), second.read_bytes())
        again = run("draft", run_dir, "--drafts", drafts)  # a refused draft prints only its refusal
        self.assertEqual((again.returncode, again.stdout, again.stderr), (1, "", "draft: EMAIL-DRAFT already exists\n"))

    def test_same_site_contact_drafts_without_warning(self):
        run_dir = self.rundir("a-good", row("A Good", "city.example.gov"))
        done = run("draft", run_dir, "--drafts", self.tmp / "drafts")
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, "written=2 to_domain=city.example.gov\n", ""))

    def test_usage_mentions_the_warning(self):
        self.assertIn("warn (never refuse) when the address domain is not the source_url host's site", run("--help").stdout)


if __name__ == "__main__":
    unittest.main()
