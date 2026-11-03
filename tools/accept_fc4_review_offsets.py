"""Acceptance runner for FC4-REVIEW-OFFSETS (offline, synthetic, review-required).

Usage (one check per invocation):
    <interpreter> tools/accept_fc4_review_offsets.py --check CHECK --output-root DIR

CHECK is one of: focused, compatibility, negative, portability, credentials,
public-tree. Each invocation creates a fresh unique child directory under DIR
(never overwriting earlier evidence) holding raw child stdout/stderr, argv,
cwd, exit codes, executed test counts/IDs and source hashes in ``run.json``.
The process exits 0 only when every assertion of the selected check holds.

The repository root is derived from this file. Import search paths are set
only inside this script. Children are argument arrays without a shell; the
interpreter is the one running this script. A passing result is machine
validity only: REVIEW REQUIRED, never semantic/legal acceptance, activation or
publication authority.
"""
import argparse
import ast
import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import types
import unittest

sys.dont_write_bytecode = True

REPO = Path(__file__).resolve().parents[1]
CHECKS = ("focused", "compatibility", "negative", "portability", "credentials", "public-tree")
CARD = "FC4-REVIEW-OFFSETS"
BRIEF_TASK = "t_a958e910"
FOCUSED_TEST_PATH = "tests/records/test_fc4_review_offsets.py"
BASE_SELECTOR = "parent of the first commit adding " + FOCUSED_TEST_PATH
GATE_PATH = "campaign_tool/records/gates/validate_findings.py"
GATE_MODULE = "campaign_tool.records.gates.validate_findings"
FOCUSED_MODULE = "tests.records.test_fc4_review_offsets"
COMPAT_SELECTORS = (
    "campaign_tool.records.gates.tests.test_gate",
    "tests.records.test_packaged_gates",
    "tests.records.test_review_collection",
    "tests.test_secret_report",
    "tests.test_public_tree",
)
REQUIRED_CASES = {
    "z_and_fractional_seconds": "ReviewedTimestampTests.test_z_and_fractional_seconds",
    "signed_offsets_at_boundaries": "ReviewedTimestampTests.test_signed_offsets_at_boundaries",
    "offset_minutes_59_60_99": "ReviewedTimestampTests.test_offset_minutes_59_60_99",
    "offset_hours_23_24": "ReviewedTimestampTests.test_offset_hours_23_24",
    "invalid_calendar_and_clock": "ReviewedTimestampTests.test_invalid_calendar_and_clock",
    "unicode_digits_and_nonstring": "ReviewedTimestampTests.test_unicode_digits_and_nonstring",
    "ready_gate_control": "FindingGateTests.test_ready_gate_control",
    "invalid_timestamp_cannot_supply_required_role":
        "FindingGateTests.test_invalid_timestamp_cannot_supply_required_role",
    "existing_challenge_still_blocks": "FindingGateTests.test_existing_challenge_still_blocks",
}
HASHED_SOURCES = (
    GATE_PATH,
    "campaign_tool/records/gates/safe_output.py",
    "campaign_tool/records/gates/tests/test_gate.py",
    FOCUSED_TEST_PATH,
    "tools/accept_fc4_review_offsets.py",
    "tools/check_secret_report.py",
    "tools/check_public_tree.py",
)
# Application closure inspected before import (portability check).
CLOSURE_SOURCES = (
    "campaign_tool/__init__.py",
    "campaign_tool/records/__init__.py",
    "campaign_tool/records/gates/__init__.py",
    GATE_PATH,
    "campaign_tool/records/gates/safe_output.py",
    "tests/__init__.py",
    "tests/records/__init__.py",
    FOCUSED_TEST_PATH,
)
ALLOWED_CLOSURE_IMPORTS = {"argparse", "hashlib", "json", "re", "datetime", "pathlib", "collections", "os",
                           "secrets", "stat", "copy", "tempfile", "unittest", "safe_output",
                           "campaign_tool.records.gates", "campaign_tool.records.gates.safe_output",
                           "__future__"}
STATIC_SCANNED = (FOCUSED_TEST_PATH, "tools/accept_fc4_review_offsets.py")
# Assembled from parts so this list does not match its own source text.
BOX_SPECIFIC = ("/ho" + "me/", "agent" + "-work", "venv" + "-fullsuite", "/Us" + "ers/", ".her" + "mes",
                "kan" + "ban/", "/tm" + "p/")
INLINE_FORMS = (
    re.compile(r"""["']-""" + r"""[ce]["']"""),
    re.compile("shell" + r"\s*=\s*True"),
    re.compile("PYTHON" + "PATH"),
    re.compile(r"\bos\.sys" + r"tem\("),
)
RAN = re.compile(r"^Ran (\d+) tests? in ", re.M)
SUMMARY = re.compile(r"^(OK|FAILED)(?: \(([^)]*)\))?\s*$", re.M)
VERBOSE = re.compile(r"^(test\w*) \(([\w.]+)\)(?:\n[^\n]*?)? \.\.\. (ok|FAIL|ERROR|skipped.*|expected failure|unexpected success)$",
                     re.M)
# Value tables in the focused module and whether the candidate must accept them.
TABLES = {
    "VALID_Z_AND_FRACTIONAL": True,
    "VALID_SIGNED_OFFSETS": True,
    "INVALID_OFFSET_MINUTES": False,
    "INVALID_OFFSET_HOURS": False,
    "INVALID_CALENDAR_AND_CLOCK": False,
    "UNICODE_AND_MALFORMED_TEXT": False,
    "NONSTRING": False,
}
# Tables in which the candidate may intentionally differ from the baseline.
INTENDED_DELTA_TABLES = {"INVALID_OFFSET_MINUTES", "UNICODE_AND_MALFORMED_TEXT"}
MEASURED_BASELINE_DEFECTS = ("2030-01-01T00:00:00+00:60", "2030-01-01T00:00:00-01:99")


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    return sha256_bytes(Path(path).read_bytes())


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def new_run_dir(output_root, check):
    root = Path(output_root).resolve()
    if root == REPO or root.is_relative_to(REPO):
        raise SystemExit("output root must be outside the repository")
    root.mkdir(parents=True, exist_ok=True)
    run = root / ("{}-{}-{}-{}".format(check, now(), os.getpid(), secrets.token_hex(4)))
    run.mkdir(exist_ok=False)
    return run


def source_hashes(root):
    hashes = {}
    for relative in HASHED_SOURCES:
        path = root / relative
        hashes[relative] = sha256_file(path) if path.is_file() else None
    return hashes


def git_text(argv, root=None):
    done = subprocess.run(["git", "-C", str(root or REPO)] + argv, capture_output=True, text=True,
                          timeout=120, check=True)
    return done.stdout.strip()


def git_identity():
    identity = {}
    for name, argv in (("head", ["rev-parse", "HEAD"]),
                       ("status_porcelain", ["status", "--porcelain", "--untracked-files=all"])):
        try:
            identity[name] = git_text(argv)
        except (OSError, subprocess.SubprocessError) as error:
            identity[name] = None
            identity[name + "_error"] = type(error).__name__
    identity["clean"] = identity.get("status_porcelain") == ""
    return identity


def child_env(run_dir):
    """Inherited environment with temporary files redirected into the run directory."""
    temporary = run_dir / "tmp"
    temporary.mkdir(exist_ok=True)
    env = dict(os.environ)
    env["TMPDIR"] = str(temporary)
    return env


def run_child(run_dir, label, argv, cwd, env=None, stdout_path=None, timeout=1200):
    """Run one argument-array child (no shell) and preserve its raw streams."""
    record = {"label": label, "argv": [str(a) for a in argv], "cwd": str(cwd)}
    out_file = Path(stdout_path) if stdout_path else run_dir / (label + ".stdout")
    err_file = run_dir / (label + ".stderr")
    try:
        with open(out_file, "xb") as out, open(err_file, "xb") as err:
            done = subprocess.run([str(a) for a in argv], cwd=str(cwd), stdout=out, stderr=err,
                                  stdin=subprocess.DEVNULL, env=env if env is not None else child_env(run_dir),
                                  timeout=timeout, check=False)
        record["exit"] = done.returncode
    except (OSError, subprocess.SubprocessError) as error:
        record["exit"] = None
        record["launch_error"] = type(error).__name__
    for key, path in (("stdout", out_file), ("stderr", err_file)):
        if path.is_file():
            record[key + "_path"] = str(path)
            record[key + "_sha256"] = sha256_file(path)
    return record


def verbose_test_id(name, parenthesized):
    """Actual TestCase.id() from one verbose line (3.11+ already appends the method)."""
    if parenthesized.endswith("." + name):
        return parenthesized
    return parenthesized + "." + name


def parse_unittest(stderr_text):
    ran = RAN.findall(stderr_text)
    summary = SUMMARY.findall(stderr_text)
    tests = [{"name": m.group(1), "id": verbose_test_id(m.group(1), m.group(2)),
              "outcome": "skipped" if m.group(3).startswith("skipped") else m.group(3)}
             for m in VERBOSE.finditer(stderr_text)]
    detail = summary[-1][1] if summary else ""
    counts = dict((k.strip(), int(v)) for k, v in (part.split("=") for part in detail.split(", ") if "=" in part))
    return {"tests_run": int(ran[-1]) if ran else 0, "summary": summary[-1][0] if summary else None,
            "summary_detail": detail, "summary_counts": counts, "tests": tests}


class _ParserControl(unittest.TestCase):
    """Synthetic in-memory outcomes used only to check the verbose parser."""

    def test_control_ok(self):
        pass

    def test_control_documented(self):
        """A first docstring line printed beneath the verbose test line."""

    def test_control_fail(self):
        self.fail("deliberate synthetic failure")

    def test_control_error(self):
        raise ValueError("deliberate synthetic error")

    def test_control_subtest_fail(self):
        for value in (1, 2):
            with self.subTest(value=value):
                self.assertEqual(value, 1)

    @unittest.skip("deliberate synthetic skip")
    def test_control_skip(self):
        pass


def parser_self_check():
    """Run known outcomes in memory; parsed IDs/outcomes must equal case.id() results."""
    suite = unittest.TestLoader().loadTestsFromTestCase(_ParserControl)
    actual = {case.id(): "ok" for case in suite}
    stream = io.StringIO()
    outcome = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    for case, _ in outcome.failures:
        actual[getattr(case, "test_case", case).id()] = "FAIL"
    for case, _ in outcome.errors:
        actual[getattr(case, "test_case", case).id()] = "ERROR"
    for case, _ in outcome.skipped:
        actual[case.id()] = "skipped"
    parsed = parse_unittest(stream.getvalue())
    recorded = {t["id"]: t["outcome"] for t in parsed["tests"]}
    # A test with failing subtests prints "..." then subtest lines; it is not "ok".
    missing = sorted(set(actual) - set(recorded))
    consistent = all(recorded.get(k) == v for k, v in actual.items() if k in recorded)
    return {"expected": actual, "parsed": recorded, "tests_run": parsed["tests_run"], "missing": missing,
            "match": consistent and parsed["tests_run"] == len(actual) and parsed["summary"] == "FAILED"
            and missing == [_ParserControl.__module__ + "._ParserControl.test_control_subtest_fail"],
            "python_version": sys.version}


def loaded_suite_ids(targets, root):
    """IDs of the suite unittest would execute for ``targets`` (loaded, not run)."""
    root_text = str(root)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    suite = unittest.TestLoader().loadTestsFromNames(list(targets))
    ids = []

    def walk(item):
        if isinstance(item, unittest.TestSuite):
            for child in item:
                walk(child)
        else:
            ids.append(item.id())
    walk(suite)
    return ids


def unittest_child(run_dir, label, targets, root=None, compare_loaded=True):
    """Run unittest -v on ``targets``; every executed test must be ok, none skipped."""
    root = root or REPO
    argv = [sys.executable, "-B", "-m", "unittest", "-v"] + list(targets)
    record = run_child(run_dir, label, argv, root)
    stderr_text = Path(record["stderr_path"]).read_text(errors="replace") if "stderr_path" in record else ""
    parsed = parse_unittest(stderr_text)
    record.update(parsed)
    problems = []
    parsed_ids = [t["id"] for t in parsed["tests"]]
    if compare_loaded:
        try:
            expected_ids = loaded_suite_ids(targets, root)
        except Exception as error:  # Unloadable suite: fail closed, keep the class.
            expected_ids = None
            record["loaded_suite_error"] = repr(error)[:2000]
        record["loaded_suite_count"] = None if expected_ids is None else len(expected_ids)
        record["parsed_ids_match_loaded_suite"] = (expected_ids is not None
                                                   and len(parsed_ids) == len(set(parsed_ids))
                                                   and sorted(parsed_ids) == sorted(expected_ids))
        if not record["parsed_ids_match_loaded_suite"]:
            problems.append(label + ":parsed_ids_differ_from_loaded_suite")
    if record.get("exit") != 0:
        problems.append(label + ":unittest_exit_nonzero")
    if parsed["summary"] != "OK":
        problems.append(label + ":unittest_summary_not_ok")
    if parsed["tests_run"] <= 0:
        problems.append(label + ":no_tests_executed")
    if len(parsed["tests"]) != parsed["tests_run"]:
        problems.append(label + ":executed_test_ids_incomplete")
    extras = sorted(k for k in parsed["summary_counts"] if k in ("skipped", "expected failures", "unexpected successes"))
    if extras:
        problems.append(label + ":unexpected_" + "_".join(k.replace(" ", "_") for k in extras))
    bad = [t["id"] for t in parsed["tests"] if t["outcome"] != "ok"]
    record["non_ok_test_ids"] = bad
    if bad:
        problems.append(label + ":non_ok_tests")
    record["problems"] = problems
    return record, problems


def _load_in_process(root):
    root_text = str(root)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    gates = __import__(GATE_MODULE, fromlist=["*"])
    focused = __import__(FOCUSED_MODULE, fromlist=["*"])
    for module in (gates, focused):
        if not Path(module.__file__).resolve().is_relative_to(Path(root).resolve()):
            raise RuntimeError("module_loaded_outside_candidate")
    if focused.gates is not gates:
        raise RuntimeError("focused_module_uses_a_different_gate_module")
    return gates, focused


def baseline_commit():
    """Pinned base = parent of the commit that first added the focused test module.

    Derived from history (no high-entropy literal in source); run.json records
    the resolved commit and file hash for comparison with the frozen brief pin.
    """
    added = [line for line in git_text(["log", "--format=%H", "--diff-filter=A", "HEAD", "--",
                                        FOCUSED_TEST_PATH]).split() if line]
    if not added:
        raise RuntimeError("focused_test_commit_not_found")
    return git_text(["rev-parse", "--verify", added[-1] + "^1"])


def baseline_gate_module():
    """Build the pinned-baseline gate module in memory from the git object."""
    base = baseline_commit()
    done = subprocess.run(["git", "-C", str(REPO), "show", base + ":" + GATE_PATH],
                          capture_output=True, timeout=60, check=True)
    source = done.stdout
    module = types.ModuleType("campaign_tool.records.gates._fc4_baseline_validate_findings")
    module.__package__ = "campaign_tool.records.gates"
    module.__file__ = "<git {}:{}>".format(base, GATE_PATH)
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    return module, sha256_bytes(source), base


def _call(function, value):
    try:
        return function(value)
    except Exception as error:  # A raise is recorded, never treated as acceptance.
        return "raised:" + type(error).__name__


def baseline_differential(run_dir, result):
    """Baseline vs candidate on every focused value table and on the real gate."""
    problems = []
    gates, focused = _load_in_process(REPO)
    baseline, baseline_sha, base = baseline_gate_module()
    rows = []
    for table, expected in TABLES.items():
        for value in getattr(focused, table):
            old = _call(baseline.reviewed_timestamp, value)
            new = _call(gates.reviewed_timestamp, value)
            rows.append({"table": table, "value": ascii(value), "expected": expected, "old": old, "new": new})
    wrong = [r for r in rows if r["new"] is not r["expected"]]
    old_valid_rejected = [r for r in rows if r["expected"] and r["old"] is not True]
    unintended = [r for r in rows if r["old"] != r["new"] and r["table"] not in INTENDED_DELTA_TABLES]
    old_by_value = {r["value"]: r["old"] for r in rows}
    reproduced = {value: old_by_value.get(ascii(value)) for value in MEASURED_BASELINE_DEFECTS}
    # Gate-level: only the legal receipt timestamp is replaced.
    gate_rows = []
    with tempfile.TemporaryDirectory(prefix="fc4-differential-", dir=str(run_dir)) as temporary:
        finding, reviews = focused.synthetic_case(temporary)
        for value in MEASURED_BASELINE_DEFECTS + ("2030-01-01T00:00:00+00:59", "2030-01-01T00:00:00Z"):
            changed = focused.with_timestamp(reviews, "legal", value)
            old = baseline.gate(finding, changed, True)
            new = gates.gate(finding, changed, True)
            gate_rows.append({"legal_reviewed_at": value, "old_ready": old["ready"], "new_ready": new["ready"],
                              "old_blockers": old["blockers"], "new_blockers": new["blockers"]})
    path = run_dir / "baseline-differential.json"
    with open(path, "x") as handle:
        json.dump({"baseline_commit": base, "baseline_selector": BASE_SELECTOR,
                   "baseline_gate_sha256": baseline_sha, "rows": rows, "gate_rows": gate_rows},
                  handle, indent=2, sort_keys=True)
    result["baseline_differential"] = {
        "path": str(path), "sha256": sha256_file(path), "values": len(rows), "baseline_commit": base,
        "baseline_gate_sha256": baseline_sha, "measured_defects_at_baseline": reproduced,
        "candidate_wrong": wrong, "baseline_rejected_valid": old_valid_rejected,
        "unintended_deltas": unintended,
        "intended_deltas": [r for r in rows if r["old"] != r["new"] and r["table"] in INTENDED_DELTA_TABLES],
        "gate_rows": gate_rows}
    if not rows:
        problems.append("differential_no_values")
    if wrong:
        problems.append("candidate_predicate_wrong")
    if old_valid_rejected:
        problems.append("valid_table_not_accepted_at_baseline")
    if unintended:
        problems.append("unintended_behavior_delta_vs_baseline")
    if any(v is not True for v in reproduced.values()):
        problems.append("baseline_defect_not_reproduced")
    expected_gate = [(True, False), (True, False), (True, True), (True, True)]
    if [(g["old_ready"], g["new_ready"]) for g in gate_rows] != expected_gate:
        problems.append("gate_level_differential_unexpected")
    return problems


# ----------------------------------------------------------------- checks


def check_focused(run_dir, result):
    record, problems = unittest_child(run_dir, "focused-unittest", [FOCUSED_MODULE])
    result["children"].append(record)
    executed = {t["id"]: t["outcome"] for t in record["tests"]}
    case_map = {}
    for case, name in REQUIRED_CASES.items():
        test_id = FOCUSED_MODULE + "." + name
        case_map[case] = {"test_id": test_id, "outcome": executed.get(test_id)}
        if executed.get(test_id) != "ok":
            problems.append("required_case_not_ok:" + case)
    result["case_map"] = case_map
    try:
        problems = problems + baseline_differential(run_dir, result)
    except (subprocess.SubprocessError, OSError, KeyError, ValueError, RuntimeError, AttributeError) as error:
        problems = problems + ["baseline_differential_incomplete:" + type(error).__name__]
        result["baseline_differential_error"] = repr(error)[:2000]
    return problems


def check_compatibility(run_dir, result):
    """Exact existing selectors at the pinned baseline (local clone) and at the candidate."""
    problems = []
    base = baseline_commit()
    clone = run_dir / "baseline-clone"
    for label, argv in (("baseline-clone", ["git", "clone", "--quiet", "--no-hardlinks", "--no-checkout",
                                            str(REPO), str(clone)]),
                        ("baseline-checkout", ["git", "-C", str(clone), "checkout", "--quiet", "--detach", base])):
        record = run_child(run_dir, label, argv, run_dir)
        result["children"].append(record)
        if record.get("exit") != 0:
            return problems + [label + "_failed"]
    baseline_head = git_text(["rev-parse", "HEAD"], clone)
    result["compatibility_baseline"] = {"commit": base, "clone": str(clone), "clone_head": baseline_head,
                                        "gate_sha256": sha256_file(clone / GATE_PATH)}
    if baseline_head != base:
        problems.append("baseline_clone_head_mismatch")
    if (clone / FOCUSED_TEST_PATH).exists():
        problems.append("baseline_clone_contains_candidate_test")
    old, old_problems = unittest_child(run_dir, "compatibility-baseline", COMPAT_SELECTORS, root=clone,
                                       compare_loaded=False)
    result["children"].append(old)
    problems += old_problems
    new, new_problems = unittest_child(run_dir, "compatibility-candidate", COMPAT_SELECTORS)
    result["children"].append(new)
    problems += new_problems
    old_ids = sorted(t["id"] for t in old["tests"])
    new_ids = sorted(t["id"] for t in new["tests"])
    result["compatibility_summary"] = {"selectors": list(COMPAT_SELECTORS),
                                       "baseline": {"tests_run": old["tests_run"], "summary": old["summary"]},
                                       "candidate": {"tests_run": new["tests_run"], "summary": new["summary"]},
                                       "same_test_ids": old_ids == new_ids}
    if old_ids != new_ids:
        problems.append("compatibility_test_ids_differ_from_baseline")
    return problems


def _run_cases(focused, names):
    loader = unittest.TestLoader()
    suite = unittest.TestSuite(loader.loadTestsFromName(name, focused) for name in names)
    stream = io.StringIO()
    outcome = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    return outcome, stream.getvalue()


def _run_module(focused):
    suite = unittest.TestLoader().loadTestsFromModule(focused)
    stream = io.StringIO()
    outcome = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    return outcome, stream.getvalue()


def _case_id(case):
    return getattr(case, "test_case", case).id()


def _summarise(outcome):
    return {"tests_run": outcome.testsRun,
            "failed_tests": sorted({_case_id(case) for case, _ in outcome.failures}),
            "errored_tests": sorted({_case_id(case) for case, _ in outcome.errors}),
            "failure_records": len(outcome.failures), "error_records": len(outcome.errors),
            "skipped": [case.id() for case, _ in outcome.skipped],
            "unexpected_successes": [case.id() for case in outcome.unexpectedSuccesses],
            "successful": outcome.wasSuccessful()}


NEGATIVE_REGRESSIONS = (
    "ReviewedTimestampTests.test_offset_minutes_59_60_99",
    "FindingGateTests.test_invalid_timestamp_cannot_supply_required_role",
)
NEGATIVE_POSITIVES = (
    "ReviewedTimestampTests.test_z_and_fractional_seconds",
    "FindingGateTests.test_ready_gate_control",
)
CONTROL_VALUES = {"+00:60": "2030-01-01T00:00:00+00:60", "+00:59": "2030-01-01T00:00:00+00:59",
                  "Z": "2030-01-01T00:00:00Z"}


def _direct(gates, focused, run_dir):
    values = {label: gates.reviewed_timestamp(value) for label, value in CONTROL_VALUES.items()}
    with tempfile.TemporaryDirectory(prefix="fc4-negative-", dir=str(run_dir)) as temporary:
        finding, reviews = focused.synthetic_case(temporary)
        for label, value in CONTROL_VALUES.items():
            values["gate_ready_legal_" + label] = gates.gate(
                finding, focused.with_timestamp(reviews, "legal", value), True)["ready"]
    return values


def check_negative(run_dir, result):
    """In-memory mutant: only the explicit offset-component validation is bypassed."""
    problems = []
    gates, focused = _load_in_process(REPO)
    original = gates.offset_components_valid
    on_disk_before = sha256_file(REPO / GATE_PATH)
    phases = {}
    try:
        gates.offset_components_valid = lambda match: True
        regress, regress_text = _run_cases(focused, NEGATIVE_REGRESSIONS)
        positive, positive_text = _run_cases(focused, NEGATIVE_POSITIVES)
        phases["mutant_regressions"] = _summarise(regress)
        phases["mutant_positives"] = _summarise(positive)
        phases["mutant_direct"] = _direct(gates, focused, run_dir)
        (run_dir / "negative-mutant.txt").write_text(regress_text + "\n" + positive_text)
    finally:
        gates.offset_components_valid = original
    phases["restored_offset_validation"] = gates.offset_components_valid is original
    restored, restored_text = _run_module(focused)
    phases["restored"] = _summarise(restored)
    phases["restored_direct"] = _direct(gates, focused, run_dir)
    (run_dir / "negative-restored.txt").write_text(restored_text)
    phases["gate_source_unchanged_on_disk"] = sha256_file(REPO / GATE_PATH) == on_disk_before
    for name in ("negative-mutant.txt", "negative-restored.txt"):
        phases[name + "_sha256"] = sha256_file(run_dir / name)
    result["negative"] = phases
    mutant = phases["mutant_regressions"]
    detected = set(mutant["failed_tests"] + mutant["errored_tests"])
    expected = {focused.__name__ + "." + name for name in NEGATIVE_REGRESSIONS}
    if mutant["tests_run"] != len(NEGATIVE_REGRESSIONS) or detected != expected or mutant["errored_tests"]:
        problems.append("mutant_not_detected_by_every_regression")
    md = phases["mutant_direct"]
    if not (md["+00:60"] is True and md["gate_ready_legal_+00:60"] is True):
        problems.append("mutant_did_not_reopen_normalization_path")
    mp = phases["mutant_positives"]
    if not mp["successful"] or mp["tests_run"] != len(NEGATIVE_POSITIVES) or mp["skipped"]:
        problems.append("positives_not_green_under_mutant")
    if not phases["restored_offset_validation"]:
        problems.append("offset_validation_not_restored")
    rs = phases["restored"]
    if not rs["successful"] or rs["tests_run"] != len(REQUIRED_CASES) or rs["skipped"]:
        problems.append("restored_module_not_green")
    rd = phases["restored_direct"]
    if not (rd["+00:59"] is True and rd["Z"] is True and rd["+00:60"] is False
            and rd["gate_ready_legal_+00:59"] is True and rd["gate_ready_legal_Z"] is True
            and rd["gate_ready_legal_+00:60"] is False):
        problems.append("restored_controls_wrong")
    if not phases["gate_source_unchanged_on_disk"]:
        problems.append("gate_source_mutated_on_disk")
    return problems


def static_inspection(root):
    findings = []
    for relative in STATIC_SCANNED:
        text = (root / relative).read_text()
        for number, line in enumerate(text.splitlines(), 1):
            for token in BOX_SPECIFIC:
                if token in line:
                    findings.append({"file": relative, "line": number, "kind": "box_specific_path"})
            for pattern in INLINE_FORMS:
                if pattern.search(line):
                    findings.append({"file": relative, "line": number, "kind": "inline_or_shell_form"})
    return findings


def closure_inspection(root):
    """Source-inspect the application closure before any import of it."""
    imports, unexpected, module_constants = {}, [], {}
    for relative in CLOSURE_SOURCES:
        tree = ast.parse((root / relative).read_text(), relative)
        names = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names += [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                prefix = "." * node.level + (node.module or "")
                names.append(prefix)
        imports[relative] = sorted(set(names))
        for name in names:
            stripped = name.lstrip(".")
            if name.startswith("."):
                continue
            if stripped not in ALLOWED_CLOSURE_IMPORTS:
                unexpected.append({"file": relative, "import": name})
        module_constants[relative] = sorted(
            target.id for node in tree.body if isinstance(node, ast.Assign)
            for target in node.targets if isinstance(target, ast.Name))
    return {"imports": imports, "unexpected_imports": unexpected, "module_level_names": module_constants}


def candidate_files():
    done = subprocess.run(["git", "-C", str(REPO), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                          capture_output=True, timeout=120, check=True)
    names = [name for name in done.stdout.decode().split("\0") if name]
    return sorted(set(names))


def check_portability(run_dir, result):
    problems = []
    findings = static_inspection(REPO)
    result["static_inspection"] = {"files": list(STATIC_SCANNED), "findings": findings}
    if findings:
        problems.append("static_box_specific_or_inline_findings")
    closure = closure_inspection(REPO)
    result["closure_inspection"] = closure
    if closure["unexpected_imports"]:
        problems.append("closure_has_unexpected_imports")
    copy_root = run_dir / "relocated" / "second-prefix" / "candidate-source"
    copied = 0
    for name in candidate_files():
        source = REPO / name
        if source.is_symlink() or not source.is_file():
            continue
        target = copy_root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied += 1
    copy_hashes = source_hashes(copy_root)
    result["portability_copy"] = {"prefix": str(copy_root), "files_copied": copied,
                                  "source_hashes_match": copy_hashes == result["source_hashes"]}
    if copy_hashes != result["source_hashes"]:
        problems.append("copy_hashes_differ")
    home = run_dir / "isolated-home"
    home.mkdir()
    # Inherit the environment; redirect only data/home defaults (no search-path variables).
    env = dict(os.environ)
    env.update({"HOME": str(home), "TMPDIR": str(home), "XDG_CONFIG_HOME": str(home),
                "XDG_DATA_HOME": str(home), "XDG_CACHE_HOME": str(home)})
    report = run_dir / "portability-child.json"
    argv = [sys.executable, "-I", "-B", str(copy_root / "tools" / "accept_fc4_review_offsets.py"),
            "--portability-child-report", str(report)]
    record = run_child(run_dir, "portability-child", argv, copy_root, env=env)
    result["children"].append(record)
    if record.get("exit") != 0:
        problems.append("portability_child_failed")
    if not report.is_file():
        problems.append("portability_report_missing")
        return problems
    child = json.loads(report.read_text())
    record["report_sha256"] = sha256_file(report)
    result["portability_child"] = {k: child.get(k) for k in
                                   ("repo_root", "tests_run", "failures", "errors", "skipped", "sentinel_hits",
                                    "modules_outside_copy", "loaded_modules", "sentinels_installed_before_import",
                                    "tempdir")}
    if child.get("repo_root") != str(copy_root.resolve()):
        problems.append("child_did_not_use_copy")
    if child.get("tests_run") != len(REQUIRED_CASES) or child.get("failures") or child.get("errors") \
            or child.get("skipped"):
        problems.append("child_focused_not_green")
    if child.get("sentinel_hits"):
        problems.append("network_or_process_sentinel_triggered")
    if not child.get("sentinels_installed_before_import"):
        problems.append("sentinels_not_installed_before_import")
    loaded = child.get("loaded_modules") or {}
    if child.get("modules_outside_copy") or GATE_MODULE not in loaded or FOCUSED_MODULE not in loaded:
        problems.append("application_modules_loaded_outside_copy")
    if not str(child.get("tempdir", "")).startswith(str(home)):
        problems.append("child_tempdir_not_redirected")
    return problems


def portability_child(report):
    """Runs inside the relocated copy under -I: sentinels before any app import."""
    import socket
    hits = []
    app_preloaded = sorted(n for n in sys.modules if n.split(".")[0] in ("campaign_tool", "tests"))

    def refuse(name):
        def blocked(*_args, **_kwargs):
            hits.append(name)
            raise PermissionError("offline sentinel: " + name)
        return blocked

    socket.socket.connect = refuse("socket.connect")
    socket.socket.connect_ex = refuse("socket.connect_ex")
    socket.create_connection = refuse("socket.create_connection")
    socket.getaddrinfo = refuse("socket.getaddrinfo")
    subprocess.Popen.__init__ = refuse("subprocess.Popen")
    os.system = refuse("os.system")
    for name in ("execv", "execve", "execvp", "execvpe", "fork", "forkpty", "posix_spawn", "posix_spawnp"):
        if hasattr(os, name):
            setattr(os, name, refuse("os." + name))
    tempfile.tempdir = None  # Re-read TMPDIR (redirected by the parent) on next use.
    root = REPO.resolve()
    sys.path.insert(0, str(root))
    suite = unittest.TestLoader().loadTestsFromName(FOCUSED_MODULE)
    stream = io.StringIO()
    outcome = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    loaded, outside = {}, []
    for name, module in sorted(sys.modules.items()):
        if name.split(".")[0] not in ("campaign_tool", "tests"):
            continue
        path = getattr(module, "__file__", None)
        if path is None:
            continue
        loaded[name] = path
        if not Path(path).resolve().is_relative_to(root):
            outside.append(name)
    data = {"repo_root": str(root), "tests_run": outcome.testsRun,
            "failures": sorted({_case_id(c) for c, _ in outcome.failures}),
            "errors": sorted({_case_id(c) for c, _ in outcome.errors}),
            "skipped": [c.id() for c, _ in outcome.skipped], "sentinel_hits": hits,
            "sentinels_installed_before_import": not app_preloaded, "tempdir": tempfile.gettempdir(),
            "modules_outside_copy": outside, "loaded_modules": loaded, "unittest_output": stream.getvalue()}
    with open(report, "x") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
    ok = outcome.wasSuccessful() and outcome.testsRun > 0 and not outcome.skipped and not hits and not outside
    return 0 if ok else 1


def check_credentials(run_dir, result):
    problems = []
    scanner = Path(sys.executable).parent / "detect-secrets"
    if not scanner.is_file():
        result["scanner"] = {"path": str(scanner), "present": False}
        return ["detect_secrets_unavailable_adjacent_to_interpreter"]
    report = run_dir / "secret-scan.json"
    scan = run_child(run_dir, "detect-secrets-scan",
                     [scanner, "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"],
                     REPO, stdout_path=report)
    result["children"].append(scan)
    version = run_child(run_dir, "detect-secrets-version", [scanner, "--version"], REPO)
    result["children"].append(version)
    if scan.get("exit") != 0:
        problems.append("secret_scan_failed")
    try:
        data = json.loads(report.read_text())
        if not isinstance(data.get("results"), dict):
            raise ValueError("results")
        result["secret_scan"] = {"files_with_hits": len(data["results"]),
                                 "raw_hits": sum(len(v) for v in data["results"].values()),
                                 "candidate_files_with_hits": sorted(
                                     f for f in data["results"] if f in (FOCUSED_TEST_PATH, GATE_PATH,
                                                                          "tools/accept_fc4_review_offsets.py"))}
    except (OSError, ValueError, AttributeError):
        problems.append("secret_report_malformed")
        return problems
    checker = run_child(run_dir, "check-secret-report",
                        [sys.executable, "-B", REPO / "tools" / "check_secret_report.py", report], REPO)
    result["children"].append(checker)
    text = Path(checker["stdout_path"]).read_text(errors="replace") if "stdout_path" in checker else ""
    match = re.search(r"potential secrets: (\d+);", text)
    potential = int(match.group(1)) if match else None
    result["secret_scan"]["potential_secrets"] = potential
    if checker.get("exit") != 0:
        problems.append("check_secret_report_failed")
    if potential != 0:
        problems.append("potential_secrets_nonzero_or_unreported")
    if result["secret_scan"]["candidate_files_with_hits"]:
        problems.append("changed_files_have_scanner_hits")
    return problems


def check_public_tree(run_dir, result):
    record = run_child(run_dir, "check-public-tree",
                       [sys.executable, "-B", REPO / "tools" / "check_public_tree.py"], REPO)
    result["children"].append(record)
    return [] if record.get("exit") == 0 else ["public_tree_check_failed"]


HANDLERS = {"focused": check_focused, "compatibility": check_compatibility, "negative": check_negative,
            "portability": check_portability, "credentials": check_credentials, "public-tree": check_public_tree}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", choices=CHECKS)
    parser.add_argument("--output-root")
    parser.add_argument("--portability-child-report", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.portability_child_report:
        if args.check or args.output_root:
            parser.error("child mode takes no other arguments")
        return portability_child(args.portability_child_report)
    if not args.check or not args.output_root:
        parser.error("--check and --output-root are required")
    run_dir = new_run_dir(args.output_root, args.check)
    (run_dir / "tmp").mkdir()
    tempfile.tempdir = str(run_dir / "tmp")
    result = {"schema": "fc4-review-offsets-acceptance-v1", "card": CARD, "brief_task": BRIEF_TASK,
              "check": args.check, "argv": [sys.executable] + list(sys.argv),
              "cwd": os.getcwd(), "interpreter": sys.executable, "python_version": sys.version,
              "repo_root": str(REPO), "run_dir": str(run_dir), "started_utc": now(),
              "base_selector": BASE_SELECTOR, "git": git_identity(), "source_hashes": source_hashes(REPO),
              "parser_self_check": parser_self_check(),
              "children": [], "acceptance": "REVIEW REQUIRED; machine validity only, not semantic/legal acceptance"}
    problems = []
    if not result["git"].get("clean") or not result["git"].get("head"):
        problems.append("candidate_tree_not_clean_or_head_unknown")
    if not result["parser_self_check"]["match"]:
        problems.append("parser_self_check_failed")
    try:
        result["base_commit"] = baseline_commit()
        result["base_gate_sha256"] = sha256_bytes(subprocess.run(
            ["git", "-C", str(REPO), "show", result["base_commit"] + ":" + GATE_PATH],
            capture_output=True, timeout=60, check=True).stdout)
        problems += HANDLERS[args.check](run_dir, result)
    except Exception as error:  # Incomplete check: record the class, fail closed.
        problems.append("check_incomplete:" + type(error).__name__)
        result["exception"] = repr(error)[:2000]
    result["finished_utc"] = now()
    result["git_after"] = git_identity()
    if result["git_after"].get("head") != result["git"].get("head") \
            or result["git_after"].get("status_porcelain") != result["git"].get("status_porcelain"):
        problems.append("repository_changed_during_check")
    result["problems"] = problems
    result["verdict"] = "PASS_REVIEW_REQUIRED" if not problems else "FAIL"
    with open(run_dir / "run.json", "x") as handle:
        json.dump(result, handle, indent=2, sort_keys=True, default=str)
    print(json.dumps({"check": args.check, "verdict": result["verdict"], "problems": problems,
                      "run_dir": str(run_dir), "head": result["git"].get("head"),
                      "clean": result["git"].get("clean"),
                      "run_json_sha256": sha256_file(run_dir / "run.json")}, sort_keys=True))
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
