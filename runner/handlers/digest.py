"""digest: redact, run detectors, optionally narrate, return a validated digest.

Inputs: ``{"sha256", "text_sha256", "jurisdiction", "law_package_version"?, "event_date"?}``.
``jurisdiction`` falls back to ``CAMPAIGN_JURISDICTION`` in the runner's
environment. The law package is loaded from the ``jurisdictions/`` copy in
the image (``JURISDICTIONS_DIR`` overrides). Cron-created digest jobs that
carry only a ``slot`` finish as ``done`` with ``skipped`` set; fan-out to
individual originals is the workspace's job.

Under ``redacted_cloud`` the text is redacted before the model call and the
redaction count is logged. Under ``strict_local`` a non-local model URL
fails the job before any request is made. A model reply that is not valid
digest JSON fails the job with the reason.
"""
import json
import os

from campaign_tool import law
from campaign_tool.digest import build_digest
from campaign_tool.digest.detectors import units_from_text
from campaign_tool.digest.model import ModelError, check_config
from ..loop import Result


def load_units(data):
    """units.jsonl -> [{locator, text}]; anything else is treated as plain text."""
    text = data.decode("utf-8", errors="replace")
    units = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            unit = json.loads(line)
        except json.JSONDecodeError:
            return units_from_text(text)
        if not isinstance(unit, dict) or "locator" not in unit:
            return units_from_text(text)
        body = unit.get("text") or ""
        if not body and isinstance(unit.get("data"), dict) and unit["data"].get("cells"):
            body = " | ".join(f"{c.get('cell')}={c.get('value')}" for c in unit["data"]["cells"])
        units.append({"kind": unit.get("kind"), "locator": unit.get("locator") or {}, "text": body})
    return units


def run(ctx):
    inputs = ctx.job.get("inputs") or {}
    sha = inputs.get("sha256")
    if not sha:
        return Result("done", {"skipped": "digest job carries no sha256; nothing to digest in this run"})
    text_sha = inputs.get("text_sha256")
    if not text_sha:
        return Result("failed", error="digest needs text_sha256 from a prior extract job")
    jurisdiction = inputs.get("jurisdiction") or os.environ.get("CAMPAIGN_JURISDICTION")
    if not jurisdiction:
        return Result("failed", error="digest needs jurisdiction (job input or CAMPAIGN_JURISDICTION)")
    try:
        package = law.load_package(jurisdiction, base=ctx.settings.jurisdictions_dir)
    except ValueError as exc:
        return Result("failed", error="law package: " + str(exc)[:200])
    version = inputs.get("law_package_version") or f"{jurisdiction}:{package['status']}:{package.get('reviewed_at') or 'unreviewed'}"
    model_config = ctx.settings.model_config()
    tier = ctx.privacy_tier
    if model_config is not None:
        model_config.privacy_tier = tier
        try:
            check_config(model_config)
        except ModelError as exc:
            return Result("failed", error=str(exc))
    data, _ = ctx.workspace.get_original(text_sha)
    units = load_units(data)
    if not any((u.get("text") or "").strip() for u in units):
        return Result("done", {"sha256": sha, "skipped": "no extracted text; OCR or visual review needed"})
    try:
        digest = build_digest(sha, units, package, jurisdiction, version, tier, model_config=model_config,
                              allowlist=ctx.settings.redaction_allowlist, denylist=ctx.settings.redaction_denylist,
                              event_date=inputs.get("event_date"))
    except ModelError as exc:
        return Result("failed", error="model: " + str(exc)[:300])
    ctx.log("digest.redacted", job_id=ctx.job_id, tier=tier, redactions=digest["redaction_count"], model=bool(model_config))
    digest_bytes = json.dumps(digest, sort_keys=True, ensure_ascii=True).encode("utf-8")
    stored = ctx.workspace.put_original(digest_bytes, "application/json")
    outputs = {
        "sha256": sha,
        "digest_sha256": stored["sha256"],
        "digest_row": {"sha256": sha, "law_package_version": version, "privacy_tier": tier,
                       "redaction_count": digest["redaction_count"], "model_id": digest["model_id"], "digest_json": digest},
        "hit_count": len(digest["hits"]),
        "conclusion_confidences": sorted({c["confidence"] for c in digest["conclusions"]}),
    }
    return Result("done", outputs, {"sha256": sha, "digest_sha256": stored["sha256"], "redaction_count": digest["redaction_count"]})
