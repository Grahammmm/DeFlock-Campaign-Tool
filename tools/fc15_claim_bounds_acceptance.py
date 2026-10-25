"""FC15-CLAIM-BOUNDS bounded offline acceptance recorder (five frozen rows).

Each invocation writes a fresh UUID evidence directory beneath --out-base and
outside the repository. It records existing tests and unchanged repository
checkers only; it is not a product checker, full repository CI, or any form
of semantic, legal or release acceptance. Result is PASS_REVIEW_REQUIRED at best.
Ordinary unittest discovery never imports this module's worker setup.
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
CARD = "FC15-CLAIM-BOUNDS"
TEST = "tests/records/test_fc15_claim_bounds.py"
FIXTURE = "tests/fixtures/fc15-claim-bounds/cases.json"
RUNNER = "tools/fc15_claim_bounds_acceptance.py"
AUTHORED = [TEST, FIXTURE, RUNNER]
PIN_PATHS = ["campaign_tool/records/grounded_evidence.py", "tests/records/test_understanding.py"]
CHECKERS = ["tools/check_secret_report.py", "tools/check_public_tree.py", "campaign_tool/records/public_scan.py"]
MODULE = "tests.records.test_fc15_claim_bounds"
CLASS = MODULE + ".ClaimBoundsTests"
SELECTORS = ["tests.records.test_understanding.EvidenceTests", "tests.records.test_understanding.GroundedEvidenceTests"]
REQUIRED = ["test_text_bytes", "test_support_count", "test_container_types", "test_replay",
            "test_hold_invariants", "test_text_limit_mutant", "test_support_limit_mutant"]
CONTROL_LINES = ["CONTROL: text-limit mutant killed by evidence_claim_bound",
                 "CONTROL: support-limit mutant killed by evidence_support_bound"]
AUTHOR_RUNTIME = "claude-opus-5-5/anthropic on opus-coder-bopus (kanban run 2131)"


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


def expected_subtests():
    """Subtest identities required from the frozen fixture (count per test)."""
    f = json.loads((ROOT / FIXTURE).read_text(encoding="utf-8"))
    accepted = [r for r in f["text_bytes"] if r["expect"] == "ok"]
    return {
        "test_text_bytes": len(f["text_bytes"]),
        "test_support_count": len(f["support_count"]),
        "test_container_types": len(f["container_types"]["claims"]) + len(f["container_types"]["supports"]),
        "test_replay": len(f["replay"]["accepted"]) + len(f["replay"]["forged"]),
        "test_hold_invariants": 2 * len(accepted),
        "test_text_limit_mutant": 8,
        "test_support_limit_mutant": 2 + len([r for r in f["support_count"] if r["expect"] == "ok"]),
    }


def worker(mode, temp_root, report, import_root=None):
    """Process-scoped offline guards; installed only in this dedicated process."""
    temp_root = temp_root.resolve()
    temp_root.mkdir(parents=True, exist_ok=True)
    sys.dont_write_bytecode = True
    if import_root is not None:
        sys.path.insert(0, str(import_root.resolve()))

    def audit(event, args):
        if event in {"socket.connect", "socket.bind", "subprocess.Popen", "os.system", "os.posix_spawn",
                     "os.fork", "os.exec", "pty.spawn"}:
            denied()
        target = None
        if event == "open":
            path, _mode, flags = args
            if isinstance(path, (str, bytes, os.PathLike)) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
                target = path
        elif event in {"os.mkdir", "os.remove", "os.rmdir", "os.rename", "os.unlink"}:
            target = args[0]
        if target is not None and not isinstance(target, int):
            need(Path(os.fsdecode(target)).resolve().is_relative_to(temp_root), "worker_write_outside_fixture")
    sys.addaudithook(audit)
    details = {"mode": mode, "temp_root": str(temp_root), "interpreter": sys.executable,
               "import_root": str(import_root) if import_root else str(ROOT),
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
            suite = unittest.defaultTestLoader.loadTestsFromNames(SELECTORS)
        elif mode == "discovery":
            before = (dict(os.environ), tempfile.tempdir, socket.socket.connect, subprocess.Popen, list(sys.path))
            found = unittest.defaultTestLoader.discover(str(ROOT / "tests" / "records"),
                                                        pattern="test_fc15_claim_bounds.py", top_level_dir=str(ROOT))
            need(before[:4] == (dict(os.environ), tempfile.tempdir, socket.socket.connect, subprocess.Popen),
                 "discovery_ambient_contamination")

            class Sentinel(unittest.TestCase):
                def runTest(self):
                    self.assertEqual(dict(os.environ), before[0])
                    self.assertEqual(tempfile.tempdir, before[1])
                    self.assertIs(socket.socket.connect, before[2])
                    self.assertIs(subprocess.Popen, before[3])
            suite = unittest.TestSuite([Sentinel(), found, Sentinel()])
        else:
            suite = unittest.defaultTestLoader.loadTestsFromName(MODULE)
        result = unittest.TextTestRunner(stream=sys.stdout, verbosity=2, resultclass=Result).run(suite)
        loaded = sys.modules.get("campaign_tool.records.grounded_evidence")
        details["grounded_evidence_file"] = getattr(loaded, "__file__", None)
        details.update(tests_run=result.testsRun,
                       errors=[{"id": t.id(), "traceback": text} for t, text in result.errors],
                       failures=[{"id": t.id(), "traceback": text} for t, text in result.failures],
                       skipped=[{"id": t.id(), "reason": reason} for t, reason in result.skipped])
    if mode != "compatibility":
        observed = sorted(i.rsplit(".", 1)[-1] for i in details["ids"] if i.startswith(CLASS + "."))
        need(observed == sorted(REQUIRED), "required_case_set_mismatch")
        counts = {}
        for sub in details["subtests"]:
            name = sub["id"].split(" ", 1)[0].rsplit(".", 1)[-1]
            counts[name] = counts.get(name, 0) + 1
        details["subtest_counts"] = counts
        need(counts == expected_subtests(), "required_subtest_dimensions_missing")
        need(all(sub["passed"] for sub in details["subtests"]), "subtest_failed")
    need(not result.skipped, "skips_not_accepted")
    save(report, details)
    return int(not result.wasSuccessful())


class Invocation:
    def __init__(self, args):
        self.args = args
        base = args.out_base.resolve()
        need(not base.is_relative_to(ROOT), "evidence_root_inside_repository")
        base.mkdir(parents=True, exist_ok=True)
        self.out = base / (args.phase + "-" + uuid.uuid4().hex)
        self.out.mkdir()
        self.rows = []
        self.evidence = {"schema": "fc15-claim-bounds-evidence/v1", "card": CARD, "phase": args.phase,
                         "argv": sys.argv, "cwd": os.getcwd(), "interpreter": sys.executable,
                         "author_runtime": AUTHOR_RUNTIME, "commands": self.rows,
                         "review_acceptance": False, "semantic_acceptance": False,
                         "full_discovery": "OWED: operator-run in venv-fullsuite; exact-head CI owed to shipping operator"}

    def run(self, label, argv, cwd=ROOT, expected=(0,)):
        need(isinstance(argv, list) and all(isinstance(x, str) for x in argv), "literal_argv_required")
        env = dict(os.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        done = subprocess.run(argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              shell=False, timeout=600)
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
        return {"head": self.git(label + "-head", "rev-parse", "HEAD"),
                "tree": self.git(label + "-tree", "rev-parse", "HEAD^{tree}"),
                "status": self.git(label + "-status", "status", "--porcelain=v1", "--untracked-files=all"),
                "authored_sha256": {p: digest((ROOT / p).read_bytes()) for p in AUTHORED}}

    def validate(self):
        raw = self.args.source_manifest.read_bytes()
        manifest = json.loads(raw)
        need(manifest["schema"] == "agentorange.source-manifest/v1" and manifest["card"] == CARD, "manifest_schema")
        need(Path(manifest["repo"]) == ROOT and Path(manifest["write_root"]) == ROOT.parent, "manifest_paths")
        base, base_tree = manifest["head"], manifest["tree"]
        need(re.fullmatch(r"[0-9a-f]{40}", base) and re.fullmatch(r"[0-9a-f]{40}", base_tree), "manifest_source_identity")
        runtime = manifest["runtime"]
        need((ROOT / ".venv").is_symlink() and Path(runtime["repo_venv"]).resolve() == (ROOT / ".venv").resolve(),
             "manifest_venv_binding")
        python = Path(runtime["credential_scan_python3"]) if self.args.phase == "credentials" else ROOT / ".venv/bin/python3"
        need(Path(sys.executable).absolute() == python.absolute(), "runtime_interpreter_mismatch")
        need(Path(runtime["detect_secrets"]).parent == Path(runtime["credential_scan_python3"]).parent, "scanner_runtime_mismatch")
        self.base = base
        self.evidence.update(source_manifest_sha256=digest(raw), source_manifest=manifest,
                             fixture_sha256=digest((ROOT / FIXTURE).read_bytes()))
        need(self.git("base-tree", "rev-parse", base + "^{tree}") == base_tree, "base_tree_mismatch")
        self.run("base-ancestor", ["git", "merge-base", "--is-ancestor", base, "HEAD"])
        self.evidence["source_pins"] = {}
        for ordinal, path in enumerate(PIN_PATHS):
            expected = digest(self.run("base-pin-" + str(ordinal), ["git", "show", base + ":" + path]))
            need(digest((ROOT / path).read_bytes()) == expected, "read_only_source_changed:" + path)
            self.evidence["source_pins"][path] = expected
        self.evidence["checker_sha256"] = {}
        for ordinal, path in enumerate(CHECKERS):
            expected = digest(self.run("base-checker-" + str(ordinal), ["git", "show", base + ":" + path]))
            need(digest((ROOT / path).read_bytes()) == expected, "checker_changed:" + path)
            self.evidence["checker_sha256"][path] = expected
        self.evidence["before"] = self.identity("before")
        need(self.evidence["before"]["status"] == "", "candidate_not_clean")
        need(self.evidence["before"]["head"] != base, "candidate_head_is_base")
        changed = self.git("diff-paths", "diff", "--name-only", base, "HEAD").splitlines()
        need(sorted(changed) == sorted(AUTHORED), "exclusive_write_set_violation")
        self.evidence["changed_paths"] = changed
        self.run("full-diff", ["git", "diff", "--binary", base, "HEAD"])
        self.run("commit-log", ["git", "log", "--format=%H%n%an <%ae>%n%B", base + "..HEAD"])
        self.git("path-inventory", "ls-files", "--cached", "--others", "--exclude-standard")
        return runtime

    def tests(self, mode, label, *, script=False, unrelated=False, import_root=None):
        temp = self.out / label
        temp.mkdir()
        report = temp / "tests.json"
        cwd = temp if unrelated else ROOT
        if script:
            argv = [sys.executable, str(ROOT / TEST), "--temp-root", str(temp), "--report", str(report)]
        else:
            argv = [sys.executable, str(ROOT / RUNNER), "--worker", mode, "--temp-root", str(temp), "--report", str(report)]
            if import_root is not None:
                argv += ["--import-root", str(import_root)]
        stdout = self.run(label, argv, cwd)
        data = json.loads(report.read_text(encoding="utf-8"))
        need(data["tests_run"] > 0 and not data["skipped"], "missing_or_skipped_tests")
        need(not data["failures"] and not data["errors"], "test_report_not_green")
        expected_root = Path(import_root) if import_root is not None else ROOT
        need(Path(data["grounded_evidence_file"]).resolve().is_relative_to(expected_root.resolve()), "product_import_root_mismatch")
        self.evidence[label] = data
        return stdout, data

    def clone(self, label, commit):
        target = self.out / label
        self.run(label + "-clone", ["git", "clone", "--local", "--no-hardlinks", "--no-checkout",
                                    "--no-recurse-submodules", str(ROOT), str(target)])
        self.run(label + "-checkout", ["git", "checkout", "--detach", commit], target)
        need(self.git(label + "-clone-head", "rev-parse", "HEAD", cwd=target) == commit, label + "_clone_head_mismatch")
        return target

    def execute(self):
        runtime = self.validate()
        phase = self.args.phase
        if phase == "suite":
            stdout, data = self.tests("suite", "suite")
            text = stdout.decode("utf-8") + (self.out / "suite.stderr").read_text(encoding="utf-8")
            for line in CONTROL_LINES:
                need(line in text, "deciding_control_missing:" + line)
            self.evidence["deciding_controls"] = CONTROL_LINES
        elif phase == "compatibility":
            baseline = self.clone("baseline-tree", self.base)
            _, before = self.tests("compatibility", "compatibility-baseline", import_root=baseline)
            _, after = self.tests("compatibility", "compatibility-final")
            need(before["ids"] == after["ids"] and before["tests_run"] == after["tests_run"], "compatibility_delta")
            self.evidence["compatibility_delta"] = {"baseline_tests": before["tests_run"], "final_tests": after["tests_run"],
                                                    "ids_equal": True, "behavior_delta": "none measured"}
        elif phase == "portable":
            for path in AUTHORED:
                text = (ROOT / path).read_text(encoding="utf-8")
                need(not re.search(r"/(?:home|Users|root|workspace|tmp)/", text), "box_specific_path:" + path)
            self.tests("suite", "absolute-script", script=True, unrelated=True)
            self.tests("discovery", "ordinary-discovery", unrelated=True)
        elif phase == "credentials":
            report = self.out / "scanner.json"
            report.write_bytes(self.run("detect-secrets", [runtime["detect_secrets"], "scan", "--all-files",
                                                           "--exclude-files", "^.git/", "--no-verify"]))
            json.loads(report.read_text(encoding="utf-8"))
            self.run("check-secret-report", [runtime["credential_scan_python3"], str(ROOT / "tools/check_secret_report.py"), str(report)])
        elif phase == "public-tree":
            head, tree = self.evidence["before"]["head"], self.evidence["before"]["tree"]
            clone = self.clone("committed-tree", head)
            need(self.git("committed-tree-clone-tree", "rev-parse", "HEAD^{tree}", cwd=clone) == tree, "clone_tree_mismatch")
            need(not (clone / ".venv").exists(), "clone_has_runtime_link")
            for path, expected in self.evidence["before"]["authored_sha256"].items():
                need(digest((clone / path).read_bytes()) == expected, "clone_authored_hash_mismatch:" + path)
            public = json.loads(self.run("committed-public-tree", [sys.executable, str(clone / "tools/check_public_tree.py")], clone))
            need(public["findings"] == [] and public["scanned_files"] > 0, "committed_tree_findings")
            self.evidence["committed_public_tree"] = public
            info = json.loads(self.run("in-place-informational", [sys.executable, str(ROOT / "tools/check_public_tree.py")],
                                       expected=(0, 1)))
            code = self.rows[-1]["exit"]
            need((code == 0 and info["findings"] == []) or
                 (code == 1 and info["findings"] == [[".venv", 0, "symlink_not_scanned"]]), "in_place_unrecognized_finding")
            self.evidence["in_place_informational"] = {"label": "in_place_informational (INC-174)", "exit": code, "report": info}
        self.evidence["after"] = self.identity("after")
        need(self.evidence["after"] == self.evidence["before"], "candidate_changed_during_acceptance")
        self.evidence["result"] = "PASS_REVIEW_REQUIRED"

    def seal(self):
        # Every child process has exited; clone git internals are excluded from the map.
        files = {}
        for path in sorted(self.out.rglob("*")):
            parts = path.relative_to(self.out).parts
            if path.is_file() and parts[0] not in {"committed-tree", "baseline-tree"}:
                files[path.relative_to(self.out).as_posix()] = digest(path.read_bytes())
        self.evidence["files"] = files
        save(self.out / "evidence.json", self.evidence)
        print(json.dumps({"evidence": str(self.out / "evidence.json"),
                          "sha256": digest((self.out / "evidence.json").read_bytes()),
                          "result": self.evidence.get("result", "FAIL")}, sort_keys=True))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=["suite", "compatibility", "portable", "credentials", "public-tree"])
    parser.add_argument("--source-manifest", type=Path)
    parser.add_argument("--out-base", type=Path)
    parser.add_argument("--worker", choices=["suite", "compatibility", "discovery"])
    parser.add_argument("--temp-root", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--import-root", type=Path)
    args = parser.parse_args()
    if args.worker:
        need(args.temp_root is not None and args.report is not None, "worker_paths_required")
        return worker(args.worker, args.temp_root, args.report, args.import_root)
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
