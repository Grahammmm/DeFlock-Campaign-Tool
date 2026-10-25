"""Small offline FA97 launcher; evidence is actual output, never precomputed PASS.

Only named suites run. Subprocesses use file argv, never shell/inline source.
All imported production interfaces use explicit fixture roots; this launcher
also redirects home/temp before imports and checks path globals/bound defaults.
"""
import argparse
from contextlib import ExitStack
import hashlib
import inspect
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

REPO = Path(__file__).resolve().parents[1]
COMPATIBILITY = ["tests.records.test_export_bridge.ExportBridgeTests." + name for name in (
    "test_replay_and_uidvalidity_reset_keep_originals_and_add_occurrence",
    "test_reopen_scope_guard_precedes_page_replay_discovery_and_readiness",
    "test_body_alternatives_collection_without_extension_and_explicit_exclusions")]
REGRESSION = "tests.records.test_fa97.Behavior.test_completed_empty_reopen_nonempty_refuses_before_targets"
SAME_EMPTY = "tests.records.test_fa97.Behavior.test_completed_empty_reopen_same_empty_is_noop"
SOURCE_MANIFEST = REPO.parent / "SOURCE-MANIFEST.json"


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def capture(argv, cwd, directory, name):
    result = subprocess.run(argv, cwd=cwd, capture_output=True, check=False)
    (directory / (name + ".stdout")).write_bytes(result.stdout)
    (directory / (name + ".stderr")).write_bytes(result.stderr)
    receipt = {"argv": list(map(str, argv)), "cwd": str(cwd), "exit": result.returncode,
               "stdout_sha256": sha(result.stdout), "stderr_sha256": sha(result.stderr)}
    save(directory / (name + ".json"), receipt)
    return receipt


def identity(repo, manifest):
    def git(*args):
        result = subprocess.run(["git", *args], cwd=repo, capture_output=True, check=False)
        return result.stdout if result.returncode == 0 else None
    head = git("rev-parse", "HEAD")
    diff = git("diff", "HEAD", "--binary")
    files = ["campaign_tool/records/portal/collections.py", "tests/records/test_fa97.py",
             "tools/accept_fa97.py", "tests/records/test_export_bridge.py"]
    return {"interpreter": sys.executable, "python_version": sys.version,
            "source_manifest_sha256": sha(manifest.read_bytes()),
            "git_head": head.decode().strip() if head else None,
            "dirty_diff_sha256": sha(diff) if diff is not None else None,
            "fixture_test_hashes": {name: sha((repo / name).read_bytes()) for name in files}}


def deny_socket(*args, **kwargs):
    raise RuntimeError("fa97_offline_socket")


def deny_live(*args, **kwargs):
    raise RuntimeError("fa97_live_interface_forbidden")


def path_inventory(run):
    """Assert closure has no host-output globals or bound Path defaults.

    This scope has no legacy host-path constants to rewrite. Reject any newly
    encountered one instead of silently calling an unredirected default. Home
    and tempfile have already been redirected before these modules execute.
    """
    checked = []
    for name, module in list(sys.modules.items()):
        if not name.startswith("campaign_tool.") or module is None:
            continue
        for key, value in list(vars(module).items()):
            if key.startswith("__"):
                continue
            values = [(key, value)]
            if inspect.isfunction(value) and value.__module__ == name:
                values += [(key + ".default", item) for item in (value.__defaults__ or ())]
                values += [(key + ".kwdefault", item) for item in (value.__kwdefaults__ or {}).values()]
            for label, item in values:
                if isinstance(item, Path) and item.is_absolute():
                    resolved = item.resolve()
                    if not (resolved.is_relative_to(REPO) or resolved.is_relative_to(run)):
                        raise RuntimeError("fa97_unredirected_path:" + name + "." + label)
                    checked.append(name + "." + label)
    return checked


def worker(selectors, run, manifest):
    global SOURCE_MANIFEST
    SOURCE_MANIFEST = manifest
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(REPO))
    temporary = run / "temp"
    temporary.mkdir(mode=0o700)
    previous_temp = tempfile.tempdir
    stream = io.StringIO()
    result = None
    with ExitStack() as stack:
        stack.callback(setattr, tempfile, "tempdir", previous_temp)
        tempfile.tempdir = str(temporary)
        stack.enter_context(patch.dict(os.environ, {"HOME": str(temporary), "TMPDIR": str(temporary)}))
        stack.enter_context(patch.object(socket.socket, "connect", deny_socket))
        stack.enter_context(patch.object(socket.socket, "connect_ex", deny_socket))
        stack.enter_context(patch.object(socket.socket, "sendto", deny_socket))
        stack.enter_context(patch.object(socket.socket, "sendall", deny_socket))
        stack.enter_context(patch.object(socket.socket, "send", deny_socket))
        stack.enter_context(patch.object(socket, "create_connection", deny_socket))
        stack.enter_context(patch.object(socket, "getaddrinfo", deny_socket))
        stack.enter_context(patch.object(socket, "gethostbyname", deny_socket))
        stack.enter_context(patch.object(socket, "gethostbyname_ex", deny_socket))
        # Guard installation precedes any production/test import.
        from campaign_tool.records.intake import imap_intake
        from campaign_tool.records import config
        stack.enter_context(patch.object(imap_intake, "load_config", deny_live))
        stack.enter_context(patch.object(config, "load_config", deny_live))
        from tests.records import test_fa97
        checked = path_inventory(run)
        # Probe only local guard functions: no DNS lookup or external target.
        try:
            socket.create_connection(("fixture.example.invalid", 443))
        except RuntimeError as error:
            if str(error) != "fa97_offline_socket":
                raise
        else:
            raise RuntimeError("fa97_socket_boundary_missing")
        if Path.home() != temporary or Path(tempfile.gettempdir()) != temporary:
            raise RuntimeError("fa97_path_boundary_missing")
        save(run / "boundary.json", {"pre_import_guards": True, "home_temp_redirected": True,
                                      "path_globals_and_defaults_checked": checked})
        suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(name) for name in selectors)
        selected = suite.countTestCases()
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
        failed = [getattr(test, "test_case", test).id() for test, _ in result.failures]
        errors = [getattr(test, "test_case", test).id() for test, _ in result.errors]
        counts = {"selected": selected, "executed": result.testsRun, "skipped": len(result.skipped),
                  "failures": len(result.failures), "errors": len(result.errors),
                  "failed_test_ids": sorted(set(failed)), "error_test_ids": errors}
        save(run / "tests.json", counts)
        (run / "unittest.stdout").write_text(stream.getvalue())
        print(stream.getvalue(), end="")
    return 0 if (result.wasSuccessful() and selected > 0 and result.testsRun == selected
                 and not result.skipped) else 1


def run_child(repo, selectors, directory, manifest, name):
    directory.mkdir(mode=0o700, parents=True)
    child_run = directory / "run"
    child_run.mkdir(mode=0o700)
    argv = [sys.executable, str(repo / "tools/accept_fa97.py"), "--worker", "--run", str(child_run),
            "--manifest", str(manifest), "--selectors", *selectors]
    receipt = capture(argv, repo, directory, name)
    receipt["identity"] = identity(repo, manifest)
    # A disposable mutation has no .git directory. Bind it to the exact
    # submitted candidate HEAD as well as its separately hashed executed bytes.
    receipt["candidate_identity"] = identity(REPO, manifest)
    receipt["tests"] = json.loads((child_run / "tests.json").read_text()) if (child_run / "tests.json").exists() else None
    save(directory / (name + ".json"), receipt)
    return receipt


def negative_control(root):
    # Copy only candidate-local bytes. Never mutate the submitted production tree.
    copy = root / "disposable-repo"
    shutil.copytree(REPO, copy, ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__", "node_modules"))
    source = copy / "campaign_tool/records/portal/collections.py"
    positive = source.read_text()
    guard = 'if row["discovery"] == "enumerated" or existing:'
    if positive.count(guard) != 1:
        raise RuntimeError("fa97_negative_control_guard_not_found")
    source.write_text(positive.replace(guard, "if existing:", 1))
    mutant_hash = sha(source.read_bytes())
    mutant = run_child(copy, [REGRESSION], root / "mutant", SOURCE_MANIFEST, "targeted-defect")
    same = run_child(copy, [SAME_EMPTY], root / "mutant-same-empty", SOURCE_MANIFEST, "same-empty")
    source.write_text(positive)
    restored = run_child(copy, [REGRESSION, SAME_EMPTY], root / "restored", SOURCE_MANIFEST, "positive")
    tests = mutant.get("tests") or {}
    detected = (mutant["exit"] == 1 and tests.get("executed") == 1
                and tests.get("errors") == 0 and tests.get("skipped") == 0
                and tests.get("failed_test_ids") == [REGRESSION]
                and same["exit"] == 0 and restored["exit"] == 0)
    value = {"detected": detected, "mutant_failed_tests": tests.get("failed_test_ids"),
             "mutant_same_empty_exit": same["exit"], "restored_positive_exit": restored["exit"],
             "mutant_source_sha256": mutant_hash, "restored_source_sha256": sha(source.read_bytes()),
             "parent_identity": identity(REPO, SOURCE_MANIFEST)}
    save(root / "negative-control.json", value)
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=["behavior", "compatibility", "negative-control", "credential-scan", "public-tree"])
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--run", type=Path)
    parser.add_argument("--manifest", type=Path, default=SOURCE_MANIFEST)
    parser.add_argument("--selectors", nargs="+")
    args = parser.parse_args()
    if args.worker:
        allowed = {*COMPATIBILITY, REGRESSION, SAME_EMPTY,
                   "tests.records.test_fa97.Behavior", "tests.records.test_fa97.GateControl"}
        if not args.selectors or not set(args.selectors) <= allowed or args.run is None:
            parser.error("worker requires an admitted named selector and run root")
        return worker(args.selectors, args.run, args.manifest)
    if not args.case or args.output_root is None:
        parser.error("--case and --output-root required")
    output = args.output_root.absolute()
    if any(path.is_symlink() for path in (output, *output.parents)):
        raise RuntimeError("fa97_output_symlink")
    if output.is_relative_to(REPO):
        raise RuntimeError("fa97_output_inside_repo")
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    run = output / (args.case + "-" + uuid.uuid4().hex)
    run.mkdir(mode=0o700)
    before = identity(REPO, args.manifest)
    if args.case in {"behavior", "compatibility", "negative-control"}:
        selectors = {"behavior": ["tests.records.test_fa97.Behavior"],
                     "compatibility": COMPATIBILITY,
                     "negative-control": ["tests.records.test_fa97.GateControl"]}[args.case]
        receipt = run_child(REPO, selectors, run / "suite", args.manifest, "command")
        code = receipt["exit"]
    elif args.case == "credential-scan":
        runtime = json.loads(args.manifest.read_text())["runtime"]
        python, tool = runtime["credential_scan_python3"], runtime["detect_secrets"]
        if not (Path(python).is_file() and Path(tool).is_file()):
            raise RuntimeError("fa97_declared_credential_runtime_missing")
        receipt = capture([python, tool, "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"],
                          REPO, run, "scan")
        report = run / "scan.stdout"
        json.loads(report.read_text())
        checked = capture([python, str(REPO / "tools/check_secret_report.py"), str(report)], REPO, run, "check")
        code = 0 if receipt["exit"] == checked["exit"] == 0 else 1
    else:
        receipt = capture([sys.executable, str(REPO / "tools/check_public_tree.py")], REPO, run, "command")
        code = receipt["exit"]
    after = identity(REPO, args.manifest)
    if before != after:
        code = 1
    save(run / "receipt.json", {"case": args.case, "argv": [sys.executable, *sys.argv],
         "cwd": str(Path.cwd()), "exit": code, "identity_before": before,
         "identity_after": after, "raw_receipt": receipt})
    print(json.dumps({"case": args.case, "exit": code, "evidence": str(run)}))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
