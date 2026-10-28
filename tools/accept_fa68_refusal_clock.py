#!/usr/bin/env python3
"""FA68 offline receipts. A row PASS is never full-card or release acceptance.

Luna authored the original helper; Sol rebuilt its evidence and control paths.
Full discovery remains OWED on the unattended surface: unchanged shared tests
execute inline interpreter children. No selector is skipped to hide that fact.
"""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

CHECKS = ("regression", "compatibility", "control", "portability", "full-suite", "credentials", "public-tree")
BASE = ("2d80de6595" "73f556e42e" "bcbca5313f" "2d18153f88")
TEST = "tests/records/test_fa68_refusal_clock.py"
SOURCE = "campaign_tool/records/portal/store.py"
HELPER = "tools/accept_fa68_refusal_clock.py"
SELECTOR = "tests.records.test_fa68_refusal_clock.Regression"
COMPAT = ("tests.records.test_portal_intake", "tests.test_records_portal",
          "tests.test_records_portal_clock_contract", "tests.test_secret_report", "tests.test_public_tree")

# Written to a real file before execution, never interpreter -c/-e input.
CHILD = r'''import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

repo, output, selector = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve(), sys.argv[3]
sys.path.insert(0, str(repo))
sys.dont_write_bytecode = True
tempfile.tempdir = os.environ["TMPDIR"]

def offline(event, args):
    if event.startswith("socket."):
        raise RuntimeError("FA68 offline network sentinel")
    if event == "import" and str(args[0]).split(".")[0] in {
            "openai", "anthropic", "ollama", "transformers", "requests", "httpx"}:
        raise RuntimeError("FA68 model/network import sentinel")
    if event == "subprocess.Popen":
        argv = args[1]
        allowed = (selector == "tests.test_secret_report" and len(argv) == 3
                   and argv[0] == sys.executable
                   and Path(argv[1]).resolve() == repo / "tools/check_secret_report.py"
                   and Path(argv[2]).resolve().is_relative_to(Path(tempfile.tempdir)))
        if not allowed:
            raise RuntimeError("FA68 offline child sentinel")
    if event in {"os.system", "os.exec", "os.posix_spawn", "os.fork", "pty.spawn"}:
        raise RuntimeError("FA68 offline process sentinel")

# This audit hook is a scoped sentinel, not a hostile-code sandbox. All local
# imports have been statically inspected before this child is invoked. The
# portal closure is definition-only, with no live roots/bound live defaults.
sys.addaudithook(offline)

class Result(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.events = []
    def addSuccess(self, test):
        self.events.append({"id": test.id(), "outcome": "PASS"})
        super().addSuccess(test)
    def addFailure(self, test, err):
        self.events.append({"id": test.id(), "outcome": "FAIL", "exception": err[0].__name__})
        super().addFailure(test, err)
    def addError(self, test, err):
        self.events.append({"id": test.id(), "outcome": "ERROR", "exception": err[0].__name__})
        super().addError(test, err)
    def addSubTest(self, test, subtest, err):
        self.events.append({"id": subtest.id(), "outcome": "PASS" if err is None else "FAIL",
                            "exception": None if err is None else err[0].__name__})
        super().addSubTest(test, subtest, err)

suite = unittest.defaultTestLoader.loadTestsFromName(selector)
result = unittest.TextTestRunner(verbosity=2, resultclass=Result).run(suite)
loaded = {}
for name, module in tuple(sys.modules.items()):
    path = getattr(module, "__file__", None)
    if path and Path(path).resolve().is_relative_to(repo):
        actual = Path(path).resolve()
        loaded[name] = {"path": str(actual), "sha256": hashlib.sha256(actual.read_bytes()).hexdigest()}
fixture = sys.modules.get("tests.records.test_fa68_refusal_clock")
data = {"count": result.testsRun, "failures": [t.id() for t, _ in result.failures],
        "errors": [t.id() for t, _ in result.errors],
        "skips": [[t.id(), reason] for t, reason in result.skipped],
        "unexpected_successes": [t.id() for t in result.unexpectedSuccesses],
        "expected_failures": [t.id() for t, _ in result.expectedFailures],
        "events": result.events, "loaded_modules": loaded, "interpreter": sys.executable,
        "fixture_sha256": None if fixture is None else hashlib.sha256(fixture.FIXTURE_BYTES).hexdigest()}
output.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
sys.exit(0 if result.wasSuccessful() and result.testsRun > 0 else 1)
'''


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def fixture_digest(repo):
    tree = ast.parse((repo / TEST).read_text())
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "FIXTURE_BYTES"
                and isinstance(node.value, ast.Constant) and isinstance(node.value.value, bytes)):
            return hashlib.sha256(node.value.value).hexdigest()
    raise ValueError("labelled synthetic fixture bytes missing")


def git(repo, *args):
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, check=False)
    if result.returncode:
        raise ValueError("git evidence lookup failed: " + repr(args))
    return result.stdout


def source_map(repo):
    files = set(git(repo, "ls-files", "-z").decode().split("\0")) - {""}
    files.update((SOURCE, TEST, HELPER))
    result = {}
    for rel in sorted(files):
        path = repo / rel
        if path.is_symlink() or not path.is_file():
            raise ValueError("source map requires regular tracked files: " + rel)
        result[rel] = digest(path)
    return result


def local_module(repo, name):
    path = repo.joinpath(*name.split("."))
    for candidate in (path.with_suffix(".py"), path / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def inspect_closure(repo, selector):
    """Record every statically named local import, including package initializers.

    Dynamic imports are not asserted safe by this inventory. The admitted portal
    and compatibility paths are definition-only at import and use synthetic
    injected collaborators at execution; the child sentinel remains in place.
    """
    todo = [selector]
    seen = {}
    while todo:
        name = todo.pop()
        path = local_module(repo, name)
        if path is None:
            if "." in name:
                todo.append(name.rsplit(".", 1)[0])
            continue
        rel = path.relative_to(repo).as_posix()
        if rel in seen:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        seen[rel] = digest(path)
        parts = name.split(".")
        todo.extend(".".join(parts[:index]) for index in range(1, len(parts)))
        package = name if path.name == "__init__.py" else name.rpartition(".")[0]
        if package:
            todo.append(package)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                todo.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    prefix = package.split(".")[:len(package.split(".")) - node.level + 1]
                    module = ".".join(prefix + ([node.module] if node.module else []))
                else:
                    module = node.module or ""
                if module:
                    todo.append(module)
                    todo.extend(module + "." + alias.name for alias in node.names if alias.name != "*")
    return seen


def raw_run(root, name, argv, cwd, env):
    stdout = root / (name + ".stdout.txt")
    stderr = root / (name + ".stderr.txt")
    # Stream to retained files even if an outer worker is interrupted.
    with stdout.open("x") as out, stderr.open("x") as err:
        result = subprocess.run(argv, cwd=cwd, env=env, stdout=out, stderr=err, check=False)
    return {"argv": argv, "cwd": str(cwd), "exit": result.returncode,
            "stdout": str(stdout), "stdout_sha256": digest(stdout),
            "stderr": str(stderr), "stderr_sha256": digest(stderr)}


def test_run(root, name, repo, selector, env, python):
    closure = inspect_closure(repo, selector)
    (root / (name + ".import-closure.json")).write_text(json.dumps(closure, indent=2, sort_keys=True) + "\n")
    child = root / (name + ".child.py")
    child.write_text(CHILD)
    metadata = root / (name + ".tests.json")
    result = raw_run(root, name, [python, str(child), str(repo), str(metadata), selector], repo, env)
    result["import_closure"] = closure
    result["child_sha256"] = digest(child)
    if metadata.is_file():
        result.update(json.loads(metadata.read_text()))
        result["metadata"] = str(metadata)
        result["metadata_sha256"] = digest(metadata)
    else:
        result.update(count=0, failures=[], errors=["child_metadata_missing"], skips=[])
    result["passed"] = (result["exit"] == 0 and result["count"] > 0
                        and not result["failures"] and not result["errors"]
                        and not result.get("unexpected_successes"))
    return result


def copy_source(repo, destination, baseline=False):
    destination.mkdir(mode=0o700)
    names = source_map(repo)
    for rel in names:
        target = destination / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if baseline and rel not in (TEST, HELPER):
            target.write_bytes(git(repo, "show", BASE + ":" + rel))
        else:
            shutil.copyfile(repo / rel, target)
    return names


def validate_manifest(repo, manifest, python, check):
    if manifest.get("schema") != "agentorange.source-manifest/v1":
        raise ValueError("invalid source manifest schema")
    if manifest.get("head") != BASE or manifest.get("repo") != str(repo):
        raise ValueError("source manifest base/repo mismatch")
    if manifest.get("clean") is not True or manifest.get("card") != "FA68-REFUSAL-CLOCK":
        raise ValueError("manifest admission missing")
    if manifest.get("tree") != git(repo, "rev-parse", BASE + "^{tree}").decode().strip():
        raise ValueError("manifest base tree mismatch")
    git(repo, "merge-base", "--is-ancestor", BASE, "HEAD")
    runtime = manifest.get("runtime", {})
    for name in ("credential_scan_python3", "detect_secrets"):
        if not isinstance(runtime.get(name), str) or not Path(runtime[name]).is_file():
            raise ValueError("missing named staged runtime: " + name)
    expected = runtime["credential_scan_python3"] if check == "credentials" else str(repo / ".venv/bin/python3")
    if os.path.abspath(python) != os.path.abspath(expected):
        raise ValueError("wrong invocation interpreter")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", required=True, choices=CHECKS)
    parser.add_argument("--source-manifest", required=True)
    parser.add_argument("--output-root", required=True)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    manifest_path = Path(args.source_manifest).resolve()
    manifest = json.loads(manifest_path.read_text())
    validate_manifest(repo, manifest, sys.executable, args.check)
    output = Path(args.output_root).resolve()
    if output.is_relative_to(repo):
        raise ValueError("outputs must be outside the repository")
    output.mkdir(parents=True, exist_ok=True)
    root = output / (args.check + "-" + uuid.uuid4().hex)
    root.mkdir(mode=0o700)
    temp = root / "tmp"
    temp.mkdir(mode=0o700)
    os.environ["TMPDIR"] = str(temp)
    tempfile.tempdir = str(temp)
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    before = source_map(repo)
    receipt = {"schema": "fa68.acceptance/v2", "check": args.check,
               "scope": "offline-row-only; full discovery and shipping CI remain owed",
               "status": "REVIEW REQUIRED", "manifest": str(manifest_path),
               "manifest_sha256": digest(manifest_path), "base": BASE,
               "base_tree": manifest["tree"], "candidate_head": git(repo, "rev-parse", "HEAD").decode().strip(),
               "candidate_tree": git(repo, "rev-parse", "HEAD^{tree}").decode().strip(),
               "dirty": git(repo, "status", "--porcelain").decode(),
               "source_hashes": before, "fixture_sha256": fixture_digest(repo),
               "interpreter": sys.executable, "interpreter_realpath": str(Path(sys.executable).resolve()),
               "loaded_module_paths": {}, "runs": [], "result": "FAIL"}
    good = False
    try:
        if args.check == "regression":
            result = test_run(root, "regression", repo, SELECTOR, env, sys.executable)
            receipt["runs"].append(result)
            good = result["passed"] and not result["skips"]
        elif args.check == "compatibility":
            baseline = root / "pinned-baseline"
            copy_source(repo, baseline, baseline=True)
            comparisons = []
            for index, selector in enumerate(COMPAT):
                old = test_run(root, "baseline-" + str(index), baseline, selector, env, sys.executable)
                new = test_run(root, "candidate-" + str(index), repo, selector, env, sys.executable)
                receipt["runs"].extend((old, new))
                comparisons.append(old["passed"] and new["passed"] and old["count"] == new["count"]
                                   and old["skips"] == new["skips"]
                                   and old.get("expected_failures") == new.get("expected_failures"))
            receipt["baseline_comparisons"] = comparisons
            good = bool(comparisons) and all(comparisons)
        elif args.check == "control":
            mutated = root / "gate-only-mutation"
            copy_source(repo, mutated)
            source = (repo / SOURCE).read_text()
            original = git(repo, "show", BASE + ":" + SOURCE).decode()
            start = source.index("        clock = now if now is not None else time.time()", source.index("    def refuse("))
            end = source.index("        with self.lock(), self.db:", start)
            unguarded = "        stamp = float(now if now is not None else time.time())\n"
            broken = source[:start] + unguarded + source[end:]
            if broken != original:
                raise ValueError("gate-only mutation differs from pinned baseline")
            (mutated / SOURCE).write_text(broken)
            receipt["mutation"] = {"path": SOURCE, "sha256": digest(mutated / SOURCE),
                                   "baseline_equal": True, "changed_gate_only": True}
            negative = test_run(root, "unguarded", mutated, "tests.records.test_fa68_refusal_clock.GateControl", env, sys.executable)
            restored = test_run(root, "restored", repo, "tests.records.test_fa68_refusal_clock.GateControl", env, sys.executable)
            positive = test_run(root, "restored-regression", repo, SELECTOR, env, sys.executable)
            receipt["runs"].extend((negative, restored, positive))
            expected = {"tests.records.test_fa68_refusal_clock.GateControl.test_boolean_is_typed_refusal",
                        "tests.records.test_fa68_refusal_clock.GateControl.test_infinity_is_typed_refusal"}
            finite = "tests.records.test_fa68_refusal_clock.GateControl.test_finite_controls"
            detected = (negative["exit"] == 1 and negative["count"] == 3
                        and set(negative["failures"]) == expected and not negative["errors"]
                        and not negative["skips"] and any(e["id"] == finite and e["outcome"] == "PASS"
                                                          for e in negative.get("events", [])))
            receipt["negative_control_detected"] = detected
            good = detected and restored["passed"] and positive["passed"] and not restored["skips"] and not positive["skips"]
        elif args.check == "portability":
            copied = root / "different-prefix" / "source-copy"
            copied.parent.mkdir()
            copy_source(repo, copied)
            violations = []
            for rel in (TEST, HELPER):
                text = (repo / rel).read_text()
                for forbidden in (os.sep.join(("", "home", "user")), "-".join(("agent", "work"))):
                    if forbidden in text:
                        violations.append(rel)
            result = test_run(root, "portability", copied, SELECTOR, env, sys.executable)
            receipt["runs"].append(result)
            receipt["portability_source_scan_violations"] = violations
            receipt["copy_prefix"] = str(copied)
            actual = result.get("loaded_modules", {}).get("campaign_tool.records.portal.store", {}).get("path")
            good = result["passed"] and not result["skips"] and not violations and actual == str(copied / SOURCE)
        elif args.check == "full-suite":
            receipt["owed_argv"] = [str(repo / ".venv/bin/python3"), "-m", "unittest", "discover", "-s", "tests", "-t", "."]
            receipt["owed_cwd"] = str(repo)
            receipt["not_executed"] = "UNCHANGED_INLINE_CHILDREN_REQUIRE_EXISTING_ADMISSION_OWNER"
            # Do not filter discovery, run forbidden children, or claim a pass.
        elif args.check == "credentials":
            runtime = manifest["runtime"]
            scanned = raw_run(root, "scan", [runtime["detect_secrets"], "scan", "--all-files",
                                             "--exclude-files", "^.git/", "--no-verify"], repo, env)
            receipt["runs"].append(scanned)
            report = root / "secret-scan.json"
            report.write_bytes(Path(scanned["stdout"]).read_bytes())
            report_data = json.loads(report.read_text())
            if not isinstance(report_data.get("results"), dict):
                raise ValueError("malformed real scanner report")
            receipt["scan_report"] = {"path": str(report), "sha256": digest(report)}
            checked = raw_run(root, "secret-report", [runtime["credential_scan_python3"],
                               str(repo / "tools/check_secret_report.py"), str(report)], repo, env)
            receipt["runs"].append(checked)
            good = scanned["exit"] == 0 and checked["exit"] == 0 and "potential secrets: 0;" in Path(checked["stdout"]).read_text()
        elif args.check == "public-tree":
            result = raw_run(root, "public-tree", [sys.executable, str(repo / "tools/check_public_tree.py")], repo, env)
            receipt["runs"].append(result)
            good = result["exit"] == 0
        for result in receipt["runs"]:
            receipt["loaded_module_paths"].update(result.get("loaded_modules", {}))
            if result.get("fixture_sha256") and result["fixture_sha256"] != receipt["fixture_sha256"]:
                raise ValueError("loaded fixture bytes mismatch")
        receipt["preserved_source_bytes"] = source_map(repo) == before
        good = good and receipt["preserved_source_bytes"] and bool(receipt["runs"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        receipt["error"] = type(exc).__name__ + ": " + str(exc)
        good = False
    receipt["result"] = "PASS" if good else "FAIL"
    receipt["finished_unix"] = time.time()
    path = root / "receipt.json"
    path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"result": receipt["result"], "receipt": str(path),
                      "runs": [{k: r[k] for k in ("exit", "count", "failures", "errors", "skips") if k in r}
                               for r in receipt["runs"]]}))
    return 0 if good else 1


if __name__ == "__main__":
    raise SystemExit(main())
