"""Small synthetic stage controller fixtures; no corpus or real promotions."""

import hashlib
import json
import sqlite3
import unittest
from unittest import mock

from campaign_tool.records.ledger import store, stages
from tests.records.test_ledger import LedgerTests


class StageTests(unittest.TestCase):
    def setUp(self):
        self.fixture = LedgerTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.database = self.fixture.output
        self.run = store.import_legacy(self.fixture.snapshot, self.database)["import_id"]
        with store.ledger(self.database) as c:
            self.subject = c.execute("SELECT sha256 FROM originals").fetchone()[0]
            # Synthetic original bytes need not reverse a hash from the import fixture.
            self.original = b"small representative synthetic original"
            self.sample_sha = hashlib.sha256(self.original).hexdigest()
            original = dict(c.execute("SELECT * FROM originals").fetchone())
            original.update(sha256=self.sample_sha, bytes=len(self.original))
            columns = list(original)
            c.execute("INSERT INTO originals(" + ",".join(columns) + ") VALUES(" +
                      ",".join("?" for _ in columns) + ")", [original[k] for k in columns])
            for stage in stages.STAGES:
                c.execute("INSERT INTO stage_state VALUES(?,?,'pending',NULL,?,?,?,?)",
                          (self.sample_sha, stage, "synthetic", store.now(), self.run, "synthetic pending"))
            c.execute("INSERT INTO occurrences VALUES(?,?,?,?,?,?,?,?)",
                      ("synthetic-local", self.sample_sha, "local", "synthetic-local",
                       None, None, "synthetic", "{}"))
            c.commit()
        self.subject = self.sample_sha
        self.tier = "A"
        self.config_hash = hashlib.sha256(b"synthetic installed configuration").hexdigest()
        self.run = self.new_run("synthetic-run")
        self.runner = self.make_runner(self.run)

    def new_run(self, run_id):
        with store.ledger(self.database) as c:
            c.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (run_id,"stage-runner",store.now(),None,"synthetic-engine-v1",
                       None,self.config_hash,None,"running","{}"))
            c.commit()
        return run_id

    def make_runner(self, run_id, owner="runner", version="synthetic-v1"):
        def verify(context):
            return bool(context["content"]) and context["receipt"]["rationale"] != "reject synthetic evidence"
        return stages.testing_runner(
            self.database,run_id=run_id,owner=owner,engine_version="synthetic-engine-v1",
            config_sha256=self.config_hash,validators={stage:verify for stage in stages.STAGES},version=version)

    def rows(self, table):
        with store.ledger(self.database) as c:
            return [dict(row) for row in c.execute("SELECT * FROM " + table)]

    def prepare(self, stage, content=None, author="author", tier=None):
        if content is None:
            content = self.original if stage == "preserve" else ("synthetic " + stage).encode()
        return self.runner.set_content(self.subject, stage, content, author_id=author, tier=tier or self.tier)

    def packet(self, stage, **changes):
        with store.ledger(self.database) as c:
            head = c.execute("SELECT * FROM stage_content WHERE subject_sha256=? AND stage=? "
                             "ORDER BY revision DESC LIMIT 1", (self.subject, stage)).fetchone()
            states = {r["stage"]: r for r in c.execute(
                "SELECT * FROM stage_state WHERE original_sha256=?", (self.subject,))}
        inputs = {"original": self.subject}
        for prerequisite in stages.PREREQUISITES[stage]:
            inputs[prerequisite] = states[prerequisite]["receipt_sha256"]
        packet = {
            "schema": "ledger-stage-receipt-v1", "subject_sha256": self.subject,
            "stage": stage, "content_sha256": head["content_sha256"], "tier": head["tier"],
            "author_id": head["author_id"], "reviewer_id": "reviewer1" if stage in
            ("review", "compare", "privacy") else "runner",
            "role": stages.ROLES[stage], "verdict": "pass",
            "coverage": {"denominator": {"kind": "items", "total": 1},
                         "covered": 1, "scope": "full_text"},
            "locators": ["synthetic item 1"], "rationale": "synthetic receipt",
            "model_or_tool": "synthetic-validator-v1", "created_at_tz": "2026-09-30T00:00:00+00:00",
            "input_hashes": inputs, "reviews": [],
        }
        roles = {"review": ("factual",), "compare": ("legal",),
                 "privacy": ("factual", "privacy") if head["tier"] == "A"
                 else ("factual", "legal", "privacy")}.get(stage, ())
        for role in roles:
            packet["reviews"].append({
                "reviewer_id": "reviewer2" if role == "legal" else "reviewer1",
                "role": role, "verdict": "pass", "content_sha256": head["content_sha256"],
                "reviewed_primary_sha256": self.subject, "checked_locators": packet["locators"],
                "reviewed_at": "2026-09-30T00:00:00+00:00", "rationale": "synthetic check",
                "blind_first_pass": True,
            })
        packet.update(changes)
        return packet

    def submit(self, stage, packet=None):
        packet = packet or self.packet(stage)
        raw = json.dumps(packet).encode()
        digest = hashlib.sha256(raw).hexdigest()
        current = next(row for row in self.rows("stage_state")
                       if row["original_sha256"] == self.subject and row["stage"] == stage)
        claim_id = None
        if current["receipt_sha256"] != digest:
            claim_id = self.runner.claim(self.subject, stage, supersedes=packet.get("supersedes"))["claim_id"]
        return self.runner.promote(self.subject, stage, raw, claim_id=claim_id)

    def advance(self, until, tier="A"):
        self.tier = tier
        for stage in stages.STAGES:
            self.prepare(stage)
            self.submit(stage)
            if stage == until:
                break

    def test_pending_counts_exact_seven(self):
        result = stages.counts(self.database)
        self.assertEqual(result["originals"], 2)
        self.assertTrue(result["candidate"])
        self.assertEqual(result["acceptance"], "candidate_with_explicit_validation_authority")
        self.assertEqual(result["verified_seven_stage_complete"], 0)
        self.assertEqual(stages.query_counts(self.database)["end_to_end_complete"], 0)
        self.assertEqual(result["stage_slots_observed"], 14)
        self.assertEqual(result["end_to_end_complete"], 0)
        self.assertTrue(all(value["pending"] == 2 for value in result["stages"].values()))

    def test_tier_a_sequence_and_history(self):
        self.advance("privacy")
        result = stages.counts(self.database)
        self.assertEqual(result["end_to_end_complete"], 0)
        self.assertEqual(result["synthetic_seven_stage_complete"], 1)
        self.assertEqual(result["verified_seven_stage_complete"], 0)
        self.assertEqual(result["stages"]["privacy"]["pending"], 1)
        self.assertFalse(result["publication_ready"])
        self.assertFalse(result["owner_approval"])
        self.assertEqual(len(self.rows("stage_transitions")), 7)

    def test_tier_b_two_distinct_reviewers(self):
        self.advance("privacy", tier="B")
        self.assertEqual(stages.counts(self.database)["end_to_end_complete"], 0)
        self.assertEqual(stages.counts(self.database)["synthetic_seven_stage_complete"], 1)

    def test_missing_trusted_validator_cannot_promote(self):
        self.prepare("preserve")
        with self.assertRaises(TypeError):
            self.runner.promote(self.subject,"preserve",json.dumps(self.packet("preserve")).encode(),
                                validator=lambda context:True,validator_id="invented")
        self.assertEqual(stages.counts(self.database)["stages"]["preserve"]["done"],0)

    def test_wrong_stage_subject_or_content(self):
        self.prepare("preserve")
        for changes in ({"stage": "extract"}, {"subject_sha256": "f" * 64},
                        {"content_sha256": "e" * 64}, {"role": "author"}):
            packet = self.packet("preserve")
            packet.update(changes)
            with self.subTest(changes=changes), self.assertRaises(stages.StageError):
                self.submit("preserve", packet)
        self.assertEqual(len(self.rows("stage_transitions")), 0)

    def test_prerequisite_failure_does_not_change_state(self):
        self.prepare("extract")
        before = self.rows("stage_state")
        with self.assertRaisesRegex(stages.StageError, "prerequisite_preserve"):
            self.submit("extract")
        self.assertEqual(self.rows("stage_state"), before)
        self.assertEqual(len(self.rows("stage_transitions")), 0)

    def test_catalog_allowed_after_blocked_extraction(self):
        self.advance("preserve")
        self.prepare("extract")
        self.submit("extract", self.packet("extract", verdict="blocked", reason="unsupported synthetic"))
        self.prepare("catalog")
        self.submit("catalog")
        result = stages.counts(self.database)
        self.assertEqual(result["stages"]["extract"]["blocked"], 1)
        self.assertEqual(result["stages"]["catalog"]["done"], 1)
        self.assertEqual(result["end_to_end_complete"], 0)

    def test_receipt_replay_no_extra_transition_or_history(self):
        self.prepare("preserve")
        packet = self.packet("preserve")
        first = self.submit("preserve", packet)
        history = self.rows("stage_events")
        second = self.submit("preserve", packet)
        self.assertEqual(first["receipt_sha256"], second["receipt_sha256"])
        self.assertTrue(second["reused"])
        self.assertEqual(history, self.rows("stage_events"))
        self.assertEqual(len(self.rows("stage_transitions")), 1)

    def test_case_alias_self_review_rejected(self):
        self.advance("detect")
        self.prepare("review")
        packet = self.packet("review")
        packet["reviews"][0]["reviewer_id"] = "AUTHOR"
        with self.assertRaisesRegex(stages.StageError, "self_review"):
            self.submit("review", packet)

    def test_ambiguous_or_missing_independent_roles(self):
        self.advance("detect")
        self.prepare("review")
        for reviews in ([], [{"reviewer_id": "not an identity"}]):
            with self.assertRaises(stages.StageError):
                self.submit("review", self.packet("review", reviews=reviews))

    def test_tier_b_same_reviewer_for_all_roles_rejected(self):
        self.advance("compare", tier="B")
        self.prepare("privacy")
        packet = self.packet("privacy")
        for review in packet["reviews"]:
            review["reviewer_id"] = "Reviewer1"
        with self.assertRaisesRegex(stages.StageError, "tier_b_two_reviewers"):
            self.submit("privacy", packet)

    def test_challenge_and_hold_prevent_done(self):
        self.advance("detect")
        self.prepare("review")
        packet = self.packet("review")
        packet["reviews"][0]["verdict"] = "challenge"
        with self.assertRaisesRegex(stages.StageError, "unresolved_review"):
            self.submit("review", packet)
        with self.assertRaisesRegex(stages.StageError, "unresolved_hold"):
            self.submit("review", self.packet("review", holds=["synthetic hold"]))

    def test_changed_digest_reopens_dependencies_not_preserve(self):
        self.advance("privacy")
        result = self.prepare("review", b"changed synthetic digest")
        self.assertEqual(set(result["invalidated"]), {"review", "compare", "privacy"})
        counts = stages.counts(self.database)
        self.assertEqual(counts["stages"]["preserve"]["done"], 1)
        self.assertEqual(counts["stages"]["review"]["done"], 0)
        self.assertEqual(counts["end_to_end_complete"], 0)

    def test_changed_public_content_rejects_stale_review(self):
        self.advance("compare")
        self.prepare("privacy")
        stale = self.packet("privacy")
        self.prepare("privacy", b"changed public synthetic text")
        current = self.packet("privacy")
        current["reviews"] = stale["reviews"]
        with self.assertRaisesRegex(stages.StageError, "stale_review_content"):
            self.submit("privacy", current)

    def test_rule_change_invalidates_detect_compare_privacy(self):
        self.advance("privacy")
        result = self.runner.invalidate(self.subject,"detect",reason="synthetic rules v2")
        self.assertEqual(set(result["invalidated"]), {"detect", "compare", "privacy"})
        self.assertEqual(stages.counts(self.database)["stages"]["review"]["done"], 1)

    def test_preservation_never_invalidated(self):
        self.advance("preserve")
        with self.assertRaises(stages.StageError):
            self.runner.invalidate(self.subject,"preserve",reason="change")
        with self.assertRaises(stages.StageError):
            self.prepare("preserve", b"not original bytes")

    def test_domain_rejection_records_reason_without_receipt(self):
        self.prepare("preserve")
        with self.assertRaisesRegex(stages.StageError, "domain_validation_failed"):
            self.submit("preserve",self.packet("preserve",rationale="reject synthetic evidence"))
        self.assertEqual(self.rows("receipts"), [])
        self.assertEqual(self.rows("stage_attempts")[-1]["reason"], "domain_validation_failed")

    def test_atomic_interruption_rolls_back_receipt_state_history(self):
        self.prepare("preserve")
        self.runner.claim(self.subject,"preserve")
        before = self.rows("stage_state"), self.rows("stage_events")
        real = stages._Writer.insert
        def interrupt(writer, table, values):
            if table == "stage_transitions":
                raise RuntimeError("synthetic interruption")
            return real(writer, table, values)
        with mock.patch.object(stages._Writer, "insert", interrupt):
            with self.assertRaises(RuntimeError):
                self.submit("preserve")
        self.assertEqual((self.rows("stage_state"), self.rows("stage_events")), before)
        self.assertEqual(self.rows("receipts"), [])
        self.assertEqual(self.submit("preserve")["status"], "done")

    def test_uncontrolled_stage_update_fails_closed(self):
        self.prepare("preserve")
        with store.ledger(self.database) as c:
            with self.assertRaises(sqlite3.Error):
                c.execute("UPDATE stage_state SET status='blocked' WHERE original_sha256=?",
                          (self.subject,))
            c.rollback()

    def test_invalid_coverage_cannot_promote(self):
        self.prepare("preserve")
        packet = self.packet("preserve")
        packet["coverage"]["covered"] = True
        with self.assertRaisesRegex(stages.StageError, "coverage_counts"):
            self.submit("preserve", packet)

    def test_missing_occurrence_blocks_preservation(self):
        self.prepare("preserve")
        with store.ledger(self.database) as c:
            c.execute("DELETE FROM occurrences WHERE id='synthetic-local'")
            c.commit()
        with self.assertRaisesRegex(stages.StageError, "preservation_occurrence"):
            self.submit("preserve")

    def test_lease_required_for_in_progress_and_other_owner_rejected(self):
        self.prepare("preserve")
        result = self.runner.claim(self.subject,"preserve")
        self.assertFalse(result["reused"])
        self.assertEqual(stages.counts(self.database)["stages"]["preserve"]["in_progress"], 1)
        with self.assertRaisesRegex(stages.StageError, "lease_owned_elsewhere"):
            self.make_runner(self.run,owner="other").claim(self.subject,"preserve")

    def test_inapplicability_is_explicit_and_requires_review(self):
        self.advance("review")
        self.prepare("compare")
        self.submit("compare", self.packet("compare", verdict="inapplicable",
                                          reason="tier A synthetic record update has no legal claim"))
        result = stages.counts(self.database)
        self.assertEqual(result["stages"]["compare"]["inapplicable"], 1)
        self.assertEqual(result["end_to_end_complete"], 0)

    def test_explicit_receipt_supersession_required(self):
        self.advance("extract")
        packet = self.packet("extract", rationale="new synthetic check")
        with self.assertRaisesRegex(stages.StageError, "explicit_supersession"):
            self.submit("extract", packet)
        current = next(row for row in self.rows("stage_state")
                       if row["original_sha256"] == self.subject and row["stage"] == "extract")
        packet["supersedes"] = current["receipt_sha256"]
        self.assertEqual(self.submit("extract", packet)["status"], "done")

    def test_no_claim_cannot_promote(self):
        self.prepare("preserve")
        with self.assertRaisesRegex(stages.StageError,"current_claim_required"):
            self.runner.promote(self.subject,"preserve",json.dumps(self.packet("preserve")).encode())

    def test_foreign_worker_cannot_promote_live_claim(self):
        self.prepare("preserve")
        claim=self.runner.claim(self.subject,"preserve")
        other=self.make_runner(self.run,owner="other")
        before=self.rows("stage_state")
        with self.assertRaisesRegex(stages.StageError,"claim_execution_authority"):
            other.promote(self.subject,"preserve",json.dumps(self.packet("preserve")).encode(),claim_id=claim["claim_id"])
        self.assertEqual(before,self.rows("stage_state"))

    def test_same_owner_other_run_cannot_reuse_claim(self):
        self.prepare("preserve")
        claim=self.runner.claim(self.subject,"preserve")
        other=self.make_runner(self.new_run("other-run"))
        with self.assertRaisesRegex(stages.StageError,"lease_run_mismatch"):
            other.claim(self.subject,"preserve")
        with self.assertRaisesRegex(stages.StageError,"claim_execution_authority"):
            other.promote(self.subject,"preserve",json.dumps(self.packet("preserve")).encode(),claim_id=claim["claim_id"])

    def test_expired_claim_cannot_promote(self):
        self.prepare("preserve")
        claim=self.runner.claim(self.subject,"preserve",ttl_seconds=1)
        real=stages.datetime
        class Future(real):
            @classmethod
            def now(cls,tz=None):
                return real.now(tz)+stages.timedelta(seconds=10)
        with mock.patch.object(stages,"datetime",Future):
            with self.assertRaisesRegex(stages.StageError,"lease_expired"):
                self.runner.promote(self.subject,"preserve",json.dumps(self.packet("preserve")).encode(),claim_id=claim["claim_id"])

    def test_changed_content_revision_stales_claim(self):
        self.advance("preserve")
        self.prepare("extract")
        claim=self.runner.claim(self.subject,"extract")
        self.prepare("extract",b"changed synthetic extraction")
        with self.assertRaisesRegex(stages.StageError,"stale_lease_revision"):
            self.runner.promote(self.subject,"extract",json.dumps(self.packet("extract")).encode(),claim_id=claim["claim_id"])

    def test_finished_run_cannot_promote(self):
        self.prepare("preserve")
        claim=self.runner.claim(self.subject,"preserve")
        with store.ledger(self.database) as c:
            c.execute("UPDATE runs SET status='complete',ended_at=? WHERE run_id=?",(store.now(),self.run));c.commit()
        with self.assertRaisesRegex(stages.StageError,"live_runner_run_required"):
            self.runner.promote(self.subject,"preserve",json.dumps(self.packet("preserve")).encode(),claim_id=claim["claim_id"])

    def test_changed_run_config_cannot_promote(self):
        self.prepare("preserve")
        claim=self.runner.claim(self.subject,"preserve")
        with store.ledger(self.database) as c:
            c.execute("UPDATE runs SET config_sha256=? WHERE run_id=?",("f"*64,self.run));c.commit()
        with self.assertRaisesRegex(stages.StageError,"run_config_version_mismatch"):
            self.runner.promote(self.subject,"preserve",json.dumps(self.packet("preserve")).encode(),claim_id=claim["claim_id"])

    def test_profile_substitution_for_bound_run_rejected(self):
        with self.assertRaisesRegex(stages.StageError,"run_profile_substitution"):
            self.make_runner(self.run,version="substitute-v2")

    def test_submission_cannot_select_policy(self):
        self.prepare("preserve")
        for field in ("validator_id","profile_id","config_sha256","run_id","owner"):
            with self.subTest(field=field),self.assertRaisesRegex(stages.StageError,"submission_policy_override"):
                self.submit("preserve",self.packet("preserve",**{field:"invented"}))

    def test_unregistered_production_validator_rejected(self):
        for adapter in ("invented",lambda context:True):
            with self.assertRaisesRegex(stages.StageError,"unregistered_installed_validator"):
                stages.configure_installed_profile("fixture-unregistered",engine_version="synthetic-engine-v1",
                    config_sha256=self.config_hash,validators={"preserve":adapter})

    def test_missing_production_adapter_remains_blocked(self):
        run=self.new_run("empty-production-run")
        stages.configure_installed_profile("fixture-no-adapters",engine_version="synthetic-engine-v1",
                                          config_sha256=self.config_hash,validators={})
        runner=stages.installed_runner(self.database,run_id=run,owner="production-runner",profile_id="fixture-no-adapters")
        self.prepare("preserve")
        claim=runner.claim(self.subject,"preserve")
        with self.assertRaisesRegex(stages.StageError,"domain_adapter_unavailable"):
            runner.promote(self.subject,"preserve",json.dumps(self.packet("preserve")).encode(),claim_id=claim["claim_id"])
        result=stages.counts(self.database)
        self.assertEqual(result["stages"]["preserve"]["blocked"],1)
        self.assertEqual(result["verified_seven_stage_complete"],0)

    def test_always_true_test_validators_never_verify_production(self):
        run=self.new_run("always-true-test-run")
        self.runner=stages.testing_runner(self.database,run_id=run,owner="test-runner",
            engine_version="synthetic-engine-v1",config_sha256=self.config_hash,
            validators={stage:lambda context:True for stage in stages.STAGES},version="always-true-test-only")
        self.advance("privacy")
        result=stages.query_counts(self.database)
        self.assertEqual(result["candidate_seven_stage_complete"],1)
        self.assertEqual(result["synthetic_seven_stage_complete"],1)
        self.assertEqual(result["end_to_end_complete"],0)
        self.assertEqual(result["verified_seven_stage_complete"],0)


    def metadata(self):
        return {"schema": "preservation-evidence-v1", "original_sha256": self.subject,
                "byte_length": len(self.original), "storage_ref": "synthetic-content-addressed-store",
                "verification_receipt_sha256": hashlib.sha256(b"synthetic byte verification receipt").hexdigest(),
                "occurrence_ids": ["synthetic-local"]}

    def test_metadata_preservation_does_not_embed_original_bytes(self):
        evidence = self.metadata()
        registered = self.runner.set_preservation_evidence(self.subject, evidence, author_id="author")
        self.assertNotEqual(registered["content_sha256"], self.subject)
        result = self.submit("preserve")
        self.assertTrue(result["test_only"])
        self.assertEqual(stages.counts(self.database)["verified_seven_stage_complete"], 0)

    def test_large_original_preservation_metadata_stays_small(self):
        large_sha = hashlib.sha256(b"synthetic large-original declaration, no large file").hexdigest()
        with store.ledger(self.database) as c:
            original = dict(c.execute("SELECT * FROM originals WHERE sha256=?", (self.subject,)).fetchone())
            original.update(sha256=large_sha, bytes=9 * 1024 * 1024)
            keys = list(original)
            c.execute("INSERT INTO originals(" + ",".join(keys) + ") VALUES(" +
                      ",".join("?" for _ in keys) + ")", [original[k] for k in keys])
            for stage in stages.STAGES:
                c.execute("INSERT INTO stage_state VALUES(?,?,'pending',NULL,?,?,?,?)",
                          (large_sha, stage, "synthetic", store.now(), self.run, "synthetic metadata only"))
            c.execute("INSERT INTO occurrences VALUES(?,?,?,?,?,?,?,?)",
                      ("synthetic-large-local", large_sha, "local", "synthetic",
                       None, None, "synthetic", "{}"))
            c.commit()
        self.subject = large_sha
        evidence = self.metadata()
        evidence.update(byte_length=9 * 1024 * 1024, occurrence_ids=["synthetic-large-local"])
        self.runner.set_preservation_evidence(self.subject, evidence, author_id="author")
        result = self.submit("preserve")
        self.assertEqual(result["status"], "done")
        self.assertTrue(result["test_only"])
        self.assertTrue(all(len(row["payload"]) < 65536 for row in self.rows("stage_artifacts")))
        self.assertEqual(stages.counts(self.database)["verified_seven_stage_complete"], 0)

    def test_metadata_preservation_binding_mismatch_rejected(self):
        for field, value in (("original_sha256", "f" * 64), ("byte_length", True),
                             ("occurrence_ids", ["missing"]), ("verification_receipt_sha256", None)):
            evidence = self.metadata()
            evidence[field] = value
            with self.subTest(field=field), self.assertRaises(stages.StageError):
                self.runner.set_preservation_evidence(self.subject, evidence, author_id="author")


    def test_supersession_claim_reopens_downstream_and_counts_remain_valid(self):
        self.advance("privacy")
        old=next(x for x in self.rows("stage_state") if x["original_sha256"]==self.subject and x["stage"]=="catalog")["receipt_sha256"]
        packet=self.packet("catalog",supersedes=old,rationale="synthetic replacement check")
        claim=self.runner.claim(self.subject,"catalog",supersedes=old)
        counts=stages.counts(self.database)
        self.assertEqual(counts["stages"]["catalog"]["in_progress"],1)
        for stage in ("detect","review","compare","privacy"):
            self.assertEqual(counts["stages"][stage]["done"],0)
        current=next(x for x in self.rows("stage_state") if x["original_sha256"]==self.subject and x["stage"]=="catalog")
        self.assertEqual(current["receipt_sha256"],old)
        self.runner.promote(self.subject,"catalog",json.dumps(packet).encode(),claim_id=claim["claim_id"])
        self.assertEqual(stages.counts(self.database)["stages"]["catalog"]["done"],1)
        self.assertTrue(any(x["sha256"]==old for x in self.rows("receipts")))

    def test_supersession_claim_atomically_releases_stale_downstream_lease(self):
        self.advance("catalog")
        self.prepare("review")
        prior=self.runner.claim(self.subject,"review")
        old=next(x for x in self.rows("stage_state") if x["original_sha256"]==self.subject and x["stage"]=="catalog")["receipt_sha256"]
        replacement=self.packet("catalog",supersedes=old,rationale="synthetic fresh catalog acceptance")
        claim=self.runner.claim(self.subject,"catalog",supersedes=old)
        self.assertFalse(any(x["item_key"]=="stage:"+self.subject+":review" for x in self.rows("work_leases")))
        self.runner.promote(self.subject,"catalog",json.dumps(replacement).encode(),claim_id=claim["claim_id"])
        current=self.runner.claim(self.subject,"review")
        self.assertNotEqual(prior["claim_id"],current["claim_id"])
        with self.assertRaises(stages.StageError):
            self.runner.promote(self.subject,"review",json.dumps(self.packet("review")).encode(),claim_id=prior["claim_id"])

    def test_supersession_claim_interruption_rolls_back_dependents(self):
        self.advance("privacy")
        old=next(x for x in self.rows("stage_state") if x["original_sha256"]==self.subject and x["stage"]=="catalog")["receipt_sha256"]
        before=(self.rows("stage_state"),self.rows("stage_events"),self.rows("work_leases"))
        change=stages._change
        def interrupt(*args,**kwargs):
            if args[3:5]==("catalog","in_progress"):raise RuntimeError("synthetic interruption")
            return change(*args,**kwargs)
        with mock.patch.object(stages,"_change",interrupt):
            with self.assertRaises(RuntimeError):self.runner.claim(self.subject,"catalog",supersedes=old)
        self.assertEqual(before,(self.rows("stage_state"),self.rows("stage_events"),self.rows("work_leases")))
        self.assertEqual(stages.counts(self.database)["synthetic_seven_stage_complete"],1)


if __name__ == "__main__":
    unittest.main()
