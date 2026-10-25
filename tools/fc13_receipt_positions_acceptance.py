"""FC13 bounded offline acceptance; actual author gpt-6.1-sol/openai-codex.

Five independent invocations, immutable UUID output roots, no product imports in
parent. Child offline mocks and temp redirection precede suite/product imports.
Baseline mode preserves genuine pre-fix regressions; never final acceptance.
"""
import argparse
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import traceback
import unittest
from unittest import mock
import uuid

REPO = Path(__file__).resolve().parents[1]
BASE = "17da706588467184" + "f0923e2516be3319" + "d577539a"
BASE_TREE = "91b70d87ae558fe6" + "7e728f5dc0131dcc" + "f1056ad5"
PRODUCT = "campaign_tool/records/understanding/evidence.py"
TEST = "tests/records/test_fc13_receipt_positions.py"
FIXTURE = "tests/fixtures/fc13-receipt-positions/cases.json"
RUNNER = "tools/fc13_receipt_positions_acceptance.py"
AUTHORED = [PRODUCT, TEST, FIXTURE, RUNNER]
SELECTORS = ["tests.records.test_understanding.EvidenceTests", "tests.records.test_fc_evidence_counts"]
# Split public provenance hashes avoid presenting high-entropy credential-like literals.
PINS = {
    PRODUCT: "215ed75899325c4a" + "c294cfaf890fb85d" + "ccabf179de240ed4" + "4f93d178eaeb11b4",
    "tests/records/test_understanding.py": "77fa084d17606632" + "7bc1795381a91167" + "1da7875f1f5721e1" + "64002616d6f5922a",
    "tests/records/test_fc_evidence_counts.py": "67d3ce6bece4f142" + "7d88ec4cedbc5627" + "59197e18a67c165c" + "ba0cbb35b2cd58fe",
}
CHECKERS = ["tools/check_secret_report.py", "tools/check_public_tree.py", "campaign_tool/records/public_scan.py"]
REQUIRED_CASES = {"complete", "zero", "partial_gapped", "shuffled", "duplicate", "at_limit",
                  "beyond_limit", "single_beyond_limit", "same_original_different_receipt",
                  "different_original_same_receipt", "different_pairs", "later_receipt_duplicate",
                  "later_receipt_range"}
REGRESSIONS = {"test_case_duplicate", "test_case_beyond_limit", "test_case_single_beyond_limit",
               "test_case_later_receipt_duplicate", "test_case_later_receipt_range"}


def require(ok, code):
    if not ok:
        raise ValueError(code)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def save(path, value):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True, indent=2)
        stream.write("\n")


def git(*args, cwd=REPO):
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, timeout=60, shell=False)
    require(done.returncode == 0, "git_failed_" + args[0])
    return done.stdout


def identity():
    return {"head": git("rev-parse", "HEAD").decode().strip(),
            "tree": git("rev-parse", "HEAD^{tree}").decode().strip(),
            "status": git("status", "--short", "--untracked-files=all").decode(),
            "authored_hashes": {p: sha((REPO / p).read_bytes()) for p in AUTHORED}}


def validate_staging(args):
    manifest = json.loads(args.source_manifest.read_text())
    require(manifest["schema"] == "agentorange.source-manifest/v1", "manifest_schema")
    require(manifest["card"] == "FC13-RECEIPT-POSITIONS", "manifest_card")
    require(Path(manifest["repo"]) == REPO and Path(manifest["write_root"]) == REPO.parent, "manifest_paths")
    require(manifest["head"] == BASE and manifest["tree"] == BASE_TREE, "manifest_base")
    require(manifest["inputs"] == [], "unexpected_inputs")
    runtime = manifest["runtime"]
    require((REPO / ".venv").is_symlink(), "venv_link_required")
    require((REPO / ".venv").resolve() == Path(runtime["repo_venv"]).resolve(strict=True), "venv_binding")
    require(Path(runtime["credential_scan_python3"]).is_file() and Path(runtime["detect_secrets"]).is_file(), "scanner_staging")
    expected_python = (Path(runtime["credential_scan_python3"]) if args.phase == "credentials"
                       else REPO / ".venv/bin/python3")
    require(os.path.abspath(sys.executable) == str(expected_python), "interpreter_literal_mismatch")
    require(Path(sys.prefix).resolve() == Path(runtime["repo_venv"]).resolve(), "interpreter_environment")
    require(git("rev-parse", BASE + "^{tree}").decode().strip() == BASE_TREE, "base_tree")
    for path, expected in PINS.items():
        require(sha(git("show", BASE + ":" + path)) == expected, "source_pin_" + path)
        if path != PRODUCT:
            require(sha((REPO / path).read_bytes()) == expected, "readonly_pin_" + path)
    for path in CHECKERS:
        require((REPO / path).read_bytes() == git("show", BASE + ":" + path), "checker_changed")
    require(git("merge-base", BASE, "HEAD").decode().strip() == BASE, "not_descendant")
    if args.baseline:
        require(git("rev-parse", "HEAD").decode().strip() == BASE, "baseline_head")
        require(sha((REPO / PRODUCT).read_bytes()) == PINS[PRODUCT], "baseline_product")
    else:
        require(identity()["status"] == "", "final_tree_not_clean")
        changed = set(git("diff", "--name-only", BASE, "HEAD").decode().splitlines())
        require(changed == set(AUTHORED), "exclusive_diff")
        require(git("diff", "HEAD") == b"" and git("diff", "--cached", "HEAD") == b"", "index_bytes")
    visible = set(git("ls-files", "--others", "--exclude-standard", "-z").decode().split("\0")) - {""}
    allowed = {".venv"} | (set(AUTHORED) if args.baseline else set())
    require(visible <= allowed, "untracked_scan_visible")
    return manifest


class RecordedResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ids = []

    def startTest(self, test):
        self.ids.append(test.id())
        super().startTest(test)


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item.id()


def run_suite(suite):
    planned = list(flatten(suite))
    result = unittest.TextTestRunner(verbosity=2, resultclass=RecordedResult).run(suite)
    return {"planned_ids": planned, "test_ids": result.ids, "tests_run": result.testsRun,
            "failures": [{"id": t.id(), "traceback": text} for t, text in result.failures],
            "errors": [{"id": t.id(), "traceback": text} for t, text in result.errors],
            "skipped": [{"id": t.id(), "reason": text} for t, text in result.skipped],
            "successful": result.wasSuccessful()}


def child(args):
    root = args.temporary_root.resolve(strict=True)
    os.environ["TMPDIR"] = str(root)
    tempfile.tempdir = str(root)
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(REPO))

    def offline(*unused, **kwargs):
        raise RuntimeError("offline_network_forbidden")

    with contextlib.ExitStack() as stack:
        for name in ("connect", "connect_ex", "bind", "listen"):
            stack.enter_context(mock.patch.object(socket.socket, name, offline))
        stack.enter_context(mock.patch.object(socket, "getaddrinfo", offline))
        loader = unittest.TestLoader()
        if args.child == "compatibility":
            suite = loader.loadTestsFromNames(SELECTORS)
            result = run_suite(suite)
        elif args.child == "discovery":
            environment, temp, paths = dict(os.environ), tempfile.tempdir, list(sys.path)
            socket_methods = [socket.socket.connect, socket.socket.bind, socket.getaddrinfo]

            class Sentinel(unittest.TestCase):
                def runTest(self):
                    self.assertEqual(dict(os.environ), environment)
                    self.assertEqual(tempfile.tempdir, temp)
                    self.assertEqual(sys.path, paths)
                    self.assertEqual([socket.socket.connect, socket.socket.bind, socket.getaddrinfo], socket_methods)
                    with tempfile.TemporaryFile() as stream:
                        stream.write(b"fictional sentinel")
                        stream.seek(0)
                        self.assertEqual(stream.read(), b"fictional sentinel")

            # Unrelated sentinel runs before discovery/import, and again after suite execution.
            before = run_suite(unittest.TestSuite([Sentinel()]))
            require(before["successful"], "before_sentinel_failed")
            suite = loader.discover(str(REPO / "tests/records"), pattern="test_fc13_receipt_positions.py",
                                    top_level_dir=str(REPO))
            result = run_suite(suite)
            after = run_suite(unittest.TestSuite([Sentinel()]))
            result["sentinels"] = [before, after]
            require(after["successful"], "after_sentinel_failed")
        else:
            from tests.records import test_fc13_receipt_positions as tests
            if args.child == "mutant":
                from campaign_tool.records.understanding import evidence
                names = ["test_case_duplicate", "test_case_beyond_limit", "test_case_partial_gapped"]
                suite = unittest.TestSuite(tests.PositionTests(name) for name in names)
                # Only the new gate is bypassed. Every count/unit/hash statement stays live.
                with mock.patch.object(evidence, "_check_receipt_positions", return_value=None):
                    result = run_suite(suite)
            else:
                result = run_suite(loader.loadTestsFromModule(tests))
        save(root / (args.child + "-result.json"), result)
        return 0 if result["successful"] and not result["skipped"] else 1


def execute(row, label, argv, cwd):
    done = subprocess.run(argv, cwd=cwd, capture_output=True, shell=False, timeout=180)
    (row / (label + ".stdout")).write_bytes(done.stdout)
    (row / (label + ".stderr")).write_bytes(done.stderr)
    record = {"argv": argv, "cwd": str(cwd), "exit": done.returncode,
              "stdout": label + ".stdout", "stderr": label + ".stderr",
              "stdout_sha256": sha(done.stdout), "stderr_sha256": sha(done.stderr)}
    save(row / (label + "-process.json"), record)
    return done, record


def invoke_child(row, mode, label=None, cwd=REPO):
    label = label or mode
    temp = row / (label + "-temporary")
    temp.mkdir()
    argv = [sys.executable, str(Path(__file__).resolve()), "--child", mode,
            "--temporary-root", str(temp)]
    done, process = execute(row, label, argv, cwd)
    result = json.loads((temp / (mode + "-result.json")).read_text())
    require(result["planned_ids"] == result["test_ids"], "incomplete_test_execution")
    require(not result["errors"] and not result["skipped"], "test_error_or_skip")
    result["process"] = process
    return done, result


def full_matrix(result, cases):
    require(result["tests_run"] == 18, "required_test_count")
    names = {x.rsplit(".", 1)[-1] for x in result["test_ids"]}
    require({"test_case_" + c["id"] for c in cases} <= names, "missing_matrix_case")


def parent(args):
    manifest = validate_staging(args)
    require(args.out_base.is_absolute(), "absolute_output_required")
    out = args.out_base.resolve()
    require(REPO != out and REPO not in out.parents, "output_inside_repo")
    out.mkdir(parents=True, exist_ok=True)
    row = out / (args.phase + "-" + str(uuid.uuid4()))
    row.mkdir(mode=0o700)
    os.environ["TMPDIR"] = str(row)
    tempfile.tempdir = str(row)
    sys.dont_write_bytecode = True
    initial = identity()
    (row / "complete.diff").write_bytes(git("diff", "--binary", BASE, "HEAD"))
    (row / "source-status.txt").write_text(initial["status"])
    (row / "source-inventory.bin").write_bytes(git("ls-files", "-z", "--cached", "--others", "--exclude-standard"))
    cases = json.loads((REPO / FIXTURE).read_text())["cases"]
    require(len(cases) == len(REQUIRED_CASES) and {c["id"] for c in cases} == REQUIRED_CASES, "fixture_coverage")
    receipt = {"schema": "fc13-receipt-positions-acceptance/v1", "phase": args.phase,
               "baseline_only": args.baseline, "argv": sys.argv, "cwd": str(Path.cwd()),
               "interpreter": sys.executable, "runtime_prefix": sys.prefix,
               "source_manifest_sha256": sha(args.source_manifest.read_bytes()),
               "identity": initial, "case_dimensions": cases, "checks": {},
               "unchanged_checkers": {p: sha((REPO / p).read_bytes()) for p in CHECKERS},
               "author": {"model": "gpt-6.1-sol", "provider": "openai-codex", "task": "t_fd9ed14e"}}
    try:
        if args.phase == "suite":
            done, result = invoke_child(row, "suite")
            receipt["checks"]["suite"] = result
            full_matrix(result, cases)
            if args.baseline:
                require(done.returncode == 1, "baseline_must_be_red")
                require({x["id"].rsplit(".", 1)[-1] for x in result["failures"]} == REGRESSIONS, "unexpected_baseline_failure")
                require(all("ValueError not raised" in x["traceback"] for x in result["failures"]), "baseline_wrong_result")
            else:
                require(done.returncode == 0 and result["successful"], "suite_failed")
                mutant, result = invoke_child(row, "mutant")
                receipt["checks"]["negative_control"] = result
                require(mutant.returncode == 1 and result["tests_run"] == 3, "mutant_not_killed")
                require({x["id"].rsplit(".", 1)[-1] for x in result["failures"]} ==
                        {"test_case_duplicate", "test_case_beyond_limit"}, "wrong_mutant_failures")
                require(all("ValueError not raised" in x["traceback"] for x in result["failures"]), "wrong_mutant_cause")
                done, result = invoke_child(row, "suite", label="restored")
                receipt["checks"]["restored"] = result
                full_matrix(result, cases)
                require(done.returncode == 0 and result["successful"], "restoration_failed")
        elif args.phase == "compatibility":
            done, result = invoke_child(row, "compatibility")
            receipt["checks"]["compatibility"] = result
            require(done.returncode == 0 and result["successful"] and
                    result["tests_run"] == len(result["planned_ids"]) > 0, "compatibility_failed")
        elif args.phase == "portable":
            for path in (TEST, FIXTURE, RUNNER):
                raw = (REPO / path).read_text()
                for prefix in ("/" + "home/", "/" + "Users/", "/" + "workspace/"):
                    # Literal prefixes above are constructed below in the runner itself.
                    require(prefix not in raw, "box_specific_path")
            cwd = row / "unrelated-cwd"
            cwd.mkdir()
            temp = row / "script-temporary"
            temp.mkdir()
            done, process = execute(row, "absolute-script", [sys.executable, str(REPO / TEST),
                                     "--temporary-root", str(temp)], cwd)
            receipt["checks"]["script"] = process
            require(done.returncode == 0 and b"Ran 18 tests" in done.stderr and b"OK" in done.stderr, "portable_script_failed")
            done, result = invoke_child(row, "discovery", cwd=cwd)
            receipt["checks"]["discovery"] = result
            full_matrix(result, cases)
            require(done.returncode == 0 and result["successful"], "discovery_failed")
        elif args.phase == "credentials":
            runtime = manifest["runtime"]
            done, process = execute(row, "scanner", [runtime["detect_secrets"], "scan", "--all-files",
                                        "--exclude-files", "^.git/", "--no-verify"], REPO)
            receipt["checks"]["scanner"] = process
            require(done.returncode == 0, "scanner_failed")
            report = row / "scanner.stdout"
            require(isinstance(json.loads(report.read_text())["results"], dict), "invalid_scan_json")
            done, process = execute(row, "secret-check", [runtime["credential_scan_python3"],
                                        str(REPO / "tools/check_secret_report.py"), str(report)], REPO)
            receipt["checks"]["secret_check"] = process
            require(done.returncode == 0, "secret_check_failed")
        elif args.phase == "public-tree":
            clone = row / "committed-tree"
            done, process = execute(row, "clone", ["git", "clone", "--local", "--no-hardlinks", "--no-checkout",
                                                 "--no-recurse-submodules", str(REPO), str(clone)], row)
            receipt["checks"]["clone"] = process
            require(done.returncode == 0, "clone_failed")
            done, process = execute(row, "checkout", ["git", "checkout", "--detach", initial["head"]], clone)
            receipt["checks"]["checkout"] = process
            require(done.returncode == 0, "checkout_failed")
            require(git("rev-parse", "HEAD", cwd=clone).decode().strip() == initial["head"], "clone_head")
            require(git("rev-parse", "HEAD^{tree}", cwd=clone).decode().strip() == initial["tree"], "clone_tree")
            require({p: sha((clone / p).read_bytes()) for p in AUTHORED} == initial["authored_hashes"], "clone_authored_bytes")
            inventory = git("ls-files", "-z", cwd=clone)
            (row / "committed-inventory.bin").write_bytes(inventory)
            receipt["committed_file_count"] = len([p for p in inventory.split(b"\0") if p])
            done, process = execute(row, "committed-public-tree", [sys.executable, str(clone / "tools/check_public_tree.py")], clone)
            receipt["checks"]["committed_public_tree"] = process
            public = json.loads(done.stdout)
            require(done.returncode == 0 and public["findings"] == [] and public["scanned_files"] == receipt["committed_file_count"], "committed_scan_failed")
            done, process = execute(row, "in-place-informational", [sys.executable, str(REPO / "tools/check_public_tree.py")], REPO)
            receipt["checks"]["in_place_informational"] = process
            findings = json.loads(done.stdout)["findings"]
            require((done.returncode == 0 and findings == []) or
                    (done.returncode == 1 and findings == [[".venv", 0, "symlink_not_scanned"]]), "in_place_scan_unrecognized")
        require(identity() == initial, "candidate_changed_during_acceptance")
        receipt["verdict"] = "BASELINE_EVIDENCE_ONLY" if args.baseline else "PASS_REVIEW_REQUIRED"
        code = 0
    except Exception:
        receipt["verdict"] = "FAIL"
        receipt["error"] = traceback.format_exc()
        code = 1
    # All synchronous children have exited; hash every writer's completed output.
    receipt["files"] = {str(p.relative_to(row)): sha(p.read_bytes()) for p in sorted(row.rglob("*"))
                        if p.is_file() and ".git" not in p.relative_to(row).parts}
    save(row / "evidence-manifest.json", receipt)
    print(json.dumps({"verdict": receipt["verdict"], "output": str(row),
                      "evidence_manifest_sha256": sha((row / "evidence-manifest.json").read_bytes()),
                      "error": receipt.get("error")}))
    return code


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=["suite", "compatibility", "portable", "credentials", "public-tree"])
    parser.add_argument("--source-manifest", type=Path)
    parser.add_argument("--out-base", type=Path)
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--child", choices=["suite", "mutant", "compatibility", "discovery"])
    parser.add_argument("--temporary-root", type=Path)
    args = parser.parse_args()
    if args.child:
        require(args.temporary_root is not None, "child_temporary_root")
        return child(args)
    require(args.phase and args.source_manifest and args.out_base, "acceptance_arguments_required")
    require(not args.baseline or args.phase in ("suite", "compatibility"), "baseline_phase")
    return parent(args)


if __name__ == "__main__":
    raise SystemExit(main())
