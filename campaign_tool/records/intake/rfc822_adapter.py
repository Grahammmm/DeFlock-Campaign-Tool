"""Pure RFC822 intake plans and strict receipt binding; no persistence or IMAP."""
import base64
import binascii
from dataclasses import dataclass
from collections.abc import Mapping
import quopri
import re

from .rfc822_inventory import (
    BudgetUsage, INVENTORY_FAILURE_CODES, PayloadCapture,
    RFC822Inventory, inventory_rfc822, sha256,
)

SCHEMA = "rfc822-wire-receipt-v1"
ADAPTER_FAILURE_CODES = frozenset({
    "rfc822_invalid_plan", "rfc822_incomplete_inventory",
    "rfc822_invalid_scalar_classification", "rfc822_scalar_payload_invalid",
    "rfc822_total_byte_limit", "rfc822_capture_limit",
    "rfc822_invalid_receipt_shape", "rfc822_missing_receipt",
    "rfc822_unlisted_receipt", "rfc822_duplicate_receipt", "rfc822_receipt_mismatch",
})
RFC822_FAILURE_CODES = INVENTORY_FAILURE_CODES | ADAPTER_FAILURE_CODES


class RFC822AdapterError(ValueError):
    """Fixed non-narrative error code, suitable for a literal typed allowlist."""


def _require(condition, code):
    if not condition:
        raise RFC822AdapterError(code)


def _safe_filename(name):
    base = re.split(r"[/\\]", name or "")[-1]
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", base).strip(".")[:100]
    return cleaned or "attachment.bin"


def receipt_metadata(capture):
    """Return fresh JSON-ready metadata; private writer adds and checks its path."""
    return {
        "schema": SCHEMA, "part": capture.part, "mime": capture.mime,
        "mime_chain": list(capture.mime_chain), "parent_part": capture.parent_part,
        "parent_sha256": capture.parent_sha256, "sha256": capture.sha256,
        "bytes": len(capture.payload), "wire_start": capture.wire_start,
        "wire_end": capture.wire_end, "content_type": capture.content_type,
        "original_filename": capture.original_filename,
        "filename": _safe_filename(capture.original_filename), "kind": capture.kind,
        "format": "eml" if capture.kind == "eml" else None,
    }


@dataclass(frozen=True)
class RFC822IntakePlan:
    inventory: RFC822Inventory
    captures: tuple[PayloadCapture, ...]
    scalar_roles: tuple[tuple[str, str], ...]
    usage: BudgetUsage
    complete: bool

    @property
    def receipts(self):
        return tuple(receipt_metadata(capture) for capture in self.captures)

    def as_manifest(self):
        root = self.inventory.sources[0]
        return {
            "schema": SCHEMA, "complete": self.complete,
            "original_eml": {"part": "0", "sha256": root.sha256,
                             "bytes": len(root.raw_bytes), "kind": "eml"},
            "attachments": list(self.receipts),
            "scalar_roles": [{"part": part, "role": role} for part, role in self.scalar_roles],
            "budget": {"total_bytes": self.usage.total_bytes,
                       "messages": self.usage.messages, "mime_parts": self.usage.mime_parts,
                       "captures": self.usage.captures, "max_depth": self.usage.max_depth},
        }


def prepare_rfc822_intake(raw_bytes, *, limits=None):
    """Preserve every RFC822 capture in a plan; scalar policy is not guessed."""
    inventory = inventory_rfc822(raw_bytes, limits=limits)
    return RFC822IntakePlan(inventory, inventory.captures, (), inventory.usage,
                           not inventory.scalar_parts)


def _decode_scalar(raw, encoding):
    if encoding in {"7bit", "8bit", "binary"}:
        return raw
    if encoding == "base64":
        compact = raw.translate(None, b" \t\r\n")
        try:
            decoded = base64.b64decode(compact, validate=True)
        except (ValueError, binascii.Error):
            raise RFC822AdapterError("rfc822_scalar_payload_invalid") from None
        _require(base64.b64encode(decoded) == compact, "rfc822_scalar_payload_invalid")
        return decoded
    if encoding == "quoted-printable":
        # A strict subset: explicit hex escapes and LF/CRLF soft breaks only.
        # No tolerant repair, bare CR, non-ASCII literals or transport padding.
        for line in raw.split(b"\n"):
            line = line[:-1] if line.endswith(b"\r") else line
            _require(len(line) <= 76 and not line.endswith((b" ", b"\t")),
                     "rfc822_scalar_payload_invalid")
        index = 0
        while index < len(raw):
            byte = raw[index]
            if byte == 61:
                if raw[index + 1:index + 3] == b"\r\n":
                    index += 3
                elif raw[index + 1:index + 2] == b"\n":
                    index += 2
                else:
                    pair = raw[index + 1:index + 3]
                    _require(len(pair) == 2 and re.fullmatch(rb"[0-9A-Fa-f]{2}", pair) is not None,
                             "rfc822_scalar_payload_invalid")
                    index += 3
            elif byte == 13:
                _require(raw[index + 1:index + 2] == b"\n", "rfc822_scalar_payload_invalid")
                index += 2
            else:
                _require(byte in (9, 10) or 32 <= byte <= 126, "rfc822_scalar_payload_invalid")
                index += 1
        return quopri.decodestring(raw)
    raise RFC822AdapterError("rfc822_scalar_payload_invalid")


def classify_scalar_parts(plan, roles):
    """Apply an exhaustive owner-supplied scalar policy and shared capture budget.

    The caller owns related/body ambiguity semantics. RFC822 nodes are never
    scalar choices, so no filename/body policy can exempt those captures.
    Reclassification rebuilds from the original immutable inventory, not the
    prior derived plan, preventing accumulated duplicate scalar captures.
    """
    _require(type(plan) is RFC822IntakePlan, "rfc822_invalid_plan")
    _require(isinstance(roles, Mapping), "rfc822_invalid_scalar_classification")
    leaves = plan.inventory.scalar_parts
    _require(len(roles) == len(leaves) and set(roles) == {part.part for part in leaves},
             "rfc822_invalid_scalar_classification")
    _require(all(type(role) is str and role in {"body", "attachment"}
                 for role in roles.values()), "rfc822_invalid_scalar_classification")
    sources = {source.part: source for source in plan.inventory.sources}
    captures = list(plan.inventory.captures)
    total = plan.inventory.usage.total_bytes
    for part in leaves:
        if roles[part.part] != "attachment":
            continue
        _require(len(captures) < plan.inventory.limits.max_captures, "rfc822_capture_limit")
        source = sources[part.message_part]
        payload = _decode_scalar(source.raw_bytes[part.body_start:part.wire_end],
                                 part.transfer_encoding)
        _require(total + len(payload) <= plan.inventory.limits.max_total_bytes,
                 "rfc822_total_byte_limit")
        total += len(payload)
        captures.append(PayloadCapture(
            part.part, part.mime_chain, part.message_part, part.message_sha256,
            part.mime, part.body_start, part.wire_end, part.content_type,
            part.original_filename, "mime", payload, sha256(payload),
        ))
    old = plan.inventory.usage
    return RFC822IntakePlan(
        plan.inventory, tuple(captures), tuple((part.part, roles[part.part]) for part in leaves),
        BudgetUsage(total, old.messages, old.mime_parts, len(captures), old.max_depth), True,
    )


def bind_rfc822_receipts(plan, receipts):
    """Bind every expected capture and immediate parent edge exactly once.

    This checks metadata/membership only. The persistence owner must securely
    read and verify staged bytes and paths before any ledger mutation.
    """
    _require(type(plan) is RFC822IntakePlan, "rfc822_invalid_plan")
    _require(plan.complete is True, "rfc822_incomplete_inventory")
    _require(type(receipts) in (list, tuple), "rfc822_invalid_receipt_shape")
    _require(len(receipts) <= plan.inventory.limits.max_captures,
             "rfc822_invalid_receipt_shape")
    expected = {capture.part: receipt_metadata(capture) for capture in plan.captures}
    supplied = set()
    for receipt in receipts:
        _require(type(receipt) is dict and type(receipt.get("part")) is str,
                 "rfc822_invalid_receipt_shape")
        key = receipt["part"]
        _require(key in expected, "rfc822_unlisted_receipt")
        _require(key not in supplied, "rfc822_duplicate_receipt")
        supplied.add(key)
        wanted = expected[key]
        _require(set(receipt) in (set(wanted), set(wanted) | {"path"}),
                 "rfc822_invalid_receipt_shape")
        _require(all(type(receipt[field]) is int for field in ("bytes", "wire_start", "wire_end")),
                 "rfc822_invalid_receipt_shape")
        _require(type(receipt["mime_chain"]) is list, "rfc822_invalid_receipt_shape")
        _require(all(receipt[field] == value for field, value in wanted.items()),
                 "rfc822_receipt_mismatch")
    _require(supplied == set(expected), "rfc822_missing_receipt")
    return plan.captures
