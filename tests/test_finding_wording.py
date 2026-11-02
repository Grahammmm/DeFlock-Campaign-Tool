"""DF2037-02: legal-conclusion wording in a finding's title, summary or body_md is a review blocker.

Synthetic findings, receipts and campaign only.
Run from the repository root: python3 -B -m unittest tests.test_finding_wording -v
"""
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import findings  # noqa: E402
from campaign_tool.content import ContentError, load_finding  # noqa: E402
from campaign_tool.review import content_hash, legal_conclusion_fields, review_blockers  # noqa: E402

EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign" / "content"
RECORDS = [b"synthetic wording record %d\n" % index for index in range(2)]
TERMS = ("violat", "illegal", "unlawful", "broke the law", "criminal")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def finding(**changes):
    base = {"id": "syn-word-1", "title": "Synthetic title", "slug": "synthetic-wording",
            "summary": "Synthetic summary.", "classification": "apparent_conflict", "confidence": "verified",
            "event_date": "2026-01-02", "rule_version": "Synthetic rule v1", "duty": "Synthetic duty.",
            "exceptions": ["None stated."], "sources": [{"locator": "page:1", "sha256": sha(RECORDS[0])},
                                                        {"locator": "page:2", "sha256": sha(RECORDS[1])}],
            "author": "Ada Lovelace", "limitations": ["Synthetic limits."], "counterevidence": ["None found."]}
    base.update(changes)
    return base


def receipt(doc, role, reviewer, decision="approve", digest=None):
    return {"content_sha256": digest or content_hash(doc), "role": role, "decision": decision,
            "reviewer": reviewer, "rationale": "Checked synthetic pages.", "reviewed_at": "2026-02-01T00:00:00Z"}


def all_receipts(doc):
    return [receipt(doc, "factual", "Rev One"), receipt(doc, "legal", "Rev Two"), receipt(doc, "privacy", "Rev One")]


def published(**changes):
    return finding(state="published", published_at="2026-02-02T00:00:00Z", corrections=[], **changes)


class WordingBlockerTests(unittest.TestCase):
    def test_apparent_conflict_with_all_receipts_is_blocked_by_field(self):
        doc = finding(summary="The department violated the policy.")
        self.assertEqual(review_blockers(doc, all_receipts(doc)), ["legal_conclusion_wording_in_summary"])
        clean = finding()
        self.assertEqual(review_blockers(clean, all_receipts(clean)), [])

    def test_every_non_confirmed_classification_is_blocked(self):
        for classification in ("documented_fact", "information_gap", "agency_assertion", "no_conflict"):
            with self.subTest(classification=classification):
                doc = finding(classification=classification, title="Illegal retention")
                self.assertIn("legal_conclusion_wording_in_title", review_blockers(doc, all_receipts(doc)))

    def test_confirmed_conflict_needs_a_current_legal_approve_receipt(self):
        doc = finding(classification="confirmed_conflict", body_md="Retention was unlawful.")
        self.assertEqual(review_blockers(doc, all_receipts(doc)), [])
        old = dict(doc, body_md="Retention was long.")
        cases = {
            "stale legal receipt": [receipt(doc, "factual", "Rev One"), receipt(old, "legal", "Rev Two"),
                                    receipt(doc, "privacy", "Rev One")],
            "legal reject": [receipt(doc, "factual", "Rev One"), receipt(doc, "legal", "Rev Two", "reject")],
            "legal changes_requested": [receipt(doc, "legal", "Rev Two", "changes_requested")],
            "legal receipt by the author": [receipt(doc, "factual", "Rev One"),
                                            receipt(doc, "legal", "ada  LOVELACE")],
            "no legal receipt": [receipt(doc, "factual", "Rev One"), receipt(doc, "privacy", "Rev Two")],
            "legal receipt without rationale": [dict(receipt(doc, "legal", "Rev Two"), rationale="")],
            "invalid receipts value": None,
        }
        for name, receipts in cases.items():
            with self.subTest(name):
                blockers = review_blockers(doc, receipts)
                self.assertIn("legal_conclusion_wording_in_body_md", blockers)
                self.assertTrue("missing_legal_review" in blockers or "unresolved_challenge" in blockers
                                or "invalid_receipts" in blockers, blockers)

    def test_confirmed_conflict_without_receipts_still_needs_legal_review(self):
        doc = finding(classification="confirmed_conflict", summary="They violated the rule.")
        blockers = review_blockers(doc, [])
        self.assertNotIn("legal_conclusion_wording_in_summary", blockers)
        self.assertIn("missing_legal_review", blockers)

    def test_term_forms(self):
        for text in ("A Violation occurred.", "Stored ILLEGALLY.", "Acted unlawfully.", "He broke  the\nlaw.",
                     "Criminally negligent.", "violated", "CRIMINAL"):
            with self.subTest(text=text):
                self.assertEqual(legal_conclusion_fields({"summary": text}), ["summary"])
        for text in ("A violet sign.", "A violin case.", "Lawful retention.", "An illegible page.",
                     "The law was cited.", "nonviolent", "broke the lawn"):
            with self.subTest(text=text):
                self.assertEqual(legal_conclusion_fields({"summary": text}), [])

    def test_obfuscation_is_normalised(self):
        for text in ("They vi\u200bolated it.", "ill\u00adegal", "\uff49\uff4c\uff4c\uff45\uff47\uff41\uff4c",
                     "un\u2060lawful", "broke\u200b the law"):
            with self.subTest(text=ascii(text)):
                doc = finding(summary=text)
                self.assertEqual(review_blockers(doc, all_receipts(doc)), ["legal_conclusion_wording_in_summary"])

    def test_two_fields_sorted_and_non_string_skipped(self):
        doc = finding(title="Illegal cameras", summary="It was criminal.", body_md=["violated", "illegal"])
        self.assertEqual(review_blockers(doc, all_receipts(doc)),
                         ["legal_conclusion_wording_in_summary", "legal_conclusion_wording_in_title"])
        doc = finding(title="Unlawful", summary="Unlawful.", body_md="Unlawful.")
        self.assertEqual(legal_conclusion_fields(doc), ["body_md", "summary", "title"])
        self.assertEqual(legal_conclusion_fields({"title": 7, "summary": None, "body_md": {"x": "illegal"}}), [])

    def test_blocker_names_never_carry_the_matched_text(self):
        doc = finding(title="Criminal", summary="violated; broke the law", body_md="ILLEGAL and unlawful")
        text = json.dumps(review_blockers(doc, all_receipts(doc))).lower()
        for term in TERMS:
            self.assertNotIn(term, text)


class LoadFindingTests(unittest.TestCase):
    def test_published_confirmed_conflict_builds(self):
        loaded = load_finding(published(classification="confirmed_conflict", summary="They violated the rule."),
                              "findings/x.json")
        self.assertEqual(loaded["summary"], "They violated the rule.")

    def test_published_documented_fact_with_legal_title_is_refused(self):
        with self.assertRaises(ContentError) as caught:
            load_finding(published(classification="documented_fact", title="Illegal retention"), "findings/x.json")
        self.assertIn("legal_conclusion_wording_in_title", str(caught.exception))
        self.assertNotIn("Illegal", str(caught.exception))


class WordingCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-finding-wording-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "campaign"
        self.cli("init", "--directory", self.root, "--name", "Synthetic", "--county", "Synthetic County",
                 "--state", "ca")
        self.content = self.root / "content"
        (self.content / "findings").mkdir(parents=True, exist_ok=True)
        shutil.copyfile(EXAMPLE / "site.json", self.content / "site.json")
        for data in RECORDS:
            path = self.base / ("record-%s.txt" % sha(data)[:8])
            path.write_bytes(data)
            self.cli("ingest", "--directory", self.root, "--file", path, "--source-id", "synthetic-source")
        entries = [{"sha256": sha(data), "title": "Synthetic record", "agency_id": "ca-synthetic",
                    "received_at": "2026-01-20T16:30:00+00:00", "public_url": None,
                    "fragment": "synthetic-records"} for data in RECORDS]
        (self.content / "sources.json").write_text(json.dumps(entries), encoding="utf-8")
        self.draft = self.base / "draft.json"

    def cli(self, *arguments, returncode=0):
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *map(str, arguments)],
                                cwd=REPO_ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, returncode, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        return result

    def reviewed_draft(self, **changes):
        document = dict(finding(**changes), state="draft", published_at=None, corrections=[])
        self.draft.write_text(json.dumps(document), encoding="utf-8")
        for role, reviewer in (("factual", "Rev One"), ("legal", "Rev Two"), ("privacy", "Rev One")):
            findings.add_receipt(self.root, document, role, "approve", reviewer, "Checked synthetic pages.")

    def published_files(self):
        return sorted(p.name for p in (self.content / "findings").iterdir())

    def publish(self, returncode):
        return self.cli("finding", "publish", self.draft, "--directory", self.root, "--approved-by",
                        "Pat Organizer", returncode=returncode)

    def test_review_status_lists_and_publish_refuses(self):
        self.reviewed_draft(summary="The department violated the policy.")
        status = self.cli("finding", "review-status", self.draft, "--directory", self.root)
        self.assertEqual(json.loads(status.stdout)["review_blockers"], ["legal_conclusion_wording_in_summary"])
        self.assertNotIn("violated", status.stdout)
        refused = self.publish(returncode=1)
        self.assertIn("legal_conclusion_wording_in_summary", refused.stderr)
        self.assertEqual(self.published_files(), [])
        self.assertFalse((self.root / "private" / "publication-log.jsonl").exists())

    def test_confirmed_conflict_with_legal_approval_publishes(self):
        self.reviewed_draft(classification="confirmed_conflict", summary="The department violated the policy.")
        status = self.cli("finding", "review-status", self.draft, "--directory", self.root)
        self.assertEqual(json.loads(status.stdout)["review_blockers"], [])
        self.publish(returncode=0)
        self.assertEqual(self.published_files(), ["synthetic-wording.json"])


if __name__ == "__main__":
    unittest.main()
