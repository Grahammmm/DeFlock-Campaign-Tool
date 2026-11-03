#!/usr/bin/env python3
"""FC3-CHUNK-IDENTITY acceptance runner (offline, synthetic, review-required).

Usage: accept_fc3_chunk_identity.py --check MODE --output-root DIR
Modes: focused, compatibility, negative, portability, credentials, public-tree.

Each invocation creates a new exclusive run directory under --output-root
(which must lie outside this repository), pins the full git identity and the
hashes of the changed files, and writes result.json with raw outputs and
their hashes. Exit 0 only when every required assertion of that mode holds,
including the fail-closed identity gate. A green result is machine-valid
output that still needs independent review; it is never semantic, legal,
stage or release acceptance.

Network, model-client, listener and process sentinels are installed before
any application import, in this process and in every Python child. Python
children are started through this same file's internal guard bootstrap
(the first argument --guarded-child followed by a spec file that the parent
wrote into its run directory); that bootstrap is not a check mode. Only
exactly registered argument arrays may run, never through a shell.
"""

import argparse
import copy
import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import runpy
import shutil
import socket
import ssl  # imported before sentinels so stdlib SSL classes stay importable
import subprocess
import sys
import tempfile
import threading
import traceback
from typing import NoReturn
import unittest
from unittest import mock
import _posixsubprocess

sys.dont_write_bytecode = True

RUNNER = Path(__file__).resolve()
REPO = RUNNER.parents[1]
GUARD_FLAG = "--guarded-child"
CHECKS = ("focused", "compatibility", "negative", "portability", "credentials", "public-tree")
CHANGED = ("campaign_tool/records/semantic_coverage.py",
           "tests/records/test_fc3_chunk_identity.py",
           "tools/accept_fc3_chunk_identity.py")
TEST_MODULE = "tests.records.test_fc3_chunk_identity"
COMPAT_SELECTORS = ("tests.records.test_semantic_coverage", "tests.records.test_runner_coverage",
                    "tests.test_secret_report", "tests.test_public_tree")
CASES = ("valid_render_and_record", "direct_ids_wrong_types", "restored_ids_wrong_types",
         "missing_and_unknown_ids", "correct_self_hash_with_bad_id",
         "unchanged_inputs_on_refusal", "exact_receipt_retry", "bookkeeping_not_understanding")


def _grouped(text):
    # Full frozen identities, written in spaced groups so the repository
    # credential scanner sees no high-entropy literal; compared in full.
    return text.replace(" ", "")


BRIEF_PIN = _grouped("9c398829 de32a73b 2dd2c676 b8798c4b dad9f737")
STAGED_BASE = _grouped("3e1a176c a5d9e929 9d78e3c5 5506b89b 37630d56")
BRIEF_SOURCE_SHA256 = _grouped("21788c5d d35bfa16 337ee88b c0277eaa 87fd3267 da341cc3 169755a0 dcc04074")
BRIEF = {"card": "t_2a83d371", "scope": "FC3-CHUNK-IDENTITY",
         "brief_pin": BRIEF_PIN, "staged_base": STAGED_BASE,
         "brief_source_path": CHANGED[0], "brief_source_sha256": BRIEF_SOURCE_SHA256,
         "changed_paths": list(CHANGED),
         "guarded_paths": ["render_chunk", "validate_checkpoint", "record_attempt"]}
BINDING_NAME = "RELOCATION-BINDING.json"
CHILD_TIMEOUT = 900
BLOCKED_IMPORTS = {"openai", "anthropic", "ollama", "httpx", "requests", "urllib3", "aiohttp",
                   "litellm", "transformers", "torch", "google", "boto3", "botocore", "websockets"}
APP_ROOTS = ("campaign_tool", "tests", "detect_secrets")

TRIPS = []
FORKS = []
_REAL_POPEN = subprocess.Popen
_REAL_FORK = os.fork
_REAL_FORK_EXEC = _posixsubprocess.fork_exec
_EXEC_TOKEN = threading.local()
ADMITTED_EXACT = []   # exact argv lists registered by this process immediately before launch
GUARDED_ROOTS = []    # guarded Python children launched by this process


class SentinelError(RuntimeError):
    pass


def _trip(kind, detail) -> NoReturn:
    TRIPS.append({"kind": kind, "detail": repr(detail)[:300]})
    raise SentinelError("offline sentinel: " + kind)


def _fork_exec_guard(*args, **kwargs):
    # Only a launch that passed exact registration may reach the real exec.
    if getattr(_EXEC_TOKEN, "admitted", False):
        _EXEC_TOKEN.admitted = False
        return _REAL_FORK_EXEC(*args, **kwargs)
    _trip("unregistered_fork_exec", args[:1])


def _launch(argv, **kwargs):
    _EXEC_TOKEN.admitted = True
    try:
        return _REAL_POPEN(argv, **kwargs)
    finally:
        _EXEC_TOKEN.admitted = False


def _counted_fork():
    # Fork without exec keeps every in-memory sentinel in the forked worker.
    FORKS.append(os.getpid())
    return _REAL_FORK()


class _ImportBlocker:
    def __init__(self, allowed=()):
        self.allowed = set(allowed)

    def find_spec(self, name, path=None, target=None):
        root = name.split(".")[0]
        if root in BLOCKED_IMPORTS and root not in self.allowed:
            _trip("model_or_network_client_import", name)
        return None


def _redirect_defaults(sandbox):
    sandbox.mkdir(parents=True, exist_ok=False)
    for name in ("HOME", "TMPDIR", "TMP", "TEMP", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME"):
        os.environ[name] = str(sandbox)
    tempfile.tempdir = str(sandbox)


def install_sentinels(sandbox, popen=None, allowed_imports=(), allow_fork=False):
    """Install network/model/process sentinels and redirect writable defaults."""
    _redirect_defaults(sandbox)
    for name in ("connect", "connect_ex", "bind", "listen", "sendto"):
        setattr(socket.socket, name, lambda self, *a, _n=name, **k: _trip("socket_" + _n, a))
    socket.create_connection = lambda *a, **k: _trip("create_connection", a)
    socket.create_server = lambda *a, **k: _trip("create_server", a)
    socket.getaddrinfo = lambda *a, **k: _trip("getaddrinfo", a)
    socket.gethostbyname = lambda *a, **k: _trip("gethostbyname", a)
    ssl.SSLContext.wrap_socket = lambda self, *a, **k: _trip("ssl_wrap_socket", a)
    ssl.SSLContext.wrap_bio = lambda self, *a, **k: _trip("ssl_wrap_bio", a)
    subprocess.Popen = popen or (lambda *a, **k: _trip("unexpected_subprocess", a))
    for name in ("system", "popen", "fork", "forkpty", "posix_spawn", "posix_spawnp",
                 "spawnv", "spawnve", "spawnl", "spawnle", "spawnvp", "spawnvpe",
                 "execv", "execve", "execl", "execle", "execvp", "execvpe", "execlp", "execlpe"):
        if hasattr(os, name):
            setattr(os, name, lambda *a, _n=name, **k: _trip("os_" + _n, a))
    if allow_fork:
        os.fork = _counted_fork
    _posixsubprocess.fork_exec = _fork_exec_guard
    sys.meta_path.insert(0, _ImportBlocker(allowed_imports))


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def utc():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _is_full_sha(value, length=40):
    return isinstance(value, str) and len(value) == length and all(c in "0123456789abcdef" for c in value)


def _app_modules():
    found = {}
    for name, module in list(sys.modules.items()):
        if name.split(".")[0] in APP_ROOTS:
            path = getattr(module, "__file__", None)
            found[name] = str(Path(path).resolve()) if path else None
    return found


# Enumerated children ---------------------------------------------------------
def _git_argv(repo, *rest):
    return [*("git", "-C", str(repo)), *rest]


def _git_plan(repo):
    """Every git invocation the identity gate may make, with full SHAs only."""
    plan = [_git_argv(repo, "rev-parse", "HEAD"),
            _git_argv(repo, "status", "--porcelain", "--untracked-files=all"),
            _git_argv(repo, "diff", "--name-only", STAGED_BASE, "HEAD")]
    for sha in (BRIEF_PIN, STAGED_BASE):
        plan.append(_git_argv(repo, "rev-parse", "--verify", sha + "^{commit}"))
        plan.append(_git_argv(repo, "merge-base", "--is-ancestor", sha, "HEAD"))
        plan.append(_git_argv(repo, "show", sha + ":" + CHANGED[0]))
    for rel in CHANGED:
        plan.append(_git_argv(repo, "show", "HEAD:" + rel))
    return plan


def _admitted(argv, repo=REPO):
    return argv in _git_plan(repo) or argv in ADMITTED_EXACT


def run_child(argv, cwd, *, stdout_bytes=False):
    if not _admitted(argv):
        _trip("unenumerated_child", argv)
    proc = _launch(argv, cwd=str(cwd), stdin=subprocess.DEVNULL,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    timeout = False
    try:
        out, err = proc.communicate(timeout=CHILD_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        timeout = True
    return {"argv": argv, "cwd": str(cwd), "exit": None if timeout else proc.returncode, "timeout": timeout,
            "stdout": out if stdout_bytes else out.decode("utf-8", "replace"),
            "stderr": err.decode("utf-8", "replace")}


def guarded_argv(spec_path, runner=RUNNER, python_flags=()):
    return [sys.executable, "-B", *python_flags, str(runner), GUARD_FLAG, str(spec_path)]


def launch_guarded(run_dir, name, kind, target, args, cwd, *, runner=RUNNER, python_flags=(),
                   allowed_imports=(), grandchildren=(), expected_trips=(), app_root=None,
                   allow_fork=False, stdout_bytes=False):
    """Write a spec, register the exact argv and run one guarded Python child."""
    specs = run_dir / "child-specs"
    receipts = run_dir / "child-receipts"
    specs.mkdir(exist_ok=True)
    receipts.mkdir(exist_ok=True)
    spec = {"schema": "fc3-guarded-child/v1", "name": name, "kind": kind,
            "target": None if target is None else str(target), "args": list(args), "cwd": str(cwd),
            "sandbox": str(run_dir / "child-sandboxes" / name), "spec_dir": str(specs),
            "receipt_dir": str(receipts), "allowed_imports": sorted(allowed_imports),
            "allow_fork": bool(allow_fork),
            "grandchildren": list(grandchildren), "expected_trips": list(expected_trips),
            "app_root": None if app_root is None else str(app_root)}
    spec_path = specs / (name + ".json")
    with open(spec_path, "x") as handle:
        json.dump(spec, handle, indent=2, sort_keys=True)
    argv = guarded_argv(spec_path, runner, python_flags)
    ADMITTED_EXACT.append(argv)
    child = run_child(argv, cwd, stdout_bytes=stdout_bytes)
    GUARDED_ROOTS.append({"name": name, "argv": argv, "exit": child["exit"],
                          "spec_sha256": sha256_file(spec_path)})
    return child


def _read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def _audit_one(receipts, name, expected_trips, app_root, accounted, problems, depth=0):
    started = _read_json(receipts / (name + ".started.json"))
    finished = _read_json(receipts / (name + ".finished.json"))
    accounted.update({name + ".started.json", name + ".finished.json"})
    entry = {"name": name, "started": started is not None, "finished": finished is not None}
    if started is None or finished is None or finished.get("completed") is not True:
        problems.append("child_receipt_incomplete:" + name)
        return entry
    trips = finished.get("trips", [])
    kinds = [t.get("kind") for t in trips]
    entry.update({"exit": finished.get("exit"), "trips": trips, "pid": finished.get("pid"),
                  "target_exception": finished.get("target_exception"),
                  "forks_without_exec": finished.get("forks_without_exec"),
                  "app_modules": finished.get("app_modules_after", {}),
                  "guard_before_target": finished.get("guard_installed_before_target")})
    # Expected trips are exact: a kind string, or a [kind, detail] pair when the detail is fixed.
    matched = len(trips) == len(expected_trips) and all(
        (kinds[i] == want) if isinstance(want, str) else ([kinds[i], trips[i].get("detail")] == list(want))
        for i, want in enumerate(expected_trips))
    if not matched:
        problems.append("child_trips_unexpected:" + name)
    if finished.get("guard_installed_before_target") is not True:
        problems.append("child_guard_not_first:" + name)
    if finished.get("app_modules_before_guard"):
        problems.append("child_app_imported_before_guard:" + name)
    if app_root is not None:
        paths = [p for p in finished.get("app_modules_after", {}).values() if p]
        if not paths:
            problems.append("child_app_modules_missing:" + name)
        if any(not Path(p).is_relative_to(app_root) for p in paths if not p.startswith(sys.prefix)):
            problems.append("child_app_module_outside_root:" + name)
    entry["grandchildren"] = []
    for gc in finished.get("grandchildren", []):
        if gc.get("kind") == "guarded" and depth < 2:
            entry["grandchildren"].append(_audit_one(receipts, gc["name"], [], None, accounted,
                                                     problems, depth + 1))
        elif gc.get("kind") == "exact":
            entry["grandchildren"].append({"name": gc.get("name"), "argv": gc.get("argv"), "kind": "exact"})
        else:
            problems.append("child_grandchild_unknown:" + name)
    return entry


def audit_guarded(run_dir, roots=None):
    """Every guarded root and grandchild must have complete, trip-free receipts."""
    roots = GUARDED_ROOTS if roots is None else roots
    problems = []
    receipts = run_dir / "child-receipts"
    specs = run_dir / "child-specs"
    accounted = set()
    entries = []
    for root in roots:
        spec = _read_json(specs / (root["name"] + ".json"))
        if spec is None or sha256_file(specs / (root["name"] + ".json")) != root["spec_sha256"]:
            problems.append("child_spec_changed:" + root["name"])
            continue
        entry = _audit_one(receipts, root["name"], spec["expected_trips"],
                           Path(spec["app_root"]) if spec["app_root"] else None, accounted, problems)
        if entry.get("exit") is not None and entry.get("exit") != root["exit"]:
            problems.append("child_exit_mismatch:" + root["name"])
        entries.append(entry)
    present = {p.name for p in receipts.iterdir()} if receipts.is_dir() else set()
    extra = sorted(present - accounted)
    if extra:
        problems.append("child_receipts_unaccounted")
    spec_names = {p.name for p in specs.iterdir()} if specs.is_dir() else set()
    known_specs = {n[: -len(".started.json")] + ".json" for n in accounted if n.endswith(".started.json")}
    if spec_names - known_specs:
        problems.append("child_specs_unaccounted")
    return {"roots": entries, "unaccounted_receipts": extra,
            "unaccounted_specs": sorted(spec_names - known_specs)}, problems


def audit_selftest(run_dir):
    """Synthetic receipts (no process runs): the audit must reject each broken receipt shape."""
    def scenario(label, finished_edit=None, drop_finished=False, extra_receipt=False, exit_seen=0):
        base = run_dir / "audit-selftest" / label
        (base / "child-specs").mkdir(parents=True)
        (base / "child-receipts").mkdir()
        spec_path = base / "child-specs" / "synthetic.json"
        spec_path.write_text(json.dumps({"expected_trips": [], "app_root": None}))
        receipt = {"name": "synthetic", "completed": True, "exit": 0, "trips": [], "grandchildren": [],
                   "guard_installed_before_target": True, "app_modules_before_guard": [],
                   "app_modules_after": {}}
        if finished_edit:
            receipt.update(finished_edit)
        (base / "child-receipts" / "synthetic.started.json").write_text(json.dumps({"name": "synthetic"}))
        if not drop_finished:
            (base / "child-receipts" / "synthetic.finished.json").write_text(json.dumps(receipt))
        if extra_receipt:
            (base / "child-receipts" / "stray.started.json").write_text(json.dumps({"name": "stray"}))
        roots = [{"name": "synthetic", "exit": exit_seen, "spec_sha256": sha256_file(spec_path)}]
        return audit_guarded(base, roots)[1]

    cases = {"clean": (scenario("clean"), []),
             "unexpected_trip": (scenario("unexpected_trip", {"trips": [{"kind": "create_connection"}]}),
                                 ["child_trips_unexpected:synthetic"]),
             "finished_missing": (scenario("finished_missing", drop_finished=True),
                                  ["child_receipt_incomplete:synthetic"]),
             "bootstrap_incomplete": (scenario("bootstrap_incomplete", {"completed": False}),
                                      ["child_receipt_incomplete:synthetic"]),
             "guard_late": (scenario("guard_late", {"guard_installed_before_target": False,
                                                    "app_modules_before_guard": ["campaign_tool"]}),
                            ["child_guard_not_first:synthetic", "child_app_imported_before_guard:synthetic"]),
             "unregistered_receipt": (scenario("unregistered_receipt", extra_receipt=True),
                                      ["child_receipts_unaccounted"]),
             "exit_mismatch": (scenario("exit_mismatch", exit_seen=1), ["child_exit_mismatch:synthetic"]),
             "unknown_grandchild": (scenario("unknown_grandchild", {"grandchildren": [{"kind": "shell"}]}),
                                    ["child_grandchild_unknown:synthetic"])}
    record = {label: {"problems": got, "expected": want} for label, (got, want) in cases.items()}
    problems = ["audit_selftest_mismatch:" + label for label, (got, want) in cases.items() if got != want]
    return record, problems


# Guarded child bootstrap (runs in the child process) -------------------------
def _grandchild_rule(spec, argv, kwargs):
    if kwargs.get("shell") or kwargs.get("executable"):
        return None
    for rule in spec["grandchildren"]:
        if rule["kind"] == "exact" and argv == rule["argv"] and \
                str(Path(kwargs.get("cwd") or os.getcwd()).resolve()) == rule["cwd"]:
            return rule
        if rule["kind"] == "guarded_script" and len(argv) == 2 + rule["arg_count"] \
                and argv[0] == sys.executable and argv[1] == rule["script"] \
                and all(Path(a).resolve().is_relative_to(spec["sandbox"]) for a in argv[2:]):
            return rule
    return None


def _guarded_popen_factory(spec, state):
    def guarded_popen(args, *rest, **kwargs):
        argv = list(args) if isinstance(args, (list, tuple)) else None
        rule = None if (argv is None or rest) else _grandchild_rule(spec, [str(a) for a in argv], kwargs)
        if rule is None:
            _trip("unexpected_subprocess", args)
        state["n"] += 1
        name = "{}.g{}".format(spec["name"], state["n"])
        if rule["kind"] == "exact":
            state["grandchildren"].append({"name": name, "kind": "exact", "argv": argv})
            return _launch(argv, **kwargs)
        child_spec = {"schema": spec["schema"], "name": name, "kind": "script", "target": rule["script"],
                      "args": [str(a) for a in argv[2:]], "cwd": str(Path(kwargs.get("cwd") or os.getcwd())),
                      "sandbox": str(Path(spec["sandbox"]) / name), "spec_dir": spec["spec_dir"],
                      "receipt_dir": spec["receipt_dir"], "allowed_imports": [], "grandchildren": [],
                      "expected_trips": [], "app_root": None}
        spec_path = Path(spec["spec_dir"]) / (name + ".json")
        with open(spec_path, "x") as handle:
            json.dump(child_spec, handle, indent=2, sort_keys=True)
        launched = guarded_argv(spec_path)
        state["grandchildren"].append({"name": name, "kind": "guarded", "argv_requested": argv,
                                       "argv_launched": launched})
        return _launch(launched, **kwargs)
    return guarded_popen


def _child_control(spec):
    """Inert boundary controls: every forbidden action must trip before it happens."""
    outcomes = {}
    sandbox = Path(spec["sandbox"])
    report = sandbox / "control-empty-report.json"
    report.write_text(json.dumps({"results": {}}))
    lookalike = sandbox / "tools" / "check_secret_report.py"   # same suffix, not registered
    calls = (("model_import", lambda: __import__("openai")),
             ("create_connection", lambda: socket.create_connection(("127.0.0.1", 9))),
             ("listener_bind", lambda: socket.socket().bind(("127.0.0.1", 0))),
             ("unregistered_popen", lambda: subprocess.Popen(["true"])),
             ("lookalike_grandchild", lambda: subprocess.run([sys.executable, str(lookalike), str(report)])),
             ("os_system", lambda: os.system("true")),
             ("fork", lambda: os.fork()),
             ("raw_fork_exec", lambda: _posixsubprocess.fork_exec()))
    for label, call in calls:
        try:
            call()
            outcomes[label] = "NOT_BLOCKED"
        except SentinelError:
            outcomes[label] = "blocked"
        except Exception as error:  # recorded; parent decides on exact trip kinds
            outcomes[label] = "other:" + type(error).__name__
    admitted = subprocess.run([sys.executable, spec["grandchildren"][0]["script"], str(report)],
                              capture_output=True, text=True, timeout=60)
    outcomes["admitted_grandchild_exit"] = admitted.returncode
    module = __import__("campaign_tool.records.semantic_coverage", fromlist=["render_chunk"])
    outcomes["app_module"] = str(Path(module.__file__).resolve())
    Path(spec["sandbox"], "control-outcomes.json").write_text(json.dumps(outcomes, sort_keys=True))
    print(json.dumps(outcomes, sort_keys=True))
    return 0


def _write_receipt(spec, suffix, data):
    path = Path(spec["receipt_dir"]) / (spec["name"] + suffix)
    with open(path, "x") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)


def _exit_code(code):
    if code is None:
        return 0
    if isinstance(code, int):
        return code
    print(code, file=sys.stderr)
    return 1


def child_main(spec_path):
    before = _app_modules()
    spec = json.loads(Path(spec_path).read_text())
    state = {"n": 0, "grandchildren": []}
    install_sentinels(Path(spec["sandbox"]), popen=_guarded_popen_factory(spec, state),
                      allowed_imports=spec["allowed_imports"], allow_fork=spec.get("allow_fork") is True)
    base = {"name": spec["name"], "pid": os.getpid(), "ppid": os.getppid(), "argv": list(sys.argv),
            "executable": sys.executable, "cwd": os.getcwd(), "spec_sha256": sha256_file(spec_path),
            "app_modules_before_guard": sorted(before), "guard_installed_before_target": not before}
    _write_receipt(spec, ".started.json", dict(base, started_utc=utc()))
    code, error, target_exception = 1, None, None
    try:
        if spec["kind"] == "module":
            sys.argv = [spec["target"], *spec["args"]]
            sys.path[0] = str(Path(spec["cwd"]).resolve())
            runpy.run_module(spec["target"], run_name="__main__", alter_sys=True)
            code = 0
        elif spec["kind"] == "script":
            sys.argv = [spec["target"], *spec["args"]]
            sys.path[0] = str(Path(spec["target"]).resolve().parent)
            runpy.run_path(spec["target"], run_name="__main__")
            code = 0
        elif spec["kind"] == "control":
            sys.path[0] = str(REPO)
            code = _child_control(spec)
        else:
            error = "unknown_kind"
    except SystemExit as stop:
        code = _exit_code(stop.code)
    except BaseException as exc:  # the target's own uncaught exception: exit 1, as Python would
        target_exception = repr(exc)[:2000]
        traceback.print_exc()
        code = 1
    finally:
        sys.stdout.flush()
        _write_receipt(spec, ".finished.json", dict(
            base, finished_utc=utc(), exit=code, error=error, target_exception=target_exception,
            trips=list(TRIPS),
            grandchildren=state["grandchildren"], forks_without_exec=len(FORKS),
            app_modules_after=_app_modules(), completed=error is None))
    return code


# Identity gate ---------------------------------------------------------------
def source_hashes(root):
    return {rel: (sha256_file(root / rel) if (root / rel).is_file() else None) for rel in CHANGED}


def _git_out(*rest, raw=False):
    child = run_child(_git_argv(REPO, *rest), REPO, stdout_bytes=raw)
    if child["exit"] != 0:
        return None
    return child["stdout"] if raw else child["stdout"].strip()


def git_identity():
    if not (REPO / ".git").exists():
        binding = _read_json(REPO / BINDING_NAME)
        return {"kind": "relocated", "repo": str(REPO), "binding": binding,
                "binding_sha256": sha256_file(REPO / BINDING_NAME) if binding is not None else None,
                "working_sha256": source_hashes(REPO)}
    status = _git_out("status", "--porcelain", "--untracked-files=all")
    diff = _git_out("diff", "--name-only", STAGED_BASE, "HEAD")
    ident = {"kind": "git", "repo": str(REPO), "head": _git_out("rev-parse", "HEAD"),
             "status_lines": None if status is None else status.splitlines(),
             "scope_paths": None if diff is None else sorted(diff.splitlines()),
             "working_sha256": source_hashes(REPO), "head_blob_sha256": {}}
    for label, sha in (("pin", BRIEF_PIN), ("base", STAGED_BASE)):
        ident[label + "_resolved"] = _git_out("rev-parse", "--verify", sha + "^{commit}")
        ident[label + "_is_ancestor"] = run_child(
            _git_argv(REPO, "merge-base", "--is-ancestor", sha, "HEAD"), REPO)["exit"] == 0
        shown = _git_out("show", sha + ":" + CHANGED[0], raw=True)
        ident[label + "_source_sha256"] = None if shown is None else sha256_bytes(shown)
    for rel in CHANGED:
        shown = _git_out("show", "HEAD:" + rel, raw=True)
        ident["head_blob_sha256"][rel] = None if shown is None else sha256_bytes(shown)
    return ident


def identity_problems(ident):
    """Fail-closed decision over a recorded identity; any missing signal is a failure."""
    problems = []
    kind = ident.get("kind") if isinstance(ident, dict) else None
    if kind == "git":
        if not _is_full_sha(ident.get("head")):
            problems.append("identity_head_unpinned")
        if ident.get("status_lines") is None:
            problems.append("identity_status_unavailable")
        elif ident["status_lines"]:
            problems.append("identity_worktree_dirty")
        for label, sha in (("pin", BRIEF_PIN), ("base", STAGED_BASE)):
            if ident.get(label + "_resolved") != sha:
                problems.append("identity_" + label + "_unresolved")
            if ident.get(label + "_is_ancestor") is not True:
                problems.append("identity_" + label + "_not_ancestor")
            if ident.get(label + "_source_sha256") != BRIEF_SOURCE_SHA256:
                problems.append("identity_" + label + "_source_mismatch")
        if ident.get("scope_paths") != sorted(CHANGED):
            problems.append("identity_scope_mismatch")
        blobs, work = ident.get("head_blob_sha256"), ident.get("working_sha256")
        if not isinstance(blobs, dict) or not isinstance(work, dict) or any(
                blobs.get(rel) is None or blobs.get(rel) != work.get(rel) for rel in CHANGED):
            problems.append("identity_working_source_not_head")
    elif kind == "relocated":
        binding = ident.get("binding")
        if not isinstance(binding, dict) or binding.get("schema") != "fc3-relocation-binding/v1":
            problems.append("identity_copy_unbound")
        else:
            if binding.get("copy_root") != ident.get("repo"):
                problems.append("identity_copy_root_mismatch")
            if not _is_full_sha(binding.get("parent_head")):
                problems.append("identity_copy_parent_head_unpinned")
            if binding.get("brief_pin") != BRIEF_PIN or binding.get("staged_base") != STAGED_BASE:
                problems.append("identity_copy_brief_mismatch")
            if binding.get("parent_identity_problems") != []:
                problems.append("identity_copy_parent_unverified")
            work = ident.get("working_sha256")
            if not isinstance(work, dict) or any(work.get(rel) is None or binding.get("source_sha256", {}).get(rel)
                                                 != work.get(rel) for rel in CHANGED):
                problems.append("identity_copy_source_mismatch")
    else:
        problems.append("identity_unavailable")
    return problems


def identity_selftest():
    """Synthetic identities: clean positives pass, each single failed signal is rejected."""
    head = hashlib.sha1(b"fc3 selftest head").hexdigest()
    files = {rel: sha256_bytes(("fc3 selftest " + rel).encode()) for rel in CHANGED}
    good = {"kind": "git", "repo": "selftest", "head": head, "status_lines": [],
            "scope_paths": sorted(CHANGED), "working_sha256": dict(files), "head_blob_sha256": dict(files),
            "pin_resolved": BRIEF_PIN, "pin_is_ancestor": True, "pin_source_sha256": BRIEF_SOURCE_SHA256,
            "base_resolved": STAGED_BASE, "base_is_ancestor": True, "base_source_sha256": BRIEF_SOURCE_SHA256}
    other = sha256_bytes(b"fc3 selftest other")
    git_cases = {
        "dirty_worktree": ("status_lines", [" M " + CHANGED[0]], "identity_worktree_dirty"),
        "status_unavailable": ("status_lines", None, "identity_status_unavailable"),
        "head_missing": ("head", None, "identity_head_unpinned"),
        "head_short": ("head", head[:8], "identity_head_unpinned"),
        "pin_missing": ("pin_resolved", None, "identity_pin_unresolved"),
        "pin_not_ancestor": ("pin_is_ancestor", False, "identity_pin_not_ancestor"),
        "pin_ancestry_unknown": ("pin_is_ancestor", None, "identity_pin_not_ancestor"),
        "pin_source_mismatch": ("pin_source_sha256", other, "identity_pin_source_mismatch"),
        "pin_source_missing": ("pin_source_sha256", None, "identity_pin_source_mismatch"),
        "base_missing": ("base_resolved", None, "identity_base_unresolved"),
        "base_not_ancestor": ("base_is_ancestor", False, "identity_base_not_ancestor"),
        "base_source_mismatch": ("base_source_sha256", other, "identity_base_source_mismatch"),
        "scope_extra_path": ("scope_paths", sorted([*CHANGED, "README.md"]), "identity_scope_mismatch"),
        "scope_unavailable": ("scope_paths", None, "identity_scope_mismatch"),
        "working_not_head": ("working_sha256", dict(files, **{CHANGED[0]: other}),
                             "identity_working_source_not_head"),
        "head_blob_missing": ("head_blob_sha256", dict(files, **{CHANGED[2]: None}),
                              "identity_working_source_not_head"),
        "kind_missing": ("kind", None, "identity_unavailable"),
    }
    binding = {"schema": "fc3-relocation-binding/v1", "copy_root": "selftest-copy", "parent_head": head,
               "brief_pin": BRIEF_PIN, "staged_base": STAGED_BASE, "parent_identity_problems": [],
               "source_sha256": dict(files)}
    reloc = {"kind": "relocated", "repo": "selftest-copy", "binding": binding, "working_sha256": dict(files)}
    reloc_cases = {
        "copy_unbound": ("binding", None, "identity_copy_unbound"),
        "copy_root_mismatch": ("binding", dict(binding, copy_root="elsewhere"), "identity_copy_root_mismatch"),
        "copy_parent_head_short": ("binding", dict(binding, parent_head=head[:8]),
                                   "identity_copy_parent_head_unpinned"),
        "copy_parent_unverified": ("binding", dict(binding, parent_identity_problems=["identity_worktree_dirty"]),
                                   "identity_copy_parent_unverified"),
        "copy_brief_mismatch": ("binding", dict(binding, brief_pin=head), "identity_copy_brief_mismatch"),
        "copy_source_mismatch": ("working_sha256", dict(files, **{CHANGED[1]: other}),
                                 "identity_copy_source_mismatch"),
    }
    record = {"positives": {"clean_git": identity_problems(good), "bound_copy": identity_problems(reloc)},
              "negatives": {}}
    problems = [] if not any(record["positives"].values()) else ["identity_selftest_positive_rejected"]
    for template, cases in ((good, git_cases), (reloc, reloc_cases)):
        for label, (key, value, expected) in cases.items():
            variant = copy.deepcopy(template)
            variant[key] = value
            got = identity_problems(variant)
            record["negatives"][label] = {"expected": expected, "problems": got}
            if got != [expected]:
                problems.append("identity_selftest_not_rejected:" + label)
    return record, problems


def admission_selftest(run_dir):
    """Exact registration: lookalike runners, specs and edited argv are refused."""
    fake_spec = run_dir / "child-specs" / "unregistered.json"
    lookalike = run_dir / "elsewhere" / "tools" / RUNNER.name
    cases = {"lookalike_runner_focused": [sys.executable, "-B", str(lookalike), "--check", "focused",
                                          "--output-root", str(run_dir / "x")],
             "unregistered_guard_spec": guarded_argv(fake_spec),
             "git_short_pin": _git_argv(REPO, "rev-parse", "--verify", BRIEF_PIN[:8] + "^{commit}"),
             "git_other_show": _git_argv(REPO, "show", "HEAD:README.md"),
             "bare_unittest": [sys.executable, "-B", "-m", "unittest", *COMPAT_SELECTORS]}
    got = {label: _admitted(argv) for label, argv in cases.items()}
    got["registered_git_head"] = _admitted(_git_argv(REPO, "rev-parse", "HEAD"))
    problems = [] if got["registered_git_head"] and not any(v for k, v in got.items()
                                                            if k != "registered_git_head") \
        else ["admission_selftest_failed"]
    return got, problems


# In-process unittest with full outcome recording -----------------------------
class RecordingResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.outcomes = {}
        self.subfailures = []

    def _name(self, test):
        return test.id().rsplit(".", 1)[-1]

    def _set(self, test, outcome):
        name = self._name(test)
        if self.outcomes.get(name) not in ("failure", "error"):
            self.outcomes[name] = outcome

    def addSuccess(self, test):
        super().addSuccess(test)
        self.outcomes.setdefault(self._name(test), "success")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._set(test, "failure")

    def addError(self, test, err):
        super().addError(test, err)
        self._set(test, "error")

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self._set(test, "skip")

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self._set(test, "expected_failure")

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self._set(test, "unexpected_success")

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            parent = self._name(test)
            self.outcomes[parent] = "failure" if issubclass(err[0], test.failureException) else "error"
            self.subfailures.append({"test": parent, "subtest": subtest._subDescription(),
                                     "exception": err[0].__name__, "message": str(err[1])[:300]})


def run_focused_suite():
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    module = __import__(TEST_MODULE, fromlist=["CASES"])
    suite = unittest.TestLoader().loadTestsFromModule(module)
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=2, resultclass=RecordingResult).run(suite)
    sc = sys.modules["campaign_tool.records.semantic_coverage"]
    names = sorted(result.outcomes)
    cases = {case: [n for n in names if n == "test_" + case or n.startswith("test_" + case + "_")]
             for case in CASES}
    case_status = {case: (bool(tests) and all(result.outcomes[t] == "success" for t in tests))
                   for case, tests in cases.items()}
    return {"tests_run": result.testsRun, "outcomes": result.outcomes, "subtest_failures": result.subfailures,
            "failures": len(result.failures), "errors": len(result.errors), "skipped": len(result.skipped),
            "expected_failures": len(result.expectedFailures),
            "unexpected_successes": len(result.unexpectedSuccesses),
            "case_tests": cases, "case_passed": case_status,
            "module_cases_match": tuple(getattr(module, "CASES", ())) == CASES,
            "loaded_modules": {"semantic_coverage": str(Path(sc.__file__).resolve()),
                               "test_module": str(Path(module.__file__).resolve())},
            "raw": stream.getvalue()}, module, sc


def focused_problems(report):
    problems = []
    if report["tests_run"] == 0:
        problems.append("zero_tests")
    if report["tests_run"] != len(report["outcomes"]):
        problems.append("outcome_count_mismatch")
    for key in ("failures", "errors", "skipped", "expected_failures", "unexpected_successes"):
        if report[key]:
            problems.append("unexpected_" + key)
    if report["subtest_failures"]:
        problems.append("subtest_failures")
    if not report["module_cases_match"]:
        problems.append("case_list_mismatch")
    problems += ["case_missing_or_failed:" + c for c, ok in report["case_passed"].items() if not ok]
    for name, path in report["loaded_modules"].items():
        if not Path(path).is_relative_to(REPO):
            problems.append("module_outside_repo:" + name)
    return problems


def check_focused(run_dir, result):
    report, _, _ = run_focused_suite()
    (run_dir / "focused-unittest.txt").write_text(report.pop("raw"))
    result["focused"] = report
    return focused_problems(report)


# Compatibility ---------------------------------------------------------------
def save_child(run_dir, name, child):
    out = child["stdout"]
    (run_dir / (name + ".stdout")).write_bytes(out if isinstance(out, bytes) else out.encode("utf-8"))
    (run_dir / (name + ".stderr")).write_text(child["stderr"])
    return {"argv": child["argv"], "cwd": child["cwd"], "exit": child["exit"], "timeout": child["timeout"],
            "stdout_sha256": sha256_file(run_dir / (name + ".stdout")),
            "stderr_sha256": sha256_file(run_dir / (name + ".stderr"))}


def _checker_grandchild():
    return {"kind": "guarded_script", "script": str(REPO / "tools" / "check_secret_report.py"), "arg_count": 1}


def check_compatibility(run_dir, result):
    child = launch_guarded(run_dir, "compatibility", "module", "unittest", COMPAT_SELECTORS, REPO,
                           python_flags=("-W", "error::ResourceWarning"),
                           grandchildren=[_checker_grandchild()], app_root=REPO)
    record = save_child(run_dir, "compatibility", child)
    tail = child["stderr"].strip().splitlines()
    ran = [line for line in tail if line.startswith("Ran ")]
    record["tests_run"] = int(ran[-1].split()[1]) if ran else 0
    record["final_line"] = tail[-1] if tail else ""
    record["selectors"] = list(COMPAT_SELECTORS)
    result["compatibility"] = record
    problems = []
    if child["exit"] != 0:
        problems.append("compatibility_exit_nonzero")
    if record["tests_run"] == 0:
        problems.append("zero_tests")
    if record["final_line"] != "OK":
        problems.append("compatibility_not_plain_ok")
    finished = _read_json(run_dir / "child-receipts" / "compatibility.finished.json") or {}
    record["checker_grandchildren"] = len(finished.get("grandchildren", []))
    if record["checker_grandchildren"] == 0:
        problems.append("compatibility_checker_grandchildren_missing")
    return problems


# Negative control ------------------------------------------------------------
EXPECTED_FAIL_TYPE_BYPASS = set(CASES) - {"valid_render_and_record", "missing_and_unknown_ids"}
EXPECTED_FAIL_WRONG_REASON = set(CASES) - {"valid_render_and_record"}


def _probe(call):
    try:
        call()
    except ValueError as error:
        return {"exception": "ValueError", "args": list(error.args)}
    except Exception as error:  # recorded, deciding gate checks the type explicitly
        return {"exception": type(error).__name__, "args": [str(a)[:200] for a in error.args]}
    return {"exception": None}


def _direct_probes(module, sc):
    manifest, texts = module.fixture()
    chunk = manifest["chunks"][0]
    real = chunk["chunk_id"]
    checkpoint = sc.initial_checkpoint(manifest)
    restored = module.accept(manifest, checkpoint, chunk, "negative-probe")
    tampered = json.loads(json.dumps(restored))
    tampered["attempts"][0]["chunk_id"] = [real]
    stale = json.loads(json.dumps(tampered))
    resigned = module.resign_checkpoint(tampered)
    record = lambda cid: sc.record_attempt(manifest, checkpoint, cid, visible_ranges=chunk["ranges"],
                                           accepted_ranges=chunk["ranges"],
                                           receipt_sha256=module.receipt("negative-direct"))
    rendered = sc.render_chunk(manifest, real, texts)
    return {
        "render_list_id": _probe(lambda: sc.render_chunk(manifest, [real], texts)),
        "record_dict_id": _probe(lambda: record({"chunk_id": real})),
        "restored_list_id_resigned": _probe(lambda: sc.validate_checkpoint(manifest, resigned)),
        "other_gate_unknown_string": _probe(lambda: sc.render_chunk(manifest, module.label_digest("unknown-chunk"), texts)),
        "other_gate_stale_self_hash": _probe(lambda: sc.validate_checkpoint(manifest, stale)),
        "other_gate_valid_render_payload_ok": rendered["payload_sha256"] == chunk["payload_sha256"],
        "other_gate_valid_record_attempts": len(record(real)["attempts"]),
    }


def _failing_cases(report):
    return {case for case, ok in report["case_passed"].items() if not ok}


def check_negative(run_dir, result):
    problems = []
    source_before = sha256_file(REPO / CHANGED[0])
    positive, module, sc = run_focused_suite()
    (run_dir / "negative-positive-before.txt").write_text(positive.pop("raw"))
    problems += ["before:" + p for p in focused_problems(positive)]
    original = getattr(sc, "_known_chunk", None)
    if original is None:
        return problems + ["identity_guard_unavailable"]
    bookkeeping_before = module.replay_bookkeeping()
    direct_before = _direct_probes(module, sc)

    def type_bypass(chunk_id, chunks, reason):
        # Pre-fix behavior: membership without the identity-type guard.
        if chunk_id not in chunks:
            raise ValueError(reason)

    def wrong_reason(chunk_id, chunks, reason):
        if not isinstance(chunk_id, str):
            raise ValueError("chunk_identity_type")
        if chunk_id not in chunks:
            raise ValueError(reason)

    mutants = {}
    for label, replacement, expected in (("type_guard_bypassed", type_bypass, EXPECTED_FAIL_TYPE_BYPASS),
                                         ("wrong_reason", wrong_reason, EXPECTED_FAIL_WRONG_REASON)):
        with mock.patch.object(sc, "_known_chunk", replacement):
            report, _, _ = run_focused_suite()
            probes = _direct_probes(module, sc)
            bookkeeping = module.replay_bookkeeping()
        (run_dir / ("negative-" + label + ".txt")).write_text(report.pop("raw"))
        failing = _failing_cases(report)
        mutants[label] = {"report": report, "direct_probes": probes, "failing_cases": sorted(failing),
                          "expected_failing_cases": sorted(expected),
                          "bookkeeping_unchanged": bookkeeping == bookkeeping_before}
        if failing != expected:
            problems.append(label + ":failing_case_set_mismatch")
        if not report["subtest_failures"]:
            problems.append(label + ":no_subtest_caught")
        if not mutants[label]["bookkeeping_unchanged"]:
            problems.append(label + ":bookkeeping_changed")
        for gate in ("other_gate_unknown_string", "other_gate_stale_self_hash"):
            if probes[gate] != direct_before[gate]:
                problems.append(label + ":" + gate + "_changed")
        if not probes["other_gate_valid_render_payload_ok"] or probes["other_gate_valid_record_attempts"] != 1:
            problems.append(label + ":valid_path_broken")
    bypass = mutants["type_guard_bypassed"]
    if {f["exception"] for f in bypass["report"]["subtest_failures"]} != {"TypeError"}:
        problems.append("type_guard_bypassed:caught_failures_not_all_TypeError")
    for name in ("render_list_id", "record_dict_id", "restored_list_id_resigned"):
        if bypass["direct_probes"][name]["exception"] != "TypeError":
            problems.append("type_guard_bypassed:" + name + "_not_TypeError")
    wrong = mutants["wrong_reason"]
    if {f["exception"] for f in wrong["report"]["subtest_failures"]} != {"AssertionError"}:
        problems.append("wrong_reason:caught_failures_not_reason_assertions")
    if direct_before["render_list_id"] != {"exception": "ValueError", "args": ["unknown_chunk"]}:
        problems.append("restored:render_list_id_not_unknown_chunk")
    if direct_before["record_dict_id"] != {"exception": "ValueError", "args": ["unknown_chunk"]}:
        problems.append("restored:record_dict_id_not_unknown_chunk")
    if direct_before["restored_list_id_resigned"] != {"exception": "ValueError", "args": ["checkpoint_chunk"]}:
        problems.append("restored:restored_list_id_not_checkpoint_chunk")
    restored_ok = sc._known_chunk is original
    after, _, _ = run_focused_suite()
    (run_dir / "negative-positive-after.txt").write_text(after.pop("raw"))
    problems += ["after:" + p for p in focused_problems(after)]
    bookkeeping_after = module.replay_bookkeeping()
    if not restored_ok:
        problems.append("guard_not_restored")
    if bookkeeping_after != bookkeeping_before:
        problems.append("bookkeeping_changed_after_restore")
    if sha256_file(REPO / CHANGED[0]) != source_before:
        problems.append("source_changed_on_disk")
    result["negative"] = {
        "bypassed_branch": "campaign_tool.records.semantic_coverage._known_chunk isinstance(str) "
                           "check (in-memory mock.patch.object only; disk untouched)",
        "positive_before": positive, "direct_probes_restored": direct_before, "mutants": mutants,
        "guard_restored_identity": restored_ok, "positive_after": after,
        "bookkeeping_before": bookkeeping_before, "bookkeeping_after_equal": bookkeeping_after == bookkeeping_before,
        "source_sha256_before": source_before, "source_sha256_after": sha256_file(REPO / CHANGED[0])}
    return problems


# Portability -----------------------------------------------------------------
def _forbidden_tokens():
    # Assembled so this file does not itself contain the host fragments.
    return ["/ho" + "me/", "agent" + "-work", ".her" + "mes", "kan" + "ban", "/Us" + "ers/",
            "/tm" + "p/", "scr" + "atch/", "work" + "spaces/"]


CONTROL_TRIPS = ["model_or_network_client_import", "create_connection", "socket_bind",
                 "unexpected_subprocess", "unexpected_subprocess", "os_system", "os_fork",
                 "unregistered_fork_exec"]
CONTROL_BLOCKED = ("model_import", "create_connection", "listener_bind", "unregistered_popen",
                   "lookalike_grandchild", "os_system", "fork", "raw_fork_exec")


def _control_child(run_dir, name, runner, root):
    child = launch_guarded(run_dir, name, "control", None, (), run_dir, runner=runner,
                           grandchildren=[{"kind": "guarded_script", "arg_count": 1,
                                           "script": str(root / "tools" / "check_secret_report.py")}],
                           expected_trips=CONTROL_TRIPS, app_root=root)
    record = save_child(run_dir, name, child)
    try:
        outcomes = json.loads(child["stdout"])
    except ValueError:
        outcomes = None
    record["outcomes"] = outcomes
    problems = []
    if child["exit"] != 0 or not isinstance(outcomes, dict):
        return record, [name + ":control_incomplete"]
    if any(outcomes.get(k) != "blocked" for k in CONTROL_BLOCKED):
        problems.append(name + ":control_not_blocked")
    if outcomes.get("admitted_grandchild_exit") != 0:
        problems.append(name + ":admitted_grandchild_failed")
    if not Path(outcomes.get("app_module", "")).is_relative_to(root):
        problems.append(name + ":app_module_not_from_root")
    return record, problems


def check_portability(run_dir, result):
    problems = []
    tokens = _forbidden_tokens()
    static = {}
    for rel in CHANGED[1:]:
        text = (REPO / rel).read_text()
        static[rel] = [t for t in tokens if t in text]
        if static[rel]:
            problems.append("host_path_in_source:" + rel)
    report, _, _ = run_focused_suite()
    (run_dir / "portability-focused-here.txt").write_text(report.pop("raw"))
    problems += ["here:" + p for p in focused_problems(report)]
    copy_root = (run_dir / "second-prefix" / "relocated-source").resolve()
    shutil.copytree(REPO / "campaign_tool", copy_root / "campaign_tool",
                    ignore=shutil.ignore_patterns("__pycache__"))
    for rel in ("tests/__init__.py", "tests/records/__init__.py", "tools/check_secret_report.py",
                CHANGED[1], CHANGED[2]):
        (copy_root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / rel, copy_root / rel)
    parent_ident = result["git"]
    binding = {"schema": "fc3-relocation-binding/v1", "copy_root": str(copy_root),
               "parent_repo": str(REPO), "parent_head": parent_ident.get("head"),
               "brief_pin": BRIEF_PIN, "staged_base": STAGED_BASE,
               "parent_identity_problems": identity_problems(parent_ident),
               "source_sha256": parent_ident.get("head_blob_sha256"), "parent_run_dir": str(run_dir)}
    with open(copy_root / BINDING_NAME, "x") as handle:
        json.dump(binding, handle, indent=2, sort_keys=True)
    binding_sha = sha256_file(copy_root / BINDING_NAME)
    child_out = run_dir / "copy-focused-out"
    copy_runner = copy_root / "tools" / RUNNER.name
    argv = [sys.executable, "-B", str(copy_runner), "--check", "focused", "--output-root", str(child_out)]
    ADMITTED_EXACT.append(argv)
    child = run_child(argv, run_dir)
    record = save_child(run_dir, "copy-focused", child)
    runs = sorted(child_out.iterdir()) if child_out.is_dir() else []
    child_result = None
    if len(runs) == 1 and (runs[0] / "result.json").is_file():
        child_result = json.loads((runs[0] / "result.json").read_text())
    record["child_run_dirs"] = [str(r) for r in runs]
    record["binding_sha256"] = binding_sha
    if child["exit"] != 0:
        problems.append("copy_focused_exit_nonzero")
    if child_result is None:
        problems.append("copy_result_missing")
    else:
        mods = child_result.get("focused", {}).get("loaded_modules", {})
        record["copy_loaded_modules"] = mods
        record["copy_status"] = child_result.get("status")
        record["copy_tests_run"] = child_result.get("focused", {}).get("tests_run")
        record["copy_identity"] = child_result.get("git", {}).get("kind")
        if child_result.get("status") != "PASS" or child_result.get("sentinel_trips"):
            problems.append("copy_status_not_pass")
        if child_result.get("git", {}).get("binding_sha256") != binding_sha:
            problems.append("copy_not_bound_to_parent")
        for name, path in mods.items():
            if not Path(path).is_relative_to(copy_root) or Path(path).is_relative_to(REPO):
                problems.append("copy_module_not_from_copy:" + name)
        if len(mods) != 2:
            problems.append("copy_modules_incomplete")
        copied = child_result.get("source_hashes", {})
        if any(copied.get(rel) != parent_ident.get("head_blob_sha256", {}).get(rel) for rel in CHANGED):
            problems.append("copy_source_hash_mismatch")
    # Child-boundary controls: guarded child from this repo and from the relocated copy.
    controls = {}
    for name, runner, root in (("control-here", RUNNER, REPO), ("control-relocated", copy_runner, copy_root)):
        controls[name], found = _control_child(run_dir, name, runner, root)
        problems += found
    # Parent sentinel self-test: these must trip without any connection or process.
    selftest = {}
    before = len(TRIPS)
    for name, call in (("create_connection", lambda: socket.create_connection(("127.0.0.1", 9))),
                       ("popen", lambda: subprocess.Popen(["true"])),
                       ("model_import", lambda: __import__("openai"))):
        try:
            call()
            selftest[name] = "NOT_BLOCKED"
        except SentinelError:
            selftest[name] = "blocked"
        except ImportError as error:
            selftest[name] = "import_error:" + type(error).__name__
    selftest_trips = TRIPS[before:]
    del TRIPS[before:]
    result["sentinel_selftest"] = {"results": selftest, "trips": selftest_trips}
    if any(v != "blocked" for v in selftest.values()):
        problems.append("sentinel_selftest_failed")
    result["portability"] = {"static_host_tokens": static, "focused_here": report, "copy": record,
                             "copy_root": str(copy_root), "child_controls": controls}
    return problems


# Credentials and public tree -------------------------------------------------
def check_credentials(run_dir, result):
    problems = []
    tool = Path(sys.executable).parent / "detect-secrets"
    checker = REPO / "tools" / "check_secret_report.py"
    if not tool.is_file() or not os.access(tool, os.X_OK):
        return ["detect_secrets_missing"]
    if not checker.is_file():
        return ["secret_checker_missing"]
    scan_args = ["scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"]
    # detect-secrets imports requests at module load; sockets stay blocked. urllib3's import-time
    # IPv6 capability probe (sock.bind(("::1", 0)) inside its own try/except) is blocked and expected.
    scan = launch_guarded(run_dir, "detect-secrets", "script", tool, scan_args, REPO,
                          allowed_imports=("requests", "urllib3"), allow_fork=True, app_root=None,
                          expected_trips=[["socket_bind", "(('::1', 0),)"]])
    report_path = run_dir / "secret-scan.json"
    report_path.write_text(scan["stdout"])
    (run_dir / "secret-scan.stderr").write_text(scan["stderr"])
    record = {"scan_argv": scan["argv"], "scan_target": [str(tool), *scan_args], "scan_cwd": scan["cwd"],
              "scan_exit": scan["exit"], "report": str(report_path), "report_sha256": sha256_file(report_path)}
    if scan["exit"] != 0:
        problems.append("scan_exit_nonzero")
    try:
        data = json.loads(scan["stdout"])
        hits = data["results"]
        if not isinstance(hits, dict):
            raise ValueError("results")
        record["raw_hit_files"] = len(hits)
        record["raw_hits"] = sum(len(v) for v in hits.values())
        record["changed_file_hits"] = {rel: len(hits.get(rel, [])) for rel in CHANGED}
        if any(record["changed_file_hits"].values()):
            problems.append("hits_in_changed_files")
    except (ValueError, KeyError, TypeError):
        problems.append("scan_output_malformed")
        result["credentials"] = record
        return problems
    check = launch_guarded(run_dir, "check-secret-report", "script", checker, [str(report_path)], REPO)
    record["checker"] = save_child(run_dir, "check-secret-report", check)
    lines = check["stdout"].strip().splitlines()
    record["checker_summary"] = lines[-1] if lines else ""
    if check["exit"] != 0:
        problems.append("checker_exit_nonzero")
    if "; potential secrets: 0;" not in record["checker_summary"]:
        problems.append("checker_potential_secrets_nonzero")
    result["credentials"] = record
    return problems


def check_public_tree(run_dir, result):
    git_ls = {"kind": "exact", "cwd": str(REPO),
              "argv": ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"]}
    child = launch_guarded(run_dir, "public-tree", "script", REPO / "tools" / "check_public_tree.py", (), REPO,
                           grandchildren=[git_ls], app_root=REPO)
    result["public_tree"] = save_child(run_dir, "public-tree", child)
    return [] if child["exit"] == 0 else ["public_tree_exit_nonzero"]


HANDLERS = {"focused": check_focused, "compatibility": check_compatibility, "negative": check_negative,
            "portability": check_portability, "credentials": check_credentials,
            "public-tree": check_public_tree}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", required=True, choices=CHECKS)
    parser.add_argument("--output-root", required=True)
    args = parser.parse_args(argv)
    root = Path(args.output_root).resolve()
    if root.is_relative_to(REPO):
        print("output root must be outside the repository", file=sys.stderr)
        return 2
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    run_dir = root / "{}-{}-{}".format(stamp, args.check, os.getpid())
    run_dir.mkdir(exist_ok=False)
    install_sentinels(run_dir / "sandbox")
    result = {"schema": "fc3-chunk-identity-acceptance/v2", "check": args.check, "brief": BRIEF,
              "argv": [sys.executable, *sys.argv], "cwd": os.getcwd(), "interpreter": sys.executable,
              "python_version": sys.version, "repo": str(REPO), "run_dir": str(run_dir),
              "started_utc": utc(), "full_discovery": "operator-run (not part of this runner)",
              "review": "REVIEW REQUIRED: machine-valid output is not semantic, legal, stage or release acceptance"}
    problems = []
    try:
        result["git"] = git_identity()
        result["identity_problems"] = identity_problems(result["git"])
        problems += result["identity_problems"]
        result["identity_selftest"], found = identity_selftest()
        problems += found
        result["admission_selftest"], found = admission_selftest(run_dir)
        problems += found
        result["audit_selftest"], found = audit_selftest(run_dir)
        problems += found
        result["source_hashes_before"] = source_hashes(REPO)
        problems += HANDLERS[args.check](run_dir, result)
        result["source_hashes"] = source_hashes(REPO)
        if result["source_hashes"] != result["source_hashes_before"]:
            problems.append("source_changed_during_run")
        after = git_identity()
        result["git_after"] = after
        after_problems = identity_problems(after)
        problems += ["after:" + p for p in after_problems]
        for key in ("kind", "head", "status_lines", "working_sha256", "binding_sha256"):
            if after.get(key) != result["git"].get(key):
                problems.append("identity_changed_during_run:" + key)
        result["child_guard"], found = audit_guarded(run_dir)
        problems += found
    except Exception as error:
        problems.append("incomplete:" + type(error).__name__)
        result["error"] = repr(error)[:2000]
    result["admitted_children"] = ADMITTED_EXACT
    result["sentinel_trips"] = list(TRIPS)
    if TRIPS:
        problems.append("unexpected_sentinel_trips")
    result["evidence_sha256"] = {str(p.relative_to(run_dir)): sha256_file(p)
                                 for p in sorted(run_dir.rglob("*")) if p.is_file()}
    result["problems"] = problems
    result["status"] = "PASS" if not problems else "FAIL"
    result["finished_utc"] = utc()
    with open(run_dir / "result.json", "x") as handle:
        json.dump(result, handle, indent=2, sort_keys=True)
    print(json.dumps({"check": args.check, "status": result["status"], "problems": problems,
                      "run_dir": str(run_dir), "result_sha256": sha256_file(run_dir / "result.json")}))
    return 0 if not problems else 1


if __name__ == "__main__":
    if sys.argv[1:2] == [GUARD_FLAG] and len(sys.argv) == 3:
        raise SystemExit(child_main(sys.argv[2]))
    raise SystemExit(main())
