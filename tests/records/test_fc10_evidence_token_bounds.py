"""FC10 grounded JSON evidence token/depth bound qualification; imports are discovery-inert.

Synthetic generated JSON only. Documents are built independently at one below,
exactly at and one above the real configured MAX_TOKENS and MAX_DEPTH caps and
exercised through both pointer entry points (text JSON and canonical retained
unit data). A value is the root or any array element / object member value;
object keys are not values. Structural support is never semantic acceptance.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "tests/fixtures/fc10-evidence-token-bounds/cases.json"
MODULE = "tests.records.test_fc10_evidence_token_bounds"
BASELINE, CONTROL, RESTORED = "BASELINE_REAL", "CONTROL_MUTANT_INFORMATIONAL", "RESTORED_REAL"


def expect(condition, reason):
    if not condition:
        raise AssertionError(reason)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def save(path, value):
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")


def fixture():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    expect(data["schema"] == "fc10-evidence-token-bounds/v1" and data["synthetic_only"] is True,
           "synthetic_fixture_required")
    return data


def product():
    # Delayed until a dedicated runner/child has set its temporary-root policy.
    from campaign_tool.records import grounded_evidence, retained_units
    from campaign_tool.records.understanding import evidence
    return grounded_evidence, retained_units, evidence


# ---- independent model of the documented walker (no product code) ----

def children(value):
    if isinstance(value, list):
        return [(str(i), v) for i, v in enumerate(value)]
    if isinstance(value, dict):
        return [(k.replace("~", "~0").replace("/", "~1"), v) for k, v in value.items()]
    return []


def count(value):
    return 1 + sum(count(v) for _, v in children(value))


def depth(value):
    kids = children(value)
    return 1 + max(depth(v) for _, v in kids) if kids else 0


def pointers(value, path=""):
    out = [path]
    for token, child in children(value):
        out.extend(pointers(child, path + "/" + token))
    return out


def resolve(value, pointer):
    for token in pointer.split("/")[1:]:
        if isinstance(value, list):
            value = value[int(token)]
        else:
            value = value[token.replace("~1", "/").replace("~0", "~")]
    return value


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def strict_loads(text):
    def pairs(items):
        seen = {}
        for key, val in items:
            expect(key not in seen, "duplicate_key_in_generated_document")
            seen[key] = val
        return seen
    return json.loads(text, object_pairs_hook=pairs)


def token_document(spec, surface, shape, total):
    """Return a value with exactly `total` values; data roots are objects."""
    if shape == "array":
        if surface == "text":
            value = list(range(total - 1))
        else:
            value = {"fill": list(range(total - 2))}
    elif shape == "object":
        value = {spec["object_key_template"].format(i): i for i in range(total - 1)}
    else:
        empty_array, empty_object, nested, fill = spec["mixed_keys"]
        value = {empty_array: [], empty_object: {},
                 nested: {"x": [True, None, "s\u00e9\"\\"], "y": [[], {}], "z~": {"q/": -1.5}},
                 fill: []}
        value[fill] = list(range(total - count(value)))
    expect(count(value) == total, "independent_token_construction")
    return value


def depth_document(spec, surface, shape, deepest):
    """Return a chain whose deepest value is at walker depth `deepest`."""
    def kind(level):
        if shape == "array":
            return "o" if surface == "data" and level == 0 else "a"
        if shape == "object":
            return "o"
        return ("o" if level % 2 == 0 else "a") if surface == "data" else ("a" if level % 2 == 0 else "o")
    leaf = 0 if shape != "empty_leaf" else ({} if surface == "text" else [])
    value = leaf
    for level in range(deepest - 1, -1, -1):
        value = [value] if kind(level) == "a" else {spec["depth_key"]: value}
    expect(depth(value) == deepest and count(value) == deepest + 1, "independent_depth_construction")
    return value


def serialize(value, style):
    if style == "compact":
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    if style == "indented_ascii":
        return json.dumps(value, indent=1, ensure_ascii=True)
    return "\n\t " + json.dumps(value, separators=(", ", ": "), ensure_ascii=False) + " \r\n"


class Case:
    def __init__(self, spec, surface, limit, shape, offset):
        self.surface, self.limit, self.shape, self.offset = surface, limit, shape, offset
        cap = spec["limits"]["max_tokens" if limit == "tokens" else "max_depth"]
        self.size = cap + offset
        self.case_id = "%s-%s-%s-%+d" % (surface, limit, shape, offset)
        self.value = (token_document(spec, surface, shape, self.size) if limit == "tokens"
                      else depth_document(spec, surface, shape, self.size))
        self.refuse = offset > 0
        u = spec["unit"]
        text = (serialize(self.value, spec["text_styles"][shape]) if surface == "text" else u["data_text"])
        data = {} if surface == "text" else self.value
        _, _, evidence = product()
        self.unit = evidence.unit({"kind": u["kind"], "locator": u["locator"], "text": text, "data": data},
                                  digest(u["subject_label"].encode()), digest(u["receipt_label"].encode()), 1)
        self.document = text if surface == "text" else canonical(self.value).decode("utf-8")
        self.read_result = {"snapshot_sha256": digest(("fc10-snapshot:" + self.case_id).encode()),
                            "units": [self.unit]}

    def targets(self):
        every = pointers(self.value)
        chosen = {"root": ""}
        if self.limit == "depth":
            chosen["deepest"] = max(every, key=lambda p: p.count("/"))
        tail = every[-1]
        chosen["near_tail"] = tail
        # The last container member path (near the tail) and an inner container.
        containers = [p for p in every if p and isinstance(resolve(self.value, p), (list, dict))]
        empties = [p for p in containers if not resolve(self.value, p)]
        leaves = [p for p in every if not isinstance(resolve(self.value, p), (list, dict))]
        if containers:
            chosen["container"] = containers[-1]
        if empties:
            chosen["empty_container"] = empties[0]
        if leaves:
            chosen["leaf"] = leaves[-1]
        for p in chosen.values():
            expect(len(p) <= 1024, "pointer_length_prerequisite")
        return chosen


class BoundsMixin:
    def setUp(self):
        self.spec = fixture()
        self.coverage = []
        self.observations = []

    def api(self, surface):
        ge = product()[0]
        return ge.pointer_support if surface == "text" else ge.data_pointer_support

    def prerequisites(self, case):
        ge, ru, _ = product()
        unit = case.unit
        expect(digest(unit["text"].encode()) == unit["text_sha256"], "fresh_text_hash")
        expect(digest(canonical({k: unit[k] for k in ("kind", "locator", "text", "data")}))
               == unit["artifact_sha256"], "fresh_artifact_hash")
        expect(unit["authority_basis"] == "receipt_bound_derivative_not_canonical", "derivative_authority")
        expect(strict_loads(case.document) == case.value, "valid_strict_json_document")
        ru._json(case.document, "evidence_json_invalid")
        expect(len(case.document.encode("utf-8")) <= ge.MAX_TEXT, "text_size_prerequisite")

    def check_case(self, case, label):
        """Deciding assertions; raises AssertionError(reason) with an explicit reason."""
        ge = product()[0]
        self.prerequisites(case)
        before = copy.deepcopy(case.unit)
        api = self.api(case.surface)
        targets = case.targets()
        observation = {"case_id": case.case_id, "label": label, "size": case.size,
                       "independent_count": count(case.value), "independent_depth": depth(case.value),
                       "document_sha256": digest(case.document.encode("utf-8")),
                       "caps": {"MAX_TOKENS": ge.MAX_TOKENS, "MAX_DEPTH": ge.MAX_DEPTH}}
        if case.refuse:
            errors = {}
            for name, pointer in sorted(targets.items()):
                try:
                    api(case.unit, pointer)
                except ValueError as error:
                    errors[name] = str(error)
                    continue
                observation.update(outcome="accepted", targets=sorted(targets))
                self.observations.append(observation)
                raise AssertionError("over_limit_refused:" + case.limit)
            observation.update(outcome="refused", errors=errors)
            self.observations.append(observation)
            expect(set(errors.values()) == {self.spec["refusal_code"]}, "exact_refusal_code")
        else:
            tokens = ge._tokens(case.document)
            expected = pointers(case.value)
            expect(len(tokens) == count(case.value) == len(set(expected)), "walker_value_inventory_count")
            expect(set(tokens) == set(expected), "walker_pointer_inventory")
            raw = case.document.encode("utf-8")
            quotes = {}
            for name, pointer in sorted(targets.items()):
                support = api(case.unit, pointer)
                start, end = support["span"]["start_byte"], support["span"]["end_byte"]
                expect(support["json_pointer"] == pointer and (start, end) == tokens[pointer], "exact_span")
                expect(support["quote"] == raw[start:end].decode("utf-8"), "exact_retained_quote_bytes")
                expect(support["quote"] == support["quote"].strip(), "quote_excludes_whitespace")
                expect(strict_loads(support["quote"]) == resolve(case.value, pointer), "quote_is_target_value")
                if case.surface == "data":
                    expect(support["quote"].encode("utf-8") == canonical(resolve(case.value, pointer)),
                           "canonical_data_quote")
                    expect(support["data_sha256"] == digest(raw), "data_sha256_binding")
                    expect(support["span_basis"] == "canonical_retained_unit_data", "data_span_basis")
                    expect(support["schema"] == ge.DATA_VERSION, "data_schema")
                else:
                    expect(support["schema"] == ge.VERSION, "text_schema")
                verdict = ge.validate_support(support, case.read_result)
                expect(verdict["semantic_assessment"] == "NOT_ASSESSED", "semantic_not_assessed")
                expect(verdict["review_acceptance"] is False, "review_acceptance_false")
                expect(verdict["structural_support"] == ("verified_exact_retained_data_projection"
                       if case.surface == "data" else "verified_exact_retained_bytes"), "structural_label")
                quotes[name] = {"pointer": pointer, "span": [start, end],
                                "quote_sha256": digest(support["quote"].encode("utf-8"))}
            observation.update(outcome="accepted", targets=quotes, walker_values=len(tokens))
            self.observations.append(observation)
            expect("near_tail" in quotes and "root" in quotes, "target_coverage")
            expect("leaf" in quotes or "empty_container" in quotes, "leaf_or_empty_target")
        expect(case.unit == before, "inputs_unchanged")
        return observation

    def table(self, surface, limit):
        seen = set()
        for shape in self.spec["shapes"][limit]:
            for offset in self.spec["offsets"]:
                case = Case(self.spec, surface, limit, shape, offset)
                with self.subTest(case=case.case_id):
                    observation = self.check_case(case, BASELINE)
                    seen.add(offset)
                    names = set(observation.get("targets", {}))
                    if {"container", "leaf"} <= names or "empty_container" in names:
                        self.coverage.append("container_and_leaf_targets")
        expect(seen == {-1, 0, 1}, "below_at_over_coverage")
        self.coverage.extend(["%s_%s_below_at_over" % (surface, limit), "exact_retained_quote",
                              "near_tail_pointer", "inputs_unchanged", "review_flags_preserved"])


class EvidenceTokenBoundsTests(BoundsMixin, unittest.TestCase):
    def test_configured_caps_match_frozen_fixture(self):
        ge = product()[0]
        self.assertEqual((ge.MAX_TOKENS, ge.MAX_DEPTH),
                         (self.spec["limits"]["max_tokens"], self.spec["limits"]["max_depth"]))

    def test_text_tokens_below_at_over(self):
        self.table("text", "tokens")

    def test_data_tokens_below_at_over(self):
        self.table("data", "tokens")

    def test_text_depth_below_at_over(self):
        self.table("text", "depth")

    def test_data_depth_below_at_over(self):
        self.table("data", "depth")

    def control(self, attribute, killed_limit, other_limit, dimension):
        ge = product()[0]
        original = {"MAX_TOKENS": ge.MAX_TOKENS, "MAX_DEPTH": ge.MAX_DEPTH}
        for surface in self.spec["surfaces"]:
            over = Case(self.spec, surface, killed_limit, self.spec["shapes"][killed_limit][0], 1)
            at = Case(self.spec, surface, killed_limit, self.spec["shapes"][killed_limit][0], 0)
            other_over = Case(self.spec, surface, other_limit, self.spec["shapes"][other_limit][0], 1)
            # In-process only: no on-disk mutation; mock restores the attribute on exit.
            with mock.patch.object(ge, attribute, original[attribute] + 1):
                with self.assertRaises(AssertionError) as killed:
                    self.check_case(over, CONTROL)
                self.assertEqual(str(killed.exception), "over_limit_refused:" + killed_limit)
                self.check_case(at, CONTROL)            # exact-limit positive still active
                self.check_case(other_over, CONTROL)    # the other limit still refuses
            self.assertEqual({"MAX_TOKENS": ge.MAX_TOKENS, "MAX_DEPTH": ge.MAX_DEPTH}, original)
            self.check_case(over, RESTORED)
            self.check_case(at, RESTORED)
            self.check_case(other_over, RESTORED)
        self.coverage.extend([dimension, "negative_control_restored"])

    def test_negative_control_max_tokens(self):
        self.control("MAX_TOKENS", "tokens", "depth", "negative_control_max_tokens_killed")

    def test_negative_control_max_depth(self):
        self.control("MAX_DEPTH", "depth", "tokens", "negative_control_max_depth_killed")


class CoverageResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.dimensions = set()
        self.observations = []
        self.passed = []

    def addSuccess(self, test):
        self.dimensions.update(getattr(test, "coverage", []))
        self.passed.append(test.id())
        super().addSuccess(test)

    def stopTest(self, test):
        self.observations.extend(getattr(test, "observations", []))
        super().stopTest(test)


def run_suite(report_path, discovery=False):
    loader = unittest.TestLoader()
    suite = (loader.discover(str(REPO / "tests/records"), pattern="test_fc10_evidence_token_bounds.py",
                             top_level_dir=str(REPO)) if discovery
             else loader.loadTestsFromName(MODULE + ".EvidenceTokenBoundsTests"))
    with (Path(report_path).parent / "unittest.txt").open("x") as stream:
        result = unittest.TextTestRunner(stream=stream, verbosity=2, resultclass=CoverageResult).run(suite)
    required = set(fixture()["required_dimensions"])
    by_label = {label: [o for o in result.observations if o["label"] == label]
                for label in (BASELINE, CONTROL, RESTORED)}
    summary = {"tests_run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
               "skipped": len(result.skipped), "passed_test_ids": sorted(result.passed),
               "failure_details": [[t.id(), tb] for t, tb in result.failures + result.errors],
               "dimensions": sorted(result.dimensions), "required_dimensions": sorted(required),
               "missing_dimensions": sorted(required - result.dimensions),
               "case_ids": {label: sorted({o["case_id"] for o in obs}) for label, obs in by_label.items()},
               "observations": by_label, "semantic_acceptance": False}
    save(report_path, summary)
    return int(not result.wasSuccessful() or bool(result.skipped) or bool(summary["missing_dimensions"])
               or result.testsRun == 0)


def sentinel_discovery(report_path):
    # Snapshot BEFORE ordinary discovery, then run an unrelated peer before/after.
    old_env, old_temp, old_path = dict(os.environ), tempfile.tempdir, list(sys.path)
    sentinel_file = Path(tempfile.gettempdir()) / "sentinel-fixture.txt"
    sentinel_file.write_text("unrelated sentinel")

    class Sentinel(unittest.TestCase):
        def runTest(self):
            self.assertEqual(dict(os.environ), old_env)
            self.assertEqual(tempfile.tempdir, old_temp)
            self.assertEqual(sys.path, old_path)
            self.assertEqual(sentinel_file.read_text(), "unrelated sentinel")
            with tempfile.TemporaryDirectory() as directory:
                self.assertEqual(Path(directory).parent, Path(old_temp))
    before = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite([Sentinel()]))
    code = run_suite(report_path, discovery=True)
    after = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite([Sentinel()]))
    expect(before.wasSuccessful() and after.wasSuccessful(), "discovery_peer_contamination")
    return code


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("suite", "discovery"), default="suite")
    parser.add_argument("--temp-root", required=True)
    args = parser.parse_args()
    temp = Path(args.temp_root)
    expect(temp.is_absolute() and ".." not in temp.parts and temp.is_dir(), "literal_existing_temp_root")
    tempfile.tempdir = str(temp)  # before any product import
    sys.path.insert(0, str(REPO))
    report = temp / "suite-summary.json"
    return sentinel_discovery(report) if args.mode == "discovery" else run_suite(report)


if __name__ == "__main__":
    raise SystemExit(main())
