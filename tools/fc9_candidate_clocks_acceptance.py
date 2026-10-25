"""FC9-CANDIDATE-CLOCKS acceptance runner (offline, synthetic, review-required).

Phases (each invocation writes a fresh UUID directory under --out-base):
  red            test-first demonstration on the pinned base product (before the fix)
  suite          A1: the new card suite, requirement map, case coverage and declared controls
  compatibility  A2: the named existing selectors only
  portable       A3: absolute-script run from a fresh cwd, isolated discovery with sentinel, path literals
  credentials    A4: unchanged repository secret scan + report check under the manifest interpreter
  public-tree    A5: committed-tree clone scan, plus the INC-174 in-place informational scan

The repository is derived from this file. Manifest and output paths are literal
CLI arguments. Every child command is a literal argv list with shell disabled.
Machine-valid output is REVIEW REQUIRED, never semantic, legal, human or live acceptance.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

REPO = Path(__file__).resolve().parents[1]
BASE = "6dd2ae508e316dd83aa6d74445c46fb8c8d25757"
PINNED = {
    "campaign_tool/records/understanding/document.py": "f3a48098d27ad71061781d2eaacfde803f3f1be108f2a4fce3085f12dacd9a27",
    "campaign_tool/records/understanding/correspondence.py": "78db1be2a0e927cbde586fde3c0f53f2550e4494b2de938a90fade33b38756ad",
    "campaign_tool/records/understanding/rows.py": "0e3dc92d23343c571629e16582ddcd60432c69bed1c2f756956eb42487bec820",
    "tests/records/test_correspondence.py": "e9b98af06e3415f7a5e982fe3285dcb4d9b8f1cd3bfa2171705da9924afacf7b",
}
PRODUCT = ["campaign_tool/records/understanding/document.py",
           "campaign_tool/records/understanding/correspondence.py"]
AUTHORED = PRODUCT + ["tests/records/test_fc9_candidate_clocks.py",
                      "tests/fixtures/fc9-candidate-clocks/cases.json",
                      "tools/fc9_candidate_clocks_acceptance.py"]
NEW_SUITE = "tests.records.test_fc9_candidate_clocks"
FIXTURE = "tests/fixtures/fc9-candidate-clocks/cases.json"
COMPAT = ["tests.records.test_correspondence",
          "tests.records.test_understanding.DocumentTests",
          "tests.records.test_understanding.CorrespondenceTests"]
SENTINEL = "tests.records.test_understanding.EvidenceTests.test_receipt_normalizes_to_pinned_bundle"
UNCHANGED_CHECKERS = ["tools/check_public_tree.py", "campaign_tool/records/public_scan.py",
                      "tools/check_secret_report.py", ".gitignore"]
RED_EXPECTED_FAIL = {
    "DocumentOffsetTests.test_analyze_offset_lexical_matrix",
    "DocumentOffsetTests.test_nc1_target_plus_01_60_value_null",
    "CorrespondenceClockTests.test_track_occurred_at_matrix",
    "CorrespondenceClockTests.test_nc2_target_occurred_at_high_overflow_no_escape",
    "CorrespondenceClockTests.test_track_promised_due_at",
    "CorrespondenceClockTests.test_track_string_as_of",
    "CorrespondenceClockTests.test_track_datetime_as_of",
    "RfcDateMetadataTests.test_events_from_bundle_and_track",
}
RED_EXPECTED_PASS = {"DocumentOffsetTests.test_analyze_retained_behaviors"}
REVIEW = "REVIEW REQUIRED: machine-valid output is not semantic, legal, human or live acceptance."


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def short_id(test_id):
    parts = test_id.split(" ")[0].split(".")
    return ".".join(parts[-2:])


class Row:
    def __init__(self, phase, out_base):
        self.phase = phase
        self.dir = Path(out_base) / f"{phase}-{uuid.uuid4().hex}"
        self.dir.mkdir(parents=True, exist_ok=False)
        self.tmp = self.dir / "tmp"
        self.tmp.mkdir()
        tempfile.tempdir = str(self.tmp)
        self.commands = []
        self.checks = []
        self.receipt = {"schema": "fc9-candidate-clocks-acceptance/v1", "phase": phase,
                        "argv": list(sys.argv), "cwd": os.getcwd(), "interpreter": sys.executable,
                        "interpreter_resolved": str(Path(sys.executable).resolve()),
                        "sys_prefix": sys.prefix, "python_version": sys.version,
                        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "out_dir": str(self.dir),
                        "review_status": REVIEW}

    def run(self, name, argv, cwd, timeout=3600):
        argv = [str(item) for item in argv]
        started = time.time()
        proc = subprocess.run(argv, cwd=str(cwd), capture_output=True, shell=False, timeout=timeout)
        out, err = self.dir / f"{name}.stdout", self.dir / f"{name}.stderr"
        out.write_bytes(proc.stdout)
        err.write_bytes(proc.stderr)
        entry = {"name": name, "argv": argv, "cwd": str(cwd), "shell": False, "exit": proc.returncode,
                 "seconds": round(time.time() - started, 3),
                 "stdout": str(out), "stdout_sha256": sha256_file(out),
                 "stderr": str(err), "stderr_sha256": sha256_file(err)}
        self.commands.append(entry)
        return proc, entry

    def check(self, name, ok, detail=None):
        self.checks.append({"check": name, "pass": bool(ok), "detail": detail})
        return bool(ok)

    def finish(self, extra=None):
        self.receipt.update(extra or {})
        self.receipt["commands"] = self.commands
        self.receipt["checks"] = self.checks
        self.receipt["pass"] = all(item["pass"] for item in self.checks) and bool(self.checks)
        self.receipt["ended_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        artifacts = {}
        for path in sorted(self.dir.iterdir()):
            if path.is_file() and path.name != "receipt.json":
                artifacts[path.name] = sha256_file(path)
        self.receipt["artifact_sha256"] = artifacts
        target = self.dir / "receipt.json"
        target.write_text(json.dumps(self.receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        failed = [item["check"] for item in self.checks if not item["pass"]]
        print(json.dumps({"phase": self.phase, "pass": self.receipt["pass"], "out_dir": str(self.dir),
                          "receipt_sha256": sha256_file(target), "failed_checks": failed,
                          "review_status": REVIEW}, indent=2))
        return 0 if self.receipt["pass"] else 1


def git(row, name, args, cwd=REPO):
    proc, _ = row.run(name, ["git", *args], cwd)
    return proc.returncode, proc.stdout.decode("utf-8", "replace").strip()


def identity(row, manifest_path, *, expect_base_head):
    """Check manifest, repo, runtime and head identity against the staged checkout."""
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    runtime = manifest.get("runtime", {})
    row.check("manifest_schema", manifest.get("schema") == "agentorange.source-manifest/v1", manifest.get("schema"))
    row.check("manifest_repo_is_this_repo", Path(manifest.get("repo", "")).resolve() == REPO, manifest.get("repo"))
    row.check("manifest_head_is_pinned_base", manifest.get("head") == BASE, manifest.get("head"))
    _, base_tree = git(row, "git-base-tree", ["rev-parse", BASE + "^{tree}"])
    row.check("manifest_tree_is_base_tree", manifest.get("tree") == base_tree, base_tree)
    _, head = git(row, "git-head", ["rev-parse", "HEAD"])
    _, tree = git(row, "git-head-tree", ["rev-parse", "HEAD^{tree}"])
    code, _ = git(row, "git-base-is-ancestor", ["merge-base", "--is-ancestor", BASE, "HEAD"])
    row.check("head_descends_from_base", code == 0, head)
    venv_link = REPO / ".venv"
    row.check("staged_venv_link_is_manifest_venv", venv_link.is_symlink()
              and venv_link.resolve() == Path(runtime.get("repo_venv", "")).resolve(), str(venv_link))
    if expect_base_head:
        row.check("red_head_is_base", head == BASE, head)
        for rel, digest in PINNED.items():
            row.check("red_pinned_source_" + rel, sha256_file(REPO / rel) == digest, rel)
    else:
        row.check("candidate_head_is_not_base", head != BASE, head)
        _, log = git(row, "git-candidate-log", ["log", "--format=%H%x1f%B%x1e", BASE + "..HEAD"])
        commits = [item.strip() for item in log.split("\x1e") if item.strip()]
        row.check("candidate_commits_signed_off", commits and all("Signed-off-by:" in item for item in commits),
                  [item.split("\x1f")[0] for item in commits])
    sources = {rel: sha256_file(REPO / rel) for rel in sorted(set(AUTHORED) | set(PINNED)) if (REPO / rel).is_file()}
    row.receipt.update({"manifest": str(manifest_path), "manifest_sha256": sha256_file(manifest_path),
                        "base": BASE, "candidate_head": head, "candidate_tree": tree,
                        "fixture_sha256": sha256_file(REPO / FIXTURE) if (REPO / FIXTURE).is_file() else None,
                        "source_sha256": sources})
    return manifest, head, tree


def staged_interpreter(row, manifest):
    venv = Path(manifest["runtime"]["repo_venv"]).resolve()
    row.check("interpreter_is_staged_repo_venv", Path(sys.prefix).resolve() == venv
              and Path(sys.executable) == REPO / ".venv" / "bin" / "python3", sys.executable)


def unittest_child(row, name, names, cwd=REPO):
    result_path = row.dir / f"{name}.result.json"
    argv = [sys.executable, "-B", Path(__file__).resolve(), "--phase", "_unittest-child",
            "--temp-root", row.tmp, "--result-out", result_path, "--names", *names]
    proc, entry = row.run(name, argv, cwd)
    data = json.loads(result_path.read_text(encoding="utf-8")) if result_path.is_file() else {}
    entry["result"] = str(result_path)
    return proc, data


# ----------------------------------------------------------------- child modes
def _child_setup(temp_root):
    child_tmp = Path(temp_root) / ("child-" + uuid.uuid4().hex)
    child_tmp.mkdir(parents=True)
    tempfile.tempdir = str(child_tmp)
    sys.dont_write_bytecode = True
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))


def _recording_result():
    import unittest

    class Recording(unittest.TextTestResult):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.passed = []
            self.subtest_failures = []

        def addSuccess(self, test):
            super().addSuccess(test)
            self.passed.append(test.id())

        def addSubTest(self, test, subtest, err):
            super().addSubTest(test, subtest, err)
            if err is not None:
                self.subtest_failures.append(subtest.id())
    return Recording


def _summarize(result):
    failed = {short_id(t.id()) for t, _ in result.failures + result.errors}
    return {"tests_run": result.testsRun, "failures": [t.id() for t, _ in result.failures],
            "errors": [t.id() for t, _ in result.errors], "skipped": [t.id() for t, _ in result.skipped],
            "subtest_failures": result.subtest_failures,
            "passed": sorted({p for p in result.passed if short_id(p) not in failed}),
            "failure_text": [text[-1500:] for _, text in result.failures + result.errors],
            "successful": result.wasSuccessful()}


def _card_module_facts():
    module = sys.modules.get(NEW_SUITE)
    if module is None:
        return {}
    return {"exercised_case_ids": sorted(module.EXERCISED), "fixture_case_ids": module.fixture_case_ids(),
            "case_dimensions": module.case_dimensions(), "requirements": module.REQUIREMENTS}


def child_unittest(args):
    _child_setup(args.temp_root)
    import unittest
    suite = unittest.TestLoader().loadTestsFromNames(args.names)
    runner = unittest.TextTestRunner(stream=sys.stderr, verbosity=2, resultclass=_recording_result())
    data = _summarize(runner.run(suite))
    data.update(_card_module_facts())
    Path(args.result_out).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0 if data["successful"] else 1


def child_discovery(args):
    _child_setup(args.temp_root)
    import unittest

    def snapshot():
        return {"environ": dict(os.environ), "tempdir": tempfile.tempdir, "sys_path": list(sys.path),
                "cwd": os.getcwd(), "dont_write_bytecode": sys.dont_write_bytecode}

    loader = unittest.TestLoader()
    runner = unittest.TextTestRunner(stream=sys.stderr, verbosity=2, resultclass=_recording_result())
    before = snapshot()
    sentinel_before = _summarize(runner.run(loader.loadTestsFromName(SENTINEL)))
    after_sentinel = snapshot()
    discovered = loader.discover(start_dir=str(REPO / "tests" / "records"),
                                 pattern="test_fc9_candidate_clocks.py", top_level_dir=str(REPO))
    after_import = snapshot()
    card = _summarize(runner.run(discovered))
    after_run = snapshot()
    sentinel_after = _summarize(runner.run(loader.loadTestsFromName(SENTINEL)))
    after_all = snapshot()
    data = {"sentinel": SENTINEL, "sentinel_before": sentinel_before, "sentinel_after": sentinel_after,
            "discovered": card, "discovered_count": discovered.countTestCases(),
            "ambient_unchanged_by_import": before == after_sentinel == after_import,
            "ambient_unchanged_by_run": after_import == after_run == after_all,
            "module_name_loaded": NEW_SUITE in sys.modules}
    data.update(_card_module_facts())
    Path(args.result_out).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    ok = (sentinel_before["successful"] and sentinel_after["successful"] and card["successful"]
          and data["ambient_unchanged_by_import"] and data["ambient_unchanged_by_run"])
    return 0 if ok else 1


# ----------------------------------------------------------------- phases
def check_card_suite(row, data, prefix):
    row.check(prefix + "no_failures_or_errors", not data.get("failures") and not data.get("errors"),
              {"failures": data.get("failures"), "errors": data.get("errors")})
    row.check(prefix + "zero_skipped", data.get("skipped") == [], data.get("skipped"))
    passed = {short_id(p) for p in data.get("passed", [])}
    requirements = data.get("requirements", {})
    missing = {req: [t for t in tests if t not in passed] for req, tests in requirements.items()}
    missing = {req: tests for req, tests in missing.items() if tests}
    row.check(prefix + "every_requirement_mapped_and_passed", requirements and not missing, missing)
    exercised, fixture = data.get("exercised_case_ids", []), data.get("fixture_case_ids", [])
    row.check(prefix + "every_fixture_case_exercised", fixture and exercised == fixture,
              sorted(set(fixture) - set(exercised)))


def phase_red(args):
    row = Row("red", args.out_base)
    manifest, _, _ = identity(row, args.source_manifest, expect_base_head=True)
    staged_interpreter(row, manifest)
    _, data = unittest_child(row, "card-suite-on-base", [NEW_SUITE])
    failed = {short_id(t) for t in data.get("failures", []) + data.get("errors", [])}
    failures_only = {short_id(t) for t in data.get("failures", [])}
    passed = {short_id(p) for p in data.get("passed", [])}
    row.check("red_fix_dependent_tests_fail_by_assertion", RED_EXPECTED_FAIL <= failures_only,
              sorted(RED_EXPECTED_FAIL - failures_only))
    row.check("red_valid_controls_pass_on_base", RED_EXPECTED_PASS <= passed, sorted(RED_EXPECTED_PASS - passed))
    return row.finish({"label": "TEST-FIRST RED on pinned base product; not an acceptance pass",
                       "baseline_failed_tests": sorted(failed), "baseline_passed_tests": sorted(passed),
                       "baseline_failure_text": data.get("failure_text", [])})


def phase_suite(args):
    row = Row("suite", args.out_base)
    manifest, _, _ = identity(row, args.source_manifest, expect_base_head=False)
    staged_interpreter(row, manifest)
    proc, data = unittest_child(row, "card-suite", [NEW_SUITE])
    row.check("suite_exit_zero", proc.returncode == 0, proc.returncode)
    check_card_suite(row, data, "suite_")
    reds = sorted(Path(args.out_base).glob("red-*/receipt.json"))
    baseline = None
    if reds:
        red = json.loads(reds[-1].read_text(encoding="utf-8"))
        baseline = {"receipt": str(reds[-1]), "receipt_sha256": sha256_file(reds[-1]),
                    "head": red.get("candidate_head"), "pass": red.get("pass"),
                    "failed_tests": red.get("baseline_failed_tests")}
    controls = {
        "NC1": {"injected_behavior": "bypass _offset_components_in_range only for fixture doc-colon-plus-01-60",
                "target": "DocumentOffsetTests.test_nc1_target_plus_01_60_value_null",
                "required_kill": "exactly one AssertionError failure carrying FC9-NC1-NULL-VALUE, zero errors",
                "then": "restore; rerun target and valid controls green"},
        "NC2": {"injected_behavior": "replace correspondence._to_utc with an unguarded astimezone(timezone.utc)",
                "target": "CorrespondenceClockTests.test_nc2_target_occurred_at_high_overflow_no_escape",
                "required_kill": "exactly one AssertionError failure carrying FC9-NC2-NO-OVERFLOW, zero errors",
                "then": "restore; rerun target and valid controls green"},
    }
    passed = {short_id(p) for p in data.get("passed", [])}
    for key, test in (("NC1", "SyntheticNegativeControlTests.test_nc1_lexical_bypass_breaks_null_value_assertion"),
                      ("NC2", "SyntheticNegativeControlTests.test_nc2_unguarded_conversion_breaks_no_overflow_assertion")):
        controls[key]["final_candidate_output"] = "killed_and_restored" if test in passed else "NOT_PASSED"
        row.check("control_" + key, test in passed, test)
    return row.finish({"tests_run": data.get("tests_run"), "case_dimensions": data.get("case_dimensions"),
                       "fixture_case_count": len(data.get("fixture_case_ids", [])),
                       "requirements": data.get("requirements"), "controls": controls,
                       "baseline_observation": baseline})


def phase_compatibility(args):
    row = Row("compatibility", args.out_base)
    manifest, _, _ = identity(row, args.source_manifest, expect_base_head=False)
    staged_interpreter(row, manifest)
    proc, data = unittest_child(row, "compat-selectors", COMPAT)
    row.check("compat_exit_zero", proc.returncode == 0, proc.returncode)
    row.check("compat_no_failures_or_errors", not data.get("failures") and not data.get("errors"),
              {"failures": data.get("failures"), "errors": data.get("errors")})
    row.check("compat_ran_tests", (data.get("tests_run") or 0) > 0, data.get("tests_run"))
    return row.finish({"selectors": COMPAT, "tests_run": data.get("tests_run"),
                       "skipped": data.get("skipped"),
                       "scope_note": "Scoped selectors only; full repository discovery/CI = operator-run, OWED."})


def phase_portable(args):
    row = Row("portable", args.out_base)
    manifest, _, _ = identity(row, args.source_manifest, expect_base_head=False)
    staged_interpreter(row, manifest)
    cwd = row.dir / "fresh-cwd-script"
    cwd.mkdir()
    coverage = row.dir / "script-coverage.json"
    script = REPO / "tests" / "records" / "test_fc9_candidate_clocks.py"
    proc, _ = row.run("script-run", [sys.executable, "-B", script, "--temp-root", row.tmp,
                                     "--coverage-out", coverage], cwd)
    row.check("script_exit_zero", proc.returncode == 0, proc.returncode)
    script_data = json.loads(coverage.read_text(encoding="utf-8")) if coverage.is_file() else {}
    row.check("script_full_fixture_coverage", script_data.get("fixture_case_ids")
              and script_data.get("exercised_case_ids") == script_data.get("fixture_case_ids"), None)
    row.check("script_zero_skipped_failures", script_data.get("skipped") == 0
              and script_data.get("failures") == 0 and script_data.get("errors") == 0, None)
    dcwd = row.dir / "fresh-cwd-discovery"
    dcwd.mkdir()
    result_path = row.dir / "discovery.result.json"
    proc, _ = row.run("discovery-child", [sys.executable, "-B", Path(__file__).resolve(), "--phase",
                                          "_discovery-child", "--temp-root", row.tmp,
                                          "--result-out", result_path], dcwd)
    row.check("discovery_exit_zero", proc.returncode == 0, proc.returncode)
    disc = json.loads(result_path.read_text(encoding="utf-8")) if result_path.is_file() else {}
    row.check("discovery_sentinel_before_after_pass", disc.get("sentinel_before", {}).get("successful")
              and disc.get("sentinel_after", {}).get("successful"), None)
    row.check("discovery_ambient_unchanged", disc.get("ambient_unchanged_by_import")
              and disc.get("ambient_unchanged_by_run"), None)
    row.check("discovery_equal_case_coverage", disc.get("exercised_case_ids")
              and disc.get("exercised_case_ids") == script_data.get("exercised_case_ids"), None)
    row.check("discovery_no_skips", disc.get("discovered", {}).get("skipped") == [], None)
    literals = ["/" + "home" + "/", "/" + "Users" + "/", "/" + "tmp" + "/", "/" + "root" + "/",
                "agent" + "-work", "." + "hermes", "C:" + "\\"]
    hits = {}
    for rel in AUTHORED[2:]:
        text = (REPO / rel).read_text(encoding="utf-8")
        found = [lit for lit in literals if lit in text]
        if found:
            hits[rel] = found
    row.check("authored_test_fixture_runner_have_no_host_literals", not hits, hits)
    return row.finish({"script_tests_run": script_data.get("tests_run"),
                       "discovery_tests_run": disc.get("discovered", {}).get("tests_run"),
                       "required_case_count": len(script_data.get("fixture_case_ids", []) or []),
                       "case_dimensions": script_data.get("case_dimensions")})


def phase_credentials(args):
    row = Row("credentials", args.out_base)
    manifest, _, _ = identity(row, args.source_manifest, expect_base_head=False)
    runtime = manifest.get("runtime", {})
    declared = Path(runtime.get("credential_scan_python3", ""))
    row.check("interpreter_is_manifest_credential_scan_python3", Path(sys.executable) == declared
              and Path(sys.executable).resolve() == declared.resolve(), sys.executable)
    scanner = Path(runtime.get("detect_secrets", ""))
    row.check("scanner_is_manifest_detect_secrets", scanner.is_file() and REPO not in scanner.resolve().parents,
              str(scanner))
    proc, entry = row.run("detect-secrets", [scanner, "scan", "--all-files", "--exclude-files", "^.git/",
                                             "--no-verify"], REPO)
    row.check("detect_secrets_exit_zero", proc.returncode == 0, proc.returncode)
    report = Path(entry["stdout"])
    try:
        parsed = json.loads(report.read_text(encoding="utf-8"))
        row.check("scanner_json_parseable", isinstance(parsed.get("results"), dict), None)
        result_count = sum(len(v) for v in parsed["results"].values())
    except (ValueError, AttributeError) as exc:
        row.check("scanner_json_parseable", False, str(exc))
        result_count = None
    proc, _ = row.run("check-secret-report", [sys.executable, "-B", REPO / "tools" / "check_secret_report.py",
                                              report], REPO)
    row.check("check_secret_report_exit_zero", proc.returncode == 0, proc.returncode)
    return row.finish({"scanner_report": str(report), "scanner_result_count": result_count})


def phase_public_tree(args):
    row = Row("public-tree", args.out_base)
    manifest, head, tree = identity(row, args.source_manifest, expect_base_head=False)
    staged_interpreter(row, manifest)
    for rel in AUTHORED:
        code, _ = git(row, "ls-files-" + rel.replace("/", "_"), ["ls-files", "--error-unmatch", "--", rel])
        row.check("authored_tracked_" + rel, code == 0, rel)
    code_index, _ = git(row, "diff-cached-quiet", ["diff", "--cached", "--quiet", "HEAD", "--"])
    code_work, _ = git(row, "diff-worktree-quiet", ["diff", "--quiet", "--"])
    row.check("index_and_worktree_equal_head", code_index == 0 and code_work == 0, [code_index, code_work])
    _, status = git(row, "status-before", ["status", "--porcelain=v1", "--untracked-files=all"])
    _, ignored = git(row, "status-ignored-inventory", ["status", "--porcelain=v1", "--untracked-files=all", "--ignored"])
    row.check("only_untracked_is_venv_link", set(status.splitlines()) <= {"?? .venv"}, status.splitlines())
    ignored_other = [line for line in ignored.splitlines() if line.startswith("!! ")
                     and line != "!! .venv" and "__pycache__/" not in line]
    row.check("ignored_inventory_only_venv_link_and_bytecode", not ignored_other, ignored_other)
    git(row, "ls-files-inventory", ["ls-files", "--cached", "--others", "--exclude-standard"])
    for rel in UNCHANGED_CHECKERS:
        _, at_head = git(row, "blob-head-" + rel.replace("/", "_"), ["rev-parse", "HEAD:" + rel])
        _, at_base = git(row, "blob-base-" + rel.replace("/", "_"), ["rev-parse", BASE + ":" + rel])
        row.check("checker_unchanged_" + rel, at_head == at_base and len(at_head) == 40, at_head)
    clone = row.dir / "committed-tree"
    proc, _ = row.run("clone", ["git", "clone", "--local", "--no-hardlinks", "--no-checkout",
                                "--no-recurse-submodules", REPO, clone], row.dir)
    row.check("clone_exit_zero", proc.returncode == 0, proc.returncode)
    proc, _ = row.run("clone-checkout", ["git", "checkout", "--detach", head], clone)
    row.check("clone_checkout_exit_zero", proc.returncode == 0, proc.returncode)
    _, clone_head = git(row, "clone-head", ["rev-parse", "HEAD"], clone)
    _, clone_tree = git(row, "clone-tree", ["rev-parse", "HEAD^{tree}"], clone)
    row.check("clone_head_and_tree_equal_candidate", clone_head == head and clone_tree == tree,
              [clone_head, clone_tree])
    row.check("clone_has_no_staged_runtime_link", not (clone / ".venv").exists() and not (clone / ".venv").is_symlink(),
              None)
    hashes = {}
    for rel in AUTHORED + UNCHANGED_CHECKERS:
        hashes[rel] = [sha256_file(REPO / rel), sha256_file(clone / rel)]
    row.check("authored_and_checker_hashes_equal_in_clone", all(a == b for a, b in hashes.values()), hashes)
    proc, _ = row.run("public-tree-committed", [sys.executable, "-B", clone / "tools" / "check_public_tree.py"], clone)
    try:
        committed = json.loads(proc.stdout.decode("utf-8"))
    except ValueError:
        committed = {}
    row.check("committed_tree_scan_exit_zero_and_no_findings", proc.returncode == 0
              and committed.get("findings") == [], {"exit": proc.returncode, "findings": committed.get("findings")})
    proc, _ = row.run("public-tree-in-place-informational-INC-174",
                      [sys.executable, "-B", REPO / "tools" / "check_public_tree.py"], REPO)
    try:
        in_place = json.loads(proc.stdout.decode("utf-8"))
    except ValueError:
        in_place = {}
    venv_ok = (REPO / ".venv").is_symlink() and (REPO / ".venv").resolve() == Path(
        manifest["runtime"]["repo_venv"]).resolve()
    allowed = ((proc.returncode == 0 and in_place.get("findings") == [])
               or (proc.returncode == 1 and in_place.get("findings") == [[".venv", 0, "symlink_not_scanned"]]
                   and venv_ok))
    row.check("in_place_informational_INC_174_allowed_outcome", allowed,
              {"exit": proc.returncode, "findings": in_place.get("findings")})
    _, head_after = git(row, "head-after", ["rev-parse", "HEAD"])
    _, status_after = git(row, "status-after", ["status", "--porcelain=v1", "--untracked-files=all"])
    row.check("source_head_and_status_unchanged", head_after == head and status_after == status,
              [head_after, status_after])
    return row.finish({"committed_tree_scan": {"exit": 0 if committed.get("findings") == [] else None,
                                               "scanned_files": committed.get("scanned_files"),
                                               "findings": committed.get("findings")},
                       "in_place_informational": {"label": "INC-174 in_place_informational (not a staged-tree pass)",
                                                  "findings": in_place.get("findings"),
                                                  "scanned_files": in_place.get("scanned_files")},
                       "clone": str(clone), "status_before": status})


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--phase", required=True, choices=["red", "suite", "compatibility", "portable",
                                                           "credentials", "public-tree",
                                                           "_unittest-child", "_discovery-child"])
    parser.add_argument("--source-manifest")
    parser.add_argument("--out-base")
    parser.add_argument("--temp-root")
    parser.add_argument("--result-out")
    parser.add_argument("--names", nargs="*", default=[])
    args = parser.parse_args()
    if args.phase == "_unittest-child":
        return child_unittest(args)
    if args.phase == "_discovery-child":
        return child_discovery(args)
    if not args.source_manifest or not args.out_base:
        parser.error("--source-manifest and --out-base are required")
    phases = {"red": phase_red, "suite": phase_suite, "compatibility": phase_compatibility,
              "portable": phase_portable, "credentials": phase_credentials, "public-tree": phase_public_tree}
    return phases[args.phase](args)


if __name__ == "__main__":
    sys.exit(main())
