"""An unreadable export time must not erase independently verified counts."""
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from campaign_tool.records import campaign_summary as cs
from tests.records.test_campaign_summary import PID, SUMMARY, hand_export


class InvalidExportTimestampTests(unittest.TestCase):
    def setUp(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory(prefix="synthetic-cs-time-"))
        root = Path(os.path.realpath(tmp))
        stamp = "2026-11-31T00:00:00Z"
        self.top = hand_export(root, summary={**SUMMARY, "evaluated_at": stamp}, exported_at=stamp)
        self.binding = {"export_root": str(root), "source_project_id": PID}
        self.now = datetime(2026, 12, 1, tzinfo=timezone.utc)

    def test_calendar_parse_error_keeps_verified_counts(self):
        # The stamp passes the shape check, but strptime must not drop the whole row.
        row = cs.read_export(self.binding, now=self.now)
        self.assertEqual(row, {
            "state": "ok", "code": "RECORDS_EXPORT_TIME_UNAVAILABLE",
            "exported_at": None, "age_hours": None, "stale": None,
            **SUMMARY["counts"], "unavailable": [],
        })

    def test_bad_hash_still_discards_counts_with_an_impossible_date(self):
        # Same JSON and timestamp, different bytes: only the hash check should refuse it.
        summary = self.top / cs.REL
        summary.write_bytes(summary.read_bytes() + b"\n")
        row = cs.read_export(self.binding, now=self.now)
        self.assertEqual(row, {
            "state": "unavailable", "code": "RECORDS_EXPORT_UNAVAILABLE",
            "exported_at": None, "age_hours": None, "stale": None,
            **dict.fromkeys(SUMMARY["counts"]), "unavailable": sorted(SUMMARY["counts"]),
        })
