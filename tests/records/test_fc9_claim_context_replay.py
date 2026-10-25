"""FC9-CLAIM-CONTEXT-REPLAY: saved cited claims bind their analysis context.

A claim made against bundle A is serialized to JSON and restored. Bundle B is
rebuilt from identical receipt-bound units with a changed analysis context and
therefore a fresh, valid snapshot hash. A's citations still validate against
B, but the saved claim must be refused with evidence_claim_stale_or_changed; a
fresh claim on B passes. A changed receipt identity is refused earlier with
evidence_unit_not_current until supports are recited. Restoring byte-identical
A makes the original claim pass again: this API is content addressed, so no
instance or ABA restriction is asserted.

All fixture values are fictional (tests/fixtures/fc9-claim-context-replay).
Results are structural only: NOT_ASSESSED and review_acceptance false.

Script use: python3 <this file> [--tmp-root DIR] [--case-report FILE] [unittest args]
Both options are handled before any product import.
"""
import copy
import hashlib
import json
from pathlib import Path
import sys
import unittest

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "tests" / "fixtures" / "fc9-claim-context-replay" / "cases.json"


def _script_options(argv):
    """Strip --tmp-root/--case-report from argv; only used under __main__."""
    rest, opts, i = [argv[0]], {}, 1
    while i < len(argv):
        if argv[i] in ("--tmp-root", "--case-report") and i + 1 < len(argv):
            opts[argv[i]] = argv[i + 1]
            i += 2
        else:
            rest.append(argv[i])
            i += 1
    return rest, opts


_OPTS = {}
if __name__ == "__main__":
    import tempfile
    sys.argv, _OPTS = _script_options(sys.argv)
    if "--tmp-root" in _OPTS:
        _tmp = Path(_OPTS["--tmp-root"]).absolute()
        _tmp.mkdir(parents=True, exist_ok=False)
        tempfile.tempdir = str(_tmp)
    sys.dont_write_bytecode = True
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))

from campaign_tool.records import grounded_evidence  # noqa: E402
from campaign_tool.records.understanding import evidence  # noqa: E402

CASES = json.loads(FIXTURE.read_text(encoding="utf-8"))
EXPECTED = CASES["expected"]
CONTEXT_REFUSAL = EXPECTED["context_refusal"]
RECEIPT_REFUSAL = EXPECTED["receipt_refusal"]
CORRUPT_REFUSAL = EXPECTED["corrupt_bundle_refusal"]
SUBJECT = hashlib.sha256(CASES["original_label"].encode("utf-8")).hexdigest()
RECEIPT_A = hashlib.sha256(CASES["receipt_label"].encode("utf-8")).hexdigest()
RECEIPT_B = hashlib.sha256(CASES["changed_receipt_label"].encode("utf-8")).hexdigest()
BUILDERS = tuple(CASES["builders"])
CLAIMS = tuple(CASES["claims"])
CHANGED = tuple(CASES["changed_contexts"])

# Case ids exercised in this process; read by the acceptance runner and --case-report.
CASES_EXERCISED = set()


def expected_case_ids():
    """Every generated case id the suite must exercise, derived from the fixture."""
    ids = set()
    for b in BUILDERS:
        for c in CLAIMS:
            cid = c["id"]
            ids.add("roundtrip/%s/%s" % (b, cid))
            ids.add("restore/%s/%s" % (b, cid))
            ids.add("receipt/%s/%s" % (b, cid))
            ids.add("reordered/%s/%s" % (b, cid))
            ids.add("corrupt/%s/%s" % (b, cid))
            for v in CHANGED:
                ids.add("rehash/%s/%s/%s" % (b, v["id"], cid))
                ids.add("fresh/%s/%s/%s" % (b, v["id"], cid))
                ids.add("reverse/%s/%s/%s" % (b, v["id"], cid))
                ids.add("forged_context_pin/%s/%s/%s" % (b, v["id"], cid))
                ids.add("forged_snapshot_pin/%s/%s/%s" % (b, v["id"], cid))
        for m in MALFORMED:
            ids.add("malformed/%s/%s" % (b, m))
    return ids


MALFORMED = ("claim_not_dict", "claim_text_changed", "claim_text_changed_rehashed", "supports_empty",
             "supports_missing", "extra_key_rehashed", "review_acceptance_true_rehashed",
             "semantic_assessment_changed_rehashed")


def mark(*parts):
    CASES_EXERCISED.add("/".join(parts))


def changed_context(variant):
    ctx = copy.deepcopy(CASES["base_context"])
    ctx.update(copy.deepcopy(variant["set"]))
    return ctx


def build(builder, context, receipt_sha256=RECEIPT_A):
    """A valid bundle from the fixture units under one analysis context."""
    if builder == "hand_built":
        units = [evidence.unit(copy.deepcopy(raw), SUBJECT, receipt_sha256, n)
                 for n, raw in enumerate(CASES["units"], 1)]
        source = {"original_sha256": SUBJECT, "receipt_sha256": receipt_sha256, "attempt_kind": "native",
                  "declared_unit_count": len(units), "normalized_unit_count": len(units)}
        return evidence.bundle(units, {SUBJECT: copy.deepcopy(context)}, [source])
    if builder == "native_receipt":
        receipt = dict(copy.deepcopy(CASES["native_receipt"]), original_sha256=SUBJECT,
                       units=copy.deepcopy(CASES["units"]))
        return evidence.normalize_receipt(receipt, SUBJECT, receipt_sha256,
                                          extra_context={"analysis": copy.deepcopy(context)})
    raise AssertionError("unknown builder " + builder)


def supports_for(value, spec):
    out = []
    for s in spec["supports"]:
        u = next(x for x in value["units"] if x["locator"] == {"page": s["page"]})
        if "quote" in s:
            out.append(evidence.cite_text(u, s["quote"]))
        else:
            out.append(grounded_evidence.pointer_support(u, s["pointer"]))
    return out


def make_claim(value, spec):
    return evidence.claim(value, spec["text"], supports_for(value, spec))


def persist(obj):
    """Serialize as stored JSON bytes and restore with the strict decoder."""
    raw = evidence.canonical(obj)
    return raw, evidence.decode(raw)


def rehash(claim_obj):
    body = {k: v for k, v in claim_obj.items() if k != "evidence_sha256"}
    return dict(body, evidence_sha256=hashlib.sha256(evidence.canonical(body)).hexdigest())


class ReplayBase(unittest.TestCase):
    def assert_structural(self, result):
        self.assertEqual(result, EXPECTED["validate_claim"])
        self.assertEqual(result["semantic_assessment"], "NOT_ASSESSED")
        self.assertIs(result["review_acceptance"], False)

    def assert_claim_flags(self, claim_obj):
        self.assertIs(claim_obj["derived"], True)
        self.assertEqual(claim_obj["semantic_assessment"], "NOT_ASSESSED")
        self.assertIs(claim_obj["review_acceptance"], False)

    def assert_refused(self, code, fn, *args):
        with self.assertRaises(ValueError) as caught:
            fn(*args)
        self.assertEqual(str(caught.exception), code)

    def each(self, fn):
        for b in BUILDERS:
            for c in CLAIMS:
                with self.subTest(builder=b, claim=c["id"]):
                    fn(b, c)


class ClaimPersistenceTests(ReplayBase):
    def test_claim_a_json_roundtrip_validates(self):
        def case(b, c):
            a = build(b, CASES["base_context"])
            made = make_claim(a, c)
            self.assert_claim_flags(made)
            raw, saved = persist(made)
            self.assertEqual(saved, made)
            self.assertEqual(evidence.canonical(saved), raw)
            self.assertEqual(saved["context_sha256"], evidence.sha(a["source_context"]))
            self.assertEqual(saved["snapshot_sha256"], a["snapshot_sha256"])
            self.assert_structural(evidence.validate_claim(saved, a))
            mark("roundtrip", b, c["id"])

        self.each(case)

    def test_restored_byte_identical_bundle_replays_original_claim(self):
        def case(b, c):
            a = build(b, CASES["base_context"])
            raw_a, _ = persist(a)
            raw_claim, saved = persist(make_claim(a, c))
            for v in CHANGED:  # move through every changed context, then return to A
                other = build(b, changed_context(v))
                self.assert_refused(CONTEXT_REFUSAL, evidence.validate_claim, saved, other)
            restored = evidence.decode(raw_a)
            self.assertEqual(evidence.canonical(restored), raw_a)
            self.assertIs(evidence.validate_bundle(restored), restored)
            rebuilt = build(b, CASES["base_context"])
            self.assertEqual(evidence.canonical(rebuilt), raw_a)
            self.assert_structural(evidence.validate_claim(evidence.decode(raw_claim), restored))
            self.assert_structural(evidence.validate_claim(saved, rebuilt))
            mark("restore", b, c["id"])

        self.each(case)

    def test_reordered_equal_context_is_not_a_change(self):
        reordered = CASES["equal_reordered_context"]["context"]
        def case(b, c):
            a = build(b, CASES["base_context"])
            same = build(b, reordered)
            self.assertEqual(same["snapshot_sha256"], a["snapshot_sha256"])
            _, saved = persist(make_claim(a, c))
            self.assert_structural(evidence.validate_claim(saved, same))
            mark("reordered", b, c["id"])

        self.each(case)


class RehashedContextTests(ReplayBase):
    def variants(self, fn):
        for b in BUILDERS:
            for c in CLAIMS:
                a = build(b, CASES["base_context"])
                _, saved = persist(make_claim(a, c))
                for v in CHANGED:
                    with self.subTest(builder=b, claim=c["id"], context=v["id"]):
                        fn(b, c, v, a, saved, build(b, changed_context(v)))

    def test_rehashed_context_bundle_is_valid_with_identical_units(self):
        def case(b, c, v, a, saved, other):
            self.assertIs(evidence.validate_bundle(other), other)
            self.assertEqual(other["units"], a["units"])
            self.assertEqual(other["source_receipts"], a["source_receipts"])
            self.assertNotEqual(other["source_context"], a["source_context"])
            self.assertNotEqual(other["snapshot_sha256"], a["snapshot_sha256"])
            self.assertNotEqual(evidence.sha(other["source_context"]), saved["context_sha256"])

        self.variants(case)

    def test_old_citations_validate_against_rehashed_context(self):
        def case(b, c, v, a, saved, other):
            for support in saved["supports"]:
                checked = evidence.validate_citation(support, other)
                self.assertEqual(checked["snapshot_sha256"], other["snapshot_sha256"])
                self.assertEqual(checked["semantic_assessment"], "NOT_ASSESSED")
                self.assertIs(checked["review_acceptance"], False)
                self.assertEqual(checked["structural_support"], "verified_exact_retained_bytes")

        self.variants(case)

    def test_saved_claim_rejected_on_rehashed_context(self):
        def case(b, c, v, a, saved, other):
            self.assert_refused(CONTEXT_REFUSAL, evidence.validate_claim, saved, other)
            mark("rehash", b, v["id"], c["id"])

        self.variants(case)

    def test_fresh_claim_on_rehashed_context_passes(self):
        def case(b, c, v, a, saved, other):
            fresh = make_claim(other, c)
            self.assert_claim_flags(fresh)
            self.assertEqual(fresh["supports"], saved["supports"])
            self.assertNotEqual(fresh["evidence_sha256"], saved["evidence_sha256"])
            _, fresh_saved = persist(fresh)
            self.assert_structural(evidence.validate_claim(fresh_saved, other))
            mark("fresh", b, v["id"], c["id"])

        self.variants(case)

    def test_fresh_claim_rejected_on_original_context(self):
        def case(b, c, v, a, saved, other):
            _, fresh_saved = persist(make_claim(other, c))
            self.assert_refused(CONTEXT_REFUSAL, evidence.validate_claim, fresh_saved, a)
            mark("reverse", b, v["id"], c["id"])

        self.variants(case)

    def test_forged_context_pin_with_recomputed_hash_rejected(self):
        def case(b, c, v, a, saved, other):
            forged = rehash(dict(saved, context_sha256=evidence.sha(other["source_context"])))
            self.assert_refused(CONTEXT_REFUSAL, evidence.validate_claim, forged, a)
            self.assert_refused(CONTEXT_REFUSAL, evidence.validate_claim, forged, other)
            mark("forged_context_pin", b, v["id"], c["id"])

        self.variants(case)

    def test_forged_snapshot_pin_with_recomputed_hash_rejected(self):
        def case(b, c, v, a, saved, other):
            forged = rehash(dict(saved, snapshot_sha256=other["snapshot_sha256"]))
            self.assert_refused(CONTEXT_REFUSAL, evidence.validate_claim, forged, other)
            self.assert_refused(CONTEXT_REFUSAL, evidence.validate_claim, forged, a)
            mark("forged_snapshot_pin", b, v["id"], c["id"])

        self.variants(case)


class ReceiptIdentityTests(ReplayBase):
    def test_changed_receipt_rejects_old_supports_as_not_current(self):
        def case(b, c):
            a = build(b, CASES["base_context"])
            _, saved = persist(make_claim(a, c))
            moved = build(b, CASES["base_context"], RECEIPT_B)
            self.assertIs(evidence.validate_bundle(moved), moved)
            self.assertEqual([u["text"] for u in moved["units"]], [u["text"] for u in a["units"]])
            self.assertTrue(set(u["unit_id"] for u in moved["units"]).isdisjoint(
                u["unit_id"] for u in a["units"]))
            for support in saved["supports"]:
                self.assert_refused(RECEIPT_REFUSAL, evidence.validate_citation, support, moved)
            self.assert_refused(RECEIPT_REFUSAL, evidence.validate_claim, saved, moved)
            self.assertNotEqual(RECEIPT_REFUSAL, CONTEXT_REFUSAL)
            if b == "hand_built":  # receipt-only change: the analysis context is byte-equal
                self.assertEqual(moved["source_context"], a["source_context"])
            mark("receipt", b, c["id"])

        self.each(case)

    def test_recited_supports_on_changed_receipt_pass(self):
        def case(b, c):
            a = build(b, CASES["base_context"])
            moved = build(b, CASES["base_context"], RECEIPT_B)
            recited = make_claim(moved, c)
            self.assert_claim_flags(recited)
            self.assertEqual([s["quote"] for s in recited["supports"]],
                             [s["quote"] for s in make_claim(a, c)["supports"]])
            _, saved = persist(recited)
            self.assert_structural(evidence.validate_claim(saved, moved))
            self.assert_refused(RECEIPT_REFUSAL, evidence.validate_claim, saved, a)

        self.each(case)


class MalformedTests(ReplayBase):
    def test_corrupt_snapshot_hash_is_bundle_refusal_not_context_refusal(self):
        def case(b, c):
            a = build(b, CASES["base_context"])
            _, saved = persist(make_claim(a, c))
            corrupt = dict(a, snapshot_sha256="0" * 64)
            self.assert_refused(CORRUPT_REFUSAL, evidence.validate_claim, saved, corrupt)
            stale_ctx = dict(a, source_context=build(b, changed_context(CHANGED[0]))["source_context"])
            self.assert_refused(CORRUPT_REFUSAL, evidence.validate_claim, saved, stale_ctx)
            mark("corrupt", b, c["id"])

        self.each(case)

    def test_malformed_saved_claims_rejected(self):
        for b in BUILDERS:
            a = build(b, CASES["base_context"])
            _, saved = persist(make_claim(a, CLAIMS[0]))
            cases = {
                "claim_not_dict": ([saved], "evidence_claim_shape"),
                "claim_text_changed": (dict(saved, claim=saved["claim"] + " Altered."), CONTEXT_REFUSAL),
                "claim_text_changed_rehashed": (rehash(dict(saved, claim=saved["claim"] + " Altered.")),
                                                None),
                "supports_empty": (dict(saved, supports=[]), "evidence_support_bound"),
                "supports_missing": ({k: v for k, v in saved.items() if k != "supports"},
                                     "evidence_support_bound"),
                "extra_key_rehashed": (rehash(dict(saved, accepted_by="fictional")), CONTEXT_REFUSAL),
                "review_acceptance_true_rehashed": (rehash(dict(saved, review_acceptance=True)),
                                                    CONTEXT_REFUSAL),
                "semantic_assessment_changed_rehashed": (rehash(dict(saved, semantic_assessment="ACCEPTED")),
                                                         CONTEXT_REFUSAL),
            }
            self.assertEqual(set(cases), set(MALFORMED))
            for name, (item, code) in sorted(cases.items()):
                with self.subTest(builder=b, case=name):
                    if code is None:
                        # Claim text is author-supplied and never semantically checked: a
                        # rewritten text with a recomputed hash is a different structurally
                        # valid claim on the same context, still NOT_ASSESSED.
                        self.assert_structural(evidence.validate_claim(item, a))
                    else:
                        self.assert_refused(code, evidence.validate_claim, item, a)
                    mark("malformed", b, name)
            self.assert_structural(evidence.validate_claim(saved, a))


class CoverageTests(unittest.TestCase):
    def test_fixture_dimensions_are_complete(self):
        self.assertEqual(CASES["schema"], "fc9-claim-context-replay-cases/v1")
        self.assertEqual(set(BUILDERS), {"hand_built", "native_receipt"})
        self.assertGreaterEqual(len(CHANGED), 3)
        self.assertEqual(len({v["id"] for v in CHANGED}), len(CHANGED))
        for v in CHANGED:
            self.assertNotEqual(changed_context(v), CASES["base_context"])
        self.assertEqual(CASES["equal_reordered_context"]["context"], CASES["base_context"])
        self.assertNotEqual(list(CASES["equal_reordered_context"]["context"]), list(CASES["base_context"]))
        self.assertNotEqual(RECEIPT_A, RECEIPT_B)
        self.assertEqual(len(expected_case_ids()), len(BUILDERS) * (len(CLAIMS) * (5 + 5 * len(CHANGED))
                                                                     + len(MALFORMED)))


if __name__ == "__main__":
    _prog = unittest.main(exit=False)
    if "--case-report" in _OPTS:
        _report = {"expected": sorted(expected_case_ids()), "exercised": sorted(CASES_EXERCISED),
                   "tests_run": _prog.result.testsRun, "successful": _prog.result.wasSuccessful()}
        with open(_OPTS["--case-report"], "x", encoding="utf-8") as _fh:
            json.dump(_report, _fh, indent=2, sort_keys=True)
    raise SystemExit(0 if _prog.result.wasSuccessful() else 1)
