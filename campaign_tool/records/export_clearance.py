"""Private indexed clearance for one exact UTF-8 source export; never uploads."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import unicodedata

from . import public_scan

MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_INDEX_BYTES = 8 * 1024 * 1024
MAX_WINDOWS = 500000
WINDOW = 24
ARCHIVE_SUFFIXES = {".zip", ".whl", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar"}
COVERAGE_KEYS = {"schema_version", "inventory_sha256", "originals_complete",
                 "portal_hosts_complete", "correspondence_complete",
                 "correspondence_source_sha256"}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def valid_sha(value):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def normalize(value):
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def decode(raw):
    return json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError("invalid_json")))


def checked_path(value):
    path = Path(os.path.abspath(value))
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("symlink_path")
    return path


def _outside_checkout(path):
    if any((parent / ".git").exists() for parent in (path, *path.parents)):
        raise ValueError("private_gate_requires_paths_outside_git")


def private_external(value, root, *, existing):
    path = checked_path(value)
    if path == root or root in path.parents:
        raise ValueError("private_material_inside_export")
    _outside_checkout(path)
    parent = path.parent.stat()
    if parent.st_uid != os.geteuid() or parent.st_mode & 0o077:
        raise ValueError("private_parent_required")
    if existing or path.exists():
        info = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError("private_file_required")
    return path


def identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, info.st_mode)


def read_stable(path, limit):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise ValueError("unsupported_or_oversize_file")
        raw = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    if len(raw) > limit or identity(before) != identity(after) or identity(after) != identity(path.stat()):
        raise ValueError("file_changed_during_scan")
    return raw, after


def coverage_windows(index):
    public_scan.validate_private_index(index)
    coverage = index.get("coverage")
    if type(coverage) is not dict or set(coverage) != COVERAGE_KEYS:
        raise ValueError("missing_or_invalid_coverage")
    if type(coverage["schema_version"]) is not int or coverage["schema_version"] != 1:
        raise ValueError("invalid_coverage_version")
    if not valid_sha(coverage["inventory_sha256"]):
        raise ValueError("invalid_inventory_binding")
    if any(coverage[name] is not True for name in
           ("originals_complete", "portal_hosts_complete", "correspondence_complete")):
        raise ValueError("incomplete_private_coverage")
    originals = index.get("original_sha256")
    hosts = index.get("portal_hosts")
    if type(originals) is not list or type(hosts) is not list or len(set(originals)) != len(originals):
        raise ValueError("missing_or_duplicate_private_inventory")
    expected = coverage["correspondence_source_sha256"]
    sources = index.get("correspondence_sources")
    if (type(expected) is not list or any(not valid_sha(item) for item in expected)
            or len(set(expected)) != len(expected) or not set(expected) <= set(originals)
            or type(sources) is not list):
        raise ValueError("invalid_correspondence_coverage")
    seen, windows, short = set(), set(), set()
    window_count = 0
    for source in sources:
        if type(source) is not dict or set(source) != {"source_sha256", "text"}:
            raise ValueError("invalid_correspondence_source")
        source_sha = source["source_sha256"]
        if not valid_sha(source_sha) or source_sha in seen or source_sha not in expected:
            raise ValueError("invalid_correspondence_source_binding")
        if type(source["text"]) is not str:
            raise ValueError("invalid_correspondence_text")
        text = normalize(source["text"])
        if not text:
            raise ValueError("empty_correspondence_coverage")
        seen.add(source_sha)
        window_count += max(1, len(text) - WINDOW + 1)
        if window_count > MAX_WINDOWS:
            raise ValueError("correspondence_index_resource_limit")
        if len(text) < WINDOW:
            short.add(text)
        else:
            windows.update(text[pos:pos + WINDOW] for pos in range(len(text) - WINDOW + 1))
    if seen != set(expected):
        raise ValueError("missing_correspondence_sources")
    return windows, short


def _paths(root):
    paths = sorted(root.rglob("*"))
    for path in paths:
        relative = path.relative_to(root)
        if ".git" in relative.parts:
            raise ValueError("git_metadata_in_export")
        if any(ord(char) < 32 or char == "\\" for char in relative.as_posix()):
            raise ValueError("unsafe_export_name")
        if path.is_symlink():
            raise ValueError("symlink_in_export")
    return paths


def scan_export(root, index):
    windows, short = coverage_windows(index)
    inventory, findings, non_content, identities = {}, [], [], {}
    paths = _paths(root)
    file_count = 0
    for path in paths:
        relative = path.relative_to(root).as_posix()
        info = path.stat(follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            findings.extend(public_scan.violations(relative, b"", strict=True, private_index=index))
            inventory[relative] = {"kind": "directory", "mode": stat.S_IMODE(info.st_mode)}
        elif stat.S_ISREG(info.st_mode):
            raw, info = read_stable(path, MAX_FILE_BYTES)
            inventory[relative] = {"kind": "file", "mode": stat.S_IMODE(info.st_mode),
                                   "bytes": len(raw), "sha256": sha(raw)}
            file_count += 1
            findings.extend(public_scan.violations(relative, raw, strict=True, private_index=index))
            if not raw and sha(raw) in index["original_sha256"]:
                non_content.append({"path": relative, "classification": "zero_byte_original_non_content"})
            if path.suffix.lower() in ARCHIVE_SUFFIXES or raw.startswith((b"PK\x03\x04", b"\x1f\x8b")):
                findings.append((relative, 0, "archive_content_not_scanned"))
            try:
                text = normalize(raw.decode("utf-8"))
            except UnicodeDecodeError:
                findings.append((relative, 0, "non_utf8_content_not_scanned"))
            else:
                if (any(value in text for value in short)
                        or any(text[pos:pos + WINDOW] in windows for pos in range(max(0, len(text) - WINDOW + 1)))):
                    findings.append((relative, 0, "private_correspondence_normalized"))
        else:
            raise ValueError("special_file_in_export")
        identities[path] = identity(info)
    if not file_count:
        raise ValueError("empty_export")
    if paths != _paths(root) or any(identity(path.stat(follow_symlinks=False)) != value for path, value in identities.items()):
        raise ValueError("export_changed_during_scan")
    return inventory, findings, non_content, file_count


def scanner_binding():
    return {path.name: sha(path.read_bytes()) for path in
            (Path(__file__), Path(public_scan.__file__))}


def clearance(root, index_path, receipt_path, *, verify=False):
    root = checked_path(root)
    if not root.is_dir():
        raise ValueError("export_directory_required")
    _outside_checkout(root)
    index_path = private_external(index_path, root, existing=True)
    receipt_path = private_external(receipt_path, root, existing=verify)
    if index_path == receipt_path:
        raise ValueError("receipt_overlaps_index")
    raw_index, index_info = read_stable(index_path, MAX_INDEX_BYTES)
    index = decode(raw_index)
    inventory, findings, non_content, file_count = scan_export(root, index)
    if identity(index_path.stat()) != identity(index_info):
        raise ValueError("index_changed_during_scan")
    result = {"private_export_clearance": not findings, "findings": findings,
              "scanned_files": file_count, "non_content_matches": non_content,
              "export_sha256": sha(canonical(inventory)),
              "limits": "Declared-index literal coverage only; not semantic privacy review, signature, or publication approval."}
    if findings:
        return result
    attestation = {"schema_version": 1, "kind": "private_indexed_exact_export",
                   "export_sha256": result["export_sha256"], "inventory": inventory,
                   "private_index_sha256": sha(raw_index),
                   "coverage_sha256": sha(canonical(index["coverage"])),
                   "scanner_bindings": scanner_binding(),
                   "non_content_matches": non_content, "publication_approved": False}
    encoded = canonical(attestation) + b"\n"
    if verify:
        saved, _ = read_stable(receipt_path, MAX_FILE_BYTES)
        if saved != encoded:
            raise ValueError("stale_or_mismatched_attestation")
    else:
        fd = os.open(receipt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        directory_fd = os.open(receipt_path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    result["attestation_verified"] = verify
    return result
