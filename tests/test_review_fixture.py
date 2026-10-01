"""Parity fixture for review_blockers shared with workers/shared/src/review.ts.

Run ``python3 -B tests/test_review_fixture.py --regenerate`` after changing
campaign_tool/review.py to refresh ``expected_blockers``; the TypeScript port is
then checked against the same file by ``workers/shared/test/review.test.ts``.
"""
import json
import sys
import unittest
from pathlib import Path

from campaign_tool import review

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "review-cases.json"


PLACEHOLDER = "@finding_digest"


def load_cases():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def resolved_receipts(case):
    """Receipts store ``@finding_digest`` instead of a literal content hash.

    The placeholder keeps high-entropy digests out of the tracked fixture (the CI
    secret scan flags them) while still binding each receipt to the exact finding.
    """
    digest = review.content_hash(case["finding"])
    return [dict(r, content_sha256=digest) if r.get("content_sha256") == PLACEHOLDER else r
            for r in case["receipts"]]


class ReviewFixtureTest(unittest.TestCase):
    def test_fixture_matches_python_reference(self):
        data = load_cases()
        self.assertGreaterEqual(len(data["cases"]), 10)
        names = [case["name"] for case in data["cases"]]
        self.assertEqual(len(names), len(set(names)), "case names must be unique")
        for case in data["cases"]:
            with self.subTest(case=case["name"]):
                blockers = review.review_blockers(case["finding"], resolved_receipts(case))
                self.assertEqual(blockers, case["expected_blockers"])

    def test_fixture_is_synthetic(self):
        text = FIXTURE.read_text(encoding="utf-8")
        self.assertIn("example.invalid", text)
        self.assertNotIn("@gmail.com", text)


def regenerate():
    data = load_cases()
    for case in data["cases"]:
        case["expected_blockers"] = review.review_blockers(case["finding"], resolved_receipts(case))
    FIXTURE.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
    print(f"regenerated {len(data['cases'])} cases in {FIXTURE}")


if __name__ == "__main__":
    if "--regenerate" in sys.argv:
        regenerate()
    else:
        unittest.main()
