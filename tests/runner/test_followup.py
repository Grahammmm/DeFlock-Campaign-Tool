"""draft_followup: template rendering, statutory deadline, proposal only, redundancy rules."""
import unittest

from runner.client import FakeWorkspace
from runner.handlers import draft_followup
from tests.runner.helpers import context, job, tempdir

REQUEST = {"subject": "ALPR agreements and policies", "agency_id": "ca-example-police", "agency_name": "Example Police Department",
           "sent_at": "2026-09-01", "scope_id": "agreements", "scope_version": 1, "channel": "email", "to": "records@example.invalid"}


class FollowupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempdir()

    def tearDown(self):
        self.tmp.cleanup()

    def run_with(self, inputs, ws=None):
        ws = ws or FakeWorkspace()
        ctx = context(self.tmp.name, job("draft_followup", inputs), ws)
        return ws, draft_followup.run(ctx)

    def test_proposes_send_followup_with_deadline_and_never_sends(self):
        inputs = {"request_id": "req_0000000000000001", "due": "2026-09-11", "request": REQUEST, "jurisdiction": "us-ca", "today": "2026-09-30",
                  "last_correspondence": {"received_at": "2026-09-03T10:00:00Z", "classification": "acknowledgement", "provider_message_id": "ack@example.invalid"},
                  "missing_item": "the sharing exhibit referenced in the agreement", "evidence_gap": "agreement.pdf page 4"}
        ws, result = self.run_with(inputs)
        self.assertEqual(result.status, "done", result.error)
        self.assertEqual(result.outputs["statutory_deadline"], "2026-09-11")
        self.assertEqual(len(ws.proposals), 1)
        action = next(iter(ws.proposals.values()))
        self.assertEqual(action["kind"], "send_followup")
        self.assertEqual(action["state"], "proposed")
        self.assertEqual(action["subject_id"], "req_0000000000000001")
        self.assertEqual(action["proposed_by"], "job_0000000000000001")
        p = action["proposal"]
        self.assertEqual(p["to"], "records@example.invalid")
        self.assertEqual(p["in_reply_to"], "ack@example.invalid")
        self.assertTrue(p["subject"].startswith("Follow-up: ALPR agreements"))
        body = p["body_md"]
        self.assertIn("req_0000000000000001 sent 2026-09-01", body)
        self.assertIn("the sharing exhibit referenced in the agreement", body)
        self.assertIn("agreement.pdf page 4", body)
        self.assertIn("Gov. Code", body)
        self.assertIn("2026-09-11", body)
        self.assertNotIn("[ID and original date]", body)
        self.assertNotIn("[precise missing item]", body)
        # template's own safety lines are retained for the reviewer
        self.assertIn("Do not resend after an ambiguous delivery failure", body)
        # same request/due proposes once
        ws, again = self.run_with(inputs, ws)
        self.assertFalse(again.outputs["created"])
        self.assertEqual(len(ws.proposals), 1)

    def test_extension_moves_deadline(self):
        inputs = {"request_id": "req_2", "due": "2026-09-25", "request": {**REQUEST, "extension_claimed_until": "2026-09-20"}, "jurisdiction": "us-ca", "today": "2026-09-30"}
        ws, result = self.run_with(inputs)
        self.assertEqual(result.outputs["statutory_deadline"], "2026-09-25")

    def test_redundant_when_fresh_response_or_promised_date_pending(self):
        ws, result = self.run_with({"request_id": "req_3", "due": "2026-09-11", "request": REQUEST, "today": "2026-09-30",
                                    "last_correspondence": {"received_at": "2026-09-12T00:00:00Z", "classification": "production"}})
        self.assertIn("redundant", result.outputs["skipped"])
        self.assertEqual(ws.proposals, {})
        ws, result = self.run_with({"request_id": "req_4", "due": "2026-09-11", "request": REQUEST, "today": "2026-09-30", "promised_date": "2026-10-15"})
        self.assertIn("has not elapsed", result.outputs["skipped"])
        self.assertEqual(ws.proposals, {})

    def test_minimal_cron_inputs_still_draft(self):
        ws, result = self.run_with({"request_id": "req_5", "due": "2026-09-11"})
        self.assertEqual(result.status, "done")
        self.assertEqual(len(ws.proposals), 1)
        p = next(iter(ws.proposals.values()))["proposal"]
        self.assertIsNone(p["to"])
        self.assertIn("no law package loaded", p["basis"])
        self.assertIn("date not supplied", p["body_md"])

    def test_missing_request_id_fails(self):
        ws, result = self.run_with({})
        self.assertEqual(result.status, "failed")


if __name__ == "__main__":
    unittest.main()
