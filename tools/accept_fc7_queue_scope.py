"""FC7-QUEUE-SCOPE acceptance runner (author material: claude-opus-5-5).

Checks: focused, compatibility, negative, portability, credentials, public-tree.
Every invocation creates a fresh exclusive run directory below --output-root
(outside the repo), records literal child argv/cwd/exit/stdout/stderr, and
exits 0 only when its complete named contract passes. A pass is machine
evidence only: REVIEW REQUIRED, never semantic, legal, stage, live or
publication acceptance.

Child test processes are this same file in --child mode with shell-free argv.
They install network/model/subprocess and database-path sentinels before any
application import. These guards are not a hostile-code sandbox.
"""
import argparse
import ast
import copy
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

REPO = Path(__file__).resolve().parents[1]
PARENT = REPO.parent
MANIFEST = PARENT / "SOURCE-MANIFEST.json"
CONTRACT = "FC7-QUEUE-SCOPE/acceptance-v1"
CHECKS = ("focused", "compatibility", "negative", "portability", "credentials", "public-tree")
ALLOWED_CHANGES = ("campaign_tool/records/queue.py", "tests/records/test_fc7_queue_scope.py",
                   "tools/accept_fc7_queue_scope.py")
STAGED_UNTRACKED = (".venv",)
FOCUSED = "tests.records.test_fc7_queue_scope"
COMPAT = "tests.records.test_queue"
CLASS = FOCUSED + ".FC7QueueScopeTests."
CASE_IDS = ("current_and_legacy_scope_exclusions", "excluded_role_with_in_scope",
            "ordinary_and_unresolved_preserved", "mixed_population_counts_and_order",
            "from_ledger_projects_role", "no_excluded_claim_candidate",
            "unchanged_input_and_publication_flags")
FOCUSED_IDS = sorted([CLASS + "test_" + case for case in CASE_IDS] + [CLASS + "test_source_vocabulary_is_pinned"])
# Unfixed base must fail exactly the exclusion/projection cases and keep the preservation cases green.
BASE_RED = sorted(CLASS + "test_" + case for case in (
    "current_and_legacy_scope_exclusions", "excluded_role_with_in_scope", "mixed_population_counts_and_order",
    "from_ledger_projects_role", "no_excluded_claim_candidate"))
NEGATIVE_DECIDING = sorted(CLASS + "test_" + case for case in (
    "current_and_legacy_scope_exclusions", "excluded_role_with_in_scope", "mixed_population_counts_and_order"))


class AcceptanceError(RuntimeError):
    pass


def check(condition, reason):
    if not condition:
        raise AcceptanceError(reason)


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


# ---------------------------------------------------------------- child mode

def install_sentinels(allowed_root):
    """Deny sockets, HTTP, subprocesses and databases outside allowed_root."""
    import socket
    import sqlite3
    from urllib.parse import unquote, urlsplit
    allowed = Path(allowed_root).resolve()
    denied = []

    def deny(kind):
        def blocked(*args, **kwargs):
            denied.append(kind)
            raise PermissionError("fc7_sentinel_denied:" + kind)
        return blocked

    socket.socket.connect = deny("socket.connect")
    socket.socket.connect_ex = deny("socket.connect_ex")
    socket.create_connection = deny("socket.create_connection")
    socket.getaddrinfo = deny("socket.getaddrinfo")
    import http.client
    import urllib.request
    http.client.HTTPConnection.connect = deny("http.connect")
    urllib.request.urlopen = deny("urllib.urlopen")
    subprocess.Popen.__init__ = deny("subprocess.Popen")
    os.system = deny("os.system")
    real_connect = sqlite3.connect

    def guarded(database, *args, **kwargs):
        text = os.fspath(database)
        if text == ":memory:" or text == "":
            return real_connect(database, *args, **kwargs)
        path = unquote(urlsplit(text).path) if text.startswith("file:") else text
        resolved = Path(path).resolve()
        if resolved != allowed and allowed not in resolved.parents:
            denied.append("sqlite:" + str(resolved))
            raise PermissionError("fc7_sentinel_denied:sqlite_path")
        return real_connect(database, *args, **kwargs)

    sqlite3.connect = guarded
    return denied


class Collect:
    """Plain per-test outcome collection, including subtests."""

    def __init__(self):
        import unittest

        class Result(unittest.TextTestResult):
            outcomes = {}
            subtests = []

            def _set(self, test, outcome):
                previous = self.outcomes.get(test.id())
                if previous in (None, "success"):
                    self.outcomes[test.id()] = outcome

            def addSuccess(self, test):
                super().addSuccess(test)
                self._set(test, "success")

            def addFailure(self, test, err):
                super().addFailure(test, err)
                self._set(test, "failure")

            def addError(self, test, err):
                super().addError(test, err)
                self._set(test, "error")

            def addSkip(self, test, reason):
                super().addSkip(test, reason)
                self._set(test, "skip")

            def addExpectedFailure(self, test, err):
                super().addExpectedFailure(test, err)
                self._set(test, "expected_failure")

            def addUnexpectedSuccess(self, test):
                super().addUnexpectedSuccess(test)
                self._set(test, "unexpected_success")

            def addSubTest(self, test, subtest, err):
                super().addSubTest(test, subtest, err)
                if err is None:
                    outcome = "success"
                elif err[0] is not None and issubclass(err[0], test.failureException):
                    outcome = "failure"
                else:
                    outcome = "error"
                self.subtests.append({"id": subtest.id(), "outcome": outcome})
                if outcome == "error" or (outcome == "failure" and self.outcomes.get(test.id()) != "error"):
                    self.outcomes[test.id()] = outcome

        self.result_class = Result


def run_suite(module, names=None):
    import unittest
    loader = unittest.TestLoader()
    if names:
        suite = unittest.TestSuite(loader.loadTestsFromName(module + "." + name) for name in names)
    else:
        suite = loader.loadTestsFromName(module)
    ids = sorted(test.id() for test in iter_tests(suite))
    stream = io.StringIO()
    collect = Collect()
    collect.result_class.outcomes = {}
    collect.result_class.subtests = []
    result = unittest.TextTestRunner(stream=stream, verbosity=2, resultclass=collect.result_class).run(suite)
    return {"selected_ids": ids, "selected": len(ids), "ran": result.testsRun,
            "outcomes": dict(sorted(result.outcomes.items())), "subtests": result.subtests,
            "failures": len(result.failures), "errors": len(result.errors), "skipped": len(result.skipped),
            "expected_failures": len(result.expectedFailures),
            "unexpected_successes": len(result.unexpectedSuccesses),
            "was_successful": result.wasSuccessful(), "log": stream.getvalue()}


def iter_tests(suite):
    for item in suite:
        if hasattr(item, "__iter__"):
            yield from iter_tests(item)
        else:
            yield item


def child(args):
    source = Path(args.source_root).resolve()
    allowed = Path(args.allowed_root).resolve()
    temp = allowed / "tmp"
    temp.mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = str(temp)
    import tempfile
    tempfile.tempdir = str(temp)
    denied = install_sentinels(allowed)
    sys.path.insert(0, str(source))
    os.chdir(source)
    report = {"source_root": str(source), "allowed_root": str(allowed), "tempdir": tempfile.gettempdir(),
              "python": sys.executable, "mode": args.child}
    probe = {}
    for name, attempt in (("socket", lambda: __import__("socket").create_connection(("127.0.0.1", 9))),
                          ("sqlite_outside", lambda: __import__("sqlite3").connect(str(source / "fc7-denied.sqlite")))):
        try:
            attempt()
            probe[name] = "allowed"
        except PermissionError as error:
            probe[name] = str(error)
    report["sentinel_probe"] = probe
    report["probe_denied"] = list(denied)
    denied.clear()
    names = args.names.split(",") if args.names else None
    if args.child == "unittest":
        report["run"] = run_suite(args.module, names)
    elif args.child == "negative":
        from campaign_tool.records import queue
        original = queue.excluded_from_claims

        def old_predicate(row):  # base behavior: only the current WP1 label excluded
            return row.get("scope") == "out_of_scope"

        from tests.records import test_fc7_queue_scope as fixture
        queue.excluded_from_claims = old_predicate
        try:
            report["mutant"] = run_suite(args.module, names)
            case = fixture.FC7QueueScopeTests("test_mixed_population_counts_and_order")
            originals, rows, leases, facts = case.mixed_population()
            plan = queue.plan(originals, rows, leases, now=fixture.NOW, facts=facts)
            top = {item["original_sha256"] for item in plan["top"]}
            report["mutant_probe"] = {
                "ordinary_in_scope_eligible": fixture.subject("ordinary-high") in top,
                "legacy_scope_leaked": fixture.subject("legacy") in top,
                "excluded_role_leaked": fixture.subject("personal") in top,
                "current_scope_excluded": {"original_sha256": fixture.subject("current"),
                                           "reason": "original_out_of_scope"} in plan["excluded_from_claims"],
                "eligible": plan["eligible"]}
        finally:
            queue.excluded_from_claims = original
        report["restored_identity"] = queue.excluded_from_claims is original
        report["restored"] = run_suite(args.module, names)
    report["denied"] = denied
    report["loaded_modules"] = {name: getattr(module, "__file__", None)
                                for name, module in sorted(sys.modules.items())
                                if name.split(".")[0] in ("campaign_tool", "tests") and module is not None}
    write_json(args.result, report)
    if args.child == "unittest":
        return 0 if report["run"]["was_successful"] else 1
    return 0 if report["restored"]["was_successful"] else 1


# ---------------------------------------------------------------- parent mode

def _walk_components(path, candidate_root):
    """Yield candidate_root and every lexical component of path below it."""
    yield candidate_root
    current = candidate_root
    for part in path.relative_to(candidate_root).parts:
        current = current / part
        yield current


def _check_components(path, candidate_root):
    for component in _walk_components(path, candidate_root):
        check(not component.is_symlink(), "output_root_symlink_component")
        if component.exists():
            check(component.is_dir(), "output_root_not_directory")


def prepare_output_root(output_root, repo, candidate_root):
    """Validate --output-root canonically before any write, then create it.

    Rules: no '..' components; lexically strictly inside the candidate root;
    no symlink at or below the candidate root on the way to it; never at or
    inside the repo, lexically or after resolution; resolution must equal the
    lexical path both before and after creation.
    """
    repo = Path(repo).resolve()
    candidate_root = Path(candidate_root).resolve()
    raw = Path(output_root)
    if not raw.is_absolute():
        raw = Path(os.getcwd()) / raw
    check(".." not in raw.parts, "output_root_dotdot")
    lexical = Path(os.path.normpath(str(raw)))
    check(lexical != candidate_root and lexical.is_relative_to(candidate_root), "output_root_outside_candidate_root")
    check(lexical != repo and not lexical.is_relative_to(repo), "output_root_inside_repo")
    _check_components(lexical, candidate_root)
    resolved = lexical.resolve()
    check(resolved == lexical, "output_root_alias")
    check(resolved != repo and not resolved.is_relative_to(repo), "output_root_inside_repo")
    lexical.mkdir(parents=True, exist_ok=True)
    _check_components(lexical, candidate_root)
    check(lexical.resolve(strict=True) == lexical, "output_root_alias")
    return lexical


class Run:
    def __init__(self, check_name, output_root, argv):
        self.check = check_name
        root = prepare_output_root(output_root, REPO, PARENT)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        self.dir = root / ("run-" + stamp + "-" + str(os.getpid()))
        self.dir.mkdir(exist_ok=False)
        check(not self.dir.is_symlink() and self.dir.resolve(strict=True) == self.dir, "run_dir_alias")
        self.calls = []
        self.findings = []
        self.evidence = {"schema": "fc7-queue-scope-acceptance-v1", "contract": CONTRACT, "check": check_name,
                         "argv": list(argv), "cwd": os.getcwd(), "python": sys.executable,
                         "run_dir": str(self.dir), "started_at": stamp,
                         "verdict_scope": "REVIEW REQUIRED: machine evidence only; not semantic, legal, human, "
                                          "stage, live or publication acceptance",
                         "full_discovery": "operator-run (owed, not claimed by this runner)"}

    def call(self, label, argv, cwd, *, stdin=None, binary_stdout=False, env=None):
        argv = [str(part) for part in argv]
        completed = subprocess.run(argv, cwd=str(cwd), input=stdin, capture_output=True, env=env, timeout=1800)
        out_path = self.dir / (label + ".stdout" + (".bin" if binary_stdout else ".txt"))
        if binary_stdout:
            out_path.write_bytes(completed.stdout)
        else:
            out_path.write_text(completed.stdout.decode(errors="replace"))
        (self.dir / (label + ".stderr.txt")).write_text(completed.stderr.decode(errors="replace"))
        record = {"label": label, "argv": argv, "cwd": str(cwd), "exit": completed.returncode,
                  "stdout": out_path.name, "stderr": label + ".stderr.txt"}
        self.calls.append(record)
        return completed

    def git(self, label, *args):
        completed = self.call(label, [shutil.which("git"), "-C", str(REPO), *args], REPO)
        check(completed.returncode == 0, "git_failed:" + label)
        return completed.stdout.decode().rstrip("\n")

    def child(self, label, mode, source_root, module, names=()):
        result = self.dir / (label + ".result.json")
        sandbox = self.dir / (label + "-sandbox")
        sandbox.mkdir(exist_ok=False)
        env = {"PATH": os.defpath, "LANG": "C.UTF-8", "TMPDIR": str(sandbox / "tmp"),
               "HOME": str(sandbox), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"}
        argv = [sys.executable, Path(__file__).resolve(), "--child", mode, "--source-root", source_root,
                "--allowed-root", sandbox, "--module", module, "--result", result]
        if names:
            argv += ["--names", ",".join(names)]
        completed = self.call(label, argv, source_root, env=env)
        check(result.exists(), "child_result_missing:" + label)
        report = json.loads(result.read_text())
        report["exit"] = completed.returncode
        check(report["sentinel_probe"].get("socket", "").startswith("fc7_sentinel_denied"), "socket_sentinel_inactive")
        check(report["sentinel_probe"].get("sqlite_outside", "").startswith("fc7_sentinel_denied"),
              "sqlite_sentinel_inactive")
        return report

    def finish(self, passed, error=None):
        self.evidence.update({"passed": passed, "error": error, "calls": self.calls, "findings": self.findings,
                              "finished_at": datetime.now(timezone.utc).isoformat()})
        write_json(self.dir / "evidence.json", self.evidence)
        print(json.dumps({"check": self.check, "passed": passed, "error": error, "run_dir": str(self.dir),
                          "findings": self.findings, "verdict": "REVIEW REQUIRED"}, indent=2, sort_keys=True))
        return 0 if passed else 1


def load_manifest(run, check_name):
    check(MANIFEST.is_file(), "source_manifest_missing")
    manifest = json.loads(MANIFEST.read_text())
    check(manifest.get("schema") == "agentorange.source-manifest/v1", "manifest_schema")
    check(manifest.get("card") == "FC7-QUEUE-SCOPE", "manifest_card")
    check(Path(manifest.get("repo", "")) == REPO, "manifest_repo_mismatch")
    check(Path(manifest.get("write_root", "")) == PARENT, "manifest_write_root_mismatch")
    runtime = manifest.get("runtime") or {}
    if check_name == "credentials":
        check(Path(sys.executable) == Path(runtime.get("credential_scan_python3", "")),
              "credential_interpreter_mismatch")
    else:
        check(Path(sys.executable).absolute() == REPO / ".venv" / "bin" / "python3", "repo_interpreter_mismatch")
        check((REPO / ".venv").resolve() == Path(runtime.get("repo_venv", "")).resolve(), "repo_venv_mismatch")
    base = manifest.get("head")
    remote = run.git("git-remote", "remote", "get-url", "origin")
    check(remote == manifest.get("remote"), "remote_mismatch")
    check(run.git("git-base-tree", "rev-parse", base + "^{tree}") == manifest.get("tree"), "base_tree_mismatch")
    head = run.git("git-head", "rev-parse", "HEAD")
    ancestor = run.call("git-ancestor", [shutil.which("git"), "-C", REPO, "merge-base", "--is-ancestor", base, "HEAD"],
                        REPO)
    check(ancestor.returncode == 0, "base_not_ancestor")
    committed = run.git("git-diff-names", "diff", "--name-only", base, "HEAD").splitlines()
    status = run.git("git-status", "status", "--porcelain", "--untracked-files=all")
    working = [line[3:] for line in status.splitlines() if line[3:] not in STAGED_UNTRACKED]
    changed = sorted(set(committed) | set(working))
    outside = [path for path in changed if path not in ALLOWED_CHANGES]
    check(not outside, "changes_outside_write_set:" + ",".join(outside))
    run.evidence.update({"base": base, "candidate_head": head, "remote": remote,
                         "working_tree_clean": not working, "working_changes": working,
                         "changed_files": {path: sha256_file(REPO / path) for path in changed},
                         "runner_sha256": sha256_file(Path(__file__).resolve()),
                         "manifest_sha256": sha256_file(MANIFEST)})
    return manifest, base


def export_base(run, base, label):
    archive = run.call(label + "-archive", [shutil.which("git"), "-C", REPO, "archive", "--format=tar", base], REPO,
                       binary_stdout=True)
    check(archive.returncode == 0, "git_archive_failed")
    target = run.dir / label
    target.mkdir(exist_ok=False)
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as handle:
        handle.extractall(target, filter="data")
    return target


def require_outcomes(run, exit_code, denied, expected_ids, expected_failures, label):
    """Fail-closed gate on one structured child unittest report.

    expected_ids must be selected and run exactly once each. Every test listed
    in expected_failures must be an actual assertion failure and every other
    test an actual success. Any error, skip, expected failure, unexpected
    success, subtest error, count mismatch or sentinel denial is rejected.
    exit_code None means the child exit belongs to a different suite.
    """
    expected_ids = sorted(expected_ids)
    expected_failures = sorted(expected_failures)
    check(len(expected_ids) > 0 and run.get("selected", 0) > 0 and run.get("ran", 0) > 0, label + ":zero_selected")
    check(set(expected_failures) <= set(expected_ids), label + ":expected_failure_not_selected")
    check(run["selected_ids"] == expected_ids, label + ":selected_ids_mismatch")
    check(run["selected"] == len(expected_ids) and run["ran"] == len(expected_ids), label + ":count_mismatch")
    outcomes = run["outcomes"]
    check(sorted(outcomes) == expected_ids, label + ":outcome_ids_mismatch")
    check(run["errors"] == 0 and run["skipped"] == 0 and run["expected_failures"] == 0
          and run["unexpected_successes"] == 0, label + ":error_skip_or_xfail")
    check(set(outcomes.values()) <= {"success", "failure"}, label + ":non_pass_fail_outcome")
    check(all(item.get("outcome") in ("success", "failure") for item in run.get("subtests", [])),
          label + ":subtest_error")
    failed = sorted(key for key, value in outcomes.items() if value == "failure")
    check(failed == expected_failures, label + ":failure_set_mismatch:" + ",".join(failed))
    if expected_failures:
        check(run["failures"] >= len(expected_failures) and run["was_successful"] is False, label + ":red_not_reported")
        if exit_code is not None:
            check(exit_code != 0, label + ":red_child_exit_zero")
    else:
        check(run["failures"] == 0 and run["was_successful"] is True, label + ":failed")
        if exit_code is not None:
            check(exit_code == 0, label + ":child_exit")
    check(not denied, label + ":sentinel_denied_call")
    return failed


def require_green(report, expected_ids, label):
    require_outcomes(report["run"], report["exit"], report["denied"], expected_ids, (), label)


def summary(report_run):
    return {key: report_run[key] for key in ("selected", "ran", "failures", "errors", "skipped", "selected_ids",
                                             "outcomes")}


def do_focused(run, base):
    for case in CASE_IDS:
        check(CLASS + "test_" + case in FOCUSED_IDS, "case_map_missing:" + case)
    base_src = export_base(run, base, "base-src")
    check(not (base_src / "tests/records/test_fc7_queue_scope.py").exists(), "base_already_has_fc7_test")
    shutil.copy2(REPO / "tests/records/test_fc7_queue_scope.py", base_src / "tests/records/test_fc7_queue_scope.py")
    red = run.child("focused-base", "unittest", base_src, FOCUSED)
    failed = require_outcomes(red["run"], red["exit"], red["denied"], FOCUSED_IDS, BASE_RED, "base_red")
    check(Path(red["loaded_modules"]["campaign_tool.records.queue"]).is_relative_to(base_src), "base_queue_not_loaded")
    green = run.child("focused-candidate", "unittest", REPO, FOCUSED)
    require_green(green, FOCUSED_IDS, "focused")
    check(green["loaded_modules"]["campaign_tool.records.queue"] == str(REPO / "campaign_tool/records/queue.py"),
          "candidate_queue_not_loaded")
    run.evidence["case_map"] = {case: CLASS + "test_" + case for case in CASE_IDS}
    run.evidence["base_red"] = {"failed_ids": failed, **summary(red["run"])}
    run.evidence["candidate"] = summary(green["run"])
    run.findings.append("base red %d/%d; candidate green %d/%d" % (len(failed), red["run"]["selected"],
                                                                  green["run"]["ran"], green["run"]["selected"]))


def static_imports(path):
    tree = ast.parse(Path(path).read_text())
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.add(("." * node.level) + (node.module or ""))
        elif isinstance(node, ast.Call) and getattr(node.func, "attr", "") in ("run", "Popen", "call", "check_output",
                                                                               "system"):
            found.add("CALL:" + ast.unparse(node.func))
    return sorted(found)


def do_compatibility(run, base):
    run.evidence["static_closure"] = {"tests/records/test_queue.py": static_imports(REPO / "tests/records/test_queue.py"),
                                      "campaign_tool/records/queue.py": static_imports(
                                          REPO / "campaign_tool/records/queue.py")}
    check(not any(item.startswith("CALL:") for item in run.evidence["static_closure"]["tests/records/test_queue.py"]),
          "compat_test_spawns_children")
    base_src = export_base(run, base, "base-src")
    check(sha256_file(base_src / "tests/records/test_queue.py") == sha256_file(REPO / "tests/records/test_queue.py"),
          "compat_test_changed")
    before = run.child("compat-before", "unittest", base_src, COMPAT)
    before_ids = before["run"].get("selected_ids") or []
    check(before_ids and all(name.startswith(COMPAT + ".") for name in before_ids), "compat_before_ids_invalid")
    require_green(before, before_ids, "compat_before")
    after = run.child("compat-after", "unittest", REPO, COMPAT)
    require_green(after, before["run"]["selected_ids"], "compat_after")
    check(all(name.startswith(COMPAT + ".") for name in after["run"]["selected_ids"]), "compat_foreign_ids")
    run.evidence["before"] = summary(before["run"])
    run.evidence["after"] = summary(after["run"])
    run.evidence["loaded_modules_after"] = after["loaded_modules"]
    run.findings.append("test_queue before %d pass, after %d pass" % (before["run"]["ran"], after["run"]["ran"]))


def do_negative(run, base):
    queue_hash = sha256_file(REPO / "campaign_tool/records/queue.py")
    names = [test_id.rsplit(".", 2)[-2] + "." + test_id.rsplit(".", 1)[-1] for test_id in NEGATIVE_DECIDING]
    names.append("FC7QueueScopeTests.test_ordinary_and_unresolved_preserved")
    report = run.child("negative", "negative", REPO, FOCUSED, names)
    check(sha256_file(REPO / "campaign_tool/records/queue.py") == queue_hash, "production_file_changed_on_disk")
    mutant, probe = report["mutant"], report["mutant_probe"]
    selected = sorted(CLASS + name.split(".", 1)[1] for name in names)
    # The child exit reflects the restored suite, so the mutant gate takes exit None.
    failed = require_outcomes(mutant, None, report["denied"], selected, NEGATIVE_DECIDING, "mutant")
    check(mutant["outcomes"].get(CLASS + "test_ordinary_and_unresolved_preserved") == "success",
          "mutant_broke_ordinary_case")
    check(probe["ordinary_in_scope_eligible"] is True and probe["legacy_scope_leaked"] is True
          and probe["excluded_role_leaked"] is True and probe["current_scope_excluded"] is True,
          "mutant_probe_unexpected")
    check(report["restored_identity"] is True, "predicate_not_restored")
    require_outcomes(report["restored"], report["exit"], report["denied"], selected, (), "restored")
    run.evidence.update({"mutation": "queue.excluded_from_claims -> scope == 'out_of_scope' (in memory only)",
                         "mutant": {"failed_ids": failed, **summary(mutant)}, "mutant_probe": probe,
                         "restored": summary(report["restored"]), "queue_sha256_unchanged": queue_hash})
    run.findings.append("mutant detected by %d deciding tests; restored positive %d/%d" % (
        len(failed), report["restored"]["ran"], len(names)))


def do_portability(run, base):
    for relative in ("tests/records/test_fc7_queue_scope.py", "tools/accept_fc7_queue_scope.py"):
        text = (REPO / relative).read_text()
        check(str(Path.home()) not in text and "/" + "home/" not in text, "box_absolute_constant:" + relative)
    tracked = run.git("git-ls-files", "ls-files", "-z", "--cached", "--others", "--exclude-standard").split("\0")
    copy_root = run.dir / "second-prefix" / "copy" / "repo"
    copied = 0
    for relative in sorted(set(filter(None, tracked))):
        source = REPO / relative
        if relative in STAGED_UNTRACKED or source.is_symlink() or not source.is_file():
            continue
        target = copy_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied += 1
    check(copied > 0, "copy_empty")
    check(sha256_file(copy_root / "campaign_tool/records/queue.py") == sha256_file(REPO / "campaign_tool/records/queue.py"),
          "copy_queue_mismatch")
    primary = run.child("portability-primary", "unittest", REPO, FOCUSED)
    require_green(primary, FOCUSED_IDS, "portability_primary")
    second = run.child("portability-second", "unittest", copy_root, FOCUSED)
    require_green(second, FOCUSED_IDS, "portability_second")
    for name, path in second["loaded_modules"].items():
        if path and (name.startswith("campaign_tool.records") or name.startswith("tests.records")):
            check(Path(path).resolve().is_relative_to(copy_root.resolve()), "second_copy_loaded_foreign:" + name)
    check(second["tempdir"].startswith(str(run.dir)), "tempdir_not_isolated")
    run.evidence.update({"second_copy_root": str(copy_root), "copied_files": copied,
                         "primary": summary(primary["run"]), "second": summary(second["run"]),
                         "second_loaded_modules": second["loaded_modules"],
                         "primary_loaded_modules": primary["loaded_modules"],
                         "sentinel_probe": second["sentinel_probe"],
                         "guard_note": "network/subprocess/sqlite-path sentinels are not a hostile-code sandbox"})
    run.findings.append("focused green from %s and %s" % (REPO, copy_root))


def do_credentials(run, manifest):
    scanner = Path(manifest["runtime"]["detect_secrets"])
    check(scanner.is_file() and os.access(scanner, os.X_OK), "detect_secrets_unavailable")
    scan = run.call("secret-scan", [scanner, "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"], REPO)
    report = run.dir / "secret-scan.json"
    report.write_bytes(scan.stdout)
    check(scan.returncode == 0, "secret_scan_failed")
    json.loads(scan.stdout)
    checker = run.call("secret-check", [sys.executable, REPO / "tools/check_secret_report.py", report], REPO)
    check(checker.returncode == 0, "secret_report_check_failed")
    run.evidence.update({"scanner": str(scanner), "report_sha256": sha256_file(report),
                         "checker_sha256": sha256_file(REPO / "tools/check_secret_report.py")})
    run.findings.append("detect-secrets exit 0; check_secret_report exit 0")


def do_public_tree(run):
    completed = run.call("public-tree", [sys.executable, REPO / "tools/check_public_tree.py"], REPO)
    run.evidence["public_tree_checker_sha256"] = sha256_file(REPO / "tools/check_public_tree.py")
    try:
        findings = json.loads(completed.stdout).get("findings", [])
    except ValueError:
        findings = None
    # Diagnostic only; never changes the verdict. A staged untracked .venv link is
    # a staging-population conflict for its owner, not something this runner may ignore.
    run.evidence["public_tree_findings"] = findings
    run.evidence["public_tree_findings_other_than_staged_link"] = None if findings is None else [
        item for item in findings if item[:1] != [".venv"]]
    check(completed.returncode == 0, "public_tree_failed")
    run.findings.append("check_public_tree exit 0")


def _expect_rejected(cases, label, reason, function):
    try:
        function()
    except AcceptanceError as error:
        cases.append({"case": label, "expected": reason, "actual": str(error), "ok": str(error).startswith(reason)})
        return
    cases.append({"case": label, "expected": reason, "actual": "accepted", "ok": False})


def _expect_accepted(cases, label, function):
    try:
        value = function()
    except AcceptanceError as error:
        cases.append({"case": label, "expected": "accepted", "actual": str(error), "ok": False})
        return None
    cases.append({"case": label, "expected": "accepted", "actual": "accepted", "ok": True})
    return value


def _synthetic_run(ids, failures=()):
    outcomes = {key: ("failure" if key in failures else "success") for key in ids}
    return {"selected_ids": sorted(ids), "selected": len(ids), "ran": len(ids), "outcomes": outcomes,
            "subtests": [], "failures": len(failures), "errors": 0, "skipped": 0, "expected_failures": 0,
            "unexpected_successes": 0, "was_successful": not failures, "log": "synthetic gate fixture"}


def gate_selftest(run):
    """Exercise the output-root and child-report gates on synthetic inputs only.

    Paths live in a fresh disposable directory inside this run directory; every
    negative must be rejected before it writes anything. Reports are synthetic
    dicts at the parent gate seam, never presented as actual suite output.
    """
    cases = []
    base = run.dir / "gate-selftest"
    candidate = base / "candidate"
    repo = candidate / "repo"
    out = candidate / "out"
    outside = base / "outside"
    repo.mkdir(parents=True)
    out.mkdir()
    (candidate / "alias").symlink_to(repo, target_is_directory=True)
    (out / "alias-out").symlink_to(repo, target_is_directory=True)
    (candidate / "plain-file").write_text("synthetic\n")
    path_cases = (
        ("dotdot_into_repo", out / ".." / "repo", "output_root_dotdot"),
        ("dotdot_escape", candidate / ".." / "outside", "output_root_dotdot"),
        ("absolute_escape", outside, "output_root_outside_candidate_root"),
        ("candidate_root_itself", candidate, "output_root_outside_candidate_root"),
        ("inside_repo", repo / "out", "output_root_inside_repo"),
        ("repo_itself", repo, "output_root_inside_repo"),
        ("symlink_into_repo", candidate / "alias", "output_root_symlink_component"),
        ("below_symlink_into_repo", candidate / "alias" / "nested", "output_root_symlink_component"),
        ("nested_symlink_into_repo", out / "alias-out", "output_root_symlink_component"),
        ("file_component", candidate / "plain-file" / "nested", "output_root_not_directory"),
    )
    for label, path, reason in path_cases:
        _expect_rejected(cases, "path:" + label, reason,
                         lambda path=path: prepare_output_root(path, repo, candidate))
    fresh = out / "fresh"
    made = _expect_accepted(cases, "path:fresh_outside_repo", lambda: prepare_output_root(fresh, repo, candidate))
    cases.append({"case": "path:fresh_created", "ok": made == fresh and fresh.is_dir() and not fresh.is_symlink()})
    cases.append({"case": "path:negatives_wrote_nothing",
                  "ok": not any(repo.iterdir()) and not outside.exists()
                  and sorted(p.name for p in out.iterdir()) == ["alias-out", "fresh"]})

    ids, red = FOCUSED_IDS, BASE_RED

    def gate(report, failures, exit_code=1, denied=(), expected=ids):
        return lambda: require_outcomes(report, exit_code, list(denied), expected, failures, "selftest")

    _expect_accepted(cases, "report:valid_base_red", gate(_synthetic_run(ids, red), red))
    _expect_accepted(cases, "report:valid_green", gate(_synthetic_run(ids), (), exit_code=0))
    skips = _synthetic_run(ids, red[:1])
    for key in red[1:]:
        skips["outcomes"][key] = "skip"
    skips["skipped"] = len(red) - 1
    _expect_rejected(cases, "report:red_one_failure_four_skips", "selftest:error_skip_or_xfail", gate(skips, red))
    skips_hidden = copy.deepcopy(skips)
    skips_hidden["skipped"] = 0
    _expect_rejected(cases, "report:red_skip_outcome_uncounted", "selftest:non_pass_fail_outcome",
                     gate(skips_hidden, red))
    errors = _synthetic_run(ids, red)
    errors["outcomes"][red[0]] = "error"
    errors["errors"], errors["failures"] = 1, len(red) - 1
    _expect_rejected(cases, "report:red_error_not_failure", "selftest:error_skip_or_xfail", gate(errors, red))
    errors_hidden = copy.deepcopy(errors)
    errors_hidden["errors"] = 0
    _expect_rejected(cases, "report:red_error_outcome_uncounted", "selftest:non_pass_fail_outcome",
                     gate(errors_hidden, red))
    xfail = _synthetic_run(ids, red)
    xfail["expected_failures"] = 1
    _expect_rejected(cases, "report:red_expected_failure", "selftest:error_skip_or_xfail", gate(xfail, red))
    unexpected = _synthetic_run(ids, red)
    unexpected["unexpected_successes"] = 1
    _expect_rejected(cases, "report:red_unexpected_success", "selftest:error_skip_or_xfail", gate(unexpected, red))
    extra = sorted(set(red) | {CLASS + "test_ordinary_and_unresolved_preserved"})
    _expect_rejected(cases, "report:red_extra_failure", "selftest:failure_set_mismatch",
                     gate(_synthetic_run(ids, extra), red))
    _expect_rejected(cases, "report:red_missing_failure", "selftest:failure_set_mismatch",
                     gate(_synthetic_run(ids, red[1:]), red))
    missing = _synthetic_run(ids[1:], red[1:])
    _expect_rejected(cases, "report:selected_id_missing", "selftest:selected_ids_mismatch", gate(missing, red))
    short = _synthetic_run(ids, red)
    short["ran"] = len(ids) - 1
    _expect_rejected(cases, "report:ran_count_short", "selftest:count_mismatch", gate(short, red))
    lost = _synthetic_run(ids, red)
    del lost["outcomes"][ids[-1]]
    _expect_rejected(cases, "report:outcome_missing", "selftest:outcome_ids_mismatch", gate(lost, red))
    subtest = _synthetic_run(ids, red)
    subtest["subtests"] = [{"id": red[0] + " (case)", "outcome": "error"}]
    _expect_rejected(cases, "report:subtest_error", "selftest:subtest_error", gate(subtest, red))
    _expect_rejected(cases, "report:sentinel_denied", "selftest:sentinel_denied_call",
                     gate(_synthetic_run(ids, red), red, denied=["socket.connect"]))
    _expect_rejected(cases, "report:red_exit_zero", "selftest:red_child_exit_zero",
                     gate(_synthetic_run(ids, red), red, exit_code=0))
    flagged = _synthetic_run(ids, red)
    flagged["was_successful"] = True
    _expect_rejected(cases, "report:red_marked_successful", "selftest:red_not_reported", gate(flagged, red))
    _expect_rejected(cases, "report:empty_selection", "selftest:zero_selected",
                     gate(_synthetic_run([]), (), exit_code=0, expected=[]))
    green_skip = _synthetic_run(ids)
    green_skip["outcomes"][ids[0]] = "skip"
    green_skip["skipped"] = 1
    _expect_rejected(cases, "report:green_with_skip", "selftest:error_skip_or_xfail",
                     gate(green_skip, (), exit_code=0))
    _expect_rejected(cases, "report:green_child_exit_nonzero", "selftest:child_exit",
                     gate(_synthetic_run(ids), (), exit_code=1))
    _expect_rejected(cases, "report:green_with_failure", "selftest:failure_set_mismatch",
                     gate(_synthetic_run(ids, red[:1]), (), exit_code=0))
    deciding = sorted(NEGATIVE_DECIDING + [CLASS + "test_ordinary_and_unresolved_preserved"])
    _expect_accepted(cases, "report:valid_mutant", gate(_synthetic_run(deciding, NEGATIVE_DECIDING),
                                                        NEGATIVE_DECIDING, exit_code=None, expected=deciding))
    mutant_skip = _synthetic_run(deciding, NEGATIVE_DECIDING[:1])
    for key in NEGATIVE_DECIDING[1:]:
        mutant_skip["outcomes"][key] = "skip"
    mutant_skip["skipped"] = len(NEGATIVE_DECIDING) - 1
    _expect_rejected(cases, "report:mutant_skips", "selftest:error_skip_or_xfail",
                     gate(mutant_skip, NEGATIVE_DECIDING, exit_code=None, expected=deciding))
    mutant_ordinary = _synthetic_run(deciding, deciding)
    _expect_rejected(cases, "report:mutant_broke_ordinary", "selftest:failure_set_mismatch",
                     gate(mutant_ordinary, NEGATIVE_DECIDING, exit_code=None, expected=deciding))
    failed_cases = [case["case"] for case in cases if not case["ok"]]
    run.evidence["gate_selftest"] = {"scope": "synthetic gate inputs only; not actual test-suite output",
                                     "cases": cases, "count": len(cases), "failed": failed_cases}
    check(not failed_cases, "gate_selftest_failed:" + ",".join(failed_cases))
    run.findings.append("gate self-test %d/%d synthetic cases behaved" % (len(cases), len(cases)))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", choices=CHECKS)
    parser.add_argument("--output-root")
    parser.add_argument("--child", choices=("unittest", "negative"), help=argparse.SUPPRESS)
    parser.add_argument("--source-root", help=argparse.SUPPRESS)
    parser.add_argument("--allowed-root", help=argparse.SUPPRESS)
    parser.add_argument("--module", help=argparse.SUPPRESS)
    parser.add_argument("--names", help=argparse.SUPPRESS)
    parser.add_argument("--result", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.child:
        return child(args)
    if not args.check or not args.output_root:
        parser.error("--check and --output-root are required")
    try:
        run = Run(args.check, args.output_root, [sys.executable, str(Path(__file__).resolve()), *argv])
    except AcceptanceError as error:  # rejected before any write; no run directory exists
        print(json.dumps({"check": args.check, "passed": False, "error": "AcceptanceError: " + str(error),
                          "run_dir": None, "verdict": "REVIEW REQUIRED"}, indent=2, sort_keys=True))
        return 1
    try:
        gate_selftest(run)
        manifest, base = load_manifest(run, args.check)
        if args.check == "focused":
            do_focused(run, base)
        elif args.check == "compatibility":
            do_compatibility(run, base)
        elif args.check == "negative":
            do_negative(run, base)
        elif args.check == "portability":
            do_portability(run, base)
        elif args.check == "credentials":
            do_credentials(run, manifest)
        else:
            do_public_tree(run)
    except Exception as error:  # fail closed with evidence for every unexpected failure
        return run.finish(False, type(error).__name__ + ": " + str(error))
    return run.finish(True)


if __name__ == "__main__":
    raise SystemExit(main())
