"""FC13 bounded local acceptance receipts; no publication or semantic acceptance.

Authored by actual gpt-6.1-sol / openai-codex, t_2532b0ef. Only stdlib is
imported here. Product imports run in the test script after scoped sentinels and
explicit temporary-root redirection. subprocesses are fixed local argv lists.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import traceback
import uuid

REPO = Path(__file__).resolve().parents[1]
BASE = "17da706588467184 f0923e2516be3319 d577539a".replace(" ", "")
BASE_TREE = "91b70d87ae558fe6 7e728f5dc0131dcc f1056ad5".replace(" ", "")
TEST = "tests/records/test_fc13_structured_revisions.py"
FIXTURE = "tests/fixtures/fc13-structured-revisions/cases.json"
RUNNER = "tools/fc13_structured_revisions_acceptance.py"
AUTHORED = (TEST, FIXTURE, RUNNER)
PINS = {
    "campaign_tool/records/detectors/pipeline.py":
        "88a0a21e729ac0a2 ba050ddd9953c071 b166eff872ba1446 954d11bc9557ffd2".replace(" ", ""),
    "tests/records/test_structured_detector_pipeline.py":
        "4e7e74a05ceeb67b d2bcfaee6f578e66 521856bdf3cbe13d 84f5a001e86bc65f".replace(" ", ""),
    "tests/records/test_detectors.py":
        "b59fea6b9bbf3e4d 858366a3237e6f56 31bd69dad81024b5 27835e12709c72b0".replace(" ", ""),
}
CHECKERS = ("tools/check_secret_report.py", "tools/check_public_tree.py",
            "campaign_tool/records/public_scan.py")
CASES = ("baseline", "normalization_version", "source_artifact", "agency_metadata",
         "rules_version", "config_annotation")
TEST_NAMES = tuple("test_%02d_%s" % (i, case) for i, case in enumerate(CASES)) + (
    "test_90_stale_config_negative_control",)
COMPAT = (
    "tests.records.test_structured_detector_pipeline.StructuredPipelineTests.test_duplicates_determinism_revisions_and_deep_copy",
    "tests.records.test_structured_detector_pipeline.StructuredPipelineTests.test_bare_typed_join_and_stale_snapshot_block",
)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def write_json(path, value):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True, indent=2)
        stream.write("\n")


class Evidence:
    def __init__(self, root):
        self.root = root
        self.calls = []
        self.env = os.environ.copy()
        self.env.update({key: str(root) for key in ("TMPDIR", "TMP", "TEMP")})
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"

    def call(self, label, argv, cwd=REPO, allowed=(0,)):
        require(all(isinstance(arg, str) for arg in argv), "argv strings required")
        require(not any(arg in ("-c", "-e") for arg in argv), "inline interpreter forbidden")
        completed = subprocess.run(argv, cwd=cwd, env=self.env, shell=False,
                                   capture_output=True, timeout=180)
        number = len(self.calls)
        stem = "%02d-%s" % (number, label)
        stdout, stderr = self.root / (stem + ".stdout"), self.root / (stem + ".stderr")
        stdout.write_bytes(completed.stdout)
        stderr.write_bytes(completed.stderr)
        record = {"label": label, "argv": argv, "cwd": str(cwd), "exit": completed.returncode,
                  "stdout": stdout.name, "stdout_sha256": sha(completed.stdout),
                  "stderr": stderr.name, "stderr_sha256": sha(completed.stderr)}
        self.calls.append(record)
        write_json(self.root / (stem + ".json"), record)
        require(completed.returncode in allowed, label + " unexpected exit " + str(completed.returncode))
        return completed

    def git(self, label, *args, cwd=REPO):
        return self.call(label, ["git", *args], cwd).stdout.decode("utf-8").strip()


def identity(evidence, label):
    return {"head": evidence.git(label + "-head", "rev-parse", "HEAD"),
            "tree": evidence.git(label + "-tree", "rev-parse", "HEAD^{tree}"),
            "status": evidence.git(label + "-status", "status", "--porcelain=v1", "--untracked-files=all"),
            "diff": evidence.git(label + "-complete-diff", "diff", "--no-ext-diff", BASE, "--"),
            "tracked_paths": evidence.git(label + "-inventory", "ls-files", "-z").split("\0")}


def preflight(options, evidence):
    raw = options.source_manifest.read_bytes()
    manifest = json.loads(raw)
    require(manifest["schema"] == "agentorange.source-manifest/v1", "manifest schema")
    require(Path(manifest["repo"]) == REPO and REPO.resolve() == REPO, "manifest repo path")
    require(Path(manifest["write_root"]).resolve() == REPO.parent, "write root mismatch")
    require(manifest["head"] == BASE and manifest["tree"] == BASE_TREE and manifest["clean"] is True,
            "staged source identity mismatch")
    require(manifest["inputs"] == [], "no acquired/private inputs admitted")
    runtime = manifest["runtime"]
    venv = REPO / ".venv"
    require(venv.is_symlink() and venv.resolve(strict=True) == Path(runtime["repo_venv"]).resolve(strict=True),
            "manifest venv link mismatch")
    expected_python = (Path(runtime["credential_scan_python3"]) if options.phase == "credentials"
                       else venv / "bin/python3")
    require(Path(sys.executable).absolute() == expected_python.absolute(), "row interpreter spelling mismatch")
    require(expected_python.is_file() and Path(runtime["detect_secrets"]).is_file(), "staged runtime unavailable")
    require(Path(runtime["credential_scan_python3"]).parent.parent.resolve() == venv.resolve(),
            "credential interpreter outside manifest venv")
    source = {}
    for path, expected in PINS.items():
        actual = sha((REPO / path).read_bytes())
        require(actual == expected, "source pin changed: " + path)
        source[path] = actual
    for path in CHECKERS:
        baseline = evidence.call("checker-base", ["git", "show", BASE + ":" + path]).stdout
        actual = (REPO / path).read_bytes()
        require(actual == baseline, "scanner/allowlist source changed: " + path)
        source[path] = sha(actual)
    evidence.git("base-is-ancestor", "merge-base", "--is-ancestor", BASE, "HEAD")
    require(evidence.git("base-tree", "rev-parse", BASE + "^{tree}") == BASE_TREE, "base tree changed")
    changed = evidence.git("changed-names", "diff", "--name-only", BASE, "--").splitlines()
    require(set(changed) <= set(AUTHORED), "outside exclusive write set")
    untracked = evidence.git("untracked-visible", "ls-files", "--others", "--exclude-standard").splitlines()
    require(set(untracked) <= set(AUTHORED) | {".venv"}, "unexpected scan-visible untracked file")
    start = identity(evidence, "initial")
    # One genuine pre-authoring compatibility baseline is permitted at base.
    baseline_only = options.phase == "compatibility" and start["head"] == BASE
    if not baseline_only:
        require(start["head"] != BASE and not start["status"], "final candidate must be committed and clean")
        require(set(changed) == set(AUTHORED), "incomplete authored commit")
        require(not evidence.git("index-diff", "diff", "--cached", "--name-only", "HEAD"), "index mismatch")
    authored = {path: sha((REPO / path).read_bytes()) for path in AUTHORED}
    fixture = json.loads((REPO / FIXTURE).read_text())
    require(tuple(case["id"] for case in fixture["cases"]) == CASES, "frozen fixture coverage missing")
    require(fixture["negative_control"]["failure"] == "wrong_revision:config_sha256", "control changed")
    return {"source_manifest": str(options.source_manifest), "source_manifest_sha256": sha(raw),
            "staging": manifest, "source_hashes": source, "authored_hashes": authored,
            "fixture": fixture, "initial_identity": start, "baseline_only": baseline_only,
            "actual_author": {"model": "gpt-6.1-sol", "provider": "openai-codex", "task": "t_2532b0ef"}}


def run_tests(evidence, mode, label):
    work = evidence.root / label
    work.mkdir()
    receipt = work / "test-receipt.json"
    evidence.call(label, [sys.executable, str(REPO / TEST), "--mode", mode,
                          "--temp-root", str(work), "--receipt", str(receipt)], cwd=work)
    result = json.loads(receipt.read_text())
    require(result["successful"] and result["errors"] == result["failures"] == result["skipped"] == 0,
            "test failures/errors/skips")
    ids = result["test_ids"]
    require(len(ids) == result["tests_run"], "test ID count mismatch")
    if mode == "compatibility":
        require(tuple(ids) == COMPAT and result["tests_run"] == 2, "compatibility selectors differ")
    else:
        expected_count = 9 if mode == "discovery" else 7
        require(result["tests_run"] == expected_count, "matrix test count mismatch")
        tested = [name.rsplit(".", 1)[-1] for name in ids if "StructuredRevisionTests." in name]
        require(sorted(tested) == sorted(TEST_NAMES), "missing/extra/duplicate required test IDs")
        events = result["events"]
        require([event["case"] for event in events if "case" in event] == list(CASES), "case coverage missing")
        controls = [event for event in events if "control" in event]
        require(len(controls) == 1 and controls[0]["control"] == "stale_config_valid_outer_hash"
                and controls[0]["outer_hash_valid"] is True and controls[0]["restored_positive"] is True
                and "wrong_revision:config_sha256" in controls[0]["specific_failure"], "deciding control missing")
    return result


def public_tree(evidence, packet):
    start = packet["initial_identity"]
    clone = evidence.root / "committed-tree"
    evidence.call("clone-local", ["git", "clone", "--local", "--no-hardlinks", "--no-checkout",
                                 "--no-recurse-submodules", str(REPO), str(clone)])
    evidence.call("checkout-exact-head", ["git", "checkout", "--detach", start["head"]], cwd=clone)
    require(evidence.git("clone-head", "rev-parse", "HEAD", cwd=clone) == start["head"], "clone head mismatch")
    require(evidence.git("clone-tree", "rev-parse", "HEAD^{tree}", cwd=clone) == start["tree"], "clone tree mismatch")
    require(not evidence.git("clone-status", "status", "--porcelain=v1", cwd=clone), "clone dirty")
    for path, expected in packet["authored_hashes"].items():
        require(sha((clone / path).read_bytes()) == expected, "clone authored hash mismatch")
    scan = evidence.call("committed-tree-scan", [sys.executable, str(clone / "tools/check_public_tree.py")], cwd=clone)
    data = json.loads(scan.stdout)
    require(data["findings"] == [] and data["scanned_files"] > 0, "committed tree findings/incomplete scan")
    informational = evidence.call("in-place-informational", [sys.executable, str(REPO / "tools/check_public_tree.py")],
                                  allowed=(0, 1))
    info = json.loads(informational.stdout)
    allowed_link = [[".venv", 0, "symlink_not_scanned"]]
    require(info["scanned_files"] > 0 and
            ((informational.returncode == 0 and info["findings"] == []) or
             (informational.returncode == 1 and info["findings"] == allowed_link)),
            "unrecognized in-place result (INC-174)")
    return {"exact_committed_tree": data, "in_place_informational": info,
            "in_place_exit": informational.returncode, "classification": "INC-174 informational only"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", required=True,
                        choices=("suite", "compatibility", "portable", "credentials", "public-tree"))
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--out-base", type=Path, required=True)
    options = parser.parse_args()
    require(Path.cwd() == REPO, "acceptance cwd must be staged repo")
    out = options.out_base.absolute()
    require(out == REPO.parent / "out" or REPO.parent / "out" in out.parents,
            "evidence must remain under admitted output root")
    require(not any(p.is_symlink() for p in (out, *out.parents)), "output symlink rejected")
    out.mkdir(parents=True, exist_ok=True)
    root = out / (options.phase + "-" + str(uuid.uuid4()))
    root.mkdir()
    evidence = Evidence(root)
    packet = {"schema": "fc13-acceptance-evidence/v1", "phase": options.phase, "argv": sys.argv,
              "cwd": str(Path.cwd()), "interpreter": sys.executable, "calls": evidence.calls,
              "status": "FAIL", "limits": "REVIEW REQUIRED; not semantic, human, legal, full CI or shipping acceptance"}
    exit_code = 1
    try:
        packet.update(preflight(options, evidence))
        if options.phase == "suite":
            packet["suite"] = run_tests(evidence, "suite", "matrix")
        elif options.phase == "compatibility":
            packet["compatibility"] = run_tests(evidence, "compatibility", "compatibility")
        elif options.phase == "portable":
            for path in AUTHORED:
                content = (REPO / path).read_text()
                require(not re.search(r"/(?:home|Users|workspace|tmp)/", content), "box-specific literal in " + path)
            packet["absolute_script_unrelated_cwd"] = run_tests(evidence, "suite", "absolute-script")
            packet["ordinary_discovery_with_peer_sentinels"] = run_tests(evidence, "discovery", "discovery")
        elif options.phase == "credentials":
            runtime = packet["staging"]["runtime"]
            scan = evidence.call("detect-secrets", [runtime["detect_secrets"], "scan", "--all-files",
                                                    "--exclude-files", "^.git/", "--no-verify"])
            report = root / "secret-report.json"
            report.write_bytes(scan.stdout)
            require(isinstance(json.loads(scan.stdout)["results"], dict), "invalid scanner JSON")
            checked = evidence.call("unchanged-secret-check", [runtime["credential_scan_python3"],
                                                               str(REPO / "tools/check_secret_report.py"), str(report)])
            packet["credentials"] = {"report": report.name, "report_sha256": sha(scan.stdout),
                                     "checker_stdout": checked.stdout.decode()}
        else:
            packet["public_tree"] = public_tree(evidence, packet)
        final = identity(evidence, "final")
        require(final == packet["initial_identity"], "source candidate changed during acceptance")
        for path, expected in {**packet["source_hashes"], **packet["authored_hashes"]}.items():
            require(sha((REPO / path).read_bytes()) == expected, "post-run source bytes changed")
        require(sha(options.source_manifest.read_bytes()) == packet["source_manifest_sha256"], "staging manifest changed")
        packet["final_identity"] = final
        packet["status"] = "BASELINE_PASS" if packet["baseline_only"] else "PASS_REVIEW_REQUIRED"
        exit_code = 0
    except Exception as error:
        packet["error"] = type(error).__name__ + ": " + str(error)
        (root / "failure.txt").write_text(traceback.format_exc())
    # All synchronous child processes/writers have returned before this seal.
    packet["evidence_files"] = {str(path.relative_to(root)): sha(path.read_bytes())
                                for path in sorted(root.rglob("*")) if path.is_file()
                                and "committed-tree" not in path.relative_to(root).parts}
    manifest_path = root / "evidence-manifest.json"
    write_json(manifest_path, packet)
    print(json.dumps({"phase": options.phase, "status": packet["status"], "exit": exit_code,
                      "evidence_manifest": str(manifest_path), "evidence_manifest_sha256": sha(manifest_path.read_bytes()),
                      "error": packet.get("error")}, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
