"""FA34 scoped acceptance runner: one fail-closed case per invocation, evidence outside the repo.

    python3 tools/accept_fa34_ocr_page_unique.py --case CASE --output-parent DIR

CASE is behavior (A1/A2/A3/A5/A6), negative-control (A4), compatibility (three unchanged
extraction-validation selectors) or credentials (installed detect-secrets scan, then the
unchanged tools/check_secret_report.py on that exact report). Each invocation creates a new
exclusive ``<case>-<utc>-<pid>`` directory under DIR holding output.txt and result.json
(argv, cwd, exit, counts, test ids, head, source and pre-existing evidence hashes).

Temporary roots are redirected into that directory and network connections are refused
before any repository module is imported. A missing dependency fails closed; nothing is
installed or substituted. A pass is REVIEW REQUIRED: scoped evidence, not full CI.
"""
import argparse
import datetime
import hashlib
import importlib.metadata
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile

REPO = Path(__file__).resolve().parents[1]
NEW = "tests.test_fa34_ocr_page_unique."
LEGACY = "tests.records.test_extraction_validation.ExtractionValidationTests."
CASES = {
    "behavior": [NEW + "FA34OcrPageUniqueTests.test_valid_round_trip",
                 NEW + "FA34OcrPageUniqueTests.test_malformed_refusal",
                 NEW + "FA34OcrPageUniqueTests.test_boundary_replay",
                 NEW + "FA34ConsumerContractTests.test_consumer_contract",
                 NEW + "FA34OcrPageUniqueTests.test_preservation"],
    "negative-control": [NEW + "FA34RegressionControlTests.test_regression_control"],
    "compatibility": [LEGACY + "test_per_page_ocr_import_is_accepted_with_visual_hold_recorded",
                      LEGACY + "test_ocr_receipt_page_marker_refuses_acceptance_hold",
                      LEGACY + "test_enrollment_remains_pending_without_installed_opt_in"],
}
HASHED = ("campaign_tool/records/extraction_validation.py", "tests/test_fa34_ocr_page_unique.py",
          "tools/accept_fa34_ocr_page_unique.py", "tests/records/test_extraction_validation.py",
          "tests/records/test_extraction_ledger.py", "tests/records/ocr_fixtures.py",
          "tools/check_secret_report.py")


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def git(*args):
    done = subprocess.run(["git", *args], cwd=REPO, capture_output=True, check=False)
    return done.stdout.decode("utf-8", "replace")


def tree_hashes(root, skip):
    """Hashes of files already under the output parent (prior evidence must stay unchanged)."""
    found = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink() and skip not in path.parents:
            found[str(path.relative_to(root))] = sha256(path.read_bytes())
    return found


def make_run_directory(parent, case):
    parent = Path(parent)
    if not parent.is_absolute():
        raise SystemExit("output_parent_must_be_absolute")
    parent = parent.resolve()
    if parent == REPO or REPO in parent.parents:
        raise SystemExit("output_parent_inside_repository")
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    run = parent / "{}-{}-{}".format(case, stamp, os.getpid())
    run.mkdir(mode=0o700)               # exclusive: an existing directory is an error
    return parent, run


def offline(attempts):
    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex
    inet = (socket.AF_INET, socket.AF_INET6)

    def connect(sock, address):
        if sock.family in inet:
            attempts.append("connect:" + repr(address))
            raise OSError("fa34_acceptance_offline")
        return real_connect(sock, address)

    def connect_ex(sock, address):
        if sock.family in inet:
            attempts.append("connect_ex:" + repr(address))
            raise OSError("fa34_acceptance_offline")
        return real_connect_ex(sock, address)

    def lookup(*args, **kwargs):
        attempts.append("getaddrinfo:" + repr(args[:2]))
        raise OSError("fa34_acceptance_offline")
    socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo = connect, connect_ex, lookup


def missing_dependencies():
    missing = []
    try:
        distribution = importlib.metadata.distribution("pypdf")
        if not distribution.read_text("METADATA") or importlib.util.find_spec("pypdf") is None:
            missing.append("pypdf")
    except importlib.metadata.PackageNotFoundError:
        missing.append("pypdf")
    return missing


def run_tests(case, run, record):
    missing = missing_dependencies()
    if missing:
        record["failure"] = "missing_dependency:" + ",".join(missing)
        return 2, ""
    scratch = run / "tmp"
    scratch.mkdir(mode=0o700)
    tempfile.tempdir = str(scratch)
    os.environ["TMPDIR"] = str(scratch)
    sys.dont_write_bytecode = True
    attempts = []
    offline(attempts)
    sys.path.insert(0, str(REPO))
    import unittest                      # only after guards and temp redirection

    passed = []

    class Recording(unittest.TextTestResult):
        def addSuccess(self, test):
            super().addSuccess(test)
            passed.append(test.id())
    stream = io.StringIO()
    suite = unittest.defaultTestLoader.loadTestsFromNames(CASES[case])
    result = unittest.TextTestRunner(stream=stream, verbosity=2, resultclass=Recording).run(suite)
    record.update(tests_run=result.testsRun, passed=len(passed), failures=len(result.failures),
                  errors=len(result.errors), skipped=len(result.skipped),
                  expected_failures=len(result.expectedFailures),
                  unexpected_successes=len(result.unexpectedSuccesses),
                  skip_reasons=sorted({reason for _, reason in result.skipped}),
                  selected=CASES[case], test_ids=passed, network_attempts=attempts)
    problems = []
    if result.testsRun == 0:
        problems.append("no_tests_ran")
    if result.failures or result.errors or result.expectedFailures or result.unexpectedSuccesses:
        problems.append("test_failures")
    if result.skipped:
        problems.append("unexpected_skips")
    if sorted(passed) != sorted(CASES[case]):
        problems.append("selected_tests_not_all_passed")
    if attempts:
        problems.append("network_attempted")
    if any(scratch.iterdir()):
        problems.append("temporary_residue")
    record["failure"] = ",".join(problems) or None
    return (1 if problems else 0), stream.getvalue()


def run_credentials(run, record):
    scanner = shutil.which("detect-secrets")
    sibling = Path(sys.executable).parent / "detect-secrets"
    if scanner is None and os.access(sibling, os.X_OK):
        scanner = str(sibling)
    if scanner is None:
        record["failure"] = "missing_dependency:detect-secrets"
        return 2, ""
    report = run / "detect-secrets.json"
    scan = [scanner, "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"]
    done = subprocess.run(scan, cwd=REPO, capture_output=True, check=False)
    report.write_bytes(done.stdout)
    record["subprocesses"] = [{"argv": scan, "cwd": str(REPO), "exit": done.returncode,
                               "stdout_sha256": sha256(done.stdout), "stderr_bytes": len(done.stderr)}]
    if done.returncode != 0:
        record["failure"] = "scan_failed"
        return 1, "detect-secrets exited {} (stderr {} bytes)".format(done.returncode, len(done.stderr))
    check = [sys.executable, str(REPO / "tools" / "check_secret_report.py"), str(report)]
    checked = subprocess.run(check, cwd=REPO, capture_output=True, check=False)
    output = checked.stdout.decode("utf-8", "replace") + checked.stderr.decode("utf-8", "replace")
    record["subprocesses"].append({"argv": check, "cwd": str(REPO), "exit": checked.returncode,
                                   "stdout_sha256": sha256(checked.stdout)})
    try:
        files = len(json.loads(done.stdout)["results"])
    except (ValueError, KeyError, TypeError):
        files = None
    record.update(report_path=str(report), report_sha256=sha256(done.stdout), files_with_hits=files)
    record["failure"] = None if checked.returncode == 0 and files is not None else "report_check_failed"
    return (0 if record["failure"] is None else 1), output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--case", required=True, choices=(*CASES, "credentials"))
    parser.add_argument("--output-parent", required=True)
    args = parser.parse_args(argv)
    parent, run = make_run_directory(args.output_parent, args.case)
    prior = tree_hashes(parent, run)
    status_before = git("status", "--porcelain", "--untracked-files=all")
    record = {"case": args.case, "argv": [sys.executable, *sys.argv], "cwd": os.getcwd(),
              "interpreter": sys.executable, "python": sys.version.split()[0],
              "candidate_head": git("rev-parse", "HEAD").strip(), "status_before": status_before.splitlines(),
              "source_sha256": {name: sha256((REPO / name).read_bytes()) if (REPO / name).is_file() else None
                                for name in HASHED},
              "prior_evidence_files": len(prior),
              "full_discovery": "operator-run (not part of this scoped acceptance)",
              "disposition": "REVIEW REQUIRED - scoped offline acceptance, not full CI or semantic acceptance"}
    if args.case == "credentials":
        code, output = run_credentials(run, record)
    else:
        code, output = run_tests(args.case, run, record)
    status_after = git("status", "--porcelain", "--untracked-files=all")
    record["status_after"] = status_after.splitlines()
    if code == 0 and status_after != status_before:
        code, record["failure"] = 1, "repository_changed_during_acceptance"
    if code == 0 and tree_hashes(parent, run) != prior:
        code, record["failure"] = 1, "prior_evidence_changed"
    raw = output.encode("utf-8")
    (run / "output.txt").write_bytes(raw)
    record.update(exit=code, output_path=str(run / "output.txt"), output_sha256=sha256(raw))
    (run / "result.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    print(output[-6000:])
    print(json.dumps({"run_directory": str(run), **{key: record.get(key) for key in (
        "case", "exit", "failure", "tests_run", "passed", "failures", "errors", "skipped",
        "interpreter", "candidate_head", "output_sha256", "report_sha256", "files_with_hits")}}, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
