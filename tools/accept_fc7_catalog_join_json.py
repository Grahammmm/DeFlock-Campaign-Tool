"""Scoped offline acceptance runner for FC7-CATALOG-JOIN-JSON.

Exactly one --check mode per invocation; evidence goes to a fresh, exclusive run
directory below --output-root, which must be absolute and outside this repo.

  focused        tests/records/test_fc7_catalog_join_json.py (the six frozen cases)
  compatibility  unchanged tests.records.test_ledger_catalog and
                 tests.records.test_correspondence, run twice in this process under
                 the same sentinels: first from a local clone checked out at the
                 pinned base, then (after purging every application module) from
                 this tree. Both static closures, including named "-m" child entry
                 points, are inspected before either run. Identical test IDs and
                 counts are required.
  negative       the strict catalog decoder seam (_strict_loads) is replaced in
                 memory by ordinary json.loads; the deciding last-wins/nonfinite
                 rejection cases must FAIL with "CatalogError not raised", the
                 shape/positive controls must still pass, and once restored every
                 focused case must pass again
  portability    the focused check re-run from a local clone of the exact committed
                 HEAD at a different absolute prefix; loaded module paths recorded
  credentials    the installed detect-secrets declared in SOURCE-MANIFEST.json,
                 then the unchanged tools/check_secret_report.py on that report
  public-tree    the unchanged tools/check_public_tree.py, cwd this repo; its exit
                 code decides the mode, with no exclusion or ignore of any kind. A
                 second, NON-GATING diagnostic runs the same checker inside a clean
                 clone of the exact HEAD so a reviewer can see what the tracked
                 tree alone yields; it can never turn a failure into a pass.

Before any mode, SOURCE-MANIFEST.json beside the repo is read and its base,
remote, repo and runtime declarations are verified.

In-process test phases install, before any application import: bytecode
suppression, tempfile/TMPDIR/HOME/XDG redirection into the phase directory,
removal of model endpoint variables, socket refusal, model-client import
refusal and an audit hook that refuses writes and sqlite connections outside the
phase directory and every process launch. Each phase re-qualifies the sentinels
with inert refused probes and allowed synthetic controls. These are sentinels,
not a hostile-code sandbox. Test code never runs in a child process; the only
child processes are runner-owned literal argv commands (git, the scanner, the
unchanged checkers and, for portability, this runner itself, which installs its
own sentinels), each receipted with argv, cwd, exit and both full raw streams.

A pass prints REVIEW_REQUIRED. It is not full-repository CI, nor semantic,
legal, stage or release acceptance. Full discovery is operator-run.
"""
import argparse
import ast
import datetime
import hashlib
import importlib
import importlib.abc
import io
import json
import os
from pathlib import Path
import platform
import secrets
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import warnings

sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

REPO = Path(__file__).resolve().parents[1]
MANIFEST = REPO.parent / "SOURCE-MANIFEST.json"
SCHEMA = "fc7-catalog-join-json-acceptance/v2"
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
APP_PACKAGES = ("campaign_tool", "tests")
FORBIDDEN_IMPORTS = {"requests", "httpx", "urllib3", "aiohttp", "openai", "anthropic", "ollama",
                     "google", "boto3", "botocore", "paramiko", "websocket", "websockets"}
NETWORK_MODULES = {"socket", "ssl", "http.client", "http.server", "urllib.request", "smtplib", "imaplib",
                   "poplib", "ftplib", "telnetlib", "xmlrpc.client", "xmlrpc.server", "socketserver", "asyncio"}
LAUNCH_ATTRS = {"subprocess": {"Popen", "run", "check_output", "check_call", "call", "getoutput", "getstatusoutput"},
                "os": {"system", "popen", "execv", "execve", "execvp", "execvpe", "execl", "execlp", "spawnv",
                       "spawnve", "spawnvp", "posix_spawn", "posix_spawnp", "fork", "forkpty"}}
# Endpoint variables are removed (names recorded, values never) before test phases.
MODEL_ENV_PREFIXES = ("MODEL_", "CHALLENGE_MODEL_", "OPENAI", "ANTHROPIC", "OLLAMA", "GEMINI", "GOOGLE_API",
                      "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")
# Box-specific markers that must not appear in the new test/runner (split so this file does not match itself).
BOX_MARKERS = ("/ho" + "me/", "/Us" + "ers/", "agent" + "-work", "/t" + "mp/", ".her" + "mes", "kan" + "ban")

GUARD = {"active": False, "installed": False, "root": None, "allowed_argv": (), "os_open_depth": 0}
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


def run_phase(run, record, label, argv, cwd, *, env=None, stdout_name=None, required=True):
    """Run one runner-owned child command and keep a complete receipt.

    Literal argv, cwd, checked exit, both full raw streams (saved as files) and
    their hashes are recorded; env overrides are recorded by name and value.
    A required phase that exits nonzero adds a problem.
    """
    receipts = run / "receipts"
    receipts.mkdir(exist_ok=True)
    started = now()
    code, out, err = run_cmd(argv, cwd, env=env)
    out_path = run / stdout_name if stdout_name else receipts / (label + ".stdout")
    err_path = receipts / (label + ".stderr")
    out_path.write_bytes(out)
    err_path.write_bytes(err)
    overrides = {k: v for k, v in (env or {}).items() if os.environ.get(k) != v}
    record["phases"][label] = {
        "kind": "child command (runner-owned, shell-free literal argv)",
        "argv": [str(a) for a in argv], "cwd": str(cwd), "exit": code, "required": required,
        "started": started, "finished": now(), "env_overrides": overrides,
        "stdout_file": str(out_path.relative_to(run)), "stdout_sha256": sha256_bytes(out), "stdout_bytes": len(out),
        "stderr_file": str(err_path.relative_to(run)), "stderr_sha256": sha256_bytes(err), "stderr_bytes": len(err)}
    if required and code != 0:
        record["problems"].append(label + "_failed")
    return code, out, err


def git(*args, cwd=None):
    """Read-only git introspection used for identity and scope (not an acceptance phase)."""
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


def staged_venv_link(root=REPO):
    """Standard staging adds an untracked .venv symlink to the interpreter's venv; nothing else."""
    link = root / ".venv"
    return link.is_symlink() and os.path.realpath(link) == os.path.realpath(Path(sys.executable).parent.parent)


def porcelain(root=REPO):
    """git status lines, excluding only the verified operator-staged .venv link.

    This filter only decides whether the committed tree is clean; it is never
    applied to the public-tree checker, which sees the tree exactly as staged.
    """
    code, text, _ = git("status", "--porcelain", "--untracked-files=all", cwd=root)
    lines = [line for line in text.splitlines() if line]
    if staged_venv_link(root):
        lines = [line for line in lines if line != "?? .venv"]
    return code, lines


def identity(root=REPO):
    hashes = {rel: (sha256_file(root / rel) if (root / rel).is_file() else None) for rel in HASHED}
    head = git("rev-parse", "HEAD", cwd=root)
    code, lines = porcelain(root)
    return {"repo": str(root), "head": head[1] if head[0] == 0 else None,
            "status_porcelain": lines if code == 0 else None, "staged_venv_link": staged_venv_link(root),
            "clean": code == 0 and not lines, "source_sha256": hashes}


def scope_check(root=REPO):
    problems = []
    ancestor = git("merge-base", "--is-ancestor", BASE_COMMIT, "HEAD", cwd=root)
    if ancestor[0] != 0:
        problems.append("base_not_ancestor_of_head")
    diff = git("diff", "--name-only", BASE_COMMIT, "HEAD", cwd=root)
    status = porcelain(root)
    if diff[0] != 0 or status[0] != 0:
        problems.append("git_unavailable")
    changed = set(filter(None, diff[1].splitlines()))
    for line in status[1]:
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
    if not isinstance(target, int) and os.fsdecode(target) in (":memory:", ""):
        return False  # sqlite in-memory / private temporary database
    resolved = _resolve(target, dir_fd)
    if resolved is None or resolved == os.devnull:
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
            raise PermissionError("acceptance sentinel refused write outside phase directory")
    elif event in ("os.mkdir", "os.remove", "os.rmdir", "os.truncate", "shutil.rmtree"):
        dir_fd = args[2] if event == "os.mkdir" and len(args) > 2 else (args[1] if len(args) > 1 and event != "os.truncate" else None)
        if _outside(args[0], dir_fd if isinstance(dir_fd, int) else None):
            HITS["write"].append([event, os.fsdecode(args[0]) if not isinstance(args[0], int) else args[0]])
            raise PermissionError("acceptance sentinel refused filesystem change outside phase directory")
    elif event in ("os.rename", "os.replace", "os.link", "os.symlink"):
        src, dst = args[0], args[1]
        src_fd = args[2] if len(args) > 2 and isinstance(args[2], int) else None
        dst_fd = args[3] if len(args) > 3 and isinstance(args[3], int) else src_fd
        targets = [(dst, dst_fd)] if event in ("os.link", "os.symlink") else [(src, src_fd), (dst, dst_fd)]
        if any(_outside(t, fd) for t, fd in targets):
            HITS["write"].append([event, os.fsdecode(dst) if not isinstance(dst, int) else dst])
            raise PermissionError("acceptance sentinel refused filesystem change outside phase directory")
    elif event == "sqlite3.connect":
        target = args[0] if args else ""
        text = os.fsdecode(target) if not isinstance(target, int) else ""
        # Only declared synthetic databases: in-memory or beneath the phase directory.
        if _outside(target):
            HITS["write"].append([event, text])
            raise PermissionError("acceptance sentinel refused sqlite outside phase directory")
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
        raise PermissionError("acceptance sentinel refused write outside phase directory")
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


def _refuse(name):
    def guard(*args, **kwargs):
        HITS["socket"].append(name)
        raise OSError("acceptance sentinel refused " + name)
    return guard


def install_sentinels():
    """Install the process-wide hooks once, before any application import."""
    if GUARD["installed"]:
        return
    socket.socket.connect = _refuse("socket.connect")
    socket.socket.connect_ex = _refuse("socket.connect_ex")
    socket.socket.sendto = _refuse("socket.sendto")
    socket.create_connection = _refuse("socket.create_connection")
    socket.getaddrinfo = _refuse("socket.getaddrinfo")
    sys.meta_path.insert(0, ImportSentinel())
    os.open = guarded_os_open
    GUARD["allowed_argv"] = ()
    sys.addaudithook(audit)
    GUARD["installed"] = True


def enter_phase(phase_dir, record_phase):
    """Point every redirect and the sentinel root at phase_dir, then qualify the guards.

    Must run before the phase's application modules are imported, so import-time
    constants and bound defaults (tempfile.gettempdir(), HOME paths) bind here.
    """
    phase_dir = Path(phase_dir)
    phase_dir.mkdir(mode=0o700, exist_ok=True)
    tmp, home = phase_dir / "tmp", phase_dir / "home"
    tmp.mkdir(mode=0o700)
    home.mkdir(mode=0o700)
    tempfile.tempdir = str(tmp)
    redirects = {"TMPDIR": str(tmp), "TEMP": str(tmp), "TMP": str(tmp), "HOME": str(home),
                 "XDG_CACHE_HOME": str(home / ".cache"), "XDG_CONFIG_HOME": str(home / ".config"),
                 "XDG_DATA_HOME": str(home / ".local" / "share"), "XDG_STATE_HOME": str(home / ".local" / "state")}
    os.environ.update(redirects)
    removed = sorted(k for k in os.environ if k.startswith(MODEL_ENV_PREFIXES))
    for key in removed:
        del os.environ[key]
    install_sentinels()
    GUARD["root"] = os.path.realpath(phase_dir)
    GUARD["active"] = True
    record_phase["redirects"] = redirects
    record_phase["model_env_removed"] = removed
    record_phase["sentinel_root"] = GUARD["root"]
    record_phase["sentinel_probes"] = qualify(tmp)
    return record_phase["sentinel_probes"]


def qualify(tmp):
    """Inert refused probes and allowed synthetic controls under the active guards.

    Hits raised by the probes are recorded separately and removed from HITS, so
    HITS afterwards reflects only the code under test.
    """
    import sqlite3
    before = {k: len(v) for k, v in HITS.items()}
    probes = {}
    outside_dir = os.path.dirname(GUARD["root"])
    tag = "fc7-escape-probe-" + secrets.token_hex(4)
    parent_fd = REAL_OS_OPEN(outside_dir, os.O_RDONLY | os.O_DIRECTORY)

    def model_import():
        importlib.import_module("openai")

    refused = (
        ("write_escape", lambda: open(os.path.join(outside_dir, tag), "w")),
        ("dir_fd_write_escape", lambda: os.open(tag, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent_fd)),
        ("sqlite_escape", lambda: sqlite3.connect(os.path.join(outside_dir, tag + ".db"))),
        ("process_launch", lambda: subprocess.run(["fc7-refused-probe"])),
        ("os_system", lambda: os.system("fc7-refused-probe")),
        ("socket_connect", lambda: socket.create_connection(("192.0.2.1", 9), timeout=1)),
        ("model_client_import", model_import))
    try:
        for name, action in refused:
            try:
                action()
                probes[name] = "NOT_REFUSED"
            except PermissionError:
                probes[name] = "refused"
            except ImportError:
                probes[name] = "refused" if len(HITS["import"]) > before["import"] else "error:ImportError"
            except OSError as exc:
                probes[name] = "refused" if name == "socket_connect" and len(HITS["socket"]) > before["socket"] \
                    else "error:" + type(exc).__name__
    finally:
        os.close(parent_fd)
    expected_hits = {"write": 3, "process": 2, "socket": 1, "import": 1}
    probe_hits = {k: HITS[k][before[k]:] for k in HITS}
    for key in HITS:
        del HITS[key][before[key]:]
    # allowed synthetic controls
    (tmp / "in-root-probe").write_text("ok\n")
    probes["in_root_write"] = "ok"
    connection = sqlite3.connect(str(tmp / "control.sqlite3"))
    connection.execute("CREATE TABLE control (x INTEGER)")
    connection.close()
    probes["in_root_sqlite"] = "ok"
    memory = sqlite3.connect(":memory:")
    memory.execute("SELECT 1")
    memory.close()
    probes["memory_sqlite"] = "ok"
    probes["probe_hit_counts"] = {k: len(v) for k, v in probe_hits.items()}
    probes["probe_hit_counts_expected"] = expected_hits
    return probes


def probes_ok(probes):
    refused_ok = all(v == "refused" for k, v in probes.items()
                     if k not in ("in_root_write", "in_root_sqlite", "memory_sqlite",
                                  "probe_hit_counts", "probe_hit_counts_expected"))
    allowed_ok = all(probes.get(k) == "ok" for k in ("in_root_write", "in_root_sqlite", "memory_sqlite"))
    return refused_ok and allowed_ok and probes.get("probe_hit_counts") == probes.get("probe_hit_counts_expected")


def leave_phase():
    GUARD["active"] = False


# static closure ---------------------------------------------------------------
def module_file(name, root=REPO):
    parts = name.split(".")
    base = root.joinpath(*parts)
    if (base / "__init__.py").is_file():
        return base / "__init__.py"
    if base.with_suffix(".py").is_file():
        return base.with_suffix(".py")
    return None


def entry_point_file(name, root=REPO):
    """File executed by "python -m name": a module file or a package's __main__.py."""
    parts = name.split(".")
    base = root.joinpath(*parts)
    if (base / "__init__.py").is_file():
        main = base / "__main__.py"
        return main if main.is_file() else None
    return base.with_suffix(".py") if base.with_suffix(".py").is_file() else None


def _parse(path):
    """Parse without executing; existing SyntaxWarnings in unchanged sources are not ours to print."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        return ast.parse(Path(path).read_bytes(), str(path))


def _module_level_names(tree):
    names = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            names.update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            names.update((alias.asname or alias.name).split(".")[0] for alias in node.names)
    return names


def _launch_sites(tree):
    """Yield (node, enclosing scope chain, call or None) for subprocess/os launch names.

    Direct calls carry their ast.Call so a literal "-m" entry point can be read;
    bare references (aliases, default arguments, callbacks) are yielded too.
    """
    def is_launch(node):
        return isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and \
            node.attr in LAUNCH_ATTRS.get(node.value.id, ())

    def visit(node, scope):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                kind = "class" if isinstance(child, ast.ClassDef) else "def"
                for decorator in child.decorator_list:
                    yield from visit(decorator, scope)
                if not isinstance(child, ast.ClassDef):
                    yield from visit(child.args, scope)
                inner = scope + [(kind, child.name)]
                for statement in child.body:
                    yield from visit_node(statement, inner)
                continue
            yield from visit_node(child, scope)

    def visit_node(child, scope):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            wrapper = ast.Module(body=[child], type_ignores=[])
            yield from visit(wrapper, scope)
            return
        if isinstance(child, ast.Call) and is_launch(child.func):
            yield child.func, scope, child
            for arg in list(child.args) + [k.value for k in child.keywords]:
                yield from visit_node(arg, scope)
            return
        if is_launch(child):
            yield child, scope, None
            return
        yield from visit(child, scope)
    yield from visit(tree, [])


def _entry_module(call):
    """The literal module named after "-m" in a call's first list/tuple argument, if any."""
    if call is None or not call.args or not isinstance(call.args[0], (ast.List, ast.Tuple)):
        return None, False
    items = call.args[0].elts
    for index, item in enumerate(items):
        if isinstance(item, ast.Constant) and item.value == "-m":
            nxt = items[index + 1] if index + 1 < len(items) else None
            if isinstance(nxt, ast.Constant) and isinstance(nxt.value, str):
                return nxt.value, True
            return "<non-literal>", True
    return None, False


def inspect_closure(roots, root=REPO):
    """Static import closure of the selected roots, plus every process launch site.

    Launch sites are classified by reachability from the selection: a site in a
    test method of a TestCase that neither lives in a selected module nor is
    bound into one at module level cannot be selected by these selectors. Named
    "-m" child entry points are resolved and added to the closure; an unresolved
    entry point anywhere else fails closed. At runtime every launch is refused by
    the process sentinel regardless, and any attempt fails the run.
    """
    root = Path(root)
    root_rel = {Path(r).as_posix() for r in roots}
    selected_names = set()
    for rel in root_rel:
        selected_names |= _module_level_names(_parse(root / rel))
    pending = [root / r for r in roots]
    seen, external, launches, unresolved = {}, set(), [], []
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
        tree = _parse(path)
        for node in ast.walk(tree):
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
                if stem in LAUNCH_ATTRS and any(a.name in LAUNCH_ATTRS[stem] for a in node.names):
                    launches.append({"file": relative, "call": "from %s import %s" % (stem, ",".join(
                        a.name for a in node.names)), "line": node.lineno, "scope": None,
                        "reachability": "imported_launch_name", "entry_module": None, "entry_file": None})
            for name in names:
                target = module_file(name, root)
                if target is not None:
                    pending.append(target)
                elif name and module_file(name.split(".")[0], root) is None and not (root / name.split(".")[0]).is_dir():
                    external.add(name)
        for attr, scope, call in _launch_sites(tree):
            classes = [n for k, n in scope if k == "class"]
            defs = [n for k, n in scope if k == "def"]
            if relative in root_rel:
                reach = "selected_module"
            elif classes and defs and defs[-1].startswith("test") and not (set(classes) & selected_names):
                reach = "unselected_test_method"
            else:
                reach = "reachable_or_unknown"
            module, has_m = _entry_module(call)
            entry = entry_point_file(module, root) if has_m and module != "<non-literal>" else None
            site = {"file": relative, "call": ("" if call is not None else "reference:") + attr.value.id + "." + attr.attr,
                    "line": attr.lineno,
                    "scope": ".".join(n for _, n in scope) or "<module>", "reachability": reach,
                    "entry_module": module, "entry_file": entry.relative_to(root).as_posix() if entry else None}
            launches.append(site)
            if entry is not None:
                pending.append(entry)
            elif has_m:
                site["entry_resolution"] = "unresolved"
                if reach != "unselected_test_method":
                    unresolved.append(site)
    model_clients = sorted(n for n in external if n.split(".")[0] in FORBIDDEN_IMPORTS)
    network_stdlib = sorted(n for n in external if any(n == m or n.startswith(m + ".") for m in NETWORK_MODULES))
    nonstdlib = sorted({n.split(".")[0] for n in external} - set(sys.stdlib_module_names))
    return {"root": str(root), "selector_files": sorted(root_rel), "files": seen,
            "external_top_level": sorted(external), "forbidden": model_clients,
            "network_stdlib_imports": network_stdlib, "non_stdlib": nonstdlib,
            "process_launch_sites": sorted(launches, key=lambda s: (s["file"], s["line"])),
            "unresolved_reachable_entry_points": unresolved,
            "runtime_launch_policy": "every launch refused by the process sentinel (allowed argv: none)"}


def closure_problems(closure, label):
    problems = []
    if closure["forbidden"]:
        problems.append(label + "_closure_imports_model_or_network_client")
    if closure["unresolved_reachable_entry_points"]:
        problems.append(label + "_closure_unresolved_child_entry_point")
    return problems


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
    active, GUARD["active"] = GUARD["active"], False  # runner-owned evidence write, not test code
    try:
        raw_path.write_bytes(raw)
    finally:
        GUARD["active"] = active
    summary = {"kind": "in-process unittest under sentinels (no child command)",
               "selectors": list(names), "ids": ids, "tests_run": result.testsRun,
               "failures": sorted({case_id(t) for t, _ in result.failures}),
               "failure_text": collect(result.failures, 600),
               "errors": sorted({case_id(t) for t, _ in result.errors}),
               "error_text": collect(result.errors, 1200),
               "skipped": [case_id(t) for t, _ in result.skipped],
               "expected_failures": len(result.expectedFailures),
               "unexpected_successes": len(result.unexpectedSuccesses),
               "successful": result.wasSuccessful(), "raw_output": str(raw_path.relative_to(run)),
               "raw_output_sha256": sha256_bytes(raw)}
    record["phases"][label] = summary
    return summary


def clean_pass(summary, expected_ids=None):
    ok = (summary["successful"] and summary["tests_run"] > 0 and not summary["skipped"]
          and not summary["expected_failures"] and not summary["unexpected_successes"]
          and len(summary["ids"]) == summary["tests_run"])
    if expected_ids is not None:
        ok = ok and summary["ids"] == sorted(expected_ids)
    return ok


def loaded_modules():
    found = {}
    for name, module in sorted(sys.modules.items()):
        if name.split(".")[0] in APP_PACKAGES + ("tools",):
            path = getattr(module, "__file__", None)
            if path:
                found[name] = os.path.realpath(path)
    return found


def purge_app_modules():
    for name in [n for n in sys.modules if n.split(".")[0] in APP_PACKAGES]:
        del sys.modules[name]
    importlib.invalidate_caches()


def use_source_root(root):
    """Make root the only application source on sys.path."""
    real = {os.path.realpath(REPO), os.path.realpath(root)}
    sys.path[:] = [p for p in sys.path if os.path.realpath(p or os.getcwd()) not in real]
    sys.path.insert(0, str(root))


def focused_ids(cases=FROZEN_CASES):
    return [FOCUSED_CLASS + ".test_" + case for case in cases]


# modes -------------------------------------------------------------------------
def guarded_start(run, mode, record):
    record["closure"] = inspect_closure(CLOSURE_ROOTS[mode])
    record["problems"].extend(closure_problems(record["closure"], "candidate"))
    record["guard"] = {}
    if not probes_ok(enter_phase(run, record["guard"])):
        record["problems"].append("sentinel_self_check_failed")
    use_source_root(REPO)


def check_loaded(record, key, root):
    modules = record[key] = loaded_modules()
    outside = [n for n, p in modules.items() if n.split(".")[0] in APP_PACKAGES
               and not p.startswith(os.path.realpath(root) + os.sep)]
    if outside:
        record["problems"].append(key + "_outside_source_root")


def finish_hits(record):
    record["sentinel_hits"] = {k: list(v) for k, v in HITS.items()}
    record["allowed_processes"] = list(ALLOWED_PROCESS_LOG)
    if any(HITS.values()):
        record["problems"].append("sentinel_hits")


def guarded_stop(record):
    leave_phase()
    check_loaded(record, "loaded_modules", REPO)
    finish_hits(record)


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
    # 1. Baseline source: a local clone at the pinned base (runner-owned git, receipted).
    baseline_dir = run / "baseline"
    baseline_dir.mkdir(mode=0o700)
    clone = baseline_dir / "repo"
    if run_phase(run, record, "baseline_clone", ["git", "clone", "--quiet", "--no-hardlinks", str(REPO), str(clone)],
                 run)[0] != 0:
        return
    if run_phase(run, record, "baseline_checkout", ["git", "-C", str(clone), "checkout", "--quiet", "--detach",
                                                   BASE_COMMIT], run)[0] != 0:
        return
    code, out, _ = run_phase(run, record, "baseline_rev_parse", ["git", "-C", str(clone), "rev-parse", "HEAD"], run)
    baseline_head = out.decode("utf-8", "replace").strip() if code == 0 else None
    baseline_target = sha256_file(clone / TARGET) if (clone / TARGET).is_file() else None
    record["baseline_source"] = {"repo": str(clone), "head": baseline_head, "target_sha256": baseline_target}
    if baseline_head != BASE_COMMIT or baseline_target != BASE_TARGET_SHA256:
        record["problems"].append("baseline_not_at_pinned_base")
        return
    # 2. Both closures are inspected before ANY test code runs.
    record["closure_baseline"] = inspect_closure(CLOSURE_ROOTS["compatibility"], clone)
    record["closure"] = inspect_closure(CLOSURE_ROOTS["compatibility"], REPO)
    record["problems"].extend(closure_problems(record["closure_baseline"], "baseline"))
    record["problems"].extend(closure_problems(record["closure"], "candidate"))
    if record["problems"]:
        record["problems"].append("compatibility_not_run_closure_or_prerequisite_failed")
        return
    clone_before = tree_digest(clone)
    # 3. Baseline phase, in this process, under the sentinels; no application module loaded yet.
    if any(n.split(".")[0] in APP_PACKAGES for n in sys.modules):
        record["problems"].append("application_module_loaded_before_baseline")
        return
    record["guard_baseline"] = {}
    if not probes_ok(enter_phase(baseline_dir / "phase", record["guard_baseline"])):
        record["problems"].append("baseline_sentinel_self_check_failed")
    use_source_root(clone)
    original_cwd = os.getcwd()
    os.chdir(clone)  # relative paths in the baseline resolve to baseline files
    try:
        baseline = run_tests(COMPATIBILITY, "baseline", run, record)
    finally:
        os.chdir(original_cwd)
    record["baseline_cwd"] = str(clone)
    leave_phase()
    check_loaded(record, "loaded_modules_baseline", clone)
    record["sentinel_hits_baseline"] = {k: list(v) for k, v in HITS.items()}
    purge_app_modules()
    # 4. Candidate phase from this tree, fresh imports, same protections.
    record["guard_candidate"] = {}
    if not probes_ok(enter_phase(run / "candidate", record["guard_candidate"])):
        record["problems"].append("candidate_sentinel_self_check_failed")
    use_source_root(REPO)
    candidate = run_tests(COMPATIBILITY, "candidate", run, record)
    leave_phase()
    check_loaded(record, "loaded_modules", REPO)
    finish_hits(record)
    clone_after = tree_digest(clone)
    record["baseline_tree_before"], record["baseline_tree_after"] = clone_before, clone_after
    if clone_before != clone_after:
        record["problems"].append("baseline_tree_changed_during_run")
    if not clean_pass(baseline):
        record["problems"].append("baseline_not_clean_pass")
    if not clean_pass(candidate):
        record["problems"].append("candidate_not_clean_pass")
    for selector in COMPATIBILITY:
        for label, summary in (("baseline", baseline), ("candidate", candidate)):
            if not any(i.startswith(selector + ".") for i in summary["ids"]):
                record["problems"].append("%s_selector_selected_no_tests:%s" % (label, selector))
    if baseline["ids"] != candidate["ids"] or baseline["tests_run"] != candidate["tests_run"]:
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
    if run_phase(run, record, "second_copy_clone", ["git", "clone", "--quiet", "--no-hardlinks", str(REPO),
                                                   str(second)], run)[0] != 0:
        return
    if run_phase(run, record, "second_copy_checkout", ["git", "-C", str(second), "checkout", "--quiet", "--detach",
                                                      record["identity"]["head"]], run)[0] != 0:
        return
    code, out, _ = run_phase(run, record, "second_copy_rev_parse", ["git", "-C", str(second), "rev-parse", "HEAD"],
                             run)
    clone_head = out.decode("utf-8", "replace").strip() if code == 0 else None
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
    code, out, err = run_phase(run, record, "relocated_focused", argv, second)
    children = sorted(p for p in child_out.iterdir() if p.is_dir()) if child_out.is_dir() else []
    child = json.loads((children[0] / "evidence.json").read_text()) if len(children) == 1 else None
    rel = {
        "clone_head": clone_head,
        "child_run_dir": str(children[0]) if len(children) == 1 else None,
        "child_verdict": child and child.get("verdict"), "child_repo": child and child["identity"]["repo"],
        "child_head": child and child["identity"]["head"],
        "child_tests_run": child and child["phases"]["focused"]["tests_run"],
        "child_manifest": child and child.get("manifest"),
        "child_loaded_modules": child and child.get("loaded_modules")}
    record["relocated"] = rel
    second_real = os.path.realpath(second)
    if code != 0 or not child or child.get("verdict") != "REVIEW_REQUIRED":
        record["problems"].append("relocated_focused_not_review_required")
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
    record["interpreter_check"] = {"sys_executable": sys.executable, "declared": declared_python}
    if sys.executable != declared_python:
        record["problems"].append("interpreter_not_declared_credential_scan_python3")
        return
    scanner = runtime.get("detect_secrets")
    checker = REPO / "tools" / "check_secret_report.py"
    if not isinstance(scanner, str) or not os.path.isfile(scanner) or not os.access(scanner, os.X_OK):
        record["problems"].append("detect_secrets_unavailable")
        return
    code, version, _ = run_phase(run, record, "scanner_version", [scanner, "--version"], REPO)
    record["scanner"] = {"path": scanner, "version": version.decode("utf-8", "replace").strip() if code == 0 else None}
    scan_argv = [scanner, "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"]
    code, out, _ = run_phase(run, record, "secret_scan", scan_argv, REPO, stdout_name="secret-scan.json")
    if code != 0:
        return
    try:
        parsed = json.loads(out)
        results = parsed["results"]
        if not isinstance(results, dict):
            raise ValueError("results not an object")
        record["scan_summary"] = {"raw_hit_files": sorted(results), "raw_hits": sum(len(v) for v in results.values()),
                                  "hits_in_changed_files": sorted(set(results) & set(ALLOWED_CHANGES))}
        if record["scan_summary"]["hits_in_changed_files"]:
            record["problems"].append("scanner_hits_in_changed_files")
    except (ValueError, KeyError, TypeError) as exc:
        record["problems"].append("secret_report_unreadable:" + type(exc).__name__)
        return
    run_phase(run, record, "check_secret_report", [sys.executable, str(checker), str(run / "secret-scan.json")], REPO)


def mode_public_tree(run, record):
    if not record["identity"]["clean"]:
        record["problems"].append("public_tree_requires_clean_exact_head")
    argv = [sys.executable, str(REPO / "tools" / "check_public_tree.py")]
    # GATING: the unchanged checker, cwd this repo, tree exactly as staged, no env
    # overrides, no exclusion. A nonzero exit fails the mode; nothing can convert it.
    code, out, _ = run_phase(run, record, "public_tree", argv, REPO)
    try:
        record["public_tree_findings"] = json.loads(out).get("findings")
    except ValueError:
        record["public_tree_findings"] = None
    if code != 0:
        record["problems"].append("public_tree_check_failed")
    # NON-GATING diagnostic: the same unchanged checker inside a clean clone of the
    # exact HEAD (outside the repo, under this run). Recorded for the staging/contract
    # owner only. Its result never removes a problem and never adds a pass.
    diag = run / "diagnostic-clean-clone"
    diag.mkdir(mode=0o700)
    clone = diag / "repo"
    record["diagnostic_clean_clone"] = {"gating": False, "purpose": "what the tracked tree alone yields"}
    if run_phase(run, record, "diagnostic_clone", ["git", "clone", "--quiet", "--no-hardlinks", str(REPO), str(clone)],
                 run, required=False)[0] != 0:
        return
    if run_phase(run, record, "diagnostic_checkout", ["git", "-C", str(clone), "checkout", "--quiet", "--detach",
                                                     record["identity"]["head"] or "HEAD"], run, required=False)[0] != 0:
        return
    dcode, dout, _ = run_phase(run, record, "diagnostic_public_tree",
                               [sys.executable, str(clone / "tools" / "check_public_tree.py")], clone, required=False)
    try:
        findings = json.loads(dout).get("findings")
    except ValueError:
        findings = None
    record["diagnostic_clean_clone"].update({"exit": dcode, "findings": findings,
                                             "checker_sha256": sha256_file(clone / "tools" / "check_public_tree.py")})


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
                         "Sentinels are not a hostile-code sandbox. Test code runs only in-process under them.",
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
        elif isinstance(phase, dict) and "exit" in phase:
            print("%s: exit=%s required=%s" % (label, phase["exit"], phase.get("required")))
    return record["exit"]


if __name__ == "__main__":
    raise SystemExit(main())
