#!/usr/bin/env python3
"""Source-preserving snapshot evidence for the owner runbook [S1]-[S3].

Stdlib only. The owner runs it; nothing in this repository calls it on real
data. It has three subcommands:

  manifest ROOT
      Read-only. Prints a JSON manifest of ROOT: for every entry its relative
      path, type, numeric uid:gid and mode, plus the SHA-256 of every regular
      file and the target of every symlink. Files are opened read-only as plain
      bytes; SQLite is never used, so -wal and -shm sidecars are hashed as they
      are and never checkpointed or removed. Special files (sockets, fifos,
      character and block devices) are entries of type "other" with a
      "special" kind and are also listed in the top-level "non_archivable"
      map (path -> kind), empty when there are none.

  compare A.json B.json
      Exit 0 only if the two manifests are identical; otherwise print every
      differing path and exit 1. Special files are compared too; the rootless
      driver instead uses compare_archivable(), which leaves them out and
      reports them.

  sqlite-evidence COPY_ROOT --out DIR --live LIVE [--live LIVE ...]
                  [--accept-malformed RELPATH=SHA256 ...]
      Runs the SQLite backup API and PRAGMA integrity_check on every SQLite
      database under COPY_ROOT, an isolated, disposable copy extracted from the
      snapshot archive. Opening a WAL database with SQLite may checkpoint it and
      delete its sidecars when the last connection closes, so this command
      refuses to run when COPY_ROOT is, contains or is inside any LIVE mount
      source. Writes <relpath>.backup files under DIR and prints a JSON report.
      Fails closed (exit 2): a missing or non-directory COPY_ROOT, a directory
      that cannot be enumerated, a file that cannot be stat'ed or read, or a
      SQLite error is an error, never "no databases". Every database is tried
      before failing, and each SQLite error is printed on its own FAIL line.
      A fully inspected database-free directory is a valid result: empty
      "databases", ok true, with "files_inspected" counting the regular files
      read.

      --accept-malformed RELPATH=SHA256 (repeatable) exempts one known,
      intentionally corrupt database (for example a mutation-test fixture)
      from the SQLite requirement only. Its bytes in the copy must hash to
      SHA256 exactly; it is then never opened with SQLite and is reported as
      "accepted_malformed" with no backup. A pinned path that is missing, is
      not a SQLite file or has another hash is an error. Without the flag
      nothing changes.

  survey ROOT [--accept-malformed RELPATH=SHA256 ...]
      Read-only preflight survey of a LIVE mount (the rootless driver runs it
      in a helper with the mount read-only, before anything is stopped). No
      file is hashed except pinned databases. Prints a JSON report: entry and
      byte counts, space estimates for one tar archive and one extracted copy
      (space_estimate), special files, entries owned by the overflow ID
      65534, and every SQLite database with an integrity_check result. A
      database is opened only as file:...?mode=ro&immutable=1, so SQLite takes
      no lock, never reads or replays a -wal and never creates a sidecar; a
      database being written at that moment can therefore read as malformed
      (a false refusal before any change, never a false pass of S2: the
      snapshot evidence at S2/A2 still runs on the isolated copy). A pinned
      database whose bytes hash to its pin is never opened. Exit 0 when the
      survey completed (findings are in the report), 2 when it could not.

SQLite databases are found by their 16-byte file header, not by name.
"""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
from pathlib import Path

SQLITE_MAGIC = b"SQLite format 3\x00"
HEX64_RE = re.compile(r"[0-9a-f]{64}")
_KINDS = {stat.S_IFREG: "f", stat.S_IFDIR: "d", stat.S_IFLNK: "l"}
ARCHIVABLE = frozenset(_KINDS.values())
_SPECIAL = {stat.S_IFSOCK: "socket", stat.S_IFIFO: "fifo",
            stat.S_IFCHR: "char_device", stat.S_IFBLK: "block_device"}


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
    entries, special = {}, {}
    for path, rel in _walk(root):
        try:
            info = os.lstat(path)
        except OSError as error:
            raise InspectionError(f"cannot stat {rel}: {_why(error)}")
        kind = _KINDS.get(stat.S_IFMT(info.st_mode), "other")
        entry: dict = {"type": kind, "owner": f"{info.st_uid}:{info.st_gid}",
                       "mode": format(stat.S_IMODE(info.st_mode), "04o")}
        if kind == "other":
            entry["special"] = _SPECIAL.get(stat.S_IFMT(info.st_mode), "unknown")
            special[rel] = entry["special"]
        elif kind == "f":
            try:
                entry["sha256"] = _sha256(path)
            except OSError as error:
                raise InspectionError(f"cannot read {rel}: {_why(error)}")
            entry["size"] = info.st_size
        elif kind == "l":
            entry["target"] = os.readlink(path)
        entries[rel] = entry
    return {"manifest_version": 1, "entries": dict(sorted(entries.items())),
            "non_archivable": dict(sorted(special.items()))}


def compare(left, right):
    """Return the sorted list of paths whose manifest entries differ."""
    a, b = left["entries"], right["entries"]
    return sorted(p for p in set(a) | set(b) if a.get(p) != b.get(p))


def non_archivable(manifest_data):
    """Map each special-file path (socket, fifo, device) to its kind.

    Derived from the entries, not the top-level "non_archivable" key, so a
    manifest written before that key existed (type "other", no "special")
    is read the same way.
    """
    return {p: e.get("special", "other") for p, e in sorted(manifest_data["entries"].items())
            if e.get("type") not in ARCHIVABLE}


def compare_archivable(left, right):
    """Compare only regular files, directories and symlinks.

    Returns (diff, excluded): the sorted paths whose archivable entries
    differ, and the sorted special-file paths of either side that were left
    out. A path that is a special file on one side and a regular file,
    directory or symlink on the other is still a difference.
    """
    a = {p: e for p, e in left["entries"].items() if e.get("type") in ARCHIVABLE}
    b = {p: e for p, e in right["entries"].items() if e.get("type") in ARCHIVABLE}
    diff = sorted(p for p in set(a) | set(b) if a.get(p) != b.get(p))
    excluded = sorted(set(non_archivable(left)) | set(non_archivable(right)))
    return diff, excluded


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


def parse_accepted(values):
    """Parse RELPATH=SHA256 pins into a dict; malformed or duplicate pins raise."""
    accepted = {}
    for value in values or ():
        rel, sep, digest = str(value).rpartition("=")
        if not sep or not HEX64_RE.fullmatch(digest):
            raise ValueError(f"--accept-malformed needs RELPATH=SHA256 (64 lowercase hex): {value!r}")
        parts = rel.split("/")
        if (not rel or rel.startswith("/") or any(p in ("", ".", "..") for p in parts)
                or any(ch in rel for ch in "\n\r\0")):
            raise ValueError(f"--accept-malformed path must be a normalized relative path: {rel!r}")
        if rel in accepted:
            raise ValueError(f"--accept-malformed names {rel!r} more than once")
        accepted[rel] = digest
    return accepted


def sqlite_evidence(copy_root, out_dir, live_roots, accepted=None):
    """Back up and integrity-check every SQLite database in an isolated copy.

    ``accepted`` maps relative paths of known, intentionally corrupt databases
    to the SHA-256 their bytes must have. Such a file is hash-checked, never
    opened with SQLite, and reported as accepted_malformed. Every other
    database must back up and pass integrity_check as before.
    """
    accepted = dict(accepted or {})
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
    found = {rel for _, rel in databases}
    missing = sorted(set(accepted) - found)
    if missing:
        raise InspectionError(f"accepted malformed database(s) not found as SQLite files: {missing}")
    for path, rel in databases:
        if rel not in accepted:
            continue
        try:
            actual = _sha256(path)
        except OSError as error:
            raise InspectionError(f"cannot read {rel}: {_why(error)}")
        if actual != accepted[rel]:
            raise InspectionError(f"accepted malformed database {rel} has sha256 {actual}, "
                                  f"not the pinned {accepted[rel]}")
    failures = []
    for path, rel in databases:
        if rel in accepted:
            # Pinned bytes, verified above; never opened with SQLite.
            report[rel] = {"accepted_malformed": True, "sha256": accepted[rel],
                           "integrity_check": None, "ok": True}
            continue
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
            # Keep going so one run names every failing database; still fails.
            failures.append(f"SQLite evidence failed for {rel}: {error}")
            continue
        report[rel] = {"backup": str(target), "backup_sha256": _sha256(target),
                       "integrity_check": result, "ok": result == ["ok"]}
    if failures:
        raise InspectionError("\n".join(failures))
    # An empty "databases" map is valid only because the root was verified to
    # be a directory and every entry was enumerated and read without error.
    return {"databases": report, "files_inspected": files_inspected,
            "ok": all(item["ok"] for item in report.values())}


TAR_BLOCK, TAR_RECORD, FS_BLOCK = 512, 10240, 4096
OVERFLOW_ID = "65534"


def _round_up(value, unit):
    return -(-value // unit) * unit


def copy_estimate(entries):
    """Block-rounded payload plus one metadata block per manifest/survey entry."""
    return sum(_round_up(e.get("size", 0), FS_BLOCK) + FS_BLOCK for e in entries.values())


def space_estimate(entries):
    """Upper-bound bytes for one GNU tar archive and one extracted copy of entries.

    entries maps relpath -> {"type", "size"?, "target"?} (a manifest's or a
    survey's entries). tar, per entry: its 512-byte header, a reserve of two
    blocks for a pax extended header (--acls/--xattrs), a GNU long-name (and
    long-link) header plus name blocks when the archived name ("./" + path,
    "/" for directories) or a symlink target is 100 bytes or longer, and file
    data padded to 512; then the end-of-archive blocks, rounded up to the
    10240-byte record. Extract: file data rounded up to 4 KiB blocks plus one
    block per directory.
    """
    def long_name(value):
        n = len(value.encode())
        return 0 if n < 100 else TAR_BLOCK * (1 + _round_up(n + 1, TAR_BLOCK) // TAR_BLOCK)

    tar = TAR_BLOCK * 2
    extract = 0
    for rel, entry in entries.items():
        kind = entry.get("type")
        tar += TAR_BLOCK * 3 + long_name("./" + rel + ("/" if kind == "d" else ""))
        if kind == "f":
            size = int(entry.get("size") or 0)
            tar += _round_up(size, TAR_BLOCK)
            extract += _round_up(size, FS_BLOCK)
        elif kind == "d":
            extract += FS_BLOCK
        elif kind == "l":
            tar += long_name(str(entry.get("target") or ""))
    return {"tar_bytes": _round_up(tar, TAR_RECORD), "extract_bytes": extract}


def _integrity_ro(path):
    """PRAGMA integrity_check on a live file without locks, WAL or sidecars."""
    uri = Path(os.path.abspath(path)).as_uri() + "?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    try:
        return [row[0] for row in conn.execute("PRAGMA integrity_check")]
    finally:
        conn.close()


def survey(root, accepted=None):
    """Read-only preflight survey of a live mount (see the module docstring)."""
    accepted = dict(accepted or {})
    entries, special, overflow, databases = {}, {}, [], []
    total = 0
    for path, rel in _walk(root):
        try:
            info = os.lstat(path)
        except OSError as error:
            raise InspectionError(f"cannot stat {rel}: {_why(error)}")
        kind = _KINDS.get(stat.S_IFMT(info.st_mode), "other")
        entry: dict = {"type": kind}
        if OVERFLOW_ID in (str(info.st_uid), str(info.st_gid)):
            overflow.append(rel)
        if kind == "other":
            special[rel] = _SPECIAL.get(stat.S_IFMT(info.st_mode), "unknown")
        elif kind == "f":
            entry["size"] = info.st_size
            total += info.st_size
            if is_sqlite(path):
                databases.append((path, rel, info.st_size))
        elif kind == "l":
            entry["target"] = os.readlink(path)
        entries[rel] = entry
    report, sqlite_bytes, pins = {}, 0, {}
    for path, rel, size in databases:
        wal = entries.get(rel + "-wal", {}).get("size", 0)
        sqlite_bytes += size + wal
        item = {"size": size, "wal_size": wal}
        if rel in accepted:
            try:
                digest = _sha256(path)
            except OSError as error:
                raise InspectionError(f"cannot read {rel}: {_why(error)}")
            if digest == accepted[rel]:
                item["status"] = "accepted_malformed"
                pins[rel] = "accepted"
                report[rel] = item
                continue
            pins[rel] = "hash_differs"
        try:
            result = _integrity_ro(path)
            item["status"] = "ok" if result == ["ok"] else "malformed"
            if result != ["ok"]:
                item["detail"] = result[:5]
        except sqlite3.Error as error:
            item["status"], item["detail"] = "malformed", [str(error)]
        report[rel] = item
    for rel in accepted:
        if rel not in pins:
            pins[rel] = "not_sqlite" if entries.get(rel, {}).get("type") == "f" else "absent"
    estimate = space_estimate(entries)
    return {"survey_version": 1, "entries": len(entries),
            "files": sum(e["type"] == "f" for e in entries.values()), "bytes": total,
            **estimate, "estimated_copy_bytes": copy_estimate(entries),
            "sqlite_bytes": sqlite_bytes, "sqlite": dict(sorted(report.items())),
            "non_archivable": dict(sorted(special.items())), "overflow_owned": sorted(overflow),
            "pins": dict(sorted(pins.items()))}


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
    p_sqlite.add_argument("--accept-malformed", action="append", default=[], metavar="RELPATH=SHA256",
                          help="exempt one hash-pinned, intentionally corrupt database (repeatable)")
    p_survey = sub.add_parser("survey")
    p_survey.add_argument("root")
    p_survey.add_argument("--accept-malformed", action="append", default=[], metavar="RELPATH=SHA256")
    args = parser.parse_args(argv)
    try:
        if args.command == "survey":
            print(json.dumps(survey(args.root, parse_accepted(args.accept_malformed)), indent=1, sort_keys=True))
            return 0
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
        report = sqlite_evidence(args.copy_root, args.out, args.live,
                                 accepted=parse_accepted(args.accept_malformed))
        print(json.dumps(report, indent=1, sort_keys=True))
        return 0 if report["ok"] else 1
    except (ValueError, OSError) as error:
        for line in str(error).splitlines() or [""]:
            print("FAIL", line, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
