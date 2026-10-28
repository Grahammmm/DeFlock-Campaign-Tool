"""FA31: saved unattended-safety state with duplicate JSON members fails closed.

Synthetic only: every ledger is a fresh temporary records root, every identity
is the SHA-256 of a synthetic counter, and no network, model, mailbox or record
corpus is used. Acquisition and stage work are Mock callbacks that may only run
after RunSafety.begin() admits an attempt.
"""
import hashlib
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import Mock, patch

from campaign_tool.records import run
from campaign_tool.records import run_safety
from campaign_tool.records.ledger import store
from campaign_tool.records.run_safety import LOCAL_CODES, STATE_KEY, RunSafety, RunSafetyPolicy

REPO = Path(__file__).resolve().parents[1]
INVALID = "invalid_safety_state"
STATE_KEYS = {"schema", "hold", "active", "repeats"}
REPORT_KEYS = {"schema", "stop_code", "phases", "max_fetches", "max_originals", "seconds",
               "requires_owner_acknowledgement"}


def synthetic(number):
    return hashlib.sha256(("fa31-synthetic-" + str(number)).encode()).hexdigest()


A, B, C, D = (synthetic(n) for n in range(4))
SCHEMA = '"schema": "unattended-safety-v1"'
HOLD_NULL = '"hold": null'
HOLD_REVIEW = '"hold": "review_required"'
ACTIVE_NULL = '"active": null'
ACTIVE_SET = '"active": {"phase": "advance", "identity": "' + A + '"}'
REPEATS_EMPTY = '"repeats": {}'
REPEATS_SET = '"repeats": {"advance:stage_failed": ["' + A + '", "' + B + '"]}'


def doc(*members):
    return "{" + ", ".join(members) + "}"


def canonical(hold=None, active=None, repeats=None):
    return json.dumps({"schema": STATE_KEY, "hold": hold, "active": active,
                       "repeats": {} if repeats is None else repeats}, sort_keys=True)


# Every entry repeats a member name in one JSON object at some depth. Under a
# last-member-wins decoder several of them would load as a valid unheld state.
DUPLICATES = {
    "hold_review_then_null": doc(SCHEMA, HOLD_REVIEW, HOLD_NULL, ACTIVE_NULL, REPEATS_EMPTY),
    "hold_null_then_review": doc(SCHEMA, HOLD_NULL, HOLD_REVIEW, ACTIVE_NULL, REPEATS_EMPTY),
    "hold_identical_null": doc(SCHEMA, HOLD_NULL, HOLD_NULL, ACTIVE_NULL, REPEATS_EMPTY),
    "hold_identical_value": doc(SCHEMA, HOLD_REVIEW, HOLD_REVIEW, ACTIVE_NULL, REPEATS_EMPTY),
    "schema_identical": doc(SCHEMA, SCHEMA, HOLD_NULL, ACTIVE_NULL, REPEATS_EMPTY),
    "active_then_null": doc(SCHEMA, HOLD_NULL, ACTIVE_SET, ACTIVE_NULL, REPEATS_EMPTY),
    "active_null_then_set": doc(SCHEMA, HOLD_NULL, ACTIVE_NULL, ACTIVE_SET, REPEATS_EMPTY),
    "active_inner_identity": doc(SCHEMA, HOLD_NULL, REPEATS_EMPTY,
                                 '"active": {"phase": "advance", "identity": "' + A
                                 + '", "identity": "' + B + '"}'),
    "active_inner_phase": doc(SCHEMA, HOLD_NULL, REPEATS_EMPTY,
                              '"active": {"phase": "intake", "phase": "advance", "identity": "' + A + '"}'),
    "repeats_then_empty": doc(SCHEMA, HOLD_NULL, ACTIVE_NULL, REPEATS_SET, REPEATS_EMPTY),
    "repeats_empty_then_set": doc(SCHEMA, HOLD_NULL, ACTIVE_NULL, REPEATS_EMPTY, REPEATS_SET),
    "nested_repeat_key": doc(SCHEMA, HOLD_NULL, ACTIVE_NULL,
                             '"repeats": {"advance:stage_failed": ["' + A + '", "' + B + '", "' + C
                             + '"], "advance:stage_failed": ["' + D + '"]}'),
    "nested_repeat_identical": doc(SCHEMA, HOLD_NULL, ACTIVE_NULL,
                                   '"repeats": {"intake:imap_fetch_failed": ["' + A
                                   + '"], "intake:imap_fetch_failed": ["' + A + '"]}'),
    "escaped_hold": doc(SCHEMA, HOLD_REVIEW, r'"h\u006fld": null', ACTIVE_NULL, REPEATS_EMPTY),
    "escaped_hold_reversed": doc(SCHEMA, r'"\u0068old": null', HOLD_REVIEW, ACTIVE_NULL, REPEATS_EMPTY),
    "escaped_active": doc(SCHEMA, HOLD_NULL, ACTIVE_SET, r'"\u0061ctive": null', REPEATS_EMPTY),
    "escaped_schema": doc(SCHEMA, r'"\u0073chema": "unattended-safety-v1"', HOLD_NULL, ACTIVE_NULL,
                          REPEATS_EMPTY),
    "escaped_nested_repeat": doc(SCHEMA, HOLD_NULL, ACTIVE_NULL,
                                 '"repeats": {"advance:stage_failed": ["' + A + '", "' + B + '", "' + C
                                 + r'"], "advance:stage\u005ffailed": []}'),
    "deep_hold_object": doc(SCHEMA, '"hold": {"code": null, "code": null}', ACTIVE_NULL, REPEATS_EMPTY),
}


class SafetyLedger:
    """Fresh, initialized synthetic records roots inside one temporary directory."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.count = 0

    def fresh(self):
        self.count += 1
        return run.Root(Path(self.temp.name) / ("root-" + str(self.count))).prepare()

    @staticmethod
    def write(root, raw):
        with store.ledger(root.ledger) as con:
            con.execute("INSERT INTO ledger_meta(key,value) VALUES(?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (STATE_KEY, raw))
            con.commit()

    @staticmethod
    def stored(root):
        with store.ledger(root.ledger, readonly=True) as con:
            row = con.execute("SELECT value FROM ledger_meta WHERE key=?", (STATE_KEY,)).fetchone()
        return None if row is None else row[0]

    def load(self, raw):
        root = self.fresh()
        if raw is not None:
            self.write(root, raw)
        return root, RunSafety(root.ledger)

    @staticmethod
    def attempt(guard, phase, identity, acquire, stage):
        """Consumer shape: acquisition and stage work run only after begin admits."""
        if not guard.begin(phase, identity):
            return False
        acquire(identity)
        stage(identity)
        guard.finish()
        return True

    def assert_refused(self, root, guard):
        """The FA31 stopped expectation for an ambiguous saved state."""
        acquire, stage = Mock(name="acquire"), Mock(name="stage")
        self.assertTrue(guard.stopped)
        report = guard.report()
        self.assertEqual(report["stop_code"], INVALID)
        self.assertTrue(report["requires_owner_acknowledgement"])
        stored = json.loads(self.stored(root))
        self.assertEqual(stored, {"schema": STATE_KEY, "hold": INVALID, "active": None, "repeats": {}})
        for phase in ("intake", "advance"):
            self.assertFalse(self.attempt(guard, phase, D, acquire, stage))
        acquire.assert_not_called()
        stage.assert_not_called()
        self.assertEqual(guard.phases["intake"]["attempted"] + guard.phases["advance"]["attempted"], 0)


class FA31SafetyJsonTests(SafetyLedger, unittest.TestCase):
    def test_valid_round_trip(self):
        # Canonical empty: no saved row.
        root, guard = self.load(None)
        self.assertFalse(guard.stopped)
        self.assertEqual(self.stored(root), canonical())
        acquire, stage = Mock(), Mock()
        self.assertTrue(self.attempt(guard, "intake", A, acquire, stage))
        acquire.assert_called_once_with(A)
        stage.assert_called_once_with(A)

        # Explicit canonical unheld state, plus an escaped but unique spelling.
        for raw in (canonical(), doc(REPEATS_EMPTY, r'"h\u006fld": null', ACTIVE_NULL, SCHEMA)):
            with self.subTest(raw=raw):
                root, guard = self.load(raw)
                self.assertFalse(guard.stopped)
                self.assertIsNone(guard.report()["stop_code"])
                self.assertEqual(self.stored(root), canonical())

        # Valid owner-held states remain held and unacknowledged.
        for hold in ("review_required", "decoder_needed", "repeated_failure_code", INVALID):
            with self.subTest(hold=hold):
                root, guard = self.load(canonical(hold=hold))
                self.assertEqual(guard.report()["stop_code"], hold)
                self.assertTrue(guard.report()["requires_owner_acknowledgement"])
                self.assertEqual(self.stored(root), canonical(hold=hold))
                acquire = Mock()
                self.assertFalse(self.attempt(guard, "advance", B, acquire, acquire))
                acquire.assert_not_called()

        # An active interrupted attempt still stops and keeps its identity.
        active = {"phase": "intake", "identity": A}
        root, guard = self.load(canonical(active=active))
        self.assertEqual(guard.report()["stop_code"], "interrupted_attempt")
        self.assertEqual(json.loads(self.stored(root)),
                         {"schema": STATE_KEY, "hold": "interrupted_attempt", "active": active, "repeats": {}})

        # Repeats-bearing producer-shaped state round-trips unchanged and unheld.
        repeats = {"advance:stage_failed": [A, B], "intake:imap_fetch_failed": [C]}
        root, guard = self.load(canonical(repeats=repeats))
        self.assertFalse(guard.stopped)
        self.assertEqual(guard.state["repeats"], repeats)
        self.assertEqual(self.stored(root), canonical(repeats=repeats))

    def test_malformed_refusal(self):
        self.assertGreaterEqual(len(DUPLICATES), 19)
        for label, raw in DUPLICATES.items():
            with self.subTest(label=label):
                root, guard = self.load(raw)
                self.assert_refused(root, guard)
        # The pre-existing malformed refusal stays on the same code.
        root, guard = self.load("{")
        self.assert_refused(root, guard)

    def test_boundary_replay(self):
        # A duplicate refusal stays stopped after construction and every reload.
        root, guard = self.load(DUPLICATES["hold_review_then_null"])
        self.assert_refused(root, guard)
        for _ in range(2):
            reloaded = RunSafety(root.ledger)
            self.assert_refused(root, reloaded)

        # Canonical repeats at the existing limit keep their identities while held.
        at_limit = {"advance:stage_failed": [A, B, C]}
        root, guard = self.load(canonical(hold="repeated_failure_code", repeats=at_limit))
        for _ in range(2):
            guard = RunSafety(root.ledger)
            self.assertEqual(guard.report()["stop_code"], "repeated_failure_code")
            self.assertEqual(guard.state["repeats"], at_limit)
            self.assertEqual(self.stored(root), canonical(hold="repeated_failure_code", repeats=at_limit))

        # Unheld repeats do not reset on a valid replay; the third identity still stops.
        below = {"advance:stage_failed": [A, B]}
        root, guard = self.load(canonical(repeats=below))
        guard = RunSafety(root.ledger)
        self.assertEqual(guard.state["repeats"], below)
        self.assertTrue(guard.begin("advance", D))
        guard.finish("fault", "stage_failed", (D,))
        self.assertEqual(guard.report()["stop_code"], "repeated_failure_code")
        self.assertEqual(RunSafety(root.ledger).state["repeats"], {"advance:stage_failed": sorted([A, B, D])})

        # Existing thresholds are unchanged: four identities exceed the repeat limit.
        root, guard = self.load(canonical(repeats={"advance:stage_failed": [A, B, C, D]}))
        self.assert_refused(root, guard)
        policy = RunSafetyPolicy()
        self.assertEqual((policy.max_originals, policy.max_fetches, policy.seconds), (200, 200, 2700))

    def test_regression_control(self):
        """Disable only the duplicate gate with a last-member-wins double."""
        def last_member_wins(raw):
            return json.loads(raw)

        duplicate = DUPLICATES["hold_review_then_null"]
        with patch.object(run_safety, "_decode_state", last_member_wins):
            root, guard = self.load(duplicate)
            self.assertFalse(guard.stopped)
            self.assertEqual(self.stored(root), canonical())
            with self.assertRaises(AssertionError):
                self.assert_refused(root, guard)
            # Unique-key positive stays valid under the same double.
            root, guard = self.load(canonical(hold="review_required"))
            self.assertEqual(guard.report()["stop_code"], "review_required")
        # The real decoder refuses the identical input.
        root, guard = self.load(duplicate)
        self.assert_refused(root, guard)

    def test_consumer_contract(self):
        self.assertEqual(RunSafety.__module__, "campaign_tool.records.run_safety")
        self.assertIs(RunSafety, run_safety.RunSafety)

        # Producer: a real fault hold written by RunSafety itself.
        root = self.fresh()
        producer = RunSafety(root.ledger)
        self.assertTrue(producer.begin("intake", A))
        producer.finish("fault", "imap_fetch_failed", (A,))
        written = self.stored(root)
        self.assertEqual(json.loads(written)["hold"], "failure_rate_exceeded")
        self.assertEqual(RunSafety(root.ledger).report()["stop_code"], "failure_rate_exceeded")
        self.write(root, written[:-1] + ", " + HOLD_NULL + "}")
        self.assert_refused(root, RunSafety(root.ledger))

        # Producer: an interrupted active attempt; a trailing active null must not clear it.
        root = self.fresh()
        producer = RunSafety(root.ledger)
        self.assertTrue(producer.begin("advance", B))
        written = self.stored(root)
        self.assertEqual(json.loads(written)["active"], {"phase": "advance", "identity": B})
        self.write(root, written[:-1] + ", " + ACTIVE_NULL + "}")
        self.assert_refused(root, RunSafety(root.ledger))

        # Producer repeats followed by an empty duplicate mapping.
        root = self.fresh()
        producer = RunSafety(root.ledger)
        for number in range(9):
            self.assertTrue(producer.begin("advance", synthetic(100 + number)))
            producer.finish()
        self.assertTrue(producer.begin("advance", C))
        producer.finish("fault", "stage_failed", (C,))
        written = self.stored(root)
        self.assertEqual(json.loads(written)["repeats"], {"advance:stage_failed": [C]})
        reloaded = RunSafety(root.ledger)
        self.assertFalse(reloaded.stopped)
        self.write(root, written[:-1] + ", " + REPEATS_EMPTY + "}")
        self.assert_refused(root, RunSafety(root.ledger))

    def test_preservation(self):
        watched = [REPO / "campaign_tool/records/run_safety.py", REPO / "tests/records/test_run_safety.py"]
        watched += sorted(p for p in (REPO / "tests/fixtures").rglob("*") if p.is_file())
        before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in watched}

        temp = Path(self.temp.name).resolve()
        self.assertTrue(temp.is_relative_to(Path(tempfile.gettempdir()).resolve()))
        run_root = os.environ.get("FA31_RUN_ROOT")
        if run_root is not None:
            self.assertTrue(temp.is_relative_to(Path(run_root).resolve()))

        refuse = Mock(side_effect=AssertionError("network refused in FA31"))
        model = Mock(name="model")
        with patch.object(socket.socket, "connect", refuse), patch.object(socket, "create_connection", refuse), \
                patch.object(socket, "getaddrinfo", refuse):
            for raw in (DUPLICATES["hold_review_then_null"], DUPLICATES["escaped_nested_repeat"]):
                root, guard = self.load(raw)
                self.assert_refused(root, guard)
                self.assertEqual(set(guard.report()), REPORT_KEYS)
                self.assertEqual(set(guard.state), STATE_KEYS)
            root, guard = self.load(canonical())
            self.assertEqual(set(guard.report()), REPORT_KEYS)
        refuse.assert_not_called()
        model.assert_not_called()

        self.assertIn(INVALID, LOCAL_CODES)
        for path in Path(self.temp.name).rglob("*"):
            self.assertTrue(path.resolve().is_relative_to(temp))
        after = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in watched}
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
