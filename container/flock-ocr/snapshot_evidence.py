#!/usr/bin/env python3
"""Source-preserving snapshot evidence for the owner runbook [S1]-[S3].

Stdlib only. The owner runs it; nothing in this repository calls it on real
data. It has three subcommands:

  manifest ROOT
      Read-only. Prints a JSON manifest of ROOT: for every entry its relative
      path, type, numeric uid:gid and mode, plus the SHA-256 of every regular
      file and the target of every symlink. Files are opened read-only as plain
      bytes; SQLite is never used, so -wal and -shm sidecars are hashed as they
      are and never checkpointed or removed.

  compare A.json B.json
      Exit 0 only if the two manifests are identical; otherwise print every
      differing path and exit 1.

  sqlite-evidence COPY_ROOT --out DIR --live LIVE [--live LIVE ...]
      Runs the SQLite backup API and PRAGMA integrity_check on every SQLite
      database under COPY_ROOT, an isolated, disposable copy extracted from the
      snapshot archive. Opening a WAL database with SQLite may checkpoint it and
      delete its sidecars when the last connection closes, so this command
      refuses to run when COPY_ROOT is, contains or is inside any LIVE mount
      source. Writes <relpath>.backup files under DIR and prints a JSON report.
      Fails closed (exit 2): a missing or non-directory COPY_ROOT, a directory
      that cannot be enumerated, a file that cannot be stat'ed or read, or a
      SQLite error is an error, never "no databases". A fully inspected
      database-free directory is a valid result: empty "databases", ok true,
      with "files_inspected" counting the regular files read.

SQLite databases are found by their 16-byte file header, not by name.
"""
import argparse
import hashlib
import json
import os
import sqlite3
import stat
import sys
from pathlib import Path

SQLITE_MAGIC = b"SQLite format 3\x00"
_KINDS = {stat.S_IFREG: "f", stat.S_IFDIR: "d", stat.S_IFLNK: "l"}


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


class InspectionError(ValueError):
    """Evidence could not be gathered; never treated as an empty result."""


def _why(error):
    return error.strerror or str(error)


def _raise(error):
    raise InspectionError(f"cannot enumerate {error.filename}: {_why(error)}")


def _require_dir(root):
    """Reject a missing or non-directory root before any inspection."""
    try:
        info = os.stat(root)
    except OSError as error:
        raise InspectionError(f"cannot inspect root {root}: {_why(error)}")
    if not stat.S_ISDIR(info.st_mode):
        raise InspectionError(f"not a directory: {root}")


def _walk(root):
    """Yield every entry under ``root``; any enumeration error is fatal."""
    _require_dir(root)
    root = Path(root)
    yield root, "."
    for dirpath, dirnames, filenames in os.walk(root, onerror=_raise, followlinks=False):
        dirnames.sort()
        for name in sorted(dirnames + filenames):
            path = Path(dirpath) / name
            yield path, path.relative_to(root).as_posix()


def manifest(root):
    """Return the read-only content and owner/mode manifest of ``root``."""
    entries = {}
    for path, rel in _walk(root):
        try:
            info = os.lstat(path)
        except OSError as error:
            raise InspectionError(f"cannot stat {rel}: {_why(error)}")
        kind = _KINDS.get(stat.S_IFMT(info.st_mode), "other")
        entry: dict = {"type": kind, "owner": f"{info.st_uid}:{info.st_gid}",
                       "mode": format(stat.S_IMODE(info.st_mode), "04o")}
        if kind == "f":
            try:
                entry["sha256"] = _sha256(path)
            except OSError as error:
                raise InspectionError(f"cannot read {rel}: {_why(error)}")
            entry["size"] = info.st_size
        elif kind == "l":
            entry["target"] = os.readlink(path)
        entries[rel] = entry
    return {"manifest_version": 1, "entries": dict(sorted(entries.items()))}


def compare(left, right):
    """Return the sorted list of paths whose manifest entries differ."""
    a, b = left["entries"], right["entries"]
    return sorted(p for p in set(a) | set(b) if a.get(p) != b.get(p))


def _within(child, parent):
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def check_isolated(copy_root, live_roots):
    """Raise ValueError if ``copy_root`` overlaps any live mount source."""
    if not live_roots:
        raise ValueError("name every live mount source with --live")
    copy_real = Path(os.path.realpath(copy_root))
    for live in live_roots:
        live_real = Path(os.path.realpath(live))
        if _within(copy_real, live_real) or _within(live_real, copy_real):
            raise ValueError(f"{copy_root} overlaps live mount {live}; "
                             "run SQLite only on an isolated extracted copy")


def is_sqlite(path):
    """True if ``path`` starts with the SQLite header; read errors are fatal."""
    try:
        with open(path, "rb") as handle:
            return handle.read(16) == SQLITE_MAGIC
    except OSError as error:
        raise InspectionError(f"cannot read {path}: {_why(error)}")


def sqlite_evidence(copy_root, out_dir, live_roots):
    """Back up and integrity-check every SQLite database in an isolated copy."""
    check_isolated(copy_root, live_roots)
    check_isolated(out_dir, live_roots)
    out_dir = Path(out_dir)
    report = {}
    # Inspect the whole copy before opening any database: opening a WAL copy
    # may checkpoint it and delete its sidecars, which must not be mistaken for
    # (or hide) an inspection error.
    databases = []
    files_inspected = 0
    for path, rel in list(_walk(copy_root)):
        try:
            mode = os.lstat(path).st_mode
        except OSError as error:
            raise InspectionError(f"cannot stat {rel}: {_why(error)}")
        if not stat.S_ISREG(mode):
            continue
        files_inspected += 1
        if is_sqlite(path):
            databases.append((path, rel))
    for path, rel in databases:
        target = out_dir / (rel + ".backup")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise ValueError(f"refusing to overwrite {target}")
        try:
            source = sqlite3.connect(path)
            try:
                destination = sqlite3.connect(target)
                try:
                    source.backup(destination)
                finally:
                    destination.close()
            finally:
                source.close()
            check = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
            try:
                result = [row[0] for row in check.execute("PRAGMA integrity_check")]
            finally:
                check.close()
        except sqlite3.Error as error:
            raise InspectionError(f"SQLite evidence failed for {rel}: {error}")
        report[rel] = {"backup": str(target), "backup_sha256": _sha256(target),
                       "integrity_check": result, "ok": result == ["ok"]}
    # An empty "databases" map is valid only because the root was verified to
    # be a directory and every entry was enumerated and read without error.
    return {"databases": report, "files_inspected": files_inspected,
            "ok": all(item["ok"] for item in report.values())}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Source-preserving snapshot evidence.")
    sub = parser.add_subparsers(dest="command", required=True)
    p_manifest = sub.add_parser("manifest")
    p_manifest.add_argument("root")
    p_compare = sub.add_parser("compare")
    p_compare.add_argument("left")
    p_compare.add_argument("right")
    p_sqlite = sub.add_parser("sqlite-evidence")
    p_sqlite.add_argument("copy_root")
    p_sqlite.add_argument("--out", required=True)
    p_sqlite.add_argument("--live", action="append", default=[])
    args = parser.parse_args(argv)
    try:
        if args.command == "manifest":
            print(json.dumps(manifest(args.root), indent=1, sort_keys=True))
            return 0
        if args.command == "compare":
            diff = compare(json.loads(Path(args.left).read_text()),
                           json.loads(Path(args.right).read_text()))
            for path in diff:
                print("DIFF", path)
            print("manifests: equal" if not diff else f"manifests: {len(diff)} difference(s)")
            return int(bool(diff))
        report = sqlite_evidence(args.copy_root, args.out, args.live)
        print(json.dumps(report, indent=1, sort_keys=True))
        return 0 if report["ok"] else 1
    except (ValueError, OSError) as error:
        print("FAIL", error, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
