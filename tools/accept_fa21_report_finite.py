"""FA21 scoped acceptance runner: finite run-report durations (offline only).

Usage: python3 tools/accept_fa21_report_finite.py --case CASE --output-parent DIR

CASE is one of behavior, compatibility, negative-control or credentials. Each
invocation writes into a fresh directory below DIR (outside the repository)
and prints its result path. Exit 0 means the scoped machine checks passed and
the candidate is REVIEW REQUIRED. It is not full-repository CI, semantic
acceptance, human review or a data-source approval. Missing dependencies,
unknown failures and unreviewed scan hits fail closed; nothing is installed.

Guards (installed before any test or product import, in the parent and in
every Python child): socket connect/resolve calls are refused and recorded,
tempfile points at the per-run root, and an audit hook refuses and records
writes outside the allowed roots. Python children started by tests are
re-launched through this file's --child-launch mode, which installs the same
guards before running the requested script or module and appends any
violation to a ledger that the parent reads to fail closed.
"""
import argparse
import hashlib
import io
import json
import os
import platform
import runpy
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LAUNCHER = str(Path(__file__).resolve())
MODULE_NAME = "tools.accept_fa21_report_finite"
AUTHORED = ("campaign_tool/records/run_report.py", "tests/test_fa21_report_finite.py",
            "tools/accept_fa21_report_finite.py")
SUITES = {
    "behavior": ("tests.test_fa21_report_finite",),
    "compatibility": ("tests.test_records_run_report", "tests.test_run_report_check",
                      "tests.records.test_run_coverage"),
    "negative-control": ("tests.test_fa21_report_finite.BrokenControlTests",
                         "tests.test_fa21_report_finite.ChildGuardTests"),
}
SCAN_ARGV = ("scan", "--all-files", "--exclude-files", "^.git/", "--no-verify")
GUARD_MARK = "__fa21_guard__"
NO_ARG_FLAGS = {"-B", "-E", "-s", "-S", "-u", "-I", "-O", "-OO", "-b", "-bb", "-q", "-P"}
ARG_FLAGS = {"-W", "-X"}
WRITE_FLAGS = (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC)
# Audit events that change the filesystem: (path arg index, dir_fd arg index or None).
MUTATING_EVENTS = {
    "os.mkdir": ((0, 2),), "os.rename": ((0, 2), (1, 3)), "os.remove": ((0, 1),),
    "os.rmdir": ((0, 1),), "os.symlink": ((1, 2),), "os.link": ((1, 3),),
    "os.truncate": ((0, None),), "os.chmod": ((0, 2),), "os.chown": ((0, 3),),
    "os.utime": ((0, 3),), "shutil.rmtree": ((0, 1),), "shutil.copyfile": ((1, None),),
    "shutil.copytree": ((1, None),), "shutil.move": ((0, None), (1, None)),
    "os.mkfifo": ((0, 2),), "os.mknod": ((0, 3),),
}
ALWAYS_WRITABLE = ("/dev/null",)

# In-process accounting (parent or child). Children also append to the ledger.
NETWORK_ATTEMPTS = []
WRITE_VIOLATIONS = []
CHILDREN = []
STATE = {"installed": False, "allowed": [], "ledger": None, "root": None,
         "child_ledger": None, "product_modules_at_install": None}
_LOCAL = threading.local()


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


def _record(kind, detail):
    entry = {"pid": os.getpid(), "kind": kind, "detail": detail}
    (NETWORK_ATTEMPTS if kind == "network" else WRITE_VIOLATIONS).append(entry)
    ledger = STATE["ledger"]
    if ledger:
        fd = os.open(ledger, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, (json.dumps(entry, sort_keys=True) + "\n").encode("utf-8"))
        finally:
            os.close(fd)


def _fd_directory(fd):
    """Directory path behind a dir_fd, or None when it cannot be established."""
    try:
        return os.readlink(f"/proc/self/fd/{fd}")
    except OSError:
        pass
    try:
        import fcntl
        if hasattr(fcntl, "F_GETPATH"):
            raw = fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024))
            return os.fsdecode(raw.split(b"\0", 1)[0])
    except (OSError, ImportError):
        pass
    return None


UNRESOLVED = "<unresolved dir_fd path>"


def _resolve(value, dir_fd=None):
    if isinstance(value, int) or value is None:
        return None
    try:
        raw = os.fsdecode(os.fspath(value))
    except TypeError:
        return None
    if isinstance(dir_fd, int) and not os.path.isabs(raw):
        base = _fd_directory(dir_fd)
        if base is None:
            return UNRESOLVED  # never allowed: fails closed
        raw = os.path.join(base, raw)
    return os.path.realpath(os.path.abspath(raw))


def is_allowed_write(path):
    if path in ALWAYS_WRITABLE:
        return True
    return any(path == root or path.startswith(root + os.sep) for root in STATE["allowed"])


def _audit(event, args):
    if getattr(_LOCAL, "busy", False):
        return
    if event == "open":
        flags = args[2] if len(args) > 2 and isinstance(args[2], int) else 0
        mode = args[1] if len(args) > 1 and isinstance(args[1], str) else ""
        if not (flags & WRITE_FLAGS or any(c in mode for c in "wax+")):
            return
        positions = ((0, None),)
    elif event in MUTATING_EVENTS:
        positions = MUTATING_EVENTS[event]
    else:
        return
    _LOCAL.busy = True
    try:
        for index, fd_index in positions:
            if index >= len(args):
                continue
            dir_fd = args[fd_index] if fd_index is not None and fd_index < len(args) else None
            path = _resolve(args[index], dir_fd)
            if path is not None and not is_allowed_write(path):
                _record("write", {"event": event, "path": path})
                raise PermissionError(f"write outside the FA21 per-run root refused ({event})")
    finally:
        _LOCAL.busy = False


def _refuse(name):
    def refused(*args, **kwargs):
        _record("network", {"call": name})
        raise OSError("network disabled by accept_fa21")
    setattr(refused, GUARD_MARK, True)
    return refused


def install_guards(root, allowed, ledger=None):
    """Install offline/tempfile/write guards. Call before any test or product import."""
    if STATE["installed"]:
        raise RuntimeError("accept_fa21 guards already installed")
    root = Path(root)
    tempfile.tempdir = str(root)
    os.environ["TMPDIR"] = str(root)
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    sys.dont_write_bytecode = True
    sys.modules.setdefault(MODULE_NAME, sys.modules[__name__])
    STATE.update(installed=True, root=str(root), ledger=str(ledger) if ledger else None,
                 allowed=[_resolve(p) for p in allowed],
                 product_modules_at_install=sorted(
                     m for m in sys.modules if m.split(".")[0] in ("campaign_tool", "tests")))
    socket.socket.connect = _refuse("socket.connect")
    socket.socket.connect_ex = _refuse("socket.connect_ex")
    socket.create_connection = _refuse("create_connection")
    socket.getaddrinfo = _refuse("getaddrinfo")
    sys.addaudithook(_audit)


def guards_active():
    return {
        "installed": STATE["installed"],
        "socket.connect": getattr(socket.socket.connect, GUARD_MARK, False),
        "socket.connect_ex": getattr(socket.socket.connect_ex, GUARD_MARK, False),
        "create_connection": getattr(socket.create_connection, GUARD_MARK, False),
        "getaddrinfo": getattr(socket.getaddrinfo, GUARD_MARK, False),
        "tempdir": tempfile.tempdir,
    }


def ledger_entries(path):
    """Violations a guarded child recorded; any entry fails the parent closed."""
    path = Path(path)
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def unguarded_children(children):
    """Children neither guarded nor on the fixed non-Python list (git, scanner CLI)."""
    return [c for c in children if c["route"] not in ("wrapped", "already-guarded")
            and c["argv0"] not in ("git", "detect-secrets")]


def guard_failures(network, writes, child_violations, unguarded):
    """Names of guard categories that must fail the parent closed; [] means clean."""
    named = (("network", network), ("write", writes), ("child", child_violations),
             ("unguarded-child", unguarded))
    return [name for name, items in named if items]


def launcher_argv(root, ledger, rest):
    return [sys.executable, "-B", LAUNCHER, "--child-launch", "--root", str(root),
            "--ledger", str(ledger), "--", *rest]


def _is_python(program):
    try:
        found = shutil.which(os.fspath(program)) or os.fspath(program)
    except TypeError:
        return False
    return (os.path.realpath(found) == os.path.realpath(sys.executable)
            or os.path.basename(found).startswith("python"))


def wrap_child_argv(args):
    """Route an unguarded Python child through the guarded launcher."""
    if isinstance(args, (str, bytes)) or not args:
        return args, "not-wrapped"
    args = [os.fspath(a) if not isinstance(a, (str, bytes)) else a for a in args]
    if LAUNCHER in args:
        return args, "already-guarded"
    if not _is_python(args[0]):
        return args, "non-python"
    return launcher_argv(STATE["root"], STATE["child_ledger"], args[1:]), "wrapped"


def install_child_wrapper(child_ledger):
    STATE["child_ledger"] = str(child_ledger)
    original = subprocess.Popen.__init__

    def guarded_init(self, args, *rest, **kwargs):
        if kwargs.get("shell"):
            CHILDREN.append({"argv0": str(args)[:40], "route": "shell-refused"})
            raise PermissionError("accept_fa21 refuses shell children")
        args, route = wrap_child_argv(args)
        program = args if isinstance(args, (str, bytes)) else args[0]
        CHILDREN.append({"argv0": os.path.basename(os.fsdecode(program)), "route": route})
        original(self, args, *rest, **kwargs)

    subprocess.Popen.__init__ = guarded_init


def child_launch(argv):
    """--child-launch --root R --ledger L -- [python flags] (script | -m module) args."""
    parser = argparse.ArgumentParser(prog="accept_fa21 --child-launch")
    parser.add_argument("--root", required=True)
    parser.add_argument("--ledger", required=True)
    split = argv.index("--") if "--" in argv else len(argv)
    opts = parser.parse_args(argv[:split])
    rest = argv[split + 1:]
    root = Path(opts.root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    install_guards(root, [root, opts.ledger], opts.ledger)
    while rest and rest[0] in NO_ARG_FLAGS | ARG_FLAGS:
        rest = rest[2:] if rest[0] in ARG_FLAGS else rest[1:]
    if rest[:1] == ["-m"] and len(rest) > 1:
        sys.argv = [rest[1], *rest[2:]]
        sys.path[0] = os.getcwd()
        runpy.run_module(rest[1], run_name="__main__", alter_sys=True)
        return 0
    if not rest or rest[0].startswith("-"):
        _record("refused-child", {"reason": "inline source or unknown interpreter option"})
        sys.stderr.write("accept_fa21: child refused; only script files and -m modules run\n")
        return 97
    script = os.path.realpath(rest[0])
    sys.argv = rest
    sys.path[0] = os.path.dirname(script)
    runpy.run_path(script, run_name="__main__")
    return 0


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
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--child-launch"]:
        return child_launch(argv[1:])
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--case", required=True,
                        choices=("behavior", "compatibility", "negative-control", "credentials"))
    parser.add_argument("--output-parent", required=True)
    args = parser.parse_args(argv)
    run_dir = fresh_run_dir(args.output_parent, args.case)
    tmp = run_dir / "tmp"
    tmp.mkdir()
    child_ledger = run_dir / "child-ledger.jsonl"
    here = str(Path(__file__).resolve().parent)
    sys.path[:] = [p for p in sys.path if p != here]
    sys.path.insert(0, str(REPO))
    install_guards(tmp, [run_dir])
    install_child_wrapper(child_ledger)
    record = {"case": args.case, "argv": [sys.executable, *sys.argv], "cwd": os.getcwd(),
              "interpreter": sys.executable, "python": platform.python_version(),
              "run_dir": str(run_dir), "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "guards": guards_active(), "before": identity()}
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
    child_violations = ledger_entries(child_ledger)
    unguarded = unguarded_children(CHILDREN)
    record.update(network_attempts=list(NETWORK_ATTEMPTS), write_violations=list(WRITE_VIOLATIONS),
                  child_violations=child_violations, children=list(CHILDREN),
                  unguarded_children=unguarded)
    ok = ok and not guard_failures(NETWORK_ATTEMPTS, WRITE_VIOLATIONS, child_violations, unguarded)
    record["after"] = identity()
    record["exit"] = 0 if ok else 1
    record["verdict"] = "PASS_REVIEW_REQUIRED" if ok else "FAIL"
    record["note"] = "Scoped acceptance only; not full-repository CI or semantic acceptance."
    result_path = run_dir / "result.json"
    result_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary = {k: record.get(k) for k in ("case", "verdict", "exit", "tests_run", "skipped",
                                           "failures", "errors", "direct_control",
                                           "scan_files_with_hits", "stop", "network_attempts",
                                           "write_violations", "child_violations")}
    summary["children"] = len(CHILDREN)
    summary["candidate_head"] = record["after"]["candidate_head"]
    summary["result"] = str(result_path)
    summary["result_sha256"] = sha256_file(result_path)
    print(json.dumps(summary, sort_keys=True))
    return record["exit"]


if __name__ == "__main__":
    raise SystemExit(main())
