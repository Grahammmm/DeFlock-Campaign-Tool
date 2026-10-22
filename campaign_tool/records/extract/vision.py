"""Local vision fallback for pages the pinned Tesseract pass could not read well.

Tesseract (``extract.ocr``) always runs first. Only a page whose verified
``local-ocr-3`` receipt is ``visual_check_queued`` (low confidence) or blocked
as ``no_text_after_ocr``/``confidence_unavailable`` is routed here. The page
raster recorded by that receipt is handed to an operator-configured local
command (for example the Flock container's Qwen vision wrapper); the engine
never names a provider, URL or model endpoint. Output stays under the private
OCR root as an immutable ``local-vision-1`` receipt bound to the Tesseract
receipt it supersedes for text. Vision text is never review: every receipt is
``not_reviewed`` and needs a visual check.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time

from . import ocr
from ..ocr_stage import BackendError, Quality, VISION_OUTPUT_GATE, clean_vision_text

VERSION = "local-vision-1"
ARTIFACTS = ("page.png", "sidecar.txt")
STATES = {"vision_text_unreviewed", "blocked"}
TEXT_STATE = "vision_text_unreviewed"
# Tesseract dispositions that route a page to the vision fallback.
FALLBACK_BLOCKED_REASONS = frozenset({"no_text_after_ocr", "confidence_unavailable"})
ENGINE = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
LABEL = re.compile(r"[\w .:+/-]{1,120}\Z")
ATTEMPT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
MAX_TEXT_BYTES = 4 * 1024 * 1024
RECEIPT_KEYS = {"receipt_id", "identity", "source", "locator", "base", "engine", "model", "status",
                "blocked_reason", "visual_check_required", "visual_check_status", "fidelity_status",
                "review_status", "artifact_sha256", "published_at_ns"}


def adapter(argv, engine, model, signature) -> dict:
    """Validated local vision adapter: ``argv + [page.png]`` prints UTF-8 page text on stdout."""
    if (not isinstance(argv, (list, tuple)) or not argv
            or any(not isinstance(part, str) or not part or "\0" in part for part in argv)
            or not os.path.isabs(argv[0])):
        raise ValueError("vision adapter argv must start with an absolute executable path")
    if not isinstance(engine, str) or not ENGINE.fullmatch(engine) or engine == "tesseract":
        raise ValueError("vision adapter engine must be a short lowercase label other than tesseract")
    for name, value in (("model", model), ("signature", signature)):
        if not isinstance(value, str) or not LABEL.fullmatch(value):
            raise ValueError("vision adapter " + name + " must be an operator-pinned label")
    return {"argv": tuple(argv), "engine": engine, "model": model, "signature": signature}


CONFIG_SCHEMA = "local-vision-config-v1"
MAX_CONFIG_BYTES = 16 * 1024


def load_config(path) -> dict:
    """Adapter from an operator-owned ``local-vision-config-v1`` JSON file (owner-only, no symlink).

    ``{"schema": "local-vision-config-v1", "argv": [...], "engine": "qwen", "model": "...",
    "signature": "..."}``. The file names a local command only; endpoints and credentials, if
    any, belong to that command's own configuration and never to the engine.
    """
    import stat as _stat
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        raise ValueError("vision_config_unreadable") from None
    try:
        info = os.fstat(fd)
        if (not _stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or _stat.S_IMODE(info.st_mode) & 0o077 or info.st_size > MAX_CONFIG_BYTES):
            raise ValueError("vision_config_not_owner_only_regular_file")
        raw = os.read(fd, MAX_CONFIG_BYTES + 1)
    finally:
        os.close(fd)
    try:
        config = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError):
        raise ValueError("vision_config_invalid_json") from None
    if not isinstance(config, dict) or set(config) != {"schema", "argv", "engine", "model", "signature"} \
            or config["schema"] != CONFIG_SCHEMA:
        raise ValueError("vision_config_schema_mismatch")
    return adapter(config["argv"], config["engine"], config["model"], config["signature"])


def needs_fallback(receipt: dict) -> bool:
    """True when a Tesseract page receipt is empty or low confidence and has a raster."""
    if not isinstance(receipt, dict) or receipt.get("identity", {}).get("fidelity_hold"):
        return False
    if "page.png" not in receipt.get("artifact_sha256", {}):
        return False
    status = receipt.get("status")
    return status == "visual_check_queued" or (
        status == "blocked" and receipt.get("blocked_reason") in FALLBACK_BLOCKED_REASONS)


def _page_dir(output: Path, source_sha256: str, page: int) -> Path:
    return output / "vision" / source_sha256 / f"page-{page:06d}"


def _validate(receipt: dict, identity: dict, directory: Path) -> str:
    if not isinstance(receipt, dict) or set(receipt) != RECEIPT_KEYS:
        raise ValueError("vision_receipt_schema_mismatch")
    receipt_id = ocr._hash(ocr._json(identity))
    base = receipt["base"]
    if (receipt["identity"] != identity or receipt["receipt_id"] != receipt_id
            or identity.get("version") != VERSION
            or (ocr.SHA256.fullmatch(directory.name) and directory.name != receipt_id)
            or receipt["source"] != {"kind": "preserved_blob", "sha256": identity["source_sha256"]}
            or receipt["locator"] != {"page": identity["page"]}
            or receipt["engine"] != identity["engine"] or receipt["model"] != identity["model"]
            or not isinstance(base, dict)
            or set(base) != {"receipt_id", "receipt_sha256", "status", "blocked_reason"}
            or base["receipt_id"] != identity["base_receipt_id"]
            or base["receipt_sha256"] != identity["base_receipt_sha256"]
            or receipt["status"] not in STATES or receipt["review_status"] != "not_reviewed"
            or receipt["visual_check_required"] is not True
            or receipt["visual_check_status"] != "not_done" or receipt["fidelity_status"] != "not_checked"
            or type(receipt["published_at_ns"]) is not int or receipt["published_at_ns"] <= 0):
        raise ValueError("vision_receipt_state_mismatch")
    blocked = receipt["status"] == "blocked"
    if blocked != (isinstance(receipt["blocked_reason"], str) and bool(receipt["blocked_reason"])):
        raise ValueError("vision_receipt_blocked_reason_mismatch")
    hashes = receipt["artifact_sha256"]
    if not isinstance(hashes, dict) or any(not isinstance(v, str) or not ocr.SHA256.fullmatch(v)
                                           for v in hashes.values()):
        raise ValueError("vision_receipt_artifact_schema_mismatch")
    names = set(hashes)
    if (not blocked and names != set(ARTIFACTS)) or (
            blocked and names not in (set(ARTIFACTS[:n]) for n in range(len(ARTIFACTS) + 1))):
        raise ValueError("vision_receipt_artifact_set_mismatch")
    allowed = names | {"receipt.json", "manifest.json"}
    if any(entry.is_symlink() or entry.name not in allowed for entry in directory.iterdir()):
        raise ValueError("vision_receipt_unexpected_or_symlink_artifact")
    for name, expected in hashes.items():
        if ocr._hash(ocr._read(directory / name, 128 * 1024 * 1024)) != expected:
            raise ValueError("existing_vision_artifact_mismatch")
    return ocr._hash(ocr._json(receipt))


def _load(directory: Path, identity: dict) -> tuple[dict, str, str] | None:
    try:
        os.close(ocr._managed_directory(directory))
    except FileNotFoundError:
        return None
    try:
        manifest_bytes = ocr._read(directory / "manifest.json")
        receipt_bytes = ocr._read(directory / "receipt.json")
    except FileNotFoundError:
        raise ValueError("existing_vision_receipt_incomplete") from None
    receipt = json.loads(receipt_bytes)
    receipt_sha = _validate(receipt, identity, directory)
    manifest = json.loads(manifest_bytes)
    if (manifest != {"schema_version": VERSION, "receipt_sha256": receipt_sha,
                     "artifact_sha256": receipt["artifact_sha256"]}
            or receipt_bytes != ocr._json(receipt) or manifest_bytes != ocr._json(manifest)):
        raise ValueError("existing_vision_manifest_mismatch")
    return receipt, receipt_sha, ocr._hash(manifest_bytes)


def load_vision_receipt(output_root: str | Path, source_sha256: str, page: int, receipt_id: str) -> dict:
    """Read-only verified load of one vision receipt and its exact text bytes."""
    if (not isinstance(source_sha256, str) or not ocr.SHA256.fullmatch(source_sha256)
            or not isinstance(receipt_id, str) or not ocr.SHA256.fullmatch(receipt_id)
            or type(page) is not int or page < 1):
        raise ValueError("vision_receipt_reference_invalid")
    output = Path(os.path.abspath(output_root))
    os.close(ocr._managed_directory(output))
    directory = _page_dir(output, source_sha256, page) / receipt_id
    try:
        raw = json.loads(ocr._read(directory / "receipt.json", 16 * 1024 * 1024))
    except FileNotFoundError:
        raise ValueError("vision_receipt_missing") from None
    identity = raw.get("identity") if isinstance(raw, dict) else None
    if (not isinstance(identity, dict) or identity.get("source_sha256") != source_sha256
            or identity.get("page") != page or identity.get("version") != VERSION):
        raise ValueError("vision_receipt_identity_mismatch")
    loaded = _load(directory, identity)
    if loaded is None or loaded[0]["receipt_id"] != receipt_id:
        raise ValueError("vision_receipt_missing")
    receipt, receipt_sha, manifest_sha = loaded
    sidecar = None
    if "sidecar.txt" in receipt["artifact_sha256"]:
        sidecar = ocr._read(directory / "sidecar.txt", MAX_TEXT_BYTES)
        if ocr._hash(sidecar) != receipt["artifact_sha256"]["sidecar.txt"]:
            raise ValueError("existing_vision_artifact_mismatch")
    return {"receipt": receipt, "receipt_sha256": receipt_sha, "manifest_sha256": manifest_sha,
            "receipt_bytes": ocr._json(receipt), "sidecar": sidecar}


def run_fallback(output_root: str | Path, source_sha256: str, page: int, base_receipt_id: str, *,
                 vision_adapter: dict, attempt_id: str = "initial", timeout: int = 300,
                 runner=subprocess.run) -> dict:
    """Publish one immutable vision receipt for an eligible Tesseract page receipt.

    Idempotent per identity: an existing valid receipt is returned without calling the adapter.
    Adapter failures, timeouts and empty output become blocked receipts, never exceptions.
    """
    if not isinstance(vision_adapter, dict):
        raise ValueError("vision adapter must come from vision.adapter()")
    checked = adapter(vision_adapter.get("argv"), vision_adapter.get("engine"),
                      vision_adapter.get("model"), vision_adapter.get("signature"))
    if not isinstance(attempt_id, str) or not ATTEMPT.fullmatch(attempt_id):
        raise ValueError("attempt_id must be a safe 1-64 character label")
    if type(timeout) is not int or timeout <= 0:
        raise ValueError("invalid vision timeout")
    output = Path(os.path.abspath(output_root))
    base = ocr.load_page_receipt(output, source_sha256, page, base_receipt_id)
    base_receipt = base["receipt"]
    if not needs_fallback(base_receipt):
        raise ValueError("vision_fallback_not_eligible")
    identity = {"source_sha256": source_sha256, "page": page, "version": VERSION,
                "vision_output_gate": VISION_OUTPUT_GATE,
                "base_receipt_id": base_receipt_id, "base_receipt_sha256": base["receipt_sha256"],
                "engine": checked["engine"], "model": checked["model"],
                "adapter_signature": checked["signature"], "attempt_id": attempt_id}
    receipt_id = ocr._hash(ocr._json(identity))
    for directory in (output / "vision", output / "vision" / source_sha256,
                      _page_dir(output, source_sha256, page)):
        os.close(ocr._managed_directory(directory, create=True))
    page_dir = _page_dir(output, source_sha256, page)
    final = page_dir / receipt_id
    prior = _load(final, identity)
    if prior is not None:
        return prior[0]
    ocr_page_dir = output / source_sha256 / f"page-{page:06d}"
    artifacts: dict = {}
    receipt = {"receipt_id": receipt_id, "identity": identity,
               "source": {"kind": "preserved_blob", "sha256": source_sha256},
               "locator": {"page": page},
               "base": {"receipt_id": base_receipt_id, "receipt_sha256": base["receipt_sha256"],
                        "status": base_receipt["status"], "blocked_reason": base_receipt["blocked_reason"]},
               "engine": checked["engine"], "model": checked["model"], "status": "blocked",
               "blocked_reason": None, "visual_check_required": True, "visual_check_status": "not_done",
               "fidelity_status": "not_checked", "review_status": "not_reviewed",
               "artifact_sha256": artifacts, "published_at_ns": 0}
    temporary = Path(tempfile.mkdtemp(prefix=".attempt-", dir=page_dir))
    try:
        os.close(ocr._managed_directory(temporary))
        with ocr._page_lock(ocr_page_dir):
            held = ocr._hold(ocr_page_dir, source_sha256, page, False)
        if held:
            receipt["blocked_reason"] = "fidelity_hold_requires_visual_comparison"
        else:
            raster = ocr._read(output / source_sha256 / f"page-{page:06d}" / base_receipt_id / "page.png",
                               128 * 1024 * 1024)
            if ocr._hash(raster) != base_receipt["artifact_sha256"]["page.png"]:
                raise ValueError("vision_raster_hash_mismatch")
            image = temporary / "page.png"
            ocr._write_new(image, raster)
            artifacts["page.png"] = ocr._hash(raster)
            try:
                result = runner(list(checked["argv"]) + [str(image)], capture_output=True, timeout=timeout,
                                check=False, stdin=subprocess.DEVNULL, cwd=str(temporary),
                                preexec_fn=ocr._limit_child)
            except subprocess.TimeoutExpired:
                receipt["blocked_reason"] = "vision_timeout"
            except OSError:
                receipt["blocked_reason"] = "vision_adapter_unavailable"
            else:
                text = result.stdout if isinstance(result.stdout, bytes) else None
                if result.returncode:
                    receipt["blocked_reason"] = "vision_command_failed"
                elif text is None or len(text) > MAX_TEXT_BYTES:
                    receipt["blocked_reason"] = "vision_output_bound"
                else:
                    try:
                        decoded = text.decode("utf-8")
                    except UnicodeError:
                        receipt["blocked_reason"] = "vision_output_encoding"
                    else:
                        try:
                            decoded = clean_vision_text(decoded)
                        except BackendError as error:
                            receipt["blocked_reason"] = error.code
                        else:
                            quality_reason = Quality().check(decoded)
                            if quality_reason is not None:
                                receipt["blocked_reason"] = "vision_low_quality"
                            else:
                                text = decoded.encode("utf-8")
                                ocr._write_new(temporary / "sidecar.txt", text)
                                artifacts["sidecar.txt"] = ocr._hash(text)
                                receipt["status"] = TEXT_STATE
        if ocr._prune_unrecorded(temporary, artifacts):
            artifacts.clear()
            receipt.update(status="blocked", blocked_reason="vision_derivative_unsafe")
            ocr._prune_unrecorded(temporary, artifacts)
        with ocr._page_lock(ocr_page_dir):
            if receipt["status"] == TEXT_STATE and ocr._hold(ocr_page_dir, source_sha256, page, False):
                (temporary / "sidecar.txt").unlink()
                artifacts.pop("sidecar.txt")
                receipt.update(status="blocked", blocked_reason="fidelity_hold_requires_visual_comparison")
            receipt["published_at_ns"] = time.time_ns()
            receipt_sha = _validate(receipt, identity, temporary)
            ocr._write_new(temporary / "receipt.json", ocr._json(receipt))
            ocr._write_new(temporary / "manifest.json", ocr._json(
                {"schema_version": VERSION, "receipt_sha256": receipt_sha, "artifact_sha256": artifacts}))
            if not ocr._publish_directory(temporary, final):
                winner = _load(final, identity)
                if winner is None:
                    raise ValueError("concurrent_vision_receipt_incomplete")
                return winner[0]
        return receipt
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
