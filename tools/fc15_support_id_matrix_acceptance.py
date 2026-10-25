"""Bounded five-row offline FC15 recorder. Machine-valid means REVIEW REQUIRED.

Actual author: gpt-6.1-sol / openai-codex, sol-reviewer. No copied probes.
The existing shipping operator owns full discovery, transport and exact-head CI.
"""
import argparse
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import traceback
import unittest
from unittest.mock import patch
import uuid

REPO = Path(__file__).resolve().parents[1]
TEST = "tests/records/test_fc15_support_id_matrix.py"
FIXTURE = "tests/fixtures/fc15-support-id-matrix/cases.json"
RUNNER = "tools/fc15_support_id_matrix_acceptance.py"
AUTHORED = (TEST, FIXTURE, RUNNER)
BASE = "126accc8568009abe" + "c5bc0698e1c4ed3f" + "75ef36b"
BASE_TREE = "955eb92f356ca1ed" + "47a2335ef83ddc7b" + "a1dbe467"
PINS = {
    "campaign_tool/records/grounded_evidence.py":
        "43ace8a617ab8147" + "7e72ceb39633f1b6" + "c00dfa32213aba12" + "34367bd15fbd87f8",
    "campaign_tool/records/understanding/evidence.py":
        "215ed75899325c4a" + "c294cfaf890fb85d" + "ccabf179de240ed4" + "4f93d178eaeb11b4",
    "tests/records/test_understanding.py":
        "77fa084d17606632" + "7bc1795381a91167" + "1da7875f1f5721e1" + "64002616d6f5922a",
}
CHECKERS = ("tools/check_secret_report.py", "tools/check_public_tree.py",
            "campaign_tool/records/public_scan.py", "docs/records-gates-import.json",
            "docs/records-intake-import.json", "container/flock-ocr/pins.json")
PHASES = ("suite", "compatibility", "portable", "credentials", "public-tree")


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def no_symlink(path):
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError("symlink outside admitted runtime link: " + str(part))


def write_json(path, value):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True, indent=2)
        stream.write("\n")


def deny(*args, **kwargs):
    raise RuntimeError("FC15 recorder offline sentinel")


class Recorder:
    def __init__(self, args):
        self.args = args
        self.check = unittest.TestCase()
        self.manifest_path = args.source_manifest.absolute()
        no_symlink(self.manifest_path)
        self.manifest_raw = self.manifest_path.read_bytes()
        self.manifest = json.loads(self.manifest_raw)
        m = self.manifest
        self.check.assertEqual(m["schema"], "agentorange.source-manifest/v1")
        self.check.assertEqual(m["card"], "FC15-SUPPORT-ID-MATRIX")
        self.check.assertEqual(Path(m["repo"]), REPO)
        self.check.assertEqual(Path(m["write_root"]), REPO.parent)
        self.check.assertEqual(self.manifest_path, REPO.parent / "SOURCE-MANIFEST.json")
        self.check.assertEqual(m["head"], BASE)
        self.check.assertEqual(m["tree"], BASE_TREE)
        self.check.assertIs(m["clean"], True)
        self.check.assertEqual(args.out_base.absolute(), REPO.parent / "out")
        no_symlink(REPO)
        no_symlink(args.out_base.absolute())
        no_symlink(Path(m["runtime"]["repo_venv"]))
        self.check.assertTrue((REPO / ".venv").is_symlink())
        self.check.assertEqual((REPO / ".venv").resolve(), Path(m["runtime"]["repo_venv"]).resolve())
        expected_python = (Path(m["runtime"]["credential_scan_python3"]) if args.phase == "credentials"
                           else REPO / ".venv/bin/python3")
        # Preserve lexical venv identity, not only the common system-python target.
        self.check.assertEqual(Path(sys.executable).absolute(), expected_python.absolute())
        self.check.assertTrue(expected_python.is_file())
        self.check.assertTrue(Path(m["runtime"]["detect_secrets"]).is_file())
        self.check.assertEqual(Path(m["runtime"]["detect_secrets"]).parent, Path(m["runtime"]["credential_scan_python3"]).parent)
        args.out_base.mkdir(mode=0o700, exist_ok=True)
        self.row = args.out_base / (args.phase + "-" + str(uuid.uuid4()))
        self.row.mkdir(mode=0o700)
        self.temp = self.row / "temporary"
        self.temp.mkdir(mode=0o700)
        for key in ("TMPDIR", "TMP", "TEMP"):
            os.environ[key] = str(self.temp)
        tempfile.tempdir = str(self.temp)
        sys.dont_write_bytecode = True
        self.children = []
        self.receipt = {"schema": "fc15-support-id-matrix-acceptance/v1", "phase": args.phase,
                        "row": str(self.row), "argv": [sys.executable, *sys.argv],
                        "cwd": str(Path.cwd()), "interpreter": sys.executable,
                        "source_manifest_sha256": sha(self.manifest_raw),
                        "author": {"model": "gpt-6.1-sol", "provider": "openai-codex", "profile": "sol-reviewer"},
                        "children": self.children, "exit": 1, "review_required": True,
                        "full_discovery_and_exact_head_ci": "OWED to existing shipping operator"}
        self.check.assertEqual(Path.cwd(), REPO)

    def child(self, label, argv, cwd=REPO, expected=(0,)):
        self.check.assertTrue(all(isinstance(v, str) for v in argv))
        # No shell, interpreter one-liners, module-path overrides or network tools.
        self.check.assertFalse(any(v in ("-c", "-e") for v in argv))
        stdout_path = self.row / (label + ".stdout")
        stderr_path = self.row / (label + ".stderr")
        record = {"label": label, "argv": argv, "cwd": str(cwd), "shell": False,
                  "stdout": str(stdout_path), "stderr": str(stderr_path), "exit": None}
        self.children.append(record)
        with stdout_path.open("xb") as stdout, stderr_path.open("xb") as stderr:
            process = subprocess.run(argv, cwd=cwd, stdout=stdout, stderr=stderr,
                                     timeout=240, shell=False,
                                     env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1", GIT_TERMINAL_PROMPT="0"))
        record.update({"exit": process.returncode, "complete": True,
                       "stdout_sha256": sha(stdout_path.read_bytes()),
                       "stderr_sha256": sha(stderr_path.read_bytes())})
        self.check.assertIn(process.returncode, expected, label + ": " + stderr_path.read_text(errors="replace"))
        return stdout_path.read_bytes()

    def git(self, label, *args, cwd=REPO):
        return self.child(label, ["git", *args], cwd=cwd)

    def identity(self, prefix):
        head = self.git(prefix + "-head", "rev-parse", "HEAD").decode().strip()
        tree = self.git(prefix + "-tree", "rev-parse", "HEAD^{tree}").decode().strip()
        status = self.git(prefix + "-status", "status", "--porcelain=v1", "--untracked-files=all").decode()
        # Scan-visible inventory, including ignored-but-tracked paths and runtime link.
        inventory = self.git(prefix + "-inventory", "ls-files", "-z", "--cached", "--others", "--exclude-standard").decode().split("\0")
        inventory = sorted(set(inventory) - {""})
        authored_hashes = {}
        for name in AUTHORED:
            path = REPO / name
            no_symlink(path)
            authored_hashes[name] = sha(path.read_bytes())
        unchanged = {}
        for name in (*PINS, *CHECKERS):
            raw = (REPO / name).read_bytes()
            original = self.git(prefix + "-base-" + str(len(unchanged)), "show", BASE + ":" + name)
            self.check.assertEqual(raw, original, "unchanged source/checker " + name)
            if name in PINS:
                self.check.assertEqual(sha(raw), PINS[name])
            unchanged[name] = sha(raw)
        baseline = head == BASE
        if baseline:
            self.check.assertEqual(tree, BASE_TREE)
            self.check.assertEqual(self.args.phase, "compatibility", "only baseline compatibility can precede the commit")
            for line in status.splitlines():
                self.check.assertEqual(line[:3], "?? ")
                self.check.assertIn(line[3:], (*AUTHORED, ".venv"))
        else:
            self.git(prefix + "-ancestor", "merge-base", "--is-ancestor", BASE, head)
            changed = self.git(prefix + "-changed-paths", "diff", "--name-only", BASE, head).decode().splitlines()
            self.check.assertEqual(set(changed), set(AUTHORED))
            self.check.assertEqual(len(changed), len(AUTHORED))
            self.check.assertIn(status, ("", "?? .venv\n"))
            self.git(prefix + "-tracked-head-equal", "diff", "--exit-code", "HEAD", "--")
            self.git(prefix + "-index-head-equal", "diff", "--cached", "--exit-code", "HEAD", "--")
            tracked = set(self.git(prefix + "-tracked", "ls-files", "-z").decode().split("\0")) - {""}
            self.check.assertLessEqual(set(inventory) - tracked, {".venv"})
            for n, name in enumerate(AUTHORED):
                committed = self.git(prefix + "-authored-" + str(n), "show", head + ":" + name)
                self.check.assertEqual(sha(committed), authored_hashes[name])
        self.check.assertEqual(self.git(prefix + "-base-tree", "rev-parse", BASE + "^{tree}").decode().strip(), BASE_TREE)
        diff = self.git(prefix + "-complete-diff", "diff", "--binary", BASE, head, "--")
        return {"head": head, "tree": tree, "status": status, "baseline": baseline,
                "path_inventory": inventory, "authored_sha256": authored_hashes,
                "unchanged_source_scanner_allowlist_sha256": unchanged,
                "complete_diff_sha256": sha(diff),
                "source_manifest_sha256": sha(self.manifest_path.read_bytes()),
                "runtime_link": os.readlink(REPO / ".venv")}

    def tests(self, label, mode, cwd):
        report = self.row / (label + ".json")
        temporary = self.row / (label + "-tmp")
        temporary.mkdir(mode=0o700)
        self.child(label, [sys.executable, "-B", str(REPO / TEST), "--mode", mode,
                           "--temp-root", str(temporary), "--report", str(report)], cwd=cwd)
        data = json.loads(report.read_bytes())
        self.check.assertIs(data["success"], True)
        self.check.assertIs(data["ambient_unchanged"], True)
        self.check.assertEqual(data["skips"], 0)
        self.check.assertEqual(data["errors"], 0)
        self.check.assertEqual(data["failures"], 0)
        self.check.assertEqual(data["fixture_sha256"], sha((REPO / FIXTURE).read_bytes()))
        self.receipt.setdefault("test_reports", {})[label] = {"path": str(report), "sha256": sha(report.read_bytes()), **data}
        return data

    def suite(self):
        self.tests("matrix", "matrix", REPO)

    def compatibility(self):
        result = self.tests("compatibility", "compatibility", REPO)
        self.check.assertEqual(result["tests_run"], 2)
        self.check.assertEqual(result["subcases"], [])
        self.check.assertEqual(set(result["test_ids"]), {
            "tests.records.test_understanding.GroundedEvidenceTests.test_json_pointer_support_is_exact",
            "tests.records.test_understanding.EvidenceTests.test_quote_citation_and_claim_round_trip"})
        if not self.receipt["before"]["baseline"]:
            baseline = []
            for path in sorted(self.args.out_base.glob("compatibility-*/receipt.json")):
                entry = json.loads(path.read_bytes())
                if (entry.get("exit") == 0 and entry.get("before", {}).get("baseline") is True
                        and entry.get("source_manifest_sha256") == sha(self.manifest_raw)):
                    baseline.append((path, entry))
            self.check.assertGreaterEqual(len(baseline), 1, "actual baseline compatibility evidence required")
            path, entry = baseline[0]
            old = entry["test_reports"]["compatibility"]
            for field in ("test_ids", "tests_run", "skips", "failures", "errors", "success"):
                self.check.assertEqual(old[field], result[field])
            for child in entry["children"]:
                self.check.assertIs(child["complete"], True)
                for channel in ("stdout", "stderr"):
                    self.check.assertEqual(sha(Path(child[channel]).read_bytes()), child[channel + "_sha256"])
            self.check.assertEqual(sha(Path(old["path"]).read_bytes()), old["sha256"])
            self.receipt["baseline_compatibility"] = {"path": str(path), "sha256": sha(path.read_bytes()),
                                                       "measured_behavior_delta": "none; exact same two test IDs pass with no skips"}

    def portable(self):
        unrelated = self.row / "unrelated-cwd"
        unrelated.mkdir(mode=0o700)
        self.tests("absolute-script", "matrix", unrelated)
        self.tests("discovery-with-peer-sentinels", "discovery", unrelated)
        findings = []
        for name in AUTHORED:
            text = (REPO / name).read_text(encoding="utf-8")
            for forbidden in ("/" + "home/", "/" + "Users/", "/" + "workspace/", "PYTHON" + "PATH", "LD_" + "LIBRARY_PATH"):
                if forbidden in text:
                    findings.append([name, forbidden])
        self.check.assertEqual(findings, [], "portable source path inspection")
        self.receipt["box_specific_path_findings"] = findings

    def credentials(self):
        runtime = self.manifest["runtime"]
        self.check.assertEqual(sys.executable, runtime["credential_scan_python3"])
        self.child("detect-secrets", [runtime["detect_secrets"], "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"])
        report = self.row / "detect-secrets.stdout"
        scanner = json.loads(report.read_bytes())
        self.check.assertIsInstance(scanner["results"], dict)
        self.child("check-secret-report", [runtime["credential_scan_python3"], str(REPO / "tools/check_secret_report.py"), str(report)])
        self.receipt["scanner_result_paths"] = sorted(scanner["results"])
        self.receipt["scanner_report_sha256"] = sha(report.read_bytes())

    def public_tree(self):
        identity = self.receipt["before"]
        clone = self.row / "committed-tree"
        self.git("local-clone", "clone", "--local", "--no-hardlinks", "--no-checkout", "--no-recurse-submodules", str(REPO), str(clone))
        self.git("local-checkout", "checkout", "--detach", identity["head"], cwd=clone)
        self.check.assertEqual(self.git("clone-head", "rev-parse", "HEAD", cwd=clone).decode().strip(), identity["head"])
        self.check.assertEqual(self.git("clone-tree", "rev-parse", "HEAD^{tree}", cwd=clone).decode().strip(), identity["tree"])
        self.check.assertEqual(self.git("clone-status", "status", "--porcelain=v1", "--untracked-files=all", cwd=clone), b"")
        self.check.assertFalse((clone / ".venv").exists())
        for name, expected in identity["authored_sha256"].items():
            self.check.assertEqual(sha((clone / name).read_bytes()), expected)
        data = json.loads(self.child("committed-public-tree", [sys.executable, "-B", str(clone / "tools/check_public_tree.py")], cwd=clone))
        self.check.assertEqual(data["findings"], [])
        self.check.assertGreater(data["scanned_files"], 0)
        self.check.assertNotIn("error", data)
        self.receipt["committed_tree_scan"] = data
        info = json.loads(self.child("in_place_informational", [sys.executable, "-B", str(REPO / "tools/check_public_tree.py")], expected=(0, 1)))
        exit_code = self.children[-1]["exit"]
        self.check.assertIn(info["findings"], ([], [[".venv", 0, "symlink_not_scanned"]]))
        self.check.assertEqual(exit_code, int(bool(info["findings"])))
        self.check.assertGreater(info["scanned_files"], 0)
        self.check.assertNotIn("error", info)
        self.receipt["in_place_informational"] = {"inc": "INC-174", "exit": exit_code, **info}

    def run(self):
        try:
            with ExitStack() as stack:
                for target in ("socket.socket", "socket.create_connection", "socket.getaddrinfo", "sqlite3.connect"):
                    stack.enter_context(patch(target, side_effect=deny))
                self.receipt["before"] = self.identity("before")
                self.check.assertEqual(self.receipt["before"]["source_manifest_sha256"], sha(self.manifest_raw))
                getattr(self, self.args.phase.replace("-", "_"))()
                self.receipt["after"] = self.identity("after")
                self.check.assertEqual(self.receipt["after"], self.receipt["before"], "candidate changed during row")
                # All subprocess.run calls have returned and their file writers closed.
                for child in self.children:
                    self.check.assertIs(child["complete"], True)
                    for channel in ("stdout", "stderr"):
                        self.check.assertEqual(sha(Path(child[channel]).read_bytes()), child[channel + "_sha256"])
                self.receipt["exit"] = 0
        except Exception:
            self.receipt["error"] = traceback.format_exc()
            print(self.receipt["error"], file=sys.stderr)
        # Do not include this receipt in its own digest. Nothing writes after it.
        retained = {}
        for path in self.row.rglob("*"):
            # Retained clone is identified by git tree, not a traversal of .git or links.
            if path.is_file() and not path.is_symlink() and "committed-tree" not in path.relative_to(self.row).parts:
                retained[str(path.relative_to(self.row))] = sha(path.read_bytes())
        self.receipt["retained_file_sha256"] = retained
        write_json(self.row / "receipt.json", self.receipt)
        print(json.dumps({"phase": self.args.phase, "exit": self.receipt["exit"], "row": str(self.row),
                          "head": self.receipt.get("before", {}).get("head"),
                          "receipt_sha256": sha((self.row / "receipt.json").read_bytes())}, sort_keys=True))
        return self.receipt["exit"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=PHASES, required=True)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--out-base", type=Path, required=True)
    args = parser.parse_args()
    return Recorder(args).run()


if __name__ == "__main__":
    raise SystemExit(main())
