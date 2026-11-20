"""Synthetic regression for malformed citations in private digest reminders."""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from io import StringIO
from pathlib import Path
import tempfile
import unittest

from campaign_tool.records.understanding import digest
from tests.test_understanding_digest import fixture


class DigestReminderAnchorShapeTests(unittest.TestCase):
    def test_malformed_anchor_citation_suppresses_only_its_reminder(self):
        # A valid transition makes the old comparison call .get() on the list.
        marker = "synthetic-raw-anchor-value"
        for citation in ([marker], None, marker, True, 3):
            with self.subTest(citation_type=type(citation).__name__), tempfile.TemporaryDirectory() as tmp:
                ctx = fixture(Path(tmp).resolve())
                requests = ctx["requests"]["requests"]
                control = requests[0]
                broken = deepcopy(control)
                broken["request_id"] = "synthetic-malformed-anchor-request"
                # Replace the nested citation without modifying the transition.
                broken["reminders"]["anchor"]["citation"] = citation
                requests.insert(0, broken)
                ctx["request_provenance"]["sha256"] = digest.digest(ctx["requests"])

                stdout, stderr = StringIO(), StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    prepared = digest.prepare_digest(**ctx)
                assert prepared is not None
                _, report, data, text = prepared

                rows = {row["request_id_sha256"]: row for row in report["requests"]}
                self.assertEqual(len(rows), 2)
                bad_row = rows[digest.digest(broken["request_id"])]
                self.assertEqual(bad_row["state"], "UNRESOLVED")
                self.assertEqual(bad_row["unresolved_reasons"], ["reminder_citation_unresolved"])
                self.assertEqual(bad_row["reminders"]["status"], "UNRESOLVED")
                self.assertIsNone(bad_row["reminders"]["ten_day_candidate"])
                self.assertIsNone(bad_row["reminders"]["twenty_four_day_candidate"])
                self.assertEqual(len(bad_row["citations"]), 1)

                good_row = rows[digest.digest(control["request_id"])]
                self.assertEqual(good_row["state"], "sent")
                self.assertEqual(good_row["unresolved_reasons"], [])
                self.assertEqual(good_row["reminders"]["status"], "candidate")
                self.assertEqual(good_row["reminders"]["ten_day_candidate"], "2020-01-11")
                self.assertEqual(good_row["reminders"]["twenty_four_day_candidate"], "2020-01-25")
                for output in (data.decode(), text.decode(), stdout.getvalue(), stderr.getvalue()):
                    self.assertNotIn(marker, output)
