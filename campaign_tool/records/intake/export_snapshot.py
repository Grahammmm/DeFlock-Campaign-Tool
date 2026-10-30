"""Explicit preserved-export byte manifest; no discovery or acquisition."""
from contextlib import contextmanager, ExitStack
import hashlib
import os
from pathlib import Path

from . import mail_delta
from ..container_host import PrivateDir, Rejected, _fingerprint, digest, encoded, require

MAX_SELECTION_BYTES = 1024**3


def receipt_paths(receipts):
    require(type(receipts) is tuple and 1 <= len(receipts) <= 60)
    for name in receipts:
        require(isinstance(name, str) and 0 < len(name) <= 4096
                and not name.startswith("/") and "\\" not in name
                and all(p not in ("", ".", "..") for p in name.split("/"))
                and not any(ord(c) < 32 or ord(c) == 127 for c in name))
    require(len(set(receipts)) == len(receipts))
    return receipts


@contextmanager
def _opened(root, name, readonly, signatures):
    """Hold component descriptors and check their named identities on exit."""
    with ExitStack() as stack:
        directory = stack.enter_context(PrivateDir(root))
        directories = [directory]
        for part in name.split("/")[:-1]:
            directory = stack.enter_context(PrivateDir(directory.path + "/" + part))
            directories.append(directory)
        before = tuple(_fingerprint(os.fstat(d.fd)) for d in directories)
        fd = directory.open_file(name.split("/")[-1])
        stack.callback(os.close, fd)
        value = _fingerprint(os.fstat(fd))
        if readonly:
            require(all(os.fstatvfs(d.fd).f_flag & os.ST_RDONLY for d in directories)
                    and os.fstatvfs(fd).f_flag & os.ST_RDONLY)
        yield fd, value
        for d, fingerprint in zip(directories, before):
            d.check()
            require(_fingerprint(os.fstat(d.fd)) == fingerprint)
        require(_fingerprint(os.fstat(fd)) == value
                and _fingerprint(os.stat(name.split("/")[-1], dir_fd=directory.fd,
                                         follow_symlinks=False)) == value)
        signatures.append((name, before, value))


def _hash(fd, size):
    hasher, count = hashlib.sha256(), 0
    while True:
        chunk = os.read(fd, min(1024 * 1024, size - count + 1))
        if not chunk:
            break
        count += len(chunk)
        require(count <= size)
        hasher.update(chunk)
    require(count == size)
    return hasher.hexdigest()


def snapshot(root, receipts, *, canonical_root=None, expected=None, readonly=False, prior=None):
    """Hash selected receipts and members, or recheck a prior stat signature."""
    try:
        receipt_paths(receipts)
        root = Path(os.path.abspath(root))
        output = Path(canonical_root) / "intake" if canonical_root is not None else root.parent / (root.name + ".not-output")
        entries, signatures, shared, total = [], [], {}, 0
        if prior is not None:
            require(tuple(e["receipt"] for e in prior["entries"]) == receipts)
            for entry in prior["entries"]:
                for name in (entry["receipt"], *(m["path"] for m in entry["members"])):
                    with _opened(root, name, readonly, signatures):
                        pass
            require(tuple(signatures) == prior["signature"])
            result = prior
        else:
            for name in receipts:
                with _opened(root, name, readonly, signatures) as (fd, fingerprint):
                    require(fingerprint[2] <= mail_delta.MAX_RECEIPT)
                    receipt_sha = _hash(fd, fingerprint[2])
                    _, _, items = mail_delta.load_receipt(root / name)
                total += fingerprint[2]
                entry = {"receipt": name, "receipt_sha256": receipt_sha,
                         "receipt_bytes": fingerprint[2], "members": []}
                for item in items:
                    path = mail_delta.source_path(root, item["path"], output)
                    relative = str(path.relative_to(root))
                    receipt_paths((relative,))
                    total += item["size"]
                    require(total <= MAX_SELECTION_BYTES)
                    with _opened(root, relative, readonly, signatures) as (fd, fingerprint):
                        require(fingerprint[2] == item["size"])
                        sha = _hash(fd, item["size"])
                        require(sha == item["sha"])
                        binding = (sha, fingerprint)
                        require(relative not in shared or shared[relative] == binding)
                        shared[relative] = binding
                    entry["members"].append({"part": item["part"], "path": relative,
                                             "sha256": sha, "bytes": item["size"]})
                require(total <= MAX_SELECTION_BYTES)
                entries.append(entry)
            result = {"sha256": digest(encoded(entries)), "receipts": len(entries),
                      "members": sum(len(e["members"]) for e in entries), "bytes": total,
                      "entries": entries, "signature": tuple(signatures)}
            # Catch changes to an earlier member while later receipts were parsed.
            snapshot(root, receipts, canonical_root=canonical_root, readonly=readonly, prior=result)
        require(expected is None or result["sha256"] == expected)
        return result
    except Exception:
        raise Rejected() from None
