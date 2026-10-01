"""End-to-end over the FakeWorkspace: an inbound production email flows through the real
handler registry (classify_mail -> extract x2 -> digest x2) and a send_request stays blocked.
No model, no network; the law package is the drafted us-ca copy in jurisdictions/."""
import json
import unittest

from runner.client import FakeWorkspace
from runner.loop import Runner, default_handlers
from tests.runner.helpers import Silent, settings, tempdir
from tests.runner.test_classify_mail import production_eml


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempdir()
        self.ws = FakeWorkspace()
        self.log = Silent()
        self.settings = settings(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def drain(self, runner, limit=20):
        outcomes = []
        for _ in range(limit):
            outcome = runner.step()
            if outcome == "idle":
                break
            outcomes.append(outcome)
        return outcomes

    def test_mail_to_digest_chain(self):
        raw_sha = self.ws.put_original(production_eml(), "message/rfc822")["sha256"]
        self.ws.correspondence.append({"correspondence_id": "cor_0000000000000001", "provider_message_id": "prod-0042@example.invalid", "raw_sha256": raw_sha})
        self.ws.enqueue("classify_mail", {"correspondence_id": "cor_0000000000000001", "raw_sha256": raw_sha})
        runner = Runner(self.ws, self.settings, handlers=default_handlers(), log=self.log, sleep=lambda s: None)
        import os
        os.environ["CAMPAIGN_JURISDICTION"] = "us-ca"
        try:
            outcomes = self.drain(runner)
        finally:
            os.environ.pop("CAMPAIGN_JURISDICTION", None)
        kinds = [self.ws.jobs[j]["kind"] for j in self.ws.order]
        self.assertEqual(kinds[:3], ["classify_mail", "extract", "extract"])
        self.assertEqual(kinds.count("digest"), 2, kinds)
        self.assertEqual(outcomes, ["done"] * len(kinds), [(k, self.ws.jobs[j]["error"]) for k, j in zip(kinds, self.ws.order)])
        states = {self.ws.jobs[j]["kind"] + ":" + self.ws.jobs[j]["state"] for j in self.ws.order}
        self.assertEqual(states, {"classify_mail:done", "extract:done", "digest:done"})
        # the raw message, two attachments, two units files and two digests are stored by hash
        self.assertEqual(len(self.ws.originals), 7, sorted(self.ws.media_types.values()))
        self.assertEqual(sorted(self.ws.media_types.values()).count("application/json"), 2)
        # receipts: one per attachment with the message-id source locator
        self.assertEqual(sorted(r["source_id"] for r in self.ws.receipts.values()), ["prod-0042@example.invalid#1", "prod-0042@example.invalid#2"])
        # the digest conclusions are detector-only and gated for attorney review; no external action was proposed
        digests = [r for r in self.ws.results if r["outputs"].get("digest_row")]
        self.assertEqual(len(digests), 2)
        for result in digests:
            row = result["outputs"]["digest_row"]
            self.assertEqual(row["privacy_tier"], "redacted_cloud")
            self.assertIsNone(row["model_id"])
            for conclusion in row["digest_json"]["conclusions"]:
                self.assertEqual(conclusion["confidence"], "needs_attorney_review")
            dumped = json.dumps(row["digest_json"])
            self.assertNotIn("7ABC123", dumped)   # the synthetic plate from the workbook never survives
            self.assertNotIn("Casey", dumped)
        self.assertEqual(self.ws.proposals, {})
        # idempotency: re-running classify_mail enqueues nothing new
        before = len(self.ws.jobs)
        job = self.ws.enqueue("classify_mail", {"correspondence_id": "cor_0000000000000001", "raw_sha256": raw_sha})
        self.assertEqual(job["state"], "done")
        self.assertEqual(len(self.ws.jobs), before)

    def test_send_request_is_blocked_by_the_default_registry(self):
        self.ws.enqueue("send_request", {"request_id": "req_0000000000000001"})
        runner = Runner(self.ws, self.settings, handlers=default_handlers(), log=self.log, sleep=lambda s: None)
        self.assertEqual(runner.step(), "blocked")
        self.assertEqual(self.ws.results[0]["status"], "blocked")
        self.assertIn("approval", self.ws.results[0]["error"])
        self.assertEqual(self.ws.proposals, {})
        self.assertEqual(self.ws.correspondence, [])


if __name__ == "__main__":
    unittest.main()
