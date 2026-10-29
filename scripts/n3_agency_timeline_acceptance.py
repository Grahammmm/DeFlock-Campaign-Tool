"""Acceptance runner for N3-AGENCY-TIMELINE (offline, synthetic, REVIEW REQUIRED).

Usage (one mode per invocation):
    <interpreter> scripts/n3_agency_timeline_acceptance.py --mode MODE --output-root DIR

MODE is focused, compatibility, negative, portability or hygiene. DIR must be
absolute, link-free and outside this repository. Every invocation creates a
fresh unique child directory below DIR recording argv, cwd, interpreter, exit,
check/test counts, module origins, staged HEAD and raw output.

Socket/DNS and subprocess guard seams are installed before any candidate import;
only runner-owned children (git, guarded compatibility children, the relocated
focused child, the CLI child and the two repository scanners) are launched, as
argument arrays without a shell. The repository root is derived from this
file's path and inserted into sys.path here; an ambient checkout import fails.
Exit status is computed from explicit checks (never from assert): 0 all checks
passed, 1 a check failed, 2 refused arguments/root, 3 INPUT_UNAVAILABLE.

The internal entry ``--compat-child MODULE`` is launched only by the
compatibility mode and accepts only the two pinned compatibility modules.
A passing result is machine validity only, never semantic or release acceptance.
"""
import sys

sys.dont_write_bytecode = True

import argparse  # noqa: E402
import ast  # noqa: E402
import datetime  # noqa: E402
import hashlib  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
from pathlib import Path  # noqa: E402
import secrets  # noqa: E402
import socket  # noqa: E402
import subprocess  # noqa: E402
import threading  # noqa: E402
import unittest  # noqa: E402

RUNNER = Path(__file__).resolve()
REPO = RUNNER.parents[1]
MODES = ("focused", "compatibility", "negative", "portability", "hygiene")
TEST_MODULE = "tests.records.test_agency_timeline"
NEGATIVE_TEST = TEST_MODULE + ".ReadOnlyTests.test_ledger_open_has_no_write_capability"
NEGATIVE_REASON = "write_capability_present"
MODULE_REL = "campaign_tool/records/understanding/agency_timeline.py"
TOOL_REL = "tools/agency_timeline.py"
TEST_REL = "tests/records/test_agency_timeline.py"
RUNNER_REL = "scripts/n3_agency_timeline_acceptance.py"
DOC_REL = "docs/AGENCY-TIMELINE.md"
FIXTURE_REL = ("tests/fixtures/n3_agency_timeline/ledger_spec.json",
               "tests/fixtures/n3_agency_timeline/expected_table.txt")
DELIVERABLES = (MODULE_REL, TOOL_REL, TEST_REL, RUNNER_REL, DOC_REL, *FIXTURE_REL)
COMPAT_FILES = ("tests/records/test_correspondence.py", "tests/records/test_ledger.py")
COMPAT_MODULES = ("tests.records.test_correspondence", "tests.records.test_ledger")
PIN_PREFIX = "608dfc54"
SUPPORT_FILES = ("tests/__init__.py", "tests/records/__init__.py")
# Assembled from parts so the list does not match its own source text.
BOX_SPECIFIC = ("/ho" + "me/", "/Us" + "ers/", "agent" + "-work", ".her" + "mes", "venv" + "-full")
NETWORK_MODULES = {"socket", "ssl", "urllib", "http", "requests", "smtplib", "imaplib", "ftplib",
                   "poplib", "telnetlib", "asyncio", "subprocess"}
EXIT_PASS, EXIT_FAIL, EXIT_REFUSED, EXIT_INPUT = 0, 1, 2, 3


class RootRefused(Exception):
    pass


class InputUnavailable(Exception):
    pass


class GuardRefused(RuntimeError):
    pass


# ---------------------------------------------------------------- guard seams
REFUSALS = []
_ALLOW = threading.local()


def _refuse(kind, detail=""):
    REFUSALS.append({"kind": kind, "detail": str(detail)[:200]})
    raise GuardRefused(kind + "_refused_by_acceptance_guard")


def install_guards():
    """Before any candidate import: no network/DNS, no unsanctioned process."""
    real_popen = subprocess.Popen

    class GuardedPopen(real_popen):
        def __init__(self, args, *rest, **kwargs):
            if not getattr(_ALLOW, "on", False):
                first = args[0] if isinstance(args, (list, tuple)) and args else args
                _refuse("subprocess", first)
            super().__init__(args, *rest, **kwargs)

    subprocess.Popen = GuardedPopen
    for name in ("system", "popen", "posix_spawn", "posix_spawnp", "execv", "execve", "execvp",
                 "execvpe", "spawnv", "spawnve", "spawnvp", "spawnvpe", "fork", "forkpty"):
        if hasattr(os, name):
            setattr(os, name, lambda *a, _n=name, **k: _refuse("process_" + _n, a[0] if a else ""))

    def no_network(*args, **kwargs):
        _refuse("network", args[0] if args else "")

    socket.socket.connect = no_network
    socket.socket.connect_ex = no_network
    socket.socket.sendto = no_network
    for name in ("create_connection", "getaddrinfo", "gethostbyname", "gethostbyname_ex",
                 "gethostbyaddr", "getnameinfo"):
        setattr(socket, name, no_network)


class children_allowed:
    def __enter__(self):
        _ALLOW.on = True

    def __exit__(self, *exc):
        _ALLOW.on = False
        return False


# ------------------------------------------------------------------- helpers
def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def stamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def linked(path):
    return any(candidate.is_symlink() for candidate in (path, *path.parents))


def checked_output_root(value):
    absolute = Path(value)
    if not absolute.is_absolute() or Path(os.path.normpath(absolute)) != absolute:
        raise RootRefused("output_root_must_be_absolute_and_normalized")
    if linked(absolute):
        raise RootRefused("output_root_linked")
    if absolute == REPO or REPO in absolute.parents:
        raise RootRefused("output_root_inside_repository")
    if absolute.exists() and not absolute.is_dir():
        raise RootRefused("output_root_not_directory")
    return absolute


def new_run_dir(root, mode):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if linked(root):
        raise RootRefused("output_root_linked")
    run = root / "{}-{}-{}-{}".format(mode, stamp(), os.getpid(), secrets.token_hex(4))
    run.mkdir(mode=0o700)
    return run


def run_child(run_dir, label, argv, cwd=REPO, env=None, timeout=1800):
    record = {"label": label, "argv": [str(a) for a in argv], "cwd": str(cwd)}
    out_path, err_path = run_dir / (label + ".stdout"), run_dir / (label + ".stderr")
    try:
        with open(out_path, "xb") as out, open(err_path, "xb") as err, children_allowed():
            done = subprocess.run([str(a) for a in argv], cwd=str(cwd), stdout=out, stderr=err,
                                  stdin=subprocess.DEVNULL, env=env, timeout=timeout, check=False)
        record["exit"] = done.returncode
    except (OSError, subprocess.SubprocessError) as error:
        record["exit"] = None
        record["launch_error"] = type(error).__name__
    for key, path in (("stdout", out_path), ("stderr", err_path)):
        if path.is_file():
            record[key + "_path"] = str(path)
            record[key + "_sha256"] = sha256_bytes(path.read_bytes())
    return record


def git(*argv):
    try:
        with children_allowed():
            done = subprocess.run(["git", "-C", str(REPO), *argv], capture_output=True, timeout=120,
                                  check=False, stdin=subprocess.DEVNULL)
        return done.returncode, done.stdout
    except (OSError, subprocess.SubprocessError):
        return None, b""


def staged_identity():
    code, head = git("rev-parse", "HEAD")
    status_code, status = git("status", "--porcelain", "--untracked-files=all")
    return {"head": head.decode().strip() if code == 0 else None,
            "clean": status_code == 0 and status.strip() == b"",
            "status_porcelain": status.decode("utf-8", "replace") if status_code == 0 else None}


def source_hashes(root=REPO):
    return {name: (sha256_bytes((root / name).read_bytes()) if (root / name).is_file() else None)
            for name in DELIVERABLES}


def import_candidate():
    """Repository-root import only; an ambient campaign_tool is refused."""
    ambient = sorted(name for name in sys.modules if name == "campaign_tool" or name.startswith("campaign_tool.")
                     or name == "tests" or name.startswith("tests."))
    if ambient:
        raise RuntimeError("ambient_import_present")
    if str(REPO) in sys.path:
        sys.path.remove(str(REPO))
    sys.path.insert(0, str(REPO))
    try:
        from campaign_tool.records import correspondence
        from campaign_tool.records.ledger import store
        from campaign_tool.records.understanding import agency_timeline
        tests = __import__(TEST_MODULE, fromlist=["*"])
    except ImportError as error:
        name = str(getattr(error, "name", None) or error)
        if name.split(".")[0] in ("campaign_tool", "tests"):
            raise RuntimeError("candidate_import_failed:" + name) from None
        raise InputUnavailable("module_unavailable:" + name) from None
    origins = {}
    for module in (sys.modules["campaign_tool"], correspondence, store, agency_timeline, tests):
        origins[module.__name__] = str(Path(module.__file__).resolve())
    outside = [name for name, origin in origins.items() if not Path(origin).is_relative_to(REPO)]
    if outside:
        raise RuntimeError("module_loaded_outside_candidate:" + ",".join(outside))
    return {"correspondence": correspondence, "store": store, "timeline": agency_timeline,
            "tests": tests, "origins": origins}


def run_suite(suite):
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    return result, stream.getvalue()


def summary(result):
    return {"tests_run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
            "skipped": len(result.skipped), "expected_failures": len(result.expectedFailures),
            "unexpected_successes": len(result.unexpectedSuccesses), "successful": result.wasSuccessful()}


def clean_pass(counts, minimum=1):
    return (counts["tests_run"] >= minimum and counts["failures"] == 0 and counts["errors"] == 0
            and counts["skipped"] == 0 and counts["expected_failures"] == 0
            and counts["unexpected_successes"] == 0 and counts["successful"])


class Checks:
    def __init__(self):
        self.items = []

    def add(self, name, ok, detail=None):
        self.items.append({"name": name, "ok": bool(ok), "detail": detail})
        return bool(ok)

    def passed(self):
        return bool(self.items) and all(item["ok"] for item in self.items)


# --------------------------------------------------------------------- modes
def mode_focused(run_dir, checks, record):
    loaded = import_candidate()
    record["module_origins"] = loaded["origins"]
    suite = unittest.defaultTestLoader.loadTestsFromName(TEST_MODULE)
    expected = suite.countTestCases()
    result, text = run_suite(suite)
    (run_dir / "unittest-output.txt").write_text(text, encoding="utf-8")
    counts = summary(result)
    record["tests"] = counts
    checks.add("A1_at_least_10_tests", counts["tests_run"] >= 10, counts["tests_run"])
    checks.add("A1_all_loaded_tests_ran", counts["tests_run"] == expected, expected)
    checks.add("A1_zero_failures_errors_skips", clean_pass(counts, 10), counts)
    checks.add("A1_no_guard_refusals", not REFUSALS, REFUSALS)


def mode_negative(run_dir, checks, record):
    loaded = import_candidate()
    record["module_origins"] = loaded["origins"]
    correspondence, store = loaded["correspondence"], loaded["store"]
    original = correspondence.ledger
    checks.add("A3_precondition_reader_uses_store_ledger", original is store.ledger)

    def writable_open(path, readonly=False):
        # Isolated synthetic mutation: ignore the requested read-only mode.
        return store.ledger(path, readonly=False)

    loader = unittest.defaultTestLoader
    correspondence.ledger = writable_open
    try:
        mutant, mutant_text = run_suite(loader.loadTestsFromName(NEGATIVE_TEST))
    finally:
        correspondence.ledger = original
    (run_dir / "mutant-output.txt").write_text(mutant_text, encoding="utf-8")
    mutant_counts = summary(mutant)
    failure_text = "\n".join(text for _, text in mutant.failures)
    record["mutant"] = mutant_counts
    checks.add("A3_mutant_ran_one_test", mutant_counts["tests_run"] == 1, mutant_counts)
    checks.add("A3_mutant_failed_not_errored", mutant_counts["failures"] == 1 and mutant_counts["errors"] == 0,
               mutant_counts)
    checks.add("A3_mutant_failed_for_write_capability", NEGATIVE_REASON in failure_text
               and "write_refused_readonly" in failure_text, NEGATIVE_REASON)
    checks.add("A3_seam_restored", correspondence.ledger is store.ledger)
    positive, positive_text = run_suite(loader.loadTestsFromName(NEGATIVE_TEST))
    full, full_text = run_suite(loader.loadTestsFromName(TEST_MODULE))
    (run_dir / "positive-output.txt").write_text(positive_text + "\n" + full_text, encoding="utf-8")
    record["positive"], record["positive_module"] = summary(positive), summary(full)
    record["tests"] = {"tests_run": mutant.testsRun + positive.testsRun + full.testsRun}
    checks.add("A3_positive_regression_passed", clean_pass(summary(positive)), summary(positive))
    checks.add("A3_positive_module_passed", clean_pass(summary(full), 10), summary(full))
    checks.add("A3_no_guard_refusals", not REFUSALS, REFUSALS)


def staged_base():
    manifest = REPO.parent / "SOURCE-MANIFEST.json"
    if not manifest.is_file() or manifest.is_symlink():
        raise InputUnavailable("staged_base_manifest_unavailable")
    head = json.loads(manifest.read_text(encoding="utf-8")).get("head")
    if not isinstance(head, str) or len(head) != 40 or not all(c in "0123456789abcdef" for c in head):
        raise InputUnavailable("staged_base_manifest_head_invalid")
    return head


def mode_compatibility(run_dir, checks, record):
    base = staged_base()
    record["staged_base"] = base
    record["staged_base_matches_card_pin_prefix"] = base.startswith(PIN_PREFIX)
    for rel in COMPAT_FILES:
        code, pinned = git("show", base + ":" + rel)
        current = (REPO / rel).read_bytes() if (REPO / rel).is_file() else None
        checks.add("A2_byte_identical:" + rel, code == 0 and current == pinned,
                   {"base_sha256": sha256_bytes(pinned) if code == 0 else None,
                    "current_sha256": sha256_bytes(current) if current is not None else None})
    code, diff = git("diff", "--name-status", "--no-renames", base)
    changes = [line.split("\t", 1) for line in diff.decode().splitlines() if line.strip()]
    code_untracked, untracked = git("ls-files", "--others", "--exclude-standard")
    extra = [p for p in untracked.decode().splitlines() if p.strip()]
    record["changed_vs_base"] = changes
    record["untracked"] = extra
    checks.add("A2_only_added_deliverables_vs_base",
               code == 0 and code_untracked == 0 and all(status == "A" and path in DELIVERABLES
                                                         for status, path in changes)
               and all(path in DELIVERABLES for path in extra),
               {"changes": changes, "untracked": extra})
    children = []
    for module in COMPAT_MODULES:
        child = run_child(run_dir, "compat-" + module.rsplit(".", 1)[-1],
                          [sys.executable, "-B", str(RUNNER), "--compat-child", module])
        try:
            lines = Path(child["stdout_path"]).read_text(encoding="utf-8").strip().splitlines()
            report = json.loads(lines[-1])
        except (KeyError, OSError, ValueError, IndexError):
            report = None
        child["report"] = report
        children.append(child)
        ok = (child.get("exit") == 0 and isinstance(report, dict) and report.get("module") == module
              and clean_pass(report.get("tests", {"tests_run": 0, "failures": 1, "errors": 1, "skipped": 1,
                                                  "expected_failures": 0, "unexpected_successes": 0,
                                                  "successful": False}))
              and report.get("guard_refusals") == []
              and all(Path(o).is_relative_to(REPO) for o in report.get("origins", {}).values()))
        checks.add("A2_guarded_child_passed_zero_skips:" + module, ok,
                   None if report is None else report.get("tests"))
    record["children"] = children
    record["tests"] = {"tests_run": sum(((c.get("report") or {}).get("tests") or {}).get("tests_run", 0)
                                        for c in children)}


def compat_child(module):
    if module not in COMPAT_MODULES:
        print(json.dumps({"error": "compat_module_not_admitted"}))
        return EXIT_REFUSED
    install_guards()
    loaded = import_candidate()
    target = __import__(module, fromlist=["*"])
    origins = dict(loaded["origins"], **{module: str(Path(target.__file__).resolve())})
    result, text = run_suite(unittest.defaultTestLoader.loadTestsFromName(module))
    sys.stderr.write(text)
    counts = summary(result)
    print(json.dumps({"module": module, "tests": counts, "guard_refusals": REFUSALS, "origins": origins},
                     sort_keys=True))
    return EXIT_PASS if clean_pass(counts) and not REFUSALS else EXIT_FAIL


def copy_tree(destination):
    code, listed = git("ls-files", "-z", "campaign_tool")
    if code != 0:
        raise InputUnavailable("git_ls_files_unavailable")
    names = sorted({*(n for n in listed.decode().split("\0") if n), *SUPPORT_FILES, *DELIVERABLES})
    for name in names:
        source = REPO / name
        if not source.is_file() or source.is_symlink():
            continue
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with open(target, "xb") as stream:
            stream.write(source.read_bytes())
    return len(names)


def mode_portability(run_dir, checks, record):
    home = run_dir / "synthetic-home"
    home.mkdir(mode=0o700)
    os.environ["HOME"] = str(home)  # before any candidate import
    relocated = run_dir / ("n3-relocated-root-" + secrets.token_hex(4))
    relocated.mkdir(mode=0o700)
    record["relocated_files"] = copy_tree(relocated)
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    env["HOME"] = str(home)
    child = run_child(run_dir, "relocated-focused",
                      [sys.executable, "-B", str(relocated / RUNNER_REL), "--mode", "focused",
                       "--output-root", str(run_dir / "relocated-out")], cwd=home, env=env)
    try:
        line = Path(child["stdout_path"]).read_text(encoding="utf-8").strip().splitlines()[-1]
        child_summary = json.loads(line)
        child_record = json.loads((Path(child_summary["run_dir"]) / "run.json").read_text(encoding="utf-8"))
    except (KeyError, OSError, ValueError, IndexError):
        child_record = None
    record["relocated_child"] = child
    record["tests"] = {"tests_run": (child_record or {}).get("tests", {}).get("tests_run", 0)}
    origins = (child_record or {}).get("module_origins", {})
    checks.add("A4_relocated_focused_exit_0", child.get("exit") == 0 and child_record is not None
               and child_record.get("exit") == 0, child.get("exit"))
    checks.add("A4_relocated_tests_ran", (child_record or {}).get("tests", {}).get("tests_run", 0) >= 10,
               (child_record or {}).get("tests"))
    checks.add("A4_relocated_imports_from_relocated_root",
               bool(origins) and all(Path(o).is_relative_to(relocated) and not Path(o).is_relative_to(REPO)
                                     for o in origins.values()), origins)
    found = {}
    for rel in DELIVERABLES:
        data = (REPO / rel).read_bytes() if (REPO / rel).is_file() else b""
        hits = [token for token in BOX_SPECIFIC if token.encode() in data]
        if hits or not data:
            found[rel] = hits or ["missing"]
    checks.add("A4_no_box_only_paths_in_committed_files", not found, found)
    loaded = import_candidate()
    record["module_origins"] = loaded["origins"]
    checks.add("A4_home_is_synthetic_before_imports", Path(os.path.expanduser("~")) == home, str(home))
    probe_root = run_dir / "allocation-probe"
    first, second = new_run_dir(probe_root, "probe"), new_run_dir(probe_root, "probe")
    try:
        first.mkdir()
        reused = True
    except FileExistsError:
        reused = False
    checks.add("A4_unique_output_allocation", first != second and not reused, [first.name, second.name])
    ledger_dir = run_dir / "synthetic-ledger"
    ledger_dir.mkdir(mode=0o700)
    path = loaded["tests"].build_ledger(ledger_dir / "ledger.sqlite")
    tool = loaded["tests"].load_tool()
    codes, outputs = [], []
    for name in ("cli-one", "cli-two", "cli-one"):
        with io.StringIO() as stream:
            saved, sys.stdout = sys.stdout, stream
            try:
                codes.append(tool.main(["--ledger", str(path), "--output-dir", str(run_dir / name)]))
            finally:
                sys.stdout = saved
            outputs.append(stream.getvalue())
    one = {p.name: p.read_bytes() for p in sorted((run_dir / "cli-one").iterdir())}
    two = {p.name: p.read_bytes() for p in sorted((run_dir / "cli-two").iterdir())}
    checks.add("A4_cli_outputs_identical_across_fresh_dirs", codes[:2] == [0, 0] and one == two and len(one) == 2,
               codes)
    checks.add("A4_cli_refuses_existing_output_dir", codes[2] == 2 and "output_dir_exists" in outputs[2], codes)
    checks.add("A4_no_guard_refusals", not REFUSALS, REFUSALS)


def imports_of(path):
    names = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def mode_hygiene(run_dir, checks, record):
    for rel in (MODULE_REL, TOOL_REL, TEST_REL):
        hits = sorted(imports_of(REPO / rel) & NETWORK_MODULES)
        checks.add("A5_no_network_or_process_imports:" + rel, not hits, hits)
    module_source = (REPO / MODULE_REL).read_text(encoding="utf-8")
    write_forms = ("open(", ".write", "mkdir", "unlink", "rename", "rmtree", "remove(", "readonly=False",
                   "mode=rw", "sqlite3", "initialize", "import_legacy")
    checks.add("A5_module_has_no_write_path", not [f for f in write_forms if f in module_source],
               [f for f in write_forms if f in module_source])
    tool_source = (REPO / TOOL_REL).read_text(encoding="utf-8")
    tool_forbidden = ("unlink", "rename", "rmtree", "remove(", "replace(", "readonly=False", "mode=rw",
                      "sqlite3", "initialize", "import_legacy", "fdopen")
    tool_hits = [f for f in tool_forbidden if f in tool_source]
    if tool_source.count("open(") != tool_source.count("os.open("):
        tool_hits.append("open_other_than_os_open")
    checks.add("A5_tool_writes_only_exclusive_new_files", "O_EXCL" in tool_source and not tool_hits, tool_hits)
    detector = Path(sys.executable).parent / "detect-secrets"
    if not detector.is_file():
        raise InputUnavailable("module_unavailable:detect-secrets")
    scan = run_child(run_dir, "detect-secrets", [detector, "scan", *DELIVERABLES])
    report_path = run_dir / "detect-secrets-report.json"
    report_path.write_bytes(Path(scan["stdout_path"]).read_bytes())
    checker = run_child(run_dir, "check-secret-report",
                        [sys.executable, "-B", "tools/check_secret_report.py", str(report_path)])
    try:
        scanned = sorted(json.loads(report_path.read_text(encoding="utf-8"))["results"])
    except (OSError, ValueError, KeyError):
        scanned = None
    record["secret_scan"] = {"scan": scan, "check": checker, "files_with_hits": scanned}
    checks.add("A5_detect_secrets_scan_ran", scan.get("exit") == 0 and scanned is not None, scan.get("exit"))
    checks.add("A5_check_secret_report_passed", checker.get("exit") == 0, checker.get("exit"))
    public = run_child(run_dir, "check-public-tree", [sys.executable, "-B", "tools/check_public_tree.py"])
    record["public_tree"] = public
    checks.add("A5_check_public_tree_passed", public.get("exit") == 0, public.get("exit"))
    loaded = import_candidate()
    record["module_origins"] = loaded["origins"]
    ledger_dir = run_dir / "synthetic-ledger"
    ledger_dir.mkdir(mode=0o700)
    path = loaded["tests"].build_ledger(ledger_dir / "ledger.sqlite")
    cli = run_child(run_dir, "cli", [sys.executable, "-B", TOOL_REL, "--ledger", str(path),
                                     "--output-dir", str(run_dir / "cli-output")])
    record["cli"] = cli
    checks.add("A5_cli_exit_0", cli.get("exit") == 0, cli.get("exit"))
    needles = [s.casefold() for s in loaded["tests"].sentinels()]
    leaks, scanned_files = {}, 0
    for item in sorted(run_dir.rglob("*")):
        if not item.is_file() or item.is_symlink() or ledger_dir in item.parents:
            continue
        scanned_files += 1
        text = item.read_bytes().decode("utf-8", "replace").casefold()
        hits = [n for n in needles if n in text]
        if hits:
            leaks[str(item.relative_to(run_dir))] = hits
    cli_texts = [p.read_text(encoding="utf-8") for p in sorted((run_dir / "cli-output").glob("*"))]
    record["sentinel_scan"] = {"files": scanned_files, "excluded": [str(ledger_dir.relative_to(run_dir))]}
    checks.add("A5_cli_outputs_present", len(cli_texts) == 2, len(cli_texts))
    checks.add("A5_sentinels_absent_from_every_output", scanned_files > 0 and not leaks, leaks)
    checks.add("A5_no_address_marker_in_report_outputs", cli_texts and not any("@" in t for t in cli_texts))
    checks.add("A5_no_guard_refusals", not REFUSALS, REFUSALS)


MODE_FUNCTIONS = {"focused": mode_focused, "compatibility": mode_compatibility, "negative": mode_negative,
                  "portability": mode_portability, "hygiene": mode_hygiene}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) == 2 and argv[0] == "--compat-child":
        return compat_child(argv[1])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", required=True, choices=MODES)
    parser.add_argument("--output-root", required=True)
    args = parser.parse_args(argv)
    install_guards()
    try:
        root = checked_output_root(args.output_root)
        run_dir = new_run_dir(root, args.mode)
    except (RootRefused, OSError) as error:
        print(json.dumps({"mode": args.mode, "exit": EXIT_REFUSED, "error": str(error)[:200]}))
        return EXIT_REFUSED
    record = {"schema": "n3-agency-timeline-acceptance-v1", "mode": args.mode, "argv": [sys.executable, *sys.argv],
              "cwd": os.getcwd(), "interpreter": sys.executable, "python": sys.version.split()[0],
              "repo": str(REPO), "run_dir": str(run_dir), "started_at": stamp(),
              "staged": staged_identity(), "source_sha256": source_hashes(),
              "review_required": True}
    checks = Checks()
    try:
        MODE_FUNCTIONS[args.mode](run_dir, checks, record)
        code = EXIT_PASS if checks.passed() else EXIT_FAIL
    except InputUnavailable as error:
        record["input_unavailable"] = str(error)
        code = EXIT_INPUT
    except Exception as error:  # Any unexpected error fails closed with its type recorded.
        record["unexpected_error"] = type(error).__name__ + ":" + str(error)[:500]
        code = EXIT_FAIL
    record.update(checks=checks.items, checks_total=len(checks.items),
                  checks_passed=sum(item["ok"] for item in checks.items),
                  guard_refusals=REFUSALS, finished_at=stamp(), exit=code)
    (run_dir / "run.json").write_text(json.dumps(record, sort_keys=True, indent=2, default=str) + "\n",
                                      encoding="utf-8")
    lines = ["{} {}".format("PASS" if item["ok"] else "FAIL", item["name"]) for item in checks.items]
    lines.append("mode={} exit={} checks={}/{} tests_run={}".format(
        args.mode, code, record["checks_passed"], record["checks_total"],
        record.get("tests", {}).get("tests_run")))
    for key in ("input_unavailable", "unexpected_error"):
        if key in record:
            lines.append(key.upper() + " " + record[key])
    (run_dir / "output.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    sys.stderr.write("\n".join(lines) + "\n")
    print(json.dumps({"mode": args.mode, "exit": code, "checks_passed": record["checks_passed"],
                      "checks_total": record["checks_total"], "run_dir": str(run_dir)}, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
