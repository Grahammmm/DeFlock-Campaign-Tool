"""build_site: render the public site from a content manifest and stage it.

Worker envelope v1 (publication, correction and withdrawal use the same contract)::

    {"schema_version": 1, "site_version": "m<first 16 hash characters>",
     "reason": "workspace-only reason", "manifest": {
       "schema_version": 2, "site_version": "...", "manifest_sha256": "...",
       "campaign": {"schema_version": 1, "name": "...", "county": "...",
                    "state": "CA", "country": "US"},
       "site": {}, "findings": [], "meetings": [], "sources": [],
       "agencies": [], "map": null}}

The input digest covers all manifest fields except site_version/manifest_sha256,
using compact sorted ASCII JSON. V2 permits safe integer numbers only, matching
the Worker's canonical serializer without cross-language float ambiguity. Maps
are refused until an envelope can carry their required reviewed assets. Hashes
bind bytes, not review authority. The existing content gates and public scan still
apply, and the Worker-owned, job-bound deploy card remains a separate approval.

Explicit legacy adapter for manually authored file manifests (no schema_version)::

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
is PUT to ``/api/runner/site/<version>/<path>`` (the public bucket). Only legacy
jobs propose their own ``deploy_site`` action; the public-site Worker keeps
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
SLUG = re.compile(r"[a-z][a-z0-9-]{0,63}")
MANIFEST_KEYS = {"schema_version", "site_version", "manifest_sha256", "campaign", "site",
                 "findings", "meetings", "sources", "agencies", "map"}
MEDIA = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".json": "application/json", ".geojson": "application/geo+json", ".xml": "application/xml", ".txt": "text/plain; charset=utf-8",
         ".svg": "image/svg+xml", ".webp": "image/webp", ".ico": "image/x-icon"}


def _media_type(path):
    if path.name == "_headers":
        return "text/plain; charset=utf-8"
    return MEDIA.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def _check_path(path):
    if not isinstance(path, str):
        raise ValueError("content paths must be strings")
    parts = path.split("/")
    if not parts or any(not SAFE_PART.match(part) for part in parts):
        raise ValueError("unsafe content path: " + path)
    return Path(*parts)


def _keys(value, keys, label):
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(label + " has missing or unsupported fields")


def _hash_values(value):
    if value is None or isinstance(value, (str, bool)):
        return
    if type(value) is int and abs(value) <= 9007199254740991:
        return
    if isinstance(value, (list, dict)):
        for child in (value.values() if isinstance(value, dict) else value):
            _hash_values(child)
        return
    raise ValueError("build_site manifest numbers must be safe integers")


def _worker_manifest(inputs):
    _keys(inputs, {"schema_version", "site_version", "reason", "manifest"}, "build_site envelope")
    if type(inputs["schema_version"]) is not int or inputs["schema_version"] != 1:
        raise ValueError("unsupported build_site envelope schema_version")
    if not isinstance(inputs["reason"], str):
        raise ValueError("build_site reason must be a string")
    manifest = inputs["manifest"]
    _keys(manifest, MANIFEST_KEYS, "build_site manifest")
    if type(manifest["schema_version"]) is not int or manifest["schema_version"] != 2:
        raise ValueError("unsupported build_site manifest schema_version")
    _hash_values(manifest)
    body = {k: v for k, v in manifest.items() if k not in ("site_version", "manifest_sha256")}
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"),
                                       ensure_ascii=True, allow_nan=False).encode()).hexdigest()
    version = "m" + digest[:16]
    if manifest["manifest_sha256"] != digest or manifest["site_version"] != version or inputs["site_version"] != version:
        raise ValueError("build_site manifest hash or site_version mismatch")
    campaign = manifest["campaign"]
    _keys(campaign, {"schema_version", "name", "county", "state", "country"}, "build_site campaign")
    if type(campaign["schema_version"]) is not int or campaign["schema_version"] != 1:
        raise ValueError("unsupported build_site campaign schema_version")
    for key in ("name", "county"):
        if not isinstance(campaign[key], str) or not campaign[key].strip():
            raise ValueError("build_site campaign needs name and county")
    for key in ("state", "country"):
        if not isinstance(campaign[key], str) or not re.fullmatch(r"[A-Z]{2}", campaign[key]):
            raise ValueError("build_site campaign needs two-letter state and country")
    if not isinstance(manifest["site"], dict):
        raise ValueError("build_site manifest site must be an object")
    for key in ("findings", "meetings", "sources", "agencies"):
        if not isinstance(manifest[key], list) or any(not isinstance(item, dict) for item in manifest[key]):
            raise ValueError("build_site manifest collections must be arrays of objects")
    if manifest["map"] is not None:
        raise ValueError("build_site manifest maps require a future schema with reviewed map assets")
    content = {"site.json": manifest["site"], "sources.json": manifest["sources"], "agencies.json": manifest["agencies"]}
    for collection, key in (("findings", "slug"), ("meetings", "id")):
        for document in manifest[collection]:
            name = document.get(key)
            if not isinstance(name, str) or not SLUG.fullmatch(name):
                raise ValueError("build_site manifest has an invalid document filename")
            path = f"{collection}/{name}.json"
            if path in content:
                raise ValueError("build_site manifest has duplicate document filenames")
            content[path] = document
    return version, campaign, content, digest


def _inputs(inputs):
    if not isinstance(inputs, dict):
        raise ValueError("build_site inputs must be an object")
    # Presence, not truthiness: malformed/new/old Worker envelopes never fall back
    # to the permissive legacy file contract.
    if {"schema_version", "site_version", "manifest"} & inputs.keys():
        return _worker_manifest(inputs)
    _keys(inputs, {"version", "campaign", "content"}, "legacy build_site inputs")
    version = inputs["version"]
    if not isinstance(version, str) or not SAFE_VERSION.fullmatch(version):
        raise ValueError("build_site needs version matching [a-z0-9._-]{1,64}")
    campaign, content = inputs["campaign"], inputs["content"]
    if not isinstance(campaign, dict) or not isinstance(content, dict) or "site.json" not in content:
        raise ValueError("build_site needs campaign and content with site.json")
    return version, campaign, content, None


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
    try:
        version, campaign, content, input_manifest_sha = _inputs(ctx.job.get("inputs"))
    except ValueError as exc:
        return Result("failed", error=str(exc))
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
    outputs = {"version": version, "manifest": manifest, "manifest_sha256": manifest_sha}
    receipt = {"version": version, "manifest_sha256": manifest_sha}
    if input_manifest_sha is not None:
        # The producer already owns the deploy card and its build_job_id gate.
        # Bind the reviewed input/dependency manifest and built artifact hashes in
        # the result, without creating a second, unbound approval path on retry.
        outputs.update(input_manifest_sha256=input_manifest_sha, build_job_id=ctx.job_id)
        receipt.update(input_manifest_sha256=input_manifest_sha, build_job_id=ctx.job_id)
    else:
        proposal = {"site_version": version, "file_count": len(manifest), "manifest_sha256": manifest_sha,
                    "note": "Files are staged in the public bucket; nothing is served until approved."}
        action = ctx.workspace.propose("deploy_site", version, proposal, idempotency_key("deploy_site", version, manifest_sha), ctx.job_id)
        outputs["action_id"] = action.get("action_id")
    ctx.log("site.built", job_id=ctx.job_id, version=version, files=len(manifest), action_id=outputs.get("action_id"))
    return Result("done", outputs, receipt)
