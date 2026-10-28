"""FC9-AUDIT-WINDOWS acceptance runner (offline, synthetic, REVIEW REQUIRED).

Usage (one row per invocation):
    <interpreter> tools/fc9_audit_windows_acceptance.py --phase PHASE \
        --source-manifest MANIFEST --out-base DIR

PHASE is one of suite, compatibility, portable, credentials, public-tree.
Every invocation creates a fresh UUID directory beneath DIR, redirects
``tempfile.tempdir`` into it before any product import, and writes ``run.json``
with argv/cwd/interpreter, candidate HEAD/tree, source/fixture hashes, test and
case counts, child commands (literal argv, shell disabled) with raw
stdout/stderr, and artifact hashes. Exit 0 only when every assertion of the row
holds. The repository is derived from this file. A pass is machine validity
only: REVIEW REQUIRED, never semantic, legal, human or live acceptance, and
never export or publication authority.
"""
import argparse
import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
import uuid

sys.dont_write_bytecode = True

REPO = Path(__file__).resolve().parents[1]
PHASES = ("suite", "compatibility", "portable", "credentials", "public-tree")
CHILD_PHASE = "discovery-child"
MANIFEST_SCHEMA = "agentorange.source-manifest/v1"
SUITE_MODULE = "tests.records.test_fc9_audit_windows"
COMPAT_SELECTORS = ("tests.records.test_detectors.DetectorRepairTests",)
SENTINEL_TEST = "tests.records.test_detectors.DetectorTests.test_run_all_repeatable"
AUTHORED = (
    "tests/records/test_fc9_audit_windows.py",
    "tests/fixtures/fc9-audit-windows/cases.json",
    "tools/fc9_audit_windows_acceptance.py",
)
FIXTURE = "tests/fixtures/fc9-audit-windows/cases.json"
READ_ONLY_SOURCES = (
    "campaign_tool/records/detectors/core.py",
    "campaign_tool/records/detectors/__init__.py",
    "tests/records/test_detectors.py",
)
CHECKER_FILES = (
    "tools/check_public_tree.py",
    "campaign_tool/records/public_scan.py",
    "tools/check_secret_report.py",
)
# Assembled from parts so the list does not match its own source text.
HOST_LITERALS = ("/ho" + "me/", "/Us" + "ers/", "agent" + "-work", "venv" + "-fullsuite", ".her" + "mes",
                 "/t" + "mp/", "C:" + "\\")
INLINE_FORMS = (re.compile(r"""["']-""" + r"""[ce]["']"""), re.compile("shell" + r"\s*=\s*True"),
                re.compile("PYTHON" + "PATH"), re.compile(r"\bos\.sys" + r"tem\("))
RAN = re.compile(r"^Ran (\d+) tests? in ", re.M)
ACCEPTANCE = "REVIEW REQUIRED; machine validity only, not semantic, legal, human or live acceptance"


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    return sha256_bytes(Path(path).read_bytes())


def utc():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def git(*args, cwd=None):
    done = subprocess.run(["git", *args], cwd=str(cwd or REPO), capture_output=True, timeout=300,
                          stdin=subprocess.DEVNULL, check=False)
    return done.returncode, done.stdout.decode(errors="replace"), done.stderr.decode(errors="replace")


def git_out(*args, cwd=None):
    code, out, err = git(*args, cwd=cwd)
    if code != 0:
        raise RuntimeError("git {} failed: {}".format(args[0], err.strip()[:300]))
    return out.strip()


def run_child(run_dir, label, argv, cwd, stdout_path=None, timeout=1800):
    """One literal-argv child, shell disabled; raw streams preserved beneath the run directory."""
    record = {"label": label, "argv": [str(a) for a in argv], "cwd": str(cwd), "shell": False}
    out_file = Path(stdout_path) if stdout_path else run_dir / (label + ".stdout")
    err_file = run_dir / (label + ".stderr")
    try:
        with open(out_file, "xb") as out, open(err_file, "xb") as err:
            done = subprocess.run([str(a) for a in argv], cwd=str(cwd), stdout=out, stderr=err,
                                  stdin=subprocess.DEVNULL, shell=False, timeout=timeout, check=False)
        record["exit"] = done.returncode
    except (OSError, subprocess.SubprocessError) as error:
        record["exit"] = None
        record["launch_error"] = repr(error)[:500]
    for key, path in (("stdout", out_file), ("stderr", err_file)):
        if path.is_file():
            record[key + "_path"] = str(path)
            record[key + "_sha256"] = sha256_file(path)
            record[key + "_bytes"] = path.stat().st_size
    return record


def summarise(outcome):
    return {"tests_run": outcome.testsRun,
            "failures": [case.id() for case, _ in outcome.failures],
            "errors": [case.id() for case, _ in outcome.errors],
            "skipped": [case.id() for case, _ in outcome.skipped],
            "expected_failures": [case.id() for case, _ in outcome.expectedFailures],
            "unexpected_successes": [case.id() for case in outcome.unexpectedSuccesses],
            "successful": outcome.wasSuccessful()}


def suite_ids(suite):
    ids = []

    def walk(item):
        if isinstance(item, unittest.TestSuite):
            for child in item:
                walk(child)
        else:
            ids.append(item.id())
    walk(suite)
    return ids


def run_suite(run_dir, label, suite):
    ids = suite_ids(suite)
    stream = io.StringIO()
    outcome = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    path = run_dir / (label + ".unittest.txt")
    with open(path, "x", encoding="utf-8") as handle:
        handle.write(stream.getvalue())
    record = summarise(outcome)
    record.update(label=label, loaded_ids=ids, output_path=str(path), output_sha256=sha256_file(path))
    problems = []
    if not outcome.wasSuccessful():
        problems.append(label + "_not_successful")
    if outcome.testsRun <= 0 or outcome.testsRun != len(ids):
        problems.append(label + "_test_count_mismatch")
    for key in ("skipped", "expected_failures", "unexpected_successes"):
        if record[key]:
            problems.append(label + "_" + key)
    return record, problems


def ensure_repo_path():
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))


def import_suite_module():
    ensure_repo_path()
    module = __import__(SUITE_MODULE, fromlist=["*"])
    if not Path(module.__file__).resolve().is_relative_to(REPO):
        raise RuntimeError("suite_module_outside_candidate")
    return module


# --------------------------------------------------------------- preflight


def load_manifest(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    problems = []
    if data.get("schema") != MANIFEST_SCHEMA:
        problems.append("manifest_schema_mismatch")
    if Path(str(data.get("repo", ""))).resolve() != REPO:
        problems.append("manifest_repo_not_this_checkout")
    if not re.fullmatch(r"[0-9a-f]{40}", str(data.get("head", ""))):
        problems.append("manifest_head_invalid")
    runtime = data.get("runtime")
    if not isinstance(runtime, dict):
        problems.append("manifest_runtime_missing")
    return data, problems


def identity(manifest):
    base = manifest["head"]
    info = {"head": git_out("rev-parse", "HEAD"), "tree": git_out("rev-parse", "HEAD^{tree}"),
            "base": base, "base_tree_manifest": manifest.get("tree")}
    info["base_tree_actual"] = git_out("rev-parse", base + "^{tree}")
    code, _, _ = git("merge-base", "--is-ancestor", base, "HEAD")
    info["descends_from_base"] = code == 0
    info["changed_files"] = sorted(line for line in git_out("diff", "--name-only", base, "HEAD").splitlines() if line)
    commits = [line for line in git_out("rev-list", "--reverse", base + "..HEAD").splitlines() if line]
    info["commits"] = []
    for commit in commits:
        message = git_out("show", "-s", "--format=%B", commit)
        info["commits"].append({"commit": commit, "subject": message.splitlines()[0] if message else "",
                                "signed_off": "\nSigned-off-by: " in "\n" + message,
                                "author_disclosed": "Author: " in message})
    code, status, _ = git("status", "--porcelain=v1", "--untracked-files=all")
    info["status_porcelain"] = status
    problems = []
    if info["base_tree_actual"] != manifest.get("tree"):
        problems.append("base_tree_differs_from_manifest")
    if not info["descends_from_base"]:
        problems.append("head_not_descended_from_base")
    if info["head"] == base:
        problems.append("head_is_old_base")
    if info["changed_files"] != sorted(AUTHORED):
        problems.append("changed_files_not_exact_write_set")
    if not commits or not all(c["signed_off"] for c in info["commits"]):
        problems.append("commit_not_signed_off")
    return info, problems


def interpreter_check(phase, manifest):
    runtime = manifest.get("runtime") or {}
    venv = REPO / ".venv"
    info = {"sys_executable": sys.executable, "realpath": os.path.realpath(sys.executable),
            "python_version": sys.version, "venv_link": str(venv), "venv_is_symlink": venv.is_symlink(),
            "venv_realpath": os.path.realpath(venv), "manifest_repo_venv": runtime.get("repo_venv")}
    problems = []
    if not venv.is_symlink() or os.path.realpath(venv) != os.path.realpath(str(runtime.get("repo_venv"))):
        problems.append("staged_venv_link_not_manifest_bound")
    if phase == "credentials":
        declared = str(runtime.get("credential_scan_python3"))
        info["manifest_credential_scan_python3"] = declared
        if os.path.abspath(sys.executable) != declared or info["realpath"] != os.path.realpath(declared):
            problems.append("credential_interpreter_not_manifest_literal")
    else:
        staged = str(venv / "bin" / "python3")
        info["expected_staged_interpreter"] = staged
        if os.path.abspath(sys.executable) != staged:
            problems.append("interpreter_not_staged_repo_venv")
    return info, problems


def hashes(root, names):
    return {name: (sha256_file(root / name) if (root / name).is_file() else None) for name in names}


# ------------------------------------------------------------------- rows


def phase_suite(run_dir, result, manifest):
    module = import_suite_module()
    suite = unittest.TestLoader().loadTestsFromModule(module)
    record, problems = run_suite(run_dir, "suite", suite)
    result["unittest"] = [record]
    coverage, required = module.coverage_summary(), module.required_coverage()
    result["case_dimensions"] = module.case_dimensions()
    result["case_coverage"] = {k: len(v) for k, v in coverage.items()}
    result["coverage_equals_required"] = coverage == required
    if coverage != required:
        problems.append("case_coverage_differs_from_required")
    control = module.run_negative_control()
    path = run_dir / "negative-control.json"
    with open(path, "x", encoding="utf-8") as handle:
        json.dump(control, handle, indent=2, sort_keys=True)
    result["negative_control"] = {"path": str(path), "sha256": sha256_file(path), "record": control}
    if not (control.get("caught") and control.get("restored") and control.get("restored_positive_ok")):
        problems.append("negative_control_not_caught_or_not_restored")
    if control.get("mutant_enumeration_disagreements", 0) <= 0:
        problems.append("negative_control_no_enumeration_disagreement")
    return problems


def phase_compatibility(run_dir, result, manifest):
    ensure_repo_path()
    problems, records = [], []
    for selector in COMPAT_SELECTORS:
        suite = unittest.TestLoader().loadTestsFromName(selector)
        record, found = run_suite(run_dir, "compat-" + selector.rsplit(".", 1)[-1], suite)
        record["selector"] = selector
        records.append(record)
        problems += found
    result["unittest"] = records
    result["compatibility_tests_run"] = sum(r["tests_run"] for r in records)
    result["scope_note"] = "Scoped compatibility only; full discovery = operator-run; exact-head CI OWED."
    return problems


def static_check():
    findings = []
    for name in AUTHORED:
        for number, line in enumerate((REPO / name).read_text(encoding="utf-8").splitlines(), 1):
            for token in HOST_LITERALS:
                if token in line:
                    findings.append({"file": name, "line": number, "kind": "host_specific_literal"})
            for pattern in INLINE_FORMS:
                if pattern.search(line):
                    findings.append({"file": name, "line": number, "kind": "inline_or_shell_form"})
    return findings


def phase_portable(run_dir, result, manifest, args):
    problems = []
    findings = static_check()
    result["static_check"] = {"files": list(AUTHORED), "findings": findings}
    if findings:
        problems.append("static_host_or_inline_findings")
    required = import_suite_module().required_coverage()
    # 1. Absolute script from a different fresh cwd, temp root handled before product imports.
    script_cwd = run_dir / "script-cwd"
    script_cwd.mkdir()
    script_tmp = run_dir / "script-tmp"
    coverage_path = run_dir / "script-coverage.json"
    script = run_child(run_dir, "script", [sys.executable, "-B", REPO / AUTHORED[0], "--tmp-root", script_tmp,
                                           "--coverage-out", coverage_path], script_cwd)
    result["children"].append(script)
    stderr = Path(script.get("stderr_path", run_dir / "missing")).read_text(errors="replace") \
        if script.get("stderr_path") else ""
    ran = RAN.findall(stderr)
    script["tests_run"] = int(ran[-1]) if ran else 0
    if script.get("exit") != 0 or not stderr.rstrip().endswith("OK"):
        problems.append("script_mode_failed")
    script_cov = json.loads(coverage_path.read_text()) if coverage_path.is_file() else {}
    script["coverage_sha256"] = sha256_file(coverage_path) if coverage_path.is_file() else None
    script_tempdir = script_cov.get("tempdir", "")
    script["tempdir"] = script_tempdir
    if not Path(script_tempdir).resolve().is_relative_to(script_tmp.resolve()):
        problems.append("script_tempdir_not_redirected")
    if script_cov.get("coverage") != required:
        problems.append("script_coverage_differs_from_required")
    # 2. Ordinary unittest discovery in an isolated child with before/after sentinel.
    child_out = run_dir / "discovery-child-out"
    child_cwd = run_dir / "discovery-cwd"
    child_cwd.mkdir()
    child = run_child(run_dir, "discovery-child",
                      [sys.executable, "-B", REPO / "tools" / "fc9_audit_windows_acceptance.py",
                       "--phase", CHILD_PHASE, "--source-manifest", args.source_manifest,
                       "--out-base", child_out], child_cwd)
    result["children"].append(child)
    reports = sorted(child_out.glob("*/run.json")) if child_out.is_dir() else []
    if child.get("exit") != 0 or len(reports) != 1:
        problems.append("discovery_child_failed")
        return problems
    report = json.loads(reports[0].read_text())
    child["report_path"] = str(reports[0])
    child["report_sha256"] = sha256_file(reports[0])
    result["discovery"] = {k: report.get(k) for k in ("discovery", "sentinel_before", "sentinel_after",
                                                     "ambient_changes", "audit_events", "problems")}
    if report.get("discovery", {}).get("coverage") != required:
        problems.append("discovery_coverage_differs_from_required")
    result["coverage"] = {"required": {k: len(v) for k, v in required.items()},
                          "script_equal": script_cov.get("coverage") == required,
                          "discovery_equal": report.get("discovery", {}).get("coverage") == required,
                          "script_tests_run": script["tests_run"],
                          "discovery_tests_run": report.get("discovery", {}).get("tests_run")}
    return problems


def _ambient():
    return {"environ": dict(os.environ), "tempdir": tempfile.tempdir, "gettempdir": tempfile.gettempdir(),
            "sys_path": list(sys.path), "cwd": os.getcwd(), "dont_write_bytecode": sys.dont_write_bytecode,
            "argv": list(sys.argv)}


def _diff(before, after):
    return sorted(key for key in before if before[key] != after[key])


def phase_discovery_child(run_dir, result, manifest):
    """Isolated child: sentinel, ordinary discovery import/run, sentinel again; record ambient drift."""
    problems = []
    events = []
    watched = ("sys.addaudithook", "socket.connect", "socket.getaddrinfo", "socket.bind", "subprocess.Popen",
               "os.system", "os.exec", "os.posix_spawn", "urllib.Request")
    recording = {"on": False}

    def hook(event, _args):
        if recording["on"] and event in watched:
            events.append(event)
    sys.addaudithook(hook)  # Child-only sentinel; never installed in a discovery process under test.
    ensure_repo_path()
    loader = unittest.TestLoader()
    before_record, found = run_suite(run_dir, "sentinel-before", loader.loadTestsFromName(SENTINEL_TEST))
    problems += found
    result["sentinel_before"] = before_record
    if SUITE_MODULE in sys.modules:
        problems.append("suite_module_preloaded")
    ambient_start = _ambient()
    recording["on"] = True
    suite = loader.discover(start_dir=str(REPO / "tests" / "records"), pattern="test_fc9_audit_windows.py",
                            top_level_dir=str(REPO))
    ambient_import = _ambient()
    record, found = run_suite(run_dir, "discovery", suite)
    ambient_run = _ambient()
    recording["on"] = False
    problems += found
    module = sys.modules.get(SUITE_MODULE)
    if module is None or not Path(module.__file__).resolve().is_relative_to(REPO):
        problems.append("discovered_module_missing_or_outside_candidate")
        coverage = None
    else:
        coverage = module.coverage_summary()
    record["coverage"] = coverage
    result["discovery"] = record
    result["ambient_changes"] = {"after_import": _diff(ambient_start, ambient_import),
                                 "after_run": _diff(ambient_start, ambient_run)}
    if result["ambient_changes"]["after_import"] or result["ambient_changes"]["after_run"]:
        problems.append("discovery_changed_ambient_state")
    result["audit_events"] = events
    if events:
        problems.append("discovery_raised_watched_audit_events")
    after_record, found = run_suite(run_dir, "sentinel-after", loader.loadTestsFromName(SENTINEL_TEST))
    problems += found
    result["sentinel_after"] = after_record
    return problems


def phase_credentials(run_dir, result, manifest):
    problems = []
    scanner = Path(str((manifest.get("runtime") or {}).get("detect_secrets")))
    result["scanner"] = {"path": str(scanner), "present": scanner.is_file()}
    if not scanner.is_file():
        return ["detect_secrets_missing_stop_for_operator"]
    report = run_dir / "secret-scan.json"
    scan = run_child(run_dir, "detect-secrets-scan",
                     [scanner, "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"], REPO,
                     stdout_path=report)
    result["children"].append(scan)
    result["children"].append(run_child(run_dir, "detect-secrets-version", [scanner, "--version"], REPO))
    if scan.get("exit") != 0:
        problems.append("secret_scan_failed")
    try:
        data = json.loads(report.read_text())
        scanned = data["results"]
        if not isinstance(scanned, dict):
            raise ValueError("results")
        result["secret_scan"] = {"files_with_hits": len(scanned),
                                 "raw_hits": sum(len(v) for v in scanned.values()),
                                 "authored_files_with_hits": sorted(set(scanned) & set(AUTHORED))}
    except (OSError, ValueError, KeyError, TypeError):
        return problems + ["secret_report_malformed"]
    checker = run_child(run_dir, "check-secret-report",
                        [sys.executable, "-B", REPO / "tools" / "check_secret_report.py", report], REPO)
    result["children"].append(checker)
    text = Path(checker["stdout_path"]).read_text(errors="replace") if checker.get("stdout_path") else ""
    match = re.search(r"potential secrets: (\d+);", text)
    result["secret_scan"]["potential_secrets"] = int(match.group(1)) if match else None
    if checker.get("exit") != 0:
        problems.append("check_secret_report_failed")
    if result["secret_scan"]["potential_secrets"] != 0:
        problems.append("potential_secrets_nonzero_or_unreported")
    return problems


def _status_entries():
    code, out, _ = git("status", "--porcelain=v1", "-z", "--untracked-files=all")
    entries = [item for item in out.split("\0") if item]
    return code, out, entries


def phase_public_tree(run_dir, result, manifest):
    problems = []
    head, tree = result["git"]["head"], result["git"]["tree"]
    code, raw_status, entries = _status_entries()
    others = sorted(x for x in git_out("ls-files", "-z", "--others", "--exclude-standard").split("\0") if x)
    tracked = {name: git("ls-files", "--error-unmatch", name)[0] == 0 for name in AUTHORED}
    unstaged = git("diff", "--quiet")[0]
    staged = git("diff", "--cached", "--quiet", "HEAD")[0]
    inventory = {"status_exit": code, "status_porcelain_z": raw_status, "untracked_scan_visible": others,
                 "authored_tracked": tracked, "worktree_equals_index": unstaged == 0,
                 "index_equals_head": staged == 0, "head": head, "tree": tree}
    result["source_inventory"] = inventory
    if code != 0 or [e for e in entries if not e.startswith("?? ")]:
        problems.append("tracked_changes_present")
    if [name for name in others if name != ".venv"]:
        problems.append("untracked_scan_visible_files")
    if not all(tracked.values()) or unstaged != 0 or staged != 0:
        problems.append("authored_files_not_committed")
    # Committed-tree clone: separate shell-disabled argv calls; never link staged runtime paths.
    row = run_dir / "committed-tree"
    row.mkdir()
    clone = row / "clone"
    result["children"].append(run_child(run_dir, "git-clone", ["git", "clone", "--local", "--no-hardlinks",
                                                               "--no-checkout", "--no-recurse-submodules",
                                                               REPO, clone], row))
    result["children"].append(run_child(run_dir, "git-checkout",
                                        ["git", "-C", clone, "checkout", "--detach", head], row))
    if any(child.get("exit") != 0 for child in result["children"][-2:]):
        return problems + ["committed_tree_clone_failed"]
    clone_head, clone_tree = git_out("rev-parse", "HEAD", cwd=clone), git_out("rev-parse", "HEAD^{tree}", cwd=clone)
    names = AUTHORED + CHECKER_FILES
    source_hashes, clone_hashes = hashes(REPO, names), hashes(clone, names)
    clone_venv = clone / ".venv"
    result["committed_tree"] = {"clone": str(clone), "head": clone_head, "tree": clone_tree,
                                "hashes_equal": source_hashes == clone_hashes, "hashes": clone_hashes,
                                "venv_present_in_clone": os.path.lexists(clone_venv)}
    if clone_head != head or clone_tree != tree:
        problems.append("clone_identity_differs")
    if source_hashes != clone_hashes:
        problems.append("clone_file_hashes_differ")
    if os.path.lexists(clone_venv):
        problems.append("staged_runtime_linked_into_clone")
    recheck = {"head": git_out("rev-parse", "HEAD"), "status": _status_entries()[1]}
    result["source_recheck"] = recheck
    if recheck["head"] != head or recheck["status"] != raw_status:
        problems.append("source_changed_during_row")
    scan = run_child(run_dir, "check-public-tree-committed",
                     [sys.executable, "-B", clone / "tools" / "check_public_tree.py"], clone)
    result["children"].append(scan)
    try:
        report = json.loads(Path(scan["stdout_path"]).read_text())
        result["committed_tree"]["scan"] = {"findings": report.get("findings"),
                                            "scanned_files": report.get("scanned_files")}
        if scan.get("exit") != 0 or report.get("findings") != []:
            problems.append("committed_tree_public_scan_findings")
    except (OSError, ValueError, KeyError):
        problems.append("committed_tree_public_scan_unparseable")
    # INC-174 in-place informational scan of the staged checkout (not a staged-tree pass).
    place = run_child(run_dir, "check-public-tree-in-place", [sys.executable, "-B", REPO / "tools" / "check_public_tree.py"],
                      REPO)
    place["label_inc"] = "INC-174 in_place_informational"
    result["children"].append(place)
    info = {"label": "INC-174 in_place_informational", "exit": place.get("exit"),
            "staged_tree_pass_claimed": False}
    try:
        report = json.loads(Path(place["stdout_path"]).read_text())
        info["findings"] = report.get("findings")
        venv_ok = os.path.realpath(REPO / ".venv") == os.path.realpath(str(manifest["runtime"]["repo_venv"]))
        info["venv_link_manifest_bound"] = venv_ok
        allowed = (place.get("exit") == 0 and report.get("findings") == []) or (
            place.get("exit") == 1 and report.get("findings") == [[".venv", 0, "symlink_not_scanned"]] and venv_ok)
        info["outcome"] = "allowed_informational" if allowed else "disallowed"
        if not allowed:
            problems.append("in_place_informational_outcome_disallowed")
    except (OSError, ValueError, KeyError, TypeError):
        info["outcome"] = "unparseable"
        problems.append("in_place_informational_unparseable")
    result["in_place_informational"] = info
    return problems


# ------------------------------------------------------------------- main


def artifact_hashes(run_dir):
    found = {}
    for path in sorted(run_dir.rglob("*")):
        relative = path.relative_to(run_dir)
        if relative.parts[0] in ("committed-tree", "tmp") or path.is_symlink() or not path.is_file():
            continue
        found[str(relative)] = sha256_file(path)
    return found


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--phase", required=True, choices=PHASES + (CHILD_PHASE,))
    parser.add_argument("--source-manifest", required=True)
    parser.add_argument("--out-base", required=True)
    args = parser.parse_args(argv)
    out_base = Path(args.out_base).resolve()
    if out_base.is_relative_to(REPO):
        parser.error("--out-base must be outside the repository")
    out_base.mkdir(parents=True, exist_ok=True)
    run_dir = out_base / "{}-{}-{}".format(args.phase, utc(), uuid.uuid4().hex)
    run_dir.mkdir(exist_ok=False)
    tmp = run_dir / "tmp"
    tmp.mkdir()
    tempfile.tempdir = str(tmp)  # Before any product or test-module import.
    result = {"schema": "fc9-audit-windows-acceptance-v1", "phase": args.phase,
              "argv": [sys.executable] + list(sys.argv), "cwd": os.getcwd(), "interpreter": sys.executable,
              "repo_root": str(REPO), "run_dir": str(run_dir), "tempdir": tempfile.tempdir,
              "started_utc": utc(), "children": [], "acceptance": ACCEPTANCE,
              "author_runtime": "claude-opus-5-5 / anthropic (opus-coder-bopus)",
              "full_discovery": "operator-run", "exact_head_ci": "OWED"}
    problems = []
    try:
        manifest, found = load_manifest(args.source_manifest)
        problems += found
        result["source_manifest"] = {"path": str(Path(args.source_manifest).resolve()),
                                     "sha256": sha256_file(args.source_manifest)}
        info, found = interpreter_check("credentials" if args.phase == "credentials" else args.phase, manifest)
        result["interpreter_check"] = info
        problems += found
        result["git"], found = identity(manifest)
        problems += found
        result["authored_hashes"] = hashes(REPO, AUTHORED)
        result["fixture_sha256"] = result["authored_hashes"][FIXTURE]
        result["read_only_source_hashes"] = hashes(REPO, READ_ONLY_SOURCES)
        result["checker_hashes"] = hashes(REPO, CHECKER_FILES)
        if not problems:
            if args.phase == "suite":
                problems += phase_suite(run_dir, result, manifest)
            elif args.phase == "compatibility":
                problems += phase_compatibility(run_dir, result, manifest)
            elif args.phase == "portable":
                problems += phase_portable(run_dir, result, manifest, args)
            elif args.phase == "credentials":
                problems += phase_credentials(run_dir, result, manifest)
            elif args.phase == "public-tree":
                problems += phase_public_tree(run_dir, result, manifest)
            else:
                problems += phase_discovery_child(run_dir, result, manifest)
    except Exception as error:  # Incomplete row: fail closed and keep the class/message.
        problems.append("row_incomplete:" + type(error).__name__)
        result["exception"] = repr(error)[:2000]
    result["finished_utc"] = utc()
    result["problems"] = problems
    result["verdict"] = "PASS_REVIEW_REQUIRED" if not problems else "FAIL"
    result["artifacts"] = artifact_hashes(run_dir)
    with open(run_dir / "run.json", "x", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2, sort_keys=True, default=str)
    print(json.dumps({"phase": args.phase, "verdict": result["verdict"], "problems": problems,
                      "run_dir": str(run_dir), "head": result.get("git", {}).get("head"),
                      "tree": result.get("git", {}).get("tree"),
                      "run_json_sha256": sha256_file(run_dir / "run.json")}, sort_keys=True))
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
