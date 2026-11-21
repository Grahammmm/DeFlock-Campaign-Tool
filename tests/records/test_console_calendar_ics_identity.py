"""Calendar export identity on synthetic, in-memory release links."""
import unittest

from campaign_tool.records import console_calendar as cc
from tests.records.test_console_calendar import NOW, TODAY, link, register, stub


EXPIRY = "2026-11-17"
HOSTS = ("first.example.test", "second.example.test")
# Legacy UID for link_expiry|alpha-pd||2026-11-17, not computed by the code under test.
LEGACY_UID = "c8285b5bdb75769c25243e0ddcb4957f@calendar.deflock.invalid"


def expiry_rows(*, second_inferred=False):
    model = stub(register([]))
    model["links"]["doc"]["links"] = [link(EXPIRY, host=HOSTS[0]),
                                      link(EXPIRY, host=HOSTS[1], inferred=second_inferred)]
    return cc.entries(model, TODAY)


def events(text):
    blocks = text.replace("\r\n ", "").split("BEGIN:VEVENT\r\n")[1:]
    return [dict(line.split(":", 1) for line in block.split("END:VEVENT\r\n", 1)[0].splitlines())
            for block in blocks]


class CalendarIcsIdentity(unittest.TestCase):
    def test_same_day_expiries_with_identical_export_text_both_survive(self):
        # Neither the legacy UID nor the exported display text distinguishes the hosts.
        rows = expiry_rows()
        self.assertEqual(len(rows), 2)
        text = cc.to_ics(rows, NOW)
        exported = events(text)
        self.assertEqual([e["DTSTART;VALUE=DATE"] for e in exported], ["20261117"] * 2)
        self.assertEqual(len({e["UID"] for e in exported}), 2)
        for host in HOSTS:
            self.assertNotIn(host, text)

    def test_collision_uids_stay_with_each_event_when_reordered_or_repeated(self):
        # Use different date bases to identify each exported event without exposing the host.
        first, second = expiry_rows(second_inferred=True)
        forward = events(cc.to_ics([first, second], NOW))
        self.assertEqual(len(forward), 2)
        by_summary = {e["SUMMARY"]: e["UID"] for e in forward}
        self.assertEqual(len(set(by_summary.values())), 2)
        for rows in ([second, first], [second, dict(first), first, dict(second)]):
            with self.subTest(size=len(rows)):
                exported = events(cc.to_ics(iter(rows), NOW))
                self.assertEqual(len(exported), 2)
                self.assertEqual({e["SUMMARY"]: e["UID"] for e in exported}, by_summary)

    def test_single_event_keeps_legacy_uid_even_when_repeated(self):
        row, _ = expiry_rows()
        for rows in ([row], [row, dict(row)]):
            with self.subTest(size=len(rows)):
                self.assertEqual([e["UID"] for e in events(cc.to_ics(rows, NOW))], [LEGACY_UID])
