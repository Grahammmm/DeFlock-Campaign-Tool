"""Bounded FC15 five-row recorder, authored by gpt-6.1-sol / openai-codex.

No product checker is reimplemented here. Child processes exercise unittest and
unchanged repository scanners. Evidence is append-only in fresh UUID roots.
Full repository discovery and exact-head CI remain the shipping operator's job.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import uuid

REPO = Path(__file__).resolve().parents[1]
TEST = "tests/records/test_fc15_row_header_cardinality.py"
FIXTURE = "tests/fixtures/fc15-row-header-cardinality/cases.json"
RUNNER = "tools/fc15_row_header_cardinality_acceptance.py"
AUTHORED = (TEST, FIXTURE, RUNNER)
CHECKERS = ("tools/check_secret_report.py", "tools/check_public_tree.py",
            "campaign_tool/records/public_scan.py")
SOURCES = ("campaign_tool/records/understanding/rows.py", "tests/test_understanding_rows.py")
SELECTORS = (
    "tests.test_understanding_rows.RowTests.test_exact_csv_contract_identity_and_states",
    "tests.test_understanding_rows.RowTests.test_text_header_order_mismatch_stays_unknown",
)
METHODS = ("test_one_matching", "test_zero_headers", "test_two_equal", "test_two_conflicting",
           "test_foreign_header", "test_preservation", "test_deciding_cardinality_control")


def need(condition, message):
    if not condition:
        raise ValueError(message)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def save(path, data):
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def git(*args, cwd=REPO):
    return subprocess.check_output(["git", *args], cwd=cwd).decode().strip()


def load_tests_module():
    spec = importlib.util.spec_from_file_location("fc15_cardinality", REPO / TEST)
    if spec is None or spec.loader is None:
        raise ValueError("missing_test_loader")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RecordingResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ids = []
        self.subcases = []

    def startTest(self, test):
        self.ids.append(test.id())
        super().startTest(test)

    def addSubTest(self, test, subtest, err):
        self.subcases.append({"id": subtest.id(), "passed": err is None})
        super().addSubTest(test, subtest, err)


def ambient():
    return (dict(os.environ), tempfile.tempdir, socket.socket, socket.create_connection,
            socket.getaddrinfo, sqlite3.connect, subprocess.Popen, os.system,
            sys.dont_write_bytecode)


def child(options):
    # No product import until the scoped sentinels and temporary-root redirect.
    sys.path.insert(0, str(REPO))
    module = load_tests_module()
    original = ambient()
    with module.offline_scope(options.temporary_root):
        within = ambient()
        loader = unittest.TestLoader()
        if options.child == "compatibility":
            suite = loader.loadTestsFromNames(SELECTORS)
        elif options.child == "peer":
            class Before(unittest.TestCase):
                def runTest(self):
                    self.assertEqual(ambient(), within)
                    with tempfile.TemporaryFile() as stream:
                        stream.write(b"inert-peer")
            class After(Before):
                pass
            # Discovery, import, setup, teardown all occur between the peers.
            class Discovery(unittest.TestSuite):
                def run(self, result, debug=False):
                    discovered = loader.discover(str(REPO / "tests/records"),
                        pattern=Path(TEST).name, top_level_dir=str(REPO))
                    return discovered.run(result, debug=debug)
            suite = unittest.TestSuite([Before(), Discovery(), After()])
        else:
            suite = loader.loadTestsFromTestCase(module.HeaderCardinalityTests)
        result = unittest.TextTestRunner(verbosity=2, resultclass=RecordingResult).run(suite)
        if not isinstance(result, RecordingResult):
            raise ValueError("wrong_result_recorder")
        need(ambient() == within, "ambient_state_changed_inside_suite")
    need(ambient() == original, "ambient_state_not_restored")
    report = {"tests": result.testsRun, "ids": result.ids, "subcases": result.subcases,
              "skips": len(result.skipped), "failures": len(result.failures),
              "errors": len(result.errors), "successful": result.wasSuccessful(),
              "ambient_unchanged": True, "cases": list(module.CASE_IDS)}
    save(options.report, report)
    return int(not result.wasSuccessful() or bool(result.skipped))


def snapshot(manifest, *, baseline=False):
    need(manifest["schema"] == "agentorange.source-manifest/v1", "manifest_schema")
    need(Path(manifest["repo"]).resolve() == REPO, "manifest_repo")
    need(REPO.parent == Path(manifest["write_root"]).resolve(), "manifest_write_root")
    head, tree = git("rev-parse", "HEAD"), git("rev-parse", "HEAD^{tree}")
    need(git("rev-parse", manifest["head"] + "^{tree}") == manifest["tree"], "initial_tree")
    need(git("merge-base", manifest["head"], head) == manifest["head"], "not_baseline_descendant")
    changed = git("diff", "--name-only", manifest["head"], head).splitlines()
    need(set(changed) <= set(AUTHORED), "exclusive_write_set")
    if baseline:
        need(head == manifest["head"] and tree == manifest["tree"], "baseline_identity")
    else:
        need(set(changed) == set(AUTHORED), "final_committed_files_required")
        need(not git("diff", "--name-only", "HEAD"), "uncommitted_tracked_bytes")
        need(not git("diff", "--cached", "--name-only", "HEAD"), "index_not_head")
        for name in AUTHORED:
            need(subprocess.check_output(["git", "show", head + ":" + name], cwd=REPO)
                 == (REPO / name).read_bytes(), "authored_head_bytes")
    untracked = git("ls-files", "--others", "--exclude-standard").splitlines()
    need(set(untracked) <= ({*AUTHORED, ".venv"} if baseline else {".venv"}), "untracked_scan_visible")
    link = REPO / ".venv"
    need(link.is_symlink() and link.resolve() == Path(manifest["runtime"]["repo_venv"]).resolve(),
         "manifest_venv_link")
    for name in (*CHECKERS, *SOURCES):
        need(subprocess.check_output(["git", "show", manifest["head"] + ":" + name], cwd=REPO)
             == (REPO / name).read_bytes(), "checker_changed")
    return {"head": head, "tree": tree, "status": git("status", "--porcelain=v1", "--untracked-files=all"),
            "scan_inventory": git("ls-files", "--cached", "--others", "--exclude-standard").splitlines(),
            "hashes": {name: sha((REPO / name).read_bytes()) for name in (*AUTHORED, *CHECKERS, *SOURCES)},
            "complete_diff_sha256": sha(subprocess.check_output(
                ["git", "diff", "--binary", manifest["head"], head], cwd=REPO))}


def run_row(options):
    manifest_path = options.source_manifest.resolve(strict=True)
    manifest_raw = manifest_path.read_bytes()
    manifest = json.loads(manifest_raw)
    out = options.out_base.resolve()
    need(out == REPO.parent / "out" and REPO not in out.parents, "output_root")
    # All staging/path/runtime checks precede product imports and child writers.
    need(options.baseline is False or options.phase == "compatibility", "baseline_phase")
    expected_python = (Path(manifest["runtime"]["credential_scan_python3"])
                       if options.phase == "credentials" else REPO / ".venv/bin/python3")
    need(Path(sys.executable).absolute() == expected_python.absolute(), "runtime_interpreter")
    before = snapshot(manifest, baseline=options.baseline)
    out.mkdir(exist_ok=True)
    row = out / (options.phase + "-" + str(uuid.uuid4()))
    row.mkdir(mode=0o700)
    temporary = row / "temporary"
    temporary.mkdir(mode=0o700)
    receipt = {"schema": "fc15-row-acceptance/v1", "phase": options.phase,
               "baseline": options.baseline, "argv": sys.argv, "cwd": str(Path.cwd()),
               "interpreter": sys.executable, "source_manifest_sha256": sha(manifest_raw),
               "identity": before, "children": [], "author": "gpt-6.1-sol/openai-codex",
               "review_required": True, "qualified": False}
    diff_path = row / "candidate.diff"
    diff_path.write_bytes(subprocess.check_output(
        ["git", "diff", "--binary", manifest["head"], before["head"]], cwd=REPO))
    need(sha(diff_path.read_bytes()) == before["complete_diff_sha256"], "diff_changed")
    receipt["complete_diff"] = str(diff_path)
    # Only an explicit, private child environment; the parent is never mutated.
    env = dict(os.environ, TMPDIR=str(temporary), TMP=str(temporary), TEMP=str(temporary),
               PYTHONDONTWRITEBYTECODE="1")

    def invoke(label, argv, cwd=REPO, allowed=(0,)):
        completed = subprocess.run([str(v) for v in argv], cwd=cwd, env=env,
                                   capture_output=True, timeout=180, shell=False)
        stdout, stderr = row / (label + ".stdout"), row / (label + ".stderr")
        stdout.write_bytes(completed.stdout)
        stderr.write_bytes(completed.stderr)
        receipt["children"].append({"label": label, "argv": [str(v) for v in argv],
            "cwd": str(cwd), "exit": completed.returncode, "stdout": str(stdout), "stderr": str(stderr),
            "stdout_sha256": sha(completed.stdout), "stderr_sha256": sha(completed.stderr)})
        need(completed.returncode in allowed, label + "_child_exit")
        return completed

    def recorded_suite(label, mode, cwd=REPO):
        report_path = row / (label + ".json")
        invoke(label, [sys.executable, Path(__file__).resolve(), "--child", mode,
                       "--temporary-root", temporary, "--report", report_path], cwd=cwd)
        data = json.loads(report_path.read_text())
        need(data["successful"] and data["skips"] == 0 and data["ambient_unchanged"], "incomplete_suite")
        expected = 2 if mode == "compatibility" else (9 if mode == "peer" else 7)
        need(data["tests"] == expected and len(data["ids"]) == expected, "missing_tests")
        if mode == "compatibility":
            need(data["ids"] == list(SELECTORS), "compatibility_ids")
            need(len(data["subcases"]) == 4, "compatibility_subcases")
            expected_subcases = {SELECTORS[1] + " (kind=" + repr(kind) + ", labels=" + repr(labels) + ")"
                for kind in ("text_line", "pdf_page")
                for labels in (["When", "ID", "Reason", "User"], ["ID", "When", "User", "Reason"])}
            need({s["id"] for s in data["subcases"]} == expected_subcases, "compatibility_subcase_ids")
        else:
            need({i.rsplit(".", 1)[-1] for i in data["ids"] if ".HeaderCardinalityTests." in i}
                 == set(METHODS), "missing_frozen_test")
            need(len(data["subcases"]) == 12 and all(s["passed"] for s in data["subcases"]), "missing_subcase")
            need(set(data["cases"]) == {"one_matching", "zero_headers", "two_equal",
                 "two_conflicting_matching_first", "two_conflicting_matching_last", "foreign_header"}, "missing_case")
            expected_suffixes = {"test_preservation (case=" + repr(case) + ")" for case in data["cases"]}
            expected_suffixes.update("test_two_conflicting (case=" + repr(case) + ")" for case in (
                "two_conflicting_matching_first", "two_conflicting_matching_last"))
            expected_suffixes.update("test_deciding_cardinality_control (control=" + repr(control) + ")"
                for control in ("weakened_one_matching_green", "weakened_two_equal_named_assertion_fails",
                                "restored_one_matching_green", "restored_two_equal_green"))
            need({s["id"].split(".HeaderCardinalityTests.", 1)[1] for s in data["subcases"]}
                 == expected_suffixes, "missing_subcase_ids")
        receipt.setdefault("test_reports", {})[label] = {"path": str(report_path), "sha256": sha(report_path.read_bytes()), **data}

    try:
        if options.phase == "suite":
            recorded_suite("suite", "suite")
        elif options.phase == "compatibility":
            recorded_suite("compatibility", "compatibility")
            if not options.baseline:
                baselines = []
                for path in out.glob("compatibility-*/receipt.json"):
                    item = json.loads(path.read_text())
                    if item.get("baseline") and item.get("exit") == 0 and item.get("identity", {}).get("head") == manifest["head"]:
                        need(item["source_manifest_sha256"] == sha(manifest_raw), "baseline_manifest_changed")
                        for entry in item["children"]:
                            need(sha(Path(entry["stdout"]).read_bytes()) == entry["stdout_sha256"], "baseline_stdout_changed")
                            need(sha(Path(entry["stderr"]).read_bytes()) == entry["stderr_sha256"], "baseline_stderr_changed")
                        baselines.append({"path": str(path), "sha256": sha(path.read_bytes())})
                need(bool(baselines), "baseline_compatibility_owed")
                receipt["baseline_receipts"] = baselines
                receipt["measured_behavior_delta"] = "none: exact two selectors green at baseline and final"
        elif options.phase == "portable":
            cwd = row / "unrelated-cwd"
            cwd.mkdir()
            completed = invoke("absolute-script", [sys.executable, REPO / TEST, "--temporary-root", temporary], cwd=cwd)
            text = completed.stderr.decode()
            need("Ran 7 tests" in text and text.rstrip().endswith("OK") and "skipped" not in text,
                 "portable_script_incomplete")
            recorded_suite("portable-matrix", "suite", cwd=cwd)
            recorded_suite("peer-discovery", "peer", cwd=cwd)
            for name in AUTHORED:
                text = (REPO / name).read_text()
                need(re.search(r"/(?:home|Users|workspace)/", text) is None, "box_specific_path")
        elif options.phase == "credentials":
            scanner = manifest["runtime"]["detect_secrets"]
            need(Path(scanner).read_text().splitlines()[0] == "#!" + str(expected_python),
                 "scanner_interpreter")
            completed = invoke("detect-secrets", [scanner, "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"])
            json.loads(completed.stdout)
            invoke("check-secret-report", [sys.executable, REPO / "tools/check_secret_report.py", row / "detect-secrets.stdout"])
        elif options.phase == "public-tree":
            clone = row / "committed-tree"
            invoke("clone", ["git", "clone", "--local", "--no-hardlinks", "--no-checkout",
                             "--no-recurse-submodules", REPO, clone])
            invoke("checkout", ["git", "checkout", "--detach", before["head"]], cwd=clone)
            need(git("rev-parse", "HEAD", cwd=clone) == before["head"], "clone_head")
            need(git("rev-parse", "HEAD^{tree}", cwd=clone) == before["tree"], "clone_tree")
            for name in (*AUTHORED, *CHECKERS, *SOURCES):
                need(sha((clone / name).read_bytes()) == before["hashes"][name], "clone_hash")
            completed = invoke("committed-tree-scan", [sys.executable, clone / "tools/check_public_tree.py"], cwd=clone)
            report = json.loads(completed.stdout)
            need(report["findings"] == [] and report["scanned_files"] == len(before["scan_inventory"]), "clone_findings_or_count")
            completed = invoke("in_place_informational", [sys.executable, REPO / "tools/check_public_tree.py"], allowed=(0, 1))
            report = json.loads(completed.stdout)
            need((completed.returncode == 0 and report["findings"] == []) or
                 (completed.returncode == 1 and report["findings"] == [[".venv", 0, "symlink_not_scanned"]]),
                 "unrecognized_in_place_result")
        need(snapshot(manifest, baseline=options.baseline) == before, "candidate_changed_during_row")
        need(manifest_path.read_bytes() == manifest_raw, "manifest_changed_during_row")
        receipt["exit"] = 0
    except Exception as error:
        receipt["exit"] = 1
        receipt["error"] = type(error).__name__ + ": " + str(error)
    # subprocess.run has joined every child writer before hashing or sealing.
    for entry in receipt["children"]:
        need(sha(Path(entry["stdout"]).read_bytes()) == entry["stdout_sha256"], "late_stdout_change")
        need(sha(Path(entry["stderr"]).read_bytes()) == entry["stderr_sha256"], "late_stderr_change")
    save(row / "receipt.json", receipt)
    print(json.dumps({"phase": options.phase, "exit": receipt["exit"], "head": before["head"],
                      "receipt": str(row / "receipt.json"), "sha256": sha((row / "receipt.json").read_bytes()),
                      "error": receipt.get("error")}))
    return receipt["exit"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("suite", "compatibility", "portable", "credentials", "public-tree"))
    parser.add_argument("--source-manifest", type=Path)
    parser.add_argument("--out-base", type=Path)
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--child", choices=("suite", "compatibility", "peer"))
    parser.add_argument("--temporary-root", type=Path)
    parser.add_argument("--report", type=Path)
    options = parser.parse_args()
    if options.child:
        need(options.temporary_root is not None and options.report is not None, "child_arguments")
        return child(options)
    need(options.phase and options.source_manifest and options.out_base, "row_arguments")
    need(Path.cwd().resolve() == REPO, "acceptance_cwd")
    return run_row(options)


if __name__ == "__main__":
    raise SystemExit(main())
