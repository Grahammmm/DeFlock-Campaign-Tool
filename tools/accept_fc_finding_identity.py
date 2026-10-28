"""Acceptance runner for FC-FINDING-IDENTITY (offline, synthetic, review-required).

Usage (one check per invocation):
    <interpreter> tools/accept_fc_finding_identity.py --check CHECK --output-root DIR

CHECK is one of: focused, compatibility, negative, portability, credentials,
public-tree. Each invocation creates a fresh unique child directory under DIR
(never overwriting earlier evidence) holding raw child stdout/stderr, argv,
cwd, exit codes, executed test counts/IDs and source hashes in ``run.json``.
The process exits 0 only when every assertion of the selected check holds.

The repository root is derived from this file. Import search paths are set
only inside this script. Children are argument arrays without a shell; the
interpreter is the one running this script. A passing result is machine
validity only: REVIEW REQUIRED, never semantic/legal acceptance, activation or
publication authority.
"""
import argparse
import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import types
import unittest

sys.dont_write_bytecode = True

REPO = Path(__file__).resolve().parents[1]
CHECKS = ("focused", "compatibility", "negative", "portability", "credentials", "public-tree")
BASE_SELECTOR = "parent of the first commit adding tests/records/test_fc_finding_identity.py"
RECONCILE_PATH = "campaign_tool/records/understanding/reconcile.py"
FOCUSED_MODULE = "tests.records.test_fc_finding_identity"
COMPAT_TARGET = "tests.records.test_understanding.ReconcileTests"
HASHED_SOURCES = (
    RECONCILE_PATH,
    "tests/records/test_fc_finding_identity.py",
    "tests/records/test_understanding.py",
    "tools/accept_fc_finding_identity.py",
    "tools/check_secret_report.py",
    "tools/check_public_tree.py",
)
STATIC_SCANNED = ("tests/records/test_fc_finding_identity.py", "tools/accept_fc_finding_identity.py")
# Assembled from parts so this list does not match its own source text.
BOX_SPECIFIC = ("/ho" + "me/", "agent" + "-work", "venv" + "-fullsuite", "/Us" + "ers/", ".her" + "mes")
INLINE_FORMS = (
    re.compile(r"""["']-""" + r"""[ce]["']"""),
    re.compile("shell" + r"\s*=\s*True"),
    re.compile("PYTHON" + "PATH"),
    re.compile(r"\bos\.sys" + r"tem\("),
)
RAN = re.compile(r"^Ran (\d+) tests? in ", re.M)
SUMMARY = re.compile(r"^(OK|FAILED)(?: \(([^)]*)\))?\s*$", re.M)
VERBOSE = re.compile(r"^(test\w*) \(([\w.]+)\)(?:\n[^\n]*?)? \.\.\. (ok|FAIL|ERROR|skipped.*|expected failure|unexpected success)$",
                     re.M)


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    return sha256_bytes(Path(path).read_bytes())


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def new_run_dir(output_root, check):
    root = Path(output_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    run = root / ("{}-{}-{}-{}".format(check, now(), os.getpid(), secrets.token_hex(4)))
    run.mkdir(exist_ok=False)
    return run


def source_hashes(root):
    hashes = {}
    for relative in HASHED_SOURCES:
        path = root / relative
        hashes[relative] = sha256_file(path) if path.is_file() else None
    return hashes


def git_identity():
    identity = {}
    for name, argv in (("head", ["git", "-C", str(REPO), "rev-parse", "HEAD"]),
                       ("status_porcelain", ["git", "-C", str(REPO), "status", "--porcelain", "--untracked-files=all"])):
        try:
            done = subprocess.run(argv, capture_output=True, text=True, timeout=60, check=False)
            identity[name] = done.stdout.strip() if done.returncode == 0 else None
            identity[name + "_exit"] = done.returncode
        except (OSError, subprocess.SubprocessError) as error:
            identity[name] = None
            identity[name + "_error"] = type(error).__name__
    identity["clean"] = identity.get("status_porcelain") == ""
    return identity


def run_child(run_dir, label, argv, cwd, env=None, stdout_path=None, timeout=900):
    """Run one argument-array child (no shell) and preserve its raw streams."""
    record = {"label": label, "argv": [str(a) for a in argv], "cwd": str(cwd)}
    out_file = Path(stdout_path) if stdout_path else run_dir / (label + ".stdout")
    err_file = run_dir / (label + ".stderr")
    try:
        with open(out_file, "xb") as out, open(err_file, "xb") as err:
            done = subprocess.run([str(a) for a in argv], cwd=str(cwd), stdout=out, stderr=err,
                                  stdin=subprocess.DEVNULL, env=env, timeout=timeout, check=False)
        record["exit"] = done.returncode
    except (OSError, subprocess.SubprocessError) as error:
        record["exit"] = None
        record["launch_error"] = type(error).__name__
    for key, path in (("stdout", out_file), ("stderr", err_file)):
        if path.is_file():
            record[key + "_path"] = str(path)
            record[key + "_sha256"] = sha256_file(path)
    return record


def verbose_test_id(name, parenthesized):
    """Actual TestCase.id() from one verbose line.

    Python 3.11+ prints ``name (module.Class.name)``; older runtimes printed
    ``name (module.Class)``. Only the latter needs the method appended.
    """
    if parenthesized.endswith("." + name):
        return parenthesized
    return parenthesized + "." + name


def parse_unittest(stderr_text):
    ran = RAN.findall(stderr_text)
    summary = SUMMARY.findall(stderr_text)
    tests = [{"name": m.group(1), "id": verbose_test_id(m.group(1), m.group(2)),
              "outcome": "skipped" if m.group(3).startswith("skipped") else m.group(3)}
             for m in VERBOSE.finditer(stderr_text)]
    detail = summary[-1][1] if summary else ""
    counts = dict((k.strip(), int(v)) for k, v in (part.split("=") for part in detail.split(", ") if "=" in part))
    return {"tests_run": int(ran[-1]) if ran else 0, "summary": summary[-1][0] if summary else None,
            "summary_detail": detail, "summary_counts": counts, "tests": tests}


class _ParserControl(unittest.TestCase):
    """Synthetic in-memory outcomes used only to check the verbose parser."""

    def test_control_ok(self):
        pass

    def test_control_documented(self):
        """A first docstring line printed beneath the verbose test line."""

    def test_control_fail(self):
        self.fail("deliberate synthetic failure")

    def test_control_error(self):
        raise ValueError("deliberate synthetic error")

    @unittest.skip("deliberate synthetic skip")
    def test_control_skip(self):
        pass


def parser_self_check():
    """Run known ok/docstring/fail/error/skip cases; parsed IDs must equal case.id()."""
    suite = unittest.TestLoader().loadTestsFromTestCase(_ParserControl)
    actual = {case.id(): "ok" for case in suite}  # Collected before run(), which releases cases.
    stream = io.StringIO()
    outcome = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    for case, _ in outcome.failures:
        actual[case.id()] = "FAIL"
    for case, _ in outcome.errors:
        actual[case.id()] = "ERROR"
    for case, _ in outcome.skipped:
        actual[case.id()] = "skipped"
    parsed = parse_unittest(stream.getvalue())
    recorded = {t["id"]: t["outcome"] for t in parsed["tests"]}
    return {"expected": actual, "parsed": recorded, "tests_run": parsed["tests_run"],
            "match": recorded == actual and len(parsed["tests"]) == len(actual) == parsed["tests_run"],
            "python_version": sys.version}


def loaded_suite_ids(target, root):
    """IDs of the suite unittest would execute for ``target`` (loaded, not run)."""
    root_text = str(root)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    suite = unittest.TestLoader().loadTestsFromName(target)
    ids = []

    def walk(item):
        if isinstance(item, unittest.TestSuite):
            for child in item:
                walk(child)
        else:
            ids.append(item.id())
    walk(suite)
    return ids


def unittest_child(run_dir, label, target, root=None):
    root = root or REPO
    argv = [sys.executable, "-B", "-m", "unittest", "-v", target]
    record = run_child(run_dir, label, argv, root)
    stderr_text = Path(record["stderr_path"]).read_text(errors="replace") if "stderr_path" in record else ""
    parsed = parse_unittest(stderr_text)
    record.update(parsed)
    problems = []
    record["parser_self_check"] = parser_self_check()
    if not record["parser_self_check"]["match"]:
        problems.append("parser_self_check_failed")
    try:
        expected_ids = loaded_suite_ids(target, root)
    except Exception as error:  # Unloadable suite: fail closed, keep the class.
        expected_ids = None
        record["loaded_suite_error"] = repr(error)[:2000]
    parsed_ids = [t["id"] for t in parsed["tests"]]
    record["loaded_suite_ids"] = expected_ids
    record["parsed_ids_match_loaded_suite"] = (expected_ids is not None and len(parsed_ids) == len(set(parsed_ids))
                                               and sorted(parsed_ids) == sorted(expected_ids))
    if not record["parsed_ids_match_loaded_suite"]:
        problems.append("parsed_ids_differ_from_loaded_suite")
    if record.get("exit") != 0:
        problems.append("unittest_exit_nonzero")
    if parsed["summary"] != "OK":
        problems.append("unittest_summary_not_ok")
    if parsed["tests_run"] <= 0:
        problems.append("no_tests_executed")
    if len(parsed["tests"]) != parsed["tests_run"]:
        problems.append("executed_test_ids_incomplete")
    extras = sorted(k for k in parsed["summary_counts"] if k in ("skipped", "expected failures", "unexpected successes"))
    if extras:
        problems.append("unexpected_" + "_".join(k.replace(" ", "_") for k in extras))
    bad = [t["id"] for t in parsed["tests"] if t["outcome"] != "ok"]
    if bad:
        problems.append("non_ok_tests")
    record["non_ok_test_ids"] = bad
    record["problems"] = problems
    return record, problems


def _load_in_process(root):
    root_text = str(root)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    from campaign_tool.records.understanding import reconcile
    focused = __import__(FOCUSED_MODULE, fromlist=["*"])
    for module in (reconcile, focused):
        if not Path(module.__file__).resolve().is_relative_to(Path(root).resolve()):
            raise RuntimeError("module_loaded_outside_candidate")
    return reconcile, focused


def baseline_commit():
    """Pinned base = parent of the commit that first added the focused test module.

    Derived from history (no high-entropy literal in source); run.json records
    the resolved commit so a reviewer can compare it with the frozen brief pin.
    """
    done = subprocess.run(["git", "-C", str(REPO), "log", "--format=%H", "--diff-filter=A", "HEAD", "--",
                           "tests/records/test_fc_finding_identity.py"],
                          capture_output=True, text=True, timeout=60, check=True)
    added = [line for line in done.stdout.split() if line]
    if not added:
        raise RuntimeError("focused_test_commit_not_found")
    parent = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--verify", added[-1] + "^1"],
                            capture_output=True, text=True, timeout=60, check=True)
    return parent.stdout.strip()


def _baseline_reconcile():
    """Build the pinned-baseline reconcile module in memory from the git object."""
    base = baseline_commit()
    done = subprocess.run(["git", "-C", str(REPO), "show", base + ":" + RECONCILE_PATH],
                          capture_output=True, timeout=60, check=True)
    source = done.stdout
    module = types.ModuleType("campaign_tool.records.understanding._fc_baseline_reconcile")
    module.__package__ = "campaign_tool.records.understanding"
    module.__file__ = "<git {}:{}>".format(base, RECONCILE_PATH)
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    return module, sha256_bytes(source), base


def _projection(result):
    return [(f.get("family_id"), f.get("requirement_id"), f.get("event_key"), f.get("classification"),
             f.get("reason"), f.get("legal_assessment"), f.get("review_acceptance"))
            for f in result["findings"]] + [("legal_conclusions", result.get("legal_conclusions"),
                                             result.get("status"), result.get("review_acceptance"))]


def baseline_differential(run_dir, result):
    """Old/new classification comparison over every shared synthetic scenario."""
    problems = []
    reconcile, focused = _load_in_process(REPO)
    baseline, baseline_sha, base = _baseline_reconcile()
    rows = []
    for name, (_f, _v, _b, _s, expected) in sorted(focused.scenarios().items()):
        old = focused.run_scenario(name, compare=baseline.compare)
        new = focused.run_scenario(name, compare=reconcile.compare)
        row = {"scenario": name, "expected": expected, "old": _projection(old), "new": _projection(new),
               "old_finding_ids": [f.get("finding_id") for f in old["findings"]],
               "new_finding_ids": [f.get("finding_id") for f in new["findings"]],
               "new_identity_versions": [f.get("finding_identity_version") for f in new["findings"]]}
        row["classification_equal"] = row["old"] == row["new"]
        row["expected_matches_old"] = [p[3] for p in row["old"][:-1]] == [expected]
        rows.append(row)
    ids = {r["scenario"]: r for r in rows}
    measured = {
        "old_changed_value_same_id": ids["baseline_value_30"]["old_finding_ids"] == ids["value_45"]["old_finding_ids"],
        "old_reordered_facts_changed_id":
            ids["two_facts"]["old_finding_ids"] != ids["two_facts_reversed"]["old_finding_ids"],
        "new_changed_value_same_id": ids["baseline_value_30"]["new_finding_ids"] == ids["value_45"]["new_finding_ids"],
        "new_reordered_facts_changed_id":
            ids["two_facts"]["new_finding_ids"] != ids["two_facts_reversed"]["new_finding_ids"],
    }
    path = run_dir / "baseline-differential.json"
    with open(path, "x") as handle:
        json.dump({"baseline_commit": base, "baseline_selector": BASE_SELECTOR,
                   "baseline_reconcile_sha256": baseline_sha,
                   "rows": rows, "measured": measured}, handle, indent=2, sort_keys=True)
    result["baseline_differential"] = {
        "path": str(path), "sha256": sha256_file(path), "scenarios": len(rows), "baseline_commit": base,
        "baseline_reconcile_sha256": baseline_sha, "measured": measured,
        "classification_mismatches": [r["scenario"] for r in rows if not r["classification_equal"]],
        "expected_table_mismatches": [r["scenario"] for r in rows if not r["expected_matches_old"]]}
    if not rows:
        problems.append("differential_no_scenarios")
    if result["baseline_differential"]["classification_mismatches"]:
        problems.append("classification_changed_vs_baseline")
    if result["baseline_differential"]["expected_table_mismatches"]:
        problems.append("expected_table_differs_from_baseline")
    if not (measured["old_changed_value_same_id"] and measured["old_reordered_facts_changed_id"]):
        problems.append("baseline_defect_not_reproduced")
    if measured["new_changed_value_same_id"] or measured["new_reordered_facts_changed_id"]:
        problems.append("candidate_identity_defect_present")
    if any(v != ["content-bound-v2"] for v in (r["new_identity_versions"] for r in rows)):
        problems.append("identity_version_missing")
    return problems


# ----------------------------------------------------------------- checks


def check_focused(run_dir, result):
    record, problems = unittest_child(run_dir, "focused-unittest", FOCUSED_MODULE)
    result["children"].append(record)
    try:
        problems = problems + baseline_differential(run_dir, result)
    except (subprocess.SubprocessError, OSError, KeyError, ValueError, RuntimeError) as error:
        problems = problems + ["baseline_differential_incomplete:" + type(error).__name__]
        result["baseline_differential_error"] = repr(error)[:2000]
    return problems


def check_compatibility(run_dir, result):
    record, problems = unittest_child(run_dir, "compatibility-unittest", COMPAT_TARGET)
    result["children"].append(record)
    return problems


def _run_cases(focused, names):
    loader = unittest.TestLoader()
    suite = unittest.TestSuite(loader.loadTestsFromName(name, focused) for name in names)
    stream = io.StringIO()
    outcome = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    return outcome, stream.getvalue()


def _run_module(focused):
    suite = unittest.TestLoader().loadTestsFromModule(focused)
    stream = io.StringIO()
    outcome = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    return outcome, stream.getvalue()


NEGATIVE_REGRESSIONS = (
    "EvidenceBindingTests.test_same_fact_id_different_cited_value_changes_identity",
    "IdentityContractTests.test_fact_order_permutation_keeps_identity_and_rendering",
)
NEGATIVE_POSITIVE = "ClassificationPreservedTests.test_every_scenario_keeps_baseline_classification"


def _summarise(outcome):
    return {"tests_run": outcome.testsRun,
            "failures": [case.id() for case, _ in outcome.failures],
            "errors": [case.id() for case, _ in outcome.errors],
            "skipped": [case.id() for case, _ in outcome.skipped],
            "unexpected_successes": [case.id() for case in outcome.unexpectedSuccesses],
            "successful": outcome.wasSuccessful()}


def check_negative(run_dir, result):
    """In-memory mutant: only the identity helper reverts to the old fact-ID preimage."""
    problems = []
    reconcile, focused = _load_in_process(REPO)
    original = reconcile.finding_identity
    evidence = reconcile.evidence

    def old_fact_id_only(**parts):
        scope = parts["scope"]
        return evidence.sha([scope["agency_id"], scope["request_number"], parts["family_id"],
                             parts["requirement"]["requirement_id"], parts["event_key"],
                             [f["fact_id"] for f in parts["facts"]], parts["rule_selection"]])

    phases = {}
    try:
        reconcile.finding_identity = old_fact_id_only
        regress, regress_text = _run_cases(focused, NEGATIVE_REGRESSIONS)
        positive, positive_text = _run_cases(focused, [NEGATIVE_POSITIVE])
        phases["mutant_regressions"] = _summarise(regress)
        phases["mutant_positive"] = _summarise(positive)
        phases["mutant_direct"] = {
            "changed_value_same_id": focused.finding_id("baseline_value_30") == focused.finding_id("value_45"),
            "reordered_facts_changed_id":
                focused.finding_id("two_facts") != focused.finding_id("two_facts_reversed")}
        (run_dir / "negative-mutant.txt").write_text(regress_text + "\n" + positive_text)
    finally:
        reconcile.finding_identity = original
    phases["restored_identity_helper"] = reconcile.finding_identity is original
    restored, restored_text = _run_module(focused)
    phases["restored"] = _summarise(restored)
    phases["restored_direct"] = {
        "changed_value_same_id": focused.finding_id("baseline_value_30") == focused.finding_id("value_45"),
        "reordered_facts_changed_id": focused.finding_id("two_facts") != focused.finding_id("two_facts_reversed")}
    (run_dir / "negative-restored.txt").write_text(restored_text)
    for name in ("negative-mutant.txt", "negative-restored.txt"):
        phases[name + "_sha256"] = sha256_file(run_dir / name)
    result["negative"] = phases
    mutant = phases["mutant_regressions"]
    detected = set(mutant["failures"] + mutant["errors"])
    expected = {focused.__name__ + "." + name for name in NEGATIVE_REGRESSIONS}
    if mutant["tests_run"] != len(NEGATIVE_REGRESSIONS) or detected != expected:
        problems.append("mutant_not_detected_by_every_regression")
    if not (phases["mutant_direct"]["changed_value_same_id"] and phases["mutant_direct"]["reordered_facts_changed_id"]):
        problems.append("mutant_did_not_reproduce_both_baseline_defects")
    if not phases["mutant_positive"]["successful"] or phases["mutant_positive"]["tests_run"] != 1:
        problems.append("classification_positive_not_green_under_mutant")
    if not phases["restored_identity_helper"]:
        problems.append("identity_helper_not_restored")
    if not phases["restored"]["successful"] or phases["restored"]["tests_run"] <= len(NEGATIVE_REGRESSIONS) \
            or phases["restored"]["skipped"]:
        problems.append("restored_positive_not_green")
    if phases["restored_direct"]["changed_value_same_id"] or phases["restored_direct"]["reordered_facts_changed_id"]:
        problems.append("restored_identity_defect_present")
    return problems


def static_inspection(root):
    findings = []
    for relative in STATIC_SCANNED:
        text = (root / relative).read_text()
        for number, line in enumerate(text.splitlines(), 1):
            for token in BOX_SPECIFIC:
                if token in line:
                    findings.append({"file": relative, "line": number, "kind": "box_specific_path"})
            for pattern in INLINE_FORMS:
                if pattern.search(line):
                    findings.append({"file": relative, "line": number, "kind": "inline_or_shell_form"})
    return findings


def candidate_files():
    done = subprocess.run(["git", "-C", str(REPO), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                          capture_output=True, timeout=120, check=True)
    names = [name for name in done.stdout.decode().split("\0") if name]
    return sorted(set(names))


def check_portability(run_dir, result):
    problems = []
    findings = static_inspection(REPO)
    result["static_inspection"] = {"files": list(STATIC_SCANNED), "findings": findings}
    if findings:
        problems.append("static_box_specific_or_inline_findings")
    with tempfile.TemporaryDirectory(prefix="fc-identity-copy-", dir=str(run_dir)) as temporary:
        copy_root = Path(temporary) / "relocated" / "candidate-source"
        copied = 0
        for name in candidate_files():
            source = REPO / name
            if source.is_symlink() or not source.is_file():
                continue
            target = copy_root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            copied += 1
        copy_hashes = source_hashes(copy_root)
        result["portability_copy"] = {"prefix": str(copy_root), "files_copied": copied,
                                      "source_hashes_match": copy_hashes == result["source_hashes"]}
        if copy_hashes != result["source_hashes"]:
            problems.append("copy_hashes_differ")
        home = Path(temporary) / "isolated-home"
        home.mkdir()
        # Inherit the environment; redirect only data/home defaults (no search-path variables).
        env = dict(os.environ)
        env.update({"HOME": str(home), "TMPDIR": str(home), "XDG_CONFIG_HOME": str(home),
                    "XDG_DATA_HOME": str(home), "XDG_CACHE_HOME": str(home)})
        report = run_dir / "portability-child.json"
        argv = [sys.executable, "-I", "-B", str(copy_root / "tools" / "accept_fc_finding_identity.py"),
                "--portability-child-report", str(report)]
        record = run_child(run_dir, "portability-child", argv, copy_root, env=env)
        result["children"].append(record)
        if record.get("exit") != 0:
            problems.append("portability_child_failed")
        if not report.is_file():
            problems.append("portability_report_missing")
            return problems
        child = json.loads(report.read_text())
        record["report_sha256"] = sha256_file(report)
        result["portability_child"] = {k: child.get(k) for k in
                                       ("repo_root", "tests_run", "failures", "errors", "skipped",
                                        "sentinel_hits", "modules_outside_copy", "loaded_modules")}
        if child.get("repo_root") != str(copy_root.resolve()):
            problems.append("child_did_not_use_copy")
        if not child.get("tests_run") or child.get("failures") or child.get("errors") or child.get("skipped"):
            problems.append("child_focused_not_green")
        if child.get("sentinel_hits"):
            problems.append("network_or_process_sentinel_triggered")
        if child.get("modules_outside_copy") or not child.get("loaded_modules"):
            problems.append("application_modules_loaded_outside_copy")
    return problems


def portability_child(report):
    """Runs inside the relocated copy under -I: sentinels before any app import."""
    import socket
    hits = []

    def refuse(name):
        def blocked(*_args, **_kwargs):
            hits.append(name)
            raise PermissionError("offline sentinel: " + name)
        return blocked

    socket.socket.connect = refuse("socket.connect")
    socket.socket.connect_ex = refuse("socket.connect_ex")
    socket.create_connection = refuse("socket.create_connection")
    socket.getaddrinfo = refuse("socket.getaddrinfo")
    subprocess.Popen.__init__ = refuse("subprocess.Popen")
    os.system = refuse("os.system")
    for name in ("execv", "execve", "execvp", "execvpe", "fork", "forkpty", "posix_spawn", "posix_spawnp"):
        if hasattr(os, name):
            setattr(os, name, refuse("os." + name))
    root = REPO.resolve()
    sys.path.insert(0, str(root))
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromName(FOCUSED_MODULE)
    stream = io.StringIO()
    outcome = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    loaded = {}
    outside = []
    for name, module in sorted(sys.modules.items()):
        if name.split(".")[0] not in ("campaign_tool", "tests"):
            continue
        path = getattr(module, "__file__", None)
        if path is None:
            continue
        loaded[name] = path
        if not Path(path).resolve().is_relative_to(root):
            outside.append(name)
    data = {"repo_root": str(root), "tests_run": outcome.testsRun,
            "failures": [c.id() for c, _ in outcome.failures], "errors": [c.id() for c, _ in outcome.errors],
            "skipped": [c.id() for c, _ in outcome.skipped], "sentinel_hits": hits,
            "modules_outside_copy": outside, "loaded_modules": loaded, "unittest_output": stream.getvalue()}
    with open(report, "x") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
    ok = outcome.wasSuccessful() and outcome.testsRun > 0 and not outcome.skipped and not hits and not outside
    return 0 if ok else 1


def check_credentials(run_dir, result):
    problems = []
    scanner = Path(sys.executable).parent / "detect-secrets"
    if not scanner.is_file():
        result["scanner"] = {"path": str(scanner), "present": False}
        return ["detect_secrets_unavailable_adjacent_to_interpreter"]
    report = run_dir / "secret-scan.json"
    scan = run_child(run_dir, "detect-secrets-scan",
                     [scanner, "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"],
                     REPO, stdout_path=report)
    result["children"].append(scan)
    version = run_child(run_dir, "detect-secrets-version", [scanner, "--version"], REPO)
    result["children"].append(version)
    if scan.get("exit") != 0:
        problems.append("secret_scan_failed")
    try:
        data = json.loads(report.read_text())
        if not isinstance(data.get("results"), dict):
            raise ValueError("results")
        result["secret_scan"] = {"files_with_hits": len(data["results"]),
                                 "raw_hits": sum(len(v) for v in data["results"].values())}
    except (OSError, ValueError, AttributeError):
        problems.append("secret_report_malformed")
        return problems
    checker = run_child(run_dir, "check-secret-report",
                        [sys.executable, "-B", REPO / "tools" / "check_secret_report.py", report], REPO)
    result["children"].append(checker)
    text = Path(checker["stdout_path"]).read_text(errors="replace") if "stdout_path" in checker else ""
    match = re.search(r"potential secrets: (\d+);", text)
    potential = int(match.group(1)) if match else None
    result["secret_scan"]["potential_secrets"] = potential
    if checker.get("exit") != 0:
        problems.append("check_secret_report_failed")
    if potential != 0:
        problems.append("potential_secrets_nonzero_or_unreported")
    return problems


def check_public_tree(run_dir, result):
    record = run_child(run_dir, "check-public-tree",
                       [sys.executable, "-B", REPO / "tools" / "check_public_tree.py"], REPO)
    result["children"].append(record)
    return [] if record.get("exit") == 0 else ["public_tree_check_failed"]


HANDLERS = {"focused": check_focused, "compatibility": check_compatibility, "negative": check_negative,
            "portability": check_portability, "credentials": check_credentials, "public-tree": check_public_tree}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", choices=CHECKS)
    parser.add_argument("--output-root")
    parser.add_argument("--portability-child-report", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.portability_child_report:
        if args.check or args.output_root:
            parser.error("child mode takes no other arguments")
        return portability_child(args.portability_child_report)
    if not args.check or not args.output_root:
        parser.error("--check and --output-root are required")
    run_dir = new_run_dir(args.output_root, args.check)
    result = {"schema": "fc-finding-identity-acceptance-v1", "check": args.check,
              "argv": [sys.executable] + list(sys.argv),
              "cwd": os.getcwd(), "interpreter": sys.executable, "python_version": sys.version,
              "repo_root": str(REPO), "run_dir": str(run_dir), "started_utc": now(),
              "base_selector": BASE_SELECTOR, "git": git_identity(), "source_hashes": source_hashes(REPO),
              "children": [], "acceptance": "REVIEW REQUIRED; machine validity only, not semantic/legal acceptance"}
    try:
        problems = HANDLERS[args.check](run_dir, result)
    except Exception as error:  # Incomplete check: record the class, fail closed.
        problems = ["check_incomplete:" + type(error).__name__]
        result["exception"] = repr(error)[:2000]
    result["finished_utc"] = now()
    result["problems"] = problems
    result["verdict"] = "PASS_REVIEW_REQUIRED" if not problems else "FAIL"
    with open(run_dir / "run.json", "x") as handle:
        json.dump(result, handle, indent=2, sort_keys=True, default=str)
    print(json.dumps({"check": args.check, "verdict": result["verdict"], "problems": problems,
                      "run_dir": str(run_dir), "head": result["git"].get("head"),
                      "clean": result["git"].get("clean"),
                      "run_json_sha256": sha256_file(run_dir / "run.json")}, sort_keys=True))
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
