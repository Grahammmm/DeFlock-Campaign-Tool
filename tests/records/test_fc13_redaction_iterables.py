"""FC13: independently authored synthetic, in-process two-stage policy tests.

Import is inert: no product imports, path/environment changes or permanent hooks.
The excerpt case augments a real detector hit only inside the invocation double.
No request is sent: the narrative double uses the real ModelSources boundary.
"""
import ast
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from datetime import date
import importlib
import inspect
import itertools
import json
from pathlib import Path
import socket
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "tests/fixtures/fc13-redaction-iterables/cases.json"
SCENARIOS = ("citation", "duty", "excerpt", "allow_rule", "combined_source")
KINDS = ("list", "tuple", "generator")


def fixture():
    data = json.loads(FIXTURE.read_text())
    unittest.TestCase().assertEqual(data["scenarios"], list(SCENARIOS))
    unittest.TestCase().assertEqual(data["representations"], list(KINDS))
    unittest.TestCase().assertEqual(data["empty_representations"], list(KINDS))
    return data


@contextmanager
def offline():
    def forbidden(*args, **kwargs):
        raise RuntimeError("FC13 offline boundary: transport is forbidden")
    with ExitStack() as stack:
        for name in ("connect", "connect_ex", "bind", "listen", "sendto"):
            stack.enter_context(mock.patch.object(socket.socket, name, forbidden))
        for name in ("create_connection", "getaddrinfo"):
            stack.enter_context(mock.patch.object(socket, name, forbidden))
        stack.enter_context(mock.patch("urllib.request.urlopen", forbidden))
        stack.enter_context(mock.patch("http.client.HTTPConnection.connect", forbidden))
        yield


def represent(values, kind):
    if kind == "list":
        return list(values)
    if kind == "tuple":
        return tuple(values)
    if kind == "generator":
        return (value for value in values)
    raise ValueError("unknown policy representation")


class Once:
    def __init__(self, values):
        self.values = values
        self.iterations = 0
        self.yielded = []

    def __iter__(self):
        self.iterations += 1
        if self.iterations != 1:
            raise AssertionError("caller iterable consumed more than once")
        for value in self.values:
            self.yielded.append(value)
            yield value


class RedactionIterableTests(unittest.TestCase):
    def invoke(self, scenario, allow_kind="list", deny_kind="list", *,
               builder=None, policies=None, detector_only=False, defaults=False):
        data = fixture()
        phrase, allowed = data["deny_phrase"], data["allow_value"]
        deny_enabled = scenario != "allow_rule"
        allow_enabled = scenario in ("allow_rule", "combined_source")
        allow = [allowed] if allow_enabled else []
        deny = [phrase] if deny_enabled else []
        unit = {"locator": {"page": 7, "member": "fictional-record.txt"},
                "text": "Data retained for 30 days. " + data["unit_email"]}
        rule = {"rule_id": "ca-fc-policy", "citation": "Veh. Code 2413",
                "duty": "Maintain fictional records.", "effective_from": "2020-01-01",
                "effective_to": None}
        if scenario == "citation":
            rule["citation"] += " " + phrase
        elif scenario == "duty":
            rule["duty"] += " " + phrase
        elif scenario == "allow_rule":
            rule["duty"] += " " + allowed
        elif scenario == "combined_source":
            unit["text"] += " " + phrase
            rule["citation"] += " " + phrase
            rule["duty"] += " " + allowed + " " + phrase
        package = {"rules": [rule]}
        original = deepcopy((unit, package))
        captured, stage_policies = [], []
        with offline():
            build = importlib.import_module("campaign_tool.digest.build")
            from campaign_tool.digest.model import ModelConfig
            from campaign_tool.digest.model_input import ModelSources
            from campaign_tool.digest.schema import validate_digest, validate_model_output
            real_detectors, real_redact = build.run_detectors, build.redact_units

            def detectors(units, supplied_package):
                hits = real_detectors(units, supplied_package)
                if scenario == "excerpt":
                    hits[0]["excerpt"] += " " + phrase
                return hits

            def first_stage(units, **kwargs):
                stage_policies.append((kwargs["allowlist"], kwargs["denylist"]))
                return real_redact(units, **kwargs)

            def narrative(config, units, hits, rules, opener=None, *, allowlist=(), denylist=()):
                self.assertIsNone(opener)
                self.assertEqual(rules, [rule])
                stage_policies.append((allowlist, denylist))
                sources = ModelSources(units, allowlist=allowlist, denylist=denylist)
                payload = sources.primary_payload(hits, rules)
                captured.append(payload)
                output = {"scope": "Fictional offline digest", "actors": [], "dates": [],
                          "statements": [{"text": "Records described.", "locator": {"unit": 0}}],
                          "omissions": [], "counterevidence": [],
                          "conclusions": [{"text": "A fictional duty needs review.", "confidence": "verified",
                                           "sources": [{"locator": {"unit": 0}, "rule_id": rule["rule_id"]}]}]}
                return sources.restore_output(validate_model_output(output))

            fn = builder or build.build_digest
            kwargs = {} if defaults else {"allowlist": represent(allow, allow_kind),
                                          "denylist": represent(deny, deny_kind)}
            if policies is not None:
                kwargs = {"allowlist": policies[0], "denylist": policies[1]}
            config = None if detector_only else ModelConfig("https://model.example.invalid/v1", model_id="synthetic-double")
            # patch.dict also supports a source-derived isolated mutant's globals.
            with mock.patch.dict(fn.__globals__, {"narrative": narrative, "run_detectors": detectors,
                                                  "redact_units": first_stage}):
                digest = fn("a" * 64, [unit], package, "us-ca", "fictional-v1", "redacted_cloud",
                            model_config=config, event_date=date.fromisoformat(data["event_date"]), **kwargs)
            self.assertIs(validate_digest(digest), digest)
        self.assertEqual((unit, package), original)
        expected_counts = {"EMAIL": 1}
        if scenario == "combined_source" and not defaults:
            expected_counts["DENYLIST"] = 1
        self.assertEqual(digest["redaction_counts"], expected_counts)
        self.assertEqual(digest["redaction_count"], sum(expected_counts.values()))
        self.assertEqual(digest["sha256"], "a" * 64)
        self.assertEqual(digest["jurisdiction"], "us-ca")
        self.assertEqual(digest["law_package_version"], "fictional-v1")
        self.assertEqual(digest["privacy_tier"], "redacted_cloud")
        self.assertEqual(digest["duties"], [{"rule_id": rule["rule_id"], "citation": rule["citation"],
                                           "locators": [unit["locator"]]}])
        self.assertTrue(digest["limitations"])
        self.assertTrue(digest["conclusions"])
        for conclusion in digest["conclusions"]:
            self.assertEqual(conclusion["confidence"], "needs_attorney_review")
            self.assertEqual(conclusion["sources"][0]["sha256"], "a" * 64)
            self.assertEqual(conclusion["sources"][0]["locator"], unit["locator"])
        if detector_only:
            self.assertEqual(captured, [])
            self.assertIsNone(digest["model_id"])
        else:
            self.assertEqual(len(captured), 1)
            self.assertEqual(digest["statements"][0]["locator"], unit["locator"])
            self.assertEqual(digest["model_id"], "synthetic-double")
            encoded = json.dumps(captured[0])
            self.assertNotIn("fictional-record.txt", encoded)
            self.assertNotIn(data["unit_email"], encoded)
        return digest, captured, stage_policies

    def check_policy(self, scenario, allow_kind, deny_kind, builder=None):
        digest, payloads, stages = self.invoke(scenario, allow_kind, deny_kind, builder=builder)
        data = fixture()
        encoded = json.dumps(payloads[0])
        if scenario != "allow_rule":
            self.assertNotIn(data["deny_phrase"], encoded, "second-stage deny phrase escaped")
        if scenario in ("allow_rule", "combined_source"):
            self.assertIn(data["allow_value"], payloads[0]["rules"][0]["duty"], "second-stage allow value lost")
        if scenario == "excerpt":
            self.assertIn("[DENYLIST-", payloads[0]["detector_hits"][0]["excerpt"])
        if scenario in ("citation", "duty"):
            self.assertNotIn(data["deny_phrase"], payloads[0]["record_text"][0])
            self.assertIn(data["deny_phrase"], self._rule_original(scenario, data))
        return digest, payloads, stages

    @staticmethod
    def _rule_original(scenario, data):
        return ("Veh. Code 2413 " if scenario == "citation" else "Maintain fictional records. ") + data["deny_phrase"]

    def test_consumed_once_and_shared_immutable_unmodified_snapshots(self):
        data = fixture()
        values = (["", data["allow_value"], "  Mixed Case  ", data["allow_value"]],
                  ["", data["deny_phrase"], "  literal  ", data["deny_phrase"]])
        policies = tuple(Once(value) for value in values)
        _, _, stages = self.invoke("combined_source", policies=policies)
        self.assertEqual(len(stages), 2)
        for index in (0, 1):
            self.assertEqual(policies[index].iterations, 1)
            self.assertEqual(policies[index].yielded, values[index])
            self.assertIsInstance(stages[0][index], tuple)
            self.assertEqual(stages[0][index], tuple(values[index]))
            self.assertIs(stages[0][index], stages[1][index])

    def test_repeated_fresh_equivalent_generators(self):
        first = self.check_policy("combined_source", "generator", "generator")[:2]
        self.assertEqual(first, self.check_policy("combined_source", "generator", "generator")[:2])
        self.assertEqual(first, self.check_policy("combined_source", "list", "list")[:2])

    def test_defaults_equal_explicit_empty(self):
        self.assertEqual(self.invoke("allow_rule", defaults=True)[:2],
                         self.invoke("allow_rule", policies=((), ()))[:2])

    def test_privacy_mismatch_precedes_redaction_and_narrative(self):
        with offline():
            build = importlib.import_module("campaign_tool.digest.build")
            from campaign_tool.digest.model import ModelConfig, ModelError
            with mock.patch.object(build, "redact_units") as redact, mock.patch.object(build, "narrative") as narrate:
                for tier, config_tier in (("strict_local", "redacted_cloud"), ("redacted_cloud", "strict_local")):
                    with self.assertRaisesRegex(ModelError, "model privacy tier does not match"):
                        build.build_digest("a" * 64, [], {"rules": []}, "us-ca", "fictional", tier,
                                           model_config=ModelConfig("https://model.example.invalid/v1", privacy_tier=config_tier))
                redact.assert_not_called()
                narrate.assert_not_called()

    def test_deciding_materialization_bypass_and_restoration(self):
        with offline():
            build = importlib.import_module("campaign_tool.digest.build")
            source = inspect.getsource(build.build_digest)
        lines = ("    allowlist = tuple(allowlist)\n", "    denylist = tuple(denylist)\n")
        for line in lines:
            self.assertEqual(source.count(line), 1, "candidate materialization missing")
        mutated = source
        for line in lines:
            mutated = mutated.replace(line, "", 1)
        original_ast, mutant_ast = ast.parse(source), ast.parse(mutated)
        original_ast.body[0].body = [node for node in original_ast.body[0].body
                                     if not (isinstance(node, ast.Assign) and
                                             isinstance(node.targets[0], ast.Name) and
                                             node.targets[0].id in ("allowlist", "denylist"))]
        self.assertEqual(ast.dump(original_ast), ast.dump(mutant_ast))
        namespace = dict(build.build_digest.__globals__)
        exec(compile(mutated, "<fc13-only-materialization-bypass>", "exec"), namespace)
        mutant = namespace["build_digest"]
        self.check_policy("duty", "list", "list", builder=mutant)
        with self.assertRaisesRegex(AssertionError, "second-stage deny phrase escaped"):
            self.check_policy("duty", "list", "generator", builder=mutant)
        # The genuine candidate is untouched. Restore and rerun both positives.
        self.check_policy("duty", "list", "list")
        self.check_policy("duty", "list", "generator")
        print("FC13_NEGATIVE_CONTROL: list mutant PASS; deny-generator mutant rejected by second-stage deny phrase escaped; restored list/generator PASS")


def _matrix(scenario, allow_kind, deny_kind):
    def test(self):
        actual = self.check_policy(scenario, allow_kind, deny_kind)
        reference = self.check_policy(scenario, "list", "list")
        self.assertEqual(actual[:2], reference[:2])
    return test


def _empty(allow_kind, deny_kind):
    def test(self):
        actual = self.invoke("allow_rule", policies=(represent([], allow_kind), represent([], deny_kind)))
        reference = self.invoke("allow_rule", defaults=True)
        self.assertEqual(actual[:2], reference[:2])
    return test


def _detector(allow_kind, deny_kind):
    def test(self):
        actual = self.invoke("combined_source", allow_kind, deny_kind, detector_only=True)
        reference = self.invoke("combined_source", "list", "list", detector_only=True)
        self.assertEqual(actual[:2], reference[:2])
    return test


for _scenario, _allow, _deny in itertools.product(SCENARIOS, KINDS, KINDS):
    setattr(RedactionIterableTests, f"test_matrix_{_scenario}_{_allow}_{_deny}", _matrix(_scenario, _allow, _deny))
for _allow, _deny in itertools.product(KINDS, KINDS):
    setattr(RedactionIterableTests, f"test_empty_{_allow}_{_deny}", _empty(_allow, _deny))
    setattr(RedactionIterableTests, f"test_detector_only_{_allow}_{_deny}", _detector(_allow, _deny))


if __name__ == "__main__":
    import argparse
    import os
    import sys
    import tempfile
    parser = argparse.ArgumentParser()
    parser.add_argument("--temporary-root", required=True, type=Path)
    args = parser.parse_args()
    if not args.temporary_root.is_absolute() or not args.temporary_root.is_dir():
        parser.error("temporary root must be an existing absolute directory")
    # Script setup only. Ordinary discovery never executes this block.
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(REPO))
    with mock.patch.dict(os.environ, {"TMPDIR": str(args.temporary_root), "TEMP": str(args.temporary_root),
                                     "TMP": str(args.temporary_root)}), mock.patch.object(tempfile, "tempdir", str(args.temporary_root)):
        result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(RedactionIterableTests))
    raise SystemExit(0 if result.wasSuccessful() and not result.skipped else 1)
