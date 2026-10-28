#!/usr/bin/env python3
"""Small offline unittest/evidence launcher for FA1702; no release authority."""
import sys
sys.dont_write_bytecode = True

import argparse
from contextlib import redirect_stdout, redirect_stderr, nullcontext
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import tempfile
import traceback
import unittest
import uuid

REPO = Path(__file__).resolve().parents[1]
MODULE = "tests.records.test_fa1702_selection_use_boundary"
BEHAVIOR = MODULE + ".SelectionUseBoundaryTests"
BEHAVIOR_IDS = sorted(BEHAVIOR + "." + name for name in (
    "test_unchanged_selection_normalizes_yields_and_closes_once",
    "test_export_root_ancestor_changed_after_construction",
    "test_receipt_intermediate_ancestor_changed_after_construction",
    "test_canonical_root_ancestor_changed_after_construction",
))
COMPATIBILITY = [
    "tests.records.test_export_selection_cli.ExportSelectionCLITests."
    "test_inventory_cannot_open_outside_existing_root_lock",
    "tests.records.test_export_selection_cli.ExportSelectionCLITests."
    "test_queue_closes_when_inventory_construction_raises",
]
AUTHORED = ["tests/records/test_fa1702_selection_use_boundary.py", "tools/accept_fa1702.py"]
SOURCE = "campaign_tool/records/export_selection.py"
CASES = ("behavior", "compatibility", "negative-control", "isolation", "credential-scan", "public-tree")


def require(value, reason):
    if not value:
        raise RuntimeError(reason)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def safe(path):
    path = Path(os.path.abspath(path))
    require(not any(p.is_symlink() for p in (path, *path.parents)), "symlink output ancestry")
    return path


def save(path, value):
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def command(argv, cwd, out, label):
    completed = subprocess.run([str(arg) for arg in argv], cwd=cwd,
                               capture_output=True, check=False)
    stdout, stderr = out / (label + ".stdout"), out / (label + ".stderr")
    stdout.write_bytes(completed.stdout)
    stderr.write_bytes(completed.stderr)
    record = {"argv": [str(arg) for arg in argv], "cwd": str(cwd),
              "exit": completed.returncode, "stdout": str(stdout), "stderr": str(stderr),
              "stdout_sha256": digest(stdout), "stderr_sha256": digest(stderr)}
    save(out / (label + ".command.json"), record)
    require(completed.returncode == 0, "child failed: " + label)
    return completed.stdout


def git(*args):
    result = subprocess.run(["git", *args], cwd=REPO, capture_output=True, check=False)
    require(result.returncode == 0, "git identity failed: " + repr(args))
    return result.stdout.decode().strip()


def identity():
    paths = [SOURCE, *AUTHORED]
    return {"head": git("rev-parse", "HEAD"), "tree": git("rev-parse", "HEAD^{tree}"),
            "hashes": {p: digest(REPO / p) for p in paths},
            "blobs": {p: git("hash-object", p) for p in paths}}


def ids(suite):
    found = []
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            found.extend(ids(item))
        else:
            found.append(item.id())
    return found


def run_suite(selectors, out, label, suite=None, expect_failure=False, guard=None):
    stream = io.StringIO()
    with guard or nullcontext():
        if suite is None:
            suite = unittest.defaultTestLoader.loadTestsFromNames(selectors)
        selected = ids(suite)
        with redirect_stdout(stream), redirect_stderr(stream):
            result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    raw = out / (label + ".unittest.txt")
    raw.write_text(stream.getvalue())
    record = {"selected_test_ids": selected, "testsRun": result.testsRun,
              "failures": [(t.id(), text) for t, text in result.failures],
              "errors": [(t.id(), text) for t, text in result.errors],
              "skips": [(t.id(), text) for t, text in result.skipped],
              "expectedFailures": len(result.expectedFailures),
              "unexpectedSuccesses": len(result.unexpectedSuccesses),
              "raw_output": str(raw), "raw_sha256": digest(raw)}
    save(out / (label + ".tests.json"), record)
    if selectors == [BEHAVIOR]:
        require(sorted(selected) == BEHAVIOR_IDS, "missing behavior case")
    if selectors == COMPATIBILITY:
        require(selected == COMPATIBILITY, "missing compatibility case")
    require(result.testsRun == len(selected) and result.testsRun > 0, "missing/zero tests")
    require(not result.errors and not result.skipped and not result.expectedFailures
            and not result.unexpectedSuccesses, "unexpected unittest outcome")
    if expect_failure:
        require(result.testsRun == 1 and len(result.failures) == 1,
                "control must fail exactly one assertion")
        failed, text = result.failures[0]
        require(failed.id() == selected[0] and "USE_BOUNDARY_NO_QUEUE" in text
                and "AssertionError:" in text, "wrong control failure")
    else:
        require(result.wasSuccessful(), "unittest failure")
    print(label, "executed", result.testsRun, "failures", len(result.failures),
          "errors", len(result.errors), "skips", len(result.skipped))
    return record


def worker(case, out):
    # Named child only. Process-local temp redirection precedes app imports;
    # tests restore all scoped mocks. This process has no scan/clone authority.
    scratch = safe(out / "fixtures")
    scratch.mkdir()
    os.environ["TMPDIR"] = str(scratch)
    tempfile.tempdir = str(scratch)
    sys.path.insert(0, str(REPO))
    module = importlib.import_module(MODULE)  # stdlib-only module import
    if case == "behavior":
        run_suite([BEHAVIOR], out, "behavior")
    elif case == "compatibility":
        record = run_suite(COMPATIBILITY, out, "compatibility", guard=module.offline(scratch))
        require(record["selected_test_ids"] == COMPATIBILITY, "compatibility selector drift")
    elif case == "negative-control":
        run_suite([], out, "missing-recheck", module.negative_suite(), expect_failure=True)
        run_suite([BEHAVIOR], out, "restored-real-behavior")
    elif case == "isolation":
        snapshot = (tuple(sys.path), tempfile.tempdir, socket.getaddrinfo,
                    socket.socket.connect, io.open, subprocess.Popen)
        run_suite([BEHAVIOR], out, "ordinary-before")
        # A second suite creates an independent fixture prefix for every case.
        run_suite([BEHAVIOR], out, "second-prefix")
        run_suite(COMPATIBILITY, out, "peer-compatibility", guard=module.offline(scratch))
        run_suite([BEHAVIOR], out, "ordinary-after")
        require(snapshot == (tuple(sys.path), tempfile.tempdir, socket.getaddrinfo,
                             socket.socket.connect, io.open, subprocess.Popen),
                "poisoned process globals")
        save(out / "isolation.json", {"globals_restored": True,
                                     "fresh_fixture_prefixes": True})
    else:
        raise RuntimeError("unknown worker case")


def clone_exact(out, head):
    checkout = out / "scan-checkout"
    command(["git", "clone", "--local", "--no-hardlinks", "--no-checkout", str(REPO), str(checkout)],
            REPO, out, "clone")
    command(["git", "checkout", "--detach", head], checkout, out, "detach")
    actual = command(["git", "rev-parse", "HEAD"], checkout, out, "scan-head").decode().strip()
    require(actual == head, "scan head mismatch")
    tree = command(["git", "rev-parse", "HEAD^{tree}"], checkout, out, "scan-tree").decode().strip()
    require(tree == git("rev-parse", "HEAD^{tree}"), "scan tree mismatch")
    paths_raw = command(["git", "ls-files", "-z"], checkout, out, "scan-paths")
    require(paths_raw == subprocess.check_output(["git", "ls-files", "-z"], cwd=REPO),
            "incomplete scan path set")
    paths = [p.decode() for p in paths_raw.split(b"\0") if p]
    require(all(p in paths for p in AUTHORED), "uncommitted authored files")
    require(not command(["git", "status", "--porcelain", "--untracked-files=all"],
                        checkout, out, "scan-status").strip(), "dirty scan checkout")
    for path in paths:
        file = checkout / path
        require(not file.is_symlink(), "symlink in scan checkout")
        require(file.is_file() and digest(file) == digest(REPO / path), "scan content mismatch")
    save(out / "clone-identity.json", {"head": actual, "tree": tree, "tracked_paths": paths,
                                      "tracked_count": len(paths), "complete_bytes_match": True})
    return checkout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=CASES)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--worker", choices=CASES[:4], help=argparse.SUPPRESS)
    parser.add_argument("--run-dir", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        require(args.run_dir is not None, "missing worker root")
        worker(args.worker, safe(args.run_dir))
        return 0
    require(args.case is not None and args.output_root is not None, "case/output-root required")
    root = safe(args.output_root)
    require(not root.is_relative_to(REPO), "evidence must be outside repo")
    root.mkdir(parents=True, exist_ok=True)
    out = root / ("run-" + uuid.uuid4().hex)
    out.mkdir()
    before = identity()
    receipt = {"case": args.case, "argv": sys.argv, "cwd": str(Path.cwd()),
               "interpreter": sys.executable, "identity": before, "exit": None}
    save(out / "receipt.json", receipt)
    try:
        manifest = json.loads((REPO.parent / "SOURCE-MANIFEST.json").read_text())
        base = manifest["head"]
        require(git("show", base + ":" + SOURCE).encode() + b"\n" == (REPO / SOURCE).read_bytes(),
                "source differs from staged base")
        for path in AUTHORED:
            text = (REPO / path).read_text()
            require(not re.search(r"(?<![a-fA-F0-9])[a-fA-F0-9]{40}(?:[a-fA-F0-9]{24})?(?![a-fA-F0-9])", text),
                    "committed hex literal")
            box_paths = ("/" + "home/", "/" + "Users/", "agent-work/" + "candidates", "plans/" + "astra")
            require(not any(token in text for token in box_paths), "box-only authored path")
        if args.case in CASES[:4]:
            command([sys.executable, "-B", str(Path(__file__).resolve()), "--worker", args.case,
                     "--run-dir", str(out)], REPO, out, "tests")
        else:
            require(not git("status", "--porcelain", "--untracked-files=all"), "candidate must be committed clean")
            checkout = clone_exact(out, before["head"])
            if args.case == "credential-scan":
                runtime = manifest["runtime"]
                python = runtime["credential_scan_python3"]
                detector = runtime["detect_secrets"]
                require(Path(python).is_file() and Path(detector).is_file(), "missing declared scan tools")
                raw = command([python, "-B", detector, "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"],
                              checkout, out, "detect-secrets")
                report = out / "secret-report.json"
                report.write_bytes(raw)
                require(isinstance(json.loads(raw)["results"], dict), "invalid actual scanner JSON")
                command([python, "-B", str(checkout / "tools/check_secret_report.py"), str(report)],
                        checkout, out, "check-secret-report")
            else:
                command([sys.executable, "-B", str(checkout / "tools/check_public_tree.py")],
                        checkout, out, "check-public-tree")
        command(["git", "diff", "--check", base, "HEAD"], REPO, out, "diff-check")
        require(identity() == before, "source identity changed during acceptance")
        receipt["exit"] = 0
    except Exception:
        receipt["exit"] = 1
        (out / "failure.txt").write_text(traceback.format_exc())
        raise
    finally:
        receipt["evidence_hashes"] = {str(p.relative_to(out)): digest(p)
                                      for p in out.iterdir() if p.is_file() and p.name != "receipt.json"}
        save(out / "receipt.json", receipt)
        print(json.dumps({"case": args.case, "exit": receipt["exit"], "output": str(out)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
