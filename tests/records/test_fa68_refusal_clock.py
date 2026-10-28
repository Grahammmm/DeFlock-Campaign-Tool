"""Synthetic real-API regressions; inherited Luna tests expanded by Sol.

No source policy is copied here. Queue and intake execute from the selected
repository. Acceptance sets TMPDIR before importing this module. Test-created
roots are deliberately retained beneath that temporary root as evidence.
"""
import hashlib
import json
import math
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from campaign_tool.records.portal import engine
from campaign_tool.records.portal.intake import intake_notice
from campaign_tool.records.portal.policy import Approval, PortalError
from campaign_tool.records.portal.store import Queue

FIXTURE_BYTES = b"FA68 labelled synthetic notice evidence; not a real record"
SOURCE_HASH = hashlib.sha256(FIXTURE_BYTES).hexdigest()
HOST = "unapproved.example.test"
URL = "https://" + HOST + "/documents/7/download?signature=synthetic-only"
APPROVAL_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


def invalid_clocks():
    return (
        ("nan", float("nan")), ("positive_infinity", float("inf")),
        ("negative_infinity", float("-inf")), ("true", True), ("false", False),
        ("numeric_string", "12"), ("overflowing_integer", 10 ** 10000),
        ("empty_list", []), ("empty_dict", {}), ("empty_tuple", ()),
        ("empty_string", ""), ("numeric_bytes", b"12"),
    )


class QueueCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="fa68-synthetic-"))
        self.queue = Queue(self.root / "queue")
        self.addCleanup(self.queue.close)
        self.inventory = self.enterContext(patch.object(
            self.queue, "inventory", side_effect=AssertionError("unexpected inventory")))
        self.fetch = self.enterContext(patch.object(
            engine, "fetch_queue", side_effect=AssertionError("unexpected fetch")))

    def refuse(self, value):
        return self.queue.refuse(HOST, "portal_not_approved", URL, SOURCE_HASH, now=value)

    def rows(self):
        return [tuple(row) for row in self.queue.db.execute(
            "SELECT * FROM portal_refusals ORDER BY id")]

    def snapshot(self):
        dump = "\n".join(self.queue.db.iterdump()).encode()
        return {
            "rows": self.rows(), "dump_sha256": hashlib.sha256(dump).hexdigest(),
            "database_sha256": hashlib.sha256(self.queue.database.read_bytes()).hexdigest(),
            "total_changes": self.queue.db.total_changes,
            "status": json.dumps(self.queue.status(), allow_nan=False, sort_keys=True),
        }

    def assert_private_and_idle(self):
        status = self.queue.status()
        encoded = json.dumps(status, allow_nan=False, sort_keys=True)
        self.assertNotIn(URL, encoded)
        self.assertNotIn("signature=", encoded)
        self.assertNotIn(b"signature=", self.queue.database.read_bytes())
        self.assertEqual(status["items"], [])
        for name in ("attempts", "versions", "ledger_pending"):
            self.assertEqual(status[name], 0)
        self.assertEqual(list(self.queue.objects.iterdir()), [])
        self.inventory.assert_not_called()
        self.fetch.assert_not_called()

    def seed(self):
        self.refuse(-3)
        self.queue.refuse(HOST, "path_not_approved", URL + "-second", SOURCE_HASH, now=2)
        self.assert_private_and_idle()

    def assert_rejected(self, action):
        before = self.snapshot()
        with patch.object(self.queue, "lock", side_effect=AssertionError("lock acquired")) as lock:
            with self.assertRaises(PortalError) as caught:
                action()
        self.assertEqual(type(caught.exception), PortalError)
        self.assertEqual(str(caught.exception), "invalid_refusal_clock")
        lock.assert_not_called()
        self.assertEqual(self.snapshot(), before)
        self.assert_private_and_idle()

    def intake(self):
        approval = Approval(datetime(2027, 1, 1, tzinfo=timezone.utc),
                            frozenset({"approved.example.test"}), frozenset(), {})
        return intake_notice(
            self.queue, body='<a href="' + URL + '">synthetic document</a>',
            request_id="synthetic-7", source_sha256=SOURCE_HASH,
            approval_loader=lambda: approval, now=APPROVAL_TIME)


class Regression(QueueCase):
    def test_finite_values_deduplicate_and_serialize(self):
        for count, (name, value) in enumerate((
                ("integer", 3), ("float", 4.5), ("zero", 0),
                ("negative_integer", -2), ("negative_float", -0.25),
                ("large_finite", 1e200)), start=1):
            with self.subTest(clock=name):
                with patch("campaign_tool.records.portal.store.time.time",
                           side_effect=AssertionError("explicit clock ignored")):
                    hashed = self.refuse(value)
                row = self.queue.status()["refusals"][0]
                self.assertEqual(len(self.rows()), 1)
                self.assertEqual(row["first_seen"], 3.0)
                self.assertEqual(row["last_seen"], float(value))
                self.assertEqual(row["count"], count)
                self.assertEqual(hashed, hashlib.sha256(URL.encode()).hexdigest())
                self.assertEqual(row["url_sha256"], hashed)
                self.assertEqual(row["source_sha256"], SOURCE_HASH)
                self.assertTrue(math.isfinite(row["last_seen"]))
                self.assert_private_and_idle()

    def test_default_and_explicit_none_use_one_clock_sample(self):
        for name, value in (("default_zero", 0), ("default_negative", -5.5)):
            with self.subTest(clock=name):
                with patch("campaign_tool.records.portal.store.time.time", return_value=value) as clock:
                    if name == "default_zero":
                        self.queue.refuse(HOST, "portal_not_approved", URL, SOURCE_HASH)
                    else:
                        self.refuse(None)
                clock.assert_called_once_with()
                self.assertEqual(self.queue.status()["refusals"][0]["last_seen"], value)
                self.assert_private_and_idle()

    def test_invalid_supplied_clocks_fail_before_lock_or_mutation(self):
        self.seed()
        for name, value in invalid_clocks():
            with self.subTest(clock=name):
                with patch("campaign_tool.records.portal.store.time.time",
                           side_effect=AssertionError("invalid supplied clock fell back")):
                    self.assert_rejected(lambda: self.refuse(value))

    def test_invalid_default_clocks_fail_before_lock_or_mutation(self):
        self.seed()
        for name, value in (*invalid_clocks(), ("null_default", None)):
            with self.subTest(clock=name):
                with patch("campaign_tool.records.portal.store.time.time", return_value=value) as clock:
                    self.assert_rejected(lambda: self.refuse(None))
                clock.assert_called_once_with()

    def test_public_intake_default_clock_reaches_real_refusal_writer(self):
        for count, value in enumerate((0, -1.5, 3), start=1):
            with self.subTest(clock=value):
                with patch("campaign_tool.records.portal.store.time.time", return_value=value):
                    self.assertEqual(self.intake(), {"enrolled": 0, "refused": 1, "skipped": 0})
                row = self.queue.status()["refusals"][0]
                self.assertEqual(row["count"], count)
                self.assertEqual(row["first_seen"], 0)
                self.assertEqual(row["last_seen"], value)
                self.assert_private_and_idle()

    def test_public_intake_rejects_malformed_default_without_mutation(self):
        self.seed()
        for name, value in (*invalid_clocks(), ("null_default", None)):
            with self.subTest(clock=name):
                with patch("campaign_tool.records.portal.store.time.time", return_value=value):
                    self.assert_rejected(self.intake)


class GateControl(QueueCase):
    """The runner checks THESE deciding IDs on an isolated gate-only mutation."""
    def test_finite_controls(self):
        for count, value in enumerate((-1, 0, 1), start=1):
            with self.subTest(clock=value):
                self.refuse(value)
                row = self.queue.status()["refusals"][0]
                self.assertEqual(row["first_seen"], -1)
                self.assertEqual(row["last_seen"], value)
                self.assertEqual(row["count"], count)
                self.assert_private_and_idle()

    def test_infinity_is_typed_refusal(self):
        with self.assertRaises(PortalError) as caught:
            self.refuse(float("inf"))
        self.assertEqual(str(caught.exception), "invalid_refusal_clock")

    def test_boolean_is_typed_refusal(self):
        with self.assertRaises(PortalError) as caught:
            self.refuse(True)
        self.assertEqual(str(caught.exception), "invalid_refusal_clock")


if __name__ == "__main__":
    unittest.main()
