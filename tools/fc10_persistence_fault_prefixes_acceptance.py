"""Bounded offline acceptance; each invocation retains fresh, hashed evidence."""
import argparse
from contextlib import ExitStack
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
import uuid
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
ROOT = REPO.parent
BASE = "17da706588467184f0923e2516be3319d577539a"
AUTHORED = ("tests/records/test_fc10_persistence_fault_prefixes.py",
            "tests/fixtures/fc10-persistence-fault-prefixes/cases.json",
            "tools/fc10_persistence_fault_prefixes_acceptance.py")
UNCHANGED = ("campaign_tool/records/detectors/persistence.py", "tests/records/test_detector_persistence.py",
             "campaign_tool/records/ledger/migrations/v001.py", "campaign_tool/records/ledger/store.py",
             "campaign_tool/records/detectors/migrations/persistence_v001.py",
             "tools/check_secret_report.py", "tools/check_public_tree.py", "campaign_tool/records/public_scan.py")
LIMITS = ["REVIEW REQUIRED; not semantic, human, legal, live or publication acceptance.",
          "Scoped local compatibility only; remote exact-head shipping CI remains OWED to the existing operator.",
          "Synthetic in-memory SQLite only. No canonical stores, real records, network or model calls.",
          "In-place public scan is informational (INC-174); only the committed clone is the A5 deciding scan."]


def require(value, reason):
    if not value:
        raise ValueError(reason)


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_path(path):
    require(path.is_absolute() and path == path.resolve(), "canonical_absolute_path_required:" + str(path))
    for part in (path, *path.parents):
        require(not part.is_symlink(), "symlink_ancestor_refused:" + str(part))
    return path


class Row:
    def __init__(self, args):
        self.args = args
        self.manifest_path = canonical_path(args.source_manifest)
        self.out_base = canonical_path(args.out_base)
        require(self.manifest_path == ROOT / "SOURCE-MANIFEST.json" and self.out_base == ROOT / "out", "staging_paths_mismatch")
        self.source = json.loads(self.manifest_path.read_text())
        require(self.source["schema"] == "agentorange.source-manifest/v1", "source_manifest_schema")
        require(self.source["head"] == BASE and Path(self.source["repo"]) == REPO
                and Path(self.source["write_root"]) == ROOT, "source_manifest_identity")
        canonical_path(REPO)
        self.runtime = self.source["runtime"]
        self.python = str(REPO / ".venv/bin/python3")
        self.scan_python = self.runtime["credential_scan_python3"]
        expected_python = self.scan_python if args.phase == "credentials" else self.python
        require(os.path.abspath(sys.executable) == expected_python, "literal_interpreter_mismatch")
        require((REPO / ".venv").is_symlink() and (REPO / ".venv").resolve() == Path(self.runtime["repo_venv"]), "standard_runtime_link_mismatch")
        self.out_base.mkdir(mode=0o700, exist_ok=True)
        require(self.out_base.stat().st_uid == os.getuid() and not self.out_base.stat().st_mode & 0o077, "output_root_must_be_owner_private")
        self.out = self.out_base / (args.phase + "-" + str(uuid.uuid4()))
        self.out.mkdir(mode=0o700)
        self.temp = self.out / "tmp"
        self.temp.mkdir(mode=0o700)
        tempfile.tempdir = str(self.temp)
        sys.dont_write_bytecode = True
        self.calls = []
        self.details = {}
        self.head = self.git("rev-parse", "HEAD").strip()
        self.tree = self.git("rev-parse", "HEAD^{tree}").strip()
        require(self.git("rev-parse", BASE + "^{tree}").strip() == self.source["tree"], "base_tree_mismatch")
        self.git("merge-base", "--is-ancestor", BASE, self.head)
        if args.phase == "baseline":
            require(self.head == BASE, "initial_head_must_equal_pinned_base")
        else:
            require(self.head != BASE, "final_candidate_commit_required")
        self.initial_status = self.git("status", "--porcelain=v1", "--untracked-files=all")
        self.validate_tree(args.phase == "baseline")
        self.hashes = {name: sha(REPO / name) for name in AUTHORED + UNCHANGED}
        for name in UNCHANGED:
            original = self.run(["git", "show", BASE + ":" + name], REPO)[1]
            require(hashlib.sha256(original).hexdigest() == self.hashes[name], "read_only_source_changed:" + name)
        self.spec_sha = sha(REPO / AUTHORED[1])
        self.source_sha = sha(self.manifest_path)

    def run(self, argv, cwd, allowed=(0,)):
        require(isinstance(argv, list) and all(isinstance(x, str) for x in argv), "literal_argv_list_required")
        cwd = canonical_path(Path(cwd))
        require(cwd == REPO or self.out in cwd.parents or cwd == self.out, "child_cwd_outside_admission")
        require(argv[0] in ("git", self.python, self.scan_python, self.runtime["detect_secrets"]), "unadmitted_child_executable")
        if argv[0] == "git":
            require(argv[1] in ("rev-parse", "merge-base", "status", "diff", "ls-files", "show", "clone", "checkout"), "unadmitted_git_operation")
        else:
            require(not any(arg in ("-c", "-e") for arg in argv), "inline_interpreter_forbidden")
        index = len(self.calls)
        process = subprocess.run(argv, cwd=cwd, shell=False, capture_output=True, timeout=180)
        stdout = self.out / (str(index).zfill(3) + ".stdout")
        stderr = self.out / (str(index).zfill(3) + ".stderr")
        stdout.write_bytes(process.stdout)
        stderr.write_bytes(process.stderr)
        entry = {"argv": argv, "cwd": str(cwd), "exit": process.returncode,
                 "interpreter": argv[0], "stdout": stdout.name, "stderr": stderr.name,
                 "stdout_sha256": sha(stdout), "stderr_sha256": sha(stderr)}
        self.calls.append(entry)
        require(process.returncode in allowed, "child_failed:" + json.dumps(entry))
        return process.returncode, process.stdout, process.stderr

    def git(self, *args, cwd=None):
        return self.run(["git", *args], REPO if cwd is None else cwd)[1].decode()

    def validate_tree(self, baseline=False):
        require(not self.git("diff", "--name-only"), "tracked_worktree_dirty")
        require(not self.git("diff", "--cached", "--name-only"), "index_dirty")
        changed = set(self.git("diff", "--name-only", BASE, self.head).splitlines())
        require(changed == (set() if baseline else set(AUTHORED)), "exclusive_write_set_mismatch")
        visible = set(self.git("ls-files", "--others", "--exclude-standard").splitlines())
        require(visible <= ({".venv", *AUTHORED} if baseline else {".venv"}), "unexpected_scan_visible_untracked:" + repr(visible))
        if not baseline:
            for name in AUTHORED:
                require(self.git("ls-files", "--error-unmatch", name).strip() == name, "authored_file_uncommitted")
        self.details["path_inventory"] = self.git("ls-files", "--cached", "--others", "--exclude-standard", "-z").split("\0")[:-1]

    def child_suite(self, mode, foreign=False):
        folder = self.out / (mode + "-child")
        folder.mkdir(mode=0o700)
        report = folder / "report.json"
        cwd = folder if foreign else REPO
        argv = [self.python, str(REPO / AUTHORED[0]), "--temp-root", str(folder), "--report", str(report), "--mode", mode]
        self.run(argv, cwd)
        data = json.loads(report.read_text())
        require(data["tests_run"] == len(data["test_ids"]) and not data["skips"] and not data["failures"]
                and not data["errors"] and data["ambient_preserved"], "test_report_invalid")
        require(data["fixture_sha256"] == self.spec_sha, "child_fixture_identity_mismatch")
        self.details[mode] = {"report": str(report.relative_to(self.out)), "tests_run": data["tests_run"],
                              "test_ids": data["test_ids"], "skips": 0, "failures": 0, "errors": 0}
        if mode != "compatibility":
            observations = data["observations"]
            baselines = [x for x in observations if x["kind"] == "unmodified_baseline"]
            require({x["mode"] for x in baselines} == {"fresh", "existing"}, "baseline_modes_missing")
            if mode != "baseline":
                expected = {(m, t, n) for m in ("fresh", "existing") for t in json.loads((REPO / AUTHORED[1]).read_text())["insert_sites"]
                            for n in ((1, 2) if t in ("wp6_input_originals", "detector_hits", "wp6_hit_identities", "wp6_run_hits") else (1,))}
                actual = {(x["mode"], x["table"], x["ordinal"]) for x in observations if x["kind"] == "prefix"}
                require(actual == expected and len(actual) == 20 and data["tests_run"] == 25, "frozen_generated_case_coverage")
                controls = [x for x in observations if x["kind"] == "injected_control"]
                require(len(controls) == 1 and "exact_state_and_transaction_closed" in controls[0]["deciding_failure"], "negative_control_missing")
                require(len([x for x in observations if x["kind"] == "restored_positive"]) == 1, "restored_positive_missing")
                self.details[mode]["generated_prefix_cases"] = len(actual)
                self.details[mode]["negative_controls"] = len(controls)
                for kind in ("unmodified_baseline", "injected_control", "restored_positive"):
                    (folder / (kind + ".json")).write_text(json.dumps([x for x in observations if x["kind"] == kind], indent=2) + "\n")

    def portable(self):
        literals = []
        for name in AUTHORED:
            text = (REPO / name).read_text()
            # Reject host-specific roots without embedding such a literal in authored sources.
            for prefix in ("/" + "home/", "/" + "Users/", "/" + "workspace/", "/" + "tmp/", "/" + "var/tmp/"):
                if prefix in text:
                    literals.append((name, prefix))
        require(not literals, "host_only_literals:" + repr(literals))
        self.details["host_only_literals"] = literals
        self.child_suite("suite", foreign=True)
        self.child_suite("discovery", foreign=True)
        require(self.details["suite"]["test_ids"] == self.details["discovery"]["test_ids"], "portable_discovery_coverage_mismatch")

    def credentials(self):
        require(Path(self.runtime["detect_secrets"]).parent == Path(self.scan_python).parent, "scanner_runtime_mismatch")
        status, output, _ = self.run([self.runtime["detect_secrets"], "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"], REPO)
        report = self.out / "secret-report.json"
        report.write_bytes(output)
        data = json.loads(output)
        require(isinstance(data["results"], dict), "credential_scan_json_invalid")
        self.run([self.scan_python, str(REPO / "tools/check_secret_report.py"), str(report)], REPO)
        self.details["credential_scan"] = {"scan_exit": status, "checker_exit": 0, "report": report.name,
                                           "reported_hits": sum(len(x) for x in data["results"].values())}

    def public_tree(self):
        clone = self.out / "committed-tree"
        self.run(["git", "clone", "--local", "--no-hardlinks", "--no-checkout", "--no-recurse-submodules", str(REPO), str(clone)], self.out)
        self.run(["git", "checkout", "--detach", self.head], clone)
        require(self.git("rev-parse", "HEAD", cwd=clone).strip() == self.head, "clone_head_mismatch")
        require(self.git("rev-parse", "HEAD^{tree}", cwd=clone).strip() == self.tree, "clone_tree_mismatch")
        require(not (clone / ".venv").exists() and not (clone / ".venv").is_symlink(), "clone_runtime_link_forbidden")
        for name, digest in self.hashes.items():
            require(sha(clone / name) == digest, "clone_source_hash_mismatch:" + name)
        _, output, _ = self.run([self.python, str(clone / "tools/check_public_tree.py")], clone)
        clean = json.loads(output)
        require(clean["findings"] == [] and clean["scanned_files"] > 0, "committed_tree_findings_or_incomplete")
        status, output, _ = self.run([self.python, str(REPO / "tools/check_public_tree.py")], REPO, allowed=(0, 1))
        info = json.loads(output)
        require(info["scanned_files"] > 0 and ((status == 0 and info["findings"] == [])
                or (status == 1 and info["findings"] == [[".venv", 0, "symlink_not_scanned"]])), "in_place_unexpected_findings")
        self.details["committed_tree"] = {"head": self.head, "tree": self.tree, "exit": 0, "result": clean}
        self.details["in_place_informational"] = {"incident": "INC-174", "exit": status, "result": info}

    def finish(self, error=None):
        if error is None:
            require(self.git("rev-parse", "HEAD").strip() == self.head and self.git("rev-parse", "HEAD^{tree}").strip() == self.tree, "source_identity_changed")
            require(self.git("status", "--porcelain=v1", "--untracked-files=all") == self.initial_status, "source_status_changed")
            require(all(sha(REPO / name) == digest for name, digest in self.hashes.items()), "source_bytes_changed")
            require(sha(self.manifest_path) == self.source_sha, "source_manifest_changed")
        receipt = {"schema": "fc10.acceptance-row/v1", "phase": self.args.phase,
                   "invocation": {"argv": [sys.executable, *sys.argv], "cwd": str(Path.cwd()), "interpreter": sys.executable},
                   "source_manifest": str(self.manifest_path), "source_manifest_sha256": getattr(self, "source_sha", None),
                   "head": self.head, "tree": self.tree, "base": BASE,
                   "authored_and_unchanged_hashes": self.hashes, "fixture_spec_sha256": self.spec_sha,
                   "exit": int(error is not None), "error": error, "calls": self.calls, "details": self.details,
                   "author_runtime": {"model": "gpt-6.1-sol", "provider": "openai-codex", "profile": "sol-reviewer", "card": "t_8e497b27", "run": 1848},
                   "limitations": LIMITS}
        (self.out / "receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
        # All children have exited. Freeze every produced regular file, including local clone bytes.
        files = {}
        for path in sorted(self.out.rglob("*")):
            require(not path.is_symlink(), "evidence_symlink_refused")
            if path.is_file():
                relative = path.relative_to(self.out).as_posix()
                require(relative == str(Path(relative)) and ".." not in Path(relative).parts and relative not in files, "noncanonical_evidence_path")
                files[relative] = {"sha256": sha(path), "bytes": path.stat().st_size}
        evidence = {"schema": "fc10.evidence/v1", "head": self.head, "tree": self.tree, "files": files,
                    "manifest_self_excluded": "evidence-manifest.json"}
        manifest = self.out / "evidence-manifest.json"
        manifest.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
        stored = json.loads(manifest.read_text())
        actual = {path.relative_to(self.out).as_posix() for path in self.out.rglob("*") if path.is_file() and path != manifest}
        require(actual == set(stored["files"]), "omitted_evidence_output")
        for name, entry in stored["files"].items():
            require(sha(self.out / name) == entry["sha256"] and (self.out / name).stat().st_size == entry["bytes"], "frozen_evidence_hash_invalid")
        print(json.dumps({"phase": self.args.phase, "exit": receipt["exit"], "head": self.head, "tree": self.tree,
                          "receipt": str(self.out / "receipt.json"), "manifest": str(manifest), "manifest_sha256": sha(manifest),
                          "evidence_files": len(files), "details": {key: value for key, value in self.details.items() if key != "path_inventory"}, "error": error}, sort_keys=True))
        return receipt["exit"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("baseline", "suite", "compatibility", "portable", "credentials", "public-tree"), required=True)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--out-base", type=Path, required=True)
    args = parser.parse_args()
    # Product modules are imported only by the guarded child. Parent never imports them.
    def no_network(*args, **kwargs):
        raise RuntimeError("offline acceptance boundary: network forbidden")
    with ExitStack() as stack:
        for name in ("socket", "create_connection", "getaddrinfo"):
            stack.enter_context(mock.patch.object(socket, name, no_network))
        row = Row(args)
        try:
            if args.phase in ("suite", "baseline", "compatibility"):
                row.child_suite(args.phase)
                if args.phase == "baseline":
                    row.child_suite("suite")
            elif args.phase == "portable":
                row.portable()
            elif args.phase == "credentials":
                row.credentials()
            else:
                row.public_tree()
        except Exception:
            return row.finish(traceback.format_exc())
        return row.finish()


if __name__ == "__main__":
    raise SystemExit(main())
