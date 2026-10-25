"""Small FA1302 offline acceptance runner; each invocation retains unique evidence."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import traceback

sys.dont_write_bytecode = True
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from tests.fixtures.fa1302.boundary import WORKER, fresh_output, child_env

CASES = ("behavior", "compatibility", "negative-control", "isolation", "credential-scan", "public-tree")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def source_identity():
    def git(*args):
        result = subprocess.run(["git", *args], cwd=REPO, capture_output=True, check=True)
        return result.stdout
    names = git("ls-files", "-z", "--cached", "--others", "--exclude-standard").decode().split("\0")
    files = {}
    for name in sorted(set(names) - {""}):
        path = REPO / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("unsafe_source_file")
        files[name] = digest(path.read_bytes())
    return {"head": git("rev-parse", "HEAD").decode().strip(),
            "dirty_diff_sha256": digest(git("diff", "HEAD", "--binary")),
            "files": files, "files_sha256": digest(json.dumps(files, sort_keys=True).encode())}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=CASES, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    output = fresh_output(args.output_root)
    records = []
    before = source_identity()
    receipt = {"case": args.case, "argv": sys.argv, "cwd": str(Path.cwd()), "repo": str(REPO),
               "interpreter": sys.executable, "source": before, "commands": records,
               "result": "FAIL", "scope": "REVIEW_REQUIRED; not full CI or live/semantic acceptance"}

    def run(argv, label, env=None):
        result = subprocess.run(argv, cwd=REPO, env=env, capture_output=True, timeout=240)
        stdout, stderr = output / (label + ".stdout"), output / (label + ".stderr")
        stdout.write_bytes(result.stdout)
        stderr.write_bytes(result.stderr)
        records.append({"argv": argv, "cwd": str(REPO), "exit": result.returncode,
                        "stdout": str(stdout), "stdout_sha256": digest(result.stdout),
                        "stderr": str(stderr), "stderr_sha256": digest(result.stderr)})
        if result.returncode != 0:
            raise RuntimeError("child_failed:" + label)
        return result

    try:
        manifest_path = REPO.parent / "SOURCE-MANIFEST.json"
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
        receipt["source_manifest_sha256"] = digest(manifest_bytes)
        if args.case in {"behavior", "compatibility", "negative-control", "isolation"}:
            data_root = output / "data"
            data_root.mkdir(mode=0o700)
            run([sys.executable, "-B", str(WORKER), args.case, str(data_root)], "tests", child_env(data_root))
            counts = json.loads((data_root / "tests.json").read_text())
            receipt["tests"] = counts
            if (counts["selected"] <= 0 or counts["selected"] != counts["tests"]
                    or any(counts[key] for key in ("failures", "errors", "skips")) or counts["events"]):
                raise RuntimeError("invalid_test_result")
            if Path(counts["module"]) != REPO / "campaign_tool/records/mail_adoption.py":
                raise RuntimeError("wrong_candidate_module")
            receipt["module_hashes"] = {name: digest(Path(path).read_bytes()) for name, path in counts["modules"].items()}
            if args.case == "negative-control":
                controls = list(data_root.rglob("controls.json"))
                if len(controls) != 1:
                    raise RuntimeError("missing_negative_control_outputs")
                control = json.loads(controls[0].read_text())
                if (set(control) != {"valid-broken-control", "broken-peer", "restored-peer"}
                        or control["broken-peer"]["errors"] != 1
                        or "TypeError: unhashable type: 'list'" not in control["broken-peer"]["output"]):
                    raise RuntimeError("invalid_negative_control")
                receipt["controls"] = str(controls[0])
        elif args.case == "credential-scan":
            runtime = manifest["runtime"]
            scan = run([runtime["credential_scan_python3"], runtime["detect_secrets"], "scan", "--all-files",
                        "--exclude-files", "^.git/", "--no-verify"], "scan")
            report = output / "scan.stdout"
            json.loads(scan.stdout)  # malformed or missing scanner output is fatal
            checked = run([runtime["credential_scan_python3"], str(REPO / "tools/check_secret_report.py"),
                           str(report)], "secret-check")
            if b"potential secrets: 0;" not in checked.stdout:
                raise RuntimeError("missing_zero_secret_verdict")
            receipt["checks"] = 2
        else:
            result = run([str(REPO / ".venv/bin/python3"), str(REPO / "tools/check_public_tree.py")], "public-tree",
                         dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
            data = json.loads(result.stdout)
            if data["scanned_files"] <= 0 or data["findings"]:
                raise RuntimeError("public_tree_not_clear")
            receipt["public_tree"] = data
        after = source_identity()
        if after != before:
            raise RuntimeError("source_changed_during_acceptance")
        receipt["result"] = "PASS"
    except Exception as error:
        receipt["error"] = str(error)
        (output / "exception.txt").write_text(traceback.format_exc())
    # Finish all writers before hashing outputs. No old receipt is overwritten.
    receipt["evidence_files"] = {str(p.relative_to(output)): digest(p.read_bytes())
                                 for p in sorted(output.rglob("*")) if p.is_file()}
    path = output / "receipt.json"
    path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"case": args.case, "result": receipt["result"], "receipt": str(path),
                      "receipt_sha256": digest(path.read_bytes())}, sort_keys=True))
    return int(receipt["result"] != "PASS")


if __name__ == "__main__":
    raise SystemExit(main())
