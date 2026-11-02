"""`ingest --zip`: one production archive and its regular members stored by hash, never extracted to disk.

The archive bytes are read once (no link followed) and stored first. Each acceptable member is read from those
bytes in memory with a hard output cap, stored with its own receipt named `<archive>/<member path>`, and linked
to the archive by one ledger `contains` row. Nested archives are stored as ordinary members and never opened.
Members that are not stored are listed by skip code; nothing is ever written outside private/objects."""
import bz2
import hashlib
import io
import json
import lzma
import os
import re
import stat
import struct
import zipfile
import zlib

from .cli import MAX_BYTES, MAX_DIR_FILES, _ledger_lookup, _receipt_id, _store_bytes, database

MAX_ARCHIVE_BYTES = 200 * 1024 * 1024
MAX_MEMBERS = MAX_DIR_FILES  # 2000 entries, directories included
MAX_RATIO = 100
MAX_NAME = 255  # receipt original_name, archive basename included
SKIP_CODES = ("directory", "unsafe_name", "duplicate_name", "encrypted", "symlink", "too_large", "ratio",
              "unreadable")
_DRIVE = re.compile(r"[A-Za-z]:")
_LOCAL_HEADER = struct.Struct("<4s22xHH")  # signature, then file name and extra field lengths


def _read_archive(path):
    """The archive bytes: one regular, non-symlink file of at most 200 MiB."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise ValueError("--zip must be a regular, non-symlink file") from None
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("--zip must be a regular, non-symlink file")
        data = handle.read(MAX_ARCHIVE_BYTES + 1)
    if len(data) > MAX_ARCHIVE_BYTES:
        raise ValueError("Alpha limit: 200 MiB per archive; nothing was imported")
    return data


def _member_path(info):
    """Forward-slash member path, or None for an absolute, drive-letter, NUL, empty, `.` or `..` name.

    Backslashes count as separators, so `a\\..\\..\\b` is caught as traversal too."""
    raw = info.orig_filename  # zipfile truncates `filename` at NUL; the original keeps it
    path = raw.replace("\\", "/")
    if "\0" in raw or _DRIVE.match(path) or any(part in ("", ".", "..") for part in path.split("/")):
        return None
    return path


def _precheck(info, path, receipt_name, seen):
    """Skip code from the central directory alone (nothing decompressed), or None."""
    if info.is_dir():
        return "directory"
    if path is None or len(receipt_name) > MAX_NAME:
        return "unsafe_name"
    if path in seen:
        return "duplicate_name"
    seen.add(path)
    if info.flag_bits & 0x1:
        return "encrypted"
    if stat.S_ISLNK(info.external_attr >> 16):
        return "symlink"
    if info.file_size > MAX_BYTES:
        return "too_large"
    if info.file_size > MAX_RATIO * max(info.compress_size, 1):
        return "ratio"
    return None


def _lzma_decoder(raw, cap):
    """(raw LZMA1 decoder, compressed stream) from a zip LZMA member: version(2) props size(2) props(5) stream.

    The dictionary is clamped to the output cap (no back-reference can reach further) so a forged 4 GiB
    dictionary is never allocated."""
    _version, props_size = struct.unpack_from("<HH", raw)
    if props_size != 5:
        raise ValueError("unexpected LZMA properties size")
    first, dict_size = struct.unpack_from("<BI", raw, 4)
    if first >= 9 * 5 * 5:
        raise ValueError("bad LZMA properties")
    lc, rest = first % 9, first // 9
    lzma_filter = {"id": lzma.FILTER_LZMA1, "lc": lc, "lp": rest % 5, "pb": rest // 5,
                   "dict_size": max(4096, min(dict_size, cap))}
    return lzma.LZMADecompressor(lzma.FORMAT_RAW, filters=[lzma_filter]), raw[4 + props_size:]


def _member_bytes(data, info):
    """(bytes, None) or (None, skip code). Output stops at MAX_BYTES + 1 whatever the header claims.

    Every method is decoded here from the member's raw bytes (zipfile's own reader trims bzip2/lzma output to
    the header size, which would hide a longer real stream). The decoded length, ratio and CRC are therefore
    measured on what the stream actually holds, and a stream that does not reach its end marker is unreadable."""
    cap = MAX_BYTES + 1
    try:
        signature, name_length, extra_length = _LOCAL_HEADER.unpack_from(data, info.header_offset)
        start = info.header_offset + _LOCAL_HEADER.size + name_length + extra_length
        raw = memoryview(data)[start:start + info.compress_size]
        if signature != b"PK\x03\x04" or len(raw) != info.compress_size:
            return None, "unreadable"
        if info.compress_type == zipfile.ZIP_STORED:
            out, complete = raw[:cap], True
        else:
            if info.compress_type == zipfile.ZIP_DEFLATED:
                decoder, stream = zlib.decompressobj(-zlib.MAX_WBITS), raw
            elif info.compress_type == zipfile.ZIP_BZIP2:
                decoder, stream = bz2.BZ2Decompressor(), raw
            elif info.compress_type == zipfile.ZIP_LZMA:
                decoder, stream = _lzma_decoder(raw, cap)
            else:
                return None, "unreadable"
            out = decoder.decompress(stream, cap)
            complete = decoder.eof
    except (EOFError, OSError, ValueError, MemoryError, lzma.LZMAError, zlib.error, struct.error):
        return None, "unreadable"
    if len(out) > MAX_BYTES:
        return None, "too_large"
    if len(out) > MAX_RATIO * max(info.compress_size, 1):
        return None, "ratio"
    if not complete or len(out) != info.file_size or zlib.crc32(out) != info.CRC:
        return None, "unreadable"
    return bytes(out), None


def _open_archive(data, name):
    """Parse the central directory and refuse before any write: not a zip, or more than 2000 entries."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError, OSError, ValueError):
        raise ValueError("not a zip archive") from None
    if len(archive.infolist()) > MAX_MEMBERS:
        archive.close()
        raise ValueError(f"{name}: more than {MAX_MEMBERS} entries; nothing was imported")
    return archive


def _previewer(known, source_id):
    """Dry-run stand-in for _store_bytes: outcome would_store/present from the read-only ledger copy."""
    def preview(blob, receipt_name, contained_in=None):
        digest = hashlib.sha256(blob).hexdigest()
        identity = _receipt_id(source_id, digest)
        return {"sha256": digest, "receipt_id": identity,
                "outcome": "present" if known(digest, identity)[0] else "would_store"}
    return preview


def ingest_zip(args, root):
    """Store the archive, then each acceptable member; print the listing (dry run: write nothing)."""
    data = _read_archive(args.zip)
    name = os.path.basename(os.path.abspath(args.zip))
    dry = getattr(args, "dry_run", False)
    with _open_archive(data, name) as archive:
        if dry:
            known, close = _ledger_lookup(root)
            store = _previewer(known, args.source_id)
        else:
            db = database(root)
            close = db.close

            def store(blob, receipt_name, contained_in=None):
                return _store_bytes(db, root, blob, args.source_id, receipt_name, contained_in)
        try:
            container = store(data, name)
            members, skipped, seen = [], [], set()
            for info in archive.infolist():
                path = _member_path(info)
                receipt_name = f"{name}/{path}"
                code = _precheck(info, path, receipt_name, seen)
                blob = None
                if code is None:
                    blob, code = _member_bytes(data, info)
                if code:
                    skipped.append({"path": info.filename, "code": code})
                    continue
                result = store(blob, receipt_name, (container["sha256"], path))
                members.append({"path": path, "sha256": result["sha256"], "outcome": result["outcome"]})
        finally:
            close()
    outcomes = [container["outcome"]] + [m["outcome"] for m in members]
    stored_key = "would_store" if dry else "stored"
    report = {"archive": {k: container[k] for k in ("sha256", "receipt_id", "outcome")}, "members": members,
              "skipped": skipped, stored_key: outcomes.count(stored_key), "present": outcomes.count("present")}
    print(json.dumps({**report, **({"dry_run": True} if dry else {})}, indent=2))
    if dry:
        print("Dry run: nothing was written.")
