"""Current extraction inventory and conservative runner processing coverage.

No new registry, semantic acceptance, model dispatch or independent review.
Progress uses existing stage-content checkpoints for verified whole-unit primary
requests rebuilt with the model's public builder, not observed at transport.
Interrupted requests record nothing; emission is never semantic acceptance.
"""
import hashlib
import json
from pathlib import Path

from campaign_tool.digest.build import redact_units
from . import semantic_coverage as sc


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def unit_view(pipeline, subject, limit):
    """Only current accepted extraction IDs; capped/unreadable units stay gaps."""
    content = pipeline._content(subject, "extract")
    if content.get("schema") != "canonical-extraction-content-v1" or content.get("original_sha256") != subject:
        raise ValueError("coverage_extraction_content_unbound")
    imports = pipeline._query(
        "SELECT i.* FROM extraction_adapter_current c JOIN extraction_adapter_imports i "
        "ON i.id=c.import_id WHERE c.original_sha256=?", (subject,))
    if len(imports) != 1:
        raise ValueError("coverage_current_extraction_missing")
    current = imports[0]
    raw = current["manifest_json"].encode()
    if (current["id"] != content["import_id"] or
            current["manifest_sha256"] != content["manifest_sha256"] or
            _hash(raw) != content["manifest_sha256"]):
        raise ValueError("coverage_extraction_snapshot_changed")
    manifest = json.loads(raw)
    identities = manifest["unit_ids"]
    if len(identities) != len(set(identities)) or len(identities) != content["unit_count"]:
        raise ValueError("coverage_extraction_inventory_invalid")
    rows = {r["id"]: r for r in pipeline._query(
        "SELECT * FROM units WHERE original_sha256=?", (subject,))}
    inventory, units = [], []
    for index, identity in enumerate(identities):
        row = rows.get(identity)
        item = {"unit_id": identity, "locator": None, "blocked_reason": "canonical_unit_missing"}
        if row:
            try:
                provenance = json.loads(row["provenance_json"])
                locator = json.loads(row["locator"])
                if provenance["extraction_import_id"] != current["id"] or not isinstance(locator, dict) or not locator:
                    raise ValueError("unit binding")
                item = {"unit_id": identity, "locator": locator, "unit_type": row["unit_type"],
                        "hidden": locator.get("state") in ("hidden", "veryHidden")}
                if index < limit:
                    raw = Path(row["derived_path"]).read_bytes()
                    payload = json.loads(raw)
                    text = payload.get("text") if isinstance(payload, dict) else None
                    if (not isinstance(text, str) or _hash(raw) != provenance["payload_sha256"] or
                            _hash(text.encode()) != row["text_sha256"]):
                        raise ValueError("unit bytes")
                    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
                    units.append({"unit_id": identity, "locator": locator, "locator_text": row["locator"],
                                  "text": text, "artifact_sha256": _hash(raw), "status": row["status"],
                                  "unit_type": row["unit_type"], "ocr": "ocr_receipt_id" in data})
            except (OSError, ValueError, KeyError, TypeError):
                item = {"unit_id": identity, "locator": None, "blocked_reason": "canonical_unit_unreadable_or_unbound"}
        inventory.append(item)
    return units, inventory, content


def processing_coverage(pipeline, subject, units, inventory, extraction, *, stage, checkpoint=None):
    """Plan redacted ranges; no automatic semantic acceptance or emission claims."""
    redacted, _ = redact_units(units, allowlist=pipeline.allowlist, denylist=pipeline.denylist)
    selected = {u["unit_id"]: u["text"] for u in redacted}
    # This identity includes source/config/code, not secrets or endpoint values.
    context = _hash(json.dumps({
        "stage": stage, "config": pipeline.config_sha256,
        "redaction": _hash(json.dumps([pipeline.allowlist, pipeline.denylist]).encode()),
        "model": None if pipeline.model is None else [pipeline.model.model_id, pipeline.model.privacy_tier],
        "adapter": _hash(Path(__file__).read_bytes()),
        "coverage": _hash(Path(sc.__file__).read_bytes()),
        "acceptance": "no-automatic-semantic-acceptance-v1",
    }, sort_keys=True).encode())
    manifest = sc.build_manifest(subject, inventory, selected,
        source_manifest_sha256=extraction["manifest_sha256"],
        extraction_version=extraction["parser"] + "/" + extraction["parser_version"],
        context_sha256=context, inventory_complete=True)
    resume = {"status": "fresh"}
    if checkpoint is not None:
        try:
            checkpoint = sc.validate_checkpoint(manifest, checkpoint)
            resume = {"status": "resumed"}
        except ValueError as exc:
            checkpoint = None
            resume = {"status": "rejected", "reason": str(exc)}
    if checkpoint is None:
        checkpoint = sc.initial_checkpoint(manifest)
    return _progress({"manifest": manifest, "resume": resume}, checkpoint)


def _progress(progress, checkpoint):
    summary = sc.coverage_summary(progress["manifest"], checkpoint)
    recorded = bool(checkpoint["attempts"])
    status = "emission_complete" if summary["emission_complete"] else "emission_partial"
    return {**progress, "checkpoint": checkpoint, "summary": summary,
            "status": status if recorded else "planned_only", "model_emission_recorded": recorded,
            "semantic_review_established": False, "review_required": True}


def record_emission(progress, chunk_id, *, payload_sha256, receipt_sha256, completed):
    """Return mechanical progress only; never persist, dispatch or accept ranges."""
    checkpoint = sc.record_emission(progress["manifest"], progress["checkpoint"], chunk_id,
        payload_sha256=payload_sha256, receipt_sha256=receipt_sha256, completed=completed)
    return _progress(progress, checkpoint)


def record_request(progress, request_text, unit_ids):
    """Account for verified whole views in a rebuilt primary request; never accept."""
    try:
        request = json.loads(request_text)
        lines = request["record_text"]
        if not isinstance(lines, list) or type(request.get("truncated")) is not bool:
            raise ValueError
        sent, previous = {}, -1
        for line in lines:
            left, text = line.split("\t", 1)
            reference = json.loads(left)
            index = reference["unit"]
            if (type(index) is not int or not previous < index < len(unit_ids) or
                    left != json.dumps({"unit": index})):
                raise ValueError
            sent[unit_ids[index]], previous = text, index
    except (ValueError, KeyError, TypeError, AttributeError):
        raise ValueError("request_shape") from None
    manifest, checkpoint = progress["manifest"], progress["checkpoint"]
    verified = {u["unit_id"] for u in manifest["units"] if u["unit_id"] in sent and
                len(sent[u["unit_id"]]) == u["view_chars"] and
                _hash(sent[u["unit_id"]].encode("utf-8")) == u["view_sha256"]}
    request_hash, refused = _hash(request_text.encode("utf-8")), []
    for chunk in manifest["chunks"]:
        visible = [r for r in chunk["ranges"] if r["unit_id"] in verified]
        if not visible:
            continue
        receipt = _hash(json.dumps({"schema": "records-review-emission-v1",
            "request_sha256": request_hash, "chunk_id": chunk["chunk_id"]},
            sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8"))
        attempts = [a for a in checkpoint["attempts"] if a["chunk_id"] == chunk["chunk_id"]]
        if (len(attempts) >= sc.MAX_EMISSIONS_PER_CHUNK and
                not any(a["receipt_sha256"] == receipt for a in attempts)):
            refused.append({"ordinal": chunk["ordinal"], "reason": "emission_attempts_exhausted"})
            continue
        checkpoint = sc.record_attempt(manifest, checkpoint, chunk["chunk_id"],
            visible_ranges=visible, accepted_ranges=[], receipt_sha256=receipt, completed=True)
    return {**_progress(progress, checkpoint), "request": {"status": "recorded",
        "request_sha256": request_hash, "sent_units": len(lines), "verified_units": len(verified),
        "unverified_units": len(lines) - len(verified), "truncated": request["truncated"],
        "refused_chunks": refused}}
