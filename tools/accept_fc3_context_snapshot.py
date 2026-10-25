"""FC3-CONTEXT-SNAPSHOT acceptance runner.

Usage: python3 tools/accept_fc3_context_snapshot.py --check MODE --output-root DIR

MODE is one of focused, compatibility, negative, portability, credentials,
public-tree. Each invocation creates a new exclusive run directory under DIR
(which must be outside this repository). That directory receives the receipt,
the child argv, cwd, exit codes, raw stdout/stderr and their hashes. The run
exits 0 only when every gate for that mode passes.

A passing run is machine-valid evidence and is REVIEW REQUIRED. It is never
semantic, legal, stage or release acceptance. Full repository discovery is
run by the operator and is not part of this runner.

Children are started only from enumerated argument arrays, never through a
shell: this script itself (as a test child), the repository credential
checker, the repository public-tree checker, the detect-secrets executable
installed next to this interpreter, and read-only git queries.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import traceback

sys.dont_write_bytecode = True

REPO = Path(__file__).resolve().parents[1]
RUNNER_REL = "tools/accept_fc3_context_snapshot.py"
TEST_REL = "tests/records/test_fc3_context_snapshot.py"
PRODUCTION_REL = "campaign_tool/records/context_revision.py"
ALLOWED_CHANGES = (PRODUCTION_REL, TEST_REL, RUNNER_REL)
CARD = "t_cf694e2f"
SCOPE = "FC3-CONTEXT-SNAPSHOT"
BRIEF_NAME = "deflock-FC3-CONTEXT-SNAPSHOT.brief"
CHECKS = ("focused", "compatibility", "negative", "portability", "credentials", "public-tree")
FOCUSED_MODULE = "tests.records.test_fc3_context_snapshot"
FOCUSED_CLASS = "FC3ContextSnapshotTests"
CASES = (
    "source_bound_positive_equal_values",
    "mutate_result_nested_evidence",
    "mutate_context_after_return",
    "independent_repeated_results",
    "multiple_compare_rows_detached",
    "excluded_and_unresolved_controls",
    "revision_hash_unchanged",
    "inputs_unchanged_during_call",
)
COMPATIBILITY_SELECTORS = (
    "tests.records.test_context_revision",
    "tests.records.test_context_date_boundaries",
    "tests.test_secret_report",
    "tests.test_public_tree",
)
# Test count of the compatibility selectors at the frozen base, measured before
# implementation. A different count means acceptance was narrowed or widened.
COMPATIBILITY_EXPECTED_TESTS = 43
# With event_period re-attached to the input period, these cases must FAIL ...
NEGATIVE_MUST_FAIL = (
    "mutate_result_nested_evidence",
    "mutate_context_after_return",
    "independent_repeated_results",
    "multiple_compare_rows_detached",
    "revision_hash_unchanged",
    "inputs_unchanged_during_call",
)
# ... while these controls, which do not depend on detachment, must still pass.
NEGATIVE_MUST_PASS = ("source_bound_positive_equal_values", "excluded_and_unresolved_controls")
NEGATIVE_BYPASS = ("resolve_applicability covering-result return only: the in-memory wrapper calls the real "
                   "resolver, then re-points result['event_period'] at context['dates'][event_role][0] (the "
                   "caller's input period object). Validation, unresolved/excluded returns, hashing, "
                   "classification and compare_applicability's own logic are not bypassed.")
SCAN_REPORT_NAME = "secret-scan.json"
MODEL_ENV_REDIRECTS = {
    "OLLAMA_HOST": "127.0.0.1:9",
    "OLLAMA_BASE_URL": "http://127.0.0.1:9",
    "OPENAI_BASE_URL": "http://127.0.0.1:9",
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:9",
    "HTTP_PROXY": "http://127.0.0.1:9",
    "HTTPS_PROXY": "http://127.0.0.1:9",
    "ALL_PROXY": "http://127.0.0.1:9",
    "http_proxy": "http://127.0.0.1:9",
    "https_proxy": "http://127.0.0.1:9",
    "all_proxy": "http://127.0.0.1:9",
    "NO_PROXY": "",
    "no_proxy": "",
}
DROPPED_ENV = ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE", "PYTHONINSPECT")


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    path = Path(path)
    return sha256_bytes(path.read_bytes()) if path.is_file() else None


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# ---------------------------------------------------------------- children ---

def detect_secrets_path():
    # Adjacent to the admitted interpreter; deliberately not resolved through symlinks.
    return Path(sys.executable).parent / "detect-secrets"


def allowed_parent_child(argv):
    """Enumerated children of the parent runner (shell-free argument arrays)."""
    if not argv or not all(isinstance(item, str) for item in argv):
        return False
    if argv[0] == "git":
        return len(argv) > 1 and argv[1] in ("rev-parse", "rev-list", "diff", "status", "ls-files")
    if argv[0] == str(detect_secrets_path()):
        return argv[1:] == ["scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"]
    if argv[0] == sys.executable and len(argv) > 1:
        script = Path(argv[1])
        return script.name in ("accept_fc3_context_snapshot.py", "check_secret_report.py",
                               "check_public_tree.py") and script.parent.name == "tools"
    return False


class Recorder:
    def __init__(self, run_dir):
        self.run_dir = run_dir
        self.children = []
        self.git_calls = []

    def git(self, *args):
        argv = ["git", *args]
        if not allowed_parent_child(argv):
            raise RuntimeError("unexpected git child refused: %r" % (argv,))
        proc = subprocess.run(argv, cwd=str(REPO), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              check=False, shell=False, timeout=300)
        self.git_calls.append({"argv": argv, "exit": proc.returncode})
        if proc.returncode != 0:
            raise RuntimeError("git %s failed: %s" % (" ".join(args), proc.stderr.decode("utf-8", "replace")))
        return proc.stdout.decode("utf-8")

    def child(self, label, argv, cwd, env=None, timeout=1800):
        argv = [str(item) for item in argv]
        if not allowed_parent_child(argv):
            raise RuntimeError("unexpected child refused: %r" % (argv,))
        proc = subprocess.run(argv, cwd=str(cwd), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              check=False, shell=False, timeout=timeout)
        out_path = self.run_dir / (label + ".stdout")
        err_path = self.run_dir / (label + ".stderr")
        out_path.write_bytes(proc.stdout)
        err_path.write_bytes(proc.stderr)
        record = {"label": label, "argv": argv, "cwd": str(cwd), "exit": proc.returncode,
                  "stdout_file": out_path.name, "stdout_sha256": sha256_bytes(proc.stdout),
                  "stdout_bytes": len(proc.stdout), "stderr_file": err_path.name,
                  "stderr_sha256": sha256_bytes(proc.stderr), "stderr_bytes": len(proc.stderr)}
        self.children.append(record)
        return record, proc


def child_env(run_dir):
    env = {key: value for key, value in os.environ.items() if key not in DROPPED_ENV}
    env.update(MODEL_ENV_REDIRECTS)
    tmp = run_dir / "tmp"
    tmp.mkdir(exist_ok=True)
    env["TMPDIR"] = str(tmp)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONNOUSERSITE"] = "1"
    return env


def test_child(recorder, label, mode, repo_root, run_dir, extra=()):
    result_path = run_dir / (label + ".result.json")
    argv = [sys.executable, str(repo_root / RUNNER_REL), "--child-tests", mode,
            "--child-result", str(result_path), *extra]
    record, _ = recorder.child(label, argv, repo_root, env=child_env(run_dir))
    data = None
    if result_path.is_file():
        try:
            data = json.loads(result_path.read_text("utf-8"))
        except ValueError:
            data = None
    record["result_file"] = result_path.name
    record["result_sha256"] = sha256_file(result_path)
    return record, data


# ---------------------------------------------------------------- identity ---

def identity(recorder):
    head = recorder.git("rev-parse", "HEAD").strip()
    head_tree = recorder.git("rev-parse", "HEAD^{tree}").strip()
    history = recorder.git("rev-list", "--first-parent", "--max-count=60", "HEAD").split()
    base, candidate_commits = None, []
    for commit in history:
        parents = recorder.git("rev-list", "--parents", "--max-count=1", commit).split()[1:]
        if not parents:
            base = commit
            break
        changed = [line for line in recorder.git("diff", "--name-only", parents[0], commit).splitlines() if line]
        if changed and set(changed) <= set(ALLOWED_CHANGES):
            candidate_commits.append({"commit": commit, "changed": changed})
            continue
        base = commit
        break
    if base is None:
        raise RuntimeError("base commit not found within 60 first-parent commits")
    base_tree = recorder.git("rev-parse", base + "^{tree}").strip()
    changed_vs_base = [line for line in recorder.git("diff", "--name-only", base).splitlines() if line]
    untracked = [line for line in recorder.git("ls-files", "--others", "--exclude-standard").splitlines() if line]
    status = recorder.git("status", "--porcelain", "--untracked-files=all")
    scope = sorted(set(changed_vs_base) | set(untracked))
    outside = [path for path in scope if path not in ALLOWED_CHANGES]
    return {
        "head": head, "head_tree": head_tree, "base": base, "base_tree": base_tree,
        "candidate_commits": candidate_commits, "changed_vs_base_including_worktree": scope,
        "outside_allowed_scope": outside, "worktree_status_porcelain": status,
        "worktree_clean": status == "",
        "file_sha256": {rel: sha256_file(REPO / rel) for rel in ALLOWED_CHANGES},
    }


# ---------------------------------------------------------------- gates ---

def case_test_id(case):
    return "%s.%s.test_%s" % (FOCUSED_MODULE, FOCUSED_CLASS, case)


def focused_gate(data, expected_root):
    problems = []
    if data is None:
        return ["child result missing or malformed"], {}
    suite = data.get("suite") or {}
    outcomes = suite.get("outcomes") or {}
    coverage = {case: (outcomes.get(case_test_id(case)) or {}).get("status", "missing") for case in CASES}
    if suite.get("tests_run") != len(CASES):
        problems.append("tests_run %r != %d" % (suite.get("tests_run"), len(CASES)))
    for case, status in coverage.items():
        if status != "pass":
            problems.append("case %s status %s" % (case, status))
    for key in ("failures", "errors", "skipped", "expected_failures", "unexpected_successes"):
        if suite.get(key):
            problems.append("%s=%r" % (key, suite.get(key)))
    if not suite.get("subtests_passed"):
        problems.append("no subtests executed")
    problems.extend(isolation_problems(data, expected_root))
    return problems, coverage


def isolation_problems(data, expected_root):
    problems = []
    if data.get("sentinel_hits"):
        problems.append("sentinel hits: %r" % data["sentinel_hits"][:10])
    if data.get("refused_subprocesses"):
        problems.append("refused subprocesses: %r" % data["refused_subprocesses"][:10])
    if data.get("foreign_modules"):
        problems.append("modules loaded outside repo: %r" % data["foreign_modules"][:10])
    if data.get("unredirected_constants"):
        problems.append("network-like import-time constants/defaults: %r" % data["unredirected_constants"][:10])
    if data.get("repo_root") != str(expected_root):
        problems.append("child repo root %r != %r" % (data.get("repo_root"), str(expected_root)))
    return problems


def suite_clean(suite, expected_tests=None):
    problems = []
    if not suite or not suite.get("tests_run"):
        return ["zero tests or missing suite"]
    if expected_tests is not None and suite.get("tests_run") != expected_tests:
        problems.append("tests_run %r != expected %d" % (suite.get("tests_run"), expected_tests))
    for key in ("failures", "errors", "skipped", "expected_failures", "unexpected_successes"):
        if suite.get(key):
            problems.append("%s=%r" % (key, suite.get(key)))
    return problems


def run_focused(recorder, run_dir, receipt):
    record, data = test_child(recorder, "focused", "focused", REPO, run_dir)
    problems, coverage = focused_gate(data, REPO)
    receipt["test_count"] = (data or {}).get("suite", {}).get("tests_run")
    receipt["subtests_passed"] = (data or {}).get("suite", {}).get("subtests_passed")
    receipt["case_coverage"] = coverage
    receipt["loaded_modules"] = (data or {}).get("loaded_repo_modules")
    return problems


def run_compatibility(recorder, run_dir, receipt):
    record, data = test_child(recorder, "compatibility", "compatibility", REPO, run_dir)
    if data is None:
        return ["child result missing or malformed"]
    suite = data.get("suite") or {}
    receipt["selectors"] = list(COMPATIBILITY_SELECTORS)
    receipt["test_count"] = suite.get("tests_run")
    receipt["expected_test_count"] = COMPATIBILITY_EXPECTED_TESTS
    receipt["outcomes"] = suite.get("outcomes")
    receipt["child_subprocesses"] = data.get("subprocesses")
    return suite_clean(suite, COMPATIBILITY_EXPECTED_TESTS) + isolation_problems(data, REPO)


def run_negative(recorder, run_dir, receipt):
    record, data = test_child(recorder, "negative", "negative", REPO, run_dir)
    if data is None:
        return ["child result missing or malformed"]
    problems = []
    broken = data.get("broken_phase") or {}
    restored = data.get("restored_phase") or {}
    outcomes = broken.get("outcomes") or {}
    broken_status = {case: (outcomes.get(case_test_id(case)) or {}).get("status", "missing") for case in CASES}
    caught = [case for case in NEGATIVE_MUST_FAIL if broken_status.get(case) == "fail"]
    if broken.get("tests_run") != len(CASES):
        problems.append("broken phase tests_run %r" % broken.get("tests_run"))
    for case in NEGATIVE_MUST_FAIL:
        if broken_status.get(case) != "fail":
            problems.append("broken event_period NOT caught by %s (status %s)" % (case, broken_status.get(case)))
    for case in NEGATIVE_MUST_PASS:
        if broken_status.get(case) != "pass":
            problems.append("control %s did not stay green under the bypass (status %s)"
                            % (case, broken_status.get(case)))
    if broken.get("errors") or broken.get("skipped"):
        problems.append("broken phase had errors/skips: %r/%r" % (broken.get("errors"), broken.get("skipped")))
    restored_problems, restored_coverage = focused_gate({**data, "suite": restored}, REPO)
    problems.extend("restored: " + item for item in restored_problems)
    if data.get("wrapper_installed") is not True:
        problems.append("wrapper was not installed")
    if data.get("restored_identity") is not True:
        problems.append("original resolver not restored")
    if data.get("source_sha256_before") != data.get("source_sha256_after") or not data.get("source_sha256_before"):
        problems.append("production source changed on disk during the negative control")
    if data.get("reference_before") != data.get("reference_after") or not data.get("reference_before"):
        problems.append("restored fingerprints/classifications differ from the originals")
    receipt["bypassed_branch"] = NEGATIVE_BYPASS
    receipt["broken_phase_status"] = broken_status
    receipt["broken_phase_failed_subtests"] = {case: (outcomes.get(case_test_id(case)) or {}).get("subtests_failed")
                                               for case in CASES}
    receipt["caught_by"] = caught
    receipt["controls_green_under_bypass"] = {case: broken_status.get(case) for case in NEGATIVE_MUST_PASS}
    receipt["restored_case_coverage"] = restored_coverage
    receipt["restored_identity"] = data.get("restored_identity")
    receipt["reference_fingerprints_equal"] = data.get("reference_before") == data.get("reference_after")
    receipt["reference_entries"] = len(data.get("reference_before") or [])
    receipt["test_count"] = {"broken": broken.get("tests_run"), "restored": restored.get("tests_run")}
    return problems


def forbidden_fragments():
    # Built from parts so this file does not itself contain the host fragments it rejects.
    return ("/" + "home" + "/", "agent" + "-work", "." + "hermes", "/" + "tmp" + "/", "/" + "Users" + "/",
            "/" + "root" + "/", "work" + "spaces/", "candi" + "dates/")


def run_portability(recorder, run_dir, receipt):
    problems = []
    embedded = {}
    for rel in (TEST_REL, RUNNER_REL):
        text = (REPO / rel).read_text("utf-8")
        hits = [fragment for fragment in forbidden_fragments() if fragment in text]
        embedded[rel] = hits
        if hits:
            problems.append("%s embeds host-specific path fragments %r" % (rel, hits))
    receipt["embedded_path_scan"] = embedded
    copy_root = run_dir / "second-prefix" / "relocated-source"
    tracked = [item for item in recorder.git("ls-files", "-z").split("\0") if item]
    names = sorted(set(tracked) | set(rel for rel in ALLOWED_CHANGES if (REPO / rel).is_file()))
    skipped = []
    for rel in names:
        source = REPO / rel
        if source.is_symlink() or not source.is_file():
            skipped.append(rel)
            continue
        target = copy_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    receipt["copy_root"] = str(copy_root)
    receipt["copied_files"] = len(names) - len(skipped)
    receipt["copy_skipped_non_regular"] = skipped
    copied_hashes = {rel: sha256_file(copy_root / rel) for rel in ALLOWED_CHANGES}
    receipt["copied_changed_file_sha256"] = copied_hashes
    for rel in ALLOWED_CHANGES:
        if copied_hashes[rel] != sha256_file(REPO / rel):
            problems.append("copied %s differs from source" % rel)
    runs = {}
    for label, root in (("portability-original", REPO), ("portability-relocated", copy_root)):
        record, data = test_child(recorder, label, "focused", root, run_dir)
        gate, coverage = focused_gate(data, root)
        problems.extend("%s: %s" % (label, item) for item in gate)
        modules = (data or {}).get("loaded_repo_modules") or {}
        for name in ("campaign_tool.records.context_revision", FOCUSED_MODULE,
                     "tests.records.test_context_revision"):
            path = modules.get(name)
            if not path or not Path(path).is_relative_to(root):
                problems.append("%s: module %s loaded from %r, not %s" % (label, name, path, root))
            other = copy_root if root == REPO else REPO
            if path and Path(path).is_relative_to(other) and not Path(path).is_relative_to(root):
                problems.append("%s: module %s leaked from the other prefix" % (label, name))
        runs[label] = {"root": str(root), "case_coverage": coverage,
                       "tests_run": (data or {}).get("suite", {}).get("tests_run"),
                       "subtests_passed": (data or {}).get("suite", {}).get("subtests_passed"),
                       "loaded_modules": {name: modules.get(name) for name in sorted(modules)
                                          if name.startswith(("campaign_tool", "tests"))},
                       "sentinels": (data or {}).get("sentinels_installed"),
                       "subprocesses": (data or {}).get("subprocesses")}
    receipt["runs"] = runs
    receipt["test_count"] = {label: run["tests_run"] for label, run in runs.items()}
    return problems


def run_credentials(recorder, run_dir, receipt):
    problems = []
    scanner = detect_secrets_path()
    checker = REPO / "tools" / "check_secret_report.py"
    receipt["scanner"] = str(scanner)
    receipt["checker"] = str(checker)
    if not scanner.is_file() or not os.access(scanner, os.X_OK):
        return ["installed detect-secrets not found next to the admitted interpreter"]
    if not checker.is_file():
        return ["repository checker missing"]
    scan_record, scan = recorder.child("detect-secrets", [str(scanner), "scan", "--all-files", "--exclude-files",
                                                          "^.git/", "--no-verify"], REPO, env=child_env(run_dir))
    report = run_dir / SCAN_REPORT_NAME
    report.write_bytes(scan.stdout)
    receipt["scan_report"] = report.name
    receipt["scan_report_sha256"] = sha256_bytes(scan.stdout)
    receipt["scanner_exit"] = scan.returncode
    if scan.returncode != 0:
        problems.append("detect-secrets exit %d" % scan.returncode)
    try:
        parsed = json.loads(scan.stdout.decode("utf-8"))
        results = parsed["results"]
        if not isinstance(results, dict):
            raise ValueError("results not a mapping")
        receipt["raw_hit_files"] = {path: len(hits) for path, hits in sorted(results.items())}
        receipt["scanner_version"] = parsed.get("version")
    except (ValueError, KeyError, TypeError, UnicodeDecodeError) as error:
        problems.append("scanner output unreadable or malformed: %s" % error)
    check_record, check = recorder.child("check-secret-report", [sys.executable, str(checker), str(report)],
                                         REPO, env=child_env(run_dir))
    receipt["checker_exit"] = check.returncode
    if check.returncode != 0:
        problems.append("repository checker rejected the scan (exit %d)" % check.returncode)
    return problems


def run_public_tree(recorder, run_dir, receipt):
    script = REPO / "tools" / "check_public_tree.py"
    record, proc = recorder.child("check-public-tree", [sys.executable, str(script)], REPO, env=child_env(run_dir))
    receipt["public_tree_exit"] = proc.returncode
    return [] if proc.returncode == 0 else ["check_public_tree exit %d" % proc.returncode]


MODES = {"focused": run_focused, "compatibility": run_compatibility, "negative": run_negative,
         "portability": run_portability, "credentials": run_credentials, "public-tree": run_public_tree}


def parent_main(args):
    output_root = Path(args.output_root).expanduser().resolve()
    if output_root == REPO or output_root.is_relative_to(REPO):
        print("output root must be outside the repository", file=sys.stderr)
        return 2
    output_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    run_dir = output_root / ("run-%s-%s-%d" % (stamp, args.check, os.getpid()))
    run_dir.mkdir(exist_ok=False)
    recorder = Recorder(run_dir)
    receipt = {
        "schema": "fc3-context-snapshot-acceptance-v1", "scope": SCOPE, "card": CARD, "brief": BRIEF_NAME,
        "check": args.check, "argv": [sys.executable, *sys.argv], "cwd": os.getcwd(),
        "interpreter": sys.executable, "python_version": sys.version, "repo": str(REPO),
        "run_dir": str(run_dir), "started_utc": utc_now(),
        "verdict_scope": "REVIEW REQUIRED: machine-valid output only; not semantic, legal, stage or "
                         "release acceptance.",
        "full_discovery": "operator-run (not part of this runner)",
    }
    problems = []
    try:
        receipt["identity_before"] = identity(recorder)
        if receipt["identity_before"]["outside_allowed_scope"]:
            problems.append("changes outside the allowed files: %r" % receipt["identity_before"]["outside_allowed_scope"])
        problems.extend(MODES[args.check](recorder, run_dir, receipt))
        receipt["identity_after"] = identity(recorder)
        before, after = receipt["identity_before"], receipt["identity_after"]
        for key in ("head", "worktree_status_porcelain", "file_sha256"):
            if before[key] != after[key]:
                problems.append("repository %s changed during the run" % key)
    except Exception:  # Record and fail closed; never hide an unavailable check.
        problems.append("runner exception: " + traceback.format_exc())
    receipt["children"] = recorder.children
    receipt["git_calls"] = recorder.git_calls
    receipt["problems"] = problems
    receipt["passed"] = not problems
    receipt["result"] = ("PASS (machine-valid; REVIEW REQUIRED)" if not problems else "FAIL")
    receipt["finished_utc"] = utc_now()
    receipt_path = run_dir / "receipt.json"
    receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", "utf-8")
    print(json.dumps({"check": args.check, "result": receipt["result"], "run_dir": str(run_dir),
                      "receipt_sha256": sha256_file(receipt_path), "test_count": receipt.get("test_count"),
                      "problems": [item[:500] for item in problems[:20]]}, indent=2))
    return 0 if not problems else 1


# ---------------------------------------------------------------- child side ---

def install_sentinels(repo_root, state):
    """Block network, listeners, model transports and unexpected processes in this child."""
    import http.client
    import socket
    import ssl
    import urllib.request

    def deny(name):
        def blocked(*args, **kwargs):
            state["sentinel_hits"].append(name)
            raise RuntimeError("FC3 sentinel blocked " + name)
        return blocked

    for name in ("connect", "connect_ex", "bind", "listen", "sendto"):
        setattr(socket.socket, name, deny("socket.socket." + name))
    for name in ("create_connection", "create_server", "getaddrinfo", "gethostbyname", "socketpair"):
        setattr(socket, name, deny("socket." + name))
    ssl.SSLContext.wrap_socket = deny("ssl.SSLContext.wrap_socket")
    ssl.SSLContext.wrap_bio = deny("ssl.SSLContext.wrap_bio")
    http.client.HTTPConnection.connect = deny("http.client.HTTPConnection.connect")
    http.client.HTTPSConnection.connect = deny("http.client.HTTPSConnection.connect")
    urllib.request.urlopen = deny("urllib.request.urlopen")
    for name in ("system", "popen", "fork", "forkpty", "execv", "execve", "execl", "execle", "execlp",
                 "execlpe", "execvp", "execvpe", "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv",
                 "spawnve", "spawnvp", "spawnvpe"):
        if hasattr(os, name):
            setattr(os, name, deny("os." + name))
    os.environ.update(MODEL_ENV_REDIRECTS)

    allowed_scripts = {str((repo_root / "tools" / "check_secret_report.py").resolve())}
    git_list = ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"]
    real_init = subprocess.Popen.__init__

    def guarded(self, args, *rest, **kwargs):
        argv = [str(item) for item in args] if isinstance(args, (list, tuple)) else None
        ok = (argv is not None and not kwargs.get("shell") and
              ((len(argv) >= 2 and argv[0] == sys.executable and str(Path(argv[1]).resolve()) in allowed_scripts)
               or argv == git_list))
        if not ok:
            state["refused_subprocesses"].append(repr(args)[:300])
            raise RuntimeError("FC3 sentinel refused subprocess")
        state["subprocesses"].append(argv)
        return real_init(self, args, *rest, **kwargs)

    subprocess.Popen.__init__ = guarded
    state["sentinels_installed"] = ["socket", "ssl", "http.client", "urllib.request", "os.exec/spawn/system",
                                    "subprocess.Popen allow-list", "model/proxy environment redirects"]


def scan_constants(repo_root, state):
    """Report network-like import-time constants or bound defaults in loaded application modules."""
    import types
    found = []

    def network_like(value):
        return isinstance(value, str) and value.startswith(("http://", "https://")) and \
            not value.startswith(("http://127.0.0.1:9", "https://127.0.0.1:9"))

    for name, module in sorted(sys.modules.items()):
        if not name.startswith("campaign_tool") or module is None:
            continue
        for attr, value in list(vars(module).items()):
            if network_like(value):
                found.append("%s.%s" % (name, attr))
            if isinstance(value, types.FunctionType) and getattr(value, "__module__", None) == name:
                defaults = list(value.__defaults__ or ()) + list((value.__kwdefaults__ or {}).values())
                if any(network_like(item) for item in defaults):
                    found.append("%s.%s()" % (name, attr))
    state["unredirected_constants"] = found


def loaded_modules(repo_root, state):
    repo_modules, foreign = {}, []
    for name, module in sorted(sys.modules.items()):
        path = getattr(module, "__file__", None)
        if not path:
            continue
        resolved = Path(path).resolve()
        if resolved.is_relative_to(repo_root):
            repo_modules[name] = str(resolved)
        elif name.split(".")[0] in ("campaign_tool", "tests", "tools"):
            foreign.append("%s=%s" % (name, resolved))
    state["loaded_repo_modules"] = repo_modules
    state["foreign_modules"] = foreign


def run_suite(names):
    import unittest

    class Result(unittest.TextTestResult):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.outcomes = {}
            self.subtests_passed = 0

        def _mark(self, test, status):
            entry = self.outcomes.setdefault(test.id(), {"status": "pass", "subtests_failed": 0})
            if status != "pass":
                entry["status"] = status if entry["status"] == "pass" else entry["status"]

        def startTest(self, test):
            super().startTest(test)
            self.outcomes.setdefault(test.id(), {"status": "pass", "subtests_failed": 0})

        def addFailure(self, test, err):
            super().addFailure(test, err)
            self._mark(test, "fail")

        def addError(self, test, err):
            super().addError(test, err)
            self._mark(test, "error")

        def addSkip(self, test, reason):
            super().addSkip(test, reason)
            self._mark(test, "skip")

        def addExpectedFailure(self, test, err):
            super().addExpectedFailure(test, err)
            self._mark(test, "expected_failure")

        def addUnexpectedSuccess(self, test):
            super().addUnexpectedSuccess(test)
            self._mark(test, "unexpected_success")

        def addSubTest(self, test, subtest, err):
            super().addSubTest(test, subtest, err)
            if err is None:
                self.subtests_passed += 1
                return
            failed = issubclass(err[0], test.failureException)
            self._mark(test, "fail" if failed else "error")
            self.outcomes[test.id()]["subtests_failed"] += 1

    suite = unittest.defaultTestLoader.loadTestsFromNames(list(names))
    runner = unittest.TextTestRunner(stream=sys.stderr, verbosity=2, resultclass=Result)
    result = runner.run(suite)
    return {"selectors": list(names), "tests_run": result.testsRun, "failures": len(result.failures),
            "errors": len(result.errors), "skipped": len(result.skipped),
            "expected_failures": len(result.expectedFailures),
            "unexpected_successes": len(result.unexpectedSuccesses),
            "subtests_passed": result.subtests_passed, "outcomes": result.outcomes,
            "was_successful": result.wasSuccessful()}


def negative_reference(cr, fixtures):
    reference = []
    for scenario in fixtures.positive_scenarios() + fixtures.unresolved_scenarios():
        label, rule_id, rules, context, bases = scenario[:5]
        result = cr.resolve_applicability(rule_id, rules, context, bases)
        reference.append({"scenario": label, "context_fingerprint": cr.fingerprint(context),
                          "classification": result["classification"], "applicability": result["applicability"],
                          "reasons": list(result["reasons"]), "context_sha256": result["context_sha256"],
                          "basis_sha256": result.get("basis_sha256"),
                          "rule_revision_sha256": result["rule_revision_sha256"]})
    return reference


def child_main(args):
    repo_root = Path(__file__).resolve().parents[1]
    # In-script, repo-relative import setup: this repo first; loaded paths are verified afterwards.
    sys.path[:] = [str(repo_root)] + [entry for entry in sys.path
                                      if entry not in ("", ".", str(repo_root))]
    state = {"repo_root": str(repo_root), "mode": args.child_tests, "sentinel_hits": [],
             "refused_subprocesses": [], "subprocesses": [], "sys_path": list(sys.path)}
    install_sentinels(repo_root, state)
    exit_code = 1
    try:
        if args.child_tests == "focused":
            state["suite"] = run_suite([FOCUSED_MODULE])
            exit_code = 0 if state["suite"]["was_successful"] else 1
        elif args.child_tests == "compatibility":
            state["suite"] = run_suite(COMPATIBILITY_SELECTORS)
            exit_code = 0 if state["suite"]["was_successful"] else 1
        elif args.child_tests == "negative":
            from campaign_tool.records import context_revision as cr
            import importlib
            fixtures = importlib.import_module(FOCUSED_MODULE)
            source = Path(cr.__file__)
            state["source_sha256_before"] = sha256_file(source)
            original = cr.resolve_applicability
            state["reference_before"] = negative_reference(cr, fixtures)

            def reattached(rule_id, rules, context, bases=()):
                result = original(rule_id, rules, context, bases)
                if result.get("event_period") is not None:
                    result["event_period"] = context["dates"][result["event_role"]][0]
                return result

            cr.resolve_applicability = reattached
            state["wrapper_installed"] = cr.resolve_applicability is reattached
            try:
                state["broken_phase"] = run_suite([FOCUSED_MODULE])
            finally:
                cr.resolve_applicability = original
            state["restored_identity"] = cr.resolve_applicability is original
            state["reference_after"] = negative_reference(cr, fixtures)
            state["restored_phase"] = run_suite([FOCUSED_MODULE])
            state["source_sha256_after"] = sha256_file(source)
            exit_code = 0 if state["restored_phase"]["was_successful"] else 1
        else:
            state["error"] = "unknown child mode"
    except Exception:
        state["error"] = traceback.format_exc()
        exit_code = 1
    loaded_modules(repo_root, state)
    scan_constants(repo_root, state)
    Path(args.child_result).write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", "utf-8")
    return exit_code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", choices=CHECKS)
    parser.add_argument("--output-root")
    parser.add_argument("--child-tests", choices=("focused", "compatibility", "negative"), help=argparse.SUPPRESS)
    parser.add_argument("--child-result", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.child_tests:
        if not args.child_result or args.check:
            parser.error("internal child mode requires --child-result only")
        return child_main(args)
    if not args.check or not args.output_root:
        parser.error("--check and --output-root are required")
    return parent_main(args)


if __name__ == "__main__":
    sys.exit(main())
