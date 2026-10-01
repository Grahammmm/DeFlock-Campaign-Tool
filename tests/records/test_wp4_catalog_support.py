"""Actual composed-fixture challenges; invoked by test_pipeline_composition.

No standalone mock acceptance. Every probe calls the WP5 adapter; its installed
WP4 validator is the real one. Temporary malformed rows are rolled back and
never enter positive stage acceptance. Canonical states are never rewritten.
"""
from pathlib import Path
from unittest import mock


def challenge(case, adapter, database, import_id, unit, proof):
    from campaign_tool.records import extraction_validation as validation
    from campaign_tool.records import catalog_stage
    completed = []

    def sql_probe(name, sql, parameters, expected, submitted=None):
        with adapter.store.ledger(database) as con:
            con.execute("SAVEPOINT negative_fixture")
            try:
                con.execute(sql, parameters)
                with case.assertRaisesRegex(catalog_stage.CatalogStageError, expected):
                    adapter._unit(con, submitted or proof)
            finally:
                con.execute("ROLLBACK TO negative_fixture")
                con.execute("RELEASE negative_fixture")
        completed.append(name)

    # A different existing import pointer, not merely a missing pointer.
    with adapter.store.ledger(database) as con:
        con.execute("SAVEPOINT stale_import_fixture")
        try:
            old = con.execute("SELECT * FROM extraction_adapter_imports WHERE id=?", (import_id,)).fetchone()
            other_id = "0" * 64 if import_id != "0" * 64 else "1" * 64
            con.execute("INSERT INTO extraction_adapter_imports VALUES(?,?,?,?,?,?,?)", (other_id, old["original_sha256"], other_id, *tuple(old)[3:]))
            con.execute("UPDATE extraction_adapter_current SET import_id=? WHERE original_sha256=?", (other_id, unit["original_sha256"]))
            with case.assertRaisesRegex(catalog_stage.CatalogStageError, "wp4_current_import_mismatch"):
                adapter._unit(con, proof)
        finally:
            con.execute("ROLLBACK TO stale_import_fixture")
            con.execute("RELEASE stale_import_fixture")
    completed.append("stale_current_import")
    sql_probe("extra_units", "INSERT INTO units SELECT ?,original_sha256,parser,parser_version,locator,unit_type,text_sha256,derived_path,status,legacy_ordinal,provenance_json FROM units WHERE id=?",
              ("synthetic-extra-unmanifested-unit", unit["id"]), "wp4_unit_set_mismatch")
    sql_probe("blank_locator", "UPDATE units SET locator='' WHERE id=?", (unit["id"],),
              "invalid_exact_locator", {**proof, "locator": ""})
    sql_probe("changed_ordinal", "UPDATE units SET legacy_ordinal=999 WHERE id=?", (unit["id"],),
              "wp4_import_membership_mismatch")
    sql_probe("changed_parser", "UPDATE units SET parser_version='unverified' WHERE id=?", (unit["id"],),
              "wp4_parser_mismatch")
    path = Path(unit["derived_path"])
    original, mode = path.read_bytes(), path.stat().st_mode & 0o777
    path.chmod(0o600)
    try:
        path.write_bytes(original + b"extra exact line\n")
        with adapter.store.ledger(database, readonly=True) as con:
            with case.assertRaisesRegex(catalog_stage.CatalogStageError, "wp4_line_hash_mismatch"):
                adapter._unit(con, proof)
    finally:
        path.write_bytes(original)
        path.chmod(mode)
    completed.append("changed_line")
    # Reject a missing installed authority without invoking any replacement code.
    with mock.patch.dict(adapter.stages._INSTALLED_VALIDATORS, {}, clear=True):
        with adapter.store.ledger(database, readonly=True) as con:
            with case.assertRaisesRegex(catalog_stage.CatalogStageError, "wp4_installed_binding_required"):
                adapter._unit(con, proof)
    completed.append("uninstalled_validator")
    return completed
