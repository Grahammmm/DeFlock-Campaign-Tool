"""FA1301 scoped acceptance helper: base ledger schema gate.

Cases: behavior, compatibility, negative-control, isolation, credential-scan,
public-tree. Every invocation writes only below a fresh unique child of
--output-root, which must lie outside the repository with no symlinked
ancestry. Test suites run in file-launched children that establish the
offline boundary (no bytecode, redirected temporary roots, network/process/
live-path sentinels) before any application import. Success here is scoped
and REVIEW REQUIRED; it is not full-repository CI or semantic acceptance.
"""
import sys

sys.dont_write_bytecode = True

import argparse  # noqa: E402
import hashlib  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
from pathlib import Path  # noqa: E402
import re  # noqa: E402
import secrets  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402
import urllib.parse  # noqa: E402

HELPER = Path(os.path.abspath(__file__))
REPO = HELPER.parents[1]
PROBE = REPO / "tests" / "fixtures" / "fa1301" / "ordinary_probe.py"
BOUNDARY_ATTR = "FA1301_BOUNDARY_RUN_DIR"  # set on this __main__ module only after the boundary is installed
CASES = ("behavior", "compatibility", "negative-control", "isolation", "credential-scan", "public-tree")
MODULE = "tests.records.test_fa1301"
SUITES = {
    "behavior": [MODULE + ".Behavior"],
    "compatibility": [
        "tests.records.test_ledger.LedgerTests.test_immutable_evidence_rejects_updates",
        "tests.records.test_ledger.LedgerTests.test_original_replace_refused_even_without_recursive_triggers",
        "tests.records.test_ledger.LedgerTests.test_legacy_row_and_history_replacement_refused",
        "tests.records.test_ledger.LedgerTests.test_writer_connections_enable_replacement_protection",
    ],
    "isolation": [MODULE + ".Isolation"],
    "gatecontrol": [MODULE + ".GateControl"],
}
EXPECTED_COUNTS = {"compatibility": 4}
DECIDING_TEST = MODULE + ".GateControl.test_missing_originals_update_guard_refuses_before_yield"
DECIDING_MARK = "FA1301-GATE-MISSING: ledger yielded a connection and UPDATE originals succeeded"
GATE_LINE = "        _verify_base_schema(con)\n"
DISABLED_LINE = "        pass  # FA1301 negative control: base-schema comparison disabled\n"
CHILD_TIMEOUT = 1200
EXIT_REFUSED = 3


class Refused(Exception):
    pass


def sha_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def sha_bytes(data):
    return hashlib.sha256(data).hexdigest()


def checked_root(value, repo):
    """Refuse symlinked ancestry or a repository-contained root BEFORE any write."""
    path = Path(os.path.abspath(value))
    for candidate in (path, *path.parents):
        if os.path.islink(candidate):
            raise Refused("output root ancestry contains a symlink: %s" % candidate)
    real_repo = os.path.realpath(repo)
    if path == Path(real_repo) or Path(real_repo) in path.parents or Path(repo) in path.parents or path == Path(repo):
        raise Refused("output root must be outside the repository")
    return path


def fresh_dir(root, label):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    run = root / ("%s-%s-%d-%s" % (label, stamp, os.getpid(), secrets.token_hex(4)))
    os.mkdir(run, 0o700)  # exclusive: never reuse earlier evidence
    if os.path.islink(run) or not run.is_dir():
        raise Refused("run directory is not a fresh directory")
    return run


# ---------------------------------------------------------------- child side
_HITS = []
_STATE = {"phase": "workload", "busy": False}


def _install_boundary(run_dir, repo):
    """Offline boundary for a dedicated child process, installed before app imports."""
    import socket
    import tempfile

    run_real = os.path.realpath(run_dir)
    tmp = Path(run_dir) / "tmp"
    home = Path(run_dir) / "home"
    for item in (tmp, home):
        item.mkdir(mode=0o700, exist_ok=True)
    os.environ["TMPDIR"] = os.environ["TMP"] = os.environ["TEMP"] = str(tmp)
    os.environ["HOME"] = str(home)
    tempfile.tempdir = str(tmp)
    read_roots = {run_real, os.path.realpath(repo)}
    for value in (sys.prefix, sys.base_prefix, sys.exec_prefix, sys.base_exec_prefix):
        read_roots.add(os.path.realpath(value))
    for value in sys.path:
        if value and os.path.isdir(value):
            read_roots.add(os.path.realpath(value))
    read_roots.update({"/usr/lib", "/usr/share/zoneinfo", "/proc/self"})
    read_files = {"/dev/null", "/dev/urandom", "/etc/localtime"}
    allowed_exec = (os.path.realpath(sys.executable), (str(HELPER), str(PROBE)))

    def under(path, roots):
        return any(path == root or path.startswith(root.rstrip("/") + "/") for root in roots)

    def record(kind, detail):
        _HITS.append({"phase": _STATE["phase"], "kind": kind, "detail": str(detail)[:300]})
        raise PermissionError("FA1301 sentinel blocked %s: %s" % (kind, detail))

    def hook(event, args):
        if _STATE["busy"]:
            return
        _STATE["busy"] = True
        try:
            if event == "open":
                target, mode, flags = args
                if isinstance(target, int):
                    return
                if isinstance(target, bytes):
                    target = os.fsdecode(target)
                real = os.path.realpath(os.path.abspath(str(target)))
                writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                    isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC))
                if writing and not under(real, {run_real}):
                    record("file-write", real)
                if not writing and real not in read_files and not under(real, read_roots):
                    record("file-read", real)
            elif event == "sqlite3.connect":
                database = args[0]
                if isinstance(database, bytes):
                    database = os.fsdecode(database)
                database = str(database)
                if database in (":memory:", ""):
                    return
                if database.startswith("file:"):
                    parsed = urllib.parse.urlparse(database)
                    if parsed.path in ("", ":memory:") or "memory" in urllib.parse.parse_qs(parsed.query).get("mode", []):
                        return
                    database = urllib.parse.unquote(parsed.path)
                real = os.path.realpath(os.path.abspath(database))
                if not under(real, {run_real}):
                    record("sqlite-path", real)
            elif event in ("socket.connect", "socket.getaddrinfo", "socket.sendto", "socket.bind",
                           "urllib.Request", "http.client.connect", "ftplib.connect", "smtplib.connect"):
                record("network", event)
            elif event == "subprocess.Popen":
                executable, argv = args[0], list(args[1] or [])
                argv0 = os.path.realpath(str(executable or (argv[0] if argv else "")))
                if not (len(argv) >= 3 and argv0 == allowed_exec[0] and argv[1] == "-B"
                        and os.path.abspath(str(argv[2])) in allowed_exec[1]):
                    record("process", argv[:3])
            elif event in ("os.system", "os.exec", "os.posix_spawn", "os.spawn", "os.fork", "pty.spawn"):
                record("process", event)
            elif event in ("os.remove", "os.rmdir", "os.rename", "shutil.rmtree", "os.chmod", "os.symlink", "os.link"):
                positions = {"os.remove": ((0, 1),), "os.rmdir": ((0, 1),), "shutil.rmtree": ((0, 1),),
                             "os.chmod": ((0, 2),), "os.rename": ((0, 2), (1, 3)),
                             "os.symlink": ((1, 2),), "os.link": ((1, 3),)}[event]
                for path_index, fd_index in positions:
                    target = args[path_index] if len(args) > path_index else None
                    dir_fd = args[fd_index] if len(args) > fd_index else None
                    if target is None or isinstance(target, int):
                        continue
                    target = os.fsdecode(target)
                    if isinstance(dir_fd, int) and not os.path.isabs(target):
                        target = os.path.join(os.readlink("/proc/self/fd/%d" % dir_fd), target)
                    real = os.path.realpath(os.path.abspath(target))
                    if not under(real, {run_real}):
                        record("file-mutation", "%s %s" % (event, real))
        finally:
            _STATE["busy"] = False

    sys.addaudithook(hook)  # dedicated child process only; never peer discovery

    def blocked(*args, **kwargs):
        _HITS.append({"phase": _STATE["phase"], "kind": "network", "detail": "socket API"})
        raise PermissionError("FA1301 sentinel blocked network")

    socket.create_connection = blocked
    socket.getaddrinfo = blocked
    socket.socket.connect = blocked
    socket.socket.connect_ex = blocked


def _child_modules(repo):
    import campaign_tool
    from campaign_tool.records.ledger import store
    return {"campaign_tool": os.path.abspath(campaign_tool.__file__), "store": os.path.abspath(store.__file__)}


def _under_repo(path, repo):
    return os.path.realpath(path).startswith(os.path.realpath(repo) + os.sep)


def child_main(suite, run_dir):
    run_dir = Path(os.path.abspath(run_dir))
    for candidate in (run_dir, *run_dir.parents):
        if os.path.islink(candidate):
            print("refused: child run directory ancestry contains a symlink", file=sys.stderr)
            return EXIT_REFUSED
    if not run_dir.is_dir():
        print("refused: child run directory missing", file=sys.stderr)
        return EXIT_REFUSED
    repo = REPO
    sys.path[:] = [str(repo)] + [p for p in sys.path if p and os.path.abspath(p) not in (str(repo), os.getcwd())]
    _install_boundary(run_dir, repo)
    # In-process marker for tests.records.test_fa1301: application tests run only when it is present.
    setattr(sys.modules[__name__], BOUNDARY_ATTR, os.path.realpath(run_dir))
    result_path = run_dir / "child-result.json"
    if suite == "probe":
        return _probe(run_dir, repo, result_path)
    if suite not in SUITES:
        print("refused: unknown child suite", file=sys.stderr)
        return 2
    import tempfile
    import unittest
    modules = _child_modules(repo)
    loader = unittest.TestLoader()
    tests = loader.loadTestsFromNames(SUITES[suite])
    selected = tests.countTestCases()
    stream = io.StringIO()
    outcome = unittest.TextTestRunner(stream=stream, verbosity=2).run(tests)
    test_module = sys.modules.get(MODULE)
    data = {
        "suite": suite, "selectors": SUITES[suite], "selected": selected, "run": outcome.testsRun,
        "failures": [[case.id(), text] for case, text in outcome.failures],
        "errors": [[case.id(), text] for case, text in outcome.errors],
        "skipped": [[case.id(), text] for case, text in outcome.skipped],
        "expected_failures": len(outcome.expectedFailures),
        "unexpected_successes": len(outcome.unexpectedSuccesses),
        "modules": modules, "test_module": getattr(test_module, "__file__", None),
        "boundary": getattr(sys.modules[__name__], BOUNDARY_ATTR, None),
        "tempdir": os.path.realpath(tempfile.gettempdir()),
        "dont_write_bytecode": bool(sys.flags.dont_write_bytecode and sys.dont_write_bytecode),
        "sentinel_hits": _HITS, "runner_output": stream.getvalue(),
    }
    result_path.write_text(json.dumps(data, indent=2, sort_keys=True))
    sys.stdout.write(stream.getvalue())
    ok = (outcome.wasSuccessful() and selected > 0 and outcome.testsRun == selected and not outcome.skipped
          and not outcome.unexpectedSuccesses and not outcome.expectedFailures and not _HITS
          and all(_under_repo(path, repo) for path in modules.values()))
    return 0 if ok else 1


def _probe(run_dir, repo, result_path):
    """Isolation probe: real ledger workload, then deliberately blocked controls."""
    import socket
    import sqlite3
    import tempfile
    modules = _child_modules(repo)
    from campaign_tool.records.ledger import store
    data = {"modules": modules, "dont_write_bytecode": bool(sys.flags.dont_write_bytecode and sys.dont_write_bytecode),
            "tempdir": tempfile.gettempdir(), "home": os.environ.get("HOME"), "controls": {}}
    work = Path(tempfile.mkdtemp(prefix="probe-"))
    work.chmod(0o700)
    ledger_path = work / "ledger" / "ledger.sqlite"
    store.initialize(ledger_path)
    with store.ledger(ledger_path, readonly=True) as con:
        data["workload_tables"] = con.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
    data["workload_hits"] = list(_HITS)
    _STATE["phase"] = "control"
    outside = os.environ.get("FA1301_PROBE_OUTSIDE_FILE", "")
    controls = {
        "network": lambda: socket.create_connection(("127.0.0.1", 9), timeout=1),
        "network_raw": lambda: socket.socket().connect(("127.0.0.1", 9)),
        "process": lambda: subprocess.run(["/bin/true"], check=False),
        "outside_read": lambda: open(outside, "rb").close(),
        "outside_sqlite": lambda: sqlite3.connect(outside).close(),
    }
    for name, action in controls.items():
        before = len(_HITS)
        try:
            action()
            data["controls"][name] = {"blocked": False}
        except PermissionError as exc:
            data["controls"][name] = {"blocked": len(_HITS) > before, "message": str(exc)[:200]}
        except Exception as exc:  # noqa: BLE001 - recorded, judged by the test
            data["controls"][name] = {"blocked": False, "error": type(exc).__name__}
    data["control_hits"] = _HITS[len(data["workload_hits"]):]
    result_path.write_text(json.dumps(data, indent=2, sort_keys=True))
    return 0


# --------------------------------------------------------------- parent side
def run_command(argv, cwd, run_dir, label, env=None):
    started = time.time()
    proc = subprocess.run([str(a) for a in argv], cwd=str(cwd), capture_output=True, timeout=CHILD_TIMEOUT, env=env)
    (run_dir / (label + ".stdout")).write_bytes(proc.stdout)
    (run_dir / (label + ".stderr")).write_bytes(proc.stderr)
    return {"label": label, "argv": [str(a) for a in argv], "cwd": str(cwd), "exit": proc.returncode,
            "seconds": round(time.time() - started, 3),
            "stdout_sha256": sha_bytes(proc.stdout), "stderr_sha256": sha_bytes(proc.stderr),
            "stdout": proc.stdout.decode("utf-8", "replace"), "stderr": proc.stderr.decode("utf-8", "replace")}


def child_env(run_dir):
    return {"HOME": str(run_dir / "home"), "TMPDIR": str(run_dir / "tmp"), "LC_ALL": "C.UTF-8", "PATH": "/usr/bin:/bin"}


def launch_child(helper, repo, suite, run_dir, label):
    child_dir = run_dir / label
    os.mkdir(child_dir, 0o700)
    for name in ("home", "tmp"):
        os.mkdir(child_dir / name, 0o700)
    record = run_command([sys.executable, "-B", helper, "--child", suite, "--run-dir", child_dir], repo, run_dir,
                         label, env=child_env(child_dir))
    result_file = child_dir / "child-result.json"
    record["result"] = json.loads(result_file.read_text()) if result_file.is_file() else None
    record["result_sha256"] = sha_file(result_file) if result_file.is_file() else None
    return record


def judge_suite(record, repo, expected=None):
    problems = []
    result = record["result"]
    if record["exit"] != 0:
        problems.append("child exit %s" % record["exit"])
    if result is None:
        return problems + ["child result missing"]
    if result["selected"] <= 0 or result["run"] != result["selected"]:
        problems.append("selected/run count %s/%s" % (result["selected"], result["run"]))
    if expected is not None and result["run"] != expected:
        problems.append("expected %d tests, ran %d" % (expected, result["run"]))
    for key in ("failures", "errors", "skipped"):
        if result[key]:
            problems.append("%s: %s" % (key, [item[0] for item in result[key]]))
    if result["unexpected_successes"] or result["expected_failures"]:
        problems.append("expected-failure bookkeeping present")
    if result["sentinel_hits"]:
        problems.append("sentinel hits: %s" % result["sentinel_hits"])
    if not result["dont_write_bytecode"]:
        problems.append("bytecode writes not disabled")
    for name, path in result["modules"].items():
        if not _under_repo(path, repo):
            problems.append("module %s outside candidate: %s" % (name, path))
    return problems


def case_suite(case, run_dir, evidence):
    record = launch_child(HELPER, REPO, case, run_dir, "child-" + case)
    evidence["commands"].append(record)
    return judge_suite(record, REPO, EXPECTED_COUNTS.get(case))


def case_negative_control(run_dir, evidence):
    copy = run_dir / "negative-copy"
    copy.mkdir(mode=0o700)
    shutil.copytree(REPO / "campaign_tool", copy / "campaign_tool", ignore=shutil.ignore_patterns("__pycache__"))
    for relative in ("tests/__init__.py", "tests/records/__init__.py", "tests/records/test_fa1301.py",
                     "tools/accept_fa1301.py"):
        (copy / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / relative, copy / relative)
    target = copy / "campaign_tool/records/ledger/store.py"
    original = target.read_bytes()
    repo_store = sha_file(REPO / "campaign_tool/records/ledger/store.py")
    text = original.decode("utf-8")
    problems = []
    if sha_bytes(original) != repo_store:
        return ["copy store.py differs from candidate before mutation"]
    if text.count(GATE_LINE) != 1:
        return ["gate call line occurs %d times; cannot isolate the deciding gate" % text.count(GATE_LINE)]
    target.write_text(text.replace(GATE_LINE, DISABLED_LINE))
    evidence["mutation"] = {"file": "campaign_tool/records/ledger/store.py", "removed": GATE_LINE.strip(),
                            "inserted": DISABLED_LINE.strip(), "original_sha256": repo_store,
                            "mutated_sha256": sha_file(target)}
    copy_helper = copy / "tools/accept_fa1301.py"
    broken = launch_child(copy_helper, copy, "gatecontrol", run_dir, "child-broken")
    evidence["commands"].append(broken)
    result = broken["result"]
    if broken["exit"] == 0 or result is None:
        problems.append("broken control did not fail (exit %s)" % broken["exit"])
    else:
        failed = [item[0] for item in result["failures"]]
        if failed != [DECIDING_TEST]:
            problems.append("broken control failures %s != [%s]" % (failed, DECIDING_TEST))
        elif DECIDING_MARK not in result["failures"][0][1]:
            problems.append("broken control failure lacks the precise yielded-update assertion")
        if result["errors"] or result["skipped"] or result["run"] != result["selected"] or result["run"] < 2:
            problems.append("broken control errors/skips/count unexpected")
        if result["sentinel_hits"]:
            problems.append("broken control sentinel hits")
        if not all(_under_repo(path, copy) for path in result["modules"].values()):
            problems.append("broken control did not import the disposable copy")
    target.write_bytes(original)
    evidence["restored_sha256"] = sha_file(target)
    if evidence["restored_sha256"] != repo_store:
        problems.append("restore did not reproduce candidate store.py")
    restored = launch_child(copy_helper, copy, "gatecontrol", run_dir, "child-restored")
    evidence["commands"].append(restored)
    problems += ["restored: " + item for item in judge_suite(restored, copy)]
    candidate = launch_child(HELPER, REPO, "gatecontrol", run_dir, "child-candidate")
    evidence["commands"].append(candidate)
    problems += ["candidate: " + item for item in judge_suite(candidate, REPO)]
    return problems


def case_credential_scan(run_dir, evidence):
    manifest_path = REPO.parent / "SOURCE-MANIFEST.json"
    if not manifest_path.is_file():
        return ["staging failure: SOURCE-MANIFEST.json missing"]
    evidence["source_manifest_sha256"] = sha_file(manifest_path)
    manifest = json.loads(manifest_path.read_text())
    runtime = manifest.get("runtime", {})
    scan_python, detector = runtime.get("credential_scan_python3"), runtime.get("detect_secrets")
    if not scan_python or not detector or not os.path.isfile(scan_python) or not os.path.isfile(detector):
        return ["staging failure: credential scan runtime missing"]
    scan = run_command([scan_python, detector, "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"],
                       REPO, run_dir, "detect-secrets")
    report = run_dir / "detect-secrets-report.json"
    report.write_bytes((run_dir / "detect-secrets.stdout").read_bytes())
    scan["stdout"] = "<saved as detect-secrets-report.json>"
    evidence["commands"].append(scan)
    problems = [] if scan["exit"] == 0 else ["detect-secrets exit %s" % scan["exit"]]
    try:
        findings = sum(len(v) for v in json.loads(report.read_text())["results"].values())
    except (ValueError, KeyError, AttributeError):
        return problems + ["detect-secrets report unreadable"]
    evidence["raw_findings"] = findings
    check = run_command([scan_python, REPO / "tools/check_secret_report.py", report], REPO, run_dir, "check-secret-report")
    evidence["commands"].append(check)
    if check["exit"] != 0:
        problems.append("check_secret_report exit %s" % check["exit"])
    match = re.search(r"potential secrets: (\d+);", check["stdout"])
    if not match or int(match.group(1)) != 0:
        problems.append("potential secrets not zero")
    return problems


def case_public_tree(run_dir, evidence):
    interpreter = REPO / ".venv/bin/python3"
    if not os.path.isfile(interpreter):
        return ["staging failure: repo .venv interpreter missing"]
    record = run_command([interpreter, REPO / "tools/check_public_tree.py"], REPO, run_dir, "check-public-tree")
    evidence["commands"].append(record)
    problems = [] if record["exit"] == 0 else ["check_public_tree exit %s" % record["exit"]]
    if "violation" in (record["stdout"] + record["stderr"]).lower() and record["exit"] != 0:
        problems.append("public-tree violations reported")
    return problems


def identity():
    def git(*args):
        proc = subprocess.run(["git", *args], cwd=REPO, capture_output=True)
        return proc.stdout
    files = {}
    for relative in ("campaign_tool/records/ledger/store.py", "tests/records/test_fa1301.py", "tools/accept_fa1301.py",
                     "tests/fixtures/fa1301/ordinary_probe.py"):
        path = REPO / relative
        files[relative] = sha_file(path) if path.is_file() else None
    manifest = REPO.parent / "SOURCE-MANIFEST.json"
    return {"head": git("rev-parse", "HEAD").decode().strip(), "status": git("status", "--porcelain").decode(),
            "dirty_diff_sha256": sha_bytes(git("diff", "HEAD", "--binary")), "files": files,
            "source_manifest_sha256": sha_file(manifest) if manifest.is_file() else None,
            "interpreter": sys.executable, "python": sys.version, "helper": str(HELPER)}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--child"]:
        if len(argv) != 4 or argv[2] != "--run-dir":
            print("usage: --child SUITE --run-dir DIR", file=sys.stderr)
            return 2
        return child_main(argv[1], argv[3])
    parser = argparse.ArgumentParser(description="FA1301 scoped acceptance helper")
    parser.add_argument("--case", required=True)
    parser.add_argument("--output-root", required=True)
    args = parser.parse_args(argv)
    if args.case not in CASES:
        print("refused: unknown case %r; supported: %s" % (args.case, ", ".join(CASES)), file=sys.stderr)
        return 2
    try:
        root = checked_root(args.output_root, REPO)
        run_dir = fresh_dir(root, args.case)
    except Refused as exc:
        print("refused: %s" % exc, file=sys.stderr)
        return EXIT_REFUSED
    evidence = {"case": args.case, "argv": [sys.executable, str(HELPER), *argv], "cwd": os.getcwd(),
                "run_dir": str(run_dir), "identity": identity(), "commands": [],
                "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    try:
        if args.case in ("behavior", "compatibility", "isolation"):
            problems = case_suite(args.case, run_dir, evidence)
        elif args.case == "negative-control":
            problems = case_negative_control(run_dir, evidence)
        elif args.case == "credential-scan":
            problems = case_credential_scan(run_dir, evidence)
        else:
            problems = case_public_tree(run_dir, evidence)
    except Exception as exc:  # noqa: BLE001 - any harness fault is a failed row
        problems = ["harness error: %s: %s" % (type(exc).__name__, exc)]
    evidence["problems"] = problems
    evidence["verdict"] = "PASS_REVIEW_REQUIRED" if not problems else "FAIL"
    evidence["identity_after"] = identity()
    if evidence["identity_after"] != evidence["identity"]:
        evidence["problems"].append("candidate identity changed during the run")
        evidence["verdict"] = "FAIL"
    out = run_dir / "evidence.json"
    out.write_text(json.dumps(evidence, indent=2, sort_keys=True))
    for record in evidence["commands"]:
        result = record.get("result") or {}
        print("%s exit=%s selected=%s run=%s failures=%d errors=%d skipped=%d" % (
            record["label"], record["exit"], result.get("selected"), result.get("run"),
            len(result.get("failures", [])), len(result.get("errors", [])), len(result.get("skipped", []))))
    print("evidence: %s sha256=%s" % (out, sha_file(out)))
    print("verdict: %s%s" % (evidence["verdict"], "" if not problems else " " + json.dumps(problems)))
    return 0 if evidence["verdict"] == "PASS_REVIEW_REQUIRED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
