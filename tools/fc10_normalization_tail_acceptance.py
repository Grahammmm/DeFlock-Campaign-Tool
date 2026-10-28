"""Bounded offline acceptance; each invocation preserves a new immutable receipt set.

Not a sandbox or semantic acceptance. Scanner subprocesses retain their unchanged
repository behavior; tests use scoped offline guards before product imports.
"""
import argparse
import contextlib
import hashlib
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
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
AUTHORED = (
    "campaign_tool/records/understanding/evidence.py",
    "tests/records/test_fc10_normalization_tail.py",
    "tests/fixtures/fc10-normalization-tail/cases.json",
    "tools/fc10_normalization_tail_acceptance.py",
)
SCANNERS = ("tools/check_secret_report.py", "tools/check_public_tree.py",
            "campaign_tool/records/public_scan.py")
SUITE = "tests.records.test_fc10_normalization_tail"
COMPAT = "tests.records.test_understanding.EvidenceTests"


def need(ok, message):
    if not ok:
        raise ValueError(message)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, data):
    Path(path).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def canonical_path(path):
    path = Path(path)
    need(path.is_absolute() and str(path) == str(path.resolve()), "noncanonical_path:" + str(path))
    return path


def offline():
    stack = contextlib.ExitStack()
    for target in ("socket.socket", "socket.create_connection", "socket.getaddrinfo",
                   "subprocess.Popen", "os.system", "os.popen", "sqlite3.connect"):
        stack.enter_context(mock.patch(target, side_effect=RuntimeError("offline_call_forbidden")))
    return stack


def ids(suite):
    result = []
    for item in suite:
        result.extend(ids(item) if isinstance(item, unittest.TestSuite) else [item.id()])
    return result


def worker(kind, output):
    """Fresh child only; temporary policy and guards precede product imports."""
    sys.dont_write_bytecode = True
    tempfile.tempdir = str(output)
    sys.path.insert(0, str(REPO))
    with offline():
        loader = unittest.TestLoader()
        if kind == "discovery":
            import socket
            before = (dict(os.environ), tempfile.tempdir, socket.socket, subprocess.Popen, tuple(sys.path))
            suite = loader.discover(str(REPO / "tests" / "records"),
                                    pattern="test_fc10_normalization_tail.py", top_level_dir=str(REPO))
            after = (dict(os.environ), tempfile.tempdir, socket.socket, subprocess.Popen, tuple(sys.path))
            need(before == after, "discovery_ambient_contamination")
            class Sentinel(unittest.TestCase):
                def runTest(self):
                    self.assertEqual((dict(os.environ), tempfile.tempdir, socket.socket,
                                      subprocess.Popen, tuple(sys.path)), before)
            suite = unittest.TestSuite([Sentinel(), suite, Sentinel()])
        else:
            suite = loader.loadTestsFromName(COMPAT if kind == "compatibility" else SUITE)
        names = ids(suite)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        summary = {"kind": kind, "test_ids": names, "tests_run": result.testsRun,
                   "failures": [(t.id(), s) for t, s in result.failures],
                   "errors": [(t.id(), s) for t, s in result.errors],
                   "skips": [(t.id(), s) for t, s in result.skipped],
                   "fixture_cases": len(json.loads((REPO / AUTHORED[2]).read_text())["cases"])}
        save(output / "test-summary.json", summary)
        return 0 if result.wasSuccessful() and not result.skipped else 1


class Run:
    def __init__(self, args):
        self.args = args
        self.manifest_path = canonical_path(args.source_manifest)
        self.source = json.loads(self.manifest_path.read_text())
        need(self.source["schema"] == "agentorange.source-manifest/v1", "manifest_schema")
        need(canonical_path(self.source["repo"]) == REPO, "manifest_repo")
        root = canonical_path(self.source["write_root"])
        need(REPO == root / "repo", "write_root")
        need(canonical_path(args.out_base) == root / "out", "out_base")
        need(Path.cwd() == REPO, "acceptance_cwd")
        self.output = args.out_base / (args.phase + "-" + str(uuid.uuid4()))
        self.output.mkdir(parents=True, mode=0o700)
        tempfile.tempdir = str(self.output)
        self.commands = []
        self.details = {}
        self.head = None
        self.tree = None

    def command(self, argv, label, cwd=None, allowed=(0,)):
        need(isinstance(argv, list) and all(isinstance(x, str) for x in argv), "literal_argv_required")
        cwd = Path(cwd or REPO)
        result = subprocess.run(argv, cwd=cwd, shell=False, capture_output=True)
        stdout = self.output / (label + ".stdout")
        stderr = self.output / (label + ".stderr")
        stdout.write_bytes(result.stdout)
        stderr.write_bytes(result.stderr)
        record = {"argv": argv, "cwd": str(cwd), "exit": result.returncode,
                  "interpreter": argv[0], "stdout": str(stdout), "stderr": str(stderr),
                  "stdout_sha256": digest(stdout), "stderr_sha256": digest(stderr)}
        self.commands.append(record)
        need(result.returncode in allowed, "child_failed:" + label + ":" + str(result.returncode))
        return result.stdout.decode()

    def git(self, args, label, cwd=None):
        return self.command(["git", *args], label, cwd).strip()

    def validate(self):
        source = self.source
        runtime = source["runtime"]
        need((REPO / ".venv").is_symlink(), "standard_venv_link_required")
        need((REPO / ".venv").resolve() == canonical_path(runtime["repo_venv"]), "venv_pin")
        expected = (runtime["credential_scan_python3"] if self.args.phase == "credentials"
                    else str(REPO / ".venv" / "bin" / "python3"))
        need(os.path.abspath(sys.executable) == expected, "interpreter_pin")
        for key in ("credential_scan_python3", "detect_secrets"):
            p = Path(runtime[key])
            need(p.parent == Path(runtime["repo_venv"]) / "bin" and p.is_file(), "runtime_tool_pin")
        self.head, self.tree = self.git(["rev-parse", "HEAD", "HEAD^{tree}"], "identity").splitlines()
        need(re.fullmatch(r"[0-9a-f]{40}", source["head"]) is not None, "base_head_format")
        base_tree = self.git(["rev-parse", source["head"] + "^{tree}"], "base-tree")
        need(base_tree == source["tree"], "base_tree_pin")
        self.git(["merge-base", "--is-ancestor", source["head"], self.head], "ancestry")
        self.details["mode"] = "pinned_baseline_expected_red" if self.head == source["head"] else "candidate_review_required"
        changed = self.git(["diff", "--name-only", source["head"]], "changed-paths").splitlines()
        need(set(changed) <= set(AUTHORED), "exclusive_write_set")
        self.details["authored_sha256"] = {p: digest(REPO / p) for p in AUTHORED}
        self.details["scanner_sha256"] = {p: digest(REPO / p) for p in SCANNERS}
        for i, p in enumerate(SCANNERS):
            raw = self.command(["git", "show", source["head"] + ":" + p], "original-scanner-" + str(i))
            need(hashlib.sha256(raw.encode()).hexdigest() == digest(REPO / p), "scanner_changed")
        self.details["source_manifest_sha256"] = digest(self.manifest_path)
        if self.head != source["head"]:
            self.clean("initial")

    def clean(self, label):
        status = self.git(["status", "--porcelain=v1", "--untracked-files=all"], label + "-status")
        need(not status, "dirty_candidate")
        inventory = self.git(["ls-files", "--cached", "--others", "--exclude-standard"], label + "-inventory").splitlines()
        tracked = self.git(["ls-files"], label + "-tracked").splitlines()
        need(set(inventory) - set(tracked) <= {".venv"}, "scan_visible_untracked")
        self.git(["diff", "--exit-code", "HEAD"], label + "-head-diff")
        self.git(["diff", "--cached", "--exit-code", "HEAD"], label + "-index-diff")
        for i, p in enumerate(AUTHORED):
            raw = self.command(["git", "show", self.head + ":" + p], label + "-committed-" + str(i))
            need(hashlib.sha256(raw.encode()).hexdigest() == digest(REPO / p), "uncommitted_authored_bytes")

    def tests(self, kind):
        child = self.output / (kind + "-child")
        child.mkdir(mode=0o700)
        self.command([sys.executable, str(Path(__file__).resolve()), "--worker", kind,
                      "--output-dir", str(child)], kind)
        data = json.loads((child / "test-summary.json").read_text())
        self.details[kind] = data
        expected = 5 if kind == "compatibility" else 17 if kind == "discovery" else 15
        need(data["tests_run"] == expected and not data["errors"] and not data["failures"] and not data["skips"], "test_counts")

    def portable(self):
        for p in AUTHORED[1:]:
            text = (REPO / p).read_text()
            need(not re.search(r"/(?:home|Users|workspace)/", text), "host_only_literal:" + p)
        child = self.output / "portable-cwd"
        child.mkdir(mode=0o700)
        output = self.command([sys.executable, str(REPO / AUTHORED[1]), "--temp-root", str(child)], "absolute-test", child)
        record = self.commands[-1]
        raw = Path(record["stderr"]).read_text()
        need(re.search(r"Ran 15 tests", raw) is not None and "\nOK\n" in raw, "portable_test_counts")
        observations = [json.loads(line) for line in output.splitlines()]
        need(len({o["case"] for o in observations if "case" in o}) == 13, "portable_case_coverage")
        self.details["absolute_test"] = {"tests_run": 15, "fixture_cases": 13, "observations": observations}
        self.tests("discovery")

    def credentials(self):
        runtime = self.source["runtime"]
        # The installed scanner uses os.walk without followlinks for --all-files;
        # the standard directory symlink is not traversed. Invoke it unchanged.
        output = self.command([runtime["detect_secrets"], "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"], "detect-secrets")
        report = self.output / "detect-secrets.stdout"
        data = json.loads(output)
        need(isinstance(data.get("results"), dict), "credential_report_shape")
        need(not any(p.startswith(".venv/") or p == ".venv" for p in data["results"]), "dependency_scan_boundary")
        self.command([runtime["credential_scan_python3"], str(REPO / "tools/check_secret_report.py"), str(report)], "check-secret-report")
        self.details["credentials"] = {"scan_result_paths": sorted(data["results"]), "both_exits": 0}

    def public_tree(self):
        self.clean("public-before")
        clone = self.output / "committed-tree"
        self.command(["git", "clone", "--local", "--no-hardlinks", "--no-checkout", "--no-recurse-submodules", str(REPO), str(clone)], "clone")
        self.command(["git", "checkout", "--detach", self.head], "checkout", clone)
        identity = self.git(["rev-parse", "HEAD", "HEAD^{tree}"], "clone-identity", clone).splitlines()
        need(identity == [self.head, self.tree], "clone_identity")
        need(not (clone / ".venv").exists(), "clone_runtime_link_forbidden")
        for p in (*AUTHORED, *SCANNERS):
            need(digest(clone / p) == digest(REPO / p), "clone_hash:" + p)
        output = self.command([sys.executable, str(clone / "tools/check_public_tree.py")], "committed-public-scan", clone)
        data = json.loads(output)
        need(data.get("findings") == [] and data.get("scanned_files", 0) > 0, "public_findings")
        self.details["committed_tree"] = data
        output = self.command([sys.executable, str(REPO / "tools/check_public_tree.py")], "in-place-informational", allowed=(0, 1))
        data = json.loads(output)
        findings = data.get("findings")
        code = self.commands[-1]["exit"]
        need((code == 0 and findings == []) or (code == 1 and findings == [[".venv", 0, "symlink_not_scanned"]]), "unexpected_in_place_findings")
        need((REPO / ".venv").resolve() == Path(self.source["runtime"]["repo_venv"]), "venv_changed")
        self.details["in_place_informational_INC_174"] = data
        need(self.git(["rev-parse", "HEAD", "HEAD^{tree}"], "source-recheck").splitlines() == identity, "source_changed")
        self.clean("public-after")

    def finish(self, exit_code, error=None):
        save(self.output / "receipt.json", {
            "schema": "fc10-normalization-tail-receipt/v1", "argv": sys.argv,
            "cwd": str(Path.cwd()), "interpreter": sys.executable,
            "candidate_head": self.head, "candidate_tree": self.tree, "phase": self.args.phase,
            "exit": exit_code, "error": error, "commands": self.commands, "details": self.details,
            "limitations": "Offline REVIEW REQUIRED; remote exact-head CI OWED to operator; no semantic/legal/human/live acceptance; scanners pattern-only, manual privacy review owed.",
        })
        paths = sorted(p for p in self.output.rglob("*") if p.is_file())
        entries = []
        seen = set()
        for path in paths:
            need(not path.is_symlink() and path.resolve() == path, "output_alias")
            relative = path.relative_to(self.output).as_posix()
            need(relative not in seen, "duplicate_output")
            seen.add(relative)
            entries.append({"path": relative, "sha256": digest(path), "bytes": path.stat().st_size})
        manifest = self.output / "evidence-manifest.json"
        save(manifest, {"schema": "fc10-output-manifest/v1", "root": str(self.output),
                        "self_excluded": "evidence-manifest.json", "files": entries})
        recorded = json.loads(manifest.read_text())["files"]
        actual = {p.relative_to(self.output).as_posix() for p in self.output.rglob("*") if p.is_file() and p != manifest}
        need(actual == {e["path"] for e in recorded}, "omitted_output")
        for entry in recorded:
            need(digest(self.output / entry["path"]) == entry["sha256"], "output_hash_invalid")
        print(json.dumps({"phase": self.args.phase, "exit": exit_code, "head": self.head,
                          "manifest": str(manifest), "manifest_sha256": digest(manifest),
                          "files": len(entries), "error": error}, sort_keys=True))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("suite", "compatibility", "portable", "credentials", "public-tree"))
    parser.add_argument("--source-manifest", type=Path)
    parser.add_argument("--out-base", type=Path)
    parser.add_argument("--worker", choices=("suite", "compatibility", "discovery"))
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    if args.worker:
        return worker(args.worker, canonical_path(args.output_dir))
    need(args.phase and args.source_manifest and args.out_base, "missing_acceptance_arguments")
    run = Run(args)
    error = None
    code = 0
    try:
        run.validate()
        if args.phase in ("suite", "compatibility"):
            run.tests(args.phase)
        elif args.phase == "portable":
            run.portable()
        elif args.phase == "credentials":
            run.credentials()
        else:
            run.public_tree()
    except Exception:
        code = 1
        error = traceback.format_exc()
    run.finish(code, error)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
