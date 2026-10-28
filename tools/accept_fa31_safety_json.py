"""Scoped FA31 acceptance runner (saved safety JSON duplicate members).

One case per invocation, each writing only to a new exclusive directory below
--output-parent:

  behavior          A1/A2/A3/A5/A6 in tests/test_fa31_safety_json.py
  negative-control  A4 only (last-member-wins decoder double)
  compatibility     unchanged tests.records.test_run_safety.SafetyTests
  credentials       installed detect-secrets scan, then the unchanged
                    tools/check_secret_report.py on that exact JSON report

Before any repository import the temporary root is redirected into the run
directory and an audit hook refuses network, child processes and writes outside
it. That hook is an offline guard for trusted synthetic tests, not a sandbox.
A pass is REVIEW REQUIRED: scoped evidence, not full CI or semantic acceptance.
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
import subprocess
import sys
import tempfile

REPO = Path(__file__).resolve().parents[1]
AUTHORED = ("campaign_tool/records/run_safety.py", "tests/test_fa31_safety_json.py",
            "tools/accept_fa31_safety_json.py")
REFERENCED = ("tests/records/test_run_safety.py", "tools/check_secret_report.py", "tools/check_public_tree.py")
MODULE = "tests.test_fa31_safety_json.FA31SafetyJsonTests."
SELECTORS = {
    "behavior": tuple(MODULE + name for name in ("test_valid_round_trip", "test_malformed_refusal",
                                                 "test_boundary_replay", "test_consumer_contract",
                                                 "test_preservation")),
    "negative-control": (MODULE + "test_regression_control",),
    "compatibility": ("tests.records.test_run_safety.SafetyTests",),
}
CASES = (*SELECTORS, "credentials")
LABEL = "REVIEW REQUIRED - scoped offline acceptance, not full-repository CI or semantic acceptance"
WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def git(*args):
    done = subprocess.run(["git", *args], cwd=REPO, capture_output=True, check=False)
    if done.returncode != 0:
        raise RuntimeError("git " + args[0] + " failed")
    return done.stdout.decode()


def identity():
    status = git("status", "--porcelain", "--untracked-files=all")
    return {"head": git("rev-parse", "HEAD").strip(), "status": status.splitlines(), "clean_tree": status == "",
            "authored_sha256": {p: sha256_file(REPO / p) for p in AUTHORED},
            "referenced_sha256": {p: sha256_file(REPO / p) for p in REFERENCED}}


def tree_hashes(parent):
    return {str(p.relative_to(parent)): sha256_file(p) for p in sorted(parent.rglob("*"))
            if p.is_file() and not p.is_symlink()}


class OfflineGuard:
    NETWORK = ("socket.connect", "socket.getaddrinfo", "socket.gethostbyname", "socket.bind", "socket.sendto")
    PROCESS = ("subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn", "os.fork", "os.forkpty")
    # event -> ((path argument index, dir_fd argument index or None), ...)
    PATHS = {"os.mkdir": ((0, 2),), "os.remove": ((0, 1),), "os.rmdir": ((0, 1),),
             "os.rename": ((0, 2), (1, 3)), "os.replace": ((0, 2), (1, 3)), "os.chmod": ((0, 2),),
             "os.chown": ((0, 3),), "os.symlink": ((1, 2),), "os.link": ((0, 2), (1, 3)),
             "os.truncate": ((0, None),), "shutil.rmtree": ((0, 1),)}

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.active = False
        self.refusals = []

    def inside(self, value, dir_fd=None):
        if isinstance(value, int):
            return True
        try:
            text = os.fsdecode(value)
        except TypeError:
            return False
        if text.startswith("file:"):
            text = text[5:].split("?", 1)[0]
        path = Path(text)
        if not path.is_absolute():
            base = os.readlink("/proc/self/fd/" + str(dir_fd)) if isinstance(dir_fd, int) else os.getcwd()
            path = Path(base) / path
        resolved = Path(os.path.realpath(path))
        return resolved == self.root or resolved.is_relative_to(self.root)

    def refuse(self, event, detail):
        self.refusals.append({"event": event, "detail": str(detail)[:200]})
        raise PermissionError("FA31 offline guard refused " + event)

    def __call__(self, event, args):
        if not self.active:
            return
        if event in self.NETWORK:
            self.refuse(event, "")
        elif event in self.PROCESS:
            self.refuse(event, args[:1])
        elif event == "open":
            path, mode, flags = args
            writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or \
                (isinstance(flags, int) and flags & WRITE_FLAGS)
            if writing and path is not None and not self.inside(path):
                self.refuse(event, path)
        elif event == "sqlite3.connect":
            if args[0] not in (":memory:", b":memory:") and not self.inside(args[0]):
                self.refuse(event, args[0])
        elif event in self.PATHS:
            for index, fd_index in self.PATHS[event]:
                if index >= len(args) or args[index] is None:
                    continue
                dir_fd = args[fd_index] if fd_index is not None and fd_index < len(args) else None
                if not self.inside(args[index], dir_fd):
                    self.refuse(event, args[index])


def tests_of(suite):
    import unittest
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from tests_of(item)
        else:
            yield item


def run_tests(case, stream):
    import unittest
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(name) for name in SELECTORS[case])
    selected = [test.id() for test in tests_of(suite)]
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    counts = {"tests_run": result.testsRun, "selected": selected,
              "failures": [t.id() for t, _ in result.failures], "errors": [t.id() for t, _ in result.errors],
              "skipped": [t.id() for t, _ in result.skipped],
              "expected_failures": [t.id() for t, _ in result.expectedFailures],
              "unexpected_successes": [t.id() for t in result.unexpectedSuccesses]}
    ok = (result.testsRun > 0 and result.testsRun == len(selected) and not counts["failures"]
          and not counts["errors"] and not counts["skipped"] and not counts["expected_failures"]
          and not counts["unexpected_successes"])
    if case != "compatibility":
        ok = ok and selected == list(SELECTORS[case])
    return ok, counts


def scanner_path():
    found = shutil.which("detect-secrets")
    if found:
        return found
    sibling = Path(sys.executable).parent / "detect-secrets"
    return str(sibling) if sibling.is_file() and os.access(sibling, os.X_OK) else None


def run_credentials(run_root, stream, record):
    scanner = scanner_path()
    record["detect_secrets"] = scanner
    if scanner is None:
        record["stop_reason"] = "missing_dependency:detect-secrets"
        stream.write("detect-secrets is not installed for this interpreter; nothing installed or substituted\n")
        return False
    report = run_root / "secret-scan.json"
    scan_argv = [scanner, "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"]
    with report.open("xb") as out:
        scan = subprocess.run(scan_argv, cwd=REPO, stdout=out, stderr=subprocess.PIPE, check=False)
    check_argv = [sys.executable, "-B", str(REPO / "tools/check_secret_report.py"), str(report)]
    check = subprocess.run(check_argv, cwd=REPO, capture_output=True, check=False)
    stream.write("== detect-secrets stderr ==\n" + scan.stderr.decode(errors="replace"))
    stream.write("== check_secret_report.py stdout ==\n" + check.stdout.decode(errors="replace"))
    stream.write("== check_secret_report.py stderr ==\n" + check.stderr.decode(errors="replace"))
    record["scan"] = {"argv": scan_argv, "cwd": str(REPO), "exit": scan.returncode,
                      "report": str(report), "report_sha256": sha256_file(report)}
    record["check"] = {"argv": check_argv, "cwd": str(REPO), "exit": check.returncode}
    return scan.returncode == 0 and check.returncode == 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--case", required=True, choices=CASES)
    parser.add_argument("--output-parent", required=True, type=Path)
    args = parser.parse_args(argv)

    parent = args.output_parent.resolve()
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    prior = tree_hashes(parent)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    run_root = parent / "-".join((args.case, stamp, str(os.getpid()), secrets.token_hex(4)))
    run_root.mkdir(mode=0o700)
    start = identity()
    record = {"label": LABEL, "case": args.case, "argv": [sys.executable, *sys.argv], "cwd": os.getcwd(),
              "python": sys.version, "run_root": str(run_root), "identity_before": start,
              "selectors": list(SELECTORS.get(args.case, ()))}
    stream = io.StringIO()
    guard = OfflineGuard(run_root)
    try:
        if args.case == "credentials":
            passed = run_credentials(run_root, stream, record)
        else:
            tmp = run_root / "tmp"
            tmp.mkdir(mode=0o700)
            tempfile.tempdir = str(tmp)
            os.environ["TMPDIR"] = str(tmp)
            os.environ["FA31_RUN_ROOT"] = str(run_root)
            sys.dont_write_bytecode = True
            if str(REPO) not in sys.path:
                sys.path.insert(0, str(REPO))
            sys.addaudithook(guard)
            guard.active = True
            try:
                passed, record["counts"] = run_tests(args.case, stream)
            finally:
                guard.active = False
    except Exception as error:  # recorded and failed closed, never treated as an expected red
        stream.write("unexpected runner fault: " + type(error).__name__ + "\n")
        record["runner_fault"] = type(error).__name__
        passed = False
    record["guard_refusals"] = guard.refusals
    end = identity()
    record["identity_after"] = end
    after = tree_hashes(parent)
    record["prior_evidence_files"] = len(prior)
    record["prior_evidence_unchanged"] = all(after.get(k) == v for k, v in prior.items())
    passed = (passed and not guard.refusals and record["prior_evidence_unchanged"]
              and start["authored_sha256"] == end["authored_sha256"]
              and start["referenced_sha256"] == end["referenced_sha256"] and start["head"] == end["head"])
    raw = stream.getvalue().encode()
    (run_root / "output.txt").write_bytes(raw)
    record["raw_output"] = str(run_root / "output.txt")
    record["raw_output_sha256"] = hashlib.sha256(raw).hexdigest()
    record["exit"] = 0 if passed else 1
    (run_root / "result.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    sys.stdout.write(stream.getvalue())
    sys.stdout.write(json.dumps({"case": args.case, "exit": record["exit"], "run_root": str(run_root),
                                 "tests_run": record.get("counts", {}).get("tests_run"),
                                 "head": end["head"], "clean_tree": end["clean_tree"],
                                 "result_sha256": sha256_file(run_root / "result.json"), "label": LABEL}) + "\n")
    return record["exit"]


if __name__ == "__main__":
    raise SystemExit(main())
