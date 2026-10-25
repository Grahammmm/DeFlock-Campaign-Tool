#!/usr/bin/env python3
"""FC4-SOURCE-ORDINAL acceptance runner: six independent --check modes.

Each invocation creates a fresh exclusive run directory under --output-root,
records argv, cwd, identity, exits, counts, raw output and hashes, and exits 0
only when every check in its mode passed. Output is REVIEW REQUIRED evidence,
never semantic, legal, stage or release acceptance. Subprocesses are limited to
the enumerated local git, unittest and scanner argv arrays below; none uses a
shell.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import http.client  # noqa: F401  (guarded owners are read from these stdlib modules)
import importlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import socket  # noqa: F401
import subprocess
import sys
import unittest
from unittest import mock
import urllib.request  # noqa: F401

sys.dont_write_bytecode = True
REPO = Path(__file__).resolve().parents[1]
BRIEF = {"card": "FC4-SOURCE-ORDINAL", "task": "t_8f990174",
         "title": "Reject boolean and floating provenance ordinals in detector source resolution.",
         "pinned_base_ref": "main at the admitted FC4 freeze (see SOURCE-MANIFEST head)"}
CHECKS = ("focused", "compatibility", "negative", "portability", "credentials", "public-tree")
SOURCE = "campaign_tool/records/detectors/sources.py"
TEST = "tests/records/test_fc4_source_ordinal.py"
RUNNER = "tools/accept_fc4_source_ordinal.py"
ALLOWED_CHANGES = (SOURCE, TEST, RUNNER)
TEST_MODULE = "tests.records.test_fc4_source_ordinal"
VALID_CONTROL = TEST_MODULE + ".SourceOrdinalTest.test_valid_integer_first_and_later_positions"
REQUIRED_CASES = ("valid_integer_first_and_later_positions", "bool_not_integer", "float_not_integer",
                  "missing_null_string_container", "zero_negative_wrong_position",
                  "otherwise_valid_binding_isolation", "whole_snapshot_refusal",
                  "valid_revision_unchanged", "read_only_database_and_file_preservation")
COMPATIBILITY = ("tests.records.test_detector_preparation", "tests.test_understanding_rows",
                 "tests.test_secret_report", "tests.test_public_tree")
NETWORK_MODULES = ("campaign_tool.records.portal.https_transport", "campaign_tool.digest.model",
                   "campaign_tool.outbox", "campaign_tool.meetings", "campaign_tool.discovery",
                   "campaign_tool.records.worker_lifecycle", "campaign_tool.records.ocr_stage",
                   "campaign_tool.records.intake.imap_intake")
# Built from pieces so this file does not match its own portability scan.
HOST_TOKENS = ("/" + "home/", "/" + "Users/", "agent" + "-work", "." + "hermes", "kan" + "ban",
               "/" + "tmp/", "/" + "workspace/")
ARTIFACT_ENV = "FC4_SOURCE_ORDINAL_ARTIFACT_ROOT"
TIMEOUT = 1800


def sha_bytes(raw):
    return hashlib.sha256(raw).hexdigest()


def sha_file(path):
    return sha_bytes(Path(path).read_bytes())


def now():
    return datetime.now(timezone.utc).isoformat()


class Run:
    def __init__(self, check, output_root):
        self.check = check
        output_root = Path(os.path.abspath(output_root))
        if output_root == REPO or REPO in output_root.parents:
            raise SystemExit("output root must be outside the repository")
        output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        self.directory = output_root / f"run-{stamp}-{os.getpid()}"
        self.directory.mkdir(mode=0o700)
        self.checks = []
        self.files = {}
        self.receipt = {"schema": "fc4-source-ordinal-acceptance-v1", "brief": BRIEF, "check": check,
                        "argv": list(sys.argv), "cwd": os.getcwd(), "executable": sys.executable,
                        "python": sys.version, "repo": str(REPO), "started_at": now(),
                        "run_directory": str(self.directory),
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
        """Run one enumerated literal argv without a shell; save raw stdout/stderr."""
        started = now()
        try:
            completed = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, timeout=TIMEOUT,
                                       shell=False, check=False)
            exit_code, stdout, stderr, error = completed.returncode, completed.stdout, completed.stderr, None
        except (OSError, subprocess.TimeoutExpired) as caught:
            exit_code, stdout, stderr, error = None, b"", b"", type(caught).__name__ + ": " + str(caught)
        record = {"label": label, "argv": [str(part) for part in argv], "cwd": str(cwd),
                  "started_at": started, "ended_at": now(), "exit": exit_code, "error": error,
                  "stdout": self.save(label + ".stdout", stdout).name,
                  "stderr": self.save(label + ".stderr", stderr).name}
        self.receipt.setdefault("commands", []).append(record)
        return record, stdout, stderr

    def finish(self):
        passed = bool(self.checks) and all(row["ok"] for row in self.checks)
        self.receipt.update({"checks": self.checks, "files": self.files, "ended_at": now(),
                             "result": "PASS_REVIEW_REQUIRED" if passed else "FAIL"})
        raw = json.dumps(self.receipt, indent=2, sort_keys=True, default=str) + "\n"
        with open(self.directory / "receipt.json", "x", encoding="utf-8") as stream:
            stream.write(raw)
        print(json.dumps({"check": self.check, "result": self.receipt["result"],
                          "run_directory": str(self.directory),
                          "receipt_sha256": sha_bytes(raw.encode()),
                          "checks": [(row["name"], row["ok"]) for row in self.checks]}, indent=2))
        return 0 if passed else 1


def git(run, *arguments):
    """Raw decoded stdout of one literal git argv, or None when unavailable."""
    if not (REPO / ".git").exists():
        return None
    record, stdout, _ = run.command("git-" + "-".join(arguments).replace("/", "_").replace("^", "parent"),
                                    ["git", *arguments], cwd=REPO)
    return stdout.decode() if record["exit"] == 0 else None


def identity(run):
    hashes = {name: sha_file(REPO / name) if (REPO / name).is_file() else None for name in ALLOWED_CHANGES}
    head = (git(run, "rev-parse", "HEAD") or "").strip() or None
    status = git(run, "status", "--porcelain", "--untracked-files=all")
    record = {"repo": str(REPO), "git_available": head is not None, "head": head,
              "status_porcelain": status, "file_sha256": hashes}
    if head is not None:
        # The candidate commits are those since the first commit that added the
        # new FC4 test/runner files; the base is that first commit's parent. If
        # no commit has added them yet, HEAD itself is the base.
        added = (git(run, "rev-list", "HEAD", "--", TEST, RUNNER) or "").split()
        record["candidate_commits"] = added
        record["base"] = ((git(run, "rev-parse", added[-1] + "^") or "").strip() or None
                          if added else head)
        changed = set((git(run, "diff", "--name-only", record["base"], "HEAD") or "").split())
        record["committed_changes_from_base"] = sorted(changed)
        dirty = {line[3:] for line in (status or "").splitlines() if line}
        record["paths_outside_write_set"] = sorted((changed | dirty) - set(ALLOWED_CHANGES))
        run.require("identity_write_set", not record["paths_outside_write_set"],
                    record["paths_outside_write_set"])
    run.receipt["identity"] = record
    return record


class RecordingResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.outcomes = {}

    def _mark(self, test, outcome):
        self.outcomes[test.id().rsplit(".", 1)[-1]] = outcome

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
            failed = issubclass(err[0], test.failureException)
            self._mark(test, "fail" if failed else "error")


def load_test_module(run):
    preloaded = sorted(name for name in sys.modules if name.split(".")[0] == "campaign_tool")
    run.require("no_application_import_before_sentinels", not preloaded, preloaded)
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    module = importlib.import_module(TEST_MODULE)
    run.require("test_module_from_this_repo", Path(module.__file__).resolve() == REPO / TEST,
                module.__file__)
    run.require("sentinels_installed_before_application_import", module.APPLICATION_PRELOADED == [],
                module.APPLICATION_PRELOADED)
    return module


def run_cases(run, label, module, names):
    loader = unittest.TestLoader()
    suite = unittest.TestSuite(loader.loadTestsFromName("SourceOrdinalTest.test_" + name, module)
                               for name in names)
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=2, resultclass=RecordingResult).run(suite)
    run.save(label + ".unittest.txt", stream.getvalue())
    summary = {"tests_run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
               "skipped": len(result.skipped), "expected_failures": len(result.expectedFailures),
               "unexpected_successes": len(result.unexpectedSuccesses),
               "successful": result.wasSuccessful(), "outcomes": result.outcomes}
    run.receipt.setdefault("unittest", {})[label] = summary
    return summary


def loaded_application_modules():
    return {name: str(Path(module.__file__).resolve()) for name, module in sorted(sys.modules.items())
            if name.split(".")[0] in ("campaign_tool", "tests") and getattr(module, "__file__", None)}


# Every process-global callable the test module may replace, read the same way
# an unrelated consumer would see it. Captured before the test module is imported.
GUARDED = (("socket", "create_connection"), ("socket", "getaddrinfo"), ("socket.socket", "connect"),
           ("socket.socket", "connect_ex"), ("http.client.HTTPConnection", "connect"),
           ("urllib.request", "urlopen"), ("subprocess", "Popen"))


def _owner(dotted):
    head, _, tail = dotted.partition(".")
    owner = sys.modules[head]
    for part in tail.split(".") if tail else ():
        owner = getattr(owner, part)
    return owner


def guarded_identity():
    """(own-dict value or None, resolved callable) per guarded name."""
    state = {}
    for dotted, attribute in GUARDED:
        owner = _owner(dotted)
        state[dotted + "." + attribute] = (vars(owner).get(attribute), getattr(owner, attribute))
    return state


def identity_diff(before, after):
    return sorted(name for name in before if before[name][0] is not after[name][0]
                  or before[name][1] is not after[name][1])


class UnrelatedConsumer(unittest.TestCase):
    """Runs after the FC4 module in one suite, as an unrelated discovered test would."""
    expected = None
    module = None

    def test_sees_original_callables_and_no_new_hits(self):
        hits = list(self.module.SENTINEL_HITS)
        self.assertEqual(identity_diff(self.expected, guarded_identity()), [])
        self.assertEqual(self.module.guards_active(), [])
        self.assertEqual(self.module.SENTINEL_HITS, hits)


def check_isolation(run, module, before):
    """Inert import/execution/teardown regression: identity checks only, no I/O."""
    run.require("import_guard_restored_after_import", identity_diff(before, guarded_identity()) == [],
                identity_diff(before, guarded_identity()))
    UnrelatedConsumer.expected, UnrelatedConsumer.module = before, module
    loader = unittest.TestLoader()
    suite = unittest.TestSuite([loader.loadTestsFromName("SourceOrdinalTest.test_whole_snapshot_refusal",
                                                         module),
                                loader.loadTestsFromTestCase(UnrelatedConsumer)])
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=2, resultclass=RecordingResult).run(suite)
    run.save("isolation.unittest.txt", stream.getvalue())
    summary = {"tests_run": result.testsRun, "successful": result.wasSuccessful(),
               "outcomes": result.outcomes, "skipped": len(result.skipped)}
    run.receipt.setdefault("unittest", {})["isolation"] = summary
    run.require("isolation_guarded_case_then_unrelated_consumer_pass",
                summary["successful"] and summary["tests_run"] == 2 and not summary["skipped"]
                and summary["outcomes"] == {"test_whole_snapshot_refusal": "pass",
                                            "test_sees_original_callables_and_no_new_hits": "pass"},
                summary)
    after = identity_diff(before, guarded_identity())
    run.require("all_guarded_callables_restored_after_suite", after == [], after)
    run.require("no_guards_left_active", module.guards_active() == [], module.guards_active())


def check_focused(run):
    identity(run)
    artifacts = run.directory / "artifacts"
    artifacts.mkdir(mode=0o700)
    os.environ[ARTIFACT_ENV] = str(artifacts)
    before = guarded_identity()
    module = load_test_module(run)
    check_isolation(run, module, before)
    summary = run_cases(run, "focused", module, REQUIRED_CASES)
    after = identity_diff(before, guarded_identity())
    run.require("guarded_callables_restored_after_focused_suite", after == [], after)
    run.require("tests_ran", summary["tests_run"] == len(REQUIRED_CASES), summary["tests_run"])
    run.require("all_named_cases_pass",
                {name: summary["outcomes"].get("test_" + name) for name in REQUIRED_CASES} ==
                {name: "pass" for name in REQUIRED_CASES}, summary["outcomes"])
    run.require("no_failures_errors_or_skips",
                summary["successful"] and not (summary["skipped"] or summary["expected_failures"]
                                               or summary["unexpected_successes"]), summary)
    declared = sorted(name[5:] for name in dir(module.SourceOrdinalTest) if name.startswith("test_"))
    run.require("frozen_case_set_exact", declared == sorted(REQUIRED_CASES), declared)
    run.require("offline_sentinels_untouched", module.SENTINEL_HITS == [], module.SENTINEL_HITS)
    modules = loaded_application_modules()
    run.receipt["loaded_modules"] = modules
    outside = sorted(name for name, path in modules.items() if not Path(path).is_relative_to(REPO))
    run.require("application_modules_from_this_repo", not outside, outside)
    network = sorted(name for name in NETWORK_MODULES if name in sys.modules)
    run.require("network_capable_application_modules_not_loaded", not network, network)
    summary_path = artifacts / module.SUMMARY_NAME
    snapshot = json.loads(summary_path.read_text()) if summary_path.is_file() else None
    run.receipt["valid_snapshot"] = snapshot
    run.require("valid_snapshot_summary_recorded", snapshot is not None
                and snapshot.get("resolver_code_sha256") == sha_file(REPO / SOURCE), snapshot)
    payload_path = artifacts / module.PAYLOAD_NAME
    payload = json.loads(payload_path.read_text()) if payload_path.is_file() else None
    run.receipt["valid_revision_payload"] = payload
    run.require("valid_revision_payload_recorded", payload is not None
                and payload.get("payload", {}).get("resolver_code_sha256") == sha_file(REPO / SOURCE)
                and snapshot is not None and payload.get("revision_sha256") == snapshot.get("revision_sha256"),
                payload and payload.get("revision_sha256"))
    stray = sorted(str(path) for path in artifacts.iterdir()
                   if path.name not in (module.SUMMARY_NAME, module.PAYLOAD_NAME)
                   and not path.name.startswith("ledger-"))
    run.require("synthetic_artifacts_only_in_run_directory", not stray, stray)


def check_negative(run):
    identity(run)
    artifacts = run.directory / "artifacts"
    artifacts.mkdir(mode=0o700)
    os.environ[ARTIFACT_ENV] = str(artifacts)
    module = load_test_module(run)
    resolver = importlib.import_module("campaign_tool.records.detectors.sources")
    on_disk = sha_file(REPO / SOURCE)
    original = getattr(resolver, "_exact_ordinal", None)
    if not run.require("exact_type_predicate_present", callable(original)):
        return

    def equality_only(value, ordinal):
        # Deliberate in-memory regression: drop only the exact-type check.
        return value == ordinal

    with mock.patch.object(resolver, "_exact_ordinal", equality_only):
        broken = run_cases(run, "regressed", module, ("bool_not_integer", "float_not_integer"))
    run.require("regression_detected_bool", broken["outcomes"].get("test_bool_not_integer") == "fail",
                broken["outcomes"])
    run.require("regression_detected_float", broken["outcomes"].get("test_float_not_integer") == "fail",
                broken["outcomes"])
    run.require("regression_is_assertion_failure_not_error", broken["errors"] == 0 and broken["failures"] >= 2,
                broken)
    run.require("predicate_restored", resolver._exact_ordinal is original)
    restored = run_cases(run, "restored", module, ("valid_integer_first_and_later_positions",
                                                   "bool_not_integer", "float_not_integer"))
    run.require("restored_positive_and_refusals_pass",
                restored["successful"] and restored["tests_run"] == 3 and not restored["skipped"], restored)
    run.require("production_file_unchanged_on_disk", sha_file(REPO / SOURCE) == on_disk, on_disk)
    run.require("offline_sentinels_untouched", module.SENTINEL_HITS == [], module.SENTINEL_HITS)


def parse_unittest(stderr):
    text = stderr.decode(errors="replace")
    ran = re.findall(r"^Ran (\d+) tests? in ", text, re.M)
    final = re.findall(r"^(OK|FAILED)(?: \(([^)]*)\))?\s*$", text, re.M)
    return {"tests_run": int(ran[-1]) if ran else 0, "final": final[-1][0] if final else None,
            "detail": final[-1][1] if final else None}


def valid_payload_run(run, label, repo, root):
    """Run the valid-control case once in `repo` via the admitted unittest entry point."""
    artifacts = root / (label + "-artifacts")
    artifacts.mkdir(mode=0o700)
    environment = dict(os.environ)
    environment[ARTIFACT_ENV] = str(artifacts)
    record, _, stderr = run.command(label, [sys.executable, "-B", "-m", "unittest", "-v", VALID_CONTROL],
                                    cwd=repo, env=environment)
    parsed = parse_unittest(stderr)
    path = artifacts / "valid-snapshot-revision-payload.json"
    payload = json.loads(path.read_text()) if path.is_file() else None
    return {"exit": record["exit"], **parsed, "payload_path": str(path),
            "payload_sha256": sha_file(path) if path.is_file() else None,
            "resolver_file_sha256": sha_file(repo / SOURCE)}, payload


def check_revision_across_change(run, record):
    """Base vs candidate valid revision, with the installed-code binding kept intact.

    The resolver's revision hashes its own source bytes, so base and candidate
    revisions necessarily differ. This proves every other revision input is
    identical, and that each revision is exactly the hash of its own inputs.
    Nothing in production is frozen, removed or spoofed; the base resolver runs
    from a copy under this run directory only.
    """
    base = record.get("base")
    if not run.require("base_commit_known", bool(base), base):
        return
    shown, base_source, _ = run.command("git-show-base-source", ["git", "show", f"{base}:{SOURCE}"], cwd=REPO)
    if not run.require("base_source_read", shown["exit"] == 0 and base_source, shown["exit"]):
        return
    copy = run.directory / "base-copy" / "repo"
    shutil.copytree(REPO, copy, symlinks=True,
                    ignore=shutil.ignore_patterns(".git", "__pycache__", "node_modules"))
    (copy / SOURCE).write_bytes(base_source)
    base_info, base_payload = valid_payload_run(run, "base-valid-control", copy, run.directory)
    head_info, head_payload = valid_payload_run(run, "head-valid-control", REPO, run.directory)
    base_info["base_commit"] = base
    base_info["git_show_sha256"] = sha_bytes(base_source)
    comparison = {"base": base_info, "head": head_info}
    run.receipt["revision_across_change"] = comparison
    for side, info in comparison.items():
        run.require(side + "_valid_control_ok", info["exit"] == 0 and info["final"] == "OK"
                    and info["tests_run"] == 1 and not info["detail"], info)
    if not run.require("both_revision_payloads_recorded", bool(base_payload and head_payload)):
        return
    left, right = dict(base_payload["payload"]), dict(head_payload["payload"])
    comparison["base_revision_sha256"] = base_payload["revision_sha256"]
    comparison["head_revision_sha256"] = head_payload["revision_sha256"]
    comparison["base_resolver_code_sha256"] = left.pop("resolver_code_sha256")
    comparison["head_resolver_code_sha256"] = right.pop("resolver_code_sha256")
    differing = sorted(key for key in set(left) | set(right) if left.get(key) != right.get(key))
    comparison["differing_inputs_other_than_resolver_code"] = differing
    run.require("resolver_code_identities_are_the_actual_files",
                comparison["base_resolver_code_sha256"] == sha_bytes(base_source)
                and comparison["head_resolver_code_sha256"] == sha_file(REPO / SOURCE), comparison)
    run.require("all_revision_inputs_identical_except_resolver_code", differing == [], differing)
    comparison["revisions_equal"] = comparison["base_revision_sha256"] == comparison["head_revision_sha256"]
    comparison["note"] = ("revision_sha256 binds resolver_code_sha256 (sources.py bytes) by design; it "
                          "differs between base and candidate only through that binding. Exact-revision "
                          "wording reconciliation is owned by the admission owner, not this runner.")


def check_compatibility(run):
    record_identity = identity(run)
    record, _, stderr = run.command("compatibility", [sys.executable, "-B", "-m", "unittest", "-v",
                                                      *COMPATIBILITY], cwd=REPO)
    parsed = parse_unittest(stderr)
    run.receipt["compatibility"] = {"selectors": list(COMPATIBILITY), **parsed}
    run.require("compatibility_exit_zero", record["exit"] == 0, record["exit"])
    run.require("compatibility_tests_ran", parsed["tests_run"] > 0, parsed["tests_run"])
    run.require("compatibility_ok_without_skips", parsed["final"] == "OK" and not parsed["detail"], parsed)
    check_revision_across_change(run, record_identity)
    run.receipt["full_discovery"] = "operator-run; scoped selector success is not full-suite success"


def check_portability(run):
    identity(run)
    scanned = {}
    for name in (TEST, RUNNER):
        text = (REPO / name).read_text()
        scanned[name] = sorted(token for token in HOST_TOKENS if token in text)
    run.require("no_host_paths_in_new_test_or_runner", not any(scanned.values()), scanned)
    copy = run.directory / "second-prefix" / "fc4-source-copy" / "repo"
    shutil.copytree(REPO, copy, symlinks=True,
                    ignore=shutil.ignore_patterns(".git", "__pycache__", "node_modules"))
    copied = {name: sha_file(copy / name) for name in ALLOWED_CHANGES}
    run.receipt["copy"] = {"path": str(copy), "file_sha256": copied}
    run.require("copy_bytes_match", copied == {name: sha_file(REPO / name) for name in ALLOWED_CHANGES},
                copied)
    child_root = run.directory / "copy-focused"
    record, _, _ = run.command("copy-focused", [sys.executable, "-B", str(copy / RUNNER), "--check", "focused",
                                                "--output-root", str(child_root)], cwd=copy)
    run.require("copy_focused_exit_zero", record["exit"] == 0, record["exit"])
    receipts = sorted(child_root.glob("run-*/receipt.json"))
    if not run.require("copy_receipt_present", len(receipts) == 1, [str(path) for path in receipts]):
        return
    child = json.loads(receipts[0].read_text())
    run.receipt["copy_receipt"] = {"path": str(receipts[0]), "sha256": sha_file(receipts[0]),
                                   "result": child.get("result"), "repo": child.get("repo"),
                                   "loaded_modules": child.get("loaded_modules")}
    run.require("copy_focused_passed", child.get("result") == "PASS_REVIEW_REQUIRED", child.get("result"))
    run.require("copy_ran_from_second_prefix", child.get("repo") == str(copy.resolve()), child.get("repo"))
    modules = child.get("loaded_modules") or {}
    stray = sorted(name for name, path in modules.items() if not Path(path).is_relative_to(copy.resolve()))
    run.require("copy_loaded_only_copy_modules", bool(modules) and not stray, stray)
    first = run.directory / "first-prefix-focused"
    record, _, _ = run.command("first-prefix-focused", [sys.executable, "-B", str(REPO / RUNNER), "--check",
                                                        "focused", "--output-root", str(first)], cwd=REPO)
    run.require("first_prefix_focused_exit_zero", record["exit"] == 0, record["exit"])
    first_receipts = sorted(first.glob("run-*/receipt.json"))
    if run.require("first_prefix_receipt_present", len(first_receipts) == 1):
        left = json.loads(first_receipts[0].read_text()).get("valid_snapshot") or {}
        right = child.get("valid_snapshot") or {}
        keys = ("revision_sha256", "bindings_sha256", "units_sha256", "originals_sha256",
                "join_revision_sha256", "resolver_code_sha256")
        run.require("valid_snapshot_identical_across_prefixes",
                    bool(left) and all(left.get(key) == right.get(key) for key in keys),
                    {"first": left, "second": right})


def check_credentials(run):
    identity(run)
    scanner = Path(sys.executable).parent / "detect-secrets"
    if not run.require("detect_secrets_installed_beside_interpreter", scanner.is_file(), str(scanner)):
        return
    record, stdout, _ = run.command("detect-secrets", [str(scanner), "scan", "--all-files", "--exclude-files",
                                                       "^.git/", "--no-verify"], cwd=REPO)
    report = run.directory / "secret-scan.json"
    report.write_bytes(stdout)
    run.files["secret-scan.json"] = {"path": str(report), "sha256": sha_bytes(stdout), "bytes": len(stdout)}
    run.require("scan_exit_zero", record["exit"] == 0, record["exit"])
    try:
        results = json.loads(stdout)["results"]
        readable = isinstance(results, dict)
    except (ValueError, KeyError, TypeError):
        results, readable = {}, False
    run.require("scan_report_readable", readable)
    hits = {name: len(rows) for name, rows in (results or {}).items()}
    run.receipt["raw_hits_by_file"] = hits
    run.require("no_raw_hits_in_fc4_files", not any(name in hits for name in ALLOWED_CHANGES),
                {name: hits.get(name) for name in ALLOWED_CHANGES})
    record, _, _ = run.command("check-secret-report", [sys.executable, "-B", str(REPO / "tools" /
                                                       "check_secret_report.py"), str(report)], cwd=REPO)
    run.require("unchanged_checker_exit_zero", record["exit"] == 0, record["exit"])


def check_public_tree(run):
    identity(run)
    record, _, _ = run.command("public-tree", [sys.executable, "-B", str(REPO / "tools" /
                                                                     "check_public_tree.py")], cwd=REPO)
    run.require("public_tree_exit_zero", record["exit"] == 0, record["exit"])


MODES = {"focused": check_focused, "compatibility": check_compatibility, "negative": check_negative,
         "portability": check_portability, "credentials": check_credentials,
         "public-tree": check_public_tree}


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
