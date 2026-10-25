"""Acceptance runner for card t_177ff79b (historical coverage checker, M-26).

Modes: focused, negative, portability, hygiene. Each mode allocates a fresh
unique directory below --output-root and records argv, cwd, exit, check
counts, module origins, staged HEAD and raw output there.

Guards are installed before any candidate import: socket and DNS calls raise,
subprocess creation is limited to the exact hygiene commands this runner
builds, and an audit hook refuses file writes outside the run directory.
Exit codes: 0 pass, 1 acceptance failure, 3 INPUT_UNAVAILABLE, 4 guard or
import-origin refusal. Never relies on assert for the exit decision.
"""
import argparse
import ast
import datetime
import hashlib
import importlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import secrets
import sys
import unittest

sys.dont_write_bytecode = True

REPO = Path(__file__).resolve().parents[1]
CARD = "t_177ff79b"
TEST_REL = "tests/records/test_historical_coverage_check.py"
TOOL_REL = "tools/historical_coverage_check.py"
DOC_REL = "docs/HISTORICAL-COVERAGE.md"
RUNNER_REL = "scripts/n3_historical_check_acceptance.py"
FIXTURE_REL = "tests/fixtures/n3_historical_check/scenarios.json"
CARD_FILES = [TOOL_REL, TEST_REL, DOC_REL, RUNNER_REL, FIXTURE_REL]
RETENTION_TEST = "test_historical_only_retained_in_denominator"
RETENTION_MARKER = "RETENTION_DENOMINATOR"
PINNED_ATTACHMENT = "/".join(["", "home", "user", ".hermes", "kanban", "boards", "deflock",
                              "attachments", "t_64ecd488", "mailbox-rebuild-design_2.zip"])


class GuardRefusal(Exception):
    pass


class InputUnavailable(Exception):
    pass


STATE = {"run_dir": None, "allowed_commands": set(), "refusals": []}


def _refuse(what):
    STATE["refusals"].append(what)
    raise GuardRefusal(what)


def install_guards():
    import socket
    import subprocess

    def no_network(*_args, **_kwargs):
        _refuse("network")

    class NoSocket:
        def __init__(self, *_args, **_kwargs):
            _refuse("network")

    socket.socket = NoSocket
    for name in ("create_connection", "getaddrinfo", "gethostbyname", "gethostbyname_ex",
                 "gethostbyaddr", "socketpair", "fromfd"):
        if hasattr(socket, name):
            setattr(socket, name, no_network)

    original_popen = subprocess.Popen

    class GuardedPopen(original_popen):
        def __init__(self, args, *rest, **kwargs):
            key = tuple(str(x) for x in args) if isinstance(args, (list, tuple)) else None
            if key is None or kwargs.get("shell") or key not in STATE["allowed_commands"]:
                _refuse(f"subprocess:{args!r}")
            super().__init__(args, *rest, **kwargs)

    subprocess.Popen = GuardedPopen
    for name in ("system", "popen", "execv", "execve", "execl", "execlp", "execvp", "execvpe",
                 "spawnv", "spawnve", "posix_spawn", "posix_spawnp", "fork", "forkpty"):
        if hasattr(os, name):
            setattr(os, name, no_network)

    write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC

    def inside_run_dir(path):
        run_dir = STATE["run_dir"]
        if run_dir is None:
            return False
        try:
            target = Path(os.fsdecode(path))
        except TypeError:
            return False
        if not target.is_absolute():
            target = Path.cwd() / target
        target = Path(os.path.normpath(str(target)))
        return target == run_dir or run_dir in target.parents

    def hook(event, args):
        if event == "open":
            path, mode, flags = args
            writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or \
                (isinstance(flags, int) and flags & write_flags)
            if writing and isinstance(path, (str, bytes, os.PathLike)) and not inside_run_dir(path):
                _refuse(f"write-outside-run-dir:{path!r}")
        elif event in ("os.remove", "os.rmdir", "os.rename", "os.symlink", "os.link",
                       "os.truncate", "os.chmod", "os.mkdir", "shutil.rmtree", "shutil.move"):
            path = args[0] if args else None
            if event == "os.chmod" and isinstance(path, int):
                return
            if not (isinstance(path, (str, bytes, os.PathLike)) and inside_run_dir(path)):
                _refuse(f"{event}-outside-run-dir:{path!r}")
            if event in ("os.rename", "os.link", "os.symlink") and len(args) > 1:
                if not inside_run_dir(args[1]):
                    _refuse(f"{event}-outside-run-dir:{args[1]!r}")
            if event in ("os.remove", "os.rmdir", "shutil.rmtree"):
                _refuse(f"{event}:no deletion in acceptance")
        elif event in ("socket.connect", "socket.getaddrinfo", "socket.gethostbyname",
                       "socket.sendto", "socket.bind"):
            _refuse(f"network:{event}")
        elif event in ("os.system", "os.exec", "os.posix_spawn", "os.spawn", "os.fork"):
            _refuse(f"process:{event}")

    sys.addaudithook(hook)


def allocate_run_dir(output_root, mode):
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    name = f"{mode}-{stamp}-{os.getpid()}-{secrets.token_hex(6)}"
    run_dir = output_root / name
    os.mkdir(run_dir, 0o700)
    return run_dir


def staged_head():
    git = REPO / ".git"
    try:
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref: "):
            ref = head[5:]
            loose = git / ref
            if loose.is_file():
                return {"ref": ref, "sha": loose.read_text(encoding="utf-8").strip()}
            packed = git / "packed-refs"
            if packed.is_file():
                for line in packed.read_text(encoding="utf-8").splitlines():
                    parts = line.split(" ")
                    if len(parts) == 2 and parts[1] == ref:
                        return {"ref": ref, "sha": parts[0]}
            return {"ref": ref, "sha": None}
        return {"ref": None, "sha": head}
    except OSError as error:
        return {"ref": None, "sha": None, "error": type(error).__name__}


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def module_origins(prefixes):
    origins = {}
    for name, module in sorted(sys.modules.items()):
        if any(name == p or name.startswith(p + ".") for p in prefixes):
            origins[name] = getattr(module, "__file__", None)
    return origins


def check_origins(origins, root):
    bad = {}
    for name, origin in origins.items():
        if origin is None:
            continue
        path = Path(origin).resolve()
        if not (path == root or root in path.parents):
            bad[name] = origin
    if bad:
        raise GuardRefusal(f"ambient import outside candidate root: {bad}")


def configure_import_root(root, record):
    """Put the runner-resolved candidate root first on sys.path before candidate imports.

    No environment variable is consulted. When a relocated root is active, the
    staged checkout entry is taken off sys.path so it cannot satisfy an import.
    """
    root = Path(root).resolve()
    entry = str(root)
    others = {str(REPO)} if root != REPO else set()
    sys.path[:] = [p for p in sys.path if p != entry and p not in others]
    sys.path.insert(0, entry)
    setup = {"root": entry, "sys_path_0": sys.path[0],
             "root_first_on_sys_path": sys.path[0] == entry,
             "staged_repo_absent_when_relocated": not (others & set(sys.path)),
             "environment_override_used": False}
    record.setdefault("import_path_setup", []).append(setup)
    if not (setup["root_first_on_sys_path"] and setup["staged_repo_absent_when_relocated"]):
        raise GuardRefusal(f"candidate root not configured on sys.path: {setup}")
    return setup


def import_card_test(root, module_name, record):
    path = root / TEST_REL
    if not path.is_file():
        raise InputUnavailable(f"missing card test file {TEST_REL}")
    setup = configure_import_root(root, record)
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    setup["root_first_after_import"] = sys.path[0] == setup["root"]
    if not setup["root_first_after_import"]:
        raise GuardRefusal(f"sys.path changed during candidate import: {sys.path[0]!r}")
    return module


def import_path_ok(record, root):
    """Runtime assertion that the active root was configured and is still first."""
    entry = str(Path(root).resolve())
    setups = record.get("import_path_setup") or []
    ok = bool(setups) and all(s["root"] == entry and s["root_first_on_sys_path"]
                              and s["staged_repo_absent_when_relocated"]
                              and s.get("root_first_after_import") for s in setups)
    record["import_path_final_sys_path_0"] = sys.path[0]
    return ok and sys.path[0] == entry


def run_suite(suite, stream):
    runner = unittest.TextTestRunner(stream=stream, verbosity=2)
    return runner.run(suite)


def result_summary(result):
    return {"tests_run": result.testsRun, "failures": len(result.failures),
            "errors": len(result.errors), "skipped": len(result.skipped),
            "expected_failures": len(result.expectedFailures),
            "unexpected_successes": len(result.unexpectedSuccesses),
            "successful": result.wasSuccessful()}


def prepare_scratch(run_dir):
    scratch = run_dir / "scratch"
    os.mkdir(scratch, 0o700)
    tmp = run_dir / "tmp"
    os.mkdir(tmp, 0o700)
    import tempfile
    tempfile.tempdir = str(tmp)
    os.environ["N3_HC_SCRATCH"] = str(scratch)
    return scratch


def card_origins(module):
    origins = module_origins(["campaign_tool", "tests", "historical_coverage_check"])
    origins.update({module.__name__: module.__file__,
                    "tool": str(Path(module.hcc.__file__).resolve())})
    return origins


def mode_focused(run_dir, stream, record):
    prepare_scratch(run_dir)
    module = import_card_test(REPO, "n3_hc_card_test", record)
    origins = card_origins(module)
    record["module_origins"] = origins
    check_origins(origins, REPO)
    suite = unittest.defaultTestLoader.loadTestsFromModule(module)
    result = run_suite(suite, stream)
    summary = result_summary(result)
    record["result"] = summary
    record["checks_executed"] = summary["tests_run"]
    record["criteria"] = {"successful": summary["successful"], "at_least_8_tests": summary["tests_run"] >= 8,
                          "no_skips": summary["skipped"] == 0,
                          "candidate_root_on_sys_path": import_path_ok(record, REPO)}
    return 0 if all(record["criteria"].values()) else 1


def mode_negative(run_dir, stream, record):
    prepare_scratch(run_dir)
    module = import_card_test(REPO, "n3_hc_card_test", record)
    origins = card_origins(module)
    record["module_origins"] = origins
    check_origins(origins, REPO)
    tool = module.hcc
    if not callable(getattr(tool, "historical_denominator", None)):
        record["criteria"] = {"seam_present": False}
        return 1
    original = tool.historical_denominator

    def mutant_drops_historical_only(counts):
        return counts["changed"] + counts["unchanged"]

    loader = unittest.defaultTestLoader
    test_class = module.HistoricalCoverageCheckTest
    stream.write("== mutant: historical-only occurrences dropped from the denominator ==\n")
    tool.historical_denominator = mutant_drops_historical_only
    try:
        mutant = run_suite(loader.loadTestsFromName(RETENTION_TEST, test_class), stream)
    finally:
        tool.historical_denominator = original
    failure_text = "\n".join(text for _case, text in mutant.failures)
    mutant_ok = (mutant.testsRun == 1 and len(mutant.failures) == 1 and not mutant.errors
                 and RETENTION_MARKER in failure_text)
    stream.write("== restored: retention test ==\n")
    restored = run_suite(loader.loadTestsFromName(RETENTION_TEST, test_class), stream)
    stream.write("== restored: full card suite ==\n")
    full = run_suite(loader.loadTestsFromModule(module), stream)
    record["mutant"] = result_summary(mutant)
    record["mutant_failed_for_marker"] = RETENTION_MARKER in failure_text
    record["restored_retention"] = result_summary(restored)
    record["restored_full"] = result_summary(full)
    record["seam_restored"] = tool.historical_denominator is original
    record["checks_executed"] = mutant.testsRun + restored.testsRun + full.testsRun
    record["criteria"] = {"mutant_failed_for_exact_reason": mutant_ok,
                          "restored_retention_passed": restored.wasSuccessful() and restored.testsRun == 1,
                          "restored_full_passed": full.wasSuccessful() and full.testsRun >= 8,
                          "seam_restored": record["seam_restored"],
                          "candidate_root_on_sys_path": import_path_ok(record, REPO)}
    return 0 if all(record["criteria"].values()) else 1


def box_path_hits(root):
    prefixes = ["/" + "home" + "/", "/" + "Users" + "/", "/" + "root" + "/", "/" + "tmp" + "/",
                "/" + "var" + "/", "/" + "mnt" + "/", "/" + "opt" + "/", "/" + "private" + "/"]
    hits = []
    for rel in CARD_FILES:
        path = root / rel
        if not path.is_file():
            hits.append({"file": rel, "line": 0, "reason": "missing"})
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for prefix in prefixes:
                if prefix in line:
                    allowed = rel == DOC_REL and PINNED_ATTACHMENT in line and \
                        line.count(prefix) == line.count(PINNED_ATTACHMENT)
                    if not allowed:
                        hits.append({"file": rel, "line": number, "reason": prefix})
    return hits


def mode_portability(run_dir, stream, record):
    scratch = prepare_scratch(run_dir)
    relocated = run_dir / f"relocated-{secrets.token_hex(4)}" / f"alt-checkout-{secrets.token_hex(4)}"
    for rel in (TOOL_REL, TEST_REL, FIXTURE_REL):
        source = REPO / rel
        if not source.is_file():
            raise InputUnavailable(f"missing card file {rel}")
        target = relocated / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
    home = run_dir / f"synthetic-home-{secrets.token_hex(4)}"
    os.mkdir(home, 0o700)
    os.environ["HOME"] = str(home)
    os.chdir(scratch)
    module = import_card_test(relocated, "n3_hc_relocated_test", record)
    tool_origin = Path(module.hcc.__file__).resolve()
    record["module_origins"] = {"n3_hc_relocated_test": module.__file__, "tool": str(tool_origin)}
    check_origins(record["module_origins"], relocated)
    result = run_suite(unittest.defaultTestLoader.loadTestsFromModule(module), stream)
    summary = result_summary(result)
    hits = box_path_hits(REPO)
    first, second = allocate_run_dir(run_dir, "alloc"), allocate_run_dir(run_dir, "alloc")
    tool_text = (REPO / TOOL_REL).read_text(encoding="utf-8")
    record["result"] = summary
    record["relocated_root"] = str(relocated)
    record["synthetic_home"] = str(home)
    record["box_path_hits"] = hits
    record["allocations"] = [first.name, second.name]
    record["criteria"] = {
        "relocated_suite_passed": summary["successful"] and summary["tests_run"] >= 8,
        "tool_loaded_from_relocated_root": relocated in tool_origin.parents,
        "relocated_root_on_sys_path": import_path_ok(record, relocated),
        "home_is_synthetic": os.path.expanduser("~") == str(home),
        "tool_reads_no_home": "expanduser" not in tool_text and "HOME" not in tool_text,
        "no_box_paths": not hits,
        "unique_allocation": first != second and first.is_dir() and second.is_dir(),
    }
    record["checks_executed"] = summary["tests_run"] + len(CARD_FILES) + 5
    return 0 if all(record["criteria"].values()) else 1


NETWORK_MODULES = {"socket", "ssl", "http", "urllib", "ftplib", "smtplib", "imaplib", "poplib",
                   "telnetlib", "requests", "httpx", "asyncio", "xmlrpc", "socketserver"}
PROCESS_MODULES = {"subprocess", "multiprocessing", "pty", "concurrent"}
FORBIDDEN_CALLS = {"remove", "unlink", "rmdir", "rmtree", "rename", "replace", "system", "popen",
                   "symlink", "chown", "truncate", "removedirs", "move"}


def static_scan(rel, allowed_write_functions):
    tree = ast.parse((REPO / rel).read_text(encoding="utf-8"), rel)
    findings = []
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    def enclosing_function(node):
        while node in parents:
            node = parents[node]
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return node.name
        return None

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [(node.module or "").split(".")[0]]
        else:
            names = []
        for name in names:
            if name in NETWORK_MODULES or name in PROCESS_MODULES:
                findings.append(f"{rel}:{node.lineno}: import {name}")
        if isinstance(node, ast.Call):
            func = node.func
            attr = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else None
            if attr in FORBIDDEN_CALLS:
                findings.append(f"{rel}:{node.lineno}: call {attr}")
            writes = attr in ("write_text", "write_bytes", "mkdir", "makedirs", "symlink_to", "touch")
            if attr == "open":
                mode = None
                if isinstance(func, ast.Name) and len(node.args) > 1:
                    mode = node.args[1]
                for keyword in node.keywords:
                    if keyword.arg == "mode":
                        mode = keyword.value
                if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "os":
                    writes = len(node.args) > 1 and "O_CREAT" in ast.dump(node.args[1])
                elif mode is not None:
                    writes = not (isinstance(mode, ast.Constant) and set(str(mode.value)) <= set("rb"))
            if writes and enclosing_function(node) not in allowed_write_functions:
                findings.append(f"{rel}:{node.lineno}: write call {attr} outside {sorted(allowed_write_functions)}")
    return findings


def mode_hygiene(run_dir, stream, record):
    for rel in CARD_FILES:
        if not (REPO / rel).is_file():
            raise InputUnavailable(f"missing card file {rel}")
    findings = static_scan(TOOL_REL, {"write_new_private"}) + static_scan(TEST_REL, {"_put"})
    tool_text = (REPO / TOOL_REL).read_text(encoding="utf-8")
    if tool_text.count("O_CREAT") != 1:
        findings.append(f"{TOOL_REL}: expected exactly one O_CREAT site, found {tool_text.count('O_CREAT')}")
    if "O_EXCL" not in tool_text or "O_NOFOLLOW" not in tool_text:
        findings.append(f"{TOOL_REL}: output must be created with O_EXCL and O_NOFOLLOW")
    stream.write("static findings: %d\n" % len(findings))
    for line in findings:
        stream.write(line + "\n")
    detect = Path(sys.executable).parent / "detect-secrets"
    if not detect.is_file():
        raise InputUnavailable("detect-secrets (module detect_secrets) next to the interpreter")
    import subprocess
    report = run_dir / "secret-scan.json"
    scan = (str(detect), "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify")
    secret_check = (sys.executable, "-B", str(REPO / "tools" / "check_secret_report.py"), str(report))
    public_check = (sys.executable, "-B", str(REPO / "tools" / "check_public_tree.py"))
    STATE["allowed_commands"].update({scan, secret_check, public_check})
    commands = []
    child_env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "LD_"))}
    child_env["TMPDIR"] = str(run_dir)
    with report.open("x", encoding="utf-8") as handle:
        scanned = subprocess.run(list(scan), cwd=REPO, stdout=handle, stderr=subprocess.PIPE,
                                 text=True, timeout=1500, env=child_env)
    commands.append({"argv": list(scan), "cwd": str(REPO), "exit": scanned.returncode,
                     "stderr_tail": scanned.stderr[-2000:], "stdout_file": report.name,
                     "stdout_sha256": sha256_file(report)})
    outcomes = {"detect_secrets_scan": scanned.returncode == 0}
    for key, argv in (("check_secret_report", secret_check), ("check_public_tree", public_check)):
        done = subprocess.run(list(argv), cwd=REPO, capture_output=True, text=True, timeout=900, env=child_env)
        stream.write(f"$ {' '.join(argv)}\nexit {done.returncode}\n{done.stdout}{done.stderr}\n")
        commands.append({"argv": list(argv), "cwd": str(REPO), "exit": done.returncode,
                         "stdout": done.stdout[-4000:], "stderr": done.stderr[-4000:]})
        outcomes[key] = done.returncode == 0
    record["commands"] = commands
    record["static_findings"] = findings
    record["criteria"] = {"static_no_network_no_stray_writes": not findings, **outcomes}
    record["checks_executed"] = 2 + 2 + len(commands)
    return 0 if all(record["criteria"].values()) else 1


MODES = {"focused": mode_focused, "negative": mode_negative,
         "portability": mode_portability, "hygiene": mode_hygiene}


def probe_guards(run_dir):
    """Synthetic self-check that the guards refuse; probes stay inside the output root."""
    import socket
    results = {}
    before = len(STATE["refusals"])
    try:
        socket.getaddrinfo("guard-probe.invalid", 1)
        results["dns_refused"] = False
    except GuardRefusal:
        results["dns_refused"] = True
    target = run_dir.parent / f"guard-probe-{secrets.token_hex(6)}.txt"
    try:
        with open(target, "x", encoding="utf-8") as handle:
            handle.write("guard probe\n")
        results["outside_write_refused"] = False
    except GuardRefusal:
        results["outside_write_refused"] = not target.exists()
    del STATE["refusals"][before:]
    return results


def main(argv):
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--mode", required=True, choices=sorted(MODES))
    parser.add_argument("--output-root", required=True)
    args = parser.parse_args(argv)
    output_root = Path(args.output_root)
    if not output_root.is_absolute():
        print("output root must be absolute", file=sys.stderr)
        return 1
    output_root.mkdir(parents=True, exist_ok=True)
    output_root = output_root.resolve()
    run_dir = allocate_run_dir(output_root, args.mode)
    STATE["run_dir"] = run_dir
    install_guards()
    guard_probe = probe_guards(run_dir)
    record = {"card": CARD, "mode": args.mode, "argv": [sys.executable, *sys.argv],
              "cwd": os.getcwd(), "interpreter": sys.executable, "python": sys.version.split()[0],
              "repo": str(REPO), "staged_head": staged_head(), "run_dir": str(run_dir),
              "card_file_sha256": {rel: sha256_file(REPO / rel) for rel in CARD_FILES
                                   if (REPO / rel).is_file()}}
    stream = io.StringIO()
    record["guard_probe"] = guard_probe
    try:
        if not all(guard_probe.values()):
            raise GuardRefusal(f"guards not effective: {guard_probe}")
        code = MODES[args.mode](run_dir, stream, record)
    except InputUnavailable as error:
        stream.write(f"INPUT_UNAVAILABLE: {error}\n")
        record["input_unavailable"] = str(error)
        code = 3
    except GuardRefusal as error:
        stream.write(f"GUARD_REFUSAL: {error}\n")
        record["guard_refusal"] = str(error)
        code = 4
    except Exception as error:  # recorded failure, never a pass
        import traceback
        stream.write("UNEXPECTED_ERROR:\n" + traceback.format_exc())
        record["unexpected_error"] = f"{type(error).__name__}: {error}"
        code = 1
    if STATE["refusals"] and code == 0:
        code = 4
    record["guard_refusals"] = list(STATE["refusals"])
    raw = stream.getvalue()
    raw_path = run_dir / "raw-output.txt"
    raw_path.write_text(raw, encoding="utf-8")
    record["raw_output_sha256"] = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    record["exit"] = code
    record.setdefault("checks_executed", 0)
    if code == 0 and not record["checks_executed"]:
        code = record["exit"] = 1
    (run_dir / "receipt.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    sys.stdout.write(raw)
    summary = {"mode": args.mode, "exit": code, "checks_executed": record["checks_executed"],
               "criteria": record.get("criteria"), "run_dir": str(run_dir),
               "receipt_sha256": sha256_file(run_dir / "receipt.json")}
    print(json.dumps(summary, sort_keys=True))
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
