"""FC13-MAPPING-CONSISTENCY acceptance: five separate offline rows, fresh receipts each.

Usage (one phase per invocation):
    <interpreter> tools/fc13_mapping_consistency_acceptance.py --phase PHASE
        --source-manifest <write-root>/SOURCE-MANIFEST.json --out-base <write-root>/out

Every invocation writes a new UUID directory below --out-base (outside the repo),
records argv/cwd/interpreter/candidate identity/raw child output with hashes and
seals evidence-manifest.json only after every child process has exited. Exit 0
is REVIEW REQUIRED machine evidence, never semantic, legal or release acceptance.
"""
import argparse
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import traceback
import unittest
from unittest import mock
import uuid

REPO = Path(__file__).resolve().parents[1]
PRODUCT = "campaign_tool/records/detectors/pipeline.py"
EXISTING_TEST = "tests/records/test_structured_detector_pipeline.py"
NEW_TEST = "tests/records/test_fc13_mapping_consistency.py"
FIXTURE = "tests/fixtures/fc13-mapping-consistency/cases.json"
RUNNER = "tools/fc13_mapping_consistency_acceptance.py"
WRITE_SET = (PRODUCT, EXISTING_TEST, NEW_TEST, FIXTURE, RUNNER)
# Read-only sources whose bytes must equal the pinned base blob.
UNCHANGED = ("campaign_tool/records/understanding/rows.py", "campaign_tool/records/detectors/core.py",
             "campaign_tool/records/detectors/persistence.py", "tests/records/test_detectors.py",
             "tests/test_understanding_rows.py", "tools/check_secret_report.py",
             "tools/check_public_tree.py", "campaign_tool/records/public_scan.py")
SELECTORS = ("tests.records.test_structured_detector_pipeline", "tests.test_understanding_rows")
CORRECTED_TEST_ID = ("tests.records.test_structured_detector_pipeline.StructuredPipelineTests."
                     "test_missing_redacted_rows_do_not_create_subset_rates")
# Box-specific path shapes; written so this source does not match itself.
HOST_LITERAL = re.compile(r"/(?:home|Users|root|tmp|var/folders)/|agent[-]work|[.]hermes")
PHASES = ("suite", "compatibility", "portable", "credentials", "public-tree")


class RowFailure(Exception):
    pass


def check(condition, reason):
    if not condition:
        raise RowFailure(reason)


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    return sha256_bytes(Path(path).read_bytes())


def no_network(*_args, **_kwargs):
    raise RuntimeError("FC13 acceptance is offline")


def checked_dir(path):
    path = Path(path)
    check(path.is_absolute() and str(path) == os.path.normpath(str(path)), "non-canonical path: " + str(path))
    for part in (path, *path.parents):
        check(not part.is_symlink(), "symlink in path ancestry: " + str(part))
    return path


def last_tagged(text, tag):
    lines = [line[len(tag) + 1:] for line in text.splitlines() if line.startswith(tag + " ")]
    check(lines, "missing " + tag + " line")
    return json.loads(lines[-1])


# ---------------------------------------------------------------- child modes

def _child_setup(root, temp_root):
    """Offline guards, temp redirect and import root BEFORE any product import."""
    root, temp_root = Path(root), Path(temp_root)
    check(temp_root.is_dir() and not temp_root.is_symlink(), "child temp root missing")
    tempfile.tempdir = str(temp_root)
    sys.dont_write_bytecode = True
    here = Path(__file__).resolve().parent
    sys.path[:] = [entry for entry in sys.path if not entry or Path(entry).resolve() != here]
    sys.path.insert(0, str(root))
    return root


def _ambient():
    return (dict(os.environ), tempfile.tempdir, tuple(sys.path), sys.dont_write_bytecode,
            socket.socket, socket.create_connection, socket.getaddrinfo, subprocess.Popen, os.system)


def _run_suite(suite):
    ran = []

    class Recording(unittest.TextTestResult):
        def startTest(self, test):
            ran.append(test.id())
            super().startTest(test)

    result = unittest.TextTestRunner(stream=sys.stderr, verbosity=2, resultclass=Recording).run(suite)
    return result, ran


def child(mode, root, temp_root):
    root = _child_setup(root, temp_root)
    with ExitStack() as guards:
        for owner, name in ((socket, "socket"), (socket, "create_connection"), (socket, "getaddrinfo"),
                            (subprocess, "Popen"), (os, "system")):
            guards.enter_context(mock.patch.object(owner, name, no_network))
        summary = {"mode": mode, "root": str(root)}
        if mode == "selectors":
            suite = unittest.defaultTestLoader.loadTestsFromNames(list(SELECTORS))
            result, ran = _run_suite(suite)
        else:
            before = _ambient()
            calls = []

            def sentinel():
                if _ambient() != before:
                    raise AssertionError("FC13 discovery contaminated ambient state")
                calls.append(len(calls))

            sentinel()
            discovered = unittest.defaultTestLoader.discover(str(root / "tests" / "records"),
                                                             pattern=Path(NEW_TEST).name,
                                                             top_level_dir=str(root))
            sentinel()
            suite = unittest.TestSuite([unittest.FunctionTestCase(sentinel, description="sentinel-before"),
                                        discovered,
                                        unittest.FunctionTestCase(sentinel, description="sentinel-after")])
            result, ran = _run_suite(suite)
            summary["sentinel_calls"] = len(calls)
            summary["ambient_unchanged_after"] = _ambient() == before
        import campaign_tool
        summary["imported_package"] = str(Path(campaign_tool.__file__).resolve())
    summary.update(testsRun=result.testsRun, failures=len(result.failures), errors=len(result.errors),
                   skips=len(result.skipped), test_ids=ran,
                   failed_ids=sorted(test.id() for test, _ in result.failures),
                   error_ids=sorted(test.id() for test, _ in result.errors))
    print("FC13_CHILD " + json.dumps(summary, sort_keys=True), flush=True)
    return 0 if result.wasSuccessful() and not result.skipped else 1


# ---------------------------------------------------------------- rows

class Row:
    def __init__(self, args):
        self.phase = args.phase
        self.manifest_path = checked_dir(Path(args.source_manifest))
        self.out_base = checked_dir(Path(args.out_base))
        self.manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        self.commands, self.observations = [], {}
        self.identity, self.status_before = None, None
        check(self.manifest.get("schema") == "agentorange.source-manifest/v1", "source manifest schema")
        write_root = checked_dir(Path(self.manifest["write_root"]))
        check(Path(self.manifest["repo"]) == REPO and REPO.parent == write_root, "staged repo identity")
        check(self.manifest_path.parent == write_root, "source manifest outside write root")
        check(self.out_base == write_root / "out", "out-base must be the write root out directory")
        check(re.fullmatch("[0-9a-f]{40}", self.manifest["head"]) is not None, "manifest head grammar")
        check(re.fullmatch("[0-9a-f]{40}", self.manifest["tree"]) is not None, "manifest tree grammar")
        self.out_base.mkdir(mode=0o700, exist_ok=True)
        self.root = self.out_base / (self.phase + "-" + str(uuid.uuid4()))
        self.root.mkdir(mode=0o700)
        self.temp = self.root / "temp"
        self.temp.mkdir(mode=0o700)
        tempfile.tempdir = str(self.temp)
        sys.dont_write_bytecode = True

    # -- process plumbing
    def run(self, label, argv, cwd=REPO, expect=(0,), timeout=300):
        check(type(argv) is list and all(type(item) is str for item in argv), "argv must be literal strings")
        check(all(entry["label"] != label for entry in self.commands), "duplicate label " + label)
        done = subprocess.run(argv, cwd=str(cwd), shell=False, capture_output=True, timeout=timeout)
        out, err = self.root / (label + ".stdout"), self.root / (label + ".stderr")
        out.write_bytes(done.stdout)
        err.write_bytes(done.stderr)
        self.commands.append({"label": label, "argv": argv, "cwd": str(cwd), "interpreter": argv[0],
                              "exit": done.returncode, "stdout": out.name, "stdout_sha256": sha256_file(out),
                              "stderr": err.name, "stderr_sha256": sha256_file(err)})
        check(done.returncode in expect, label + " exited " + str(done.returncode))
        return done.stdout.decode()

    def git(self, label, *args, cwd=REPO, expect=(0,)):
        return self.run(label, ["git", *args], cwd=cwd, expect=expect).strip()

    def raw_blob(self, label, commit, relative):
        done = subprocess.run(["git", "show", commit + ":" + relative], cwd=str(REPO), shell=False,
                              capture_output=True, timeout=60)
        self.commands.append({"label": label, "argv": ["git", "show", commit + ":" + relative],
                              "cwd": str(REPO), "interpreter": "git", "exit": done.returncode,
                              "stdout_sha256": sha256_bytes(done.stdout), "stderr_sha256": sha256_bytes(done.stderr)})
        check(done.returncode == 0, label + " failed")
        return done.stdout

    def clone(self, label, commit):
        target = self.root / label
        self.run(label + "-clone", ["git", "clone", "--local", "--no-hardlinks", "--no-checkout",
                                    "--no-recurse-submodules", str(REPO), str(target)])
        self.run(label + "-checkout", ["git", "checkout", "--detach", commit], cwd=target)
        check(self.git(label + "-head", "rev-parse", "HEAD", cwd=target) == commit, label + " clone head")
        check(not (target / ".venv").exists(), label + " clone must not carry the runtime link")
        return target

    # -- shared identity checks
    def prepare(self):
        m = self.manifest
        base = m["head"]
        self.identity = {"head": self.git("head", "rev-parse", "HEAD"),
                         "tree": self.git("tree", "rev-parse", "HEAD^{tree}"), "base": base}
        check(self.git("base-tree", "rev-parse", base + "^{tree}") == m["tree"], "base tree differs from manifest")
        self.git("base-is-ancestor", "merge-base", "--is-ancestor", base, "HEAD")
        check(self.identity["head"] != base, "final acceptance requires the signed-off candidate head")
        check(self.git("remote", "remote", "get-url", "origin") == m["remote"], "remote differs from manifest")
        venv = REPO / ".venv"
        check(venv.is_symlink() and os.path.realpath(venv) == os.path.realpath(m["runtime"]["repo_venv"]),
              "runtime link differs from manifest")
        wanted = m["runtime"]["credential_scan_python3"] if self.phase == "credentials" else str(venv / "bin" / "python3")
        check(os.path.abspath(sys.executable) == wanted, "row interpreter must be " + wanted)
        self.status_before = self.git("status-before", "status", "--porcelain=v1", "--untracked-files=all")
        check(self.status_before == "", "candidate must be committed and clean")
        check(self.git("index-vs-head", "diff", "--cached", "--name-only") == "", "index differs from HEAD")
        changed = set(self.git("diff-names", "diff", "--name-only", base, "HEAD").splitlines())
        check(changed == set(WRITE_SET), "diff must equal the exclusive write set: " + ",".join(sorted(changed)))
        commits = self.git("commits", "rev-list", "--reverse", base + "..HEAD").splitlines()
        signoffs = {}
        for index, commit in enumerate(commits):
            body = self.git("commit-body-" + str(index), "log", "-1", "--format=%B", commit)
            check("Signed-off-by:" in body, "commit lacks Signed-off-by: " + commit)
            signoffs[commit] = body
        self.observations["commits"] = signoffs
        self.observations["authored_sha256"] = {path: sha256_file(REPO / path) for path in WRITE_SET}
        self.observations["base_authored_sha256"] = {}
        for index, path in enumerate((PRODUCT, EXISTING_TEST)):
            self.observations["base_authored_sha256"][path] = sha256_bytes(
                self.raw_blob("base-blob-" + str(index), base, path))
        self.observations["unchanged_sha256"] = {}
        for index, path in enumerate(UNCHANGED):
            original = self.raw_blob("unchanged-blob-" + str(index), base, path)
            check(sha256_file(REPO / path) == sha256_bytes(original), "read-only source drift: " + path)
            self.observations["unchanged_sha256"][path] = sha256_bytes(original)
        self.observations["source_manifest_sha256"] = sha256_file(self.manifest_path)
        self.observations["fixture_sha256"] = sha256_file(REPO / FIXTURE)
        self.observations["complete_diff_sha256"] = sha256_bytes(
            self.run("complete-diff", ["git", "diff", "--binary", base, "HEAD"]).encode())

    # -- A1
    def suite(self):
        spec = json.loads((REPO / FIXTURE).read_text(encoding="utf-8"))
        cases = [case["id"] for case in spec["cases"]]
        conflicts = sorted(case["id"] for case in spec["cases"] if case["expect"]["outcome"] == "conflict")
        names = {case: "test_case_" + case.replace("-", "_") for case in cases}
        final = last_tagged(self.run("final-matrix", [sys.executable, str(REPO / NEW_TEST),
                                                      "--temp-root", str(self.temp)]), "FC13_SUMMARY")
        check(final["failures"] == final["errors"] == final["skips"] == 0, "final matrix not green")
        check(final["testsRun"] == len(cases) + 2, "final matrix test count")
        check(sorted(final["case_ids"]) == sorted(cases), "case ids differ from frozen fixture")
        check(final["dimensions"] == spec["dimensions"], "dimensions differ from frozen fixture")
        ran = {test.rsplit(".", 1)[-1] for test in final["test_ids"]}
        check(set(names.values()) <= ran, "frozen case missing from run")
        check({"test_fixture_matrix_is_complete", "test_negative_control_disables_only_record_comparison"} <= ran,
              "matrix or negative-control test missing")
        check(final["pipeline_path"] == str(REPO / PRODUCT), "final matrix imported another product")
        control = last_tagged((self.root / "final-matrix.stdout").read_text(), "FC13_CONTROL")
        expectation = spec["negative_control"]
        check(control["control"] == expectation["id"] and control["case"] == expectation["case"], "control identity")
        check(control["mutant"]["evaluated"] == expectation["mutant_expectation"]["evaluated"] and
              control["mutant"]["hit_count"] == expectation["mutant_expectation"]["hit_count"],
              "mutant did not evaluate the duplicated source twice")
        check(expectation["assertion_marker"] in control["killed_assertion"] and
              spec["conflict_reason"] in control["killed_assertion"], "mutant not killed by the deciding assertion")
        check(control["restored"] == {name: "passed" for name in expectation["restored_positive_cases"]},
              "restored positives did not pass")
        self.observations["final_matrix"] = final
        self.observations["negative_control"] = control
        # Genuine pre-fix regression: the final frozen test/fixture bytes against the
        # pinned base product, in a row-local committed-tree clone.
        base = self.manifest["head"]
        clone = self.clone("prefix-base", base)
        for relative in (NEW_TEST, FIXTURE):
            target = clone / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((REPO / relative).read_bytes())
        check(sha256_file(clone / PRODUCT) == self.observations["base_authored_sha256"][PRODUCT],
              "pre-fix clone product is not the pinned base")
        red = last_tagged(self.run("prefix-base-matrix", [sys.executable, str(clone / NEW_TEST),
                                                          "--temp-root", str(self.temp), "-k", "test_case_"],
                                   cwd=clone, expect=(1,)), "FC13_SUMMARY")
        check(red["pipeline_path"] == str(clone / PRODUCT), "pre-fix run imported the candidate product")
        check(red["pipeline_sha256"] == self.observations["base_authored_sha256"][PRODUCT], "pre-fix product hash")
        check(red["testsRun"] == len(cases) and red["errors"] == 0 and red["skips"] == 0, "pre-fix run shape")
        failed = sorted(test.rsplit(".", 1)[-1] for test in red["failed_ids"])
        check(failed == sorted(names[case] for case in conflicts),
              "pre-fix failures must be exactly the frozen conflict cases")
        self.observations["prefix_base_red"] = {"summary": red, "expected_failed_cases": conflicts,
                                                "note": "expected pre-fix failure; not final acceptance"}

    # -- A2
    def selectors(self, label, root, expect=(0,)):
        out = self.run(label, [sys.executable, str(REPO / RUNNER), "--child", "selectors",
                               "--root", str(root), "--temp-root", str(self.temp)], expect=expect)
        summary = last_tagged(out, "FC13_CHILD")
        check(summary["imported_package"] == str(root / "campaign_tool" / "__init__.py"), label + " import root")
        check(summary["skips"] == 0 and summary["errors"] == 0, label + " skipped or errored")
        return summary

    def compatibility(self):
        final = self.selectors("final-selectors", REPO)
        check(final["failures"] == 0 and final["testsRun"] > 0, "final selectors not green")
        base = self.manifest["head"]
        baseline_root = self.clone("baseline", base)
        baseline = self.selectors("baseline-selectors", baseline_root)
        check(baseline["failures"] == 0, "baseline selectors not green")
        check(sorted(baseline["test_ids"]) == sorted(final["test_ids"]), "selector test inventory changed")
        # Intentional delta: the unmodified base test file against the candidate product
        # fails only in the one admitted fixture correction.
        delta_root = self.clone("candidate-with-base-test", self.identity["head"])
        (delta_root / EXISTING_TEST).write_bytes(self.raw_blob("base-existing-test", base, EXISTING_TEST))
        delta = self.selectors("candidate-with-base-test-selectors", delta_root, expect=(1,))
        check(delta["failed_ids"] == [CORRECTED_TEST_ID], "behavior delta beyond the admitted fixture correction")
        diff = self.run("existing-test-diff", ["git", "diff", base, "HEAD", "--", EXISTING_TEST])
        self.observations.update(final_selectors=final, baseline_selectors=baseline,
                                 intentional_delta={"summary": delta, "existing_test_diff": diff,
                                                    "admitted": "single fixture correction in " + CORRECTED_TEST_ID},
                                 owed=["mandatory full discovery (operator-run)", "exact-head required CI"])

    # -- A3
    def portable(self):
        elsewhere = self.root / "unrelated-cwd"
        elsewhere.mkdir(mode=0o700)
        script = last_tagged(self.run("portable-script", [sys.executable, str(REPO / NEW_TEST),
                                                          "--temp-root", str(self.temp)], cwd=elsewhere),
                             "FC13_SUMMARY")
        spec = json.loads((REPO / FIXTURE).read_text(encoding="utf-8"))
        check(script["failures"] == script["errors"] == script["skips"] == 0, "portable script not green")
        check(script["testsRun"] == len(spec["cases"]) + 2, "portable script count")
        discovery_out = self.run("portable-discovery", [sys.executable, str(REPO / RUNNER), "--child", "discovery",
                                                        "--root", str(REPO), "--temp-root", str(self.temp)],
                                 cwd=elsewhere)
        discovery = last_tagged(discovery_out, "FC13_CHILD")
        check(discovery["failures"] == discovery["errors"] == discovery["skips"] == 0, "discovery not green")
        check(discovery["testsRun"] == script["testsRun"] + 2, "discovery coverage differs from script mode")
        check(discovery["sentinel_calls"] == 4 and discovery["ambient_unchanged_after"] is True,
              "discovery-time ambient contamination")
        tail = sorted(test.rsplit(".", 1)[-1] for test in discovery["test_ids"] if ".test_" in test)
        head = sorted(test.rsplit(".", 1)[-1] for test in script["test_ids"])
        check(tail == head, "discovered test ids differ from script mode")
        findings = {path: HOST_LITERAL.findall((REPO / path).read_text(encoding="utf-8"))
                    for path in (NEW_TEST, FIXTURE, RUNNER)}
        check(not any(findings.values()), "box-specific literal in authored file")
        self.observations.update(portable_script=script, portable_discovery=discovery,
                                 host_literal_findings=findings)

    # -- A4
    def credentials(self):
        runtime = self.manifest["runtime"]
        scanner = Path(runtime["detect_secrets"])
        check(scanner.is_file() and scanner.parent == Path(runtime["credential_scan_python3"]).parent,
              "detect-secrets is not in the credential scan runtime")
        links = []
        for directory, dirs, files in os.walk(REPO, followlinks=False):
            if Path(directory) == REPO and ".git" in dirs:
                dirs.remove(".git")
            links += [str((Path(directory) / name).relative_to(REPO)) for name in dirs + files
                      if (Path(directory) / name).is_symlink()]
        check(links == [".venv"], "unexpected symlink inside scanner boundary: " + ",".join(links))
        report = self.run("detect-secrets", [str(scanner), "scan", "--all-files", "--exclude-files", "^.git/",
                                             "--no-verify"], timeout=1500)
        parsed = json.loads(report)
        check(isinstance(parsed.get("results"), dict) and parsed.get("plugins_used"), "incomplete scanner report")
        report_path = self.root / "detect-secrets.stdout"
        self.run("check-secret-report", [sys.executable, str(REPO / "tools" / "check_secret_report.py"),
                                         str(report_path)])
        self.observations.update(symlinks_not_traversed=links, scanned_result_paths=sorted(parsed["results"]),
                                 report=report_path.name)

    # -- A5
    def public_tree(self):
        visible = sorted(set(self.git("scan-visible", "ls-files", "-z", "--cached", "--others",
                                      "--exclude-standard").split("\0")) - {""})
        tracked = sorted(set(self.git("tracked", "ls-files", "-z").split("\0")) - {""})
        check(set(visible) - set(tracked) <= {".venv"}, "untracked scan-visible file")
        for path in WRITE_SET:
            check(path in tracked, "authored file not tracked: " + path)
        clone = self.clone("committed-tree", self.identity["head"])
        check(self.git("committed-tree-tree", "rev-parse", "HEAD^{tree}", cwd=clone) == self.identity["tree"],
              "committed clone tree")
        check(self.git("committed-tree-status", "status", "--porcelain", "--untracked-files=all", cwd=clone) == "",
              "committed clone not clean")
        for path, digest in {**self.observations["authored_sha256"], **self.observations["unchanged_sha256"]}.items():
            check(sha256_file(clone / path) == digest, "clone bytes differ: " + path)
        clean = json.loads(self.run("committed-tree-scan", [sys.executable, str(clone / "tools" / "check_public_tree.py")],
                                    cwd=clone))
        check(clean.get("findings") == [] and clean.get("scanned_files") == len(tracked),
              "committed public-tree scan not clean and complete")
        info_text = self.run("in-place-informational", [sys.executable, str(REPO / "tools" / "check_public_tree.py")],
                             expect=(0, 1))
        info = json.loads(info_text)
        exit_code = self.commands[-1]["exit"]
        recognized = ((exit_code == 0 and info.get("findings") == []) or
                      (exit_code == 1 and info.get("findings") == [[".venv", 0, "symlink_not_scanned"]]))
        check(recognized and info.get("scanned_files") == len(tracked), "unrecognized in-place outcome")
        self.observations.update(scan_visible=visible, tracked_count=len(tracked), committed_tree_scan=clean,
                                 in_place_informational_INC_174={"exit": exit_code, "result": info})

    # -- receipts
    def finish(self, error):
        if self.identity is not None:
            try:
                check(self.git("head-after", "rev-parse", "HEAD") == self.identity["head"], "HEAD moved during row")
                check(self.git("status-after", "status", "--porcelain=v1", "--untracked-files=all")
                      == self.status_before, "candidate status changed during row")
                if "authored_sha256" in self.observations:
                    check({path: sha256_file(REPO / path) for path in WRITE_SET}
                          == self.observations["authored_sha256"], "authored bytes changed during row")
            except Exception:
                error = (error or "") + "\nPOST-ROW RECHECK FAILED\n" + traceback.format_exc()
        receipt = {"schema": "fc13-mapping-consistency-row/v1", "phase": self.phase, "row_root": str(self.root),
                   "argv": sys.argv, "cwd": os.getcwd(), "interpreter": sys.executable,
                   "candidate": self.identity, "commands": self.commands, "observations": self.observations,
                   "exit": 1 if error else 0, "error": error,
                   "limits": ["REVIEW REQUIRED: machine evidence only, no semantic/legal/human acceptance",
                              "Scoped local rows, not full repository discovery or exact-head CI (both OWED)",
                              "No push, PR, merge, deploy, publication or hold change"]}
        (self.root / "receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
        # All children have exited; seal the complete file set.
        files = {}
        for path in sorted(self.root.rglob("*")):
            check(not path.is_symlink(), "symlink in evidence: " + str(path))
            if path.is_file():
                files[str(path.relative_to(self.root))] = {"sha256": sha256_file(path), "bytes": path.stat().st_size}
        seal = self.root / "evidence-manifest.json"
        seal.write_text(json.dumps({"schema": "fc13-evidence-manifest/v1", "candidate": self.identity,
                                    "phase": self.phase, "files": files}, indent=2, sort_keys=True) + "\n")
        present = {str(p.relative_to(self.root)) for p in self.root.rglob("*") if p.is_file() and p != seal}
        if present != set(files) or any(sha256_file(self.root / k) != v["sha256"] for k, v in files.items()):
            receipt["exit"] = 1
        print(json.dumps({"phase": self.phase, "exit": receipt["exit"], "candidate": self.identity,
                          "row_root": str(self.root), "evidence_manifest_sha256": sha256_file(seal),
                          "error": error}, sort_keys=True))
        return receipt["exit"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--phase", choices=PHASES)
    parser.add_argument("--source-manifest")
    parser.add_argument("--out-base")
    parser.add_argument("--child", choices=("selectors", "discovery"))
    parser.add_argument("--root")
    parser.add_argument("--temp-root")
    args = parser.parse_args(argv)
    if args.child:
        check(args.root and args.temp_root, "child requires --root and --temp-root")
        return child(args.child, args.root, args.temp_root)
    check(args.phase and args.source_manifest and args.out_base, "--phase, --source-manifest, --out-base required")
    with ExitStack() as guards:
        for name in ("socket", "create_connection", "getaddrinfo"):
            guards.enter_context(mock.patch.object(socket, name, no_network))
        row = Row(args)
        error = None
        try:
            row.prepare()
            getattr(row, args.phase.replace("-", "_"))()
        except Exception:
            error = traceback.format_exc()
        return row.finish(error)


if __name__ == "__main__":
    raise SystemExit(main())
