"""newsletter_draft: build the newsletter draft from the campaign's own content; never send.

The campaign directory is ``CAMPAIGN_ROOT`` in the runner's environment (the same way
``digest`` reads ``CAMPAIGN_JURISDICTION``), never a job input. The shipped builder
(``--from-content``: findings since the last recorded send) writes
``kit/newsletter-draft.{html,txt,json}`` under ``<RUNNER_WORKDIR>/artifacts/<job_id>/``, which
outlives the per-job scratch directory. The campaign directory and ``sent.py`` state are only read.

Outputs ``{"draft", "chars", "findings"}`` plus ``already`` when the job's draft existed. Failures
are fixed enum reasons: bad_job_id, campaign_root_missing, campaign_unreadable, draft_refused,
no_published_findings, draft_unreadable, write_refused. Receipts hold ids, counts and a hash only.
"""
import hashlib
import json
import os
import re
from datetime import date, timedelta
from pathlib import Path

from campaign_tool.cli import read_config
from campaign_tool.content import ContentError
from campaign_tool.newsletter import build_draft, write_draft
from campaign_tool.newsletter.draft import DraftError
from campaign_tool.newsletter.from_content import manifest_from_content
from campaign_tool.newsletter.sent import last_sent
from ..loop import Result, private_dir

SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
NOTE = "draft only; sending needs approval"
HTML, DOC = "kit/newsletter-draft.html", "kit/newsletter-draft.json"


def _fail(reason):
    return Result("failed", {"reason": reason}, None, reason)


def _ok(job_id, rel, digest, chars, findings, already=False):
    outputs = {"draft": rel, "chars": chars, "findings": findings}
    if already:
        outputs["already"] = True
    receipt = {"job_id": job_id, "draft_sha256": digest, "chars": chars, "findings": findings}
    return Result("done", outputs, receipt, NOTE)


def run(ctx):
    job_id = str(ctx.job_id)
    if not SAFE_ID.fullmatch(job_id):
        return _fail("bad_job_id")
    out = Path(ctx.settings.workdir) / "artifacts" / job_id
    rel = f"artifacts/{job_id}/{HTML}"
    if (out / HTML).is_file():  # a re-run never rewrites the draft
        try:
            doc = (out / DOC).read_bytes()
            draft = json.loads(doc)
            return _ok(job_id, rel, hashlib.sha256(doc).hexdigest(), len(draft["text"]), draft["counts"]["findings"], True)
        except (OSError, ValueError, KeyError, TypeError):
            return _fail("draft_unreadable")
    raw = os.environ.get("CAMPAIGN_ROOT")
    root = Path(raw) if raw else None
    if root is None or not root.is_dir():
        return _fail("campaign_root_missing")
    try:
        cfg = read_config(root)
        previous = last_sent(root)
        since = (date.fromisoformat(previous) + timedelta(days=1)).isoformat() + "T00:00:00+00:00" if previous else None
        draft = build_draft(manifest_from_content(root, cfg, since))
    except DraftError:
        return _fail("draft_refused")
    except (OSError, ValueError, KeyError, ContentError):
        return _fail("campaign_unreadable")
    findings = draft["counts"]["findings"]
    if not findings:
        return _fail("no_published_findings")
    try:
        private_dir(out)
        write_draft(out, draft)
        digest = hashlib.sha256((out / DOC).read_bytes()).hexdigest()
    except OSError:
        return _fail("write_refused")
    ctx.log("newsletter.drafted", job_id=job_id, findings=findings, chars=len(draft["text"]))
    return _ok(job_id, rel, digest, len(draft["text"]), findings)
