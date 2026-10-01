"""Strict installed WP4 text-line support for WP5; no unit/status conversion.

Only the fixed WP4 validator registered at trusted startup supplies filesystem
roots. Cards select a canonical unit ID, never code, callbacks or root paths.
"""
from pathlib import Path

from .catalog_stage import CatalogStageError, require, _sha, _decode, _locator, MAX_UNIT_BYTES


DRIFT_REASONS = frozenset({
    "wp4_current_import_mismatch", "wp4_import_membership_mismatch", "wp4_manifest_mismatch",
    "wp4_unit_set_mismatch", "wp4_unit_identity_mismatch", "wp4_line_binding_mismatch",
    "wp4_line_hash_mismatch", "wp4_provenance_mismatch", "wp4_parser_mismatch",
    "wp4_evidence_drift", "wp4_accepted_content_mismatch",
})


def _artifact(con, digest, limit):
    require(type(digest) is str and len(digest) == 64, "wp4_stage_artifact_identity")
    row = con.execute("SELECT payload FROM stage_artifacts WHERE sha256=?", (digest,)).fetchone()
    require(row is not None and type(row[0]) is bytes and len(row[0]) <= limit
            and _sha(row[0]) == digest, "wp4_stage_artifact_identity")
    return bytes(row[0])


def verified_text_line(adapter, con, row, proof):
    """Validate accepted-current extraction, then decode one bounded exact unit."""
    try:
        from . import extraction_validation as validation
        from . import extraction_ledger as enrollment
    except ImportError as error:
        raise CatalogStageError("wp4_installed_dependency_unavailable") from error
    binding = adapter.stages._INSTALLED_VALIDATORS.get(validation.ADAPTER_ID)
    require(type(binding) is validation._InstalledExtractionValidator
            and binding.database == adapter.database
            and binding.evidence_root.is_relative_to(adapter.root), "wp4_installed_binding_required")
    subject = row["original_sha256"]
    state = con.execute("SELECT * FROM stage_state WHERE original_sha256=? AND stage='extract'", (subject,)).fetchone()
    require(state is not None and state["status"] == "done" and state["receipt_sha256"],
            "wp4_accepted_extraction_required")
    head = con.execute("SELECT * FROM stage_content WHERE subject_sha256=? AND stage='extract' ORDER BY revision DESC LIMIT 1", (subject,)).fetchone()
    transition = con.execute("SELECT * FROM stage_transitions WHERE receipt_sha256=?", (state["receipt_sha256"],)).fetchone()
    authority = con.execute("SELECT * FROM stage_validation_authority WHERE receipt_sha256=?", (state["receipt_sha256"],)).fetchone()
    require(head is not None and transition is not None and authority is not None
            and authority["test_only"] == 0 and authority["validator_id"] == validation.ADAPTER_ID,
            "wp4_production_authority_required")
    require(transition["subject_sha256"] == subject and transition["stage"] == "extract"
            and transition["status"] == "done" and transition["revision"] == head["revision"]
            and transition["run_id"] == authority["run_id"]
            and transition["validator_id"] == validation.ADAPTER_ID, "wp4_current_acceptance_binding")
    claim = con.execute("SELECT * FROM stage_claims WHERE claim_id=?", (authority["claim_id"],)).fetchone()
    run = con.execute("SELECT * FROM stage_runner_bindings WHERE run_id=?", (authority["run_id"],)).fetchone()
    require(claim is not None and run is not None and run["test_only"] == 0
            and run["profile_sha256"] == claim["profile_sha256"] == authority["profile_sha256"]
            and claim["run_id"] == authority["run_id"] and claim["owner"] == authority["owner"]
            and claim["subject_sha256"] == subject and claim["stage"] == "extract"
            and claim["revision"] == head["revision"] and claim["content_sha256"] == head["content_sha256"],
            "wp4_production_authority_required")
    preserved = con.execute("SELECT * FROM stage_state WHERE original_sha256=? AND stage='preserve'", (subject,)).fetchone()
    require(preserved is not None and preserved["status"] == "done", "wp4_preservation_required")
    preservation_authority = con.execute("SELECT test_only FROM stage_validation_authority WHERE receipt_sha256=?", (preserved["receipt_sha256"],)).fetchone()
    require(preservation_authority is not None and preservation_authority[0] == 0, "wp4_production_authority_required")
    inputs = {"original": subject, "preserve": preserved["receipt_sha256"]}
    require(_decode(transition["input_receipts"]) == _decode(claim["input_receipts"]) == inputs,
            "wp4_current_acceptance_binding")
    packet = _decode(_artifact(con, state["receipt_sha256"], adapter.stages.MAX_RECEIPT_BYTES))
    content_raw = _artifact(con, head["content_sha256"], validation.MAX_CONTENT)
    content = _decode(content_raw)
    require(packet.get("schema") == "ledger-stage-receipt-v1" and packet.get("subject_sha256") == subject
            and packet.get("stage") == "extract" and packet.get("content_sha256") == head["content_sha256"]
            and packet.get("input_hashes") == inputs and packet.get("author_id") == head["author_id"]
            and packet.get("tier") == head["tier"] and packet.get("reviewer_id") == authority["owner"],
            "wp4_accepted_content_mismatch")
    require(content.get("schema") == validation.ENVELOPE_SCHEMA and content.get("original_sha256") == subject,
            "wp4_accepted_content_mismatch")
    import_id = content.get("import_id")
    pointer = con.execute("SELECT import_id FROM extraction_adapter_current WHERE original_sha256=?", (subject,)).fetchone()
    require(pointer is not None and pointer[0] == import_id, "wp4_current_import_mismatch")
    imported = con.execute("SELECT * FROM extraction_adapter_imports WHERE id=?", (import_id,)).fetchone()
    require(imported is not None and imported["original_sha256"] == subject
            and imported["manifest_sha256"] == content.get("manifest_sha256"), "wp4_import_membership_mismatch")
    manifest = _decode(imported["manifest_json"])
    require(_sha(imported["manifest_json"].encode()) == imported["manifest_sha256"], "wp4_manifest_mismatch")
    members = manifest.get("unit_ids")
    require(type(members) is list and 0 < len(members) <= enrollment.MAX_UNITS
            and all(type(member) is str for member in members) and len(set(members)) == len(members)
            and row["id"] in members, "wp4_import_membership_mismatch")
    # Historical imports may coexist, but the selected import cannot acquire
    # extra canonical units that were absent from its immutable manifest.
    current_rows = con.execute("SELECT id FROM units WHERE original_sha256=? AND json_valid(provenance_json) AND json_extract(provenance_json,'$.extraction_import_id')=? LIMIT ?",
                               (subject, import_id, enrollment.MAX_UNITS + 1)).fetchall()
    require({item[0] for item in current_rows} == set(members) and len(current_rows) == len(members),
            "wp4_unit_set_mismatch")
    provenance = _decode(row["provenance_json"])
    ordinal = row["legacy_ordinal"]
    require(type(ordinal) is int and 1 <= ordinal <= len(members) and members[ordinal - 1] == row["id"]
            and provenance.get("extraction_import_id") == import_id
            and provenance.get("ordinal") == ordinal, "wp4_import_membership_mismatch")
    require(row["status"] == "candidate_extracted" and row["unit_type"] == "text_line"
            and row["parser"] == content.get("parser") == manifest.get("parser")
            and row["parser_version"] == content.get("parser_version") == manifest.get("parser_version")
            and row["parser"] in {"legacy-intake:" + form for form in validation.TEXT_FORMS}, "wp4_parser_mismatch")
    _locator(row["locator"])
    require(proof["source_sha256"] == subject and proof["locator"] == row["locator"], "wp4_line_binding_mismatch")
    digest = provenance.get("payload_sha256")
    require(type(digest) is str and len(digest) == 64 and proof["artifact_sha256"] == digest
            and row["derived_path"] == str(binding.evidence_root / digest), "wp4_line_hash_mismatch")
    raw = adapter._read(binding.evidence_root / digest, MAX_UNIT_BYTES)
    require(_sha(raw) == digest and len(raw.splitlines()) == 1 and raw.endswith(b"\n"), "wp4_line_hash_mismatch")
    require(_sha(enrollment.canonical([import_id, ordinal, digest]).encode()) == row["id"], "wp4_unit_identity_mismatch")
    line = _decode(raw)
    require(type(line) is dict and set(line) == {"kind", "locator", "text", "data"}
            and line["kind"] == "text_line" and line["locator"] == {"line": ordinal}
            and row["locator"] == enrollment.canonical(line["locator"])
            and type(line["text"]) is str and line["data"] == {}, "wp4_line_binding_mismatch")
    require(_sha(line["text"].encode()) == row["text_sha256"]
            and proof["quote"] in line["text"], "wp4_line_hash_mismatch")
    original = con.execute("SELECT * FROM originals WHERE sha256=?", (subject,)).fetchone()
    require(original is not None and original["scope"] not in {"excluded", "out_of_scope"}
            and original["role"] != "excluded_unrelated_personal", "support_original_missing")
    # Fixed code rechecks original bytes, complete source denominator, every
    # exact JSONL line and parser identity. Submitted paths are never followed.
    try:
        require(binding({"stage": "extract", "subject_sha256": subject, "original": dict(original),
                         "content": content_raw, "receipt": packet}) is True, "wp4_evidence_drift")
    except enrollment.ExtractionBindingError as error:
        # Conservative finite classification: runtime/path availability failures
        # are blocked, not automatically treated as proof that bytes changed.
        reason = str(error)
        confirmed = {"evidence_hash_mismatch", "evidence_size_mismatch", "canonical_original_bytes_mismatch",
            "unit_payload_mismatch", "canonical_unit_binding", "receipt_units_mismatch", "parser_units_hash_mismatch",
            "source_text_denominator_or_content", "canonical_unit_count_mismatch", "canonical_unit_identity",
            "manifest_bytes_unbound", "manifest_binding", "canonical_content_binding", "not_current_extraction"}
        raise CatalogStageError("wp4_evidence_drift" if reason in confirmed else "wp4_revalidation_blocked:" + reason) from error
    return {**proof, "text_sha256": row["text_sha256"], "parser": row["parser"],
            "parser_version": row["parser_version"], "extraction_import_id": import_id,
            "extraction_receipt_sha256": state["receipt_sha256"]}, len(raw)
