"""Five offline, identity-bound acceptance rows for FC10 factual event-key refusal.

Only the standard library is used here. All subprocesses use literal argv and
shell=False; every invocation retains its own stdout, stderr, and receipt files.
"""
import argparse
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import traceback
import unittest
from unittest import mock
import uuid

REPO = Path(__file__).resolve().parents[1]
PRODUCT = "campaign_tool/records/understanding/reconcile.py"
AUTHORED = ("tests/records/test_fc10_fact_event_key.py",
            "tests/fixtures/fc10-fact-event-key/cases.json",
            "tools/fc10_fact_event_key_acceptance.py", PRODUCT)
UNCHANGED = ("campaign_tool/records/understanding/policy.py",
             "campaign_tool/records/understanding/evidence.py",
             "tests/records/test_understanding.py", "tools/check_secret_report.py",
             "tools/check_public_tree.py", "campaign_tool/records/public_scan.py", ".gitignore")
TEST_MODULE = "tests.records.test_fc10_fact_event_key"
COMPATIBILITY = ("tests.records.test_understanding.ReconcileTests", "tests.records.test_understanding.PolicyTests")
PHASES = ("suite", "compatibility", "portable", "credentials", "public-tree")
SUITE_TESTS = 10
COMPATIBILITY_TESTS = 4
OFFLINE_TARGETS = ("socket.socket.connect", "socket.socket.connect_ex", "socket.create_connection",
                   "sqlite3.connect", "subprocess.Popen")
SELFTEST_PROBE = "fc10_guard_selftest_probe_absent"
GUARDED_CHILDREN = ("suite", "compatibility", "baseline")


def need(condition, label):
    if not condition:
        raise ValueError(label)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def safe_absolute(path):
    need(path.is_absolute() and ".." not in path.parts, "literal_absolute_path_required")
    need(not any(p.is_symlink() for p in (path, *path.parents)), "symlink_path_refused")
    return path


class Recorder:
    def __init__(self, root):
        self.root = root
        self.commands = []

    def run(self, label, argv, cwd, allowed=(0,)):
        need(re.fullmatch(r"[a-z0-9_-]+", label) is not None, "command_label")
        argv = [str(a) for a in argv]
        info = {"label": label, "argv": argv, "cwd": str(cwd), "shell": False}
        self.commands.append(info)
        write_json(self.root / (label + ".command.json"), info)
        try:
            proc = subprocess.run(argv, cwd=cwd, shell=False, capture_output=True, timeout=180)
        except BaseException as error:
            info.update(error=repr(error), exit=None)
            write_json(self.root / (label + ".command.json"), info)
            raise
        (self.root / (label + ".stdout")).write_bytes(proc.stdout)
        (self.root / (label + ".stderr")).write_bytes(proc.stderr)
        info.update(exit=proc.returncode, stdout_sha256=digest(proc.stdout), stderr_sha256=digest(proc.stderr))
        write_json(self.root / (label + ".command.json"), info)
        need(proc.returncode in allowed, "child_failed:" + label + ":" + str(proc.returncode))
        return proc.stdout

    def git(self, label, *argv, cwd=REPO):
        return self.run(label, ["git", *argv], cwd).decode().strip()


def resolve_target(target):
    owner_name, attr = target.rsplit(".", 1)
    owner = importlib.import_module(owner_name.split(".")[0])
    for part in owner_name.split(".")[1:]:
        owner = getattr(owner, part)
    return owner, attr


def current_targets():
    return {t: getattr(*resolve_target(t)) for t in OFFLINE_TARGETS}


def classify_guard(fn):
    # Runner-owned guard, a test's own nested scoped offline guard, or the unguarded original.
    if getattr(fn, "_fc10_offline_guard", False) is True:
        return "runner_guard"
    effect = getattr(fn, "side_effect", None) if isinstance(fn, mock.NonCallableMock) else None
    if isinstance(effect, RuntimeError) and str(effect) == "offline_api_not_allowed":
        return "nested_test_guard"
    return "unguarded"


def guard_state():
    # Classification only; no guard or original target is ever invoked by these checks.
    return {t: classify_guard(fn) for t, fn in current_targets().items()}


def all_guarded(state):
    return set(state) == set(OFFLINE_TARGETS) and all(v != "unguarded" for v in state.values())


def runner_guarded(state):
    return set(state) == set(OFFLINE_TARGETS) and all(v == "runner_guard" for v in state.values())


def none_guarded(state):
    return set(state) == set(OFFLINE_TARGETS) and all(v == "unguarded" for v in state.values())


def make_guard(target):
    def guard(*args, **kwargs):
        raise RuntimeError("offline_api_not_allowed:" + target)
    guard._fc10_offline_guard = True
    return guard


class ImportObserver:
    """Inert meta-path observer: records guard state when a product/test module is first located."""

    def __init__(self, log):
        self.log = log

    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in ("campaign_tool", "tests", SELFTEST_PROBE):
            self.log.append({"module": name, "guards": guard_state()})
        return None


class GuardScope:
    """Child-owned scoped offline guards for pure-API paths; restored on every exit."""

    def __init__(self, record):
        self.record = record

    def __enter__(self):
        self.original = current_targets()
        self.patches = [mock.patch(t, new=make_guard(t)) for t in OFFLINE_TARGETS]
        self.started = []
        try:
            for p in self.patches:
                p.start()
                self.started.append(p)
            self.observer = ImportObserver(self.record.setdefault("import_observations", []))
            sys.meta_path.insert(0, self.observer)
        except BaseException:
            self.__exit__(*sys.exc_info())
            raise
        return self

    def checkpoint(self, label):
        self.record.setdefault("checkpoints", []).append({"label": label, "guards": guard_state()})

    def __exit__(self, kind, value, tb):
        observer = getattr(self, "observer", None)
        if observer in sys.meta_path:
            sys.meta_path.remove(observer)
        for p in reversed(self.started):
            p.stop()
        restored = current_targets()
        self.record["restored"] = all(restored[t] is self.original[t] for t in OFFLINE_TARGETS)
        self.record["observer_removed"] = not any(isinstance(f, ImportObserver) for f in sys.meta_path)
        self.record["exit_exception"] = None if kind is None else kind.__name__
        return False


def guard_selftest():
    # Inert regression: guards install, the import observer fires, and an exceptional exit restores all.
    record = {}
    try:
        with GuardScope(record) as scope:
            scope.checkpoint("selftest_inside")
            # Absent top-level name: walks sys.meta_path without importing any real package.
            need(importlib.util.find_spec(SELFTEST_PROBE) is None, "selftest_probe_absent")
            raise LookupError("synthetic_selftest_exit")
    except LookupError:
        pass
    inside = record["checkpoints"][0]["guards"]
    seen = [o for o in record.get("import_observations", []) if o["module"] == SELFTEST_PROBE]
    ok = (runner_guarded(inside) and len(seen) >= 1 and all(runner_guarded(o["guards"]) for o in seen)
          and record["restored"] and record["observer_removed"] and record["exit_exception"] == "LookupError"
          and none_guarded(guard_state()))
    need(ok, "guard_selftest")
    return {"inside_all_guarded": runner_guarded(inside), "observer_fired": len(seen),
            "restored_after_exception": record["restored"], "observer_removed": record["observer_removed"]}


class GuardedResult(unittest.TextTestResult):
    # Records guard presence at the start and stop of every executed test.
    runtime = []

    def startTest(self, test):
        GuardedResult.runtime.append({"test": test.id(), "at": "start", "guards": guard_state()})
        super().startTest(test)

    def stopTest(self, test):
        super().stopTest(test)
        GuardedResult.runtime.append({"test": test.id(), "at": "stop", "guards": guard_state()})


def no_product_modules():
    return not any(n.split(".")[0] == "campaign_tool" or n.startswith("tests.records") for n in sys.modules)


def emit_guard(child, record, selftest):
    print("FC10_GUARD " + json.dumps({"child": child, "selftest": selftest, **record}, sort_keys=True))


def baseline_child(args, row, scope):
    # Observe every frozen variant against the pinned base product (from its git blob) and the candidate.
    source = safe_absolute(args.base_source)
    need(source.parent == row, "base_source_boundary")
    scope.checkpoint("before_product_import")
    importlib.import_module("campaign_tool.records.understanding")
    evidence = importlib.import_module("campaign_tool.records.understanding.evidence")
    candidate = importlib.import_module("campaign_tool.records.understanding.reconcile")
    spec = importlib.util.spec_from_file_location("campaign_tool.records.understanding.fc10_base_reconcile", source)
    base = importlib.util.module_from_spec(spec)
    scope.checkpoint("before_base_exec_module")
    spec.loader.exec_module(base)
    need(not hasattr(base, "valid_fact_event_key") and hasattr(candidate, "valid_fact_event_key"), "base_is_original")
    module = importlib.import_module(TEST_MODULE)
    scope.checkpoint("before_observations")
    observed = {"baseline": module.observe_cases(evidence, base), "candidate": module.observe_cases(evidence, candidate)}
    scope.checkpoint("after_observations")
    fixture = module.load_fixture()
    checks = []
    for case in fixture["cases"]:
        for i, variant in enumerate(case["variants"]):
            old = observed["baseline"][case["id"]][i]
            new = observed["candidate"][case["id"]][i]
            need(old["inputs_unchanged"] and new["inputs_unchanged"], "baseline_input_mutation")
            if variant["expect"] == "refusal":
                # Measured premise: base admits scalars/blank strings and fails containers incidentally.
                need(module.old_label(old) == variant["old_behavior"], "baseline_premise:" + case["id"] + ":" + str(i))
                need(new["outcome"] == "raised" and (new["type"], new["message"]) == ("ValueError", variant["error"]),
                     "candidate_refusal:" + case["id"] + ":" + str(i))
                checks.append([case["id"], i, "refusal", module.old_label(old), new["message"]])
            else:
                # Valid keys: findings, including every finding identity, are byte-for-byte unchanged.
                need(old["outcome"] == new["outcome"] == "findings" and old["result"] == new["result"],
                     "valid_findings_changed:" + case["id"] + ":" + str(i))
                checks.append([case["id"], i, "unchanged", [f["finding_id"] for f in new["result"]["findings"]]])
    print("FC10_BASELINE " + json.dumps({"observations": observed, "checks": checks,
                                         "base_source_sha256": digest(source.read_bytes())}, sort_keys=True))
    print("FC10_COVERAGE " + json.dumps({"mode": "baseline", "tests_run": 0, "case_ids": [c["id"] for c in fixture["cases"]],
                                         "variants_checked": len(checks), "skipped": 0, "successful": True,
                                         "sentinel_checks": []}, sort_keys=True))
    return 0


def child_main(args):
    # Dedicated child owns its redirects. Importing the test never installs these.
    row = safe_absolute(args.row_root)
    temp = row / ("temporary-" + args.child)
    temp.mkdir(mode=0o700, exist_ok=False)
    tempfile.tempdir = str(temp)
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(REPO))
    if args.child not in GUARDED_CHILDREN:
        # Ordinary-discovery contamination check: its sentinel must see the unguarded originals,
        # and the discovered tests install their own scoped guards before any product import.
        return test_child(args, temp, None)
    # Pure-API children: guards precede every product/test import and span execution and teardown.
    need(no_product_modules(), "product_imported_before_guards")
    selftest = guard_selftest()
    record = {}
    GuardedResult.runtime = record.setdefault("runtime", [])
    with GuardScope(record) as scope:
        scope.checkpoint("scope_entered_before_product_import")
        if args.child == "baseline":
            code = baseline_child(args, row, scope)
        else:
            code = test_child(args, temp, scope)
        scope.checkpoint("before_scope_exit")
    record["guards_after_exit"] = guard_state()
    emit_guard(args.child, record, selftest)
    need(record["restored"] and record["observer_removed"] and none_guarded(record["guards_after_exit"]),
         "guard_restore")
    return code


def test_child(args, temp, scope):
    fixture = json.loads((REPO / AUTHORED[1]).read_text())
    case_ids = [c["id"] for c in fixture["cases"]]
    loader = unittest.TestLoader()
    sentinel_checks = []
    discovery_imports = None
    if args.child == "compatibility":
        scope.checkpoint("before_compatibility_loader_import")
        suite = loader.loadTestsFromNames(list(COMPATIBILITY))
        scope.checkpoint("after_compatibility_loader_import")
    elif args.child == "suite":
        scope.checkpoint("before_suite_loader_import")
        suite = loader.loadTestsFromName(TEST_MODULE)
        scope.checkpoint("after_suite_loader_import")
    else:
        # Snapshot before ordinary discovery and again after test execution.
        ambient = (dict(os.environ), tempfile.tempdir, list(sys.path), sys.dont_write_bytecode, os.getcwd())
        import socket
        import sqlite3
        original_functions = (socket.socket.connect, socket.socket.connect_ex,
                              socket.create_connection, sqlite3.connect, subprocess.Popen)

        class Sentinel(unittest.TestCase):
            def runTest(self):
                self.assertEqual((dict(os.environ), tempfile.tempdir, list(sys.path),
                                  sys.dont_write_bytecode, os.getcwd()), ambient)
                self.assertEqual((socket.socket.connect, socket.socket.connect_ex,
                                  socket.create_connection, sqlite3.connect, subprocess.Popen), original_functions)
                sentinel_checks.append("ambient_and_unrelated_peer_unchanged")

        # Run peer before import and after discovery; also after the whole suite.
        initial = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite([Sentinel()]))
        need(initial.wasSuccessful(), "pre_discovery_sentinel")
        suite = loader.discover(str(REPO / "tests/records"),
                                pattern="test_fc10_fact_event_key.py", top_level_dir=str(REPO))
        # Discovery is inert: no product module is imported until a guarded setUp runs.
        discovery_imports = sorted(n for n in sys.modules if n.split(".")[0] == "campaign_tool")
        need(discovery_imports == [], "product_imported_at_discovery")
        immediate = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite([Sentinel()]))
        need(immediate.wasSuccessful(), "post_discovery_sentinel")
        suite = unittest.TestSuite([suite, Sentinel()])
    runner = unittest.TextTestRunner(verbosity=2, resultclass=GuardedResult if scope else None)
    result = runner.run(suite)
    if scope:
        scope.checkpoint("after_test_execution")
    need(result.wasSuccessful() and not result.skipped and not result.expectedFailures,
         "required_tests_not_all_green")
    if args.child == "compatibility":
        need(result.testsRun == COMPATIBILITY_TESTS, "compatibility_count")
        coverage = []
    else:
        module = importlib.import_module(TEST_MODULE)
        need(list(module.CASE_IDS) == case_ids, "fixture_case_coverage_mismatch")
        need(result.testsRun == SUITE_TESTS + (1 if args.child == "discovery" else 0), "suite_count")
        coverage = case_ids
    data = {"mode": args.child, "tests_run": result.testsRun, "case_ids": coverage,
            "fixture_dimensions": fixture["cases"] if coverage else [],
            "skipped": len(result.skipped), "successful": result.wasSuccessful(),
            "sentinel_checks": sentinel_checks, "temporary_root": str(temp),
            "product_modules_after_discovery": discovery_imports}
    print("FC10_COVERAGE " + json.dumps(data, sort_keys=True))
    return 0


def tagged(raw, tag):
    return [json.loads(line[len(tag) + 1:]) for line in raw.decode().splitlines() if line.startswith(tag + " ")]


def coverage(raw):
    rows = tagged(raw, "FC10_COVERAGE")
    need(len(rows) == 1 and rows[0]["successful"] and rows[0]["skipped"] == 0, "coverage_receipt")
    return rows[0]


def validate_candidate(rec, manifest, phase):
    need(manifest["schema"] == "agentorange.source-manifest/v1", "manifest_schema")
    need(Path(manifest["repo"]) == REPO, "manifest_repo")
    need(Path(manifest["write_root"]) == REPO.parent, "manifest_root")
    need(rec.git("source-remote", "remote", "get-url", "origin") == manifest["remote"], "manifest_remote")
    base, tree = manifest["head"], manifest["tree"]
    need(re.fullmatch(r"[a-f0-9]{40}", base) and re.fullmatch(r"[a-f0-9]{40}", tree), "manifest_git_identity")
    need(rec.git("base-tree", "rev-parse", base + "^{tree}") == tree, "base_tree_mismatch")
    head = rec.git("candidate-head", "rev-parse", "HEAD")
    candidate_tree = rec.git("candidate-tree", "rev-parse", "HEAD^{tree}")
    need(head != base and re.fullmatch(r"[a-f0-9]{40}", head), "committed_candidate_required")
    rec.git("ancestry", "merge-base", "--is-ancestor", base, head)
    delta = rec.git("exclusive-delta", "diff", "--name-only", base, head).splitlines()
    need(set(delta) == set(AUTHORED), "exclusive_write_set")
    status = rec.git("initial-status", "status", "--porcelain=v1", "--untracked-files=all")
    need(status in ("", "?? .venv"), "dirty_candidate")
    link = REPO / ".venv"
    need(link.is_symlink() and link.resolve() == Path(manifest["runtime"]["repo_venv"]).resolve(), "runtime_link_mismatch")
    need(Path(sys.prefix).resolve() == Path(manifest["runtime"]["repo_venv"]).resolve(), "runtime_prefix_mismatch")
    if phase == "credentials":
        need(os.path.abspath(sys.executable) == manifest["runtime"]["credential_scan_python3"], "credential_interpreter_mismatch")
    else:
        need(os.path.abspath(sys.executable) == str(REPO / ".venv/bin/python3"), "repo_interpreter_mismatch")
    hashes = {}
    for i, name in enumerate((*AUTHORED, *UNCHANGED)):
        path = REPO / name
        safe_absolute(path)
        raw = path.read_bytes()
        committed = rec.run("head-file-" + str(i), ["git", "show", head + ":" + name], REPO)
        index = rec.run("index-file-" + str(i), ["git", "show", ":" + name], REPO)
        need(raw == committed == index, "file_not_committed:" + name)
        hashes[name] = digest(raw)
        if name in UNCHANGED:
            original = rec.run("base-file-" + str(i), ["git", "show", base + ":" + name], REPO)
            need(raw == original, "baseline_file_modified:" + name)
    return {"head": head, "tree": candidate_tree, "base": base, "base_tree": tree,
            "status": status, "file_sha256": hashes,
            "interpreter": sys.executable, "interpreter_resolved": str(Path(sys.executable).resolve()),
            "prefix_resolved": str(Path(sys.prefix).resolve()), "runtime_link": str(link.resolve())}


def public_tree(rec, identity, manifest):
    initial_inventory = rec.run("path-inventory", ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], REPO)
    paths = set(initial_inventory.decode().split("\0")) - {""}
    tracked = set(rec.run("tracked-inventory", ["git", "ls-files", "-z"], REPO).decode().split("\0")) - {""}
    need(paths - tracked <= {".venv"}, "untracked_scan_visible_files")
    # One unchanged staged-tree check is explicitly informational (INC-174).
    raw = rec.run("in-place-informational", [sys.executable, "-B", REPO / "tools/check_public_tree.py"], REPO, allowed=(0, 1))
    informational = json.loads(raw)
    need(informational.get("findings") in ([], [[".venv", 0, "symlink_not_scanned"]]), "unexpected_in_place_finding")
    need(informational.get("scanned_files", 0) > 0 and "error" not in informational, "in_place_incomplete")
    code = rec.commands[-1]["exit"]
    need(code == int(bool(informational["findings"])), "in_place_exit_mismatch")
    need((REPO / ".venv").resolve() == Path(manifest["runtime"]["repo_venv"]).resolve(), "in_place_runtime_mismatch")
    clone = rec.root / "committed-tree"
    rec.run("clone", ["git", "clone", "--local", "--no-hardlinks", "--no-checkout", "--no-recurse-submodules", REPO, clone], rec.root)
    rec.run("checkout", ["git", "checkout", "--detach", identity["head"]], clone)
    need(rec.git("clone-head", "rev-parse", "HEAD", cwd=clone) == identity["head"], "clone_head")
    need(rec.git("clone-tree", "rev-parse", "HEAD^{tree}", cwd=clone) == identity["tree"], "clone_tree")
    need(not (clone / ".venv").exists() and not (clone / ".venv").is_symlink(), "clone_runtime_link_forbidden")
    for name, expected in identity["file_sha256"].items():
        need(digest((clone / name).read_bytes()) == expected, "clone_file_hash:" + name)
    clone_raw = rec.run("committed-public-scan", [sys.executable, "-B", clone / "tools/check_public_tree.py"], clone)
    result = json.loads(clone_raw)
    need(result.get("findings") == [] and result.get("scanned_files", 0) == len(tracked), "committed_public_scan_incomplete")
    need(rec.git("final-head", "rev-parse", "HEAD") == identity["head"], "source_head_changed")
    need(rec.git("final-tree", "rev-parse", "HEAD^{tree}") == identity["tree"], "source_tree_changed")
    need(rec.git("final-status", "status", "--porcelain=v1", "--untracked-files=all") == identity["status"], "source_status_changed")
    need(rec.run("final-path-inventory", ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], REPO) == initial_inventory, "source_inventory_changed")
    return {"committed_tree": result, "in_place_informational": {"incident": "INC-174", "result": informational,
            "not_a_staged_tree_pass": bool(informational["findings"])}, "scanned_files": result["scanned_files"]}


def check_guard(raw, child, root, tests_run, required_modules):
    # Parent-side validation of the child's scoped-guard evidence (import time, runtime, restoration).
    rows = tagged(raw, "FC10_GUARD")
    need(len(rows) == 1 and rows[0]["child"] == child, "guard_receipt:" + child)
    g = rows[0]
    st = g["selftest"]
    need(st["inside_all_guarded"] and st["observer_fired"] >= 1 and st["restored_after_exception"]
         and st["observer_removed"], "guard_selftest:" + child)
    need(g["restored"] and g["observer_removed"] and g["exit_exception"] is None
         and none_guarded(g["guards_after_exit"]), "guard_restoration:" + child)
    labels = [c["label"] for c in g["checkpoints"]]
    need(labels[0] == "scope_entered_before_product_import" and labels[-1] == "before_scope_exit", "guard_span:" + child)
    # Checkpoints sit outside any test, so the runner's own guards must be the active ones.
    need(all(runner_guarded(c["guards"]) for c in g["checkpoints"]), "guard_checkpoint_unguarded:" + child)
    imports = g["import_observations"]
    need(imports and all(all_guarded(o["guards"]) for o in imports), "guard_import_unguarded:" + child)
    seen = {o["module"] for o in imports}
    need(set(required_modules) <= seen, "guard_import_coverage:" + child)
    need(any(m.split(".")[0] == "campaign_tool" for m in seen), "guard_product_import_observed:" + child)
    # Test start/stop sit outside setUp/cleanup nesting, so runner guards are active there too.
    need(len(g["runtime"]) == 2 * tests_run and all(runner_guarded(r["guards"]) for r in g["runtime"]),
         "guard_runtime:" + child)
    write_json(root / ("guard-" + child + ".json"), g)
    return {"child": child, "import_observations": len(imports), "required_modules_observed": sorted(required_modules),
            "checkpoints": labels, "runtime_observations": len(g["runtime"]), "restored": True}


def run_phase(args, rec, identity, manifest):
    fixture = json.loads((REPO / AUTHORED[1]).read_text())
    required_ids = [c["id"] for c in fixture["cases"]]
    if args.phase in ("suite", "compatibility"):
        raw = rec.run(args.phase, [sys.executable, "-B", Path(__file__).resolve(), "--child", args.phase,
                                  "--row-root", rec.root], REPO)
        data = coverage(raw)
        need(data["case_ids"] == (required_ids if args.phase == "suite" else []), "required_case_coverage")
        modules = (("tests.records.test_understanding", "campaign_tool.records.understanding.reconcile")
                   if args.phase == "compatibility" else
                   (TEST_MODULE, "campaign_tool.records.understanding.reconcile"))
        data["guard"] = check_guard(raw, args.phase, rec.root, data["tests_run"], modules)
        if args.phase == "compatibility":
            return data
        # Separately preserved: injected control and final candidate observations from the suite.
        controls = tagged(raw, "FC10_CONTROL")
        need(len(controls) == 1, "negative_control_receipt")
        need(controls[0]["killed_assertion"] == fixture["negative_control"]["killed_assertion"], "negative_control_label")
        write_json(rec.root / "observations-injected-control.json", {
            k: controls[0][k] for k in ("injected_behavior", "killed_assertion", "injected_observations",
                                        "injected_prerequisites")})
        write_json(rec.root / "observations-final-candidate.json", {
            k: controls[0][k] for k in ("pre_injection_candidate", "final_candidate_output")})
        # Actual unmodified baseline: the pinned base product bytes, read from the base git blob.
        base_bytes = rec.run("base-product", ["git", "show", identity["base"] + ":" + PRODUCT], REPO)
        base_source = rec.root / "base-reconcile.py"
        base_source.write_bytes(base_bytes)
        base_raw = rec.run("baseline", [sys.executable, "-B", Path(__file__).resolve(), "--child", "baseline",
                                        "--row-root", rec.root, "--base-source", base_source], REPO)
        base_cov = coverage(base_raw)
        data["baseline_guard"] = check_guard(base_raw, "baseline", rec.root, 0,
                                             ("campaign_tool.records.understanding.reconcile", TEST_MODULE))
        baseline = tagged(base_raw, "FC10_BASELINE")
        need(len(baseline) == 1 and baseline[0]["base_source_sha256"] == digest(base_bytes), "baseline_receipt")
        variants = sum(len(c["variants"]) for c in fixture["cases"])
        need(base_cov["variants_checked"] == len(baseline[0]["checks"]) == variants, "baseline_variant_coverage")
        write_json(rec.root / "observations-unmodified-baseline.json", baseline[0])
        data.update(baseline_variants_checked=variants, baseline_checks=baseline[0]["checks"],
                    base_product_sha256=digest(base_bytes), candidate_product_sha256=identity["file_sha256"][PRODUCT])
        return data
    if args.phase == "portable":
        findings = []
        for name in AUTHORED:
            text = (REPO / name).read_text()
            # No platform-specific absolute paths in any authored source/fixture.
            for line_no, line in enumerate(text.splitlines(), 1):
                if re.search(r"/(?:home|Users|workspace|tmp|var/tmp)/|[A-Za-z]:\\", line):
                    findings.append([name, line_no])
        need(findings == [], "host_specific_authored_literal")
        other_cwd = rec.root / "fresh-cwd"
        other_cwd.mkdir(mode=0o700)
        raw = rec.run("portable-script", [sys.executable, "-B", REPO / AUTHORED[0],
                      "--temporary-root", rec.root / "script-temporary"], other_cwd)
        script = coverage(raw)
        discovery_raw = rec.run("portable-discovery", [sys.executable, "-B", Path(__file__).resolve(),
                                "--child", "discovery", "--row-root", rec.root], other_cwd)
        discovery = coverage(discovery_raw)
        need(script["case_ids"] == discovery["case_ids"] == required_ids, "portable_case_coverage")
        need(len(discovery["sentinel_checks"]) == 3, "sentinel_coverage")
        need(discovery.get("product_modules_after_discovery") == [], "discovery_product_import")
        return {"script": script, "discovery": discovery, "static_findings": findings}
    if args.phase == "credentials":
        runtime = manifest["runtime"]
        scanner = Path(runtime["detect_secrets"])
        need(scanner.is_absolute() and scanner.is_file() and os.access(scanner, os.X_OK), "scanner_unavailable")
        need(scanner.parent == Path(runtime["credential_scan_python3"]).parent, "scanner_runtime_mismatch")
        raw = rec.run("detect-secrets", [scanner, "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"], REPO)
        report = json.loads(raw)
        need(type(report.get("results")) is dict, "scanner_report_shape")
        report_path = rec.root / "scanner-report.json"
        report_path.write_bytes(raw)
        rec.run("check-secret-report", [sys.executable, REPO / "tools/check_secret_report.py", report_path], REPO)
        return {"scanner_report_sha256": digest(raw), "scanner_result_files": len(report["results"]),
                "scanner": str(scanner), "credential_scan_python3": sys.executable}
    return public_tree(rec, identity, manifest)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=PHASES)
    parser.add_argument("--source-manifest", type=Path)
    parser.add_argument("--out-base", type=Path)
    parser.add_argument("--child", choices=("suite", "compatibility", "discovery", "baseline"))
    parser.add_argument("--row-root", type=Path)
    parser.add_argument("--base-source", type=Path)
    args = parser.parse_args()
    if args.child:
        need(args.row_root is not None and args.phase is None, "child_arguments")
        need((args.base_source is not None) == (args.child == "baseline"), "baseline_arguments")
        return child_main(args)
    if args.phase is None or args.source_manifest is None or args.out_base is None:
        raise ValueError("phase_arguments")
    safe_absolute(args.source_manifest)
    safe_absolute(args.out_base)
    need(args.out_base == REPO.parent / "out", "output_boundary")
    args.out_base.mkdir(mode=0o700, parents=True, exist_ok=True)
    root = args.out_base / (args.phase + "-" + uuid.uuid4().hex)
    root.mkdir(mode=0o700)
    temp = root / "parent-temporary"
    temp.mkdir(mode=0o700)
    tempfile.tempdir = str(temp)
    sys.dont_write_bytecode = True
    rec = Recorder(root)
    receipt = {"schema": "fc10-fact-event-key-acceptance/v1", "phase": args.phase,
               "argv": [sys.executable, *sys.argv], "cwd": os.getcwd(), "run_root": str(root),
               "source_manifest": str(args.source_manifest), "manifest_sha256": digest(args.source_manifest.read_bytes()),
               "status": "FAIL", "review_required": True, "remote_ci": "OWED",
               "semantic_legal_human_live_acceptance": False}
    exit_code = 1
    try:
        need(Path.cwd().resolve() == REPO, "acceptance_cwd")
        manifest = json.loads(args.source_manifest.read_text())
        identity = validate_candidate(rec, manifest, args.phase)
        receipt["candidate"] = identity
        receipt["results"] = run_phase(args, rec, identity, manifest)
        # All rows bind to unchanged final identity, not merely their initial HEAD.
        need(rec.git("row-end-head", "rev-parse", "HEAD") == identity["head"], "row_head_changed")
        need(rec.git("row-end-status", "status", "--porcelain=v1", "--untracked-files=all") == identity["status"], "row_status_changed")
        for name, expected in identity["file_sha256"].items():
            need(digest((REPO / name).read_bytes()) == expected, "row_source_changed:" + name)
        need(digest(args.source_manifest.read_bytes()) == receipt["manifest_sha256"], "row_manifest_changed")
        receipt["status"] = "PASS_REVIEW_REQUIRED"
        exit_code = 0
    except BaseException as error:
        receipt["error"] = repr(error)
        (root / "failure.txt").write_text(traceback.format_exc())
        print(traceback.format_exc(), file=sys.stderr)
    finally:
        receipt["commands"] = rec.commands
        # Every retained output file is hashed, recursively. The disposable committed-tree clone
        # is bound by its checked HEAD/tree/file hashes instead and is listed as excluded.
        files = sorted(p for p in root.rglob("*") if p.is_file() and not p.is_symlink()
                       and "committed-tree" not in p.relative_to(root).parts)
        names = [p.relative_to(root).as_posix() for p in files]
        receipt["artifact_paths_unique_canonical"] = (len(names) == len(set(names))
                                                      and all(".." not in n.split("/") for n in names))
        receipt["artifact_sha256"] = {n: digest(p.read_bytes()) for n, p in zip(names, files)}
        receipt["artifact_excluded_trees"] = (["committed-tree"] if (root / "committed-tree").exists() else [])
        receipt["limitations"] = ["REVIEW REQUIRED; not semantic, legal, human or live acceptance",
                                  "scoped local rows are not full-repository CI; full discovery = operator-run",
                                  "required exact-head remote CI remains OWED"]
        write_json(root / "receipt.json", receipt)
    print(json.dumps({"phase": args.phase, "status": receipt["status"], "receipt": str(root / "receipt.json"),
                      "receipt_sha256": digest((root / "receipt.json").read_bytes()),
                      "head": receipt.get("candidate", {}).get("head")}, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
