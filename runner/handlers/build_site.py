"""build_site: render the public site from a content manifest and stage it.

Inputs::

    {"version": "<[a-z0-9._-]{1,64}>",
     "campaign": {...campaign.json...},
     "content": {"site.json": {...}, "findings/<slug>.json": {...}, "meetings/<id>.json": {...},
                 "sources.json": {...}, "agencies.json": {...}, "map/map-config.json": {...},
                 "map/cameras.geojson": {...}, "map/attribution.txt": "text",
                 "vendor/maplibre-gl.js": {"base64": "..."}}}

Every manifest entry is written under a private temp campaign directory
(paths are validated: relative, plain names, no ``..``), then
``campaign_tool.site.build_site`` renders ``public/`` and
``tools/check_public_tree.violations`` scans every emitted file. Any
potential leak fails the job and nothing is uploaded. Otherwise each file
is PUT to ``/api/runner/site/<version>/<path>`` (the public bucket) and a
``deploy_site`` external action is proposed; the public-site Worker keeps
serving the previous version until an organizer approves it.
"""
import base64
import hashlib
import json
import mimetypes
import re
import sys
from pathlib import Path

from campaign_tool import law
from campaign_tool.site import build_site
from ..loop import Result, private_dir, private_write
from . import idempotency_key

SAFE_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")
SAFE_VERSION = re.compile(r"^[a-z0-9._-]{1,64}$")
MEDIA = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".json": "application/json", ".geojson": "application/geo+json", ".xml": "application/xml", ".txt": "text/plain; charset=utf-8",
         ".svg": "image/svg+xml", ".webp": "image/webp", ".ico": "image/x-icon"}


def _media_type(path):
    if path.name == "_headers":
        return "text/plain; charset=utf-8"
    return MEDIA.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def _check_path(path):
    parts = path.split("/")
    if not parts or any(not SAFE_PART.match(part) for part in parts):
        raise ValueError("unsafe content path: " + path)
    return Path(*parts)


def _encode(value):
    if isinstance(value, dict) and set(value) == {"base64"}:
        return base64.b64decode(value["base64"], validate=True)
    if isinstance(value, (dict, list)):
        return json.dumps(value, indent=2, sort_keys=True).encode("utf-8")
    if isinstance(value, str):
        return value.encode("utf-8")
    raise ValueError("manifest values must be JSON objects, strings or {base64}")


def write_manifest(root, campaign, content):
    private_write(root / "campaign.json", json.dumps(campaign, indent=2, sort_keys=True))
    for path, value in content.items():
        relative = _check_path(path)
        private_write(root / "content" / relative, _encode(value))


def scan_public(public):
    sys.path.insert(0, str(Path(law.REPO_ROOT) / "tools"))
    try:
        from check_public_tree import violations
    finally:
        sys.path.pop(0)
    hits = []
    for path in sorted(public.rglob("*")):
        if path.is_symlink():
            hits.append((str(path.relative_to(public)), 0, "symlink"))
        elif path.is_file():
            hits.extend(violations(str(path.relative_to(public)), path.read_bytes()))
    return hits


def run(ctx):
    inputs = ctx.job.get("inputs") or {}
    version = inputs.get("version")
    if not version or not SAFE_VERSION.match(version):
        return Result("failed", error="build_site needs version matching [a-z0-9._-]{1,64}")
    campaign = inputs.get("campaign")
    content = inputs.get("content")
    if not isinstance(campaign, dict) or not isinstance(content, dict) or "site.json" not in content:
        return Result("failed", error="build_site needs campaign and content with site.json")
    root = private_dir(ctx.dir / "campaign")
    try:
        write_manifest(root, campaign, content)
        written = build_site(root)
    except (ValueError, OSError) as exc:
        return Result("failed", error=f"{type(exc).__name__}: {str(exc)[:300]}")
    public = root / "public"
    leaks = scan_public(public)
    if leaks:
        ctx.log("site.leak_check_failed", job_id=ctx.job_id, count=len(leaks), labels=sorted({label for _, _, label in leaks}))
        return Result("failed", error=f"public tree check found {len(leaks)} potential leak(s); nothing uploaded")
    manifest = []
    for path in sorted(p for p in written if p.is_file()):
        relative = path.relative_to(public).as_posix()
        data = path.read_bytes()
        ctx.workspace.put_site_file(version, relative, data, _media_type(path))
        manifest.append({"path": relative, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)})
    manifest_sha = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    proposal = {"site_version": version, "file_count": len(manifest), "manifest_sha256": manifest_sha,
                "note": "Files are staged in the public bucket; nothing is served until approved."}
    action = ctx.workspace.propose("deploy_site", version, proposal, idempotency_key("deploy_site", version, manifest_sha), ctx.job_id)
    ctx.log("site.built", job_id=ctx.job_id, version=version, files=len(manifest), action_id=action.get("action_id"))
    return Result("done", {"version": version, "manifest": manifest, "manifest_sha256": manifest_sha, "action_id": action.get("action_id")},
                  {"version": version, "manifest_sha256": manifest_sha})
