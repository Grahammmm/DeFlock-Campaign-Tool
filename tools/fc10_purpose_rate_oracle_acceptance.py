"""Bounded offline FC10 acceptance; each invocation preserves a fresh receipt.

Runtime paths come only from the operator source manifest. No product edits,
network, installs, publication, semantic acceptance or remote CI claims.
"""
import argparse
import hashlib
import importlib
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
import uuid

AUTHORED = (
    "tests/records/test_fc10_purpose_rate_oracle.py",
    "tests/fixtures/fc10-purpose-rate-oracle/cases.json",
    "tools/fc10_purpose_rate_oracle_acceptance.py",
)
SCANNERS = ("tools/check_secret_report.py", "tools/check_public_tree.py", "campaign_tool/records/public_scan.py")
REPO = Path(__file__).resolve().parents[1]


def need(condition, reason):
    if not condition:
        raise RuntimeError(reason)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def dump(path, value):
    Path(path).write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")


def plain(path):
    path = Path(path).absolute()
    need(path == path.resolve(), "noncanonical path: " + str(path))
    for ancestor in (*path.parents, path):
        need(not ancestor.is_symlink(), "symlink ancestor: " + str(ancestor))
    return path


def git(*args, cwd=REPO):
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, shell=False, check=False)
    need(result.returncode == 0, "git identity query failed: " + result.stderr.decode(errors="replace"))
    return result.stdout.decode().strip()


def identity():
    return {"head": git("rev-parse", "HEAD"), "tree": git("rev-parse", "HEAD^{tree}"),
            "status": git("status", "--porcelain=v1", "--untracked-files=all"),
            "visible_paths": git("ls-files", "--cached", "--others", "--exclude-standard", "-z").split("\0"),
            "authored_hashes": {name: sha(REPO / name) for name in AUTHORED},
            "scanner_hashes": {name: sha(REPO / name) for name in SCANNERS}}


def validate(args):
    manifest_path = plain(args.source_manifest)
    manifest = json.loads(manifest_path.read_text())
    need(manifest["schema"] == "agentorange.source-manifest/v1", "manifest schema")
    need(plain(manifest["repo"]) == REPO, "manifest repo mismatch")
    write_root = plain(manifest["write_root"])
    need(REPO == write_root / "repo" and manifest_path == write_root / "SOURCE-MANIFEST.json", "staging layout")
    out = plain(args.out_base)
    need(out == write_root / "out", "output root mismatch")
    runtime = manifest["runtime"]
    need((REPO / ".venv").is_symlink(), "standard .venv link missing")
    need((REPO / ".venv").resolve() == Path(runtime["repo_venv"]).resolve(), "runtime link mismatch")
    expected_python = runtime["credential_scan_python3"] if args.phase == "credentials" else str(REPO / ".venv/bin/python3")
    need(str(Path(sys.executable).absolute()) == expected_python, "literal interpreter mismatch")
    need(Path(runtime["credential_scan_python3"]).parent.parent == Path(runtime["repo_venv"]), "scan interpreter root")
    need(Path(runtime["detect_secrets"]).parent == Path(runtime["credential_scan_python3"]).parent, "scanner runtime root")
    need(re.fullmatch("[0-9a-f]{40}", manifest["head"]) is not None, "base SHA format")
    need(git("rev-parse", manifest["head"] + "^{tree}") == manifest["tree"], "base tree mismatch")
    need(git("merge-base", manifest["head"], "HEAD") == manifest["head"], "candidate not descended from base")
    changed = set(filter(None, git("diff", "--name-only", manifest["head"]).splitlines()))
    need(changed <= set(AUTHORED), "out-of-scope tracked changes")
    for name in AUTHORED + SCANNERS:
        plain(REPO / name)
    need(git("diff", manifest["head"], "--", *SCANNERS, "campaign_tool/records/detectors/core.py",
             "tests/records/test_detectors.py") == "", "pinned source changed")
    if args.baseline:
        need(args.phase == "suite" and git("rev-parse", "HEAD") == manifest["head"], "baseline requires initial source HEAD")
    else:
        need(git("diff", "--exit-code", "HEAD") == "", "tracked/index bytes differ from final HEAD")
        tracked = set(git("ls-files").splitlines())
        need(set(AUTHORED) <= tracked, "authored files must be committed")
        others = set(filter(None, git("ls-files", "--others", "--exclude-standard").splitlines()))
        need(others <= {".venv"}, "unexpected untracked scan-visible material")
    return manifest, out


def isolated_policy(root):
    # Only dedicated fresh script processes install irreversible hooks.
    tempfile.tempdir = str(root)
    sys.dont_write_bytecode = True
    def audit(event, arguments):
        if event in {"socket.connect", "socket.bind", "socket.getaddrinfo", "socket.gethostbyname", "os.system"}:
            raise RuntimeError("offline call graph forbids " + event)
    sys.addaudithook(audit)


def worker(args, manifest):
    row = plain(args.row)
    need(row.parent == plain(args.out_base) and row.is_dir(), "worker output row")
    report = plain(args.report)
    need(report.parent == row, "worker report outside row")
    isolated_policy(row)
    sys.path.insert(0, str(REPO))
    if args.worker == "discovery":
        # Snapshot BEFORE importing the authored test; no product module imported yet.
        before = {"environment": dict(os.environ), "tempdir": tempfile.tempdir,
                  "gettempdir": tempfile.gettempdir(), "socket_connect": socket.socket.connect,
                  "socket_bind": socket.socket.bind, "popen": subprocess.Popen,
                  "system": os.system, "sys_path": list(sys.path)}
        sentinel = row / "unrelated-sentinel.txt"
        sentinel.write_text("unchanged peer sentinel\n")
        sentinel_before = sha(sentinel)
        loader = unittest.TestLoader()
        suite = loader.discover(str(REPO / "tests/records"), pattern="test_fc10_purpose_rate_oracle.py", top_level_dir=str(REPO))
        after_import = {"environment": dict(os.environ), "tempdir": tempfile.tempdir,
                        "gettempdir": tempfile.gettempdir(), "socket_connect": socket.socket.connect,
                        "socket_bind": socket.socket.bind, "popen": subprocess.Popen,
                        "system": os.system, "sys_path": list(sys.path)}
        need(before == after_import, "discovery contamination")
        module = importlib.import_module("tests.records.test_fc10_purpose_rate_oracle")
        data = module.run_recorded(suite, report)
        # Real unrelated existing peer suite checks scoped patch cleanup.
        peer = module.run_recorded(loader.loadTestsFromName("tests.records.test_detectors.DetectorTests.test_input_not_mutated"), row / "peer.json")
        after_run = {"environment": dict(os.environ), "tempdir": tempfile.tempdir,
                     "gettempdir": tempfile.gettempdir(), "socket_connect": socket.socket.connect,
                     "socket_bind": socket.socket.bind, "popen": subprocess.Popen,
                     "system": os.system, "sys_path": list(sys.path)}
        need(before == after_run and sha(sentinel) == sentinel_before, "suite contaminated unrelated sentinel/ambient state")
        dump(row / "discovery-sentinel.json", {"before_after_equal": True, "sentinel_sha256": sentinel_before,
                                               "peer_passed": peer["passed"], "peer_tests": peer["tests_run"]})
        need(data["passed"] and peer["passed"], "discovery or peer failed")
        return 0
    module = importlib.import_module("tests.records.test_fc10_purpose_rate_oracle")
    if args.worker == "compatibility":
        with module.offline():
            suite = unittest.defaultTestLoader.loadTestsFromName("tests.records.test_detectors")
            data = module.run_recorded(suite, report)
    elif args.worker == "control":
        with module.offline():
            core = importlib.import_module("campaign_tool.records.detectors.core")
            original = core._aggregate
            with module.inclusive_control(core) as mutation:
                data = module.run_recorded(unittest.defaultTestLoader.loadTestsFromName(
                    "test_exact_five_percent_deciding", module.PurposeRateOracleTests), report)
            need(core._aggregate is original, "mutation not restored")
        need(data["tests_run"] == 1 and len(data["failures"]) == 1 and not data["errors"] and not data["skips"], "control failed for unrelated reason")
        marker = module.specification()["negative_control"]["expected_failure_marker"]
        need(marker in data["failures"][0][1], "wrong deciding assertion")
        need(data["observation_count"] == 1, "control did not reach deciding observation")
        observation = data["observations"][0]
        need(observation["counts"]["evaluated"] == 20 and observation["counts"]["blocked"] == 0, "control prerequisite invalid")
        need({tuple(x["scope"]) for x in observation["actual"]} == {("overall",), ("user", "alpha")}, "control grouping broken")
        need(all(x["severity"] == 2 and x["qualified_observations"] == 20 and x["qualified_problem_observations"] == 1
                 for x in observation["actual"]), "control not isolated to exact boundary")
        dump(row / "mutation.json", {**mutation, "restored": True, "killed_at": data["failures"][0][0],
                                     "marker": marker, "expected_failure": True})
        return 0
    else:
        data = module.run_recorded(unittest.defaultTestLoader.loadTestsFromTestCase(module.PurposeRateOracleTests), report)
    need(data["passed"], "required tests failed or skipped")
    return 0


def run_child(row, name, argv, cwd, commands, allowed=(0,)):
    record = {"name": name, "argv": argv, "cwd": str(cwd), "interpreter_or_executable": argv[0]}
    stdout, stderr = row / (name + ".stdout"), row / (name + ".stderr")
    # File redirection is performed without a shell; both streams survive failure.
    with stdout.open("wb") as out, stderr.open("wb") as err:
        child = subprocess.run(argv, cwd=cwd, stdout=out, stderr=err, shell=False, check=False, timeout=600)
    record.update(exit=child.returncode, stdout=str(stdout), stderr=str(stderr),
                  stdout_sha256=sha(stdout), stderr_sha256=sha(stderr))
    commands.append(record)
    dump(row / "commands.json", commands)
    need(child.returncode in allowed, name + " unexpected exit " + str(child.returncode))
    return child.returncode, stdout


def validate_coverage(path):
    data = json.loads(path.read_text())
    need(data["passed"] and data["tests_run"] == 10 and not data["skips"], "required suite coverage")
    cases = data["observations"]
    spec = json.loads((REPO / AUTHORED[1]).read_text())
    grid = {(n, b) for n in range(spec["grid"]["minimum_total"], spec["grid"]["maximum_total"] + 1) for b in range(n + 1)}
    for dimension in ("grid", "unequal_grid"):
        actual = [(x["case"][1], x["case"][2]) for x in cases if isinstance(x["case"], list) and x["case"][0] == dimension]
        need(set(actual) == grid and len(actual) == len(grid), "incomplete/duplicate " + dimension)
    return {"tests_run": data["tests_run"], "generated_grid_pairs_per_population": len(grid),
            "grid_population_scopes": ["overall", "user alpha"], "unequal_grid_scopes": ["overall", "user alpha", "user beta"],
            "observations": len(cases), "subtests": len(data["subtests"]), "skips": len(data["skips"]), "ids": data["ids"],
            "dimensions": spec["dimensions"]}


def phase(args, manifest, row, commands):
    python = str(Path(sys.executable).absolute())
    runner = str(Path(__file__).resolve())
    def scripted(name, mode):
        report = row / (name + ".json")
        argv = [python, "-B", runner, "--phase", args.phase, "--source-manifest", str(args.source_manifest),
                "--out-base", str(args.out_base), "--worker", mode, "--row", str(row), "--report", str(report)]
        if args.baseline:
            argv.append("--baseline")
        run_child(row, name, argv, REPO, commands)
        return report
    if args.phase == "suite":
        first = scripted("unmodified_baseline", "baseline")
        initial = validate_coverage(first)
        scripted("injected_control", "control")
        final = validate_coverage(scripted("restored_positive", "restored"))
        return {"initial": initial, "restored": final, "control": json.loads((row / "mutation.json").read_text())}
    if args.phase == "compatibility":
        data = json.loads(scripted("compatibility", "compatibility").read_text())
        need(data["tests_run"] > 0 and data["passed"], "compatibility empty/failed")
        return {"tests_run": data["tests_run"], "ids": data["ids"], "skips": len(data["skips"]), "selector": "tests.records.test_detectors"}
    if args.phase == "portable":
        host_literals = re.compile(r"/(?:home|Users|workspace|tmp|var/tmp)/")
        for name in AUTHORED:
            need(host_literals.search((REPO / name).read_text()) is None, "host-only source literal: " + name)
        cwd = row / "fresh-cwd"
        cwd.mkdir(mode=0o700)
        report = row / "portable.json"
        run_child(row, "portable", [python, "-B", str(REPO / AUTHORED[0]), "--temp-root", str(row), "--report", str(report)], cwd, commands)
        portable = validate_coverage(report)
        discovered = validate_coverage(scripted("discovery", "discovery"))
        return {"portable": portable, "discovery": discovered, "sentinel": json.loads((row / "discovery-sentinel.json").read_text()),
                "host_literal_check": list(AUTHORED)}
    if args.phase == "credentials":
        runtime = manifest["runtime"]
        need(python == runtime["credential_scan_python3"], "credential row wrong interpreter")
        # Installed scanner uses os.walk without following directory symlinks.
        # Verify all scan-visible symlinks before invoking the unchanged scanner.
        symlinks = []
        for root, directories, files in os.walk(REPO, followlinks=False):
            if Path(root) == REPO / ".git":
                directories[:] = []
                continue
            for name in directories + files:
                path = Path(root) / name
                if path.is_symlink():
                    need(path == REPO / ".venv" and path.resolve() == Path(runtime["repo_venv"]).resolve(), "unexpected scanner symlink")
                    symlinks.append(str(path.relative_to(REPO)))
        _, report = run_child(row, "detect-secrets", [python, "-B", runtime["detect_secrets"], "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"], REPO, commands)
        data = json.loads(report.read_text())
        need(isinstance(data.get("results"), dict), "credential report schema")
        need(all(not p.startswith((".venv/", "../")) and not Path(p).is_absolute() for p in data["results"]), "scanner boundary")
        run_child(row, "check-secret-report", [python, "-B", str(REPO / "tools/check_secret_report.py"), str(report)], REPO, commands)
        return {"scanner_hit_count": sum(len(v) for v in data["results"].values()), "symlinks_not_followed": symlinks,
                "scanner_argv_unchanged": True, "manual_privacy_review": "OWED"}
    if args.phase == "public-tree":
        source = identity()
        clone = row / "committed-tree"
        run_child(row, "clone", ["git", "clone", "--local", "--no-hardlinks", "--no-checkout", "--no-recurse-submodules", str(REPO), str(clone)], REPO, commands)
        run_child(row, "checkout", ["git", "checkout", "--detach", source["head"]], clone, commands)
        need(git("rev-parse", "HEAD", cwd=clone) == source["head"] and git("rev-parse", "HEAD^{tree}", cwd=clone) == source["tree"], "clone identity mismatch")
        need(not (clone / ".venv").exists(), "clone has runtime link")
        for name in AUTHORED + SCANNERS:
            need(sha(clone / name) == sha(REPO / name), "committed-tree hash mismatch: " + name)
        _, output = run_child(row, "committed-tree-scan", [python, "-B", str(clone / "tools/check_public_tree.py")], clone, commands)
        data = json.loads(output.read_text())
        tracked_count = len(git("ls-files", cwd=clone).splitlines())
        need(data.get("findings") == [] and data.get("scanned_files") == tracked_count, "incomplete/nonempty committed-tree scan")
        exit_code, output = run_child(row, "in_place_informational", [python, "-B", str(REPO / "tools/check_public_tree.py")], REPO, commands, allowed=(0, 1))
        informational = json.loads(output.read_text())
        permitted = [[".venv", 0, "symlink_not_scanned"]]
        need((exit_code == 0 and informational.get("findings") == []) or
             (exit_code == 1 and informational.get("findings") == permitted), "INC-174 unexpected informational outcome")
        need(informational.get("scanned_files") == tracked_count, "in-place scan incomplete")
        need(identity() == source, "source changed during public scan")
        return {"committed_tree": data, "tracked_files": tracked_count, "in_place_informational": informational,
                "in_place_exit": exit_code, "INC-174": True, "source_rechecked": True}
    raise RuntimeError("unknown phase")


def seal(row):
    # Exclude only this manifest itself; enumerate every retained evidence file,
    # including the committed clone. Never follow a symlink or reseal old rows.
    target = row / "evidence-manifest.json"
    need(not target.exists(), "refuse resealing")
    files = []
    for root, directories, names in os.walk(row, followlinks=False):
        need(all(not (Path(root) / n).is_symlink() for n in directories + names), "receipt symlink")
        for name in names:
            path = Path(root) / name
            relative = path.relative_to(row).as_posix()
            need(path.resolve().relative_to(row).as_posix() == relative, "noncanonical receipt name")
            files.append({"path": relative, "size": path.stat().st_size, "sha256": sha(path)})
    files.sort(key=lambda f: f["path"])
    need(len({f["path"] for f in files}) == len(files), "duplicate receipt names")
    dump(target, {"schema": "fc10-evidence-manifest/v1", "files": files})
    loaded = json.loads(target.read_text())["files"]
    actual = {p.relative_to(row).as_posix() for p in row.rglob("*") if p.is_file() and p != target}
    need(actual == {f["path"] for f in loaded}, "omitted receipt output")
    for entry in loaded:
        need(sha(row / entry["path"]) == entry["sha256"], "receipt hash mismatch")
    return {"path": str(target), "sha256": sha(target), "files": len(files)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", required=True, choices=("suite", "compatibility", "portable", "credentials", "public-tree"))
    parser.add_argument("--source-manifest", required=True, type=Path)
    parser.add_argument("--out-base", required=True, type=Path)
    parser.add_argument("--baseline", action="store_true", help="test-only precommit observation, never final acceptance")
    parser.add_argument("--worker", choices=("baseline", "control", "restored", "compatibility", "discovery"))
    parser.add_argument("--row", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    sys.dont_write_bytecode = True
    manifest, out = validate(args)
    if args.worker:
        return worker(args, manifest)
    out.mkdir(mode=0o700, exist_ok=True)
    row = out / (args.phase + "-" + str(uuid.uuid4()))
    row.mkdir(mode=0o700)
    isolated_policy(row)
    commands = []
    receipt = {"schema": "fc10-row-receipt/v1", "phase": args.phase,
               "argv": [sys.executable, *sys.argv], "cwd": str(Path.cwd()), "interpreter": sys.executable,
               "source_manifest": str(args.source_manifest), "source_manifest_sha256": sha(args.source_manifest),
               "candidate": identity(), "spec_sha256": sha(REPO / AUTHORED[1]),
               "label": "precommit_unmodified_baseline" if args.baseline else "final_candidate_REVIEW_REQUIRED",
               "limitations": ["offline synthetic triage only", "remote exact-head shipping CI OWED to operator",
                               "independent review OWED", "no semantic/legal/human/live acceptance"]}
    exit_code = 1
    try:
        receipt["results"] = phase(args, manifest, row, commands)
        need(identity() == receipt["candidate"], "candidate changed during row")
        exit_code = 0
    except Exception:
        (row / "error.txt").write_text(traceback.format_exc())
        traceback.print_exc()
    receipt["exit"] = exit_code
    receipt["commands"] = commands
    dump(row / "receipt.json", receipt)
    evidence = seal(row)
    print(json.dumps({"row": str(row), "exit": exit_code, "evidence_manifest": evidence,
                      "receipt": str(row / "receipt.json"), "label": receipt["label"]}, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
