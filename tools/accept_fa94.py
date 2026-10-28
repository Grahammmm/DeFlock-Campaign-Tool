"""Fresh file-launched, offline FA94 acceptance with durable raw receipts.

Only the named unittest selectors are loaded. Repository shipping/full discovery
remains the operator's separate gate. Output evidence is never put in git.
"""
import argparse
import hashlib
import inspect
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
CANDIDATE = REPO.parent
SELECTORS = {
    "behavior": "tests.records.test_fa94.Behavior",
    "compatibility": "tests.test_portal_reconcile",
    "negative-control": "tests.records.test_fa94.GateControl",
}


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def offline(root):
    temp = root / "tmp"
    temp.mkdir(exist_ok=True)
    tempfile.tempdir = str(temp)
    sys.dont_write_bytecode = True

    def denied(*args, **kwargs):
        raise RuntimeError("fa94_network_forbidden")

    socket.create_connection = denied
    socket.getaddrinfo = denied
    socket.socket.connect = denied
    socket.socket.connect_ex = denied
    socket.socket.bind = denied

    def audit(event, args):
        if event in {"socket.connect", "socket.bind", "socket.getaddrinfo", "socket.sendto"}:
            denied()
    sys.addaudithook(audit)


def write_json(path, value):
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, sort_keys=True, indent=2)
        handle.write("\n")


def git(*args):
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True, check=True).stdout


def identity():
    manifest = CANDIDATE / "SOURCE-MANIFEST.json"
    files = ["tools/portal_reconcile.py", "tools/accept_fa94.py",
             "tests/records/test_fa94.py", "tests/test_portal_reconcile.py"]
    files += sorted(str(p.relative_to(REPO)) for p in
                    (REPO / "tests/fixtures/portal_reconcile").rglob("*") if p.is_file())
    # Include new/untracked authored files as well as the tracked patch.
    hashes = {name: digest((REPO / name).read_bytes()) for name in files}
    return {"interpreter": sys.executable,
            "interpreter_realpath": str(Path(sys.executable).resolve()),
            "source_manifest_sha256": digest(manifest.read_bytes()),
            "head": git("rev-parse", "HEAD").decode().strip(),
            "dirty_diff_sha256": digest(git("diff", "HEAD", "--binary")),
            "source_sha256": hashes}


def worker(case, root):
    # This runs in a NEW process. Guards and tempfile redirection are installed
    # before importing production code or any test module.
    offline(root)
    sys.path.insert(0, str(REPO))
    from tools import portal_reconcile as portal
    # This producer and its transitive notice/policy/store dependencies have no
    # host-live path constants or bound path defaults. Verify the entry surface.
    if set(inspect.signature(portal.read_json).parameters) != {"path", "schema", "list_key"}:
        raise RuntimeError("unexpected_read_surface")
    if any(p.default is not inspect.Parameter.empty for p in
           inspect.signature(portal.write_new).parameters.values()):
        raise RuntimeError("unexpected_writer_default")
    if any(isinstance(v, Path) for v in vars(portal).values()):
        raise RuntimeError("unexpected_producer_path_global")
    if Path(tempfile.gettempdir()) != root / "tmp":
        raise RuntimeError("temp_boundary_failed")
    try:
        socket.getaddrinfo("offline.example.test", 443)
    except RuntimeError as exc:
        if str(exc) != "fa94_network_forbidden":
            raise
    else:
        raise RuntimeError("network_boundary_failed")
    write_json(root / "boundary.json", {"pre_import_offline_guard": True,
               "temp_root": tempfile.gettempdir(), "live_path_defaults": [],
               "fresh_process_pid": os.getpid()})
    if case == "compatibility":
        # Preserve the pinned selectors/source; redirect their fixture globals
        # and their child CLI entry point to this file-launched guarded entry.
        from tests import test_portal_reconcile as tests
        fixtures = root / "fixtures"
        fixtures.mkdir()
        for name in ("observations.json", "queue-export.json"):
            shutil.copyfile(REPO / "tests/fixtures/portal_reconcile" / name, fixtures / name)
        tests.OBSERVATIONS = fixtures / "observations.json"
        tests.QUEUE = fixtures / "queue-export.json"
        tests.FIXTURES = fixtures
        tests.SCRIPT = Path(__file__).resolve()
        os.environ["FA94_CLI_RUN_ROOT"] = str(root)
    suite = unittest.defaultTestLoader.loadTestsFromName(SELECTORS[case])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    data = {"executed": result.testsRun, "skipped": len(result.skipped),
            "failures": len(result.failures), "errors": len(result.errors),
            "success": result.wasSuccessful(), "selector": SELECTORS[case]}
    write_json(root / "tests.json", data)
    return int(not (data["success"] and data["executed"] > 0 and data["skipped"] == 0))


def run_child(root, label, argv, pin):
    result = subprocess.run(argv, cwd=REPO, capture_output=True)
    stdout, stderr = root / (label + ".stdout"), root / (label + ".stderr")
    stdout.write_bytes(result.stdout)
    stderr.write_bytes(result.stderr)
    receipt = dict(pin, interpreter=argv[0],
                   interpreter_realpath=str(Path(argv[0]).resolve()),
                   argv=argv, cwd=str(REPO), exit=result.returncode,
                   stdout=str(stdout), stderr=str(stderr),
                   stdout_sha256=digest(result.stdout), stderr_sha256=digest(result.stderr))
    tests_path = root / "tests.json"
    if tests_path.exists():
        receipt.update(json.loads(tests_path.read_text()))
    else:
        receipt.update(executed=None, skipped=None)
    write_json(root / (label + ".receipt.json"), receipt)
    print(json.dumps({"label": label, "exit": result.returncode,
                      "receipt": str(root / (label + ".receipt.json"))}))
    return result


def main():
    # Pinned compatibility tests spawn this file with the production CLI args.
    if "--observations" in sys.argv or "--queue-export" in sys.argv or len(sys.argv) == 1:
        root = Path(os.environ["FA94_CLI_RUN_ROOT"])
        offline(root)
        sys.path.insert(0, str(REPO))
        from tools.portal_reconcile import main as portal_main
        return portal_main(sys.argv[1:])
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", choices=[*SELECTORS, "credential-scan", "public-tree"])
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--worker", choices=SELECTORS)
    parser.add_argument("--run-root", type=Path)
    args = parser.parse_args()
    if args.worker:
        return worker(args.worker, args.run_root.resolve())
    if not args.case or args.output_root is None:
        parser.error("case and output root required")
    args.output_root.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="fa94-", dir=args.output_root.resolve()))
    offline(root)
    pin = identity()
    write_json(root / "identity.json", pin)
    if args.case in SELECTORS:
        result = run_child(root, args.case, [sys.executable, str(Path(__file__).resolve()),
                           "--worker", args.case, "--run-root", str(root)], pin)
        return result.returncode
    if args.case == "public-tree":
        return run_child(root, "public-tree", [sys.executable,
                         str(REPO / "tools/check_public_tree.py")], pin).returncode
    manifest = json.loads((CANDIDATE / "SOURCE-MANIFEST.json").read_text())
    runtime = manifest["runtime"]
    interpreter = Path(runtime["credential_scan_python3"])
    scanner = Path(runtime["detect_secrets"])
    if not interpreter.is_file() or not scanner.is_file():
        raise RuntimeError("missing_declared_scanner_runtime")
    scan = run_child(root, "credential-scan", [str(interpreter), str(scanner),
                     "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"], pin)
    if scan.returncode:
        return scan.returncode
    # Validate actual scanner JSON, never synthesize a scan report.
    json.loads(scan.stdout)
    check = run_child(root, "credential-check", [str(interpreter),
                      str(REPO / "tools/check_secret_report.py"),
                      str(root / "credential-scan.stdout")], pin)
    return check.returncode


if __name__ == "__main__":
    raise SystemExit(main())
