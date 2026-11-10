"""Image metadata in public assets: name the kinds a JPEG/PNG/WebP carries; strip them without re-encoding.

Stdlib only and bounded (``MAX_BYTES`` per file, ``MAX_PARTS`` segments/chunks). Only kind names ever leave this
module, never a metadata value. A truncated or malformed structure is ``unreadable image`` and is refused too.
"""
import errno
import os
import struct
import tempfile
import zlib
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


def _png_chunk(data, pos, size, count):
    """Frame, type-check and CRC-check one chunk at ``pos``: ``(name, body_start, end)``; else ``Unreadable``."""
    _need(count < MAX_PARTS and pos + 12 <= size)
    length, name = int.from_bytes(data[pos:pos + 4], "big"), data[pos + 4:pos + 8]
    end = pos + 12 + length
    _need(length <= 2 ** 31 - 1 and end <= size)
    _need(all(65 <= c <= 90 or 97 <= c <= 122 for c in name) and name[2] < 97)  # letters; reserved bit clear
    _need(zlib.crc32(data[pos + 4:pos + 8 + length]) == int.from_bytes(data[end - 4:end], "big"))
    return name, pos + 8, end


def _png(data):
    _need(data.startswith(PNG_SIGNATURE))
    parts, pos, size = [(None, 0, 8)], 8, len(data)
    seen, previous = [], None
    while True:
        name, body_start, end = _png_chunk(data, pos, size, len(seen))
        length = end - 12 - pos
        if not seen:
            _need(name == b"IHDR" and length == 13)
        else:
            _need(name != b"IHDR")
        if name == b"PLTE":
            _need(b"PLTE" not in seen and b"IDAT" not in seen and length % 3 == 0 and 3 <= length <= 768)
        elif name == b"IDAT":
            _need(previous == b"IDAT" or b"IDAT" not in seen)  # one contiguous run
        elif name == b"IEND":
            _need(length == 0 and b"IDAT" in seen)
        elif name[0] < 97:  # critical, not one of the four known
            _need(name == b"IHDR")
        body, kind = data[body_start:end - 4], None
        if name == b"eXIf":
            kind = _exif_kind(body)
        elif name == b"iTXt":
            kind = "XMP" if body.split(b"\0", 1)[0] == b"XML:com.adobe.xmp" else "text"
        elif name in (b"tEXt", b"zTXt"):
            kind = "text"
        parts.append((kind, pos, end))
        seen.append(name)
        previous, pos = name, end
        if name == b"IEND":
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


MAX_SIDE = 20_000
_NOT_SOF = (0xC4, 0xC8, 0xCC)  # DHT, JPG, DAC share the 0xC0-0xCF range but carry no frame size


def _size(data, image):
    if image == "png":
        start, end = _png(data)[1][1:]
        _need(data[start + 4:start + 8] == b"IHDR" and end - start == 25)
        return struct.unpack(">II", data[start + 8:start + 16])
    if image == "jpeg":
        for kind, start, end in _jpeg(data)[1:]:
            if kind == "trailing data":  # bytes after EOI are not the image
                break
            pos = start
            while data[pos] == 0xFF:
                pos += 1
            if 0xC0 <= data[pos] <= 0xCF and data[pos] not in _NOT_SOF:
                _need(end >= pos + 8)
                height, width = struct.unpack(">HH", data[pos + 4:pos + 8])
                return width, height
        raise Unreadable()
    parts = _webp(data)
    _need(len(parts) > 1)
    start, end = parts[1][1:]
    fourcc, body = data[start:start + 4], data[start + 8:end]
    if fourcc == b"VP8 ":
        _need(len(body) >= 10 and body[3:6] == b"\x9d\x01\x2a")
        return int.from_bytes(body[6:8], "little") & 0x3FFF, int.from_bytes(body[8:10], "little") & 0x3FFF
    if fourcc == b"VP8L":
        _need(len(body) >= 5 and body[0] == 0x2F)
        bits = int.from_bytes(body[1:5], "little")
        return 1 + (bits & 0x3FFF), 1 + ((bits >> 14) & 0x3FFF)
    _need(fourcc == b"VP8X" and len(body) >= 10)
    return 1 + int.from_bytes(body[4:7], "little"), 1 + int.from_bytes(body[7:10], "little")


def image_size(data, suffix):
    """``(width, height)`` of a PNG/JPEG/WebP, else ``None`` (other suffix, malformed, 0 or over ``MAX_SIDE`` px)."""
    image = FORMATS.get(suffix.lower())
    if image is None or len(data) > MAX_BYTES:
        return None
    try:
        width, height = _size(data, image)
    except (Unreadable, IndexError, struct.error):
        return None
    return (width, height) if 0 < width <= MAX_SIDE and 0 < height <= MAX_SIDE else None


def check_asset(path, relative, limit=MAX_BYTES):
    """Refuse a public asset carrying image metadata; symlinks and oversized files are left to the copy check."""
    if path.suffix.lower() not in FORMATS or path.is_symlink() or path.stat().st_size > limit:
        return
    kinds = metadata_kinds(path.read_bytes(), path.suffix)
    if kinds:
        raise ContentError(f"public-assets/{relative}: image metadata ({', '.join(kinds)}); "
                           "run content strip-metadata")


def strip_file(source, out=None):
    """Write a cleaned copy of ``source`` (default ``<stem>.clean<suffix>`` beside it); never overwrites.

    The copy appears under its final name only once it is completely written, synced and closed, so a failed write
    leaves no partial file and the same command can be retried. A staging directory may survive an abrupt exit.
    """
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
    # RF1925-10: stage the cleaned bytes in a private 0700 sibling directory, write, fsync and close them, then publish
    # with a no-replace hard link. A failure before the link leaves no file under the requested name, so a retry works.
    with tempfile.TemporaryDirectory(prefix=".strip-metadata-", dir=target.parent) as stage:
        payload = os.path.join(stage, "payload")
        descriptor = os.open(payload, flags, 0o644)
        try:
            view = memoryview(cleaned)
            while len(view):
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError(errno.EIO, "short write while staging the cleaned copy")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            os.link(payload, target)  # never replaces: a regular file, symlink or directory that arrived first wins
        except FileExistsError:
            raise ValueError(f"{target}: already exists; nothing written") from None
    return {"removed": kinds, "written": str(target)}
