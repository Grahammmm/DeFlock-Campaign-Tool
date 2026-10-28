"""FA70: real synthetic promotion APIs; no corpus or live acceptance.

Fixtures use the unchanged producer-shaped packet in test_ledger_stages.
The negative control copies the complete promote function, removing ONLY the
string predicate; it still uses the real ledger, claims and transaction code.
"""
import contextlib
import hashlib
import inspect
import io
import json
import unittest
from unittest import mock

from campaign_tool.records.ledger import stages
from tests.records import test_ledger_stages as fixtures

INVALID = {
    "list": ["pass"], "object": {"value": "pass"}, "empty_list": [],
    "empty_object": {}, "nested_list": [["pass"]],
    "nested_object": {"value": ["pass"]}, "missing": None, "null": None,
    "true": True, "false": False, "integer": 1, "zero": 0,
    "float": 3.5, "unknown": "unknown", "empty_string": "",
}
VALID = {"pass": "done", "blocked": "blocked", "challenge": "blocked",
         "inapplicable": "inapplicable"}


@contextlib.contextmanager
def synthetic(stage):
    case = fixtures.StageTests(methodName="runTest")
    case.setUp()
    try:
        index = stages.STAGES.index(stage)
        if index:
            case.advance(stages.STAGES[index - 1])
        case.prepare(stage)
        yield case
    finally:
        case.doCleanups()


def raw_packet(case, stage, label):
    packet = case.packet(stage, verdict=INVALID[label])
    if label == "missing":
        packet.pop("verdict")
    return packet, json.dumps(packet).encode()


def rejection_oracle(test, case, stage, raw, claim):
    tables = ("stage_state", "stage_claims", "work_leases", "stage_transitions",
              "stage_validation_authority", "receipts", "stage_events")
    before = {name: case.rows(name) for name in tables}
    attempts = case.rows("stage_attempts")
    with test.assertRaises(stages.StageError) as caught:
        case.runner.promote(case.subject, stage, raw, claim_id=claim)
    test.assertEqual(str(caught.exception), "invalid_verdict")
    after = case.rows("stage_attempts")
    test.assertEqual(after[:-1], attempts)
    test.assertEqual(len(after), len(attempts) + 1)
    attempt = after[-1]
    digest = hashlib.sha256(raw).hexdigest()
    for key, value in {"outcome": "rejected", "reason": "invalid_verdict",
                       "receipt_sha256": digest, "subject_sha256": case.subject,
                       "stage": stage, "run_id": case.run}.items():
        test.assertEqual(attempt[key], value, key)
    artifacts = [r for r in case.rows("stage_artifacts") if r["sha256"] == digest]
    test.assertEqual(len(artifacts), 1)
    test.assertEqual(artifacts[0]["payload"], raw)
    for name in tables:
        test.assertEqual(case.rows(name), before[name], name)


class Regression(unittest.TestCase):
    def test_reason_and_preservation_restrictions_unchanged(self):
        for stage, verdict, reason in (("preserve", "inapplicable", "preservation_required"),
                                        ("extract", "blocked", "reason_required"),
                                        ("extract", "challenge", "reason_required"),
                                        ("extract", "inapplicable", "reason_required")):
            with self.subTest(stage=stage, verdict=verdict), synthetic(stage) as case:
                with self.assertRaises(stages.StageError) as caught:
                    case.submit(stage, case.packet(stage, verdict=verdict))
                self.assertEqual(str(caught.exception), reason)
                self.assertEqual(case.rows("stage_attempts")[-1]["reason"], reason)


def invalid_test(stage, label):
    def check(self):
        with synthetic(stage) as case:
            packet, raw = raw_packet(case, stage, label)
            claim = case.runner.claim(case.subject, stage)["claim_id"]
            rejection_oracle(self, case, stage, raw, claim)
            packet["verdict"] = "pass"
            corrected = json.dumps(packet).encode()
            first = case.runner.promote(case.subject, stage, corrected, claim_id=claim)
            self.assertEqual(first["status"], "done")
            self.assertFalse(first["reused"])
            self.assertTrue(first["test_only"])
            self.assertEqual(first["receipt_sha256"], hashlib.sha256(corrected).hexdigest())
            replay = case.runner.promote(case.subject, stage, corrected)
            self.assertTrue(replay["reused"])
            self.assertTrue(replay["test_only"])
            self.assertEqual(case.rows("stage_attempts")[-1]["outcome"], "replayed")
            self.assertEqual(first["receipt_sha256"], replay["receipt_sha256"])
            authority = case.rows("stage_validation_authority")[-1]
            self.assertEqual(authority["claim_id"], claim)
            self.assertEqual(authority["test_only"], 1)
    return check


def valid_test(stage, verdict, status):
    def check(self):
        with synthetic(stage) as case:
            packet = case.packet(stage, verdict=verdict, reason="synthetic reason")
            raw = json.dumps(packet).encode()
            claim = case.runner.claim(case.subject, stage)["claim_id"]
            first = case.runner.promote(case.subject, stage, raw, claim_id=claim)
            events = case.rows("stage_events")
            transitions = case.rows("stage_transitions")
            authority = case.rows("stage_validation_authority")
            second = case.runner.promote(case.subject, stage, raw)
            for result in (first, second):
                self.assertEqual(result["status"], status)
                self.assertEqual(result["receipt_sha256"], hashlib.sha256(raw).hexdigest())
                self.assertTrue(result["test_only"])
            self.assertFalse(first["reused"])
            self.assertTrue(second["reused"])
            self.assertEqual(case.rows("stage_events"), events)
            self.assertEqual(case.rows("stage_transitions"), transitions)
            self.assertEqual(case.rows("stage_validation_authority"), authority)
            self.assertEqual([r["outcome"] for r in case.rows("stage_attempts")[-2:]],
                             ["accepted", "replayed"])
            self.assertEqual(authority[-1]["test_only"], 1)
    return check


for _stage in stages.STAGES:
    for _label in INVALID:
        setattr(Regression, "test_invalid_" + _stage + "_" + _label,
                invalid_test(_stage, _label))
    for _verdict, _status in VALID.items():
        if (_stage, _verdict) != ("preserve", "inapplicable"):
            setattr(Regression, "test_valid_" + _stage + "_" + _verdict,
                    valid_test(_stage, _verdict, _status))


class GateControl(unittest.TestCase):
    def test_only_removed_type_predicate_breaks_typed_and_durable_oracles(self):
        original = inspect.getsource(stages.promote)
        deciding = '            _require(isinstance(verdict, str), "invalid_verdict")\n'
        self.assertEqual(original.count(deciding), 1)
        namespace = dict(vars(stages))
        exec(compile(original.replace(deciding, "", 1), "<FA70-test-local-promote>", "exec"),
             namespace)
        # Capture both independent oracles, even if the wrong exception escapes.
        class Probe(unittest.TestCase):
            pass
        def probe(label, oracle):
            def check(inner):
                with synthetic("preserve") as case:
                    _, raw = raw_packet(case, "preserve", label)
                    claim = case.runner.claim(case.subject, "preserve")["claim_id"]
                    before = case.rows("stage_attempts")
                    exception = None
                    try:
                        case.runner.promote(case.subject, "preserve", raw, claim_id=claim)
                    except Exception as exc:
                        exception = exc
                    if oracle == "typed":
                        inner.assertIsInstance(exception, stages.StageError)
                        inner.assertEqual(str(exception), "invalid_verdict")
                    else:
                        rows = case.rows("stage_attempts")
                        inner.assertEqual(len(rows), len(before) + 1)
                        inner.assertEqual(rows[-1]["reason"], "invalid_verdict")
                        inner.assertEqual(rows[-1]["receipt_sha256"], hashlib.sha256(raw).hexdigest())
            return check
        for label in ("list", "object"):
            for oracle in ("typed", "durable"):
                setattr(Probe, "test_" + label + "_" + oracle, probe(label, oracle))
        stream = io.StringIO()
        with mock.patch.object(stages, "promote", namespace["promote"]):
            result = unittest.TextTestRunner(stream=stream, verbosity=2).run(
                unittest.defaultTestLoader.loadTestsFromTestCase(Probe))
            for verdict, status in VALID.items():
                with self.subTest(mutated_valid=verdict):
                    valid_test("extract", verdict, status)(self)
        print("FA70 deciding negative control (expected failures):\n" + stream.getvalue())
        self.assertEqual(result.testsRun, 4)
        self.assertEqual(len(result.failures), 4)
        self.assertEqual(result.errors, [])
        self.assertEqual(result.skipped, [])
        # Restoration positives exercise the complete consumer, not a mapping mock.
        class Restored(unittest.TestCase):
            pass
        for label in ("list", "object"):
            setattr(Restored, "test_restored_" + label, invalid_test("preserve", label))
        for verdict, status in VALID.items():
            setattr(Restored, "test_restored_" + verdict, valid_test("extract", verdict, status))
        restored_stream = io.StringIO()
        restored = unittest.TextTestRunner(stream=restored_stream, verbosity=2).run(
            unittest.defaultTestLoader.loadTestsFromTestCase(Restored))
        print("FA70 restored-positive control:\n" + restored_stream.getvalue())
        self.assertEqual(restored.testsRun, 6)
        self.assertTrue(restored.wasSuccessful())
        self.assertEqual(restored.skipped, [])
