#!/usr/bin/env python3
"""Small retained-evidence launcher for the offline FA1704 test-only scope."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import traceback
import uuid

sys.dont_write_bytecode = True
REPO = Path(__file__).resolve().parents[1]
NEW_PATHS = ("tests/records/test_fa1704_intake_limit_meet.py", "tools/accept_fa1704.py")
SOURCE = "campaign_tool/records/intake/folder.py"
MODULE = "tests.records.test_fa1704_intake_limit_meet"
METHODS = (
    "test_ordinary_lattice", "test_direct_inherited_helper_does_not_set",
    "test_defensive_sentinel_combinations", "test_tightening_each_finite_input_cannot_increase_result",
    "test_get_failure_propagates_without_set", "test_set_failure_propagates_once_without_retry_or_restore",
    "test_oracle_detects_faulty_setter_that_ignores_tighter_soft_limit",
    "test_scoped_guards_and_ordinary_loading_restore_peer_state",
)
BEHAVIOR = [MODULE + ".NativeIntakeLimitMeetTests." + name for name in METHODS]
COMPATIBILITY = [
    "tests.records.test_ocr_inherited_limits.OCRInheritedLimitsTests.test_memory_and_file_ceilings_are_independent",
    "tests.records.test_ocr_inherited_limits.OCRInheritedLimitsTests.test_memory_set_failure_propagates_without_fallback",
]


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def safe_output_root(value):
    path = Path(value)
    require(path.is_absolute() and ".." not in path.parts, "absolute canonical output root required")
    allowed = REPO.parent / "out"
    require(allowed == path or allowed in path.parents, "output must remain beneath candidate/out")
    # Check existing ancestry BEFORE creating anything. No symlink traversal.
    cursor = Path(path.anchor)
    for part in path.parts[1:]:
        cursor = cursor / part
        try:
            mode = cursor.lstat().st_mode
        except FileNotFoundError:
            cursor.mkdir(mode=0o700)
            mode = cursor.lstat().st_mode
        require(stat.S_ISDIR(mode) and not stat.S_ISLNK(mode), "unsafe output ancestor")
    return path


class Evidence:
    def __init__(self, child, case):
        self.child = child
        self.steps = []
        self.row = {"case": case, "launcher_argv": [sys.executable, *sys.argv],
                    "launcher_cwd": os.getcwd(), "interpreter": sys.executable,
                    "time_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "steps": self.steps, "exit": 1}
        self.env = dict(os.environ, TMPDIR=str(child), TMP=str(child), TEMP=str(child),
                        PYTHONDONTWRITEBYTECODE="1")

    def command(self, label, argv, cwd=REPO, *, fixture=None):
        require(all(isinstance(item, str) for item in argv), "argv must be flat strings")
        prefix = str(len(self.steps) + 1).zfill(2) + "-" + label
        env = dict(self.env)
        if fixture is not None:
            env["FA1704_FIXTURE_ROOT"] = str(fixture)
        proc = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, check=False)
        stdout, stderr = self.child / (prefix + ".stdout"), self.child / (prefix + ".stderr")
        stdout.write_bytes(proc.stdout)
        stderr.write_bytes(proc.stderr)
        row = {"label": label, "argv": argv, "cwd": str(cwd), "exit": proc.returncode,
               "stdout": str(stdout), "stderr": str(stderr),
               "stdout_sha256": digest(stdout), "stderr_sha256": digest(stderr),
               "temporary_root": str(self.child), "fixture_root": str(fixture) if fixture else None}
        self.steps.append(row)
        if "unittest" in argv:
            text = proc.stderr.decode("utf-8", errors="replace")
            counts = re.findall(r"(?m)^Ran (\d+) tests? in ", text)
            row["selected_test_ids"] = argv[5:]
            row["executed_count"] = int(counts[0]) if len(counts) == 1 else None
            for field in ("failures", "errors", "skipped", "expected failures", "unexpected successes"):
                match = re.search(re.escape(field) + r"=(\d+)", text)
                row[field.replace(" ", "_")] = int(match[1]) if match else 0
        require(proc.returncode == 0, "child failed: " + label)
        return proc.stdout.decode("utf-8"), proc.stderr.decode("utf-8"), row

    def git(self, *args, cwd=REPO):
        return self.command("git-" + args[0], ["git", *args], cwd)[0].strip()

    def tests(self, label, ids, expected):
        fixture = self.child / (label + "-fictional-fixtures")
        fixture.mkdir(mode=0o700)
        out, err, row = self.command(label, [sys.executable, "-B", "-m", "unittest", "-v", *ids], fixture=fixture)
        matches = re.findall(r"(?m)^Ran (\d+) tests? in ", err)
        require(len(matches) == 1, "missing or ambiguous real unittest count")
        count = int(matches[0])
        row.update(selected_test_ids=ids, executed_count=count, skips=row["skipped"])
        require(all(row[field] == 0 for field in ("failures", "errors", "skipped", "expected_failures", "unexpected_successes")),
                "unexpected test failure, error or skip")
        require(count == expected and count > 0, "incorrect executed test count")
        require(re.search(r"(?m)^OK\s*$", err) is not None, "missing clean unittest success")
        require(not re.search(r"(?m)^(FAILED|OK \()|\.\.\. skipped|unexpected success", err), "unexpected unittest outcome")
        # Verbose unittest output proves the specified methods actually ran.
        for selector in ids:
            method = selector.rsplit(".", 1)[-1]
            require(re.search(r"(?m)^" + re.escape(method) + r" \(.*\) \.\.\. ok$", err) is not None,
                    "selected test missing from real output: " + selector)
        self.row["executed_count"] = self.row.get("executed_count", 0) + count
        return out, err

    def identity(self):
        self.row["head"] = self.git("rev-parse", "HEAD")
        self.row["tree"] = self.git("rev-parse", "HEAD^{tree}")
        self.row["hashes"] = {name: digest(REPO / name) for name in (SOURCE, *NEW_PATHS)}
        self.row["blobs"] = {name: self.git("rev-parse", "HEAD:" + name) for name in (SOURCE, *NEW_PATHS)}
        require(not self.git("status", "--porcelain", "--untracked-files=all"), "candidate must be committed and clean")
        manifest = json.loads((REPO.parent / "SOURCE-MANIFEST.json").read_text())
        self.row["base"] = manifest["head"]
        require(self.git("rev-parse", manifest["head"] + ":" + SOURCE) == self.row["blobs"][SOURCE], "source drift from staged base")
        return manifest

    def exact_checkout(self):
        require(not self.git("status", "--porcelain", "--untracked-files=all"), "candidate must be committed and clean")
        paths = self.git("ls-files", "-z").split("\0")
        require(all(name in paths for name in NEW_PATHS), "new files not tracked")
        changed = self.git("diff", "--name-only", self.row["base"], "HEAD").splitlines()
        require(set(changed) == set(NEW_PATHS), "changed paths differ from admitted new paths")
        scan = self.child / "scan-checkout"
        self.command("clone", ["git", "clone", "--local", "--no-hardlinks", "--no-checkout", str(REPO), str(scan)], REPO.parent)
        self.command("detach", ["git", "checkout", "--detach", self.row["head"]], scan)
        require(self.git("rev-parse", "HEAD", cwd=scan) == self.row["head"], "clone head mismatch")
        require(self.git("rev-parse", "HEAD^{tree}", cwd=scan) == self.row["tree"], "clone tree mismatch")
        require(self.git("ls-files", "-z", cwd=scan).split("\0") == paths, "clone tracked path-set mismatch")
        for name in paths:
            if not name:
                continue
            path = scan / name
            require(path.is_file() and not path.is_symlink(), "nonregular scan input")
        require(not any(path.is_symlink() for path in scan.rglob("*")), "symlink artifact in scan checkout")
        require(not self.git("status", "--porcelain", "--untracked-files=all", cwd=scan), "unclean clone")
        require(not self.git("diff", "HEAD", "--", cwd=scan), "clone worktree bytes differ")
        self.row["scan_checkout"] = str(scan)
        self.row["scan_tracked_count"] = len([name for name in paths if name])
        self.row["scan_tracked_path_set_sha256"] = hashlib.sha256("\0".join(paths).encode()).hexdigest()
        return scan


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", required=True, choices=("behavior", "compatibility", "negative-control", "isolation", "credential-scan", "public-tree"))
    parser.add_argument("--output-root", required=True)
    args = parser.parse_args()
    root = safe_output_root(args.output_root)
    child = root / ("run-" + uuid.uuid4().hex)
    child.mkdir(mode=0o700)
    evidence = Evidence(child, args.case)
    try:
        manifest = evidence.identity()
        if args.case == "behavior":
            evidence.tests("behavior", BEHAVIOR, len(BEHAVIOR))
        elif args.case == "compatibility":
            evidence.tests("compatibility", COMPATIBILITY, len(COMPATIBILITY))
        elif args.case == "negative-control":
            out, _ = evidence.tests("negative-control", [BEHAVIOR[-2]], 1)
            lines = [line.removeprefix("FA1704_EXPECTED_ASSERTION ") for line in out.splitlines() if line.startswith("FA1704_EXPECTED_ASSERTION ")]
            require(len(lines) == 1, "missing actual negative-control assertion")
            assertion = json.loads(lines[0])
            require(assertion["exception"] == "AssertionError" and assertion["faulty_value"] == 7 and assertion["expected_value"] == 2,
                    "wrong negative-control cause")
            require("FA1704 meet trace/value mismatch" in assertion["traceback"], "wrong deciding assertion")
            (child / "expected-assertion.json").write_text(json.dumps(assertion, indent=2) + "\n")
            evidence.row["expected_assertion"] = assertion
            evidence.tests("restored-positive", [BEHAVIOR[0]], 1)
        elif args.case == "isolation":
            original = dict(evidence.row["hashes"])
            for name in NEW_PATHS:
                text = (REPO / name).read_text()
                require(not re.search(r"/(?:home|Users|root)/|[A-Z]:\\\\", text), "box-only path in committed code")
                require(not re.search(r"(?<![a-fA-F0-9])(?:[a-fA-F0-9]{40}|[a-fA-F0-9]{64})(?![a-fA-F0-9])", text), "literal identity hash in committed code")
            # Ordinary loader before/after a fresh, independently prefixed run.
            evidence.tests("ordinary-before", BEHAVIOR, len(BEHAVIOR))
            evidence.tests("second-prefix", BEHAVIOR, len(BEHAVIOR))
            evidence.tests("ordinary-after", BEHAVIOR, len(BEHAVIOR))
            require(original == {name: digest(REPO / name) for name in original}, "isolation source mutation")
            evidence.row["source_identity_preserved"] = True
        else:
            runtime = manifest["runtime"]
            scan = evidence.exact_checkout()
            if args.case == "credential-scan":
                python = runtime["credential_scan_python3"]
                detector = runtime["detect_secrets"]
                require(Path(python).is_file() and Path(detector).is_file(), "missing declared credential tools")
                out, _, row = evidence.command("detect-secrets", [python, detector, "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"], scan)
                report = child / "detect-secrets.json"
                report.write_text(out)
                evidence.row["scan_report_sha256"] = digest(report)
                data = json.loads(out)
                require(isinstance(data.get("results"), dict), "invalid real scan report")
                evidence.row["detector_hit_count"] = sum(len(hits) for hits in data["results"].values())
                check, _, _ = evidence.command("secret-checker", [python, str(scan / "tools/check_secret_report.py"), str(report)], scan)
                require("potential secrets: 0;" in check, "unapproved potential secrets")
            else:
                linked_python = REPO / ".venv/bin/python3"
                declared_python = Path(runtime["repo_venv"]) / "bin/python3"
                require(linked_python.resolve(strict=True) == declared_python.resolve(strict=True), "staged repo interpreter mismatch")
                out, _, _ = evidence.command("public-tree", [str(linked_python), str(scan / "tools/check_public_tree.py")], scan)
                evidence.row["public_tree_output"] = out
            # No scanner may mutate the exact-HEAD source tree.
            require(not evidence.git("status", "--porcelain", "--untracked-files=all", cwd=scan), "scanner mutated tree")
        require(evidence.git("rev-parse", "HEAD") == evidence.row["head"], "candidate HEAD changed")
        require(evidence.row["hashes"] == {name: digest(REPO / name) for name in evidence.row["hashes"]}, "candidate bytes changed")
        require(not evidence.git("status", "--porcelain", "--untracked-files=all"), "candidate status changed")
        evidence.row["exit"] = 0
    except Exception:
        evidence.row["error"] = traceback.format_exc()
        (child / "launcher-error.txt").write_text(evidence.row["error"])
        print(evidence.row["error"], file=sys.stderr)
    finally:
        (child / "receipt.json").write_text(json.dumps(evidence.row, indent=2, sort_keys=True) + "\n")
        print(json.dumps({"case": args.case, "exit": evidence.row["exit"], "executed_count": evidence.row.get("executed_count"), "receipt": str(child / "receipt.json")}, sort_keys=True))
    return evidence.row["exit"]


if __name__ == "__main__":
    raise SystemExit(main())
