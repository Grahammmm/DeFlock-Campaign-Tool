#!/usr/bin/env python3
"""Offline, fail-closed acceptance for FC7-WP4-ARTIFACT-BOUND.

Only main installs process guards. Importing this runner does not modify peers.
The static closure includes optional bodies; only the named synthetic selection
is executable. These sentinels are not a hostile-code sandbox.
"""
import argparse
import ast
import contextlib
import hashlib
import importlib
import importlib.abc
import io
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import warnings
from urllib.parse import unquote, urlsplit

CHECKS = ("focused", "compatibility", "negative", "portability", "credentials", "public-tree")
CONTRACT = "FC7-WP4-ARTIFACT-BOUND/frozen-v1"
CHANGED = ("campaign_tool/records/catalog_wp4_support.py",
           "tests/records/test_fc7_wp4_artifact_bound.py", "tools/accept_fc7_wp4_artifact_bound.py")
FOCUSED = "tests.records.test_fc7_wp4_artifact_bound"
COMPATIBILITY = "tests.records.test_catalog_stage"
CASE_IDS = ("below_exact_and_over_limit", "missing_null_text_and_wrong_hash",
            "observed_fetch_never_contains_oversize_blob", "bound_remains_on_payload_select",
            "valid_artifact_fetch_and_consumer_bound_forwarding", "database_and_authority_unchanged")
# Exact application import surface for these synthetic selections. Optional
# extraction, private-source, model, mailbox and container branches stay inert.
ALLOWED_MODULES = frozenset((
    "campaign_tool", "campaign_tool.records", "campaign_tool.records.catalog_stage",
    "campaign_tool.records.catalog_wp4_support", "campaign_tool.records.ledger_catalog",
    "campaign_tool.records.config", "campaign_tool.records.intake", "campaign_tool.records.intake.folder",
    "campaign_tool.records.context_runtime", "campaign_tool.records.context_revision",
    "campaign_tool.records.context_snapshot_io", "campaign_tool.records.ledger",
    "campaign_tool.records.ledger.store", "campaign_tool.records.ledger.stages",
    "campaign_tool.records.ledger.original_resolution", "campaign_tool.records.ledger.original_resolution_mail",
    "campaign_tool.records.ledger.mail_resolution_policy", "campaign_tool.records.ledger.migrations",
    "campaign_tool.records.ledger.migrations.v001", "campaign_tool.records.ledger.migrations.stages_v001",
    "campaign_tool.records.ledger.migrations.stages_authority_v001",
    "campaign_tool.records.ledger.migrations.original_resolution_v001",
    "tests", "tests.records", FOCUSED, COMPATIBILITY,
))


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def file_sha(path):
    return sha(Path(path).read_bytes())


def save(output, name, data):
    with (output / name).open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(data, indent=2, sort_keys=True) + "\n")


def safe_path(path):
    path = Path(os.path.abspath(path))
    require(not any(part.is_symlink() for part in (path, *path.parents)), "symlink in evidence/source path")
    return path


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], cwd=repo, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout.strip()


def manifest_for(repo):
    manifest_path = safe_path(repo.parent / "SOURCE-MANIFEST.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    require(manifest.get("schema") == "agentorange.source-manifest/v1", "manifest schema mismatch")
    require(manifest.get("repo") == str(repo) and manifest.get("write_root") == str(repo.parent)
            and manifest.get("card") == "FC7-WP4-ARTIFACT-BOUND", "manifest repo/root/card mismatch")
    require(manifest.get("clean") is True, "staged base not declared clean")
    base = manifest.get("head", "")
    require(re.fullmatch(r"[0-9a-f]{40}", base) is not None, "invalid base HEAD")
    require(git(repo, "rev-parse", base + "^{tree}") == manifest.get("tree"), "staged base tree mismatch")
    git(repo, "merge-base", "--is-ancestor", base, "HEAD")
    require(git(repo, "remote", "get-url", "origin") == manifest.get("remote"), "origin mismatch")
    runtime = manifest["runtime"]
    venv = Path(runtime["repo_venv"])
    require(venv.is_dir() and Path(sys.prefix).resolve() == venv.resolve(), "invoked virtualenv mismatch")
    require((repo / ".venv").resolve() == venv.resolve(), "staged virtualenv link mismatch")
    require(Path(runtime["credential_scan_python3"]).parent.parent.resolve() == venv.resolve(),
            "credential runtime declaration mismatch")
    require(Path(runtime["detect_secrets"]).parent.parent.resolve() == venv.resolve(),
            "scanner runtime declaration mismatch")
    paths = set(git(repo, "diff", "--name-only", base).splitlines())
    paths.update(git(repo, "ls-files", "--others", "--exclude-standard").splitlines())
    require(paths <= set(CHANGED), "out-of-scope changed paths: " + repr(sorted(paths - set(CHANGED))))
    return manifest


def locate(repo, name):
    stem = repo.joinpath(*name.split("."))
    if stem.with_suffix(".py").is_file():
        return safe_path(stem.with_suffix(".py"))
    if (stem / "__init__.py").is_file():
        return safe_path(stem / "__init__.py")
    return None


def inspect_closure(repo, output):
    """AST-only transitive census, including optional imports in function bodies.

    Dynamic import/process expressions are retained verbatim for review, not
    treated as admission. All child processes in the test process are refused;
    optional local imports outside ALLOWED_MODULES are refused before execution.
    """
    pending = [FOCUSED, COMPATIBILITY, "tools.check_public_tree", "tools.check_secret_report"]
    seen = {}
    def enqueue(name):
        for end in range(1, len(name.split(".")) + 1):
            parent = ".".join(name.split(".")[:end])
            if locate(repo, parent) and parent not in seen and parent not in pending:
                pending.append(parent)
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        path = locate(repo, name)
        if path is None:
            raise RuntimeError("unavailable closure module " + name)
        with warnings.catch_warnings(record=True) as parse_warnings:
            warnings.simplefilter("always")
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imports, operations, defaults = [], [], []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append(alias.name)
                    enqueue(alias.name)
            elif isinstance(node, ast.ImportFrom):
                package = name.split(".") if path.name == "__init__.py" else name.split(".")[:-1]
                prefix = ".".join(package[:len(package) - node.level + 1]) if node.level else ""
                base = ".".join(filter(None, (prefix, node.module)))
                imports.append(base)
                enqueue(base)
                for alias in node.names:
                    enqueue(base + "." + alias.name)
            elif isinstance(node, ast.Call):
                function = ast.unparse(node.func)
                if any(word in function.lower() for word in (
                        "subprocess", "popen", "system", "spawn", "exec", "import_module", "__import__",
                        "socket", "connect", "model", "client", "process", "pool", "temp")):
                    operations.append({"line": node.lineno, "expression": ast.unparse(node)})
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defaults.extend(ast.unparse(value) for value in
                                [*node.args.defaults, *[v for v in node.args.kw_defaults if v is not None]])
        seen[name] = {"path": str(path.relative_to(repo)), "sha256": file_sha(path),
                      "imports": sorted(set(imports)), "operations": operations, "bound_defaults": defaults,
                      "parse_warnings": [str(warning.message) for warning in parse_warnings],
                      "module_statements": [ast.unparse(node) for node in tree.body if not isinstance(
                          node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Import, ast.ImportFrom))],
                      "test_process_import_allowed": name in ALLOWED_MODULES}
        enqueue(name)
    require(ALLOWED_MODULES <= set(seen), "synthetic import surface absent from closure census")
    report = {"modules": seen, "module_count": len(seen),
              "test_child_processes_allowed": [],
              "selection": [FOCUSED, COMPATIBILITY],
              "path_defaults": "Selected tests: tempfile defaults set before imports; no host-bound database/root defaults.",
              "optional_bodies": "Statically inventoried, not executed; nonselected application imports fail closed."}
    save(output, "closure.json", report)
    return report


@contextlib.contextmanager
def guards(output, source):
    """Process-local synthetic sentinels, active before any application import."""
    temporary = output / "tmp"
    temporary.mkdir(mode=0o700)
    old_temp = tempfile.tempdir
    environment = {key: os.environ.get(key) for key in ("TMPDIR", "TEMP", "TMP", "RECORDS_TEST_DEPENDENCIES")}
    require(environment["RECORDS_TEST_DEPENDENCIES"] in (None, "", "[]"), "external test dependency roots refused")
    for key in ("TMPDIR", "TEMP", "TMP"):
        os.environ[key] = str(temporary)
    os.environ["RECORDS_TEST_DEPENDENCIES"] = "[]"
    tempfile.tempdir = str(temporary)
    active = {"value": True}
    violations = []
    database_paths = []
    old_umask = os.umask(0o077)

    def refuse(message):
        violations.append(message)
        raise RuntimeError(message)

    def audit(event, args):
        if not active["value"]:
            return
        if event.startswith("socket.") or event in (
                "subprocess.Popen", "os.system", "os.fork", "os.forkpty", "os.posix_spawn", "os.exec", "pty.spawn"):
            refuse("offline sentinel: " + event)
        if event == "sqlite3.connect":
            value = os.fspath(args[0])
            if value == ":memory:":
                database_paths.append(value)
                return
            if value.startswith("file:"):
                parsed = urlsplit(value)
                if parsed.netloc:
                    refuse("database URI authority refused")
                value = unquote(parsed.path)
            path = safe_path(value)
            if not path.is_relative_to(output):
                refuse("database outside fresh synthetic run: " + str(path))
            database_paths.append(str(path))

    class Imports(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname.split(".")[0] in ("campaign_tool", "tests"):
                if fullname not in ALLOWED_MODULES:
                    refuse("unadmitted application/model import: " + fullname)
                expected = locate(source, fullname)
                if expected is None:
                    refuse("missing admitted source module: " + fullname)
            elif fullname.split(".")[0] in ("openai", "anthropic", "ollama", "litellm", "imaplib"):
                refuse("model/mail import sentinel: " + fullname)
            return None

    finder = Imports()
    sys.meta_path.insert(0, finder)
    sys.addaudithook(audit)
    sys.path.insert(0, str(source))
    try:
        yield
        for name, module in list(sys.modules.items()):
            if name in ALLOWED_MODULES:
                loaded = getattr(module, "__file__", None)
                require(loaded is not None and safe_path(loaded) == locate(source, name),
                        "loaded module path mismatch: " + name)
        require(not violations, "caught sentinel violation: " + repr(violations))
    finally:
        active["value"] = False
        sys.meta_path.remove(finder)
        sys.path.remove(str(source))
        tempfile.tempdir = old_temp
        os.umask(old_umask)
        for key, value in environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        save(output, "guards.json", {"violations": violations, "database_paths": database_paths,
                                    "temporary_root": str(temporary), "source_root": str(source),
                                    "limits": "Synthetic selections only; not a hostile-code sandbox."})


def test_ids(suite):
    if isinstance(suite, unittest.TestSuite):
        return [identity for test in suite for identity in test_ids(test)]
    return [suite.id()]


def run_suite(selector, output, name, suite=None):
    full_selection = suite is None
    stream, stdout, stderr = io.StringIO(), io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        if suite is None:
            suite = unittest.defaultTestLoader.loadTestsFromName(selector)
        ids = test_ids(suite)
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    complete = len(ids) == len(set(ids))
    if full_selection and selector == FOCUSED:
        complete = complete and set(ids) == {FOCUSED + ".ArtifactBoundTests.test_" + case for case in CASE_IDS}
    if full_selection and selector == COMPATIBILITY:
        complete = complete and len(ids) == 27 and all(identity.startswith(
            COMPATIBILITY + ".CatalogStageTests.test_") for identity in ids)
    okay = (bool(ids) and complete and result.wasSuccessful() and not result.skipped
            and not result.expectedFailures and result.testsRun == len(ids))
    payload = {"selector": selector, "selected_test_ids": ids, "selected_count": len(ids),
               "tests_run": result.testsRun, "failures": [(test.id(), trace) for test, trace in result.failures],
               "errors": [(test.id(), trace) for test, trace in result.errors],
               "skips": [(test.id(), why) for test, why in result.skipped],
               "expected_failures": [(test.id(), why) for test, why in result.expectedFailures],
               "complete_named_selection": complete,
               "stdout": stdout.getvalue(), "stderr": stream.getvalue() + stderr.getvalue(), "exit": 0 if okay else 1,
               "argv": list(sys.orig_argv), "cwd": os.getcwd()}
    save(output, name + ".json", payload)
    return payload


def negative(output):
    tests = importlib.import_module(FOCUSED)
    original = tests.ObservedCursor.execute
    def unbounded(self, sql, parameters=()):
        if "SELECT payload FROM stage_artifacts" in sql:
            sql, parameters = "SELECT payload FROM stage_artifacts WHERE sha256=?", tuple(parameters[:1])
        return original(self, sql, parameters)
    tests.ObservedCursor.execute = unbounded
    try:
        suite = unittest.TestSuite([tests.ArtifactBoundTests("test_observed_fetch_never_contains_oversize_blob")])
        mutant = run_suite(FOCUSED, output, "mutant", suite)
    finally:
        tests.ObservedCursor.execute = original
    failures = mutant["failures"]
    detected = (mutant["tests_run"] == 1 and not mutant["errors"] and not mutant["skips"]
                and len(failures) == 1 and "OVERSIZE_PAYLOAD_TRANSFER" in failures[0][1]
                and "[9] != []" in failures[0][1])
    suite = unittest.TestSuite([tests.ArtifactBoundTests("test_below_exact_and_over_limit"),
                               tests.ArtifactBoundTests("test_valid_artifact_fetch_and_consumer_bound_forwarding")])
    restored = run_suite(FOCUSED, output, "restored-positive", suite)
    require(detected, "mutant did not fail the deciding oversize-transfer assertion")
    require(restored["exit"] == 0 and restored["tests_run"] == 2, "restored exact-limit/hash controls failed")
    payload = {"mutation": "in-memory original unconditional SELECT payload at observed-cursor seam only",
               "mutant_detected": detected, "mutant": mutant, "restored": restored}
    save(output, "negative-control.json", payload)
    return payload


def run_child(argv, cwd, output, name):
    result = subprocess.run(argv, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False)
    receipt = {"argv": list(map(str, argv)), "cwd": str(cwd), "exit": result.returncode,
               "stdout": result.stdout, "stderr": result.stderr}
    save(output, name + ".json", receipt)
    require(result.returncode == 0, name + " failed; see " + str(output / (name + ".json")))
    return receipt


def execute(check, repo, output, manifest):
    if check in ("focused", "compatibility", "negative", "portability"):
        source = repo
        if check == "portability":
            source = output / "second-prefix" / "source-copy"
            for name in sorted(ALLOWED_MODULES):
                path = locate(repo, name)
                if path is None:
                    raise RuntimeError("missing portability source: " + name)
                destination = source / path.relative_to(repo)
                destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                shutil.copyfile(path, destination)
        with guards(output, source):
            if check == "negative":
                return negative(output)
            selector = COMPATIBILITY if check == "compatibility" else FOCUSED
            result = run_suite(selector, output, "tests")
            require(result["exit"] == 0, "selected tests failed/errored/skipped/empty; see tests.json")
            loaded = {name: str(module.__file__) for name, module in list(sys.modules.items())
                      if name in ALLOWED_MODULES}
            result["loaded_modules"] = loaded
            if check == "portability":
                require(all(Path(path).is_relative_to(source) for path in loaded.values()), "portability prefix mismatch")
                save(output, "portability.json", {"copy_prefix": str(source), "loaded_modules": loaded})
            return result
    if check == "credentials":
        runtime = manifest["runtime"]
        interpreter = Path(runtime["credential_scan_python3"])
        scanner = Path(runtime["detect_secrets"])
        require(interpreter.is_file() and scanner.is_file(), "scanner prerequisite unavailable")
        require(Path(sys.executable).absolute() == interpreter.absolute(), "literal credential interpreter mismatch")
        scan = run_child([str(scanner), "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"],
                         repo, output, "secret-scan-command")
        report = output / "secret-scan.json"
        with report.open("x", encoding="utf-8") as stream:
            stream.write(scan["stdout"])
        checker = run_child([str(interpreter), str(repo / "tools/check_secret_report.py"), str(report)],
                            repo, output, "secret-report-check")
        return {"scanner": scan, "checker": checker, "report_sha256": file_sha(report)}
    return run_child([str(repo / ".venv/bin/python3"), str(repo / "tools/check_public_tree.py")],
                     repo, output, "public-tree-check")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", required=True, choices=CHECKS)
    parser.add_argument("--output-root", required=True)
    args = parser.parse_args()
    sys.dont_write_bytecode = True
    repo = safe_path(Path(__file__).absolute().parents[1])
    manifest = manifest_for(repo)
    output_root = safe_path(args.output_root)
    require(output_root.is_relative_to(repo.parent) and not output_root.is_relative_to(repo)
            and output_root != repo.parent, "output root must be candidate-local and outside repository")
    output_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix="run-", dir=output_root))
    output.chmod(0o700)
    identity = {"check": args.check, "contract": CONTRACT, "contract_sha256": sha(CONTRACT.encode()),
                "run_directory": str(output), "base_head": manifest["head"],
                "candidate_head": git(repo, "rev-parse", "HEAD"),
                "source_hashes": {path: file_sha(repo / path) for path in CHANGED},
                "manifest_sha256": file_sha(repo.parent / "SOURCE-MANIFEST.json"),
                "argv": list(sys.orig_argv), "cwd": os.getcwd(),
                "python": sys.executable, "python_prefix": sys.prefix}
    save(output, "identity.json", identity)
    try:
        require(Path.cwd() == repo, "acceptance cwd must be staged repository")
        inspect_closure(repo, output)
        result = execute(args.check, repo, output, manifest)
        require(identity["candidate_head"] == git(repo, "rev-parse", "HEAD"), "HEAD changed during acceptance")
        require(identity["source_hashes"] == {path: file_sha(repo / path) for path in CHANGED},
                "source bytes changed during acceptance")
        summary = {**identity, "result": result, "status": "PASS_REVIEW_REQUIRED"}
        save(output, "summary.json", summary)
        line = json.dumps({"output": str(output), "status": summary["status"]}, sort_keys=True)
        save(output, "invocation.json", {"argv": list(sys.orig_argv), "cwd": os.getcwd(),
                                         "exit": 0, "stdout": line + "\n", "stderr": ""})
        print(line)
        return 0
    except Exception as error:
        failure = {**identity, "status": "FAIL", "error": type(error).__name__ + ": " + str(error)}
        save(output, "failure.json", failure)
        line = json.dumps({"output": str(output), "status": "FAIL", "error": failure["error"]}, sort_keys=True)
        save(output, "invocation.json", {"argv": list(sys.orig_argv), "cwd": os.getcwd(),
                                         "exit": 1, "stdout": "", "stderr": line + "\n"})
        print(line, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
