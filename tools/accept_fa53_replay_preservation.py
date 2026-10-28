"""Scoped FA53 acceptance runner: unchanged mail replay needs preservation declarations.

Each invocation validates --output-parent, creates one fresh run directory below
it and exits 0 only when its case passed. A pass is REVIEW REQUIRED scoped
evidence; it is not full-repository CI, semantic acceptance, a stage promotion
or permission. Full discovery is operator-run.

Test cases run in a file-launched child interpreter that disables bytecode,
redirects temporary paths and installs network/process/live-file/write
sentinels before importing any repository module. The parent never imports
repository code.
"""
import argparse
from datetime import datetime, timezone
import difflib
import hashlib
import json
import os
from pathlib import Path
import platform
import secrets
import shutil
import stat
import subprocess
import sys

sys.dont_write_bytecode = True

REPO = Path(__file__).resolve().parents[1]
# Exclusive candidate root: this file is <candidate>/repo/tools/<name>. Evidence
# parents must lie strictly below it (and outside the repository).
ADMITTED_ROOT = REPO.parent
PRODUCT = "campaign_tool/records/intake/mail_delta.py"
TEST_MODULE = "tests.records.test_fa53_replay_preservation"
AUTHORED = (PRODUCT, "tests/records/test_fa53_replay_preservation.py",
            "tools/accept_fa53_replay_preservation.py")
COMPATIBILITY = ("tests.records.test_mail_delta", "tests.records.test_mail_wire_intake")
REFERENCED = ("tests/records/test_mail_delta.py", "tests/records/test_mail_wire_intake.py",
              "campaign_tool/records/intake/folder.py", "tools/check_secret_report.py",
              "tools/check_public_tree.py")
# A4: exactly these assertions must fail against the original unchanged-replay gate.
BASELINE_EXPECTED_FAILURES = (
    "test_a2_missing_original_declaration_refused",
    "test_a2_missing_one_attachment_declaration_refused",
    "test_a2_missing_multiple_declarations_refused",
    "test_a3_outcome_driver_matches_frozen_behavior",
)
APP_MODULES = ("campaign_tool.records.intake.mail_delta", "campaign_tool.records.intake.folder")
# A3: retained before/after snapshots, one JSON file per refused-replay case ID.
# Missing-declaration cases map to how many preservation declarations they omit.
SNAPSHOT_DIR = "a3-snapshots"
MISSING_SNAPSHOTS = {"a2-missing-original": 1, "a2-missing-one_attachment": 1, "a2-missing-multiple": 3}
OTHER_SNAPSHOTS = {"a2-wrong-size": "preservation_size_conflict", "a3-absent-blob": "existing_blob_invalid",
                   "a3-corrupt-blob": "existing_blob_invalid"}
CASES = ("behavior", "compatibility", "negative-control", "isolation", "credentials")
SCRUBBED_ENV_PREFIXES = ("PYTHON", "LD_")
SCRUBBED_ENV_MARKERS = ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "OLLAMA", "OPENAI", "ANTHROPIC",
                        "PROXY")


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    return sha256_bytes(Path(path).read_bytes())


def git(*args):
    done = subprocess.run(["git", *args], cwd=REPO, capture_output=True, check=False)
    return done.returncode, done.stdout


def identity():
    _, head = git("rev-parse", "HEAD")
    _, status = git("status", "--porcelain=v1", "--untracked-files=all")
    files = {}
    for name in AUTHORED + REFERENCED:
        path = REPO / name
        files[name] = sha256_file(path) if path.is_file() else None
    return {"head": head.decode().strip(), "status_porcelain": status.decode(),
            "clean_tree": status == b"", "file_sha256": files}


def safe_output_parent(raw, admitted=None, repo=None):
    """Refuse relative, out-of-candidate, linked, in-repo or non-directory evidence parents.

    Pure predicate: it only inspects (lstat) and never creates anything. The
    containment test runs before any filesystem access, so an out-of-root path
    is refused without touching it.
    """
    admitted = Path(os.path.abspath(admitted or ADMITTED_ROOT))
    repo = Path(os.path.abspath(repo or REPO))
    if not Path(raw).is_absolute():
        return None, "output_parent_not_absolute"
    path = Path(os.path.abspath(raw))
    if path == admitted or not path.is_relative_to(admitted):
        return None, "output_parent_outside_candidate"
    probe = Path("/")
    for part in path.parts[1:]:
        probe = probe / part
        try:
            mode = probe.lstat()
        except FileNotFoundError:
            if probe != path:
                return None, "output_parent_ancestor_missing"
            break
        if stat.S_ISLNK(mode.st_mode):
            return None, "output_parent_linked:" + str(probe)
        if not stat.S_ISDIR(mode.st_mode):
            return None, "output_parent_not_directory:" + str(probe)
    if path == repo or path.is_relative_to(repo):
        return None, "output_parent_inside_repo"
    if repo.is_relative_to(path):
        return None, "output_parent_contains_repo"
    return path, None


class Run:
    def __init__(self, parent, case):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        parent.mkdir(mode=0o700, exist_ok=True)
        self.dir = parent / ("%s-%s-%d-%s" % (case, stamp, os.getpid(), secrets.token_hex(4)))
        self.dir.mkdir(mode=0o700)
        self.children = []

    def env(self):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(SCRUBBED_ENV_PREFIXES)
               and not any(marker in k.upper() for marker in SCRUBBED_ENV_MARKERS)}
        tmp = self.dir / "tmp"
        tmp.mkdir(mode=0o700, exist_ok=True)
        env.update(TMPDIR=str(tmp), TMP=str(tmp), TEMP=str(tmp), PYTHONDONTWRITEBYTECODE="1",
                   NO_PROXY="*", no_proxy="*")
        return env

    def child(self, label, argv, stdout_name=None, cwd=None):
        cwd = Path(cwd or REPO)
        done = subprocess.run([str(a) for a in argv], cwd=cwd, env=self.env(), capture_output=True,
                              check=False)
        out_path = self.dir / (stdout_name or label + ".stdout")
        err_path = self.dir / (label + ".stderr")
        out_path.write_bytes(done.stdout)
        err_path.write_bytes(done.stderr)
        entry = {"label": label, "argv": [str(a) for a in argv], "cwd": str(cwd), "exit": done.returncode,
                 "stdout": str(out_path), "stdout_sha256": sha256_bytes(done.stdout),
                 "stderr": str(err_path), "stderr_sha256": sha256_bytes(done.stderr)}
        self.children.append(entry)
        return entry, done.stdout

    def test_child(self, label, mode, extra=(), source=None):
        source = Path(source or REPO)
        result_path = self.dir / (label + "-result.json")
        entry, _ = self.child(label, [sys.executable, source / "tools" / Path(__file__).name,
                                      "--child", mode, "--run-dir", self.dir,
                                      "--source-root", source, "--result-json", result_path, *extra],
                              cwd=source)
        try:
            record = json.loads(result_path.read_text())
            record["result_json"] = str(result_path)
            record["result_sha256"] = sha256_file(result_path)
        except (OSError, ValueError):
            record = {"pass": False, "error": "child_result_missing_or_invalid"}
        record["child_exit"] = entry["exit"]
        record["pass"] = record.get("pass") is True and entry["exit"] == 0
        return record


def original_baseline(run):
    """Original module: PRODUCT at the parent of the latest commit that changed it.

    The deciding correction must be the ONLY delta: every changed line is an
    added line naming ``undeclared`` or its adjacent comment; nothing removed.
    The resolved revision and SHA256 are recorded for comparison with the
    frozen brief source pin.
    """
    code, rev = git("rev-list", "-1", "HEAD", "--", PRODUCT)
    if code != 0 or not rev.strip():
        return None, "baseline_revision_unavailable"
    code, parent = git("rev-parse", rev.decode().strip() + "^")
    if code != 0:
        return None, "baseline_parent_unavailable"
    parent = parent.decode().strip()
    code, blob = git("show", parent + ":" + PRODUCT)
    if code != 0:
        return None, "baseline_git_show_failed"
    candidate = (REPO / PRODUCT).read_bytes()
    if blob == candidate:
        return None, "baseline_equals_candidate"
    delta = [line for line in difflib.ndiff(blob.decode().splitlines(), candidate.decode().splitlines())
             if line[:2] in ("+ ", "- ")]
    allowed = all(line.startswith("+ ") and ("undeclared" in line or line[2:].lstrip().startswith("#"))
                  for line in delta)
    if not delta or not allowed:
        return None, "baseline_delta_not_scoped"
    target = run.dir / "baseline" / "mail_delta_original.py"
    target.parent.mkdir(mode=0o700)
    target.write_bytes(blob)
    return {"revision": parent, "correction_commit": rev.decode().strip(), "path": str(target),
            "sha256": sha256_file(target), "delta_lines": delta}, None


def source_copy(run):
    """Second complete source copy (working tree bytes, no .git) under this run."""
    target = run.dir / "source-copy"
    shutil.copytree(REPO, target, symlinks=True,
                    ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
    mismatched = [name for name in AUTHORED + REFERENCED
                  if sha256_file(REPO / name) != sha256_file(target / name)]
    return target, mismatched


def check_credentials(run):
    scanner = Path(sys.executable).parent / "detect-secrets"
    if not scanner.is_file():
        return {"pass": False, "error": "missing_dependency_detect_secrets", "scanner": str(scanner)}
    scan, raw = run.child("detect-secrets", [scanner, "scan", "--all-files", "--exclude-files", "^.git/",
                                             "--no-verify"], stdout_name="secret-scan.json")
    report_path = run.dir / "secret-scan.json"
    try:
        report = json.loads(raw)
        report_ok = isinstance(report["results"], dict)
        hits = sum(len(v) for v in report["results"].values()) if report_ok else None
    except (ValueError, KeyError, TypeError):
        report_ok, hits = False, None
    check, out = run.child("check-secret-report",
                           [sys.executable, REPO / "tools" / "check_secret_report.py", report_path])
    summary = [line for line in out.decode(errors="replace").splitlines() if "potential secrets:" in line]
    return {"pass": scan["exit"] == 0 and report_ok and check["exit"] == 0,
            "scanner": str(scanner), "report": str(report_path), "report_sha256": sha256_file(report_path),
            "raw_report_hits": hits, "checker_summary": summary}


def rebind_snapshots(run, verdict):
    """Parent re-hash of the child's retained A3 snapshots; fail closed on any gap."""
    report = verdict.get("a3_snapshots")
    expected = set(MISSING_SNAPSHOTS) | set(OTHER_SNAPSHOTS)
    if not isinstance(report, dict) or set(report.get("cases") or {}) != expected or report.get("problems"):
        return False
    for case in sorted(expected):
        entry = report["cases"][case]
        path = Path(entry["path"])
        if path.parent != run.dir / SNAPSHOT_DIR or not path.is_file() or sha256_file(path) != entry["sha256"]:
            return False
    return True


def parent_main(args):
    started = datetime.now(timezone.utc).isoformat()
    parent, problem = safe_output_parent(args.output_parent)
    if problem:
        print(json.dumps({"case": args.case, "pass": False, "exit": 2, "error": problem}))
        return 2
    run = Run(parent, args.case)
    before = identity()
    if args.case == "behavior":
        verdict = run.test_child("behavior", "unittest", ("--module", TEST_MODULE))
    elif args.case == "compatibility":
        extra = []
        for name in COMPATIBILITY:
            extra += ["--module", name]
        verdict = run.test_child("compatibility", "unittest", extra)
    elif args.case == "negative-control":
        baseline, problem = original_baseline(run)
        if problem:
            verdict = {"pass": False, "error": problem}
        else:
            verdict = run.test_child("negative-control", "negative-control",
                                     ("--baseline", baseline["path"], "--baseline-sha256", baseline["sha256"]))
            verdict["baseline"] = baseline
    elif args.case == "isolation":
        copy, mismatched = source_copy(run)
        if mismatched:
            verdict = {"pass": False, "error": "source_copy_mismatch", "files": mismatched}
        else:
            verdict = run.test_child("isolation", "isolation", ("--module", TEST_MODULE,
                                                                "--primary-repo", REPO), source=copy)
            verdict["source_copy"] = str(copy)
    else:
        verdict = check_credentials(run)
    if args.case in ("behavior", "isolation"):
        verdict["a3_snapshots_rebound"] = rebind_snapshots(run, verdict)
        if not verdict["a3_snapshots_rebound"]:
            verdict["pass"] = False
    after = identity()
    stable = before == after
    passed = verdict.get("pass") is True and stable and bool(run.children) and all(
        c["exit"] is not None for c in run.children)
    record = {"case": args.case, "argv": [sys.executable, *sys.argv], "cwd": os.getcwd(), "repo": str(REPO),
              "interpreter": sys.executable, "python_version": platform.python_version(),
              "run_dir": str(run.dir), "started_utc": started, "ended_utc": datetime.now(timezone.utc).isoformat(),
              "identity_before": before, "identity_after": after, "identity_stable": stable,
              "children": run.children, "verdict": verdict, "pass": passed, "exit": 0 if passed else 1,
              "review_required": True,
              "scope": "scoped FA53 acceptance; not full-repository CI or semantic acceptance; "
                       "full discovery = operator-run"}
    (run.dir / "result.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"case": args.case, "pass": passed, "exit": record["exit"], "run_dir": str(run.dir),
                      "result_sha256": sha256_file(run.dir / "result.json"),
                      "head": after["head"], "tests_run": verdict.get("tests_run"),
                      "failures": verdict.get("failures"), "errors": verdict.get("errors"),
                      "skipped": verdict.get("skipped"),
                      "a3_snapshots_rebound": verdict.get("a3_snapshots_rebound")}, sort_keys=True))
    return record["exit"]


# ---------------------------------------------------------------- child side


def install_guards(run_dir, source_root, state):
    """Refuse network, unlisted processes, live-file reads and writes outside this run."""
    run_root = Path(run_dir).resolve()
    home = Path(os.path.expanduser("~")).resolve()
    readable = [run_root, Path(source_root).resolve(), REPO]
    readable += [Path(p).resolve() for p in {sys.prefix, sys.base_prefix, sys.exec_prefix}]
    readable += [Path(p).resolve() for p in sys.path if p and os.path.isabs(p)]
    write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
    allowed_cli = [sys.executable, "-B", "-m", "campaign_tool.records.intake.mail_delta"]

    def resolve(path, dir_fd=None):
        if isinstance(path, int):
            return None
        if isinstance(path, bytes):
            path = os.fsdecode(path)
        try:
            path = os.fspath(path)
        except TypeError:
            return False
        if path in (":memory:", ""):
            return None
        if path.startswith("file:"):
            path = path[5:].split("?", 1)[0]
        if not os.path.isabs(path) and isinstance(dir_fd, int) and not isinstance(dir_fd, bool):
            try:
                path = os.path.join(os.readlink("/proc/self/fd/%d" % dir_fd), path)
            except OSError:
                return False
        return Path(os.path.abspath(path)).resolve()

    def inside(path, dir_fd=None):
        target = resolve(path, dir_fd)
        return target is None or (target is not False and target.is_relative_to(run_root))

    def live(path):
        target = resolve(path)
        if target is None:
            return False
        if target is False:
            return True
        return target.is_relative_to(home) and not any(target.is_relative_to(r) for r in readable)

    def refuse(event, detail):
        state["violations"].append({"phase": state["phase"], "event": event, "detail": repr(detail)[:200]})
        raise PermissionError("fa53 acceptance guard refused " + event)

    mutations = {"os.mkdir": ((0, 2),), "os.remove": ((0, 1),), "os.rmdir": ((0, 1),),
                 "os.chmod": ((0, 2),), "os.chown": ((0, 3),), "os.utime": ((0, 3),),
                 "os.truncate": ((0, None),), "shutil.rmtree": ((0, 1),),
                 "os.rename": ((0, 2), (1, 3)), "os.replace": ((0, 2), (1, 3)), "os.link": ((0, 2), (1, 3)),
                 "os.symlink": ((1, 2),), "shutil.copyfile": ((1, None),), "shutil.move": ((0, None), (1, None))}

    def hook(event, args):
        if event.startswith("socket.") and event not in ("socket.__new__", "socket.gethostname"):
            refuse(event, args)
        elif event == "subprocess.Popen":
            argv = [str(a) for a in (args[1] or [])]
            if argv[:4] == allowed_cli:
                state["allowed_spawns"].append({"phase": state["phase"], "argv": argv[:4]})
            else:
                refuse(event, argv[:3])
        elif event == "ctypes.dlopen" and args and args[0] is None:
            state["allowed_spawns"].append({"phase": state["phase"], "dlopen": "self-process"})
        elif event in ("os.system", "os.posix_spawn", "os.spawn", "os.exec", "os.fork", "os.forkpty",
                       "pty.spawn", "ctypes.dlopen", "urllib.Request", "webbrowser.open"):
            refuse(event, args)
        elif event == "open":
            path, mode, flags = args
            writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                isinstance(flags, int) and flags & write_flags)
            if writing and not inside(path):
                refuse(event, path)
            if live(path):
                refuse("live-file-read", path)
        elif event == "sqlite3.connect":
            if not inside(args[0]):
                refuse(event, args[0])
        elif event in mutations:
            for index, fd_index in mutations[event]:
                if index >= len(args):
                    continue
                dir_fd = args[fd_index] if fd_index is not None and fd_index < len(args) else None
                if not inside(args[index], dir_fd):
                    refuse(event, args[index])

    sys.addaudithook(hook)


def sentinel_probes(run_dir, state):
    """A5 synthetic controls: each sentinel must refuse BEFORE any side effect."""
    import socket
    probes = {}
    outside = Path(run_dir).resolve().parent / ("fa53-sentinel-" + secrets.token_hex(6))
    live = Path(os.path.expanduser("~")) / (".fa53-sentinel-" + secrets.token_hex(6))

    def attempt(name, action):
        state["phase"] = "sentinel:" + name
        before = len(state["violations"])
        try:
            action()
            refused = False
        except PermissionError:
            refused = len(state["violations"]) == before + 1
        except OSError:
            refused = False
        probes[name] = refused
        state["phase"] = "tests"

    def network():
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.1)
            s.connect(("192.0.2.1", 9))

    attempt("network_connect", network)
    attempt("write_outside_run", lambda: open(outside, "wb").close())
    attempt("live_file_read", lambda: open(live, "rb").close())
    attempt("process_launch", lambda: subprocess.run(["true"], check=False))
    probes["write_outside_run_absent"] = not outside.exists()
    return probes


def scan_constants(names, run_dir):
    """Report absolute-path data constants and bound defaults reachable in app modules."""
    import inspect
    found = []

    def check(where, value):
        if isinstance(value, (str, os.PathLike)):
            text = os.fspath(value)
            if isinstance(text, str) and (text.startswith("/") or text.startswith("~")):
                found.append({"where": where, "inside_run": Path(text).resolve().is_relative_to(run_dir)})

    for name in names:
        module = sys.modules[name]
        for attr, value in vars(module).items():
            if attr.startswith("__") and attr.endswith("__"):
                continue  # import metadata (__file__, __cached__), not data constants
            check(name + "." + attr, value)
            targets = [value]
            if inspect.isclass(value):
                targets = [getattr(value, n) for n in vars(value)]
            for target in targets:
                if inspect.isfunction(target) or inspect.ismethod(target):
                    for n, default in enumerate(target.__defaults__ or ()):
                        check("%s.%s default[%d]" % (name, target.__qualname__, n), default)
                    for key, default in (target.__kwdefaults__ or {}).items():
                        check("%s.%s default[%s]" % (name, target.__qualname__, key), default)
    return found


def unittest_record(result):
    return {"tests_run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
            "skipped": len(result.skipped), "expected_failures": len(result.expectedFailures),
            "unexpected_successes": len(result.unexpectedSuccesses),
            "failed_ids": sorted(t.id() for t, _ in result.failures),
            "error_ids": sorted(t.id() for t, _ in result.errors)}


def clean(result):
    return (result.wasSuccessful() and result.testsRun > 0 and not result.skipped
            and not result.expectedFailures and not result.unexpectedSuccesses)


def verify_snapshots(sink):
    """A3: every refused-replay case left a complete, unchanged before/after snapshot."""
    expected = dict(OTHER_SNAPSHOTS, **{case: "existing_blob_invalid" for case in MISSING_SNAPSHOTS})
    report = {"dir": str(sink), "cases": {}, "problems": []}
    present = sorted(p.name for p in sink.iterdir()) if sink.is_dir() else []
    unexpected = sorted(set(present) - {case + ".json" for case in expected})
    if unexpected:
        report["problems"].append("unexpected_snapshot_files:" + ",".join(unexpected))
    for case, code in sorted(expected.items()):
        path = sink / (case + ".json")
        try:
            raw = path.read_bytes()
            data = json.loads(raw)
        except (OSError, ValueError):
            report["problems"].append(case + ":missing_or_malformed")
            continue
        before, after = data.get("before"), data.get("after")
        problems = []
        if data.get("case_id") != case or data.get("expected_refusal") != code:
            problems.append("case_or_code_mismatch")
        if data.get("outcome") != ["rejected", code]:
            problems.append("outcome_not_refusal")
        complete = all(isinstance(s, dict) and isinstance(s.get("tables"), dict) and s["tables"]
                       and isinstance(s.get("files"), dict) and s["files"] for s in (before, after))
        if not complete:
            problems.append("snapshot_incomplete")
        elif before != after or data.get("unchanged") is not True:
            problems.append("snapshot_changed")
        else:
            files = before["files"]
            if not any(name.endswith("/receipt.json") and entry[1] for name, entry in files.items()):
                problems.append("receipt_hash_missing")
            if not any("/blobs/" in name and entry[1] for name, entry in files.items()):
                problems.append("blob_hashes_missing")
            if not before["tables"].get("preservations") and case not in MISSING_SNAPSHOTS:
                problems.append("preservation_rows_missing")
        if case in MISSING_SNAPSHOTS and complete:
            missing = data.get("missing_declarations") or []
            declared = {json.loads(row)[0] for row in before["tables"].get("preservations", [])}
            if len(missing) != MISSING_SNAPSHOTS[case] or set(missing) & declared:
                problems.append("missing_declarations_inconsistent")
        report["cases"][case] = {"path": str(path), "sha256": sha256_bytes(raw), "problems": problems,
                                 "tables": sorted(before["tables"]) if complete else None,
                                 "file_count": len(before["files"]) if complete else None}
        report["problems"] += [case + ":" + p for p in problems]
    return report


def child_main(args):
    run_dir = Path(args.run_dir).resolve()
    source = Path(args.source_root).resolve()
    tmp = run_dir / "tmp"
    tmp.mkdir(mode=0o700, exist_ok=True)
    sys.dont_write_bytecode = True
    import tempfile
    tempfile.tempdir = str(tmp)
    state = {"phase": "tests", "violations": [], "allowed_spawns": []}
    install_guards(run_dir, source, state)
    record = {"mode": args.child, "tempdir": tempfile.gettempdir(), "source_root": str(source),
              "dont_write_bytecode": sys.dont_write_bytecode}
    if args.child == "isolation":
        record["sentinels"] = sentinel_probes(run_dir, state)
    sys.path.insert(0, str(source))
    import unittest
    checks = {}
    if args.child in ("unittest", "isolation"):
        sink = None
        if TEST_MODULE in args.module:
            import importlib
            fa53 = importlib.import_module(TEST_MODULE)
            sink = run_dir / SNAPSHOT_DIR
            setattr(fa53, "SNAPSHOT_SINK", sink)
        suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(name) for name in args.module)
        result = unittest.TextTestRunner(stream=sys.stderr, verbosity=2).run(suite)
        record.update(unittest_record(result), modules=args.module)
        checks["tests_clean"] = clean(result)
        if sink is not None:
            record["a3_snapshots"] = verify_snapshots(sink)
            checks["a3_snapshots_retained"] = not record["a3_snapshots"]["problems"]
    else:
        record.update(negative_control(Path(args.baseline), args.baseline_sha256, tmp))
        checks["negative_control"] = record.pop("pass")
    loaded = {name: sys.modules[name].__file__ for name in APP_MODULES + (TEST_MODULE,) + tuple(args.module or ())
              if name in sys.modules}
    record["module_paths"] = loaded
    checks["modules_from_source_root"] = bool(loaded) and all(
        Path(path).resolve().is_relative_to(source) for path in loaded.values())
    found = scan_constants([n for n in APP_MODULES if n in sys.modules], run_dir)
    record["path_constants_and_defaults"] = found
    checks["no_reachable_path_constants_outside_run"] = all(item["inside_run"] for item in found)
    if args.child == "isolation":
        checks["sentinels_refused"] = all(record["sentinels"].values()) and len(record["sentinels"]) == 5
        checks["no_bytecode_in_source_copy"] = not any(source.rglob("*.pyc"))
        primary = Path(args.primary_repo or "/").resolve()
        checks["source_is_not_primary_repo"] = (bool(args.primary_repo) and source != primary
                                                and not source.is_relative_to(primary))
        record["primary_repo"] = str(primary)
    record["guard_violations"] = state["violations"]
    record["allowed_spawns"] = state["allowed_spawns"]
    checks["no_unexpected_guard_violations"] = not [v for v in state["violations"]
                                                    if not v["phase"].startswith("sentinel:")]
    record["checks"] = checks
    record["pass"] = all(v is True for v in checks.values())
    Path(args.result_json).write_text(json.dumps(record, indent=2, sort_keys=True, default=str) + "\n")
    return 0 if record["pass"] else 1


def negative_control(baseline_path, baseline_sha256, tmp):
    """A4: the original unchanged-replay gate must fail exactly the new refusal assertions."""
    import importlib.util
    import unittest
    from campaign_tool.records.intake import mail_delta as candidate
    from tests.records import test_fa53_replay_preservation as fa53
    name = "campaign_tool.records.intake.fa53_original_mail_delta"
    spec = importlib.util.spec_from_file_location(name, baseline_path)
    baseline = importlib.util.module_from_spec(spec)
    sys.modules[name] = baseline
    spec.loader.exec_module(baseline)

    def run_against(module, label):
        suite = unittest.TestSuite()
        for attr in sorted(vars(fa53)):
            cls = getattr(fa53, attr)
            if isinstance(cls, type) and issubclass(cls, unittest.TestCase) and attr.startswith("A"):
                bound = type(attr + "_" + label, (cls,), {"MODULE": module, "__module__": fa53.__name__})
                suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(bound))
        result = unittest.TextTestRunner(stream=sys.stderr, verbosity=2).run(suite)
        record = unittest_record(result)
        record["failed_methods"] = sorted(t._testMethodName for t, _ in result.failures)
        record["clean"] = clean(result)
        return record

    base_run = run_against(baseline, "original")
    cand_run = run_against(candidate, "candidate")
    base_out = fa53.missing_declaration_outcomes(baseline, tmp / "nc-original")
    cand_out = fa53.missing_declaration_outcomes(candidate, tmp / "nc-candidate")
    checks = {
        "baseline_matches_recorded_original": sha256_file(baseline_path) == baseline_sha256,
        "baseline_is_not_candidate": Path(baseline.__file__).read_bytes() != Path(candidate.__file__).read_bytes(),
        "original_fails_exactly_refusal_assertions": base_run["failed_methods"] == sorted(BASELINE_EXPECTED_FAILURES)
        and base_run["errors"] == 0 and base_run["skipped"] == 0,
        "original_passes_every_other_assertion": base_run["tests_run"] - base_run["failures"] > 0,
        "original_incorrectly_replays_missing_declarations": all(
            base_out[case][0] == "replay" for case in fa53.MISSING_CASES),
        "candidate_refuses_missing_declarations": all(
            cand_out[case] == ["rejected", fa53.EXPECTED_REFUSAL] for case in fa53.MISSING_CASES),
        "fully_declared_replays_on_both": base_out["fully_declared"][0] == "replay"
        and cand_out["fully_declared"][0] == "replay",
        "candidate_suite_clean": cand_run["clean"] and cand_run["tests_run"] == base_run["tests_run"],
    }
    return {"original_run": base_run, "candidate_run": cand_run, "original_outcomes": base_out,
            "candidate_outcomes": cand_out, "checks_a4": checks, "tests_run": base_run["tests_run"] +
            cand_run["tests_run"], "skipped": base_run["skipped"] + cand_run["skipped"],
            "pass": all(checks.values())}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--case", choices=CASES)
    parser.add_argument("--output-parent")
    parser.add_argument("--child", choices=("unittest", "negative-control", "isolation"))
    parser.add_argument("--run-dir")
    parser.add_argument("--source-root")
    parser.add_argument("--result-json")
    parser.add_argument("--module", action="append", default=[])
    parser.add_argument("--baseline")
    parser.add_argument("--baseline-sha256")
    parser.add_argument("--primary-repo")
    args = parser.parse_args(argv)
    if args.child:
        return child_main(args)
    if not args.case or not args.output_parent:
        parser.error("--case and --output-parent are required")
    return parent_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
