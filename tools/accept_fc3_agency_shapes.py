"""Acceptance runner for FC3-AGENCY-SHAPES (offline, synthetic, review-required).

Usage (one check per invocation):
    <interpreter> tools/accept_fc3_agency_shapes.py --check CHECK --output-root DIR

CHECK is one of: focused, compatibility, negative, portability, credentials,
public-tree. Each invocation creates a new exclusive child directory under DIR
(earlier evidence is never overwritten or removed). That directory holds raw
child stdout/stderr, argv, cwd, exit codes, executed test IDs and outcomes,
case coverage and source hashes in ``run.json``. The process exits 0 only
when every assertion of the selected check holds.

Isolation boundary. The parent never imports application or test modules.
Every application import (focused, compatibility, negative and portability
tests, and the pinned-base differential) happens in a guarded child: this
same script re-run with ``-I -B --guarded-child SPEC``. Before its first
application import the child points HOME, TMPDIR/TMP/TEMP and XDG paths, and
tempfile's cached default, at per-run directories under the run directory;
installs offline sentinels (socket connect/bind/resolve), model-client import
sentinels and an unexpected-child guard (only the repository secret-report
checker on a report inside the per-run temporary root may be launched);
snapshots that guard state at the first application import; and runs inert
probes showing unexpected child specifications, connects and model imports
are refused before anything executes. The parent records and verifies all
of it.

The repository root comes from this file's location. Import search paths are
set only inside this script. Children are argument arrays without a shell,
limited to the enumerated local programs. A passing result is machine
validity only: REVIEW REQUIRED. It is never semantic, legal, stage or release
acceptance, and it grants no publication authority.
"""
import argparse
import datetime
import hashlib
import importlib
import importlib.abc
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
import traceback
import types
import unittest

sys.dont_write_bytecode = True

REPO = Path(__file__).resolve().parents[1]
CARD = "FC3-AGENCY-SHAPES"
CHECKS = ("focused", "compatibility", "negative", "portability", "credentials", "public-tree")
PRODUCTION = "campaign_tool/records/agency_reconciliation.py"
FOCUSED_PATH = "tests/records/test_fc3_agency_shapes.py"
RUNNER_PATH = "tools/accept_fc3_agency_shapes.py"
SECRET_CHECKER = "tools/check_secret_report.py"
ALLOWED_CHANGES = (PRODUCTION, FOCUSED_PATH, RUNNER_PATH)
FOCUSED_MODULE = "tests.records.test_fc3_agency_shapes"
COMPAT_SELECTORS = (
    "tests.records.test_agency_reconciliation",
    "tests.records.test_agency_reconciliation_regressions",
    "tests.records.test_agency_reconciliation_io",
    "tests.test_secret_report",
    "tests.test_public_tree",
)
# Compatibility selectors that create temporary fixtures / launch the checker.
COMPAT_TEMP_USERS = ("tests.records.test_agency_reconciliation_io", "tests.test_secret_report")
COMPAT_CHECKER_USER = "tests.test_secret_report"
BASE_SELECTOR = "parent of the first commit adding " + FOCUSED_PATH
HASHED_SOURCES = ALLOWED_CHANGES + (
    "tests/records/test_agency_reconciliation.py",
    "tests/records/test_agency_reconciliation_regressions.py",
    "tests/records/test_agency_reconciliation_io.py",
    "tests/test_secret_report.py",
    "tests/test_public_tree.py",
    SECRET_CHECKER,
    "tools/check_public_tree.py",
)
APPLICATION_ROOTS = ("campaign_tool", "tests", "tools")
# Model-client packages refused at import inside guarded children.
MODEL_CLIENTS = ("openai", "anthropic", "ollama", "litellm", "google.genai", "google.generativeai",
                 "transformers", "llama_cpp", "mistralai", "cohere", "groq")
ISOLATED_VARS = ("HOME", "TMPDIR", "TMP", "TEMP", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME",
                 "XDG_STATE_HOME")
# Frozen named case -> TestCase class in the focused module.
CASES = {
    "both_target_roles": "BothTargetRolesTests",
    "existing_nontarget_roles": "ExistingNontargetRolesTests",
    "malformed_status_types": "MalformedStatusTypesTests",
    "malformed_role_types": "MalformedRoleTypesTests",
    "malformed_excluded_card": "MalformedExcludedCardTests",
    "unknown_status_string": "UnknownStatusStringTests",
    "empty_inputs_and_conflicting_candidates": "EmptyInputsAndConflictingCandidatesTests",
    "inputs_unchanged_and_no_approval": "InputsUnchangedAndNoApprovalTests",
}
# Focused tests that must fail against the pinned base source (raw TypeError or
# silent skip) - the preserved regression. Every other focused test must pass there.
BASELINE_RED = (
    "MalformedStatusTypesTests.test_malformed_status_types_raise_fixed_value_error",
    "MalformedStatusTypesTests.test_malformed_status_after_valid_cards_and_with_matching_records",
    "MalformedRoleTypesTests.test_malformed_role_types_raise_fixed_value_error",
    "MalformedRoleTypesTests.test_malformed_role_after_valid_cards_and_with_matching_records",
    "MalformedExcludedCardTests.test_malformed_role_on_scope_excluded_card_with_digest_rows",
    "MalformedExcludedCardTests.test_malformed_role_on_hint_present_parent_only_card",
    "MalformedExcludedCardTests.test_malformed_status_on_card_without_any_reference",
    "InputsUnchangedAndNoApprovalTests.test_rejected_inputs_unchanged",
)
# Negative control: each new guard is bypassed independently, in memory only.
# A bypassed status guard falls back to the pinned membership-only test; a
# bypassed role guard performs no role check (pinned behaviour).
BYPASS_DETECTORS = {
    "status_type_guard": (
        "MalformedStatusTypesTests.test_malformed_status_types_raise_fixed_value_error",
        "MalformedStatusTypesTests.test_malformed_status_after_valid_cards_and_with_matching_records",
        "MalformedExcludedCardTests.test_malformed_status_on_card_without_any_reference",
    ),
    "role_guard": (
        "MalformedRoleTypesTests.test_malformed_role_types_raise_fixed_value_error",
        "MalformedRoleTypesTests.test_malformed_role_after_valid_cards_and_with_matching_records",
        "MalformedExcludedCardTests.test_malformed_role_on_scope_excluded_card_with_digest_rows",
        "MalformedExcludedCardTests.test_malformed_role_on_hint_present_parent_only_card",
        "InputsUnchangedAndNoApprovalTests.test_rejected_inputs_unchanged",
    ),
}
STATIC_SCANNED = (FOCUSED_PATH, RUNNER_PATH)
# Assembled from parts so this list does not match its own source text.
BOX_SPECIFIC = ("/ho" + "me/", "agent" + "-work", "venv" + "-fullsuite", "/Us" + "ers/", ".her" + "mes",
                "kan" + "ban/", "/t" + "mp/")
INLINE_FORMS = (
    re.compile(r"""["']-""" + r"""[ce]["']"""),
    re.compile("shell" + r"\s*=\s*True"),
    re.compile("PYTHON" + "PATH"),
    re.compile(r"\bos\.sys" + r"tem\("),
    re.compile(r"\bos\.po" + r"pen\("),
)


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    return sha256_bytes(Path(path).read_bytes())


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def within(path, root):
    path, root = Path(path).resolve(), Path(root).resolve()
    return path == root or root in path.parents


def new_run_dir(output_root, check):
    root = Path(output_root).resolve()
    if within(root, REPO):
        raise SystemExit("output root must be outside the candidate repository")
    root.mkdir(parents=True, exist_ok=True)
    run = root / "{}-{}-{}-{}".format(check, now(), os.getpid(), secrets.token_hex(4))
    run.mkdir(exist_ok=False)
    return run


def source_hashes(root):
    return {rel: (sha256_file(root / rel) if (root / rel).is_file() else None) for rel in HASHED_SOURCES}


# ----------------------------------------------------------------- isolation primitives


def isolated_dirs(base):
    """Per-run HOME/temp/XDG directories, all under ``base`` (inside the run dir)."""
    base = Path(base)
    dirs = {"HOME": base / "home", "TMPDIR": base / "tmp", "XDG_CONFIG_HOME": base / "home" / "config",
            "XDG_DATA_HOME": base / "home" / "data", "XDG_CACHE_HOME": base / "home" / "cache",
            "XDG_STATE_HOME": base / "home" / "state"}
    dirs["TMP"] = dirs["TEMP"] = dirs["TMPDIR"]
    return {key: str(dirs[key]) for key in ISOLATED_VARS}


def isolated_env(base):
    """Inherited environment minus interpreter overrides, with per-run HOME/temp/XDG."""
    paths = isolated_dirs(base)
    for value in paths.values():
        Path(value).mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
    env.update(paths)
    return env


def child_spec_refusal(args, positional, kwargs, interpreter, allowed_script, temp_root):
    """Pure check of one child specification; returns a refusal reason or None.

    The only child a guarded test process may start is the repository
    secret-report checker, run by this interpreter, on a report file inside
    the per-run temporary root, without a shell or executable override.
    """
    if positional:
        return "positional_popen_options"
    if kwargs.get("shell"):
        return "shell_requested"
    if kwargs.get("executable") is not None:
        return "executable_override"
    if kwargs.get("env") is not None:
        return "explicit_child_environment"
    if isinstance(args, (str, bytes, os.PathLike)) or not isinstance(args, (list, tuple)):
        return "argv_not_argument_array"
    argv = [str(a) for a in args]
    if len(argv) != 3:
        return "argv_shape"
    if argv[0] != str(interpreter):
        return "program_not_admitted_interpreter"
    if argv[1] != str(allowed_script):
        return "script_not_enumerated_checker"
    if not within(argv[2], temp_root):
        return "report_outside_per_run_temp_root"
    return None


class Guard:
    """Early offline / model / child / temp controls for one guarded child."""

    def __init__(self, repo_root, isolated, temp_root):
        self.repo_root = Path(repo_root).resolve()
        self.isolated = isolated
        self.temp_root = Path(temp_root).resolve()
        self.allowed_script = self.repo_root / SECRET_CHECKER
        self.hits = []
        self.allowed_children = []
        self.temp_dirs = []
        self.first_import = None
        self.allowing = False
        self.sentinels = {}

    def refuse(self, name):
        def blocked(*_args, **_kwargs):
            if name in ("os.posix_spawn", "os.posix_spawnp") and self.allowing:
                return self.originals[name](*_args, **_kwargs)
            self.hits.append(name)
            raise PermissionError("offline sentinel: " + name)
        blocked.fc3_sentinel = name
        self.sentinels[name] = blocked
        return blocked

    def install(self):
        os.environ.update(self.isolated)
        tempfile.tempdir = str(self.temp_root)  # Replaces any cached default.
        self.originals = {}
        targets = [(socket.socket, "connect"), (socket.socket, "connect_ex"), (socket.socket, "bind"),
                   (socket.socket, "sendto"), (socket, "create_connection"), (socket, "create_server"),
                   (socket, "getaddrinfo"), (socket, "gethostbyname"), (socket, "gethostbyname_ex")]
        for name in ("system", "popen", "execv", "execve", "execvp", "execvpe", "execl", "execle", "execlp",
                     "execlpe", "fork", "forkpty", "posix_spawn", "posix_spawnp", "spawnv", "spawnve",
                     "spawnvp", "spawnvpe", "spawnl", "spawnle", "spawnlp", "spawnlpe"):
            if hasattr(os, name):
                targets.append((os, name))
        for owner, attribute in targets:
            label = "{}.{}".format("socket.socket" if owner is socket.socket else owner.__name__, attribute)
            self.originals[label] = getattr(owner, attribute)
            setattr(owner, attribute, self.refuse(label))
        self.install_popen_guard()
        self.install_temp_recorder()
        self.blocker = ModelImportBlocker(self)
        self.watch = FirstImportWatch(self)
        sys.meta_path.insert(0, self.blocker)
        sys.meta_path.insert(0, self.watch)

    def install_popen_guard(self):
        guard = self
        original = subprocess.Popen.__init__

        def guarded_init(popen, args, *positional, **kwargs):
            reason = child_spec_refusal(args, positional, kwargs, sys.executable, guard.allowed_script,
                                        guard.temp_root)
            if reason:
                guard.hits.append("subprocess.Popen:" + reason)
                raise PermissionError("unexpected child refused: " + reason)
            guard.allowed_children.append([str(a) for a in args])
            guard.allowing = True
            try:
                return original(popen, args, *positional, **kwargs)
            finally:
                guard.allowing = False
        guarded_init.fc3_sentinel = "subprocess.Popen"
        subprocess.Popen.__init__ = guarded_init

    def install_temp_recorder(self):
        guard = self
        original = tempfile.mkdtemp

        def recorded_mkdtemp(*args, **kwargs):
            path = original(*args, **kwargs)
            guard.temp_dirs.append({"path": str(path), "under_temp_root": within(path, guard.temp_root)})
            return path
        recorded_mkdtemp.fc3_sentinel = "tempfile.mkdtemp"
        tempfile.mkdtemp = recorded_mkdtemp

    def state(self):
        def guarded(owner, attribute):
            return hasattr(getattr(owner, attribute, None), "fc3_sentinel")
        return {
            "tempfile_tempdir": tempfile.tempdir,
            "tempfile_gettempdir": tempfile.gettempdir(),
            "gettempdir_under_temp_root": within(tempfile.gettempdir(), self.temp_root),
            "environment": {k: os.environ.get(k) for k in ISOLATED_VARS},
            "environment_redirected": all(os.environ.get(k) == v for k, v in self.isolated.items()),
            "python_override_vars": sorted(k for k in os.environ if k.startswith("PYTHON")),
            "socket_guarded": all(guarded(socket.socket, a) for a in ("connect", "connect_ex", "bind", "sendto"))
            and all(guarded(socket, a) for a in ("create_connection", "getaddrinfo", "gethostbyname")),
            "process_guarded": guarded(subprocess.Popen, "__init__")
            and all(guarded(os, a) for a in ("system", "fork", "execv", "execve", "posix_spawn") if hasattr(os, a)),
            "model_import_blocker_installed": self.blocker in sys.meta_path,
            "temp_recorder_installed": guarded(tempfile, "mkdtemp"),
        }

    def state_ok(self, state):
        return bool(state and state["gettempdir_under_temp_root"] and state["environment_redirected"]
                    and not state["python_override_vars"] and state["socket_guarded"]
                    and state["process_guarded"] and state["model_import_blocker_installed"]
                    and state["temp_recorder_installed"])


class FirstImportWatch(importlib.abc.MetaPathFinder):
    """Snapshots guard state when the first application module is looked up."""

    def __init__(self, guard):
        self.guard = guard

    def find_spec(self, name, path=None, target=None):
        if self.guard.first_import is None and name.split(".")[0] in APPLICATION_ROOTS:
            self.guard.first_import = dict(self.guard.state(), module=name)
        return None


class ModelImportBlocker(importlib.abc.MetaPathFinder):
    def __init__(self, guard):
        self.guard = guard

    def find_spec(self, name, path=None, target=None):
        if any(name == blocked or name.startswith(blocked + ".") for blocked in MODEL_CLIENTS):
            self.guard.hits.append("model_import:" + name)
            raise ImportError("offline sentinel: model client import refused: " + name)
        return None


def inert_probes(guard):
    """Show refusals with inputs that would be harmless even if a guard failed.

    Absent programs/scripts/report paths and an absent local socket path are
    used, and every refusal must be the sentinel's PermissionError/ImportError.
    """
    temp = guard.temp_root
    absent = temp / "fc3-absent"
    probes = []

    def expect(label, action, error_type, marker):
        before = len(guard.hits)
        try:
            action()
            outcome = "not_refused"
        except error_type as error:
            outcome = "refused" if marker in str(error) else "other:" + str(error)[:120]
        except Exception as error:  # Any other error means the guard did not decide.
            outcome = "other:" + type(error).__name__
        probes.append({"probe": label, "outcome": outcome, "hit_recorded": len(guard.hits) > before})

    checker = str(guard.allowed_script)
    for label, args, kwargs in (
            ("absent_program", [str(absent / "program")], {}),
            ("interpreter_unenumerated_script", [sys.executable, str(absent / "script.py")], {}),
            ("checker_report_outside_temp_root", [sys.executable, checker, str(guard.repo_root / "fc3-absent.json")], {}),
            ("checker_extra_argument", [sys.executable, checker, str(absent / "r.json"), "--extra"], {}),
            ("string_command", str(absent / "program"), {}),
            ("shell_flag", [str(absent / "program")], {"shell": True}),
            ("executable_override", [sys.executable, checker, str(absent / "r.json")],
             {"executable": str(absent / "program")}),
            ("explicit_environment", [sys.executable, checker, str(absent / "r.json")], {"env": {}})):
        expect("child:" + label, lambda a=args, k=kwargs: subprocess.Popen(a, **k), PermissionError,
               "unexpected child refused")

    def unix_connect():
        handle = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            handle.connect(str(absent / "socket"))
        finally:
            handle.close()
    expect("network:unix_socket_connect", unix_connect, PermissionError, "offline sentinel")
    expect("network:getaddrinfo", lambda: socket.getaddrinfo("fc3-absent.invalid", 9), PermissionError,
           "offline sentinel")
    expect("model:import_client", lambda: importlib.import_module(MODEL_CLIENTS[0]), ImportError,
           "offline sentinel")
    with tempfile.TemporaryDirectory() as folder:
        probes.append({"probe": "temp:temporary_directory_root", "path": folder,
                       "outcome": "under_temp_root" if within(folder, guard.temp_root) else "escaped"})
    return probes


# ----------------------------------------------------------------- guarded child


class OutcomeResult(unittest.TextTestResult):
    """Explicit per-test outcomes; a failing subtest marks its parent test."""
    RANK = {"ok": 0, "skipped": 1, "expected failure": 1, "unexpected success": 2, "FAIL": 3, "ERROR": 4}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.outcomes = {}

    def mark(self, test, outcome):
        key = test.id()
        if self.RANK[outcome] >= self.RANK.get(self.outcomes.get(key), -1):
            self.outcomes[key] = outcome

    def addSuccess(self, test):
        super().addSuccess(test)
        self.mark(test, "ok")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.mark(test, "FAIL")

    def addError(self, test, err):
        super().addError(test, err)
        self.mark(test, "ERROR")

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.mark(test, "skipped")

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self.mark(test, "expected failure")

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self.mark(test, "unexpected success")

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            self.mark(test, "FAIL" if issubclass(err[0], test.failureException) else "ERROR")


def suite_ids(suite):
    ids = []

    def walk(item):
        if isinstance(item, unittest.TestSuite):
            for child in item:
                walk(child)
        else:
            ids.append(item.id())
    walk(suite)
    return ids


def run_suite(suite, output_path):
    """Run ``suite``; returns the result with ``loaded_ids`` captured before the run
    (TestSuite drops its tests after running them)."""
    loaded = suite_ids(suite)
    stream = io.StringIO()
    outcome = unittest.TextTestRunner(stream=stream, verbosity=2, resultclass=OutcomeResult).run(suite)
    Path(output_path).write_text(stream.getvalue())
    outcome.loaded_ids = loaded
    return outcome


def suite_record(outcome, output_path):
    return {"loaded_ids": outcome.loaded_ids, "tests_run": outcome.testsRun, "outcomes": outcome.outcomes,
            "successful": outcome.wasSuccessful(), "skipped": len(outcome.skipped),
            "expected_failures": len(outcome.expectedFailures),
            "unexpected_successes": len(outcome.unexpectedSuccesses),
            "output_path": str(output_path), "output_sha256": sha256_file(output_path)}


def summarise(outcome, prefix=""):
    """Per-test outcome IDs; failing subtests are folded into their parent test ID."""
    def ident(case):
        value = getattr(case, "test_case", case).id()
        return value[len(prefix):] if prefix and value.startswith(prefix) else value

    def subtests(pairs):
        return sum(1 for case, _ in pairs if hasattr(case, "test_case"))
    return {"tests_run": outcome.testsRun,
            "failures": sorted({ident(c) for c, _ in outcome.failures}),
            "errors": sorted({ident(c) for c, _ in outcome.errors}),
            "failing_subtests": subtests(outcome.failures) + subtests(outcome.errors),
            "skipped": sorted({ident(c) for c, _ in outcome.skipped}),
            "unexpected_successes": sorted({ident(c) for c in outcome.unexpectedSuccesses}),
            "successful": outcome.wasSuccessful()}


def focused_method_names(focused):
    loader = unittest.TestLoader()
    return sorted(cls + "." + name for cls in CASES.values()
                  for name in loader.getTestCaseNames(getattr(focused, cls)))


def run_names(module, names, output_path):
    loader = unittest.TestLoader()
    suite = unittest.TestSuite(loader.loadTestsFromName(name, module) for name in names)
    return run_suite(suite, output_path)


def classify(reconcile_function, cards):
    """Observed outcome class of one direct call (for the evidence record)."""
    try:
        result = reconcile_function(cards, [])
    except Exception as error:
        return type(error).__name__ + ":" + str(error)[:80]
    return "returned:targets={}".format(len(result["cards"]))


def variant_observations(focused, reconcile_function):
    rows = []
    for name, value in focused.NON_STRING_STATUSES:
        rows.append({"field": "agency_status", "variant": name,
                     "outcome": classify(reconcile_function,
                                         [focused.with_field(focused.card("probe"), "agency_status", value)])})
    for name, value in focused.NON_STRING_ROLES + focused.BLANK_ROLES:
        rows.append({"field": "role", "variant": name,
                     "outcome": classify(reconcile_function,
                                         [focused.with_field(focused.card("probe"), "role", value)])})
    return rows


def load_focused(root):
    from campaign_tool.records import agency_reconciliation as production
    focused = importlib.import_module(FOCUSED_MODULE)
    for module in (production, focused):
        if not within(module.__file__, root):
            raise RuntimeError("module_loaded_outside_candidate")
    return production, focused


def task_unittest(spec, out):
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromNames(list(spec["targets"]))
    path = out / (spec["label"] + "-unittest.txt")
    return {"unittest": suite_record(run_suite(suite, path), path)}


def task_focused(spec, out):
    data = task_unittest(spec, out)
    _production, focused = load_focused(spec["repo_root"])
    source = Path(spec["base_source_path"]).read_bytes()
    base = types.ModuleType("campaign_tool.records._fc3_baseline_agency_reconciliation")
    base.__file__ = "<git {}:{}>".format(spec["base_commit"], PRODUCTION)
    exec(compile(source, base.__file__, "exec"), base.__dict__)
    candidate = focused.reconcile
    names = focused_method_names(focused)
    path = out / "baseline-differential.txt"
    try:
        focused.reconcile = base.reconcile
        outcome = run_names(focused, names, path)
        observed = variant_observations(focused, base.reconcile)
    finally:
        focused.reconcile = candidate
    summary = summarise(outcome, FOCUSED_MODULE + ".")
    data["baseline_differential"] = {
        "base_commit": spec["base_commit"], "base_production_sha256": sha256_bytes(source),
        "names": names, "tests_run": outcome.testsRun, "skipped": summary["skipped"],
        "base_red": sorted(set(summary["failures"] + summary["errors"])),
        "base_variant_outcomes": observed, "candidate_variant_outcomes": variant_observations(focused, candidate),
        "status_message": focused.STATUS_MESSAGE, "role_message": focused.ROLE_MESSAGE,
        "output_sha256": sha256_file(path), "restored": focused.reconcile is candidate}
    return data


def task_negative(spec, out):
    production, focused = load_focused(spec["repo_root"])
    names = focused_method_names(focused)
    guards = {"status_type_guard": "_require_agency_status", "role_guard": "_require_role"}
    originals = {}
    for attribute in guards.values():
        if not callable(getattr(production, attribute, None)):
            return {"guard_helper_missing": attribute}
        originals[attribute] = getattr(production, attribute)

    def pinned_status_membership(card):
        # Pinned base behaviour: membership only, no string-type check.
        if card.get("agency_status") not in production.AGENCY_STATUSES:
            raise ValueError("card requires explicit agency_status")
        return card["agency_status"]

    def no_role_check(card):
        return card.get("role")

    replacements = {"status_type_guard": pinned_status_membership, "role_guard": no_role_check}
    phases = {"names": names}
    for bypass, attribute in guards.items():
        phase = {"bypassed_attribute": "{}.{}".format(production.__name__, attribute),
                 "other_guard_intact": [a for a in guards.values() if a != attribute]}
        path = out / "negative-{}.txt".format(bypass)
        try:
            setattr(production, attribute, replacements[bypass])
            phase["other_guard_identity_ok"] = all(getattr(production, a) is originals[a]
                                                   for a in phase["other_guard_intact"])
            outcome = run_names(focused, names, path)
            phase["variant_outcomes"] = variant_observations(focused, production.reconcile)
        finally:
            setattr(production, attribute, originals[attribute])
        summary = summarise(outcome, FOCUSED_MODULE + ".")
        detected = sorted(set(summary["failures"] + summary["errors"]))
        phase.update(summary=summary, detected=detected, expected_detectors=sorted(BYPASS_DETECTORS[bypass]),
                     positives_green=sorted(set(names) - set(detected)), output_sha256=sha256_file(path),
                     restored=getattr(production, attribute) is originals[attribute])
        phases[bypass] = phase
    path = out / "negative-restored-focused.txt"
    phases["restored_focused"] = summarise(run_names(focused, names, path), FOCUSED_MODULE + ".")
    controls = {}
    for module_name in ("tests.records.test_agency_reconciliation",
                        "tests.records.test_agency_reconciliation_regressions"):
        module = importlib.import_module(module_name)
        path = out / "negative-restored-{}.txt".format(module_name.rsplit(".", 1)[-1])
        controls[module_name] = summarise(run_suite(unittest.TestLoader().loadTestsFromModule(module), path))
    phases["restored_controls"] = controls
    return {"negative": phases}


CHILD_TASKS = {"unittest": task_unittest, "focused": task_focused, "negative": task_negative}


def guarded_child(spec_path):
    """Entry point of every application-importing child (run under -I -B)."""
    spec = json.loads(Path(spec_path).read_text())
    out = Path(spec["out_dir"])
    guard = Guard(spec["repo_root"], spec["isolated"], spec["isolated"]["TMPDIR"])
    guard.install()
    installed_state = guard.state()
    probes = inert_probes(guard)
    probe_hits = list(guard.hits)
    del guard.hits[:]
    del guard.temp_dirs[:]
    root = str(Path(spec["repo_root"]).resolve())
    sys.path.insert(0, root)
    data, error = {}, None
    try:
        data = CHILD_TASKS[spec["task"]](spec, out)
    except Exception:  # Recorded; the parent fails closed on it.
        error = traceback.format_exc()[-4000:]
    loaded, outside = {}, []
    for name, module in sorted(sys.modules.items()):
        path = getattr(module, "__file__", None)
        if name.split(".")[0] not in APPLICATION_ROOTS or path is None:
            continue
        loaded[name] = path
        if not within(path, root):
            outside.append(name)
    report = {"task": spec["task"], "label": spec["label"], "repo_root": str(REPO), "spec_repo_root": root,
              "interpreter": sys.executable, "isolated_flag": bool(sys.flags.isolated),
              "state_after_install": installed_state, "first_application_import": guard.first_import,
              "probes": probes, "probe_hits": probe_hits, "sentinel_hits": list(guard.hits),
              "allowed_children": guard.allowed_children, "temp_dirs_created": guard.temp_dirs,
              "loaded_application_modules": loaded, "modules_outside_root": outside,
              "task_error": error, "data": data}
    with open(spec["report_path"], "x") as handle:
        json.dump(report, handle, indent=2, sort_keys=True, default=str)
    return 0 if error is None else 1


# ----------------------------------------------------------------- parent side


def allowed_program(program):
    """Only the enumerated local children: this interpreter, its scanner, git."""
    program = str(program)
    return program in (sys.executable, str(Path(sys.executable).parent / "detect-secrets"), "git")


def run_child(run_dir, label, argv, cwd, env, stdout_path=None, timeout=900):
    """Run one argument-array child (no shell) and keep its raw streams."""
    record = {"label": label, "argv": [str(a) for a in argv], "cwd": str(cwd),
              "env_redirected": None if env is None else {k: env.get(k) for k in ISOLATED_VARS}}
    if not allowed_program(argv[0]):
        record.update(exit=None, launch_error="unexpected_subprocess_refused")
        return record
    out_file = Path(stdout_path) if stdout_path else run_dir / (label + ".stdout")
    err_file = run_dir / (label + ".stderr")
    try:
        with open(out_file, "xb") as out, open(err_file, "xb") as err:
            done = subprocess.run([str(a) for a in argv], cwd=str(cwd), stdout=out, stderr=err,
                                  stdin=subprocess.DEVNULL, env=env, timeout=timeout, check=False)
        record["exit"] = done.returncode
    except (OSError, subprocess.SubprocessError) as error:
        record["exit"] = None
        record["launch_error"] = type(error).__name__
    for key, path in (("stdout", out_file), ("stderr", err_file)):
        if path.is_file():
            record[key + "_path"] = str(path)
            record[key + "_sha256"] = sha256_file(path)
    return record


def git(*args, binary=False, root=None):
    # Local read-only git on the candidate; no network subcommands are used.
    argv = ["git", "-C", str(root or REPO)] + list(args)
    if not allowed_program(argv[0]):
        raise RuntimeError("unexpected_subprocess_refused")
    done = subprocess.run(argv, capture_output=True, timeout=120, check=True)
    return done.stdout if binary else done.stdout.decode().strip()


def git_identity():
    identity = {}
    for name, args in (("head", ("rev-parse", "HEAD")),
                       ("status_porcelain", ("status", "--porcelain", "--untracked-files=all"))):
        try:
            identity[name] = git(*args)
        except (OSError, subprocess.SubprocessError, RuntimeError) as error:
            identity[name] = None
            identity[name + "_error"] = type(error).__name__
    identity["clean"] = identity.get("status_porcelain") == ""
    return identity


def baseline_commit():
    """Pinned base = parent of the commit that first added the focused test.

    Derived from history so no high-entropy literal sits in source; run.json
    records the resolved commit for comparison with the frozen brief pin.
    """
    added = [line for line in git("log", "--format=%H", "--diff-filter=A", "HEAD", "--", FOCUSED_PATH).split() if line]
    if not added:
        raise RuntimeError("focused_test_commit_not_found")
    return git("rev-parse", "--verify", added[-1] + "^1")


def change_scope(result):
    """Every file changed since the base (committed or not) must be an allowed path."""
    base = baseline_commit()
    committed = [n for n in git("diff", "--name-only", base, "HEAD").splitlines() if n]
    status = git("status", "--porcelain", "-z", "--untracked-files=all", binary=True).decode()
    pending = [entry[3:] for entry in status.split("\0") if len(entry) > 3]
    changed = sorted(set(committed + pending))
    outside = [name for name in changed if name not in ALLOWED_CHANGES]
    result["change_scope"] = {"base_commit": base, "base_selector": BASE_SELECTOR, "changed_files": changed,
                              "outside_allowed": outside,
                              "base_production_sha256": sha256_bytes(git("show", base + ":" + PRODUCTION, binary=True))}
    return ["files_changed_outside_allowed_scope"] if outside else []


def install_parent_guards(run_dir):
    """The parent imports no application code; it still runs offline with a per-run temp root."""
    temp = Path(run_dir) / "parent-isolated" / "tmp"
    temp.mkdir(parents=True)
    tempfile.tempdir = str(temp)
    hits = []

    def refuse(name):
        def blocked(*_args, **_kwargs):
            hits.append(name)
            raise PermissionError("offline sentinel: " + name)
        blocked.fc3_sentinel = name
        return blocked
    for owner, attribute in ((socket.socket, "connect"), (socket.socket, "connect_ex"), (socket.socket, "bind"),
                             (socket, "create_connection"), (socket, "getaddrinfo")):
        setattr(owner, attribute, refuse(attribute))
    return {"temp_root": str(temp), "hits": hits}


def parent_state(parent):
    application = sorted(name for name in sys.modules if name.split(".")[0] in APPLICATION_ROOTS)
    return {"temp_root": parent["temp_root"], "tempfile_gettempdir": tempfile.gettempdir(),
            "gettempdir_under_run": within(tempfile.gettempdir(), parent["temp_root"]),
            "socket_guarded": all(hasattr(getattr(socket.socket, a), "fc3_sentinel") for a in ("connect", "bind"))
            and hasattr(socket.getaddrinfo, "fc3_sentinel"),
            "sentinel_hits": list(parent["hits"]), "application_modules_in_parent": application}


def refusal_table():
    """Inert parent-side regression of the pure child-specification check (nothing runs)."""
    temp = Path("/") / "fc3-runner-temp-root"
    interpreter, checker = "/fc3/interpreter", "/fc3/repo/" + SECRET_CHECKER
    allowed = ([interpreter, checker, str(temp / "case" / "report.json")], (), {})
    refused = {
        "positional_popen_options": ([interpreter, checker, str(temp / "r.json")], (0,), {}),
        "shell_requested": ([interpreter, checker, str(temp / "r.json")], (), {"shell": True}),
        "executable_override": ([interpreter, checker, str(temp / "r.json")], (), {"executable": "/fc3/other"}),
        "explicit_child_environment": ([interpreter, checker, str(temp / "r.json")], (), {"env": {}}),
        "argv_not_argument_array": (interpreter + " " + checker, (), {}),
        "argv_shape": ([interpreter, checker], (), {}),
        "program_not_admitted_interpreter": (["/fc3/other", checker, str(temp / "r.json")], (), {}),
        "script_not_enumerated_checker": ([interpreter, "/fc3/repo/tools/other.py", str(temp / "r.json")], (), {}),
        "report_outside_per_run_temp_root": ([interpreter, checker, "/fc3/elsewhere/r.json"], (), {}),
    }
    rows = [{"case": "allowed", "expected": None,
             "actual": child_spec_refusal(allowed[0], allowed[1], allowed[2], interpreter, checker, temp)}]
    for expected, (args, positional, kwargs) in refused.items():
        rows.append({"case": expected, "expected": expected,
                     "actual": child_spec_refusal(args, positional, kwargs, interpreter, checker, temp)})
    return rows


def launch_guarded(run_dir, result, label, task, root, extra=None):
    """Launch one guarded child of ``root``'s runner and return (record, report, problems)."""
    base = run_dir / (label + "-isolated")
    env = isolated_env(base)
    report_path = run_dir / (label + "-report.json")
    out_dir = run_dir / (label + "-outputs")
    out_dir.mkdir()
    spec = {"task": task, "label": label, "repo_root": str(Path(root).resolve()), "out_dir": str(out_dir),
            "report_path": str(report_path), "isolated": isolated_dirs(base)}
    spec.update(extra or {})
    spec_path = run_dir / (label + "-spec.json")
    with open(spec_path, "x") as handle:
        json.dump(spec, handle, indent=2, sort_keys=True)
    argv = [sys.executable, "-I", "-B", Path(root) / RUNNER_PATH, "--guarded-child", spec_path]
    record = run_child(run_dir, label, argv, root, env=env)
    result["children"].append(record)
    problems = [] if record.get("exit") == 0 else ["guarded_child_failed:" + label]
    if not report_path.is_file():
        return record, None, problems + ["guarded_child_report_missing:" + label]
    report = json.loads(report_path.read_text())
    record["report_path"], record["report_sha256"] = str(report_path), sha256_file(report_path)
    problems += verify_guard_report(label, spec, report)
    result.setdefault("guarded_children", {})[label] = {
        k: report.get(k) for k in ("repo_root", "isolated_flag", "first_application_import", "probes",
                                   "probe_hits", "sentinel_hits", "allowed_children", "temp_dirs_created",
                                   "modules_outside_root", "loaded_application_modules", "task_error")}
    return record, report, problems


def verify_guard_report(label, spec, report):
    problems = []
    temp_root = spec["isolated"]["TMPDIR"]
    tag = ":" + label
    if report.get("task_error"):
        problems.append("guarded_task_error" + tag)
    if report.get("repo_root") != spec["repo_root"] or report.get("spec_repo_root") != spec["repo_root"]:
        problems.append("child_repo_root_mismatch" + tag)
    if not report.get("isolated_flag"):
        problems.append("child_not_isolated_mode" + tag)
    first = report.get("first_application_import")
    if not first:
        problems.append("no_application_import_observed" + tag)
    for name, state in (("after_install", report.get("state_after_install")), ("first_import", first)):
        if not (state and state.get("gettempdir_under_temp_root") and within(state.get("tempfile_gettempdir"), temp_root)
                and state.get("environment_redirected") and not state.get("python_override_vars")
                and state.get("socket_guarded") and state.get("process_guarded")
                and state.get("model_import_blocker_installed") and state.get("temp_recorder_installed")):
            problems.append("guard_state_incomplete_{}{}".format(name, tag))
        elif any(not within(state["environment"][k], spec["out_dir"].rsplit("-outputs", 1)[0] + "-isolated")
                 for k in ISOLATED_VARS):
            problems.append("environment_outside_run_isolated_dir_{}{}".format(name, tag))
    probes = report.get("probes") or []
    expected = {"refused"}
    if len(probes) < 12 or any(p["outcome"] not in expected and p["outcome"] != "under_temp_root" for p in probes):
        problems.append("inert_probe_not_refused" + tag)
    if any(p["outcome"] == "refused" and not p.get("hit_recorded") for p in probes):
        problems.append("inert_probe_hit_not_recorded" + tag)
    if report.get("sentinel_hits"):
        problems.append("sentinel_triggered_during_task" + tag)
    if any(not d.get("under_temp_root") for d in report.get("temp_dirs_created") or []):
        problems.append("temporary_directory_escaped_run_root" + tag)
    for argv in report.get("allowed_children") or []:
        if child_spec_refusal(argv, (), {}, report.get("interpreter"), Path(spec["repo_root"]) / SECRET_CHECKER,
                              temp_root):
            problems.append("unexpected_child_recorded_as_allowed" + tag)
    if report.get("modules_outside_root") or not report.get("loaded_application_modules"):
        problems.append("application_modules_loaded_outside_root" + tag)
    return problems


def verify_unittest(label, unit, prefix_required=None):
    problems = []
    if not unit:
        return ["unittest_record_missing:" + label]
    ids, outcomes = unit["loaded_ids"], unit["outcomes"]
    if unit["tests_run"] <= 0 or not ids:
        problems.append("no_tests_executed:" + label)
    if len(ids) != len(set(ids)) or sorted(ids) != sorted(outcomes) or unit["tests_run"] != len(ids):
        problems.append("executed_ids_differ_from_loaded_suite:" + label)
    if not unit["successful"] or any(v != "ok" for v in outcomes.values()):
        problems.append("non_ok_tests:" + label)
    if unit["skipped"] or unit["expected_failures"] or unit["unexpected_successes"]:
        problems.append("unexpected_skips_or_expected_failures:" + label)
    return problems


def case_coverage(outcomes, problems, prefix=""):
    coverage = {}
    for case, cls in CASES.items():
        ids = sorted(i for i in outcomes if i.startswith(FOCUSED_MODULE + "." + cls + "."))
        coverage[case] = {"class": cls, "tests": ids, "all_ok": bool(ids) and all(outcomes[i] == "ok" for i in ids)}
        if not coverage[case]["all_ok"]:
            problems.append(prefix + "case_not_covered_or_not_green:" + case)
    return coverage


# ----------------------------------------------------------------- checks


def check_focused(run_dir, result):
    base = result["change_scope"]["base_commit"]
    base_source = run_dir / "baseline-source" / "agency_reconciliation.py.base"
    base_source.parent.mkdir()
    with open(base_source, "xb") as handle:
        handle.write(git("show", base + ":" + PRODUCTION, binary=True))
    _record, report, problems = launch_guarded(
        run_dir, result, "focused", "focused", REPO,
        {"targets": [FOCUSED_MODULE], "base_commit": base, "base_source_path": str(base_source)})
    if report is None:
        return problems
    data = report.get("data") or {}
    unit = data.get("unittest")
    problems += verify_unittest("focused", unit)
    result["focused_unittest"] = unit
    result["case_coverage"] = case_coverage((unit or {}).get("outcomes", {}), problems)
    if report.get("allowed_children"):
        problems.append("focused_launched_children")
    diff = data.get("baseline_differential")
    result["baseline_differential"] = diff
    if not diff:
        return problems + ["baseline_differential_incomplete"]
    if diff["tests_run"] != len(diff["names"]) or diff["skipped"]:
        problems.append("baseline_differential_incomplete_run")
    if diff["base_red"] != sorted(BASELINE_RED):
        problems.append("baseline_red_set_differs_from_frozen_regression")
    if not diff["restored"]:
        problems.append("focused_reconcile_not_restored")
    for row in diff["candidate_variant_outcomes"]:
        expected = "ValueError:" + (diff["status_message"] if row["field"] == "agency_status" else diff["role_message"])
        if row["outcome"] != expected:
            problems.append("candidate_variant_not_rejected:{}:{}".format(row["field"], row["variant"]))
    raw = [row["outcome"] for row in diff["base_variant_outcomes"]]
    if not any(o.startswith("TypeError") for o in raw) or not any(o.startswith("returned") for o in raw):
        problems.append("baseline_defect_not_reproduced")
    return problems


def check_compatibility(run_dir, result):
    _record, report, problems = launch_guarded(run_dir, result, "compatibility", "unittest", REPO,
                                               {"targets": list(COMPAT_SELECTORS)})
    if report is None:
        return problems
    unit = (report.get("data") or {}).get("unittest")
    problems += verify_unittest("compatibility", unit)
    result["compatibility_unittest"] = unit
    per_selector = {}
    for selector in COMPAT_SELECTORS:
        ids = [i for i in (unit or {}).get("outcomes", {}) if i.startswith(selector + ".")]
        per_selector[selector] = {"tests": len(ids), "non_ok": [i for i in ids if unit["outcomes"][i] != "ok"]}
        if not ids:
            problems.append("selector_executed_no_tests:" + selector)
    result["per_selector"] = per_selector
    # The temp-using selectors must have created their fixtures under the per-run root,
    # and the checker selector must have used only its enumerated local child.
    temp_dirs = report.get("temp_dirs_created") or []
    if not temp_dirs:
        problems.append("compatibility_temp_fixtures_not_observed")
    if not report.get("allowed_children"):
        problems.append("compatibility_checker_child_not_observed")
    result["compatibility_isolation"] = {"temp_dirs_created": len(temp_dirs),
                                         "all_under_run_temp_root": all(d["under_temp_root"] for d in temp_dirs),
                                         "allowed_checker_children": len(report.get("allowed_children") or []),
                                         "temp_using_selectors": list(COMPAT_TEMP_USERS),
                                         "checker_selector": COMPAT_CHECKER_USER}
    result["full_discovery"] = "operator-run (not part of this runner)"
    return problems


def check_negative(run_dir, result):
    before_disk = source_hashes(REPO)
    _record, report, problems = launch_guarded(run_dir, result, "negative", "negative", REPO)
    after_disk = source_hashes(REPO)
    result["source_unchanged_on_disk"] = before_disk == after_disk
    if before_disk != after_disk:
        problems.append("source_changed_on_disk")
    if report is None:
        return problems
    data = report.get("data") or {}
    if "guard_helper_missing" in data:
        return problems + ["guard_helper_missing:" + data["guard_helper_missing"]]
    phases = data.get("negative")
    result["negative"] = phases
    if not phases:
        return problems + ["negative_phases_missing"]
    names = phases["names"]
    for bypass in BYPASS_DETECTORS:
        phase = phases[bypass]
        summary = phase["summary"]
        if summary["tests_run"] != len(names) or summary["skipped"]:
            problems.append("mutant_run_incomplete:" + bypass)
        if phase["detected"] != phase["expected_detectors"]:
            problems.append("mutant_detection_differs:" + bypass)
        if not phase["other_guard_identity_ok"]:
            problems.append("other_guard_not_intact:" + bypass)
        if not phase["restored"]:
            problems.append("guard_not_restored:" + bypass)
        raw = [row["outcome"] for row in phase["variant_outcomes"]]
        if not any(o.startswith("TypeError") or o.startswith("returned") for o in raw):
            problems.append("mutant_raw_behaviour_not_observed:" + bypass)
    restored = phases["restored_focused"]
    if not restored["successful"] or restored["tests_run"] != len(names) or restored["skipped"]:
        problems.append("restored_focused_not_green")
    for name, control in phases["restored_controls"].items():
        if not control["successful"] or control["tests_run"] <= 0 or control["skipped"]:
            problems.append("restored_control_not_green:" + name)
    return problems


def static_inspection(root):
    findings = []
    for relative in STATIC_SCANNED:
        for number, line in enumerate((root / relative).read_text().splitlines(), 1):
            if any(token in line for token in BOX_SPECIFIC):
                findings.append({"file": relative, "line": number, "kind": "box_specific_path"})
            if any(pattern.search(line) for pattern in INLINE_FORMS):
                findings.append({"file": relative, "line": number, "kind": "inline_or_shell_form"})
    return findings


def candidate_files():
    names = git("ls-files", "-z", "--cached", "--others", "--exclude-standard", binary=True).decode().split("\0")
    return sorted({name for name in names if name})


def check_portability(run_dir, result):
    problems = []
    findings = static_inspection(REPO)
    result["static_inspection"] = {"files": list(STATIC_SCANNED), "findings": findings}
    if findings:
        problems.append("static_box_specific_or_inline_findings")
    # Retained second copy at a different absolute prefix (never removed).
    copy_root = run_dir / "relocated-prefix" / "second-copy"
    copied = 0
    for name in candidate_files():
        source = REPO / name
        if source.is_symlink() or not source.is_file():
            continue
        target = copy_root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied += 1
    copy_hashes = source_hashes(copy_root)
    result["portability_copy"] = {"prefix": str(copy_root), "files_copied": copied,
                                  "source_hashes_match": copy_hashes == result["source_hashes"]}
    if copy_hashes != result["source_hashes"]:
        problems.append("copy_hashes_differ")
    _record, report, child_problems = launch_guarded(run_dir, result, "portability", "unittest", copy_root,
                                                     {"targets": [FOCUSED_MODULE]})
    problems += child_problems
    if report is None:
        return problems
    unit = (report.get("data") or {}).get("unittest")
    problems += verify_unittest("portability", unit)
    result["portability_unittest"] = unit
    result["portability_case_coverage"] = case_coverage((unit or {}).get("outcomes", {}), problems, "portability_")
    if report.get("repo_root") != str(copy_root.resolve()):
        problems.append("child_did_not_use_copy")
    if any(not within(path, copy_root) for path in (report.get("loaded_application_modules") or {}).values()):
        problems.append("application_modules_loaded_outside_copy")
    return problems


def check_credentials(run_dir, result):
    problems = []
    scanner = Path(sys.executable).parent / "detect-secrets"
    result["scanner"] = {"path": str(scanner), "present": scanner.is_file()}
    if not scanner.is_file():
        return ["detect_secrets_unavailable_adjacent_to_interpreter"]
    env = isolated_env(run_dir / "credentials-isolated")
    report = run_dir / "secret-scan.json"
    scan = run_child(run_dir, "detect-secrets-scan",
                     [scanner, "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"],
                     REPO, env=env, stdout_path=report)
    result["children"].append(scan)
    result["children"].append(run_child(run_dir, "detect-secrets-version", [scanner, "--version"], REPO, env=env))
    if scan.get("exit") != 0:
        problems.append("secret_scan_failed")
    try:
        data = json.loads(report.read_text())
        if not isinstance(data.get("results"), dict):
            raise ValueError("results")
        result["secret_scan"] = {"files_with_hits": len(data["results"]),
                                 "raw_hits": sum(len(v) for v in data["results"].values()),
                                 "hits_in_changed_files": sorted(p for p in data["results"] if p in ALLOWED_CHANGES)}
    except (OSError, ValueError, AttributeError):
        return problems + ["secret_report_malformed"]
    checker = run_child(run_dir, "check-secret-report",
                        [sys.executable, "-B", REPO / SECRET_CHECKER, report], REPO, env=env)
    result["children"].append(checker)
    text = Path(checker["stdout_path"]).read_text(errors="replace") if "stdout_path" in checker else ""
    match = re.search(r"potential secrets: (\d+);", text)
    result["secret_scan"]["potential_secrets"] = int(match.group(1)) if match else None
    if checker.get("exit") != 0:
        problems.append("check_secret_report_failed")
    if result["secret_scan"]["potential_secrets"] != 0:
        problems.append("potential_secrets_nonzero_or_unreported")
    return problems


def check_public_tree(run_dir, result):
    env = isolated_env(run_dir / "public-tree-isolated")
    record = run_child(run_dir, "check-public-tree", [sys.executable, "-B", REPO / "tools" / "check_public_tree.py"],
                       REPO, env=env)
    result["children"].append(record)
    return [] if record.get("exit") == 0 else ["public_tree_check_failed"]


HANDLERS = {"focused": check_focused, "compatibility": check_compatibility, "negative": check_negative,
            "portability": check_portability, "credentials": check_credentials, "public-tree": check_public_tree}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", choices=CHECKS)
    parser.add_argument("--output-root")
    parser.add_argument("--guarded-child", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.guarded_child:
        if args.check or args.output_root:
            parser.error("child mode takes no other arguments")
        return guarded_child(args.guarded_child)
    if not args.check or not args.output_root:
        parser.error("--check and --output-root are required")
    run_dir = new_run_dir(args.output_root, args.check)
    parent = install_parent_guards(run_dir)
    result = {"schema": "fc3-agency-shapes-acceptance-v2", "card": CARD, "check": args.check,
              "argv": [sys.executable] + list(sys.argv), "cwd": os.getcwd(), "interpreter": sys.executable,
              "python_version": sys.version, "repo_root": str(REPO), "run_dir": str(run_dir),
              "started_utc": now(), "git": git_identity(), "source_hashes": source_hashes(REPO),
              "parent_guard_at_start": parent_state(parent), "children": [],
              "acceptance": "REVIEW REQUIRED; machine validity only, not semantic/legal acceptance"}
    try:
        problems = change_scope(result)
        table = refusal_table()
        result["child_spec_refusal_table"] = table
        if any(row["actual"] != row["expected"] for row in table):
            problems.append("child_spec_refusal_table_mismatch")
        problems += HANDLERS[args.check](run_dir, result)
    except Exception as error:  # Incomplete check: record the class, fail closed.
        problems = [("check_incomplete:" + type(error).__name__)]
        result["exception"] = repr(error)[:2000]
    result["parent_guard_at_end"] = end = parent_state(parent)
    if end["application_modules_in_parent"] or end["sentinel_hits"] or not end["gettempdir_under_run"] \
            or not end["socket_guarded"]:
        problems.append("parent_isolation_violated")
    result["finished_utc"] = now()
    result["problems"] = problems
    result["verdict"] = "PASS_REVIEW_REQUIRED" if not problems else "FAIL"
    with open(run_dir / "run.json", "x") as handle:
        json.dump(result, handle, indent=2, sort_keys=True, default=str)
    print(json.dumps({"check": args.check, "verdict": result["verdict"], "problems": problems,
                      "run_dir": str(run_dir), "head": result["git"].get("head"), "clean": result["git"].get("clean"),
                      "run_json_sha256": sha256_file(run_dir / "run.json")}, sort_keys=True))
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
