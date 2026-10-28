"""Acceptance runner for FC4-ROW-PAGE-STATE (offline, synthetic, review-required).

Usage (one check per invocation):
    <interpreter> tools/accept_fc4_row_page_state.py --check CHECK --output-root DIR

CHECK is one of: focused, compatibility, negative, portability, credentials,
public-tree. Each invocation creates a fresh unique child directory under DIR
(never overwriting earlier evidence) holding raw child stdout/stderr, argv,
cwd, exit codes, executed test counts/IDs and source hashes in ``run.json``.
The process exits 0 only when every assertion of the selected check holds.

The repository root is derived from this file. Import search paths are set
only inside this script. Children are literal argument arrays without a shell;
the interpreter is the one running this script. A passing result is machine
validity only: REVIEW REQUIRED, never semantic/legal/stage acceptance,
activation or publication authority.

Frozen brief binding. The frozen brief lives outside the public tree in a
``frozen-brief`` directory beside the repository root (the candidate root).
Every receipt records the full SHA-256 and size of each frozen brief file and
the pinned values below. A missing or mismatched brief fails the run, and each
run qualifies that gate with inert missing/mutated copies inside its own run
directory.

Offline guard (A4). Before any application or test module is imported, every
process here that imports application code installs ``OfflineGuard``. That
covers the acceptance parent and each guarded unittest child (focused,
compatibility final/baseline, portability). The guard does three things:
  * adds a permanent audit hook that refuses network events and process
    creation. The only processes allowed are the per-profile enumerated local
    argument arrays in ``process_allowed``.
  * swaps socket/subprocess/os entry points for sentinels, so the guard state
    can also be seen by checking those objects' identity.
  * registers an import observer that records whether the guard was active at
    the first application import.
Every sentinel event is recorded before it raises, so catching the exception
does not hide it: any recorded event fails the process and the acceptance
verdict. Each run that imports app code also proves this with a guarded child
that emits inert synthetic audit events (no socket or process is created),
catches them, and must still fail.
Closure inspection: after the tests, each guarded process scans, with ast,
every loaded application/test module for code that runs at import time and
reads environment, home, cwd or temp-dir defaults, or starts sockets or
processes. Any such finding fails the run. The only import-time bindings in the
measured closure are bound defaults ``runner=subprocess.run`` in the
OCR/vision/extraction_routes modules. The guard swaps in guarded wrappers for
subprocess.run and the related helpers before any import, so those defaults
bind the wrappers. A runtime check then walks every loaded function and method
default and fails if any original network/process callable is still bound.
That check is itself qualified inertly with a synthetic probe module. No
environment, home or temp-dir constant is bound at import, so no other
redirect applies. Guarded children also run with HOME/TMPDIR/XDG_* redirected
to a fresh directory inside the run directory.
Limits: these sentinels are not a hostile-code sandbox. Grandchild processes
are not guarded: the admitted compatibility CLI and scanner subprocesses and
the existing checker scripts run under their existing admission.
"""
import argparse
import ast
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
import types
import unittest

sys.dont_write_bytecode = True

RUNNER = Path(__file__).resolve()
REPO = RUNNER.parents[1]
CHECKS = ("focused", "compatibility", "negative", "portability", "credentials", "public-tree")
CARD = "FC4-ROW-PAGE-STATE"
SCHEMA = "fc4-row-page-state-acceptance-v2"
BASE_SELECTOR = "parent of the first commit adding tests/records/test_fc4_row_page_state.py"
# Pins expressed as byte tuples so no high-entropy literal appears in source.
PINNED_BASE = bytes((62, 26, 23, 108, 165, 217, 233, 41, 157, 120, 227, 197, 85, 6, 184, 155, 55, 99, 13,
                     86)).hex()
PINNED_BASE_ROWS_SHA256 = bytes((247, 139, 139, 141, 108, 196, 90, 230, 216, 141, 251, 177, 96, 70, 148, 196,
                                 83, 246, 12, 230, 42, 123, 242, 49, 111, 249, 226, 117, 200, 46, 241,
                                 231)).hex()
FROZEN_BRIEF_DIR_NAME = "frozen-brief"
FROZEN_BRIEF_VERSION = ("FC4-ROW-PAGE-STATE frozen brief v1: card body as dispatched (card-body.txt) and the "
                        "sealed astra-refill-FC-4 plan brief it embeds (plan.brief)")
FROZEN_BRIEF = {
    "card-body.txt": (18311, bytes((115, 19, 123, 81, 17, 209, 215, 44, 109, 79, 195, 36, 38, 212, 74, 216, 160,
                                    243, 30, 122, 23, 128, 168, 100, 42, 84, 191, 83, 40, 147, 157, 2))),
    "plan.brief": (17113, bytes((166, 61, 3, 116, 176, 207, 185, 198, 192, 62, 87, 60, 224, 177, 203, 100, 214,
                                 168, 158, 248, 145, 248, 38, 241, 178, 64, 5, 98, 254, 113, 232, 173))),
}
ROWS_PATH = "campaign_tool/records/understanding/rows.py"
FOCUSED_PATH = "tests/records/test_fc4_row_page_state.py"
FOCUSED_MODULE = "tests.records.test_fc4_row_page_state"
COMPAT_SELECTORS = ("tests.test_understanding_rows", "tests.records.test_understanding",
                    "tests.test_secret_report", "tests.test_public_tree")
# Frozen named-case coverage from the brief; each maps to exactly one test method.
FROZEN_CASES = ("ok_page_and_empty_page_inventory", "partial_blocked_unknown_page_status",
                "missing_and_container_page_status", "visual_review_true_and_malformed",
                "malformed_page_entry", "multiple_pages_one_not_clear", "top_level_hold_stays_hold",
                "rows_retained_for_triage", "aggregate_reason_no_payload", "publication_stays_false",
                "inputs_unchanged")
CASE_CLASS = "PageStateTests"
# Cases that already hold on the unchanged base (positives / preservation controls).
BASE_GREEN = {"ok_page_and_empty_page_inventory", "publication_stays_false", "inputs_unchanged"}
HASHED_SOURCES = (
    ROWS_PATH,
    FOCUSED_PATH,
    "tools/accept_fc4_row_page_state.py",
    "tests/test_understanding_rows.py",
    "campaign_tool/records/extraction_routes.py",
    "campaign_tool/records/native_extract.py",
    "tools/check_secret_report.py",
    "tools/check_public_tree.py",
)
STATIC_SCANNED = (FOCUSED_PATH, "tools/accept_fc4_row_page_state.py")
# Assembled from parts so this list does not match its own source text.
BOX_SPECIFIC = ("/ho" + "me/", "agent" + "-work", "venv" + "-fullsuite", "/Us" + "ers/", ".her" + "mes",
                "/t" + "mp/", "scr" + "atch")
INLINE_FORMS = (
    re.compile(r"""["']-""" + r"""[ce]["']"""),
    re.compile("shell" + r"\s*=\s*True"),
    re.compile("PYTHON" + "PATH"),
    re.compile(r"\bos\.sys" + r"tem\("),
)
RAN = re.compile(r"^Ran (\d+) tests? in ", re.M)
SUMMARY = re.compile(r"^(OK|FAILED)(?: \(([^)]*)\))?\s*$", re.M)
VERBOSE = re.compile(r"^(test\w*) \(([\w.]+)\)(?:\n[^\n]*?)? \.\.\. (ok|FAIL|ERROR|skipped.*|expected failure|unexpected success)$",
                     re.M)
APP_IMPORTING_CHECKS = ("focused", "compatibility", "negative")
PARENT_RUN_DIR = None


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    return sha256_bytes(Path(path).read_bytes())


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


# ------------------------------------------------------------ offline guard

APP_ROOTS = ("campaign_tool", "tests", "tools")
NETWORK_EVENTS = frozenset((
    "socket.connect", "socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr",
    "socket.getnameinfo", "socket.sendto", "socket.sendmsg", "socket.bind", "urllib.Request",
    "http.client.connect", "ftplib.connect", "smtplib.connect", "poplib.connect", "imaplib.open",
    "nntplib.connect", "telnetlib.Telnet.open", "webbrowser.open"))
PROCESS_EVENTS = frozenset(("os.system", "os.exec", "os.fork", "os.forkpty", "os.posix_spawn", "os.spawn",
                            "os.startfile", "pty.spawn"))
MODEL_CLIENTS = ("openai", "anthropic", "ollama", "litellm", "httpx", "requests", "urllib3",
                 "google.generativeai", "transformers", "torch")
REDIRECTED_ENV = ("HOME", "TMPDIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME")
GIT_SUBCOMMANDS = frozenset(("rev-parse", "status", "log", "show", "diff", "ls-files", "clone", "checkout"))
PROCESS_HELPERS = ("run", "call", "check_call", "check_output", "getoutput", "getstatusoutput")
SENTINEL_MARK = "fc4_offline_sentinel"


def _argv_list(args, executable=None):
    if isinstance(args, (str, bytes, os.PathLike)):
        argv = [os.fsdecode(args)]
    else:
        try:
            argv = [os.fsdecode(a) if isinstance(a, (bytes, os.PathLike)) else str(a) for a in args]
        except TypeError:
            argv = [repr(args)]
    if executable is not None and not argv:
        argv = [os.fsdecode(executable)]
    return argv


def _git_subcommand(argv):
    rest = argv[1:]
    while rest[:1] == ["-C"]:
        rest = rest[2:]
    return rest[0] if rest else None


def _script_of(argv):
    rest = argv[1:]
    while rest and rest[0] in ("-B", "-I"):
        rest = rest[1:]
    return (Path(rest[0]).resolve(), rest[1:]) if rest and not rest[0].startswith("-") else (None, rest)


def process_allowed(profile, root, argv):
    """Enumerated local subprocess allowances per guard profile (anything else is fatal)."""
    if not argv:
        return False
    exe = sys.executable
    if profile == "compatibility":
        # Exactly the subprocesses the unchanged admitted selectors start.
        if argv[0] == "git":
            return argv[1:2] == ["ls-files"]
        if argv[0] == exe:
            if argv[1:4] == ["-B", "-m", "campaign_tool.records.understanding"]:
                return True
            return len(argv) == 3 and Path(argv[1]).resolve() == root / "tools" / "check_secret_report.py"
        return False
    if profile == "parent":
        if argv[0] == "git":
            return _git_subcommand(argv) in GIT_SUBCOMMANDS
        if argv[0] == exe:
            script, _ = _script_of(argv)
            if script in (RUNNER, REPO / "tools" / "check_secret_report.py", REPO / "tools" / "check_public_tree.py"):
                return True
            # The portability check's relocated, byte-identical copy of this runner inside the run directory.
            return (script is not None and PARENT_RUN_DIR is not None and script.name == RUNNER.name
                    and script.is_relative_to(PARENT_RUN_DIR) and script.is_file()
                    and sha256_file(script) == sha256_file(RUNNER))
        if argv[0] == str(Path(exe).parent / "detect-secrets"):
            return argv[1:2] in (["scan"], ["--version"])
        return False
    return False  # focused / portability children: no subprocess at all.


class _FirstAppImport:
    """meta_path observer: records guard state at the first application import; never loads anything."""

    def __init__(self, guard):
        self.guard = guard

    def find_spec(self, name, path=None, target=None):
        guard = self.guard
        if guard.first_app_import is None and name.split(".")[0] in APP_ROOTS:
            guard.first_app_import = {"module": name, "guard_active": guard.is_active()}
        return None


class OfflineGuard:
    def __init__(self, profile, root):
        self.profile = profile
        self.root = Path(root).resolve()
        self.hits = []
        self.allowed = []
        self.first_app_import = None
        self.preexisting_app_modules = None
        self.installed_utc = None
        self._active = False
        self._allowed_popen_depth = 0
        self.originals = {}

    def _hit(self, event, detail):
        # Record first, then raise: catching the exception does not hide the event.
        self.hits.append({"event": event, "detail": detail[:500]})
        raise PermissionError("offline sentinel: " + event)

    def _audit(self, event, args):
        if not self._active:
            return
        if event == "os.posix_spawn" and self._allowed_popen_depth > 0:
            return  # The allowed Popen's own spawn; its argv was already checked.
        if event in NETWORK_EVENTS or event in PROCESS_EVENTS:
            self._hit(event, repr(args))
        elif event == "subprocess.Popen":
            argv = _argv_list(args[1], args[0])
            if process_allowed(self.profile, self.root, argv):
                self.allowed.append(argv)
            else:
                self._hit(event, repr(argv))

    def _refuse(self, name):
        def sentinel(*args, **_kwargs):
            self._hit(name, repr(args)[:500])
        setattr(sentinel, SENTINEL_MARK, True)
        return sentinel

    def _spawn_sentinel(self, name, original):
        def sentinel(*args, **kwargs):
            if self._allowed_popen_depth <= 0:
                self._hit("os." + name, repr(args)[:500])
            return original(*args, **kwargs)
        setattr(sentinel, SENTINEL_MARK, True)
        return sentinel

    def _helper_wrapper(self, name, original):
        def wrapper(*args, **kwargs):
            command = args[0] if args else kwargs.get("args", kwargs.get("cmd"))
            if name.startswith("get"):
                self._hit("subprocess." + name, repr(command)[:500])  # Shell helpers: never allowed.
            argv = _argv_list(command, kwargs.get("executable"))
            if not process_allowed(self.profile, self.root, argv):
                self._hit("subprocess." + name, repr(argv))
            return original(*args, **kwargs)
        setattr(wrapper, SENTINEL_MARK, True)
        return wrapper

    def unredirected_bound_defaults(self):
        """Function/method defaults in loaded app modules that still hold an original network/process callable."""
        found = []
        for module_name, module in sorted(sys.modules.items()):
            if module_name.split(".")[0] not in APP_ROOTS or not getattr(module, "__file__", None):
                continue
            candidates = []
            for value in list(vars(module).values()):
                if isinstance(value, type) and getattr(value, "__module__", None) == module_name:
                    candidates += [v for v in vars(value).values()]
                candidates.append(value)
            for value in candidates:
                function = getattr(value, "__func__", value)
                if not isinstance(function, types.FunctionType) or function.__module__ != module_name:
                    continue
                defaults = list(function.__defaults__ or ()) + list((function.__kwdefaults__ or {}).values())
                for default in defaults:
                    if id(default) in self.originals:
                        found.append({"module": module_name, "function": function.__qualname__,
                                      "default": self.originals[id(default)][0]})
        return found

    def qualify_default_check(self):
        """Inert: a synthetic app-named module whose default still binds the original subprocess.run is found."""
        label_original = next((v for v in self.originals.values() if v[0] == "subprocess.run"), None)
        if label_original is None:
            return False
        name = "tests._fc4_bound_default_probe"
        probe = types.ModuleType(name)
        probe.__file__ = str(RUNNER)

        def synthetic(runner=None):
            return runner
        synthetic.__module__ = name
        synthetic.__defaults__ = (label_original[1],)
        probe.synthetic = synthetic
        sys.modules[name] = probe
        try:
            found = [f for f in self.unredirected_bound_defaults() if f["module"] == name]
        finally:
            del sys.modules[name]
        return found == [{"module": name, "function": synthetic.__qualname__, "default": "subprocess.run"}]

    def install(self):
        self.preexisting_app_modules = sorted(n for n in sys.modules if n.split(".")[0] in APP_ROOTS)
        sys.addaudithook(self._audit)  # Permanent: audit hooks cannot be removed.
        for owner, name in ((socket.socket, "connect"), (socket.socket, "connect_ex"), (socket.socket, "sendto"),
                            (socket, "create_connection"), (socket, "getaddrinfo"), (socket, "gethostbyname"),
                            (os, "system"), (os, "popen")):
            label = "{}.{}".format(getattr(owner, "__name__", owner), name)
            self.originals[id(getattr(owner, name))] = (label, getattr(owner, name))
            setattr(owner, name, self._refuse(label))
        for name in ("execv", "execve", "execvp", "execvpe", "execl", "execle", "execlp", "execlpe", "fork",
                     "forkpty", "spawnv", "spawnve", "spawnvp", "spawnvpe"):
            if hasattr(os, name):
                self.originals[id(getattr(os, name))] = ("os." + name, getattr(os, name))
                setattr(os, name, self._refuse("os." + name))
        for name in ("posix_spawn", "posix_spawnp"):
            # subprocess itself may use posix_spawn for an already allowed Popen; nothing else may.
            if hasattr(os, name):
                setattr(os, name, self._spawn_sentinel(name, getattr(os, name)))
        original_init = subprocess.Popen.__init__
        guard = self

        def guarded_init(popen, args, *rest, **kwargs):
            argv = _argv_list(args, kwargs.get("executable"))
            if not process_allowed(guard.profile, guard.root, argv):
                guard._hit("subprocess.Popen.__init__", repr(argv))
            guard._allowed_popen_depth += 1
            try:
                return original_init(popen, args, *rest, **kwargs)
            finally:
                guard._allowed_popen_depth -= 1
        setattr(guarded_init, SENTINEL_MARK, True)
        subprocess.Popen.__init__ = guarded_init
        # Redirect the module-level helpers too, so application defaults such as ``runner=subprocess.run``
        # bound at import time capture a guarded wrapper (they also reach the guarded Popen underneath).
        for name in PROCESS_HELPERS:
            original = getattr(subprocess, name)
            self.originals[id(original)] = ("subprocess." + name, original)
            setattr(subprocess, name, self._helper_wrapper(name, original))
        sys.meta_path.insert(0, _FirstAppImport(self))
        self._active = True
        self.installed_utc = now()
        return self

    def is_active(self):
        return bool(self._active and getattr(socket.socket.connect, SENTINEL_MARK, False)
                    and getattr(socket.create_connection, SENTINEL_MARK, False)
                    and getattr(subprocess.Popen.__init__, SENTINEL_MARK, False)
                    and getattr(os.system, SENTINEL_MARK, False))

    def snapshot(self):
        loaded, outside = {}, []
        for name, module in sorted(sys.modules.items()):
            if name.split(".")[0] not in APP_ROOTS:
                continue
            path = getattr(module, "__file__", None)
            if not path:
                continue
            loaded[name] = path
            if not Path(path).resolve().is_relative_to(self.root):
                outside.append(name)
        return {"profile": self.profile, "root": str(self.root), "installed_utc": self.installed_utc,
                "active_at_snapshot": self.is_active(),
                "preexisting_app_modules": self.preexisting_app_modules,
                "first_app_import": self.first_app_import, "hits": list(self.hits),
                "allowed_processes": list(self.allowed), "loaded_app_modules": loaded,
                "modules_outside_root": outside, "closure_scan": scan_closure(loaded),
                "unredirected_bound_defaults": self.unredirected_bound_defaults(),
                "bound_defaults_checked": bool(self.originals),
                "model_client_modules": sorted(n for n in MODEL_CLIENTS if n in sys.modules),
                "env": {k: os.environ.get(k) for k in REDIRECTED_ENV}}


def guard_problems(state, expect_app_import, prefix):
    problems = []
    if state["hits"]:
        problems.append(prefix + ":offline_sentinel_triggered")
    if state["preexisting_app_modules"]:
        problems.append(prefix + ":app_module_imported_before_guard")
    first = state["first_app_import"]
    if (expect_app_import and not first) or (first and not first.get("guard_active")):
        problems.append(prefix + ":first_app_import_not_guarded")
    if not state["active_at_snapshot"]:
        problems.append(prefix + ":guard_not_active_at_end")
    if state["modules_outside_root"]:
        problems.append(prefix + ":app_modules_loaded_outside_root")
    if state["model_client_modules"]:
        problems.append(prefix + ":model_or_network_client_loaded")
    if state["closure_scan"]["findings"] or state["closure_scan"]["unreadable"]:
        problems.append(prefix + ":import_time_environment_or_io_reference")
    if state["unredirected_bound_defaults"] or (state["closure_scan"]["bound_process_defaults"]
                                                 and not state["bound_defaults_checked"]):
        problems.append(prefix + ":bound_process_default_not_redirected")
    return problems


# Attribute pairs that would bind environment, home, cwd or temp defaults or start IO when evaluated at import.
IMPORT_TIME_REFERENCES = frozenset((
    ("os", "environ"), ("os", "getenv"), ("os", "putenv"), ("os", "getcwd"), ("path", "expanduser"),
    ("path", "expandvars"), ("Path", "home"), ("Path", "cwd"), ("tempfile", "gettempdir"),
    ("tempfile", "mkdtemp"), ("tempfile", "gettempdirb"), ("getpass", "getuser"), ("socket", "socket"),
    ("socket", "create_connection"), ("subprocess", "run"), ("subprocess", "Popen"),
    ("subprocess", "check_output"), ("subprocess", "check_call"), ("subprocess", "call"),
    ("urllib", "request"), ("request", "urlopen")))
IMPORT_TIME_NAMES = frozenset(("environ", "getenv", "expanduser", "expandvars", "gettempdir", "urlopen"))
# A default value that merely *references* one of these (e.g. ``runner=subprocess.run``) is redirected by the
# guard before import; the runtime default check then proves no original callable remains bound.
REDIRECTED_DEFAULT_REFERENCES = frozenset("subprocess." + name for name in PROCESS_HELPERS)


class _ImportTimeScanner(ast.NodeVisitor):
    """Visits only code evaluated at import: module/class bodies, decorators and default values."""

    def __init__(self):
        self.findings = []
        self.bound_defaults = []
        self._in_default = False

    def _functionish(self, node):
        for item in getattr(node, "decorator_list", []):
            self.visit(item)
        for item in node.args.defaults + [d for d in node.args.kw_defaults if d is not None]:
            self._in_default = True
            try:
                self.visit(item)
            finally:
                self._in_default = False

    visit_FunctionDef = visit_AsyncFunctionDef = visit_Lambda = _functionish

    def _record(self, node, reference):
        finding = {"line": node.lineno, "reference": reference}
        if self._in_default and isinstance(node, ast.Attribute) and reference in REDIRECTED_DEFAULT_REFERENCES:
            self.bound_defaults.append(finding)
        else:
            self.findings.append(finding)

    def visit_Attribute(self, node):
        owner = node.value.id if isinstance(node.value, ast.Name) else getattr(node.value, "attr", None)
        if (owner, node.attr) in IMPORT_TIME_REFERENCES:
            self._record(node, "{}.{}".format(owner, node.attr))
        self.generic_visit(node)

    def visit_Name(self, node):
        if node.id in IMPORT_TIME_NAMES:
            self._record(node, node.id)


def scan_closure(loaded):
    modules, findings, bound, unreadable = {}, [], [], []
    for name, path in sorted(loaded.items()):
        try:
            source = Path(path).read_bytes()
            tree = ast.parse(source, filename=path)
        except (OSError, SyntaxError, ValueError):
            unreadable.append(name)
            continue
        modules[name] = sha256_bytes(source)
        scanner = _ImportTimeScanner()
        scanner.visit(tree)
        findings += [dict(item, module=name) for item in scanner.findings]
        bound += [dict(item, module=name) for item in scanner.bound_defaults]
    return {"modules": modules, "findings": findings, "bound_process_defaults": bound, "unreadable": unreadable}


def guarded_child(args):
    """Guarded unittest child: guard before any application import, then the named selectors."""
    guard = OfflineGuard(args.guard_profile, args.guard_root).install()
    root = guard.root
    sys.path[:] = [p for p in sys.path if Path(p or os.curdir).resolve() != RUNNER.parent]
    sys.path.insert(0, str(root))
    synthetic = []
    if args.synthetic_sentinel:
        # Inert qualification: synthetic audit events only; no socket or process is created.
        for event, event_args in (("socket.getaddrinfo", ("fc4-synthetic.invalid", 0, 0, 0, 0)),
                                  ("subprocess.Popen", ("fc4-synthetic-not-allowed",
                                                        ["fc4-synthetic-not-allowed"], None, None))):
            try:
                sys.audit(event, *event_args)
                synthetic.append({"event": event, "caught": False})
            except PermissionError:
                synthetic.append({"event": event, "caught": True})
    program = unittest.main(module=None, argv=["unittest", "-v"] + list(args.targets), exit=False)
    outcome = program.result
    state = guard.snapshot()
    problems = guard_problems(state, True, "child")
    default_check_qualified = guard.qualify_default_check() if args.synthetic_sentinel else None
    tests_ok = outcome.wasSuccessful() and outcome.testsRun > 0 and not outcome.skipped
    data = dict(state, targets=list(args.targets), cwd=os.getcwd(), sys_path_head=sys.path[:3],
                tests_run=outcome.testsRun, failures=[c.id() for c, _ in outcome.failures],
                errors=[c.id() for c, _ in outcome.errors], skipped=[c.id() for c, _ in outcome.skipped],
                tests_successful=tests_ok, synthetic_events=synthetic,
                default_check_qualified=default_check_qualified, problems=problems)
    with open(args.guarded_report, "x") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
    return 0 if tests_ok and not problems else 1


# ------------------------------------------------------------ frozen brief


def frozen_brief_dir():
    return REPO.parent / FROZEN_BRIEF_DIR_NAME


def verify_frozen_brief(directory):
    files, problems = {}, []
    for name, (size, digest) in sorted(FROZEN_BRIEF.items()):
        path = Path(directory) / name
        entry = {"path": str(path), "expected_bytes": size, "expected_sha256": digest.hex()}
        if not path.is_file():
            entry["present"] = False
            problems.append("frozen_brief_missing:" + name)
        else:
            data = path.read_bytes()
            entry.update(present=True, bytes=len(data), sha256=sha256_bytes(data))
            if len(data) != size or hashlib.sha256(data).digest() != digest:
                problems.append("frozen_brief_mismatch:" + name)
        files[name] = entry
    return {"version": FROZEN_BRIEF_VERSION, "location": "sibling '{}' of the repository root, outside the public "
            "tree".format(FROZEN_BRIEF_DIR_NAME), "files": files, "problems": problems}


def qualify_brief_gate(run_dir):
    """Inert qualification: the gate must refuse a missing brief and a one-bit-mutated copy."""
    base = run_dir / "brief-gate-qualification"
    missing, mutated = base / "missing", base / "mutated"
    missing.mkdir(parents=True)
    mutated.mkdir()
    for name in FROZEN_BRIEF:
        source = frozen_brief_dir() / name
        if source.is_file():
            data = bytearray(source.read_bytes())
            if data:
                data[0] ^= 1
            (mutated / name).write_bytes(bytes(data))
    on_missing, on_mutated = verify_frozen_brief(missing), verify_frozen_brief(mutated)
    expected_missing = sorted("frozen_brief_missing:" + n for n in FROZEN_BRIEF)
    expected_mutated = sorted("frozen_brief_mismatch:" + n for n in FROZEN_BRIEF)
    ok = sorted(on_missing["problems"]) == expected_missing and sorted(on_mutated["problems"]) == expected_mutated
    return {"missing_problems": on_missing["problems"], "mutated_problems": on_mutated["problems"],
            "refused_as_expected": ok}


# ------------------------------------------------------------ git / children


def new_run_dir(output_root, check):
    root = Path(output_root).resolve()
    if root == REPO or REPO in root.parents:
        raise SystemExit("output root must be outside the repository")
    root.mkdir(parents=True, exist_ok=True)
    run = root / "{}-{}-{}-{}".format(check, now(), os.getpid(), secrets.token_hex(4))
    run.mkdir(exist_ok=False)
    return run


def source_hashes(root):
    return {rel: (sha256_file(root / rel) if (root / rel).is_file() else None) for rel in HASHED_SOURCES}


def git(*args, binary=False):
    done = subprocess.run(["git", "-C", str(REPO)] + list(args), capture_output=True,
                          text=not binary, timeout=120, check=True)
    return done.stdout


def git_identity():
    identity = {}
    for name, args in (("head", ("rev-parse", "HEAD")),
                       ("status_porcelain", ("status", "--porcelain", "--untracked-files=all"))):
        try:
            identity[name] = git(*args).strip()
        except (OSError, subprocess.SubprocessError) as error:
            identity[name] = None
            identity[name + "_error"] = type(error).__name__
    identity["clean"] = identity.get("status_porcelain") == ""
    try:
        identity["base"] = baseline_commit()
        identity["changed_files_vs_base"] = sorted(git("diff", "--name-only", identity["base"], "HEAD").split())
    except (OSError, subprocess.SubprocessError, RuntimeError) as error:
        identity["base"] = None
        identity["base_error"] = type(error).__name__
    identity["pinned_base"] = PINNED_BASE
    identity["base_matches_pin"] = identity.get("base") == PINNED_BASE
    return identity


def baseline_commit():
    """Base = parent of the commit that first added the focused test module (checked against PINNED_BASE)."""
    added = [line for line in git("log", "--format=%H", "--diff-filter=A", "HEAD", "--", FOCUSED_PATH).split() if line]
    if not added:
        raise RuntimeError("focused_test_commit_not_found")
    return git("rev-parse", "--verify", added[-1] + "^1").strip()


def run_child(run_dir, label, argv, cwd, env=None, stdout_path=None, timeout=900):
    """Run one argument-array child (no shell) and preserve its raw streams."""
    record = {"label": label, "argv": [str(a) for a in argv], "cwd": str(cwd)}
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


def isolated_env(run_dir, label):
    home = run_dir / (label + "-isolated-home")
    home.mkdir()
    env = dict(os.environ)  # Inherit; redirect only data/home defaults (no search-path variables).
    env.update({key: str(home) for key in REDIRECTED_ENV})
    return env, home


def guarded_argv(runner, flags, report, profile, root, targets, synthetic=False):
    return ([sys.executable] + list(flags) + [runner, "--guarded-report", report, "--guard-profile", profile,
                                               "--guard-root", root]
            + (["--synthetic-sentinel"] if synthetic else []) + ["--targets"] + list(targets))


def read_report(record, report):
    if not Path(report).is_file():
        return None
    record["guard_report_path"] = str(report)
    record["guard_report_sha256"] = sha256_file(report)
    return json.loads(Path(report).read_text())


def verbose_test_id(name, parenthesized):
    # Python 3.11+ prints ``name (module.Class.name)``; older runtimes ``name (module.Class)``.
    return parenthesized if parenthesized.endswith("." + name) else parenthesized + "." + name


def parse_unittest(stderr_text):
    ran = RAN.findall(stderr_text)
    summary = SUMMARY.findall(stderr_text)
    tests = [{"name": m.group(1), "id": verbose_test_id(m.group(1), m.group(2)),
              "outcome": "skipped" if m.group(3).startswith("skipped") else m.group(3)}
             for m in VERBOSE.finditer(stderr_text)]
    detail = summary[-1][1] if summary else ""
    counts = dict((k.strip(), int(v)) for k, v in (part.split("=") for part in detail.split(", ") if "=" in part))
    return {"tests_run": int(ran[-1]) if ran else 0, "summary": summary[-1][0] if summary else None,
            "summary_detail": detail, "summary_counts": counts, "tests": tests}


def loaded_suite_ids(targets, root):
    """IDs of the suite unittest would execute for ``targets`` (loaded, not run; parent is guarded)."""
    root_text = str(root)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    ids = []

    def walk(item):
        if isinstance(item, unittest.TestSuite):
            for child in item:
                walk(child)
        else:
            ids.append(item.id())
    for target in targets:
        walk(unittest.TestLoader().loadTestsFromName(target))
    return ids


def unittest_child(run_dir, label, targets, root, profile, expect_loaded=True, runner=RUNNER, flags=("-B",)):
    """Guarded unittest child for ``targets`` under ``root``; fails closed on any guard problem."""
    root = Path(root).resolve()
    report = run_dir / (label + ".guard-report.json")
    env, home = isolated_env(run_dir, label)
    record = run_child(run_dir, label, guarded_argv(runner, flags, report, profile, root, targets), root, env=env)
    record["isolated_home"] = str(home)
    stderr_text = Path(record["stderr_path"]).read_text(errors="replace") if "stderr_path" in record else ""
    parsed = parse_unittest(stderr_text)
    record.update(parsed)
    problems = []
    guard = read_report(record, report)
    record["guard"] = guard and {k: guard.get(k) for k in (
        "profile", "root", "installed_utc", "preexisting_app_modules", "first_app_import", "hits",
        "allowed_processes", "modules_outside_root", "model_client_modules", "env", "sys_path_head", "problems",
        "synthetic_events", "unredirected_bound_defaults", "bound_defaults_checked")}
    if guard is None:
        problems.append(label + ":guard_report_missing")
    else:
        record["guard_closure"] = guard["closure_scan"]
        record["loaded_app_modules"] = guard["loaded_app_modules"]
        if guard.get("profile") != profile or guard.get("root") != str(root):
            problems.append(label + ":guard_report_identity_mismatch")
        if guard.get("synthetic_events"):
            problems.append(label + ":unexpected_synthetic_events")
        problems += [label + ":" + p for p in guard.get("problems", [])]
        problems += [label + ":" + p for p in guard_problems(guard, True, "recheck")]
        if any(guard["env"].get(k) != str(home) for k in REDIRECTED_ENV):
            problems.append(label + ":environment_not_redirected")
    if expect_loaded:
        try:
            expected_ids = loaded_suite_ids(targets, root)
        except Exception as error:  # Unloadable suite: fail closed, keep the class.
            expected_ids = None
            record["loaded_suite_error"] = repr(error)[:2000]
        parsed_ids = [t["id"] for t in parsed["tests"]]
        record["loaded_suite_count"] = None if expected_ids is None else len(expected_ids)
        record["parsed_ids_match_loaded_suite"] = (expected_ids is not None and len(parsed_ids) == len(set(parsed_ids))
                                                   and sorted(parsed_ids) == sorted(expected_ids))
        if not record["parsed_ids_match_loaded_suite"]:
            problems.append(label + ":parsed_ids_differ_from_loaded_suite")
    if record.get("exit") != 0:
        problems.append(label + ":unittest_exit_nonzero")
    if parsed["summary"] != "OK":
        problems.append(label + ":unittest_summary_not_ok")
    if parsed["tests_run"] <= 0:
        problems.append(label + ":no_tests_executed")
    if len(parsed["tests"]) != parsed["tests_run"]:
        problems.append(label + ":executed_test_ids_incomplete")
    if guard is not None and guard.get("tests_run") != parsed["tests_run"]:
        problems.append(label + ":guard_report_test_count_mismatch")
    extras = sorted(k for k in parsed["summary_counts"] if k in ("skipped", "expected failures", "unexpected successes"))
    if extras:
        problems.append(label + ":unexpected_" + "_".join(k.replace(" ", "_") for k in extras))
    record["non_ok_test_ids"] = [t["id"] for t in parsed["tests"] if t["outcome"] != "ok"]
    if record["non_ok_test_ids"]:
        problems.append(label + ":non_ok_tests")
    record["problems"] = problems
    return record, problems


def qualify_guard_failure(run_dir, result):
    """Inert startup/propagation qualification of the guard used by the guarded paths.

    A guarded focused child emits two synthetic audit events (one network, one disallowed process),
    catches both and runs the real focused suite green. Because recorded sentinel events are fatal,
    it must still exit nonzero and report offline_sentinel_triggered. Nothing external is attempted.
    """
    label = "guard-qualification"
    report = run_dir / (label + ".guard-report.json")
    env, home = isolated_env(run_dir, label)
    record = run_child(run_dir, label, guarded_argv(RUNNER, ("-B",), report, "focused", REPO, [FOCUSED_MODULE],
                                                    synthetic=True), REPO, env=env)
    result["children"].append(record)
    data = read_report(record, report)
    problems = []
    if data is None:
        return [label + ":report_missing"]
    events = sorted(h["event"] for h in data["hits"])
    summary = {"exit": record.get("exit"), "synthetic_events": data["synthetic_events"], "hit_events": events,
               "tests_run": data["tests_run"], "tests_successful": data["tests_successful"],
               "child_problems": data["problems"], "first_app_import": data["first_app_import"],
               "preexisting_app_modules": data["preexisting_app_modules"],
               "default_check_qualified": data.get("default_check_qualified"),
               "bound_process_defaults": data["closure_scan"]["bound_process_defaults"],
               "unredirected_bound_defaults": data["unredirected_bound_defaults"]}
    result["guard_qualification"] = summary
    if data.get("default_check_qualified") is not True:
        problems.append(label + ":bound_default_check_not_qualified")
    if not (data["synthetic_events"] and all(e["caught"] for e in data["synthetic_events"])
            and len(data["synthetic_events"]) == 2):
        problems.append(label + ":synthetic_events_not_caught_in_child")
    if events != ["socket.getaddrinfo", "subprocess.Popen"]:
        problems.append(label + ":synthetic_events_not_recorded")
    if not (data["tests_successful"] and data["tests_run"] == len(FROZEN_CASES)):
        problems.append(label + ":focused_suite_not_green_under_guard")
    if "child:offline_sentinel_triggered" not in data["problems"] or record.get("exit") in (0, None):
        problems.append(label + ":caught_sentinel_not_fatal")
    if not (data["first_app_import"] and data["first_app_import"].get("guard_active")) \
            or data["preexisting_app_modules"]:
        problems.append(label + ":guard_not_installed_before_first_app_import")
    return problems


def _load_in_process(root):
    root_text = str(root)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    from campaign_tool.records.understanding import rows
    focused = __import__(FOCUSED_MODULE, fromlist=["*"])
    for module in (rows, focused):
        if not Path(module.__file__).resolve().is_relative_to(Path(root).resolve()):
            raise RuntimeError("module_loaded_outside_candidate")
    return rows, focused


def _baseline_rows():
    """The pinned-base rows module, built in memory from the git object (no disk write)."""
    base = baseline_commit()
    source = git("show", base + ":" + ROWS_PATH, binary=True)
    module = types.ModuleType("campaign_tool.records.understanding._fc4_baseline_rows")
    module.__package__ = "campaign_tool.records.understanding"
    module.__file__ = "<git {}:{}>".format(base, ROWS_PATH)
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    return module, sha256_bytes(source), base


def _test_id(case):
    """Owning test method id; a failing subTest reports its parent test, not its parameters."""
    parent = getattr(case, "test_case", None)
    return (parent if parent is not None else case).id()


def _summarise(outcome):
    return {"tests_run": outcome.testsRun,
            "failures": sorted({_test_id(case) for case, _ in outcome.failures}),
            "errors": sorted({_test_id(case) for case, _ in outcome.errors}),
            "skipped": sorted({_test_id(case) for case, _ in outcome.skipped}),
            "failing_subtests": sum(1 for case, _ in outcome.failures + outcome.errors
                                    if getattr(case, "test_case", None) is not None),
            "unexpected_successes": [case.id() for case in outcome.unexpectedSuccesses],
            "successful": outcome.wasSuccessful()}


def _run_names(module, names):
    loader = unittest.TestLoader()
    suite = unittest.TestSuite(loader.loadTestsFromName(name, module) for name in names)
    stream = io.StringIO()
    outcome = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    return outcome, stream.getvalue()


def _case_id(focused, case):
    return "{}.{}.test_{}".format(focused.__name__, CASE_CLASS, case)


# ----------------------------------------------------------------- checks


def check_focused(run_dir, result):
    record, problems = unittest_child(run_dir, "focused-unittest", [FOCUSED_MODULE], REPO, "focused")
    result["children"].append(record)
    rows, focused = _load_in_process(REPO)
    expected_ids = {_case_id(focused, case) for case in FROZEN_CASES}
    executed = {t["id"] for t in record.get("tests", [])}
    result["case_map"] = {case: _case_id(focused, case) for case in FROZEN_CASES}
    if executed != expected_ids:
        problems.append("frozen_case_coverage_mismatch")
        result["case_map_missing"] = sorted(expected_ids - executed)
        result["case_map_extra"] = sorted(executed - expected_ids)
    # Genuine regression: the same cases run against the pinned-base rows module in memory.
    try:
        baseline, baseline_sha, base = _baseline_rows()
        original = focused.rows
        try:
            focused.rows = baseline
            outcome, text = _run_names(focused, [CASE_CLASS])
        finally:
            focused.rows = original
        (run_dir / "baseline-red.txt").write_text(text)
        summary = _summarise(outcome)
        failing = {i.rsplit(".", 1)[-1][len("test_"):] for i in summary["failures"] + summary["errors"]}
        result["baseline_red"] = {"base": base, "baseline_rows_sha256": baseline_sha,
                                  "pinned_base_rows_sha256": PINNED_BASE_ROWS_SHA256,
                                  "summary": summary, "failing_cases": sorted(failing),
                                  "output_sha256": sha256_file(run_dir / "baseline-red.txt"),
                                  "restored_rows_module": focused.rows is rows}
        if baseline_sha != PINNED_BASE_ROWS_SHA256:
            problems.append("baseline_rows_differs_from_pin")
        if failing != set(FROZEN_CASES) - BASE_GREEN:
            problems.append("baseline_red_cases_differ_from_expected")
        if summary["tests_run"] != len(FROZEN_CASES) or summary["skipped"]:
            problems.append("baseline_red_incomplete")
        if focused.rows is not rows:
            problems.append("rows_module_not_restored")
    except (subprocess.SubprocessError, OSError, RuntimeError, SyntaxError) as error:
        problems.append("baseline_red_incomplete:" + type(error).__name__)
    problems += qualify_guard_failure(run_dir, result)
    return problems


def check_compatibility(run_dir, result):
    problems = []
    final, final_problems = unittest_child(run_dir, "compatibility-final", COMPAT_SELECTORS, REPO, "compatibility")
    result["children"].append(final)
    problems += final_problems
    # Baseline: the same unchanged selectors in a local clone checked out at the pinned base
    # (a real git work tree, since some selectors assert git-boundary behaviour).
    base = baseline_commit()
    base_root = run_dir / "baseline-clone"
    for label, argv in (("baseline-clone", ["git", "clone", "-q", "--no-hardlinks", "--no-checkout", REPO, base_root]),
                        ("baseline-checkout", ["git", "-C", base_root, "checkout", "-q", "--detach", base])):
        record = run_child(run_dir, label, argv, run_dir)
        result["children"].append(record)
        if record.get("exit") != 0:
            return problems + [label + "_failed"]
    head = subprocess.run(["git", "-C", str(base_root), "rev-parse", "HEAD"], capture_output=True, text=True,
                          timeout=60, check=True).stdout.strip()
    if head != base:
        return problems + ["baseline_clone_head_mismatch"]
    baseline, baseline_problems = unittest_child(run_dir, "compatibility-baseline", COMPAT_SELECTORS, base_root,
                                                 "compatibility", expect_loaded=False)
    result["children"].append(baseline)
    result["compatibility"] = {"base": base, "baseline_tests_run": baseline["tests_run"],
                               "baseline_summary": baseline["summary"], "final_tests_run": final["tests_run"],
                               "final_summary": final["summary"],
                               "baseline_ids": sorted(t["id"] for t in baseline["tests"]),
                               "final_ids": sorted(t["id"] for t in final["tests"]),
                               "baseline_problems": baseline_problems}
    if baseline_problems:
        problems.append("unexpected_baseline_failure")
    if result["compatibility"]["baseline_ids"] != result["compatibility"]["final_ids"]:
        problems.append("compatibility_test_ids_changed")
    problems += qualify_guard_failure(run_dir, result)
    return problems


NEGATIVE_REGRESSIONS = ("test_partial_blocked_unknown_page_status", "test_rows_retained_for_triage",
                        "test_multiple_pages_one_not_clear")
NEGATIVE_POSITIVES = ("test_ok_page_and_empty_page_inventory", "test_publication_stays_false")


def _direct(rows, focused):
    """Blocked page / ok page / incomplete top level through the real normalize()."""
    out = {}
    for name, pages, status in (("blocked_page", [focused.page("blocked", True)], "complete"),
                                ("ok_page", [focused.page()], "complete"),
                                ("top_level_partial", [focused.page()], "partial")):
        output, args, before = focused.run(pages, status=status)
        out[name] = {"complete": output["normalized"]["complete"],
                     "upstream_reasons": sorted(u["reason"] for u in output["upstream"]),
                     "receipt_hash_bound": sha256_bytes(args["receipts"][0]["derived_receipt"])
                     == args["receipts"][0]["derived_receipt_sha256"],
                     "inputs_unchanged": args == before,
                     "publication_ready": output["publication_ready"]}
    return out


def check_negative(run_dir, result):
    """In-memory mutant: only the page-state predicate is bypassed (always clear). Parent is guarded."""
    problems = []
    rows, focused = _load_in_process(REPO)
    original = rows._page_clear
    disk_before = sha256_file(REPO / ROWS_PATH)
    phases = {}
    try:
        rows._page_clear = lambda page: True
        regress, regress_text = _run_names(focused, [CASE_CLASS + "." + n for n in NEGATIVE_REGRESSIONS])
        positive, positive_text = _run_names(focused, [CASE_CLASS + "." + n for n in NEGATIVE_POSITIVES])
        phases["mutant_regressions"] = _summarise(regress)
        phases["mutant_positives"] = _summarise(positive)
        phases["mutant_direct"] = _direct(rows, focused)
        try:  # Shape refusal is a separate gate and must survive the predicate bypass.
            focused.run([None])
            phases["mutant_shape_refusal"] = None
        except rows.RowsError as error:
            phases["mutant_shape_refusal"] = str(error)
        (run_dir / "negative-mutant.txt").write_text(regress_text + "\n" + positive_text)
    finally:
        rows._page_clear = original
    phases["restored_predicate"] = rows._page_clear is original
    restored, restored_text = _run_names(focused, [CASE_CLASS])
    phases["restored"] = _summarise(restored)
    phases["restored_direct"] = _direct(rows, focused)
    (run_dir / "negative-restored.txt").write_text(restored_text)
    for name in ("negative-mutant.txt", "negative-restored.txt"):
        phases[name + "_sha256"] = sha256_file(run_dir / name)
    phases["rows_on_disk_unchanged"] = sha256_file(REPO / ROWS_PATH) == disk_before
    result["negative"] = phases
    mutant = phases["mutant_regressions"]
    detected = set(mutant["failures"] + mutant["errors"])
    expected = {"{}.{}.{}".format(focused.__name__, CASE_CLASS, n) for n in NEGATIVE_REGRESSIONS}
    if mutant["tests_run"] != len(NEGATIVE_REGRESSIONS) or detected != expected:
        problems.append("mutant_not_detected_by_every_regression")
    md = phases["mutant_direct"]
    if not (md["blocked_page"]["complete"] is True and md["blocked_page"]["upstream_reasons"] == []
            and md["blocked_page"]["receipt_hash_bound"]):
        problems.append("mutant_did_not_reproduce_blocked_page_complete")
    if md["top_level_partial"]["complete"] is not False or md["ok_page"]["complete"] is not True:
        problems.append("mutant_controls_wrong")
    if phases["mutant_shape_refusal"] != "receipt_page_shape":
        problems.append("shape_gate_not_independent_of_predicate")
    if not phases["mutant_positives"]["successful"] or phases["mutant_positives"]["tests_run"] != len(NEGATIVE_POSITIVES):
        problems.append("positives_not_green_under_mutant")
    if not phases["restored_predicate"]:
        problems.append("predicate_not_restored")
    if not phases["restored"]["successful"] or phases["restored"]["tests_run"] != len(FROZEN_CASES) \
            or phases["restored"]["skipped"]:
        problems.append("restored_suite_not_green")
    rd = phases["restored_direct"]
    if not (rd["blocked_page"]["complete"] is False and rd["blocked_page"]["upstream_reasons"] == ["upstream_pages_not_clear"]
            and rd["ok_page"]["complete"] is True and rd["ok_page"]["upstream_reasons"] == []
            and rd["top_level_partial"]["complete"] is False
            and rd["top_level_partial"]["upstream_reasons"] == ["upstream_not_clear"]):
        problems.append("restored_direct_controls_wrong")
    if any(v["publication_ready"] is not False or not v["inputs_unchanged"] for d in (md, rd) for v in d.values()):
        problems.append("publication_or_input_preservation_broken")
    if not phases["rows_on_disk_unchanged"]:
        problems.append("rows_mutated_on_disk")
    problems += qualify_guard_failure(run_dir, result)
    return problems


def static_inspection(root):
    findings = []
    for relative in STATIC_SCANNED:
        text = (root / relative).read_text()
        for number, line in enumerate(text.splitlines(), 1):
            for token in BOX_SPECIFIC:
                if token in line:
                    findings.append({"file": relative, "line": number, "kind": "box_specific_path"})
            for pattern in INLINE_FORMS:
                if pattern.search(line):
                    findings.append({"file": relative, "line": number, "kind": "inline_or_shell_form"})
    return findings


def candidate_files():
    names = git("ls-files", "-z", "--cached", "--others", "--exclude-standard").split("\0")
    return sorted({name for name in names if name})


def check_portability(run_dir, result):
    problems = []
    findings = static_inspection(REPO)
    result["static_inspection"] = {"files": list(STATIC_SCANNED), "findings": findings}
    if findings:
        problems.append("static_box_specific_or_inline_findings")
    with tempfile.TemporaryDirectory(prefix="fc4-page-copy-", dir=str(run_dir)) as temporary:
        copy_root = (Path(temporary) / "relocated" / "candidate-source").resolve()
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
        # The relocated runner (isolated mode) installs the guard before importing from the copy.
        record, child_problems = unittest_child(run_dir, "portability-child", [FOCUSED_MODULE], copy_root,
                                                "portability", expect_loaded=False,
                                                runner=copy_root / "tools" / RUNNER.name, flags=("-I", "-B"))
        result["children"].append(record)
        problems += child_problems
        guard = record.get("guard") or {}
        loaded = record.get("loaded_app_modules") or {}
        result["portability_child"] = {"repo_root": guard.get("root"), "tests_run": record.get("tests_run"),
                                       "sentinel_hits": guard.get("hits"),
                                       "modules_outside_copy": guard.get("modules_outside_root"),
                                       "loaded_modules": loaded, "first_app_import": guard.get("first_app_import")}
        if guard.get("root") != str(copy_root):
            problems.append("child_did_not_use_copy")
        if record.get("tests_run") != len(FROZEN_CASES):
            problems.append("child_focused_not_green")
        if not loaded or any(not Path(p).resolve().is_relative_to(copy_root) for p in loaded.values()):
            problems.append("application_modules_loaded_outside_copy")
    return problems


def check_credentials(run_dir, result):
    problems = []
    scanner = Path(sys.executable).parent / "detect-secrets"
    if not scanner.is_file():
        result["scanner"] = {"path": str(scanner), "present": False}
        return ["detect_secrets_unavailable_adjacent_to_interpreter"]
    report = run_dir / "secret-scan.json"
    scan = run_child(run_dir, "detect-secrets-scan",
                     [scanner, "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"],
                     REPO, stdout_path=report)
    result["children"].append(scan)
    result["children"].append(run_child(run_dir, "detect-secrets-version", [scanner, "--version"], REPO))
    if scan.get("exit") != 0:
        problems.append("secret_scan_failed")
    try:
        data = json.loads(report.read_text())
        if not isinstance(data.get("results"), dict):
            raise ValueError("results")
        result["secret_scan"] = {"files_with_hits": len(data["results"]),
                                 "raw_hits": sum(len(v) for v in data["results"].values()),
                                 "hits_in_changed_files": sorted(set(data["results"]) & set(STATIC_SCANNED + (ROWS_PATH,)))}
    except (OSError, ValueError, AttributeError):
        return problems + ["secret_report_malformed"]
    checker = run_child(run_dir, "check-secret-report",
                        [sys.executable, "-B", REPO / "tools" / "check_secret_report.py", report], REPO)
    result["children"].append(checker)
    text = Path(checker["stdout_path"]).read_text(errors="replace") if "stdout_path" in checker else ""
    match = re.search(r"potential secrets: (\d+);", text)
    potential = int(match.group(1)) if match else None
    result["secret_scan"]["potential_secrets"] = potential
    if checker.get("exit") != 0:
        problems.append("check_secret_report_failed")
    if potential != 0:
        problems.append("potential_secrets_nonzero_or_unreported")
    if result["secret_scan"]["hits_in_changed_files"]:
        problems.append("raw_hits_in_changed_files")
    return problems


def check_public_tree(run_dir, result):
    record = run_child(run_dir, "check-public-tree",
                       [sys.executable, "-B", REPO / "tools" / "check_public_tree.py"], REPO)
    result["children"].append(record)
    return [] if record.get("exit") == 0 else ["public_tree_check_failed"]


HANDLERS = {"focused": check_focused, "compatibility": check_compatibility, "negative": check_negative,
            "portability": check_portability, "credentials": check_credentials, "public-tree": check_public_tree}


def main(argv=None):
    global PARENT_RUN_DIR
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", choices=CHECKS)
    parser.add_argument("--output-root")
    parser.add_argument("--guarded-report", help=argparse.SUPPRESS)
    parser.add_argument("--guard-profile", choices=("focused", "compatibility", "portability"), help=argparse.SUPPRESS)
    parser.add_argument("--guard-root", help=argparse.SUPPRESS)
    parser.add_argument("--synthetic-sentinel", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--targets", nargs="+", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.guarded_report:
        if args.check or args.output_root or not (args.guard_profile and args.guard_root and args.targets):
            parser.error("guarded child mode takes only its own arguments")
        return guarded_child(args)
    if not args.check or not args.output_root:
        parser.error("--check and --output-root are required")
    # Guard the parent before anything can import application or test code.
    parent_guard = OfflineGuard("parent", REPO).install()
    run_dir = new_run_dir(args.output_root, args.check)
    PARENT_RUN_DIR = run_dir.resolve()
    result = {"schema": SCHEMA, "card": CARD, "check": args.check,
              "argv": [sys.executable] + list(sys.argv),
              "cwd": os.getcwd(), "interpreter": sys.executable, "python_version": sys.version,
              "repo_root": str(REPO), "run_dir": str(run_dir), "started_utc": now(),
              "frozen_brief": verify_frozen_brief(frozen_brief_dir()),
              "brief_gate_qualification": qualify_brief_gate(run_dir),
              "base_selector": BASE_SELECTOR, "git": git_identity(), "source_hashes": source_hashes(REPO),
              "children": [], "acceptance": "REVIEW REQUIRED; machine validity only, not semantic/legal acceptance"}
    try:
        problems = HANDLERS[args.check](run_dir, result)
    except Exception as error:  # Incomplete check: record the class, fail closed.
        problems = ["check_incomplete:" + type(error).__name__]
        result["exception"] = repr(error)[:2000]
    problems = list(problems) + result["frozen_brief"]["problems"]
    if not result["brief_gate_qualification"]["refused_as_expected"]:
        problems.append("frozen_brief_gate_qualification_failed")
    if not result["git"].get("clean") or not result["git"].get("head") or not result["git"].get("base"):
        problems.append("candidate_identity_not_bound_clean_head")
    if not result["git"].get("base_matches_pin"):
        problems.append("base_differs_from_pin")
    result["parent_guard"] = parent_guard.snapshot()
    problems += guard_problems(result["parent_guard"], args.check in APP_IMPORTING_CHECKS, "parent")
    result["finished_utc"] = now()
    result["problems"] = problems
    result["verdict"] = "PASS_REVIEW_REQUIRED" if not problems else "FAIL"
    with open(run_dir / "run.json", "x") as handle:
        json.dump(result, handle, indent=2, sort_keys=True, default=str)
    brief = {name: entry.get("sha256") for name, entry in result["frozen_brief"]["files"].items()}
    print(json.dumps({"check": args.check, "verdict": result["verdict"], "problems": problems,
                      "run_dir": str(run_dir), "head": result["git"].get("head"),
                      "base": result["git"].get("base"), "clean": result["git"].get("clean"),
                      "frozen_brief_sha256": brief,
                      "run_json_sha256": sha256_file(run_dir / "run.json")}, sort_keys=True))
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
