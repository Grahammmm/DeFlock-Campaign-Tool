"""FC16-DIGEST-DURATION-SCALES: detect_retention unit conversions against a fixed scale table.

The oracle is the committed fixture's reference scale (hour=1/24, day=1, week=7,
month=30, year=365) evaluated with fractions.Fraction. These are frozen
reference conversions, not calendar or legal retention periods. Tests drive
only the public ``detect_retention`` function with synthetic single-line text.

Deciding negative controls build isolated in-memory copies (a mutated ``_days``
copy and a mutated ``detect_retention`` copy executed in a copied namespace);
the detectors module itself is never patched or written.

Script mode (portable acceptance): ``python3 <this file> --temp-root DIR
[--result-json FILE]``; the arguments are handled before product imports.
"""
from pathlib import Path
import sys
import tempfile

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "tests" / "fixtures" / "fc16_digest_duration_scales" / "cases.json"
_SCRIPT_OPTIONS = {}

if __name__ == "__main__":
    _args = sys.argv[1:]
    _rest = []
    while _args:
        _flag = _args.pop(0)
        if _flag in ("--temp-root", "--result-json") and _args:
            _SCRIPT_OPTIONS[_flag] = _args.pop(0)
        else:
            _rest.append(_flag)
    if "--temp-root" not in _SCRIPT_OPTIONS:
        raise SystemExit("--temp-root is required in script mode")
    _temp_root = Path(_SCRIPT_OPTIONS["--temp-root"]).absolute()
    _temp_root.mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = str(_temp_root)
    sys.argv[1:] = _rest
    sys.dont_write_bytecode = True
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))

import copy  # noqa: E402
from fractions import Fraction  # noqa: E402
import hashlib  # noqa: E402
import inspect  # noqa: E402
import json  # noqa: E402
import types  # noqa: E402
import unittest  # noqa: E402

from campaign_tool import law  # noqa: E402
from campaign_tool.digest import detectors  # noqa: E402
from campaign_tool.digest.detectors import detect_retention  # noqa: E402

CASES = json.loads(FIXTURE.read_text(encoding="utf-8"))
SCALE = {unit: Fraction(num, den) for unit, (num, den) in CASES["scale"].items()}
IDENTITY = CASES["hit_identity"]
REFERENCE = CASES["reference_days"]
PACKAGE = law.load_package("us-ca")


def oracle_days(value, unit):
    """Independent oracle: integer token times the frozen reference scale."""
    return Fraction(int(value)) * SCALE[unit]


def label(comparison):
    return IDENTITY["comparison_labels"][comparison]


def spell(word, case):
    if case == "lower":
        return word.lower()
    if case == "upper":
        return word.upper()
    if case == "mixed":
        return "".join(c.upper() if i % 2 == 0 else c.lower() for i, c in enumerate(word))
    raise ValueError(case)


def unit_table_cases():
    table = CASES["unit_table"]
    index = 0
    for unit, words in table["units"].items():
        for value in table["values"]:
            for form in table["forms"]:
                for case in table["cases"]:
                    index += 1
                    word = spell(words[form], case)
                    yield {"id": "%s_%s_%d_%s" % (unit, form, value, case), "unit": unit, "value": value,
                           "word": word, "locator": {"page": table["locator_page"], "line": index},
                           "text": table["line_template"].format(value=value, word=word)}


def threshold_case(case_id):
    for case in CASES["threshold"]["cases"]:
        if case["id"] == case_id:
            return case
    raise KeyError(case_id)


def threshold_unit(case):
    index = [c["id"] for c in CASES["threshold"]["cases"]].index(case["id"]) + 1
    return {"locator": {"page": CASES["threshold"]["locator_page"], "line": index},
            "text": CASES["threshold"]["line_template"].format(value=case["value"], word=case["word"])}


def source_sha256():
    return hashlib.sha256(Path(detectors.__file__).read_bytes()).hexdigest()


class Checks:
    """Shared assertions; every failure is an explicit unittest assertion."""

    def assert_days(self, produced, expected, unit, msg=None):
        if unit == "hour":
            # _days divides by 24 (a float); a correctly rounded quotient equals float(expected) exactly.
            self.assertIsInstance(produced, float, msg)
            self.assertEqual(produced, float(expected), msg)
            self.assertLessEqual(abs(Fraction(produced) - expected), Fraction(1, 2 ** 40), msg)
        else:
            self.assertIs(type(produced), int, msg)
            self.assertEqual(Fraction(produced), expected, msg)

    def assert_identity(self, hit, locator, line):
        self.assertEqual(sorted(hit), IDENTITY["hit_keys"])
        self.assertEqual(sorted(hit["detail"]), IDENTITY["detail_keys"])
        self.assertEqual(hit["detector"], IDENTITY["detector"])
        self.assertEqual(hit["detector_version"], IDENTITY["detector_version"])
        self.assertEqual(hit["kind"], IDENTITY["kind"])
        self.assertEqual(hit["rule_id"], IDENTITY["rule_id"])
        self.assertEqual(hit["citation"], IDENTITY["citation"])
        self.assertEqual(hit["detail"]["note"], IDENTITY["note"])
        self.assertEqual(hit["detail"]["reference_days"], REFERENCE)
        self.assertEqual(hit["locator"], locator)
        self.assertEqual(hit["excerpt"], line.strip())
        for forbidden in ("confidence", "finding", "conclusion", "legal_finding"):
            self.assertNotIn(forbidden, hit)
            self.assertNotIn(forbidden, hit["detail"])

    def assert_threshold(self, detect, case_id):
        case = threshold_case(case_id)
        unit = threshold_unit(case)
        expected = Fraction(*case["days"])
        self.assertEqual(oracle_days(case["value"], case["unit"]), expected, case_id + ":oracle")
        self.assertEqual(expected > REFERENCE, case["comparison"] == "exceeds", case_id + ":strict")
        hits = detect([unit], PACKAGE)
        self.assertEqual(len(hits), 1, case_id + ":hit_count")
        hit = hits[0]
        self.assert_identity(hit, unit["locator"], unit["text"])
        self.assertEqual(hit["detail"]["stated"], "%d %s" % (case["value"], case["word"]))
        self.assert_days(hit["detail"]["stated_days"], expected, case["unit"], case_id + ":days")
        self.assertEqual(hit["detail"]["comparison"], label(case["comparison"]), case_id + ":comparison")


class ScaleTableTests(unittest.TestCase):
    def test_scale_table_is_frozen(self):
        self.assertEqual(SCALE, {"hour": Fraction(1, 24), "day": Fraction(1), "week": Fraction(7),
                                 "month": Fraction(30), "year": Fraction(365)})
        self.assertEqual(REFERENCE, 60)
        self.assertEqual(sorted(CASES["unit_table"]["units"]), sorted(SCALE))

    def test_frozen_case_counts(self):
        self.assertEqual(len(list(unit_table_cases())), CASES["unit_table"]["expected_case_count"])
        self.assertEqual(CASES["unit_table"]["expected_case_count"], 90)
        self.assertEqual(len(CASES["threshold"]["cases"]), CASES["threshold"]["expected_case_count"])
        self.assertEqual(CASES["threshold"]["expected_case_count"], 13)


class UnitTableTests(Checks, unittest.TestCase):
    def test_unit_table(self):
        for case in unit_table_cases():
            with self.subTest(case=case["id"]):
                unit = {"locator": dict(case["locator"]), "text": case["text"]}
                hits = detect_retention([unit], PACKAGE)
                self.assertEqual(len(hits), 1)
                hit = hits[0]
                self.assert_identity(hit, case["locator"], case["text"])
                self.assertEqual(hit["detail"]["stated"], "%d %s" % (case["value"], case["word"]))
                expected = oracle_days(case["value"], case["unit"])
                self.assert_days(hit["detail"]["stated_days"], expected, case["unit"])
                self.assertEqual(hit["detail"]["comparison"], label("exceeds" if expected > REFERENCE else "within"))


class ThresholdTests(Checks, unittest.TestCase):
    def test_threshold(self):
        for case in CASES["threshold"]["cases"]:
            with self.subTest(case=case["id"]):
                self.assert_threshold(detect_retention, case["id"])

    def test_exact_sixty_is_within(self):
        for case_id in CASES["threshold"]["exact_sixty_ids"]:
            with self.subTest(case=case_id):
                case = threshold_case(case_id)
                self.assertEqual(Fraction(*case["days"]), REFERENCE)
                hit = detect_retention([threshold_unit(case)], PACKAGE)[0]
                self.assertEqual(hit["detail"]["comparison"], label("within"))


class MultipleAndNoneTests(Checks, unittest.TestCase):
    def test_two_mentions_same_line(self):
        spec = CASES["multiple_and_none"]["same_line"]
        hits = detect_retention([{"locator": dict(spec["locator"]), "text": spec["text"]}], PACKAGE)
        self.assertEqual([h["detail"]["stated"] for h in hits], [e["stated"] for e in spec["expected"]])
        self.assertIsNot(hits[0], hits[1])
        self.assertNotEqual(hits[0]["detail"], hits[1]["detail"])
        for hit, expected in zip(hits, spec["expected"]):
            self.assert_identity(hit, spec["locator"], spec["text"])
            unit = expected["stated"].split()[1].rstrip("s")
            self.assert_days(hit["detail"]["stated_days"], Fraction(*expected["days"]), unit)
            self.assertEqual(hit["detail"]["comparison"], label(expected["comparison"]))

    def test_two_mentions_across_units_in_source_order(self):
        spec = CASES["multiple_and_none"]["two_units"]
        hits = detect_retention(copy.deepcopy(spec["units"]), PACKAGE)
        self.assertEqual(len(hits), 2)
        self.assertEqual([h["locator"] for h in hits], [e["locator"] for e in spec["expected"]])
        for hit, expected, unit in zip(hits, spec["expected"], spec["units"]):
            self.assert_identity(hit, expected["locator"], unit["text"])
            self.assertEqual(hit["detail"]["stated"], expected["stated"])
            name = expected["stated"].split()[1].rstrip("s")
            self.assert_days(hit["detail"]["stated_days"], Fraction(*expected["days"]), name)
            self.assertEqual(hit["detail"]["comparison"], label(expected["comparison"]))

    def test_missing_retention_context_yields_none(self):
        units = copy.deepcopy(CASES["multiple_and_none"]["no_context"])
        self.assertEqual(detect_retention(units, PACKAGE), [])
        for unit in units:
            self.assertTrue(detectors.DURATION.search(unit["text"]))


class CaveatTests(Checks, unittest.TestCase):
    def all_units(self):
        units = [{"locator": dict(c["locator"]), "text": c["text"]} for c in unit_table_cases()]
        units += [threshold_unit(c) for c in CASES["threshold"]["cases"]]
        units.append(copy.deepcopy(CASES["multiple_and_none"]["same_line"]))
        units[-1].pop("expected")
        units += copy.deepcopy(CASES["multiple_and_none"]["two_units"]["units"])
        return units

    def test_every_hit_keeps_caveat_and_identity(self):
        units = self.all_units()
        hits = detect_retention(units, PACKAGE)
        self.assertEqual(len(hits), len(units) + 1)  # the same-line unit holds two mentions
        by_locator = {json.dumps(u["locator"], sort_keys=True): u for u in units}
        for hit in hits:
            unit = by_locator[json.dumps(hit["locator"], sort_keys=True)]
            self.assert_identity(hit, unit["locator"], unit["text"])

    def test_caller_units_and_package_unchanged(self):
        units = self.all_units()
        units_before = copy.deepcopy(units)
        package_before = copy.deepcopy(PACKAGE)
        hits = detect_retention(units, PACKAGE)
        self.assertEqual(units, units_before)
        self.assertEqual(PACKAGE, package_before)
        self.assertEqual(len(hits), len(units) + 1)
        caller_locators = {id(unit["locator"]) for unit in units}
        for hit in hits:
            self.assertNotIn(id(hit["locator"]), caller_locators)


def isolated_namespace():
    return dict(vars(detectors))


def month_31_mutant():
    """Isolated _days copy with only the month multiplier changed; detect_retention bound to it."""
    spec = CASES["negative_controls"]["month_multiplier"]
    source = inspect.getsource(detectors._days)
    if source.count(spec["original"]) != 1:
        raise AssertionError("month multiplier anchor not unique")
    namespace = isolated_namespace()
    exec(compile(source.replace(spec["original"], spec["mutant"]), "<fc16-month-31-mutant>", "exec"), namespace)
    code = detectors.detect_retention.__code__
    return types.FunctionType(code, namespace, "detect_retention_month_31_mutant")


def inclusive_mutant():
    """Isolated detect_retention copy whose only change is an inclusive threshold comparison."""
    spec = CASES["negative_controls"]["inclusive_threshold"]
    source = inspect.getsource(detectors.detect_retention)
    if source.count(spec["original"]) != 1:
        raise AssertionError("threshold comparison anchor not unique")
    namespace = isolated_namespace()
    exec(compile(source.replace(spec["original"], spec["mutant"]), "<fc16-inclusive-mutant>", "exec"), namespace)
    return namespace["detect_retention"]


class NegativeControlTests(Checks, unittest.TestCase):
    def setUp(self):
        self.original_days = detectors._days
        self.original_detect = detectors.detect_retention
        self.original_sha = source_sha256()

    def tearDown(self):
        self.assertIs(detectors._days, self.original_days)
        self.assertIs(detectors.detect_retention, self.original_detect)
        self.assertEqual(source_sha256(), self.original_sha)

    def run_control(self, name, mutant):
        spec = CASES["negative_controls"][name]
        for case_id in spec["must_fail"]:
            with self.subTest(control=name, expect="mutant_fails", case=case_id):
                with self.assertRaises(self.failureException) as caught:
                    self.assert_threshold(mutant, case_id)
                self.assertIn("%s:%s" % (case_id, spec["failing_assertion"]), str(caught.exception))
        for case_id in spec["must_pass"]:
            with self.subTest(control=name, expect="mutant_passes_unrelated", case=case_id):
                self.assert_threshold(mutant, case_id)
        for case_id in spec["must_fail"] + spec["must_pass"]:
            with self.subTest(control=name, expect="restored_passes", case=case_id):
                self.assert_threshold(detectors.detect_retention, case_id)

    def test_month_multiplier_31_fails_two_month(self):
        mutant = month_31_mutant()
        self.assertIsNot(mutant, detectors.detect_retention)
        self.run_control("month_multiplier", mutant)

    def test_inclusive_threshold_fails_exact_sixty(self):
        mutant = inclusive_mutant()
        self.assertIsNot(mutant, detectors.detect_retention)
        self.run_control("inclusive_threshold", mutant)


class _ScriptResult(unittest.TextTestResult):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.passed, self.subtests = [], []

    def addSuccess(self, test):
        super().addSuccess(test)
        self.passed.append(test.id())

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        outcome = "pass" if err is None else ("fail" if issubclass(err[0], test.failureException) else "error")
        self.subtests.append({"test": test.id(), "params": {k: v for k, v in subtest.params.items()},
                              "outcome": outcome})


def _script_main():
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(stream=sys.stderr, verbosity=2, resultclass=_ScriptResult).run(suite)
    summary = {"tests_run": result.testsRun, "passed": sorted(result.passed),
               "failures": sorted(t.id() for t, _ in result.failures),
               "errors": sorted(t.id() for t, _ in result.errors),
               "skipped": sorted(t.id() for t, _ in result.skipped),
               "expected_failures": sorted(t.id() for t, _ in result.expectedFailures),
               "unexpected_successes": sorted(t.id() for t in result.unexpectedSuccesses),
               "subtests": result.subtests, "temp_root": tempfile.gettempdir(), "repo": str(REPO),
               "detectors_file": str(Path(detectors.__file__).resolve())}
    if "--result-json" in _SCRIPT_OPTIONS:
        with open(_SCRIPT_OPTIONS["--result-json"], "x", encoding="utf-8") as handle:
            json.dump(summary, handle, indent=2, sort_keys=True)
    return 0 if result.wasSuccessful() and not result.skipped else 1


if __name__ == "__main__":
    raise SystemExit(_script_main())
