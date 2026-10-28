"""Acceptance runner for FC2-RETAINED-SCOPE (offline, synthetic, review-required).

Usage (one check per invocation):
    <interpreter> tools/accept_fc2_retained_scope.py --check CHECK --output-root DIR

CHECK is one of: focused, compatibility, negative, portability, credentials,
public-tree. Each invocation creates a fresh unique child directory under DIR
(never overwriting or deleting earlier evidence) holding raw child
stdout/stderr, argv, cwd, exit codes, executed test counts/IDs and source
hashes in ``run.json``. The process exits 0 only when every assertion of the
selected check holds; an unknown mode is refused by argparse.

Isolation. This parent never imports application code. Every context that
imports application/test code (and every admitted local grandchild those tests
start) is a child of this same script running under ``-I`` that installs a
narrow offline/process guard before any application import: Python-level
sentinels plus an audit hook cover socket creation for internet families,
connect/connectionless sends/bind/listen/name resolution, and process
creation. Only explicitly listed local children are admitted (git; the
installed scanner from the parent only; the repository's own check scripts and the
records CLI module, which are re-launched under the same guard). Every guard
event is appended to a per-process log in the run directory at the moment it
happens, so a forbidden attempt fails the gate even when the caller catches
the refusal. An inert negative control proves this on every run. This is an
isolation check, not a hostile-code sandbox.

Identity. A missing/invalid candidate HEAD, unavailable git status, a dirty
tree or incomplete source hashes make the run FAIL (receipt still written).

A passing result is machine validity only: REVIEW REQUIRED, never
semantic/legal acceptance, activation or publication authority.
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
import runpy
import secrets
import shutil
import subprocess
import sys
import traceback
import unittest

sys.dont_write_bytecode = True

RUNNER = Path(__file__).resolve()
REPO = RUNNER.parents[1]
RUNNER_NAME = RUNNER.name
BRIEF = "FC2-RETAINED-SCOPE: typed refusal for malformed retained unit scope identifiers (frozen card brief)"
CHECKS = ("focused", "compatibility", "negative", "portability", "credentials", "public-tree")
TARGET_PATH = "campaign_tool/records/retained_units.py"
FOCUSED_PATH = "tests/records/test_fc2_retained_scope.py"
FOCUSED_MODULE = "tests.records.test_fc2_retained_scope"
BASE_SELECTOR = "parent of the first commit adding " + FOCUSED_PATH
COMPAT_TARGETS = (
    "tests.records.test_understanding.RetainedUnitsTests",
    "tests.records.test_understanding.GroundedEvidenceTests",
    "tests.test_secret_report",
    "tests.test_public_tree",
)
HASHED_SOURCES = (
    TARGET_PATH,
    FOCUSED_PATH,
    "tools/accept_fc2_retained_scope.py",
    "tests/records/test_understanding.py",
    "tests/test_secret_report.py",
    "tests/test_public_tree.py",
    "tools/check_secret_report.py",
    "tools/check_public_tree.py",
)
APP_PACKAGES = ("campaign_tool", "tests")
# Local children that guarded contexts may start; each is re-launched under the guard.
ADMITTED_MODULES = ("campaign_tool.records.understanding",)
ADMITTED_SCRIPTS = ("tools/check_secret_report.py", "tools/check_public_tree.py")
CHILD_MODES = ("--guarded-unittest", "--guarded-task", "--guarded-exec", "--guard-control")
STATIC_SCANNED = (FOCUSED_PATH, "tools/accept_fc2_retained_scope.py")
# Assembled from parts so this list does not match its own source text.
BOX_SPECIFIC = ("/ho" + "me/", "agent" + "-work", "venv" + "-fullsuite", "/Us" + "ers/", ".her" + "mes",
                "/t" + "mp/")
INLINE_FORMS = (
    re.compile(r"""["']-""" + r"""[ce]["']"""),
    re.compile("shell" + r"\s*=\s*True"),
    re.compile("PYTHON" + "PATH"),
    re.compile(r"\bos\.sys" + r"tem\("),
)
# The candidate's member-type gate and the uniqueness (set construction) gate inside _scope.
MEMBER_CHECK = 'if any(type(v)is not str or not HEX.fullmatch(v) for v in ids):_no("import_scope_invalid")'
UNIQUE_CHECK = 'if len(set(ids))!=len(ids):_no("import_scope_invalid")'
NEGATIVE_REGRESSIONS = (
    "ScopeMemberTypeTests.test_dict_member_first_entry_rejected_before_sql",
    "ScopeMemberTypeTests.test_list_member_first_entry_rejected_before_sql",
    "ReadCurrentUnitsTests.test_dict_member_rejected_before_scope_sql_and_payload_read",
    "ReadCurrentUnitsTests.test_list_member_rejected_before_scope_sql_and_payload_read",
)
NEGATIVE_POSITIVES = (
    "ScopeMemberTypeTests.test_valid_scope_preserves_order_and_rows",
    "ScopeMemberTypeTests.test_hashable_non_string_members_rejected",
    "ScopeMemberTypeTests.test_wrong_hash_strings_rejected",
    "ScopeMemberTypeTests.test_existing_codes_preserved",
    "ReadCurrentUnitsTests.test_valid_current_import_accepted",
    "ReadCurrentUnitsTests.test_stale_current_pointer_code_preserved",
)
HEAD_FORM = re.compile(r"[0-9a-f]{40}")
SHA256_FORM = re.compile(r"[0-9a-f]{64}")
RAN = re.compile(r"^Ran (\d+) tests? in ", re.M)
SUMMARY = re.compile(r"^(OK|FAILED)(?: \(([^)]*)\))?\s*$", re.M)
VERBOSE = re.compile(r"^(test\w*) \(([\w.]+)\)(?:\n[^\n]*?)? \.\.\. (ok|FAIL|ERROR|skipped.*|expected failure|unexpected success)$",
                     re.M)
# Failure/error block headers, including subTest failures that leave no "... ok" line.
FAILURE_HEADER = re.compile(r"^(ERROR|FAIL): (test\w*) \(([\w.]+)\)", re.M)
UNHASHABLE = re.compile(r"^TypeError: (?:cannot use '[^']+' as a set element \()?unhashable type: '(?:dict|list)'", re.M)


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    return sha256_bytes(Path(path).read_bytes())


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def new_run_dir(output_root, check):
    root = Path(output_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    run = root / ("{}-{}-{}-{}".format(check, now(), os.getpid(), secrets.token_hex(4)))
    run.mkdir(exist_ok=False)
    return run


def app_modules_loaded():
    return sorted(name for name in sys.modules if name.split(".")[0] in APP_PACKAGES)


# ------------------------------------------------------------------ guard

NET_EVENTS = frozenset({"socket.connect", "socket.sendto", "socket.sendmsg", "socket.bind", "socket.getaddrinfo",
                        "socket.gethostbyname", "socket.gethostbyaddr", "socket.getnameinfo"})
PROC_EVENTS = frozenset({"os.system", "os.exec", "os.spawn", "os.posix_spawn", "os.fork", "os.forkpty",
                         "os.startfile"})
SOCKET_METHODS = ("connect", "connect_ex", "sendto", "sendmsg", "bind", "listen")
SOCKET_FUNCTIONS = ("create_connection", "create_server", "getaddrinfo", "gethostbyname", "gethostbyname_ex",
                    "gethostbyaddr", "getnameinfo")
OS_FUNCTIONS = ("system", "execv", "execve", "execvp", "execvpe", "execl", "execle", "execlp", "execlpe", "fork",
                "forkpty", "posix_spawn", "posix_spawnp", "spawnv", "spawnve", "spawnvp", "spawnvpe", "spawnl",
                "spawnle", "spawnlp", "spawnlpe")


def describe(argv):
    if isinstance(argv, (list, tuple)):
        return json.dumps([os.fspath(a) if isinstance(a, (str, os.PathLike)) else repr(a) for a in argv])[:600]
    return repr(argv)[:600]


def local_python_target(rest, cwd):
    """(kind, name, args) when ``rest`` (argv after the interpreter) names an admitted local target."""
    rest = list(rest)
    while rest and rest[0] in ("-B", "-I"):
        rest = rest[1:]
    if len(rest) >= 2 and rest[0] == "-m" and rest[1] in ADMITTED_MODULES:
        return ("module", rest[1], rest[2:])
    if rest and not rest[0].startswith("-"):
        script = (Path(cwd or os.getcwd()) / rest[0]).resolve()
        for relative in ADMITTED_SCRIPTS:
            if script == (REPO / relative).resolve():
                return ("script", str(script), rest[1:])
    return None


class Guard:
    """Narrow offline/process sentinels with an append-on-event log (one file per process)."""

    def __init__(self, guard_dir, context, allow):
        self.guard_dir = Path(guard_dir).resolve()
        self.guard_dir.mkdir(parents=True, exist_ok=True)
        self.context = context
        self.allow = frozenset(allow)
        self.runner_paths = {str(RUNNER)}
        self.scanner = None
        self.hits = []
        self.pending = 0
        self.path = self.guard_dir / "{}-{}-{}.jsonl".format(context, os.getpid(), secrets.token_hex(4))
        self._handle = open(self.path, "x", encoding="utf-8")

    def record(self, kind, name, layer="", detail=""):
        entry = {"kind": kind, "name": name, "layer": layer, "detail": str(detail)[:800], "pid": os.getpid(),
                 "context": self.context}
        if kind == "hit":
            self.hits.append(entry)
        self._handle.write(json.dumps(entry, sort_keys=True) + "\n")
        self._handle.flush()

    def refuse(self, name, layer, detail=""):
        self.record("hit", name, layer, detail)
        raise PermissionError("offline sentinel: " + name)

    def classify(self, argv, cwd):
        if not isinstance(argv, (list, tuple)) or not argv:
            return None
        if not all(isinstance(a, (str, os.PathLike)) for a in argv):
            return None
        argv = [os.fspath(a) for a in argv]
        if argv[0] == "git":
            return "git"
        if self.scanner is not None and argv[0] == self.scanner:
            return "scanner"
        if argv[0] != sys.executable:
            return None
        rest = argv[1:]
        if len(rest) >= 4 and rest[0] == "-I" and rest[1] == "-B" and rest[2] in self.runner_paths \
                and rest[3] in CHILD_MODES:
            return "runner:" + rest[3]
        return "local-python" if local_python_target(rest, cwd) else None

    def allowed(self, kind):
        return kind in self.allow or (kind.startswith("runner:") and "runner:*" in self.allow)


def install_guard(guard_dir, context, allow):
    """Install before any application import. Returns the Guard (its log already has a start record)."""
    import socket
    guard = Guard(guard_dir, context, allow)
    guard.record("start", context, "install", json.dumps({"argv": sys.argv, "allow": sorted(guard.allow),
                                                          "preloaded_app_modules": app_modules_loaded()}))
    inet = {socket.AF_INET}
    if hasattr(socket, "AF_INET6"):
        inet.add(socket.AF_INET6)

    def blocker(name):
        def blocked(*_args, **_kwargs):
            guard.refuse(name, "wrapper")
        return blocked

    for attr in SOCKET_METHODS:
        setattr(socket.socket, attr, blocker("socket." + attr))
    for attr in SOCKET_FUNCTIONS:
        setattr(socket, attr, blocker("socket." + attr))
    for attr in OS_FUNCTIONS:
        if hasattr(os, attr):
            setattr(os, attr, blocker("os." + attr))

    original_init = subprocess.Popen.__init__

    def guarded_init(popen, args, *rest, **kwargs):
        if kwargs.get("executable") is not None or kwargs.get("shell") or (len(rest) >= 2 and rest[1] is not None):
            guard.refuse("subprocess.Popen", "wrapper", describe(args))
        kind = guard.classify(args, kwargs.get("cwd"))
        if kind is None or not guard.allowed(kind):
            guard.refuse("subprocess.Popen", "wrapper", describe(args))
        if kind == "local-python":
            args = [sys.executable, "-I", "-B", str(RUNNER), "--guarded-exec", str(guard.guard_dir), "--"] + \
                   [os.fspath(a) for a in args[1:]]
            guard.record("rewrite", kind, "wrapper", describe(args))
        guard.record("admitted", kind, "wrapper", describe(args))
        try:
            return original_init(popen, args, *rest, **kwargs)
        finally:
            guard.pending = 0

    subprocess.Popen.__init__ = guarded_init

    def hook(event, args):
        if event == "socket.__new__":
            if len(args) > 1 and args[1] in inet:
                guard.refuse(event, "audit", args[1])
            return
        if event in NET_EVENTS:
            guard.refuse(event, "audit")
        if event == "subprocess.Popen":
            executable, argv, cwd = args[0], args[1], args[2]
            kind = guard.classify(argv, cwd)
            first = os.fspath(list(argv)[0]) if isinstance(argv, (list, tuple)) and argv else None
            if kind is None or kind == "local-python" or not guard.allowed(kind) or \
                    (executable is not None and os.fspath(executable) != first):
                guard.refuse(event, "audit", describe(argv))
            guard.pending += 1
            return
        if event in PROC_EVENTS:
            if event in ("os.posix_spawn", "os.fork") and guard.pending > 0:
                guard.pending -= 1
                return
            guard.refuse(event, "audit")

    sys.addaudithook(hook)
    return guard


def guard_summary(guard_dir):
    """Every guard log in ``guard_dir``: processes, admitted children, rewrites and hits."""
    summary = {"dir": str(guard_dir), "files": [], "starts": [], "admitted": 0, "rewrites": 0, "hits": [],
               "malformed": []}
    for path in sorted(Path(guard_dir).glob("*.jsonl")) if Path(guard_dir).is_dir() else []:
        summary["files"].append({"name": path.name, "sha256": sha256_file(path)})
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            try:
                entry = json.loads(line)
                kind = entry["kind"]
            except (ValueError, KeyError, TypeError):
                summary["malformed"].append({"file": path.name, "line": number})
                continue
            if kind == "start":
                detail = json.loads(entry["detail"])
                summary["starts"].append({"file": path.name, "context": entry["context"],
                                          "preloaded_app_modules": detail.get("preloaded_app_modules")})
            elif kind == "admitted":
                summary["admitted"] += 1
            elif kind == "rewrite":
                summary["rewrites"] += 1
            elif kind == "hit":
                summary["hits"].append(dict(entry, file=path.name))
    return summary


def guard_gate_problems(summary):
    """The single gate applied to ordinary runs (and, inverted, to the negative control)."""
    problems = []
    if summary["hits"]:
        problems.append("guard_sentinel_hits")
    if summary["malformed"]:
        problems.append("guard_log_malformed")
    if any(start["preloaded_app_modules"] for start in summary["starts"]):
        problems.append("application_imported_before_guard")
    if not summary["starts"]:
        problems.append("guard_log_missing")
    if len(summary["starts"]) != len(summary["files"]):
        problems.append("guard_log_without_start")
    return problems


# ---------------------------------------------------------------- identity


def source_hashes(root):
    hashes = {}
    for relative in HASHED_SOURCES:
        path = root / relative
        hashes[relative] = sha256_file(path) if path.is_file() else None
    return hashes


def git_identity(repo=None, run=subprocess.run):
    repo = repo or REPO
    identity = {"repo": str(repo)}
    for name, argv in (("head", ["git", "-C", str(repo), "rev-parse", "--verify", "HEAD"]),
                       ("status_porcelain", ["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=all"])):
        try:
            done = run(argv, capture_output=True, text=True, timeout=60, check=False)
            identity[name] = done.stdout.strip() if done.returncode == 0 else None
            identity[name + "_exit"] = done.returncode
            if done.returncode != 0:
                identity[name + "_stderr"] = (done.stderr or "")[:500]
        except (OSError, subprocess.SubprocessError) as error:
            identity[name] = None
            identity[name + "_error"] = "{}: {}".format(type(error).__name__, error)[:500]
    identity["clean"] = identity.get("status_porcelain") == ""
    return identity


def identity_problems(identity, hashes):
    """Fail closed: required HEAD, status, cleanliness and every source hash must be present and valid."""
    problems = []
    head = identity.get("head")
    if not isinstance(head, str) or not HEAD_FORM.fullmatch(head):
        problems.append("git_head_unavailable_or_invalid")
    status = identity.get("status_porcelain")
    if not isinstance(status, str):
        problems.append("git_status_unavailable")
    elif status != "":
        problems.append("worktree_not_clean")
    if set(hashes) != set(HASHED_SOURCES) or not all(isinstance(v, str) and SHA256_FORM.fullmatch(v)
                                                     for v in hashes.values()):
        problems.append("source_hashes_incomplete")
    return problems


def identity_self_check(run_dir):
    """Simulated identity failures must each FAIL; the ordinary exact-head identity must pass."""
    def unavailable(*_args, **_kwargs):
        raise FileNotFoundError("synthetic fault: git executable unavailable")

    hashes = source_hashes(REPO)
    ordinary = git_identity(REPO)
    missing_hash = dict(hashes)
    missing_hash[TARGET_PATH] = None
    cases = {
        "git_unavailable": (git_identity(REPO, run=unavailable), hashes, "git_head_unavailable_or_invalid"),
        "git_failed": (git_identity(run_dir / "not-a-repository"), hashes, "git_head_unavailable_or_invalid"),
        "invalid_head": (dict(ordinary, head="not-a-commit"), hashes, "git_head_unavailable_or_invalid"),
        "status_unavailable": (dict(ordinary, status_porcelain=None), hashes, "git_status_unavailable"),
        "dirty_tree": (dict(ordinary, status_porcelain=" M synthetic"), hashes, "worktree_not_clean"),
        "missing_source_hash": (ordinary, missing_hash, "source_hashes_incomplete"),
        "ordinary_exact_head": (ordinary, hashes, None),
    }
    report = {}
    ok = True
    for name, (identity, case_hashes, expected) in cases.items():
        found = identity_problems(identity, case_hashes)
        passed = (found == []) if expected is None else (expected in found)
        ok = ok and passed
        report[name] = {"problems": found, "expected": expected, "passed": passed,
                        "head": identity.get("head")}
    return {"cases": report, "ok": ok}


# ------------------------------------------------------------- children


def child_env(run_dir, label):
    """Inherited environment with temp, HOME and XDG data defaults redirected into the run directory."""
    scratch = run_dir / (label + "-home")
    scratch.mkdir(mode=0o700)
    env = dict(os.environ)
    for key in ("HOME", "TMPDIR", "TEMP", "TMP", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
        env[key] = str(scratch)
    return env, scratch


def guarded_argv(root, *mode_args):
    return [sys.executable, "-I", "-B", str(Path(root) / "tools" / RUNNER_NAME)] + [str(a) for a in mode_args]


def run_child(run_dir, label, argv, cwd, env=None, stdout_path=None, timeout=900):
    """Run one argument-array child (no shell) and preserve its raw streams."""
    record = {"label": label, "argv": [str(a) for a in argv], "cwd": str(cwd)}
    if env is not None:
        record["env_overrides"] = {k: env[k] for k in ("HOME", "TMPDIR", "TEMP", "TMP", "XDG_CONFIG_HOME",
                                                       "XDG_DATA_HOME", "XDG_CACHE_HOME") if k in env
                                   and env[k] != os.environ.get(k)}
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


def load_child_report(report, record, problems, prefix=""):
    if not Path(report).is_file():
        problems.append(prefix + "child_report_missing")
        return None
    try:
        data = json.loads(Path(report).read_text())
    except ValueError:
        problems.append(prefix + "child_report_malformed")
        return None
    record["report_path"] = str(report)
    record["report_sha256"] = sha256_file(report)
    guard_file = data.get("guard_file")
    if not guard_file or not Path(guard_file).is_file():
        problems.append(prefix + "child_guard_not_recorded")
    if data.get("sentinel_hits"):
        problems.append(prefix + "child_sentinel_hits")
    if data.get("preloaded_app_modules"):
        problems.append(prefix + "child_application_imported_before_guard")
    return data


def defaults_redirected(data, scratch):
    scratch = Path(scratch).resolve()
    return all(isinstance(data.get(k), str) and Path(data[k]).resolve().is_relative_to(scratch)
               for k in ("tempdir", "home"))


def verbose_test_id(name, parenthesized):
    """Actual TestCase.id() from one verbose line (3.11+ prints the full id)."""
    if parenthesized.endswith("." + name):
        return parenthesized
    return parenthesized + "." + name


def parse_unittest(stderr_text):
    ran = RAN.findall(stderr_text)
    summary = SUMMARY.findall(stderr_text)
    tests = [{"name": m.group(1), "id": verbose_test_id(m.group(1), m.group(2)),
              "outcome": "skipped" if m.group(3).startswith("skipped") else m.group(3)}
             for m in VERBOSE.finditer(stderr_text)]
    detail = summary[-1][1] if summary else ""
    counts = dict((k.strip(), int(v)) for k, v in (part.split("=") for part in detail.split(", ") if "=" in part))
    failing = sorted({verbose_test_id(m.group(2), m.group(3)) + " " + m.group(1) for m in FAILURE_HEADER.finditer(stderr_text)})
    return {"tests_run": int(ran[-1]) if ran else 0, "summary": summary[-1][0] if summary else None,
            "summary_detail": detail, "summary_counts": counts, "tests": tests, "failing_test_ids": failing}


class _ParserControl(unittest.TestCase):
    """Synthetic in-memory outcomes used only to check the verbose parser."""

    def test_control_ok(self):
        pass

    def test_control_documented(self):
        """A first docstring line printed beneath the verbose test line."""

    def test_control_fail(self):
        self.fail("deliberate synthetic failure")

    def test_control_error(self):
        raise ValueError("deliberate synthetic error")

    @unittest.skip("deliberate synthetic skip")
    def test_control_skip(self):
        pass


def parser_self_check():
    """Run known ok/docstring/fail/error/skip cases; parsed IDs must equal case.id()."""
    suite = unittest.TestLoader().loadTestsFromTestCase(_ParserControl)
    actual = {case.id(): "ok" for case in suite}
    stream = io.StringIO()
    outcome = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    for case, _ in outcome.failures:
        actual[case.id()] = "FAIL"
    for case, _ in outcome.errors:
        actual[case.id()] = "ERROR"
    for case, _ in outcome.skipped:
        actual[case.id()] = "skipped"
    parsed = parse_unittest(stream.getvalue())
    recorded = {t["id"]: t["outcome"] for t in parsed["tests"]}
    return {"expected": actual, "parsed": recorded, "tests_run": parsed["tests_run"],
            "match": recorded == actual and len(parsed["tests"]) == len(actual) == parsed["tests_run"],
            "python_version": sys.version}


def unittest_child(run_dir, label, target, root=None):
    """Guarded unittest child; parsed verbose stream must match the child's loaded and started IDs."""
    root = Path(root or REPO).resolve()
    report = run_dir / (label + ".report.json")
    env, scratch = child_env(run_dir, label)
    argv = guarded_argv(root, "--guarded-unittest", target, "--child-report", report, "--guard-dir",
                        run_dir / "guard")
    record = run_child(run_dir, label, argv, root, env=env)
    stderr_text = Path(record["stderr_path"]).read_text(errors="replace") if "stderr_path" in record else ""
    parsed = parse_unittest(stderr_text)
    record.update(parsed)
    problems = []
    record["parser_self_check"] = parser_self_check()
    if not record["parser_self_check"]["match"]:
        problems.append("parser_self_check_failed")
    child = load_child_report(report, record, problems) or {}
    expected_ids = child.get("loaded_suite_ids")
    started_ids = child.get("started_ids")
    parsed_ids = [t["id"] for t in parsed["tests"]]
    record["loaded_suite_ids"] = expected_ids
    record["parsed_ids_match_loaded_suite"] = (isinstance(expected_ids, list) and isinstance(started_ids, list)
                                               and len(parsed_ids) == len(set(parsed_ids))
                                               and sorted(parsed_ids) == sorted(expected_ids)
                                               == sorted(started_ids))
    if not record["parsed_ids_match_loaded_suite"]:
        problems.append("parsed_ids_differ_from_loaded_suite")
    record["child_repo_root"] = child.get("repo_root")
    record["loaded_modules"] = child.get("loaded_modules")
    record["modules_outside_root"] = child.get("modules_outside_root")
    record["defaults"] = {k: child.get(k) for k in ("tempdir", "home")}
    if child.get("repo_root") != str(root):
        problems.append("child_used_wrong_root")
    if child.get("modules_outside_root") or not child.get("loaded_modules"):
        problems.append("application_modules_loaded_outside_root")
    if not defaults_redirected(child, scratch):
        problems.append("child_defaults_not_redirected")
    if record.get("exit") != 0:
        problems.append("unittest_exit_nonzero")
    if parsed["summary"] != "OK":
        problems.append("unittest_summary_not_ok")
    if parsed["tests_run"] <= 0:
        problems.append("no_tests_executed")
    if len(parsed["tests"]) != parsed["tests_run"]:
        problems.append("executed_test_ids_incomplete")
    extras = sorted(k for k in parsed["summary_counts"] if k in ("skipped", "expected failures", "unexpected successes"))
    if extras:
        problems.append("unexpected_" + "_".join(k.replace(" ", "_") for k in extras))
    bad = [t["id"] for t in parsed["tests"] if t["outcome"] != "ok"]
    if bad:
        problems.append("non_ok_tests")
    record["non_ok_test_ids"] = bad
    record["problems"] = problems
    return record, problems


def task_child(run_dir, result, task):
    """Guarded in-process task (baseline probe or negative control) in a child; returns its problems."""
    report = run_dir / ("task-" + task + ".report.json")
    env, scratch = child_env(run_dir, "task-" + task)
    argv = guarded_argv(REPO, "--guarded-task", task, "--run-dir", run_dir, "--child-report", report,
                        "--guard-dir", run_dir / "guard")
    record = run_child(run_dir, "task-" + task, argv, REPO, env=env)
    result["children"].append(record)
    problems = []
    data = load_child_report(report, record, problems, prefix=task + ":") or {}
    result[task] = data.get("data")
    if not isinstance(data.get("problems"), list):
        problems.append(task + ":child_incomplete")
    else:
        problems.extend(task + ":" + p for p in data["problems"])
    if record.get("exit") != 0:
        problems.append(task + ":child_exit_nonzero")
    if data and not defaults_redirected(data, scratch):
        problems.append(task + ":child_defaults_not_redirected")
    return problems


# ------------------------------------------------- in-child application work


def _load_in_process(root):
    root_text = str(root)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    from campaign_tool.records import retained_units
    focused = __import__(FOCUSED_MODULE, fromlist=["*"])
    for module in (retained_units, focused):
        if not Path(module.__file__).resolve().is_relative_to(Path(root).resolve()):
            raise RuntimeError("module_loaded_outside_candidate")
    return retained_units, focused


def _scope_source(source_text):
    """Exact source segment of the module-level ``_scope`` function."""
    tree = ast.parse(source_text)
    found = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_scope"]
    if len(found) != 1:
        raise RuntimeError("scope_function_not_unique")
    return ast.get_source_segment(source_text, found[0])


def _compile_scope(retained_units, source_segment, label):
    """Compile one ``_scope`` variant against a copy of the real module globals (no disk write)."""
    namespace = dict(retained_units.__dict__)
    exec(compile(source_segment, "<{}>".format(label), "exec"), namespace)
    return namespace["_scope"]


def baseline_commit():
    """Pinned base = parent of the commit that first added the focused test module (derived, not a literal)."""
    done = subprocess.run(["git", "-C", str(REPO), "log", "--format=%H", "--diff-filter=A", "HEAD", "--", FOCUSED_PATH],
                          capture_output=True, text=True, timeout=60, check=True)
    added = [line for line in done.stdout.split() if line]
    if not added:
        raise RuntimeError("focused_test_commit_not_found")
    parent = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--verify", added[-1] + "^1"],
                            capture_output=True, text=True, timeout=60, check=True)
    return parent.stdout.strip()


def _probe(scope, focused, retained_units):
    """Dict/list first-entry members against the recording stub: exception class, code and SQL calls."""
    rows = []
    for name, member in (("dict", {"unit_id": focused.hexid("probe:u0")}), ("list", [focused.hexid("probe:u0")])):
        ledger = focused.RecordingLedger()
        declared = focused.declared_with([member], label="probe")
        try:
            scope(ledger, [declared])
            outcome = {"raised": None}
        except retained_units.RetainedUnitError as error:
            outcome = {"raised": "RetainedUnitError", "code": error.code}
        except Exception as error:  # The defect under test: record the raw class and message.
            outcome = {"raised": type(error).__name__, "message": str(error)[:200]}
        outcome.update({"member": name, "sql_calls": len(ledger.calls)})
        rows.append(outcome)
    return rows


def baseline_probe(run_dir, result):
    """Measure the unchanged base _scope (from the git object) next to the candidate, in memory only."""
    problems = []
    retained_units, focused = _load_in_process(REPO)
    base = baseline_commit()
    done = subprocess.run(["git", "-C", str(REPO), "show", base + ":" + TARGET_PATH],
                          capture_output=True, timeout=60, check=True)
    base_source = done.stdout.decode()
    base_scope = _compile_scope(retained_units, _scope_source(base_source), "git {}:{} _scope".format(base, TARGET_PATH))
    data = {"baseline_commit": base, "baseline_selector": BASE_SELECTOR,
            "baseline_target_sha256": sha256_bytes(done.stdout),
            "candidate_target_sha256": sha256_file(REPO / TARGET_PATH),
            "baseline": _probe(base_scope, focused, retained_units),
            "candidate": _probe(retained_units._scope, focused, retained_units)}
    path = run_dir / "baseline-probe.json"
    with open(path, "x") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
    result["baseline_probe"] = dict(data, path=str(path), sha256=sha256_file(path))
    if not all(r["raised"] == "TypeError" and r["sql_calls"] == 0 for r in data["baseline"]):
        problems.append("baseline_defect_not_reproduced")
    if not all(r["raised"] == "RetainedUnitError" and r.get("code") == "import_scope_invalid" and r["sql_calls"] == 0
               for r in data["candidate"]):
        problems.append("candidate_not_typed_refusal_before_sql")
    return problems


def _run_cases(focused, names):
    loader = unittest.TestLoader()
    suite = unittest.TestSuite(loader.loadTestsFromName(name, focused) for name in names)
    stream = io.StringIO()
    outcome = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    return outcome, stream.getvalue()


def _parent_id(case):
    """A subTest failure reports a _SubTest; attribute it to its owning test method."""
    return getattr(case, "test_case", case).id()


def _summarise(outcome):
    unhashable = {}
    for case, text in outcome.errors:
        unhashable.setdefault(_parent_id(case), []).append(bool(UNHASHABLE.search(text)))
    return {"tests_run": outcome.testsRun,
            "failures": sorted({_parent_id(case) for case, _ in outcome.failures}),
            "errors": sorted({_parent_id(case) for case, _ in outcome.errors}),
            "error_entries": [case.id() for case, _ in outcome.errors],
            "error_unhashable_typeerror": {k: all(v) for k, v in unhashable.items()},
            "skipped": [case.id() for case, _ in outcome.skipped],
            "unexpected_successes": [case.id() for case in outcome.unexpectedSuccesses],
            "successful": outcome.wasSuccessful()}


def mutant_segment(candidate_segment):
    """Swap only the member-type gate and the set-construction gate (set() first, as on the base)."""
    lines = candidate_segment.split("\n")
    member = [i for i, line in enumerate(lines) if line.strip() == MEMBER_CHECK]
    unique = [i for i, line in enumerate(lines) if line.strip() == UNIQUE_CHECK]
    if len(member) != 1 or len(unique) != 1:
        raise RuntimeError("candidate_gates_not_found_exactly_once")
    if not member[0] < unique[0]:
        raise RuntimeError("candidate_member_gate_not_before_set_gate")
    i, j = member[0], unique[0]
    lines[i], lines[j] = lines[j], lines[i]
    return "\n".join(lines), {"member_line": i, "unique_line": j}


def negative_in_child(run_dir, result):
    """In-memory mutant: set() uniqueness moves ahead of member type validation; nothing on disk changes."""
    problems = []
    disk_before = sha256_file(REPO / TARGET_PATH)
    retained_units, focused = _load_in_process(REPO)
    original = retained_units._scope
    candidate_segment = _scope_source((REPO / TARGET_PATH).read_text())
    segment, where = mutant_segment(candidate_segment)
    mutant = _compile_scope(retained_units, segment, "in-memory mutant _scope")
    phases = {"swapped_lines_in_scope": where, "mutant_source_sha256": sha256_bytes(segment.encode()),
              "candidate_scope_sha256": sha256_bytes(candidate_segment.encode())}
    try:
        retained_units._scope = mutant
        regress, regress_text = _run_cases(focused, NEGATIVE_REGRESSIONS)
        positive, positive_text = _run_cases(focused, NEGATIVE_POSITIVES)
        phases["mutant_regressions"] = _summarise(regress)
        phases["mutant_positives"] = _summarise(positive)
        (run_dir / "negative-mutant.txt").write_text(regress_text + "\n" + positive_text)
    finally:
        retained_units._scope = original
    phases["restored_scope"] = retained_units._scope is original
    stream = io.StringIO()
    restored = unittest.TextTestRunner(stream=stream, verbosity=2).run(unittest.TestLoader().loadTestsFromModule(focused))
    restored_text = stream.getvalue()
    phases["restored"] = _summarise(restored)
    (run_dir / "negative-restored.txt").write_text(restored_text)
    for name in ("negative-mutant.txt", "negative-restored.txt"):
        phases[name + "_sha256"] = sha256_file(run_dir / name)
    phases["target_unchanged_on_disk"] = sha256_file(REPO / TARGET_PATH) == disk_before
    result["negative"] = phases
    mutant_result = phases["mutant_regressions"]
    expected = {focused.__name__ + "." + name for name in NEGATIVE_REGRESSIONS}
    if (mutant_result["tests_run"] != len(NEGATIVE_REGRESSIONS) or set(mutant_result["errors"]) != expected
            or mutant_result["failures"]):
        problems.append("mutant_not_detected_as_error_by_every_regression")
    if not all(mutant_result["error_unhashable_typeerror"].get(name) for name in expected):
        problems.append("mutant_detection_not_raw_unhashable_typeerror")
    pos = phases["mutant_positives"]
    if not pos["successful"] or pos["tests_run"] != len(NEGATIVE_POSITIVES) or pos["skipped"]:
        problems.append("positives_not_green_under_mutant")
    if not phases["restored_scope"]:
        problems.append("scope_not_restored")
    rest = phases["restored"]
    if not rest["successful"] or rest["tests_run"] <= len(NEGATIVE_REGRESSIONS) + len(NEGATIVE_POSITIVES) \
            or rest["skipped"]:
        problems.append("restored_module_not_green")
    if not phases["target_unchanged_on_disk"]:
        problems.append("target_changed_on_disk")
    return problems


TASKS = {"baseline": baseline_probe, "negative": negative_in_child}


def _child_common(guard, data):
    import tempfile
    data.update({"guard_file": str(guard.path), "sentinel_hits": guard.hits, "repo_root": str(REPO),
                 "tempdir": os.path.realpath(tempfile.gettempdir()), "home": str(Path.home()),
                 "preloaded_app_modules": json.loads(guard_start_detail(guard)).get("preloaded_app_modules")})


def guard_start_detail(guard):
    first = Path(guard.path).read_text(encoding="utf-8").splitlines()[0]
    return json.loads(first)["detail"]


class _StartedResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.started_ids = []

    def startTest(self, test):
        self.started_ids.append(test.id())
        super().startTest(test)


def guarded_unittest_main(target, report, guard_dir):
    guard = install_guard(guard_dir, "unittest", allow={"local-python", "runner:--guarded-exec"})
    sys.path[0] = str(REPO)
    suite = unittest.TestLoader().loadTestsFromName(target)
    loaded = []

    def walk(item):
        if isinstance(item, unittest.TestSuite):
            for child in item:
                walk(child)
        else:
            loaded.append(item.id())
    walk(suite)
    outcome = unittest.TextTestRunner(stream=sys.stderr, verbosity=2, resultclass=_StartedResult).run(suite)
    modules = {}
    outside = []
    for name in app_modules_loaded():
        path = getattr(sys.modules[name], "__file__", None)
        if path is None:
            continue
        modules[name] = path
        if not Path(path).resolve().is_relative_to(REPO):
            outside.append(name)
    data = {"target": target, "loaded_suite_ids": loaded, "started_ids": outcome.started_ids,
            "tests_run": outcome.testsRun, "failures": [c.id() for c, _ in outcome.failures],
            "errors": [c.id() for c, _ in outcome.errors], "skipped": [c.id() for c, _ in outcome.skipped],
            "loaded_modules": modules, "modules_outside_root": outside}
    _child_common(guard, data)
    with open(report, "x") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
    ok = outcome.wasSuccessful() and outcome.testsRun > 0 and not outcome.skipped and not guard.hits \
        and not outside
    return 0 if ok else 1


def guarded_task_main(task, run_dir, report, guard_dir):
    guard = install_guard(guard_dir, "task-" + task, allow={"git"})
    sys.path[0] = str(REPO)
    data = {}
    try:
        problems = TASKS[task](Path(run_dir), data)
    except Exception as error:  # Incomplete task: record the class, fail closed.
        problems = ["task_incomplete:" + type(error).__name__]
        data["exception_traceback"] = traceback.format_exc()[-4000:]
    out = {"task": task, "data": data, "problems": problems}
    _child_common(guard, out)
    with open(report, "x") as handle:
        json.dump(out, handle, indent=2, sort_keys=True, default=str)
    return 0 if not problems and not guard.hits else 1


def guarded_exec_main(guard_dir, rest):
    """Run one admitted local script/module under the guard, emulating ``python [-B] script|-m module``."""
    guard = install_guard(guard_dir, "exec", allow={"git", "local-python", "runner:--guarded-exec"})
    target = local_python_target(rest, os.getcwd())
    if target is None:
        guard.record("hit", "unadmitted_exec", "runner", describe(rest))
        return 2
    kind, name, args = target
    if kind == "module":
        sys.path[0] = os.getcwd()
        sys.argv = [name] + list(args)
        runpy.run_module(name, run_name="__main__", alter_sys=True)
    else:
        sys.path[0] = os.path.dirname(name)
        sys.argv = [name] + list(args)
        runpy.run_path(name, run_name="__main__")
    return 0


# Inert negative controls: every attempt targets a nonexistent local path, a numeric loopback-free lookup or an
# unconnected socket, so nothing is sent or spawned even if a sentinel were missing.
CONTROL_EXPECT = {
    "sendto_connectionless": ("socket.sendto", "wrapper"),
    "sendto_audit_layer": ("socket.sendto", "audit"),
    "sendmsg": ("socket.sendmsg", "wrapper"),
    "connect": ("socket.connect", "wrapper"),
    "bind": ("socket.bind", "wrapper"),
    "inet_socket_creation": ("socket.__new__", "audit"),
    "getaddrinfo": ("socket.getaddrinfo", "wrapper"),
    "unlisted_subprocess": ("subprocess.Popen", "wrapper"),
    "unadmitted_local_python": ("subprocess.Popen", "wrapper"),
    "posix_spawn": ("os.posix_spawn", "wrapper"),
    "posix_spawn_audit_layer": ("os.posix_spawn", "audit"),
    "execv": ("os.execv", "wrapper"),
}


def guard_control_main(report, guard_dir, scratch):
    import socket
    guard = install_guard(guard_dir, "control", allow=())
    scratch = Path(scratch)
    # Unique abstract-namespace AF_UNIX name that nothing listens on (short; no filesystem entry).
    missing = b"\0fc2-guard-control-" + secrets.token_hex(8).encode()
    program = str(scratch / "no-such-program")
    attempts = {}
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
    unbound = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    raw_sendto = getattr(socket, "_socket").socket.sendto
    actions = {
        "sendto_connectionless": lambda: left.sendto(b"inert", missing),
        "sendto_audit_layer": lambda: raw_sendto(left, b"inert", missing),
        "sendmsg": lambda: left.sendmsg([b"inert"], [], 0, missing),
        "connect": lambda: unbound.connect(missing),
        "bind": lambda: unbound.bind(missing),
        "inet_socket_creation": lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM).close(),
        "getaddrinfo": lambda: socket.getaddrinfo("0.0.0.0", None, flags=socket.AI_NUMERICHOST),
        "unlisted_subprocess": lambda: subprocess.run([program], check=False),
        "unadmitted_local_python": lambda: subprocess.run([sys.executable, "-I", "-B", program + ".py"], check=False),
        "posix_spawn": lambda: os.posix_spawn(program, [program], {}),
        "posix_spawn_audit_layer": lambda: __import__("posix").posix_spawn(program, [program], {}),
        "execv": lambda: os.execv(program, [program]),
    }
    for label, action in actions.items():
        before = len(guard.hits)
        try:
            action()  # Deliberately caught below: a caught attempt must still fail the gate.
            raised = None
        except Exception as error:
            raised = type(error).__name__
        attempts[label] = {"raised": raised, "hits": guard.hits[before:]}
    for sock in (left, right, unbound):
        sock.close()
    data = {"attempts": attempts, "scratch_entries": sorted(p.name for p in scratch.iterdir())}
    _child_common(guard, data)
    with open(report, "x") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
    return 0 if not guard.hits else 1


def run_guard_control(run_dir, result):
    """The ordinary gate must FAIL on caught, inert forbidden attempts; returns problems if it does not."""
    problems = []
    control_dir = run_dir / "guard-control"
    env, scratch = child_env(run_dir, "guard-control")
    endpoints = run_dir / "guard-control-endpoints"
    endpoints.mkdir(mode=0o700)
    report = run_dir / "guard-control.report.json"
    argv = guarded_argv(REPO, "--guard-control", "--child-report", report, "--guard-dir", control_dir,
                        "--scratch", endpoints)
    record = run_child(run_dir, "guard-control", argv, REPO, env=env)
    result["children"].append(record)
    data = {}
    if report.is_file():
        data = json.loads(report.read_text())
        record["report_sha256"] = sha256_file(report)
    else:
        problems.append("guard_control:report_missing")
    summary = guard_summary(control_dir)
    gate = guard_gate_problems(summary)
    checks = {}
    for label, (name, layer) in CONTROL_EXPECT.items():
        attempt = data.get("attempts", {}).get(label, {})
        checks[label] = (attempt.get("raised") == "PermissionError"
                         and any(h["name"] == name and h["layer"] == layer for h in attempt.get("hits", [])))
    result["guard_control"] = {"exit": record.get("exit"), "checks": checks, "gate_problems": gate,
                               "hits_logged": len(summary["hits"]), "scratch_entries": data.get("scratch_entries"),
                               "starts": summary["starts"], "files": summary["files"]}
    if not all(checks.values()) or set(checks) != set(CONTROL_EXPECT):
        problems.append("guard_control:sentinel_missing_or_not_refused")
    if "guard_sentinel_hits" not in gate or len(summary["hits"]) < len(CONTROL_EXPECT):
        problems.append("guard_control:caught_attempts_not_failing_gate")
    if record.get("exit") == 0:
        problems.append("guard_control:child_exit_zero_despite_hits")
    if data.get("scratch_entries"):
        problems.append("guard_control:attempt_had_side_effect")
    if sorted(p.name for p in endpoints.iterdir()):
        problems.append("guard_control:endpoint_created")
    return problems


# ----------------------------------------------------------------- checks


def check_focused(run_dir, result):
    record, problems = unittest_child(run_dir, "focused-unittest", FOCUSED_MODULE)
    result["children"].append(record)
    return problems + task_child(run_dir, result, "baseline")


def check_compatibility(run_dir, result):
    problems = []
    for index, target in enumerate(COMPAT_TARGETS, 1):
        record, found = unittest_child(run_dir, "compatibility-{}".format(index), target)
        result["children"].append(record)
        problems.extend(target + ":" + p for p in found)
    result["compatibility_total_tests"] = sum(c.get("tests_run", 0) for c in result["children"]
                                              if c["label"].startswith("compatibility-"))
    result["full_discovery"] = "operator-run (not executed by this runner)"
    return problems


def check_negative(run_dir, result):
    return task_child(run_dir, result, "negative")


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
    done = subprocess.run(["git", "-C", str(REPO), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                          capture_output=True, timeout=120, check=True)
    names = [name for name in done.stdout.decode().split("\0") if name]
    return sorted(set(names))


def check_portability(run_dir, result):
    problems = []
    findings = static_inspection(REPO)
    result["static_inspection"] = {"files": list(STATIC_SCANNED), "tokens": len(BOX_SPECIFIC), "findings": findings}
    if findings:
        problems.append("static_box_specific_or_inline_findings")
    record, found = unittest_child(run_dir, "portability-original-prefix", FOCUSED_MODULE, REPO)
    result["children"].append(record)
    problems.extend("original:" + p for p in found)
    # Second, different absolute prefix: a preserved copy inside this run directory (never deleted).
    copy_root = run_dir / "relocated" / "candidate-source"
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
    PARENT["guard"].runner_paths.add(str((copy_root / "tools" / RUNNER_NAME).resolve()))
    copy_record, found = unittest_child(run_dir, "portability-relocated", FOCUSED_MODULE, copy_root.resolve())
    result["children"].append(copy_record)
    problems.extend("relocated:" + p for p in found)
    for key, rec in (("portability_original_prefix", record), ("portability_relocated", copy_record)):
        result[key] = {k: rec.get(k) for k in ("child_repo_root", "tests_run", "non_ok_test_ids", "loaded_modules",
                                                "modules_outside_root", "defaults")}
    same = record.get("tests_run") == copy_record.get("tests_run") and record.get("tests_run", 0) > 0
    result["portability_same_test_count"] = same
    if not same:
        problems.append("prefix_test_counts_differ")
    if record.get("child_repo_root") == copy_record.get("child_repo_root"):
        problems.append("prefixes_not_distinct")
    return problems


def check_credentials(run_dir, result):
    problems = []
    scanner = Path(sys.executable).parent / "detect-secrets"
    if not scanner.is_file():
        result["scanner"] = {"path": str(scanner), "present": False}
        return ["detect_secrets_unavailable_adjacent_to_interpreter"]
    PARENT["guard"].scanner = str(scanner)
    report = run_dir / "secret-scan.json"
    scan = run_child(run_dir, "detect-secrets-scan",
                     [scanner, "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"],
                     REPO, stdout_path=report)
    result["children"].append(scan)
    version = run_child(run_dir, "detect-secrets-version", [scanner, "--version"], REPO)
    result["children"].append(version)
    if scan.get("exit") != 0:
        problems.append("secret_scan_failed")
    try:
        data = json.loads(report.read_text())
        if not isinstance(data.get("results"), dict):
            raise ValueError("results")
        result["secret_scan"] = {"files_with_hits": len(data["results"]),
                                 "raw_hits": sum(len(v) for v in data["results"].values()),
                                 "hits_in_card_files": sorted(f for f in data["results"] if f in STATIC_SCANNED
                                                              or f == TARGET_PATH)}
    except (OSError, ValueError, AttributeError):
        problems.append("secret_report_malformed")
        return problems
    if result["secret_scan"]["hits_in_card_files"]:
        problems.append("raw_scanner_hits_in_card_files")
    env, _ = child_env(run_dir, "check-secret-report")
    checker = run_child(run_dir, "check-secret-report",
                        guarded_argv(REPO, "--guarded-exec", run_dir / "guard", "--", "-B",
                                     REPO / "tools" / "check_secret_report.py", report), REPO, env=env)
    result["children"].append(checker)
    text = Path(checker["stdout_path"]).read_text(errors="replace") if "stdout_path" in checker else ""
    match = re.search(r"potential secrets: (\d+);", text)
    potential = int(match.group(1)) if match else None
    result["secret_scan"]["potential_secrets"] = potential
    if checker.get("exit") != 0:
        problems.append("check_secret_report_failed")
    if potential != 0:
        problems.append("potential_secrets_nonzero_or_unreported")
    return problems


def check_public_tree(run_dir, result):
    env, _ = child_env(run_dir, "check-public-tree")
    record = run_child(run_dir, "check-public-tree",
                       guarded_argv(REPO, "--guarded-exec", run_dir / "guard", "--", "-B",
                                    REPO / "tools" / "check_public_tree.py"), REPO, env=env)
    result["children"].append(record)
    return [] if record.get("exit") == 0 else ["public_tree_check_failed"]


HANDLERS = {"focused": check_focused, "compatibility": check_compatibility, "negative": check_negative,
            "portability": check_portability, "credentials": check_credentials, "public-tree": check_public_tree}
PARENT = {}


def child_main(argv):
    mode = argv[0]
    if mode == "--guarded-exec":
        if len(argv) < 4 or argv[2] != "--":
            raise SystemExit(2)
        return guarded_exec_main(argv[1], argv[3:])
    parser = argparse.ArgumentParser(prog=RUNNER_NAME + " " + mode)
    parser.add_argument("--child-report", required=True)
    parser.add_argument("--guard-dir", required=True)
    if mode == "--guarded-unittest":
        parser.add_argument("target")
        args = parser.parse_args(argv[1:])
        return guarded_unittest_main(args.target, args.child_report, args.guard_dir)
    if mode == "--guarded-task":
        parser.add_argument("task", choices=sorted(TASKS))
        parser.add_argument("--run-dir", required=True)
        args = parser.parse_args(argv[1:])
        return guarded_task_main(args.task, args.run_dir, args.child_report, args.guard_dir)
    parser.add_argument("--scratch", required=True)
    args = parser.parse_args(argv[1:])
    return guard_control_main(args.child_report, args.guard_dir, args.scratch)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in CHILD_MODES:
        return child_main(argv)
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", choices=CHECKS, required=True)
    parser.add_argument("--output-root", required=True)
    args = parser.parse_args(argv)
    run_dir = new_run_dir(args.output_root, args.check)
    guard = install_guard(run_dir / "guard", "parent", allow={"git", "scanner", "runner:*"})
    PARENT["guard"] = guard
    result = {"schema": "fc2-retained-scope-acceptance-v2", "brief": BRIEF, "check": args.check,
              "argv": [sys.executable] + list(sys.argv),
              "cwd": os.getcwd(), "interpreter": sys.executable, "python_version": sys.version,
              "repo_root": str(REPO), "run_dir": str(run_dir), "started_utc": now(),
              "base_selector": BASE_SELECTOR, "git": git_identity(), "source_hashes": source_hashes(REPO),
              "children": [], "acceptance": "REVIEW REQUIRED; machine validity only, not semantic/legal acceptance",
              "full_discovery": "operator-run (not executed by this runner)"}
    problems = identity_problems(result["git"], result["source_hashes"])
    result["identity_problems"] = list(problems)
    try:
        result["identity_self_check"] = identity_self_check(run_dir)
        if not result["identity_self_check"]["ok"]:
            problems.append("identity_self_check_failed")
        problems.extend(run_guard_control(run_dir, result))
        problems.extend(HANDLERS[args.check](run_dir, result))
    except Exception as error:  # Incomplete check: record the class, fail closed.
        problems.append("check_incomplete:" + type(error).__name__)
        result["exception"] = repr(error)[:2000]
        result["exception_traceback"] = traceback.format_exc()[-4000:]
    result["git_end"] = git_identity()
    if (result["git_end"].get("head"), result["git_end"].get("status_porcelain")) != \
            (result["git"].get("head"), result["git"].get("status_porcelain")):
        problems.append("identity_changed_during_run")
    if source_hashes(REPO) != result["source_hashes"]:
        problems.append("source_changed_during_run")
    result["guard"] = guard_summary(run_dir / "guard")
    problems.extend(guard_gate_problems(result["guard"]))
    result["parent_app_modules"] = app_modules_loaded()
    if result["parent_app_modules"]:
        problems.append("parent_imported_application_code")
    result["finished_utc"] = now()
    result["problems"] = problems
    result["verdict"] = "PASS_REVIEW_REQUIRED" if not problems else "FAIL"
    with open(run_dir / "run.json", "x") as handle:
        json.dump(result, handle, indent=2, sort_keys=True, default=str)
    print(json.dumps({"check": args.check, "verdict": result["verdict"], "problems": problems,
                      "run_dir": str(run_dir), "head": result["git"].get("head"),
                      "clean": result["git"].get("clean"),
                      "run_json_sha256": sha256_file(run_dir / "run.json")}, sort_keys=True))
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
