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
directory and, all before any application module is imported, arm sentinels:
an audit hook refuses Python-level file writes/renames/removals and sqlite
opens outside the run directory, and file/directory reads outside the run
directory, the admitted repository source and explicit runtime roots; every
child/model execution entry point (subprocess, os.exec*/spawn*/system/fork,
plus the matching audit events) and socket connects are refused. Inert
self-check controls (a generated run-local stand-in for a live file and
mocked child endpoints) must each be refused with their exact registered
identity and phase; any other refusal is fatal even if caught by the code
under test. The repository tree is content hashed before and after each
case. These are regression controls, not a sandbox. PASS means REVIEW
REQUIRED, never semantic or release acceptance.
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
GUARD_HITS = []        # every unexpected refusal (write/read/child/network): always fatal
WRITE_HITS = []        # the unexpected write subset, kept separately in the record
PROBE_HITS = []        # refusals that exactly matched an expected self-check probe
EXPECTED_PROBES = {}   # (kind, target) -> phase in which that one probe is expected
STATE = {"armed": False, "phase": "pre-arm"}
FORBIDDEN_CONTROL = "control-forbidden"
CHILD_ENTRY_POINTS = {
    "subprocess": ("run", "call", "check_call", "check_output", "getoutput", "getstatusoutput"),
    "os": ("system", "popen", "execv", "execve", "execl", "execle", "execlp", "execlpe", "execvp",
           "execvpe", "spawnv", "spawnve", "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnvp",
           "spawnvpe", "posix_spawn", "posix_spawnp", "fork", "forkpty"),
}
CHILD_AUDIT_EVENTS = ("subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn",
                      "os.fork", "os.forkpty", "pty.spawn")
CHILD_ORIGINALS = {}
ORIGINAL_POPEN = subprocess.Popen


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


class InertChildReached(Exception):
    """Raised by inert doubles placed behind the child guard during self-check only."""


def refuse_hit(kind, target):
    """Record a refusal and raise. A hit is expected only if its exact (kind, target) was
    registered for the current phase; every other hit stays fatal even if the caller catches."""
    hit = {"kind": kind, "target": target, "phase": STATE["phase"]}
    if EXPECTED_PROBES.get((kind, target)) == STATE["phase"]:
        del EXPECTED_PROBES[(kind, target)]
        PROBE_HITS.append(hit)
    else:
        GUARD_HITS.append(hit)
        if kind == "write":
            WRITE_HITS.append(target)
    raise PermissionError("offline acceptance guard refused " + kind + ": " + target)


def child_target(label, args, kwargs):
    first = args[0] if args else kwargs.get("args", kwargs.get("path", ""))
    if isinstance(first, int) and len(args) > 1:  # os.spawn*(mode, file, ...)
        first = args[1]
    if isinstance(first, (list, tuple)):
        first = first[0] if first else ""
    try:
        text = os.fsdecode(first)
    except TypeError:
        text = type(first).__name__
    return label + ":" + text


def guarded_child(label):
    def guarded(*args, **kwargs):
        if STATE["armed"]:
            refuse_hit("child", child_target(label, args, kwargs))
        return CHILD_ORIGINALS[label](*args, **kwargs)
    guarded.__name__ = label.rsplit(".", 1)[1]
    guarded.__qualname__ = guarded.__name__
    return guarded


class GuardedPopen(ORIGINAL_POPEN):
    def __init__(self, *args, **kwargs):
        if STATE["armed"]:
            self._child_created = False  # keeps Popen.__del__ quiet on refusal
            refuse_hit("child", child_target("subprocess.Popen", args, kwargs))
        super().__init__(*args, **kwargs)


def install_child_guards():
    """Python-level refusal of every child/model execution entry point while armed."""
    modules = {"subprocess": subprocess, "os": os}
    for module_name, names in CHILD_ENTRY_POINTS.items():
        module = modules[module_name]
        for name in names:
            label = module_name + "." + name
            if hasattr(module, name) and label not in CHILD_ORIGINALS:
                CHILD_ORIGINALS[label] = getattr(module, name)
                setattr(module, name, guarded_child(label))
    subprocess.Popen = GuardedPopen


def install_offline_guards():
    def refuse(name):
        def guard(*args, **kwargs):
            refuse_hit("network", name)
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


READ_EVENTS = {"os.listdir": 0, "os.scandir": 0, "shutil.copyfile": 0, "shutil.copytree": 0}


def under(path, base):
    return path == base or path.startswith(base.rstrip(os.sep) + os.sep)


def runtime_read_roots():
    """Explicit interpreter/runtime roots: the venv prefix and this interpreter's scheme paths."""
    import sysconfig
    roots = {os.path.realpath(sys.prefix), os.path.realpath(sys.exec_prefix)}
    for key in ("stdlib", "platstdlib", "purelib", "platlib"):
        value = sysconfig.get_paths().get(key)
        if value:
            roots.add(os.path.realpath(value))
    return sorted(roots)


def install_write_guard(run):
    """Install every pre-import sentinel, then prove each with inert controls.

    While armed, an audit hook refuses (before the syscall) file writes and sqlite opens
    outside the run root, and file/directory reads outside the run root, the admitted
    repository source and the explicit runtime roots. The run-local control-forbidden
    directory is outside both, so it acts as a generated stand-in for a live file. Every
    child/model execution entry point is refused at Python level (plus the audit events
    as a second layer) and sockets are refused. Self-check probes are registered by exact
    (kind, target, phase); any other refusal is recorded as unexpected and is fatal."""
    from unittest import mock
    root = os.path.realpath(run)
    forbidden = os.path.join(root, FORBIDDEN_CONTROL)
    os.mkdir(forbidden, 0o700)
    forbidden_file = os.path.join(forbidden, "synthetic-live-record.txt")
    with open(forbidden_file, "w") as handle:
        handle.write("generated synthetic stand-in for a live file; must never be read\n")
    read_roots = sorted({root, os.path.realpath(REPO), *runtime_read_roots()})

    def resolve(target, dir_fd=None):
        text = os.fsdecode(target)
        if text.startswith("file:"):
            text = unquote(urlsplit(text).path)
        if isinstance(dir_fd, int) and dir_fd >= 0 and not os.path.isabs(text):
            try:
                text = os.path.join(os.readlink("/proc/self/fd/" + str(dir_fd)), text)
            except OSError:
                return None
        return os.path.realpath(text)

    def allowed(kind, target, dir_fd=None):
        if isinstance(target, int):
            return True  # an already opened descriptor
        if os.fsdecode(target) in ("", ":memory:", os.devnull):
            return True
        resolved = resolve(target, dir_fd)
        if resolved is None or under(resolved, forbidden):
            return False
        if kind == "write":
            return under(resolved, root)
        return resolved == os.devnull or any(under(resolved, base) for base in read_roots)

    def display(target):
        return str(target) if isinstance(target, int) else os.fsdecode(target)

    def hook(event, args):
        if not STATE["armed"]:
            return
        checks = []
        if event == "open":
            path, mode, flags = (tuple(args) + (None, None, None))[:3]
            if path is None:
                return
            writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                isinstance(flags, int) and flags & WRITE_FLAGS)
            checks.append(("write" if writing else "read", path, None))
        elif event in WRITE_EVENTS or event in READ_EVENTS:
            if event in READ_EVENTS and READ_EVENTS[event] < len(args):
                source = args[READ_EVENTS[event]]
                checks.append(("read", "." if source is None else source, None))
            for p, d in WRITE_EVENTS.get(event, ()):
                if p < len(args) and args[p] is not None:
                    checks.append(("write", args[p], args[d] if d is not None and d < len(args) else None))
        elif event in CHILD_AUDIT_EVENTS:
            values = tuple(args)
            if event == "os.spawn":
                values = values[1:]
            elif event == "subprocess.Popen" and values and values[0] is None:
                values = values[1:]
            refuse_hit("child", child_target(event, values, {}))
        for kind, target, dir_fd in checks:
            if not allowed(kind, target, dir_fd):
                refuse_hit(kind, display(target))

    sys.addaudithook(hook)
    install_child_guards()
    install_offline_guards()
    STATE["armed"] = True
    STATE["phase"] = "guard-self-check"
    try:
        controls = guard_self_check(run, forbidden, forbidden_file, mock)
    finally:
        STATE["phase"] = "case"
    leftover = sorted(target for (_, target) in EXPECTED_PROBES)
    EXPECTED_PROBES.clear()
    ok = all(controls.values()) and not leftover and not GUARD_HITS
    return {"root": root, "read_roots": read_roots, "forbidden_control": forbidden,
            "child_entry_points": sorted(CHILD_ORIGINALS) + ["subprocess.Popen"],
            "child_audit_events": list(CHILD_AUDIT_EVENTS), "controls": controls,
            "unconsumed_expected_probes": leftover, "self_check_ok": ok}


def expect_refusal(kind, target, action):
    """Run one inert probe that must be refused with exactly this (kind, target, phase)."""
    before = len(PROBE_HITS)
    EXPECTED_PROBES[(kind, target)] = STATE["phase"]
    try:
        action()
        refused = False
    except PermissionError:
        refused = True
    except (InertChildReached, OSError):
        refused = False
    consumed = (kind, target) not in EXPECTED_PROBES
    EXPECTED_PROBES.pop((kind, target), None)
    matched = (len(PROBE_HITS) == before + 1 and PROBE_HITS[-1]["kind"] == kind
               and PROBE_HITS[-1]["target"] == target)
    return refused and consumed and matched


def guard_self_check(run, forbidden, forbidden_file, mock):
    token = secrets.token_hex(6)
    controls = {}
    escape = os.path.join(os.sep, "fa45-escape-" + token, "probe")
    controls["write_outside_run_refused"] = expect_refusal(
        "write", escape, lambda: open(escape, "w").close())
    forbidden_write = os.path.join(forbidden, "write-probe-" + token + ".txt")
    controls["write_into_forbidden_control_refused"] = expect_refusal(
        "write", forbidden_write, lambda: open(forbidden_write, "w").close())
    controls["read_generated_live_stand_in_refused"] = expect_refusal(
        "read", forbidden_file, lambda: open(forbidden_file).read())
    controls["list_generated_live_stand_in_refused"] = expect_refusal(
        "read", forbidden, lambda: os.listdir(forbidden))
    argv0 = "fa45-inert-child-" + token + "-NOT-EXECUTED"
    modules = {"subprocess": subprocess, "os": os}
    for label in sorted(CHILD_ORIGINALS):
        module_name, name = label.split(".", 1)
        if name in ("fork", "forkpty"):
            args, target = (), label + ":"
        elif name.startswith("spawn"):
            args, target = (os.P_WAIT, argv0, [argv0]), label + ":" + argv0
        elif module_name == "subprocess" and name not in ("getoutput", "getstatusoutput"):
            args, target = ([argv0],), label + ":" + argv0
        else:
            args, target = (argv0, [argv0]) if name.startswith(("exec", "posix_spawn")) else (argv0,), \
                label + ":" + argv0
        inert = mock.Mock(side_effect=InertChildReached(label))
        entry = getattr(modules[module_name], name)
        with mock.patch.dict(CHILD_ORIGINALS, {label: inert}):
            refused = expect_refusal("child", target, lambda: entry(*args))
        controls["child_refused:" + label] = refused and not inert.called
    with mock.patch.object(ORIGINAL_POPEN, "_execute_child", side_effect=InertChildReached("Popen")) as inner:
        refused = expect_refusal("child", "subprocess.Popen:" + argv0, lambda: subprocess.Popen([argv0]))
        controls["child_refused:subprocess.Popen"] = refused and not inner.called
    # Reviewer-shaped control: Popen replaced by an inert endpoint after the guard is installed.
    with mock.patch.object(subprocess, "Popen", side_effect=InertChildReached("endpoint")) as endpoint:
        refused = expect_refusal("child", "subprocess.run:" + argv0, lambda: subprocess.run([argv0]))
        controls["child_refused_before_patched_popen_endpoint"] = refused and not endpoint.called
    # Local AF_UNIX connect to a missing run-local path: inert even if the guard were absent.
    unix = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        missing = os.path.join(os.path.realpath(run), "no-such-socket-" + token)
        controls["socket_connect_refused"] = expect_refusal(
            "network", "socket.connect", lambda: unix.connect(missing))
    finally:
        unix.close()
    positive = Path(run) / "write-guard-positive.txt"
    positive.write_text("in-root write allowed\n")
    controls["run_local_write_and_read_allowed"] = positive.read_text() == "in-root write allowed\n"
    controls["admitted_source_read_allowed"] = len((REPO / PRODUCT).read_bytes()) > 0
    controls["runtime_read_allowed"] = len(Path(json.__file__).read_bytes()) > 0
    controls["no_unexpected_hits_during_self_check"] = not GUARD_HITS
    return controls


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
                      "write_guard": "audit hook, run root only",
                      "read_guard": "audit hook, run root + admitted source + runtime roots only",
                      "child_guard": sorted(CHILD_ORIGINALS) + ["subprocess.Popen"],
                      "bytecode": sys.dont_write_bytecode},
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
            if not guard["self_check_ok"]:
                raise RuntimeError("guard_self_check_failed")
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
    STATE["armed"] = False  # the case is over; identity() below runs git again
    STATE["phase"] = "post-case"
    after = identity()
    if after != before:
        ok = False
        details["stop"] = "candidate_identity_changed_during_run"
    if WRITE_HITS:
        ok = False
        details["stop"] = "write_outside_run_root_refused"
    if GUARD_HITS:  # fatal even when the code under test caught the refusal
        ok = False
        details.setdefault("stop", "unexpected_guard_hit")
    if isinstance(guard, dict) and "controls" in guard and not guard.get("self_check_ok"):
        ok = False
    guard["unexpected_hits"] = list(GUARD_HITS)
    guard["refused_writes"] = list(WRITE_HITS)
    guard["expected_probe_hits"] = list(PROBE_HITS)
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
