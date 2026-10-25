"""FA41: duplicate JSON member names are refused before portal reconciliation.

tools/portal_reconcile.py reads two operator-supplied JSON exports. A JSON
object may legally repeat a member name, and the standard decoder silently
keeps the last value, so a row such as {"allowlisted": false, "allowlisted":
true} would be read as allowlisted. These tests pin the frozen behavior: a
repeated member name anywhere in either raw input file (even with identical
values) is the fixed InputError code duplicate_json_key, the CLI exits 2 with
only that aggregate-safe error, and no report is created or overwritten.
Ordinary duplicate observations across separate rows keep their existing
deduplication/conflict accounting.

Synthetic data only: example.test hosts and low-entropy placeholder hashes.
No mailbox, queue store, ledger, allowlist or network is touched. All paths
derive from this file; inputs are written to fresh temporary directories.
"""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tools import portal_reconcile
from tools.portal_reconcile import InputError, read_json, reconcile

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "portal_reconcile"
OBSERVATIONS = FIXTURES / "observations.json"
QUEUE = FIXTURES / "queue-export.json"
SCRIPT = ROOT / "tools" / "portal_reconcile.py"

OBS_SCHEMA = portal_reconcile.OBSERVATION_SCHEMA
QUEUE_SCHEMA = portal_reconcile.QUEUE_SCHEMA
HOST = "records.example.test"
DUPLICATE = "duplicate_json_key"
ERROR_LINE = json.dumps({"error": DUPLICATE}) + "\n"

# Strings that must never appear in an error emitted for a refused input.
MARKERS = (HOST, "1" * 64, "2" * 64, "26-201", "doc-fa41", "allowlisted", "has_version",
           "synthetic-private-filename", "jane.doe@example.test", "synthetic subject marker",
           "https", "/documents")

FIXTURE_CLASSES = {"fetched": 3, "queued": 2, "failed": 2, "unproven": 4, "held_policy": 4,
                   "held_redirect": 1, "missing_request_identity": 1}


def j(value):
    return json.dumps(value)


def obj(*pairs):
    """Raw JSON object text from (name, raw-json-value) pairs; names may repeat."""
    return "{" + ", ".join(j(name) + ": " + raw for name, raw in pairs) + "}"


def arr(*raws):
    return "[" + ", ".join(raws) + "]"


def obs_pairs(url_char="1", allowlisted="true"):
    return [("host_class", j("nextrequest")), ("host", j(HOST)), ("request_id", j("26-201")),
            ("item_id", j("doc-fa41")), ("url_sha256", j(url_char * 64)), ("allowlisted", allowlisted)]


def queue_pairs(state="received"):
    return [("host", j(HOST)), ("request_id", j("26-201")), ("item_id", j("doc-fa41")),
            ("state", j(state))]


def obs_doc(*rows, extra=()):
    return obj(("schema", j(OBS_SCHEMA)), ("observations", arr(*rows)), *extra)


def queue_doc(*rows, extra=()):
    return obj(("schema", j(QUEUE_SCHEMA)), ("items", arr(*rows)), *extra)


VALID_OBS = obs_doc(obj(*obs_pairs()))
VALID_QUEUE = queue_doc(obj(*queue_pairs()))

# (label, which file, raw text). Every case repeats at least one member name.
OBSERVATION_CASES = (
    ("top_schema_identical", obj(("schema", j(OBS_SCHEMA)), ("schema", j(OBS_SCHEMA)),
                                 ("observations", arr(obj(*obs_pairs()))))),
    ("top_schema_other_then_valid", obj(("schema", j("other/v1")), ("schema", j(OBS_SCHEMA)),
                                        ("observations", arr(obj(*obs_pairs()))))),
    ("top_schema_valid_then_other", obj(("schema", j(OBS_SCHEMA)), ("schema", j("other/v1")),
                                        ("observations", arr(obj(*obs_pairs()))))),
    ("top_rows_twice", obj(("schema", j(OBS_SCHEMA)), ("observations", arr()),
                           ("observations", arr(obj(*obs_pairs()))))),
    ("row_allowlisted_false_true", obs_doc(obj(*obs_pairs(allowlisted="false"), ("allowlisted", "true")))),
    ("row_allowlisted_true_false", obs_doc(obj(*obs_pairs(allowlisted="true"), ("allowlisted", "false")))),
    ("row_allowlisted_identical", obs_doc(obj(*obs_pairs(allowlisted="true"), ("allowlisted", "true")))),
    ("row_url_sha256_twice", obs_doc(obj(*obs_pairs(), ("url_sha256", j("2" * 64))))),
    # The second name is written with a JSON escape; it decodes to the same name.
    ("row_escaped_name_equivalent", obs_doc(obj(*obs_pairs(allowlisted="false"))[:-1]
                                            + ', "allow\\u006cisted": true}')),
    ("ignored_nested_object", obs_doc(obj(*obs_pairs()), extra=(
        ("note", obj(("subject", j("synthetic subject marker")), ("subject", j("synthetic subject marker")))),))),
    ("deep_nested_in_row_list", obs_doc(obj(*obs_pairs()), arr(obj(("k", "1"), ("k", "2"))))),
)
QUEUE_CASES = (
    ("top_schema_identical", obj(("schema", j(QUEUE_SCHEMA)), ("schema", j(QUEUE_SCHEMA)),
                                 ("items", arr(obj(*queue_pairs()))))),
    ("top_items_twice", obj(("schema", j(QUEUE_SCHEMA)), ("items", arr(obj(*queue_pairs()))), ("items", arr()))),
    ("row_state_twice", queue_doc(obj(*queue_pairs("blocked"), ("state", j("received"))))),
    ("row_state_identical", queue_doc(obj(*queue_pairs("received"), ("state", j("received"))))),
    ("row_has_version_false_true", queue_doc(obj(*queue_pairs("pending"), ("has_version", "false"),
                                                 ("has_version", "true")))),
    ("ignored_nested_object", queue_doc(obj(*queue_pairs()), extra=(
        ("meta", obj(("filename", j("synthetic-private-filename.pdf")),
                     ("filename", j("synthetic-private-filename.pdf")))),))),
)


def write(directory, name, text):
    path = Path(directory) / name
    path.write_text(text, encoding="utf-8")
    return path


def invoke(*args):
    return subprocess.run([sys.executable, "-B", str(SCRIPT), *args], capture_output=True, text=True,
                          cwd=ROOT, check=False)


class ReadJsonDuplicateRefusalTests(unittest.TestCase):
    """A2 (in-process, complete imported read_json): each case must refuse.

    These are the deciding regressions for the A4 negative control: with the
    new duplicate rejection disabled, every test here must fail.
    """

    def assert_refused(self, text, schema, list_key):
        with tempfile.TemporaryDirectory() as tmp:
            path = write(tmp, "input.json", text)
            with self.assertRaises(InputError) as caught:
                read_json(str(path), schema, list_key)
        self.assertEqual(str(caught.exception), DUPLICATE)
        self.assertEqual(caught.exception.args, (DUPLICATE,))

    def test_contradictory_allowlisted_false_true_refused(self):
        self.assert_refused(dict(OBSERVATION_CASES)["row_allowlisted_false_true"], OBS_SCHEMA, "observations")

    def test_contradictory_allowlisted_true_false_refused(self):
        self.assert_refused(dict(OBSERVATION_CASES)["row_allowlisted_true_false"], OBS_SCHEMA, "observations")

    def test_identical_duplicate_values_refused(self):
        self.assert_refused(dict(OBSERVATION_CASES)["row_allowlisted_identical"], OBS_SCHEMA, "observations")
        self.assert_refused(dict(QUEUE_CASES)["row_state_identical"], QUEUE_SCHEMA, "items")
        self.assert_refused(dict(OBSERVATION_CASES)["top_schema_identical"], OBS_SCHEMA, "observations")

    def test_every_observation_case_refused(self):
        for label, text in OBSERVATION_CASES:
            with self.subTest(case=label):
                self.assert_refused(text, OBS_SCHEMA, "observations")

    def test_every_queue_case_refused(self):
        for label, text in QUEUE_CASES:
            with self.subTest(case=label):
                self.assert_refused(text, QUEUE_SCHEMA, "items")

    def test_queue_state_and_has_version_refused(self):
        self.assert_refused(dict(QUEUE_CASES)["row_state_twice"], QUEUE_SCHEMA, "items")
        self.assert_refused(dict(QUEUE_CASES)["row_has_version_false_true"], QUEUE_SCHEMA, "items")

    def test_ignored_nested_object_refused(self):
        self.assert_refused(dict(OBSERVATION_CASES)["ignored_nested_object"], OBS_SCHEMA, "observations")
        self.assert_refused(dict(QUEUE_CASES)["ignored_nested_object"], QUEUE_SCHEMA, "items")

    def test_duplicate_refused_at_existing_row_bound(self):
        # A3: a file exactly at the (patched) row limit is still refused for a duplicate.
        rows = [obj(*obs_pairs(str(n))) for n in range(1, 3)]
        rows.append(obj(*obs_pairs("3", allowlisted="false"), ("allowlisted", "true")))
        with mock.patch.object(portal_reconcile, "MAX_ROWS", 3):
            self.assert_refused(obs_doc(*rows), OBS_SCHEMA, "observations")


class ReadJsonPreservedBehaviorTests(unittest.TestCase):
    """A1/A3: valid input, malformed JSON and the existing bounds are unchanged."""

    def read(self, text, schema=OBS_SCHEMA, list_key="observations"):
        with tempfile.TemporaryDirectory() as tmp:
            return read_json(str(write(tmp, "input.json", text)), schema, list_key)

    def code(self, text, schema=OBS_SCHEMA, list_key="observations"):
        with self.assertRaises(InputError) as caught:
            self.read(text, schema, list_key)
        return str(caught.exception)

    def test_fixture_exports_read_completely(self):
        observations = read_json(str(OBSERVATIONS), OBS_SCHEMA, "observations")
        queue = read_json(str(QUEUE), QUEUE_SCHEMA, "items")
        self.assertEqual((len(observations), len(queue)), (29, 15))
        self.assertEqual(observations, json.loads(OBSERVATIONS.read_text())["observations"])
        self.assertEqual(queue, json.loads(QUEUE.read_text())["items"])
        self.assertEqual(reconcile(observations, queue)["classes"], FIXTURE_CLASSES)

    def test_valid_rows_returned_as_plain_dicts(self):
        rows = self.read(VALID_OBS)
        self.assertEqual(rows, [json.loads(obj(*obs_pairs()))])
        self.assertIs(type(rows[0]), dict)
        self.assertIs(rows[0]["allowlisted"], True)

    def test_empty_exports(self):
        self.assertEqual(self.read(obs_doc()), [])
        self.assertEqual(self.read(queue_doc(), QUEUE_SCHEMA, "items"), [])

    def test_valid_unicode_is_not_a_duplicate(self):
        # Distinct names that differ only by a combining mark are different names.
        row = obj(*obs_pairs(), ("allowlisted\u0301", "true"))
        text = obs_doc(row, extra=(("note", j("caf\u00e9 \u2713")),))
        rows = self.read(text)
        self.assertEqual(len(rows), 1)
        report = reconcile(rows, [])
        self.assertEqual(report["mail"]["malformed_reasons"], {"unknown_field": 1})

    def test_malformed_json_keeps_its_code(self):
        self.assertEqual(self.code('{"schema": '), "input_not_json")
        self.assertEqual(self.code("{not json"), "input_not_json")
        self.assertEqual(self.code(b"\xff\xfe".decode("latin-1")), "input_not_json")
        # A syntax error is still input_not_json even after a repeated name.
        self.assertEqual(self.code('{"schema": "a", "schema": "b", '), "input_not_json")

    def test_existing_fatal_codes_unchanged(self):
        self.assertEqual(self.code(obj(("schema", j("other/v1")), ("observations", arr()))), "input_schema_mismatch")
        self.assertEqual(self.code(arr()), "input_schema_mismatch")
        self.assertEqual(self.code(obj(("schema", j(OBS_SCHEMA)))), "input_rows_missing")

    def test_valid_but_malformed_rows_are_still_counted(self):
        rows = self.read(obs_doc(obj(*obs_pairs(), ("subject", j("x"))), "42", obj(*obs_pairs("2"))))
        report = reconcile(rows, [])
        self.assertEqual(report["mail"]["malformed_reasons"], {"not_object": 1, "unknown_field": 1})
        self.assertEqual(report["mail"]["unique_urls"], 1)

    def test_row_limit_seam_and_one_over(self):
        rows = [obj(*obs_pairs(str(n))) for n in range(1, 4)]
        with mock.patch.object(portal_reconcile, "MAX_ROWS", 3):
            self.assertEqual(len(self.read(obs_doc(*rows))), 3)
            self.assertEqual(self.code(obs_doc(*rows, obj(*obs_pairs("4")))), "input_too_many_rows")

    def test_byte_limit_seam_and_one_over(self):
        text = VALID_OBS
        size = len(text.encode("utf-8"))
        with mock.patch.object(portal_reconcile, "MAX_INPUT_BYTES", size):
            self.assertEqual(len(self.read(text)), 1)
        with mock.patch.object(portal_reconcile, "MAX_INPUT_BYTES", size - 1):
            self.assertEqual(self.code(text), "input_too_large")
        # The size bound is still decided before any decoding, duplicate or not.
        duplicate = dict(OBSERVATION_CASES)["row_allowlisted_false_true"]
        with mock.patch.object(portal_reconcile, "MAX_INPUT_BYTES", len(duplicate.encode()) - 1):
            self.assertEqual(self.code(duplicate), "input_too_large")


class SeparateRowAccountingTests(unittest.TestCase):
    """A1: duplicate observations across separate rows keep existing accounting."""

    def test_separate_held_observation_remains_held(self):
        # Real classifier: an explicitly held observation stays held next to a
        # receipt for the same identity. Used as the A4 restored/broken positive.
        held = obj(*obs_pairs("1", allowlisted="false"))
        govqa = obj(("host_class", j("govqa")), ("host", j(HOST)), ("request_id", j("26-201")),
                    ("item_id", j("doc-fa41")), ("url_sha256", j("2" * 64)), ("allowlisted", "true"))
        with tempfile.TemporaryDirectory() as tmp:
            rows = read_json(str(write(tmp, "obs.json", obs_doc(held, govqa))), OBS_SCHEMA, "observations")
            queue = read_json(str(write(tmp, "queue.json", queue_doc(obj(*queue_pairs(), ("has_version", "true"))))),
                              QUEUE_SCHEMA, "items")
        report = reconcile(rows, queue)
        self.assertEqual(report["classes"]["held_policy"], 2)
        self.assertEqual(report["classes"]["fetched"], 0)
        self.assertEqual(report["detail"]["fetched_queue_items"], 0)

    def test_identical_separate_rows_are_deduplicated(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = read_json(str(write(tmp, "obs.json", obs_doc(obj(*obs_pairs()), obj(*obs_pairs())))),
                             OBS_SCHEMA, "observations")
        report = reconcile(rows, [json.loads(obj(*queue_pairs()))])
        self.assertEqual(report["mail"]["duplicate_observations"], 1)
        self.assertEqual(report["mail"]["unique_urls"], 1)
        self.assertEqual(report["classes"]["fetched"], 1)

    def test_contradictory_separate_rows_stay_conflicting(self):
        rows_text = (obj(*obs_pairs(allowlisted="false")), obj(*obs_pairs(allowlisted="true")))
        for order in (rows_text, tuple(reversed(rows_text))):
            with self.subTest(first=order[0][-6:]), tempfile.TemporaryDirectory() as tmp:
                rows = read_json(str(write(tmp, "obs.json", obs_doc(*order))), OBS_SCHEMA, "observations")
                report = reconcile(rows, [json.loads(obj(*queue_pairs()))])
                self.assertEqual(report["mail"]["conflicting_urls"], 1)
                self.assertEqual(sum(report["classes"].values()), 0)

    def test_duplicate_queue_identity_rows_stay_ambiguous(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = read_json(str(write(tmp, "q.json", queue_doc(obj(*queue_pairs()), obj(*queue_pairs())))),
                              QUEUE_SCHEMA, "items")
        report = reconcile([json.loads(obj(*obs_pairs()))], queue)
        self.assertEqual(report["detail"]["unproven"]["queue_identity_ambiguous"], 1)
        self.assertEqual(report["queue"]["malformed_reasons"], {"duplicate_identity": 1})


class CliIntegrationTests(unittest.TestCase):
    """A2/A3/A5: the full CLI (subprocess) and in-process main on generated exports."""

    def assert_refusal(self, result):
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, ERROR_LINE)
        for marker in MARKERS:
            self.assertNotIn(marker, result.stdout + result.stderr)

    def test_cli_refuses_every_observation_case(self):
        for label, text in OBSERVATION_CASES:
            with self.subTest(case=label), tempfile.TemporaryDirectory() as tmp:
                obs = write(tmp, "synthetic-private-filename-obs.json", text)
                que = write(tmp, "queue.json", VALID_QUEUE)
                out = Path(tmp) / "report.json"
                self.assert_refusal(invoke("--observations", str(obs), "--queue-export", str(que),
                                           "--output", str(out)))
                self.assertFalse(out.exists())

    def test_cli_refuses_every_queue_case(self):
        for label, text in QUEUE_CASES:
            with self.subTest(case=label), tempfile.TemporaryDirectory() as tmp:
                obs = write(tmp, "obs.json", VALID_OBS)
                que = write(tmp, "synthetic-private-filename-queue.json", text)
                self.assert_refusal(invoke("--observations", str(obs), "--queue-export", str(que)))

    def test_seeded_prior_output_stays_byte_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            obs = write(tmp, "obs.json", VALID_OBS)
            que = write(tmp, "queue.json", VALID_QUEUE)
            out = Path(tmp) / "report.json"
            first = invoke("--observations", str(obs), "--queue-export", str(que), "--output", str(out))
            self.assertEqual((first.returncode, first.stdout, first.stderr), (0, "", ""))
            saved = out.read_bytes()
            self.assertEqual(json.loads(saved)["classes"]["fetched"], 1)
            bad = write(tmp, "bad.json", dict(OBSERVATION_CASES)["row_allowlisted_false_true"])
            self.assert_refusal(invoke("--observations", str(bad), "--queue-export", str(que),
                                       "--output", str(out)))
            self.assertEqual(out.read_bytes(), saved)
            seeded = write(tmp, "seeded.json", "seeded prior output\n")
            self.assert_refusal(invoke("--observations", str(obs), "--queue-export",
                                       str(write(tmp, "badq.json", dict(QUEUE_CASES)["row_state_twice"])),
                                       "--output", str(seeded)))
            self.assertEqual(seeded.read_bytes(), b"seeded prior output\n")

    def test_in_process_main_refuses_with_fixed_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = write(tmp, "obs.json", dict(OBSERVATION_CASES)["row_allowlisted_false_true"])
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = portal_reconcile.main(["--observations", str(bad), "--queue-export", str(QUEUE)])
        self.assertEqual((code, stdout.getvalue(), stderr.getvalue()), (2, "", ERROR_LINE))

    def test_cli_fixture_totals_and_byte_identical_repeats(self):
        with tempfile.TemporaryDirectory() as tmp:
            outputs = []
            for name in ("first.json", "second.json"):
                out = Path(tmp) / name
                result = invoke("--observations", str(OBSERVATIONS), "--queue-export", str(QUEUE), "--output", str(out))
                self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
                outputs.append(out.read_bytes())
        stdout = invoke("--observations", str(OBSERVATIONS), "--queue-export", str(QUEUE))
        self.assertEqual(stdout.returncode, 0)
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(outputs[0], stdout.stdout.encode("ascii"))
        report = json.loads(outputs[0])
        self.assertEqual(report["classes"], FIXTURE_CLASSES)
        self.assertEqual((report["mail"]["malformed_rows"], report["queue"]["malformed_rows"]), (9, 6))
        self.assertEqual((report["mail"]["duplicate_observations"], report["mail"]["conflicting_urls"]), (2, 1))

    def test_cli_empty_and_unicode_exports(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Raw (unescaped) UTF-8 text in an ignored member and a folded reason.
            obs = write(tmp, "obs.json", obs_doc(extra=(("note", json.dumps("caf\u00e9 \u2713", ensure_ascii=False)),)))
            que = write(tmp, "queue.json", queue_doc(obj(*queue_pairs("blocked"),
                                                         ("reason", json.dumps("d\u00e9lai", ensure_ascii=False)))))
            result = invoke("--observations", str(obs), "--queue-export", str(que))
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        report = json.loads(result.stdout)
        self.assertEqual(sum(report["classes"].values()), 0)
        self.assertEqual(report["queue"]["failed_reasons"], {"unrecognized_reason": 1})
        self.assertTrue(result.stdout.isascii())

    def test_cli_malformed_json_and_rows_keep_distinct_behavior(self):
        with tempfile.TemporaryDirectory() as tmp:
            broken = write(tmp, "broken.json", '{"schema": "a", "schema": "b", ')
            result = invoke("--observations", str(broken), "--queue-export", str(QUEUE))
            self.assertEqual((result.returncode, result.stdout), (2, ""))
            self.assertEqual(json.loads(result.stderr), {"error": "input_not_json"})
            rows = write(tmp, "rows.json", obs_doc(obj(*obs_pairs(), ("subject", j("x")))))
            result = invoke("--observations", str(rows), "--queue-export", str(QUEUE))
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertEqual(json.loads(result.stdout)["mail"]["malformed_reasons"], {"unknown_field": 1})


if __name__ == "__main__":
    unittest.main()
