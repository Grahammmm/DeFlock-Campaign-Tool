#!/usr/bin/env python3
"""FC7-REPORT-CLOSE acceptance runner: six independent --check modes.

Each invocation creates a fresh exclusive run directory under --output-root,
records argv, cwd, source identity, child argv/exit/stdout/stderr, selected test
IDs and counts, and exits 0 only when every check of its mode passed. Output is
REVIEW REQUIRED evidence, never semantic, legal, stage, live or release
acceptance. Child processes are limited to the literal git, unittest, scanner
and repository-checker argv arrays below; none uses a shell.
"""
import argparse
import ast
from datetime import datetime, timezone
import difflib
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.dont_write_bytecode = True
REPO = Path(__file__).resolve().parents[1]
MANIFEST = REPO.parent / "SOURCE-MANIFEST.json"
CHECKS = ("focused", "compatibility", "negative", "portability", "credentials", "public-tree")
SOURCE = "campaign_tool/records/gates/safe_output.py"
TEST = "tests/records/test_fc7_report_close.py"
RUNNER = "tools/accept_fc7_report_close.py"
ALLOWED_CHANGES = (SOURCE, TEST, RUNNER)
TEST_MODULE = "tests.records.test_fc7_report_close"
TEST_CLASS = "ReportCloseTest"
REQUIRED_CASES = ("successful_write_and_replace", "write_or_fsync_failure_with_successful_cleanup",
                  "unlink_failure_still_closes_directory", "close_failure_remains_failure",
                  "original_target_and_residue_evidence", "symlink_and_nonregular_target_refusal")
COMPATIBILITY_FILE = "tests/records/test_review_regressions.py"
COMPATIBILITY_CLASS = "PrivateReportTests"
COMPATIBILITY = "tests.records.test_review_regressions." + COMPATIBILITY_CLASS
# Static import closure of the focused in-process run (module file -> allowed imports).
CLOSURE = {
    TEST: {"os", "pathlib", "stat", "sys", "tempfile", "unittest", "unittest.mock",
           "campaign_tool.records.gates"},
    SOURCE: {"os", "pathlib", "secrets", "stat"},
    "campaign_tool/__init__.py": set(), "campaign_tool/records/__init__.py": set(),
    "campaign_tool/records/gates/__init__.py": set(), "tests/__init__.py": set(),
    "tests/records/__init__.py": set(),
}
NETWORK_MODULES = ("campaign_tool.records.portal.https_transport", "campaign_tool.digest.model",
                   "campaign_tool.outbox", "campaign_tool.meetings", "campaign_tool.discovery",
                   "campaign_tool.records.worker_lifecycle", "campaign_tool.records.ocr_stage",
                   "campaign_tool.records.intake.imap_intake")
# Built from pieces so this file does not match its own portability scan.
HOST_TOKENS = ("/" + "home/", "/" + "Users/", "agent" + "-work", "." + "hermes", "kan" + "ban",
               "/" + "tmp/", "/" + "workspace/", "/" + "private/var/")
ARTIFACT_ENV = "FC7_REPORT_CLOSE_ARTIFACT_ROOT"
COPY_IGNORE = shutil.ignore_patterns(".git", ".venv", "__pycache__", "node_modules")
CONTRACT = {"card": "FC7-REPORT-CLOSE", "task": "t_58006055", "checks": list(CHECKS),
            "write_set": list(ALLOWED_CHANGES), "frozen_cases": list(REQUIRED_CASES),
            "compatibility_selector": COMPATIBILITY,
            "negative": "base sequential unlink-then-close finalizer restored in memory only",
            "status": "REVIEW_REQUIRED_NOT_ACCEPTANCE"}
TIMEOUT = 1800


def sha_bytes(raw):
    return hashlib.sha256(raw).hexdigest()


def sha_file(path):
    return sha_bytes(Path(path).read_bytes())


def now():
    return datetime.now(timezone.utc).isoformat()


def canonical(data):
    return json.dumps(data, indent=2, sort_keys=True, default=str) + "\n"


class Run:
    def __init__(self, check, output_root):
        self.check = check
        output_root = Path(os.path.abspath(output_root))
        if output_root == REPO or REPO in output_root.parents:
            raise SystemExit("output root must be outside the repository")
        output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        self.directory = output_root / f"run-{stamp}-{os.getpid()}"
        self.directory.mkdir(mode=0o700)  # exclusive: fails if it already exists
        self.checks, self.files = [], {}
        self.receipt = {"schema": "fc7-report-close-acceptance-v1", "check": check,
                        "contract": CONTRACT, "contract_sha256": sha_bytes(canonical(CONTRACT).encode()),
                        "runner_sha256": sha_file(__file__), "argv": list(sys.argv), "cwd": os.getcwd(),
                        "executable": sys.executable, "python": sys.version, "repo": str(REPO),
                        "started_at": now(), "run_directory": str(self.directory),
                        "status": "REVIEW_REQUIRED_NOT_ACCEPTANCE"}

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
        """Run one literal argv without a shell; save raw stdout/stderr."""
        started = now()
        try:
            done = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, timeout=TIMEOUT,
                                  shell=False, check=False)
            code, stdout, stderr, error = done.returncode, done.stdout, done.stderr, None
        except (OSError, subprocess.TimeoutExpired) as caught:
            code, stdout, stderr, error = None, b"", b"", type(caught).__name__ + ": " + str(caught)
        record = {"label": label, "argv": [str(part) for part in argv], "cwd": str(cwd),
                  "started_at": started, "ended_at": now(), "exit": code, "error": error,
                  "stdout": self.save(label + ".stdout", stdout).name,
                  "stderr": self.save(label + ".stderr", stderr).name}
        self.receipt.setdefault("commands", []).append(record)
        return record, stdout, stderr

    def finish(self):
        passed = bool(self.checks) and all(row["ok"] for row in self.checks)
        self.receipt.update({"checks": self.checks, "files": self.files, "ended_at": now(),
                             "result": "PASS_REVIEW_REQUIRED" if passed else "FAIL"})
        raw = canonical(self.receipt)
        with open(self.directory / "receipt.json", "x", encoding="utf-8") as stream:
            stream.write(raw)
        print(json.dumps({"check": self.check, "result": self.receipt["result"],
                          "run_directory": str(self.directory), "receipt_sha256": sha_bytes(raw.encode()),
                          "checks": [(row["name"], row["ok"]) for row in self.checks]}, indent=2))
        return 0 if passed else 1


def load_manifest(run):
    """Resolve SOURCE-MANIFEST.json beside the repo and verify its declarations."""
    if not run.require("source_manifest_present", MANIFEST.is_file(), str(MANIFEST)):
        return None
    manifest = json.loads(MANIFEST.read_text())
    run.receipt["source_manifest"] = {"path": str(MANIFEST), "sha256": sha_file(MANIFEST),
                                      "head": manifest.get("head"), "remote": manifest.get("remote"),
                                      "repo": manifest.get("repo"),
                                      "derived_from": manifest.get("derived_from")}
    run.require("manifest_base_is_full_sha", bool(re.fullmatch(r"[0-9a-f]{40}", manifest.get("head") or "")),
                manifest.get("head"))
    run.require("manifest_repo_is_this_repo", os.path.abspath(manifest.get("repo") or "") == str(REPO),
                {"declared": manifest.get("repo"), "actual": str(REPO)})
    run.require("manifest_remote_declared", bool(manifest.get("remote")), manifest.get("remote"))
    runtime = manifest.get("runtime") or {}
    run.require("manifest_runtime_declared", bool(runtime.get("credential_scan_python3"))
                and bool(runtime.get("detect_secrets")), runtime)
    return manifest


def git(run, label, *arguments):
    record, stdout, _ = run.command("git-" + label, ["git", *arguments], cwd=REPO)
    return record["exit"], stdout.decode(errors="replace")


def identity(run):
    manifest = load_manifest(run)
    hashes = {name: sha_file(REPO / name) if (REPO / name).is_file() else None for name in ALLOWED_CHANGES}
    record = {"repo": str(REPO), "file_sha256": hashes, "base": manifest and manifest.get("head")}
    run.receipt["identity"] = record
    run.require("write_set_files_present", all(hashes.values()), hashes)
    if manifest is None:
        return record
    if not (REPO / ".git").exists():
        # Only a portability copy (manifest rewritten with derived_from) may lack git.
        record["git_available"] = False
        run.require("git_absent_only_in_derived_copy", bool(manifest.get("derived_from")),
                    manifest.get("derived_from"))
        return record
    base = manifest["head"]
    code, head = git(run, "rev-parse-head", "rev-parse", "HEAD")
    record["head"] = head.strip() if code == 0 else None
    run.require("candidate_head_known", bool(record["head"]), record["head"])
    code, _ = git(run, "merge-base-is-ancestor", "merge-base", "--is-ancestor", base, "HEAD")
    run.require("base_is_ancestor_of_head", code == 0, code)
    code, log = git(run, "log-base-head", "log", "--format=%H %s", base + "..HEAD")
    record["candidate_commits"] = log.splitlines() if code == 0 else None
    code, names = git(run, "diff-names-base-head", "diff", "--name-only", base, "HEAD")
    changed = set(names.split()) if code == 0 else None
    code, status = git(run, "status-porcelain", "status", "--porcelain", "--untracked-files=all")
    lines = status.splitlines() if code == 0 else None
    # The staged shared interpreter link is operator staging, not candidate material.
    dirty = {line[3:] for line in lines or () if line and line != "?? .venv"}
    record.update({"committed_changes_from_base": sorted(changed or ()), "status_porcelain": lines,
                   "dirty_paths": sorted(dirty)})
    run.require("diff_and_status_readable", changed is not None and lines is not None)
    outside = sorted(((changed or set()) | dirty) - set(ALLOWED_CHANGES))
    run.require("changes_only_in_write_set", not outside, outside)
    run.require("working_tree_clean", not dirty, sorted(dirty))
    code, base_source = git(run, "show-base-source", "show", base + ":" + SOURCE)
    record["base_source_sha256"] = sha_bytes(base_source.encode()) if code == 0 else None
    return record


def module_imports(path):
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add("." * node.level + (node.module or ""))
    return names


def inspect_closure(run, repo=REPO):
    """Static import inspection of every file the focused run executes, before execution."""
    found = {name: sorted(module_imports(repo / name)) for name in CLOSURE}
    extra = {name: sorted(set(found[name]) - CLOSURE[name]) for name in CLOSURE}
    run.receipt["static_closure"] = {"imports": found, "unexpected": extra}
    run.require("static_import_closure_admitted", not any(extra.values()), extra)


class Sentinels:
    """Network, child-process and database-path refusal while the in-process tests run."""

    def __init__(self, allowed_root):
        self.allowed_root, self.hits, self.patches = Path(allowed_root), [], []

    def refuse(self, name):
        def blocked(*args, **kwargs):
            self.hits.append({"call": name, "args": [repr(arg)[:120] for arg in args]})
            raise RuntimeError("FC7 sentinel refused " + name)
        return blocked

    def install(self):
        import http.client
        import socket
        import sqlite3
        import urllib.request
        real_connect = sqlite3.connect

        def database(target, *args, **kwargs):
            text = str(target)
            if text != ":memory:" and not Path(os.path.abspath(text)).is_relative_to(self.allowed_root):
                self.hits.append({"call": "sqlite3.connect", "args": [text]})
                raise RuntimeError("FC7 sentinel refused database path")
            return real_connect(target, *args, **kwargs)

        targets = ((socket, "create_connection"), (socket, "getaddrinfo"), (socket.socket, "connect"),
                   (socket.socket, "connect_ex"), (http.client.HTTPConnection, "connect"),
                   (urllib.request, "urlopen"), (subprocess, "Popen"), (os, "system"))
        for owner, attribute in targets:
            label = getattr(owner, "__name__", str(owner)) + "." + attribute
            self.patches.append(mock.patch.object(owner, attribute, self.refuse(label)))
        self.patches.append(mock.patch.object(sqlite3, "connect", database))
        for patch in self.patches:
            patch.start()

    def remove(self):
        for patch in reversed(self.patches):
            patch.stop()
        self.patches = []


def open_descriptors():
    try:
        return len(os.listdir("/dev/fd"))
    except OSError:
        return None


def prepare_in_process(run):
    """Fresh temporary defaults and sentinels, then import the test module."""
    preloaded = sorted(name for name in sys.modules if name.split(".")[0] in ("campaign_tool", "tests"))
    run.require("no_application_import_before_sentinels", not preloaded, preloaded)
    scratch = run.directory / "tmp"
    scratch.mkdir(mode=0o700)
    os.environ["TMPDIR"] = str(scratch)
    os.environ[ARTIFACT_ENV] = str(scratch)
    tempfile.tempdir = str(scratch)
    inspect_closure(run)
    sentinels = Sentinels(run.directory)
    sentinels.install()
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    module = importlib.import_module(TEST_MODULE)
    run.require("test_module_from_this_repo", Path(module.__file__).resolve() == REPO / TEST, module.__file__)
    run.require("sentinels_before_application_import", module.APPLICATION_PRELOADED == [],
                module.APPLICATION_PRELOADED)
    run.require("fixture_from_this_repo", module.FIXTURE == REPO / "tests" / "fixtures" / "review-cases.json",
                str(module.FIXTURE))
    return module, sentinels, scratch


class RecordingResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.outcomes = {}

    def _mark(self, test, outcome):
        self.outcomes[test.id()] = outcome

    def addSuccess(self, test):
        super().addSuccess(test)
        self.outcomes.setdefault(test.id(), "pass")

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


def run_cases(run, label, module, names):
    loader = unittest.TestLoader()
    suite = unittest.TestSuite(loader.loadTestsFromName(TEST_CLASS + ".test_" + name, module) for name in names)
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=2, resultclass=RecordingResult).run(suite)
    run.save(label + ".unittest.txt", stream.getvalue())
    outcomes = {key.rsplit(".", 1)[-1][5:]: value for key, value in result.outcomes.items()}
    summary = {"tests_run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
               "skipped": len(result.skipped), "expected_failures": len(result.expectedFailures),
               "unexpected_successes": len(result.unexpectedSuccesses), "successful": result.wasSuccessful(),
               "test_ids": sorted(result.outcomes), "outcomes": outcomes}
    run.receipt.setdefault("unittest", {})[label] = summary
    return summary


def loaded_modules():
    return {name: str(Path(module.__file__).resolve()) for name, module in sorted(sys.modules.items())
            if name.split(".")[0] in ("campaign_tool", "tests") and getattr(module, "__file__", None)}


def all_pass(summary, count):
    return (summary["successful"] and summary["tests_run"] == count and summary["failures"] == 0
            and summary["errors"] == 0 and summary["skipped"] == 0 and summary["expected_failures"] == 0
            and summary["unexpected_successes"] == 0)


def check_focused(run):
    identity(run)
    descriptors = open_descriptors()
    module, sentinels, scratch = prepare_in_process(run)
    try:
        summary = run_cases(run, "focused", module, REQUIRED_CASES)
    finally:
        sentinels.remove()
    declared = sorted(name[5:] for name in dir(getattr(module, TEST_CLASS)) if name.startswith("test_"))
    run.require("frozen_case_set_exact", declared == sorted(REQUIRED_CASES), declared)
    run.require("all_named_cases_ran", summary["tests_run"] == len(REQUIRED_CASES), summary["tests_run"])
    run.require("all_named_cases_pass", summary["outcomes"] == {name: "pass" for name in REQUIRED_CASES},
                summary["outcomes"])
    run.require("no_failures_errors_or_skips", all_pass(summary, len(REQUIRED_CASES)), summary)
    observations = dict(module.OBSERVATIONS)
    run.receipt["descriptor_observations"] = observations
    run.require("descriptor_evidence_for_every_case", sorted(observations) == sorted(REQUIRED_CASES),
                sorted(observations))
    unclosed = {case: [row.get("leaked", row.get("unclosed_directories")) for row in rows]
                for case, rows in observations.items()}
    run.require("every_acquired_directory_closed",
                all(value == [] for rows in unclosed.values() for value in rows), unclosed)
    after = open_descriptors()
    run.receipt["process_descriptors"] = {"before": descriptors, "after": after}
    run.require("no_process_descriptor_growth", descriptors is None or after <= descriptors,
                {"before": descriptors, "after": after})
    run.require("offline_sentinels_untouched", sentinels.hits == [], sentinels.hits)
    modules = loaded_modules()
    run.receipt["loaded_modules"] = modules
    outside = sorted(name for name, path in modules.items() if not Path(path).is_relative_to(REPO))
    run.require("application_modules_from_this_repo", bool(modules) and not outside, outside)
    network = sorted(name for name in NETWORK_MODULES if name in sys.modules)
    run.require("network_capable_application_modules_not_loaded", not network, network)
    leftovers = sorted(str(path) for path in scratch.iterdir())
    run.require("temporary_report_roots_released", not leftovers, leftovers)


def base_write_private(run, safe_output, base_source):
    """The base finalizer, compiled in memory and bound to the live module globals."""
    current = (REPO / SOURCE).read_text(encoding="utf-8").splitlines()
    old = base_source.splitlines()
    finally_line = old.index("    finally:") if "    finally:" in old else None
    changed = [(tag, i1, i2, j1, j2) for tag, i1, i2, j1, j2 in
               difflib.SequenceMatcher(a=old, b=current, autojunk=False).get_opcodes() if tag != "equal"]
    run.receipt["base_to_candidate_opcodes"] = changed
    run.require("base_differs_only_in_finalizer", finally_line is not None and bool(changed)
                and all(i1 > finally_line for _, i1, _, _, _ in changed), changed)
    run.require("base_finalizer_is_sequential_unlink_then_close",
                finally_line is not None and [line.strip() for line in old[finally_line + 1:]] ==
                ["if temporary is not None:", "os.unlink(temporary, dir_fd=directory)", "os.close(directory)"],
                old[finally_line:] if finally_line is not None else None)
    namespace = {}
    exec(compile(base_source, "base:" + SOURCE, "exec"), namespace)
    old_function = namespace["write_private"]
    mutant = types.FunctionType(old_function.__code__, safe_output.__dict__, "write_private",
                                old_function.__defaults__, old_function.__closure__)
    mutant.__kwdefaults__ = old_function.__kwdefaults__
    return mutant


def check_negative(run):
    record = identity(run)
    if not run.require("base_known", bool(record.get("base")) and (REPO / ".git").exists(), record.get("base")):
        return
    on_disk = sha_file(REPO / SOURCE)
    # Read the base bytes before the sentinels refuse child processes.
    code, base_source = git(run, "show-base-source-negative", "show", record["base"] + ":" + SOURCE)
    if not run.require("base_source_read", code == 0 and bool(base_source), code):
        return
    module, sentinels, _ = prepare_in_process(run)
    try:
        safe_output = importlib.import_module("campaign_tool.records.gates.safe_output")
        original = safe_output.write_private
        mutant = base_write_private(run, safe_output, base_source)
        if mutant is None:
            return
        module.OBSERVATIONS.clear()
        with mock.patch.object(safe_output, "write_private", mutant):
            broken = run_cases(run, "mutant", module, ("unlink_failure_still_closes_directory",))
        mutant_rows = module.OBSERVATIONS.get("unlink_failure_still_closes_directory", [])
        run.receipt["mutant_observations"] = mutant_rows
        run.require("mutant_detected_as_assertion_failure",
                    broken["outcomes"] == {"unlink_failure_still_closes_directory": "fail"}
                    and broken["errors"] == 0 and broken["failures"] >= 1, broken)
        run.require("mutant_still_raises_cleanup_failure", bool(mutant_rows) and all(
            row["raised"] == repr(OSError("synthetic cleanup failure")) for row in mutant_rows), mutant_rows)
        run.require("mutant_leaves_owned_directory_unclosed", bool(mutant_rows) and all(
            row["unclosed_directories"] == [1002] for row in mutant_rows), mutant_rows)
        run.require("finalizer_restored", safe_output.write_private is original)
        module.OBSERVATIONS.clear()
        restored = run_cases(run, "restored", module, ("write_or_fsync_failure_with_successful_cleanup",
                                                       "unlink_failure_still_closes_directory"))
        run.receipt["restored_observations"] = dict(module.OBSERVATIONS)
        run.require("restored_cleanup_control_and_case_pass", all_pass(restored, 2), restored)
    finally:
        sentinels.remove()
    run.require("production_file_unchanged_on_disk", sha_file(REPO / SOURCE) == on_disk, on_disk)
    run.require("offline_sentinels_untouched", sentinels.hits == [], sentinels.hits)


VERBOSE_LINE = re.compile(r"^(test_\w+) \(([\w.]+)\)(?: \(.*\))? \.\.\. (.*)$", re.M)


def parse_unittest(stderr):
    text = stderr.decode(errors="replace")
    ran = re.findall(r"^Ran (\d+) tests? in ", text, re.M)
    final = re.findall(r"^(OK|FAILED)(?: \(([^)]*)\))?\s*$", text, re.M)
    rows = {}
    for name, dotted, outcome in VERBOSE_LINE.findall(text):
        test_id = dotted if dotted.endswith("." + name) else dotted + "." + name
        rows[test_id] = outcome.strip()
    return {"tests_run": int(ran[-1]) if ran else 0, "final": final[-1][0] if final else None,
            "detail": final[-1][1] if final else None, "test_ids": sorted(rows), "outcomes": rows}


def declared_tests(path, class_name):
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return sorted(item.name for item in node.body
                          if isinstance(item, ast.FunctionDef) and item.name.startswith("test_"))
    return []


def compatibility_closure(run):
    """Static inspection of the compatibility selector's imports and child calls."""
    source = (REPO / COMPATIBILITY_FILE).read_text(encoding="utf-8")
    tree = ast.parse(source)
    children = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and \
                isinstance(node.func.value, ast.Name) and node.func.value.id == "subprocess":
            children.append({"line": node.lineno, "call": "subprocess." + node.func.attr,
                             "source": ast.get_source_segment(source, node)[:200]})
    run.receipt["compatibility_closure"] = {"imports": sorted(module_imports(REPO / COMPATIBILITY_FILE)),
                                            "child_calls": children}
    run.require("compatibility_child_calls_are_local_module_cli",
                all("campaign_tool.records" in row["source"] and "sys.executable" in row["source"]
                    for row in children), children)


def compatibility_run(run, label, repo):
    scratch = run.directory / (label + "-tmp")
    scratch.mkdir(mode=0o700)
    environment = dict(os.environ)
    environment["TMPDIR"] = str(scratch)
    record, _, stderr = run.command(label, [sys.executable, "-B", "-m", "unittest", "-v", COMPATIBILITY],
                                    cwd=repo, env=environment)
    parsed = parse_unittest(stderr)
    parsed.update({"exit": record["exit"], "repo": str(repo), "safe_output_sha256": sha_file(repo / SOURCE)})
    return parsed


def check_compatibility(run):
    record = identity(run)
    if not run.require("base_known", bool(record.get("base")) and (REPO / ".git").exists(), record.get("base")):
        return
    compatibility_closure(run)
    expected = sorted(COMPATIBILITY + "." + name for name in
                      declared_tests(REPO / COMPATIBILITY_FILE, COMPATIBILITY_CLASS))
    run.require("selector_declares_tests", bool(expected), expected)
    code, base_source = git(run, "show-base-source-compat", "show", record["base"] + ":" + SOURCE)
    if not run.require("base_source_read", code == 0 and bool(base_source), code):
        return
    copy = run.directory / "base-copy" / "repo"
    shutil.copytree(REPO, copy, symlinks=True, ignore=COPY_IGNORE)
    (copy / SOURCE).write_bytes(base_source.encode())
    before = compatibility_run(run, "compatibility-before", copy)
    after = compatibility_run(run, "compatibility-after", REPO)
    run.receipt["compatibility"] = {"selector": COMPATIBILITY, "expected_ids": expected,
                                    "before": before, "after": after}
    run.require("before_ran_base_source", before["safe_output_sha256"] == sha_bytes(base_source.encode()),
                before["safe_output_sha256"])
    run.require("after_ran_candidate_source", after["safe_output_sha256"] == sha_file(REPO / SOURCE),
                after["safe_output_sha256"])
    for side, result in (("before", before), ("after", after)):
        run.require(side + "_exit_zero", result["exit"] == 0, result["exit"])
        run.require(side + "_selected_exact_declared_tests", result["test_ids"] == expected
                    and result["tests_run"] == len(expected), {"ids": result["test_ids"],
                                                               "ran": result["tests_run"]})
        run.require(side + "_ok_without_skips", result["final"] == "OK" and not result["detail"]
                    and all(value == "ok" for value in result["outcomes"].values()), result["outcomes"])
    run.receipt["full_discovery"] = "operator-run; scoped selector success is not full-suite success"


def check_portability(run):
    identity(run)
    scanned = {name: sorted(token for token in HOST_TOKENS if token in (REPO / name).read_text())
               for name in (TEST, RUNNER)}
    run.require("no_host_paths_in_new_test_or_runner", not any(scanned.values()), scanned)
    if not run.require("source_manifest_readable", MANIFEST.is_file()):
        return
    parent = run.directory / "second-prefix" / "fc7-alternate-root"
    copy = parent / "repo"
    shutil.copytree(REPO, copy, symlinks=True, ignore=COPY_IGNORE)
    manifest = json.loads(MANIFEST.read_text())
    manifest.update({"repo": str(copy), "derived_from": {"path": str(MANIFEST), "sha256": sha_file(MANIFEST)}})
    (parent / "SOURCE-MANIFEST.json").write_text(canonical(manifest))
    copied = {name: sha_file(copy / name) for name in ALLOWED_CHANGES}
    run.receipt["copy"] = {"path": str(copy), "file_sha256": copied}
    run.require("copy_bytes_match", copied == {name: sha_file(REPO / name) for name in ALLOWED_CHANGES}, copied)
    receipts = {}
    for label, repo in (("first-prefix-focused", REPO), ("second-prefix-focused", copy)):
        root = run.directory / label
        record, _, _ = run.command(label, [sys.executable, "-B", str(repo / RUNNER), "--check", "focused",
                                           "--output-root", str(root)], cwd=repo)
        run.require(label + "_exit_zero", record["exit"] == 0, record["exit"])
        found = sorted(root.glob("run-*/receipt.json"))
        if not run.require(label + "_receipt_present", len(found) == 1, [str(path) for path in found]):
            return
        child = json.loads(found[0].read_text())
        receipts[label] = child
        modules = child.get("loaded_modules") or {}
        stray = sorted(name for name, path in modules.items() if not Path(path).is_relative_to(repo.resolve()))
        run.receipt.setdefault("child_receipts", {})[label] = {
            "path": str(found[0]), "sha256": sha_file(found[0]), "result": child.get("result"),
            "repo": child.get("repo"), "loaded_modules": modules}
        run.require(label + "_passed", child.get("result") == "PASS_REVIEW_REQUIRED", child.get("result"))
        run.require(label + "_ran_from_its_prefix", child.get("repo") == str(repo.resolve()), child.get("repo"))
        run.require(label + "_loaded_only_its_modules", bool(modules) and not stray, stray)
    first, second = (receipts[label]["unittest"]["focused"]["outcomes"] for label in receipts)
    run.require("same_case_outcomes_across_prefixes", first == second, {"first": first, "second": second})


def check_credentials(run):
    identity(run)
    manifest = json.loads(MANIFEST.read_text()) if MANIFEST.is_file() else {}
    runtime = manifest.get("runtime") or {}
    run.require("interpreter_is_declared_credential_scan_python3",
                os.path.abspath(sys.executable) == runtime.get("credential_scan_python3"),
                {"actual": sys.executable, "declared": runtime.get("credential_scan_python3")})
    scanner = runtime.get("detect_secrets")
    if not run.require("declared_detect_secrets_installed", bool(scanner) and Path(scanner).is_file(), scanner):
        return
    record, stdout, _ = run.command("detect-secrets", [scanner, "scan", "--all-files", "--exclude-files",
                                                       "^.git/", "--no-verify"], cwd=REPO)
    report = run.save("secret-scan.json", stdout)
    run.require("scan_exit_zero", record["exit"] == 0, record["exit"])
    try:
        results = json.loads(stdout)["results"]
        readable = isinstance(results, dict)
    except (ValueError, KeyError, TypeError):
        results, readable = {}, False
    run.require("scan_report_readable", readable)
    hits = {name: len(rows) for name, rows in (results or {}).items()}
    run.receipt["raw_hits_by_file"] = hits
    run.require("no_raw_hits_in_fc7_files", not any(name in hits for name in ALLOWED_CHANGES),
                {name: hits.get(name) for name in ALLOWED_CHANGES})
    record, _, _ = run.command("check-secret-report", [sys.executable, "-B",
                                                       str(REPO / "tools" / "check_secret_report.py"),
                                                       str(report)], cwd=REPO)
    run.require("unchanged_checker_exit_zero", record["exit"] == 0, record["exit"])


def check_public_tree(run):
    identity(run)
    record, _, _ = run.command("public-tree", [sys.executable, "-B", str(REPO / "tools" / "check_public_tree.py")],
                               cwd=REPO)
    run.require("public_tree_exit_zero", record["exit"] == 0, record["exit"])


MODES = {"focused": check_focused, "compatibility": check_compatibility, "negative": check_negative,
         "portability": check_portability, "credentials": check_credentials, "public-tree": check_public_tree}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", required=True, choices=CHECKS)
    parser.add_argument("--output-root", required=True)
    args = parser.parse_args()
    run = Run(args.check, args.output_root)
    try:
        MODES[args.check](run)
    except Exception as caught:  # recorded as a failed check, never as success
        run.require("mode_completed_without_exception", False, type(caught).__name__ + ": " + str(caught))
    return run.finish()


if __name__ == "__main__":
    sys.exit(main())
