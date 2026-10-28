"""Public OCR receipt bridge: capture/replay complete per-page artifact closure.

Uses the public local-ocr verifier. Private compatibility helper source is not
imported or copied. Original CAS validation belongs to the extraction adapter.
"""
import re
import tempfile
from pathlib import Path
from .extract import ocr, vision

VERSION = "public-ocr-closure-v1"
MAX_FILE = 16 * 1024 * 1024
MAX_TOTAL = 64 * 1024 * 1024
NAMES = ("manifest.json", *ocr.ARTIFACTS)


def _api():
    # Lazy import avoids the enrollment module's import cycle.
    from . import extraction_ledger
    return extraction_ledger


def artifact_name(name):
    return type(name) is str and bool(re.fullmatch(
        r"ocr_asset:[0-9a-f]{64}:(?:manifest\.json|page\.pdf|page\.png|sidecar\.txt|confidence\.tsv)", name))


def _names(receipt_id):
    _api().checked_sha(receipt_id)
    return {"ocr_receipt:" + receipt_id: "receipt.json", **{
        "ocr_asset:" + receipt_id + ":" + name: name for name in NAMES}}


def closure_artifacts(root, subject, page, receipt_id):
    api = _api()
    api.checked_sha(subject)
    if type(page) is not int or page < 1:
        raise api.ExtractionBindingError("ocr_closure_page_invalid")
    directory = api.private_dir(Path(root) / subject / f"page-{page:06d}" / receipt_id)
    names = _names(receipt_id)
    if {entry.name for entry in directory.iterdir()} != set(names.values()):
        raise api.ExtractionBindingError("ocr_closure_source_asset_set")
    artifacts = {key: api.read_file(directory / name, MAX_FILE) for key, name in names.items()}
    if sum(map(len, artifacts.values())) > MAX_TOTAL:
        raise api.ExtractionBindingError("ocr_closure_resource_bound")
    # Verify the bounded snapshot rather than reopening mutable source files.
    load_cas_page(artifacts, subject, page, receipt_id)

    return artifacts


def load_cas_page(artifacts, subject, page, receipt_id, *, scratch_root=None):
    api = _api()
    api.checked_sha(subject)
    if type(page) is not int or page < 1:
        raise api.ExtractionBindingError("ocr_closure_page_invalid")
    names = _names(receipt_id)
    if type(artifacts) is not dict or not set(names) <= set(artifacts):
        raise api.ExtractionBindingError("ocr_closure_incomplete")
    selected = {key: artifacts[key] for key in names}
    if (any(type(raw) is not bytes or len(raw) > MAX_FILE for raw in selected.values())
            or sum(map(len, selected.values())) > MAX_TOTAL):
        raise api.ExtractionBindingError("ocr_closure_resource_bound")
    receipt = api.decode(selected["ocr_receipt:" + receipt_id])
    api.decode(selected["ocr_asset:" + receipt_id + ":manifest.json"])
    identity = receipt.get("identity") if isinstance(receipt, dict) else None
    if not isinstance(identity, dict) or identity.get("source_sha256") != subject:
        raise api.ExtractionBindingError("ocr_closure_source_mismatch")
    # Only verified CAS bytes populate this disposable tree. Receipt-selected
    # paths/tools are never opened; the original OCR tree is not consulted.
    with tempfile.TemporaryDirectory(prefix="ocr-cas-verify-", dir=scratch_root) as tmp:
        root = Path(tmp)
        directory = root / subject / f"page-{page:06d}" / receipt_id
        directory.mkdir(mode=0o700, parents=True)
        for key, name in names.items():
            target = directory / name
            target.write_bytes(selected[key])
            target.chmod(0o400)
        return ocr.load_page_receipt(root, subject, page, receipt_id)


# --- local-vision-1 fallback receipts (extract.vision) ------------------------------
# A vision receipt's asset set depends on its outcome (a blocked receipt may record no
# raster or no text), so the closure carries exactly the files present in its directory
# and the verified loader rejects any missing, extra or altered one.

VISION_NAMES = ("manifest.json", *vision.ARTIFACTS)


def vision_artifact_name(name):
    return type(name) is str and bool(re.fullmatch(
        r"vision_receipt:[0-9a-f]{64}|vision_asset:[0-9a-f]{64}:(?:manifest\.json|page\.png|sidecar\.txt)", name))


def vision_receipt_ids(names):
    return sorted(name[15:] for name in names if type(name) is str and name.startswith("vision_receipt:"))


def _vision_names(artifacts, receipt_id):
    _api().checked_sha(receipt_id)
    prefix = "vision_asset:" + receipt_id + ":"
    return {"vision_receipt:" + receipt_id: "receipt.json", **{
        key: key[len(prefix):] for key in artifacts if type(key) is str and key.startswith(prefix)}}


def vision_closure_artifacts(root, subject, page, receipt_id):
    api = _api()
    api.checked_sha(subject)
    if type(page) is not int or page < 1:
        raise api.ExtractionBindingError("vision_closure_page_invalid")
    api.checked_sha(receipt_id)
    directory = api.private_dir(Path(root) / "vision" / subject / f"page-{page:06d}" / receipt_id)
    present = {entry.name for entry in directory.iterdir()}
    if not {"receipt.json", "manifest.json"} <= present or not present <= {"receipt.json", *VISION_NAMES}:
        raise api.ExtractionBindingError("vision_closure_source_asset_set")
    names = {"vision_receipt:" + receipt_id: "receipt.json", **{
        "vision_asset:" + receipt_id + ":" + name: name for name in sorted(present - {"receipt.json"})}}
    artifacts = {key: api.read_file(directory / name, MAX_FILE) for key, name in names.items()}
    if sum(map(len, artifacts.values())) > MAX_TOTAL:
        raise api.ExtractionBindingError("vision_closure_resource_bound")
    load_cas_vision(artifacts, subject, page, receipt_id)
    return artifacts


def load_cas_vision(artifacts, subject, page, receipt_id, *, scratch_root=None):
    """Verified ``vision.load_vision_receipt`` over CAS bytes only (never the original tree)."""
    api = _api()
    api.checked_sha(subject)
    if type(page) is not int or page < 1:
        raise api.ExtractionBindingError("vision_closure_page_invalid")
    if type(artifacts) is not dict:
        raise api.ExtractionBindingError("vision_closure_incomplete")
    names = _vision_names(artifacts, receipt_id)
    if ("vision_receipt:" + receipt_id not in artifacts
            or "vision_asset:" + receipt_id + ":manifest.json" not in artifacts
            or any(name not in VISION_NAMES + ("receipt.json",) for name in names.values())):
        raise api.ExtractionBindingError("vision_closure_incomplete")
    selected = {key: artifacts[key] for key in names}
    if (any(type(raw) is not bytes or len(raw) > MAX_FILE for raw in selected.values())
            or sum(map(len, selected.values())) > MAX_TOTAL):
        raise api.ExtractionBindingError("vision_closure_resource_bound")
    with tempfile.TemporaryDirectory(prefix="vision-cas-verify-", dir=scratch_root) as tmp:
        root = Path(tmp)
        directory = root / "vision" / subject / f"page-{page:06d}" / receipt_id
        directory.mkdir(mode=0o700, parents=True)
        for key, name in names.items():
            target = directory / name
            target.write_bytes(selected[key])
            target.chmod(0o400)
        return vision.load_vision_receipt(root, subject, page, receipt_id)
