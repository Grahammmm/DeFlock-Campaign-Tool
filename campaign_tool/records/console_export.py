"""Fresh read-only console copy per records run (DFGL-16).

``records console-export`` (container side) copies only the console's plain input files for one run and
streams them as a deterministic tar. ``records console-import`` (host side) verifies the stream against
EXPORT.json, installs it as ``DIR/exports/<UTC>-<run_id>/`` (dirs 0500, files 0400) and repoints the
relative symlink ``DIR/current``. Earlier exports are never deleted. The approval, mail, packet and
public-update journals, blobs and credentials are never copied: the existing journal readers cannot read a
copy, and their writers must keep failing closed on it.
"""
import argparse
import errno
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import approval_queue

SCHEMA = "records-console-export-v1"
RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")  # same grammar as admin_panel.check_run_id
NAME = re.compile(r"[0-9]{8}T[0-9]{6}Z-" + RUN_ID.pattern)
HEX = re.compile(r"[0-9a-f]{64}")
MAX_BYTES = 2 * 1024 ** 3
# Plain console inputs besides run-outputs/<run_id>/** (each optional). The ledger is copied with the SQLite
# backup API into a standalone file; no journal, -wal or -shm file is ever listed here.
LEDGER = "processing-v1/ledger.sqlite"
FILES = (LEDGER, "agency-retention.csv", "agency-aliases.tsv", "agency-roster.tsv")
MANIFEST = "EXPORT.json"


class ExportError(Exception):
    pass


def _need(ok, code):
    if not ok:
        raise ExportError(code)


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _open_dir(parent_fd, name):
    """Open directory name below the pinned parent_fd without following a symlink; None when absent."""
    try:
        return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except FileNotFoundError:
        return None
    except OSError:
        try:
            info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError:
            raise ExportError("SOURCE_UNREADABLE")
        raise ExportError("SYMLINK_REFUSED" if stat.S_ISLNK(info.st_mode) else "NOT_REGULAR")


def _open_chain(root_fd, parts):
    """Walk parts from the pinned root_fd one no-follow openat at a time; return the pinned fd or None."""
    current = os.dup(root_fd)
    for part in parts:
        try:
            nxt = _open_dir(current, part)
        finally:
            os.close(current)
        if nxt is None:
            return None
        current = nxt
    return current


def _copy_file(dir_fd, name, dst):
    """Copy one regular file opened relative to the pinned dir_fd; a symlink leaf is refused."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    except OSError as error:
        raise ExportError("SYMLINK_REFUSED" if error.errno == errno.ELOOP else "SOURCE_UNREADABLE")
    with os.fdopen(fd, "rb") as handle:
        _need(stat.S_ISREG(os.fstat(handle.fileno()).st_mode), "NOT_REGULAR")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "wb") as out:
            shutil.copyfileobj(handle, out)


def _copy_tree(dir_fd, rel, dest):
    """Copy every regular file below the pinned dir_fd; any symlink or special file is refused."""
    with os.scandir(dir_fd) as scan:
        entries = sorted(scan, key=lambda entry: entry.name)
    for entry in entries:
        info = entry.stat(follow_symlinks=False)
        _need(not stat.S_ISLNK(info.st_mode), "SYMLINK_REFUSED")
        target = os.path.join(rel, entry.name)
        if stat.S_ISDIR(info.st_mode):
            child = _open_dir(dir_fd, entry.name)
            _need(child is not None, "SOURCE_UNREADABLE")
            try:
                _copy_tree(child, target, dest)
            finally:
                os.close(child)
        else:
            _copy_file(dir_fd, entry.name, os.path.join(dest, target))


def _backup_ledger(dir_fd, name, dst):
    """Back up the ledger through the pinned directory (/proc/self/fd keeps -wal/-shm beside it)."""
    _need(not stat.S_ISLNK(os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode), "SYMLINK_REFUSED")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    source = sqlite3.connect(f"file:/proc/self/fd/{dir_fd}/{name}?mode=ro", uri=True, timeout=30)
    target = sqlite3.connect(dst)
    try:
        source.backup(target)
        target.execute("PRAGMA journal_mode=DELETE")
        _need(target.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "LEDGER_INVALID")
    finally:
        target.close()
        source.close()


def latest_run(project):
    root = os.open(os.path.abspath(project), os.O_RDONLY | os.O_DIRECTORY)
    try:
        base = _open_dir(root, "run-outputs")
    finally:
        os.close(root)
    _need(base is not None, "RUN_NOT_FOUND")
    try:
        with os.scandir(base) as scan:
            found = [(entry.stat(follow_symlinks=False).st_mtime, entry.name) for entry in scan
                     if entry.is_dir(follow_symlinks=False) and RUN_ID.fullmatch(entry.name)]
    finally:
        os.close(base)
    _need(found, "RUN_NOT_FOUND")
    return max(found)[1]


def build(project, run_id, dest):
    """Write the export tree for run_id into dest (an empty directory) and return the manifest."""
    _need(isinstance(run_id, str) and RUN_ID.fullmatch(run_id) and run_id not in (".", ".."), "RUN_ID_INVALID")
    project = os.path.abspath(project)
    root = os.open(project, os.O_RDONLY | os.O_DIRECTORY)
    try:
        run_fd = _open_chain(root, ["run-outputs", run_id])
        _need(run_fd is not None, "RUN_NOT_FOUND")
        try:
            _copy_tree(run_fd, os.path.join("run-outputs", run_id), dest)
        finally:
            os.close(run_fd)
        for rel in FILES:
            parts = rel.split("/")
            parent = _open_chain(root, parts[:-1])
            if parent is None:
                continue
            try:
                try:
                    os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                (_backup_ledger if rel == LEDGER else _copy_file)(parent, parts[-1], os.path.join(dest, rel))
            finally:
                os.close(parent)
    finally:
        os.close(root)
    files = {}
    for base, _, names in os.walk(dest):
        for name in names:
            path = os.path.join(base, name)
            files[os.path.relpath(path, dest)] = _sha256_file(path)
    _need(sum(os.path.getsize(os.path.join(dest, rel)) for rel in files) <= MAX_BYTES, "EXPORT_TOO_LARGE")
    manifest = {"schema": SCHEMA, "run_id": run_id, "source_project_id": approval_queue._project_id(project),
                "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "files": dict(sorted(files.items())), "applied_journal_ids": [], "model": None}
    Path(dest, MANIFEST).write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def write_tar(root, handle):
    with tarfile.open(fileobj=handle, mode="w|", format=tarfile.GNU_FORMAT) as tar:
        names = sorted(os.path.relpath(os.path.join(b, n), root) for b, _, ns in os.walk(root) for n in ns)
        for rel in names:
            info = tarfile.TarInfo(rel)
            info.size = os.path.getsize(os.path.join(root, rel))
            info.mode = 0o600
            info.mtime = 0
            info.uname = info.gname = ""
            with open(os.path.join(root, rel), "rb") as source:
                tar.addfile(info, source)


def export_main(argv=None):
    parser = argparse.ArgumentParser(prog="records console-export", description=__doc__)
    parser.add_argument("--project", required=True)
    pick = parser.add_mutually_exclusive_group(required=True)
    pick.add_argument("--run-id")
    pick.add_argument("--latest", action="store_true")
    out = parser.add_mutually_exclusive_group(required=True)
    out.add_argument("--out")
    out.add_argument("--stdout", action="store_true")
    args = parser.parse_args(argv)
    try:
        run_id = latest_run(args.project) if args.latest else args.run_id
        if args.stdout:
            with tempfile.TemporaryDirectory(prefix="console-export-") as tmp:
                build(args.project, run_id, tmp)
                write_tar(tmp, sys.stdout.buffer)
            sys.stdout.buffer.flush()
        else:
            os.makedirs(args.out, mode=0o700)  # must not exist yet
            build(args.project, run_id, args.out)
    except (ExportError, OSError, sqlite3.Error) as error:
        print(f"console-export: refused ({error if isinstance(error, ExportError) else type(error).__name__})",
              file=sys.stderr)
        return 1
    return 0


# ---- host side ------------------------------------------------------------------------------------

def _check_dir(out):
    _need(isinstance(out, str) and os.path.isabs(out), "EXPORT_DIR_INVALID")
    try:
        info = os.lstat(out)
        ok = (stat.S_ISDIR(info.st_mode) and os.path.realpath(out) == out and info.st_uid == os.geteuid()
              and stat.S_IMODE(info.st_mode) == 0o700)
    except OSError:
        ok = False
    _need(ok, "EXPORT_DIR_INVALID")


def _unpack(handle, staging):
    """Stream the tar into staging: plain files only, relative sorted-safe names, no duplicate."""
    seen, total = {}, 0
    with tarfile.open(fileobj=handle, mode="r|") as tar:
        for member in tar:
            parts = member.name.split("/")
            _need(member.isreg() and not member.name.startswith("/") and ".." not in parts
                  and "" not in parts and "." not in parts and member.name not in seen, "EXPORT_TAR_INVALID")
            total += member.size
            _need(total <= MAX_BYTES, "EXPORT_TOO_LARGE")
            path = os.path.join(staging, member.name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            digest = hashlib.sha256()
            with open(path, "wb") as target, tar.extractfile(member) as source:
                for block in iter(lambda: source.read(1 << 20), b""):
                    digest.update(block)
                    target.write(block)
            seen[member.name] = digest.hexdigest()
    return seen


def _verify(staging, seen):
    _need(MANIFEST in seen, "EXPORT_MANIFEST_MISSING")
    try:
        manifest = json.loads(Path(staging, MANIFEST).read_text(encoding="utf-8"))
        files = manifest["files"]
        run_id, project_id = manifest["run_id"], manifest["source_project_id"]
        ok = (manifest["schema"] == SCHEMA and isinstance(files, dict) and isinstance(run_id, str)
              and RUN_ID.fullmatch(run_id) and isinstance(project_id, str) and re.fullmatch("[0-9a-f]{16}", project_id)
              and all(isinstance(v, str) and HEX.fullmatch(v) for v in files.values()))
    except (OSError, ValueError, KeyError, TypeError):
        ok = False
    _need(ok, "EXPORT_MANIFEST_INVALID")
    _need(files == {k: v for k, v in seen.items() if k != MANIFEST}, "EXPORT_HASH_MISMATCH")
    return run_id


def _seal(top):
    for base, dirs, names in os.walk(top, topdown=False):
        for name in names:
            os.chmod(os.path.join(base, name), 0o400)
        if base != top:  # the top directory is sealed after the rename (renaming a directory needs write on it)
            os.chmod(base, 0o500)


def install(out, handle):
    _check_dir(out)
    exports = os.path.join(out, "exports")
    os.makedirs(exports, mode=0o700, exist_ok=True)
    _need(os.path.realpath(exports) == exports and not os.path.islink(exports), "EXPORT_DIR_INVALID")
    staging = tempfile.mkdtemp(prefix=".staging-", dir=out)
    try:
        run_id = _verify(staging, _unpack(handle, staging))
        name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + run_id
        _need(NAME.fullmatch(name) and not os.path.lexists(os.path.join(exports, name)), "EXPORT_EXISTS")
        _seal(staging)
        final = os.path.join(exports, name)
        os.rename(staging, final)
        os.chmod(final, 0o500)
        link = os.path.join(out, ".current.tmp")
        if os.path.lexists(link):
            os.unlink(link)
        os.symlink(f"exports/{name}", link)  # relative target
        os.replace(link, os.path.join(out, "current"))
    except BaseException:
        if os.path.isdir(staging):
            shutil.rmtree(staging, ignore_errors=True)
        raise
    return name


def import_main(argv=None):
    parser = argparse.ArgumentParser(prog="records console-import", description=__doc__)
    parser.add_argument("--out", required=True)
    parser.add_argument("--from-tar", required=True, metavar="FILE|-")
    args = parser.parse_args(argv)
    try:
        if args.from_tar == "-":
            name = install(args.out, sys.stdin.buffer)
        else:
            with open(args.from_tar, "rb") as handle:
                name = install(args.out, handle)
    except (ExportError, OSError, tarfile.TarError, EOFError) as error:
        print(f"console-import: refused ({error if isinstance(error, ExportError) else type(error).__name__})",
              file=sys.stderr)
        return 1
    print(f"console-import: installed exports/{name}")
    return 0
