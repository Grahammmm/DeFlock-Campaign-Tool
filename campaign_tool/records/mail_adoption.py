"""Adopt frozen mail-history export receipts into the canonical records ledger.

Dry-run by default. ``--apply`` enrolls each pending receipt through the
existing CanonicalMailBackend (mail_delta byte/MIME verification, canonical
mail/occurrence identity checks) and tags the run ``source=mail-history``.

The adoption gate is configuration, never file deletion: when the records root
holds ``.adoption-disabled-v1.json`` the apply step is refused unless an
owner-only adoption config explicitly enables adoption *and* pins the exact
sha256 of that marker. The marker is only read, never moved or removed.

Every receipt is classified against the ledger, read-only:

* ``new`` -- no ``mail_messages`` row for its (account, folder, uidvalidity,
  uid) identity yet;
* ``complete`` -- the identity is enrolled, its full canonical binding matches
  this receipt exactly (mail_messages row, occurrence ids, CAS hashes, MIME
  locators, parents, multiplicity, evidence incl. the content-addressed
  verification receipt bytes, original declarations; the read-only equivalent
  of CanonicalMailBackend's re-enrollment and replay checks), every bound
  canonical original in the CAS still stream-hashes to its digest at its
  declared length, and every declared original has a ``done`` preserve stage;
* ``incomplete`` -- bindings match but preservation is unfinished (for example
  enrollment committed and the process then failed or crashed). Apply resumes
  these through the same idempotent backend call;
* an ``identity_*`` conflict -- the identity is enrolled but this receipt
  declares different bytes or evidence. It is refused, never written, and the
  earlier evidence is left untouched.

A ``new`` receipt that cannot be adopted under the current canonical contract
is a *coded gap*. It is classified read-only, in the backend's own order:
first the legacy MIME check the backend runs on the export EML (for example
``unsupported_rfc822_part``), then the original-binding precondition (for
example ``original_scope_unresolved`` when the EML is already declared as an
unresolved legacy original; the backend would raise
``canonical_original_binding_conflict``). Coded gaps are never passed to the
backend, so they cause no CAS, catalog or verification-receipt side effects.
They are counted by stable code in every dry-run, apply and run summary and
are never dropped. Codes in ``ACCEPTED_GAP_CODES`` are the classes G accepted
as reported gaps (decision 2026-10-19, option c); any other coded gap blocks
(CLI exit 3).

With a pinned, owner-approved legacy-resolution policy (config schema
``mail-history-adoption-v2``, docs/RECORDS-LEGACY-MAIL-RESOLUTION.md section 8),
a ``new`` receipt whose unresolved legacy originals the policy resolves is
*resolvable* instead of a coded gap. One pure plan (``intake.mail_resolution_plan``)
decides it for dry run and apply alike; the backend writes the resolution rows.

Only eligible new, resolvable and incomplete receipts are passed to the backend; a run
with nothing pending writes nothing. Reports carry counts and hashes only, no paths,
account names, headers or exception narratives. The export directory is only
read; this command never connects to a mailbox or runs an exporter.
"""
import argparse
from collections import Counter
import contextlib
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import uuid

from .intake import folder as intake_folder, mail_delta

SOURCE = "mail-history"
CONFIG_SCHEMA = "mail-history-adoption-v1"
CONFIG_SCHEMA_V2 = "mail-history-adoption-v2"
POLICY_KEY = "legacy_resolution_policy_sha256"
DISABLE_MARKER = ".adoption-disabled-v1.json"
RUN_PREFIX = "mail-history-"
MAX_RECEIPTS = 10000
MAX_SMALL_FILE = 64 * 1024
# Coded-gap classes G accepted as reported, non-blocking gaps (2026-10-19,
# option c). Resolving them needs a separately reviewed contract (T4B).
ACCEPTED_GAP_CODES = frozenset({"original_scope_unresolved", "unsupported_rfc822_part"})


class AdoptionError(RuntimeError):
    """Safe, non-narrative refusal code."""


def _read_small(path):
    with intake_folder.secure_open(path) as stream:
        raw = stream.read(MAX_SMALL_FILE + 1)
    if len(raw) > MAX_SMALL_FILE:
        raise AdoptionError("adoption_file_bound")
    return raw


def load_config(path):
    """Owner-only adoption config. Returns (config, sha256) or raises AdoptionError."""
    from .runner.core import private_path
    try:
        raw = _read_small(private_path(path))
        config = json.loads(raw)
    except (OSError, ValueError, UnicodeError):
        raise AdoptionError("adoption_config_invalid") from None
    if (not isinstance(config, dict) or config.get("schema") not in (CONFIG_SCHEMA, CONFIG_SCHEMA_V2)
            or config.get("source") != SOURCE
            or config.get("adoption") not in ("enabled", "disabled")):
        raise AdoptionError("adoption_config_invalid")
    keys = {"schema", "source", "adoption", "disable_marker_sha256"}
    if config["schema"] == CONFIG_SCHEMA_V2:
        keys.add(POLICY_KEY)  # v1 stays valid and means "no resolution"
    for key in ("disable_marker_sha256", POLICY_KEY):
        pinned = config.get(key)
        if pinned is not None and not (isinstance(pinned, str) and len(pinned) == 64
                                       and all(c in "0123456789abcdef" for c in pinned)):
            raise AdoptionError("adoption_config_invalid")
    if set(config) - keys:
        raise AdoptionError("adoption_config_invalid")
    return config, hashlib.sha256(raw).hexdigest()


def gate(root, config):
    """Return the gate decision; never modifies the marker."""
    marker = Path(root) / DISABLE_MARKER
    try:
        marker_sha = hashlib.sha256(_read_small(marker)).hexdigest()
    except FileNotFoundError:
        marker_sha = None
    except OSError:
        return {"open": False, "state": "disable_marker_unreadable", "marker_sha256": None}
    result = {"marker_present": marker_sha is not None, "marker_sha256": marker_sha}
    if config is None:
        state = "disabled_no_config"
    elif config["adoption"] != "enabled":
        state = "disabled_by_config"
    elif config.get("disable_marker_sha256") != marker_sha:
        # Covers: marker present but not pinned, pinned to other bytes, or a pinned
        # marker that has since disappeared (state drift fails closed).
        state = "disabled_by_marker" if marker_sha is not None else "marker_binding_mismatch"
    else:
        state = "enabled_marker_superseded_by_config" if marker_sha else "enabled"
    result.update(open=state.startswith("enabled"), state=state)
    return result


def discover(mail_root, output):
    """Load every receipt under ``<mail_root>/receipts``; invalid ones become coded gaps."""
    receipts_dir = mail_root / "receipts"
    try:
        info = receipts_dir.lstat()
    except FileNotFoundError:
        raise AdoptionError("receipts_directory_missing") from None
    if not stat.S_ISDIR(info.st_mode):
        raise AdoptionError("receipts_directory_unsafe")
    names = []
    with os.scandir(receipts_dir) as entries:
        for entry in entries:
            if entry.name.endswith(".json") and not entry.name.startswith("."):
                names.append(entry.name)
                if len(names) > MAX_RECEIPTS:
                    raise AdoptionError("receipt_count_bound")
    found, gaps, seen = [], Counter(), set()
    for name in sorted(names):
        try:
            path = mail_delta.source_path(mail_root, str(receipts_dir / name), output)
            if not stat.S_ISREG(path.lstat().st_mode):
                raise mail_delta.Rejected("receipt_not_regular_file")
            declaration, identity, items = mail_delta.load_receipt(path)
            native = (declaration.get("native_items")
                      if declaration.get("schema") == "mail-wire-receipt-v2" else None)
            native_shas = frozenset(entry.get("source_original_sha256") for entry in native or ()
                                    if isinstance(entry, dict))
            with intake_folder.secure_open(path) as stream:
                receipt_sha = hashlib.sha256(stream.read(mail_delta.MAX_RECEIPT + 1)).hexdigest()
        except mail_delta.Rejected as error:
            gaps[str(error)] += 1
            continue
        except (OSError, ValueError):
            gaps["receipt_unreadable"] += 1
            continue
        if identity in seen:
            gaps["duplicate_receipt_identity"] += 1
            continue
        seen.add(identity)
        found.append({"path": path, "identity": identity, "attachments": len(items) - 1,
                      "receipt_sha256": receipt_sha, "native_shas": native_shas,
                      "declaration": declaration, "items": items,
                      "originals": tuple((item["sha"], item["size"]) for item in items)})
    return found, gaps


def binding_conflict(con, store, blobs, sha, size):
    """Mirror CanonicalMailBackend's original-binding precondition, read-only.

    Returns a safe code when an existing ledger declaration would make
    ``preserve`` raise ``canonical_original_binding_conflict``; else None.
    """
    original = store.effective_original(con, sha)
    if not original:
        return None
    if original["bytes"] != size:
        return "original_bytes_conflict"
    if original["scope"] != "in_scope":
        return "original_scope_" + ("unresolved" if original["scope"] == "unresolved" else "not_in_scope")
    if original["storage_path"] != str(blobs / sha):
        return "original_storage_elsewhere"
    return None


def mime_precheck(item, mail_root, output):
    """Read-only equivalent of the backend's legacy MIME check; a safe code or None.

    Only legacy receipts (no ``schema``) are checked: for them
    LegacyIntakeBackend stages the export EML and parses it with
    ``mail_delta.mime_candidates`` before any ledger binding, so a rejection
    there (e.g. ``unsupported_rfc822_part``) is what apply would report. The
    EML is opened with ``secure_open`` (no symlinks), bounded by its declared
    size and must hash to its declared digest; nothing is staged or written.
    Schema receipts use other adapters and are left to the backend.
    """
    if "schema" in item["declaration"]:
        return None
    eml = item["items"][0]
    try:
        path = mail_delta.source_path(mail_root, eml["path"], output)
        with intake_folder.secure_open(path) as stream:
            raw = stream.read(eml["size"] + 1)
    except mail_delta.Rejected as error:
        return str(error)
    except (OSError, ValueError):
        return "source_unreadable"
    if len(raw) != eml["size"]:
        return "source_size_mismatch"
    if hashlib.sha256(raw).hexdigest() != eml["sha"]:
        return "source_hash_mismatch"
    try:
        # BytesIO parsing (legacy_scalar) is what parsing the staged file does.
        mail_delta.mime_candidates(None, raw=raw, legacy_scalar=True)
    except mail_delta.Rejected as error:
        return str(error)
    except Exception:  # unexpected parser failure: a blocking code, never a crash
        return "mime_precheck_error"
    return None


def _sidecar_keys(path):
    keys = []
    for suffix in ("", "-wal", "-shm", "-journal"):
        try:
            value = Path(str(path) + suffix).lstat()
            keys.append((value.st_ino, value.st_size, value.st_mtime_ns, value.st_mode))
        except FileNotFoundError:
            keys.append(None)
    return keys


def _wal_pending(path):
    try:
        return Path(str(path) + "-wal").lstat().st_size > 0
    except FileNotFoundError:
        return False


@contextlib.contextmanager
def _snapshot_db(db_path, prefix):
    """Read-only SQLite access that never creates, changes or removes a file.

    ``mode=ro`` alone (as in ``store.ledger(readonly=True)``) creates ``-shm``
    and rebuilds it from a crashed writer's WAL, so it is never used here. A
    cleanly checkpointed database (no or empty WAL, no rollback journal) is
    read with ``immutable=1``, which touches no sidecar. A non-empty WAL means
    a live writer or an unrecovered crash; the read then fails closed with
    ``<prefix>_wal_not_checkpointed`` before opening anything. Recovery
    belongs to a writer holding the records-root lock (``--apply``), not to a
    dry run. The file and all sidecar identities must be unchanged afterwards,
    otherwise the read fails closed with ``<prefix>_changed_during_read``.
    """
    from .ledger import store
    path = store.checked_path(db_path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
        raise AdoptionError(prefix + "_not_owner_only")
    before = _sidecar_keys(path)
    if (before[1] is not None and before[1][1] > 0) or before[3] is not None:
        raise AdoptionError(prefix + "_wal_not_checkpointed")
    con = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA query_only=ON")
        yield con
    finally:
        con.close()
    if _sidecar_keys(path) != before:
        raise AdoptionError(prefix + "_changed_during_read")


@contextlib.contextmanager
def _snapshot_ledger(ledger_path):
    """Sidecar-free read of the canonical ledger, schema-checked."""
    from .ledger import store
    from .ledger.migrations import v001
    store.private_parent(store.checked_path(ledger_path))
    with _snapshot_db(ledger_path, "ledger") as con:
        row = con.execute("SELECT checksum FROM schema_migrations WHERE version=?",
                          (v001.VERSION,)).fetchone()
        if (con.execute("PRAGMA user_version").fetchone()[0] != v001.VERSION
                or row is None or row[0] != store.SCHEMA_HASH):
            raise AdoptionError("ledger_schema_mismatch")
        yield con


def _settle_ledger(ledger_path):
    """Writer-side WAL recovery, only under the records-root lock (apply).

    Opening and closing the ledger read-write makes SQLite recover committed
    WAL frames and checkpoint them; frames of an uncommitted transaction are
    discarded by SQLite itself. If the WAL is still non-empty afterwards some
    other connection is live outside the lock, and apply fails closed.
    """
    from .ledger import store
    if not _wal_pending(ledger_path):
        return False
    with store.ledger(ledger_path):
        pass
    if _wal_pending(ledger_path):
        raise AdoptionError("ledger_active_writer")
    return True


def _settle_intake(intake_path):
    """The same writer-side WAL recovery for the intake catalog (apply only)."""
    if not _wal_pending(intake_path) or not os.path.isfile(intake_path):
        return False
    con = sqlite3.connect(intake_path, timeout=5)
    try:
        con.execute("SELECT count(*) FROM sqlite_master").fetchone()
    finally:
        con.close()
    if _wal_pending(intake_path):
        raise AdoptionError("intake_active_writer")
    return True


def message_id(identity):
    """Same canonical mail id the backend derives for an identity."""
    account, mailbox, validity, uid = identity
    return hashlib.sha256(json.dumps(["mail", account, mailbox, int(validity), int(uid)],
                                     separators=(",", ":")).encode()).hexdigest()


_OCCURRENCE_COLUMNS = "id,original_sha256,kind,source_ref,parent_occurrence_id,acquisition_method,evidence"
_EVIDENCE_KEYS = frozenset({"receipt_sha256", "cas_sha256", "bytes", "export_receipt_path",
                            "verification_receipt_path"})


def _occurrence_groups(con, wanted):
    """Every mail/attachment occurrence whose source_ref names a wanted identity.

    Not filtered by acquisition method, so a drifted method is seen as a
    mismatch instead of silently dropped.
    """
    groups = {}
    for row in con.execute("SELECT " + _OCCURRENCE_COLUMNS + " FROM occurrences "
                           "WHERE kind IN ('mail','attachment')"):
        try:
            source = json.loads(row["source_ref"])
            key = (source["account"], source["folder"], str(source["uidvalidity"]), str(source["uid"]))
        except (ValueError, KeyError, TypeError):
            continue
        if key in wanted:
            groups.setdefault(key, []).append(row)
    return groups


def expected_occurrences(item, intake):
    """The exact occurrence rows CanonicalMailBackend.preserve derives, read-only.

    Mirrors LegacyIntakeBackend.preserve (MIME locators from the intake
    catalog, parent parts, native items) and CanonicalMailBackend.preserve
    (occurrence ids, parent occurrence ids, source_ref) without importing,
    copying or writing anything. ``intake`` is a read-only snapshot of
    ``intake.sqlite``. Returns a list of
    ``(id, sha, kind, parent_id, source_ref, native_item)`` or a safe code.
    """
    from .runner.core import hid, js
    receipt, items, identity = item["declaration"], item["items"], item["identity"]
    account, mailbox, validity, uid = identity
    mid = message_id(identity)
    extended = receipt.get("schema") == "mail-wire-receipt-v2"
    native_items = tuple({key: value for key, value in entry.items() if key not in {"manifest_path", "streams"}}
                         for entry in receipt.get("native_items", [])) if extended else ()
    attached, parents = [], []
    for entry in items[1:]:
        oid = intake_folder.hid(intake_folder.js(["mail-receipt-v1", *identity, entry["part"]]))
        rows = intake.execute("SELECT sha,locator FROM occurrences WHERE oid=?", (oid,)).fetchall()
        if len(rows) != 1 or rows[0][0] != entry["sha"]:
            return "identity_intake_occurrence_mismatch"
        try:
            loc = entry["part"] if receipt.get("schema") else json.loads(rows[0][1])["mime"]
            if receipt.get("schema"):
                parent = entry["source"]["parent_part"]
                parents.append((loc, None if parent == "0" else parent))
        except (ValueError, KeyError, TypeError):
            return "identity_intake_occurrence_mismatch"
        attached.append((loc, entry["sha"]))
    for entry in native_items:
        parents.append((entry["part"], None if entry["parent_part"] == "0" else entry["parent_part"]))
    parents = dict(parents)
    native = {entry["part"]: entry for entry in native_items}

    def parent_id(loc):
        parent = parents.get(loc)
        return mid if parent is None else hid(["native-msg-item" if parent in native else "attachment", mid, parent])

    def source(loc):
        return js({"account": account, "folder": mailbox, "uidvalidity": int(validity), "uid": int(uid),
                   "mime_path": loc})

    records = [(mid, items[0]["sha"], "mail", None, source(None), None)]
    records.extend((hid(["attachment", mid, loc]), sha, "attachment", parent_id(loc), source(loc), None)
                   for loc, sha in attached)
    records.extend((hid(["native-msg-item", mid, loc]), entry["source_original_sha256"], "attachment",
                    parent_id(loc), source(loc), entry) for loc, entry in native.items())
    return records


def _verification_receipt(blobs, receipt_sha):
    """Canonical content-addressed verification receipt, re-hashed read-only.

    The path is derived from the receipt digest, never taken from stored
    evidence; ``secure_open`` refuses a symlink on any component.
    """
    path = blobs.parent / "verification-receipts" / receipt_sha
    try:
        with intake_folder.secure_open(path) as stream:
            raw = stream.read(1024 * 1024 + 1)
    except (OSError, ValueError):
        return path, "identity_verification_receipt_missing"
    if len(raw) > 1024 * 1024 or hashlib.sha256(raw).hexdigest() != receipt_sha:
        return path, "identity_verification_receipt_changed"
    return path, None


_HEX = frozenset("0123456789abcdef")


def _stat_fields(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def verify_original_bytes(blobs, sha, size):
    """Stream-hash one canonical original in the CAS, read-only.

    The read-only equivalent of ``intake_folder.verify_blob`` plus the length
    and stability checks of CanonicalMailBackend's ``_validator``. The path
    is derived from the trusted CAS directory and the digest, never from
    evidence; ``secure_open`` refuses a symlink on any path component. The
    file is opened read-only and never chmod-ed, repaired or rewritten.
    Returns a safe ``identity_original_blob_*`` code or None when the stored
    bytes hash to ``sha`` with exactly ``size`` bytes.
    """
    if not isinstance(sha, str) or len(sha) != 64 or not set(sha) <= _HEX:
        return "identity_original_blob_invalid"
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        return "identity_original_blob_invalid"
    path = blobs / sha
    digest, length = hashlib.sha256(), 0
    try:
        with intake_folder.secure_open(path) as stream:
            before = os.fstat(stream.fileno())
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                length += len(block)
                if length > size:
                    return "identity_original_blob_changed"
                digest.update(block)
            after = os.fstat(stream.fileno())
        current = path.lstat()
    except FileNotFoundError:
        return "identity_original_blob_missing"
    except (OSError, ValueError):  # symlink (ELOOP), not a regular file, unreadable
        return "identity_original_blob_unsafe"
    if _stat_fields(before) != _stat_fields(after) or _stat_fields(after) != _stat_fields(current):
        return "identity_original_blob_unstable"
    if length != size or digest.hexdigest() != sha:
        return "identity_original_blob_changed"
    return None


def identity_conflict(con, store, blobs, item, rows, occurrences, intake=None, projection=None):
    """Validate an enrolled identity against this receipt, read-only.

    Checks the whole canonical binding CanonicalMailBackend requires on
    re-enrollment and on preservation replay (its ``_validator``): the
    mail_messages row, the exact occurrence set (ids, kinds, CAS hashes, MIME
    locators, parent occurrence ids, multiplicity, acquisition method), every
    evidence field including the verification receipt path and the stored
    receipt bytes, the original declarations, and finally the stored bytes of
    every bound canonical original (streamed, hashed and length-checked
    read-only, see ``verify_original_bytes``). Returns a safe
    ``identity_*`` code for the first difference, None when everything
    matches. Never rewrites evidence and never follows a path from evidence.
    """
    mid = message_id(item["identity"])
    if len(rows) != 1 or rows[0]["id"] != mid:
        return "identity_message_id_mismatch"
    account, mailbox, validity, uid = item["identity"]
    if (rows[0]["account"], rows[0]["folder"], str(rows[0]["uidvalidity"]),
            str(rows[0]["uid"])) != (account, mailbox, validity, uid):
        return "identity_message_id_mismatch"
    declared = dict(item["originals"])
    eml_sha = item["originals"][0][0]
    if rows[0]["eml_sha256"] != eml_sha:
        return "identity_eml_changed"
    # Attachment multiset declared by this receipt vs. what the ledger holds.
    receipt_attached = sorted([entry["sha"] for entry in item["items"][1:]] + [
        entry.get("source_original_sha256") for entry in (item["declaration"].get("native_items") or ())
        if item["declaration"].get("schema") == "mail-wire-receipt-v2" and isinstance(entry, dict)])
    if sorted(o["original_sha256"] for o in occurrences if o["kind"] == "attachment") != receipt_attached:
        return "identity_attachment_set_changed"
    for occurrence in occurrences:
        try:
            evidence = json.loads(occurrence["evidence"])
        except (TypeError, ValueError):
            return "identity_evidence_unreadable"
        if not isinstance(evidence, dict) or evidence.get("receipt_sha256") != item["receipt_sha256"]:
            return "identity_receipt_changed"
    if intake is None:
        return "identity_intake_missing"
    expected = expected_occurrences(item, intake)
    if isinstance(expected, str):
        return expected
    expected_ids = [record[0] for record in expected]
    if len(set(expected_ids)) != len(expected_ids):
        return "identity_occurrence_duplicate"
    # Rows holding an expected id even if their source_ref moved elsewhere.
    by_id = {}
    elsewhere = con.execute("SELECT " + _OCCURRENCE_COLUMNS + " FROM occurrences WHERE id IN (%s)"
                            % ",".join("?" * len(expected_ids)), expected_ids).fetchall()
    for row in list(occurrences) + elsewhere:
        by_id.setdefault(row["id"], {})[tuple(row)] = row
    if set(by_id) != set(expected_ids) or len(occurrences) != len(expected_ids):
        return "identity_occurrence_mismatch"
    path, code = _verification_receipt(blobs, item["receipt_sha256"])
    if code:
        return code
    for oid, sha, kind, parent, source, native_item in expected:
        if len(by_id[oid]) != 1:
            return "identity_occurrence_mismatch"
        row = next(iter(by_id[oid].values()))
        if (row["original_sha256"], row["kind"], row["acquisition_method"]) != (sha, kind, "verified_mail_export"):
            return "identity_occurrence_mismatch"
        if row["source_ref"] != source:
            return "identity_locator_changed"
        if row["parent_occurrence_id"] != parent:
            return "identity_parent_changed"
        evidence = json.loads(row["evidence"])
        if not isinstance(evidence, dict) or evidence.get("receipt_sha256") != item["receipt_sha256"]:
            return "identity_receipt_changed"
        original = store.effective_original(con, sha)
        if not original:
            return "identity_original_missing"
        keys = _EVIDENCE_KEYS | ({"native_item"} if native_item is not None else frozenset())
        if (set(evidence) != keys or evidence.get("cas_sha256") != sha
                or evidence.get("bytes") != original["bytes"]
                or (sha in declared and evidence["bytes"] != declared[sha])
                or not isinstance(evidence.get("export_receipt_path"), str)
                or (native_item is not None and evidence.get("native_item") != native_item)):
            return "identity_evidence_mismatch"
        if evidence.get("verification_receipt_path") != str(path):
            return "identity_verification_path_changed"
        code = binding_conflict(con, store, blobs, sha, original["bytes"])
        if code:
            return "identity_" + code
    for sha, size in item["originals"]:
        if not store.effective_original(con, sha):
            return "identity_original_missing"
        code = binding_conflict(con, store, blobs, sha, size)
        if code:
            return "identity_" + code
    # Declarations agree; now the stored bytes themselves. Every canonical
    # original bound to this identity (EML or native original, attachments,
    # native items) is stream-hashed and length-checked in the CAS, read-only,
    # exactly as preservation would verify it before a re-enrollment.
    checked = set()
    for _, sha, _, _, _, _ in expected:
        if sha in checked:
            continue
        checked.add(sha)
        code = verify_original_bytes(blobs, sha, store.effective_original(con, sha)["bytes"])
        if code:
            return code
    if projection is not None:
        try:
            values = projection(item)
        except Exception:  # an unverifiable header projection fails closed
            return "identity_mail_row_unverifiable"
        if tuple(rows[0]) != tuple(values):
            return "identity_mail_row_changed"
    return None


def _preserved(con, item):
    for sha, _ in item["originals"]:
        row = con.execute("SELECT status FROM stage_state WHERE original_sha256=? AND stage='preserve'",
                          (sha,)).fetchone()
        if row is None or row["status"] != "done":
            return False
    return True


def _projection(mail_root, output, ledger_path):
    """Read-only recomputation of the mail_messages row the backend would write."""
    from types import SimpleNamespace
    from .runner.canonical_mail import CanonicalMailBackend
    from .runner.contracts import Folder
    backend = CanonicalMailBackend(mail_root, output, ledger_path)

    def project(item):
        account, mailbox, validity, uid = item["identity"]
        value = SimpleNamespace(message_id=message_id(item["identity"]), eml_sha256=item["originals"][0][0],
                                original_format=item["declaration"]["original_eml"].get("format", "eml"))
        return backend._eml_projection(value, item["path"], account, Folder(mailbox, int(validity)), int(uid))
    return project


def receipt_occurrences(item, mail_root, output):
    """{sha: sorted occurrence ids} CanonicalMailBackend would write for a receipt, read-only.

    Nothing is staged or imported: legacy receipts get their MIME locators from the same
    candidate matching as ``mail_delta.bind_parts``; exact-wire receipts carry the part.
    Returns None when the ids cannot be derived (the plan then blocks the binding).
    """
    from .runner.core import hid
    mid = message_id(item["identity"])
    declaration, items = item["declaration"], item["items"]
    found = {items[0]["sha"]: [mid]}
    try:
        if declaration.get("schema"):
            for entry in items[1:]:
                found.setdefault(entry["sha"], []).append(hid(["attachment", mid, entry["part"]]))
            if declaration.get("schema") == "mail-wire-receipt-v2":
                for native in declaration.get("native_items") or ():
                    found.setdefault(native["source_original_sha256"], []).append(
                        hid(["native-msg-item", mid, native["part"]]))
        else:
            eml = items[0]
            with intake_folder.secure_open(mail_delta.source_path(mail_root, eml["path"], output)) as stream:
                raw = stream.read(eml["size"] + 1)
            candidates = mail_delta.mime_candidates(None, raw=raw, legacy_scalar=True)
            used = set()
            for entry in items[1:]:
                part = entry["part"]
                aliases = {part} | ({"1." + part} if part.isdigit() and part[0] != "0" else set())
                matches = [c for c in candidates if c["attachable"] and
                           (c["walk_index"] == int(part) if entry["part_is_walk_index"] else c["locator"] in aliases)
                           and c["sha"] == entry["sha"] and c["size"] == entry["size"] and c["locator"] not in used]
                if len(matches) != 1:
                    return None
                used.add(matches[0]["locator"])
                found.setdefault(entry["sha"], []).append(hid(["attachment", mid, matches[0]["locator"]]))
    except (mail_delta.Rejected, OSError, ValueError, KeyError, TypeError, RecursionError):
        return None
    return {sha: sorted(ids) for sha, ids in found.items()}


def _ledger_facts(con, receipts, policy, census=False):
    """Per-original ledger facts for the resolution plan and census (read-only)."""
    from .ledger import original_resolution
    entries = {sha: policy.entry(sha) for sha in policy.originals()} if policy is not None else {}
    sizes = {}
    for item in receipts:
        for sha, size in item["originals"]:
            sizes.setdefault(sha, size)
    shas = set(sizes) | set(entries) | (set(policy.held()) if policy is not None else set())
    facts = {}
    for sha in sorted(shas):
        row = con.execute("SELECT scope,legacy_format FROM originals WHERE sha256=?", (sha,)).fetchone()
        resolution = original_resolution.mail_resolution_row(con, sha)
        if resolution is not None:
            original_resolution.effective_original(con, sha)  # fails closed on a corrupt row
        check = None
        if sha in entries and resolution is None:
            if row is None:
                check = "resolution_requires_unaccepted_original"
            elif row["scope"] == "unresolved":
                check = ("resolution_policy_bytes_mismatch" if sizes.get(sha) != entries[sha]["bytes"]
                         else original_resolution.mail_export_precondition(con, sha, entries[sha], entries[sha]["bytes"]))
        legacy = None
        if census:
            legacy = original_resolution.legacy_binding_sha256(con, sha)
            if sha not in entries and resolution is None and row is not None and row["scope"] == "unresolved":
                check = original_resolution.mail_export_precondition(con, sha, None, sizes.get(sha))
        facts[sha] = {"scope": row["scope"] if row else None, "format": row["legacy_format"] if row else None,
                      "resolution": resolution, "check": check, "size": sizes.get(sha), "legacy_binding": legacy}
    return facts


def classify_receipts(ledger_path, receipts, blobs, mail_root=None, policy=None, census=False):
    """Read-only classification. Returns ({identity: (state, coded_gap)}, unfinalized runs).

    state is ``new``, ``complete``, ``incomplete`` or an ``identity_*`` code;
    ``coded_gap`` is the safe code for a ``new`` receipt the backend would
    refuse (legacy MIME check first when ``mail_root`` is given, then the
    original-binding precondition, i.e. the backend's own order), else None.

    With a resolution ``policy`` a ``new`` receipt keeps its MIME code in
    ``item["precheck_gap"]`` and today's binding code for every original that is not an
    unresolved policy original in ``item["foreign_gap"]``; the plan decides the rest.
    A third value carries the ledger facts and the resolution baseline.
    """
    from .ledger import original_resolution, store
    states = {}
    intake_path = blobs.parent / "intake.sqlite"
    projection = _projection(mail_root, blobs.parent, ledger_path) if mail_root is not None else None
    with contextlib.ExitStack() as stack:
        con = stack.enter_context(_snapshot_ledger(ledger_path))
        intake = None
        groups = _occurrence_groups(con, {item["identity"] for item in receipts})
        for item in receipts:
            account, mailbox, validity, uid = item["identity"]
            rows = con.execute("SELECT * FROM mail_messages WHERE id=? OR (account=? AND "
                               "folder=? AND uidvalidity=? AND uid=?)",
                               (message_id(item["identity"]), account, mailbox, int(validity),
                                int(uid))).fetchall()
            if not rows:
                gap = mime_precheck(item, mail_root, blobs.parent) if mail_root is not None else None
                if policy is None:
                    for sha, size in item["originals"]:
                        if gap:
                            break
                        gap = binding_conflict(con, store, blobs, sha, size)
                    states[item["identity"]] = ("new", gap)
                    continue
                foreign = None
                for sha, size in item["originals"]:
                    code = binding_conflict(con, store, blobs, sha, size)
                    if code == "original_scope_unresolved" and sha in policy.originals():
                        continue
                    foreign = foreign or code
                item["precheck_gap"], item["foreign_gap"] = gap, foreign
                states[item["identity"]] = ("new", None)
                continue
            if intake is None and intake_path.exists():
                intake = stack.enter_context(_snapshot_db(intake_path, "intake"))
            code = identity_conflict(con, store, blobs, item, rows, groups.get(item["identity"], []),
                                     intake=intake, projection=projection)
            if code:
                states[item["identity"]] = (code, None)
            else:
                states[item["identity"]] = ("complete" if _preserved(con, item) else "incomplete", None)
        unfinalized = [(row["run_id"], row["summary"]) for row in con.execute(
            "SELECT run_id,summary FROM runs WHERE kind='records-runner' AND status='running' "
            "AND ended_at IS NULL AND substr(run_id,1,?)=?", (len(RUN_PREFIX), RUN_PREFIX))]
        extra = {"facts": _ledger_facts(con, receipts, policy, census) if (policy is not None or census) else {},
                 "baseline": original_resolution.resolution_baseline(con)}
    return states, unfinalized, extra


def _resolution_plan(policy, receipts, facts, mail_root, output):
    """Run the shared pure plan on the discovered receipts and the ledger facts."""
    from .intake import mail_resolution_plan
    inputs = {}
    for item in receipts:
        occurrences = receipt_occurrences(item, mail_root, output) or {}
        inputs[item["receipt_sha256"]] = {
            "state": item["state"], "gap": item.get("precheck_gap"), "foreign_gap": item.get("foreign_gap"),
            "originals": {sha: occurrences.get(sha, []) for sha, _ in item["originals"]},
            "mail_id": message_id(item["identity"])}
    return mail_resolution_plan.plan(policy, inputs, facts)


def _classify(root, mail_root, policy=None, policy_state=None):
    receipts, gaps = discover(mail_root, root / "intake")
    states, unfinalized, extra = classify_receipts(root / "ledger.sqlite", receipts, root / "intake" / "blobs",
                                                   mail_root=mail_root, policy=policy)
    for item in receipts:
        item["state"], item["coded_gap"] = states[item["identity"]]
        item["resolvable"] = False
    plan = None
    by_digest = {item["receipt_sha256"]: item for item in receipts}
    if policy is not None:
        plan = _resolution_plan(policy, receipts, extra["facts"], mail_root, root / "intake")
        for item in receipts:
            if item["state"] == "new":
                item["coded_gap"] = plan["codes"].get(item["receipt_sha256"])
        for digest in plan["resolvable"]:
            by_digest[digest]["resolvable"] = True
    new = [r for r in receipts if r["state"] == "new"]
    coded = [r for r in new if r["coded_gap"]]
    resolvable = [by_digest[d] for d in (plan["resolvable"] if plan else ())]
    eligible = [r for r in new if not r["coded_gap"] and not r["resolvable"]]
    incomplete = [r for r in receipts if r["state"] == "incomplete"]
    complete = [r for r in receipts if r["state"] == "complete"]
    conflicts = Counter(r["state"] for r in receipts if r["state"].startswith("identity_"))
    gap_codes = Counter(r["coded_gap"] for r in coded)
    # Today's pending receipts first, then resolvable receipts in topological order.
    pending = eligible + incomplete + resolvable
    counts = plan["counts"] if plan else {"policy_originals": 0, "resolved": 0, "resolvable": 0, "blocked": 0}
    binding = [r for r in resolvable if r["receipt_sha256"] in plan["binding"]] if plan else []
    report = {"source": SOURCE,
              # Inventory: every valid receipt in the export, whatever its ledger state.
              "receipts": len(receipts),
              "attachments": sum(r["attachments"] for r in receipts),
              # Enrolled, bindings verified and every declared original preserved.
              "already_adopted": len(complete),
              # Not enrolled yet: eligible_new + resolvable + coded_gap_messages.
              "new_messages": len(new),
              "new_attachments": sum(r["attachments"] for r in new),
              "eligible_new_messages": len(eligible),
              # New receipts a pinned legacy-resolution policy lets the backend resolve and adopt.
              "resolvable_messages": len(resolvable),
              "resolvable_binding_messages": len(binding),
              "resolvable_dependent_messages": len(resolvable) - len(binding),
              "resolvable_attachments": sum(r["attachments"] for r in resolvable),
              "policy_originals": counts["policy_originals"],
              "policy_originals_resolved": counts["resolved"],
              "resolvable_originals": counts["resolvable"],
              "policy_originals_blocked": counts["blocked"],
              "held_originals": plan["held_present"] if plan else 0,
              "resolution_drift": plan["drift"] if plan else {},
              "resolution_plan": {"state": plan["plan_state"] if plan else "none"},
              "resolution_baseline": extra["baseline"],
              "resolution_policy": policy_state or {"state": "none", "sha256": None},
              # New receipts the canonical backend would refuse under its current
              # contract, by stable code. Never passed to the backend, never dropped.
              "coded_gap_messages": len(coded),
              "coded_gap_attachments": sum(r["attachments"] for r in coded),
              "coded_gaps": dict(sorted(gap_codes.items())),
              # The subset of coded_gaps outside ACCEPTED_GAP_CODES: blocking.
              "unaccepted_coded_gaps": dict(sorted((code, n) for code, n in gap_codes.items()
                                                   if code not in ACCEPTED_GAP_CODES)),
              # Enrolled with matching bindings but unfinished preservation.
              "incomplete_messages": len(incomplete),
              # Adoptable work remaining: eligible new, resolvable and incomplete.
              "pending_messages": len(pending),
              "pending_attachments": sum(r["attachments"] for r in pending),
              # Enrolled identities whose receipt now declares different bytes or
              # evidence: refused, never written.
              "identity_conflicts": dict(sorted(conflicts.items())),
              "unfinalized_runs": len(unfinalized),
              "gaps": dict(sorted(gaps.items()))}
    return report, pending, unfinalized, plan


def _after(report):
    keys = ["already_adopted", "new_messages", "incomplete_messages", "pending_messages",
            "coded_gap_messages", "coded_gaps", "unaccepted_coded_gaps", "identity_conflicts",
            "unfinalized_runs"]
    if report["resolution_policy"]["state"] == "loaded":  # no policy: the report is unchanged
        keys += ["resolvable_messages", "policy_originals_resolved", "resolvable_originals",
                 "policy_originals_blocked", "resolution_drift"]
    return {key: report[key] for key in keys}


def _identity(config_sha256):
    from .runner.core import observed_runtime
    return {"run_id": RUN_PREFIX + uuid.uuid4().hex,
            "config_sha256": hashlib.sha256(json.dumps(
                {"command": "records adopt-mail", "source": SOURCE, "adoption_config_sha256": config_sha256},
                sort_keys=True).encode()).hexdigest(),
            "mode": "preparation",
            "runtime": observed_runtime("local:" + sys.platform)}


def _recover_runs(backend, unfinalized):
    """Finalize dead prior mail-history runs as ``interrupted`` (root lock held).

    Only this command creates ``mail-history-`` runs and it holds the exclusive
    records-root lock for a run's whole life, so any such run still
    ``running`` once the lock is acquired belongs to a dead process. The
    identity is rebuilt from the run's own stored binding; the backend
    refuses any mismatch, which is reported as ``recovery_refused``.
    """
    recovered = Counter()
    for run_id, summary in unfinalized:
        try:
            binding = json.loads(summary)["runner_binding"]
            if binding.get("coverage_cutoff") is not None or binding.get("export_proof_sha256") is not None:
                raise ValueError("unexpected_export_proof")
            identity = {"run_id": run_id, "config_sha256": binding["config_sha256"],
                        "mode": binding["mode"],
                        "runtime": {"version": binding["engine_version"],
                                    "image_digest": binding["image_digest"],
                                    "code_sha256": binding["code_sha256"]}}
            recovered[backend.recover_run(identity)] += 1
        except (ValueError, KeyError, TypeError, sqlite3.Error):
            recovered["recovery_refused"] += 1
    return dict(sorted(recovered.items()))


def _load_policy(root, config):
    """(policy, state) for the config's pinned policy hash; never raises on a bad file."""
    pinned = (config or {}).get(POLICY_KEY)
    if not pinned:
        return None, {"state": "none", "sha256": None}
    from .intake import mail_resolution_policy
    try:
        return mail_resolution_policy.load(root, pinned), {"state": "loaded", "sha256": pinned}
    except (ValueError, OSError):
        return None, {"state": "mismatch", "sha256": pinned}


def adopt(root, mail_root, *, config_path=None, apply=False, backend_factory=None):
    """Dry-run (default) or apply. Returns a count-only report."""
    root = Path(os.path.abspath(root))
    mail_root = Path(os.path.abspath(mail_root))
    if root.resolve() != root or mail_root.resolve() != mail_root:
        raise AdoptionError("symlinked_path")
    if os.path.commonpath((root, mail_root)) == str(root):
        raise AdoptionError("export_inside_records_root")
    config, config_sha = load_config(config_path) if config_path is not None else (None, None)
    decision = gate(root, config)
    policy, policy_state = _load_policy(root, config)
    if not apply:
        if policy_state["state"] == "mismatch":
            # A pinned but missing or altered policy is reported, never silently ignored.
            report = _classify(root, mail_root)[0]
            report["resolution_policy"] = policy_state
        else:
            report = _classify(root, mail_root, policy, policy_state)[0]
        report.update(mode="dry-run", gate=decision, adopted_messages=0, resumed_messages=0,
                      adopted_attachments=0, resolved_originals=0, failures={})
        return report
    if not decision["open"]:
        raise AdoptionError("adoption_gate_closed:" + decision["state"])
    if policy_state["state"] == "mismatch":
        raise AdoptionError("resolution_policy_mismatch")
    from .run import root_lock
    from .runner.canonical_mail import CanonicalMailBackend
    from .runner.contracts import Folder
    from .runner.core import safe_code
    ledger_path = root / "ledger.sqlite"
    base_factory = backend_factory or CanonicalMailBackend
    # Only a pinned policy changes the factory call, so custom factories stay valid.
    factory = (base_factory if policy is None else
               lambda *args: base_factory(*args, resolution_policy=policy))
    with root_lock(root):
        # Gate and policy re-checked and the ledger classified only under the writer lock.
        if not gate(root, config)["open"]:
            raise AdoptionError("adoption_gate_changed")
        locked, locked_state = _load_policy(root, config)
        if locked_state["state"] != policy_state["state"]:
            raise AdoptionError("resolution_policy_changed")
        if policy is not None and locked.raw != policy.raw:
            raise AdoptionError("resolution_policy_changed")
        settled = _settle_ledger(ledger_path)
        _settle_intake(root / "intake" / "intake.sqlite")
        report, pending, unfinalized, plan = _classify(root, mail_root, policy, policy_state)
        if report["resolution_plan"]["state"] == "blocked":
            raise AdoptionError("resolution_plan_blocked")
        report.update(mode="apply", gate=decision, adopted_messages=0, resumed_messages=0,
                      adopted_attachments=0, resolved_originals=0, failures={})
        if settled:
            report["ledger_wal_recovered"] = True
        backend = None
        if unfinalized:
            backend = factory(mail_root, root / "intake", ledger_path)
            report["recovered_runs"] = _recover_runs(backend, unfinalized)
        if not pending:
            report["after"] = _after(_classify(root, mail_root, policy, policy_state)[0] if unfinalized else report)
            report["resolution_after"] = report["resolution_baseline"]
            return report
        backend = backend or factory(mail_root, root / "intake", ledger_path)
        identity = _identity(config_sha)
        backend.start_run(identity)
        failures = Counter()
        adopted = resumed = attachments = 0
        skipped = set()
        edges = plan["edges"] if plan else {}
        try:
            for item in pending:
                account, mailbox, validity, uid = item["identity"]
                if item["receipt_sha256"] in skipped:
                    # Its binding receipt was refused; the backend would refuse this one too.
                    failures["resolution_dependency_failed"] += 1
                    continue
                try:
                    value = backend.preserve(item["path"], account, Folder(mailbox, int(validity)), int(uid))
                except Exception as error:  # one bad receipt never stops the run
                    failures[safe_code(error) if not isinstance(error, mail_delta.Rejected) else str(error)] += 1
                    from .intake import mail_resolution_plan
                    skipped |= mail_resolution_plan.dependents_of(edges, {item["receipt_sha256"]})
                    continue
                if item["state"] == "incomplete":
                    resumed += 1
                else:
                    adopted += 1
                    attachments += len(value.attachments)
            summary = {"source": SOURCE, "receipts": report["receipts"], "pending": len(pending),
                       "new": report["new_messages"], "incomplete": report["incomplete_messages"],
                       "adopted": adopted, "resumed": resumed, "attachments": attachments,
                       "failures": sum(failures.values()),
                       "coded_gap_messages": report["coded_gap_messages"],
                       "coded_gaps": report["coded_gaps"]}
            backend.finish_run(identity, "completed_with_gaps" if failures or report["coded_gaps"]
                               else "slice_completed", summary)
        except BaseException:
            backend.finish_run(identity, "failed", {"source": SOURCE, "pending": len(pending),
                                                    "adopted": adopted, "resumed": resumed,
                                                    "coded_gap_messages": report["coded_gap_messages"],
                                                    "coded_gaps": report["coded_gaps"]})
            raise
        report.update(run_id=identity["run_id"], adopted_messages=adopted, resumed_messages=resumed,
                      adopted_attachments=attachments, failures=dict(sorted(failures.items())))
        # Read the post-run state while still holding the writer lock.
        _settle_ledger(ledger_path)
        _settle_intake(root / "intake" / "intake.sqlite")
        remaining = _classify(root, mail_root, policy, policy_state)[0]
    report["after"] = _after(remaining)
    report["resolution_after"] = remaining["resolution_baseline"]
    report["resolved_originals"] = remaining["resolution_baseline"]["rows"] - report["resolution_baseline"]["rows"]
    return report


INVENTORY_SCHEMA = "legacy-resolution-inventory-v1"


def inventory(root, mail_root):
    """Step-0 census (design section 8.1): read-only, counts on stdout, detail in a private file.

    Returns ``(document, counts)``. The set I holds originals declared unresolved without a
    resolution row (I_u) and originals that already have a mail-export row (I_r) that appear in
    any discovered receipt, whatever its state. The binding proposal depends only on receipt
    digests, shas and occurrence ids, never on discovery order or file names.
    """
    receipts, _ = discover(mail_root, root / "intake")
    states, _, extra = classify_receipts(root / "ledger.sqlite", receipts, root / "intake" / "blobs",
                                         mail_root=mail_root, census=True)
    facts = extra["facts"]
    containing, occurrences = {}, {}
    for item in receipts:
        item["state"] = states[item["identity"]][0]
        item["precheck"] = mime_precheck(item, mail_root, root / "intake") if mail_root is not None else None
        occurrences[item["receipt_sha256"]] = receipt_occurrences(item, mail_root, root / "intake") or {}
        for sha, _size in item["originals"]:
            containing.setdefault(sha, set()).add(item["receipt_sha256"])
    members = {sha for sha in containing if facts[sha]["resolution"] is not None
               or facts[sha]["scope"] == "unresolved"}
    by_digest = {item["receipt_sha256"]: item for item in receipts}
    proposals, listing = {}, []
    for sha in sorted(members):
        fact = facts[sha]
        mine = sorted(containing[sha])
        row = {"sha256": sha, "bytes": fact["size"], "legacy_format": fact["format"],
               "legacy_binding_sha256": fact["legacy_binding"], "checks": fact["check"],
               "state": "resolved" if fact["resolution"] is not None else "unresolved",
               "receipts": [{"receipt_sha256": d, "state": by_digest[d]["state"],
                             "mime_precheck": by_digest[d]["precheck"],
                             "occurrence_ids": occurrences[d].get(sha, [])} for d in mine]}
        if fact["resolution"] is not None:
            binding, occurrence = fact["resolution"]["receipt"], fact["resolution"]["occurrence"]
            proposal = {"binding_receipt": binding, "binding_occurrence_id": occurrence,
                        "dependent_receipts": [d for d in mine if d != binding], "fixed_by_existing_row": True}
        else:
            candidates = [d for d in mine if by_digest[d]["precheck"] is None]
            if not candidates:
                proposal = {"held": "resolution_no_eligible_binding_receipt"}
            elif fact["check"]:
                proposal = {"held": fact["check"]}
            else:
                binding = candidates[0]  # mine is sorted: the smallest receipt digest
                ids = occurrences[binding].get(sha, [])
                mid = message_id(by_digest[binding]["identity"])
                occurrence = mid if mid in ids else (ids[0] if ids else None)
                if occurrence is None:
                    proposal = {"held": "resolution_no_eligible_binding_receipt"}
                elif occurrence == mid and fact["format"] != "eml":
                    proposal = {"held": "original_resolution_mail_kind_mismatch"}
                else:
                    proposal = {"binding_receipt": binding, "binding_occurrence_id": occurrence,
                                "dependent_receipts": [d for d in mine if d != binding]}
        row["proposal"], proposals[sha] = proposal, proposal
        listing.append(row)
    held = {sha for sha, proposal in proposals.items() if "held" in proposal}
    for row in listing:
        proposal = row["proposal"]
        if "binding_receipt" in proposal and not proposal.get("fixed_by_existing_row"):
            siblings = sorted(held & {sha for sha, _ in by_digest[proposal["binding_receipt"]]["originals"]})
            if siblings:
                row["warnings"] = [{"code": "binding_receipt_contains_held_original", "originals": siblings}]
    document = {"schema": INVENTORY_SCHEMA, "receipts": len(receipts), "originals": listing}
    counts = {"receipts": len(receipts), "inventory": len(listing),
              "shared_between_receipts": sum(1 for row in listing if len(row["receipts"]) > 1),
              "repeated_in_receipt": sum(1 for row in listing if any(len(r["occurrence_ids"]) > 1
                                                                      for r in row["receipts"])),
              "proposed_bindings": sum(1 for row in listing if "binding_receipt" in row["proposal"]),
              "proposed_held": dict(sorted(Counter(p["held"] for p in proposals.values() if "held" in p).items()))}
    return document, counts


def write_inventory(root, mail_root, out_path):
    """Write the census to a new owner-only file; return count-only stdout data."""
    out_path = Path(os.path.abspath(out_path))
    if out_path.parent.resolve() != out_path.parent or os.path.commonpath((out_path, Path(mail_root))) == str(mail_root):
        raise AdoptionError("inventory_path_unsafe")
    document, counts = inventory(root, mail_root)
    raw = (json.dumps(document, sort_keys=True, indent=1) + "\n").encode()
    try:
        fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except OSError:
        raise AdoptionError("inventory_path_unsafe") from None
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    counts["inventory_sha256"] = hashlib.sha256(raw).hexdigest()
    return counts


def blocking(report):
    """True when a report must not be read as clean success (CLI exit 3).

    Coded gaps in ACCEPTED_GAP_CODES are reported but do not block; any other
    coded gap does.
    """
    if (report.get("failures") or report.get("gaps") or report.get("identity_conflicts")
            or report.get("unaccepted_coded_gaps") or report.get("resolution_drift")
            or report.get("policy_originals_blocked")
            or report.get("resolution_plan", {}).get("state") == "blocked"
            or report.get("resolution_policy", {}).get("state") == "mismatch"):
        return True
    if report.get("mode") == "dry-run" and (report.get("incomplete_messages") or report.get("unfinalized_runs")):
        return True
    after = report.get("after")
    if after and (after["pending_messages"] or after["incomplete_messages"]
                  or after["identity_conflicts"] or after["unfinalized_runs"]
                  or after["unaccepted_coded_gaps"] or after.get("resolution_drift")
                  or after.get("policy_originals_blocked")):
        return True
    return bool(report.get("recovered_runs", {}).get("recovery_refused"))


def main():
    parser = argparse.ArgumentParser(description="Adopt mail-history export receipts into the canonical ledger (dry-run by default).")
    parser.add_argument("--root", required=True, help="existing private records processing root")
    parser.add_argument("--mail-history", required=True, help="frozen export root containing receipts/")
    parser.add_argument("--config", help="owner-only mail-history-adoption-v1 config (required for --apply)")
    parser.add_argument("--apply", action="store_true", help="write to the canonical ledger (default: dry-run)")
    parser.add_argument("--legacy-resolution-inventory", metavar="PATH",
                        help="read-only census of legacy unresolved originals into a new owner-only JSON file")
    args = parser.parse_args()
    if args.legacy_resolution_inventory:
        if args.apply:
            print(json.dumps({"source": SOURCE, "status": "refused", "code": "inventory_is_read_only"}, sort_keys=True))
            return 2
        try:
            root, mail_root = Path(os.path.abspath(args.root)), Path(os.path.abspath(args.mail_history))
            if root.resolve() != root or mail_root.resolve() != mail_root:
                raise AdoptionError("symlinked_path")
            print(json.dumps(write_inventory(root, mail_root, args.legacy_resolution_inventory),
                             sort_keys=True, indent=1))
            return 0
        except AdoptionError as error:
            print(json.dumps({"source": SOURCE, "status": "refused", "code": str(error)}, sort_keys=True))
            return 2
        except (mail_delta.Rejected, ValueError, OSError, sqlite3.Error):
            print(json.dumps({"source": SOURCE, "status": "refused", "code": "adoption_input_rejected"}, sort_keys=True))
            return 2
    try:
        report = adopt(args.root, args.mail_history, config_path=args.config, apply=args.apply)
    except AdoptionError as error:
        print(json.dumps({"source": SOURCE, "status": "refused", "code": str(error)}, sort_keys=True))
        return 2
    except (mail_delta.Rejected, ValueError, OSError, sqlite3.Error):
        print(json.dumps({"source": SOURCE, "status": "refused", "code": "adoption_input_rejected"}, sort_keys=True))
        return 2
    print(json.dumps(report, sort_keys=True, indent=1))
    return 3 if blocking(report) else 0


if __name__ == "__main__":
    sys.exit(main())
