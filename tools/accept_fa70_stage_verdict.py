"""Card-specific offline acceptance and retained command receipts for FA70.

No installs, source acquisition, shared-runtime edits or automatic cleanup.
Scoped child guards are sentinels, not a hostile-code sandbox. Full discovery
runs the exact repository command and its existing isolated local facilities.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
OWNED = ("campaign_tool/records/ledger/stages.py",
         "tests/records/test_fa70_stage_verdict.py", "tools/accept_fa70_stage_verdict.py")
REGRESSION = "tests.records.test_fa70_stage_verdict.Regression"
COMPATIBILITY = ("tests.records.test_ledger_stages", "tests.test_secret_report",
                 "tests.test_public_tree")
CHECKS = ("regression", "compatibility", "control", "portability", "full-suite",
          "credentials", "public-tree")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT)


def regular(path):
    require(not any(p.is_symlink() for p in (path, *path.parents)), "symlink evidence path")
    require(path.is_file(), "missing regular file: " + str(path))
    return path


def source_identity():
    names = git("ls-files", "-z", "--cached", "--others", "--exclude-standard").decode().split("\0")
    hashes = {name: digest(regular(ROOT / name).read_bytes()) for name in sorted(set(names) - {""})}
    return {"head": git("rev-parse", "HEAD").decode().strip(),
            "base": git("rev-parse", "HEAD^{tree}").decode().strip(),
            "dirty": git("status", "--porcelain=v1").decode(), "files": hashes,
            "file_map_sha256": digest(json.dumps(hashes, sort_keys=True).encode())}


def child_suite(selectors, module_output, temporary):
    # Happens before any project import. The inspected ledger closure has no
    # ambient corpus/config defaults: paths are mandatory explicit API inputs.
    import socket
    import unittest
    from unittest import mock
    tempfile.tempdir = str(temporary)
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(ROOT))
    def forbidden(*args, **kwargs):
        raise RuntimeError("FA70 offline socket/model sentinel")
    with mock.patch.object(socket, "socket", forbidden), \
         mock.patch.object(socket, "create_connection", forbidden), \
         mock.patch.object(socket, "getaddrinfo", forbidden):
        suite = unittest.defaultTestLoader.loadTestsFromNames(selectors)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    modules = {}
    for name, module in list(sys.modules.items()):
        path = getattr(module, "__file__", None)
        if path and Path(path).resolve().is_relative_to(ROOT):
            source = Path(path).resolve()
            modules[name] = {"path": str(source), "sha256": digest(source.read_bytes())}
    write_json(module_output, {"interpreter": sys.executable, "prefix": sys.prefix,
                              "source_root": str(ROOT), "modules": modules,
                              "tests": result.testsRun, "skips": result.skipped,
                              "failures": [(t.id(), output) for t, output in result.failures],
                              "errors": [(t.id(), output) for t, output in result.errors],
                              "guard": "socket, DNS and connection sentinels before project imports",
                              "path_defaults": "ledger APIs take explicit paths; tempfile redirected before imports"})
    return int(not result.wasSuccessful() or result.testsRun == 0 or bool(result.skipped))


def snapshot(destination, revision, overlay):
    destination.mkdir(mode=0o700)
    raw = git("archive", "--format=tar", revision)
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
        for member in archive:
            relative = Path(member.name)
            require(not relative.is_absolute() and ".." not in relative.parts, "unsafe archive path")
            target = destination / relative
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                require(member.isfile(), "nonregular source archive member")
                target.parent.mkdir(parents=True, exist_ok=True)
                stream = archive.extractfile(member)
                if stream is None:
                    raise ValueError("missing archive member bytes")
                target.write_bytes(stream.read())
                target.chmod(member.mode)
    for name in overlay:
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / name).read_bytes())
    return destination


def execute(run, label, argv, cwd, *, tests=False, scoped=False, expected_red=False):
    temporary = run / (label + "-tmp")
    temporary.mkdir(mode=0o700)
    env = dict(os.environ)
    env.update(TMPDIR=str(temporary), TMP=str(temporary), TEMP=str(temporary),
               PYTHONDONTWRITEBYTECODE="1")
    # No route/credential environment is added; no interpreter path overrides.
    stdout, stderr = run / (label + ".stdout"), run / (label + ".stderr")
    timed_out = False
    with stdout.open("wb") as out, stderr.open("wb") as err:
        try:
            completed = subprocess.run([str(x) for x in argv], cwd=cwd, env=env,
                                       stdout=out, stderr=err, timeout=1800)
            exit_code = completed.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            exit_code = 124
    stdout_bytes, stderr_bytes = stdout.read_bytes(), stderr.read_bytes()
    text = stderr_bytes.decode(errors="replace")
    matches = re.findall(r"Ran (\d+) tests? in", text)
    count = int(matches[-1]) if matches else None
    skips = re.findall(r"skipped=(\d+)", text)
    skipped = int(skips[-1]) if skips else 0
    record = {"argv": [str(x) for x in argv], "cwd": str(cwd), "exit": exit_code,
              "timed_out": timed_out, "test_count": count, "skip_count": skipped,
              "stdout": str(stdout), "stderr": str(stderr),
              "stdout_sha256": digest(stdout_bytes), "stderr_sha256": digest(stderr_bytes)}
    write_json(run / (label + ".command.json"), record)
    if tests:
        require(count is not None and count > 0 and skipped == 0, label + ": zero tests or skips")
    if expected_red:
        require(exit_code == 1, "baseline must expose the genuine verdict defect")
        details = json.loads((run / (label + ".modules.json")).read_text())
        require(not details["failures"] and details["errors"], "unrelated baseline failures")
        expected = {REGRESSION + ".test_invalid_" + stage + "_" + shape
                    for stage in ("preserve", "extract", "catalog", "detect", "review", "compare", "privacy")
                    for shape in ("list", "object", "empty_list", "empty_object", "nested_list", "nested_object")}
        require({t for t, _ in details["errors"]} == expected, "unexpected baseline failing IDs")
        require(all("TypeError: unhashable type:" in error for _, error in details["errors"]),
                "baseline error is not the deciding defect")
    else:
        require(exit_code == 0, label + ": child failed; see retained raw output")
    if scoped:
        details = json.loads((run / (label + ".modules.json")).read_text())
        require(details["modules"] and details["tests"] == count, "missing loaded-module evidence")
        require(all(Path(v["path"]).is_relative_to(cwd) for v in details["modules"].values()),
                "module escaped source prefix")
    return record


def run_scoped(run, label, root, selectors, *, red=False):
    temporary = run / (label + "-fixtures")
    temporary.mkdir(mode=0o700)
    return execute(run, label, [sys.executable, root / OWNED[2], "--internal-suite",
                               "--module-output", run / (label + ".modules.json"),
                               "--temporary", temporary, *selectors], root,
                   tests=True, scoped=True, expected_red=red)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", choices=CHECKS)
    parser.add_argument("--source-manifest", type=Path)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--internal-suite", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--module-output", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--temporary", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("selectors", nargs="*")
    args = parser.parse_args()
    if args.internal_suite:
        return child_suite(args.selectors, args.module_output, args.temporary)
    require(args.check and args.source_manifest and args.output_root, "required acceptance arguments")
    manifest = json.loads(regular(args.source_manifest.absolute()).read_text())
    require(manifest["schema"] == "agentorange.source-manifest/v1", "source schema")
    require(Path(manifest["repo"]).resolve() == ROOT, "source repo mismatch")
    require(Path(manifest["write_root"]).resolve() == ROOT.parent, "write root mismatch")
    require(manifest["clean"] is True and manifest["fixtures_dir"] == "tests/fixtures", "staging fields")
    require(re.fullmatch(r"[0-9a-f]{40}", manifest["head"]) is not None, "base commit")
    require(git("rev-parse", manifest["head"] + "^{tree}").decode().strip() == manifest["tree"], "base tree")
    require(git("merge-base", manifest["head"], "HEAD").decode().strip() == manifest["head"], "base ancestry")
    runtime = manifest["runtime"]
    for key in ("repo_venv", "credential_scan_python3", "detect_secrets"):
        require(Path(runtime[key]).is_absolute() and Path(runtime[key]).exists(), "runtime " + key)
    expected_python = runtime["credential_scan_python3"] if args.check == "credentials" else str(ROOT / ".venv/bin/python3")
    require(Path(sys.executable).absolute() == Path(expected_python).absolute(), "wrong acceptance interpreter")
    require(Path(sys.prefix).resolve() == Path(runtime["repo_venv"]).resolve(), "wrong staged venv")
    output = args.output_root.absolute()
    require(not output.is_relative_to(ROOT) and output.is_relative_to(ROOT.parent), "evidence outside candidate")
    require(not any(p.is_symlink() for p in (output, *output.parents)), "symlink output root")
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    run = Path(tempfile.mkdtemp(prefix=args.check + "-", dir=output))
    print("FA70 evidence: " + str(run), flush=True)
    before = source_identity()
    receipt = {"schema": "fa70-acceptance/v1", "check": args.check,
               "source_manifest_sha256": digest(args.source_manifest.read_bytes()),
               "source_identity": before, "interpreter": sys.executable, "prefix": sys.prefix,
               "argv": sys.argv, "cwd": str(Path.cwd()), "status": "INCOMPLETE"}
    write_json(run / "receipt.json", receipt)
    try:
        if args.check == "regression":
            base = snapshot(run / "pinned-baseline", manifest["head"], OWNED[1:])
            baseline_bytes = (base / OWNED[0]).read_bytes()
            require(baseline_bytes == git("show", manifest["head"] + ":" + OWNED[0]), "baseline source drift")
            receipt["baseline_stage_sha256"] = digest(baseline_bytes)
            receipt["baseline"] = run_scoped(run, "baseline-red", base, [REGRESSION], red=True)
            receipt["candidate"] = run_scoped(run, "candidate", ROOT, [REGRESSION])
        elif args.check == "compatibility":
            base = snapshot(run / "pinned-baseline", manifest["head"], [OWNED[2]])
            receipt["baseline"] = run_scoped(run, "baseline", base, COMPATIBILITY)
            receipt["candidate"] = run_scoped(run, "candidate", ROOT, COMPATIBILITY)
            require(receipt["baseline"]["test_count"] == receipt["candidate"]["test_count"], "compatibility count changed")
        elif args.check == "control":
            receipt["candidate"] = run_scoped(run, "control", ROOT,
                ["tests.records.test_fa70_stage_verdict.GateControl"])
        elif args.check == "portability":
            for name in OWNED[1:]:
                text = (ROOT / name).read_text()
                require(not any(value in text for value in ("/home/" + "user", "agent-" + "work", "other-" + "candidate")),
                        "box-specific committed path")
            second = snapshot(run / "second-source-prefix", "HEAD", OWNED)
            receipt["second_prefix"] = run_scoped(run, "second-prefix", second, [REGRESSION])
        elif args.check == "full-suite":
            receipt["candidate"] = execute(run, "full-suite", [sys.executable, "-m", "unittest",
                "discover", "-s", "tests", "-t", "."], ROOT, tests=True)
        elif args.check == "credentials":
            scan = execute(run, "secret-scan", [runtime["detect_secrets"], "scan", "--all-files",
                "--exclude-files", "^.git/", "--no-verify"], ROOT)
            scan_path = run / "secret-scan.json"
            scan_path.write_bytes(Path(scan["stdout"]).read_bytes())
            data = json.loads(scan_path.read_text())
            require(isinstance(data["results"], dict), "invalid scanner output")
            receipt["scanner"] = scan
            receipt["checker"] = execute(run, "secret-check", [sys.executable,
                ROOT / "tools/check_secret_report.py", scan_path], ROOT)
            text = Path(receipt["checker"]["stdout"]).read_text()
            require("potential secrets: 0;" in text, "unapproved potential secrets")
            receipt["check_count"] = 2
        elif args.check == "public-tree":
            receipt["candidate"] = execute(run, "public-tree", [sys.executable,
                ROOT / "tools/check_public_tree.py"], ROOT)
            report = json.loads(Path(receipt["candidate"]["stdout"]).read_text())
            require(report["scanned_files"] > 0 and report["findings"] == [], "public tree scan")
            receipt["check_count"] = report["scanned_files"]
        require(source_identity() == before, "source changed during acceptance")
        receipt["status"] = "PASS_REVIEW_REQUIRED"
        receipt["exit"] = 0
    except Exception as exc:
        receipt["status"] = "FAILED"
        receipt["error"] = str(exc)
        receipt["exit"] = 1
    write_json(run / "receipt.json", receipt)
    print(json.dumps({"status": receipt["status"], "receipt": str(run / "receipt.json"),
                      "error": receipt.get("error")}, sort_keys=True))
    return receipt["exit"]


if __name__ == "__main__":
    raise SystemExit(main())
