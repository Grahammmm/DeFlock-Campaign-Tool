"""PDF and SVG public assets: name the active, hidden or revealing kinds a file carries; never a value.

Stdlib only and bounded: input <= ``MAX_BYTES``; at most ``MAX_STREAMS`` Flate streams inflated, each to at most
``MAX_INFLATE`` bytes (``decompressobj().decompress(data, max_length)``, so memory stays bounded). Anything malformed
or over a bound, and any stream encoded other than Flate-only or image-only, is ``unreadable PDF`` / ``unreadable SVG``
and is refused too. Only kind names leave this module.
"""
import re
import zlib
from xml.etree import ElementTree

from .content import ContentError

MAX_BYTES = 5 * 1024 * 1024  # equals site.ASSET_MAX_BYTES
MAX_STREAMS = 2_000
MAX_INFLATE = 8 * 1024 * 1024
PDF_UNREADABLE, SVG_UNREADABLE = "unreadable PDF", "unreadable SVG"
PDF_NAMES = {b"JavaScript": "JavaScript", b"JS": "JavaScript", b"OpenAction": "actions", b"Launch": "actions",
             b"AA": "actions", b"EmbeddedFile": "embedded files", b"EmbeddedFiles": "embedded files",
             b"RichMedia": "embedded files", b"Author": "author metadata"}
PDF_NAME = re.compile(rb"/([^\x00\t\n\x0c\r ()<>\[\]{}/%]+)")
PDF_HEX = re.compile(rb"#([0-9A-Fa-f]{2})")
XMP_AUTHOR = re.compile(rb"<(?:pdf:Author|dc:creator)\b|\bpdf:Author\s*=")
STREAM = re.compile(rb"(?<![A-Za-z])stream\r?\n")
# whitespace and comments; a comment runs to end of line (the lookahead stops ``%%%...`` backtracking)
PDF_GAP = rb"(?:[\x00\t\n\x0c\r ]|%[^\r\n]*(?=[\r\n]|\Z))*"
FILTER = re.compile(rb"/Filter(?![^\x00\t\n\x0c\r ()<>\[\]{}/%])" + PDF_GAP)
FILTER_ARRAY = re.compile(rb"\[(" + PDF_GAP + rb"(?:/[^\x00\t\n\x0c\r ()<>\[\]{}/%]+" + PDF_GAP + rb")*)\]")
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


def _filters(header):
    """Each ``/Filter`` value in a stream header as a list of names; ``Unreadable`` if it is not a name or array."""
    for match in FILTER.finditer(header):
        at = match.end()
        array = FILTER_ARRAY.match(header, at)
        if array:
            names = PDF_NAME.findall(re.sub(rb"%[^\r\n]*", b"", array[1]))
        else:
            name = PDF_NAME.match(header, at)
            if not name:
                raise Unreadable()  # an indirect reference or a malformed name: cannot tell what encodes it
            names = [name[1]]
        yield names


def _is_flate(header):
    """True for a Flate-only stream, False for none or image-only; other encodings could hide names: ``Unreadable``."""
    flate = False
    for names in _filters(header):
        if len(names) == 1 and names[0] in FLATE:
            flate = True
        elif not set(names) <= IMAGE_FILTERS:
            raise Unreadable()
    return flate


def _inflated(data):
    """Yield each ``/FlateDecode`` stream's inflated bytes; raises ``Unreadable`` past a bound or on bad data."""
    view, count = memoryview(data), 0
    for match in STREAM.finditer(data):
        start = match.end()
        header = _unhex(data[data.rfind(b"obj", 0, match.start()) + 3:match.start()])
        if not _is_flate(header):
            continue
        count += 1
        end = data.find(b"endstream", start)
        if count > MAX_STREAMS or end < 0:
            raise Unreadable()
        inflater = zlib.decompressobj()
        try:
            out = inflater.decompress(view[start:end], MAX_INFLATE)
        except zlib.error:
            raise Unreadable() from None
        if not inflater.eof:  # truncated, or more than MAX_INFLATE bytes (input left in unconsumed_tail)
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
