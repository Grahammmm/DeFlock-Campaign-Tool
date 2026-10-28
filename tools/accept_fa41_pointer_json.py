"""Scoped offline acceptance runner for FA41 (duplicate JSON member names).

Each invocation runs exactly one case and writes its evidence to a fresh,
uniquely named directory below --output-parent, which must be an absolute,
symlink-free path outside this repository:

  behavior          tests/records/test_fa41_pointer_json.py, with every A1/A2/
                    A3/A5 acceptance row mapped to named tests that must pass
  compatibility     the unchanged tests/test_portal_reconcile.py
  negative-control  A4: the deciding duplicate-refusal tests run with ONLY the
                    new duplicate rejection disabled by an in-memory double
                    (the complete frozen population must fail through the
                    refusal assertions alone: any error, skip or other
                    outcome fails the control) while the held-observation
                    positive still passes; then the real hook is restored
                    and both rerun clean
  isolation         A6: a second complete source copy inside the run directory
                    runs the focused suites; loaded module paths, runtime path
                    constants/bound defaults and sentinel self-checks recorded
  full-ci           complete repository discovery (SD-15 argv and the committed
                    records.yml argv) plus the CI S3a module invocation; every
                    population must have zero failures, errors, skips,
                    expected failures and unexpected successes (no skip
                    exemption), and the verbose discovery must report every
                    new regression test ok
  credentials       installed detect-secrets scan + unchanged
                    tools/check_secret_report.py on that exact JSON report

Before any repository module is imported the runner disables bytecode writes,
redirects tempfile/TMPDIR into the per-run directory and installs sentinels:
socket connect/resolve guards, a model-client import guard, an audit-hook
write guard (no Python-level write outside the run directory) and a live-file
read guard (no open below the user's home outside the source tree, the run
directory and the interpreter prefixes). Sentinels self-check with inert
probes. They are regression controls, not a sandbox; child CLI processes the
tests start are not audited. A pass is REVIEW REQUIRED, never semantic,
legal, human, stage or release acceptance.
"""
import argparse
import datetime
import hashlib
import importlib.abc
import io
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile

REPO = Path(__file__).resolve().parents[1]
CASES = ("behavior", "compatibility", "negative-control", "isolation", "full-ci", "credentials")
REGRESSION = "tests.records.test_fa41_pointer_json"
COMPATIBILITY = ("tests.test_portal_reconcile",)
HASHED = (
    "tools/portal_reconcile.py",
    "tests/records/test_fa41_pointer_json.py",
    "tools/accept_fa41_pointer_json.py",
    "tests/test_portal_reconcile.py",
    "tests/fixtures/portal_reconcile/observations.json",
    "tests/fixtures/portal_reconcile/queue-export.json",
    "tools/check_secret_report.py",
    "tools/check_public_tree.py",
    ".github/workflows/records.yml",
)
R = REGRESSION + "."
ROWS = {
    "A1_positive": [
        R + "ReadJsonPreservedBehaviorTests.test_fixture_exports_read_completely",
        R + "ReadJsonPreservedBehaviorTests.test_valid_rows_returned_as_plain_dicts",
        R + "ReadJsonPreservedBehaviorTests.test_empty_exports",
        R + "ReadJsonPreservedBehaviorTests.test_valid_unicode_is_not_a_duplicate",
        R + "SeparateRowAccountingTests.test_identical_separate_rows_are_deduplicated",
        R + "SeparateRowAccountingTests.test_contradictory_separate_rows_stay_conflicting",
        R + "SeparateRowAccountingTests.test_duplicate_queue_identity_rows_stay_ambiguous",
        R + "CliIntegrationTests.test_cli_fixture_totals_and_byte_identical_repeats",
        R + "CliIntegrationTests.test_cli_empty_and_unicode_exports",
    ],
    "A2_negative": [
        R + "ReadJsonDuplicateRefusalTests.test_contradictory_allowlisted_false_true_refused",
        R + "ReadJsonDuplicateRefusalTests.test_contradictory_allowlisted_true_false_refused",
        R + "ReadJsonDuplicateRefusalTests.test_identical_duplicate_values_refused",
        R + "ReadJsonDuplicateRefusalTests.test_every_observation_case_refused",
        R + "ReadJsonDuplicateRefusalTests.test_every_queue_case_refused",
        R + "ReadJsonDuplicateRefusalTests.test_queue_state_and_has_version_refused",
        R + "ReadJsonDuplicateRefusalTests.test_ignored_nested_object_refused",
        R + "CliIntegrationTests.test_cli_refuses_every_observation_case",
        R + "CliIntegrationTests.test_cli_refuses_every_queue_case",
        R + "CliIntegrationTests.test_seeded_prior_output_stays_byte_identical",
        R + "CliIntegrationTests.test_in_process_main_refuses_with_fixed_error",
        R + "ReadJsonPreservedBehaviorTests.test_malformed_json_keeps_its_code",
        R + "ReadJsonPreservedBehaviorTests.test_existing_fatal_codes_unchanged",
        R + "ReadJsonPreservedBehaviorTests.test_valid_but_malformed_rows_are_still_counted",
        R + "CliIntegrationTests.test_cli_malformed_json_and_rows_keep_distinct_behavior",
        R + "SeparateRowAccountingTests.test_separate_held_observation_remains_held",
    ],
    "A3_boundary_recovery": [
        R + "ReadJsonPreservedBehaviorTests.test_row_limit_seam_and_one_over",
        R + "ReadJsonPreservedBehaviorTests.test_byte_limit_seam_and_one_over",
        R + "ReadJsonDuplicateRefusalTests.test_duplicate_refused_at_existing_row_bound",
        R + "CliIntegrationTests.test_seeded_prior_output_stays_byte_identical",
        R + "CliIntegrationTests.test_cli_fixture_totals_and_byte_identical_repeats",
    ],
    "A5_actual_integration": [
        R + "ReadJsonPreservedBehaviorTests.test_fixture_exports_read_completely",
        R + "CliIntegrationTests.test_cli_refuses_every_observation_case",
        R + "CliIntegrationTests.test_cli_refuses_every_queue_case",
        R + "CliIntegrationTests.test_in_process_main_refuses_with_fixed_error",
        R + "CliIntegrationTests.test_cli_fixture_totals_and_byte_identical_repeats",
        R + "SeparateRowAccountingTests.test_separate_held_observation_remains_held",
    ],
    "runner_fail_closed_controls": [
        R + "AcceptanceRunnerNegativeControlTests.test_decisive_control_is_accepted",
        R + "AcceptanceRunnerNegativeControlTests.test_unrelated_errors_are_not_detection",
        R + "AcceptanceRunnerNegativeControlTests.test_real_case_rejects_injected_error_and_accepts_decisive_control",
        R + "AcceptanceRunnerFullCiAggregationTests.test_complete_clean_reports_pass",
        R + "AcceptanceRunnerFullCiAggregationTests.test_skips_and_expected_failures_fail_every_population",
        R + "AcceptanceRunnerFullCiAggregationTests.test_unqualified_unrelated_skip_line_fails",
        R + "AcceptanceRunnerFullCiAggregationTests.test_incomplete_new_collection_fails",
        R + "AcceptanceRunnerFullCiAggregationTests.test_frozen_new_population_matches_loaded_module",
        R + "AcceptanceRunnerFullCiAggregationTests.test_case_full_ci_rejects_synthetic_skip_protocol",
    ],
}
DECIDING_CLASS = R + "ReadJsonDuplicateRefusalTests"
DECIDING_CONTRADICTION = DECIDING_CLASS + ".test_contradictory_allowlisted_false_true_refused"
# The complete frozen deciding population: every one must FAIL (assertion,
# not error) when only the duplicate rejection is disabled.
DECIDING_TESTS = tuple(sorted(DECIDING_CLASS + "." + name for name in (
    "test_contradictory_allowlisted_false_true_refused",
    "test_contradictory_allowlisted_true_false_refused",
    "test_identical_duplicate_values_refused",
    "test_every_observation_case_refused",
    "test_every_queue_case_refused",
    "test_queue_state_and_has_version_refused",
    "test_ignored_nested_object_refused",
    "test_duplicate_refused_at_existing_row_bound",
)))
# The refusal assertions the deciding tests make: assertRaises(InputError)
# and the fixed-code check on the raised error (assert_refused).
REFUSAL_FAILURE = re.compile(r"^AssertionError: (?:InputError not raised|'[a-z_]+' != 'duplicate_json_key')$", re.M)
# Complete population of the new regression module; full-ci requires every
# one of them to be collected and reported ok by the records.yml discovery.
EXPECTED_NEW_TEST_COUNT = 45
HELD_POSITIVE = R + "SeparateRowAccountingTests.test_separate_held_observation_remains_held"
FOCUSED_MODULES = ("tools.portal_reconcile", REGRESSION, "tests.test_portal_reconcile",
                   "campaign_tool.records.portal.notice", "campaign_tool.records.portal.policy",
                   "campaign_tool.records.portal.store")
MODEL_CLIENTS = ("openai", "anthropic", "ollama", "httpx", "requests", "aiohttp", "urllib3", "google.generativeai")
CI_S3A = ("tests.records.test_mail_resolution_census", "tests.records.test_mail_adoption",
          "tests.records.test_original_reconciliation")

GUARD_HITS = []    # network / model-client sentinel refusals during tests
WRITE_HITS = []    # refused writes outside the run root
READ_HITS = []     # refused live-file reads
PROBE_HITS = []    # refusals of the inert self-check probes (expected)
PROBE_PREFIX = "fa41-probe-"


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def git(*args):
    done = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True, check=False)
    return done.stdout.strip() if done.returncode == 0 else f"unavailable:{done.returncode}"


def tree_digest(root):
    digest = hashlib.sha256()
    pycache = 0
    for directory, dirs, files in os.walk(root):
        pycache += sum(1 for d in dirs if d == "__pycache__")
        dirs[:] = sorted(d for d in dirs if d not in (".git", "__pycache__"))
        for name in sorted(files):
            path = Path(directory) / name
            data = b"symlink:" + os.readlink(path).encode() if path.is_symlink() else path.read_bytes()
            digest.update(str(path.relative_to(root)).encode() + b"\0" + hashlib.sha256(data).digest())
    return digest.hexdigest(), pycache


def identity():
    hashes = {}
    for relative in HASHED:
        path = REPO / relative
        hashes[relative] = sha256_bytes(path.read_bytes()) if path.is_file() else None
    digest, pycache = tree_digest(REPO)
    return {"head": git("rev-parse", "HEAD"), "status_porcelain": git("status", "--porcelain"),
            "source_sha256": hashes, "tree_sha256": digest, "pycache_dirs": pycache}


# --- evidence root ------------------------------------------------------------

def fresh_run_dir(parent, case):
    """Validate the evidence parent (and its linked ancestors) before writing."""
    text = os.fspath(parent)
    if not os.path.isabs(text) or os.path.normpath(text) != text.rstrip(os.sep):
        raise SystemExit("output_parent_must_be_absolute_and_normalized")
    parent = Path(text)
    if parent == REPO or REPO in parent.parents:
        raise SystemExit("output_parent_inside_repository")
    existing = parent
    while not existing.exists():
        if existing.is_symlink():
            raise SystemExit("output_parent_dangling_symlink")
        existing = existing.parent
    for ancestor in [existing, *existing.parents]:
        info = os.lstat(ancestor)
        if stat.S_ISLNK(info.st_mode):
            raise SystemExit("output_parent_linked_ancestor")
        if info.st_mode & stat.S_IWOTH and not info.st_mode & stat.S_ISVTX:
            raise SystemExit("output_parent_world_writable_ancestor")
    if os.path.realpath(existing) != str(existing):
        raise SystemExit("output_parent_not_canonical")
    if existing == parent and (not parent.is_dir() or os.stat(parent).st_uid != os.geteuid()):
        raise SystemExit("output_parent_not_owned_directory")
    resolved = parent.resolve()
    if resolved == REPO or REPO in resolved.parents:
        raise SystemExit("output_parent_inside_repository")
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run = parent / f"{case}-{stamp}-{os.getpid()}-{secrets.token_hex(4)}"
    run.mkdir(mode=0o700, exist_ok=False)
    return run


# --- sentinels ----------------------------------------------------------------

def install_network_guards():
    def refuse(name):
        def guard(*args, **kwargs):
            GUARD_HITS.append(name)
            raise OSError(f"offline acceptance guard refused {name}")
        return guard
    socket.socket.connect = refuse("socket.connect")
    socket.socket.connect_ex = refuse("socket.connect_ex")
    socket.create_connection = refuse("socket.create_connection")
    socket.getaddrinfo = refuse("socket.getaddrinfo")
    socket.gethostbyname = refuse("socket.gethostbyname")


class ModelClientGuard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + ".") for name in MODEL_CLIENTS):
            GUARD_HITS.append(f"import:{fullname}")
            raise ImportError(f"offline acceptance guard refused model client import {fullname}")
        return None


WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
# audit event -> ((path arg index, dir_fd arg index or None), ...)
WRITE_EVENTS = {"os.mkdir": ((0, 2),), "os.rename": ((0, 2), (1, 3)), "os.replace": ((0, 2), (1, 3)),
                "os.remove": ((0, 1),), "os.rmdir": ((0, 1),), "os.truncate": ((0, None),),
                "os.symlink": ((1, 2),), "os.link": ((1, 3),), "os.chmod": ((0, 2),),
                "shutil.rmtree": ((0, 1),), "shutil.copyfile": ((1, None),), "sqlite3.connect": ((0, None),)}
# Events that act on a final symlink itself rather than its target.
NO_FOLLOW_EVENTS = {"os.remove", "os.rmdir", "os.rename", "os.replace", "os.symlink", "os.link", "shutil.rmtree"}


def under(resolved, root):
    return resolved == root or resolved.startswith(root.rstrip(os.sep) + os.sep)


def install_file_guards(run, source_root):
    """Audit-hook write guard (run root only) and live-file read guard."""
    root = os.path.realpath(run)
    home = os.path.realpath(str(Path.home()))
    read_roots = sorted({os.path.realpath(p) for p in (REPO, source_root, run, sys.prefix, sys.base_prefix,
                                                         sys.exec_prefix)})

    def resolve(target, dir_fd=None, follow=True):
        if isinstance(target, int):
            return None  # already-open descriptor
        text = os.fsdecode(target)
        if text in ("", ":memory:", os.devnull):
            return None
        if text.startswith("file:"):
            text = text[5:].split("?", 1)[0]
        if isinstance(dir_fd, int) and dir_fd >= 0 and not os.path.isabs(text):
            try:
                text = os.path.join(os.readlink(f"/proc/self/fd/{dir_fd}"), text)
            except OSError:
                return "\0unresolvable"
        if not follow:
            # The operation acts on the link itself: resolve its directory only.
            head, tail = os.path.split(os.path.abspath(text))
            return os.path.join(os.path.realpath(head), tail)
        return os.path.realpath(text)

    def refuse(kind, event, resolved):
        probe = os.path.basename(resolved).startswith(PROBE_PREFIX) or PROBE_PREFIX in resolved
        (PROBE_HITS if probe else kind).append(f"{event}:{'probe' if probe else resolved}")
        raise PermissionError(f"acceptance guard refused {event}")

    def hook(event, args):
        if event == "open":
            path, mode, flags = (tuple(args) + (None, None, None))[:3]
            if path is None:
                return
            resolved = resolve(path)
            if resolved is None:
                return
            writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                isinstance(flags, int) and flags & WRITE_FLAGS)
            if writing and not under(resolved, root):
                refuse(WRITE_HITS, event, resolved)
            if under(resolved, home) and not any(under(resolved, r) for r in read_roots):
                refuse(READ_HITS, event, resolved)
        elif event in WRITE_EVENTS:
            follow = event not in NO_FOLLOW_EVENTS
            for p, d in WRITE_EVENTS[event]:
                if p < len(args) and args[p] is not None:
                    resolved = resolve(args[p], args[d] if d is not None and d < len(args) else None, follow)
                    if resolved is not None and not under(resolved, root):
                        refuse(WRITE_HITS, event, resolved)

    sys.addaudithook(hook)
    return {"run_root": root, "read_roots": read_roots, "home_guarded": True}


def sentinel_self_check(run):
    """Inert probes: each must be refused by the guard, not by the OS."""
    token = secrets.token_hex(6)
    results = {}
    write_escape = os.path.join(os.sep, f"{PROBE_PREFIX}{token}", "probe")
    try:
        open(write_escape, "w").close()
        results["write_escape_refused"] = False
    except PermissionError:
        results["write_escape_refused"] = True
    except OSError:
        results["write_escape_refused"] = False
    live = Path.home() / f".{PROBE_PREFIX}{token}-absent"
    try:
        open(live, "rb").close()
        results["live_read_refused"] = False
    except PermissionError:
        results["live_read_refused"] = True
    except OSError:
        results["live_read_refused"] = False
    before = len(GUARD_HITS)
    try:
        socket.create_connection(("192.0.2.1", 9), timeout=0.01)  # TEST-NET-1; refused before any syscall
        results["network_refused"] = False
    except OSError as exc:
        results["network_refused"] = "offline acceptance guard" in str(exc)
    try:
        __import__(MODEL_CLIENTS[0])
        results["model_import_refused"] = False
    except ImportError as exc:
        results["model_import_refused"] = "offline acceptance guard" in str(exc)
    del GUARD_HITS[before:]  # probe hits are expected; only test-time hits fail a case
    positive = run / "guard-positive.txt"
    positive.write_text("in-root write allowed\n")
    results["in_root_positive_written"] = positive.is_file()
    results["probe_refusals"] = len(PROBE_HITS)
    results["ok"] = all(results[k] for k in ("write_escape_refused", "live_read_refused", "network_refused",
                                             "model_import_refused", "in_root_positive_written"))
    return results


def prepare(run, source_root):
    """Everything that must happen before any repository module is imported."""
    tmp = run / "tmp"
    tmp.mkdir(mode=0o700)
    tempfile.tempdir = str(tmp)
    os.environ["TMPDIR"] = str(tmp)
    sys.dont_write_bytecode = True
    install_network_guards()
    sys.meta_path.insert(0, ModelClientGuard())
    guards = install_file_guards(run, source_root)
    guards["tempdir"] = tempfile.gettempdir()
    guards["self_check"] = sentinel_self_check(run)
    if str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))
    return guards


# --- unittest helpers -----------------------------------------------------------

def make_result_class():
    import unittest

    class Recording(unittest.TextTestResult):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.passed = []

        def addSuccess(self, test):
            super().addSuccess(test)
            self.passed.append(test.id())
    return Recording


def run_suite(names, stream):
    import unittest
    suite = unittest.defaultTestLoader.loadTestsFromNames(list(names))
    runner = unittest.TextTestRunner(stream=stream, verbosity=2, resultclass=make_result_class())
    return runner.run(suite)


def parent_id(test):
    return getattr(test, "test_case", test).id()


def counts(result):
    return {"tests_run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
            "skipped": len(result.skipped), "expected_failures": len(result.expectedFailures),
            "unexpected_successes": len(result.unexpectedSuccesses)}


def clean(c):
    return (c["tests_run"] > 0 and c["failures"] == 0 and c["errors"] == 0 and c["skipped"] == 0
            and c["expected_failures"] == 0 and c["unexpected_successes"] == 0)


def failing_ids(result):
    return sorted({parent_id(t) for t, _ in result.failures + result.errors})


def module_paths(names):
    paths = {}
    for name in names:
        module = sys.modules.get(name)
        paths[name] = os.path.realpath(module.__file__) if module is not None and getattr(module, "__file__", None) else None
    return paths


def write_raw(run, name, text):
    raw = text.encode()
    (run / name).write_bytes(raw)
    return sha256_bytes(raw)


# --- cases ----------------------------------------------------------------------

def case_behavior(run):
    stream = io.StringIO()
    result = run_suite([REGRESSION], stream)
    c = counts(result)
    passed = set(result.passed)
    rows = {row: {"tests": names, "missing_or_failed": sorted(set(names) - passed)} for row, names in ROWS.items()}
    rows_ok = all(names and not rows[row]["missing_or_failed"] for row, names in ROWS.items())
    ok = clean(c) and rows_ok and not GUARD_HITS
    return ok, {"modules": [REGRESSION], "counts": c, "acceptance_rows": rows, "rows_ok": rows_ok,
                "red_or_failing_ids": failing_ids(result), "module_paths": module_paths(FOCUSED_MODULES),
                "raw_output": "unittest.txt", "raw_output_sha256": write_raw(run, "unittest.txt", stream.getvalue())}


def case_compatibility(run):
    stream = io.StringIO()
    result = run_suite(COMPATIBILITY, stream)
    c = counts(result)
    ok = clean(c) and not GUARD_HITS
    return ok, {"modules": list(COMPATIBILITY), "counts": c, "failing_ids": failing_ids(result),
                "module_paths": module_paths(FOCUSED_MODULES),
                "raw_output": "unittest.txt", "raw_output_sha256": write_raw(run, "unittest.txt", stream.getvalue())}


def case_negative_control(run):
    import unittest
    from unittest import mock
    from tools import portal_reconcile
    deciding = sorted(t.id() for t in iter_tests(unittest.defaultTestLoader.loadTestsFromName(DECIDING_CLASS)))
    real_hook = portal_reconcile._pairs_recorder
    stream = io.StringIO()
    stream.write("=== broken control: duplicate rejection disabled (object_pairs_hook -> plain dict) ===\n")
    with mock.patch.object(portal_reconcile, "_pairs_recorder", lambda duplicates: dict):
        broken = run_suite([DECIDING_CLASS], stream)
        stream.write("=== broken control: held-observation positive (real classifier) ===\n")
        broken_positive = run_suite([HELD_POSITIVE], stream)
    restored_hook = portal_reconcile._pairs_recorder is real_hook
    stream.write("=== restored hook: deciding regression ===\n")
    restored = run_suite([DECIDING_CLASS], stream)
    stream.write("=== restored hook: held-observation positive ===\n")
    restored_positive = run_suite([HELD_POSITIVE], stream)
    phases = {"broken": outcome_summary(broken), "broken_positive": outcome_summary(broken_positive),
              "restored": outcome_summary(restored), "restored_positive": outcome_summary(restored_positive)}
    ok, reasons = negative_control_verdict(deciding, phases, restored_hook)
    ok = ok and not GUARD_HITS
    broken_failing = phases["broken"]["failure_ids"]
    return ok, {"deciding_tests": deciding, "expected_deciding_tests": list(DECIDING_TESTS),
                "broken_control": phases["broken"]["counts"],
                "broken_control_assertion_failure_ids": broken_failing,
                "broken_control_error_ids": phases["broken"]["error_ids"],
                "broken_control_failures_are_refusal_assertions": phases["broken"]["failures_are_refusal_assertions"],
                "every_deciding_test_detected_broken_control": broken_failing == list(DECIDING_TESTS),
                "contradictory_allowlisted_detected": DECIDING_CONTRADICTION in broken_failing
                and phases["broken"]["failures_are_refusal_assertions"],
                "broken_held_positive": phases["broken_positive"]["counts"], "hook_restored": restored_hook,
                "restored": phases["restored"]["counts"],
                "restored_held_positive": phases["restored_positive"]["counts"],
                "verdict_reasons": reasons, "module_paths": module_paths(FOCUSED_MODULES),
                "raw_output": "unittest.txt", "raw_output_sha256": write_raw(run, "unittest.txt", stream.getvalue())}


def outcome_summary(result):
    """Counts plus assertion-failure and error ids, kept apart."""
    return {"counts": counts(result),
            "failure_ids": sorted({parent_id(t) for t, _ in result.failures}),
            "error_ids": sorted({parent_id(t) for t, _ in result.errors}),
            "failures_are_refusal_assertions": bool(result.failures) and all(
                REFUSAL_FAILURE.search(tb) for _, tb in result.failures)}


def negative_control_verdict(loaded, phases, hook_restored, expected=DECIDING_TESTS,
                             positive_count=1, contradiction=DECIDING_CONTRADICTION):
    """A4 verdict; returns (ok, reasons). Fails closed on anything but detection.

    The broken phase must run the complete expected deciding population and
    every test must fail ONLY through the refusal assertion: any error, skip,
    expected failure, unexpected success, pass or other assertion is a
    failed control, never detection. The held positive must pass in both
    phases and the restored deciding population must pass completely.
    """
    expected = sorted(expected)
    reasons = []
    broken = phases["broken"]
    bc = broken["counts"]
    if not expected or sorted(loaded) != expected:
        reasons.append("deciding_population_mismatch")
    if bc["tests_run"] != len(expected):
        reasons.append("broken_population_incomplete")
    for key in ("errors", "skipped", "expected_failures", "unexpected_successes"):
        if bc[key]:
            reasons.append(f"broken_control_{key}")
    if broken["failure_ids"] != expected:
        reasons.append("broken_control_not_detected_by_every_deciding_test")
    if not broken["failures_are_refusal_assertions"]:
        reasons.append("broken_control_failures_not_refusal_assertions")
    if contradiction not in broken["failure_ids"]:
        reasons.append("contradictory_allowlisted_not_detected")
    for name, population in (("broken_positive", positive_count), ("restored", len(expected)),
                             ("restored_positive", positive_count)):
        c = phases[name]["counts"]
        if not clean(c) or c["tests_run"] != population:
            reasons.append(f"{name}_not_clean")
    if not hook_restored:
        reasons.append("hook_not_restored")
    return not reasons, reasons


def iter_tests(suite):
    for item in suite:
        if hasattr(item, "__iter__"):
            yield from iter_tests(item)
        else:
            yield item


def copy_source(destination):
    listed = subprocess.run(["git", "-C", str(REPO), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                            capture_output=True, check=True).stdout.decode().split("\0")
    copied = 0
    digest = hashlib.sha256()
    for relative in sorted(set(listed) - {""}):
        source = REPO / relative
        if not source.exists() and not source.is_symlink():
            continue  # deleted in the working tree
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target, follow_symlinks=False)
        data = b"symlink:" + os.readlink(source).encode() if source.is_symlink() else source.read_bytes()
        digest.update(relative.encode() + b"\0" + hashlib.sha256(data).digest())
        copied += 1
    return copied, digest.hexdigest()


def path_values(value):
    if isinstance(value, Path):
        yield str(value)
    elif isinstance(value, (str, bytes)):
        text = os.fsdecode(value) if isinstance(value, bytes) else value
        # Absolute strings whose top-level directory exists on this host are
        # treated as filesystem paths; URL-path markers such as "/documents"
        # (no such top-level directory) are not runtime data locations.
        parts = text.split(os.sep)
        if (text.startswith(os.sep) and "\n" not in text and len(parts) > 1 and parts[1]
                and os.path.isdir(os.sep + parts[1])):
            yield text
    elif isinstance(value, (tuple, list, frozenset, set)):
        for item in value:
            yield from path_values(item)


def runtime_path_inventory(module):
    """Absolute path-like module constants and bound function defaults."""
    found = []
    for name, value in sorted(vars(module).items()):
        if name.startswith("__"):
            continue
        for text in path_values(value):
            found.append({"kind": "constant", "name": name, "path": os.path.realpath(text)})
        if callable(value) and getattr(value, "__module__", None) == module.__name__:
            defaults = list(getattr(value, "__defaults__", None) or ()) + list(
                (getattr(value, "__kwdefaults__", None) or {}).values())
            for text in path_values(defaults):
                found.append({"kind": "default", "name": name, "path": os.path.realpath(text)})
    return found


def case_isolation(run, copy_root):
    import unittest  # noqa: F401 - stdlib only before the copy is imported
    stream = io.StringIO()
    result = run_suite([REGRESSION, *COMPATIBILITY], stream)
    c = counts(result)
    paths = module_paths(FOCUSED_MODULES)
    copy_text = os.path.realpath(copy_root)
    repo_text = str(REPO)
    paths_ok = all(p is not None and under(p, copy_text) and not under(p, repo_text) for p in paths.values())
    run_text = os.path.realpath(run)
    inventory = {}
    for name in ("tools.portal_reconcile", REGRESSION, "tests.test_portal_reconcile"):
        inventory[name] = runtime_path_inventory(sys.modules[name])
    outside = [dict(entry, module=name) for name, entries in inventory.items() for entry in entries
               if not under(entry["path"], run_text)]
    _, copy_pycache = tree_digest(copy_root)
    ok = (clean(c) and paths_ok and not outside and copy_pycache == 0 and sys.dont_write_bytecode
          and under(os.path.realpath(tempfile.gettempdir()), run_text) and not GUARD_HITS)
    return ok, {"modules": [REGRESSION, *COMPATIBILITY], "counts": c, "failing_ids": failing_ids(result),
                "source_copy": str(copy_root), "module_paths": paths, "module_paths_in_copy": paths_ok,
                "runtime_path_inventory": inventory, "paths_outside_run": outside,
                "copy_pycache_dirs": copy_pycache, "dont_write_bytecode": sys.dont_write_bytecode,
                "tempdir": tempfile.gettempdir(),
                "child_processes": "CLI subprocesses started by tests use the copy's script with -B; not audited",
                "raw_output": "unittest.txt", "raw_output_sha256": write_raw(run, "unittest.txt", stream.getvalue())}


SUMMARY = re.compile(r"^Ran (\d+) tests? in [0-9.]+s$", re.M)
FINAL = re.compile(r"^(OK|FAILED)(?: \(([^)]*)\))?$", re.M)
VERBOSE_RESULT = re.compile(r"^(\S+) \(([\w.]+)\)(?:\n.*?)? \.\.\. (ok|FAIL|ERROR|skipped.*|expected failure|unexpected success)$",
                            re.M)


def parse_unittest(text):
    ran = SUMMARY.findall(text)
    finals = FINAL.findall(text)
    if len(ran) != 1 or not finals:
        return None
    status, detail = finals[-1]
    fields = dict(part.strip().split("=", 1) for part in detail.split(",") if "=" in part)
    return {"tests_run": int(ran[0]), "status": status,
            "failures": int(fields.get("failures", 0)), "errors": int(fields.get("errors", 0)),
            "skipped": int(fields.get("skipped", 0)), "expected_failures": int(fields.get("expected failures", 0)),
            "unexpected_successes": int(fields.get("unexpected successes", 0))}


def verbose_id(name, location):
    """Test id from a verbose line; Python >= 3.11 prints the full id."""
    return location if location.endswith("." + name) else f"{location}.{name}"


def expected_new_test_ids():
    """Complete loaded population of the new regression module, checked
    against the frozen count and every named acceptance/deciding test."""
    import unittest
    loaded = sorted(t.id() for t in iter_tests(unittest.defaultTestLoader.loadTestsFromName(REGRESSION)))
    named = {name for names in ROWS.values() for name in names} | set(DECIDING_TESTS)
    reasons = []
    if len(loaded) != EXPECTED_NEW_TEST_COUNT or len(set(loaded)) != len(loaded):
        reasons.append("new_test_population_count_mismatch")
    if not named <= set(loaded):
        reasons.append("named_acceptance_tests_not_loaded")
    return loaded, reasons


def aggregate_command(label, code, text, expected_new):
    """Fail-closed verdict for one full-ci child; returns (passed, entry).

    Every required population must exit 0, report OK with a nonzero count and
    ZERO failures, errors, skips, expected failures and unexpected successes;
    there is no skip exemption here (any qualification belongs to the existing
    owner). The verbose records.yml discovery must additionally report every
    expected new regression test exactly once, each ok.
    """
    parsed = parse_unittest(text)
    reasons = []
    if code != 0:
        reasons.append("nonzero_exit")
    if parsed is None:
        reasons.append("missing_or_malformed_report")
    else:
        if parsed["status"] != "OK":
            reasons.append("status_not_ok")
        if parsed["tests_run"] <= 0:
            reasons.append("empty_population")
        for key in ("failures", "errors", "skipped", "expected_failures", "unexpected_successes"):
            if parsed[key]:
                reasons.append(f"unqualified_{key}")
    entry = {"exit": code, "counts": parsed}
    if label == "ci_records_discovery":
        outcomes = [(verbose_id(name, location), outcome) for name, location, outcome in VERBOSE_RESULT.findall(text)]
        new = [(tid, outcome) for tid, outcome in outcomes if tid.startswith(R)]
        new_ok = sorted(tid for tid, outcome in new if outcome == "ok")
        not_ok = sorted(f"{tid}: {outcome}" for tid, outcome in outcomes
                        if outcome != "ok" and tid.startswith((R, COMPATIBILITY[0] + ".")))
        skipped = sorted(f"{tid}: {outcome}" for tid, outcome in outcomes
                         if outcome.startswith("skipped") or outcome in ("expected failure", "unexpected success"))
        expected = sorted(expected_new)
        missing = sorted(set(expected) - set(new_ok))
        unexpected_new = sorted({tid for tid, _ in new} - set(expected))
        if not expected:
            reasons.append("expected_new_population_empty")
        if missing:
            reasons.append("new_regression_tests_not_all_collected_ok")
        if len(new) != len(expected) or unexpected_new:
            reasons.append("new_regression_population_mismatch")
        if not_ok:
            reasons.append("focused_not_ok")
        if skipped:
            reasons.append("unqualified_skip_or_expected_failure_lines")
        entry.update({"new_regression_tests_expected": len(expected),
                      "new_regression_tests_collected_ok": len(new_ok),
                      "new_regression_tests_missing": missing, "new_regression_tests_unexpected": unexpected_new,
                      "focused_not_ok": not_ok, "skip_or_expected_failure_inventory": skipped})
    entry["verdict_reasons"] = reasons
    entry["result"] = "PASS" if not reasons else "FAIL"
    return not reasons, entry


def case_full_ci(run, timeout):
    tmp = run / "tmp-full-ci"
    tmp.mkdir(mode=0o700)
    env = dict(os.environ, TMPDIR=str(tmp), PYTHONDONTWRITEBYTECODE="1")
    commands = {
        "sd15_discovery": [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."],
        "ci_s3a": [sys.executable, "-B", "-W", "error::ResourceWarning", "-m", "unittest", *CI_S3A],
        "ci_records_discovery": [sys.executable, "-B", "-W", "error::ResourceWarning", "-m", "unittest",
                                 "discover", "-v"],
    }
    expected_new, population_reasons = expected_new_test_ids()
    details = {"env_overrides": {"TMPDIR": str(tmp), "PYTHONDONTWRITEBYTECODE": "1"}, "cwd": str(REPO),
               "expected_new_tests": expected_new, "expected_new_population_reasons": population_reasons,
               "commands": {}}
    ok = not population_reasons
    for label, argv in commands.items():
        try:
            done = subprocess.run(argv, cwd=REPO, env=env, capture_output=True, timeout=timeout, check=False)
            stdout, stderr, code = done.stdout, done.stderr, done.returncode
        except subprocess.TimeoutExpired as exc:
            stdout, stderr, code = exc.stdout or b"", exc.stderr or b"", "timeout"
        (run / f"{label}.stdout.txt").write_bytes(stdout)
        (run / f"{label}.stderr.txt").write_bytes(stderr)
        passed, entry = aggregate_command(label, code, stderr.decode(errors="replace"), expected_new)
        entry.update({"argv": argv, "stdout_sha256": sha256_bytes(stdout), "stderr_sha256": sha256_bytes(stderr)})
        details["commands"][label] = entry
        ok = ok and passed
    return ok, details


def case_credentials(run):
    sibling = Path(sys.executable).parent / "detect-secrets"
    if not (sibling.is_file() and os.access(sibling, os.X_OK)):
        return False, {"stop": "missing_dependency: detect-secrets not installed beside the interpreter; no install"}
    scan_argv = [str(sibling), "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"]
    report = run / "secret-scan.json"
    with open(report, "wb") as out:
        scan = subprocess.run(scan_argv, cwd=REPO, stdout=out, stderr=subprocess.PIPE, check=False)
    (run / "scan-stderr.txt").write_bytes(scan.stderr)
    if scan.returncode != 0:
        return False, {"scan_argv": scan_argv, "scan_exit": scan.returncode, "stop": "scan_failed"}
    try:
        data = json.loads(report.read_bytes())
        results = data["results"]
        if not isinstance(results, dict):
            raise ValueError
    except (ValueError, KeyError):
        return False, {"scan_argv": scan_argv, "stop": "malformed_scan_report"}
    check_argv = [sys.executable, str(REPO / "tools" / "check_secret_report.py"), str(report)]
    check = subprocess.run(check_argv, cwd=REPO, capture_output=True, check=False)
    raw = check.stdout + check.stderr
    (run / "check-secret-report.txt").write_bytes(raw)
    return check.returncode == 0, {
        "scan_argv": scan_argv, "scan_cwd": str(REPO), "scan_exit": scan.returncode,
        "scan_json": "secret-scan.json", "scan_json_sha256": sha256_bytes(report.read_bytes()),
        "detect_secrets_version": data.get("version"), "files_with_hits": sorted(results),
        "check_argv": check_argv, "check_exit": check.returncode,
        "check_output_last_line": raw.decode(errors="replace").strip().splitlines()[-1:] if raw else [],
        "check_output_sha256": sha256_bytes(raw)}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Scoped offline acceptance for FA41 duplicate JSON names.")
    parser.add_argument("--case", required=True, choices=CASES)
    parser.add_argument("--output-parent", required=True)
    parser.add_argument("--timeout", type=int, default=3600, help="full-ci per-command timeout (seconds)")
    args = parser.parse_args(argv)
    run = fresh_run_dir(args.output_parent, args.case)
    before = identity()
    details = {}
    guards = {}
    ok = False
    try:
        source_root = REPO
        if args.case == "isolation":
            source_root = run / "source-copy"
            source_root.mkdir()
            details["source_copy_files"], details["source_copy_sha256"] = copy_source(source_root)
        guards = prepare(run, source_root)
        if not guards["self_check"]["ok"]:
            ok, details["stop"] = False, "sentinel_self_check_failed"
        elif args.case == "behavior":
            ok, more = case_behavior(run)
            details.update(more)
        elif args.case == "compatibility":
            ok, more = case_compatibility(run)
            details.update(more)
        elif args.case == "negative-control":
            ok, more = case_negative_control(run)
            details.update(more)
        elif args.case == "isolation":
            ok, more = case_isolation(run, source_root)
            details.update(more)
        elif args.case == "full-ci":
            ok, more = case_full_ci(run, args.timeout)
            details.update(more)
        elif args.case == "credentials":
            ok, more = case_credentials(run)
            details.update(more)
        else:
            ok, details["stop"] = False, "unknown_case"
    except Exception as exc:  # unknown failure stops acceptance
        ok, details["stop"] = False, f"unknown_failure: {type(exc).__name__}: {exc}"
    after = identity()
    if after != before:
        ok, details["stop"] = False, "candidate_identity_changed_during_run"
        details["identity_after"] = after
    for name, hits in (("write_outside_run_root_refused", WRITE_HITS), ("live_file_read_refused", READ_HITS),
                       ("network_or_model_guard_hit", GUARD_HITS)):
        if hits:
            ok, details["stop"] = False, name
    guards.update({"refused_writes": list(WRITE_HITS), "refused_reads": list(READ_HITS),
                   "network_model_hits": list(GUARD_HITS), "probe_refusals_total": len(PROBE_HITS)})
    record = {"case": args.case, "argv": [sys.executable, *sys.argv], "cwd": os.getcwd(),
              "interpreter": sys.executable, "python": sys.version.split()[0], "run_dir": str(run),
              "guards": guards, "identity": before,
              "result": "PASS_REVIEW_REQUIRED" if ok else "FAIL", "exit": 0 if ok else 1, **details}
    text = json.dumps(record, indent=2, sort_keys=True) + "\n"
    (run / "result.json").write_text(text)
    print(text, end="")
    return 0 if ok is True and record["exit"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
