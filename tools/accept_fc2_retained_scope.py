"""Acceptance runner for FC2-RETAINED-SCOPE (offline, synthetic, review-required).

Usage (one check per invocation):
    <interpreter> tools/accept_fc2_retained_scope.py --check CHECK --output-root DIR

CHECK is one of: focused, compatibility, negative, portability, credentials,
public-tree. Each invocation creates a fresh unique child directory under DIR
(never overwriting or deleting earlier evidence) holding raw child
stdout/stderr, argv, cwd, exit codes, executed test counts/IDs and source
hashes in ``run.json``. The process exits 0 only when every assertion of the
selected check holds; an unknown mode is refused by argparse.

The repository root is derived from this file. Import search paths are set
only inside this script. Children are argument arrays without a shell; the
interpreter is the one running this script. Temporary test data for children
lives under the per-run directory. A passing result is machine validity only:
REVIEW REQUIRED, never semantic/legal acceptance, activation or publication
authority.
"""
import argparse
import ast
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
import traceback
import unittest

sys.dont_write_bytecode = True

REPO = Path(__file__).resolve().parents[1]
BRIEF = "FC2-RETAINED-SCOPE: typed refusal for malformed retained unit scope identifiers (frozen card brief)"
CHECKS = ("focused", "compatibility", "negative", "portability", "credentials", "public-tree")
TARGET_PATH = "campaign_tool/records/retained_units.py"
FOCUSED_PATH = "tests/records/test_fc2_retained_scope.py"
FOCUSED_MODULE = "tests.records.test_fc2_retained_scope"
BASE_SELECTOR = "parent of the first commit adding " + FOCUSED_PATH
COMPAT_TARGETS = (
    "tests.records.test_understanding.RetainedUnitsTests",
    "tests.records.test_understanding.GroundedEvidenceTests",
    "tests.test_secret_report",
    "tests.test_public_tree",
)
HASHED_SOURCES = (
    TARGET_PATH,
    FOCUSED_PATH,
    "tools/accept_fc2_retained_scope.py",
    "tests/records/test_understanding.py",
    "tests/test_secret_report.py",
    "tests/test_public_tree.py",
    "tools/check_secret_report.py",
    "tools/check_public_tree.py",
)
STATIC_SCANNED = (FOCUSED_PATH, "tools/accept_fc2_retained_scope.py")
# Assembled from parts so this list does not match its own source text.
BOX_SPECIFIC = ("/ho" + "me/", "agent" + "-work", "venv" + "-fullsuite", "/Us" + "ers/", ".her" + "mes",
                "/t" + "mp/")
INLINE_FORMS = (
    re.compile(r"""["']-""" + r"""[ce]["']"""),
    re.compile("shell" + r"\s*=\s*True"),
    re.compile("PYTHON" + "PATH"),
    re.compile(r"\bos\.sys" + r"tem\("),
)
# The candidate's member-type gate and the uniqueness (set construction) gate inside _scope.
MEMBER_CHECK = 'if any(type(v)is not str or not HEX.fullmatch(v) for v in ids):_no("import_scope_invalid")'
UNIQUE_CHECK = 'if len(set(ids))!=len(ids):_no("import_scope_invalid")'
NEGATIVE_REGRESSIONS = (
    "ScopeMemberTypeTests.test_dict_member_first_entry_rejected_before_sql",
    "ScopeMemberTypeTests.test_list_member_first_entry_rejected_before_sql",
    "ReadCurrentUnitsTests.test_dict_member_rejected_before_scope_sql_and_payload_read",
    "ReadCurrentUnitsTests.test_list_member_rejected_before_scope_sql_and_payload_read",
)
NEGATIVE_POSITIVES = (
    "ScopeMemberTypeTests.test_valid_scope_preserves_order_and_rows",
    "ScopeMemberTypeTests.test_hashable_non_string_members_rejected",
    "ScopeMemberTypeTests.test_wrong_hash_strings_rejected",
    "ScopeMemberTypeTests.test_existing_codes_preserved",
    "ReadCurrentUnitsTests.test_valid_current_import_accepted",
    "ReadCurrentUnitsTests.test_stale_current_pointer_code_preserved",
)
RAN = re.compile(r"^Ran (\d+) tests? in ", re.M)
SUMMARY = re.compile(r"^(OK|FAILED)(?: \(([^)]*)\))?\s*$", re.M)
VERBOSE = re.compile(r"^(test\w*) \(([\w.]+)\)(?:\n[^\n]*?)? \.\.\. (ok|FAIL|ERROR|skipped.*|expected failure|unexpected success)$",
                     re.M)
# Failure/error block headers, including subTest failures that leave no "... ok" line.
FAILURE_HEADER = re.compile(r"^(ERROR|FAIL): (test\w*) \(([\w.]+)\)", re.M)
UNHASHABLE = re.compile(r"^TypeError: (?:cannot use '[^']+' as a set element \()?unhashable type: '(?:dict|list)'", re.M)


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


def child_env(run_dir, label):
    """Inherited environment with only temp/home data defaults redirected into the run directory."""
    scratch = run_dir / (label + "-tmp")
    scratch.mkdir(mode=0o700)
    env = dict(os.environ)
    env.update({"TMPDIR": str(scratch), "TEMP": str(scratch), "TMP": str(scratch)})
    return env


def run_child(run_dir, label, argv, cwd, env=None, stdout_path=None, timeout=900):
    """Run one argument-array child (no shell) and preserve its raw streams."""
    record = {"label": label, "argv": [str(a) for a in argv], "cwd": str(cwd)}
    if env is not None:
        record["env_overrides"] = {k: env[k] for k in ("HOME", "TMPDIR", "TEMP", "TMP", "XDG_CONFIG_HOME",
                                                       "XDG_DATA_HOME", "XDG_CACHE_HOME") if k in env
                                   and env[k] != os.environ.get(k)}
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
    """Actual TestCase.id() from one verbose line (3.11+ prints the full id)."""
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
    failing = sorted({verbose_test_id(m.group(2), m.group(3)) + " " + m.group(1) for m in FAILURE_HEADER.finditer(stderr_text)})
    return {"tests_run": int(ran[-1]) if ran else 0, "summary": summary[-1][0] if summary else None,
            "summary_detail": detail, "summary_counts": counts, "tests": tests, "failing_test_ids": failing}


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
    actual = {case.id(): "ok" for case in suite}
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
    record = run_child(run_dir, label, argv, root, env=child_env(run_dir, label))
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
    from campaign_tool.records import retained_units
    focused = __import__(FOCUSED_MODULE, fromlist=["*"])
    for module in (retained_units, focused):
        if not Path(module.__file__).resolve().is_relative_to(Path(root).resolve()):
            raise RuntimeError("module_loaded_outside_candidate")
    return retained_units, focused


def _scope_source(source_text):
    """Exact source segment of the module-level ``_scope`` function."""
    tree = ast.parse(source_text)
    found = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_scope"]
    if len(found) != 1:
        raise RuntimeError("scope_function_not_unique")
    return ast.get_source_segment(source_text, found[0])


def _compile_scope(retained_units, source_segment, label):
    """Compile one ``_scope`` variant against a copy of the real module globals (no disk write)."""
    namespace = dict(retained_units.__dict__)
    exec(compile(source_segment, "<{}>".format(label), "exec"), namespace)
    return namespace["_scope"]


def baseline_commit():
    """Pinned base = parent of the commit that first added the focused test module (derived, not a literal)."""
    done = subprocess.run(["git", "-C", str(REPO), "log", "--format=%H", "--diff-filter=A", "HEAD", "--", FOCUSED_PATH],
                          capture_output=True, text=True, timeout=60, check=True)
    added = [line for line in done.stdout.split() if line]
    if not added:
        raise RuntimeError("focused_test_commit_not_found")
    parent = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--verify", added[-1] + "^1"],
                            capture_output=True, text=True, timeout=60, check=True)
    return parent.stdout.strip()


def _probe(scope, focused, retained_units):
    """Dict/list first-entry members against the recording stub: exception class, code and SQL calls."""
    rows = []
    for name, member in (("dict", {"unit_id": focused.hexid("probe:u0")}), ("list", [focused.hexid("probe:u0")])):
        ledger = focused.RecordingLedger()
        declared = focused.declared_with([member], label="probe")
        try:
            scope(ledger, [declared])
            outcome = {"raised": None}
        except retained_units.RetainedUnitError as error:
            outcome = {"raised": "RetainedUnitError", "code": error.code}
        except Exception as error:  # The defect under test: record the raw class and message.
            outcome = {"raised": type(error).__name__, "message": str(error)[:200]}
        outcome.update({"member": name, "sql_calls": len(ledger.calls)})
        rows.append(outcome)
    return rows


def baseline_probe(run_dir, result):
    """Measure the unchanged base _scope (from the git object) next to the candidate, in memory only."""
    problems = []
    retained_units, focused = _load_in_process(REPO)
    base = baseline_commit()
    done = subprocess.run(["git", "-C", str(REPO), "show", base + ":" + TARGET_PATH],
                          capture_output=True, timeout=60, check=True)
    base_source = done.stdout.decode()
    base_scope = _compile_scope(retained_units, _scope_source(base_source), "git {}:{} _scope".format(base, TARGET_PATH))
    data = {"baseline_commit": base, "baseline_selector": BASE_SELECTOR,
            "baseline_target_sha256": sha256_bytes(done.stdout),
            "candidate_target_sha256": sha256_file(REPO / TARGET_PATH),
            "baseline": _probe(base_scope, focused, retained_units),
            "candidate": _probe(retained_units._scope, focused, retained_units)}
    path = run_dir / "baseline-probe.json"
    with open(path, "x") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
    result["baseline_probe"] = dict(data, path=str(path), sha256=sha256_file(path))
    if not all(r["raised"] == "TypeError" and r["sql_calls"] == 0 for r in data["baseline"]):
        problems.append("baseline_defect_not_reproduced")
    if not all(r["raised"] == "RetainedUnitError" and r.get("code") == "import_scope_invalid" and r["sql_calls"] == 0
               for r in data["candidate"]):
        problems.append("candidate_not_typed_refusal_before_sql")
    return problems


# ----------------------------------------------------------------- checks


def check_focused(run_dir, result):
    record, problems = unittest_child(run_dir, "focused-unittest", FOCUSED_MODULE)
    result["children"].append(record)
    try:
        problems = problems + baseline_probe(run_dir, result)
    except (subprocess.SubprocessError, OSError, KeyError, ValueError, RuntimeError, SyntaxError) as error:
        problems = problems + ["baseline_probe_incomplete:" + type(error).__name__]
        result["baseline_probe_error"] = repr(error)[:2000]
    return problems


def check_compatibility(run_dir, result):
    problems = []
    for index, target in enumerate(COMPAT_TARGETS, 1):
        record, found = unittest_child(run_dir, "compatibility-{}".format(index), target)
        result["children"].append(record)
        problems.extend(target + ":" + p for p in found)
    result["compatibility_total_tests"] = sum(c.get("tests_run", 0) for c in result["children"])
    result["full_discovery"] = "operator-run (not executed by this runner)"
    return problems


def _run_cases(focused, names):
    loader = unittest.TestLoader()
    suite = unittest.TestSuite(loader.loadTestsFromName(name, focused) for name in names)
    stream = io.StringIO()
    outcome = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    return outcome, stream.getvalue()


def _parent_id(case):
    """A subTest failure reports a _SubTest; attribute it to its owning test method."""
    return getattr(case, "test_case", case).id()


def _summarise(outcome):
    unhashable = {}
    for case, text in outcome.errors:
        unhashable.setdefault(_parent_id(case), []).append(bool(UNHASHABLE.search(text)))
    return {"tests_run": outcome.testsRun,
            "failures": sorted({_parent_id(case) for case, _ in outcome.failures}),
            "errors": sorted({_parent_id(case) for case, _ in outcome.errors}),
            "error_entries": [case.id() for case, _ in outcome.errors],
            "error_unhashable_typeerror": {k: all(v) for k, v in unhashable.items()},
            "skipped": [case.id() for case, _ in outcome.skipped],
            "unexpected_successes": [case.id() for case in outcome.unexpectedSuccesses],
            "successful": outcome.wasSuccessful()}


def mutant_segment(candidate_segment):
    """Swap only the member-type gate and the set-construction gate (set() first, as on the base)."""
    lines = candidate_segment.split("\n")
    member = [i for i, line in enumerate(lines) if line.strip() == MEMBER_CHECK]
    unique = [i for i, line in enumerate(lines) if line.strip() == UNIQUE_CHECK]
    if len(member) != 1 or len(unique) != 1:
        raise RuntimeError("candidate_gates_not_found_exactly_once")
    if not member[0] < unique[0]:
        raise RuntimeError("candidate_member_gate_not_before_set_gate")
    i, j = member[0], unique[0]
    lines[i], lines[j] = lines[j], lines[i]
    return "\n".join(lines), {"member_line": i, "unique_line": j}


def check_negative(run_dir, result):
    """In-memory mutant: set() uniqueness moves ahead of member type validation; nothing on disk changes."""
    problems = []
    import tempfile
    in_process_tmp = run_dir / "in-process-tmp"
    in_process_tmp.mkdir(mode=0o700)
    tempfile.tempdir = str(in_process_tmp)  # In-process synthetic ledgers stay inside this run directory.
    disk_before = sha256_file(REPO / TARGET_PATH)
    retained_units, focused = _load_in_process(REPO)
    original = retained_units._scope
    candidate_segment = _scope_source((REPO / TARGET_PATH).read_text())
    segment, where = mutant_segment(candidate_segment)
    mutant = _compile_scope(retained_units, segment, "in-memory mutant _scope")
    phases = {"swapped_lines_in_scope": where, "mutant_source_sha256": sha256_bytes(segment.encode()),
              "candidate_scope_sha256": sha256_bytes(candidate_segment.encode())}
    try:
        retained_units._scope = mutant
        regress, regress_text = _run_cases(focused, NEGATIVE_REGRESSIONS)
        positive, positive_text = _run_cases(focused, NEGATIVE_POSITIVES)
        phases["mutant_regressions"] = _summarise(regress)
        phases["mutant_positives"] = _summarise(positive)
        (run_dir / "negative-mutant.txt").write_text(regress_text + "\n" + positive_text)
    finally:
        retained_units._scope = original
    phases["restored_scope"] = retained_units._scope is original
    stream = io.StringIO()
    restored = unittest.TextTestRunner(stream=stream, verbosity=2).run(unittest.TestLoader().loadTestsFromModule(focused))
    restored_text = stream.getvalue()
    phases["restored"] = _summarise(restored)
    (run_dir / "negative-restored.txt").write_text(restored_text)
    for name in ("negative-mutant.txt", "negative-restored.txt"):
        phases[name + "_sha256"] = sha256_file(run_dir / name)
    phases["target_unchanged_on_disk"] = sha256_file(REPO / TARGET_PATH) == disk_before
    result["negative"] = phases
    mutant_result = phases["mutant_regressions"]
    expected = {focused.__name__ + "." + name for name in NEGATIVE_REGRESSIONS}
    if (mutant_result["tests_run"] != len(NEGATIVE_REGRESSIONS) or set(mutant_result["errors"]) != expected
            or mutant_result["failures"]):
        problems.append("mutant_not_detected_as_error_by_every_regression")
    if not all(mutant_result["error_unhashable_typeerror"].get(name) for name in expected):
        problems.append("mutant_detection_not_raw_unhashable_typeerror")
    pos = phases["mutant_positives"]
    if not pos["successful"] or pos["tests_run"] != len(NEGATIVE_POSITIVES) or pos["skipped"]:
        problems.append("positives_not_green_under_mutant")
    if not phases["restored_scope"]:
        problems.append("scope_not_restored")
    rest = phases["restored"]
    if not rest["successful"] or rest["tests_run"] <= len(NEGATIVE_REGRESSIONS) + len(NEGATIVE_POSITIVES) \
            or rest["skipped"]:
        problems.append("restored_module_not_green")
    if not phases["target_unchanged_on_disk"]:
        problems.append("target_changed_on_disk")
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


def _first_prefix_child(run_dir):
    """Focused tests from the original candidate prefix, in the same sentinel child mode."""
    report = run_dir / "portability-original-prefix.json"
    home = run_dir / "original-prefix-home"
    home.mkdir(mode=0o700)
    env = dict(os.environ)
    env.update({"HOME": str(home), "TMPDIR": str(home), "TEMP": str(home), "TMP": str(home),
                "XDG_CONFIG_HOME": str(home), "XDG_DATA_HOME": str(home), "XDG_CACHE_HOME": str(home)})
    argv = [sys.executable, "-I", "-B", str(REPO / "tools" / "accept_fc2_retained_scope.py"),
            "--portability-child-report", str(report)]
    return run_child(run_dir, "portability-original-prefix", argv, REPO, env=env), report, REPO.resolve()


def _judge_child(record, report, expected_root, problems, label):
    if record.get("exit") != 0:
        problems.append(label + ":portability_child_failed")
    if not report.is_file():
        problems.append(label + ":portability_report_missing")
        return None
    child = json.loads(report.read_text())
    record["report_sha256"] = sha256_file(report)
    summary = {k: child.get(k) for k in ("repo_root", "tests_run", "failures", "errors", "skipped",
                                          "sentinel_hits", "modules_outside_root", "loaded_modules", "tempdir")}
    if child.get("repo_root") != str(expected_root):
        problems.append(label + ":child_used_wrong_root")
    if not child.get("tests_run") or child.get("failures") or child.get("errors") or child.get("skipped"):
        problems.append(label + ":child_focused_not_green")
    if child.get("sentinel_hits"):
        problems.append(label + ":network_or_process_sentinel_triggered")
    if child.get("modules_outside_root") or not child.get("loaded_modules"):
        problems.append(label + ":application_modules_loaded_outside_root")
    return summary


def check_portability(run_dir, result):
    problems = []
    findings = static_inspection(REPO)
    result["static_inspection"] = {"files": list(STATIC_SCANNED), "tokens": len(BOX_SPECIFIC), "findings": findings}
    if findings:
        problems.append("static_box_specific_or_inline_findings")
    record, report, root = _first_prefix_child(run_dir)
    result["children"].append(record)
    result["portability_original_prefix"] = _judge_child(record, report, root, problems, "original")
    # Second, different absolute prefix: a preserved copy inside this run directory (never deleted).
    copy_root = run_dir / "relocated" / "candidate-source"
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
    home = run_dir / "relocated-home"
    home.mkdir(mode=0o700)
    env = dict(os.environ)
    env.update({"HOME": str(home), "TMPDIR": str(home), "TEMP": str(home), "TMP": str(home),
                "XDG_CONFIG_HOME": str(home), "XDG_DATA_HOME": str(home), "XDG_CACHE_HOME": str(home)})
    copy_report = run_dir / "portability-relocated.json"
    argv = [sys.executable, "-I", "-B", str(copy_root / "tools" / "accept_fc2_retained_scope.py"),
            "--portability-child-report", str(copy_report)]
    copy_record = run_child(run_dir, "portability-relocated", argv, copy_root, env=env)
    result["children"].append(copy_record)
    result["portability_relocated"] = _judge_child(copy_record, copy_report, copy_root.resolve(), problems,
                                                   "relocated")
    if result["portability_original_prefix"] and result["portability_relocated"]:
        same = (result["portability_original_prefix"]["tests_run"] == result["portability_relocated"]["tests_run"])
        result["portability_same_test_count"] = same
        if not same:
            problems.append("prefix_test_counts_differ")
    return problems


def portability_child(report):
    """Runs under -I: offline/process sentinels installed before any application import."""
    import socket
    import tempfile
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
    suite = unittest.TestLoader().loadTestsFromName(FOCUSED_MODULE)
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
    data = {"repo_root": str(root), "tests_run": outcome.testsRun, "tempdir": tempfile.gettempdir(),
            "failures": [c.id() for c, _ in outcome.failures], "errors": [c.id() for c, _ in outcome.errors],
            "skipped": [c.id() for c, _ in outcome.skipped], "sentinel_hits": hits,
            "modules_outside_root": outside, "loaded_modules": loaded, "unittest_output": stream.getvalue()}
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
                                 "raw_hits": sum(len(v) for v in data["results"].values()),
                                 "hits_in_card_files": sorted(f for f in data["results"] if f in STATIC_SCANNED
                                                              or f == TARGET_PATH)}
    except (OSError, ValueError, AttributeError):
        problems.append("secret_report_malformed")
        return problems
    if result["secret_scan"]["hits_in_card_files"]:
        problems.append("raw_scanner_hits_in_card_files")
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
    result = {"schema": "fc2-retained-scope-acceptance-v1", "brief": BRIEF, "check": args.check,
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
        result["exception_traceback"] = traceback.format_exc()[-4000:]
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
