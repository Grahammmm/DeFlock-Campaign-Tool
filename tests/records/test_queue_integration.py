"""WP1 composition tests; absent dependency is an explicit skip, never a pass."""
import importlib.util
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock
from campaign_tool.records import queue as q

AVAILABLE = importlib.util.find_spec("campaign_tool.records.ledger") is not None


@unittest.skipUnless(AVAILABLE, "WP1 canonical ledger dependency not installed")
class QueueIntegrationTests(unittest.TestCase):
    def setUp(self):
        from tests.records.test_ledger_stages import StageTests
        from campaign_tool.records.ledger import stages
        self.stages = stages
        self.fixture = StageTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.facts = {self.fixture.subject: {"document_type": "policy"}}

    def now(self):
        return datetime.now(timezone.utc).isoformat()

    def test_claim_comparison_after_digest_review(self):
        self.fixture.advance("review")
        self.fixture.prepare("compare")
        result = q.claim_next(self.fixture.runner, now=self.now(), facts=self.facts)
        item = result["claimed"]
        self.assertEqual(item["stage"], "compare")
        lease = next(row for row in self.fixture.rows("work_leases") if row["stage"] == "compare")
        self.assertEqual((q.timestamp(lease["expires_at"]) - q.timestamp(lease["leased_at"])).total_seconds(), 2700)
        self.assertEqual(lease["attempts"], 1)
        self.assertEqual(self.stages.counts(self.fixture.database)["verified_seven_stage_complete"], 0)

    def test_expired_claim_reclaimed_with_increment(self):
        self.fixture.prepare("preserve")
        old = self.fixture.runner.claim(self.fixture.subject, "preserve", ttl_seconds=1)
        real = self.stages.datetime
        class Future(real):
            @classmethod
            def now(cls, tz=None):
                return real.now(tz) + timedelta(seconds=10)
        with mock.patch.object(self.stages, "datetime", Future):
            result = q.claim_next(self.fixture.runner, now=Future.now(timezone.utc).isoformat(), facts=self.facts)
        item = result["claimed"]
        self.assertNotEqual(item["claim"]["claim_id"], old["claim_id"])
        self.assertEqual(item["expected_attempt"], 2)
        lease = next(row for row in self.fixture.rows("work_leases") if row["stage"] == "preserve")
        self.assertEqual(lease["attempts"], 2)

    def test_other_live_owner_not_stolen(self):
        self.fixture.prepare("preserve")
        claim = self.fixture.runner.claim(self.fixture.subject, "preserve")
        other = self.fixture.make_runner(self.fixture.run, owner="other")
        result = q.claim_next(other, now=self.now(), facts=self.facts)
        self.assertIsNone(result["claimed"])
        self.assertEqual(self.fixture.rows("work_leases")[0]["owner"], self.fixture.runner.owner)
        self.assertTrue(claim["claim_id"])

    def test_wrong_authority_rejected(self):
        with self.assertRaisesRegex(q.QueueError, "trusted_stage_runner"):
            q.claim_next(object(), now=self.now())

    def test_unprepared_stage_reports_reason(self):
        result = q.claim_next(self.fixture.runner, now=self.now(), facts=self.facts)
        self.assertIsNone(result["claimed"])
        self.assertTrue(all(row["reason"] == "current_content_required" for row in result["skipped"]))
