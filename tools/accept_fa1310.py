#!/usr/bin/env python3
"""Six bounded FA1310 acceptance cases; evidence is never stored in the repo."""
import sys
sys.dont_write_bytecode = True
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from tests.fixtures.fa1310.support import fresh, safe_path

CASES = ("behavior", "compatibility", "negative-control", "isolation", "credential-scan", "public-tree")
CHANGED = ("campaign_tool/records/portal/notice.py", "campaign_tool/records/portal/collections.py",
           "tests/records/test_fa1310.py", "tests/fixtures/fa1310/support.py", "tools/accept_fa1310.py")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", required=True, choices=CASES)
    parser.add_argument("--output-root", required=True, type=Path)
    args = parser.parse_args()
    root = safe_path(args.output_root)
    if root == REPO or REPO in root.parents:
        parser.error("evidence_must_be_outside_repo")
    run = fresh(root)
    commands = []

    def command(argv, label):
        result = subprocess.run(list(map(str, argv)), cwd=REPO, capture_output=True)
        out, err = run / (label + ".stdout"), run / (label + ".stderr")
        out.write_bytes(result.stdout)
        err.write_bytes(result.stderr)
        commands.append({"argv": list(map(str, argv)), "cwd": str(REPO), "exit": result.returncode,
                         "stdout": str(out), "stdout_sha256": sha(out), "stderr": str(err), "stderr_sha256": sha(err)})
        return result

    head = command(["git", "rev-parse", "HEAD"], "head")
    diff = command(["git", "diff", "--no-ext-diff", "HEAD"], "diff")
    manifest = REPO.parent / "SOURCE-MANIFEST.json"
    receipt = {"case": args.case, "invocation": str(run), "argv": sys.argv, "cwd": str(Path.cwd()),
               "interpreter": sys.executable, "interpreter_resolved": str(Path(sys.executable).resolve()),
               "head": head.stdout.decode().strip(), "dirty_diff_sha256": hashlib.sha256(diff.stdout).hexdigest(),
               "source_manifest_sha256": sha(manifest), "files": {name: sha(REPO / name) for name in CHANGED},
               "commands": commands, "status": "FAIL", "limits": "Scoped offline acceptance; full CI, human/legal acceptance and shipping remain owed."}
    exit_code = 1
    try:
        if head.returncode or diff.returncode:
            raise RuntimeError("source_identity_failed")
        if args.case in CASES[:4]:
            child_root = run / "suite"
            result = command([sys.executable, "-B", REPO / "tests/records/test_fa1310.py", args.case, child_root], "suite")
            summary = json.loads((child_root / "result.json").read_text())
            receipt["test_result"] = summary
            if result.returncode or not summary["tests"] or any(summary[key] for key in ("failures", "errors", "skips")):
                raise RuntimeError("suite_failed")
        elif args.case == "credential-scan":
            runtime = json.loads(manifest.read_text())["runtime"]
            scan_python = runtime["credential_scan_python3"]
            scan = command([scan_python, runtime["detect_secrets"], "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"], "scan")
            if scan.returncode:
                raise RuntimeError("scanner_failed")
            # Feed the exact actual stdout bytes to the unchanged checker.
            json.loads(scan.stdout)
            checked = command([scan_python, REPO / "tools/check_secret_report.py", run / "scan.stdout"], "secret-report")
            if checked.returncode or b"potential secrets: 0;" not in checked.stdout:
                raise RuntimeError("secret_report_failed")
        else:
            checked = command([REPO / ".venv/bin/python3", "-B", REPO / "tools/check_public_tree.py"], "public-tree")
            report = json.loads(checked.stdout)
            if checked.returncode or report.get("findings") != [] or not report.get("scanned_files"):
                raise RuntimeError("public_tree_failed")
            receipt["scanned_files"] = report["scanned_files"]
        receipt["status"] = "PASS_REVIEW_REQUIRED"
        exit_code = 0
    except (OSError, ValueError, KeyError, RuntimeError) as error:
        receipt["error_type"] = type(error).__name__
        # No untrusted credential values or arbitrary exception text in the summary.
    receipt["evidence_hashes"] = {str(path.relative_to(run)): sha(path) for path in sorted(run.rglob("*")) if path.is_file() and not path.is_symlink()}
    (run / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({"status": receipt["status"], "receipt": str(run / "receipt.json"), "tests": receipt.get("test_result")}))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
