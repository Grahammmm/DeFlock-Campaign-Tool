"""Scoped FA1403 acceptance launcher: ambiguous serialized occurrence evidence.

Usage: accept_fa1403.py --case CASE --output-root DIR

CASE is behavior, compatibility, negative-control, isolation, credential-scan
or public-tree. Every invocation creates one fresh, uniquely named run
directory below --output-root (which must lie inside this candidate's root and
outside the repository), retains argv/cwd/exit/stdout/stderr of every child
plus test/skip counts in result.json, and exits 0 only when every check of its
case passed. A pass is REVIEW REQUIRED scoped evidence: not full-repository
CI, semantic acceptance or permission. Full discovery = operator-run.

Unittest cases run in a file-launched child interpreter (this file with
--child) that disables bytecode, redirects temporary paths into the run
directory and installs network/process/live-file/write sentinels BEFORE any
repository import. Any actual sentinel refusal fails the case; the isolation
case checks the guard policy only inertly, never by attempting a forbidden
operation. The parent never imports repository code.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import secrets
import shutil
import stat
import subprocess
import sys

sys.dont_write_bytecode = True

REPO = Path(__file__).resolve().parents[1]
CANDIDATE = REPO.parent
MANIFEST = CANDIDATE / "SOURCE-MANIFEST.json"
PRODUCT = "campaign_tool/records/mail_adoption.py"
TEST_MODULE = "tests.records.test_fa1403"
AUTHORED = (PRODUCT, "tests/records/test_fa1403.py", "tools/accept_fa1403.py")
COMPATIBILITY = (
    "tests.records.test_mail_adoption_bindings.CanonicalBindingDriftTests"
    ".test_unchanged_bindings_stay_complete_and_byte_identical",
    "tests.records.test_mail_adoption_bindings.VerifyOriginalBytesUnitTests"
    ".test_rejects_untrusted_digest_and_size_without_opening",
)
REFERENCED = ("tests/records/test_mail_adoption_bindings.py", "tests/records/test_mail_adoption_recovery.py",
              "tests/records/test_mail_adoption_regression.py", "tests/records/test_mail_wire_intake.py",
              "tests/records/native_msg_fixtures.py", "campaign_tool/records/runner/canonical_mail.py",
              "tools/check_secret_report.py", "tools/check_public_tree.py")
BEHAVIOR_TESTS = 18
# Negative control: with only the duplicate-member predicate disabled, exactly
# these methods must fail (by assertion, never error); every other passes.
CONTROL_FAILURES = frozenset({
    "test_duplicate_members_rejected_at_every_depth",
    "test_duplicate_cas_sha256_is_unreadable",
    "test_duplicate_receipt_sha256_is_unreadable",
    "test_duplicate_bytes_is_unreadable",
    "test_duplicate_verification_receipt_path_is_unreadable",
    "test_duplicate_export_receipt_path_is_unreadable",
    "test_duplicate_member_on_mail_occurrence_is_unreadable",
    "test_escaped_duplicate_member_name_is_unreadable",
    "test_repeated_classification_keeps_clean_peer_complete_and_inputs_unchanged",
    "test_duplicate_native_item_member_is_unreadable",
    "test_duplicate_member_inside_native_item_is_unreadable",
})
# The deciding observation: wrong-then-correct CAS duplicates are accepted (no conflict).
CONTROL_ACCEPTS = ("test_duplicate_cas_sha256_is_unreadable", "order='wrong_then_correct'",
                   "AssertionError: {} != {'identity_evidence_unreadable': 1}")
APP_MODULES = ("campaign_tool.records.mail_adoption", "campaign_tool.records.runner.canonical_mail",
               "campaign_tool.records.intake.folder", "campaign_tool.records.intake.mail_delta")
CASES = ("behavior", "compatibility", "negative-control", "isolation", "credential-scan", "public-tree")
SCRUB_PREFIXES = ("PYTHON", "LD_")
SCRUB_MARKERS = ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "OLLAMA", "OPENAI", "ANTHROPIC", "PROXY")


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def identity():
    def git(*args):
        done = subprocess.run(["git", *args], cwd=REPO, capture_output=True, check=False)
        return done.stdout.decode(errors="replace")
    status = git("status", "--porcelain=v1", "--untracked-files=all")
    return {"head": git("rev-parse", "HEAD").strip(), "status_porcelain": status, "clean_tree": status == "",
            "file_sha256": {name: sha256_file(REPO / name) if (REPO / name).is_file() else None
                            for name in AUTHORED + REFERENCED}}


def checked_output_root(raw):
    """Absolute, inside the candidate root, outside the repo, no linked/non-directory ancestor."""
    if not raw or not os.path.isabs(raw):
        return None, "output_root_not_absolute"
    path = Path(os.path.abspath(raw))
    if path == CANDIDATE or not path.is_relative_to(CANDIDATE):
        return None, "output_root_outside_candidate"
    if path == REPO or path.is_relative_to(REPO) or REPO.is_relative_to(path):
        return None, "output_root_overlaps_repo"
    probe = Path(path.anchor)
    for part in path.parts[1:]:
        probe = probe / part
        try:
            info = probe.lstat()
        except FileNotFoundError:
            break
        if stat.S_ISLNK(info.st_mode):
            return None, "output_root_linked"
        if not stat.S_ISDIR(info.st_mode):
            return None, "output_root_not_directory"
    return path, None


class Run:
    def __init__(self, root, case):
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        self.dir = root / ("%s-%s-%d-%s" % (case, stamp, os.getpid(), secrets.token_hex(4)))
        self.dir.mkdir(mode=0o700)   # never reuses an earlier run's output
        self.children = []

    def env(self):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(SCRUB_PREFIXES) and not any(m in key.upper() for m in SCRUB_MARKERS)}
        tmp = self.dir / "tmp"
        tmp.mkdir(mode=0o700, exist_ok=True)
        env.update(TMPDIR=str(tmp), TMP=str(tmp), TEMP=str(tmp), PYTHONDONTWRITEBYTECODE="1",
                   NO_PROXY="*", no_proxy="*")
        return env

    def child(self, label, argv, cwd=REPO, stdout_name=None):
        argv = [str(arg) for arg in argv]
        done = subprocess.run(argv, cwd=cwd, env=self.env(), capture_output=True, check=False)
        out = self.dir / (stdout_name or label + ".stdout")
        err = self.dir / (label + ".stderr")
        out.write_bytes(done.stdout)
        err.write_bytes(done.stderr)
        self.children.append({"label": label, "argv": argv, "cwd": str(cwd), "exit": done.returncode,
                              "stdout": str(out), "stdout_sha256": sha256_file(out),
                              "stderr": str(err), "stderr_sha256": sha256_file(err)})
        return done

    def test_child(self, label, mode, extra=(), source=REPO):
        result = self.dir / (label + "-result.json")
        done = self.child(label, [sys.executable, source / "tools" / "accept_fa1403.py", "--child", mode,
                                  "--run-dir", self.dir, "--source-root", source, "--result-json", result,
                                  *extra], cwd=source)
        try:
            record = json.loads(result.read_text())
            record["result_json"], record["result_sha256"] = str(result), sha256_file(result)
        except (OSError, ValueError):
            record = {"pass": False, "error": "child_result_missing_or_invalid"}
        record["child_exit"] = done.returncode
        record["pass"] = record.get("pass") is True and done.returncode == 0
        return record


def credential_scan(run):
    try:
        runtime = json.loads(MANIFEST.read_text())["runtime"]
        python, scanner = Path(runtime["credential_scan_python3"]), Path(runtime["detect_secrets"])
    except (OSError, ValueError, KeyError, TypeError):
        return {"pass": False, "error": "source_manifest_runtime_unavailable"}
    if not python.is_file() or not scanner.is_file():
        return {"pass": False, "error": "credential_scan_runtime_missing"}
    report = run.dir / "secret-scan.json"
    scan = run.child("detect-secrets", [python, scanner, "scan", "--all-files", "--exclude-files", "^.git/",
                                        "--no-verify"], stdout_name="secret-scan.json")
    try:
        results = json.loads(report.read_bytes())["results"]
        raw_hits = sum(len(hits) for hits in results.values())
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raw_hits = None
    check = run.child("check-secret-report", [python, REPO / "tools" / "check_secret_report.py", report])
    summary = [line for line in check.stdout.decode(errors="replace").splitlines() if "potential secrets:" in line]
    zero = len(summary) == 1 and "potential secrets: 0;" in summary[0]
    return {"pass": scan.returncode == 0 and raw_hits is not None and check.returncode == 0 and zero,
            "interpreter": str(python), "scanner": str(scanner), "report": str(report),
            "report_sha256": sha256_file(report), "raw_report_hits": raw_hits, "checker_summary": summary,
            "potential_secrets_zero": zero}


def public_tree(run):
    python = REPO / ".venv" / "bin" / "python3"
    done = run.child("check-public-tree", [python, REPO / "tools" / "check_public_tree.py"])
    text = (done.stdout + done.stderr).decode(errors="replace")
    try:
        report = json.loads(done.stdout)
        no_violations = report["findings"] == [] and report["non_content_matches"] == []
        scanned = report.get("scanned_files")
    except (ValueError, KeyError, TypeError):
        no_violations, scanned = False, None
    return {"pass": done.returncode == 0 and no_violations, "interpreter": str(python), "exit": done.returncode,
            "no_violations": no_violations, "scanned_files": scanned, "output_tail": text.splitlines()[-5:]}


def parent_main(args):
    started = datetime.now(timezone.utc).isoformat()
    root, problem = checked_output_root(args.output_root)
    if problem:
        print(json.dumps({"case": args.case, "pass": False, "exit": 2, "error": problem}))
        return 2
    run = Run(root, args.case)
    before = identity()
    if args.case == "behavior":
        verdict = run.test_child("behavior", "unittest", ("--select", TEST_MODULE,
                                                          "--expect-tests", BEHAVIOR_TESTS))
    elif args.case == "compatibility":
        extra = []
        for name in COMPATIBILITY:
            extra += ["--select", name]
        verdict = run.test_child("compatibility", "unittest", (*extra, "--expect-tests", len(COMPATIBILITY)))
    elif args.case == "negative-control":
        verdict = run.test_child("negative-control", "negative-control")
    elif args.case == "isolation":
        copy = run.dir / "source-copy"
        shutil.copytree(REPO, copy, symlinks=True, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
        mismatched = [name for name in AUTHORED + REFERENCED if sha256_file(REPO / name) != sha256_file(copy / name)]
        if mismatched:
            verdict = {"pass": False, "error": "source_copy_mismatch", "files": mismatched}
        else:
            verdict = run.test_child("isolation", "isolation", ("--select", TEST_MODULE, "--expect-tests",
                                                                BEHAVIOR_TESTS, "--primary-repo", REPO), source=copy)
            verdict["source_copy"] = str(copy)
    elif args.case == "credential-scan":
        verdict = credential_scan(run)
    else:
        verdict = public_tree(run)
    after = identity()
    stable = before == after
    passed = (verdict.get("pass") is True and stable and bool(run.children)
              and all(isinstance(c["exit"], int) for c in run.children))
    record = {"case": args.case, "argv": [sys.executable, *sys.argv], "cwd": os.getcwd(), "repo": str(REPO),
              "interpreter": sys.executable, "python_version": platform.python_version(), "run_dir": str(run.dir),
              "started_utc": started, "ended_utc": datetime.now(timezone.utc).isoformat(),
              "identity_before": before, "identity_after": after, "identity_stable": stable,
              "children": run.children, "verdict": verdict, "pass": passed, "exit": 0 if passed else 1,
              "review_required": True,
              "scope": "scoped FA1403 acceptance; not full-repository CI or semantic acceptance; "
                       "full discovery = operator-run"}
    (run.dir / "result.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"case": args.case, "pass": passed, "exit": record["exit"], "run_dir": str(run.dir),
                      "result_sha256": sha256_file(run.dir / "result.json"), "head": after["head"],
                      "clean_tree": after["clean_tree"], "tests_run": verdict.get("tests_run"),
                      "failures": verdict.get("failures"), "errors": verdict.get("errors"),
                      "skipped": verdict.get("skipped")}, sort_keys=True))
    return record["exit"]


# ---------------------------------------------------------------- child side


def install_guards(run_dir, source_root, state):
    """Refuse network, process launches, writes outside the run and live home-file reads."""
    run_root = Path(run_dir).resolve()
    home = Path(os.path.expanduser("~")).resolve()
    readable = [run_root, Path(source_root).resolve(), REPO]
    readable += [Path(p).resolve() for p in {sys.prefix, sys.base_prefix, sys.exec_prefix}]
    readable += [Path(p).resolve() for p in sys.path if p and os.path.isabs(p)]
    write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC

    def resolve(path, dir_fd=None):
        if isinstance(path, int):
            return None
        try:
            path = os.fsdecode(os.fspath(path))
        except TypeError:
            return False
        if path in ("", ":memory:"):
            return None
        if path.startswith("file:"):
            path = path[5:].split("?", 1)[0]
        if not os.path.isabs(path) and isinstance(dir_fd, int) and not isinstance(dir_fd, bool):
            try:
                path = os.path.join(os.readlink("/proc/self/fd/%d" % dir_fd), path)
            except OSError:
                return False
        return Path(os.path.abspath(path)).resolve()

    def inside(path, dir_fd=None):
        target = resolve(path, dir_fd)
        return target is None or (target is not False and target.is_relative_to(run_root))

    def live(path):
        target = resolve(path)
        if target is None:
            return False
        if target is False:
            return True
        return target.is_relative_to(home) and not any(target.is_relative_to(r) for r in readable)

    mutations = {"os.mkdir": ((0, 2),), "os.remove": ((0, 1),), "os.rmdir": ((0, 1),), "os.chmod": ((0, 2),),
                 "os.chown": ((0, 3),), "os.utime": ((0, 3),), "os.truncate": ((0, None),),
                 "shutil.rmtree": ((0, 1),), "os.rename": ((0, 2), (1, 3)), "os.replace": ((0, 2), (1, 3)),
                 "os.link": ((0, 2), (1, 3)), "os.symlink": ((1, 2),), "shutil.copyfile": ((1, None),),
                 "shutil.move": ((0, None), (1, None))}

    def decide(event, args):
        """Pure policy: return (refused_event, detail) or None. Performs no guarded operation."""
        if event.startswith("socket.") and event not in ("socket.__new__", "socket.gethostname"):
            return event, args
        if event in ("subprocess.Popen", "os.system", "os.posix_spawn", "os.spawn", "os.exec", "os.fork",
                     "os.forkpty", "pty.spawn", "urllib.Request", "webbrowser.open"):
            return event, (args[:2] if isinstance(args, tuple) else args)
        if event == "ctypes.dlopen":
            return None if args and args[0] is None else (event, args)
        if event == "open":
            path, mode, flags = args
            writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                isinstance(flags, int) and flags & write_flags)
            if writing and not inside(path):
                return event, path
            if live(path):
                return "live-file-read", path
        elif event == "sqlite3.connect":
            if not inside(args[0]):
                return event, args[0]
        elif event in mutations:
            for index, fd_index in mutations[event]:
                if index < len(args):
                    dir_fd = args[fd_index] if fd_index is not None and fd_index < len(args) else None
                    if not inside(args[index], dir_fd):
                        return event, args[index]
        return None

    def hook(event, args):
        refused = decide(event, args)
        if refused is None:
            if event == "ctypes.dlopen":
                state["allowed"].append({"phase": state["phase"], "dlopen": "self-process"})
            return
        state["violations"].append({"phase": state["phase"], "event": refused[0],
                                    "detail": repr(refused[1])[:200]})
        raise PermissionError("fa1403 acceptance guard refused " + refused[0])

    sys.addaudithook(hook)
    return decide


def guard_policy(decide, run_dir):
    """Inert policy check: evaluate the installed guard's decision on synthetic event tuples.

    No socket, process, outside write or home-file read is attempted; any actual
    refusal anywhere in the child remains fatal.
    """
    run_dir = Path(run_dir).resolve()
    outside = str(run_dir.parent / "fa1403-policy-outside")
    live = str(Path(os.path.expanduser("~")) / ".fa1403-policy-live")
    inside = str(run_dir / "tmp" / "fa1403-policy-inside")
    refuse = {"network_connect": ("socket.connect", (None, ("192.0.2.1", 9))),
              "process_launch": ("subprocess.Popen", (["true"], None, None, None)),
              "write_outside_run": ("open", (outside, "wb", os.O_WRONLY | os.O_CREAT)),
              "live_file_read": ("open", (live, "rb", os.O_RDONLY)),
              "sqlite_outside_run": ("sqlite3.connect", (outside,))}
    allow = {"write_inside_run": ("open", (inside, "wb", os.O_WRONLY | os.O_CREAT)),
             "sqlite_inside_run": ("sqlite3.connect", (inside,)),
             "sqlite_memory": ("sqlite3.connect", (":memory:",))}
    result = {name: decide(*event) is not None for name, event in refuse.items()}
    result.update({name: decide(*event) is None for name, event in allow.items()})
    return result


def path_constants(names, run_dir):
    """Absolute-path data constants and bound defaults reachable in loaded app modules."""
    import inspect
    found = []

    def check(where, value):
        if isinstance(value, (str, os.PathLike)):
            text = os.fspath(value)
            if isinstance(text, str) and text.startswith(("/", "~")):
                found.append({"where": where, "inside_run": Path(text).resolve().is_relative_to(run_dir)})

    for name in names:
        for attr, value in vars(sys.modules[name]).items():
            if attr.startswith("__") and attr.endswith("__"):
                continue
            check(name + "." + attr, value)
            targets = [getattr(value, n) for n in vars(value)] if inspect.isclass(value) else [value]
            for target in targets:
                if inspect.isfunction(target) or inspect.ismethod(target):
                    for n, default in enumerate(target.__defaults__ or ()):
                        check("%s.%s default[%d]" % (name, target.__qualname__, n), default)
                    for key, default in (target.__kwdefaults__ or {}).items():
                        check("%s.%s default[%s]" % (name, target.__qualname__, key), default)
    return found


def run_suite(suite):
    import io
    import unittest
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    sys.stderr.write(stream.getvalue())
    def assertion_line(text):
        lines = [line for line in text.splitlines() if line.startswith("AssertionError:")]
        return lines[0][:300] if lines else ""

    failures = [(t.id(), assertion_line(text)) for t, text in result.failures]
    record = {"tests_run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
              "skipped": len(result.skipped), "expected_failures": len(result.expectedFailures),
              "unexpected_successes": len(result.unexpectedSuccesses),
              "failure_ids": [f[0] for f in failures], "failure_assertions": [f[1] for f in failures],
              "error_ids": [t.id() for t, _ in result.errors],
              "failed_methods": sorted({getattr(t, "test_case", t)._testMethodName for t, _ in result.failures}),
              "errored_methods": sorted({getattr(t, "test_case", t)._testMethodName for t, _ in result.errors})}
    record["clean"] = (result.wasSuccessful() and result.testsRun > 0 and not result.skipped
                       and not result.expectedFailures and not result.unexpectedSuccesses)
    return record


def _flatten(suite):
    for test in suite:
        if hasattr(test, "_testMethodName"):
            yield test
        else:
            yield from _flatten(test)


def global_state():
    """Process-global values a test module could poison for unrelated peers."""
    import json as json_module
    import tempfile
    from campaign_tool.records import mail_adoption
    return {"environ": sorted(os.environ.items()), "sys_path": list(sys.path), "tempdir": tempfile.tempdir,
            "cwd": os.getcwd(), "json_loads": id(json_module.loads),
            "mail_adoption": {n: id(v) for n, v in vars(mail_adoption).items() if callable(v)}}


def child_main(args):
    run_dir = Path(args.run_dir).resolve()
    source = Path(args.source_root).resolve()
    tmp = run_dir / "tmp"
    tmp.mkdir(mode=0o700, exist_ok=True)
    sys.dont_write_bytecode = True
    import tempfile
    tempfile.tempdir = str(tmp)
    state = {"phase": "tests", "violations": [], "allowed": []}
    decide = install_guards(run_dir, source, state)
    record = {"mode": args.child, "tempdir": tempfile.gettempdir(), "source_root": str(source),
              "dont_write_bytecode": sys.dont_write_bytecode}
    if args.child == "isolation":
        record["guard_policy"] = guard_policy(decide, run_dir)
    sys.path.insert(0, str(source))
    import unittest
    from unittest import mock
    checks = {}
    if args.child in ("unittest", "isolation"):
        peer = COMPATIBILITY[1]
        before = global_state() if args.child == "isolation" else None
        suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(name) for name in args.select)
        record.update(run_suite(suite), selected=args.select)
        checks["tests_clean"] = record["clean"]
        checks["expected_test_count"] = record["tests_run"] == args.expect_tests
        if args.child == "isolation":
            checks["no_global_state_poisoning"] = global_state() == before
            peer_run = run_suite(unittest.defaultTestLoader.loadTestsFromName(peer))
            record["unrelated_peer_after"] = dict(peer_run, selector=peer)
            checks["unrelated_peer_passes_after"] = peer_run["clean"] and peer_run["tests_run"] == 1
    else:
        from campaign_tool.records import mail_adoption
        from tests.records import test_fa1403
        load = lambda: unittest.defaultTestLoader.loadTestsFromModule(test_fa1403)
        methods = {test._testMethodName for test in _flatten(load())}
        with mock.patch.object(mail_adoption, "_reject_duplicate_members", dict):
            control = run_suite(load())
        restored = run_suite(load())
        record.update(control=control, restored=restored, tests_run=control["tests_run"] + restored["tests_run"],
                      failures=control["failures"] + restored["failures"],
                      errors=control["errors"] + restored["errors"],
                      skipped=control["skipped"] + restored["skipped"],
                      control_passing_methods=sorted(methods - set(control["failed_methods"])))
        method, order, message = CONTROL_ACCEPTS
        checks["control_fails_exactly_duplicate_assertions"] = (
            set(control["failed_methods"]) == CONTROL_FAILURES and control["errors"] == 0
            and control["skipped"] == 0)
        checks["control_accepts_conflicting_duplicates"] = any(
            method in tid and order in tid and line.startswith(message)
            for tid, line in zip(control["failure_ids"], control["failure_assertions"]))
        checks["control_unaffected_positives_pass"] = (
            len(methods) == BEHAVIOR_TESTS and CONTROL_FAILURES < methods
            and not set(control["errored_methods"]) and len(record["control_passing_methods"]) > 0
            and set(record["control_passing_methods"]) == methods - CONTROL_FAILURES)
        checks["restored_candidate_clean"] = restored["clean"] and restored["tests_run"] == BEHAVIOR_TESTS
        checks["predicate_restored"] = mail_adoption._reject_duplicate_members is not dict
    loaded = {name: sys.modules[name].__file__ for name in APP_MODULES + (TEST_MODULE,) if name in sys.modules}
    record["module_paths"] = loaded
    checks["modules_from_source_root"] = bool(loaded) and all(
        Path(path).resolve().is_relative_to(source) for path in loaded.values())
    found = path_constants([name for name in APP_MODULES if name in sys.modules], run_dir)
    record["path_constants_and_defaults"] = found
    checks["no_reachable_path_constants_outside_run"] = all(item["inside_run"] for item in found)
    if args.child == "isolation":
        policy = record["guard_policy"]
        checks["guard_policy_inert"] = len(policy) == 8 and all(policy.values())
        checks["no_bytecode_in_source_copy"] = not any(source.rglob("*.pyc"))
        primary = Path(args.primary_repo).resolve() if args.primary_repo else None
        checks["source_is_fresh_copy"] = primary is not None and source != primary and not source.is_relative_to(
            primary)
        checks["fixture_prefix_inside_copy_run"] = Path(record["tempdir"]).resolve().is_relative_to(run_dir)
    record["guard_violations"] = state["violations"]
    record["allowed_events"] = state["allowed"]
    checks["no_guard_violations"] = not state["violations"]   # every actual refusal is fatal
    record["checks"] = checks
    record["pass"] = all(value is True for value in checks.values())
    Path(args.result_json).write_text(json.dumps(record, indent=2, sort_keys=True, default=str) + "\n")
    return 0 if record["pass"] else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--case", choices=CASES)
    parser.add_argument("--output-root")
    parser.add_argument("--child", choices=("unittest", "negative-control", "isolation"))
    parser.add_argument("--run-dir")
    parser.add_argument("--source-root")
    parser.add_argument("--result-json")
    parser.add_argument("--select", action="append", default=[])
    parser.add_argument("--expect-tests", type=int)
    parser.add_argument("--primary-repo")
    args = parser.parse_args(argv)
    if args.child:
        return child_main(args)
    if not args.case or not args.output_root:
        parser.error("--case and --output-root are required")
    return parent_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
