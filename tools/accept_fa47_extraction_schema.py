"""FA47 acceptance runner: adapter schema objects verified beyond the stored marker.

Usage: accept_fa47_extraction_schema.py --case CASE --output-parent DIR

Each invocation validates DIR, then writes one new exclusive run directory below
it and exits 0 only when every check of that case executed and passed. A pass is
REVIEW REQUIRED machine evidence, not semantic, legal, human, stage or release
acceptance. The parent never imports repository code; test cases run in a
file-launched child interpreter that redirects tempfile/TMPDIR and installs
offline, no-spawn and write/database sentinels before any application import.
All paths are derived from this file or the supplied output parent.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import sys

sys.dont_write_bytecode = True

REPO = Path(__file__).resolve().parents[1]
PRODUCT = "campaign_tool/records/extraction_ledger.py"
TEST_MODULE = "tests.records.test_fa47_extraction_schema"
AUTHORED = (PRODUCT, "tests/records/test_fa47_extraction_schema.py", "tools/accept_fa47_extraction_schema.py")
COMPATIBILITY = ("tests.records.test_extraction_ledger", "tests.records.test_extraction_validation")
REFERENCED = ("tests/records/test_extraction_ledger.py", "tests/records/test_extraction_validation.py",
              "tests/records/ocr_fixtures.py", "campaign_tool/records/ledger/store.py",
              "campaign_tool/records/ledger/migrations/v001.py", "campaign_tool/records/extraction_routes.py",
              ".github/workflows/records.yml", "tools/check_secret_report.py", "tools/check_public_tree.py")
CASES = ("behavior", "compatibility", "negative-control", "isolation", "full-ci", "credentials")
# A4 deciding regressions: they must fail against the marker-only control.
DECIDING = (TEST_MODULE + ".A2MarkerMatchesButObjectsDiffer.test_missing_required_trigger_refused",
            TEST_MODULE + ".A5EnrollmentBoundary.test_enrollment_rejects_corrupted_adapter_before_writes")
BEHAVIOR_MAP = {
    "A1": ("A1FreshAndReuse", "A1ExistingRows"),
    "A2": ("A2MarkerMatchesButObjectsDiffer",),
    "A3": ("A3BoundaryAndNoRepair", "A3ImmutableHistoryStillEnforced"),
    "A5": ("A5EnrollmentBoundary",),
}
FULL_CI = (
    ("sd15-discovery", ["-m", "unittest", "discover", "-s", "tests", "-t", "."]),
    ("records-yml-s3a", ["-B", "-W", "error::ResourceWarning", "-m", "unittest",
                         "tests.records.test_mail_resolution_census", "tests.records.test_mail_adoption",
                         "tests.records.test_original_reconciliation"]),
    ("records-yml-discovery", ["-B", "-W", "error::ResourceWarning", "-m", "unittest", "discover", "-v"]),
)
# Inventoried existing child protocol: the unchanged extraction route launches its
# own decoder worker with the same interpreter. Nothing else may spawn.
WORKER_PREFIX = ("-B", "-m", "campaign_tool.records.extraction_routes", "_worker")
SCRUBBED_ENV = re.compile(r"^(PYTHON\w*|LD_\w+|RECORDS_WP1_SOURCE_ROOT)$")
RAN = re.compile(rb"^Ran (\d+) tests? in ", re.M)
SKIPPED = re.compile(rb"skipped=(\d+)")
FAILED = re.compile(rb"^FAILED \(([^)]*)\)", re.M)


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    return sha256_bytes(Path(path).read_bytes())


class UnsafeRoot(Exception):
    pass


def checked_output_parent(value, repo=REPO):
    """Absolute, link-free, outside the source tree and not shared-writable."""
    if not value or not os.path.isabs(value):
        raise UnsafeRoot("output_parent_not_absolute")
    path = Path(os.path.normpath(value))
    for item in (path, *path.parents):
        if item.is_symlink():
            raise UnsafeRoot("output_parent_linked_ancestor")
    if path == repo or path.is_relative_to(repo) or repo.is_relative_to(path):
        raise UnsafeRoot("output_parent_overlaps_source")
    if path.exists():
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o002:
            raise UnsafeRoot("output_parent_not_owned_directory")
    else:
        if not path.parent.is_dir():
            raise UnsafeRoot("output_parent_ancestor_missing")
        path.mkdir(mode=0o700)
    return path


def git(*args, cwd=REPO):
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=False)
    return done.returncode, done.stdout


def identity(root=REPO):
    _, head = git("rev-parse", "HEAD", cwd=root)
    _, status = git("status", "--porcelain=v1", "--untracked-files=all", cwd=root)
    return {"head": head.decode().strip(), "status_porcelain": status.decode(), "clean_tree": status == b"",
            "file_sha256": {name: sha256_file(root / name) for name in AUTHORED + REFERENCED}}


def unittest_counts(stream):
    ran = RAN.findall(stream)
    skipped = SKIPPED.findall(stream)
    failed = FAILED.findall(stream)
    return {"tests_run": int(ran[-1]) if ran else None,
            "skipped": int(skipped[-1]) if skipped else 0,
            "ok_line": bool(re.search(rb"^OK\b", stream, re.M)),
            "failed_line": failed[-1].decode() if failed else None}


class Run:
    def __init__(self, parent, case):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        self.dir = parent / ("%s-%s-%d-%s" % (case, stamp, os.getpid(), secrets.token_hex(4)))
        self.dir.mkdir(mode=0o700)
        self.tmp = self.dir / "tmp"
        self.tmp.mkdir(mode=0o700)
        self.children = []

    def child(self, label, argv, cwd=REPO, stdout_name=None, timeout=3000):
        env = {k: v for k, v in os.environ.items() if not SCRUBBED_ENV.match(k)}
        env["TMPDIR"] = str(self.tmp)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        argv = [str(a) for a in argv]
        try:
            done = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, check=False, timeout=timeout)
            code, out, err = done.returncode, done.stdout, done.stderr
        except subprocess.TimeoutExpired as expired:
            code, out, err = None, expired.stdout or b"", (expired.stderr or b"") + b"\nTIMEOUT\n"
        out_path = self.dir / (stdout_name or label + ".stdout")
        err_path = self.dir / (label + ".stderr")
        out_path.write_bytes(out)
        err_path.write_bytes(err)
        entry = {"label": label, "argv": argv, "cwd": str(cwd), "interpreter": sys.executable, "exit": code,
                 "stdout": str(out_path), "stdout_sha256": sha256_bytes(out),
                 "stderr": str(err_path), "stderr_sha256": sha256_bytes(err)}
        self.children.append(entry)
        return entry, out, err

    def test_child(self, label, mode, extra=(), source=REPO):
        result_path = self.dir / (label + "-result.json")
        entry, _, _ = self.child(label, [sys.executable, "-B", source / "tools" / Path(__file__).name, "--child", mode,
                                         "--run-dir", self.dir, "--result-json", result_path, *extra], cwd=source)
        try:
            record = json.loads(result_path.read_text())
        except (OSError, ValueError):
            record = {"pass": False, "error": "child_result_missing_or_invalid"}
        record["child_exit"] = entry["exit"]
        record["pass"] = record.get("pass") is True and entry["exit"] == 0
        return record


def modules_args(names):
    extra = []
    for name in names:
        extra += ["--module", name]
    return extra


def marker_only_baseline(run):
    """Most recent committed revision of the product whose bytes differ from the candidate."""
    candidate = (REPO / PRODUCT).read_bytes()
    code, revs = git("rev-list", "HEAD", "--", PRODUCT)
    if code != 0:
        return None
    for rev in revs.decode().split():
        code, blob = git("show", rev + ":" + PRODUCT)
        if code == 0 and blob != candidate:
            target = run.dir / "baseline" / "extraction_ledger_marker_only_baseline.py"
            target.parent.mkdir(mode=0o700)
            target.write_bytes(blob)
            return {"revision": rev, "path": str(target), "sha256": sha256_bytes(blob),
                    "verifies_objects": b"_verify_adapter_schema" in blob}
    return None


def case_behavior(run):
    record = run.test_child("behavior", "unittest", modules_args([TEST_MODULE]))
    executed = record.get("test_ids", [])
    mapping = {}
    for label, classes in BEHAVIOR_MAP.items():
        ids = [t for t in executed if any(".%s." % c in t for c in classes)]
        mapping[label] = ids
    record["requirement_map"] = mapping
    record["pass"] = record["pass"] and all(mapping.values())
    return record


def case_negative_control(run):
    baseline = marker_only_baseline(run)
    if baseline is None or baseline["verifies_objects"]:
        return {"pass": False, "error": "marker_only_baseline_unavailable", "baseline": baseline}
    broken = run.test_child("control-broken", "control", ["--baseline", baseline["path"]])
    restored = run.test_child("control-restored", "unittest", modules_args([TEST_MODULE]))
    failed = set(broken.get("failure_ids", []))
    checks = {
        "deciding_regressions_fail_on_marker_only_control": set(DECIDING) <= failed,
        "deciding_failures_are_assertions_not_errors": broken.get("errors") == 0,
        "control_population_nonzero": (broken.get("tests_run") or 0) > 0,
        "control_child_reported": broken.get("mode") == "control" and broken.get("guard_violations") == [],
        "restored_candidate_passes": restored["pass"] is True,
        "restored_population_nonzero": (restored.get("tests_run") or 0) > 0,
    }
    return {"baseline": baseline, "broken": broken, "restored": restored, "deciding": list(DECIDING),
            "checks": checks, "pass": all(checks.values())}


def copy_source(run):
    """Second complete source copy of the working tree (tracked plus unignored files)."""
    code, listing = git("ls-files", "-z", "--cached", "--others", "--exclude-standard")
    if code != 0:
        return None
    target = run.dir / "source-copy"
    copied = 0
    for raw in listing.split(b"\0"):
        if not raw:
            continue
        name = os.fsdecode(raw)
        source = REPO / name
        if source.is_symlink() or not source.is_file():
            continue
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        copied += 1
    return target, copied


def case_isolation(run):
    checks = {}
    # Unsafe evidence roots are refused before any write.
    probe = run.dir / "root-probe"
    probe.mkdir(mode=0o700)
    (probe / "link").symlink_to(probe)
    refusals = {}
    for label, value in (("relative", "relative/evidence"), ("linked", str(probe / "link" / "child")),
                         ("inside_source", str(REPO / "fa47-evidence"))):
        try:
            checked_output_parent(value)
            refusals[label] = None
        except UnsafeRoot as error:
            refusals[label] = str(error)
    checks["unsafe_roots_refused"] = all(refusals.values()) and not (REPO / "fa47-evidence").exists() \
        and not (probe / "child").exists()
    # Committed test/helper text stays portable.
    text = b"".join((REPO / name).read_bytes() for name in AUTHORED[1:])
    # Split literals so this check does not match its own source text.
    banned = [p for p in (b"/ho" b"me/", b"agent" b"-work", b"/Us" b"ers/", b"/t" b"mp/") if p in text]
    checks["portable_committed_sources"] = banned == []
    copied = copy_source(run)
    if copied is None:
        return {"pass": False, "error": "source_copy_failed"}
    copy_root, count = copied
    copy_hashes = {name: sha256_file(copy_root / name) for name in AUTHORED + REFERENCED}
    checks["copy_matches_candidate"] = copy_hashes == {n: sha256_file(REPO / n) for n in AUTHORED + REFERENCED}
    focused = run.test_child("isolation-copy", "unittest", modules_args([TEST_MODULE, *COMPATIBILITY]),
                             source=copy_root)
    sentinel = run.test_child("isolation-sentinels", "sentinels", source=copy_root)
    loaded = focused.get("loaded_modules", {})
    checks["focused_suite_passes_from_copy"] = focused["pass"] is True
    checks["modules_loaded_from_copy"] = bool(loaded) and all(
        Path(p).resolve().is_relative_to(copy_root.resolve()) for p in loaded.values())
    checks["sentinels_refuse_network_spawn_and_outside_writes"] = sentinel["pass"] is True
    return {"refusals": refusals, "banned_literals": [b.decode() for b in banned], "copy_root": str(copy_root),
            "copied_files": count, "copy_sha256": copy_hashes, "focused": focused, "sentinels": sentinel,
            "checks": checks, "pass": all(checks.values())}


def case_compatibility(run):
    record = run.test_child("compatibility", "unittest", modules_args(COMPATIBILITY))
    record["pass"] = record["pass"] and set(record.get("modules", [])) == set(COMPATIBILITY)
    return record


def case_full_ci(run):
    results = {}
    for label, args in FULL_CI:
        entry, out, err = run.child(label, [sys.executable, *args])
        counts = unittest_counts(err + out)
        results[label] = dict(counts, exit=entry["exit"],
                              pass_=entry["exit"] == 0 and counts["ok_line"] and (counts["tests_run"] or 0) > 0)
    return {"populations": results, "unexpected_skips_disposition": "skips are recorded per population; any "
            "nonzero skip count fails this case until its owner qualifies it",
            "pass": all(r["pass_"] and r["skipped"] == 0 for r in results.values())}


def case_credentials(run):
    scanner = Path(sys.executable).parent / "detect-secrets"
    if not scanner.is_file():
        return {"pass": False, "error": "missing_dependency_detect_secrets", "scanner": str(scanner)}
    scan, raw, _ = run.child("detect-secrets", [scanner, "scan", "--all-files", "--exclude-files", "^.git/",
                                                "--no-verify"], stdout_name="secret-scan.json")
    report_path = run.dir / "secret-scan.json"
    try:
        report = json.loads(raw)
        report_ok = isinstance(report["results"], dict)
        hits = {k: len(v) for k, v in report["results"].items()} if report_ok else None
    except (ValueError, KeyError, TypeError):
        report_ok, hits = False, None
    check, out, _ = run.child("check-secret-report", [sys.executable, "-B", REPO / "tools" / "check_secret_report.py",
                                                       report_path])
    summary = [line for line in out.decode(errors="replace").splitlines() if "potential secrets:" in line]
    return {"pass": scan["exit"] == 0 and report_ok and check["exit"] == 0 and len(summary) == 1,
            "scanner": str(scanner), "report": str(report_path), "raw_report_hits_by_file": hits,
            "checker_summary": summary}


def parent_main(args):
    started = datetime.now(timezone.utc).isoformat()
    try:
        parent = checked_output_parent(args.output_parent)
    except UnsafeRoot as error:
        print(json.dumps({"case": args.case, "pass": False, "exit": 2, "error": str(error)}))
        return 2
    run = Run(parent, args.case)
    before = identity()
    handler = {"behavior": case_behavior, "compatibility": case_compatibility,
               "negative-control": case_negative_control, "isolation": case_isolation,
               "full-ci": case_full_ci, "credentials": case_credentials}[args.case]
    verdict = handler(run)
    after = identity()
    stable = before == after
    passed = (verdict.get("pass") is True and stable and bool(run.children)
              and all(c["exit"] is not None for c in run.children))
    record = {"case": args.case, "argv": [sys.executable, *sys.argv], "cwd": os.getcwd(), "repo": str(REPO),
              "run_dir": str(run.dir), "started_utc": started, "ended_utc": datetime.now(timezone.utc).isoformat(),
              "identity_before": before, "identity_after": after, "identity_stable": stable,
              "children": run.children, "verdict": verdict, "pass": passed, "exit": 0 if passed else 1,
              "review_required": True,
              "scope": "FA47 acceptance evidence; machine-valid is REVIEW REQUIRED, not semantic acceptance"}
    (run.dir / "result.json").write_text(json.dumps(record, indent=2, sort_keys=True, default=str) + "\n")
    print(json.dumps({"case": args.case, "pass": passed, "exit": record["exit"], "run_dir": str(run.dir),
                      "head": after["head"], "clean_tree": after["clean_tree"],
                      "tests_run": verdict.get("tests_run"), "skipped": verdict.get("skipped")}, sort_keys=True))
    return record["exit"]


# ---------------------------------------------------------------- child side


def install_sentinels(run_dir):
    """Regression controls (not a hostile-code sandbox): no network, no spawn other
    than the inventoried worker, no writes or database opens outside run_dir."""
    root = Path(run_dir).resolve()
    state = {"violations": [], "spawns": [], "expected": None}
    write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC

    def resolve(path, dir_fd=None):
        try:
            path = os.fsdecode(os.fspath(path))
        except TypeError:
            return None
        if path.startswith("file:"):
            path = path[5:].split("?", 1)[0]
        if not os.path.isabs(path) and isinstance(dir_fd, int) and not isinstance(dir_fd, bool):
            try:
                path = os.path.join(os.readlink("/proc/self/fd/%d" % dir_fd), path)
            except OSError:
                return None
        return Path(os.path.abspath(path))

    def inside(path, dir_fd=None):
        if isinstance(path, int):
            return True
        if isinstance(path, (str, bytes)) and os.fsdecode(path) in ("", ":memory:", os.devnull):
            return True
        resolved = resolve(path, dir_fd)
        return resolved is not None and resolved.resolve().is_relative_to(root)

    def refuse(event, detail):
        entry = {"event": event, "detail": repr(detail)[:200]}
        if state["expected"] is not None:
            state["expected"].append(entry)
        else:
            state["violations"].append(entry)
        raise PermissionError("fa47 sentinel refused " + event)

    mutations = {"os.mkdir": ((0, 2),), "os.remove": ((0, 1),), "os.rmdir": ((0, 1),), "os.chmod": ((0, 2),),
                 "os.chown": ((0, 3),), "os.utime": ((0, 3),), "os.truncate": ((0, None),),
                 "shutil.rmtree": ((0, 1),), "os.rename": ((0, 2), (1, 3)), "os.replace": ((0, 2), (1, 3)),
                 "os.link": ((0, 2), (1, 3)), "os.symlink": ((1, 2),), "shutil.copyfile": ((1, None),),
                 "shutil.move": ((0, None), (1, None))}

    def hook(event, args):
        if event.startswith("socket.") and event != "socket.__new__" or event in (
                "urllib.Request", "http.client.connect", "webbrowser.open", "ftplib.connect", "smtplib.connect"):
            refuse(event, args)
        elif event == "subprocess.Popen":
            argv = [os.fsdecode(a) if isinstance(a, (bytes, str)) else repr(a) for a in (args[1] or [])]
            if argv[:1] == [sys.executable] and tuple(argv[1:5]) == WORKER_PREFIX and all(
                    inside(a) for a in argv[5:] if os.path.isabs(a)):
                state["spawns"].append(argv[1:5])
            else:
                refuse(event, argv)
        elif event in ("os.system", "os.posix_spawn", "os.spawn", "os.exec", "os.fork", "os.forkpty",
                       "pty.spawn"):
            refuse(event, args)
        elif event == "open":
            path, mode, flags = args
            writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                isinstance(flags, int) and flags & write_flags)
            if state.get("checked") is not None and state["checked"] == path:
                state["checked"] = None  # already resolved against its dir_fd by guarded_open
            elif writing and not inside(path):
                refuse(event, path)
        elif event == "sqlite3.connect":
            if not inside(args[0]):
                refuse(event, args[0])
        elif event in mutations:
            for index, fd_index in mutations[event]:
                if index < len(args):
                    dir_fd = args[fd_index] if fd_index is not None and fd_index < len(args) else None
                    target = resolve(args[index], dir_fd) if event == "os.mkdir" else None
                    if target is not None and target.is_dir():
                        continue  # an existing directory: mkdir can only fail with EEXIST
                    if not inside(args[index], dir_fd):
                        refuse(event, args[index])

    sys.addaudithook(hook)
    real_open = os.open

    def guarded_open(path, flags, mode=0o777, *, dir_fd=None):
        # The audit "open" event omits dir_fd, so relative descriptor-based opens
        # are resolved here, against their directory descriptor, first.
        if flags & write_flags and not inside(path, dir_fd):
            refuse("open", path)
        state["checked"] = path
        try:
            return real_open(path, flags, mode, dir_fd=dir_fd)
        finally:
            state["checked"] = None
    os.open = guarded_open
    return state


def repository_modules(source_root):
    loaded = {}
    for name, module in list(sys.modules.items()):
        path = getattr(module, "__file__", None)
        if path and (name.startswith("campaign_tool") or name.startswith("tests")):
            loaded[name] = os.path.abspath(path)
    return loaded


def run_suite(suite):
    import io
    import unittest
    ids = []

    def walk(item):
        for test in item:
            if isinstance(test, unittest.TestSuite):
                walk(test)
            else:
                ids.append(test.id())
    walk(suite)  # before running: a finished suite drops its tests
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    sys.stderr.write(stream.getvalue())
    return result, ids


def test_id(test):
    """Owning test id; subtest failures report their parent test."""
    return getattr(test, "test_case", test).id()


def data_constants_outside(module, root):
    """Absolute filesystem constants or bound defaults that escape the run directory."""
    found = []

    def check(label, value):
        if isinstance(value, (str, Path)) and os.path.isabs(str(value)) and "\n" not in str(value):
            if not Path(str(value)).resolve().is_relative_to(root):
                found.append(label)
    for name, value in vars(module).items():
        if name.startswith("__"):
            continue  # interpreter metadata (__file__, __cached__), not runtime data
        check(name, value)
        defaults = getattr(getattr(value, "__func__", value), "__defaults__", None) or ()
        kwdefaults = getattr(getattr(value, "__func__", value), "__kwdefaults__", None) or {}
        for index, default in enumerate((*defaults, *kwdefaults.values())):
            check("%s.default[%d]" % (name, index), default)
    return found


def child_main(args):
    run_dir = Path(args.run_dir).resolve()
    source_root = Path(__file__).resolve().parents[1]
    tmp = run_dir / "tmp"
    tmp.mkdir(exist_ok=True)
    os.environ["TMPDIR"] = str(tmp)
    import tempfile
    tempfile.tempdir = str(tmp)
    state = install_sentinels(run_dir)
    sys.path.insert(0, str(source_root))
    import unittest
    record = {"mode": args.child, "tempdir": tempfile.gettempdir(), "source_root": str(source_root),
              "sys_path0": sys.path[0]}
    if args.child == "unittest":
        suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(n) for n in args.module)
        result, ids = run_suite(suite)
        from campaign_tool.records import extraction_ledger
        record.update(modules=args.module, test_ids=ids, tests_run=result.testsRun,
                      failures=len(result.failures), errors=len(result.errors), skipped=len(result.skipped),
                      skip_reasons=[r for _, r in result.skipped], expected_failures=len(result.expectedFailures),
                      unexpected_successes=len(result.unexpectedSuccesses),
                      failure_ids=sorted({test_id(t) for t, _ in result.failures}),
                      error_ids=sorted({test_id(t) for t, _ in result.errors}),
                      data_constants_outside_run=data_constants_outside(extraction_ledger, run_dir))
        record["pass"] = (result.wasSuccessful() and result.testsRun > 0 and not result.skipped
                          and not result.expectedFailures and not record["data_constants_outside_run"])
    elif args.child == "control":
        record.update(marker_only_control(Path(args.baseline)))
    else:
        record.update(sentinel_probe(state, run_dir))
    record["loaded_modules"] = repository_modules(source_root)
    record["inventoried_spawns"] = state["spawns"]
    record["guard_violations"] = state["violations"]
    record["pass"] = record.get("pass") is True and not state["violations"]
    Path(args.result_json).write_text(json.dumps(record, indent=2, sort_keys=True, default=str) + "\n")
    return 0 if record["pass"] else 1


def marker_only_control(baseline_path):
    """A4: substitute only ensure_schema with the marker-only baseline, in memory."""
    import importlib.util
    import unittest
    from unittest import mock
    from campaign_tool.records import extraction_ledger as candidate
    name = "campaign_tool.records.fa47_marker_only_baseline"
    spec = importlib.util.spec_from_file_location(name, baseline_path)
    baseline = importlib.util.module_from_spec(spec)
    sys.modules[name] = baseline
    spec.loader.exec_module(baseline)

    def marker_only(con):
        try:
            return baseline.ensure_schema(con)
        except baseline.ExtractionBindingError as error:
            raise candidate.ExtractionBindingError(str(error)) from None
    suite = unittest.defaultTestLoader.loadTestsFromName(TEST_MODULE)
    with mock.patch.object(candidate, "ensure_schema", marker_only):
        result, ids = run_suite(suite)
    return {"baseline_path": str(baseline_path), "schema_identical": baseline.SCHEMA == candidate.SCHEMA,
            "tests_run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
            "skipped": len(result.skipped), "failure_ids": sorted({test_id(t) for t, _ in result.failures}),
            "error_ids": sorted({test_id(t) for t, _ in result.errors}), "test_ids": ids,
            "pass": baseline.SCHEMA == candidate.SCHEMA and result.testsRun > 0 and not result.skipped}


def sentinel_probe(state, run_dir):
    """Each sentinel must refuse its probe; refusals here are expected and recorded."""
    import socket
    state["expected"] = []
    probes = {}
    outside = Path(run_dir).parent / ("fa47-sentinel-%s" % secrets.token_hex(4))
    for label, action in (
            ("network", lambda: socket.create_connection(("192.0.2.1", 9), timeout=0.1)),
            ("spawn", lambda: subprocess.run(["true"], check=False)),
            ("outside_write", lambda: open(outside, "w").close()),
            ("outside_database", lambda: __import__("sqlite3").connect(str(outside)).close())):
        try:
            action()
            probes[label] = "not_refused"
        except PermissionError:
            probes[label] = "refused"
    expected, state["expected"] = state["expected"], None
    return {"probes": probes, "expected_refusals": expected, "outside_created": outside.exists(),
            "pass": all(v == "refused" for v in probes.values()) and not outside.exists()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--case")
    parser.add_argument("--output-parent")
    parser.add_argument("--child", choices=("unittest", "control", "sentinels"))
    parser.add_argument("--run-dir")
    parser.add_argument("--result-json")
    parser.add_argument("--module", action="append", default=[])
    parser.add_argument("--baseline")
    args = parser.parse_args(argv)
    if args.child:
        return child_main(args)
    if args.case not in CASES or not args.output_parent:
        print(json.dumps({"case": args.case, "pass": False, "exit": 2, "error": "unknown_case_or_missing_parent"}))
        return 2
    return parent_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
