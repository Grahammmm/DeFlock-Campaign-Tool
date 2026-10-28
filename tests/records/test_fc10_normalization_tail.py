"""Invented receipt-shaped cases; no records, I/O or global patches at import."""
import contextlib
import copy
import hashlib
import inspect
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "tests" / "fixtures" / "fc10-normalization-tail" / "cases.json"
SUBJECT = hashlib.sha256(b"invented FC10 subject").hexdigest()


@contextlib.contextmanager
def offline():
    """Scoped guards before product imports; not a hostile-code sandbox."""
    with contextlib.ExitStack() as stack:
        for target in ("socket.socket", "socket.create_connection", "socket.getaddrinfo",
                       "subprocess.Popen", "os.system", "os.popen", "sqlite3.connect"):
            stack.enter_context(mock.patch(target, side_effect=RuntimeError("offline_call_forbidden")))
        yield


def cases():
    return json.loads(FIXTURE.read_text())["cases"]


class NormalizationTailTests(unittest.TestCase):
    def setUp(self):
        self.guard = offline()
        self.guard.__enter__()
        self.addCleanup(self.guard.__exit__, None, None, None)
        from campaign_tool.records.understanding import evidence
        self.evidence = evidence

    def check_case(self, case, normalize=None):
        e = self.evidence
        failed = case["failed"]
        invalid = set(failed + case.get("invalid_tail", []))
        receipt = {
            "schema": "format-extraction-v1", "original_sha256": SUBJECT,
            "original_changed": False, "status": "complete", "parser": "synthetic",
            "parser_version": "0", "route": {"format": "pdf"}, "pages": [],
            "units": [None if i in invalid else {
                "kind": "text", "locator": {"page": i}, "text": "synthetic", "data": {}
            } for i in range(1, case["declared"] + 1)],
        }
        before = copy.deepcopy(receipt)
        receipt_hash = e.sha(receipt)
        with mock.patch.object(e, "MAX_UNITS", case["cap"]):
            bundle = (normalize or e.normalize_receipt)(receipt, SUBJECT, receipt_hash)
            ctx = bundle["source_context"][SUBJECT]
            gaps = ctx["normalization_gaps"]
            failures = [g for g in gaps if g["code"] == "unit_normalization_failed"]
            tails = [g for g in gaps if g["code"] == "unit_count_bound"]
            remaining = sum(g["remaining"] for g in tails)
            # Deciding assertion: fails for ONLY the overcount in the old formula.
            self.assertEqual(case["declared"], len(bundle["units"]) + len(failures) + remaining,
                             "declared_normalized_failed_unprocessed_conservation")
            self.assertEqual(failures, [{"code": "unit_normalization_failed", "ordinal": i} for i in failed])
            expected_tail = ([{"code": "unit_count_bound", "first_unprocessed_ordinal": case["cap"] + 1,
                               "remaining": case["tail"]}] if case["tail"] else [])
            self.assertEqual(tails, expected_tail)
            self.assertEqual(gaps, failures + expected_tail)
            self.assertEqual(len(bundle["units"]), case["normalized"])
            self.assertEqual(ctx["declared_unit_count"], case["declared"])
            self.assertEqual(ctx["normalized_unit_count"], case["normalized"])
            self.assertEqual(bundle["source_receipts"], [{
                "original_sha256": SUBJECT, "receipt_sha256": receipt_hash, "attempt_kind": "native",
                "declared_unit_count": case["declared"], "normalized_unit_count": case["normalized"]}])
            ordinals = [i for i in range(1, min(case["declared"], case["cap"]) + 1) if i not in failed]
            self.assertEqual([u["ordinal"] for u in bundle["units"]], ordinals)
            for u, ordinal in zip(bundle["units"], ordinals):
                self.assertEqual(u, e.unit(receipt["units"][ordinal - 1], SUBJECT, receipt_hash, ordinal))
            self.assertIs(e.validate_bundle(bundle), bundle)
            self.assertFalse(ctx["review_acceptance"])
            self.assertFalse(ctx["semantic_acceptance"])
            self.assertEqual(ctx["native_status"], "complete")
            self.assertEqual(ctx["native_receipt_sha256"], receipt_hash)
            self.assertEqual(ctx["pages"], [])
            self.assertEqual(ctx["parser"], "synthetic")
            self.assertEqual(ctx["parser_version"], "0")
            self.assertEqual(ctx["format"], "pdf")
            self.assertEqual(receipt, before)
            self.assertEqual(e.sha(receipt), receipt_hash)
        print(json.dumps({"case": case["id"], "declared": case["declared"], "normalized": len(bundle["units"]),
                          "failed_ordinals": failed, "unprocessed": remaining,
                          "snapshot_sha256": bundle["snapshot_sha256"], "receipt_sha256": receipt_hash}, sort_keys=True))
        return bundle

    def test_cap_is_unchanged(self):
        self.assertEqual(self.evidence.MAX_UNITS, 10000)

    def test_old_formula_negative_control_and_restoration(self):
        e = self.evidence
        original = e.normalize_receipt
        source = textwrap.dedent(inspect.getsource(original))
        fixed = "len(raw_units)-ordinal+1"
        old = "len(raw_units)-len(out)"
        self.assertTrue(fixed in source or old in source)
        namespace = dict(vars(e))
        exec(compile(source.replace(fixed, old), "<synthetic-old-formula>", "exec"), namespace)
        mixed = next(c for c in cases() if c["id"] == "multiple_tail_units")
        import types
        mutant = types.FunctionType(namespace["normalize_receipt"].__code__, vars(e),
                                    argdefs=original.__defaults__)
        with mock.patch.object(e, "normalize_receipt", mutant):
            with self.assertRaisesRegex(AssertionError, "declared_normalized_failed_unprocessed_conservation") as caught:
                self.check_case(mixed)
            print(json.dumps({"control": "old_formula", "deciding_failure": str(caught.exception)}))
        self.assertIs(e.normalize_receipt, original)
        self.check_case(next(c for c in cases() if c["id"] == "above_real_cap_no_failure"))
        self.check_case(mixed)


def _case_test(case_id):
    def test(self):
        self.check_case(next(c for c in cases() if c["id"] == case_id))
    return test


# Literal IDs keep discovery inert: fixture reads occur only during test execution.
CASE_IDS = ("empty", "below_real_cap", "at_real_cap", "above_real_cap_no_failure",
            "failure_first_real_cap", "failure_middle_real_cap", "failure_last_real_cap",
            "several_failures_real_cap", "all_processed_failures", "multiple_tail_units",
            "failures_without_tail", "all_failures_without_tail", "unprocessed_invalid_tail")
for _id in CASE_IDS:
    setattr(NormalizationTailTests, "test_case_" + _id, _case_test(_id))


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--temp-root", type=Path, required=True)
    args = parser.parse_args()
    root = args.temp_root.resolve(strict=True)
    tempfile.tempdir = str(root)
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(REPO))
    unittest.main(argv=[sys.argv[0]], verbosity=2)
