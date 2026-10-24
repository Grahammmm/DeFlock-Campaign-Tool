#!/usr/bin/env python3
"""FC-DOC-COVERAGE acceptance runner (machine checks only; REVIEW REQUIRED).

Six independent checks: focused, compatibility, negative, portability,
credentials, public-tree. Every invocation writes a freshly created, uniquely
named run directory under --output-root with the exact child argv/cwd/exit,
raw stdout/stderr, test counts, candidate HEAD and source hashes. Children are
argument arrays without a shell. The repository is derived from this file.
A pass is machine validity only; it is never semantic or legal acceptance.
"""
from __future__ import annotations

import argparse
import ast
import datetime
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SPEC = "FC-DOC-COVERAGE acceptance runner v1"
CHECKS = ("focused", "compatibility", "negative", "portability", "credentials", "public-tree")
FOCUSED_MODULE = "tests.records.test_fc_doc_coverage"
FOCUSED_FILE = "tests/records/test_fc_doc_coverage.py"
COMPAT_TARGET = "tests.records.test_understanding.DocumentTests"
COMPAT_FILE = "tests/records/test_understanding.py"
SOURCES = ("campaign_tool/records/understanding/document.py", FOCUSED_FILE, "tools/accept_fc_doc_coverage.py")
NEGATIVE_FAIL = "test_one_of_two_pages_is_partial"
NEGATIVE_PASS = "test_complete_two_page_inventory_is_clean"
CHILD_TIMEOUT = 900
# Fragments are joined at run time so this file does not match its own scan.
FORBIDDEN_FRAGMENTS = (("/ho", "me/"), ("agent", "-work"), ("venv-", "fullsuite"), ('"-', 'c"'), ("'-", "c'"),
                       ('"-', 'e"'), ("shell", "=True"), ("PYTHON", "PATH"), ("os.sys", "tem("), ("os.po", "pen("),
                       ("bash", " -c"), ("<<", "EOF"))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str | None:
    return sha256_bytes(path.read_bytes()) if path.is_file() else None


def run_child(name: str, argv: list, cwd: Path, run_dir: Path, stdout_path: Path | None = None) -> dict:
    """Run one argument-array child (no shell) and preserve its raw output."""
    out_path = stdout_path or run_dir / f"{name}.stdout"
    err_path = run_dir / f"{name}.stderr"
    record = {"name": name, "argv": [str(a) for a in argv], "cwd": str(cwd)}
    try:
        with open(out_path, "xb") as out, open(err_path, "xb") as err:
            proc = subprocess.run([str(a) for a in argv], cwd=str(cwd), stdin=subprocess.DEVNULL,
                                  stdout=out, stderr=err, timeout=CHILD_TIMEOUT, check=False)
        record["exit"] = proc.returncode
    except (OSError, subprocess.SubprocessError) as exc:
        record["exit"] = None
        record["launch_error"] = f"{type(exc).__name__}: {exc}"
    record["stdout_path"] = str(out_path)
    record["stderr_path"] = str(err_path)
    record["stdout_sha256"] = sha256_file(out_path)
    record["stderr_sha256"] = sha256_file(err_path)
    return record


_RESULT_LINE = re.compile(r"^(\S+) \((\S+)\)(?:\n.*?)? \.\.\. (ok|FAIL|ERROR|skipped.*|expected failure|unexpected success)$",
                          re.M)


def parse_unittest(text: str) -> dict:
    ran = re.findall(r"^Ran (\d+) tests? in ", text, re.M)
    final = [line for line in text.splitlines() if re.match(r"^(OK|FAILED)\b", line)]
    skipped = re.search(r"skipped=(\d+)", final[-1]) if final else None
    tests = [{"test": m.group(2), "outcome": m.group(3)} for m in _RESULT_LINE.finditer(text)]
    return {"ran": int(ran[-1]) if ran else None, "final": final[-1] if final else None,
            "skipped": int(skipped.group(1)) if skipped else 0,
            "failure_headers": re.findall(r"^(?:FAIL|ERROR): (.+)$", text, re.M), "tests": tests}


def defined_tests(path: Path, class_name: str | None = None) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and (class_name is None or node.name == class_name):
            names += [f"{node.name}.{f.name}" for f in node.body
                      if isinstance(f, ast.FunctionDef) and f.name.startswith("test")]
    return names


def unittest_check(run_dir: Path, name: str, target: str, expected: list[str]) -> dict:
    child = run_child(name, [sys.executable, "-m", "unittest", "-v", target], REPO, run_dir)
    text = Path(child["stderr_path"]).read_text(encoding="utf-8", errors="replace")
    parsed = parse_unittest(text)
    problems = []
    if child["exit"] != 0:
        problems.append(f"exit {child['exit']}")
    if not parsed["ran"]:
        problems.append("no tests executed")
    if parsed["ran"] != len(expected):
        problems.append(f"executed {parsed['ran']} tests, defined {len(expected)}")
    if parsed["skipped"]:
        problems.append(f"skipped {parsed['skipped']}")
    if not (parsed["final"] or "").startswith("OK"):
        problems.append(f"final line {parsed['final']!r}")
    bad = [t for t in parsed["tests"] if t["outcome"] != "ok"]
    if bad:
        problems.append(f"non-ok results {bad}")
    return {"children": [child], "unittest": parsed, "expected_tests": expected, "problems": problems}


def check_focused(run_dir: Path) -> dict:
    return unittest_check(run_dir, "focused-unittest", FOCUSED_MODULE, defined_tests(REPO / FOCUSED_FILE))


def check_compatibility(run_dir: Path) -> dict:
    return unittest_check(run_dir, "compatibility-unittest", COMPAT_TARGET,
                          defined_tests(REPO / COMPAT_FILE, "DocumentTests"))


def _run_named(module, names: list[str], run_dir: Path, label: str) -> unittest.TestResult:
    suite = unittest.TestSuite(module.FcDocCoverageTests(n) for n in names)
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    with open(run_dir / f"{label}.txt", "x", encoding="utf-8") as fh:
        fh.write(stream.getvalue())
    return result


def _ids(items) -> list[str]:
    return sorted(test.id().rsplit(".", 1)[-1] for test, _ in items)


def check_negative(run_dir: Path) -> dict:
    """In-memory synthetic mutant of the count-versus-inventory predicate."""
    sys.path.insert(0, str(REPO))
    document_path = REPO / SOURCES[0]
    before = sha256_file(document_path)
    from campaign_tool.records.understanding import document
    import importlib
    tests = importlib.import_module(FOCUSED_MODULE)
    problems, phases = [], {}
    loaded = {"document": document.__file__, "tests": tests.__file__}
    for key, value in loaded.items():
        if not Path(value).resolve().is_relative_to(REPO):
            problems.append(f"{key} loaded outside candidate: {value}")
    original = document._missing_page_count

    def mutant(declared, in_range):
        # Synthetic bypass: never report a shortfall against the declared total.
        return None if declared is None else 0

    document._missing_page_count = mutant
    try:
        res = _run_named(tests, [NEGATIVE_FAIL, NEGATIVE_PASS], run_dir, "negative-mutant")
    finally:
        document._missing_page_count = original
    phases["mutant"] = {"ran": res.testsRun, "failures": _ids(res.failures), "errors": _ids(res.errors),
                        "skipped": len(res.skipped)}
    if phases["mutant"]["failures"] != [NEGATIVE_FAIL] or phases["mutant"]["errors"] or res.testsRun != 2:
        problems.append(f"mutant not detected exactly: {phases['mutant']}")
    if document._missing_page_count is not original:
        problems.append("predicate not restored")
    res = _run_named(tests, [NEGATIVE_FAIL, NEGATIVE_PASS], run_dir, "negative-restored")
    phases["restored"] = {"ran": res.testsRun, "failures": _ids(res.failures), "errors": _ids(res.errors),
                          "skipped": len(res.skipped)}
    if not res.wasSuccessful() or res.testsRun != 2 or res.skipped:
        problems.append(f"restored positive not green: {phases['restored']}")
    full = unittest.defaultTestLoader.loadTestsFromModule(tests)
    stream = io.StringIO()
    res = unittest.TextTestRunner(stream=stream, verbosity=2).run(full)
    with open(run_dir / "negative-restored-full.txt", "x", encoding="utf-8") as fh:
        fh.write(stream.getvalue())
    phases["restored_full"] = {"ran": res.testsRun, "failures": _ids(res.failures), "errors": _ids(res.errors),
                               "skipped": len(res.skipped)}
    if not res.wasSuccessful() or res.testsRun == 0 or res.skipped:
        problems.append(f"restored full module not green: {phases['restored_full']}")
    after = sha256_file(document_path)
    if before != after:
        problems.append("document.py bytes changed during negative control")
    return {"phases": phases, "loaded": loaded, "document_sha256_before": before,
            "document_sha256_after": after, "problems": problems, "children": []}


def static_inspection() -> dict:
    hits = []
    for rel in (FOCUSED_FILE, "tools/accept_fc_doc_coverage.py"):
        text = (REPO / rel).read_text(encoding="utf-8")
        for a, b in FORBIDDEN_FRAGMENTS:
            if (a + b) in text:
                hits.append({"file": rel, "pattern": a + b})
    return {"files": [FOCUSED_FILE, "tools/accept_fc_doc_coverage.py"], "hits": hits}


def check_portability(run_dir: Path) -> dict:
    problems = []
    inspection = static_inspection()
    if inspection["hits"]:
        problems.append(f"box-specific or inline-interpreter forms: {inspection['hits']}")
    listing = run_child("git-ls-files", ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                        REPO, run_dir)
    children = [listing]
    if listing["exit"] != 0:
        return {"children": children, "problems": problems + ["git ls-files failed"], "inspection": inspection}
    names = [n for n in Path(listing["stdout_path"]).read_bytes().decode("utf-8").split("\0") if n]
    report_path = run_dir / "isolated-report.json"
    copied = 0
    with tempfile.TemporaryDirectory(prefix="portable-copy-", dir=run_dir) as tmp:
        copy_root = Path(tmp) / "relocated-candidate"
        for name in names:
            src = REPO / name
            if not (src.is_file() or src.is_symlink()):
                continue
            dst = copy_root / name
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst, follow_symlinks=False)
            copied += 1
        child = run_child("isolated-focused", [sys.executable, copy_root / "tools" / "accept_fc_doc_coverage.py",
                                               "--isolated-child", report_path], copy_root, run_dir)
        children.append(child)
        report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.is_file() else None
    if child["exit"] != 0:
        problems.append(f"isolated child exit {child['exit']}")
    if not report:
        problems.append("isolated report missing")
    else:
        if report["copy_root"] != str(copy_root.resolve()) or str(REPO) in report["copy_root"]:
            problems.append("isolated child did not run from the relocated copy")
        if report["outside_copy_modules"]:
            problems.append(f"modules loaded outside copy: {report['outside_copy_modules']}")
        if report["sentinel_hits"]:
            problems.append(f"sentinel hits: {report['sentinel_hits']}")
        if not report["ran"] or report["failures"] or report["errors"] or report["skipped"]:
            problems.append(f"isolated focused not green: ran={report['ran']}")
        if report["ran"] != len(defined_tests(REPO / FOCUSED_FILE)):
            problems.append("isolated focused executed count differs from defined tests")
    return {"children": children, "copied_files": copied, "isolated_report": report,
            "inspection": inspection, "problems": problems}


def isolated_child(report_path: Path) -> int:
    """Runs inside the relocated copy: sentinels first, then application import."""
    hits: list[str] = []

    def sentinel(label):
        def refuse(*args, **kwargs):
            hits.append(label)
            raise PermissionError(f"portability sentinel refused {label}")
        return refuse

    import socket
    for attr in ("connect", "connect_ex", "sendto", "bind"):
        setattr(socket.socket, attr, sentinel(f"socket.{attr}"))
    socket.create_connection = sentinel("socket.create_connection")
    socket.getaddrinfo = sentinel("socket.getaddrinfo")
    subprocess.Popen.__init__ = sentinel("subprocess.Popen")
    for attr in ("system", "popen", "fork", "forkpty", "execv", "execve", "execvp", "execvpe", "spawnv",
                 "spawnve", "posix_spawn", "posix_spawnp"):
        if hasattr(os, attr):
            setattr(os, attr, sentinel(f"os.{attr}"))
    copy_root = Path(__file__).resolve().parents[1]
    sys.path[:] = [str(copy_root)] + [p for p in sys.path if p and Path(p).resolve() != copy_root / "tools"]
    import importlib
    tests = importlib.import_module(FOCUSED_MODULE)
    suite = unittest.defaultTestLoader.loadTestsFromModule(tests)
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    modules = {name: getattr(mod, "__file__", None) for name, mod in sorted(sys.modules.items())
               if name.split(".")[0] in ("campaign_tool", "tests")}
    outside = {n: f for n, f in modules.items() if f and not Path(f).resolve().is_relative_to(copy_root)}
    report = {"copy_root": str(copy_root), "ran": result.testsRun, "failures": _ids(result.failures),
              "errors": _ids(result.errors), "skipped": len(result.skipped), "loaded_modules": modules,
              "outside_copy_modules": outside, "sentinel_hits": hits, "unittest_output": stream.getvalue()}
    with open(report_path, "x", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, sort_keys=True)
    ok = result.wasSuccessful() and result.testsRun > 0 and not result.skipped and not outside and not hits
    return 0 if ok else 1


def check_credentials(run_dir: Path) -> dict:
    scanner = Path(sys.executable).parent / "detect-secrets"
    if not scanner.is_file():
        return {"children": [], "problems": [f"detect-secrets not installed beside interpreter: {scanner}"]}
    report = run_dir / "secret-scan.json"
    scan = run_child("detect-secrets-scan", [scanner, "scan", "--all-files", "--exclude-files", "^.git/",
                                             "--no-verify"], REPO, run_dir, stdout_path=report)
    problems = []
    if scan["exit"] != 0:
        problems.append(f"scanner exit {scan['exit']}")
    try:
        data = json.loads(report.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("results"), dict):
            problems.append("secret report malformed")
    except (OSError, ValueError) as exc:
        problems.append(f"secret report unreadable: {exc}")
    check = run_child("check-secret-report", [sys.executable, REPO / "tools" / "check_secret_report.py", report],
                      REPO, run_dir)
    out = Path(check["stdout_path"]).read_text(encoding="utf-8", errors="replace")
    found = re.search(r"potential secrets: (\d+)", out)
    if check["exit"] != 0:
        problems.append(f"check_secret_report exit {check['exit']}")
    if not found:
        problems.append("potential secrets count not reported")
    elif int(found.group(1)) != 0:
        problems.append(f"potential secrets: {found.group(1)}")
    return {"children": [scan, check], "potential_secrets": int(found.group(1)) if found else None,
            "problems": problems}


def check_public_tree(run_dir: Path) -> dict:
    child = run_child("check-public-tree", [sys.executable, REPO / "tools" / "check_public_tree.py"], REPO, run_dir)
    return {"children": [child], "problems": [] if child["exit"] == 0 else [f"exit {child['exit']}"]}


def identity(run_dir: Path) -> dict:
    head = run_child("git-rev-parse", ["git", "rev-parse", "HEAD"], REPO, run_dir)
    status = run_child("git-status", ["git", "status", "--porcelain", "--untracked-files=all"], REPO, run_dir)
    porcelain = Path(status["stdout_path"]).read_text(encoding="utf-8") if status["exit"] == 0 else None
    return {"head": Path(head["stdout_path"]).read_text(encoding="utf-8").strip() if head["exit"] == 0 else None,
            "tree_clean": porcelain == "", "status_porcelain": porcelain, "children": [head, status],
            "source_sha256": {rel: sha256_file(REPO / rel) for rel in SOURCES}}


HANDLERS = {"focused": check_focused, "compatibility": check_compatibility, "negative": check_negative,
            "portability": check_portability, "credentials": check_credentials, "public-tree": check_public_tree}


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[0] == "--isolated-child":
        return isolated_child(Path(argv[1]))
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", required=True, choices=CHECKS)
    parser.add_argument("--output-root", required=True, type=Path)
    args = parser.parse_args(argv)
    output_root = args.output_root.resolve()
    if output_root.is_relative_to(REPO):
        print("refused: --output-root must be outside the candidate repository", file=sys.stderr)
        return 2
    output_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    run_dir = output_root / f"{args.check}-{stamp}-{os.getpid()}-{secrets.token_hex(4)}"
    run_dir.mkdir(exist_ok=False)
    record = {"spec": SPEC, "check": args.check, "argv": [sys.executable, str(Path(__file__).resolve()), *argv],
              "cwd": os.getcwd(), "interpreter": sys.executable, "python_version": sys.version,
              "repo": str(REPO), "run_dir": str(run_dir), "verdict_scope": "MACHINE CHECK ONLY - REVIEW REQUIRED",
              "started_utc": stamp}
    try:
        record["identity_before"] = identity(run_dir)
        record["result"] = HANDLERS[args.check](run_dir)
        record["identity_after"] = {k: v for k, v in identity(run_dir).items() if k != "children"}
        problems = list(record["result"]["problems"])
        if record["identity_before"]["source_sha256"] != record["identity_after"]["source_sha256"]:
            problems.append("candidate source changed during check")
    except Exception as exc:  # incomplete check is a failure, never a pass
        problems = [f"incomplete: {type(exc).__name__}: {exc}"]
    record["problems"] = problems
    record["passed"] = not problems
    with open(run_dir / "result.json", "x", encoding="utf-8") as fh:
        json.dump(record, fh, indent=2, sort_keys=True, default=str)
    print(json.dumps({"check": args.check, "passed": record["passed"], "problems": problems,
                      "run_dir": str(run_dir), "head": record.get("identity_before", {}).get("head"),
                      "result_sha256": sha256_file(run_dir / "result.json")}, sort_keys=True))
    return 0 if record["passed"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
