"""FA21: run-report durations must be finite numbers (synthetic data only).

Machine-valid here means REVIEW REQUIRED only. A valid report is not human
review, a legal conclusion, a data-source approval or a stage promotion.

Assertion map (frozen acceptance):
  A1 positive path      FinitePositiveTests
  A2 negative path      NonfiniteRejectionTests, PreservedFailureTests
  A3 boundary/recovery  ToleranceBoundaryTests
  A4 broken control     BrokenControlTests
  A5 integration        ConsumerIntegrationTests
  A6 security           every test (OfflineCase), ChildGuardTests, PreservationTests
"""
import ast
import copy
import hashlib
import json
import math
import os
import re
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from campaign_tool.records import run_coverage, run_report  # noqa: E402
from tools import accept_fa21_report_finite as accept  # noqa: E402
from tools import run_report_check  # noqa: E402

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "run_report"
PASS = FIXTURES / "pass.json"
BASELINE = FIXTURES / "baseline-20261023.json"
EXAMPLE = REPO_ROOT / "examples" / "run-report" / "records-run-report-v1.synthetic.json"
COVERAGE = REPO_ROOT / "examples" / "run-report" / "coverage-projection.synthetic.json"
CHECK_SCRIPT = REPO_ROOT / "tools" / "run_report_check.py"
RETAINED = (PASS, BASELINE, EXAMPLE, COVERAGE)
NONFINITE = (("nan", float("nan")), ("inf", float("inf")), ("-inf", float("-inf")))
FINITE_DIAGNOSTIC = "must be a finite number"
VALUE_TEXT = re.compile(r"\b(nan|-?inf(inity)?)\b|1e999", re.I)
VALIDATOR = REPO_ROOT / "campaign_tool" / "records" / "run_report.py"
ALLOWED_VALIDATOR_IMPORTS = {"json", "math", "datetime", "pathlib", "campaign_tool"}

NETWORK_ATTEMPTS = []


def _refuse_network(name):
    def refused(*args, **kwargs):
        NETWORK_ATTEMPTS.append(name)
        raise OSError("network disabled in FA21 synthetic tests")
    return refused


_GUARDS = [
    mock.patch.object(socket.socket, "connect", _refuse_network("socket.connect")),
    mock.patch.object(socket.socket, "connect_ex", _refuse_network("socket.connect_ex")),
    mock.patch.object(socket, "create_connection", _refuse_network("create_connection")),
    mock.patch.object(socket, "getaddrinfo", _refuse_network("getaddrinfo")),
]
_STATE = {}


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _repo_listing():
    """Every repository file (outside .git) with its size and mtime: any created,
    removed or rewritten file changes this snapshot."""
    listing = {}
    for current, dirs, files in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs if d != ".git"]
        for name in files:
            path = Path(current, name)
            stat = path.lstat()
            listing[str(path.relative_to(REPO_ROOT))] = (stat.st_size, stat.st_mtime_ns)
    return listing


def setUpModule():
    for guard in _GUARDS:
        guard.start()
    _STATE["hashes"] = {str(p): _sha256(p) for p in RETAINED}
    _STATE["listing"] = _repo_listing()
    # Per-run root: the acceptance helper points tempfile at a fresh directory.
    _STATE["root"] = Path(tempfile.mkdtemp(prefix="fa21-"))


def tearDownModule():
    for guard in reversed(_GUARDS):
        guard.stop()


def load_pass():
    return json.loads(PASS.read_text(encoding="utf-8"))


def load_example():
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


def load_baseline():
    return json.loads(BASELINE.read_text(encoding="utf-8"))


def coverage_case(name="a_live_shaped_scheduled_run"):
    cases = json.loads(COVERAGE.read_text(encoding="utf-8"))
    return copy.deepcopy(next(c for c in cases if c["name"] == name))


def positions(report):
    """Every numeric duration position: top level and each stage."""
    yield "$.duration_seconds", lambda r, v: r.__setitem__("duration_seconds", v)
    for index in range(len(report["stages"])):
        yield (f"$.stages[{index}].duration_seconds",
               lambda r, v, i=index: r["stages"][i].__setitem__("duration_seconds", v))


def nonfinite_regression(validate, schema, base=None):
    """Return the positions/values a validator wrongly handles; [] when correct.

    Every mutated report is otherwise schema-valid, so only the finite gate can
    decide it. Correct handling: ValueError naming the position with the finite
    diagnostic and never echoing the value.
    """
    base = base if base is not None else load_example()
    validate(copy.deepcopy(base), schema)  # the unmutated control must pass
    misses = []
    for where, setter in positions(base):
        for label, value in NONFINITE:
            report = copy.deepcopy(base)
            setter(report, value)
            try:
                validate(report, schema)
            except ValueError as exc:
                text = str(exc)
                if FINITE_DIAGNOSTIC not in text or not text.startswith(where):
                    misses.append((where, label, "wrong diagnostic"))
                elif VALUE_TEXT.search(text):
                    misses.append((where, label, "value leaked"))
            else:
                misses.append((where, label, "accepted"))
    return misses


class OfflineCase(unittest.TestCase):
    """A6: no socket use, no writes outside the per-run root, fixtures intact."""

    def setUp(self):
        self.schema = run_report.load_schema()
        self.root = Path(tempfile.mkdtemp(dir=_STATE["root"]))

    def tearDown(self):
        self.assertEqual(NETWORK_ATTEMPTS, [])
        # Under the acceptance helper these are its live in-process guard records.
        self.assertEqual(accept.NETWORK_ATTEMPTS, [])
        self.assertEqual(accept.WRITE_VIOLATIONS, [])
        for path, digest in _STATE["hashes"].items():
            self.assertEqual(_sha256(path), digest, path)
        self.assertEqual(_repo_listing(), _STATE["listing"])

    def assertFiniteRejected(self, report, where):
        with self.assertRaises(ValueError) as caught:
            run_report.validate_run_report(report, self.schema)
        text = str(caught.exception)
        self.assertTrue(text.startswith(where), text)
        self.assertIn(FINITE_DIAGNOSTIC, text)
        self.assertIsNone(VALUE_TEXT.search(text), text)

    def assertInvalid(self, report, fragment):
        with self.assertRaises(ValueError) as caught:
            run_report.validate_run_report(report, self.schema)
        self.assertIn(fragment, str(caught.exception))

    def write(self, text, name="run-report.json"):
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return path

    def child(self, *argv):
        """Run a Python CLI through the guarded file-based launcher (guards before import)."""
        ledger = self.root / "child-ledger.jsonl"
        result = launch(self.root / "child", ledger, ["-B", *argv])
        self.assertEqual(accept.ledger_entries(ledger), [])
        return result


def launch(root, ledger, rest):
    env = dict(os.environ, TMPDIR=str(root), PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run(accept.launcher_argv(root, ledger, rest), cwd=REPO_ROOT, env=env,
                          capture_output=True, text=True, timeout=60)


class FinitePositiveTests(OfflineCase):
    """A1."""

    def test_existing_pass_fixture_finite_zero_is_valid(self):
        report = load_pass()
        self.assertEqual(report["duration_seconds"], 0)
        self.assertIs(run_report.validate_run_report(report, self.schema), report)

    def test_integer_and_fractional_durations_are_valid(self):
        report = load_example()
        self.assertIsInstance(report["duration_seconds"], int)
        self.assertTrue(any(isinstance(s["duration_seconds"], float) for s in report["stages"]))
        run_report.validate_run_report(report, self.schema)
        report["duration_seconds"] = 450.75
        report["stages"][0]["duration_seconds"] = 0.0
        run_report.validate_run_report(report, self.schema)

    def test_skipped_null_valid_and_non_skipped_null_rejected(self):
        report = load_example()
        report["stages"][1].update(status="skipped", duration_seconds=None)
        run_report.validate_run_report(report, self.schema)
        report = load_example()
        report["stages"][1]["duration_seconds"] = None
        self.assertInvalid(report, "null only allowed for skipped stages")

    def test_pass_remains_not_production_acceptance(self):
        code, lines = run_report_check.evaluate(load_pass(), load_baseline())
        text = "\n".join(lines)
        self.assertEqual(code, 0)
        self.assertIn("not production acceptance", text)
        self.assertIn("P1 FAIL", text)
        self.assertIn("P2 FAIL", text)
        for number in range(3, 10):
            self.assertIn(f"P{number} NA", text)
        self.assertIn("review required", text)


class NonfiniteRejectionTests(OfflineCase):
    """A2: NaN and both infinities in every duration position."""

    def test_every_position_and_value_is_rejected(self):
        base = load_example()
        count = 0
        for where, setter in positions(base):
            for label, value in NONFINITE:
                with self.subTest(where=where, value=label):
                    report = copy.deepcopy(base)
                    setter(report, value)
                    self.assertFiniteRejected(report, where)
                    count += 1
        self.assertEqual(count, 27)

    def test_pass_fixture_positions_are_rejected(self):
        self.assertEqual(nonfinite_regression(run_report.validate_run_report, self.schema,
                                              load_pass()), [])

    def test_overflowed_json_exponent_in_stage_duration(self):
        text = json.dumps(load_example())
        text = text.replace('"duration_seconds": 290.2', '"duration_seconds": 1e999', 1)
        report = json.loads(text)
        self.assertTrue(math.isinf(report["stages"][4]["duration_seconds"]))
        self.assertFiniteRejected(report, "$.stages[4].duration_seconds")

    def test_json_nan_and_infinity_tokens(self):
        for token in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(token=token):
                text = json.dumps(load_example()).replace(
                    '"duration_seconds": 450', f'"duration_seconds": {token}', 1)
                self.assertFiniteRejected(json.loads(text), "$.duration_seconds")


class PreservedFailureTests(OfflineCase):
    """A2: pre-existing type/minimum/required/status failures stay intact."""

    def test_booleans_negative_and_absent(self):
        cases = [
            (lambda r: r.__setitem__("duration_seconds", True), "expected type"),
            (lambda r: r["stages"][2].__setitem__("duration_seconds", False), "expected type"),
            (lambda r: r.__setitem__("duration_seconds", -1), "minimum"),
            (lambda r: r["stages"][3].__setitem__("duration_seconds", -0.5), "minimum"),
            (lambda r: r.pop("duration_seconds"), "missing required"),
            (lambda r: r["stages"][5].pop("duration_seconds"), "missing required"),
            (lambda r: r["stages"][0].__setitem__("duration_seconds", "1"), "expected type"),
        ]
        for mutate, fragment in cases:
            with self.subTest(fragment=fragment):
                report = load_example()
                mutate(report)
                self.assertInvalid(report, fragment)

    def test_field_and_status_failures_unchanged(self):
        report = load_example()
        report["note"] = "x"
        self.assertInvalid(report, "unexpected field")
        report = load_example()
        report["stages"][0]["status"] = "done"
        self.assertInvalid(report, "not one of")
        report = load_example()
        report["overall_status"] = "ok"
        self.assertInvalid(report, "overall_status")
        report = load_example()
        report["stages"][3]["error_code"] = None
        self.assertInvalid(report, "required when status is warn")

    def test_malformed_shapes_still_reach_schema_errors(self):
        for report, fragment in ((["not", "a", "report"], "expected type"),
                                 ({**load_example(), "stages": "x"}, "expected type")):
            with self.subTest(fragment=fragment):
                self.assertInvalid(report, fragment)


class ToleranceBoundaryTests(OfflineCase):
    """A3: one-second total-duration tolerance and recovery after rejection."""

    def timed(self, duration):
        report = load_pass()
        report["finished_at"] = "2000-01-01T00:00:10Z"
        report["duration_seconds"] = duration
        return report

    def test_below_at_and_above_tolerance(self):
        for duration in (10, 9.5, 10.5, 9, 11, 9.0, 11.0):
            with self.subTest(duration=duration):
                run_report.validate_run_report(self.timed(duration), self.schema)
        for duration in (8.999, 11.001, 8, 12):
            with self.subTest(duration=duration):
                self.assertInvalid(self.timed(duration), "disagrees with started_at/finished_at")

    def test_recovery_after_nonfinite_rejection(self):
        report = self.timed(10)
        report["stages"][6]["duration_seconds"] = float("nan")
        self.assertFiniteRejected(report, "$.stages[6].duration_seconds")
        report["stages"][6]["duration_seconds"] = 0.25
        run_report.validate_run_report(report, self.schema)
        report["duration_seconds"] = float("inf")
        self.assertFiniteRejected(report, "$.duration_seconds")
        report["duration_seconds"] = 10.0
        run_report.validate_run_report(report, self.schema)


class BrokenControlTests(OfflineCase):
    """A4: the regression detects a validator whose finite guard is disabled."""

    def test_real_validator_has_no_misses(self):
        self.assertEqual(nonfinite_regression(run_report.validate_run_report, self.schema), [])

    def test_disabled_guard_is_detected_while_finite_positive_passes(self):
        with mock.patch.object(run_report, "check_finite_durations", lambda report: None):
            misses = nonfinite_regression(run_report.validate_run_report, self.schema)
            run_report.validate_run_report(load_example(), self.schema)
            run_report.validate_run_report(load_pass(), self.schema)
        accepted = {(where, label) for where, label, reason in misses if reason == "accepted"}
        self.assertIn(("$.duration_seconds", "nan"), accepted)
        self.assertIn(("$.stages[4].duration_seconds", "nan"), accepted)
        self.assertIn(("$.stages[4].duration_seconds", "inf"), accepted)
        self.assertEqual(len(misses), 27)
        self.assertEqual(nonfinite_regression(run_report.validate_run_report, self.schema), [])


class ConsumerIntegrationTests(OfflineCase):
    """A5: run_report_check evaluate/CLI and run_coverage CLI withhold success."""

    def assert_invalid_table(self, code, lines):
        self.assertEqual(code, 1)
        self.assertEqual(lines[0], "CHECK INPUT FAIL invalid report or baseline; values suppressed")
        rows = [line for line in lines if re.match(r"^P[1-9] ", line)]
        self.assertEqual(rows, [f"P{i} NA invalid input prevents assessment" for i in range(1, 10)])
        self.assertEqual(lines[-1], "EXIT 1; machine checks only; review required")

    def test_evaluate_withholds_success(self):
        baseline = load_baseline()
        self.assertEqual(run_report_check.evaluate(load_pass(), baseline)[0], 0)
        for where, setter in positions(load_pass()):
            for label, value in NONFINITE:
                with self.subTest(where=where, value=label):
                    report = load_pass()
                    setter(report, value)
                    self.assert_invalid_table(*run_report_check.evaluate(report, baseline))

    def test_check_cli_rejects_overflowed_exponent(self):
        text = PASS.read_text(encoding="utf-8")
        good = self.child(str(CHECK_SCRIPT), str(self.write(text, "pass.json")),
                          "--baseline", str(BASELINE))
        self.assertEqual(good.returncode, 0, good.stdout + good.stderr)
        report = load_pass()
        report["stages"][4]["duration_seconds"] = "__FA21_MARKER__"
        bad = json.dumps(report).replace('"__FA21_MARKER__"', "1e999")
        self.assertTrue(math.isinf(json.loads(bad)["stages"][4]["duration_seconds"]))
        result = self.child(str(CHECK_SCRIPT), str(self.write(bad, "overflow.json")),
                            "--baseline", str(BASELINE))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")
        self.assert_invalid_table(1, result.stdout.splitlines())
        self.assertIsNone(VALUE_TEXT.search(result.stdout))

    def test_coverage_cli_withholds_line(self):
        case = coverage_case()
        ok = self.child("-m", "campaign_tool.records.run_coverage",
                        "--report", str(self.write(json.dumps(case["report"]), "ok.json")))
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(ok.stdout, case["expected"]["line"] + "\n")
        variants = {"top-nan": ("duration_seconds", None, "NaN"),
                    "stage-inf": ("stages", 3, "Infinity"),
                    "stage-overflow": ("stages", 4, "1e999"),
                    "stage-neg-inf": ("stages", 0, "-Infinity")}
        for name, (field, index, token) in variants.items():
            with self.subTest(variant=name):
                report = copy.deepcopy(case["report"])
                marker = "__FA21_MARKER__"
                if field == "duration_seconds":
                    report["duration_seconds"] = marker
                else:
                    report["stages"][index]["duration_seconds"] = marker
                text = json.dumps(report).replace(f'"{marker}"', token)
                result = self.child("-m", "campaign_tool.records.run_coverage",
                                    "--report", str(self.write(text, name + ".json")))
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr.strip(),
                                 "run_coverage: run report invalid; no coverage line")


class ChildGuardTests(OfflineCase):
    """A6: Python children are guarded before import, and violations fail closed.

    Safe synthetic controls only. The network control runs only after the guard is
    confirmed in place, and it calls the guard with a non-socket object, so the
    unguarded built-in would raise TypeError without any I/O. The write control
    targets a sibling directory inside this test's own per-run root.
    """

    def probe(self):
        inside, outside = self.root / "inside", self.root / "outside"
        outside.mkdir()
        ledger = self.root / "probe-ledger.jsonl"
        denied = outside / "denied.txt"
        result = launch(inside, ledger, ["-B", "-m", "tests.test_fa21_report_finite",
                                         "--fa21-probe", str(denied)])
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout), accept.ledger_entries(ledger), inside, denied

    def test_child_guards_are_installed_before_any_product_import(self):
        report, _, inside, _ = self.probe()
        for name in ("installed", "socket.connect", "socket.connect_ex", "create_connection",
                     "getaddrinfo"):
            self.assertIs(report["guards"][name], True, name)
        self.assertEqual(report["product_modules_at_install"], [])
        self.assertEqual(Path(report["guards"]["tempdir"]).resolve(), inside.resolve())
        self.assertTrue(Path(report["gettempdir"]).resolve().is_relative_to(inside.resolve()))

    def test_child_violations_are_refused_recorded_and_fail_the_parent(self):
        report, entries, _, denied = self.probe()
        self.assertEqual(report["connect"], "refused")
        self.assertEqual(report["write"], "refused")
        self.assertFalse(denied.exists())
        self.assertEqual(sorted(e["kind"] for e in entries), ["network", "write"])
        self.assertEqual(accept.guard_failures([], [], entries, []), ["child"])
        self.assertEqual(accept.guard_failures([], [], [], []), [])

    def test_launcher_refuses_non_file_children(self):
        ledger = self.root / "refused-ledger.jsonl"
        result = launch(self.root / "refused", ledger, ["--not-a-script-file"])
        self.assertEqual(result.returncode, 97)
        self.assertEqual([e["kind"] for e in accept.ledger_entries(ledger)], ["refused-child"])

    def test_unguarded_python_children_are_rerouted_through_the_launcher(self):
        state = {"root": str(self.root / "r"), "child_ledger": str(self.root / "l.jsonl")}
        with mock.patch.dict(accept.STATE, state):
            argv, route = accept.wrap_child_argv([sys.executable, "-m", "campaign_tool.x", "--a"])
            self.assertEqual(route, "wrapped")
            self.assertEqual(argv[:4], [sys.executable, "-B", accept.LAUNCHER, "--child-launch"])
            self.assertEqual(argv[-4:], ["--", "-m", "campaign_tool.x", "--a"])
            self.assertEqual(accept.wrap_child_argv(argv), (argv, "already-guarded"))
            self.assertEqual(accept.wrap_child_argv(["git", "status"])[1], "non-python")
        children = [{"argv0": "git", "route": "non-python"},
                    {"argv0": "other-tool", "route": "non-python"},
                    {"argv0": "python3", "route": "wrapped"}]
        self.assertEqual(accept.unguarded_children(children), [children[1]])
        self.assertEqual(accept.guard_failures([], [], [], children[1:2]), ["unguarded-child"])

    def test_dir_fd_relative_paths_resolve_against_their_directory(self):
        fd = os.open(self.root, os.O_RDONLY)
        try:
            self.assertEqual(accept._resolve("entry.json", fd),
                             os.path.realpath(self.root / "entry.json"))
        finally:
            os.close(fd)
        self.assertFalse(accept.is_allowed_write(accept.UNRESOLVED))


class PreservationTests(OfflineCase):
    """A6: synthetic-only inputs, no acquisition modules, schema unchanged."""

    def test_validator_imports_no_acquisition_or_model_code(self):
        tree = ast.parse(VALIDATOR.read_text(encoding="utf-8"))
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                roots.add((node.module or "").split(".")[0])
                if node.module == "campaign_tool":
                    self.assertEqual([alias.name for alias in node.names], ["law"])
        self.assertLessEqual(roots, ALLOWED_VALIDATOR_IMPORTS)

    def test_inputs_are_synthetic_metadata(self):
        for path in (PASS, EXAMPLE):
            text = path.read_text(encoding="utf-8")
            self.assertEqual(json.loads(text)["mode"], "test")
            for pattern in (r"https?://", r"@", r"\.(pdf|docx?|xlsx?|msg|eml)\b"):
                self.assertIsNone(re.search(pattern, text, re.I), (path.name, pattern))

    def test_schema_contract_unchanged(self):
        schema = self.schema
        self.assertEqual(schema["properties"]["schema_version"], {"const": 1})
        self.assertEqual(schema["properties"]["duration_seconds"], {"type": "number", "minimum": 0})
        stage = schema["$defs"]["stage"]["properties"]["duration_seconds"]
        self.assertEqual(stage["type"], ["number", "null"])
        self.assertEqual(stage["minimum"], 0)

    def test_temporary_root_is_the_per_run_root(self):
        self.assertTrue(self.root.is_relative_to(Path(tempfile.gettempdir())))
        self.assertTrue(self.root.is_relative_to(_STATE["root"]))


def _guard_probe(denied):
    """Child-side probe for ChildGuardTests; only meaningful under the launcher."""
    report = {"guards": accept.guards_active(), "gettempdir": tempfile.gettempdir(),
              "product_modules_at_install": accept.STATE["product_modules_at_install"]}
    report["connect"] = "unguarded-not-called"
    if report["guards"]["socket.connect"]:
        try:
            socket.socket.connect(object(), ("192.0.2.1", 9))
        except OSError:
            report["connect"] = "refused"
    try:
        with open(denied, "w", encoding="utf-8") as handle:
            handle.write("x")
        report["write"] = "allowed"
    except PermissionError:
        report["write"] = "refused"
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--fa21-probe"]:
        raise SystemExit(_guard_probe(sys.argv[2]))
    unittest.main()
