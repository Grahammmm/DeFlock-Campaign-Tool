"""Design S1-S4 replay against candidate: old invalid histories are never rewritten."""
import json
import unittest
from unittest.mock import patch
from campaign_tool import approval
from campaign_tool.records import approval_queue as q

ROOT = "/synthetic/project"
IDENT = q.draft_id("item", "reply")
SNAP = {"item_id": "item", "kind": "reply", "agency_id": "agency", "request_id": "request",
        "body": "synthetic text", "evidence": {}, "source_run_id": "synthetic"}
OLD, NEW = "sig-old-key", "sig-new-key"


def approve_ev(rev, signed_rev, sig):
    return q._event("approve", IDENT, rev, "g", approval={
        "signed_revision": signed_rev, "project": q._project_id(ROOT), "principal": "g", "signature": sig})


def raw(events):
    return b"".join((json.dumps(e) + "\n").encode() for e in events)


def verifier(mode):
    def fn(payload, sig):
        if mode == "gone":
            raise approval.TrustUnavailable("trust file missing")
        if mode == "rotated" and sig != NEW:
            raise approval.ApprovalError("no trusted principal")
        return "g"
    return fn


def fold(events, mode):
    with patch.object(approval, "verify", side_effect=verifier(mode)):
        try:
            row = q._fold(ROOT, raw(events))[IDENT]
            return f"{row['status']}@r{row['revision']}"
        except q.ApprovalQueueError as error:
            return f"RAISES {error}"


DRAFT = q._event("draft", IDENT, 0, "records-pipeline", snapshot=SNAP)
cases = {
    # S1: approved, anchor removed, same anchor restored. Same journal bytes throughout.
    "S1 approve": [DRAFT, approve_ev(1, 0, OLD)],
    # S2: during the window the row looks pending, so today's reject() accepts a Reject (status check
    # passes) and appends it. Once the anchor is back the fold meets reject-after-approved.
    "S2 approve+reject-in-window": [DRAFT, approve_ev(1, 0, OLD),
                                    q._event("reject", IDENT, 2, "g", reason="synthetic")],
    # S3: old signer removed in a rotation; G re-approves the pending-looking row with the new key.
    # If the old line ever comes back, two approves sit on one row.
    "S3 approve(old)+approve(new)": [DRAFT, approve_ev(1, 0, OLD), approve_ev(2, 1, NEW)],
    # S4: an Edit in the window: anchor-independent, pending either way.
    "S4 approve+edit-in-window": [DRAFT, approve_ev(1, 0, OLD),
                                  q._event("edited", IDENT, 2, "g", body="synthetic text 2")],
}
expect = {
    "S1 approve": {"ok": "approved@r1", "gone": "pending@r1", "rotated": "pending@r1"},
    "S2 approve+reject-in-window": {"ok": "RAISES queue_invalid", "gone": "rejected@r2",
                                    "rotated": "rejected@r2"},
    "S3 approve(old)+approve(new)": {"ok": "RAISES queue_invalid", "gone": "pending@r2",
                                     "rotated": "approved@r2"},
    "S4 approve+edit-in-window": {"ok": "pending@r2", "gone": "pending@r2", "rotated": "pending@r2"},
}
class FoldWindowReplay(unittest.TestCase):
    def test_original_window_histories(self):
        out = {name: {mode: fold(evs, mode) for mode in ("ok", "gone", "rotated")} for name, evs in cases.items()}
        self.assertEqual(out, expect)
