"""Bounded private first-unfinished-stage planning; WP1 owns every mutation."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3

STAGES = ("preserve", "extract", "catalog", "detect", "review", "compare", "privacy")
PREREQUISITES = {
    "preserve": {}, "extract": {"preserve": ("done",)},
    "catalog": {"extract": ("done", "blocked", "inapplicable")},
    "detect": {"catalog": ("done",)}, "review": {"catalog": ("done",)},
    "compare": {"review": ("done",)},
    "privacy": {"review": ("done",), "compare": ("done", "inapplicable")},
}
IMPORTANT = frozenset(("policy", "log", "audit", "sharing", "contract", "denial"))
VERSION = "first-unfinished-v1"
MAX_ORIGINALS = 10000
LEASE_SECONDS = 45 * 60


class QueueError(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise QueueError(reason)


def timestamp(value):
    require(isinstance(value, str), "timestamp_required")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise QueueError("invalid_timestamp") from exc
    require(result.tzinfo is not None, "timezone_required")
    return result.astimezone(timezone.utc)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def score(facts):
    """Caller supplies versioned catalog/detector facts, never raw record instructions."""
    require(isinstance(facts, dict), "priority_facts_object")
    for key in ("new_production_open_request", "live_agency", "proposed_low_value"):
        require(type(facts.get(key, False)) is bool, "priority_boolean_required")
    hits = facts.get("severity3_hits", 0)
    require(type(hits) is int and 0 <= hits <= 1000000, "invalid_hit_count")
    kind = facts.get("document_type", "unknown")
    require(isinstance(kind, str) and len(kind) <= 80, "invalid_document_type")
    terms = [
        ("new_production_open_request", 40 if facts.get("new_production_open_request") else 0),
        ("important_document_type", 30 if kind in IMPORTANT else 0),
        ("severity3_hits_capped", min(40, 20 * hits)),
        ("agency_in_live_findings", 10 if facts.get("live_agency") else 0),
        ("proposed_low_value", -50 if facts.get("proposed_low_value") else 0),
    ]
    raw = sum(amount for _, amount in terms)
    return {"score": max(0, min(100, raw)), "unclamped_score": raw,
            "reasons": [{"code": code, "points": amount} for code, amount in terms],
            "config_version": VERSION, "facts_sha256": digest(facts),
            "low_value_closed": False}


def plan(originals, stage_rows, leases, *, now, facts=None, config_version=VERSION, limit=20):
    """Pure deterministic metadata planner; never equates settled stages with review."""
    when = timestamp(now)
    require(type(limit) is int and 1 <= limit <= 20, "top20_limit_required")
    require(isinstance(config_version, str) and 0 < len(config_version) <= 128,
            "config_version_required")
    require(len(originals) <= MAX_ORIGINALS and len(stage_rows) <= 7 * MAX_ORIGINALS
            and len(leases) <= 7 * MAX_ORIGINALS, "metadata_bound_exceeded")
    subjects = {}
    for row in originals:
        subject = row.get("sha256")
        require(isinstance(subject, str) and re.fullmatch("[0-9a-f]{64}", subject), "invalid_original_hash")
        require(subject not in subjects, "duplicate_original")
        subjects[subject] = row
    states = {subject: {} for subject in subjects}
    for row in stage_rows:
        subject, stage = row.get("original_sha256"), row.get("stage")
        require(subject in states and stage in STAGES, "unknown_stage_subject")
        require(stage not in states[subject], "duplicate_stage_slot")
        require(row.get("status") in ("pending", "in_progress", "done", "blocked", "inapplicable"),
                "unknown_stage_status")
        states[subject][stage] = row
    require(all(set(rows) == set(STAGES) for rows in states.values()), "incomplete_stage_slots")
    by_key = {}
    for lease in leases:
        key = lease.get("item_key")
        require(isinstance(key, str) and key not in by_key, "duplicate_or_invalid_lease")
        if not key.startswith("stage:"):
            continue  # Other installed modules own their work keys.
        require(type(lease.get("attempts")) is int and lease["attempts"] >= 1, "invalid_lease_attempts")
        timestamp(lease["expires_at"])
        if lease.get("next_eligible_at"):
            timestamp(lease["next_eligible_at"])
        by_key[key] = lease
    facts = facts or {}
    require(set(facts).issubset(subjects), "priority_subject_outside_snapshot")
    ready, held, settled, excluded = [], [], [], []
    for subject in sorted(subjects):
        if subjects[subject].get("scope") == "out_of_scope":
            excluded.append({"original_sha256":subject,"reason":"original_out_of_scope"})
            continue
        rows, reasons, chosen = states[subject], [], None
        ranking = score(facts.get(subject, {}))
        for stage in STAGES:
            row = rows[stage]
            if row["status"] in ("done", "inapplicable"):
                continue
            lease = by_key.get("stage:" + subject + ":" + stage)
            reason, next_at = None, None
            if lease and timestamp(lease["expires_at"]) > when:
                reason, next_at = "live_lease", lease["expires_at"]
            elif lease and lease.get("next_eligible_at") and timestamp(lease["next_eligible_at"]) > when:
                reason, next_at = "retry_not_due", lease["next_eligible_at"]
            elif row["status"] == "blocked" and not (lease and lease.get("next_eligible_at")):
                reason = "blocked_without_retry"
            if reason is None:
                for prerequisite, accepted in PREREQUISITES[stage].items():
                    if rows[prerequisite]["status"] not in accepted:
                        reason = "prerequisite_" + prerequisite
                        break
            if reason:
                reasons.append({"stage": stage, "code": reason, "reason": row.get("reason"),
                                "owner": row.get("owner"), "next_eligible_at": next_at})
                continue
            chosen = {"original_sha256": subject, "stage": stage, **ranking,
                      "config_version": config_version, "planner_version": VERSION,
                      "prior_blockers": reasons, "next_eligible_at": None,
                      "reclaim_expired_lease": bool(lease),
                      "expected_attempt": lease["attempts"] + 1 if lease else 1,
                      "timebox_seconds": LEASE_SECONDS,
                      "prior_receipts": {name: rows[name].get("receipt_sha256") for name in STAGES
                                         if rows[name].get("receipt_sha256")}}
            break
        if chosen:
            ready.append(chosen)
        elif reasons:
            held.append({"original_sha256": subject, **ranking, "score_reasons": ranking["reasons"], "reasons": reasons})
        else:
            settled.append(subject)
    ready.sort(key=lambda row: (-row["score"], row["original_sha256"], STAGES.index(row["stage"])))
    result = {"schema": "records-queue-plan-v1", "as_of": when.isoformat(),
              "config_version": config_version, "planner_version": VERSION,
              "snapshot_originals": len(subjects), "eligible": len(ready),
              "top": ready[:limit], "blocked": held, "settled_candidate_originals": settled,
              "excluded_from_claims": excluded,
              "publication_ready": False, "corpus_coverage_claim": False}
    result["plan_sha256"] = digest(result)
    return result


def from_ledger(database, *, now, facts=None, config_version=VERSION, limit=20):
    """Read one consistent canonical snapshot, capped without partial success."""
    uri = Path(database).absolute().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=5) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        originals = [dict(row) for row in connection.execute(
            "SELECT sha256,scope FROM originals ORDER BY sha256 LIMIT ?", (MAX_ORIGINALS + 1,))]
        states = [dict(row) for row in connection.execute(
            "SELECT * FROM stage_state LIMIT ?", (7 * MAX_ORIGINALS + 1,))]
        leases = [dict(row) for row in connection.execute(
            "SELECT * FROM work_leases LIMIT ?", (7 * MAX_ORIGINALS + 1,))]
        return plan(originals, states, leases, now=now, facts=facts,
                    config_version=config_version, limit=limit)


def claim_next(runner, *, now, facts=None, config_version=VERSION, limit=20):
    """Installed startup supplies WP1 StageRunner; no validator or authority overrides."""
    from .ledger.stages import StageError, StageRunner
    require(isinstance(runner, StageRunner), "trusted_stage_runner_required")
    snapshot = from_ledger(runner.database, now=now, facts=facts,
                           config_version=config_version, limit=limit)
    failed = []
    for item in snapshot["top"]:
        try:
            claim = runner.claim(item["original_sha256"], item["stage"], ttl_seconds=LEASE_SECONDS)
        except StageError as exc:
            reason = str(exc)
            # Configuration/corruption errors are not swallowed as ordinary contention.
            if reason not in ("lease_owned_elsewhere", "lease_run_mismatch", "stale_lease_revision",
                              "current_content_required", "explicit_supersession_required", "original_out_of_scope") and not reason.startswith("prerequisite_"):
                raise
            failed.append({"original_sha256": item["original_sha256"], "stage": item["stage"], "reason": reason})
            continue
        return {"claimed": {**item, "claim": claim, "owner": runner.owner, "run_id": runner.run_id},
                "plan_sha256": snapshot["plan_sha256"], "skipped": failed}
    return {"claimed": None, "plan_sha256": snapshot["plan_sha256"], "skipped": failed}


def packet(item, *, denominator, derived_text=(), page_images=(), receipt_template):
    """Private packet for an already claimed item; no completion or publication implied."""
    require(isinstance(item.get("claim"), dict) and item["claim"].get("claim_id"), "claim_required")
    require(isinstance(denominator, dict) and denominator.get("kind") in
            ("bytes", "pages", "rows", "sheets", "items") and
            type(denominator.get("total")) is int and denominator["total"] >= 0, "explicit_denominator_required")
    require(len(derived_text) + len(page_images) <= 1000, "packet_path_bound_exceeded")
    require(all(isinstance(path, str) and 0 < len(path) <= 4096 for path in (*derived_text, *page_images)),
            "invalid_private_path")
    require(isinstance(receipt_template, dict) and receipt_template.get("schema") == "ledger-stage-receipt-v1"
            and receipt_template.get("subject_sha256") == item["original_sha256"]
            and receipt_template.get("stage") == item["stage"], "bound_receipt_template_required")
    return {"schema": "records-work-packet-v1", **item, "denominator": denominator,
            "derived_text": list(derived_text), "page_images": list(page_images),
            "receipt_template": receipt_template, "timebox_seconds": LEASE_SECONDS,
            "private": True, "publication_ready": False}


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="Read-only private queue preview; never claims or publishes")
    parser.add_argument("database")
    parser.add_argument("--as-of", required=True)
    args = parser.parse_args(argv)
    print(json.dumps(from_ledger(args.database, now=args.as_of), sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
