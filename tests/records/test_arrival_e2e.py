"""REC-AUTO-6: arrival -> outputs -> classified item -> pending draft -> signed panel approval -> summary text.

Offline, synthetic. Not exercised here: IMAP/EML intake and real OCR (covered by test_arrival_runtime and
test_attachment_extraction_arrival); nothing is sent and the panel only records approved-to-send.
"""
import importlib.util
import socket
import unittest
from unittest import mock

from campaign_tool.records import approval_queue as aq
from campaign_tool.records import arrival_summary as summary
from campaign_tool.records import run_report
from campaign_tool.records.ledger import store
from campaign_tool.records.outcomes import received_items as ri
from tests.approval_signing import Signer
from tests.records import arrival_e2e_harness as h


def load_notify():
    spec = importlib.util.spec_from_file_location("records_notify_e2e", h.REPO / "deploy" / "records-runtime" / "notify.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ArrivalProof(unittest.TestCase):
    setUp = h.Roots.setUp
    document, mail, private, enroll, tree = (h.Roots.document, h.Roots.mail, h.Roots.private,
                                             h.Roots.enroll, h.Roots.tree)

    def signer(self):
        """A throwaway trusted approver; trust stays patched in-process so folded approvals still verify."""
        signer = Signer()
        self.addCleanup(signer.close)
        trust = signer.trust()
        trust.__enter__()
        self.addCleanup(trust.__exit__, None, None, None)
        return signer

    def arrive(self):
        """Old backlog exists, baseline is taken, then the new Caltrans notice lands."""
        self.document(h.OLD_DENIAL, "atascadero")  # already here before the run
        import os
        with mock.patch.dict(os.environ, {"RECORDS_RUN_TRIGGER": "arrival"}):
            summary.capture_baseline(self.project, h.RUN_1)
        sha, _ = self.document(h.CALTRANS, "caltrans")
        with store.ledger(self.database) as con:  # the request this notice answers
            con.execute("INSERT INTO requests(id,agency_id,status) VALUES('req-1','caltrans','open')")
            con.execute("UPDATE joins SET request_id='req-1' WHERE original_sha256=?", (sha,))
            con.commit()
        self.mail(sha, "2026-11-02T10:00:00-08:00")
        return ri.item_id(sha)

    def test_arrival_to_signed_approval_and_summary(self):
        item_id = self.arrive()
        code, line = h.run_outputs(self.project, h.RUN_1)
        self.assertEqual(code, 0, line)
        block = line["arrival_summary"]
        self.assertEqual(block["received_by_kind"]["extension"], 1)
        self.assertEqual(sum(block["received_by_kind"].values()), 1)  # the old denial is not "new"
        drafts = h.pending_for(self.project, item_id)
        self.assertEqual(len(drafts), 1)
        self.assertEqual(block["pending_drafts"], len([r for r in aq.load_queue(self.project) if r["status"] == "pending"]))
        # The Telegram text describes exactly this run, before any approval, with no record identifiers.
        text = load_notify().compose_arrival(h.arrival_report(block), 0)
        run_report.validate_run_report(h.arrival_report(block))
        self.assertIn("extension", text)
        self.assertNotIn(item_id, text)
        self.assertNotIn(drafts[0]["id"], text)
        self.assertLess(len(text.splitlines()), 20)
        # The real panel renders the draft; a signed POST approves it (approved-to-send only).
        signer = self.signer()
        with h.Panel(self.project, h.RUN_1) as panel:
            forms = {ident: token for token, ident in panel.forms()}
            self.assertIn(drafts[0]["id"], forms)
            sig = signer.sign(aq.approval_payload(self.project, drafts[0]["id"]))
            status = panel.http.post("/approvals/approve", {"csrf_token": forms[drafts[0]["id"]],
                                                            "id": drafts[0]["id"], "signature": sig})[0]
            self.assertEqual(status, 303)
        rows = [r for r in aq.load_queue(self.project) if r["id"] == drafts[0]["id"]]
        self.assertEqual([(r["status"], r["approved_by"]) for r in rows], [("approved", signer.principal)])
        self.assertEqual(h.pending_for(self.project, item_id), [])
        # Approval does not rewrite the already-composed summary.
        self.assertEqual(load_notify().compose_arrival(h.arrival_report(block), 0), text)

    def test_replay_under_new_run_adds_nothing(self):
        item_id = self.arrive()
        h.run_outputs(self.project, h.RUN_1)
        draft = h.pending_for(self.project, item_id)[0]
        signer = self.signer()
        aq.approve(self.project, draft["id"], {"kind": "ssh", "signature": signer.sign(
            aq.approval_payload(self.project, draft["id"]))})
        before = len(aq.load_queue(self.project))
        import os
        with mock.patch.dict(os.environ, {"RECORDS_RUN_TRIGGER": "arrival"}):
            summary.capture_baseline(self.project, h.RUN_2)
        code, line = h.run_outputs(self.project, h.RUN_2)
        self.assertEqual(code, 0, line)
        self.assertEqual(set(line["arrival_summary"]["received_by_kind"].values()), {0})
        self.assertEqual(len(aq.load_queue(self.project)), before)
        self.assertEqual(h.pending_for(self.project, item_id), [])
        self.assertEqual([r["status"] for r in aq.load_queue(self.project) if r["id"] == draft["id"]], ["approved"])

    def test_negative_controls_fail_the_proof(self):
        # No arrival document: nothing new, and no Caltrans draft.
        self.document(h.OLD_DENIAL, "atascadero")
        import os
        with mock.patch.dict(os.environ, {"RECORDS_RUN_TRIGGER": "arrival"}):
            summary.capture_baseline(self.project, h.RUN_1)
        _, line = h.run_outputs(self.project, h.RUN_1)
        self.assertEqual(sum(line["arrival_summary"]["received_by_kind"].values()), 0)
        # A non-arrival run carries no summary at all (legacy artifact set), and without a baseline the
        # counts are unavailable rather than zero.
        _, legacy = h.run_outputs(self.project, h.RUN_2, arrival=False)
        self.assertNotIn("arrival_summary", legacy)
        with mock.patch.dict(os.environ, {"RECORDS_RUN_TRIGGER": "arrival"}):
            _, nobase = h.run_outputs(self.project, "run_20261105T020000Z_abcdef03")
        self.assertIsNone(nobase["arrival_summary"]["received_by_kind"])

    def test_no_network_during_the_chain(self):
        def refuse(*a, **k):
            raise AssertionError("network used")
        self.arrive()
        with mock.patch.object(socket.socket, "connect", refuse):
            code, _ = h.run_outputs(self.project, h.RUN_1)
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
