"""FC10-LAW-JURISDICTION acceptance runner (offline, synthetic, REVIEW REQUIRED only).

Usage (one row per invocation):
  python3 tools/fc10_law_jurisdiction_acceptance.py --phase PHASE \
      --source-manifest SOURCE-MANIFEST.json --out-base DIR

PHASE is one of: suite, compatibility, portable, credentials, public-tree.

The repository is derived from this file. Every invocation creates a NEW
directory DIR/<phase>-<utc>-<uuid>/ (DIR must be outside the repository),
validates the staging manifest against the checkout before any product code is
imported, and records the literal argv, cwd, interpreter, candidate HEAD/tree,
authored-file and fixture hashes, every child's exit and raw stdout/stderr with
hashes, counts and limitations. Children are literal argument lists with the
shell disabled and the inherited environment. Test children set their own
temporary root inside the invocation directory and install terminal offline
guards before importing product code: a genuine guard attempt ends that child
at once (exit 86, typed refusal record), the parent launches no further child
and the row STOPs with exit 3 for the existing attended guard owner. Exit 0
means machine validity of that row only:
REVIEW REQUIRED, never semantic, legal, human or live acceptance.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import traceback
import uuid

REPO = Path(__file__).resolve().parents[1]
PHASES = ("suite", "compatibility", "portable", "credentials", "public-tree")
FOCUSED = "tests.records.test_fc10_law_jurisdiction"
CLASS = FOCUSED + ".JurisdictionGrammarTests"
DECIDING = CLASS + ".test_trailing_newline_cr_crlf"
NEWLINE_CASE = "(value='us-ca\\n')"
MUTATION_POSITIVES = (CLASS + ".test_ordinary_us_ca_and_other_two_letter_pairs",
                      CLASS + ".test_explicit_base_preserved")
COMPATIBILITY = ("tests.test_law.CaliforniaPackageTests", "tests.test_law.LawFunctionTests",
                 "tests.test_law.LoadPackageValidationTests")
PEER = "tests.test_law.LawFunctionTests"
LAW = "campaign_tool/law.py"
TEST = "tests/records/test_fc10_law_jurisdiction.py"
FIXTURE = "tests/fixtures/fc10-law-jurisdiction/cases.json"
RUNNER = "tools/fc10_law_jurisdiction_acceptance.py"
AUTHORED = (LAW, TEST, FIXTURE, RUNNER)
STATIC_CHECKED = (TEST, FIXTURE, RUNNER)
SCANNERS = ("tools/check_public_tree.py", "campaign_tool/records/public_scan.py",
            "tools/check_secret_report.py")
TIMEOUT = 1200
# Host-only or inline-interpreter forms forbidden in authored test/fixture/runner
# source. Split literals keep the list from matching itself.
FORBIDDEN_SOURCE = ("/ho" "me/", "agent" "-work", "venv" "-fullsuite", "PYTHON" "PATH",
                    "shell" "=True", "os." "system", "<" "<EOF", "LD_" "PRELOAD", ".her" "mes",
                    "/t" "mp/", "candi" "dates/")
INLINE_FLAG = re.compile("[\"']-[ce][\"']")


def sha256_bytes(raw):
    return hashlib.sha256(raw).hexdigest()


def sha256_file(path):
    path = Path(path)
    return sha256_bytes(path.read_bytes()) if path.is_file() and not path.is_symlink() else None


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


# =========================================================================== children
GUARD_REFUSAL_EXIT = 86
GUARD_REFUSAL_KIND = "fc10_guard_refusal"
SCRIPT_REFUSAL_MARK = "FC10-GUARD-REFUSAL "
SCRIPT_CLEAN_MARK = re.compile(r"^FC10-GUARD installed=(\d+) attempts=0$", re.M)
CHILD_CONTEXT = {}


def terminal_refusal(label):
    """A genuine guard attempt ends this child at once with a typed refusal record.

    Nothing after this runs: no later test, test group or result aggregation.
    The parent sees exit GUARD_REFUSAL_EXIT and the typed record and launches no
    further child.
    """
    record = {"kind": GUARD_REFUSAL_KIND, "pass": False, "label": label,
              "error": "offline guard refused " + label, "argv": list(sys.argv),
              "pid": os.getpid(), "child": CHILD_CONTEXT.get("child"),
              "run_identity": CHILD_CONTEXT.get("run_identity"),
              "stack": "".join(traceback.format_stack(limit=25))}
    raw = (json.dumps(record, indent=2, sort_keys=True, default=str) + "\n").encode()
    try:
        fd = os.open(CHILD_CONTEXT["result_json"], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(fd, raw)
        os.close(fd)
    finally:
        os.write(2, (SCRIPT_REFUSAL_MARK + label + "\n").encode())
        os._exit(GUARD_REFUSAL_EXIT)


def install_child_policy(temp_root):
    """Before any product import: temporary root inside the run, terminal offline guards."""
    import socket
    import subprocess as subprocess_module
    import tempfile

    temp_root = Path(temp_root)
    temp_root.mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = str(temp_root)
    attempts = []  # Stays empty: a guard attempt never returns (terminal_refusal).

    def sentinel(label):
        def refuse(*_args, **_kwargs):
            attempts.append(label)
            terminal_refusal(label)
        return refuse

    socket.socket.connect = sentinel("socket.connect")
    socket.socket.connect_ex = sentinel("socket.connect_ex")
    socket.socket.bind = sentinel("socket.bind")
    socket.create_connection = sentinel("socket.create_connection")
    socket.create_server = sentinel("socket.create_server")
    socket.getaddrinfo = sentinel("socket.getaddrinfo")
    subprocess_module.Popen = sentinel("subprocess.Popen")
    for name in ("system", "fork", "forkpty", "execv", "execve", "execvp", "execvpe",
                 "spawnv", "spawnve", "posix_spawn", "posix_spawnp", "popen"):
        if hasattr(os, name):
            setattr(os, name, sentinel("os." + name))
    sys.path.insert(0, str(REPO))
    return attempts


def run_tests(names=None, suite=None):
    import unittest

    class Recorder(unittest.TextTestResult):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.started, self.subtests_ok, self.subtest_failures = [], [], []

        def startTest(self, test):
            self.started.append(test.id())
            super().startTest(test)

        def addSubTest(self, test, subtest, err):
            if err is None:
                self.subtests_ok.append(subtest.id())
            else:
                self.subtest_failures.append([subtest.id(), self._exc_info_to_string(err, test)])
            super().addSubTest(test, subtest, err)

    if suite is None:
        suite = unittest.defaultTestLoader.loadTestsFromNames(list(names))
    runner = unittest.TextTestRunner(stream=sys.stderr, verbosity=2, resultclass=Recorder)
    result = runner.run(suite)
    return {
        "tests_run": result.testsRun,
        "executed": list(result.started),
        "subtests_ok": list(result.subtests_ok),
        "subtest_failures": list(result.subtest_failures),
        "failures": [t.id() for t, _ in result.failures],
        "errors": [[t.id(), text] for t, text in result.errors],
        "skipped": [[t.id(), reason] for t, reason in result.skipped],
        "expected_failures": [t.id() for t, _ in result.expectedFailures],
        "unexpected_successes": [t.id() for t in result.unexpectedSuccesses],
        "was_successful": result.wasSuccessful(),
    }


def clean(summary):
    return (summary.get("was_successful") is True and summary.get("tests_run", 0) > 0
            and summary["tests_run"] == len(summary["executed"])
            and not (summary["failures"] or summary["errors"] or summary["skipped"]
                     or summary["expected_failures"] or summary["unexpected_successes"]
                     or summary["subtest_failures"]))


def fixture_coverage(summary):
    """Successful subtests per dimension test versus the frozen fixture values."""
    cases = json.loads((REPO / FIXTURE).read_text(encoding="utf-8"))["dimensions"]
    coverage = {}
    for dimension, spec in cases.items():
        test_id = CLASS + ".test_" + dimension
        observed = sum(1 for sid in summary.get("subtests_ok", []) if sid.startswith(test_id + " "))
        coverage[dimension] = {"fixture_values": len(spec["values"]), "subtests_ok": observed,
                               "test_executed": test_id in summary.get("executed", [])}
    complete = all(c["test_executed"] and c["subtests_ok"] == c["fixture_values"]
                   for c in coverage.values())
    return coverage, complete


def child_suite(args):
    attempts = install_child_policy(args.temp_root)
    summary = run_tests([FOCUSED])
    coverage, complete = fixture_coverage(summary)
    return {"suite": summary, "fixture_coverage": coverage, "coverage_complete": complete,
            "sentinel_attempts": attempts,
            "pass": clean(summary) and complete and not attempts}


def child_baseline(args):
    """Run the new suite against the pinned-base law.py bytes, loaded in memory."""
    import types

    attempts = install_child_policy(args.temp_root)
    source = Path(args.baseline_source).read_bytes()
    record = {"baseline_source_sha256": sha256_bytes(source),
              "expected_source_sha256": args.baseline_sha256}
    if record["baseline_source_sha256"] != args.baseline_sha256:
        record["pass"] = False
        return record
    import campaign_tool
    module = types.ModuleType("campaign_tool.law")
    module.__file__ = str(REPO / LAW)  # Same resource root as the real module.
    module.__package__ = "campaign_tool"
    exec(compile(source, str(REPO / LAW), "exec"), module.__dict__)  # noqa: S102 pinned blob
    sys.modules["campaign_tool.law"] = module
    campaign_tool.law = module
    summary = run_tests([FOCUSED])
    from campaign_tool import law as loaded
    failed_subtests = [sid for sid, _ in summary["subtest_failures"]]
    allowed_parents = {DECIDING, CLASS + ".test_package_status_invalid_is_missing",
                       CLASS + ".test_no_read_on_invalid"}
    newline_only = all(sid.split(" ", 1)[0] in allowed_parents and sid.endswith("\\n')")
                       for sid in failed_subtests)
    record.update({
        "suite": summary, "sentinel_attempts": attempts,
        "baseline_module_in_use": loaded is module and not hasattr(module, "_valid_jurisdiction"),
        "decisions": {
            "deciding_newline_case_failed": any(sid == DECIDING + " " + NEWLINE_CASE
                                                for sid in failed_subtests),
            "only_trailing_newline_subtests_failed": bool(failed_subtests) and newline_only,
            "no_errors_or_skips": not (summary["errors"] or summary["skipped"]),
            "expected_red": summary["was_successful"] is False,
        }})
    record["pass"] = (record["baseline_module_in_use"] and not attempts
                      and all(record["decisions"].values()))
    return record


def child_negative(args):
    from unittest import mock

    attempts = install_child_policy(args.temp_root)
    from campaign_tool import law

    original = law._valid_jurisdiction

    def old_prefix_predicate(jurisdiction):
        return isinstance(jurisdiction, str) and law.JURISDICTION_PATTERN.match(jurisdiction) is not None

    record = {"before": run_tests([FOCUSED])}
    with mock.patch.object(law, "_valid_jurisdiction", old_prefix_predicate):
        record["mutant_deciding"] = run_tests([DECIDING])
        record["mutant_positives"] = run_tests(list(MUTATION_POSITIVES))
    record["restored_identity"] = law._valid_jurisdiction is original
    record["after"] = run_tests([FOCUSED])
    deciding = record["mutant_deciding"]
    target = [text for sid, text in deciding["subtest_failures"]
              if sid == DECIDING + " " + NEWLINE_CASE]
    record["decisions"] = {
        "before_clean": clean(record["before"]),
        "deciding_newline_assertion_failed": bool(target) and "ValueError not raised" in target[0],
        "deciding_no_errors": not deciding["errors"] and deciding["tests_run"] == 1,
        "mutant_positives_green": clean(record["mutant_positives"])
        and record["mutant_positives"]["tests_run"] == len(MUTATION_POSITIVES),
        "restored_identity": record["restored_identity"],
        "after_clean": clean(record["after"]),
        "no_sentinel_attempts": not attempts,
    }
    record["sentinel_attempts"] = attempts
    record["pass"] = all(record["decisions"].values())
    return record


def child_compatibility(args):
    attempts = install_child_policy(args.temp_root)
    summary = run_tests(list(COMPATIBILITY))
    classes = sorted({tid.rsplit(".", 1)[0] for tid in summary["executed"]})
    return {"suite": summary, "selectors": list(COMPATIBILITY), "executed_classes": classes,
            "sentinel_attempts": attempts,
            "pass": clean(summary) and classes == sorted(COMPATIBILITY) and not attempts}


def ambient_snapshot():
    import socket
    import subprocess as subprocess_module
    import tempfile
    guarded = {"socket.socket.connect": socket.socket.connect, "socket.getaddrinfo": socket.getaddrinfo,
               "subprocess.Popen": subprocess_module.Popen, "os.fork": getattr(os, "fork", None),
               "os.execv": getattr(os, "execv", None), "os.posix_spawn": getattr(os, "posix_spawn", None)}
    return {"environ": dict(os.environ), "tempfile_tempdir": tempfile.tempdir,
            "gettempdir": tempfile.gettempdir(), "cwd": os.getcwd(),
            "umask": os.umask(os.umask(0o022)), "sys_path": list(sys.path),
            "guarded_function_ids": {k: id(v) for k, v in guarded.items()}}


def child_discover(args):
    import unittest

    attempts = install_child_policy(args.temp_root)
    sentinel_key = "FC10_UNRELATED_SENTINEL"
    os.environ[sentinel_key] = "sentinel-" + uuid.uuid4().hex  # set inside this child only
    peer_before = run_tests([PEER])
    before = ambient_snapshot()
    suite = unittest.TestLoader().discover(start_dir=str(REPO / "tests" / "records"),
                                           pattern="test_fc10_law_jurisdiction.py",
                                           top_level_dir=str(REPO))
    after_import = ambient_snapshot()
    summary = run_tests(suite=suite)
    after_run = ambient_snapshot()
    peer_after = run_tests([PEER])
    coverage, complete = fixture_coverage(summary)
    unchanged_import = before == after_import
    unchanged_run = before == after_run
    module = sys.modules.get(FOCUSED)
    import_installed_no_guards = module is not None and getattr(module, "SCRIPT_GUARDS", None) == []
    return {"suite": summary, "fixture_coverage": coverage, "coverage_complete": complete,
            "peer_before": peer_before, "peer_after": peer_after,
            "ambient_unchanged_after_import": unchanged_import,
            "ambient_unchanged_after_run": unchanged_run,
            "import_installed_no_script_guards": import_installed_no_guards,
            "sentinel_attempts": attempts,
            "pass": (clean(summary) and complete and clean(peer_before) and clean(peer_after)
                     and peer_before["executed"] == peer_after["executed"]
                     and unchanged_import and unchanged_run and import_installed_no_guards
                     and not attempts)}


CHILDREN = {"suite": child_suite, "baseline": child_baseline, "negative": child_negative,
            "compatibility": child_compatibility, "discover": child_discover}


def child_main(args):
    CHILD_CONTEXT.update({"child": args.child, "result_json": args.result_json,
                          "run_identity": Path(args.result_json).parent.name})
    try:
        record = CHILDREN[args.child](args)
    except Exception:  # An exception is a failed child, never a killed mutation.
        record = {"pass": False, "exception": traceback.format_exc()}
    fd = os.open(args.result_json, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(json.dumps(record, indent=2, sort_keys=True, default=str) + "\n")
    return 0 if record.get("pass") is True else 1


# =========================================================================== parent
class Run:
    def __init__(self, phase, out_base):
        stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
        self.dir = out_base / f"{phase}-{stamp}-{uuid.uuid4()}"
        self.dir.mkdir(mode=0o700)  # Fresh directory; earlier outputs are preserved.
        self.work = self.dir / "work"
        self.work.mkdir(mode=0o700)
        self.children, self.files = [], []

    def write_new(self, name, raw):
        fd = os.open(self.dir / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
        self.files.append(name)
        return sha256_bytes(raw)

    def child(self, label, argv, cwd, stdout_name=None):
        index = len(self.children)
        entry = {"label": label, "argv": [str(a) for a in argv], "cwd": str(cwd), "shell": False}
        try:
            result = subprocess.run(entry["argv"], cwd=cwd, capture_output=True, timeout=TIMEOUT)
            entry["exit"], out, err = result.returncode, result.stdout, result.stderr
        except subprocess.TimeoutExpired as exc:
            entry["exit"], entry["timeout"] = None, True
            out, err = exc.stdout or b"", exc.stderr or b""
        entry["stdout_file"] = stdout_name or f"{index:02d}-{label}.stdout"
        entry["stderr_file"] = f"{index:02d}-{label}.stderr"
        entry["stdout_sha256"] = self.write_new(entry["stdout_file"], out)
        entry["stderr_sha256"] = self.write_new(entry["stderr_file"], err)
        self.children.append(entry)
        return entry, out, err


def git(args, cwd=REPO, raw=False):
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, timeout=120)
    out = result.stdout if raw else result.stdout.decode("utf-8", "surrogateescape").strip()
    return result.returncode, out


def identity(repo=REPO):
    return {"head": git(["rev-parse", "HEAD"], repo)[1],
            "tree": git(["rev-parse", "HEAD^{tree}"], repo)[1],
            "porcelain": git(["status", "--porcelain=v1", "--untracked-files=all"], repo)[1],
            "authored_sha256": {rel: sha256_file(repo / rel) for rel in AUTHORED},
            "scanner_sha256": {rel: sha256_file(repo / rel) for rel in SCANNERS}}


def preflight(phase, manifest_path):
    """Validate the staging manifest against this checkout before any product import."""
    raw = Path(manifest_path).read_bytes()
    manifest = json.loads(raw)
    runtime = manifest.get("runtime", {})
    head = git(["rev-parse", "HEAD"])[1]
    base = manifest.get("head")
    base_tree = git(["rev-parse", f"{base}^{{tree}}"])[1] if isinstance(base, str) else None
    venv_link = REPO / ".venv"
    expected_python = (runtime.get("credential_scan_python3") if phase == "credentials"
                       else str(REPO / ".venv" / "bin" / "python3"))
    checks = {
        "schema": manifest.get("schema") == "agentorange.source-manifest/v1",
        "repo_matches_runner": Path(manifest.get("repo", "")) == REPO,
        "write_root_is_repo_parent": Path(manifest.get("write_root", "")) == REPO.parent,
        "base_tree_matches_manifest": base_tree is not None and base_tree == manifest.get("tree"),
        "head_descends_from_base": isinstance(base, str)
        and git(["merge-base", "--is-ancestor", base, "HEAD"])[0] == 0,
        "venv_link_matches_manifest": venv_link.is_symlink()
        and os.readlink(venv_link) == runtime.get("repo_venv"),
        "interpreter_matches": sys.executable == expected_python,
        "runtime_paths_exist": all(Path(runtime.get(k, "")).exists()
                                   for k in ("repo_venv", "credential_scan_python3", "detect_secrets")),
    }
    return {"manifest_path": str(Path(manifest_path).absolute()), "manifest_sha256": sha256_bytes(raw),
            "base": base, "base_tree": base_tree, "head_at_start": head,
            "head_equals_base": head == base, "expected_interpreter": expected_python,
            "interpreter": sys.executable, "checks": checks,
            "pass": all(checks.values())}, manifest


class GuardRefusal(Exception):
    """A child reported a genuine offline-guard attempt: the row stops here."""

    def __init__(self, info):
        super().__init__(info.get("label"))
        self.info = info


def refusal_check(entry, label, record=None, stderr=b""):
    """Raise GuardRefusal for a typed refusal record, the refusal exit or marker."""
    text = stderr.decode("utf-8", "replace") if isinstance(stderr, bytes) else str(stderr)
    typed = isinstance(record, dict) and record.get("kind") == GUARD_REFUSAL_KIND
    marked = any(line.startswith(SCRIPT_REFUSAL_MARK) for line in text.splitlines())
    if typed or marked or entry.get("exit") == GUARD_REFUSAL_EXIT:
        entry["guard_refusal"] = True
        raise GuardRefusal({"label": label, "argv": entry["argv"], "cwd": entry["cwd"],
                            "exit": entry.get("exit"), "stderr_file": entry.get("stderr_file"),
                            "refusal_record": record if typed else None,
                            "refusal_lines": [line for line in text.splitlines()
                                              if line.startswith(SCRIPT_REFUSAL_MARK)]})


def runner_child(run, label, child, extra=(), cwd=REPO, script=None):
    result_name = f"{len(run.children):02d}-{label}.result.json"
    result_path = run.dir / result_name
    temp_root = run.work / ("tmp-" + label)
    argv = [sys.executable, script or (REPO / RUNNER), "--child", child,
            "--result-json", result_path, "--temp-root", temp_root, *extra]
    entry, _, err = run.child(label, argv, cwd)
    try:
        record = json.loads(result_path.read_text())
        run.files.append(result_name)
    except (OSError, ValueError):
        record = {"pass": False, "missing_child_result": True}
    entry["result_json"] = result_name
    entry["result_sha256"] = sha256_file(result_path)
    entry["child_pass"] = entry["exit"] == 0 and record.get("pass") is True
    refusal_check(entry, label, record, err)  # Stops all further scheduling.
    return entry, record


def phase_suite(run, prelim, manifest):
    base = manifest["head"]
    code, blob = git(["show", f"{base}:{LAW}"], raw=True)
    baseline_hash = run.write_new("baseline-law.py", blob) if code == 0 else None
    b_entry, b_record = runner_child(run, "baseline-original-law", "baseline", extra=(
        "--baseline-source", run.dir / "baseline-law.py", "--baseline-sha256", baseline_hash or "none"))
    n_entry, n_record = runner_child(run, "negative-control", "negative")
    s_entry, s_record = runner_child(run, "candidate-suite", "suite")
    suite = s_record.get("suite", {})
    return {
        "baseline_unmodified": {"base": base, "law_blob_sha256": baseline_hash,
                                "decisions": b_record.get("decisions"),
                                "failed_subtests": [s for s, _ in b_record.get("suite", {})
                                                    .get("subtest_failures", [])],
                                "tests_run": b_record.get("suite", {}).get("tests_run"),
                                "pass_as_expected_red": b_entry["child_pass"]},
        "injected_control": {"mutation": "law._valid_jurisdiction -> old JURISDICTION_PATTERN.match predicate (in memory)",
                             "deciding_test": DECIDING + " " + NEWLINE_CASE,
                             "decisions": n_record.get("decisions"), "pass": n_entry["child_pass"]},
        "candidate": {"tests_run": suite.get("tests_run"), "executed": suite.get("executed"),
                      "subtests_ok": len(suite.get("subtests_ok", [])),
                      "failures": suite.get("failures"), "errors": suite.get("errors"),
                      "skipped": suite.get("skipped"),
                      "fixture_coverage": s_record.get("fixture_coverage"),
                      "pass": s_entry["child_pass"]},
        "tests_run": suite.get("tests_run"),
        "pass": code == 0 and b_entry["child_pass"] and n_entry["child_pass"] and s_entry["child_pass"],
    }


def phase_compatibility(run, prelim, manifest):
    entry, record = runner_child(run, "compatibility-selectors", "compatibility")
    suite = record.get("suite", {})
    return {"selectors": list(COMPATIBILITY), "tests_run": suite.get("tests_run"),
            "executed": suite.get("executed"), "failures": suite.get("failures"),
            "errors": suite.get("errors"), "skipped": suite.get("skipped"),
            "scope_note": "Scoped local selectors only; not full-repository CI.",
            "pass": entry["child_pass"]}


def static_scan():
    findings = {}
    for rel in STATIC_CHECKED:
        text = (REPO / rel).read_text(encoding="utf-8")
        hits = [pattern for pattern in FORBIDDEN_SOURCE if pattern in text]
        if INLINE_FLAG.search(text):
            hits.append("inline-interpreter-flag")
        findings[rel] = hits
    return findings


def phase_portable(run, prelim, manifest):
    fresh = run.work / ("cwd-" + uuid.uuid4().hex)
    fresh.mkdir()
    argv = [sys.executable, REPO / TEST, "--temp-root", run.work / "tmp-script", "-v"]
    s_entry, _, s_err = run.child("script-by-path", argv, fresh)
    refusal_check(s_entry, "script-by-path", None, s_err)  # Stops before discovery.
    text = s_err.decode("utf-8", "replace")
    guard_mark = SCRIPT_CLEAN_MARK.search(text)
    guards_installed = int(guard_mark[1]) if guard_mark else 0
    ran = re.search(r"^Ran (\d+) tests? in ", text, re.M)
    ok_lines = re.findall(r"^(test_\w+) \(([\w.]+)\) \.\.\. ok$", text, re.M)
    cases = json.loads((REPO / FIXTURE).read_text(encoding="utf-8"))["dimensions"]
    script_ids = {f"{cls}" for _, cls in ok_lines}
    expected_ids = {"__main__.JurisdictionGrammarTests.test_" + d for d in cases}
    expected_ids.add("__main__.JurisdictionGrammarTests.test_fixture_dimensions_all_covered")
    script_ok = (s_entry["exit"] == 0 and ran is not None and re.search(r"^OK$", text, re.M) is not None
                 and script_ids == expected_ids and int(ran[1]) == len(expected_ids)
                 and guards_installed > 0)
    d_entry, d_record = runner_child(run, "discovery-isolated", "discover")
    static = static_scan()
    return {
        "script_mode": {"cwd": str(fresh), "tests_run": int(ran[1]) if ran else None,
                        "ok_test_ids": sorted(script_ids), "dimensions_expected": sorted(cases),
                        "pre_import_guards_installed": guards_installed,
                        "guard_attempts": 0 if guard_mark else None,
                        "pass": script_ok},
        "discovery": {"tests_run": d_record.get("suite", {}).get("tests_run"),
                      "fixture_coverage": d_record.get("fixture_coverage"),
                      "ambient_unchanged_after_import": d_record.get("ambient_unchanged_after_import"),
                      "ambient_unchanged_after_run": d_record.get("ambient_unchanged_after_run"),
                      "peer": PEER, "pass": d_entry["child_pass"]},
        "static_host_literal_findings": static,
        "tests_run": int(ran[1]) if ran else None,
        "guard_note": "Script mode installs the test file's own terminal pre-import offline guards "
                      "(script __main__ only) and must report attempts=0; the discovery child "
                      "uses the runner's terminal guards.",
        "pass": script_ok and d_entry["child_pass"] and not any(static.values()),
    }


def phase_credentials(run, prelim, manifest):
    runtime = manifest["runtime"]
    scanner = runtime["detect_secrets"]
    argv = [scanner, "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"]
    scan, out, _ = run.child("detect-secrets", argv, REPO, stdout_name="secret-scan.json")
    report_path = run.dir / "secret-scan.json"
    try:
        report = json.loads(out)
        hits = sum(len(v) for v in report["results"].values())
        report_ok = isinstance(report["results"], dict)
    except (ValueError, KeyError, TypeError, AttributeError):
        hits, report_ok = None, False
    checker, cout, _ = run.child("check-secret-report",
                                 [sys.executable, REPO / "tools/check_secret_report.py", report_path], REPO)
    return {"scanner": scanner, "interpreter": sys.executable,
            "interpreter_is_manifest_credential_python": sys.executable == runtime["credential_scan_python3"],
            "report_valid": report_ok, "raw_report_hits": hits,
            "checker_stdout": cout.decode("utf-8", "replace").strip(),
            "pass": scan["exit"] == 0 and report_ok and checker["exit"] == 0
            and sys.executable == runtime["credential_scan_python3"]}


def phase_public_tree(run, prelim, manifest):
    runtime = manifest["runtime"]
    head = git(["rev-parse", "HEAD"])[1]
    tree = git(["rev-parse", "HEAD^{tree}"])[1]
    status = git(["status", "--porcelain=v1", "--untracked-files=all"])[1]
    visible_untracked = sorted(n for n in git(["ls-files", "-z", "--others", "--exclude-standard"],
                                              raw=True)[1].decode().split("\0") if n)
    authored_tracked = {rel: git(["ls-files", "--error-unmatch", rel])[0] == 0 for rel in AUTHORED}
    index_clean = git(["diff", "--cached", "--quiet", "HEAD"])[0] == 0
    worktree_clean = git(["diff", "--quiet"])[0] == 0
    venv = REPO / ".venv"
    venv_ok = venv.is_symlink() and os.readlink(venv) == runtime["repo_venv"]
    # Only the verified standard .venv link may be untracked (git may also ignore it).
    source_ok = (status in ("", "?? .venv") and visible_untracked in ([], [".venv"]) and venv_ok
                 and all(authored_tracked.values()) and index_clean and worktree_clean)
    inventory = {"head": head, "tree": tree, "status": status, "scan_visible_untracked": visible_untracked,
                 "authored_tracked": authored_tracked, "index_matches_head": index_clean,
                 "worktree_matches_index": worktree_clean, "venv_link_verified": venv_ok}
    run.write_new("status-inventory.json", (json.dumps(inventory, indent=2, sort_keys=True) + "\n").encode())

    clone = run.work / "committed-tree"
    c1, _, _ = run.child("git-clone", ["git", "clone", "--local", "--no-hardlinks", "--no-checkout",
                                       "--no-recurse-submodules", REPO, clone], run.work)
    c2, _, _ = run.child("git-checkout", ["git", "checkout", "--detach", head], clone)
    clone_id = identity(clone) if c1["exit"] == 0 and c2["exit"] == 0 else {}
    source_id = identity(REPO)
    clone_ok = (clone_id.get("head") == head and clone_id.get("tree") == tree
                and clone_id.get("porcelain") == "" and not (clone / ".venv").exists()
                and clone_id.get("authored_sha256") == source_id["authored_sha256"]
                and clone_id.get("scanner_sha256") == source_id["scanner_sha256"])
    scan, sout, _ = run.child("check-public-tree-committed",
                              [sys.executable, clone / "tools/check_public_tree.py"], clone)
    try:
        committed = json.loads(sout)
        committed_ok = scan["exit"] == 0 and committed.get("findings") == [] and committed.get("scanned_files", 0) > 0
    except ValueError:
        committed, committed_ok = None, False
    place, pout, _ = run.child("check-public-tree-in_place_informational",
                               [sys.executable, REPO / "tools/check_public_tree.py"], REPO)
    try:
        in_place = json.loads(pout)
        findings = in_place.get("findings")
        informational_ok = ((place["exit"] == 0 and findings == [])
                            or (place["exit"] == 1 and findings == [[".venv", 0, "symlink_not_scanned"]]
                                and venv_ok))
    except ValueError:
        in_place, findings, informational_ok = None, None, False
    after = identity(REPO)
    return {
        "source_inventory": inventory, "source_ok": source_ok,
        "committed_tree": {"clone": str(clone), "clone_identity": clone_id, "clone_ok": clone_ok,
                           "scanner_exit": scan["exit"],
                           "scanned_files": (committed or {}).get("scanned_files"),
                           "findings": (committed or {}).get("findings"), "pass": committed_ok},
        "in_place_informational": {"label": "in_place_informational (INC-174)", "exit": place["exit"],
                                   "findings": findings, "allowed_outcome": informational_ok},
        "source_head_status_after": {"head": after["head"], "porcelain": after["porcelain"],
                                     "unchanged": after["head"] == head and after["porcelain"] == status},
        "limitation": "Local disposable committed-tree clone only; not publication. The staged "
                      "tree itself is reported only as in_place_informational.",
        "pass": (source_ok and c1["exit"] == 0 and c2["exit"] == 0 and clone_ok and committed_ok
                 and informational_ok and after["head"] == head and after["porcelain"] == status),
    }


PHASE_FUNCTIONS = {"suite": phase_suite, "compatibility": phase_compatibility,
                   "portable": phase_portable, "credentials": phase_credentials,
                   "public-tree": phase_public_tree}


class RefusalDoubleRun(Run):
    """Inert scheduling double: no subprocess; each child returns a scripted result.

    Used only to qualify that a typed refusal stops scheduling and that an ordinary
    failing child does not. No guarded function is called and no guard fires.
    """

    def __init__(self, root, script):
        self.dir = root
        self.dir.mkdir(mode=0o700)
        self.work = self.dir / "work"
        self.work.mkdir(mode=0o700)
        self.children, self.files, self.script = [], [], list(script)

    def child(self, label, argv, cwd, stdout_name=None):
        kind = self.script.pop(0) if self.script else "ordinary-red"
        entry = {"label": label, "argv": [str(a) for a in argv], "cwd": str(cwd), "shell": False,
                 "double": kind, "stderr_file": f"{len(self.children):02d}-{label}.stderr"}
        argv = entry["argv"]
        result_path = Path(argv[argv.index("--result-json") + 1]) if "--result-json" in argv else None
        err = b""
        if kind == "typed-refusal":
            entry["exit"] = GUARD_REFUSAL_EXIT
            record = {"kind": GUARD_REFUSAL_KIND, "pass": False, "label": "synthetic.double",
                      "error": "synthetic double, no guard fired", "argv": argv}
            err = (SCRIPT_REFUSAL_MARK + "synthetic.double\n").encode()
        elif kind == "script-refusal":
            entry["exit"] = GUARD_REFUSAL_EXIT
            record = None
            err = (SCRIPT_REFUSAL_MARK + "synthetic.double\n").encode()
        else:  # ordinary-red: an ordinary failing child (assertion failure, expected red)
            entry["exit"] = 1
            record = {"pass": False, "suite": {"tests_run": 1, "subtest_failures": [["x", "AssertionError"]]}}
            err = b"FAIL: synthetic ordinary assertion\n"
        if result_path is not None and record is not None:
            result_path.write_text(json.dumps(record))
        self.children.append(entry)
        return entry, b"", err


def qualify_refusal_scheduling(run, manifest):
    """Qualify STOP scheduling with inert doubles (no real guard call or control)."""
    scenarios = {
        "suite_typed_refusal_first": (phase_suite, ["typed-refusal"], True, 1),
        "suite_ordinary_red_continues": (phase_suite, ["ordinary-red"] * 3, False, 3),
        "portable_script_refusal_first": (phase_portable, ["script-refusal"], True, 1),
        "portable_ordinary_red_continues": (phase_portable, ["ordinary-red"] * 2, False, 2),
    }
    root = run.work / ("refusal-double-" + uuid.uuid4().hex)
    root.mkdir(mode=0o700)
    outcome = {}
    for name, (function, script, expect_refusal, expect_children) in scenarios.items():
        double = RefusalDoubleRun(root / name, script)
        raised, verdict = None, None
        try:
            verdict = function(double, {}, manifest)
        except GuardRefusal as refusal:
            raised = refusal.info
        launched = len(double.children)
        outcome[name] = {"refusal_raised": raised is not None, "children_launched": launched,
                         "verdict_pass": None if verdict is None else verdict.get("pass"),
                         "pass": ((raised is not None) == expect_refusal and launched == expect_children
                                  and (expect_refusal or (verdict or {}).get("pass") is False))}
    return {"scenarios": outcome, "pass": all(s["pass"] for s in outcome.values())}


QUALIFIED_PHASES = ("suite", "portable")


def evidence_manifest(run):
    """Every output file outside work/ is declared exactly once and hash-valid."""
    walked = sorted(str(p.relative_to(run.dir)) for p in run.dir.rglob("*")
                    if p.is_file() and not p.is_relative_to(run.work))
    declared = sorted(run.files)
    entries = {name: sha256_file(run.dir / name) for name in declared}
    canonical = all(name == os.path.normpath(name) and not name.startswith(("/", "..")) for name in declared)
    ok = (walked == declared and len(set(declared)) == len(declared) and canonical
          and all(entries.values()))
    return {"files": entries, "walked_equals_declared": walked == declared,
            "unique": len(set(declared)) == len(declared), "canonical": canonical, "pass": ok}


def parent_main(args):
    started = utc_now().isoformat()
    out_base = Path(args.out_base).absolute()
    if out_base.is_symlink() or out_base.resolve().is_relative_to(REPO):
        print(json.dumps({"error": "out_base_must_be_outside_repo_and_not_symlink"}))
        return 2
    out_base.mkdir(parents=True, exist_ok=True)
    run = Run(args.phase, out_base)
    prelim, manifest = preflight(args.phase, args.source_manifest)
    before = identity()
    refusal = None
    if prelim["pass"]:
        qualification = None
        try:
            if args.phase in QUALIFIED_PHASES:
                qualification = qualify_refusal_scheduling(run, manifest)
            verdict = PHASE_FUNCTIONS[args.phase](run, prelim, manifest)
            if qualification is not None:
                verdict["refusal_scheduling_qualification"] = qualification
                verdict["pass"] = verdict.get("pass") is True and qualification["pass"]
        except GuardRefusal as stop:  # Genuine guard attempt: STOP, launch nothing more.
            refusal = dict(stop.info, run_dir=str(run.dir), run_identity=run.dir.name,
                           runner_argv=[sys.executable, *sys.argv],
                           disposition="STOP: genuine offline-guard refusal; preserve for the "
                                       "existing attended guard owner; do not retry")
            verdict = {"pass": False, "guard_refusal": refusal,
                       "refusal_scheduling_qualification": qualification}
        except Exception:  # An incomplete row is a failed row.
            verdict = {"pass": False, "exception": traceback.format_exc()}
    else:
        verdict = {"pass": False, "skipped_reason": "preflight_failed"}
    after = identity()
    stable = before == after
    evidence = evidence_manifest(run)
    evidence_raw = (json.dumps(evidence, indent=2, sort_keys=True) + "\n").encode()
    evidence_hash = run.write_new("evidence-manifest.json", evidence_raw)
    recheck = all(sha256_file(run.dir / n) == h for n, h in evidence["files"].items())
    passed = (prelim["pass"] and verdict.get("pass") is True and stable and evidence["pass"]
              and recheck and bool(run.children)
              and all(c.get("exit") is not None for c in run.children))
    record = {
        "phase": args.phase, "argv": [sys.executable, *sys.argv], "cwd": os.getcwd(),
        "interpreter": sys.executable, "repo": str(REPO), "run_dir": str(run.dir),
        "started_utc": started, "finished_utc": utc_now().isoformat(),
        "preflight": prelim, "candidate_head": after["head"], "candidate_tree": after["tree"],
        "identity_before": before, "identity_after": after, "identity_stable": stable,
        "fixture_sha256": sha256_file(REPO / FIXTURE), "runner_sha256": sha256_file(REPO / RUNNER),
        "children": run.children, "verdict": verdict,
        "evidence_manifest_sha256": evidence_hash, "evidence_recheck": recheck,
        "pass": passed, "exit": 0 if passed else (3 if refusal else 1), "review_required": True,
        "guard_refusal_stop": refusal,
        "semantic_assessment": "NOT_ASSESSED",
        "limitations": ["Machine validity only: REVIEW REQUIRED, not semantic/legal/human/live acceptance.",
                        "Full repository discovery and exact-head CI are operator-owned and OWED.",
                        "campaign_tool/discovery.py still uses JURISDICTION_PATTERN.match (outside write set)."],
    }
    raw = (json.dumps(record, indent=2, sort_keys=True, default=str) + "\n").encode()
    digest = run.write_new("result.json", raw)
    print(json.dumps({"phase": args.phase, "pass": passed, "run_dir": str(run.dir),
                      "result_sha256": digest, "evidence_manifest_sha256": evidence_hash,
                      "tests_run": verdict.get("tests_run"), "head": after["head"],
                      "guard_refusal_stop": refusal is not None,
                      "review_required": True}, sort_keys=True))
    return record["exit"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--phase", choices=PHASES)
    parser.add_argument("--source-manifest")
    parser.add_argument("--out-base")
    parser.add_argument("--child", choices=sorted(CHILDREN), help=argparse.SUPPRESS)
    parser.add_argument("--result-json", help=argparse.SUPPRESS)
    parser.add_argument("--temp-root", help=argparse.SUPPRESS)
    parser.add_argument("--baseline-source", help=argparse.SUPPRESS)
    parser.add_argument("--baseline-sha256", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.child:
        if not (args.result_json and args.temp_root):
            parser.error("--result-json and --temp-root are required for a child")
        return child_main(args)
    if not (args.phase and args.source_manifest and args.out_base):
        parser.error("--phase, --source-manifest and --out-base are required")
    return parent_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
