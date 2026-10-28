"""FA92: malformed duplicate pointer observations never gain a classification.

Offline, synthetic, counts-only. Rows have the same shape as the M-09
producer rows in tests/fixtures/portal_reconcile (example.test hosts and
low-entropy placeholder hashes). The real entry points are used:
tools.portal_reconcile.reconcile, render and main.

Frozen behavior: a syntactically valid url_sha256 on an otherwise malformed
row is tracked by itself. If a fully valid row has the same hash, that hash
counts as conflicting and is not classified. unique_urls counts only hashes
that have at least one fully valid row. duplicate_observations counts
redundant fully valid rows. malformed_rows counts each invalid source row
once.

Behavior selects the module under test through the ``prc`` class attribute.
GateControl uses that attribute to run the same assertions against a
disposable in-memory copy of the module in which malformed rows are dropped
before hash tracking again (the old early continue). The production module
object and its file are never changed.
"""
import contextlib
import copy
import io
import itertools
import json
import os
from pathlib import Path
import tempfile
import types
import unittest

from tools import portal_reconcile

HOST = "records.example.test"
CLASSES = portal_reconcile.CLASSES


def h(char):
    """Low-entropy synthetic 64-hex placeholder (never a real URL hash)."""
    return char * 64


def nr(url_char, request_id="26-101", item_id="doc-a", **extra):
    """Fully valid allowlisted NextRequest observation (producer shape)."""
    row = {"host_class": "nextrequest", "host": HOST, "allowlisted": True, "url_sha256": h(url_char)}
    if request_id is not None:
        row["request_id"] = request_id
    if item_id is not None:
        row["item_id"] = item_id
    row.update(extra)
    return row


def received(request_id="26-101", item_id="doc-a"):
    return {"host": HOST, "request_id": request_id, "item_id": item_id, "state": "received",
            "has_version": True}


# Malformed variants that keep a syntactically valid url_sha256. Each maps to
# its exact fixed reason code from normalize_observation.
def bad_allowlisted(c):
    return nr(c, allowlisted="yes")


def bad_host_class(c):
    return nr(c, host_class="dropbox")


def bad_path_mismatch(c):
    return nr(c, item_id="doc-b", path="/documents/doc-a")


def bad_unknown_field(c):
    return nr(c, url="https://records.example.test/documents/doc-a")


def bad_missing_host(c):
    row = nr(c)
    del row["host"]
    return row


MALFORMED_KINDS = {
    "invalid_allowlisted": bad_allowlisted,
    "unknown_host_class": bad_host_class,
    "item_id_path_mismatch": bad_path_mismatch,
    "unknown_field": bad_unknown_field,
    "missing_host": bad_missing_host,
}

TAINT_MAIL = {"unique_urls": 1, "conflicting_urls": 1, "classified_urls": 0,
              "duplicate_observations": 0, "malformed_rows": 1}


def zero_classes():
    return {name: 0 for name in CLASSES}


class Behavior(unittest.TestCase):
    prc = portal_reconcile

    def run_reconcile(self, observations, queue):
        before = copy.deepcopy((observations, queue))
        report = self.prc.reconcile(observations, queue)
        self.assertEqual((observations, queue), before, "inputs must not be mutated")
        return report

    def mail(self, report):
        return {key: report["mail"][key] for key in TAINT_MAIL}

    def assert_tainted(self, report, code):
        self.assertEqual(report["classes"], zero_classes())
        self.assertEqual(self.mail(report), TAINT_MAIL)
        self.assertEqual(report["mail"]["malformed_reasons"], {code: 1})
        self.assertEqual(report["detail"]["fetched_queue_items"], 0)
        self.assertEqual(report["queue"]["referenced_by_mail"], 0)
        self.assertEqual(report["queue"]["not_referenced_by_mail"], 1)
        self.assertEqual(report["by_host_class"]["nextrequest"], zero_classes())

    def check_kind(self, code):
        maker = MALFORMED_KINDS[code]
        bad = maker("1")
        # Precondition: the variant really is malformed for this exact reason
        # and still declares a syntactically valid hash.
        self.assertEqual(self.prc._normalized(self.prc.normalize_observation, bad), (None, code))
        self.assertRegex(bad["url_sha256"], "^[0-9a-f]{64}$")
        for label, rows in (("valid_first", [nr("1"), bad]), ("malformed_first", [bad, nr("1")])):
            with self.subTest(order=label):
                self.assert_tainted(self.run_reconcile(rows, [received()]), code)

    # --- positives: current counts preserved -------------------------------

    def test_valid_singleton_counts_preserved(self):
        report = self.run_reconcile([nr("1")], [received()])
        self.assertEqual(self.mail(report), {"unique_urls": 1, "conflicting_urls": 0, "classified_urls": 1,
                                             "duplicate_observations": 0, "malformed_rows": 0})
        self.assertEqual(report["classes"], dict(zero_classes(), fetched=1))
        self.assertEqual(report["detail"]["fetched_queue_items"], 1)
        self.assertEqual(report["queue"]["referenced_by_mail"], 1)

    def test_identical_valid_duplicates_counts_preserved(self):
        rows = [nr("1"), nr("1"), nr("1")]
        report = self.run_reconcile(rows, [received()])
        self.assertEqual(self.mail(report), {"unique_urls": 1, "conflicting_urls": 0, "classified_urls": 1,
                                             "duplicate_observations": 2, "malformed_rows": 0})
        self.assertEqual(report["mail"]["observation_rows"], 3)
        self.assertEqual(report["classes"], dict(zero_classes(), fetched=1))

    # --- valid + malformed same hash, both orders, every kind ---------------

    def test_taint_invalid_allowlisted(self):
        self.check_kind("invalid_allowlisted")

    def test_taint_unknown_host_class(self):
        self.check_kind("unknown_host_class")

    def test_taint_item_id_path_mismatch(self):
        self.check_kind("item_id_path_mismatch")

    def test_taint_unknown_field(self):
        self.check_kind("unknown_field")

    def test_taint_missing_host(self):
        self.check_kind("missing_host")

    # --- malformed hashes / malformed-only groups do not poison others ------

    def test_malformed_hash_does_not_poison_valid_hash(self):
        upper = nr("a", url_sha256=h("a").upper())
        short = nr("a", url_sha256=h("a")[:63])
        rows = [42, None, ["x"], upper, short, {"url_sha256": 7}, nr("a")]
        for order in (rows, list(reversed(rows))):
            with self.subTest(first=type(order[0]).__name__):
                report = self.run_reconcile(order, [received()])
                self.assertEqual(self.mail(report), {"unique_urls": 1, "conflicting_urls": 0,
                                                     "classified_urls": 1, "duplicate_observations": 0,
                                                     "malformed_rows": 6})
                self.assertEqual(report["mail"]["malformed_reasons"],
                                 {"not_object": 3, "invalid_url_sha256": 2, "unknown_host_class": 1})
                self.assertEqual(report["classes"], dict(zero_classes(), fetched=1))

    def test_malformed_only_group_not_in_denominator(self):
        rows = [bad_allowlisted("2"), bad_unknown_field("2"), bad_missing_host("3"), nr("1")]
        for order in (rows, list(reversed(rows))):
            with self.subTest(first=order[0]["url_sha256"][:1]):
                report = self.run_reconcile(order, [received()])
                self.assertEqual(self.mail(report), {"unique_urls": 1, "conflicting_urls": 0,
                                                     "classified_urls": 1, "duplicate_observations": 0,
                                                     "malformed_rows": 3})
                self.assertEqual(report["mail"]["malformed_reasons"],
                                 {"invalid_allowlisted": 1, "unknown_field": 1, "missing_host": 1})
                self.assertEqual(report["classes"], dict(zero_classes(), fetched=1))
        report = self.run_reconcile([bad_allowlisted("2"), bad_allowlisted("2")], [received()])
        self.assertEqual(self.mail(report), {"unique_urls": 0, "conflicting_urls": 0, "classified_urls": 0,
                                             "duplicate_observations": 0, "malformed_rows": 2})

    # --- many good/bad rows conserve denominators under permutation --------

    def test_mixed_rows_conserve_denominators_under_permutation(self):
        queue = [received(), received("26-102", "doc-b"), received("26-103", "doc-d")]
        rows = [
            nr("1"), nr("1"), bad_path_mismatch("1"),                       # tainted A, 1 dup
            nr("2", "26-102", "doc-b"),                                       # clean B: fetched
            bad_unknown_field("3"),                                           # malformed-only C
            nr("4", "26-103", "doc-d"),                                       # D: valid conflict pair
            {"host_class": "govqa", "host": HOST, "url_sha256": h("4")},
        ]
        expected_mail = {"unique_urls": 3, "conflicting_urls": 2, "classified_urls": 1,
                         "duplicate_observations": 2, "malformed_rows": 2}
        rendered = set()
        count = 0
        for order in itertools.permutations(rows):
            order = [copy.deepcopy(row) for row in order]
            report = self.run_reconcile(order, queue)
            self.assertEqual(self.mail(report), expected_mail)
            self.assertEqual(report["mail"]["malformed_reasons"], {"item_id_path_mismatch": 1, "unknown_field": 1})
            self.assertEqual(report["classes"], dict(zero_classes(), fetched=1))
            self.assertEqual(report["queue"]["referenced_by_mail"], 1)
            rendered.add(self.prc.render(report))
            count += 1
        self.assertEqual(count, 5040)
        self.assertEqual(len(rendered), 1)

    # --- existing conservative behavior ------------------------------------

    def test_valid_conflicting_duplicates_stay_conservative(self):
        pair = [nr("1"), {"host_class": "govqa", "host": HOST, "url_sha256": h("1")}]
        for rows in (pair, list(reversed(pair)), pair + [bad_allowlisted("1")],
                     [bad_allowlisted("1")] + list(reversed(pair))):
            with self.subTest(rows=len(rows), first=rows[0]["host_class"]):
                report = self.run_reconcile(rows, [received()])
                self.assertEqual(report["mail"]["unique_urls"], 1)
                self.assertEqual(report["mail"]["conflicting_urls"], 1)
                self.assertEqual(report["mail"]["classified_urls"], 0)
                self.assertEqual(report["mail"]["duplicate_observations"], 1)
                self.assertEqual(report["mail"]["malformed_rows"], len(rows) - 2)
                self.assertEqual(report["classes"], zero_classes())

    def test_held_classes_stay_conservative(self):
        held = [({"host_class": "govqa", "host": HOST, "request_id": "26-101", "item_id": "doc-a",
                  "url_sha256": h("1")}, "held_policy"),
                ({"host_class": "tracked_redirect", "host": HOST, "url_sha256": h("1")}, "held_redirect"),
                (nr("1", allowlisted=False), "held_policy")]
        for row, cls in held:
            with self.subTest(host_class=row["host_class"], alone=True):
                report = self.run_reconcile([row], [received()])
                self.assertEqual(report["classes"], dict(zero_classes(), **{cls: 1}))
            for rows in ([row, bad_host_class("1")], [bad_host_class("1"), row]):
                with self.subTest(host_class=row["host_class"], first=rows[0]["host_class"]):
                    report = self.run_reconcile(rows, [received()])
                    self.assertEqual(report["classes"], zero_classes())
                    self.assertEqual(self.mail(report), TAINT_MAIL)
                    self.assertEqual(report["mail"]["malformed_reasons"], {"unknown_host_class": 1})

    # --- determinism / aggregate-only ---------------------------------------

    def test_empty_input_deterministic(self):
        first = self.run_reconcile([], [])
        second = self.run_reconcile([], [])
        self.assertEqual(self.prc.render(first), self.prc.render(second))
        self.assertEqual(first["mail"], {"observation_rows": 0, "malformed_rows": 0, "malformed_reasons": {},
                                         "duplicate_observations": 0, "unique_urls": 0,
                                         "conflicting_urls": 0, "classified_urls": 0})
        self.assertEqual(first["classes"], zero_classes())
        self.assertEqual(first["queue"]["rows"], 0)

    def test_repeat_render_aggregate_only(self):
        rows = [bad_unknown_field("1"), nr("1")]
        texts = [self.prc.render(self.run_reconcile(rows, [received()])) for _ in range(3)]
        self.assertEqual(len(set(texts)), 1)
        report = json.loads(texts[0])
        self.assertEqual(self.mail(report), TAINT_MAIL)
        for marker in (h("1"), HOST, "26-101", "doc-a", "https", "/documents", "example"):
            self.assertNotIn(marker, texts[0])

    def test_cli_entry_point_reports_taint(self):
        observations = {"schema": self.prc.OBSERVATION_SCHEMA, "observations": [nr("1"), bad_missing_host("1")]}
        queue = {"schema": self.prc.QUEUE_SCHEMA, "items": [received()]}
        with tempfile.TemporaryDirectory(prefix="fa92-cli-") as tmp:
            obs, que, out = (Path(tmp) / name for name in ("obs.json", "queue.json", "report.json"))
            obs.write_text(json.dumps(observations))
            que.write_text(json.dumps(queue))
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = self.prc.main(["--observations", str(obs), "--queue-export", str(que), "--output", str(out)])
            self.assertEqual((code, stdout.getvalue(), stderr.getvalue()), (0, "", ""))
            self.assertEqual(os.stat(out).st_mode & 0o777, 0o600)
            text = out.read_text()
        report = json.loads(text)
        self.assertEqual(self.mail(report), TAINT_MAIL)
        self.assertEqual(report["mail"]["malformed_reasons"], {"missing_host": 1})
        self.assertEqual(report["classes"], zero_classes())
        self.assertNotIn(tmp, text)


# ---------------------------------------------------------------- gate control

TAINT_REGRESSION = "test_taint_invalid_allowlisted"
POSITIVE_CONTROL = "test_valid_singleton_counts_preserved"


def disposable_mutant():
    """In-memory copy of the module with the old early continue restored.

    Every function of tools.portal_reconcile is rebuilt over a copied global
    namespace, and only the copy's hash-declaration helper is replaced by one
    that returns None. In the copy a malformed row therefore contributes
    nothing after its reason is counted, which is the pre-FA92 behavior. The
    production module object, its globals and its file stay unchanged.
    """
    real = portal_reconcile
    module = types.ModuleType("tools._fa92_disposable_mutant")
    namespace = module.__dict__
    namespace.update({k: v for k, v in vars(real).items() if k not in ("__name__", "__spec__", "__loader__")})
    for name, value in list(namespace.items()):
        if isinstance(value, types.FunctionType) and value.__module__ == real.__name__:
            clone = types.FunctionType(value.__code__, namespace, value.__name__, value.__defaults__,
                                       value.__closure__)
            clone.__kwdefaults__ = copy.copy(value.__kwdefaults__)
            namespace[name] = clone
    if "_declared_url_sha256" not in namespace:
        raise LookupError("hash_declaration_helper_missing")
    namespace["_declared_url_sha256"] = lambda row: None
    return module


def run_behavior(module, names):
    """Run named Behavior tests with ``prc`` bound to ``module``; returns (result, text)."""
    bound = type("BehaviorOn_" + module.__name__.rsplit(".", 1)[-1], (Behavior,), {"prc": module})
    suite = unittest.TestSuite(bound(name) for name in names)
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    return result, stream.getvalue()


class GateControl(unittest.TestCase):
    def test_mutant_reproduces_fetched_classification(self):
        mutant = disposable_mutant()
        report = mutant.reconcile([nr("1"), bad_allowlisted("1")], [received()])
        self.assertEqual(report["classes"]["fetched"], 1)
        self.assertEqual(report["mail"]["conflicting_urls"], 0)
        self.assertEqual(report["mail"]["malformed_rows"], 1)
        real = portal_reconcile.reconcile([nr("1"), bad_allowlisted("1")], [received()])
        self.assertEqual(real["classes"]["fetched"], 0)
        self.assertEqual(real["mail"]["conflicting_urls"], 1)

    def test_taint_regression_detects_mutant(self):
        result, text = run_behavior(disposable_mutant(), [TAINT_REGRESSION])
        self.assertEqual(result.testsRun, 1)
        self.assertEqual(result.errors, [], text)
        # One failure per row order (subTests), each owned by the regression
        # and each naming the fetched classification it must not produce.
        self.assertEqual(len(result.failures), 2, text)
        for case, trace in result.failures:
            self.assertEqual(getattr(case, "test_case", case)._testMethodName, TAINT_REGRESSION)
            self.assertIn("'fetched': 1", trace)

    def test_positive_control_green_on_mutant_and_restored(self):
        result, text = run_behavior(disposable_mutant(), [POSITIVE_CONTROL])
        self.assertTrue(result.wasSuccessful() and result.testsRun == 1, text)
        result, text = run_behavior(portal_reconcile, [TAINT_REGRESSION, POSITIVE_CONTROL])
        self.assertTrue(result.wasSuccessful() and result.testsRun == 2, text)

    def test_production_module_untouched(self):
        helper = portal_reconcile._declared_url_sha256
        reconcile = portal_reconcile.reconcile
        disposable_mutant()
        self.assertIs(portal_reconcile._declared_url_sha256, helper)
        self.assertIs(portal_reconcile.reconcile, reconcile)
        self.assertEqual(helper(nr("1")), h("1"))
        self.assertIsNone(helper(nr("1", url_sha256="X")))
        self.assertIsNone(helper(42))


if __name__ == "__main__":
    unittest.main()
