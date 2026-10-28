"""Finite synthetic volume oracle; imports/discovery have no runtime side effects.

Expected hit populations/counts live in the committed table, independently of
product grouping arithmetic. Same-day same-label fingerprint isolation uses an
explicit prepared-record seam because public rule selection correctly rejects
simultaneously applicable overlapping rules. Public calls also qualify each
fingerprint independently. No product code is edited by either synthetic seam.
"""
import copy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

DIMENSIONS = (
    "daily_threshold_triplets", "off_hours_threshold_triplets", "combined_OR_condition",
    "start_end_minute_edges", "local_midnight_split", "positive_negative_offset_controls",
    "offset_mismatch", "users_separate", "rule_fingerprint_separate",
    "duplicate_event_prints", "preserved_source_refs_and_triage_flags",
)
OBSERVATIONS = []
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "fc10-volume-boundary-oracle" / "cases.json"


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def material(case):
    original = hashlib.sha256(b"synthetic fc10 volume export").hexdigest()
    units = [{"original_sha256": original, "locator": {"row": i + 1},
              "event_id": event["id"], "event_at": event["at"], "record_type": "search-log",
              "fields": {"user_id": event["user"]}}
             for i, event in enumerate(case["events"])]
    for duplicate in case["copies"]:
        other = copy.deepcopy(units[duplicate["event"]])
        other["locator"] = {"row": duplicate["row"]}
        units.append(other)
    rules = []
    for slot in range(2):
        rules.append({"detector": "volume-anomaly", "agency_id": "synthetic-agency",
                      "rule_id": "same-label", "version": "same-version",
                      "source_sha256": hashlib.sha256(("synthetic policy " + str(slot)).encode()).hexdigest(),
                      "effective_from": "2020-01-01", "effective_to": "2021-01-01",
                      "parameters": {"timezone_offset_minutes": case["policy_offset"],
                                     "daily_count_threshold": case["daily_threshold"],
                                     "off_hours_threshold": case["off_threshold"], "business_hours": [9, 17]}})
    joins = [{"original_sha256": original, "status": "typed", "agency_id": "synthetic-agency"}]
    return units, joins, rules


class VolumeBoundaryOracle(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Only setUp executes imports/fixture reads; ordinary discovery stays inert.
        from campaign_tool.records.detectors import core
        cls.core = core
        cls.table = json.loads(FIXTURE.read_text())
        if (cls.table["schema"] != "fc10-volume-boundary-oracle/v1"
                or len(cls.table["cases"]) != 28
                or len({c["id"] for c in cls.table["cases"]}) != 28
                or {c["dimension"] for c in cls.table["cases"]} != set(DIMENSIONS)):
            raise ValueError("wrong or incomplete frozen fixture")

    def execute(self, case):
        units, joins, rules = material(case)
        before = copy.deepcopy((units, joins, rules))
        if case["mode"] == "evaluate":
            result = self.core.evaluate("volume-anomaly", units, joins, {"version": "synthetic-v1", "entries": [rules[0]]}, {})
        else:
            prepared, public_hits = [], []
            for slot in (0, 1):
                selected = [unit for unit, event in zip(units, case["events"]) if event["rule"] == slot]
                public = self.core.evaluate("volume-anomaly", selected, joins, {"version": "synthetic-v1", "entries": [rules[slot]]}, {})
                self.assertEqual(public["manifest"]["counts"]["blocked"], 0)
                self.assertEqual(public["manifest"]["counts"]["evaluated"], len(selected))
                public_hits.extend(public["hits"])
                for record in self.core._records(selected, joins):
                    prepared.append((record, rules[slot], datetime.fromisoformat(record["unit"]["event_at"])))
            hits = self.core._aggregate("volume-anomaly", prepared)
            # The seam must agree with the two public, unambiguous calls.
            self.assertEqual(sorted(hits, key=lambda h: h["dedupe_key"]),
                             sorted([{k: v for k, v in h.items() if k != "rules_version"} for h in public_hits], key=lambda h: h["dedupe_key"]))
            result = {"hits": hits, "prepared_public_hits": public_hits}
        self.assertEqual((units, joins, rules), before, "caller_inputs_preserved")
        return result

    def check_case(self, case, result):
        units, _, rules = material(case)
        hits = result["hits"]
        self.assertEqual(len(hits), len(case["expected_hits"]), "boundary_hit_count:" + case["id"])
        expected_by_key = {(h["user"], h["date"], fingerprint(rules[h["rule"]])): h for h in case["expected_hits"]}
        actual_by_key = {(h["observed_values_private"]["scope"][1], h["observed_values_private"]["scope"][2], h["rule_fingerprint_sha256"]): h for h in hits}
        self.assertEqual(set(actual_by_key), set(expected_by_key), "group_identity:" + case["id"])
        self.assertEqual(len(actual_by_key), len(hits), "no_duplicate_group_hits")
        for key, wanted in expected_by_key.items():
            hit = actual_by_key[key]
            self.assertEqual(hit["observed_values_private"], {
                "scope": ["user_day", wanted["user"], wanted["date"]],
                "unique_searches": wanted["unique"], "off_hours_searches": wanted["off"],
                "daily_threshold": case["daily_threshold"], "off_hours_threshold": case["off_threshold"]}, "exact_counts:" + case["id"])
            event_ids = {e["id"] for e in case["events"] if e["user"] == wanted["user"] and e["at"][:10] == wanted["date"] and e["rule"] == wanted["rule"]}
            refs = {json.dumps({"original_sha256": u["original_sha256"], "locator": u["locator"]}, sort_keys=True) for u in units if u["event_id"] in event_ids}
            self.assertEqual({json.dumps(r, sort_keys=True) for r in hit["evidence"]}, refs, "all_and_only_source_refs")
            self.assertEqual(len(hit["evidence"]), len(refs))
            self.assertIn({"original_sha256": hit["original_sha256"], "locator": hit["locator"]}, hit["evidence"])
            self.assertIs(hit["triage_only"], True)
            self.assertIs(hit["publication_ready"], False)
            self.assertEqual(hit["severity"], 1)
            self.assertEqual(hit["signal"], "search_volume_triage")
            self.assertEqual(hit["detector"], "volume-anomaly")
            self.assertIn("Triage signal only; not a legal conclusion or publication approval.", hit["limits"])
            self.assertEqual(hit["applicability"]["rule_source_sha256"], rules[wanted["rule"]]["source_sha256"])
        if case["mode"] == "evaluate":
            manifest = result["manifest"]
            self.assertEqual(manifest["counts"], {
                "evaluated": len(case["events"]) - len(case["blocked"]), "skipped": 0,
                "blocked": len(case["blocked"]), "input_rows": len(units),
                "unique_records": len(case["events"]), "eligible": len(case["events"]),
                "duplicate_rows": len(case["copies"]), "hits": len(case["expected_hits"])})
            expected_outcomes = {}
            for event in case["events"]:
                event_units = [u for u in units if u["event_id"] == event["id"]]
                identity = json.dumps(["synthetic-agency", "search-log", event["id"]], separators=(",", ":"))
                references = {json.dumps({"original_sha256": u["original_sha256"], "locator": u["locator"]}, sort_keys=True) for u in event_units}
                expected_outcomes[fingerprint(identity)] = (event, event_units, references)
            self.assertEqual({o["event_key"] for o in manifest["outcomes"]}, set(expected_outcomes))
            for outcome in manifest["outcomes"]:
                event, event_units, references = expected_outcomes[outcome["event_key"]]
                blocked = event["id"] in case["blocked"]
                self.assertEqual(outcome["disposition"], "blocked" if blocked else "evaluated")
                self.assertEqual(outcome["reason"], "event_policy_timezone_mismatch" if blocked else "supplied_context_evaluated")
                self.assertEqual(outcome["duplicate_rows"], len(event_units) - 1)
                self.assertEqual({json.dumps(r, sort_keys=True) for r in outcome["evidence"]}, references)
                self.assertEqual(len(outcome["evidence"]), len(references))
            self.assertEqual(sum(len(o["evidence"]) for o in manifest["outcomes"]),
                             len({(u["event_id"], json.dumps(u["locator"], sort_keys=True)) for u in units}))
            self.assertEqual(manifest["stage_promotions"], 0)
            self.assertEqual(manifest["acceptance"], "supplied_evidence_triage_only")
            self.assertIs(manifest["publication_ready"], False)

    def test_synthetic_end_inclusive_killed_and_restored(self):
        case = next(c for c in self.table["cases"] if c["id"] == "edge_1700")
        baseline = self.execute(case)
        self.check_case(case, baseline)
        original = self.core._aggregate

        def end_inclusive(detector, prepared):
            # Only the clock used by aggregation changes. Provenance, offset,
            # identity, rules, inputs and all other prerequisites remain valid.
            changed = [(r, policy, event.replace(hour=16) if event.hour == 17 else event)
                       for r, policy, event in prepared]
            return original(detector, changed)

        with patch.object(self.core, "_aggregate", end_inclusive):
            mutant = self.execute(case)
            self.assertEqual(mutant["manifest"]["counts"]["evaluated"], 1)
            self.assertEqual(mutant["manifest"]["counts"]["blocked"], 0)
            self.assertEqual(mutant["manifest"]["counts"]["skipped"], 0)
            with self.assertRaisesRegex(AssertionError, "boundary_hit_count:edge_1700") as killed:
                self.check_case(case, mutant)
        restored = self.execute(case)
        self.check_case(case, restored)
        self.assertEqual(restored, baseline)
        for control in ("edge_0900", "edge_1659", "daily_equal", "off_equal"):
            positive = next(c for c in self.table["cases"] if c["id"] == control)
            self.check_case(positive, self.execute(positive))
        OBSERVATIONS.append({"phase": "synthetic_end_inclusive", "case": case["id"],
                             "baseline": baseline, "injected": mutant, "assertion": str(killed.exception),
                             "restored": restored, "restored_controls": ["edge_0900", "edge_1659", "daily_equal", "off_equal"]})


def dimension_test(dimension):
    def test(self):
        cases = [c for c in self.table["cases"] if c["dimension"] == dimension]
        self.assertTrue(cases, "required_dimension_missing:" + dimension)
        for case in cases:
            with self.subTest(case=case["id"]):
                result = self.execute(case)
                self.check_case(case, result)
                self.assertEqual(result, self.execute(case), "repeatable")
                if case["copies"]:
                    no_copies = copy.deepcopy(case)
                    no_copies["copies"] = []
                    plain = self.execute(no_copies)
                    self.assertEqual([h["dedupe_key"] for h in result["hits"]], [h["dedupe_key"] for h in plain["hits"]], "duplicate_print_identity_invariant")
                OBSERVATIONS.append({"phase": "case", "case": case["id"], "dimension": dimension, "result": result})
    return test


for _dimension in DIMENSIONS:
    setattr(VolumeBoundaryOracle, "test_" + _dimension, dimension_test(_dimension))


def script_main():
    """Fresh child policy; never called during import or unittest discovery."""
    import argparse
    import contextlib
    import os
    import socket
    import subprocess
    import sys
    import tempfile
    parser = argparse.ArgumentParser()
    parser.add_argument("--temp-root", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--mode", choices=("suite", "compatibility", "discovery"), default="suite")
    args = parser.parse_args()
    root = args.temp_root.resolve(strict=True)
    tempfile.tempdir = str(root)
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    with contextlib.ExitStack() as guards:
        def offline(*unused, **kwargs):
            raise RuntimeError("offline_call_graph_violation")
        for target in ("socket.socket", "socket.create_connection", "socket.getaddrinfo", "subprocess.Popen", "os.system"):
            guards.enter_context(patch(target, offline))
        env_before, temp_before = dict(os.environ), tempfile.tempdir
        class Sentinel(unittest.TestCase):
            def runTest(self):
                self.assertEqual(dict(os.environ), env_before)
                self.assertEqual(tempfile.tempdir, temp_before)
                self.assertEqual(Path(tempfile.gettempdir()), root)
        loader = unittest.TestLoader()
        if args.mode == "compatibility":
            suite = loader.loadTestsFromName("tests.records.test_detectors")
        elif args.mode == "discovery":
            suite = unittest.TestSuite([Sentinel(), loader.discover(str(Path(__file__).parent), pattern=Path(__file__).name, top_level_dir=str(Path(__file__).resolve().parents[2])), Sentinel()])
        else:
            suite = loader.loadTestsFromTestCase(VolumeBoundaryOracle)
        observations = (sys.modules["tests.records.test_fc10_volume_boundary_oracle"].OBSERVATIONS
                        if args.mode == "discovery" else OBSERVATIONS)
        def ids(s):
            return [i for child in s for i in (ids(child) if isinstance(child, unittest.TestSuite) else [child.id()])]
        test_ids = ids(suite)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        report = {"mode": args.mode, "test_ids": test_ids, "tests_run": result.testsRun,
                  "failures": [(t.id(), text) for t, text in result.failures],
                  "errors": [(t.id(), text) for t, text in result.errors], "skips": result.skipped,
                  "observations": observations, "case_count": len([o for o in observations if o["phase"] == "case"]),
                  "dimensions": sorted({o["dimension"] for o in observations if o["phase"] == "case"}),
                  "ambient_environment_unchanged": dict(os.environ) == env_before,
                  "temp_default_unchanged": tempfile.tempdir == temp_before}
        args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        return 0 if result.wasSuccessful() and not result.skipped and report["ambient_environment_unchanged"] and report["temp_default_unchanged"] else 1


if __name__ == "__main__":
    raise SystemExit(script_main())
