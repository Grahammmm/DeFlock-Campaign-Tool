"""Five bounded offline FC15 evidence rows; machine pass is REVIEW REQUIRED.

Only stdlib is imported here. Product tests run in dedicated guarded processes.
No output is written to the repository, and every invocation retains fresh logs.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

REPO = Path(__file__).resolve().parents[1]
PRODUCT = "campaign_tool/records/understanding/correspondence.py"
TEST = "tests/records/test_fc15_message_id_case.py"
FIXTURE = "tests/fixtures/fc15-message-id-case/cases.json"
RUNNER = "tools/fc15_message_id_case_acceptance.py"
AUTHORED = (PRODUCT, TEST, FIXTURE, RUNNER)
CHECKERS = ("tools/check_secret_report.py", "tools/check_public_tree.py",
            "campaign_tool/records/public_scan.py", "campaign_tool/records/export_clearance.py")
CASE_TESTS = {case: "test_" + case for case in
              ("case_distinct", "exact_replay", "fallback_event_id", "content_conflict",
               "source_cited", "scope_and_preservation")}
EXPECTED = set(CASE_TESTS.values()) | {"test_deciding_control"}


def need(ok, reason):
    if not ok:
        raise ValueError(reason)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def git(*args, root=REPO):
    return subprocess.check_output(["git", *args], cwd=root)


def identity():
    return {"head": git("rev-parse", "HEAD").decode().strip(),
            "tree": git("rev-parse", "HEAD^{tree}").decode().strip(),
            "status": git("status", "--porcelain=v1", "--untracked-files=all").decode(),
            "diff_sha256": sha(git("diff", "HEAD", "--binary")),
            "authored": {p: sha((REPO / p).read_bytes()) for p in AUTHORED}}


class Row:
    def __init__(self, args):
        self.args = args
        self.out = args.out_base / (args.phase + "-" + str(uuid.uuid4()))
        self.out.mkdir(parents=True, exist_ok=False)
        self.data = {"schema": "fc15-acceptance/v1", "phase": args.phase,
                     "baseline": args.baseline, "review_required": True,
                     "argv": [sys.executable, *sys.argv], "cwd": str(Path.cwd()),
                     "interpreter": sys.executable, "prefix": sys.prefix,
                     "source_manifest": str(args.source_manifest),
                     "source_manifest_sha256": sha(args.source_manifest.read_bytes()),
                     "children": [], "case_to_test": CASE_TESTS,
                     "author": {"model": "gpt-6.1-sol", "provider": "openai-codex"}}

    def run(self, label, argv, cwd=REPO, allowed=(0,)):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        completed = subprocess.run([str(x) for x in argv], cwd=cwd, env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   timeout=300, shell=False)
        paths = {}
        for name, raw in (("stdout", completed.stdout), ("stderr", completed.stderr)):
            path = self.out / (label + "." + name)
            path.write_bytes(raw)
            paths[name] = {"path": str(path), "sha256": sha(raw), "bytes": len(raw)}
        self.data["children"].append({"label": label, "argv": [str(x) for x in argv],
                                      "cwd": str(cwd), "exit": completed.returncode, **paths})
        need(completed.returncode in allowed, label + "_exit_" + str(completed.returncode))
        return completed

    def tests(self, label, mode, cwd=REPO):
        report = self.out / (label + ".tests.json")
        completed = self.run(label, [sys.executable, "-B", REPO / TEST,
                                    "--mode", mode, "--temporary-root", self.out / (label + "-tmp"),
                                    "--report", report], cwd=cwd,
                             allowed=(0, 1) if self.args.baseline else (0,))
        result = json.loads(report.read_text())
        self.data.setdefault("test_reports", {})[label] = {
            "path": str(report), "sha256": sha(report.read_bytes()), **result}
        need(not result["skips"] and not result["errors"], "skipped_or_error")
        if mode == "compatibility":
            need(result["tests"] == 2 and len(result["ids"]) == 2, "compatibility_count")
        elif mode == "baseline":
            need(result["tests"] == 2, "baseline_count")
        else:
            names = {x.rsplit(".", 1)[-1] for x in result["ids"]}
            need(EXPECTED <= names and result["tests"] == (9 if mode == "peer" else 7), "test_matrix_missing")
            for case in CASE_TESTS:
                need(any("case='" + case + "'" in s["id"] for s in result["subcases"]), "subcase_missing_" + case)
        if self.args.baseline and mode != "compatibility":
            failures = dict(result["failures"])
            need(any(".test_case_distinct" in k and "case_distinct_requires_two_rows" in v
                     for k, v in failures.items()), "baseline_regression_not_deciding")
            need(not any("test_exact_replay" in k for k in failures), "baseline_replay_failed")
            need(completed.returncode == 1, "baseline_must_be_red")
        else:
            need(result["successful"] and not result["failures"], "test_failure")

    def validate(self):
        m = json.loads(self.args.source_manifest.read_text())
        need(m["schema"] == "agentorange.source-manifest/v1" and Path(m["repo"]) == REPO,
             "manifest_repo")
        need(Path(m["write_root"]) == REPO.parent and self.args.out_base == REPO.parent / "out", "output_root")
        need(Path.cwd() == REPO, "acceptance_cwd")
        need(re.fullmatch(r"[a-f0-9]{40}", m["head"]) is not None, "base_format")
        need(git("rev-parse", m["head"] + "^{tree}").decode().strip() == m["tree"], "base_tree")
        git("merge-base", "--is-ancestor", m["head"], "HEAD")
        need((REPO / ".venv").is_symlink() and (REPO / ".venv").resolve() == Path(m["runtime"]["repo_venv"]).resolve(), "runtime_link")
        expected_python = (Path(m["runtime"]["credential_scan_python3"]) if self.args.phase == "credentials"
                           else REPO / ".venv/bin/python3")
        need(Path(sys.executable) == expected_python and Path(sys.prefix).resolve() == expected_python.parent.parent.resolve(), "runtime_interpreter")
        initial = identity()
        self.data["initial"] = initial
        self.data["base"] = {"head": m["head"], "tree": m["tree"]}
        diff = git("diff", m["head"], "HEAD", "--binary")
        (self.out / "complete-candidate.diff").write_bytes(diff)
        self.data["complete_diff_sha256"] = sha(diff)
        changed = set(git("diff", "--name-only", m["head"], "HEAD").decode().splitlines())
        need(changed <= set(AUTHORED), "exclusive_write_set")
        if self.args.baseline:
            need(self.args.phase in ("suite", "compatibility") and initial["head"] == m["head"], "baseline_only_at_pin")
        else:
            need(not initial["status"], "uncommitted_candidate")
            need(changed == set(AUTHORED), "missing_committed_file")
            need(not git("diff", "HEAD", "--", *AUTHORED), "tracked_byte_mismatch")
            need(not git("diff", "--cached", "HEAD", "--", *AUTHORED), "index_byte_mismatch")
        self.data["unchanged_checkers"] = {}
        for path in CHECKERS:
            current = (REPO / path).read_bytes()
            need(current == git("show", m["head"] + ":" + path), "checker_changed_" + path)
            self.data["unchanged_checkers"][path] = sha(current)
        self.data["fixture_sha256"] = sha((REPO / FIXTURE).read_bytes())
        fixture = json.loads((REPO / FIXTURE).read_text())
        need(set(fixture["cases"]) == set(CASE_TESTS), "fixture_cases")
        for path in (TEST, FIXTURE, RUNNER):
            text = (REPO / path).read_text()
            need(re.search(r"/(?:home|Users|workspace)/", text) is None, "box_specific_path")
        return m

    def execute(self):
        m = self.validate()
        phase = self.args.phase
        if phase == "suite":
            self.tests("suite", "baseline" if self.args.baseline else "suite")
        elif phase == "compatibility":
            self.tests("compatibility", "compatibility")
        elif phase == "portable":
            cwd = self.out / "unrelated-cwd"
            cwd.mkdir()
            self.tests("absolute-script", "suite", cwd)
            self.tests("discovery-peer", "peer", cwd)
        elif phase == "credentials":
            report = self.out / "scanner.stdout"
            self.run("scanner", [m["runtime"]["detect_secrets"], "scan", "--all-files",
                                 "--exclude-files", "^.git/", "--no-verify"])
            parsed = json.loads(report.read_text())
            need(isinstance(parsed.get("results"), dict), "scanner_json")
            self.run("secret-checker", [sys.executable, "-B", REPO / "tools/check_secret_report.py", report])
        elif phase == "public-tree":
            names = git("ls-files", "-z", "--cached", "--others", "--exclude-standard").decode().split("\0")
            self.data["path_inventory"] = sorted(n for n in names if n)
            others = set(git("ls-files", "--others", "--exclude-standard").decode().splitlines())
            need(others <= {".venv"}, "untracked_scan_visible")
            clone = self.out / "committed-tree"
            self.run("clone", ["git", "clone", "--local", "--no-hardlinks", "--no-checkout",
                               "--no-recurse-submodules", REPO, clone])
            self.run("checkout", ["git", "checkout", "--detach", self.data["initial"]["head"]], cwd=clone)
            need(git("rev-parse", "HEAD", root=clone).decode().strip() == self.data["initial"]["head"], "clone_head")
            need(git("rev-parse", "HEAD^{tree}", root=clone).decode().strip() == self.data["initial"]["tree"], "clone_tree")
            for p, digest in self.data["initial"]["authored"].items():
                need(sha((clone / p).read_bytes()) == digest, "clone_file_" + p)
            need(not (clone / ".venv").exists() and not (clone / ".venv").is_symlink(), "clone_runtime_link")
            result = self.run("committed-tree", [sys.executable, "-B", clone / "tools/check_public_tree.py"], cwd=clone)
            parsed = json.loads(result.stdout)
            need(parsed["findings"] == [] and parsed["scanned_files"] > 0, "public_findings")
            self.data["committed_tree_scan"] = parsed
            result = self.run("in_place_informational", [sys.executable, "-B", REPO / "tools/check_public_tree.py"], allowed=(0, 1))
            parsed = json.loads(result.stdout)
            need((result.returncode == 0 and parsed["findings"] == []) or
                 (result.returncode == 1 and parsed["findings"] == [[".venv", 0, "symlink_not_scanned"]]),
                 "unexpected_in_place_finding")
            need(parsed["scanned_files"] > 0, "incomplete_scan")
            self.data["in_place_informational"] = parsed
        final = identity()
        self.data["final"] = final
        need(final == self.data["initial"], "candidate_changed_during_row")
        need(sha(self.args.source_manifest.read_bytes()) == self.data["source_manifest_sha256"], "manifest_changed")
        self.data["exit"] = 0
        self.data["outcome"] = "BASELINE_RECORDED" if self.args.baseline else "REVIEW_REQUIRED"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", required=True, choices=("suite", "compatibility", "portable", "credentials", "public-tree"))
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--out-base", type=Path, required=True)
    parser.add_argument("--baseline", action="store_true", help="Retain initial-pin before-fix evidence only")
    args = parser.parse_args()
    row = Row(args)
    try:
        row.execute()
    except Exception as exc:
        row.data["exit"] = 1
        row.data["error"] = type(exc).__name__ + ": " + str(exc)
    path = row.out / "row.json"
    path.write_text(json.dumps(row.data, indent=2))
    print(json.dumps({"row": str(path), "sha256": sha(path.read_bytes()),
                      "exit": row.data["exit"], "error": row.data.get("error"),
                      "outcome": row.data.get("outcome")}))
    return row.data["exit"]


if __name__ == "__main__":
    raise SystemExit(main())
