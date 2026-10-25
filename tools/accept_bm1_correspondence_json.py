"""Acceptance runner for BM1-CORRESPONDENCE-JSON (offline, synthetic, review-required).

Usage (one suite per invocation):
    <interpreter> tools/accept_bm1_correspondence_json.py --suite SUITE --output-root DIR

SUITE is one of: focused, compatibility, negative, isolation, credential-scan,
public-tree. The output root must not be (or sit below) a symbolic link and must
lie outside this repository; it is checked before anything is written. Each
invocation then creates a fresh unique child directory under it holding raw
child stdout/stderr, argv, cwd, interpreter, git head/dirty state, source and
brief hashes, selector counts and ``run.json``. The exit status is computed from
explicit checks (never from ``assert``): 0 only when every check of the suite
holds. The repository root is derived from this file; import search paths are
set only inside this script; children are argument arrays without a shell.
The internal ``--isolation-child REPORT HOME`` entry (launched only by the
isolation suite) is validated the same way before any resolve, import or
write: both paths must be the link-free ``isolation-child.json`` and empty
``isolated-home`` of one unique isolation run directory outside the repository.

A passing result is machine validity only: REVIEW REQUIRED, never semantic,
source, legal or live acceptance, request fulfillment or publication authority.
"""
import sys

sys.dont_write_bytecode = True

import argparse  # noqa: E402  (bytecode writing is disabled before any further import)
import datetime  # noqa: E402
import difflib  # noqa: E402
import hashlib  # noqa: E402
import importlib.util  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
from pathlib import Path  # noqa: E402
import re  # noqa: E402
import secrets  # noqa: E402
import subprocess  # noqa: E402
import unittest  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
SUITES = ("focused", "compatibility", "negative", "isolation", "credential-scan", "public-tree")
TARGET = "campaign_tool/records/correspondence.py"
TEST_PATH = "tests/records/test_bm1_correspondence_json.py"
RUNNER_PATH = "tools/accept_bm1_correspondence_json.py"
TEST_MODULE = "tests.records.test_bm1_correspondence_json"
FOCUSED_CLASSES = ("PositiveHeaderTests", "DuplicateMemberTests", "BoundaryReplayTests")
BASE_SELECTOR = "parent of the first commit adding " + TEST_PATH + " (HEAD while it is uncommitted)"
HASHED = (TARGET, TEST_PATH, RUNNER_PATH, "tools/check_secret_report.py", "tools/check_public_tree.py")
# Candidate-root companions (outside the repository), hashed when present.
COMPANIONS = ("FROZEN-BRIEF.brief", "SOURCE-MANIFEST.json")
STATIC_SCANNED = (TEST_PATH, RUNNER_PATH)
# Assembled from parts so these lists do not match their own source text.
BOX_SPECIFIC = ("/ho" + "me/", "agent" + "-work", "venv" + "-full", "/Us" + "ers/", ".her" + "mes")
INLINE_FORMS = (re.compile(r"""["']-""" + r"""[ce]["']"""), re.compile("shell" + r"\s*=\s*True"),
                re.compile("PYTHON" + "PATH"), re.compile(r"\bos\.sys" + r"tem\("))
RAN = re.compile(r"^Ran (\d+) tests? in ", re.M)
SUMMARY = re.compile(r"^(OK|FAILED)(?: \(([^)]*)\))?\s*$", re.M)
VERBOSE = re.compile(r"^(test\w*) \(([\w.]+)\)(?:\n[^\n]*?)? \.\.\. (ok|FAIL|ERROR|skipped.*|expected failure|unexpected success)$",
                     re.M)


class RootRefused(Exception):
    """The requested output root is linked or inside the repository."""


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    return sha256_bytes(Path(path).read_bytes())


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def checked_output_root(value):
    """Absolute, link-free and outside REPO; checked before any write."""
    absolute = Path(os.path.abspath(value))
    for candidate in (absolute, *absolute.parents):
        if candidate.is_symlink():
            raise RootRefused("output_root_linked")
    resolved = absolute.resolve()
    repo = REPO.resolve()
    if resolved == repo or repo in resolved.parents:
        raise RootRefused("output_root_inside_repository")
    if resolved.exists() and not resolved.is_dir():
        raise RootRefused("output_root_not_directory")
    return absolute


def new_run_dir(root, suite):
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink():  # Re-check after creation: never follow a raced link.
        raise RootRefused("output_root_linked")
    run = root / "{}-{}-{}-{}".format(suite, now(), os.getpid(), secrets.token_hex(4))
    run.mkdir(exist_ok=False)
    return run


ISOLATION_RUN_NAME = re.compile(r"^isolation-\d{8}T\d{6}\.\d{6}Z-\d+-[0-9a-f]{8}$")
CHILD_REPORT_NAME = "isolation-child.json"
CHILD_HOME_NAME = "isolated-home"


def checked_child_paths(report, home):
    """Validate the internal isolation-child entry before any resolve, import or write.

    The report and home must be exactly ``<run>/isolation-child.json`` and
    ``<run>/isolated-home`` of one unique isolation run directory whose parent is
    an acceptable output root (link-free at every level, outside REPO). The run
    directory and home must be real (unlinked) directories, the home must be
    empty and the report must not yet exist. Raises RootRefused otherwise.
    """
    report_path = Path(os.path.abspath(report))
    home_path = Path(os.path.abspath(home))
    for path in (report_path, home_path):
        for candidate in (path, *path.parents):
            if candidate.is_symlink():
                raise RootRefused("isolation_child_path_linked")
    repo = REPO.resolve()
    for path in (report_path, home_path):
        if path == repo or repo in path.parents:
            raise RootRefused("isolation_child_path_inside_repository")
    run = report_path.parent
    if report_path.name != CHILD_REPORT_NAME or home_path.name != CHILD_HOME_NAME or home_path.parent != run:
        raise RootRefused("isolation_child_layout_mismatch")
    if not ISOLATION_RUN_NAME.match(run.name):
        raise RootRefused("isolation_child_not_in_unique_run_directory")
    checked_output_root(run.parent)
    if not run.is_dir() or not home_path.is_dir():
        raise RootRefused("isolation_child_directories_missing")
    if any(home_path.iterdir()):
        raise RootRefused("isolation_child_home_not_empty")
    if os.path.lexists(report_path):
        raise RootRefused("isolation_child_report_exists")
    return report_path, home_path


def source_hashes(root=REPO):
    return {name: (sha256_file(root / name) if (root / name).is_file() else None) for name in HASHED}


def companion_hashes():
    result = {}
    for name in COMPANIONS:
        path = REPO.parent / name
        result[name] = sha256_file(path) if path.is_file() and not path.is_symlink() else None
    return result


def git(*argv, binary=False):
    done = subprocess.run(["git", "-C", str(REPO), *argv], capture_output=True, timeout=120, check=False)
    return done.returncode, (done.stdout if binary else done.stdout.decode("utf-8", "replace").strip())


def git_identity():
    identity = {}
    for name, argv in (("head", ("rev-parse", "HEAD")),
                       ("status_porcelain", ("status", "--porcelain", "--untracked-files=all"))):
        try:
            code, out = git(*argv)
            identity[name] = out if code == 0 else None
            identity[name + "_exit"] = code
        except (OSError, subprocess.SubprocessError) as error:
            identity[name] = None
            identity[name + "_error"] = type(error).__name__
    identity["clean"] = identity.get("status_porcelain") == ""
    return identity


def run_child(run_dir, label, argv, cwd=REPO, env=None, stdout_path=None, timeout=900):
    """One argument-array child (no shell); raw streams are preserved."""
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


def parse_unittest(text):
    ran = RAN.findall(text)
    summary = SUMMARY.findall(text)
    tests = [{"id": verbose_test_id(m.group(1), m.group(2)),
              "outcome": "skipped" if m.group(3).startswith("skipped") else m.group(3)}
             for m in VERBOSE.finditer(text)]
    detail = summary[-1][1] if summary else ""
    counts = dict((k.strip(), int(v)) for k, v in (p.split("=") for p in detail.split(", ") if "=" in p))
    return {"tests_run": int(ran[-1]) if ran else 0, "summary": summary[-1][0] if summary else None,
            "summary_counts": counts, "tests": tests}


def import_candidate():
    root = str(REPO)
    if root not in sys.path:
        sys.path.insert(0, root)
    from campaign_tool.records import correspondence
    focused = __import__(TEST_MODULE, fromlist=["*"])
    for module in (correspondence, focused):
        if not Path(module.__file__).resolve().is_relative_to(REPO):
            raise RuntimeError("module_loaded_outside_candidate")
    return correspondence, focused


def loaded_ids(targets):
    import_candidate()
    ids = []

    def walk(item):
        if isinstance(item, unittest.TestSuite):
            for child in item:
                walk(child)
        else:
            ids.append(item.id())
    walk(unittest.TestLoader().loadTestsFromNames(list(targets)))
    return ids


def unittest_child(run_dir, label, targets):
    """``python -m unittest -v`` in a child; every loaded test must run and pass."""
    argv = [sys.executable, "-B", "-m", "unittest", "-v", *targets]
    record = run_child(run_dir, label, argv)
    text = Path(record["stderr_path"]).read_text(errors="replace") if "stderr_path" in record else ""
    parsed = parse_unittest(text)
    record.update(parsed)
    problems = []
    try:
        expected = loaded_ids(targets)
    except Exception as error:  # Unloadable selector: fail closed.
        expected = None
        record["loaded_suite_error"] = repr(error)[:2000]
    parsed_ids = [t["id"] for t in parsed["tests"]]
    record["selector_count"] = None if expected is None else len(expected)
    record["ids_match_loaded_suite"] = expected is not None and sorted(parsed_ids) == sorted(expected) \
        and len(parsed_ids) == len(set(parsed_ids))
    if not record["ids_match_loaded_suite"]:
        problems.append(label + ":ids_differ_from_loaded_suite")
    if record.get("exit") != 0:
        problems.append(label + ":exit_nonzero")
    if parsed["summary"] != "OK":
        problems.append(label + ":summary_not_ok")
    if parsed["tests_run"] <= 0 or len(parsed["tests"]) != parsed["tests_run"]:
        problems.append(label + ":test_count_invalid")
    if any(k in parsed["summary_counts"] for k in ("skipped", "expected failures", "unexpected successes")):
        problems.append(label + ":unexpected_skips_or_expectations")
    record["non_ok"] = [t["id"] for t in parsed["tests"] if t["outcome"] != "ok"]
    if record["non_ok"]:
        problems.append(label + ":non_ok_tests")
    return record, problems


def baseline_commit():
    code, out = git("log", "--format=%H", "--diff-filter=A", "HEAD", "--", TEST_PATH)
    added = [line for line in out.split() if line] if code == 0 else []
    if not added:
        code, head = git("rev-parse", "--verify", "HEAD")
        if code != 0:
            raise RuntimeError("head_unresolved")
        return head, "HEAD (focused test not yet committed)"
    code, parent = git("rev-parse", "--verify", added[-1] + "^1")
    if code != 0:
        raise RuntimeError("baseline_parent_unresolved")
    return parent, BASE_SELECTOR


def baseline_module(focused):
    base, selector = baseline_commit()
    code, source = git("show", base + ":" + TARGET, binary=True)
    if code != 0:
        raise RuntimeError("baseline_source_unavailable")
    module = focused.module_from_source(source.decode("utf-8"), "_bm1_pinned_baseline",
                                        "<git " + base + ":" + TARGET + ">")
    return module, {"commit": base, "selector": selector, "target_sha256": sha256_bytes(source)}


def write_text(run_dir, name, text):
    path = run_dir / name
    with open(path, "x", encoding="utf-8") as handle:
        handle.write(text)
    return {"path": str(path), "sha256": sha256_file(path)}


# --------------------------------------------------------------------- suites


def suite_focused(run_dir, result):
    targets = [TEST_MODULE + "." + name for name in FOCUSED_CLASSES]
    record, problems = unittest_child(run_dir, "focused-unittest", targets)
    result["children"].append(record)
    _, focused = import_candidate()
    baseline, identity = baseline_module(focused)
    red, red_text = focused.run_suite(focused.bound_suite(baseline, focused.DECIDING_TESTS))
    green, green_text = focused.run_suite(focused.bound_suite(baseline, focused.POSITIVE_CONTROLS))
    result["baseline"] = dict(identity, deciding=red, positives=green,
                              deciding_output=write_text(run_dir, "baseline-deciding.txt", red_text),
                              positives_output=write_text(run_dir, "baseline-positives.txt", green_text))
    if red["tests_run"] != len(focused.DECIDING_TESTS) or red["failed"] != sorted(focused.DECIDING_TESTS) \
            or red["errors"]:
        problems.append("baseline_regression_not_reproduced_by_every_deciding_test")
    if not green["successful"] or green["skipped"]:
        problems.append("baseline_legitimate_positives_not_green")
    return problems


def suite_compatibility(run_dir, result):
    record, problems = unittest_child(run_dir, "compatibility-unittest", [TEST_MODULE + ".Compatibility"])
    result["children"].append(record)
    correspondence, focused = import_candidate()
    baseline, identity = baseline_module(focused)
    rows = []
    for name, mail in sorted(focused.compatibility_cases().items()):
        old = focused.observe(baseline, mail)
        new = focused.observe(correspondence, mail)
        rows.append({"case": name, "baseline": old, "candidate": new,
                     "equal": old == new, "matches_pinned": new == focused.PINNED[name]})
    result["baseline_differential"] = dict(identity, rows=rows)
    if len(rows) != 5 or not all(row["equal"] and row["matches_pinned"] for row in rows):
        problems.append("compatibility_differs_from_pinned_baseline")
    return problems


def suite_negative(run_dir, result):
    """A4: isolated on-disk copy of the complete module with only the gate reverted."""
    problems = []
    correspondence, focused = import_candidate()
    production = REPO / TARGET
    before = production.read_bytes()
    source = before.decode("utf-8")
    mutated = focused.permissive_source(source)
    control_dir = run_dir / "gate-control"
    control_dir.mkdir()
    copy_path = control_dir / "correspondence_permissive_control.py"
    with open(copy_path, "x", encoding="utf-8") as handle:
        handle.write(mutated)
    delta = [line for line in difflib.unified_diff(source.splitlines(), mutated.splitlines(), lineterm="", n=0)
             if line[:1] in "+-" and not line.startswith(("+++", "---"))]
    spec = importlib.util.spec_from_file_location("campaign_tool.records._bm1_gate_control", copy_path)
    mutant = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mutant)
    red, red_text = focused.run_suite(focused.bound_suite(mutant, focused.DECIDING_TESTS))
    green, green_text = focused.run_suite(focused.bound_suite(mutant, focused.POSITIVE_CONTROLS))
    restored, restored_text = focused.run_suite(
        focused.bound_suite(correspondence, focused.DECIDING_TESTS + focused.POSITIVE_CONTROLS))
    after = production.read_bytes()
    result["negative"] = {
        "control_copy": {"path": str(copy_path), "sha256": sha256_file(copy_path)},
        "control_delta": delta, "mutant_file": mutant.__file__,
        "mutant_red": red, "mutant_positive": green, "restored": restored,
        "outputs": {"mutant_red": write_text(run_dir, "mutant-red.txt", red_text),
                    "mutant_positive": write_text(run_dir, "mutant-positive.txt", green_text),
                    "restored": write_text(run_dir, "restored-green.txt", restored_text)},
        "production_sha256_before": sha256_bytes(before), "production_sha256_after": sha256_bytes(after)}
    if delta != ["-" + " " * 8 + focused.GATE_LINE, "+" + " " * 8 + focused.PERMISSIVE_LINE]:
        problems.append("control_copy_changes_more_than_the_gate_line")
    if red["tests_run"] != len(focused.DECIDING_TESTS) or red["failed"] != sorted(focused.DECIDING_TESTS) \
            or red["errors"]:
        problems.append("mutation_not_detected_by_every_deciding_assertion")
    for required in ("DuplicateMemberTests.test_conflicting_parser_defects_valid_last_is_invalid",
                     "DuplicateMemberTests.test_equal_value_duplicates_are_invalid"):
        if required not in red["failed"]:
            problems.append("required_red_missing:" + required)
    if not green["successful"] or green["skipped"] or green["tests_run"] <= len(focused.POSITIVE_CONTROLS):
        problems.append("ordinary_valid_controls_not_green_under_mutation")
    if not restored["successful"] or restored["skipped"]:
        problems.append("restored_production_not_green")
    if after != before:
        problems.append("production_file_changed_by_control")
    record, child_problems = unittest_child(run_dir, "gate-control-unittest", [TEST_MODULE + ".GateControl"])
    result["children"].append(record)
    return problems + child_problems


def static_inspection():
    findings = []
    for relative in STATIC_SCANNED:
        for number, line in enumerate((REPO / relative).read_text().splitlines(), 1):
            if any(token in line for token in BOX_SPECIFIC):
                findings.append({"file": relative, "line": number, "kind": "box_specific_path"})
            if any(pattern.search(line) for pattern in INLINE_FORMS):
                findings.append({"file": relative, "line": number, "kind": "inline_or_shell_form"})
    return findings


def rejection_probes(run_dir):
    """Runner refusals, using only links and paths generated inside this run directory."""
    probes = []
    runner = REPO / RUNNER_PATH
    real = run_dir / "probe-real-target"
    real.mkdir()
    alias = run_dir / "probe-alias"
    alias.symlink_to(real, target_is_directory=True)
    inside = REPO / ("out-bm1-refusal-probe-" + secrets.token_hex(4))
    cases = (("unknown-suite", ["--suite", "everything", "--output-root", str(real)], real),
             ("linked-root", ["--suite", "public-tree", "--output-root", str(alias)], real),
             ("linked-ancestor", ["--suite", "public-tree", "--output-root", str(alias / "child")], real),
             ("repository-root", ["--suite", "public-tree", "--output-root", str(inside)], inside),
             ("missing-output-root", ["--suite", "public-tree"], real))
    for label, args, watched in cases:
        record = run_child(run_dir, "refusal-" + label, [sys.executable, "-I", "-B", runner, *args])
        record["watched_entries_after"] = sorted(p.name for p in watched.iterdir()) if watched.exists() else None
        record["refused_without_write"] = record.get("exit") == 2 and (
            (watched is inside and not inside.exists()) or (watched is real and record["watched_entries_after"] == []))
        probes.append(record)
    return probes


def fake_isolation_run(parent, home=True):
    run = parent / "isolation-{}-{}-{}".format(now(), os.getpid(), secrets.token_hex(4))
    run.mkdir()
    if home:
        (run / CHILD_HOME_NAME).mkdir()
    return run


def child_entry_probes(run_dir):
    """Internal ``--isolation-child`` entry refusals plus the legitimate-layout positive.

    Every link and path is generated inside this run directory (or is a
    never-created repository path). Each refusal must exit 2 with the expected
    reason and leave its watched directories unchanged; nothing is imported.
    """
    runner = REPO / RUNNER_PATH
    root = run_dir / "child-probe-root"
    root.mkdir()
    real_run = fake_isolation_run(root)
    alias_root = run_dir / "child-probe-alias"
    alias_root.symlink_to(root, target_is_directory=True)
    linked_run = root / "isolation-{}-{}-{}".format(now(), os.getpid(), secrets.token_hex(4))
    linked_run.symlink_to(real_run, target_is_directory=True)
    home_link_run = fake_isolation_run(root, home=False)
    home_target = run_dir / "child-probe-home-target"
    home_target.mkdir()
    (home_link_run / CHILD_HOME_NAME).symlink_to(home_target, target_is_directory=True)
    plain = root / "not-a-run-directory"
    plain.mkdir()
    (plain / CHILD_HOME_NAME).mkdir()
    inside = REPO / ("out-bm1-child-probe-" + secrets.token_hex(4))
    inside_run = inside / "isolation-{}-{}-{}".format(now(), os.getpid(), secrets.token_hex(4))

    def layout(run):
        return [str(run / CHILD_REPORT_NAME), str(run / CHILD_HOME_NAME)]

    real_entries = lambda: (sorted(p.name for p in real_run.iterdir()),  # noqa: E731
                            sorted(p.name for p in (real_run / CHILD_HOME_NAME).iterdir()))
    untouched = ([CHILD_HOME_NAME], [])
    cases = (
        ("child-linked-root", layout(alias_root / real_run.name), "isolation_child_path_linked",
         lambda: real_entries() == untouched),
        ("child-linked-intermediate", layout(linked_run), "isolation_child_path_linked",
         lambda: real_entries() == untouched),
        ("child-linked-home", layout(home_link_run), "isolation_child_path_linked",
         lambda: sorted(p.name for p in home_link_run.iterdir()) == [CHILD_HOME_NAME]
         and not any(home_target.iterdir())),
        ("child-repository-contained", layout(inside_run), "isolation_child_path_inside_repository",
         lambda: not os.path.lexists(inside)),
        ("child-layout-mismatch", [str(real_run / "report.json"), str(real_run / CHILD_HOME_NAME)],
         "isolation_child_layout_mismatch", lambda: real_entries() == untouched),
        ("child-not-unique-run", layout(plain), "isolation_child_not_in_unique_run_directory",
         lambda: sorted(p.name for p in plain.iterdir()) == [CHILD_HOME_NAME]
         and not any((plain / CHILD_HOME_NAME).iterdir())),
    )
    probes = []
    for label, args, reason, unchanged in cases:
        record = run_child(run_dir, "refusal-" + label, [sys.executable, "-I", "-B", runner, "--isolation-child", *args])
        try:
            record["refusal_reason"] = json.loads(Path(record["stderr_path"]).read_text()).get("reason")
        except (KeyError, OSError, ValueError, AttributeError):
            record["refusal_reason"] = None
        record["expected_reason"] = reason
        record["refused_without_write"] = record.get("exit") == 2 and record["refusal_reason"] == reason \
            and unchanged()
        record["passed"] = record["refused_without_write"]
        probes.append(record)
    # Legitimate positive: the same generated layout, unlinked, is accepted by the gate (no write, no import).
    try:
        accepted = [str(p) for p in checked_child_paths(*layout(real_run))]
        positive = {"label": "child-legitimate-layout-accepted", "accepted": accepted == layout(real_run)}
    except RootRefused as error:
        positive = {"label": "child-legitimate-layout-accepted", "accepted": False, "reason": str(error)}
    positive["passed"] = positive["accepted"] and real_entries() == untouched
    probes.append(positive)
    return probes


def suite_isolation(run_dir, result):
    problems = []
    findings = static_inspection()
    result["static_inspection"] = {"files": list(STATIC_SCANNED), "findings": findings}
    if findings:
        problems.append("static_box_specific_or_inline_findings")
    probes = rejection_probes(run_dir)
    result["children"].extend(probes)
    for probe in probes:
        if not probe["refused_without_write"]:
            problems.append("refusal_probe_failed:" + probe["label"])
    child_probes = child_entry_probes(run_dir)
    result["children"].extend(child_probes)
    result["child_entry_probes"] = [{"label": p["label"], "passed": p["passed"],
                                     "reason": p.get("refusal_reason", p.get("reason"))} for p in child_probes]
    if len(child_probes) != 7:
        problems.append("child_entry_probe_count_invalid")
    for probe in child_probes:
        if not probe["passed"]:
            problems.append("child_entry_probe_failed:" + probe["label"])
    isolated = run_dir / "isolated-home"
    isolated.mkdir()
    report = run_dir / "isolation-child.json"
    argv = [sys.executable, "-I", "-B", REPO / RUNNER_PATH, "--isolation-child", report, isolated]
    record = run_child(run_dir, "isolation-child", argv)
    result["children"].append(record)
    if record.get("exit") != 0:
        problems.append("isolation_child_failed")
    if not report.is_file():
        return problems + ["isolation_report_missing"]
    child = json.loads(report.read_text())
    record["report_sha256"] = sha256_file(report)
    result["isolation_child"] = {k: v for k, v in child.items() if k != "unittest_output"}
    if child.get("repo_root") != str(REPO):
        problems.append("child_used_another_repository")
    if not child.get("tests_run") or child.get("failures") or child.get("errors") or child.get("skipped"):
        problems.append("child_suite_not_green")
    if child.get("selector_count") != child.get("tests_run"):
        problems.append("child_selector_count_mismatch")
    for key in ("sentinel_hits", "audit_violations", "modules_outside_repository", "home_defaults_outside_repository",
                "imported_before_guards"):
        if child.get(key):
            problems.append("child_" + key)
    if not child.get("loaded_modules") or not child.get("home_redirected") or not child.get("bytecode_disabled"):
        problems.append("child_isolation_not_established")
    return problems


WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND


def isolation_child(report, isolated):
    """File-launched with -I: guards and redirects precede every project import."""
    preloaded = sorted(name for name in sys.modules if name.split(".")[0] in ("campaign_tool", "tests"))
    real_home = os.path.expanduser("~")
    isolated = str(isolated)  # Already validated link-free, absolute and outside REPO by checked_child_paths.
    for name in ("HOME", "TMPDIR", "TMP", "TEMP", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME",
                 "XDG_STATE_HOME", "XDG_RUNTIME_DIR"):
        os.environ[name] = isolated
    import tempfile
    tempfile.tempdir = isolated
    import socket
    hits, violations, state = [], [], {"recording": True}

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
    watched = ("socket.", "subprocess.", "os.system", "os.exec", "os.posix_spawn", "os.fork", "sqlite3.connect",
               "urllib.Request", "smtplib.", "imaplib.", "http.client.", "ftplib.", "webbrowser.")

    def audit(event, args):
        if not state["recording"]:
            return
        if event.startswith(watched):
            violations.append({"event": event})
        elif event == "open" and args and isinstance(args[0], (str, bytes, os.PathLike)):
            mode, flags = (args[1] or ""), (args[2] or 0)
            writing = any(c in str(mode) for c in "wax+") or bool(flags & WRITE_FLAGS)
            path = os.path.abspath(os.fsdecode(args[0]))
            if writing and not (path == isolated or path.startswith(isolated + os.sep)):
                violations.append({"event": "open-write", "path_inside_repository": path.startswith(str(REPO))})
    sys.addaudithook(audit)
    sys.path.insert(0, str(REPO))
    from campaign_tool.records import correspondence  # noqa: F401  (first project import, after guards)
    focused = __import__(TEST_MODULE, fromlist=["*"])
    defaults = home_defaults(real_home)
    suite = unittest.TestLoader().loadTestsFromModule(focused)
    selector_count = suite.countTestCases()
    stream = io.StringIO()
    outcome = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    loaded, outside = {}, []
    for name, module in sorted(sys.modules.items()):
        path = getattr(module, "__file__", None)
        if name.split(".")[0] not in ("campaign_tool", "tests") or path is None:
            continue
        loaded[name] = path
        if not Path(path).resolve().is_relative_to(REPO):
            outside.append(name)
    state["recording"] = False
    data = {"repo_root": str(REPO), "interpreter": sys.executable, "flags_isolated": sys.flags.isolated,
            "bytecode_disabled": sys.dont_write_bytecode, "imported_before_guards": preloaded,
            "home_redirected": Path.home() == Path(isolated) and tempfile.gettempdir() == isolated,
            "selector_count": selector_count, "tests_run": outcome.testsRun,
            "failures": [c.id() for c, _ in outcome.failures], "errors": [c.id() for c, _ in outcome.errors],
            "skipped": [c.id() for c, _ in outcome.skipped], "sentinel_hits": hits, "audit_violations": violations,
            "modules_outside_repository": outside, "loaded_modules": loaded,
            "home_defaults_outside_repository": defaults, "unittest_output": stream.getvalue()}
    with open(report, "x") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
    ok = (outcome.wasSuccessful() and outcome.testsRun == selector_count > 0 and not outcome.skipped
          and not hits and not violations and not outside and not defaults and not preloaded)
    return 0 if ok else 1


def home_defaults(real_home):
    """Module globals and bound function defaults that point into the real home outside REPO."""
    import types as _types
    found = []
    repo = str(REPO)

    def suspicious(value):
        text = os.fspath(value) if isinstance(value, (str, os.PathLike)) else None
        return bool(text) and (text.startswith("~") or (text.startswith(real_home + os.sep)
                                                       and not text.startswith(repo)))
    for name, module in sorted(sys.modules.items()):
        if name.split(".")[0] not in ("campaign_tool", "tests"):
            continue
        for attribute, value in sorted(vars(module).items()):
            values = [value]
            if isinstance(value, _types.FunctionType):
                values = list(value.__defaults__ or ()) + list((value.__kwdefaults__ or {}).values())
            for item in values:
                if suspicious(item):
                    found.append({"module": name, "attribute": attribute})
    return found


def suite_credential_scan(run_dir, result):
    problems = []
    scanner = Path(sys.executable).parent / "detect-secrets"
    result["scanner"] = {"path": str(scanner), "present": scanner.is_file()}
    if not scanner.is_file():
        return ["detect_secrets_unavailable_adjacent_to_interpreter"]
    version = run_child(run_dir, "detect-secrets-version", [scanner, "--version"])
    result["children"].append(version)
    report = run_dir / "secret-scan.json"
    scan = run_child(run_dir, "detect-secrets-scan",
                     [scanner, "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"],
                     stdout_path=report)
    result["children"].append(scan)
    if scan.get("exit") != 0:
        problems.append("secret_scan_failed")
    try:
        data = json.loads(report.read_text())
        if not isinstance(data.get("results"), dict):
            raise ValueError("results")
        result["secret_scan"] = {"files_with_hits": len(data["results"]),
                                 "raw_hits": sum(len(v) for v in data["results"].values())}
    except (OSError, ValueError, AttributeError):
        return problems + ["secret_report_malformed"]
    checker = run_child(run_dir, "check-secret-report",
                        [sys.executable, "-B", REPO / "tools" / "check_secret_report.py", report])
    result["children"].append(checker)
    text = Path(checker["stdout_path"]).read_text(errors="replace") if "stdout_path" in checker else ""
    match = re.search(r"potential secrets: (\d+);", text)
    result["secret_scan"]["potential_secrets"] = int(match.group(1)) if match else None
    if checker.get("exit") != 0:
        problems.append("check_secret_report_failed")
    if result["secret_scan"]["potential_secrets"] != 0:
        problems.append("potential_secrets_nonzero_or_unreported")
    return problems


def suite_public_tree(run_dir, result):
    record = run_child(run_dir, "check-public-tree", [sys.executable, "-B", REPO / "tools" / "check_public_tree.py"])
    result["children"].append(record)
    problems = [] if record.get("exit") == 0 else ["public_tree_check_failed"]
    try:
        data = json.loads(Path(record["stdout_path"]).read_text())
        result["public_tree"] = {"scanned_files": data.get("scanned_files"), "findings": data.get("findings")}
        if data.get("findings") or not data.get("scanned_files"):
            problems.append("public_tree_violations_or_empty_scan")
    except (KeyError, OSError, ValueError):
        problems.append("public_tree_output_unparsed")
    return problems


HANDLERS = {"focused": suite_focused, "compatibility": suite_compatibility, "negative": suite_negative,
            "isolation": suite_isolation, "credential-scan": suite_credential_scan, "public-tree": suite_public_tree}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--isolation-child"]:  # Internal: launched only by the isolation suite.
        if len(argv) != 3:
            print("isolation child takes exactly a report path and an isolated home", file=sys.stderr)
            return 2
        try:
            report, home = checked_child_paths(argv[1], argv[2])
        except (RootRefused, OSError) as error:
            print(json.dumps({"entry": "isolation-child", "verdict": "REFUSED",
                              "reason": str(error) or type(error).__name__}), file=sys.stderr)
            return 2
        return isolation_child(report, home)
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--suite", choices=SUITES, required=True)
    parser.add_argument("--output-root", required=True)
    args = parser.parse_args(argv)
    try:
        root = checked_output_root(args.output_root)
        run_dir = new_run_dir(root, args.suite)
    except (RootRefused, OSError) as error:
        print(json.dumps({"suite": args.suite, "verdict": "REFUSED", "reason": str(error) or type(error).__name__}),
              file=sys.stderr)
        return 2
    result = {"schema": "bm1-correspondence-json-acceptance-v1", "suite": args.suite,
              "argv": [sys.executable] + list(sys.argv), "cwd": os.getcwd(), "interpreter": sys.executable,
              "python_version": sys.version, "repo_root": str(REPO), "run_dir": str(run_dir),
              "started_utc": now(), "git": git_identity(), "source_hashes": source_hashes(),
              "companion_hashes": companion_hashes(), "children": [],
              "acceptance": "REVIEW REQUIRED; machine validity only, not semantic, source, legal or live acceptance"}
    try:
        problems = HANDLERS[args.suite](run_dir, result)
    except Exception as error:  # Incomplete suite: record the class and fail closed.
        problems = ["suite_incomplete:" + type(error).__name__]
        result["exception"] = repr(error)[:2000]
    result["finished_utc"] = now()
    result["source_hashes_after"] = source_hashes()
    if result["source_hashes_after"] != result["source_hashes"]:
        problems.append("sources_changed_during_run")
    result["problems"] = problems
    result["verdict"] = "PASS_REVIEW_REQUIRED" if not problems else "FAIL"
    with open(run_dir / "run.json", "x") as handle:
        json.dump(result, handle, indent=2, sort_keys=True, default=str)
    print(json.dumps({"suite": args.suite, "verdict": result["verdict"], "problems": problems,
                      "run_dir": str(run_dir), "head": result["git"].get("head"), "clean": result["git"].get("clean"),
                      "run_json_sha256": sha256_file(run_dir / "run.json")}, sort_keys=True))
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
