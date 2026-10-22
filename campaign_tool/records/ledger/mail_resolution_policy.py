"""Hash-pinned owner policy for legacy mail resolution; never an approval oracle."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from . import store

MAX_POLICY_BYTES = 2 * 1024 * 1024
MAX_ORIGINALS = 10000
MAX_RECEIPTS = 10000
POLICY_FIELDS = {"schema", "inventory_sha256", "approval_sha256", "originals", "held"}
ENTRY_FIELDS = {"bytes", "role", "scope", "verification_receipt_sha256",
                "binding_occurrence_id", "dependent_receipts", "legacy_binding_sha256"}


def require(value):
    if not value:
        raise ValueError("resolution_policy_invalid")


def sha(value):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result)
        result[key] = value
    return result


def decode(raw):
    def invalid(_):
        raise ValueError("resolution_policy_invalid")
    try:
        return json.loads(raw, object_pairs_hook=_pairs, parse_constant=invalid)
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("resolution_policy_invalid") from None


def read_regular(path, limit, *, owner_only=False):
    """No symlinks in any component, bounded reads, and no blocking FIFO open."""
    path = Path(os.path.abspath(path))
    directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            require(stat.S_ISREG(before.st_mode) and before.st_size <= limit)
            if owner_only:
                require(before.st_uid == os.getuid() and not stat.S_IMODE(before.st_mode) & 0o077)
            raw = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
            current = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
            fingerprint = lambda st: (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
            require(len(raw) <= limit and fingerprint(before) == fingerprint(after) == fingerprint(current))
            return raw
    finally:
        os.close(directory)


class ApprovedMailResolutionPolicy:
    """Trusted startup supplies the expected hash, never evidence or a CLI flag.

    File location/permissions are enforced by load(root, expected_sha256=...).
    The bytes constructor permits synthetic fixtures and already securely read
    artifacts. A matching hash does not authenticate the owner's approval.
    """
    def __init__(self, raw, *, expected_sha256):
        require(type(raw) is bytes and len(raw) <= MAX_POLICY_BYTES and sha(expected_sha256))
        if hashlib.sha256(raw).hexdigest() != expected_sha256:
            raise ValueError("resolution_policy_mismatch")
        value = decode(raw)
        require(type(value) is dict and set(value) == POLICY_FIELDS
                and value["schema"] == "approved-mail-resolution-policy-v1")
        require(sha(value["inventory_sha256"]) and sha(value["approval_sha256"]))
        entries, held = value["originals"], value["held"]
        require(type(entries) is dict and type(held) is list and len(entries) + len(held) <= MAX_ORIGINALS)
        self._sorted_hashes(held, MAX_ORIGINALS)
        require(not set(entries).intersection(held))
        for subject, item in entries.items():
            require(sha(subject) and type(item) is dict and set(item) == ENTRY_FIELDS)
            require(type(item["bytes"]) is int and item["bytes"] >= 0
                    and item["role"] == "original" and item["scope"] == "in_scope"
                    and all(sha(item[key]) for key in ("verification_receipt_sha256",
                            "binding_occurrence_id", "legacy_binding_sha256")))
            self._sorted_hashes(item["dependent_receipts"], MAX_RECEIPTS)
            require(item["verification_receipt_sha256"] not in item["dependent_receipts"])
        # Canonical bytes make the policy identity independent of input layout.
        require(raw == store.canonical(value).encode())
        self.raw, self.sha256 = raw, expected_sha256

    @staticmethod
    def _sorted_hashes(values, limit):
        require(type(values) is list and len(values) <= limit and all(sha(v) for v in values))
        require(values == sorted(set(values)))

    @classmethod
    def load(cls, root, *, expected_sha256):
        require(sha(expected_sha256))
        try:
            raw = read_regular(Path(root) / "resolution-policies" / expected_sha256,
                               MAX_POLICY_BYTES, owner_only=True)
        except (OSError, ValueError):
            raise ValueError("resolution_policy_invalid") from None
        return cls(raw, expected_sha256=expected_sha256)

    def admission(self, subject, size):
        item = decode(self.raw)["originals"].get(subject)
        require(item is not None and type(size) is int and item["bytes"] == size)
        return item  # Fresh decode: caller mutations cannot change this policy.
