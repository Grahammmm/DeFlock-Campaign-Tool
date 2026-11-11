"""PDF and SVG public assets: name the active, hidden or revealing kinds a file carries; never a value.

Stdlib only and bounded: input <= ``MAX_BYTES``; at most ``MAX_STREAMS`` zlib streams inflated, each to at most
``MAX_INFLATE`` bytes and all to at most ``MAX_INFLATE_TOTAL`` (``decompressobj().decompress(data, max_length)``, so
memory stays bounded). Streams are found by sweeping every raw ``stream`` keyword, not by parsing, so no comment,
string, ``/Length`` or stray ``endstream`` can hide one. Anything malformed or over a bound, and any ``/Filter`` other
than Flate-only or image-only, is ``unreadable PDF`` / ``unreadable SVG`` and is refused too. A Flate stream is also
read as a PDF reader sees it once a ``/DecodeParms`` PNG or TIFF predictor is undone (at most ``MAX_PREDICT_TOTAL``
bytes in all); a ``/DecodeParms`` that is not direct, any other predictor, or a predictor followed by anything but
the rest of a stream dictionary (a string or comment could hold a decoy ``stream``) is unreadable. Only kind names
leave this module. The SVG profile (including why any ``xml:base`` is refused) is in docs/SVG-ASSETS.md.
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
PDF_SPACE = frozenset(b"\x00\t\n\x0c\r ")
PDF_DELIMITERS = frozenset(b"()<>[]{}/%")
# a Flate stream's /DecodeParms (/DP, as some readers also accept) predictor is undone before its names are read
DECODE_PARMS = {b"DecodeParms", b"DP"}
PREDICTOR_KEYS = (b"Predictor", b"Colors", b"BitsPerComponent", b"Columns")
PDF_PARAM_INT = re.compile(rb"([+-]?\d+)(?![\d.])")
PDF_PARAM_REF = re.compile(rb"\d+[\x00\t\n\x0c\r ]+\d+[\x00\t\n\x0c\r ]+R(?![^\x00\t\n\x0c\r ()<>\[\]{}/%])")
PDF_PARAM_OTHER = re.compile(rb"[+-]?\d*\.\d*|true\b|false\b|null\b|/[^\x00\t\n\x0c\r ()<>\[\]{}/%]*")
MAX_PREDICT_TOTAL = MAX_INFLATE  # all predictor-undone bytes together (pure Python, so kept smaller than inflate)
# what may lie between a predictor's /DecodeParms and the ``stream`` keyword it applies to: the rest of a stream
# dictionary (names, numbers, references, booleans, null, arrays, dictionaries, hex strings), ending in ``>>``. A
# literal string, comment or other token there could hold a decoy ``stream`` keyword, so it is not accepted.
PDF_DICT_TAIL = re.compile(rb"(?:[\x00\t\n\x0c\r ]+|/[^\x00\t\n\x0c\r ()<>\[\]{}/%]+|[+-]?(?:\d+\.?\d*|\.\d+)"
                           rb"|R|true|false|null|\[|\]|<<|>>|<[0-9A-Fa-f\x00\t\n\x0c\r ]*>)*+")  # possessive: linear
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


def _params_value(data, at):
    """The ``/DecodeParms`` value at ``at`` as (list of predictor tuples, end); ``Unreadable`` unless it is ``null``,
    a direct dictionary or an array of those. A tuple is (Predictor, Colors, BitsPerComponent, Columns)."""
    at = PDF_GAP.match(data, at).end()
    if data.startswith(b"null", at):
        return [], at + 4
    if data.startswith(b"<<", at):
        params, at = _params_dict(data, at + 2)
        return [params], at
    if data[at:at + 1] != b"[":
        raise Unreadable()  # an indirect reference or a malformed value: cannot tell how the stream is predicted
    found, at = [], at + 1
    while True:
        at = PDF_GAP.match(data, at).end()
        if data[at:at + 1] == b"]":
            return found, at + 1
        if data.startswith(b"null", at):
            at += 4
        elif data.startswith(b"<<", at):
            params, at = _params_dict(data, at + 2)
            found.append(params)
        else:
            raise Unreadable()


def _params_dict(data, at):
    """A direct decode-parameters dictionary from just after ``<<``: the predictor keys must be direct integers, each
    key at most once; other keys (image filters' /K, /JBIG2Globals ...) may be a number, name, boolean or reference."""
    seen = {}
    while True:
        at = PDF_GAP.match(data, at).end()
        if data.startswith(b">>", at):
            break
        key = PDF_NAME.match(data, at)
        if not key:
            raise Unreadable()
        name, at = _unhex(key[1]), PDF_GAP.match(data, key.end()).end()
        if name in seen:
            raise Unreadable()
        reference = PDF_PARAM_REF.match(data, at)
        value = None if reference else PDF_PARAM_INT.match(data, at)
        if value:
            try:
                number = int(value[1])
            except ValueError:  # the interpreter's integer-digit safeguard refused it: an unreadable PDF, not a crash
                raise Unreadable() from None
            seen[name], at = number, value.end()
        elif name in PREDICTOR_KEYS:
            raise Unreadable()  # a reference, real or other value: cannot tell how the stream is predicted
        else:
            other = reference or PDF_PARAM_OTHER.match(data, at)
            if not other:
                raise Unreadable()
            seen[name], at = None, other.end()
    defaults = (1, 1, 8, 1)
    return tuple(seen.get(k, d) for k, d in zip(PREDICTOR_KEYS, defaults)), at + 2


def _unpredict(data, params):
    """``data`` with a PNG (10-15) or TIFF (2, 8-bit) predictor undone as a reader would, ``None`` for predictor 1;
    ``Unreadable`` for any other predictor or parameter out of range."""
    predictor, colors, bits, columns = params
    if predictor == 1:
        return None
    if not (1 <= colors <= 32 and bits in (1, 2, 4, 8, 16) and 1 <= columns <= MAX_INFLATE):
        raise Unreadable()
    pixel, row = max(1, (colors * bits + 7) // 8), (columns * colors * bits + 7) // 8
    if row > MAX_INFLATE:
        raise Unreadable()
    out = bytearray()
    if predictor == 2:
        if bits != 8:
            raise Unreadable()
        for start in range(0, len(data), row):
            line = bytearray(data[start:start + row])
            for i in range(colors, len(line)):
                line[i] = (line[i] + line[i - colors]) & 0xFF
            out += line
        return bytes(out)
    if not 10 <= predictor <= 15:
        raise Unreadable()
    prior = bytes(row)
    for start in range(0, len(data), row + 1):
        kind, line = data[start], bytearray(data[start + 1:start + 1 + row])
        if kind == 1:
            for i in range(pixel, len(line)):
                line[i] = (line[i] + line[i - pixel]) & 0xFF
        elif kind == 2:
            for i in range(len(line)):
                line[i] = (line[i] + prior[i]) & 0xFF
        elif kind == 3:
            for i in range(len(line)):
                left = line[i - pixel] if i >= pixel else 0
                line[i] = (line[i] + ((left + prior[i]) >> 1)) & 0xFF
        elif kind == 4:
            for i in range(len(line)):
                left = line[i - pixel] if i >= pixel else 0
                up, corner = prior[i], prior[i - pixel] if i >= pixel else 0
                guess = left + up - corner
                near_left, near_up, near_corner = abs(guess - left), abs(guess - up), abs(guess - corner)
                if near_left <= near_up and near_left <= near_corner:
                    best = left
                else:
                    best = up if near_up <= near_corner else corner
                line[i] = (line[i] + best) & 0xFF
        elif kind != 0:
            raise Unreadable()
        out += line
        prior = bytes(line) + bytes(row - len(line))
    return bytes(out)


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
    if not _terminated(data, at - len(inflater.unused_data)):
        raise Unreadable()
    return bytes(out)


def _terminated(data, at):
    """True if, from ``at`` (the byte after the compressed member), only PDF whitespace precedes a complete
    ``endstream`` token. Never scans forward: another stream's terminator, a keyword prefix or junk is not it."""
    end = len(data)
    while at < end and data[at] in PDF_SPACE:
        at += 1
    if data[at:at + 9] != b"endstream":
        return False
    at += 9
    return at >= end or data[at] in PDF_SPACE or data[at] in PDF_DELIMITERS


def _dict_tail(data, end, keyword):
    """True if only the rest of a stream dictionary, closed by ``>>``, lies between ``end`` and the ``stream``
    keyword at ``keyword``: no string, comment or stray token in which a decoy keyword could be."""
    tail = data[end:keyword]
    return PDF_DICT_TAIL.fullmatch(tail) is not None and tail.rstrip(b"\x00\t\n\x0c\r ").endswith(b">>")


def _inflated(data):
    """Yield the inflated bytes of every zlib stream after any raw ``stream`` keyword; raises ``Unreadable`` past a
    bound, on bad data, on a ``/Filter`` that is neither Flate-only nor image-only, or on a Flate ``/Filter`` whose
    next stream is not zlib. Nothing is lexed, so no comment, string or stream data can hide a stream from the sweep."""
    view = memoryview(data)
    keywords = [match.span() for match in PDF_STREAM.finditer(data)]
    starts = [end for _, end in keywords]
    predicted = {}  # stream start -> every predictor any /DecodeParms before it (and after the previous one) names
    for match in PDF_NAME.finditer(data):
        key = _unhex(match[1])
        if key == b"Filter":
            names, end = _filter_value(data, match.end())
            if _is_flate(names):
                following = bisect.bisect_left(starts, end)
                if following == len(starts) or not _zlib_header(view, starts[following]):
                    raise Unreadable()
        elif key in DECODE_PARMS:
            found, end = _params_value(data, match.end())
            found = {p for p in found if p[0] != 1}
            following = bisect.bisect_left(starts, end)
            if found and following < len(starts):
                # checked once a stream: a later /DecodeParms' tail lies inside the earlier one, already checked
                if starts[following] not in predicted and not _dict_tail(data, end, keywords[following][0]):
                    raise Unreadable()  # a string, comment or other token before it: which stream is predicted?
                predicted.setdefault(starts[following], set()).update(found)
    count = total = undone = 0
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
        for params in sorted(predicted.get(start, ())):  # also as a reader sees it once the predictor is undone
            undone += len(out)
            if undone > MAX_PREDICT_TOTAL:
                raise Unreadable()
            yield _unpredict(out, params)


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


XML_BASE = "{http://www.w3.org/XML/1998/namespace}base"  # the exact expanded name; plain or custom "base" is not it


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
            if key == XML_BASE:  # any xml:base re-points every #fragment; the supported profile has none
                kinds.add("external reference")
            if name.startswith("on"):
                kinds.add("event handler")
            elif name == "href" and not value.strip().startswith("#"):
                kinds.add("external reference")
            elif name == "attributename" and _local(value.strip()).lower() == "href":
                kinds.add("external reference")  # <set>/<animate> could write a link at run time
            elif _css_external(value):
                kinds.add("external reference")
    return sorted(kinds)


SVG_LENGTH = re.compile(r"\s*(\d+(?:\.\d*)?|\.\d+)\s*(?:px)?\s*", re.IGNORECASE)
SVG_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")


def svg_size(data):
    """(width, height) in whole pixels from the root's numeric ``width``/``height`` (unitless or px), else its
    ``viewBox``; None when neither is readable (percent, em, other units, a bad viewBox). Run after ``svg_kinds``."""
    try:
        root = ElementTree.fromstring(data)
    except (ElementTree.ParseError, ValueError, LookupError):
        return None
    if _local(root.tag).lower() != "svg":
        return None
    sides = [SVG_LENGTH.fullmatch(root.attrib.get(name, "")) for name in ("width", "height")]
    if all(sides):
        values = [float(side[1]) for side in sides]
    else:
        parts = re.split(r"[\s,]+", root.attrib.get("viewBox", "").strip())
        if len(parts) != 4 or not all(SVG_NUMBER.fullmatch(part) for part in parts):
            return None
        values = [float(part) for part in parts[2:]]
    if not all(0 < value < float("inf") for value in values):
        return None
    return round(values[0]), round(values[1])


def check_asset(path, relative, limit=MAX_BYTES):
    """Refuse a public PDF/SVG naming its kinds; symlinks and oversized files are left to the copy check."""
    suffix = path.suffix.lower()
    if suffix not in (".pdf", ".svg") or path.is_symlink() or path.stat().st_size > limit:
        return
    data = path.read_bytes()
    kinds, label = (pdf_kinds(data), "PDF") if suffix == ".pdf" else (svg_kinds(data), "SVG")
    if kinds:
        raise ContentError(f"public-assets/{relative}: {label} ({', '.join(kinds)})")
