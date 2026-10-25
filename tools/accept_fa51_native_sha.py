"""Scoped offline acceptance runner for FA51 (native item source digests).

Each invocation runs exactly ONE case and writes evidence to a fresh, unique
directory below --output-parent, which must be an absolute, link-free path
outside this repository:

  behavior          tests/records/test_fa51_native_sha.py (A1 producer
                    positive, A2 malformed digests, A3 discovery replay and the
                    in-suite A4 checks), mapped to named test IDs
  compatibility     unchanged tests/records/test_mail_wire_intake.py and
                    tests/records/test_mail_delta.py
  negative-control  A4: the same regression with ONLY the native digest gate
                    disabled in memory (must be detected), its valid positive
                    (must pass) and the candidate gate (must pass)
  isolation         A5: guard/redirect self-checks, reachable constants and
                    bound defaults, module paths, then a rerun of the behavior
                    case from a second complete source copy
  credentials       installed detect-secrets (beside this interpreter) scan +
                    unchanged tools/check_secret_report.py on that exact JSON

Before any application module is imported the runner disables bytecode
writes, redirects tempfile/TMPDIR/HOME into the per-run directory and installs
sentinels: socket/urllib/http.client refusals (no network or model calls), an
audit-hook write guard (no write outside the per-run directory), a live-file
read guard (no read under the user's home outside this repository, the
per-run directory or the interpreter prefixes) and a subprocess allowlist.
These are test guards, not a hostile-code sandbox. A pass is REVIEW REQUIRED;
it is not full-repository CI, semantic, legal or release acceptance.
"""
import argparse
import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import pwd
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile

REPO = Path(__file__).resolve().parents[1]
REGRESSION = "tests.records.test_fa51_native_sha"
COMPATIBILITY = ("tests.records.test_mail_wire_intake", "tests.records.test_mail_delta")
REQUIREMENTS = {
    "A1": f"{REGRESSION}.ProducerPositiveTests",
    "A2": f"{REGRESSION}.MalformedDigestTests",
    "A3": f"{REGRESSION}.DiscoveryReplayTests",
    "A4-in-suite": f"{REGRESSION}.NegativeControlTests",
}
CONTROL_DETECTS = (REQUIREMENTS["A2"], REQUIREMENTS["A3"],
                   f"{REGRESSION}.NegativeControlTests.test_a4_candidate_gate_is_the_deciding_check")
CONTROL_POSITIVES = (f"{REGRESSION}.MalformedDigestTests.test_a2_valid_digest_and_unrelated_fields_still_parse",
                     f"{REGRESSION}.ProducerPositiveTests",
                     f"{REGRESSION}.NegativeControlTests.test_a4_control_accepts_malformed_and_consumer_raises_type_error")
HASHED = (
    "campaign_tool/records/intake/native_receipt.py",
    "campaign_tool/records/intake/mail_delta.py",
    "campaign_tool/records/mail_adoption.py",
    "tests/records/test_fa51_native_sha.py",
    "tests/records/native_msg_fixtures.py",
    "tests/records/test_mail_wire_intake.py",
    "tests/records/test_mail_delta.py",
    "tools/accept_fa51_native_sha.py",
    "tools/check_secret_report.py",
)
APP_MODULES = ("campaign_tool.records.intake.native_receipt", "campaign_tool.records.intake.mail_delta",
               "campaign_tool.records.mail_adoption", REGRESSION)
HOME_ROOT = os.path.realpath(pwd.getpwuid(os.getuid()).pw_dir)
GUARD_HITS, WRITE_HITS, READ_HITS, PROC_HITS, PROBE_HITS = [], [], [], [], []
STATE = {"root": None, "read_roots": (), "procs": ()}


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def git(*args):
    done = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True, check=False)
    return done.stdout.strip() if done.returncode == 0 else f"unavailable:{done.returncode}"


def tree_digest():
    digest = hashlib.sha256()
    for directory, dirs, files in os.walk(REPO):
        dirs[:] = sorted(d for d in dirs if d not in (".git", "__pycache__"))
        for name in sorted(files):
            path = Path(directory) / name
            digest.update(str(path.relative_to(REPO)).encode() + b"\0" + hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def identity():
    hashes = {rel: (sha256_bytes((REPO / rel).read_bytes()) if (REPO / rel).is_file() else None) for rel in HASHED}
    return {"repo": str(REPO), "head": git("rev-parse", "HEAD"), "status_porcelain": git("status", "--porcelain"),
            "source_sha256": hashes, "tree_sha256": tree_digest()}


def safe_parent(parent):
    """Validate evidence-root ancestry before any write; create missing parts one by one."""
    text = os.fspath(parent)
    if not os.path.isabs(text) or os.path.normpath(text) != text.rstrip(os.sep) or ".." in text.split(os.sep):
        raise SystemExit("output_parent_must_be_absolute_and_normalized")
    path = Path(text)
    if path == REPO or REPO in path.parents:
        raise SystemExit("output_parent_inside_repository")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            current.mkdir(mode=0o700)
            info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise SystemExit("output_parent_linked_or_not_directory")
        if info.st_mode & stat.S_IWOTH and not info.st_mode & stat.S_ISVTX:
            raise SystemExit("output_parent_world_writable")
    if os.path.realpath(path) != str(path):
        raise SystemExit("output_parent_not_canonical")
    if path.lstat().st_uid != os.getuid():
        raise SystemExit("output_parent_wrong_owner")
    return path


def fresh_run_dir(parent, case):
    parent = safe_parent(parent)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run = parent / f"{case}-{stamp}-{os.getpid()}-{secrets.token_hex(4)}"
    run.mkdir(mode=0o700, exist_ok=False)
    return run


def _inside(text, roots):
    resolved = os.path.realpath(text)
    return any(resolved == root or resolved.startswith(root + os.sep) for root in roots)


WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
WRITE_EVENTS = {"os.mkdir": ((0, 2),), "os.rename": ((0, 2), (1, 3)), "os.replace": ((0, 2), (1, 3)),
                "os.remove": ((0, 1),), "os.rmdir": ((0, 1),), "os.truncate": ((0, None),),
                "os.symlink": ((1, 2),), "os.link": ((1, 3),), "os.chmod": ((0, 2),),
                "shutil.rmtree": ((0, 1),), "shutil.copyfile": ((1, None),), "sqlite3.connect": ((0, None),)}


def _target(target, dir_fd):
    if isinstance(target, int):
        return None  # already-open descriptor
    text = os.fsdecode(target)
    if text in ("", ":memory:", os.devnull):
        return None
    if text.startswith("file:"):
        text = text[5:].split("?", 1)[0]
    if isinstance(dir_fd, int) and dir_fd >= 0 and not os.path.isabs(text):
        try:
            text = os.path.join(os.readlink(f"/proc/self/fd/{dir_fd}"), text)
        except OSError:
            return "/unresolvable-dir-fd"
    return os.path.abspath(text)


def _audit(event, args):
    root = STATE["root"]
    if root is None:
        return
    if event == "subprocess.Popen":
        executable = os.fsdecode(args[0]) if args and args[0] is not None else ""
        argv = args[1] if len(args) > 1 else None
        first = os.fsdecode(argv[0]) if isinstance(argv, (list, tuple)) and argv else executable
        if first not in STATE["procs"]:
            PROC_HITS.append(os.path.basename(first))
            raise PermissionError("acceptance subprocess sentinel refused an unlisted program")
        return
    if event == "open":
        path, mode, flags = (tuple(args) + (None, None, None))[:3]
        if path is None:
            return
        writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
            isinstance(flags, int) and flags & WRITE_FLAGS)
        text = _target(path, None)
        if text is None:
            return
        probe = os.path.basename(text).startswith("fa51-escape-") or os.path.basename(
            os.path.dirname(text)).startswith("fa51-escape-")
        if writing and not _inside(text, (root,)):
            (PROBE_HITS if probe else WRITE_HITS).append(f"write:{text}")
            raise PermissionError("acceptance write guard refused a write outside the per-run root")
        if not writing and _inside(text, (HOME_ROOT,)) and not _inside(text, STATE["read_roots"]):
            (PROBE_HITS if probe else READ_HITS).append(f"read:{text}")
            raise PermissionError("acceptance live-file sentinel refused a read outside admitted roots")
        return
    if event in WRITE_EVENTS:
        for p, d in WRITE_EVENTS[event]:
            if p < len(args) and args[p] is not None:
                text = _target(args[p], args[d] if d is not None and d < len(args) else None)
                if text is not None and not _inside(text, (root,)):
                    WRITE_HITS.append(f"{event}:{text}")
                    raise PermissionError("acceptance write guard refused a write outside the per-run root")


def install_guards(run):
    """Sentinels and redirects; must run before any application import."""
    sys.dont_write_bytecode = True
    tmp = run / "tmp"
    home = run / "home"
    tmp.mkdir(mode=0o700)
    home.mkdir(mode=0o700)
    tempfile.tempdir = str(tmp)
    os.environ.update(TMPDIR=str(tmp), TEMP=str(tmp), TMP=str(tmp), HOME=str(home),
                      PYTHONDONTWRITEBYTECODE="1")

    def refuse(name):
        def guard(*args, **kwargs):
            GUARD_HITS.append(name)
            raise OSError(f"offline acceptance guard refused {name}")
        return guard
    socket.socket.connect = refuse("socket.connect")
    socket.socket.connect_ex = refuse("socket.connect_ex")
    socket.create_connection = refuse("socket.create_connection")
    socket.getaddrinfo = refuse("socket.getaddrinfo")
    import http.client
    import urllib.request
    http.client.HTTPConnection.connect = refuse("http.client.connect")
    urllib.request.urlopen = refuse("urllib.request.urlopen")
    prefixes = {os.path.realpath(p) for p in (sys.prefix, sys.base_prefix, sys.exec_prefix)}
    STATE["read_roots"] = (str(REPO), os.path.realpath(run), *sorted(prefixes))
    detect = Path(sys.executable).parent / "detect-secrets"
    STATE["procs"] = ("git", sys.executable, str(detect))
    STATE["root"] = os.path.realpath(run)
    sys.addaudithook(_audit)
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    # Inert self-checks: targets live under a nonexistent directory.
    escape_dir = os.path.join(HOME_ROOT, f"fa51-escape-{secrets.token_hex(6)}")
    checks = {}
    for label, mode in (("write_probe_refused", "w"), ("read_probe_refused", "r")):
        try:
            open(os.path.join(escape_dir, "probe"), mode).close()
            checks[label] = False
        except PermissionError:
            checks[label] = True
        except OSError:
            checks[label] = False
    positive = run / "write-guard-positive.txt"
    positive.write_text("in-root write allowed\n")
    checks["in_root_positive_written"] = positive.is_file()
    checks["probe_hits"] = len(PROBE_HITS)
    checks["ok"] = (checks["write_probe_refused"] and checks["read_probe_refused"]
                    and checks["in_root_positive_written"] and len(PROBE_HITS) == 2
                    and not (WRITE_HITS or READ_HITS))
    return checks


class RecordingResult:
    @staticmethod
    def make():
        import unittest

        class Result(unittest.TextTestResult):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.passed = []

            def addSuccess(self, test):
                super().addSuccess(test)
                self.passed.append(test.id())
        return Result


def run_names(names, stream):
    import unittest
    suite = unittest.defaultTestLoader.loadTestsFromNames(list(names))
    result = unittest.TextTestRunner(stream=stream, verbosity=2, resultclass=RecordingResult.make()).run(suite)
    failed = sorted({getattr(t, "test_case", t).id() for t, _ in result.failures + result.errors})
    detail = sorted({t.id() for t, _ in result.failures + result.errors})
    return {"tests_run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
            "skipped": len(result.skipped), "expected_failures": len(result.expectedFailures),
            "unexpected_successes": len(result.unexpectedSuccesses), "passed_ids": sorted(result.passed),
            "failed_or_errored_ids": failed, "failed_or_errored_subtests": len(detail)}


def clean(c):
    return (c["tests_run"] > 0 and c["failures"] == 0 and c["errors"] == 0 and c["skipped"] == 0
            and c["expected_failures"] == 0 and c["unexpected_successes"] == 0)


def module_paths():
    paths = {}
    for name in APP_MODULES:
        module = sys.modules.get(name)
        paths[name] = getattr(module, "__file__", None) if module else None
    return paths


def all_under(paths, root):
    return all(p is not None and _inside(p, (str(root),)) for p in paths.values())


def save(run, name, text):
    raw = text.encode()
    (run / name).write_bytes(raw)
    return {"raw_output": name, "raw_output_sha256": sha256_bytes(raw)}


def guards_quiet():
    return not (GUARD_HITS or WRITE_HITS or READ_HITS or PROC_HITS)


def case_behavior(run):
    stream = io.StringIO()
    c = run_names([REGRESSION], stream)
    mapping = {}
    for key, prefix in REQUIREMENTS.items():
        ids = [i for i in c["passed_ids"] if i.startswith(prefix + ".")]
        mapping[key] = ids
    mapped = all(mapping.values())
    paths = module_paths()
    ok = clean(c) and mapped and all_under(paths, REPO) and guards_quiet()
    return ok, {"modules": [REGRESSION], "counts": c, "requirement_tests": mapping,
                "module_paths": paths, **save(run, "unittest.txt", stream.getvalue())}


def case_compatibility(run):
    stream = io.StringIO()
    c = run_names(COMPATIBILITY, stream)
    ok = clean(c) and guards_quiet()
    return ok, {"modules": list(COMPATIBILITY), "counts": {k: v for k, v in c.items() if k != "passed_ids"},
                **save(run, "unittest.txt", stream.getvalue())}


def case_negative_control(run):
    from unittest import mock
    from campaign_tool.records.intake import native_receipt
    regression = __import__(REGRESSION, fromlist=["omit_native_digest_validation"])
    stream = io.StringIO()
    stream.write("=== broken control: native_receipt._native_source_digest -> no validation ===\n")
    gate_present = callable(getattr(native_receipt, "_native_source_digest", None))
    with mock.patch.object(native_receipt, "_native_source_digest", regression.omit_native_digest_validation):
        broken_stream = io.StringIO()
        broken = run_names(CONTROL_DETECTS, broken_stream)
        stream.write(broken_stream.getvalue())
        stream.write("=== broken control: positives stay valid ===\n")
        broken_positive = run_names(CONTROL_POSITIVES, stream)
    # The unchanged consumer projection reproduces the raw TypeError for list/object values.
    projection_type_errors = sorted(kind for kind in ("list", "dict")
                                    if f"TypeError: unhashable type: '{kind}'" in broken_stream.getvalue())
    stream.write("=== candidate gate restored: same regression and positives ===\n")
    candidate = run_names(CONTROL_DETECTS + CONTROL_POSITIVES, stream)
    detected = broken["tests_run"] > 0 and (broken["failures"] + broken["errors"]) > 0
    expected = {f"{REGRESSION}.MalformedDigestTests.test_a2_parse_rejects_each_malformed_native_digest",
                f"{REGRESSION}.MalformedDigestTests.test_a2_load_receipt_rejects_each_malformed_native_digest",
                f"{REGRESSION}.DiscoveryReplayTests.test_a3_discover_counts_gap_and_keeps_valid_peer",
                f"{REGRESSION}.NegativeControlTests.test_a4_candidate_gate_is_the_deciding_check"}
    detected_all = expected <= set(broken["failed_or_errored_ids"])
    ok = (gate_present and detected and detected_all and projection_type_errors == ["dict", "list"]
          and clean(broken_positive) and clean(candidate) and guards_quiet())
    return ok, {"gate_present": gate_present, "broken_control": broken, "broken_control_detected_ids_expected":
                sorted(expected), "broken_control_projection_type_errors": projection_type_errors,
                "broken_positive": broken_positive, "candidate": candidate,
                **save(run, "unittest.txt", stream.getvalue())}


def scan_reachable_paths():
    """Module constants and bound defaults that name absolute paths outside admitted roots."""
    import inspect
    allowed = (str(REPO), STATE["root"])
    found = []

    def check(owner, value):
        values = value if isinstance(value, (tuple, list, frozenset, set)) else (value,)
        for item in values:
            if isinstance(item, (str, Path)):
                text = os.fspath(item)
                if os.path.isabs(text) and len(text) > 1 and not _inside(text, allowed):
                    found.append({"owner": owner, "path": text, "under_home": _inside(text, (HOME_ROOT,))})

    for name, module in sorted(sys.modules.items()):
        if not (name.startswith("campaign_tool") or name.startswith("tests.records")) or module is None:
            continue
        for attr, value in list(vars(module).items()):
            check(f"{name}.{attr}", value)
            targets = [value]
            if inspect.isclass(value) and getattr(value, "__module__", None) == name:
                targets += [v for v in vars(value).values()]
            for target in targets:
                function = getattr(target, "__func__", target)
                if inspect.isfunction(function):
                    for default in (function.__defaults__ or ()) + tuple((function.__kwdefaults__ or {}).values()):
                        check(f"{name}.{attr}:{function.__qualname__}:default", default)
    return found


def case_isolation(run, guard):
    import importlib
    import types
    for name in APP_MODULES:
        importlib.import_module(name)
    # Scanner self-check: an inert synthetic module with a home-rooted bound default.
    probe = types.ModuleType("campaign_tool._fa51_scan_probe")
    probe_target = os.path.join(HOME_ROOT, "fa51-scan-probe")

    def probe_function(path=probe_target):
        return path
    setattr(probe, "probe_function", probe_function)
    probe_function.__module__ = probe.__name__
    sys.modules[probe.__name__] = probe
    try:
        probe_seen = any(entry["path"] == probe_target and entry["under_home"] for entry in scan_reachable_paths())
    finally:
        sys.modules.pop(probe.__name__, None)
    reach = scan_reachable_paths()
    paths = module_paths()
    checks = {
        "guard_self_check": guard["ok"],
        "reachable_scan_detects_probe": probe_seen,
        "bytecode_disabled": sys.dont_write_bytecode is True,
        "tempdir_redirected": _inside(tempfile.gettempdir(), (STATE["root"],)),
        "home_redirected": _inside(os.environ["HOME"], (STATE["root"],)),
        "modules_from_repo": all_under(paths, REPO),
        "no_reachable_home_paths": not any(entry["under_home"] for entry in reach),
    }
    try:
        socket.create_connection(("192.0.2.1", 9), timeout=0.01)  # TEST-NET-1, never sent
        checks["network_sentinel"] = False
    except OSError:
        checks["network_sentinel"] = GUARD_HITS[-1:] == ["socket.create_connection"]
    GUARD_HITS.clear()
    stream = io.StringIO()
    in_process = run_names([REGRESSION], stream)
    checks["in_process_focused_clean"] = clean(in_process)
    copy_root = run / "copy"
    copy_root.mkdir(mode=0o700)
    copy_repo = copy_root / "repo"
    shutil.copytree(REPO, copy_repo, symlinks=True, ignore=shutil.ignore_patterns("__pycache__"))
    evidence = copy_root / "evidence"
    child_argv = [sys.executable, str(copy_repo / "tools" / "accept_fa51_native_sha.py"),
                  "--case", "behavior", "--output-parent", str(evidence)]
    child_env = {key: os.environ[key] for key in ("LANG", "LC_ALL", "TMPDIR", "HOME") if key in os.environ}
    child_env["PYTHONDONTWRITEBYTECODE"] = "1"
    done = subprocess.run(child_argv, cwd=copy_repo, capture_output=True, env=child_env, check=False)
    (run / "copy-child-stdout.txt").write_bytes(done.stdout)
    (run / "copy-child-stderr.txt").write_bytes(done.stderr)
    runs = sorted(evidence.iterdir()) if evidence.is_dir() else []
    child = json.loads((runs[0] / "result.json").read_bytes()) if len(runs) == 1 else {}
    own = identity()
    checks["copy_child_exit_0"] = done.returncode == 0
    checks["copy_child_pass"] = child.get("result") == "PASS_REVIEW_REQUIRED" and clean(child.get("counts", {"tests_run": 0}))
    checks["copy_modules_from_copy"] = bool(child.get("module_paths")) and all_under(child["module_paths"], copy_repo)
    checks["copy_same_sources"] = (child.get("identity", {}).get("source_sha256") == own["source_sha256"]
                                   and child.get("identity", {}).get("tree_sha256") == own["tree_sha256"]
                                   and child.get("identity", {}).get("head") == own["head"])
    ok = all(checks.values()) and guards_quiet()
    return ok, {"checks": checks, "module_paths": paths, "reachable_absolute_paths": reach,
                "in_process_counts": in_process, "copy_child": {
                    "argv": child_argv, "cwd": str(copy_repo), "exit": done.returncode,
                    "stdout": "copy-child-stdout.txt", "stdout_sha256": sha256_bytes(done.stdout),
                    "stderr": "copy-child-stderr.txt", "stderr_sha256": sha256_bytes(done.stderr),
                    "result_json": str(runs[0] / "result.json") if len(runs) == 1 else None,
                    "counts": child.get("counts"), "module_paths": child.get("module_paths")},
                **save(run, "unittest.txt", stream.getvalue())}


def case_credentials(run):
    tool = Path(sys.executable).parent / "detect-secrets"
    if not (tool.is_file() and os.access(tool, os.X_OK)):
        return False, {"stop": "missing_dependency: detect-secrets beside the interpreter; no install or substitution"}
    scan_argv = [str(tool), "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"]
    report = run / "secret-scan.json"
    with open(report, "wb") as out:
        scan = subprocess.run(scan_argv, cwd=REPO, stdout=out, stderr=subprocess.PIPE, check=False)
    (run / "scan-stderr.txt").write_bytes(scan.stderr)
    base = {"scan_argv": scan_argv, "scan_cwd": str(REPO), "scan_exit": scan.returncode,
            "scan_stderr_sha256": sha256_bytes(scan.stderr)}
    if scan.returncode != 0:
        return False, {**base, "stop": "scan_failed"}
    check_argv = [sys.executable, str(REPO / "tools" / "check_secret_report.py"), str(report)]
    check = subprocess.run(check_argv, cwd=REPO, capture_output=True, check=False)
    (run / "check-stdout.txt").write_bytes(check.stdout)
    (run / "check-stderr.txt").write_bytes(check.stderr)
    try:
        data = json.loads(report.read_bytes())
        files_with_hits = sorted(data.get("results", {}))
        version = data.get("version")
    except (ValueError, AttributeError):
        return False, {**base, "stop": "scan_report_unparseable"}
    return check.returncode == 0, {
        **base, "scan_json": "secret-scan.json", "scan_json_sha256": sha256_bytes(report.read_bytes()),
        "detect_secrets_version": version, "files_with_hits": files_with_hits,
        "check_argv": check_argv, "check_cwd": str(REPO), "check_exit": check.returncode,
        "check_stdout_sha256": sha256_bytes(check.stdout), "check_stderr_sha256": sha256_bytes(check.stderr),
        "check_last_line": (check.stdout + check.stderr).decode(errors="replace").strip().splitlines()[-1:]}


CASES = ("behavior", "compatibility", "negative-control", "isolation", "credentials")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Scoped offline acceptance for FA51 native source digests.")
    parser.add_argument("--case", required=True, choices=CASES)
    parser.add_argument("--output-parent", required=True)
    args = parser.parse_args(argv)
    run = fresh_run_dir(args.output_parent, args.case)
    before = identity()
    guard = install_guards(run)
    try:
        if not guard["ok"]:
            ok, details = False, {"stop": "guard_self_check_failed"}
        elif args.case == "behavior":
            ok, details = case_behavior(run)
        elif args.case == "compatibility":
            ok, details = case_compatibility(run)
        elif args.case == "negative-control":
            ok, details = case_negative_control(run)
        elif args.case == "isolation":
            ok, details = case_isolation(run, guard)
        else:
            ok, details = case_credentials(run)
    except Exception as exc:  # any unknown failure stops acceptance
        ok, details = False, {"stop": f"unknown_failure: {type(exc).__name__}"}
    after = identity()
    if after != before:
        ok, details["stop"] = False, "candidate_identity_changed_during_run"
    if not guards_quiet():
        ok, details["stop"] = False, "guard_refusal_during_run"
    record = {"case": args.case, "argv": [sys.executable, *sys.argv], "cwd": os.getcwd(),
              "interpreter": sys.executable, "python": sys.version.split()[0], "run_dir": str(run),
              "guards": {**guard, "network_hits": list(GUARD_HITS), "refused_writes": list(WRITE_HITS),
                         "refused_reads": list(READ_HITS), "refused_processes": list(PROC_HITS)},
              "identity": before, "result": "PASS_REVIEW_REQUIRED" if ok else "FAIL",
              "exit": 0 if ok else 1, **details}
    (run / "result.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    print(json.dumps({key: record[key] for key in ("case", "result", "exit", "run_dir")}, sort_keys=True))
    for key in ("counts", "broken_control", "candidate", "checks", "stop", "scan_exit", "check_exit"):
        if key in record:
            value = record[key]
            if isinstance(value, dict):
                value = {k: v for k, v in value.items() if k != "passed_ids"}
            print(key, json.dumps(value, sort_keys=True))
    return record["exit"]


if __name__ == "__main__":
    raise SystemExit(main())
