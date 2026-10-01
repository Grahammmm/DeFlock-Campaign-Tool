"""extract: run the sandboxed intake worker on one original and store its text.

Reuses ``campaign_tool.records.intake.folder``'s ``_worker`` subprocess
(resource limits, zip/docx/xlsx safety checks, pypdf/openpyxl parsers) with
its default LIMITS and timeout; nothing is re-implemented here. The
``units.jsonl`` the worker writes is uploaded as a private object and its
hash is returned as ``text_sha256``. Children the worker split out of
containers (zip members, nested mail parts) are stored as originals with
receipts and get their own ``extract`` follow-ups. A ``digest`` follow-up
is enqueued when any text was extracted.
"""
import json
import subprocess
import sys
from pathlib import Path

from campaign_tool.records.intake import folder
from ..loop import Result, private_dir
from . import idempotency_key

MEDIA_TYPE_UNITS = "application/x-ndjson"


def extractor_label(form):
    if form == "pdf":
        try:
            import pypdf
            return "pypdf " + pypdf.__version__
        except ImportError:
            return "pypdf (missing)"
    if form == "xlsx":
        try:
            import openpyxl
            return "openpyxl " + openpyxl.__version__
        except ImportError:
            return "openpyxl (missing)"
    return {"eml": "eml", "docx": "docx-xml", "zip": "zip", "csv": "csv", "tsv": "csv"}.get(form, "text" if form in {"txt", "md", "json", "jsonl", "xml", "html", "htm", "log", "rst", "yaml", "yml"} else form)


def run_worker(out, source, sha, form, limits=None, depth=0):
    """Invoke folder.py _worker exactly as folder.extract does; returns the digest dict."""
    limits = {**folder.LIMITS, **(limits or {})}
    config = {"output": str(out), "roots": [], "agency_patterns": {}, "excluded_path_fragments": []}
    dest = out / "derived" / sha
    private_dir(dest)
    cmd = [sys.executable, "-B", folder.__file__, "_worker", "--output", str(out), "--source", str(source), "--sha", sha,
           "--format", form, "--depth", str(depth), "--limits", json.dumps(limits), "--worker-config", json.dumps(config)]
    with (dest / "stderr.log").open("w") as err:
        try:
            proc = subprocess.run(cmd, stdout=err, stderr=err, timeout=limits["seconds"])
            failure = None if proc.returncode == 0 else f"worker_exit_{proc.returncode}"
        except subprocess.TimeoutExpired:
            failure = "worker_timeout"
    digest_path = dest / "digest.json"
    if digest_path.exists():
        digest = json.loads(digest_path.read_text())
    else:
        digest = {"stage": "failed", "counts": {}, "issues": [{"code": failure or "missing_worker_digest"}], "children": []}
    if failure:
        digest["stage"] = "failed"
        digest.setdefault("issues", []).append({"code": failure})
    return digest, dest


def row_fields(form, digest):
    counts = digest.get("counts") or {}
    status = digest.get("stage", "failed")
    if status not in ("complete", "partial", "failed", "unsupported"):
        status = "failed"
    page_count = counts.get("pages_expected")
    pages_with_text = None
    if form == "pdf" and page_count is not None:
        pages_with_text = max(0, counts.get("pdf_page", 0) - counts.get("ocr_needed", 0))
    elif form == "xlsx":
        page_count = counts.get("workbook_sheet")
    codes = sorted({issue.get("code", "") for issue in digest.get("issues", []) if issue.get("code")})
    return {"status": status, "page_count": page_count, "pages_with_text": pages_with_text,
            "extractor": extractor_label(form), "notes": "; ".join(codes)[:1000] or None, "units": counts.get("units", 0)}


def run(ctx):
    inputs = ctx.job.get("inputs") or {}
    sha = inputs.get("sha256")
    if not sha:
        return Result("failed", error="extract needs sha256")
    data, media_type = ctx.workspace.get_original(sha)
    name = inputs.get("original_name") or inputs.get("media_type") or media_type or "unknown"
    form = folder.fmt(name, data[:16384], data)
    if form == "unknown" and media_type == "message/rfc822":
        form = "eml"
    out = private_dir(ctx.dir / "intake")
    private_dir(out / "blobs")
    source = ctx.write("intake/blobs/" + sha, data)
    digest, dest = run_worker(out, source, sha, form)
    fields = row_fields(form, digest)
    text_sha = None
    units_path = dest / "units.jsonl"
    if units_path.exists() and units_path.stat().st_size:
        text_sha = ctx.workspace.put_original(units_path.read_bytes(), MEDIA_TYPE_UNITS)["sha256"]
    followups = []
    children = []
    for child in digest.get("children") or []:
        blob = out / "blobs" / child["sha"]
        if not blob.exists():
            continue
        put = ctx.workspace.put_original(blob.read_bytes(), "application/octet-stream")
        ctx.workspace.post_receipt({"sha256": put["sha256"], "source_id": f"{sha}:{json.dumps(child['locator'], sort_keys=True)}",
                                    "original_name": child["name"], **({"correspondence_id": inputs["correspondence_id"]} if inputs.get("correspondence_id") else {})})
        children.append({"sha256": put["sha256"], "name": child["name"], "relation": child["relation"], "format": child["format"]})
        followups.append({"kind": "extract", "idempotency_key": idempotency_key("extract", put["sha256"]),
                          "inputs": {"sha256": put["sha256"], "original_name": child["name"], "parent_sha256": sha,
                                     **({"correspondence_id": inputs["correspondence_id"]} if inputs.get("correspondence_id") else {})}})
    if text_sha and fields["status"] in ("complete", "partial") and fields["units"]:
        followups.append({"kind": "digest", "idempotency_key": idempotency_key("digest", sha, text_sha),
                          "inputs": {"sha256": sha, "text_sha256": text_sha, "original_name": name,
                                     **({"jurisdiction": inputs["jurisdiction"]} if inputs.get("jurisdiction") else {})}})
    ctx.log("extract.done", job_id=ctx.job_id, format=form, status=fields["status"], units=fields["units"], children=len(children))
    outputs = {"sha256": sha, "format": form, "text_sha256": text_sha, "extraction": {**fields, "text_sha256": text_sha},
               "children": children, "followups": followups}
    status = "done" if fields["status"] != "failed" else "failed"
    return Result(status, outputs, {"sha256": sha, "text_sha256": text_sha}, None if status == "done" else fields["notes"])
