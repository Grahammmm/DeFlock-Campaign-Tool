"""Scoped offline acceptance runner for FC2-DURATION-TOKENS.

Exactly one --check per invocation; evidence goes to a fresh, uniquely named
directory below --output-root, which must lie outside this repository:

  focused        tests.runner.test_fc2_duration_tokens (pinned test count)
  compatibility  unchanged existing selectors (no skips admitted)
  negative       in-memory mutants: (A) the original fragment-matching DURATION
                 pattern must make the decimal, grouped and signed regressions
                 fail on their own FRAGMENT_HIT gate, (B) a disabled numeric
                 range guard must make the range regressions fail the same way;
                 supported positives pass under both; the restored candidate
                 then passes the full focused module
  portability    focused tests in-process here AND in a child run from a second
                 copy of the candidate source at a different absolute prefix;
                 loaded module paths and the child's own guard evidence recorded
  credentials    installed detect-secrets (adjacent to this interpreter) scan
                 of the candidate repo + unchanged tools/check_secret_report.py
  public-tree    unchanged tools/check_public_tree.py from the candidate repo

Isolation. Before any application import the runner redirects tempfile to the
per-run directory, disables bytecode writes, installs socket sentinels, refuses
Python-level file writes outside the per-run directory and refuses every child
process whose complete argv/cwd/environment is not an explicitly admitted shape
(see CommandPolicy). Children that run repository scripts (check_public_tree.py,
check_secret_report.py) are started through this same file in its internal
--guarded-child bootstrap, which installs the same guards in the child before
the unchanged script is loaded and writes guard-result.json; the parent fails
closed unless that evidence is complete and clean. --guarded-child is internal
plumbing, not a seventh check. The repository tree is content-hashed before and
after. These are isolation checks, not a sandbox for hostile code.

Receipts keep the acceptance invocation (exact sys.orig_argv, cwd, exit) in
immutable top-level and "invocation" fields; check data, including child
commands and results, live under "details". A pass is REVIEW REQUIRED, never
semantic/legal acceptance or publication.
"""
import argparse
import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import re
import runpy
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import traceback

RUNNER_FILE = Path(__file__).resolve()
REPO = RUNNER_FILE.parents[1]
CHECKS = ("focused", "compatibility", "negative", "portability", "credentials", "public-tree")
BRIEF = {"card": "FC2-DURATION-TOKENS", "task": "t_150541a0",
         # short prefix only (no high-entropy literal); the full commit is resolved via git at run time
         "base_pin_prefix": "9c398829",
         "acceptance": "A1-A6 as frozen in the t_150541a0 card body"}
FOCUSED = "tests.runner.test_fc2_duration_tokens"
FOCUSED_TEST_COUNT = 26
COMPATIBILITY = ("tests.runner.test_digest.DetectorTests",
                 "tests.runner.test_digest.BuildDigestTests.test_no_model_is_detector_only_and_needs_attorney_review",
                 "tests.test_model_input", "tests.test_secret_report", "tests.test_public_tree")
HASHED = ("campaign_tool/digest/detectors.py", "tests/runner/test_fc2_duration_tokens.py",
          "tools/accept_fc2_duration_tokens.py", "tests/runner/test_digest.py", "tests/test_model_input.py",
          "tests/test_secret_report.py", "tests/test_public_tree.py", "tools/check_secret_report.py",
          "tools/check_public_tree.py", "campaign_tool/records/public_scan.py")
# Exact DURATION source at the base pin; the negative control restores it in memory only.
ORIGINAL_DURATION = r"(?i)\b(\d{1,4})\s*[- ]?\s*(day|days|month|months|year|years|week|weeks|hour|hours)\b"
FRAGMENT = "FRAGMENT_HIT"
NEG_FAIL = (f"{FOCUSED}.MeasuredBaselineFailures.test_decimal_1_5_years_is_uninterpreted",
            f"{FOCUSED}.MeasuredBaselineFailures.test_grouped_1000_days_is_uninterpreted",
            f"{FOCUSED}.MeasuredBaselineFailures.test_signed_minus_30_days_is_uninterpreted",
            f"{FOCUSED}.GroupingVariants.test_grouping_variants",
            f"{FOCUSED}.SignedVariants.test_signed_variants")
NEG_FAIL_RANGE = (f"{FOCUSED}.NumericRanges.test_numeric_ranges",
                  f"{FOCUSED}.NumericRangeWhitespace.test_variable_horizontal_whitespace_ranges",
                  f"{FOCUSED}.NumericRangeWhitespace.test_range_then_supported_duration")
NEG_PASS = (f"{FOCUSED}.SupportedDurations.test_days_months_years_weeks_hours",
            f"{FOCUSED}.SupportedDurations.test_hyphenated_30_day_form",
            f"{FOCUSED}.SupportedDurations.test_explicit_zero")
# Files a second-prefix copy needs: the application package plus the focused closure.
COPY_TREES = ("campaign_tool",)
COPY_FILES = ("tests/__init__.py", "tests/runner/__init__.py", "tests/runner/test_fc2_duration_tokens.py",
              "tools/accept_fc2_duration_tokens.py")
# Repository scripts run as guarded children (internal --guarded-child bootstrap).
GUARDED_TARGETS = {"public-tree": "tools/check_public_tree.py", "secret-report": "tools/check_secret_report.py"}
GUARD_SELF_CHECKS = ("escape_write_refused", "unlisted_child_refused", "unlisted_argv_refused",
                     "network_probe_refused", "in_root_positive_written")

GUARD_HITS = []
WRITE_HITS = []
PROC_HITS = []
PROCS = []
POLICY = None


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _norm_cwd(cwd):
    return None if cwd is None else os.path.realpath(os.fsdecode(os.fspath(cwd)))


def _full_base(text, prefix):
    return re.fullmatch(r"[0-9a-f]{40}", text) is not None and text.startswith(prefix)


class CommandPolicy:
    """Explicit child-command shapes. Nothing else may start.

    Admitted: (1) exact (argv, cwd) pairs registered with allow(); (2) the
    runner's own read-only git metadata reads against this repository, with no
    cwd; (3) the unchanged tests/test_secret_report.py child, i.e. this
    interpreter on tools/check_secret_report.py with one report.json inside a
    registered fixture directory, no cwd. Every shape also requires the default
    executable (none given, which subprocess reports as argv[0]) and an
    inherited environment (env=None); shell=True is refused because its argv
    starts with /bin/sh -c.
    """

    def __init__(self, repo, base_prefix):
        self.repo = str(Path(repo).resolve())
        self.base_prefix = base_prefix
        self.exact = []
        self.report_dirs = []

    def allow(self, argv, cwd):
        self.exact.append((tuple(os.fsdecode(a) for a in argv), _norm_cwd(cwd)))

    def allow_report_fixture_dir(self, directory):
        self.report_dirs.append(os.path.realpath(os.fsdecode(directory)))

    def git_read_only(self, argv):
        if argv[:4] != ["git", "--no-optional-locks", "-C", self.repo]:
            return False
        rest = argv[4:]
        if rest in (["rev-parse", "--show-toplevel"], ["rev-parse", "HEAD"], ["status", "--porcelain"],
                    ["rev-parse", "--verify", "--quiet", f"{self.base_prefix}^{{commit}}"]):
            return True
        if len(rest) == 2 and rest[0] == "show" and rest[1].endswith(":campaign_tool/digest/detectors.py"):
            return _full_base(rest[1].split(":", 1)[0], self.base_prefix)
        if len(rest) == 4 and rest[:2] == ["merge-base", "--is-ancestor"] and rest[3] == "HEAD":
            return _full_base(rest[2], self.base_prefix)
        return False

    def report_fixture(self, argv):
        if len(argv) != 3 or argv[0] != sys.executable:
            return False
        if argv[1] != str(Path(self.repo) / GUARDED_TARGETS["secret-report"]):
            return False
        report = os.path.realpath(argv[2])
        return os.path.basename(report) == "report.json" and any(
            report.startswith(d + os.sep) for d in self.report_dirs)

    def permits(self, executable, argv, cwd, env):
        if env is not None or not isinstance(argv, (list, tuple)) or not argv:
            return False
        try:
            argv = [os.fsdecode(a) for a in argv]
            # subprocess reports executable=argv[0] when none was given; any other value is an override
            if executable is not None and os.fsdecode(executable) != argv[0]:
                return False
        except TypeError:
            return False
        cwd = _norm_cwd(cwd)
        if cwd is None and (self.git_read_only(argv) or self.report_fixture(argv)):
            return True
        return (tuple(argv), cwd) in self.exact


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


def prepare_offline(run):
    """Defaults redirect + network sentinels; must run before any application import."""
    tmp = run / "tmp"
    tmp.mkdir()
    tempfile.tempdir = str(tmp)
    os.environ["TMPDIR"] = str(tmp)
    sys.dont_write_bytecode = True
    install_offline_guards()
    return tmp


WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
# audit event -> ((path arg index, dir_fd arg index or None), ...)
WRITE_EVENTS = {"os.mkdir": ((0, 2),), "os.rename": ((0, 2), (1, 3)), "os.replace": ((0, 2), (1, 3)),
                "os.remove": ((0, 1),), "os.rmdir": ((0, 1),), "os.truncate": ((0, None),),
                "os.symlink": ((1, 2),), "os.link": ((1, 3),), "shutil.rmtree": ((0, 1),),
                "sqlite3.connect": ((0, None),)}
REFUSED_PROC_EVENTS = ("os.system", "os.exec", "os.spawn", "os.posix_spawn", "os.fork", "os.forkpty",
                       "pty.spawn", "os.startfile")


def install_hooks(run):
    """Refuse Python-level writes outside the run root and every child POLICY does not admit."""
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

    def describe(argv):
        if isinstance(argv, (list, tuple)):
            return [os.fsdecode(a) if isinstance(a, (str, bytes, os.PathLike)) else repr(a) for a in argv]
        return repr(argv)

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
            executable, argv, cwd, env = (tuple(args) + (None, None, None, None))[:4]
            if POLICY is None or not POLICY.permits(executable, argv, cwd, env):
                PROC_HITS.append({"event": event, "argv": describe(argv),
                                  "cwd": None if cwd is None else os.fsdecode(os.fspath(cwd)),
                                  "env_given": env is not None, "executable_given": executable is not None})
                raise PermissionError("acceptance process guard refused an unlisted child command")
            PROCS.append({"argv": [os.fsdecode(a) for a in argv], "cwd": _norm_cwd(cwd)})
        elif event.startswith(REFUSED_PROC_EVENTS):
            PROC_HITS.append({"event": event})
            raise PermissionError(f"acceptance process guard refused {event}")
    sys.addaudithook(hook)
    # Self-checks. Each probe is inert even if a guard were missing: a write
    # under a nonexistent top-level directory, a nonexistent executable, a git
    # subcommand that does not exist, and a connect to a nonexistent local
    # socket path in the run directory.
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

    def probe_child(argv, marker):
        try:
            subprocess.run(argv, check=False, capture_output=True)
            refused = False
        except PermissionError:
            refused = True
        except OSError:
            refused = False
        hits = [h for h in PROC_HITS if marker in json.dumps(h)]
        PROC_HITS[:] = [h for h in PROC_HITS if marker not in json.dumps(h)]
        return refused and len(hits) == 1
    child_refused = probe_child(["fc2-unlisted-probe"], "fc2-unlisted-probe")
    argv_refused = probe_child(["git", "--no-optional-locks", "-C", str(REPO), "fc2-unlisted-argv-probe"],
                               "fc2-unlisted-argv-probe")
    before = len(GUARD_HITS)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.connect(os.path.join(root, f"fc2-no-such-socket-{secrets.token_hex(4)}"))
        network_refused = False
    except OSError as exc:
        network_refused = "offline acceptance guard refused" in str(exc) and GUARD_HITS[before:] == ["socket.connect"]
    del GUARD_HITS[before:]
    positive = run / "guard-positive.txt"
    positive.write_text("in-root write allowed\n")
    return {"root": root, "policy": "CommandPolicy exact argv/cwd/env shapes",
            "escape_write_refused": write_refused and len(probe_write) == 1,
            "unlisted_child_refused": child_refused, "unlisted_argv_refused": argv_refused,
            "network_probe_refused": network_refused, "in_root_positive_written": positive.is_file()}


def guard_self_checks_ok(guard):
    return all(guard.get(name) is True for name in GUARD_SELF_CHECKS)


def finish_guard(guard):
    guard.update({"refused_writes": list(WRITE_HITS), "refused_children": list(PROC_HITS),
                  "network_guard_hits": list(GUARD_HITS), "children_started": list(PROCS)})
    return guard


def prepare_imports(run):
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


def check_focused(run, tmp):
    prepare_imports(run)
    stream = io.StringIO()
    result, ids = run_names([FOCUSED], stream)
    c = counts(result, ids)
    ok = clean(c) and c["tests_run"] == FOCUSED_TEST_COUNT
    return ok, {"selectors": [FOCUSED], "counts": c, "expected_test_count": FOCUSED_TEST_COUNT,
                "loaded_module_paths": loaded_paths(), "children": [], **save_raw(run, "unittest.txt", stream)}


def check_compatibility(run, tmp):
    prepare_imports(run)
    # The unchanged tests/test_secret_report.py launches this interpreter on
    # tools/check_secret_report.py with a report.json in its TemporaryDirectory,
    # which lives below the redirected per-run tmp directory.
    POLICY.allow_report_fixture_dir(tmp)
    stream = io.StringIO()
    result, ids = run_names(COMPATIBILITY, stream)
    c = counts(result, ids)
    return clean(c), {"selectors": list(COMPATIBILITY), "counts": c,
                      "admitted_fixture_children": "tests.test_secret_report: interpreter + check_secret_report.py "
                                                   "+ report.json below the per-run tmp directory",
                      "full_discovery": "operator-run (not executed by this runner)",
                      **save_raw(run, "unittest.txt", stream)}


def resolve_base_pin():
    full = git("rev-parse", "--verify", "--quiet", f"{BRIEF['base_pin_prefix']}^{{commit}}")
    if full is None or not _full_base(full, BRIEF["base_pin_prefix"]):
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


def run_mutant(stream, label, expect_fail, expect_pass):
    per_fail, per_pass = {}, {}
    for name in expect_fail:
        stream.write(f"=== mutant {label}, expect FRAGMENT_HIT failure: {name} ===\n")
        result, ids = run_names([name], stream)
        per_fail[name] = {"counts": counts(result, ids), "gate_failed": gate_failures(result, name)}
    for name in expect_pass:
        stream.write(f"=== mutant {label}, expect pass: {name} ===\n")
        result, ids = run_names([name], stream)
        per_pass[name] = {"counts": counts(result, ids)}
    detected = all(v["gate_failed"] and v["counts"]["tests_run"] == 1 and v["counts"]["failures"] >= 1
                   and v["counts"]["errors"] == 0 for v in per_fail.values())
    positives = all(clean(v["counts"]) and v["counts"]["tests_run"] == 1 for v in per_pass.values())
    return detected and positives, {"expected_failures": per_fail, "expected_passes": per_pass,
                                    "detected": detected, "positives_pass": positives}


def check_negative(run, tmp):
    prepare_imports(run)
    from unittest import mock
    from campaign_tool.digest import detectors
    pinned = baseline_has_original_pattern()
    if pinned is not True:
        return False, {"stop": f"original_pattern_not_verified_at_base_pin: {pinned}", "children": []}
    disk_before = sha256_bytes((REPO / "campaign_tool/digest/detectors.py").read_bytes())
    candidate = detectors.DURATION
    range_lead = getattr(detectors, "NUMERIC_RANGE_LEAD", None)
    if candidate.pattern == ORIGINAL_DURATION or range_lead is None:
        return False, {"stop": "candidate_pattern_equals_original_or_range_guard_missing", "children": []}
    stream = io.StringIO()
    mutant_a = re.compile(ORIGINAL_DURATION)
    with mock.patch.object(detectors, "DURATION", mutant_a):
        a_installed = detectors.DURATION is mutant_a
        a_ok, a = run_mutant(stream, "A (original DURATION pattern)", NEG_FAIL, NEG_PASS)
    mutant_b = re.compile(r"(?!)")  # never matches: the range guard is disabled
    with mock.patch.object(detectors, "NUMERIC_RANGE_LEAD", mutant_b):
        b_installed = detectors.NUMERIC_RANGE_LEAD is mutant_b
        b_ok, b = run_mutant(stream, "B (numeric range guard disabled)", NEG_FAIL_RANGE, NEG_PASS)
    restored = detectors.DURATION is candidate and detectors.NUMERIC_RANGE_LEAD is range_lead
    stream.write("=== restored candidate: full focused module ===\n")
    result, ids = run_names([FOCUSED], stream)
    after = counts(result, ids)
    disk_after = sha256_bytes((REPO / "campaign_tool/digest/detectors.py").read_bytes())
    ok = (a_installed and b_installed and a_ok and b_ok and restored and clean(after)
          and after["tests_run"] == FOCUSED_TEST_COUNT and disk_before == disk_after)
    return ok, {"original_pattern": ORIGINAL_DURATION, "original_pattern_at_base_pin": pinned,
                "candidate_pattern": candidate.pattern, "candidate_range_guard": range_lead.pattern,
                "mutant_a_installed": a_installed, "mutant_a": a,
                "mutant_b_installed": b_installed, "mutant_b_pattern": mutant_b.pattern, "mutant_b": b,
                "candidate_restored": restored, "restored_focused": after,
                "detectors_py_sha256_before": disk_before, "detectors_py_sha256_after": disk_after,
                "deciding_gate": f"unittest failure (not error) whose message carries {FRAGMENT}",
                "children": [], **save_raw(run, "unittest.txt", stream)}


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


def child_guard_problems(guard, expected_children, read_only_repo=None):
    """Fail-closed review of a child's own guard evidence."""
    problems = []
    if not isinstance(guard, dict):
        return ["guard_evidence_missing"]
    for name in GUARD_SELF_CHECKS:
        if guard.get(name) is not True:
            problems.append(f"self_check_not_true:{name}")
    for name in ("refused_writes", "refused_children", "network_guard_hits"):
        if guard.get(name) != []:
            problems.append(f"nonempty:{name}")
    started = guard.get("children_started")
    if not isinstance(started, list):
        problems.append("children_started_missing")
    elif read_only_repo is not None:
        policy = CommandPolicy(read_only_repo, BRIEF["base_pin_prefix"])
        for entry in started:
            if entry.get("cwd") is not None or not policy.git_read_only(entry.get("argv") or []):
                problems.append(f"unexpected_child:{entry}")
    elif started != expected_children:
        problems.append(f"children_started_mismatch:{started}")
    return problems


def check_portability(run, tmp):
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
    POLICY.allow(child_argv, prefix_b)
    child = subprocess.run(child_argv, cwd=prefix_b, capture_output=True, check=False)
    (run / "prefix-b-stdout.txt").write_bytes(child.stdout)
    (run / "prefix-b-stderr.txt").write_bytes(child.stderr)
    child_runs = sorted(receipts_b.glob("focused-*/result.json")) if receipts_b.is_dir() else []
    child_record = json.loads(child_runs[0].read_text()) if len(child_runs) == 1 else None
    record = child_record or {}
    child_details = record.get("details", {})
    child_paths = child_details.get("loaded_module_paths", {})
    child_counts = child_details.get("counts", {})
    problems = [] if child_record is not None else ["child_result_missing_or_ambiguous"]
    if child_record is not None:
        problems += [f"receipt:{p}" for p in validate_record(child_record, "focused", child_argv, str(prefix_b))]
        problems += [f"guard:{p}" for p in child_guard_problems(record.get("guards"), None, prefix_b)]
    a_ok = clean(here) and here["tests_run"] == FOCUSED_TEST_COUNT and all(
        under(p, REPO) for p in here_paths.values())
    b_ok = (child.returncode == 0 and child_record is not None and record.get("exit") == 0 and not problems
            and child_counts.get("tests_run") == FOCUSED_TEST_COUNT and bool(child_paths)
            and all(under(p, prefix_b) for p in child_paths.values()))
    ok = a_ok and b_ok and not mismatched and str(prefix_b.resolve()) != str(REPO)
    receipt = {"name": "prefix-b-focused", "argv": child_argv, "cwd": str(prefix_b), "exit": child.returncode,
               "stdout_file": "prefix-b-stdout.txt", "stdout_sha256": sha256_bytes(child.stdout),
               "stderr_file": "prefix-b-stderr.txt", "stderr_sha256": sha256_bytes(child.stderr),
               "result_json": str(child_runs[0]) if len(child_runs) == 1 else None,
               "result_json_sha256": sha256_bytes(child_runs[0].read_bytes()) if len(child_runs) == 1 else None,
               "guard_ok": child_record is not None and not problems, "guard_problems": problems}
    return ok, {"prefix_a": str(REPO), "prefix_a_counts": here, "prefix_a_loaded_module_paths": here_paths,
                **{f"prefix_a_{k}": v for k, v in primary.items()},
                "prefix_b": str(prefix_b), "prefix_b_copy_mismatches": mismatched,
                "prefix_b_counts": child_counts, "prefix_b_loaded_module_paths": child_paths,
                "prefix_b_guards": record.get("guards"), "children": [receipt]}


def guarded_child_argv(name, child_dir, target_args):
    """Exact command for a guarded repository-script child (see --guarded-child)."""
    if name not in GUARDED_TARGETS:
        raise ValueError(f"unknown guarded child: {name}")
    target_args = [os.fsdecode(a) for a in target_args]
    if name == "public-tree" and target_args:
        raise ValueError("public-tree child takes no arguments")
    if name == "secret-report" and len(target_args) != 1:
        raise ValueError("secret-report child takes exactly one report path")
    return [sys.executable, "-B", str(RUNNER_FILE), "--guarded-child", name, "--child-run-dir",
            os.fsdecode(child_dir), "--", *target_args]


def guarded_child_allowed_commands(name):
    """Grandchildren a guarded child may start: the public scanner's own git file listing only."""
    if name == "public-tree":
        return [(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], str(REPO))]
    return []


def run_guarded_child(run, name, target_args):
    child_dir = run / f"child-{name}"
    child_dir.mkdir()
    argv = guarded_child_argv(name, child_dir, target_args)
    POLICY.allow(argv, REPO)
    done = subprocess.run(argv, cwd=REPO, capture_output=True, check=False)
    (run / f"{name}-stdout.txt").write_bytes(done.stdout)
    (run / f"{name}-stderr.txt").write_bytes(done.stderr)
    evidence = child_dir / "guard-result.json"
    problems, data = [], None
    try:
        data = json.loads(evidence.read_bytes())
    except (OSError, ValueError) as exc:
        problems.append(f"guard_result_unreadable:{type(exc).__name__}")
    if data is not None:
        expected = [{"argv": a, "cwd": c} for a, c in guarded_child_allowed_commands(name)]
        if data.get("name") != name or data.get("target") != str(REPO / GUARDED_TARGETS[name]):
            problems.append("guard_result_identity_mismatch")
        if data.get("target_args") != [os.fsdecode(a) for a in target_args]:
            problems.append("guard_result_args_mismatch")
        if data.get("problems") != []:
            problems.append(f"child_reported:{data.get('problems')}")
        if data.get("target_exit") != done.returncode or data.get("guards_installed_before_target") is not True:
            problems.append("guard_result_exit_or_order_mismatch")
        modules = data.get("application_modules")
        if not isinstance(modules, dict) or not all(under(p, REPO) for p in modules.values() if p):
            problems.append("application_modules_outside_repo")
        problems += child_guard_problems(data.get("guards"), expected)
    receipt = {"name": name, "argv": argv, "cwd": str(REPO), "exit": done.returncode,
               "stdout_file": f"{name}-stdout.txt", "stdout_sha256": sha256_bytes(done.stdout),
               "stderr_file": f"{name}-stderr.txt", "stderr_sha256": sha256_bytes(done.stderr),
               "guard_result_file": str(evidence.relative_to(run)),
               "guard_result_sha256": sha256_bytes(evidence.read_bytes()) if evidence.is_file() else None,
               "guard_result": data, "guard_ok": not problems, "guard_problems": problems}
    return done, receipt


def check_credentials(run, tmp, tool):
    if tool is None:
        return False, {"stop": "missing_prerequisite: detect-secrets not adjacent to the admitted interpreter",
                       "children": []}
    scan_argv = [tool, "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"]
    POLICY.allow(scan_argv, REPO)
    report = run / "secret-scan.json"
    with open(report, "wb") as out:
        scan = subprocess.run(scan_argv, cwd=REPO, stdout=out, stderr=subprocess.PIPE, check=False)
    (run / "secret-scan-stderr.txt").write_bytes(scan.stderr)
    scan_receipt = {"name": "detect-secrets-scan", "argv": scan_argv, "cwd": str(REPO), "exit": scan.returncode,
                    "stdout_file": "secret-scan.json", "stdout_sha256": sha256_bytes(report.read_bytes()),
                    "stderr_file": "secret-scan-stderr.txt", "stderr_sha256": sha256_bytes(scan.stderr)}
    details = {"scan_json": "secret-scan.json", "scan_json_sha256": scan_receipt["stdout_sha256"],
               "children": [scan_receipt]}
    if scan.returncode != 0:
        return False, {**details, "stop": "scan_failed"}
    try:
        data = json.loads(report.read_bytes())
        results = data["results"]
        if not isinstance(results, dict):
            raise ValueError("results not an object")
    except (ValueError, KeyError, TypeError) as exc:
        return False, {**details, "stop": f"malformed_report: {type(exc).__name__}"}
    check, check_receipt = run_guarded_child(run, "secret-report", [str(report)])
    details["children"].append(check_receipt)
    out_text = check.stdout.decode(errors="replace").strip().splitlines()
    last = out_text[-1:] if out_text else []
    zero = bool(last) and re.search(r"potential secrets: 0;", last[0]) is not None
    ok = check.returncode == 0 and zero and check_receipt["guard_ok"]
    return ok, {**details, "detect_secrets_version": data.get("version"), "files_with_scanner_hits": sorted(results),
                "check_summary": last}


def check_public_tree(run, tmp):
    done, receipt = run_guarded_child(run, "public-tree", [])
    summary = None
    try:
        parsed = json.loads(done.stdout)
        summary = {"scanned_files": parsed.get("scanned_files"), "findings": parsed.get("findings")}
    except ValueError:
        pass
    ok = done.returncode == 0 and receipt["guard_ok"] and summary is not None and summary["findings"] == []
    return ok, {"children": [receipt], "summary": summary,
                "stdout_tail": done.stdout.decode(errors="replace")[-2000:],
                "stderr_tail": done.stderr.decode(errors="replace")[-2000:]}


def build_record(check, invocation_argv, invocation_cwd, ok, details, context):
    """Outer invocation fields are written last and can never be replaced by check details."""
    result = "PASS_REVIEW_REQUIRED" if ok else "FAIL"
    exit_code = 0 if ok else 1
    record = {k: v for k, v in dict(context).items() if k not in ("check", "invocation", "argv", "cwd", "exit",
                                                                    "result", "details")}
    record["details"] = dict(details)
    invocation = {"argv": list(invocation_argv), "cwd": invocation_cwd, "exit": exit_code, "result": result}
    record.update({"check": check, "invocation": invocation, "argv": list(invocation_argv), "cwd": invocation_cwd,
                   "exit": exit_code, "result": result})
    return record


def validate_record(record, check, invocation_argv, invocation_cwd):
    """Receipt contract: the outer acceptance invocation is intact and names this check."""
    problems = []
    inv = record.get("invocation")
    if check not in CHECKS or record.get("check") != check:
        problems.append("check_mismatch")
    if not isinstance(inv, dict) or set(inv) != {"argv", "cwd", "exit", "result"}:
        return problems + ["invocation_missing"]
    if inv["argv"] != list(invocation_argv) or inv["cwd"] != invocation_cwd:
        problems.append("invocation_argv_or_cwd_mismatch")
    if (inv["exit"], inv["result"]) not in ((0, "PASS_REVIEW_REQUIRED"), (1, "FAIL")):
        problems.append("invocation_exit_result_inconsistent")
    for key in ("argv", "cwd", "exit", "result"):
        if record.get(key) != inv.get(key):
            problems.append(f"top_level_{key}_differs_from_invocation")
    argv = inv["argv"] if isinstance(inv["argv"], list) else []
    if "--check" not in argv or argv.index("--check") + 1 >= len(argv) or argv[argv.index("--check") + 1] != check:
        problems.append("invocation_does_not_name_check")
    if "--output-root" not in argv:
        problems.append("invocation_missing_output_root")
    if not isinstance(record.get("details"), dict):
        problems.append("details_missing")
    return problems


def guarded_child_main(argv):
    """Internal bootstrap: guards first, then run the unchanged repository script as __main__."""
    global POLICY
    if len(argv) < 5 or argv[0] != "--guarded-child" or argv[2] != "--child-run-dir" or argv[4] != "--":
        print("guarded child: malformed arguments", file=sys.stderr)
        return 2
    name, child_dir, target_args = argv[1], Path(argv[3]), argv[5:]
    try:
        if argv != guarded_child_argv(name, child_dir, target_args)[3:]:
            raise ValueError("argument shape")
    except ValueError as exc:
        print(f"guarded child: {exc}", file=sys.stderr)
        return 2
    resolved = child_dir.resolve()
    if (not child_dir.is_absolute() or not resolved.is_dir() or any(resolved.iterdir())
            or resolved == REPO or REPO in resolved.parents):
        print("guarded child: child run directory must be an existing empty directory outside the repo",
              file=sys.stderr)
        return 2
    POLICY = CommandPolicy(REPO, BRIEF["base_pin_prefix"])
    for allowed, cwd in guarded_child_allowed_commands(name):
        POLICY.allow(allowed, cwd)
    prepare_offline(resolved)
    guard = install_hooks(resolved)
    target = REPO / GUARDED_TARGETS[name]
    problems, code, ran = [], None, False
    installed_first = guard_self_checks_ok(guard) and "campaign_tool" not in sys.modules
    if not installed_first:
        problems.append("guard_self_check_failed")
        code = 3
    else:
        sys.argv = [str(target), *target_args]
        sys.path[0] = str(target.parent)
        ran = True
        try:
            runpy.run_path(str(target), run_name="__main__")
            code = 0
        except SystemExit as exc:
            if exc.code is None:
                code = 0
            elif isinstance(exc.code, int):
                code = exc.code
            else:
                print(exc.code, file=sys.stderr)
                code = 1
        except BaseException:  # mirror the interpreter: traceback and exit 1
            traceback.print_exc()
            code = 1
        sys.stdout.flush()
        sys.stderr.flush()
    finish_guard(guard)
    if WRITE_HITS or PROC_HITS or GUARD_HITS:
        problems.append("guard_hits_recorded")
    if problems and code == 0:
        code = 3
    modules = {m: getattr(mod, "__file__", None) for m, mod in sorted(sys.modules.items())
               if m == "campaign_tool" or m.startswith("campaign_tool.")}
    evidence = {"name": name, "target": str(target), "target_sha256": sha256_bytes(target.read_bytes()),
                "target_args": list(target_args), "target_ran": ran, "guards_installed_before_target": installed_first,
                "target_exit": code, "guards": guard, "application_modules": modules, "problems": problems,
                "python": sys.version.split()[0], "interpreter": sys.executable, "orig_argv": list(sys.orig_argv)}
    (resolved / "guard-result.json").write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    return code


def main(argv=None):
    global POLICY
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--guarded-child"]:
        return guarded_child_main(argv)
    parser = argparse.ArgumentParser(description="Scoped offline acceptance for FC2-DURATION-TOKENS.")
    parser.add_argument("--check", required=True, choices=CHECKS)
    parser.add_argument("--output-root", required=True)
    args = parser.parse_args(argv)
    invocation_argv, invocation_cwd = list(sys.orig_argv), os.getcwd()
    run = fresh_run_dir(args.output_root, args.check)
    sibling = Path(sys.executable).parent / "detect-secrets"
    tool = str(sibling) if sibling.is_file() and os.access(sibling, os.X_OK) else None
    POLICY = CommandPolicy(REPO, BRIEF["base_pin_prefix"])
    tmp = prepare_offline(run)
    guard = install_hooks(run)
    before = identity()
    try:
        if not guard_self_checks_ok(guard):
            ok, details = False, {"stop": "guard_self_check_failed", "children": []}
        elif args.check == "focused":
            ok, details = check_focused(run, tmp)
        elif args.check == "compatibility":
            ok, details = check_compatibility(run, tmp)
        elif args.check == "negative":
            ok, details = check_negative(run, tmp)
        elif args.check == "portability":
            ok, details = check_portability(run, tmp)
        elif args.check == "credentials":
            ok, details = check_credentials(run, tmp, tool)
        elif args.check == "public-tree":
            ok, details = check_public_tree(run, tmp)
        else:
            ok, details = False, {"stop": "unknown_check", "children": []}
    except Exception as exc:  # any unexpected failure fails closed
        ok, details = False, {"stop": f"unknown_failure: {type(exc).__name__}: {exc}",
                              "traceback": traceback.format_exc()}
    after = identity()
    if after != before:
        ok = False
        details["stop"] = "candidate_identity_changed_during_run"
    for name, hits in (("write_outside_run_root_refused", WRITE_HITS), ("unlisted_child_refused", PROC_HITS),
                       ("network_guard_hit", GUARD_HITS)):
        if hits:
            ok = False
            details.setdefault("stop", name)
    finish_guard(guard)
    base = resolve_base_pin() if before["git_checkout"] else None
    ancestry = None
    if base:
        done = subprocess.run(["git", "--no-optional-locks", "-C", str(REPO), "merge-base", "--is-ancestor", base,
                               "HEAD"], capture_output=True, check=False)
        ancestry = done.returncode == 0
    context = {"brief": BRIEF, "base_pin_resolved": base, "base_pin_is_ancestor_of_head": ancestry,
               "interpreter": sys.executable, "python": sys.version.split()[0], "run_dir": str(run),
               "guards": guard, "identity_before": before, "identity_after": after}
    record = build_record(args.check, invocation_argv, invocation_cwd, ok, details, context)
    problems = validate_record(record, args.check, invocation_argv, invocation_cwd)
    if problems:
        details = {**details, "contract_problems": problems, "stop": details.get("stop", "receipt_contract")}
        record = build_record(args.check, invocation_argv, invocation_cwd, False, details, context)
    (run / "result.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    print(json.dumps(record, indent=2, sort_keys=True))
    return record["exit"]


if __name__ == "__main__":
    raise SystemExit(main())
