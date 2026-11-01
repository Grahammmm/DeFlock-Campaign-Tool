"""kit-verify-agency records an organizer-checked contact with its official source and date. Offline, synthetic."""
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import kit  # noqa: E402

POLICE, SHERIFF = "ca-example-police", "ca-example-sheriff"
EMAIL, SOURCE = "records@police.example", "https://police.example/records"
FICTIONAL = [{"agency_id": POLICE, "name": "Example Police Department", "kind": "police", "selected": True,
              "jurisdiction_name": "Example City", "verified": False},
             {"agency_id": SHERIFF, "name": "Example Sheriff", "kind": "sheriff", "selected": True,
              "portal": {"vendor": "nextrequest", "url": None}, "verified": False}]


def cli(*arguments):
    return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *map(str, arguments)],
                          cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)


class VerifyAgencyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-kit-verify-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        result = cli("init", "--directory", self.root, "--name", "Synthetic", "--county", "San Luis Obispo",
                     "--state", "CA")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.kit = self.root / "kit"
        self.agencies_path = self.kit / "agencies.json"
        self.build()
        self.agencies_path.write_text(json.dumps(self.agencies() + FICTIONAL, indent=2) + "\n")
        self.build()

    def build(self):
        with contextlib.redirect_stdout(io.StringIO()):
            kit.build_kit(self.root)

    def agencies(self):
        return json.loads(self.agencies_path.read_text())

    def entry(self, agency_id):
        return next(a for a in self.agencies() if a["agency_id"] == agency_id)

    def verify(self, agency_id=POLICE, *extra, source=SOURCE, checked="2026-10-01"):
        return cli("kit-verify-agency", "--directory", self.root, "--agency-id", agency_id,
                   "--source-url", source, "--checked-on", checked, *extra)

    def assert_refused(self, result, *words):
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertTrue(result.stderr.startswith("Stopped: "), result.stderr)
        for word in words:
            self.assertIn(word, result.stderr)
        self.assertNotIn("Nothing was sent", result.stdout)

    @contextlib.contextmanager
    def unchanged(self):
        before = (self.agencies_path.read_bytes(), self.agencies_path.stat().st_mtime_ns)
        yield
        self.assertEqual((self.agencies_path.read_bytes(), self.agencies_path.stat().st_mtime_ns), before)
        self.assertEqual(list(self.kit.rglob(".*.tmp")), [])

    def test_records_email_recorded_others_unchanged(self):
        others = [a for a in self.agencies() if a["agency_id"] != POLICE]
        result = self.verify(POLICE, "--records-email", EMAIL)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout.rsplit("Nothing was sent.", 1)[0]),
                         {"agency_id": POLICE, "fields": ["records_email"], "checked_on": "2026-10-01",
                          "verified": True})
        self.assertTrue(result.stdout.rstrip().endswith("Nothing was sent."))
        entry = self.entry(POLICE)
        self.assertEqual(entry["records_email"], EMAIL)
        self.assertIs(entry["verified"], True)
        self.assertEqual(entry["verification"],
                         {"source_url": SOURCE, "checked_on": "2026-10-01", "fields": ["records_email"]})
        self.assertEqual([a for a in self.agencies() if a["agency_id"] != POLICE], others)

    def test_second_call_updates_only_named_fields(self):
        self.assertEqual(self.verify(POLICE, "--records-email", EMAIL).returncode, 0)
        result = self.verify(POLICE, "--portal", "https://police.example/portal",
                             source="https://police.example/portal-info", checked="2026-10-15")
        self.assertEqual(result.returncode, 0, result.stderr)
        entry = self.entry(POLICE)
        self.assertEqual(entry["records_email"], EMAIL)
        self.assertEqual(entry["portal"], {"vendor": "unknown", "url": "https://police.example/portal"})
        self.assertEqual(entry["verification"], {"source_url": "https://police.example/portal-info",
                                                 "checked_on": "2026-10-15", "fields": ["portal"]})
        # an existing portal vendor is kept; several fields in one call are listed sorted
        result = self.verify(SHERIFF, "--portal", "https://sheriff.example/p", "--records-url",
                             "https://sheriff.example/records", "--records-email", "pra@sheriff.example")
        self.assertEqual(result.returncode, 0, result.stderr)
        entry = self.entry(SHERIFF)
        self.assertEqual(entry["portal"], {"vendor": "nextrequest", "url": "https://sheriff.example/p"})
        self.assertEqual(entry["verification"]["fields"], ["portal", "records_email", "records_url"])

    def test_kit_rerun_keeps_entry_and_renders_email(self):
        draft = self.kit / "requests" / (SHERIFF + ".md")
        draft.write_bytes(draft.read_bytes() + b"Fee cap: 50\n")
        edited = draft.read_bytes()
        self.assertEqual(self.verify(POLICE, "--records-email", EMAIL).returncode, 0)
        self.assertEqual(self.verify(SHERIFF, "--records-email", "pra@sheriff.example").returncode, 0)
        verified = self.entry(POLICE)
        self.build()
        self.assertEqual(self.entry(POLICE), verified)
        self.assertIn(f"To: {EMAIL}\n", (self.kit / "requests" / (POLICE + ".md")).read_text())
        self.assertEqual(draft.read_bytes(), edited)
        self.assertIn("To: pra@sheriff.example\n",
                      (self.kit / "requests.suggested" / (SHERIFF + ".md")).read_text())

    def test_missing_agencies_json_refused_nothing_created(self):
        bare = self.root.parent / "bare"
        self.assertEqual(cli("init", "--directory", bare, "--name", "Bare", "--county", "San Luis Obispo",
                             "--state", "CA").returncode, 0)
        before = sorted(p.relative_to(bare) for p in bare.rglob("*"))
        result = cli("kit-verify-agency", "--directory", bare, "--agency-id", POLICE, "--source-url", SOURCE,
                     "--checked-on", "2026-10-01", "--records-email", EMAIL)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, "Stopped: run kit first: kit/agencies.json is missing\n")
        self.assertEqual(sorted(p.relative_to(bare) for p in bare.rglob("*")), before)

    def test_unknown_duplicate_and_no_contact_refused(self):
        with self.unchanged():
            self.assert_refused(self.verify("ca-example-nowhere", "--records-email", EMAIL), "ca-example-nowhere")
            self.assert_refused(self.verify(POLICE), "records_email", "records_url", "portal")
        self.agencies_path.write_text(json.dumps(self.agencies() + FICTIONAL[:1], indent=2) + "\n")
        with self.unchanged():
            self.assert_refused(self.verify(POLICE, "--records-email", EMAIL), POLICE, "exactly once")

    def test_bad_urls_and_email_refused(self):
        cases = [((), "http://police.example/records", "source_url"),
                 ((), "https://", "source_url"),
                 ((), "https://police.example/a b", "source_url"),
                 (("--portal", "ftp://police.example/portal"), SOURCE, "portal"),
                 (("--records-url", "http://police.example/records"), SOURCE, "records_url"),
                 (("--records-url", "javascript:alert(1)"), SOURCE, "records_url"),
                 (("--records-email", "not-an-email"), SOURCE, "records_email"),
                 (("--records-email", "Records <records@police.example>"), SOURCE, "records_email"),
                 (("--records-email", "records@police.example\nBcc: x@y.example"), SOURCE, "records_email")]
        with self.unchanged():
            for extra, source, field in cases:
                contact = extra or ("--records-email", EMAIL)
                with self.subTest(field=field, value=extra or source):
                    self.assert_refused(self.verify(POLICE, *contact, source=source), field)

    def test_bad_and_future_dates_refused(self):
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        with self.unchanged():
            for checked in ("2026-13-40", "2026-1-5", "20261001", tomorrow):
                with self.subTest(checked=checked):
                    self.assert_refused(self.verify(POLICE, "--records-email", EMAIL, checked=checked), "checked_on")
            with self.assertRaisesRegex(ValueError, "after today"):
                kit.verify_agency(self.root, POLICE, source_url=SOURCE, checked_on="2026-10-02",
                                  records_email=EMAIL, today=date(2026, 10, 1))
        kit.verify_agency(self.root, POLICE, source_url=SOURCE, checked_on="2026-10-01", records_email=EMAIL,
                          today=date(2026, 10, 1))
        self.assertEqual(self.entry(POLICE)["verification"]["checked_on"], "2026-10-01")

    def test_crash_before_replace_keeps_original_and_never_fetches(self):
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("network")) as urlopen:
            with self.unchanged(), mock.patch("campaign_tool.kit.os.replace", side_effect=OSError("disk full")):
                with self.assertRaisesRegex(OSError, "disk full"):
                    kit.verify_agency(self.root, POLICE, source_url=SOURCE, checked_on="2026-10-01",
                                      records_email=EMAIL)
            kit.verify_agency(self.root, POLICE, source_url=SOURCE, checked_on="2026-10-01", records_url=SOURCE)
        urlopen.assert_not_called()
        self.assertEqual(self.entry(POLICE)["records_url"], SOURCE)
        self.assertNotIn("records_email", self.entry(POLICE))


if __name__ == "__main__":
    unittest.main()
