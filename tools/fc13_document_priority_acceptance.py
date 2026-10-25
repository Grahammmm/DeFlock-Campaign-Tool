"""FC13-DOCUMENT-PRIORITY acceptance runner (five separate phases).

Usage (each phase is a separate invocation):

    python3 tools/fc13_document_priority_acceptance.py --phase PHASE \
        --source-manifest SOURCE-MANIFEST.json --out-base OUT_BASE

PHASE is one of suite, compatibility, portable, credentials, public-tree.
Every invocation writes to a fresh UUID directory below OUT_BASE, which must be
outside the repository, and never overwrites earlier evidence. The repository
and fixtures are derived from this file's location. Box-specific paths come
only from the command line and the operator source manifest.

Every check is an explicit comparison recorded in receipt.json. The evidence
manifest is sealed only after all child processes have exited. A pass is
machine evidence for review only (REVIEW REQUIRED), not semantic acceptance.
"""
import argparse
import datetime
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

REPO = Path(__file__).resolve().parents[1]
TEST_REL = "tests/records/test_fc13_document_priority.py"
FIXTURE_REL = "tests/fixtures/fc13-document-priority/cases.json"
RUNNER_REL = "tools/fc13_document_priority_acceptance.py"
AUTHORED = (TEST_REL, FIXTURE_REL, RUNNER_REL)
PRODUCT_REL = "campaign_tool/records/understanding/document.py"
COMPAT_TEST_REL = "tests/records/test_understanding.py"
COMPAT_SELECTOR = "tests.records.test_understanding.DocumentTests"
TEST_MODULE = "tests.records.test_fc13_document_priority"
SCANNER_FILES = ("tools/check_public_tree.py", "campaign_tool/records/public_scan.py",
                 "tools/check_secret_report.py")
PHASES = ("suite", "compatibility", "portable", "credentials", "public-tree")
CHILD_TIMEOUT = 900


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    return sha256_bytes(Path(path).read_bytes())


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class Row:
    """One phase invocation: fresh directory, recorded checks and children."""

    def __init__(self, phase, out_base, argv):
        self.phase = phase
        self.dir = out_base / ("%s-%s" % (phase, uuid.uuid4().hex))
        self.dir.mkdir(parents=False, exist_ok=False)
        self.checks = []
        self.children = []
        self.data = {"schema": "fc13-document-priority-receipt/v1", "phase": phase,
                     "argv": list(argv), "cwd": os.getcwd(), "interpreter": sys.executable,
                     "python_version": sys.version, "started_at": now(),
                     "review_status": "REVIEW REQUIRED; machine-valid output is not semantic acceptance",
                     "full_discovery": "OWED: operator-run mandatory full discovery and exact-head required CI"}

    def check(self, name, ok, detail=None):
        self.checks.append({"name": name, "ok": bool(ok), "detail": detail})
        return bool(ok)

    def run(self, label, argv, cwd, env=None, stdout_path=None):
        """Run one shell-disabled argv child; save raw stdout/stderr and hashes."""
        record = {"label": label, "argv": [str(a) for a in argv], "cwd": str(cwd), "started_at": now()}
        if env is not None:
            record["env_delta"] = {k: env[k] for k in sorted(env) if os.environ.get(k) != env.get(k)}
        try:
            proc = subprocess.run([str(a) for a in argv], cwd=str(cwd), env=env, shell=False,
                                  capture_output=True, timeout=CHILD_TIMEOUT, check=False)
            code, out, err = proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired as exc:
            code, out, err = "timeout", exc.stdout or b"", exc.stderr or b""
        index = len(self.children) + 1
        out_path = Path(stdout_path) if stdout_path else self.dir / ("%02d-%s.stdout" % (index, label))
        err_path = self.dir / ("%02d-%s.stderr" % (index, label))
        with open(out_path, "xb") as handle:
            handle.write(out)
        with open(err_path, "xb") as handle:
            handle.write(err)
        record.update(exit=code, ended_at=now(), stdout_path=out_path.name, stderr_path=err_path.name,
                      stdout_sha256=sha256_bytes(out), stderr_sha256=sha256_bytes(err),
                      stdout_bytes=len(out), stderr_bytes=len(err))
        self.children.append(record)
        return code, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

    def git(self, label, args, cwd=REPO):
        return self.run(label, ["git", *args], cwd)

    def finish(self):
        ok = bool(self.checks) and all(c["ok"] for c in self.checks)
        self.data.update(ended_at=now(), checks=self.checks, children=self.children,
                         failed_checks=[c["name"] for c in self.checks if not c["ok"]],
                         verdict="PASS_REVIEW_REQUIRED" if ok else "FAIL")
        receipt = self.dir / "receipt.json"
        with open(receipt, "x", encoding="utf-8") as handle:
            json.dump(self.data, handle, indent=2, sort_keys=True, default=str)
        # Seal after every child has exited and the receipt is written.
        entries = []
        skipped_dirs = []
        for path in sorted(self.dir.rglob("*")):
            rel = path.relative_to(self.dir).as_posix()
            if rel.split("/")[0] in ("clone", "baseline-clone"):
                if "/" not in rel:
                    skipped_dirs.append(rel)
                continue
            if path.is_symlink():
                entries.append({"path": rel, "symlink": os.readlink(path)})
            elif path.is_file():
                entries.append({"path": rel, "sha256": sha256_file(path), "bytes": path.stat().st_size})
        manifest = {"schema": "fc13-document-priority-evidence-manifest/v1", "phase": self.phase,
                    "sealed_at": now(), "verdict": self.data["verdict"], "files": entries,
                    "excluded_clone_dirs": skipped_dirs,
                    "excluded_note": "row-local git clones are bound by recorded HEAD/tree, not file hashes"}
        sealed = self.dir / "evidence-manifest.json"
        with open(sealed, "x", encoding="utf-8") as handle:
            json.dump(manifest, handle, indent=2, sort_keys=True)
        digest = sha256_file(sealed)
        print(json.dumps({"phase": self.phase, "verdict": self.data["verdict"], "row_dir": str(self.dir),
                          "evidence_manifest_sha256": digest, "failed_checks": self.data["failed_checks"],
                          "checks": len(self.checks)}, sort_keys=True))
        return 0 if ok else 1


def load_manifest(row, manifest_path):
    """Verify manifest repo/head/tree/runtime and candidate identity before any test import."""
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    row.data["source_manifest"] = {"path": str(manifest_path), "sha256": sha256_file(manifest_path)}
    row.check("manifest_schema", manifest.get("schema") == "agentorange.source-manifest/v1", manifest.get("schema"))
    row.check("manifest_repo_is_runner_repo",
              Path(manifest.get("repo", "")).resolve() == REPO, {"manifest": manifest.get("repo"), "runner": str(REPO)})
    runtime = manifest.get("runtime") or {}
    for key in ("repo_venv", "credential_scan_python3", "detect_secrets"):
        row.check("manifest_runtime_%s_exists" % key, bool(runtime.get(key)) and Path(runtime[key]).exists(),
                  runtime.get(key))
    base = manifest.get("head", "")
    code, out, _ = row.git("base-tree", ["rev-parse", "--verify", base + "^{tree}"])
    row.check("manifest_base_tree", code == 0 and out.strip() == manifest.get("tree"),
              {"base": base, "tree": out.strip(), "manifest_tree": manifest.get("tree")})
    code, out, _ = row.git("head", ["rev-parse", "--verify", "HEAD^{commit}"])
    head = out.strip()
    code2, tree_out, _ = row.git("head-tree", ["rev-parse", "--verify", "HEAD^{tree}"])
    row.data["candidate"] = {"base": base, "head": head, "tree": tree_out.strip()}
    row.check("head_resolved", code == 0 and code2 == 0 and bool(head), head)
    row.check("head_is_descendant_not_base", head != base and
              row.git("ancestry", ["merge-base", "--is-ancestor", base, head])[0] == 0, {"base": base, "head": head})
    code, out, _ = row.git("diff-names", ["diff", "--name-status", "--no-renames", base, head])
    names = sorted(line.split("\t", 1)[1] for line in out.splitlines() if "\t" in line)
    statuses = sorted(line.split("\t", 1)[0] for line in out.splitlines() if "\t" in line)
    row.check("diff_is_exact_write_set", code == 0 and names == sorted(AUTHORED) and set(statuses) == {"A"},
              out.splitlines())
    for rel in (PRODUCT_REL, COMPAT_TEST_REL) + SCANNER_FILES:
        c1, b1, _ = row.git("blob-base", ["rev-parse", "%s:%s" % (base, rel)])
        c2, b2, _ = row.git("blob-head", ["rev-parse", "%s:%s" % (head, rel)])
        row.check("unchanged_from_base:" + rel, c1 == 0 and c2 == 0 and b1.strip() == b2.strip(),
                  {"sha256": sha256_file(REPO / rel)})
    code, out, _ = row.git("commit-bodies", ["log", "--format=%H%x00%B%x01", "%s..%s" % (base, head)])
    commits = [c.strip() for c in out.split("\x01") if c.strip()]
    signed = [c for c in commits if re.search(r"^Signed-off-by: .+<.+>$", c.split("\x00", 1)[-1], re.M)]
    row.check("all_commits_signed_off", code == 0 and commits and len(signed) == len(commits),
              {"commits": len(commits), "signed": len(signed)})
    clean_worktree(row, "pre")
    authored = {}
    for rel in AUTHORED:
        c, blob, _ = row.git("authored-blob", ["rev-parse", "%s:%s" % (head, rel)])
        authored[rel] = {"sha256": sha256_file(REPO / rel), "blob": blob.strip()}
    row.data["authored"] = authored
    row.data["product_sha256"] = {rel: sha256_file(REPO / rel) for rel in (PRODUCT_REL, COMPAT_TEST_REL)}
    return manifest, head


def clean_worktree(row, when, venv_target=None):
    code, out, _ = row.git("status-" + when, ["status", "--porcelain=v1", "--untracked-files=all"])
    lines = [line for line in out.splitlines() if line]
    row.check("worktree_clean_except_venv_link:" + when, code == 0 and lines in ([], ["?? .venv"]), lines)
    c1 = row.git("index-vs-head-" + when, ["diff", "--cached", "--quiet"])[0]
    c2 = row.git("worktree-vs-index-" + when, ["diff", "--quiet"])[0]
    row.check("index_and_worktree_equal_head:" + when, c1 == 0 and c2 == 0, {"cached": c1, "worktree": c2})
    return lines


def child_env(tmp_dir):
    env = dict(os.environ)
    env["TMPDIR"] = str(tmp_dir)
    env.pop("PYTHONPATH", None)
    return env


def fixture_cases():
    return json.loads((REPO / FIXTURE_REL).read_text(encoding="utf-8"))


def expected_test_names(cases):
    """Complete frozen test-name set derived from the fixture, not from the module."""
    order = cases["priority_order"]
    names = {"FC13FixtureTests.test_fixture_is_frozen_shape",
             "FC13FixtureTests.test_configured_categories_match_frozen_set_without_reading_order",
             "FC13SingleCategoryTests.test_each_synonym_and_case_variant_is_a_single_positive",
             "FC13SingleCategoryTests.test_word_boundary_negatives_classify_nothing",
             "FC13SingleCategoryTests.test_neutral_units_classify_nothing",
             "FC13MultiUnitTests.test_units_in_one_bundle_are_classified_independently",
             "FC13MultiUnitTests.test_receipt_and_unit_permutations_keep_per_unit_identity",
             "FC13NegativeControlTests.test_reversed_classifiers_kill_competing_assertion"}
    names |= {"FC13RepeatedTermTests.test_" + c["name"] for c in cases["repeated_cases"]}
    names |= {"FC13PairPriorityTests.test_pair_%s_then_%s" % (a, b) for a in order for b in order if a != b}
    return names


def case_dimensions(cases):
    order = cases["priority_order"]
    syn = cases["synonyms"]
    return {"categories": len(order), "ordered_category_pairs": len(order) * (len(order) - 1),
            "pair_text_cases": sum(len(syn[a]) * len(syn[b]) for a in order for b in order if a != b),
            "single_synonym_positives": sum(len(syn[c]) for c in order),
            "case_positives": len(cases["case_positives"]),
            "boundary_negatives": len(cases["boundary_negatives"]),
            "neutral_texts": len(cases["neutral_texts"]), "repeated_cases": len(cases["repeated_cases"]),
            "multi_unit_texts": len(cases["multi_unit_texts"]),
            "permutation_bundles": len(cases["text_rotations"]) * len(cases["input_orders"])}


def short(test_id):
    return ".".join(test_id.split(".")[-2:])


def script_run(row, label, cwd, control="none"):
    tmp = row.dir / (label + "-tmp")
    tmp.mkdir()
    result = row.dir / (label + "-result.json")
    argv = [sys.executable, str(REPO / TEST_REL), "--fc13-tmp-root", str(tmp), "--fc13-result", str(result),
            "--fc13-control", control]
    code, _, err = row.run(label, argv, cwd, env=child_env(tmp))
    report = json.loads(result.read_text(encoding="utf-8")) if result.is_file() else None
    leftovers = sorted(p.name for p in tmp.iterdir())
    return code, report, err, leftovers


def check_full_pass(row, label, code, report, expected):
    row.check(label + ":exit_zero", code == 0, code)
    if not row.check(label + ":result_present", report is not None):
        return
    passed = {short(t) for t in report["passed"]}
    row.check(label + ":exact_frozen_test_set", passed == expected and report["tests_run"] == len(expected),
              {"missing": sorted(expected - passed), "extra": sorted(passed - expected),
               "tests_run": report["tests_run"]})
    row.check(label + ":no_fail_error_skip", not report["failures"] and not report["errors"] and
              not report["skipped"] and report["expected_failures"] == 0 and report["unexpected_successes"] == 0,
              {"failures": len(report["failures"]), "errors": len(report["errors"]),
               "skipped": report["skipped"]})


def phase_suite(row, manifest, head):
    cases = fixture_cases()
    expected = expected_test_names(cases)
    row.data["case_dimensions"] = case_dimensions(cases)
    row.data["expected_test_ids"] = sorted(expected)
    code, report, _, _ = script_run(row, "genuine-positive", row.dir)
    check_full_pass(row, "genuine-positive", code, report, expected)
    row.data["genuine_positive_test_ids"] = report and sorted(report["passed"])
    # Declared deciding negative control: reverse only _CLASSIFIERS in memory in an isolated child.
    code, report, _, _ = script_run(row, "control-reverse-classifiers", row.dir, control="reverse-classifiers")
    patterns = cases["negative_control"]["expected_failing_test_patterns"]
    expected_fail = {n for n in expected if any(fnmatch.fnmatchcase(n, p) for p in patterns)}
    row.check("control:exit_nonzero", code == 1, code)
    if row.check("control:result_present", report is not None):
        failed = {short(f["id"]) for f in report["failures"]}
        passed = {short(t) for t in report["passed"]}
        row.check("control:classifier_order_reversed",
                  report["classifier_order"] == list(reversed(cases["priority_order"])), report["classifier_order"])
        row.check("control:failing_set_is_exactly_competing_tests", failed == expected_fail,
                  {"missing": sorted(expected_fail - failed), "extra": sorted(failed - expected_fail)})
        row.check("control:failures_are_priority_assertions",
                  all(cases["negative_control"]["failure_tag"] in f["traceback"] and
                      "AssertionError" in f["traceback"] for f in report["failures"]), len(report["failures"]))
        row.check("control:no_errors_or_skips", not report["errors"] and not report["skipped"],
                  [e["id"] for e in report["errors"]])
        row.check("control:neutral_and_single_positives_still_pass", passed == expected - expected_fail,
                  {"passed": len(passed), "expected": len(expected - expected_fail)})
        row.data["control_failing_test_ids"] = sorted(failed)
    # Restore (fresh process, unmutated) and rerun the genuine positive.
    code, report, _, _ = script_run(row, "restored-positive", row.dir)
    check_full_pass(row, "restored-positive", code, report, expected)
    # Ordinary unittest invocation of the module only.
    tmp = row.dir / "unittest-tmp"
    tmp.mkdir()
    code, _, err = row.run("unittest-module", [sys.executable, "-m", "unittest", "-v", TEST_MODULE], REPO,
                           env=child_env(tmp))
    ran = re.search(r"^Ran (\d+) tests? in", err, re.M)
    row.check("unittest-module:exit_zero_ok", code == 0 and re.search(r"^OK$", err, re.M) is not None, code)
    row.check("unittest-module:count", ran is not None and int(ran.group(1)) == len(expected),
              ran and ran.group(1))
    row.check("unittest-module:no_skips", "skipped" not in err.splitlines()[-1] if err.splitlines() else False)
    clean_worktree(row, "post")


def clone_at(row, name, commit):
    target = row.dir / name
    code = row.git("clone-" + name, ["clone", "--local", "--no-hardlinks", "--no-checkout",
                                     "--no-recurse-submodules", str(REPO), str(target)], cwd=row.dir)[0]
    code2 = row.git("checkout-" + name, ["checkout", "--detach", commit], cwd=target)[0]
    c3, out, _ = row.git("clone-head-" + name, ["rev-parse", "HEAD"], cwd=target)
    c4, tree, _ = row.git("clone-tree-" + name, ["rev-parse", "HEAD^{tree}"], cwd=target)
    row.check("clone:%s:exact_head" % name, code == 0 and code2 == 0 and c3 == 0 and out.strip() == commit,
              out.strip())
    row.data.setdefault("clones", {})[name] = {"head": out.strip(), "tree": tree.strip()}
    return target


def phase_compatibility(row, manifest, head):
    base = manifest["head"]
    baseline = clone_at(row, "baseline-clone", base)
    outcomes = {}
    for label, cwd in (("baseline", baseline), ("final", REPO)):
        tmp = row.dir / (label + "-tmp")
        tmp.mkdir()
        code, _, err = row.run(label + "-selector", [sys.executable, "-m", "unittest", "-v", COMPAT_SELECTOR], cwd,
                               env=child_env(tmp))
        ran = re.search(r"^Ran (\d+) tests? in", err, re.M)
        ids = sorted(re.findall(r"^(test_\w+) \(([\w.]+)\)", err, re.M))
        outcomes[label] = {"exit": code, "ran": ran and int(ran.group(1)), "ids": ids,
                           "ok": re.search(r"^OK$", err, re.M) is not None}
        row.check(label + ":selector_exit_zero_ok", code == 0 and outcomes[label]["ok"], code)
        row.check(label + ":selector_ran_tests", bool(ran) and int(ran.group(1)) > 0, ran and ran.group(1))
        row.check(label + ":no_skips", "skipped" not in err, None)
    row.check("baseline_and_final_same_tests", outcomes["baseline"]["ids"] == outcomes["final"]["ids"] and
              outcomes["baseline"]["ran"] == outcomes["final"]["ran"], outcomes)
    row.data["compatibility"] = {"selector": COMPAT_SELECTOR, "outcomes": outcomes,
                                 "intentional_deltas": "none: test-only change; selector file and product unchanged",
                                 "scope_note": "scoped local compatibility only; full discovery and exact-head CI OWED"}
    clean_worktree(row, "post")


DISCOVERY_CHILD = "discovery-child"


def ambient_snapshot():
    import socket
    import tempfile
    return {"environ": dict(os.environ), "tempfile_tempdir": tempfile.tempdir, "cwd": os.getcwd(),
            "argv": list(sys.argv), "socket_socket": id(socket.socket),
            "socket_create_connection": id(socket.create_connection), "subprocess_popen": id(subprocess.Popen),
            "sys_path": [p for p in sys.path if Path(p or ".").resolve() != REPO]}


def discovery_child(result_path):
    """Isolated child: sentinel tests before/after ordinary discovery of the new module only."""
    import unittest
    before_import = ambient_snapshot()
    loader = unittest.TestLoader()
    discovered = loader.discover(start_dir=str(REPO / "tests" / "records"),
                                 pattern=Path(TEST_REL).name, top_level_dir=str(REPO))
    after_import = ambient_snapshot()
    from campaign_tool.records.understanding import document
    classifiers = document._CLASSIFIERS
    seen = {}

    class SentinelBefore(unittest.TestCase):
        def test_sentinel_before(self):
            seen["before_run"] = ambient_snapshot()

    class SentinelAfter(unittest.TestCase):
        def test_sentinel_after(self):
            seen["after_run"] = ambient_snapshot()
            self.assertEqual(seen["after_run"], seen["before_run"])
            self.assertIs(document._CLASSIFIERS, classifiers)

    suite = unittest.TestSuite([loader.loadTestsFromTestCase(SentinelBefore), discovered,
                                loader.loadTestsFromTestCase(SentinelAfter)])
    ids = []

    def walk(item):
        for sub in item:
            if isinstance(sub, unittest.TestSuite):
                walk(sub)
            else:
                ids.append(sub.id())
    walk(suite)
    result = unittest.TextTestRunner(stream=sys.stderr, verbosity=2).run(suite)
    report = {"ids": ids, "tests_run": result.testsRun,
              "failures": [t.id() for t, _ in result.failures], "errors": [t.id() for t, _ in result.errors],
              "skipped": [t.id() for t, _ in result.skipped],
              "import_time_ambient_unchanged": before_import == after_import,
              "import_time_diff_keys": sorted(k for k in before_import if before_import[k] != after_import.get(k)),
              "run_time_ambient_unchanged": seen.get("before_run") == seen.get("after_run") and "after_run" in seen,
              "classifiers_restored": document._CLASSIFIERS is classifiers}
    with open(result_path, "x", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
    return 0 if result.wasSuccessful() else 1


def phase_portable(row, manifest, head):
    cases = fixture_cases()
    expected = expected_test_names(cases)
    row.data["case_dimensions"] = case_dimensions(cases)
    cwd = row.dir / "unrelated-cwd"
    cwd.mkdir()
    code, report, _, leftovers = script_run(row, "absolute-script", cwd)
    check_full_pass(row, "absolute-script", code, report, expected)
    row.check("absolute-script:tmp_root_was_literal_argument",
              report is not None and report["tmp_root"] == str(row.dir / "absolute-script-tmp"),
              report and report["tmp_root"])
    row.check("absolute-script:no_tmp_residue", leftovers == [], leftovers)
    row.check("absolute-script:cwd_untouched", sorted(p.name for p in cwd.iterdir()) == [], None)
    tmp = row.dir / "discovery-tmp"
    tmp.mkdir()
    result = row.dir / "discovery-result.json"
    code, _, _ = row.run("discovery-isolated", [sys.executable, str(Path(__file__).resolve()), "--child",
                                                DISCOVERY_CHILD, "--child-result", str(result)],
                         cwd, env=child_env(tmp))
    report = json.loads(result.read_text(encoding="utf-8")) if result.is_file() else None
    row.check("discovery:exit_zero", code == 0, code)
    if row.check("discovery:result_present", report is not None):
        names = [short(i) for i in report["ids"]]
        row.check("discovery:sentinels_bracket_module",
                  names[:1] == ["SentinelBefore.test_sentinel_before"] and
                  names[-1:] == ["SentinelAfter.test_sentinel_after"], names[:1] + names[-1:])
        module_ids = report["ids"][1:-1]
        row.check("discovery:module_ids_are_ordinary_module",
                  all(i.startswith(TEST_MODULE + ".") for i in module_ids), len(module_ids))
        row.check("discovery:exact_frozen_test_set", set(names[1:-1]) == expected and len(names) == len(expected) + 2,
                  {"missing": sorted(expected - set(names[1:-1])), "extra": sorted(set(names[1:-1]) - expected)})
        row.check("discovery:no_fail_error_skip", not report["failures"] and not report["errors"] and
                  not report["skipped"], report["failures"] + report["errors"] + report["skipped"])
        row.check("discovery:import_time_ambient_unchanged", report["import_time_ambient_unchanged"],
                  report["import_time_diff_keys"])
        row.check("discovery:run_time_ambient_unchanged", report["run_time_ambient_unchanged"])
        row.check("discovery:classifiers_restored", report["classifiers_restored"])
    # Box-specific path inspection of authored bytes; needles come from runtime, not literals.
    runtime = manifest.get("runtime") or {}
    needles = sorted({str(Path.home()), manifest.get("write_root", ""), manifest.get("repo", ""),
                      str(REPO), *(str(v) for v in runtime.values() if isinstance(v, str) and v.startswith("/"))}
                     - {""})
    hits = {rel: [n for n in needles if n.encode() in (REPO / rel).read_bytes()] for rel in AUTHORED}
    row.check("authored_bytes_have_no_box_paths", not any(hits.values()), {"needle_count": len(needles), "hits": hits})
    clean_worktree(row, "post")


def phase_credentials(row, manifest, head):
    runtime = manifest.get("runtime") or {}
    expected_python = runtime.get("credential_scan_python3", "")
    row.check("interpreter_is_manifest_credential_scan_python3",
              os.path.abspath(sys.executable) == os.path.abspath(expected_python),
              {"sys.executable": sys.executable, "manifest": expected_python})
    report_path = row.dir / "detect-secrets-report.json"
    code, out, _ = row.run("detect-secrets", [runtime.get("detect_secrets", ""), "scan", "--all-files",
                                              "--exclude-files", "^.git/", "--no-verify"], REPO,
                           stdout_path=report_path)
    row.check("detect_secrets_exit_zero", code == 0, code)
    try:
        report = json.loads(out)
        results = report.get("results")
        row.check("detect_secrets_report_parseable", isinstance(results, dict),
                  {"files_with_hits": len(results) if isinstance(results, dict) else None,
                   "authored_hits": {rel: results.get(rel, []) for rel in AUTHORED} if isinstance(results, dict) else None,
                   "venv_paths_scanned": sorted(p for p in results if p.startswith(".venv")) if isinstance(results, dict) else None})
    except ValueError as exc:
        row.check("detect_secrets_report_parseable", False, str(exc))
    code, out, _ = row.run("check-secret-report", [sys.executable, str(REPO / "tools" / "check_secret_report.py"),
                                                   str(report_path)], REPO)
    row.check("check_secret_report_exit_zero", code == 0, out.strip().splitlines()[-1:] if out else None)
    clean_worktree(row, "post")


def phase_public_tree(row, manifest, head):
    runtime = manifest.get("runtime") or {}
    venv = REPO / ".venv"
    venv_ok = venv.is_symlink() and os.readlink(venv) == runtime.get("repo_venv")
    row.check("venv_link_is_manifest_bound", venv_ok, os.readlink(venv) if venv.is_symlink() else None)
    lines = clean_worktree(row, "pre-scan")
    code, out, _ = row.git("ls-files", ["ls-files", "-z", "--cached", "--others", "--exclude-standard"])
    inventory = sorted(set(out.split("\0")) - {""})
    row.check("authored_files_tracked", all(rel in inventory for rel in AUTHORED), None)
    row.check("only_untracked_is_venv_link", [line for line in lines if line.startswith("??")] in ([], ["?? .venv"]),
              lines)
    row.data["path_inventory"] = {"count": len(inventory), "sha256": sha256_bytes("\n".join(inventory).encode())}
    row.data["scanner_sha256"] = {rel: sha256_file(REPO / rel) for rel in SCANNER_FILES}
    clone = clone_at(row, "clone", head)
    tree = row.data["clones"]["clone"]["tree"]
    row.check("clone:exact_tree", tree == row.data["candidate"]["tree"], tree)
    row.check("clone:authored_hashes", all(sha256_file(clone / rel) == row.data["authored"][rel]["sha256"]
                                           for rel in AUTHORED), None)
    row.check("clone:no_dependency_links", not (clone / ".venv").exists() and not (clone / "node_modules").exists())
    row.check("clone:scanner_hashes", all(sha256_file(clone / rel) == row.data["scanner_sha256"][rel]
                                          for rel in SCANNER_FILES), None)
    code, out, err = row.run("committed-tree-check", [sys.executable, str(clone / "tools" / "check_public_tree.py")],
                             clone)
    try:
        parsed = json.loads(out)
    except ValueError:
        parsed = None
    row.check("committed_tree:exit_zero_json_empty_findings",
              code == 0 and isinstance(parsed, dict) and parsed.get("findings") == [],
              {"exit": code, "findings": parsed and parsed.get("findings"),
               "scanned_files": parsed and parsed.get("scanned_files")})
    code, out, err = row.run("in-place-informational", [sys.executable, str(REPO / "tools" / "check_public_tree.py")],
                             REPO)
    try:
        parsed = json.loads(out)
    except ValueError:
        parsed = None
    findings = parsed.get("findings") if isinstance(parsed, dict) else None
    if code == 0 and findings == []:
        outcome = "exit0_empty_findings"
    elif code == 1 and findings == [[".venv", 0, "symlink_not_scanned"]] and venv_ok:
        outcome = "exit1_only_manifest_verified_venv_symlink_not_scanned"
    else:
        outcome = "unrecognized"
    row.data["in_place_informational"] = {"label": "in_place_informational (INC-174)", "exit": code,
                                          "findings": findings, "outcome": outcome,
                                          "note": "informational only; the committed-tree clone result is the A5 pass"}
    row.check("in_place_informational_recognized", outcome != "unrecognized", outcome)
    c, after_head, _ = row.git("head-after", ["rev-parse", "HEAD"])
    row.check("candidate_head_unchanged_after", after_head.strip() == head, after_head.strip())
    clean_worktree(row, "post")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--phase", choices=PHASES)
    parser.add_argument("--source-manifest", type=Path)
    parser.add_argument("--out-base", type=Path)
    parser.add_argument("--child", choices=(DISCOVERY_CHILD,))
    parser.add_argument("--child-result", type=Path)
    args = parser.parse_args(argv)
    if args.child:
        return discovery_child(args.child_result)
    if not (args.phase and args.source_manifest and args.out_base):
        parser.error("--phase, --source-manifest and --out-base are required")
    out_base = args.out_base.resolve()
    if out_base == REPO or REPO in out_base.parents:
        parser.error("--out-base must be outside the repository")
    out_base.mkdir(exist_ok=True)
    row = Row(args.phase, out_base, sys.argv if argv is None else argv)
    try:
        manifest, head = load_manifest(row, args.source_manifest)
        if all(c["ok"] for c in row.checks):
            {"suite": phase_suite, "compatibility": phase_compatibility, "portable": phase_portable,
             "credentials": phase_credentials, "public-tree": phase_public_tree}[args.phase](row, manifest, head)
        else:
            row.check("identity_gate_blocked_phase", False, "phase not run because identity checks failed")
    except Exception as exc:  # recorded as a failed check, never a pass
        row.check("runner_exception", False, "%s: %s" % (type(exc).__name__, exc))
    return row.finish()


if __name__ == "__main__":
    raise SystemExit(main())
