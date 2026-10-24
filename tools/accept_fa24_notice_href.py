"""Scoped acceptance runner for the FA24 ambiguous-anchor fix (offline only).

Each invocation runs exactly one case and writes its evidence to a fresh
directory below --output-parent, which must lie outside this repository:

  behavior          the complete tests/test_fa24_notice_href.py regression file
  compatibility     unchanged baseline portal notice/intake test files
  negative-control  the ambiguous-anchor regression against the preserved
                    dict-based parser double (must FAIL) and the candidate
                    parser (must PASS), plus the single-href positive
  credentials       installed detect-secrets scan + unchanged
                    tools/check_secret_report.py on that exact JSON

Before any repository module is imported the runner redirects tempfile to the
per-run directory, disables bytecode writes and installs socket guards. A pass
is REVIEW REQUIRED; it is not full-repository CI or semantic acceptance.
"""
import argparse
import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile

REPO = Path(__file__).resolve().parents[1]
REGRESSION = "tests.test_fa24_notice_href"
COMPATIBILITY = ("tests.test_records_portal", "tests.records.test_portal_intake")
HASHED = (
    "campaign_tool/records/portal/notice.py",
    "tests/test_fa24_notice_href.py",
    "tools/accept_fa24_notice_href.py",
    "tests/test_records_portal.py",
    "tests/records/test_portal_intake.py",
    "tools/check_secret_report.py",
)
GUARD_HITS = []


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def git(*args):
    done = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True, check=False)
    return done.stdout.strip() if done.returncode == 0 else f"unavailable:{done.returncode}"


def identity():
    hashes = {}
    for relative in HASHED:
        path = REPO / relative
        hashes[relative] = sha256_bytes(path.read_bytes()) if path.is_file() else None
    return {"head": git("rev-parse", "HEAD"), "status_porcelain": git("status", "--porcelain"), "source_sha256": hashes}


def fresh_run_dir(parent, case):
    parent = Path(parent)
    if not parent.is_absolute():
        raise SystemExit("output_parent_must_be_absolute")
    resolved = parent.resolve()
    if resolved == REPO or REPO in resolved.parents:
        raise SystemExit("output_parent_inside_repository")
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run = resolved / f"{case}-{stamp}-{os.getpid()}-{secrets.token_hex(4)}"
    run.mkdir(parents=True, exist_ok=False)
    return run


def install_offline_guards():
    def refuse(name):
        def guard(*args, **kwargs):
            GUARD_HITS.append(name)
            raise OSError(f"offline acceptance guard refused {name}")
        return guard
    socket.socket.connect = refuse("socket.connect")
    socket.socket.connect_ex = refuse("socket.connect_ex")
    socket.create_connection = refuse("socket.create_connection")
    socket.getaddrinfo = refuse("socket.getaddrinfo")


def prepare_import_root(run):
    tmp = run / "tmp"
    tmp.mkdir()
    tempfile.tempdir = str(tmp)
    os.environ["TMPDIR"] = str(tmp)
    sys.dont_write_bytecode = True
    install_offline_guards()
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    return tmp


def run_suite(suite, stream):
    import unittest
    runner = unittest.TextTestRunner(stream=stream, verbosity=2)
    return runner.run(suite)


def counts(result):
    return {"tests_run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
            "skipped": len(result.skipped), "expected_failures": len(result.expectedFailures),
            "unexpected_successes": len(result.unexpectedSuccesses)}


def clean(c):
    return (c["tests_run"] > 0 and c["failures"] == 0 and c["errors"] == 0 and c["skipped"] == 0
            and c["expected_failures"] == 0 and c["unexpected_successes"] == 0)


def case_tests(run, names):
    prepare_import_root(run)
    import unittest
    stream = io.StringIO()
    suite = unittest.defaultTestLoader.loadTestsFromNames(list(names))
    c = counts(run_suite(suite, stream))
    raw = stream.getvalue().encode()
    (run / "unittest.txt").write_bytes(raw)
    ok = clean(c) and not GUARD_HITS
    return ok, {"modules": list(names), "counts": c, "guard_hits": list(GUARD_HITS),
                "raw_output": "unittest.txt", "raw_output_sha256": sha256_bytes(raw)}


def case_negative_control(run):
    prepare_import_root(run)
    import unittest
    from unittest import mock
    from campaign_tool.records.portal import notice
    regression = __import__(REGRESSION, fromlist=["BaselineDictLinks"])
    names = [f"{REGRESSION}.AmbiguousAnchorTests", f"{REGRESSION}.InventoryIntegrationTests"]
    positive = f"{REGRESSION}.InventoryIntegrationTests.test_a4_single_href_inventories_once"
    stream = io.StringIO()
    stream.write("=== broken control: notice.Links -> preserved dict-based double ===\n")
    with mock.patch.object(notice, "Links", regression.BaselineDictLinks):
        broken = counts(run_suite(unittest.defaultTestLoader.loadTestsFromNames(names), stream))
        stream.write("=== broken control: single-href positive stays valid ===\n")
        broken_positive = counts(run_suite(unittest.defaultTestLoader.loadTestsFromName(positive), stream))
        last = regression.ambiguous_href_urls(notice.Links)
    stream.write("=== candidate parser: same regression ===\n")
    candidate = counts(run_suite(unittest.defaultTestLoader.loadTestsFromNames(names), stream))
    candidate_ambiguous = regression.ambiguous_href_urls(notice.Links)
    raw = stream.getvalue().encode()
    (run / "unittest.txt").write_bytes(raw)
    detected = broken["tests_run"] > 0 and (broken["failures"] + broken["errors"]) > 0
    last_selected = last == [regression.SECOND_URL]
    ok = (detected and last_selected and clean(broken_positive) and clean(candidate)
          and candidate_ambiguous == [] and not GUARD_HITS)
    return ok, {"broken_control": broken, "broken_positive": broken_positive,
                "broken_control_selected": "last_url" if last_selected else repr(last),
                "candidate": candidate, "candidate_ambiguous_href_urls": len(candidate_ambiguous),
                "guard_hits": list(GUARD_HITS), "raw_output": "unittest.txt",
                "raw_output_sha256": sha256_bytes(raw)}


def resolve_detect_secrets(explicit):
    if explicit:
        path = Path(explicit)
        return str(path) if path.is_absolute() and path.is_file() and os.access(path, os.X_OK) else None
    found = shutil.which("detect-secrets")
    if found:
        return found
    sibling = Path(sys.executable).parent / "detect-secrets"
    return str(sibling) if sibling.is_file() and os.access(sibling, os.X_OK) else None


def case_credentials(run, explicit):
    tool = resolve_detect_secrets(explicit)
    if tool is None:
        return False, {"stop": "missing_dependency: detect-secrets not installed/resolvable; no install or substitution"}
    scan_argv = [tool, "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"]
    report = run / "secret-scan.json"
    with open(report, "wb") as out:
        scan = subprocess.run(scan_argv, cwd=REPO, stdout=out, stderr=subprocess.PIPE, check=False)
    (run / "scan-stderr.txt").write_bytes(scan.stderr)
    check_argv = [sys.executable, str(REPO / "tools" / "check_secret_report.py"), str(report)]
    if scan.returncode != 0:
        return False, {"scan_argv": scan_argv, "scan_exit": scan.returncode, "stop": "scan_failed"}
    check = subprocess.run(check_argv, cwd=REPO, capture_output=True, check=False)
    raw = check.stdout + check.stderr
    (run / "check-secret-report.txt").write_bytes(raw)
    data = json.loads(report.read_bytes())
    files_with_hits = sorted(data.get("results", {}))
    return check.returncode == 0, {
        "scan_argv": scan_argv, "scan_cwd": str(REPO), "scan_exit": scan.returncode,
        "scan_json": "secret-scan.json", "scan_json_sha256": sha256_bytes(report.read_bytes()),
        "detect_secrets_version": data.get("version"), "files_with_hits": files_with_hits,
        "check_argv": check_argv, "check_exit": check.returncode,
        "check_output": raw.decode(errors="replace").strip().splitlines()[-1:] if raw else [],
        "check_output_sha256": sha256_bytes(raw)}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Scoped offline acceptance for the FA24 anchor fix.")
    parser.add_argument("--case", required=True, choices=["behavior", "compatibility", "negative-control", "credentials"])
    parser.add_argument("--output-parent", required=True)
    parser.add_argument("--detect-secrets", help="absolute path to an already-installed detect-secrets CLI")
    args = parser.parse_args(argv)
    run = fresh_run_dir(args.output_parent, args.case)
    before = identity()
    try:
        if args.case == "behavior":
            ok, details = case_tests(run, [REGRESSION])
        elif args.case == "compatibility":
            ok, details = case_tests(run, COMPATIBILITY)
        elif args.case == "negative-control":
            ok, details = case_negative_control(run)
        else:
            ok, details = case_credentials(run, args.detect_secrets)
    except Exception as exc:  # unknown failure stops acceptance
        ok, details = False, {"stop": f"unknown_failure: {type(exc).__name__}: {exc}"}
    after = identity()
    if after != before:
        ok = False
        details["stop"] = "candidate_identity_changed_during_run"
    record = {"case": args.case, "argv": [sys.executable, *sys.argv], "cwd": os.getcwd(),
              "interpreter": sys.executable, "python": sys.version.split()[0], "run_dir": str(run),
              "identity": before, "result": "PASS_REVIEW_REQUIRED" if ok else "FAIL",
              "exit": 0 if ok else 1, **details}
    (run / "result.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    print(json.dumps(record, indent=2, sort_keys=True))
    return record["exit"]


if __name__ == "__main__":
    raise SystemExit(main())
