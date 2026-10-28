"""Scoped offline acceptance runner for FA43 collection-member shape validation.

Usage: python3 tools/accept_fa43_collection_members.py --case CASE --output-parent DIR

Exactly six cases; each invocation writes to a NEW child directory of
--output-parent (absolute, outside this repository, no symlinked ancestor):

  behavior          tests/records/test_fa43_collection_members.py; every
                    acceptance label (a1..a5) must map to executed passing tests
  compatibility     the unchanged pinned collection/export test modules
  negative-control  disables ONLY the in-memory member-shape gate: the deciding
                    null/missing/unhashable tests must fail with the raw
                    TypeError/KeyError while the valid-page control passes;
                    the restored gate then passes all of them
  isolation         guard self-checks, unsafe evidence-root refusals,
                    committed-source portability scan, and the behavior case
                    rerun from a second complete source copy (module paths
                    recorded)
  full-ci           complete repository discovery (SD-15 argv and the
                    committed records.yml argv) plus the CI S3a modules, with
                    the same interpreter and a candidate-local TMPDIR
  credentials       installed detect-secrets next to this interpreter, then the
                    unchanged tools/check_secret_report.py on that exact report

Before any application module is imported, in-process cases disable bytecode,
point tempfile/TMPDIR/HOME/XDG_* at the per-run directory, refuse sockets and
urllib/http client connections, and install an audit hook that refuses writes
outside the run directory and reads of the invoking user's home outside the
repository, the run directory and the interpreter installation. These guards
are regression controls, not a sandbox. A pass is REVIEW REQUIRED only.
"""
import argparse
import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile

REPO = Path(__file__).resolve().parents[1]
HELPER = Path(__file__).resolve()
CASES = ("behavior", "compatibility", "negative-control", "isolation", "full-ci", "credentials")
REGRESSION = "tests.records.test_fa43_collection_members"
TEST_CLASS = REGRESSION + ".CollectionMemberShapeTests"
LABELS = ("a1", "a2", "a3", "a4", "a5")
COMPATIBILITY = ("tests.test_records_collection_pages", "tests.records.test_export_bridge",
                 "tests.records.test_export_selection_cli")
DECIDING = {"test_a4_null_member_is_portal_error": "TypeError",
            "test_a4_missing_kind_is_portal_error": "KeyError",
            "test_a4_unhashable_kind_is_portal_error": "TypeError"}
CONTROL = "test_a4_valid_page_control"
GATE = "_require_member_shape"
S3A = ("tests.records.test_mail_resolution_census", "tests.records.test_mail_adoption",
       "tests.records.test_original_reconciliation")
HASHED = ("campaign_tool/records/portal/collections.py", "tests/records/test_fa43_collection_members.py",
          "tools/accept_fa43_collection_members.py", "tests/test_records_collection_pages.py",
          "tests/records/test_export_bridge.py", "tests/records/test_export_selection_cli.py",
          "tools/check_secret_report.py", "tools/check_public_tree.py", ".github/workflows/records.yml")
REAL_HOME = os.path.realpath(pwd.getpwuid(os.getuid()).pw_dir)  # not $HOME, which a parent run may redirect
GUARD_HITS, WRITE_HITS, READ_HITS, PROBE_HITS = [], [], [], []
PROBE_PREFIX = "fa43-probe-"


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def git(*args):
    if not (REPO / ".git").exists():
        return "unavailable:not-a-checkout"  # e.g. the isolation source copy; never an enclosing repository
    done = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True, check=False)
    return done.stdout.strip() if done.returncode == 0 else "unavailable:" + str(done.returncode)


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
    """Reject a relative, in-repository or symlink-linked evidence parent before writing."""
    text = os.fspath(parent)
    if not os.path.isabs(text):
        raise ValueError("output_parent_must_be_absolute")
    path = Path(os.path.normpath(text))
    for ancestor in (path, *path.parents):
        if ancestor.is_symlink():
            raise ValueError("output_parent_linked_ancestor")
        if ancestor.exists() and not ancestor.is_dir():
            raise ValueError("output_parent_not_directory")
    if path == REPO or REPO in path.parents:
        raise ValueError("output_parent_inside_repository")
    return path


def fresh_run_dir(parent, case):
    path = safe_parent(parent)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.stat().st_uid != os.geteuid():
        raise ValueError("output_parent_not_owned")
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run = path / (case + "-" + stamp + "-" + str(os.getpid()) + "-" + secrets.token_hex(4))
    run.mkdir(mode=0o700, exist_ok=False)
    return run


# -- in-process guards --------------------------------------------------------
def install_network_guards():
    def refuse(name):
        def guard(*args, **kwargs):
            GUARD_HITS.append(name)
            raise OSError("offline acceptance guard refused " + name)
        return guard
    socket.socket.connect = refuse("socket.connect")
    socket.socket.connect_ex = refuse("socket.connect_ex")
    socket.create_connection = refuse("socket.create_connection")
    socket.getaddrinfo = refuse("socket.getaddrinfo")
    import http.client
    import urllib.request
    http.client.HTTPConnection.connect = refuse("http.client.connect")
    urllib.request.urlopen = refuse("urllib.request.urlopen")


WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
WRITE_EVENTS = {"os.mkdir": ((0, 2),), "os.rename": ((0, 2), (1, 3)), "os.replace": ((0, 2), (1, 3)),
                "os.remove": ((0, 1),), "os.rmdir": ((0, 1),), "os.truncate": ((0, None),),
                "os.symlink": ((1, 2),), "os.link": ((1, 3),), "os.chmod": ((0, 2),),
                "shutil.rmtree": ((0, 1),), "shutil.copyfile": ((1, None),), "sqlite3.connect": ((0, None),)}


def resolve_target(target, dir_fd=None):
    text = os.fsdecode(target)
    if text.startswith("file:"):
        text = text[5:].split("?", 1)[0]
    if isinstance(dir_fd, int) and dir_fd >= 0 and not os.path.isabs(text):
        try:
            text = os.path.join(os.readlink("/proc/self/fd/" + str(dir_fd)), text)
        except OSError:
            return None
    return os.path.realpath(text)


def within(resolved, roots):
    return any(resolved == root or resolved.startswith(root + os.sep) for root in roots)


def install_file_guard(run):
    root = os.path.realpath(run)
    readable = [root, os.path.realpath(REPO), os.path.realpath(sys.prefix), os.path.realpath(sys.base_prefix),
                os.path.realpath(sys.exec_prefix)]

    def hook(event, args):
        if event == "open":
            path, mode, flags = (tuple(args) + (None, None, None))[:3]
            if path is None or isinstance(path, int):
                return
            writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                isinstance(flags, int) and flags & WRITE_FLAGS)
            targets = [(path, None, writing)]
        elif event in WRITE_EVENTS:
            targets = [(args[p], args[d] if d is not None and d < len(args) else None, True)
                       for p, d in WRITE_EVENTS[event] if p < len(args) and args[p] is not None]
        else:
            return
        for target, dir_fd, writing in targets:
            if isinstance(target, int):
                continue
            text = os.fsdecode(target)
            if text in ("", ":memory:", os.devnull):
                continue
            resolved = resolve_target(target, dir_fd)
            probe = PROBE_PREFIX in text
            if writing and (resolved is None or not within(resolved, [root])):
                (PROBE_HITS if probe else WRITE_HITS).append(event + ":write")
                raise PermissionError("acceptance guard refused write outside per-run root")
            if not writing and resolved is not None and within(resolved, [REAL_HOME]) and not within(resolved, readable):
                (PROBE_HITS if probe else READ_HITS).append(event + ":read")
                raise PermissionError("acceptance guard refused live-file read")
    sys.addaudithook(hook)


def prepare_runtime(run):
    """Everything here happens before any repository module is imported."""
    sys.dont_write_bytecode = True
    tmp = run / "tmp"
    home = run / "home"
    tmp.mkdir()
    home.mkdir()
    tempfile.tempdir = str(tmp)
    for key in ("TMPDIR", "TMP", "TEMP"):
        os.environ[key] = str(tmp)
    os.environ["HOME"] = str(home)
    for key in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR"):
        os.environ[key] = str(home / key.lower())
    install_network_guards()
    install_file_guard(run)
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    leaked = sorted(name for name in sys.modules if name.split(".")[0] in ("campaign_tool", "tests"))
    if leaked:
        raise RuntimeError("application_imported_before_guards")
    return {"tmp": str(tmp), "home": str(home)}


def guard_self_check(run):
    checks = {}
    escape = os.path.join(os.sep, PROBE_PREFIX + secrets.token_hex(6), "write")
    try:
        open(escape, "w").close()
        checks["escape_write_refused"] = False
    except PermissionError:
        checks["escape_write_refused"] = True
    except OSError:
        checks["escape_write_refused"] = False
    live = os.path.join(REAL_HOME, PROBE_PREFIX + secrets.token_hex(6))
    try:
        open(live, "rb").close()
        checks["live_read_refused"] = False
    except PermissionError:
        checks["live_read_refused"] = True
    except OSError:
        checks["live_read_refused"] = False
    try:
        socket.create_connection(("127.0.0.1", 9), timeout=1)
        checks["socket_refused"] = False
    except OSError as exc:
        checks["socket_refused"] = "offline acceptance guard" in str(exc)
    import urllib.request
    try:
        urllib.request.urlopen("https://model.example.invalid/v1")
        checks["model_http_refused"] = False
    except OSError as exc:
        checks["model_http_refused"] = "offline acceptance guard" in str(exc)
    positive = run / "guard-positive.txt"
    positive.write_text("in-root write allowed\n")
    checks["in_root_write_allowed"] = positive.read_text() == "in-root write allowed\n"
    checks["probe_refusals"] = len(PROBE_HITS)
    GUARD_HITS.clear()  # the two deliberate network probes are not test hits
    checks["ok"] = all(checks[k] for k in ("escape_write_refused", "live_read_refused", "socket_refused",
                                            "model_http_refused", "in_root_write_allowed"))
    return checks


def bound_path_inventory():
    """Absolute filesystem paths in loaded application constants and bound defaults."""
    found = []

    def consider(where, value):
        if isinstance(value, Path):
            value = str(value)
        if isinstance(value, str) and value.startswith(os.sep) and "\n" not in value and len(value) < 4096:
            resolved = os.path.realpath(value)
            found.append({"where": where, "under_repo": within(resolved, [os.path.realpath(REPO)]),
                          "under_user_home": within(resolved, [REAL_HOME])})

    def defaults(where, func):
        for i, value in enumerate(getattr(func, "__defaults__", None) or ()):
            consider(where + ".__defaults__[" + str(i) + "]", value)
        for key, value in (getattr(func, "__kwdefaults__", None) or {}).items():
            consider(where + ".__kwdefaults__[" + key + "]", value)

    for name, module in sorted(sys.modules.items()):
        if module is None or name.split(".")[0] not in ("campaign_tool", "tests"):
            continue
        for attr, value in sorted(vars(module).items()):
            where = name + "." + attr
            consider(where, value)
            if callable(value) and getattr(value, "__module__", None) == name:
                defaults(where, value)
                for member, inner in sorted(vars(value).items()) if isinstance(value, type) else ():
                    defaults(where + "." + member, getattr(inner, "__func__", inner))
    unsafe = [f for f in found if f["under_user_home"] and not f["under_repo"]]
    return found, unsafe


# -- unittest plumbing ----------------------------------------------------------
def run_suite(suite, stream):
    import unittest

    class Recording(unittest.TextTestResult):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.passed = []

        def addSuccess(self, test):
            super().addSuccess(test)
            self.passed.append(test.id())

    runner = unittest.TextTestRunner(stream=stream, verbosity=2, resultclass=Recording)
    return runner.run(suite)


def counts(result):
    return {"tests_run": result.testsRun, "passed": len(result.passed), "failures": len(result.failures),
            "errors": len(result.errors), "skipped": len(result.skipped),
            "expected_failures": len(result.expectedFailures), "unexpected_successes": len(result.unexpectedSuccesses)}


def clean(c):
    return (c["tests_run"] > 0 and c["passed"] == c["tests_run"] and c["failures"] == 0 and c["errors"] == 0
            and c["skipped"] == 0 and c["expected_failures"] == 0 and c["unexpected_successes"] == 0)


def module_paths(names):
    return {name: os.path.realpath(sys.modules[name].__file__) for name in names if name in sys.modules}


def loaded_paths_inside_repo(paths):
    root = os.path.realpath(REPO)
    return all(within(p, [root]) for p in paths.values())


def write_raw(run, name, data):
    (run / name).write_bytes(data)
    return {"file": name, "sha256": sha256_bytes(data), "bytes": len(data)}


# -- cases --------------------------------------------------------------------
def case_behavior(run):
    import unittest
    stream = io.StringIO()
    result = run_suite(unittest.defaultTestLoader.loadTestsFromName(REGRESSION), stream)
    c = counts(result)
    mapping = {label: sorted(t.rsplit(".", 1)[-1] for t in result.passed
                             if t.rsplit(".", 1)[-1].startswith("test_" + label + "_")) for label in LABELS}
    raw = write_raw(run, "unittest.txt", stream.getvalue().encode())
    paths = module_paths(["campaign_tool.records.portal.collections", REGRESSION, "tests.records.test_export_bridge"])
    found, unsafe = bound_path_inventory()
    ok = (clean(c) and all(mapping.values()) and loaded_paths_inside_repo(paths) and len(paths) == 3
          and not unsafe and not GUARD_HITS)
    return ok, {"module": REGRESSION, "counts": c, "label_tests": mapping, "module_paths": paths,
                "bound_absolute_paths": len(found), "unsafe_bound_paths": unsafe,
                "guard_hits": list(GUARD_HITS), "raw_output": raw}


def case_compatibility(run):
    import unittest
    stream = io.StringIO()
    result = run_suite(unittest.defaultTestLoader.loadTestsFromNames(list(COMPATIBILITY)), stream)
    c = counts(result)
    per_module = {m: sum(1 for t in result.passed if t.startswith(m + ".")) for m in COMPATIBILITY}
    raw = write_raw(run, "unittest.txt", stream.getvalue().encode())
    paths = module_paths(list(COMPATIBILITY) + ["campaign_tool.records.portal.collections"])
    ok = clean(c) and all(per_module.values()) and loaded_paths_inside_repo(paths) and not GUARD_HITS
    return ok, {"modules": list(COMPATIBILITY), "counts": c, "passed_per_module": per_module,
                "module_paths": paths, "guard_hits": list(GUARD_HITS), "raw_output": raw}


def case_negative_control(run):
    import unittest
    from unittest import mock
    from campaign_tool.records.portal import collections
    names = [TEST_CLASS + "." + n for n in (*DECIDING, CONTROL)]
    stream = io.StringIO()
    if not callable(getattr(collections, GATE, None)):
        return False, {"stop": "shape_gate_missing"}
    original_source = sha256_bytes(Path(collections.__file__).read_bytes())
    stream.write("=== broken control: " + GATE + " replaced in memory by a no-op ===\n")
    with mock.patch.object(collections, GATE, lambda member: None):
        broken = run_suite(unittest.defaultTestLoader.loadTestsFromNames(names), stream)
    stream.write("=== restored gate: same tests ===\n")
    restored = run_suite(unittest.defaultTestLoader.loadTestsFromNames(names), stream)
    raw = write_raw(run, "unittest.txt", stream.getvalue().encode())
    outcomes = {}
    for test, trace in broken.errors + broken.failures:
        method = test.id().rsplit(".", 1)[-1]
        last = [line for line in trace.strip().splitlines() if line and not line.startswith(" ")][-1]
        outcomes[method] = last.split(":", 1)[0]
    detected = {m: outcomes.get(m) == exc for m, exc in DECIDING.items()}
    control_passed = TEST_CLASS + "." + CONTROL in broken.passed
    restored_c = counts(restored)
    ok = (all(detected.values()) and len(outcomes) == len(DECIDING) and control_passed and clean(restored_c)
          and restored_c["tests_run"] == len(names) and getattr(collections, GATE).__name__ == GATE
          and sha256_bytes(Path(collections.__file__).read_bytes()) == original_source and not GUARD_HITS)
    return ok, {"broken_counts": counts(broken), "broken_escaped_exception": outcomes, "deciding_detected": detected,
                "broken_valid_control_passed": control_passed, "restored_counts": restored_c,
                "product_source_unchanged_on_disk": original_source, "guard_hits": list(GUARD_HITS), "raw_output": raw}


def portability_scan():
    forbidden = sorted({REAL_HOME, str(REPO), str(REPO.parent), os.path.realpath(REPO.parent)})
    hits = {}
    for rel in ("tests/records/test_fa43_collection_members.py", "tools/accept_fa43_collection_members.py"):
        text = (REPO / rel).read_text()
        hits[rel] = [i for i, token in enumerate(forbidden) if token and token in text]
        if re.search(r"[\"'](?:/home/|/Users/|~/)", text):
            hits[rel].append("machine-home-literal")
        if re.search(r"(?m)^\s*(?:from|import)\s+(?:scratch|agent_work)", text):
            hits[rel].append("foreign-import")
    return {"files": sorted(hits), "hits": hits, "ok": not any(hits.values())}


def unsafe_root_refusals(run):
    target = run / "unsafe-target"
    target.mkdir()
    link = run / "unsafe-link"
    os.symlink(target, link)
    expected = {"relative": ("relative-evidence", "output_parent_must_be_absolute"),
                "linked_parent": (str(link), "output_parent_linked_ancestor"),
                "linked_ancestor": (str(link / "child"), "output_parent_linked_ancestor"),
                "inside_repo": (str(REPO / "evidence"), "output_parent_inside_repository"),
                "repo_itself": (str(REPO), "output_parent_inside_repository")}
    seen = {}
    for name, (parent, code) in expected.items():
        try:
            safe_parent(parent)
            seen[name] = "accepted"
        except ValueError as exc:
            seen[name] = str(exc)
    return {"observed": seen, "ok": all(seen[n] == code for n, (_, code) in expected.items())
            and not (REPO / "evidence").exists() and not (target / "child").exists()}


def case_isolation(run, checks):
    copy = run / "source-copy"
    shutil.copytree(REPO, copy, symlinks=True, ignore=shutil.ignore_patterns(".git", "__pycache__"))
    copy_helper = copy / "tools" / HELPER.name
    copy_evidence = run / "copy-evidence"
    argv = [sys.executable, "-B", str(copy_helper), "--case", "behavior", "--output-parent", str(copy_evidence)]
    env = dict(os.environ)
    child = subprocess.run(argv, cwd=copy, env=env, capture_output=True, check=False, timeout=900)
    out = write_raw(run, "copy-behavior-stdout.txt", child.stdout)
    err = write_raw(run, "copy-behavior-stderr.txt", child.stderr)
    results = sorted(copy_evidence.glob("behavior-*/result.json"))
    record = json.loads(results[0].read_text()) if len(results) == 1 else {}
    copy_root = os.path.realpath(copy)
    paths = record.get("module_paths", {})
    copy_ok = (child.returncode == 0 and record.get("result") == "PASS_REVIEW_REQUIRED" and len(paths) == 3
               and all(within(p, [copy_root]) for p in paths.values())
               and record.get("identity", {}).get("source_sha256") == identity()["source_sha256"])
    portability = portability_scan()
    refusals = unsafe_root_refusals(run)
    ok = copy_ok and portability["ok"] and refusals["ok"] and checks["ok"] and not GUARD_HITS
    return ok, {"guard_self_check": checks, "portability": portability, "unsafe_evidence_roots": refusals,
                "copy": {"argv": argv, "cwd": str(copy), "exit": child.returncode, "stdout": out, "stderr": err,
                         "result": record.get("result"), "counts": record.get("counts"),
                         "label_tests": record.get("label_tests"), "module_paths": paths,
                         "same_source_hashes_as_primary": record.get("identity", {}).get("source_sha256")
                         == identity()["source_sha256"], "copy_ok": copy_ok},
                "guard_hits": list(GUARD_HITS)}


SUMMARY = re.compile(r"^Ran (\d+) tests? in [0-9.]+s$", re.M)
STATUS = re.compile(r"^(OK|FAILED)(?: \((.*)\))?$", re.M)


def parse_unittest(stderr):
    ran = SUMMARY.findall(stderr)
    status = STATUS.findall(stderr)
    if len(ran) != 1 or not status:
        return None
    word, detail = status[-1]
    fields = dict(part.strip().split("=", 1) for part in detail.split(",") if "=" in part)
    return {"tests_run": int(ran[0]), "status": word, "failures": int(fields.get("failures", 0)),
            "errors": int(fields.get("errors", 0)), "skipped": int(fields.get("skipped", 0)),
            "expected_failures": int(fields.get("expected failures", 0)),
            "unexpected_successes": int(fields.get("unexpected successes", 0))}


def case_full_ci(run):
    tmp = run / "ci-tmp"
    home = run / "ci-home"
    tmp.mkdir()
    home.mkdir()
    env = dict(os.environ, TMPDIR=str(tmp), TMP=str(tmp), TEMP=str(tmp), HOME=str(home), PYTHONDONTWRITEBYTECODE="1")
    populations = {
        "sd15_discover": [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."],
        "records_yml_discover": [sys.executable, "-B", "-W", "error::ResourceWarning", "-m", "unittest", "discover", "-v"],
        "records_yml_s3a": [sys.executable, "-B", "-W", "error::ResourceWarning", "-m", "unittest", *S3A],
    }
    report, ok = {}, True
    for name, argv in populations.items():
        try:
            child = subprocess.run(argv, cwd=REPO, env=env, capture_output=True, check=False, timeout=3600)
            code, out, err = child.returncode, child.stdout, child.stderr
        except subprocess.TimeoutExpired as exc:
            code, out, err = "timeout", exc.stdout or b"", exc.stderr or b""
        parsed = parse_unittest(err.decode(errors="replace"))
        good = (code == 0 and parsed is not None and parsed["status"] == "OK" and parsed["tests_run"] > 0
                and parsed["failures"] == 0 and parsed["errors"] == 0 and parsed["skipped"] == 0
                and parsed["unexpected_successes"] == 0)
        listed = err.decode(errors="replace").count(REGRESSION + ".CollectionMemberShapeTests)")
        report[name] = {"argv": argv, "cwd": str(REPO), "exit": code, "counts": parsed, "ok": good,
                        "fa43_tests_listed_verbose": listed,
                        "stdout": write_raw(run, name + "-stdout.txt", out),
                        "stderr": write_raw(run, name + "-stderr.txt", err)}
        ok = ok and good
    ok = ok and report["records_yml_discover"]["fa43_tests_listed_verbose"] > 0
    return ok, {"populations": report, "tmpdir": str(tmp)}


def case_credentials(run):
    tool = Path(sys.executable).parent / "detect-secrets"
    if not (tool.is_file() and os.access(tool, os.X_OK)):
        return False, {"stop": "missing_dependency: detect-secrets is not installed beside this interpreter"}
    scan_argv = [str(tool), "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"]
    report = run / "secret-scan.json"
    with open(report, "wb") as out:
        scan = subprocess.run(scan_argv, cwd=REPO, stdout=out, stderr=subprocess.PIPE, check=False)
    scan_err = write_raw(run, "scan-stderr.txt", scan.stderr)
    if scan.returncode != 0:
        return False, {"scan_argv": scan_argv, "scan_exit": scan.returncode, "scan_stderr": scan_err, "stop": "scan_failed"}
    try:
        data = json.loads(report.read_bytes())
    except ValueError:
        return False, {"scan_argv": scan_argv, "stop": "malformed_scan_report"}
    if not isinstance(data, dict) or not isinstance(data.get("results"), dict):
        return False, {"scan_argv": scan_argv, "stop": "malformed_scan_report"}
    check_argv = [sys.executable, str(REPO / "tools" / "check_secret_report.py"), str(report)]
    check = subprocess.run(check_argv, cwd=REPO, capture_output=True, check=False)
    raw = write_raw(run, "check-secret-report.txt", check.stdout + check.stderr)
    return check.returncode == 0, {
        "scan_argv": scan_argv, "scan_cwd": str(REPO), "scan_exit": scan.returncode, "scan_stderr": scan_err,
        "scan_report": {"file": report.name, "sha256": sha256_bytes(report.read_bytes())},
        "detect_secrets_version": data.get("version"), "files_with_hits": sorted(data["results"]),
        "check_argv": check_argv, "check_exit": check.returncode, "check_output": raw}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Scoped offline acceptance for FA43 collection members.")
    parser.add_argument("--case", required=True, choices=CASES)
    parser.add_argument("--output-parent", required=True)
    args = parser.parse_args(argv)
    try:
        run = fresh_run_dir(args.output_parent, args.case)
    except (ValueError, OSError) as exc:
        print(json.dumps({"case": args.case, "result": "FAIL", "exit": 1, "stop": str(exc)}))
        return 1
    before = identity()
    runtime = checks = None
    try:
        if args.case in ("full-ci", "credentials"):
            ok, details = (case_full_ci if args.case == "full-ci" else case_credentials)(run)
        else:
            runtime = prepare_runtime(run)
            checks = guard_self_check(run)
            if not checks["ok"]:
                ok, details = False, {"stop": "guard_self_check_failed", "guard_self_check": checks}
            elif args.case == "behavior":
                ok, details = case_behavior(run)
            elif args.case == "compatibility":
                ok, details = case_compatibility(run)
            elif args.case == "negative-control":
                ok, details = case_negative_control(run)
            else:
                ok, details = case_isolation(run, checks)
    except Exception as exc:  # any unknown failure fails closed
        ok, details = False, {"stop": "unknown_failure: " + type(exc).__name__ + ": " + str(exc)}
    after = identity()
    if after != before:
        ok, details["stop"] = False, "candidate_identity_changed_during_run"
    if WRITE_HITS or READ_HITS:
        ok, details["stop"] = False, "guard_refused_file_access"
    record = {"case": args.case, "argv": [sys.executable, *sys.argv], "cwd": os.getcwd(),
              "interpreter": sys.executable, "python": sys.version.split()[0], "run_dir": str(run),
              "runtime_redirects": runtime, "guard_self_check": checks,
              "refused_writes": len(WRITE_HITS), "refused_reads": len(READ_HITS),
              "identity": before, "result": "PASS_REVIEW_REQUIRED" if ok else "FAIL", "exit": 0 if ok else 1,
              "note": "machine-valid only; semantic, human, legal, stage and release acceptance remain REVIEW REQUIRED",
              **details}
    (run / "result.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    print(json.dumps(record, indent=2, sort_keys=True))
    return record["exit"]


if __name__ == "__main__":
    raise SystemExit(main())
