"""Small, offline unittest/evidence launcher for the FA1709 test-only scope."""
import argparse
from contextlib import ExitStack, redirect_stdout, redirect_stderr
import hashlib
import io
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import traceback
import unittest
import uuid
from unittest import mock

sys.dont_write_bytecode = True
REPO = Path(__file__).resolve().parents[1]
TEST_MODULE = "tests.records.test_fa1709_unit_locator_lexemes"
BEHAVIOR = [TEST_MODULE + ".LocatorLexemeTests." + name for name in (
    "test_equivalent_lexemes_preserve_bytes_and_distinct_evidence",
    "test_semantic_value_change_changes_evidence_not_observations")]
CONTROL = [TEST_MODULE + ".LocatorLexemeTests.test_canonicalizing_result_transformer_is_detected"]
COMPATIBILITY = ["tests.records.test_ledger_units.UnitProjectionTests." + name for name in (
    "test_exact_text_locator_hash_and_missing_provenance", "test_replay_no_duplicates")]
NEW_PATHS = ["tests/records/test_fa1709_unit_locator_lexemes.py", "tools/accept_fa1709.py"]
CASES = ("behavior", "compatibility", "negative-control", "isolation", "credential-scan", "public-tree")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def safe(path):
    path = Path(os.path.abspath(path))
    require(not any(p.is_symlink() for p in (path, *path.parents)), "symlink ancestry")
    return path


def save(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def git(*args, cwd=REPO):
    return subprocess.check_output(["git", *args], cwd=cwd, text=True).strip()


def identity(manifest):
    base = manifest["head"]
    head = git("rev-parse", "HEAD")
    require(git("rev-parse", base) == base, "base unavailable")
    changed = git("diff", "--name-only", base, "HEAD").splitlines()
    require(set(changed) <= set(NEW_PATHS), "out-of-scope committed changes")
    source = "campaign_tool/records/ledger/unit_projection.py"
    require(git("hash-object", source) == git("rev-parse", base + ":" + source),
            "source drift from staged base")
    paths = NEW_PATHS + [source, "campaign_tool/records/ledger/store.py",
                         "tests/records/test_ledger.py", "tests/records/test_ledger_units.py",
                         "tools/check_secret_report.py", "tools/check_public_tree.py"]
    return {"head": head, "base": base, "tree": git("rev-parse", "HEAD^{tree}"),
            "source_test_sha256": {p: digest(REPO / p) for p in paths},
            "tracked_blobs_sha256": hashlib.sha256(git("ls-tree", "-r", "HEAD").encode()).hexdigest(),
            "status": git("status", "--porcelain", "--untracked-files=all")}


def run_command(argv, cwd, run, label):
    completed = subprocess.run([str(a) for a in argv], cwd=cwd, capture_output=True,
                               timeout=180, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
                                                     TMPDIR=str(run)))
    out, err = run / (label + ".stdout"), run / (label + ".stderr")
    out.write_bytes(completed.stdout)
    err.write_bytes(completed.stderr)
    row = {"argv": [str(a) for a in argv], "cwd": str(cwd), "exit": completed.returncode,
           "stdout": str(out), "stderr": str(err),
           "stdout_sha256": digest(out), "stderr_sha256": digest(err)}
    save(run / (label + ".command.json"), row)
    require(completed.returncode == 0, "child failure: " + label)
    return row


def worker(run, selected, ordinary=False):
    """Imports and fixtures are guarded only within this worker's test scope."""
    sys.path.insert(0, str(REPO))
    from tests.records import test_fa1709_unit_locator_lexemes as fixtures
    fixtures.FIXTURE_ROOT = run
    before = (socket.socket, socket.create_connection, socket.getaddrinfo,
              subprocess.Popen, sqlite3.connect, tempfile.tempdir, list(sys.path), os.umask(0o077))
    os.umask(before[-1])
    stream, output = io.StringIO(), io.StringIO()
    with ExitStack() as stack, redirect_stdout(output), redirect_stderr(output):
        fixtures.offline(stack, run)
        stack.callback(os.umask, before[-1])
        stack.enter_context(mock.patch.object(tempfile, "tempdir", str(run)))
        # Compatibility selectors have an inspected inert import closure and
        # use only LedgerTests.TemporaryDirectory; redirect before loading them.
        if ordinary:
            program = unittest.main(module=None, argv=["unittest", "-v", *selected],
                                    testRunner=unittest.TextTestRunner(stream=stream, verbosity=2),
                                    exit=False)
            result = program.result
        else:
            suite = unittest.defaultTestLoader.loadTestsFromNames(selected)
            result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    after = (socket.socket, socket.create_connection, socket.getaddrinfo,
             subprocess.Popen, sqlite3.connect, tempfile.tempdir, list(sys.path), os.umask(0o077))
    os.umask(after[-1])
    require(before == after, "test scope leaked ambient state")
    (run / "tests.stdout").write_text(output.getvalue())
    (run / "tests.stderr").write_text(stream.getvalue())
    row = {"selected_test_ids": selected, "executed": result.testsRun,
           "errors": len(result.errors), "failures": len(result.failures),
           "skips": len(result.skipped), "unexpected_successes": len(result.unexpectedSuccesses),
           "expected_failures": len(result.expectedFailures), "ordinary_loader": ordinary,
           "ambient_restored": True, "argv": sys.argv, "cwd": str(Path.cwd()),
           "interpreter": sys.executable,
           "stdout_sha256": digest(run / "tests.stdout"),
           "stderr_sha256": digest(run / "tests.stderr")}
    save(run / "tests.json", row)
    require(result.testsRun == len(selected) and result.wasSuccessful() and
            not (result.skipped or result.expectedFailures or result.unexpectedSuccesses),
            "missing tests, failures, errors or skips")
    if selected == CONTROL:
        require(output.getvalue().count("EXPECTED SOURCE-SPELLING ASSERTION FAILURE") == 1 and
                "AssertionError:" in output.getvalue(), "missing actual deciding assertion failure")
    return row


def test_child(run, label, selected, ordinary=False):
    child = run / label
    child.mkdir(mode=0o700)
    argv = [sys.executable, "-B", str(Path(__file__).resolve()), "--worker", str(child),
            "--selectors", *selected]
    if ordinary:
        argv.append("--ordinary")
    run_command(argv, REPO, run, label)
    return json.loads((child / "tests.json").read_text())


def clean_checkout(run, head):
    require(not git("status", "--porcelain", "--untracked-files=all"), "scan requires clean signed-off HEAD")
    for path in NEW_PATHS:
        require(path in git("ls-files").splitlines(), "new file not committed")
    require("Signed-off-by:" in git("log", "-1", "--format=%B"), "missing signoff")
    checkout = run / "scan-checkout"
    run_command(["git", "clone", "--local", "--no-hardlinks", "--no-checkout", str(REPO), str(checkout)],
                REPO, run, "clone")
    run_command(["git", "checkout", "--detach", head], checkout, run, "detach")
    require(git("rev-parse", "HEAD", cwd=checkout) == head, "scan head mismatch")
    require(git("rev-parse", "HEAD^{tree}", cwd=checkout) == git("rev-parse", "HEAD^{tree}"),
            "scan tree mismatch")
    names = git("ls-files", "-z").split("\0")
    require(git("ls-files", "-z", cwd=checkout).split("\0") == names, "tracked set mismatch")
    for name in filter(None, names):
        path = safe(checkout / name)
        require(path.is_file() and digest(path) == digest(REPO / name), "tracked byte mismatch: " + name)
    require(not any(p.is_symlink() for p in checkout.rglob("*")), "symlink in scan checkout")
    require(not git("status", "--porcelain", cwd=checkout), "dirty scan checkout")
    save(run / "checkout.json", {"head": head, "tree": git("rev-parse", "HEAD^{tree}"),
                                "tracked_files": len(list(filter(None, names))),
                                "complete_tracked_paths": list(filter(None, names)),
                                "all_tracked_bytes_match": True, "no_symlinks": True})
    return checkout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=CASES)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--worker", type=Path)
    parser.add_argument("--selectors", nargs="+")
    parser.add_argument("--ordinary", action="store_true")
    args = parser.parse_args()
    if args.worker:
        worker(safe(args.worker), args.selectors, args.ordinary)
        return 0
    require(args.case and args.output_root, "case and output root required")
    parent = safe(REPO.parent)
    output = safe(args.output_root)
    require(parent in output.parents and REPO not in (output, *output.parents),
            "output must be outside repository beneath candidate parent")
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    run = output / (args.case + "-" + uuid.uuid4().hex)
    run.mkdir(mode=0o700)
    receipt = {"case": args.case, "argv": sys.argv, "cwd": str(Path.cwd()),
               "interpreter": sys.executable, "run": str(run), "exit": 1}
    try:
        manifest_path = safe(parent / "SOURCE-MANIFEST.json")
        manifest = json.loads(manifest_path.read_text())
        receipt["manifest_sha256"] = digest(manifest_path)
        receipt["identity"] = identity(manifest)
        initial = receipt["identity"]
        if args.case == "behavior":
            receipt["tests"] = [test_child(run, "behavior", BEHAVIOR)]
        elif args.case == "compatibility":
            receipt["tests"] = [test_child(run, "compatibility", COMPATIBILITY)]
        elif args.case == "negative-control":
            receipt["tests"] = [test_child(run, "control", CONTROL),
                                test_child(run, "restored-positive", BEHAVIOR)]
        elif args.case == "isolation":
            for name in NEW_PATHS:
                content = (REPO / name).read_text()
                require(not re.search(r"(?:/home/|/Users/|[a-fA-F0-9]{40}|[a-fA-F0-9]{64})", content),
                        "box-only path or hex literal in new source")
            receipt["tests"] = [test_child(run, "ordinary-before", BEHAVIOR + COMPATIBILITY, ordinary=True),
                                test_child(run, "second-prefix", BEHAVIOR),
                                test_child(run, "ordinary-after", BEHAVIOR + COMPATIBILITY, ordinary=True)]
        else:
            checkout = clean_checkout(run, initial["head"])
            runtime = manifest["runtime"]
            if args.case == "credential-scan":
                python, scanner = runtime["credential_scan_python3"], runtime["detect_secrets"]
                require(Path(python).is_file() and Path(scanner).is_file(), "staged scan tools missing")
                row = run_command([python, "-B", scanner, "scan", "--all-files", "--exclude-files",
                                   "^.git/", "--no-verify"], checkout, run, "detect-secrets")
                report = Path(row["stdout"])
                require(isinstance(json.loads(report.read_text())["results"], dict), "invalid scan report")
                receipt["scanner"] = row
                receipt["checker"] = run_command([python, "-B", "tools/check_secret_report.py", str(report)],
                                                  checkout, run, "check-secret-report")
            else:
                receipt["scanner"] = run_command([sys.executable, "-B", "tools/check_public_tree.py"],
                                                  checkout, run, "public-tree")
                result = json.loads(Path(receipt["scanner"]["stdout"]).read_text())
                require(result["scanned_files"] > 0 and not result["findings"], "public-tree violations")
        require(identity(manifest) == initial, "candidate identity changed during acceptance")
        receipt["exit"] = 0
    except Exception:
        receipt["error"] = traceback.format_exc()
        (run / "failure.stderr").write_text(receipt["error"])
    save(run / "receipt.json", receipt)
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return receipt["exit"]


if __name__ == "__main__":
    raise SystemExit(main())
