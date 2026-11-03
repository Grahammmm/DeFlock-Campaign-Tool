#!/usr/bin/env python3
"""Offline FC7 acceptance. Guards cover inspected code, not hostile code.

Public arguments are deliberately limited to the six admitted checks. Internal
fresh-process workers are script files in the exclusive output directory, never
interpreter snippets. Importing this module installs no process-global hooks.
"""
from __future__ import annotations

import argparse
import ast

import hashlib
import importlib
import importlib.util
import io
import json
import os
from pathlib import Path
import runpy
import shutil
import socket
import sqlite3
import ssl  # Finish socket subclass definitions before patching network entrypoints.
import subprocess
import sys
import tarfile
import tempfile
import traceback
import unittest
from urllib.parse import unquote, urlsplit


CONTRACT = "FC7-SEARCH-UNICODE:v1:2026-10-24"

CASES = (
    "valid_ascii_and_multibyte_query", "high_low_and_embedded_surrogates",
    "byte_limit_exact_and_plus_one", "blank_nonstring_and_boolean_limit",
    "invalid_before_path_or_sqlite", "valid_search_results_and_index_unchanged",
)
FOCUSED = "tests.records.test_fc7_search_unicode"
COMPAT = ["tests.records.test_understanding", "tests.records.test_fc2_retained_scope"]
MODES = ("focused", "compatibility", "negative", "portability", "credentials", "public-tree")
ALLOWED_CHANGED = (
    "campaign_tool/records/retained_units.py", "tests/records/test_fc7_search_unicode.py",
    "tools/accept_fc7_search_unicode.py",
)
# Inspected import and child-command closure. No dependency/model loaders occur.
CLOSURE = (
    "campaign_tool/__init__.py", "campaign_tool/records/__init__.py",
    "campaign_tool/records/retained_units.py", "campaign_tool/records/grounded_evidence.py",
    "campaign_tool/records/understanding/__init__.py",
    "campaign_tool/records/understanding/__main__.py",
    "campaign_tool/records/understanding/cli.py",
    "campaign_tool/records/understanding/evidence.py",
    "campaign_tool/records/understanding/document.py",
    "campaign_tool/records/understanding/correspondence.py",
    "campaign_tool/records/understanding/policy.py",
    "campaign_tool/records/understanding/reconcile.py",
    "tests/__init__.py", "tests/records/__init__.py",
    "tests/records/test_understanding.py", "tests/records/test_fc2_retained_scope.py",
    "tests/records/test_fc7_search_unicode.py",
)


def need(ok, message):
    if not ok:
        raise RuntimeError(message)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def record(path, value):
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n")


def inside(path, root):
    return path == root or root in path.parents


def no_symlinks(path):
    need(path.is_absolute() and ".." not in path.parts, "absolute canonical path required")
    need(not any(p.is_symlink() for p in (path, *path.parents)), "symlink path refused")


def command(argv, cwd, evidence, *, binary=False):
    need(all(isinstance(a, str) for a in argv), "literal argv required")
    done = subprocess.run(argv, cwd=cwd, capture_output=True, text=not binary, timeout=180)
    row = {"argv": argv, "cwd": str(cwd), "exit": done.returncode,
           "stdout": done.stdout if not binary else "binary archive saved separately",
           "stderr": done.stderr if not binary else done.stderr.decode("utf-8", "replace")}
    evidence.setdefault("commands", []).append(row)
    return done


def git(root, evidence, *args):
    done = command(["git", *args], root, evidence)
    need(done.returncode == 0, f"git {args!r} failed: {done.stderr}")
    return done.stdout.strip()


def inspect_closure(root, focused=True):
    """Inventory complete admitted local closure before importing any application.

    All application imports are local/stdlib. Any new external or unresolved
    local import fails closed. The only selected subprocess site is run_cli in
    the unchanged understanding test; it is wrapped below for guarded execution.
    """
    paths = set(CLOSURE) - (set() if focused else {ALLOWED_CHANGED[1]})
    inventory = {}
    child_sites = []
    for name in sorted(paths):
        path = root / name
        need(path.is_file() and not path.is_symlink(), f"missing closure file: {name}")
        tree = ast.parse(path.read_text(), filename=str(path))
        module = name[:-3].replace("/", ".")
        package = module.rsplit(".", 1)[0]
        for node in ast.walk(tree):
            imports = []
            if isinstance(node, ast.Import):
                imports = [n.name for n in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = importlib.util.resolve_name("." * node.level + (node.module or ""), package) if node.level else (node.module or "")
                imports = [base] + [base + "." + n.name for n in node.names]
            for imported in imports:
                top = imported.split(".")[0]
                if top in ("campaign_tool", "tests"):
                    local = imported.replace(".", "/")
                    found = next((p for p in (local + ".py", local + "/__init__.py") if (root / p).is_file()), None)
                    # from-import attributes need not be modules; their base was checked.
                    if found:
                        need(found in paths, f"uninspected local dependency: {found}")
                    elif isinstance(node, ast.Import):
                        raise RuntimeError(f"unresolved local import: {imported}")
                else:
                    need(top in sys.stdlib_module_names or top == "__future__", f"external import not admitted: {imported}")
            if isinstance(node, ast.Call):
                text = ast.unparse(node.func)
                if text.startswith(("subprocess.", "os.system", "os.popen", "os.spawn", "os.exec", "pty.", "multiprocessing.", "asyncio.create_subprocess", "concurrent.futures.ProcessPool")):
                    need(name == "tests/records/test_understanding.py" and text == "subprocess.run", f"uninspected child site: {name}:{node.lineno}")
                    child_sites.append({"path": name, "line": node.lineno, "source": ast.unparse(node)})
        inventory[name] = sha(path)
    return {"hashes": inventory, "child_sites": child_sites,
            "redirects": "tempfile.tempdir, TMPDIR/TEMP/TMP set before tests; no source-path defaults in selected closure",
            "limits": "Inspected code only; not a hostile-code sandbox"}


def install_guards(root, temp_root, child_rows, launcher):
    """Process-local guards, installed before application imports including CLI children."""
    temp_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    tempfile.tempdir = str(temp_root)
    for key in ("TMPDIR", "TEMP", "TMP"):
        os.environ[key] = str(temp_root)
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(root))
    original_popen = subprocess.Popen
    original_connect = sqlite3.connect
    permitted_children = []

    def audit(event, args):
        if event.startswith("socket.") or event in ("os.system", "os.fork", "os.forkpty", "os.posix_spawn", "pty.spawn"):
            raise RuntimeError(f"offline sentinel: {event}")
        if event == "subprocess.Popen":
            need(list(args[1]) in permitted_children, "unapproved subprocess")
        if event == "import":
            need(args[0].split(".")[0] not in {"openai", "anthropic", "ollama", "litellm", "hermes_tools", "requests", "httpx"}, "model/network import sentinel")

    sys.addaudithook(audit)

    def no_network(*args, **kwargs):
        raise RuntimeError("offline network sentinel")

    socket.socket = no_network
    socket.create_connection = no_network
    socket.getaddrinfo = no_network

    def guarded_connect(database, *args, **kwargs):
        need(isinstance(database, (str, bytes, os.PathLike)), "unknown database locator")
        raw = os.fsdecode(database)
        if raw != ":memory:":
            if raw.startswith("file:"):
                parts = urlsplit(raw)
                need(not parts.netloc and parts.scheme == "file", "nonlocal database URI")
                raw = unquote(parts.path)
            path = Path(raw)
            no_symlinks(path)
            need(inside(path.resolve(), temp_root.resolve()), "database outside synthetic run")
        return original_connect(database, *args, **kwargs)

    sqlite3.connect = guarded_connect
    sqlite3.dbapi2.connect = guarded_connect

    def guarded_popen(argv, *args, **kwargs):
        # The unchanged tests use exactly this one CLI. Re-execute its real
        # module under the same pre-import guards, retaining original argv/cwd.
        need(not args and not kwargs.get("shell"), "shell/process form not admitted")
        need(isinstance(argv, list) and argv[:4] == [sys.executable, "-B", "-m", "campaign_tool.records.understanding"], "child not admitted")
        need(Path(kwargs.get("cwd", "")).resolve() == root, "child cwd mismatch")
        tail = argv[4:]
        need(len(tail) == 5 and tail[0] in ("policy", "document") and tail[1] == "--input" and tail[3] == "--output", "unexpected CLI arguments")
        for value in (tail[2], tail[4]):
            no_symlinks(Path(value))
            need(inside(Path(value), temp_root), "CLI path outside run")
        child_dir = Path(tempfile.mkdtemp(prefix="cli-", dir=temp_root))
        request = child_dir / "request.json"
        record(request, {"kind": "cli", "root": str(root), "temp_root": str(child_dir / "tmp"),
                         "argv": tail, "result": str(child_dir / "result.json"), "launcher": str(launcher)})
        wrapped = [sys.executable, str(launcher), str(request)]
        permitted_children.append(wrapped)
        row = {"argv": argv, "cwd": str(root), "guarded_argv": wrapped, "request": str(request), "result": str(child_dir / "result.json")}
        child_rows.append(row)
        process = original_popen(wrapped, **kwargs)
        original_communicate = process.communicate
        def communicate(*communicate_args, **communicate_kwargs):
            stdout, stderr = original_communicate(*communicate_args, **communicate_kwargs)
            row.update(exit=process.returncode, stdout=stdout, stderr=stderr)
            return stdout, stderr
        process.communicate = communicate
        return process

    subprocess.Popen = guarded_popen


def flatten(suite):
    if isinstance(suite, unittest.TestSuite):
        return [t for child in suite for t in flatten(child)]
    return [suite]


def run_suite(suite):
    tests = flatten(suite)
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    return {"ids": [t.id() for t in tests], "count": len(tests), "tests_run": result.testsRun,
            "failures": [[t.id(), text] for t, text in result.failures],
            "errors": [[t.id(), text] for t, text in result.errors],
            "skipped": [[t.id(), reason] for t, reason in result.skipped],
            "unexpected_successes": [t.id() for t in result.unexpectedSuccesses],
            "expected_failures": [[t.id(), text] for t, text in result.expectedFailures],
            "stdout": "", "stderr": stream.getvalue(),
            "success": bool(tests) and result.testsRun == len(tests) and result.wasSuccessful()
                       and not result.skipped and not result.expectedFailures}


def worker(request_path):
    request = json.loads(Path(request_path).read_text())
    root, temp_root = Path(request["root"]), Path(request["temp_root"])
    rows = []
    result = {"kind": request["kind"], "success": False, "children": rows}
    try:
        closure = inspect_closure(root, focused=request["kind"] != "cli" and request.get("focused", True))
        install_guards(root, temp_root, rows, Path(request["launcher"]))
        if request["kind"] == "cli":
            sys.argv = ["campaign_tool.records.understanding", *request["argv"]]
            try:
                runpy.run_module("campaign_tool.records.understanding", run_name="__main__")
            except SystemExit as exc:
                result["exit"] = exc.code if isinstance(exc.code, int) else 1
            else:
                result["exit"] = 0
            result["success"] = result["exit"] == 0
        else:
            loader = unittest.TestLoader()
            suite = unittest.TestSuite(loader.loadTestsFromName(n) for n in request["names"])
            need(not loader.errors, f"test loading failed: {loader.errors}")
            if request["kind"] == "negative":
                ru = importlib.import_module("campaign_tool.records.retained_units")
                fixture = importlib.import_module(FOCUSED)
                original = ru.search_index
                # Mutate only the encoding handler, in memory, keeping all
                # other validator/path/pin behavior byte-for-byte unchanged.
                source = (root / ALLOWED_CHANGED[0]).read_text()
                guarded = '    try: query_bytes=query.encode("utf-8")\n    except UnicodeError: _no("search_query_invalid")'
                need(source.count(guarded) == 1, "encoding mutation target changed")
                mutated_source = source.replace(guarded, '    query_bytes=query.encode("utf-8")')
                result["mutation"] = {"scope": "in-memory only; no on-disk production modification",
                                      "before": guarded, "after": '    query_bytes=query.encode("utf-8")',
                                      "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
                                      "mutant_sha256": hashlib.sha256(mutated_source.encode()).hexdigest()}
                namespace = dict(vars(ru))
                exec(compile(mutated_source, str(root / ALLOWED_CHANGED[0]), "exec"), namespace)
                ru.search_index = namespace["search_index"]
                test = fixture.SearchUnicodeTests("test_high_low_and_embedded_surrogates")
                def isolated():
                    test.setUp()
                    try:
                        with test.assertRaises(ru.RetainedUnitError) as caught:
                            test.search("\ud800")
                        test.assertEqual(caught.exception.code, "search_query_invalid")
                    finally:
                        test.doCleanups()
                try:
                    mutant = run_suite(unittest.TestSuite([unittest.FunctionTestCase(isolated, description="isolated surrogate typed-error assertion")]))
                finally:
                    ru.search_index = original
                restored = run_suite(loader.loadTestsFromName(FOCUSED + ".SearchUnicodeTests.test_valid_ascii_and_multibyte_query"))
                result.update(mutant=mutant, restored_positive=restored)
                result["success"] = (mutant["tests_run"] == 1 and len(mutant["errors"]) == 1
                                     and not mutant["failures"] and "UnicodeEncodeError" in mutant["errors"][0][1]
                                     and restored["success"])
            else:
                result.update(run_suite(suite))
            result["closure"] = closure
        result["loaded_modules"] = {n: str(Path(m.__file__).resolve()) for n, m in sorted(sys.modules.items())
                                    if (n == "tests" or n.startswith("tests.") or n == "campaign_tool" or n.startswith("campaign_tool.")) and getattr(m, "__file__", None)}
        need(all(inside(Path(p), root) for p in result["loaded_modules"].values()), "module origin leakage")
    except Exception:
        result["exception"] = traceback.format_exc()
        traceback.print_exc()
        result["success"] = False
    finally:
        record(request["result"], result)
    return 0 if result["success"] else 1


def launch(root, run_dir, label, names, evidence, *, kind="tests", focused=True):
    area = run_dir / label
    area.mkdir(mode=0o700)
    launcher = area / "worker.py"
    # A normal script-file bootstrap; no inline interpreter command or path env.
    launcher.write_text("import importlib.util, sys\nfrom pathlib import Path\n"
                        f"p = Path({str(Path(__file__).resolve())!r})\n"
                        "s = importlib.util.spec_from_file_location('fc7_acceptance', p)\n"
                        "m = importlib.util.module_from_spec(s)\ns.loader.exec_module(m)\n"
                        "raise SystemExit(m.worker(sys.argv[1]))\n")
    request = area / "request.json"
    result_path = area / "result.json"
    record(request, {"kind": kind, "root": str(root), "temp_root": str(area / "tmp"),
                     "names": names, "focused": focused, "result": str(result_path), "launcher": str(launcher)})
    done = command([sys.executable, str(launcher), str(request)], root, evidence)
    need(result_path.is_file(), f"missing worker evidence: {label}")
    result = json.loads(result_path.read_text())
    result["process_exit"] = done.returncode
    # Preserve every guarded CLI child's actual output and final process state.
    for row in result.get("children", []):
        child = Path(row["result"])
        need(child.is_file(), "missing CLI child result")
        row["result_data"] = json.loads(child.read_text())
    record(result_path, result)
    evidence[label] = result
    return result


def archive_baseline(root, run_dir, evidence, *, add_focused=False):
    base = evidence["base_head"]
    target = run_dir / "baseline" / "repo"
    target.mkdir(parents=True)
    done = command(["git", "archive", base], root, evidence, binary=True)
    need(done.returncode == 0, "baseline archive failed")
    archive = run_dir / "baseline.tar"
    archive.write_bytes(done.stdout)
    evidence["baseline_archive"] = {"path": str(archive), "sha256": sha(archive)}
    with tarfile.open(fileobj=io.BytesIO(done.stdout), mode="r:") as bundle:
        bundle.extractall(target, filter="data")
    # Preserve actual checkout context required by unchanged _outside_git test.
    # The marker has no objects/remotes and performs no git operation.
    (target / ".git").mkdir()
    evidence["baseline_checkout_context"] = "synthetic .git directory marker; no baseline source modifications"
    if add_focused:
        shutil.copyfile(root / ALLOWED_CHANGED[1], target / ALLOWED_CHANGED[1])
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", choices=MODES, required=True)
    parser.add_argument("--output-root", required=True)
    args = parser.parse_args()
    sys.dont_write_bytecode = True
    root = Path(__file__).resolve().parents[1]
    out_root = Path(args.output_root)
    no_symlinks(out_root)
    need(inside(out_root, root.parent) and not inside(out_root, root), "output must be candidate-local outside repo")
    out_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix=args.check + "-", dir=out_root))
    evidence = {"contract": CONTRACT, "mode": args.check, "run_dir": str(run_dir),
                "argv": [sys.executable, *sys.argv], "cwd": os.getcwd(), "commands": [], "exit": 1}
    try:
        manifest_path = root.parent / "SOURCE-MANIFEST.json"
        manifest = json.loads(manifest_path.read_text())
        runtime = manifest["runtime"]
        # Operator-owned manifest is the source pin, not a duplicated token in
        # synthetic code. Validate its complete commit/tree binding below.
        base = manifest.get("head")
        need(type(base) is str and len(base) == 40 and set(base) <= set("0123456789abcdef"), "invalid manifest base")
        need(manifest.get("schema") == "agentorange.source-manifest/v1" and manifest.get("card") == "FC7-SEARCH-UNICODE", "manifest schema/card mismatch")
        need(manifest.get("clean") is True and manifest.get("inputs") == [], "unexpected staging/input declarations")
        need(manifest.get("repo") == str(root) and manifest.get("write_root") == str(root.parent), "manifest source identity mismatch")
        need(git(root, evidence, "remote", "get-url", "origin") == manifest["remote"], "remote mismatch")
        need(git(root, evidence, "rev-parse", base + "^{tree}") == manifest["tree"], "base tree mismatch")
        git(root, evidence, "merge-base", "--is-ancestor", base, "HEAD")
        repo_python = root / ".venv/bin/python3"
        # Compare venv prefixes, not only resolved executable (venv links share a binary).
        need((root / ".venv").resolve() == Path(runtime["repo_venv"]).resolve(), "staged venv mismatch")
        need(Path(sys.prefix).resolve() == Path(runtime["repo_venv"]).resolve(), "runtime prefix mismatch")
        need(Path(sys.executable).absolute() in (repo_python.absolute(), Path(runtime["credential_scan_python3"]).absolute()), "unexpected interpreter invocation")
        need(repo_python.is_file(), "staged interpreter unavailable")
        if args.check == "credentials":
            need(Path(sys.executable).absolute() == Path(runtime["credential_scan_python3"]).absolute(), "credential interpreter mismatch")
        paths = set(git(root, evidence, "diff", "--name-only", base).splitlines()) | set(git(root, evidence, "ls-files", "--others", "--exclude-standard").splitlines())
        need(paths <= set(ALLOWED_CHANGED), f"out-of-scope changes: {sorted(paths)}")
        evidence.update(base_head=base, candidate_head=git(root, evidence, "rev-parse", "HEAD"),
                        source_manifest_sha256=sha(manifest_path), runtime={"executable": sys.executable, "prefix": sys.prefix},
                        changed_hashes={p: sha(root / p) for p in ALLOWED_CHANGED}, changed_paths=sorted(paths))
        if args.check in ("focused", "compatibility", "negative", "portability"):
            evidence["closure"] = inspect_closure(root)
        if args.check == "focused":
            baseline = archive_baseline(root, run_dir, evidence, add_focused=True)
            red = launch(baseline, run_dir, "baseline_focused", [FOCUSED], evidence)
            expected = {FOCUSED + ".SearchUnicodeTests.test_" + n for n in CASES}
            need(set(red["ids"]) == expected and red["count"] == 6 and red["tests_run"] == 6 and red["process_exit"] == 1, "baseline focused did not execute exact suite")
            error_ids = {r[0].split(" (")[0] for r in red["errors"]}
            need(error_ids == {FOCUSED + ".SearchUnicodeTests.test_high_low_and_embedded_surrogates", FOCUSED + ".SearchUnicodeTests.test_invalid_before_path_or_sqlite"}
                 and all("UnicodeEncodeError" in r[1] for r in red["errors"]) and not red["failures"] and not red["skipped"], "baseline red was not deciding encoding defect")
            green = launch(root, run_dir, "focused", [FOCUSED], evidence)
            need(green["success"] and green["process_exit"] == 0 and set(green["ids"]) == expected, "focused failed")
        elif args.check == "compatibility":
            baseline = archive_baseline(root, run_dir, evidence)
            before = launch(baseline, run_dir, "baseline_compatibility", COMPAT, evidence, focused=False)
            need(before["success"] and before["process_exit"] == 0, "unexpected baseline compatibility failure; owner disposition required")
            after = launch(root, run_dir, "candidate_compatibility", COMPAT, evidence)
            need(after["success"] and after["process_exit"] == 0 and before["ids"] == after["ids"], "compatibility failed or selectors differ")
            need(all(any(t.startswith(s + ".") for t in before["ids"]) for s in COMPAT), "selector selected no actual tests")
        elif args.check == "negative":
            negative = launch(root, run_dir, "negative", [FOCUSED], evidence, kind="negative")
            need(negative["success"] and negative["process_exit"] == 0, "mutant not detected or restoration failed")
        elif args.check == "portability":
            copy_root = run_dir / "relocated-prefix" / "repo"
            shutil.copytree(root, copy_root, ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__"))
            (copy_root / ".git").mkdir()
            relocated = launch(copy_root, run_dir, "relocated", [FOCUSED], evidence)
            need(relocated["success"] and relocated["process_exit"] == 0 and relocated["count"] == len(CASES), "relocated suite failed")
            need(relocated["loaded_modules"] and all(inside(Path(p), copy_root) for p in relocated["loaded_modules"].values()), "relocated module origin mismatch")
        elif args.check == "credentials":
            scanner = runtime["detect_secrets"]
            need(Path(scanner).is_file(), "installed scanner unavailable")
            report = run_dir / "secret-scan.json"
            scan = command([scanner, "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"], root, evidence)
            report.write_text(scan.stdout)
            need(scan.returncode == 0, "secret scanner failed")
            check = command([runtime["credential_scan_python3"], str(root / "tools/check_secret_report.py"), str(report)], root, evidence)
            evidence["secret_report_sha256"] = sha(report)
            need(check.returncode == 0, "credential report checker failed")
        elif args.check == "public-tree":
            check = command([str(repo_python), str(root / "tools/check_public_tree.py")], root, evidence)
            need(check.returncode == 0, "public tree failed")
            evidence["public_tree"] = json.loads(check.stdout)
            need(evidence["public_tree"]["scanned_files"] > 0, "empty public scan")
        need(evidence["changed_hashes"] == {p: sha(root / p) for p in ALLOWED_CHANGED}, "source changed during acceptance")
        evidence["exit"] = 0
    except Exception:
        evidence["exception"] = traceback.format_exc()
        traceback.print_exc()
    finally:
        record(run_dir / "evidence.json", evidence)
    print(json.dumps({"run_dir": str(run_dir), "exit": evidence["exit"], "mode": args.check, "evidence": str(run_dir / "evidence.json")}, sort_keys=True))
    return evidence["exit"]


if __name__ == "__main__":
    raise SystemExit(main())
