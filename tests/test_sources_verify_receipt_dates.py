"""`sources verify-copy` receipt-day bound (RF1900-4): --checked-on is compared with the parsed calendar day of every
selected entry's received_at, not with the first ten characters of its spelling.

Synthetic campaign and bytes only; nothing is fetched. Expected calendar days are written as literals.
"""
import hashlib
import itertools
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from campaign_tool import sources_cmd
from campaign_tool.content import load_sources
from campaign_tool.sources_cmd import verify_copy

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
RECORD = b"synthetic RF1900-4 record bytes; not a real document\n"
A = hashlib.sha256(RECORD).hexdigest()
OTHER = hashlib.sha256(b"synthetic RF1900-4 other record\n").hexdigest()
URL = "https://example.invalid/fixtures/policy"
TODAY = date(2026, 6, 1)
REFUSAL = r"--checked-on: must not be before the record was received \(%s\)"


def entry(received_at, fragment, sha=A, url=URL):
    return {"sha256": sha, "title": "Synthetic record", "agency_id": "ca-cedar-police", "received_at": received_at,
            "public_url": url, "fragment": fragment}


def cli(root, checked_on, file, *extra):
    return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "sources", "verify-copy", "--directory",
                           str(root), "--sha256", A[:16], "--checked-on", checked_on, "--file", str(file), *extra],
                          cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)


class ReceiptDayTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-receipt-dates-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.sources = self.root / "content" / "sources.json"
        self.copy = self.tmp / "downloaded.pdf"
        self.copy.write_bytes(RECORD)

    def write(self, document):
        self.sources.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        load_sources(json.loads(self.sources.read_text(encoding="utf-8")))  # the real loader admits every spelling

    def library(self):
        return json.loads(self.sources.read_text(encoding="utf-8"))

    def snapshot(self):
        info = self.sources.stat()
        return (self.sources.read_bytes(), info.st_mode, info.st_mtime_ns, sorted(os.listdir(self.sources.parent)))

    def assert_refused(self, day, checked_on, **kwargs):
        before = self.snapshot()
        with mock.patch.object(sources_cmd, "_hash_copy", side_effect=AssertionError("hashed")), \
                mock.patch.object(sources_cmd, "_rewrite", side_effect=AssertionError("written")):
            with self.assertRaisesRegex(ValueError, REFUSAL % day):
                verify_copy(self.root, A[:12], str(self.copy), checked_on, today=TODAY, **kwargs)
        self.assertEqual(self.snapshot(), before)

    def assert_accepted(self, checked_on, **kwargs):
        result = verify_copy(self.root, A[:12], str(self.copy), checked_on, today=TODAY, **kwargs)
        self.assertEqual(result, {"sha256_prefix": A[:12], "matches": True, "checked_on": checked_on})
        return result

    def test_week_year_spillover_refuses_check_before_actual_receipt(self):
        self.write([entry("2020-W53-7T10:00:00Z", "cedar-police-records")])
        self.assert_refused("2021-01-03", "2021-01-01")
        before = self.snapshot()
        result = cli(self.root, "2021-01-01", self.copy)
        self.assertEqual((result.returncode, result.stdout), (3, ""))
        self.assertIn("Stopped: --checked-on: must not be before the record was received (2021-01-03)", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(self.snapshot(), before)
        self.assertNotIn("public_copy", self.library()[0])
        # an existing check is not overwritten by the refused one
        self.assert_accepted("2021-01-03")
        kept = self.snapshot()
        self.assert_refused("2021-01-03", "2021-01-02")
        self.assertEqual(self.snapshot(), kept)

    def test_compact_and_week_only_receipts_accept_valid_checks(self):
        cases = (("2026-01-20T16:30:00+00:00", "2026-01-20"), ("20260120T163000Z", "2026-01-20"),
                 ("2026-W04T16:30:00+00:00", "2026-01-19"), ("2026W04T163000Z", "2026-01-19"),
                 ("2026-W04-2T16:30:00+00:00", "2026-01-20"), ("2026W042T163000Z", "2026-01-20"),
                 ("2026-W04-2 16:30:00+00:00", "2026-01-20"), ("2026-01-20 16:30:00+00:00", "2026-01-20"))
        for received_at, day in cases:
            for checked_on in (day, "2026-02-01"):
                with self.subTest(received_at=received_at, checked_on=checked_on):
                    self.write([entry(received_at, "cedar-police-records")])
                    self.assert_accepted(checked_on)
                    stored = self.library()[0]
                    self.assertEqual(stored["public_copy"], {"matches": True, "checked_on": checked_on})
                    self.assertEqual(stored["received_at"], received_at)  # spelling is not canonicalized
        self.write([entry("20260120T163000Z", "cedar-police-records")])
        result = cli(self.root, "2026-02-01", self.copy)
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertEqual(json.loads(result.stdout), {"sha256_prefix": A[:12], "matches": True, "checked_on": "2026-02-01"})
        self.assertEqual(self.library()[0]["public_copy"], {"matches": True, "checked_on": "2026-02-01"})

    def test_iso_week_year_can_differ_from_calendar_year_in_both_directions(self):
        for received_at, day, before in (("2020-W01T10:00:00Z", "2019-12-30", "2019-12-29"),
                                         ("2020-W53-7T10:00:00Z", "2021-01-03", "2021-01-02"),
                                         ("2024-02-29T10:00:00Z", "2024-02-29", "2024-02-28"),
                                         ("2025-12-31T10:00:00Z", "2025-12-31", "2025-12-30")):
            with self.subTest(received_at=received_at):
                self.write([entry(received_at, "cedar-police-records")])
                self.assert_refused(day, before)
                self.assert_accepted(day)

    def test_mixed_spelling_alias_maximum_is_chronological_and_order_independent(self):
        aliases = [entry("20260105T100000Z", "cedar-first"), entry("2026-W03T10:00:00Z", "cedar-second"),
                   entry("2026-01-10T10:00:00+00:00", "cedar-third")]  # latest day: 2026-01-12 (week 3 Monday)
        for order in itertools.permutations(aliases):
            for fragment in (None, "cedar-first", "cedar-third"):
                with self.subTest(order=[e["fragment"] for e in order], fragment=fragment):
                    self.write([dict(item) for item in order])
                    self.assert_refused("2026-01-12", "2026-01-11", fragment=fragment)
                    self.assert_accepted("2026-01-12", fragment=fragment)
                    self.assertEqual([item["public_copy"] for item in self.library()],
                                     [{"matches": True, "checked_on": "2026-01-12"}] * 3)

    def test_unselected_url_null_alias_and_other_record_do_not_constrain_date(self):
        other_url = "https://example.invalid/fixtures/other"
        self.write([entry("2026-01-10T10:00:00Z", "cedar-police-records"),
                    entry("2026-04-01T10:00:00Z", "cedar-other-url", url=other_url),
                    entry("2026-04-02T10:00:00Z", "cedar-no-url", url=None),
                    entry("2026-04-03T10:00:00Z", "cedar-other-record", sha=OTHER)])
        self.assert_accepted("2026-01-10", fragment="cedar-police-records")
        library = self.library()
        self.assertEqual(library[0]["public_copy"], {"matches": True, "checked_on": "2026-01-10"})
        self.assertEqual(["public_copy" in item for item in library[1:]], [False, False, False])
        # the later-dated other URL of the same hash binds only when it is the selected one
        self.assert_refused("2026-04-01", "2026-03-31", fragment="cedar-other-url")
        # a later record sharing the chosen URL does not bind either
        self.write([entry("2026-01-10T10:00:00Z", "cedar-police-records"),
                    entry("2026-05-01T10:00:00Z", "cedar-other-record", sha=OTHER)])
        self.assert_accepted("2026-01-10")
        self.assertNotIn("public_copy", self.library()[1])
        # precedence of the existing selection refusals is unchanged
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "no library entry for this record in the selected fragment"):
            verify_copy(self.root, A[:12], str(self.copy), "2026-01-01", today=TODAY, fragment="cedar-missing")
        with self.assertRaisesRegex(ValueError, "no library entry starts with"):
            verify_copy(self.root, "0" * 12, str(self.copy), "2026-01-01", today=TODAY)
        self.assertEqual(self.snapshot(), before)

    def test_written_offset_date_is_not_converted_to_utc(self):
        cases = (("2026-03-10T23:30:00-08:00", "2026-03-10"),  # UTC day is 03-11
                 ("2026-03-10T00:30:00+09:00", "2026-03-10"),  # UTC day is 03-09
                 ("2020-W01T00:30:00+09:00", "2019-12-30"),  # UTC instant is 2019-12-29
                 ("2020-W53-7T23:30:00-08:00", "2021-01-03"),  # UTC instant is 2021-01-04
                 ("2026-03-10T10:00:00", "2026-03-10"), ("2026-03-10", "2026-03-10"))
        for received_at, day in cases:
            with self.subTest(received_at=received_at):
                self.write([entry(received_at, "cedar-police-records")])
                before_day = date.fromordinal(date.fromisoformat(day).toordinal() - 1).isoformat()
                self.assert_refused(day, before_day)
                self.assert_accepted(day)

    def test_date_refusal_precedes_hashing_and_preserves_existing_state(self):
        earlier = {"matches": False, "checked_on": "2026-01-01", "reason": "redacted"}
        self.write([{**entry("2020-W53-7T10:00:00Z", "cedar-first"), "public_copy": earlier},
                    entry("20210102T100000Z", "cedar-second")])
        os.chmod(self.sources, 0o640)
        self.assert_refused("2021-01-03", "2021-01-02")
        self.assertEqual(self.library()[0]["public_copy"], earlier)
        with mock.patch.object(sources_cmd, "_hash_copy", side_effect=AssertionError("hashed")):
            for checked_on, message in (("2999-01-01", "must not be in the future"),
                                        ("2021-13-01", "a real YYYY-MM-DD date"), ("2021-1-3", "a real YYYY-MM-DD date")):
                with self.subTest(checked_on=checked_on), self.assertRaisesRegex(ValueError, message):
                    verify_copy(self.root, A[:12], str(self.copy), checked_on, today=TODAY)
        # a malformed receipt is refused by the unchanged loader, with no fabricated date
        document = self.library()
        document[1]["received_at"] = "not-a-date"
        self.sources.write_text(json.dumps(document), encoding="utf-8")
        before = self.snapshot()
        with mock.patch.object(sources_cmd, "_hash_copy", side_effect=AssertionError("hashed")), \
                self.assertRaisesRegex(Exception, "received_at: invalid ISO-8601"):
            verify_copy(self.root, A[:12], str(self.copy), "2026-02-01", today=TODAY)
        self.assertEqual(self.snapshot(), before)

    def test_ordinary_success_mismatch_reason_and_json_remain_compatible(self):
        self.write([entry("2026-01-20T16:30:00+00:00", "cedar-police-records") | {"pages": "1-2"}])
        self.assert_refused("2026-01-20", "2026-01-19")
        self.assertEqual(self.assert_accepted("2026-01-20"), {"sha256_prefix": A[:12], "matches": True,
                                                              "checked_on": "2026-01-20"})
        stored = self.library()[0]
        self.assertEqual(list(stored), ["sha256", "title", "agency_id", "received_at", "public_url", "fragment", "pages",
                                        "public_copy"])
        self.assertEqual(stored["received_at"], "2026-01-20T16:30:00+00:00")
        other = self.tmp / "other.pdf"
        other.write_bytes(b"synthetic different public copy\n")
        digest = hashlib.sha256(other.read_bytes()).hexdigest()
        with self.assertRaisesRegex(ValueError, "copy differs from the record; pass --differs-because"):
            verify_copy(self.root, A[:12], str(other), "2026-02-01", today=TODAY)
        result = verify_copy(self.root, A[:12], str(other), "2026-02-01", "converted", today=TODAY)
        self.assertEqual(result, {"sha256_prefix": A[:12], "matches": False, "checked_on": "2026-02-01",
                                  "reason": "converted", "copy_sha256_prefix": digest[:12]})
        self.assertEqual(self.library()[0]["public_copy"],
                         {"matches": False, "checked_on": "2026-02-01", "reason": "converted"})
        with self.assertRaisesRegex(ValueError, "copy matches; --differs-because is only"):
            verify_copy(self.root, A[:12], str(self.copy), "2026-02-01", "redacted", today=TODAY)


if __name__ == "__main__":
    unittest.main()
