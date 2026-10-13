"""Read-only distinction between captured declarations and genuine acquisition.

Labels are insufficient: recompute the released projection from immutable input
bindings. This never upgrades the declaration or resolves its provenance gaps.
"""
from . import occurrence_projection as projection


def verify_captured_declaration(con, row, *, depth=0):
    if depth > 32:
        raise ValueError("captured_declaration_parent_bound")
    marker = con.execute("SELECT value FROM ledger_meta WHERE key=?",
                         ("extension:"+projection.migration.NAME,)).fetchone()
    if marker is None or marker[0] != projection.SCHEMA_HASH:
        raise ValueError("captured_declaration_extension_unbound")
    evidence = projection.decode(row["evidence"])
    keys = {"projection_id", "import_id", "source_table", "source_rowid",
            "payload_sha256", "verification", "agency_request_join"}
    if (type(evidence) is not dict or set(evidence) != keys
            or evidence["verification"] != "captured_declaration_only"
            or evidence["agency_request_join"] != "blocked"):
        raise ValueError("captured_declaration_evidence_mismatch")
    source = con.execute("SELECT l.*,r.payload_json AS projected_payload,r.payload_sha256 AS projected_sha "
        "FROM occurrence_projection_rows r JOIN legacy_rows l ON "
        "l.import_id=r.import_id AND l.source_table=r.source_table AND l.source_rowid=r.source_rowid "
        "WHERE r.projection_id=? AND r.import_id=? AND r.source_table='occurrences' "
        "AND r.source_rowid=? AND r.occurrence_id=? AND r.disposition='projected'",
        (evidence["projection_id"], evidence["import_id"], evidence["source_rowid"], row["id"])).fetchone()
    if (source is None or source["projected_payload"] != source["payload_json"]
            or source["projected_sha"] != source["payload_sha256"]):
        raise ValueError("captured_declaration_source_unbound")
    if source["payload_sha256"] != evidence["payload_sha256"]:
        raise ValueError("captured_declaration_source_unbound")
    raw = projection.decode_source(source)
    original = con.execute("SELECT sha256,scope,legacy_format FROM originals WHERE sha256=?", (raw.get("sha"),)).fetchone()
    candidate, reason, receipt, locator = projection._base_plan(raw, original)
    if reason == "container_pending":
        identity = projection.decode(row["source_ref"])
        parent = con.execute("SELECT * FROM occurrences WHERE id=?", (identity.get("message_occurrence_id"),)).fetchone()
        if parent is None:
            raise ValueError("captured_declaration_parent_missing")
        parent_candidate = verify_captured_declaration(con, parent, depth=depth+1)
        candidate, reason = projection._container_plan(raw, receipt, locator,
            {(receipt.get("parent_legacy_oid"), raw.get("parent")): {"candidate": parent_candidate}})
    if candidate is None or candidate["id"] != row["id"]:
        raise ValueError("captured_declaration_plan_mismatch")
    projection._verify_canonical_occurrence(con, candidate, required=True)
    return candidate


def validate_declarations(con, subject):
    # Projection ownership comes from immutable source bindings, never mutable
    # occurrence labels. Include current claims too, so relabeling or changing
    # the subject cannot hide a corrupted projected declaration.
    exists = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                         "AND name='occurrence_projection_rows'").fetchone()
    claimed = list(con.execute("SELECT id FROM occurrences WHERE original_sha256=? "
                              "AND acquisition_method LIKE 'captured_legacy_%' LIMIT 1025", (subject,)))
    owned = [] if not exists else list(con.execute(
        "SELECT DISTINCT r.occurrence_id FROM occurrence_projection_rows r "
        "LEFT JOIN occurrences o ON o.id=r.occurrence_id WHERE r.disposition='projected' "
        "AND (json_extract(r.payload_json,'$.sha')=? OR o.original_sha256=?) LIMIT 1025",
        (subject, subject)))
    ids = {row[0] for row in owned + claimed}
    if len(ids) > 1024:
        raise ValueError("preservation_occurrence_bound")
    for oid in sorted(ids):
        row = con.execute("SELECT * FROM occurrences WHERE id=?", (oid,)).fetchone()
        if row is None:
            raise ValueError("captured_declaration_occurrence_missing")
        verify_captured_declaration(con, row)
