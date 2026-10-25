"""FC9-DOCUMENT-CANDIDATE-COUNTS: capped document candidates report every omission.

Resource-accounting regression suite driven through the production API
``document.analyze``. Every source unit is generated here from the synthetic
case specs in ``tests/fixtures/fc9-document-candidate-counts/cases.json``; no
real record, agency or person is described. Each generated unit contributes
exactly one candidate to exactly one family. Expected totals come from those
generated counts and the frozen cap values, never from analyze output. Page
metadata is held complete, so candidate truncation alone decides
``partial_understanding``. A capped result is still REVIEW REQUIRED:
``review_acceptance`` stays false and ``semantic_assessment`` NOT_ASSESSED.

Script mode (``python3 <this file> --temp-root DIR [unittest args]``) handles
the literal temporary root and the repository import path before any product
import. Under ordinary unittest discovery the module changes no ambient state.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "tests" / "fixtures" / "fc9-document-candidate-counts" / "cases.json"


def _script_bootstrap(argv: list[str]) -> list[str]:
    """Script mode only: apply --temp-root and the repo path before product imports."""
    rest = [argv[0]]
    index = 1
    while index < len(argv):
        if argv[index] == "--temp-root":
            if index + 1 >= len(argv):
                raise SystemExit("--temp-root requires a directory")
            root = Path(argv[index + 1]).resolve()
            root.mkdir(parents=True, exist_ok=True)
            tempfile.tempdir = str(root)
            index += 2
            continue
        rest.append(argv[index])
        index += 1
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    return rest


if __name__ == "__main__":
    _ARGV = _script_bootstrap(sys.argv)

from campaign_tool.records.understanding import document, evidence  # noqa: E402

SCHEMA = "fc9-document-candidate-counts-cases/v1"
CAP_NAMES = ("MAX_CLASSIFICATIONS", "MAX_ENTITIES", "MAX_DATES", "MAX_SUMMARY_FACTS",
             "MAX_KEY_GROUPS", "MAX_CROSS_RECORD_KEYS")
# Output total/omission key -> emitted list key.
FAMILIES = {"classifications": "classifications", "entity_candidates": "entity_candidates",
            "date_mentions": "date_mentions", "summary_facts": "summary_facts",
            "cross_record_key_candidates": "cross_record_key_candidates"}
OMISSION_KEYS = tuple(FAMILIES) + ("key_occurrences_not_tracked",)
DIMENSIONS = tuple(FAMILIES) + ("key_occurrences_not_tracked", "all")
BOUNDARIES = ("zero", "below", "at", "above")
COUNT_KEYS = ("classifications", "entity_candidates", "date_mentions", "summary_facts",
              "cross_record_keys", "single_original_keys", "repeat_last_single_key")
CASE_KEYS = {"id", "dimension", "boundary", "originals", "interleave", "counts"}
MAX_KEY_NUMBER = 999  # three-digit key identifiers keep every key line below 12 characters
NEGATIVE_CASE = "classifications_above"
NEGATIVE_FAMILY = "classifications"

ORIGINALS = sorted(hashlib.sha256(label).hexdigest() for label in
                   (b"fc9 synthetic original document A", b"fc9 synthetic original document B"))
FIRST, SECOND = ORIGINALS
_ASSERT = unittest.TestCase()
_ASSERT.maxDiff = 2000


def _no_duplicates(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("fixture_duplicate_key")
        out[key] = value
    return out


def expected_from_counts(counts: dict, caps: dict) -> dict:
    """Frozen arithmetic over generated counts and cap values (no analyze call)."""
    c = {k: counts.get(k, 0) for k in COUNT_KEYS}
    totals = {"classifications": c["classifications"], "entity_candidates": c["entity_candidates"],
              "date_mentions": c["date_mentions"], "summary_facts": c["summary_facts"],
              "cross_record_key_candidates": c["cross_record_keys"]}
    cap_of = {"classifications": "MAX_CLASSIFICATIONS", "entity_candidates": "MAX_ENTITIES",
              "date_mentions": "MAX_DATES", "summary_facts": "MAX_SUMMARY_FACTS",
              "cross_record_key_candidates": "MAX_CROSS_RECORD_KEYS"}
    emitted = {f: min(n, caps[cap_of[f]]) for f, n in totals.items()}
    omissions = {f: totals[f] - emitted[f] for f in totals}
    groups = c["cross_record_keys"] + c["single_original_keys"]
    untracked_groups = max(0, groups - caps["MAX_KEY_GROUPS"])
    # Repeats belong to the last single key, which is untracked whenever any group is.
    omissions["key_occurrences_not_tracked"] = untracked_groups + (c["repeat_last_single_key"] if untracked_groups else 0)
    return {"candidate_totals": totals, "emitted_lengths": emitted, "candidate_omissions": omissions,
            "tracked_key_groups": min(groups, caps["MAX_KEY_GROUPS"]),
            "partial_understanding": any(omissions.values()), "gaps": [],
            "review_acceptance": False, "semantic_assessment": "NOT_ASSESSED"}


def validate_case(case, caps: dict) -> dict:
    """Reject malformed case specs with a specific code before generating anything."""
    if type(case) is not dict or set(case) - {"expect"} != CASE_KEYS or type(case.get("id")) is not str \
            or not case["id"] or type(case.get("counts")) is not dict or type(case.get("interleave")) is not bool:
        raise ValueError("case_shape_invalid")
    if case["dimension"] not in DIMENSIONS:
        raise ValueError("case_dimension_unknown")
    if case["boundary"] not in BOUNDARIES:
        raise ValueError("case_boundary_unknown")
    counts = case["counts"]
    if set(counts) - set(COUNT_KEYS):
        raise ValueError("case_count_unknown")
    if not all(type(v) is int and v >= 0 for v in counts.values()):
        raise ValueError("case_count_invalid")
    c = {k: counts.get(k, 0) for k in COUNT_KEYS}
    needs_two = case["dimension"] == "cross_record_key_candidates" or c["cross_record_keys"] > 0
    if type(case["originals"]) is not int or case["originals"] != (2 if needs_two else 1):
        raise ValueError("case_originals_invalid")
    if c["cross_record_keys"] + c["single_original_keys"] > MAX_KEY_NUMBER:
        raise ValueError("case_key_space_exhausted")
    if c["cross_record_keys"] > caps["MAX_KEY_GROUPS"]:
        raise ValueError("case_cross_keys_untrackable")
    if c["repeat_last_single_key"] and not c["single_original_keys"]:
        raise ValueError("case_repeat_without_key")
    if c["entity_candidates"] > MAX_KEY_NUMBER:
        raise ValueError("case_count_invalid")
    return case


def load_fixture(path: Path = FIXTURE) -> dict:
    doc = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_no_duplicates)
    if type(doc) is not dict or doc.get("schema") != SCHEMA or doc.get("synthetic") is not True:
        raise ValueError("fixture_schema_invalid")
    caps = doc.get("frozen_caps")
    if type(caps) is not dict or set(caps) != set(CAP_NAMES) or not all(type(v) is int and v > 0 for v in caps.values()):
        raise ValueError("fixture_caps_invalid")
    ids = set()
    for case in doc["cases"]:
        validate_case(case, caps)
        if case["id"] in ids:
            raise ValueError("fixture_duplicate_case")
        ids.add(case["id"])
    return doc


FIXTURE_DOC = load_fixture()
CAPS = FIXTURE_DOC["frozen_caps"]
CASES = {case["id"]: case for case in FIXTURE_DOC["cases"]}


# ---------------------------------------------------------------- generation

def _unit_raw(text: str) -> dict:
    return {"kind": "text", "locator": {"page": 1}, "text": text, "data": {}}


def _short(text: str) -> str:
    # Lines of 12+ characters would also be summary-fact candidates.
    if len(text) >= 12 or "\n" in text:
        raise ValueError("case_text_ambiguous")
    return text


def generate(case: dict) -> tuple[dict, dict]:
    """Build a synthetic bundle and the input-side plan of every generated candidate."""
    c = {k: case["counts"].get(k, 0) for k in COUNT_KEYS}
    lists = {
        "classifications": [("classifications", _short("policy"), "policy") for _ in range(c["classifications"])],
        "entity_candidates": [("entity_candidates", _short(f"vendor:E{i}"), f"E{i}")
                              for i in range(1, c["entity_candidates"] + 1)],
        "date_mentions": [("date_mentions", d, d) for d in
                          ((date(2030, 1, 1) + timedelta(days=i)).isoformat() for i in range(c["date_mentions"]))],
        "summary_facts": [("summary_facts", f"Synthetic fact {i:04d}", f"Synthetic fact {i:04d}")
                          for i in range(1, c["summary_facts"] + 1)],
    }
    if case["interleave"]:
        body, longest = [], max((len(v) for v in lists.values()), default=0)
        for i in range(longest):
            body += [lists[f][i] for f in lists if i < len(lists[f])]
    else:
        body = [item for f in lists for item in lists[f]]
    cross = [f"{k:03d}" for k in range(1, c["cross_record_keys"] + 1)]
    single = [f"{k:03d}" for k in range(c["cross_record_keys"] + 1, c["cross_record_keys"] + c["single_original_keys"] + 1)]
    keys_first = cross + single + ([single[-1]] * c["repeat_last_single_key"] if single else [])
    body += [("key", _short(f"request:{k}"), k) for k in keys_first]
    per_original = {FIRST: [("filler", "x", None)] + body}
    if case["originals"] == 2:
        per_original[SECOND] = [("filler", "x", None)] + [("key", _short(f"request:{k}"), k) for k in cross]
    units, contexts, receipts, tags = [], {}, [], {}
    for original, items in per_original.items():
        receipt_sha = hashlib.sha256(b"fc9 synthetic receipt " + original.encode()).hexdigest()
        for ordinal, (family, text, quote) in enumerate(items, 1):
            units.append(evidence.unit(_unit_raw(text), original, receipt_sha, ordinal))
            tags[(original, ordinal)] = (family, quote)
        contexts[original] = {"format": "pdf", "native_status": "complete", "pages": [{"page": 1}],
                              "declared_page_count": 1, "declared_unit_count": len(items),
                              "normalized_unit_count": len(items), "normalization_gaps": [], "quality_gaps": [],
                              "review_acceptance": False, "semantic_acceptance": False}
        receipts.append({"original_sha256": original, "receipt_sha256": receipt_sha, "attempt_kind": "native",
                         "declared_unit_count": len(items), "normalized_unit_count": len(items)})
    bundle = evidence.bundle(units, contexts, receipts)
    # Input-side plan in bundle order (the order analyze scans).
    plan = {f: [] for f in ("classifications", "entity_candidates", "date_mentions", "summary_facts")}
    key_units: dict[str, list] = {}
    for unit in bundle["units"]:
        family, quote = tags[(unit["original_sha256"], unit["ordinal"])]
        if family in plan:
            plan[family].append((unit["unit_id"], quote))
        elif family == "key":
            key_units.setdefault(quote, []).append(unit)
    plan["cross_record_key_candidates"] = [(k, key_units[k][0], next(u for u in key_units[k] if u["original_sha256"] == SECOND))
                                           for k in cross]
    plan["originals"] = sorted(per_original)
    return bundle, plan


# ---------------------------------------------------------------- independent assertions

def check_conservation(result: dict) -> None:
    """Every family: total == emitted + omitted, with no missing or negative counter."""
    totals, omissions = result["candidate_totals"], result["candidate_omissions"]
    _ASSERT.assertEqual(set(totals), set(FAMILIES))
    _ASSERT.assertEqual(set(omissions), set(OMISSION_KEYS))
    for family, list_key in FAMILIES.items():
        _ASSERT.assertIs(type(omissions[family]), int)
        _ASSERT.assertGreaterEqual(omissions[family], 0)
        _ASSERT.assertEqual(totals[family], len(result[list_key]) + omissions[family],
                            f"conservation broken for {family}")
    _ASSERT.assertGreaterEqual(omissions["key_occurrences_not_tracked"], 0)


def check_partial(result: dict) -> None:
    """partial_understanding must follow any omission (or gap) and never claim completeness."""
    expected = any(result["candidate_omissions"].values()) or bool(result["gaps"])
    _ASSERT.assertIs(result["partial_understanding"], expected,
                     "partial_understanding disagrees with reported omissions")
    _ASSERT.assertIs(result["review_acceptance"], False)
    _ASSERT.assertEqual(result["semantic_assessment"], "NOT_ASSESSED")


def check_citation(cite: dict, unit_id: str, quote: str) -> None:
    _ASSERT.assertEqual(cite["unit_id"], unit_id)
    _ASSERT.assertEqual(cite["quote"], quote)
    _ASSERT.assertEqual(cite["span"]["end_byte"] - cite["span"]["start_byte"], len(quote.encode("utf-8")))


def check_case(case: dict, result: dict, plan: dict) -> None:
    exp = case["expect"]
    _ASSERT.assertEqual(result["candidate_totals"], exp["candidate_totals"])
    _ASSERT.assertEqual(result["candidate_omissions"], exp["candidate_omissions"])
    _ASSERT.assertEqual({f: len(result[k]) for f, k in FAMILIES.items()}, exp["emitted_lengths"])
    _ASSERT.assertIs(result["partial_understanding"], exp["partial_understanding"])
    _ASSERT.assertEqual(result["gaps"], exp["gaps"])
    _ASSERT.assertIs(result["review_acceptance"], False)
    _ASSERT.assertEqual(result["semantic_assessment"], "NOT_ASSESSED")
    check_conservation(result)
    check_partial(result)
    # Metadata held clear: every original has a complete one-page inventory.
    _ASSERT.assertEqual([row["original_sha256"] for row in result["coverage"]], plan["originals"])
    for row in result["coverage"]:
        _ASSERT.assertEqual(row["page_inventory"]["status"], "complete")
        _ASSERT.assertEqual(row["quality_gaps"], [])
    # Exact cited units: the first emitted generated candidates, in scan order.
    for family in ("classifications", "entity_candidates", "date_mentions", "summary_facts"):
        emitted = exp["emitted_lengths"][family]
        _ASSERT.assertEqual([item["unit_id"] for item in result[family]], [u for u, _ in plan[family][:emitted]])
    for item, (unit_id, quote) in zip(result["classifications"], plan["classifications"]):
        _ASSERT.assertEqual(item["candidate"], "policy")
        _ASSERT.assertEqual(item["semantic_assessment"], "NOT_ASSESSED")
        check_citation(item["citation"], unit_id, quote)
    for item, (unit_id, quote) in zip(result["entity_candidates"], plan["entity_candidates"]):
        _ASSERT.assertEqual((item["type"], item["value"]), ("vendor", quote))
        _ASSERT.assertEqual(item["semantic_assessment"], "NOT_ASSESSED")
        check_citation(item["citation"], unit_id, quote)
    for item, (unit_id, quote) in zip(result["date_mentions"], plan["date_mentions"]):
        _ASSERT.assertEqual((item["raw"], item["value"], item["time_basis"]), (quote, quote, "local_date_only"))
        _ASSERT.assertEqual(item["semantic_assessment"], "NOT_ASSESSED")
        check_citation(item["citation"], unit_id, quote)
    for item, (unit_id, quote) in zip(result["summary_facts"], plan["summary_facts"]):
        claim = item["claim"]
        _ASSERT.assertEqual(item["kind"], "verbatim_summary_candidate")
        _ASSERT.assertEqual(claim["claim"], "Source text states: " + quote)
        _ASSERT.assertEqual((claim["review_acceptance"], claim["semantic_assessment"]), (False, "NOT_ASSESSED"))
        _ASSERT.assertEqual(len(claim["supports"]), 1)
        check_citation(claim["supports"][0], unit_id, quote)
    emitted = exp["emitted_lengths"]["cross_record_key_candidates"]
    expected_keys = plan["cross_record_key_candidates"][:emitted]
    _ASSERT.assertEqual([item["key_candidate"] for item in result["cross_record_key_candidates"]],
                        [k for k, _, _ in expected_keys])
    for item, (key, first, second) in zip(result["cross_record_key_candidates"], expected_keys):
        _ASSERT.assertEqual((item["occurrence_count"], item["omitted_occurrence_count"]), (2, 0))
        _ASSERT.assertEqual((item["distinct_originals"], item["semantic_assessment"]), ("at_least_two", "NOT_ASSESSED"))
        _ASSERT.assertEqual([(o["unit_id"], o["original_sha256"], o["key_candidate"]) for o in item["occurrences"]],
                            [(first["unit_id"], FIRST, key), (second["unit_id"], SECOND, key)])
        for occurrence, unit in zip(item["occurrences"], (first, second)):
            check_citation(occurrence["citation"], unit["unit_id"], key)


def analyze_case(case_id: str) -> tuple[dict, dict, dict]:
    case = CASES[case_id]
    bundle, plan = generate(case)
    return case, document.analyze(bundle), plan


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode("utf-8")).hexdigest()


def run_negative_control() -> dict:
    """Declared synthetic result-corruption control (not a measured product bug).

    The test-local copy of one over-cap family's returned omission is zeroed
    while its emitted list and total stay unchanged. The independent
    conservation and partial_understanding checks must each reject exactly
    that mutant; the untouched baseline result must pass before and after.
    """
    case, result, plan = analyze_case(NEGATIVE_CASE)
    before = _digest(result)
    baseline = {"candidate_total": result["candidate_totals"][NEGATIVE_FAMILY],
                "emitted_length": len(result[NEGATIVE_FAMILY]),
                "candidate_omission": result["candidate_omissions"][NEGATIVE_FAMILY],
                "partial_understanding": result["partial_understanding"],
                "result_sha256": before}
    check_case(case, result, plan)
    mutant = copy.deepcopy(result)
    mutant["candidate_omissions"][NEGATIVE_FAMILY] = 0
    injected = {"label": "INJECTED synthetic result corruption (test-local copy only)",
                "case": NEGATIVE_CASE, "field": f"candidate_omissions.{NEGATIVE_FAMILY}",
                "from": baseline["candidate_omission"], "to": 0,
                "unchanged": ["candidate_totals", NEGATIVE_FAMILY]}
    differences = sorted(k for k in result if result[k] != mutant[k])
    verdicts = {}
    for name, check in (("conservation", check_conservation), ("partial_understanding", check_partial)):
        try:
            check(mutant)
        except AssertionError as exc:
            verdicts[name] = {"rejected": True, "assertion": str(exc).splitlines()[0][:300]}
        else:
            verdicts[name] = {"rejected": False}
    check_case(case, result, plan)
    final = {"label": "FINAL candidate output (unpatched analyze result)",
             "result_sha256": _digest(result), "unchanged_by_control": _digest(result) == before,
             "candidate_totals": result["candidate_totals"], "candidate_omissions": result["candidate_omissions"],
             "partial_understanding": result["partial_understanding"], "checks_passed": True}
    return {"injected_behavior": injected, "baseline_observation": baseline,
            "mutant_top_level_differences": differences,
            "mutant_emitted_unchanged": mutant[NEGATIVE_FAMILY] == result[NEGATIVE_FAMILY],
            "mutant_total_unchanged": mutant["candidate_totals"] == result["candidate_totals"],
            "mutant_verdicts": verdicts, "final_candidate_output": final}


# ---------------------------------------------------------------- tests

class FC9FixtureContractTests(unittest.TestCase):
    def test_frozen_caps_match_product_constants(self):
        self.assertEqual({name: getattr(document, name) for name in CAP_NAMES}, CAPS)

    def test_fixture_expectations_are_frozen_count_arithmetic(self):
        for case_id, case in CASES.items():
            with self.subTest(case=case_id):
                self.assertEqual(case["expect"], expected_from_counts(case["counts"], CAPS))

    def test_fixture_covers_every_cap_boundary(self):
        caps = {"classifications": "MAX_CLASSIFICATIONS", "entity_candidates": "MAX_ENTITIES",
                "date_mentions": "MAX_DATES", "summary_facts": "MAX_SUMMARY_FACTS",
                "cross_record_key_candidates": "MAX_CROSS_RECORD_KEYS"}
        for family, cap_name in caps.items():
            cap = CAPS[cap_name]
            for boundary, total in (("zero", 0), ("below", cap - 1), ("at", cap), ("above", cap + 1)):
                with self.subTest(family=family, boundary=boundary):
                    case = CASES[f"{family}_{boundary}"]
                    self.assertEqual((case["dimension"], case["boundary"]), (family, boundary))
                    self.assertEqual(case["expect"]["candidate_totals"][family], total)
                    others = {k: v for k, v in case["expect"]["candidate_totals"].items() if k != family}
                    self.assertEqual(set(others.values()), {0})
        self.assertEqual(CASES["key_groups_at"]["counts"], {"single_original_keys": CAPS["MAX_KEY_GROUPS"]})
        self.assertEqual(CASES["key_groups_above"]["counts"], {"single_original_keys": CAPS["MAX_KEY_GROUPS"] + 1})
        self.assertEqual(CASES["summary_facts_cap_amid_other_candidates"]["expect"]["candidate_omissions"],
                         {**{k: 0 for k in OMISSION_KEYS}, "summary_facts": 1})
        all_over = CASES["all_families_above_interleaved"]["expect"]["candidate_omissions"]
        self.assertTrue(all(all_over[k] > 0 for k in OMISSION_KEYS), all_over)

    def test_malformed_specs_are_rejected(self):
        probes = FIXTURE_DOC["malformed_specs"]
        self.assertGreaterEqual(len(probes), 8)
        for probe in probes:
            with self.subTest(probe=probe["id"]):
                with self.assertRaises(ValueError) as caught:
                    validate_case(probe["case"], CAPS)
                self.assertEqual(str(caught.exception), probe["error"])

    def test_malformed_fixture_documents_are_rejected(self):
        with tempfile.TemporaryDirectory(prefix="fc9-fixture-probe-") as tmp:
            for name, text, code in (("duplicate_key", '{"schema": "a", "schema": "b"}', "fixture_duplicate_key"),
                                     ("wrong_schema", json.dumps({"schema": "other", "synthetic": True}),
                                      "fixture_schema_invalid"),
                                     ("changed_caps", json.dumps({"schema": SCHEMA, "synthetic": True,
                                                                  "frozen_caps": {"MAX_DATES": 1}, "cases": []}),
                                      "fixture_caps_invalid"),
                                     ("duplicate_case", json.dumps({**FIXTURE_DOC, "cases": [CASES["classifications_zero"]] * 2}),
                                      "fixture_duplicate_case")):
                with self.subTest(probe=name):
                    path = Path(tmp) / f"{name}.json"
                    path.write_text(text, encoding="utf-8")
                    with self.assertRaises(ValueError) as caught:
                        load_fixture(path)
                    self.assertEqual(str(caught.exception), code)

    def test_each_generated_unit_feeds_exactly_one_family(self):
        probe = {"id": "probe", "dimension": "all", "boundary": "at", "originals": 2, "interleave": False,
                 "counts": {"classifications": 1, "entity_candidates": 1, "date_mentions": 1, "summary_facts": 1,
                            "cross_record_keys": 1, "single_original_keys": 1}}
        validate_case(probe, CAPS)
        bundle, plan = generate(probe)
        result = document.analyze(bundle)
        self.assertEqual(result["candidate_totals"], {f: 1 for f in FAMILIES})
        self.assertEqual(result["candidate_omissions"], {k: 0 for k in OMISSION_KEYS})
        self.assertIs(result["partial_understanding"], False)
        check_case({"expect": expected_from_counts(probe["counts"], CAPS)}, result, plan)

    def test_negative_control_rejects_zeroed_omissions(self):
        receipt = run_negative_control()
        self.assertEqual(receipt["injected_behavior"]["from"], 1)
        self.assertEqual(receipt["mutant_top_level_differences"], ["candidate_omissions"])
        self.assertTrue(receipt["mutant_emitted_unchanged"])
        self.assertTrue(receipt["mutant_total_unchanged"])
        self.assertEqual({k: v["rejected"] for k, v in receipt["mutant_verdicts"].items()},
                         {"conservation": True, "partial_understanding": True})
        self.assertTrue(receipt["final_candidate_output"]["unchanged_by_control"])
        self.assertIs(receipt["baseline_observation"]["partial_understanding"], True)


class FC9CandidateCountCaseTests(unittest.TestCase):
    """One named test per fixture case (generated below)."""


def _case_test(case_id: str):
    def test(self):
        case, result, plan = analyze_case(case_id)
        check_case(case, result, plan)
    test.__name__ = f"test_case_{case_id}"
    test.__doc__ = f"FC9 fixture case {case_id}"
    return test


for _case_id in CASES:
    setattr(FC9CandidateCountCaseTests, f"test_case_{_case_id}", _case_test(_case_id))


# ------------------------------------------------------------ runner stop-gate controls
# Inert controls for the acceptance runner: every child goes to a fake launcher
# (nothing is executed), phases are marker callbacks, and all receipts land in a
# per-test temporary directory. No real guard refusal is attempted.

RUNNER_PATH = REPO / "tools" / "fc9_document_candidate_counts_acceptance.py"


def load_runner():
    spec = importlib.util.spec_from_file_location("fc9_candidate_counts_runner_under_test", RUNNER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeLauncher:
    """Stands in for subprocess.run; ``respond(argv)`` returns (exit, stdout) or an exception."""

    def __init__(self, respond):
        self.respond = respond
        self.calls: list[list[str]] = []

    def __call__(self, argv, cwd, stdin, stdout, stderr, timeout, check, shell):
        assert shell is False and isinstance(argv, list)
        self.calls.append(list(argv))
        outcome = self.respond(argv)
        if isinstance(outcome, BaseException):
            raise outcome
        code, text = outcome
        stdout.write(text.encode("utf-8"))
        return subprocess.CompletedProcess(argv, code)


def _ok(argv):
    return 0, ""


class FC9RunnerStopGateTests(unittest.TestCase):
    """Prerequisite failures and terminal child outcomes stop all later work."""

    def setUp(self):
        self.runner = load_runner()
        holder = tempfile.TemporaryDirectory(prefix="fc9-runner-control-")
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name)
        self.entered: list[str] = []

    def make_run(self, respond=_ok, name="run"):
        run_dir = self.tmp / name
        run_dir.mkdir()
        launcher = FakeLauncher(respond)
        return self.runner.Run(run_dir, launcher=launcher), launcher

    def marker_phases(self, also=None):
        def marker(run, facts, before):
            self.entered.append("phase")
            if also:
                also(run)
            return {"marker": True}
        return {phase: marker for phase in self.runner.PHASES}

    def manifest(self, **overrides) -> Path:
        value = {"schema": self.runner.MANIFEST_SCHEMA, "repo": str(self.tmp / "not-the-candidate"),
                 "write_root": str(self.tmp), "head": "0" * 40, "tree": "1" * 40,
                 "runtime": {"repo_venv": str(self.tmp / "venv")}}
        value.update(overrides)
        path = self.tmp / "manifest.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_invalid_manifest_stops_before_any_child_or_phase(self):
        path = self.manifest()
        for phase in self.runner.PHASES:
            with self.subTest(phase=phase):
                run, launcher = self.make_run(name=f"invalid-{phase}")
                record = self.runner.execute(run, phase, path, {}, phases=self.marker_phases())
                self.assertEqual(self.entered, [])
                self.assertEqual(launcher.calls, [])
                self.assertFalse(record["passed"])
                self.assertFalse(record["phase_entered"])
                self.assertEqual(record["stopped"]["stage"], "manifest-static")
                self.assertIn("manifest repo is not this candidate", record["problems"])
                self.assertTrue((run.run_dir / "result.json").is_file())

    def test_unreadable_manifest_stops_before_any_child_or_phase(self):
        for label, content in (("not-json", "{"), ("not-object", "[]")):
            with self.subTest(label=label):
                path = self.tmp / f"{label}.json"
                path.write_text(content, encoding="utf-8")
                run, launcher = self.make_run(name=label)
                record = self.runner.execute(run, "suite", path, {}, phases=self.marker_phases())
                self.assertEqual((self.entered, launcher.calls), ([], []))
                self.assertEqual(record["stopped"]["stage"], "manifest-static")
                self.assertFalse(record["passed"])
        run, launcher = self.make_run(name="missing")
        record = self.runner.execute(run, "suite", self.tmp / "absent.json", {}, phases=self.marker_phases())
        self.assertEqual((self.entered, launcher.calls, record["passed"]), ([], [], False))

    def test_git_prerequisite_failure_stops_before_phase(self):
        base, base_tree, head = "2" * 40, "3" * 40, "4" * 40

        def respond(argv):
            if argv[:2] == ["git", "rev-parse"]:
                return 0, (base_tree if argv[2].endswith("^{tree}") else head) + "\n"
            if argv[:3] == ["git", "merge-base", "--is-ancestor"]:
                return 1, ""  # ordinary nonzero: HEAD does not descend from base
            return 0, ""

        def check(run, manifest_path, phase):
            run.manifest_facts.update(base=base, base_tree=base_tree)
            return self.runner.check_manifest_git(run, run.manifest_facts)

        run, launcher = self.make_run(respond)
        record = self.runner.execute(run, "public-tree", self.tmp / "unused.json", {},
                                     phases=self.marker_phases(), check=check)
        self.assertEqual(self.entered, [])
        self.assertEqual(record["stopped"]["stage"], "manifest-git")
        self.assertIn("candidate HEAD does not descend from the pinned base", record["problems"])
        names = [c["name"] for c in record["children"]]
        self.assertFalse(any(n.startswith("before-") for n in names), names)
        self.assertEqual(len(launcher.calls), len(names))

    def test_launch_error_is_terminal_and_refuses_later_children(self):
        run, launcher = self.make_run(lambda argv: PermissionError(13, "refused by guard (inert mock)"))
        with self.assertRaises(self.runner.Halt):
            run.child("first", ["git", "status"], self.tmp)
        record = run.children[0]
        self.assertEqual((record["argv"], record["cwd"], record["exit"]), (["git", "status"], str(self.tmp), None))
        self.assertIn("PermissionError", record["launch_error"])
        self.assertEqual(record["terminal"], "launch_error")
        self.assertEqual(record["run_dir"], str(run.run_dir))
        self.assertTrue(run.problems and run.problems[0].startswith("STOP launch_error"))
        with self.assertRaises(self.runner.Halt):
            run.child("second", ["git", "log"], self.tmp)
        self.assertEqual(len(launcher.calls), 1)
        self.assertEqual(len(run.children), 1)

    def test_timeout_and_signal_and_exec_refusal_are_terminal(self):
        outcomes = {"timeout": subprocess.TimeoutExpired(["git"], 1), "killed_by_signal": (-9, ""),
                    "exec_refused_or_missing": (126, ""), "launch_error": FileNotFoundError(2, "missing")}
        for label, outcome in outcomes.items():
            with self.subTest(label=label):
                run, launcher = self.make_run(lambda argv, outcome=outcome: outcome, name=label)
                with self.assertRaises(self.runner.Halt):
                    run.child("only", ["git", "status"], self.tmp)
                self.assertEqual(run.terminal["terminal"], label)
                self.assertEqual(len(run.problems), 1)
                with self.assertRaises(self.runner.Halt):
                    run.child("later", ["git", "status"], self.tmp)
                self.assertEqual(len(launcher.calls), 1)

    def test_ordinary_nonzero_exit_is_not_terminal(self):
        run, launcher = self.make_run(lambda argv: (1, "findings"))
        first = run.child("informational", ["git", "diff", "--quiet"], self.tmp)
        second = run.child("next", ["git", "status"], self.tmp)
        self.assertEqual((first["exit"], second["exit"]), (1, 1))
        self.assertNotIn("terminal", first)
        self.assertIsNone(run.terminal)
        self.assertEqual(run.problems, [])
        self.assertEqual(len(launcher.calls), 2)

    def test_terminal_child_in_phase_finalizes_without_more_children(self):
        def respond(argv):
            if argv[0] == "refused-tool":
                return PermissionError(13, "refused (inert mock)")
            return 0, ""

        def inside(run):
            run.child("phase-child", ["refused-tool", "--flag"], self.tmp)
            self.entered.append("after-terminal")  # must never run
            run.child("phase-child-2", ["git", "status"], self.tmp)

        run, launcher = self.make_run(respond)
        record = self.runner.execute(run, "suite", self.tmp / "unused.json", {},
                                     phases=self.marker_phases(also=inside), check=lambda r, p, ph: {})
        self.assertEqual(self.entered, ["phase"])
        self.assertEqual(launcher.calls[-1], ["refused-tool", "--flag"])
        names = [c["name"] for c in record["children"]]
        self.assertEqual(names[-1], "phase-child")
        self.assertFalse(any(n.startswith("after-") for n in names), names)
        self.assertNotIn("identity_after", record)
        self.assertEqual(record["stopped"]["terminal_child"]["name"], "phase-child")
        self.assertEqual(record["stopped"]["terminal_child"]["argv"], ["refused-tool", "--flag"])
        self.assertFalse(record["passed"])
        saved = json.loads((run.run_dir / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["stopped"]["stage"], "child:phase-child")

    def test_public_tree_stops_dependent_clone_checkout_and_scans(self):
        facts = {"runtime": {"repo_venv": str(self.tmp / "venv")}}
        clean = {"head": "5" * 40, "tree": "6" * 40, "status_porcelain": ""}
        failing = {  # label: (before, (git verb, exit) to fail, gate stage, last child launched)
            "dirty-candidate": (dict(clean, status_porcelain=" M x\n"), None, "public-tree-identity", None),
            "untracked-authored": (clean, ("ls-files", 1), "public-tree-committed", "tracked-inventory"),
            "clone-failed": (clean, ("clone", 128), "public-tree-clone", "clone"),
            "checkout-failed": (clean, ("checkout", 1), "public-tree-checkout", "clone-checkout"),
        }
        downstream = ("clone", "clone-checkout", "clone-head", "clone-tree", "clone-check-public-tree",
                      "inplace-check-public-tree", "source-head-recheck", "source-status-recheck")
        for label, (before, fail, stage, last) in failing.items():
            with self.subTest(label=label):
                def respond(argv, fail=fail):
                    if fail and argv[0] == "git" and argv[1] == fail[0]:
                        return fail[1], ""
                    return 0, ""
                run, launcher = self.make_run(respond, name=label)
                with self.assertRaises(self.runner.Halt):
                    self.runner.phase_public_tree(run, facts, before)
                self.assertEqual(run.stopped_at, stage)
                names = [c["name"] for c in run.children]
                self.assertEqual(names[-1] if names else None, last)
                cut = downstream.index(last) + 1 if last in downstream else 0
                for forbidden in downstream[cut:]:
                    self.assertNotIn(forbidden, names)
                self.assertEqual(len(launcher.calls), len(names))

    def test_credentials_scanner_failure_stops_checker(self):
        facts = {"runtime": {"detect_secrets": sys.executable}}
        for label, outcome in (("scanner-exit", (2, "{\"results\": {}}")), ("scanner-garbage", (0, "not json"))):
            with self.subTest(label=label):
                run, launcher = self.make_run(lambda argv, outcome=outcome: outcome, name=label)
                with self.assertRaises(self.runner.Halt):
                    self.runner.phase_credentials(run, facts, {})
                self.assertEqual(run.stopped_at, "credentials-scan")
                self.assertEqual([c["name"] for c in run.children], ["detect-secrets-scan"])
                self.assertEqual(len(launcher.calls), 1)
        run, launcher = self.make_run(name="scanner-missing")
        with self.assertRaises(self.runner.Halt):
            self.runner.phase_credentials(run, {"runtime": {"detect_secrets": str(self.tmp / "absent")}}, {})
        self.assertEqual((run.stopped_at, launcher.calls), ("credentials-scanner-available", []))


if __name__ == "__main__":
    unittest.main(argv=_ARGV)
