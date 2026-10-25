#!/usr/bin/env python3
"""FC7-BOARD-UNICODE acceptance runner (synthetic, offline, review-required).

Modes: --check focused | compatibility | negative | portability | credentials |
public-tree, each with --output-root. Every invocation creates a fresh,
exclusive run directory beneath the output root, records actual argv, cwd,
exit codes, stdout/stderr, selected test IDs/counts, base and candidate HEAD,
the acceptance-contract identity and changed-file hashes, and exits 0 only
when its complete named contract passes. Passing output is REVIEW REQUIRED,
never semantic, legal, human, stage, live or publication acceptance.

Paths derive from __file__ and the staged SOURCE-MANIFEST.json in the repo
parent. Child processes use literal argv lists without a shell. Guards are
offline sentinels, not a hostile-code sandbox.
"""
import argparse
import ast
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import tarfile
import time
from pathlib import Path

RUNNER = Path(__file__).resolve()
REPO = RUNNER.parents[1]
CANDIDATE = REPO.parent
MANIFEST = CANDIDATE / "SOURCE-MANIFEST.json"
CARD = "FC7-BOARD-UNICODE"
TARGET = "campaign_tool/records/catalog_board.py"
FOCUSED_MODULE = "tests.records.test_fc7_board_unicode"
COMPAT_MODULE = "tests.records.test_catalog_board"
CASES = ("ordinary_ascii_and_nonascii_html", "high_low_embedded_surrogates_each_emitted_surface",
         "unused_metadata_stays_ignored", "html_hostile_text_remains_escaped",
         "exact_card_count_and_input_unchanged", "fixed_error_no_value_echo")
FOCUSED_IDS = sorted(f"{FOCUSED_MODULE}.Fc7BoardUnicodeTests.test_{case}" for case in CASES)
CHANGED = (TARGET, "tests/records/test_fc7_board_unicode.py", "tools/accept_fc7_board_unicode.py")
STAGING_UNTRACKED = {".venv"}
MODES = ("focused", "compatibility", "negative", "portability", "credentials", "public-tree")
CONTRACT = {
    "card": CARD, "version": 1, "modes": list(MODES), "focused_ids": FOCUSED_IDS,
    "compatibility_selector": COMPAT_MODULE, "changed_paths": list(CHANGED),
    "error": "invalid_catalog_text",
    "negative": "disable catalog_board._require_encodable in memory; agency-hint refusal must fail "
                "with an unencodable return; restore; refusal and accented+emoji positive pass",
    "secret_scan_args": ["scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"],
}
CONTRACT_ID = hashlib.sha256(json.dumps(CONTRACT, sort_keys=True).encode()).hexdigest()
# Imports allowed anywhere in the selected test/renderer closure.
STDLIB_OK = {"base64", "copy", "hashlib", "html", "inspect", "math", "re", "unittest",
             "collections", "__future__"}
CHILD_TIMEOUT = 900


# ---------------------------------------------------------------- child side
def _child(args):
    """Runs in a fresh interpreter: sentinels first, then application imports."""
    import tempfile
    run_dir = Path(args.run_dir).resolve()
    source_root = Path(args.source_root).resolve()
    tmp = run_dir / "tmp"
    tempfile.tempdir = str(tmp)
    for name in ("TMPDIR", "TMP", "TEMP"):
        os.environ[name] = str(tmp)
    sys.dont_write_bytecode = True
    trips = []
    blocked_net = {"socket.connect", "socket.getaddrinfo", "socket.gethostbyname",
                   "socket.gethostbyaddr", "socket.bind", "socket.sendto", "socket.sendmsg"}
    blocked_proc = {"subprocess.Popen", "os.system", "os.posix_spawn", "os.exec", "os.spawn",
                    "os.fork", "os.forkpty", "pty.spawn"}
    blocked_import = {"openai", "anthropic", "ollama", "requests", "httpx", "urllib3", "aiohttp"}
    write_bits = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
    allowed_db = (run_dir / "db").resolve()
    # Writes are confined to the run's disposable subdirectories and the child
    # report; source copies created inside the run directory stay read-only.
    write_roots = [(run_dir / sub).resolve() for sub in ("tmp", "home", "db")]
    report_path = Path(os.path.abspath(args.report))

    def inside(path, root):
        try:
            Path(os.path.abspath(path)).relative_to(root)
            return True
        except ValueError:
            return False

    def hook(event, hook_args):
        reason = None
        if event in blocked_net:
            reason = "network"
        elif event in blocked_proc:
            reason = "process"
        elif event == "import" and hook_args and str(hook_args[0]).split(".")[0] in blocked_import:
            reason = "model-or-network-client-import"
        elif event == "sqlite3.connect":
            database = hook_args[0] if hook_args else None
            if not (database == ":memory:" or (isinstance(database, (str, bytes, os.PathLike))
                                               and inside(os.fsdecode(database), allowed_db))):
                reason = "database-path"
        elif event == "open" and hook_args and isinstance(hook_args[0], (str, bytes, os.PathLike)):
            mode = hook_args[1] if len(hook_args) > 1 else None
            flags = hook_args[2] if len(hook_args) > 2 else 0
            writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or \
                      (isinstance(flags, int) and flags & write_bits)
            target = os.fsdecode(hook_args[0])
            if writing and Path(os.path.abspath(target)) != report_path and \
                    not any(inside(target, root) for root in write_roots):
                reason = "write-outside-run-scratch"
        if reason:
            trips.append({"event": event, "reason": reason})
            raise RuntimeError(f"fc7 sentinel blocked {event} ({reason})")

    sys.addaudithook(hook)
    import socket
    import sqlite3

    def probe(name, action):
        before = len(trips)
        try:
            action()
        except RuntimeError as error:
            return {"probe": name, "blocked": "fc7 sentinel" in str(error) and len(trips) > before}
        except Exception as error:  # reached the real operation: not blocked
            return {"probe": name, "blocked": False, "error": type(error).__name__}
        return {"probe": name, "blocked": False}

    self_test = [
        probe("getaddrinfo", lambda: socket.getaddrinfo("localhost", 9)),
        probe("sqlite-undeclared-path", lambda: sqlite3.connect(str(run_dir / "undeclared.db"))),
        probe("subprocess", lambda: subprocess.Popen([sys.executable, "--version"])),
        probe("write-into-source", lambda: open(source_root / "fc7-sentinel-probe", "w")),
        probe("model-client-import", lambda: __import__("openai")),
    ]
    memory = sqlite3.connect(":memory:")
    memory.close()
    self_test.append({"probe": "sqlite-memory-allowed", "blocked": False, "allowed": True})
    self_test_trips = list(trips)
    trips.clear()

    sys.path.insert(0, str(source_root))
    import io
    import unittest

    class Result(unittest.TextTestResult):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.outcomes, self.details = {}, {}

        def _mark(self, test, outcome, err=None):
            # Any non-pass outcome is sticky; a later success never clears it.
            tid = test.id()
            if self.outcomes.get(tid) in (None, "running", "pass"):
                self.outcomes[tid] = outcome
            if err is not None:
                self.details.setdefault(tid, []).append(self._exc_info_to_string(err, test)[-3000:])

        def startTest(self, test):
            super().startTest(test)
            self.outcomes.setdefault(test.id(), "running")

        def addSuccess(self, test):
            super().addSuccess(test)
            if self.outcomes.get(test.id()) == "running":
                self.outcomes[test.id()] = "pass"

        def addFailure(self, test, err):
            super().addFailure(test, err)
            self._mark(test, "fail", err)

        def addError(self, test, err):
            super().addError(test, err)
            self._mark(test, "error", err)

        def addSkip(self, test, reason):
            super().addSkip(test, reason)
            self._mark(test, "skip")

        def addExpectedFailure(self, test, err):
            super().addExpectedFailure(test, err)
            self._mark(test, "expected-failure", err)

        def addUnexpectedSuccess(self, test):
            super().addUnexpectedSuccess(test)
            self._mark(test, "unexpected-success")

        def addSubTest(self, test, subtest, err):
            super().addSubTest(test, subtest, err)
            if err is not None:
                self._mark(test, "fail", err)

    def run_suite(suite):
        stream = io.StringIO()
        result = unittest.TextTestRunner(stream=stream, verbosity=2, resultclass=Result).run(suite)
        outcomes = {tid: ("fail" if o == "running" else o) for tid, o in result.outcomes.items()}
        return {"tests_run": result.testsRun, "outcomes": dict(sorted(outcomes.items())),
                "details": result.details, "text": stream.getvalue()}

    report = {"kind": args.child, "source_root": str(source_root), "python": sys.executable,
              "self_test": self_test, "self_test_trips": self_test_trips}
    if args.child == "unittest":
        suite = unittest.defaultTestLoader.loadTestsFromName(args.module)
        report["suite"] = run_suite(suite)
    elif args.child == "negative":
        report["negative"] = _negative(run_suite)
    modules = {}
    for name, module in sorted(sys.modules.items()):
        path = getattr(module, "__file__", None)
        if path and (name.startswith(("campaign_tool", "tests")) or inside(path, source_root)):
            modules[name] = str(Path(path).resolve())
    report["loaded_modules"] = modules
    report["trips"] = list(trips)
    Path(args.report).write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(report.get("suite", {}).get("text", ""))
    print(json.dumps(report.get("negative", {}), indent=2, sort_keys=True))
    return 0


def _negative(run_suite):
    import copy
    import importlib
    import unittest
    board = importlib.import_module("campaign_tool.records.catalog_board")
    tests = importlib.import_module(FOCUSED_MODULE)
    original = board._require_encodable
    data = tests.agency_hint_surrogate_catalog()
    pristine = copy.deepcopy(data)
    case = tests.Fc7BoardUnicodeTests("test_fixed_error_no_value_echo")
    out = {"mutation": "campaign_tool.records.catalog_board._require_encodable = lambda text: None"}
    board._require_encodable = lambda text: None
    try:
        try:
            case.check_refusal(data)
            out["mutant_assertion"] = "passed"
        except AssertionError as error:
            out["mutant_assertion"] = "failed"
            out["mutant_assertion_message"] = str(error).splitlines()[0][:200]
        returned = board.render_board(copy.deepcopy(pristine))
        out["mutant_returned_type"] = type(returned).__name__
        try:
            returned.encode("utf-8", "strict")
            out["mutant_output_utf8"] = "encodable"
        except UnicodeEncodeError as error:
            out["mutant_output_utf8"] = "UnicodeEncodeError"
            out["mutant_unencodable_at"] = [error.start, error.end]
    finally:
        board._require_encodable = original
    out["restored_identity"] = board._require_encodable is original
    try:
        error = case.check_refusal(data)
        out["restored_refusal"] = "pass" if error.args == ("invalid_catalog_text",) else "fail"
    except AssertionError as error:
        out["restored_refusal"] = "fail: " + str(error).splitlines()[0][:200]
    try:
        positive = case.render_strict(tests.accented_emoji_catalog())
        ok = "Comté de Fictión 📄" in positive and "instantané-🚓" in positive
        out["restored_positive"] = "pass" if ok else "fail: text missing"
    except Exception as error:  # record, never swallow into success
        out["restored_positive"] = f"fail: {type(error).__name__}"
    out["input_unchanged"] = data == pristine
    suite = unittest.defaultTestLoader.loadTestsFromName(FOCUSED_MODULE)
    out["restored_focused_suite"] = run_suite(suite)
    return out


# --------------------------------------------------------------- parent side
def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run_cmd(evidence, label, argv, cwd, run_dir, env=None, stdout_path=None):
    started = time.time()
    proc = subprocess.run(argv, cwd=str(cwd), env=env, capture_output=True, timeout=CHILD_TIMEOUT,
                          shell=False, check=False)
    out_file = Path(stdout_path) if stdout_path else run_dir / f"{label}.stdout"
    out_file.write_bytes(proc.stdout)
    (run_dir / f"{label}.stderr").write_bytes(proc.stderr)
    entry = {"label": label, "argv": [str(a) for a in argv], "cwd": str(cwd), "exit": proc.returncode,
             "seconds": round(time.time() - started, 3), "stdout_file": str(out_file),
             "stdout_sha256": hashlib.sha256(proc.stdout).hexdigest(),
             "stderr_file": str(run_dir / f"{label}.stderr"),
             "stdout_tail": proc.stdout.decode("utf-8", "replace")[-4000:],
             "stderr_tail": proc.stderr.decode("utf-8", "replace")[-4000:]}
    evidence["commands"].append(entry)
    return proc, entry


def git(evidence, run_dir, label, *args, cwd=REPO):
    proc, _ = run_cmd(evidence, label, ["git", "-C", str(cwd), *args], cwd, run_dir)
    if proc.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed")
    return proc.stdout.decode("utf-8")


class Checks:
    def __init__(self):
        self.items = []

    def add(self, name, ok, detail=None):
        self.items.append({"check": name, "ok": bool(ok), "detail": detail})
        return bool(ok)

    def passed(self):
        return bool(self.items) and all(item["ok"] for item in self.items)


def child_env(run_dir):
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("PYTHON", "LD_")) and key not in {"TMPDIR", "TMP", "TEMP", "HOME"}}
    for name in ("TMPDIR", "TMP", "TEMP"):
        env[name] = str(run_dir / "tmp")
    env["HOME"] = str(run_dir / "home")
    return env


def run_child(evidence, checks, run_dir, label, kind, source_root, module, runner=RUNNER):
    report = run_dir / f"{label}.report.json"
    argv = [sys.executable, "-B", "-I", str(runner), "--child", kind, "--source-root", str(source_root),
            "--module", module, "--run-dir", str(run_dir), "--report", str(report)]
    proc, _ = run_cmd(evidence, label, argv, source_root, run_dir, env=child_env(run_dir))
    checks.add(f"{label}: child exit 0", proc.returncode == 0, proc.returncode)
    if not report.is_file():
        checks.add(f"{label}: child report written", False)
        return None
    data = json.loads(report.read_text(encoding="utf-8"))
    probes = [p for p in data["self_test"] if not p.get("allowed")]
    checks.add(f"{label}: offline sentinels block network/process/db/write/model probes",
               len(probes) == 5 and all(p["blocked"] for p in probes), data["self_test"])
    checks.add(f"{label}: no sentinel trips during tests", data["trips"] == [], data["trips"])
    loaded = data["loaded_modules"]
    board = loaded.get("campaign_tool.records.catalog_board", "")
    checks.add(f"{label}: renderer loaded from source root",
               board.startswith(str(Path(source_root).resolve()) + os.sep), board)
    checks.add(f"{label}: every repo module loaded from source root",
               all(path.startswith(str(Path(source_root).resolve()) + os.sep)
                   for name, path in loaded.items() if name.startswith(("campaign_tool", "tests"))),
               loaded)
    return data


def suite_checks(checks, label, suite, expected_ids=None):
    outcomes = suite["outcomes"]
    ids = sorted(outcomes)
    checks.add(f"{label}: selected tests > 0", len(ids) > 0 and suite["tests_run"] == len(ids),
               {"tests_run": suite["tests_run"], "ids": len(ids)})
    if expected_ids is not None:
        checks.add(f"{label}: exact selected test IDs", ids == sorted(expected_ids), ids)
    bad = {tid: o for tid, o in outcomes.items() if o != "pass"}
    checks.add(f"{label}: every selected test passed (no skips/expected failures)", not bad,
               {"bad": bad, "details": {tid: suite["details"].get(tid) for tid in bad}})
    return ids


def import_closure(module_name, root):
    """Statically list the in-repo import closure and every top-level import."""
    seen, queue, found = {}, [module_name], {}
    while queue:
        name = queue.pop()
        if name in seen:
            continue
        parts = name.split(".")
        candidates = [root.joinpath(*parts).with_suffix(".py"), root.joinpath(*parts, "__init__.py")]
        path = next((p for p in candidates if p.is_file()), None)
        seen[name] = str(path) if path else None
        if path is None:
            continue
        for index in range(1, len(parts)):
            init = root.joinpath(*parts[:index], "__init__.py")
            if init.is_file():
                queue.append(".".join(parts[:index]))
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module] + [f"{node.module}.{alias.name}" for alias in node.names]
            for imported in names:
                top = imported.split(".")[0]
                found.setdefault(top, set()).add(name)
                if top in {"campaign_tool", "tests"}:
                    queue.append(imported)
    return seen, {top: sorted(users) for top, users in sorted(found.items())}


def closure_check(checks, label, module_name, root):
    files, imports = import_closure(module_name, root)
    unexpected = sorted(set(imports) - STDLIB_OK - {"campaign_tool", "tests"})
    checks.add(f"{label}: import closure is stdlib-only plus in-repo renderer (no process/network/db)",
               not unexpected and files.get(module_name), {"files": files, "imports": imports,
                                                            "unexpected": unexpected})
    return {"files": files, "imports": imports}


def preflight(evidence, checks, run_dir, mode):
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    evidence["manifest"] = {"path": str(MANIFEST), "sha256": sha256_file(MANIFEST), "content": manifest}
    runtime = manifest.get("runtime", {})
    base = manifest.get("head", "")
    checks.add("manifest: card", manifest.get("card") == CARD, manifest.get("card"))
    checks.add("manifest: repo is this repo", Path(manifest.get("repo", "")).resolve() == REPO,
               manifest.get("repo"))
    checks.add("manifest: write root is repo parent",
               Path(manifest.get("write_root", "")).resolve() == CANDIDATE, manifest.get("write_root"))
    checks.add("manifest: base is a full commit id", re.fullmatch(r"[0-9a-f]{40}", base), base)
    remote = Path(manifest.get("remote", ""))
    checks.add("manifest: declared remote exists (read-only)", remote.is_dir(), str(remote))
    venv = Path(runtime.get("repo_venv", "")).resolve()
    checks.add("runtime: interpreter belongs to declared repo venv",
               Path(sys.prefix).resolve() == venv, {"prefix": sys.prefix, "declared": str(venv)})
    if mode == "credentials":
        checks.add("runtime: interpreter equals runtime.credential_scan_python3",
                   sys.executable == runtime.get("credential_scan_python3"),
                   {"executable": sys.executable, "declared": runtime.get("credential_scan_python3")})
    else:
        checks.add("runtime: invoked through the staged repo .venv",
                   sys.executable == str(REPO / ".venv" / "bin" / "python3"), sys.executable)
    head = git(evidence, run_dir, "git-head", "rev-parse", "HEAD").strip()
    git(evidence, run_dir, "git-base-commit", "cat-file", "-e", f"{base}^{{commit}}")
    git(evidence, run_dir, "git-base-ancestor", "merge-base", "--is-ancestor", base, "HEAD")
    diff = git(evidence, run_dir, "git-diff-names", "diff", "--name-only", base, "HEAD").split()
    status = git(evidence, run_dir, "git-status", "status", "--porcelain=v1", "--untracked-files=all")
    dirty = sorted({line[3:].strip() for line in status.splitlines() if line.strip()})
    touched = sorted(set(diff) | set(dirty))
    checks.add("scope: only the three declared paths differ from base (plus staged .venv link)",
               set(touched) <= set(CHANGED) | STAGING_UNTRACKED, touched)
    checks.add("scope: all three deliverables committed", set(CHANGED) <= set(diff), diff)
    proc, _ = run_cmd(evidence, "git-base-target", ["git", "-C", str(REPO), "show", f"{base}:{TARGET}"],
                      REPO, run_dir)
    # The brief's pin is not committed as a literal (no high-entropy test tokens);
    # the computed value is recorded for the reviewer to compare with the brief.
    base_target_sha256 = hashlib.sha256(proc.stdout).hexdigest()
    checks.add("source pin: base target readable at declared base", proc.returncode == 0 and proc.stdout,
               {"base_target_sha256": base_target_sha256})
    evidence.update(base_head=base, candidate_head=head, worktree_dirty=sorted(set(dirty) - STAGING_UNTRACKED),
                    base_target_sha256=base_target_sha256,
                    changed_files={p: sha256_file(REPO / p) for p in CHANGED},
                    runtime=runtime)
    return manifest, base, head


def mode_focused(evidence, checks, run_dir, manifest, base):
    closure_check(checks, "focused", FOCUSED_MODULE, REPO)
    data = run_child(evidence, checks, run_dir, "focused", "unittest", REPO, FOCUSED_MODULE)
    if data:
        evidence["selected_tests"] = suite_checks(checks, "focused", data["suite"], FOCUSED_IDS)


def mode_compatibility(evidence, checks, run_dir, manifest, base):
    tar_path = run_dir / "baseline.tar"
    git(evidence, run_dir, "git-archive-base", "archive", "--format=tar", "-o", str(tar_path), base,
        "--", "campaign_tool", "tests")
    baseline = run_dir / "baseline-src"
    with tarfile.open(tar_path) as archive:
        archive.extractall(baseline, filter="data")
    checks.add("compatibility: extracted baseline renderer equals the declared base blob",
               sha256_file(baseline / TARGET) == evidence["base_target_sha256"],
               evidence["base_target_sha256"])
    closure_check(checks, "compatibility-before", COMPAT_MODULE, baseline)
    closure_check(checks, "compatibility-after", COMPAT_MODULE, REPO)
    before = run_child(evidence, checks, run_dir, "compat-before", "unittest", baseline, COMPAT_MODULE)
    after = run_child(evidence, checks, run_dir, "compat-after", "unittest", REPO, COMPAT_MODULE)
    if before and after:
        ids_before = suite_checks(checks, "compat-before", before["suite"])
        ids_after = suite_checks(checks, "compat-after", after["suite"])
        checks.add("compatibility: selectors are real tests in the named module",
                   all(t.startswith(COMPAT_MODULE + ".") for t in ids_before + ids_after))
        checks.add("compatibility: identical test IDs before and after", ids_before == ids_after,
                   {"before": len(ids_before), "after": len(ids_after)})
        evidence["selected_tests"] = {"before": ids_before, "after": ids_after}


def mode_negative(evidence, checks, run_dir, manifest, base):
    target_before = sha256_file(REPO / TARGET)
    data = run_child(evidence, checks, run_dir, "negative", "negative", REPO, FOCUSED_MODULE)
    checks.add("negative: production file unchanged on disk", sha256_file(REPO / TARGET) == target_before)
    if data:
        neg = data["negative"]
        checks.add("negative: mutant refusal assertion failed (mutant detected)",
                   neg.get("mutant_assertion") == "failed", neg.get("mutant_assertion_message"))
        checks.add("negative: deciding cause is an unencodable returned string",
                   neg.get("mutant_returned_type") == "str" and neg.get("mutant_output_utf8") == "UnicodeEncodeError",
                   {k: neg.get(k) for k in ("mutant_returned_type", "mutant_output_utf8", "mutant_unencodable_at")})
        checks.add("negative: original check restored", neg.get("restored_identity") is True)
        checks.add("negative: restored refusal passes", neg.get("restored_refusal") == "pass",
                   neg.get("restored_refusal"))
        checks.add("negative: restored accented+emoji positive passes",
                   neg.get("restored_positive") == "pass", neg.get("restored_positive"))
        checks.add("negative: synthetic input unchanged", neg.get("input_unchanged") is True)
        evidence["selected_tests"] = suite_checks(checks, "negative-restored-focused",
                                                  neg["restored_focused_suite"], FOCUSED_IDS)


BOX_PATH = re.compile(r"^/(home|Users|root|tmp|var|opt|srv|mnt)/")


def box_constants(path):
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if BOX_PATH.search(node.value) or ("agent" + "-work") in node.value:
                found.append(node.value[:120])
    return found


def mode_portability(evidence, checks, run_dir, manifest, base):
    for rel in CHANGED[1:]:
        hits = box_constants(REPO / rel)
        checks.add(f"portability: no box-only absolute constants in {rel}", not hits, hits)
    closure_check(checks, "portability", FOCUSED_MODULE, REPO)
    listing = git(evidence, run_dir, "git-ls-files", "ls-files", "-z", "--", "campaign_tool", "tests",
                  CHANGED[2])
    copy_root = run_dir / "copy-b" / "relocated-prefix" / "DeFlock-Campaign-Tool"
    copied = 0
    for rel in filter(None, listing.split("\0")):
        source = REPO / rel
        if source.is_symlink() or not source.is_file():
            continue
        dest = copy_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(source.read_bytes())
        copied += 1
    checks.add("portability: second source copy created at a different prefix",
               copied > 0 and all((copy_root / p).read_bytes() == (REPO / p).read_bytes() for p in CHANGED),
               {"root": str(copy_root), "files": copied})
    first = run_child(evidence, checks, run_dir, "portability-a", "unittest", REPO, FOCUSED_MODULE)
    second = run_child(evidence, checks, run_dir, "portability-b", "unittest", copy_root, FOCUSED_MODULE,
                       runner=copy_root / CHANGED[2])
    if first and second:
        suite_checks(checks, "portability-a", first["suite"], FOCUSED_IDS)
        evidence["selected_tests"] = suite_checks(checks, "portability-b", second["suite"], FOCUSED_IDS)
        a = first["loaded_modules"].get("campaign_tool.records.catalog_board")
        b = second["loaded_modules"].get("campaign_tool.records.catalog_board")
        checks.add("portability: copies loaded distinct renderer files", a and b and a != b, [a, b])
        evidence["loaded_modules"] = {"a": first["loaded_modules"], "b": second["loaded_modules"]}


def mode_credentials(evidence, checks, run_dir, manifest, base):
    scanner = manifest["runtime"].get("detect_secrets", "")
    checks.add("credentials: runtime.detect_secrets is an installed executable",
               os.path.isfile(scanner) and os.access(scanner, os.X_OK), scanner)
    report = run_dir / "secret-scan.json"
    proc, _ = run_cmd(evidence, "detect-secrets", [scanner, *CONTRACT["secret_scan_args"]], REPO, run_dir,
                      stdout_path=report)
    checks.add("credentials: scanner exit 0", proc.returncode == 0, proc.returncode)
    try:
        results = json.loads(report.read_text(encoding="utf-8"))["results"]
        checks.add("credentials: scanner produced a results map", isinstance(results, dict),
                   {"files_with_hits": len(results)})
    except (ValueError, KeyError) as error:
        checks.add("credentials: scanner produced a results map", False, type(error).__name__)
    proc, _ = run_cmd(evidence, "check-secret-report",
                      [sys.executable, str(REPO / "tools" / "check_secret_report.py"), str(report)], REPO, run_dir)
    checks.add("credentials: unchanged repo checker exit 0", proc.returncode == 0, proc.returncode)


def mode_public_tree(evidence, checks, run_dir, manifest, base):
    proc, _ = run_cmd(evidence, "check-public-tree",
                      [sys.executable, str(REPO / "tools" / "check_public_tree.py")], REPO, run_dir)
    try:
        findings = json.loads(proc.stdout.decode("utf-8")).get("findings")
    except ValueError:
        findings = "unparseable"
    # No exemption: any finding, including the staging .venv link, fails closed.
    checks.add("public-tree: unchanged repo checker exit 0", proc.returncode == 0,
               {"exit": proc.returncode, "findings": findings,
                "staging_venv_is_symlink": (REPO / ".venv").is_symlink()})


HANDLERS = {"focused": mode_focused, "compatibility": mode_compatibility, "negative": mode_negative,
            "portability": mode_portability, "credentials": mode_credentials, "public-tree": mode_public_tree}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", choices=MODES)
    parser.add_argument("--output-root")
    parser.add_argument("--child", choices=("unittest", "negative"), help=argparse.SUPPRESS)
    parser.add_argument("--source-root", help=argparse.SUPPRESS)
    parser.add_argument("--module", help=argparse.SUPPRESS)
    parser.add_argument("--run-dir", help=argparse.SUPPRESS)
    parser.add_argument("--report", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.child:
        return _child(args)
    if not args.check or not args.output_root:
        parser.error("--check and --output-root are required")
    out_root = Path(args.output_root).resolve()
    try:
        out_root.relative_to(REPO)
        parser.error("--output-root must be outside the repo")
    except ValueError:
        pass
    try:
        out_root.relative_to(CANDIDATE)
    except ValueError:
        parser.error("--output-root must be inside the candidate write root")
    out_root.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    run_dir = out_root / f"run-{stamp}-{os.getpid()}-{secrets.token_hex(4)}"
    run_dir.mkdir(exist_ok=False)
    for sub in ("tmp", "home", "db"):
        (run_dir / sub).mkdir()
    evidence = {"card": CARD, "check": args.check, "argv": [sys.executable, *sys.argv], "cwd": os.getcwd(),
                "python": sys.executable, "run_dir": str(run_dir), "contract_id": CONTRACT_ID,
                "contract": CONTRACT, "runner_sha256": sha256_file(RUNNER),
                "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "commands": [],
                "status": "REVIEW REQUIRED (machine checks only; not semantic, legal, live or publication acceptance)"}
    checks = Checks()
    try:
        manifest, base, _ = preflight(evidence, checks, run_dir, args.check)
        if checks.passed():
            HANDLERS[args.check](evidence, checks, run_dir, manifest, base)
        else:
            checks.add("preflight passed before work", False)
    except Exception as error:  # fail closed with the recorded reason
        checks.add("runner completed without exception", False, f"{type(error).__name__}: {error}")
    evidence["checks"] = checks.items
    evidence["verdict"] = "PASS" if checks.passed() else "FAIL"
    evidence["finished_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    (run_dir / "result.json").write_text(json.dumps(evidence, indent=2, sort_keys=True), encoding="utf-8")
    for item in checks.items:
        print(("PASS " if item["ok"] else "FAIL ") + item["check"])
    print(f"{args.check}: {evidence['verdict']} run_dir={run_dir} contract={CONTRACT_ID}")
    return 0 if evidence["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
