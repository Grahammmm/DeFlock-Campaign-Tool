#!/usr/bin/env python3
"""Retained offline unittest/evidence launcher for the FA1706 test-only scope."""
import argparse
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys
import tempfile
import traceback
import unittest
from unittest import mock
import uuid

sys.dont_write_bytecode = True
REPO = Path(__file__).resolve().parents[1]
SELF = Path(__file__).resolve()
sys.path.insert(0, str(REPO))
TEST = "tests.records.test_fa1706_zip_same_name_occurrences"
TEST_PATH = "tests/records/test_fa1706_zip_same_name_occurrences.py"
HELPER_PATH = "tools/accept_fa1706.py"
FOLDER = REPO / "campaign_tool/records/intake/folder.py"
COMPAT = ["tests.records.test_intake.IntakeTests.test_zip_traversal_nested_duplicates_and_secrets",
          "tests.records.test_intake.IntakeTests.test_duplicate_occurrences_idempotence_and_changed_bytes"]
BEHAVIOR = [TEST + ".ZipSameNameTests"]
CONTROL = [TEST + ".FilenameLookupControl.test_filename_lookup_control"]
REAL_RUN = subprocess.run
REAL_POPEN = subprocess.Popen


def require(value, message):
    if not value:
        raise RuntimeError(message)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, data):
    Path(path).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def safe_directory(path):
    path = Path(os.path.abspath(path))
    require(path.resolve() == path, "symlink ancestry refused")
    for part in [*reversed(path.parents), path]:
        if part.exists():
            mode = part.lstat().st_mode
            require(stat.S_ISDIR(mode) and not stat.S_ISLNK(mode), "non-directory ancestry")
    return path


def command(argv, cwd, out, label, *, env=None):
    """Every invoked command retains real argv, output, exit and hashes."""
    label = label + "-" + uuid.uuid4().hex[:10]
    stdout, stderr = out / (label + ".stdout"), out / (label + ".stderr")
    record = {"argv": [str(a) for a in argv], "cwd": str(cwd),
              "interpreter": sys.executable}
    save(out / (label + ".start.json"), record)
    with stdout.open("wb") as so, stderr.open("wb") as se:
        p = REAL_RUN(record["argv"], cwd=cwd, env=env, stdout=so, stderr=se,
                     timeout=180)
    record.update(exit=p.returncode, stdout=str(stdout), stderr=str(stderr),
                  stdout_sha256=sha(stdout), stderr_sha256=sha(stderr))
    save(out / (label + ".json"), record)
    require(p.returncode == 0, "child failed: " + label)
    return stdout.read_text()


def git(*args, cwd=REPO):
    p = REAL_RUN(["git", *args], cwd=cwd, capture_output=True, check=True)
    return p.stdout


def identity(manifest):
    head = git("rev-parse", "HEAD").decode().strip()
    base = manifest["head"]
    require(git("merge-base", base, head).decode().strip() == base, "base drift")
    require(not git("status", "--porcelain"), "candidate must be committed and clean")
    changed = git("diff", "--name-only", base, head).decode().splitlines()
    require(set(changed) == {TEST_PATH, HELPER_PATH}, "unexpected changed path set")
    for path in changed:
        require(not re.search(rb"(?<![a-fA-F0-9])(?:[a-fA-F0-9]{40}|[a-fA-F0-9]{64})(?![a-fA-F0-9])",
                              (REPO / path).read_bytes()), "hex literal in new code")
        require((b"/" + b"home/") not in (REPO / path).read_bytes(), "box-only path in new code")
    source = str(FOLDER.relative_to(REPO))
    require(git("rev-parse", head + ":" + source) == git("rev-parse", base + ":" + source),
            "source drift")
    return {"head": head, "base": base, "tree": git("rev-parse", "HEAD^{tree}").decode().strip(),
            "changed_paths": changed, "source_sha256": sha(FOLDER),
            "test_sha256": sha(REPO / TEST_PATH), "helper_sha256": sha(SELF),
            "source_blob": git("rev-parse", head + ":" + source).decode().strip()}


def suite(ids, count, out, label):
    loader = unittest.TestLoader()
    tests = loader.loadTestsFromNames(ids)
    require(not loader.errors and tests.countTestCases() == count, "missing or extra test cases")
    def test_ids(group):
        for item in group:
            if isinstance(item, unittest.TestSuite):
                yield from test_ids(item)
            else:
                yield item.id()
    selected = list(test_ids(tests))
    text_out, text_err = io.StringIO(), io.StringIO()
    with redirect_stdout(text_out), redirect_stderr(text_err):
        result = unittest.TextTestRunner(stream=text_err, verbosity=2).run(tests)
    (out / (label + ".stdout")).write_text(text_out.getvalue())
    (out / (label + ".stderr")).write_text(text_err.getvalue())
    row = {"selectors": ids, "selected_test_ids": selected, "executed": result.testsRun,
           "failures": len(result.failures), "errors": len(result.errors),
           "skips": len(result.skipped), "expected_failures": len(result.expectedFailures),
           "unexpected_successes": len(result.unexpectedSuccesses),
           "stdout_sha256": sha(out / (label + ".stdout")),
           "stderr_sha256": sha(out / (label + ".stderr"))}
    save(out / (label + ".json"), row)
    require(result.testsRun == count and result.wasSuccessful() and not result.skipped
            and not result.expectedFailures and not result.unexpectedSuccesses, "unittest failed: " + label)
    return row


def folder_popen(root):
    """Only the inspected folder CLI/worker closure may spawn, via this file."""
    def spawn(argv, *args, **kwargs):
        require(isinstance(argv, list) and len(argv) >= 3, "unknown child")
        require(argv[0] == sys.executable and Path(argv[1]).resolve() == FOLDER
                and argv[2] in {"run", "_worker"}, "non-folder child refused")
        require(not kwargs.get("shell"), "shell child refused")
        tail = argv[2:]
        for option in ("--root", "--output", "--source"):
            for i, value in enumerate(tail):
                if value == option:
                    p = Path(tail[i + 1])
                    require(p.is_absolute() and p.resolve() == p and p.is_relative_to(root),
                            "non-fixture child path")
        wrapped = [sys.executable, "-B", str(SELF), "--folder-root", str(root),
                   "--folder-args", *tail]
        return REAL_POPEN(wrapped, *args, **kwargs)
    return spawn


def folder_child(root, args):
    # This named child is only reached from the compatibility closure above.
    root = safe_directory(root)
    evidence = safe_directory(os.environ["FA1706_EVIDENCE_ROOT"])
    require(root.is_relative_to(evidence), "child fixture outside retained run")
    from tests.records.test_fa1706_zip_same_name_occurrences import offline_guards, load_folder
    record = {"argv": sys.argv, "cwd": str(Path.cwd()), "interpreter": sys.executable}
    receipt = evidence / ("folder-child-" + uuid.uuid4().hex + ".json")
    save(receipt, record)
    captured_out, captured_err = io.StringIO(), io.StringIO()
    record["exit"] = 1
    try:
        with redirect_stdout(captured_out), redirect_stderr(captured_err):
            with offline_guards(root, allow_children=True), ExitStack() as stack:
                stack.enter_context(mock.patch.object(subprocess, "Popen", folder_popen(root)))
                folder = load_folder()
                stack.enter_context(mock.patch.object(folder, "apply_resource_limit",
                                                      side_effect=lambda kind, requested: requested))
                stack.enter_context(mock.patch.object(sys, "argv", [str(FOLDER), *args]))
                folder.main()
        record["exit"] = 0
    except BaseException:
        captured_err.write(traceback.format_exc())
        raise
    finally:
        so, se = receipt.with_suffix(".stdout"), receipt.with_suffix(".stderr")
        so.write_text(captured_out.getvalue())
        se.write_text(captured_err.getvalue())
        record.update(stdout=str(so), stderr=str(se), stdout_sha256=sha(so), stderr_sha256=sha(se))
        save(receipt, record)
        print(captured_out.getvalue(), end="")
        print(captured_err.getvalue(), end="", file=sys.stderr)
    return 0


def compatibility(out, tmp):
    from tests.records.test_fa1706_zip_same_name_occurrences import offline_guards
    with offline_guards(out, allow_children=True), mock.patch.object(
            subprocess, "Popen", folder_popen(tmp)), mock.patch.object(sys, "path", list(sys.path)):
        return suite(COMPAT, 2, out, "compatibility")


def ambient():
    return (list(sys.path), dict(os.environ), tempfile.tempdir, socket.socket.connect,
            subprocess.Popen, __import__("builtins").open, io.open, os.open)


def portable(out, label):
    output = command([sys.executable, "-B", "-m", "unittest", "-v", *BEHAVIOR],
                     REPO, out, label)
    # unittest writes its run summary to stderr; inspect the actual retained log.
    records = sorted(out.glob(label + "-*.json"))
    records = [p for p in records if not p.name.endswith(".start.json")]
    require(len(records) == 1, "ambiguous portable receipt")
    data = json.loads(records[0].read_text())
    err = Path(data["stderr"]).read_text()
    require(re.search(r"Ran 4 tests? in ", err) and "\nOK\n" in err
            and "skipped=" not in err, "portable test count/result mismatch")
    return {"executed": 4, "receipt": str(records[0]), "stdout": output}


def clone_scan(out, pinned):
    checkout = out / "scan-repo"
    command(["git", "clone", "--local", "--no-hardlinks", "--no-checkout", str(REPO), str(checkout)],
            REPO, out, "clone")
    command(["git", "checkout", "--detach", pinned["head"]], checkout, out, "detach")
    require(git("rev-parse", "HEAD", cwd=checkout).decode().strip() == pinned["head"], "scan head mismatch")
    require(git("rev-parse", "HEAD^{tree}", cwd=checkout).decode().strip() == pinned["tree"], "scan tree mismatch")
    paths = git("ls-files", "-z")
    require(paths == git("ls-files", "-z", cwd=checkout), "incomplete scan checkout")
    names = paths.decode().strip("\0").split("\0")
    require(TEST_PATH in names and HELPER_PATH in names, "new files not in scan tree")
    for relative in names:
        item = checkout / relative
        require(item.is_file() and not item.is_symlink(), "nonregular scan source")
        require(item.read_bytes() == (REPO / relative).read_bytes(), "scan worktree bytes mismatch")
    require(not git("status", "--porcelain", cwd=checkout), "dirty scan clone")
    save(out / "scan-identity.json", {**pinned, "tracked_paths": names, "tracked_count": len(names),
                                      "tracked_paths_sha256": hashlib.sha256(paths).hexdigest()})
    return checkout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=["behavior", "compatibility", "negative-control", "isolation",
                                           "credential-scan", "public-tree"])
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--folder-root", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--folder-args", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.folder_root is not None:
        require(args.folder_args and not args.case, "invalid child invocation")
        return folder_child(args.folder_root, args.folder_args)
    require(args.case and args.output_root, "case and output root required")
    require(Path.cwd().resolve() == REPO, "run from candidate repo")
    parent = safe_directory(REPO.parent)
    manifest_path = parent / "SOURCE-MANIFEST.json"
    manifest = json.loads(manifest_path.read_text())
    output_root = safe_directory(args.output_root)
    require(output_root.is_relative_to(parent / "out") and not output_root.is_relative_to(REPO),
            "output must be beneath candidate out, outside repo")
    output_root.mkdir(parents=True, exist_ok=True)
    out = output_root / (args.case + "-" + uuid.uuid4().hex)
    out.mkdir()
    record = {"argv": sys.argv, "cwd": str(Path.cwd()), "interpreter": sys.executable,
              "case": args.case, "output": str(out), "manifest_sha256": sha(manifest_path),
              "author_runtime": "gpt-6.1-sol / openai-codex (declared by task run; not independent proof)"}
    save(out / "start.json", record)
    original = ambient()
    exit_code = 1
    try:
        pinned = identity(manifest)
        record["identity"] = pinned
        tmp = out / "tmp"
        tmp.mkdir()
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(tempfile, "tempdir", str(tmp)))
            stack.enter_context(mock.patch.dict(os.environ, {"TMPDIR": str(tmp), "TEMP": str(tmp),
                                "TMP": str(tmp), "PYTHONDONTWRITEBYTECODE": "1",
                                "FA1706_EVIDENCE_ROOT": str(out),
                                "FA1706_CONTROL_EVIDENCE": str(out / "expected-assertion.txt")}))
            if args.case == "behavior":
                record["tests"] = suite(BEHAVIOR, 4, out, "behavior")
            elif args.case == "compatibility":
                record["tests"] = compatibility(out, tmp)
            elif args.case == "negative-control":
                record["tests"] = suite(CONTROL, 1, out, "negative-control")
                expected = (out / "expected-assertion.txt").read_text()
                require("AssertionError" in expected and "FA1706 ordered payloads" in expected,
                        "missing decisive assertion evidence")
                record["expected_assertion_sha256"] = sha(out / "expected-assertion.txt")
            elif args.case == "isolation":
                record["portable_before"] = portable(out, "portable-before")
                before_suite = ambient()
                second = tmp / "second-prefix"
                second.mkdir()
                with mock.patch.object(tempfile, "tempdir", str(second)):
                    record["tests"] = suite(BEHAVIOR, 4, out, "second-prefix")
                require(ambient() == before_suite, "suite poisoned ambient process state")
                record["portable_after"] = portable(out, "portable-after")
                require(identity(manifest) == pinned, "source identity changed during isolation")
            else:
                checkout = clone_scan(out, pinned)
                if args.case == "credential-scan":
                    runtime = manifest["runtime"]
                    python, detector = runtime["credential_scan_python3"], runtime["detect_secrets"]
                    require(Path(python).is_file() and Path(detector).is_file(), "missing staged scan tools")
                    report = command([python, detector, "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"],
                                     checkout, out, "detect-secrets")
                    report_path = out / "detect-secrets.json"
                    report_path.write_text(report)
                    json.loads(report)
                    result = command([python, str(checkout / "tools/check_secret_report.py"), str(report_path)],
                                     checkout, out, "secret-checker")
                    require("potential secrets: 0;" in result, "secret checker did not confirm zero")
                    record["scan_report_sha256"] = sha(report_path)
                else:
                    output = command([str(REPO / ".venv/bin/python3"), "-B", str(checkout / "tools/check_public_tree.py")],
                                     checkout, out, "public-tree")
                    report = json.loads(output)
                    require(report["scanned_files"] > 0 and not report["findings"], "public tree violations")
                    record["public_tree"] = report
                require(not git("status", "--porcelain", cwd=checkout), "scanner modified source checkout")
        require(ambient() == original, "launcher left ambient changes")
        require(identity(manifest) == pinned, "candidate identity changed")
        exit_code = 0
    except Exception:
        (out / "failure.txt").write_text(traceback.format_exc())
        record["failure"] = str(out / "failure.txt")
    record["exit"] = exit_code
    save(out / "result.json", record)
    print(json.dumps({"exit": exit_code, "output": str(out), "tests": record.get("tests")}))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
