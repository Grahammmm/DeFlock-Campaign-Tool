"""FC2-DURATION-TOKENS: retention detectors read only complete plain-integer durations.

A duration such as "1.5 years", "1,000 days", "-30 days" or "30-60 days" is an
unsupported numeric form. The digest detector must leave it uninterpreted
instead of comparing a fragment ("5 years", "000 days", "30 days", "60 days")
against the CHP 60-day reference. Supported nonnegative plain integers keep
their existing vocabulary, conversion factors, locators, excerpts and
comparison metadata. All fixtures are synthetic and in memory.
"""
import unittest

from campaign_tool.digest import run_detectors
from campaign_tool.digest import detectors
from campaign_tool.digest.detectors import detect_retention, units_from_text

# Unambiguous, active, synthetic rule fixture: exactly one rule carries the
# retention citation fragment, so rule lookup cannot depend on package dates.
RULE_ID = "fixture-veh-2413-retention"
CITATION = "Veh. Code \u00a7 2413 (synthetic fixture)"
PACKAGE = {"rules": [{"rule_id": RULE_ID, "citation": CITATION, "status": "active"}]}
NOTE = "Veh. Code 2413 binds CHP; for other agencies this is a reference point, not the governing period."
FRAGMENT = "FRAGMENT_HIT"


def retention_hits(units):
    """Retention hits from the real detector and through the real pipeline; both must agree."""
    direct = detect_retention(units, PACKAGE)
    piped = [h for h in run_detectors(units, PACKAGE) if h["detector"] == "retention_period"]
    return direct, piped


class _Base(unittest.TestCase):
    def assert_uninterpreted(self, line):
        direct, piped = retention_hits(units_from_text(line))
        if direct or piped:
            self.fail(f"{FRAGMENT}: {line!r} -> "
                      f"{[(h['detail']['stated'], h['detail']['stated_days']) for h in direct + piped]}")
        self.assertEqual(direct, piped)

    def assert_stated(self, line, expected):
        """``expected`` is a list of (stated, stated_days, comparison)."""
        direct, piped = retention_hits(units_from_text(line))
        self.assertEqual(direct, piped)
        got = [(h["detail"]["stated"], h["detail"]["stated_days"], h["detail"]["comparison"]) for h in direct]
        if len(got) > len(expected):
            self.fail(f"{FRAGMENT}: {line!r} -> {got}")
        self.assertEqual(got, expected, line)
        return direct


class MeasuredBaselineFailures(_Base):
    """The three failures measured against the pinned source."""

    def test_decimal_1_5_years_is_uninterpreted(self):
        self.assert_uninterpreted("Data is retained for 1.5 years.")

    def test_grouped_1000_days_is_uninterpreted(self):
        self.assert_uninterpreted("Data is retained for 1,000 days.")

    def test_signed_minus_30_days_is_uninterpreted(self):
        self.assert_uninterpreted("Data is retained for -30 days.")


class SignedVariants(_Base):
    def test_signed_variants(self):
        for line in ("Data is retained for +30 days.", "Data is retained for \u221230 days.",
                     "Data is retained for \u00b130 days.", "Records are kept -30-day minimum.",
                     "Retention: -2 years", "Retention: +6 months"):
            with self.subTest(line=line):
                self.assert_uninterpreted(line)


class DecimalVariants(_Base):
    def test_decimal_variants(self):
        for line in ("Data is retained for 0.5 years.", "Data is retained for .5 years.",
                     "Data is retained for 2.25 months.", "Data is retained for 1,5 years.",
                     "Footage is kept 1.5-year period.", "Images are stored for 30.5 days.",
                     "Retention 7.5 weeks", "Retained 1.5 hours"):
            with self.subTest(line=line):
                self.assert_uninterpreted(line)


class GroupingVariants(_Base):
    def test_grouping_variants(self):
        for line in ("Data is retained for 10,000 days.", "Data is retained for 1,000,000 days.",
                     "Data is retained for 1.000 days.", "Data is retained for 1 000 days.",
                     "Data is retained for 1\u202f000 days.", "Data is retained for 1\u00a0000 days.",
                     "Data is retained for 1'000 days.", "Data is retained for 1\u2019000 days.",
                     "Data is retained for 1,000-day period."):
            with self.subTest(line=line):
                self.assert_uninterpreted(line)


class NumericRanges(_Base):
    def test_numeric_ranges(self):
        for line in ("Data is retained for 30-60 days.", "Data is retained for 30\u201360 days.",
                     "Data is retained for 30\u201460 days.", "Data is retained for 30 - 60 days.",
                     "Data is retained for 30 \u2013 60 days.", "Data is retained for 30- 60 days.",
                     "Data is retained for 30 -60 days.", "Data is retained for 1/2 year.",
                     "Data is retained for 1-2 years.", "Data is retained for 30-60-day period."):
            with self.subTest(line=line):
                self.assert_uninterpreted(line)


class SupportedDurations(_Base):
    def test_days_months_years_weeks_hours(self):
        cases = [
            ("Data is retained for 30 days.", [("30 days", 30, "within_chp_60_days")]),
            ("Data is retained for 1 day.", [("1 day", 1, "within_chp_60_days")]),
            ("Images are kept 6 months.", [("6 months", 180, "exceeds_chp_60_days")]),
            ("Images are kept 1 month.", [("1 month", 30, "within_chp_60_days")]),
            ("Data is stored for 2 years.", [("2 years", 730, "exceeds_chp_60_days")]),
            ("Data is stored for 1 year.", [("1 year", 365, "exceeds_chp_60_days")]),
            ("Data is purged after 3 weeks.", [("3 weeks", 21, "within_chp_60_days")]),
            ("Data is deleted after 48 hours.", [("48 hours", 2.0, "within_chp_60_days")]),
            ("Data is retained for 60 days.", [("60 days", 60, "within_chp_60_days")]),
            ("Data is retained for 61 days.", [("61 days", 61, "exceeds_chp_60_days")]),
            ("Data is retained for 2024 days.", [("2024 days", 2024, "exceeds_chp_60_days")]),
            ("DATA IS RETAINED FOR 90 DAYS.", [("90 DAYS", 90, "exceeds_chp_60_days")]),
        ]
        for line, expected in cases:
            with self.subTest(line=line):
                self.assert_stated(line, expected)

    def test_explicit_zero(self):
        self.assert_stated("Data is retained for 0 days.", [("0 days", 0, "within_chp_60_days")])

    def test_hyphenated_30_day_form(self):
        self.assert_stated("Records follow a 30-day retention period.", [("30-day", 30, "within_chp_60_days")])
        self.assert_stated("Records follow a 30 - day retention period.", [("30 - day", 30, "within_chp_60_days")])

    def test_punctuation_around_complete_token(self):
        for line, stated in (("Retention (30 days) applies.", "30 days"),
                             ("Retention: 30 days.", "30 days"),
                             ('Retention is "30 days".', "30 days"),
                             ("Retention is '30 days'.", "30 days"),
                             ("Data is retained for 30 days, then purged.", "30 days"),
                             ("Data is retained for 30 days; then purged.", "30 days"),
                             ("Data is retained [30 days]!", "30 days"),
                             ("Retained, 30 days.", "30 days"),
                             ("- Data retained for 30 days", "30 days"),
                             ("Data is retained for 30 days.30 is the limit", "30 days")):
            with self.subTest(line=line):
                self.assert_stated(line, [(stated, 30, "within_chp_60_days")])

    def test_multiple_supported_durations(self):
        self.assert_stated("Data is retained 30 days or 2 years.",
                           [("30 days", 30, "within_chp_60_days"), ("2 years", 730, "exceeds_chp_60_days")])

    def test_word_ranges_are_two_complete_tokens(self):
        # "to"/"and" ranges are separate complete integers, not fragments of one token.
        self.assert_stated("Data is retained for 30 days to 60 days.",
                           [("30 days", 30, "within_chp_60_days"), ("60 days", 60, "within_chp_60_days")])


class MixedLines(_Base):
    def test_unsupported_then_supported_on_one_line(self):
        cases = [
            ("Data is retained for 1.5 years, then purged after 30 days.", [("30 days", 30, "within_chp_60_days")]),
            ("Data is retained for 1,000 days or 45 days.", [("45 days", 45, "within_chp_60_days")]),
            ("Data is retained for -30 days and 90 days.", [("90 days", 90, "exceeds_chp_60_days")]),
            ("Data is retained for 30-60 days; purged at 90 days.", [("90 days", 90, "exceeds_chp_60_days")]),
            ("Data is retained for 90 days, not 1.5 years.", [("90 days", 90, "exceeds_chp_60_days")]),
            ("Retained +6 months, 1,000 days, 2.5 years, 7 days.", [("7 days", 7, "within_chp_60_days")]),
        ]
        for line, expected in cases:
            with self.subTest(line=line):
                self.assert_stated(line, expected)


class LocatorsAndShape(_Base):
    def test_full_hit_shape_is_preserved(self):
        line = "  Data is retained for 1.5 years, then purged after 90 days.  "
        hits = detect_retention([{"locator": {"line": 7}, "text": line}], PACKAGE)
        self.assertEqual(hits, [{
            "detector": "retention_period",
            "detector_version": "digest-detectors-1",
            "rule_id": RULE_ID,
            "citation": CITATION,
            "locator": {"line": 7},
            "excerpt": line.strip(),
            "kind": "comparison",
            "detail": {"stated": "90 days", "stated_days": 90, "reference_days": 60,
                       "comparison": "exceeds_chp_60_days", "note": NOTE},
        }])

    def test_multiline_page_unit_locators(self):
        units = [{"locator": {"page": 3},
                  "text": "Retention schedule\nData is retained for 1.5 years.\nVideo is purged after 45 days.\n"
                          "Logs are kept 1,000 days.\nBackups are kept 2 years."}]
        direct, piped = retention_hits(units)
        self.assertEqual(direct, piped)
        self.assertEqual([(h["locator"], h["excerpt"], h["detail"]["stated_days"]) for h in direct], [
            ({"page": 3, "line": 3}, "Video is purged after 45 days.", 45),
            ({"page": 3, "line": 5}, "Backups are kept 2 years.", 730),
        ])

    def test_units_from_text_line_locators(self):
        text = "Policy\nData is retained for -30 days.\n\nData is retained for 30 days."
        direct, piped = retention_hits(units_from_text(text))
        self.assertEqual(direct, piped)
        self.assertEqual([(h["locator"], h["detail"]["stated"]) for h in direct], [({"line": 4}, "30 days")])

    def test_no_retention_phrase_no_hit(self):
        for line in ("The board meets every 30 days.", "Notice is posted 1.5 years ahead.", ""):
            with self.subTest(line=line):
                self.assertEqual(retention_hits(units_from_text(line)), ([], []))

    def test_missing_rule_keeps_null_identity(self):
        hits = detect_retention(units_from_text("Data is retained for 30 days."), {"rules": []})
        self.assertEqual([(h["rule_id"], h["citation"], h["detail"]["stated_days"]) for h in hits], [(None, None, 30)])


class ConversionFactors(unittest.TestCase):
    def test_factors_and_reference_unchanged(self):
        self.assertEqual(detectors.CHP_RETENTION_DAYS, 60)
        self.assertEqual(detectors.DETECTOR_VERSION, "digest-detectors-1")
        for unit, factor in (("hour", 1 / 24), ("hours", 1 / 24), ("day", 1), ("days", 1), ("week", 7),
                             ("weeks", 7), ("month", 30), ("months", 30), ("year", 365), ("years", 365),
                             ("YEARS", 365)):
            with self.subTest(unit=unit):
                self.assertAlmostEqual(detectors._days("12", unit), 12 * factor, places=12)

    def test_vocabulary_unchanged(self):
        found = [m.group(2).lower() for m in detectors.DURATION.finditer(
            "1 day 2 days 3 week 4 weeks 5 month 6 months 7 year 8 years 9 hour 10 hours 11 fortnights 12 decades")]
        self.assertEqual(found, ["day", "days", "week", "weeks", "month", "months",
                                 "year", "years", "hour", "hours"])


if __name__ == "__main__":
    unittest.main()
