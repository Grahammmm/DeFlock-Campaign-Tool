"""Synthetic queue fixtures, no private records or service calls."""
import copy
import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from campaign_tool.records import queue as q

NOW = "2026-01-01T12:00:00+00:00"
PAST = "2026-01-01T11:00:00+00:00"
FUTURE = "2026-01-01T13:00:00+00:00"


def fixture(count=1, first="preserve"):
    originals = [{"sha256": hashlib.sha256(("synthetic-%d" % i).encode()).hexdigest()} for i in range(count)]
    rows = [{"original_sha256": obj["sha256"], "stage": stage,
             "status": "done" if q.STAGES.index(stage) < q.STAGES.index(first) else "pending",
             "receipt_sha256": None, "owner": "synthetic", "reason": None}
            for obj in originals for stage in q.STAGES]
    return originals, rows


class QueueTests(unittest.TestCase):
    def run_plan(self, original, rows, leases=(), **kwargs):
        return q.plan(original, rows, leases, now=NOW, **kwargs)

    def test_first_stage_not_absent_digest(self):
        for stage in ("review", "compare", "privacy"):
            originals, rows = fixture(2, stage)
            result = self.run_plan(originals, rows)
            self.assertEqual(result["eligible"], 2)
            self.assertTrue(all(item["stage"] == stage for item in result["top"]))

    def test_non_pdf_and_low_value_remain_open(self):
        originals, rows = fixture()
        result = self.run_plan(originals, rows, facts={originals[0]["sha256"]: {"proposed_low_value": True}})
        self.assertEqual(result["eligible"], 1)
        self.assertEqual(result["top"][0]["score"], 0)
        self.assertFalse(result["top"][0]["low_value_closed"])

    def test_weight_details_and_caps(self):
        facts = {"new_production_open_request": True, "document_type": "policy", "severity3_hits": 3,
                 "live_agency": True, "proposed_low_value": True}
        result = q.score(facts)
        self.assertEqual(result["score"], 70)
        self.assertEqual([x["points"] for x in result["reasons"]], [40, 30, 40, 10, -50])
        facts["proposed_low_value"] = False
        self.assertEqual(q.score(facts)["score"], 100)

    def test_all_important_types(self):
        for kind in q.IMPORTANT:
            self.assertEqual(q.score({"document_type": kind})["score"], 30)

    def test_deterministic_top20_and_input_order(self):
        originals, rows = fixture(25)
        result = self.run_plan(originals, rows)
        self.assertEqual(len(result["top"]), 20)
        self.assertEqual(result, self.run_plan(list(reversed(originals)), list(reversed(rows))))
        self.assertEqual(result["eligible"], 25)

    def test_priority_beats_hash_tie(self):
        originals, rows = fixture(2)
        subject = max(x["sha256"] for x in originals)
        result = self.run_plan(originals, rows, facts={subject: {"document_type": "log"}})
        self.assertEqual(result["top"][0]["original_sha256"], subject)

    def test_blocked_portal_does_not_block_local(self):
        originals, rows = fixture(2)
        rows[0].update(status="blocked", reason="portal_login_required")
        result = self.run_plan(originals, rows)
        self.assertEqual(result["eligible"], 1)
        self.assertEqual(len(result["blocked"]), 1)

    def test_blocked_extract_allows_catalog(self):
        originals, rows = fixture(1, "extract")
        rows[1].update(status="blocked", reason="ocr_not_active")
        result = self.run_plan(originals, rows)
        self.assertEqual(result["top"][0]["stage"], "catalog")
        self.assertEqual(result["top"][0]["prior_blockers"][0]["code"], "blocked_without_retry")

    def lease(self, subject, expires=PAST, **extra):
        return {"item_key": "stage:" + subject + ":preserve", "stage": "preserve", "owner": "old-worker",
                "expires_at": expires, "attempts": 3, **extra}

    def test_expired_lease_attempts_increment(self):
        originals, rows = fixture()
        rows[0]["status"] = "in_progress"
        item = self.run_plan(originals, rows, [self.lease(originals[0]["sha256"])])["top"][0]
        self.assertTrue(item["reclaim_expired_lease"])
        self.assertEqual(item["expected_attempt"], 4)

    def test_live_lease_is_held(self):
        originals, rows = fixture()
        result = self.run_plan(originals, rows, [self.lease(originals[0]["sha256"], FUTURE)])
        self.assertEqual(result["eligible"], 0)
        self.assertEqual(result["blocked"][0]["reasons"][0]["next_eligible_at"], FUTURE)

    def test_blocked_retry_due_and_not_due(self):
        originals, rows = fixture()
        rows[0]["status"] = "blocked"
        for due, expected in ((PAST, 1), (FUTURE, 0)):
            result = self.run_plan(originals, rows, [self.lease(originals[0]["sha256"], next_eligible_at=due)])
            self.assertEqual(result["eligible"], expected)

    def test_privacy_hold_not_closed(self):
        originals, rows = fixture(1, "privacy")
        rows[-1].update(status="blocked", reason="identifier_needs_removal")
        result = self.run_plan(originals, rows)
        self.assertEqual(len(result["blocked"]), 1)
        self.assertEqual(result["settled_candidate_originals"], [])
        self.assertFalse(result["publication_ready"])

    def test_missing_duplicate_unknown_slots_fail(self):
        originals, rows = fixture()
        for changed in (rows[:-1], rows + [rows[0]], [dict(rows[0], stage="invented")] + rows[1:]):
            with self.assertRaises(q.QueueError):
                self.run_plan(originals, changed)

    def test_invalid_priority_types(self):
        for facts in ({"severity3_hits": True}, {"severity3_hits": -1}, {"proposed_low_value": "yes"}):
            with self.assertRaises(q.QueueError):
                q.score(facts)

    def test_bounds_and_unknown_priority_subject(self):
        originals, rows = fixture()
        with self.assertRaises(q.QueueError):
            self.run_plan(originals, rows, limit=21)
        with self.assertRaises(q.QueueError):
            self.run_plan(originals, rows, facts={"missing": {}})
        with self.assertRaises(q.QueueError):
            self.run_plan(originals * (q.MAX_ORIGINALS + 1), rows)

    def test_timezone_required(self):
        originals, rows = fixture()
        with self.assertRaises(q.QueueError):
            q.plan(originals, rows, [], now="2026-01-01T12:00:00")

    def test_plan_does_not_mutate_inputs(self):
        originals, rows = fixture()
        before = copy.deepcopy((originals, rows))
        self.run_plan(originals, rows)
        self.assertEqual((originals, rows), before)

    def test_readonly_canonical_sqlite_adapter(self):
        originals, rows = fixture(1, "compare")
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "synthetic.sqlite"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE originals(sha256 TEXT,scope TEXT)")
                connection.execute("CREATE TABLE stage_state(original_sha256 TEXT,stage TEXT,status TEXT,receipt_sha256 TEXT,owner TEXT,reason TEXT)")
                connection.execute("CREATE TABLE work_leases(item_key TEXT,expires_at TEXT,attempts INTEGER)")
                connection.executemany("INSERT INTO originals VALUES(?,?)", [(x["sha256"],"in_scope") for x in originals])
                connection.executemany("INSERT INTO stage_state VALUES(?,?,?,?,?,?)", [tuple(x.values()) for x in rows])
            result = q.from_ledger(database, now=NOW)
            self.assertEqual(result["top"][0]["stage"], "compare")

    def test_bound_packet_and_unknown_denominator(self):
        originals, rows = fixture()
        item = self.run_plan(originals, rows)["top"][0]
        item["claim"] = {"claim_id": "synthetic-claim"}
        template = {"schema": "ledger-stage-receipt-v1", "subject_sha256": item["original_sha256"], "stage": item["stage"]}
        result = q.packet(item, denominator={"kind": "pages", "total": 1}, receipt_template=template)
        self.assertEqual(result["timebox_seconds"], 2700)
        self.assertTrue(result["private"])
        with self.assertRaises(q.QueueError):
            q.packet(item, denominator={"kind": "pages", "total": None}, receipt_template=template)
        with self.assertRaises(q.QueueError):
            q.packet(item, denominator={"kind": "pages", "total": 1}, receipt_template=dict(template, stage="privacy"))


if __name__ == "__main__":
    unittest.main()
