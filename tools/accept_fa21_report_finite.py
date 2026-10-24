"""FA21 scoped acceptance runner: finite run-report durations (offline only).

Usage: python3 tools/accept_fa21_report_finite.py --case CASE --output-parent DIR

CASE is one of behavior, compatibility, negative-control or credentials. Each
invocation writes into a fresh directory below DIR (outside the repository)
and prints its result path. Exit 0 means the scoped machine checks passed and
the candidate is REVIEW REQUIRED. It is not full-repository CI, semantic
acceptance, human review or a data-source approval. Missing dependencies,
unknown failures and unreviewed scan hits fail closed; nothing is installed.
"""
import argparse
import hashlib
import io
import json
import os
import platform
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
AUTHORED = ("campaign_tool/records/run_report.py", "tests/test_fa21_report_finite.py",
            "tools/accept_fa21_report_finite.py")
SUITES = {
    "behavior": ("tests.test_fa21_report_finite",),
    "compatibility": ("tests.test_records_run_report", "tests.test_run_report_check",
                      "tests.records.test_run_coverage"),
    "negative-control": ("tests.test_fa21_report_finite.BrokenControlTests",),
}
SCAN_ARGV = ("scan", "--all-files", "--exclude-files", "^.git/", "--no-verify")
NETWORK_ATTEMPTS = []


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    return sha256_bytes(Path(path).read_bytes())


def fresh_run_dir(parent, case):
    parent = Path(parent).resolve()
    if parent == REPO or parent.is_relative_to(REPO):
        raise SystemExit("accept_fa21: output parent must be outside the repository")
    parent.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    run_dir = parent / f"{case}-{stamp}-{os.getpid()}-{secrets.token_hex(4)}"
    run_dir.mkdir(exist_ok=False)
    return run_dir


def install_offline_guards(run_dir):
    """Runs before any test or product import; children inherit the env values."""
    tmp = run_dir / "tmp"
    tmp.mkdir()
    tempfile.tempdir = str(tmp)
    os.environ["TMPDIR"] = str(tmp)
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    sys.dont_write_bytecode = True
    here = str(Path(__file__).resolve().parent)
    sys.path[:] = [p for p in sys.path if p != here]
    sys.path.insert(0, str(REPO))

    def refuse(name):
        def refused(*args, **kwargs):
            NETWORK_ATTEMPTS.append(name)
            raise OSError("network disabled by accept_fa21")
        return refused

    socket.socket.connect = refuse("socket.connect")
    socket.socket.connect_ex = refuse("socket.connect_ex")
    socket.create_connection = refuse("create_connection")
    socket.getaddrinfo = refuse("getaddrinfo")


def git(*args):
    result = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True)
    return {"argv": ["git", *args], "exit": result.returncode, "stdout": result.stdout}


def identity():
    head = git("rev-parse", "HEAD")
    status = git("status", "--porcelain", "--untracked-files=all")
    return {
        "candidate_head": head["stdout"].strip() if head["exit"] == 0 else None,
        "status_porcelain": status["stdout"].splitlines() if status["exit"] == 0 else None,
        "source_sha256": {name: sha256_file(REPO / name) for name in AUTHORED
                          if (REPO / name).is_file()},
    }


def run_unittest(case, run_dir, record):
    import unittest
    names = SUITES[case]
    stream = io.StringIO()
    suite = unittest.defaultTestLoader.loadTestsFromNames(names)
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    raw = stream.getvalue().encode("utf-8")
    raw_path = run_dir / "unittest.txt"
    raw_path.write_bytes(raw)
    record.update(names=list(names), tests_run=result.testsRun, skipped=len(result.skipped),
                  failures=len(result.failures), errors=len(result.errors),
                  unexpected_successes=len(result.unexpectedSuccesses),
                  raw_output={"path": str(raw_path), "sha256": sha256_bytes(raw)})
    return (result.testsRun > 0 and result.wasSuccessful() and not result.skipped
            and not result.unexpectedSuccesses)


def run_negative_direct(record):
    """Disable only the finite guard in-process; the regression must detect it."""
    from unittest import mock
    from campaign_tool.records import run_report
    import tests.test_fa21_report_finite as regression
    schema = run_report.load_schema()
    real = regression.nonfinite_regression(run_report.validate_run_report, schema)
    with mock.patch.object(run_report, "check_finite_durations", lambda report: None):
        broken = regression.nonfinite_regression(run_report.validate_run_report, schema)
        run_report.validate_run_report(regression.load_example(), schema)
        run_report.validate_run_report(regression.load_pass(), schema)
    record["direct_control"] = {"real_misses": len(real), "broken_misses": len(broken),
                                "broken_accepted": sum(m[2] == "accepted" for m in broken),
                                "finite_positive_valid_under_broken_guard": True}
    return not real and len(broken) > 0


def locate_scanner():
    found = shutil.which("detect-secrets")
    if found:
        return found
    sibling = Path(sys.executable).parent / "detect-secrets"
    return str(sibling) if sibling.is_file() and os.access(sibling, os.X_OK) else None


def run_credentials(run_dir, record):
    scanner = locate_scanner()
    record["scanner"] = scanner
    if scanner is None:
        record["stop"] = "MISSING_DEPENDENCY detect-secrets not installed for this interpreter"
        return False
    report_path = run_dir / "secret-scan.json"
    steps = []
    argv = [scanner, *SCAN_ARGV]
    with report_path.open("wb") as out:
        scan = subprocess.run(argv, cwd=REPO, stdout=out, stderr=subprocess.PIPE)
    (run_dir / "scan.stderr.txt").write_bytes(scan.stderr)
    raw = report_path.read_bytes()
    steps.append({"argv": argv, "cwd": str(REPO), "exit": scan.returncode,
                  "raw_output": {"path": str(report_path), "sha256": sha256_bytes(raw)},
                  "stderr_sha256": sha256_bytes(scan.stderr)})
    try:
        parsed = json.loads(raw)
        files_with_hits = len(parsed["results"])
    except (ValueError, KeyError, TypeError):
        files_with_hits = None
    record["scan_files_with_hits"] = files_with_hits
    record["steps"] = steps
    if scan.returncode != 0 or files_with_hits is None:
        record["stop"] = "SCAN_FAILED"
        return False
    argv = [sys.executable, "-B", str(REPO / "tools" / "check_secret_report.py"), str(report_path)]
    check = subprocess.run(argv, cwd=REPO, capture_output=True)
    check_path = run_dir / "check.stdout.txt"
    check_path.write_bytes(check.stdout)
    (run_dir / "check.stderr.txt").write_bytes(check.stderr)
    lines = check.stdout.decode("utf-8", "replace").splitlines()
    steps.append({"argv": argv, "cwd": str(REPO), "exit": check.returncode,
                  "raw_output": {"path": str(check_path), "sha256": sha256_bytes(check.stdout)},
                  "stderr_sha256": sha256_bytes(check.stderr),
                  "summary": lines[-1] if lines else None})
    if check.returncode != 0:
        record["stop"] = "UNREVIEWED_SCAN_HIT_OR_CHECK_FAILURE"
    return check.returncode == 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--case", required=True,
                        choices=("behavior", "compatibility", "negative-control", "credentials"))
    parser.add_argument("--output-parent", required=True)
    args = parser.parse_args(argv)
    run_dir = fresh_run_dir(args.output_parent, args.case)
    install_offline_guards(run_dir)
    record = {"case": args.case, "argv": [sys.executable, *sys.argv], "cwd": os.getcwd(),
              "interpreter": sys.executable, "python": platform.python_version(),
              "run_dir": str(run_dir), "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "before": identity()}
    try:
        if args.case == "credentials":
            ok = run_credentials(run_dir, record)
        else:
            ok = run_unittest(args.case, run_dir, record)
            if args.case == "negative-control":
                ok = run_negative_direct(record) and ok
    except Exception as exc:  # fail closed on any unknown failure
        record["stop"] = f"UNKNOWN_FAILURE {type(exc).__name__}"
        ok = False
    record["network_attempts"] = list(NETWORK_ATTEMPTS)
    ok = ok and not NETWORK_ATTEMPTS
    record["after"] = identity()
    record["exit"] = 0 if ok else 1
    record["verdict"] = "PASS_REVIEW_REQUIRED" if ok else "FAIL"
    record["note"] = "Scoped acceptance only; not full-repository CI or semantic acceptance."
    result_path = run_dir / "result.json"
    result_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary = {k: record.get(k) for k in ("case", "verdict", "exit", "tests_run", "skipped",
                                           "failures", "errors", "direct_control",
                                           "scan_files_with_hits", "stop", "network_attempts")}
    summary["candidate_head"] = record["after"]["candidate_head"]
    summary["result"] = str(result_path)
    summary["result_sha256"] = sha256_file(result_path)
    print(json.dumps(summary, sort_keys=True))
    return record["exit"]


if __name__ == "__main__":
    raise SystemExit(main())
