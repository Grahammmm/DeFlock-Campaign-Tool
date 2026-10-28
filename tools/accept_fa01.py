"""FA01 acceptance launcher (unsupported mail occurrence kinds in reconcile).

Exactly one suite per invocation; every invocation writes only to a fresh,
uniquely named child of --output-root:

  regression       tests.records.test_fa01_reconcile_kind.Regression (A1)
  compatibility    unchanged tests.records.test_ledger_reconcile (A2)
  control          tests.records.test_fa01_reconcile_kind.GateControl (A3)
  credential-scan  installed detect-secrets with the repository CI arguments,
                   then the unchanged tools/check_secret_report.py (A4)
  public-tree      the unchanged tools/check_public_tree.py (A5)

Test suites run in this fresh process after tempfile.tempdir is redirected
below the invocation directory and an audit hook refuses network, child
processes and writes outside it. That hook is an offline guard for trusted
synthetic tests, not a sandbox. Only the named test modules are loaded (no
discovery) and the imported repository modules are recorded and bounded.
A pass is REVIEW REQUIRED: scoped evidence, not full CI or semantic acceptance.
"""
import argparse
import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
REPO = Path(__file__).resolve().parents[1]
AUTHORED = ("campaign_tool/records/ledger/reconcile.py", "tests/records/test_fa01_reconcile_kind.py",
            "tools/accept_fa01.py")
REFERENCED = ("tests/records/test_ledger_reconcile.py", "tools/check_secret_report.py",
              "tools/check_public_tree.py")
FA01 = "tests.records.test_fa01_reconcile_kind"
SELECTORS = {
    "regression": (FA01 + ".Regression",),
    "compatibility": ("tests.records.test_ledger_reconcile",),
    "control": (FA01 + ".GateControl",),
}
SUITES = (*SELECTORS, "credential-scan", "public-tree")
# Repository modules the test suites may import: the named test modules and the
# ledger package that contains the reconciliation library (its __init__ imports store).
ADMITTED_EXACT = {"campaign_tool", "campaign_tool.records", "tests", "tests.records", FA01,
                  "tests.records.test_ledger_reconcile"}
ADMITTED_PREFIX = "campaign_tool.records.ledger"
CONTROL_BAD_KINDS = ("'unsupported-kind'", "None", "1", "[]")
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
    return {"head": git("rev-parse", "HEAD").strip(), "dirty_files": status.splitlines(),
            "clean_tree": status == "",
            "authored_sha256": {p: sha256_file(REPO / p) for p in AUTHORED if (REPO / p).is_file()},
            "referenced_sha256": {p: sha256_file(REPO / p) for p in REFERENCED}}


def tree_hashes(parent):
    return {str(p.relative_to(parent)): sha256_file(p) for p in sorted(parent.rglob("*"))
            if p.is_file() and not p.is_symlink()}


class OfflineGuard:
    NETWORK = ("socket.connect", "socket.getaddrinfo", "socket.gethostbyname", "socket.bind", "socket.sendto")
    PROCESS = ("subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn", "os.fork", "os.forkpty")
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
        raise PermissionError("FA01 offline guard refused " + event)

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


def repo_modules():
    return sorted(name for name in sys.modules if name.split(".")[0] in ("campaign_tool", "tests"))


def run_tests(suite_name, stream, record):
    import unittest
    record["repo_modules_before_import"] = repo_modules()
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(name)
                               for name in SELECTORS[suite_name])
    selected = [test.id() for test in tests_of(suite)]
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    counts = {"tests_run": result.testsRun, "selected": selected,
              "failures": [t.id() for t, _ in result.failures], "errors": [t.id() for t, _ in result.errors],
              "skipped": [t.id() for t, _ in result.skipped],
              "expected_failures": [t.id() for t, _ in result.expectedFailures],
              "unexpected_successes": [t.id() for t in result.unexpectedSuccesses]}
    record["counts"] = counts
    loaded = repo_modules()
    outside = [m for m in loaded if m not in ADMITTED_EXACT
               and m != ADMITTED_PREFIX and not m.startswith(ADMITTED_PREFIX + ".")]
    record["import_boundary"] = {"fresh_process": record["repo_modules_before_import"] == [],
                                 "repo_modules_loaded": loaded, "outside_admitted": outside}
    reconcile_module = sys.modules.get("campaign_tool.records.ledger.reconcile")
    record["reconcile_module_file"] = getattr(reconcile_module, "__file__", None)
    ok = (result.testsRun > 0 and result.testsRun == len(selected) and not counts["failures"]
          and not counts["errors"] and not counts["skipped"] and not counts["expected_failures"]
          and not counts["unexpected_successes"] and not outside
          and record["import_boundary"]["fresh_process"]
          and record["reconcile_module_file"] == str(REPO / AUTHORED[0]))
    if suite_name == "control":
        outcomes = list(getattr(sys.modules.get(FA01), "CONTROL_OUTCOMES", []))
        record["control_outcomes"] = outcomes
        broken = sorted(o["kind"] for o in outcomes if o["case"] == "bypass" and o["outcome"] == "assertion_failed")
        restored = sorted(o["kind"] for o in outcomes if o["case"] == "restored" and o["outcome"] == "passed")
        ok = ok and broken == sorted(CONTROL_BAD_KINDS) and restored == sorted((*CONTROL_BAD_KINDS, "valid-match"))
        stream.write("== control raw outcomes ==\n" + json.dumps(outcomes, indent=2) + "\n")
    return ok


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
    try:
        results = json.loads(report.read_bytes()).get("results", {})
        raw_hits = sum(len(v) for v in results.values())
    except (ValueError, AttributeError):
        raw_hits = None
    # "potential secrets" is the unchanged repository checker's own count (raw
    # hits minus verified provenance hashes); parsed from its literal summary line.
    match = re.search(rb"potential secrets: (\d+);", check.stdout)
    potential = int(match.group(1)) if match else None
    stream.write("raw detect-secrets hits: " + str(raw_hits) + "; checker potential secrets: "
                 + str(potential) + "\n")
    record["scan"] = {"argv": scan_argv, "cwd": str(REPO), "exit": scan.returncode,
                      "report": str(report), "report_sha256": sha256_file(report),
                      "raw_hits": raw_hits, "potential_secrets": potential}
    record["check"] = {"argv": check_argv, "cwd": str(REPO), "exit": check.returncode}
    return scan.returncode == 0 and check.returncode == 0 and potential == 0


def run_public_tree(stream, record):
    argv = [sys.executable, "-B", str(REPO / "tools/check_public_tree.py")]
    done = subprocess.run(argv, cwd=REPO, capture_output=True, check=False)
    stream.write("== check_public_tree.py stdout ==\n" + done.stdout.decode(errors="replace"))
    stream.write("== check_public_tree.py stderr ==\n" + done.stderr.decode(errors="replace"))
    record["public_tree"] = {"argv": argv, "cwd": str(REPO), "exit": done.returncode,
                             "stdout_sha256": hashlib.sha256(done.stdout).hexdigest()}
    return done.returncode == 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--suite", required=True, choices=SUITES)
    parser.add_argument("--output-root", required=True, type=Path)
    args = parser.parse_args(argv)

    parent = args.output_root.resolve()
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    prior = tree_hashes(parent)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    run_root = parent / "-".join((args.suite, stamp, str(os.getpid()), secrets.token_hex(4)))
    run_root.mkdir(mode=0o700)
    start = identity()
    record = {"label": LABEL, "suite": args.suite, "argv": [sys.executable, *sys.argv], "cwd": os.getcwd(),
              "interpreter": sys.executable, "python": sys.version, "run_root": str(run_root),
              "identity_before": start, "selectors": list(SELECTORS.get(args.suite, ()))}
    stream = io.StringIO()
    guard = OfflineGuard(run_root)
    try:
        if args.suite == "credential-scan":
            passed = run_credentials(run_root, stream, record)
        elif args.suite == "public-tree":
            passed = run_public_tree(stream, record)
        else:
            tmp = run_root / "tmp"
            tmp.mkdir(mode=0o700)
            tempfile.tempdir = str(tmp)
            os.environ["TMPDIR"] = str(tmp)
            if str(REPO) not in sys.path:
                sys.path.insert(0, str(REPO))
            sys.addaudithook(guard)
            guard.active = True
            try:
                passed = run_tests(args.suite, stream, record)
            finally:
                guard.active = False
    except Exception as error:  # recorded and failed closed, never treated as an expected red
        stream.write("unexpected runner fault: " + type(error).__name__ + ": " + str(error)[:300] + "\n")
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
              and start["referenced_sha256"] == end["referenced_sha256"] and start["head"] == end["head"]
              and start["dirty_files"] == end["dirty_files"])
    raw = stream.getvalue().encode()
    (run_root / "stderr.txt").write_bytes(raw)
    record["raw_stderr"] = str(run_root / "stderr.txt")
    record["raw_stderr_sha256"] = hashlib.sha256(raw).hexdigest()
    record["exit"] = 0 if passed else 1
    counts = record.get("counts", {})
    summary = {"suite": args.suite, "exit": record["exit"], "run_root": str(run_root),
               "tests_run": counts.get("tests_run"), "skipped": len(counts.get("skipped", [])) if counts else None,
               "expected_failures": len(counts.get("expected_failures", [])) if counts else None,
               "failing_ids": sorted(counts.get("failures", []) + counts.get("errors", [])) if counts else None,
               "head": end["head"], "clean_tree": end["clean_tree"], "label": LABEL}
    out = (json.dumps(summary, sort_keys=True) + "\n").encode()
    (run_root / "stdout.txt").write_bytes(out)
    record["raw_stdout"] = str(run_root / "stdout.txt")
    record["raw_stdout_sha256"] = hashlib.sha256(out).hexdigest()
    (run_root / "receipt.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    sys.stderr.write(stream.getvalue())
    sys.stdout.write(out.decode())
    return record["exit"]


if __name__ == "__main__":
    raise SystemExit(main())
