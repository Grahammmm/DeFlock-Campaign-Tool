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
* ``complete`` -- the identity is enrolled, its canonical bindings (message id,
  EML hash, occurrence set, attachment set, verification-receipt hash, byte
  sizes, original declarations) match this receipt exactly, and every declared
  original has a ``done`` preserve stage;
* ``incomplete`` -- bindings match but preservation is unfinished (for example
  enrollment committed and the process then failed or crashed). Apply resumes
  these through the same idempotent backend call;
* an ``identity_*`` conflict -- the identity is enrolled but this receipt
  declares different bytes or evidence. It is refused, never written, and the
  earlier evidence is left untouched.

Only new and incomplete receipts are passed to the backend; a run with nothing
pending writes nothing. Reports carry counts and hashes only, no paths,
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
DISABLE_MARKER = ".adoption-disabled-v1.json"
RUN_PREFIX = "mail-history-"
MAX_RECEIPTS = 10000
MAX_SMALL_FILE = 64 * 1024


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
    if (not isinstance(config, dict) or config.get("schema") != CONFIG_SCHEMA
            or config.get("source") != SOURCE
            or config.get("adoption") not in ("enabled", "disabled")):
        raise AdoptionError("adoption_config_invalid")
    pinned = config.get("disable_marker_sha256")
    if pinned is not None and not (isinstance(pinned, str) and len(pinned) == 64
                                   and all(c in "0123456789abcdef" for c in pinned)):
        raise AdoptionError("adoption_config_invalid")
    if set(config) - {"schema", "source", "adoption", "disable_marker_sha256"}:
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
def _snapshot_ledger(ledger_path):
    """Read-only ledger access that never creates, changes or removes a file.

    ``store.ledger(readonly=True)`` opens ``mode=ro``, which creates ``-shm``
    (and rebuilds it from a crashed writer's WAL), so it is never used here.
    A cleanly checkpointed ledger (no or empty WAL, no rollback journal) is
    read with ``immutable=1``, which touches no sidecar. A non-empty WAL means
    a live writer or an unrecovered crash; the read then fails closed with
    ``ledger_wal_not_checkpointed`` before opening anything. Recovery belongs
    to a writer holding the records-root lock (``--apply``), not to a dry run.
    The ledger and all sidecar identities must be unchanged afterwards,
    otherwise the read fails closed with ``ledger_changed_during_read``.
    """
    from .ledger import store
    from .ledger.migrations import v001
    path = store.checked_path(ledger_path)
    store.private_parent(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
        raise AdoptionError("ledger_not_owner_only")
    before = _sidecar_keys(path)
    if (before[1] is not None and before[1][1] > 0) or before[3] is not None:
        raise AdoptionError("ledger_wal_not_checkpointed")
    con = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA query_only=ON")
        row = con.execute("SELECT checksum FROM schema_migrations WHERE version=?",
                          (v001.VERSION,)).fetchone()
        if (con.execute("PRAGMA user_version").fetchone()[0] != v001.VERSION
                or row is None or row[0] != store.SCHEMA_HASH):
            raise AdoptionError("ledger_schema_mismatch")
        yield con
    finally:
        con.close()
    if _sidecar_keys(path) != before:
        raise AdoptionError("ledger_changed_during_read")


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


def message_id(identity):
    """Same canonical mail id the backend derives for an identity."""
    account, mailbox, validity, uid = identity
    return hashlib.sha256(json.dumps(["mail", account, mailbox, int(validity), int(uid)],
                                     separators=(",", ":")).encode()).hexdigest()


def _occurrence_groups(con, wanted):
    groups = {}
    for row in con.execute("SELECT id,original_sha256,kind,source_ref,evidence FROM occurrences "
                           "WHERE acquisition_method='verified_mail_export' AND kind IN ('mail','attachment')"):
        try:
            source = json.loads(row["source_ref"])
            key = (source["account"], source["folder"], str(source["uidvalidity"]), str(source["uid"]))
        except (ValueError, KeyError, TypeError):
            continue
        if key in wanted:
            groups.setdefault(key, []).append(row)
    return groups


def identity_conflict(con, store, blobs, item, rows, occurrences):
    """Validate an enrolled identity against this receipt, read-only.

    Returns a safe ``identity_*`` code when the canonical bindings differ from
    the receipt; None when they match exactly. Never rewrites evidence.
    """
    mid = message_id(item["identity"])
    if len(rows) != 1 or rows[0]["id"] != mid:
        return "identity_message_id_mismatch"
    declared = dict(item["originals"])
    eml_sha = item["originals"][0][0]
    if rows[0]["eml_sha256"] != eml_sha:
        return "identity_eml_changed"
    mails = [o for o in occurrences if o["kind"] == "mail"]
    if len(mails) != 1 or mails[0]["id"] != mid or mails[0]["original_sha256"] != eml_sha:
        return "identity_occurrence_mismatch"
    attached = [o["original_sha256"] for o in occurrences if o["kind"] == "attachment"]
    expected = {sha for sha, _ in item["originals"][1:]} | item["native_shas"]
    if set(attached) != expected or len(attached) < len(item["originals"]) - 1:
        return "identity_attachment_set_changed"
    for occurrence in occurrences:
        try:
            evidence = json.loads(occurrence["evidence"])
        except ValueError:
            return "identity_evidence_unreadable"
        if not isinstance(evidence, dict) or evidence.get("receipt_sha256") != item["receipt_sha256"]:
            return "identity_receipt_changed"
        sha = occurrence["original_sha256"]
        if evidence.get("cas_sha256") != sha or (sha in declared and evidence.get("bytes") != declared[sha]):
            return "identity_evidence_mismatch"
    for sha, size in item["originals"]:
        if not store.effective_original(con, sha):
            return "identity_original_missing"
        code = binding_conflict(con, store, blobs, sha, size)
        if code:
            return "identity_" + code
    return None


def _preserved(con, item):
    for sha, _ in item["originals"]:
        row = con.execute("SELECT status FROM stage_state WHERE original_sha256=? AND stage='preserve'",
                          (sha,)).fetchone()
        if row is None or row["status"] != "done":
            return False
    return True


def classify_receipts(ledger_path, receipts, blobs):
    """Read-only classification. Returns ({identity: (state, predicted)}, unfinalized runs).

    state is ``new``, ``complete``, ``incomplete`` or an ``identity_*`` code;
    ``predicted`` is the backend binding refusal expected for a ``new`` receipt.
    """
    from .ledger import store
    states = {}
    with _snapshot_ledger(ledger_path) as con:
        groups = _occurrence_groups(con, {item["identity"] for item in receipts})
        for item in receipts:
            account, mailbox, validity, uid = item["identity"]
            rows = con.execute("SELECT id,eml_sha256 FROM mail_messages WHERE id=? OR (account=? AND "
                               "folder=? AND uidvalidity=? AND uid=?)",
                               (message_id(item["identity"]), account, mailbox, int(validity),
                                int(uid))).fetchall()
            if not rows:
                predicted = None
                for sha, size in item["originals"]:
                    predicted = binding_conflict(con, store, blobs, sha, size)
                    if predicted:
                        break
                states[item["identity"]] = ("new", predicted)
                continue
            code = identity_conflict(con, store, blobs, item, rows, groups.get(item["identity"], []))
            if code:
                states[item["identity"]] = (code, None)
            else:
                states[item["identity"]] = ("complete" if _preserved(con, item) else "incomplete", None)
        unfinalized = [(row["run_id"], row["summary"]) for row in con.execute(
            "SELECT run_id,summary FROM runs WHERE kind='records-runner' AND status='running' "
            "AND ended_at IS NULL AND substr(run_id,1,?)=?", (len(RUN_PREFIX), RUN_PREFIX))]
    return states, unfinalized


def _classify(root, mail_root):
    receipts, gaps = discover(mail_root, root / "intake")
    states, unfinalized = classify_receipts(root / "ledger.sqlite", receipts, root / "intake" / "blobs")
    for item in receipts:
        item["state"], item["predicted"] = states[item["identity"]]
    new = [r for r in receipts if r["state"] == "new"]
    incomplete = [r for r in receipts if r["state"] == "incomplete"]
    complete = [r for r in receipts if r["state"] == "complete"]
    conflicts = Counter(r["state"] for r in receipts if r["state"].startswith("identity_"))
    predicted = Counter(r["predicted"] for r in new if r["predicted"])
    pending = new + incomplete
    report = {"source": SOURCE,
              # Inventory: every valid receipt in the export, whatever its ledger state.
              "receipts": len(receipts),
              "attachments": sum(r["attachments"] for r in receipts),
              # Enrolled, bindings verified and every declared original preserved.
              "already_adopted": len(complete),
              # Not enrolled yet; eligible excludes predicted backend refusals.
              "new_messages": len(new),
              "new_attachments": sum(r["attachments"] for r in new),
              "eligible_new_messages": len(new) - sum(predicted.values()),
              # Enrolled with matching bindings but unfinished preservation.
              "incomplete_messages": len(incomplete),
              # Work remaining: new plus incomplete. Zero only when nothing is unfinished.
              "pending_messages": len(pending),
              "pending_attachments": sum(r["attachments"] for r in pending),
              # Enrolled identities whose receipt now declares different bytes or
              # evidence: refused, never written.
              "identity_conflicts": dict(sorted(conflicts.items())),
              # New receipts the canonical backend will refuse until the existing
              # ledger declaration is resolved through its own reviewed contract.
              "predicted_binding_conflicts": dict(sorted(predicted.items())),
              "unfinalized_runs": len(unfinalized),
              "gaps": dict(sorted(gaps.items()))}
    return report, pending, unfinalized


def _after(report):
    return {key: report[key] for key in ("already_adopted", "new_messages", "incomplete_messages",
                                         "pending_messages", "identity_conflicts", "unfinalized_runs")}


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
    if not apply:
        report = _classify(root, mail_root)[0]
        report.update(mode="dry-run", gate=decision, adopted_messages=0, resumed_messages=0,
                      adopted_attachments=0, failures={})
        return report
    if not decision["open"]:
        raise AdoptionError("adoption_gate_closed:" + decision["state"])
    from .run import root_lock
    from .runner.canonical_mail import CanonicalMailBackend
    from .runner.contracts import Folder
    from .runner.core import safe_code
    ledger_path = root / "ledger.sqlite"
    factory = backend_factory or CanonicalMailBackend
    with root_lock(root):
        # Gate re-checked and the ledger classified only under the writer lock.
        if not gate(root, config)["open"]:
            raise AdoptionError("adoption_gate_changed")
        settled = _settle_ledger(ledger_path)
        report, pending, unfinalized = _classify(root, mail_root)
        report.update(mode="apply", gate=decision, adopted_messages=0, resumed_messages=0,
                      adopted_attachments=0, failures={})
        if settled:
            report["ledger_wal_recovered"] = True
        backend = None
        if unfinalized:
            backend = factory(mail_root, root / "intake", ledger_path)
            report["recovered_runs"] = _recover_runs(backend, unfinalized)
        if not pending:
            report["after"] = _after(_classify(root, mail_root)[0] if unfinalized else report)
            return report
        backend = backend or factory(mail_root, root / "intake", ledger_path)
        identity = _identity(config_sha)
        backend.start_run(identity)
        failures = Counter()
        adopted = resumed = attachments = 0
        try:
            for item in pending:
                account, mailbox, validity, uid = item["identity"]
                try:
                    value = backend.preserve(item["path"], account, Folder(mailbox, int(validity)), int(uid))
                except Exception as error:  # one bad receipt never stops the run
                    failures[safe_code(error) if not isinstance(error, mail_delta.Rejected) else str(error)] += 1
                    continue
                if item["state"] == "incomplete":
                    resumed += 1
                else:
                    adopted += 1
                    attachments += len(value.attachments)
            summary = {"source": SOURCE, "receipts": report["receipts"], "pending": len(pending),
                       "new": report["new_messages"], "incomplete": report["incomplete_messages"],
                       "adopted": adopted, "resumed": resumed, "attachments": attachments,
                       "failures": sum(failures.values())}
            backend.finish_run(identity, "completed_with_gaps" if failures else "slice_completed", summary)
        except BaseException:
            backend.finish_run(identity, "failed", {"source": SOURCE, "pending": len(pending),
                                                    "adopted": adopted, "resumed": resumed})
            raise
        report.update(run_id=identity["run_id"], adopted_messages=adopted, resumed_messages=resumed,
                      adopted_attachments=attachments, failures=dict(sorted(failures.items())))
        # Read the post-run state while still holding the writer lock.
        _settle_ledger(ledger_path)
        remaining = _classify(root, mail_root)[0]
    report["after"] = _after(remaining)
    return report


def blocking(report):
    """True when a report must not be read as clean success (CLI exit 3)."""
    if report.get("failures") or report.get("gaps") or report.get("identity_conflicts"):
        return True
    if report.get("mode") == "dry-run" and (report.get("incomplete_messages") or report.get("unfinalized_runs")):
        return True
    after = report.get("after")
    if after and (after["pending_messages"] or after["incomplete_messages"]
                  or after["identity_conflicts"] or after["unfinalized_runs"]):
        return True
    return bool(report.get("recovered_runs", {}).get("recovery_refused"))


def main():
    parser = argparse.ArgumentParser(description="Adopt mail-history export receipts into the canonical ledger (dry-run by default).")
    parser.add_argument("--root", required=True, help="existing private records processing root")
    parser.add_argument("--mail-history", required=True, help="frozen export root containing receipts/")
    parser.add_argument("--config", help="owner-only mail-history-adoption-v1 config (required for --apply)")
    parser.add_argument("--apply", action="store_true", help="write to the canonical ledger (default: dry-run)")
    args = parser.parse_args()
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
