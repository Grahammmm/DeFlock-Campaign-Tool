"""Owner-approved mail-resolution policy for legacy unresolved originals (T4B slice A).

Trusted startup only. The policy is a private, content-addressed artifact; its hash
and exact schema are checked here, not the authenticity of the owner's approval.
No receipt, CLI flag or callback selects a policy. This grants no review role and
resolves nothing by itself: ledger/original_resolution.append_mail_export does that.
"""
import json
import os
from pathlib import Path
import re
import stat
from . import folder
from .local_capture import digest, sha

SCHEMA = "approved-mail-resolution-policy-v1"
MAX_POLICY_BYTES = 2 * 1024 * 1024
MAX_POLICY_ENTRIES = 10000
MAX_DEPENDENT_RECEIPTS = 10000
TOP_KEYS = {"schema", "inventory_sha256", "approval_sha256", "originals", "held"}
ENTRY_KEYS = {"bytes", "role", "scope", "verification_receipt_sha256", "binding_occurrence_id",
              "dependent_receipts", "legacy_binding_sha256"}


def require(value, reason):
    if not value:
        raise ValueError(reason)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "resolution_policy_invalid")
        result[key] = value
    return result


def _refuse_constant(_):
    raise ValueError("resolution_policy_invalid")


def decode(raw):
    try:
        return json.loads(raw, object_pairs_hook=_pairs, parse_constant=_refuse_constant)
    except (ValueError, RecursionError):
        raise ValueError("resolution_policy_invalid") from None


def _digest(value):
    """Lower-case 64-hex only; every malformed digest is one safe code."""
    require(type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value), "resolution_policy_invalid")
    return value


def _sorted_unique(values):
    require(type(values) is list and all(type(item) is str for item in values)
            and values == sorted(set(values)), "resolution_policy_invalid")
    for item in values:
        _digest(item)
    return values


class ApprovedMailResolutionPolicy:
    """Exact owner-approved policy: one binding receipt and occurrence per original."""

    def __init__(self, raw, *, expected_sha256):
        require(type(raw) is bytes and len(raw) <= MAX_POLICY_BYTES, "resolution_policy_invalid")
        require(digest(raw) == sha(expected_sha256), "resolution_policy_hash_mismatch")
        value = decode(raw)
        require(type(value) is dict and set(value) == TOP_KEYS and value["schema"] == SCHEMA,
                "resolution_policy_invalid")
        _digest(value["inventory_sha256"])
        _digest(value["approval_sha256"])
        entries, held = value["originals"], value["held"]
        require(type(entries) is dict, "resolution_policy_invalid")
        _sorted_unique(held)
        require(1 <= len(entries) + len(held) <= MAX_POLICY_ENTRIES, "resolution_policy_invalid")
        require(not set(entries) & set(held), "resolution_policy_invalid")
        for subject, item in entries.items():
            _digest(subject)
            require(type(item) is dict and set(item) == ENTRY_KEYS
                    and type(item["bytes"]) is int and 0 <= item["bytes"] <= folder.LIMITS["source_bytes"]
                    and item["role"] == "original" and item["scope"] == "in_scope",
                    "resolution_policy_invalid")
            _digest(item["verification_receipt_sha256"])
            _digest(item["binding_occurrence_id"])
            _digest(item["legacy_binding_sha256"])
            dependents = _sorted_unique(item["dependent_receipts"])
            require(len(dependents) <= MAX_DEPENDENT_RECEIPTS
                    and item["verification_receipt_sha256"] not in dependents, "resolution_policy_invalid")
        self.raw, self.sha256 = raw, expected_sha256
        self.inventory_sha256, self.approval_sha256 = value["inventory_sha256"], value["approval_sha256"]
        self._entries = decode(json.dumps(entries, sort_keys=True, allow_nan=False).encode())
        self._held = frozenset(held)

    def entry(self, subject):
        """A copy of the policy entry for one original, or None when not in `originals`."""
        item = self._entries.get(subject)
        return None if item is None else json.loads(json.dumps(item))

    def originals(self):
        return sorted(self._entries)

    def held(self):
        return sorted(self._held)

    def is_held(self, subject):
        return subject in self._held


def load(root, expected_sha256):
    """Read `<root>/resolution-policies/<sha256>`: owner-only, regular, non-symlink."""
    expected = sha(expected_sha256)
    path = Path(root) / "resolution-policies" / expected
    try:
        with folder.secure_open(path) as stream:
            info = os.fstat(stream.fileno())
            require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                    and not stat.S_IMODE(info.st_mode) & 0o077 and info.st_nlink == 1,
                    "resolution_policy_unsafe_file")
            raw = stream.read(MAX_POLICY_BYTES + 1)
    except (OSError, ValueError):
        raise ValueError("resolution_policy_unsafe_file") from None
    return ApprovedMailResolutionPolicy(raw, expected_sha256=expected)
