"""FA45: overflowing JSON numbers in captured units are refused, not projected.

Synthetic-only. Every store is created fresh by the existing ledger fixture
factory (tests.records.test_ledger) and projected through the full
project_units API with the real schema, append permit and binding guards.

Acceptance mapping (see tools/accept_fa45_unit_finite.py):
  A1 FinitePositiveTests       finite/zero/underflow/integer locators still project
  A2 OverflowRefusalTests      exponent overflow refused at every depth
  A3 MixedBatchRecoveryTests   reason histograms survive fresh run and restart
  A5 every class above uses the real import factory and project_units
"""

from contextlib import closing
import hashlib
import json
import math
import sqlite3
import unittest
from unittest import mock

from campaign_tool.records.ledger import store, unit_projection as projection
from tests.records import test_ledger as ledger_fixtures

BLOCKED_LOCATOR = "exact_locator_missing_or_invalid"
INVALID_JSON = "invalid_captured_json"
PROJECTED = "missing_parser_provenance"

HUGE_INT = "9" * 400  # exact integer, far beyond float range
HUGE_FRACTION = "1" + "0" * 400 + ".5"  # decimal mantissa that overflows a float

# Locators that a finite-only decoder must refuse. Each is a JSON object so the
# refusal can only come from the number itself, never from the shape check.
OVERFLOW_LOCATORS = {
    "root_positive_exponent": '{"page":1e309}',
    "root_negative_value": '{"page":-1e309}',
    "root_uppercase_signed_exponent": '{"page":1E+400}',
    "root_fractional_mantissa": '{"page":' + HUGE_FRACTION + '}',
    "ignored_field": '{"line":1,"ignored":2.5e308}',
    "nested_object": '{"line":1,"box":{"x":{"y":-1.8e308}}}',
    "nested_array": '{"line":1,"box":[0,[1,[1e999]]]}',
    "array_of_objects": '{"line":1,"spans":[{"a":1.0},{"b":-9e9999}]}',
}

FINITE_LOCATOR = (
    '{"line":1,"zero":0.0,"neg_zero":-0.0,"under":1e-400,"neg_under":-1e-999,'
    '"max":1.7976931348623157e308,"min":-1.7976931348623157e308,'
    '"big_int":' + HUGE_INT + ',"neg_int":-' + HUGE_INT + ','
    '"nested":[1.5,{"g":2.5e10,"h":[-0.0,7]}]}'
)


class _Store(unittest.TestCase):
    """One fresh synthetic legacy capture and ledger per test."""

    def setUp(self):
        self.fixture = ledger_fixtures.LedgerTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.database = self.fixture.output
        self.sha = self.fixture.sha

    def set_units(self, locators):
        """Replace the legacy units table: one row per locator, ordinal = index."""
        with closing(sqlite3.connect(self.fixture.snapshot / "intake.sqlite")) as connection:
            connection.execute("DELETE FROM units")
            connection.executemany(
                "INSERT INTO units VALUES(?,?,?,?,?,?)",
                [(self.sha, ordinal, "text", locator, "synthetic " + str(ordinal), "{}")
                 for ordinal, locator in enumerate(locators)])
            connection.commit()
        self.fixture.refresh_manifest()

    def capture(self):
        return store.import_legacy(self.fixture.snapshot, self.database)["import_id"]

    def rows(self, table, order=""):
        with store.ledger(self.database, readonly=True) as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM " + table + order)]

    def evidence(self):
        return self.rows("unit_projection_rows", " ORDER BY source_rowid")

    def legacy_units(self, capture):
        return [row for row in self.rows("legacy_rows", " ORDER BY source_rowid")
                if row["import_id"] == capture and row["source_table"] == "units"]

    def clone_capture(self, base, payloads):
        """Append a second completed synthetic capture whose unit payloads are given verbatim.

        The legacy importer always writes allow_nan=False canonical JSON, so an
        overflowing number in the outer payload can only be represented by
        writing a fresh capture directly. Only new keys are inserted; existing
        evidence is never updated, and no projection run exists for this import.
        """
        clone = hashlib.sha256(("fa45-clone:" + base + ":" + str(len(payloads))).encode()).hexdigest()
        with store.ledger(self.database) as connection:
            source = dict(connection.execute(
                "SELECT * FROM legacy_imports WHERE id=?", (base,)).fetchone())
            counts = json.loads(source["declared_table_counts"])
            counts["units"] = len(payloads)
            source.update(id=clone, declared_table_counts=store.canonical(counts),
                          config_sha256=hashlib.sha256(clone.encode()).hexdigest())
            columns = list(source)
            connection.execute(
                "INSERT INTO legacy_imports(" + ",".join(columns) + ") VALUES(" +
                ",".join("?" for _ in columns) + ")", [source[key] for key in columns])
            for rowid, (identity, payload) in enumerate(payloads, start=1):
                connection.execute(
                    "INSERT INTO legacy_rows VALUES(?,?,?,?,?,?)",
                    (clone, "units", rowid, identity, payload,
                     hashlib.sha256(payload.encode("utf-8")).hexdigest()))
            connection.commit()
        return clone

    def payload(self, ordinal, extra=""):
        """Hand-written legacy unit payload; extra is raw JSON members appended verbatim."""
        identity = '{"ordinal":%d,"sha":"%s"}' % (ordinal, self.sha)
        payload = ('{"data":"{}","kind":"text","locator":"{\\"line\\":1}","ordinal":%d,'
                   '"sha":"%s","text":"synthetic %d"%s}' % (ordinal, self.sha, ordinal, extra))
        return identity, payload

    def assert_accounted(self, result, capture):
        """Every captured source row has exactly one association and one gap."""
        sources = self.legacy_units(capture)
        evidence = [row for row in self.evidence() if row["import_id"] == capture]
        self.assertEqual(result["expected_source_rows"], len(sources))
        self.assertEqual(result["processed_source_rows"], len(sources))
        self.assertEqual(result["status"], "complete")
        self.assertEqual([row["source_rowid"] for row in evidence],
                         [row["source_rowid"] for row in sources])
        for source, row in zip(sources, evidence):
            self.assertEqual(row["payload_sha256"], source["payload_sha256"])
            self.assertEqual(hashlib.sha256(source["payload_json"].encode()).hexdigest(),
                             source["payload_sha256"])
            self.assertEqual(row["integrity_checked"], 1)
        self.assertEqual(sum(result["unresolved_reasons"].values()), len(sources))
        self.assertEqual(result["stage_promotions"], 0)
        self.assertFalse(result["original_parser_verified"])
        self.assertFalse(result["publication_ready"])
        self.assertIsNone(result["end_to_end_complete"])
        gaps = [row for row in self.rows("import_gaps") if row["category"] == "unit_capture"
                and json.loads(row["reason"])["projection_id"] == result["projection_id"]]
        self.assertEqual(len(gaps), len(sources))


class DecoderBoundaryTests(unittest.TestCase):
    """_json itself: overflow joins the existing ValueError refusals."""

    def test_overflow_raises_value_error_at_every_depth(self):
        for name, text in OVERFLOW_LOCATORS.items():
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "nonfinite"):
                projection._json(text)
        for text in ("1e309", "-1e309", "[1e309]", "[[[-2e308]]]", HUGE_FRACTION):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "nonfinite"):
                projection._json(text)

    def test_finite_values_zero_underflow_and_integers_unchanged(self):
        decoded = projection._json(FINITE_LOCATOR)
        self.assertEqual(decoded, json.loads(FINITE_LOCATOR))
        self.assertEqual(decoded["zero"], 0.0)
        self.assertEqual(math.copysign(1.0, decoded["neg_zero"]), -1.0)
        self.assertEqual(decoded["under"], 0.0)
        self.assertEqual(math.copysign(1.0, decoded["neg_under"]), -1.0)
        self.assertEqual(decoded["max"], 1.7976931348623157e308)
        self.assertIs(type(decoded["big_int"]), int)
        self.assertEqual(decoded["big_int"], int(HUGE_INT))
        self.assertEqual(decoded["neg_int"], -int(HUGE_INT))
        self.assertIs(type(decoded["nested"][1]["h"][1]), int)
        self.assertTrue(all(math.isfinite(value) for value in (
            decoded["max"], decoded["min"], decoded["nested"][0], decoded["nested"][1]["g"])))

    def test_existing_refusals_unchanged(self):
        for token in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(token=token), self.assertRaisesRegex(ValueError, "nonfinite JSON value"):
                projection._json('{"line":' + token + '}')
        with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
            projection._json('{"line":1,"line":2}')
        with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
            projection._json('{"a":[{"b":1,"b":1.5}]}')


class FinitePositiveTests(_Store):
    """A1: finite, zero, underflow and exact-integer locators keep projecting."""

    def test_a1_finite_nested_locator_projects_as_captured_unverified(self):
        locators = [FINITE_LOCATOR, '{"line":1e308}', '{"page":[0,-0.0,1e-320]}']
        self.set_units(locators)
        capture = self.capture()
        sources_before = self.legacy_units(capture)
        result = projection.project_units(self.database, capture)
        self.assertEqual(result["unresolved_reasons"], {PROJECTED: len(locators)})
        self.assertEqual(result["projected_source_rows"], len(locators))
        self.assert_accounted(result, capture)
        units = sorted(self.rows("units"), key=lambda unit: unit["legacy_ordinal"])
        self.assertEqual([unit["locator"] for unit in units], locators)
        for unit in units:
            self.assertEqual(unit["status"], "captured_unverified")
            self.assertIsNone(unit["parser"])
            self.assertIsNone(unit["parser_version"])
            provenance = json.loads(unit["provenance_json"])
            self.assertEqual(provenance["parser_provenance"], "missing")
            self.assertFalse(provenance["original_parser_verified"])
        self.assertEqual(self.legacy_units(capture), sources_before)

    def test_a1_valid_replay_is_a_noop(self):
        self.set_units([FINITE_LOCATOR])
        capture = self.capture()
        first = projection.project_units(self.database, capture)
        tables = ("units", "unit_projection_rows", "unit_projection_checkpoints", "import_gaps")
        before = {table: self.rows(table) for table in tables}
        stages = {table: self.rows(table) for table in ("stage_state", "stage_events")}
        second = projection.project_units(self.database, capture)
        self.assertTrue(second["reused"])
        self.assertEqual(second["projection_id"], first["projection_id"])
        self.assertEqual(second["stage_promotions"], 0)
        for table in tables:
            self.assertEqual(self.rows(table), before[table])
        for table, rows in stages.items():
            self.assertEqual(self.rows(table), rows)


class OverflowRefusalTests(_Store):
    """A2: overflow in a locator or the outer payload is refused, never projected."""

    def test_a2_overflow_locators_block_without_units(self):
        names = list(OVERFLOW_LOCATORS)
        locators = [OVERFLOW_LOCATORS[name] for name in names]
        self.set_units(locators)
        capture = self.capture()
        sources_before = self.legacy_units(capture)
        result = projection.project_units(self.database, capture)
        self.assertEqual(result["unresolved_reasons"], {BLOCKED_LOCATOR: len(locators)})
        self.assertEqual(result["projected_source_rows"], 0)
        self.assertEqual(result["blocked_source_rows"], len(locators))
        self.assert_accounted(result, capture)
        self.assertEqual(self.rows("units"), [])
        for name, locator, row in zip(names, locators, self.evidence()):
            with self.subTest(name=name):
                self.assertEqual(row["disposition"], "blocked")
                self.assertEqual(row["reason"], BLOCKED_LOCATOR)
                self.assertIsNone(row["unit_id"])
                self.assertEqual(row["observed_locator"], locator)
                self.assertIsNone(row["observed_text_sha256"])
        self.assertEqual(self.legacy_units(capture), sources_before)

    def test_a2_root_level_locator_number_remains_invalid(self):
        self.set_units(["1e309", "-1e309", "[1e309]"])
        result = projection.project_units(self.database, self.capture())
        self.assertEqual(result["unresolved_reasons"], {BLOCKED_LOCATOR: 3})
        self.assertEqual(self.rows("units"), [])

    def test_a2_outer_payload_overflow_is_invalid_captured_json(self):
        self.set_units(['{"line":1}'])
        base = self.capture()
        cases = [
            ("root_number", ('{"ordinal":0,"sha":"%s"}' % self.sha, "1e309")),
            ("root_member", self.payload(1, ',"overflow":1e309')),
            ("negative_member", self.payload(2, ',"overflow":-1E+999')),
            ("nested_member", self.payload(3, ',"extra":{"deep":[[0,' + HUGE_FRACTION + ']]}')),
        ]
        clone = self.clone_capture(base, [payload for _, payload in cases])
        sources_before = self.legacy_units(clone)
        result = projection.project_units(self.database, clone)
        self.assertEqual(result["unresolved_reasons"], {INVALID_JSON: len(cases)})
        self.assert_accounted(result, clone)
        self.assertEqual(self.rows("units"), [])
        for (name, (_, payload)), row, source in zip(cases, self.evidence(), sources_before):
            with self.subTest(name=name):
                self.assertEqual(row["reason"], INVALID_JSON)
                self.assertIsNone(row["unit_id"])
                self.assertIsNone(row["observed_text"])
                self.assertIsNone(row["observed_locator"])
                self.assertEqual(source["payload_json"], payload)
        self.assertEqual(self.legacy_units(clone), sources_before)

    def test_a2_existing_token_and_duplicate_refusals_unchanged(self):
        locators = ['{"line":NaN}', '{"line":Infinity}', '{"line":-Infinity}',
                    '{"line":1,"line":2}', '{"a":{"b":1,"b":2}}']
        self.set_units(locators)
        result = projection.project_units(self.database, self.capture())
        self.assertEqual(result["unresolved_reasons"], {BLOCKED_LOCATOR: len(locators)})
        self.assertEqual(self.rows("units"), [])
        self.assertEqual([row["observed_locator"] for row in self.evidence()], locators)


MIXED = [
    '{"line":1}',
    OVERFLOW_LOCATORS["root_positive_exponent"],
    FINITE_LOCATOR,
    OVERFLOW_LOCATORS["nested_array"],
    '{"line":NaN}',
    '{"line":2,"under":1e-400}',
    OVERFLOW_LOCATORS["ignored_field"],
]
MIXED_HISTOGRAM = {PROJECTED: 3, BLOCKED_LOCATOR: 4}


class MixedBatchRecoveryTests(_Store):
    """A3: mixed batches keep exact histograms across fresh run, restart and replay."""

    def _check(self, result, capture):
        self.assertEqual(result["unresolved_reasons"], MIXED_HISTOGRAM)
        self.assertEqual(result["projected_source_rows"], 3)
        self.assertEqual(result["blocked_source_rows"], 4)
        self.assert_accounted(result, capture)
        units = self.rows("units")
        self.assertEqual(sorted(unit["legacy_ordinal"] for unit in units), [0, 2, 5])
        reasons = [row["reason"] for row in self.evidence()]
        self.assertEqual(reasons, [PROJECTED, BLOCKED_LOCATOR, PROJECTED, BLOCKED_LOCATOR,
                                   BLOCKED_LOCATOR, PROJECTED, BLOCKED_LOCATOR])

    def test_a3_fresh_single_run(self):
        self.set_units(MIXED)
        capture = self.capture()
        result = projection.project_units(self.database, capture)
        self.assertEqual(result["batches"], 1)
        self._check(result, capture)

    def test_a3_restart_with_unchanged_limits(self):
        self.set_units(MIXED)
        capture = self.capture()
        first = projection.project_units(self.database, capture, batch_size=2)
        self.assertEqual(first["status"], "in_progress")
        self.assertEqual(first["unresolved_reasons"], {PROJECTED: 1, BLOCKED_LOCATOR: 1})
        second = projection.project_units(self.database, capture, batch_size=2, max_batches=1)
        self.assertEqual(second["processed_source_rows"], 4)
        final = projection.project_units(self.database, capture, batch_size=2, max_batches=10)
        self.assertEqual(final["projection_id"], first["projection_id"])
        self.assertEqual(final["batches"], 4)
        self._check(final, capture)
        checkpoints = self.rows("unit_projection_checkpoints", " ORDER BY batch_number")
        self.assertEqual(len(checkpoints), 4)
        for previous, current in zip(checkpoints, checkpoints[1:]):
            self.assertEqual(current["previous_sha256"], previous["checkpoint_sha256"])
        before = {table: self.rows(table) for table in (
            "units", "unit_projection_rows", "unit_projection_checkpoints", "import_gaps")}
        replay = projection.project_units(self.database, capture, batch_size=2)
        self.assertTrue(replay["reused"])
        self.assertEqual(replay["unresolved_reasons"], MIXED_HISTOGRAM)
        for table, rows in before.items():
            self.assertEqual(self.rows(table), rows)

    def test_a3_interrupted_batch_rolls_back_then_resumes(self):
        self.set_units(MIXED)
        capture = self.capture()
        projection.project_units(self.database, capture, batch_size=1)
        original = projection._project_row

        def interrupted(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("synthetic interruption")

        with mock.patch.object(projection, "_project_row", side_effect=interrupted):
            with self.assertRaises(RuntimeError):
                projection.project_units(self.database, capture, batch_size=3)
        self.assertEqual(len(self.evidence()), 1)
        self.assertEqual(len(self.rows("unit_projection_checkpoints")), 1)
        result = projection.project_units(self.database, capture, batch_size=3, max_batches=10)
        self._check(result, capture)

    def test_a3_mixed_outer_and_locator_overflow(self):
        self.set_units(['{"line":1}'])
        base = self.capture()
        payloads = [
            self.payload(0),
            self.payload(1, ',"overflow":1e309'),
            ('{"ordinal":2,"sha":"%s"}' % self.sha,
             self.payload(2)[1].replace('{\\"line\\":1}', '{\\"line\\":1,\\"z\\":[1e309]}')),
            self.payload(3, ',"under":1e-400,"neg_zero":-0.0,"int":' + HUGE_INT),
        ]
        clone = self.clone_capture(base, payloads)
        result = projection.project_units(self.database, clone, batch_size=1, max_batches=10)
        self.assertEqual(result["unresolved_reasons"],
                         {PROJECTED: 2, INVALID_JSON: 1, BLOCKED_LOCATOR: 1})
        self.assert_accounted(result, clone)
        self.assertEqual(sorted(unit["legacy_ordinal"] for unit in self.rows("units")), [0, 3])


if __name__ == "__main__":
    unittest.main()
