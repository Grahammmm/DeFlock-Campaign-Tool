"""Bounded projection of retained legacy units, not parser verification."""

import argparse
from contextlib import contextmanager
import hashlib
import json
import re
import sqlite3
import sys

from . import store
from .migrations import unit_projection_v001 as migration
from .migrations import unit_projection_guards_v001 as guards

ADAPTER_VERSION = "retained-legacy-unit-capture-v1"
MAX_BATCH_ROWS = 10000
MAX_ROW_BYTES = 2 * 1024 * 1024
MAX_BATCH_BYTES = 8 * 1024 * 1024
_SCHEMA_HASH = hashlib.sha256(migration.SQL.encode("utf-8")).hexdigest()
_SHA = re.compile(r"[0-9a-f]{64}\Z")


def _hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json(value):
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = item
        return result

    def constant(value):
        raise ValueError("nonfinite JSON value")

    return json.loads(value, object_pairs_hook=pairs, parse_constant=constant)


def _positive(value, maximum, name):
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError("invalid " + name)


def _install(connection):
    marker = "extension:" + migration.NAME
    current = connection.execute(
        "SELECT value FROM ledger_meta WHERE key=?", (marker,)
    ).fetchone()
    tables = connection.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='table' AND name IN "
        "('unit_projection_runs','unit_projection_rows','unit_projection_checkpoints')"
    ).fetchone()[0]
    if current:
        if current[0] != _SCHEMA_HASH or tables != 3:
            raise ValueError("unit extension mismatch")
        return
    if tables:
        raise ValueError("unbound unit extension")
    statement = ""
    for character in migration.SQL:
        statement += character
        if character == ";" and sqlite3.complete_statement(statement):
            connection.execute(statement)
            statement = ""
    if statement.strip():
        raise ValueError("incomplete extension SQL")
    connection.execute(
        "INSERT INTO ledger_meta(key,value) VALUES(?,?)", (marker, _SCHEMA_HASH)
    )



def _statements(sql):
    statement = ""
    for character in sql:
        statement += character
        if character == ";" and sqlite3.complete_statement(statement):
            yield statement.strip().rstrip(";").strip()
            statement = ""
    if statement.strip():
        raise ValueError("incomplete guard SQL")


def _verify_schema(connection, sql):
    # Constant-sized DDL checks, not an unbounded scan of prior unit payloads.
    for statement in _statements(sql):
        match = re.match(r"CREATE (TABLE|TRIGGER|INDEX) (\w+)", statement)
        if match is None:
            raise ValueError("unsupported guard declaration")
        kind, name = match.groups()
        actual = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type=? AND name=?",
            (kind.lower(), name),
        ).fetchone()
        if actual is None or actual[0].strip().rstrip(";").strip() != statement:
            raise ValueError("unit capture protection missing or changed")


def _protect_bindings(connection):
    _verify_schema(connection, migration.SQL)
    checksum = _hash(guards.SQL)
    marker = connection.execute(
        "SELECT value FROM ledger_meta WHERE key=?", (guards.MARKER,)
    ).fetchone()
    if marker is not None:
        if marker[0] != checksum:
            raise ValueError("unit binding guard checksum mismatch")
        _verify_schema(connection, guards.SQL)
        return
    # Never grandfather evidence that may already have been modified before guards.
    # Revalidation/migration of an older candidate is a separate explicit operation.
    if connection.execute("SELECT 1 FROM unit_projection_runs LIMIT 1").fetchone():
        raise ValueError("unguarded unit history requires separate revalidation")
    for statement in _statements(guards.SQL):
        connection.execute(statement)
    connection.execute(
        "INSERT INTO ledger_meta(key,value) VALUES(?,?)", (guards.MARKER, checksum)
    )



class _AppendPermit:
    """Connection-local, statement-specific append capability; unavailable to plain SQL."""

    def __init__(self, connection):
        self.pending = None
        connection.create_function("unit_capture_append_allowed", -1, self._allowed)
        connection.create_function(
            "unit_capture_association_valid", 11,
            lambda *values: self._validate(_association_matches, connection, values))
        connection.create_function(
            "unit_capture_checkpoint_valid", 6,
            lambda *values: self._validate(_checkpoint_matches, connection, values))

    def _allowed(self, *values):
        return int(self.pending is not None and self.pending == values)

    @staticmethod
    def _validate(function, connection, values):
        try:
            return int(function(connection, *values))
        except (ValueError, TypeError, KeyError, IndexError, RecursionError, sqlite3.Error):
            return 0

    @contextmanager
    def allow(self, kind, values):
        if self.pending is not None:
            raise ValueError("nested append permit")
        self.pending = (kind, *values)
        try:
            yield
        finally:
            self.pending = None


def _association_matches(connection, run, import_id, rowid, payload_hash, unit_id,
                         disposition, reason, text, text_hash, locator, checked):
    capture = connection.execute(
        "SELECT * FROM unit_projection_runs WHERE id=? AND import_id=?",
        (run, import_id)).fetchone()
    if capture is None or not isinstance(reason, str) or not 1 <= len(reason) <= 128:
        return False
    checkpoint = connection.execute(
        "SELECT last_source_rowid,summary_json FROM unit_projection_checkpoints "
        "WHERE projection_id=? ORDER BY batch_number DESC LIMIT 1", (run,)).fetchone()
    if checkpoint and (_json(checkpoint["summary_json"])["status"] == "complete" or
                       (checkpoint["last_source_rowid"] is not None and
                        rowid <= checkpoint["last_source_rowid"])):
        return False
    source = connection.execute(
        "SELECT payload_sha256,length(CAST(payload_json AS BLOB)) AS size "
        "FROM legacy_rows WHERE import_id=? AND source_table='units' AND source_rowid=?",
        (import_id, rowid)).fetchone()
    if source is None or source["payload_sha256"] != payload_hash:
        return False
    if checked == 0:
        return (disposition == "blocked" and reason == "resource_row_too_large" and
                unit_id is None and text is None and text_hash is None and locator is None)
    if checked != 1 or source["size"] > MAX_ROW_BYTES:
        return False
    source = connection.execute(
        "SELECT identity_json,payload_json FROM legacy_rows WHERE import_id=? "
        "AND source_table='units' AND source_rowid=?", (import_id, rowid)).fetchone()
    if _hash(source["payload_json"]) != payload_hash:
        return False
    if disposition != "projected":
        return disposition in ("blocked", "scope_excluded") and unit_id is None
    row = _json(source["payload_json"])
    if (not isinstance(row, dict) or
        _json(source["identity_json"]) != {"sha": row.get("sha"), "ordinal": row.get("ordinal")} or
        type(row.get("ordinal")) is not int or row["ordinal"] < 0 or
        not isinstance(row.get("sha"), str) or not _SHA.fullmatch(row["sha"]) or
        not isinstance(text, str) or row.get("text") != text or
        not isinstance(locator, str) or row.get("locator") != locator or
        not isinstance(row.get("kind"), str) or not row["kind"].strip() or
        "\x00" in row["kind"] or reason != "missing_parser_provenance"):
        return False
    decoded_locator = _json(locator)
    if not isinstance(decoded_locator, dict) or not decoded_locator:
        return False
    original = connection.execute(
        "SELECT scope FROM originals WHERE sha256=?", (row["sha"],)).fetchone()
    if original is None or original["scope"] == "out_of_scope":
        return False
    adapter_hash = capture["adapter_sha256"]
    expected_id = "captured-unit:" + _hash(store.canonical(
        [ADAPTER_VERSION, adapter_hash, row["sha"], row["ordinal"], payload_hash]))
    provenance = {
        "adapter": ADAPTER_VERSION, "adapter_sha256": adapter_hash,
        "source_payload_sha256": payload_hash, "parser_provenance": "missing",
        "original_parser_verified": False, "text_claim": "captured_observation_only",
    }
    expected = {
        "id": expected_id, "original_sha256": row["sha"],
        "parser": None, "parser_version": None, "locator": locator,
        "unit_type": row["kind"], "text_sha256": _hash(text),
        "derived_path": "ledger-unit-capture:" + expected_id,
        "status": "captured_unverified", "legacy_ordinal": row["ordinal"],
        "provenance_json": store.canonical(provenance),
    }
    canonical = connection.execute("SELECT * FROM units WHERE id=?", (unit_id,)).fetchone()
    return (unit_id == expected_id and text_hash == expected["text_sha256"] and
            canonical is not None and all(canonical[key] == value for key, value in expected.items()))


def _checkpoint_matches(connection, run, batch, last, previous_hash, checksum, summary_json):
    """Validate one bounded batch of metadata; preceding checkpoints are immutable."""
    capture = connection.execute(
        "SELECT * FROM unit_projection_runs WHERE id=?", (run,)).fetchone()
    if capture is None or type(batch) is not int or batch < 1:
        return False
    previous = connection.execute(
        "SELECT * FROM unit_projection_checkpoints WHERE projection_id=? AND batch_number<? "
        "ORDER BY batch_number DESC LIMIT 1", (run, batch)).fetchone()
    if previous is None:
        if batch != 1 or previous_hash is not None:
            return False
        before = None
        expected = _initial_summary(run, capture["import_id"], capture["expected_rows"])
    else:
        if batch != previous["batch_number"] + 1 or previous_hash != previous["checkpoint_sha256"]:
            return False
        before = previous["last_source_rowid"]
        expected = _json(previous["summary_json"])
        if expected["status"] == "complete":
            return False
    predicate, args = "projection_id=?", [run]
    if before is not None:
        predicate += " AND source_rowid>?"
        args.append(before)
    rows = connection.execute(
        "SELECT source_rowid,disposition,reason FROM unit_projection_rows WHERE " +
        predicate + " ORDER BY source_rowid LIMIT ?", (*args, MAX_BATCH_ROWS + 1)).fetchall()
    if len(rows) > MAX_BATCH_ROWS or last != (rows[-1]["source_rowid"] if rows else before):
        return False
    predicate, args = "import_id=? AND source_table='units'", [capture["import_id"]]
    if before is not None:
        predicate += " AND source_rowid>?"
        args.append(before)
    if last is not None:
        predicate += " AND source_rowid<=?"
        args.append(last)
    sources = connection.execute(
        "SELECT source_rowid FROM legacy_rows WHERE " + predicate +
        " ORDER BY source_rowid LIMIT ?", (*args, MAX_BATCH_ROWS + 1)).fetchall()
    if [row["source_rowid"] for row in sources] != [row["source_rowid"] for row in rows]:
        return False
    for row in rows:
        if row["disposition"] not in ("projected", "blocked", "scope_excluded"):
            return False
        expected["processed_source_rows"] += 1
        expected[row["disposition"] + "_source_rows"] += 1
        reasons = expected["unresolved_reasons"]
        reasons[row["reason"]] = reasons.get(row["reason"], 0) + 1
    if expected["processed_source_rows"] > capture["expected_rows"]:
        return False
    expected["batches"] += 1
    if expected["processed_source_rows"] == capture["expected_rows"]:
        expected["status"] = "complete"
    if not rows and not (batch == 1 and capture["expected_rows"] == 0):
        return False
    actual = _json(summary_json)
    return actual == expected and checksum == _hash(store.canonical(
        [run, batch, last, previous_hash, actual]))


def _gap(connection, run, rowid, sha, reason):
    gap_id = "unit-capture-gap:" + _hash(store.canonical([run, rowid, reason]))
    connection.execute(
        "INSERT INTO import_gaps(id,import_id,subject_sha256,category,reason,owner,status) "
        "SELECT ?,import_id,?,'unit_capture',?,'unit_capture_reconciliation','blocked' "
        "FROM unit_projection_runs WHERE id=?",
        (gap_id, sha, store.canonical({"reason": reason, "projection_id": run,
                                     "source_table": "units", "source_rowid": rowid}), run),
    )


def _insert_unit(connection, values):
    previous = connection.execute(
        "SELECT * FROM units WHERE id=?", (values["id"],)
    ).fetchone()
    if previous:
        if any(previous[key] != value for key, value in values.items()):
            raise ValueError("canonical unit conflicts with captured evidence")
        return
    columns = tuple(values)
    connection.execute(
        "INSERT INTO units(" + ",".join(columns) + ") VALUES(" +
        ",".join("?" for _ in columns) + ")",
        tuple(values[key] for key in columns),
    )


def _project_row(connection, run, import_id, source, adapter_hash, oversized=False, permit=None):
    rowid, payload_hash = source["source_rowid"], source["payload_sha256"]
    disposition, reason = "blocked", "resource_row_too_large"
    observed_text = observed_hash = locator = unit_id = sha = None
    checked = 0
    if not oversized:
        payload = source["payload_json"]
        if not isinstance(payload, str) or _hash(payload) != payload_hash:
            raise ValueError("captured payload hash mismatch")
        checked = 1
        try:
            row = _json(payload)
            identity = _json(source["identity_json"])
        except (ValueError, TypeError, RecursionError):
            row = None
            reason = "invalid_captured_json"
        if isinstance(row, dict):
            if identity != {"sha": row.get("sha"), "ordinal": row.get("ordinal")}:
                raise ValueError("captured unit identity mismatch")
            sha = row.get("sha")
            if not isinstance(sha, str) or not _SHA.fullmatch(sha):
                sha, reason = None, "invalid_original_sha256"
            elif type(row.get("ordinal")) is not int or row["ordinal"] < 0:
                reason = "invalid_legacy_ordinal"
            else:
                original = connection.execute(
                    "SELECT scope FROM originals WHERE sha256=?", (sha,)
                ).fetchone()
                if original is None:
                    reason = "canonical_original_missing"
                elif original["scope"] == "out_of_scope":
                    disposition, reason = "scope_excluded", "original_out_of_scope"
                else:
                    observed_text = row.get("text")
                    locator = row.get("locator")
                    kind = row.get("kind")
                    try:
                        decoded_locator = _json(locator) if isinstance(locator, str) else None
                    except (ValueError, TypeError, RecursionError):
                        decoded_locator = None
                    if not isinstance(observed_text, str):
                        observed_text, reason = None, "observed_text_missing_or_invalid"
                    elif not isinstance(locator, str) or not isinstance(decoded_locator, dict) or not decoded_locator:
                        reason = "exact_locator_missing_or_invalid"
                    elif not isinstance(kind, str) or not kind.strip() or "\x00" in kind:
                        reason = "unit_kind_missing_or_invalid"
                    else:
                        observed_hash = _hash(observed_text)
                        unit_id = "captured-unit:" + _hash(store.canonical(
                            [ADAPTER_VERSION, adapter_hash, sha, row["ordinal"], payload_hash]
                        ))
                        provenance = {
                            "adapter": ADAPTER_VERSION,
                            "adapter_sha256": adapter_hash,
                            "source_payload_sha256": payload_hash,
                            "parser_provenance": "missing",
                            "original_parser_verified": False,
                            "text_claim": "captured_observation_only",
                        }
                        # A logical ledger locator, never a claimed filesystem artifact.
                        derived = "ledger-unit-capture:" + unit_id
                        _insert_unit(connection, {
                            "id": unit_id, "original_sha256": sha,
                            "parser": None, "parser_version": None,
                            "locator": locator, "unit_type": kind,
                            "text_sha256": observed_hash, "derived_path": derived,
                            "status": "captured_unverified",
                            "legacy_ordinal": row["ordinal"],
                            "provenance_json": store.canonical(provenance),
                        })
                        disposition, reason = "projected", "missing_parser_provenance"
        elif row is not None:
            reason = "captured_unit_not_object"
    # Invalid fields remain available in the exact legacy payload, not coerced here.
    if not isinstance(locator, str):
        locator = None
    values = (run, import_id, rowid, payload_hash, unit_id, disposition, reason,
              observed_text, observed_hash, locator, checked)
    if permit is None:
        raise ValueError("unit association append permit required")
    with permit.allow("row", values):
        connection.execute(
            "INSERT INTO unit_projection_rows VALUES(?,?,'units',?,?,?,?,?,?,?,?,?)", values)

    _gap(connection, run, rowid, sha, reason)
    return disposition, reason


def _initial_summary(run, import_id, expected):
    return {
        "projection_id": run, "import_id": import_id, "adapter": ADAPTER_VERSION,
        "expected_source_rows": expected, "processed_source_rows": 0,
        "projected_source_rows": 0, "blocked_source_rows": 0,
        "scope_excluded_source_rows": 0, "unresolved_reasons": {},
        "batches": 0, "status": "in_progress", "stage_promotions": 0,
        "original_parser_verified": False, "publication_ready": False,
        "end_to_end_complete": None,
    }


def project_units(database, import_id, *, batch_size=1000, max_batches=1,
                  max_row_bytes=MAX_ROW_BYTES, max_batch_bytes=MAX_BATCH_BYTES):
    """Append atomic bounded batches. Reinvoke with the same capture to resume."""
    if not isinstance(import_id, str) or not _SHA.fullmatch(import_id):
        raise ValueError("invalid import id")
    _positive(batch_size, MAX_BATCH_ROWS, "batch size")
    _positive(max_batches, 1000, "max batches")
    _positive(max_row_bytes, MAX_ROW_BYTES, "row byte limit")
    _positive(max_batch_bytes, MAX_BATCH_BYTES, "batch byte limit")
    if max_row_bytes > max_batch_bytes:
        raise ValueError("row limit exceeds batch limit")
    adapter_hash = store.file_hash(__file__)
    with store.ledger(database) as connection:
        if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise ValueError("unit projection requires foreign keys")
        permit = _AppendPermit(connection)
        for _ in range(max_batches):
            connection.execute("BEGIN IMMEDIATE")
            try:
                capture = connection.execute(
                    "SELECT * FROM legacy_imports WHERE id=?", (import_id,)
                ).fetchone()
                if capture is None or capture["status"] != "captured":
                    raise ValueError("completed capture required")
                # Resource policy is part of the immutable interpretation/run identity.
                run = _hash(store.canonical([
                    ADAPTER_VERSION, adapter_hash, _SCHEMA_HASH, import_id,
                    capture["source_database_sha256"], capture["source_manifest_sha256"],
                    max_row_bytes, max_batch_bytes,
                ]))
                _install(connection)
                _protect_bindings(connection)
                existing = connection.execute(
                    "SELECT * FROM unit_projection_runs WHERE id=?", (run,)
                ).fetchone()
                if existing is None:
                    declared = _json(capture["declared_table_counts"])
                    expected = declared.get("units")
                    if type(expected) is not int or expected < 0:
                        raise ValueError("invalid declared unit count")
                    actual = connection.execute(
                        "SELECT count(*) FROM legacy_rows WHERE import_id=? AND source_table='units'",
                        (import_id,),
                    ).fetchone()[0]
                    if actual != expected:
                        raise ValueError("captured unit count mismatch")
                    values = (run, import_id, ADAPTER_VERSION, adapter_hash,
                              capture["source_database_sha256"],
                              capture["source_manifest_sha256"], expected)
                    with permit.allow("run", values):
                        connection.execute(
                            "INSERT INTO unit_projection_runs VALUES(?,?,?,?,?,?,?)", values)
                else:
                    expected = existing["expected_rows"]
                checkpoint = connection.execute(
                    "SELECT * FROM unit_projection_checkpoints WHERE projection_id=? "
                    "ORDER BY batch_number DESC LIMIT 1", (run,),
                ).fetchone()
                if checkpoint:
                    summary = _json(checkpoint["summary_json"])
                    previous_hash = checkpoint["checkpoint_sha256"]
                    expected_hash = _hash(store.canonical([
                        run, checkpoint["batch_number"], checkpoint["last_source_rowid"],
                        checkpoint["previous_sha256"], summary,
                    ]))
                    if previous_hash != expected_hash or not _checkpoint_matches(
                        connection, run, checkpoint["batch_number"],
                        checkpoint["last_source_rowid"], checkpoint["previous_sha256"],
                        previous_hash, checkpoint["summary_json"],
                    ):
                        raise ValueError("checkpoint integrity or correspondence mismatch")
                    last = checkpoint["last_source_rowid"]
                    if summary["status"] == "complete":
                        connection.rollback()
                        return dict(summary, reused=True)
                else:
                    summary = _initial_summary(run, import_id, expected)
                    previous_hash, last = None, None
                predicate = "import_id=? AND source_table='units'"
                parameters = [import_id]
                if last is not None:
                    predicate += " AND source_rowid>?"
                    parameters.append(last)
                metadata = connection.execute(
                    "SELECT source_rowid,payload_sha256,length(CAST(payload_json AS BLOB)) AS size "
                    "FROM legacy_rows WHERE " + predicate +
                    " ORDER BY source_rowid LIMIT ?", (*parameters, batch_size),
                ).fetchall()
                selected, total_bytes = [], 0
                for source in metadata:
                    cost = source["size"] if source["size"] <= max_row_bytes else 0
                    if selected and total_bytes + cost > max_batch_bytes:
                        break
                    selected.append(source)
                    total_bytes += cost
                if not selected and summary["processed_source_rows"] != expected:
                    raise ValueError("checkpoint exhausted before declared count")
                for source in selected:
                    oversized = source["size"] > max_row_bytes
                    if not oversized:
                        source = connection.execute(
                            "SELECT * FROM legacy_rows WHERE import_id=? AND "
                            "source_table='units' AND source_rowid=?",
                            (import_id, source["source_rowid"]),
                        ).fetchone()
                    disposition, reason = _project_row(
                        connection, run, import_id, source, adapter_hash, oversized, permit=permit,
                    )
                    summary["processed_source_rows"] += 1
                    summary[disposition + "_source_rows"] += 1
                    reasons = summary["unresolved_reasons"]
                    reasons[reason] = reasons.get(reason, 0) + 1
                    last = source["source_rowid"]
                if summary["processed_source_rows"] > expected:
                    raise ValueError("projection exceeds declared count")
                summary["batches"] += 1
                if summary["processed_source_rows"] == expected:
                    summary["status"] = "complete"
                checkpoint_hash = _hash(store.canonical([
                    run, summary["batches"], last, previous_hash, summary,
                ]))
                values = (run, summary["batches"], last, previous_hash,
                          checkpoint_hash, store.canonical(summary))
                with permit.allow("checkpoint", values):
                    connection.execute(
                        "INSERT INTO unit_projection_checkpoints VALUES(?,?,?,?,?,?)", values)
                connection.commit()
                if summary["status"] == "complete":
                    return dict(summary, reused=False)
            except BaseException:
                connection.rollback()
                raise
        return dict(summary, reused=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--import-id", required=True)
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--max-batches", type=int, default=1)
    args = parser.parse_args(argv)
    try:
        result = project_units(args.database, args.import_id,
                               batch_size=args.batch_size, max_batches=args.max_batches)
    except (ValueError, OSError, sqlite3.Error, TypeError, KeyError):
        print('{"status":"blocked","reason":"unit_capture_failed"}', file=sys.stderr)
        return 1
    print(store.canonical(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
