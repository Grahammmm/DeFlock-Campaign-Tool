"""Scoped offline acceptance runner for FA45 (finite JSON numbers in unit capture).

Exactly one --case per invocation; each writes a fresh, never-reused child
directory below --output-parent, which must be absolute, outside this
repository and free of symlinked ancestors:

  behavior          tests.records.test_fa45_unit_finite, with every A1/A2/A3/A5
                    acceptance ID mapped to named tests that must all pass
  compatibility     unchanged tests.records.test_ledger_units
  negative-control  A4: the finite-float hook is bypassed in memory only
                    (unit_projection._finite_float -> float); the overflow
                    refusal tests must FAIL while finite/explicit-token
                    controls PASS; then the restored hook must pass them all
  isolation         A6: a second complete source copy under the run directory,
                    imported from there only; module paths, constants and
                    sentinels are recorded and checked
  full-ci           complete repository discovery (SD-15 argv and the
                    records.yml argv) plus the CI S3a modules, cwd repo,
                    candidate-local TMPDIR, same interpreter
  credentials       installed detect-secrets adjacent to this interpreter, then
                    unchanged tools/check_secret_report.py on that exact report

In-process cases disable bytecode, point tempfile/TMPDIR at the run
directory, refuse sockets and refuse (via an audit hook) Python-level file
writes, renames, removals and sqlite opens outside the run directory, all
before any application module is imported. The repository tree is content
hashed before and after each case. These are regression controls, not a
sandbox. PASS means REVIEW REQUIRED, never semantic or release acceptance.
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
import socket
import subprocess
import sys
import tempfile
from urllib.parse import unquote, urlsplit

REPO = Path(__file__).resolve().parents[1]
CASES = ("behavior", "compatibility", "negative-control", "isolation", "full-ci", "credentials")
REGRESSION = "tests.records.test_fa45_unit_finite"
COMPATIBILITY = ("tests.records.test_ledger_units",)
PRODUCT = "campaign_tool/records/ledger/unit_projection.py"
HASHED = (
    PRODUCT,
    "tests/records/test_fa45_unit_finite.py",
    "tools/accept_fa45_unit_finite.py",
    "tests/records/test_ledger_units.py",
    "tests/records/test_ledger.py",
    "campaign_tool/records/ledger/store.py",
    "tools/check_secret_report.py",
    "tools/check_public_tree.py",
    ".github/workflows/records.yml",
)
_R = REGRESSION + "."
ACCEPTANCE = {
    "A1": [_R + "FinitePositiveTests.test_a1_finite_nested_locator_projects_as_captured_unverified",
           _R + "FinitePositiveTests.test_a1_valid_replay_is_a_noop",
           _R + "DecoderBoundaryTests.test_finite_values_zero_underflow_and_integers_unchanged"],
    "A2": [_R + "OverflowRefusalTests.test_a2_overflow_locators_block_without_units",
           _R + "OverflowRefusalTests.test_a2_root_level_locator_number_remains_invalid",
           _R + "OverflowRefusalTests.test_a2_outer_payload_overflow_is_invalid_captured_json",
           _R + "OverflowRefusalTests.test_a2_existing_token_and_duplicate_refusals_unchanged",
           _R + "DecoderBoundaryTests.test_overflow_raises_value_error_at_every_depth",
           _R + "DecoderBoundaryTests.test_existing_refusals_unchanged"],
    "A3": [_R + "MixedBatchRecoveryTests.test_a3_fresh_single_run",
           _R + "MixedBatchRecoveryTests.test_a3_restart_with_unchanged_limits",
           _R + "MixedBatchRecoveryTests.test_a3_interrupted_batch_rolls_back_then_resumes",
           _R + "MixedBatchRecoveryTests.test_a3_mixed_outer_and_locator_overflow"],
}
# A5 (actual integration) is every store-backed test above: they all use the
# tests.records.test_ledger import factory and the full project_units API.
ACCEPTANCE["A5"] = [name for key in ("A1", "A2", "A3") for name in ACCEPTANCE[key]
                    if ".DecoderBoundaryTests." not in name]
# A4 deciding control: tests that must fail once only the finite hook is bypassed ...
DECIDING = [_R + "DecoderBoundaryTests.test_overflow_raises_value_error_at_every_depth",
            _R + "OverflowRefusalTests.test_a2_overflow_locators_block_without_units",
            _R + "OverflowRefusalTests.test_a2_outer_payload_overflow_is_invalid_captured_json",
            _R + "MixedBatchRecoveryTests.test_a3_fresh_single_run",
            _R + "MixedBatchRecoveryTests.test_a3_mixed_outer_and_locator_overflow"]
# ... and controls that must still pass with the hook bypassed.
CONTROLS = [_R + "DecoderBoundaryTests.test_finite_values_zero_underflow_and_integers_unchanged",
            _R + "DecoderBoundaryTests.test_existing_refusals_unchanged",
            _R + "FinitePositiveTests.test_a1_finite_nested_locator_projects_as_captured_unverified",
            _R + "FinitePositiveTests.test_a1_valid_replay_is_a_noop",
            _R + "OverflowRefusalTests.test_a2_existing_token_and_duplicate_refusals_unchanged",
            _R + "OverflowRefusalTests.test_a2_root_level_locator_number_remains_invalid"]
FULL_CI = {
    "sd15_discovery": ["-m", "unittest", "discover", "-s", "tests", "-t", "."],
    "records_yml_discovery": ["-B", "-W", "error::ResourceWarning", "-m", "unittest", "discover", "-v"],
    "records_yml_s3a": ["-B", "-W", "error::ResourceWarning", "-m", "unittest",
                        "tests.records.test_mail_resolution_census",
                        "tests.records.test_mail_adoption",
                        "tests.records.test_original_reconciliation"],
}
FULL_CI_TIMEOUT = 3600
GUARD_HITS = []
WRITE_HITS = []
PROBE_HITS = []


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def git(*args):
    done = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True, check=False)
    return done.stdout.strip() if done.returncode == 0 else "unavailable:" + str(done.returncode)


def tree_digest(root):
    digest = hashlib.sha256()
    for directory, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in (".git", "__pycache__"))
        for name in sorted(files):
            path = Path(directory) / name
            if path.is_symlink():
                data = b"symlink:" + os.fsencode(os.readlink(path))
            else:
                data = path.read_bytes()
            digest.update(str(path.relative_to(root)).encode() + b"\0" + hashlib.sha256(data).digest())
    return digest.hexdigest()


def identity():
    hashes = {}
    for relative in HASHED:
        path = REPO / relative
        hashes[relative] = sha256_bytes(path.read_bytes()) if path.is_file() else None
    return {"head": git("rev-parse", "HEAD"), "status_porcelain": git("status", "--porcelain"),
            "source_sha256": hashes, "tree_sha256": tree_digest(REPO)}


def fresh_run_dir(parent, case):
    """Validate the evidence parent (absolute, outside repo, no linked ancestor) then mkdir."""
    parent = Path(parent)
    if not parent.is_absolute():
        raise SystemExit("output_parent_must_be_absolute")
    normal = Path(os.path.normpath(parent))
    for ancestor in (normal, *normal.parents):
        if ancestor.is_symlink():
            raise SystemExit("output_parent_has_symlinked_ancestor")
    if normal == REPO or REPO in normal.parents:
        raise SystemExit("output_parent_inside_repository")
    if normal.exists() and not normal.is_dir():
        raise SystemExit("output_parent_not_directory")
    normal.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run = normal / (case + "-" + stamp + "-" + str(os.getpid()) + "-" + secrets.token_hex(4))
    run.mkdir(mode=0o700, exist_ok=False)
    return run


def install_offline_guards():
    def refuse(name):
        def guard(*args, **kwargs):
            GUARD_HITS.append(name)
            raise OSError("offline acceptance guard refused " + name)
        return guard
    socket.socket.connect = refuse("socket.connect")
    socket.socket.connect_ex = refuse("socket.connect_ex")
    socket.socket.sendto = refuse("socket.sendto")
    socket.create_connection = refuse("socket.create_connection")
    socket.getaddrinfo = refuse("socket.getaddrinfo")


WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
WRITE_EVENTS = {"os.mkdir": ((0, 2),), "os.rename": ((0, 2), (1, 3)), "os.replace": ((0, 2), (1, 3)),
                "os.remove": ((0, 1),), "os.rmdir": ((0, 1),), "os.truncate": ((0, None),),
                "os.symlink": ((1, 2),), "os.link": ((1, 3),), "os.chmod": ((0, 2),),
                "shutil.rmtree": ((0, 1),), "shutil.copyfile": ((1, None),),
                "sqlite3.connect": ((0, None),)}


def install_write_guard(run):
    """Refuse, before the syscall, Python-level writes and sqlite opens outside the run root."""
    root = os.path.realpath(run)

    def inside(target, dir_fd=None):
        if isinstance(target, int):
            return True
        text = os.fsdecode(target)
        if text in ("", ":memory:", os.devnull):
            return True
        if text.startswith("file:"):
            text = unquote(urlsplit(text).path)
        if isinstance(dir_fd, int) and dir_fd >= 0 and not os.path.isabs(text):
            try:
                text = os.path.join(os.readlink("/proc/self/fd/" + str(dir_fd)), text)
            except OSError:
                return False
        resolved = os.path.realpath(text)
        return resolved == root or resolved.startswith(root + os.sep)

    def hook(event, args):
        if event == "open":
            path, mode, flags = (tuple(args) + (None, None, None))[:3]
            writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                isinstance(flags, int) and flags & WRITE_FLAGS)
            targets = [(path, None)] if writing and path is not None else []
        elif event in WRITE_EVENTS:
            targets = [(args[p], args[d] if d is not None and d < len(args) else None)
                       for p, d in WRITE_EVENTS[event] if p < len(args) and args[p] is not None]
        else:
            return
        for target, dir_fd in targets:
            if not inside(target, dir_fd):
                text = os.fsdecode(target) if not isinstance(target, int) else str(target)
                probe = text.startswith(os.path.join(os.sep, "fa45-escape-"))
                (PROBE_HITS if probe else WRITE_HITS).append(event + ":" + text)
                raise PermissionError("acceptance write guard refused " + event + " outside run root")
    sys.addaudithook(hook)
    escape = os.path.join(os.sep, "fa45-escape-" + secrets.token_hex(6), "probe")
    try:
        open(escape, "w").close()
        refused = False
    except PermissionError:
        refused = True
    except OSError:
        refused = False
    positive = run / "write-guard-positive.txt"
    positive.write_text("in-root write allowed\n")
    probe_hits = [hit for hit in PROBE_HITS if escape in hit]
    return {"root": root, "escape_probe_refused": refused and len(probe_hits) == 1 and not WRITE_HITS,
            "in_root_positive_written": positive.is_file()}


def prepare_import_root(run, source_root):
    """Isolation setup that must precede every application import."""
    tmp = run / "tmp"
    tmp.mkdir(mode=0o700)
    tempfile.tempdir = str(tmp)
    os.environ["TMPDIR"] = str(tmp)
    sys.dont_write_bytecode = True
    install_offline_guards()
    loaded = sorted(name for name in sys.modules if name == "campaign_tool" or name.startswith(
        ("campaign_tool.", "tests.")) or name == "tests")
    if loaded:
        raise RuntimeError("application modules imported before isolation: " + ",".join(loaded))
    script_dir = str(Path(__file__).resolve().parent)
    sys.path[:] = [p for p in sys.path if p not in ("", script_dir, str(REPO))]
    sys.path.insert(0, str(source_root))
    return tmp


class Recorder:
    """unittest result wrapper that keeps the outcome of every test id."""

    @staticmethod
    def run(names, stream):
        import unittest

        class Result(unittest.TextTestResult):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.outcomes = {}

            def addSuccess(self, test):
                super().addSuccess(test)
                self.outcomes.setdefault(test.id(), "pass")

            def addFailure(self, test, err):
                super().addFailure(test, err)
                self.outcomes[test.id()] = "fail"

            def addError(self, test, err):
                super().addError(test, err)
                self.outcomes[test.id()] = "error"

            def addSubTest(self, test, subtest, err):
                super().addSubTest(test, subtest, err)
                if err is not None:
                    failed = issubclass(err[0], test.failureException)
                    self.outcomes[test.id()] = "fail" if failed else "error"

            def addSkip(self, test, reason):
                super().addSkip(test, reason)
                self.outcomes[test.id()] = "skip"

        suite = unittest.defaultTestLoader.loadTestsFromNames(list(names))
        runner = unittest.TextTestRunner(stream=stream, verbosity=2, resultclass=Result)
        return runner.run(suite)


def counts(result):
    return {"tests_run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
            "skipped": len(result.skipped), "expected_failures": len(result.expectedFailures),
            "unexpected_successes": len(result.unexpectedSuccesses)}


def clean(c):
    return (c["tests_run"] > 0 and c["failures"] == 0 and c["errors"] == 0 and c["skipped"] == 0
            and c["expected_failures"] == 0 and c["unexpected_successes"] == 0)


def write_raw(run, name, stream):
    raw = stream.getvalue().encode()
    (run / name).write_bytes(raw)
    return {"raw_output": name, "raw_output_sha256": sha256_bytes(raw)}


def module_paths(prefixes):
    return {name: getattr(module, "__file__", None) for name, module in sorted(sys.modules.items())
            if module is not None and name.startswith(prefixes)}


def case_tests(run, names, mapping=None):
    prepare_import_root(run, REPO)
    stream = io.StringIO()
    result = Recorder.run(names, stream)
    c = counts(result)
    details = {"modules": list(names), "counts": c, "guard_hits": list(GUARD_HITS),
               **write_raw(run, "unittest.txt", stream)}
    ok = clean(c) and not GUARD_HITS
    if mapping is not None:
        mapped = {}
        for key, tests in mapping.items():
            mapped[key] = {test: result.outcomes.get(test, "not_run") for test in tests}
            ok = ok and bool(tests) and all(value == "pass" for value in mapped[key].values())
        details["acceptance_mapping"] = mapped
        details["unmapped_tests"] = sorted(set(result.outcomes) - {
            test for tests in mapping.values() for test in tests})
    details["loaded_modules"] = module_paths(("campaign_tool.records.ledger", "tests.records"))
    return ok, details


def case_negative_control(run):
    prepare_import_root(run, REPO)
    from unittest import mock
    from campaign_tool.records.ledger import unit_projection as projection
    stream = io.StringIO()
    hook = getattr(projection, "_finite_float", None)
    if hook is None:
        return False, {"stop": "finite_float_hook_missing"}
    stream.write("=== broken control: unit_projection._finite_float -> float (in memory) ===\n")
    with mock.patch.object(projection, "_finite_float", float):
        bypassed = projection._json('{"page":1e309}')["page"]
        broken = Recorder.run(DECIDING, stream)
        stream.write("=== broken control: finite and explicit-token controls ===\n")
        broken_controls = Recorder.run(CONTROLS, stream)
    restored_hook = projection._finite_float is hook
    stream.write("=== restored hook: deciding regression and controls ===\n")
    restored = Recorder.run(DECIDING + CONTROLS, stream)
    details = write_raw(run, "unittest.txt", stream)
    deciding = {test: broken.outcomes.get(test, "not_run") for test in DECIDING}
    detected = all(value == "fail" for value in deciding.values())
    ok = (bypassed == float("inf") and detected and clean(counts(broken_controls))
          and restored_hook and clean(counts(restored))
          and all(restored.outcomes.get(test) == "pass" for test in DECIDING + CONTROLS)
          and not GUARD_HITS)
    details.update({
        "control": "unit_projection._finite_float patched to builtin float in memory only",
        "bypassed_decode_of_1e309": repr(bypassed),
        "broken_deciding_outcomes": deciding, "broken_deciding_counts": counts(broken),
        "broken_controls_counts": counts(broken_controls),
        "broken_controls_outcomes": broken_controls.outcomes,
        "restored_hook_identity": restored_hook, "restored_counts": counts(restored),
        "restored_outcomes": restored.outcomes, "guard_hits": list(GUARD_HITS),
        "product_sha256_in_memory_module": sha256_bytes(Path(projection.__file__).read_bytes()),
    })
    return ok, details


def path_constants(modules):
    """Module-level str/Path constants that look like absolute filesystem paths."""
    found = {}
    for module in modules:
        for name, value in vars(module).items():
            if name.startswith("__"):
                continue
            if isinstance(value, Path) or (isinstance(value, str) and value.startswith(os.sep)):
                found[module.__name__ + "." + name] = str(value)
    return found


def bound_defaults(modules):
    import inspect
    found = {}
    for module in modules:
        for name, value in vars(module).items():
            if inspect.isfunction(value) and value.__module__ == module.__name__:
                for parameter in inspect.signature(value).parameters.values():
                    default = parameter.default
                    if isinstance(default, Path) or (isinstance(default, str) and os.sep in default):
                        found[module.__name__ + "." + name + "." + parameter.name] = str(default)
    return found


def case_isolation(run):
    copy = run / "source-copy"
    shutil.copytree(REPO, copy, ignore=shutil.ignore_patterns(".git", "__pycache__"), symlinks=True)
    copy_digest = tree_digest(copy)
    tmp = prepare_import_root(run, copy)
    import importlib
    stream = io.StringIO()
    result = Recorder.run([REGRESSION, *COMPATIBILITY], stream)
    c = counts(result)
    details = write_raw(run, "unittest.txt", stream)
    names = ("campaign_tool.records.ledger.store", "campaign_tool.records.ledger.unit_projection",
             "campaign_tool.records.ledger.migrations.unit_projection_v001",
             "campaign_tool.records.ledger.migrations.unit_projection_guards_v001",
             "campaign_tool.records.ledger.migrations.v001")
    modules = [importlib.import_module(name) for name in names]
    loaded = module_paths(("campaign_tool", "tests"))
    copy_root = os.path.realpath(copy) + os.sep
    outside = {name: path for name, path in loaded.items()
               if path is not None and not os.path.realpath(path).startswith(copy_root)}
    constants = path_constants(modules)
    defaults = bound_defaults(modules)
    run_root = os.path.realpath(run) + os.sep
    unsafe_constants = {k: v for k, v in {**constants, **defaults}.items()
                        if not os.path.realpath(v).startswith(run_root)}
    leftovers = sorted(p.name for p in tmp.iterdir())
    portable = {}
    for relative in ("tests/records/test_fa45_unit_finite.py", "tools/accept_fa45_unit_finite.py"):
        text = (REPO / relative).read_text()
        portable[relative] = sorted(set(re.findall(r"/(?:home|Users|tmp|var|opt)/[^\s\"']*", text)))
    hostile = {k: v for k, v in portable.items() if any("fa45-escape" not in hit for hit in v)}
    ok = (clean(c) and not outside and not unsafe_constants and not GUARD_HITS and not hostile
          and tree_digest(copy) == copy_digest and len(loaded) > 0)
    details.update({
        "source_copy": str(copy), "source_copy_tree_sha256": copy_digest,
        "source_copy_matches_repo_tree": copy_digest == tree_digest(REPO),
        "counts": c, "loaded_module_paths": loaded, "modules_outside_copy": outside,
        "path_constants": constants, "bound_path_defaults": defaults,
        "unsafe_constants_or_defaults": unsafe_constants,
        "tempfile_tempdir": tempfile.gettempdir(), "tmp_leftovers": leftovers,
        "sentinels": {"socket": "connect/connect_ex/sendto/create_connection/getaddrinfo refused",
                      "write_guard": "audit hook, run root only", "bytecode": sys.dont_write_bytecode},
        "absolute_paths_in_committed_files": portable, "guard_hits": list(GUARD_HITS),
    })
    return ok, details


def parse_unittest(text):
    ran = re.findall(r"^Ran (\d+) tests? in ", text, re.M)
    status = re.findall(r"^(OK|FAILED)(?: \(([^)]*)\))?\s*$", text, re.M)
    if len(ran) != 1 or len(status) != 1:
        return None
    extras = {}
    for item in filter(None, (status[0][1] or "").split(", ")):
        key, _, value = item.partition("=")
        extras[key.strip()] = int(value)
    return {"tests_run": int(ran[0]), "status": status[0][0], "failures": extras.get("failures", 0),
            "errors": extras.get("errors", 0), "skipped": extras.get("skipped", 0),
            "expected_failures": extras.get("expected failures", 0),
            "unexpected_successes": extras.get("unexpected successes", 0)}


def case_full_ci(run):
    tmp = run / "tmp"
    tmp.mkdir(mode=0o700)
    env = dict(os.environ, TMPDIR=str(tmp))
    rows, ok = {}, True
    for name, args in FULL_CI.items():
        argv = [sys.executable, *args]
        started = datetime.datetime.now(datetime.timezone.utc).isoformat()
        try:
            done = subprocess.run(argv, cwd=REPO, env=env, capture_output=True, check=False,
                                  timeout=FULL_CI_TIMEOUT, stdin=subprocess.DEVNULL)
            exit_code, out, err = done.returncode, done.stdout, done.stderr
        except subprocess.TimeoutExpired as exc:
            exit_code, out, err = "timeout", exc.stdout or b"", exc.stderr or b""
        (run / (name + ".stdout.txt")).write_bytes(out)
        (run / (name + ".stderr.txt")).write_bytes(err)
        parsed = parse_unittest(err.decode(errors="replace"))
        passed = (exit_code == 0 and parsed is not None and parsed["status"] == "OK"
                  and clean(parsed))
        ok = ok and passed
        rows[name] = {"argv": argv, "cwd": str(REPO), "tmpdir": str(tmp), "started": started,
                      "exit": exit_code, "counts": parsed, "passed": passed,
                      "stdout_sha256": sha256_bytes(out), "stderr_sha256": sha256_bytes(err)}
    return ok, {"runs": rows}


def case_credentials(run):
    tool = Path(sys.executable).parent / "detect-secrets"
    if not (tool.is_file() and os.access(tool, os.X_OK)):
        return False, {"stop": "missing_dependency: detect-secrets not adjacent to interpreter"}
    scan_argv = [str(tool), "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"]
    report = run / "secret-scan.json"
    with open(report, "wb") as out:
        scan = subprocess.run(scan_argv, cwd=REPO, stdout=out, stderr=subprocess.PIPE, check=False,
                              stdin=subprocess.DEVNULL)
    (run / "scan-stderr.txt").write_bytes(scan.stderr)
    if scan.returncode != 0:
        return False, {"scan_argv": scan_argv, "scan_exit": scan.returncode, "stop": "scan_failed"}
    try:
        data = json.loads(report.read_bytes())
        results = data["results"]
        if not isinstance(results, dict):
            raise ValueError("results is not an object")
    except (ValueError, KeyError) as exc:
        return False, {"scan_argv": scan_argv, "stop": "malformed_report: " + type(exc).__name__}
    check_argv = [sys.executable, str(REPO / "tools" / "check_secret_report.py"), str(report)]
    check = subprocess.run(check_argv, cwd=REPO, capture_output=True, check=False,
                           stdin=subprocess.DEVNULL)
    raw = check.stdout + check.stderr
    (run / "check-secret-report.txt").write_bytes(raw)
    return check.returncode == 0, {
        "scan_argv": scan_argv, "scan_cwd": str(REPO), "scan_exit": scan.returncode,
        "scan_json": "secret-scan.json", "scan_json_sha256": sha256_bytes(report.read_bytes()),
        "detect_secrets_version": data.get("version"), "files_with_hits": sorted(results),
        "check_argv": check_argv, "check_exit": check.returncode,
        "check_summary": raw.decode(errors="replace").strip().splitlines()[-1:] if raw else [],
        "check_output_sha256": sha256_bytes(raw)}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Scoped offline acceptance for FA45.")
    parser.add_argument("--case", required=True, choices=CASES)
    parser.add_argument("--output-parent", required=True)
    args = parser.parse_args(argv)
    run = fresh_run_dir(args.output_parent, args.case)
    before = identity()
    guard = {"mode": "subprocess case; parent guard not installed"}
    try:
        if args.case in ("behavior", "compatibility", "negative-control", "isolation"):
            guard = install_write_guard(run)
            if not (guard["escape_probe_refused"] and guard["in_root_positive_written"]):
                raise RuntimeError("write_guard_self_check_failed")
        if args.case == "behavior":
            ok, details = case_tests(run, [REGRESSION], ACCEPTANCE)
        elif args.case == "compatibility":
            ok, details = case_tests(run, COMPATIBILITY)
        elif args.case == "negative-control":
            ok, details = case_negative_control(run)
        elif args.case == "isolation":
            ok, details = case_isolation(run)
        elif args.case == "full-ci":
            ok, details = case_full_ci(run)
        elif args.case == "credentials":
            ok, details = case_credentials(run)
        else:
            ok, details = False, {"stop": "unknown_case"}
    except Exception as exc:  # any unexpected failure fails closed
        ok, details = False, {"stop": "unknown_failure: " + type(exc).__name__ + ": " + str(exc)}
    after = identity()
    if after != before:
        ok = False
        details["stop"] = "candidate_identity_changed_during_run"
    if WRITE_HITS:
        ok = False
        details["stop"] = "write_outside_run_root_refused"
    guard["refused_writes"] = list(WRITE_HITS)
    guard["refused_inert_probes"] = len(PROBE_HITS)
    record = {"case": args.case, "argv": [sys.executable, *sys.argv], "cwd": os.getcwd(),
              "interpreter": sys.executable, "python": sys.version.split()[0],
              "run_dir": str(run), "identity": before, "write_guard": guard,
              "result": "PASS_REVIEW_REQUIRED" if ok else "FAIL", "exit": 0 if ok else 1, **details}
    (run / "result.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    print(json.dumps({key: record[key] for key in ("case", "result", "exit", "run_dir")}
                     | {"summary": {k: v for k, v in details.items() if k in (
                         "counts", "stop", "acceptance_mapping", "broken_deciding_outcomes",
                         "restored_counts", "runs", "check_summary", "modules_outside_copy",
                         "unsafe_constants_or_defaults", "files_with_hits")}}, indent=2, sort_keys=True))
    return record["exit"]


if __name__ == "__main__":
    raise SystemExit(main())
