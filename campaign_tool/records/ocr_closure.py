"""Public OCR receipt bridge: capture/replay complete per-page artifact closure.

Uses the public local-ocr verifier. Private compatibility helper source is not
imported or copied. Original CAS validation belongs to the extraction adapter.
"""
import re
import tempfile
from pathlib import Path
from .extract import ocr

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
