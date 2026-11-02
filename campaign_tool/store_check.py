"""Read-only integrity check of private/objects against private/ledger.sqlite.

The result names only hashes, file names under objects/, sizes and problem
codes. It never reads receipts, so no source id or original name can leak.
"""
import hashlib
import os
import re
import sqlite3
import stat

HEX64 = re.compile(r"[0-9a-f]{64}")
CHUNK = 1024 * 1024


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


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
    path = objects / sha
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return {"sha256": sha, "problem": "missing_file"}
    if not stat.S_ISREG(info.st_mode):
        return {"sha256": sha, "problem": "not_a_regular_file"}
    if info.st_size != expected:
        return {"sha256": sha, "problem": "size_mismatch", "expected": expected,
                "actual": info.st_size}
    if _sha256(path) != sha:
        return {"sha256": sha, "problem": "hash_mismatch"}
    return None


def check_store(root):
    private = root / "private"
    objects = private / "objects"
    if private.is_symlink() or objects.is_symlink():
        raise ValueError("private/objects must not be a symlink")
    ledger = private / "ledger.sqlite"
    rows = _ledger_rows(ledger) if ledger.exists() else []
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


def fix_modes(root):
    """chmod each loose_mode entry to 0700/0600 if a fresh lstat shows the same kind; nothing else."""
    if (root / "private").is_symlink() or (root / "private" / "objects").is_symlink():
        raise ValueError("private/objects must not be a symlink")
    fixed, skipped = [], []
    nofollow = {"follow_symlinks": False} if os.chmod in os.supports_follow_symlinks else {}
    for path, kind, problem in _mode_entries(root):
        try:
            same = stat.S_IFMT(_lstat(path).st_mode) == kind
        except OSError:
            same = False
        if same:
            os.chmod(path, 0o700 if kind == stat.S_IFDIR else 0o600, **nofollow)
        (fixed if same else skipped).append(problem["name"])
    return {"fixed": fixed, "skipped": skipped}
