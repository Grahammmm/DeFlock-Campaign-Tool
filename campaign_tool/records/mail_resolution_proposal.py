"""Offline authoring of a mail-resolution policy PROPOSAL from a census file.

A proposal is not approval: nothing here installs, pins, applies or loads the
policy, opens the ledger, takes a lock or reads source mail. The only new
filesystem entry is the exclusively created private output file.
"""
import hashlib
import os
from pathlib import Path
import stat

from . import mail_adoption as adoption
from .ledger import store
from .ledger.mail_resolution_policy import (
    MAX_ORIGINALS, MAX_POLICY_BYTES, MAX_RECEIPTS, ApprovedMailResolutionPolicy,
    decode, read_regular, sha)

MAX_INVENTORY_BYTES = 32 * 1024 * 1024
MAX_APPROVAL_BYTES = 2 * 1024 * 1024
INVENTORY_SCHEMA = "legacy-mail-resolution-inventory-v1"
POLICY_SCHEMA = "approved-mail-resolution-policy-v1"
_STATES = {"proposed", "fixed", "held"}


def _invalid():
    return adoption.AdoptionError("resolution_proposal_input_invalid")


def _need(value):
    if not value:
        raise _invalid()


def _read(path, limit):
    path = Path(os.path.abspath(path))
    try:
        return read_regular(path, limit)
    except (OSError, ValueError):
        raise _invalid() from None


def _hashes(values):
    _need(type(values) is list and len(values) <= MAX_RECEIPTS and all(sha(v) for v in values)
          and values == sorted(set(values)))


def _project(inventory):
    """Select by entry['proposal']['state'] only; shape-check everything first."""
    _need(type(inventory) is dict and set(inventory) == {"schema", "counts", "originals"}
          and inventory["schema"] == INVENTORY_SCHEMA and type(inventory["counts"]) is dict
          and all(type(v) is int and v >= 0 for k, v in inventory["counts"].items()
                  if k not in {"proposed_held", "discovery_gaps"}))
    entries = inventory["originals"]
    _need(type(entries) is dict and len(entries) <= MAX_ORIGINALS)
    originals, held, fixed = {}, [], 0
    for subject, entry in entries.items():
        _need(sha(subject) and type(entry) is dict and type(entry.get("proposal")) is dict
              and type(entry.get("receipts")) is list and type(entry.get("bytes")) is int
              and type(entry["bytes"]) is not bool and entry["bytes"] >= 0)
        proposal = entry["proposal"]
        state = proposal.get("state")
        _need(state in _STATES)
        receipts = {}
        for member in entry["receipts"]:
            _need(type(member) is dict and sha(member.get("receipt_sha256"))
                  and member["receipt_sha256"] not in receipts)
            _hashes(member.get("occurrence_ids"))
            receipts[member["receipt_sha256"]] = set(member["occurrence_ids"])
        if state == "held":
            held.append(subject)
        elif state == "fixed":
            fixed += 1
        else:
            binding = proposal.get("verification_receipt_sha256")
            occurrence = proposal.get("binding_occurrence_id")
            dependents = proposal.get("dependent_receipts")
            _need(sha(binding) and sha(occurrence) and sha(entry.get("legacy_binding_sha256"))
                  and binding in receipts and occurrence in receipts[binding])
            _hashes(dependents)
            _need(set(dependents) == set(receipts) - {binding})
            originals[subject] = {
                "bytes": entry["bytes"], "role": "original", "scope": "in_scope",
                "verification_receipt_sha256": binding, "binding_occurrence_id": occurrence,
                "dependent_receipts": list(dependents),
                "legacy_binding_sha256": entry["legacy_binding_sha256"]}
    return originals, sorted(held), fixed


def _open_parent(target, sources, inputs):
    unsafe = adoption.AdoptionError("resolution_proposal_output_unsafe")
    if (target.resolve() != target or target in inputs
            or any(target.is_relative_to(p) for p in sources)
            or any(os.path.lexists(p / ".git") for p in target.parents)):
        raise unsafe
    try:
        directory = os.open(target.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in target.parts[1:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                os.close(directory)
                directory = child
            info = os.fstat(directory)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
                raise unsafe
        except BaseException:
            os.close(directory)
            raise
    except OSError:
        raise unsafe from None
    return directory


def write_proposal(root, mail_root, output_path, *, inventory_path, approval_record_path):
    """Create OUT exclusively (0600) and return the count/hash summary."""
    inventory_raw = _read(inventory_path, MAX_INVENTORY_BYTES)
    approval_raw = _read(approval_record_path, MAX_APPROVAL_BYTES)
    try:
        decoded = decode(inventory_raw)
    except ValueError:
        raise _invalid() from None
    _need(inventory_raw == store.canonical(decoded).encode())
    originals, held, fixed = _project(decoded)
    value = {"schema": POLICY_SCHEMA,
             "inventory_sha256": hashlib.sha256(inventory_raw).hexdigest(),
             "approval_sha256": hashlib.sha256(approval_raw).hexdigest(),
             "originals": originals, "held": held}
    raw = store.canonical(value).encode()
    digest = hashlib.sha256(raw).hexdigest()
    try:
        # Self-computed hash: schema/canonicality check only, never authority.
        ApprovedMailResolutionPolicy(raw, expected_sha256=digest)
    except ValueError:
        raise _invalid() from None
    target = Path(os.path.abspath(output_path))
    sources = [Path(os.path.abspath(p)) for p in (root, mail_root)]
    inputs = {Path(os.path.abspath(p)) for p in (inventory_path, approval_record_path)}
    directory = _open_parent(target, sources, inputs)
    try:
        try:
            fd = os.open(target.name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                         0o600, dir_fd=directory)
        except FileExistsError:
            raise adoption.AdoptionError("resolution_proposal_output_exists") from None
        except OSError:
            raise adoption.AdoptionError("resolution_proposal_output_unsafe") from None
        mine = os.fstat(fd)
        try:
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            try:
                now = os.stat(target.name, dir_fd=directory, follow_symlinks=False)
                if (now.st_dev, now.st_ino) == (mine.st_dev, mine.st_ino):
                    os.unlink(target.name, dir_fd=directory)
            except OSError:
                pass
            raise
    finally:
        os.close(directory)
    return {"counts": {"originals": len(originals), "held": len(held), "fixed_omitted": fixed},
            "proposal_sha256": digest}
