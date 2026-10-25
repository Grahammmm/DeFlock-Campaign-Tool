"""FC13 offline evidence runner; authored by gpt-6.1-sol / openai-codex.

Each invocation seals new evidence after all child writers exit. No production
imports happen until source/runtime checks, temp routing and scoped sentinels.
These local rows are REVIEW REQUIRED, not repository CI or shipping authority.
"""
import argparse
from contextlib import ExitStack, contextmanager
import hashlib
import importlib
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
from unittest.mock import patch
import uuid

REPO = Path(__file__).resolve().parents[1]
AUTHORED = (
    "tests/records/test_fc13_rule_qualification.py",
    "tests/fixtures/fc13-rule-qualification/cases.json",
    "tools/fc13_rule_qualification_acceptance.py",
)
SOURCE = (
    "campaign_tool/records/detectors/pipeline.py",
    "tests/records/test_structured_detector_pipeline.py",
    "tests/records/test_detectors.py",
    "tools/check_secret_report.py", "tools/check_public_tree.py",
    "campaign_tool/records/public_scan.py",
)
BASE = "".join(("17da706588", "467184f092", "3e2516be33", "19d577539a"))
BASE_TREE = "".join(("91b70d87ae", "558fe67e72", "8f5dc0131d", "ccf1056ad5"))
MODULE = "tests.records.test_fc13_rule_qualification"
SELECTORS = ["tests.records.test_structured_detector_pipeline.StructuredPipelineTests." + name for name in (
    "test_unverified_rule_versions_do_not_evaluate",
    "test_current_rule_intervals_and_unbound_rules",
    "test_all_seven_positive_and_zero_hit_evaluations",
)]


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def save(path, value):
    with path.open("x") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")


def git(*args, cwd=REPO):
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=False)
    require(result.returncode == 0, "git identity command failed: " + repr(args))
    return result.stdout


def regular(path):
    require(not any(part.is_symlink() for part in (path, *path.parents)), "symlink source rejected")
    require(path.is_file(), "regular source unavailable: " + str(path))
    return path.read_bytes()


def snapshot():
    return {"head": git("rev-parse", "HEAD").decode().strip(),
            "tree": git("rev-parse", "HEAD^{tree}").decode().strip(),
            "status": git("status", "--porcelain=v1", "--untracked-files=all").decode(),
            "complete_diff": git("diff", "--binary", BASE).decode(),
            "authored_hashes": {name: sha(regular(REPO / name)) for name in AUTHORED},
            "source_hashes": {name: sha(regular(REPO / name)) for name in SOURCE}}


def validate(manifest_path, phase):
    raw = regular(manifest_path)
    manifest = json.loads(raw)
    require(manifest["schema"] == "agentorange.source-manifest/v1", "manifest schema")
    require(Path(manifest["repo"]) == REPO, "manifest repo identity")
    require(Path(manifest["write_root"]) == REPO.parent, "manifest write root")
    require(manifest["head"] == BASE and manifest["tree"] == BASE_TREE, "frozen staging pin mismatch")
    require(git("rev-parse", BASE + "^{tree}").decode().strip() == BASE_TREE, "base tree mismatch")
    require(subprocess.run(["git", "merge-base", "--is-ancestor", BASE, "HEAD"],
                           cwd=REPO, capture_output=True).returncode == 0, "candidate not descendant")
    changed = set(git("diff", "--name-only", BASE).decode().splitlines())
    require(changed <= set(AUTHORED), "exclusive write set changed")
    untracked = set(git("ls-files", "--others", "--exclude-standard", "-z").decode().split("\0")) - {""}
    require(untracked <= set(AUTHORED) | {".venv"}, "unexpected untracked scan-visible file")
    for name in SOURCE:
        require(regular(REPO / name) == git("show", BASE + ":" + name), "read-only pin changed: " + name)
    # All tracked production bytes remain at base, not just sampled pins.
    production_delta = git("diff", BASE, "--", "campaign_tool")
    require(not production_delta, "test-only production bytes changed")
    runtime = manifest["runtime"]
    link = REPO / ".venv"
    require(link.is_symlink() and link.resolve() == Path(runtime["repo_venv"]).resolve(), "staged venv link")
    expected = Path(runtime["credential_scan_python3"]) if phase == "credentials" else link / "bin/python3"
    require(Path(sys.executable).absolute() == expected.absolute(), "literal interpreter mismatch")
    require(Path(sys.executable).resolve() == expected.resolve(), "runtime interpreter mismatch")
    require(Path(runtime["detect_secrets"]).parent == Path(runtime["credential_scan_python3"]).parent,
            "scanner runtime boundary")
    return manifest, sha(raw)


@contextmanager
def network_offline():
    def denied(*args, **kwargs):
        raise RuntimeError("fc13_network_not_admitted")
    with ExitStack() as stack:
        for name in ("socket.socket", "socket.create_connection", "socket.getaddrinfo"):
            stack.enter_context(patch(name, denied))
        yield


def setup_temp(root):
    require(root.is_absolute() and root.is_dir(), "fresh temporary root missing")
    for key in ("TMPDIR", "TEMP", "TMP"):
        os.environ[key] = str(root)
    tempfile.tempdir = str(root)
    sys.dont_write_bytecode = True


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item.id()


class RecordingResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.executed_ids = []

    def startTest(self, test):
        self.executed_ids.append(test.id())
        super().startTest(test)


def run_suite(suite, root, label, expected):
    planned = list(flatten(suite))
    require(len(planned) == expected and len(set(planned)) == expected, "test coverage mismatch")
    if expected in (287, 289):
        names = ("search-before-training", "purpose-quality", "external-sharing",
                 "retention-over-policy", "audit-gap", "cpra-deadline", "volume-anomaly")
        import itertools
        required_methods = {"test_matrix__" + "__".join(value.replace("-", "_") for value in row)
            for row in itertools.product(names, ("bound", "unbound"),
                ("true", "false", "integer_one", "null", "missing"),
                ("yes", "no", "unknown", "missing"))}
        required_methods.update("test_control__" + name.replace("-", "_") for name in names)
        actual_methods = {test_id.rsplit(".", 1)[-1] for test_id in planned
                          if test_id not in ("fc13_peer_before", "fc13_peer_after")}
        require(actual_methods == required_methods, "missing frozen named test cases")
    else:
        require(planned == SELECTORS, "compatibility selectors changed")
    result = unittest.TextTestRunner(verbosity=2, resultclass=RecordingResult).run(suite)
    if not isinstance(result, RecordingResult):
        raise TypeError("recording result unavailable")
    receipt = {"test_ids": planned, "executed_ids": result.executed_ids,
               "tests_run": result.testsRun, "skipped": len(result.skipped),
               "failures": len(result.failures), "errors": len(result.errors),
               "unexpected_successes": len(result.unexpectedSuccesses),
               "expected_failures": len(result.expectedFailures)}
    save(root / (label + ".json"), receipt)
    require(result.executed_ids == planned, "missing or reordered test execution")
    require(result.wasSuccessful() and not result.skipped and not result.expectedFailures,
            "test acceptance failed")
    return receipt


def child(args):
    validate(args.source_manifest, args.phase)
    setup_temp(args.temporary_root)
    sys.path.insert(0, str(REPO))
    with network_offline():
        # Reuse only this card's scoped sentinel implementation, before product imports.
        module = importlib.import_module(MODULE)
        with module.offline():
            if args.child == "compatibility":
                suite = unittest.TestLoader().loadTestsFromNames(SELECTORS)
                run_suite(suite, args.temporary_root, "tests", 3)
            elif args.child == "suite":
                suite = unittest.TestLoader().loadTestsFromModule(module)
                run_suite(suite, args.temporary_root, "tests", 287)
            else:
                # Fresh discovery import, without the module in sys.modules, and
                # unrelated peers before/after. Snapshot around import+discovery.
                del sys.modules[MODULE]
                environment = dict(os.environ)
                temp_default = tempfile.tempdir
                process_api = subprocess.Popen
                network_api = socket.socket
                db_api = __import__("sqlite3").connect
                peer = unittest.TestCase()

                def sentinel():
                    peer.assertEqual(dict(os.environ), environment)
                    peer.assertEqual(tempfile.tempdir, temp_default)
                    peer.assertIs(subprocess.Popen, process_api)
                    peer.assertIs(socket.socket, network_api)
                    peer.assertIs(__import__("sqlite3").connect, db_api)
                    peer.assertTrue(Path(tempfile.gettempdir()).is_relative_to(args.temporary_root))

                sentinel()
                discovered = unittest.TestLoader().discover(
                    str(REPO / "tests/records"), pattern="test_fc13_rule_qualification.py",
                    top_level_dir=str(REPO))
                sentinel()
                class Peer(unittest.FunctionTestCase):
                    def id(self):
                        return self.shortDescription() or "unnamed_peer"

                suite = unittest.TestSuite([
                    Peer(sentinel, description="fc13_peer_before"), discovered,
                    Peer(sentinel, description="fc13_peer_after")])
                run_suite(suite, args.temporary_root, "discovery", 289)
                sentinel()
                save(args.temporary_root / "ambient.json", {"unchanged": True, "peer_tests": 2,
                     "environment": True, "tempfile_default": True, "mock_targets": True,
                     "permanent_audit_hooks": "none in authored module (static inspection required)"})
    return 0


class Evidence:
    def __init__(self, root):
        self.root, self.commands = root, []

    def run(self, label, argv, cwd, allowed=(0,)):
        require(all(isinstance(value, str) for value in argv), "literal argv strings required")
        result = subprocess.run(argv, cwd=cwd, capture_output=True, check=False)
        stdout, stderr = self.root / (label + ".stdout"), self.root / (label + ".stderr")
        stdout.write_bytes(result.stdout)
        stderr.write_bytes(result.stderr)
        self.commands.append({"label": label, "argv": argv, "cwd": str(cwd),
                              "exit": result.returncode, "stdout": stdout.name, "stderr": stderr.name,
                              "stdout_sha256": sha(result.stdout), "stderr_sha256": sha(result.stderr)})
        require(result.returncode in allowed, "child exit failure: " + label)
        return result


def public_tree(evidence, manifest, identity):
    require(not identity["status"], "authored files must be committed; source tree must be clean")
    tracked = git("ls-files", "-z").decode().split("\0")
    scan_visible = set(git("ls-files", "-z", "--cached", "--others", "--exclude-standard").decode().split("\0")) - {""}
    require(scan_visible - set(tracked) <= {".venv"}, "untracked scan-visible files")
    for name in AUTHORED:
        require(name in tracked, "authored file untracked")
        require(regular(REPO / name) == git("show", "HEAD:" + name) == git("show", ":" + name),
                "authored committed/index bytes mismatch")
    save(evidence.root / "source_inventory.json", {"tracked": tracked, "scan_visible": sorted(scan_visible)})
    clone = evidence.root / "committed-tree"
    evidence.run("clone", ["git", "clone", "--local", "--no-hardlinks", "--no-checkout",
                           "--no-recurse-submodules", str(REPO), str(clone)], evidence.root)
    evidence.run("checkout", ["git", "checkout", "--detach", identity["head"]], clone)
    require(git("rev-parse", "HEAD", cwd=clone).decode().strip() == identity["head"], "clone HEAD mismatch")
    require(git("rev-parse", "HEAD^{tree}", cwd=clone).decode().strip() == identity["tree"], "clone tree mismatch")
    require(not git("status", "--porcelain=v1", cwd=clone), "clone not clean")
    for name in AUTHORED + SOURCE:
        require(sha(regular(clone / name)) == sha(regular(REPO / name)), "clone hash mismatch")
    checked = evidence.run("committed_public_tree", [sys.executable, str(clone / "tools/check_public_tree.py")], clone)
    report = json.loads(checked.stdout)
    require(report["findings"] == [] and report["scanned_files"] > 0, "committed public-tree findings")
    info = evidence.run("in_place_informational", [sys.executable, str(REPO / "tools/check_public_tree.py")], REPO,
                        allowed=(0, 1))
    in_place = json.loads(info.stdout)
    require(in_place["scanned_files"] > 0, "in-place scan incomplete")
    require((info.returncode == 0 and in_place["findings"] == []) or
            (info.returncode == 1 and in_place["findings"] == [[".venv", 0, "symlink_not_scanned"]]),
            "unrecognized INC-174 in-place informational result")
    require((REPO / ".venv").is_symlink() and
            (REPO / ".venv").resolve() == Path(manifest["runtime"]["repo_venv"]).resolve(), "venv changed")
    return {"committed_tree": "PASS", "clone_head": identity["head"], "clone_tree": identity["tree"],
            "in_place_informational": {"exit": info.returncode, "findings": in_place["findings"]},
            "scanner_allowlist_hashes": identity["source_hashes"]}


def parent(args):
    manifest, manifest_hash = validate(args.source_manifest, args.phase)
    out = args.out_base.absolute()
    require(not any(part.is_symlink() for part in (out, *out.parents)), "output ancestry symlink")
    require(out.is_relative_to(REPO.parent / "out") and not out.is_relative_to(REPO), "output root boundary")
    out.mkdir(parents=True, exist_ok=True)
    root = out / (args.phase + "-" + uuid.uuid4().hex)
    root.mkdir()
    setup_temp(root)
    evidence = Evidence(root)
    identity = snapshot()
    receipt = {"schema": "fc13-acceptance/v1", "phase": args.phase,
               "argv": sys.argv, "cwd": str(Path.cwd()), "interpreter": sys.executable,
               "interpreter_resolved": str(Path(sys.executable).resolve()),
               "source_manifest_sha256": manifest_hash, "identity": identity,
               "author_runtime": {"model": "gpt-6.1-sol", "provider": "openai-codex",
                                  "profile": "sol-reviewer", "kanban_task": "t_2846fe7a"},
               "status": "FAIL", "commands": evidence.commands,
               "limits": "Offline scoped rows only; mandatory full discovery, supported surface and required exact-head CI OWED to shipping operator. No semantic/legal acceptance or publication authority."}
    exit_code = 1
    try:
        with network_offline():
            if args.phase in ("suite", "compatibility"):
                temp = root / "child"
                temp.mkdir()
                evidence.run(args.phase, [sys.executable, str(Path(__file__).resolve()),
                    "--phase", args.phase, "--source-manifest", str(args.source_manifest),
                    "--out-base", str(args.out_base), "--child", args.phase,
                    "--temporary-root", str(temp)], REPO)
                receipt["tests"] = json.loads((temp / "tests.json").read_text())
            elif args.phase == "portable":
                for name in AUTHORED:
                    raw = regular(REPO / name)
                    for suffix in (b"home/", b"Users/", b"workspace/", b"tmp/"):
                        require(b"/" + suffix not in raw, "box-specific authored path")
                unrelated = root / "unrelated-cwd"
                unrelated.mkdir()
                evidence.run("absolute_script", [sys.executable, str(REPO / AUTHORED[0]),
                             "--temporary-root", str(unrelated)], unrelated)
                receipt["absolute_script"] = json.loads((unrelated / "absolute-script-tests.json").read_text())
                require(receipt["absolute_script"]["tests_run"] == 287 and
                        receipt["absolute_script"]["test_ids"] == receipt["absolute_script"]["executed_ids"],
                        "portable script execution incomplete")
                discovery = root / "discovery-child"
                discovery.mkdir()
                evidence.run("ordinary_discovery", [sys.executable, str(Path(__file__).resolve()),
                    "--phase", "portable", "--source-manifest", str(args.source_manifest),
                    "--out-base", str(args.out_base), "--child", "discovery",
                    "--temporary-root", str(discovery)], unrelated)
                receipt["discovery"] = json.loads((discovery / "discovery.json").read_text())
                receipt["ambient"] = json.loads((discovery / "ambient.json").read_text())
            elif args.phase == "credentials":
                runtime = manifest["runtime"]
                scan = evidence.run("detect_secrets", [runtime["detect_secrets"], "scan", "--all-files",
                    "--exclude-files", "^.git/", "--no-verify"], REPO)
                report = root / "scanner.json"
                report.write_bytes(scan.stdout)
                require(isinstance(json.loads(scan.stdout)["results"], dict), "scanner JSON invalid")
                evidence.run("secret_report_check", [runtime["credential_scan_python3"],
                             str(REPO / "tools/check_secret_report.py"), str(report)], REPO)
            else:
                receipt["public_tree"] = public_tree(evidence, manifest, identity)
        require(snapshot() == identity, "candidate changed during acceptance")
        # Preserve complete dimensions independent of generated case counts.
        data = json.loads(regular(REPO / AUTHORED[1]))
        import itertools
        receipt["case_dimensions"] = list(itertools.product(data["detectors"], data["bindings"],
                                              data["version_flags"], data["applicabilities"]))
        require(len(receipt["case_dimensions"]) == 280, "missing required dimensions")
        receipt["status"] = "PASS_REVIEW_REQUIRED"
        exit_code = 0
    except Exception:
        receipt["error"] = traceback.format_exc()
    # No children survive subprocess.run. Logs and child receipts are closed now.
    # Exclude local clone internals from evidence hashes; its exact tree is bound above.
    files = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if relative.parts[0] != "committed-tree" and path.is_file():
            files[str(relative)] = sha(path.read_bytes())
    receipt["evidence_files_sha256"] = files
    save(root / "evidence-manifest.json", receipt)
    digest = sha((root / "evidence-manifest.json").read_bytes())
    print(json.dumps({"status": receipt["status"], "phase": args.phase, "head": identity["head"],
                      "manifest": str(root / "evidence-manifest.json"), "sha256": digest,
                      "error": receipt.get("error")}))
    return exit_code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", required=True, choices=("suite", "compatibility", "portable", "credentials", "public-tree"))
    parser.add_argument("--source-manifest", required=True, type=Path)
    parser.add_argument("--out-base", required=True, type=Path)
    parser.add_argument("--child", choices=("suite", "compatibility", "discovery"))
    parser.add_argument("--temporary-root", type=Path)
    args = parser.parse_args()
    if args.child:
        require(args.temporary_root is not None, "child temp root required")
        return child(args)
    return parent(args)


if __name__ == "__main__":
    raise SystemExit(main())
