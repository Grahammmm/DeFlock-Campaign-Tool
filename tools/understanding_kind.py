"""Offline kind-receipt CLI; reads ONLY an explicitly pinned extraction copy.

Usage (admitted synthetic/sanitized copies only):
  python3 tools/understanding_kind.py --copies-only --input-root INPUT_ROOT \
    --receipt extraction.json --derived-receipt-sha256 SHA --artifact-sha256 SHA \
    --run-id PRODUCER_ID --filename search-log.pdf --mime application/pdf \
    --output-root EXCLUSIVE_EXISTING_OUTPUT_ROOT

Creates a new kind-<uuid>/kind-receipt.json, never overwrites. Stdout reports only
its relative output path and exact file hash. Roots must be existing, disjoint,
immutable admitted directories, with no symlink ancestry. No acquisition,
original-content access, handoff rewrite or production acceptance is performed.
Concurrent hostile ancestor renames are outside this offline-copy contract.
"""
import argparse
import os
from pathlib import Path
import re
import stat
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from campaign_tool.records.understanding.kind import (
    KindError, MAX_RECEIPT_BYTES, canonical_bytes, classify, sha256,
)

_COMPONENT = re.compile(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*\Z")


def checked_root(raw):
    path = Path(raw)
    if not path.is_absolute() or ".." in path.parts:
        raise KindError("root_absolute_required")
    for part in reversed((path, *path.parents)):
        mode = part.lstat().st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise KindError("root_not_real_directory")
    return path


def checked_relative(root, raw):
    parts = raw.split("/")
    if not parts or any(_COMPONENT.fullmatch(part) is None for part in parts):
        raise KindError("receipt_path_invalid")
    path = root
    for index, part in enumerate(parts):
        path = path / part
        mode = path.lstat().st_mode
        expected = stat.S_ISREG(mode) if index == len(parts) - 1 else stat.S_ISDIR(mode)
        if stat.S_ISLNK(mode) or not expected:
            raise KindError("receipt_path_not_regular")
    return path


def read_receipt(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_RECEIPT_BYTES:
            raise KindError("receipt_file_bound")
        chunks, size = [], 0
        while True:
            block = os.read(fd, min(65536, MAX_RECEIPT_BYTES - size + 1))
            if not block:
                break
            chunks.append(block)
            size += len(block)
            if size > MAX_RECEIPT_BYTES:
                raise KindError("receipt_file_bound")
        after = os.fstat(fd)
        identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if identity(before) != identity(after):
            raise KindError("receipt_file_changed")
        return b"".join(chunks)
    finally:
        os.close(fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--copies-only", action="store_true", required=True)
    for name in ("input-root", "receipt", "derived-receipt-sha256", "artifact-sha256", "run-id", "output-root"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--filename", default="")
    parser.add_argument("--mime", default="")
    args = parser.parse_args(argv)
    try:
        # All path validation precedes reads; classify before creating anything.
        source = checked_root(args.input_root)
        output = checked_root(args.output_root)
        if source == output or source in output.parents or output in source.parents:
            raise KindError("input_output_overlap")
        path = checked_relative(source, args.receipt)
        receipt = classify(run_id=args.run_id, artifact_sha256=args.artifact_sha256,
                           derived_receipt_sha256=args.derived_receipt_sha256,
                           derived_receipt=read_receipt(path), filename=args.filename, mime=args.mime)
        raw = canonical_bytes(receipt)
        run = output / ("kind-" + uuid.uuid4().hex)
        run.mkdir(mode=0o700)
        target = run / "kind-receipt.json"
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        print(canonical_bytes({"path": target.relative_to(output).as_posix(),
                               "sha256": sha256(raw), "review_required": True}).decode(), end="")
        return 0
    except (KindError, OSError, UnicodeError) as exc:
        # Never echo hostile path/text or OS exception details.
        code = str(exc) if isinstance(exc, KindError) else "offline_io_refused"
        print(canonical_bytes({"error": code, "review_required": True}).decode(), end="")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
