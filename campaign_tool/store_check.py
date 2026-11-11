"""Read-only integrity check of private/objects against private/ledger.sqlite.

The result names only hashes, file names under objects/, sizes and problem
codes. It never reads receipts, so no source id or original name can leak.

Each object is verified through one no-follow, nonblocking regular-file descriptor
with a bounded read; a change seen during the check stops it. Guarantee and limits:
docs/STORE-CHECK.md.
"""
import errno
import hashlib
import os
import re
import sqlite3
import stat

HEX64 = re.compile(r"[0-9a-f]{64}")
CHUNK = 1024 * 1024
CHANGED = "Stored object changed during check; retry with the store idle"
UNREADABLE = "Stored object could not be read; check permissions and retry"
OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)


def _sha256(fd, byte_limit):
    """(hexdigest, bytes_read) from an open descriptor; never requests more than CHUNK or the bytes left of byte_limit."""
    digest, total = hashlib.sha256(), 0
    while total < byte_limit:
        block = os.read(fd, min(CHUNK, byte_limit - total))
        if not block:
            break
        digest.update(block)
        total += len(block)
    return digest.hexdigest(), total


def _signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _ledger_rows(path):
    try:
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        try:
            return db.execute("SELECT sha256, byte_count FROM objects ORDER BY sha256").fetchall()
        finally:
            db.close()
    except sqlite3.Error as exc:
        raise ValueError("ledger unreadable: " + type(exc).__name__) from None


def _check_row(objects, sha, expected):
    if not isinstance(sha, str) or not HEX64.fullmatch(sha):
        return {"sha256": str(sha), "problem": "invalid_ledger_row"}
    # Exact nonnegative int only (no bool/float/text/bytes; never coerced); the bad value is not echoed.
    if type(expected) is not int or expected < 0:
        return {"sha256": sha, "problem": "invalid_ledger_row"}
    path = objects / sha
    try:
        info = os.lstat(path)
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            return {"sha256": sha, "problem": "missing_file"}
        raise ValueError(UNREADABLE) from None
    if not stat.S_ISREG(info.st_mode):
        return {"sha256": sha, "problem": "not_a_regular_file"}
    if info.st_size != expected:
        return {"sha256": sha, "problem": "size_mismatch", "expected": expected,
                "actual": info.st_size}
    try:
        fd = os.open(path, OPEN_FLAGS)
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            return {"sha256": sha, "problem": "missing_file"}
        if exc.errno in (errno.ELOOP, errno.ENXIO):  # replaced by a symlink / socket or device after the lstat
            return {"sha256": sha, "problem": "not_a_regular_file"}
        raise ValueError(UNREADABLE) from None
    try:
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode):
                return {"sha256": sha, "problem": "not_a_regular_file"}
            if opened.st_size != expected:
                return {"sha256": sha, "problem": "size_mismatch", "expected": expected,
                        "actual": opened.st_size}
            digest, count = _sha256(fd, expected + 1)
            closed = os.fstat(fd)
        except OSError:
            raise ValueError(UNREADABLE) from None
        if count != expected or _signature(opened) != _signature(closed):
            raise ValueError(CHANGED)
        try:
            final = os.lstat(path)
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                return {"sha256": sha, "problem": "missing_file"}
            raise ValueError(UNREADABLE) from None
        if not stat.S_ISREG(final.st_mode):
            return {"sha256": sha, "problem": "not_a_regular_file"}
        if _signature(final) != _signature(closed):
            raise ValueError(CHANGED)
        if digest != sha:
            return {"sha256": sha, "problem": "hash_mismatch"}
        return None
    finally:
        os.close(fd)


def check_store(root, rows=None):
    """``rows``: (sha256, byte_count) objects rows from a caller's in-memory ledger copy; the ledger file is then
    never opened (SQLite mode=ro can still create -wal/-shm beside a WAL-mode file)."""
    private = root / "private"
    objects = private / "objects"
    if private.is_symlink() or objects.is_symlink():
        raise ValueError("private/objects must not be a symlink")
    ledger = private / "ledger.sqlite"
    if rows is None:
        rows = _ledger_rows(ledger) if ledger.exists() else []
    rows = list(rows)
    problems, known = [], set()
    for sha, expected in rows:
        known.add(sha)
        problem = _check_row(objects, sha, expected)
        if problem:
            problems.append(problem)
    entries = []
    if objects.is_dir():
        with os.scandir(objects) as listing:
            entries = sorted(entry.name for entry in listing)
    for name in entries:
        if name in known and HEX64.fullmatch(name):
            continue
        if name.startswith(".intake-"):
            code = "leftover_temp"
        elif HEX64.fullmatch(name):
            code = "no_ledger_row"
        else:
            code = "unexpected_entry"
        problems.append({"name": name, "problem": code})
    problems.extend(mode_problems(root))
    return {"checked_objects": len(rows), "problems": problems}


def _lstat(path):
    return os.stat(path, follow_symlinks=False)


def _mode_entries(root):
    """(path, file type, problem) for private dirs/files with a group or other bit; symlinks never followed."""
    private = root / "private"
    names = [".", "objects"] + ["ledger.sqlite" + suffix for suffix in ("", "-journal", "-wal", "-shm")]
    objects = private / "objects"
    if not private.is_symlink() and not objects.is_symlink() and objects.is_dir():
        with os.scandir(objects) as listing:
            names += sorted("objects/" + entry.name for entry in listing)
    found = []
    for name in names:
        try:
            info = _lstat(private / name)
        except (FileNotFoundError, NotADirectoryError):
            continue
        kind = stat.S_IFMT(info.st_mode)
        if kind in (stat.S_IFDIR, stat.S_IFREG) and info.st_mode & 0o077:
            shown = "objects/" + name[8:20] if name.startswith("objects/") else name
            found.append((private / name, kind, {"name": shown, "problem": "loose_mode",
                                                 "mode": "0o%o" % stat.S_IMODE(info.st_mode)}))
    return found


def mode_problems(root):
    return [problem for _, _, problem in _mode_entries(root)]


MODE_FIX_UNSUPPORTED = "Private mode repair needs no-follow descriptor support"
SUBSTITUTED = (errno.ENOENT, errno.ENOTDIR, errno.ELOOP, errno.ENXIO)


def _repair_one(path, kind, expected):
    """True if the admitted inode was tightened; False if the entry was substituted. Other OSErrors propagate."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError as error:
        if error.errno in SUBSTITUTED:
            return False
        raise
    try:
        info = os.fstat(fd)
        if stat.S_IFMT(info.st_mode) != kind or (info.st_dev, info.st_ino) != (expected.st_dev, expected.st_ino):
            return False
        os.fchmod(fd, 0o700 if kind == stat.S_IFDIR else 0o600)
        return True
    finally:
        os.close(fd)


def fix_modes(root):
    """Tighten each loose_mode entry to 0700/0600 through one read-only, no-follow descriptor.

    A fresh lstat must show the same kind; the entry is then opened without following links and
    fchmod'ed only if that descriptor is the same regular file/directory (device and inode) the
    lstat saw. A replaced entry is skipped, never repaired by pathname. Parent directories are
    trusted and stationary; no rollback of earlier repairs. Needs O_NOFOLLOW and fchmod and a
    readable candidate, otherwise it refuses rather than fall back to a pathname chmod.
    """
    if (root / "private").is_symlink() or (root / "private" / "objects").is_symlink():
        raise ValueError("private/objects must not be a symlink")
    if not getattr(os, "O_NOFOLLOW", 0) or getattr(os, "fchmod", None) is None:
        raise ValueError(MODE_FIX_UNSUPPORTED)
    fixed, skipped = [], []
    for path, kind, problem in _mode_entries(root):
        try:
            expected = _lstat(path)
        except OSError:
            expected = None
        same = expected is not None and stat.S_IFMT(expected.st_mode) == kind and _repair_one(path, kind, expected)
        (fixed if same else skipped).append(problem["name"])
    return {"fixed": fixed, "skipped": skipped}
