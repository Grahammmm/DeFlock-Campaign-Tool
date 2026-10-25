"""Acceptance launcher for FA92 (offline, synthetic, REVIEW REQUIRED).

Usage (one case per invocation):
    <repo>/.venv/bin/python3 tools/accept_fa92.py --case CASE --output-root DIR

CASE is one of: behavior, compatibility, negative-control, credential-scan,
public-tree. Every invocation creates a new, uniquely named child directory
under DIR and never overwrites earlier author or reviewer evidence. That
directory holds the raw argv/cwd/stdout/stderr/exit of every child plus
``receipt.json``. The receipt records the interpreter, SOURCE-MANIFEST.json
hash, full git HEAD, dirty-diff hash, source/test/fixture hashes and the
executed/skipped test counts. Exit 0 only when every assertion of the case
holds. A pass is machine validity only: REVIEW REQUIRED, not semantic, legal
or production acceptance and not source/live permission.

Paths come from this file: the repository is the parent of tools/, and the
operator-staged SOURCE-MANIFEST.json sits beside the repository directory.
Only the named selectors are loaded; there is no blanket discovery.

Boundary. Before any application or test import, each process that imports
application code (this parent and each fresh file-launched unittest child)
does three things:
  * sets tempfile.tempdir inside its own run directory;
  * installs an offline guard: an audit hook that refuses network events and
    any process creation outside a per-profile list of literal argv shapes,
    plus sentinel replacements for the socket entry points;
  * runs a measured boundary scan of the loaded application modules after
    the tests. The scan looks for module globals and function defaults
    (positional and keyword) bound to absolute host paths outside the
    repository/run directory, or to original socket/process callables. Any
    finding fails the run.
Children also get HOME/TMPDIR/XDG_* pointed at a fresh directory inside the
run directory. Each child probes its own boundary with an inert synthetic
audit event and checks that the event is refused; no socket is ever opened.
These are tripwires, not a hostile-code sandbox. Grandchild processes (the
CLI invocations inside the unchanged compatibility selectors, and the
existing checker scripts) run under their existing admission.
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
import types
import unittest

sys.dont_write_bytecode = True

RUNNER = Path(__file__).resolve()
REPO = RUNNER.parents[1]
CANDIDATE = REPO.parent
MANIFEST = CANDIDATE / "SOURCE-MANIFEST.json"
CASES = ("behavior", "compatibility", "negative-control", "credential-scan", "public-tree")
CARD = "FA92-OBSERVATION-TAINT"
SCHEMA = "fa92-acceptance-receipt-v1"

MODULE_PATH = "tools/portal_reconcile.py"
TEST_PATH = "tests/records/test_fa92.py"
TEST_MODULE = "tests.records.test_fa92"
BEHAVIOR_SELECTOR = TEST_MODULE + ".Behavior"
CONTROL_SELECTOR = TEST_MODULE + ".GateControl"
COMPAT_SELECTORS = ("tests.test_portal_reconcile",)
HASHED = (MODULE_PATH, TEST_PATH, "tools/accept_fa92.py", "tests/test_portal_reconcile.py",
          "tests/fixtures/portal_reconcile/observations.json",
          "tests/fixtures/portal_reconcile/queue-export.json",
          "tools/check_secret_report.py", "tools/check_public_tree.py")
BEHAVIOR_TESTS = frozenset((
    "test_valid_singleton_counts_preserved", "test_identical_valid_duplicates_counts_preserved",
    "test_taint_invalid_allowlisted", "test_taint_unknown_host_class", "test_taint_item_id_path_mismatch",
    "test_taint_unknown_field", "test_taint_missing_host", "test_malformed_hash_does_not_poison_valid_hash",
    "test_malformed_only_group_not_in_denominator", "test_mixed_rows_conserve_denominators_under_permutation",
    "test_valid_conflicting_duplicates_stay_conservative", "test_held_classes_stay_conservative",
    "test_empty_input_deterministic", "test_repeat_render_aggregate_only", "test_cli_entry_point_reports_taint"))
# The precise defect tests: red on the pinned base module, green after the fix.
EXPECTED_BASE_RED = frozenset((
    "test_taint_invalid_allowlisted", "test_taint_unknown_host_class", "test_taint_item_id_path_mismatch",
    "test_taint_unknown_field", "test_taint_missing_host", "test_mixed_rows_conserve_denominators_under_permutation",
    "test_held_classes_stay_conservative", "test_repeat_render_aggregate_only", "test_cli_entry_point_reports_taint"))
CONTROL_TESTS = frozenset(("test_mutant_reproduces_fetched_classification", "test_taint_regression_detects_mutant",
                           "test_positive_control_green_on_mutant_and_restored", "test_production_module_untouched"))

APP_ROOTS = ("campaign_tool", "tests", "tools")
NETWORK_EVENTS = frozenset((
    "socket.connect", "socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr", "socket.getnameinfo",
    "socket.sendto", "socket.sendmsg", "socket.bind", "urllib.Request", "http.client.connect", "smtplib.connect",
    "imaplib.open", "poplib.connect", "ftplib.connect", "webbrowser.open"))
PROCESS_EVENTS = frozenset(("os.system", "os.exec", "os.fork", "os.forkpty", "os.spawn", "os.startfile", "pty.spawn"))
REDIRECTED_ENV = ("HOME", "TMPDIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR")
SENTINEL = "fa92_offline_sentinel"
RAN = re.compile(r"^Ran (\d+) tests? in ", re.M)


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    return sha256_bytes(Path(path).read_bytes())


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


# --------------------------------------------------------------- offline guard


def _argv(args):
    if isinstance(args, (str, bytes, os.PathLike)):
        return [os.fsdecode(args)]
    try:
        return [os.fsdecode(a) if isinstance(a, (bytes, os.PathLike)) else str(a) for a in args]
    except TypeError:
        return [repr(args)]


class Guard:
    """Audit-hook tripwire; every refused event is recorded before it raises."""

    def __init__(self, profile, root, allowed_extra=()):
        self.profile = profile
        self.root = Path(root).resolve()
        self.allowed_extra = [list(map(str, a)) for a in allowed_extra]
        self.hits, self.allowed, self.probes = [], [], []
        self.first_app_import = None
        self.preexisting = None
        self.originals = {}
        self.active = False
        self._probing = False

    def process_allowed(self, argv):
        exe = sys.executable
        if self.profile == "compatibility":
            # The unchanged selectors launch the CLI under the same interpreter.
            return (len(argv) >= 3 and argv[0] == exe and argv[1] == "-B"
                    and Path(argv[2]).resolve() == self.root / MODULE_PATH)
        if self.profile == "parent":
            if argv[:1] == ["git"]:
                return True  # Local repository identity, diff and baseline clone only.
            if argv[:2] == [exe, "-B"] and len(argv) >= 3 and Path(argv[2]).resolve() == RUNNER:
                return True  # The guarded unittest children of this launcher.
            return any(argv[:len(prefix)] == prefix for prefix in self.allowed_extra)
        return False  # behavior / negative-control children: no process at all.

    def _hit(self, event, detail):
        if self._probing:
            self.probes.append(event)
        else:
            self.hits.append({"event": event, "detail": detail[:400]})
        raise PermissionError("fa92 offline sentinel: " + event)

    def _audit(self, event, args):
        if not self.active:
            return
        if event in NETWORK_EVENTS or event in PROCESS_EVENTS:
            self._hit(event, repr(args))
        elif event == "subprocess.Popen":
            argv = _argv(args[1] if args[1] is not None else args[0])
            if self.process_allowed(argv):
                self.allowed.append(argv)
            else:
                self._hit(event, repr(argv))
        elif event == "import" and self.first_app_import is None and str(args[0]).split(".")[0] in APP_ROOTS:
            self.first_app_import = {"module": args[0], "guard_active": self.active}

    def _sentinel(self, label):
        def refuse(*args, **_kwargs):
            self._hit(label, repr(args))
        setattr(refuse, SENTINEL, True)
        return refuse

    def install(self):
        self.preexisting = sorted(n for n in sys.modules if n.split(".")[0] in APP_ROOTS)
        sys.addaudithook(self._audit)
        for owner, name in ((socket.socket, "connect"), (socket.socket, "connect_ex"), (socket.socket, "sendto"),
                            (socket, "create_connection"), (socket, "getaddrinfo"), (socket, "gethostbyname"),
                            (os, "system")):
            original = getattr(owner, name)
            label = "{}.{}".format(getattr(owner, "__name__", owner), name)
            self.originals[id(original)] = (label, original)  # Keep the object alive: ids stay unique.
            setattr(owner, name, self._sentinel(label))
        for name in ("run", "call", "check_call", "check_output", "Popen"):
            self.originals[id(getattr(subprocess, name))] = ("subprocess." + name, getattr(subprocess, name))
        self.active = True
        return self

    def probe(self):
        """Inert boundary probe: synthetic audit events must be refused (no socket/process is created)."""
        results = {}
        self._probing = True
        try:
            for event, args in (("socket.getaddrinfo", ("fa92-boundary.invalid", 0, 0, 0, 0)),
                                ("subprocess.Popen", ("fa92-not-allowed", ["fa92-not-allowed"], None, None))):
                try:
                    sys.audit(event, *args)
                    results[event] = "not_refused"
                except PermissionError:
                    results[event] = "refused"
        finally:
            self._probing = False
        results["socket_sentinels"] = all(getattr(f, SENTINEL, False) for f in (
            socket.socket.connect, socket.create_connection, socket.getaddrinfo))
        results["tempdir"] = tempfile.gettempdir()
        return results


def boundary_scan(guard, allowed_roots):
    """Measured scan of loaded app modules for host-path or original-callable bindings."""
    allowed_roots = [Path(r).resolve() for r in allowed_roots]
    findings, scanned = [], []

    def host_path(value):
        if isinstance(value, (str, os.PathLike)) and not isinstance(value, bytes):
            text = os.fspath(value)
            if isinstance(text, str) and text.startswith("/") and "\n" not in text and len(text) < 4096:
                resolved = Path(text)
                return not any(resolved == r or r in resolved.parents for r in allowed_roots)
        return False

    def check(module_name, where, value):
        if id(value) in guard.originals and guard.originals[id(value)][1] is value:
            findings.append({"module": module_name, "where": where, "kind": "original_callable",
                             "callable": guard.originals[id(value)][0]})
        elif host_path(value):
            findings.append({"module": module_name, "where": where, "kind": "host_path"})

    for name, module in sorted(sys.modules.items()):
        if name.split(".")[0] not in APP_ROOTS or not getattr(module, "__file__", None):
            continue
        scanned.append(name)
        for key, value in list(vars(module).items()):
            if key.startswith("__"):
                continue
            check(name, key, value)
            functions = [value]
            if isinstance(value, type) and value.__module__ == name:
                functions += list(vars(value).values())
            for item in functions:
                function = getattr(item, "__func__", item)
                if isinstance(function, types.FunctionType) and function.__module__ == name:
                    for index, default in enumerate(function.__defaults__ or ()):
                        check(name, "{}#default{}".format(function.__qualname__, index), default)
                    for kw, default in (function.__kwdefaults__ or {}).items():
                        check(name, "{}#{}".format(function.__qualname__, kw), default)
    return {"modules_scanned": scanned, "findings": findings}


def qualify_scan(guard, allowed_roots):
    """Inert: a synthetic app-named module with a host-path global and default and an original-callable default
    must give exactly those three findings. Nothing is opened or called."""
    name = "tests._fa92_boundary_scan_probe"
    probe = types.ModuleType(name)
    probe.__file__ = str(RUNNER)
    host = "/" + "fa92-synthetic-host-root/live.db"
    original = next(obj for label, obj in guard.originals.values() if label == "socket.create_connection")

    def synthetic(path=host, *, connect=original):
        return path, connect
    synthetic.__module__ = name
    synthetic.__qualname__ = "synthetic"
    probe.LIVE_PATH = host
    probe.synthetic = synthetic
    sys.modules[name] = probe
    try:
        found = [f for f in boundary_scan(guard, allowed_roots)["findings"] if f["module"] == name]
    finally:
        del sys.modules[name]
    expected = [{"module": name, "where": "LIVE_PATH", "kind": "host_path"},
                {"module": name, "where": "synthetic#default0", "kind": "host_path"},
                {"module": name, "where": "synthetic#connect", "kind": "original_callable",
                 "callable": "socket.create_connection"}]
    key = lambda item: json.dumps(item, sort_keys=True)  # noqa: E731
    return {"found": found, "qualified": sorted(map(key, found)) == sorted(map(key, expected))}


def redirect_process(run_dir):
    temp = run_dir / "tmp"
    temp.mkdir(exist_ok=True)
    tempfile.tempdir = str(temp)
    return temp


# ------------------------------------------------------------- guarded child


def child_main(args):
    run_dir = Path(args.child_run_dir).resolve()
    redirect_process(run_dir)
    guard = Guard(args.child_profile, args.child_root).install()
    probe = guard.probe()
    root = guard.root
    sys.path[:] = [p for p in sys.path if Path(p or os.curdir).resolve() != RUNNER.parent]
    sys.path.insert(0, str(root))
    loader = unittest.TestLoader()
    suite = unittest.TestSuite(loader.loadTestsFromName(name) for name in args.child_selectors)
    executed = sorted(_iter_ids(suite))  # Before running: the runner releases tests as it goes.
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    text = stream.getvalue()
    sys.stderr.write(text)
    scan = boundary_scan(guard, [root, run_dir, Path(sys.prefix), Path(sys.base_prefix)])
    scan_qualification = qualify_scan(guard, [root, run_dir, Path(sys.prefix), Path(sys.base_prefix)])

    def ids(items):
        return sorted({getattr(case, "test_case", case).id() for case, _ in items})
    report = {
        "profile": guard.profile, "root": str(root), "selectors": list(args.child_selectors),
        "interpreter": sys.executable, "cwd": os.getcwd(), "tempdir": tempfile.gettempdir(),
        "env": {k: os.environ.get(k) for k in REDIRECTED_ENV}, "probe": probe, "probe_events": guard.probes,
        "preexisting_app_modules": guard.preexisting, "first_app_import": guard.first_app_import,
        "hits": guard.hits, "allowed_processes": guard.allowed, "boundary_scan": scan,
        "scan_qualification": scan_qualification,
        "tests_run": result.testsRun, "executed": executed,
        "failures": ids(result.failures), "errors": ids(result.errors), "skipped": ids(result.skipped),
        "expected_failures": len(result.expectedFailures), "unexpected_successes": len(result.unexpectedSuccesses),
        "successful": result.wasSuccessful(), "output_sha256": sha256_bytes(text.encode()),
    }
    with open(args.child_report, "x") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
    return 0 if result.wasSuccessful() else 1


def _iter_ids(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _iter_ids(item)
        else:
            yield item.id()


def child_problems(report, run_dir, home, expect_process):
    label = report["profile"] + "@" + Path(report["root"]).name
    problems = []
    if report["hits"]:
        problems.append(label + ":offline_sentinel_triggered")
    if report["preexisting_app_modules"]:
        problems.append(label + ":app_imported_before_guard")
    if not (report["first_app_import"] and report["first_app_import"]["guard_active"]):
        problems.append(label + ":first_app_import_not_guarded")
    probe = report["probe"]
    if probe.get("socket.getaddrinfo") != "refused" or probe.get("subprocess.Popen") != "refused" \
            or not probe.get("socket_sentinels"):
        problems.append(label + ":boundary_probe_not_refused")
    if not Path(report["tempdir"]).resolve().is_relative_to(run_dir) or \
            not Path(probe["tempdir"]).resolve().is_relative_to(run_dir):
        problems.append(label + ":tempdir_not_redirected")
    if any(report["env"].get(k) != str(home) for k in REDIRECTED_ENV):
        problems.append(label + ":environment_not_redirected")
    if report["boundary_scan"]["findings"] or not report["boundary_scan"]["modules_scanned"]:
        problems.append(label + ":boundary_scan_findings")
    if not report["scan_qualification"]["qualified"]:
        problems.append(label + ":boundary_scan_not_qualified")
    if report["allowed_processes"] and not expect_process:
        problems.append(label + ":unexpected_process")
    if report["skipped"] or report["expected_failures"] or report["unexpected_successes"]:
        problems.append(label + ":skip_or_expected_failure")
    if report["tests_run"] <= 0 or len(report["executed"]) != report["tests_run"]:
        problems.append(label + ":empty_or_incomplete_selection")
    return problems


# ------------------------------------------------------------- parent helpers


def new_run_dir(output_root, case):
    root = Path(output_root).resolve()
    if root == REPO or REPO in root.parents:
        raise SystemExit("output root must be outside the repository")
    root.mkdir(parents=True, exist_ok=True)
    run = root / "{}-{}-{}-{}".format(case, now(), os.getpid(), secrets.token_hex(4))
    run.mkdir()
    return run.resolve()


def git(*args, binary=False, cwd=REPO):
    return subprocess.run(["git", "-C", str(cwd)] + list(args), capture_output=True, text=not binary,
                          timeout=120, check=True).stdout


def read_manifest():
    raw = MANIFEST.read_bytes()
    return json.loads(raw), sha256_bytes(raw)


def identity(manifest):
    data = {"head": git("rev-parse", "HEAD").strip()}
    status = git("status", "--porcelain=v1", "--untracked-files=all")
    diff = git("diff", "--binary", "HEAD", binary=True)
    untracked = []
    for name in sorted(n for n in git("ls-files", "-z", "--others", "--exclude-standard").split("\0") if n):
        path = REPO / name
        content = os.readlink(path).encode() if path.is_symlink() else path.read_bytes()
        untracked.append({"path": name, "symlink": path.is_symlink(), "sha256": sha256_bytes(content)})
    data["status_porcelain"] = status
    data["untracked"] = untracked
    data["dirty_diff_sha256"] = sha256_bytes(status.encode() + b"\0" + diff + b"\0"
                                             + json.dumps(untracked, sort_keys=True).encode())
    data["dirty"] = bool(status.strip())
    # The operator-staged .venv link is the only expected untracked entry in a frozen candidate.
    data["clean_except_staged_venv"] = [line for line in status.splitlines() if line] in ([], ["?? .venv"])
    base = manifest.get("head")
    data["base"] = base
    data["base_tree"] = git("rev-parse", str(base) + "^{tree}").strip()
    data["base_tree_matches_manifest"] = data["base_tree"] == manifest.get("tree")
    data["base_is_ancestor"] = subprocess.run(["git", "-C", str(REPO), "merge-base", "--is-ancestor", str(base),
                                               "HEAD"], capture_output=True, timeout=60).returncode == 0
    data["changed_files_vs_base"] = sorted(git("diff", "--name-only", str(base)).split())
    return data


def run_child(run_dir, label, argv, cwd, env=None, stdout_path=None, timeout=1800):
    record = {"label": label, "argv": [str(a) for a in argv], "cwd": str(cwd)}
    out_path = Path(stdout_path) if stdout_path else run_dir / (label + ".stdout")
    err_path = run_dir / (label + ".stderr")
    try:
        with open(out_path, "xb") as out, open(err_path, "xb") as err:
            done = subprocess.run(record["argv"], cwd=str(cwd), stdout=out, stderr=err, stdin=subprocess.DEVNULL,
                                  env=env, timeout=timeout, check=False)
        record["exit"] = done.returncode
    except (OSError, subprocess.SubprocessError) as error:
        record["exit"] = None
        record["launch_error"] = type(error).__name__
    for key, path in (("stdout", out_path), ("stderr", err_path)):
        if path.is_file():
            record[key + "_path"] = str(path)
            record[key + "_sha256"] = sha256_file(path)
            record[key + "_bytes"] = path.stat().st_size
    return record


def guarded_unittest(run_dir, result, label, selectors, root, profile):
    """Fresh file-launched guarded child running exactly ``selectors`` under ``root``."""
    home = run_dir / (label + "-home")
    home.mkdir()
    env = dict(os.environ)
    env.update({key: str(home) for key in REDIRECTED_ENV})
    report_path = run_dir / (label + ".child-report.json")
    argv = [sys.executable, "-B", str(RUNNER), "--child-report", str(report_path), "--child-profile", profile,
            "--child-root", str(root), "--child-run-dir", str(run_dir), "--child-selectors"] + list(selectors)
    record = run_child(run_dir, label, argv, root, env=env)
    result["children"].append(record)
    problems = []
    if not report_path.is_file():
        return record, None, [label + ":child_report_missing"]
    report = json.loads(report_path.read_text())
    record["child_report_path"] = str(report_path)
    record["child_report_sha256"] = sha256_file(report_path)
    stderr = Path(record["stderr_path"]).read_text(errors="replace")
    ran = RAN.findall(stderr)
    record.update(tests_run=report["tests_run"], skipped=len(report["skipped"]), failures=report["failures"],
                  errors=report["errors"], ran_line=int(ran[-1]) if ran else None)
    problems += child_problems(report, run_dir, home, profile == "compatibility")
    if record["ran_line"] != report["tests_run"]:
        problems.append(label + ":ran_line_mismatch")
    if report["root"] != str(Path(root).resolve()):
        problems.append(label + ":child_root_mismatch")
    return record, report, problems


def load_in_parent():
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    from tools import portal_reconcile
    focused = __import__(TEST_MODULE, fromlist=["*"])
    for module in (portal_reconcile, focused):
        if not Path(module.__file__).resolve().is_relative_to(REPO):
            raise RuntimeError("module_loaded_outside_candidate")
    return portal_reconcile, focused


def summarise(outcome):
    def ids(items):
        return sorted({getattr(case, "test_case", case).id().rsplit(".", 1)[-1] for case, _ in items})
    return {"tests_run": outcome.testsRun, "failures": ids(outcome.failures), "errors": ids(outcome.errors),
            "failing_subtests": len(outcome.failures) + len(outcome.errors), "skipped": ids(outcome.skipped),
            "successful": outcome.wasSuccessful()}


# ---------------------------------------------------------------------- cases


def case_behavior(run_dir, result):
    record, report, problems = guarded_unittest(run_dir, result, "behavior-final", [BEHAVIOR_SELECTOR], REPO,
                                                "behavior")
    if report is not None:
        executed = {i.rsplit(".", 1)[-1] for i in report["executed"]}
        result["named_cases"] = sorted(executed)
        if executed != BEHAVIOR_TESTS or report["tests_run"] != len(BEHAVIOR_TESTS):
            problems.append("behavior_named_cases_mismatch")
        if record.get("exit") != 0 or not report["successful"]:
            problems.append("behavior_not_green")
    # Pinned-base reproduction: the same Behavior tests bound to the base module, built in memory from git.
    portal_reconcile, focused = load_in_parent()
    base = result["identity"]["base"]
    source = git("show", "{}:{}".format(base, MODULE_PATH), binary=True)
    # Retained copy of the pinned-base bytes inside this run (evidence only; never imported by path).
    retained = run_dir / "pinned-base" / MODULE_PATH
    retained.parent.mkdir(parents=True)
    retained.write_bytes(source)
    module = types.ModuleType("tools._fa92_pinned_base")
    module.__file__ = str(retained)
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    outcome, text = focused.run_behavior(module, sorted(BEHAVIOR_TESTS))
    (run_dir / "baseline-red.txt").write_text(text)
    summary = summarise(outcome)
    red = set(summary["failures"] + summary["errors"])
    result["baseline_red"] = dict(summary, base=base, base_module_sha256=sha256_bytes(source),
                                  output_path=str(run_dir / "baseline-red.txt"),
                                  output_sha256=sha256_file(run_dir / "baseline-red.txt"),
                                  expected_red=sorted(EXPECTED_BASE_RED))
    if red != EXPECTED_BASE_RED or summary["errors"] or summary["skipped"] \
            or summary["tests_run"] != len(BEHAVIOR_TESTS):
        problems.append("baseline_red_differs_from_expected_defect_tests")
    return problems


def case_compatibility(run_dir, result):
    final, report, problems = guarded_unittest(run_dir, result, "compatibility-final", COMPAT_SELECTORS, REPO,
                                               "compatibility")
    if report is None or final.get("exit") != 0 or not report["successful"]:
        problems.append("compatibility_final_not_green")
    base = result["identity"]["base"]
    clone = run_dir / "baseline-clone"
    for label, argv in (("baseline-clone", ["git", "clone", "-q", "--no-hardlinks", "--no-checkout", str(REPO),
                                            str(clone)]),
                        ("baseline-checkout", ["git", "-C", str(clone), "checkout", "-q", "--detach", base])):
        record = run_child(run_dir, label, argv, run_dir)
        result["children"].append(record)
        if record.get("exit") != 0:
            return problems + [label + "_failed"]
    if git("rev-parse", "HEAD", cwd=clone).strip() != base:
        return problems + ["baseline_clone_head_mismatch"]
    baseline, base_report, base_problems = guarded_unittest(run_dir, result, "compatibility-baseline",
                                                            COMPAT_SELECTORS, clone, "compatibility")
    result["compatibility"] = {
        "selectors": list(COMPAT_SELECTORS), "final_tests_run": final.get("tests_run"),
        "final_exit": final.get("exit"), "baseline_tests_run": baseline.get("tests_run"),
        "baseline_exit": baseline.get("exit"), "baseline_problems": base_problems,
        "same_test_ids": bool(report and base_report and report["executed"] == base_report["executed"])}
    if base_problems or base_report is None or not base_report["successful"]:
        problems.append("baseline_compatibility_unexpected_result")
    if not result["compatibility"]["same_test_ids"]:
        problems.append("compatibility_test_ids_changed")
    return problems


def case_negative_control(run_dir, result):
    record, report, problems = guarded_unittest(run_dir, result, "gate-control", [CONTROL_SELECTOR], REPO,
                                                "negative-control")
    if report is not None:
        executed = {i.rsplit(".", 1)[-1] for i in report["executed"]}
        if executed != CONTROL_TESTS:
            problems.append("control_named_cases_mismatch")
        if record.get("exit") != 0 or not report["successful"]:
            problems.append("gate_control_not_green")
    # Parent-side retained evidence: real failing output on the disposable copy, then the restored positive.
    portal_reconcile, focused = load_in_parent()
    disk_before = sha256_file(REPO / MODULE_PATH)
    names = [focused.TAINT_REGRESSION, focused.POSITIVE_CONTROL]
    mutant_outcome, mutant_text = focused.run_behavior(focused.disposable_mutant(), names)
    restored_outcome, restored_text = focused.run_behavior(portal_reconcile, names)
    (run_dir / "control-mutant-failing.txt").write_text(mutant_text)
    (run_dir / "control-restored-positive.txt").write_text(restored_text)
    mutant, restored = summarise(mutant_outcome), summarise(restored_outcome)
    result["negative_control"] = {
        "mutation": "disposable in-memory module copy; hash-declaration helper returns None (old early continue)",
        "mutant": mutant, "restored": restored,
        "mutant_output_sha256": sha256_file(run_dir / "control-mutant-failing.txt"),
        "restored_output_sha256": sha256_file(run_dir / "control-restored-positive.txt"),
        "production_file_unchanged": sha256_file(REPO / MODULE_PATH) == disk_before,
        "defect_detected": mutant["failures"] == [focused.TAINT_REGRESSION] and not mutant["errors"]
        and "'fetched': 1" in mutant_text}
    nc = result["negative_control"]
    if not nc["defect_detected"] or mutant["tests_run"] != 2:
        problems.append("targeted_defect_not_detected")
    if not restored["successful"] or restored["tests_run"] != 2:
        problems.append("restored_positive_not_green")
    if not nc["production_file_unchanged"]:
        problems.append("production_file_changed")
    return problems


def case_credential_scan(run_dir, result):
    manifest = result["manifest"]
    runtime = manifest.get("runtime") or {}
    python, scanner = runtime.get("credential_scan_python3"), runtime.get("detect_secrets")
    result["credential_runtime"] = {"credential_scan_python3": python, "detect_secrets": scanner}
    if not (python and scanner and Path(python).is_file() and Path(scanner).is_file()):
        return ["declared_credential_runtime_missing"]
    report = run_dir / "secret-scan.json"
    scan = run_child(run_dir, "detect-secrets-scan",
                     [python, scanner, "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"],
                     REPO, stdout_path=report)
    result["children"].append(scan)
    problems = [] if scan.get("exit") == 0 else ["secret_scan_failed"]
    try:
        data = json.loads(report.read_text())
        hits = data["results"]
        result["secret_scan"] = {"files_with_hits": len(hits), "raw_hits": sum(len(v) for v in hits.values()),
                                 "hits_in_card_files": sorted(set(hits) & {MODULE_PATH, TEST_PATH,
                                                                           "tools/accept_fa92.py"})}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return problems + ["secret_report_malformed"]
    checker = run_child(run_dir, "check-secret-report", [python, str(REPO / "tools" / "check_secret_report.py"),
                                                         str(report)], REPO)
    result["children"].append(checker)
    text = Path(checker["stdout_path"]).read_text(errors="replace") if "stdout_path" in checker else ""
    match = re.search(r"potential secrets: (\d+);", text)
    result["secret_scan"]["potential_secrets"] = int(match.group(1)) if match else None
    if checker.get("exit") != 0:
        problems.append("check_secret_report_failed")
    if result["secret_scan"]["potential_secrets"] != 0:
        problems.append("potential_secrets_nonzero_or_unreported")
    if result["secret_scan"]["hits_in_card_files"]:
        problems.append("raw_hits_in_card_files")
    return problems


def case_public_tree(run_dir, result):
    python = REPO / ".venv" / "bin" / "python3"
    record = run_child(run_dir, "check-public-tree", [str(python), str(REPO / "tools" / "check_public_tree.py")],
                       REPO)
    result["children"].append(record)
    return [] if record.get("exit") == 0 else ["public_tree_violations"]


HANDLERS = {"behavior": case_behavior, "compatibility": case_compatibility,
            "negative-control": case_negative_control, "credential-scan": case_credential_scan,
            "public-tree": case_public_tree}
APP_IMPORTING = ("behavior", "negative-control")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--case", choices=CASES)
    parser.add_argument("--output-root")
    parser.add_argument("--child-report", help=argparse.SUPPRESS)
    parser.add_argument("--child-profile", choices=("behavior", "compatibility", "negative-control"),
                        help=argparse.SUPPRESS)
    parser.add_argument("--child-root", help=argparse.SUPPRESS)
    parser.add_argument("--child-run-dir", help=argparse.SUPPRESS)
    parser.add_argument("--child-selectors", nargs="+", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.child_report:
        if args.case or args.output_root or not (args.child_profile and args.child_root and args.child_run_dir
                                                 and args.child_selectors):
            parser.error("child mode takes only its own arguments")
        return child_main(args)
    if not (args.case and args.output_root):
        parser.error("--case and --output-root are required")
    run_dir = new_run_dir(args.output_root, args.case)
    redirect_process(run_dir)
    manifest, manifest_sha = read_manifest()
    runtime = manifest.get("runtime") or {}
    extra = []
    if runtime.get("credential_scan_python3") and runtime.get("detect_secrets"):
        extra.append([runtime["credential_scan_python3"], runtime["detect_secrets"], "scan"])
        extra.append([runtime["credential_scan_python3"], str(REPO / "tools" / "check_secret_report.py")])
    extra.append([str(REPO / ".venv" / "bin" / "python3"), str(REPO / "tools" / "check_public_tree.py")])
    guard = Guard("parent", REPO, extra).install()
    result = {"schema": SCHEMA, "card": CARD, "case": args.case, "argv": [sys.executable] + list(sys.argv),
              "cwd": os.getcwd(), "interpreter": sys.executable, "python_version": sys.version,
              "repo": str(REPO), "run_dir": str(run_dir), "started_utc": now(),
              "manifest": manifest, "manifest_sha256": manifest_sha, "children": [],
              "acceptance": "REVIEW REQUIRED; machine validity only, not semantic/legal/production acceptance"}
    problems = []
    try:
        result["identity"] = identity(manifest)
        result["hashes"] = {rel: (sha256_file(REPO / rel) if (REPO / rel).is_file() else None) for rel in HASHED}
        if not result["identity"]["base_tree_matches_manifest"] or not result["identity"]["base_is_ancestor"]:
            problems.append("manifest_base_not_bound")
        problems += HANDLERS[args.case](run_dir, result)
    except Exception as error:  # Incomplete case: record the class and fail closed.
        problems.append("case_incomplete:" + type(error).__name__)
        result["exception"] = repr(error)[:2000]
    result["parent_probe"] = guard.probe()
    parent_roots = [REPO, run_dir, Path(sys.prefix), Path(sys.base_prefix)]
    result["parent_guard"] = {"hits": guard.hits, "allowed_processes": guard.allowed,
                              "preexisting_app_modules": guard.preexisting,
                              "first_app_import": guard.first_app_import,
                              "boundary_scan": boundary_scan(guard, parent_roots),
                              "scan_qualification": qualify_scan(guard, parent_roots)}
    pg = result["parent_guard"]
    if pg["hits"] or pg["preexisting_app_modules"] or pg["boundary_scan"]["findings"] \
            or not pg["scan_qualification"]["qualified"]:
        problems.append("parent_guard_problem")
    if args.case in APP_IMPORTING and not (pg["first_app_import"] and pg["first_app_import"]["guard_active"]):
        problems.append("parent_first_app_import_not_guarded")
    if result["parent_probe"].get("socket.getaddrinfo") != "refused":
        problems.append("parent_boundary_probe_not_refused")
    result["finished_utc"] = now()
    result["problems"] = problems
    result["verdict"] = "PASS_REVIEW_REQUIRED" if not problems else "FAIL"
    with open(run_dir / "receipt.json", "x") as handle:
        json.dump(result, handle, indent=2, sort_keys=True, default=str)
    ident = result.get("identity", {})
    print(json.dumps({"case": args.case, "verdict": result["verdict"], "problems": problems,
                      "run_dir": str(run_dir), "head": ident.get("head"), "dirty": ident.get("dirty"),
                      "dirty_diff_sha256": ident.get("dirty_diff_sha256"), "manifest_sha256": manifest_sha,
                      "receipt_sha256": sha256_file(run_dir / "receipt.json")}, sort_keys=True))
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
