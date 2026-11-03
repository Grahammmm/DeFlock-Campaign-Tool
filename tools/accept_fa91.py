"""Scoped offline acceptance runner for FA91 (malformed duplicate queue identity).

One invocation runs exactly one case and writes evidence to a fresh unique
child below --output-root (absolute, outside this repository, with no
symlinked component). Prior evidence is never overwritten.

  behavior          tests.records.test_fa91.Behavior (A1), plus a boundary
                    self-check in a fresh file-launched child process
  compatibility     tests.test_portal_reconcile, unchanged (A2)
  negative-control  tests.records.test_fa91.GateControl (A3): the pre-FA91
                    defect restored in an in-memory disposable module copy
                    must turn exactly the mixed-duplicate regressions red
                    while the unrelated positives stay green; the restored
                    real module must then pass every selected test
  credential-scan   the operator-staged detect-secrets entry script from
                    SOURCE-MANIFEST.json, then the unchanged
                    tools/check_secret_report.py against that exact report (A4)
  public-tree       the unchanged tools/check_public_tree.py with the repo
                    .venv interpreter (A5)

Before any repository import the runner disables bytecode writes, redirects
tempfile/TMPDIR into the run directory, refuses sockets/HTTP, refuses
Python-level writes outside the run directory and refuses reads under the
home directory outside the repository, run directory, staged manifest and
interpreter roots. These guards are test hygiene, not a hostile-code sandbox.
Every subprocess uses a literal argv list without a shell. A pass is
PASS_REVIEW_REQUIRED; it is not full-suite CI or semantic acceptance.
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
MANIFEST = REPO.parent / "SOURCE-MANIFEST.json"
BEHAVIOR = "tests.records.test_fa91.Behavior"
GATE = "tests.records.test_fa91.GateControl"
TEST_MODULE = "tests.records.test_fa91"
COMPATIBILITY = ("tests.test_portal_reconcile",)
PRODUCT = "tools.portal_reconcile"
NEW_FILES = ("tests/records/test_fa91.py", "tools/accept_fa91.py")
HASHED = ("tools/portal_reconcile.py", *NEW_FILES, "tests/test_portal_reconcile.py",
          "tests/fixtures/portal_reconcile/observations.json",
          "tests/fixtures/portal_reconcile/queue-export.json",
          "tools/check_secret_report.py", "tools/check_public_tree.py")
CASES = ("behavior", "compatibility", "negative-control", "credential-scan", "public-tree")
INTERNAL_CASES = ("boundary-probe",)

GUARD_HITS = []   # unexpected guard refusals: always fatal
PROBE_HITS = []   # refusals of the runner's own inert self-check probes
PROBE_PREFIX = "fa91-probe-"


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def git_bytes(*args):
    if not (REPO / ".git").exists():
        return None
    done = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, check=False)
    return done.stdout if done.returncode == 0 else None


def git(*args):
    raw = git_bytes(*args)
    return "unavailable" if raw is None else raw.decode("utf-8", "replace").strip()


def identity():
    hashes = {rel: sha256_bytes((REPO / rel).read_bytes()) if (REPO / rel).is_file() else None for rel in HASHED}
    diff = git_bytes("diff", "HEAD", "--binary")
    status = git("status", "--porcelain", "--untracked-files=all")
    manifest = MANIFEST.read_bytes() if MANIFEST.is_file() else None
    return {"repo": str(REPO), "head": git("rev-parse", "HEAD"), "status_porcelain": status,
            "tracked_diff_sha256": None if diff is None else sha256_bytes(diff),
            "source_manifest": str(MANIFEST), "source_manifest_sha256": None if manifest is None else sha256_bytes(manifest),
            "source_sha256": hashes}


def fresh_run_dir(parent, case):
    """Validate evidence ancestry before the first write, then create a unique child."""
    raw = Path(parent)
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
    """Audit hook: writes only inside run; home reads only inside allowed roots."""
    root = os.path.realpath(run)
    home = os.path.realpath(os.path.expanduser("~"))
    readable = {root, os.path.realpath(REPO), os.path.realpath(MANIFEST), os.path.realpath(sys.prefix),
                os.path.realpath(sys.base_prefix), os.path.realpath(sys.exec_prefix)}
    readable.update(os.path.realpath(p) for p in sys.path if p)

    def resolve(target, dir_fd=None, follow_leaf=True):
        text = os.fsdecode(target)
        if text.startswith("file:"):
            text = text[5:].split("?", 1)[0]
        if isinstance(dir_fd, int) and dir_fd >= 0 and not os.path.isabs(text):
            try:
                text = os.path.join(os.readlink("/proc/self/fd/" + str(dir_fd)), text)
            except OSError:
                return None
        if follow_leaf:
            return os.path.realpath(text)
        # Link-level operations (remove/rename/symlink/...) act on the
        # directory entry itself, never on a symlink's target.
        absolute = os.path.abspath(text)
        return os.path.join(os.path.realpath(os.path.dirname(absolute)), os.path.basename(absolute))

    def record(event, text):
        probe = os.path.basename(os.path.dirname(text)).startswith(PROBE_PREFIX) or \
            os.path.basename(text).startswith(PROBE_PREFIX)
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
            resolved = resolve(target, dir_fd, follow_leaf=event in ("open", "os.truncate", "os.chmod",
                                                                     "shutil.copyfile", "sqlite3.connect"))
            if resolved is None or not within(resolved, root):
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
    home_probe = os.path.join(home, PROBE_PREFIX + secrets.token_hex(6))
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
                    or m.startswith(("campaign_tool.", "tools.", "tests.")))
    if leaked:
        raise RuntimeError("repository modules imported before guards")
    return tmp


class Recorder:
    """unittest result collector keyed by the owning test id (subtests folded)."""

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
    """Absolute-path module constants and bound defaults reachable from repo imports."""
    allowed = [os.path.realpath(run), os.path.realpath(REPO)]
    found = []

    def check(where, value):
        if isinstance(value, (str, os.PathLike)):
            text = os.fspath(value)
            if isinstance(text, str) and text.startswith(os.sep) and len(text) > 1 and "\n" not in text \
                    and not any(within(os.path.realpath(text), a) for a in allowed) and text != os.devnull:
                found.append(where)
    for name, module in sorted(sys.modules.items()):
        if module is None or not (name in ("campaign_tool", "tools") or name.startswith(("campaign_tool.", "tools."))):
            continue
        for attr, value in sorted(vars(module).items()):
            check(name + "." + attr, value)
            defaults = list(getattr(value, "__defaults__", None) or ()) + list(
                (getattr(value, "__kwdefaults__", None) or {}).values())
            for index, default in enumerate(defaults):
                check(name + "." + attr + ".default[" + str(index) + "]", default)
    return found


def case_boundary_probe(run):
    """Run in a fresh file-launched child: guards and redirects hold before/after import."""
    tmp = prepare_import_root(run)
    checks = {"bytecode_disabled": sys.dont_write_bytecode is True,
              "tempdir_is_run_tmp": os.path.realpath(tempfile.gettempdir()) == os.path.realpath(tmp)}
    try:
        socket.create_connection(("192.0.2.1", 9), timeout=1)
        checks["network_probe_refused"] = False
    except OSError as exc:
        checks["network_probe_refused"] = "offline acceptance guard" in str(exc)
    product = __import__(PRODUCT, fromlist=["reconcile"])
    __import__(TEST_MODULE)
    paths = module_paths(PRODUCT, TEST_MODULE)
    checks["module_paths_inside_repo"] = len(paths) == 2 and all(
        within(p, os.path.realpath(REPO)) for p in paths.values())
    checks["product_has_no_path_defaults"] = all(
        not isinstance(v, (str, os.PathLike))
        for f in (product.reconcile, product.build_queue, product.main, product.read_json)
        for v in (list(f.__defaults__ or ()) + list((f.__kwdefaults__ or {}).values())))
    absolute = scan_absolute_constants(run)
    checks["no_unredirected_absolute_path_constants"] = not absolute
    hits = literal_hits()
    checks["new_files_have_no_machine_paths"] = not hits
    probe_file = Path(tempfile.mkstemp(prefix=PROBE_PREFIX, dir=None)[1])
    checks["mkstemp_inside_run"] = within(os.path.realpath(probe_file), os.path.realpath(run))
    return all(checks.values()), {"checks": checks, "module_paths": paths, "absolute_constants": absolute,
                                  "literal_hits": hits}


def run_boundary_child(run):
    child_root = run / "boundary"
    argv = [sys.executable, str(Path(__file__).resolve()), "--case", "boundary-probe",
            "--output-root", str(child_root)]
    child = subprocess.run(argv, cwd=REPO, capture_output=True, check=False)
    out = save(run, "boundary-child-stdout.txt", child.stdout)
    err = save(run, "boundary-child-stderr.txt", child.stderr)
    produced = sorted(child_root.glob("boundary-probe-*/result.json")) if child_root.is_dir() else []
    record = json.loads(produced[0].read_text()) if len(produced) == 1 else {}
    ok = child.returncode == 0 and record.get("exit") == 0 and all(record.get("checks", {"x": False}).values())
    return ok, {"argv": argv, "cwd": str(REPO), "exit": child.returncode, "stdout": out, "stderr": err,
                "result": str(produced[0]) if produced else None, "checks": record.get("checks")}


def case_behavior(run):
    prepare_import_root(run)
    stream = io.StringIO()
    counts, ran, bad = Recorder().run([BEHAVIOR], stream)
    paths = module_paths(PRODUCT, TEST_MODULE)
    paths_ok = len(paths) == 2 and all(within(p, os.path.realpath(REPO)) for p in paths.values())
    boundary_ok, boundary = run_boundary_child(run)
    ok = clean(counts) and not bad and paths_ok and boundary_ok
    return ok, {"selectors": [BEHAVIOR], "counts": counts, "executed_test_ids": ran, "failed_test_ids": bad,
                "module_paths": paths, "module_paths_inside_repo": paths_ok, "boundary_child": boundary,
                "unittest_output": save(run, "unittest.txt", stream.getvalue())}


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
    counts, ran, bad = Recorder().run([GATE], stream)
    evidence = dict(sys.modules[TEST_MODULE].CONTROL_EVIDENCE) if TEST_MODULE in sys.modules else {}
    details = {"selectors": [GATE], "counts": counts, "executed_test_ids": ran, "failed_test_ids": bad,
               "unittest_output": save(run, "unittest.txt", stream.getvalue())}
    if not evidence:
        return False, {**details, "stop": "control_evidence_missing"}
    broken, restored = evidence["broken"], evidence["restored"]
    details.update({
        "broken_control": {k: broken[k] for k in ("tests_run", "failed", "errors", "skipped")},
        "broken_output": save(run, "control-broken.txt", broken["output"]),
        "restored_positive": {k: restored[k] for k in ("tests_run", "failed", "errors", "skipped")},
        "restored_output": save(run, "control-restored.txt", restored["output"]),
        "expected_red": evidence["expected_red"], "required_green": evidence["required_green"]})
    detected = (broken["failed"] == sorted(evidence["expected_red"]) and broken["errors"] == 0
                and not set(evidence["required_green"]) & set(broken["failed"]))
    restored_ok = restored["failed"] == [] and restored["errors"] == 0 and restored["skipped"] == 0 \
        and restored["tests_run"] == broken["tests_run"] > 0
    details.update({"defect_detected": detected, "restored_positive_passed": restored_ok})
    return clean(counts) and not bad and detected and restored_ok, details


POTENTIAL = re.compile(rb"potential secrets: (\d+);")


def case_credential_scan(run):
    try:
        runtime = json.loads(MANIFEST.read_bytes())["runtime"]
        python = runtime["credential_scan_python3"]
        tool = runtime["detect_secrets"]
    except (OSError, ValueError, KeyError, TypeError):
        return False, {"stop": "staging_failure: SOURCE-MANIFEST.json runtime missing"}
    if not (isinstance(python, str) and isinstance(tool, str) and os.path.isfile(python)
            and os.access(python, os.X_OK) and os.path.isfile(tool)):
        return False, {"stop": "staging_failure: declared credential-scan runtime not installed",
                       "credential_scan_python3": python, "detect_secrets": tool}
    scan_argv = [python, tool, "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"]
    report = run / "secret-scan.json"
    with open(report, "wb") as out:
        scan = subprocess.run(scan_argv, cwd=REPO, stdout=out, stderr=subprocess.PIPE, check=False)
    details = {"scan_argv": scan_argv, "scan_cwd": str(REPO), "scan_exit": scan.returncode,
               "scan_stdout": {"file": report.name, "sha256": sha256_bytes(report.read_bytes())},
               "scan_stderr": save(run, "scan-stderr.txt", scan.stderr)}
    if scan.returncode != 0:
        return False, {**details, "stop": "scan_failed"}
    try:
        data = json.loads(report.read_bytes())
    except ValueError:
        return False, {**details, "stop": "scan_report_unparseable"}
    check_argv = [python, str(REPO / "tools" / "check_secret_report.py"), str(report)]
    check = subprocess.run(check_argv, cwd=REPO, capture_output=True, check=False)
    match = POTENTIAL.search(check.stdout)
    potential = int(match.group(1)) if match else None
    details.update({"detect_secrets_version": data.get("version"), "files_with_hits": sorted(data.get("results", {})),
                    "check_argv": check_argv, "check_cwd": str(REPO), "check_exit": check.returncode,
                    "check_stdout": save(run, "check-secret-report-stdout.txt", check.stdout),
                    "check_stderr": save(run, "check-secret-report-stderr.txt", check.stderr),
                    "potential_secrets": potential})
    return check.returncode == 0 and potential == 0, details


def case_public_tree(run):
    python = REPO / ".venv" / "bin" / "python3"
    argv = [str(python), str(REPO / "tools" / "check_public_tree.py")]
    done = subprocess.run(argv, cwd=REPO, capture_output=True, check=False)
    return done.returncode == 0, {"argv": argv, "cwd": str(REPO), "exit": done.returncode,
                                  "stdout": save(run, "check-public-tree-stdout.txt", done.stdout),
                                  "stderr": save(run, "check-public-tree-stderr.txt", done.stderr)}


HANDLERS = {"behavior": case_behavior, "compatibility": case_compatibility,
            "negative-control": case_negative_control, "credential-scan": case_credential_scan,
            "public-tree": case_public_tree, "boundary-probe": case_boundary_probe}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Scoped offline acceptance for FA91 queue identity taint.")
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
            ok, details = HANDLERS[args.case](run)
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
