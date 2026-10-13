"""Current local byte capture; never an inferred mail delivery or content review."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from . import folder
from .mail_delta import private_path

VERSION = "local-capture-v1"
MAX_POLICY_BYTES = 2 * 1024 * 1024
MAX_POLICY_ORIGINALS = 10000


def require(value, reason):
    if not value:
        raise ValueError(reason)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def sha(value):
    require(type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value), "invalid_capture_hash")
    return value


def fingerprint(value):
    return [value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns]


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate_capture_json_key")
        result[key] = value
    return result


def decode(raw):
    return json.loads(raw, object_pairs_hook=_pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("invalid_capture_json")))


class ApprovedCapturePolicy:
    """Trusted startup only, from an exact owner-approved private policy artifact.

    No receipt/CLI input selects this policy. Approval is an external prerequisite,
    not inferred by this class or by a matching hash. This grants no review role.
    """
    def __init__(self, raw, *, expected_sha256):
        require(type(raw) is bytes and len(raw) <= MAX_POLICY_BYTES, "capture_policy_bound")
        require(digest(raw) == sha(expected_sha256), "capture_policy_hash_mismatch")
        value = decode(raw)
        require(type(value) is dict and set(value) == {"schema", "inventory_sha256", "approval_sha256", "originals"}
                and value["schema"] == "approved-local-capture-policy-v1", "capture_policy_schema")
        sha(value["inventory_sha256"]); sha(value["approval_sha256"])
        entries = value["originals"]
        require(type(entries) is dict and 1 <= len(entries) <= MAX_POLICY_ORIGINALS, "capture_policy_inventory_bound")
        for subject, item in entries.items():
            sha(subject)
            require(type(item) is dict and set(item) == {"bytes", "role", "scope", "evidence_sha256"}
                    and type(item["bytes"]) is int and 0 <= item["bytes"] <= folder.LIMITS["source_bytes"]
                    and item["role"] == "original" and item["scope"] == "in_scope", "capture_policy_original")
            sha(item["evidence_sha256"])
        self.raw, self.sha256 = raw, expected_sha256
        self._entries = decode(encoded(entries))

    def admission(self, subject, size):
        item = self._entries.get(subject)
        require(item is not None and item["bytes"] == size, "capture_not_in_approved_inventory")
        return dict(item)


def _store(output, directory, raw):
    key = digest(raw)
    target = Path(output) / directory
    target.mkdir(mode=0o700, exist_ok=True)
    private_path(target, directory=True)
    destination = target / key
    if destination.exists():
        with folder.secure_open(destination) as stream:
            require(stream.read(len(raw) + 1) == raw, "capture_artifact_conflict")
        return key, str(destination)
    fd, temporary = tempfile.mkstemp(prefix=".capture-", dir=target)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        os.chmod(temporary, 0o400)
        try:
            os.link(temporary, destination, follow_symlinks=False)
        except FileExistsError:
            with folder.secure_open(destination) as stream:
                require(stream.read(len(raw) + 1) == raw, "capture_artifact_conflict")
        directory_fd = os.open(target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return key, str(destination)


def capture(source_path, output, subject, policy):
    require(type(policy) is ApprovedCapturePolicy, "installed_capture_policy_required")
    subject = sha(subject)
    source = Path(os.path.abspath(source_path))
    private_path(source)
    output = Path(output)
    private_path(output, directory=True)
    private_path(output / "blobs", directory=True)
    require(not any((parent / ".git").exists() for parent in (source.parent,*source.parents)),
            "local_capture_source_in_git")
    require(not any((parent / ".git").exists() for parent in (output,*output.parents)),
            "local_capture_output_in_git")
    require(not source.is_relative_to(output), "local_capture_source_inside_output")
    with folder.secure_open(source) as stream:
        before = os.fstat(stream.fileno())
        require(before.st_uid == os.getuid() and before.st_nlink == 1
                and stat.S_ISREG(before.st_mode) and not stat.S_IMODE(before.st_mode) & 0o077,
                "unsafe_local_capture_source")
        require(bool(os.fstatvfs(stream.fileno()).f_flag & os.ST_RDONLY), "local_capture_requires_readonly_source")
        policy.admission(subject, before.st_size)
        folder.check_space(output, before.st_size)
        fd, temporary = tempfile.mkstemp(prefix=".local-capture-", dir=output)
        try:
            hasher, consumed = hashlib.sha256(), 0
            with os.fdopen(fd, "wb") as target:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    consumed += len(block)
                    require(consumed <= before.st_size, "local_capture_source_changed")
                    hasher.update(block); target.write(block)
                target.flush(); os.fsync(target.fileno())
            require(consumed == before.st_size and hasher.hexdigest() == subject
                    and fingerprint(before) == fingerprint(os.fstat(stream.fileno()))
                    == fingerprint(source.stat(follow_symlinks=False)), "local_capture_source_changed")
            folder.seal_blob(temporary, subject, output)
        finally:
            Path(temporary).unlink(missing_ok=True)
    policy_hash, _ = _store(output, "resolution-policies", policy.raw)
    source_ref = {"schema": VERSION, "path": str(source), "stat": fingerprint(before),
                  "meaning": "current_local_capture_no_delivery_inference"}
    receipt = {"schema": VERSION, "source": source_ref, "original_sha256": subject,
               "bytes": consumed, "policy_sha256": policy_hash,
               "admission": policy.admission(subject, consumed)}
    receipt_hash, receipt_path = _store(output, "local-capture-receipts", encoded(receipt))
    oid = "local-capture:" + digest(encoded([source_ref, subject, policy_hash]))
    row = dict(id=oid, original_sha256=subject, kind="local", source_ref=encoded(source_ref).decode(),
               parent_occurrence_id=None, acquired_at=None, acquisition_method="verified_local_capture",
               evidence=encoded({"receipt_sha256": receipt_hash, "verification_receipt_path": receipt_path,
                                 "policy_sha256": policy_hash}).decode())
    return receipt, receipt_hash, row


def verify_occurrence(row, output, subject, *, expected_policy=None, verify_cas=True):
    require(row["original_sha256"] == subject and row["kind"] == "local"
            and row["parent_occurrence_id"] is None and row["acquired_at"] is None
            and row["acquisition_method"] == "verified_local_capture", "local_capture_occurrence_binding")
    output = Path(output)
    evidence = decode(row["evidence"])
    require(type(evidence) is dict and set(evidence) == {"receipt_sha256", "verification_receipt_path", "policy_sha256"},
            "local_capture_evidence_schema")
    receipt_hash = sha(evidence["receipt_sha256"])
    require(evidence["verification_receipt_path"] == str(output / "local-capture-receipts" / receipt_hash),
            "local_capture_receipt_path")
    with folder.secure_open(evidence["verification_receipt_path"]) as stream:
        raw = stream.read(16385)
    require(len(raw) <= 16384 and digest(raw) == receipt_hash, "local_capture_receipt_hash")
    receipt = decode(raw)
    require(type(receipt) is dict and set(receipt) == {"schema", "source", "original_sha256", "bytes", "policy_sha256", "admission"}
            and receipt["schema"] == VERSION and receipt["original_sha256"] == subject
            and receipt["source"] == decode(row["source_ref"]), "local_capture_receipt_binding")
    source = receipt["source"]
    require(type(source) is dict and set(source) == {"schema", "path", "stat", "meaning"}
            and source["schema"] == VERSION and source["meaning"] == "current_local_capture_no_delivery_inference"
            and type(source["path"]) is str and len(source["path"]) <= 4096
            and type(source["stat"]) is list and len(source["stat"]) == 5
            and all(type(value) is int for value in source["stat"]), "local_capture_source_binding")
    policy_hash = sha(receipt["policy_sha256"])
    require(evidence["policy_sha256"] == policy_hash, "local_capture_policy_binding")
    require(expected_policy is None or policy_hash == expected_policy, "local_capture_policy_changed")
    with folder.secure_open(output / "resolution-policies" / policy_hash) as stream:
        raw_policy = stream.read(MAX_POLICY_BYTES + 1)
    policy = ApprovedCapturePolicy(raw_policy, expected_sha256=policy_hash)
    require(receipt["admission"] == policy.admission(subject, receipt["bytes"])
            and row["id"] == "local-capture:" + digest(encoded([source, subject, policy_hash])),
            "local_capture_admission_binding")
    require(type(receipt["bytes"]) is int and receipt["bytes"] == source["stat"][2], "local_capture_cas_length")
    if verify_cas:
        path = output / "blobs" / subject
        with folder.secure_open(path) as stream:
            before = os.fstat(stream.fileno())
            require(before.st_uid == os.getuid() and before.st_nlink == 1
                    and stat.S_IMODE(before.st_mode) == 0o400, "unsafe_local_capture_cas")
            hasher, size = hashlib.sha256(), 0
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                size += len(block)
                require(size <= receipt["bytes"], "local_capture_cas_length")
                hasher.update(block)
            require(size == receipt["bytes"] and hasher.hexdigest() == subject
                    and fingerprint(before) == fingerprint(os.fstat(stream.fileno()))
                    == fingerprint(path.stat(follow_symlinks=False)), "local_capture_cas_changed")
    return receipt_hash
