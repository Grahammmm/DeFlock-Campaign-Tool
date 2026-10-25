#!/usr/bin/env python3
"""Per-card retained evidence launcher for FA1609; offline/local only."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
TEST = ROOT / "tests" / "records" / "test_fa1609_portal_tuple_cardinality.py"
SELECTORS = {
    "behavior": [
        "tests.records.test_fa1609_portal_tuple_cardinality.PortalTupleCardinalityTests.test_identity_components_and_url_alias_cardinality",
        "tests.records.test_fa1609_portal_tuple_cardinality.PortalTupleCardinalityTests.test_item_id_and_equivalent_document_paths_are_one_identity",
        "tests.records.test_fa1609_portal_tuple_cardinality.PortalTupleCardinalityTests.test_each_valid_identity_boundary_is_independent",
    ],
    "negative-control": [
        "tests.records.test_fa1609_portal_tuple_cardinality.PortalTupleCardinalityTests.test_item_only_lookup_fails_same_tuple_oracle",
    ],
    "compatibility": [
        "tests.test_portal_reconcile.PortalReconcileTests.test_two_urls_same_item_count_two_urls_one_item",
        "tests.test_portal_reconcile.PortalReconcileTests.test_output_order_independent",
    ],
}


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def checked_output_root(raw):
    root = Path(raw).absolute()
    current = Path(root.anchor)
    for part in root.parts[1:]:
        current = current / part
        if current.exists() and current.is_symlink():
            raise RuntimeError("unsafe_output_ancestry")
    root.mkdir(parents=True, exist_ok=True)
    if not root.is_dir() or root.is_symlink():
        raise RuntimeError("unsafe_output_root")
    return root.resolve()


def new_run(root):
    run = root / (time.strftime("run-%Y%m%dT%H%M%S-") + uuid.uuid4().hex)
    run.mkdir(mode=0o700)
    if run.resolve().parent != root or run.is_symlink():
        raise RuntimeError("unsafe_run_directory")
    return run


def invoke(argv, cwd, run, label):
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    temp_root = run / ("tmp-" + uuid.uuid4().hex)
    temp_root.mkdir(mode=0o700)
    env["TMPDIR"] = str(temp_root)
    env["TMP"] = str(temp_root)
    env["TEMP"] = str(temp_root)
    result = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, check=False)
    (run / (label + ".stdout.txt")).write_text(result.stdout, encoding="utf-8")
    (run / (label + ".stderr.txt")).write_text(result.stderr, encoding="utf-8")
    return {"argv": argv, "cwd": str(cwd), "exit": result.returncode,
            "stdout_sha256": hashlib.sha256(result.stdout.encode()).hexdigest(),
            "stderr_sha256": hashlib.sha256(result.stderr.encode()).hexdigest(),
            "stdout": result.stdout, "stderr": result.stderr}


def test_case(case, run, interpreter):
    selectors = SELECTORS[case]
    command = [interpreter, "-B", "-m", "unittest", "-v", *selectors]
    record = invoke(command, ROOT, run, "tests-" + uuid.uuid4().hex)
    output = record["stdout"] + record["stderr"]
    import re
    count = re.search(r"Ran (\d+) tests?", output)
    record["test_count"] = int(count.group(1)) if count else 0
    record["selected_test_ids"] = selectors
    record["skips"] = re.findall(r"skipped ['\"]([^'\"]+)", output)
    record["errors"] = len(re.findall(r"\bERROR:\s", output))
    record["failures"] = len(re.findall(r"\bFAIL:\s", output))
    if record["exit"] != 0 or record["test_count"] != len(selectors) or record["skips"] or record["errors"] or record["failures"]:
        raise RuntimeError("tests_failed:" + case)
    return record


def compatibility_closure():
    paths = [ROOT / "tests" / "test_portal_reconcile.py",
             ROOT / "tools" / "portal_reconcile.py",
             ROOT / "tests" / "fixtures" / "portal_reconcile" / "observations.json",
             ROOT / "tests" / "fixtures" / "portal_reconcile" / "queue-export.json"]
    result = []
    for path in paths:
        if not path.is_file() or path.is_symlink():
            raise RuntimeError("compatibility_closure_missing_or_unsafe")
        result.append({"path": str(path.relative_to(ROOT)), "sha256": sha(path)})
    fixtures = [path for path in paths if "fixtures" in path.parts]
    for fixture in fixtures:
        text = fixture.read_text(encoding="utf-8")
        if "example.test" not in text:
            raise RuntimeError("compatibility_fixture_not_synthetic")
    return result


def clone_exact(run, interpreter):
    export = run / "exact-head-export"
    records = []
    records.append(invoke(["git", "clone", "--no-hardlinks", "--no-checkout", str(ROOT), str(export)], ROOT, run, "clone"))
    if records[-1]["exit"]:
        raise RuntimeError("clone_failed")
    records.append(invoke(["git", "checkout", "--detach", "HEAD"], export, run, "checkout"))
    if records[-1]["exit"]:
        raise RuntimeError("checkout_failed")
    head = invoke(["git", "rev-parse", "HEAD"], export, run, "export-head")
    source_head = invoke(["git", "rev-parse", "HEAD"], ROOT, run, "source-head")
    tracked_source = invoke(["git", "ls-files", "-z"], ROOT, run, "source-paths")
    tracked_export = invoke(["git", "ls-files", "-z"], export, run, "export-paths")
    if head["exit"] or source_head["exit"] or head["stdout"].strip() != source_head["stdout"].strip():
        raise RuntimeError("head_mismatch")
    source_tree = invoke(["git", "rev-parse", "HEAD^{tree}"], ROOT, run, "source-tree")
    export_tree = invoke(["git", "rev-parse", "HEAD^{tree}"], export, run, "export-tree")
    if source_tree["exit"] or export_tree["exit"] or source_tree["stdout"].strip() != export_tree["stdout"].strip():
        raise RuntimeError("tree_mismatch")
    if tracked_source["stdout"] != tracked_export["stdout"]:
        raise RuntimeError("tracked_paths_mismatch")
    paths = set(tracked_export["stdout"].split("\0"))
    required = {"tests/records/test_fa1609_portal_tuple_cardinality.py", "tools/accept_fa1609.py"}
    if not required.issubset(paths):
        raise RuntimeError("required_paths_missing_from_export")
    records.extend((source_tree, export_tree))
    return export, records, head["stdout"].strip()


def scan_case(case, run, interpreter):
    manifest_path = ROOT.parent / "SOURCE-MANIFEST.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    export, records, head = clone_exact(run, interpreter)
    if case == "credential-scan":
        scanner = manifest["runtime"]["detect_secrets"]
        scan_argv = [interpreter, scanner, "scan", "--all-files", "--exclude-files", r"^.git/", "--no-verify", str(export)]
        scan = subprocess.run(scan_argv,
                              cwd=export, capture_output=True, text=True, check=False)
        report = run / "detect-secrets-report.json"
        report.write_text(scan.stdout, encoding="utf-8")
        (run / "scanner.stderr.txt").write_text(scan.stderr, encoding="utf-8")
        records.append({"argv": scan_argv,
                        "cwd": str(export), "exit": scan.returncode,
                        "stdout_sha256": hashlib.sha256(scan.stdout.encode()).hexdigest(),
                        "stderr_sha256": hashlib.sha256(scan.stderr.encode()).hexdigest(),
                        "report_sha256": sha(report)})
        checker = invoke([interpreter, str(export / "tools" / "check_secret_report.py"), str(report)], export, run, "secret-checker")
        records.append(checker)
        if scan.returncode or checker["exit"] or not scan.stdout.strip():
            raise RuntimeError("credential_scan_failed")
    else:
        check = invoke([interpreter, str(export / "tools" / "check_public_tree.py")], export, run, "public-tree")
        records.append(check)
        if check["exit"]:
            raise RuntimeError("public_tree_failed")
    return {"head": head, "commands": records}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", required=True, choices=("behavior", "compatibility", "negative-control", "isolation", "credential-scan", "public-tree"))
    parser.add_argument("--output-root", required=True)
    args = parser.parse_args()
    output_root = checked_output_root(args.output_root)
    run = new_run(output_root)
    interpreter = sys.executable
    record = {"case": args.case, "launcher_argv": list(sys.argv), "run_dir": str(run), "cwd": str(ROOT), "interpreter": interpreter,
              "head": subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip(),
              "source_sha256": sha(Path(__file__)), "test_sha256": sha(TEST), "commands": []}
    try:
        if args.case == "compatibility":
            record["compatibility_closure"] = compatibility_closure()
            record["commands"].append(test_case(args.case, run, interpreter))
        elif args.case in SELECTORS:
            record["commands"].append(test_case(args.case, run, interpreter))
        elif args.case == "isolation":
            original_path = list(sys.path)
            original_tempdir = __import__("tempfile").tempdir
            from unittest.mock import _patch
            original_patches = list(_patch._active_patches)
            source_bytes = (Path(__file__).read_bytes(), TEST.read_bytes())
            box_path_patterns = (b"/" + b"home/", b"/" + b"agent-work/", b"plans" + b"/")
            record["box_only_path_scan"] = {str(path.relative_to(ROOT)): [pattern.decode() for pattern in box_path_patterns if pattern in path.read_bytes()]
                                             for path in (Path(__file__), TEST)}
            if any(record["box_only_path_scan"].values()):
                raise RuntimeError("box_only_path_found")
            first_root = run / "fixture-prefix-one"
            second_root = run / "fixture-prefix-two"
            first_root.mkdir(mode=0o700)
            second_root.mkdir(mode=0o700)
            first = test_case("behavior", first_root, interpreter)
            second = test_case("behavior", second_root, interpreter)
            record["commands"].extend((first, second))
            record["unittest_importable"] = True
            record["unpoisoned_state"] = {
                "sys_path_unchanged": list(sys.path) == original_path,
                "tempfile_tempdir_unchanged": __import__("tempfile").tempdir == original_tempdir,
                "mock_patches_unchanged": list(_patch._active_patches) == original_patches,
            }
            record["source_hashes_unchanged"] = source_bytes == (Path(__file__).read_bytes(), TEST.read_bytes())
            if not all(record["unpoisoned_state"].values()) or not record["source_hashes_unchanged"]:
                raise RuntimeError("isolation_state_poisoned")
        elif args.case in ("credential-scan", "public-tree"):
            manifest = json.loads((ROOT.parent / "SOURCE-MANIFEST.json").read_text(encoding="utf-8"))
            selected = manifest["runtime"]["credential_scan_python3"] if args.case == "credential-scan" else interpreter
            record["interpreter"] = selected
            record.update(scan_case(args.case, run, selected))
        record["status"] = "PASS"
    except Exception as exc:
        record["status"] = "FAIL"
        record["error"] = str(exc)
    record_path = run / "acceptance.json"
    record_path.write_text(json.dumps(record, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": record["status"], "case": args.case, "evidence": str(record_path),
                      "head": record["head"], "commands": len(record["commands"])}))
    return 0 if record["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
