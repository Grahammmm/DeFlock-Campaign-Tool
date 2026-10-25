#!/usr/bin/env python3
"""FC3-CHUNK-IDENTITY acceptance runner (offline, synthetic, review-required).

Usage: accept_fc3_chunk_identity.py --check MODE --output-root DIR
Modes: focused, compatibility, negative, portability, credentials, public-tree.

Each invocation creates a new exclusive run directory under --output-root
(which must lie outside this repository), pins the git identity and the
hashes of the changed files, and writes result.json with raw outputs and
their hashes. Exit 0 only when every required assertion of that mode holds.
A green result is machine-valid output that still needs independent review;
it is never semantic, legal, stage or release acceptance.

Network, model-client, listener and process sentinels are installed before
any application import. Only the enumerated local children below may run,
always as argument arrays without a shell.
"""

import argparse
import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import socket
import ssl  # imported before sentinels so stdlib SSL classes stay importable
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.dont_write_bytecode = True

REPO = Path(__file__).resolve().parents[1]
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
# Brief identity. The base is recorded as a short prefix and resolved by git.
BRIEF = {"card": "t_2a83d371", "scope": "FC3-CHUNK-IDENTITY",
         "brief_base_prefix": "9c398829", "brief_source_sha256_prefix": "21788c5d",
         "guarded_paths": ["render_chunk", "validate_checkpoint", "record_attempt"]}
CHILD_TIMEOUT = 900
BLOCKED_IMPORTS = {"openai", "anthropic", "ollama", "httpx", "requests", "urllib3", "aiohttp",
                   "litellm", "transformers", "torch", "google", "boto3", "botocore", "websockets"}

TRIPS = []
_REAL_POPEN = subprocess.Popen


class SentinelError(RuntimeError):
    pass


def _trip(kind, detail):
    TRIPS.append({"kind": kind, "detail": repr(detail)[:300]})
    raise SentinelError("offline sentinel: " + kind)


class _ImportBlocker:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in BLOCKED_IMPORTS:
            _trip("model_or_network_client_import", name)
        return None


def install_sentinels(sandbox):
    """Install network/model/process sentinels and redirect writable defaults."""
    sandbox.mkdir(parents=True, exist_ok=False)
    for name in ("HOME", "TMPDIR", "TMP", "TEMP", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME"):
        os.environ[name] = str(sandbox)
    tempfile.tempdir = str(sandbox)
    for name in ("connect", "connect_ex", "bind", "listen", "sendto"):
        setattr(socket.socket, name, lambda self, *a, _n=name, **k: _trip("socket_" + _n, a))
    socket.create_connection = lambda *a, **k: _trip("create_connection", a)
    socket.create_server = lambda *a, **k: _trip("create_server", a)
    socket.getaddrinfo = lambda *a, **k: _trip("getaddrinfo", a)
    socket.gethostbyname = lambda *a, **k: _trip("gethostbyname", a)
    ssl.SSLContext.wrap_socket = lambda self, *a, **k: _trip("ssl_wrap_socket", a)
    ssl.SSLContext.wrap_bio = lambda self, *a, **k: _trip("ssl_wrap_bio", a)
    subprocess.Popen = lambda *a, **k: _trip("unexpected_subprocess", a)
    for name in ("system", "popen", "fork", "forkpty", "posix_spawn", "posix_spawnp",
                 "spawnv", "spawnve", "spawnl", "spawnle", "spawnvp", "spawnvpe",
                 "execv", "execve", "execl", "execle", "execvp", "execvpe", "execlp", "execlpe"):
        if hasattr(os, name):
            setattr(os, name, lambda *a, _n=name, **k: _trip("os_" + _n, a))
    sys.meta_path.insert(0, _ImportBlocker())


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def utc():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# Enumerated children ---------------------------------------------------------
def _git_argv(repo, *rest):
    return ["git", "-C", str(repo), *rest]


def _admitted(argv, repo):
    py = sys.executable
    exact = [
        _git_argv(repo, "rev-parse", "HEAD"),
        _git_argv(repo, "status", "--porcelain", "--untracked-files=all"),
        _git_argv(repo, "rev-parse", "--verify", BRIEF["brief_base_prefix"] + "^{commit}"),
        [py, "-B", "-W", "error::ResourceWarning", "-m", "unittest", *COMPAT_SELECTORS],
        [str(Path(py).parent / "detect-secrets"), "scan", "--all-files",
         "--exclude-files", "^.git/", "--no-verify"],
        [py, "-B", str(repo / "tools" / "check_public_tree.py")],
    ]
    if argv in exact:
        return True
    if argv[:4] == _git_argv(repo, "merge-base")[:4] and len(argv) == 7 and argv[4] == "--is-ancestor":
        return argv[6] == "HEAD"
    if argv[:4] == _git_argv(repo, "show") and len(argv) == 5:
        return argv[4].endswith(":" + CHANGED[0])
    if argv[:3] == [py, "-B", str(repo / "tools" / "check_secret_report.py")] and len(argv) == 4:
        return True
    if (len(argv) == 7 and argv[:2] == [py, "-B"] and argv[2].endswith("/tools/accept_fc3_chunk_identity.py")
            and argv[3:5] == ["--check", "focused"] and argv[5] == "--output-root"):
        return True
    return False


def run_child(argv, cwd, *, repo=REPO, stdout_bytes=False):
    if not _admitted(argv, repo):
        _trip("unenumerated_child", argv)
    proc = _REAL_POPEN(argv, cwd=str(cwd), stdin=subprocess.DEVNULL,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        out, err = proc.communicate(timeout=CHILD_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        return {"argv": argv, "cwd": str(cwd), "exit": None, "timeout": True,
                "stdout": out if stdout_bytes else out.decode("utf-8", "replace"),
                "stderr": err.decode("utf-8", "replace")}
    return {"argv": argv, "cwd": str(cwd), "exit": proc.returncode, "timeout": False,
            "stdout": out if stdout_bytes else out.decode("utf-8", "replace"),
            "stderr": err.decode("utf-8", "replace")}


def git_identity():
    if not (REPO / ".git").exists():
        return {"head": None, "head_reason": "no .git in this source copy"}
    head = run_child(_git_argv(REPO, "rev-parse", "HEAD"), REPO)
    status = run_child(_git_argv(REPO, "status", "--porcelain", "--untracked-files=all"), REPO)
    pin = run_child(_git_argv(REPO, "rev-parse", "--verify", BRIEF["brief_base_prefix"] + "^{commit}"), REPO)
    ident = {"head": head["stdout"].strip() if head["exit"] == 0 else None,
             "status_lines": status["stdout"].splitlines() if status["exit"] == 0 else None,
             "pin_resolved": pin["stdout"].strip() if pin["exit"] == 0 else None}
    ident["clean"] = ident["status_lines"] == []
    if ident["pin_resolved"]:
        anc = run_child(_git_argv(REPO, "merge-base", "--is-ancestor", ident["pin_resolved"], "HEAD"), REPO)
        ident["pin_is_ancestor_of_head"] = anc["exit"] == 0
        shown = run_child(_git_argv(REPO, "show", ident["pin_resolved"] + ":" + CHANGED[0]), REPO,
                          stdout_bytes=True)
        if shown["exit"] == 0:
            digest = hashlib.sha256(shown["stdout"]).hexdigest()
            ident["pin_semantic_coverage_sha256"] = digest
            ident["pin_source_matches_brief_prefix"] = digest.startswith(BRIEF["brief_source_sha256_prefix"])
    return ident


def source_hashes(root):
    return {rel: (sha256_file(root / rel) if (root / rel).is_file() else None) for rel in CHANGED}


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


def check_compatibility(run_dir, result):
    py = sys.executable
    child = run_child([py, "-B", "-W", "error::ResourceWarning", "-m", "unittest", *COMPAT_SELECTORS], REPO)
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


def check_portability(run_dir, result):
    problems = []
    status_before = git_identity().get("status_lines")
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
    copy_root = run_dir / "second-prefix" / "relocated-source"
    shutil.copytree(REPO / "campaign_tool", copy_root / "campaign_tool",
                    ignore=shutil.ignore_patterns("__pycache__"))
    for rel in ("tests/__init__.py", "tests/records/__init__.py", CHANGED[1], CHANGED[2]):
        (copy_root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / rel, copy_root / rel)
    child_out = run_dir / "copy-focused-out"
    argv = [sys.executable, "-B", str(copy_root / "tools" / "accept_fc3_chunk_identity.py"),
            "--check", "focused", "--output-root", str(child_out)]
    child = run_child(argv, run_dir)
    record = save_child(run_dir, "copy-focused", child)
    runs = sorted(child_out.iterdir()) if child_out.is_dir() else []
    child_result = None
    if len(runs) == 1 and (runs[0] / "result.json").is_file():
        child_result = json.loads((runs[0] / "result.json").read_text())
    record["child_run_dirs"] = [str(r) for r in runs]
    if child["exit"] != 0:
        problems.append("copy_focused_exit_nonzero")
    if child_result is None:
        problems.append("copy_result_missing")
    else:
        mods = child_result.get("focused", {}).get("loaded_modules", {})
        record["copy_loaded_modules"] = mods
        record["copy_status"] = child_result.get("status")
        record["copy_tests_run"] = child_result.get("focused", {}).get("tests_run")
        if child_result.get("status") != "PASS":
            problems.append("copy_status_not_pass")
        for name, path in mods.items():
            if not Path(path).is_relative_to(copy_root.resolve()) or Path(path).is_relative_to(REPO):
                problems.append("copy_module_not_from_copy:" + name)
        if len(mods) != 2:
            problems.append("copy_modules_incomplete")
        copied = child_result.get("source_hashes", {})
        here = source_hashes(REPO)
        if any(copied.get(rel) != here[rel] for rel in CHANGED):
            problems.append("copy_source_hash_mismatch")
    # Sentinel self-test: these must trip without any connection or process.
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
    status_after = git_identity().get("status_lines")
    record["repo_status_unchanged"] = status_before == status_after
    if status_before != status_after:
        problems.append("repo_tree_changed_during_run")
    result["portability"] = {"static_host_tokens": static, "focused_here": report, "copy": record,
                             "copy_root": str(copy_root)}
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
    scan = run_child([str(tool), "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"], REPO)
    report_path = run_dir / "secret-scan.json"
    report_path.write_text(scan["stdout"])
    (run_dir / "secret-scan.stderr").write_text(scan["stderr"])
    record = {"scan_argv": scan["argv"], "scan_cwd": scan["cwd"], "scan_exit": scan["exit"],
              "report": str(report_path), "report_sha256": sha256_file(report_path)}
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
    check = run_child([sys.executable, "-B", str(checker), str(report_path)], REPO)
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
    child = run_child([sys.executable, "-B", str(REPO / "tools" / "check_public_tree.py")], REPO)
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
    result = {"schema": "fc3-chunk-identity-acceptance/v1", "check": args.check, "brief": BRIEF,
              "argv": [sys.executable, *sys.argv], "cwd": os.getcwd(), "interpreter": sys.executable,
              "python_version": sys.version, "repo": str(REPO), "run_dir": str(run_dir),
              "started_utc": utc(), "full_discovery": "operator-run (not part of this runner)",
              "review": "REVIEW REQUIRED: machine-valid output is not semantic, legal, stage or release acceptance"}
    problems = []
    try:
        result["git"] = git_identity()
        if (REPO / ".git").exists() and not result["git"].get("head"):
            problems.append("head_unpinned")
        result["source_hashes_before"] = source_hashes(REPO)
        problems += HANDLERS[args.check](run_dir, result)
        result["source_hashes"] = source_hashes(REPO)
        if result["source_hashes"] != result["source_hashes_before"]:
            problems.append("source_changed_during_run")
        if (REPO / ".git").exists() and git_identity().get("head") != result["git"].get("head"):
            problems.append("head_changed_during_run")
    except Exception as error:
        problems.append("incomplete:" + type(error).__name__)
        result["error"] = repr(error)[:2000]
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
    raise SystemExit(main())
