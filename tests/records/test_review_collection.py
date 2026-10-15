"""C01 original collection/CLI/batch acceptance, synthetic source fixtures only."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from campaign_tool.records.gates import validate_findings as gate
from campaign_tool.records.gates.tests import test_gate as fixtures


class ReviewCollectionTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.ReviewGateTest("runTest")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        self.finding, self.reviews = fixture.f, fixture.reviews()
        self.assertTrue(gate.gate(self.finding, self.reviews, True)["ready"])
        folder = tempfile.TemporaryDirectory(prefix="review-collection-synthetic-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.agency = self.root / "agencies" / "synthetic"
        self.agency.mkdir(parents=True)
        (self.agency / "findings.json").write_text(json.dumps([self.finding]))
        (self.agency / "document-digests.jsonl").write_text("")
        self.collected = self.agency / "independent-reviews.json"

    def cli(self, *args):
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool.records",
            *map(str, args)], capture_output=True, text=True, timeout=10)

    def write(self, name, value):
        (self.agency / name).write_text(json.dumps(value))

    def collect(self):
        result = self.cli("collect-reviews", self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(self.collected.read_text())

    def check_consumers(self, *, ready):
        result = self.cli("validate-findings", self.agency / "findings.json",
            "--reviews", self.collected, "--check-files", "--require-ready")
        self.assertEqual(result.returncode, 0 if ready else 1, result.stderr)
        self.assertEqual(json.loads(result.stdout)["ready"], int(ready))
        report = self.root / "batch.json"
        result = self.cli("batch-report", self.root, "--check-files", "--output", report)
        self.assertEqual(result.returncode, 0, result.stderr)
        batch = json.loads(report.read_text())
        self.assertEqual(batch["publication_ready"], int(ready))
        return batch

    def challenge(self):
        challenge = copy.deepcopy(self.reviews[0])
        challenge.update(verdict="challenge", rationale="Synthetic contradiction")
        return challenge

    def test_challenge_in_either_supported_filename_cannot_be_shadowed(self):
        for passes, challenges in (("gate-reviews.json", "factual-review.json"),
                                   ("factual-review.json", "gate-reviews.json")):
            with self.subTest(challenges=challenges):
                self.write(passes, self.reviews)
                self.write(challenges, [self.challenge()])
                collected = self.collect()
                self.assertIn(self.challenge(), collected["reviews"])
                self.assertEqual(len(collected["receipt_sources"]), 2)
                self.check_consumers(ready=False)

    def test_malformed_challenge_errors_block_cli_and_batch(self):
        malformed = self.challenge()
        del malformed["reviewed_at"]
        for name in ("gate-reviews.json", "factual-review.json",
                     "legal-review.json", "privacy-review.json"):
            with self.subTest(source=name):
                for old in self.agency.glob("*review*.json"):
                    old.unlink()
                passes = "factual-review.json" if name == "gate-reviews.json" else "gate-reviews.json"
                self.write(passes, self.reviews)
                self.write(name, [malformed])
                collected = self.collect()
                self.assertEqual(collected["reviews"], self.reviews)
                self.assertTrue(collected["collection_errors"])
                batch = self.check_consumers(ready=False)
                self.assertTrue(batch["errors"])
                self.assertIn("review_collection_failed", batch["agencies"][0]["gates"][0]["blockers"])

    def test_malformed_json_and_envelopes_are_not_a_passing_subset(self):
        self.write("gate-reviews.json", self.reviews)
        path = self.agency / "factual-review.json"
        for raw in ('{', 'null', '{"reviews":null}', '{"approved":true}'):
            with self.subTest(raw=raw):
                path.write_text(raw)
                collected = self.collect()
                self.assertTrue(collected["collection_errors"])
                self.check_consumers(ready=False)

    def test_exact_duplicates_only_deduplicate_and_healthy_control_stays_ready(self):
        self.write("gate-reviews.json", self.reviews)
        reordered = [dict(reversed(list(review.items()))) for review in self.reviews]
        self.write("factual-review.json", {"reviews": reordered})
        collected = self.collect()
        self.assertEqual(collected["reviews"], self.reviews)
        self.assertEqual(collected["collection_errors"], [])
        self.assertEqual(len(collected["receipt_sources"]), 2)
        self.check_consumers(ready=True)
        self.write("factual-review.json", self.reviews + [self.challenge()])
        self.assertEqual(len(self.collect()["reviews"]), 4)
        self.check_consumers(ready=False)

    def test_all_four_sources_are_collected_without_role_reassignment(self):
        for name in ("gate-reviews.json", "factual-review.json",
                     "legal-review.json", "privacy-review.json"):
            self.write(name, self.reviews)
        collected = self.collect()
        self.assertEqual(collected["reviews"], self.reviews)
        self.assertEqual(len(collected["receipt_sources"]), 4)
        self.check_consumers(ready=True)

    def test_unreadable_review_source_overwrites_stale_passing_collection_with_error(self):
        self.write("gate-reviews.json", self.reviews)
        self.collect()
        self.check_consumers(ready=True)
        (self.agency / "legal-review.json").mkdir()
        collected = self.collect()
        self.assertTrue(collected["collection_errors"])
        self.check_consumers(ready=False)

    def test_nested_and_malformed_collection_errors_block_recollection_and_consumers(self):
        for errors in ([{"issue": "synthetic prior failure"}], None, {}, False):
            with self.subTest(errors=errors):
                self.write("gate-reviews.json", {"reviews": self.reviews, "collection_errors": errors})
                self.assertTrue(self.collect()["collection_errors"])
                self.check_consumers(ready=False)
                # Direct consumers must also reject an envelope not made by this collector.
                self.collected.write_text(json.dumps({"reviews": self.reviews, "collection_errors": errors}))
                self.check_consumers(ready=False)

    def test_array_and_clean_envelope_remain_supported(self):
        for payload in (self.reviews, {"reviews": self.reviews, "collection_errors": []}):
            with self.subTest(envelope=isinstance(payload, dict)):
                self.collected.write_text(json.dumps(payload))
                self.check_consumers(ready=True)
