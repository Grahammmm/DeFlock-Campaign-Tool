"""Offline folder preservation with source labels and private per-file receipts."""
import json
import os
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from . import cli


def _kind(name):
    ext = Path(name).suffix.lower()
    for kind, extensions in (("pdf", {".pdf"}), ("email", {".eml", ".msg", ".mbox"}),
                             ("image", {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".gif", ".webp"}),
                             ("spreadsheet", {".csv", ".xls", ".xlsx", ".ods"}),
                             ("document", {".txt", ".doc", ".docx", ".odt", ".rtf"})):
        if ext in extensions:
            return kind
    return "other"


def _entries(top_fd):
    entries, stack, subfolders = [], [()], set()
    while stack:
        parts = stack.pop()
        fd = cli._open_dir(top_fd, parts)
        try:
            with os.scandir(fd) as listing:
                for entry in listing:
                    hidden = entry.name.startswith(".")
                    if entry.is_dir(follow_symlinks=False) and not hidden:
                        if not parts:
                            subfolders.add(entry.name)
                        stack.append(parts + (entry.name,))
                        continue
                    code = ("hidden" if hidden else "symlink" if entry.is_symlink()
                            else "not_regular" if not entry.is_file(follow_symlinks=False)
                            else "too_large" if entry.stat(follow_symlinks=False).st_size > cli.MAX_BYTES else None)
                    entries.append(("/".join(parts + (entry.name,)), parts, entry.name, code))
                    if len(entries) > cli.MAX_DIR_FILES:
                        raise ValueError(f"More than {cli.MAX_DIR_FILES} files; split the folder")
        finally:
            os.close(fd)
    slugs, used = {}, {"_top"}
    for name in sorted(subfolders):
        base = re.sub("[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "_unnamed"
        slug, index = base, 1
        while slug in used:
            index += 1
            suffix = f"-{index}"
            slug = base[:40 - len(suffix)] + suffix
        slugs[name] = slug
        used.add(slug)
    return sorted(entries), slugs


def _write_receipt(root, stamp, label, receipt):
    directory = root / "private" / "imports"
    directory.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fchmod(fd, 0o700)
        index = 1
        while True:
            name = f"{stamp}-{label}" + (f"-{index}" if index > 1 else "") + ".json"
            try:
                out_fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=fd)
                break
            except FileExistsError:
                index += 1
        with os.fdopen(out_fd, "w", encoding="utf-8") as out:
            os.fchmod(out.fileno(), 0o600)
            json.dump(receipt, out, indent=2)
            out.write("\n")
    finally:
        os.close(fd)
    return f"private/imports/{name}"


def run(root, folder, label, now=None) -> dict:
    """Preserve local bytes; skip unsafe inputs and continue past per-file errors."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", label):
        raise ValueError("Label must be 1-40 lowercase letters, digits or hyphens, starting with a letter or digit")
    root, folder = Path(root).resolve(), Path(folder)
    cli.read_config(root)
    if folder.resolve().is_relative_to(root):
        raise ValueError("Import folder must not be inside the campaign directory")
    try:
        top_fd = cli._open_root(folder)
    except OSError:
        raise ValueError("Import folder must be a real directory, not a symlink or file") from None
    clock = (lambda: now.astimezone(timezone.utc)) if now is not None else lambda: datetime.now(timezone.utc)
    started = clock()
    files, stopped = [], False
    try:
        entries, slugs = _entries(top_fd)  # Finish inventory and enforce cap before writes.
        db = cli.database(root)
        try:
            for rel, parts, name, code in entries:
                source = f"import:{label}:" + (slugs[parts[0]] if parts else "_top")
                row = {"path": rel, "source_id": source, "sha256_prefix": None,
                       "outcome": code, "new_receipt": False, "kind": _kind(name)}
                if not code:
                    try:
                        fd = cli._open_dir(top_fd, parts)
                        try:
                            result = cli._ingest_one(db, root, name, source, dir_fd=fd)
                        finally:
                            os.close(fd)
                        row.update(sha256_prefix=result["sha256"][:12], outcome=result["outcome"],
                                   new_receipt=result["new_receipt"])
                    except (ValueError, OSError) as exc:
                        message = str(exc) if isinstance(exc, ValueError) else "Input could not be read"
                        # Existing ingest errors carry no input paths or original contents.
                        row.update(outcome="error", error=message)
                        stopped = "Stored object integrity mismatch" in message
                files.append(row)
                if stopped:
                    break
        finally:
            db.close()
    finally:
        os.close(top_fd)
    counts = {code: 0 for code in (*cli.OUTCOMES, "error")}
    counts.update(Counter(row["outcome"] for row in files))
    receipt = {"schema": "import-receipt-v1", "label": label, "folder_name": folder.resolve().name,
               "started_at": started.isoformat(), "finished_at": clock().isoformat(), "stopped": stopped,
               "files": files, "counts": counts, "sources": dict(Counter(row["source_id"] for row in files))}
    path = _write_receipt(root, started.strftime("%Y%m%dT%H%M%SZ"), label, receipt)
    return {key: value for key, value in receipt.items() if key != "files"} | {"receipt": path}
