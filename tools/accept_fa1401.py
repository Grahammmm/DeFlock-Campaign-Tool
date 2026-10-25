#!/usr/bin/env python3
"""FA1401 receipt-ordinal acceptance launcher: one --case per invocation.

Every invocation creates a unique retained run directory under --output-root
(outside the repository), records argv, cwd, identity, child commands with raw
stdout/stderr and exits, unittest counts and outcomes, and exits 0 only when
every recorded check passed. The result is REVIEW REQUIRED evidence, never
semantic, legal, stage, release or live acceptance. Child processes are the
enumerated literal argv arrays below, run without a shell.
"""
import argparse
import ast
from datetime import datetime, timezone
import hashlib
import http.client
import importlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import urllib.request

sys.dont_write_bytecode = True
REPO = Path(__file__).resolve().parents[1]
CARD = {"brief": "FA1401-RECEIPT-ORDINAL", "task": "t_bbb2338f",
        "title": "Reject negative receipt-source ordinals before they alias the last manifest entry."}
CASES = ("behavior", "compatibility", "negative-control", "isolation", "credential-scan", "public-tree")
SOURCE = "campaign_tool/records/ledger/receipt_projection.py"
TEST = "tests/records/test_fa1401.py"
RUNNER = "tools/accept_fa1401.py"
FIXTURE_PREFIX = "tests/fixtures/fa1401/"
BOUNDARY = FIXTURE_PREFIX + "offline_boundary.py"
BOUNDARY_NAME = "fa1401_offline_boundary"
WRITE_SET = (SOURCE, TEST, RUNNER, BOUNDARY)
TEST_MODULE = "tests.records.test_fa1401"
TEST_CLASS = "ReceiptOrdinalTests"
REQUIRED_CASES = ("offline_file_boundary_scoped", "index_zero_selects_first_entry",
                  "registered_exact_ordinals_select_their_matching_entry",
                  "registered_negative_ordinals_return_zero", "direct_negative_source_insert_raises_integrity_error",
                  "non_exact_integer_ordinals_rejected", "out_of_range_ordinals_rejected",
                  "complete_binding_comparison_preserved", "sql_affinity_cases_judged_as_delivered",
                  "historical_negative_source_refused_not_repaired", "historical_valid_capture_still_verifies",
                  "projected_manifest_captures_and_replays_identically")
DECIDING = ("registered_negative_ordinals_return_zero", "direct_negative_source_insert_raises_integrity_error",
            "sql_affinity_cases_judged_as_delivered", "historical_negative_source_refused_not_repaired")
POSITIVE = "index_zero_selects_first_entry"
COMPAT_MODULE = "tests.records.test_ledger_receipts"
COMPATIBILITY = (COMPAT_MODULE + ".ReceiptProjectionTests.test_fresh_insert_wrong_source_mapping_rejected",
                 COMPAT_MODULE + ".ReceiptProjectionTests.test_exact_bytes_roles_and_nonpromotion",
                 COMPAT_MODULE + ".ReceiptProjectionTests.test_checkpoint_resume")
COMPAT_FILE = "tests/records/test_ledger_receipts.py"
COMPAT_ALLOWED_IMPORTS = {"hashlib", "json", "pathlib", "sqlite3", "tempfile", "unittest", "unittest.mock",
                          "campaign_tool.records.ledger"}
NETWORK_MODULES = ("campaign_tool.records.portal.https_transport", "campaign_tool.digest.model",
                   "campaign_tool.outbox", "campaign_tool.meetings", "campaign_tool.discovery",
                   "campaign_tool.records.intake.imap_intake")
# Assembled from pieces so this file does not match its own portability scan.
HOST_TOKENS = ("/" + "home/", "/" + "Users/", "agent" + "-work", "." + "hermes", "kan" + "ban", "/" + "tmp/")
ARTIFACT_ENV = "FA1401_ARTIFACT_ROOT"
TIMEOUT = 1800
MARK = "fa1401_launcher_sentinel"


def sha_bytes(raw):
    return hashlib.sha256(raw).hexdigest()


def sha_file(path):
    return sha_bytes(Path(path).read_bytes())


def now():
    return datetime.now(timezone.utc).isoformat()


class Run:
    def __init__(self, case, output_root):
        self.case = case
        output_root = Path(os.path.abspath(output_root))
        if output_root == REPO or REPO in output_root.parents:
            raise SystemExit("output root must be outside the repository")
        output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        self.directory = output_root / ("run-%s-%d" % (stamp, os.getpid()))
        self.directory.mkdir(mode=0o700)
        self.checks = []
        self.files = {}
        self.receipt = {"schema": "fa1401-acceptance-v1", "card": CARD, "case": case, "argv": list(sys.argv),
                        "cwd": os.getcwd(), "executable": sys.executable, "python": sys.version,
                        "sqlite": sqlite3.sqlite_version, "repo": str(REPO), "started_at": now(),
                        "run_directory": str(self.directory), "status": "REVIEW_REQUIRED_NOT_ACCEPTANCE",
                        "full_discovery": "operator-run; scoped rows are not full-repository CI"}

    def require(self, name, ok, detail=None):
        self.checks.append({"name": name, "ok": bool(ok), "detail": detail})
        return bool(ok)

    def save(self, name, data):
        raw = data.encode() if isinstance(data, str) else data
        path = self.directory / name
        with open(path, "xb") as stream:
            stream.write(raw)
        self.files[name] = {"path": str(path), "sha256": sha_bytes(raw), "bytes": len(raw)}
        return path

    def command(self, label, argv, *, cwd, env=None):
        started = now()
        try:
            done = subprocess.run([str(part) for part in argv], cwd=cwd, env=env, capture_output=True,
                                  timeout=TIMEOUT, shell=False, check=False)
            code, stdout, stderr, error = done.returncode, done.stdout, done.stderr, None
        except (OSError, subprocess.TimeoutExpired) as caught:
            code, stdout, stderr, error = None, b"", b"", type(caught).__name__ + ": " + str(caught)
        record = {"label": label, "argv": [str(part) for part in argv], "cwd": str(cwd), "started_at": started,
                  "ended_at": now(), "exit": code, "error": error,
                  "stdout": self.save(label + ".stdout", stdout).name,
                  "stderr": self.save(label + ".stderr", stderr).name}
        self.receipt.setdefault("commands", []).append(record)
        return record, stdout, stderr

    def scratch(self):
        """Redirect bytecode-free temp roots and fixture roots into this run before any app import."""
        tmp = self.directory / "tmp"
        fixtures = self.directory / "fixtures"
        tmp.mkdir(mode=0o700)
        fixtures.mkdir(mode=0o700)
        tempfile.tempdir = str(tmp)
        os.environ["TMPDIR"] = str(tmp)
        os.environ[ARTIFACT_ENV] = str(fixtures)
        self.receipt["temp_root"] = str(tmp)
        self.receipt["fixture_root"] = str(fixtures)
        return fixtures

    def finish(self):
        passed = bool(self.checks) and all(row["ok"] for row in self.checks)
        code = 0 if passed else 1
        self.receipt.update({"checks": self.checks, "files": self.files, "ended_at": now(), "exit": code,
                             "result": "PASS_REVIEW_REQUIRED" if passed else "FAIL"})
        raw = json.dumps(self.receipt, indent=2, sort_keys=True, default=str) + "\n"
        with open(self.directory / "receipt.json", "x", encoding="utf-8") as stream:
            stream.write(raw)
        summary = json.dumps({"case": self.case, "result": self.receipt["result"], "exit": code,
                              "run_directory": str(self.directory), "receipt_sha256": sha_bytes(raw.encode()),
                              "checks": [(row["name"], row["ok"]) for row in self.checks]}, indent=2)
        with open(self.directory / "summary.stdout.json", "x", encoding="utf-8") as stream:
            stream.write(summary + "\n")
        print(summary)
        return code


def staged_manifest():
    return json.loads((REPO.parent / "SOURCE-MANIFEST.json").read_text())


def git(run, label, *arguments):
    record, stdout, _ = run.command("git-" + label, ["git", *arguments], cwd=REPO)
    return stdout.decode() if record["exit"] == 0 else None


def in_write_set(path):
    return path in WRITE_SET or path.startswith(FIXTURE_PREFIX)


def identity(run):
    if not (REPO / ".git").exists():
        # Only the isolation row's second-prefix copy has no git metadata; the
        # parent row binds that copy's write-set bytes to the staged repository.
        run.receipt["identity"] = {"git_available": False, "second_prefix_copy": True,
                                   "file_sha256": {name: sha_file(REPO / name) for name in WRITE_SET}}
        return run.receipt["identity"]
    base = staged_manifest().get("head")
    head = (git(run, "head", "rev-parse", "HEAD") or "").strip() or None
    status = git(run, "status", "status", "--porcelain", "--untracked-files=all")
    changed = set((git(run, "diff-names", "diff", "--name-only", base, "HEAD") or "").split()) if base else set()
    dirty = {line[3:] for line in (status or "").splitlines() if line}
    outside = sorted(path for path in changed | dirty if not in_write_set(path))
    commits = (git(run, "commits", "rev-list", base + "..HEAD") or "").split() if base else []
    record = {"base_from_source_manifest": base, "head": head, "status_porcelain": status,
              "candidate_commits": commits, "committed_changes_from_base": sorted(changed),
              "paths_outside_write_set": outside,
              "file_sha256": {name: sha_file(REPO / name) if (REPO / name).is_file() else None
                              for name in WRITE_SET}}
    run.receipt["identity"] = record
    run.require("identity_head_and_base_known", bool(head and base), {"head": head, "base": base})
    run.require("identity_write_set_only", not outside, outside)
    return record


def _sentinel(hits, name):
    def refuse(*args, **kwargs):
        hits.append(name)
        raise OSError(MARK + ":" + name)
    setattr(refuse, MARK, name)
    return refuse


GUARDED = ((socket, "create_connection"), (socket, "getaddrinfo"), (socket.socket, "connect"),
           (socket.socket, "connect_ex"), (http.client.HTTPConnection, "connect"), (urllib.request, "urlopen"),
           (subprocess, "Popen"))


def load_boundary():
    """The committed scoped data-file/SQLite boundary, loaded once from this repository."""
    path = REPO / BOUNDARY
    loaded = sys.modules.get(BOUNDARY_NAME)
    if loaded is None:
        spec = importlib.util.spec_from_file_location(BOUNDARY_NAME, path)
        loaded = importlib.util.module_from_spec(spec)
        sys.modules[BOUNDARY_NAME] = loaded
        spec.loader.exec_module(loaded)
    if Path(loaded.__file__).resolve() != path:
        raise RuntimeError("fa1401 boundary loaded from another tree: " + str(loaded.__file__))
    return loaded


class Sentinels:
    """Launcher-owned network/process and data-file/SQLite boundary for in-process selected suites."""

    def __init__(self, write_roots):
        self.hits = []
        self.saved = []
        self.files = load_boundary().FileBoundary(repo=REPO, write_roots=write_roots, hits=self.hits)
        self.file_hooks_active = []
        self.file_hooks_after_suite = []

    def __enter__(self):
        for owner, attribute in GUARDED:
            self.saved.append((owner, attribute, vars(owner).get(attribute, self)))
            setattr(owner, attribute, _sentinel(self.hits, owner.__name__ + "." + attribute))
        self.files.install()
        self.file_hooks_active = load_boundary().active()
        return self

    def __exit__(self, *exc):
        self.files.restore()
        while self.saved:
            owner, attribute, value = self.saved.pop()
            if value is self:
                delattr(owner, attribute)
            else:
                setattr(owner, attribute, value)
        return False


def guarded_identity():
    record = {owner.__name__ + "." + attribute: (vars(owner).get(attribute), getattr(owner, attribute))
              for owner, attribute in GUARDED}
    record.update(load_boundary().identity())
    return record


def identity_diff(before, after):
    return sorted(name for name in before if before[name][0] is not after[name][0]
                  or before[name][1] is not after[name][1])


class RecordingResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.outcomes = {}

    def _mark(self, test, outcome):
        key = test.id().rsplit(".", 1)[-1]
        if self.outcomes.get(key) not in ("fail", "error"):
            self.outcomes[key] = outcome

    def addSuccess(self, test):
        super().addSuccess(test)
        self.outcomes.setdefault(test.id().rsplit(".", 1)[-1], "pass")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._mark(test, "fail")

    def addError(self, test, err):
        super().addError(test, err)
        self._mark(getattr(test, "test_case", test), "error")

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self._mark(test, "skip")

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self._mark(test, "expected_failure")

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self._mark(test, "unexpected_success")

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            self._mark(test, "fail" if issubclass(err[0], test.failureException) else "error")


def run_suite(run, label, suite):
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=2, resultclass=RecordingResult).run(suite)
    run.save(label + ".unittest.txt", stream.getvalue())
    summary = {"tests_run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
               "skipped": len(result.skipped), "expected_failures": len(result.expectedFailures),
               "unexpected_successes": len(result.unexpectedSuccesses), "successful": result.wasSuccessful(),
               "outcomes": result.outcomes}
    run.receipt.setdefault("unittest", {})[label] = summary
    return summary


def clean(summary, expected_count):
    return (summary["successful"] and summary["tests_run"] == expected_count and expected_count > 0
            and not (summary["skipped"] or summary["expected_failures"] or summary["unexpected_successes"]))


def load_test_module(run):
    preloaded = sorted(name for name in sys.modules if name.split(".")[0] == "campaign_tool")
    run.require("no_application_import_before_boundary", not preloaded, preloaded)
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    module = importlib.import_module(TEST_MODULE)
    run.require("test_module_from_this_repo", Path(module.__file__).resolve() == REPO / TEST, module.__file__)
    run.require("sentinels_before_application_import", module.APPLICATION_PRELOADED == [],
                module.APPLICATION_PRELOADED)
    rp = sys.modules["campaign_tool.records.ledger.receipt_projection"]
    run.require("product_module_from_this_repo", Path(rp.__file__).resolve() == REPO / SOURCE, rp.__file__)
    return module, rp


def named(module, names):
    loader = unittest.TestLoader()
    return unittest.TestSuite(loader.loadTestsFromName(TEST_CLASS + ".test_" + name, module) for name in names)


def loaded_application_modules():
    return {name: str(Path(module.__file__).resolve()) for name, module in sorted(sys.modules.items())
            if name.split(".")[0] in ("campaign_tool", "tests") and getattr(module, "__file__", None)}


def check_module_closure(run):
    modules = loaded_application_modules()
    run.receipt["loaded_modules"] = modules
    outside = sorted(name for name, path in modules.items() if not Path(path).is_relative_to(REPO))
    run.require("application_modules_from_this_repo", bool(modules) and not outside, outside)
    network = sorted(name for name in NETWORK_MODULES if name in sys.modules)
    run.require("network_capable_modules_not_loaded", not network, network)


def check_module_boundary(run, module, label, summary):
    """The test module's own file/SQLite boundary was live for every case and left nothing behind."""
    run.require(label + "_file_boundary_live_during_import", module.IMPORT_BOUNDARY_ACTIVE
                == load_boundary().target_names(), module.IMPORT_BOUNDARY_ACTIVE)
    guarded = [name for name in module.GUARDED_RUNS]
    run.receipt.setdefault("boundary_guarded_tests", {})[label] = guarded
    run.require(label + "_file_boundary_live_for_every_case", len(guarded) == summary["tests_run"] > 0,
                {"guarded": len(guarded), "tests_run": summary["tests_run"]})
    del module.GUARDED_RUNS[:]
    run.require(label + "_offline_sentinels_untouched", module.SENTINEL_HITS == [], module.SENTINEL_HITS)
    run.require(label + "_file_boundary_restored", load_boundary().active() == [] and module.guards_active() == []
                and module.MODULE_BOUNDARY == [], {"active": module.guards_active()})


def case_behavior(run):
    identity(run)
    fixtures = run.scratch()
    before = guarded_identity()
    module, _ = load_test_module(run)
    declared = sorted(name[5:] for name in dir(getattr(module, TEST_CLASS)) if name.startswith("test_"))
    run.require("frozen_case_set_exact", declared == sorted(REQUIRED_CASES), declared)
    summary = run_suite(run, "behavior", named(module, REQUIRED_CASES))
    run.require("behavior_all_named_cases_pass", clean(summary, len(REQUIRED_CASES))
                and summary["outcomes"] == {"test_" + name: "pass" for name in REQUIRED_CASES}, summary)
    check_module_boundary(run, module, "behavior", summary)
    run.require("guarded_callables_restored", identity_diff(before, guarded_identity()) == [],
                identity_diff(before, guarded_identity()))
    check_module_closure(run)
    stray = sorted(str(path) for path in fixtures.iterdir())
    run.require("fixture_roots_cleaned_by_their_owner", not stray, stray)


def inspect_compat_file(run):
    tree = ast.parse((REPO / COMPAT_FILE).read_text())
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.add(node.module)
    run.receipt["compatibility_static"] = {"file": COMPAT_FILE, "sha256": sha_file(REPO / COMPAT_FILE),
                                           "imports": sorted(imports)}
    run.require("compatibility_imports_inspected_and_inert", imports <= COMPAT_ALLOWED_IMPORTS,
                sorted(imports - COMPAT_ALLOWED_IMPORTS))
    changed = git(run, "compat-unchanged", "diff", "--name-only", staged_manifest()["head"], "--", COMPAT_FILE)
    run.require("compatibility_file_unchanged_from_base", changed == "", changed)


def case_compatibility(run):
    identity(run)
    run.scratch()
    inspect_compat_file(run)
    before = guarded_identity()
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    with Sentinels((run.directory,)) as guard:
        module = importlib.import_module(COMPAT_MODULE)
        suite = unittest.TestSuite(unittest.TestLoader().loadTestsFromName(name) for name in COMPATIBILITY)
        summary = run_suite(run, "compatibility", suite)
        guard.file_hooks_after_suite = load_boundary().active()
    run.require("compatibility_module_from_this_repo", Path(module.__file__).resolve() == REPO / COMPAT_FILE,
                module.__file__)
    run.require("compatibility_exact_selectors_pass", clean(summary, len(COMPATIBILITY))
                and sorted(summary["outcomes"]) == sorted(name.rsplit(".", 1)[-1] for name in COMPATIBILITY)
                and set(summary["outcomes"].values()) == {"pass"}, summary)
    run.require("compatibility_file_boundary_live_during_suite",
                guard.file_hooks_active == guard.file_hooks_after_suite == load_boundary().target_names(),
                [guard.file_hooks_active, guard.file_hooks_after_suite])
    run.receipt["compatibility_write_roots"] = guard.files.write_roots
    run.require("launcher_sentinels_untouched", guard.hits == [], guard.hits)
    run.require("compatibility_file_boundary_restored", load_boundary().active() == [], load_boundary().active())
    run.require("guarded_callables_restored", identity_diff(before, guarded_identity()) == [],
                identity_diff(before, guarded_identity()))
    check_module_closure(run)


def case_negative_control(run):
    identity(run)
    run.scratch()
    module, rp = load_test_module(run)
    on_disk = sha_file(REPO / SOURCE)
    original = getattr(rp, "_manifest_entry", None)
    if not run.require("deciding_predicate_present", callable(original)):
        return

    def unguarded(indexes, ordinal):
        # Deliberate in-memory regression: plain Python indexing, nothing else changed.
        return indexes[ordinal]

    with mock.patch.object(rp, "_manifest_entry", unguarded):
        broken = run_suite(run, "regressed", named(module, DECIDING + (POSITIVE,)))
    check_module_boundary(run, module, "regressed", broken)
    for name in DECIDING:
        run.require("regression_detected_" + name, broken["outcomes"].get("test_" + name) == "fail",
                    broken["outcomes"])
    run.require("regression_unaffected_positive_passes", broken["outcomes"].get("test_" + POSITIVE) == "pass",
                broken["outcomes"])
    run.require("regression_is_assertion_failure_not_error",
                broken["errors"] == 0 and broken["failures"] >= len(DECIDING) and broken["tests_run"] == len(DECIDING) + 1, broken)
    run.require("predicate_restored_in_memory", rp._manifest_entry is original)
    restored = run_suite(run, "restored", named(module, DECIDING + (POSITIVE,)))
    run.require("restored_candidate_passes", clean(restored, len(DECIDING) + 1)
                and set(restored["outcomes"].values()) == {"pass"}, restored)
    check_module_boundary(run, module, "restored", restored)
    run.require("product_file_unchanged_on_disk", sha_file(REPO / SOURCE) == on_disk, on_disk)
    run.require("offline_sentinels_untouched", module.SENTINEL_HITS == [], module.SENTINEL_HITS)


class UnrelatedConsumer(unittest.TestCase):
    """Runs after the FA1401 cases in the same suite, as an unrelated discovered test would."""
    expected = None
    module = None
    temp_root = None

    def test_sees_original_process_state(self):
        hits = list(self.module.SENTINEL_HITS)
        self.assertEqual(identity_diff(self.expected, guarded_identity()), [])
        self.assertEqual(self.module.guards_active(), [])
        self.assertEqual(load_boundary().active(), [])
        self.assertEqual(tempfile.tempdir, self.temp_root)
        self.assertEqual(self.module.SENTINEL_HITS, hits)


def case_isolation(run):
    identity(run)
    scanned = {name: sorted(token for token in HOST_TOKENS if token in (REPO / name).read_text())
               for name in (TEST, RUNNER, BOUNDARY)}
    run.require("no_host_paths_in_new_test_or_launcher", not any(scanned.values()), scanned)
    copy = run.directory / "second-prefix" / "fa1401-copy" / "repo"
    shutil.copytree(REPO, copy, symlinks=True, ignore=shutil.ignore_patterns(".git", "__pycache__"))
    copied = {name: sha_file(copy / name) for name in WRITE_SET}
    run.require("copy_bytes_match", copied == {name: sha_file(REPO / name) for name in WRITE_SET}, copied)
    child_root = run.directory / "copy-behavior"
    env = dict(os.environ)
    env["TMPDIR"] = str(run.directory)
    record, _, _ = run.command("copy-behavior", [sys.executable, "-B", copy / RUNNER, "--case", "behavior",
                                                 "--output-root", child_root], cwd=copy, env=env)
    run.require("copy_behavior_exit_zero", record["exit"] == 0, record["exit"])
    receipts = sorted(child_root.glob("run-*/receipt.json"))
    if run.require("copy_receipt_present", len(receipts) == 1, [str(p) for p in receipts]):
        child = json.loads(receipts[0].read_text())
        modules = child.get("loaded_modules") or {}
        stray = sorted(name for name, path in modules.items() if not Path(path).is_relative_to(copy.resolve()))
        run.receipt["copy_receipt"] = {"path": str(receipts[0]), "sha256": sha_file(receipts[0]),
                                       "result": child.get("result"), "repo": child.get("repo")}
        run.require("copy_behavior_passed", child.get("result") == "PASS_REVIEW_REQUIRED", child.get("result"))
        child_checks = {row.get("name"): row.get("ok") for row in child.get("checks") or []}
        boundary_checks = ("behavior_file_boundary_live_during_import", "behavior_file_boundary_live_for_every_case",
                           "behavior_offline_sentinels_untouched", "behavior_file_boundary_restored")
        run.require("copy_file_boundary_established_in_child",
                    all(child_checks.get(name) is True for name in boundary_checks),
                    {name: child_checks.get(name) for name in boundary_checks})
        run.require("copy_ran_from_second_prefix", child.get("repo") == str(copy.resolve()), child.get("repo"))
        run.require("copy_loaded_only_copy_modules", bool(modules) and not stray, stray)
        run.require("copy_product_module_is_copy",
                    modules.get("campaign_tool.records.ledger.receipt_projection") == str((copy / SOURCE).resolve()),
                    modules.get("campaign_tool.records.ledger.receipt_projection"))
    # In-process: FA1401 cases, then an unrelated peer receipt test and a state consumer.
    run.scratch()
    before = guarded_identity()
    module, _ = load_test_module(run)
    run.require("import_guard_restored_after_import", identity_diff(before, guarded_identity()) == [],
                identity_diff(before, guarded_identity()))
    UnrelatedConsumer.expected, UnrelatedConsumer.module = before, module
    UnrelatedConsumer.temp_root = tempfile.tempdir
    peer = COMPATIBILITY[2]
    suite = unittest.TestSuite([named(module, (DECIDING[-1], POSITIVE)),
                                unittest.TestLoader().loadTestsFromName(peer),
                                unittest.TestLoader().loadTestsFromTestCase(UnrelatedConsumer)])
    summary = run_suite(run, "isolation", suite)
    run.require("isolation_fa1401_then_unrelated_peers_pass", clean(summary, 4)
                and set(summary["outcomes"].values()) == {"pass"}, summary)
    guarded = list(module.GUARDED_RUNS)
    del module.GUARDED_RUNS[:]
    run.receipt.setdefault("boundary_guarded_tests", {})["isolation"] = guarded
    expected_guarded = sorted("test_" + name for name in (DECIDING[-1], POSITIVE))
    run.require("isolation_file_boundary_live_for_fa1401_cases_only",
                sorted(name.rsplit(".", 1)[-1] for name in guarded) == expected_guarded, guarded)
    run.require("isolation_offline_sentinels_untouched", module.SENTINEL_HITS == [], module.SENTINEL_HITS)
    run.require("guarded_callables_restored_after_suite", identity_diff(before, guarded_identity()) == [],
                identity_diff(before, guarded_identity()))
    run.require("no_guards_left_active", module.guards_active() == [] and load_boundary().active() == [],
                module.guards_active())
    check_module_closure(run)


def case_credential_scan(run):
    identity(run)
    runtime = staged_manifest().get("runtime") or {}
    python, scanner = runtime.get("credential_scan_python3"), runtime.get("detect_secrets")
    if not run.require("scan_runtime_declared", bool(python and scanner) and Path(python).is_file()
                       and Path(scanner).is_file(), {"python": python, "scanner": scanner}):
        return
    record, stdout, _ = run.command("detect-secrets", [python, scanner, "scan", "--all-files", "--exclude-files",
                                                       "^.git/", "--no-verify"], cwd=REPO)
    report = run.save("secret-scan.json", stdout)
    run.require("scan_exit_zero", record["exit"] == 0, record["exit"])
    try:
        results = json.loads(stdout)["results"]
        readable = isinstance(results, dict)
    except (ValueError, KeyError, TypeError):
        results, readable = {}, False
    run.require("scan_report_readable", readable)
    run.receipt["raw_hits_by_file"] = {name: len(rows) for name, rows in (results or {}).items()}
    run.require("no_raw_hits_in_write_set", not any(in_write_set(name) for name in (results or {})),
                sorted(name for name in (results or {}) if in_write_set(name)))
    record, out, _ = run.command("check-secret-report", [python, "-B", REPO / "tools" / "check_secret_report.py",
                                                         report], cwd=REPO)
    found = re.findall(r"potential secrets: (\d+)", out.decode(errors="replace"))
    run.receipt["potential_secrets"] = int(found[-1]) if found else None
    run.require("unchanged_checker_exit_zero", record["exit"] == 0, record["exit"])
    run.require("potential_secrets_zero", found == ["0"], found)


def case_public_tree(run):
    identity(run)
    python = REPO / ".venv" / "bin" / "python3"
    record, stdout, _ = run.command("public-tree", [python, "-B", REPO / "tools" / "check_public_tree.py"], cwd=REPO)
    run.require("public_tree_exit_zero", record["exit"] == 0, record["exit"])
    try:
        report = json.loads(stdout)
    except ValueError:
        report = None
    run.receipt["public_tree"] = report
    run.require("public_tree_no_violations", isinstance(report, dict) and report.get("findings") == []
                and report.get("non_content_matches") == [] and report.get("scanned_files", 0) > 0, report)


MODES = {"behavior": case_behavior, "compatibility": case_compatibility, "negative-control": case_negative_control,
         "isolation": case_isolation, "credential-scan": case_credential_scan, "public-tree": case_public_tree}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--case", required=True, choices=CASES)
    parser.add_argument("--output-root", required=True)
    args = parser.parse_args()
    run = Run(args.case, args.output_root)
    try:
        MODES[args.case](run)
    except Exception as caught:  # recorded as a failed check, never as success
        run.require("case_completed_without_exception", False, type(caught).__name__ + ": " + str(caught))
    return run.finish()


if __name__ == "__main__":
    sys.exit(main())
