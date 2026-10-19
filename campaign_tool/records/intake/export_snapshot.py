"""Bounded explicit export-byte snapshot; no enrollment, MIME or provider calls."""
import hashlib
import os
from pathlib import Path

from . import mail_delta
from ..container_host import PrivateDir, _absolute, _fingerprint, digest, encoded, require
from ..portal.collections import Scope

MAX_TOTAL = 256 * 1024 * 1024


def selected_paths(root, receipts, output):
    """Normalize logical paths before mapping a container root onto its host bind."""
    _absolute(str(root))
    _absolute(str(output))
    require(isinstance(receipts, (list, tuple)) and 1 <= len(receipts) <= 60)
    root, output = Path(root), Path(output)
    require(all(isinstance(p, (str, Path)) for p in receipts))
    paths = tuple(str(mail_delta.source_path(root, str(p), output).relative_to(root)) for p in receipts)
    for name in paths:
        _absolute(str(root / name))
    require(len(set(paths)) == len(paths))
    return paths


def scope_value(scope):
    require(type(scope) is Scope and type(scope.hosts) is frozenset
            and type(scope.excluded_hosts) is frozenset)
    return {"hosts": sorted(scope.hosts), "excluded": sorted(scope.excluded_hosts),
            "depth": scope.max_depth, "pages": scope.max_pages,
            "members": scope.max_members, "links": scope.max_links}


def _readonly(fd, required):
    if required:
        require(bool(os.fstatvfs(fd).f_flag & os.ST_RDONLY))


def verify(path, snapshot, *, readonly=False):
    """Recheck all pinned source names/inodes; never substitutes for importer hashes."""
    root = Path(path)
    for name, expected in snapshot["directories"]:
        with PrivateDir(str(root / name)) as directory:
            _readonly(directory.fd, readonly)
            require(_fingerprint(os.fstat(directory.fd)) == expected)
            directory.check()
    for item in snapshot["files"]:
        source = root / item["path"]
        with PrivateDir(str(source.parent)) as directory:
            fd = directory.open_file(source.name)
            try:
                _readonly(fd, readonly)
                require(_fingerprint(os.fstat(fd)) == item["signature"])
                require(_fingerprint(os.stat(source.name, dir_fd=directory.fd,
                                             follow_symlinks=False)) == item["signature"])
            finally:
                os.close(fd)


def snapshot(path, *, receipts, scope, logical_root, output, expected=None,
             readonly=False, prior=None):
    """Hash only selected receipts and their declarations, with a unique-byte cap.

    Paths in the manifest are logical root-relative paths, so the host bind and
    container produce identical hashes. Fingerprints are local recheck evidence,
    not a claim that a mutable host filesystem became an atomic snapshot.
    """
    names = selected_paths(logical_root, receipts, output)
    scoped = scope_value(scope)
    root, logical = Path(path), Path(logical_root)
    files, directories, declarations, entries, total = {}, {}, {}, [], 0

    def remember_directory(parent):
        relative = str(parent.relative_to(root))
        with PrivateDir(str(parent)) as directory:
            _readonly(directory.fd, readonly)
            signature = _fingerprint(os.fstat(directory.fd))
            require(relative not in directories or directories[relative] == signature)
            directories[relative] = signature
            directory.check()

    def read(name, limit, *, declaration=None, receipt=False):
        nonlocal total
        role = "receipt" if receipt else "object"
        binding = (role, declaration)
        require(name not in declarations or declarations[name] == binding)
        declarations[name] = binding
        if name in files:
            require(files[name]["bytes"] <= limit)
            return files[name]
        source = root / name
        _absolute(str(source))
        remember_directory(root)
        parent = root
        for part in Path(name).parts[:-1]:
            parent /= part
            remember_directory(parent)
        with PrivateDir(str(source.parent)) as directory:
            fd = directory.open_file(source.name)
            try:
                before = os.fstat(fd)
                _readonly(fd, readonly)
                require(0 <= before.st_size <= limit and total + before.st_size <= MAX_TOTAL)
                hasher, consumed = hashlib.sha256(), 0
                while True:
                    chunk = os.read(fd, min(1024 * 1024, before.st_size - consumed + 1))
                    if not chunk:
                        break
                    consumed += len(chunk)
                    require(consumed <= before.st_size)
                    hasher.update(chunk)
                require(consumed == before.st_size and _fingerprint(before) == _fingerprint(os.fstat(fd)))
                require(_fingerprint(before) == _fingerprint(os.stat(source.name, dir_fd=directory.fd,
                                                                    follow_symlinks=False)))
                item = {"path": name, "bytes": consumed, "sha256": hasher.hexdigest(),
                        "signature": _fingerprint(before)}
                require(declaration is None or declaration == (item["bytes"], item["sha256"]))
                files[name] = item
                total += consumed
                return item
            finally:
                os.close(fd)

    remember_directory(root)
    for name in names:
        item = read(name, mail_delta.MAX_RECEIPT, receipt=True)
        _, identity, originals = mail_delta.load_receipt(root / name)
        # load_receipt reuses the existing schema validator. Its separate secure
        # read must leave the same source inode/size/timestamps as the hashed read.
        require(_fingerprint(os.stat(root / name, follow_symlinks=False)) == item["signature"])
        parts = []
        for original in originals:
            source = mail_delta.source_path(logical, original["path"], Path(output))
            relative = str(source.relative_to(logical))
            limit = mail_delta.MAX_EML if original["part"] == "0" else mail_delta.MAX_ATTACHMENT
            blob = read(relative, limit, declaration=(original["size"], original["sha"]))
            parts.append({"part": original["part"], "path": relative,
                          "bytes": blob["bytes"], "sha256": blob["sha256"]})
        entries.append({"path": name, "sha256": item["sha256"], "identity": list(identity), "parts": parts})
    manifest = {"schema": "selected-export-input-v1", "scope": scoped, "receipts": entries,
                "files": [{k: item[k] for k in ("path", "bytes", "sha256")}
                          for _, item in sorted(files.items())]}
    result = {"sha256": digest(encoded(manifest)), "messages": len(entries), "bytes": total,
              "receipts": entries, "files": list(files.values()),
              "directories": sorted(directories.items())}
    verify(root, result, readonly=readonly)
    require(expected is None or result["sha256"] == expected)
    if prior is not None:
        require(result == prior)
    return result
