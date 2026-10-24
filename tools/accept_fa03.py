"""FA03 acceptance launcher: run one declared suite and keep raw, hashed evidence.

Usage (from the repository root)::

    python3 tools/accept_fa03.py --suite SUITE --output-root DIR

SUITE is one of ``regression``, ``compatibility``, ``control``,
``credential-scan`` or ``public-tree``. Every invocation creates a fresh,
uniquely named child of DIR holding raw stdout/stderr and ``receipt.json``
(argv, cwd, interpreter, git head, dirty files, file hashes, exit and test
counts). Nothing is precomputed: a missing prerequisite exits non-zero.

Unit-test suites run in a fresh child process of this same file. Before any
repository module is imported the child points ``tempfile``/TMPDIR/HOME at the
run directory, installs an offline socket guard, then loads only the exact
named test modules and records which repository modules were imported. The
internal ``--child`` option is used only for that self-spawn. Subprocesses
are literal argv lists; no shell or inline interpreter source is used.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid

REPO = Path(__file__).resolve().parents[1]
SUITES = ("regression", "compatibility", "control", "credential-scan", "public-tree")
FA03 = "tests.records.test_fa03_receipt_read_binding"
UNIT_SUITES = {
    "regression": [FA03 + ".Regression"],
    "control": [FA03 + ".GateControl"],
    "compatibility": ["tests.records.test_mail_adoption", "tests.records.test_mail_adoption_bindings",
                      "tests.records.test_mail_resolution_census"],
}
# Exact test modules (targets plus the fixture producers they import) a suite may load.
FA03_HELPERS = {"tests", "tests.records", FA03, "tests.records.test_export_bridge",
                "tests.records.test_mail_wire_intake", "tests.records.native_msg_fixtures",
                "tests.records.test_imap_intake", "tests.records.test_run_slice"}
ADMITTED_TESTS = {
    "regression": FA03_HELPERS,
    "control": FA03_HELPERS,
    "compatibility": {"tests", "tests.records", "tests.records.test_mail_adoption",
                      "tests.records.test_mail_adoption_bindings", "tests.records.test_mail_resolution_census",
                      "tests.records.test_export_bridge", "tests.records.test_mail_wire_intake",
                      "tests.records.native_msg_fixtures", "tests.records.test_imap_intake",
                      "tests.records.test_run_slice", "tests.records.test_mail_adoption_recovery",
                      "tests.records.test_mail_adoption_regression",
                      "tests.records.test_canonical_mail_resolution", "tests.records.test_ledger",
                      "tests.records.test_ledger_occurrences", "tests.records.test_mail_resolution_policy",
                      "tests.records.test_original_reconciliation"},
}
HASHED = ("campaign_tool/records/mail_adoption.py", "campaign_tool/records/intake/mail_delta.py",
          "tests/records/test_fa03_receipt_read_binding.py", "tools/accept_fa03.py",
          "tools/check_secret_report.py", "tools/check_public_tree.py")


def file_sha(path):
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def git(*args):
    done = subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, check=False)
    return done.returncode, done.stdout


def run_logged(argv, run_dir, label, stdout_path=None):
    out_path = stdout_path or run_dir / (label + ".stdout")
    err_path = run_dir / (label + ".stderr")
    with open(out_path, "wb") as out, open(err_path, "wb") as err:
        done = subprocess.run(argv, cwd=REPO, stdout=out, stderr=err, check=False,
                              env=child_env(run_dir))
    return {"argv": argv, "exit": done.returncode, "stdout": str(out_path), "stderr": str(err_path),
            "stdout_sha256": file_sha(out_path), "stderr_sha256": file_sha(err_path),
            "stdout_bytes": out_path.stat().st_size, "stderr_bytes": err_path.stat().st_size}


def child_env(run_dir):
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("PYTHON", "LD_")) and key not in {"TMPDIR", "TEMP", "TMP", "HOME"}}
    sandbox = run_dir / "sandbox"
    env.update(TMPDIR=str(sandbox / "tmp"), TEMP=str(sandbox / "tmp"), TMP=str(sandbox / "tmp"),
               HOME=str(sandbox / "home"), PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1")
    return env


def unit_suite(suite, run_dir):
    record = run_logged([sys.executable, "-B", str(Path(__file__).resolve()), "--suite", suite,
                         "--output-root", str(run_dir.parent), "--child", str(run_dir)], run_dir, "unittest")
    result_path = run_dir / "child-result.json"
    if not result_path.exists():
        return 2, {"child": record, "error": "child_result_missing"}
    child = json.loads(result_path.read_text())
    ok = (record["exit"] == 0 and child["ok"] and child["tests_run"] > 0)
    return (0 if ok else 1), {"child": record, "result": child,
                              "child_result_sha256": file_sha(result_path)}


def detect_secrets_command():
    sibling = Path(sys.executable).parent / "detect-secrets"
    if sibling.is_file() and os.access(sibling, os.X_OK):
        return str(sibling)
    return shutil.which("detect-secrets")


def credential_scan(run_dir):
    tool = detect_secrets_command()
    if tool is None:
        return 2, {"error": "detect_secrets_not_installed"}
    report = run_dir / "secret-scan.json"
    scan = run_logged([tool, "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"],
                      run_dir, "detect-secrets", stdout_path=report)
    detail = {"detect_secrets": tool, "scan": scan}
    try:
        results = json.loads(report.read_text()).get("results", {})
        raw_hits = sum(len(hits) for hits in results.values())
        detail.update(raw_detector_hits=raw_hits, files_with_hits=sorted(results))
    except (OSError, ValueError):
        detail.update(potential_secrets=None, error="scan_report_unreadable")
        return 2, detail
    check = run_logged([sys.executable, "-B", str(REPO / "tools" / "check_secret_report.py"), str(report)],
                       run_dir, "check-secret-report")
    detail["check"] = check
    # The unchanged repository checker owns its existing exact provenance pins.
    # Raw detector hits include those nonsecret hashes; never invent approvals
    # here, nor incorrectly report the raw count as potential secrets.
    summary = re.fullmatch(
        r"Verified provenance hash hits: (\d+); potential secrets: (\d+); manual privacy review is still required\.\n?",
        Path(check["stdout"]).read_text())
    potential = int(summary[2]) if summary else None
    verified = int(summary[1]) if summary else None
    detail.update(potential_secrets=potential, verified_provenance_hits=verified)
    ok = (scan["exit"] == 0 and check["exit"] == 0 and potential == 0
          and verified == raw_hits and scan["stdout_bytes"] > 0)
    return (0 if ok else 1), detail


def public_tree(run_dir):
    check = run_logged([sys.executable, "-B", str(REPO / "tools" / "check_public_tree.py")], run_dir,
                       "check-public-tree")
    return (0 if check["exit"] == 0 else 1), {"check": check}


def parent(args):
    root = Path(args.output_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    run_dir = root / ("%s-%s-%s" % (args.suite, stamp, uuid.uuid4().hex[:8]))
    run_dir.mkdir(mode=0o700)
    for sub in ("sandbox", "sandbox/tmp", "sandbox/home"):
        (run_dir / sub).mkdir(mode=0o700)
    head_exit, head = git("rev-parse", "HEAD")
    dirty_exit, dirty = git("status", "--porcelain", "--untracked-files=all")
    receipt = {
        "suite": args.suite, "argv": sys.argv, "cwd": os.getcwd(), "repo": str(REPO),
        "interpreter": sys.executable, "interpreter_realpath": os.path.realpath(sys.executable),
        "python_version": sys.version, "git_head": head.strip() if head_exit == 0 else None,
        "dirty_files": dirty.splitlines() if dirty_exit == 0 else None,
        "file_sha256": {name: file_sha(REPO / name) for name in HASHED},
        "started_utc": stamp, "run_dir": str(run_dir),
    }
    if args.suite in UNIT_SUITES:
        code, detail = unit_suite(args.suite, run_dir)
    elif args.suite == "credential-scan":
        code, detail = credential_scan(run_dir)
    else:
        code, detail = public_tree(run_dir)
    receipt.update(detail=detail, exit=code)
    counts = detail.get("result", {})
    receipt.update(tests_run=counts.get("tests_run", 0), skipped=counts.get("skipped_count", 0),
                   expected_failures=counts.get("expected_failures", 0),
                   checks_run=2 if args.suite == "credential-scan" else int(args.suite == "public-tree"))
    (run_dir / "receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"suite": args.suite, "exit": code, "run_dir": str(run_dir),
                      "receipt_sha256": file_sha(run_dir / "receipt.json"),
                      "tests_run": receipt["tests_run"], "skipped": receipt["skipped"],
                      "potential_secrets": detail.get("potential_secrets")}, sort_keys=True))
    return code


def child(args):
    """Fresh process: sandbox globals and network before importing repository code."""
    run_dir = Path(args.child).resolve()
    sandbox = run_dir / "sandbox"
    import tempfile
    tempfile.tempdir = str(sandbox / "tmp")
    for key in ("TMPDIR", "TEMP", "TMP"):
        os.environ[key] = tempfile.tempdir
    os.environ["HOME"] = str(sandbox / "home")
    import socket
    attempts = []

    def refuse(*a, **k):
        attempts.append(repr(a[:2])[:200])
        raise OSError("offline_socket_guard")
    socket.socket.connect = refuse
    socket.socket.connect_ex = refuse
    socket.socket.bind = refuse
    socket.socket.sendto = refuse
    if hasattr(socket.socket, "sendmsg"):
        socket.socket.sendmsg = refuse
    socket.create_connection = refuse
    socket.getaddrinfo = refuse
    preloaded = sorted(name for name in sys.modules if name.split(".")[0] in {"tests", "campaign_tool"})
    sys.path.insert(0, str(REPO))
    import unittest
    import importlib.abc

    class AdmittedTests(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname.startswith("tests.") and fullname not in ADMITTED_TESTS[args.suite]:
                raise ImportError("unadmitted_test_module: " + fullname)
            return None

    sys.meta_path.insert(0, AdmittedTests())
    # The admitted source closure has no ambient host-path globals/defaults:
    # records roots/configs are explicit arguments. HOME and tempfile are its
    # only ambient filesystem defaults, redirected above BEFORE execution.
    # Audit imports as a second check, not as a hostile-code sandbox. No import
    # may read a host records/config file or start a process/database.
    importing, import_accesses = [True], []
    library_roots = {Path(sys.base_prefix).resolve(), Path(sys.prefix).resolve()}
    allowed_roots = (REPO, sandbox, *library_roots)

    def audit_import(event, values):
        if not importing[0]:
            return
        if event in {"subprocess.Popen", "sqlite3.connect"}:
            raise RuntimeError("side_effect_during_import: " + event)
        if event in {"open", "os.listdir", "os.scandir"} and values:
            value = values[0]
            if isinstance(value, (str, bytes, os.PathLike)):
                path = Path(os.fsdecode(value)).absolute()
                import_accesses.append({"event": event, "path": str(path)})
                if not any(path.is_relative_to(root) for root in allowed_roots):
                    raise RuntimeError("outside_admitted_import_roots: " + str(path))
                if event == "open" and (values[2] or 0) & (os.O_WRONLY | os.O_RDWR | os.O_CREAT):
                    if not path.is_relative_to(sandbox):
                        raise RuntimeError("write_during_import: " + str(path))

    sys.addaudithook(audit_import)
    names = UNIT_SUITES[args.suite]
    suite = unittest.defaultTestLoader.loadTestsFromNames(names)
    importing[0] = False
    result = unittest.TextTestRunner(stream=sys.stderr, verbosity=2).run(suite)
    loaded_tests = sorted(name for name in sys.modules if name == "tests" or name.startswith("tests."))
    loaded_code = sorted(name for name in sys.modules if name.split(".")[0] == "campaign_tool")
    outside = sorted(set(loaded_tests) - ADMITTED_TESTS[args.suite])
    skipped = [{"id": case.id(), "reason": reason} for case, reason in result.skipped]
    strict = args.suite in {"regression", "control"}
    ok = (result.wasSuccessful() and result.testsRun > 0 and not preloaded and not outside
          and not attempts and not (strict and skipped)
          and not result.expectedFailures and not result.unexpectedSuccesses
          and tempfile.gettempdir() == str(sandbox / "tmp"))
    data = {"suite": args.suite, "names": names, "ok": ok, "tests_run": result.testsRun,
            "failures": [case.id() for case, _ in result.failures],
            "errors": [case.id() for case, _ in result.errors],
            "skipped": skipped, "skipped_count": len(skipped),
            "expected_failures": len(result.expectedFailures),
            "unexpected_successes": len(result.unexpectedSuccesses),
            "preloaded_repo_modules": preloaded, "loaded_test_modules": loaded_tests,
            "test_modules_outside_admitted_set": outside, "loaded_campaign_tool_modules": loaded_code,
            "socket_attempts": attempts, "tempdir": tempfile.gettempdir(),
            "import_guard_before_repository_execution": True,
            "import_accesses": import_accesses,
            "ambient_path_defaults": {"HOME": os.environ["HOME"], "tempfile": tempfile.gettempdir()},
            "loaded_file_sha256": {name: file_sha(getattr(sys.modules[name], "__file__", ""))
                                   for name in loaded_tests + loaded_code}}
    (run_dir / "child-result.json").write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    return 0 if ok else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--suite", required=True, choices=SUITES)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--child", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.child is not None:
        if args.suite not in UNIT_SUITES:
            parser.error("--child is internal to unit-test suites")
        return child(args)
    return parent(args)


if __name__ == "__main__":
    raise SystemExit(main())
