"""Selected-file recovery sets; no service activation, sends or deployment."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time

CATEGORIES = frozenset(("originals", "ledger", "rules", "receipts"))
CHUNK = 1024 * 1024
MAX_FILE = 256 * 1024 * 1024
MAX_TOTAL = 1024 * 1024 * 1024
MAX_ENTRIES = 1000
MAX_MANIFEST = 1024 * 1024


class RecoveryError(ValueError):
    pass


def require(value, reason):
    if not value:
        raise RecoveryError(reason)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def fingerprint(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


@contextmanager
def parent_fd(path):
    """Walk real directories without following any symlink component."""
    path = Path(os.path.abspath(os.fspath(path)))
    require(path.name not in ("", ".", ".."), "invalid_path")
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            newer = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = newer
        yield fd, path.name
    finally:
        os.close(fd)


@contextmanager
def source_fd(path):
    with parent_fd(path) as (parent, name):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            info = os.fstat(fd)
            require(stat.S_ISREG(info.st_mode), "regular_file_required")
            require(info.st_size <= MAX_FILE, "file_size_bound")
            yield fd, info
        finally:
            os.close(fd)


def new_directory(path):
    with parent_fd(path) as (parent, name):
        os.mkdir(name, 0o700, dir_fd=parent)
        os.fsync(parent)
    return Path(os.path.abspath(os.fspath(path)))


def write_new(path, raw):
    with parent_fd(path) as (parent, name):
        fd = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            with os.fdopen(fd, "wb", closefd=False) as stream:
                stream.write(raw); stream.flush(); os.fsync(fd)
            os.fchmod(fd, 0o400)
        finally:
            os.close(fd)
        os.fsync(parent)


def copy_exact(source, destination, *, expected=None):
    """Stream, hash and compare file identity before/after; never replace a file."""
    with source_fd(source) as (src, before), parent_fd(destination) as (parent, name):
        dst = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        total, hasher = 0, hashlib.sha256()
        try:
            with os.fdopen(dst, "wb", closefd=False) as stream:
                while chunk := os.read(src, CHUNK):
                    total += len(chunk)
                    require(total <= MAX_FILE, "file_size_bound")
                    hasher.update(chunk); stream.write(chunk)
                stream.flush(); os.fsync(dst)
            require(fingerprint(before) == fingerprint(os.fstat(src)), "source_changed_during_copy")
            result = {"sha256": hasher.hexdigest(), "bytes": total}
            if expected is not None:
                require(result == expected, "file_hash_or_size_mismatch")
            os.fchmod(dst, 0o400)
        finally:
            os.close(dst)
        os.fsync(parent)
        return result


def snapshot_ledger(source, destination):
    """SQLite online backup captures a transaction-consistent ledger, including WAL."""
    with source_fd(source) as (fd, before), parent_fd(destination) as (parent, name):
        # SQLite must use the real database pathname to locate WAL/SHM correctly.
        # Pin and check its inode, with no symlink path, before and after backup.
        source_path = Path(os.path.abspath(os.fspath(source)))
        require(fingerprint(source_path.stat(follow_symlinks=False)) == fingerprint(before), "ledger_path_changed")
        out = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        os.close(out)
        started = time.monotonic()
        def bounded(status, remaining, total):
            require(time.monotonic() - started < 30, "ledger_snapshot_timeout")
            require(total * page_size <= MAX_FILE, "ledger_snapshot_bound")
        with sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True, timeout=2) as live:
            live.execute("PRAGMA query_only=ON")
            page_size = live.execute("PRAGMA page_size").fetchone()[0]
            pages = live.execute("PRAGMA page_count").fetchone()[0]
            require(page_size * pages <= MAX_FILE, "ledger_size_bound")
            with sqlite3.connect(destination, timeout=2) as target:
                live.backup(target, pages=128, progress=bounded, sleep=0.01)
                require(target.execute("PRAGMA quick_check").fetchone()[0] == "ok", "ledger_integrity_failed")
        with parent_fd(source) as (src_parent, src_name):
            after = os.stat(src_name, dir_fd=src_parent, follow_symlinks=False)
        require(stat.S_ISREG(after.st_mode) and (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino), "ledger_path_changed")
        # Snapshot bytes, not the changing live database file, are the backed-up hash.
        with source_fd(destination) as (snapshot, info):
            hasher = hashlib.sha256()
            while chunk := os.read(snapshot, CHUNK): hasher.update(chunk)
            os.fsync(snapshot); os.fchmod(snapshot, 0o400)
        os.fsync(parent)
        return {"sha256":hasher.hexdigest(), "bytes":info.st_size}


def backup(destination, entries):
    """entries: [{category, name, source}], explicit selected scope only."""
    require(isinstance(entries, list) and 1 <= len(entries) <= MAX_ENTRIES, "entry_count_bound")
    keys, categories = set(), set()
    for entry in entries:
        require(isinstance(entry, dict) and set(entry) == {"category", "name", "source"}, "entry_schema")
        require(entry["category"] in CATEGORIES and isinstance(entry["name"], str) and
                re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}", entry["name"]), "entry_identity")
        key = (entry["category"], entry["name"])
        require(key not in keys, "duplicate_entry"); keys.add(key); categories.add(entry["category"])
    require(categories == CATEGORIES and sum(e["category"] == "ledger" for e in entries) == 1, "four_categories_one_ledger_required")
    root = new_directory(destination)
    for category in sorted(CATEGORIES): new_directory(root/category)
    files, total = [], 0
    for entry in sorted(entries, key=lambda e:(e["category"], e["name"])):
        relative = entry["category"] + "/" + entry["name"]
        copied = (snapshot_ledger if entry["category"] == "ledger" else copy_exact)(entry["source"], root/relative)
        total += copied["bytes"]; require(total <= MAX_TOTAL, "total_size_bound")
        files.append({"path":relative, "category":entry["category"], **copied})
    manifest = {"schema":"records-backup-v1", "scope":"explicit_selected_files",
                "ledger_consistency":"sqlite_online_snapshot", "files":files, "total_bytes":total,
                "sends_allowed":False, "deploy_allowed":False}
    raw = canonical(manifest); require(len(raw) <= MAX_MANIFEST, "manifest_size_bound")
    # Written last. Interrupted destinations without this file are incomplete.
    write_new(root/"manifest.json", raw)
    return {"manifest_sha256":sha(raw), "files":len(files), "total_bytes":total,
            "scope":"explicit_selected_files", "backup_complete":True}


def restore(source, destination, *, manifest_sha256):
    """Caller supplies the separately retained trusted manifest digest."""
    require(isinstance(manifest_sha256,str) and re.fullmatch(r"[0-9a-f]{64}",manifest_sha256), "trusted_manifest_hash_required")
    source, destination = Path(os.path.abspath(source)), Path(os.path.abspath(destination))
    require(source != destination and source not in destination.parents, "restore_must_be_isolated")
    with source_fd(source/"manifest.json") as (fd, info):
        require(info.st_size <= MAX_MANIFEST, "manifest_size_bound")
        raw = os.read(fd, MAX_MANIFEST+1)
    require(sha(raw) == manifest_sha256, "manifest_hash_mismatch")
    manifest = json.loads(raw)
    require(isinstance(manifest,dict) and manifest.get("schema") == "records-backup-v1" and
            manifest.get("sends_allowed") is False and manifest.get("deploy_allowed") is False, "manifest_schema")
    files=manifest.get("files")
    require(isinstance(files,list) and 1 <= len(files) <= MAX_ENTRIES, "entry_count_bound")
    seen, total, categories, ledgers = set(), 0, set(), 0
    for row in files:
        require(isinstance(row,dict) and set(row)=={"path","category","sha256","bytes"}, "manifest_entry_schema")
        relative=row["path"];category=row["category"]
        require(category in CATEGORIES and isinstance(relative,str) and
                re.fullmatch(re.escape(category)+r"/[A-Za-z0-9][A-Za-z0-9_.-]{0,159}",relative), "unsafe_manifest_path")
        require(relative not in seen, "duplicate_manifest_entry");seen.add(relative)
        require(type(row["bytes"]) is int and 0 <= row["bytes"] <= MAX_FILE and
                isinstance(row["sha256"],str) and re.fullmatch(r"[0-9a-f]{64}",row["sha256"]), "manifest_hash_size")
        total+=row["bytes"];categories.add(category);ledgers+=int(category=="ledger")
    require(total <= MAX_TOTAL and manifest.get("total_bytes")==total and categories==CATEGORIES and ledgers==1, "manifest_totals")
    root=new_directory(destination)
    for category in sorted(CATEGORIES):new_directory(root/category)
    for row in files:
        copy_exact(source/row["path"],root/row["path"],expected={"sha256":row["sha256"],"bytes":row["bytes"]})
    receipt={"schema":"records-restore-v1","manifest_sha256":manifest_sha256,"files":len(files),
             "total_bytes":total,"hash_reconciliation":"exact","scope":manifest["scope"],
             "isolated":True,"sends":0,"deployments":0,"activation":False}
    write_new(root/"RESTORE-RECEIPT.json",canonical(receipt))
    return receipt
