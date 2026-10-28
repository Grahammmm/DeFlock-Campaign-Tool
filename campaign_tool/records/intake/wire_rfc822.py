"""Bounded, fail-closed message/rfc822 wire capture; no I/O or reserialization."""
from dataclasses import dataclass, field
from email import policy
from email.errors import MultipartInvariantViolationDefect
from email.parser import BytesHeaderParser, BytesParser
import re

MAX_BYTES = 64 * 1024 * 1024
MAX_DEPTH = 32
MAX_PARTS = 1000
MAX_HEADER_BYTES = 65536
MAX_HEADER_LINE = 998
_TOKEN = r"[!#$%&\x27*+\-.^_`|~0-9A-Za-z]+"
_TYPE = re.compile(rf"({_TOKEN})/({_TOKEN})")
_PARAM = re.compile(
    rf"[ \t]*;[ \t]*({_TOKEN})[ \t]*=[ \t]*"
    rf"({_TOKEN}|\"(?:[\t\x20-\x21\x23-\x5b\x5d-\x7e]|\\[\t\x20-\x7e])*\")"
)
_FIELD = re.compile(rb"[\x21-\x39\x3b-\x7e]+")
_BOUNDARY = re.compile(r"[0-9A-Za-z\x27()+_,\-./:=? ]{1,70}")
_IDENTITY = {"7bit", "8bit", "binary"}
_KNOWN_CTE = _IDENTITY | {"base64", "quoted-printable"}
_STRUCTURAL = {"content-type", "content-transfer-encoding", "content-disposition",
               "mime-version"}
_OPAQUE = b"Subject: synthetic wire placeholder\r\nContent-Type: text/plain\r\n\r\nopaque"


class WireRFC822Error(ValueError):
    """Stable non-narrative error code, never input content or a filesystem path."""


def _require(condition, code):
    if not condition:
        raise WireRFC822Error(code)


def _lines(raw, start, end):
    """Yield offsets and physical LF/CRLF lines without normalizing source bytes."""
    while start < end:
        lf = raw.find(b"\n", start, end)
        stop = end if lf < 0 else lf + 1
        content_end = stop if lf < 0 else lf
        if lf >= 0 and content_end > start and raw[content_end - 1] == 13:
            content_end -= 1
        yield start, stop, raw[start:content_end]
        start = stop


def _content_type(value, default_type):
    if value is None:
        return default_type, {}
    match = _TYPE.match(value)
    _require(match is not None, "invalid_content_type")
    ctype = (match[1] + "/" + match[2]).lower()
    params, position = {}, match.end()
    while position < len(value):
        match = _PARAM.match(value, position)
        _require(match is not None, "invalid_content_type")
        key, literal = match[1].lower(), match[2]
        _require(key not in params, "duplicate_content_type_parameter")
        if literal.startswith('"'):
            literal = re.sub(r"\\(.)", r"\1", literal[1:-1])
        params[key] = literal
        position = match.end()
    _require(not any(key.startswith("boundary*") for key in params),
             "unsupported_boundary_parameter")
    return ctype, params


@dataclass
class _Node:
    start: int
    end: int
    body: int
    locator: str
    headers: object
    ctype: str
    cte: str
    boundary: bytes | None
    children: list = field(default_factory=list)


def _headers(raw, start, end, locator, default_type="text/plain"):
    values, name, body = {}, None, None
    for line_start, stop, line in _lines(raw, start, end):
        _require(stop - start <= MAX_HEADER_BYTES, "header_size_limit")
        _require(len(line) <= MAX_HEADER_LINE, "header_line_limit")
        _require(stop > line_start and raw[stop - 1:stop] == b"\n",
                 "invalid_header_framing")
        _require(all(byte == 9 or 32 <= byte <= 126 for byte in line),
                 "invalid_header_bytes")
        if not line:
            body = stop
            break
        if line[:1] in (b" ", b"\t"):
            _require(name is not None, "orphan_header_fold")
            values[name][-1] += b" " + line.lstrip(b" \t")
            continue
        field_name, colon, value = line.partition(b":")
        _require(bool(colon) and _FIELD.fullmatch(field_name) is not None,
                 "invalid_header_field")
        name = field_name.decode("ascii").lower()
        values.setdefault(name, []).append(value)
    _require(body is not None, "missing_header_separator")
    for name in _STRUCTURAL:
        _require(len(values.get(name, [])) <= 1, "duplicate_structural_header")
    normalized = {key: entries[0].decode("ascii").strip() for key, entries in values.items()}
    ctype, params = _content_type(normalized.get("content-type"), default_type)
    cte = normalized.get("content-transfer-encoding", "7bit").lower()
    _require(re.fullmatch(_TOKEN, cte) is not None, "invalid_transfer_encoding")
    _require(cte in _KNOWN_CTE, "unsupported_transfer_encoding")
    if "mime-version" in normalized:
        _require(normalized["mime-version"] == "1.0", "invalid_mime_version")
    boundary = None
    if ctype.startswith("multipart/"):
        value = params.get("boundary")
        _require(value is not None and _BOUNDARY.fullmatch(value) is not None
                 and not value.endswith(" "), "invalid_boundary")
        boundary = value.encode("ascii")
    else:
        _require("boundary" not in params, "unexpected_boundary_parameter")
    message = BytesHeaderParser(policy=policy.default).parsebytes(raw[start:body])
    message.set_default_type(default_type)
    # Header-only parsing intentionally has no multipart body. Stdlib reports
    # this invariant even when these validated multipart headers are valid.
    # All other defects remain fatal; full-body mapping below has no exception.
    defects = [defect for defect in message.defects
               if not (boundary is not None and
                       isinstance(defect, MultipartInvariantViolationDefect))]
    _require(not defects and
             not any(header.defects for _, header in message.items()), "header_parse_defect")
    _require(message.get_content_type() == ctype, "header_mapping_mismatch")
    if boundary is not None:
        _require(message.get_boundary() == boundary.decode("ascii"),
                 "header_mapping_mismatch")
    return _Node(start, end, body, locator, message, ctype, cte, boundary)


def _child_ranges(raw, node, max_parts):
    marker = b"--" + node.boundary
    child_start, closed, ranges = None, False, []
    for start, stop, line in _lines(raw, node.body, node.end):
        if not line.startswith(marker):
            continue
        suffix = line[len(marker):]
        closing = suffix.startswith(b"--")
        if closing:
            suffix = suffix[2:]
        _require(not suffix.strip(b" \t"), "ambiguous_boundary_line")
        _require(not closed, "boundary_after_close")
        if child_start is not None:
            _require(start > child_start and raw[start - 1:start] == b"\n",
                     "invalid_boundary_framing")
            child_end = start - 1
            if child_end > child_start and raw[child_end - 1:child_end] == b"\r":
                child_end -= 1
            _require(child_end > child_start, "empty_mime_part")
            ranges.append((child_start, child_end))
            _require(len(ranges) < max_parts, "part_limit")
        if closing:
            _require(child_start is not None, "missing_open_boundary")
            closed = True
            child_start = None
        else:
            _require(raw[stop - 1:stop] == b"\n", "invalid_boundary_framing")
            child_start = stop
    _require(closed and ranges, "missing_close_boundary")
    return ranges


def _wire_tree(raw, max_depth, max_parts):
    nodes, boundaries = [], set()

    def visit(start, end, locator, depth, default_type):
        _require(depth <= max_depth, "depth_limit")
        _require(len(nodes) < max_parts, "part_limit")
        node = _headers(raw, start, end, locator, default_type)
        nodes.append(node)
        if node.ctype == "message/rfc822":
            _require(node.cte in _IDENTITY, "unsupported_transfer_encoding")
            # Validate only the encapsulated header block, not its MIME body.
            embedded = _headers(raw, node.body, node.end, locator)
            _require(any(embedded.headers[name] is not None
                         for name in ("From", "Subject", "Date")),
                     "missing_encapsulated_message_header")
        elif node.boundary is not None:
            _require(node.cte in _IDENTITY, "unsupported_transfer_encoding")
            _require(not any(node.boundary.startswith(old) or old.startswith(node.boundary)
                             for old in boundaries), "duplicate_or_prefix_boundary")
            boundaries.add(node.boundary)
            child_type = "message/rfc822" if node.ctype == "multipart/digest" else "text/plain"
            for number, (child_start, child_end) in enumerate(
                    _child_ranges(raw, node, max_parts), 1):
                node.children.append(visit(child_start, child_end,
                                           locator + "." + str(number), depth + 1, child_type))
        else:
            _require(not node.ctype.startswith("message/"), "unsupported_message_type")
        return node

    root = visit(0, len(raw), "1", 1, "text/plain")
    return root, nodes


def _map_stdlib(raw, root, nodes):
    # Replace RFC822 bodies only in a comparison copy. The original is immutable.
    # This prevents stdlib from recursing into an arbitrarily deep opaque EML.
    chunks, cursor = [], 0
    for node in nodes:
        if node.ctype == "message/rfc822":
            chunks.extend((raw[cursor:node.body], _OPAQUE))
            cursor = node.end
    chunks.append(raw[cursor:])
    parsed = BytesParser(policy=policy.default).parsebytes(b"".join(chunks))
    stack = [(root, parsed)]
    while stack:
        node, message = stack.pop()
        _require(not message.defects and
                 message.get_content_type() == node.ctype and
                 list(message.raw_items()) == list(node.headers.raw_items()),
                 "stdlib_mapping_mismatch")
        if node.ctype == "message/rfc822":
            # Synthetic child is not a source locator and is never traversed.
            _require(message.is_multipart() and len(message.get_payload()) == 1,
                     "stdlib_mapping_mismatch")
        elif node.children:
            _require(message.is_multipart(), "stdlib_mapping_mismatch")
            children = list(message.iter_parts())
            _require(len(children) == len(node.children), "stdlib_mapping_mismatch")
            stack.extend(zip(node.children, children))
        else:
            _require(not message.is_multipart(), "stdlib_mapping_mismatch")


def capture_rfc822_payload(raw_bytes, locator, *, max_bytes=MAX_BYTES,
                           max_depth=MAX_DEPTH, max_parts=MAX_PARTS):
    """Return exact identity-decoded encapsulated EML bytes at a canonical locator.

    Root is ``1``; ordered multipart children are ``1.1``, ``1.2``, etc.
    RFC822 nodes are opaque, so locators below them are not listed. Limits may
    only be lowered. Invalid, ambiguous, or unsupported input raises a safe
    ``WireRFC822Error`` code. This is not attachment inventory or integration.
    """
    for value, ceiling in ((max_bytes, MAX_BYTES), (max_depth, MAX_DEPTH),
                           (max_parts, MAX_PARTS)):
        _require(type(value) is int and 1 <= value <= ceiling, "invalid_limit")
    _require(type(raw_bytes) is bytes, "invalid_raw_bytes")
    _require(0 < len(raw_bytes) <= max_bytes, "byte_limit")
    _require(isinstance(locator, str) and len(locator) <= 256 and
             re.fullmatch(r"1(?:\.[1-9][0-9]{0,8})*", locator) is not None,
             "invalid_locator")
    try:
        root, nodes = _wire_tree(raw_bytes, max_depth, max_parts)
        _map_stdlib(raw_bytes, root, nodes)
    except WireRFC822Error:
        raise
    except (ValueError, TypeError, IndexError, RecursionError, UnicodeError):
        raise WireRFC822Error("invalid_mime") from None
    selected = next((node for node in nodes if node.locator == locator), None)
    _require(selected is not None, "unlisted_locator")
    _require(selected.ctype == "message/rfc822", "not_rfc822")
    return raw_bytes[selected.body:selected.end]
