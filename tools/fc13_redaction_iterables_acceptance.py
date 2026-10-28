"""Offline FC13 acceptance. Fresh per-invocation receipts; no evidence reuse.

Actual author: gpt-6.1-sol / openai-codex. Scoped local checks are not full CI.
Only the five admitted phases and their explicitly bounded children run here.
"""
import argparse
from contextlib import ExitStack
import hashlib
import importlib
import itertools
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
TEST = "tests/records/test_fc13_redaction_iterables.py"
FIXTURE = "tests/fixtures/fc13-redaction-iterables/cases.json"
RUNNER = "tools/fc13_redaction_iterables_acceptance.py"
BUILD = "campaign_tool/digest/build.py"
AUTHORED = (BUILD, TEST, FIXTURE, RUNNER)
SELECTORS = ("tests.test_model_input", "tests.test_redact")
BOUND = ("campaign_tool/digest/model_input.py", "campaign_tool/digest/model.py", "tests/test_model_input.py",
         "tools/check_secret_report.py", "tools/check_public_tree.py", "campaign_tool/records/public_scan.py", ".gitignore")
SCENARIOS = ("citation", "duty", "excerpt", "allow_rule", "combined_source")
KINDS = ("list", "tuple", "generator")
sys.dont_write_bytecode = True


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def dump(path, data):
    Path(path).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def expected_ids():
    prefix = "tests.records.test_fc13_redaction_iterables.RedactionIterableTests."
    names = ["test_consumed_once_and_shared_immutable_unmodified_snapshots", "test_repeated_fresh_equivalent_generators",
             "test_defaults_equal_explicit_empty", "test_privacy_mismatch_precedes_redaction_and_narrative",
             "test_deciding_materialization_bypass_and_restoration"]
    names += [f"test_matrix_{s}_{a}_{d}" for s, a, d in itertools.product(SCENARIOS, KINDS, KINDS)]
    names += [f"test_{family}_{a}_{d}" for family in ("empty", "detector_only") for a, d in itertools.product(KINDS, KINDS)]
    return sorted(prefix + name for name in names)


def child(args):
    root = args.temporary_root.resolve()
    require(root.is_dir(), "child temporary root missing")
    sys.path.insert(0, str(REPO))
    def forbidden(*a, **kw):
        raise RuntimeError("FC13 offline child: network/process call forbidden")
    with ExitStack() as stack:
        stack.enter_context(mock.patch.dict(os.environ, {"TMPDIR": str(root), "TEMP": str(root), "TMP": str(root)}))
        stack.enter_context(mock.patch.object(tempfile, "tempdir", str(root)))
        for name in ("connect", "connect_ex", "bind", "listen", "sendto"):
            stack.enter_context(mock.patch.object(socket.socket, name, forbidden))
        for name in ("create_connection", "getaddrinfo"):
            stack.enter_context(mock.patch.object(socket, name, forbidden))
        stack.enter_context(mock.patch("urllib.request.urlopen", forbidden))
        stack.enter_context(mock.patch("http.client.HTTPConnection.connect", forbidden))
        stack.enter_context(mock.patch.object(subprocess, "Popen", forbidden))
        # All redirection/sentinels precede imports of product or test modules.
        ambient = (dict(os.environ), tempfile.tempdir, list(sys.path), socket.create_connection, subprocess.Popen)
        def sentinel():
            unittest.TestCase().assertEqual((dict(os.environ), tempfile.tempdir, list(sys.path), socket.create_connection, subprocess.Popen), ambient)
            unittest.TestCase().assertEqual(tempfile.gettempdir(), str(root))
        loader = unittest.TestLoader()
        if args.child == "compatibility":
            suite = loader.loadTestsFromNames(SELECTORS)
        elif args.child == "discovery":
            sentinel()
            discovered = loader.discover(str(REPO / "tests/records"), pattern=Path(TEST).name, top_level_dir=str(REPO))
            sentinel()
            suite = unittest.TestSuite([unittest.FunctionTestCase(sentinel, description="unrelated-before"), discovered,
                                        unittest.FunctionTestCase(sentinel, description="unrelated-after")])
        else:
            suite = loader.loadTestsFromName("tests.records.test_fc13_redaction_iterables")
        def ids(node):
            if isinstance(node, unittest.TestSuite):
                return [name for item in node for name in ids(item)]
            return [node.id()]
        planned = ids(suite)
        require(not loader.errors and not any("_FailedTest" in name for name in planned), "test loading failed")
        class Result(unittest.TextTestResult):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self.started = []
            def startTest(self, test):
                self.started.append(test.id())
                super().startTest(test)
        result = unittest.TextTestRunner(verbosity=2, resultclass=Result).run(suite)
        report = {"planned_ids": planned, "test_ids": result.started, "tests_run": result.testsRun,
                  "failures": len(result.failures), "errors": len(result.errors), "skipped": len(result.skipped),
                  "failure_ids": [test.id() for test, text in result.failures],
                  "error_ids": [test.id() for test, text in result.errors],
                  "dimensions": json.loads((REPO / FIXTURE).read_text()),
                  "ambient_unchanged": (dict(os.environ), tempfile.tempdir, list(sys.path), socket.create_connection, subprocess.Popen) == ambient}
        dump(root / "child-report.json", report)
        require(report["ambient_unchanged"], "ambient contamination")
        require(result.started == planned, "missing or extra test execution")
        if args.child != "compatibility":
            require(sorted(name for name in planned if "RedactionIterableTests." in name) == expected_ids(), "frozen matrix incomplete")
        return 0 if result.wasSuccessful() and not result.skipped else 1


class Acceptance:
    def __init__(self, args):
        self.args = args
        base = args.out_base.resolve()
        require(base != REPO and REPO not in base.parents, "output must be outside repo")
        # Operator-staged location, not arbitrary filesystem acquisition.
        admitted_out = REPO.parent / "out"
        require(base == admitted_out or admitted_out in base.parents, "out-base must stay beneath candidate/out")
        base.mkdir(exist_ok=True)
        self.root = base / (args.phase + "-" + str(uuid.uuid4()))
        self.root.mkdir()
        self.receipt = {"phase": args.phase, "argv": sys.argv, "cwd": str(Path.cwd()),
                        "interpreter": sys.executable, "author_model": "gpt-6.1-sol", "author_provider": "openai-codex",
                        "commands": [], "status": "INCOMPLETE", "full_repository_ci": "OWED to shipping operator"}

    def run(self, label, argv, cwd=REPO, allowed=(0,)):
        require(isinstance(argv, list) and all(isinstance(value, str) for value in argv), "argv must be literal string list")
        child_env = dict(os.environ)
        child_env.update({"TMPDIR": str(self.root), "TMP": str(self.root), "TEMP": str(self.root),
                          "PYTHONDONTWRITEBYTECODE": "1"})
        result = subprocess.run(argv, cwd=cwd, shell=False, capture_output=True, env=child_env)
        out, err = self.root / (label + ".stdout"), self.root / (label + ".stderr")
        out.write_bytes(result.stdout)
        err.write_bytes(result.stderr)
        self.receipt["commands"].append({"label": label, "argv": argv, "cwd": str(cwd), "exit": result.returncode,
                                         "stdout": out.name, "stderr": err.name, "stdout_sha256": sha(out), "stderr_sha256": sha(err)})
        require(result.returncode in allowed, label + " unexpected exit " + str(result.returncode))
        return result

    def git(self, label, *args, cwd=REPO):
        return self.run(label, ["git", *args], cwd=cwd).stdout.decode().strip()

    def identity(self, prefix):
        head = self.git(prefix + "-head", "rev-parse", "HEAD")
        tree = self.git(prefix + "-tree", "rev-parse", "HEAD^{tree}")
        status = self.git(prefix + "-status", "status", "--porcelain=v1", "--untracked-files=all")
        return {"head": head, "tree": tree, "status": status, "authored_sha256": {path: sha(REPO / path) for path in AUTHORED}}

    def preflight(self):
        args = self.args
        require(Path.cwd().resolve() == REPO, "acceptance cwd must be staged repo")
        require(args.source_manifest.resolve() == REPO.parent / "SOURCE-MANIFEST.json", "manifest path mismatch")
        manifest = json.loads(args.source_manifest.read_text())
        self.manifest = manifest
        self.receipt["source_manifest_sha256"] = sha(args.source_manifest)
        require(manifest["schema"] == "agentorange.source-manifest/v1", "manifest schema")
        require(Path(manifest["repo"]).resolve() == REPO and Path(manifest["write_root"]).resolve() == REPO.parent, "manifest repo/root mismatch")
        require(manifest["inputs"] == [], "unexpected input acquisition")
        require(re.fullmatch(r"[0-9a-f]{40}", manifest["head"]) and re.fullmatch(r"[0-9a-f]{40}", manifest["tree"]), "invalid base identity")
        require(self.git("base-tree", "rev-parse", manifest["head"] + "^{tree}") == manifest["tree"], "base tree mismatch")
        self.run("base-ancestor", ["git", "merge-base", "--is-ancestor", manifest["head"], "HEAD"])
        venv = REPO / ".venv"
        runtime = manifest["runtime"]
        require(venv.is_symlink() and venv.resolve() == Path(runtime["repo_venv"]).resolve(), "venv link mismatch")
        require(Path(runtime["credential_scan_python3"]).is_file() and Path(runtime["detect_secrets"]).is_file(), "staged scanner missing")
        require(Path(runtime["detect_secrets"]).parent == Path(runtime["credential_scan_python3"]).parent, "scanner runtime boundary")
        expected = Path(runtime["credential_scan_python3"]) if args.phase == "credentials" else venv / "bin/python3"
        require(Path(sys.executable).absolute() == expected.absolute(), "literal interpreter mismatch")
        self.before = self.identity("before")
        self.receipt["candidate"] = self.before
        self.receipt["base"] = {"head": manifest["head"], "tree": manifest["tree"]}
        baseline_build = self.run("base-build", ["git", "show", manifest["head"] + ":" + BUILD]).stdout
        self.baseline = self.before["head"] == manifest["head"] and (REPO / BUILD).read_bytes() == baseline_build
        require(not self.baseline or args.phase in ("suite", "compatibility"), "baseline allowed only for original regression/compatibility evidence")
        if not self.baseline:
            require(self.before["status"] == "", "final candidate must be clean/committed")
            changed = self.git("complete-diff-paths", "diff", "--name-only", manifest["head"], "HEAD").splitlines()
            require(set(changed) == set(AUTHORED), "exclusive write set mismatch")
        self.run("complete-diff", ["git", "diff", "--binary", manifest["head"]])
        self.receipt["unchanged_bound_sha256"] = {}
        for index, relative in enumerate(BOUND):
            original = self.run("bound-" + str(index), ["git", "show", manifest["head"] + ":" + relative]).stdout
            require(original == (REPO / relative).read_bytes(), "source-bound file changed: " + relative)
            self.receipt["unchanged_bound_sha256"][relative] = sha(REPO / relative)
        require(self.git("manifest-base-pins", "rev-parse", manifest["head"]) == manifest["head"], "base identity drift")
        self.receipt["fixture_sha256"] = sha(REPO / FIXTURE)
        self.receipt["baseline_observation"] = self.baseline

    def tests(self, mode, label, unrelated=False):
        temporary = self.root / label
        temporary.mkdir()
        result = self.run(label, [sys.executable, str(Path(__file__).resolve()), "--child", mode,
                                 "--temporary-root", str(temporary)], cwd=temporary if unrelated else REPO,
                          allowed=(0, 1) if self.baseline and mode == "suite" else (0,))
        report = json.loads((temporary / "child-report.json").read_text())
        require(report["skipped"] == 0 and report["errors"] == 0, "skipped tests or arbitrary errors")
        require(report["ambient_unchanged"], "test ambient contamination")
        if mode != "compatibility":
            require(sorted(name for name in report["test_ids"] if "RedactionIterableTests." in name) == expected_ids(), "missing required case IDs")
        if self.baseline and mode == "suite":
            expected_red = [name for name in expected_ids() if
                            name.endswith(("test_consumed_once_and_shared_immutable_unmodified_snapshots",
                                           "test_deciding_materialization_bypass_and_restoration",
                                           "test_repeated_fresh_equivalent_generators")) or
                            ("test_matrix_" in name and
                             (("_allow_rule_generator_" in name or "_combined_source_generator_" in name) or
                              ("_allow_rule_" not in name and name.endswith("_generator"))))]
            require(result.returncode == 1 and sorted(report["failure_ids"]) == sorted(expected_red), "unexpected baseline failure identities")
        else:
            require(report["failures"] == 0 and result.returncode == 0, "tests failed")
        self.receipt[label] = report

    def portable(self):
        for relative in (TEST, FIXTURE, RUNNER):
            text = (REPO / relative).read_text()
            require(not any(token in text for token in ("/" + "home/", "/" + "Users/", "agent-work/" + "candidates", "PYTHON" + "PATH=")), "box-specific path: " + relative)
        temporary = self.root / "unrelated-script-cwd"
        temporary.mkdir()
        result = self.run("absolute-script", [sys.executable, str(REPO / TEST), "--temporary-root", str(temporary)], cwd=temporary)
        text = result.stderr.decode()
        names = re.findall(r"^(test_\w+) \(__main__\.RedactionIterableTests\.\w+\) \.\.\. ok$", text, flags=re.M)
        require(sorted("tests.records.test_fc13_redaction_iterables.RedactionIterableTests." + name for name in names) == expected_ids(), "portable script matrix incomplete")
        require("skipped" not in text.lower() and re.search(r"\nOK\s*$", text), "portable output not green")
        self.receipt["absolute_script_ids"] = sorted(names)
        self.tests("discovery", "ordinary-discovery", unrelated=True)

    def credentials(self):
        runtime = self.manifest["runtime"]
        scan = self.run("detect-secrets", [runtime["detect_secrets"], "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"])
        report = self.root / "scanner-report.json"
        report.write_bytes(scan.stdout)
        require(isinstance(json.loads(scan.stdout)["results"], dict), "invalid scanner JSON")
        self.run("check-secret-report", [runtime["credential_scan_python3"], str(REPO / "tools/check_secret_report.py"), str(report)])

    def public_tree(self):
        require(self.before["status"] == "", "dirty candidate")
        self.run("tracked-index-bytes", ["git", "diff", "--exit-code", "HEAD", "--", *AUTHORED])
        inventory = self.git("scan-visible-inventory", "ls-files", "--cached", "--others", "--exclude-standard").splitlines()
        tracked = self.git("tracked-inventory", "ls-files").splitlines()
        require(set(inventory) - set(tracked) <= {".venv"}, "untracked scan-visible file")
        clone = self.root / "committed-tree"
        self.run("local-clone", ["git", "clone", "--local", "--no-hardlinks", "--no-checkout", "--no-recurse-submodules", str(REPO), str(clone)])
        self.run("clone-checkout", ["git", "checkout", "--detach", self.before["head"]], cwd=clone)
        require(self.git("clone-head", "rev-parse", "HEAD", cwd=clone) == self.before["head"], "clone head")
        require(self.git("clone-tree", "rev-parse", "HEAD^{tree}", cwd=clone) == self.before["tree"], "clone tree")
        require({relative: sha(clone / relative) for relative in AUTHORED} == self.before["authored_sha256"], "clone authored bytes")
        result = self.run("committed-public-tree", [sys.executable, str(clone / "tools/check_public_tree.py")], cwd=clone)
        clean = json.loads(result.stdout)
        require(clean["findings"] == [] and clean["scanned_files"] == len(tracked), "incomplete/unclean committed tree")
        result = self.run("in-place-informational", [sys.executable, str(REPO / "tools/check_public_tree.py")], allowed=(0, 1))
        info = json.loads(result.stdout)
        findings = info["findings"]
        require((result.returncode == 0 and findings == []) or
                (result.returncode == 1 and findings == [[".venv", 0, "symlink_not_scanned"]]), "unrecognized informational outcome")
        require(info["scanned_files"] == len(tracked), "incomplete in-place scan")
        self.receipt["in_place_informational"] = info
        self.receipt["committed_public_tree"] = clean

    def finish(self):
        after = self.identity("after")
        require(after == self.before, "candidate changed during acceptance")
        require(sha(self.args.source_manifest) == self.receipt["source_manifest_sha256"], "manifest changed")
        self.receipt["after"] = after
        self.receipt["status"] = "BASELINE_EXPECTED_RED" if self.baseline and self.args.phase == "suite" else "BASELINE_COMPATIBILITY" if self.baseline else "PASS_REVIEW_REQUIRED"

    def seal(self):
        # Every synchronous child writer has exited before sealing. Hash all raw
        # files and child reports, not clone internals (identity is above).
        evidence = [path for path in self.root.rglob("*") if path.is_file() and "committed-tree" not in path.relative_to(self.root).parts]
        self.receipt["files"] = {str(path.relative_to(self.root)): {"sha256": sha(path), "bytes": path.stat().st_size} for path in sorted(evidence)}
        manifest = self.root / "evidence.json"
        require(not manifest.exists(), "evidence already sealed")
        dump(manifest, self.receipt)
        print(json.dumps({"status": self.receipt["status"], "evidence": str(manifest), "sha256": sha(manifest)}, sort_keys=True))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("suite", "compatibility", "portable", "credentials", "public-tree"))
    parser.add_argument("--source-manifest", type=Path)
    parser.add_argument("--out-base", type=Path)
    parser.add_argument("--child", choices=("suite", "compatibility", "discovery"))
    parser.add_argument("--temporary-root", type=Path)
    args = parser.parse_args()
    if args.child:
        require(args.temporary_root is not None and args.phase is None, "child arguments")
        return child(args)
    require(args.phase and args.source_manifest and args.out_base, "phase/manifest/out-base required")
    acceptance = Acceptance(args)
    exit_code = 1
    try:
        acceptance.preflight()
        if args.phase in ("suite", "compatibility"):
            acceptance.tests(args.phase, args.phase + "-tests")
        elif args.phase == "portable":
            acceptance.portable()
        elif args.phase == "credentials":
            acceptance.credentials()
        else:
            acceptance.public_tree()
        acceptance.finish()
        exit_code = 1 if acceptance.baseline and args.phase == "suite" else 0
    except Exception as error:
        acceptance.receipt["status"] = "FAIL"
        acceptance.receipt["error"] = str(error)
        (acceptance.root / "failure.txt").write_text(traceback.format_exc())
    finally:
        acceptance.seal()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
