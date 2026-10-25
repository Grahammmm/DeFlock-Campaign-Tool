"""Scoped offline acceptance runner for FA52 receipt budget counter binding.

    accept_fa52_budget_types.py --case CASE --output-parent DIR

Cases (each invocation runs exactly one, in a fresh unique child directory of
--output-parent, which must be an absolute, link-free path outside the repo):

  behavior          tests.records.test_fa52_budget_types; A1/A2/A3 named assertions
  compatibility     unchanged test_mail_wire_intake, test_native_completion, test_mail_delta
  negative-control  A4: the original ``supplied == recomputed`` equality must let the
                    bool and integral-float regressions accept (detected as failures)
                    while the reordered integer-budget positive stays green
  isolation         A5: guard/redirect self-checks, bound-default/constant scan, path
                    ancestry refusals, portability scan, and the focused tests rerun
                    from a second complete source copy with recorded module paths
  credentials       installed detect-secrets beside this interpreter, then the
                    unchanged tools/check_secret_report.py on that exact report

Repository code is only imported by file-launched child interpreters (argv
arrays, no shell) that disable bytecode writes, redirect HOME/TMPDIR/tempfile and
install network, process, live-file and out-of-root write sentinels BEFORE any
application import. Compatibility tests launch Python descendants (extraction and
folder workers, the mail_delta CLI); only those admitted forms may start, each is
rewritten to this runner's guarded bootstrap (same sentinels before the real
module/script runs) and every descendant refusal, even one the application
catches, fails the launching child. The parent records every child's argv, cwd,
exit, stdout and
stderr with hashes. A pass is REVIEW REQUIRED scoped evidence only: not
full-repository CI, semantic, legal, human, stage or release acceptance.
Full discovery = operator-run.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import subprocess
import sys

sys.dont_write_bytecode = True

REPO = Path(__file__).resolve().parents[1]
PRODUCT = "campaign_tool/records/intake/native_receipt.py"
TEST_FILE = "tests/records/test_fa52_budget_types.py"
RUNNER_FILE = "tools/accept_fa52_budget_types.py"
TEST_MODULE = "tests.records.test_fa52_budget_types"
AUTHORED = (PRODUCT, TEST_FILE, RUNNER_FILE)
COMPATIBILITY = ("tests.records.test_mail_wire_intake", "tests.records.test_native_completion",
                 "tests.records.test_mail_delta")
REFERENCED = ("tests/records/test_mail_wire_intake.py", "tests/records/test_native_completion.py",
              "tests/records/test_mail_delta.py", "tests/records/test_imap_intake.py",
              "tests/records/native_msg_fixtures.py", "campaign_tool/records/intake/mail_delta.py",
              "campaign_tool/records/intake/mail_wire.py", "campaign_tool/records/intake/eml_export.py",
              "tools/check_secret_report.py", "tools/check_public_tree.py")
CASES = ("behavior", "compatibility", "negative-control", "isolation", "credentials")
A1 = "A1PositiveBoundaryTests"
A2 = "A2MalformedBudgetTests"
A3 = "A3ImporterReplayTests"
NAMED = {
    "A1": [A1 + "." + n for n in ("test_a1_parser_closure_available",
                                  "test_a1_producer_budget_is_flat_exact_integer_counters",
                                  "test_a1_zero_and_one_integer_counters_bind",
                                  "test_a1_larger_counts_and_nested_receipts_bind",
                                  "test_a1_reordered_integer_budget_binds")],
    "A2": [A2 + "." + n for n in ("test_a2_bool_counter_refused", "test_a2_integral_float_counter_refused",
                                  "test_a2_null_negative_string_list_object_counter_refused",
                                  "test_a2_missing_extra_and_changed_counter_refused",
                                  "test_a2_missing_or_nonobject_budget_refused")],
    "A3": [A3 + "." + n for n in ("test_a3_importer_round_trip_and_replay",
                                  "test_a3_tampered_budget_never_publishes_or_mutates")],
}
DETECT = [A2 + ".test_a2_bool_counter_refused", A2 + ".test_a2_integral_float_counter_refused"]
POSITIVE = A1 + ".test_a1_reordered_integer_budget_binds"
SCRUBBED_ENV = ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONINSPECT", "PYTHONUSERBASE",
                "PYTHONSAFEPATH", "PYTHONPYCACHEPREFIX")
# Machine-specific path fragments that must not appear in committed FA52 test/helper
# source. Assembled from pieces so this scanner does not flag its own token table.
FORBIDDEN_FRAGMENTS = tuple("".join(parts) for parts in (
    ("/ho", "me/"), ("/Us", "ers/"), ("agent", "-work"), (".her", "mes"), ("/t", "mp/"),
    ("candi", "dates/"), ("venv-", "fullsuite"), ("t_", "ad0d5928")))


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def git(*args, cwd=None):
    done = subprocess.run(["git", *args], cwd=cwd or REPO, capture_output=True, check=False)
    return done.returncode, done.stdout


def tree_digest(root):
    """Content digest of every file below root except .git (bytecode included)."""
    digest = hashlib.sha256()
    count = 0
    for directory, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d != ".git")
        for name in sorted(files):
            path = Path(directory) / name
            data = os.readlink(path).encode() if path.is_symlink() else path.read_bytes()
            digest.update(str(path.relative_to(root)).encode() + b"\0" + hashlib.sha256(data).digest())
            count += 1
    return {"sha256": digest.hexdigest(), "files": count}


def identity():
    _, head = git("rev-parse", "HEAD")
    _, status = git("status", "--porcelain=v1", "--untracked-files=all")
    files = {}
    for name in AUTHORED + REFERENCED:
        path = REPO / name
        files[name] = sha256_bytes(path.read_bytes()) if path.is_file() else None
    return {"head": head.decode().strip(), "status_porcelain": status.decode(), "clean_tree": status == b"",
            "file_sha256": files, "tree": tree_digest(REPO)}


# ------------------------------------------------------------ evidence root


class UnsafePath(Exception):
    pass


def safe_output_parent(value, repo=REPO):
    """Absolute, normalized, link-free, owner-controlled evidence parent outside the repo."""
    if not isinstance(value, str) or not value or "\0" in value:
        raise UnsafePath("output_parent_invalid")
    if not os.path.isabs(value):
        raise UnsafePath("output_parent_not_absolute")
    if os.path.normpath(value) != value.rstrip("/") or "/../" in value + "/" or "/./" in value + "/":
        raise UnsafePath("output_parent_not_normalized")
    path = Path(value)
    current = Path(path.anchor)
    missing = False
    for part in path.parts[1:]:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            missing = True
            continue
        if missing:
            raise UnsafePath("output_parent_ancestry_changed")
        if stat.S_ISLNK(info.st_mode):
            raise UnsafePath("output_parent_linked")
        if not stat.S_ISDIR(info.st_mode):
            raise UnsafePath("output_parent_not_directory")
        if info.st_mode & stat.S_IWOTH and not info.st_mode & stat.S_ISVTX:
            raise UnsafePath("output_parent_world_writable")
    repo = Path(repo).resolve()
    if path == repo or repo in path.parents:
        raise UnsafePath("output_parent_inside_repository")
    existing = path
    while not existing.exists():
        existing = existing.parent
    if existing.stat().st_uid != os.getuid() and existing != path:
        raise UnsafePath("output_parent_not_owned")
    return path


class Run:
    def __init__(self, parent, case):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        resolved = parent.resolve()
        if resolved != parent:
            raise UnsafePath("output_parent_resolution_changed")
        self.dir = parent / ("%s-%s-%d-%s" % (case, stamp, os.getpid(), secrets.token_hex(4)))
        self.dir.mkdir(mode=0o700)
        self.children = []

    def child(self, label, argv, cwd=REPO, stdout_name=None, env_root=None):
        root = env_root or self.dir
        env = {k: v for k, v in os.environ.items() if k not in SCRUBBED_ENV}
        for name in ("tmp", "home"):
            (root / name).mkdir(mode=0o700, exist_ok=True)
        env.update(TMPDIR=str(root / "tmp"), HOME=str(root / "home"), XDG_CACHE_HOME=str(root / "home"),
                   XDG_CONFIG_HOME=str(root / "home"), XDG_DATA_HOME=str(root / "home"),
                   PYTHONDONTWRITEBYTECODE="1")  # inherited by grandchildren (e.g. extraction workers)
        argv = [str(a) for a in argv]
        done = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, check=False)
        out_path = self.dir / (stdout_name or label + ".stdout")
        err_path = self.dir / (label + ".stderr")
        out_path.write_bytes(done.stdout)
        err_path.write_bytes(done.stderr)
        entry = {"label": label, "argv": argv, "cwd": str(cwd), "exit": done.returncode,
                 "stdout": str(out_path), "stdout_sha256": sha256_bytes(done.stdout),
                 "stderr": str(err_path), "stderr_sha256": sha256_bytes(done.stderr)}
        self.children.append(entry)
        return entry, done.stdout

    def test_child(self, label, mode, extra=(), repo=REPO, child_root=None):
        child_root = child_root or self.dir
        child_root.mkdir(mode=0o700, exist_ok=True)
        result_path = child_root / (label + "-result.json")
        script = repo / RUNNER_FILE
        entry, _ = self.child(label, [sys.executable, "-B", script, "--child", mode, "--run-dir", child_root,
                                      "--result-json", result_path, "--sentinel-root", Path.home(), *extra],
                              cwd=repo, env_root=child_root)
        try:
            record = json.loads(result_path.read_text())
            if not isinstance(record, dict):
                raise ValueError
        except (OSError, ValueError):
            record = {"pass": False, "error": "child_result_missing_or_malformed"}
        record["child_exit"] = entry["exit"]
        record["result_json"] = str(result_path)
        record["result_json_sha256"] = sha256_bytes(result_path.read_bytes()) if result_path.is_file() else None
        record["pass"] = record.get("pass") is True and entry["exit"] == 0
        return record


def clean_counts(record):
    return (isinstance(record.get("tests_run"), int) and record["tests_run"] > 0 and record.get("failures") == 0
            and record.get("errors") == 0 and record.get("skipped") == 0
            and record.get("expected_failures") == 0 and record.get("unexpected_successes") == 0)


def case_behavior(run):
    verdict = run.test_child("behavior", "unittest", ("--module", TEST_MODULE))
    outcomes = verdict.get("outcomes", {})
    prefix = TEST_MODULE + "."
    mapping = {}
    for group, names in NAMED.items():
        mapping[group] = {name: outcomes.get(prefix + name, "not_run") for name in names}
    named = {prefix + name for names in NAMED.values() for name in names}
    unnamed = sorted(set(outcomes) - named)
    verdict["named_assertions"] = mapping
    verdict["unnamed_tests"] = unnamed
    verdict["pass"] = (verdict["pass"] and clean_counts(verdict) and not unnamed
                       and all(v == "pass" for group in mapping.values() for v in group.values()))
    return verdict


def case_compatibility(run):
    extra = []
    for name in COMPATIBILITY:
        extra += ["--module", name]
    verdict = run.test_child("compatibility", "unittest", extra)
    per_module = {name: {"tests": 0, "pass": 0} for name in COMPATIBILITY}
    for test_id, status in verdict.get("outcomes", {}).items():
        for name in COMPATIBILITY:
            if test_id.startswith(name + "."):
                per_module[name]["tests"] += 1
                per_module[name]["pass"] += status == "pass"
    verdict["per_module"] = per_module
    verdict["pass"] = (verdict["pass"] and clean_counts(verdict)
                       and all(v["tests"] > 0 and v["tests"] == v["pass"] for v in per_module.values()))
    return verdict


def case_negative_control(run):
    return run.test_child("negative-control", "negative-control")


def portability_scan(repo):
    hits = []
    for name in (TEST_FILE, RUNNER_FILE):
        text = (repo / name).read_text()
        for number, line in enumerate(text.splitlines(), 1):
            for fragment in FORBIDDEN_FRAGMENTS:
                if fragment in line:
                    hits.append({"file": name, "line": number, "fragment_index": FORBIDDEN_FRAGMENTS.index(fragment)})
    return hits


def ancestry_refusals(run):
    """Exercise safe_output_parent on synthetic unsafe inputs inside this run directory."""
    probe = run.dir / "ancestry-probe"
    real = probe / "real"
    real.mkdir(parents=True, mode=0o700)
    link = probe / "linked"
    link.symlink_to(real, target_is_directory=True)
    afile = probe / "file"
    afile.write_bytes(b"not a directory\n")
    cases = {"relative": ("relative/evidence", "output_parent_not_absolute"),
             "dotdot": (str(real) + "/../real", "output_parent_not_normalized"),
             "linked_leaf": (str(link), "output_parent_linked"),
             "linked_ancestor": (str(link / "child"), "output_parent_linked"),
             "file_ancestor": (str(afile / "child"), "output_parent_not_directory"),
             "inside_repository": (str(REPO / "evidence"), "output_parent_inside_repository"),
             "repository_itself": (str(REPO), "output_parent_inside_repository")}
    results = {}
    for label, (value, expected) in cases.items():
        try:
            safe_output_parent(value)
            results[label] = {"refused": False, "expected": expected}
        except UnsafePath as error:
            results[label] = {"refused": str(error) == expected, "code": str(error), "expected": expected}
    try:
        accepted = safe_output_parent(str(real / "new-child"))
        results["positive_new_child"] = {"refused": False, "accepted": accepted == real / "new-child"}
    except UnsafePath as error:
        results["positive_new_child"] = {"refused": True, "code": str(error)}
    ok = all(v.get("refused") is True for k, v in results.items() if k != "positive_new_child") and \
        results["positive_new_child"].get("accepted") is True
    return ok, results


def complete_copy(run):
    """Second complete source copy (every tracked and untracked-unignored file)."""
    code, listing = git("ls-files", "-z", "--cached", "--others", "--exclude-standard")
    if code != 0:
        return None, {"error": "git_ls_files_failed"}
    copy = run.dir / "source-copy"
    copy.mkdir(mode=0o700)
    names = sorted({os.fsdecode(n) for n in listing.split(b"\0") if n})
    manifest = hashlib.sha256()
    copied = 0
    for name in names:
        source = REPO / name
        target = copy / name
        if not source.exists() and not source.is_symlink():
            continue  # deleted in the working tree; not part of this source state
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_symlink():
            target.symlink_to(os.readlink(source))
            data = os.readlink(source).encode()
        else:
            data = source.read_bytes()
            target.write_bytes(data)
            target.chmod(stat.S_IMODE(source.stat().st_mode))
            if target.read_bytes() != data:
                return None, {"error": "copy_mismatch", "file": name}
        manifest.update(name.encode() + b"\0" + hashlib.sha256(data).digest())
        copied += 1
    return copy, {"path": str(copy), "files": copied, "manifest_sha256": manifest.hexdigest()}


def case_isolation(run):
    verdict = {}
    hits = portability_scan(REPO)
    ancestry_ok, ancestry = ancestry_refusals(run)
    original = run.test_child("isolation-original", "isolation-probe", ("--module", TEST_MODULE),
                              child_root=run.dir / "original-run")
    copy, copy_info = complete_copy(run)
    if copy is None:
        second = {"pass": False, "error": copy_info}
    else:
        copy_before = tree_digest(copy)
        second = run.test_child("isolation-copy", "isolation-probe", ("--module", TEST_MODULE),
                                repo=copy, child_root=run.dir / "copy-run")
        copy_after = tree_digest(copy)
        copy_info.update(tree_before=copy_before, tree_after=copy_after, unchanged=copy_before == copy_after)
        paths = second.get("module_paths", {})
        copy_info["module_paths_inside_copy"] = bool(paths) and all(
            p is not None and Path(p).resolve().is_relative_to(copy.resolve()) for p in paths.values())
        copy_info["module_paths_outside_original"] = bool(paths) and all(
            p is not None and not Path(p).resolve().is_relative_to(REPO) for p in paths.values())
    original_paths = original.get("module_paths", {})
    original_inside = bool(original_paths) and all(
        p is not None and Path(p).resolve().is_relative_to(REPO) for p in original_paths.values())
    descendants = run.test_child("isolation-descendants", "descendant-probe", child_root=run.dir / "descendant-run")
    copy_descendants = (run.test_child("isolation-copy-descendants", "descendant-probe", repo=copy,
                                       child_root=run.dir / "copy-descendant-run")
                        if copy is not None else {"pass": False, "error": "no_copy"})
    verdict.update(portability_hits=hits, ancestry_refusals=ancestry, original=original, second_copy=second,
                   copy=copy_info, original_module_paths_inside_repo=original_inside,
                   descendant_guards=descendants, copy_descendant_guards=copy_descendants)
    verdict["pass"] = (not hits and ancestry_ok and original_inside and original.get("pass") is True
                       and clean_counts(original) and second.get("pass") is True and clean_counts(second)
                       and copy is not None and copy_info.get("unchanged") is True
                       and copy_info.get("module_paths_inside_copy") is True
                       and copy_info.get("module_paths_outside_original") is True
                       and original.get("tests_run") == second.get("tests_run")
                       and descendants.get("pass") is True and copy_descendants.get("pass") is True)
    return verdict


def case_credentials(run):
    scanner = Path(sys.executable).parent / "detect-secrets"
    if not (scanner.is_file() and os.access(scanner, os.X_OK)):
        return {"pass": False, "error": "missing_dependency_detect_secrets_beside_interpreter",
                "scanner": str(scanner)}
    scan, raw = run.child("detect-secrets", [scanner, "scan", "--all-files", "--exclude-files", "^.git/",
                                             "--no-verify"], stdout_name="secret-scan.json")
    report_path = run.dir / "secret-scan.json"
    try:
        report = json.loads(raw)
        report_ok = isinstance(report.get("results"), dict)
        files_with_hits = sorted(report["results"]) if report_ok else None
        raw_hits = sum(len(v) for v in report["results"].values()) if report_ok else None
    except (ValueError, AttributeError, TypeError):
        report_ok, files_with_hits, raw_hits = False, None, None
    check, out = run.child("check-secret-report",
                           [sys.executable, "-B", REPO / "tools" / "check_secret_report.py", report_path])
    summary = [line for line in out.decode(errors="replace").splitlines() if "potential secrets" in line]
    return {"pass": scan["exit"] == 0 and report_ok and check["exit"] == 0,
            "scanner": str(scanner), "scan_exit": scan["exit"], "report": str(report_path),
            "report_parsed": report_ok, "raw_report_hits": raw_hits, "files_with_raw_hits": files_with_hits,
            "check_exit": check["exit"], "checker_summary": summary}


def parent_main(args):
    started = datetime.now(timezone.utc).isoformat()
    try:
        parent = safe_output_parent(args.output_parent)
        run = Run(parent, args.case)
    except UnsafePath as error:
        print(json.dumps({"case": args.case, "pass": False, "exit": 2, "refused": str(error)}))
        return 2
    before = identity()
    try:
        verdict = {"behavior": case_behavior, "compatibility": case_compatibility,
                   "negative-control": case_negative_control, "isolation": case_isolation,
                   "credentials": case_credentials}[args.case](run)
    except Exception as error:  # an unknown failure is never a pass
        verdict = {"pass": False, "error": "unknown_failure: %s: %s" % (type(error).__name__, error)}
    after = identity()
    stable = before == after
    passed = (verdict.get("pass") is True and stable and bool(run.children)
              and all(isinstance(c["exit"], int) for c in run.children))
    record = {"case": args.case, "argv": [sys.executable, *sys.argv], "cwd": os.getcwd(), "repo": str(REPO),
              "interpreter": sys.executable, "python": sys.version.split()[0], "run_dir": str(run.dir),
              "started_utc": started, "ended_utc": datetime.now(timezone.utc).isoformat(),
              "identity_before": before, "identity_after": after, "identity_stable": stable,
              "children": run.children, "verdict": verdict, "pass": passed, "exit": 0 if passed else 1,
              "result": "PASS_REVIEW_REQUIRED" if passed else "FAIL", "review_required": True,
              "scope": "scoped FA52 acceptance; not full-repository CI, semantic or release acceptance; "
                       "full discovery = operator-run"}
    (run.dir / "result.json").write_text(json.dumps(record, indent=2, sort_keys=True, default=str) + "\n")
    summary = {"case": args.case, "pass": passed, "exit": record["exit"], "run_dir": str(run.dir),
               "head": after["head"], "clean_tree": after["clean_tree"], "tests_run": verdict.get("tests_run"),
               "failures": verdict.get("failures"), "errors": verdict.get("errors"),
               "skipped": verdict.get("skipped"),
               "result_json_sha256": sha256_bytes((run.dir / "result.json").read_bytes())}
    print(json.dumps(summary, sort_keys=True))
    return record["exit"]


# ---------------------------------------------------------------- child side


DESCENDANT_FLAG = "--fa52-guarded-descendant"
PROBE_FLAG = "--fa52-descendant-probe"
APP_PACKAGES = ("campaign_tool", "tests")


def descendant_target(argv, repo=REPO):
    """Classify an admitted Python descendant launch; anything else returns None.

    Admitted forms (exactly what the unchanged compatibility closure launches):
      [this interpreter, -B..., -m, campaign_tool.<dotted module>, args...]
      [this interpreter, -B..., <absolute .py script under repo/campaign_tool>, args...]
      [this interpreter, -B..., <this runner>, PROBE_FLAG, args...]   (inert self-probe)
    """
    if not isinstance(argv, (list, tuple)):
        return None
    try:
        argv = [os.fsdecode(os.fspath(a)) for a in argv]
    except TypeError:
        return None
    if not argv or os.path.realpath(argv[0]) != os.path.realpath(sys.executable):
        return None
    index = 1
    while index < len(argv) and argv[index] == "-B":
        index += 1
    rest = argv[index:]
    if len(rest) >= 2 and rest[0] == "-m":
        parts = rest[1].split(".")
        if parts[0] == "campaign_tool" and len(parts) > 1 and all(p.isidentifier() for p in parts):
            return {"kind": "module", "target": rest[1], "args": rest[2:], "form": rest}
        return None
    if rest and os.path.isabs(rest[0]) and rest[0].endswith(".py"):
        script = Path(os.path.realpath(rest[0]))
        repo = Path(repo).resolve()
        if script.is_file() and script.is_relative_to(repo / "campaign_tool"):
            return {"kind": "script", "target": rest[0], "args": rest[1:], "form": rest}
        if script == (repo / RUNNER_FILE).resolve() and rest[1:2] == [PROBE_FLAG]:
            return {"kind": "probe", "target": rest[0], "args": rest[1:], "form": rest}
    return None


def install_guards(run_dir, sentinel_root, allow_processes, violations_fd=None):
    """Audit-hook sentinels: network, model/HTTP clients, process launch, live files, writes.

    With allow_processes, only admitted Python descendants (descendant_target) may launch,
    and each is rewritten to start through this runner's guarded bootstrap, which installs
    the same sentinels before any application import and streams every refusal (caught or
    not) to a per-descendant violations file that the launching child verifies.
    Every other launch form and every direct fork_exec stays refused.
    """
    import threading
    root = Path(run_dir).resolve()
    sentinel = Path(sentinel_root).resolve() if sentinel_root else None
    readable = [root, REPO.resolve()] + [Path(p).resolve() for p in (
        sys.prefix, sys.exec_prefix, sys.base_prefix, sys.base_exec_prefix,
        os.path.dirname(os.path.realpath(sys.executable))) if p]
    readable += [Path(p).resolve() for p in sys.path if p and os.path.isabs(p)]
    record = {"violations": [], "processes": [], "probes": [], "descendants": [],
              "descendant_dir": str(root / "descendants")}
    write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
    probing = []
    local = threading.local()

    def approved():
        return allow_processes and not probing and getattr(local, "approved", 0) > 0

    def resolve(path, dir_fd=None):
        if isinstance(path, int):
            return None
        try:
            path = os.fsdecode(os.fspath(path))
        except TypeError:
            return False
        if path in ("", ":memory:", os.devnull):
            return None
        if path.startswith("file:"):
            path = path[5:].split("?", 1)[0]
        if not os.path.isabs(path) and isinstance(dir_fd, int) and not isinstance(dir_fd, bool) and dir_fd >= 0:
            try:
                path = os.path.join(os.readlink("/proc/self/fd/%d" % dir_fd), path)
            except OSError:
                return False
        return Path(os.path.realpath(os.path.abspath(path)))

    def inside(path, dir_fd=None):
        target = resolve(path, dir_fd)
        return target is None or (target is not False and target.is_relative_to(root))

    def live(path, dir_fd=None):
        """A read below the sentinel root (the real home) outside admitted roots."""
        target = resolve(path, dir_fd)
        if target is None or sentinel is None:
            return False
        if target is False:
            return True
        return target.is_relative_to(sentinel) and not any(target.is_relative_to(r) for r in readable)

    def refuse(event, detail):
        entry = {"event": event, "detail": repr(detail)[:240]}
        if probing:
            record["probes"].append(entry)
        else:
            record["violations"].append(entry)
            if violations_fd is not None:  # survives application code catching the refusal
                os.write(violations_fd, (json.dumps(entry, sort_keys=True) + "\n").encode())
        raise PermissionError("fa52 acceptance guard refused " + event)

    mutations = {"os.mkdir": ((0, 2),), "os.remove": ((0, 1),), "os.rmdir": ((0, 1),),
                 "os.chmod": ((0, 2),), "os.chown": ((0, 3),), "os.utime": ((0, 3),),
                 "os.truncate": ((0, None),), "shutil.rmtree": ((0, 1),),
                 "os.rename": ((0, 2), (1, 3)), "os.replace": ((0, 2), (1, 3)), "os.link": ((0, 2), (1, 3)),
                 "os.symlink": ((1, 2),), "shutil.copyfile": ((1, None),), "shutil.move": ((0, None), (1, None))}
    launches = ("subprocess.Popen", "os.system", "os.posix_spawn", "os.spawn", "os.exec", "os.fork",
                "os.forkpty", "pty.spawn")

    def hook(event, args):
        if event == "fa52.sentinel_probe":  # inert presence check used by descendant bootstraps
            raise PermissionError("fa52 acceptance guard present")
        if event.startswith("socket.") and event not in ("socket.__new__", "socket.sethostname"):
            refuse(event, args)
        elif event in ("urllib.Request", "http.client.connect", "webbrowser.open", "ftplib.connect",
                       "smtplib.connect", "imaplib.open", "poplib.connect", "nntplib.connect"):
            refuse(event, args)
        elif event in launches:
            if approved():
                record["processes"].append({"event": event, "detail": repr(args)[:240]})
            else:
                refuse(event, args)
        elif event == "open":
            path, mode, flags = (tuple(args) + (None, None, None))[:3]
            writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                isinstance(flags, int) and flags & write_flags)
            if writing and not inside(path):
                refuse(event, path)
            if live(path):
                refuse("live-file:" + event, path)
        elif event in ("os.listdir", "os.scandir"):
            if args and args[0] is not None and live(args[0]):
                refuse("live-file:" + event, args[0])
        elif event == "sqlite3.connect":
            if not inside(args[0]) or live(args[0]):
                refuse(event, args[0])
        elif event in mutations:
            for index, fd_index in mutations[event]:
                if index >= len(args):
                    continue
                dir_fd = args[fd_index] if fd_index is not None and fd_index < len(args) else None
                if not inside(args[index], dir_fd):
                    refuse(event, args[index])

    sys.addaudithook(hook)

    import _posixsubprocess
    original_fork_exec = _posixsubprocess.fork_exec

    def gated_fork_exec(*a, **k):
        """fork_exec is not audited itself (multiprocessing spawn calls it directly)."""
        if not approved():
            refuse("_posixsubprocess.fork_exec", a[0] if a else None)
        return original_fork_exec(*a, **k)

    _posixsubprocess.fork_exec = gated_fork_exec
    subprocess._fork_exec = gated_fork_exec

    original_init = subprocess.Popen.__init__

    def guarded_init(self, args, *positional, **kwargs):
        target = None
        if not positional and kwargs.get("executable") is None and not kwargs.get("shell"):
            target = descendant_target(args)
        if target is None:
            refuse("unsupported-descendant", args)
        if probing or not allow_processes:
            refuse("subprocess.Popen", args)
        token = secrets.token_hex(8)
        ddir = Path(record["descendant_dir"])
        ddir.mkdir(mode=0o700, exist_ok=True)
        rewritten = [sys.executable, "-B", str(REPO / RUNNER_FILE), DESCENDANT_FLAG, "--run-dir", str(root),
                     "--sentinel-root", str(sentinel) if sentinel else "", "--descendant-dir", str(ddir),
                     "--token", token, "--", *target["form"]]
        record["descendants"].append({"token": token, "kind": target["kind"], "target": target["target"],
                                      "dir": str(ddir), "original_argv": [os.fsdecode(a) for a in args],
                                      "cwd": str(kwargs.get("cwd") or os.getcwd())})
        local.approved = getattr(local, "approved", 0) + 1
        try:
            original_init(self, rewritten, **kwargs)
        finally:
            local.approved -= 1

    if allow_processes:
        subprocess.Popen.__init__ = guarded_init
    return record, probing


def self_check(guards, probing, run_dir, sentinel_root):
    """Inert probes: each must be refused by the hook before any syscall could act."""
    import socket
    token = secrets.token_hex(6)
    checks = {}
    probing.append(True)
    try:
        for label, action in (
                ("network_resolve", lambda: socket.getaddrinfo("fa52-%s.invalid" % token, 443)),
                ("network_connect", lambda: socket.create_connection(("192.0.2.1", 9), timeout=0.01)),
                ("escape_write", lambda: open(os.path.join(os.sep, "fa52-escape-" + token, "probe"), "w")),
                ("process_launch", lambda: subprocess.run(["fa52-nonexistent-" + token], check=False)),
                ("live_file_read", lambda: open(os.path.join(sentinel_root, "fa52-sentinel-" + token), "rb"))):
            before = len(guards["probes"])
            try:
                action()
                checks[label] = False
            except PermissionError:
                checks[label] = len(guards["probes"]) == before + 1
            except OSError:
                checks[label] = False
    finally:
        probing.clear()
    positive = Path(run_dir) / "guard-positive.txt"
    positive.write_text("in-root write allowed\n")
    checks["in_root_write"] = positive.read_text() == "in-root write allowed\n"
    return checks


def path_constants(run_dir, sentinel_root):
    """Absolute-path module constants and bound defaults in loaded repository modules."""
    import inspect
    import types
    root = Path(run_dir).resolve()
    sentinel = Path(sentinel_root).resolve()
    found, unsafe = [], []

    def consider(owner, value):
        if isinstance(value, (str, Path)) and str(value).startswith(os.sep) and len(str(value)) < 4096:
            target = Path(os.path.abspath(str(value)))
            entry = {"owner": owner, "value_sha256": sha256_bytes(str(value).encode()),
                     "inside_run": target.is_relative_to(root), "inside_repo": target.is_relative_to(REPO)}
            found.append(entry)
            if target.is_relative_to(sentinel) and not (entry["inside_run"] or entry["inside_repo"]):
                unsafe.append(entry)

    for name, module in sorted(sys.modules.items()):
        path = getattr(module, "__file__", None)
        if not path or not Path(path).resolve().is_relative_to(REPO) or not name.startswith(("campaign_tool", "tests")):
            continue
        for attr, value in vars(module).items():
            if attr.startswith("__"):
                continue
            consider("%s.%s" % (name, attr), value)
            functions = []
            if isinstance(value, types.FunctionType):
                functions.append((attr, value))
            elif inspect.isclass(value) and getattr(value, "__module__", None) == name:
                functions += [(attr + "." + k, v) for k, v in vars(value).items() if isinstance(v, types.FunctionType)]
            for label, function in functions:
                for default in (function.__defaults__ or ()) + tuple((function.__kwdefaults__ or {}).values()):
                    consider("%s.%s()" % (name, label), default)
    return {"absolute_path_values": len(found), "owners": sorted({entry["owner"] for entry in found}),
            "under_real_home_outside_run_or_repo": unsafe}


class Recorder:
    """unittest result that keeps one outcome per test id (subtest failures included)."""

    @staticmethod
    def build(stream):
        import unittest

        class Result(unittest.TextTestResult):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.outcomes = {}
                self.failed_subtests = 0

            def addSuccess(self, test):
                super().addSuccess(test)
                self.outcomes.setdefault(test.id(), "pass")

            def addFailure(self, test, err):
                super().addFailure(test, err)
                self.outcomes[test.id()] = "fail"

            def addError(self, test, err):
                super().addError(test, err)
                self.outcomes[test.id()] = "error"

            def addSkip(self, test, reason):
                super().addSkip(test, reason)
                self.outcomes[test.id()] = "skip"

            def addSubTest(self, test, subtest, err):
                super().addSubTest(test, subtest, err)
                if err is not None:
                    self.failed_subtests += 1
                    failed = issubclass(err[0], test.failureException)
                    if self.outcomes.get(test.id()) != "error":
                        self.outcomes[test.id()] = "fail" if failed else "error"

        return unittest.TextTestRunner(stream=stream, verbosity=2, resultclass=Result)


def run_tests(names, stream):
    import unittest
    stream.write("=== %s ===\n" % ", ".join(names))
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(name) for name in names)
    result = Recorder.build(stream).run(suite)
    return {"names": list(names), "tests_run": result.testsRun, "failures": len(result.failures),
            "errors": len(result.errors), "skipped": len(result.skipped),
            "expected_failures": len(result.expectedFailures),
            "unexpected_successes": len(result.unexpectedSuccesses),
            "failed_subtests": result.failed_subtests, "outcomes": result.outcomes,
            "successful": result.wasSuccessful()}


def negative_control(stream):
    """A4: only the original equality expression replaces the candidate budget gate."""
    from unittest import mock
    from campaign_tool.records.intake import native_receipt
    prefix = TEST_MODULE + "."
    candidate_gate = native_receipt._budget_matches
    calls = []

    def original_equality(supplied, recomputed):
        calls.append(1)
        return supplied == recomputed  # unchanged bind: receipt["budget"] == plan.budget

    with mock.patch.object(native_receipt, "_budget_matches", original_equality):
        broken = run_tests([prefix + n for n in DETECT], stream)
        broken_positive = run_tests([prefix + POSITIVE], stream)
    restored = native_receipt._budget_matches is candidate_gate
    candidate = run_tests([prefix + n for n in DETECT + [POSITIVE]], stream)
    checks = {
        "control_reached_bind": len(calls) > 0,
        "bool_regression_detects_acceptance": broken["outcomes"].get(prefix + DETECT[0]) == "fail",
        "float_regression_detects_acceptance": broken["outcomes"].get(prefix + DETECT[1]) == "fail",
        "control_failures_not_errors": broken["errors"] == 0 and broken["failures"] >= 2
        and sorted(broken["outcomes"]) == sorted(prefix + n for n in DETECT),
        "reordered_positive_green_under_control": clean_counts(broken_positive) and broken_positive["tests_run"] == 1,
        "candidate_gate_restored": restored,
        "candidate_all_green": clean_counts(candidate) and candidate["tests_run"] == 3,
    }
    return {"broken_control": broken, "broken_positive": broken_positive, "candidate": candidate,
            "control_calls": len(calls), "checks": checks, "tests_run": candidate["tests_run"],
            "failures": candidate["failures"], "errors": candidate["errors"], "skipped": candidate["skipped"],
            "expected_failures": 0, "unexpected_successes": 0, "pass": all(checks.values())}


def _write_json_exclusive(path, data):
    with open(path, "x") as stream:
        stream.write(json.dumps(data, indent=2, sort_keys=True, default=str) + "\n")


def descendant_main(argv):
    """Guarded bootstrap for one admitted descendant: sentinels first, then the real target.

    argv: --run-dir R --sentinel-root S --descendant-dir D --token T -- <original form>
    Preserves module (-m) / script semantics: sys.argv, sys.path[0], __main__ run name,
    exit status and stdout/stderr. Nothing from the application is imported before the
    guards and the start record exist; a missing guard or preloaded module fails closed.
    """
    if "--" not in argv:
        return 70
    split = argv.index("--")
    options, form = argv[:split], argv[split + 1:]
    keys = ["--run-dir", "--sentinel-root", "--descendant-dir", "--token"]
    if len(options) != 8 or options[0::2] != keys:
        return 70
    values = dict(zip(options[0::2], options[1::2]))
    run_dir = Path(values["--run-dir"]).resolve()
    ddir = Path(values["--descendant-dir"]).resolve()
    token = values["--token"]
    sentinel_root = values["--sentinel-root"] or None
    if not ddir.is_relative_to(run_dir) or not (len(token) == 16 and all(c in "0123456789abcdef" for c in token)):
        return 70
    preloaded = sorted(m for m in sys.modules if m.split(".")[0] in APP_PACKAGES)
    sys.dont_write_bytecode = True
    import tempfile
    if (run_dir / "tmp").is_dir():
        tempfile.tempdir = str(run_dir / "tmp")
        os.environ["TMPDIR"] = str(run_dir / "tmp")
    if (run_dir / "home").is_dir():
        for key in ("HOME", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME"):
            os.environ[key] = str(run_dir / "home")
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    violations_fd = os.open(ddir / (token + ".violations.jsonl"),
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND, 0o600)
    install_guards(run_dir, sentinel_root, False, violations_fd=violations_fd)
    try:
        sys.audit("fa52.sentinel_probe")
        guards_active = False
    except PermissionError:
        guards_active = True
    target = descendant_target([sys.executable, *form])
    start = {"token": token, "pid": os.getpid(), "cwd": os.getcwd(), "form": form,
             "kind": target and target["kind"], "target": target and target["target"],
             "guards_active_before_target": guards_active, "app_modules_before_guard": preloaded,
             "app_modules_at_start": sorted(m for m in sys.modules if m.split(".")[0] in APP_PACKAGES),
             "bytecode_disabled": sys.dont_write_bytecode, "tempdir": tempfile.gettempdir(),
             "home": os.path.expanduser("~")}
    _write_json_exclusive(ddir / (token + ".start.json"), start)
    if target is None or not guards_active or preloaded or start["app_modules_at_start"]:
        return 70
    status = "exception"
    try:
        if target["kind"] == "module":
            sys.argv = [target["target"], *target["args"]]
            sys.path[0] = os.getcwd()  # what ``python -m`` puts first
            import runpy
            runpy.run_module(target["target"], run_name="__main__", alter_sys=True)
        else:
            script = target["target"]
            sys.argv = [script, *target["args"]]
            sys.path[0] = os.path.dirname(os.path.realpath(script))  # what ``python script.py`` puts first
            import runpy
            runpy.run_path(script, run_name="__main__")
        status = 0
    except SystemExit as stop:
        status = stop.code
        raise
    finally:
        try:
            _write_json_exclusive(ddir / (token + ".end.json"), {"token": token, "exit": repr(status)})
        except OSError:
            pass
    return 0


def descendant_probe_main(args):
    """Inert self-probe run through the guarded bootstrap: audit events only, all caught."""
    try:
        sys.audit("fa52.sentinel_probe")
        present = False
    except PermissionError:
        present = True
    caught = {}
    token = secrets.token_hex(6)
    for label, event, event_args in (
            ("network", "socket.getaddrinfo", ("fa52-probe-%s.invalid" % token, 443, 0, 0, 0, 0)),
            ("process", "subprocess.Popen", ("fa52-nonexistent-" + token, ["fa52-nonexistent-" + token], None, None)),
            ("escape_write", "open", (os.path.join(os.sep, "fa52-escape-" + token, "probe"), "w", 0))):
        try:
            sys.audit(event, *event_args)  # reports the event to hooks; performs no operation
            caught[label] = False
        except PermissionError:  # deliberately swallowed, as application code might
            caught[label] = True
    print(json.dumps({"sentinel_present": present, "caught_refusals": caught, "argv": sys.argv,
                      "main_name": __name__}, sort_keys=True))
    sys.stdout.flush()
    return int(args[0]) if args and args[0].isdigit() else 0


EXPECTED_PROBE_EVENTS = ["socket.getaddrinfo", "subprocess.Popen", "open"]


def read_descendant(entry):
    ddir = Path(entry["dir"])
    token = entry["token"]
    out = {"token": token, "kind": entry["kind"], "target": entry["target"]}
    try:
        start = json.loads((ddir / (token + ".start.json")).read_text())
    except (OSError, ValueError):
        start = None
    out["start"] = start
    try:
        lines = (ddir / (token + ".violations.jsonl")).read_text().splitlines()
        out["violations"] = [json.loads(line) for line in lines if line.strip()]
        out["violations_file_present"] = True
    except (OSError, ValueError):
        out["violations"] = None
        out["violations_file_present"] = False
    end = ddir / (token + ".end.json")
    out["end"] = json.loads(end.read_text()) if end.is_file() else None
    out["bootstrapped"] = (isinstance(start, dict) and start.get("token") == token
                           and start.get("guards_active_before_target") is True
                           and start.get("app_modules_before_guard") == []
                           and start.get("app_modules_at_start") == []
                           and start.get("kind") == entry["kind"] and start.get("target") == entry["target"]
                           and start.get("bytecode_disabled") is True)
    return out


def verify_descendants(guards, require_forms=()):
    """Every admitted launch bootstrapped under guards; no descendant refusal, even if caught."""
    ddir = Path(guards["descendant_dir"])
    entries = [e for e in guards["descendants"] if Path(e["dir"]) == ddir]
    results = [read_descendant(e) for e in entries]
    expected = {e["token"] + suffix for e in entries for suffix in (".start.json", ".violations.jsonl",
                                                                    ".end.json")}
    present = sorted(p.name for p in ddir.iterdir()) if ddir.is_dir() else []
    unexpected_files = sorted(set(present) - expected)
    forms = sorted({e["kind"] for e in entries})
    checks = {"all_bootstrapped_under_guards": all(r["bootstrapped"] for r in results),
              "no_descendant_violations": all(r["violations"] == [] for r in results),
              "no_unexpected_descendant_files": not unexpected_files,
              "required_forms_seen": all(f in forms for f in require_forms)}
    return {"launched": len(entries), "forms": forms, "unexpected_files": unexpected_files,
            "descendants": results, "checks": checks, "pass": all(checks.values())}


def descendant_probes(guards, probing, run_dir):
    """A5 descendant coverage: unsupported forms refused pre-launch; admitted form guarded."""
    token = secrets.token_hex(6)
    runner = str(REPO / RUNNER_FILE)
    checks = {}
    refusals = {}
    unsupported = (
        ("interpreter_flag", [sys.executable, "-E", runner, PROBE_FLAG, "0"], {}),
        ("module_outside_application", [sys.executable, "-B", "-m", "json.tool"], {}),
        ("script_outside_application", [sys.executable, "-B", str(REPO / "tools" / "check_public_tree.py")], {}),
        ("relative_script", [sys.executable, "-B", "campaign_tool/records/intake/folder.py"], {}),
        ("other_executable", ["fa52-nonexistent-" + token], {}),
        ("shell_string", "fa52-nonexistent-" + token, {"shell": True}),
        ("explicit_executable", [sys.executable, "-B", runner, PROBE_FLAG, "0"], {"executable": sys.executable}))
    probing.append(True)
    try:
        for label, argv, kwargs in unsupported:
            before, launched = len(guards["probes"]), len(guards["descendants"])
            try:
                subprocess.run(argv, check=False, capture_output=True, **kwargs)
                checks["refused_" + label] = False
            except PermissionError:
                last = guards["probes"][-1] if len(guards["probes"]) == before + 1 else {}
                refusals[label] = last
                checks["refused_" + label] = (last.get("event") == "unsupported-descendant"
                                              and len(guards["descendants"]) == launched)
        import _posixsubprocess
        before = len(guards["probes"])
        try:  # multiprocessing-style direct fork_exec: refused before the real call runs
            _posixsubprocess.fork_exec([b"fa52-nonexistent-" + token.encode()])
            checks["refused_direct_fork_exec"] = False
        except PermissionError:
            checks["refused_direct_fork_exec"] = (len(guards["probes"]) == before + 1 and
                                                  guards["probes"][-1]["event"] == "_posixsubprocess.fork_exec")
    finally:
        probing.clear()
    default_dir = guards["descendant_dir"]
    guards["descendant_dir"] = str(Path(run_dir).resolve() / "descendant-probes")
    before = len(guards["descendants"])
    try:
        done = subprocess.run([sys.executable, "-B", runner, PROBE_FLAG, "7"], cwd=str(REPO),
                              capture_output=True, check=False)
    finally:
        guards["descendant_dir"] = default_dir
    entries = guards["descendants"][before:]
    try:
        report = json.loads(done.stdout)
    except ValueError:
        report = {}
    detail = read_descendant(entries[0]) if len(entries) == 1 else None
    events = [v.get("event") for v in (detail or {}).get("violations") or []]
    checks.update(
        admitted_probe_launched_once=len(entries) == 1,
        probe_exit_status_preserved=done.returncode == 7,
        probe_stdout_preserved=report.get("main_name") == "__main__" and report.get("argv", [None])[0] == runner,
        probe_sentinel_present_before_target=report.get("sentinel_present") is True
        and bool(detail) and detail["bootstrapped"],
        probe_refusals_raised_in_descendant=report.get("caught_refusals") == {
            "network": True, "process": True, "escape_write": True},
        caught_descendant_refusals_propagated=events == EXPECTED_PROBE_EVENTS)
    return {"refusals": refusals, "probe_exit": done.returncode, "probe_stdout": done.stdout.decode(errors="replace"),
            "probe_stdout_sha256": sha256_bytes(done.stdout), "probe_stderr_sha256": sha256_bytes(done.stderr),
            "probe_descendant": detail, "checks": checks, "pass": all(checks.values())}


def child_main(args):
    run_dir = Path(args.run_dir).resolve()
    tmp = run_dir / "tmp"
    tmp.mkdir(exist_ok=True)
    import tempfile
    tempfile.tempdir = str(tmp)
    sys.dont_write_bytecode = True
    allow = args.child == "descendant-probe" or (
        args.child == "unittest" and list(args.module) == list(COMPATIBILITY))
    guards, probing = install_guards(run_dir, args.sentinel_root, allow)
    checks = self_check(guards, probing, run_dir, args.sentinel_root) if args.sentinel_root else {}
    if str(REPO) in sys.path:
        sys.path.remove(str(REPO))
    sys.path.insert(0, str(REPO))
    environment = {"dont_write_bytecode": sys.dont_write_bytecode, "tempdir": tempfile.gettempdir(),
                   "home": os.path.expanduser("~"), "cwd": os.getcwd(), "repo": str(REPO)}
    checks["bytecode_disabled"] = sys.dont_write_bytecode is True
    checks["tempdir_redirected"] = Path(tempfile.gettempdir()).resolve().is_relative_to(run_dir)
    checks["home_redirected"] = Path(os.path.expanduser("~")).resolve().is_relative_to(run_dir)
    stream = sys.stderr
    record = {"mode": args.child, "environment": environment, "guard_checks": checks,
              "descendants_admitted": allow}
    if allow:
        # Inert descendant coverage before any application import or test.
        record["descendant_probes"] = descendant_probes(guards, probing, run_dir)
        checks["descendant_probes"] = record["descendant_probes"]["pass"]
        checks["no_application_import_before_tests"] = not any(
            m.split(".")[0] in APP_PACKAGES for m in sys.modules)
    if args.child == "negative-control":
        record.update(negative_control(stream))
    elif args.child == "descendant-probe":
        record["pass"] = True
    else:
        record.update(run_tests(args.module, stream))
        record["pass"] = record.pop("successful") and clean_counts(record)
    if allow:
        required = ("module", "script") if args.child == "unittest" else ()
        record["descendant_verification"] = verify_descendants(guards, required)
        record["pass"] = record["pass"] is True and record["descendant_verification"]["pass"]
    if args.child == "isolation-probe":
        from campaign_tool.records.intake import mail_delta, mail_wire, native_receipt
        test_module = sys.modules[TEST_MODULE]
        record["module_paths"] = {m.__name__: getattr(m, "__file__", None)
                                  for m in (native_receipt, mail_delta, mail_wire, test_module)}
        record["path_constants"] = path_constants(run_dir, args.sentinel_root)
        record["pass"] = record["pass"] and not record["path_constants"]["under_real_home_outside_run_or_repo"]
    record["guard_violations"] = guards["violations"]
    record["guard_probe_refusals"] = guards["probes"]
    record["processes_recorded"] = guards["processes"]
    record["pass"] = (record["pass"] is True and not guards["violations"] and bool(checks)
                      and all(checks.values()))
    Path(args.result_json).write_text(json.dumps(record, indent=2, sort_keys=True, default=str) + "\n")
    return 0 if record["pass"] else 1


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == [DESCENDANT_FLAG]:  # guarded bootstrap; must precede any application import
        return descendant_main(argv[1:])
    if argv[:1] == [PROBE_FLAG]:
        return descendant_probe_main(argv[1:])
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--case", choices=CASES)
    parser.add_argument("--output-parent")
    parser.add_argument("--child", choices=("unittest", "negative-control", "isolation-probe", "descendant-probe"))
    parser.add_argument("--run-dir")
    parser.add_argument("--result-json")
    parser.add_argument("--sentinel-root")
    parser.add_argument("--module", action="append", default=[])
    args = parser.parse_args(argv)
    if args.child:
        if not (args.run_dir and args.result_json and args.sentinel_root):
            parser.error("--child requires --run-dir, --result-json and --sentinel-root")
        if args.child not in ("negative-control", "descendant-probe") and not args.module:
            parser.error("--child %s requires --module" % args.child)
        return child_main(args)
    if not args.case or not args.output_parent:
        parser.error("--case and --output-parent are required")
    return parent_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
