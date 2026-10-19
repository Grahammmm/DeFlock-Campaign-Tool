"""Adopt frozen mail-history export receipts into the canonical records ledger.

Dry-run by default. ``--apply`` enrolls each pending receipt through the
existing CanonicalMailBackend (mail_delta byte/MIME verification, canonical
mail/occurrence identity checks) and tags the run ``source=mail-history``.

The adoption gate is configuration, never file deletion: when the records root
holds ``.adoption-disabled-v1.json`` the apply step is refused unless an
owner-only adoption config explicitly enables adoption *and* pins the exact
sha256 of that marker. The marker is only read, never moved or removed.

Idempotent: receipts whose (account, folder, uidvalidity, uid) identity already
has a ``mail_messages`` row are counted as already adopted and skipped; a run
with nothing pending writes nothing. Reports carry counts and hashes only, no
paths, account names, headers or exception narratives. The export directory is
only read; this command never connects to a mailbox or runs an exporter.
"""
import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import uuid

from .intake import folder as intake_folder, mail_delta

SOURCE = "mail-history"
CONFIG_SCHEMA = "mail-history-adoption-v1"
DISABLE_MARKER = ".adoption-disabled-v1.json"
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
            _, identity, items = mail_delta.load_receipt(path)
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
                      "receipt_sha256": receipt_sha,
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


def adopted_identities(ledger_path, receipts, blobs):
    """Return (adopted identity set, {identity: conflict code} for pending ones)."""
    from .ledger import store
    adopted, conflicts = set(), {}
    with store.ledger(ledger_path, readonly=True) as con:
        for item in receipts:
            account, mailbox, validity, uid = item["identity"]
            row = con.execute("SELECT 1 FROM mail_messages WHERE account=? AND folder=? AND "
                              "uidvalidity=? AND uid=?", (account, mailbox, int(validity), int(uid))).fetchone()
            if row:
                adopted.add(item["identity"])
                continue
            for sha, size in item["originals"]:
                code = binding_conflict(con, store, blobs, sha, size)
                if code:
                    conflicts[item["identity"]] = code
                    break
    return adopted, conflicts


def _classify(root, mail_root):
    receipts, gaps = discover(mail_root, root / "intake")
    done, conflicts = adopted_identities(root / "ledger.sqlite", receipts, root / "intake" / "blobs")
    pending = [r for r in receipts if r["identity"] not in done]
    report = {"source": SOURCE, "receipts": len(receipts),
              "attachments": sum(r["attachments"] for r in receipts),
              "already_adopted": len(receipts) - len(pending),
              "pending_messages": len(pending),
              "pending_attachments": sum(r["attachments"] for r in pending),
              # Pending receipts the canonical backend will refuse until the existing
              # ledger declaration is resolved through its own reviewed contract.
              "predicted_binding_conflicts": dict(sorted(Counter(conflicts.values()).items())),
              "gaps": dict(sorted(gaps.items()))}
    return report, pending


def _identity(config_sha256):
    from .runner.core import observed_runtime
    return {"run_id": "mail-history-" + uuid.uuid4().hex,
            "config_sha256": hashlib.sha256(json.dumps(
                {"command": "records adopt-mail", "source": SOURCE, "adoption_config_sha256": config_sha256},
                sort_keys=True).encode()).hexdigest(),
            "mode": "preparation",
            "runtime": observed_runtime("local:" + sys.platform)}


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
    report, pending = _classify(root, mail_root)
    report.update(mode="apply" if apply else "dry-run", gate=decision,
                  adopted_messages=0, adopted_attachments=0, failures={})
    if not apply:
        return report
    if not decision["open"]:
        raise AdoptionError("adoption_gate_closed:" + decision["state"])
    from .run import root_lock
    from .runner.canonical_mail import CanonicalMailBackend
    from .runner.contracts import Folder
    from .runner.core import safe_code
    with root_lock(root):
        # Re-check gate and pending set under the writer lock.
        if not gate(root, config)["open"]:
            raise AdoptionError("adoption_gate_changed")
        fresh, pending = _classify(root, mail_root)
        report.update(fresh)
        if not pending:
            return report
        factory = backend_factory or CanonicalMailBackend
        backend = factory(mail_root, root / "intake", root / "ledger.sqlite")
        identity = _identity(config_sha)
        backend.start_run(identity)
        failures = Counter()
        adopted = attachments = 0
        try:
            for item in pending:
                account, mailbox, validity, uid = item["identity"]
                try:
                    value = backend.preserve(item["path"], account, Folder(mailbox, int(validity)), int(uid))
                except Exception as error:  # one bad receipt never stops the run
                    failures[safe_code(error) if not isinstance(error, mail_delta.Rejected) else str(error)] += 1
                    continue
                adopted += 1
                attachments += len(value.attachments)
            summary = {"source": SOURCE, "receipts": report["receipts"], "pending": len(pending),
                       "adopted": adopted, "attachments": attachments, "failures": sum(failures.values())}
            backend.finish_run(identity, "completed_with_gaps" if failures else "slice_completed", summary)
        except BaseException:
            backend.finish_run(identity, "failed", {"source": SOURCE, "pending": len(pending), "adopted": adopted})
            raise
        report.update(run_id=identity["run_id"], adopted_messages=adopted, adopted_attachments=attachments,
                      failures=dict(sorted(failures.items())))
    remaining = _classify(root, mail_root)[0]
    report["after"] = {"already_adopted": remaining["already_adopted"],
                       "pending_messages": remaining["pending_messages"]}
    return report


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
    except (mail_delta.Rejected, ValueError, OSError):
        print(json.dumps({"source": SOURCE, "status": "refused", "code": "adoption_input_rejected"}, sort_keys=True))
        return 2
    print(json.dumps(report, sort_keys=True, indent=1))
    return 3 if report["failures"] or report["gaps"] else 0


if __name__ == "__main__":
    sys.exit(main())
