"""Bounded local OCR of preserved PDF pages; extraction is never review."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import csv
import ctypes
import errno
import fcntl
import hashlib
import importlib
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import time
import uuid

VERSION = "local-ocr-2"
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
ARTIFACTS = ("page.pdf", "page.png", "sidecar.txt", "searchable.pdf", "confidence.tsv")
RECEIPT_KEYS = {"receipt_id", "identity", "source", "locator", "page_count", "status",
                "blocked_reason", "confidence", "confidence_basis", "visual_check_required",
                "visual_check_status", "fidelity_status", "review_status", "artifact_sha256",
                "published_at_ns", "tool_versions"}
STATES = {"blocked", "skipped_machine_text", "ocr_text_unreviewed", "visual_check_queued"}
O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
OCR_TOOLS = ("ocrmypdf", "tesseract", "pdftoppm", "gs")
RASTER_DPI = 300
MAX_RASTER_PIXELS = 40_000_000
# Mirrors the intake worker's child bounds (folder.LIMITS memory_bytes/source_bytes).
CHILD_MEMORY_BYTES = 3 * 1024 ** 3
CHILD_FILE_BYTES = 512 * 1024 ** 2


def _json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("ascii")


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _directory(path: Path, *, create: bool = False) -> int:
    """Open every component without following links; caller closes the fd."""
    absolute = Path(os.path.abspath(path))
    fd = os.open("/", os.O_RDONLY | O_DIRECTORY)
    try:
        for part in absolute.parts[1:]:
            if part in {".", ".."}:
                raise ValueError("unsafe_directory_component")
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            try:
                next_fd = os.open(part, os.O_RDONLY | O_DIRECTORY | O_NOFOLLOW, dir_fd=fd)
            except OSError as error:
                if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise ValueError("symlink_or_nondirectory_rejected") from error
                raise
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _managed_directory(path: Path, *, create: bool = False) -> int:
    """Require owner-controlled output, without policing intake ancestors."""
    fd = _directory(path, create=create)
    info = os.fstat(fd)
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
        os.close(fd)
        raise ValueError("managed_output_directory_not_owner_only")
    return fd


@contextmanager
def _page_lock(page_dir: Path):
    parent = _managed_directory(page_dir)
    try:
        fd = os.open(".page.lock", os.O_RDWR | os.O_CREAT | O_NOFOLLOW, 0o600, dir_fd=parent)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
            os.close(fd)
            raise ValueError("page_lock_not_owner_only")
    finally:
        os.close(parent)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _read(path: Path, limit: int = 512 * 1024 * 1024) -> bytes:
    parent = _directory(path.parent)
    try:
        try:
            fd = os.open(path.name, os.O_RDONLY | O_NOFOLLOW, dir_fd=parent)
        except OSError as error:
            if error.errno == errno.ELOOP:
                raise ValueError("symlink_file_rejected") from error
            raise
        with os.fdopen(fd, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ValueError("nonregular_file_rejected")
            raw = source.read(limit + 1)
        if len(raw) > limit:
            raise ValueError("file_size_limit")
        return raw
    finally:
        os.close(parent)


def _write_new(path: Path, content: bytes) -> None:
    parent = _directory(path.parent)
    try:
        fd = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | O_NOFOLLOW, 0o600, dir_fd=parent)
        with os.fdopen(fd, "wb") as target:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        os.fsync(parent)
    finally:
        os.close(parent)


def _link_new(source: Path, destination: Path) -> bool:
    source_parent = _directory(source.parent)
    dest_parent = _directory(destination.parent)
    try:
        try:
            os.link(source.name, destination.name, src_dir_fd=source_parent,
                    dst_dir_fd=dest_parent, follow_symlinks=False)
        except FileExistsError:
            return False
        os.fsync(dest_parent)
        return True
    finally:
        os.close(source_parent)
        os.close(dest_parent)


def _publish_directory(source: Path, destination: Path) -> bool:
    """Linux no-replace rename; never replace a concurrent receipt directory."""
    parent = _directory(source.parent)
    try:
        if source.parent != destination.parent:
            raise ValueError("publication_parent_mismatch")
        rename = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
        if rename is None:
            raise OSError(errno.ENOSYS, "no_replace_rename_unavailable")
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        result = rename(parent, os.fsencode(source.name), parent, os.fsencode(destination.name), 1)
        if result:
            code = ctypes.get_errno()
            if code == errno.EEXIST:
                return False
            raise OSError(code, os.strerror(code))
        os.fsync(parent)
        return True
    finally:
        os.close(parent)


def _confidence(tsv: str) -> float | None:
    values = []
    for row in csv.DictReader(io.StringIO(tsv), delimiter="\t"):
        if not row.get("text", "").strip():
            continue
        try:
            value = float(row["conf"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= value <= 100:
            values.append(value)
    return round(sum(values) / len(values), 2) if values else None


def _limit_child() -> None:
    """preexec_fn for OCR children: bound address space and written file size."""
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (CHILD_MEMORY_BYTES, CHILD_MEMORY_BYTES))
    resource.setrlimit(resource.RLIMIT_FSIZE, (CHILD_FILE_BYTES, CHILD_FILE_BYTES))
    os.umask(0o077)


def _raster_pixels(page) -> float:
    """Pixels pdftoppm would allocate for the page MediaBox at RASTER_DPI."""
    try:
        box = page.mediabox
        width, height = abs(float(box.width)), abs(float(box.height))
        unit = float(getattr(page, "user_unit", 1) or 1)
    except Exception as error:
        raise PageFailure("page_geometry_unavailable") from error
    if not all(math.isfinite(v) and v > 0 for v in (width, height, unit)):
        raise PageFailure("page_geometry_unavailable")
    scale = RASTER_DPI / 72 * unit
    return width * height * scale * scale


def _resolved_tools(tools: dict | None) -> tuple[dict, dict]:
    """Doctor-resolved absolute executables and probed versions; never PATH lookups."""
    if not isinstance(tools, dict):
        raise ValueError("tools must be the doctor-resolved tool map")
    paths, versions = {}, {}
    for name in OCR_TOOLS:
        entry = tools.get(name)
        path = entry.get("path") if isinstance(entry, dict) else None
        version = entry.get("version") if isinstance(entry, dict) else None
        if not isinstance(path, str) or not os.path.isabs(path):
            raise ValueError("tools must give an absolute path for " + name)
        if not isinstance(version, str) or not version:
            raise ValueError("tools must give a probed version for " + name)
        paths[name] = path
        versions[name] = version
    pypdf = tools.get("pypdf")
    if isinstance(pypdf, dict) and isinstance(pypdf.get("version"), str) and pypdf["version"]:
        versions["pypdf"] = pypdf["version"]
    return paths, versions


def _run(runner, command: list[str], timeout: int, env: dict | None = None):
    result = runner(command, capture_output=True, text=True, timeout=timeout, check=False,
                    preexec_fn=_limit_child, env=env)
    if result.returncode:
        raise RuntimeError("ocr_command_failed")
    return result


def _hold(page_dir: Path, source_sha256: str, page: int, requested: bool) -> bool:
    marker = page_dir / "fidelity-hold.json"
    if requested:
        staging = page_dir / (".hold-" + uuid.uuid4().hex)
        try:
            _write_new(staging, _json({"source_sha256": source_sha256, "locator": {"page": page},
                                       "reason": "fidelity_hold_requires_visual_comparison"}))
            _link_new(staging, marker)
        finally:
            staging.unlink(missing_ok=True)
    try:
        record = json.loads(_read(marker))
    except FileNotFoundError:
        return False
    if record != {"source_sha256": source_sha256, "locator": {"page": page},
                  "reason": "fidelity_hold_requires_visual_comparison"}:
        raise ValueError("fidelity_hold_marker_mismatch")
    return True


def _validate_receipt(receipt: dict, identity: dict, directory: Path) -> tuple[str, str]:
    if not isinstance(receipt, dict) or set(receipt) != RECEIPT_KEYS:
        raise ValueError("ocr_receipt_schema_mismatch")
    receipt_id = _hash(_json(identity))
    if (receipt["identity"] != identity or receipt["receipt_id"] != receipt_id
            or (SHA256.fullmatch(directory.name) and directory.name != receipt_id)
            or type(receipt["published_at_ns"]) is not int or receipt["published_at_ns"] <= 0
            or receipt["source"] != {"kind": "preserved_blob", "sha256": identity["source_sha256"]}
            or receipt["locator"] != {"page": identity["page"]}
            or type(receipt["page_count"]) is not int or receipt["page_count"] < identity["page"]
            or receipt["status"] not in STATES or receipt["review_status"] != "not_reviewed"
            or receipt["visual_check_status"] != "not_done" or receipt["fidelity_status"] != "not_checked"
            or receipt["confidence_basis"] != "tesseract_word_tsv_mean"
            or type(receipt["visual_check_required"]) is not bool
            or receipt["visual_check_required"] != (receipt["status"] == "visual_check_queued")):
        raise ValueError("ocr_receipt_state_mismatch")
    blocked = receipt["status"] == "blocked"
    if blocked != (isinstance(receipt["blocked_reason"], str) and bool(receipt["blocked_reason"])):
        raise ValueError("ocr_receipt_blocked_reason_mismatch")
    confidence = receipt["confidence"]
    if confidence is not None and (type(confidence) not in {int, float} or not 0 <= confidence <= 100):
        raise ValueError("ocr_receipt_confidence_mismatch")
    versions = receipt["tool_versions"]
    if (not isinstance(versions, dict) or not set(OCR_TOOLS) <= set(versions)
            or any(not isinstance(k, str) or not isinstance(v, str) or not v for k, v in versions.items())):
        raise ValueError("ocr_receipt_tool_versions_mismatch")
    if not blocked and receipt["status"] != "skipped_machine_text" and confidence is None:
        raise ValueError("ocr_receipt_confidence_missing")
    hashes = receipt["artifact_sha256"]
    if not isinstance(hashes, dict) or any(not isinstance(value, str) or not SHA256.fullmatch(value) for value in hashes.values()):
        raise ValueError("ocr_receipt_artifact_schema_mismatch")
    names = set(hashes)
    if receipt["status"] in {"ocr_text_unreviewed", "visual_check_queued"} and names != set(ARTIFACTS):
        raise ValueError("ocr_receipt_artifact_set_mismatch")
    if receipt["status"] == "skipped_machine_text" and (names or confidence is not None):
        raise ValueError("ocr_receipt_skip_mismatch")
    if blocked and names not in (set(ARTIFACTS[:n]) for n in range(len(ARTIFACTS) + 1)):
        raise ValueError("ocr_receipt_artifact_set_mismatch")
    allowed = names | {"receipt.json", "manifest.json"}
    if any(entry.is_symlink() or entry.name not in allowed for entry in directory.iterdir()):
        raise ValueError("ocr_receipt_unexpected_or_symlink_artifact")
    for name, expected in hashes.items():
        if _hash(_read(directory / name, 128 * 1024 * 1024)) != expected:
            raise ValueError("existing_ocr_artifact_mismatch")
    return receipt_id, _hash(_json(receipt))


def _load_receipt(directory: Path, identity: dict) -> tuple[dict, str, str] | None:
    try:
        os.close(_managed_directory(directory))
    except FileNotFoundError:
        return None
    try:
        manifest_bytes = _read(directory / "manifest.json")
        receipt_bytes = _read(directory / "receipt.json")
    except FileNotFoundError:
        parent = _directory(directory.parent)
        try:
            try:
                os.stat(directory.name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                return None
        finally:
            os.close(parent)
        raise ValueError("existing_ocr_receipt_incomplete")
    manifest = json.loads(manifest_bytes)
    receipt = json.loads(receipt_bytes)
    receipt_id, receipt_sha = _validate_receipt(receipt, identity, directory)
    if (manifest != {"schema_version": VERSION, "receipt_sha256": receipt_sha,
                     "artifact_sha256": receipt["artifact_sha256"]}
            or receipt_bytes != _json(receipt) or manifest_bytes != _json(manifest)):
        raise ValueError("existing_ocr_manifest_mismatch")
    return receipt, receipt_sha, _hash(manifest_bytes)


def _index(output: Path, receipt: dict, receipt_sha: str, manifest_sha: str) -> None:
    state = receipt["status"]
    if state not in {"blocked", "visual_check_queued"}:
        return
    kind = "blocked" if state == "blocked" else "visual-check"
    directory = output / "indexes" / kind
    os.close(_managed_directory(output / "indexes", create=True))
    fd = _managed_directory(directory, create=True)
    os.close(fd)
    destination = directory / (receipt["receipt_id"] + ".json")
    entry = {"source_sha256": receipt["source"]["sha256"], "locator": receipt["locator"],
             "receipt_id": receipt["receipt_id"], "receipt_sha256": receipt_sha,
             "manifest_sha256": manifest_sha, "status": state,
             "blocked_reason": receipt["blocked_reason"], "confidence": receipt["confidence"]}
    staging = directory / (".index-" + uuid.uuid4().hex)
    try:
        _write_new(staging, _json(entry))
        if not _link_new(staging, destination) and _read(destination) != _json(entry):
            raise ValueError("ocr_index_conflict")
    finally:
        staging.unlink(missing_ok=True)


def _reconcile_page(page_dir: Path, source_sha256: str, page: int, output: Path) -> int:
    """Repair indexes from immutable receipts and refresh the active pointer."""
    history = []
    for entry in page_dir.iterdir():
        if not SHA256.fullmatch(entry.name):
            continue
        raw = json.loads(_read(entry / "receipt.json"))
        identity = raw.get("identity")
        if (not isinstance(identity, dict) or identity.get("source_sha256") != source_sha256
                or identity.get("page") != page or identity.get("version") != VERSION
                or not isinstance(identity.get("attempt_id"), str)):
            raise ValueError("published_ocr_identity_mismatch")
        loaded = _load_receipt(entry, identity)
        if loaded is None:
            raise ValueError("published_ocr_receipt_missing")
        receipt, receipt_sha, manifest_sha = loaded
        _index(output, receipt, receipt_sha, manifest_sha)
        history.append((receipt["published_at_ns"], receipt["receipt_id"], receipt,
                        receipt_sha, manifest_sha))
    held = _hold(page_dir, source_sha256, page, False)
    if not history and not held:
        return 0
    history.sort(key=lambda row: (row[0], row[1]))
    eligible = [row for row in history if row[2]["identity"]["fidelity_hold"]] if held else history
    chosen = eligible[-1] if eligible else None
    current = {"source_sha256": source_sha256, "locator": {"page": page},
               "disposition": "active", "status": chosen[2]["status"] if chosen else "blocked",
               "blocked_reason": ("fidelity_hold_requires_visual_comparison" if held
                                  else chosen[2]["blocked_reason"] if chosen else "no_published_receipt"),
               "receipt_id": chosen[1] if chosen else None,
               "receipt_sha256": chosen[3] if chosen else None,
               "manifest_sha256": chosen[4] if chosen else None,
               "hold_marker_sha256": _hash(_read(page_dir / "fidelity-hold.json")) if held else None,
               "supersedes_receipt_ids": [row[1] for row in history if chosen is None or row[1] != chosen[1]]}
    destination = page_dir / "current.json"
    encoded = _json(current)
    try:
        if _read(destination) == encoded:
            return len(history)
    except FileNotFoundError:
        pass
    staging = page_dir / (".current-" + uuid.uuid4().hex)
    _write_new(staging, encoded)
    parent = _managed_directory(page_dir)
    try:
        os.replace(staging.name, destination.name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
    finally:
        os.close(parent)
    return len(history)


def reconcile_pages(output_root: str | Path, source_sha256: str) -> dict:
    """Rebuild private indexes/current dispositions; no OCR or source reads."""
    if not SHA256.fullmatch(source_sha256):
        raise ValueError("source_sha256 must be a lowercase SHA-256")
    output = Path(os.path.abspath(output_root))
    os.close(_managed_directory(output))
    os.close(_managed_directory(output / "indexes", create=True))
    source_dir = output / source_sha256
    os.close(_managed_directory(source_dir))
    pages = 0
    receipts = 0
    for page_dir in source_dir.iterdir():
        if not re.fullmatch(r"page-[0-9]{6}", page_dir.name):
            continue
        os.close(_managed_directory(page_dir))
        page = int(page_dir.name[5:])
        if page < 1:
            raise ValueError("invalid_ocr_page_directory")
        with _page_lock(page_dir):
            receipts += _reconcile_page(page_dir, source_sha256, page, output)
        pages += 1
    return {"source_sha256": source_sha256, "pages": pages,
            "published_receipts": receipts, "status": "reconciled"}


class PageFailure(Exception):
    """A bounded page-local extraction failure."""


def _publish_blocked_disposition(page_dir: Path, output: Path, base: dict,
                                 reason: str, source_sha256: str, page: int) -> dict:
    """Record an operational failure independently of an earlier page receipt."""
    identity = dict(base["identity"])
    identity["attempt_id"] = "recovery-" + _hash(_json(identity) + reason.encode("ascii"))[:24]
    identity["fidelity_hold"] = _hold(page_dir, source_sha256, page, False)
    receipt = {**base, "identity": identity, "receipt_id": _hash(_json(identity)),
               "status": "blocked", "blocked_reason": reason, "confidence": None,
               "visual_check_required": False, "artifact_sha256": {},
               "published_at_ns": time.time_ns()}
    temporary = Path(tempfile.mkdtemp(prefix=".recovery-", dir=page_dir))
    try:
        os.close(_managed_directory(temporary))
        _validate_receipt(receipt, identity, temporary)
        receipt_bytes = _json(receipt)
        manifest = {"schema_version": VERSION, "receipt_sha256": _hash(receipt_bytes),
                    "artifact_sha256": {}}
        _write_new(temporary / "receipt.json", receipt_bytes)
        _write_new(temporary / "manifest.json", _json(manifest))
        final = page_dir / receipt["receipt_id"]
        if not _publish_directory(temporary, final):
            winner = _load_receipt(final, identity)
            if winner is None:
                raise ValueError("recovery_receipt_conflict")
            receipt = winner[0]
        _reconcile_page(page_dir, source_sha256, page, output)
        return receipt
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)



def _prune_unrecorded(directory: Path, artifacts: dict) -> str | None:
    reason = None
    for entry in directory.iterdir():
        if entry.name in artifacts:
            continue
        if entry.is_symlink():
            reason = "page_derivative_symlink_rejected"
            entry.unlink()
        elif entry.is_file():
            entry.unlink()
        elif entry.is_dir():
            reason = reason or "unexpected_page_derivative_directory"
            shutil.rmtree(entry)
        else:
            raise ValueError("unexpected_ocr_file_type")
    return reason


def extract_image_only_pages(
    intake_root: str | Path, source_sha256: str, output_root: str | Path, *,
    tool_signature: str, tools: dict | None = None, attempt_id: str = "initial",
    pages: tuple[int, ...] | None = None,
    fidelity_holds: tuple[int, ...] = (), language: str = "eng",
    low_confidence: float = 80.0, timeout: int = 180, runner=subprocess.run,
) -> list[dict]:
    """Persist page receipts for a preserved PDF, including failures and holds.

    ``tools`` is the ``dependency_status()["tools"]`` map: children are invoked by
    those absolute paths and the probed versions are recorded in every receipt.
    """
    if not SHA256.fullmatch(source_sha256):
        raise ValueError("source_sha256 must be a lowercase SHA-256")
    if not isinstance(tool_signature, str) or not re.fullmatch(r"[\w .+/-]{1,120}", tool_signature):
        raise ValueError("tool_signature must be an operator-pinned version label")
    if not isinstance(attempt_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", attempt_id):
        raise ValueError("attempt_id must be a safe 1-64 character label")
    if not isinstance(language, str) or not re.fullmatch(r"[a-zA-Z0-9_+]{1,40}", language):
        raise ValueError("invalid OCR language")
    if type(low_confidence) not in {int, float} or not 0 <= low_confidence <= 100 or type(timeout) is not int or timeout <= 0:
        raise ValueError("invalid OCR threshold or timeout")
    tool_paths, tool_versions = _resolved_tools(tools)
    search = []
    for name in OCR_TOOLS:
        directory = os.path.dirname(tool_paths[name])
        if directory not in search:
            search.append(directory)
    # ocrmypdf locates tesseract and gs itself; put the doctor-resolved copies first.
    child_env = {**os.environ, "PATH": os.pathsep.join(search + [os.environ.get("PATH", "")])}
    raw = _read(Path(intake_root) / "blobs" / source_sha256)
    if _hash(raw) != source_sha256:
        raise ValueError("preserved_blob_hash_mismatch")
    from pypdf import PdfReader, PdfWriter
    reader = PdfReader(io.BytesIO(raw), strict=False)
    if reader.is_encrypted and not reader.decrypt(""):
        raise ValueError("encrypted_pdf_requires_user_password")
    count = len(reader.pages)
    selected = tuple(range(1, count + 1)) if pages is None else tuple(pages)
    holds = set(fidelity_holds)
    if (len(set(selected)) != len(selected) or any(type(n) is not int or n < 1 or n > count for n in selected)
            or any(type(n) is not int or n < 1 or n > count for n in holds)):
        raise ValueError("invalid or duplicate page selector")
    output = Path(os.path.abspath(output_root))
    os.close(_managed_directory(output, create=True))
    for index_dir in (output / "indexes", output / "indexes" / "blocked",
                      output / "indexes" / "visual-check"):
        os.close(_managed_directory(index_dir, create=True))
    receipts = []
    for number in selected:
        source_dir = output / source_sha256
        os.close(_managed_directory(source_dir, create=True))
        page_dir = source_dir / f"page-{number:06d}"
        os.close(_managed_directory(page_dir, create=True))
        with _page_lock(page_dir):
            held = _hold(page_dir, source_sha256, number, number in holds)
            _reconcile_page(page_dir, source_sha256, number, output)
            identity = {"source_sha256": source_sha256, "page": number, "version": VERSION,
                        "tool_signature": tool_signature, "attempt_id": attempt_id,
                        "language": language, "low_confidence": low_confidence,
                        "fidelity_hold": held}
            receipt_id = _hash(_json(identity))
            final = page_dir / receipt_id
            prior = _load_receipt(final, identity)
            if prior is not None:
                receipt, receipt_sha, manifest_sha = prior
                _index(output, receipt, receipt_sha, manifest_sha)
                receipts.append(receipt)
                continue
        temporary = Path(tempfile.mkdtemp(prefix=".attempt-", dir=page_dir))
        os.close(_managed_directory(temporary))
        artifacts = {}
        receipt = {"receipt_id": receipt_id, "identity": identity,
                   "source": {"kind": "preserved_blob", "sha256": source_sha256},
                   "locator": {"page": number}, "page_count": count,
                   "status": "blocked", "blocked_reason": None, "confidence": None,
                   "confidence_basis": "tesseract_word_tsv_mean", "visual_check_required": False,
                   "visual_check_status": "not_done", "fidelity_status": "not_checked",
                   "review_status": "not_reviewed", "artifact_sha256": artifacts,
                   "published_at_ns": 0, "tool_versions": dict(tool_versions)}
        try:
            if held:
                receipt["blocked_reason"] = "fidelity_hold_requires_visual_comparison"
            else:
                try:
                    machine_text = reader.pages[number - 1].extract_text() or ""
                except Exception:
                    receipt["blocked_reason"] = "page_text_probe_failed"
                else:
                    if machine_text.strip():
                        receipt["status"] = "skipped_machine_text"
                    else:
                        if _raster_pixels(reader.pages[number - 1]) > MAX_RASTER_PIXELS:
                            raise PageFailure("page_raster_bound")
                        page_pdf = temporary / "page.pdf"
                        try:
                            writer = PdfWriter()
                            writer.add_page(reader.pages[number - 1])
                            buffer = io.BytesIO()
                            writer.write(buffer)
                        except Exception as error:
                            raise PageFailure("page_writer_failed") from error
                        _write_new(page_pdf, buffer.getvalue())
                        artifacts["page.pdf"] = _hash(_read(page_pdf))
                        _run(runner, [tool_paths["pdftoppm"], "-f", "1", "-l", "1", "-singlefile",
                                      "-r", str(RASTER_DPI), "-png", str(page_pdf),
                                      str(temporary / "page")], timeout, child_env)
                        image = temporary / "page.png"
                        artifacts["page.png"] = _hash(_read(image, 128 * 1024 * 1024))
                        _run(runner, [tool_paths["ocrmypdf"], "--jobs", "1", "--output-type", "pdf",
                                      "-l", language, "--sidecar", str(temporary / "sidecar.txt"),
                                      str(page_pdf), str(temporary / "searchable.pdf")], timeout, child_env)
                        sidecar = _read(temporary / "sidecar.txt", 128 * 1024 * 1024)
                        searchable = _read(temporary / "searchable.pdf", 128 * 1024 * 1024)
                        artifacts["sidecar.txt"] = _hash(sidecar)
                        artifacts["searchable.pdf"] = _hash(searchable)
                        probe = _run(runner, [tool_paths["tesseract"], str(image), "stdout", "-l",
                                              language, "tsv"], timeout, child_env)
                        _write_new(temporary / "confidence.tsv", probe.stdout.encode("utf-8"))
                        artifacts["confidence.tsv"] = _hash(_read(temporary / "confidence.tsv"))
                        receipt["confidence"] = _confidence(probe.stdout)
                        if not sidecar.decode("utf-8").strip():
                            receipt["blocked_reason"] = "no_text_after_ocr"
                        elif receipt["confidence"] is None:
                            receipt["blocked_reason"] = "confidence_unavailable"
                        elif receipt["confidence"] < low_confidence:
                            receipt["status"] = "visual_check_queued"
                            receipt["visual_check_required"] = True
                        else:
                            receipt["status"] = "ocr_text_unreviewed"
        except PageFailure as error:
            receipt["blocked_reason"] = str(error)
        except FileNotFoundError:
            receipt["blocked_reason"] = "local_ocr_dependency_or_output_missing"
        except subprocess.TimeoutExpired:
            receipt["blocked_reason"] = "local_ocr_timeout"
        except RuntimeError as error:
            receipt["blocked_reason"] = str(error)
        except (ValueError, OSError):
            receipt["blocked_reason"] = "page_derivative_failed_or_unsafe"
        if receipt["status"] == "blocked" and receipt["blocked_reason"] is None:
            receipt["blocked_reason"] = "page_ocr_unresolved"
        outcome = None
        publication_failed = None
        cleanup_failed = False
        try:
            with _page_lock(page_dir):
                if _hold(page_dir, source_sha256, number, False) and not identity["fidelity_hold"]:
                    identity = {**identity, "fidelity_hold": True}
                    receipt["identity"] = identity
                    receipt["receipt_id"] = _hash(_json(identity))
                    receipt["status"] = "blocked"
                    receipt["blocked_reason"] = "fidelity_hold_requires_visual_comparison"
                    receipt["confidence"] = None
                    receipt["visual_check_required"] = False
                    artifacts.clear()
                prune_reason = _prune_unrecorded(temporary, artifacts)
                if prune_reason and not identity["fidelity_hold"]:
                    receipt["status"] = "blocked"
                    receipt["visual_check_required"] = False
                    receipt["blocked_reason"] = prune_reason
                receipt["published_at_ns"] = time.time_ns()
                _validate_receipt(receipt, identity, temporary)
                receipt_bytes = _json(receipt)
                receipt_sha = _hash(receipt_bytes)
                manifest = {"schema_version": VERSION, "receipt_sha256": receipt_sha,
                            "artifact_sha256": artifacts}
                manifest_bytes = _json(manifest)
                _write_new(temporary / "receipt.json", receipt_bytes)
                _write_new(temporary / "manifest.json", manifest_bytes)
                final = page_dir / receipt["receipt_id"]
                if not _publish_directory(temporary, final):
                    winner = _load_receipt(final, identity)
                    if winner is None:
                        raise ValueError("concurrent_ocr_receipt_incomplete")
                    outcome = winner[0]
                else:
                    outcome = receipt
                _reconcile_page(page_dir, source_sha256, number, output)
        except OSError:
            publication_failed = "page_publication_failed"
        except ValueError:
            publication_failed = "page_publication_rejected"
        finally:
            if temporary.exists():
                try:
                    shutil.rmtree(temporary)
                except OSError:
                    cleanup_failed = True
        if cleanup_failed or publication_failed:
            with _page_lock(page_dir):
                outcome = _publish_blocked_disposition(
                    page_dir, output, receipt,
                    "attempt_cleanup_failed" if cleanup_failed else publication_failed,
                    source_sha256, number)
        receipts.append(outcome)
    return receipts


def _pypdf_version() -> str:
    module = importlib.import_module("pypdf")
    version = getattr(module, "__version__", None)
    if not version:
        raise ValueError("pypdf_version_unavailable")
    return str(version)


def dependency_status(which=shutil.which, probe=subprocess.run,
                      module_probe=_pypdf_version) -> dict:
    """Read-only import/path/version check; never processes evidence or installs tools."""
    tools = {}
    missing = []
    unverified = []
    try:
        version = module_probe()
        if not version:
            raise ValueError("empty_version")
        tools["pypdf"] = {"available": True, "version": str(version)}
    except (ImportError, ValueError):
        tools["pypdf"] = {"available": False, "version": None}
        missing.append("pypdf")
    for name in OCR_TOOLS:
        path = which(name)
        if path is not None:
            path = os.path.abspath(path)
        if path is None:
            tools[name] = {"available": False, "path": None, "version": None}
            missing.append(name)
            continue
        try:
            version_flag = "-v" if name == "pdftoppm" else "--version"
            result = probe([path, version_flag], capture_output=True, text=True,
                           timeout=5, check=False)
            lines = (result.stdout or result.stderr or "").strip().splitlines()
            label = lines[0][:200] if result.returncode == 0 and lines else None
        except (OSError, subprocess.TimeoutExpired):
            label = None
        tools[name] = {"available": True, "path": path, "version": label}
        if label is None:
            unverified.append(name)
    return {"ready": not missing and not unverified, "missing": missing,
            "version_unverified": unverified, "tools": tools, "method": VERSION}

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline OCR of preserved PDF pages; no review approval")
    parser.add_argument("--doctor", "--check-dependencies", action="store_true")
    parser.add_argument("--reconcile", action="store_true")
    parser.add_argument("--intake-root")
    parser.add_argument("--sha")
    parser.add_argument("--output-root")
    parser.add_argument("--tool-signature")
    parser.add_argument("--attempt-id", default="initial")
    parser.add_argument("--page", type=int, action="append")
    parser.add_argument("--hold-page", type=int, action="append", default=[])
    parser.add_argument("--language", default="eng")
    parser.add_argument("--low-confidence", type=float, default=80.0)
    args = parser.parse_args(argv)
    if args.doctor:
        status = dependency_status()
        print(_json(status).decode("ascii"), end="")
        return 0 if status["ready"] else 2
    if args.reconcile:
        if not args.sha or not args.output_root:
            parser.error("--reconcile requires --sha and --output-root")
        try:
            summary = reconcile_pages(args.output_root, args.sha)
        except Exception as error:
            print(_json({"status": "blocked", "blocked_reason": "reconciliation_failed",
                         "error_type": type(error).__name__}).decode("ascii"), end="")
            return 2
        print(_json(summary).decode("ascii"), end="")
        return 0
    if not all((args.intake_root, args.sha, args.output_root, args.tool_signature)):
        parser.error("--intake-root, --sha, --output-root and --tool-signature are required")
    readiness = dependency_status()
    if not readiness["ready"]:
        print(_json({"status": "blocked", "blocked_reason": "local_ocr_dependency_missing",
                     "dependency_status": readiness}).decode("ascii"), end="")
        return 2
    try:
        receipts = extract_image_only_pages(args.intake_root, args.sha, args.output_root,
                                            tool_signature=args.tool_signature,
                                            tools=readiness["tools"], attempt_id=args.attempt_id,
                                            pages=tuple(args.page) if args.page else None,
                                            fidelity_holds=tuple(args.hold_page),
                                            language=args.language, low_confidence=args.low_confidence)
    except (ValueError, OSError, Exception) as error:
        print(_json({"status": "blocked", "blocked_reason": "source_or_batch_failure",
                     "error_type": type(error).__name__}).decode("ascii"), end="")
        return 2
    summary = {"source_sha256": args.sha, "pages": len(receipts),
               "statuses": {state: sum(r["status"] == state for r in receipts) for state in sorted(STATES)},
               "receipt_ids": [r["receipt_id"] for r in receipts],
               "dependency_status": readiness}
    print(_json(summary).decode("ascii"), end="")
    return 0 if summary["statuses"]["blocked"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
