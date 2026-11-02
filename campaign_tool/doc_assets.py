"""PDF and SVG public assets: name the active, hidden or revealing kinds a file carries; never a value.

Stdlib only and bounded: input <= ``MAX_BYTES``; at most ``MAX_STREAMS`` zlib streams inflated, each to at most
``MAX_INFLATE`` bytes and all to at most ``MAX_INFLATE_TOTAL`` (``decompressobj().decompress(data, max_length)``, so
memory stays bounded). Streams are found by sweeping every raw ``stream`` keyword, not by parsing, so no comment,
string, ``/Length`` or stray ``endstream`` can hide one. Anything malformed or over a bound, and any ``/Filter`` other
than Flate-only or image-only, is ``unreadable PDF`` / ``unreadable SVG`` and is refused too. Only kind names leave
this module.
"""
import bisect
import re
import zlib
from xml.etree import ElementTree

from .content import ContentError

MAX_BYTES = 5 * 1024 * 1024  # equals site.ASSET_MAX_BYTES
MAX_STREAMS = 2_000
MAX_INFLATE = 8 * 1024 * 1024
MAX_INFLATE_TOTAL = 16 * MAX_INFLATE  # all streams together: bounds the time a 5 MiB file of bombs can take
PDF_UNREADABLE, SVG_UNREADABLE = "unreadable PDF", "unreadable SVG"
PDF_NAMES = {b"JavaScript": "JavaScript", b"JS": "JavaScript", b"OpenAction": "actions", b"Launch": "actions",
             b"AA": "actions", b"EmbeddedFile": "embedded files", b"EmbeddedFiles": "embedded files",
             b"RichMedia": "embedded files", b"Author": "author metadata"}
PDF_NAME = re.compile(rb"/([^\x00\t\n\x0c\r ()<>\[\]{}/%]+)")
PDF_HEX = re.compile(rb"#([0-9A-Fa-f]{2})")
XMP_AUTHOR = re.compile(rb"<(?:pdf:Author|dc:creator)\b|\bpdf:Author\s*=")
# whitespace and comments; a comment runs to end of line (the lookahead stops ``%%%...`` backtracking)
PDF_GAP = re.compile(rb"(?:[\x00\t\n\x0c\r ]|%[^\r\n]*(?=[\r\n]|\Z))*")
# every ``stream`` keyword in the raw bytes (not ``endstream``), with the blanks and end of line a reader skips; it is
# found wherever it is - in a comment, string or other stream's data too - so no parsing can be led astray
PDF_STREAM = re.compile(rb"(?<![^\x00\t\n\x0c\r ()<>\[\]{}/%])stream[\x00\t\x0c ]*(?:\r\n|\n|\r)?")
INFLATE_CHUNK = 64 * 1024
FLATE = {b"FlateDecode", b"Fl"}
IMAGE_FILTERS = {b"DCTDecode", b"DCT", b"JPXDecode", b"CCITTFaxDecode", b"CCF", b"JBIG2Decode"}  # carry no names
SVG_DOCTYPE = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)", re.IGNORECASE)
CSS_ESCAPE = re.compile(r"\\([0-9A-Fa-f]{1,6})\s?|\\(.)", re.DOTALL)
CSS_URL = re.compile(r"url\(\s*['\"]?\s*([^)'\"\s]*)", re.IGNORECASE)


class Unreadable(ValueError):
    pass


def _unhex(name):
    return PDF_HEX.sub(lambda match: bytes([int(match[1], 16)]), name)


def _names(data, kinds):
    for match in PDF_NAME.finditer(data):
        kind = PDF_NAMES.get(_unhex(match[1]))
        if kind:
            kinds.add(kind)
    if XMP_AUTHOR.search(data):
        kinds.add("author metadata")


def _filter_value(data, at):
    """The ``/Filter`` value at ``at`` as (names, end); ``Unreadable`` unless it is a name or an array of names."""
    at = PDF_GAP.match(data, at).end()
    name = PDF_NAME.match(data, at)
    if name:
        return [_unhex(name[1])], name.end()
    if data[at:at + 1] != b"[":
        raise Unreadable()  # an indirect reference or a malformed value: cannot tell what encodes it
    names, at = [], at + 1
    while True:
        at = PDF_GAP.match(data, at).end()
        if data[at:at + 1] == b"]":
            return names, at + 1
        name = PDF_NAME.match(data, at)
        if not name:
            raise Unreadable()
        names.append(_unhex(name[1]))
        at = name.end()


def _is_flate(names):
    """True for a Flate-only ``/Filter`` value, False for image-only; other encodings may hide names: Unreadable."""
    if len(names) == 1 and names[0] in FLATE:
        return True
    if not set(names) <= IMAGE_FILTERS:
        raise Unreadable()
    return False


def _zlib_header(view, at):
    """True if a zlib header (deflate, no preset dictionary, valid check bits) starts at ``at``."""
    head = view[at:at + 2]
    return len(head) == 2 and head[0] & 0x0F == 8 and head[0] >> 4 <= 7 and not head[1] & 0x20 and (
        (head[0] << 8 | head[1]) % 31 == 0)


def _inflate(data, at):
    """Inflate the zlib stream at ``at`` to its end; ``Unreadable`` if corrupt, truncated (no ``endstream`` after it)
    or over ``MAX_INFLATE``. No ``/Length`` or ``endstream`` bounds it: the data ends where the deflate stream says."""
    view, inflater, out = memoryview(data), zlib.decompressobj(), bytearray()
    while not inflater.eof:
        if at >= len(view):
            raise Unreadable()  # truncated
        chunk = view[at:at + INFLATE_CHUNK]
        try:
            out += inflater.decompress(chunk, MAX_INFLATE + 1 - len(out))
        except zlib.error:
            raise Unreadable() from None
        if inflater.unconsumed_tail or len(out) > MAX_INFLATE:
            raise Unreadable()
        at += len(chunk)
    if data.find(b"endstream", at - len(inflater.unused_data)) < 0:
        raise Unreadable()
    return bytes(out)


def _inflated(data):
    """Yield the inflated bytes of every zlib stream after any raw ``stream`` keyword; raises ``Unreadable`` past a
    bound, on bad data, on a ``/Filter`` that is neither Flate-only nor image-only, or on a Flate ``/Filter`` whose
    next stream is not zlib. Nothing is lexed, so no comment, string or stream data can hide a stream from the sweep."""
    view = memoryview(data)
    starts = [match.end() for match in PDF_STREAM.finditer(data)]
    for match in PDF_NAME.finditer(data):
        if _unhex(match[1]) == b"Filter":
            names, end = _filter_value(data, match.end())
            if _is_flate(names):
                following = bisect.bisect_left(starts, end)
                if following == len(starts) or not _zlib_header(view, starts[following]):
                    raise Unreadable()
    count = total = 0
    for start in starts:
        if not _zlib_header(view, start):
            continue  # unfiltered or image data: carries no compressed names
        count += 1
        if count > MAX_STREAMS:
            raise Unreadable()
        out = _inflate(data, start)
        total += len(out)
        if total > MAX_INFLATE_TOTAL:
            raise Unreadable()
        yield out


def pdf_kinds(data):
    """Sorted kinds in a PDF's raw bytes and inflated Flate streams, or ``[unreadable PDF]``."""
    if len(data) > MAX_BYTES or not data.startswith(b"%PDF-"):
        return [PDF_UNREADABLE]
    kinds = set()
    try:
        _names(data, kinds)
        for inflated in _inflated(data):
            _names(inflated, kinds)
    except Unreadable:
        return [PDF_UNREADABLE]
    revisions = data.count(b"%%EOF")
    if revisions == 0:
        return [PDF_UNREADABLE]
    first_end = data.find(b"endobj")
    linearized = first_end >= 0 and re.search(rb"/Linearized\b", _unhex(data[:first_end])) is not None
    if revisions > (2 if linearized else 1):
        kinds.add("more than one revision")
    return sorted(kinds)


def _local(name):
    return name.rsplit("}", 1)[-1].rsplit(":", 1)[-1]


def _css_char(match):
    """A CSS escape's character; zero, surrogates and code points past U+10FFFF become U+FFFD (CSS Syntax 4.3.7)."""
    if not match[1]:
        return match[2]
    code = int(match[1], 16)
    return chr(code) if 0 < code <= 0x10FFFF and not 0xD800 <= code <= 0xDFFF else "\ufffd"


def _css_external(text):
    text = CSS_ESCAPE.sub(_css_char, text)
    if "@import" in text.lower():
        return True
    return any(not target.startswith("#") for target in CSS_URL.findall(text))


def svg_kinds(data):
    """Sorted kinds in an SVG; a DOCTYPE/ENTITY is refused on the bytes, before any parse expands an entity."""
    if len(data) > MAX_BYTES:
        return [SVG_UNREADABLE]
    if SVG_DOCTYPE.search(data):
        return ["DOCTYPE"]
    try:
        data.decode("utf-8")
        root = ElementTree.fromstring(data)
    except (UnicodeDecodeError, ElementTree.ParseError, LookupError, ValueError):
        return [SVG_UNREADABLE]
    kinds = {"external reference"} if re.search(rb"<\?xml-stylesheet", data) else set()
    for element in root.iter():
        tag = _local(element.tag).lower() if isinstance(element.tag, str) else ""
        if tag in ("script", "foreignobject"):
            kinds.add("script" if tag == "script" else "foreignObject")
        if tag == "style" and _css_external(element.text or ""):
            kinds.add("external reference")
        for key, value in element.attrib.items():
            name = _local(key).lower()
            if name.startswith("on"):
                kinds.add("event handler")
            elif name == "href" and not value.strip().startswith("#"):
                kinds.add("external reference")
            elif name == "attributename" and _local(value.strip()).lower() == "href":
                kinds.add("external reference")  # <set>/<animate> could write a link at run time
            elif _css_external(value):
                kinds.add("external reference")
    return sorted(kinds)


def check_asset(path, relative, limit=MAX_BYTES):
    """Refuse a public PDF/SVG naming its kinds; symlinks and oversized files are left to the copy check."""
    suffix = path.suffix.lower()
    if suffix not in (".pdf", ".svg") or path.is_symlink() or path.stat().st_size > limit:
        return
    data = path.read_bytes()
    kinds, label = (pdf_kinds(data), "PDF") if suffix == ".pdf" else (svg_kinds(data), "SVG")
    if kinds:
        raise ContentError(f"public-assets/{relative}: {label} ({', '.join(kinds)})")
