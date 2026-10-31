"""review_blockers compares identities after casefold + whitespace collapse and
blocks (never raises) on malformed JSON-shaped findings or receipts. Synthetic only."""
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from campaign_tool import review
from campaign_tool.content import ContentError, load_content

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "fictional-campaign"
ROLE_BLOCKERS = {"missing_factual_review", "missing_legal_review", "missing_privacy_review",
                 "need_two_independent_reviewers"}


def finding():
    return {
        "id": "synthetic-finding", "author": "synthetic-author",
        "classification": "documented_fact", "summary": "A synthetic policy was produced.",
        "sources": [{"sha256": hashlib.sha256(b"Synthetic policy only.\n").hexdigest(),
                     "locator": "page:1"}],
        "limitations": ["Synthetic fixture; not a real finding."], "counterevidence": [],
    }


def receipt(item, role, reviewer, decision="approve"):
    return {"content_sha256": review.content_hash(item), "decision": decision,
            "reviewer": reviewer, "role": role, "rationale": "Synthetic structural check only.",
            "reviewed_at": "2026-01-01T00:00:00+00:00"}


def receipts(item, a="synthetic-reviewer-a", b="synthetic-reviewer-b", c="synthetic-reviewer-a"):
    return [receipt(item, "factual", a), receipt(item, "legal", b), receipt(item, "privacy", c)]


class IdentityTests(unittest.TestCase):
    def test_golden_unchanged(self):
        item = finding()
        self.assertEqual(review.review_blockers(item, receipts(item)), [])

    def test_author_look_alike_never_counts(self):
        item = finding()
        for alias in (" Synthetic-Author ", "SYNTHETIC-AUTHOR", "synthetic-author\t", "Synthetic-author"):
            with self.subTest(alias=alias):
                blockers = review.review_blockers(item, receipts(item, a=alias))
                self.assertIn("missing_factual_review", blockers)
                self.assertNotIn("missing_legal_review", blockers)

    def test_author_internal_whitespace_collapsed(self):
        item = {**finding(), "author": "Synthetic  Author"}
        blockers = review.review_blockers(item, receipts(item, b=" synthetic\tauthor "))
        self.assertIn("missing_legal_review", blockers)

    def test_same_reviewer_two_spellings_is_one_reviewer(self):
        item = finding()
        for b in ("Reviewer-A", " reviewer-a ", "REVIEWER-A"):
            with self.subTest(b=b):
                blockers = review.review_blockers(item, receipts(item, a="reviewer-a", b=b, c="reviewer-a"))
                self.assertEqual(blockers, ["need_two_independent_reviewers"])

    def test_bad_identities_never_count(self):
        item = finding()
        for bad in ("", "   ", None, 42, ["x"], {"name": "x"}, True):
            with self.subTest(bad=bad):
                blockers = review.review_blockers(item, receipts(item, b=bad))
                self.assertIn("missing_legal_review", blockers)
                self.assertIn("need_two_independent_reviewers", blockers)
                missing = receipts(item)
                del missing[1]["reviewer"]
                self.assertIn("missing_legal_review", review.review_blockers(item, missing))

    def test_non_string_author_never_matches_reviewer(self):
        for author in (None, 42, ["synthetic-reviewer-a"]):
            with self.subTest(author=author):
                item = {**finding(), "author": author}
                self.assertEqual(review.review_blockers(item, receipts(item)), [])

    def test_challenge_from_other_spelling_still_wins(self):
        item = finding()
        rows = receipts(item) + [receipt(item, "legal", "SYNTHETIC-REVIEWER-B", "reject")]
        self.assertEqual(review.review_blockers(item, rows), ["unresolved_challenge"])
        rows[-1]["decision"] = "changes_requested"
        self.assertEqual(review.review_blockers(item, rows), ["unresolved_challenge"])
        stale = receipts(item) + [dict(rows[-1], content_sha256="0" * 64)]
        self.assertEqual(review.review_blockers(item, stale), [])


class MalformedInputTests(unittest.TestCase):
    def test_receipts_not_a_list(self):
        item = finding()
        for bad in (None, "approve", {"reviewer": "x"}, 7):
            with self.subTest(bad=bad):
                self.assertEqual(set(review.review_blockers(item, bad)), ROLE_BLOCKERS | {"invalid_receipts"})

    def test_non_dict_receipts_blocked_others_evaluated(self):
        item = finding()
        blockers = review.review_blockers(item, ["approve", {}] + receipts(item))
        self.assertEqual(blockers, ["invalid_receipt"])
        blockers = review.review_blockers(item, [None, 3, ["x"], receipts(item)[0]])
        self.assertEqual(set(blockers), {"invalid_receipt", "missing_legal_review",
                                         "missing_privacy_review", "need_two_independent_reviewers"})

    def test_unhashable_receipt_fields_do_not_raise(self):
        item = finding()
        rows = receipts(item)
        rows[0]["role"] = ["factual"]
        rows[1]["decision"] = ["reject"]
        self.assertEqual(set(review.review_blockers(item, rows)),
                         {"missing_factual_review", "missing_legal_review",
                          "need_two_independent_reviewers"})

    def test_finding_not_a_dict(self):
        for bad in ([], None, "finding", 3, [finding()]):
            with self.subTest(bad=bad):
                self.assertEqual(review.review_blockers(bad, []), ["invalid_finding"])
                self.assertEqual(review.review_blockers(bad, None), ["invalid_finding"])

    def test_unhashable_finding(self):
        base = finding()
        good = receipts(base)
        for key, value in (("summary", float("nan")), ("limitations", [float("inf")]),
                           ("counterevidence", [float("-inf")]), ("extra", {1, 2}), ("extra", b"x")):
            with self.subTest(key=key, value=value):
                item = {**base, key: value}
                rows = good + [dict(good[0], decision="reject")]
                blockers = review.review_blockers(item, rows)
                self.assertIn("unhashable_finding", blockers)
                self.assertLessEqual(ROLE_BLOCKERS, set(blockers))
                self.assertNotIn("unresolved_challenge", blockers)

    def test_unhashable_classification_does_not_raise(self):
        for bad in (["documented_fact"], {"a": 1}, None, 5):
            with self.subTest(bad=bad):
                item = {**finding(), "classification": bad}
                self.assertEqual(review.review_blockers(item, receipts(item)), ["unknown_classification"])


class ContentPathTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-content-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.path = self.root / "content" / "findings" / "cedar-policy-posted.json"

    def test_nan_in_content_finding_is_content_error(self):
        self.assertIsNotNone(load_content(self.root))
        original = json.loads(self.path.read_text())
        for key in ("limitations", "counterevidence"):
            with self.subTest(key=key):
                document = {**original, key: [float("nan")]}
                self.path.write_text(json.dumps(document))
                self.assertIn("NaN", self.path.read_text())
                with self.assertRaisesRegex(ContentError, r"review blockers \['unhashable_finding'\]"):
                    load_content(self.root)


if __name__ == "__main__":
    unittest.main()
