"""FC13 attempt-label binding between derivative units and their source receipt.

Every input here is fictional and in memory: synthetic original/receipt
identities are hashes of invented tokens, unit text is invented, and no OCR,
receipt file, network endpoint or durable store is touched.

Run modes:
  python3 -m unittest tests.records.test_fc13_attempt_binding
  python3 /abs/path/tests/records/test_fc13_attempt_binding.py --fc13-tmp-root DIR
The script-mode temporary-root argument is processed before any product import.
"""
import copy
import hashlib
import inspect
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "tests" / "fixtures" / "fc13-attempt-binding" / "cases.json"
TMP_FLAG = "--fc13-tmp-root"

if __name__ == "__main__":
    # Script mode only: bind temporary storage and the module-derived repository
    # path before anything from the product is imported (see setUpModule).
    if TMP_FLAG in sys.argv:
        _at = sys.argv.index(TMP_FLAG)
        _root = Path(sys.argv[_at + 1]).resolve()
        if not _root.is_dir():
            raise SystemExit("fc13_tmp_root_missing")
        tempfile.tempdir = str(_root)
        del sys.argv[_at:_at + 2]
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))

CASES = json.loads(FIXTURE.read_text(encoding="utf-8"))
ALT = CASES["alternate_label"]
DEFAULT_TWIN = "native"

evidence = None
_PATCHES = []
_SENTINEL_CALLS = []


def _offline(*args, **kwargs):
    _SENTINEL_CALLS.append("network")
    raise OSError("fc13_offline_sentinel")


def _no_receipt_read(*args, **kwargs):
    _SENTINEL_CALLS.append("read_pinned")
    raise AssertionError("fc13_receipt_read_forbidden")


def setUpModule():
    """Install scoped offline sentinels, then import the product."""
    global evidence
    import socket
    for target, name in ((socket.socket, "connect"), (socket.socket, "connect_ex"),
                         (socket, "create_connection"), (socket, "getaddrinfo")):
        p = mock.patch.object(target, name, _offline)
        p.start()
        _PATCHES.append(p)
    from campaign_tool.records.understanding import evidence as product
    evidence = product
    p = mock.patch.object(product, "read_pinned", _no_receipt_read)
    p.start()
    _PATCHES.append(p)


def tearDownModule():
    while _PATCHES:
        _PATCHES.pop().stop()


# Independent canonical hashing (not the product's helpers).
def canon(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def h(value):
    return hashlib.sha256(value if isinstance(value, bytes) else canon(value)).hexdigest()


def token_hash(kind, token):
    return h(("fc13-synthetic-" + kind + ":" + token).encode("utf-8"))


def raw_unit(token, ordinal):
    return {"kind": "paragraph", "locator": {"page": ordinal},
            "text": "Fictional unit " + str(ordinal) + " of " + token + ".", "data": {}}


def valid_label(spec):
    v = spec.get("value") if not spec.get("absent") else None
    return type(v) is str and v.strip() != ""


def make_unit(subject, receipt, token, ordinal, spec):
    """Product-built unit whose import/unit IDs hash the supplied label."""
    if spec.get("absent"):
        u = evidence.unit(raw_unit(token, ordinal), subject, receipt, ordinal, "native")
        del u["attempt_kind"]
        return u
    return evidence.unit(raw_unit(token, ordinal), subject, receipt, ordinal, spec["value"])


def assemble(pairs):
    """Build a bundle with valid counts, ordinals, payload hashes and snapshot."""
    units, receipts, context = [], [], {}
    for pair in pairs:
        subject = token_hash("original", pair["original"])
        receipt = token_hash("receipt", pair["receipt"])
        context[subject] = {"fictional": True, "token": pair["original"]}
        row = {"original_sha256": subject, "receipt_sha256": receipt,
               "declared_unit_count": len(pair["unit_labels"]),
               "normalized_unit_count": len(pair["unit_labels"])}
        if not pair["receipt_label"].get("absent"):
            row["attempt_kind"] = pair["receipt_label"]["value"]
        receipts.append(row)
        for ordinal, spec in enumerate(pair["unit_labels"], 1):
            units.append(make_unit(subject, receipt, pair["receipt"], ordinal, spec))
    units.sort(key=lambda u: (u["original_sha256"], u["receipt_sha256"], u["ordinal"]))
    receipts.sort(key=lambda r: (r["original_sha256"], r["receipt_sha256"]))
    body = {"schema": "records-understanding-input-v1", "units": units,
            "source_context": context, "source_receipts": receipts}
    return {**body, "snapshot_sha256": h(body)}


def twin_pairs(pairs):
    """Same bundle shape with every label repaired to its receipt's valid label."""
    out = []
    for pair in pairs:
        label = pair["receipt_label"]["value"] if valid_label(pair["receipt_label"]) else DEFAULT_TWIN
        spec = {"value": label}
        out.append({**pair, "receipt_label": spec, "unit_labels": [spec for _ in pair["unit_labels"]]})
    return out


def generated_cases():
    out = []
    for kind in CASES["required_invalid_kinds"]:
        bad = CASES["invalid_labels"][kind]
        out.append({"id": "invalid_unit_" + kind, "dimension": "invalid_unit", "expect": "unit_attempt_kind_invalid",
                    "pairs": [{"original": "doc-a", "receipt": "rcpt-a1", "receipt_label": {"value": "native"},
                               "unit_labels": [{"value": "native"}, bad]}]})
        out.append({"id": "invalid_receipt_" + kind, "dimension": "invalid_receipt", "expect": "receipt_attempt_kind_invalid",
                    "pairs": [{"original": "doc-a", "receipt": "rcpt-a1", "receipt_label": bad,
                               "unit_labels": [{"value": "native"}, {"value": "native"}]}]})
    return out


ALL_CASES = list(CASES["cases"]) + generated_cases()
MISMATCH_IDS = [c["id"] for c in ALL_CASES if c["expect"] == "receipt_attempt_kind_mismatch"]
POSITIVE_CONTROL_IDS = ("native_match", "alternate_match")


def case_by_id(case_id):
    return next(c for c in ALL_CASES if c["id"] == case_id)


class Checks:
    def assert_independent_identity(self, value):
        for u in value["units"]:
            label = u.get("attempt_kind")
            if "attempt_kind" not in u:
                continue
            imp = h(["receipt-bound-derivative-v1", u["original_sha256"], u["receipt_sha256"], label])
            raw = {k: u[k] for k in ("kind", "locator", "text", "data")}
            self.assertEqual(u["import_id"], imp)
            self.assertEqual(u["artifact_sha256"], h(canon(raw)))
            self.assertEqual(u["unit_id"], h([imp, u["ordinal"], u["artifact_sha256"]]))
        body = {k: v for k, v in value.items() if k != "snapshot_sha256"}
        self.assertEqual(value["snapshot_sha256"], h(body))

    def assert_accepted(self, validate, value):
        before = copy.deepcopy(value)
        result = validate(value)
        self.assertIs(result, value)
        self.assertEqual(value, before)
        self.assertEqual(h(value), h(before))

    def assert_refused(self, validate, value, code):
        before = copy.deepcopy(value)
        with self.assertRaises(ValueError) as caught:
            validate(value)
        self.assertEqual(caught.exception.args, (code,))
        self.assertEqual(value, before)


class AttemptBindingCases(Checks, unittest.TestCase):
    """One generated test per frozen fixture case (see _install_cases)."""

    def run_case(self, case):
        value = assemble(case["pairs"])
        self.assert_independent_identity(value)
        if case["expect"] == "accept":
            self.assert_accepted(evidence.validate_bundle, value)
            again = assemble(case["pairs"])
            self.assertEqual(again["snapshot_sha256"], value["snapshot_sha256"])
            for u in value["units"]:
                r = next(r for r in value["source_receipts"]
                         if (r["original_sha256"], r["receipt_sha256"]) == (u["original_sha256"], u["receipt_sha256"]))
                self.assertEqual(u["attempt_kind"], r["attempt_kind"])
        else:
            twin = assemble(twin_pairs(case["pairs"]))
            self.assert_accepted(evidence.validate_bundle, twin)
            for r in value["source_receipts"]:
                bound = [u["ordinal"] for u in value["units"]
                         if (u["original_sha256"], u["receipt_sha256"]) == (r["original_sha256"], r["receipt_sha256"])]
                self.assertEqual(bound, list(range(1, len(bound) + 1)))
                self.assertEqual(r["declared_unit_count"], len(bound))
                self.assertEqual(r["normalized_unit_count"], len(bound))
            self.assert_refused(evidence.validate_bundle, value, case["expect"])
        self.assertEqual(_SENTINEL_CALLS, [])


def _install_cases():
    for case in ALL_CASES:
        def test(self, case=case):
            self.run_case(case)
        test.__name__ = "test_case_" + case["id"]
        test.__doc__ = None
        setattr(AttemptBindingCases, test.__name__, test)


_install_cases()


class AttemptBindingContract(Checks, unittest.TestCase):
    def test_fixture_matrix_coverage(self):
        ids = [c["id"] for c in ALL_CASES]
        self.assertEqual(len(ids), len(set(ids)))
        sides = {c["dimension"] for c in ALL_CASES}
        for need in ("positive", "positive_two_pairs", "mismatch", "mismatch_two_pairs",
                     "invalid_unit", "invalid_receipt", "invalid_both"):
            self.assertIn(need, sides)
        for side in CASES["required_sides"]:
            for kind in ("absent", "null", "true", "false", "list", "object", "empty", "whitespace"):
                self.assertIn("invalid_" + side + "_" + kind, ids)
        self.assertIn("native_match", ids)
        self.assertIn("alternate_match", ids)
        self.assertNotEqual(ALT, "native")
        alt_case = case_by_id("alternate_match")
        self.assertEqual(alt_case["pairs"][0]["receipt_label"]["value"], ALT)
        two = case_by_id("two_pairs_same_original")["pairs"]
        self.assertNotEqual(two[0]["receipt_label"], two[1]["receipt_label"])
        self.assertGreaterEqual(len(MISMATCH_IDS), 5)

    def test_normalize_receipt_emits_native_on_both_sides(self):
        subject = token_hash("original", "doc-n")
        receipt_sha = token_hash("receipt", "rcpt-n1")
        receipt = {"schema": "format-extraction-v1", "original_sha256": subject, "original_changed": False,
                   "status": "ok", "route": {"format": "txt"}, "parser": "fictional", "parser_version": "0",
                   "pages": [{"page": 1}, {"page": 2}],
                   "units": [raw_unit("rcpt-n1", 1), raw_unit("rcpt-n1", 2)]}
        before = copy.deepcopy(receipt)
        first = evidence.normalize_receipt(receipt, subject, receipt_sha)
        second = evidence.normalize_receipt(copy.deepcopy(receipt), subject, receipt_sha)
        self.assertEqual(receipt, before)
        self.assertEqual(first, second)
        self.assertEqual(first["snapshot_sha256"], second["snapshot_sha256"])
        self.assertEqual([r["attempt_kind"] for r in first["source_receipts"]], ["native"])
        self.assertEqual([u["attempt_kind"] for u in first["units"]], ["native", "native"])
        self.assert_independent_identity(first)
        self.assert_accepted(evidence.validate_bundle, first)
        self.assertEqual(_SENTINEL_CALLS, [])

    def test_accepted_hashes_are_deterministic(self):
        for case_id in ("native_match", "alternate_match", "two_pairs_same_original", "two_pairs_two_originals"):
            a = assemble(case_by_id(case_id)["pairs"])
            b = assemble(copy.deepcopy(case_by_id(case_id)["pairs"]))
            self.assertEqual(a, b)
            self.assert_accepted(evidence.validate_bundle, a)
            self.assertEqual(h(a), h(b))
        native = assemble(case_by_id("native_match")["pairs"])
        alt = assemble(case_by_id("alternate_match")["pairs"])
        self.assertNotEqual(native["units"][0]["import_id"], alt["units"][0]["import_id"])
        self.assertNotEqual(native["snapshot_sha256"], alt["snapshot_sha256"])


class AttemptEqualityNegativeControl(Checks, unittest.TestCase):
    GUARD = "receipt_attempt_kind_mismatch"

    def mutant(self):
        """Source-derived validate_bundle with only the equality guard disabled."""
        original = evidence.validate_bundle
        src = inspect.getsource(original)
        lines = src.splitlines(keepends=True)
        hits = [i for i, line in enumerate(lines) if self.GUARD in line]
        self.assertEqual(len(hits), 1, "equality guard must be one isolated line")
        line = lines[hits[0]]
        self.assertTrue(line.lstrip().startswith("need("))
        self.assertEqual(line.count("need("), 1)
        indent = line[:len(line) - len(line.lstrip())]
        mutated = lines[:hits[0]] + [indent + "pass\n"] + lines[hits[0] + 1:]
        changed = [i for i, (a, b) in enumerate(zip(lines, mutated)) if a != b]
        self.assertEqual(changed, hits)
        namespace = dict(vars(evidence))
        exec(compile("".join(mutated), "<fc13-mutant-validate_bundle>", "exec"), namespace)
        self.assertIs(evidence.validate_bundle, original)
        return namespace["validate_bundle"], original

    def test_disabled_equality_guard_is_killed_then_restored(self):
        mutant, original = self.mutant()
        for case_id in MISMATCH_IDS:
            value = assemble(case_by_id(case_id)["pairs"])
            self.assertIs(mutant(value), value)
            with self.assertRaises(AssertionError) as killed:
                self.assert_refused(mutant, value, self.GUARD)
            self.assertIn("ValueError not raised", str(killed.exception))
        for case_id in POSITIVE_CONTROL_IDS:
            self.assert_accepted(mutant, assemble(case_by_id(case_id)["pairs"]))
        for case in ALL_CASES:
            if case["expect"] in ("unit_attempt_kind_invalid", "receipt_attempt_kind_invalid"):
                self.assert_refused(mutant, assemble(case["pairs"]), case["expect"])
        # Restore: the product function was never replaced; rerun genuine controls.
        self.assertIs(evidence.validate_bundle, original)
        for case_id in MISMATCH_IDS:
            self.assert_refused(evidence.validate_bundle, assemble(case_by_id(case_id)["pairs"]), self.GUARD)
        for case_id in POSITIVE_CONTROL_IDS:
            self.assert_accepted(evidence.validate_bundle, assemble(case_by_id(case_id)["pairs"]))
        self.assertEqual(_SENTINEL_CALLS, [])


if __name__ == "__main__":
    unittest.main()
