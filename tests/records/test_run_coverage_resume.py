"""Synthetic primary-request accounting, persistence and identity boundaries."""
from datetime import date
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from campaign_tool.digest.model import ModelConfig, ModelError
from campaign_tool.records import challenge, context_runtime, runner_coverage
from campaign_tool.records.run import Pipeline, encoded, root_lock, rules_in_force
from tests.records.test_run_slice import FakeModelServer, synthetic_email


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class RunCoverageResumeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="coverage-resume-")
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        inbox = base / "inbox"
        inbox.mkdir(mode=0o700)
        (inbox / "synthetic.eml").write_bytes(synthetic_email())
        self.primary = FakeModelServer(self.answer)
        self.addCleanup(self.primary.close)
        self.second = FakeModelServer({"disputes": [], "notes": "synthetic check"})
        self.addCleanup(self.second.close)
        model = ModelConfig(base_url=self.primary.url, model_id="synthetic-primary", privacy_tier="strict_local")
        second = challenge.ChallengeConfig(ModelConfig(base_url=self.second.url,
            model_id="synthetic-second", privacy_tier="strict_local"), primary_model_id=model.model_id)
        self.pipeline = Pipeline(base / "root", model=model, challenge=second)
        dates = patch.object(Pipeline, "_event_date", return_value=date(2025, 7, 1))
        dates.start()
        self.addCleanup(dates.stop)
        self.report = self.pipeline.run(inbox)
        self.subjects = [r["sha256"] for r in self.pipeline._query("SELECT sha256 FROM originals", ())]
        self.subject = next(s for s in self.subjects if self.pipeline._content(s, "review")["digest"]["duties"])

    @staticmethod
    def user(body):
        return next(m["content"] for m in body["messages"] if m["role"] == "user")

    @staticmethod
    def answer(body):
        lines = json.loads(RunCoverageResumeTests.user(body))["record_text"]
        line = next((s for s in lines if "retained for 90 days" in s),
                    next(s for s in lines if s.split("\t", 1)[1].strip()))
        reference, text = line.split("\t", 1)
        locator = json.loads(reference)
        return {"scope": "Synthetic source", "actors": [], "dates": [],
            "statements": [{"text": text, "locator": locator}], "omissions": [], "counterevidence": [],
            "conclusions": [{"text": "Synthetic source observation.", "confidence": "likely",
                             "sources": [{"locator": locator, "rule_id": None}]}]}

    def direct(self, name="review", content=None):
        p = self.pipeline
        self.pipeline = Pipeline(p.root.path, model=p.model, challenge=p.challenge,
                                 redaction_allowlist=p.allowlist, redaction_denylist=p.denylist)
        with root_lock(self.pipeline.root.path):
            stage = self.pipeline._open_stage_run()
            try:
                if content is not None:
                    return stage["runner"].set_content(self.subject, "review", encoded(content),
                        author_id="records-digest", tier="A")
                return getattr(self.pipeline, "stage_" + name)(self.subject, stage)
            finally:
                self.pipeline._close_stage_run({"synthetic_coverage_test": True})

    def content(self):
        return self.pipeline._content(self.subject, "review")

    def revisions(self):
        return self.pipeline._query("SELECT count(*) AS n FROM stage_content WHERE subject_sha256=? AND stage='review'",
                                    (self.subject,))[0]["n"]

    def test_recorded_hash_and_ranges_equal_actual_primary_not_challenger(self):
        self.assertEqual(len(self.primary.requests), len(self.subjects))
        self.assertGreaterEqual(len(self.second.requests), 1)
        for subject in self.subjects:
            content = self.pipeline._content(subject, "review")
            progress = content["processing_coverage"]
            request = next(self.user(b) for b in self.primary.requests
                           if sha(self.user(b)) == progress["request"]["request_sha256"])
            units = self.pipeline._unit_view(subject)[0]
            indexes = [json.loads(s.split("\t", 1)[0])["unit"] for s in json.loads(request)["record_text"]]
            emitted = {r["unit_id"] for a in progress["checkpoint"]["attempts"] for r in a["visible_ranges"]}
            self.assertEqual(emitted, {units[i]["unit_id"] for i in indexes})
            self.assertEqual(progress["request"]["unverified_units"], 0)
            self.assertEqual(progress["request"]["verified_units"], len(indexes))
            self.assertEqual(progress["status"], "emission_complete")
            self.assertTrue(progress["model_emission_recorded"])
            self.assertTrue(progress["review_required"])
            self.assertFalse(progress["semantic_review_established"])
            summary = progress["summary"]
            self.assertEqual((summary["accepted_units"], summary["status"], summary["interrupted_chunks"]), (0, "partial", 0))
            self.assertEqual(summary["receipt_coverage"]["covered"], 0)
            self.assertEqual(self.pipeline._states(subject)["review"]["status"], "blocked")
            self.assertTrue(all(a["accepted_ranges"] == [] for a in progress["checkpoint"]["attempts"]))
            for body in self.second.requests:
                self.assertNotEqual(sha(self.user(body)), progress["request"]["request_sha256"])
        self.assertEqual(self.pipeline._query("SELECT count(*) AS n FROM receipts WHERE stage='review' AND verdict='pass'", ())[0]["n"], 0)

    def test_resume_interrupt_replay_and_changed_request_preserve_union(self):
        first = self.content()["processing_coverage"]
        self.direct()
        second = self.content()["processing_coverage"]
        self.assertEqual(second["resume"], {"status": "resumed"})
        self.assertEqual(second["checkpoint"], first["checkpoint"])
        count = self.revisions()
        with patch("campaign_tool.records.run.build_digest", side_effect=ModelError("synthetic_interruption")):
            with self.assertRaises(ModelError):
                self.direct()
        self.assertEqual(self.revisions(), count)
        self.assertEqual(self.content()["processing_coverage"]["checkpoint"], second["checkpoint"])
        self.direct()
        fourth = self.content()["processing_coverage"]
        self.assertEqual(fourth["resume"], {"status": "resumed"})
        self.assertEqual(fourth["checkpoint"], second["checkpoint"])
        rules = rules_in_force(self.pipeline.package, date(2025, 7, 1))
        with patch("campaign_tool.records.run.rules_in_force", return_value=rules[:-1]), \
                patch("campaign_tool.digest.build.rules_in_force", return_value=rules[:-1]):
            self.direct()
        changed = self.content()["processing_coverage"]
        self.assertGreater(len(changed["checkpoint"]["attempts"]), len(second["checkpoint"]["attempts"]))
        self.assertEqual(changed["summary"]["emitted_chars"], second["summary"]["emitted_chars"])

    def test_stale_tampered_and_missing_checkpoints_are_visibly_rejected(self):
        prior = self.content()["processing_coverage"]
        self.pipeline.denylist = ("synthetic-private-phrase",)
        self.direct()
        fresh = self.content()["processing_coverage"]
        self.assertEqual(fresh["resume"], {"status": "rejected", "reason": "checkpoint_binding"})
        self.assertEqual(len(fresh["checkpoint"]["attempts"]), len(prior["checkpoint"]["attempts"]))
        content = self.content()
        content["processing_coverage"]["checkpoint"]["attempts"][0]["completed"] = False
        self.direct(content=content)
        self.direct()
        self.assertEqual(self.content()["processing_coverage"]["resume"],
                         {"status": "rejected", "reason": "checkpoint_sha256_mismatch"})
        for broken in ({}, {"checkpoint": None}, {"checkpoint": []}, None):
            content = self.content()
            content["processing_coverage"] = broken
            self.direct(content=content)
            self.direct()
            self.assertEqual(self.content()["processing_coverage"]["resume"]["status"], "rejected")

    def test_source_request_and_policy_comparison_identities_survive_resume(self):
        first = self.content()
        before = context_runtime.snapshot(self.pipeline, self.subject)["context"]["evidence_context"]
        counts = [self.pipeline._query("SELECT count(*) AS n FROM " + table, ())[0]["n"]
                  for table in ("occurrences", "originals", "requests")]
        self.direct()
        second = self.content()
        after = context_runtime.snapshot(self.pipeline, self.subject)["context"]["evidence_context"]
        for key in ("occurrences", "joins", "requests"):
            self.assertEqual(before[key], after[key])
        self.assertEqual(counts, [self.pipeline._query("SELECT count(*) AS n FROM " + table, ())[0]["n"]
                                  for table in ("occurrences", "originals", "requests")])
        print("Synthetic requests count:", counts[2])
        self.assertEqual(first["digest"], second["digest"])
        units = self.pipeline._unit_view(self.subject)[0]
        request = self.primary.requests[-1]
        answer = self.answer(request)
        self.assertEqual(second["digest"]["statements"][0]["locator"],
                         units[answer["statements"][0]["locator"]["unit"]]["locator"])
        self.assertEqual(second["digest"]["conclusions"][0]["sources"][0]["locator"],
                         units[answer["conclusions"][0]["sources"][0]["locator"]["unit"]]["locator"])
        self.direct("compare")
        compare = self.pipeline._content(self.subject, "compare")
        self.assertEqual(compare["review_content_sha256"], self.pipeline._states(self.subject)["review"]["receipt_sha256"])
        self.assertGreaterEqual(len(compare["comparisons"]), 1)
        duties = {d["rule_id"]: d for d in second["digest"]["duties"]}
        for row in compare["comparisons"]:
            duty = duties[row["rule_id"]]
            self.assertEqual(row["citation"], duty["citation"])
            self.assertEqual(row["evidence"], [{"sha256": self.subject, "locator": loc} for loc in duty["locators"][:10]])

    def test_request_shape_fault_writes_no_review_content(self):
        prior, count = self.content(), self.revisions()
        with patch("campaign_tool.digest.model.build_user_message", return_value="not-json"):
            with self.assertRaisesRegex(ValueError, "^request_shape$"):
                self.direct()
        self.assertEqual(self.revisions(), count)
        self.assertEqual(self.content(), prior)
