"""FA42 acceptance runner: nonblocking special-file refusal in catalog_links._read.

Offline only.  Exactly one --case per invocation; each writes a brand-new run
directory below --output-parent (which must lie outside this repository):

  behavior          A1/A2/A3/A5 requirement-mapped tests in
                    tests/records/test_fa42_link_nonblock.py
  compatibility     unchanged tests/records/test_catalog_links.py
  negative-control  A4 in-memory deciding controls, plus the inert flag check
                    against the preserved unchanged baseline _read (loaded
                    from git into memory, never written over product source)
  isolation         A6: guards installed before application imports, focused
                    suite run in-process from a second complete source copy
  full-ci           SD-15 discovery, records.yml discovery and the S3a
                    invocation under -W error::ResourceWarning
  credentials       adjacent detect-secrets scan + unchanged
                    tools/check_secret_report.py on that exact report

Children are launched with argv arrays (no shell).  TMPDIR is set to a per-run
test-data directory only.  A pass is REVIEW REQUIRED: machine-valid evidence,
not semantic, legal, human, stage or release acceptance.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import sys
import time

REPO = Path(__file__).resolve().parents[1]
MODULE = "tests.records.test_fa42_link_nonblock"
TEST_FILE = "tests/records/test_fa42_link_nonblock.py"
HELPER_FILE = "tools/accept_fa42_link_nonblock.py"
PRODUCT_FILE = "campaign_tool/records/catalog_links.py"
COMPAT_MODULES = ("tests.records.test_catalog_links",)
COMPAT_FILES = ("tests/records/test_catalog_links.py",)
S3A_MODULES = ("tests.records.test_mail_resolution_census", "tests.records.test_mail_adoption",
               "tests.records.test_original_reconciliation")
CASES = ("behavior", "compatibility", "negative-control", "isolation", "full-ci", "credentials")
CHILD_TIMEOUT = 600
FULL_TIMEOUT = 3600

BEHAVIOR_MAP = {
    "A1-positive": [
        "RegularReadTests.test_empty_and_nonempty_regular_bytes_exact",
        "RegularReadTests.test_changed_descriptor_identity_refused",
        "RegularReadTests.test_unsafe_ancestry_leaf_symlink_and_missing_refused_before_open",
        "CatalogueIntegrationTests.test_snapshot_registry_run_and_reuse_through_nonblocking_reads",
        "CatalogueIntegrationTests.test_consumer_refusals_preserved",
    ],
    "A2-negative": [
        "FinalOpenFlagTests.test_inert_seam_final_open_is_nonblocking_nofollow_readonly",
        "FinalOpenFlagTests.test_delegating_seam_flags_on_regular_read",
        "RegularReadTests.test_stable_fifo_refused_by_genuine_precheck_without_open",
        "DescriptorLifecycleTests.test_nonregular_fstat_doubles_refused_before_read_and_closed",
        "FifoReplacementRaceTests.test_regular_to_writerless_fifo_after_precheck_is_invalid_input_file",
    ],
    "A3-boundary-recovery": [
        "RegularReadTests.test_limit_edge_uses_existing_bound",
        "RegularReadTests.test_growth_past_bound_is_source_changed_or_oversize",
        "DescriptorLifecycleTests.test_fstat_failure_closes_descriptor",
        "DescriptorLifecycleTests.test_read_failure_closes_descriptor",
        "DescriptorLifecycleTests.test_open_failures_map_and_acquire_nothing",
        "DescriptorLifecycleTests.test_repeated_mixed_runs_do_not_leak_descriptors",
    ],
    "A5-integration": [
        "CatalogueIntegrationTests.test_snapshot_registry_run_and_reuse_through_nonblocking_reads",
        "CatalogueIntegrationTests.test_snapshot_reader_bytes_and_bindings_exact",
        "FifoReplacementRaceTests.test_snapshot_consumer_fifo_replacement_refused_without_output",
    ],
}
NEGATIVE_TESTS = [
    "NegativeControlTests.test_control_without_nonblock_fails_flag_assertion_before_special_open",
    "NegativeControlTests.test_control_permitting_nonregular_read_fails_no_read_assertion",
]
STATUS_LINE = re.compile(r"^(test\w*) \(([\w.]+)\) \.\.\. (.+)$", re.M)
RAN_LINE = re.compile(r"^Ran (\d+) tests? in ", re.M)
FINAL_LINE = re.compile(r"^(OK|FAILED)(?: \(([^)]*)\))?\s*$", re.M)
HIJACK_ENV = ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONINSPECT", "PYTHONUSERBASE",
              "PYTHONEXECUTABLE", "PYTHONSAFEPATH")


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    path = Path(path)
    return sha256_bytes(path.read_bytes()) if path.is_file() else None


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def git(*args):
    done = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True,
                          check=False)
    return done.returncode, done.stdout.strip()


def baseline_ref():
    # Baseline = parent of the commit that added the FA42 test file; before that
    # commit exists the baseline is HEAD itself.
    code, added = git("log", "--format=%H", "--diff-filter=A", "--", TEST_FILE)
    commits = added.split() if code == 0 else []
    if not commits:
        return git("rev-parse", "HEAD")[1]
    code, parent = git("rev-parse", commits[-1] + "^")
    return parent if code == 0 else None


def identity():
    hashed = (PRODUCT_FILE, TEST_FILE, HELPER_FILE, *COMPAT_FILES, "tools/check_secret_report.py",
              "tools/check_public_tree.py", ".github/workflows/records.yml")
    return {"head": git("rev-parse", "HEAD")[1],
            "status_porcelain": git("status", "--porcelain", "--untracked-files=all")[1],
            "baseline_ref": baseline_ref(),
            "source_sha256": {name: sha256_file(REPO / name) for name in hashed}}


# ------------------------------------------------------------ evidence roots

def safe_output_parent(raw):
    parent = Path(raw)
    if not parent.is_absolute():
        raise SystemExit("output_parent_must_be_absolute")
    parent = Path(os.path.normpath(parent))
    if parent == REPO or REPO in parent.parents:
        raise SystemExit("output_parent_inside_repository")
    for ancestor in reversed(parent.parents):
        try:
            info = os.lstat(ancestor)
        except FileNotFoundError:
            os.mkdir(ancestor, 0o700)
            info = os.lstat(ancestor)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise SystemExit("output_parent_unsafe_ancestor")
        if info.st_mode & stat.S_IWOTH and not info.st_mode & stat.S_ISVTX:
            raise SystemExit("output_parent_world_writable_ancestor")
    try:
        os.mkdir(parent, 0o700)
    except FileExistsError:
        pass
    info = os.lstat(parent)
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid() or info.st_mode & 0o022):
        raise SystemExit("output_parent_unsafe")
    if Path(os.path.realpath(parent)) != parent:
        raise SystemExit("output_parent_not_canonical")
    return parent


def fresh_run_dir(parent, case):
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run = parent / f"{case}-{stamp}-{os.getpid()}-{secrets.token_hex(4)}"
    os.mkdir(run, 0o700)
    os.mkdir(run / "tmp", 0o700)
    return run


def child_env(run):
    env = {key: value for key, value in os.environ.items() if key not in HIJACK_ENV}
    env["TMPDIR"] = str(run / "tmp")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONHASHSEED"] = "0"
    return env


def run_child(run, label, argv, *, cwd, timeout=CHILD_TIMEOUT, stdout_path=None):
    started = time.monotonic()
    stdout_target = stdout_path or run / f"{label}.stdout.txt"
    stderr_target = run / f"{label}.stderr.txt"
    with open(stdout_target, "wb") as out, open(stderr_target, "wb") as err:
        try:
            done = subprocess.run(argv, cwd=cwd, env=child_env(run), stdout=out, stderr=err,
                                  timeout=timeout, check=False)
            exit_code, timed_out = done.returncode, False
        except subprocess.TimeoutExpired:
            exit_code, timed_out = None, True
    return {"label": label, "argv": [str(part) for part in argv], "cwd": str(cwd),
            "exit": exit_code, "timed_out": timed_out,
            "seconds": round(time.monotonic() - started, 3),
            "stdout_path": str(stdout_target), "stdout_sha256": sha256_file(stdout_target),
            "stderr_path": str(stderr_target), "stderr_sha256": sha256_file(stderr_target)}


def parse_unittest(text):
    statuses = {}
    for name, where, status in STATUS_LINE.findall(text):
        statuses[f"{where}" if where.endswith("." + name) else f"{where}.{name}"] = status.strip()
    ran = [int(value) for value in RAN_LINE.findall(text)]
    finals = FINAL_LINE.findall(text)
    counts = {"failures": 0, "errors": 0, "skipped": 0, "expected failures": 0,
              "unexpected successes": 0}
    final = finals[-1][0] if finals else None
    if finals and finals[-1][1]:
        for part in finals[-1][1].split(","):
            key, _, value = part.strip().partition("=")
            if key in counts and value.isdigit():
                counts[key] = int(value)
    return {"ran": ran[-1] if ran else None, "final": final, "counts": counts,
            "statuses": statuses}


def unittest_step(run, label, argv, failures, *, expected_ids=None, expected_count=None,
                  timeout=CHILD_TIMEOUT):
    step = run_child(run, label, argv, cwd=REPO, timeout=timeout)
    text = Path(step["stderr_path"]).read_text(errors="replace")
    text += "\n" + Path(step["stdout_path"]).read_text(errors="replace")
    parsed = parse_unittest(text)
    step["unittest"] = {key: parsed[key] for key in ("ran", "final", "counts")}
    step["unittest"]["status_count"] = len(parsed["statuses"])
    if step["timed_out"] or step["exit"] != 0:
        failures.append(f"{label}: exit {step['exit']} timed_out={step['timed_out']}")
    if not parsed["ran"]:
        failures.append(f"{label}: empty or missing test population")
    if parsed["final"] != "OK":
        failures.append(f"{label}: final status {parsed['final']}")
    for key, value in parsed["counts"].items():
        if value:
            failures.append(f"{label}: {key}={value}")
    if expected_count is not None and parsed["ran"] != expected_count:
        failures.append(f"{label}: ran {parsed['ran']} != expected {expected_count}")
    if expected_ids is not None:
        missing = [test for test in expected_ids if parsed["statuses"].get(test) != "ok"]
        step["unittest"]["expected_ids"] = {test: parsed["statuses"].get(test)
                                            for test in expected_ids}
        if missing:
            failures.append(f"{label}: not ok: {missing}")
    return step, parsed


def count_tests(relative):
    text = (REPO / relative).read_text(encoding="utf-8")
    return len(re.findall(r"^    def test_\w+\(self", text, re.M))


def interpreter():
    return [sys.executable, "-B", "-W", "error::ResourceWarning"]


# --------------------------------------------------------------------- cases

def case_behavior(run, record, failures):
    ids = []
    for tests in BEHAVIOR_MAP.values():
        for test in tests:
            if f"{MODULE}.{test}" not in ids:
                ids.append(f"{MODULE}.{test}")
    step, _ = unittest_step(run, "behavior", [*interpreter(), "-m", "unittest", "-v", *ids],
                            failures, expected_ids=ids, expected_count=len(ids))
    record["steps"].append(step)
    record["requirement_map"] = {key: [f"{MODULE}.{test}" for test in tests]
                                 for key, tests in BEHAVIOR_MAP.items()}


def case_compatibility(run, record, failures):
    base = record["identity"]["baseline_ref"]
    for relative in COMPAT_FILES:
        code, _ = git("diff", "--quiet", base or "HEAD", "--", relative)
        dirty = git("status", "--porcelain", "--", relative)[1]
        record.setdefault("unchanged_checks", {})[relative] = {
            "baseline_ref": base, "diff_exit": code, "status": dirty}
        if code != 0 or dirty:
            failures.append(f"compatibility file changed: {relative}")
    expected = sum(count_tests(relative) for relative in COMPAT_FILES)
    step, _ = unittest_step(run, "compatibility",
                            [*interpreter(), "-m", "unittest", "-v", *COMPAT_MODULES],
                            failures, expected_count=expected)
    record["steps"].append(step)


def install_guards(run, allowed_extra=()):
    """Offline/live-file guards; must run before any application import."""
    import socket
    import tempfile
    import urllib.request
    import http.client

    hits = []

    def refuse(*_args, **_kwargs):
        hits.append("network")
        raise OSError("fa42 acceptance: network/model access refused")

    socket.socket.connect = refuse
    socket.socket.connect_ex = refuse
    socket.create_connection = refuse
    socket.getaddrinfo = refuse
    urllib.request.urlopen = refuse
    http.client.HTTPConnection.connect = refuse
    sys.dont_write_bytecode = True
    os.environ["TMPDIR"] = str(run / "tmp")
    tempfile.tempdir = str(run / "tmp")
    writable = sorted({str(run), os.path.realpath(run)})
    readable = [*map(str, allowed_extra), sys.prefix, sys.base_prefix, sys.exec_prefix,
                sys.base_exec_prefix, os.path.dirname(os.__file__), "/dev/null", "/dev/urandom",
                "/proc/self", "/dev/fd", "/etc/localtime", "/usr/share/zoneinfo"]
    readable = sorted({os.path.realpath(item) for item in readable} | set(readable))
    allowed = {"writable": writable, "read_only": readable}

    def inside(path, roots):
        return any(path == root or path.startswith(root.rstrip("/") + "/") for root in roots)

    def read_only(mode, flags):
        if isinstance(mode, str):
            return not set(mode) & set("wax+")
        if isinstance(flags, int):
            return (flags & os.O_ACCMODE == os.O_RDONLY
                    and not flags & (os.O_CREAT | os.O_TRUNC | os.O_APPEND))
        return False

    def hook(event, args):
        if event != "open" or not args:
            return
        target = args[0]
        if isinstance(target, int):
            return
        if isinstance(target, bytes):
            target = os.fsdecode(target)
        if not isinstance(target, str):
            target = os.fspath(target)
        absolute = os.path.abspath(target)
        mode = args[1] if len(args) > 1 else None
        flags = args[2] if len(args) > 2 else None
        if inside(absolute, writable):
            return
        if inside(absolute, readable) and read_only(mode, flags):
            return
        hits.append("live-file:" + absolute[:200])
        raise PermissionError("fa42 acceptance: path outside per-run roots refused")

    sys.addaudithook(hook)
    probe = "/" + "fa42-escape-probe-" + secrets.token_hex(4)
    try:
        open(probe, "rb").close()
        probe_refused = False
    except PermissionError:
        probe_refused = True
    except OSError:
        probe_refused = False
    probe_hits = [hit for hit in hits if probe in hit]
    for hit in probe_hits:
        hits.remove(hit)
    inside_probe = run / "tmp" / "guard-positive.txt"
    inside_probe.write_text("ok\n")
    return {"hits": hits, "probe_refused": probe_refused,
            "inside_positive": inside_probe.read_text() == "ok\n", "allowed_roots": allowed}


def redirect_runtime_constants(run, modules):
    """Inventory absolute path constants/defaults in the closure; redirect any."""
    found = []
    for module in modules:
        for name, value in sorted(vars(module).items()):
            if isinstance(value, (str, Path)) and os.path.isabs(str(value)) and name.isupper():
                found.append(f"{module.__name__}.{name}")
                setattr(module, name, type(value)(run / "tmp" / "redirected" / name))
            function = value if callable(value) else None
            defaults = getattr(function, "__defaults__", None) or ()
            if getattr(function, "__module__", None) == module.__name__:
                for default in defaults:
                    if isinstance(default, (str, Path)) and os.path.isabs(str(default)):
                        found.append(f"{module.__name__}.{name}(default)")
    return found


def case_negative_control(run, record, failures):
    import importlib
    import types
    import unittest
    from unittest import mock

    guards = install_guards(run, allowed_extra=(REPO,))
    sys.path.insert(0, str(REPO))
    test_module = importlib.import_module(MODULE)
    candidate = importlib.import_module("campaign_tool.records.catalog_links")
    record["loaded_modules"] = {"test": test_module.__file__, "product": candidate.__file__}

    stream_path = run / "negative-control.unittest.txt"
    with open(stream_path, "w") as stream:
        suite = unittest.defaultTestLoader.loadTestsFromNames(
            [f"{MODULE}.{test}" for test in NEGATIVE_TESTS])
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    record["steps"].append({"label": "a4-in-memory-controls", "ran": result.testsRun,
                            "failures": len(result.failures), "errors": len(result.errors),
                            "skipped": len(result.skipped),
                            "output_path": str(stream_path),
                            "output_sha256": sha256_file(stream_path)})
    if (result.testsRun != len(NEGATIVE_TESTS) or result.failures or result.errors
            or result.skipped or not result.wasSuccessful()):
        failures.append("a4 in-memory controls did not all pass")

    base = record["identity"]["baseline_ref"]
    shown = subprocess.run(["git", "-C", str(REPO), "show", f"{base}:{PRODUCT_FILE}"],
                           capture_output=True, check=False)
    baseline_step = {"label": "preserved-baseline-flag-control", "baseline_ref": base}
    if shown.returncode != 0 or not shown.stdout:
        failures.append("baseline product source unavailable")
        record["steps"].append(baseline_step)
        return guards
    source = shown.stdout.decode("utf-8")
    baseline_copy = run / "baseline_catalog_links.py"
    baseline_copy.write_bytes(shown.stdout)
    baseline_step["baseline_sha256"] = sha256_file(baseline_copy)
    baseline_step["candidate_sha256"] = sha256_file(REPO / PRODUCT_FILE)
    module = types.ModuleType("campaign_tool.records._fa42_preserved_baseline")
    module.__file__ = str(baseline_copy)
    module.__package__ = "campaign_tool.records"
    exec(compile(source, str(baseline_copy), "exec"), module.__dict__)

    fixture = test_module.make_fixture_root(str(run / "tmp"))
    probe = test_module.write_private(fixture / "probe.json", b"{}\n")

    def inert_flags(target):
        opener = test_module.OpenRecorder(delegate=False)
        with mock.patch.object(os, "open", opener):
            try:
                target._read(probe)
                outcome = None
            except target.LinkError as error:
                outcome = error.code
        try:
            test_module.assert_final_open_flags(opener.calls, probe)
            verdict = "flags_ok"
        except AssertionError as error:
            verdict = f"flag_assertion_failed: {error}"
        return {"code": outcome, "verdict": verdict, "opens": len(opener.calls),
                "acquired": len(opener.fds)}

    baseline_step["baseline"] = inert_flags(module)
    baseline_step["candidate"] = inert_flags(candidate)
    record["steps"].append(baseline_step)
    if baseline_step["baseline_sha256"] == baseline_step["candidate_sha256"]:
        failures.append("baseline equals candidate; deciding control has no restored positive")
    if not baseline_step["baseline"]["verdict"].startswith("flag_assertion_failed") \
            or "O_NONBLOCK" not in baseline_step["baseline"]["verdict"]:
        failures.append("preserved baseline was not detected by the flag regression")
    if baseline_step["baseline"]["acquired"] or baseline_step["candidate"]["acquired"]:
        failures.append("inert seam acquired a descriptor")
    if baseline_step["candidate"]["verdict"] != "flags_ok" \
            or baseline_step["candidate"]["code"] != "unsafe_or_missing_input":
        failures.append("restored candidate positive did not pass")
    return guards


def case_isolation(run, record, failures):
    import importlib
    import unittest
    import warnings

    copy = run / "source-copy"
    shutil.copytree(REPO, copy, symlinks=True,
                    ignore=shutil.ignore_patterns(".git", "__pycache__", "node_modules"))
    record["source_copy"] = {"root": str(copy),
                             "test_sha256": sha256_file(copy / TEST_FILE),
                             "product_sha256": sha256_file(copy / PRODUCT_FILE)}
    if (record["source_copy"]["test_sha256"] != sha256_file(REPO / TEST_FILE)
            or record["source_copy"]["product_sha256"] != sha256_file(REPO / PRODUCT_FILE)):
        failures.append("source copy differs from repository")
    preloaded = sorted(name for name in sys.modules
                       if name.split(".")[0] in ("campaign_tool", "tests"))
    if preloaded:
        failures.append(f"application modules imported before guards: {preloaded}")
    guards = install_guards(run, allowed_extra=(REPO,))
    record["guards_installed_before_import"] = True
    os.chdir(copy)
    sys.path.insert(0, str(copy))
    warnings.simplefilter("error", ResourceWarning)
    test_module = importlib.import_module(MODULE)
    product = importlib.import_module("campaign_tool.records.catalog_links")
    closure = [product, importlib.import_module("campaign_tool.records.agency_reconciliation")]
    record["runtime_constants_redirected"] = redirect_runtime_constants(run, closure)
    loaded = {name: getattr(module, "__file__", None) for name, module in sorted(sys.modules.items())
              if name.split(".")[0] in ("campaign_tool", "tests") and module is not None}
    record["loaded_modules"] = loaded
    outside = [name for name, path in loaded.items()
               if path and not os.path.abspath(path).startswith(str(copy) + os.sep)]
    if outside:
        failures.append(f"modules loaded outside the second copy: {outside}")
    stream_path = run / "isolation.unittest.txt"
    with open(stream_path, "w") as stream:
        suite = unittest.defaultTestLoader.loadTestsFromModule(test_module)
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    expected = count_tests(TEST_FILE)
    record["steps"].append({"label": "focused-suite-second-copy", "ran": result.testsRun,
                            "expected": expected, "failures": len(result.failures),
                            "errors": len(result.errors), "skipped": len(result.skipped),
                            "output_path": str(stream_path),
                            "output_sha256": sha256_file(stream_path)})
    if (result.testsRun != expected or not expected or result.failures or result.errors
            or result.skipped or not result.wasSuccessful()):
        failures.append("focused suite from second copy did not fully pass")
    return guards


def case_full_ci(run, record, failures):
    before = git("status", "--porcelain", "--untracked-files=all")[1]
    py = sys.executable
    steps = [
        ("sd15-discovery", [py, "-m", "unittest", "discover", "-s", "tests", "-t", "."]),
        ("records-yml-discovery", [py, "-B", "-W", "error::ResourceWarning", "-m", "unittest",
                                   "discover", "-v"]),
        ("records-yml-s3a", [py, "-B", "-W", "error::ResourceWarning", "-m", "unittest",
                             *S3A_MODULES]),
    ]
    for label, argv in steps:
        step, parsed = unittest_step(run, label, argv, failures, timeout=FULL_TIMEOUT)
        if label == "records-yml-discovery":
            collected = sorted(name for name in parsed["statuses"] if name.startswith(MODULE))
            step["fa42_collected"] = len(collected)
            if len(collected) != count_tests(TEST_FILE):
                failures.append("FA42 tests not fully collected by ordinary discovery")
        record["steps"].append(step)
    after = git("status", "--porcelain", "--untracked-files=all")[1]
    record["tree_unchanged_by_suite"] = before == after
    if before != after:
        failures.append("full suite changed the repository tree")


def case_credentials(run, record, failures):
    scanner = Path(sys.executable).parent / "detect-secrets"
    if not scanner.is_file():
        failures.append("detect-secrets missing beside the interpreter (staging failure)")
        return
    report = run / "secret-scan.json"
    scan = run_child(run, "detect-secrets-scan",
                     [scanner, "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"],
                     cwd=REPO, stdout_path=report)
    record["steps"].append(scan)
    if scan["exit"] != 0:
        failures.append(f"detect-secrets exit {scan['exit']}")
    try:
        data = json.loads(report.read_text())
        results = data["results"]
        if not isinstance(results, dict):
            raise ValueError("results not a mapping")
        record["scan_summary"] = {"files_with_hits": len(results),
                                  "hits": sum(len(hits) for hits in results.values()),
                                  "hit_files": sorted(results)}
    except (OSError, ValueError, KeyError, TypeError) as error:
        failures.append(f"malformed secret-scan report: {type(error).__name__}")
        return
    check = run_child(run, "check-secret-report",
                      [sys.executable, "-B", "tools/check_secret_report.py", report], cwd=REPO)
    check["stdout_text"] = Path(check["stdout_path"]).read_text(errors="replace")[-4000:]
    record["steps"].append(check)
    if check["exit"] != 0:
        failures.append(f"check_secret_report exit {check['exit']}")
    if "potential secrets: 0;" not in check["stdout_text"]:
        failures.append("check_secret_report did not report zero potential secrets")


CASE_FUNCTIONS = {
    "behavior": case_behavior,
    "compatibility": case_compatibility,
    "negative-control": case_negative_control,
    "isolation": case_isolation,
    "full-ci": case_full_ci,
    "credentials": case_credentials,
}


def main(argv=None):
    parser = argparse.ArgumentParser(description="FA42 offline acceptance runner")
    parser.add_argument("--case", required=True)
    parser.add_argument("--output-parent", required=True)
    args = parser.parse_args(argv)
    if args.case not in CASES or set(CASE_FUNCTIONS) != set(CASES):
        print(json.dumps({"case": args.case, "verdict": "FAIL", "reason": "unknown_case"}))
        return 2
    parent = safe_output_parent(args.output_parent)
    run = fresh_run_dir(parent, args.case)
    record = {"schema": "fa42-link-nonblock-acceptance/v1", "case": args.case,
              "started_at": utc_now(), "argv": [sys.executable, *sys.argv],
              "cwd": os.getcwd(), "interpreter": sys.executable, "python": sys.version,
              "run_dir": str(run), "identity": identity(), "steps": []}
    failures = []
    try:
        guards = CASE_FUNCTIONS[args.case](run, record, failures)
    except Exception as error:  # fail closed; record type only
        failures.append(f"runner exception: {type(error).__name__}: {str(error)[:300]}")
        guards = None
    if guards is not None:
        record["guards"] = guards
        if guards["hits"]:
            failures.append(f"guard hits: {guards['hits'][:20]}")
        if not guards["probe_refused"] or not guards["inside_positive"]:
            failures.append("guard self-check failed")
    record["identity_after"] = identity()
    if record["identity_after"]["source_sha256"] != record["identity"]["source_sha256"] \
            or record["identity_after"]["head"] != record["identity"]["head"]:
        failures.append("source identity changed during the run")
    record["failures"] = failures
    record["verdict"] = "PASS_REVIEW_REQUIRED" if not failures else "FAIL"
    record["finished_at"] = utc_now()
    summary = run / "summary.json"
    summary.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"case": args.case, "verdict": record["verdict"], "run_dir": str(run),
                      "summary_sha256": sha256_file(summary), "failures": failures},
                     sort_keys=True))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
