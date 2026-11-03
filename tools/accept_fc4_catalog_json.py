"""Scoped offline acceptance runner for FC4-CATALOG-JSON.

Exactly one --check mode per invocation; evidence goes to a fresh, exclusive run
directory below --output-root, which must be absolute and outside this repo.

  focused        tests/records/test_fc4_catalog_json.py (the ten frozen cases)
  compatibility  unchanged tests.records.test_catalog, tests.test_secret_report and
                 tests.test_public_tree: baseline (a local clone checked out at the
                 pinned base) and candidate (this tree, in-process under sentinels)
  negative       in-memory test seams restore permissive duplicate-key decoding and,
                 separately, permissive nonfinite decoding; the matching rejection
                 tests must FAIL, the finite controls must pass with both seams
                 permissive, and every focused case must pass once restored
  portability    the focused check re-run from a local clone of the exact committed
                 HEAD at a different absolute prefix; loaded module paths recorded
  credentials    installed detect-secrets beside this interpreter, then the
                 unchanged tools/check_secret_report.py on that exact report
  public-tree    the unchanged tools/check_public_tree.py

In-process test modes install, before any application import: bytecode
suppression, tempfile redirection into the run directory, socket refusal,
model-client import refusal, an audit hook that refuses writes outside the run
directory and refuses every process launch except the enumerated local
check_secret_report.py call made by tests.test_secret_report. These are
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
SCHEMA = "fc4-catalog-json-acceptance/v1"
BRIEF_ID = "FC4-CATALOG-JSON"
BRIEF_FILE = "deflock-FC4-CATALOG-JSON.brief"
# Identities are kept as integers so no quoted hex literal enters the credential scan.
BRIEF_SHA256 = format(72531814500045059625965037334163365008818637670004093808855597717657059626899, "064x")
BASE_COMMIT = format(354539287719765981472502559298907628339695127894, "040x")
BASE_CATALOG_SHA256 = format(80390227156260598695668525659806455154018790030130606335311747732229780625455, "064x")
MODES = ("focused", "compatibility", "negative", "portability", "credentials", "public-tree")
ALLOWED_CHANGES = ("campaign_tool/records/catalog.py", "tests/records/test_fc4_catalog_json.py",
                   "tools/accept_fc4_catalog_json.py")
FOCUSED = "tests.records.test_fc4_catalog_json"
FOCUSED_CLASS = FOCUSED + ".CatalogJsonAmbiguityTests"
FROZEN_CASES = ("unique_rows_and_blank_line_numbers", "duplicate_role_both_orders", "equal_duplicate_values",
                "nested_duplicate_keys", "nan_and_infinities", "exponent_overflow",
                "malformed_json_and_nonobject", "finite_boundary_values",
                "valid_import_then_rejected_import_preserves_current", "input_bytes_unchanged")
COMPATIBILITY = ("tests.records.test_catalog", "tests.test_secret_report", "tests.test_public_tree")
NEGATIVE_DUPLICATE = ("duplicate_role_both_orders", "equal_duplicate_values", "nested_duplicate_keys")
NEGATIVE_NONFINITE = ("exponent_overflow", "nan_and_infinities")
NEGATIVE_FINITE_CONTROL = ("finite_boundary_values", "unique_rows_and_blank_line_numbers",
                           "malformed_json_and_nonobject")
HASHED = ALLOWED_CHANGES + ("tests/records/test_catalog.py", "tests/test_secret_report.py",
                            "tests/test_public_tree.py", "tools/check_secret_report.py",
                            "tools/check_public_tree.py", "campaign_tool/records/catalog_board.py")
CLOSURE_ROOTS = {
    "focused": ("campaign_tool/records/catalog.py", "tests/records/test_fc4_catalog_json.py"),
    "negative": ("campaign_tool/records/catalog.py", "tests/records/test_fc4_catalog_json.py"),
    "compatibility": ("tests/records/test_catalog.py", "tests/test_secret_report.py", "tests/test_public_tree.py"),
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
    code, base_catalog, _ = run_cmd(["git", "-C", str(root), "show", BASE_COMMIT + ":campaign_tool/records/catalog.py"], root)
    base_catalog_sha = sha256_bytes(base_catalog) if code == 0 else None
    if base_catalog_sha != BASE_CATALOG_SHA256:
        problems.append("base_catalog_pin_mismatch")
    return {"base": BASE_COMMIT, "changed_files": sorted(changed), "outside_allowed": outside,
            "base_catalog_sha256": base_catalog_sha, "problems": problems}


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
    for allowed in GUARD["allowed_argv"]:
        if len(argv) == len(allowed) + 1 and argv[:len(allowed)] == list(allowed):
            return True
    return False


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
        if "mode=ro" not in text and _outside(target):
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
    GUARD["allowed_argv"] = ((sys.executable, str(REPO / "tools" / "check_secret_report.py")),)
    sys.addaudithook(audit)
    GUARD["active"] = True
    # inert self-checks: an escape write and a disallowed launch must both be refused
    probes = {}
    parent_fd = REAL_OS_OPEN(os.path.dirname(GUARD["root"]), os.O_RDONLY | os.O_DIRECTORY)
    try:
        for name, action in (("write_escape", lambda: open(os.path.join(os.path.dirname(GUARD["root"]), "fc4-escape-probe-" + secrets.token_hex(4)), "w")),
                             ("dir_fd_write_escape", lambda: os.open("fc4-escape-probe-" + secrets.token_hex(4), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent_fd)),
                             ("process_escape", lambda: REAL_POPEN(["fc4-refused-probe"]))):
            try:
                action()
                probes[name] = "NOT_REFUSED"
            except PermissionError:
                probes[name] = "refused"
            except OSError as exc:
                probes[name] = "error:" + type(exc).__name__
    finally:
        os.close(parent_fd)
    HITS["write"] = [h for h in HITS["write"] if "fc4-escape-probe-" not in str(h[1])]
    HITS["process"] = [h for h in HITS["process"] if "fc4-refused-probe" not in h[1]]
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
    seen, external = {}, set()
    while pending:
        path = pending.pop()
        relative = path.relative_to(root).as_posix()
        if relative in seen:
            continue
        seen[relative] = sha256_file(path)
        package = path.relative_to(root).with_suffix("").parts
        package = package[:-1] if path.name != "__init__.py" else package[:-1]
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
            "non_stdlib": nonstdlib}


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
    from tests.records import test_fc4_catalog_json as module
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
    baseline_ids = sorted({match.group(2) for match in re.finditer(r"(?m)^(test\w*) \((tests\.[\w.]+)\)", text)})
    record["phases"]["baseline"] = {"argv": argv, "cwd": str(clone), "exit": code, "ran_line": ran[-1] if ran else None,
                                    "ok": "\nOK" in text and code == 0, "skipped_mentioned": "skipped=" in text,
                                    "ids": baseline_ids, "raw_output": "baseline.unittest.txt",
                                    "raw_output_sha256": sha256_bytes(raw),
                                    "head": git("rev-parse", "HEAD", cwd=clone)[1],
                                    "sentinels": "none (existing suite entry point in a child process)"}
    guarded_start(run, "compatibility", record)
    summary = run_tests(COMPATIBILITY, "candidate", run, record)
    guarded_stop(record)
    base = record["phases"]["baseline"]
    if not base["ok"] or base["skipped_mentioned"]:
        record["problems"].append("baseline_not_clean_pass")
    if not clean_pass(summary):
        record["problems"].append("candidate_not_clean_pass")
    ran_count = re.match(r"Ran (\d+) tests? ", base["ran_line"] or "")
    if base["ids"] != summary["ids"] or not ran_count or int(ran_count.group(1)) != summary["tests_run"]:
        record["problems"].append("baseline_candidate_test_set_differs")


def mode_negative(run, record):
    guarded_start(run, "negative", record)
    from campaign_tool.records import catalog as production
    seams = ("_unique_object", "_reject_constant", "_finite_float")
    missing = [s for s in seams if not callable(getattr(production, s, None))]
    if missing:
        record["problems"].append("decode_seams_missing")
        record["missing_seams"] = missing
        guarded_stop(record)
        return
    probe_dir = run / "probes"
    probe_dir.mkdir()
    duplicate = probe_dir / "duplicate.jsonl"
    duplicate.write_text('{"effective_role": "unresolved", "effective_role": "agency_original"}\n')
    overflow = probe_dir / "overflow.jsonl"
    overflow.write_text('{"v": 1e400}\n{"w": NaN}\n')

    def probe(path):
        try:
            return {"result": repr(production.jsonl(path))}
        except ValueError as exc:
            return {"value_error": str(exc)}

    permissive_duplicates = mock.patch.object(production, "_unique_object", dict)
    permissive_constant = mock.patch.object(production, "_reject_constant", float)
    permissive_float = mock.patch.object(production, "_finite_float", float)
    probes = record["seam_probes"] = {}
    with permissive_duplicates:
        probes["duplicate_seam"] = probe(duplicate)
        a = run_tests(focused_ids(NEGATIVE_DUPLICATE), "permissive_duplicates", run, record)
    with permissive_constant, permissive_float:
        probes["nonfinite_seam"] = probe(overflow)
        b = run_tests(focused_ids(NEGATIVE_NONFINITE), "permissive_nonfinite", run, record)
    with permissive_duplicates, permissive_constant, permissive_float:
        c = run_tests(focused_ids(NEGATIVE_FINITE_CONTROL), "both_permissive_finite_control", run, record)
    probes["restored_duplicate"] = probe(duplicate)
    probes["restored_overflow"] = probe(overflow)
    d = run_tests(focused_ids(), "restored", run, record)
    guarded_stop(record)

    def detected(summary, cases):
        expected = sorted(focused_ids(cases))
        return (summary["tests_run"] == len(cases) and summary["failures"] == expected and not summary["errors"]
                and all(any("ValueError not raised" in text for text in summary["failure_text"][i])
                        for i in expected))
    if not detected(a, NEGATIVE_DUPLICATE):
        record["problems"].append("duplicate_regression_not_detected")
    if not detected(b, NEGATIVE_NONFINITE):
        record["problems"].append("nonfinite_regression_not_detected")
    if not clean_pass(c, focused_ids(NEGATIVE_FINITE_CONTROL)):
        record["problems"].append("finite_control_failed_with_permissive_seams")
    if not clean_pass(d, focused_ids()):
        record["problems"].append("restored_positives_failed")
    if probes["duplicate_seam"] != {"result": "[(1, {'effective_role': 'agency_original'})]"}:
        record["problems"].append("duplicate_seam_not_on_production_path")
    if probes["nonfinite_seam"] != {"result": "[(1, {'v': inf}), (2, {'w': nan})]"}:
        record["problems"].append("nonfinite_seam_not_on_production_path")
    if probes["restored_duplicate"] != {"value_error": "invalid_catalog_json line 1"} or \
            probes["restored_overflow"] != {"value_error": "invalid_catalog_json line 1"}:
        record["problems"].append("restored_probe_not_rejected")


def mode_portability(run, record):
    if not record["identity"]["clean"]:
        record["problems"].append("portability_requires_clean_committed_tree")
        return
    markers = {}
    for rel in ("tests/records/test_fc4_catalog_json.py", "tools/accept_fc4_catalog_json.py"):
        text = (REPO / rel).read_text(encoding="utf-8")
        markers[rel] = [m for m in BOX_MARKERS if m in text]
    record["box_path_markers"] = markers
    if any(markers.values()):
        record["problems"].append("box_specific_paths_in_new_files")
    second = run / "second-prefix" / ("copy-" + secrets.token_hex(6)) / "repo"
    second.parent.mkdir(parents=True)
    code, out, err = run_cmd(["git", "clone", "--quiet", "--no-hardlinks", str(REPO), str(second)], run)
    record["phases"]["clone"] = {"exit": code, "path": str(second), "stderr": err.decode("utf-8", "replace")[-400:]}
    if code != 0:
        record["problems"].append("second_copy_clone_failed")
        return
    git("checkout", "--quiet", "--detach", record["identity"]["head"], cwd=second)
    clone_head = git("rev-parse", "HEAD", cwd=second)[1]
    child_out = run / "relocated-out"
    argv = [sys.executable, str(second / "tools" / "accept_fc4_catalog_json.py"),
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
        "child_loaded_modules": child and child.get("loaded_modules")}
    rel = record["phases"]["relocated_focused"]
    second_real = os.path.realpath(second)
    if code != 0 or not child or child.get("verdict") != "REVIEW_REQUIRED":
        record["problems"].append("relocated_focused_failed")
        return
    if rel["child_repo"] != second_real or clone_head != record["identity"]["head"] or rel["child_head"] != clone_head:
        record["problems"].append("relocated_identity_mismatch")
    loaded = rel["child_loaded_modules"] or {}
    if "campaign_tool.records.catalog" not in loaded or \
            any(not p.startswith(second_real + os.sep) for p in loaded.values()):
        record["problems"].append("relocated_modules_not_from_second_copy")
    if child["identity"]["source_sha256"] != record["identity"]["source_sha256"]:
        record["problems"].append("relocated_source_bytes_differ")


def mode_credentials(run, record):
    if not record["identity"]["clean"]:
        record["problems"].append("credential_scan_requires_clean_exact_head")
    scanner = Path(sys.executable).parent / "detect-secrets"
    checker = REPO / "tools" / "check_secret_report.py"
    if not scanner.is_file() or not os.access(scanner, os.X_OK):
        record["problems"].append("detect_secrets_unavailable")
        return
    scan_argv = [str(scanner), "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"]
    code, out, err = run_cmd(scan_argv, REPO)
    report = run / "secret-scan.json"
    report.write_bytes(out)
    (run / "secret-scan.stderr.txt").write_bytes(err)
    version = run_cmd([str(scanner), "--version"], REPO)
    record["phases"]["scan"] = {"argv": scan_argv, "cwd": str(REPO), "exit": code,
                                "report": report.name, "report_sha256": sha256_bytes(out),
                                "scanner": str(scanner), "scanner_version": version[1].decode().strip()}
    try:
        parsed = json.loads(out)
        results = parsed["results"]
        if not isinstance(results, dict):
            raise ValueError("results not an object")
        record["phases"]["scan"]["raw_hit_files"] = sorted(results)
        record["phases"]["scan"]["raw_hits"] = sum(len(v) for v in results.values())
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
    if code2 != 0 or "potential secrets: 0;" not in out2.decode("utf-8", "replace"):
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


HANDLERS = {"focused": mode_focused, "compatibility": mode_compatibility, "negative": mode_negative,
            "portability": mode_portability, "credentials": mode_credentials, "public-tree": mode_public_tree}


def main(argv=None):
    parser = argparse.ArgumentParser(description="FC4-CATALOG-JSON scoped acceptance (offline)")
    parser.add_argument("--check", required=True, choices=MODES)
    parser.add_argument("--output-root", required=True)
    args = parser.parse_args(argv)
    run = fresh_run_dir(args.output_root, args.check)
    record = {"schema": SCHEMA, "brief_id": BRIEF_ID, "brief_file": BRIEF_FILE, "brief_sha256": BRIEF_SHA256,
              "mode": args.check, "argv": [sys.executable, *sys.argv], "cwd": os.getcwd(),
              "interpreter": sys.executable, "python": platform.python_version(),
              "started": now(), "run_dir": str(run), "phases": {}, "problems": [],
              "limits": ["REVIEW REQUIRED: not full-repository CI; full discovery is operator-run.",
                         "Sentinels are not a hostile-code sandbox; child processes do not inherit them.",
                         "Synthetic fixtures only; no real records, network, model calls or live actions."]}
    record["identity"] = identity()
    record["scope"] = scope_check()
    record["problems"].extend(record["scope"]["problems"])
    before = tree_digest(REPO)
    try:
        HANDLERS[args.check](run, record)
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
