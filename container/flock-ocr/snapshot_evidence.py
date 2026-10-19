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


def _walk(root):
    root = Path(root)
    yield root, "."
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        for name in sorted(dirnames + filenames):
            path = Path(dirpath) / name
            yield path, path.relative_to(root).as_posix()


def manifest(root):
    """Return the read-only content and owner/mode manifest of ``root``."""
    if not Path(root).is_dir():
        raise ValueError(f"not a directory: {root}")
    entries = {}
    for path, rel in _walk(root):
        info = os.lstat(path)
        kind = _KINDS.get(stat.S_IFMT(info.st_mode), "other")
        entry: dict = {"type": kind, "owner": f"{info.st_uid}:{info.st_gid}",
                       "mode": format(stat.S_IMODE(info.st_mode), "04o")}
        if kind == "f":
            entry["sha256"] = _sha256(path)
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
    try:
        with open(path, "rb") as handle:
            return handle.read(16) == SQLITE_MAGIC
    except OSError:
        return False


def sqlite_evidence(copy_root, out_dir, live_roots):
    """Back up and integrity-check every SQLite database in an isolated copy."""
    check_isolated(copy_root, live_roots)
    check_isolated(out_dir, live_roots)
    out_dir = Path(out_dir)
    report = {}
    for path, rel in _walk(copy_root):
        if not path.is_file() or path.is_symlink() or not is_sqlite(path):
            continue
        target = out_dir / (rel + ".backup")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise ValueError(f"refusing to overwrite {target}")
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
        report[rel] = {"backup": str(target), "backup_sha256": _sha256(target),
                       "integrity_check": result, "ok": result == ["ok"]}
    return {"databases": report, "ok": all(item["ok"] for item in report.values())}


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
    except ValueError as error:
        print("FAIL", error, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
