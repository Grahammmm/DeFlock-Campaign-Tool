"""Acceptance runner for FC3-BUNDLE-JSON (offline, synthetic, review-required).

Usage (exactly one check per invocation):
    <interpreter> tools/accept_fc3_bundle_json.py --check CHECK --output-root DIR

CHECK is one of: focused, compatibility, negative, portability, credentials,
public-tree. Every invocation creates a new exclusive run directory under DIR
and writes ``run.json`` there with the actual argv, cwd, exits, test counts,
case coverage, raw output paths and SHA256 hashes. The process exits 0 only
when every assertion of the selected check holds.

The repository root comes from this file's location; the import path is set
only inside this script. Every process that imports application or test code
installs the same guards first: network and model-transport sentinels, an
always-on process gate (subprocess allowlist plus every os/_posixsubprocess
process primitive), a synthetic HOME/TMPDIR and an import hook that redirects
URL constants and the bound defaults that copy them to a refused sentinel URL.
Fresh children that import application code (compatibility, the secret-report
checker, public-tree and their enumerated checker descendants) start through
this script's internal ``--guarded-child SPEC`` bootstrap, run with ``-I``.
Each guarded process records inert startup probes and a receipt outside the
repository. The installed detect-secrets scanner keeps its literal A5 argv.
Children are argument arrays without a shell. Passing is machine validity
only: REVIEW REQUIRED, never semantic/legal/stage acceptance or publication.
"""
import argparse
import datetime
import hashlib
import importlib.abc
import io
import json
import os
from pathlib import Path
import re
import runpy
import secrets
import shutil
import sys
import threading
import types

# Standard-library network and process modules load before any sentinel.
import _posixsubprocess
import http.client
import socket
import ssl  # noqa: F401  (loaded before sentinels so its classes import normally)
import subprocess
import tempfile
import unittest
from unittest import mock
import urllib.request

sys.dont_write_bytecode = True

RUNNER = Path(__file__).resolve()
REPO = RUNNER.parents[1]
CHECKS = ("focused", "compatibility", "negative", "portability", "credentials", "public-tree")
BRIEF = {
    "scope": "FC3-BUNDLE-JSON",
    "card": "t_48188e28",
    "title": "Reject overflowing JSON numbers at the private review bundle boundary.",
    # Short prefixes, resolved by git at run time and recorded in full.
    "brief_pin_prefix": "9c398829",
    "staged_base_prefix": "3e1a176c",
    "review_bundle_pinned_sha256_prefix": "32bc9ca7",
}
CASES = ("finite_scalar_and_nested", "positive_and_negative_overflow", "nested_overflow",
         "explicit_nonfinite_tokens", "duplicate_keys_still_rejected", "byte_bound_exact_and_over",
         "finite_underflow_and_signed_zero", "no_authority_or_io_required")
BUNDLE_MODULE = "campaign_tool.records.review_bundle"
FOCUSED_MODULE = "tests.records.test_fc3_bundle_json"
COMPAT_SELECTORS = ("tests.records.test_review_bundle", "tests.records.test_proposal_provenance",
                    "tests.test_secret_report", "tests.test_public_tree")
CHANGED_FILES = ("campaign_tool/records/review_bundle.py", "tests/records/test_fc3_bundle_json.py",
                 "tools/accept_fc3_bundle_json.py")
HASHED_FILES = CHANGED_FILES + ("tools/check_secret_report.py", "tools/check_public_tree.py",
                                "tests/records/test_review_bundle.py")
HOOK = "_finite_float"
FAIL_MARK = "FC3 typed refusal missing"
_T = FOCUSED_MODULE + "."
# With the finite hook replaced by plain float, exactly these tests must fail.
MUTANT_MUST_FAIL = frozenset(_T + name for name in (
    "PositiveAndNegativeOverflow.test_positive_overflow_scalars_rejected",
    "PositiveAndNegativeOverflow.test_negative_overflow_scalars_rejected",
    "PositiveAndNegativeOverflow.test_overflow_with_surrounding_whitespace_rejected",
    "NestedOverflow.test_every_overflow_in_every_nested_form_rejected",
    "NestedOverflow.test_overflow_after_many_valid_items_rejected",
    "SyntheticBundleBoundary.test_overflow_public_content_refused_before_any_write",
    "SyntheticBundleBoundary.test_planted_overflow_receipt_refused_as_invalid_json",
    "ByteBoundExactAndOver.test_explicit_bound_with_numbers",
    "NoAuthorityOrIoRequired.test_decode_needs_no_authority_files_or_network",
))
# Assembled from parts so these lists never match their own source text.
BOX_SPECIFIC = ("/ho" + "me/", "agent" + "-work", "venv" + "-full", "/Us" + "ers/", ".her" + "mes",
                "/t" + "mp/", "kan" + "ban/")
INLINE_FORMS = (re.compile(r"""["']-""" + r"""[ce]["']"""), re.compile("shell" + r"\s*=\s*True"),
                re.compile("PYTHON" + "PATH"), re.compile(r"\bos\.sys" + r"tem\("),
                re.compile(r"\bos\.po" + r"pen\("))
RAN = re.compile(r"^Ran (\d+) tests? in ", re.M)
SUMMARY = re.compile(r"^(OK|FAILED)(?: \(([^)]*)\))?\s*$", re.M)
VERBOSE = re.compile(r"^test\w* \(([\w.]+)\)", re.M)

SENTINEL_URL = "http://fc3-model-network-sentinel.invalid/refused"
URL_VALUE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
NO_PROGRAM = "fc3-unexpected-child-probe-not-a-program"
NO_PATH = "/nonexistent-fc3-probe/not-a-program"
SENTINEL_HITS = []
PROBE_HITS = []
ALLOWED_CHILDREN = []  # exact argv lists the runner itself launches
DESCENDANT_TEMPLATES = []  # enumerated checker descendants of a guarded child
STATE = {"hits_file": None, "launches_file": None, "spec": None, "probing": False, "installed": None,
         "redirects": []}
_GATE = threading.local()
_ORIGINAL_POPEN_INIT = subprocess.Popen.__init__
_ORIGINAL = {}
APP_PREFIXES = ("campaign_tool", "tests")
# Enumerated descendants of guarded children. The report checker is re-bootstrapped with guards;
# the public-tree scanner's own file listing is a local git child allowed only with this exact argv.
REPORT_CHECKER_ROLE = "descendant:report-checker"
GIT_LIST_ROLE = "descendant:git-ls-files"
GIT_LIST_ARGV = ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"]


# ---------------------------------------------------------------- utilities
def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def stamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def new_run_dir(output_root, check):
    root = Path(output_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    if root == REPO or REPO in root.parents:
        raise SystemExit("output root must be outside the repository")
    run = root / "{}-{}-{}-{}".format(check, stamp(), os.getpid(), secrets.token_hex(4))
    run.mkdir(mode=0o700, exist_ok=False)
    return run


def write_new(path, data):
    with open(path, "xb") as stream:
        stream.write(data if type(data) is bytes else data.encode("utf-8"))
    return {"path": str(path), "sha256": sha256_file(path)}


# ---------------------------------------------------------------- guards
NETWORK_GUARDS = (
    ("network:socket.connect", socket.socket, "connect"),
    ("network:socket.connect_ex", socket.socket, "connect_ex"),
    ("network:socket.sendto", socket.socket, "sendto"),
    ("network:socket.sendmsg", socket.socket, "sendmsg"),
    ("network:socket.create_connection", socket, "create_connection"),
    ("network:socket.getaddrinfo", socket, "getaddrinfo"),
    ("network:socket.gethostbyname", socket, "gethostbyname"),
    ("network:socket.gethostbyname_ex", socket, "gethostbyname_ex"),
    # Model calls in this repository use these transports (urllib openers and http.client).
    ("model/network:urllib.request.urlopen", urllib.request, "urlopen"),
    ("model/network:urllib.request.OpenerDirector.open", urllib.request.OpenerDirector, "open"),
    ("model/network:http.client.HTTPConnection.connect", http.client.HTTPConnection, "connect"),
    ("model/network:http.client.HTTPConnection.request", http.client.HTTPConnection, "request"),
    ("model/network:http.client.HTTPSConnection.connect", http.client.HTTPSConnection, "connect"),
)
BLOCKED_OS = ("system", "popen", "fork", "forkpty", "execv", "execve", "execvp", "execvpe", "execl", "execle",
              "execlp", "execlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe", "spawnl", "spawnle", "spawnlp",
              "spawnlpe")
GATED_OS = ("posix_spawn", "posix_spawnp")
SENSITIVE_ENV = re.compile(r"KEY|TOKEN|SECRET|PASS|CRED|AUTH|COOKIE|SESSION|OPENAI|ANTHROPIC|OLLAMA|MODEL|"
                           r"PROXY|^PYTHON|^LD_", re.I)


def append_jsonl(path, row):
    with open(path, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, sort_keys=True, default=str) + "\n")


def read_jsonl(path):
    path = Path(path)
    return [json.loads(line) for line in path.read_text().splitlines() if line] if path.is_file() else []


def _record_hit(name):
    entry = str(name)[:300]
    if STATE["probing"]:
        PROBE_HITS.append(entry)
        return
    SENTINEL_HITS.append(entry)
    if STATE["hits_file"]:
        append_jsonl(STATE["hits_file"], {"pid": os.getpid(), "hit": entry})


def _blocked(name):
    def refuse(*args, **kwargs):
        _record_hit(name)
        raise PermissionError("FC3 acceptance sentinel refused " + name)
    refuse._fc3_guard = name
    return refuse


def _gated(name, original):
    """A process primitive usable only inside an allowlisted subprocess.Popen launch."""
    def gate(*args, **kwargs):
        if getattr(_GATE, "open", False):
            return original(*args, **kwargs)
        _record_hit(name)
        raise PermissionError("FC3 acceptance sentinel refused " + name)
    gate._fc3_guard = name
    return gate


def match_descendant(argv, kwargs, templates):
    """Pure check: the enumerated descendant template this exact argv matches, else None."""
    if argv is None or kwargs.get("shell") or kwargs.get("executable") is not None:
        return None
    for template in templates:
        if "exact_argv" in template:
            if argv == list(template["exact_argv"]):
                return template
            continue
        prefix = list(template["argv_prefix"])
        if len(argv) != len(prefix) + template["extra_args"] or argv[:len(prefix)] != prefix:
            continue
        under = Path(template["arg_under"])
        extra = [Path(item) for item in argv[len(prefix):]]
        if all(item.is_absolute() and item.resolve() != under and item.resolve().is_relative_to(under)
               for item in extra):
            return template
    return None


def bootstrap_argv(spec_path):
    return [sys.executable, "-I", str(RUNNER), "--guarded-child", str(spec_path)]


def descendant_bootstrap(template, argv):
    """Relaunch an enumerated checker descendant through the guarded bootstrap."""
    spec = STATE["spec"]
    base = Path(spec["run_dir"]) / "descendants" / ("d-" + secrets.token_hex(6))
    home, temp = base / "home", base / "tmp"
    home.mkdir(mode=0o700, parents=True)
    temp.mkdir(mode=0o700)
    child = {"role": template["role"], "run_dir": spec["run_dir"], "home": str(home), "tmp": str(temp),
             "hits_file": spec["hits_file"], "launches_file": spec["launches_file"],
             "receipts_dir": spec["receipts_dir"], "descendants": [],
             "target": {"kind": "script", "path": template["argv_prefix"][1], "argv": argv[1:]}}
    spec_path = base / "spec.json"
    write_new(spec_path, json.dumps(child, indent=2, sort_keys=True) + "\n")
    launch = bootstrap_argv(spec_path)
    append_jsonl(spec["launches_file"], {"parent_pid": os.getpid(), "role": template["role"],
                                         "requested_argv": argv, "launched_argv": launch, "spec": str(spec_path)})
    return launch


def _guarded_popen_init(self, args, *rest, **kwargs):
    argv = [str(item) for item in args] if isinstance(args, (list, tuple)) else None
    launch = None
    if not rest and not kwargs.get("shell") and kwargs.get("executable") is None and argv is not None:
        if argv in ALLOWED_CHILDREN:
            launch = argv
        else:
            template = match_descendant(argv, kwargs, DESCENDANT_TEMPLATES)
            if template is not None and "exact_argv" in template:
                launch = argv
                append_jsonl(STATE["spec"]["launches_file"], {
                    "parent_pid": os.getpid(), "role": template["role"], "requested_argv": argv,
                    "launched_argv": launch, "spec": None, "cwd": str(kwargs.get("cwd"))})
            elif template is not None:
                launch = descendant_bootstrap(template, argv)
    if launch is None:
        _record_hit("subprocess:" + repr(args)[:200])
        raise PermissionError("FC3 acceptance sentinel refused an unexpected subprocess")
    _GATE.open = True
    try:
        return _ORIGINAL_POPEN_INIT(self, launch, **kwargs)
    finally:
        _GATE.open = False


_guarded_popen_init._fc3_guard = "process:subprocess.Popen allowlist"


def guard_rows():
    rows = [(label, holder, attr) for label, holder, attr in NETWORK_GUARDS]
    rows += [("process:os." + name, os, name) for name in BLOCKED_OS + GATED_OS if name in _ORIGINAL]
    rows += [("process:subprocess._fork_exec", subprocess, "_fork_exec"),
             ("process:_posixsubprocess.fork_exec", _posixsubprocess, "fork_exec"),
             ("process:subprocess.Popen.__init__", subprocess.Popen, "__init__")]
    return rows


def redirect_module(module, log):
    """Point URL constants, and bound defaults copied from them, at the refused sentinel URL."""
    name = module.__name__
    originals = set()
    for attr, value in list(vars(module).items()):
        if attr.isupper() and isinstance(value, str) and URL_VALUE.match(value) and value != SENTINEL_URL:
            originals.add(value)
            setattr(module, attr, SENTINEL_URL)
            log.append({"module": name, "constant": attr})
    if not originals:
        return

    def swap(value):
        return SENTINEL_URL if isinstance(value, str) and value in originals else value

    for attr, value in list(vars(module).items()):
        functions = []
        if isinstance(value, types.FunctionType) and value.__module__ == name:
            functions.append((attr, value))
        elif isinstance(value, type) and value.__module__ == name:
            for member, item in vars(value).items():
                item = getattr(item, "__func__", item)
                if isinstance(item, types.FunctionType):
                    functions.append((attr + "." + member, item))
        for label, function in functions:
            defaults = function.__defaults__ or ()
            if any(swap(item) is not item for item in defaults):
                function.__defaults__ = tuple(swap(item) for item in defaults)
                log.append({"module": name, "default": label})
            keywords = function.__kwdefaults__ or {}
            if any(swap(item) is not item for item in keywords.values()):
                function.__kwdefaults__ = {key: swap(item) for key, item in keywords.items()}
                log.append({"module": name, "kwdefault": label})


class _RedirectLoader(importlib.abc.Loader):
    def __init__(self, inner):
        self._inner = inner

    def create_module(self, spec):
        return self._inner.create_module(spec)

    def exec_module(self, module):
        self._inner.exec_module(module)
        redirect_module(module, STATE["redirects"])

    def __getattr__(self, name):
        return getattr(self._inner, name)


class RedirectFinder(importlib.abc.MetaPathFinder):
    """Wrap application module loaders so constants are redirected before first use."""
    def find_spec(self, name, path, target=None):
        if name.split(".")[0] != "campaign_tool":
            return None
        for finder in sys.meta_path:
            if finder is self or not hasattr(finder, "find_spec"):
                continue
            spec = finder.find_spec(name, path, target)
            if spec is not None:
                if spec.loader is not None and hasattr(spec.loader, "exec_module"):
                    spec.loader = _RedirectLoader(spec.loader)
                return spec
        return None


def app_modules():
    return sorted(name for name in sys.modules if name.split(".")[0] in APP_PREFIXES)


def install_guards(home, temp):
    """Install every guard before any application or test import; they stay on for the process."""
    early = app_modules()
    if early:
        raise RuntimeError("application module imported before guards: " + early[0])
    if STATE["installed"] is not None:
        raise RuntimeError("guards already installed")
    for label, holder, attr in NETWORK_GUARDS:
        setattr(holder, attr, _blocked(label))
    for name in BLOCKED_OS + GATED_OS:
        if hasattr(os, name):
            _ORIGINAL[name] = getattr(os, name)
            setattr(os, name, _gated("process:os." + name, _ORIGINAL[name]) if name in GATED_OS
                    else _blocked("process:os." + name))
    subprocess._fork_exec = _gated("process:subprocess._fork_exec", subprocess._fork_exec)
    _posixsubprocess.fork_exec = _gated("process:_posixsubprocess.fork_exec", _posixsubprocess.fork_exec)
    subprocess.Popen.__init__ = _guarded_popen_init
    dropped = sorted(name for name in os.environ if SENSITIVE_ENV.search(name))
    for name in dropped:
        del os.environ[name]
    os.environ["HOME"] = str(home)
    os.environ["TMPDIR"] = str(temp)
    tempfile.tempdir = str(temp)
    if str(REPO) in sys.path:
        sys.path.remove(str(REPO))
    sys.path.insert(0, str(REPO))
    sys.meta_path.insert(0, RedirectFinder())
    STATE["installed"] = {"pid": os.getpid(), "pre_guard_app_modules": early, "home": str(home),
                          "tempdir": str(temp), "sys_path_0": sys.path[0], "env_dropped_names": dropped,
                          "guards": [label for label, _, _ in guard_rows()],
                          "import_hook": "RedirectFinder (campaign_tool URL constants and bound defaults)",
                          "sentinel_url": SENTINEL_URL}
    return dict(STATE["installed"])


def child_env(home, temp):
    env = {key: value for key, value in os.environ.items() if not SENSITIVE_ENV.search(key)}
    env["HOME"], env["TMPDIR"] = str(home), str(temp)
    return env


def startup_probes():
    """Inert regression evidence for the startup boundary; nothing here can launch or connect."""
    identity = {label: getattr(getattr(holder, attr, None), "_fc3_guard", None) is not None
                for label, holder, attr in guard_rows()}
    calls = {}

    def attempt(label, action):
        before = len(PROBE_HITS)
        try:
            action()
        except PermissionError as error:
            ok = "FC3 acceptance sentinel" in str(error) and len(PROBE_HITS) == before + 1
            calls[label] = "refused" if ok else "unexpected_permission_error"
        except Exception as error:  # a missing guard fails inertly on the nonexistent target
            calls[label] = "not_guarded:" + type(error).__name__
        else:
            calls[label] = "not_guarded:returned"

    def unix_connect():
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.connect(NO_PATH)
        finally:
            probe.close()

    STATE["probing"] = True
    try:
        attempt("unexpected child via subprocess.Popen", lambda: subprocess.Popen(
            [NO_PROGRAM], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        attempt("os.posix_spawn outside Popen", lambda: os.posix_spawn(NO_PATH, [NO_PATH], {}))
        attempt("os.execv", lambda: os.execv(NO_PATH, [NO_PATH]))
        attempt("socket.connect (local unix path)", unix_connect)
        attempt("urllib.request.urlopen (local file url)", lambda: urllib.request.urlopen("file://" + NO_PATH))
        attempt("OpenerDirector.open (local file url)",
                lambda: urllib.request.build_opener().open("file://" + NO_PATH))
    finally:
        STATE["probing"] = False
    temp = Path(tempfile.gettempdir())
    script = str(REPO / "tools" / "check_secret_report.py")
    template = {"role": "probe", "argv_prefix": [sys.executable, script], "extra_args": 1, "arg_under": str(temp)}
    inside = str(temp / "probe-report.json")
    exact = {"role": "probe-exact", "exact_argv": list(GIT_LIST_ARGV)}
    matcher = {
        "allows_enumerated_descendant": match_descendant([sys.executable, script, inside], {}, [template])
        is template,
        "refuses_shell_flag": match_descendant([sys.executable, script, inside], {"shell": True}, [template])
        is None,
        "refuses_executable_override": match_descendant([sys.executable, script, inside],
                                                        {"executable": NO_PATH}, [template]) is None,
        "refuses_other_script": match_descendant(
            [sys.executable, str(REPO / "tools" / "check_public_tree.py"), inside], {}, [template]) is None,
        "refuses_extra_argument": match_descendant([sys.executable, script, inside, inside], {}, [template])
        is None,
        "refuses_argument_outside_temp": match_descendant(
            [sys.executable, script, str(REPO / "README.md")], {}, [template]) is None,
        "refuses_relative_argument": match_descendant([sys.executable, script, "probe-report.json"], {},
                                                      [template]) is None,
        "refuses_interpreter_flag": match_descendant([sys.executable, "-I", script, inside], {}, [template])
        is None,
        "refuses_without_templates": match_descendant([sys.executable, script, inside], {}, []) is None,
        "allows_exact_descendant": match_descendant(list(GIT_LIST_ARGV), {}, [exact]) is exact,
        "refuses_exact_with_extra_argument": match_descendant(GIT_LIST_ARGV + ["--", NO_PATH], {}, [exact])
        is None,
        "refuses_exact_reordered": match_descendant(GIT_LIST_ARGV[:2] + GIT_LIST_ARGV[:1:-1], {}, [exact]) is None,
        "refuses_exact_shell_flag": match_descendant(list(GIT_LIST_ARGV), {"shell": True}, [exact]) is None,
    }
    module = types.ModuleType("fc3_redirect_probe")
    module.PROBE_ENDPOINT = "http://127.0.0.1:9/fc3-probe"

    def probe_function(endpoint=module.PROBE_ENDPOINT, *, other=module.PROBE_ENDPOINT):
        return endpoint
    probe_function.__module__ = module.__name__
    module.probe_function = probe_function
    log = []
    redirect_module(module, log)
    redirect = {"constant": module.PROBE_ENDPOINT == SENTINEL_URL, "default": probe_function() == SENTINEL_URL,
                "kwdefault": probe_function.__kwdefaults__["other"] == SENTINEL_URL, "logged": len(log) == 3}
    installed = STATE["installed"] or {}
    environment = {"hook_first_on_meta_path": isinstance(sys.meta_path[0], RedirectFinder),
                   "pre_guard_app_modules_empty": installed.get("pre_guard_app_modules") == [],
                   "home": os.environ.get("HOME") == installed.get("home"),
                   "tempdir": tempfile.gettempdir() == installed.get("tempdir"),
                   "sys_path_0_is_repo": sys.path[0] == str(REPO)}
    ok = (bool(identity) and all(identity.values()) and all(v == "refused" for v in calls.values())
          and all(matcher.values()) and all(redirect.values()) and all(environment.values()))
    return {"pass": ok, "identity": identity, "calls": calls, "matcher": matcher, "redirect": redirect,
            "environment": environment, "probe_hits": len(PROBE_HITS)}


def probe_reasons(info):
    probes = info.get("startup_probes") or {}
    if probes.get("pass"):
        return []
    bad = [key for group in ("identity", "matcher", "redirect", "environment")
           for key, value in probes.get(group, {}).items() if value is not True]
    bad += [key for key, value in probes.get("calls", {}).items() if value != "refused"]
    return ["startup_probes_failed:" + ",".join(bad)[:1500]]


def install_sentinels(run_dir):
    """In-process guards and inert startup probes, before any application import."""
    temp, home = run_dir / "tmp", run_dir / "home"
    temp.mkdir(mode=0o700)
    home.mkdir(mode=0o700)
    info = install_guards(home, temp)
    info["startup_probes"] = startup_probes()
    return info


class TestWindow:
    """No runner child may start while test code runs (the process gate itself is always on)."""
    def __enter__(self):
        self.saved = list(ALLOWED_CHILDREN)
        ALLOWED_CHILDREN.clear()
        return self

    def __exit__(self, *exc):
        ALLOWED_CHILDREN[:] = self.saved
        return False


# ---------------------------------------------------------------- guarded children
def run_target(target, receipt):
    """Run the target as ``__main__``; an uncaught target exception exits 1 with a traceback, as Python does."""
    sys.argv[:] = [str(item) for item in target["argv"]]
    try:
        if target["kind"] == "module":
            runpy.run_module(target["name"], run_name="__main__", alter_sys=True)
        else:
            runpy.run_path(target["path"], run_name="__main__")
    except SystemExit as stop:
        if stop.code is None:
            return 0
        if isinstance(stop.code, int):
            return int(stop.code)
        print(stop.code, file=sys.stderr)
        return 1
    except Exception as error:
        receipt["target_exception"] = type(error).__name__
        import traceback
        traceback.print_exc()
        return 1
    return 0


def guarded_child(spec_path):
    """Bootstrap of a fresh child: guards and probes first, then the enumerated target."""
    spec = json.loads(Path(spec_path).read_text())
    STATE.update(spec=spec, hits_file=spec["hits_file"], launches_file=spec["launches_file"])
    receipt = {"role": spec["role"], "pid": os.getpid(), "ppid": os.getppid(), "spec": str(spec_path),
               "argv": [sys.executable, *sys.argv], "isolated_flag": sys.flags.isolated,
               "target": spec["target"], "started_at": stamp()}
    code = 70
    try:
        receipt["guards"] = install_guards(Path(spec["home"]), Path(spec["tmp"]))
        DESCENDANT_TEMPLATES[:] = spec["descendants"]
        receipt["startup_probes"] = startup_probes()
        if receipt["startup_probes"]["pass"]:
            code = run_target(spec["target"], receipt)
        else:
            receipt["error"] = "startup_probes_failed"
            print("FC3 guarded child refused to start: startup probes failed", file=sys.stderr)
    except Exception as error:
        receipt["error"] = type(error).__name__
        import traceback
        traceback.print_exc()
        code = 1
    loaded = {name: getattr(sys.modules[name], "__file__", None) for name in app_modules()}
    receipt.update(exit_code=code, finished_at=stamp(), app_module_count=len(loaded),
                   loaded_app_modules_outside_repo=sorted(
                       name for name, path in loaded.items() if path and not Path(path).resolve().is_relative_to(REPO)),
                   redirects=list(STATE["redirects"]), hits=len(SENTINEL_HITS), probe_hits=len(PROBE_HITS))
    safe_role = re.sub(r"[^A-Za-z0-9_-]", "_", spec["role"])
    write_new(Path(spec["receipts_dir"]) / "{}-{}-{}.json".format(safe_role, os.getpid(), secrets.token_hex(4)),
              json.dumps(receipt, indent=2, sort_keys=True, default=str) + "\n")
    return code


def guarded_launch(run_dir, label, role, target, descendants=()):
    """Launch a fresh guarded child of this runner with its enumerated descendants."""
    base = run_dir / ("child-" + label)
    home, temp = base / "home", base / "tmp"
    home.mkdir(mode=0o700, parents=True)
    temp.mkdir(mode=0o700)
    (run_dir / "guard-receipts").mkdir(mode=0o700, exist_ok=True)
    templates = []
    for item in descendants:
        if "exact_argv" in item:
            templates.append({"role": item["role"], "exact_argv": list(item["exact_argv"])})
        else:
            templates.append({"role": item["role"], "argv_prefix": [sys.executable, item["script"]],
                              "extra_args": 1, "arg_under": str(temp)})
    spec = {"role": role, "run_dir": str(run_dir), "home": str(home), "tmp": str(temp),
            "hits_file": str(run_dir / "guard-hits.jsonl"), "launches_file": str(run_dir / "guard-launches.jsonl"),
            "receipts_dir": str(run_dir / "guard-receipts"), "target": target, "descendants": templates}
    spec_path = base / "spec.json"
    write_new(spec_path, json.dumps(spec, indent=2, sort_keys=True) + "\n")
    child = run_child(run_dir, label, bootstrap_argv(spec_path), REPO, env=child_env(home, temp))
    child["spec"] = str(spec_path)
    child["target"] = target
    return child


def verify_guarded(run_dir, expected_roles, exact_roles=()):
    """Every guarded process left a passing receipt; descendants match their recorded launches."""
    reasons, receipts = [], []
    receipt_dir = run_dir / "guard-receipts"
    for path in sorted(receipt_dir.glob("*.json")) if receipt_dir.is_dir() else []:
        data = json.loads(path.read_text())
        data["receipt_path"], data["receipt_sha256"] = str(path), sha256_file(path)
        receipts.append(data)
    launches = read_jsonl(run_dir / "guard-launches.jsonl")
    hits = read_jsonl(run_dir / "guard-hits.jsonl")
    counts = {}
    for receipt in receipts:
        counts[receipt["role"]] = counts.get(receipt["role"], 0) + 1
    for role, want in expected_roles.items():
        if want == "launches":
            want = sum(1 for launch in launches if launch["role"] == role)
            if want < 1:
                reasons.append("descendant_not_exercised:" + role)
        if counts.get(role, 0) != want:
            reasons.append("guard_receipt_count:%s:%s!=%s" % (role, counts.get(role, 0), want))
    unknown = set(counts) - set(expected_roles)
    if unknown:
        reasons.append("unexpected_guarded_roles:" + ",".join(sorted(unknown)))
    receipt_specs = {receipt["spec"] for receipt in receipts}
    for role in exact_roles:
        if not any(launch["role"] == role for launch in launches):
            reasons.append("descendant_not_exercised:" + role)
    for launch in launches:
        if launch["spec"] is None:
            if launch["role"] not in exact_roles or launch["launched_argv"] != launch["requested_argv"]:
                reasons.append("unexpected_exact_descendant:" + launch["role"])
            continue
        if launch["role"] not in expected_roles:
            reasons.append("unexpected_bootstrapped_descendant:" + launch["role"])
        if launch["spec"] not in receipt_specs:
            reasons.append("descendant_launch_without_receipt:" + launch["spec"])
        if launch["launched_argv"][:4] != [sys.executable, "-I", str(RUNNER), "--guarded-child"]:
            reasons.append("descendant_not_bootstrapped:" + launch["spec"])
    for receipt in receipts:
        where = "%s:%s" % (receipt["role"], receipt["pid"])
        if not (receipt.get("startup_probes") or {}).get("pass"):
            reasons.append("child_startup_probes_failed:" + where)
        if (receipt.get("guards") or {}).get("pre_guard_app_modules") != []:
            reasons.append("child_app_import_before_guards:" + where)
        if receipt.get("loaded_app_modules_outside_repo"):
            reasons.append("child_app_module_outside_repo:" + where)
        if receipt.get("hits"):
            reasons.append("child_sentinel_hits:" + where)
        if receipt.get("isolated_flag") != 1:
            reasons.append("child_not_isolated_mode:" + where)
        if receipt.get("error"):
            reasons.append("child_error:%s:%s" % (where, receipt["error"]))
    if hits:
        reasons.append("child_sentinel_hits_present")
    summary = {"receipts": [{key: receipt.get(key) for key in (
        "role", "pid", "ppid", "exit_code", "target_exception", "isolated_flag", "app_module_count", "redirects",
        "probe_hits", "hits", "receipt_path", "receipt_sha256")} | {"startup_probes_pass": receipt["startup_probes"]["pass"]
                                                       if receipt.get("startup_probes") else None}
        for receipt in receipts], "launches": launches, "hits": hits, "role_counts": counts}
    return reasons, summary


def run_child(run_dir, label, argv, cwd, env=None, timeout=1800):
    argv = [str(item) for item in argv]
    record = {"label": label, "argv": argv, "cwd": str(cwd)}
    out_path, err_path = run_dir / (label + ".stdout"), run_dir / (label + ".stderr")
    ALLOWED_CHILDREN.append(argv)
    try:
        with open(out_path, "xb") as out, open(err_path, "xb") as err:
            done = subprocess.run(argv, cwd=str(cwd), stdout=out, stderr=err, stdin=subprocess.DEVNULL,
                                  env=env, timeout=timeout, check=False)
        record["exit"] = done.returncode
    except (OSError, subprocess.SubprocessError) as error:
        record["exit"] = None
        record["launch_error"] = type(error).__name__
    finally:
        ALLOWED_CHILDREN.remove(argv)
    for key, path in (("stdout", out_path), ("stderr", err_path)):
        if path.is_file():
            record[key + "_path"] = str(path)
            record[key + "_sha256"] = sha256_file(path)
    return record


# ---------------------------------------------------------------- identity
def git(*args):
    argv = ["git", "-C", str(REPO), *args]
    ALLOWED_CHILDREN.append(argv)
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=120, check=False)
        return done.returncode, done.stdout
    except (OSError, subprocess.SubprocessError) as error:
        return None, type(error).__name__
    finally:
        ALLOWED_CHILDREN.remove(argv)


def source_identity():
    identity = {"repo": str(REPO), "hashes": {}}
    for relative in HASHED_FILES:
        path = REPO / relative
        identity["hashes"][relative] = sha256_file(path) if path.is_file() else None
    if not (REPO / ".git").exists():
        identity["git"] = "no_git_metadata_in_this_copy"
        return identity
    code, head = git("rev-parse", "HEAD")
    identity["head"] = head.strip() if code == 0 else None
    code, status = git("status", "--porcelain", "--untracked-files=all")
    identity["status_porcelain"] = status if code == 0 else None
    identity["clean"] = code == 0 and status == ""
    for key in ("brief_pin_prefix", "staged_base_prefix"):
        code, full = git("rev-parse", "--verify", BRIEF[key] + "^{commit}")
        identity[key.replace("_prefix", "")] = full.strip() if code == 0 else None
    pin, base = identity.get("brief_pin"), identity.get("staged_base")
    if pin and base:
        code, _ = git("merge-base", "--is-ancestor", pin, base)
        identity["brief_pin_is_ancestor_of_staged_base"] = code == 0
        code, delta = git("diff", "--name-only", pin, base, "--", *HASHED_FILES[3:],
                          "campaign_tool/records/review_bundle.py", *[s.replace(".", "/") + ".py"
                                                                      for s in COMPAT_SELECTORS])
        identity["pin_to_staged_base_delta_on_scope_files"] = delta.split() if code == 0 else None
        code, changed = git("diff", "--name-only", base)
        names = set(changed.split()) if code == 0 else None
        code, untracked = git("ls-files", "--others", "--exclude-standard")
        if names is not None and code == 0:
            names |= set(untracked.split())
            identity["changed_since_staged_base"] = sorted(names)
            identity["changes_within_admitted_files"] = names <= set(CHANGED_FILES)
        code, committed = git("diff", "--name-only", base, "HEAD")
        identity["committed_since_staged_base"] = committed.split() if code == 0 else None
    return identity


def scope_reasons(identity):
    reasons = []
    if identity.get("git") is None:
        if identity.get("head") is None:
            reasons.append("head_unavailable")
        if identity.get("brief_pin_is_ancestor_of_staged_base") is not True:
            reasons.append("brief_pin_not_ancestor_of_staged_base")
        if identity.get("pin_to_staged_base_delta_on_scope_files") != []:
            reasons.append("scope_files_changed_between_pin_and_staged_base")
        if identity.get("changes_within_admitted_files") is not True:
            reasons.append("changes_outside_admitted_files")
    return reasons


# ---------------------------------------------------------------- unittest
def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


def owner_id(test):
    return getattr(test, "test_case", test).id()


def run_tests(run_dir, label, suite):
    """Run in-process under the process window; return explicit result facts."""
    tests = list(flatten(suite))
    stream = io.StringIO()
    with TestWindow():
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(unittest.TestSuite(tests))
    failing = {}
    for kind, rows in (("failure", result.failures), ("error", result.errors),
                       ("unexpected_success", [(t, "") for t in result.unexpectedSuccesses])):
        for test, text in rows:
            failing.setdefault(owner_id(test), []).append({"kind": kind, "marked": FAIL_MARK in text})
    output = write_new(run_dir / (label + ".unittest.txt"), stream.getvalue())
    cases = {}
    for test in tests:
        case = getattr(type(test), "CASE", None)
        cases.setdefault(case, {"tests": 0, "passing": 0})
        cases[case]["tests"] += 1
        cases[case]["passing"] += test.id() not in failing
    return {"label": label, "tests_run": result.testsRun, "test_ids": [t.id() for t in tests],
            "failing": failing, "skipped": [owner_id(t) for t, _ in result.skipped],
            "expected_failures": [owner_id(t) for t, _ in result.expectedFailures],
            "was_successful": result.wasSuccessful(), "case_coverage": cases, "output": output}


def load_focused():
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromName(FOCUSED_MODULE)
    if loader.errors:
        raise RuntimeError("focused import errors: " + " | ".join(loader.errors)[:2000])
    return suite


def loaded_modules():
    names = ("campaign_tool", BUNDLE_MODULE, FOCUSED_MODULE)
    paths = {name: str(Path(sys.modules[name].__file__).resolve()) for name in names if name in sys.modules}
    paths["all_under_repo"] = all(Path(paths.get(name, "/")).is_relative_to(REPO) for name in names)
    return paths


def focused_reasons(facts):
    reasons = []
    if facts["tests_run"] <= 0:
        reasons.append("zero_tests")
    if facts["failing"]:
        reasons.append("unexpected_failures:" + ",".join(sorted(facts["failing"]))[:1500])
    if facts["skipped"] or facts["expected_failures"]:
        reasons.append("skips_or_expected_failures_present")
    for case in CASES:
        row = facts["case_coverage"].get(case)
        if not row or row["passing"] < 1 or row["passing"] != row["tests"]:
            reasons.append("case_not_fully_green:" + case)
    unknown = set(facts["case_coverage"]) - set(CASES)
    if unknown:
        reasons.append("tests_without_frozen_case:" + repr(sorted(map(str, unknown))))
    return reasons


# ---------------------------------------------------------------- checks
def check_focused(run_dir, record):
    record["sentinels"] = install_sentinels(run_dir)
    reasons = probe_reasons(record["sentinels"])
    facts = run_tests(run_dir, "focused", load_focused())
    record["focused"] = facts
    record["loaded_modules"] = loaded_modules()
    reasons += focused_reasons(facts)
    if not record["loaded_modules"]["all_under_repo"]:
        reasons.append("module_loaded_outside_repo")
    return reasons


def parse_unittest_stream(text):
    ran = RAN.findall(text)
    summary = SUMMARY.findall(text)
    modules = {}
    for owner in VERBOSE.findall(text):
        for selector in COMPAT_SELECTORS:
            if owner.startswith(selector + "."):
                modules[selector] = modules.get(selector, 0) + 1
    details = summary[-1][1] if summary else ""
    skipped = re.search(r"skipped=(\d+)", details)
    return {"ran": int(ran[-1]) if ran else None, "status": summary[-1][0] if summary else None,
            "details": details, "skipped": int(skipped[1]) if skipped else 0, "per_selector": modules}


def check_compatibility(run_dir, record):
    record["sentinels"] = install_sentinels(run_dir)
    reasons = probe_reasons(record["sentinels"])
    child = guarded_launch(run_dir, "compatibility", "compatibility",
                           {"kind": "module", "name": "unittest", "argv": ["unittest", "-v", *COMPAT_SELECTORS]},
                           descendants=[{"role": REPORT_CHECKER_ROLE,
                                         "script": str(REPO / "tools" / "check_secret_report.py")}])
    record["children"] = [child]
    parsed = parse_unittest_stream(Path(child["stderr_path"]).read_text(errors="replace")) \
        if "stderr_path" in child else {}
    record["compatibility"] = parsed
    guard_reasons, record["guarded_processes"] = verify_guarded(
        run_dir, {"compatibility": 1, REPORT_CHECKER_ROLE: "launches"})
    reasons += guard_reasons
    if child.get("exit") != 0:
        reasons.append("compatibility_exit_%s" % child.get("exit"))
    if not parsed.get("ran"):
        reasons.append("zero_or_unparsed_tests")
    if parsed.get("status") != "OK":
        reasons.append("compatibility_not_ok")
    if parsed.get("skipped"):
        reasons.append("unexpected_skips")
    for selector in COMPAT_SELECTORS:
        if not parsed.get("per_selector", {}).get(selector):
            reasons.append("selector_ran_no_tests:" + selector)
    return reasons


def check_negative(run_dir, record):
    record["sentinels"] = install_sentinels(run_dir)
    suite = load_focused()
    bundle = sys.modules[BUNDLE_MODULE]
    record["loaded_modules"] = loaded_modules()
    reasons = probe_reasons(record["sentinels"])
    original = getattr(bundle, HOOK, None)
    if not callable(original):
        record["negative"] = {"hook_present": False}
        return ["negative_check_unavailable:hook_missing"]
    source_before = sha256_file(bundle.__file__)
    neg = {"bypassed_branch": BUNDLE_MODULE + "." + HOOK
           + " (decode's parse_float finiteness hook) replaced in memory by builtins.float",
           "hook_present": True, "module_source_sha256_before": source_before}

    def probe(label):
        try:
            value = bundle.decode(b"[1e999]")
        except bundle.BundleError as error:
            return {"probe": label, "outcome": "BundleError", "reason": list(error.args)}
        except Exception as error:  # untyped escape is recorded, never hidden
            return {"probe": label, "outcome": "untyped:" + type(error).__name__}
        return {"probe": label, "outcome": "accepted", "value_repr": repr(value)}

    with mock.patch.object(bundle, HOOK, float):
        neg["mutant_hook_is_float"] = getattr(bundle, HOOK) is float
        neg["mutant_probe"] = probe("mutant")
        mutant = run_tests(run_dir, "mutant", suite)
    neg["restored_hook_identity"] = getattr(bundle, HOOK) is original
    neg["restored_probe"] = probe("restored")
    restored = run_tests(run_dir, "restored", load_focused())
    neg["module_source_sha256_after"] = sha256_file(bundle.__file__)
    neg["mutant"], neg["restored"] = mutant, restored
    failed = set(mutant["failing"])
    neg["mutant_failed_tests"] = sorted(failed)
    neg["mutant_control_tests_passing"] = sorted(set(mutant["test_ids"]) - failed)
    record["negative"] = neg
    if not neg["mutant_hook_is_float"] or not neg["restored_hook_identity"]:
        reasons.append("mutation_not_applied_or_not_restored")
    if neg["mutant_probe"]["outcome"] != "accepted":
        reasons.append("mutant_probe_not_bypassed")
    if neg["restored_probe"] != {"probe": "restored", "outcome": "BundleError", "reason": ["invalid_json"]}:
        reasons.append("restored_probe_not_typed_refusal")
    if mutant["tests_run"] <= 0 or failed != MUTANT_MUST_FAIL:
        reasons.append("mutant_failure_set_mismatch:missing=%s:extra=%s" % (
            sorted(MUTANT_MUST_FAIL - failed), sorted(failed - MUTANT_MUST_FAIL)))
    for test_id, rows in mutant["failing"].items():
        if not all(row["kind"] == "failure" and row["marked"] for row in rows):
            reasons.append("mutant_failure_not_typed_refusal_assertion:" + test_id)
    if mutant["skipped"] or len(mutant["test_ids"]) - len(failed) < 1:
        reasons.append("mutant_controls_missing_or_skipped")
    reasons.extend("restored:" + reason for reason in focused_reasons(restored))
    if neg["module_source_sha256_after"] != source_before:
        reasons.append("source_mutated_on_disk")
    if not record["loaded_modules"]["all_under_repo"]:
        reasons.append("module_loaded_outside_repo")
    return reasons


def static_portability_scan():
    findings = []
    for relative in CHANGED_FILES[1:]:
        text = (REPO / relative).read_text()
        for token in BOX_SPECIFIC:
            if token in text:
                findings.append({"file": relative, "box_specific": token})
        for pattern in INLINE_FORMS:
            if pattern.search(text):
                findings.append({"file": relative, "inline_or_shell_form": pattern.pattern})
    return findings


def check_portability(run_dir, record):
    record["sentinels"] = install_sentinels(run_dir)
    record["static_scan"] = static_portability_scan()
    reasons = probe_reasons(record["sentinels"])
    reasons += ["static:" + json.dumps(item) for item in record["static_scan"]]
    copy_root = run_dir / "second-prefix" / ("copy-" + secrets.token_hex(6)) / "repo"
    copy_root.mkdir(parents=True)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    for top in ("campaign_tool", "tests", "tools"):
        shutil.copytree(REPO / top, copy_root / top, symlinks=True, ignore=ignore)
    copied = {relative: sha256_file(copy_root / relative) for relative in CHANGED_FILES}
    original = {relative: sha256_file(REPO / relative) for relative in CHANGED_FILES}
    record["copy"] = {"root": str(copy_root), "hashes": copied, "match_original": copied == original}
    if copied != original:
        reasons.append("copy_hash_mismatch")
    copy_out = run_dir / "copy-focused-output"
    copy_home, copy_temp = run_dir / "copy-env" / "home", run_dir / "copy-env" / "tmp"
    copy_home.mkdir(mode=0o700, parents=True)
    copy_temp.mkdir(mode=0o700)
    child = run_child(run_dir, "copy-focused",
                      [sys.executable, "-I", copy_root / "tools" / "accept_fc3_bundle_json.py",
                       "--check", "focused", "--output-root", copy_out], copy_root,
                      env=child_env(copy_home, copy_temp))
    record["children"] = [child]
    runs = sorted(copy_out.glob("focused-*/run.json")) if copy_out.is_dir() else []
    copy_record = json.loads(runs[0].read_text()) if len(runs) == 1 else None
    record["copy_run"] = {"run_json": str(runs[0]) if len(runs) == 1 else None,
                          "run_json_sha256": sha256_file(runs[0]) if len(runs) == 1 else None}
    if child.get("exit") != 0 or not copy_record or copy_record.get("result") != "pass":
        reasons.append("copy_focused_failed")
    else:
        modules = copy_record.get("loaded_modules", {})
        record["copy_run"]["loaded_modules"] = modules
        record["copy_run"]["tests_run"] = copy_record["focused"]["tests_run"]
        record["copy_run"]["startup_probes_pass"] = copy_record["sentinels"]["startup_probes"]["pass"]
        if not modules.get("all_under_repo") or any(
                not Path(path).is_relative_to(copy_root) for key, path in modules.items() if key != "all_under_repo"):
            reasons.append("copy_loaded_modules_outside_copy")
        if copy_record.get("repo") != str(copy_root.resolve()):
            reasons.append("copy_repo_root_not_derived_from_file")
        if record["copy_run"]["startup_probes_pass"] is not True:
            reasons.append("copy_startup_probes_failed")
    # Original-prefix focused run in this process, compared with the copy.
    facts = run_tests(run_dir, "original-focused", load_focused())
    record["focused"] = facts
    record["loaded_modules"] = loaded_modules()
    reasons.extend("original:" + reason for reason in focused_reasons(facts))
    if not record["loaded_modules"]["all_under_repo"]:
        reasons.append("module_loaded_outside_repo")
    if copy_record and copy_record["focused"]["tests_run"] != facts["tests_run"]:
        reasons.append("copy_and_original_test_counts_differ")
    return reasons


def require_exact_head(record):
    identity = record["source_identity"]
    if identity.get("clean") is not True:
        return ["tree_not_clean_exact_head_required"]
    return []


def check_credentials(run_dir, record):
    reasons = require_exact_head(record)
    record["sentinels"] = install_sentinels(run_dir)
    reasons += probe_reasons(record["sentinels"])
    scanner = Path(sys.executable).parent / "detect-secrets"
    record["scanner"] = {"path": str(scanner), "present": scanner.is_file()}
    if not scanner.is_file():
        return reasons + ["detect_secrets_missing_beside_interpreter"]
    scan_home, scan_temp = run_dir / "scanner-env" / "home", run_dir / "scanner-env" / "tmp"
    scan_home.mkdir(mode=0o700, parents=True)
    scan_temp.mkdir(mode=0o700)
    # A5: the installed scanner runs with its literal argv; it imports no application code.
    scan = run_child(run_dir, "secret-scan", [scanner, "scan", "--all-files", "--exclude-files", "^.git/",
                                              "--no-verify"], REPO, env=child_env(scan_home, scan_temp))
    report = run_dir / "secret-scan.json"
    if "stdout_path" in scan:
        os.link(scan["stdout_path"], report)
    checker_script = REPO / "tools" / "check_secret_report.py"
    checker = guarded_launch(run_dir, "check-secret-report", "check-secret-report",
                             {"kind": "script", "path": str(checker_script),
                              "argv": [str(checker_script), str(report)]})
    record["children"] = [scan, checker]
    guard_reasons, record["guarded_processes"] = verify_guarded(run_dir, {"check-secret-report": 1})
    reasons += guard_reasons
    try:
        data = json.loads(report.read_text())
        results = data["results"]
        if type(results) is not dict:
            raise ValueError("results not an object")
        record["raw_scanner_hits"] = {path: len(hits) for path, hits in sorted(results.items())}
    except (OSError, ValueError, KeyError, TypeError) as error:
        reasons.append("scan_report_unreadable:" + type(error).__name__)
    if scan.get("exit") != 0:
        reasons.append("detect_secrets_exit_%s" % scan.get("exit"))
    if checker.get("exit") != 0:
        reasons.append("check_secret_report_exit_%s" % checker.get("exit"))
    if "stdout_path" in checker:
        record["checker_summary"] = Path(checker["stdout_path"]).read_text().strip().splitlines()[-1:]
    return reasons


def check_public_tree(run_dir, record):
    reasons = require_exact_head(record)
    record["sentinels"] = install_sentinels(run_dir)
    reasons += probe_reasons(record["sentinels"])
    script = REPO / "tools" / "check_public_tree.py"
    child = guarded_launch(run_dir, "public-tree", "public-tree",
                           {"kind": "script", "path": str(script), "argv": [str(script)]},
                           descendants=[{"role": GIT_LIST_ROLE, "exact_argv": GIT_LIST_ARGV}])
    record["children"] = [child]
    guard_reasons, record["guarded_processes"] = verify_guarded(run_dir, {"public-tree": 1},
                                                                exact_roles=(GIT_LIST_ROLE,))
    reasons += guard_reasons
    if child.get("exit") != 0:
        reasons.append("public_tree_exit_%s" % child.get("exit"))
    return reasons


HANDLERS = {"focused": check_focused, "compatibility": check_compatibility, "negative": check_negative,
            "portability": check_portability, "credentials": check_credentials,
            "public-tree": check_public_tree}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", choices=CHECKS)
    parser.add_argument("--output-root")
    parser.add_argument("--guarded-child", metavar="SPEC", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.guarded_child is not None:
        if args.check is not None or args.output_root is not None:
            parser.error("--guarded-child is an internal bootstrap and takes no other option")
        return guarded_child(args.guarded_child)
    if args.check is None or args.output_root is None:
        parser.error("--check and --output-root are required")
    run_dir = new_run_dir(args.output_root, args.check)
    record = {"schema": "fc3-bundle-json-acceptance/v2", "brief": BRIEF, "check": args.check,
              "argv": [sys.executable, *sys.argv], "cwd": os.getcwd(), "repo": str(REPO),
              "interpreter": sys.executable, "python": sys.version.split()[0],
              "run_dir": str(run_dir), "started_at": stamp(), "review_required": True,
              "full_discovery": "operator-run (not part of this runner)"}
    try:
        record["source_identity"] = source_identity()
        reasons = scope_reasons(record["source_identity"])
        reasons += HANDLERS[args.check](run_dir, record)
    except Exception as error:  # an unavailable or incomplete check fails closed
        reasons = ["check_error:%s:%s" % (type(error).__name__, str(error)[:500])]
    record["sentinel_hits"] = list(SENTINEL_HITS)
    if SENTINEL_HITS:
        reasons.append("sentinel_hits_present")
    record["finished_at"] = stamp()
    record["reasons"] = reasons
    record["result"] = "pass" if not reasons else "fail"
    record["exit"] = 0 if not reasons else 1
    write_new(run_dir / "run.json", json.dumps(record, indent=2, sort_keys=True, default=str) + "\n")
    print(json.dumps({"check": args.check, "result": record["result"], "run_dir": str(run_dir),
                      "reasons": reasons}, sort_keys=True))
    return record["exit"]


if __name__ == "__main__":
    raise SystemExit(main())
