"""FC13: per-unit document keyword classification keeps the frozen priority.

document.analyze reports at most one keyword classification per unit. When a
unit contains words from more than one category, the higher-priority category
wins even if the lower-priority word comes first in the text. The selected
quote must be the exact source bytes of the selected keyword. This file only
tests that frozen ordering. It does not claim that any document kind is
semantically correct, and every result stays NOT_ASSESSED with
review_acceptance false.

All inputs are fictional ASCII strings from
tests/fixtures/fc13-document-priority/cases.json. Hashes are computed at run
time from fictional labels and never describe a real record.

Script mode (used by the acceptance runner) takes a literal temporary root
before any product import:

    python3 tests/records/test_fc13_document_priority.py --fc13-tmp-root DIR
        [--fc13-result FILE] [--fc13-control none|reverse-classifiers]

Ordinary unittest discovery does not change the environment, tempfile
defaults or audit hooks; only script mode installs its process-local guards.
"""
import hashlib
import itertools
import json
import os
from pathlib import Path
import sys
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "tests" / "fixtures" / "fc13-document-priority" / "cases.json"
CASES = json.loads(FIXTURE.read_text(encoding="utf-8"))
ORDER = list(CASES["priority_order"])
TAG = CASES["negative_control"]["failure_tag"]
_SCRIPT_OPTIONS = None


def _script_options(argv):
    """Parse and remove script-only options; refuse unknown ones."""
    options = {"tmp_root": None, "result": None, "control": "none"}
    names = {"--fc13-tmp-root": "tmp_root", "--fc13-result": "result", "--fc13-control": "control"}
    rest = [argv[0]]
    index = 1
    while index < len(argv):
        item = argv[index]
        if item in names:
            if index + 1 >= len(argv):
                raise SystemExit("missing value for " + item)
            options[names[item]] = argv[index + 1]
            index += 2
        elif item in ("-v", "--verbose"):
            rest.append(item)
            index += 1
        else:
            raise SystemExit("unsupported argument: " + item)
    if not options["tmp_root"]:
        raise SystemExit("--fc13-tmp-root is required in script mode")
    root = Path(options["tmp_root"])
    if not root.is_absolute() or not root.is_dir() or root.is_symlink():
        raise SystemExit("--fc13-tmp-root must be an existing absolute directory")
    if options["control"] not in ("none", "reverse-classifiers"):
        raise SystemExit("unsupported --fc13-control value")
    argv[:] = rest
    return options


def _install_script_guards(tmp_root):
    """Process-local offline sentinels for the dedicated script-mode child only."""
    import tempfile

    os.environ["TMPDIR"] = tmp_root
    tempfile.tempdir = tmp_root
    blocked = ("socket.connect", "socket.bind", "socket.getaddrinfo", "socket.sendto",
               "subprocess.Popen", "os.system", "os.posix_spawn", "os.exec", "os.spawn",
               "urllib.Request")

    def hook(event, _args):
        if event in blocked:
            raise RuntimeError("fc13_offline_sentinel:" + event)

    sys.addaudithook(hook)


if __name__ == "__main__":
    _SCRIPT_OPTIONS = _script_options(sys.argv)
    _install_script_guards(_SCRIPT_OPTIONS["tmp_root"])
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))

from campaign_tool.records.understanding import document, evidence  # noqa: E402


def _digest(label):
    return hashlib.sha256(("fc13-document-priority synthetic " + label).encode("utf-8")).hexdigest()


def _bundle(slot_texts):
    """Build a validated bundle from (original_label, receipt_label, ordinal, text) slots."""
    units = []
    counts = {}
    context = {}
    for original_label, receipt_label, ordinal, text in slot_texts:
        original = _digest(original_label)
        receipt = _digest(receipt_label)
        raw = {"kind": "text", "locator": {"page": ordinal}, "text": text, "data": {}}
        units.append(evidence.unit(raw, original, receipt, ordinal))
        counts[(original, receipt)] = counts.get((original, receipt), 0) + 1
        context[original] = {"format": "txt", "review_acceptance": False}
    receipts = [{"original_sha256": o, "receipt_sha256": r, "attempt_kind": "native",
                 "declared_unit_count": n, "normalized_unit_count": n}
                for (o, r), n in counts.items()]
    return evidence.bundle(units, context, receipts)


def _single_receipt_bundle(texts):
    return _bundle([("single-original", "single-receipt", i, t) for i, t in enumerate(texts, 1)])


def _winner(first, second):
    return first if ORDER.index(first) < ORDER.index(second) else second


def _pair_cases(first, second):
    cases = []
    for term_a, term_b in itertools.product(CASES["synonyms"][first], CASES["synonyms"][second]):
        text = CASES["pair_template"].format(first=term_a, second=term_b)
        winner = _winner(first, second)
        quote = term_a if winner == first else term_b
        cases.append((text, winner, quote))
    return cases


class _Checks(unittest.TestCase):
    """Shared assertions over the real document.analyze and evidence.validate_citation."""

    def analyze(self, bundle):
        result = document.analyze(bundle)
        self.assertEqual(result["schema"], document.SCHEMA)
        self.assertIs(result["review_acceptance"], False)
        self.assertEqual(result["semantic_assessment"], "NOT_ASSESSED")
        self.assertEqual(result["candidate_omissions"]["classifications"], 0)
        return result

    def check_units(self, bundle, expected_by_text):
        """expected_by_text maps unit text -> (category or None, quote or None)."""
        result = self.analyze(bundle)
        by_unit = {}
        for item in result["classifications"]:
            self.assertNotIn(item["unit_id"], by_unit, "more than one classification for a unit")
            by_unit[item["unit_id"]] = item
        expected_ids = []
        for unit in bundle["units"]:
            category, quote = expected_by_text[unit["text"]]
            item = by_unit.get(unit["unit_id"])
            if category is None:
                self.assertIsNone(item, "unexpected classification for neutral/negative text %r" % unit["text"])
                continue
            expected_ids.append(unit["unit_id"])
            self.assertIsNotNone(item, "missing classification for %r" % unit["text"])
            self.assertEqual(item["candidate"], category,
                             "%s: unit text %r selected %r, frozen priority expects %r"
                             % (TAG, unit["text"], item["candidate"], category))
            self.check_citation(bundle, unit, item, quote)
        # One candidate per unit, reported in bundle unit order.
        self.assertEqual([item["unit_id"] for item in result["classifications"]], expected_ids)
        self.assertEqual(result["candidate_totals"]["classifications"], len(expected_ids))
        return result

    def check_citation(self, bundle, unit, item, quote):
        self.assertEqual(item["basis"], "deterministic_keyword_match")
        self.assertEqual(item["semantic_assessment"], "NOT_ASSESSED")
        cite = item["citation"]
        raw = unit["text"].encode("utf-8")
        start = raw.find(quote.encode("utf-8"))
        self.assertGreaterEqual(start, 0, "frozen quote is not in the fixture text")
        self.assertEqual(cite["quote"], quote, "%s: quote must name the selected keyword" % TAG)
        self.assertEqual(cite["span"], {"start_byte": start, "end_byte": start + len(quote.encode("utf-8"))},
                         "%s: citation must point at the first source occurrence" % TAG)
        self.assertEqual(raw[cite["span"]["start_byte"]:cite["span"]["end_byte"]].decode("utf-8"), quote)
        for key in ("unit_id", "original_sha256", "import_id", "artifact_sha256", "text_sha256"):
            self.assertEqual(cite[key], unit[key])
        self.assertEqual(cite["locator"], unit["locator"])
        checked = evidence.validate_citation(cite, bundle)
        self.assertEqual(checked["structural_support"], "verified_exact_retained_bytes")
        self.assertEqual(checked["semantic_assessment"], "NOT_ASSESSED")
        self.assertIs(checked["review_acceptance"], False)

    def check_pair(self, first, second):
        cases = _pair_cases(first, second)
        bundle = _single_receipt_bundle([text for text, _, _ in cases])
        self.check_units(bundle, {text: (winner, quote) for text, winner, quote in cases})
        return len(cases)

    def check_singles(self):
        texts = {}
        for category in ORDER:
            for term in CASES["synonyms"][category]:
                texts[CASES["single_template"].format(term=term)] = (category, term)
        for item in CASES["case_positives"]:
            texts[CASES["single_template"].format(term=item["term"])] = (item["category"], item["term"])
        self.check_units(_single_receipt_bundle(list(texts)), texts)
        return len(texts)

    def check_neutral(self):
        texts = {text: (None, None) for text in CASES["neutral_texts"]}
        result = self.check_units(_single_receipt_bundle(list(texts)), texts)
        self.assertEqual(result["classifications"], [])


class FC13FixtureTests(_Checks):
    def test_fixture_is_frozen_shape(self):
        self.assertEqual(CASES["schema"], "fc13-document-priority-cases/v1")
        self.assertEqual(len(ORDER), 5)
        self.assertEqual(len(set(ORDER)), 5)
        self.assertEqual(list(CASES["synonyms"]), ORDER)
        self.assertTrue(all(len(CASES["synonyms"][c]) == 4 for c in ORDER))
        self.assertTrue(all(isinstance(t, str) and t.isascii() for c in ORDER for t in CASES["synonyms"][c]))
        self.assertEqual(sorted(CASES["text_rotations"]), list(range(len(CASES["slots"]))))
        for order in CASES["input_orders"]:
            self.assertEqual(sorted(order), list(range(len(CASES["slots"]))))

    def test_configured_categories_match_frozen_set_without_reading_order(self):
        labels = [label for label, _ in document._CLASSIFIERS]
        self.assertEqual(sorted(labels), sorted(ORDER))
        self.assertEqual(len(labels), len(set(labels)))


class FC13PairPriorityTests(_Checks):
    """Generated below: one test per ordered category pair (both text orders)."""


def _make_pair_test(first, second):
    def test(self):
        self.assertEqual(self.check_pair(first, second),
                         len(CASES["synonyms"][first]) * len(CASES["synonyms"][second]))
    test.__doc__ = "%s written before %s selects %s." % (first, second, _winner(first, second))
    return test


for _first, _second in itertools.permutations(ORDER, 2):
    setattr(FC13PairPriorityTests, "test_pair_%s_then_%s" % (_first, _second), _make_pair_test(_first, _second))
del _first, _second


class FC13SingleCategoryTests(_Checks):
    def test_each_synonym_and_case_variant_is_a_single_positive(self):
        expected = sum(len(CASES["synonyms"][c]) for c in ORDER) + len(CASES["case_positives"])
        self.assertEqual(self.check_singles(), expected)

    def test_word_boundary_negatives_classify_nothing(self):
        texts = {CASES["single_template"].format(term=t): (None, None) for t in CASES["boundary_negatives"]}
        self.assertEqual(len(texts), len(CASES["boundary_negatives"]))
        result = self.check_units(_single_receipt_bundle(list(texts)), texts)
        self.assertEqual(result["classifications"], [])

    def test_neutral_units_classify_nothing(self):
        self.check_neutral()


class FC13RepeatedTermTests(_Checks):
    def _case(self, name):
        case = next(c for c in CASES["repeated_cases"] if c["name"] == name)
        bundle = _single_receipt_bundle([case["text"]])
        self.check_units(bundle, {case["text"]: (case["expected_category"], case["expected_quote"])})

    def test_winner_repeated_after_lower_first(self):
        self._case("winner_repeated_after_lower_first")

    def test_winner_first_synonym_in_source_order(self):
        self._case("winner_first_synonym_in_source_order")

    def test_same_term_repeated_selects_first(self):
        self._case("same_term_repeated_selects_first")


class FC13MultiUnitTests(_Checks):
    def _expected(self):
        return {e["text"]: (e["expected_category"], e["expected_quote"]) for e in CASES["multi_unit_texts"]}

    def _slot_texts(self, rotation, order):
        texts = [e["text"] for e in CASES["multi_unit_texts"]]
        slots = CASES["slots"]
        placed = [(s["original"], s["receipt"], s["ordinal"], texts[(i + rotation) % len(texts)])
                  for i, s in enumerate(slots)]
        return [placed[i] for i in order]

    def test_units_in_one_bundle_are_classified_independently(self):
        texts = [e["text"] for e in CASES["multi_unit_texts"]]
        self.check_units(_single_receipt_bundle(texts), self._expected())

    def test_receipt_and_unit_permutations_keep_per_unit_identity(self):
        expected = self._expected()
        checked = 0
        for rotation in CASES["text_rotations"]:
            reference = None
            for order in CASES["input_orders"]:
                bundle = _bundle(self._slot_texts(rotation, order))
                self.assertEqual(len({u["original_sha256"] for u in bundle["units"]}), 2)
                self.assertEqual(len({u["receipt_sha256"] for u in bundle["units"]}), 3)
                result = self.check_units(bundle, expected)
                projection = [(c["unit_id"], c["candidate"], c["citation"]["quote"], c["citation"]["span"],
                               c["citation"]["original_sha256"]) for c in result["classifications"]]
                by_id = {u["unit_id"]: u for u in bundle["units"]}
                for unit_id, candidate, quote, _, original in projection:
                    self.assertEqual(by_id[unit_id]["original_sha256"], original)
                    self.assertEqual(expected[by_id[unit_id]["text"]], (candidate, quote))
                if reference is None:
                    reference = projection
                self.assertEqual(projection, reference, "input order changed per-unit results")
                checked += 1
        self.assertEqual(checked, len(CASES["text_rotations"]) * len(CASES["input_orders"]))


class FC13NegativeControlTests(_Checks):
    def test_reversed_classifiers_kill_competing_assertion(self):
        """Declared synthetic control: reverse only _CLASSIFIERS in memory, fixtures fixed."""
        original = document._CLASSIFIERS
        mutant = tuple(sorted(original, key=lambda item: -ORDER.index(item[0])))
        self.assertEqual([label for label, _ in mutant], list(reversed(ORDER)))
        with mock.patch.object(document, "_CLASSIFIERS", mutant):
            with self.assertRaises(self.failureException) as caught:
                self.check_pair("policy", "invoice_or_fee_record")
            self.assertIn(TAG, str(caught.exception))
            self.assertIn("'invoice_or_fee_record'", str(caught.exception))
            # Neutral and single-category positives are unaffected by the mutation.
            self.check_singles()
            self.check_neutral()
        self.assertIs(document._CLASSIFIERS, original)
        # Restored: the genuine positive passes again.
        self.check_pair("policy", "invoice_or_fee_record")


class _Recorder(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.passed = []

    def addSuccess(self, test):
        super().addSuccess(test)
        self.passed.append(test.id())


def _script_main(options):
    if options["control"] == "reverse-classifiers":
        document._CLASSIFIERS = tuple(sorted(document._CLASSIFIERS, key=lambda item: -ORDER.index(item[0])))
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(stream=sys.stderr, verbosity=2, resultclass=_Recorder)
    result = runner.run(suite)
    report = {
        "schema": "fc13-document-priority-script-result/v1",
        "control": options["control"],
        "classifier_order": [label for label, _ in document._CLASSIFIERS],
        "tests_run": result.testsRun,
        "passed": sorted(result.passed),
        "failures": sorted([{"id": t.id(), "traceback": tb} for t, tb in result.failures], key=lambda x: x["id"]),
        "errors": sorted([{"id": t.id(), "traceback": tb} for t, tb in result.errors], key=lambda x: x["id"]),
        "skipped": sorted(t.id() for t, _ in result.skipped),
        "expected_failures": len(result.expectedFailures),
        "unexpected_successes": len(result.unexpectedSuccesses),
        "tmp_root": options["tmp_root"],
    }
    if options["result"]:
        with open(options["result"], "x", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, sort_keys=True)
    return 0 if result.wasSuccessful() and not result.skipped else 1


if __name__ == "__main__":
    raise SystemExit(_script_main(_SCRIPT_OPTIONS))
