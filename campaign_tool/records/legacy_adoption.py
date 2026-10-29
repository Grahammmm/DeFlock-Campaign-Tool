"""Offline, owner-pinned legacy declaration adoption. No content acceptance."""
import argparse
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import sqlite3
import sys
import uuid

from campaign_tool import __version__
from .run import root_lock
from .runner.canonical_reconciliation import CanonicalOriginalReconciler

from .intake import local_capture as capture
from .ledger import store
from .ledger.mail_resolution_policy import read_regular

CLASSES = ("received", "received-user-files", "portal-intake", "productions", "supplement")
FORMATS = {"pdf", "png", "jpg", "jpeg", "docx", "xlsx", "zip", "txt", "html", "htm", "md"}
require = capture.require


def private_bytes(path, limit, *, exact_mode=False):
    path = Path(os.path.abspath(path))
    try:
        raw = read_regular(path, limit, owner_only=True)
    except ValueError:
        raise ValueError("legacy_input_mode") from None
    require(not exact_mode or stat.S_IMODE(path.stat().st_mode) == 0o600, "legacy_input_mode")
    return raw


def load_inputs(args):
    raw = private_bytes(args.config, 65536, exact_mode=True)
    config = capture.decode(raw)
    fields = {"schema", "manifest_sha256", "policy_sha256", "export_root", "supplement_root", "adoption"}
    require(type(config) is dict and set(config) == fields and config["schema"] == "legacy-adoption-v1"
            and config["adoption"] in ("enabled", "disabled"), "legacy_config_invalid")
    for key in ("manifest_sha256", "policy_sha256"):
        capture.sha(config[key])
    for key in ("export_root", "supplement_root"):
        require(type(config[key]) is str and Path(config[key]).is_absolute()
                and ".." not in Path(config[key]).parts, "legacy_config_invalid")
    manifest = private_bytes(args.manifest, 48 * 1024 * 1024, exact_mode=True)
    require(capture.digest(manifest) == config["manifest_sha256"], "legacy_manifest_hash_mismatch")
    policy_raw = private_bytes(args.policy, capture.MAX_POLICY_BYTES)
    require(capture.digest(policy_raw) == config["policy_sha256"], "legacy_policy_hash_mismatch")
    policy = capture.ApprovedCapturePolicy(policy_raw, expected_sha256=config["policy_sha256"])
    return config, capture.digest(raw), manifest, policy


def source_class(path, config):
    parts = path.parts
    require(".hermes" not in parts and not any(parts[i:i+2] == ("agent-work", "flock-mvp")
            for i in range(len(parts)-1)), "legacy_source_path_forbidden")
    require(".." not in parts, "legacy_source_path_forbidden")
    export, supplement = Path(config["export_root"]), Path(config["supplement_root"])
    if path.is_relative_to(export):
        relative = path.relative_to(export).parts
        require(len(relative) >= 2 and relative[0] in CLASSES[:-1], "legacy_source_class_unknown")
        return relative[0]
    require(path.is_relative_to(supplement) and path != supplement, "legacy_source_root_not_admitted")
    return "supplement"


def parse_manifest(raw, config):
    reader = csv.reader(io.StringIO(raw.decode("utf-8"), newline=""), delimiter="\t", strict=True)
    require(next(reader, None) == ["sha256", "size", "source_path"], "legacy_manifest_invalid")
    selected, seen = {}, set()
    for number, values in enumerate(reader, 1):
        require(number <= 10000 and len(values) == 3, "legacy_manifest_invalid")
        subject, size, source = values
        capture.sha(subject)
        require(re.fullmatch(r"[0-9]+", size) and len(size) <= 12
                and 0 <= int(size) <= capture.folder.LIMITS["source_bytes"]
                and Path(source).is_absolute() and len(source) <= 4096
                and not any(ord(char) < 32 for char in source), "legacy_manifest_invalid")
        require((subject, source) not in seen, "legacy_manifest_duplicate")
        seen.add((subject, source))
        row = {"sha256": subject, "size": int(size), "source_path": source}
        kind = source_class(Path(source), config)
        prior = selected.get(subject)
        require(prior is None or prior[0]["size"] == row["size"], "legacy_manifest_conflict")
        if prior is None or (CLASSES.index(kind), source) < (CLASSES.index(prior[1]), prior[0]["source_path"]):
            selected[subject] = (row, kind)
    return [selected[key] for key in sorted(selected)]


def verify_source(row, *, apply=False):
    """Stream once, without following links or blocking on a FIFO."""
    path = Path(row["source_path"])
    directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            require(stat.S_ISREG(before.st_mode) and before.st_uid == os.getuid()
                    and before.st_nlink == 1 and not stat.S_IMODE(before.st_mode) & 0o077,
                    "legacy_source_unsafe")
            if apply:
                require(bool(os.fstatvfs(stream.fileno()).f_flag & os.ST_RDONLY), "local_capture_requires_readonly_source")
            hasher, size = hashlib.sha256(), 0
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                hasher.update(block)
                size += len(block)
                if size > row["size"]:
                    return False
            return (size == row["size"] and hasher.hexdigest() == row["sha256"]
                    and capture.fingerprint(before) == capture.fingerprint(os.fstat(stream.fileno()))
                    == capture.fingerprint(os.stat(path.name, dir_fd=directory, follow_symlinks=False)))
    finally:
        os.close(directory)


def preflight(root, rows, *, apply=False, policy=None):
    counts = dict.fromkeys(("eligible", "excluded_metadata_class", "format_unknown", "already_bound",
                           "already_adopted", "adopted", "hash_mismatch"), 0)
    counts["source_classes"] = dict.fromkeys(CLASSES, 0)
    results, eligible = [], []
    with store.ledger(root / "ledger.sqlite", readonly=True) as con:
        for row, kind in rows:
            subject = row["sha256"]
            declared = con.execute("SELECT * FROM originals WHERE sha256=?", (subject,)).fetchone()
            require(declared is not None, "canonical_original_missing")
            form = declared["legacy_format"]
            counts["source_classes"][kind] += 1
            if form in {"json", "csv"}:
                result = "excluded_metadata_class"
            elif form not in FORMATS:
                result = "format_unknown"
            else:
                require(declared["scope"] != "out_of_scope", "original_out_of_scope")
                original = store.effective_original(con, subject)
                if original.get("resolution_sha256"):
                    state = con.execute("SELECT status FROM stage_state WHERE original_sha256=? AND stage='preserve'",
                                        (subject,)).fetchone()
                    binding = con.execute("SELECT policy_sha256 FROM original_resolutions WHERE original_sha256=?",
                                          (subject,)).fetchone()
                    if apply:
                        policy.admission(subject, row["size"])
                        require(binding[0] == policy.sha256 and original["storage_path"] == str(root / "intake" / "blobs" / subject),
                                "legacy_existing_binding_conflict")
                    result = "already_adopted" if state[0] == "done" else "eligible"
                elif not (declared["role"] == declared["scope"] == "unresolved" and declared["storage_path"] is None):
                    result = "already_bound"
                else:
                    result = "eligible"
                if result == "eligible":
                    require(declared["bytes"] == row["size"], "legacy_manifest_conflict")
                    if apply:
                        policy.admission(subject, row["size"])
                        source = Path(row["source_path"])
                        require(not source.is_relative_to(root / "intake")
                                and not any((parent / ".git").exists() for parent in source.parents),
                                "legacy_source_unsafe")
                    eligible.append((row, kind))
                    if not verify_source(row, apply=apply):
                        counts["hash_mismatch"] += 1
            counts[result] += 1
            results.append(dict(original_sha256=subject, source_class=kind,
                                result="legacy_format_unknown" if result == "format_unknown" else result))
    return counts, results, eligible


def apply_batch(root, policy, config_hash, config, counts, results, eligible):
    backend = CanonicalOriginalReconciler(root / "mail", root / "intake", root / "ledger.sqlite", policy=policy)
    identity = {"run_id": "legacy-adoption-" + uuid.uuid4().hex, "config_sha256": config_hash,
        "mode": "preparation", "runtime": {"version": "legacy-adoption-v1/" + __version__,
        "image_digest": "local:" + sys.platform, "code_sha256": capture.digest(Path(__file__).read_bytes()),
        "image_verification": {"state": "local_process_unverified"}}}
    backend.start_run(identity)
    status = "failed"
    try:
        for row, kind in eligible:
            backend._enroll_local_capture_locked(row["source_path"], original_sha256=row["sha256"])
            backend._accept_locked(row["sha256"])
            counts["adopted"] += 1
            next(item for item in results if item["original_sha256"] == row["sha256"])["result"] = "adopted"
        for item in results:
            if item["result"] == "already_adopted":
                backend._accept_locked(item["original_sha256"])
        status = "completed_with_gaps" if any(counts[key] for key in
            ("excluded_metadata_class", "format_unknown", "already_bound")) else "slice_completed"
    finally:
        backend.finish_run(identity, status, counts)
        directory = root / "legacy-adoption"
        directory.mkdir(mode=0o700, exist_ok=True)
        capture.private_path(directory, directory=True)
        receipt = {"schema": "legacy-adoption-receipt-v1", "config_sha256": config_hash,
            "manifest_sha256": config["manifest_sha256"], "policy_sha256": config["policy_sha256"],
            "counts": counts, "rows": results}
        with capture.folder.private_file(directory / (identity["run_id"] + ".json"), exclusive=True) as stream:
            stream.write(capture.encoded(receipt).decode() + "\n")


def write_proposal(path, value):
    path = store.checked_path(path, existing=False)
    capture.private_path(path.parent, directory=True)
    with capture.folder.private_file(path, exclusive=True) as stream:
        stream.write(capture.encoded(value).decode() + "\n")


def proposal(config, config_hash, eligible):
    return {"schema": "approved-local-capture-policy-v1", "inventory_sha256": config["manifest_sha256"],
        "approval_sha256": config_hash, "originals": {row["sha256"]: {"bytes": row["size"],
        "role": "original", "scope": "in_scope", "evidence_sha256": capture.digest(capture.encoded(row))}
        for row, kind in eligible}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("root", "config", "manifest", "policy"):
        parser.add_argument("--" + key, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--write-policy-proposal")
    args = parser.parse_args(argv)
    try:
        config, config_hash, raw, policy = load_inputs(args)
        rows = parse_manifest(raw, config)
        root = store.checked_path(Path(os.path.abspath(args.root)) / "ledger.sqlite").parent
        if args.apply:
            require(not args.write_policy_proposal, "legacy_config_invalid")
            require(config["adoption"] == "enabled", "legacy_adoption_disabled")
            # Revalidate the owner-pinned closure under the writer lock; no
            # backend construction, run row or CAS mutation before preflight.
            with root_lock(root):
                fresh = load_inputs(args)
                require(fresh[1] == config_hash, "legacy_config_changed")
                counts, results, eligible = preflight(root, rows, apply=True, policy=policy)
                require(not counts["hash_mismatch"], "legacy_source_hash_mismatch")
                apply_batch(root, policy, config_hash, config, counts, results, eligible)
        else:
            counts, results, eligible = preflight(root, rows)
            if args.write_policy_proposal and not counts["hash_mismatch"]:
                write_proposal(args.write_policy_proposal, proposal(config, config_hash, eligible))
        print(json.dumps(counts, sort_keys=True))
        return 3 if counts["hash_mismatch"] else 0
    except (OSError, ValueError, KeyError, TypeError, csv.Error, sqlite3.Error, RuntimeError) as exc:
        # Only fixed code keys can escape. OS errors and parser details may
        # contain paths, filenames or source text and must never be printed.
        codes = {"legacy_input_mode", "legacy_config_invalid", "legacy_manifest_hash_mismatch",
            "legacy_policy_hash_mismatch", "legacy_source_path_forbidden", "legacy_source_root_not_admitted",
            "legacy_source_class_unknown", "legacy_manifest_invalid", "legacy_manifest_duplicate",
            "legacy_manifest_conflict", "legacy_source_unsafe", "legacy_adoption_disabled",
            "legacy_config_changed", "legacy_source_hash_mismatch", "capture_not_in_approved_inventory",
            "original_out_of_scope", "canonical_original_missing", "local_capture_requires_readonly_source"}
        code = str(exc) if str(exc) in codes else "legacy_adoption_refused"
        print(json.dumps({code: 1}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
