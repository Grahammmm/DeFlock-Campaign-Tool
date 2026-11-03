"""Five-row offline FC15 recorder; Sol 6.1 / OpenAI Codex authored.

No shipping, operational inputs, installs, or generic job execution. Worker modes
are private fixed child entrypoints for pre-import sentinels and test recording.
"""
import argparse
from contextlib import ExitStack, contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import runpy
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.request
import uuid

REPO = Path(__file__).resolve().parents[1]
BASE = "126accc856" "8009abec5b" "c0698e1c4e" "d3f75ef36b"
AUTHORED = (
    "tests/records/test_fc15_policy_empty_requirements.py",
    "tests/fixtures/fc15-policy-empty-requirements/cases.json",
    "tools/fc15_policy_empty_requirements_acceptance.py",
)
SELECTORS = ("tests.records.test_understanding.PolicyTests",
             "tests.records.test_understanding.ReconcileTests")
CASES = {
    "selected_empty": ["omitted-empty-facts", "explicit-empty-facts"],
    "exceptions_only": ["exception", "term", "both"],
    "facts_not_rules": ["omitted-supplied-facts", "explicit-supplied-facts"],
    "mixed_families": ["empty-and-transcribed"],
    "nonempty_positive": ["complete-cited-positive"],
    "replay_preservation": ["permuted-inputs", "source-hold", "applicability-hold"],
}
TEST_METHODS = sorted(["test_" + k for k in CASES] +
                      ["test_deciding_compare_control", "test_independent_gap_control"])
CHECKERS = ("tools/check_secret_report.py", "tools/check_public_tree.py",
            "campaign_tool/records/public_scan.py")


def require(ok, message):
    if not ok:
        raise ValueError(message)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def git(*args, cwd=REPO):
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=False,
                          timeout=60, shell=False)
    require(done.returncode == 0, "git failure: " + repr(args) + ": " + done.stderr.decode())
    return done.stdout


def no_symlinks(path):
    require(path.is_absolute(), "absolute_path_required")
    require(not any(p.is_symlink() for p in (path, *path.parents)), "source_or_output_symlink")


def validate_manifest(path, phase, baseline):
    no_symlinks(path)
    manifest = json.loads(path.read_text())
    require(manifest["schema"] == "agentorange.source-manifest/v1", "manifest_schema")
    require(Path(manifest["repo"]) == REPO, "manifest_repo")
    no_symlinks(REPO)
    require(manifest["head"] == BASE, "admitted_initial_head")
    require(git("rev-parse", BASE + "^{tree}").decode().strip() == manifest["tree"], "initial_tree")
    git("merge-base", "--is-ancestor", BASE, "HEAD")
    if baseline:
        require(phase == "compatibility", "baseline_only_compatibility")
        require(git("rev-parse", "HEAD").decode().strip() == BASE, "initial_HEAD_must_equal_base")
    runtime = manifest["runtime"]
    require((REPO / ".venv").is_symlink(), "staged_venv_link_required")
    require((REPO / ".venv").resolve() == Path(runtime["repo_venv"]).resolve(), "venv_binding")
    expected = (Path(runtime["credential_scan_python3"]) if phase == "credentials"
                else REPO / ".venv/bin/python3")
    require(Path(sys.executable).absolute() == expected.absolute(), "literal_runtime_interpreter")
    require(expected.is_file() and Path(runtime["detect_secrets"]).is_file(), "runtime_unavailable")
    require(Path(runtime["credential_scan_python3"]).parent ==
            Path(runtime["detect_secrets"]).parent, "scanner_runtime_mismatch")
    for name in CHECKERS:
        require((REPO / name).read_bytes() == git("show", BASE + ":" + name), "changed_checker:" + name)
    # Every pre-existing tracked file, not only the three headline pins, stays unchanged.
    changed = set(filter(None, git("diff", "--name-only", BASE).decode().splitlines()))
    require(changed <= set(AUTHORED), "exclusive_write_set")
    return manifest


def identity(baseline=False):
    head = git("rev-parse", "HEAD").decode().strip()
    tree = git("rev-parse", "HEAD^{tree}").decode().strip()
    names = sorted(filter(None, git("ls-files", "-z").decode().split("\0")))
    hashes = {}
    for name in names:
        p = REPO / name
        no_symlinks(p)
        hashes[name] = digest(p.read_bytes())
    status = git("status", "--porcelain=v1", "--untracked-files=all").decode()
    untracked = set(filter(None, git("ls-files", "--others", "--exclude-standard", "-z").decode().split("\0")))
    if baseline:
        require(untracked <= set(AUTHORED), "unexpected_untracked_baseline")
    else:
        require(not status, "final_candidate_not_clean:" + status)
        require(not untracked, "untracked_scan_visible")
        git("diff", "--exit-code", "HEAD")
        require(set(AUTHORED) <= set(names), "authored_files_not_committed")
    diff = git("diff", "--binary", BASE, "HEAD")
    return {"head": head, "tree": tree, "status": status,
            "tracked_hashes": hashes, "diff_sha256": digest(diff),
            "authored_hashes": {n: digest((REPO / n).read_bytes()) for n in AUTHORED},
            "scanner_allowlist_hashes": {n: hashes[n] for n in CHECKERS},
            "venv_target": str((REPO / ".venv").resolve())}


def deny(*args, **kwargs):
    raise RuntimeError("fc15_offline_effect_forbidden")


@contextmanager
def sentinels(testing=False):
    with ExitStack() as stack:
        for obj, name in ((socket.socket, "connect"), (socket.socket, "connect_ex"),
                          (socket.socket, "bind"), (socket.socket, "listen"),
                          (socket, "create_connection"), (socket, "getaddrinfo"),
                          (sqlite3, "connect"), (urllib.request, "urlopen"), (os, "system")):
            stack.enter_context(patch.object(obj, name, deny))
        if testing:
            stack.enter_context(patch.object(subprocess, "Popen", deny))
        yield


class Recorded(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ids, self.subcases = [], []

    def startTest(self, test):
        self.ids.append(test.id())
        super().startTest(test)

    def addSubTest(self, test, subtest, err):
        self.subcases.append({"test_id": test.id(), "id": subtest.id(),
                              "params": dict(subtest.params), "success": err is None})
        super().addSubTest(test, subtest, err)


def run_tests(args):
    # Only process-local CLI execution redirects defaults. The test module itself
    # must remain inert for ordinary discovery, proved by the peer sentinels.
    tempfile.tempdir = str(args.temporary_root)
    sys.path.insert(0, str(REPO))
    with sentinels(testing=True):
        before = (dict(os.environ), tempfile.tempdir, socket.create_connection,
                  sqlite3.connect, subprocess.Popen)
        class Peer(unittest.TestCase):
            def runTest(self):
                self.assertEqual((dict(os.environ), tempfile.tempdir, socket.create_connection,
                                  sqlite3.connect, subprocess.Popen), before)
                # Inert unrelated peer behavior; no fixture cleanup or I/O.
                self.assertEqual(json.loads('{"peer": [1, 2]}'), {"peer": [1, 2]})
        loader = unittest.TestLoader()
        if args.worker == "compatibility":
            suite = loader.loadTestsFromNames(SELECTORS)
        elif args.worker == "discovery":
            # One module only: no broad host/mailbox/acquisition discovery.
            suite = loader.discover(str(REPO / "tests/records"),
                                    pattern=Path(AUTHORED[0]).name, top_level_dir=str(REPO))
        else:
            suite = loader.loadTestsFromName("tests.records.test_fc15_policy_empty_requirements")
        require((dict(os.environ), tempfile.tempdir, socket.create_connection,
                 sqlite3.connect, subprocess.Popen) == before, "discovery_import_not_inert")
        if args.worker == "discovery":
            suite = unittest.TestSuite([Peer(), suite, Peer()])
        require(not loader.errors, "loader_errors:" + repr(loader.errors))
        result = unittest.TextTestRunner(verbosity=2, resultclass=Recorded).run(suite)
        data = {"test_ids": result.ids, "subcases": result.subcases,
                "tests_run": result.testsRun, "skips": result.skipped,
                "failures": [(t.id(), trace) for t, trace in result.failures],
                "errors": [(t.id(), trace) for t, trace in result.errors],
                "success": result.wasSuccessful() and not result.skipped,
                "peer_state_unchanged": (dict(os.environ), tempfile.tempdir,
                    socket.create_connection, sqlite3.connect, subprocess.Popen) == before}
        save(args.report, data)
        return 0 if data["success"] and data["peer_state_unchanged"] else 1


def scanner_worker(args):
    tempfile.tempdir = str(args.temporary_root)
    # Manifest/runtime validation happens before loading any installed scanner.
    manifest = validate_manifest(args.source_manifest, "credentials", False)
    command = [manifest["runtime"]["detect_secrets"], "scan", "--all-files",
               "--exclude-files", "^.git/", "--no-verify"]
    with sentinels():
        sys.argv = command
        runpy.run_path(command[0], run_name="__main__")
    return 0


def checker_worker(args):
    tempfile.tempdir = str(args.temporary_root)
    if args.worker == "secret-check":
        validate_manifest(args.source_manifest, "credentials", False)
        require(args.report.parent == args.temporary_root.parent, "secret_report_location")
        command = [str(REPO / CHECKERS[0]), str(args.report)]
    else:
        validate_manifest(args.source_manifest, "public-tree", False)
        require(args.scan_root in (REPO, args.temporary_root.parent / "committed-tree"), "public_scan_root")
        require(Path.cwd() == args.scan_root, "public_scan_cwd")
        command = [str(args.scan_root / CHECKERS[1])]
    original_popen = subprocess.Popen
    def local_git_only(argv, *a, **kw):
        require(type(argv) is list and argv == ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"]
                and not kw.get("shell"), "undeclared_checker_child")
        return original_popen(argv, *a, **kw)
    with sentinels(), patch.object(subprocess, "Popen", local_git_only):
        sys.argv = command
        runpy.run_path(command[0], run_name="__main__")
    return 0


def check_report(path, kind):
    data = json.loads(path.read_text())
    expected_count = 4 if kind == "compatibility" else (10 if kind == "discovery" else 8)
    require(data["success"] and data["tests_run"] == expected_count, "required_test_count")
    require(not data["skips"] and not data["failures"] and not data["errors"], "required_test_failure_or_skip")
    require(data["peer_state_unchanged"], "peer_behavior_changed")
    if kind != "compatibility":
        methods = sorted(t.rsplit(".", 1)[-1] for t in data["test_ids"] if ".EmptyRequirementsTests." in t)
        require(methods == TEST_METHODS, "missing_frozen_test_ID")
        expected = sorted(("test_" + k, case) for k, cases in CASES.items() for case in cases)
        observed = sorted((s["test_id"].rsplit(".", 1)[-1], s["params"].get("case"))
                          for s in data["subcases"])
        require(expected == observed and all(s["success"] for s in data["subcases"]), "missing_frozen_subcase_ID")
    return data


def main(args):
    manifest = validate_manifest(args.source_manifest, args.phase, args.baseline)
    expected_out = Path(manifest["write_root"]) / "out"
    require(args.out_base == expected_out, "out_base_manifest_binding")
    no_symlinks(args.out_base)
    args.out_base.mkdir(mode=0o700, exist_ok=True)
    row = args.out_base / (args.phase + "-" + str(uuid.uuid4()))
    row.mkdir(mode=0o700)
    temp = row / "temporary"
    temp.mkdir(mode=0o700)
    receipt = {"schema": "fc15-policy-empty-acceptance/v1", "phase": args.phase,
               "baseline": args.baseline, "argv": [sys.executable, *sys.argv],
               "cwd": str(Path.cwd()), "interpreter": sys.executable,
               "interpreter_resolved": str(Path(sys.executable).resolve()),
               "source_manifest_sha256": digest(args.source_manifest.read_bytes()),
               "author_runtime": {"model": "gpt-6.1-sol", "provider": "openai-codex"},
               "commands": [], "exit": 1, "review_acceptance": False,
               "limitations": "Scoped offline evidence only; full discovery and exact-head required CI OWED to shipping operator. No legal/semantic acceptance or hold release."}
    save(row / "source-manifest-copy.json", manifest)

    def child(label, argv, cwd=REPO, accepted=(0,)):
        env = dict(os.environ)
        env.update(TMPDIR=str(temp), TEMP=str(temp), TMP=str(temp), PYTHONDONTWRITEBYTECODE="1")
        done = subprocess.run([str(a) for a in argv], cwd=cwd, env=env,
                              capture_output=True, shell=False, timeout=240)
        stdout, stderr = row / (label + ".stdout"), row / (label + ".stderr")
        stdout.write_bytes(done.stdout)
        stderr.write_bytes(done.stderr)
        entry = {"label": label, "argv": [str(a) for a in argv], "cwd": str(cwd),
                 "exit": done.returncode, "stdout": str(stdout), "stderr": str(stderr),
                 "stdout_sha256": digest(done.stdout), "stderr_sha256": digest(done.stderr)}
        receipt["commands"].append(entry)
        save(row / "receipt.pending.json", receipt)
        require(done.returncode in accepted, "child_failed:" + label + ":" + str(done.returncode))
        return done

    def tests(kind):
        report = row / (kind + ".tests.json")
        child(kind, [sys.executable, "-B", __file__, "--worker", kind,
                     "--temporary-root", temp, "--report", report], cwd=row if kind == "discovery" else REPO)
        data = check_report(report, kind)
        receipt.setdefault("test_reports", {})[kind] = data

    try:
        before = identity(args.baseline)
        receipt["identity"] = before
        (row / "complete.diff").write_bytes(git("diff", "--binary", BASE, "HEAD"))
        require(json.loads((REPO / AUTHORED[1]).read_text())["cases"] == CASES, "fixture_case_map")
        receipt["case_to_test_mapping"] = {k: "EmptyRequirementsTests.test_" + k for k in CASES}
        if args.phase == "suite":
            tests("suite")
        elif args.phase == "compatibility":
            tests("compatibility")
            receipt["behavior_delta"] = "None: product bytes and existing tests unchanged. Baseline and final run the same four exact-selector tests."
        elif args.phase == "portable":
            for name in AUTHORED:
                raw = (REPO / name).read_text()
                require(not any(mark in raw for mark in ("/" + "home" + "/", "/" + "Users" + "/",
                            "candidates/" + "deflock/")), "box_specific_path:" + name)
            # Absolute script, unrelated fresh cwd and literal redirected root.
            script_report = row / "absolute-script.tests.json"
            child("absolute-script", [sys.executable, "-B", REPO / AUTHORED[0],
                                      "--temporary-root", temp, "--report", script_report], cwd=row)
            receipt.setdefault("test_reports", {})["absolute-script"] = check_report(script_report, "suite")
            tests("discovery")
        elif args.phase == "credentials":
            scan_argv = [sys.executable, manifest["runtime"]["detect_secrets"],
                         "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"]
            receipt["scanner_argv"] = scan_argv
            child("detect-secrets", [sys.executable, "-B", __file__, "--worker", "scanner",
                  "--temporary-root", temp, "--source-manifest", args.source_manifest])
            scan_report = row / "detect-secrets.stdout"
            scan = json.loads(scan_report.read_text())
            require(type(scan.get("results")) is dict, "scanner_json_incomplete")
            receipt["scanner_hit_count"] = sum(len(v) for v in scan["results"].values())
            receipt["secret_checker_argv"] = [sys.executable, str(REPO / CHECKERS[0]), str(scan_report)]
            child("secret-report", [sys.executable, "-B", __file__, "--worker", "secret-check",
                  "--temporary-root", temp, "--source-manifest", args.source_manifest, "--report", scan_report])
        elif args.phase == "public-tree":
            clone = row / "committed-tree"
            child("clone", ["git", "clone", "--local", "--no-hardlinks", "--no-checkout",
                            "--no-recurse-submodules", REPO, clone])
            child("checkout", ["git", "checkout", "--detach", before["head"]], cwd=clone)
            require(git("rev-parse", "HEAD", cwd=clone).decode().strip() == before["head"], "clone_HEAD")
            require(git("rev-parse", "HEAD^{tree}", cwd=clone).decode().strip() == before["tree"], "clone_tree")
            require(not (clone / ".venv").exists(), "clone_runtime_link")
            for name, hashed in before["tracked_hashes"].items():
                p = clone / name
                no_symlinks(p)
                require(digest(p.read_bytes()) == hashed, "clone_bytes:" + name)
            receipt["public_checker_argv"] = [sys.executable, str(clone / CHECKERS[1])]
            done = child("committed-tree-check", [sys.executable, "-B", __file__, "--worker", "public-check",
                         "--temporary-root", temp, "--source-manifest", args.source_manifest, "--scan-root", clone], cwd=clone)
            public = json.loads(done.stdout)
            require(public.get("findings") == [] and type(public.get("scanned_files")) is int
                    and public["scanned_files"] == len(before["tracked_hashes"]), "committed_scan_incomplete")
            done = child("in_place_informational", [sys.executable, "-B", __file__, "--worker", "public-check",
                         "--temporary-root", temp, "--source-manifest", args.source_manifest, "--scan-root", REPO], accepted=(0, 1))
            info = json.loads(done.stdout)
            require(type(info.get("scanned_files")) is int, "in_place_incomplete")
            require((done.returncode == 0 and info.get("findings") == []) or
                    (done.returncode == 1 and info.get("findings") == [[".venv", 0, "symlink_not_scanned"]]),
                    "unexpected_in_place_findings")
            receipt["committed_tree_scan"] = public
            receipt["in_place_informational"] = info
        require(identity(args.baseline) == before, "candidate_changed_during_row")
        require(digest(args.source_manifest.read_bytes()) == receipt["source_manifest_sha256"], "manifest_changed")
        receipt["exit"] = 0
    except Exception as error:
        receipt["error"] = type(error).__name__ + ": " + str(error)
    # All children have exited. Hash final raw outputs/reports, then seal once.
    receipt["evidence_hashes"] = {p.name: digest(p.read_bytes()) for p in row.iterdir()
        if p.is_file() and p.name not in ("receipt.pending.json", "receipt.json")}
    save(row / "receipt.json", receipt)
    print(json.dumps({"phase": args.phase, "baseline": args.baseline, "exit": receipt["exit"],
                      "head": receipt.get("identity", {}).get("head"),
                      "receipt": str(row / "receipt.json"),
                      "receipt_sha256": digest((row / "receipt.json").read_bytes()),
                      "error": receipt.get("error"), "review_required": True}, sort_keys=True))
    return receipt["exit"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("suite", "compatibility", "portable", "credentials", "public-tree"))
    parser.add_argument("--source-manifest", type=Path)
    parser.add_argument("--out-base", type=Path)
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--worker", choices=("suite", "compatibility", "discovery", "scanner", "secret-check", "public-check"))
    parser.add_argument("--temporary-root", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--scan-root", type=Path)
    args = parser.parse_args()
    if args.worker:
        require(args.temporary_root and args.temporary_root.is_absolute() and
                args.temporary_root.is_dir(), "worker_temporary_root_required")
        if args.worker == "scanner":
            raise SystemExit(scanner_worker(args))
        if args.worker in ("secret-check", "public-check"):
            raise SystemExit(checker_worker(args))
        raise SystemExit(run_tests(args))
    require(args.phase and args.source_manifest and args.out_base, "five_row_arguments_required")
    require(Path.cwd() == REPO, "acceptance_cwd")
    raise SystemExit(main(args))
