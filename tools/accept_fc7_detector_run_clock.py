"""Acceptance runner for FC7-DETECTOR-RUN-CLOCK (runs.started_at admission in persist_evaluation).

Usage (each check is a separate invocation with its own fresh evidence directory):

    <python3> tools/accept_fc7_detector_run_clock.py --check {focused,compatibility,negative,
        portability,credentials,public-tree} --output-root <dir outside the repo>

Every path is derived from __file__ and the staged SOURCE-MANIFEST.json in the repo parent.
Children are spawned with literal, shell-free argv. Synthetic, offline, in-memory only.
A zero exit means the named machine contract held: REVIEW REQUIRED, never semantic, legal,
human, stage, live or publication acceptance.
"""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

CARD = "FC7-DETECTOR-RUN-CLOCK"
REPO = Path(__file__).resolve().parents[1]
CANDIDATE = REPO.parent
MANIFEST_PATH = CANDIDATE / "SOURCE-MANIFEST.json"
CHILD_ENV = "FC7_RUN_CLOCK_CHILD_SPEC"
CHECKS = ("focused", "compatibility", "negative", "portability", "credentials", "public-tree")
CHANGED = (
    "campaign_tool/records/detectors/persistence.py",
    "tests/records/test_fc7_detector_run_clock.py",
    "tools/accept_fc7_detector_run_clock.py",
)
FOCUSED = "tests.records.test_fc7_detector_run_clock"
COMPATIBILITY = "tests.records.test_detector_persistence"
CASES = {
    "valid_z_offset_and_fractional_times": "test_valid_z_offset_and_fractional_times",
    "offset_minute_60_99_and_hour_24": "test_offset_minute_60_99_and_hour_24",
    "naive_null_nonstring_and_calendar_errors": "test_naive_null_nonstring_and_calendar_errors",
    "no_migration_or_evidence_write_on_invalid": "test_no_migration_or_evidence_write_on_invalid",
    "valid_replay_idempotence": "test_valid_replay_idempotence",
    "future_run_rule_and_publication_flag": "test_future_run_rule_and_publication_flag",
}
FOCUSED_CLASS = FOCUSED + ".RunClockTests"
NETWORK_MODULES = ("socket", "ssl", "http", "urllib", "requests", "httpx", "aiohttp", "smtplib",
                   "imaplib", "poplib", "ftplib", "telnetlib", "subprocess", "ollama", "openai",
                   "anthropic")
BLOCKED_IMPORTS = ("requests", "httpx", "aiohttp", "ollama", "openai", "anthropic", "smtplib",
                   "imaplib", "poplib", "ftplib")
CONTRACT = {
    "card": CARD, "checks": CHECKS, "changed_paths": CHANGED, "focused_selector": FOCUSED,
    "compatibility_selector": COMPATIBILITY, "case_ids": CASES,
    "error": "run_started_at_invalid", "negative_deciding_value": "2020-01-20T00:00:00+00:99",
    "secret_scan_args": ["scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"],
}


class Refused(Exception):
    """An explicit acceptance failure (fail closed)."""


def fail(reason):
    raise Refused(reason)


def check(condition, reason):
    if not condition:
        fail(reason)


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def contract_sha256():
    return hashlib.sha256(canonical(CONTRACT).encode()).hexdigest()


def run(argv, cwd, run_dir, label, env=None, timeout=1800):
    """Literal shell-free child; raw stdout/stderr are saved beneath run_dir."""
    started = time.time()
    proc = subprocess.run([str(part) for part in argv], cwd=str(cwd), capture_output=True,
                          text=True, env=env, timeout=timeout, shell=False)
    out_path, err_path = run_dir / f"{label}.stdout.txt", run_dir / f"{label}.stderr.txt"
    out_path.write_text(proc.stdout)
    err_path.write_text(proc.stderr)
    return {"label": label, "argv": [str(part) for part in argv], "cwd": str(cwd),
            "exit": proc.returncode, "stdout_path": str(out_path), "stderr_path": str(err_path),
            "stdout_sha256": sha256_file(out_path), "stderr_sha256": sha256_file(err_path),
            "seconds": round(time.time() - started, 3), "stdout": proc.stdout, "stderr": proc.stderr}


def git(*args):
    proc = subprocess.run(["git", *args], cwd=str(REPO), capture_output=True, text=True, shell=False)
    check(proc.returncode == 0, f"git_failed: {' '.join(args)}: {proc.stderr.strip()}")
    return proc.stdout


# --------------------------------------------------------------------------- admission
def load_manifest():
    check(MANIFEST_PATH.is_file(), "source_manifest_missing")
    manifest = json.loads(MANIFEST_PATH.read_text())
    check(manifest.get("card") == CARD, "source_manifest_card_mismatch")
    check(Path(manifest.get("repo", "")).resolve() == REPO, "source_manifest_repo_mismatch")
    check(Path(manifest.get("write_root", "")).resolve() == CANDIDATE, "source_manifest_write_root_mismatch")
    remote = manifest.get("remote")
    check(isinstance(remote, str) and remote, "source_manifest_remote_missing")
    check(git("remote", "get-url", "origin").strip() == remote, "source_manifest_remote_mismatch")
    base = manifest.get("head")
    check(isinstance(base, str) and len(base) == 40, "source_manifest_head_missing")
    git("cat-file", "-e", base + "^{commit}")
    check(git("rev-parse", base + "^{tree}").strip() == manifest.get("tree"), "source_manifest_tree_mismatch")
    ancestor = subprocess.run(["git", "merge-base", "--is-ancestor", base, "HEAD"], cwd=str(REPO), shell=False)
    check(ancestor.returncode == 0, "candidate_not_descended_from_base")
    runtime = manifest.get("runtime")
    check(isinstance(runtime, dict), "source_manifest_runtime_missing")
    venv = Path(runtime.get("repo_venv", ""))
    check(venv.is_dir() and (REPO / ".venv").resolve() == venv.resolve(), "repo_venv_mismatch")
    return manifest


def candidate_state(manifest):
    base = manifest["head"]
    head = git("rev-parse", "HEAD").strip()
    changed = sorted(set(filter(None, git("diff", "--name-only", base).splitlines())))
    untracked = sorted(path for path in git("ls-files", "--others", "--exclude-standard").splitlines()
                       if path and path != ".venv")
    outside = [path for path in changed + untracked if path not in CHANGED]
    check(not outside, "write_set_violation: " + ",".join(outside))
    hashes = {path: (sha256_file(REPO / path) if (REPO / path).is_file() else None) for path in CHANGED}
    for path in CHANGED:
        check(hashes[path] is not None, "changed_file_missing: " + path)
    base_target = subprocess.run(["git", "show", f"{base}:{CHANGED[0]}"], cwd=str(REPO),
                                 capture_output=True, shell=False)
    check(base_target.returncode == 0, "base_target_unreadable")
    return {"base_head": base, "candidate_head": head, "diff_vs_base": changed, "untracked": untracked,
            "status_porcelain": git("status", "--porcelain"), "changed_sha256": hashes,
            "base_target_sha256": hashlib.sha256(base_target.stdout).hexdigest()}


def require_interpreter(manifest, check_name):
    runtime = manifest["runtime"]
    if check_name == "credentials":
        check(sys.executable == runtime.get("credential_scan_python3"), "credential_interpreter_mismatch")
    else:
        check(sys.executable == str(REPO / ".venv" / "bin" / "python3"), "repo_interpreter_mismatch")
    check(Path(sys.prefix).resolve() == Path(runtime["repo_venv"]).resolve(), "interpreter_prefix_mismatch")


def fresh_run_dir(output_root, check_name):
    root = Path(output_root).resolve()
    check(root != REPO and REPO not in root.parents, "output_root_inside_repo")
    check(root == CANDIDATE or CANDIDATE in root.parents, "output_root_outside_candidate")
    root.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    run_dir = root / f"{check_name}-{stamp}-{os.getpid()}"
    run_dir.mkdir(exist_ok=False)
    (run_dir / "tmp").mkdir()
    return run_dir


# --------------------------------------------------------------------------- closure
def module_file(source_root, name):
    base = source_root.joinpath(*name.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def inspect_closure(source_root, selectors):
    """Static import closure of the selected test modules inside source_root, before execution."""
    pending, seen, external, risky = list(selectors), {}, set(), []
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        path = module_file(source_root, name)
        seen[name] = None if path is None else str(path)
        if path is None:
            continue
        package = name if path.name == "__init__.py" else name.rpartition(".")[0]
        parts = name.split(".")
        for index in range(1, len(parts)):
            parent = ".".join(parts[:index])
            if parent not in seen:
                pending.append(parent)
        tree = ast.parse(path.read_text(), str(path))
        for node in ast.walk(tree):
            targets = []
            if isinstance(node, ast.Import):
                targets = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    anchor = package.split(".")
                    anchor = anchor[:len(anchor) - (node.level - 1)] if node.level > 1 else anchor
                    stem = ".".join(anchor + ([node.module] if node.module else []))
                else:
                    stem = node.module or ""
                targets = [stem] + [f"{stem}.{alias.name}" for alias in node.names]
            for target in targets:
                top = target.split(".")[0]
                if top in ("campaign_tool", "tests"):
                    if module_file(source_root, target) is not None:
                        pending.append(target)
                elif top:
                    external.add(top)
                    if top in NETWORK_MODULES:
                        risky.append({"module": name, "imports": target})
    selected_risky = [row for row in risky if row["module"] in selectors]
    check(not selected_risky, "selected_test_module_imports_network_or_subprocess")
    missing = [name for name in selectors if seen.get(name) is None]
    check(not missing, "selected_module_missing: " + ",".join(missing))
    return {"modules": {k: v for k, v in sorted(seen.items()) if v}, "external_imports": sorted(external),
            "network_or_subprocess_imports_in_closure": risky,
            "runtime_policy": "socket, sqlite3 file paths, subprocess and model/network imports are "
                              "sentinel-blocked in the test child before application imports"}


# --------------------------------------------------------------------------- child
def child_env(run_dir):
    env = {key: value for key, value in os.environ.items() if key in ("LANG", "LC_ALL", "USER", "LOGNAME")}
    tmp = str(run_dir / "tmp")
    home = run_dir / "home"
    home.mkdir(exist_ok=True)
    # HOME/XDG point into the run dir so import-time home-derived defaults cannot reach real stores.
    env.update({"TMPDIR": tmp, "TMP": tmp, "TEMP": tmp, "HOME": str(home), "XDG_CONFIG_HOME": str(home),
                "XDG_DATA_HOME": str(home), "XDG_CACHE_HOME": str(home), "XDG_STATE_HOME": str(home),
                "PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": "0", "NO_PROXY": "*"})
    return env


def spawn_child(run_dir, label, source_root, selectors, mode, closure):
    spec_path = run_dir / f"{label}.spec.json"
    result_path = run_dir / f"{label}.result.json"
    spec = {"source_root": str(source_root), "selectors": list(selectors), "run_dir": str(run_dir),
            "mode": mode, "result_path": str(result_path), "closure": sorted(closure["modules"])}
    spec_path.write_text(json.dumps(spec, indent=2))
    env = child_env(run_dir)
    env[CHILD_ENV] = str(spec_path)
    runner = source_root / "tools" / Path(__file__).name
    record = run([sys.executable, runner, "--check", "focused", "--output-root", str(run_dir)],
                 source_root, run_dir, label, env=env)
    check(result_path.is_file(), f"{label}: child_result_missing (exit {record['exit']})")
    record["result"] = json.loads(result_path.read_text())
    return record


def install_sentinels(run_dir, source_root, events):
    import builtins
    import socket
    import sqlite3

    def refuse(kind):
        def blocked(*args, **kwargs):
            events.append(kind)
            raise PermissionError("fc7 sentinel: " + kind)
        return blocked

    for attr in ("connect", "connect_ex", "bind", "sendto", "send", "sendall"):
        setattr(socket.socket, attr, refuse("socket." + attr))
    socket.create_connection = refuse("socket.create_connection")
    socket.getaddrinfo = refuse("socket.getaddrinfo")
    subprocess.Popen = refuse("subprocess.Popen")
    os.system = refuse("os.system")
    for name in ("execv", "execve", "execvp", "execvpe", "spawnv", "spawnve", "posix_spawn", "fork"):
        if hasattr(os, name):
            setattr(os, name, refuse("os." + name))
    allowed_root = Path(run_dir).resolve()
    real_connect = sqlite3.connect

    def guarded_connect(database, *args, **kwargs):
        text = os.fspath(database) if not isinstance(database, str) else database
        if text != ":memory:":
            target = Path(text).resolve()
            if allowed_root not in target.parents:
                events.append("sqlite3.connect:" + text)
                raise PermissionError("fc7 sentinel: database path outside run dir")
        if kwargs.get("uri"):
            events.append("sqlite3.connect:uri")
            raise PermissionError("fc7 sentinel: sqlite uri refused")
        return real_connect(database, *args, **kwargs)

    sqlite3.connect = guarded_connect
    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name.split(".")[0] in BLOCKED_IMPORTS:
            events.append("import:" + name)
            raise ImportError("fc7 sentinel: network/model import refused: " + name)
        return real_import(name, *args, **kwargs)

    builtins.__import__ = guarded_import
    tempfile.tempdir = str(Path(run_dir) / "tmp")


def collect(suite):
    import unittest
    flat = []
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            flat.extend(collect(item))
        else:
            flat.append(item)
    return flat


def make_result_class():
    import unittest

    class Recorder(unittest.TestResult):
        def __init__(self):
            super().__init__()
            self.outcomes = {}

        def _mark(self, test, outcome, detail=None):
            key = test.id()
            row = self.outcomes.setdefault(key, {"outcome": "pass", "details": []})
            if outcome != "pass":
                row["outcome"] = outcome if row["outcome"] in ("pass", outcome) else "fail"
            if detail:
                row["details"].append(detail[-4000:])

        def startTest(self, test):
            super().startTest(test)
            self.outcomes.setdefault(test.id(), {"outcome": "pass", "details": []})

        def addError(self, test, err):
            super().addError(test, err)
            self._mark(test, "error", self._exc_info_to_string(err, test))

        def addFailure(self, test, err):
            super().addFailure(test, err)
            self._mark(test, "fail", self._exc_info_to_string(err, test))

        def addSkip(self, test, reason):
            super().addSkip(test, reason)
            self._mark(test, "skip", reason)

        def addExpectedFailure(self, test, err):
            super().addExpectedFailure(test, err)
            self._mark(test, "expected_failure", self._exc_info_to_string(err, test))

        def addUnexpectedSuccess(self, test):
            super().addUnexpectedSuccess(test)
            self._mark(test, "unexpected_success")

        def addSubTest(self, test, subtest, err):
            super().addSubTest(test, subtest, err)
            if err is not None:
                kind = "fail" if issubclass(err[0], test.failureException) else "error"
                self._mark(test, kind, subtest.id() + "\n" + self._exc_info_to_string(err, test))

    return Recorder


def run_suite(names):
    import unittest
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromNames(names)
    tests = collect(suite)
    recorder = make_result_class()()
    suite.run(recorder)
    return {"load_errors": list(loader.errors), "selected": [test.id() for test in tests],
            "count": len(tests), "outcomes": recorder.outcomes, "tests_run": recorder.testsRun,
            "failures": len(recorder.failures), "errors": len(recorder.errors),
            "skipped": len(recorder.skipped), "expected_failures": len(recorder.expectedFailures),
            "unexpected_successes": len(recorder.unexpectedSuccesses),
            "was_successful": recorder.wasSuccessful() and not loader.errors}


def deciding_probe(value):
    """One synthetic run whose only invalid binding is runs.started_at; returns the observed outcome."""
    from campaign_tool.records.detectors.persistence import PersistenceError
    module = sys.modules[FOCUSED]
    case = module.RunClockTests(CASES["valid_replay_idempotence"])
    try:
        case.setUp()
        case.set_started_at(value)
        try:
            receipt = case.persist()
        except PersistenceError as exc:
            return {"value": value, "raised": "PersistenceError", "message": str(exc),
                    "detector_runs": case.count("detector_runs")}
        return {"value": value, "raised": None, "replayed": receipt["replayed"],
                "detector_runs": case.count("detector_runs"),
                "persisted_started_at": case.connection.execute(
                    "SELECT started_at FROM detector_runs").fetchone()[0]}
    finally:
        case.doCleanups()


def sentinel_self_test(run_dir, events):
    """Prove the guards are live before any application import (not a hostile-code sandbox)."""
    import socket
    import sqlite3
    outcome = {}
    probe_db = Path(run_dir).parent / "fc7-sentinel-probe-must-not-exist.db"
    attempts = (("socket", lambda: socket.create_connection(("127.0.0.1", 9), timeout=1)),
                ("sqlite_path", lambda: sqlite3.connect(str(probe_db))),
                ("subprocess", lambda: subprocess.run(["true"])),
                ("model_import", lambda: __import__("openai")))
    for name, attempt in attempts:
        try:
            attempt()
            outcome[name] = "NOT_BLOCKED"
        except (PermissionError, ImportError) as exc:
            outcome[name] = "blocked: " + str(exc)
    outcome["probe_db_absent"] = not probe_db.exists()
    outcome["events"] = list(events)
    return outcome


def child_main(spec_path):
    spec = json.loads(Path(spec_path).read_text())
    events = []
    source_root = Path(spec["source_root"]).resolve()
    run_dir = Path(spec["run_dir"]).resolve()
    install_sentinels(run_dir, source_root, events)
    result_probe = sentinel_self_test(run_dir, events)
    events.clear()
    sys.path[:] = [str(source_root)] + [entry for entry in sys.path
                                        if entry and Path(entry).resolve() != REPO and
                                        Path(entry).resolve() != source_root]
    sys.dont_write_bytecode = True
    result = {"mode": spec["mode"], "python": sys.executable, "cwd": os.getcwd(),
              "tempdir": tempfile.gettempdir(), "sentinel_self_test": result_probe}
    if spec["mode"] == "negative":
        from campaign_tool.records.detectors import core, persistence
        __import__(FOCUSED)
        strict = persistence._run_started_at
        value = CONTRACT["negative_deciding_value"]
        result["control_valid_strict"] = deciding_probe("2020-01-20T00:00:00+00:00")
        persistence._run_started_at = core.moment  # in-memory only: the prior permissive parser
        try:
            result["mutant_probe"] = deciding_probe(value)
            result["mutant_suite"] = run_suite([f"{FOCUSED_CLASS}.{CASES['offset_minute_60_99_and_hour_24']}"])
            result["mutant_control_valid"] = deciding_probe("2020-01-20T00:00:00+00:00")
        finally:
            persistence._run_started_at = strict
        result["restored_is_strict"] = persistence._run_started_at is strict
        result["restored_probe"] = deciding_probe(value)
        result["restored_suite"] = run_suite([f"{FOCUSED_CLASS}.{CASES['offset_minute_60_99_and_hour_24']}",
                                              f"{FOCUSED_CLASS}.{CASES['valid_replay_idempotence']}"])
    else:
        result["suite"] = run_suite(spec["selectors"])
    loaded = {}
    for name, module in sorted(sys.modules.items()):
        path = getattr(module, "__file__", None)
        if path and name.split(".")[0] in ("campaign_tool", "tests"):
            loaded[name] = str(Path(path).resolve())
    result["loaded_project_modules"] = loaded
    result["sentinel_events"] = events
    Path(spec["result_path"]).write_text(json.dumps(result, indent=2, sort_keys=True))
    return 0


# --------------------------------------------------------------------------- suite judgement
def judge_suite(suite, label, expect_ids=None):
    check(not suite["load_errors"], f"{label}: load_errors")
    check(suite["count"] > 0 and suite["tests_run"] == suite["count"], f"{label}: zero_or_partial_selection")
    check(suite["skipped"] == 0, f"{label}: unqualified_skip")
    check(suite["expected_failures"] == 0 and suite["unexpected_successes"] == 0, f"{label}: xfail_markers")
    bad = {key: row["outcome"] for key, row in suite["outcomes"].items() if row["outcome"] != "pass"}
    check(not bad and suite["failures"] == 0 and suite["errors"] == 0, f"{label}: failing_tests {bad}")
    check(suite["was_successful"], f"{label}: unsuccessful")
    if expect_ids is not None:
        check(sorted(suite["selected"]) == sorted(expect_ids), f"{label}: selection_mismatch")


def focused_ids():
    return [f"{FOCUSED_CLASS}.{method}" for method in CASES.values()]


def judge_child(record, source_root):
    check(record["exit"] == 0, f"{record['label']}: child_exit_{record['exit']}")
    result = record["result"]
    probe = result["sentinel_self_test"]
    check(probe["probe_db_absent"] and all(str(probe[name]).startswith("blocked: ")
                                           for name in ("socket", "sqlite_path", "subprocess", "model_import")),
          f"{record['label']}: sentinel_self_test_failed {probe}")
    check(not result["sentinel_events"], f"{record['label']}: sentinel_tripped {result['sentinel_events']}")
    for name, path in result["loaded_project_modules"].items():
        check(Path(path).resolve().is_relative_to(source_root), f"{record['label']}: foreign_module {name}")


# --------------------------------------------------------------------------- checks
def check_focused(run_dir, evidence):
    closure = inspect_closure(REPO, [FOCUSED])
    evidence["closure"] = closure
    record = spawn_child(run_dir, "focused", REPO, [FOCUSED], "suite", closure)
    evidence["children"].append(record)
    judge_child(record, REPO)
    suite = record["result"]["suite"]
    judge_suite(suite, "focused", focused_ids())
    evidence["case_map"] = {case: f"{FOCUSED_CLASS}.{method}" for case, method in CASES.items()}
    evidence["selected_tests"] = suite["selected"]


def extract_base(run_dir, state):
    target = run_dir / "base-src" / "repo"
    target.mkdir(parents=True)
    archive = run_dir / "base.tar"
    with archive.open("wb") as handle:
        proc = subprocess.run(["git", "archive", "--format=tar", state["base_head"]], cwd=str(REPO),
                              stdout=handle, stderr=subprocess.PIPE, shell=False)
    check(proc.returncode == 0, "git_archive_failed")
    with tarfile.open(archive) as tar:
        tar.extractall(target, filter="data")
    check(sha256_file(target / CHANGED[0]) == state["base_target_sha256"], "base_extract_mismatch")
    for path in CHANGED[1:]:
        check(not (target / path).exists(), "base_already_contains_" + path)
    # The runner itself is candidate material; the baseline child uses an exact copy of it.
    (target / "tools").mkdir(exist_ok=True)
    shutil.copy2(REPO / CHANGED[2], target / CHANGED[2])
    return target


def check_compatibility(run_dir, evidence):
    base_root = extract_base(run_dir, evidence["state"])
    results = {}
    for label, root in (("compatibility-base", base_root), ("compatibility-candidate", REPO)):
        closure = inspect_closure(root, [COMPATIBILITY])
        evidence.setdefault("closures", {})[label] = closure
        record = spawn_child(run_dir, label, root, [COMPATIBILITY], "suite", closure)
        evidence["children"].append(record)
        judge_child(record, root)
        judge_suite(record["result"]["suite"], label)
        results[label] = record["result"]["suite"]
    before, after = results["compatibility-base"], results["compatibility-candidate"]
    check(before["selected"] == after["selected"], "compatibility_selection_changed")
    evidence["compatibility"] = {"base_count": before["count"], "candidate_count": after["count"],
                                 "selected_tests": after["selected"]}


def check_negative(run_dir, evidence):
    closure = inspect_closure(REPO, [FOCUSED])
    evidence["closure"] = closure
    record = spawn_child(run_dir, "negative", REPO, [FOCUSED], "negative", closure)
    evidence["children"].append(record)
    judge_child(record, REPO)
    result, value = record["result"], CONTRACT["negative_deciding_value"]
    control = result["control_valid_strict"]
    check(control["raised"] is None and control["detector_runs"] == 1, "negative: valid_bindings_control_failed")
    mutant_control = result["mutant_control_valid"]
    check(mutant_control["raised"] is None, "negative: mutant_rejects_valid_run (bindings not isolated)")
    mutant = result["mutant_probe"]
    check(mutant["raised"] is None and mutant.get("persisted_started_at") == value,
          "negative: mutant_not_detected_by_probe")
    suite = result["mutant_suite"]
    deciding = f"{FOCUSED_CLASS}.{CASES['offset_minute_60_99_and_hour_24']}"
    check(suite["count"] == 1 and suite["outcomes"].get(deciding, {}).get("outcome") == "fail",
          "negative: deciding_assertion_did_not_fail")
    check(any(value[-6:] in detail for detail in suite["outcomes"][deciding]["details"]),
          "negative: +00:99 subtest failure not observed")
    check(result["restored_is_strict"], "negative: strict_check_not_restored")
    restored = result["restored_probe"]
    check(restored["raised"] == "PersistenceError" and restored["message"] == CONTRACT["error"]
          and restored["detector_runs"] == 0, "negative: restored_rejection_failed")
    judge_suite(result["restored_suite"], "negative-restored",
                [deciding, f"{FOCUSED_CLASS}.{CASES['valid_replay_idempotence']}"])
    evidence["negative"] = {"mutant_probe": mutant, "mutant_failure": suite["outcomes"][deciding],
                            "restored_probe": restored, "control": control}


def copy_tree(run_dir):
    files = [path for path in git("ls-files", "-z", "--cached", "--others", "--exclude-standard").split("\0")
             if path and path != ".venv"]
    target = run_dir / "second-prefix" / "copy" / "repo"
    for relative in files:
        source = REPO / relative
        if source.is_symlink() or not source.is_file():
            continue
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    for path in CHANGED:
        check(sha256_file(target / path) == sha256_file(REPO / path), "copy_mismatch_" + path)
    return target


def check_portability(run_dir, evidence):
    box_markers = [str(REPO), str(CANDIDATE), str(Path.home()) + os.sep]
    for path in CHANGED[1:]:
        text = (REPO / path).read_text()
        hits = [marker for marker in box_markers if marker in text]
        check(not hits, f"portability: absolute box constant in {path}: {hits}")
    copy_root = copy_tree(run_dir)
    check(str(copy_root) != str(REPO) and not str(copy_root).startswith(str(REPO) + os.sep),
          "portability: copy prefix not distinct")
    records = {}
    for label, root in (("portability-original", REPO), ("portability-copy", copy_root)):
        closure = inspect_closure(root, [FOCUSED])
        evidence.setdefault("closures", {})[label] = closure
        record = spawn_child(run_dir, label, root, [FOCUSED], "suite", closure)
        evidence["children"].append(record)
        judge_child(record, root)
        judge_suite(record["result"]["suite"], label, focused_ids())
        records[label] = record
    copied = records["portability-copy"]["result"]["loaded_project_modules"]
    check(copied and all(Path(p).is_relative_to(copy_root) for p in copied.values()),
          "portability: copy loaded modules outside its prefix")
    check(records["portability-copy"]["result"]["tempdir"].startswith(str(run_dir)),
          "portability: tempdir not redirected")
    evidence["portability"] = {"copy_root": str(copy_root),
                               "copy_loaded_modules": copied,
                               "original_loaded_modules": records["portability-original"]["result"]["loaded_project_modules"]}


def check_credentials(run_dir, evidence, manifest):
    detect = Path(manifest["runtime"].get("detect_secrets", ""))
    check(detect.is_file() and os.access(detect, os.X_OK), "detect_secrets_unavailable")
    scan = run([detect, *CONTRACT["secret_scan_args"]], REPO, run_dir, "secret-scan")
    evidence["children"].append(scan)
    check(scan["exit"] == 0, "secret_scan_failed")
    report = run_dir / "secret-scan.json"
    report.write_text(scan["stdout"])
    parsed = json.loads(scan["stdout"])
    check(isinstance(parsed.get("results"), dict), "secret_scan_report_invalid")
    checker = run([sys.executable, REPO / "tools" / "check_secret_report.py", report], REPO, run_dir,
                  "check-secret-report")
    evidence["children"].append(checker)
    check(checker["exit"] == 0, "secret_report_checker_failed")
    flagged = {path: len(hits) for path, hits in parsed["results"].items()}
    for path in CHANGED:
        check(path not in flagged, "secret_scan_hit_in_changed_file_" + path)
    evidence["credentials"] = {"report": str(report), "report_sha256": sha256_file(report),
                               "flagged_files": flagged, "detect_secrets": str(detect)}


def check_public_tree(run_dir, evidence):
    record = run([sys.executable, REPO / "tools" / "check_public_tree.py"], REPO, run_dir, "public-tree")
    evidence["children"].append(record)
    check(record["exit"] == 0, "public_tree_failed")


def main(argv=None):
    if os.environ.get(CHILD_ENV):
        return child_main(os.environ[CHILD_ENV])
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", required=True, choices=CHECKS)
    parser.add_argument("--output-root", required=True)
    args = parser.parse_args(argv)
    run_dir = fresh_run_dir(args.output_root, args.check)
    evidence = {"card": CARD, "check": args.check, "argv": [sys.executable, *sys.argv], "cwd": os.getcwd(),
                "python": sys.executable, "python_version": sys.version, "run_dir": str(run_dir),
                "contract_sha256": contract_sha256(), "contract": CONTRACT, "children": [],
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "verdict": "REVIEW REQUIRED (machine contract only; not semantic, legal, human, "
                           "stage, live or publication acceptance)",
                "full_discovery": "operator-run (OWED, not PASS)"}
    status = 1
    try:
        manifest = load_manifest()
        require_interpreter(manifest, args.check)
        evidence["manifest_sha256"] = sha256_file(MANIFEST_PATH)
        evidence["state"] = candidate_state(manifest)
        if args.check == "focused":
            check_focused(run_dir, evidence)
        elif args.check == "compatibility":
            check_compatibility(run_dir, evidence)
        elif args.check == "negative":
            check_negative(run_dir, evidence)
        elif args.check == "portability":
            check_portability(run_dir, evidence)
        elif args.check == "credentials":
            check_credentials(run_dir, evidence, manifest)
        else:
            check_public_tree(run_dir, evidence)
        evidence["state_after"] = candidate_state(manifest)
        check(evidence["state_after"]["changed_sha256"] == evidence["state"]["changed_sha256"],
              "candidate_changed_during_run")
        evidence["result"] = "PASS"
        status = 0
    except Refused as exc:
        evidence["result"] = "FAIL"
        evidence["failure"] = str(exc)
    except Exception as exc:  # unavailable prerequisite or crash: fail closed with the reason
        evidence["result"] = "FAIL"
        evidence["failure"] = f"{type(exc).__name__}: {exc}"
    for child in evidence["children"]:
        child.pop("stdout", None)
        child.pop("stderr", None)
    evidence["exit"] = status
    evidence["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    (run_dir / "evidence.json").write_text(json.dumps(evidence, indent=2, sort_keys=True, default=str))
    print(json.dumps({"check": args.check, "result": evidence["result"], "failure": evidence.get("failure"),
                      "run_dir": str(run_dir), "candidate_head": evidence.get("state", {}).get("candidate_head")}))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
