"""Five separately invoked FC10 offline acceptance rows; raw evidence is never reused."""
import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import traceback
import uuid

REPO = Path(__file__).resolve().parents[1]
AUTHORED = ("tests/records/test_fc10_evidence_token_bounds.py",
            "tests/fixtures/fc10-evidence-token-bounds/cases.json",
            "tools/fc10_evidence_token_bounds_acceptance.py")
CHECKERS = ("tools/check_public_tree.py", "campaign_tool/records/public_scan.py",
            "tools/check_secret_report.py")
SOURCE = ("campaign_tool/records/grounded_evidence.py", "campaign_tool/records/retained_units.py",
          "campaign_tool/records/understanding/evidence.py", "tests/records/test_understanding.py")
MODULE = "tests.records.test_fc10_evidence_token_bounds"
SELECTORS = ("tests.records.test_understanding.GroundedEvidenceTests",
             "tests.records.test_understanding.EvidenceTests")



def require(condition, reason):
    if not condition:
        raise RuntimeError(reason)


def offline_guard():
    # Dedicated runner process only: the admitted call graph is pure in-process API
    # plus local git/scanner children, so any in-process socket use is refused.
    import socket

    def refused(*_args, **_kwargs):
        raise RuntimeError("offline_guard_network_refused")
    socket.socket.connect = refused
    socket.socket.bind = refused
    socket.create_connection = refused


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def save(path, data):
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(data, stream, indent=2, sort_keys=True)
        stream.write("\n")


def canonical(path):
    p = Path(path)
    require(p.is_absolute() and ".." not in p.parts, "literal_absolute_path_required")
    require(not any(part.is_symlink() for part in (p, *p.parents)), "output_or_manifest_symlink")
    return p


class Evidence:
    def __init__(self, output):
        self.output = output
        self.commands = []

    def run(self, label, argv, cwd=REPO, allowed=(0,)):
        require(isinstance(argv, list) and all(isinstance(a, str) for a in argv), "literal_argv_required")
        out = self.output / ("%03d-" % len(self.commands) + label)
        out.mkdir(mode=0o700)
        run = subprocess.run(argv, cwd=cwd, capture_output=True, shell=False)
        (out / "stdout.txt").write_bytes(run.stdout)
        (out / "stderr.txt").write_bytes(run.stderr)
        receipt = {"label": label, "argv": argv, "cwd": str(cwd), "exit": run.returncode,
            "stdout_sha256": sha(run.stdout), "stderr_sha256": sha(run.stderr), "directory": str(out)}
        self.commands.append(receipt)
        save(out / "command.json", receipt)
        require(run.returncode in allowed, "command_failed:" + label + ":" + str(run.returncode))
        return run.stdout

    def git(self, label, *args, cwd=REPO):
        return self.run(label, ["git", *args], cwd).decode().strip()


def identity(e, label, repo=REPO):
    return {"head": e.git(label + "-head", "rev-parse", "HEAD", cwd=repo),
            "tree": e.git(label + "-tree", "rev-parse", "HEAD^{tree}", cwd=repo),
            "status": e.git(label + "-status", "status", "--porcelain=v1", "--untracked-files=all", cwd=repo)}


def bind(e, manifest_path, phase):
    raw = canonical(manifest_path).read_bytes()
    m = json.loads(raw)
    require(m["schema"] == "agentorange.source-manifest/v1", "manifest_schema")
    require(m["repo"] == str(REPO) and Path(m["write_root"]) == REPO.parent, "manifest_repo_identity")
    require(re.fullmatch(r"[a-f0-9]{40}", m["head"]) and re.fullmatch(r"[a-f0-9]{40}", m["tree"]), "manifest_base_pin")
    base = m["head"]  # The operator's frozen manifest is the source identity authority.
    require(e.git("base-tree", "rev-parse", base + "^{tree}") == m["tree"], "base_tree_changed")
    e.run("descended-from-base", ["git", "merge-base", "--is-ancestor", base, "HEAD"])
    measured = identity(e, "candidate")
    require(measured["head"] != base, "candidate_must_be_committed_above_base")
    require(not measured["status"], "candidate_must_be_clean")
    changed = e.git("changed-paths", "diff", "--name-only", base, "HEAD").splitlines()
    require(set(changed) == set(AUTHORED), "exclusive_authored_write_set")
    require(e.git("index-diff", "diff", "--cached", "--name-only", "HEAD") == "", "index_changed")
    hashes = {}
    for relative in AUTHORED + CHECKERS + SOURCE:
        path = REPO / relative
        require(path.is_file() and not path.is_symlink(), "regular_source_required")
        head = e.run("head-bytes-" + path.name, ["git", "show", measured["head"] + ":" + relative])
        require(head == path.read_bytes(), "working_bytes_differ_from_head:" + relative)
        if relative not in AUTHORED:
            baseline = e.run("baseline-bytes-" + path.name, ["git", "show", base + ":" + relative])
            require(head == baseline, "unchanged_checker_or_source_modified")
        hashes[relative] = sha(head)
    link = REPO / ".venv"
    require(link.is_symlink() and link.resolve() == Path(m["runtime"]["repo_venv"]).resolve(),
            "manifest_runtime_link")
    expected = (Path(m["runtime"]["credential_scan_python3"]) if phase == "credentials"
                else REPO / ".venv/bin/python3")
    require(Path(sys.executable).absolute() == expected, "literal_interpreter_identity")
    require(Path(sys.prefix).resolve() == Path(m["runtime"]["repo_venv"]).resolve(), "runtime_prefix")
    require(Path(sys.executable).resolve() == Path(m["runtime"]["system_python3"]).resolve(),
            "runtime_system_interpreter")
    return m, {**measured, "source_hashes": hashes, "manifest_sha256": sha(raw),
               "manifest_path": str(manifest_path), "interpreter": sys.executable,
               "prefix": sys.prefix, "repo_venv_resolved": str(link.resolve())}


def load_test_module():
    sys.path.insert(0, str(REPO))
    return importlib.import_module(MODULE)


def suite(e):
    module = load_test_module()
    code = module.run_suite(e.output / "suite-summary.json")
    summary = json.loads((e.output / "suite-summary.json").read_text())
    require(code == 0, "suite_requirements_failed")
    require(summary["tests_run"] > 0 and not summary["skipped"] and not summary["missing_dimensions"],
            "suite_required_coverage")
    require(summary["case_ids"]["BASELINE_REAL"] and summary["case_ids"]["CONTROL_MUTANT_INFORMATIONAL"]
            and summary["case_ids"]["RESTORED_REAL"], "separate_baseline_control_restored_observations")
    return {key: summary[key] for key in ("tests_run", "failures", "errors", "skipped", "passed_test_ids",
            "dimensions", "missing_dimensions", "case_ids")}


def compatibility(e):
    import unittest
    sys.path.insert(0, str(REPO))
    selected = unittest.TestLoader().loadTestsFromNames(list(SELECTORS))
    # Raw unittest output is a real file, not a synthesized count.
    with (e.output / "unittest.txt").open("x") as stream:
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(selected)
    summary = {"selectors": list(SELECTORS), "tests_run": result.testsRun, "failures": len(result.failures),
               "errors": len(result.errors), "skipped": len(result.skipped),
               "full_discovery": "operator-run"}
    save(e.output / "compatibility-summary.json", summary)
    require(result.wasSuccessful() and result.testsRun > 0 and not result.skipped, "compatibility_failed")
    return summary


def portable(e):
    for relative in AUTHORED:
        text = (REPO / relative).read_text()
        forbidden = re.compile(r"/(?:home|Users|workspace)/|[A-Z]:\\\\|(?:PYTHONPATH|LD_LIBRARY_PATH)\s*=")
        require(not forbidden.search(text), "host_specific_authored_literal:" + relative)
    save(e.output / "static-portability.json", {"checked": list(AUTHORED), "findings": []})
    summaries = {}
    for mode in ("suite", "discovery"):
        fresh = e.output / ("foreign-cwd-" + mode)
        fresh.mkdir(mode=0o700)
        e.run(mode, [sys.executable, str(REPO / AUTHORED[0]), "--mode", mode,
                     "--temp-root", str(fresh)], cwd=fresh)
        summaries[mode] = json.loads((fresh / "suite-summary.json").read_text())
        require(not summaries[mode]["missing_dimensions"] and not summaries[mode]["skipped"],
                "portable_required_coverage")
    require(summaries["suite"]["dimensions"] == summaries["discovery"]["dimensions"]
            and summaries["suite"]["tests_run"] == summaries["discovery"]["tests_run"]
            and summaries["suite"]["case_ids"] == summaries["discovery"]["case_ids"],
            "script_discovery_coverage_mismatch")
    return summaries


def credentials(e, m):
    scanner = Path(m["runtime"]["detect_secrets"])
    require(scanner.is_absolute() and scanner.is_file() and os.access(scanner, os.X_OK), "scanner_missing")
    require(scanner.parent == Path(m["runtime"]["credential_scan_python3"]).parent, "scanner_runtime_binding")
    first_line = scanner.read_text().splitlines()[0]
    require(first_line == "#!" + m["runtime"]["credential_scan_python3"], "scanner_shebang_binding")
    raw = e.run("detect-secrets", [str(scanner), "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"])
    parsed = json.loads(raw)
    require(isinstance(parsed.get("results"), dict), "genuine_scanner_json_required")
    report = e.output / "detect-secrets.json"
    with report.open("xb") as stream:
        stream.write(raw)
    e.run("secret-report", [sys.executable, str(REPO / "tools/check_secret_report.py"), str(report)])
    return {"scanner_report_sha256": sha(raw), "report": str(report),
            "scanner_sha256": sha(scanner.read_bytes()), "potential_hit_files": len(parsed["results"])}


def public_tree(e, m, candidate):
    # Inventory includes the ignored standard link without traversing it.
    e.run("literal-status-including-ignored", ["git", "status", "--short", "--ignored", "--untracked-files=all"])
    raw_inventory = e.run("scan-visible-path-inventory", ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"])
    visible = set(raw_inventory.decode().split("\0")) - {""}
    tracked = set(e.run("tracked-inventory", ["git", "ls-files", "-z"]).decode().split("\0")) - {""}
    require(visible - tracked <= {".venv"}, "untracked_scan_visible_files")
    require(set(AUTHORED) <= tracked, "authored_files_not_tracked")
    clone = e.output / "committed-tree"
    e.run("local-clone", ["git", "clone", "--local", "--no-hardlinks", "--no-checkout",
                          "--no-recurse-submodules", str(REPO), str(clone)])
    e.run("exact-checkout", ["git", "checkout", "--detach", candidate["head"]], cwd=clone)
    cloned = identity(e, "cloned", clone)
    require(cloned["head"] == candidate["head"] and cloned["tree"] == candidate["tree"]
            and not cloned["status"], "clone_identity")
    require(not (clone / ".venv").exists() and not (clone / ".venv").is_symlink(), "clone_runtime_link_forbidden")
    for relative, expected in candidate["source_hashes"].items():
        require(sha((clone / relative).read_bytes()) == expected, "clone_hash_mismatch:" + relative)
    public = json.loads(e.run("committed-tree-public-scan", [sys.executable, str(clone / "tools/check_public_tree.py")], cwd=clone))
    require(public.get("findings") == [] and public.get("scanned_files", 0) == len(tracked), "incomplete_public_scan")
    # Preserve unchanged staged invocation; never call an informational result a pass.
    raw = e.run("in_place_informational-INC-174", [sys.executable, str(REPO / "tools/check_public_tree.py")], allowed=(0, 1))
    informational = json.loads(raw)
    findings = informational.get("findings")
    code = e.commands[-1]["exit"]
    require((code == 0 and findings == []) or
            (code == 1 and findings == [[".venv", 0, "symlink_not_scanned"]]), "unexpected_staged_finding")
    require(informational.get("scanned_files", 0) == len(tracked), "incomplete_in_place_scan")
    require(identity(e, "source-recheck") == {key: candidate[key] for key in ("head", "tree", "status")},
            "source_changed_during_scan")
    return {"committed_tree": public, "clone": str(clone), "clone_identity": cloned,
            "in_place_informational": {"issue": "INC-174", "exit": code, "result": informational}}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", required=True, choices=("suite", "compatibility", "portable", "credentials", "public-tree"))
    parser.add_argument("--source-manifest", required=True)
    parser.add_argument("--out-base", required=True)
    args = parser.parse_args()
    base = canonical(args.out_base)
    require(base.parent == REPO.parent and base.name == "out", "admitted_out_base_required")
    base.mkdir(mode=0o700, exist_ok=True)
    output = base / (args.phase + "-" + str(uuid.uuid4()))
    output.mkdir(mode=0o700)
    temp = output / "temporary"
    temp.mkdir(mode=0o700)
    tempfile.tempdir = str(temp)  # precedes every product or test import
    offline_guard()
    e = Evidence(output)
    receipt = {"schema": "fc10-acceptance-row/v1", "phase": args.phase, "output": str(output),
        "invocation": {"argv": [sys.executable, *sys.argv], "cwd": str(Path.cwd()), "interpreter": sys.executable},
        "status": "FAILED", "semantic_acceptance": False, "remote_ci": "OWED"}
    code = 1
    try:
        require(Path.cwd() == REPO, "acceptance_cwd")
        m, candidate = bind(e, Path(args.source_manifest), args.phase)
        receipt["candidate"] = candidate
        if args.phase == "suite":
            result = suite(e)
        elif args.phase == "compatibility":
            result = compatibility(e)
        elif args.phase == "portable":
            result = portable(e)
        elif args.phase == "credentials":
            result = credentials(e, m)
        else:
            result = public_tree(e, m, candidate)
        require(identity(e, "final") == {key: candidate[key] for key in ("head", "tree", "status")}, "final_identity_changed")
        require(sha(Path(args.source_manifest).read_bytes()) == candidate["manifest_sha256"], "manifest_changed")
        receipt.update(status="PASS_REVIEW_REQUIRED", result=result)
        code = 0
    except Exception as error:
        receipt["error"] = {"type": type(error).__name__, "message": str(error)}
        with (output / "exception.txt").open("x") as stream:
            traceback.print_exc(file=stream)
    receipt["commands"] = e.commands
    receipt["exit"] = code
    # Do not traverse the clone's .git or symlinks while sealing task evidence.
    receipt["artifact_hashes"] = {str(p.relative_to(output)): sha(p.read_bytes())
        for p in sorted(output.rglob("*")) if p.is_file() and not p.is_symlink()
        and "committed-tree" not in p.relative_to(output).parts}
    save(output / "row.json", receipt)
    print(json.dumps({"phase": args.phase, "exit": code, "status": receipt["status"],
                      "row": str(output / "row.json"), "row_sha256": sha((output / "row.json").read_bytes()),
                      "error": receipt.get("error"), "result": receipt.get("result")}, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
