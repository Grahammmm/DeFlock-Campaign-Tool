"""Five offline, identity-bound acceptance rows for FC9 historical composition.

Only the standard library is used here. All subprocesses use literal argv and
shell=False; every invocation retains its own stdout, stderr, and receipt files.
"""
import argparse
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import traceback
import unittest
import uuid

REPO = Path(__file__).resolve().parents[1]
AUTHORED = ("tests/records/test_fc9_policy_history_compare.py",
            "tests/fixtures/fc9-policy-history-compare/cases.json",
            "tools/fc9_policy_history_compare_acceptance.py")
UNCHANGED = ("campaign_tool/records/understanding/policy.py",
             "campaign_tool/records/understanding/reconcile.py",
             "tests/records/test_understanding.py", "tools/check_secret_report.py",
             "tools/check_public_tree.py", "campaign_tool/records/public_scan.py", ".gitignore")
TEST_MODULE = "tests.records.test_fc9_policy_history_compare"
COMPATIBILITY = ("tests.records.test_understanding.PolicyTests", "tests.records.test_understanding.ReconcileTests")
PHASES = ("suite", "compatibility", "portable", "credentials", "public-tree")


def need(condition, label):
    if not condition:
        raise ValueError(label)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def safe_absolute(path):
    need(path.is_absolute() and ".." not in path.parts, "literal_absolute_path_required")
    need(not any(p.is_symlink() for p in (path, *path.parents)), "symlink_path_refused")
    return path


class Recorder:
    def __init__(self, root):
        self.root = root
        self.commands = []

    def run(self, label, argv, cwd, allowed=(0,)):
        need(re.fullmatch(r"[a-z0-9_-]+", label) is not None, "command_label")
        argv = [str(a) for a in argv]
        info = {"label": label, "argv": argv, "cwd": str(cwd), "shell": False}
        self.commands.append(info)
        write_json(self.root / (label + ".command.json"), info)
        try:
            proc = subprocess.run(argv, cwd=cwd, shell=False, capture_output=True, timeout=180)
        except BaseException as error:
            info.update(error=repr(error), exit=None)
            write_json(self.root / (label + ".command.json"), info)
            raise
        (self.root / (label + ".stdout")).write_bytes(proc.stdout)
        (self.root / (label + ".stderr")).write_bytes(proc.stderr)
        info.update(exit=proc.returncode, stdout_sha256=digest(proc.stdout), stderr_sha256=digest(proc.stderr))
        write_json(self.root / (label + ".command.json"), info)
        need(proc.returncode in allowed, "child_failed:" + label + ":" + str(proc.returncode))
        return proc.stdout

    def git(self, label, *argv, cwd=REPO):
        return self.run(label, ["git", *argv], cwd).decode().strip()


def child_main(args):
    # Dedicated child owns its redirects. Importing the test never installs these.
    row = safe_absolute(args.row_root)
    temp = row / ("temporary-" + args.child)
    temp.mkdir(mode=0o700, exist_ok=False)
    tempfile.tempdir = str(temp)
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(REPO))
    fixture = json.loads((REPO / AUTHORED[1]).read_text())
    case_ids = [c["id"] for c in fixture["cases"]]
    loader = unittest.TestLoader()
    sentinel_checks = []
    if args.child == "compatibility":
        suite = loader.loadTestsFromNames(list(COMPATIBILITY))
    elif args.child == "suite":
        suite = loader.loadTestsFromName(TEST_MODULE)
    else:
        # Snapshot before ordinary discovery and again after test execution.
        ambient = (dict(os.environ), tempfile.tempdir, list(sys.path), sys.dont_write_bytecode, os.getcwd())
        import socket
        import sqlite3
        original_functions = (socket.socket.connect, socket.socket.connect_ex,
                              socket.create_connection, sqlite3.connect, subprocess.Popen)

        class Sentinel(unittest.TestCase):
            def runTest(self):
                self.assertEqual((dict(os.environ), tempfile.tempdir, list(sys.path),
                                  sys.dont_write_bytecode, os.getcwd()), ambient)
                self.assertEqual((socket.socket.connect, socket.socket.connect_ex,
                                  socket.create_connection, sqlite3.connect, subprocess.Popen), original_functions)
                sentinel_checks.append("ambient_and_unrelated_peer_unchanged")

        # Run peer before import and after discovery; also after the whole suite.
        initial = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite([Sentinel()]))
        need(initial.wasSuccessful(), "pre_discovery_sentinel")
        suite = loader.discover(str(REPO / "tests/records"),
                                pattern="test_fc9_policy_history_compare.py", top_level_dir=str(REPO))
        immediate = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite([Sentinel()]))
        need(immediate.wasSuccessful(), "post_discovery_sentinel")
        suite = unittest.TestSuite([suite, Sentinel()])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    need(result.wasSuccessful() and not result.skipped and not result.expectedFailures,
         "required_tests_not_all_green")
    if args.child == "compatibility":
        need(result.testsRun == 4, "compatibility_count")
        coverage = []
    else:
        module = importlib.import_module(TEST_MODULE)
        need(list(module.CASE_IDS) == case_ids, "fixture_case_coverage_mismatch")
        need(result.testsRun == (14 if args.child == "discovery" else 13), "suite_count")
        coverage = case_ids
    data = {"mode": args.child, "tests_run": result.testsRun, "case_ids": coverage,
            "fixture_dimensions": fixture["cases"] if coverage else [],
            "skipped": len(result.skipped), "successful": result.wasSuccessful(),
            "sentinel_checks": sentinel_checks, "temporary_root": str(temp)}
    print("FC9_COVERAGE " + json.dumps(data, sort_keys=True))
    return 0


def coverage(raw):
    rows = [json.loads(line[len("FC9_COVERAGE "):]) for line in raw.decode().splitlines()
            if line.startswith("FC9_COVERAGE ")]
    need(len(rows) == 1 and rows[0]["successful"] and rows[0]["skipped"] == 0, "coverage_receipt")
    return rows[0]


def validate_candidate(rec, manifest, phase):
    need(manifest["schema"] == "agentorange.source-manifest/v1", "manifest_schema")
    need(Path(manifest["repo"]) == REPO, "manifest_repo")
    need(Path(manifest["write_root"]) == REPO.parent, "manifest_root")
    need(rec.git("source-remote", "remote", "get-url", "origin") == manifest["remote"], "manifest_remote")
    base, tree = manifest["head"], manifest["tree"]
    need(re.fullmatch(r"[a-f0-9]{40}", base) and re.fullmatch(r"[a-f0-9]{40}", tree), "manifest_git_identity")
    need(rec.git("base-tree", "rev-parse", base + "^{tree}") == tree, "base_tree_mismatch")
    head = rec.git("candidate-head", "rev-parse", "HEAD")
    candidate_tree = rec.git("candidate-tree", "rev-parse", "HEAD^{tree}")
    need(head != base and re.fullmatch(r"[a-f0-9]{40}", head), "committed_candidate_required")
    rec.git("ancestry", "merge-base", "--is-ancestor", base, head)
    delta = rec.git("exclusive-delta", "diff", "--name-only", base, head).splitlines()
    need(set(delta) == set(AUTHORED), "exclusive_write_set")
    status = rec.git("initial-status", "status", "--porcelain=v1", "--untracked-files=all")
    need(status in ("", "?? .venv"), "dirty_candidate")
    link = REPO / ".venv"
    need(link.is_symlink() and link.resolve() == Path(manifest["runtime"]["repo_venv"]).resolve(), "runtime_link_mismatch")
    need(Path(sys.prefix).resolve() == Path(manifest["runtime"]["repo_venv"]).resolve(), "runtime_prefix_mismatch")
    if phase == "credentials":
        need(os.path.abspath(sys.executable) == manifest["runtime"]["credential_scan_python3"], "credential_interpreter_mismatch")
    else:
        need(os.path.abspath(sys.executable) == str(REPO / ".venv/bin/python3"), "repo_interpreter_mismatch")
    hashes = {}
    for i, name in enumerate((*AUTHORED, *UNCHANGED)):
        path = REPO / name
        safe_absolute(path)
        raw = path.read_bytes()
        committed = rec.run("head-file-" + str(i), ["git", "show", head + ":" + name], REPO)
        index = rec.run("index-file-" + str(i), ["git", "show", ":" + name], REPO)
        need(raw == committed == index, "file_not_committed:" + name)
        hashes[name] = digest(raw)
        if name in UNCHANGED:
            original = rec.run("base-file-" + str(i), ["git", "show", base + ":" + name], REPO)
            need(raw == original, "baseline_file_modified:" + name)
    return {"head": head, "tree": candidate_tree, "base": base, "base_tree": tree,
            "status": status, "file_sha256": hashes,
            "interpreter": sys.executable, "interpreter_resolved": str(Path(sys.executable).resolve()),
            "prefix_resolved": str(Path(sys.prefix).resolve()), "runtime_link": str(link.resolve())}


def public_tree(rec, identity, manifest):
    initial_inventory = rec.run("path-inventory", ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], REPO)
    paths = set(initial_inventory.decode().split("\0")) - {""}
    tracked = set(rec.run("tracked-inventory", ["git", "ls-files", "-z"], REPO).decode().split("\0")) - {""}
    need(paths - tracked <= {".venv"}, "untracked_scan_visible_files")
    # One unchanged staged-tree check is explicitly informational (INC-174).
    raw = rec.run("in-place-informational", [sys.executable, "-B", REPO / "tools/check_public_tree.py"], REPO, allowed=(0, 1))
    informational = json.loads(raw)
    need(informational.get("findings") in ([], [[".venv", 0, "symlink_not_scanned"]]), "unexpected_in_place_finding")
    need(informational.get("scanned_files", 0) > 0 and "error" not in informational, "in_place_incomplete")
    code = rec.commands[-1]["exit"]
    need(code == int(bool(informational["findings"])), "in_place_exit_mismatch")
    need((REPO / ".venv").resolve() == Path(manifest["runtime"]["repo_venv"]).resolve(), "in_place_runtime_mismatch")
    clone = rec.root / "committed-tree"
    rec.run("clone", ["git", "clone", "--local", "--no-hardlinks", "--no-checkout", "--no-recurse-submodules", REPO, clone], rec.root)
    rec.run("checkout", ["git", "checkout", "--detach", identity["head"]], clone)
    need(rec.git("clone-head", "rev-parse", "HEAD", cwd=clone) == identity["head"], "clone_head")
    need(rec.git("clone-tree", "rev-parse", "HEAD^{tree}", cwd=clone) == identity["tree"], "clone_tree")
    need(not (clone / ".venv").exists() and not (clone / ".venv").is_symlink(), "clone_runtime_link_forbidden")
    for name, expected in identity["file_sha256"].items():
        need(digest((clone / name).read_bytes()) == expected, "clone_file_hash:" + name)
    clone_raw = rec.run("committed-public-scan", [sys.executable, "-B", clone / "tools/check_public_tree.py"], clone)
    result = json.loads(clone_raw)
    need(result.get("findings") == [] and result.get("scanned_files", 0) == len(tracked), "committed_public_scan_incomplete")
    need(rec.git("final-head", "rev-parse", "HEAD") == identity["head"], "source_head_changed")
    need(rec.git("final-tree", "rev-parse", "HEAD^{tree}") == identity["tree"], "source_tree_changed")
    need(rec.git("final-status", "status", "--porcelain=v1", "--untracked-files=all") == identity["status"], "source_status_changed")
    need(rec.run("final-path-inventory", ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], REPO) == initial_inventory, "source_inventory_changed")
    return {"committed_tree": result, "in_place_informational": {"incident": "INC-174", "result": informational,
            "not_a_staged_tree_pass": bool(informational["findings"])}, "scanned_files": result["scanned_files"]}


def run_phase(args, rec, identity, manifest):
    fixture = json.loads((REPO / AUTHORED[1]).read_text())
    required_ids = [c["id"] for c in fixture["cases"]]
    if args.phase in ("suite", "compatibility"):
        raw = rec.run(args.phase, [sys.executable, "-B", Path(__file__).resolve(), "--child", args.phase,
                                  "--row-root", rec.root], REPO)
        data = coverage(raw)
        need(data["case_ids"] == (required_ids if args.phase == "suite" else []), "required_case_coverage")
        return data
    if args.phase == "portable":
        findings = []
        for name in AUTHORED:
            text = (REPO / name).read_text()
            # No platform-specific absolute paths in any authored source/fixture.
            for line_no, line in enumerate(text.splitlines(), 1):
                if re.search(r"/(?:home|Users|workspace|tmp|var/tmp)/|[A-Za-z]:\\", line):
                    findings.append([name, line_no])
        need(findings == [], "host_specific_authored_literal")
        other_cwd = rec.root / "fresh-cwd"
        other_cwd.mkdir(mode=0o700)
        raw = rec.run("portable-script", [sys.executable, "-B", REPO / AUTHORED[0],
                      "--temporary-root", rec.root / "script-temporary"], other_cwd)
        script = coverage(raw)
        discovery_raw = rec.run("portable-discovery", [sys.executable, "-B", Path(__file__).resolve(),
                                "--child", "discovery", "--row-root", rec.root], other_cwd)
        discovery = coverage(discovery_raw)
        need(script["case_ids"] == discovery["case_ids"] == required_ids, "portable_case_coverage")
        need(len(discovery["sentinel_checks"]) == 3, "sentinel_coverage")
        return {"script": script, "discovery": discovery, "static_findings": findings}
    if args.phase == "credentials":
        runtime = manifest["runtime"]
        scanner = Path(runtime["detect_secrets"])
        need(scanner.is_absolute() and scanner.is_file() and os.access(scanner, os.X_OK), "scanner_unavailable")
        need(scanner.parent == Path(runtime["credential_scan_python3"]).parent, "scanner_runtime_mismatch")
        raw = rec.run("detect-secrets", [scanner, "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"], REPO)
        report = json.loads(raw)
        need(type(report.get("results")) is dict, "scanner_report_shape")
        report_path = rec.root / "scanner-report.json"
        report_path.write_bytes(raw)
        rec.run("check-secret-report", [sys.executable, REPO / "tools/check_secret_report.py", report_path], REPO)
        return {"scanner_report_sha256": digest(raw), "scanner_result_files": len(report["results"]),
                "scanner": str(scanner), "credential_scan_python3": sys.executable}
    return public_tree(rec, identity, manifest)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=PHASES)
    parser.add_argument("--source-manifest", type=Path)
    parser.add_argument("--out-base", type=Path)
    parser.add_argument("--child", choices=("suite", "compatibility", "discovery"))
    parser.add_argument("--row-root", type=Path)
    args = parser.parse_args()
    if args.child:
        need(args.row_root is not None and args.phase is None, "child_arguments")
        return child_main(args)
    if args.phase is None or args.source_manifest is None or args.out_base is None:
        raise ValueError("phase_arguments")
    safe_absolute(args.source_manifest)
    safe_absolute(args.out_base)
    need(args.out_base == REPO.parent / "out", "output_boundary")
    args.out_base.mkdir(mode=0o700, parents=True, exist_ok=True)
    root = args.out_base / (args.phase + "-" + uuid.uuid4().hex)
    root.mkdir(mode=0o700)
    temp = root / "parent-temporary"
    temp.mkdir(mode=0o700)
    tempfile.tempdir = str(temp)
    sys.dont_write_bytecode = True
    rec = Recorder(root)
    receipt = {"schema": "fc9-policy-history-acceptance/v1", "phase": args.phase,
               "argv": [sys.executable, *sys.argv], "cwd": os.getcwd(), "run_root": str(root),
               "source_manifest": str(args.source_manifest), "manifest_sha256": digest(args.source_manifest.read_bytes()),
               "status": "FAIL", "review_required": True, "remote_ci": "OWED",
               "semantic_legal_human_live_acceptance": False}
    exit_code = 1
    try:
        need(Path.cwd().resolve() == REPO, "acceptance_cwd")
        manifest = json.loads(args.source_manifest.read_text())
        identity = validate_candidate(rec, manifest, args.phase)
        receipt["candidate"] = identity
        receipt["results"] = run_phase(args, rec, identity, manifest)
        # All rows bind to unchanged final identity, not merely their initial HEAD.
        need(rec.git("row-end-head", "rev-parse", "HEAD") == identity["head"], "row_head_changed")
        need(rec.git("row-end-status", "status", "--porcelain=v1", "--untracked-files=all") == identity["status"], "row_status_changed")
        for name, expected in identity["file_sha256"].items():
            need(digest((REPO / name).read_bytes()) == expected, "row_source_changed:" + name)
        need(digest(args.source_manifest.read_bytes()) == receipt["manifest_sha256"], "row_manifest_changed")
        receipt["status"] = "PASS_REVIEW_REQUIRED"
        exit_code = 0
    except BaseException as error:
        receipt["error"] = repr(error)
        (root / "failure.txt").write_text(traceback.format_exc())
        print(traceback.format_exc(), file=sys.stderr)
    finally:
        receipt["commands"] = rec.commands
        # Git clone scratch is not an authored output artifact; logs bind its IDs.
        receipt["artifact_sha256"] = {str(p.relative_to(root)): digest(p.read_bytes())
                                      for p in sorted(root.iterdir()) if p.is_file()}
        write_json(root / "receipt.json", receipt)
    print(json.dumps({"phase": args.phase, "status": receipt["status"], "receipt": str(root / "receipt.json"),
                      "receipt_sha256": digest((root / "receipt.json").read_bytes()),
                      "head": receipt.get("candidate", {}).get("head")}, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
