"""Run the existing seven usage detectors through their authoritative contract.

F-042 thin runner, REVIEW REQUIRED. It composes only existing interfaces:

    pipeline.canonical_joins(connection, source_bindings)
    pipeline.evaluate_structured(*, subject_sha256, normalized, source_bindings,
                                 joins, rules, config)
    persistence.persist_evaluation(connection, *, run_id, detector, units,
                                   joins, rules, config)
    ledger.store.ledger(path, readonly=True)   # sanctioned read-only copy API

No second unit, joins or rules format is introduced. Every input path is
absolute and must match an owner-supplied expected SHA256 for its exact bytes:

    --inventory        coordinator-supplied source_bindings list (authoritative;
                       never derived from producer/worker output)
    --units            F-040a producer output ({"normalized": ..., "run_id": ...})
                       or a bare structured-normalization-v1 object
    --canonical-copy   admitted canonical-ledger v001 copy (rollback journal only)
    --rules            existing WP6 rules {version, entries[, holiday_tables]}
    --config           detector config object

A required input that is not supplied blocks every detector with the adapter's
typed reason required_context_unresolved; the adapter is not called and nothing
is fabricated. Missing context is never a clean result. Adapter outcomes,
typed reasons, hits, suppressed hit counts and revisions are preserved
verbatim. Computed evaluations are persisted with the existing persistence API
into a private ledger inside an exclusive new --out-root, never into the
canonical copy, whose bytes are verified unchanged after success and failure.

Exit codes: 0 clean (every detector evaluated or inapplicable, all persisted);
2 refused (identity/shape/boundary failure, nothing evaluated); 3 completed
with blocked detectors or missing context; 4 persistence failed.
publication_ready=false, stage_promotions=0, review_required=true always.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from campaign_tool.records.detectors import core, persistence, pipeline  # noqa: E402
from campaign_tool.records.ledger import store  # noqa: E402

SCHEMA = "detectors-run-v1"
MANIFEST_SCHEMA = "detectors-run-manifest-v1"
ENGINE_VERSION = "detectors-run-v1"
RUN_KIND = "detector-runner"
INPUTS = ("inventory", "units", "canonical_copy", "rules", "config")
JSON_INPUTS = ("inventory", "units", "rules", "config")
MAX_CANONICAL_COPY_BYTES = 1024 * 1024 * 1024
SIDECARS = ("-wal", "-shm", "-journal")
# Exact current adapter identities. Any drift refuses before inputs are read.
BOUND_SIGNATURES = {
    "pipeline.evaluate_structured": (
        lambda: pipeline.evaluate_structured,
        "(*, subject_sha256, normalized=None, source_bindings=None, joins=None, rules=None, config=None)"),
    "pipeline.canonical_joins": (lambda: pipeline.canonical_joins, "(connection, source_bindings)"),
    "pipeline._entry": (lambda: pipeline._entry, "(status, reason, result=None)"),
    "persistence.persist_evaluation": (
        lambda: persistence.persist_evaluation,
        "(connection, *, run_id, detector, units, joins, rules, config)"),
    "store.ledger": (lambda: store.ledger, "(path, *, readonly=False)"),
    "store.initialize": (lambda: store.initialize, "(path)"),
    "store.effective_original": (lambda: store.effective_original, "(connection, subject)"),
}
BOUND_VERSIONS = {
    "pipeline.VERSION": (lambda: pipeline.VERSION, "structured-detector-adapter-v1"),
    "pipeline.NORMALIZED_SCHEMA": (lambda: pipeline.NORMALIZED_SCHEMA, "structured-normalization-v1"),
    "core.VERSION": (lambda: core.VERSION, "wp6-pure-detectors-v2"),
    "persistence.ADAPTER_VERSION": (lambda: persistence.ADAPTER_VERSION, "detector-persistence-v1"),
    "store.v001.VERSION": (lambda: store.v001.VERSION, 1),
    "pipeline.CanonicalJoins.fields": (
        lambda: tuple(pipeline.CanonicalJoins.__dataclass_fields__),
        ("source_bindings_sha256", "rows_json", "revision_sha256")),
}
_SHA = re.compile(r"[0-9a-f]{64}\Z")
FLAGS = {"publication_ready": False, "stage_promotions": 0, "review_required": True,
         "qualified": False, "activation_allowed": False}


class Refused(Exception):
    """Boundary/identity refusal with a stable reason and no partial evaluation."""


def refuse(condition, reason):
    if not condition:
        raise Refused(reason)


def is_sha(value):
    return isinstance(value, str) and _SHA.fullmatch(value) is not None


def bind_adapter():
    """Return the bound identities, refusing on any signature or version drift."""
    bound = {}
    for name, (getter, expected) in BOUND_SIGNATURES.items():
        try:
            actual = str(inspect.signature(getter()))
        except (AttributeError, TypeError, ValueError):
            actual = None
        refuse(actual == expected, "adapter_signature_mismatch:" + name)
        bound[name] = actual
    for name, (getter, expected) in BOUND_VERSIONS.items():
        try:
            actual = getter()
        except AttributeError:
            actual = None
        refuse(actual == expected, "adapter_version_mismatch:" + name)
        bound[name] = list(actual) if isinstance(actual, tuple) else actual
    return bound


def _no_symlink(path):
    return not any(part.is_symlink() for part in (path, *path.parents))


def file_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _reject_constant(value):
    raise ValueError("nonfinite_json_constant")


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def strict_json(raw):
    return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_pairs,
                      parse_constant=_reject_constant)


def locate(name, value, expected):
    """Return (path or None if not supplied/absent). Refuse unsafe supplied paths."""
    if value is None:
        return None
    refuse(is_sha(expected), "expected_identity_missing:" + name)
    refuse(os.path.isabs(value), "path_not_absolute:" + name)
    path = Path(os.path.normpath(value))
    refuse(str(path) == value, "path_not_normalized:" + name)
    refuse(_no_symlink(path), "path_symlink:" + name)
    if not os.path.lexists(path):
        return None
    mode = path.lstat().st_mode
    refuse(stat.S_ISREG(mode), "path_not_regular_file:" + name)
    return path


def sidecars_present(path):
    return sorted(suffix for suffix in SIDECARS if os.path.lexists(str(path) + suffix))


def check_canonical_copy(path):
    """Admit only a plain rollback-journal SQLite copy without sidecars."""
    refuse(not sidecars_present(path), "canonical_copy_sidecar_present")
    refuse(path.stat().st_size <= MAX_CANONICAL_COPY_BYTES, "canonical_copy_byte_bound")
    with path.open("rb") as stream:
        header = stream.read(100)
    refuse(len(header) == 100 and header[:16] == b"SQLite format 3\x00", "canonical_copy_not_sqlite")
    # Header bytes 18/19: 1 = legacy rollback journal, 2 = WAL. This runner
    # qualifies only rollback-journal copies; WAL/sidecar safety stays owner-run.
    refuse(header[18] == 1 and header[19] == 1, "canonical_copy_wal_unqualified")


def make_out_root(value):
    refuse(isinstance(value, str) and os.path.isabs(value), "out_root_not_absolute")
    path = Path(os.path.normpath(value))
    refuse(str(path) == value, "out_root_not_normalized")
    refuse(path.parent.is_dir() and _no_symlink(path.parent), "out_root_parent_unavailable")
    refuse(not os.path.lexists(path), "out_root_not_exclusive")
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        raise Refused("out_root_not_exclusive") from None
    os.chmod(path, 0o700)
    return path


def write_exclusive(path, value):
    data = (core.canonical(value) + "\n").encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    return hashlib.sha256(data).hexdigest()


def status_counts(structured):
    counts = {"evaluated": 0, "blocked": 0, "inapplicable": 0}
    for entry in structured.values():
        counts[entry["status"]] = counts.get(entry["status"], 0) + 1
    return counts


def blocked_everywhere(reason):
    # Exactly the adapter's own blocked entry shape and typed reason.
    return {name: pipeline._entry("blocked", reason) for name in core.DETECTORS}


def select_normalized(document, run_id):
    """Return the producer's normalization object; the producer's echoed
    source_bindings are deliberately ignored (never an authoritative inventory)."""
    if isinstance(document, dict) and "normalized" in document and "schema" not in document:
        producer_run = document.get("run_id")
        refuse(producer_run is None or producer_run == run_id, "producer_run_id_mismatch")
        return document["normalized"]
    return document


def read_canonical(path, source_bindings, normalized):
    """One read-only snapshot: fresh joins plus originals declarations to copy."""
    joins, joins_error, originals, originals_error = None, None, {}, None
    subjects = set()
    if isinstance(normalized, dict) and isinstance(normalized.get("units"), list):
        for item in normalized["units"]:
            record = item.get("record") if isinstance(item, dict) else None
            if isinstance(record, dict) and core.sha(record.get("original_sha256")):
                subjects.add(record["original_sha256"])
    try:
        with store.ledger(path, readonly=True) as connection:
            connection.execute("BEGIN")
            try:
                try:
                    joins = pipeline.canonical_joins(connection, source_bindings)
                except pipeline.ContractError as error:
                    joins_error = str(error)
                except (ValueError, TypeError, KeyError, sqlite3.Error):
                    joins_error = "canonical_joins_invalid"
                for subject in sorted(subjects):
                    cursor = connection.execute("SELECT * FROM originals WHERE sha256=?", (subject,))
                    row = cursor.fetchone()
                    if row is None:
                        originals_error = originals_error or "canonical_original_missing"
                        continue
                    declaration = dict(zip([column[0] for column in cursor.description], row))
                    try:
                        effective = store.effective_original(connection, subject)
                    except ValueError:
                        effective = None
                    if effective is None or core.digest(dict(effective)) != core.digest(declaration):
                        # Overlays need their owner's trusted context; never copy a guess.
                        originals_error = originals_error or "canonical_original_resolution_unqualified"
                        continue
                    originals[subject] = declaration
            finally:
                connection.rollback()
    except sqlite3.Error:
        raise Refused("canonical_copy_unreadable") from None
    except ValueError:
        raise Refused("canonical_copy_unqualified") from None
    return joins, joins_error, originals, originals_error


def persist(out_root, run_id, structured, normalized, joins, rules, config, originals, originals_error):
    """Persist every computed evaluation through the existing persistence API."""
    detectors = {}
    computed = [name for name in core.DETECTORS if structured[name]["manifest"] is not None]
    for name in core.DETECTORS:
        if name not in computed:
            detectors[name] = {"status": "not_persisted", "reason": "adapter_blocked_before_evaluation"}
    if not computed:
        return {"status": "not_applicable", "reason": "no_computed_evaluation", "detectors": detectors}
    if originals_error:
        for name in computed:
            detectors[name] = {"status": "not_persisted", "reason": originals_error}
        return {"status": "failed", "reason": originals_error, "detectors": detectors}
    units = [item["record"] for item in normalized["units"]]
    join_rows = json.loads(joins.rows_json)
    database = out_root / "detectors.sqlite"
    store.initialize(database)
    failure = None
    with store.ledger(database) as connection:
        columns = None
        for subject in sorted(originals):
            row = originals[subject]
            columns = list(row)
            connection.execute("INSERT INTO originals(" + ",".join(columns) + ") VALUES(" +
                               ",".join("?" for _ in columns) + ")", tuple(row[c] for c in columns))
        started = datetime.now(timezone.utc).isoformat()
        for name in computed:
            connection.execute("INSERT INTO runs VALUES(?,?,?,NULL,?,NULL,?,NULL,'running','{}')",
                               (run_id + "/" + name, RUN_KIND, started, ENGINE_VERSION, core.digest(config)))
        connection.commit()
        for name in computed:
            try:
                receipt = persistence.persist_evaluation(
                    connection, run_id=run_id + "/" + name, detector=name, units=units,
                    joins=join_rows, rules=rules, config=config)
            except persistence.PersistenceError as error:
                reason = str(error).split(":")[0]
                detectors[name] = {"status": "not_persisted", "reason": reason}
                failure = failure or reason
                continue
            manifest = structured[name]["manifest"]
            if (receipt["manifest_sha256"] != manifest["manifest_sha256"] or
                    receipt["hit_keys"] != manifest["hit_keys"]):
                detectors[name] = {"status": "not_persisted", "reason": "persistence_manifest_mismatch"}
                failure = failure or "persistence_manifest_mismatch"
                continue
            receipt = dict(receipt, status="persisted")
            detectors[name] = receipt
            connection.execute("UPDATE runs SET status='completed', ended_at=? WHERE run_id=?",
                               (datetime.now(timezone.utc).isoformat(), run_id + "/" + name))
            connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    status = "failed" if failure else "persisted"
    return {"status": status, "reason": failure, "detectors": detectors}


def parser():
    result = argparse.ArgumentParser(description="Run existing usage detectors (F-042, review required).")
    result.add_argument("--run-id", required=True, help="producer run ID, preserved verbatim")
    result.add_argument("--subject-sha256", required=True)
    result.add_argument("--out-root", required=True, help="absolute, must not exist")
    for name in INPUTS:
        flag = "--" + name.replace("_", "-")
        result.add_argument(flag, default=None)
        result.add_argument(flag + "-sha256", default=None, dest=name + "_sha256")
    return result


def base_result(args):
    return {"schema": SCHEMA, "run_id": args.run_id, "subject_sha256": args.subject_sha256,
            "outcome": None, "reason": None, "context_missing": [],
            "inputs": {name: None for name in INPUTS},
            "adapter": {"called": False, "version": pipeline.VERSION, "result": None},
            "joins_error": None, "structured": None, "status_counts": None, "clean": False,
            "persistence": {"status": "not_attempted", "reason": None, "detectors": {}},
            "canonical_copy": None, **FLAGS}


def execute(args, result, state):
    """Fill result; return exit code. Raises Refused for boundary failures."""
    result["adapter"]["bound"] = bind_adapter()
    refuse(isinstance(args.run_id, str) and 0 < len(args.run_id.strip()) <= 200 and
           args.run_id == args.run_id.strip() and "/" not in args.run_id, "run_id_invalid")
    refuse(is_sha(args.subject_sha256), "subject_hash_invalid")
    located = {}
    for name in INPUTS:
        located[name] = locate(name, getattr(args, name), getattr(args, name + "_sha256"))
    canonical_path = located["canonical_copy"]
    if canonical_path is not None:
        check_canonical_copy(canonical_path)
        before = file_sha256(canonical_path)
        result["canonical_copy"] = {"sha256_before": before, "sha256_after": None, "unchanged": None}
        refuse(before == args.canonical_copy_sha256, "input_identity_mismatch:canonical_copy")
        result["inputs"]["canonical_copy"] = {"sha256": before}
    payloads = {}
    for name in JSON_INPUTS:
        path = located[name]
        if path is None:
            continue
        refuse(path.stat().st_size <= core.MAX_INPUT_BYTES, "input_byte_bound:" + name)
        raw = path.read_bytes()
        refuse(len(raw) <= core.MAX_INPUT_BYTES, "input_byte_bound:" + name)
        actual = hashlib.sha256(raw).hexdigest()
        refuse(actual == getattr(args, name + "_sha256"), "input_identity_mismatch:" + name)
        try:
            payloads[name] = strict_json(raw)
        except (ValueError, UnicodeError, RecursionError):
            raise Refused("input_json_invalid:" + name) from None
        result["inputs"][name] = {"sha256": actual}
    out_root = make_out_root(args.out_root)
    state["out_root"] = out_root
    missing = [name for name in INPUTS if located[name] is None]
    if missing:
        # Required context is unresolved: never call the adapter with a
        # fabricated/derived inventory, joins snapshot or rules.
        result.update(outcome="context_missing", reason="required_context_unresolved",
                      context_missing=missing, structured=blocked_everywhere("required_context_unresolved"))
        result["status_counts"] = status_counts(result["structured"])
        return 3
    normalized = select_normalized(payloads["units"], args.run_id)
    inventory, rules, config = payloads["inventory"], payloads["rules"], payloads["config"]
    joins, joins_error, originals, originals_error = read_canonical(canonical_path, inventory, normalized)
    result["joins_error"] = joins_error
    output = pipeline.evaluate_structured(subject_sha256=args.subject_sha256, normalized=normalized,
                                          source_bindings=inventory, joins=joins, rules=rules,
                                          config=config)
    result["adapter"].update(called=True, result=output)
    structured = output["structured"]
    result.update(outcome="adapter_completed", structured=structured, status_counts=status_counts(structured))
    try:
        result["persistence"] = persist(out_root, args.run_id, structured, normalized, joins, rules, config,
                                        originals, originals_error)
    except (sqlite3.Error, ValueError, OSError):
        result["persistence"] = {"status": "failed", "reason": "persistence_integrity_failure",
                                 "detectors": {}}
    if result["persistence"]["status"] == "failed":
        result.update(outcome="persistence_failed", reason=result["persistence"]["reason"])
        return 4
    clean = result["status_counts"]["blocked"] == 0
    result["clean"] = clean
    if not clean:
        result["reason"] = "detector_context_blocked"
    return 0 if clean else 3


def main(argv=None, stdout=None):
    stdout = stdout or sys.stdout
    args = parser().parse_args(argv)
    result = base_result(args)
    state, code = {"out_root": None}, 2
    try:
        code = execute(args, result, state)
    except Refused as error:
        result.update(outcome="refused", reason=str(error), structured=None, clean=False)
        code = 2
    finally:
        copy = result.get("canonical_copy")
        if copy is not None:
            path = Path(args.canonical_copy)
            after = file_sha256(path) if path.is_file() else None
            unchanged = after == copy["sha256_before"] and not sidecars_present(path)
            copy.update(sha256_after=after, unchanged=unchanged)
            if not unchanged:
                result.update(outcome="refused", reason="canonical_copy_changed", clean=False)
                code = 2
    result["exit_code"] = code
    result["result_sha256"] = core.digest(result)
    out_root = state["out_root"]
    if out_root is not None:
        result_file_sha256 = write_exclusive(out_root / "result.json", result)
        manifest = {"schema": MANIFEST_SCHEMA, "run_id": args.run_id, "subject_sha256": args.subject_sha256,
                    "inputs": result["inputs"], "adapter_version": pipeline.VERSION,
                    "normalized_schema": pipeline.NORMALIZED_SCHEMA, "detector_version": core.VERSION,
                    "persistence_version": persistence.ADAPTER_VERSION, "detectors": list(core.DETECTORS),
                    "outcome": result["outcome"], "exit_code": code,
                    "result_sha256": result["result_sha256"], "result_file_sha256": result_file_sha256,
                    **FLAGS}
        write_exclusive(out_root / "manifest.json", manifest)
    stdout.write(core.canonical(result) + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
