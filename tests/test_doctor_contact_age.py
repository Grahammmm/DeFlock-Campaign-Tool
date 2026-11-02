"""doctor --contact-max-age-days N names selected, verified kit/agencies.json contacts checked more than N days ago.

Offline, synthetic agencies, fixed today. Starter kit only; the flock store CONTACT.csv is out of scope.
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli, kit  # noqa: E402

TODAY = date(2026, 10, 31)
ACTION = "Re-check {} agency contact(s) on their official sites (kit-verify-agency)."


class FixedDate(date):
    @classmethod
    def today(cls):
        return TODAY


def ago(days):
    return (TODAY - timedelta(days=days)).isoformat()


def agency(agency_id, checked_on=None, *, selected=True, verified=True, verification=True):
    entry = {"agency_id": agency_id, "name": agency_id.title(), "kind": "police", "selected": selected,
             "verified": verified}
    if verification:
        entry["verification"] = {"source_url": "https://police.example/records", "fields": ["records_email"]}
        if checked_on is not None:
            entry["verification"]["checked_on"] = checked_on
    return entry


class DoctorContactAgeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-doctor-contact-age-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "init", "--directory", str(self.root),
                                 "--name", "Synthetic", "--county", "San Luis Obispo", "--state", "CA"],
                                cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.kit = self.root / "kit"
        self.agencies_path = self.kit / "agencies.json"

    def write(self, agencies):
        self.kit.mkdir(exist_ok=True)
        self.agencies_path.write_text(json.dumps(agencies) if not isinstance(agencies, str) else agencies,
                                      encoding="utf-8")

    def doctor(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        argv = ["campaign_tool", "doctor", "--directory", str(self.root), *map(str, extra)]
        umask = os.umask(0o077)
        os.umask(umask)
        self.addCleanup(os.umask, umask)
        with mock.patch.object(sys, "argv", argv), mock.patch("campaign_tool.kit.date", FixedDate), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                cli.main()
                code = 0
            except SystemExit as stop:
                code = stop.code
        return code, out.getvalue(), err.getvalue()

    def report(self, *extra):
        code, out, err = self.doctor(*extra)
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def test_lists_entries_older_than_n_oldest_first_with_one_next_action(self):
        self.write([agency("ca-fresh", ago(10)), agency("ca-mid", ago(181)), agency("ca-old", ago(400))])
        report = self.report("--contact-max-age-days", 180)
        self.assertEqual(report["stale_contacts"], [
            {"agency_id": "ca-old", "checked_on": ago(400), "age_days": 400},
            {"agency_id": "ca-mid", "checked_on": ago(181), "age_days": 181}])
        self.assertNotIn("stale_contacts_more", report)
        self.assertEqual([a for a in report["next_actions"] if a.startswith("Re-check")], [ACTION.format(2)])

    def test_exactly_n_days_old_and_future_dates_are_not_stale(self):
        self.write([agency("ca-boundary", ago(180)), agency("ca-future", ago(-3))])
        report = self.report("--contact-max-age-days", 180)
        self.assertEqual(report["stale_contacts"], [])
        self.assertFalse([a for a in report["next_actions"] if a.startswith("Re-check")])
        self.assertEqual(kit.stale_contacts(self.root, 179, today=TODAY),
                         [{"agency_id": "ca-boundary", "checked_on": ago(180), "age_days": 180}])

    def test_unverified_unselected_and_truthy_wording_entries_never_listed(self):
        self.write([agency("ca-unverified", ago(900), verified=False),
                    agency("ca-unselected", ago(900), selected=False),
                    agency("ca-verified-string", ago(900), verified="true"),
                    agency("ca-selected-string", ago(900), selected="yes"),
                    {"agency_id": "ca-no-flags", "verification": {"checked_on": ago(900)}},
                    "ca-not-an-object"])
        self.assertEqual(self.report("--contact-max-age-days", 1)["stale_contacts"], [])

    def test_missing_or_unparseable_checked_on_is_listed_with_nulls_first(self):
        self.write([agency("ca-old", ago(400)), agency("ca-missing"), agency("ca-bad-month", "2026-13-01"),
                    agency("ca-number", 20260101), agency("ca-no-verification", verification=False),
                    agency("ca-short", "2026-1-01")])
        stale = self.report("--contact-max-age-days", 180)["stale_contacts"]
        nulls = ["ca-bad-month", "ca-missing", "ca-no-verification", "ca-number", "ca-short"]
        self.assertEqual(stale, [{"agency_id": i, "checked_on": None, "age_days": None} for i in nulls]
                         + [{"agency_id": "ca-old", "checked_on": ago(400), "age_days": 400}])

    def test_without_flag_output_is_unchanged(self):
        self.write([agency("ca-old", ago(400))])
        code, plain, err = self.doctor()
        self.assertEqual(code, 0, err)
        self.assertNotIn("stale_contacts", plain)
        flagged = self.report("--contact-max-age-days", 180)
        self.assertEqual(flagged.pop("stale_contacts"), [{"agency_id": "ca-old", "checked_on": ago(400),
                                                         "age_days": 400}])
        flagged["next_actions"].remove(ACTION.format(1))
        self.assertEqual(plain, json.dumps(flagged, indent=2) + "\n")

    def test_bad_values_exit_2_and_missing_kit_gives_empty_list(self):
        for value, message in (("0", "at least 1"), ("-5", "at least 1"), ("abc", "invalid int value")):
            with self.subTest(value=value):
                code, out, err = self.doctor("--contact-max-age-days", value)
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertIn(message, err)
        self.assertFalse(self.agencies_path.exists())
        report = self.report("--contact-max-age-days", 180)
        self.assertEqual(report["stale_contacts"], [])
        self.assertNotIn("kit_error", report)

    def test_unreadable_kit_keeps_exit_0_with_kit_error_and_no_list(self):
        self.write("{not json")
        report = self.report("--contact-max-age-days", 180)
        self.assertEqual(report["kit_error"], "kit/agencies.json is not readable JSON")
        self.assertNotIn("stale_contacts", report)
        self.assertIsNone(kit.stale_contacts(self.root, 180, today=TODAY))

    def test_caps_at_100_with_more_count_and_writes_nothing(self):
        self.write([agency(f"ca-{n:03d}", ago(200 + n)) for n in range(120)])
        before = {p: os.stat(p).st_mtime_ns for p in (self.kit, self.agencies_path)}
        content = self.agencies_path.read_bytes()
        report = self.report("--contact-max-age-days", 180)
        self.assertEqual(len(report["stale_contacts"]), 100)
        self.assertEqual(report["stale_contacts"][0], {"agency_id": "ca-119", "checked_on": ago(319),
                                                       "age_days": 319})
        self.assertEqual(report["stale_contacts"][-1]["agency_id"], "ca-020")
        self.assertEqual(report["stale_contacts_more"], 20)
        self.assertIn(ACTION.format(120), report["next_actions"])
        self.assertEqual({p: os.stat(p).st_mtime_ns for p in before}, before)
        self.assertEqual(self.agencies_path.read_bytes(), content)
        self.assertEqual(sorted(p.name for p in self.kit.iterdir()), ["agencies.json"])


if __name__ == "__main__":
    unittest.main()
