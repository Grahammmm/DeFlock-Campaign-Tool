"""FC15 derived-claim text and support-count bounds (fictional, test-only).

Freezes the existing derived_claim limits: claim text is 1..8192 UTF-8 bytes
(evidence_claim_bound) and supports is a list of 1..32 entries
(evidence_support_bound). Accepted claims stay derived, NOT_ASSESSED and
review_acceptance False. Ordinary discovery has no ambient effects: product
imports happen inside setUp and script mode delegates guards to the card runner.
"""
from copy import deepcopy
import hashlib
import inspect
import json
from pathlib import Path
import unittest

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "fc15-claim-bounds" / "cases.json"
CASES = ("text_bytes", "support_count", "container_types", "replay", "hold_invariants",
         "text_limit_mutant", "support_limit_mutant")
TEXT_GATE = 'type(claim)is str and 0<len(claim.encode("utf-8"))<=8192'
TEXT_GATE_DISABLED = 'type(claim)is str and 0<len(claim.encode("utf-8"))'
SUPPORT_GATE = "type(supports)is list and 0<len(supports)<=32"
SUPPORT_GATE_DISABLED = "type(supports)is list and 0<len(supports)"
NOT_RAISED = "FC15_BOUND_NOT_ENFORCED"
WRONG_CODE = "FC15_BOUND_WRONG_CODE"


def sha(label):
    # Fictional identities are derived at runtime; no hex literals are committed.
    return hashlib.sha256(("fc15-claim-bounds:" + label).encode("utf-8")).hexdigest()


def canonical_digest(value):
    # Independent restatement of the documented canonical form (sorted keys,
    # compact separators, UTF-8, no NaN); not the product helper.
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def make_text(fixture, form, size):
    unit, pad = fixture["forms"][form]["unit"], fixture["pad"]
    width = len(unit.encode("utf-8"))
    text = unit * (size // width)
    used = len(text.encode("utf-8"))
    return text + pad * (size - used)


class ClaimBoundsTests(unittest.TestCase):
    def setUp(self):
        from campaign_tool.records import grounded_evidence
        self.g = grounded_evidence
        self.f = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(self.f["cases"], list(CASES))
        self.assertEqual(self.f["limits"], {"claim_utf8_bytes": 8192, "supports": 32})
        text = self.f["unit_text"]
        self.unit = {"unit_id": sha("unit"), "original_sha256": sha("original"), "import_id": sha("import"),
                     "artifact_sha256": sha("artifact"),
                     "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                     "text": text, "locator": {"page": 1}}
        self.read = {"snapshot_sha256": sha("snapshot"), "units": [self.unit]}
        self.context = sha("context")
        start, end = self.f["support_span"]
        self.support = self.g.span_support(self.unit, start, end)
        # The support is genuinely valid on its own, so every boundary below is isolated.
        self.assertEqual(self.g.validate_support(self.support, self.read)["structural_support"],
                         "verified_exact_retained_bytes")
        self.assertEqual(self.support["quote"], text[start:end])
        self.texts = {row["id"]: row for row in self.f["text_bytes"]}

    # helpers -----------------------------------------------------------
    def claim_text(self, case_id):
        row = self.texts[case_id]
        text = make_text(self.f, row["form"], row["bytes"])
        self.assertEqual(len(text.encode("utf-8")), row["bytes"])
        if self.f["forms"][row["form"]]["multibyte"]:
            self.assertLess(len(text), row["bytes"], "multibyte form must contain non-ASCII characters")
        return text

    def supports(self, count):
        return [deepcopy(self.support) for _ in range(count)]

    def assert_refused(self, fn, code, *args):
        with self.assertRaises(ValueError, msg=NOT_RAISED + ":" + code) as caught:
            fn(*args)
        self.assertEqual(str(caught.exception), code, WRONG_CODE + ":" + code)

    def assert_accepted(self, fn, claim, supports):
        claim_before, supports_before, read_before = deepcopy(claim), deepcopy(supports), deepcopy(self.read)
        made = fn(self.read, self.context, claim, supports)
        self.assertEqual(claim, claim_before)
        self.assertEqual(supports, supports_before)
        self.assertEqual(self.read, read_before)
        body = {"schema": self.g.VERSION, "snapshot_sha256": self.read["snapshot_sha256"],
                "context_sha256": self.context, "claim": claim, "supports": supports, "derived": True,
                "semantic_assessment": "NOT_ASSESSED", "review_acceptance": False}
        self.assertEqual(made, {**body, "evidence_sha256": canonical_digest(body)})
        self.assertIs(made["derived"], True)
        self.assertEqual(made["semantic_assessment"], "NOT_ASSESSED")
        self.assertIs(made["review_acceptance"], False)
        return made

    def check_text_row(self, fn, case_id, count=1):
        row = self.texts[case_id]
        claim = self.claim_text(case_id)
        if row["expect"] == "ok":
            return self.assert_accepted(fn, claim, self.supports(count))
        self.assert_refused(fn, row["expect"], self.read, self.context, claim, self.supports(count))
        return None

    def check_count_row(self, fn, row, case_id="ascii_8192"):
        claim = self.claim_text(case_id)
        if row["expect"] == "ok":
            return self.assert_accepted(fn, claim, self.supports(row["count"]))
        self.assert_refused(fn, row["expect"], self.read, self.context, claim, self.supports(row["count"]))
        return None

    def mutant(self, gate, disabled):
        # Isolated in-memory copy of the read-only product function with exactly
        # one gate weakened; all other statements are kept. The product module
        # and its function object are never changed.
        original = self.g.derived_claim
        source = inspect.getsource(original)
        self.assertEqual(source.count(gate), 1)
        namespace = dict(vars(self.g))
        exec(compile(source.replace(gate, disabled), "<fc15-mutant>", "exec"), namespace)
        self.assertIs(self.g.derived_claim, original)
        self.assertEqual(inspect.getsource(self.g.derived_claim), source)
        return namespace["derived_claim"]

    # frozen cases ---------------------------------------------------------
    def test_text_bytes(self):
        limit = self.f["limits"]["claim_utf8_bytes"]
        for row in self.f["text_bytes"]:
            with self.subTest(case=row["id"]):
                self.assertEqual(row["expect"] == "ok", 0 < row["bytes"] <= limit)
                self.check_text_row(self.g.derived_claim, row["id"])

    def test_support_count(self):
        limit = self.f["limits"]["supports"]
        for row in self.f["support_count"]:
            with self.subTest(case=row["id"]):
                self.assertEqual(row["expect"] == "ok", 0 < row["count"] <= limit)
                self.check_count_row(self.g.derived_claim, row)

    def test_container_types(self):
        good_claim, good_supports = "Synthetic claim.", self.supports(1)
        claims = {"none": None, "bytes": b"Synthetic claim.", "int": 7, "list": ["Synthetic claim."],
                  "dict": {"claim": "Synthetic claim."}, "bool": True}
        supports = {"none": None, "tuple": tuple(self.supports(1)), "single_support_dict": deepcopy(self.support),
                    "str": "support", "int": 1, "generator": (s for s in self.supports(1))}
        self.assertEqual(sorted(claims), sorted(self.f["container_types"]["claims"]))
        self.assertEqual(sorted(supports), sorted(self.f["container_types"]["supports"]))
        for name, value in claims.items():
            with self.subTest(claim=name):
                self.assert_refused(self.g.derived_claim, self.f["codes"]["claim"], self.read, self.context, value, good_supports)
        for name, value in supports.items():
            with self.subTest(supports=name):
                self.assert_refused(self.g.derived_claim, self.f["codes"]["supports"], self.read, self.context, good_claim, value)
        self.assert_accepted(self.g.derived_claim, good_claim, good_supports)

    def test_replay(self):
        for case_id, count in self.f["replay"]["accepted"]:
            with self.subTest(accepted=case_id, count=count):
                made = self.check_text_row(self.g.derived_claim, case_id, count)
                self.assertIsNotNone(made)
                before, read_before = deepcopy(made), deepcopy(self.read)
                result = self.g.validate_claim(made, self.read, self.context)
                self.assertEqual(result, {"structural_support": "verified_exact_retained_bytes",
                                          "semantic_assessment": "NOT_ASSESSED", "review_acceptance": False})
                self.assertEqual(made, before)
                self.assertEqual(self.read, read_before)
        base = self.assert_accepted(self.g.derived_claim, self.claim_text("ascii_8192"), self.supports(1))
        for case_id, count, code in self.f["replay"]["forged"]:
            with self.subTest(forged=case_id, count=count):
                forged = {k: v for k, v in base.items() if k != "evidence_sha256"}
                forged["claim"] = self.claim_text(case_id)
                forged["supports"] = self.supports(count)
                forged["evidence_sha256"] = canonical_digest(forged)
                before = deepcopy(forged)
                self.assert_refused(self.g.validate_claim, code, forged, self.read, self.context)
                self.assertEqual(forged, before)

    def test_hold_invariants(self):
        accepted = [r["id"] for r in self.f["text_bytes"] if r["expect"] == "ok"]
        for case_id in accepted:
            for count in (1, self.f["limits"]["supports"]):
                with self.subTest(case=case_id, count=count):
                    made = self.check_text_row(self.g.derived_claim, case_id, count)
                    self.assertEqual(set(made), {"schema", "snapshot_sha256", "context_sha256", "claim", "supports",
                                                 "derived", "semantic_assessment", "review_acceptance", "evidence_sha256"})
                    self.assertIs(made["derived"], True)
                    self.assertEqual(made["semantic_assessment"], "NOT_ASSESSED")
                    self.assertIs(made["review_acceptance"], False)

    # deciding synthetic controls -----------------------------------------
    def test_text_limit_mutant(self):
        weak = self.mutant(TEXT_GATE, TEXT_GATE_DISABLED)
        for case_id in ("ascii_8193", "mixed_8193", "two_byte_8192_plus_ascii", "four_byte_8192_plus_ascii"):
            with self.subTest(killed=case_id):
                with self.assertRaises(self.failureException) as caught:
                    self.check_text_row(weak, case_id)
                self.assertIn(NOT_RAISED + ":evidence_claim_bound", str(caught.exception))
                # Restored genuine product still refuses the same input.
                self.check_text_row(self.g.derived_claim, case_id)
        for case_id in ("ascii_8192", "mixed_8192", "four_byte_8192", "ascii_one"):
            with self.subTest(positive=case_id):
                self.assertEqual(self.check_text_row(weak, case_id), self.check_text_row(self.g.derived_claim, case_id))
        # Unrelated gates stay intact in this mutant.
        self.check_text_row(weak, "ascii_empty")
        self.check_count_row(weak, self.f["support_count"][-1])
        print("CONTROL: text-limit mutant killed by evidence_claim_bound; restored product refused and limit positives passed")

    def test_support_limit_mutant(self):
        weak = self.mutant(SUPPORT_GATE, SUPPORT_GATE_DISABLED)
        over = next(r for r in self.f["support_count"] if r["count"] == self.f["limits"]["supports"] + 1)
        for case_id in ("ascii_8192", "mixed_8192"):
            with self.subTest(killed=case_id):
                with self.assertRaises(self.failureException) as caught:
                    self.check_count_row(weak, over, case_id)
                self.assertIn(NOT_RAISED + ":evidence_support_bound", str(caught.exception))
                self.check_count_row(self.g.derived_claim, over, case_id)
        for row in self.f["support_count"]:
            if row["expect"] == "ok":
                with self.subTest(positive=row["id"]):
                    self.assertEqual(self.check_count_row(weak, row), self.check_count_row(self.g.derived_claim, row))
        self.check_count_row(weak, self.f["support_count"][0])
        self.check_text_row(weak, "ascii_8193")
        print("CONTROL: support-limit mutant killed by evidence_support_bound; restored product refused and limit positives passed")


if __name__ == "__main__":
    # Derive imports from this file, never the caller's cwd or a module-path flag.
    import argparse
    import sys
    parser = argparse.ArgumentParser()
    parser.add_argument("--temp-root", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from tools.fc15_claim_bounds_acceptance import worker
    raise SystemExit(worker("suite", args.temp_root, args.report))
