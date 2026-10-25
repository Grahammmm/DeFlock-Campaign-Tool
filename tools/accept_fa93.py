"""Scoped offline acceptance runner for FA93 (portal report path ancestry).

One invocation runs exactly one case and writes evidence to a fresh, uniquely
named child of --output-root (absolute, outside this repository, no
symlinked component). Earlier author/reviewer evidence is never overwritten.

  behavior          A1: tests.records.test_fa93.Behavior, then a boundary
                    probe in a fresh file-launched process of this runner
  compatibility     A2: tests.test_portal_reconcile, unchanged
  negative-control  A3: tests.records.test_fa93.GateControl (leaf-only
                    opening restored in a disposable copy under this run);
                    both the real failing and the candidate outputs are kept
  credential-scan   A4: the SOURCE-MANIFEST.json declared detect-secrets with
                    its declared interpreter, then the unchanged
                    tools/check_secret_report.py on that exact report
  public-tree       A5: the unchanged tools/check_public_tree.py with the
                    repository .venv interpreter

Only the named selectors are loaded; there is no blanket discovery. Before
any repository import the in-process cases disable bytecode writes, point
tempfile/TMPDIR into the run directory, refuse sockets/HTTP and install an
audit hook refusing absolute-path writes outside the run directory and
absolute home reads outside the repository, run directory and interpreter
roots (descriptor-relative opens carry no directory in the audit event, so
they are covered by the before/after source identity check instead). These
guards are test hygiene, not a hostile-code sandbox. A pass is
REVIEW REQUIRED; it is not full-suite CI or semantic acceptance.
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
import socket
import subprocess
import sys
import tempfile

REPO = Path(__file__).resolve().parents[1]
CANDIDATE = REPO.parent
MANIFEST = CANDIDATE / "SOURCE-MANIFEST.json"
BEHAVIOR = "tests.records.test_fa93.Behavior"
CONTROL = "tests.records.test_fa93.GateControl"
REGRESSION_MODULE = "tests.records.test_fa93"
COMPATIBILITY = ("tests.test_portal_reconcile",)
PRODUCT = "tools.portal_reconcile"
NEW_FILES = ("tests/records/test_fa93.py", "tools/accept_fa93.py")
HASHED = ("tools/portal_reconcile.py", *NEW_FILES, "tests/test_portal_reconcile.py",
          "tests/fixtures/portal_reconcile/observations.json",
          "tests/fixtures/portal_reconcile/queue-export.json",
          "tools/check_secret_report.py", "tools/check_public_tree.py")
CASES = ("behavior", "compatibility", "negative-control", "credential-scan", "public-tree")
INTERNAL_CASES = ("boundary-probe",)

GUARD_HITS = []   # unexpected guard refusals: always fatal
PROBE_HITS = []   # refusals of the runner's own inert self-check probes
PROBE_PREFIX = "fa93-probe-"


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def git_bytes(*args):
    if not (REPO / ".git").exists():
        return None
    done = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, check=False)
    return done.stdout if done.returncode == 0 else None


def git(*args):
    raw = git_bytes(*args)
    return raw.decode().strip() if raw is not None else "unavailable"


def tree_digest():
    digest = hashlib.sha256()
    for directory, dirs, files in os.walk(REPO):
        dirs[:] = sorted(d for d in dirs if d not in (".git", "__pycache__", ".venv"))
        for name in sorted(files):
            path = Path(directory) / name
            if path.suffix == ".pyc" or path.is_symlink():
                continue
            digest.update(str(path.relative_to(REPO)).encode() + b"\0" + hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def identity():
    hashes = {rel: sha256_bytes((REPO / rel).read_bytes()) if (REPO / rel).is_file() else None for rel in HASHED}
    diff = git_bytes("diff", "HEAD", "--binary")
    return {"repo": str(REPO), "head": git("rev-parse", "HEAD"), "status_porcelain": git("status", "--porcelain"),
            "dirty_diff_sha256": sha256_bytes(diff) if diff is not None else None,
            "source_manifest_sha256": sha256_bytes(MANIFEST.read_bytes()) if MANIFEST.is_file() else None,
            "file_sha256": hashes, "tree_sha256": tree_digest()}


def fresh_run_dir(root, case):
    """Validate evidence ancestry before the first write, then create a unique child."""
    raw = Path(root)
    if not raw.is_absolute():
        raise SystemExit("output_root_must_be_absolute")
    if any(part in (".", "..") for part in raw.parts):
        raise SystemExit("output_root_not_canonical")
    for ancestor in (raw, *raw.parents):
        if ancestor.is_symlink():
            raise SystemExit("output_root_has_symlinked_component")
        if ancestor.exists() and not ancestor.is_dir():
            raise SystemExit("output_root_component_not_directory")
    if raw == REPO or REPO in raw.parents:
        raise SystemExit("output_root_inside_repository")
    if os.path.realpath(raw) != str(raw):
        raise SystemExit("output_root_not_real_path")
    raw.mkdir(mode=0o700, parents=True, exist_ok=True)
    if raw.is_symlink() or not raw.is_dir():
        raise SystemExit("output_root_unsafe_after_create")
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run = raw / (case + "-" + stamp + "-" + str(os.getpid()) + "-" + secrets.token_hex(4))
    run.mkdir(mode=0o700, exist_ok=False)
    return run


def install_network_guards():
    def refuse(name):
        def guard(*args, **kwargs):
            target = repr(args[1:2] or args[:1])
            (PROBE_HITS if "192.0.2." in target else GUARD_HITS).append(name)
            raise OSError("offline acceptance guard refused " + name)
        return guard
    socket.socket.connect = refuse("socket.connect")
    socket.socket.connect_ex = refuse("socket.connect_ex")
    socket.socket.sendto = refuse("socket.sendto")
    socket.create_connection = refuse("socket.create_connection")
    socket.getaddrinfo = refuse("socket.getaddrinfo")
    import http.client
    import urllib.request
    http.client.HTTPConnection.connect = refuse("http.client.connect")
    urllib.request.urlopen = refuse("urllib.request.urlopen")


WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
# audit event -> ((path argument index, dir_fd argument index or None), ...)
WRITE_EVENTS = {"os.mkdir": ((0, 2),), "os.rename": ((0, 2), (1, 3)), "os.replace": ((0, 2), (1, 3)),
                "os.remove": ((0, 1),), "os.rmdir": ((0, 1),), "os.truncate": ((0, None),),
                "os.symlink": ((1, 2),), "os.link": ((1, 3),), "os.chmod": ((0, 2),),
                "shutil.rmtree": ((0, 1),), "shutil.copyfile": ((1, None),), "sqlite3.connect": ((0, None),)}


def within(path, root):
    return path == root or path.startswith(root + os.sep)


def install_file_guards(run):
    root = os.path.realpath(run)
    home = os.path.realpath(os.path.expanduser("~"))
    readable = {root, os.path.realpath(REPO), os.path.realpath(sys.prefix), os.path.realpath(sys.base_prefix),
                os.path.realpath(sys.exec_prefix), os.path.realpath(Path(sys.executable).parent),
                os.path.realpath(MANIFEST)}
    readable.update(os.path.realpath(p) for p in sys.path if p)

    def resolve(target, dir_fd=None, parent=False):
        text = os.fsdecode(target)
        if not os.path.isabs(text):
            if not (isinstance(dir_fd, int) and dir_fd >= 0):
                return None
            try:
                text = os.path.join(os.readlink("/proc/self/fd/" + str(dir_fd)), text)
            except OSError:
                return None
        if parent:  # a created leaf need not exist yet; its directory decides
            return os.path.join(os.path.realpath(os.path.dirname(text) or os.sep), os.path.basename(text))
        return os.path.realpath(text)

    def record(event, text):
        probe = PROBE_PREFIX in text
        (PROBE_HITS if probe else GUARD_HITS).append(event + ":" + text)

    def hook(event, args):
        writes, reads = [], []
        if event == "open":
            path, mode, flags = (tuple(args) + (None, None, None))[:3]
            if path is None or isinstance(path, int):
                return
            writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                isinstance(flags, int) and flags & WRITE_FLAGS)
            (writes if writing else reads).append((path, None))
        elif event in WRITE_EVENTS:
            for index, fd_index in WRITE_EVENTS[event]:
                if index < len(args) and args[index] is not None and not isinstance(args[index], int):
                    dir_fd = args[fd_index] if fd_index is not None and fd_index < len(args) else None
                    writes.append((args[index], dir_fd))
        else:
            return
        for target, dir_fd in writes:
            text = os.fsdecode(target)
            if text in ("", ":memory:", os.devnull):
                continue
            resolved = resolve(target, dir_fd, parent=True)
            if resolved is None:
                continue  # descriptor-relative: covered by the source identity check
            if not within(resolved, root):
                record(event, text)
                raise PermissionError("acceptance write guard refused " + event + " outside the run root")
        for target, dir_fd in reads:
            resolved = resolve(target, dir_fd)
            if resolved is not None and within(resolved, home) and not any(within(resolved, r) for r in readable):
                record(event + ":read", os.fsdecode(target))
                raise PermissionError("acceptance live-file sentinel refused a home read outside allowed roots")
    sys.addaudithook(hook)
    escape = os.path.join(os.sep, PROBE_PREFIX + secrets.token_hex(6), "write")
    try:
        open(escape, "w").close()
        write_refused = False
    except PermissionError:
        write_refused = True
    except OSError:
        write_refused = False
    home_probe = os.path.join(home, PROBE_PREFIX + secrets.token_hex(6), "read")
    try:
        open(home_probe, "rb").close()
        read_refused = False
    except PermissionError:
        read_refused = True
    except OSError:
        read_refused = False
    positive = run / "guard-positive.txt"
    positive.write_text("in-root write allowed\n")
    return {"run_root": root, "write_escape_refused": write_refused, "home_read_refused": read_refused,
            "in_root_positive_written": positive.read_text() == "in-root write allowed\n"}


def prepare_import_root(run):
    """Everything here precedes the first repository import."""
    sys.dont_write_bytecode = True
    tmp = run / "tmp"
    tmp.mkdir(mode=0o700)
    tempfile.tempdir = str(tmp)
    os.environ["TMPDIR"] = str(tmp)
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    install_network_guards()
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    leaked = sorted(m for m in sys.modules if m in ("campaign_tool", "tools", "tests")
                    or m.startswith(("campaign_tool.", "tests.", "tools.")))
    if leaked:
        raise RuntimeError("repository modules imported before guards")
    return tmp


class Recorder:
    """unittest result collector keyed by test id (subtests folded)."""

    def __init__(self):
        import unittest

        class Result(unittest.TextTestResult):
            def __init__(inner, *args, **kwargs):
                super().__init__(*args, **kwargs)
                inner.bad_ids = set()
                inner.ran_ids = []

            def startTest(inner, test):
                inner.ran_ids.append(test.id())
                super().startTest(test)

            def addFailure(inner, test, err):
                inner.bad_ids.add(test.id())
                super().addFailure(test, err)

            def addError(inner, test, err):
                inner.bad_ids.add(getattr(test, "test_case", test).id())
                super().addError(test, err)

            def addSubTest(inner, test, subtest, err):
                if err is not None:
                    inner.bad_ids.add(test.id())
                super().addSubTest(test, subtest, err)

        self.result_class = Result

    def run(self, names, stream):
        import unittest
        suite = unittest.defaultTestLoader.loadTestsFromNames(list(names))
        runner = unittest.TextTestRunner(stream=stream, verbosity=2, resultclass=self.result_class)
        result = runner.run(suite)
        counts = {"tests_run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
                  "skipped": len(result.skipped), "expected_failures": len(result.expectedFailures),
                  "unexpected_successes": len(result.unexpectedSuccesses)}
        return counts, sorted(result.ran_ids), sorted(result.bad_ids)


def clean(counts):
    return (counts["tests_run"] > 0 and counts["failures"] == 0 and counts["errors"] == 0 and counts["skipped"] == 0
            and counts["expected_failures"] == 0 and counts["unexpected_successes"] == 0)


def module_paths(*names):
    return {name: os.path.realpath(sys.modules[name].__file__) for name in names if name in sys.modules}


def save(run, name, data):
    raw = data.encode() if isinstance(data, str) else data
    (run / name).write_bytes(raw)
    return {"file": name, "sha256": sha256_bytes(raw), "bytes": len(raw)}


def child(run, label, argv, cwd):
    """Run one literal argv (no shell) and keep a raw receipt."""
    done = subprocess.run(argv, cwd=cwd, capture_output=True, check=False)
    return done, {"argv": [str(a) for a in argv], "cwd": str(cwd), "exit": done.returncode,
                  "stdout": save(run, label + "-stdout.txt", done.stdout),
                  "stderr": save(run, label + "-stderr.txt", done.stderr)}


def case_behavior(run):
    prepare_import_root(run)
    import unittest
    stream = io.StringIO()
    counts, ran, bad = Recorder().run([BEHAVIOR], stream)
    module = sys.modules[REGRESSION_MODULE]
    required = sorted(BEHAVIOR + "." + n for n in unittest.defaultTestLoader.getTestCaseNames(module.Behavior))
    paths = module_paths(PRODUCT, REGRESSION_MODULE)
    paths_ok = len(paths) == 2 and all(within(p, os.path.realpath(REPO)) for p in paths.values())
    probe_root = run / "boundary"
    argv = [sys.executable, str(REPO / "tools" / "accept_fa93.py"), "--case", "boundary-probe",
            "--output-root", str(probe_root)]
    probe, receipt = child(run, "boundary-probe", argv, REPO)
    produced = sorted(probe_root.glob("boundary-probe-*/result.json")) if probe_root.is_dir() else []
    probe_record = json.loads(produced[0].read_text()) if len(produced) == 1 else {}
    probe_ok = probe.returncode == 0 and probe_record.get("exit") == 0
    ok = clean(counts) and not bad and ran == required and len(required) >= 20 and paths_ok and probe_ok
    return ok, {"selectors": [BEHAVIOR], "counts": counts, "required_tests": required, "ran_tests": ran,
                "failed_test_ids": bad, "module_paths": paths, "module_paths_inside_repo": paths_ok,
                "unittest_output": save(run, "unittest.txt", stream.getvalue()),
                "boundary_probe": {**receipt, "result_json": str(produced[0]) if produced else None,
                                   "checks": probe_record.get("checks"), "ok": probe_ok}}


def case_compatibility(run):
    prepare_import_root(run)
    stream = io.StringIO()
    counts, ran, bad = Recorder().run(COMPATIBILITY, stream)
    per_module = {m: sum(1 for t in ran if t.startswith(m + ".")) for m in COMPATIBILITY}
    ok = clean(counts) and all(per_module.values()) and not bad
    return ok, {"selectors": list(COMPATIBILITY), "counts": counts, "tests_per_module": per_module,
                "failed_test_ids": bad, "module_paths": module_paths(PRODUCT, *COMPATIBILITY),
                "unittest_output": save(run, "unittest.txt", stream.getvalue())}


def case_negative_control(run):
    prepare_import_root(run)
    stream = io.StringIO()
    counts, ran, bad = Recorder().run([CONTROL], stream)
    module = sys.modules[REGRESSION_MODULE]
    evidence = getattr(module.GateControl, "evidence", {}) or {}
    broken = evidence.get("broken", {})
    candidate = evidence.get("candidate", {})
    control_copy_inside_run = within(os.path.realpath(tempfile.gettempdir()), os.path.realpath(run))
    detected = (broken.get("tests_run") == 2 and broken.get("failed") == [module.GateControl.DEFECT_TEST]
                and broken.get("errored") == [] and "alias target was opened" in broken.get("output", ""))
    restored_positive = (candidate.get("tests_run") == 2 and candidate.get("failed") == []
                         and candidate.get("errored") == [] and candidate.get("skipped") == 0)
    ok = clean(counts) and not bad and detected and restored_positive and control_copy_inside_run
    return ok, {"selectors": [CONTROL], "counts": counts, "ran_tests": ran, "failed_test_ids": bad,
                "defect_detected": detected, "restored_positive_passed": restored_positive,
                "broken_summary": {k: broken.get(k) for k in ("tests_run", "failed", "errored", "skipped")},
                "candidate_summary": {k: candidate.get(k) for k in ("tests_run", "failed", "errored", "skipped")},
                "broken_output": save(run, "control-broken-unittest.txt", broken.get("output", "")),
                "candidate_output": save(run, "control-candidate-unittest.txt", candidate.get("output", "")),
                "control_copy_parent": tempfile.gettempdir(),
                "unittest_output": save(run, "unittest.txt", stream.getvalue())}


POTENTIAL = re.compile(r"potential secrets: (\d+);")


def case_credential_scan(run):
    if not MANIFEST.is_file():
        return False, {"stop": "staging_failure: SOURCE-MANIFEST.json missing"}
    runtime = json.loads(MANIFEST.read_text()).get("runtime", {})
    python = runtime.get("credential_scan_python3")
    tool = runtime.get("detect_secrets")
    if not (python and tool and os.access(python, os.X_OK) and os.path.isfile(tool)):
        return False, {"stop": "staging_failure: declared credential-scan runtime missing",
                       "credential_scan_python3": python, "detect_secrets": tool}
    scan_argv = [python, tool, "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"]
    scan, scan_receipt = child(run, "detect-secrets", scan_argv, REPO)
    report = run / "detect-secrets-stdout.txt"
    details = {"manifest": str(MANIFEST), "scan": scan_receipt}
    if scan.returncode != 0:
        return False, {**details, "stop": "scan_failed"}
    try:
        data = json.loads(report.read_bytes())
    except ValueError:
        return False, {**details, "stop": "scan_report_unparseable"}
    check_argv = [python, str(REPO / "tools" / "check_secret_report.py"), str(report)]
    check, check_receipt = child(run, "check-secret-report", check_argv, REPO)
    match = POTENTIAL.search(check.stdout.decode())
    potential = int(match.group(1)) if match else None
    ok = check.returncode == 0 and potential == 0
    return ok, {**details, "detect_secrets_version": data.get("version"),
                "files_with_raw_hits": sorted(data.get("results", {})), "check": check_receipt,
                "potential_secrets": potential}


def case_public_tree(run):
    python = REPO / ".venv" / "bin" / "python3"
    argv = [str(python), str(REPO / "tools" / "check_public_tree.py")]
    done, receipt = child(run, "check-public-tree", argv, REPO)
    return done.returncode == 0, {"check": receipt}


def literal_hits():
    tokens = ["/" + "home" + "/", "agent" + "-work", "/" + "Users" + "/", "." + "hermes", "kan" + "ban",
              "/" + "tmp" + "/", "scratch" + "/"]
    hits = {}
    for rel in NEW_FILES:
        text = (REPO / rel).read_text()
        found = sorted(t for t in tokens if t in text)
        if found:
            hits[rel] = found
    return hits


def scan_absolute_constants(run):
    """Absolute-path module constants and bound defaults outside run/repo/interpreter."""
    allowed = [os.path.realpath(run), os.path.realpath(REPO)]
    found = []

    def check(where, value):
        if isinstance(value, (str, os.PathLike)):
            text = os.fspath(value)
            if isinstance(text, str) and text.startswith(os.sep) and len(text) > 1 and "\n" not in text \
                    and not any(within(os.path.realpath(text), a) for a in allowed) and text != os.devnull:
                found.append(where)
    for name, module in sorted(sys.modules.items()):
        if module is None or not (name in ("campaign_tool", PRODUCT, REGRESSION_MODULE)
                                  or name.startswith("campaign_tool.")):
            continue
        for attr, value in sorted(vars(module).items()):
            check(name + "." + attr, value)
            defaults = list(getattr(value, "__defaults__", None) or ()) + list(
                (getattr(value, "__kwdefaults__", None) or {}).values())
            for index, default in enumerate(defaults):
                check(name + "." + attr + ".default[" + str(index) + "]", default)
    return found


def case_boundary_probe(run):
    tmp = prepare_import_root(run)
    checks = {"bytecode_disabled": sys.dont_write_bytecode is True,
              "tempdir_inside_run": os.path.realpath(tempfile.gettempdir()) == os.path.realpath(tmp)}
    try:
        socket.create_connection(("192.0.2.1", 9), timeout=1)
        checks["network_probe_refused"] = False
    except OSError as exc:
        checks["network_probe_refused"] = "offline acceptance guard" in str(exc)
    product = __import__(PRODUCT, fromlist=["read_json"])
    __import__(REGRESSION_MODULE)
    paths = module_paths(PRODUCT, REGRESSION_MODULE)
    checks["module_paths_inside_repo"] = len(paths) == 2 and all(
        within(p, os.path.realpath(REPO)) for p in paths.values())
    checks["io_functions_have_no_path_defaults"] = all(
        not (getattr(f, "__defaults__", None) or getattr(f, "__kwdefaults__", None))
        for f in (product.read_json, product.write_new))
    absolute = scan_absolute_constants(run)
    checks["no_unredirected_absolute_path_constants"] = not absolute
    hits = literal_hits()
    checks["new_files_have_no_machine_paths"] = not hits
    return all(checks.values()), {"checks": checks, "module_paths": paths, "absolute_constants": absolute,
                                  "literal_hits": hits}


RUNNERS = {"behavior": case_behavior, "compatibility": case_compatibility,
           "negative-control": case_negative_control, "credential-scan": case_credential_scan,
           "public-tree": case_public_tree, "boundary-probe": case_boundary_probe}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Scoped offline acceptance for FA93 path ancestry.")
    parser.add_argument("--case", required=True, choices=CASES + INTERNAL_CASES)
    parser.add_argument("--output-root", required=True)
    args = parser.parse_args(argv)
    run = fresh_run_dir(args.output_root, args.case)
    before = identity()
    guard = install_file_guards(run)
    details = {}
    try:
        if not (guard["write_escape_refused"] and guard["home_read_refused"] and guard["in_root_positive_written"]):
            ok, details = False, {"stop": "guard_self_check_failed"}
        else:
            ok, details = RUNNERS[args.case](run)
    except Exception as exc:  # any unexpected failure stops acceptance
        ok, details = False, {**details, "stop": "unknown_failure: " + type(exc).__name__ + ": " + str(exc)[:500]}
    after = identity()
    if after != before:
        ok = False
        details["stop"] = "candidate_identity_changed_during_run"
        details["identity_after"] = after
    if GUARD_HITS:
        ok = False
        details["stop"] = "guard_refused_unexpected_operation"
    record = {"case": args.case, "argv": [sys.executable, *sys.argv], "cwd": os.getcwd(),
              "interpreter": sys.executable, "interpreter_realpath": os.path.realpath(sys.executable),
              "python": sys.version.split()[0], "run_dir": str(run),
              "guards": {**guard, "unexpected_refusals": list(GUARD_HITS), "inert_probe_refusals": len(PROBE_HITS)},
              "identity": before, "result": "PASS_REVIEW_REQUIRED" if ok else "FAIL", "exit": 0 if ok else 1,
              **details}
    (run / "result.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    print(json.dumps(record, indent=2, sort_keys=True))
    return record["exit"]


if __name__ == "__main__":
    raise SystemExit(main())
