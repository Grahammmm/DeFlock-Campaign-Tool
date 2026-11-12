"""Deterministic ``data/sources.zip`` (DF0022-11): the public source copies THIS build hosts, plus a safe CSV manifest.

A source-library entry is bundled only when its ``public_url`` points at a file this same build copies from
``content/public-assets`` (the ``asset_paths`` of the build's own copy plan). Nothing is fetched, no private lookup is made
and ``public/`` is never walked. The archive has no timestamps, modes or compression of its own, so two builds of the same
inputs write identical bytes. ``manifest.csv`` columns: ``sha256`` is the library's 12-character RECORD NUMBER (not the full
private-original hash, and a redacted public copy need not match the original), ``title``, ``agency`` (public name, else the
agency id), ``received_at`` unchanged, ``public_path`` (starts ``/``) and ``bytes`` (length of the copied file).
"""
import io
import os
import stat
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

from .content import ContentError

ZIP_PATH = "data/sources.zip"
MANIFEST = "manifest.csv"
MAX_BYTES = 209715200  # 200 MiB, inclusive, for the final archive
COLUMNS = ["sha256", "title", "agency", "received_at", "public_path", "bytes"]
NOTE_EMPTY = "sources.zip skipped: no local public source copies"
NOTE_OVERSIZE = "sources.zip skipped: bundle would exceed 200 MiB"
CHANGED = "sources.zip: public copy changed during build"
DIFFERS = "sources.zip: differs from rebuilt public-source bundle; run build without --check"
_DATE = (1980, 1, 1, 0, 0, 0)


def _relative_path(public_url, base_url):
    """The canonical asset path a library ``public_url`` names under ``base_url``, or None when it is not that exact origin and
    path prefix, has a query, an alias (``%``, dot, empty or backslash segment) or credentials. A fragment is ignored."""
    if not base_url or not isinstance(public_url, str):
        return None
    public_url = public_url.split("#", 1)[0]  # drop the fragment FIRST: its ``%``, ``?`` or ``\`` never name another file
    if "\\" in public_url or "%" in public_url:
        return None
    try:
        url, base = urlsplit(public_url), urlsplit(base_url.rstrip("/"))
        if url.scheme != "https" or base.scheme != "https" or url.query or "?" in public_url or url.username or url.password:
            return None
        if (url.hostname, url.port or 443) != (base.hostname, base.port or 443):
            return None
    except ValueError:
        return None
    prefix = base.path.rstrip("/") + "/"
    if not url.path.startswith(prefix):
        return None
    parts = url.path[len(prefix):].split("/")
    if any(part in ("", ".", "..") for part in parts):
        return None
    return "/".join(parts)


def bundle_selection(sources, base_url, asset_paths):
    """``[(source entry, asset path)]`` for the entries whose ``public_url`` is a copy THIS build makes (``asset_paths``)."""
    paths = set(asset_paths)
    chosen = []
    for source in sources:
        relative = _relative_path(source.get("public_url"), base_url)
        if relative is not None and relative in paths:
            chosen.append((source, relative))
    return chosen


def stored_size(sizes):
    """Exact bytes of the stored archive for member ``{zip name: data length}`` (local + central headers, end record)."""
    return 22 + sum(30 + len(name.encode("utf-8")) + 46 + len(name.encode("utf-8")) + size for name, size in sizes.items())


def manifest_csv(rows, sizes, agencies_by_id):
    """``manifest.csv`` text: the shared findings.csv writer (RFC 4180, CRLF) and formula guard."""
    from .site import _cell, _csv_text, short_hash  # local: site imports this module
    table = []
    for source, relative in rows:
        agency = agencies_by_id.get(source["agency_id"])
        table.append({"sha256": short_hash(source["sha256"]), "title": source["title"],
                      "agency": agency["name"] if agency else source["agency_id"], "received_at": source["received_at"],
                      "public_path": "/" + relative, "bytes": sizes[relative]})
    table.sort(key=lambda r: (r["public_path"], r["sha256"], r["agency"], r["received_at"], r["title"]))
    return _csv_text(COLUMNS, [{key: _cell(value) for key, value in row.items()} for row in table])


def bundle_bytes(rows, payloads, *, agencies_by_id=None, max_bytes=None):
    """The archive bytes for ``rows`` (from ``bundle_selection``) and ``{asset path: bytes}``; ``None`` when it would pass
    ``max_bytes``. The size is computed first, so an oversize archive is never allocated."""
    max_bytes = MAX_BYTES if max_bytes is None else max_bytes
    sizes = {relative: len(payloads[relative]) for _, relative in rows}
    manifest = manifest_csv(rows, sizes, agencies_by_id or {}).encode("utf-8")
    members = {"files/" + relative: payloads[relative] for relative in sizes}
    members[MANIFEST] = manifest
    if stored_size({name: len(data) for name, data in members.items()}) > max_bytes:
        return None
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED, allowZip64=False) as archive:
        for name in sorted(members):
            info = zipfile.ZipInfo(name, _DATE)
            info.compress_type, info.create_system, info.external_attr = zipfile.ZIP_STORED, 3, 0o100644 << 16
            archive.writestr(info, members[name])
    return out.getvalue()


def read_copy(source, expected_size):
    """Bytes of one regular, non-symlink asset copy, bounded to its stat size + 1 (a changed file raises).

    The one ``lstat`` is retained; the file is opened ``O_NOFOLLOW | O_NONBLOCK`` (fail closed when unavailable) and the
    DESCRIPTOR must be a regular file with that inode and ``expected_size`` before any byte is read, and again afterwards.
    Every failure is the fixed ``CHANGED`` refusal without OS text."""
    nofollow, nonblock = getattr(os, "O_NOFOLLOW", 0), getattr(os, "O_NONBLOCK", 0)
    if not nofollow or not nonblock:
        raise ContentError(CHANGED)
    fd = None
    try:
        before = os.lstat(source)
        if not stat.S_ISREG(before.st_mode) or before.st_size != expected_size:
            raise ContentError(CHANGED)
        fd = os.open(source, os.O_RDONLY | nofollow | nonblock)
        identity = (before.st_dev, before.st_ino)

        def admitted():
            info = os.fstat(fd)
            return stat.S_ISREG(info.st_mode) and (info.st_dev, info.st_ino) == identity and info.st_size == expected_size
        if not admitted():
            raise ContentError(CHANGED)
        chunks, remaining = [], expected_size + 1
        while remaining > 0:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) != expected_size or not admitted():
            raise ContentError(CHANGED)
    except OSError:
        raise ContentError(CHANGED) from None
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
    return data


def _admitted_sizes(by_path, rows):
    """``{asset path: size}`` for each UNIQUE selected copy, admitted BEFORE any size shortcut: every one must be a regular,
    non-symlink file by a nonfollowing ``lstat`` (no open, so a FIFO cannot block). Any other entry, or any OS error, is the
    fixed ``CHANGED`` refusal without paths or OS text, so an invalid copy never hides behind an oversize skip. The later
    ``read_copy`` re-inspects independently; this does not bind the two inspections atomically."""
    sizes = {}
    for _, relative in rows:
        if relative in sizes:
            continue
        try:
            info = os.lstat(by_path[relative])
        except OSError:
            raise ContentError(CHANGED) from None
        if not stat.S_ISREG(info.st_mode):
            raise ContentError(CHANGED)
        sizes[relative] = info.st_size
    return sizes


def plan_bundle(sources, base_url, asset_plan, agencies_by_id, notes=None, max_bytes=None):
    """``bytes | None`` for this build: ``asset_plan`` is ``[(source file, relative Path)]``. ``None`` (with one note) when
    nothing is eligible or the archive would pass ``max_bytes``; oversize archives are refused from file sizes alone."""
    max_bytes = MAX_BYTES if max_bytes is None else max_bytes
    by_path = {Path(relative).as_posix(): Path(source) for source, relative in asset_plan}
    rows = bundle_selection(sources, base_url, by_path)
    if not rows:
        _note(notes, NOTE_EMPTY)
        return None
    sizes = _admitted_sizes(by_path, rows)
    if stored_size({"files/" + path: size for path, size in sizes.items()}) > max_bytes:  # the files alone are too big: read nothing
        _note(notes, NOTE_OVERSIZE)
        return None
    payloads = {relative: read_copy(by_path[relative], size) for relative, size in sizes.items()}
    data = bundle_bytes(rows, payloads, agencies_by_id=agencies_by_id, max_bytes=max_bytes)
    if data is None:
        _note(notes, NOTE_OVERSIZE)
    return data


def rebuild_from_staged(sources, base_url, relatives, staged_root, agencies_by_id, expected, max_bytes=None):
    """After copying: rebuild the archive from the staged files and require the planned bytes (``expected`` may be None)."""
    staged = [(Path(staged_root) / relative, relative) for relative in relatives]
    again = plan_bundle(sources, base_url, staged, agencies_by_id, None, max_bytes)
    if again != expected:
        raise ContentError(CHANGED)


def _note(notes, text):
    if isinstance(notes, list):
        notes.append(text)


def check_source_bundle(public, expected):
    """Raise ``ContentError`` unless ``public/data/sources.zip`` is exactly ``expected`` (bytes), or absent when ``expected`` is
    None. A missing ``public/`` is a first build and passes. Reads only; a symlink anywhere on the path is refused."""
    public = Path(public)
    if not os.path.lexists(public):
        return
    current, missing, mode = public, False, 0
    for part in ZIP_PATH.split("/"):
        current = current / part
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            missing = True
            break
        except OSError:
            raise ContentError(DIFFERS) from None
        if stat.S_ISLNK(mode):
            raise ContentError(DIFFERS)
    if expected is None:
        if not missing:
            raise ContentError(DIFFERS)
        return
    if missing or not stat.S_ISREG(mode):
        raise ContentError(DIFFERS)
    # The final component is read through the descriptor-verified, nonfollowing, bounded ``read_copy`` (at most
    # ``len(expected) + 1`` bytes); its fixed refusal is reported here as the checker's own ``DIFFERS``.
    try:
        found = read_copy(current, len(expected))
    except (ContentError, OSError):
        raise ContentError(DIFFERS) from None
    if found != expected:
        raise ContentError(DIFFERS)
