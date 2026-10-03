"""Canonical local document enrollment using the installed preservation authority."""
from datetime import datetime, timezone
import json
from .canonical_mail import CanonicalMailBackend
from .core import hid, js
from .contracts import IntegrationGap
from .wp1_bridge import installed_preservation_runner
from ..intake import document_drop, folder


class CanonicalDocumentsBackend(CanonicalMailBackend):
    def preserve_document(self, path, manifest, entry):
        if self.run_identity is None:
            raise IntegrationGap("canonical_run_identity_missing")
        receipt, receipt_sha, receipt_path = document_drop.capture(self.output, path, manifest, entry)
        subject, size = receipt["sha256"], receipt["bytes"]
        source = js(receipt["source"])
        oid = folder.hid(folder.js(["document-drop-v1", receipt["source"], subject]))
        evidence = js({"receipt_sha256": receipt_sha, "cas_sha256": subject, "bytes": size,
                       "verification_receipt_path": receipt_path})
        stamp = datetime.now(timezone.utc).isoformat()
        rid = self.run_identity["run_id"]
        storage = str(self.output / "blobs" / subject)
        with self.store.ledger(self.database) as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                run = con.execute("SELECT status,ended_at FROM runs WHERE run_id=?", (rid,)).fetchone()
                if not run or run["status"] != "running" or run["ended_at"] is not None:
                    raise ValueError("canonical_enrollment_run_not_live")
                original = con.execute("SELECT bytes,storage_path,scope FROM originals WHERE sha256=?", (subject,)).fetchone()
                if original and (original["bytes"], original["storage_path"], original["scope"]) != (size, storage, "in_scope"):
                    raise ValueError("canonical_original_binding_conflict")
                if not original:
                    mime = {"pdf": "application/pdf", "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}[receipt["form"]]
                    con.execute("INSERT INTO originals VALUES(?,?,?,?,?,?,?,?,?,?)",
                                (subject, size, mime, stamp, "original", "in_scope", storage, "captured", None,
                                 js({"method": "verified_local_document_drop", "receipt_sha256": receipt_sha})))
                    for stage in self.store.STAGES:
                        con.execute("INSERT INTO stage_state VALUES(?,?,'pending',NULL,?,?,?,?)",
                                    (subject, stage, "runner", stamp, rid, "domain validation pending"))
                prior = con.execute("SELECT original_sha256,kind,source_ref,parent_occurrence_id,acquisition_method,evidence FROM occurrences WHERE id=?", (oid,)).fetchone()
                expected = (subject, "local", source, None, "verified_local_document_drop", evidence)
                if prior and tuple(prior) != expected:
                    raise ValueError("canonical_document_identity_conflict")
                if not prior:
                    con.execute("INSERT INTO occurrences VALUES(?,?,?,?,?,?,?,?)",
                                (oid, subject, "local", source, None, stamp, "verified_local_document_drop", evidence))
                con.commit()
            except BaseException:
                con.rollback()
                raise
        if self.stage_runner is None:
            self.stage_runner = installed_preservation_runner(self.stages, self.database, self.run_identity, self._validator)
        proof = folder.verify_blob(subject, self.output)
        with self.store.ledger(self.database, readonly=True) as con:
            original = con.execute("SELECT first_seen_at FROM originals WHERE sha256=?", (subject,)).fetchone()
            state = con.execute("SELECT status FROM stage_state WHERE original_sha256=? AND stage='preserve'", (subject,)).fetchone()
            ids = [row[0] for row in con.execute("SELECT id FROM occurrences WHERE original_sha256=? ORDER BY id LIMIT 1025", (subject,))]
        if len(ids) > 1024:
            raise IntegrationGap("preservation_occurrence_bound")
        envelope = {"schema": "preservation-evidence-v1", "original_sha256": subject,
                    "byte_length": proof["bytes"], "storage_ref": storage,
                    "verification_receipt_sha256": receipt_sha, "occurrence_ids": ids}
        context = {"receipt": {"subject_sha256": subject, "stage": "preserve"},
                   "content_kind": "preservation_evidence", "preservation_evidence": envelope}
        if state["status"] == "done":
            if not self._validator(context):
                raise ValueError("preservation_replay_domain_failed")
            return bool(prior)
        registered = self.stage_runner.set_preservation_evidence(subject, envelope, author_id="runner-preserver", tier="A")
        claim = self.stage_runner.claim(subject, "preserve", ttl_seconds=300)
        packet = {"schema": "ledger-stage-receipt-v1", "subject_sha256": subject, "stage": "preserve",
                  "content_sha256": registered["content_sha256"], "author_id": "runner-preserver", "tier": "A",
                  "reviewer_id": "runner-preserver", "role": "preserver", "verdict": "pass",
                  "coverage": {"denominator": {"kind": "bytes", "total": size}, "covered": size, "scope": "selected"},
                  "locators": ["cas:" + subject], "rationale": "Exact CAS bytes and local acquisition receipt verified",
                  "model_or_tool": "runner-cas-preserver-v3", "created_at_tz": original["first_seen_at"],
                  "input_hashes": {"original": subject}}
        self.stage_runner.promote(subject, "preserve", json.dumps(packet, sort_keys=True).encode(), claim_id=claim["claim_id"])
        return bool(prior)
