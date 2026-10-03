"""Pure bounded RFC822 inventory over the frozen exact-wire sidecar.

No body classification, filesystem writes, mailbox access or reserialization.
"""
from dataclasses import dataclass
import hashlib

from . import wire_rfc822 as wire

MAX_TOTAL_BYTES = 512 * 1024 * 1024
MAX_CAPTURES = 100
INVENTORY_FAILURE_CODES = frozenset({
    "rfc822_invalid_limits", "rfc822_invalid_raw_bytes",
    "rfc822_message_byte_limit", "rfc822_total_byte_limit",
    "rfc822_depth_limit", "rfc822_part_limit", "rfc822_capture_limit",
    "rfc822_wire_rejected",
})


class RFC822InventoryError(ValueError):
    """Fixed safe code; never source headers, filenames or exception text."""


def require(condition, code):
    if not condition:
        raise RFC822InventoryError(code)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class RFC822Limits:
    max_message_bytes: int = wire.MAX_BYTES
    max_total_bytes: int = MAX_TOTAL_BYTES
    max_depth: int = wire.MAX_DEPTH
    max_parts: int = wire.MAX_PARTS
    max_captures: int = MAX_CAPTURES

    def __post_init__(self):
        for value, ceiling in (
            (self.max_message_bytes, wire.MAX_BYTES),
            (self.max_total_bytes, MAX_TOTAL_BYTES),
            (self.max_depth, wire.MAX_DEPTH),
            (self.max_parts, wire.MAX_PARTS),
            (self.max_captures, MAX_CAPTURES),
        ):
            require(type(value) is int and 1 <= value <= ceiling,
                    "rfc822_invalid_limits")


@dataclass(frozen=True)
class BudgetUsage:
    total_bytes: int
    messages: int
    mime_parts: int
    captures: int
    max_depth: int


@dataclass(frozen=True)
class MessageSource:
    part: str
    mime_chain: tuple[str, ...]
    parent_part: str | None
    parent_sha256: str | None
    sha256: str
    raw_bytes: bytes
    root_depth: int


@dataclass(frozen=True)
class WirePart:
    part: str
    mime_chain: tuple[str, ...]
    message_part: str
    message_sha256: str
    mime: str
    parent_mime: str | None
    depth: int
    wire_start: int
    body_start: int
    wire_end: int
    content_type: str
    transfer_encoding: str
    original_filename: str | None
    disposition: str | None
    content_id: str | None
    headers: tuple[tuple[str, str], ...]

    @property
    def is_scalar(self):
        return not self.content_type.startswith("multipart/") and self.content_type != "message/rfc822"


@dataclass(frozen=True)
class PayloadCapture:
    part: str
    mime_chain: tuple[str, ...]
    parent_part: str
    parent_sha256: str
    mime: str
    wire_start: int
    wire_end: int
    content_type: str
    original_filename: str | None
    kind: str
    payload: bytes
    sha256: str


@dataclass(frozen=True)
class RFC822Inventory:
    limits: RFC822Limits
    sources: tuple[MessageSource, ...]
    parts: tuple[WirePart, ...]
    captures: tuple[PayloadCapture, ...]
    usage: BudgetUsage

    @property
    def scalar_parts(self):
        return tuple(part for part in self.parts if part.is_scalar)


def inventory_rfc822(raw_bytes, *, limits=None):
    """Enumerate all nested RFC822 occurrences with one non-resetting budget.

    Message-root depths include RFC822 transitions, not just multipart paths.
    Total bytes count the outer original and every captured EML occurrence,
    including duplicate content. Scalar decoding is a separate adapter phase.
    """
    limits = RFC822Limits() if limits is None else limits
    require(type(limits) is RFC822Limits, "rfc822_invalid_limits")
    require(type(raw_bytes) is bytes and bool(raw_bytes), "rfc822_invalid_raw_bytes")
    sources, parts, captures = [], [], []
    total_bytes, total_parts, deepest = 0, 0, 0

    def visit(raw, chain, source_part, parent_part, parent_sha, root_depth):
        nonlocal total_bytes, total_parts, deepest
        require(root_depth <= limits.max_depth, "rfc822_depth_limit")
        require(0 < len(raw) <= limits.max_message_bytes, "rfc822_message_byte_limit")
        require(total_bytes + len(raw) <= limits.max_total_bytes, "rfc822_total_byte_limit")
        require(total_parts < limits.max_parts, "rfc822_part_limit")
        total_bytes += len(raw)
        message_sha = sha256(raw)
        try:
            root, nodes = wire._wire_tree(
                raw, limits.max_depth - root_depth + 1,
                limits.max_parts - total_parts)
            wire._map_stdlib(raw, root, nodes)
        except wire.WireRFC822Error as error:
            code = {"depth_limit": "rfc822_depth_limit", "part_limit": "rfc822_part_limit"}.get(
                str(error), "rfc822_wire_rejected")
            raise RFC822InventoryError(code) from None
        except (ValueError, TypeError, IndexError, RecursionError, UnicodeError):
            raise RFC822InventoryError("rfc822_wire_rejected") from None
        total_parts += len(nodes)
        sources.append(MessageSource(source_part, chain, parent_part, parent_sha,
                                     message_sha, raw, root_depth))
        # Charge every outer part before entering any contained message.
        for node in nodes:
            depth = root_depth + node.locator.count(".")
            deepest = max(deepest, depth)
            node_chain = chain + (node.locator,)
            headers = node.headers
            part_key = "mime:" + "/".join(node_chain)
            parts.append(WirePart(
                part_key, node_chain, source_part, message_sha, node.locator,
                node.locator.rsplit(".", 1)[0] if "." in node.locator else None,
                depth, node.start, node.body, node.end, node.ctype, node.cte,
                headers.get_filename(), headers.get_content_disposition(),
                str(headers["Content-ID"]) if headers["Content-ID"] is not None else None,
                tuple(headers.raw_items()),
            ))
        for node in nodes:
            if node.ctype != "message/rfc822":
                continue
            require(len(captures) < limits.max_captures, "rfc822_capture_limit")
            depth = root_depth + node.locator.count(".")
            require(depth < limits.max_depth, "rfc822_depth_limit")
            size = node.end - node.body
            require(total_bytes + size <= limits.max_total_bytes, "rfc822_total_byte_limit")
            node_chain = chain + (node.locator,)
            capture_part = "rfc822:" + "/".join(node_chain)
            payload = raw[node.body:node.end]
            capture = PayloadCapture(
                capture_part, node_chain, source_part, message_sha, node.locator,
                node.body, node.end, "message/rfc822", node.headers.get_filename(),
                "eml", payload, sha256(payload),
            )
            captures.append(capture)
            visit(payload, node_chain, capture_part, source_part, message_sha, depth + 1)

    visit(raw_bytes, (), "0", None, None, 1)
    return RFC822Inventory(
        limits, tuple(sources), tuple(parts), tuple(captures),
        BudgetUsage(total_bytes, len(sources), total_parts, len(captures), deepest),
    )
