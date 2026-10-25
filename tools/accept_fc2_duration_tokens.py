"""Scoped offline acceptance runner for FC2-DURATION-TOKENS.

Exactly one --check per invocation; evidence goes to a fresh, uniquely named
directory below --output-root, which must lie outside this repository:

  focused        tests.runner.test_fc2_duration_tokens (pinned test count)
  compatibility  unchanged existing selectors (no skips admitted)
  negative       in-memory mutant: the original fragment-matching DURATION
                 pattern must make the decimal, grouped and signed regressions
                 fail on their own FRAGMENT_HIT gate while supported positives
                 pass; restored candidate then passes the full focused module
  portability    focused tests in-process here AND in a child run from a second
                 copy of the candidate source at a different absolute prefix;
                 loaded module paths recorded for both
  credentials    installed detect-secrets (adjacent to this interpreter) scan
                 of the candidate repo + unchanged tools/check_secret_report.py
  public-tree    unchanged tools/check_public_tree.py from the candidate repo

Before any application import the runner redirects tempfile to the per-run
directory, disables bytecode writes, installs socket sentinels, refuses
Python-level file writes outside the per-run directory and refuses any
subprocess except the listed local children (git, this interpreter, the
adjacent detect-secrets). These are isolation checks, not a sandbox for
hostile code. The repository tree is content-hashed before and after.
A pass is REVIEW REQUIRED, never semantic/legal acceptance or publication.
"""
import argparse
import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile

REPO = Path(__file__).resolve().parents[1]
CHECKS = ("focused", "compatibility", "negative", "portability", "credentials", "public-tree")
BRIEF = {"card": "FC2-DURATION-TOKENS", "task": "t_150541a0",
         # short prefix only (no high-entropy literal); the full commit is resolved via git at run time
         "base_pin_prefix": "9c398829",
         "acceptance": "A1-A6 as frozen in the t_150541a0 card body"}
FOCUSED = "tests.runner.test_fc2_duration_tokens"
FOCUSED_TEST_COUNT = 21
COMPATIBILITY = ("tests.runner.test_digest.DetectorTests",
                 "tests.runner.test_digest.BuildDigestTests.test_no_model_is_detector_only_and_needs_attorney_review",
                 "tests.test_model_input", "tests.test_secret_report", "tests.test_public_tree")
HASHED = ("campaign_tool/digest/detectors.py", "tests/runner/test_fc2_duration_tokens.py",
          "tools/accept_fc2_duration_tokens.py", "tests/runner/test_digest.py", "tests/test_model_input.py",
          "tests/test_secret_report.py", "tests/test_public_tree.py", "tools/check_secret_report.py",
          "tools/check_public_tree.py")
# Exact DURATION source at the base pin; the negative control restores it in memory only.
ORIGINAL_DURATION = r"(?i)\b(\d{1,4})\s*[- ]?\s*(day|days|month|months|year|years|week|weeks|hour|hours)\b"
FRAGMENT = "FRAGMENT_HIT"
NEG_FAIL = (f"{FOCUSED}.MeasuredBaselineFailures.test_decimal_1_5_years_is_uninterpreted",
            f"{FOCUSED}.MeasuredBaselineFailures.test_grouped_1000_days_is_uninterpreted",
            f"{FOCUSED}.MeasuredBaselineFailures.test_signed_minus_30_days_is_uninterpreted",
            f"{FOCUSED}.GroupingVariants.test_grouping_variants",
            f"{FOCUSED}.SignedVariants.test_signed_variants")
NEG_PASS = (f"{FOCUSED}.SupportedDurations.test_days_months_years_weeks_hours",
            f"{FOCUSED}.SupportedDurations.test_hyphenated_30_day_form",
            f"{FOCUSED}.SupportedDurations.test_explicit_zero")
# Files a second-prefix copy needs: the application package plus the focused closure.
COPY_TREES = ("campaign_tool",)
COPY_FILES = ("tests/__init__.py", "tests/runner/__init__.py", "tests/runner/test_fc2_duration_tokens.py",
              "tools/accept_fc2_duration_tokens.py")

GUARD_HITS = []
WRITE_HITS = []
PROC_HITS = []
PROCS = []
ALLOWED_EXE = set()


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def git(*args):
    done = subprocess.run(["git", "--no-optional-locks", "-C", str(REPO), *args],
                          capture_output=True, text=True, check=False)
    return done.stdout.strip() if done.returncode == 0 else None


def tree_digest():
    digest = hashlib.sha256()
    for directory, dirs, files in os.walk(REPO):
        dirs[:] = sorted(d for d in dirs if d not in (".git", "__pycache__"))
        for name in sorted(files):
            path = Path(directory) / name
            digest.update(str(path.relative_to(REPO)).encode() + b"\0" + hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def identity():
    hashes = {}
    for relative in HASHED:
        path = REPO / relative
        hashes[relative] = sha256_bytes(path.read_bytes()) if path.is_file() else None
    top = git("rev-parse", "--show-toplevel")
    checkout = top is not None and Path(top).resolve() == REPO
    return {"repo": str(REPO), "git_checkout": checkout,
            "head": git("rev-parse", "HEAD") if checkout else "not_a_git_checkout",
            "status_porcelain": git("status", "--porcelain") if checkout else "not_a_git_checkout",
            "source_sha256": hashes, "tree_sha256": tree_digest()}


def fresh_run_dir(root, check):
    root = Path(root)
    if not root.is_absolute():
        raise SystemExit("output_root_must_be_absolute")
    resolved = root.resolve()
    if resolved == REPO or REPO in resolved.parents:
        raise SystemExit("output_root_inside_repository")
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run = resolved / f"{check}-{stamp}-{os.getpid()}-{secrets.token_hex(4)}"
    run.mkdir(parents=True, exist_ok=False)
    return run


def install_offline_guards():
    def refuse(name):
        def guard(*args, **kwargs):
            GUARD_HITS.append(name)
            raise OSError(f"offline acceptance guard refused {name}")
        return guard
    socket.socket.connect = refuse("socket.connect")
    socket.socket.connect_ex = refuse("socket.connect_ex")
    socket.socket.bind = refuse("socket.bind")
    socket.create_connection = refuse("socket.create_connection")
    socket.getaddrinfo = refuse("socket.getaddrinfo")


WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
# audit event -> ((path arg index, dir_fd arg index or None), ...)
WRITE_EVENTS = {"os.mkdir": ((0, 2),), "os.rename": ((0, 2), (1, 3)), "os.replace": ((0, 2), (1, 3)),
                "os.remove": ((0, 1),), "os.rmdir": ((0, 1),), "os.truncate": ((0, None),),
                "os.symlink": ((1, 2),), "os.link": ((1, 3),), "shutil.rmtree": ((0, 1),),
                "sqlite3.connect": ((0, None),)}
REFUSED_PROC_EVENTS = ("os.system", "os.exec", "os.spawn", "os.fork", "os.forkpty", "pty.spawn", "os.startfile")


def install_hooks(run):
    """Refuse Python-level writes outside the run root and unlisted child processes."""
    root = os.path.realpath(run)

    def inside(target, dir_fd=None):
        if isinstance(target, int):
            return True
        text = os.fsdecode(target)
        if text in ("", ":memory:", os.devnull):
            return True
        if text.startswith("file:"):
            text = text[5:].split("?", 1)[0]
        if isinstance(dir_fd, int) and dir_fd >= 0 and not os.path.isabs(text):
            try:  # dir_fd-relative call (e.g. TemporaryDirectory cleanup): resolve via its directory
                text = os.path.join(os.readlink(f"/proc/self/fd/{dir_fd}"), text)
            except OSError:
                return False  # unresolvable descriptor: fail closed
        resolved = os.path.realpath(text)
        return resolved == root or resolved.startswith(root + os.sep)

    def allowed_proc(argv0):
        name = os.fsdecode(argv0) if argv0 is not None else ""
        return name in ALLOWED_EXE

    def hook(event, args):
        if event == "open":
            path, mode, flags = (tuple(args) + (None, None, None))[:3]
            writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                isinstance(flags, int) and bool(flags & WRITE_FLAGS))
            if writing and path is not None and not inside(path):
                WRITE_HITS.append(f"open:{os.fsdecode(path)}")
                raise PermissionError("acceptance write guard refused open outside per-run root")
        elif event in WRITE_EVENTS:
            for p, d in WRITE_EVENTS[event]:
                target = args[p] if p < len(args) else None
                dir_fd = args[d] if d is not None and d < len(args) else None
                if target is not None and not inside(target, dir_fd):
                    WRITE_HITS.append(f"{event}:{os.fsdecode(target) if not isinstance(target, int) else target}")
                    raise PermissionError(f"acceptance write guard refused {event} outside per-run root")
        elif event == "subprocess.Popen":
            argv = list(args[1]) if isinstance(args[1], (list, tuple)) else [args[1]]
            exe = args[0] if args[0] is not None else (argv[0] if argv else None)
            if not allowed_proc(exe):
                PROC_HITS.append(f"{event}:{os.fsdecode(exe) if exe is not None else None}")
                raise PermissionError("acceptance process guard refused an unlisted child")
            PROCS.append([os.fsdecode(a) for a in argv])
        elif event == "os.posix_spawn":
            if not allowed_proc(args[0]):
                PROC_HITS.append(f"{event}:{os.fsdecode(args[0])}")
                raise PermissionError("acceptance process guard refused posix_spawn")
        elif event.startswith(REFUSED_PROC_EVENTS):
            PROC_HITS.append(event)
            raise PermissionError(f"acceptance process guard refused {event}")
    sys.addaudithook(hook)
    # Self-checks: an out-of-root write under a nonexistent top-level directory
    # (inert even if unguarded) and an unlisted child must be refused.
    escape = os.path.join(os.sep, f"fc2-escape-{secrets.token_hex(6)}", "probe")
    try:
        open(escape, "w").close()
        write_refused = False
    except PermissionError:
        write_refused = True
    except OSError:
        write_refused = False
    probe_write = [h for h in WRITE_HITS if escape in h]
    WRITE_HITS[:] = [h for h in WRITE_HITS if escape not in h]
    try:
        subprocess.run(["fc2-unlisted-probe"], check=False)
        proc_refused = False
    except PermissionError:
        proc_refused = True
    except OSError:
        proc_refused = False
    probe_proc = [h for h in PROC_HITS if "fc2-unlisted-probe" in h]
    PROC_HITS[:] = [h for h in PROC_HITS if "fc2-unlisted-probe" not in h]
    positive = run / "guard-positive.txt"
    positive.write_text("in-root write allowed\n")
    return {"root": root, "allowed_children": sorted(ALLOWED_EXE),
            "escape_write_refused": write_refused and len(probe_write) == 1,
            "unlisted_child_refused": proc_refused and len(probe_proc) == 1,
            "in_root_positive_written": positive.is_file()}


def prepare_imports(run):
    tmp = run / "tmp"
    tmp.mkdir()
    tempfile.tempdir = str(tmp)
    os.environ["TMPDIR"] = str(tmp)
    sys.dont_write_bytecode = True
    install_offline_guards()
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))


def flatten(suite):
    import unittest
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


def run_names(names, stream):
    import unittest
    suite = unittest.defaultTestLoader.loadTestsFromNames(list(names))
    ids = [t.id() for t in flatten(suite)]
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    return result, ids


def counts(result, ids):
    return {"tests_run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
            "skipped": len(result.skipped), "expected_failures": len(result.expectedFailures),
            "unexpected_successes": len(result.unexpectedSuccesses), "test_ids": ids,
            "failed_ids": sorted({t.id() for t, _ in result.failures}),
            "error_ids": sorted({t.id() for t, _ in result.errors}),
            "skipped_ids": sorted({t.id() for t, _ in result.skipped})}


def clean(c):
    return (c["tests_run"] > 0 and c["failures"] == 0 and c["errors"] == 0 and c["skipped"] == 0
            and c["expected_failures"] == 0 and c["unexpected_successes"] == 0)


def loaded_paths():
    names = ("campaign_tool", "campaign_tool.digest", "campaign_tool.digest.detectors", FOCUSED)
    return {name: getattr(sys.modules.get(name), "__file__", None) for name in names}


def save_raw(run, name, stream):
    raw = stream.getvalue().encode()
    (run / name).write_bytes(raw)
    return {"raw_output": name, "raw_output_sha256": sha256_bytes(raw)}


def check_focused(run):
    prepare_imports(run)
    stream = io.StringIO()
    result, ids = run_names([FOCUSED], stream)
    c = counts(result, ids)
    ok = clean(c) and c["tests_run"] == FOCUSED_TEST_COUNT
    return ok, {"selectors": [FOCUSED], "counts": c, "expected_test_count": FOCUSED_TEST_COUNT,
                "loaded_module_paths": loaded_paths(), **save_raw(run, "unittest.txt", stream)}


def check_compatibility(run):
    prepare_imports(run)
    stream = io.StringIO()
    result, ids = run_names(COMPATIBILITY, stream)
    c = counts(result, ids)
    return clean(c), {"selectors": list(COMPATIBILITY), "counts": c,
                      "full_discovery": "operator-run (not executed by this runner)",
                      **save_raw(run, "unittest.txt", stream)}


def resolve_base_pin():
    full = git("rev-parse", "--verify", "--quiet", f"{BRIEF['base_pin_prefix']}^{{commit}}")
    if full is None or not full.startswith(BRIEF["base_pin_prefix"]):
        return None
    return full


def baseline_has_original_pattern():
    base = resolve_base_pin()
    blob = git("show", f"{base}:campaign_tool/digest/detectors.py") if base else None
    if blob is None:
        return None
    return f'DURATION = re.compile(r"{ORIGINAL_DURATION}")' in blob.splitlines()


def gate_failures(result, wanted):
    """True when ``wanted`` failed (not errored) only on the FRAGMENT_HIT gate."""
    texts = [text for test, text in result.failures if test.id() == wanted or test.id().startswith(wanted + " ")]
    return (bool(texts) and len(texts) == len(result.failures) and all(FRAGMENT in t for t in texts)
            and not result.errors)


def check_negative(run):
    prepare_imports(run)
    import unittest  # noqa: F401
    from unittest import mock
    from campaign_tool.digest import detectors
    pinned = baseline_has_original_pattern()
    if pinned is not True:
        return False, {"stop": f"original_pattern_not_verified_at_base_pin: {pinned}"}
    disk_before = sha256_bytes((REPO / "campaign_tool/digest/detectors.py").read_bytes())
    candidate = detectors.DURATION
    if candidate.pattern == ORIGINAL_DURATION:
        return False, {"stop": "candidate_pattern_equals_original"}
    stream = io.StringIO()
    mutant = re.compile(ORIGINAL_DURATION)
    per_fail, per_pass = {}, {}
    with mock.patch.object(detectors, "DURATION", mutant):
        installed = detectors.DURATION is mutant
        for name in NEG_FAIL:
            stream.write(f"=== mutant (original pattern), expect FRAGMENT_HIT failure: {name} ===\n")
            result, ids = run_names([name], stream)
            per_fail[name] = {"counts": counts(result, ids), "gate_failed": gate_failures(result, name)}
        for name in NEG_PASS:
            stream.write(f"=== mutant (original pattern), expect pass: {name} ===\n")
            result, ids = run_names([name], stream)
            per_pass[name] = {"counts": counts(result, ids)}
    restored = detectors.DURATION is candidate
    stream.write("=== restored candidate pattern: full focused module ===\n")
    result, ids = run_names([FOCUSED], stream)
    after = counts(result, ids)
    disk_after = sha256_bytes((REPO / "campaign_tool/digest/detectors.py").read_bytes())
    detected = all(v["gate_failed"] and v["counts"]["tests_run"] == 1 and v["counts"]["failures"] >= 1
                   and v["counts"]["errors"] == 0 for v in per_fail.values())
    positives = all(clean(v["counts"]) and v["counts"]["tests_run"] == 1 for v in per_pass.values())
    ok = (installed and restored and detected and positives and clean(after)
          and after["tests_run"] == FOCUSED_TEST_COUNT and disk_before == disk_after)
    return ok, {"original_pattern": ORIGINAL_DURATION, "original_pattern_at_base_pin": pinned,
                "candidate_pattern": candidate.pattern, "mutant_installed": installed,
                "candidate_restored": restored, "mutant_expected_failures": per_fail,
                "mutant_expected_passes": per_pass, "restored_focused": after,
                "detectors_py_sha256_before": disk_before, "detectors_py_sha256_after": disk_after,
                "deciding_gate": f"unittest failure (not error) whose message carries {FRAGMENT}",
                **save_raw(run, "unittest.txt", stream)}


def copy_candidate(dest):
    for tree in COPY_TREES:
        shutil.copytree(REPO / tree, dest / tree, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for relative in COPY_FILES:
        (dest / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / relative, dest / relative)
    mismatched = []
    for base in [REPO / t for t in COPY_TREES]:
        for path in base.rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                rel = path.relative_to(REPO)
                if sha256_bytes(path.read_bytes()) != sha256_bytes((dest / rel).read_bytes()):
                    mismatched.append(str(rel))
    for relative in COPY_FILES:
        if sha256_bytes((REPO / relative).read_bytes()) != sha256_bytes((dest / relative).read_bytes()):
            mismatched.append(relative)
    return mismatched


def under(path, prefix):
    return path is not None and Path(path).resolve().is_relative_to(Path(prefix).resolve())


def check_portability(run):
    prepare_imports(run)
    stream = io.StringIO()
    result, ids = run_names([FOCUSED], stream)
    here = counts(result, ids)
    here_paths = loaded_paths()
    primary = save_raw(run, "prefix-a-unittest.txt", stream)
    prefix_b = run / "prefix-b" / "candidate"
    mismatched = copy_candidate(prefix_b)
    receipts_b = run / "prefix-b-receipts"
    child_argv = [sys.executable, "-B", str(prefix_b / "tools" / "accept_fc2_duration_tokens.py"),
                  "--check", "focused", "--output-root", str(receipts_b)]
    child = subprocess.run(child_argv, cwd=prefix_b, capture_output=True, check=False)
    (run / "prefix-b-stdout.txt").write_bytes(child.stdout)
    (run / "prefix-b-stderr.txt").write_bytes(child.stderr)
    child_runs = sorted(receipts_b.glob("focused-*/result.json")) if receipts_b.is_dir() else []
    child_record = json.loads(child_runs[0].read_text()) if len(child_runs) == 1 else None
    child_paths = (child_record or {}).get("loaded_module_paths", {})
    child_counts = (child_record or {}).get("counts", {})
    a_ok = clean(here) and here["tests_run"] == FOCUSED_TEST_COUNT and all(
        under(p, REPO) for p in here_paths.values())
    b_ok = (child.returncode == 0 and child_record is not None and child_record.get("exit") == 0
            and child_counts.get("tests_run") == FOCUSED_TEST_COUNT and bool(child_paths)
            and all(under(p, prefix_b) for p in child_paths.values()))
    ok = a_ok and b_ok and not mismatched and str(prefix_b.resolve()) != str(REPO)
    return ok, {"prefix_a": str(REPO), "prefix_a_counts": here, "prefix_a_loaded_module_paths": here_paths,
                **{f"prefix_a_{k}": v for k, v in primary.items()},
                "prefix_b": str(prefix_b), "prefix_b_copy_mismatches": mismatched,
                "prefix_b_argv": child_argv, "prefix_b_cwd": str(prefix_b), "prefix_b_exit": child.returncode,
                "prefix_b_stdout_sha256": sha256_bytes(child.stdout),
                "prefix_b_stderr_sha256": sha256_bytes(child.stderr),
                "prefix_b_result": str(child_runs[0]) if len(child_runs) == 1 else None,
                "prefix_b_counts": child_counts, "prefix_b_loaded_module_paths": child_paths}


def check_credentials(run, tool):
    if tool is None:
        return False, {"stop": "missing_prerequisite: detect-secrets not adjacent to the admitted interpreter"}
    scan_argv = [tool, "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"]
    report = run / "secret-scan.json"
    with open(report, "wb") as out:
        scan = subprocess.run(scan_argv, cwd=REPO, stdout=out, stderr=subprocess.PIPE, check=False)
    (run / "secret-scan-stderr.txt").write_bytes(scan.stderr)
    details = {"scan_argv": scan_argv, "scan_cwd": str(REPO), "scan_exit": scan.returncode,
               "scan_json": "secret-scan.json", "scan_json_sha256": sha256_bytes(report.read_bytes())}
    if scan.returncode != 0:
        return False, {**details, "stop": "scan_failed"}
    try:
        data = json.loads(report.read_bytes())
        results = data["results"]
        if not isinstance(results, dict):
            raise ValueError("results not an object")
    except (ValueError, KeyError, TypeError) as exc:
        return False, {**details, "stop": f"malformed_report: {type(exc).__name__}"}
    check_argv = [sys.executable, "-B", str(REPO / "tools" / "check_secret_report.py"), str(report)]
    check = subprocess.run(check_argv, cwd=REPO, capture_output=True, check=False)
    raw = check.stdout + check.stderr
    (run / "check-secret-report.txt").write_bytes(raw)
    last = raw.decode(errors="replace").strip().splitlines()[-1:] if raw else []
    zero = bool(last) and re.search(r"potential secrets: 0;", last[0]) is not None
    return check.returncode == 0 and zero, {
        **details, "detect_secrets_version": data.get("version"), "files_with_scanner_hits": sorted(results),
        "check_argv": check_argv, "check_cwd": str(REPO), "check_exit": check.returncode,
        "check_summary": last, "check_output": "check-secret-report.txt", "check_output_sha256": sha256_bytes(raw)}


def check_public_tree(run):
    argv = [sys.executable, "-B", str(REPO / "tools" / "check_public_tree.py")]
    done = subprocess.run(argv, cwd=REPO, capture_output=True, check=False)
    (run / "public-tree-stdout.txt").write_bytes(done.stdout)
    (run / "public-tree-stderr.txt").write_bytes(done.stderr)
    return done.returncode == 0, {"argv": argv, "cwd": str(REPO), "exit": done.returncode,
                                  "stdout": done.stdout.decode(errors="replace")[-2000:],
                                  "stderr": done.stderr.decode(errors="replace")[-2000:],
                                  "stdout_sha256": sha256_bytes(done.stdout),
                                  "stderr_sha256": sha256_bytes(done.stderr)}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Scoped offline acceptance for FC2-DURATION-TOKENS.")
    parser.add_argument("--check", required=True, choices=CHECKS)
    parser.add_argument("--output-root", required=True)
    args = parser.parse_args(argv)
    run = fresh_run_dir(args.output_root, args.check)
    sibling = Path(sys.executable).parent / "detect-secrets"
    tool = str(sibling) if sibling.is_file() and os.access(sibling, os.X_OK) else None
    ALLOWED_EXE.update({"git", sys.executable})
    if tool:
        ALLOWED_EXE.add(tool)
    before = identity()
    guard = install_hooks(run)
    try:
        if not (guard["escape_write_refused"] and guard["unlisted_child_refused"] and guard["in_root_positive_written"]):
            ok, details = False, {"stop": "guard_self_check_failed"}
        elif args.check == "focused":
            ok, details = check_focused(run)
        elif args.check == "compatibility":
            ok, details = check_compatibility(run)
        elif args.check == "negative":
            ok, details = check_negative(run)
        elif args.check == "portability":
            ok, details = check_portability(run)
        elif args.check == "credentials":
            ok, details = check_credentials(run, tool)
        elif args.check == "public-tree":
            ok, details = check_public_tree(run)
        else:
            ok, details = False, {"stop": "unknown_check"}
    except Exception as exc:  # any unexpected failure fails closed
        ok, details = False, {"stop": f"unknown_failure: {type(exc).__name__}: {exc}"}
    after = identity()
    if after != before:
        ok = False
        details["stop"] = "candidate_identity_changed_during_run"
    for name, hits in (("write_outside_run_root_refused", WRITE_HITS), ("unlisted_child_refused", PROC_HITS),
                       ("network_guard_hit", GUARD_HITS)):
        if hits:
            ok = False
            details.setdefault("stop", name)
    guard.update({"refused_writes": list(WRITE_HITS), "refused_children": list(PROC_HITS),
                  "network_guard_hits": list(GUARD_HITS), "children_started": list(PROCS)})
    base = resolve_base_pin() if before["git_checkout"] else None
    ancestry = git("merge-base", "--is-ancestor", base, "HEAD") if base else None
    record = {"check": args.check, "brief": BRIEF, "argv": [sys.executable, *sys.argv], "cwd": os.getcwd(),
              "base_pin_resolved": base, "base_pin_is_ancestor_of_head": ancestry is not None if base else None,
              "interpreter": sys.executable, "python": sys.version.split()[0], "run_dir": str(run),
              "guards": guard, "identity_before": before, "identity_after": after,
              "result": "PASS_REVIEW_REQUIRED" if ok else "FAIL", "exit": 0 if ok else 1, **details}
    (run / "result.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    print(json.dumps(record, indent=2, sort_keys=True))
    return record["exit"]


if __name__ == "__main__":
    raise SystemExit(main())
