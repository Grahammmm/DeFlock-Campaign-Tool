"""Read-only current primary/support extraction resolution; no semantic authority."""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
import sqlite3

from .. import extraction_ledger as extraction, extraction_validation
from ..ledger_catalog import scope_excluded
from ..ledger import store
from . import pipeline as structured_adapter
from .pipeline import canonical_joins, CanonicalJoins

VERSION = "structured-source-resolver-v1"
MAX_ORIGINALS = 16
MAX_UNITS = 10000
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024


class ResolutionError(ValueError):
    pass


def require(ok, reason):
    if not ok:
        raise ResolutionError(reason)


def secure_open(path):
    """Walk directory descriptors without symlinks; validate before buffered IO."""
    parts = Path(os.path.abspath(path)).parts
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=directory)
            os.close(directory)
            directory = child
        source = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=directory)
    finally:
        os.close(directory)
    try:
        require(stat.S_ISREG(os.fstat(source).st_mode), "private_artifact_required")
        return os.fdopen(source, "rb")
    except BaseException:
        os.close(source)
        raise


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class SourceSnapshot:
    subject_sha256: str
    bindings_json: str
    units_json: str
    originals_json: str
    joins: CanonicalJoins
    revision_sha256: str
    # Exhaustive over declared originals, not proof that every needed source exists.
    support_scope: str = "explicit_originals_only"
    requirements: tuple = ("trusted_normalizer_unavailable",
                           "verified_date_provider_unavailable",
                           "verified_applicability_provider_unavailable",
                           "support_dependency_closure_unverified",
                           "runtime_context_and_receipt_wiring_unavailable")

    @property
    def bindings(self):
        return json.loads(self.bindings_json)

    @property
    def units(self):
        return json.loads(self.units_json)

    @property
    def originals(self):
        return json.loads(self.originals_json)


def resolve_sources(*, database, original_root, derived_root, subject_sha256,
                    support_sha256=()):
    """Trusted local caller supplies root configuration and explicit original IDs.

    All canonical reads and fresh joins share one SQLite read transaction.
    Every unit in each declared current accepted extraction is read, with fixed
    bounds and no text-selection cap. Artifacts are independently hash checked
    at read time, not locked against later filesystem changes. Re-resolve before
    use; never treat this snapshot as semantic, date or publication acceptance.
    """
    try:
        extraction.checked_sha(subject_sha256)
        require(type(support_sha256) in (list, tuple), "support_inventory_required")
        require(len(support_sha256) < MAX_ORIGINALS, "original_count_bound")
        for identity in support_sha256:
            extraction.checked_sha(identity)
        identities = [subject_sha256, *support_sha256]
        require(len(identities) == len(set(identities)), "duplicate_original_identity")
        original_root = extraction.private_dir(original_root)
        derived_root = extraction.private_dir(derived_root)
        used = 0

        def read(path, root):
            nonlocal used
            path = extraction.checked_path(path)
            require(path.is_relative_to(root), "artifact_outside_configured_root")
            with secure_open(path) as stream:
                before = os.fstat(stream.fileno())
                require(stat.S_ISREG(before.st_mode) and before.st_uid == os.geteuid()
                        and not before.st_mode & 0o077, "private_artifact_required")
                require(before.st_size <= MAX_FILE_BYTES, "artifact_byte_bound")
                limit = min(MAX_FILE_BYTES, MAX_TOTAL_BYTES - used)
                raw = stream.read(limit + 1)
                require(len(raw) <= limit, "snapshot_byte_bound")
                after = os.fstat(stream.fileno())
                require((before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) ==
                        (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                        and len(raw) == after.st_size, "artifact_changed_during_read")
            used += len(raw)
            return raw

        bindings, units, originals = [], [], []
        with store.ledger(database, readonly=True) as con:
            con.execute("BEGIN")

            def artifact(identity):
                nonlocal used
                extraction.checked_sha(identity)
                row = con.execute("SELECT payload FROM stage_artifacts WHERE sha256=? AND length(payload)<=?", (identity, MAX_FILE_BYTES)).fetchone()
                require(row is not None, "accepted_artifact_missing")
                require(len(row[0]) <= MAX_FILE_BYTES, "accepted_artifact_byte_bound")
                raw = bytes(row[0])
                used += len(raw)
                require(used <= MAX_TOTAL_BYTES, "snapshot_byte_bound")
                require(sha(raw) == identity, "accepted_artifact_hash_mismatch")
                return extraction.decode(raw)

            for subject in identities:
                original = store.effective_original(con, subject)
                require(original is not None and not scope_excluded(original),
                        "original_missing_or_excluded")
                raw = read(original["storage_path"], original_root)
                require(sha(raw) == subject and len(raw) == original["bytes"], "original_bytes_changed")
                states = {r["stage"]: dict(r) for r in con.execute(
                    "SELECT * FROM stage_state WHERE original_sha256=?", (subject,))}
                for name in ("preserve", "extract"):
                    state = states.get(name, {})
                    require(state.get("status") == "done" and state.get("receipt_sha256"),
                            "current_preserve_extract_acceptance_required")
                    receipt = artifact(state["receipt_sha256"])
                    require(receipt.get("subject_sha256") == subject and receipt.get("stage") == name
                            and receipt.get("verdict") == "pass", "accepted_receipt_binding")
                receipt = artifact(states["extract"]["receipt_sha256"])
                row = con.execute("SELECT c.content_sha256 FROM stage_content c "
                    "WHERE c.subject_sha256=? AND c.stage='extract' ORDER BY c.revision DESC LIMIT 1",
                    (subject,)).fetchone()
                require(row is not None and row[0] == receipt.get("content_sha256"),
                        "accepted_content_changed")
                content = artifact(row[0])
                require(content.get("schema") == "canonical-extraction-content-v1"
                        and content.get("original_sha256") == subject
                        and content.get("installed_code") == extraction_validation.code_identity(),
                        "installed_extraction_binding")
                require(receipt.get("input_hashes", {}).get("preserve") ==
                        states["preserve"]["receipt_sha256"], "accepted_preservation_changed")
                imports = con.execute("SELECT i.* FROM extraction_adapter_current c "
                    "JOIN extraction_adapter_imports i ON i.id=c.import_id "
                    "WHERE c.original_sha256=? AND length(i.manifest_json)<=?", (subject, MAX_FILE_BYTES)).fetchall()
                require(len(imports) == 1, "current_extraction_missing_or_ambiguous")
                current = imports[0]
                require(current["id"] == content.get("import_id")
                        and current["manifest_sha256"] == content.get("manifest_sha256")
                        and len(current["manifest_json"].encode()) <= MAX_FILE_BYTES
                        and sha(current["manifest_json"].encode()) == current["manifest_sha256"],
                        "current_extraction_changed")
                used += len(current["manifest_json"].encode())
                require(used <= MAX_TOTAL_BYTES, "snapshot_byte_bound")
                manifest = extraction.decode(current["manifest_json"])
                inventory = manifest.get("unit_ids")
                require(type(inventory) is list and inventory
                        and len(inventory) <= MAX_UNITS - len(bindings)
                        and len(inventory) == len(set(inventory)) == content.get("unit_count"),
                        "current_inventory_invalid_or_over_bound")
                require(manifest.get("original_sha256") == subject
                        and manifest.get("original_bytes") == len(raw)
                        and manifest.get("receipt_sha256") == content.get("source_receipt_sha256"),
                        "extraction_manifest_binding")
                for ordinal, identity in enumerate(inventory, 1):
                    unit = con.execute("SELECT * FROM units WHERE id=? AND original_sha256=?",
                                       (identity, subject)).fetchone()
                    require(unit is not None, "current_unit_missing")
                    provenance = extraction.decode(unit["provenance_json"])
                    payload_raw = read(unit["derived_path"], derived_root)
                    payload = extraction.decode(payload_raw)
                    locator = extraction.decode(unit["locator"])
                    require(type(locator) is dict and locator and type(payload) is dict
                            and type(payload.get("text")) is str, "current_unit_shape")
                    require(provenance.get("extraction_import_id") == current["id"]
                            and provenance.get("ordinal") == ordinal
                            and provenance.get("receipt_sha256") == manifest["receipt_sha256"]
                            and provenance.get("payload_sha256") == sha(payload_raw)
                            and unit["text_sha256"] == sha(payload["text"].encode())
                            and payload.get("locator") == locator
                            and payload.get("kind") == unit["unit_type"]
                            and identity == sha(extraction.canonical(
                                [current["id"], ordinal, sha(payload_raw)]).encode()),
                            "current_unit_binding_changed")
                    bindings.append({"unit_id": identity, "original_sha256": subject,
                                     "artifact_sha256": sha(payload_raw), "locator": locator})
                    units.append({"unit_id": identity, "original_sha256": subject, "payload": payload})
                originals.append({"original_sha256": subject, "role": "primary" if subject == subject_sha256 else "support",
                    "bytes": original["bytes"], "resolution_sha256": original.get("resolution_sha256"),
                    "preserve_receipt_sha256": states["preserve"]["receipt_sha256"],
                    "extract_receipt_sha256": states["extract"]["receipt_sha256"],
                    "manifest_sha256": current["manifest_sha256"], "import_id": current["id"],
                    "unit_count": len(inventory)})
            joins = canonical_joins(con, bindings)
        payload = {"schema": VERSION, "subject_sha256": subject_sha256, "originals": originals,
                   "bindings": bindings, "units": units, "join_revision": joins.revision_sha256,
                   "join_rows": joins.rows_json, "resolver_code_sha256": store.file_hash(__file__),
                   "join_adapter_code_sha256": store.file_hash(
                       structured_adapter.__file__)}
        raw = extraction.canonical(payload)
        require(len(raw.encode()) <= MAX_TOTAL_BYTES, "snapshot_serialized_byte_bound")
        return SourceSnapshot(subject_sha256, extraction.canonical(bindings), extraction.canonical(units),
                              extraction.canonical(originals), joins, sha(raw.encode()))
    except ResolutionError:
        raise
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, OverflowError, RecursionError):
        raise ResolutionError("source_resolution_unavailable_or_invalid") from None
