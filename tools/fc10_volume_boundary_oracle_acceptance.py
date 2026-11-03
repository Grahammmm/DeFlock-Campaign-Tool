"""Offline FC10 volume acceptance, one fresh UUID receipt per invocation.

No imports from product or tests occur in this coordinator. The test-script
children install scoped offline patches and candidate-local temporary defaults
before loading product code. Scanner call graphs were inspected: public scan
uses only git ls-files and file reads; secrets scan uses --no-verify and the
unchanged report checker reads only its report and pinned public provenance.
Local git transport is used solely for the exact committed-tree scan, not a
remote push/publication. All subprocesses are literal argv lists, shell=False.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import uuid

AUTHORED = (
    "tests/records/test_fc10_volume_boundary_oracle.py",
    "tests/fixtures/fc10-volume-boundary-oracle/cases.json",
    "tools/fc10_volume_boundary_oracle_acceptance.py",
)
UNCHANGED = (
    "campaign_tool/records/detectors/core.py", "tests/records/test_detectors.py",
    "tools/check_public_tree.py", "campaign_tool/records/public_scan.py",
    "tools/check_secret_report.py", ".gitignore",
)
LIMITS = ["REVIEW REQUIRED; machine validity is not semantic/legal/human/live acceptance.",
          "Scoped compatibility is not full-repository CI; exact-head shipping CI remains OWED to operator.",
          "Public scan is patterns-only; private export clearance and manual privacy review remain owed.",
          "No remote push, PR, publication, email, live execution or hold change."]


def need(condition, reason):
    if not condition:
        raise ValueError(reason)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dump(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def canonical_path(path):
    need(path.is_absolute() and path == path.resolve(), "noncanonical_or_symlink_path:" + str(path))
    need(all(not p.is_symlink() for p in (path, *path.parents)), "symlink_ancestor:" + str(path))
    return path


class Row:
    def __init__(self, args):
        self.repo = canonical_path(Path(__file__).absolute().parents[1])
        self.manifest_path = canonical_path(Path(args.source_manifest))
        self.source = json.loads(self.manifest_path.read_text())
        s = self.source
        need(s["schema"] == "agentorange.source-manifest/v1", "source_schema")
        need(s["repo"] == str(self.repo), "source_repo")
        need(s["write_root"] == str(self.repo.parent), "source_write_root")
        need(self.manifest_path.parent == self.repo.parent, "source_manifest_location")
        need(re.fullmatch("[a-f0-9]{40}", s["head"]) and re.fullmatch("[a-f0-9]{40}", s["tree"]), "source_identity_format")
        self.out_base = canonical_path(Path(args.out_base))
        need(self.out_base == self.repo.parent / "out", "out_base_location")
        self.out_base.mkdir(exist_ok=True)
        self.root = self.out_base / (args.phase + "-" + uuid.uuid4().hex)
        self.root.mkdir(mode=0o700)
        (self.root / "tmp").mkdir(mode=0o700)
        tempfile.tempdir = str(self.root / "tmp")
        sys.dont_write_bytecode = True
        self.phase = args.phase
        self.commands = []
        self.data = {"schema": "fc10-volume-acceptance/v1", "phase": self.phase,
                     "invocation": self.root.name, "cwd": str(Path.cwd()),
                     "argv": list(sys.argv), "interpreter": sys.executable,
                     "source_manifest": str(self.manifest_path), "source_manifest_sha256": sha(self.manifest_path),
                     "author_runtime": {"model": "gpt-6.1-sol", "provider": "openai-codex", "profile": "sol-reviewer", "run_id": 1851, "basis": "Hermes session runtime and admitted card comment"},
                     "limits": LIMITS, "commands": self.commands}

    def run(self, name, argv, cwd=None, expected=(0,)):
        need(all(isinstance(a, str) and a for a in argv), "literal_argv_required")
        stdout, stderr = self.root / (name + ".stdout"), self.root / (name + ".stderr")
        need(not stdout.exists() and not stderr.exists(), "receipt_overwrite")
        cwd = self.repo if cwd is None else cwd
        with stdout.open("wb") as out, stderr.open("wb") as err:
            child = subprocess.run(argv, cwd=cwd, stdout=out, stderr=err, shell=False, timeout=180)
        record = {"name": name, "argv": argv, "cwd": str(cwd), "exit": child.returncode,
                  "stdout": stdout.name, "stdout_sha256": sha(stdout),
                  "stderr": stderr.name, "stderr_sha256": sha(stderr)}
        self.commands.append(record)
        need(child.returncode in expected, "child_failed:" + name + ":" + str(child.returncode))
        return stdout.read_text()

    def git(self, name, tail, cwd=None):
        return self.run(name, ["git", *tail], cwd=cwd).strip()

    def preflight(self):
        s = self.source
        head = self.git("head", ["rev-parse", "HEAD"])
        tree = self.git("tree", ["rev-parse", "HEAD^{tree}"])
        self.data.update(head=head, tree=tree)
        need(self.git("base_tree", ["rev-parse", s["head"] + "^{tree}"]) == s["tree"], "base_tree_mismatch")
        self.git("ancestry", ["merge-base", "--is-ancestor", s["head"], head])
        need(self.git("origin", ["remote", "get-url", "origin"]) == s["remote"], "source_remote_mismatch")
        runtime = s["runtime"]
        need((self.repo / ".venv").is_symlink(), "missing_standard_venv")
        need(str((self.repo / ".venv").resolve()) == runtime["repo_venv"], "runtime_link_mismatch")
        interpreter = runtime["credential_scan_python3"] if self.phase == "credentials" else str(self.repo / ".venv/bin/python3")
        need(sys.executable == interpreter, "literal_interpreter_mismatch")
        need(Path(interpreter).is_file(), "interpreter_missing")
        self.python = str(self.repo / ".venv/bin/python3")
        diff = self.git("base_diff_names", ["diff", "--name-only", s["head"], "HEAD"]).splitlines()
        need(set(diff) <= set(AUTHORED), "write_set_exceeded")
        self.data["unchanged_hashes"] = {}
        for i, relative in enumerate(UNCHANGED):
            self.run("source_" + str(i), ["git", "show", s["head"] + ":" + relative])
            # Raw stdout is authoritative: read_text must not normalize byte hashes.
            raw = (self.root / ("source_" + str(i) + ".stdout")).read_bytes()
            need(hashlib.sha256(raw).hexdigest() == sha(self.repo / relative), "read_only_source_changed:" + relative)
            self.data["unchanged_hashes"][relative] = sha(self.repo / relative)
        self.data["authored_hashes"] = {name: sha(self.repo / name) for name in AUTHORED}
        self.data["fixture_sha256"] = sha(self.repo / AUTHORED[1])
        self.data["spec_sha256"] = sha(self.repo / AUTHORED[2])
        self.data["status_before"] = self.git("status_before", ["status", "--porcelain=v1", "--untracked-files=all"])
        self.data["scan_inventory"] = self.git("scan_inventory", ["ls-files", "--cached", "--others", "--exclude-standard"]).splitlines()
        for relative in self.data["scan_inventory"]:
            path = self.repo / relative
            need(not path.is_symlink() or (relative == ".venv" and str(path.resolve()) == runtime["repo_venv"]), "unexpected_scanner_visible_symlink:" + relative)

    def tests(self, label, mode, cwd=None):
        temp = self.root / (label + "-temp")
        temp.mkdir(mode=0o700)
        report = self.root / (label + ".json")
        self.run(label, [self.python, "-B", str(self.repo / AUTHORED[0]), "--temp-root", str(temp),
                         "--report", str(report), "--mode", mode], cwd=cwd)
        value = json.loads(report.read_text())
        need(value["tests_run"] == len(value["test_ids"]) and value["tests_run"] > 0, "test_count")
        need(not value["failures"] and not value["errors"] and not value["skips"], "required_tests_not_green")
        need(value["ambient_environment_unchanged"] and value["temp_default_unchanged"], "ambient_contamination")
        if mode != "compatibility":
            fixture = json.loads((self.repo / AUTHORED[1]).read_text())
            expected_cases = sorted(c["id"] for c in fixture["cases"])
            observed_cases = sorted(o["case"] for o in value["observations"] if o["phase"] == "case")
            need(len(expected_cases) == len(set(expected_cases)) and observed_cases == expected_cases, "frozen_case_coverage")
            need(value["dimensions"] == sorted({c["dimension"] for c in fixture["cases"]}), "dimension_coverage")
            controls = [o for o in value["observations"] if o["phase"] == "synthetic_end_inclusive"]
            need(len(controls) == 1 and "boundary_hit_count:edge_1700" in controls[0]["assertion"], "missing_killed_control")
            need(controls[0]["baseline"] == controls[0]["restored"], "control_not_restored")
        return {"report": report.name, "report_sha256": sha(report), "tests_run": value["tests_run"],
                "case_count": value["case_count"], "dimensions": value["dimensions"], "skips": 0}

    def suite(self):
        self.data["suite"] = self.tests("suite", "suite")
        self.data["baseline_note"] = "Test-only: real pinned product is unchanged; report preserves ordinary baseline, synthetic injected failure and restored observations separately. No product regression repair is admitted."

    def compatibility(self):
        self.data["compatibility"] = self.tests("compatibility", "compatibility")
        self.data["compatibility"]["selector"] = "tests.records.test_detectors"

    def portable(self):
        findings = []
        for name in AUTHORED:
            for number, line in enumerate((self.repo / name).read_text().splitlines(), 1):
                if re.search(r"/(?:home|Users|workspace|tmp)/|[A-Za-z]:\\", line):
                    findings.append({"path": name, "line": number})
        dump(self.root / "host_literals.json", findings)
        need(not findings, "host_only_literals")
        cwd = self.root / "unrelated-cwd"
        cwd.mkdir(mode=0o700)
        self.data["portable"] = self.tests("portable", "suite", cwd=cwd)
        self.data["discovery"] = self.tests("discovery", "discovery", cwd=cwd)

    def credentials(self):
        runtime = self.source["runtime"]
        scanner = Path(runtime["detect_secrets"])
        need(scanner.is_file() and scanner.parent == Path(runtime["credential_scan_python3"]).parent, "scanner_runtime_mismatch")
        self.data["scanner_sha256"] = sha(scanner)
        raw = self.run("detect_secrets", [runtime["credential_scan_python3"], "-B", str(scanner), "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"])
        report = self.root / "detect_secrets.stdout"
        parsed = json.loads(raw)
        need(isinstance(parsed.get("results"), dict), "secret_scan_incomplete")
        self.run("check_secret_report", [runtime["credential_scan_python3"], "-B", str(self.repo / "tools/check_secret_report.py"), str(report)])
        self.data["credentials"] = {"report": report.name, "files_with_hits": len(parsed["results"]), "scanner_and_checker_exit": 0}

    def public_tree(self):
        need(not self.data["status_before"], "candidate_not_clean")
        names = self.git("authored_committed", ["ls-files", "--", *AUTHORED]).splitlines()
        need(set(names) == set(AUTHORED), "authored_files_not_committed")
        tracked = self.git("tracked_names", ["ls-files"]).splitlines()
        visible = set(self.data["scan_inventory"])
        need(visible - set(tracked) <= {".venv"}, "scan_visible_untracked")
        self.git("index_clean", ["diff", "--exit-code", "--cached", "HEAD"])
        self.git("tracked_clean", ["diff", "--exit-code", "HEAD"])
        clone = self.root / "committed-tree"
        self.run("clone", ["git", "clone", "--local", "--no-hardlinks", "--no-checkout", "--no-recurse-submodules", str(self.repo), str(clone)])
        self.run("checkout", ["git", "checkout", "--detach", self.data["head"]], cwd=clone)
        need(self.git("clone_head", ["rev-parse", "HEAD"], cwd=clone) == self.data["head"], "clone_head")
        need(self.git("clone_tree", ["rev-parse", "HEAD^{tree}"], cwd=clone) == self.data["tree"], "clone_tree")
        need(not (clone / ".venv").exists(), "clone_runtime_link_forbidden")
        for relative, digest in {**self.data["authored_hashes"], **self.data["unchanged_hashes"]}.items():
            need(sha(clone / relative) == digest, "clone_hash_mismatch:" + relative)
        output = self.run("public_committed_tree", [self.python, "-B", str(clone / "tools/check_public_tree.py")], cwd=clone)
        result = json.loads(output)
        need(result.get("findings") == [] and result.get("scanned_files") == len(tracked), "public_committed_scan_incomplete_or_findings")
        informational = json.loads(self.run("in_place_informational", [self.python, "-B", str(self.repo / "tools/check_public_tree.py")], expected=(0, 1)))
        status = self.commands[-1]["exit"]
        need((status == 0 and informational.get("findings") == []) or
             (status == 1 and informational.get("findings") == [[".venv", 0, "symlink_not_scanned"]]), "in_place_unexpected_finding")
        need(informational.get("scanned_files") == len(tracked), "in_place_scan_incomplete")
        self.data["public_tree"] = {"scanned_files": result["scanned_files"], "findings": [], "clone": str(clone),
                                    "in_place_informational": {"incident": "INC-174", "exit": status, "findings": informational["findings"]}}
        self.data["retained_clone_files"] = sorted(p.relative_to(clone).as_posix() for p in clone.rglob("*") if p.is_file() and ".git" not in p.relative_to(clone).parts)
        need(self.data["retained_clone_files"] == sorted(tracked), "clone_inventory_incomplete")

    def finish(self, error=None):
        if "head" in self.data:
            after_head = self.git("head_after", ["rev-parse", "HEAD"])
            after_tree = self.git("tree_after", ["rev-parse", "HEAD^{tree}"])
            after_status = self.git("status_after", ["status", "--porcelain=v1", "--untracked-files=all"])
            need(after_head == self.data["head"] and after_tree == self.data["tree"], "source_identity_changed")
            need(after_status == self.data["status_before"], "source_status_changed")
            need({name: sha(self.repo / name) for name in AUTHORED} == self.data["authored_hashes"], "authored_bytes_changed")
            self.data["status_after"] = after_status
        self.data.update(exit=1 if error else 0, error=error, children_finished=True)
        # All retained files, including disposable clone administration, are
        # inventoried after children finish. Only the receipt itself is outside
        # its own inventory; the caller records its separate file digest.
        paths = sorted(p for p in self.root.rglob("*") if p.is_file())
        inventory = [{"path": p.relative_to(self.root).as_posix(), "sha256": sha(p), "bytes": p.stat().st_size} for p in paths]
        self.data["files"] = inventory
        self.data["inventory_exclusion"] = "receipt.json itself only (separately hashed by caller); no retained child output omitted"
        seal = self.root / "receipt.json"
        dump(seal, self.data)
        verify = json.loads(seal.read_text())
        rels = [f["path"] for f in verify["files"]]
        need(len(rels) == len(set(rels)), "duplicate_manifest_path")
        for entry in verify["files"]:
            relative = Path(entry["path"])
            need(not relative.is_absolute() and ".." not in relative.parts and relative.as_posix() == entry["path"], "noncanonical_manifest_path")
            target = self.root / relative
            canonical_path(target)
            need(sha(target) == entry["sha256"] and target.stat().st_size == entry["bytes"], "receipt_hash_invalid")
        actual = {p.relative_to(self.root).as_posix() for p in self.root.rglob("*") if p.is_file() and p != seal}
        need(set(rels) == actual, "omitted_output")
        print(json.dumps({"phase": self.phase, "exit": self.data["exit"], "head": self.data.get("head"),
                          "receipt": str(seal), "receipt_sha256": sha(seal), "error": error,
                          "counts": {key: self.data[key] for key in ("suite", "compatibility", "portable", "discovery", "credentials", "public_tree") if key in self.data}}, sort_keys=True))
        return self.data["exit"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", required=True, choices=("suite", "compatibility", "portable", "credentials", "public-tree"))
    parser.add_argument("--source-manifest", required=True)
    parser.add_argument("--out-base", required=True)
    args = parser.parse_args()
    row = Row(args)
    error = None
    try:
        row.preflight()
        getattr(row, args.phase.replace("-", "_"))()
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        error = type(exc).__name__ + ":" + str(exc)
    return row.finish(error)


if __name__ == "__main__":
    raise SystemExit(main())
