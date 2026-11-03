"""Offline, synthetic FC7-THREAD-CYCLES acceptance; success still requires review.

The guards cover the inspected selected closure, not arbitrary hostile code.
Only six public checks are supported. Evidence is never reused or overwritten.
"""
import argparse
import ast
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import importlib
import importlib.abc
import importlib.machinery
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import traceback
import unittest
from urllib.parse import unquote, urlsplit

REPO = Path(__file__).resolve().parents[1]
CONTRACT = "FC7-THREAD-CYCLES/A1-A6/v1"
PATHS = ("campaign_tool/records/correspondence.py", "tests/records/test_fc7_thread_cycles.py",
         "tools/accept_fc7_thread_cycles.py")
FOCUSED = "tests.records.test_fc7_thread_cycles"
COMPATIBILITY = "tests.records.test_correspondence"
CHECKS = ("focused", "compatibility", "negative", "portability", "credentials", "public-tree")


def require(condition, reason):
    if not condition:
        raise RuntimeError(reason)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, data):
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(data, stream, indent=2, sort_keys=True)
        stream.write("\n")


def plain_path(path):
    path = Path(os.path.abspath(path))
    require(not any(p.is_symlink() for p in (path, *path.parents)), "symlink_output_path")
    return path


def within(path, root):
    return path == root or root in path.parents


def closure(repo):
    """Resolve all local imports, including package initializers and literal dynamic imports.

    Effect inventory includes deferred functions: a recorded effect is not permission
    to call it. Selected tests do not call extraction, model, or real IMAP transports.
    """
    seeds = [FOCUSED, COMPATIBILITY, "tools.check_public_tree", "tools.check_secret_report"]
    found = {}
    while seeds:
        name = seeds.pop()
        if name in found:
            continue
        stem = repo.joinpath(*name.split("."))
        path = next((p for p in (stem.with_suffix(".py"), stem / "__init__.py") if p.is_file()), None)
        if path is None:
            continue
        text = path.read_text()
        tree = ast.parse(text)
        package = name if path.name == "__init__.py" else name.rpartition(".")[0]
        imports, effects, defaults, top = [], [], [], []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                prefix = node.module or ""
                if node.level:
                    parts = package.split(".")
                    prefix = ".".join(parts[:len(parts) - node.level + 1] + ([prefix] if prefix else []))
                imports.append(prefix)
                imports.extend(prefix + "." + a.name for a in node.names)
            elif isinstance(node, ast.Call):
                fn = ast.unparse(node.func)
                if fn == "__import__" and node.args and isinstance(node.args[0], ast.Constant):
                    imports.append(node.args[0].value)
                if any(s in fn.lower() for s in ("subprocess", "popen", "system", "spawn", "fork", "pty",
                       "multiprocessing", "processpool", "connect", "socket", "urlopen", "model",
                       "getenv", "environ", "path.home", "create_subprocess")):
                    effects.append({"line": node.lineno, "call": ast.get_source_segment(text, node)})
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                values = node.args.defaults + [v for v in node.args.kw_defaults if v is not None]
                defaults.extend({"function": node.name, "value": ast.unparse(v)} for v in values)
        for node in tree.body:
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.Expr)) and any(
                    isinstance(v, ast.Call) for v in ast.walk(node)):
                top.append(ast.get_source_segment(text, node))
        found[name] = {"path": str(path.relative_to(repo)), "sha256": digest(path),
                       "imports": imports, "effect_calls": effects, "defaults": defaults, "top_level": top}
        for imported in imports:
            seeds.extend(".".join(imported.split(".")[:i]) for i in range(1, len(imported.split(".")) + 1))
    return found


def guards(run):
    """Install before application imports; all databases and temp defaults are synthetic.

    No subprocess is allowed inside tests. FakeIMAP and synthetic_email are imported
    as helpers, not selected as extra suites. Repo-derived resource paths need no
    redirection; no selected closure constant/default targets a live data directory.
    """
    temporary = run / "temporary"
    temporary.mkdir(mode=0o700)
    tempfile.tempdir = str(temporary)
    for key in ("TMPDIR", "TEMP", "TMP"):
        os.environ[key] = str(temporary)
    sys.dont_write_bytecode = True
    events = []

    def audit(event, args):
        if event.startswith("socket.") or event in ("subprocess.Popen", "os.system", "os.fork", "os.forkpty",
                                                    "os.posix_spawn", "os.spawn", "pty.spawn"):
            events.append({"event": event, "disposition": "REFUSED"})
            raise RuntimeError("offline_test_effect_refused:" + event)
        if event == "sqlite3.connect":
            value = os.fsdecode(args[0])
            if value != ":memory:":
                if value.startswith("file:"):
                    parsed = urlsplit(value)
                    require(not parsed.netloc, "database_uri_authority_refused")
                    value = unquote(parsed.path)
                path = plain_path(value)
                require(within(path, temporary), "database_outside_synthetic_run")
            events.append({"event": event, "database": value, "disposition": "synthetic"})
    sys.addaudithook(audit)

    def refuse_model(*args, **kwargs):
        events.append({"event": "model", "disposition": "REFUSED"})
        raise RuntimeError("offline_model_refused")

    class ModelLoader(importlib.abc.Loader):
        def __init__(self, original):
            self.original = original

        def create_module(self, spec):
            return self.original.create_module(spec)

        def exec_module(self, module):
            self.original.exec_module(module)
            for name in ("chat_json", "narrative", "_strict_local_post", "_connect_local", "_local_addresses"):
                setattr(module, name, refuse_model)

    class ModelFinder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname == "campaign_tool.digest.model":
                spec = importlib.machinery.PathFinder.find_spec(fullname, path)
                require(spec is not None, "model_module_missing")
                spec.loader = ModelLoader(spec.loader)
                return spec
            return None
    require(not any(n.startswith("campaign_tool") for n in sys.modules), "application_imported_before_guards")
    sys.meta_path.insert(0, ModelFinder())
    return events


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


def tests(selector, run, label):
    suite = unittest.defaultTestLoader.loadTestsFromName(selector)
    ids = [test.id() for test in flatten(suite)]
    require(ids and not unittest.defaultTestLoader.errors, "missing_or_zero_tests")
    require(len(ids) == len(set(ids)), "duplicate_selected_tests")
    stream, stdout, stderr = io.StringIO(), io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    text = stream.getvalue()
    (run / (label + ".stdout")).write_text(stdout.getvalue())
    (run / (label + ".stderr")).write_text(stderr.getvalue() + text)
    passed = result.wasSuccessful() and not result.skipped and not result.expectedFailures and result.testsRun == len(ids)
    record = {"selector": selector, "ids": ids, "count": len(ids), "tests_run": result.testsRun,
              "passed": passed, "exit": 0 if passed else 1,
              "failures": [{"id": t.id(), "traceback": trace} for t, trace in result.failures],
              "errors": [{"id": t.id(), "traceback": trace} for t, trace in result.errors],
              "skipped": result.skipped, "expected_failures": len(result.expectedFailures),
              "stdout_sha256": digest(run / (label + ".stdout")),
              "stderr_sha256": digest(run / (label + ".stderr"))}
    save(run / (label + ".json"), record)
    return record


def testing_main(repo, run, mode):
    repo, run = Path(repo).resolve(), Path(run).resolve()
    sys.path.insert(0, str(repo))
    save(run / "closure.json", closure(repo))
    events = guards(run)
    try:
        if mode in ("focused", "compatibility"):
            result = tests(FOCUSED if mode == "focused" else COMPATIBILITY, run, mode)
            if mode == "focused":
                module = importlib.import_module(FOCUSED)
                expected = {FOCUSED + ".ThreadCycleTests.test_" + c for c in module.CASE_IDS}
                require(set(result["ids"]) == expected, "frozen_case_ids_missing")
            require(result["passed"], "selected_tests_failed")
        elif mode == "negative":
            module = importlib.import_module(FOCUSED)
            production = module.correspondence
            rows = [module.message("a", "b"), module.message("b", "a")]
            original = production._cycle_edges
            try:
                production._cycle_edges = lambda edges: set()
                require(module.project(rows) == ([module.candidate("a", "b"), module.candidate("b", "a")], []),
                        "mutant_did_not_retain_both_candidate_edges")
                mutant = tests(FOCUSED + ".ThreadCycleTests.test_two_and_three_message_cycles", run, "mutant")
                require(mutant["count"] == 1 and len(mutant["failures"]) == 1 and not mutant["errors"]
                        and not mutant["skipped"] and "AssertionError" in mutant["failures"][0]["traceback"],
                        "deciding_mutant_not_detected")
            finally:
                production._cycle_edges = original
            require(module.project(rows) == ([], [module.gap("a"), module.gap("b")]), "cycle_filter_not_restored")
            for label, case in (("restored", "two_and_three_message_cycles"),
                                ("ordinary", "ordinary_chain_and_independent_chain")):
                require(tests(FOCUSED + ".ThreadCycleTests.test_" + case, run, label)["passed"], "restored_control_failed")
        else:
            raise RuntimeError("unknown_test_mode")
    finally:
        save(run / "guard-events.json", events)
        save(run / "loaded-modules.json", {name: str(Path(mod.__file__).resolve()) for name, mod in sys.modules.items()
             if (name.startswith("campaign_tool") or name.startswith("tests.records")) and getattr(mod, "__file__", None)})
        require(not any(e.get("disposition") == "REFUSED" for e in events), "unexpected_guard_refusal")
        require(all(within(Path(mod.__file__).resolve(), repo) for name, mod in sys.modules.items()
                    if (name.startswith("campaign_tool") or name.startswith("tests.records")) and getattr(mod, "__file__", None)),
                "application_module_outside_selected_copy")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", choices=CHECKS, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    root = plain_path(args.output_root)
    require(not within(root, REPO), "evidence_must_be_outside_repo")
    manifest_path = REPO.parent / "SOURCE-MANIFEST.json"
    manifest = json.loads(manifest_path.read_text())
    candidate_root = plain_path(manifest["write_root"])
    require(within(root, candidate_root), "output_outside_candidate")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix=args.check + "-", dir=root))
    os.umask(0o077)
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    sys.dont_write_bytecode = True
    receipt = {"contract": CONTRACT, "check": args.check, "argv": [sys.executable, *sys.argv],
               "cwd": str(Path.cwd()), "repo": str(REPO), "run": str(run), "status": "FAIL",
               "source_manifest_sha256": digest(manifest_path), "commands": [], "exit": 1}

    def command(argv, label, cwd=REPO):
        require(all(isinstance(x, str) for x in argv), "nonliteral_child_argv")
        try:
            result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=180)
            stdout, stderr, exit_code = result.stdout, result.stderr, result.returncode
        except (OSError, subprocess.TimeoutExpired) as error:
            stdout = getattr(error, "stdout", "") or ""
            stderr = getattr(error, "stderr", "") or ""
            stdout = stdout.decode(errors="replace") if isinstance(stdout, bytes) else stdout
            stderr = stderr.decode(errors="replace") if isinstance(stderr, bytes) else stderr
            stderr += "\n" + str(error)
            exit_code = None
        (run / (label + ".stdout")).write_text(stdout)
        (run / (label + ".stderr")).write_text(stderr)
        receipt["commands"].append({"argv": argv, "cwd": str(cwd), "exit": exit_code,
                                     "stdout_sha256": digest(run / (label + ".stdout")),
                                     "stderr_sha256": digest(run / (label + ".stderr")), "label": label})
        require(exit_code == 0, "child_failed:" + label)
        return stdout

    try:
        require(manifest["schema"] == "agentorange.source-manifest/v1" and manifest["card"] == "FC7-THREAD-CYCLES",
                "manifest_identity_mismatch")
        require(Path(manifest["repo"]) == REPO and Path.cwd().resolve() == REPO, "repo_or_cwd_mismatch")
        require(command(["git", "remote", "get-url", "origin"], "remote").strip() == manifest["remote"], "remote_mismatch")
        base = manifest["head"]
        require(command(["git", "rev-parse", base + "^{tree}"], "base-tree").strip() == manifest["tree"], "base_tree_mismatch")
        command(["git", "merge-base", "--is-ancestor", base, "HEAD"], "base-ancestor")
        receipt["base_head"] = base
        receipt["candidate_head"] = command(["git", "rev-parse", "HEAD"], "head").strip()
        receipt["source_hashes"] = {name: digest(REPO / name) for name in PATHS}
        receipt["git_status"] = command(["git", "status", "--porcelain"], "status")
        changed = set(command(["git", "diff", "--name-only", base], "changed").splitlines())
        changed.update(command(["git", "ls-files", "--others", "--exclude-standard"], "untracked").splitlines())
        require(changed <= set(PATHS), "outside_write_set_change")
        runtime = manifest["runtime"]
        require((REPO / ".venv").resolve() == Path(runtime["repo_venv"]), "staged_runtime_mismatch")
        expected_python = (Path(runtime["credential_scan_python3"]) if args.check == "credentials"
                           else REPO / ".venv" / "bin" / "python3")
        require(Path(sys.executable).absolute() == expected_python.absolute(), "wrong_literal_interpreter")
        require(sys.prefix == str(Path(runtime["repo_venv"]).resolve()) or
                Path(sys.prefix).resolve() == Path(runtime["repo_venv"]).resolve(), "wrong_runtime_prefix")
        save(run / "closure.json", closure(REPO))
        if args.check in ("focused", "compatibility", "negative"):
            # No subprocess may be started after the test guard is installed.
            test_run = run / "test-evidence"
            test_run.mkdir(mode=0o700)
            testing_main(REPO, test_run, args.check)
        elif args.check == "portability":
            copied = run / "second-prefix" / "repo"
            copied.mkdir(parents=True, mode=0o700)
            names = command(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], "copy-inventory").split("\0")
            for relative in sorted(set(names) - {""}):
                source = REPO / relative
                require(not source.is_symlink() and source.is_file(), "copy_source_not_regular")
                destination = copied / relative
                require(within(destination.resolve(), copied), "copy_escape")
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
            copied_run = run / "copied-test-evidence"
            copied_run.mkdir(mode=0o700)
            bootstrap = run / "portability_worker.py"
            bootstrap.write_text("import importlib.util\nfrom pathlib import Path\n"
                                 "root = Path(__file__).resolve().parent\n"
                                 "repo = root / 'second-prefix' / 'repo'\n"
                                 "spec = importlib.util.spec_from_file_location('fc7_accept', repo / 'tools' / 'accept_fc7_thread_cycles.py')\n"
                                 "module = importlib.util.module_from_spec(spec)\nspec.loader.exec_module(module)\n"
                                 "module.testing_main(repo, root / 'copied-test-evidence', 'focused')\n")
            command([sys.executable, str(bootstrap)], "portability", cwd=copied)
            loaded = json.loads((copied_run / "loaded-modules.json").read_text())
            require(loaded and all(within(Path(p), copied) for p in loaded.values()), "portability_loaded_original")
            require(all(digest(REPO / name) == digest(copied / name) for name in PATHS), "copy_hash_mismatch")
        elif args.check == "credentials":
            scanner = Path(runtime["detect_secrets"])
            require(scanner.is_file() and os.access(scanner, os.X_OK), "scanner_unavailable")
            receipt["scanner_sha256"] = digest(scanner)
            raw = command([str(scanner), "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"], "secret-scan")
            (run / "secret-scan.json").write_text(raw)
            command([sys.executable, str(REPO / "tools/check_secret_report.py"), str(run / "secret-scan.json")], "secret-check")
        else:
            command([sys.executable, str(REPO / "tools/check_public_tree.py")], "public-tree")
        require(receipt["source_hashes"] == {name: digest(REPO / name) for name in PATHS}, "source_changed_during_acceptance")
        receipt.update(status="REVIEW_REQUIRED", exit=0)
    except Exception as error:
        receipt["error"] = str(error)
        (run / "failure.stderr").write_text(traceback.format_exc())
    finally:
        save(run / "receipt.json", receipt)
    print(json.dumps({"status": receipt["status"], "receipt": str(run / "receipt.json"), "error": receipt.get("error")}))
    return receipt["exit"]


if __name__ == "__main__":
    raise SystemExit(main())
