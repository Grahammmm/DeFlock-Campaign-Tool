#!/usr/bin/env python3
"""FC9-DIGEST-LEGAL-DOWNGRADE acceptance runner (five separately invoked rows).

Rows: suite, compatibility, portable, credentials, public-tree. Each
invocation writes a fresh UUID directory beneath ``--out-base`` with exact
argv/cwd/interpreter, exit codes, raw stdout/stderr, candidate HEAD/tree,
fixture and source hashes, test/case counts and artifact hashes. Every child
command is a literal argv list with the shell disabled. The repository is
derived from this file's location. Machine-valid output is REVIEW REQUIRED,
never semantic, legal, human or live acceptance.
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TEST_REL = "tests/records/test_fc9_digest_legal_downgrade.py"
TEST_MODULE = "tests.records.test_fc9_digest_legal_downgrade"
FIXTURE_REL = "tests/fixtures/fc9-digest-legal-downgrade/cases.json"
RUNNER_REL = "tools/fc9_digest_legal_downgrade_acceptance.py"
AUTHORED = [TEST_REL, FIXTURE_REL, RUNNER_REL]
PRODUCT_SOURCES = ["campaign_tool/digest/build.py", "campaign_tool/digest/schema.py",
                   "campaign_tool/digest/model.py", "campaign_tool/law.py", "tests/runner/test_digest.py"]
CHECKER_FILES = ["tools/check_public_tree.py", "campaign_tool/records/public_scan.py",
                 "tools/check_secret_report.py", ".gitignore"]
COMPAT_SELECTORS = ["tests.runner.test_digest.RedactionTests", "tests.runner.test_digest.DetectorTests"]
COMPAT_EXPECTED_TESTS = 4
SENTINEL = "tests.test_markdown_lite.MarkdownLiteTests.test_raw_html_is_escaped"
MANIFEST_SCHEMA = "agentorange.source-manifest/v1"
VERDICT = "REVIEW REQUIRED: machine-valid output only; not semantic, legal, human or live acceptance"
# Host-specific literal fragments, assembled so this file does not itself match.
HOST_LITERALS = ["/" + "home" + "/", "/" + "Users" + "/", "/" + "root" + "/", "/" + "tmp" + "/",
                 "agent" + "-work", "." + "hermes", "C:" + "\\"]
HEX40 = re.compile(r"[0-9a-f]{40}")


class Stop(Exception):
    """Acceptance precondition or row failure."""


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class Row:
    def __init__(self, phase, out):
        self.phase = phase
        self.out = out
        self.commands = []
        self.checks = []
        self.counter = 0

    def run(self, name, argv, cwd, timeout=1800, stdout_path=None):
        argv = [str(item) for item in argv]
        self.counter += 1
        stem = f"{self.counter:02d}-{name}"
        stdout_file = Path(stdout_path) if stdout_path else self.out / f"{stem}.stdout"
        stderr_file = self.out / f"{stem}.stderr"
        started = now()
        completed = subprocess.run(argv, cwd=str(cwd), shell=False, stdin=subprocess.DEVNULL,
                                   capture_output=True, timeout=timeout, check=False)
        stdout_file.write_bytes(completed.stdout)
        stderr_file.write_bytes(completed.stderr)
        record = {"name": name, "argv": argv, "cwd": str(cwd), "shell": False, "started": started,
                  "ended": now(), "exit": completed.returncode,
                  "stdout_path": str(stdout_file), "stdout_sha256": hashlib.sha256(completed.stdout).hexdigest(),
                  "stderr_path": str(stderr_file), "stderr_sha256": hashlib.sha256(completed.stderr).hexdigest()}
        self.commands.append(record)
        return completed, record

    def check(self, name, ok, detail=None):
        self.checks.append({"check": name, "ok": bool(ok), "detail": detail})
        return bool(ok)


def git(row, name, *args, cwd=REPO):
    completed, record = row.run(name, ["git", *args], cwd)
    return completed.returncode, completed.stdout.decode("utf-8", "replace").strip(), record


def load_manifest(path):
    manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise Stop("manifest_schema_mismatch")
    for key in ("repo", "head", "tree"):
        if not isinstance(manifest.get(key), str):
            raise Stop(f"manifest_missing_{key}")
    if not HEX40.fullmatch(manifest["head"]) or not HEX40.fullmatch(manifest["tree"]):
        raise Stop("manifest_identity_not_full_sha")
    if Path(manifest["repo"]).resolve() != REPO:
        raise Stop("manifest_repo_is_not_runner_repo")
    if not isinstance(manifest.get("runtime"), dict):
        raise Stop("manifest_missing_runtime")
    return manifest


def identity(row, manifest):
    """Candidate HEAD/tree, descent from the pinned base and source hashes."""
    base = manifest["head"]
    _, head, _ = git(row, "rev-parse-head", "rev-parse", "--verify", "HEAD^{commit}")
    _, tree, _ = git(row, "rev-parse-tree", "rev-parse", "--verify", "HEAD^{tree}")
    _, base_tree, _ = git(row, "rev-parse-base-tree", "rev-parse", "--verify", base + "^{tree}")
    ancestor, _, _ = git(row, "merge-base-is-ancestor", "merge-base", "--is-ancestor", base, head)
    row.check("base_tree_matches_manifest", base_tree == manifest["tree"], {"base": base, "tree": base_tree})
    row.check("candidate_descends_from_base", ancestor == 0, {"base": base, "head": head})
    row.check("candidate_is_not_base", head != base, {"head": head})
    changed_code, changed, _ = git(row, "diff-name-only-base", "diff", "--name-only", base, head)
    changed_paths = sorted(p for p in changed.splitlines() if p)
    row.check("candidate_changes_only_authored_files", changed_code == 0 and changed_paths == sorted(AUTHORED),
              changed_paths)
    unchanged = {}
    for rel in PRODUCT_SOURCES:
        code, _, _ = git(row, "diff-quiet-" + Path(rel).name, "diff", "--quiet", base, head, "--", rel)
        unchanged[rel] = {"unchanged_from_base": code == 0, "sha256": sha256_file(REPO / rel)}
    row.check("product_sources_unchanged_from_base", all(v["unchanged_from_base"] for v in unchanged.values()), unchanged)
    return {"base": base, "base_tree": base_tree, "head": head, "tree": tree,
            "authored_sha256": {rel: sha256_file(REPO / rel) for rel in AUTHORED},
            "fixture_sha256": sha256_file(REPO / FIXTURE_REL), "product_sources": unchanged}


def check_interpreter(row, manifest, phase):
    executable = os.path.abspath(sys.executable)
    runtime = manifest["runtime"]
    if phase == "credentials":
        expected = os.path.abspath(runtime.get("credential_scan_python3") or "")
        ok = executable == expected
    else:
        expected = str(REPO / ".venv" / "bin" / "python3")
        ok = executable == expected
    venv_link = REPO / ".venv"
    link_ok = venv_link.is_symlink() and venv_link.resolve() == Path(runtime.get("repo_venv") or "").resolve()
    row.check("interpreter_is_declared_literal", ok, {"executable": executable, "expected": expected})
    row.check("staged_venv_link_is_manifest_repo_venv", link_ok,
              {"link": os.readlink(venv_link) if venv_link.is_symlink() else None,
               "manifest_repo_venv": runtime.get("repo_venv")})
    if not ok:
        raise Stop("interpreter_mismatch")


def child_result(row, name):
    path = row.out / name / "result.json"
    if not path.is_file():
        row.check(f"{name}_result_present", False, str(path))
        return None
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- rows

def row_suite(row, manifest):
    child = row.out / "child-suite"
    completed, _ = row.run("child-suite", [sys.executable, REPO / RUNNER_REL, "--child", "suite", "--child-out", child], REPO)
    result = child_result(row, "child-suite")
    row.check("child_suite_exit_zero", completed.returncode == 0, completed.returncode)
    if result:
        row.check("suite_successful", result["success"], {k: result[k] for k in ("tests_run", "failures", "errors", "skipped")})
        row.check("suite_zero_skipped", result["skipped"] == 0, result["skipped"])
        row.check("suite_required_cases_covered", not result["missing"] and not result["unexpected"]
                  and result["covered"] == result["required"], {"missing": result["missing"], "unexpected": result["unexpected"]})
        control = result["negative_control"]
        row.check("negative_control_killed", control.get("killed") is True, control)
    return result


def row_compatibility(row, manifest):
    child = row.out / "child-compat"
    completed, _ = row.run("child-compat", [sys.executable, REPO / RUNNER_REL, "--child", "compat", "--child-out", child], REPO)
    result = child_result(row, "child-compat")
    row.check("child_compat_exit_zero", completed.returncode == 0, completed.returncode)
    if result:
        row.check("compat_successful", result["success"], result)
        row.check("compat_exact_test_count", result["tests_run"] == COMPAT_EXPECTED_TESTS, result["tests_run"])
        row.check("compat_zero_skipped", result["skipped"] == 0, result["skipped"])
    return result


def coverage_line(stderr):
    for line in stderr.decode("utf-8", "replace").splitlines():
        if line.startswith("FC9-CASE-COVERAGE "):
            return json.loads(line[len("FC9-CASE-COVERAGE "):])
    return None


def row_portable(row, manifest):
    script_cwd = row.out / "script-cwd"
    script_tmp = row.out / "script-tmp"
    script_cwd.mkdir()
    script_tmp.mkdir()
    completed, _ = row.run("script-absolute", [sys.executable, REPO / TEST_REL, "--tmp-root", script_tmp], script_cwd)
    script = coverage_line(completed.stderr)
    row.check("script_exit_zero", completed.returncode == 0, completed.returncode)
    row.check("script_cwd_is_not_repo", script_cwd.resolve() != REPO and REPO not in script_cwd.resolve().parents,
              str(script_cwd))
    row.check("script_full_coverage", bool(script) and not script["missing"] and not script["unexpected"]
              and script["covered"] == script["required"] and script["skipped"] == 0, script)
    discover_cwd = row.out / "discover-cwd"
    discover_cwd.mkdir()
    child = row.out / "child-discover"
    completed, _ = row.run("child-discover", [sys.executable, REPO / RUNNER_REL, "--child", "discover", "--child-out", child],
                           discover_cwd)
    result = child_result(row, "child-discover")
    row.check("discover_exit_zero", completed.returncode == 0, completed.returncode)
    if result:
        row.check("discover_successful", result["success"], {k: result[k] for k in ("tests_run", "failures", "errors", "skipped")})
        row.check("discover_full_coverage", not result["missing"] and not result["unexpected"]
                  and result["covered"] == result["required"], {"missing": result["missing"], "unexpected": result["unexpected"]})
        row.check("discover_sentinel_passes_before_and_after", result["sentinel_before"] and result["sentinel_after"],
                  {"before": result["sentinel_before"], "after": result["sentinel_after"]})
        row.check("discover_import_and_run_leave_ambient_state", result["state_unchanged_after_import"]
                  and result["state_unchanged_after_run"], result["state_differences"])
        if script:
            row.check("script_and_discovery_equal_required_coverage", script["required"] == result["required"]
                      and script["covered"] == result["covered"], {"script": script["covered"], "discovery": result["covered"]})
    literals = {}
    for rel in AUTHORED:
        text = (REPO / rel).read_text(encoding="utf-8")
        hits = [fragment for fragment in HOST_LITERALS if fragment in text]
        literals[rel] = hits
    row.check("authored_files_have_no_host_literals", not any(literals.values()), literals)
    return {"script": script, "discovery": result, "host_literals": literals}


def row_credentials(row, manifest):
    runtime = manifest["runtime"]
    scanner = runtime.get("detect_secrets")
    if not scanner or not Path(scanner).is_file() or not os.access(scanner, os.X_OK):
        row.check("detect_secrets_present", False, scanner)
        raise Stop("detect_secrets_unavailable_for_operator")
    report = row.out / "secret-scan.json"
    completed, _ = row.run("detect-secrets-scan", [scanner, "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"],
                           REPO, stdout_path=report)
    row.check("detect_secrets_exit_zero", completed.returncode == 0, completed.returncode)
    try:
        parsed = json.loads(report.read_text(encoding="utf-8"))
    except ValueError:
        parsed = None
    row.check("detect_secrets_report_is_json", isinstance(parsed, dict), None)
    results = (parsed or {}).get("results", {})
    staged = sorted(path for path in results if path == ".venv" or path.startswith(".venv/"))
    row.check("scanner_did_not_report_staged_link_paths", not staged, staged)
    authored = {rel: results.get(rel, []) for rel in AUTHORED}
    row.check("authored_files_have_no_scanner_results", not any(authored.values()), authored)
    checked, _ = row.run("check-secret-report", [sys.executable, REPO / "tools" / "check_secret_report.py", report], REPO)
    row.check("check_secret_report_exit_zero", checked.returncode == 0, checked.returncode)
    return {"scanner": scanner, "report": str(report), "report_sha256": sha256_file(report),
            "result_files": len(results), "plugins": len((parsed or {}).get("plugins_used", []))}


def row_public_tree(row, manifest, ident):
    head, tree = ident["head"], ident["tree"]
    _, status, _ = git(row, "status-porcelain", "status", "--porcelain=v1", "--untracked-files=all")
    _, ignored, _ = git(row, "status-ignored", "status", "--porcelain=v1", "--untracked-files=all", "--ignored")
    _, others, _ = git(row, "ls-files-others", "ls-files", "--others", "--exclude-standard")
    tracked_code, tracked, _ = git(row, "ls-files-authored", "ls-files", "--error-unmatch", *AUTHORED)
    worktree_code, _, _ = git(row, "diff-worktree-quiet", "diff", "--quiet")
    index_code, _, _ = git(row, "diff-index-quiet", "diff", "--cached", "--quiet", "HEAD")
    row.check("status_clean", status == "", status)
    row.check("only_standard_venv_untracked_scan_visible", set(others.splitlines()) <= {".venv"}, others.splitlines())
    row.check("authored_files_tracked", tracked_code == 0 and sorted(tracked.splitlines()) == sorted(AUTHORED), tracked)
    row.check("worktree_and_index_equal_head", worktree_code == 0 and index_code == 0, [worktree_code, index_code])
    inventory = {"status": status, "status_with_ignored": ignored.splitlines(), "others": others.splitlines(),
                 "head": head, "tree": tree}
    clone = row.out / "committed-tree"
    code, _, _ = git(row, "clone-local", "clone", "--local", "--no-hardlinks", "--no-checkout", "--no-recurse-submodules",
                     str(REPO), str(clone), cwd=row.out)
    row.check("clone_exit_zero", code == 0, code)
    code, _, _ = git(row, "clone-checkout", "-C", str(clone), "checkout", "--detach", head, cwd=row.out)
    row.check("clone_checkout_exit_zero", code == 0, code)
    _, clone_head, _ = git(row, "clone-head", "-C", str(clone), "rev-parse", "--verify", "HEAD^{commit}", cwd=row.out)
    _, clone_tree, _ = git(row, "clone-tree", "-C", str(clone), "rev-parse", "--verify", "HEAD^{tree}", cwd=row.out)
    row.check("clone_identity_equals_candidate", clone_head == head and clone_tree == tree,
              {"clone_head": clone_head, "clone_tree": clone_tree})
    row.check("clone_has_no_staged_runtime_link", not (clone / ".venv").exists() and not (clone / ".venv").is_symlink(), None)
    compare = {}
    for rel in AUTHORED + CHECKER_FILES:
        compare[rel] = {"candidate": sha256_file(REPO / rel), "clone": sha256_file(clone / rel)}
    row.check("clone_authored_and_checker_hashes_equal", all(v["candidate"] == v["clone"] for v in compare.values()), compare)
    scan, _ = row.run("clone-check-public-tree", [sys.executable, "-B", clone / "tools" / "check_public_tree.py"], clone)
    try:
        scan_json = json.loads(scan.stdout.decode("utf-8"))
    except ValueError:
        scan_json = None
    row.check("clone_public_tree_exit_zero", scan.returncode == 0, scan.returncode)
    row.check("clone_public_tree_json_no_findings", isinstance(scan_json, dict) and scan_json.get("findings") == [],
              (scan_json or {}).get("findings"))
    in_place, _ = row.run("in-place-informational-INC-174", [sys.executable, "-B", REPO / "tools" / "check_public_tree.py"], REPO)
    try:
        in_place_json = json.loads(in_place.stdout.decode("utf-8"))
    except ValueError:
        in_place_json = None
    findings = (in_place_json or {}).get("findings")
    link = REPO / ".venv"
    link_ok = link.is_symlink() and link.resolve() == Path(manifest["runtime"].get("repo_venv") or "").resolve()
    allowed = isinstance(in_place_json, dict) and (
        (in_place.returncode == 0 and findings == [])
        or (in_place.returncode == 1 and findings == [[".venv", 0, "symlink_not_scanned"]] and link_ok))
    row.check("in_place_informational_INC_174_allowed_outcome", allowed,
              {"exit": in_place.returncode, "findings": findings, "venv_link_is_manifest_venv": link_ok,
               "label": "in_place_informational INC-174; not a staged-tree pass"})
    _, head_after, _ = git(row, "recheck-head", "rev-parse", "--verify", "HEAD^{commit}")
    _, status_after, _ = git(row, "recheck-status", "status", "--porcelain=v1", "--untracked-files=all")
    row.check("source_head_and_status_unchanged", head_after == head and status_after == status,
              {"head_after": head_after, "status_after": status_after})
    return {"inventory": inventory, "clone": str(clone), "clone_scan": scan_json,
            "in_place_informational": {"label": "INC-174", "exit": in_place.returncode, "result": in_place_json}}


# ---------------------------------------------------------------- children

def child_prepare(out):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=False)
    tmp = out / "tmp"
    tmp.mkdir()
    tempfile.tempdir = str(tmp)
    sys.dont_write_bytecode = True
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    return out


def run_tests(suite):
    import unittest
    result = unittest.TextTestRunner(stream=sys.stderr, verbosity=2).run(suite)
    return {"tests_run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
            "skipped": len(result.skipped), "success": result.wasSuccessful(),
            "failure_text": [text for _, text in result.failures + result.errors]}


def coverage(module):
    required = module.required_case_ids()
    return {"required": len(required), "covered": len(module.COVERED & required),
            "missing": sorted(required - module.COVERED), "unexpected": sorted(module.COVERED - required),
            "dimensions": module.case_dimensions()}


def child_suite(out):
    out = child_prepare(out)
    import importlib
    import unittest
    module = importlib.import_module(TEST_MODULE)
    outcome = run_tests(unittest.TestLoader().loadTestsFromName(TEST_MODULE))
    outcome.update(coverage(module))
    outcome["negative_control"] = module.run_negative_control()
    outcome["negative_control_labels"] = {
        "injected_behavior": "injected_behavior", "baseline": "baseline_unpatched_observed",
        "under_injection": "patched_observed", "final_candidate": "final_candidate_observed"}
    (out / "result.json").write_text(json.dumps(outcome, indent=2, sort_keys=True), encoding="utf-8")
    ok = (outcome["success"] and not outcome["skipped"] and not outcome["missing"] and not outcome["unexpected"]
          and outcome["negative_control"]["killed"])
    return 0 if ok else 1


def child_compat(out):
    out = child_prepare(out)
    import unittest
    outcome = run_tests(unittest.TestLoader().loadTestsFromNames(COMPAT_SELECTORS))
    outcome["selectors"] = COMPAT_SELECTORS
    (out / "result.json").write_text(json.dumps(outcome, indent=2, sort_keys=True), encoding="utf-8")
    return 0 if outcome["success"] and not outcome["skipped"] else 1


def ambient_state():
    import builtins
    import socket
    digest_build = sys.modules.get("campaign_tool.digest.build")
    return {
        "environ_sha256": hashlib.sha256(json.dumps(sorted(os.environ.items())).encode()).hexdigest(),
        "tempfile_tempdir": tempfile.tempdir,
        "sys_path": list(sys.path),
        "cwd": os.getcwd(),
        "dont_write_bytecode": sys.dont_write_bytecode,
        "socket_connect_is_original": "connect" not in vars(socket.socket),
        "socket_create_connection": getattr(socket.create_connection, "__module__", None),
        "builtins_any_is_builtin": builtins.any.__module__ == "builtins",
        "build_module_local_any": None if digest_build is None else ("any" in vars(digest_build)),
        "build_narrative_is_original": None if digest_build is None else (
            getattr(digest_build.narrative, "__module__", None) == "campaign_tool.digest.model"),
    }


def diff_state(before, after):
    keys = sorted(set(before) | set(after))
    return {key: [before.get(key), after.get(key)] for key in keys
            if key not in {"build_module_local_any", "build_narrative_is_original"} and before.get(key) != after.get(key)}


def child_discover(out):
    out = child_prepare(out)
    import unittest
    loader = unittest.TestLoader()
    sentinel_before = run_tests(loader.loadTestsFromName(SENTINEL))
    state0 = ambient_state()
    suite = loader.discover(start_dir=str(REPO / "tests" / "records"), pattern=Path(TEST_REL).name,
                            top_level_dir=str(REPO))
    state1 = ambient_state()
    module = sys.modules.get(TEST_MODULE)
    outcome = run_tests(suite)
    state2 = ambient_state()
    sentinel_after = run_tests(loader.loadTestsFromName(SENTINEL))
    outcome.update(coverage(module) if module else {"required": 0, "covered": 0, "missing": ["module-not-imported"],
                                                    "unexpected": [], "dimensions": {}})
    differences = {"after_import": diff_state(state0, state1), "after_run": diff_state(state1, state2)}
    patched_after = state2["build_module_local_any"] is not False or state2["build_narrative_is_original"] is not True
    outcome.update({
        "discovered_module": TEST_MODULE if module else None,
        "sentinel": SENTINEL,
        "sentinel_before": sentinel_before["success"] and sentinel_before["tests_run"] == 1,
        "sentinel_after": sentinel_after["success"] and sentinel_after["tests_run"] == 1,
        "state_before": state0, "state_after_import": state1, "state_after_run": state2,
        "state_differences": differences,
        "state_unchanged_after_import": not differences["after_import"],
        "state_unchanged_after_run": not differences["after_run"] and not patched_after,
    })
    (out / "result.json").write_text(json.dumps(outcome, indent=2, sort_keys=True), encoding="utf-8")
    ok = (outcome["success"] and not outcome["missing"] and not outcome["unexpected"] and outcome["sentinel_before"]
          and outcome["sentinel_after"] and outcome["state_unchanged_after_import"] and outcome["state_unchanged_after_run"])
    return 0 if ok else 1


# ---------------------------------------------------------------- main

def artifact_hashes(out):
    hashes = {}
    for path in sorted(out.rglob("*")):
        if path.is_file() and not path.is_symlink() and path.name != "receipt.json" and "committed-tree" not in path.relative_to(out).parts:
            hashes[str(path.relative_to(out))] = sha256_file(path)
    return hashes


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--phase", choices=["suite", "compatibility", "portable", "credentials", "public-tree"])
    parser.add_argument("--source-manifest")
    parser.add_argument("--out-base")
    parser.add_argument("--child", choices=["suite", "compat", "discover"], help=argparse.SUPPRESS)
    parser.add_argument("--child-out", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.child:
        if not args.child_out or not Path(args.child_out).is_absolute():
            parser.error("--child-out must be absolute")
        return {"suite": child_suite, "compat": child_compat, "discover": child_discover}[args.child](args.child_out)
    if not (args.phase and args.source_manifest and args.out_base):
        parser.error("--phase, --source-manifest and --out-base are required")
    out_base = Path(args.out_base)
    if not out_base.is_absolute() or out_base.resolve() == REPO or REPO in out_base.resolve().parents:
        parser.error("--out-base must be absolute and outside the repository")
    out_base.mkdir(parents=True, exist_ok=True)
    out = out_base / f"{args.phase}-{uuid.uuid4().hex}"
    out.mkdir(exist_ok=False)
    (out / "tmp").mkdir()
    tempfile.tempdir = str(out / "tmp")
    row = Row(args.phase, out)
    receipt = {"card": "FC9-DIGEST-LEGAL-DOWNGRADE", "phase": args.phase, "verdict": VERDICT,
               "argv": [os.path.abspath(sys.executable), *sys.argv], "cwd": os.getcwd(),
               "interpreter": {"executable": os.path.abspath(sys.executable), "version": sys.version,
                               "resolved": str(Path(sys.executable).resolve())},
               "repo": str(REPO), "source_manifest": args.source_manifest, "out": str(out), "started": now()}
    error = None
    try:
        receipt["source_manifest_sha256"] = sha256_file(args.source_manifest)
        manifest = load_manifest(args.source_manifest)
        check_interpreter(row, manifest, args.phase)
        ident = identity(row, manifest)
        receipt["identity"] = ident
        if args.phase == "suite":
            receipt["result"] = row_suite(row, manifest)
        elif args.phase == "compatibility":
            receipt["result"] = row_compatibility(row, manifest)
        elif args.phase == "portable":
            receipt["result"] = row_portable(row, manifest)
        elif args.phase == "credentials":
            receipt["result"] = row_credentials(row, manifest)
        else:
            receipt["result"] = row_public_tree(row, manifest, ident)
    except Stop as stop:
        error = str(stop)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        error = f"{type(exc).__name__}: {exc}"
    receipt["commands"] = row.commands
    receipt["checks"] = row.checks
    receipt["error"] = error
    receipt["failed_checks"] = [c["check"] for c in row.checks if not c["ok"]]
    receipt["pass"] = error is None and bool(row.checks) and not receipt["failed_checks"]
    receipt["ended"] = now()
    receipt["artifacts"] = artifact_hashes(out)
    (out / "receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({"phase": args.phase, "pass": receipt["pass"], "error": error,
                      "failed_checks": receipt["failed_checks"], "out": str(out),
                      "receipt_sha256": sha256_file(out / "receipt.json"),
                      "head": (receipt.get("identity") or {}).get("head"), "verdict": VERDICT}, sort_keys=True))
    return 0 if receipt["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
