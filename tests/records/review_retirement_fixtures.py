"""Synthetic non-test controller receipts, not real authenticated source review.

No validator substitution: real native-text intake and installed validators bind
all prerequisite receipts. Fictional reviewer declarations exercise preservation
of existing non-test authority metadata; they do not create an authentication seam.
"""
import json
from campaign_tool.records.context_runtime import ledger_binding
from campaign_tool.records import stage_adapters
from campaign_tool.records.ledger import stages, store
from campaign_tool.records.run import Pipeline, encoded, sha, root_lock
from tests.records.test_run_slice import synthetic_email


def independent_proposal(root, inbox, *, reviewer="synthetic-independent-reviewer",
                         original=b"FICTIONAL TEST POLICY: Retention is 90 days."):
    inbox.mkdir(mode=0o700)
    (inbox / "fixture.eml").write_bytes(synthetic_email(attachment=original,
        filename="synthetic-policy.txt", message_id="<independent@fixture.example.invalid>"))
    Pipeline(root).run(inbox)
    pipeline, subject = Pipeline(root), sha(original)
    assert pipeline._states(subject)["review"]["status"] == "blocked"
    with root_lock(root):
        runner = pipeline._open_stage_run()["runner"]
        assert runner.test_only is False
        with store.ledger(pipeline.root.ledger, readonly=True) as con:
            row = con.execute("SELECT a.payload FROM stage_content c JOIN stage_artifacts a "
                              "ON a.sha256=c.content_sha256 WHERE c.subject_sha256=? AND c.stage='review' "
                              "ORDER BY c.revision DESC LIMIT 1", (subject,)).fetchone()
            assert row is not None
            review_content = bytes(row[0])
        runner.invalidate(subject, "review", reason="synthetic independent receipt submission")
        privacy = None
        for name in ("review", "compare", "privacy"):
            states = pipeline._states(subject)
            if name == "review":
                payload = review_content
            elif name == "compare":
                payload = encoded({"schema": stage_adapters.SCHEMAS[name], "subject_sha256": subject,
                                   "event_date": "2025-01-01", "comparisons": []})
            else:
                public = "# Fictional policy\n\nThe synthetic policy describes 90-day retention.\n"
                manifest = {"schema": "records-proposal-manifest-v1", "original_sha256": subject,
                            "tier": "A", "review_receipt_sha256": states["review"]["receipt_sha256"],
                            "compare_receipt_sha256": states["compare"]["receipt_sha256"]}
                privacy = {"schema": stage_adapters.SCHEMAS[name], "subject_sha256": subject,
                           "public_markdown": public, "public_content_sha256": sha(public.encode()),
                           "manifest": manifest, "manifest_sha256": sha(encoded(manifest))}
                payload = encoded(privacy)
            payload = encoded({**json.loads(payload), **ledger_binding(pipeline.root.ledger, subject, name)})
            head = runner.set_content(subject, name, payload, author_id="synthetic-author", tier="A")
            claim = runner.claim(subject, name)
            stamp = store.now()
            roles = {"review": ("factual",), "compare": ("legal",), "privacy": ("factual", "privacy")}[name]
            reviews = [{"reviewer_id": reviewer, "role": role, "verdict": "pass",
                        "content_sha256": head["content_sha256"], "reviewed_primary_sha256": subject,
                        "checked_locators": ["synthetic:item:1"], "reviewed_at": stamp,
                        "rationale": "Fictional independent declaration for controller contract only",
                        "blind_first_pass": True} for role in roles]
            receipt = {"schema": "ledger-stage-receipt-v1", "subject_sha256": subject, "stage": name,
                       "content_sha256": head["content_sha256"], "tier": "A", "author_id": "synthetic-author",
                       "reviewer_id": reviewer, "role": stages.ROLES[name],
                       "verdict": "pass", "coverage": {"denominator": {"kind": "items", "total": 1},
                       "covered": 1, "scope": "full_text"}, "locators": ["synthetic:item:1"],
                       "rationale": "Synthetic controller acceptance, not original-record review",
                       "model_or_tool": "synthetic-fixture", "created_at_tz": stamp,
                       "input_hashes": {"original": subject, **{p: states[p]["receipt_sha256"]
                           for p in stages.PREREQUISITES[name]}}, "reviews": reviews}
            result = runner.promote(subject, name, encoded(receipt), claim_id=claim["claim_id"])
            assert result["test_only"] is False
        proposal = pipeline._record_proposal(subject, privacy, pipeline._states(subject))
        pipeline._close_stage_run({"synthetic": True})
    return subject, proposal
