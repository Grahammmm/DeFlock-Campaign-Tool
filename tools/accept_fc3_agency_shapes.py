"""Acceptance runner for FC3-AGENCY-SHAPES (offline, synthetic, review-required).

Usage (one check per invocation):
    <interpreter> tools/accept_fc3_agency_shapes.py --check CHECK --output-root DIR

CHECK is one of: focused, compatibility, negative, portability, credentials,
public-tree. Each invocation creates a new exclusive child directory under DIR
(earlier evidence is never overwritten or removed). That directory holds raw
child stdout/stderr, argv, cwd, exit codes, executed test counts and IDs,
case coverage and source hashes in ``run.json``. The process exits 0 only
when every assertion of the selected check holds.

The repository root comes from this file's location. Import search paths are
set only inside this script. Children are argument arrays without a shell,
limited to the enumerated local test, git and scanner programs. A passing
result is machine validity only: REVIEW REQUIRED. It is never semantic, legal,
stage or release acceptance, and it grants no publication authority.
"""
import argparse
import copy
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
import types
import unittest

sys.dont_write_bytecode = True

REPO = Path(__file__).resolve().parents[1]
CARD = "FC3-AGENCY-SHAPES"
CHECKS = ("focused", "compatibility", "negative", "portability", "credentials", "public-tree")
PRODUCTION = "campaign_tool/records/agency_reconciliation.py"
FOCUSED_PATH = "tests/records/test_fc3_agency_shapes.py"
RUNNER_PATH = "tools/accept_fc3_agency_shapes.py"
ALLOWED_CHANGES = (PRODUCTION, FOCUSED_PATH, RUNNER_PATH)
FOCUSED_MODULE = "tests.records.test_fc3_agency_shapes"
COMPAT_SELECTORS = (
    "tests.records.test_agency_reconciliation",
    "tests.records.test_agency_reconciliation_regressions",
    "tests.records.test_agency_reconciliation_io",
    "tests.test_secret_report",
    "tests.test_public_tree",
)
BASE_SELECTOR = "parent of the first commit adding " + FOCUSED_PATH
HASHED_SOURCES = ALLOWED_CHANGES + (
    "tests/records/test_agency_reconciliation.py",
    "tests/records/test_agency_reconciliation_regressions.py",
    "tests/records/test_agency_reconciliation_io.py",
    "tools/check_secret_report.py",
    "tools/check_public_tree.py",
)
# Frozen named case -> TestCase class in the focused module.
CASES = {
    "both_target_roles": "BothTargetRolesTests",
    "existing_nontarget_roles": "ExistingNontargetRolesTests",
    "malformed_status_types": "MalformedStatusTypesTests",
    "malformed_role_types": "MalformedRoleTypesTests",
    "malformed_excluded_card": "MalformedExcludedCardTests",
    "unknown_status_string": "UnknownStatusStringTests",
    "empty_inputs_and_conflicting_candidates": "EmptyInputsAndConflictingCandidatesTests",
    "inputs_unchanged_and_no_approval": "InputsUnchangedAndNoApprovalTests",
}
# Focused tests that must fail against the pinned base source (raw TypeError or
# silent skip) - the preserved regression. Every other focused test must pass there.
BASELINE_RED = (
    "MalformedStatusTypesTests.test_malformed_status_types_raise_fixed_value_error",
    "MalformedStatusTypesTests.test_malformed_status_after_valid_cards_and_with_matching_records",
    "MalformedRoleTypesTests.test_malformed_role_types_raise_fixed_value_error",
    "MalformedRoleTypesTests.test_malformed_role_after_valid_cards_and_with_matching_records",
    "MalformedExcludedCardTests.test_malformed_role_on_scope_excluded_card_with_digest_rows",
    "MalformedExcludedCardTests.test_malformed_role_on_hint_present_parent_only_card",
    "MalformedExcludedCardTests.test_malformed_status_on_card_without_any_reference",
    "InputsUnchangedAndNoApprovalTests.test_rejected_inputs_unchanged",
)
# Negative control: each new guard is bypassed independently, in memory only.
# A bypassed status guard falls back to the pinned membership-only test; a
# bypassed role guard performs no role check (pinned behaviour).
BYPASS_DETECTORS = {
    "status_type_guard": (
        "MalformedStatusTypesTests.test_malformed_status_types_raise_fixed_value_error",
        "MalformedStatusTypesTests.test_malformed_status_after_valid_cards_and_with_matching_records",
        "MalformedExcludedCardTests.test_malformed_status_on_card_without_any_reference",
    ),
    "role_guard": (
        "MalformedRoleTypesTests.test_malformed_role_types_raise_fixed_value_error",
        "MalformedRoleTypesTests.test_malformed_role_after_valid_cards_and_with_matching_records",
        "MalformedExcludedCardTests.test_malformed_role_on_scope_excluded_card_with_digest_rows",
        "MalformedExcludedCardTests.test_malformed_role_on_hint_present_parent_only_card",
        "InputsUnchangedAndNoApprovalTests.test_rejected_inputs_unchanged",
    ),
}
STATIC_SCANNED = (FOCUSED_PATH, RUNNER_PATH)
# Assembled from parts so this list does not match its own source text.
BOX_SPECIFIC = ("/ho" + "me/", "agent" + "-work", "venv" + "-fullsuite", "/Us" + "ers/", ".her" + "mes",
                "kan" + "ban/", "/t" + "mp/")
INLINE_FORMS = (
    re.compile(r"""["']-""" + r"""[ce]["']"""),
    re.compile("shell" + r"\s*=\s*True"),
    re.compile("PYTHON" + "PATH"),
    re.compile(r"\bos\.sys" + r"tem\("),
    re.compile(r"\bos\.po" + r"pen\("),
)
RAN = re.compile(r"^Ran (\d+) tests? in ", re.M)
SUMMARY = re.compile(r"^(OK|FAILED)(?: \(([^)]*)\))?\s*$", re.M)
VERBOSE = re.compile(r"^(test\w*) \(([\w.]+)\)(?:\n[^\n]*?)? \.\.\. (ok|FAIL|ERROR|skipped.*|expected failure|"
                     r"unexpected success)$", re.M)


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    return sha256_bytes(Path(path).read_bytes())


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def new_run_dir(output_root, check):
    root = Path(output_root).resolve()
    if root == REPO or REPO in root.parents:
        raise SystemExit("output root must be outside the candidate repository")
    root.mkdir(parents=True, exist_ok=True)
    run = root / "{}-{}-{}-{}".format(check, now(), os.getpid(), secrets.token_hex(4))
    run.mkdir(exist_ok=False)
    return run


def source_hashes(root):
    return {rel: (sha256_file(root / rel) if (root / rel).is_file() else None) for rel in HASHED_SOURCES}


def allowed_program(program):
    """Only the enumerated local children: this interpreter, its scanner, git."""
    program = str(program)
    return program in (sys.executable, str(Path(sys.executable).parent / "detect-secrets"), "git")


def run_child(run_dir, label, argv, cwd, env=None, stdout_path=None, timeout=900):
    """Run one argument-array child (no shell) and keep its raw streams."""
    record = {"label": label, "argv": [str(a) for a in argv], "cwd": str(cwd)}
    if not allowed_program(argv[0]):
        record.update(exit=None, launch_error="unexpected_subprocess_refused")
        return record
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


def git(*args, binary=False):
    argv = ["git", "-C", str(REPO)] + list(args)
    if not allowed_program(argv[0]):
        raise RuntimeError("unexpected_subprocess_refused")
    done = subprocess.run(argv, capture_output=True, timeout=120, check=True)
    return done.stdout if binary else done.stdout.decode().strip()


def git_identity():
    identity = {}
    for name, args in (("head", ("rev-parse", "HEAD")),
                       ("status_porcelain", ("status", "--porcelain", "--untracked-files=all"))):
        try:
            identity[name] = git(*args)
        except (OSError, subprocess.SubprocessError, RuntimeError) as error:
            identity[name] = None
            identity[name + "_error"] = type(error).__name__
    identity["clean"] = identity.get("status_porcelain") == ""
    return identity


def baseline_commit():
    """Pinned base = parent of the commit that first added the focused test.

    Derived from history so no high-entropy literal sits in source; run.json
    records the resolved commit for comparison with the frozen brief pin.
    """
    added = [line for line in git("log", "--format=%H", "--diff-filter=A", "HEAD", "--", FOCUSED_PATH).split() if line]
    if not added:
        raise RuntimeError("focused_test_commit_not_found")
    return git("rev-parse", "--verify", added[-1] + "^1")


def change_scope(result):
    """Every file changed since the base (committed or not) must be an allowed path."""
    base = baseline_commit()
    committed = [n for n in git("diff", "--name-only", base, "HEAD").splitlines() if n]
    pending = [line[3:] for line in git("status", "--porcelain", "--untracked-files=all").splitlines() if line]
    changed = sorted(set(committed + pending))
    outside = [name for name in changed if name not in ALLOWED_CHANGES]
    result["change_scope"] = {"base_commit": base, "base_selector": BASE_SELECTOR, "changed_files": changed,
                              "outside_allowed": outside,
                              "base_production_sha256": sha256_bytes(git("show", base + ":" + PRODUCTION, binary=True))}
    return ["files_changed_outside_allowed_scope"] if outside else []


# ----------------------------------------------------------------- unittest parsing


def verbose_test_id(name, parenthesized):
    """Python 3.11+ prints ``name (module.Class.name)``; older ``name (module.Class)``."""
    return parenthesized if parenthesized.endswith("." + name) else parenthesized + "." + name


def parse_unittest(stderr_text):
    ran = RAN.findall(stderr_text)
    summary = SUMMARY.findall(stderr_text)
    tests = [{"id": verbose_test_id(m.group(1), m.group(2)),
              "outcome": "skipped" if m.group(3).startswith("skipped") else m.group(3)}
             for m in VERBOSE.finditer(stderr_text)]
    detail = summary[-1][1] if summary else ""
    counts = dict((k.strip(), int(v)) for k, v in (p.split("=") for p in detail.split(", ") if "=" in p))
    return {"tests_run": int(ran[-1]) if ran else 0, "summary": summary[-1][0] if summary else None,
            "summary_counts": counts, "tests": tests}


def ensure_path(root):
    text = str(root)
    if text not in sys.path:
        sys.path.insert(0, text)


def loaded_suite_ids(targets, root):
    ensure_path(root)
    ids = []

    def walk(item):
        if isinstance(item, unittest.TestSuite):
            for child in item:
                walk(child)
        else:
            ids.append(item.id())
    walk(unittest.TestLoader().loadTestsFromNames(list(targets)))
    return ids


def unittest_child(run_dir, label, targets, root=None):
    root = root or REPO
    argv = [sys.executable, "-B", "-m", "unittest", "-v"] + list(targets)
    record = run_child(run_dir, label, argv, root)
    text = Path(record["stderr_path"]).read_text(errors="replace") if "stderr_path" in record else ""
    parsed = parse_unittest(text)
    record.update(parsed)
    problems = []
    try:
        expected = loaded_suite_ids(targets, root)
    except Exception as error:  # Unloadable suite: fail closed.
        expected = None
        record["loaded_suite_error"] = repr(error)[:2000]
    parsed_ids = [t["id"] for t in parsed["tests"]]
    record["loaded_suite_count"] = None if expected is None else len(expected)
    record["parsed_ids_match_loaded_suite"] = (expected is not None and len(parsed_ids) == len(set(parsed_ids))
                                               and sorted(parsed_ids) == sorted(expected))
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
    if any(k in parsed["summary_counts"] for k in ("skipped", "expected failures", "unexpected successes")):
        problems.append("unexpected_skips_or_expected_failures")
    record["non_ok_test_ids"] = [t["id"] for t in parsed["tests"] if t["outcome"] != "ok"]
    if record["non_ok_test_ids"]:
        problems.append("non_ok_tests")
    record["problems"] = problems
    return record, problems


# ----------------------------------------------------------------- in-process helpers


def load_in_process(root):
    ensure_path(root)
    from campaign_tool.records import agency_reconciliation as production
    focused = __import__(FOCUSED_MODULE, fromlist=["*"])
    for module in (production, focused):
        if not Path(module.__file__).resolve().is_relative_to(Path(root).resolve()):
            raise RuntimeError("module_loaded_outside_candidate")
    return production, focused


def run_names(module, names):
    loader = unittest.TestLoader()
    suite = unittest.TestSuite(loader.loadTestsFromName(name, module) for name in names)
    stream = io.StringIO()
    outcome = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    return outcome, stream.getvalue()


def summarise(outcome, prefix=""):
    strip = (lambda ident: ident[len(prefix):] if prefix and ident.startswith(prefix) else ident)
    return {"tests_run": outcome.testsRun,
            "failures": sorted(strip(c.id()) for c, _ in outcome.failures),
            "errors": sorted(strip(c.id()) for c, _ in outcome.errors),
            "skipped": sorted(strip(c.id()) for c, _ in outcome.skipped),
            "unexpected_successes": sorted(strip(c.id()) for c in outcome.unexpectedSuccesses),
            "successful": outcome.wasSuccessful()}


def focused_method_names(focused):
    loader = unittest.TestLoader()
    return sorted(cls + "." + name for cls in CASES.values()
                  for name in loader.getTestCaseNames(getattr(focused, cls)))


def classify(focused, reconcile_function, cards):
    """Observed outcome class of one direct call (for the evidence record)."""
    try:
        result = reconcile_function(cards, [])
    except Exception as error:
        return type(error).__name__ + ":" + str(error)[:80]
    return "returned:targets={}".format(len(result["cards"]))


def variant_observations(focused, reconcile_function):
    rows = []
    for name, value in focused.NON_STRING_STATUSES:
        rows.append({"field": "agency_status", "variant": name,
                     "outcome": classify(focused, reconcile_function,
                                         [focused.with_field(focused.card("probe"), "agency_status", value)])})
    for name, value in focused.NON_STRING_ROLES + focused.BLANK_ROLES:
        rows.append({"field": "role", "variant": name,
                     "outcome": classify(focused, reconcile_function,
                                         [focused.with_field(focused.card("probe"), "role", value)])})
    return rows


def baseline_module():
    """The pinned-base production module, built in memory from the git object."""
    base = baseline_commit()
    source = git("show", base + ":" + PRODUCTION, binary=True)
    module = types.ModuleType("campaign_tool.records._fc3_baseline_agency_reconciliation")
    module.__file__ = "<git {}:{}>".format(base, PRODUCTION)
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    return module, base, sha256_bytes(source)


# ----------------------------------------------------------------- checks


def check_focused(run_dir, result):
    record, problems = unittest_child(run_dir, "focused-unittest", [FOCUSED_MODULE])
    result["children"].append(record)
    outcomes = {t["id"]: t["outcome"] for t in record["tests"]}
    coverage = {}
    for case, cls in CASES.items():
        ids = sorted(i for i in outcomes if i.startswith(FOCUSED_MODULE + "." + cls + "."))
        coverage[case] = {"class": cls, "tests": ids, "all_ok": bool(ids) and all(outcomes[i] == "ok" for i in ids)}
        if not coverage[case]["all_ok"]:
            problems.append("case_not_covered_or_not_green:" + case)
    result["case_coverage"] = coverage
    try:
        problems += baseline_differential(run_dir, result)
    except Exception as error:  # Incomplete differential fails closed.
        problems.append("baseline_differential_incomplete:" + type(error).__name__)
        result["baseline_differential_error"] = repr(error)[:2000]
    return problems


def baseline_differential(run_dir, result):
    """Re-run the focused cases against the pinned base reconcile, in memory."""
    _production, focused = load_in_process(REPO)
    base, base_commit, base_sha = baseline_module()
    candidate = focused.reconcile
    names = focused_method_names(focused)
    try:
        focused.reconcile = base.reconcile
        outcome, text = run_names(focused, names)
        observed = variant_observations(focused, base.reconcile)
    finally:
        focused.reconcile = candidate
    (run_dir / "baseline-differential.txt").write_text(text)
    summary = summarise(outcome, FOCUSED_MODULE + ".")
    red = sorted(summary["failures"] + summary["errors"])
    diff = {"base_commit": base_commit, "base_production_sha256": base_sha, "tests_run": outcome.testsRun,
            "base_red": red, "expected_base_red": sorted(BASELINE_RED), "base_variant_outcomes": observed,
            "candidate_variant_outcomes": variant_observations(focused, candidate),
            "output_sha256": sha256_file(run_dir / "baseline-differential.txt"),
            "restored": focused.reconcile is candidate}
    result["baseline_differential"] = diff
    problems = []
    if outcome.testsRun != len(names) or summary["skipped"]:
        problems.append("baseline_differential_incomplete_run")
    if red != sorted(BASELINE_RED):
        problems.append("baseline_red_set_differs_from_frozen_regression")
    if not diff["restored"]:
        problems.append("focused_reconcile_not_restored")
    status_msg, role_msg = focused.STATUS_MESSAGE, focused.ROLE_MESSAGE
    for row in diff["candidate_variant_outcomes"]:
        expected = "ValueError:" + (status_msg if row["field"] == "agency_status" else role_msg)
        if row["outcome"] != expected:
            problems.append("candidate_variant_not_rejected:{}:{}".format(row["field"], row["variant"]))
    raw = [row["outcome"] for row in observed]
    if not any(o.startswith("TypeError") for o in raw) or not any(o.startswith("returned") for o in raw):
        problems.append("baseline_defect_not_reproduced")
    return problems


def check_compatibility(run_dir, result):
    record, problems = unittest_child(run_dir, "compatibility-unittest", list(COMPAT_SELECTORS))
    per_module = {}
    for selector in COMPAT_SELECTORS:
        ids = [t for t in record["tests"] if t["id"].startswith(selector + ".")]
        per_module[selector] = {"tests": len(ids), "non_ok": [t["id"] for t in ids if t["outcome"] != "ok"]}
        if not ids:
            problems.append("selector_executed_no_tests:" + selector)
    record["per_selector"] = per_module
    result["children"].append(record)
    result["full_discovery"] = "operator-run (not part of this runner)"
    return problems


def check_negative(run_dir, result):
    problems = []
    before_disk = source_hashes(REPO)
    production, focused = load_in_process(REPO)
    names = focused_method_names(focused)
    guards = {"status_type_guard": "_require_agency_status", "role_guard": "_require_role"}
    originals = {}
    for attribute in guards.values():
        if not callable(getattr(production, attribute, None)):
            return ["guard_helper_missing:" + attribute]
        originals[attribute] = getattr(production, attribute)

    def pinned_status_membership(card):
        # Pinned base behaviour: membership only, no string-type check.
        if card.get("agency_status") not in production.AGENCY_STATUSES:
            raise ValueError("card requires explicit agency_status")
        return card["agency_status"]

    def no_role_check(card):
        return card.get("role")

    replacements = {"status_type_guard": pinned_status_membership, "role_guard": no_role_check}
    phases = {}
    for bypass, attribute in guards.items():
        phase = {"bypassed_attribute": "{}.{}".format(production.__name__, attribute),
                 "other_guard_intact": [a for a in guards.values() if a != attribute]}
        try:
            setattr(production, attribute, replacements[bypass])
            phase["other_guard_identity_ok"] = all(getattr(production, a) is originals[a]
                                                   for a in phase["other_guard_intact"])
            outcome, text = run_names(focused, names)
            phase["variant_outcomes"] = variant_observations(focused, production.reconcile)
        finally:
            setattr(production, attribute, originals[attribute])
        name = "negative-{}.txt".format(bypass)
        (run_dir / name).write_text(text)
        summary = summarise(outcome, FOCUSED_MODULE + ".")
        detected = sorted(summary["failures"] + summary["errors"])
        expected = sorted(BYPASS_DETECTORS[bypass])
        phase.update(summary=summary, detected=detected, expected_detectors=expected,
                     positives_green=sorted(set(names) - set(detected)), output_sha256=sha256_file(run_dir / name),
                     restored=getattr(production, attribute) is originals[attribute])
        if outcome.testsRun != len(names) or summary["skipped"]:
            problems.append("mutant_run_incomplete:" + bypass)
        if detected != expected:
            problems.append("mutant_detection_differs:" + bypass)
        if not phase["other_guard_identity_ok"]:
            problems.append("other_guard_not_intact:" + bypass)
        if not phase["restored"]:
            problems.append("guard_not_restored:" + bypass)
        raw = [row["outcome"] for row in phase["variant_outcomes"]]
        if not any(o.startswith("TypeError") or o.startswith("returned") for o in raw):
            problems.append("mutant_raw_behaviour_not_observed:" + bypass)
        phases[bypass] = phase
    restored, restored_text = run_names(focused, names)
    (run_dir / "negative-restored-focused.txt").write_text(restored_text)
    phases["restored_focused"] = summarise(restored, FOCUSED_MODULE + ".")
    controls = __import__("tests.records.test_agency_reconciliation", fromlist=["*"])
    regressions = __import__("tests.records.test_agency_reconciliation_regressions", fromlist=["*"])
    control_runs = {}
    for module in (controls, regressions):
        suite = unittest.TestLoader().loadTestsFromModule(module)
        stream = io.StringIO()
        outcome = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
        name = "negative-restored-{}.txt".format(module.__name__.rsplit(".", 1)[-1])
        (run_dir / name).write_text(stream.getvalue())
        control_runs[module.__name__] = summarise(outcome)
        if not outcome.wasSuccessful() or outcome.testsRun <= 0 or outcome.skipped:
            problems.append("restored_control_not_green:" + module.__name__)
    phases["restored_controls"] = control_runs
    if not phases["restored_focused"]["successful"] or phases["restored_focused"]["tests_run"] != len(names) \
            or phases["restored_focused"]["skipped"]:
        problems.append("restored_focused_not_green")
    phases["source_unchanged_on_disk"] = source_hashes(REPO) == before_disk
    if not phases["source_unchanged_on_disk"]:
        problems.append("source_changed_on_disk")
    result["negative"] = phases
    return problems


def static_inspection(root):
    findings = []
    for relative in STATIC_SCANNED:
        for number, line in enumerate((root / relative).read_text().splitlines(), 1):
            if any(token in line for token in BOX_SPECIFIC):
                findings.append({"file": relative, "line": number, "kind": "box_specific_path"})
            if any(pattern.search(line) for pattern in INLINE_FORMS):
                findings.append({"file": relative, "line": number, "kind": "inline_or_shell_form"})
    return findings


def candidate_files():
    names = git("ls-files", "-z", "--cached", "--others", "--exclude-standard", binary=True).decode().split("\0")
    return sorted({name for name in names if name})


def check_portability(run_dir, result):
    problems = []
    findings = static_inspection(REPO)
    result["static_inspection"] = {"files": list(STATIC_SCANNED), "findings": findings}
    if findings:
        problems.append("static_box_specific_or_inline_findings")
    # Retained second copy at a different absolute prefix (never removed).
    copy_root = run_dir / "relocated-prefix" / "second-copy"
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
    home = run_dir / "isolated-home"
    home.mkdir()
    env = dict(os.environ)
    env.update({"HOME": str(home), "TMPDIR": str(home), "XDG_CONFIG_HOME": str(home),
                "XDG_DATA_HOME": str(home), "XDG_CACHE_HOME": str(home)})
    report = run_dir / "portability-child.json"
    argv = [sys.executable, "-I", "-B", copy_root / "tools" / "accept_fc3_agency_shapes.py",
            "--portability-child-report", report]
    record = run_child(run_dir, "portability-child", argv, copy_root, env=env)
    result["children"].append(record)
    if record.get("exit") != 0:
        problems.append("portability_child_failed")
    if not report.is_file():
        return problems + ["portability_report_missing"]
    child = json.loads(report.read_text())
    record["report_sha256"] = sha256_file(report)
    result["portability_child"] = {k: child.get(k) for k in
                                   ("repo_root", "tests_run", "failures", "errors", "skipped", "case_counts",
                                    "sentinel_hits", "modules_outside_copy", "loaded_modules", "redirected")}
    if child.get("repo_root") != str(copy_root.resolve()):
        problems.append("child_did_not_use_copy")
    if not child.get("tests_run") or child.get("failures") or child.get("errors") or child.get("skipped"):
        problems.append("child_focused_not_green")
    if any(not count for count in (child.get("case_counts") or {"none": 0}).values()):
        problems.append("child_case_coverage_incomplete")
    if child.get("sentinel_hits"):
        problems.append("network_model_or_process_sentinel_triggered")
    if child.get("modules_outside_copy") or not child.get("loaded_modules"):
        problems.append("application_modules_loaded_outside_copy")
    return problems


def portability_child(report):
    """Runs inside the relocated copy under -I; sentinels go in before app import."""
    import socket
    import ssl  # noqa: F401  Standard-library SSL classes may load before the sentinels.
    hits = []

    def refuse(name):
        def blocked(*_args, **_kwargs):
            hits.append(name)
            raise PermissionError("offline sentinel: " + name)
        return blocked

    socket.socket.connect = refuse("socket.connect")
    socket.socket.connect_ex = refuse("socket.connect_ex")
    socket.socket.bind = refuse("socket.bind")
    socket.create_connection = refuse("socket.create_connection")
    socket.getaddrinfo = refuse("socket.getaddrinfo")
    subprocess.Popen.__init__ = refuse("subprocess.Popen")
    os.system = refuse("os.system")
    for name in ("execv", "execve", "execvp", "execvpe", "fork", "forkpty", "posix_spawn", "posix_spawnp"):
        if hasattr(os, name):
            setattr(os, name, refuse("os." + name))
    # The reconciliation ingress has no import-time paths, hosts or model
    # endpoints; HOME/TMPDIR/XDG were redirected by the parent before launch.
    redirected = {k: os.environ.get(k) for k in ("HOME", "TMPDIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME")}
    root = REPO.resolve()
    sys.path.insert(0, str(root))
    suite = unittest.TestLoader().loadTestsFromName(FOCUSED_MODULE)
    stream = io.StringIO()
    outcome = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    text = stream.getvalue()
    case_counts = {case: len(re.findall(r"\(" + re.escape(FOCUSED_MODULE + "." + cls + "."), text))
                   for case, cls in CASES.items()}
    loaded, outside = {}, []
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
            "skipped": [c.id() for c, _ in outcome.skipped], "case_counts": case_counts, "sentinel_hits": hits,
            "modules_outside_copy": outside, "loaded_modules": loaded, "redirected": redirected,
            "unittest_output": text}
    with open(report, "x") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
    ok = outcome.wasSuccessful() and outcome.testsRun > 0 and not outcome.skipped and not hits and not outside
    return 0 if ok else 1


def check_credentials(run_dir, result):
    problems = []
    scanner = Path(sys.executable).parent / "detect-secrets"
    result["scanner"] = {"path": str(scanner), "present": scanner.is_file()}
    if not scanner.is_file():
        return ["detect_secrets_unavailable_adjacent_to_interpreter"]
    report = run_dir / "secret-scan.json"
    scan = run_child(run_dir, "detect-secrets-scan",
                     [scanner, "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"],
                     REPO, stdout_path=report)
    result["children"].append(scan)
    result["children"].append(run_child(run_dir, "detect-secrets-version", [scanner, "--version"], REPO))
    if scan.get("exit") != 0:
        problems.append("secret_scan_failed")
    try:
        data = json.loads(report.read_text())
        if not isinstance(data.get("results"), dict):
            raise ValueError("results")
        result["secret_scan"] = {"files_with_hits": len(data["results"]),
                                 "raw_hits": sum(len(v) for v in data["results"].values()),
                                 "hits_in_changed_files": sorted(p for p in data["results"] if p in ALLOWED_CHANGES)}
    except (OSError, ValueError, AttributeError):
        return problems + ["secret_report_malformed"]
    checker = run_child(run_dir, "check-secret-report",
                        [sys.executable, "-B", REPO / "tools" / "check_secret_report.py", report], REPO)
    result["children"].append(checker)
    text = Path(checker["stdout_path"]).read_text(errors="replace") if "stdout_path" in checker else ""
    match = re.search(r"potential secrets: (\d+);", text)
    result["secret_scan"]["potential_secrets"] = int(match.group(1)) if match else None
    if checker.get("exit") != 0:
        problems.append("check_secret_report_failed")
    if result["secret_scan"]["potential_secrets"] != 0:
        problems.append("potential_secrets_nonzero_or_unreported")
    return problems


def check_public_tree(run_dir, result):
    record = run_child(run_dir, "check-public-tree", [sys.executable, "-B", REPO / "tools" / "check_public_tree.py"], REPO)
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
    result = {"schema": "fc3-agency-shapes-acceptance-v1", "card": CARD, "check": args.check,
              "argv": [sys.executable] + list(sys.argv), "cwd": os.getcwd(), "interpreter": sys.executable,
              "python_version": sys.version, "repo_root": str(REPO), "run_dir": str(run_dir),
              "started_utc": now(), "git": git_identity(), "source_hashes": source_hashes(REPO),
              "children": [], "acceptance": "REVIEW REQUIRED; machine validity only, not semantic/legal acceptance"}
    try:
        problems = change_scope(result)
        problems += HANDLERS[args.check](run_dir, result)
    except Exception as error:  # Incomplete check: record the class, fail closed.
        problems = [("check_incomplete:" + type(error).__name__)]
        result["exception"] = repr(error)[:2000]
    result["finished_utc"] = now()
    result["problems"] = problems
    result["verdict"] = "PASS_REVIEW_REQUIRED" if not problems else "FAIL"
    with open(run_dir / "run.json", "x") as handle:
        json.dump(result, handle, indent=2, sort_keys=True, default=str)
    print(json.dumps({"check": args.check, "verdict": result["verdict"], "problems": problems,
                      "run_dir": str(run_dir), "head": result["git"].get("head"), "clean": result["git"].get("clean"),
                      "run_json_sha256": sha256_file(run_dir / "run.json")}, sort_keys=True))
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
