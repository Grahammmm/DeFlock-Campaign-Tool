"""FC11-EVENT-OFFSET acceptance runner (offline, synthetic, review-required).

Phases (each invocation writes a fresh UUID directory under --out-base):
  suite          A1: original-baseline red run (pinned base product in a row-local
                 committed clone with the candidate test + fixture overlaid), final
                 candidate run, the declared negative control alone, full case matrix
  compatibility  A2: the named existing selectors only
  portable       A3: absolute-script run from a fresh cwd, isolated discovery with sentinel, path literals
  credentials    A4: unchanged repository secret scan + report check under the manifest interpreter
  public-tree    A5: committed-tree clone scan, plus the INC-174 in-place informational scan

The repository is derived from this file. Manifest and output paths are literal
CLI arguments. Every child command is a literal argv list with shell disabled.
Each phase verifies manifest/repo/head/tree/runtime identity first and stops at a
fail-closed preflight barrier (failure receipt, exit 1) before any product or
test import, child, scanner or clone; inert in-process controls prove failed
identity never reaches a phase boundary and valid identity does. Unittest
children and the standalone test script each install a scoped socket guard
before any product import.
Machine-valid output is REVIEW REQUIRED, never semantic, legal, human or live acceptance.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import uuid

REPO = Path(__file__).resolve().parents[1]
# The pinned base commit and pinned source digests are bound through the
# operator's SOURCE-MANIFEST head and git blobs at that commit; no hex literal is
# embedded here (the unchanged repository secret scan treats them as entropy hits).
BASE = None
PINNED = ["campaign_tool/records/detectors/core.py",
          "tests/records/test_detectors.py",
          "campaign_tool/records/understanding/rows.py"]
PRODUCT = ["campaign_tool/records/detectors/core.py"]
TEST_FILE = "tests/records/test_fc11_event_offset.py"
FIXTURE = "tests/fixtures/fc11-event-offset/cases.json"
RUNNER = "tools/fc11_event_offset_acceptance.py"
AUTHORED = PRODUCT + [TEST_FILE, FIXTURE, RUNNER]
NEW_SUITE = "tests.records.test_fc11_event_offset"
NC_TEST = NEW_SUITE + ".SyntheticNegativeControlTests.test_nc_lexical_check_removed_breaks_blocked_assertion"
COMPAT = ["tests.records.test_detectors.DetectorTests",
          "tests.records.test_detectors.DetectorRepairTests",
          "tests.test_understanding_rows.RowTests.test_invalid_timezone_class_and_valid_offset"]
SENTINEL = "tests.test_understanding_rows.RowTests.test_unknown_target_and_type_are_refused"
UNCHANGED_CHECKERS = ["tools/check_public_tree.py", "campaign_tool/records/public_scan.py",
                      "tools/check_secret_report.py", ".gitignore"]
# Tests that must fail by assertion on the pinned original product (test-first evidence).
BASELINE_EXPECTED_FAIL = {
    "MomentOffsetTests.test_invalid_offsets_blocked",
    "MomentOffsetTests.test_nc_target_plus_00_99_blocked",
    "RetentionEvaluateTests.test_invalid_offsets_blocked_no_hit",
    "RetentionEvaluateTests.test_nc_target_plus_00_99_blocked_no_hit",
    "TrainingEvaluateTests.test_invalid_training_offsets_blocked",
}
# The control seam does not exist on the original product, so its control errors there.
BASELINE_EXPECTED_ERROR = {"SyntheticNegativeControlTests.test_nc_lexical_check_removed_breaks_blocked_assertion"}
BASELINE_EXPECTED_PASS = {
    "MomentOffsetTests.test_valid_offsets_preserve_instants",
    "MomentOffsetTests.test_missing_timezone_and_malformed_reasons",
    "RetentionEvaluateTests.test_valid_offsets_evaluated_hit",
    "RetentionEvaluateTests.test_nc_partner_plus_00_00_evaluated",
    "RetentionEvaluateTests.test_missing_and_malformed_blocked",
    "RetentionEvaluateTests.test_wall_date_rule_selection",
    "TrainingEvaluateTests.test_valid_training_offsets",
    "TrainingEvaluateTests.test_missing_and_malformed_training_blocked",
}
REVIEW = "REVIEW REQUIRED: machine-valid output is not semantic, legal, human or live acceptance."
_CASE_ID = re.compile(r"case='([^']+)'")


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def short_id(test_id):
    parts = test_id.split(" ")[0].split(".")
    return ".".join(parts[-2:])


def subcase(test_id):
    match = _CASE_ID.search(test_id)
    return (short_id(test_id), match.group(1) if match else None, test_id)


class Row:
    def __init__(self, phase, out_base):
        self.phase = phase
        self.dir = Path(out_base) / f"{phase}-{uuid.uuid4()}"
        self.dir.mkdir(parents=True, exist_ok=False)
        self.tmp = self.dir / "tmp"
        self.tmp.mkdir()
        tempfile.tempdir = str(self.tmp)
        self.commands = []
        self.checks = []
        self.receipt = {"schema": "fc11-event-offset-acceptance/v1", "phase": phase,
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
        # Every child and output writer has finished; freeze the evidence manifest last.
        artifacts = {}
        for path in sorted(self.dir.iterdir()):
            if path.is_file() and path.name != "receipt.json":
                artifacts[path.name] = sha256_file(path)
        self.receipt["artifact_sha256"] = artifacts
        target = self.dir / "receipt.json"
        target.write_text(json.dumps(self.receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        failed = [item["check"] for item in self.checks if not item["pass"]]
        print(json.dumps({"phase": self.phase, "pass": self.receipt["pass"], "out_dir": str(self.dir),
                          "receipt_sha256": sha256_file(target), "checks": len(self.checks),
                          "failed_checks": failed, "review_status": REVIEW}, indent=2))
        return 0 if self.receipt["pass"] else 1


def git(row, name, args, cwd=REPO):
    proc, _ = row.run(name, ["git", *args], cwd)
    return proc.returncode, proc.stdout.decode("utf-8", "replace").strip()


def identity(row, manifest_path):
    """Check manifest, repo, runtime and head identity against the staged checkout."""
    global BASE
    try:
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        parsed = isinstance(manifest, dict)
    except (OSError, ValueError) as exc:
        manifest, parsed = None, str(exc)[:200]
    row.check("manifest_json_parseable", parsed is True, None if parsed is True else parsed)
    if not isinstance(manifest, dict):
        manifest = {}
    runtime = manifest.get("runtime") if isinstance(manifest.get("runtime"), dict) else {}
    manifest["runtime"] = runtime
    row.check("manifest_schema", manifest.get("schema") == "agentorange.source-manifest/v1", manifest.get("schema"))
    row.check("manifest_repo_is_this_repo", Path(manifest.get("repo", "")).resolve() == REPO, manifest.get("repo"))
    BASE = str(manifest.get("head", ""))
    code, resolved = git(row, "git-base-commit", ["rev-parse", "--verify", BASE + "^{commit}"])
    row.check("manifest_head_is_full_commit_in_repo", code == 0 and resolved == BASE and len(BASE) == 40, BASE)
    _, base_tree = git(row, "git-base-tree", ["rev-parse", BASE + "^{tree}"])
    row.check("manifest_tree_is_base_tree", manifest.get("tree") == base_tree, base_tree)
    _, head = git(row, "git-head", ["rev-parse", "HEAD"])
    _, tree = git(row, "git-head-tree", ["rev-parse", "HEAD^{tree}"])
    code, _ = git(row, "git-base-is-ancestor", ["merge-base", "--is-ancestor", BASE, "HEAD"])
    row.check("head_descends_from_base", code == 0, head)
    row.check("candidate_head_is_not_base", head != BASE, head)
    _, log = git(row, "git-candidate-log", ["log", "--format=%H%x1f%B%x1e", BASE + "..HEAD"])
    commits = [item.strip() for item in log.split("\x1e") if item.strip()]
    row.check("candidate_commits_signed_off", bool(commits) and all("Signed-off-by:" in item for item in commits),
              [item.split("\x1f")[0] for item in commits])
    _, changed = git(row, "git-changed-files", ["diff", "--name-only", BASE, "HEAD"])
    row.check("changed_files_within_exclusive_write_set", set(changed.splitlines()) <= set(AUTHORED),
              changed.splitlines())
    venv_link = REPO / ".venv"
    row.check("staged_venv_link_is_manifest_venv", venv_link.is_symlink()
              and venv_link.resolve() == Path(runtime.get("repo_venv", "")).resolve(), str(venv_link))
    base_pins = {}
    for rel in PINNED:
        proc, entry = row.run("base-blob-" + rel.replace("/", "_"), ["git", "show", BASE + ":" + rel], REPO)
        base_pins[rel] = entry["stdout_sha256"] if proc.returncode == 0 else None
    row.receipt["base_pinned_source_sha256"] = base_pins
    for rel in PINNED:
        if rel not in PRODUCT:
            row.check("pinned_read_only_unchanged_" + rel, sha256_file(REPO / rel) == base_pins[rel], rel)
    sources = {rel: sha256_file(REPO / rel) for rel in sorted(set(AUTHORED) | set(PINNED)) if (REPO / rel).is_file()}
    row.receipt.update({"manifest": str(manifest_path),
                        "manifest_sha256": sha256_file(manifest_path) if Path(manifest_path).is_file() else None,
                        "base": BASE, "candidate_head": head, "candidate_tree": tree,
                        "fixture_sha256": sha256_file(REPO / FIXTURE) if (REPO / FIXTURE).is_file() else None,
                        "source_sha256": sources, "candidate_commits": [c.split("\x1f")[0] for c in commits],
                        "changed_files": changed.splitlines()})
    return manifest, head, tree


def staged_interpreter(row, manifest):
    declared = manifest.get("runtime", {}).get("repo_venv")
    venv = Path(declared).resolve() if declared else None
    row.check("interpreter_is_staged_repo_venv", venv is not None and Path(sys.prefix).resolve() == venv
              and Path(sys.executable) == REPO / ".venv" / "bin" / "python3", sys.executable)


# ----------------------------------------------------------------- preflight barrier
class PreflightRefused(Exception):
    """Raised after a failure receipt is written; carries the phase exit code."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def preflight_gate(row, stage):
    """Fail closed: any failed identity/runtime/tool check stops the phase here.

    Only read-only git identity commands may run before this point. Nothing past
    it (product or test import, unittest child, script run, scanner, clone) is
    reached unless every recorded check passed.
    """
    failed = [item["check"] for item in row.checks if not item["pass"]]
    ok = bool(row.checks) and not failed
    row.check("preflight_barrier_" + stage, ok, failed)
    row.receipt["preflight"] = {"stage": stage, "passed": ok, "failed_checks": failed,
                                "pre_gate_commands": [entry["name"] for entry in row.commands]}
    if not ok:
        row.receipt["preflight"]["note"] = ("fail-closed: no product/test import, phase child, "
                                            "script run, scanner or clone was started")
        raise PreflightRefused(row.finish())


class _BoundaryReached(Exception):
    pass


IDENTITY_COMMANDS = ("git-", "base-blob-")
_CONTROLS = {"active": False}


def _variants(phase):
    def set_key(key, value):
        return lambda m: m.__setitem__(key, value)

    def set_runtime(key, value):
        return lambda m: m.setdefault("runtime", {}).__setitem__(key, value)
    out = [("valid_positive", None, None),
           ("malformed_json", "RAW", "manifest_json_parseable"),
           ("schema", set_key("schema", "agentorange.source-manifest/v0"), "manifest_schema"),
           ("repo", set_key("repo", "fc11-not-this-repo"), "manifest_repo_is_this_repo"),
           ("head", set_key("head", "f" * 40), "manifest_head_is_full_commit_in_repo"),
           ("tree", set_key("tree", "0" * 40), "manifest_tree_is_base_tree"),
           ("runtime_repo_venv", set_runtime("repo_venv", "fc11-not-the-venv"), "staged_venv_link_is_manifest_venv")]
    if phase == "credentials":
        out += [("runtime_credential_python3", set_runtime("credential_scan_python3", "fc11-not-python3"),
                 "interpreter_is_manifest_credential_scan_python3"),
                ("runtime_detect_secrets", set_runtime("detect_secrets", "fc11-not-a-scanner"),
                 "scanner_is_manifest_detect_secrets")]
    return out


def preflight_controls(row, args, phase):
    """Inert controls: failed identity/runtime never reaches a phase boundary; valid does.

    Each variant reruns this phase in-process against an output-local manifest
    copy. Every command except the read-only identity git calls, and every
    unittest child, is replaced by a recorder that raises before anything runs,
    so no product, test, scanner or clone is executed by a control.
    """
    if _CONTROLS["active"]:
        return
    import argparse as _argparse
    import contextlib
    import copy
    from unittest.mock import patch
    saved_base, saved_tmp = BASE, tempfile.tempdir
    module = sys.modules[__name__]
    phase_fn = PHASES[phase]
    real = json.loads(Path(args.source_manifest).read_text(encoding="utf-8"))
    root = row.dir / "preflight-controls"
    root.mkdir()
    results = []
    original_run = Row.run
    _CONTROLS["active"] = True
    try:
        for name, mutate, expected in _variants(phase):
            vdir = root / name
            vdir.mkdir()
            mpath = vdir / "manifest.json"
            if mutate == "RAW":
                mpath.write_text("{fc11 synthetic malformed manifest\n", encoding="utf-8")
            else:
                data = copy.deepcopy(real)
                if mutate is not None:
                    mutate(data)
                mpath.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            reached = []

            def guarded_run(self, cname, argv, cwd, timeout=3600, _reached=reached):
                if cname.startswith(IDENTITY_COMMANDS):
                    return original_run(self, cname, argv, cwd, timeout)
                _reached.append(cname)
                raise _BoundaryReached(cname)

            def guarded_child(crow, cname, names, cwd=REPO, root=REPO, _reached=reached):
                _reached.append("unittest_child:" + cname)
                raise _BoundaryReached(cname)
            outcome, code = None, None
            log = vdir / "control.stdout"
            with open(log, "w", encoding="utf-8") as stream, contextlib.redirect_stdout(stream), \
                    patch.object(Row, "run", guarded_run), patch.object(module, "unittest_child", guarded_child):
                try:
                    phase_fn(_argparse.Namespace(source_manifest=str(mpath), out_base=str(vdir / "out")))
                    outcome = "returned_without_gate_or_boundary"
                except PreflightRefused as exc:
                    outcome, code = "preflight_refused", exc.code
                except _BoundaryReached:
                    outcome = "boundary_reached"
            receipts = sorted((vdir / "out").glob("*/receipt.json"))
            receipt = json.loads(receipts[0].read_text(encoding="utf-8")) if len(receipts) == 1 else {}
            failed = (receipt.get("preflight") or {}).get("failed_checks", [])
            if mutate is None:
                ok = outcome == "boundary_reached" and len(reached) == 1 and receipts == []
            else:
                ok = (outcome == "preflight_refused" and code == 1 and reached == []
                      and receipt.get("pass") is False and expected in failed
                      and receipt.get("preflight", {}).get("passed") is False)
            results.append({"variant": name, "expected_failed_check": expected, "outcome": outcome,
                            "exit": code, "boundaries_reached": list(reached), "failed_checks": failed,
                            "manifest_sha256": sha256_file(mpath),
                            "receipt": str(receipts[0]) if receipts else None,
                            "receipt_sha256": sha256_file(receipts[0]) if receipts else None, "pass": ok})
            row.check("preflight_control_" + name, ok, results[-1])
    finally:
        _CONTROLS["active"] = False
        globals()["BASE"] = saved_base
        tempfile.tempdir = saved_tmp
    row.check("preflight_controls_restored", Row.run is original_run and BASE == saved_base
              and tempfile.tempdir == saved_tmp and module.unittest_child is unittest_child_impl, None)
    row.receipt["preflight_controls"] = {
        "label": "inert: commands other than read-only identity git calls and every unittest child are "
                 "replaced by recorders that raise before execution",
        "results": results}


def unittest_child(row, name, names, cwd=REPO, root=REPO):
    result_path = row.dir / f"{name}.result.json"
    argv = [sys.executable, "-B", Path(__file__).resolve(), "--phase", "_unittest-child",
            "--temp-root", row.tmp, "--repo-root", root, "--result-out", result_path, "--names", *names]
    proc, entry = row.run(name, argv, cwd)
    data = json.loads(result_path.read_text(encoding="utf-8")) if result_path.is_file() else {}
    entry["result"] = str(result_path)
    return proc, data


unittest_child_impl = unittest_child


def load_cases():
    return json.loads((REPO / FIXTURE).read_text(encoding="utf-8"))


def expected_subcases(cases):
    """Frozen matrix: (test, case id) -> required number of passing subtests."""
    want = {}

    def add(test, group, times=1, only=None):
        for case in cases[group]:
            if only is None or only(case):
                key = (test, case["id"])
                want[key] = want.get(key, 0) + times
    add("MomentOffsetTests.test_valid_offsets_preserve_instants", "valid_offsets")
    add("MomentOffsetTests.test_invalid_offsets_blocked", "invalid_offsets")
    for group in ("missing_timezone", "malformed"):
        add("MomentOffsetTests.test_missing_timezone_and_malformed_reasons", group)
        add("RetentionEvaluateTests.test_missing_and_malformed_blocked", group)
        add("TrainingEvaluateTests.test_missing_and_malformed_training_blocked", group,
            only=lambda case: isinstance(case["value"], str) and bool(case["value"]))
    add("RetentionEvaluateTests.test_valid_offsets_evaluated_hit", "valid_offsets")
    add("RetentionEvaluateTests.test_invalid_offsets_blocked_no_hit", "invalid_offsets")
    add("RetentionEvaluateTests.test_wall_date_rule_selection", "wall_date_rules")
    add("TrainingEvaluateTests.test_valid_training_offsets", "valid_offsets", times=2)
    add("TrainingEvaluateTests.test_invalid_training_offsets_blocked", "invalid_offsets", times=2)
    return want


def subcase_counts(ids):
    out = {}
    for test_id in ids:
        test, case, _ = subcase(test_id)
        out[(test, case)] = out.get((test, case), 0) + 1
    return out


# ----------------------------------------------------------------- child modes
def _child_setup(temp_root, repo_root=None):
    child_tmp = Path(temp_root) / ("child-" + uuid.uuid4().hex)
    child_tmp.mkdir(parents=True)
    tempfile.tempdir = str(child_tmp)
    sys.dont_write_bytecode = True
    import socket
    trips = []

    def refuse(*args, **kwargs):  # scoped offline guard for this standalone child only
        trips.append(repr(args)[:200])
        raise OSError("fc11 acceptance child: network access refused")
    socket.socket.connect = refuse
    socket.socket.connect_ex = refuse
    socket.socket.bind = refuse
    socket.create_connection = refuse
    socket.getaddrinfo = refuse
    root = str(Path(repo_root or REPO).resolve())
    if root not in sys.path:
        sys.path.insert(0, root)
    return trips, root


def _recording_result():
    import unittest

    class Recording(unittest.TextTestResult):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.passed = []
            self.subtest_failures = []
            self.subtest_passed = []

        def addSuccess(self, test):
            super().addSuccess(test)
            self.passed.append(test.id())

        def addSubTest(self, test, subtest, err):
            super().addSubTest(test, subtest, err)
            (self.subtest_passed if err is None else self.subtest_failures).append(subtest.id())
    return Recording


def _summarize(result):
    failed = {short_id(t.id()) for t, _ in result.failures + result.errors}
    return {"tests_run": result.testsRun,
            "failures": sorted({t.id() for t, _ in result.failures}),
            "errors": sorted({t.id() for t, _ in result.errors}),
            "skipped": [t.id() for t, _ in result.skipped],
            "subtest_failures": result.subtest_failures, "subtest_passed": result.subtest_passed,
            "passed": sorted({p for p in result.passed if short_id(p) not in failed}),
            "failure_text": [text[-1500:] for _, text in result.failures + result.errors],
            "successful": result.wasSuccessful()}


def _card_module_facts():
    module = sys.modules.get(NEW_SUITE)
    core = sys.modules.get("campaign_tool.records.detectors.core")
    facts = {"product_core_file": getattr(core, "__file__", None)}
    if module is not None:
        facts.update({"exercised_case_ids": sorted(module.EXERCISED), "fixture_case_ids": module.fixture_case_ids(),
                      "case_dimensions": module.case_dimensions(), "requirements": module.REQUIREMENTS,
                      "test_module_file": module.__file__})
    return facts


def child_unittest(args):
    trips, root = _child_setup(args.temp_root, args.repo_root)
    import unittest
    suite = unittest.TestLoader().loadTestsFromNames(args.names)
    runner = unittest.TextTestRunner(stream=sys.stderr, verbosity=2, resultclass=_recording_result())
    data = _summarize(runner.run(suite))
    data.update(_card_module_facts())
    data.update({"repo_root": root, "offline_guard_trips": trips})
    Path(args.result_out).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0 if data["successful"] and not trips else 1


def child_discovery(args):
    trips, _ = _child_setup(args.temp_root)
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
                                 pattern="test_fc11_event_offset.py", top_level_dir=str(REPO))
    after_import = snapshot()
    card = _summarize(runner.run(discovered))
    after_run = snapshot()
    sentinel_after = _summarize(runner.run(loader.loadTestsFromName(SENTINEL)))
    after_all = snapshot()
    data = {"sentinel": SENTINEL, "sentinel_before": sentinel_before, "sentinel_after": sentinel_after,
            "discovered": card, "discovered_count": discovered.countTestCases(),
            "ambient_unchanged_by_import": before == after_sentinel == after_import,
            "ambient_unchanged_by_run": after_import == after_run == after_all,
            "module_name_loaded": NEW_SUITE in sys.modules, "offline_guard_trips": trips}
    data.update(_card_module_facts())
    Path(args.result_out).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    ok = (sentinel_before["successful"] and sentinel_after["successful"] and card["successful"]
          and data["ambient_unchanged_by_import"] and data["ambient_unchanged_by_run"] and not trips)
    return 0 if ok else 1


# ----------------------------------------------------------------- phases
def check_card_suite(row, data, prefix, cases):
    row.check(prefix + "no_failures_or_errors", not data.get("failures") and not data.get("errors"),
              {"failures": data.get("failures"), "errors": data.get("errors")})
    row.check(prefix + "zero_skipped", data.get("skipped") == [], data.get("skipped"))
    row.check(prefix + "no_subtest_failures", data.get("subtest_failures") == [], data.get("subtest_failures"))
    row.check(prefix + "offline_guard_not_tripped", data.get("offline_guard_trips") == [], data.get("offline_guard_trips"))
    passed = {short_id(p) for p in data.get("passed", [])}
    requirements = data.get("requirements", {})
    missing = {req: [t for t in tests if t not in passed] for req, tests in requirements.items()}
    missing = {req: tests for req, tests in missing.items() if tests}
    row.check(prefix + "every_requirement_mapped_and_passed", bool(requirements) and not missing, missing)
    exercised, fixture = data.get("exercised_case_ids", []), data.get("fixture_case_ids", [])
    row.check(prefix + "every_fixture_case_exercised", bool(fixture) and exercised == fixture,
              sorted(set(fixture) - set(exercised)))
    want = expected_subcases(cases)
    got = subcase_counts(data.get("subtest_passed", []))
    diff = {f"{k[0]}|{k[1]}": [want.get(k), got.get(k)] for k in set(want) | set(got) if want.get(k) != got.get(k)}
    row.check(prefix + "complete_frozen_subcase_matrix_passed", not diff, diff)
    return sum(want.values()), sum(got.values())


def baseline_clone(row, head):
    """Row-local committed clone at the pinned base with the candidate test + fixture overlaid."""
    clone = row.dir / "baseline-clone"
    proc, _ = row.run("baseline-clone", ["git", "clone", "--local", "--no-hardlinks", "--no-checkout",
                                         "--no-recurse-submodules", REPO, clone], row.dir)
    row.check("baseline_clone_exit_zero", proc.returncode == 0, proc.returncode)
    proc, _ = row.run("baseline-clone-checkout", ["git", "checkout", "--detach", BASE], clone)
    row.check("baseline_clone_checkout_base", proc.returncode == 0, proc.returncode)
    _, clone_head = git(row, "baseline-clone-head", ["rev-parse", "HEAD"], clone)
    row.check("baseline_clone_head_is_base", clone_head == BASE, clone_head)
    for rel in PRODUCT:
        row.check("baseline_product_is_base_blob_" + rel,
                  sha256_file(clone / rel) == row.receipt["base_pinned_source_sha256"].get(rel), rel)
    overlay = {}
    for rel in (TEST_FILE, FIXTURE):
        proc, entry = row.run("candidate-blob-" + rel.replace("/", "_"), ["git", "show", head + ":" + rel], REPO)
        target = clone / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(Path(entry["stdout"]).read_bytes())
        overlay[rel] = sha256_file(target)
        row.check("overlay_equals_candidate_" + rel, proc.returncode == 0 and overlay[rel] == sha256_file(REPO / rel), rel)
    return clone, overlay


def phase_suite(args):
    row = Row("suite", args.out_base)
    manifest, head, _ = identity(row, args.source_manifest)
    staged_interpreter(row, manifest)
    preflight_gate(row, "identity_runtime")
    preflight_controls(row, args, "suite")
    cases = load_cases()
    # 1. Original baseline: pinned base product, candidate test + fixture. Must be red for the right reasons.
    clone, overlay = baseline_clone(row, head)
    proc, base = unittest_child(row, "baseline-original-product", [NEW_SUITE], cwd=clone, root=clone)
    row.check("baseline_imported_base_product", str(base.get("product_core_file", "")).startswith(str(clone.resolve())),
              base.get("product_core_file"))
    row.check("baseline_exit_nonzero", proc.returncode != 0, proc.returncode)
    failures = {short_id(t) for t in base.get("failures", [])}
    errors = {short_id(t) for t in base.get("errors", [])}
    passed = {short_id(p) for p in base.get("passed", [])}
    row.check("baseline_fix_dependent_tests_fail_by_assertion", failures == BASELINE_EXPECTED_FAIL,
              {"missing": sorted(BASELINE_EXPECTED_FAIL - failures), "unexpected": sorted(failures - BASELINE_EXPECTED_FAIL)})
    row.check("baseline_only_seamless_control_errors", errors == BASELINE_EXPECTED_ERROR, sorted(errors))
    row.check("baseline_valid_controls_pass", BASELINE_EXPECTED_PASS <= passed, sorted(BASELINE_EXPECTED_PASS - passed))
    impossible = {c["id"] for c in cases["invalid_offsets"] if c["component"] in ("minute", "second")}
    hour24 = {c["id"] for c in cases["invalid_offsets"] if c["component"] == "hour"}
    moment_failed = {subcase(t)[1] for t in base.get("subtest_failures", [])
                     if subcase(t)[0] == "MomentOffsetTests.test_invalid_offsets_blocked"}
    row.check("baseline_minute_second_subcases_normalized_not_refused", moment_failed == impossible,
              {"missing": sorted(impossible - moment_failed), "unexpected": sorted(moment_failed - impossible)})
    moment_passed = {subcase(t)[1] for t in base.get("subtest_passed", [])
                     if subcase(t)[0] == "MomentOffsetTests.test_invalid_offsets_blocked"}
    row.check("baseline_hour_24_subcases_already_refused", hour24 <= moment_passed, sorted(hour24 - moment_passed))
    nc_text = [text for text in base.get("failure_text", []) if "FC11-NC-PLUS-00-99-BLOCKED" in text]
    row.check("baseline_plus_00_99_regression_fails_with_marker", len(nc_text) == 2, len(nc_text))
    row.check("baseline_plus_00_99_normalized_to_01_39", any("1:39:00" in text for text in nc_text), None)
    # 2. Final candidate run against the real product APIs.
    proc, final = unittest_child(row, "final-candidate", [NEW_SUITE])
    row.check("final_exit_zero", proc.returncode == 0, proc.returncode)
    row.check("final_imported_candidate_product", final.get("product_core_file") == str(REPO / PRODUCT[0]),
              final.get("product_core_file"))
    required, observed = check_card_suite(row, final, "final_", cases)
    # 3. Declared negative control alone (patch out only the lexical seam; restore; rerun).
    proc, control = unittest_child(row, "negative-control", [NC_TEST])
    row.check("control_exit_zero", proc.returncode == 0, proc.returncode)
    row.check("control_killed_and_restored", control.get("passed") == [NC_TEST] and control.get("tests_run") == 1,
              control.get("passed"))
    # 4. Repeatability: a second final run reports the same passing ids and subcases.
    proc, again = unittest_child(row, "final-candidate-repeat", [NEW_SUITE])
    row.check("repeat_exit_zero", proc.returncode == 0, proc.returncode)
    row.check("repeat_identical_pass_sets", again.get("passed") == final.get("passed")
              and sorted(again.get("subtest_passed", [])) == sorted(final.get("subtest_passed", [])), None)
    controls = {"NC1": {"injected_behavior": "core._offset_components replaced by a recording no-op; "
                                             "datetime.fromisoformat parsing retained",
                        "targets": ["MomentOffsetTests.test_nc_target_plus_00_99_blocked",
                                    "RetentionEvaluateTests.test_nc_target_plus_00_99_blocked_no_hit"],
                        "required_kill": "each target exactly one AssertionError failure carrying "
                                         "FC11-NC-PLUS-00-99-BLOCKED, zero errors",
                        "valid_partner": "RetentionEvaluateTests.test_nc_partner_plus_00_00_evaluated passes under the patch",
                        "then": "restore; rerun targets and valid controls green",
                        "result": "killed_and_restored" if proc.returncode == 0 else "NOT_PASSED"}}
    return row.finish({"tests_run": final.get("tests_run"), "case_dimensions": final.get("case_dimensions"),
                       "fixture_case_count": len(final.get("fixture_case_ids", [])),
                       "required_subcases": required, "passed_subcases": observed,
                       "requirements": final.get("requirements"), "controls": controls,
                       "baseline": {"label": "ORIGINAL BASELINE (pinned base product); expected red",
                                    "clone": str(clone), "overlay_sha256": overlay,
                                    "failed_tests": sorted(failures), "errored_tests": sorted(errors),
                                    "passed_tests": sorted(passed),
                                    "failed_subcases": sorted(base.get("subtest_failures", []))}})


def phase_compatibility(args):
    row = Row("compatibility", args.out_base)
    manifest, _, _ = identity(row, args.source_manifest)
    staged_interpreter(row, manifest)
    preflight_gate(row, "identity_runtime")
    preflight_controls(row, args, "compatibility")
    proc, data = unittest_child(row, "compat-selectors", COMPAT)
    row.check("compat_exit_zero", proc.returncode == 0, proc.returncode)
    row.check("compat_no_failures_or_errors", not data.get("failures") and not data.get("errors"),
              {"failures": data.get("failures"), "errors": data.get("errors")})
    row.check("compat_zero_skipped", data.get("skipped") == [], data.get("skipped"))
    row.check("compat_ran_tests", (data.get("tests_run") or 0) > 0, data.get("tests_run"))
    row.check("compat_offline_guard_not_tripped", data.get("offline_guard_trips") == [], data.get("offline_guard_trips"))
    return row.finish({"selectors": COMPAT, "tests_run": data.get("tests_run"),
                       "passed": data.get("passed"), "skipped": data.get("skipped"),
                       "scope_note": "Scoped selectors only; full repository discovery/CI = operator-run, OWED."})


def phase_portable(args):
    row = Row("portable", args.out_base)
    manifest, _, _ = identity(row, args.source_manifest)
    staged_interpreter(row, manifest)
    preflight_gate(row, "identity_runtime")
    preflight_controls(row, args, "portable")
    cwd = row.dir / "fresh-cwd-script"
    cwd.mkdir()
    coverage = row.dir / "script-coverage.json"
    script = REPO / TEST_FILE
    proc, _ = row.run("script-run", [sys.executable, "-B", script, "--temp-root", row.tmp,
                                     "--coverage-out", coverage], cwd)
    row.check("script_exit_zero", proc.returncode == 0, proc.returncode)
    script_data = json.loads(coverage.read_text(encoding="utf-8")) if coverage.is_file() else {}
    row.check("script_full_fixture_coverage", bool(script_data.get("fixture_case_ids"))
              and script_data.get("exercised_case_ids") == script_data.get("fixture_case_ids"), None)
    row.check("script_zero_skipped_failures", script_data.get("skipped") == 0
              and script_data.get("failures") == 0 and script_data.get("errors") == 0, None)
    row.check("script_ran_all_tests", script_data.get("tests_run") == 14, script_data.get("tests_run"))
    guard = script_data.get("offline_guard") or {}
    observed = guard.get("import_observations") or []
    row.check("script_guard_installed_before_product_import", guard.get("installed_before_product_import") is True
              and guard.get("every_product_lookup_saw_active_guard") is True
              and bool(observed) and observed[0][0] == "campaign_tool", observed[:3])
    row.check("script_guard_active_through_run_then_restored", guard.get("active_after_product_import") is True
              and guard.get("active_after_run") is True and guard.get("restored") is True, None)
    row.check("script_guard_not_tripped", guard.get("trips") == [], guard.get("trips"))
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
    row.check("discovery_equal_case_coverage", bool(disc.get("exercised_case_ids"))
              and disc.get("exercised_case_ids") == script_data.get("exercised_case_ids"), None)
    row.check("discovery_same_test_count", disc.get("discovered", {}).get("tests_run") == script_data.get("tests_run"),
              disc.get("discovered", {}).get("tests_run"))
    row.check("discovery_no_skips", disc.get("discovered", {}).get("skipped") == [], None)
    row.check("discovery_offline_guard_not_tripped", disc.get("offline_guard_trips") == [], None)
    literals = ["/" + "home" + "/", "/" + "Users" + "/", "/" + "tmp" + "/", "/" + "root" + "/",
                "agent" + "-work", "." + "hermes", "C:" + "\\"]
    hits = {}
    for rel in (TEST_FILE, FIXTURE, RUNNER):
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
    manifest, _, _ = identity(row, args.source_manifest)
    runtime = manifest.get("runtime", {})
    declared = Path(runtime.get("credential_scan_python3", ""))
    row.check("interpreter_is_manifest_credential_scan_python3", Path(sys.executable) == declared
              and Path(sys.executable).resolve() == declared.resolve(), sys.executable)
    scanner = Path(runtime.get("detect_secrets", ""))
    row.check("scanner_is_manifest_detect_secrets", scanner.is_file() and REPO not in scanner.resolve().parents,
              str(scanner))
    preflight_gate(row, "identity_runtime_tools")
    preflight_controls(row, args, "credentials")
    proc, entry = row.run("detect-secrets", [scanner, "scan", "--all-files", "--exclude-files", "^.git/",
                                             "--no-verify"], REPO)
    row.check("detect_secrets_exit_zero", proc.returncode == 0, proc.returncode)
    report = Path(entry["stdout"])
    result_count, paths = None, []
    try:
        parsed = json.loads(report.read_text(encoding="utf-8"))
        row.check("scanner_json_parseable", isinstance(parsed.get("results"), dict), None)
        result_count = sum(len(v) for v in parsed["results"].values())
        paths = sorted(parsed["results"])
    except (ValueError, AttributeError) as exc:
        row.check("scanner_json_parseable", False, str(exc))
    row.check("scanner_did_not_traverse_staging_link", not [p for p in paths if p.startswith(".venv")], None)
    row.check("authored_files_have_no_scanner_results", not set(paths) & set(AUTHORED),
              sorted(set(paths) & set(AUTHORED)))
    proc, _ = row.run("check-secret-report", [sys.executable, "-B", REPO / "tools" / "check_secret_report.py",
                                              report], REPO)
    row.check("check_secret_report_exit_zero", proc.returncode == 0, proc.returncode)
    return row.finish({"scanner_report": str(report), "scanner_result_count": result_count,
                       "scanner_result_files": len(paths)})


def phase_public_tree(args):
    row = Row("public-tree", args.out_base)
    manifest, head, tree = identity(row, args.source_manifest)
    staged_interpreter(row, manifest)
    preflight_gate(row, "identity_runtime")
    preflight_controls(row, args, "public-tree")
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
    checker_hashes = {}
    for rel in UNCHANGED_CHECKERS:
        _, at_head = git(row, "blob-head-" + rel.replace("/", "_"), ["rev-parse", "HEAD:" + rel])
        _, at_base = git(row, "blob-base-" + rel.replace("/", "_"), ["rev-parse", BASE + ":" + rel])
        checker_hashes[rel] = sha256_file(REPO / rel)
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
    return row.finish({"committed_tree_scan": {"scanned_files": committed.get("scanned_files"),
                                               "findings": committed.get("findings")},
                       "in_place_informational": {"label": "INC-174 in_place_informational (not a staged-tree pass)",
                                                  "exit": proc.returncode,
                                                  "findings": in_place.get("findings"),
                                                  "scanned_files": in_place.get("scanned_files")},
                       "checker_sha256": checker_hashes, "clone": str(clone), "status_before": status})


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--phase", required=True, choices=["suite", "compatibility", "portable",
                                                           "credentials", "public-tree",
                                                           "_unittest-child", "_discovery-child"])
    parser.add_argument("--source-manifest")
    parser.add_argument("--out-base")
    parser.add_argument("--temp-root")
    parser.add_argument("--repo-root")
    parser.add_argument("--result-out")
    parser.add_argument("--names", nargs="*", default=[])
    args = parser.parse_args()
    if args.phase == "_unittest-child":
        return child_unittest(args)
    if args.phase == "_discovery-child":
        return child_discovery(args)
    if not args.source_manifest or not args.out_base:
        parser.error("--source-manifest and --out-base are required")
    try:
        return PHASES[args.phase](args)
    except PreflightRefused as exc:
        return exc.code


PHASES = {"suite": phase_suite, "compatibility": phase_compatibility, "portable": phase_portable,
          "credentials": phase_credentials, "public-tree": phase_public_tree}


if __name__ == "__main__":
    sys.exit(main())
