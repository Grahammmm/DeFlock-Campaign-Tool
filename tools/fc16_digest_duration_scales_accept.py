"""FC16-DIGEST-DURATION-SCALES acceptance recorder (five frozen --phase rows).

Each invocation validates the staged SOURCE-MANIFEST before any child imports
product code, creates a fresh UUID directory below --out-base (outside the
repository), runs real unittest / scanner / checker children as shell-disabled
argument lists, and writes summary.json with literal argv, cwd, interpreter,
manifest identity, final candidate HEAD/tree, source and fixture hashes, every
test and subtest outcome, counts, skips, exits and raw output hashes.

Machine validity is REVIEW REQUIRED, never factual, legal or human acceptance,
activation or publication authority. Nothing here pushes, publishes or writes
inside the repository.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid

HERE = Path(__file__).resolve()
REPO = HERE.parents[1]
PHASES = ("suite", "compatibility", "portable", "credentials", "public-tree")
MANIFEST_SCHEMA = "agentorange.source-manifest/v1"
MODULE = "tests.records.test_fc16_digest_duration_scales"
MODULE_FILE = "tests/records/test_fc16_digest_duration_scales.py"
FIXTURE_FILE = "tests/fixtures/fc16_digest_duration_scales/cases.json"
RECORDER_FILE = "tools/fc16_digest_duration_scales_accept.py"
AUTHORED = (MODULE_FILE, FIXTURE_FILE, RECORDER_FILE)
PINNED_SOURCES = ("campaign_tool/digest/detectors.py", "tests/runner/test_digest.py")
CHECKER_FILES = ("tools/check_public_tree.py", "campaign_tool/records/public_scan.py",
                 "tools/check_secret_report.py")
COMPAT_SELECTOR = "tests.runner.test_digest.DetectorTests"
REQUIRED_COMPAT = tuple(COMPAT_SELECTOR + "." + n for n in (
    "test_hits_on_synthetic_policy", "test_page_units_get_line_locators"))
REQUIRED_TESTS = (
    "ScaleTableTests.test_scale_table_is_frozen",
    "ScaleTableTests.test_frozen_case_counts",
    "UnitTableTests.test_unit_table",
    "ThresholdTests.test_threshold",
    "ThresholdTests.test_exact_sixty_is_within",
    "MultipleAndNoneTests.test_two_mentions_same_line",
    "MultipleAndNoneTests.test_two_mentions_across_units_in_source_order",
    "MultipleAndNoneTests.test_missing_retention_context_yields_none",
    "CaveatTests.test_every_hit_keeps_caveat_and_identity",
    "CaveatTests.test_caller_units_and_package_unchanged",
    "NegativeControlTests.test_month_multiplier_31_fails_two_month",
    "NegativeControlTests.test_inclusive_threshold_fails_exact_sixty",
)
CASE_MAP = {
    "unit_table": ["UnitTableTests.test_unit_table", "ScaleTableTests.test_scale_table_is_frozen",
                   "ScaleTableTests.test_frozen_case_counts"],
    "threshold": ["ThresholdTests.test_threshold", "ThresholdTests.test_exact_sixty_is_within"],
    "multiple_and_none": ["MultipleAndNoneTests.test_two_mentions_same_line",
                          "MultipleAndNoneTests.test_two_mentions_across_units_in_source_order",
                          "MultipleAndNoneTests.test_missing_retention_context_yields_none"],
    "caveat": ["CaveatTests.test_every_hit_keeps_caveat_and_identity",
               "CaveatTests.test_caller_units_and_package_unchanged"],
    "deciding_negative_control": ["NegativeControlTests.test_month_multiplier_31_fails_two_month",
                                  "NegativeControlTests.test_inclusive_threshold_fails_exact_sixty"],
}
# Box-specific path and inline-interpreter markers, assembled so this file does not match itself.
FORBIDDEN_MARKERS = tuple(m.encode() for m in (
    "/" + "home/", "/" + "Users/", "agent" + "-work", "venv" + "-fullsuite", "/" + "root/", "/" + "tmp/",
    "python3 -" + "c", "python -" + "c", "\"-" + "c\"", "'-" + "c'", "<" + "<EOF", "shell" + "=True",
    "os." + "system(", "PYTHON" + "PATH",
))


def sha_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha_file(path):
    return sha_bytes(Path(path).read_bytes())


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def write_new(path, data):
    with open(path, "xb") as handle:
        handle.write(data if isinstance(data, bytes) else data.encode("utf-8"))


def write_json(path, value):
    write_new(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def git(args, cwd=REPO, text=True):
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=text, timeout=120, shell=False,
                          stdin=subprocess.DEVNULL)
    return proc.returncode, proc.stdout


def git_out(args, cwd=REPO):
    code, out = git(args, cwd)
    return out.strip() if code == 0 else None


def clean_env(extra=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "LD_"))}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.update(extra or {})
    return env


class Run:
    def __init__(self, phase, out_base):
        base = Path(out_base).absolute()
        if base == REPO or REPO in base.parents:
            raise SystemExit("out-base must be outside the repository")
        base.mkdir(parents=True, exist_ok=True)
        self.dir = base / ("%s-%s" % (phase, uuid.uuid4()))
        self.dir.mkdir(exist_ok=False)
        self.children = []

    def child(self, name, argv, cwd, env=None, stdout_path=None, timeout=1800):
        out_path = Path(stdout_path) if stdout_path else self.dir / (name + ".stdout")
        err_path = self.dir / (name + ".stderr")
        with open(out_path, "xb") as out, open(err_path, "xb") as err:
            try:
                proc = subprocess.run([str(a) for a in argv], cwd=str(cwd), stdout=out, stderr=err,
                                      env=env if env is not None else clean_env(), shell=False,
                                      stdin=subprocess.DEVNULL, timeout=timeout)
                code, launch_error = proc.returncode, None
            except (OSError, subprocess.SubprocessError) as exc:
                code, launch_error = None, type(exc).__name__
        record = {"name": name, "argv": [str(a) for a in argv], "cwd": str(cwd), "exit": code,
                  "launch_error": launch_error, "stdout": str(out_path), "stdout_sha256": sha_file(out_path),
                  "stderr": str(err_path), "stderr_sha256": sha_file(err_path)}
        self.children.append(record)
        return record

    def unittest_child(self, name, mode, targets, root=REPO, cwd=None):
        result_path = self.dir / (name + ".result.json")
        temp_root = self.dir / (name + "-tmp")
        temp_root.mkdir()
        argv = [sys.executable, HERE, "--child", mode, "--child-result", result_path, "--root", root,
                "--temp-root", temp_root, *targets]
        record = self.child(name, argv, cwd or root,
                            env=clean_env({"TMPDIR": str(temp_root), "TEMP": str(temp_root), "TMP": str(temp_root)}))
        try:
            record["result"] = json.loads(result_path.read_text())
            record["result_sha256"] = sha_file(result_path)
        except (OSError, ValueError):
            record["result"] = None
        return record


# ---------------------------------------------------------------- identity


def load_manifest(path):
    raw = Path(path).read_bytes()
    return json.loads(raw), sha_bytes(raw)


def preflight(args):
    """Validate manifest, base identity, runtime paths and source pins before any child import."""
    problems = []
    manifest, manifest_sha = load_manifest(args.manifest)
    base = manifest.get("head")
    info = {"manifest_path": str(Path(args.manifest).absolute()), "manifest_sha256": manifest_sha,
            "manifest_schema": manifest.get("schema"), "manifest_head": base, "manifest_tree": manifest.get("tree"),
            "manifest_repo": manifest.get("repo")}
    if manifest.get("schema") != MANIFEST_SCHEMA:
        problems.append("manifest_schema")
    repo_arg = Path(args.repo).absolute()
    if Path(manifest.get("repo", "")).absolute() != repo_arg or repo_arg.resolve() != REPO:
        problems.append("manifest_repo_mismatch")
    if not base or git_out(["rev-parse", "--verify", base + "^{commit}"]) != base:
        return problems + ["manifest_head_unknown"], info, manifest
    if git_out(["rev-parse", base + "^{tree}"]) != manifest.get("tree"):
        problems.append("manifest_tree_mismatch")
    head = git_out(["rev-parse", "HEAD"])
    info["candidate_head"] = head
    info["candidate_tree"] = git_out(["rev-parse", "HEAD^{tree}"])
    code, _ = git(["merge-base", "--is-ancestor", base, "HEAD"])
    if code != 0:
        problems.append("head_not_descendant_of_base")
    if head == base:
        problems.append("head_is_base_not_candidate")
    runtime = manifest.get("runtime") or {}
    info["runtime"] = runtime
    for key in ("repo_venv", "credential_scan_python3", "detect_secrets"):
        if not runtime.get(key) or not Path(runtime[key]).exists():
            problems.append("runtime_missing:" + key)
    venv = REPO / ".venv"
    info["venv_link"] = os.readlink(venv) if venv.is_symlink() else None
    if info["venv_link"] != runtime.get("repo_venv"):
        problems.append("venv_link_not_manifest_bound")
    pins = {}
    for rel in PINNED_SOURCES:
        code, blob = git(["show", "%s:%s" % (base, rel)], text=False)
        current = (REPO / rel).read_bytes()
        pins[rel] = {"worktree_sha256": sha_bytes(current), "base_sha256": sha_bytes(blob) if code == 0 else None}
        if code != 0 or blob != current:
            problems.append("source_pin_drift:" + rel)
    info["source_pins"] = pins
    changed = git_out(["diff", "--name-only", base, "HEAD"])
    info["changed_paths"] = sorted(changed.split("\n")) if changed else []
    extra = sorted(set(info["changed_paths"]) - set(AUTHORED))
    if extra:
        problems.append("diff_outside_authored:" + ",".join(extra))
    missing = sorted(set(AUTHORED) - set(info["changed_paths"]))
    if missing:
        problems.append("authored_not_committed:" + ",".join(missing))
    return problems, info, manifest


def worktree_state():
    status = git_out(["status", "--porcelain", "--untracked-files=all"])
    others = git_out(["ls-files", "--others", "--exclude-standard"])
    return {"head": git_out(["rev-parse", "HEAD"]), "tree": git_out(["rev-parse", "HEAD^{tree}"]),
            "status": status, "untracked_visible": sorted(others.split("\n")) if others else []}


def file_hashes(root=REPO):
    return {rel: sha_file(Path(root) / rel) for rel in AUTHORED + PINNED_SOURCES + CHECKER_FILES
            if (Path(root) / rel).is_file()}


# ---------------------------------------------------------------- expectations


def fixture():
    return json.loads((REPO / FIXTURE_FILE).read_text(encoding="utf-8"))


def spell(word, case):
    if case == "lower":
        return word.lower()
    if case == "upper":
        return word.upper()
    return "".join(c.upper() if i % 2 == 0 else c.lower() for i, c in enumerate(word))


def expected_subtests(prefix):
    """Frozen subtest keys derived from the committed fixture; dimension counts are asserted separately."""
    data = fixture()
    table = data["unit_table"]
    keys = []
    for unit, words in table["units"].items():
        for value in table["values"]:
            for form in table["forms"]:
                for case in table["cases"]:
                    keys.append((prefix + "UnitTableTests.test_unit_table",
                                 (("case", "%s_%s_%d_%s" % (unit, form, value, case)),)))
    for case in data["threshold"]["cases"]:
        keys.append((prefix + "ThresholdTests.test_threshold", (("case", case["id"]),)))
    for case_id in data["threshold"]["exact_sixty_ids"]:
        keys.append((prefix + "ThresholdTests.test_exact_sixty_is_within", (("case", case_id),)))
    tests = {"month_multiplier": "test_month_multiplier_31_fails_two_month",
             "inclusive_threshold": "test_inclusive_threshold_fails_exact_sixty"}
    for name, spec in data["negative_controls"].items():
        test = prefix + "NegativeControlTests." + tests[name]
        for expect, ids in (("mutant_fails", spec["must_fail"]), ("mutant_passes_unrelated", spec["must_pass"]),
                            ("restored_passes", spec["must_fail"] + spec["must_pass"])):
            for case_id in ids:
                keys.append((test, (("case", case_id), ("control", name), ("expect", expect))))
    return keys


def dimension_problems():
    data = fixture()
    problems = []
    table = data["unit_table"]
    if sorted(table["units"]) != ["day", "hour", "month", "week", "year"]:
        problems.append("unit_dimension")
    if table["values"] != [1, 2, 24] or table["forms"] != ["singular", "plural"] or \
            table["cases"] != ["lower", "upper", "mixed"]:
        problems.append("unit_table_dimension")
    ids = [c["id"] for c in data["threshold"]["cases"]]
    if ids != ["hours_1439", "hours_1440", "hours_1441", "days_59", "days_60", "days_61", "weeks_8", "weeks_9",
               "months_1", "months_2", "months_3", "year_0", "year_1"]:
        problems.append("threshold_dimension")
    if data["negative_controls"]["month_multiplier"]["must_fail"] != ["months_2"] or \
            "days_60" not in data["negative_controls"]["inclusive_threshold"]["must_fail"]:
        problems.append("control_dimension")
    return problems


def suite_problems(result, prefix, required_names, expect_subtests=True):
    problems = []
    if not result:
        return ["missing_child_result"]
    for kind in ("failures", "errors", "skipped", "expected_failures", "unexpected_successes"):
        if result.get(kind):
            problems.append("%s:%s" % (kind, ",".join(result[kind])))
    required = {prefix + n for n in required_names}
    missing = sorted(required - set(result.get("passed", [])))
    if missing:
        problems.append("required_not_passed:" + ",".join(missing))
    accounted = len(result.get("passed", [])) + sum(len(result.get(k, [])) for k in (
        "failures", "errors", "skipped", "expected_failures", "unexpected_successes"))
    if result.get("tests_run", 0) <= 0 or result.get("tests_run") != accounted:
        problems.append("unaccounted_tests")
    if expect_subtests:
        seen = {}
        for sub in result.get("subtests", []):
            key = (sub["test"], tuple(sorted(sub["params"].items())))
            if key in seen:
                problems.append("duplicate_subtest")
            seen[key] = sub["outcome"]
        expected = expected_subtests(prefix)
        absent = [k for k in expected if k not in seen]
        if absent:
            problems.append("subtests_missing:%d" % len(absent))
        bad = [k for k in expected if seen.get(k) not in (None, "pass")]
        if bad:
            problems.append("subtests_not_passed:%d" % len(bad))
        unexpected = sorted(set(seen) - set(expected))
        if unexpected:
            problems.append("subtests_unexpected:%d" % len(unexpected))
        if len(expected) != 90 + 13 + 3 + 12 + 16:
            problems.append("frozen_subtest_count")
    return problems


def controls_summary(result, prefix):
    out = {}
    for sub in (result or {}).get("subtests", []):
        if sub["test"].startswith(prefix + "NegativeControlTests."):
            p = sub["params"]
            out.setdefault(p["control"], {}).setdefault(p["expect"], {})[p["case"]] = sub["outcome"]
    return out


# ---------------------------------------------------------------- phases


def phase_suite(run, manifest):
    rec = run.unittest_child("suite", "plain", [MODULE])
    prefix = MODULE + "."
    problems = suite_problems(rec["result"], prefix, REQUIRED_TESTS) + dimension_problems()
    if rec["exit"] != 0:
        problems.append("child_exit:%s" % rec["exit"])
    if rec["result"] and rec["result"].get("sentinel_hits"):
        problems.append("sentinel_hits")
    detail = {"case_to_test": {k: [prefix + t for t in v] for k, v in CASE_MAP.items()},
              "controls": controls_summary(rec["result"], prefix),
              "subtest_count": len((rec["result"] or {}).get("subtests", []))}
    return problems, detail


def clone_commit(run, name, commit):
    target = run.dir / name
    clone = run.child(name + "-clone", ["git", "clone", "--local", "--no-hardlinks", "--no-checkout",
                                        "--no-recurse-submodules", REPO, target], run.dir)
    checkout = run.child(name + "-checkout", ["git", "-C", target, "checkout", "--detach", commit], run.dir)
    ok = clone["exit"] == 0 and checkout["exit"] == 0
    return target, ok


def phase_compatibility(run, manifest):
    problems = []
    base_root, ok = clone_commit(run, "base-clone", manifest["head"])
    if not ok:
        problems.append("base_clone_failed")
    original = run.unittest_child("compat-original-base", "plain", [COMPAT_SELECTOR], root=base_root)
    final = run.unittest_child("compat-final-candidate", "plain", [COMPAT_SELECTOR])
    for label, rec in (("original", original), ("final", final)):
        problems += ["%s:%s" % (label, p) for p in suite_problems(rec["result"], "", REQUIRED_COMPAT, False)]
        if rec["exit"] != 0:
            problems.append("%s_child_exit:%s" % (label, rec["exit"]))
        expected_root = (base_root if label == "original" else REPO).resolve()
        if rec["result"] and Path(rec["result"]["source_root"]).resolve() != expected_root:
            problems.append(label + "_wrong_root")
    deltas = {}
    if original["result"] and final["result"]:
        for kind in ("passed", "failures", "errors", "skipped"):
            a, b = set(original["result"][kind]), set(final["result"][kind])
            if a != b:
                deltas[kind] = {"only_original": sorted(a - b), "only_final": sorted(b - a)}
    if deltas:
        problems.append("behavior_delta")
    return problems, {"selector": COMPAT_SELECTOR, "behavior_deltas": deltas, "base_clone": str(base_root),
                      "full_discovery": "OWED: operator-run on its admitted surface; not run or waived here"}


def phase_portable(run, manifest):
    problems = []
    inspected = {}
    for rel in AUTHORED:
        data = (REPO / rel).read_bytes()
        hits = sorted(m.decode() for m in FORBIDDEN_MARKERS if m in data)
        inspected[rel] = hits
        if hits:
            problems.append("machine_specific_marker:%s:%s" % (rel, ",".join(hits)))
    unrelated = run.dir / "unrelated-cwd" / "elsewhere"
    unrelated.mkdir(parents=True)
    temp_root = run.dir / "script-temp-root"
    result_path = run.dir / "script.result.json"
    script = run.child("script", [sys.executable, REPO / MODULE_FILE, "--temp-root", temp_root,
                                  "--result-json", result_path], unrelated,
                       env=clean_env({"TMPDIR": str(run.dir / "script-env-tmp")}))
    try:
        res = json.loads(result_path.read_text())
        script["result"] = res
        script["result_sha256"] = sha_file(result_path)
    except (OSError, ValueError):
        res = None
    problems += ["script:" + p for p in suite_problems(res, "__main__.", REQUIRED_TESTS)]
    if script["exit"] != 0:
        problems.append("script_exit:%s" % script["exit"])
    if res:
        if Path(res["temp_root"]) != temp_root:
            problems.append("script_temp_root_not_honoured")
        if Path(res["repo"]) != REPO or not Path(res["detectors_file"]).is_relative_to(REPO):
            problems.append("script_wrong_root")
    disc = run.unittest_child("discover-with-peers", "discover", [MODULE_FILE])
    dres = disc["result"]
    problems += ["discover:" + p for p in suite_problems(dres, MODULE + ".", REQUIRED_TESTS)]
    if disc["exit"] != 0:
        problems.append("discover_exit:%s" % disc["exit"])
    if not dres or sorted(dres.get("peer_passed", [])) != ["peer_after", "peer_before"]:
        problems.append("peer_sentinels_not_green")
    if dres and dres.get("sentinel_hits"):
        problems.append("sentinel_hits")
    return problems, {"inspected_markers": inspected, "unrelated_cwd": str(unrelated)}


def phase_credentials(run, manifest):
    problems = []
    runtime = manifest.get("runtime") or {}
    interpreter = runtime.get("credential_scan_python3")
    scanner = runtime.get("detect_secrets")
    if sys.executable != interpreter:
        problems.append("interpreter_not_manifest_credential_python")
    if not scanner or not Path(scanner).is_file():
        return problems + ["scanner_unavailable"], {"scanner": scanner}
    report = run.dir / "secret-scan.json"
    scan = run.child("detect-secrets", [scanner, "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"],
                     REPO, stdout_path=report)
    if scan["exit"] != 0:
        problems.append("scan_exit:%s" % scan["exit"])
    try:
        parsed = json.loads(report.read_text())
        if not isinstance(parsed.get("results"), dict):
            problems.append("report_malformed")
        version = parsed.get("version")
        flagged_authored = sorted(p for p in parsed.get("results", {}) if p in AUTHORED)
    except (OSError, ValueError, AttributeError):
        problems.append("report_malformed")
        version, flagged_authored = None, []
    if flagged_authored:
        problems.append("authored_files_flagged:" + ",".join(flagged_authored))
    checker = run.child("check-secret-report", [sys.executable, REPO / "tools" / "check_secret_report.py", report],
                        REPO)
    if checker["exit"] != 0:
        problems.append("checker_exit:%s" % checker["exit"])
    text = Path(checker["stdout"]).read_text()
    if "potential secrets: 0;" not in text:
        problems.append("potential_secrets_nonzero_or_unreported")
    return problems, {"interpreter": sys.executable, "scanner": scanner, "scanner_report_version": version,
                      "report": str(report), "report_sha256": sha_file(report),
                      "checker_summary": text.strip().splitlines()[-1:]}


def parse_scan(path):
    try:
        data = json.loads(Path(path).read_text())
        return data if isinstance(data, dict) and isinstance(data.get("findings"), list) else None
    except (OSError, ValueError):
        return None


def phase_public_tree(run, manifest):
    problems = []
    state = worktree_state()
    if state["status"] != "":
        problems.append("worktree_not_clean")
    if [p for p in state["untracked_visible"] if p != ".venv"]:
        problems.append("untracked_scan_visible")
    for args in (["diff", "--quiet"], ["diff", "--cached", "--quiet"]):
        if git(args)[0] != 0:
            problems.append("index_or_worktree_differs_from_head")
    base = manifest["head"]
    checker_hashes = {}
    for rel in CHECKER_FILES:
        code, blob = git(["show", "%s:%s" % (base, rel)], text=False)
        checker_hashes[rel] = sha_file(REPO / rel)
        if code != 0 or blob != (REPO / rel).read_bytes():
            problems.append("checker_changed:" + rel)
    inventory = git_out(["ls-files"])
    write_new(run.dir / "path-inventory.txt", (inventory or "") + "\n")
    head = state["head"]
    clone_root, ok = clone_commit(run, "committed-tree-clone", head)
    if not ok:
        problems.append("clone_failed")
    clone_head = git_out(["rev-parse", "HEAD"], cwd=clone_root)
    clone_tree = git_out(["rev-parse", "HEAD^{tree}"], cwd=clone_root)
    if clone_head != head or clone_tree != state["tree"]:
        problems.append("clone_identity_mismatch")
    if any(p.is_symlink() for p in clone_root.iterdir()):
        problems.append("clone_has_top_level_link")
    authored = {}
    for rel in AUTHORED:
        code, blob = git(["show", "HEAD:" + rel], text=False)
        authored[rel] = {"repo": sha_file(REPO / rel), "clone": sha_file(clone_root / rel),
                         "head_blob": sha_bytes(blob) if code == 0 else None}
        if len(set(authored[rel].values())) != 1:
            problems.append("authored_hash_mismatch:" + rel)
    clone_scan = run.child("check-public-tree-clone", [sys.executable, "-B", clone_root / "tools" / "check_public_tree.py"],
                           clone_root)
    clone_report = parse_scan(clone_scan["stdout"])
    if clone_scan["exit"] != 0 or clone_report is None or clone_report["findings"]:
        problems.append("clone_public_tree_not_clean")
    inplace = run.child("check-public-tree-in-place", [sys.executable, "-B", REPO / "tools" / "check_public_tree.py"],
                        REPO)
    inplace_report = parse_scan(inplace["stdout"])
    venv_ok = (REPO / ".venv").is_symlink() and os.readlink(REPO / ".venv") == (manifest.get("runtime") or {}).get(
        "repo_venv")
    if inplace_report is not None and inplace["exit"] == 0 and inplace_report["findings"] == []:
        inplace_class = "zero_findings"
    elif inplace_report is not None and inplace["exit"] == 1 and venv_ok and \
            inplace_report["findings"] == [[".venv", 0, "symlink_not_scanned"]]:
        inplace_class = "manifest_venv_symlink_only"
    else:
        inplace_class = "unrecognized"
        problems.append("in_place_unrecognized")
    after = worktree_state()
    if after != state:
        problems.append("source_identity_changed_during_row")
    return problems, {"state_before": state, "state_after": after, "checker_hashes": checker_hashes,
                      "path_inventory_sha256": sha_file(run.dir / "path-inventory.txt"),
                      "clone": str(clone_root), "clone_head": clone_head, "clone_tree": clone_tree,
                      "authored_hashes": authored, "clone_report": clone_report,
                      "in_place_informational": {"incident": "INC-174", "classification": inplace_class,
                                                 "exit": inplace["exit"], "report": inplace_report}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--phase", required=True, choices=PHASES)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--out-base", required=True)
    args = parser.parse_args(argv)
    started = utc_now()
    problems, identity, manifest = preflight(args)
    run = Run(args.phase, args.out_base)
    state_before = worktree_state()
    detail = {}
    if not any(p.startswith(("manifest_", "runtime_missing", "source_pin_drift")) for p in problems):
        handler = {"suite": phase_suite, "compatibility": phase_compatibility, "portable": phase_portable,
                   "credentials": phase_credentials, "public-tree": phase_public_tree}[args.phase]
        try:
            phase_problems, detail = handler(run, manifest)
        except Exception as exc:  # an incomplete row is a failure, never a pass
            phase_problems = ["runner_exception:%s:%s" % (type(exc).__name__, exc)]
        problems += phase_problems
    else:
        problems.append("phase_not_run_preflight_failed")
    state_after = worktree_state()
    if state_after != state_before:
        problems.append("candidate_identity_changed")
    diff_path = run.dir / "candidate.diff"
    code, diff = git(["diff", "--binary", manifest.get("head", "HEAD"), "HEAD"], text=False)
    write_new(diff_path, diff if code == 0 else b"")
    summary = {
        "card": "FC16-DIGEST-DURATION-SCALES", "phase": args.phase,
        "verdict": "PASS" if not problems else "FAIL", "problems": problems,
        "authority": "REVIEW REQUIRED: machine validity only; not factual/legal/human acceptance or publication",
        "argv": [sys.executable, *sys.argv], "cwd": os.getcwd(), "interpreter": sys.executable,
        "python_version": sys.version, "repo": str(REPO), "run_dir": str(run.dir),
        "identity": identity, "state_before": state_before, "state_after": state_after,
        "candidate_diff": str(diff_path), "candidate_diff_sha256": sha_file(diff_path),
        "file_hashes": file_hashes(), "started": started, "finished": utc_now(),
        "children": run.children, "detail": detail,
    }
    counts = {}
    for rec in run.children:
        res = rec.get("result")
        if isinstance(res, dict) and "tests_run" in res:
            counts[rec["name"]] = {"tests_run": res["tests_run"], "passed": len(res["passed"]),
                                   "skipped": len(res["skipped"]), "subtests": len(res.get("subtests", []))}
    summary["counts"] = counts
    write_json(run.dir / "summary.json", summary)
    print("%s %s run_dir=%s summary_sha256=%s problems=%s" % (
        args.phase, summary["verdict"], run.dir, sha_file(run.dir / "summary.json"), problems))
    return 0 if not problems else 1


# ---------------------------------------------------------------- child side


class RecordingResult(unittest.TextTestResult):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.passed, self.subtests = [], []

    def addSuccess(self, test):
        super().addSuccess(test)
        self.passed.append(test.id())

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        outcome = "pass" if err is None else ("fail" if issubclass(err[0], test.failureException) else "error")
        self.subtests.append({"test": test.id(), "params": {k: v for k, v in subtest.params.items()},
                              "outcome": outcome})


def run_suite(suite):
    runner = unittest.TextTestRunner(stream=sys.stderr, verbosity=2, resultclass=RecordingResult)
    res = runner.run(suite)
    return {"tests_run": res.testsRun, "passed": sorted(res.passed),
            "failures": sorted(t.id() for t, _ in res.failures), "errors": sorted(t.id() for t, _ in res.errors),
            "skipped": sorted(t.id() for t, _ in res.skipped),
            "expected_failures": sorted(t.id() for t, _ in res.expectedFailures),
            "unexpected_successes": sorted(t.id() for t in res.unexpectedSuccesses), "subtests": res.subtests}


SENTINEL_HITS = []


def install_sentinels():
    """Scoped to this child process only; offline guards, not a hostile-code sandbox."""
    import socket

    def blocked(name):
        def refuse(*a, **k):
            SENTINEL_HITS.append(name)
            raise PermissionError("offline sentinel: " + name)
        return refuse

    socket.socket.connect = blocked("socket.connect")
    socket.socket.connect_ex = blocked("socket.connect_ex")
    socket.create_connection = blocked("socket.create_connection")
    socket.getaddrinfo = blocked("socket.getaddrinfo")
    subprocess.Popen.__init__ = blocked("subprocess.Popen")
    os.system = blocked("os.system")
    for name in ("execv", "execve", "execvp", "execvpe", "fork", "forkpty", "posix_spawn", "posix_spawnp"):
        if hasattr(os, name):
            setattr(os, name, blocked("os." + name))


def peer_snapshot():
    import socket
    detectors = sys.modules["campaign_tool.digest.detectors"]
    probe = [{"locator": {"line": 1}, "text": "Peer sample is retained for 90 days."}]
    return {"environ": dict(os.environ), "tempdir": tempfile.tempdir, "gettempdir": tempfile.gettempdir(),
            "sys_path": list(sys.path), "cwd": os.getcwd(), "dont_write_bytecode": sys.dont_write_bytecode,
            "module_id": id(detectors), "days_id": id(detectors._days),
            "detect_id": id(detectors.detect_retention), "duration": detectors.DURATION.pattern,
            "context": detectors.RETENTION_CONTEXT.pattern, "reference": detectors.CHP_RETENTION_DAYS,
            "getaddrinfo_id": id(socket.getaddrinfo), "probe": detectors.detect_retention(probe, None)}


def peer_suite(baseline, passed):
    class PeerSentinel(unittest.TestCase):
        def check(self, label):
            self.assertEqual(peer_snapshot(), baseline)
            passed.append(label)

        def test_peer(self):
            self.check(self.label)

    before = type("PeerBefore", (PeerSentinel,), {"label": "peer_before"})
    after = type("PeerAfter", (PeerSentinel,), {"label": "peer_after"})
    return before("test_peer"), after("test_peer")


def child_main(mode, result_path, root, temp_root, targets):
    sys.dont_write_bytecode = True
    root_path = Path(root).resolve()
    tempfile.tempdir = str(Path(temp_root).absolute())
    sys.path[:] = [str(root_path)] + [p for p in sys.path if p not in ("", ".") and Path(p).resolve() not in (
        REPO, root_path, HERE.parent)]
    install_sentinels()
    out = {"mode": mode, "source_root": str(root_path), "targets": targets, "temp_root": tempfile.tempdir}
    loader = unittest.TestLoader()
    if mode == "plain":
        out.update(run_suite(loader.loadTestsFromNames(targets)))
    elif mode == "discover":
        import campaign_tool.digest.detectors  # noqa: F401  peer baseline before discovery imports the module
        baseline = peer_snapshot()
        peer_passed = []
        discovered = loader.discover(start_dir=str(root_path / Path(targets[0]).parent),
                                     pattern=Path(targets[0]).name, top_level_dir=str(root_path))
        before, after = peer_suite(baseline, peer_passed)
        result = run_suite(unittest.TestSuite([before, discovered, after]))
        peers = {t for t in result["passed"] if ".PeerBefore." in t or ".PeerAfter." in t}
        result["passed"] = sorted(set(result["passed"]) - peers)
        result["tests_run"] -= len(peers)
        result["peer_passed"] = sorted(peer_passed)
        out.update(result)
    else:
        return 2
    out["modules"] = {name: str(Path(mod.__file__).resolve()) for name, mod in sorted(sys.modules.items())
                      if name.split(".")[0] in ("campaign_tool", "tests", "runner")
                      and getattr(mod, "__file__", None)}
    out["sentinel_hits"] = sorted(set(SENTINEL_HITS))
    write_json(result_path, out)
    ok = out.get("tests_run", 0) > 0 and not out.get("failures") and not out.get("errors") \
        and not out.get("skipped") and not out["sentinel_hits"]
    return 0 if ok else 1


if __name__ == "__main__":
    if len(sys.argv) >= 9 and sys.argv[1] == "--child" and sys.argv[3] == "--child-result" \
            and sys.argv[5] == "--root" and sys.argv[7] == "--temp-root":
        raise SystemExit(child_main(sys.argv[2], sys.argv[4], sys.argv[6], sys.argv[8], sys.argv[9:]))
    raise SystemExit(main())
