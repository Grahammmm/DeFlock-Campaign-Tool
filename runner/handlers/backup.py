"""backup: write one local, verified archive of the campaign directory; upload nothing.

The campaign directory is ``CAMPAIGN_ROOT`` in the runner's environment, never a job input. The
shipped ``campaign_tool.backup.export_into`` writes ``deflock-backup-<UTC stamp>.tar[.gpg]`` under
``<RUNNER_WORKDIR>/artifacts/<job_id>/``; it verifies the archive and never overwrites. Encryption is
whatever the campaign's ``backup.encrypt_to`` setting says; this handler decides nothing about it and
has no remote destination (storage and encryption per docs/BACKUP.md).

Outputs ``{"archive", "bytes", "manifest_sha256"}``. Failures are fixed enum reasons: bad_job_id,
campaign_root_missing, campaign_root_unreadable, archive_exists, backup_in_progress,
backup_config_invalid, backup_refused, write_refused. Receipts hold ids, counts, a hash and an enum only.
"""
import os
import re
from pathlib import Path

from campaign_tool.backup import BackupError, export_into
from ..loop import Result, private_dir

SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
NOTE = "local archive; storage and encryption per docs/BACKUP.md"


def _fail(reason):
    return Result("failed", {"reason": reason}, None, reason)


def _reason(exc):
    text = str(exc)  # classified here, never copied into a result
    if "another backup is running" in text:
        return "backup_in_progress"
    if "exists" in text:
        return "archive_exists"
    if "backup.encrypt_to" in text:
        return "backup_config_invalid"
    return "backup_refused"


def run(ctx):
    job_id = str(ctx.job_id)
    if not SAFE_ID.fullmatch(job_id):
        return _fail("bad_job_id")
    raw = os.environ.get("CAMPAIGN_ROOT")
    root = Path(raw) if raw else None
    if root is None or not (root / "campaign.json").is_file():
        return _fail("campaign_root_missing")
    if not os.access(root, os.R_OK | os.X_OK):
        return _fail("campaign_root_unreadable")
    out = Path(ctx.settings.workdir) / "artifacts" / job_id
    try:
        if out.is_dir() and any(out.glob("deflock-backup-*")):  # one archive per job; the first stays untouched
            return _fail("archive_exists")
        private_dir(out)
        name, manifest = export_into(root, out)
        size = (out / name).stat().st_size
    except BackupError as exc:
        return _fail(_reason(exc))
    except OSError:
        return _fail("write_refused")
    digest = manifest["manifest_sha256"]
    encrypted = bool(manifest.get("encrypted"))
    ctx.log("backup.written", job_id=job_id, bytes=size, encrypted=encrypted)
    return Result("done", {"archive": f"artifacts/{job_id}/{name}", "bytes": size, "manifest_sha256": digest},
                  {"job_id": job_id, "manifest_sha256": digest, "bytes": size, "encrypted": encrypted}, NOTE)
