#!/usr/bin/env python3
"""Offline, fail-closed acceptance harness for FC7-WP4-ARTIFACT-BOUND."""
import argparse
import contextlib
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.parse import urlsplit, unquote

CHECKS = ("focused", "compatibility", "negative", "portability", "credentials", "public-tree")
CONTRACT = "FC7-WP4-ARTIFACT-BOUND/frozen-v1"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def file_sha(path):
    return sha(Path(path).read_bytes())


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], cwd=repo, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout.strip()


def manifest_for(repo):
    manifest_path = repo.parent / "SOURCE-MANIFEST.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("repo") != str(repo) or manifest.get("card") != "FC7-WP4-ARTIFACT-BOUND":
        raise RuntimeError("SOURCE-MANIFEST repo/card mismatch")
    if git(repo, "rev-parse", "HEAD") != manifest.get("head"):
        raise RuntimeError("staged base HEAD differs from SOURCE-MANIFEST")
    if Path(git(repo, "remote", "get-url", "origin")).resolve() != Path(manifest["remote"]).resolve():
        raise RuntimeError("repo origin differs from SOURCE-MANIFEST remote")
    runtime = manifest.get("runtime", {})
    declared = Path(runtime.get("repo_venv", ""))
    actual = Path(sys.executable).resolve()
    if not declared.is_dir() or Path(sys.executable).resolve().parent.parent.resolve() != declared.resolve():
        # venv python may resolve to system Python through a symlink; verify the invoked entry point too.
        invoked = Path(sys.argv[0]).resolve()
        if not invoked.is_relative_to(repo):
            raise RuntimeError("acceptance script is not in staged repo")
        if not (repo / ".venv" / "bin" / "python3").exists():
            raise RuntimeError("staged repository interpreter unavailable")
    return manifest


def install_guards(output):
    """Install test-process guards before application/test module imports."""
    os.environ["TMPDIR"] = str(output / "tmp")
    os.environ["TEMP"] = str(output / "tmp")
    os.environ["TMP"] = str(output / "tmp")
    (output / "tmp").mkdir(mode=0o700)
    tempfile.tempdir = str(output / "tmp")
    allowed_root = output.resolve()
    original_connect = sqlite3.connect

    def guarded_connect(database, *args, **kwargs):
        if database == ":memory:":
            return original_connect(database, *args, **kwargs)
        raw = os.fspath(database)
        if kwargs.get("uri"):
            parsed = urlsplit(raw)
            if parsed.scheme != "file" or parsed.netloc:
                raise RuntimeError("database URI guard rejected " + str(database))
            raw = unquote(parsed.path)
        candidate = Path(raw).resolve()
        if candidate == allowed_root or allowed_root in candidate.parents:
            candidate.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if kwargs.get("uri"):
                return original_connect(candidate.as_uri() + ("?" + urlsplit(os.fspath(database)).query), *args, **kwargs)
            return original_connect(str(candidate), *args, **kwargs)
        raise RuntimeError("database path guard rejected " + str(database))

    def denied(self, *args, **kwargs):
        raise RuntimeError("network guard rejected socket operation")

    sqlite3.connect = guarded_connect
    socket.socket.connect = denied
    socket.socket.connect_ex = denied
    socket.create_connection = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("network guard rejected socket operation"))
    socket.getaddrinfo = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("network guard rejected DNS lookup"))
    return original_connect


def run_suite(name, output, repo, module_root=None):
    if module_root is None:
        module_root = repo
    sys.path.insert(0, str(module_root))
    try:
        module_name = "tests.records.test_fc7_wp4_artifact_bound" if name == "focused" else "tests.records.test_catalog_stage"
        suite = unittest.defaultTestLoader.loadTestsFromName(module_name)
        ids = []
        def collect(test):
            if isinstance(test, unittest.TestSuite):
                for child in test:
                    collect(child)
            else:
                ids.append(test.id())
        collect(suite)
        stream = io.StringIO()
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
        skips = [str(item[0]) + ": " + item[1] for item in result.skipped]
        payload = {"selector": module_name, "selected_test_ids": ids, "selected_count": len(ids),
                   "tests_run": result.testsRun, "failures": [str(x[0]) + ": " + x[1] for x in result.failures],
                   "errors": [str(x[0]) + ": " + x[1] for x in result.errors], "skips": skips,
                   "raw_output": stream.getvalue(), "argv": list(sys.argv), "cwd": os.getcwd(),
                   "stderr": "", "exit": int(bool(not result.wasSuccessful() or not ids or skips))}
        (output / (name + "-tests.json")).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        if payload["exit"]:
            raise RuntimeError(name + " tests failed, selected zero tests, or skipped: " + json.dumps(payload, sort_keys=True))
        return payload
    finally:
        sys.path.remove(str(module_root))


def run_child(argv, cwd, output, name):
    result = subprocess.run(argv, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    receipt = {"argv": list(map(str, argv)), "cwd": str(cwd), "exit": result.returncode,
               "stdout": result.stdout, "stderr": result.stderr}
    (output / (name + ".json")).write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    if result.returncode != 0:
        raise RuntimeError(name + " failed: " + json.dumps(receipt, sort_keys=True))
    return receipt


def focused(repo, output):
    install_guards(output)
    return run_suite("focused", output, repo)


def compatibility(repo, output):
    install_guards(output)
    return run_suite("compatibility", output, repo)


def negative(repo, output):
    install_guards(output)
    sys.path.insert(0, str(repo))
    try:
        from campaign_tool.records import catalog_wp4_support as support
        import tests.records.test_fc7_wp4_artifact_bound as tests_module
        original_execute = tests_module.ObservedCursor.execute
        def unbounded_execute(self, sql, parameters=()):
            if "SELECT payload FROM stage_artifacts" in sql:
                sql, parameters = "SELECT payload FROM stage_artifacts WHERE sha256=?", tuple(parameters[:1])
            return original_execute(self, sql, parameters)
        tests_module.ObservedCursor.execute = unbounded_execute
        test = tests_module.ArtifactBoundTests("test_observed_fetch_never_contains_oversize_blob")
        mutant_result = unittest.TestResult()
        test.run(mutant_result)
        mutant_detected = bool(mutant_result.failures or mutant_result.errors)
        tests_module.ObservedCursor.execute = original_execute
        positive_suite = unittest.TestSuite([
            tests_module.ArtifactBoundTests("test_below_exact_and_over_limit"),
            tests_module.ArtifactBoundTests("test_valid_artifact_fetch_and_consumer_bound_forwarding"),
        ])
        stream = io.StringIO()
        positive_result = unittest.TextTestRunner(stream=stream, verbosity=2).run(positive_suite)
        payload = {"mutation": "in-memory replacement of bounded fetch seam with original unconditional payload SELECT",
                   "mutant_failure_detected": mutant_detected,
                   "mutant_failure": [str(x[0]) + ": " + x[1] for x in mutant_result.failures + mutant_result.errors],
                   "restored_positive_count": positive_result.testsRun,
                   "restored_positive_failures": [str(x[0]) + ": " + x[1] for x in positive_result.failures + positive_result.errors],
                   "restored_positive_output": stream.getvalue(), "exit": 0}
        if not mutant_detected or not positive_result.wasSuccessful() or positive_result.testsRun != 2:
            payload["exit"] = 1
        (output / "negative-control.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        if payload["exit"]:
            raise RuntimeError("negative control failed: " + json.dumps(payload, sort_keys=True))
        return payload
    finally:
        sys.path.remove(str(repo))


def portability(repo, output):
    install_guards(output)
    copy = output / "source-copy"
    shutil.copytree(repo / "campaign_tool", copy / "campaign_tool", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(repo / "tests", copy / "tests", ignore=shutil.ignore_patterns("__pycache__"))
    payload = run_suite("focused", output, repo, module_root=copy)
    from campaign_tool.records import catalog_wp4_support
    payload["copy_prefix"] = str(copy)
    payload["loaded_test_module"] = str(importlib.import_module("tests.records.test_fc7_wp4_artifact_bound").__file__)
    payload["loaded_support_module"] = str(catalog_wp4_support.__file__)
    payload["loaded_paths_are_copy"] = all(str(copy) in p for p in (payload["loaded_test_module"], payload["loaded_support_module"]))
    (output / "portability.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    if not payload["loaded_paths_are_copy"]:
        raise RuntimeError("portability import did not load from second source prefix")
    return payload


def credentials(repo, output, manifest):
    scanner_python = Path(manifest["runtime"]["credential_scan_python3"])
    scanner = Path(manifest["runtime"]["detect_secrets"])
    if not scanner_python.is_file() or not scanner.is_file() or scanner_python.resolve() != Path(sys.executable).resolve():
        raise RuntimeError("credential scanner interpreter/tool unavailable or interpreter mismatch")
    scan = run_child([str(scanner), "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"],
                     repo, output, "secret-scan-command")
    report = output / "secret-scan.json"
    report.write_text(scan["stdout"], encoding="utf-8")
    run_child([str(scanner_python), str(repo / "tools" / "check_secret_report.py"), str(report)],
              repo, output, "secret-report-check")
    return {"scanner": scan, "report": str(report)}


def public_tree(repo, output, manifest):
    interpreter = Path(manifest["runtime"]["repo_venv"]) / "bin" / "python3"
    if not interpreter.is_file():
        raise RuntimeError("repository interpreter unavailable")
    return run_child([str(interpreter), str(repo / "tools" / "check_public_tree.py")],
                     repo, output, "public-tree-check")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", required=True, choices=CHECKS)
    parser.add_argument("--output-root", required=True)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    output_root = Path(args.output_root).resolve()
    manifest = manifest_for(repo)
    output_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix="run-", dir=output_root))
    output.chmod(0o700)
    run_id = output.name
    before_head = git(repo, "rev-parse", "HEAD")
    source_paths = [repo / "campaign_tool/records/catalog_wp4_support.py",
                    repo / "tests/records/test_fc7_wp4_artifact_bound.py", Path(__file__).resolve()]
    source_hashes = {str(path.relative_to(repo)): file_sha(path) for path in source_paths}
    try:
        if args.check == "focused":
            result = focused(repo, output)
        elif args.check == "compatibility":
            result = compatibility(repo, output)
        elif args.check == "negative":
            result = negative(repo, output)
        elif args.check == "portability":
            result = portability(repo, output)
        elif args.check == "credentials":
            result = credentials(repo, output, manifest)
        else:
            result = public_tree(repo, output, manifest)
        summary = {"check": args.check, "run_id": run_id, "contract": CONTRACT,
                   "contract_sha256": sha(CONTRACT.encode()), "base_head": manifest["head"],
                   "candidate_head": before_head, "manifest_sha256": file_sha(repo.parent / "SOURCE-MANIFEST.json"),
                   "source_hashes": source_hashes,
                   "result": result, "status": "PASS_REVIEW_REQUIRED"}
        output_line = json.dumps({"run_id": run_id, "status": summary["status"], "output": str(output)}, sort_keys=True)
        invocation = {"argv": list(sys.argv), "cwd": os.getcwd(), "exit": 0,
                      "stdout": output_line + "\n", "stderr": ""}
        (output / "invocation.json").write_text(json.dumps(invocation, indent=2) + "\n", encoding="utf-8")
        (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        print(output_line)
        return 0
    except Exception as error:
        failure = {"check": args.check, "run_id": run_id, "contract": CONTRACT,
                   "base_head": manifest["head"], "candidate_head": before_head,
                   "source_hashes": source_hashes,
                   "error": type(error).__name__ + ": " + str(error)}
        failure_line = json.dumps(failure, sort_keys=True)
        invocation = {"argv": list(sys.argv), "cwd": os.getcwd(), "exit": 1,
                      "stdout": "", "stderr": failure_line + "\n"}
        (output / "invocation.json").write_text(json.dumps(invocation, indent=2) + "\n", encoding="utf-8")
        (output / "failure.json").write_text(json.dumps(failure, indent=2) + "\n", encoding="utf-8")
        print(failure_line, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
