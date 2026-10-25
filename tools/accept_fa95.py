#!/usr/bin/env python3
"""File-launched, offline FA95 acceptance; every invocation retains fresh evidence."""
import argparse
import ast
import hashlib
import importlib.abc
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
MANIFEST = REPO.parent / "SOURCE-MANIFEST.json"
SOURCE = "campaign_tool/records/portal/collections.py"
OWNED = (SOURCE, "tests/records/test_fa95.py", "tools/accept_fa95.py")
COMPATIBILITY = (
    "tests.records.test_export_bridge.ExportBridgeTests.test_scope_drift_and_page_source_corruption_rejected",
    "tests.records.test_export_bridge.ExportBridgeTests.test_reopen_scope_guard_precedes_page_replay_discovery_and_readiness",
    "tests.records.test_export_bridge.ExportBridgeTests.test_shared_collection_cannot_be_populated_by_a_different_event_plan",
)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def git(*args):
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True, check=True).stdout


def identity(manifest):
    base = manifest["head"]
    if Path(sys.executable).parent.parent.resolve() != Path(manifest["runtime"]["repo_venv"]).resolve():
        raise ValueError("launcher is not using the admitted staged interpreter")
    if git("rev-parse", base + "^{tree}").decode().strip() != manifest["tree"]:
        raise ValueError("source manifest base tree mismatch")
    git("merge-base", "--is-ancestor", base, "HEAD")
    return {
        "head": git("rev-parse", "HEAD").decode().strip(),
        "base_head": base,
        "source_manifest_sha256": sha(MANIFEST.read_bytes()),
        "diff_sha256": sha(git("diff", "--binary", base)),
        "dirty_diff_sha256": sha(git("diff", "--binary", "HEAD")),
        "status": git("status", "--porcelain").decode(),
        "file_sha256": {name: sha((REPO / name).read_bytes()) for name in OWNED},
        "fixture_sha256": sha((REPO / "tests/records/test_export_bridge.py").read_bytes()),
        "interpreter": sys.executable,
        "interpreter_realpath": str(Path(sys.executable).resolve()),
        "python_version": sys.version,
    }


class OfflineDefaults(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Rewrite only operational OCR defaults BEFORE executing the imported file.

    These selectors never call OCR; this also prevents a future bound-default
    capture from selecting installed binaries or a model endpoint accidentally.
    """
    target = "campaign_tool.records.ocr_stage"

    def __init__(self, run):
        self.values = {"DEFAULT_OCRMYPDF": str(run / "disabled-ocr"),
                       "DEFAULT_PDFTOPPM": str(run / "disabled-render"),
                       "DEFAULT_VISION_ENDPOINT": "http://127.0.0.1:1/offline-disabled"}

    def find_spec(self, fullname, path=None, target=None):
        if fullname == self.target:
            return importlib.util.spec_from_file_location(fullname, REPO / "campaign_tool/records/ocr_stage.py", loader=self)
        return None

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        tree = ast.parse(Path(module.__file__).read_text(), filename=module.__file__)
        found = set()
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                name = node.targets[0].id
                if name in self.values:
                    node.value = ast.Constant(self.values[name])
                    found.add(name)
        if found != set(self.values):
            raise RuntimeError("offline default coverage changed")
        exec(compile(ast.fix_missing_locations(tree), module.__file__, "exec"), module.__dict__)


def isolate(run):
    sys.dont_write_bytecode = True
    os.chdir(REPO)
    sys.path.insert(0, str(REPO))
    temporary = run / "tmp"
    temporary.mkdir()
    tempfile.tempdir = str(temporary)

    def denied(*args, **kwargs):
        raise RuntimeError("offline acceptance: operation disabled")

    socket.socket.connect = denied
    socket.socket.connect_ex = denied
    socket.socket.bind = denied
    socket.socket.sendto = denied
    socket.create_connection = denied
    socket.getaddrinfo = denied
    # The selected synthetic tests need no children, OCR or model process.
    subprocess.Popen = denied
    os.system = denied
    sys.meta_path.insert(0, OfflineDefaults(run))
    from campaign_tool.records import ocr_stage
    if ocr_stage.OcrmypdfBackend.__init__.__defaults__[0] != str(run / "disabled-ocr"):
        raise RuntimeError("OCR bound default not isolated")
    defaults = ocr_stage.QwenVisionBackend.__init__.__defaults__
    if str(run / "disabled-render") not in defaults or ocr_stage.DEFAULT_VISION_ENDPOINT not in defaults:
        raise RuntimeError("vision bound defaults not isolated")
    try:
        socket.getaddrinfo("offline.invalid", 443)
    except RuntimeError:
        pass
    else:
        raise RuntimeError("offline socket guard missing")
    with tempfile.TemporaryFile() as stream:
        stream.write(b"synthetic boundary probe")
    return {"temp_root": tempfile.tempdir, "socket_guard": True,
            "ocr_default": ocr_stage.DEFAULT_OCRMYPDF,
            "render_default": ocr_stage.DEFAULT_PDFTOPPM,
            "vision_endpoint": ocr_stage.DEFAULT_VISION_ENDPOINT,
            "bound_defaults_checked": True, "child_processes_disabled": True}


def install_disposable_module(path):
    # File-backed replacement exists only in this disposable worker process.
    import campaign_tool.records.portal as package
    name = "campaign_tool.records.portal.collections"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    package.collections = module


def worker(args):
    run = Path(args.worker_root)
    boundary = isolate(run)
    loader = unittest.TestLoader()
    if args.scope_copy:
        install_disposable_module(Path(args.scope_copy))
    if args.case == "compatibility":
        suite = unittest.TestSuite(loader.loadTestsFromName(name) for name in COMPATIBILITY)
        expected = len(COMPATIBILITY)
    else:
        from tests.records import test_fa95
        cls = test_fa95.GateControl if args.case == "negative-control" else test_fa95.Behavior
        suite = loader.loadTestsFromTestCase(cls)
        expected = 2 if args.case == "negative-control" else 7
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    summary = {"tests_run": result.testsRun, "skipped": len(result.skipped),
               "failures": [test.id() for test, _ in result.failures],
               "errors": [test.id() for test, _ in result.errors], "boundary": boundary}
    save(run / "tests.json", summary)
    return int(not result.wasSuccessful() or result.testsRun != expected or bool(result.skipped))


def invoke(run, label, argv, context):
    proc = subprocess.run(argv, cwd=REPO, capture_output=True)
    stdout, stderr = run / (label + ".stdout"), run / (label + ".stderr")
    stdout.write_bytes(proc.stdout)
    stderr.write_bytes(proc.stderr)
    row = {**context, "argv": argv, "cwd": str(REPO), "exit": proc.returncode,
           "command_interpreter": argv[0],
           "command_interpreter_realpath": str(Path(argv[0]).resolve()),
           "stdout_path": str(stdout), "stdout_sha256": sha(proc.stdout),
           "stderr_path": str(stderr), "stderr_sha256": sha(proc.stderr)}
    save(run / (label + ".json"), row)
    return row


def test_process(run, label, case, context, scope=None):
    child = run / label
    child.mkdir()
    argv = [sys.executable, str(Path(__file__).resolve()), "--case", case,
            "--worker-root", str(child)]
    if scope is not None:
        argv += ["--scope-copy", str(scope)]
    row = invoke(run, label, argv, context)
    summary = child / "tests.json"
    row["tests"] = json.loads(summary.read_text()) if summary.exists() else {"missing_summary": True}
    save(run / (label + ".json"), row)
    return row


def defect_copy(run):
    path = run / "retained_sets.py"
    tree = ast.parse((REPO / SOURCE).read_text())
    changed = 0
    for cls in tree.body:
        if isinstance(cls, ast.ClassDef) and cls.name == "Scope":
            for method in cls.body:
                if isinstance(method, ast.FunctionDef) and method.name == "__post_init__":
                    original = len(method.body)
                    method.body = [node for node in method.body if not (
                        isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                        and isinstance(node.value.func, ast.Attribute)
                        and isinstance(node.value.func.value, ast.Name)
                        and node.value.func.value.id == "object" and node.value.func.attr == "__setattr__")]
                    changed = original - len(method.body)
    if changed != 2:
        raise RuntimeError("negative control did not isolate exactly the two snapshot assignments")
    path.write_text(ast.unparse(tree) + "\n")
    return path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", required=True, choices=("behavior", "compatibility", "negative-control", "credential-scan", "public-tree"))
    parser.add_argument("--output-root")
    parser.add_argument("--worker-root")
    parser.add_argument("--scope-copy")
    parser.add_argument("--baseline", action="store_true", help="Retain a disposable pinned-base comparison")
    args = parser.parse_args()
    if args.worker_root:
        return worker(args)
    if not args.output_root or args.scope_copy:
        parser.error("parent requires --output-root and forbids --scope-copy")
    root = Path(args.output_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix=args.case + "-", dir=root))
    manifest = json.loads(MANIFEST.read_text())
    context = identity(manifest)
    rows = []
    if args.baseline:
        if args.case not in ("behavior", "compatibility"):
            parser.error("baseline is only supported for behavior or compatibility")
        baseline = run / "pinned_collections.py"
        baseline.write_bytes(git("show", manifest["head"] + ":" + SOURCE))
        context["baseline_source_sha256"] = sha(baseline.read_bytes())
        rows.append(test_process(run, "baseline", args.case, context, baseline))
        code = rows[-1]["exit"]
    elif args.case in ("behavior", "compatibility"):
        rows.append(test_process(run, "tests", args.case, context))
        code = rows[-1]["exit"]
    elif args.case == "negative-control":
        defective = defect_copy(run)
        rows.append(test_process(run, "defective", args.case, context, defective))
        rows.append(test_process(run, "restored", args.case, context))
        bad, good = rows
        target = "tests.records.test_fa95.GateControl.test_mutation_hash_regression"
        code = int(not (bad["exit"] == 1 and bad["tests"].get("failures") == [target]
                       and bad["tests"].get("errors") == [] and bad["tests"].get("tests_run") == 2
                       and bad["tests"].get("skipped") == 0 and good["exit"] == 0
                       and good["tests"].get("tests_run") == 2 and good["tests"].get("skipped") == 0))
    elif args.case == "credential-scan":
        runtime = manifest["runtime"]
        argv = [runtime["credential_scan_python3"], runtime["detect_secrets"], "scan", "--all-files",
                "--exclude-files", "^.git/", "--no-verify"]
        rows.append(invoke(run, "scan", argv, context))
        report = run / "scan.stdout"
        rows.append(invoke(run, "checker", [runtime["credential_scan_python3"],
                    str(REPO / "tools/check_secret_report.py"), str(report)], context))
        data = json.loads(report.read_text())
        potential = sum(len(items) for items in data["results"].values())
        code = int(any(row["exit"] != 0 for row in rows) or potential != 0)
        context["potential_secrets"] = potential
    else:
        rows.append(invoke(run, "public-tree", [str(REPO / ".venv/bin/python3"),
                    str(REPO / "tools/check_public_tree.py")], context))
        code = rows[-1]["exit"]
    if identity(manifest) != {key: context[key] for key in identity(manifest)}:
        raise RuntimeError("candidate identity changed during acceptance")
    receipt = run / "receipt.json"
    save(receipt, {**context, "case": args.case, "baseline": args.baseline,
                   "argv": [sys.executable, *sys.argv], "cwd": str(REPO), "exit": code, "commands": rows})
    print(json.dumps({"case": args.case, "exit": code, "receipt": str(receipt),
                      "receipt_sha256": sha(receipt.read_bytes())}, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
