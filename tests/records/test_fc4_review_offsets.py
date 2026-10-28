"""FC4-REVIEW-OFFSETS: finding review timestamps must carry a possible UTC offset.

``reviewed_timestamp`` used to accept offsets such as ``+00:60`` or ``-01:99``
because ``datetime.fromisoformat`` normalises the out-of-range minutes into a
larger offset. A review receipt carrying such a timestamp could then supply
one of the required independent roles in the finding gate.

These tests use the real production API (``reviewed_timestamp`` and ``gate``)
with a synthetic, structurally ready finding shaped like the canonical
receipts in ``campaign_tool/records/gates/tests/test_gate.py``: author
``analyst``, ``reviewer-a`` for the factual and privacy roles and
``reviewer-b`` for the legal role. Only one required review timestamp is
replaced at a time; digest, role and independence prerequisites stay valid.
The case tables are module-level so the acceptance runner can compare the
candidate with the pinned baseline on the very same inputs.
"""
import copy
import hashlib
import tempfile
import unittest
from pathlib import Path

from campaign_tool.records.gates import validate_findings as gates

STAMP = "2030-01-01T00:00:00"

# Previously accepted and still accepted (baseline and candidate agree).
VALID_Z_AND_FRACTIONAL = (
    STAMP + "Z",
    STAMP + ".5Z",
    STAMP + ".123456Z",
    STAMP + ".123456789Z",
    "2028-02-29T23:59:59.999999Z",
    STAMP + ".25+05:30",
)
VALID_SIGNED_OFFSETS = (
    STAMP + "+00:00",
    STAMP + "-00:00",  # Negative zero keeps its existing treatment (accepted, UTC).
    STAMP + "+00:59",
    STAMP + "-00:59",
    STAMP + "+23:00",
    STAMP + "-23:00",
    STAMP + "+23:59",
    STAMP + "-23:59",
    STAMP + "+05:45",
    STAMP + "-09:30",
)
VALID = VALID_Z_AND_FRACTIONAL + VALID_SIGNED_OFFSETS

# Offset minutes outside 00..59 (several of these were accepted at baseline).
INVALID_OFFSET_MINUTES = (
    STAMP + "+00:60",
    STAMP + "-00:60",
    STAMP + "+01:60",
    STAMP + "-01:99",
    STAMP + "+00:99",
    STAMP + "+22:60",
    STAMP + "+23:60",
    STAMP + ".5+00:60",
)
# Offset hours outside 00..23.
INVALID_OFFSET_HOURS = (
    STAMP + "+24:00",
    STAMP + "-24:00",
    STAMP + "+24:59",
    STAMP + "+25:00",
    STAMP + "-99:00",
)
# Calendar and clock validation that must stay in force.
INVALID_CALENDAR_AND_CLOCK = (
    "2030-02-29T00:00:00Z",
    "2030-13-01T00:00:00Z",
    "2030-00-01T00:00:00Z",
    "2030-01-32T00:00:00Z",
    "2030-04-31T00:00:00+00:00",
    "0000-01-01T00:00:00Z",
    "2030-01-01T24:00:00Z",
    "2030-01-01T23:60:00Z",
    "2030-01-01T23:59:60Z",
    "2030-01-01T00:00:00",
    "2030-01-01",
)
# Non-ASCII digits in every numeric component, and other malformed text.
UNICODE_AND_MALFORMED_TEXT = (
    "\u0662030-01-01T00:00:00Z",          # Arabic-Indic digit in the year
    "2030-\u06f01-01T00:00:00Z",           # Extended Arabic-Indic digit in the month
    "2030-01-\uff101T00:00:00Z",           # Fullwidth digit in the day
    "2030-01-01T0\u0967:00:00Z",           # Devanagari digit in the hour
    "2030-01-01T00:\u09e6\u09e6:00Z",      # Bengali digits in the minute
    "2030-01-01T00:00:0\u0e50Z",           # Thai digit in the second
    STAMP + ".\u0661Z",                    # Arabic-Indic digit in the fraction
    STAMP + "+0\u0661:00",                 # Arabic-Indic digit in the offset hour
    STAMP + "+00:\u0660\u0660",            # Arabic-Indic digits in the offset minute
    STAMP + "+\uff10\uff10:\uff10\uff10",  # Fullwidth offset
    STAMP + "\u2212" + "01:00",            # Unicode minus sign
    "2030-01-01t00:00:00Z",
    STAMP + "z",
    "2030-01-01 00:00:00Z",
    STAMP + "+0000",
    STAMP + "+00",
    STAMP + "+1:00",
    STAMP + "+01:0",
    STAMP + "Z\n",
    " " + STAMP + "Z",
    STAMP + ".Z",
    STAMP + "Z+00:00",
    "",
)
NONSTRING = (None, 0, 1.5, True, b"2030-01-01T00:00:00Z", bytearray(b"2030-01-01T00:00:00Z"),
             ["2030-01-01T00:00:00Z"], {"reviewed_at": "2030-01-01T00:00:00Z"})
INVALID = INVALID_OFFSET_MINUTES + INVALID_OFFSET_HOURS + INVALID_CALENDAR_AND_CLOCK \
    + UNICODE_AND_MALFORMED_TEXT + NONSTRING

LEGAL_LOSS_BLOCKERS = sorted({"incomplete reviewer receipt", "missing independent roles: legal",
                              "at least two distinct independent reviewers required"})
FACTUAL_LOSS_BLOCKERS = sorted({"incomplete reviewer receipt", "missing independent roles: factual"})


def synthetic_case(directory):
    """A structurally ready synthetic finding plus its three canonical receipts."""
    original = Path(directory).resolve() / "original.txt"
    original.write_bytes(b"synthetic fc4 review offsets: query row 1 purpose field empty\n")
    finding = {
        "finding_id": "fc4-review-offsets-1", "agency": "Synthetic agency",
        "classification": "APPARENT_RULE_CONFLICT", "claim": "Synthetic bounded finding",
        "author_agent": "analyst", "next_action": "Ask for native notes",
        "counterevidence": [], "missing_evidence": ["native notes"],
        "primary_evidence": [{"path": str(original), "sha256": hashlib.sha256(original.read_bytes()).hexdigest(),
                              "locator": "line 1", "coverage": "full_text", "observation": "empty field"}],
        "rules": [], "privacy_status": "cleared", "publication_status": "draft",
    }
    digest = gates.digest(finding)
    reviews = [{"finding_id": finding["finding_id"], "finding_digest": digest,
                "reviewer_agent": "reviewer-b" if role == "legal" else "reviewer-a",
                "role": role, "verdict": "pass", "reviewed_primary": True,
                "checked_locators": ["original line 1"], "rationale": "Synthetic source compared",
                "reviewed_at": STAMP + "Z"} for role in ("factual", "legal", "privacy")]
    return finding, reviews


def with_timestamp(reviews, role, value):
    """Copy of the receipts with only ``role``'s reviewed_at replaced."""
    changed = copy.deepcopy(reviews)
    matches = [r for r in changed if r["role"] == role]
    if len(matches) != 1:
        raise AssertionError("fixture must carry exactly one %s receipt" % role)
    matches[0]["reviewed_at"] = value
    return changed


class ReviewedTimestampTests(unittest.TestCase):
    """Direct predicate behaviour."""

    def assert_all(self, values, expected):
        for value in values:
            with self.subTest(value=ascii(value)):
                self.assertIs(gates.reviewed_timestamp(value), expected)

    def test_z_and_fractional_seconds(self):
        self.assert_all(VALID_Z_AND_FRACTIONAL, True)

    def test_signed_offsets_at_boundaries(self):
        self.assert_all(VALID_SIGNED_OFFSETS, True)

    def test_offset_minutes_59_60_99(self):
        self.assert_all((STAMP + "+00:59", STAMP + "-00:59", STAMP + "+23:59"), True)
        self.assert_all(INVALID_OFFSET_MINUTES, False)

    def test_offset_hours_23_24(self):
        self.assert_all((STAMP + "+23:00", STAMP + "-23:00", STAMP + "-23:59"), True)
        self.assert_all(INVALID_OFFSET_HOURS, False)

    def test_invalid_calendar_and_clock(self):
        self.assert_all(("2028-02-29T00:00:00Z", "2030-12-31T23:59:59Z"), True)
        self.assert_all(INVALID_CALENDAR_AND_CLOCK, False)

    def test_unicode_digits_and_nonstring(self):
        self.assert_all(UNICODE_AND_MALFORMED_TEXT, False)
        self.assert_all(NONSTRING, False)


class FindingGateTests(unittest.TestCase):
    """The actual finding gate with only one review timestamp replaced."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="fc4-review-offsets-")
        self.finding, self.reviews = synthetic_case(self.directory.name)
        self.digest = gates.digest(self.finding)

    def tearDown(self):
        self.directory.cleanup()

    def run_gate(self, reviews):
        before = copy.deepcopy(self.finding)
        report = gates.gate(self.finding, reviews, True)
        self.assertEqual(self.finding, before, "gate must not mutate the finding")
        self.assertEqual(report["finding_digest"], self.digest)
        return report

    def test_ready_gate_control(self):
        report = self.run_gate(self.reviews)
        self.assertTrue(report["ready"], report["blockers"])
        self.assertEqual(report["blockers"], [])
        for role in ("factual", "legal"):
            for value in VALID:
                with self.subTest(role=role, value=value):
                    report = self.run_gate(with_timestamp(self.reviews, role, value))
                    self.assertTrue(report["ready"], report["blockers"])

    def test_invalid_timestamp_cannot_supply_required_role(self):
        for value in INVALID:
            with self.subTest(role="legal", value=ascii(value)):
                report = self.run_gate(with_timestamp(self.reviews, "legal", value))
                self.assertFalse(report["ready"])
                self.assertEqual(report["blockers"], LEGAL_LOSS_BLOCKERS)
            with self.subTest(role="factual", value=ascii(value)):
                report = self.run_gate(with_timestamp(self.reviews, "factual", value))
                self.assertFalse(report["ready"])
                self.assertEqual(report["blockers"], FACTUAL_LOSS_BLOCKERS)

    def test_existing_challenge_still_blocks(self):
        for value in (STAMP + "Z", STAMP + "+00:59", STAMP + "+00:60", STAMP + "-01:99"):
            with self.subTest(value=value):
                challenge = copy.deepcopy(self.reviews[0])
                challenge.update(verdict="challenge", reviewed_at=value)
                report = self.run_gate(self.reviews + [challenge])
                self.assertFalse(report["ready"])
                self.assertEqual(report["blockers"], ["unresolved reviewer challenge or block"])


if __name__ == "__main__":
    unittest.main()
