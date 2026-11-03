"""File-launched, offline FA99 acceptance with immutable per-invocation evidence.

Only the explicit selectors below are loaded. This is scoped evidence, not the
repository shipping gate. Audit guards are confined to the fresh worker process;
ordinary unittest imports do not alter ambient state or peer suites.
"""
import argparse
import hashlib
import inspect
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
CANDIDATE = ROOT.parent
sys.dont_write_bytecode = True
SELECTORS = {
    "behavior": ("tests.records.test_fa99.Behavior",),
    "compatibility": ("tests.records.test_eml_export_faults",
                      "tests.records.test_export_bridge.ExportBridgeTests.test_replay_and_uidvalidity_reset_keep_originals_and_add_occurrence"),
    "negative-control": ("tests.records.test_fa99.GateControl",),
}
PRODUCTION = "campaign_tool/records/intake/eml_export.py"


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def file_digest(path):
    return digest(path.read_bytes())


def document(path, value):
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")


def git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, check=True).stdout


def identity():
    manifest = CANDIDATE / "SOURCE-MANIFEST.json"
    staged = json.loads(manifest.read_text())
    measured_tree = git("rev-parse", staged["head"] + "^{tree}").decode().strip()
    if measured_tree != staged["tree"]:
        raise RuntimeError("staged_baseline_tree_mismatch")
    if (ROOT / ".venv").resolve() != Path(staged["runtime"]["repo_venv"]).resolve():
        raise RuntimeError("staged_venv_mismatch")
    if Path(sys.prefix).resolve() != Path(staged["runtime"]["repo_venv"]).resolve():
        raise RuntimeError("actual_runtime_prefix_mismatch")
    return {
        "interpreter": sys.executable,
        "python_version": sys.version,
        "runtime_prefix": sys.prefix,
        "verified_staged_base_tree": measured_tree,
        "interpreter_realpath": str(Path(sys.executable).resolve()),
        "source_manifest_sha256": file_digest(manifest),
        "head": git("rev-parse", "HEAD").decode().strip(),
        "dirty_diff_sha256": digest(git("diff", "--binary", "HEAD")),
        "status": git("status", "--porcelain").decode(),
        "source_fixture_test_hashes": {name: file_digest(ROOT / name) for name in (
            PRODUCTION, "tests/records/test_fa99.py", "tools/accept_fa99.py",
            "tests/records/test_eml_export_faults.py", "tests/records/test_export_bridge.py",
            "tools/check_secret_report.py", "tools/check_public_tree.py")},
    }


def install_worker_boundary(run):
    # No home-discovered config exists in this API closure. mail_root, Pipeline
    # root and canonical backend paths are required arguments; config path=None
    # means no discovery. tempfile has process-global AND bound dir=None users.
    temporary = run / "tmp"
    temporary.mkdir(mode=0o700)
    tempfile.tempdir = str(temporary)
    for key in ("TMPDIR", "TMP", "TEMP"):
        os.environ[key] = str(temporary)

    def checked(value, dir_fd=None):
        if isinstance(value, int):
            return  # fd was already opened within this process
        path = Path(os.fsdecode(value))
        if not path.is_absolute():
            base = Path(os.readlink("/proc/self/fd/" + str(dir_fd))) if dir_fd not in (None, -1) else Path.cwd()
            path = base / path
        path = Path(os.path.abspath(path))
        if not path.is_relative_to(run) or not path.resolve().is_relative_to(run):
            raise PermissionError("fa99_write_outside_run")

    def audit(event, args):
        if event.startswith("socket."):
            raise PermissionError("fa99_offline_socket")
        if event == "open":
            _, mode, flags = args
            if (mode and any(char in mode for char in "wax+")) or (flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)):
                checked(args[0])
        elif event in {"os.mkdir", "os.remove", "os.rmdir", "os.chmod"}:
            checked(args[0], args[2] if len(args) > 2 else args[1])
        elif event in {"os.rename", "os.link"}:
            checked(args[0], args[2])
            checked(args[1], args[3])
        elif event == "os.symlink":
            raise PermissionError("fa99_no_symlink_creation")
        elif event == "sqlite3.connect":
            value = os.fsdecode(args[0])
            if value.startswith("file:"):
                from urllib.parse import unquote, urlsplit
                uri = urlsplit(value)
                if uri.netloc or not uri.path.startswith("/"):
                    raise PermissionError("fa99_nonlocal_database_uri")
                value = unquote(uri.path)
            checked(value)
        elif event == "subprocess.Popen":
            allowed = ["git", "show", json.loads((CANDIDATE / "SOURCE-MANIFEST.json").read_text())["head"] + ":" + PRODUCTION]
            if list(args[1]) != allowed or Path(args[2]) != ROOT:
                raise PermissionError("fa99_no_external_worker_process")
        elif event in {"os.system", "os.exec", "os.posix_spawn", "os.forkpty"}:
            raise PermissionError("fa99_no_external_worker_process")
    sys.addaudithook(audit)


def worker(case, run, baseline_file):
    install_worker_boundary(run)  # precedes every project import
    sys.path.insert(0, str(ROOT))
    from campaign_tool.records.intake import eml_export
    from campaign_tool.records import config
    # Verify the real entry defaults rather than inventing host path globals.
    if inspect.signature(eml_export.export_message).parameters["mail_root"].default is not inspect.Parameter.empty:
        raise RuntimeError("unexpected_mail_root_default")
    if inspect.signature(config.load_config).parameters["path"].default is not None:
        raise RuntimeError("unexpected_config_discovery_default")
    probe = Path(tempfile.mkdtemp(prefix="boundary-"))
    if not probe.is_relative_to(run):
        raise RuntimeError("tempfile_boundary_failed")
    try:
        socket.socket()
    except PermissionError as error:
        if str(error) != "fa99_offline_socket":
            raise
    else:
        raise RuntimeError("offline_boundary_failed")
    try:
        (run.parent / "forbidden-synthetic-write").write_bytes(b"not written")
    except PermissionError as error:
        if str(error) != "fa99_write_outside_run":
            raise
    else:
        raise RuntimeError("write_boundary_failed")
    document(run / "boundary.json", {"pre_import_socket_and_write_guards": True,
        "tempfile_global_and_bound_none_defaults": str(tempfile.tempdir),
        "mail_root_required": True, "config_none_means_no_host_discovery": True,
        "host_path_globals_in_selected_API_closure": [], "fresh_pid": os.getpid()})
    if baseline_file:
        source = Path(baseline_file).read_bytes()
        exec(compile(source, str(baseline_file), "exec"), eml_export.__dict__)
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(name) for name in SELECTORS[case])
    count = suite.countTestCases()
    if count == 0:
        raise RuntimeError("empty_test_selection")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    stats = {"executed": result.testsRun, "selected": count, "skipped": len(result.skipped),
             "failures": len(result.failures), "errors": len(result.errors),
             "unexpected_successes": len(result.unexpectedSuccesses),
             "expected_failures": len(result.expectedFailures)}
    document(run / "test-counts.json", stats)
    return int(not result.wasSuccessful() or result.testsRun != count or bool(result.skipped) or bool(result.expectedFailures))


def invoke(run, label, argv):
    result = subprocess.run(argv, cwd=ROOT, capture_output=True, check=False)
    stdout = run / (label + ".stdout.txt")
    stderr = run / (label + ".stderr.txt")
    stdout.write_bytes(result.stdout)
    stderr.write_bytes(result.stderr)
    receipt = {"argv": argv, "cwd": str(ROOT), "exit": result.returncode,
               "stdout": str(stdout), "stderr": str(stderr),
               "stdout_sha256": file_digest(stdout), "stderr_sha256": file_digest(stderr)}
    document(run / (label + ".receipt.json"), receipt)
    return receipt


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", required=True, choices=(*SELECTORS, "credential-scan", "public-tree"))
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--baseline", action="store_true", help="retain pinned-baseline evidence separately")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--baseline-file", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        return worker(args.case, Path(args.output_root).resolve(), args.baseline_file)
    output = Path(args.output_root).resolve()
    if output.is_relative_to(ROOT):
        raise ValueError("evidence_must_be_outside_repository")
    output.mkdir(parents=True, exist_ok=True)
    run = output / (args.case + "-" + uuid.uuid4().hex)
    run.mkdir(mode=0o700)
    manifest_doc = json.loads((CANDIDATE / "SOURCE-MANIFEST.json").read_text())
    frozen_identity = identity()
    if args.baseline and args.case not in {"behavior", "compatibility"}:
        raise ValueError("baseline_only_supports_behavior_and_compatibility")
    records = []
    status = 1
    counts = None
    try:
        if args.case in SELECTORS:
            command = [str(ROOT / ".venv/bin/python3"), str(Path(__file__).resolve()),
                       "--worker", "--case", args.case, "--output-root", str(run)]
            if args.baseline:
                baseline_file = run / "pinned-eml-export.py"
                baseline_file.write_bytes(git("show", manifest_doc["head"] + ":" + PRODUCTION))
                command.extend(["--baseline-file", str(baseline_file)])
            records.append(invoke(run, "tests", command))
            counts = json.loads((run / "test-counts.json").read_text())
            status = records[-1]["exit"]
        elif args.case == "credential-scan":
            runtime = manifest_doc["runtime"]
            interpreter = runtime["credential_scan_python3"]
            entry = runtime["detect_secrets"]
            if not Path(interpreter).is_file() or not Path(entry).is_file():
                raise RuntimeError("declared_credential_runtime_missing")
            records.append(invoke(run, "credential-scan", [interpreter, entry, "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"]))
            if records[-1]["exit"] == 0:
                report = Path(records[-1]["stdout"])
                json.loads(report.read_text())  # actual tool JSON, never manufactured
                records.append(invoke(run, "credential-check", [interpreter, str(ROOT / "tools/check_secret_report.py"), str(report)]))
                text = Path(records[-1]["stdout"]).read_text()
                status = int(records[-1]["exit"] != 0 or "potential secrets: 0;" not in text)
        else:
            records.append(invoke(run, "public-tree", [str(ROOT / ".venv/bin/python3"), str(ROOT / "tools/check_public_tree.py")]))
            status = records[-1]["exit"]
    finally:
        if counts is None:
            counts = {"executed": 0, "skipped": 0, "commands_executed": len(records), "kind": "hygiene_commands_not_unittest"}
        receipt = {"case": args.case, "baseline": args.baseline, "identity": frozen_identity,
                   "launcher_argv": [sys.executable, *sys.argv], "cwd": str(Path.cwd()),
                   "exit": status, "commands": records, "counts": counts,
                   "files": {str(p.relative_to(run)): file_digest(p) for p in sorted(run.rglob("*")) if p.is_file()}}
        if args.baseline:
            receipt["baseline_source_head"] = manifest_doc["head"]
        document(run / "receipt.json", receipt)
        print(json.dumps({"case": args.case, "baseline": args.baseline, "exit": status,
                          "counts": counts, "receipt": str(run / "receipt.json"),
                          "receipt_sha256": file_digest(run / "receipt.json")}, sort_keys=True))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
