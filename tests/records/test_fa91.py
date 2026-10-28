"""FA91: a malformed duplicate queue identity is never reported as fetched.

All inputs are card-owned synthetic rows in the producer shapes documented in
tools/portal_reconcile.py (portal-pointer-observations/v1 and
portal-queue-export/v1). Tests run against the real production entry points
(reconcile, render and the file CLI main). No network, store, mailbox or
private record is touched.

Behavior    the frozen FA91 contract (A1).
GateControl the negative control (A3): an in-memory disposable copy of the
            production module with the pre-FA91 behavior restored (identities
            taken only from fully valid rows, so malformed rows are dropped
            before collision tracking). The mixed-duplicate regressions must
            fail against it while the unrelated valid singleton still passes,
            and everything must pass again once the real module is restored.
"""
import importlib.util
import io
import itertools
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from tools import portal_reconcile

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "tools" / "portal_reconcile.py"

# Module under test. GateControl swaps this for a disposable broken copy and
# always restores it.
MODULE = portal_reconcile

HOST = "records.example.test"
REQUEST = "26-901"
VALID_ITEM = "fa91-item-a"
OTHER_ITEM = "fa91-item-b"
FREE_TEXT = "Server said: fa91 marker <html>"

# Mutations of one valid "received" row that keep host/request_id/item_id
# valid but make the row intrinsically invalid, with the expected fixed code.
INVALID_VARIANTS = (
    ("invalid_state", {"state": "done"}, "unknown_state"),
    ("invalid_has_version", {"has_version": "yes"}, "invalid_has_version"),
    ("received_without_version", {"has_version": False}, "received_without_version"),
    ("nontext_reason", {"reason": 5}, "invalid_reason"),
    ("unknown_field", {"url": "https://records.example.test/documents/fa91-item-a"}, "unknown_field"),
)

# Evidence of the last GateControl run, read by tools/accept_fa91.py.
CONTROL_EVIDENCE = {}


def h(char):
    """Low-entropy synthetic 64-hex placeholder (never a real URL hash)."""
    return char * 64


def obs(url_char, item_id=VALID_ITEM, host_class="nextrequest", **extra):
    row = {"host_class": host_class, "host": HOST, "url_sha256": h(url_char), "request_id": REQUEST,
           "item_id": item_id, "allowlisted": True}
    row.update(extra)
    if row["allowlisted"] is None:
        del row["allowlisted"]
    return row


def q(item_id=VALID_ITEM, state="received", **extra):
    row = {"host": HOST, "request_id": REQUEST, "item_id": item_id, "state": state}
    row.update(extra)
    return row


def invalid(variant, item_id=VALID_ITEM):
    _, change, _ = variant
    row = q(item_id)
    row.update(change)
    return row


def report_for(queue_rows, observation_rows=None):
    if observation_rows is None:
        observation_rows = [obs("1")]
    return MODULE.reconcile(observation_rows, queue_rows)


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from strings(item)


def key_shape(value):
    if isinstance(value, dict):
        return {key: key_shape(item) for key, item in value.items()}
    return type(value).__name__


EMPTY_STATES = {"blocked": 0, "fetching": 0, "pending": 0, "received": 0, "retry": 0}


class Behavior(unittest.TestCase):
    maxDiff = None

    def assert_withheld(self, report, malformed_rows, reasons):
        """One observation of an ambiguous identity: never fetched, never an item."""
        self.assertEqual(report["classes"], {
            "fetched": 0, "queued": 0, "failed": 0, "unproven": 1, "held_policy": 0,
            "held_redirect": 0, "missing_request_identity": 0})
        self.assertEqual(report["detail"]["unproven"], {
            "host_policy_unknown": 0, "no_item_id": 0, "not_in_queue": 0, "queue_identity_ambiguous": 1})
        self.assertEqual(report["detail"]["fetched_queue_items"], 0)
        queue = report["queue"]
        self.assertEqual(queue["items"], 0)
        self.assertEqual(queue["ambiguous_identities"], 1)
        self.assertEqual(queue["malformed_rows"], malformed_rows)
        self.assertEqual(queue["malformed_reasons"], reasons)
        self.assertEqual(queue["by_state"], EMPTY_STATES)
        self.assertEqual(queue["with_version_receipt"], 0)
        self.assertEqual(queue["referenced_by_mail"], 0)
        self.assertEqual(queue["not_referenced_by_mail"], 0)

    # --- A1: valid singleton -----------------------------------------------

    def test_valid_singleton_fetches(self):
        report = report_for([q()])
        self.assertEqual(report["classes"]["fetched"], 1)
        self.assertEqual(sum(report["classes"].values()), 1)
        self.assertEqual(report["detail"]["fetched_queue_items"], 1)
        self.assertEqual(report["queue"]["items"], 1)
        self.assertEqual(report["queue"]["ambiguous_identities"], 0)
        self.assertEqual(report["queue"]["malformed_rows"], 0)
        self.assertEqual(report["queue"]["malformed_reasons"], {})
        self.assertEqual(report["queue"]["with_version_receipt"], 1)
        self.assertEqual(report["queue"]["referenced_by_mail"], 1)

    # --- A1: valid plus invalid, both orders ---------------------------------

    def test_mixed_valid_and_invalid_duplicate_is_ambiguous_in_both_orders(self):
        for variant in INVALID_VARIANTS:
            for order in ("valid_first", "invalid_first"):
                rows = [q(), invalid(variant)]
                if order == "invalid_first":
                    rows.reverse()
                with self.subTest(variant=variant[0], order=order):
                    report = report_for(rows)
                    self.assert_withheld(report, 1, {variant[2]: 1, "duplicate_identity": 1})
                    self.assertEqual(report["queue"]["rows"], 2)

    def test_mixed_duplicate_with_receipt_flag_and_blocked_state(self):
        # has_version=True on the valid row and a blocked valid row: neither may
        # surface as fetched/failed once an invalid twin shares the identity.
        for valid_row in (q(has_version=True), q(state="blocked", reason="timeout"), q(state="pending")):
            for variant in INVALID_VARIANTS:
                with self.subTest(state=valid_row["state"], variant=variant[0]):
                    report = report_for([invalid(variant), valid_row])
                    self.assert_withheld(report, 1, {variant[2]: 1, "duplicate_identity": 1})
                    self.assertEqual(report["detail"]["failed_reasons"], {})
                    self.assertEqual(report["queue"]["failed_reasons"], {})

    # --- A1: exact totals for two and three rows ------------------------------

    def test_two_valid_duplicates_keep_one_surplus_malformed_row(self):
        report = report_for([q(), q(state="pending")])
        self.assert_withheld(report, 1, {"duplicate_identity": 1})

    def test_two_invalid_duplicates_count_each_row_once(self):
        a, b = INVALID_VARIANTS[0], INVALID_VARIANTS[2]
        report = report_for([invalid(a), invalid(b)])
        self.assert_withheld(report, 2, {a[2]: 1, b[2]: 1, "duplicate_identity": 1})

    def test_three_row_groups_exact_distinct_row_totals(self):
        bad1, bad2 = INVALID_VARIANTS[1], INVALID_VARIANTS[4]
        valid1, valid2, valid3 = q(), q(state="pending"), q(has_version=True)
        cases = (
            # rows, malformed_rows, reasons
            ((valid1, valid2, valid3), 2, {"duplicate_identity": 2}),
            ((valid1, valid2, invalid(bad1)), 2, {bad1[2]: 1, "duplicate_identity": 2}),
            ((valid1, invalid(bad1), invalid(bad2)), 2, {bad1[2]: 1, bad2[2]: 1, "duplicate_identity": 2}),
            ((invalid(bad1), invalid(bad1), invalid(bad2)), 3, {bad1[2]: 2, bad2[2]: 1, "duplicate_identity": 2}),
        )
        for rows, malformed_rows, reasons in cases:
            for perm in itertools.permutations(range(3)):
                with self.subTest(rows=[sorted(r) for r in rows], perm=perm):
                    report = report_for([rows[i] for i in perm])
                    self.assert_withheld(report, malformed_rows, reasons)
                    self.assertEqual(report["queue"]["rows"], 3)
                    # duplicate_identity is an overlap diagnostic: the reason
                    # counters no longer sum to malformed_rows when a row is
                    # both intrinsically invalid and part of a duplicate group.
                    self.assertGreaterEqual(sum(reasons.values()), malformed_rows)

    # --- A1: no cross-identity taint, no salvage -----------------------------

    def test_malformed_row_cannot_taint_a_different_valid_singleton(self):
        for variant in INVALID_VARIANTS:
            with self.subTest(variant=variant[0]):
                report = report_for([q(), invalid(variant, item_id=OTHER_ITEM)],
                                    [obs("1"), obs("2", item_id=OTHER_ITEM)])
                self.assertEqual(report["classes"]["fetched"], 1)
                self.assertEqual(report["classes"]["unproven"], 1)
                self.assertEqual(report["detail"]["unproven"]["not_in_queue"], 1)
                self.assertEqual(report["detail"]["unproven"]["queue_identity_ambiguous"], 0)
                self.assertEqual(report["queue"]["items"], 1)
                self.assertEqual(report["queue"]["ambiguous_identities"], 0)
                self.assertEqual(report["queue"]["malformed_rows"], 1)
                self.assertEqual(report["queue"]["malformed_reasons"], {variant[2]: 1})

    def test_invalid_identity_fields_are_never_salvaged_or_grouped(self):
        # Each row resembles the valid identity but has an invalid or missing
        # identity field; none may be inferred into the valid row's group.
        lookalikes = [
            {"host": HOST.upper(), "request_id": REQUEST, "item_id": VALID_ITEM, "state": "received"},
            {"host": HOST, "request_id": REQUEST + " ", "item_id": VALID_ITEM, "state": "received"},
            {"host": HOST, "item_id": VALID_ITEM, "state": "received"},
            {"host": HOST, "request_id": REQUEST, "item_id": 7, "state": "received"},
            {"host": HOST, "request_id": [REQUEST], "item_id": VALID_ITEM, "state": "received"},
            "records.example.test 26-901 fa91-item-a",
            None,
            [HOST, REQUEST, VALID_ITEM],
        ]
        for order in ("valid_first", "valid_last"):
            rows = [q(), *lookalikes] if order == "valid_first" else [*lookalikes, q()]
            with self.subTest(order=order):
                report = report_for(rows)
                self.assertEqual(report["classes"]["fetched"], 1)
                self.assertEqual(report["queue"]["items"], 1)
                self.assertEqual(report["queue"]["ambiguous_identities"], 0)
                self.assertEqual(report["queue"]["malformed_rows"], len(lookalikes))
                self.assertEqual(report["queue"]["malformed_reasons"], {
                    "invalid_identity": 5, "not_object": 3})

    def test_unidentifiable_invalid_rows_counted_once_outside_groups(self):
        rows = ["x", 4, None, {"state": "received"}, {"host": HOST, "item_id": VALID_ITEM, "state": "pending"}]
        report = report_for(rows)
        self.assertEqual(report["queue"]["malformed_rows"], 5)
        self.assertEqual(report["queue"]["malformed_reasons"], {"invalid_identity": 2, "not_object": 3})
        self.assertEqual(report["queue"]["ambiguous_identities"], 0)
        self.assertEqual(report["queue"]["items"], 0)
        self.assertEqual(report["detail"]["unproven"]["not_in_queue"], 1)

    def test_invalid_singleton_is_not_ambiguous(self):
        for variant in INVALID_VARIANTS:
            with self.subTest(variant=variant[0]):
                report = report_for([invalid(variant)])
                self.assertEqual(report["queue"]["malformed_rows"], 1)
                self.assertEqual(report["queue"]["malformed_reasons"], {variant[2]: 1})
                self.assertEqual(report["queue"]["ambiguous_identities"], 0)
                self.assertEqual(report["queue"]["items"], 0)
                self.assertEqual(report["detail"]["unproven"]["not_in_queue"], 1)
                self.assertEqual(report["classes"]["fetched"], 0)

    # --- A1: held classes keep precedence ------------------------------------

    def test_held_classes_keep_precedence_over_ambiguous_identity(self):
        cases = (
            (obs("3", host_class="govqa"), "held_policy"),
            (obs("4", host_class="microsoft_delivery"), "held_policy"),
            (obs("5", host_class="other_delivery"), "held_policy"),
            (obs("6", host_class="tracked_redirect"), "held_redirect"),
            (obs("7", allowlisted=False), "held_policy"),
            (obs("8", allowlisted=None), "unproven"),
        )
        for queue in ([q(), invalid(INVALID_VARIANTS[2])], [q()]):
            for row, expected in cases:
                with self.subTest(queue_rows=len(queue), host_class=row["host_class"],
                                  allowlisted=row.get("allowlisted")):
                    report = report_for(queue, [row])
                    hits = [name for name, count in report["classes"].items() if count]
                    self.assertEqual(hits, [expected])
                    self.assertEqual(report["detail"]["unproven"]["queue_identity_ambiguous"], 0)
                    if expected == "unproven":
                        self.assertEqual(report["detail"]["unproven"]["host_policy_unknown"], 1)
                    self.assertEqual(report["queue"]["referenced_by_mail"], 0)

    # --- A1: rendering, empty input, order independence -----------------------

    def test_rendered_output_has_fixed_keys_and_no_input_text(self):
        rows = [q(), invalid(INVALID_VARIANTS[4]), q(item_id=OTHER_ITEM, state="blocked", reason=FREE_TEXT),
                invalid(INVALID_VARIANTS[3], item_id=OTHER_ITEM)]
        text = MODULE.render(report_for(rows, [obs("1"), obs("2", item_id=OTHER_ITEM)]))
        rendered = json.loads(text)
        empty = MODULE.reconcile([], [])
        expected_shape = key_shape(empty)
        actual_shape = key_shape(rendered)
        # Only the counter maps that are open by design may gain keys, and only
        # from the fixed vocabularies.
        for section, key in (("mail", "malformed_reasons"), ("queue", "malformed_reasons"),
                             ("queue", "failed_reasons"), ("detail", "failed_reasons")):
            self.assertTrue(set(rendered[section][key]) <= MODULE.MALFORMED_CODES | {
                "duplicate_identity", "invalid_value"} | MODULE.KNOWN_REASONS | {
                MODULE.UNRECOGNIZED_REASON, MODULE.NO_REASON}, (section, key))
            actual_shape[section][key] = {}
            expected_shape[section][key] = {}
        self.assertEqual(actual_shape, expected_shape)
        self.assertEqual(rendered["queue"]["malformed_reasons"], {
            "duplicate_identity": 2, "invalid_reason": 1, "unknown_field": 1})
        self.assertEqual(rendered["queue"]["malformed_rows"], 2)
        for secret in (HOST, REQUEST, VALID_ITEM, OTHER_ITEM, h("1"), h("2"), FREE_TEXT, "fa91", "https"):
            self.assertNotIn(secret, text)
        self.assertTrue(all(isinstance(s, str) and s.isascii() for s in strings(rendered)))

    def test_empty_input_report_is_exact(self):
        report = MODULE.reconcile([], [])
        self.assertEqual(report["queue"], {
            "rows": 0, "malformed_rows": 0, "malformed_reasons": {}, "items": 0, "ambiguous_identities": 0,
            "by_state": EMPTY_STATES, "with_version_receipt": 0, "failed_reasons": {},
            "referenced_by_mail": 0, "not_referenced_by_mail": 0})
        self.assertEqual(sum(report["classes"].values()), 0)
        self.assertEqual(report["mail"]["malformed_rows"], 0)

    def test_permutation_and_repeated_run_equivalence(self):
        rows = [q(), invalid(INVALID_VARIANTS[0]), q(item_id=OTHER_ITEM), q(item_id=OTHER_ITEM, state="pending"),
                invalid(INVALID_VARIANTS[2], item_id="fa91-item-c"), "x"]
        observations = [obs("1"), obs("2", item_id=OTHER_ITEM), obs("3", item_id="fa91-item-c")]
        frozen = json.dumps(rows, sort_keys=True)
        outputs = set()
        for perm in itertools.permutations(rows):
            outputs.add(MODULE.render(MODULE.reconcile(observations, list(perm))))
            outputs.add(MODULE.render(MODULE.reconcile(list(reversed(observations)), list(perm))))
        self.assertEqual(len(outputs), 1)
        self.assertEqual(json.dumps(rows, sort_keys=True), frozen)  # inputs not mutated
        report = json.loads(outputs.pop())
        self.assertEqual(report["classes"]["fetched"], 0)
        self.assertEqual(report["detail"]["unproven"]["queue_identity_ambiguous"], 2)
        self.assertEqual(report["detail"]["unproven"]["not_in_queue"], 1)
        self.assertEqual(report["queue"]["ambiguous_identities"], 2)
        self.assertEqual(report["queue"]["items"], 0)
        self.assertEqual(report["queue"]["malformed_rows"], 4)
        self.assertEqual(report["queue"]["malformed_reasons"], {
            "duplicate_identity": 2, "not_object": 1, "received_without_version": 1, "unknown_state": 1})

    def test_file_cli_reports_mixed_duplicate_as_unproven(self):
        with tempfile.TemporaryDirectory(prefix="fa91-cli-") as tmp:
            base = Path(tmp)
            observations = base / "observations.json"
            queue = base / "queue-export.json"
            output = base / "report.json"
            observations.write_text(json.dumps({"schema": MODULE.OBSERVATION_SCHEMA, "observations": [obs("1")]}))
            queue.write_text(json.dumps({"schema": MODULE.QUEUE_SCHEMA,
                                         "items": [q(), invalid(INVALID_VARIANTS[2])]}))
            before = (observations.read_bytes(), queue.read_bytes())
            stdout, stderr = io.StringIO(), io.StringIO()
            saved = sys.stdout, sys.stderr
            sys.stdout, sys.stderr = stdout, stderr
            try:
                code = MODULE.main(["--observations", str(observations), "--queue-export", str(queue),
                                    "--output", str(output)])
            finally:
                sys.stdout, sys.stderr = saved
            self.assertEqual(code, 0)
            self.assertEqual((stdout.getvalue(), stderr.getvalue()), ("", ""))
            self.assertEqual((observations.read_bytes(), queue.read_bytes()), before)
            self.assertEqual(os.stat(output).st_mode & 0o777, 0o600)
            text = output.read_text()
            self.assert_withheld(json.loads(text), 1, {"duplicate_identity": 1, "received_without_version": 1})
            self.assertNotIn(HOST, text)
            self.assertNotIn(VALID_ITEM, text)


# Behavior tests that must go red when malformed rows are dropped before
# collision tracking, and the unrelated positives that must stay green.
CONTROL_RED = (
    "test_mixed_valid_and_invalid_duplicate_is_ambiguous_in_both_orders",
    "test_mixed_duplicate_with_receipt_flag_and_blocked_state",
    "test_two_invalid_duplicates_count_each_row_once",
    "test_three_row_groups_exact_distinct_row_totals",
    "test_rendered_output_has_fixed_keys_and_no_input_text",
    "test_permutation_and_repeated_run_equivalence",
    "test_file_cli_reports_mixed_duplicate_as_unproven",
)
CONTROL_GREEN = (
    "test_valid_singleton_fetches",
    "test_two_valid_duplicates_keep_one_surplus_malformed_row",
    "test_malformed_row_cannot_taint_a_different_valid_singleton",
    "test_invalid_identity_fields_are_never_salvaged_or_grouped",
    "test_unidentifiable_invalid_rows_counted_once_outside_groups",
    "test_invalid_singleton_is_not_ambiguous",
    "test_held_classes_keep_precedence_over_ambiguous_identity",
    "test_empty_input_report_is_exact",
)


def disposable_broken_copy():
    """Load a fresh in-memory copy of the production module, then restore the
    pre-FA91 defect in that copy only: a queue identity is tracked only for a
    fully valid row, so malformed rows are dropped before collision tracking.
    """
    spec = importlib.util.spec_from_file_location("_fa91_disposable_portal_reconcile", SOURCE)
    copy = importlib.util.module_from_spec(spec)
    saved_path = list(sys.path)
    try:
        spec.loader.exec_module(copy)
    finally:
        sys.path[:] = saved_path
    if not hasattr(copy, "_queue_identity"):
        raise AssertionError("production module has no _queue_identity helper to mutate")

    def identity_of_valid_rows_only(row):
        normalized, _ = copy._normalized(copy.normalize_queue_row, row)
        return None if normalized is None else normalized[0]

    copy._queue_identity = identity_of_valid_rows_only
    return copy


def run_behavior(names):
    stream = io.StringIO()
    suite = unittest.TestSuite(Behavior(name) for name in names)
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    bad = sorted({getattr(test, "test_case", test)._testMethodName for test, _ in result.failures + result.errors})
    return {"tests_run": result.testsRun, "failed": bad, "errors": len(result.errors),
            "skipped": len(result.skipped), "output": stream.getvalue()}


class GateControl(unittest.TestCase):
    maxDiff = None

    def test_restored_defect_fails_mixed_regression_and_restored_module_passes(self):
        global MODULE
        production = portal_reconcile
        production_helper = production._queue_identity
        names = CONTROL_RED + CONTROL_GREEN
        broken_copy = disposable_broken_copy()
        self.assertIsNot(broken_copy, production)
        MODULE = broken_copy
        try:
            broken = run_behavior(names)
        finally:
            MODULE = production
        restored = run_behavior(names)
        CONTROL_EVIDENCE.clear()
        CONTROL_EVIDENCE.update({"broken": broken, "restored": restored,
                                 "expected_red": list(CONTROL_RED), "required_green": list(CONTROL_GREEN)})
        # The defect is detected: exactly the mixed-duplicate regressions fail,
        # as assertion failures (not errors), and the positives stay green.
        self.assertEqual(broken["tests_run"], len(names))
        self.assertEqual(broken["errors"], 0, broken["output"])
        self.assertEqual(broken["failed"], sorted(CONTROL_RED), broken["output"])
        # Restored positive: every selected test passes with the real module.
        self.assertEqual(restored["tests_run"], len(names))
        self.assertEqual((restored["failed"], restored["errors"], restored["skipped"]), ([], 0, 0),
                         restored["output"])
        # The production module object and its helper were never mutated.
        self.assertIs(MODULE, production)
        self.assertIs(production._queue_identity, production_helper)
        self.assertIs(sys.modules["tools.portal_reconcile"], production)

    def test_control_lists_cover_every_behavior_test(self):
        declared = sorted(name for name in dir(Behavior) if name.startswith("test_"))
        self.assertEqual(sorted(CONTROL_RED + CONTROL_GREEN), declared)
        self.assertFalse(set(CONTROL_RED) & set(CONTROL_GREEN))


if __name__ == "__main__":
    unittest.main()
