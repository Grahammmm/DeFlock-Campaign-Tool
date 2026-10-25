"""Scoped offline acceptance runner for FC7-CATALOG-JOIN-JSON.

Exactly one --check mode per invocation; evidence goes to a fresh, exclusive run
directory below --output-root, which must be absolute and outside this repo.

  focused        tests/records/test_fc7_catalog_join_json.py (the six frozen cases)
  compatibility  unchanged tests.records.test_ledger_catalog and
                 tests.records.test_correspondence: baseline (a local clone checked
                 out at the pinned base, child process) and candidate (this tree,
                 in-process under sentinels); identical test IDs and counts required
  negative       the strict catalog decoder seam (_strict_loads) is replaced in
                 memory by ordinary json.loads; the deciding last-wins/nonfinite
                 rejection cases must FAIL with "CatalogError not raised", the
                 shape/positive controls must still pass, and once restored every
                 focused case must pass again
  portability    the focused check re-run from a local clone of the exact committed
                 HEAD at a different absolute prefix; loaded module paths recorded
  credentials    the installed detect-secrets declared in SOURCE-MANIFEST.json,
                 then the unchanged tools/check_secret_report.py on that report
  public-tree    the unchanged tools/check_public_tree.py

Before any mode, SOURCE-MANIFEST.json beside the repo is read and its base,
remote, repo and runtime declarations are verified.

In-process test modes install, before any application import: bytecode
suppression, tempfile redirection into the run directory, socket refusal,
model-client import refusal and an audit hook that refuses writes and sqlite
connections outside the run directory and every process launch. These are
sentinels, not a hostile-code sandbox. Child processes do not inherit them.

A pass prints REVIEW_REQUIRED. It is not full-repository CI, nor semantic,
legal, stage or release acceptance. Full discovery is operator-run.
"""
import argparse
import ast
import datetime
import hashlib
import importlib.abc
import io
import json
import os
from pathlib import Path
import platform
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

REPO = Path(__file__).resolve().parents[1]
MANIFEST = REPO.parent / "SOURCE-MANIFEST.json"
SCHEMA = "fc7-catalog-join-json-acceptance/v1"
BRIEF_ID = "FC7-CATALOG-JOIN-JSON"
BRIEF_FILE = "deflock-FC7-CATALOG-JOIN-JSON.brief"
# Identities are kept as integers so no quoted hex literal enters the credential scan.
BRIEF_SHA256 = format(26888323255694551838473158950093650890431567169789929563084079390494070396531, "064x")
BASE_COMMIT = format(437689161567325937567274471431382424599200637084, "040x")
BASE_TARGET_SHA256 = format(25170324857048651017738756247022821424026670759656420149627233314376374359937, "064x")
TARGET = "campaign_tool/records/ledger_catalog.py"
MODES = ("focused", "compatibility", "negative", "portability", "credentials", "public-tree")
ALLOWED_CHANGES = (TARGET, "tests/records/test_fc7_catalog_join_json.py", "tools/accept_fc7_catalog_join_json.py")
FOCUSED = "tests.records.test_fc7_catalog_join_json"
FOCUSED_CLASS = FOCUSED + ".CatalogJoinJsonTests"
FROZEN_CASES = ("valid_typed_join_projection", "duplicate_source_and_locator", "nested_and_identical_duplicate",
                "nonfinite_constants_and_overflow", "malformed_and_consumer_shape_errors",
                "reordering_whitespace_and_source_unchanged")
COMPATIBILITY = ("tests.records.test_ledger_catalog", "tests.records.test_correspondence")
NEGATIVE_DECIDING = ("duplicate_source_and_locator", "nested_and_identical_duplicate",
                     "nonfinite_constants_and_overflow")
NEGATIVE_CONTROL = ("valid_typed_join_projection", "malformed_and_consumer_shape_errors")
# Otherwise-valid last-wins evidence: ordinary json.loads silently cites page:1.
LAST_WINS_PROBE = '{"source_sha256": "s", "locator": "page:shadow", "locator": "page:1"}'
NONFINITE_PROBE = '{"locator": "page:1", "score": 1e400, "other": NaN}'
HASHED = ALLOWED_CHANGES + ("tests/records/test_ledger_catalog.py", "tests/records/test_correspondence.py",
                            "tools/check_secret_report.py", "tools/check_public_tree.py",
                            "campaign_tool/records/catalog_links.py", "campaign_tool/records/ledger/store.py",
                            "campaign_tool/records/ledger/stages.py")
CLOSURE_ROOTS = {
    "focused": (TARGET, "tests/records/test_fc7_catalog_join_json.py"),
    "negative": (TARGET, "tests/records/test_fc7_catalog_join_json.py"),
    "compatibility": ("tests/records/test_ledger_catalog.py", "tests/records/test_correspondence.py"),
}
FORBIDDEN_IMPORTS = {"requests", "httpx", "urllib3", "aiohttp", "openai", "anthropic", "ollama",
                     "google", "boto3", "botocore", "paramiko", "websocket", "websockets"}
NETWORK_MODULES = {"socket", "ssl", "http.client", "http.server", "urllib.request", "smtplib", "imaplib",
                   "poplib", "ftplib", "telnetlib", "xmlrpc.client", "xmlrpc.server", "socketserver", "asyncio"}
# Box-specific markers that must not appear in the new test/runner (split so this file does not match itself).
BOX_MARKERS = ("/ho" + "me/", "/Us" + "ers/", "agent" + "-work", "/t" + "mp/", ".her" + "mes", "kan" + "ban")

GUARD = {"active": False, "root": None, "allowed_argv": (), "os_open_depth": 0}
HITS = {"socket": [], "import": [], "write": [], "process": []}
ALLOWED_PROCESS_LOG = []
WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
REAL_POPEN = subprocess.Popen


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    return sha256_bytes(Path(path).read_bytes())


def run_cmd(argv, cwd, *, env=None, timeout=1800):
    """Runner-owned local command with a literal argv list and no shell."""
    with REAL_POPEN(list(argv), cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    env=env, shell=False) as process:
        out, err = process.communicate(timeout=timeout)
    return process.returncode, out, err


def git(*args, cwd=None):
    code, out, err = run_cmd(["git", "-C", str(cwd or REPO), *args], cwd or REPO)
    return code, out.decode("utf-8", "replace").rstrip("\n"), err.decode("utf-8", "replace").strip()


def tree_digest(root):
    """Digest of every path and file byte under root except .git."""
    h = hashlib.sha256()
    count = 0
    for directory, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not (Path(directory) == Path(root) and d == ".git"))
        for name in sorted(files) + [d + "/" for d in dirs]:
            path = Path(directory) / name.rstrip("/")
            relative = path.relative_to(root).as_posix()
            if name.endswith("/") or path.is_symlink():
                h.update(b"L" if path.is_symlink() else b"D")
                h.update(relative.encode() + b"\0")
                if path.is_symlink():
                    h.update(os.readlink(path).encode() + b"\0")
                continue
            h.update(b"F" + relative.encode() + b"\0" + hashlib.sha256(path.read_bytes()).digest())
            count += 1
    return {"sha256": h.hexdigest(), "files": count}


def identity(root=REPO):
    hashes = {rel: (sha256_file(root / rel) if (root / rel).is_file() else None) for rel in HASHED}
    head = git("rev-parse", "HEAD", cwd=root)
    status = git("status", "--porcelain", "--untracked-files=all", cwd=root)
    return {"repo": str(root), "head": head[1] if head[0] == 0 else None,
            "status_porcelain": status[1] if status[0] == 0 else None,
            "clean": status[0] == 0 and status[1] == "", "source_sha256": hashes}


def scope_check(root=REPO):
    problems = []
    ancestor = git("merge-base", "--is-ancestor", BASE_COMMIT, "HEAD", cwd=root)
    if ancestor[0] != 0:
        problems.append("base_not_ancestor_of_head")
    diff = git("diff", "--name-only", BASE_COMMIT, "HEAD", cwd=root)
    status = git("status", "--porcelain", "--untracked-files=all", cwd=root)
    if diff[0] != 0 or status[0] != 0:
        problems.append("git_unavailable")
    changed = set(filter(None, diff[1].splitlines()))
    for line in filter(None, status[1].splitlines()):
        changed.add(line[3:].split(" -> ")[-1])
    outside = sorted(changed - set(ALLOWED_CHANGES))
    if outside:
        problems.append("changes_outside_allowed_write_set")
    code, base_target, _ = run_cmd(["git", "-C", str(root), "show", BASE_COMMIT + ":" + TARGET], root)
    base_target_sha = sha256_bytes(base_target) if code == 0 else None
    if base_target_sha != BASE_TARGET_SHA256:
        problems.append("base_target_pin_mismatch")
    return {"base": BASE_COMMIT, "changed_files": sorted(changed), "outside_allowed": outside,
            "base_target_sha256": base_target_sha, "problems": problems}


def verify_manifest():
    """SOURCE-MANIFEST.json beside the repo must declare this base, remote, repo and runtime."""
    result = {"path": str(MANIFEST), "problems": []}
    problems = result["problems"]
    try:
        raw = MANIFEST.read_bytes()
        manifest = json.loads(raw)
        if not isinstance(manifest, dict):
            raise ValueError("manifest_not_object")
    except (OSError, ValueError) as exc:
        problems.append("source_manifest_unreadable:" + type(exc).__name__)
        return result, None
    result["sha256"] = sha256_bytes(raw)
    result["declared"] = {k: manifest.get(k) for k in ("card", "repo", "remote", "head", "tree", "derived_from")}
    if manifest.get("card") != BRIEF_ID:
        problems.append("manifest_card_mismatch")
    if manifest.get("head") != BASE_COMMIT:
        problems.append("manifest_base_mismatch")
    if not isinstance(manifest.get("repo"), str) or os.path.realpath(manifest["repo"]) != os.path.realpath(REPO):
        problems.append("manifest_repo_mismatch")
    origin = git("config", "--get", "remote.origin.url")
    result["observed_remote"] = origin[1] if origin[0] == 0 else None
    if manifest.get("remote") != result["observed_remote"]:
        problems.append("manifest_remote_mismatch")
    tree = git("rev-parse", BASE_COMMIT + "^{tree}")
    if manifest.get("tree") is not None and (tree[0] != 0 or tree[1] != manifest.get("tree")):
        problems.append("manifest_tree_mismatch")
    runtime = manifest.get("runtime") if isinstance(manifest.get("runtime"), dict) else {}
    result["runtime"] = runtime
    venv = runtime.get("repo_venv")
    observed_venv = os.path.realpath(Path(sys.executable).parent.parent)
    result["observed_interpreter_venv"] = observed_venv
    if not isinstance(venv, str) or os.path.realpath(venv) != observed_venv:
        problems.append("interpreter_not_declared_repo_venv")
    for key in ("credential_scan_python3", "detect_secrets"):
        value = runtime.get(key)
        if not isinstance(value, str) or not os.path.isfile(value) or not os.access(value, os.X_OK):
            problems.append("manifest_runtime_unavailable:" + key)
    return result, manifest


def fresh_run_dir(output_root, mode):
    parent = Path(output_root)
    if not parent.is_absolute():
        raise SystemExit("output_root_must_be_absolute")
    resolved = Path(os.path.realpath(parent))
    if resolved == REPO or REPO in resolved.parents:
        raise SystemExit("output_root_inside_repository")
    resolved.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    run = resolved / ("%s-%s-%d-%s" % (mode, stamp, os.getpid(), secrets.token_hex(4)))
    run.mkdir(mode=0o700, exist_ok=False)
    return run


# sentinels -----------------------------------------------------------------
def _resolve(target, dir_fd=None):
    if isinstance(target, int):
        return None
    text = os.fsdecode(target)
    if text.startswith("file:"):
        text = text[5:].split("?", 1)[0]
        while text.startswith("//"):
            text = text[1:]
    if isinstance(dir_fd, int) and dir_fd >= 0 and not os.path.isabs(text):
        try:
            text = os.path.join(os.readlink("/proc/self/fd/%d" % dir_fd), text)
        except OSError:
            return "<unresolvable-dir-fd>"
    return os.path.realpath(text)


def _outside(target, dir_fd=None):
    resolved = _resolve(target, dir_fd)
    if resolved is None or resolved in (os.devnull, ":memory:", ""):
        return False
    root = GUARD["root"]
    return not (resolved == root or resolved.startswith(root + os.sep))


def _argv_allowed(argv):
    try:
        argv = [os.fsdecode(a) for a in argv]
    except TypeError:
        return False
    return any(argv == list(allowed) for allowed in GUARD["allowed_argv"])


PROCESS_EVENTS = {"os.system", "os.exec", "os.spawn", "os.fork", "os.forkpty", "pty.spawn", "os.startfile"}


def audit(event, args):
    if not GUARD["active"]:
        return
    if event == "open" and GUARD["os_open_depth"]:
        return  # already checked by guarded_os_open, which also sees dir_fd
    if event == "open":
        path, mode, flags = (list(args) + [None, None])[:3]
        writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or \
                  (isinstance(flags, int) and flags & WRITE_FLAGS)
        if writing and _outside(path):
            HITS["write"].append([event, os.fsdecode(path) if not isinstance(path, int) else path])
            raise PermissionError("acceptance sentinel refused write outside run directory")
    elif event in ("os.mkdir", "os.remove", "os.rmdir", "os.truncate", "shutil.rmtree"):
        dir_fd = args[2] if event == "os.mkdir" and len(args) > 2 else (args[1] if len(args) > 1 and event != "os.truncate" else None)
        if _outside(args[0], dir_fd if isinstance(dir_fd, int) else None):
            HITS["write"].append([event, os.fsdecode(args[0]) if not isinstance(args[0], int) else args[0]])
            raise PermissionError("acceptance sentinel refused filesystem change outside run directory")
    elif event in ("os.rename", "os.replace", "os.link", "os.symlink"):
        src, dst = args[0], args[1]
        src_fd = args[2] if len(args) > 2 and isinstance(args[2], int) else None
        dst_fd = args[3] if len(args) > 3 and isinstance(args[3], int) else src_fd
        targets = [(dst, dst_fd)] if event in ("os.link", "os.symlink") else [(src, src_fd), (dst, dst_fd)]
        if any(_outside(t, fd) for t, fd in targets):
            HITS["write"].append([event, os.fsdecode(dst) if not isinstance(dst, int) else dst])
            raise PermissionError("acceptance sentinel refused filesystem change outside run directory")
    elif event == "sqlite3.connect":
        target = args[0] if args else ""
        text = os.fsdecode(target) if not isinstance(target, int) else ""
        # Only declared synthetic databases: in-memory or beneath the run directory.
        if _outside(target):
            HITS["write"].append([event, text])
            raise PermissionError("acceptance sentinel refused sqlite outside run directory")
    elif event in ("subprocess.Popen", "os.posix_spawn"):
        argv = args[1] if len(args) > 1 else None
        if isinstance(argv, (str, bytes)) or argv is None or not _argv_allowed(argv):
            HITS["process"].append([event, repr(argv)[:200]])
            raise PermissionError("acceptance sentinel refused process launch")
        ALLOWED_PROCESS_LOG.append([event, [os.fsdecode(a) for a in argv]])
    elif event in PROCESS_EVENTS:
        HITS["process"].append([event, repr(args)[:200]])
        raise PermissionError("acceptance sentinel refused process launch")


REAL_OS_OPEN = os.open


def guarded_os_open(path, flags, mode=0o777, *, dir_fd=None):
    """os.open audit events omit dir_fd, so dir_fd-relative writes are checked here."""
    if GUARD["active"] and flags & WRITE_FLAGS and _outside(path, dir_fd):
        HITS["write"].append(["os.open", os.fsdecode(path) if not isinstance(path, int) else path])
        raise PermissionError("acceptance sentinel refused write outside run directory")
    GUARD["os_open_depth"] += 1
    try:
        return REAL_OS_OPEN(path, flags, mode, dir_fd=dir_fd)
    finally:
        GUARD["os_open_depth"] -= 1


class ImportSentinel(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if GUARD["active"] and name.split(".")[0] in FORBIDDEN_IMPORTS:
            HITS["import"].append(name)
            raise ImportError("acceptance sentinel refused model/network client import: " + name)
        return None


def install_sentinels(run):
    tmp = run / "tmp"
    tmp.mkdir(mode=0o700)
    tempfile.tempdir = str(tmp)
    os.environ["TMPDIR"] = str(tmp)

    def refuse(name):
        def guard(*args, **kwargs):
            HITS["socket"].append(name)
            raise OSError("acceptance sentinel refused " + name)
        return guard
    socket.socket.connect = refuse("socket.connect")
    socket.socket.connect_ex = refuse("socket.connect_ex")
    socket.socket.sendto = refuse("socket.sendto")
    socket.create_connection = refuse("socket.create_connection")
    socket.getaddrinfo = refuse("socket.getaddrinfo")
    sys.meta_path.insert(0, ImportSentinel())
    os.open = guarded_os_open
    GUARD["root"] = os.path.realpath(run)
    GUARD["allowed_argv"] = ()
    sys.addaudithook(audit)
    GUARD["active"] = True
    # inert self-checks: escape writes, an outside sqlite path and a launch must all be refused
    probes = {}
    outside_dir = os.path.dirname(GUARD["root"])
    parent_fd = REAL_OS_OPEN(outside_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        import sqlite3
        for name, action in (
                ("write_escape", lambda: open(os.path.join(outside_dir, "fc7-escape-probe-" + secrets.token_hex(4)), "w")),
                ("dir_fd_write_escape", lambda: os.open("fc7-escape-probe-" + secrets.token_hex(4),
                                                        os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent_fd)),
                ("sqlite_escape", lambda: sqlite3.connect(os.path.join(outside_dir, "fc7-escape-probe-" + secrets.token_hex(4)))),
                ("process_escape", lambda: REAL_POPEN(["fc7-refused-probe"]))):
            try:
                action()
                probes[name] = "NOT_REFUSED"
            except PermissionError:
                probes[name] = "refused"
            except OSError as exc:
                probes[name] = "error:" + type(exc).__name__
    finally:
        os.close(parent_fd)
    HITS["write"] = [h for h in HITS["write"] if "fc7-escape-probe-" not in str(h[1])]
    HITS["process"] = [h for h in HITS["process"] if "fc7-refused-probe" not in h[1]]
    (tmp / "in-root-probe").write_text("ok\n")
    probes["in_root_write"] = "ok"
    try:
        socket.create_connection(("192.0.2.1", 9))
        probes["socket"] = "NOT_REFUSED"
    except OSError:
        probes["socket"] = "refused"
    HITS["socket"] = [h for h in HITS["socket"] if h != "socket.create_connection"]
    return probes


# static closure ---------------------------------------------------------------
def module_file(name, root=REPO):
    parts = name.split(".")
    base = root.joinpath(*parts)
    if (base / "__init__.py").is_file():
        return base / "__init__.py"
    if base.with_suffix(".py").is_file():
        return base.with_suffix(".py")
    return None


def inspect_closure(roots, root=REPO):
    pending = [root / r for r in roots]
    seen, external, launches = {}, set(), []
    while pending:
        path = pending.pop()
        relative = path.relative_to(root).as_posix()
        if relative in seen:
            continue
        seen[relative] = sha256_file(path)
        package = path.relative_to(root).with_suffix("").parts[:-1]
        for index in range(1, len(package) + 1):  # package __init__ files run on import
            init = root.joinpath(*package[:index], "__init__.py")
            if init.is_file():
                pending.append(init)
        for node in ast.walk(ast.parse(path.read_bytes(), str(path))):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    anchor = list(path.relative_to(root).with_suffix("").parts[:-1])
                    anchor = anchor[:len(anchor) - (node.level - 1)] if node.level > 1 else anchor
                    stem = ".".join(anchor + ([node.module] if node.module else []))
                else:
                    stem = node.module or ""
                names = [stem] + [stem + "." + alias.name for alias in node.names]
            elif isinstance(node, ast.Attribute) and node.attr in ("Popen", "run", "check_output", "call",
                                                                    "system", "execv", "spawnv"):
                if isinstance(node.value, ast.Name) and node.value.id in ("subprocess", "os"):
                    launches.append([relative, node.value.id + "." + node.attr, getattr(node, "lineno", None)])
            for name in names:
                target = module_file(name, root)
                if target is not None:
                    pending.append(target)
                elif name and module_file(name.split(".")[0], root) is None and not (root / name.split(".")[0]).is_dir():
                    external.add(name)
    forbidden = sorted(n for n in external if n.split(".")[0] in FORBIDDEN_IMPORTS
                       or any(n == m or n.startswith(m + ".") for m in NETWORK_MODULES))
    nonstdlib = sorted({n.split(".")[0] for n in external} - set(sys.stdlib_module_names))
    return {"files": seen, "external_top_level": sorted(external), "forbidden": forbidden,
            "non_stdlib": nonstdlib, "process_launch_sites": launches}


# unittest helpers -------------------------------------------------------------
def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


def case_id(test):
    return getattr(test, "test_case", test).id()


def collect(entries, limit):
    grouped = {}
    for test, text in entries:
        grouped.setdefault(case_id(test), []).append(text[-limit:])
    return grouped


def run_tests(names, label, run, record):
    suite = unittest.TestLoader().loadTestsFromNames(list(names))
    ids = sorted(t.id() for t in flatten(suite))
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    raw = stream.getvalue().encode("utf-8")
    raw_path = run / ("%s.unittest.txt" % label)
    raw_path.write_bytes(raw)
    summary = {"selectors": list(names), "ids": ids, "tests_run": result.testsRun,
               "failures": sorted({case_id(t) for t, _ in result.failures}),
               "failure_text": collect(result.failures, 600),
               "errors": sorted({case_id(t) for t, _ in result.errors}),
               "error_text": collect(result.errors, 1200),
               "skipped": [case_id(t) for t, _ in result.skipped],
               "expected_failures": len(result.expectedFailures),
               "unexpected_successes": len(result.unexpectedSuccesses),
               "successful": result.wasSuccessful(), "raw_output": raw_path.name,
               "raw_output_sha256": sha256_bytes(raw)}
    record["phases"][label] = summary
    return summary


def clean_pass(summary, expected_ids=None):
    ok = (summary["successful"] and summary["tests_run"] > 0 and not summary["skipped"]
          and not summary["expected_failures"] and not summary["unexpected_successes"])
    if expected_ids is not None:
        ok = ok and summary["ids"] == sorted(expected_ids)
    return ok


def loaded_modules():
    found = {}
    for name, module in sorted(sys.modules.items()):
        if name.split(".")[0] in ("campaign_tool", "tests", "tools"):
            path = getattr(module, "__file__", None)
            if path:
                found[name] = os.path.realpath(path)
    return found


def focused_ids(cases=FROZEN_CASES):
    return [FOCUSED_CLASS + ".test_" + case for case in cases]


# modes -------------------------------------------------------------------------
def guarded_start(run, mode, record):
    record["closure"] = inspect_closure(CLOSURE_ROOTS[mode])
    if record["closure"]["forbidden"]:
        record["problems"].append("closure_imports_network_or_model_client")
    if record["closure"]["process_launch_sites"]:
        record["problems"].append("closure_contains_process_launch_site")
    record["sentinel_probes"] = install_sentinels(run)
    if any(v != ("ok" if k == "in_root_write" else "refused") for k, v in record["sentinel_probes"].items()):
        record["problems"].append("sentinel_self_check_failed")
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))


def guarded_stop(record):
    GUARD["active"] = False
    record["loaded_modules"] = loaded_modules()
    outside = [n for n, p in record["loaded_modules"].items() if not p.startswith(str(REPO) + os.sep)]
    if outside:
        record["problems"].append("module_loaded_outside_repo")
    record["sentinel_hits"] = HITS
    record["allowed_processes"] = ALLOWED_PROCESS_LOG
    if any(HITS.values()):
        record["problems"].append("sentinel_hits")


def mode_focused(run, record):
    guarded_start(run, "focused", record)
    summary = run_tests([FOCUSED], "focused", run, record)
    from tests.records import test_fc7_catalog_join_json as module
    record["case_map"] = getattr(module, "CASE_MAP", None)
    guarded_stop(record)
    if record["case_map"] != {case: "test_" + case for case in FROZEN_CASES}:
        record["problems"].append("case_map_mismatch")
    if not clean_pass(summary, focused_ids()):
        record["problems"].append("focused_not_clean_pass")


def mode_compatibility(run, record):
    clone = run / "baseline" / "repo"
    clone.parent.mkdir()
    steps = [("clone", ["git", "clone", "--quiet", "--no-hardlinks", str(REPO), str(clone)], run),
             ("checkout", ["git", "-C", str(clone), "checkout", "--quiet", "--detach", BASE_COMMIT], run)]
    for name, argv, cwd in steps:
        code, out, err = run_cmd(argv, cwd)
        record["phases"]["baseline_" + name] = {"argv": argv, "exit": code, "stderr": err.decode("utf-8", "replace")[-400:]}
        if code != 0:
            record["problems"].append("baseline_" + name + "_failed")
            return
    baseline_tmp = run / "baseline" / "tmp"
    baseline_tmp.mkdir(mode=0o700)
    env = dict(os.environ, TMPDIR=str(baseline_tmp), PYTHONDONTWRITEBYTECODE="1")
    argv = [sys.executable, "-B", "-m", "unittest", "-v", *COMPATIBILITY]
    code, out, err = run_cmd(argv, clone, env=env)
    raw = out + err
    (run / "baseline.unittest.txt").write_bytes(raw)
    text = raw.decode("utf-8", "replace")
    ran = [line for line in text.splitlines() if line.startswith("Ran ")]
    baseline_ids = sorted({match.group(3) + "." + match.group(1) for match in
                           re.finditer(r"(?m)^(test\w*) \(((tests\.[\w.]+)\.\1)\)", text)}) or \
        sorted({match.group(2) + "." + match.group(1) for match in
                re.finditer(r"(?m)^(test\w*) \((tests\.[\w.]+)\)", text)})
    record["phases"]["baseline"] = {"argv": argv, "cwd": str(clone), "exit": code, "ran_line": ran[-1] if ran else None,
                                    "ok": "\nOK" in text and code == 0, "skipped_mentioned": "skipped" in text,
                                    "ids": baseline_ids, "raw_output": "baseline.unittest.txt",
                                    "raw_output_sha256": sha256_bytes(raw),
                                    "head": git("rev-parse", "HEAD", cwd=clone)[1],
                                    "target_sha256": sha256_file(clone / TARGET),
                                    "sentinels": "none (existing suite entry point in a child process)"}
    guarded_start(run, "compatibility", record)
    summary = run_tests(COMPATIBILITY, "candidate", run, record)
    guarded_stop(record)
    base = record["phases"]["baseline"]
    if base["head"] != BASE_COMMIT or base["target_sha256"] != BASE_TARGET_SHA256:
        record["problems"].append("baseline_not_at_pinned_base")
    if not base["ok"] or base["skipped_mentioned"]:
        record["problems"].append("baseline_not_clean_pass")
    if not clean_pass(summary):
        record["problems"].append("candidate_not_clean_pass")
    for selector in COMPATIBILITY:
        if not any(i.startswith(selector + ".") for i in summary["ids"]):
            record["problems"].append("selector_selected_no_tests:" + selector)
    ran_count = re.match(r"Ran (\d+) tests? ", base["ran_line"] or "")
    if base["ids"] != summary["ids"] or not ran_count or int(ran_count.group(1)) != summary["tests_run"] \
            or len(summary["ids"]) != summary["tests_run"]:
        record["problems"].append("baseline_candidate_test_set_differs")


def mode_negative(run, record):
    guarded_start(run, "negative", record)
    from campaign_tool.records import ledger_catalog as production
    if not callable(getattr(production, "_strict_loads", None)):
        record["problems"].append("strict_decoder_seam_missing")
        guarded_stop(record)
        return

    def probe(text):
        try:
            return {"result": repr(production._json(text, "probe_reason"))}
        except production.CatalogError as exc:
            return {"catalog_error": list(exc.args)}

    probes = record["seam_probes"] = {}
    # Replace ONLY the strict decoding with ordinary json.loads, in memory.
    with mock.patch.object(production, "_strict_loads", json.loads):
        probes["permissive_last_wins"] = probe(LAST_WINS_PROBE)
        probes["permissive_nonfinite"] = probe(NONFINITE_PROBE)
        deciding = run_tests(focused_ids(NEGATIVE_DECIDING), "permissive_deciding", run, record)
        control = run_tests(focused_ids(NEGATIVE_CONTROL), "permissive_control", run, record)
    probes["restored_last_wins"] = probe(LAST_WINS_PROBE)
    probes["restored_nonfinite"] = probe(NONFINITE_PROBE)
    probes["restored_seam_is_original"] = production._strict_loads is not json.loads
    restored = run_tests(focused_ids(), "restored", run, record)
    guarded_stop(record)

    expected = sorted(focused_ids(NEGATIVE_DECIDING))
    detected = (deciding["tests_run"] == len(NEGATIVE_DECIDING) and deciding["failures"] == expected
                and not deciding["errors"] and not deciding["skipped"]
                and all(any("CatalogError not raised" in text for text in deciding["failure_text"].get(i, []))
                        for i in expected))
    if not detected:
        record["problems"].append("permissive_decoder_not_detected")
    if not clean_pass(control, focused_ids(NEGATIVE_CONTROL)):
        record["problems"].append("controls_failed_with_permissive_decoder")
    if not clean_pass(restored, focused_ids()):
        record["problems"].append("restored_positives_failed")
    if probes["permissive_last_wins"] != {"result": repr({"source_sha256": "s", "locator": "page:1"})}:
        record["problems"].append("last_wins_seam_not_on_production_path")
    if probes["permissive_nonfinite"] != {"result": repr({"locator": "page:1", "score": float("inf"), "other": float("nan")})}:
        record["problems"].append("nonfinite_seam_not_on_production_path")
    if probes["restored_last_wins"] != {"catalog_error": ["probe_reason"]} or \
            probes["restored_nonfinite"] != {"catalog_error": ["probe_reason"]} or \
            not probes["restored_seam_is_original"]:
        record["problems"].append("restored_probe_not_rejected")


def mode_portability(run, record):
    if not record["identity"]["clean"]:
        record["problems"].append("portability_requires_clean_committed_tree")
        return
    markers = {}
    for rel in ("tests/records/test_fc7_catalog_join_json.py", "tools/accept_fc7_catalog_join_json.py"):
        text = (REPO / rel).read_text(encoding="utf-8")
        markers[rel] = [m for m in BOX_MARKERS if m in text]
    record["box_path_markers"] = markers
    if any(markers.values()):
        record["problems"].append("box_specific_paths_in_new_files")
    prefix = run / "second-prefix" / ("copy-" + secrets.token_hex(6))
    second = prefix / "repo"
    prefix.mkdir(parents=True)
    code, out, err = run_cmd(["git", "clone", "--quiet", "--no-hardlinks", str(REPO), str(second)], run)
    record["phases"]["clone"] = {"exit": code, "path": str(second), "stderr": err.decode("utf-8", "replace")[-400:]}
    if code != 0:
        record["problems"].append("second_copy_clone_failed")
        return
    git("checkout", "--quiet", "--detach", record["identity"]["head"], cwd=second)
    clone_head = git("rev-parse", "HEAD", cwd=second)[1]
    # The relocated copy gets a derived manifest: same declarations, its own repo path, and
    # this candidate as its remote (that is what the local clone's origin really is).
    original = json.loads(MANIFEST.read_bytes())
    derived = dict(original, repo=str(second), remote=str(REPO),
                   derived_from={"path": str(MANIFEST), "sha256": sha256_file(MANIFEST),
                                 "by": "accept_fc7_catalog_join_json portability"})
    (prefix / "SOURCE-MANIFEST.json").write_text(json.dumps(derived, indent=2, sort_keys=True) + "\n")
    child_out = run / "relocated-out"
    argv = [sys.executable, str(second / "tools" / "accept_fc7_catalog_join_json.py"),
            "--check", "focused", "--output-root", str(child_out)]
    code, out, err = run_cmd(argv, second)
    (run / "relocated.stdout.txt").write_bytes(out)
    (run / "relocated.stderr.txt").write_bytes(err)
    children = sorted(p for p in child_out.iterdir() if p.is_dir()) if child_out.is_dir() else []
    child = json.loads((children[0] / "evidence.json").read_text()) if len(children) == 1 else None
    record["phases"]["relocated_focused"] = {
        "argv": argv, "cwd": str(second), "exit": code, "clone_head": clone_head,
        "stdout_sha256": sha256_bytes(out), "stderr_sha256": sha256_bytes(err),
        "child_run_dir": str(children[0]) if len(children) == 1 else None,
        "child_verdict": child and child.get("verdict"), "child_repo": child and child["identity"]["repo"],
        "child_head": child and child["identity"]["head"],
        "child_tests_run": child and child["phases"]["focused"]["tests_run"],
        "child_manifest": child and child.get("manifest"),
        "child_loaded_modules": child and child.get("loaded_modules")}
    rel = record["phases"]["relocated_focused"]
    second_real = os.path.realpath(second)
    if code != 0 or not child or child.get("verdict") != "REVIEW_REQUIRED":
        record["problems"].append("relocated_focused_failed")
        return
    if rel["child_repo"] != second_real or clone_head != record["identity"]["head"] or rel["child_head"] != clone_head:
        record["problems"].append("relocated_identity_mismatch")
    if rel["child_tests_run"] != len(FROZEN_CASES):
        record["problems"].append("relocated_test_count_mismatch")
    loaded = rel["child_loaded_modules"] or {}
    if "campaign_tool.records.ledger_catalog" not in loaded or FOCUSED not in loaded or \
            any(not p.startswith(second_real + os.sep) for p in loaded.values()):
        record["problems"].append("relocated_modules_not_from_second_copy")
    if child["identity"]["source_sha256"] != record["identity"]["source_sha256"]:
        record["problems"].append("relocated_source_bytes_differ")


def mode_credentials(run, record, manifest):
    if not record["identity"]["clean"]:
        record["problems"].append("credential_scan_requires_clean_exact_head")
    runtime = (manifest or {}).get("runtime") or {}
    declared_python = runtime.get("credential_scan_python3")
    record["phases"]["interpreter"] = {"sys_executable": sys.executable, "declared": declared_python}
    if sys.executable != declared_python:
        record["problems"].append("interpreter_not_declared_credential_scan_python3")
        return
    scanner = runtime.get("detect_secrets")
    checker = REPO / "tools" / "check_secret_report.py"
    if not isinstance(scanner, str) or not os.path.isfile(scanner) or not os.access(scanner, os.X_OK):
        record["problems"].append("detect_secrets_unavailable")
        return
    scan_argv = [scanner, "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"]
    code, out, err = run_cmd(scan_argv, REPO)
    report = run / "secret-scan.json"
    report.write_bytes(out)
    (run / "secret-scan.stderr.txt").write_bytes(err)
    version = run_cmd([scanner, "--version"], REPO)
    record["phases"]["scan"] = {"argv": scan_argv, "cwd": str(REPO), "exit": code,
                                "report": report.name, "report_sha256": sha256_bytes(out),
                                "scanner": scanner, "scanner_version": version[1].decode().strip()}
    try:
        parsed = json.loads(out)
        results = parsed["results"]
        if not isinstance(results, dict):
            raise ValueError("results not an object")
        record["phases"]["scan"]["raw_hit_files"] = sorted(results)
        record["phases"]["scan"]["raw_hits"] = sum(len(v) for v in results.values())
        new_file_hits = sorted(set(results) & set(ALLOWED_CHANGES))
        record["phases"]["scan"]["hits_in_changed_files"] = new_file_hits
        if new_file_hits:
            record["problems"].append("scanner_hits_in_changed_files")
    except (ValueError, KeyError, TypeError) as exc:
        record["problems"].append("secret_report_unreadable:" + type(exc).__name__)
        return
    check_argv = [sys.executable, str(checker), str(report)]
    code2, out2, err2 = run_cmd(check_argv, REPO)
    (run / "check-secret-report.stdout.txt").write_bytes(out2)
    (run / "check-secret-report.stderr.txt").write_bytes(err2)
    record["phases"]["checker"] = {"argv": check_argv, "cwd": str(REPO), "exit": code2,
                                   "stdout": out2.decode("utf-8", "replace")[-2000:],
                                   "stderr": err2.decode("utf-8", "replace")[-2000:],
                                   "stdout_sha256": sha256_bytes(out2)}
    if code != 0:
        record["problems"].append("detect_secrets_failed")
    if code2 != 0:
        record["problems"].append("unapproved_secret_hits_or_checker_failed")


def mode_public_tree(run, record):
    if not record["identity"]["clean"]:
        record["problems"].append("public_tree_requires_clean_exact_head")
    argv = [sys.executable, str(REPO / "tools" / "check_public_tree.py")]
    code, out, err = run_cmd(argv, REPO)
    (run / "public-tree.stdout.txt").write_bytes(out)
    (run / "public-tree.stderr.txt").write_bytes(err)
    record["phases"]["public_tree"] = {"argv": argv, "cwd": str(REPO), "exit": code,
                                       "stdout": out.decode("utf-8", "replace")[-2000:],
                                       "stderr": err.decode("utf-8", "replace")[-2000:],
                                       "stdout_sha256": sha256_bytes(out), "stderr_sha256": sha256_bytes(err)}
    if code != 0:
        record["problems"].append("public_tree_check_failed")


def main(argv=None):
    parser = argparse.ArgumentParser(description="FC7-CATALOG-JOIN-JSON scoped acceptance (offline)")
    parser.add_argument("--check", required=True, choices=MODES)
    parser.add_argument("--output-root", required=True)
    args = parser.parse_args(argv)
    run = fresh_run_dir(args.output_root, args.check)
    record = {"schema": SCHEMA, "brief_id": BRIEF_ID, "brief_file": BRIEF_FILE, "brief_sha256": BRIEF_SHA256,
              "acceptance_contract": {"modes": list(MODES), "frozen_cases": list(FROZEN_CASES),
                                      "compatibility_selectors": list(COMPATIBILITY),
                                      "allowed_changes": list(ALLOWED_CHANGES),
                                      "runner_sha256": sha256_file(Path(__file__).resolve())},
              "mode": args.check, "argv": [sys.executable, *sys.argv], "cwd": os.getcwd(),
              "interpreter": sys.executable, "python": platform.python_version(),
              "started": now(), "run_dir": str(run), "phases": {}, "problems": [],
              "limits": ["REVIEW REQUIRED: not full-repository CI; full discovery is operator-run.",
                         "Sentinels are not a hostile-code sandbox; child processes do not inherit them.",
                         "Synthetic fixtures only; no real records, network, model calls or live actions."]}
    record["manifest"], manifest = verify_manifest()
    record["problems"].extend(record["manifest"]["problems"])
    record["identity"] = identity()
    record["scope"] = scope_check()
    record["problems"].extend(record["scope"]["problems"])
    before = tree_digest(REPO)
    try:
        if record["manifest"]["problems"]:
            record["problems"].append("prerequisites_unverified_mode_not_run")
        elif args.check == "credentials":
            mode_credentials(run, record, manifest)
        else:
            {"focused": mode_focused, "compatibility": mode_compatibility, "negative": mode_negative,
             "portability": mode_portability, "public-tree": mode_public_tree}[args.check](run, record)
    except Exception as exc:  # recorded and failed closed, never treated as a pass
        GUARD["active"] = False
        record["problems"].append("runner_exception:" + type(exc).__name__ + ":" + str(exc)[:300])
    GUARD["active"] = False
    after = tree_digest(REPO)
    record["repo_tree_before"], record["repo_tree_after"] = before, after
    if before != after:
        record["problems"].append("repository_tree_changed_during_run")
    final_identity = identity()
    if final_identity != record["identity"]:
        record["problems"].append("identity_changed_during_run")
    record["finished"] = now()
    record["verdict"] = "REVIEW_REQUIRED" if not record["problems"] else "FAIL"
    record["exit"] = 0 if not record["problems"] else 1
    payload = json.dumps(record, indent=2, sort_keys=True, default=str) + "\n"
    (run / "evidence.json").write_text(payload)
    print(json.dumps({"mode": args.check, "verdict": record["verdict"], "problems": record["problems"],
                      "head": record["identity"]["head"], "clean": record["identity"]["clean"],
                      "run_dir": str(run), "evidence_sha256": sha256_bytes(payload.encode())}, indent=2))
    for label, phase in record["phases"].items():
        if isinstance(phase, dict) and "tests_run" in phase:
            print("%s: ran=%d failures=%d errors=%d skipped=%d successful=%s" % (
                label, phase["tests_run"], len(phase["failures"]), len(phase["errors"]),
                len(phase["skipped"]), phase["successful"]))
    return record["exit"]


if __name__ == "__main__":
    raise SystemExit(main())
