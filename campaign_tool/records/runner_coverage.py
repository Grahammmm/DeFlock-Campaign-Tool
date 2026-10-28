"""Current extraction inventory and conservative runner processing coverage.

No new registry, semantic acceptance, model dispatch or independent review.
Progress uses the existing semantic-coverage API and stage-content artifacts.
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


def processing_coverage(pipeline, subject, units, inventory, extraction, *, stage):
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
    checkpoint = sc.initial_checkpoint(manifest)
    summary = sc.coverage_summary(manifest, checkpoint)
    return {"manifest": manifest, "checkpoint": checkpoint, "summary": summary,
            "status": "planned_only", "model_emission_recorded": False,
            "semantic_review_established": False}
