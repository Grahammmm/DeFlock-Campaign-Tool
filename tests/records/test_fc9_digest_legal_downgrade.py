"""FC9-DIGEST-LEGAL-DOWNGRADE: every rule-bearing digest conclusion keeps the legal review label.

Composition test over ``campaign_tool.digest.build.build_digest`` with the real
schema validators. Only the narrative boundary (``build.narrative``) is
replaced, by a stand-in that validates a synthetic, fictional model document
with the real ``validate_model_output`` before returning it. No model
transport, socket, HTTP listener, credential or live law package is used; a
socket guard fails any connection attempt during each case.

For each declared confidence label, rule-bearing source shapes (rule first,
rule only last, single rule, mixed, known-but-lapsed rule) are placed first,
middle and last in a multi-conclusion batch; each must come out as
``needs_attorney_review`` while rule-free conclusions keep the supplied label.
That retained label is not evidence of semantic truth. Unknown rule ids must
raise the existing ``ModelError``. The declared synthetic negative control
shadows only the build module's ``any`` lookup.

Run as a script: ``python3 <this file> --tmp-root <empty dir> [unittest args]``.
The temporary-root argument is handled before any product import. Under
ordinary unittest discovery, importing this module changes no ambient state.
"""
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

if __name__ == "__main__":
    _arguments = sys.argv[1:]
    if len(_arguments) < 2 or _arguments[0] != "--tmp-root":
        raise SystemExit("usage: test_fc9_digest_legal_downgrade.py --tmp-root DIR [unittest arguments]")
    _tmp_root = Path(_arguments[1])
    if not _tmp_root.is_absolute() or not _tmp_root.is_dir() or any(_tmp_root.iterdir()):
        raise SystemExit("--tmp-root must be an existing, empty, absolute directory")
    tempfile.tempdir = str(_tmp_root)
    sys.dont_write_bytecode = True
    sys.argv = [sys.argv[0], *_arguments[2:]]
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))

import copy  # noqa: E402
import hashlib  # noqa: E402
import importlib  # noqa: E402
import json  # noqa: E402
import socket  # noqa: E402
import unittest  # noqa: E402
from datetime import date  # noqa: E402
from unittest import mock  # noqa: E402

build_module = importlib.import_module("campaign_tool.digest.build")
model_module = importlib.import_module("campaign_tool.digest.model")
schema_module = importlib.import_module("campaign_tool.digest.schema")
ModelConfig = model_module.ModelConfig
ModelError = model_module.ModelError

FIXTURE = REPO / "tests" / "fixtures" / "fc9-digest-legal-downgrade" / "cases.json"
CASES = json.loads(FIXTURE.read_text(encoding="utf-8"))
REVIEW = "needs_attorney_review"
POSITION_INDEX = {"first": 0, "middle": 1, "last": 2}

# Case ids exercised in this process; compared with required_case_ids() by the runner.
COVERED = set()


def required_case_ids(cases=None):
    cases = cases or CASES
    ids = {"fixture-consistency", "model-free-needs-review", "negative-control", "inputs-not-mutated",
           "real-schema-gates-bound"}
    for shape in cases["source_shapes"]:
        for confidence in cases["confidences"]:
            for position in cases["positions"]:
                ids.add(f"position/{shape}/{confidence}/{position}")
    for confidence in cases["confidences"]:
        ids.add(f"combined/{confidence}")
    for case in cases["unknown_rule_cases"]:
        for position in cases["positions"]:
            ids.add(f"unknown/{case['id']}/{position}")
    for case in cases["malformed_conclusions"]:
        ids.add(f"malformed/{case['id']}")
    return ids


def case_dimensions(cases=None):
    cases = cases or CASES
    return {
        "confidences": list(cases["confidences"]),
        "positions": list(cases["positions"]),
        "source_shapes": {name: spec["rule_bearing"] for name, spec in cases["source_shapes"].items()},
        "unknown_rule_cases": [case["id"] for case in cases["unknown_rule_cases"]],
        "malformed_conclusions": [case["id"] for case in cases["malformed_conclusions"]],
        "negative_control": dict(cases["negative_control"]),
        "required_case_count": len(required_case_ids(cases)),
    }


def original_sha256():
    """Bound hash of the synthetic original: sha256 over its canonical unit JSON."""
    canonical = json.dumps(CASES["units"], sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def units():
    return copy.deepcopy(CASES["units"])


def package():
    return copy.deepcopy(CASES["package"])


def unit_locator(index):
    return copy.deepcopy(CASES["units"][index]["locator"])


def model_sources(specs):
    """Model-shaped sources (restored locators); an absent rule_id key stays absent."""
    sources = []
    for spec in specs:
        source = {"locator": unit_locator(spec["unit"])}
        if "rule_id" in spec:
            source["rule_id"] = spec["rule_id"]
        sources.append(source)
    return sources


def spec_conclusion(label, confidence, specs):
    return {"text": f"Synthetic conclusion {label}.", "confidence": confidence, "sources": model_sources(specs)}


def narrative_document(conclusions):
    return {
        "scope": "Synthetic fictional record for confidence-label composition tests.",
        "actors": ["Fictional Agency"],
        "dates": [{"date": "2025-06-01", "locator": unit_locator(0), "note": "synthetic event date"}],
        "statements": [{"text": "Synthetic statement.", "locator": unit_locator(1)}],
        "omissions": [],
        "counterevidence": [],
        "conclusions": conclusions,
    }


def check_confidence(actual, expected):
    if actual != expected:
        raise AssertionError(f"confidence {actual!r} != {expected!r}")


class SyntheticNarrative:
    """Stand-in for build.narrative: real-schema-validated synthetic document, no transport."""

    def __init__(self, document):
        self.document = copy.deepcopy(document)
        self.calls = []

    def __call__(self, config, redacted_units, hits, rules, opener=None, *, allowlist=(), denylist=()):
        self.calls.append({"rule_ids": [rule["rule_id"] for rule in rules], "opener": opener,
                           "unit_count": len(redacted_units)})
        returned = copy.deepcopy(self.document)
        schema_module.validate_model_output(returned)
        return returned


def _refuse_connect(*args, **kwargs):
    raise AssertionError("network connection attempted during an offline digest case")


def _refuse_transport(*args, **kwargs):
    raise AssertionError("model transport invoked during an offline digest case")


def build_with(conclusions):
    """Run the real build_digest with only the narrative boundary replaced."""
    stand_in = SyntheticNarrative(narrative_document(conclusions))
    config = ModelConfig(base_url="http://127.0.0.1:9/unused", model_id=CASES["model_id"],
                         privacy_tier=CASES["privacy_tier"])
    with mock.patch.object(build_module, "narrative", stand_in), \
            mock.patch.object(model_module, "chat_json", _refuse_transport), \
            mock.patch.object(socket.socket, "connect", _refuse_connect), \
            mock.patch.object(socket.socket, "connect_ex", _refuse_connect), \
            mock.patch.object(socket, "create_connection", _refuse_connect):
        digest = build_module.build_digest(
            original_sha256(), units(), package(), CASES["jurisdiction"], CASES["law_package_version"],
            CASES["privacy_tier"], model_config=config, event_date=date.fromisoformat(CASES["event_date"]))
    return digest, stand_in


def batch_for(shape, confidence, position):
    """Target conclusion at ``position`` among rule-free fillers with rotated labels."""
    others = [label for label in CASES["confidences"] if label != confidence]
    fillers = [(f"filler-{name}", others[index % len(others)], name)
               for index, name in enumerate(CASES["filler_shapes"])]
    entries = list(fillers)
    entries.insert(POSITION_INDEX[position], (f"target-{shape}", confidence, shape))
    conclusions, expected = [], []
    for label, supplied, name in entries:
        spec = CASES["source_shapes"][name]
        conclusions.append(spec_conclusion(label, supplied, spec["sources"]))
        expected.append((label, supplied, spec))
    return conclusions, expected


def observe_negative_control():
    control = CASES["negative_control"]
    conclusions, _ = batch_for(control["shape"], control["confidence"], control["position"])
    digest, _ = build_with(conclusions)
    return digest["conclusions"][POSITION_INDEX[control["position"]]]["confidence"]


def _always_false(iterable):
    return False


def run_negative_control():
    """Declared synthetic control: shadow build.any only; catch only the exact confidence assertion."""
    control = CASES["negative_control"]
    record = {"injected_behavior": control["injected"], "case": f"{control['shape']}/{control['confidence']}/{control['position']}"}
    if "any" in vars(build_module):
        raise RuntimeError("build module already defines a local any; control precondition failed")
    baseline = observe_negative_control()
    check_confidence(baseline, REVIEW)
    record["baseline_unpatched_observed"] = baseline
    with mock.patch.object(build_module, "any", _always_false, create=True):
        record["patched_shadow_present"] = vars(build_module).get("any") is _always_false
        patched = observe_negative_control()
        record["patched_observed"] = patched
        try:
            check_confidence(patched, REVIEW)
        except AssertionError as error:
            if str(error) != control["expected_failure_message"]:
                raise
            record["patched_assertion_failed"] = True
            record["patched_assertion_message"] = str(error)
        else:
            record["patched_assertion_failed"] = False
    record["restored_shadow_absent"] = "any" not in vars(build_module)
    final = observe_negative_control()
    check_confidence(final, REVIEW)
    record["final_candidate_observed"] = final
    record["killed"] = bool(record["patched_assertion_failed"] and record["patched_shadow_present"]
                            and record["restored_shadow_absent"])
    return record


class LegalDowngradeTests(unittest.TestCase):
    maxDiff = None

    def assert_bound(self, digest_conclusion, label, supplied, spec, sha):
        self.assertEqual(digest_conclusion["text"], f"Synthetic conclusion {label}.")
        expected = REVIEW if spec["rule_bearing"] else supplied
        check_confidence(digest_conclusion["confidence"], expected)
        self.assertEqual(len(digest_conclusion["sources"]), len(spec["sources"]))
        for source, source_spec in zip(digest_conclusion["sources"], spec["sources"]):
            self.assertEqual(source, {"sha256": sha, "locator": unit_locator(source_spec["unit"]),
                                      "rule_id": source_spec.get("rule_id")})

    def assert_digest(self, digest, stand_in, expected):
        sha = original_sha256()
        schema_module.validate_digest(digest)
        self.assertEqual(digest["sha256"], sha)
        self.assertEqual(digest["model_id"], CASES["model_id"])
        self.assertEqual(len(stand_in.calls), 1)
        self.assertEqual(stand_in.calls[0]["rule_ids"], CASES["rules_in_force_on_event_date"])
        self.assertIsNone(stand_in.calls[0]["opener"])
        self.assertEqual(len(digest["conclusions"]), len(expected))
        for conclusion, (label, supplied, spec) in zip(digest["conclusions"], expected):
            self.assert_bound(conclusion, label, supplied, spec, sha)

    def test_fixture_is_synthetic_and_consistent(self):
        self.assertEqual(CASES["schema"], "fc9-digest-legal-downgrade/cases/v1")
        self.assertEqual(CASES["confidences"], schema_module.CONFIDENCE)
        self.assertEqual(set(CASES["positions"]), set(POSITION_INDEX))
        known = {rule["rule_id"] for rule in CASES["package"]["rules"]}
        for name, spec in CASES["source_shapes"].items():
            with self.subTest(shape=name):
                self.assertEqual(spec["rule_bearing"], any(s.get("rule_id") for s in spec["sources"]))
                self.assertTrue(all(s.get("rule_id") in known | {None} for s in spec["sources"]))
                self.assertTrue(all(0 <= s["unit"] < len(CASES["units"]) for s in spec["sources"]))
        for name in CASES["filler_shapes"]:
            self.assertFalse(CASES["source_shapes"][name]["rule_bearing"])
        for case in CASES["unknown_rule_cases"]:
            self.assertTrue(any(s.get("rule_id") is not None and s["rule_id"] not in known for s in case["sources"]))
        in_force = [rule["rule_id"] for rule in build_module.rules_in_force(CASES["package"], date.fromisoformat(CASES["event_date"]))]
        self.assertEqual(in_force, CASES["rules_in_force_on_event_date"])
        self.assertNotIn("fx-rule-lapsed", in_force)  # lapsed but still a known rule id
        COVERED.add("fixture-consistency")

    def test_rule_bearing_conclusions_downgraded_in_every_position(self):
        for shape, spec in CASES["source_shapes"].items():
            if not spec["rule_bearing"]:
                continue
            for confidence in CASES["confidences"]:
                for position in CASES["positions"]:
                    with self.subTest(shape=shape, confidence=confidence, position=position):
                        conclusions, expected = batch_for(shape, confidence, position)
                        digest, stand_in = build_with(conclusions)
                        self.assert_digest(digest, stand_in, expected)
                        check_confidence(digest["conclusions"][POSITION_INDEX[position]]["confidence"], REVIEW)
                        COVERED.add(f"position/{shape}/{confidence}/{position}")

    def test_rule_free_conclusions_keep_supplied_label(self):
        # The retained label is the model's claim, passed through; it implies no semantic truth.
        for shape, spec in CASES["source_shapes"].items():
            if spec["rule_bearing"]:
                continue
            for confidence in CASES["confidences"]:
                for position in CASES["positions"]:
                    with self.subTest(shape=shape, confidence=confidence, position=position):
                        conclusions, expected = batch_for(shape, confidence, position)
                        digest, stand_in = build_with(conclusions)
                        self.assert_digest(digest, stand_in, expected)
                        check_confidence(digest["conclusions"][POSITION_INDEX[position]]["confidence"], confidence)
                        COVERED.add(f"position/{shape}/{confidence}/{position}")

    def test_combined_batch_of_every_shape_per_confidence(self):
        names = list(CASES["source_shapes"])
        for confidence in CASES["confidences"]:
            with self.subTest(confidence=confidence):
                ordered = names + list(reversed(names))
                conclusions, expected = [], []
                for index, name in enumerate(ordered):
                    spec = CASES["source_shapes"][name]
                    label = f"combined-{index}-{name}"
                    conclusions.append(spec_conclusion(label, confidence, spec["sources"]))
                    expected.append((label, confidence, spec))
                digest, stand_in = build_with(conclusions)
                self.assert_digest(digest, stand_in, expected)
                labels = [c["confidence"] for c in digest["conclusions"]]
                self.assertEqual(labels, [REVIEW if CASES["source_shapes"][n]["rule_bearing"] else confidence for n in ordered])
                COVERED.add(f"combined/{confidence}")

    def test_unknown_rule_id_raises_model_error_not_partial_digest(self):
        for case in CASES["unknown_rule_cases"]:
            for position in CASES["positions"]:
                with self.subTest(case=case["id"], position=position):
                    conclusions = [spec_conclusion(f"filler-{i}", "verified", CASES["source_shapes"]["rule_free"]["sources"])
                                   for i in range(2)]
                    conclusions.insert(POSITION_INDEX[position], spec_conclusion(case["id"], "verified", case["sources"]))
                    result = None
                    with self.assertRaises(ModelError) as raised:
                        result, _ = build_with(conclusions)
                    self.assertIsNone(result)
                    self.assertIs(type(raised.exception), ModelError)
                    self.assertEqual(str(raised.exception), CASES["expected_unknown_rule_error"])
                    COVERED.add(f"unknown/{case['id']}/{position}")

    def test_malformed_synthetic_narratives_rejected_before_stand_in_returns(self):
        for case in CASES["malformed_conclusions"]:
            with self.subTest(case=case["id"]):
                document = narrative_document([copy.deepcopy(case["conclusion"])])
                with self.assertRaises(ValueError):
                    schema_module.validate_model_output(copy.deepcopy(document))
                stand_in = SyntheticNarrative(document)
                with self.assertRaises(ValueError):
                    stand_in(None, [], [], [])
                with self.assertRaises(ValueError):
                    build_with([copy.deepcopy(case["conclusion"])])
                COVERED.add(f"malformed/{case['id']}")

    def test_model_free_output_is_ordinary_needs_review(self):
        with mock.patch.object(build_module, "narrative", _refuse_transport), \
                mock.patch.object(socket.socket, "connect", _refuse_connect):
            digest = build_module.build_digest(
                original_sha256(), units(), package(), CASES["jurisdiction"], CASES["law_package_version"],
                CASES["privacy_tier"], event_date=date.fromisoformat(CASES["event_date"]))
        schema_module.validate_digest(digest)
        self.assertIsNone(digest["model_id"])
        self.assertTrue(digest["conclusions"])
        for conclusion in digest["conclusions"]:
            check_confidence(conclusion["confidence"], REVIEW)
            self.assertTrue(all(s["sha256"] == original_sha256() for s in conclusion["sources"]))
        COVERED.add("model-free-needs-review")

    def test_inputs_are_not_mutated_and_locators_stay_original(self):
        before = copy.deepcopy(CASES)
        conclusions, expected = batch_for("mixed", "verified", "middle")
        supplied = copy.deepcopy(conclusions)
        digest, stand_in = build_with(conclusions)
        self.assert_digest(digest, stand_in, expected)
        self.assertEqual(conclusions, supplied)
        self.assertEqual(CASES, before)
        self.assertEqual(stand_in.document["conclusions"], supplied)
        COVERED.add("inputs-not-mutated")

    def test_real_schema_gates_are_bound(self):
        self.assertIs(build_module.validate_digest, schema_module.validate_digest)
        self.assertNotIn("any", vars(build_module))
        self.assertIs(build_module.ModelError, ModelError)
        conclusions, _ = batch_for("rule_free", "verified", "first")
        digest, _ = build_with(conclusions)
        tampered = copy.deepcopy(digest)
        tampered["conclusions"][0]["confidence"] = "certain"
        with self.assertRaises(ValueError):
            schema_module.validate_digest(tampered)
        tampered = copy.deepcopy(digest)
        tampered["conclusions"][0]["sources"][0]["sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            schema_module.validate_digest(tampered)
        COVERED.add("real-schema-gates-bound")

    def test_declared_negative_control_last_source_rule(self):
        record = run_negative_control()
        self.assertEqual(record["baseline_unpatched_observed"], REVIEW)
        self.assertEqual(record["patched_observed"], CASES["negative_control"]["confidence"])
        self.assertTrue(record["patched_assertion_failed"])
        self.assertEqual(record["patched_assertion_message"], CASES["negative_control"]["expected_failure_message"])
        self.assertTrue(record["restored_shadow_absent"])
        self.assertEqual(record["final_candidate_observed"], REVIEW)
        self.assertTrue(record["killed"])
        COVERED.add("negative-control")


if __name__ == "__main__":
    _program = unittest.main(module="__main__", exit=False, verbosity=2)
    _required = required_case_ids()
    print("FC9-CASE-COVERAGE " + json.dumps({
        "required": len(_required), "covered": len(COVERED & _required),
        "missing": sorted(_required - COVERED), "unexpected": sorted(COVERED - _required),
        "tests_run": _program.result.testsRun, "skipped": len(_program.result.skipped),
        "tmp_root": tempfile.tempdir is not None,
    }, sort_keys=True), file=sys.stderr)
    _ok = _program.result.wasSuccessful() and _required == COVERED and not _program.result.skipped
    sys.exit(0 if _ok else 1)
