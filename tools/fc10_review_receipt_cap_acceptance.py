"""Candidate-local, offline FC10 acceptance; one immutable UUID receipt per invocation."""
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
import unittest
from unittest import mock
import uuid

REPO = Path(__file__).resolve().parents[1]
PRODUCT = "campaign_tool/records/review_bundle.py"
TEST = "tests/records/test_fc10_review_receipt_cap.py"
FIXTURE = "tests/fixtures/fc10-review-receipt-cap/cases.json"
RUNNER = "tools/fc10_review_receipt_cap_acceptance.py"
AUTHORED = [PRODUCT, TEST, FIXTURE, RUNNER]
SCANNERS = ["tools/check_public_tree.py", "campaign_tool/records/public_scan.py", "tools/check_secret_report.py"]
BASE_PARTS = ("17da706588467184f0923", "e2516be3319d577539a")
BASE = "".join(BASE_PARTS)


def require(value, message):
    if not value:
        raise RuntimeError(message)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dump(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def git(*args, cwd=REPO):
    return subprocess.check_output(["git", *args], cwd=cwd).decode().strip()


def identity():
    return {"head": git("rev-parse", "HEAD"), "tree": git("rev-parse", "HEAD^{tree}"),
            "status": git("status", "--porcelain=v1", "--untracked-files=all"),
            "authored_sha256": {name: digest(REPO / name) for name in AUTHORED},
            "scanner_sha256": {name: digest(REPO / name) for name in SCANNERS}}


def validate(manifest_path, phase):
    require(manifest_path.is_absolute() and manifest_path.resolve() == manifest_path,
            "source manifest must be canonical")
    manifest = json.loads(manifest_path.read_text())
    require(manifest["schema"] == "agentorange.source-manifest/v1", "manifest schema")
    require(Path(manifest["repo"]) == REPO and Path(manifest["write_root"]) == REPO.parent,
            "staged repo/root mismatch")
    require(manifest["head"] == BASE and git("rev-parse", BASE + "^{tree}") == manifest["tree"],
            "pinned base mismatch")
    require(subprocess.run(["git", "merge-base", "--is-ancestor", BASE, "HEAD"], cwd=REPO).returncode == 0,
            "candidate does not descend from base")
    runtime = manifest["runtime"]
    require((REPO / ".venv").is_symlink() and (REPO / ".venv").resolve() == Path(runtime["repo_venv"]),
            "standard runtime link mismatch")
    expected = (Path(runtime["credential_scan_python3"]) if phase == "credentials"
                else REPO / ".venv" / "bin" / "python3")
    require(Path(sys.executable).absolute() == expected, "literal interpreter mismatch")
    for relative in SCANNERS:
        original = subprocess.check_output(["git", "show", BASE + ":" + relative], cwd=REPO)
        require((REPO / relative).read_bytes() == original, "scanner/allowlist changed")
    require(not any((REPO / name).is_symlink() for name in AUTHORED), "authored symlink")
    return manifest


def child(mode, root, report):
    # Fresh script children may set their own temporary root, never a discovery import.
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    tempfile.tempdir = str(root.resolve())
    sys.path.insert(0, str(REPO))
    if mode == "discovery":
        before = (dict(os.environ), tempfile.tempdir, list(sys.path), Path.cwd(), socket.socket, subprocess.Popen)
        from tests.records import test_fc10_review_receipt_cap as tests
        after = (dict(os.environ), tempfile.tempdir, list(sys.path), Path.cwd(), socket.socket, subprocess.Popen)
        require(before == after, "test import contaminated ambient state")
        return tests.run_suite(report, discovery=True)
    def denied(*args, **kwargs):
        raise RuntimeError("fc10 scoped offline call forbidden")
    with ExitStack() as stack:
        for obj, name in ((socket, "socket"), (socket, "create_connection"), (subprocess, "Popen")):
            stack.enter_context(mock.patch.object(obj, name, denied))
        suite = unittest.defaultTestLoader.loadTestsFromName("tests.records.test_review_bundle")
        ids = []
        class Result(unittest.TextTestResult):
            def startTest(self, test):
                ids.append(test.id())
                super().startTest(test)
        result = unittest.TextTestRunner(verbosity=2, resultclass=Result).run(suite)
    dump(report, {"tests": result.testsRun, "test_ids": ids,
        "failures": [(str(t), text) for t, text in result.failures],
        "errors": [(str(t), text) for t, text in result.errors],
        "skips": [(str(t), text) for t, text in result.skipped]})
    return int(not result.wasSuccessful() or bool(result.skipped))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=["suite", "compatibility", "portable", "credentials", "public-tree"])
    parser.add_argument("--source-manifest", type=Path)
    parser.add_argument("--out-base", type=Path)
    parser.add_argument("--child", choices=["compatibility", "discovery"])
    parser.add_argument("--temp-root", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.child:
        return child(args.child, args.temp_root, args.report)
    manifest = validate(args.source_manifest, args.phase)
    require(args.out_base.is_absolute() and args.out_base == REPO.parent / "out"
            and args.out_base.resolve() == args.out_base, "out-base mismatch")
    args.out_base.mkdir(mode=0o700, exist_ok=True)
    output = args.out_base / (args.phase + "-" + str(uuid.uuid4()))
    output.mkdir(mode=0o700)
    temporary = output / "tmp"
    temporary.mkdir(mode=0o700)
    tempfile.tempdir = str(temporary)
    initial = identity()
    receipt = {"schema": "fc10-acceptance/v1", "phase": args.phase, "literal_argv": [sys.executable, *sys.argv],
        "cwd": str(Path.cwd()), "interpreter": sys.executable, "identity": initial,
        "source_manifest_sha256": digest(args.source_manifest), "commands": [],
        "limits": "REVIEW REQUIRED; scoped offline checks only; remote exact-head CI OWED; no semantic/legal/human/live acceptance."}
    require(Path.cwd() == REPO, "acceptance cwd mismatch")

    def run(label, argv, cwd=REPO, allowed=(0,)):
        completed = subprocess.run(argv, cwd=cwd, shell=False, capture_output=True, timeout=600)
        stdout, stderr = output / (label + ".stdout"), output / (label + ".stderr")
        stdout.write_bytes(completed.stdout)
        stderr.write_bytes(completed.stderr)
        receipt["commands"].append({"label": label, "argv": [str(a) for a in argv], "cwd": str(cwd),
            "exit": completed.returncode, "stdout": stdout.name, "stderr": stderr.name,
            "stdout_sha256": digest(stdout), "stderr_sha256": digest(stderr)})
        require(completed.returncode in allowed, label + " exit " + str(completed.returncode))
        return completed

    def suite(label, cwd, discovery=False):
        report = output / (label + ".json")
        root = temporary / label
        argv = ([sys.executable, str(REPO / RUNNER), "--child", "discovery"] if discovery
                else [sys.executable, str(REPO / TEST)])
        argv += ["--temp-root", str(root), "--report", str(report)]
        run(label, argv, cwd)
        data = json.loads(report.read_text())
        require(data["tests"] == 9 and not data["failures"] and not data["errors"] and not data["skips"],
                "required suite tests missing/failed/skipped")
        cases = {item["case"] for item in data["observations"]}
        spec = json.loads((REPO / FIXTURE).read_text())
        require(cases == set(spec["cases"]) | {"negative_control", "restored_positive"}, "frozen case coverage")
        require(len(data["observations"]) == 21, "generated-case count mismatch")
        if discovery:
            require(data["discovery_sentinel_unchanged"], "discovery sentinel")
        receipt.setdefault("test_reports", []).append({"path": report.name, "tests": data["tests"],
            "generated_cases": len(data["observations"]), "case_dimensions": sorted(cases)})

    try:
        if args.phase == "suite":
            suite("suite", REPO)
        elif args.phase == "compatibility":
            report = output / "compatibility.json"
            run("compatibility", [sys.executable, str(REPO / RUNNER), "--child", "compatibility",
                "--temp-root", str(temporary / "compatibility"), "--report", str(report)])
            data = json.loads(report.read_text())
            require(data["tests"] > 0 and not data["errors"] and not data["failures"] and not data["skips"],
                    "compatibility incomplete")
            receipt["test_reports"] = [{"path": report.name, "tests": data["tests"]}]
        elif args.phase == "portable":
            for relative in AUTHORED:
                require(not re.search(r"/(?:home|Users|workspace)/", (REPO / relative).read_text()),
                        "host-only source literal: " + relative)
            different = output / "fresh-cwd"
            different.mkdir(mode=0o700)
            suite("portable", different)
            suite("discovery", different, discovery=True)
        elif args.phase == "credentials":
            scanner = Path(manifest["runtime"]["detect_secrets"])
            require(scanner.parent == Path(manifest["runtime"]["credential_scan_python3"]).parent,
                    "scanner runtime mismatch")
            # --all-files uses the installed scanner's no-follow directory traversal;
            # do not change ignores or traverse/link the dependency runtime ourselves.
            scanned = run("detect-secrets", [str(scanner), "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"])
            report = output / "secrets.json"
            report.write_bytes(scanned.stdout)
            parsed = json.loads(scanned.stdout)
            require(isinstance(parsed["results"], dict), "invalid genuine scanner JSON")
            run("check-secret-report", [sys.executable, str(REPO / "tools/check_secret_report.py"), str(report)])
            receipt["secret_scan"] = {"result_files": len(parsed["results"]), "report": report.name}
        elif args.phase == "public-tree":
            require(initial["status"] in ("", "?? .venv"), "uncommitted/scan-visible material")
            inventory = subprocess.check_output(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=REPO)
            (output / "source-inventory.bin").write_bytes(inventory)
            tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=REPO).decode().split("\0")
            require(set(inventory.decode().split("\0")) - {"", ".venv"} == set(tracked) - {""}, "untracked scan inventory")
            require(not git("diff", "HEAD", "--") and not git("diff", "--cached", "--"), "tracked/index bytes differ")
            for relative in AUTHORED:
                raw = subprocess.check_output(["git", "show", initial["head"] + ":" + relative], cwd=REPO)
                require(raw == (REPO / relative).read_bytes(), "authored bytes differ from HEAD")
            clone = output / "committed-tree"
            run("clone", ["git", "clone", "--local", "--no-hardlinks", "--no-checkout", "--no-recurse-submodules", str(REPO), str(clone)])
            run("checkout", ["git", "checkout", "--detach", initial["head"]], clone)
            require(git("rev-parse", "HEAD", cwd=clone) == initial["head"] and
                    git("rev-parse", "HEAD^{tree}", cwd=clone) == initial["tree"], "clone identity")
            require(not (clone / ".venv").exists(), "clone runtime link forbidden")
            for relative, expected in {**initial["authored_sha256"], **initial["scanner_sha256"]}.items():
                require(digest(clone / relative) == expected, "clone hash mismatch: " + relative)
            scanned = run("public-committed-tree", [sys.executable, str(clone / "tools/check_public_tree.py")], clone)
            data = json.loads(scanned.stdout)
            require(data["findings"] == [] and data["scanned_files"] > 0, "committed-tree findings/incomplete")
            inplace = run("in_place_informational", [sys.executable, str(REPO / "tools/check_public_tree.py")], allowed=(0, 1))
            info = json.loads(inplace.stdout)
            require((inplace.returncode == 0 and info["findings"] == []) or
                    (inplace.returncode == 1 and info["findings"] == [[".venv", 0, "symlink_not_scanned"]]),
                    "unexpected in-place result")
            receipt["public_tree"] = {"committed_scanned_files": data["scanned_files"],
                "findings": data["findings"], "in_place_informational": info, "incident": "INC-174"}
        require(identity() == initial, "source changed during acceptance")
        receipt["exit"] = 0
    except Exception as error:
        receipt["exit"] = 1
        receipt["error"] = str(error)
    dump(output / "receipt.json", receipt)
    # All children have finished. Freeze every regular evidence byte, including
    # retained synthetic roots and clone files; no aliases or omitted output.
    paths = sorted(p for p in output.rglob("*") if p.is_file())
    require(not any(p.is_symlink() for p in output.rglob("*")), "evidence symlink")
    entries = [{"path": p.relative_to(output).as_posix(), "sha256": digest(p), "bytes": p.stat().st_size} for p in paths]
    require(len({e["path"] for e in entries}) == len(entries), "duplicate evidence path")
    evidence = output / "evidence-manifest.json"
    dump(evidence, {"schema": "fc10-evidence/v1", "files": entries})
    saved = json.loads(evidence.read_text())["files"]
    actual = {p.relative_to(output).as_posix() for p in output.rglob("*") if p.is_file() and p != evidence}
    require(actual == {e["path"] for e in saved}, "evidence omitted output")
    for entry in saved:
        path = output / entry["path"]
        require(path.resolve() == path and digest(path) == entry["sha256"] and
                path.stat().st_size == entry["bytes"], "invalid evidence hash/path")
    print(json.dumps({"phase": args.phase, "exit": receipt["exit"], "output": str(output),
        "receipt_sha256": digest(output / "receipt.json"), "manifest_sha256": digest(evidence),
        "tests": receipt.get("test_reports", []), "error": receipt.get("error")}))
    return receipt["exit"]


if __name__ == "__main__":
    raise SystemExit(main())
