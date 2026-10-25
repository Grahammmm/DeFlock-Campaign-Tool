"""Acceptance runner for FC4-ROW-PAGE-STATE (offline, synthetic, review-required).

Usage (one check per invocation):
    <interpreter> tools/accept_fc4_row_page_state.py --check CHECK --output-root DIR

CHECK is one of: focused, compatibility, negative, portability, credentials,
public-tree. Each invocation creates a fresh unique child directory under DIR
(never overwriting earlier evidence) holding raw child stdout/stderr, argv,
cwd, exit codes, executed test counts/IDs and source hashes in ``run.json``.
The process exits 0 only when every assertion of the selected check holds.

The repository root is derived from this file. Import search paths are set
only inside this script. Children are literal argument arrays without a shell;
the interpreter is the one running this script. A passing result is machine
validity only: REVIEW REQUIRED, never semantic/legal/stage acceptance,
activation or publication authority.
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
import tarfile
import tempfile
import types
import unittest

sys.dont_write_bytecode = True

REPO = Path(__file__).resolve().parents[1]
CHECKS = ("focused", "compatibility", "negative", "portability", "credentials", "public-tree")
CARD = "FC4-ROW-PAGE-STATE"
BASE_SELECTOR = "parent of the first commit adding tests/records/test_fc4_row_page_state.py"
ROWS_PATH = "campaign_tool/records/understanding/rows.py"
FOCUSED_PATH = "tests/records/test_fc4_row_page_state.py"
FOCUSED_MODULE = "tests.records.test_fc4_row_page_state"
COMPAT_SELECTORS = ("tests.test_understanding_rows", "tests.records.test_understanding",
                    "tests.test_secret_report", "tests.test_public_tree")
# Frozen named-case coverage from the brief; each maps to exactly one test method.
FROZEN_CASES = ("ok_page_and_empty_page_inventory", "partial_blocked_unknown_page_status",
                "missing_and_container_page_status", "visual_review_true_and_malformed",
                "malformed_page_entry", "multiple_pages_one_not_clear", "top_level_hold_stays_hold",
                "rows_retained_for_triage", "aggregate_reason_no_payload", "publication_stays_false",
                "inputs_unchanged")
CASE_CLASS = "PageStateTests"
# Cases that already hold on the unchanged base (positives / preservation controls).
BASE_GREEN = {"ok_page_and_empty_page_inventory", "publication_stays_false", "inputs_unchanged"}
HASHED_SOURCES = (
    ROWS_PATH,
    FOCUSED_PATH,
    "tools/accept_fc4_row_page_state.py",
    "tests/test_understanding_rows.py",
    "campaign_tool/records/extraction_routes.py",
    "campaign_tool/records/native_extract.py",
    "tools/check_secret_report.py",
    "tools/check_public_tree.py",
)
STATIC_SCANNED = (FOCUSED_PATH, "tools/accept_fc4_row_page_state.py")
# Assembled from parts so this list does not match its own source text.
BOX_SPECIFIC = ("/ho" + "me/", "agent" + "-work", "venv" + "-fullsuite", "/Us" + "ers/", ".her" + "mes",
                "/t" + "mp/", "scr" + "atch")
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
    if root == REPO or REPO in root.parents:
        raise SystemExit("output root must be outside the repository")
    root.mkdir(parents=True, exist_ok=True)
    run = root / "{}-{}-{}-{}".format(check, now(), os.getpid(), secrets.token_hex(4))
    run.mkdir(exist_ok=False)
    return run


def source_hashes(root):
    return {rel: (sha256_file(root / rel) if (root / rel).is_file() else None) for rel in HASHED_SOURCES}


def git(*args, binary=False):
    done = subprocess.run(["git", "-C", str(REPO)] + list(args), capture_output=True,
                          text=not binary, timeout=120, check=True)
    return done.stdout


def git_identity():
    identity = {}
    for name, args in (("head", ("rev-parse", "HEAD")),
                       ("status_porcelain", ("status", "--porcelain", "--untracked-files=all"))):
        try:
            identity[name] = git(*args).strip()
        except (OSError, subprocess.SubprocessError) as error:
            identity[name] = None
            identity[name + "_error"] = type(error).__name__
    identity["clean"] = identity.get("status_porcelain") == ""
    try:
        identity["base"] = baseline_commit()
        identity["changed_files_vs_base"] = sorted(git("diff", "--name-only", identity["base"], "HEAD").split())
    except (OSError, subprocess.SubprocessError, RuntimeError) as error:
        identity["base"] = None
        identity["base_error"] = type(error).__name__
    return identity


def baseline_commit():
    """Pinned base = parent of the commit that first added the focused test module.

    Derived from history (no high-entropy literal in source); run.json records
    the resolved commit so a reviewer can compare it with the frozen brief pin.
    """
    added = [line for line in git("log", "--format=%H", "--diff-filter=A", "HEAD", "--", FOCUSED_PATH).split() if line]
    if not added:
        raise RuntimeError("focused_test_commit_not_found")
    return git("rev-parse", "--verify", added[-1] + "^1").strip()


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
    # Python 3.11+ prints ``name (module.Class.name)``; older runtimes ``name (module.Class)``.
    return parenthesized if parenthesized.endswith("." + name) else parenthesized + "." + name


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


def loaded_suite_ids(targets, root):
    """IDs of the suite unittest would execute for ``targets`` (loaded, not run)."""
    root_text = str(root)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    ids = []

    def walk(item):
        if isinstance(item, unittest.TestSuite):
            for child in item:
                walk(child)
        else:
            ids.append(item.id())
    for target in targets:
        walk(unittest.TestLoader().loadTestsFromName(target))
    return ids


def unittest_child(run_dir, label, targets, root=None, expect_loaded=True):
    root = root or REPO
    argv = [sys.executable, "-B", "-m", "unittest", "-v"] + list(targets)
    record = run_child(run_dir, label, argv, root)
    stderr_text = Path(record["stderr_path"]).read_text(errors="replace") if "stderr_path" in record else ""
    parsed = parse_unittest(stderr_text)
    record.update(parsed)
    problems = []
    if expect_loaded:
        try:
            expected_ids = loaded_suite_ids(targets, root)
        except Exception as error:  # Unloadable suite: fail closed, keep the class.
            expected_ids = None
            record["loaded_suite_error"] = repr(error)[:2000]
        parsed_ids = [t["id"] for t in parsed["tests"]]
        record["loaded_suite_count"] = None if expected_ids is None else len(expected_ids)
        record["parsed_ids_match_loaded_suite"] = (expected_ids is not None and len(parsed_ids) == len(set(parsed_ids))
                                                   and sorted(parsed_ids) == sorted(expected_ids))
        if not record["parsed_ids_match_loaded_suite"]:
            problems.append(label + ":parsed_ids_differ_from_loaded_suite")
    if record.get("exit") != 0:
        problems.append(label + ":unittest_exit_nonzero")
    if parsed["summary"] != "OK":
        problems.append(label + ":unittest_summary_not_ok")
    if parsed["tests_run"] <= 0:
        problems.append(label + ":no_tests_executed")
    if len(parsed["tests"]) != parsed["tests_run"]:
        problems.append(label + ":executed_test_ids_incomplete")
    extras = sorted(k for k in parsed["summary_counts"] if k in ("skipped", "expected failures", "unexpected successes"))
    if extras:
        problems.append(label + ":unexpected_" + "_".join(k.replace(" ", "_") for k in extras))
    record["non_ok_test_ids"] = [t["id"] for t in parsed["tests"] if t["outcome"] != "ok"]
    if record["non_ok_test_ids"]:
        problems.append(label + ":non_ok_tests")
    record["problems"] = problems
    return record, problems


def _load_in_process(root):
    root_text = str(root)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    from campaign_tool.records.understanding import rows
    focused = __import__(FOCUSED_MODULE, fromlist=["*"])
    for module in (rows, focused):
        if not Path(module.__file__).resolve().is_relative_to(Path(root).resolve()):
            raise RuntimeError("module_loaded_outside_candidate")
    return rows, focused


def _baseline_rows():
    """The pinned-base rows module, built in memory from the git object (no disk write)."""
    base = baseline_commit()
    source = git("show", base + ":" + ROWS_PATH, binary=True)
    module = types.ModuleType("campaign_tool.records.understanding._fc4_baseline_rows")
    module.__package__ = "campaign_tool.records.understanding"
    module.__file__ = "<git {}:{}>".format(base, ROWS_PATH)
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    return module, sha256_bytes(source), base


def _summarise(outcome):
    return {"tests_run": outcome.testsRun,
            "failures": sorted({case.id() for case, _ in outcome.failures}),
            "errors": sorted({case.id() for case, _ in outcome.errors}),
            "skipped": sorted({case.id() for case, _ in outcome.skipped}),
            "unexpected_successes": [case.id() for case in outcome.unexpectedSuccesses],
            "successful": outcome.wasSuccessful()}


def _run_names(module, names):
    loader = unittest.TestLoader()
    suite = unittest.TestSuite(loader.loadTestsFromName(name, module) for name in names)
    stream = io.StringIO()
    outcome = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    return outcome, stream.getvalue()


def _case_id(focused, case):
    return "{}.{}.test_{}".format(focused.__name__, CASE_CLASS, case)


# ----------------------------------------------------------------- checks


def check_focused(run_dir, result):
    record, problems = unittest_child(run_dir, "focused-unittest", [FOCUSED_MODULE])
    result["children"].append(record)
    rows, focused = _load_in_process(REPO)
    expected_ids = {_case_id(focused, case) for case in FROZEN_CASES}
    executed = {t["id"] for t in record.get("tests", [])}
    result["case_map"] = {case: _case_id(focused, case) for case in FROZEN_CASES}
    if executed != expected_ids:
        problems.append("frozen_case_coverage_mismatch")
        result["case_map_missing"] = sorted(expected_ids - executed)
        result["case_map_extra"] = sorted(executed - expected_ids)
    # Genuine regression: the same cases run against the pinned-base rows module in memory.
    try:
        baseline, baseline_sha, base = _baseline_rows()
        original = focused.rows
        try:
            focused.rows = baseline
            outcome, text = _run_names(focused, [CASE_CLASS])
        finally:
            focused.rows = original
        (run_dir / "baseline-red.txt").write_text(text)
        summary = _summarise(outcome)
        failing = {i.rsplit(".", 1)[-1][len("test_"):] for i in summary["failures"] + summary["errors"]}
        result["baseline_red"] = {"base": base, "baseline_rows_sha256": baseline_sha,
                                  "summary": summary, "failing_cases": sorted(failing),
                                  "output_sha256": sha256_file(run_dir / "baseline-red.txt"),
                                  "restored_rows_module": focused.rows is rows}
        if failing != set(FROZEN_CASES) - BASE_GREEN:
            problems.append("baseline_red_cases_differ_from_expected")
        if summary["tests_run"] != len(FROZEN_CASES) or summary["skipped"]:
            problems.append("baseline_red_incomplete")
        if focused.rows is not rows:
            problems.append("rows_module_not_restored")
    except (subprocess.SubprocessError, OSError, RuntimeError, SyntaxError) as error:
        problems.append("baseline_red_incomplete:" + type(error).__name__)
    return problems


def check_compatibility(run_dir, result):
    problems = []
    final, final_problems = unittest_child(run_dir, "compatibility-final", COMPAT_SELECTORS)
    result["children"].append(final)
    problems += final_problems
    # Baseline: the same unchanged selectors on an extracted copy of the pinned base tree.
    base = baseline_commit()
    archive = run_dir / "baseline.tar"
    record = run_child(run_dir, "baseline-archive", ["git", "-C", REPO, "archive", "--format=tar", "-o", archive, base],
                       REPO)
    result["children"].append(record)
    if record.get("exit") != 0:
        return problems + ["baseline_archive_failed"]
    base_root = run_dir / "baseline-tree"
    with tarfile.open(archive) as handle:
        handle.extractall(base_root, filter="data")
    baseline, baseline_problems = unittest_child(run_dir, "compatibility-baseline", COMPAT_SELECTORS, root=base_root,
                                                 expect_loaded=False)
    result["children"].append(baseline)
    result["compatibility"] = {"base": base, "baseline_tests_run": baseline["tests_run"],
                               "baseline_summary": baseline["summary"], "final_tests_run": final["tests_run"],
                               "final_summary": final["summary"],
                               "baseline_ids": sorted(t["id"] for t in baseline["tests"]),
                               "final_ids": sorted(t["id"] for t in final["tests"])}
    if baseline_problems:
        problems.append("unexpected_baseline_failure")
    if result["compatibility"]["baseline_ids"] != result["compatibility"]["final_ids"]:
        problems.append("compatibility_test_ids_changed")
    return problems


NEGATIVE_REGRESSIONS = ("test_partial_blocked_unknown_page_status", "test_rows_retained_for_triage",
                        "test_multiple_pages_one_not_clear")
NEGATIVE_POSITIVES = ("test_ok_page_and_empty_page_inventory", "test_publication_stays_false")


def _direct(rows, focused):
    """Blocked page / ok page / incomplete top level through the real normalize()."""
    out = {}
    for name, pages, status in (("blocked_page", [focused.page("blocked", True)], "complete"),
                                ("ok_page", [focused.page()], "complete"),
                                ("top_level_partial", [focused.page()], "partial")):
        output, args, before = focused.run(pages, status=status)
        out[name] = {"complete": output["normalized"]["complete"],
                     "upstream_reasons": sorted(u["reason"] for u in output["upstream"]),
                     "receipt_hash_bound": sha256_bytes(args["receipts"][0]["derived_receipt"])
                     == args["receipts"][0]["derived_receipt_sha256"],
                     "inputs_unchanged": args == before,
                     "publication_ready": output["publication_ready"]}
    return out


def check_negative(run_dir, result):
    """In-memory mutant: only the page-state predicate is bypassed (always clear)."""
    problems = []
    rows, focused = _load_in_process(REPO)
    original = rows._page_clear
    disk_before = sha256_file(REPO / ROWS_PATH)
    phases = {}
    try:
        rows._page_clear = lambda page: True
        regress, regress_text = _run_names(focused, [CASE_CLASS + "." + n for n in NEGATIVE_REGRESSIONS])
        positive, positive_text = _run_names(focused, [CASE_CLASS + "." + n for n in NEGATIVE_POSITIVES])
        phases["mutant_regressions"] = _summarise(regress)
        phases["mutant_positives"] = _summarise(positive)
        phases["mutant_direct"] = _direct(rows, focused)
        try:  # Shape refusal is a separate gate and must survive the predicate bypass.
            focused.run([None])
            phases["mutant_shape_refusal"] = None
        except rows.RowsError as error:
            phases["mutant_shape_refusal"] = str(error)
        (run_dir / "negative-mutant.txt").write_text(regress_text + "\n" + positive_text)
    finally:
        rows._page_clear = original
    phases["restored_predicate"] = rows._page_clear is original
    restored, restored_text = _run_names(focused, [CASE_CLASS])
    phases["restored"] = _summarise(restored)
    phases["restored_direct"] = _direct(rows, focused)
    (run_dir / "negative-restored.txt").write_text(restored_text)
    for name in ("negative-mutant.txt", "negative-restored.txt"):
        phases[name + "_sha256"] = sha256_file(run_dir / name)
    phases["rows_on_disk_unchanged"] = sha256_file(REPO / ROWS_PATH) == disk_before
    result["negative"] = phases
    mutant = phases["mutant_regressions"]
    detected = set(mutant["failures"] + mutant["errors"])
    expected = {"{}.{}.{}".format(focused.__name__, CASE_CLASS, n) for n in NEGATIVE_REGRESSIONS}
    if mutant["tests_run"] != len(NEGATIVE_REGRESSIONS) or detected != expected:
        problems.append("mutant_not_detected_by_every_regression")
    md = phases["mutant_direct"]
    if not (md["blocked_page"]["complete"] is True and md["blocked_page"]["upstream_reasons"] == []
            and md["blocked_page"]["receipt_hash_bound"]):
        problems.append("mutant_did_not_reproduce_blocked_page_complete")
    if md["top_level_partial"]["complete"] is not False or md["ok_page"]["complete"] is not True:
        problems.append("mutant_controls_wrong")
    if phases["mutant_shape_refusal"] != "receipt_page_shape":
        problems.append("shape_gate_not_independent_of_predicate")
    if not phases["mutant_positives"]["successful"] or phases["mutant_positives"]["tests_run"] != len(NEGATIVE_POSITIVES):
        problems.append("positives_not_green_under_mutant")
    if not phases["restored_predicate"]:
        problems.append("predicate_not_restored")
    if not phases["restored"]["successful"] or phases["restored"]["tests_run"] != len(FROZEN_CASES) \
            or phases["restored"]["skipped"]:
        problems.append("restored_suite_not_green")
    rd = phases["restored_direct"]
    if not (rd["blocked_page"]["complete"] is False and rd["blocked_page"]["upstream_reasons"] == ["upstream_pages_not_clear"]
            and rd["ok_page"]["complete"] is True and rd["ok_page"]["upstream_reasons"] == []
            and rd["top_level_partial"]["complete"] is False
            and rd["top_level_partial"]["upstream_reasons"] == ["upstream_not_clear"]):
        problems.append("restored_direct_controls_wrong")
    if any(v["publication_ready"] is not False or not v["inputs_unchanged"] for d in (md, rd) for v in d.values()):
        problems.append("publication_or_input_preservation_broken")
    if not phases["rows_on_disk_unchanged"]:
        problems.append("rows_mutated_on_disk")
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
    names = git("ls-files", "-z", "--cached", "--others", "--exclude-standard").split("\0")
    return sorted({name for name in names if name})


def check_portability(run_dir, result):
    problems = []
    findings = static_inspection(REPO)
    result["static_inspection"] = {"files": list(STATIC_SCANNED), "findings": findings}
    if findings:
        problems.append("static_box_specific_or_inline_findings")
    with tempfile.TemporaryDirectory(prefix="fc4-page-copy-", dir=str(run_dir)) as temporary:
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
        argv = [sys.executable, "-I", "-B", str(copy_root / "tools" / "accept_fc4_row_page_state.py"),
                "--portability-child-report", str(report)]
        record = run_child(run_dir, "portability-child", argv, copy_root, env=env)
        result["children"].append(record)
        if record.get("exit") != 0:
            problems.append("portability_child_failed")
        if not report.is_file():
            return problems + ["portability_report_missing"]
        child = json.loads(report.read_text())
        record["report_sha256"] = sha256_file(report)
        result["portability_child"] = {k: child.get(k) for k in
                                       ("repo_root", "tests_run", "failures", "errors", "skipped",
                                        "sentinel_hits", "modules_outside_copy", "loaded_modules")}
        if child.get("repo_root") != str(copy_root.resolve()):
            problems.append("child_did_not_use_copy")
        if child.get("tests_run") != len(FROZEN_CASES) or child.get("failures") or child.get("errors") \
                or child.get("skipped"):
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
    suite = unittest.TestLoader().loadTestsFromName(FOCUSED_MODULE)
    stream = io.StringIO()
    outcome = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
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
    result["children"].append(run_child(run_dir, "detect-secrets-version", [scanner, "--version"], REPO))
    if scan.get("exit") != 0:
        problems.append("secret_scan_failed")
    try:
        data = json.loads(report.read_text())
        if not isinstance(data.get("results"), dict):
            raise ValueError("results")
        result["secret_scan"] = {"files_with_hits": len(data["results"]),
                                 "raw_hits": sum(len(v) for v in data["results"].values()),
                                 "hits_in_changed_files": sorted(set(data["results"]) & set(STATIC_SCANNED + (ROWS_PATH,)))}
    except (OSError, ValueError, AttributeError):
        return problems + ["secret_report_malformed"]
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
    if result["secret_scan"]["hits_in_changed_files"]:
        problems.append("raw_hits_in_changed_files")
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
    result = {"schema": "fc4-row-page-state-acceptance-v1", "card": CARD, "check": args.check,
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
                      "base": result["git"].get("base"), "clean": result["git"].get("clean"),
                      "run_json_sha256": sha256_file(run_dir / "run.json")}, sort_keys=True))
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
