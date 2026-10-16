"""Serialized contextual cache binding; source-only integration of PR84.

Immutable snapshots use existing stage_artifacts; ledger_meta holds only their
current hash. No separate registry, date inference or applicability authority.
"""
import hashlib
import json
from pathlib import Path

from . import context_revision as cr
from .context_snapshot_io import read_artifact
from .ledger import stages, store


def _code(module):
    return store.file_hash(module.__file__)


def snapshot(pipeline, subject):
    from importlib import import_module
    build, model, model_input, redact = [import_module('campaign_tool.digest.' + name)
                                       for name in ('build', 'model', 'model_input', 'redact')]
    from . import challenge, stage_adapters, runner_coverage, extraction_validation
    units, inventory, extracted = pipeline._unit_view(subject)
    if extracted.get("installed_code") != extraction_validation.code_identity():
        raise ValueError("context_extraction_code_unverified")
    state = pipeline._states(subject)["extract"]
    if state["status"] != "done" or not state["receipt_sha256"]:
        raise ValueError("context_extraction_not_accepted")
    occurrences = pipeline._query("SELECT * FROM occurrences WHERE original_sha256=? ORDER BY id", (subject,))
    # Include each ancestor rather than guessing a request from shared bytes.
    by_id = {row["id"]: row for row in occurrences}
    for row in occurrences:
        current, seen = row, set()
        while current.get("parent_occurrence_id"):
            parent = current["parent_occurrence_id"]
            if parent in seen or len(seen) >= 100:
                raise ValueError("context_occurrence_ancestry_invalid")
            seen.add(parent)
            parents = pipeline._query("SELECT * FROM occurrences WHERE id=?", (parent,))
            if len(parents) != 1:
                raise ValueError("context_occurrence_parent_missing")
            current = parents[0]
            by_id[parent] = current
    originals = sorted({subject, *(row["original_sha256"] for row in by_id.values())})
    joins = []
    for identity in originals:
        joins.extend(pipeline._query("SELECT * FROM joins WHERE original_sha256=? ORDER BY id", (identity,)))
    request_ids = sorted({row["request_id"] for row in joins if row["request_id"]})
    agency_ids = sorted({row["agency_id"] for row in joins if row["agency_id"]})
    requests = [row for identity in request_ids for row in pipeline._query("SELECT * FROM requests WHERE id=?", (identity,))]
    agencies = [row for identity in agency_ids for row in pipeline._query("SELECT * FROM agencies WHERE id=?", (identity,))]
    declarations = [pipeline._original(identity) for identity in originals]
    evidence = {"originals": declarations, "occurrences": sorted(by_id.values(), key=lambda row: row["id"]),
                "joins": joins, "requests": requests, "agencies": agencies,
                "receipt_declarations": [{"occurrence_id": row["id"], "acquired_at": row["acquired_at"]}
                                         for row in sorted(by_id.values(), key=lambda row: row["id"])],
                "date_authority": "source_date_adapter_unavailable",
                "requested_event_date": pipeline.event_date.isoformat() if pipeline.event_date else None}
    # Receipt declarations retain their source strings above, never conduct or
    # retention dates. CLI event_date is a declaration, not source verification.
    context = cr.event_context(evidence)
    from . import run
    versions = {"extraction": cr.fingerprint(extracted["installed_code"]),
                "catalog": _code(run), "detector": pipeline.config["detector_code_sha256"],
                "digest": _code(build),
                "model": "disabled" if pipeline.model is None else "unresolved:" + cr.fingerprint(
                    {"id": pipeline.model.model_id, "tier": pipeline.model.privacy_tier,
                     "endpoint": pipeline.model.base_url, "timeout": pipeline.model.timeout}),
                "prompt": cr.fingerprint({"system": model.SYSTEM_PROMPT, "sources": _code(model_input)}),
                "challenge_model": "disabled" if pipeline.challenge is None else "unresolved:" + cr.fingerprint(
                    {"id": pipeline.challenge.model.model_id, "tier": pipeline.challenge.model.privacy_tier,
                     "endpoint": pipeline.challenge.model.base_url, "timeout": pipeline.challenge.model.timeout}),
                "challenge_prompt": _code(challenge),
                "redaction": cr.fingerprint({"code": _code(redact), "allow": list(pipeline.allowlist), "deny": list(pipeline.denylist)}),
                "context": _code(cr) + ":" + _code(__import__(__name__, fromlist=[""])),
                "applicability": _code(cr), "privacy": _code(stage_adapters)}
    extraction = {"receipt_sha256": state["receipt_sha256"], "content": extracted,
                  "inventory": inventory, "selected_unit_ids": [unit["unit_id"] for unit in units]}
    revision = cr.build_revision(subject_sha256=subject, extraction=extraction, context=context,
        rules=pipeline.package["rules"], versions=versions, rule_context=pipeline.package)
    return {"schema": "records-context-snapshot-v1", "subject_sha256": subject,
            "context": context, "extraction": extraction, "rules": pipeline.package,
            "revision": revision, "applicability_bases": [],
            "applicability": cr.compare_applicability([], pipeline.package["rules"], context)}


def revoke(pipeline, subject):
    with store.ledger(pipeline.root.ledger) as con:
        con.execute("UPDATE proposals SET owner_approval='none',approved_content_sha256=NULL WHERE privacy_receipt_sha256 IN (SELECT sha256 FROM receipts WHERE subject_sha256=?) OR independent_receipt_sha256 IN (SELECT sha256 FROM receipts WHERE subject_sha256=?)", (subject, subject))
        con.commit()


def reconcile(pipeline, subject, runner):
    from .run import require_root_lock
    require_root_lock(pipeline.root.path)
    try:
        current = snapshot(pipeline, subject)
    except Exception:
        revoke(pipeline, subject)
        raise
    key = "context-revision-v1:" + subject
    try:
        with store.ledger(pipeline.root.ledger, readonly=True) as con:
            pointer = con.execute("SELECT value FROM ledger_meta WHERE key=?", (key,)).fetchone()
            artifact = read_artifact(con, pointer[0]) if pointer else None
    except ValueError:
        revoke(pipeline, subject)
        raise
    previous = None
    if pointer:
        if artifact is None or hashlib.sha256(artifact).hexdigest() != pointer[0]:
            revoke(pipeline, subject)
            raise ValueError("context_snapshot_corrupt")
        try:
            prior = json.loads(artifact)
            if prior["subject_sha256"] != subject:
                raise ValueError("context_snapshot_subject_mismatch")
            previous = prior["revision"]
        except (ValueError, KeyError, TypeError):
            revoke(pipeline, subject)
            raise ValueError("context_snapshot_invalid") from None
    try:
        plan = cr.invalidation_plan(previous, current["revision"])
    except ValueError:
        revoke(pipeline, subject)
        raise
    if plan["revoke_approval"]:
        # Durable revocation comes before the first fallible invalidation.
        revoke(pipeline, subject)
        for name in plan["stages"]:
            # The adapter has just checked current extraction acceptance/code.
            # Reuse original extraction across contextual changes.
            if name != "extract":
                runner.invalidate(subject, name, reason=plan["reason"])
    raw = cr.canonical(current)
    with stages._transaction(pipeline.root.ledger) as (con, writer):
        runner._check(con)
        digest = stages._artifact(con, writer, raw, stages.MAX_CONTENT_BYTES)
        con.execute("INSERT INTO ledger_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, digest))
    return current, {**plan, "applied_stages": [name for name in plan["stages"] if name != "extract"],
                     "current_extraction_reused": True}


def ledger_binding(database, subject, name):
    with store.ledger(database, readonly=True) as con:
        pointer = con.execute("SELECT value FROM ledger_meta WHERE key=?", ("context-revision-v1:" + subject,)).fetchone()
        if pointer is None:
            return {}
        artifact = read_artifact(con, pointer[0])
    if artifact is None or hashlib.sha256(artifact).hexdigest() != pointer[0]:
        raise ValueError("context_snapshot_corrupt")
    current = json.loads(artifact)
    if current["subject_sha256"] != subject or current["revision"]["dependencies"]["subject_sha256"] != subject:
        raise ValueError("context_snapshot_subject_mismatch")
    cr.invalidation_plan(current["revision"], current["revision"])
    return {"context_stage_sha256": current["revision"]["stage_hashes"][name],
            "context_revision_sha256": current["revision"]["revision_sha256"]}
