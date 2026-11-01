"""Test helper: a well-formed EMAIL-APPROVAL.json bound to a run's current draft text.

The signature is a placeholder: flock_email only reads the state field and its text binding (it has no
subprocess/campaign_tool imports). Real signatures are checked by tools/flock_approve.py; see test_email_approval.
"""
import hashlib
import json
from pathlib import Path

APPROVAL_LINE = "Approval: only a signed EMAIL-APPROVAL.json beside this file records G's approval; no line here can."
SIG = "-----BEGIN SSH SIGNATURE-----\nplaceholder\n-----END SSH SIGNATURE-----\n"


def write_field(run, approved_by="owner@example.invalid"):
    run = Path(run)
    text = (run / "EMAIL-DRAFT.md").read_text(encoding="utf-8")
    body = "\n".join(ln for ln in text.splitlines() if not ln.startswith("Sent by:")) + "\n"
    rec = {"v": 1, "state": "approved", "scope": "flock-email", "subject": f"{run.parent.parent.name}/{run.name}",
           "content_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(), "approved_by": approved_by, "signature": SIG}
    (run / "EMAIL-APPROVAL.json").write_text(json.dumps(rec), encoding="utf-8")
