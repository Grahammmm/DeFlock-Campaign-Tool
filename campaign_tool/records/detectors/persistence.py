"""Bounded private persistence of computed detector evidence, never acceptance."""
from datetime import datetime, timezone
import json
import math
import sqlite3

from .core import MAX_INPUT_BYTES, MAX_UNITS, VERSION, canonical, digest, evaluate, moment, sha
from .migrations import persistence_v001 as migration

ADAPTER_VERSION = migration.VERSION
MAX_RESULT_BYTES = 16 * 1024 * 1024
MAX_HITS = 10000
MAX_STRUCTURE_NODES = 100000
MAX_STRUCTURE_DEPTH = 32


class PersistenceError(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise PersistenceError(reason)


def _trusted_now():
    """Execution admission uses the installed process clock, never record config."""
    return datetime.now(timezone.utc)


def _preflight(units, joins, rules, config):
    """Bound native JSON shape/cardinality before copying or serializing inputs."""
    require(type(units) is list and len(units) <= MAX_UNITS, "input_unit_row_bound")
    require(type(joins) is list and len(joins) <= MAX_UNITS * 2, "input_join_row_bound")
    require(type(rules) is dict and type(config) is dict, "input_rules_config_shape")
    entries = rules.get("entries", [])
    require(type(entries) is list and len(entries) <= 256, "input_rule_row_bound")
    require(all(type(row) is dict for row in units), "input_unit_shape")
    require(all(type(row) is dict for row in joins), "input_join_shape")
    require(all(type(row) is dict for row in entries), "input_rule_shape")
    nodes, characters = 0, 0

    def visit(value, depth):
        nonlocal nodes, characters
        nodes += 1
        require(nodes <= MAX_STRUCTURE_NODES and depth <= MAX_STRUCTURE_DEPTH, "input_structure_bound")
        kind = type(value)
        if kind is str:
            characters += len(value)
            require(characters <= MAX_INPUT_BYTES, "input_character_bound")
        elif kind is dict:
            require(len(value) <= (MAX_STRUCTURE_NODES - nodes) // 2, "input_structure_bound")
            for key, item in value.items():
                require(type(key) is str, "input_json_key_shape")
                visit(key, depth + 1)
                visit(item, depth + 1)
        elif kind is list:
            require(len(value) <= MAX_STRUCTURE_NODES - nodes, "input_structure_bound")
            for item in value:
                visit(item, depth + 1)
        elif kind is int:
            require(value.bit_length() <= 4096, "input_integer_bound")
        elif kind is float:
            require(math.isfinite(value), "input_nonfinite_number")
        else:
            require(value is None or kind is bool, "input_json_value_shape")

    for value in (units, joins, rules, config):
        visit(value, 0)


def one(connection, sql, values=()):
    cursor = connection.execute(sql, values)
    row = cursor.fetchone()
    return None if row is None else dict(zip((column[0] for column in cursor.description), row))


def insert(connection, table, row):
    columns = ",".join(row)
    placeholders = ",".join("?" for _ in row)
    connection.execute(f"INSERT INTO {table}({columns}) VALUES({placeholders})", tuple(row.values()))


def semantic_hit(hit):
    # A semantic event can be printed at new locations or included in a new rule
    # pack. Keep each run's complete evidence in wp6_run_hits instead of replacing
    # the first canonical occurrence. The actual applicable rule is fingerprinted.
    return {key: value for key, value in hit.items()
            if key not in ("original_sha256", "locator", "evidence", "rules_version")}


def persist_evaluation(connection, *, run_id, detector, units, joins, rules, config):
    """Compute and atomically persist one detector evaluation under an existing run.

    The caller supplies a private WP1 SQLite connection outside a transaction.
    This API neither creates canonical runs nor accepts precomputed hits/manifests.
    Exact replay validates saved evidence; changed-input reuse of a run ID fails.
    """
    require(isinstance(connection, sqlite3.Connection), "sqlite_connection_required")
    require(not connection.in_transaction, "caller_transaction_must_be_closed")
    require(isinstance(run_id, str) and bool(run_id.strip()) and len(run_id) <= 256, "run_id_required")
    _preflight(units, joins, rules, config)
    try:
        raw = canonical({"units": units, "joins": joins, "rules": rules, "config": config})
        require(len(raw.encode()) <= MAX_INPUT_BYTES, "input_byte_bound")
        supplied = json.loads(raw)
        result = evaluate(detector, **supplied)
    except (TypeError, ValueError, OverflowError) as exc:
        raise PersistenceError("invalid_detector_input: " + str(exc)) from exc
    require(isinstance(rules.get("version"), str) and bool(rules["version"].strip()), "rules_version_required")
    hits, manifest = result["hits"], result["manifest"]
    require(len(hits) <= MAX_HITS and len(canonical(result).encode()) <= MAX_RESULT_BYTES, "result_bound")
    require(len({hit["dedupe_key"] for hit in hits}) == len(hits), "duplicate_computed_hit_keys")
    connection.execute("PRAGMA foreign_keys=ON")
    require(connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1, "foreign_keys_required")
    connection.execute("BEGIN IMMEDIATE")
    try:
        run = one(connection, "SELECT * FROM runs WHERE run_id=?", (run_id,))
        require(run is not None, "canonical_run_missing")
        require(run["kind"] in ("detector", "detector-runner", "records-runner"), "run_kind_not_detector_capable")
        require(isinstance(run["engine_version"], str) and bool(run["engine_version"].strip()), "run_engine_version_missing")
        require(moment(run["started_at"]) <= _trusted_now(), "run_started_in_future")
        require(run["config_sha256"] == digest(supplied["config"]), "run_config_mismatch")
        provenance = {key: value for key, value in run.items() if key not in ("status", "ended_at", "summary")}
        source_bindings = []
        subjects = sorted({unit.get("original_sha256") for unit in supplied["units"]
                           if sha(unit.get("original_sha256"))})
        for subject in subjects:
            original = one(connection, "SELECT * FROM originals WHERE sha256=?", (subject,))
            require(original is not None, "canonical_original_missing")
            source_bindings.append({"run_id": run_id, "original_sha256": subject,
                                    "metadata_sha256": digest(original)})
        migration.apply(connection)
        saved = one(connection, "SELECT * FROM wp6_manifests WHERE run_id=?", (run_id,))
        manifest_row = {
            "run_id": run_id, "adapter_version": ADAPTER_VERSION,
            "manifest_sha256": manifest["manifest_sha256"], "input_sha256": manifest["input_sha256"],
            "units_sha256": digest(supplied["units"]), "joins_sha256": digest(supplied["joins"]),
            "rules_sha256": digest(supplied["rules"]), "config_sha256": digest(supplied["config"]),
            "run_provenance_json": canonical(provenance), "manifest_json": canonical(manifest),
            "result_sha256": digest(result), "hit_count": len(hits),
        }
        detector_row = {"run_id": run_id, "detector": detector, "detector_version": VERSION,
                        "rules_version": supplied["rules"]["version"], "started_at": run["started_at"],
                        **{key: manifest["counts"][key] for key in ("eligible", "evaluated", "skipped", "blocked")},
                        "manifest_sha256": manifest["manifest_sha256"]}
        canonical_run = one(connection, "SELECT * FROM detector_runs WHERE run_id=?", (run_id,))
        replay = saved is not None
        if replay:
            require(saved == manifest_row, "run_manifest_replay_mismatch")
            require(canonical_run == detector_row, "canonical_detector_run_changed")
        else:
            require(run["status"] == "running" and run["ended_at"] is None, "new_evaluation_requires_active_run")
            require(canonical_run is None, "unmanaged_detector_run_refused")
            insert(connection, "detector_runs", detector_row)
            insert(connection, "wp6_manifests", manifest_row)
        for binding in source_bindings:
            if replay:
                stored = one(connection, "SELECT * FROM wp6_input_originals WHERE run_id=? AND original_sha256=?",
                             (run_id, binding["original_sha256"]))
                require(stored == binding, "source_binding_changed")
            else:
                insert(connection, "wp6_input_originals", binding)
        for hit in hits:
            key = hit["dedupe_key"]
            identity = one(connection, "SELECT * FROM wp6_hit_identities WHERE dedupe_key=?", (key,))
            canonical_hit = one(connection, "SELECT * FROM detector_hits WHERE dedupe_key=?", (key,))
            semantic = semantic_hit(hit)
            if identity is None:
                require(not replay and canonical_hit is None, "unmanaged_or_missing_hit_identity")
                canonical_hit = {"id": key, "run_id": run_id, "original_sha256": hit["original_sha256"],
                                 "locators": canonical(hit["evidence"]), "severity": hit["severity"],
                                 "observed_value_private": canonical(hit["observed_values_private"]),
                                 "applicability": canonical(hit["applicability"]), "dedupe_key": key}
                insert(connection, "detector_hits", canonical_hit)
                insert(connection, "wp6_hit_identities", {
                    "dedupe_key": key, "semantic_json": canonical(semantic), "semantic_sha256": digest(semantic),
                    "canonical_row_sha256": digest(canonical_hit),
                })
            else:
                require(identity["semantic_json"] == canonical(semantic) and
                        identity["semantic_sha256"] == digest(semantic), "semantic_hit_key_conflict")
                require(canonical_hit is not None and identity["canonical_row_sha256"] == digest(canonical_hit),
                        "canonical_hit_changed")
            link = {"run_id": run_id, "dedupe_key": key, "hit_json": canonical(hit), "hit_sha256": digest(hit)}
            if replay:
                require(one(connection, "SELECT * FROM wp6_run_hits WHERE run_id=? AND dedupe_key=?",
                            (run_id, key)) == link, "run_hit_link_changed")
            else:
                insert(connection, "wp6_run_hits", link)
        require(connection.execute("SELECT COUNT(*) FROM wp6_run_hits WHERE run_id=?", (run_id,)).fetchone()[0]
                == len(hits), "run_hit_count_mismatch")
        require(connection.execute("SELECT COUNT(*) FROM wp6_input_originals WHERE run_id=?", (run_id,)).fetchone()[0]
                == len(source_bindings), "source_binding_count_mismatch")
        connection.commit()
    except (sqlite3.Error, ValueError, KeyError) as exc:
        connection.rollback()
        if isinstance(exc, PersistenceError):
            raise
        raise PersistenceError("persistence_integrity_failure: " + str(exc)) from exc
    except BaseException:
        connection.rollback()
        raise
    return {"schema": ADAPTER_VERSION, "run_id": run_id, "detector": detector,
            "detector_version": VERSION, "manifest_sha256": manifest["manifest_sha256"],
            "input_sha256": manifest["input_sha256"], "result_sha256": digest(result),
            "hit_keys": [hit["dedupe_key"] for hit in hits], "hit_count": len(hits),
            "replayed": replay, "stage_promotions": 0, "acceptance": "supplied_evidence_triage_only",
            "publication_ready": False}
