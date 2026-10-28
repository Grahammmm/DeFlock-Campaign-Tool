"""Bounded offline FC14 acceptance; each invocation retains a new evidence root.

Not full repository CI, privacy/legal acceptance, or release authority. No product
imports occur until the worker has redirected temporary storage and set guards.
Ordinary test discovery does not execute worker setup or install audit hooks.
"""
from contextlib import ExitStack
import argparse
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
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]

PRODUCT = "campaign_tool/records/understanding/correspondence.py"
TEST = "tests/records/test_fc14_tracker_snapshot.py"
FIXTURE = "tests/fixtures/fc14-tracker-snapshot/cases.json"
RUNNER = "tools/fc14_tracker_snapshot_acceptance.py"
AUTHORED = [PRODUCT, TEST, FIXTURE, RUNNER]
PIN_PATHS = [PRODUCT, "tests/records/test_correspondence.py"]
CHECKERS = ["tools/check_secret_report.py", "tools/check_public_tree.py", "campaign_tool/records/public_scan.py"]
MODULE = "tests.records.test_fc14_tracker_snapshot"
REQUIRED = ["test_" + name for name in ("event_citation_input", "event_citation_output", "policy_deadline_basis", "metadata_citations", "caller_unverified_mode", "two_calls", "holds_and_empty", "never_send", "declared_alias_mutant")]


def need(value, message):
    if not value:
        raise ValueError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def save(path, value):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True, ensure_ascii=False)
        stream.write("\n")


def denied(*args, **kwargs):
    raise RuntimeError("offline_worker_operation_not_admitted")


def worker(mode, temp_root, report):
    """Process-scoped guards, with no permanent changes to discovery imports."""
    temp_root = temp_root.resolve()
    temp_root.mkdir(parents=True, exist_ok=True)
    sys.dont_write_bytecode = True
    def audit(event, args):
        if event in {"socket.connect", "socket.bind", "subprocess.Popen", "os.system", "os.posix_spawn", "os.fork", "pty.spawn"}:
            denied()
        target = None
        if event == "open":
            path, access, flags = args
            if isinstance(path, (str, bytes, os.PathLike)) and (flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)):
                target = path
        elif event in {"os.mkdir", "os.remove", "os.rmdir", "os.rename"}:
            target = args[0]
            source_fd = args[2] if event in {"os.mkdir", "os.rename"} else args[1]
            if not os.path.isabs(target) and source_fd != -1:
                target = Path(os.readlink("/proc/self/fd/" + str(source_fd))) / os.fsdecode(target)
            if event == "os.rename":
                destination = Path(os.fsdecode(args[1]))
                if not destination.is_absolute() and args[3] != -1:
                    destination = Path(os.readlink("/proc/self/fd/" + str(args[3]))) / destination
                need(destination.resolve().is_relative_to(temp_root), "worker_rename_outside_fixture")
        elif event == "sqlite3.connect" and str(args[0]) != ":memory:":
            raw = str(args[0])
            target = raw[5:].split("?", 1)[0] if raw.startswith("file:") else raw
        if target is not None and not isinstance(target, int):
            need(Path(os.fsdecode(target)).resolve().is_relative_to(temp_root), "worker_write_outside_fixture")
    # This hook is intentionally confined to the isolated worker process. Never
    # installed by importing the test module under normal unittest discovery.
    sys.addaudithook(audit)
    details = {"mode": mode, "temp_root": str(temp_root), "interpreter": sys.executable,
               "ids": [], "subtests": [], "errors": [], "failures": [], "skipped": []}
    class Result(unittest.TextTestResult):
        def startTest(self, test):
            details["ids"].append(test.id())
            super().startTest(test)
        def addSubTest(self, test, subtest, err):
            details["subtests"].append({"id": subtest.id(), "passed": err is None})
            super().addSubTest(test, subtest, err)
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"TMPDIR": str(temp_root), "TEMP": str(temp_root), "TMP": str(temp_root)}))
        stack.enter_context(patch.object(tempfile, "tempdir", str(temp_root)))
        for name in ("connect", "connect_ex", "bind", "listen"):
            stack.enter_context(patch.object(socket.socket, name, denied))
        stack.enter_context(patch.object(socket, "create_connection", denied))
        stack.enter_context(patch.object(subprocess, "Popen", denied))
        stack.enter_context(patch.object(os, "system", denied))
        if mode == "compatibility":
            suite = unittest.defaultTestLoader.loadTestsFromName("tests.records.test_correspondence")
        elif mode == "discovery":
            # Verify ordinary discovery itself leaves ambient state untouched.
            before = (dict(os.environ), tempfile.tempdir, socket.socket.connect, subprocess.Popen)
            suite = unittest.defaultTestLoader.discover(str(ROOT / "tests" / "records"),
                        pattern="test_fc14_tracker_snapshot.py", top_level_dir=str(ROOT))
            need(before == (dict(os.environ), tempfile.tempdir, socket.socket.connect, subprocess.Popen), "discovery_ambient_contamination")
            class Sentinel(unittest.TestCase):
                def runTest(self):
                    self.assertEqual(dict(os.environ), before[0])
                    self.assertEqual(tempfile.tempdir, before[1])
                    self.assertIs(socket.socket.connect, before[2])
                    self.assertIs(subprocess.Popen, before[3])
            suite = unittest.TestSuite([Sentinel(), suite, Sentinel()])
        else:
            suite = unittest.defaultTestLoader.loadTestsFromName(MODULE)
        result = unittest.TextTestRunner(verbosity=2, resultclass=Result).run(suite)
        details.update(tests_run=result.testsRun,
                       errors=[{"id": t.id(), "traceback": text} for t, text in result.errors],
                       failures=[{"id": t.id(), "traceback": text} for t, text in result.failures],
                       skipped=[{"id": t.id(), "reason": reason} for t, reason in result.skipped])
        if mode != "compatibility":
            details["case_dimensions"] = json.loads((ROOT / FIXTURE).read_text())["dimensions"]
            observed = [identity.rsplit(".", 1)[-1] for identity in details["ids"] if identity.startswith(MODULE + ".")]
            need(sorted(observed) == sorted(REQUIRED), "required_case_set_mismatch")
            # Subtests must really run, not merely be listed in the fixture.
            loop_cases = ("event_citation_input", "event_citation_output", "policy_deadline_basis", "metadata_citations", "never_send")
            expected_subtests = sum(len(details["case_dimensions"][name]) for name in loop_cases)
            need(len(details["subtests"]) == expected_subtests, "required_subtest_dimensions_missing")
        need(not result.skipped, "skips_not_accepted")
    # Report is evidence, not fixture state, and is written after all test guards
    # complete. The audit hook requires it to remain inside the worker root.
    save(report, details)
    return int(not result.wasSuccessful())


class Invocation:
    def __init__(self, args):
        self.args = args
        out = args.out_base.resolve()
        need(not out.is_relative_to(ROOT), "evidence_root_inside_repository")
        out.mkdir(parents=True, exist_ok=True)
        self.out = out / (args.phase + "-" + uuid.uuid4().hex)
        self.out.mkdir()
        self.rows = []
        self.evidence = {"schema": "fc14-tracker-snapshot-evidence/v1", "phase": args.phase,
                         "baseline": args.baseline, "argv": sys.argv, "cwd": os.getcwd(),
                         "interpreter": sys.executable, "author_runtime": "gpt-6.1-sol/openai-codex",
                         "commands": self.rows, "review_acceptance": False}

    def run(self, label, argv, cwd=ROOT, expected=(0,)):
        need(isinstance(argv, list) and all(isinstance(x, str) for x in argv), "literal_argv_required")
        env = dict(os.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        done = subprocess.run(argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              shell=False, timeout=240)
        stdout, stderr = self.out / (label + ".stdout"), self.out / (label + ".stderr")
        stdout.write_bytes(done.stdout)
        stderr.write_bytes(done.stderr)
        self.rows.append({"label": label, "argv": argv, "cwd": str(cwd), "exit": done.returncode,
                          "stdout": stdout.name, "stdout_sha256": digest(done.stdout),
                          "stderr": stderr.name, "stderr_sha256": digest(done.stderr)})
        need(done.returncode in expected, label + "_unexpected_exit_" + str(done.returncode))
        return done.stdout

    def git(self, label, *args, cwd=ROOT):
        return self.run(label, ["git", *args], cwd).decode().strip()

    def identity(self, label):
        head = self.git(label + "-head", "rev-parse", "HEAD")
        tree = self.git(label + "-tree", "rev-parse", "HEAD^{tree}")
        status = self.git(label + "-status", "status", "--porcelain=v1", "--untracked-files=all")
        return {"head": head, "tree": tree, "status": status,
                "authored_sha256": {p: digest((ROOT / p).read_bytes()) for p in AUTHORED}}

    def validate(self):
        manifest = json.loads(self.args.source_manifest.read_text())
        need(manifest["schema"] == "agentorange.source-manifest/v1", "manifest_schema")
        need(Path(manifest["repo"]) == ROOT and Path(manifest["write_root"]) == ROOT.parent, "manifest_paths")
        # The operator manifest is the source identity authority. Its full file
        # digest is retained in every row and frozen in the review packet. Derive
        # source SHA256 pins from those immutable Git blobs rather than repeat
        # public high-entropy identities in scanner-visible runner source.
        base, base_tree = manifest["head"], manifest["tree"]
        need(manifest["card"] == "FC14-TRACKER-SNAPSHOT" and
             re.fullmatch(r"[0-9a-f]{40}", base) and re.fullmatch(r"[0-9a-f]{40}", base_tree), "manifest_source_identity")
        need(Path(manifest["runtime"]["repo_venv"]).resolve() == (ROOT / ".venv").resolve()
             and (ROOT / ".venv").is_symlink(), "manifest_venv_binding")
        runtime = manifest["runtime"]
        python = Path(runtime["credential_scan_python3"]) if self.args.phase == "credentials" else ROOT / ".venv/bin/python3"
        need(Path(sys.executable).absolute() == python.absolute(), "runtime_interpreter_mismatch")
        need(Path(runtime["detect_secrets"]).parent == Path(runtime["credential_scan_python3"]).parent, "scanner_runtime_mismatch")
        self.evidence.update(source_manifest_sha256=digest(self.args.source_manifest.read_bytes()),
                             source_manifest=manifest, fixture_sha256=digest((ROOT / FIXTURE).read_bytes()))
        need(self.git("base-tree", "rev-parse", base + "^{tree}") == base_tree, "base_tree_mismatch")
        self.run("base-ancestor", ["git", "merge-base", "--is-ancestor", base, "HEAD"])
        self.evidence["source_pins"] = {}
        for ordinal, path in enumerate(PIN_PATHS):
            raw = self.run("base-pin-" + str(ordinal), ["git", "show", base + ":" + path])
            expected = digest(raw)
            self.evidence["source_pins"][path] = expected
            if path != PRODUCT or self.args.baseline:
                need(digest((ROOT / path).read_bytes()) == expected, "read_only_source_changed:" + path)
        self.evidence["checker_sha256"] = {}
        for ordinal, path in enumerate(CHECKERS):
            raw = self.run("base-checker-" + str(ordinal), ["git", "show", base + ":" + path])
            actual = digest((ROOT / path).read_bytes())
            need(actual == digest(raw), "checker_changed:" + path)
            self.evidence["checker_sha256"][path] = actual
        self.evidence["before"] = self.identity("before")
        if self.args.baseline:
            need(self.evidence["before"]["head"] == base, "baseline_head_changed")
            need(self.args.phase in {"suite", "compatibility"}, "baseline_phase_not_admitted")
        else:
            need(self.evidence["before"]["status"] == "", "candidate_not_clean")
        changed = self.git("diff-paths", "diff", "--name-only", base).splitlines()
        need(set(changed) <= set(AUTHORED), "exclusive_write_set_violation")
        self.run("full-diff", ["git", "diff", "--binary", base])
        self.git("path-inventory", "ls-files", "--cached", "--others", "--exclude-standard")
        return runtime

    def tests(self, mode, label, *, script=False, unrelated=False):
        temp = self.out / label
        temp.mkdir()
        report = temp / "tests.json"
        cwd = temp if unrelated else ROOT
        if script:
            argv = [sys.executable, str(ROOT / TEST), "--temp-root", str(temp), "--report", str(report)]
        else:
            argv = [sys.executable, str(ROOT / RUNNER), "--worker", mode, "--temp-root", str(temp), "--report", str(report)]
        stdout = self.run(label, argv, cwd, expected=(0, 1) if self.args.baseline else (0,))
        data = json.loads(report.read_text())
        need(not data["skipped"] and data["tests_run"] > 0, "missing_or_skipped_tests")
        if self.args.baseline and mode == "suite":
            need(self.rows[-1]["exit"] == 1 and not data["errors"] and bool(data["failures"]), "genuine_baseline_alias_failures_required")
            need(any("snapshot" in fail["traceback"] or "unchanged" in fail["traceback"] for fail in data["failures"]), "baseline_wrong_failure_class")
        else:
            need(not data["failures"] and not data["errors"], "test_report_not_green")
        self.evidence[label] = data
        return stdout

    def execute(self):
        runtime = self.validate()
        if self.args.phase == "suite":
            self.tests("suite", "suite")
        elif self.args.phase == "compatibility":
            self.tests("compatibility", "compatibility")
        elif self.args.phase == "portable":
            for path in (TEST, FIXTURE, RUNNER):
                text = (ROOT / path).read_text()
                need(not re.search(r"/(?:home|Users|workspace)/", text), "box_specific_path:" + path)
            self.tests("suite", "absolute-script", script=True, unrelated=True)
            self.tests("discovery", "ordinary-discovery", unrelated=True)
        elif self.args.phase == "credentials":
            report = self.out / "scanner.json"
            report.write_bytes(self.run("detect-secrets", [runtime["detect_secrets"], "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"]))
            json.loads(report.read_text())
            self.run("check-secret-report", [runtime["credential_scan_python3"], str(ROOT / "tools/check_secret_report.py"), str(report)])
        elif self.args.phase == "public-tree":
            clone = self.out / "committed-tree"
            head, tree = self.evidence["before"]["head"], self.evidence["before"]["tree"]
            self.run("clone", ["git", "clone", "--local", "--no-hardlinks", "--no-checkout", "--no-recurse-submodules", str(ROOT), str(clone)])
            self.run("checkout", ["git", "checkout", "--detach", head], clone)
            need(self.git("clone-head", "rev-parse", "HEAD", cwd=clone) == head, "clone_head_mismatch")
            need(self.git("clone-tree", "rev-parse", "HEAD^{tree}", cwd=clone) == tree, "clone_tree_mismatch")
            for path, expected in self.evidence["before"]["authored_sha256"].items():
                need(digest((clone / path).read_bytes()) == expected, "clone_authored_hash_mismatch")
            public = json.loads(self.run("committed-public-tree", [sys.executable, str(clone / "tools/check_public_tree.py")], clone))
            need(public["findings"] == [] and public["scanned_files"] > 0, "committed_tree_findings")
            info = json.loads(self.run("in-place-informational", [sys.executable, str(ROOT / "tools/check_public_tree.py")], expected=(0, 1)))
            code = self.rows[-1]["exit"]
            need((code == 0 and info["findings"] == []) or (code == 1 and info["findings"] == [[".venv", 0, "symlink_not_scanned"]]), "in_place_unrecognized_finding")
            self.evidence["in_place_informational"] = info
            self.evidence["committed_public_tree"] = public
        self.evidence["after"] = self.identity("after")
        need(self.evidence["after"] == self.evidence["before"], "candidate_changed_during_acceptance")
        self.evidence["result"] = "BASELINE_EVIDENCE_ONLY" if self.args.baseline else "PASS_REVIEW_REQUIRED"

    def seal(self):
        # All subprocesses have returned and all stdout/stderr/report writers
        # exited. Do not include the clone's git internals in the receipt map.
        files = {}
        for path in sorted(self.out.rglob("*")):
            if path.is_file() and "committed-tree" not in path.relative_to(self.out).parts:
                files[path.relative_to(self.out).as_posix()] = digest(path.read_bytes())
        self.evidence["files"] = files
        save(self.out / "evidence.json", self.evidence)
        value = {"evidence": str(self.out / "evidence.json"), "sha256": digest((self.out / "evidence.json").read_bytes()),
                 "result": self.evidence.get("result", "FAIL")}
        print(json.dumps(value, sort_keys=True))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=["suite", "compatibility", "portable", "credentials", "public-tree"])
    parser.add_argument("--source-manifest", type=Path)
    parser.add_argument("--out-base", type=Path)
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--worker", choices=["suite", "compatibility", "discovery"])
    parser.add_argument("--temp-root", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.worker:
        need(args.temp_root is not None and args.report is not None, "worker_paths_required")
        return worker(args.worker, args.temp_root, args.report)
    need(args.phase is not None and args.source_manifest is not None and args.out_base is not None, "phase_paths_required")
    invocation = Invocation(args)
    code = 0
    try:
        invocation.execute()
    except Exception:
        invocation.evidence["error"] = traceback.format_exc()
        invocation.evidence["result"] = "FAIL"
        code = 1
        traceback.print_exc()
    invocation.seal()
    return code


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(ROOT))
    raise SystemExit(main())
