"""Five bounded offline FC14 acceptance rows; receipts are append-only per invocation."""
import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import traceback
import uuid

sys.dont_write_bytecode = True
REPO = Path(__file__).resolve().parents[1]
PRODUCT = "campaign_tool/records/understanding/reconcile.py"
TEST = "tests/records/test_fc14_reconcile_snapshot.py"
FIXTURE = "tests/fixtures/fc14-reconcile-snapshot/cases.json"
RUNNER = "tools/fc14_reconcile_snapshot_acceptance.py"
BASELINE = "tests/fixtures/fc14-reconcile-snapshot/reconcile_baseline.py"
AUTHORED = (PRODUCT, TEST, FIXTURE, RUNNER, BASELINE)
UNCHANGED = ("tools/check_secret_report.py", "tools/check_public_tree.py",
             "campaign_tool/records/public_scan.py", ".gitignore", "tests/records/test_understanding.py")
REQUIRED = ("citation_input_to_output", "citation_output_to_input", "explicit_rule_pointer",
            "default_pointer_control", "independent_calls", "classification_branches",
            "identity_preserved", "no_semantic_acceptance", "empty_invalid_versions",
            "unresolved_version_source", "mutable_graph_disjoint")


def need(ok, message):
    if not ok:
        raise ValueError(message)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def dump(path, value):
    with path.open("x") as stream:
        json.dump(value, stream, sort_keys=True, indent=2)
        stream.write("\n")


def no_symlinks(path):
    need(path.is_absolute(), "absolute_path_required")
    need(not any(p.is_symlink() for p in (path, *path.parents)), "symlink_path_rejected")


class Row:
    def __init__(self, args):
        no_symlinks(args.out_base)
        no_symlinks(args.source_manifest)
        need(REPO not in args.out_base.parents and args.out_base != REPO, "output_must_be_outside_repo")
        args.out_base.mkdir(parents=True, exist_ok=True)
        self.root = args.out_base / (args.phase + "-" + str(uuid.uuid4()))
        self.root.mkdir(mode=0o700)
        self.record = {"schema": "fc14-acceptance/v1", "phase": args.phase,
                       "argv": [sys.executable, *sys.argv], "cwd": str(Path.cwd()),
                       "interpreter": sys.executable, "interpreter_resolved": str(Path(sys.executable).resolve()),
                       "source_manifest": str(args.source_manifest),
                       "source_manifest_sha256": digest(args.source_manifest.read_bytes()),
                       "commands": [], "passed": False,
                       "actual_material_author": {"model": "gpt-6.1-sol", "provider": "openai-codex",
                                                  "profile": "sol-reviewer", "run_id": "2083"},
                       "semantic_acceptance": False, "review_required": True}
        self.args = args
        self.manifest = json.loads(args.source_manifest.read_text())

    def child(self, label, argv, cwd=REPO, expected=(0,)):
        need(isinstance(argv, list) and all(type(a) is str for a in argv), "literal_argv_required")
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", TMPDIR=str(self.root))
        completed = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, timeout=300, shell=False)
        stdout, stderr = self.root / (label + ".stdout"), self.root / (label + ".stderr")
        stdout.write_bytes(completed.stdout)
        stderr.write_bytes(completed.stderr)
        self.record["commands"].append({"label": label, "argv": argv, "cwd": str(cwd),
                                        "exit": completed.returncode,
                                        "stdout": stdout.name, "stdout_sha256": digest(completed.stdout),
                                        "stderr": stderr.name, "stderr_sha256": digest(completed.stderr)})
        need(completed.returncode in expected, label + "_unexpected_exit_" + str(completed.returncode))
        return completed.stdout

    def git(self, label, *args, cwd=REPO):
        return self.child(label, ["git", *args], cwd).decode().strip()

    def identity(self, label, cwd=REPO):
        return {"head": self.git(label + "-head", "rev-parse", "HEAD", cwd=cwd),
                "tree": self.git(label + "-tree", "rev-parse", "HEAD^{tree}", cwd=cwd),
                "status": self.git(label + "-status", "status", "--porcelain=v1", "--untracked-files=all", cwd=cwd)}

    def verify_staging(self):
        m = self.manifest
        need(m["schema"] == "agentorange.source-manifest/v1", "manifest_schema")
        need(Path(m["repo"]) == REPO, "manifest_repo")
        no_symlinks(REPO)
        need(Path(m["write_root"]) == REPO.parent, "manifest_write_root")
        need(REPO.parent in self.args.out_base.parents, "out_base_must_stay_in_candidate")
        need((REPO / ".venv").is_symlink(), "operator_venv_link_required")
        need((REPO / ".venv").resolve() == Path(m["runtime"]["repo_venv"]), "venv_manifest_mismatch")
        fixture = json.loads((REPO / FIXTURE).read_text())
        self.fixture = fixture
        need(m["head"] == "".join(fixture["baseline_head_parts"]), "baseline_head_pin")
        need(self.git("baseline-tree", "rev-parse", m["head"] + "^{tree}") == m["tree"], "baseline_tree_pin")
        need(self.git("ancestor", "merge-base", m["head"], "HEAD") == m["head"], "candidate_not_descendant")
        baseline = self.child("baseline-source", ["git", "show", m["head"] + ":" + PRODUCT])
        need(digest(baseline) == "".join(fixture["baseline_source_sha256_parts"]), "baseline_source_pin")
        need((REPO / BASELINE).read_bytes() == baseline, "baseline_fixture_mismatch")
        self.record["baseline_source_sha256"] = digest(baseline)
        need(set(fixture["cases"]) == set(REQUIRED) - {"empty_invalid_versions", "unresolved_version_source", "mutable_graph_disjoint"}, "required_case_inventory")
        self.record["frozen_fixture"] = fixture
        self.record["source_hashes"] = {p: digest((REPO / p).read_bytes()) for p in (*AUTHORED, *UNCHANGED)}
        for index, path in enumerate(UNCHANGED):
            raw = self.child("unchanged-" + str(index), ["git", "show", m["head"] + ":" + path])
            need(digest(raw) == self.record["source_hashes"][path], "outside_scope_change_" + path)
        changed = self.git("changed-paths", "diff", "--name-only", m["head"]).splitlines()
        need(set(changed) <= set(AUTHORED), "exclusive_write_set")
        self.record["candidate_before"] = self.identity("before")
        self.child("complete-diff", ["git", "diff", "--binary", m["head"]])
        if self.args.phase == "credentials":
            need(Path(sys.executable).absolute() == Path(m["runtime"]["credential_scan_python3"]), "credential_interpreter_mismatch")
        else:
            need(Path(sys.executable).absolute() == REPO / ".venv/bin/python3", "row_interpreter_mismatch")

    def matrix(self, label, mode="matrix", unrelated=False):
        root = self.root / label
        root.mkdir()
        report = self.root / (label + ".json")
        expected = (1,) if mode == "negative" else (0,)
        self.child(label, [sys.executable, str(REPO / TEST), "--temporary-root", str(root),
                           "--report", str(report), "--mode", mode], root if unrelated else REPO,
                   expected=expected)
        data = json.loads(report.read_text())
        need(not data["skipped"] and not data["errors"], "missing_or_error_cases")
        if mode == "negative":
            need(data["tests_run"] == 1 and len(data["failures"]) == 1 and not data["successful"], "mutant_not_killed")
            need(data["failures"][0][0].endswith(".test_citation_input_to_output"), "wrong_mutant_test")
            need("FC14_INPUT_SNAPSHOT_CHANGED" in data["failures"][0][1], "wrong_mutant_assertion")
        else:
            need(data["successful"] and not data["failures"], "suite_not_successful")
            if mode == "compatibility":
                expected_ids = {"tests.records.test_understanding.ReconcileTests." + name for name in (
                    "test_classifications_never_reach_legal_conclusions", "test_uncited_facts_become_gaps")}
                need(set(data["test_ids"]) == expected_ids and data["tests_run"] == 2, "compatibility_selector")
            else:
                names = [i.rsplit(".", 1)[-1] for i in data["test_ids"] if ".SnapshotTests." in i]
                need(Counter(names) == Counter("test_" + n for n in REQUIRED), "full_matrix_required")
                need(data["tests_run"] == len(REQUIRED) + int(mode == "discovery"), "test_count")
                counts = Counter(d["test"].rsplit(".", 1)[-1] for d in data["dimensions"])
                n, p = len(self.fixture["branches"]), len(self.fixture["pointer_modes"])
                need(counts == Counter({"test_explicit_rule_pointer": 2, "test_independent_calls": p,
                                        "test_classification_branches": n*p, "test_empty_invalid_versions": 3,
                                        "test_unresolved_version_source": 2, "test_mutable_graph_disjoint": n*p,
                                        "test_identity_preserved": n*p*2, "test_no_semantic_acceptance": n}),
                     "full_case_dimensions_required")
                need(all(d["passed"] for d in data["dimensions"]), "failed_dimension")
                if mode == "discovery":
                    need(data["discovery_checks"]["before_peer_tests"] == 1, "discovery_peer_missing")
        return data

    def public_tree(self):
        need(not self.record["candidate_before"]["status"], "committed_clean_head_required")
        head = self.record["candidate_before"]["head"]
        tracked = self.git("tracked", "ls-files", "-z").split("\0")
        need(set(AUTHORED) <= set(tracked), "authored_files_must_be_committed")
        self.child("index-equal", ["git", "diff", "--cached", "--exit-code", "HEAD"])
        self.child("worktree-equal", ["git", "diff", "--exit-code", "HEAD"])
        visible = set(self.git("scan-visible", "ls-files", "-z", "--cached", "--others", "--exclude-standard").split("\0")) - {""}
        need(visible - (set(tracked) - {""}) <= {".venv"}, "untracked_scan_visible_file")
        clone = self.root / "committed-tree"
        self.child("clone", ["git", "clone", "--local", "--no-hardlinks", "--no-checkout", "--no-recurse-submodules", str(REPO), str(clone)])
        self.child("checkout", ["git", "checkout", "--detach", head], clone)
        cloned = self.identity("clone", clone)
        need(cloned == dict(self.record["candidate_before"], status=""), "clone_identity_mismatch")
        for path, sha in self.record["source_hashes"].items():
            need(digest((clone / path).read_bytes()) == sha, "clone_source_hash_mismatch")
        result = json.loads(self.child("committed-public-tree", [sys.executable, str(clone / "tools/check_public_tree.py")], clone))
        need(result["findings"] == [] and result["scanned_files"] == len(set(tracked) - {""}), "committed_tree_findings_or_incomplete")
        self.record["committed_tree_result"] = result
        informational = json.loads(self.child("in-place-informational", [sys.executable, str(REPO / "tools/check_public_tree.py")], expected=(0, 1)))
        actual_exit = self.record["commands"][-1]["exit"]
        findings = informational.get("findings")
        need((actual_exit == 0 and findings == []) or
             (actual_exit == 1 and findings == [[".venv", 0, "symlink_not_scanned"]]), "unrecognized_in_place_result")
        need(informational["scanned_files"] == result["scanned_files"], "in_place_scan_incomplete")
        self.record["in_place_informational_INC_174"] = informational
        self.record["clone_identity"] = cloned

    def run(self):
        self.verify_staging()
        phase = self.args.phase
        if phase == "suite":
            self.matrix("matrix-positive")
            self.matrix("reconnected-citation-mutant", "negative")
            self.matrix("restored-positive")
        elif phase == "compatibility":
            self.matrix("compatibility", "compatibility")
        elif phase == "portable":
            self.matrix("absolute-unrelated-cwd", unrelated=True)
            self.matrix("ordinary-discovery", "discovery", unrelated=True)
            literals = []
            for path in (TEST, FIXTURE, RUNNER):
                raw = (REPO / path).read_text()
                # Construct patterns so this inspection does not match itself.
                if re.search("/" + "home/|/" + "Users/|/" + "workspace/|agent-work/" + "candidates/", raw):
                    literals.append(path)
            need(not literals, "box_specific_path_literal")
        elif phase == "credentials":
            runtime = self.manifest["runtime"]
            output = self.child("detect-secrets", [runtime["detect_secrets"], "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"])
            json.loads(output)
            report = self.root / "detect-secrets.json"
            report.write_bytes(output)
            self.child("check-secret-report", [sys.executable, str(REPO / "tools/check_secret_report.py"), str(report)])
        elif phase == "public-tree":
            self.public_tree()
        after = self.identity("after")
        need(after == self.record["candidate_before"], "candidate_changed_during_row")
        for path, sha in self.record["source_hashes"].items():
            need(digest((REPO / path).read_bytes()) == sha, "source_changed_during_row")
        self.record["candidate_after"] = after
        self.record["passed"] = True

    def seal(self):
        # Every child is synchronous and has exited before this final inventory.
        self.record["evidence_files"] = {str(p.relative_to(self.root)): {"sha256": digest(p.read_bytes()), "bytes": p.stat().st_size}
                                         for p in sorted(self.root.rglob("*")) if p.is_file() and "committed-tree" not in p.relative_to(self.root).parts}
        manifest = self.root / "evidence-manifest.json"
        dump(manifest, self.record)
        print(json.dumps({"phase": self.args.phase, "passed": self.record["passed"],
                          "evidence_manifest": str(manifest), "sha256": digest(manifest.read_bytes()),
                          "head": self.record.get("candidate_before", {}).get("head")}, sort_keys=True))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", required=True, choices=("suite", "compatibility", "portable", "credentials", "public-tree"))
    parser.add_argument("--source-manifest", required=True, type=Path)
    parser.add_argument("--out-base", required=True, type=Path)
    args = parser.parse_args()
    row = Row(args)
    try:
        row.run()
    except Exception as exc:
        row.record["error"] = str(exc)
        row.record["traceback"] = traceback.format_exc()
    row.seal()
    return 0 if row.record["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
