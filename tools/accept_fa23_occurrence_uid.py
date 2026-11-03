"""Scoped FA23 acceptance runner for the captured mail UID/UIDVALIDITY domain.

Each invocation writes one fresh run directory below --output-parent and exits
0 only when its case passed. A pass is REVIEW REQUIRED scoped evidence; it is
not full-repository CI, semantic acceptance, a stage promotion or permission.

Test cases run in a file-launched child interpreter that sets its own import
root, temporary root and offline/write guards before importing any repository
module. The parent never imports repository code.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys

sys.dont_write_bytecode = True

REPO = Path(__file__).resolve().parents[1]
PRODUCT = "campaign_tool/records/ledger/occurrence_projection.py"
AUTHORED = (PRODUCT, "tests/test_fa23_occurrence_uid.py", "tools/accept_fa23_occurrence_uid.py")
COMPATIBILITY = ("tests.records.test_ledger_occurrences", "tests.records.test_ledger_occurrence_replay")
REFERENCED = ("tests/records/test_ledger_occurrences.py", "tests/records/test_ledger_occurrence_replay.py",
              "tests/records/test_ledger.py", "campaign_tool/records/ledger/store.py",
              "tools/check_secret_report.py", "tools/check_public_tree.py")
CASES = ("behavior", "compatibility", "negative-control", "credentials")
SCRUBBED_ENV = ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONINSPECT", "PYTHONUSERBASE")


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    return sha256_bytes(Path(path).read_bytes())


def git(*args):
    done = subprocess.run(["git", *args], cwd=REPO, capture_output=True, check=False)
    return done.returncode, done.stdout


def identity():
    _, head = git("rev-parse", "HEAD")
    _, status = git("status", "--porcelain=v1", "--untracked-files=all")
    return {"head": head.decode().strip(), "status_porcelain": status.decode(),
            "clean_tree": status == b"",
            "file_sha256": {name: sha256_file(REPO / name) for name in AUTHORED + REFERENCED}}


class Run:
    def __init__(self, parent, case):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        parent = Path(parent).resolve()
        parent.mkdir(parents=True, exist_ok=True)
        self.dir = parent / ("%s-%s-%d-%s" % (case, stamp, os.getpid(), secrets.token_hex(4)))
        self.dir.mkdir(mode=0o700)
        self.children = []

    def child(self, label, argv, stdout_name=None):
        env = {k: v for k, v in os.environ.items() if k not in SCRUBBED_ENV}
        env["TMPDIR"] = str(self.dir / "tmp")
        (self.dir / "tmp").mkdir(exist_ok=True)
        done = subprocess.run([str(a) for a in argv], cwd=REPO, env=env, capture_output=True, check=False)
        out_path = self.dir / (stdout_name or label + ".stdout")
        err_path = self.dir / (label + ".stderr")
        out_path.write_bytes(done.stdout)
        err_path.write_bytes(done.stderr)
        entry = {"label": label, "argv": [str(a) for a in argv], "cwd": str(REPO), "exit": done.returncode,
                 "stdout": str(out_path), "stdout_sha256": sha256_bytes(done.stdout),
                 "stderr": str(err_path), "stderr_sha256": sha256_bytes(done.stderr)}
        self.children.append(entry)
        return entry, done.stdout

    def test_child(self, label, mode, extra=()):
        result_path = self.dir / (label + "-result.json")
        entry, _ = self.child(label, [sys.executable, Path(__file__).resolve(), "--child", mode,
                                      "--run-dir", self.dir, "--result-json", result_path, *extra])
        try:
            record = json.loads(result_path.read_text())
        except (OSError, ValueError):
            record = {"pass": False, "error": "child_result_missing_or_invalid"}
        record["child_exit"] = entry["exit"]
        record["pass"] = record.get("pass") is True and entry["exit"] == 0
        return record


def frozen_baseline(run):
    """Most recent committed product revision whose bytes differ from the candidate file."""
    candidate = (REPO / PRODUCT).read_bytes()
    code, revs = git("rev-list", "HEAD", "--", PRODUCT)
    if code != 0:
        return None
    for rev in revs.decode().split():
        code, blob = git("show", rev + ":" + PRODUCT)
        if code == 0 and blob != candidate:
            target = run.dir / "baseline" / "occurrence_projection_frozen_baseline.py"
            target.parent.mkdir()
            target.write_bytes(blob)
            return {"revision": rev, "path": str(target), "sha256": sha256_bytes(blob)}
    return None


def check_credentials(run):
    scanner = shutil.which("detect-secrets") or str(Path(sys.executable).parent / "detect-secrets")
    if not Path(scanner).is_file():
        return {"pass": False, "error": "missing_dependency_detect_secrets", "scanner": scanner}
    scan, raw = run.child("detect-secrets", [scanner, "scan", "--all-files", "--exclude-files", "^.git/",
                                             "--no-verify"], stdout_name="secret-scan.json")
    report_path = run.dir / "secret-scan.json"
    try:
        report = json.loads(raw)
        report_ok = isinstance(report["results"], dict)
        hits = sum(len(v) for v in report["results"].values()) if report_ok else None
    except (ValueError, KeyError, TypeError):
        report_ok, hits = False, None
    check, out = run.child("check-secret-report",
                           [sys.executable, REPO / "tools" / "check_secret_report.py", report_path])
    summary = [line for line in out.decode(errors="replace").splitlines() if "potential secrets:" in line]
    return {"pass": scan["exit"] == 0 and report_ok and check["exit"] == 0,
            "scanner": scanner, "report": str(report_path), "raw_report_hits": hits,
            "checker_summary": summary}


def parent_main(args):
    started = datetime.now(timezone.utc).isoformat()
    run = Run(args.output_parent, args.case)
    before = identity()
    if args.case == "behavior":
        verdict = run.test_child("behavior", "unittest", ("--module", "tests.test_fa23_occurrence_uid"))
    elif args.case == "compatibility":
        extra = []
        for name in COMPATIBILITY:
            extra += ["--module", name]
        verdict = run.test_child("compatibility", "unittest", extra)
    elif args.case == "negative-control":
        baseline = frozen_baseline(run)
        if baseline is None:
            verdict = {"pass": False, "error": "frozen_baseline_unavailable"}
        else:
            verdict = run.test_child("negative-control", "negative-control", ("--baseline", baseline["path"]))
            verdict["baseline"] = baseline
    else:
        verdict = check_credentials(run)
    after = identity()
    stable = before == after
    passed = verdict.get("pass") is True and stable and bool(run.children) and all(
        c["exit"] is not None for c in run.children)
    record = {"case": args.case, "argv": [sys.executable, *sys.argv], "cwd": os.getcwd(), "repo": str(REPO),
              "run_dir": str(run.dir), "started_utc": started, "ended_utc": datetime.now(timezone.utc).isoformat(),
              "identity_before": before, "identity_after": after, "identity_stable": stable,
              "children": run.children, "verdict": verdict, "pass": passed, "exit": 0 if passed else 1,
              "review_required": True,
              "scope": "scoped FA23 acceptance; not full-repository CI or semantic acceptance; "
                       "full discovery = operator-run"}
    (run.dir / "result.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"case": args.case, "pass": passed, "exit": record["exit"], "run_dir": str(run.dir),
                      "head": after["head"], "tests_run": verdict.get("tests_run"),
                      "skipped": verdict.get("skipped")}, sort_keys=True))
    return record["exit"]


# ---------------------------------------------------------------- child side


def install_guards(run_dir):
    """Refuse network, process launch and writes outside this run directory."""
    root = Path(run_dir).resolve()
    violations = []
    write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC

    def inside(path, dir_fd=None):
        if isinstance(path, int):
            return True
        if isinstance(path, bytes):
            path = os.fsdecode(path)
        try:
            path = os.fspath(path)
        except TypeError:
            return False
        if path == ":memory:" or path == "":
            return True
        if path.startswith("file:"):
            path = path[5:].split("?", 1)[0]
        if not os.path.isabs(path) and isinstance(dir_fd, int) and not isinstance(dir_fd, bool):
            try:
                path = os.path.join(os.readlink("/proc/self/fd/%d" % dir_fd), path)
            except OSError:
                return False
        return Path(os.path.abspath(path)).resolve().is_relative_to(root)

    def refuse(event, detail):
        violations.append({"event": event, "detail": repr(detail)[:200]})
        raise PermissionError("fa23 acceptance guard refused " + event)

    # event -> ((path index, dir_fd index or None), ...) for mutating filesystem events.
    mutations = {"os.mkdir": ((0, 2),), "os.remove": ((0, 1),), "os.rmdir": ((0, 1),),
                 "os.chmod": ((0, 2),), "os.chown": ((0, 3),), "os.utime": ((0, 3),),
                 "os.truncate": ((0, None),), "shutil.rmtree": ((0, 1),),
                 "os.rename": ((0, 2), (1, 3)), "os.replace": ((0, 2), (1, 3)), "os.link": ((0, 2), (1, 3)),
                 "os.symlink": ((1, 2),), "shutil.copyfile": ((1, None),), "shutil.move": ((0, None), (1, None))}

    def hook(event, args):
        if event.startswith("socket.") and event != "socket.__new__":
            refuse(event, args)
        elif event in ("subprocess.Popen", "os.system", "os.posix_spawn", "os.spawn", "os.exec", "os.fork",
                       "os.forkpty", "pty.spawn", "ctypes.dlopen", "urllib.Request", "webbrowser.open"):
            refuse(event, args)
        elif event == "open":
            path, mode, flags = args
            writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                isinstance(flags, int) and flags & write_flags)
            if writing and not inside(path):
                refuse(event, path)
        elif event == "sqlite3.connect":
            if not inside(args[0]):
                refuse(event, args[0])
        elif event in mutations:
            for index, fd_index in mutations[event]:
                if index >= len(args):
                    continue
                dir_fd = args[fd_index] if fd_index is not None and fd_index < len(args) else None
                if not inside(args[index], dir_fd):
                    refuse(event, args[index])

    sys.addaudithook(hook)
    return violations


def child_main(args):
    run_dir = Path(args.run_dir).resolve()
    tmp = run_dir / "tmp"
    tmp.mkdir(exist_ok=True)
    import tempfile
    tempfile.tempdir = str(tmp)
    violations = install_guards(run_dir)
    sys.path.insert(0, str(REPO))
    import unittest
    record = {"mode": args.child, "tempdir": str(tmp)}
    if args.child == "unittest":
        suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(name) for name in args.module)
        result = unittest.TextTestRunner(stream=sys.stderr, verbosity=2).run(suite)
        record.update({"modules": args.module, "tests_run": result.testsRun, "failures": len(result.failures),
                       "errors": len(result.errors), "skipped": len(result.skipped),
                       "expected_failures": len(result.expectedFailures),
                       "unexpected_successes": len(result.unexpectedSuccesses)})
        record["pass"] = (result.wasSuccessful() and result.testsRun > 0 and not result.skipped
                          and not result.expectedFailures)
    else:
        record.update(negative_control(Path(args.baseline)))
    record["guard_violations"] = violations
    record["pass"] = record["pass"] is True and not violations
    Path(args.result_json).write_text(json.dumps(record, indent=2, sort_keys=True, default=str) + "\n")
    return 0 if record["pass"] else 1


def negative_control(baseline_path):
    """A4: the frozen baseline must break exactly the oversized-number expectation."""
    import importlib.util
    from campaign_tool.records.ledger import occurrence_projection as candidate
    from tests import test_fa23_occurrence_uid as fa23
    from tests.records import test_ledger_occurrences as fixtures
    name = "campaign_tool.records.ledger.fa23_frozen_baseline"
    spec = importlib.util.spec_from_file_location(name, baseline_path)
    baseline = importlib.util.module_from_spec(spec)
    sys.modules[name] = baseline
    spec.loader.exec_module(baseline)

    def project_high_uid(module):
        helper = fixtures.OccurrenceProjectionTests()
        helper.setUp()
        try:
            sha = helper.add_doc("synthetic fa23 control message", "eml")
            helper.add_occurrence("fa23-control", sha, receipt={
                "mail_identity": fa23.mail_identity(fa23.UID_MAX + 1, 1), "eml_sha256": sha})
            result = module.project_occurrences(helper.database, helper.capture())
            return {"canonical_kinds": result["canonical_kinds"], "unresolved_reasons": result["unresolved_reasons"]}
        finally:
            helper.doCleanups()

    expected = fa23.oversized_labels()
    baseline_failures = fa23.domain_expectation_failures(baseline._base_plan)
    candidate_failures = fa23.domain_expectation_failures(candidate._base_plan)
    baseline_ids = fa23.endpoint_candidate_ids(baseline._base_plan)
    candidate_ids = fa23.endpoint_candidate_ids(candidate._base_plan)
    baseline_projection = project_high_uid(baseline)
    candidate_projection = project_high_uid(candidate)
    checks = {
        "baseline_breaks_exactly_oversized_cases": sorted(baseline_failures) == expected and len(expected) > 0,
        "candidate_has_no_failures": candidate_failures == [],
        "endpoint_ids_identical": baseline_ids == candidate_ids and all(
            v[0] == fa23.EXPLICIT and v[1] for v in candidate_ids.values()) and len(candidate_ids) == 4,
        "baseline_projection_types_high_uid": baseline_projection["canonical_kinds"].get("mail") == 1,
        "candidate_projection_blocks_high_uid": "mail" not in candidate_projection["canonical_kinds"]
        and candidate_projection["unresolved_reasons"].get(fa23.BLOCKED) == 1,
        "baseline_is_not_candidate": Path(baseline.__file__).read_bytes() != Path(candidate.__file__).read_bytes(),
    }
    return {"baseline_failures": sorted(baseline_failures), "expected_oversized": expected,
            "candidate_failures": candidate_failures,
            "endpoint_ids": {"%d,%d" % k: v[1] for k, v in candidate_ids.items()},
            "baseline_projection": baseline_projection, "candidate_projection": candidate_projection,
            "checks": checks, "control_cases_per_plan": len(fa23.domain_cases()), "skipped": 0,
            "pass": all(checks.values())}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--case", choices=CASES)
    parser.add_argument("--output-parent")
    parser.add_argument("--child", choices=("unittest", "negative-control"))
    parser.add_argument("--run-dir")
    parser.add_argument("--result-json")
    parser.add_argument("--module", action="append", default=[])
    parser.add_argument("--baseline")
    args = parser.parse_args(argv)
    if args.child:
        return child_main(args)
    if not args.case or not args.output_parent:
        parser.error("--case and --output-parent are required")
    return parent_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
