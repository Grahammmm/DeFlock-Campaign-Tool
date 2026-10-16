"""Explicit test-only controller fixtures for publisher unit tests.

No automatic challenge is converted into independent evidence. These synthetic
receipts use the declared testing_runner API, retain test_only authority, and
cannot establish production stage completion. Production validators are unchanged.
"""
import uuid
from campaign_tool.records.ledger import store, stages
from campaign_tool.records.run import encoded, root_lock, sha


def seed(pipeline, original, public_text, *, revision=1, until="privacy"):
    if until not in store.STAGES:
        raise ValueError("invalid synthetic fixture stage")
    root, subject = pipeline.root, sha(original)
    stamp, run_id = store.now(), "publisher-fixture-" + uuid.uuid4().hex
    config = sha(b"synthetic publisher fixture configuration")
    with root_lock(root.path):
        with store.ledger(root.ledger) as con:
            con.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (run_id, "stage-runner", stamp, None, "synthetic-fixture", None,
                         config, None, "running", "{}"))
            if con.execute("SELECT 1 FROM originals WHERE sha256=?", (subject,)).fetchone() is None:
                con.execute("INSERT INTO originals VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (subject, len(original), "text/plain", stamp, "fixture", "synthetic",
                             "synthetic", "preaccepted_fixture", None, "{}"))
                con.executemany("INSERT INTO stage_state VALUES(?,?,?,?,?,?,?,?)",
                                [(subject, stage, "pending", None, "fixture", stamp, run_id, None)
                                 for stage in store.STAGES])
                con.execute("INSERT INTO occurrences VALUES(?,?,?,?,?,?,?,?)",
                            ("publisher-" + subject, subject, "local", "synthetic-publisher-fixture",
                             None, None, "synthetic", "{}"))
            con.commit()
        runner = stages.testing_runner(root.ledger, run_id=run_id, owner="synthetic-fixture",
                                       engine_version="synthetic-fixture", config_sha256=config,
                                       validators={stage: lambda context: True for stage in store.STAGES})
        for stage in store.STAGES:
            if revision > 1 and stage in ("preserve", "extract", "catalog", "detect"):
                continue
            payload = original if stage == "preserve" else encoded(["synthetic fixture", stage, revision])
            head = runner.set_content(subject, stage, payload, author_id="synthetic-author", tier="A")
            claim = runner.claim(subject, stage)
            with store.ledger(root.ledger) as con:
                states = {row["stage"]: dict(row) for row in con.execute(
                    "SELECT * FROM stage_state WHERE original_sha256=?", (subject,))}
            inputs = {"original": subject, **{name: states[name]["receipt_sha256"]
                                             for name in stages.PREREQUISITES[stage]}}
            roles = {"review": ("factual",), "compare": ("legal",),
                     "privacy": ("factual", "privacy")}.get(stage, ())
            reviews = [{"reviewer_id": "synthetic-preaccepted-issuer", "role": role, "verdict": "pass",
                        "content_sha256": head["content_sha256"], "reviewed_primary_sha256": subject,
                        "checked_locators": ["synthetic:fixture"], "reviewed_at": stamp,
                        "rationale": "Explicit synthetic fixture precondition", "blind_first_pass": True}
                       for role in roles]
            packet = {"schema": "ledger-stage-receipt-v1", "subject_sha256": subject, "stage": stage,
                      "content_sha256": head["content_sha256"], "tier": "A", "author_id": "synthetic-author",
                      "reviewer_id": "synthetic-preaccepted-issuer", "role": stages.ROLES[stage], "verdict": "pass",
                      "coverage": {"denominator": {"kind": "items", "total": 1}, "covered": 1, "scope": "full_text"},
                      "locators": ["synthetic:fixture"], "rationale": "Publisher unit fixture only",
                      "model_or_tool": "synthetic-fixture", "created_at_tz": stamp,
                      "input_hashes": inputs, "reviews": reviews}
            promoted = runner.promote(subject, stage, encoded(packet), claim_id=claim["claim_id"])
            if promoted["test_only"] is not True:
                raise AssertionError("publisher fixture must not acquire production authority")
            if stage == until:
                break
        if until != "privacy":
            return subject
        states = pipeline._states(subject)
        manifest = {"schema": "records-proposal-manifest-v1", "original_sha256": subject, "tier": "A",
                    "review_receipt_sha256": states["review"]["receipt_sha256"],
                    "compare_receipt_sha256": states["compare"]["receipt_sha256"]}
        content = {"public_markdown": public_text, "public_content_sha256": sha(public_text.encode()),
                   "manifest": manifest, "manifest_sha256": sha(encoded(manifest))}
        proposal = pipeline._record_proposal(subject, content, states)
        with store.ledger(root.ledger) as con:
            con.execute("UPDATE runs SET status='completed',ended_at=? WHERE run_id=?", (store.now(), run_id))
            con.commit()
        return proposal


def policy(pipeline, *, revision=1):
    text = "# Synthetic policy\n\nDetections are retained for 90 days, then purged.\n"
    if revision > 1:
        text = "# Synthetic corrected policy\n\nDetections are retained for 45 days.\n"
    return seed(pipeline, b"synthetic-policy-original", text, revision=revision)
