"""Scoped offline acceptance runner for FA56 (synthetic collection query identity).

One invocation runs exactly one case and writes evidence to a fresh unique
directory below --output-parent (absolute, outside this repository, with no
symlinked component):

  behavior          tests/records/test_fa56_collection_query.py (A1/A2/A3)
  compatibility     unchanged collection-page/export-bridge/selection tests
  negative-control  A4: the regression with the old blank-dropping parse_qs
                    restored (the "_ambiguous_" tests must fail, every other
                    test must pass), then the candidate (all pass)
  isolation         A5: guard/redirect self-checks, real module paths, path
                    literal scan of the new files, and a rerun of the
                    behavior case from a second complete source copy
  credentials       installed detect-secrets scan + unchanged
                    tools/check_secret_report.py against that exact report

Before any repository import the runner disables bytecode writes, redirects
tempfile/TMPDIR into the run directory, refuses sockets/HTTP, refuses
Python-level writes outside the run directory and refuses reads under the
home directory outside the repository, run directory and interpreter roots.
These guards are test hygiene, not a hostile-code sandbox. A pass is
REVIEW REQUIRED; it is not full-suite CI or semantic acceptance.
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
import socket
import subprocess
import sys
import tempfile

REPO = Path(__file__).resolve().parents[1]
REGRESSION = "tests.records.test_fa56_collection_query"
COMPATIBILITY = ("tests.test_records_collection_pages", "tests.records.test_export_bridge",
                 "tests.records.test_export_selection_cli")
PRODUCT = "campaign_tool.records.portal.collection_pages"
NEW_FILES = ("tests/records/test_fa56_collection_query.py", "tools/accept_fa56_collection_query.py")
HASHED = ("campaign_tool/records/portal/collection_pages.py", *NEW_FILES,
          "tests/test_records_collection_pages.py", "tests/records/test_export_bridge.py",
          "tests/records/test_export_selection_cli.py", "tools/check_secret_report.py",
          "tools/check_public_tree.py")
AMBIGUOUS_MARK = "_ambiguous_"
CASES = ("behavior", "compatibility", "negative-control", "isolation", "credentials")

GUARD_HITS = []   # unexpected guard refusals: always fatal
PROBE_HITS = []   # refusals of the runner's own inert self-check probes
PROBE_PREFIX = "fa56-probe-"


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def git(*args):
    if not (REPO / ".git").exists():
        return "no-git-source-copy"
    done = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True, check=False)
    return done.stdout.strip() if done.returncode == 0 else "unavailable:" + str(done.returncode)


def tree_digest():
    digest = hashlib.sha256()
    for directory, dirs, files in os.walk(REPO):
        dirs[:] = sorted(d for d in dirs if d not in (".git", "__pycache__"))
        for name in sorted(files):
            path = Path(directory) / name
            if path.suffix == ".pyc":
                continue
            digest.update(str(path.relative_to(REPO)).encode() + b"\0" + hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def pyc_count():
    return sum(1 for _ in REPO.rglob("*.pyc"))


def identity():
    hashes = {rel: sha256_bytes((REPO / rel).read_bytes()) if (REPO / rel).is_file() else None for rel in HASHED}
    return {"repo": str(REPO), "head": git("rev-parse", "HEAD"), "status_porcelain": git("status", "--porcelain"),
            "source_sha256": hashes, "tree_sha256": tree_digest(), "pyc_files": pyc_count()}


def fresh_run_dir(parent, case):
    """Validate evidence ancestry before the first write, then create a unique child."""
    raw = Path(parent)
    if not raw.is_absolute():
        raise SystemExit("output_parent_must_be_absolute")
    if any(part in (".", "..") for part in raw.parts):
        raise SystemExit("output_parent_not_canonical")
    for ancestor in (raw, *raw.parents):
        if ancestor.is_symlink():
            raise SystemExit("output_parent_has_symlinked_component")
        if ancestor.exists() and not ancestor.is_dir():
            raise SystemExit("output_parent_component_not_directory")
    if raw == REPO or REPO in raw.parents:
        raise SystemExit("output_parent_inside_repository")
    if os.path.realpath(raw) != str(raw):
        raise SystemExit("output_parent_not_real_path")
    raw.mkdir(mode=0o700, parents=True, exist_ok=True)
    if raw.is_symlink() or not raw.is_dir():
        raise SystemExit("output_parent_unsafe_after_create")
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
    readable = {root, os.path.realpath(REPO), os.path.realpath(sys.prefix), os.path.realpath(sys.base_prefix),
                os.path.realpath(sys.exec_prefix)}
    readable.update(os.path.realpath(p) for p in sys.path if p)

    def resolve(target, dir_fd=None):
        text = os.fsdecode(target)
        if text.startswith("file:"):
            text = text[5:].split("?", 1)[0]
        if isinstance(dir_fd, int) and dir_fd >= 0 and not os.path.isabs(text):
            try:
                text = os.path.join(os.readlink("/proc/self/fd/" + str(dir_fd)), text)
            except OSError:
                return None
        return os.path.realpath(text)

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
            resolved = resolve(target, dir_fd)
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
    leaked = sorted(m for m in sys.modules if m == "campaign_tool" or m.startswith(("campaign_tool.", "tests.")))
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


def save(run, name, text):
    raw = text.encode()
    (run / name).write_bytes(raw)
    return {"raw_output": name, "raw_output_sha256": sha256_bytes(raw)}


def case_behavior(run):
    prepare_import_root(run)
    stream = io.StringIO()
    counts, ran, bad = Recorder().run([REGRESSION], stream)
    groups = {key: [t for t in ran if "." + key + "_" in t] for key in ("test_a1", "test_a2", "test_a3")}
    mapping = {"A1": groups["test_a1"], "A2": groups["test_a2"], "A3": groups["test_a3"]}
    paths = module_paths(PRODUCT, REGRESSION)
    paths_ok = len(paths) == 2 and all(within(p, os.path.realpath(REPO)) for p in paths.values())
    ok = clean(counts) and all(mapping.values()) and not bad and paths_ok
    return ok, {"modules": [REGRESSION], "counts": counts, "acceptance_map": mapping, "failed_test_ids": bad,
                "module_paths": paths, "module_paths_inside_repo": paths_ok, **save(run, "unittest.txt", stream.getvalue())}


def case_compatibility(run):
    prepare_import_root(run)
    stream = io.StringIO()
    counts, ran, bad = Recorder().run(COMPATIBILITY, stream)
    per_module = {m: sum(1 for t in ran if t.startswith(m + ".")) for m in COMPATIBILITY}
    ok = clean(counts) and all(per_module.values()) and not bad
    return ok, {"modules": list(COMPATIBILITY), "counts": counts, "tests_per_module": per_module,
                "failed_test_ids": bad, "module_paths": module_paths(PRODUCT, *COMPATIBILITY),
                **save(run, "unittest.txt", stream.getvalue())}


def case_negative_control(run):
    prepare_import_root(run)
    from unittest import mock
    from urllib.parse import parse_qs as library_parse_qs
    pages = __import__(PRODUCT, fromlist=["parse_qs"])
    __import__(REGRESSION)
    calls = []

    def baseline_parse_qs(qs, keep_blank_values=False, strict_parsing=False, **kwargs):
        # Restores the pre-FA56 behaviour: blank values are dropped.
        calls.append(bool(keep_blank_values))
        return library_parse_qs(qs, strict_parsing=strict_parsing, **kwargs)

    stream = io.StringIO()
    recorder = Recorder()
    stream.write("=== broken control: blank-dropping parse_qs restored ===\n")
    with mock.patch.object(pages, "parse_qs", baseline_parse_qs):
        broken, ran, broken_bad = recorder.run([REGRESSION], stream)
    stream.write("=== candidate: unchanged module ===\n")
    candidate, candidate_ran, candidate_bad = recorder.run([REGRESSION], stream)
    expected_red = sorted(t for t in ran if AMBIGUOUS_MARK in t)
    positives = sorted(t for t in ran if AMBIGUOUS_MARK not in t)
    required_positives = [t for t in positives if t.endswith((
        "test_a1_ordinary_multipage_keeps_exact_cursor_and_member_urls",
        "test_a1_terminal_empty_single_identity_parses",
        "test_a3_valid_pagination_scope_and_denominator_unchanged"))]
    surfaces = {"base_direct": any("base_ambiguous" in t for t in expected_red),
                "next_direct": any("next_ambiguous" in t for t in expected_red),
                "consumer": any("test_a3_ambiguous" in t for t in expected_red)}
    ok = (broken["tests_run"] > 0 and broken["errors"] == 0 and broken_bad == expected_red
          and all(surfaces.values()) and len(required_positives) == 3
          and not set(required_positives) & set(broken_bad)
          and calls and all(calls) and clean(candidate) and not candidate_bad and candidate_ran == ran)
    return ok, {"broken_control": broken, "broken_failed_test_ids": broken_bad,
                "expected_red_test_ids": expected_red, "broken_positive_test_ids_green": positives,
                "required_positives_green": required_positives, "red_surfaces": surfaces,
                "control_parse_calls_requested_blank_values": len(calls),
                "candidate": candidate, "candidate_failed_test_ids": candidate_bad,
                **save(run, "unittest.txt", stream.getvalue())}


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
    """Report absolute-path module constants and bound defaults reachable from imports."""
    allowed = [os.path.realpath(run), os.path.realpath(REPO)]
    found = []

    def check(where, value):
        if isinstance(value, (str, os.PathLike)):
            text = os.fspath(value)
            if isinstance(text, str) and text.startswith(os.sep) and len(text) > 1 and "\n" not in text \
                    and not any(within(os.path.realpath(text), a) for a in allowed) and text != os.devnull:
                found.append(where)
    for name, module in sorted(sys.modules.items()):
        if not (name == "campaign_tool" or name.startswith("campaign_tool.")) or module is None:
            continue
        for attr, value in sorted(vars(module).items()):
            check(name + "." + attr, value)
            defaults = list(getattr(value, "__defaults__", None) or ()) + list(
                (getattr(value, "__kwdefaults__", None) or {}).values())
            for index, default in enumerate(defaults):
                check(name + "." + attr + ".default[" + str(index) + "]", default)
    return found


def case_isolation(run):
    tmp = prepare_import_root(run)
    checks = {"bytecode_disabled": sys.dont_write_bytecode is True,
              "tempdir_inside_run": within(os.path.realpath(tempfile.gettempdir()), os.path.realpath(run))
              and os.path.realpath(tempfile.gettempdir()) == os.path.realpath(tmp)}
    try:
        socket.create_connection(("192.0.2.1", 9), timeout=1)
        checks["network_probe_refused"] = False
    except OSError as exc:
        checks["network_probe_refused"] = "offline acceptance guard" in str(exc)
    product = __import__(PRODUCT, fromlist=["run_collection_pages"])
    __import__(REGRESSION)
    paths = module_paths(PRODUCT, REGRESSION, "tests.test_records_collection_pages")
    checks["module_paths_inside_repo"] = len(paths) == 3 and all(
        within(p, os.path.realpath(REPO)) for p in paths.values())
    defaults = product.run_collection_pages.__kwdefaults__
    checks["run_collection_pages_bound_defaults_have_no_paths"] = all(
        not isinstance(v, (str, os.PathLike)) for v in defaults.values())
    absolute = scan_absolute_constants(run)
    checks["no_unredirected_absolute_path_constants"] = not absolute
    hits = literal_hits()
    checks["new_files_have_no_machine_paths"] = not hits
    stream = io.StringIO()
    counts, ran, bad = Recorder().run([REGRESSION], stream)
    checks["focused_tests_green_in_place"] = clean(counts) and not bad
    details = {"checks": checks, "module_paths": paths, "absolute_constants": absolute, "literal_hits": hits,
               "in_place_counts": counts, **save(run, "unittest.txt", stream.getvalue())}
    copy = run / "source-copy"
    shutil.copytree(REPO, copy, symlinks=True, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
    child_out = run / "copy-evidence"
    argv = [sys.executable, str(copy / "tools" / "accept_fa56_collection_query.py"), "--case", "behavior",
            "--output-parent", str(child_out)]
    child = subprocess.run(argv, cwd=copy, capture_output=True, check=False)
    (run / "copy-child-stdout.txt").write_bytes(child.stdout)
    (run / "copy-child-stderr.txt").write_bytes(child.stderr)
    produced = sorted(child_out.glob("behavior-*/result.json")) if child_out.is_dir() else []
    record = json.loads(produced[0].read_text()) if len(produced) == 1 else {}
    child_paths = record.get("module_paths", {})
    checks["copy_child_exit_zero"] = child.returncode == 0 and record.get("exit") == 0
    checks["copy_child_counts_clean"] = bool(record) and clean(record.get("counts", {"tests_run": 0, "failures": 1,
        "errors": 0, "skipped": 0, "expected_failures": 0, "unexpected_successes": 0}))
    checks["copy_child_same_test_count"] = record.get("counts", {}).get("tests_run") == counts["tests_run"]
    checks["copy_child_modules_from_copy"] = len(child_paths) == 2 and all(
        within(p, os.path.realpath(copy)) for p in child_paths.values())
    checks["copy_is_complete_source_tree"] = record.get("identity", {}).get("tree_sha256") == tree_digest()
    details.update({"checks": checks, "source_copy": str(copy), "copy_child_argv": argv, "copy_child_cwd": str(copy),
                    "copy_child_exit": child.returncode, "copy_child_result": str(produced[0]) if produced else None,
                    "copy_child_module_paths": child_paths,
                    "copy_child_stdout_sha256": sha256_bytes(child.stdout),
                    "copy_child_stderr_sha256": sha256_bytes(child.stderr)})
    return all(checks.values()), details


def resolve_detect_secrets():
    sibling = Path(sys.executable).parent / "detect-secrets"
    return str(sibling) if sibling.is_file() and os.access(sibling, os.X_OK) else None


def case_credentials(run):
    tool = resolve_detect_secrets()
    if tool is None:
        return False, {"stop": "missing_dependency: detect-secrets not installed beside the interpreter"}
    scan_argv = [tool, "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"]
    report = run / "secret-scan.json"
    with open(report, "wb") as out:
        scan = subprocess.run(scan_argv, cwd=REPO, stdout=out, stderr=subprocess.PIPE, check=False)
    (run / "scan-stderr.txt").write_bytes(scan.stderr)
    details = {"scan_argv": scan_argv, "scan_cwd": str(REPO), "scan_exit": scan.returncode,
               "scan_json": "secret-scan.json", "scan_json_sha256": sha256_bytes(report.read_bytes())}
    if scan.returncode != 0:
        return False, {**details, "stop": "scan_failed"}
    try:
        data = json.loads(report.read_bytes())
    except ValueError:
        return False, {**details, "stop": "scan_report_unparseable"}
    check_argv = [sys.executable, str(REPO / "tools" / "check_secret_report.py"), str(report)]
    check = subprocess.run(check_argv, cwd=REPO, capture_output=True, check=False)
    raw = check.stdout + check.stderr
    (run / "check-secret-report.txt").write_bytes(raw)
    return check.returncode == 0, {**details, "detect_secrets_version": data.get("version"),
                                   "files_with_hits": sorted(data.get("results", {})),
                                   "check_argv": check_argv, "check_cwd": str(REPO), "check_exit": check.returncode,
                                   "check_output_sha256": sha256_bytes(raw)}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Scoped offline acceptance for FA56 collection query identity.")
    parser.add_argument("--case", required=True, choices=CASES)
    parser.add_argument("--output-parent", required=True)
    args = parser.parse_args(argv)
    run = fresh_run_dir(args.output_parent, args.case)
    before = identity()
    guard = install_file_guards(run)
    details = {}
    try:
        if not (guard["write_escape_refused"] and guard["home_read_refused"] and guard["in_root_positive_written"]):
            ok, details = False, {"stop": "guard_self_check_failed"}
        elif args.case == "behavior":
            ok, details = case_behavior(run)
        elif args.case == "compatibility":
            ok, details = case_compatibility(run)
        elif args.case == "negative-control":
            ok, details = case_negative_control(run)
        elif args.case == "isolation":
            ok, details = case_isolation(run)
        else:
            ok, details = case_credentials(run)
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
              "interpreter": sys.executable, "python": sys.version.split()[0], "run_dir": str(run),
              "guards": {**guard, "unexpected_refusals": list(GUARD_HITS), "inert_probe_refusals": len(PROBE_HITS)},
              "identity": before, "result": "PASS_REVIEW_REQUIRED" if ok else "FAIL", "exit": 0 if ok else 1,
              **details}
    (run / "result.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    print(json.dumps(record, indent=2, sort_keys=True))
    return record["exit"]


if __name__ == "__main__":
    raise SystemExit(main())
