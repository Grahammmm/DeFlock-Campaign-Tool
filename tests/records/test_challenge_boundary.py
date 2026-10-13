"""Synthetic challenge privacy and honest review receipts; no model or network."""
import copy
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from campaign_tool.digest.model import ModelConfig, ModelError
from campaign_tool.records import challenge, stage_adapters


def draft(locator=None, model_id="synthetic-primary"):
    return {
        "model_id": model_id,
        "privacy_tier": "redacted_cloud",
        "scope": "Synthetic extracted text",
        "actors": [],
        "dates": [],
        "statements": [{"text": "Records are retained for 30 days.",
                        "locator": copy.deepcopy(locator or {"page": 1})}],
        "omissions": [],
        "counterevidence": [],
        "conclusions": [],
    }


class ChallengeBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.units = [{"locator": {"page": 1}, "text": "Records are retained for 30 days."}]
        self.package = {"rules": []}
        self.config = challenge.ChallengeConfig(
            ModelConfig("https://synthetic.example.invalid/v1", model_id="synthetic-challenger"),
            primary_model_id="synthetic-primary")

    def check(self, digest=None, units=None, **kwargs):
        return challenge.challenge_review(digest or draft(), self.units if units is None else units,
                                          self.package, **kwargs)

    def assert_requires_factual_review(self, record):
        self.assertEqual(record["verdict"], "challenge")
        self.assertFalse(record["independent_review"])
        self.assertFalse(record["primary_evidence_inspected"])
        self.assertFalse(record["blind_first_pass"])
        self.assertIn("independent_factual_review_required", {d["reason"] for d in record["disputes"]})

    def test_unsupported_model_statement_with_valid_locator_cannot_certify_facts(self):
        digest = draft()
        digest["statements"][0]["text"] = "The agency deleted every log."
        record = self.check(digest)
        self.assertEqual(record["structural_verdict"], "pass")
        self.assertEqual(record["model_check_verdict"], "not_run")
        self.assert_requires_factual_review(record)
        with self.assertRaisesRegex(ValueError, "challenge_not_passed"):
            challenge.review_entry(record, "factual", "a" * 64, "b" * 64, [])

    def test_exact_detector_quote_is_structural_check_not_factual_review(self):
        record = self.check(draft(model_id=None))
        self.assertEqual(record["structural_verdict"], "pass")
        self.assert_requires_factual_review(record)

    def test_second_model_assent_is_not_blind_original_review(self):
        with patch.object(challenge, "chat_json", return_value={"disputes": [], "notes": "No objections."}):
            record = self.check(config=self.config)
        self.assertEqual(record["model_check_verdict"], "pass")
        self.assertEqual(record["evidence_basis"], "redacted_extracted_text")
        self.assert_requires_factual_review(record)

    def test_legacy_automated_pass_cannot_become_factual_receipt(self):
        record = {"verdict": "pass", "blind_first_pass": True, "independent_review": True}
        with self.assertRaisesRegex(ValueError, "independent_factual_review_required"):
            challenge.review_entry(record, "factual", "a" * 64, "b" * 64, [])

    def test_scanner_receipt_retains_binding_without_original_inspection_claim(self):
        record = challenge.challenge_privacy("Synthetic public policy text.")
        entry = challenge.review_entry(record, "privacy", "a" * 64, "b" * 64, ["public:full_text"])
        self.assertEqual(entry["content_sha256"], "a" * 64)
        self.assertEqual(entry["reviewed_primary_sha256"], "b" * 64)
        self.assertFalse(entry["blind_first_pass"])
        self.assertFalse(entry["independent_review"])
        self.assertFalse(entry["primary_evidence_inspected"])
        self.assertIn("Automated checks", entry["rationale"])
        self.assertNotIn("Independent challenge", entry["rationale"])

    def test_scanner_receipt_rejects_unchecked_caller_locators(self):
        record = challenge.challenge_privacy("Synthetic public policy text.")
        with self.assertRaisesRegex(ValueError, "challenge_locator_coverage_missing"):
            challenge.review_entry(record, "privacy", "a" * 64, "b" * 64, ["uninspected:page999"])

    def test_sources_and_digest_share_opaque_locators_without_mutation(self):
        private = {
            "page": 1, "sheet": "Synthetic Mira Voss", "kind": "arbitrary private value",
            "nested": {"path": "/private/fictional-person.pdf"},
            "download": "https://portal.example.invalid/file?token=synthetic-secret",
            "members": ["Synthetic member", {"email": "private@example.invalid"}],
        }
        units = [{"locator": private, "text": "Records are retained for 30 days.",
                  "model_locator": {"unsafe": "caller-supplied metadata"}, "sha256": "c" * 64}]
        digest = draft(private)
        digest["conclusions"] = [{"text": "Retention is stated.", "confidence": "likely",
                                  "sources": [{"sha256": "d" * 64, "locator": private, "rule_id": None}]}]
        originals = copy.deepcopy((digest, units))
        with patch.object(challenge, "chat_json", return_value={"disputes": []}) as chat:
            record = self.check(digest, units, config=self.config)
        self.assertEqual(record["model_check_verdict"], "pass")
        messages = chat.call_args.args[1]
        serialized = json.dumps(messages)
        for private_value in ("Synthetic Mira Voss", "arbitrary private value", "/private/fictional-person.pdf",
                              "synthetic-secret", "Synthetic member", "private@example.invalid",
                              "caller-supplied metadata", "c" * 64, "d" * 64):
            self.assertNotIn(private_value, serialized)
        sources = json.loads(messages[1]["content"].split("\n", 1)[1])
        model_digest = json.loads(messages[2]["content"].split("\n", 1)[1])
        source_locator = json.loads(sources["record_text"][0].split("\t", 1)[0])
        self.assertEqual(source_locator, {"unit": 0})
        self.assertEqual(model_digest["statements"][0]["locator"], source_locator)
        self.assertEqual(model_digest["conclusions"][0]["sources"][0]["locator"], source_locator)
        self.assertEqual((digest, units), originals)

    def test_defensive_source_text_redaction_and_denylist(self):
        units = [{"locator": {"page": 1},
                  "text": "Records are retained for 30 days. Call 805-555-0132. Synthetic Mira Voss."}]
        with patch.object(challenge, "chat_json", return_value={"disputes": []}) as chat:
            record = self.check(units=units, config=self.config, denylist=("Synthetic Mira Voss",))
        self.assertEqual(record["model_check_verdict"], "pass")
        serialized = json.dumps(chat.call_args.args[1])
        self.assertNotIn("805-555-0132", serialized)
        self.assertNotIn("Synthetic Mira Voss", serialized)
        self.assertIn("[PHONE-1]", serialized)
        self.assertIn("[DENYLIST-1]", serialized)

    def test_actor_date_and_note_identifiers_block_before_model_call(self):
        for field in ("actor", "date", "note"):
            with self.subTest(field=field):
                digest = draft()
                if field == "actor":
                    digest["actors"] = ["person@example.invalid"]
                else:
                    digest["dates"] = [{"date": "2026-01-01", "locator": {"page": 1}, "note": ""}]
                    digest["dates"][0][field] = "person@example.invalid"
                with patch.object(challenge, "chat_json") as chat:
                    record = self.check(digest, config=self.config)
                chat.assert_not_called()
                self.assertIn("unredacted_identifier_in_digest", {d["reason"] for d in record["disputes"]})

    def test_bad_locator_is_not_rescued_by_identical_text_elsewhere(self):
        digest = draft({"page": 99})
        record = self.check(digest)
        self.assertEqual(record["structural_verdict"], "challenge")
        self.assertIn("locator_not_in_sources", {d["reason"] for d in record["disputes"]})

    def test_detector_quote_must_appear_in_the_cited_unit(self):
        units = [*self.units, {"locator": {"page": 2}, "text": "This page says something different."}]
        record = self.check(draft({"page": 2}, model_id=None), units)
        self.assertIn("statement_not_quoted_from_sources", {d["reason"] for d in record["disputes"]})

    def test_bounded_line_locator_resolves_only_within_its_parent(self):
        units = [{"locator": {"page": 1}, "text": "Heading\nRecords are retained for 30 days."}]
        good = self.check(draft({"page": 1, "line": 2}, model_id=None), units)
        self.assertEqual(good["structural_verdict"], "pass")
        for locator in ({"page": 1, "line": 3}, {"page": 99, "line": 2}, {"page": 1, "line": True}):
            with self.subTest(locator=locator):
                bad = self.check(draft(locator, model_id=None), units)
                self.assertEqual(bad["structural_verdict"], "challenge")

    def test_conclusion_source_locator_is_checked(self):
        digest = draft()
        digest["conclusions"] = [{"text": "Retention is stated.", "confidence": "likely",
                                  "sources": [{"locator": {"page": 999}, "rule_id": None}]}]
        record = self.check(digest)
        self.assertIn({"field": "conclusions", "index": 0, "reason": "locator_not_in_sources"}, record["disputes"])

    def test_malformed_model_disputes_fail_closed(self):
        malformed = [None, [], {}, {"disputes": None}, {"disputes": ""}, {"disputes": {}},
                     {"disputes": [None]}, {"disputes": [{}]}, {"disputes": [], "notes": []},
                     {"disputes": [], "unexpected": True}]
        for item in ({"field": "statement", "index": 0, "reason": "No support"},
                     {"field": "statements", "index": True, "reason": "No support"},
                     {"field": "statements", "index": -1, "reason": "No support"},
                     {"field": "statements", "index": 99, "reason": "No support"},
                     {"field": "statements", "index": 0, "reason": " "},
                     {"field": "statements", "index": 0, "reason": 1},
                     {"field": "statements", "index": 0, "reason": "No support", "extra": True}):
            malformed.append({"disputes": [item]})
        for output in malformed:
            with self.subTest(output=output), patch.object(challenge, "chat_json", return_value=output):
                record = self.check(config=self.config)
                self.assertEqual(record["model_check_verdict"], "challenge")
                self.assertIn("challenge_model_output_invalid", {d["reason"] for d in record["disputes"]})
                self.assert_requires_factual_review(record)

    def test_well_formed_model_objection_is_preserved(self):
        output = {"disputes": [{"field": "statements", "index": 0, "reason": "Unsupported duration."}]}
        with patch.object(challenge, "chat_json", return_value=output):
            record = self.check(config=self.config)
        self.assertEqual(record["model_check_verdict"], "challenge")
        self.assertIn({"field": "statements", "index": 0, "reason": "model:Unsupported duration."}, record["disputes"])

    def test_model_error_details_do_not_enter_receipt(self):
        with patch.object(challenge, "chat_json", side_effect=ModelError("secret endpoint detail")):
            record = self.check(config=self.config)
        self.assertNotIn("secret endpoint detail", json.dumps(record))
        self.assertIn("challenge_model_unavailable", {d["reason"] for d in record["disputes"]})

    def test_cloud_transport_receives_only_serialized_challenge_views(self):
        locator = {"page": 1, "filename": "Synthetic Mira Voss.pdf"}
        requests = []

        def opener(request, timeout):
            requests.append(json.loads(request.data))
            envelope = {"choices": [{"message": {"content": '{"disputes": []}'}}]}
            return io.BytesIO(json.dumps(envelope).encode())

        record = self.check(draft(locator), [{"locator": locator, "text": self.units[0]["text"]}],
                            config=self.config, opener=opener)
        self.assertEqual(record["model_check_verdict"], "pass")
        self.assertEqual(len(requests), 1)
        self.assertNotIn("Synthetic Mira Voss.pdf", json.dumps(requests))
        self.assert_requires_factual_review(record)

    def test_unserializable_or_unseen_model_view_fails_before_call(self):
        digest = draft()
        digest["statements"] = []
        # Source truncation must not permit a citation to the omitted second unit.
        digest["conclusions"] = [{"text": "Retention is stated.", "confidence": "likely",
                                  "sources": [{"locator": {"page": 2}, "rule_id": None}]}]
        units = [{"locator": {"page": 1}, "text": "x" * 60000},
                 {"locator": {"page": 2}, "text": "Another page."}]
        with patch.object(challenge, "chat_json") as chat:
            record = self.check(digest, units, config=self.config)
        chat.assert_not_called()
        self.assertIn("challenge_model_input_invalid", {d["reason"] for d in record["disputes"]})
        self.assert_requires_factual_review(record)

    def test_digest_privacy_tier_cannot_be_downgraded_for_challenge(self):
        for tier in ("strict_local", None, "unknown", []):
            with self.subTest(tier=tier), patch.object(challenge, "chat_json") as chat:
                digest = draft()
                digest["privacy_tier"] = tier
                record = self.check(digest, config=self.config)
                chat.assert_not_called()
                self.assertEqual(record["model_check_verdict"], "challenge")
                self.assertIn("challenge_privacy_tier_mismatch", {d["reason"] for d in record["disputes"]})

    def test_existing_stage_adapter_blocks_without_issuing_factual_receipts(self):
        from tests.records.test_ledger_stages import StageTests

        with tempfile.TemporaryDirectory(prefix="synthetic-challenge-") as temporary:
            base = Path(temporary).resolve()
            with patch("tempfile.tempdir", str(base)):
                fixture = StageTests()
                fixture.setUp()
            self.addCleanup(fixture.doCleanups)
            fixture.advance("detect")
            record = self.check()
            result = stage_adapters.promote(
                fixture.runner, fixture.subject, "review", {"synthetic": True, "challenge": record},
                author_id="synthetic-analyst",
                coverage={"denominator": {"kind": "items", "total": 1}, "covered": 1, "scope": "full_text"},
                locators=['{"page": 1}'], challenges=[("factual", record)])
            self.assertEqual(result["status"], "blocked")
            self.assertTrue(result["disputed"])
            with sqlite3.connect(fixture.database) as connection:
                status, reason = connection.execute(
                    "SELECT status, reason FROM stage_state WHERE stage='review' AND original_sha256=?",
                    (fixture.subject,)).fetchone()
                self.assertEqual(status, "blocked")
                self.assertIn("independent_factual_review_required", reason)
                receipts = connection.execute(
                    "SELECT a.payload FROM receipts r JOIN stage_artifacts a ON a.sha256=r.sha256 WHERE r.stage='review'").fetchall()
                self.assertTrue(receipts)
                for (payload,) in receipts:
                    self.assertEqual(json.loads(bytes(payload))["reviews"], [])
                self.assertEqual(connection.execute("SELECT count(*) FROM proposals").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
