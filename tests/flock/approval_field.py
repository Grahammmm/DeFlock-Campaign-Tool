"""Test helper: an EMAIL-APPROVAL.json bound to a run's current draft text and signed by a throwaway test key.

`write_field(run)` signs with the shared test owner; `forged=True` keeps the correct text hash but stores a fake
signature, the shape an agent could write by hand. A signature counts only inside `trusted()`, which patches
campaign_tool.approval in-process (tests.approval_signing); a subprocess never trusts the test key.
"""
import atexit
import hashlib
import json
from pathlib import Path

from campaign_tool import approval
from tests.approval_signing import Signer

APPROVAL_LINE = "Approval: only a signed EMAIL-APPROVAL.json beside this file records G's approval; no line here can."
FAKE_SIG = "-----BEGIN SSH SIGNATURE-----\nnot-a-signature\n-----END SSH SIGNATURE-----\n"
PRINCIPAL = "owner@example.invalid"
_OWNER = []


def owner():
    if not _OWNER:
        _OWNER.append(Signer(PRINCIPAL))
        atexit.register(_OWNER[0].close)
    return _OWNER[0]


def trusted():
    """Context in which the test owner's key is a trusted approver (in-process only)."""
    return owner().trust()


def write_field(run, signer=None, forged=False):
    run = Path(run)
    text = (run / "EMAIL-DRAFT.md").read_bytes().decode("utf-8")
    body = text.removesuffix("\n").rpartition("\n")[0] + "\n"  # all but the last (Sent by:) line, byte for byte
    subject = f"{run.parent.parent.name}/{run.name}"
    sig = FAKE_SIG if forged else (signer or owner()).sign(approval.payload("flock-email", subject, body))
    rec = {"v": 1, "state": "approved", "scope": "flock-email", "subject": subject,
           "content_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(), "approved_by": PRINCIPAL, "signature": sig}
    (run / "EMAIL-APPROVAL.json").write_text(json.dumps(rec), encoding="utf-8")
