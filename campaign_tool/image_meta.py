"""Image metadata in public assets: name the kinds a JPEG/PNG/WebP carries; strip them without re-encoding.

Stdlib only and bounded (``MAX_BYTES`` per file, ``MAX_PARTS`` segments/chunks). Only kind names ever leave this
module, never a metadata value. A truncated or malformed structure is ``unreadable image`` and is refused too.
"""
import os
import struct
from pathlib import Path

from .content import ContentError

MAX_BYTES = 5 * 1024 * 1024  # equals site.ASSET_MAX_BYTES
MAX_PARTS = 10_000
UNREADABLE = "unreadable image"
FORMATS = {".jpg": "jpeg", ".jpeg": "jpeg", ".png": "png", ".webp": "webp"}
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
XMP_PREFIXES = (b"http://ns.adobe.com/xap/1.0/\0", b"http://ns.adobe.com/xmp/extension/\0")


class Unreadable(ValueError):
    def __init__(self):
        super().__init__(UNREADABLE)


def _need(condition):
    if not condition:
        raise Unreadable()


def _exif_kind(tiff):
    """``EXIF GPS`` when IFD0 holds the GPS pointer (tag 0x8825), else ``EXIF``."""
    if tiff.startswith(b"Exif\0\0"):  # some WebP writers keep the JPEG prefix
        tiff = tiff[6:]
    _need(len(tiff) >= 8 and tiff[:4] in (b"II*\0", b"MM\0*"))
    order = "<" if tiff[:2] == b"II" else ">"
    offset = struct.unpack(order + "I", tiff[4:8])[0]
    _need(8 <= offset <= len(tiff) - 2)
    count = struct.unpack(order + "H", tiff[offset:offset + 2])[0]
    _need(offset + 2 + 12 * count <= len(tiff))
    tags = {struct.unpack(order + "H", tiff[at:at + 2])[0] for at in range(offset + 2, offset + 2 + 12 * count, 12)}
    return "EXIF GPS" if 0x8825 in tags else "EXIF"


def _scan_end(data, pos):
    """End of entropy-coded data: the next 0xFF that is not stuffing (FF00) or a restart marker (FFD0-FFD7)."""
    while True:
        pos = data.find(b"\xff", pos)
        _need(0 <= pos < len(data) - 1)
        following = data[pos + 1]
        if following != 0 and not 0xD0 <= following <= 0xD7:
            return pos
        pos += 2


def _jpeg(data):
    _need(data[:2] == b"\xff\xd8")
    parts, pos, size = [(None, 0, 2)], 2, len(data)
    while True:
        _need(len(parts) <= MAX_PARTS)
        start = pos
        while pos < size and data[pos] == 0xFF:  # the marker's 0xFF plus any fill bytes
            pos += 1
        _need(start < pos < size)
        marker, pos = data[pos], pos + 1
        if marker == 0xD9:  # EOI: whatever follows is not the image
            parts.append((None, start, pos))
            return parts + ([("trailing data", pos, size)] if pos < size else [])
        if 0xD0 <= marker <= 0xD7 or marker == 0x01:
            parts.append((None, start, pos))
            continue
        _need(marker != 0 and pos + 2 <= size)
        end = pos + int.from_bytes(data[pos:pos + 2], "big")
        _need(pos + 2 <= end <= size)
        body, kind = data[pos + 2:end], None
        if marker == 0xE1 and body.startswith(b"Exif\0\0"):
            kind = _exif_kind(body[6:])
        elif marker == 0xE1 and body.startswith(XMP_PREFIXES):
            kind = "XMP"
        elif marker == 0xED:
            kind = "IPTC"
        elif marker == 0xFE:
            kind = "comment"
        elif marker == 0xDA:
            end = _scan_end(data, end)
        parts.append((kind, start, end))
        pos = end


def _png(data):
    _need(data.startswith(PNG_SIGNATURE))
    parts, pos, size = [(None, 0, 8)], 8, len(data)
    while True:
        _need(len(parts) <= MAX_PARTS and pos + 12 <= size)
        length, kind_name = int.from_bytes(data[pos:pos + 4], "big"), data[pos + 4:pos + 8]
        end = pos + 12 + length
        _need(length < 2 ** 31 and end <= size)
        body, kind = data[pos + 8:pos + 8 + length], None
        if kind_name == b"eXIf":
            kind = _exif_kind(body)
        elif kind_name == b"iTXt":
            kind = "XMP" if body.split(b"\0", 1)[0] == b"XML:com.adobe.xmp" else "text"
        elif kind_name in (b"tEXt", b"zTXt"):
            kind = "text"
        parts.append((kind, pos, end))
        pos = end
        if kind_name == b"IEND":
            return parts + ([("trailing data", pos, size)] if pos < size else [])


def _webp(data):
    _need(len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP")
    stop = 8 + int.from_bytes(data[4:8], "little")
    _need(12 <= stop <= len(data))
    parts, pos = [(None, 0, 12)], 12
    while pos < stop:
        _need(len(parts) <= MAX_PARTS and pos + 8 <= stop)
        fourcc, length = data[pos:pos + 4], int.from_bytes(data[pos + 4:pos + 8], "little")
        end = pos + 8 + length + (length & 1)
        _need(end <= stop)
        kind = _exif_kind(data[pos + 8:pos + 8 + length]) if fourcc == b"EXIF" else "XMP" if fourcc == b"XMP " else None
        parts.append((kind, pos, end))
        pos = end
    return parts + ([("trailing data", stop, len(data))] if stop < len(data) else [])


def _parts(data, suffix):
    image = FORMATS.get(suffix.lower())
    if image is None:
        return []
    _need(len(data) <= MAX_BYTES)
    try:
        return {"jpeg": _jpeg, "png": _png, "webp": _webp}[image](data)
    except (IndexError, struct.error):
        raise Unreadable() from None


def metadata_kinds(data, suffix):
    """Sorted metadata kinds in ``data`` (an image named ``*suffix``); ``[]`` for other suffixes."""
    try:
        return sorted({kind for kind, _, _ in _parts(data, suffix) if kind})
    except Unreadable:
        return [UNREADABLE]


def strip(data, suffix):
    """``data`` with every metadata segment/chunk dropped and all other bytes kept; raises ``Unreadable``."""
    if suffix.lower() not in FORMATS:
        raise ValueError(f"{suffix}: only {', '.join(sorted(FORMATS))} images are stripped")
    out = bytearray().join(data[start:end] for kind, start, end in _parts(data, suffix) if not kind)
    if FORMATS[suffix.lower()] == "webp":
        out[4:8] = (len(out) - 8).to_bytes(4, "little")
        if out[12:16] == b"VP8X" and len(out) > 20:
            out[20] &= ~0x0C & 0xFF  # VP8X EXIF (0x08) and XMP (0x04) flags
    return bytes(out)


def check_asset(path, relative, limit=MAX_BYTES):
    """Refuse a public asset carrying image metadata; symlinks and oversized files are left to the copy check."""
    if path.suffix.lower() not in FORMATS or path.is_symlink() or path.stat().st_size > limit:
        return
    kinds = metadata_kinds(path.read_bytes(), path.suffix)
    if kinds:
        raise ContentError(f"public-assets/{relative}: image metadata ({', '.join(kinds)}); "
                           "run content strip-metadata")


def strip_file(source, out=None):
    """Write a cleaned copy of ``source`` (default ``<stem>.clean<suffix>`` beside it); never overwrites."""
    source = Path(source)
    if source.suffix.lower() not in FORMATS:
        raise ValueError(f"{source.name}: only {', '.join(sorted(FORMATS))} images are stripped; nothing written")
    target = Path(out) if out else source.with_name(f"{source.stem}.clean{source.suffix}")
    if source.is_symlink():
        raise ValueError(f"{source}: is a symlink; nothing written")
    if target.is_symlink() or target.exists():
        raise ValueError(f"{target}: already exists; nothing written")
    if "public" in target.resolve().parent.parts:
        raise ValueError(f"{target}: is inside a public/ folder; write the copy elsewhere; nothing written")
    handle = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(handle, "rb") as stream:
        data = stream.read(MAX_BYTES + 1)
    kinds, cleaned = metadata_kinds(data, source.suffix), strip(data, source.suffix)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(target, flags, 0o644), "wb") as stream:
        stream.write(cleaned)
    return {"removed": kinds, "written": str(target)}
