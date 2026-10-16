"""Explicit imported-original enrollment followed by functional preservation.

No CLI activation, fabricated mail, parser acceptance or reviewer authority.
The installed application supplies an exact externally approved capture policy.
"""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from .canonical_mail import CanonicalMailBackend
from .wp1_bridge import installed_preservation_runner, preservation_profile
from ..intake import local_capture
from ..ledger import original_resolution


class CanonicalOriginalReconciler(CanonicalMailBackend):
    def __init__(self, mail_root, intake_output, database, *, policy):
        local_capture.require(type(policy) is local_capture.ApprovedCapturePolicy,
                              "installed_capture_policy_required")
        super().__init__(mail_root, intake_output, database)
        self.database = self.store.checked_path(database)
        self.output = Path(intake_output).absolute()
        from ..intake.mail_delta import private_path
        private_path(self.output, directory=True)
        private_path(self.output / "blobs", directory=True)
        self.policy = policy

    def _live_run(self, con):
        local_capture.require(self.run_identity is not None, "canonical_run_identity_missing")
        row = con.execute("SELECT status,ended_at FROM runs WHERE run_id=?", (self.run_identity["run_id"],)).fetchone()
        local_capture.require(row is not None and row["status"] == "running" and row["ended_at"] is None,
                              "canonical_enrollment_run_not_live")

    def recover_run(self, identity):
        """Finalize interrupted work and release that run's preservation claims.

        Use the same root lock as enrollment/acceptance, in addition to the
        caller's control-writer lock. A retry after finalization is safe if the
        process died before lease recovery completed.
        """
        from ..run import root_lock
        with root_lock(Path(self.database).parent):
            outcome = super().recover_run(identity)
            if outcome != "absent":
                self.stages.recover_abandoned_leases(self.database, owner="runner-preserver",
                    run_id=identity["run_id"], reason="reconciliation_run_recovered")
            return outcome

    def enroll_local_capture(self, source_path, *, original_sha256):
        """Append capture/resolution only; stage and receipt history stays untouched."""
        from ..run import root_lock
        subject = local_capture.sha(original_sha256)
        with root_lock(Path(self.database).parent):
            with self.store.ledger(self.database, readonly=True) as con:
                self._live_run(con)
                declared = con.execute("SELECT * FROM originals WHERE sha256=?", (subject,)).fetchone()
                local_capture.require(declared is not None, "canonical_original_missing")
                declared = dict(declared)
                self.policy.admission(subject, declared["bytes"])
                local_capture.require(declared["scope"] != "out_of_scope", "original_out_of_scope")
            receipt, receipt_hash, occurrence = local_capture.capture(source_path, self.output, subject, self.policy)
            with self.store.ledger(self.database) as con:
                con.execute("BEGIN IMMEDIATE")
                try:
                    self._live_run(con)
                    current = con.execute("SELECT * FROM originals WHERE sha256=?", (subject,)).fetchone()
                    local_capture.require(dict(current) == declared, "canonical_original_changed")
                    storage = str(self.output / "blobs" / subject)
                    unresolved = declared["role"] == declared["scope"] == "unresolved" and declared["storage_path"] is None
                    local_capture.require(unresolved or (declared["role"] == "original"
                        and declared["scope"] == "in_scope" and declared["storage_path"] == storage),
                        "canonical_original_binding_conflict")
                    prior = con.execute("SELECT * FROM occurrences WHERE id=?", (occurrence["id"],)).fetchone()
                    if prior:
                        local_capture.require(dict(prior) == occurrence, "canonical_capture_identity_conflict")
                    else:
                        con.execute("INSERT INTO occurrences VALUES(?,?,?,?,?,?,?,?)", tuple(occurrence.values()))
                    local_capture.verify_occurrence(occurrence, self.output, subject, expected_policy=self.policy.sha256)
                    resolution, reused = (None, bool(prior))
                    if unresolved:
                        resolution, reused = original_resolution.append_initial(con, subject=subject,
                            policy_sha256=self.policy.sha256, capture_receipt_sha256=receipt_hash,
                            capture_occurrence_id=occurrence["id"], storage_path=storage)
                    con.commit()
                except BaseException:
                    con.rollback()
                    raise
            return {"original_sha256": subject, "occurrence_id": occurrence["id"],
                    "resolution_sha256": resolution, "capture_receipt_sha256": receipt_hash,
                    "reused": reused, "stage_promotions": 0, "semantic_acceptance": False}

    def _validator(self, context):
        # This adapter cannot silently fall back to mutating mail replay.
        try:
            envelope = context.get("preservation_evidence")
            if context.get("content_kind") != "preservation_evidence" or type(envelope) is not dict:
                return False
            subject = context["receipt"]["subject_sha256"]
            with self.store.ledger(self.database, readonly=True) as con:
                original = self.store.effective_original(con, subject)
                if original is None or envelope.get("resolution_sha256") != original.get("resolution_sha256"):
                    return False
                from ..ledger.occurrence_classification import validate_declarations
                validate_declarations(con, subject)
                self.policy.admission(subject, original["bytes"])
                ids = envelope.get("occurrence_ids")
                if type(ids) is not list or not 1 <= len(ids) <= 1024 or len(set(ids)) != len(ids):
                    return False
                hashes = set()
                for oid in ids:
                    row = con.execute("SELECT * FROM occurrences WHERE id=? AND original_sha256=?", (oid, subject)).fetchone()
                    if row is None:
                        return False
                    hashes.add(local_capture.verify_occurrence(row, self.output, subject, expected_policy=self.policy.sha256))
            return (envelope.get("original_sha256") == subject and envelope.get("byte_length") == original["bytes"]
                    and envelope.get("storage_ref") == original["storage_path"] == str(self.output / "blobs" / subject)
                    and envelope.get("verification_receipt_sha256") in hashes)
        except (OSError, ValueError, KeyError, TypeError, RecursionError, sqlite3.Error):
            return False

    def _binding_context(self):
        from ..ledger import occurrence_classification
        from . import wp1_bridge
        modules = (local_capture, original_resolution, original_resolution.migration,
                   occurrence_classification, wp1_bridge)
        return ["local-capture-reconciliation-v1", self.policy.sha256,
                local_capture.digest(Path(__file__).read_bytes()),
                *[local_capture.digest(Path(module.__file__).read_bytes()) for module in modules]]

    def _accepted_replay(self, con, subject):
        """Require the protected, functional acceptance of the exact current head."""
        require = local_capture.require
        state = self.stages._states(con, subject)["preserve"]
        head = self.stages._head(con, subject, "preserve")
        transition = con.execute("SELECT * FROM stage_transitions WHERE receipt_sha256=?",
                                 (state["receipt_sha256"],)).fetchone()
        authority = con.execute("SELECT * FROM stage_validation_authority WHERE receipt_sha256=?",
                                (state["receipt_sha256"],)).fetchone()
        require(authority is not None and authority["test_only"] == 0,
                "functional_preservation_authority_required")
        require(head is not None and transition is not None and state["status"] == "done"
                and transition["subject_sha256"] == subject and transition["stage"] == "preserve"
                and transition["revision"] == head["revision"] and transition["status"] == "done"
                and transition["run_id"] == state["run_id"] == authority["run_id"]
                and transition["validator_id"] == authority["validator_id"]
                and state["owner"] == authority["owner"] == "runner-preserver",
                "preservation_replay_binding_failed")
        event = con.execute("SELECT * FROM stage_events WHERE sequence=?",
                            (transition["event_sequence"],)).fetchone()
        require(event is not None and all(event[key] == state[key] for key in state.keys()),
                "preservation_replay_event_failed")
        claim = con.execute("SELECT * FROM stage_claims WHERE claim_id=?", (authority["claim_id"],)).fetchone()
        binding = con.execute("SELECT * FROM stage_runner_bindings WHERE run_id=?", (authority["run_id"],)).fetchone()
        adapter_id, profile_id = preservation_profile(self.database, self.output, self.run_identity,
                                                       binding_context=self._binding_context())
        descriptor = {"profile_id": profile_id, "engine_version": self.run_identity["runtime"]["version"],
                      "config_sha256": self.run_identity["config_sha256"], "test_only": False,
                      "validators": {"preserve": adapter_id}}
        profile_hash = local_capture.digest(self.store.canonical(descriptor).encode())
        require(binding is not None and binding["test_only"] == 0 and binding["profile_id"] == profile_id
                and binding["profile_sha256"] == authority["profile_sha256"] == profile_hash
                and binding["engine_version"] == descriptor["engine_version"]
                and binding["config_sha256"] == descriptor["config_sha256"]
                and authority["validator_id"] == adapter_id, "preservation_replay_profile_failed")
        require(claim is not None and claim["subject_sha256"] == subject and claim["stage"] == "preserve"
                and claim["revision"] == head["revision"] and claim["content_sha256"] == head["content_sha256"]
                and claim["run_id"] == authority["run_id"] and claim["owner"] == authority["owner"]
                and claim["profile_sha256"] == profile_hash
                and local_capture.decode(claim["input_receipts"]) == local_capture.decode(transition["input_receipts"])
                    == {"original": subject}, "preservation_replay_claim_failed")
        content = con.execute("SELECT payload FROM stage_artifacts WHERE sha256=?", (head["content_sha256"],)).fetchone()
        packet = con.execute("SELECT payload FROM stage_artifacts WHERE sha256=?", (state["receipt_sha256"],)).fetchone()
        receipt = con.execute("SELECT * FROM receipts WHERE sha256=?", (state["receipt_sha256"],)).fetchone()
        require(content is not None and packet is not None and receipt is not None
                and local_capture.digest(bytes(content[0])) == head["content_sha256"]
                and local_capture.digest(bytes(packet[0])) == state["receipt_sha256"], "preservation_replay_artifact_failed")
        body = local_capture.decode(bytes(packet[0]))
        require(body.get("schema") == "ledger-stage-receipt-v1" and body.get("subject_sha256") == subject
                and body.get("stage") == "preserve" and body.get("verdict") == "pass"
                and body.get("content_sha256") == head["content_sha256"]
                and body.get("author_id") == head["author_id"] and body.get("tier") == head["tier"]
                and body.get("reviewer_id") == "runner-preserver" and body.get("role") == "preserver"
                and body.get("input_hashes") == {"original": subject}, "preservation_replay_receipt_failed")
        values = (state["receipt_sha256"], "preserve", subject, body["reviewer_id"], body["role"], body["verdict"],
                  self.store.canonical(body["coverage"]), self.store.canonical(body["locators"]), body["rationale"],
                  body["model_or_tool"], self.store.canonical(body["input_hashes"]), body["created_at_tz"],
                  body.get("supersedes"), "ledger-stage-artifact:" + state["receipt_sha256"])
        require(tuple(receipt) == values, "preservation_replay_receipt_row_failed")
        require(self._validator({"receipt": body, "content_kind": "preservation_evidence",
                                 "preservation_evidence": local_capture.decode(bytes(content[0]))}),
                "preservation_replay_domain_failed")
        return {"receipt_sha256": state["receipt_sha256"], "status": "done", "reused": True,
                "test_only": False, "semantic_acceptance": False}

    def accept(self, original_sha256):
        """Explicit canonical-ID callback using installed functional preserver only."""
        from ..run import root_lock
        subject = local_capture.sha(original_sha256)
        with root_lock(Path(self.database).parent):
            with self.store.ledger(self.database, readonly=True) as con:
                self._live_run(con)
                original = self.store.effective_original(con, subject)
                local_capture.require(original is not None, "canonical_original_missing")
                self.policy.admission(subject, original["bytes"])
                state = con.execute("SELECT * FROM stage_state WHERE original_sha256=? AND stage='preserve'", (subject,)).fetchone()
                rows = list(con.execute("SELECT * FROM occurrences WHERE original_sha256=? "
                                        "AND acquisition_method='verified_local_capture' ORDER BY id LIMIT 1025", (subject,)))
                local_capture.require(len(rows) <= 1024, "preservation_occurrence_bound")
                rows = [row for row in rows if local_capture.decode(row["evidence"]).get("policy_sha256") == self.policy.sha256]
                local_capture.require(rows, "verified_local_capture_required")
                if original.get("resolution_sha256"):
                    binding = con.execute("SELECT capture_occurrence_id FROM original_resolutions WHERE original_sha256=?", (subject,)).fetchone()
                    rows = [row for row in rows if row["id"] == binding[0]]
                local_capture.require(rows, "original_resolution_capture_unbound")
                receipt_hash = local_capture.verify_occurrence(rows[0], self.output, subject, expected_policy=self.policy.sha256)
                if state["status"] == "done":
                    return self._accepted_replay(con, subject)
            envelope = {"schema": "preservation-evidence-v1", "original_sha256": subject,
                "byte_length": original["bytes"], "storage_ref": original["storage_path"],
                "verification_receipt_sha256": receipt_hash, "occurrence_ids": [row["id"] for row in rows]}
            if original.get("resolution_sha256"):
                envelope["resolution_sha256"] = original["resolution_sha256"]
            local_capture.require(self._validator({"receipt": {"subject_sha256": subject, "stage": "preserve"},
                "content_kind": "preservation_evidence", "preservation_evidence": envelope}),
                "local_capture_domain_validation_failed")
            if self.stage_runner is None:
                self.stage_runner = installed_preservation_runner(self.stages, self.database,
                    self.run_identity, self._validator, binding_context=self._binding_context())
            registered = self.stage_runner.set_preservation_evidence(subject, envelope, author_id="runner-preserver", tier="A")
            claim = self.stage_runner.claim(subject, "preserve", ttl_seconds=300)
            packet = {"schema": "ledger-stage-receipt-v1", "subject_sha256": subject, "stage": "preserve",
                "content_sha256": registered["content_sha256"], "author_id": "runner-preserver", "tier": "A",
                "reviewer_id": "runner-preserver", "role": "preserver", "verdict": "pass",
                "coverage": {"denominator": {"kind": "bytes", "total": original["bytes"]}, "covered": original["bytes"], "scope": "selected"},
                "locators": ["cas:" + subject], "rationale": "Verified current local byte capture; no mail provenance or content review.",
                "model_or_tool": "local-capture-preserver-v1", "created_at_tz": datetime.now(timezone.utc).isoformat(),
                "input_hashes": {"original": subject}}
            result = self.stage_runner.promote(subject, "preserve", json.dumps(packet, sort_keys=True).encode(), claim_id=claim["claim_id"])
            return {**result, "semantic_acceptance": False}
