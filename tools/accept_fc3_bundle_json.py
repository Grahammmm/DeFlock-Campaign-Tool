"""Acceptance runner for FC3-BUNDLE-JSON (offline, synthetic, review-required).

Usage (exactly one check per invocation):
    <interpreter> tools/accept_fc3_bundle_json.py --check CHECK --output-root DIR

CHECK is one of: focused, compatibility, negative, portability, credentials,
public-tree. Every invocation creates a new exclusive run directory under DIR
and writes ``run.json`` there with the actual argv, cwd, exits, test counts,
case coverage, raw output paths and SHA256 hashes. The process exits 0 only
when every assertion of the selected check holds.

The repository root comes from this file's location; the import path is set
only inside this script. In-process checks install network/process sentinels
and redirect the temporary directory before the application is imported.
Children are argument arrays without a shell, run by this same interpreter or
the scanner installed beside it. Passing is machine validity only: REVIEW
REQUIRED, never semantic/legal/stage acceptance, activation or publication.
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
import sys

# Standard-library network and process modules load before any sentinel.
import http.client
import socket
import ssl  # noqa: F401  (loaded before sentinels so its classes import normally)
import subprocess
import tempfile
import unittest
from unittest import mock
import urllib.request

sys.dont_write_bytecode = True

REPO = Path(__file__).resolve().parents[1]
CHECKS = ("focused", "compatibility", "negative", "portability", "credentials", "public-tree")
BRIEF = {
    "scope": "FC3-BUNDLE-JSON",
    "card": "t_48188e28",
    "title": "Reject overflowing JSON numbers at the private review bundle boundary.",
    # Short prefixes, resolved by git at run time and recorded in full.
    "brief_pin_prefix": "9c398829",
    "staged_base_prefix": "3e1a176c",
    "review_bundle_pinned_sha256_prefix": "32bc9ca7",
}
CASES = ("finite_scalar_and_nested", "positive_and_negative_overflow", "nested_overflow",
         "explicit_nonfinite_tokens", "duplicate_keys_still_rejected", "byte_bound_exact_and_over",
         "finite_underflow_and_signed_zero", "no_authority_or_io_required")
BUNDLE_MODULE = "campaign_tool.records.review_bundle"
FOCUSED_MODULE = "tests.records.test_fc3_bundle_json"
COMPAT_SELECTORS = ("tests.records.test_review_bundle", "tests.records.test_proposal_provenance",
                    "tests.test_secret_report", "tests.test_public_tree")
CHANGED_FILES = ("campaign_tool/records/review_bundle.py", "tests/records/test_fc3_bundle_json.py",
                 "tools/accept_fc3_bundle_json.py")
HASHED_FILES = CHANGED_FILES + ("tools/check_secret_report.py", "tools/check_public_tree.py",
                                "tests/records/test_review_bundle.py")
HOOK = "_finite_float"
FAIL_MARK = "FC3 typed refusal missing"
_T = FOCUSED_MODULE + "."
# With the finite hook replaced by plain float, exactly these tests must fail.
MUTANT_MUST_FAIL = frozenset(_T + name for name in (
    "PositiveAndNegativeOverflow.test_positive_overflow_scalars_rejected",
    "PositiveAndNegativeOverflow.test_negative_overflow_scalars_rejected",
    "PositiveAndNegativeOverflow.test_overflow_with_surrounding_whitespace_rejected",
    "NestedOverflow.test_every_overflow_in_every_nested_form_rejected",
    "NestedOverflow.test_overflow_after_many_valid_items_rejected",
    "SyntheticBundleBoundary.test_overflow_public_content_refused_before_any_write",
    "SyntheticBundleBoundary.test_planted_overflow_receipt_refused_as_invalid_json",
    "ByteBoundExactAndOver.test_explicit_bound_with_numbers",
    "NoAuthorityOrIoRequired.test_decode_needs_no_authority_files_or_network",
))
# Assembled from parts so these lists never match their own source text.
BOX_SPECIFIC = ("/ho" + "me/", "agent" + "-work", "venv" + "-full", "/Us" + "ers/", ".her" + "mes",
                "/t" + "mp/", "kan" + "ban/")
INLINE_FORMS = (re.compile(r"""["']-""" + r"""[ce]["']"""), re.compile("shell" + r"\s*=\s*True"),
                re.compile("PYTHON" + "PATH"), re.compile(r"\bos\.sys" + r"tem\("),
                re.compile(r"\bos\.po" + r"pen\("))
RAN = re.compile(r"^Ran (\d+) tests? in ", re.M)
SUMMARY = re.compile(r"^(OK|FAILED)(?: \(([^)]*)\))?\s*$", re.M)
VERBOSE = re.compile(r"^test\w* \(([\w.]+)\)", re.M)

SENTINEL_HITS = []
ALLOWED_CHILDREN = []
_ORIGINAL_POPEN_INIT = subprocess.Popen.__init__


# ---------------------------------------------------------------- utilities
def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def stamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def new_run_dir(output_root, check):
    root = Path(output_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    if root == REPO or REPO in root.parents:
        raise SystemExit("output root must be outside the repository")
    run = root / "{}-{}-{}-{}".format(check, stamp(), os.getpid(), secrets.token_hex(4))
    run.mkdir(mode=0o700, exist_ok=False)
    return run


def write_new(path, data):
    with open(path, "xb") as stream:
        stream.write(data if type(data) is bytes else data.encode("utf-8"))
    return {"path": str(path), "sha256": sha256_file(path)}


# ---------------------------------------------------------------- sentinels
def _blocked(name):
    def refuse(*args, **kwargs):
        SENTINEL_HITS.append(name)
        raise PermissionError("FC3 acceptance sentinel refused " + name)
    return refuse


def _guarded_popen_init(self, args, *rest, **kwargs):
    argv = [str(item) for item in args] if isinstance(args, (list, tuple)) else None
    if kwargs.get("shell") or argv is None or argv not in ALLOWED_CHILDREN:
        SENTINEL_HITS.append("subprocess:" + repr(args)[:200])
        raise PermissionError("FC3 acceptance sentinel refused an unexpected subprocess")
    return _ORIGINAL_POPEN_INIT(self, args, *rest, **kwargs)


def install_sentinels(run_dir):
    """Network/process sentinels and temp redirect, before any application import."""
    for module in list(sys.modules):
        if module == "campaign_tool" or module.startswith("campaign_tool.") or module.startswith("tests."):
            raise RuntimeError("application module imported before sentinels: " + module)
    socket.socket.connect = _blocked("socket.connect")
    socket.socket.connect_ex = _blocked("socket.connect_ex")
    socket.socket.sendto = _blocked("socket.sendto")
    socket.create_connection = _blocked("socket.create_connection")
    socket.getaddrinfo = _blocked("socket.getaddrinfo")
    urllib.request.urlopen = _blocked("urllib.request.urlopen")
    http.client.HTTPConnection.connect = _blocked("http.client.connect")
    subprocess.Popen.__init__ = _guarded_popen_init
    temp = run_dir / "tmp"
    temp.mkdir(mode=0o700)
    home = run_dir / "home"
    home.mkdir(mode=0o700)
    os.environ["TMPDIR"] = str(temp)
    os.environ["HOME"] = str(home)
    tempfile.tempdir = str(temp)
    if str(REPO) in sys.path:
        sys.path.remove(str(REPO))
    sys.path.insert(0, str(REPO))
    return {"tempdir": str(temp), "home": str(home), "sys_path_0": sys.path[0],
            "network": ["socket.connect", "socket.connect_ex", "socket.sendto", "socket.create_connection",
                        "socket.getaddrinfo", "urllib.request.urlopen", "http.client.connect"],
            "process": ["subprocess.Popen allowlist", "os spawn/exec/fork during tests"]}


class TestWindow:
    """Block every os-level process primitive while test code runs in-process."""
    NAMES = ("system", "popen", "fork", "forkpty", "posix_spawn", "posix_spawnp", "execv", "execve",
             "execvp", "execvpe", "execl", "execle", "execlp", "execlpe", "spawnv", "spawnve")

    def __enter__(self):
        self.saved = list(ALLOWED_CHILDREN)
        ALLOWED_CHILDREN.clear()
        self.patches = [mock.patch.object(os, name, _blocked("os." + name))
                        for name in self.NAMES if hasattr(os, name)]
        for patch in self.patches:
            patch.start()
        return self

    def __exit__(self, *exc):
        for patch in reversed(self.patches):
            patch.stop()
        ALLOWED_CHILDREN[:] = self.saved
        return False


def run_child(run_dir, label, argv, cwd, env=None, timeout=1800):
    argv = [str(item) for item in argv]
    record = {"label": label, "argv": argv, "cwd": str(cwd)}
    out_path, err_path = run_dir / (label + ".stdout"), run_dir / (label + ".stderr")
    ALLOWED_CHILDREN.append(argv)
    try:
        with open(out_path, "xb") as out, open(err_path, "xb") as err:
            done = subprocess.run(argv, cwd=str(cwd), stdout=out, stderr=err, stdin=subprocess.DEVNULL,
                                  env=env, timeout=timeout, check=False)
        record["exit"] = done.returncode
    except (OSError, subprocess.SubprocessError) as error:
        record["exit"] = None
        record["launch_error"] = type(error).__name__
    finally:
        ALLOWED_CHILDREN.remove(argv)
    for key, path in (("stdout", out_path), ("stderr", err_path)):
        if path.is_file():
            record[key + "_path"] = str(path)
            record[key + "_sha256"] = sha256_file(path)
    return record


# ---------------------------------------------------------------- identity
def git(*args):
    argv = ["git", "-C", str(REPO), *args]
    ALLOWED_CHILDREN.append(argv)
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=120, check=False)
        return done.returncode, done.stdout
    except (OSError, subprocess.SubprocessError) as error:
        return None, type(error).__name__
    finally:
        ALLOWED_CHILDREN.remove(argv)


def source_identity():
    identity = {"repo": str(REPO), "hashes": {}}
    for relative in HASHED_FILES:
        path = REPO / relative
        identity["hashes"][relative] = sha256_file(path) if path.is_file() else None
    if not (REPO / ".git").exists():
        identity["git"] = "no_git_metadata_in_this_copy"
        return identity
    code, head = git("rev-parse", "HEAD")
    identity["head"] = head.strip() if code == 0 else None
    code, status = git("status", "--porcelain", "--untracked-files=all")
    identity["status_porcelain"] = status if code == 0 else None
    identity["clean"] = code == 0 and status == ""
    for key in ("brief_pin_prefix", "staged_base_prefix"):
        code, full = git("rev-parse", "--verify", BRIEF[key] + "^{commit}")
        identity[key.replace("_prefix", "")] = full.strip() if code == 0 else None
    pin, base = identity.get("brief_pin"), identity.get("staged_base")
    if pin and base:
        code, _ = git("merge-base", "--is-ancestor", pin, base)
        identity["brief_pin_is_ancestor_of_staged_base"] = code == 0
        code, delta = git("diff", "--name-only", pin, base, "--", *HASHED_FILES[3:],
                          "campaign_tool/records/review_bundle.py", *[s.replace(".", "/") + ".py"
                                                                      for s in COMPAT_SELECTORS])
        identity["pin_to_staged_base_delta_on_scope_files"] = delta.split() if code == 0 else None
        code, changed = git("diff", "--name-only", base)
        names = set(changed.split()) if code == 0 else None
        code, untracked = git("ls-files", "--others", "--exclude-standard")
        if names is not None and code == 0:
            names |= set(untracked.split())
            identity["changed_since_staged_base"] = sorted(names)
            identity["changes_within_admitted_files"] = names <= set(CHANGED_FILES)
        code, committed = git("diff", "--name-only", base, "HEAD")
        identity["committed_since_staged_base"] = committed.split() if code == 0 else None
    return identity


def scope_reasons(identity):
    reasons = []
    if identity.get("git") is None:
        if identity.get("head") is None:
            reasons.append("head_unavailable")
        if identity.get("brief_pin_is_ancestor_of_staged_base") is not True:
            reasons.append("brief_pin_not_ancestor_of_staged_base")
        if identity.get("pin_to_staged_base_delta_on_scope_files") != []:
            reasons.append("scope_files_changed_between_pin_and_staged_base")
        if identity.get("changes_within_admitted_files") is not True:
            reasons.append("changes_outside_admitted_files")
    return reasons


# ---------------------------------------------------------------- unittest
def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


def owner_id(test):
    return getattr(test, "test_case", test).id()


def run_tests(run_dir, label, suite):
    """Run in-process under the process window; return explicit result facts."""
    tests = list(flatten(suite))
    stream = io.StringIO()
    with TestWindow():
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(unittest.TestSuite(tests))
    failing = {}
    for kind, rows in (("failure", result.failures), ("error", result.errors),
                       ("unexpected_success", [(t, "") for t in result.unexpectedSuccesses])):
        for test, text in rows:
            failing.setdefault(owner_id(test), []).append({"kind": kind, "marked": FAIL_MARK in text})
    output = write_new(run_dir / (label + ".unittest.txt"), stream.getvalue())
    cases = {}
    for test in tests:
        case = getattr(type(test), "CASE", None)
        cases.setdefault(case, {"tests": 0, "passing": 0})
        cases[case]["tests"] += 1
        cases[case]["passing"] += test.id() not in failing
    return {"label": label, "tests_run": result.testsRun, "test_ids": [t.id() for t in tests],
            "failing": failing, "skipped": [owner_id(t) for t, _ in result.skipped],
            "expected_failures": [owner_id(t) for t, _ in result.expectedFailures],
            "was_successful": result.wasSuccessful(), "case_coverage": cases, "output": output}


def load_focused():
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromName(FOCUSED_MODULE)
    if loader.errors:
        raise RuntimeError("focused import errors: " + " | ".join(loader.errors)[:2000])
    return suite


def loaded_modules():
    names = ("campaign_tool", BUNDLE_MODULE, FOCUSED_MODULE)
    paths = {name: str(Path(sys.modules[name].__file__).resolve()) for name in names if name in sys.modules}
    paths["all_under_repo"] = all(Path(paths.get(name, "/")).is_relative_to(REPO) for name in names)
    return paths


def focused_reasons(facts):
    reasons = []
    if facts["tests_run"] <= 0:
        reasons.append("zero_tests")
    if facts["failing"]:
        reasons.append("unexpected_failures:" + ",".join(sorted(facts["failing"]))[:1500])
    if facts["skipped"] or facts["expected_failures"]:
        reasons.append("skips_or_expected_failures_present")
    for case in CASES:
        row = facts["case_coverage"].get(case)
        if not row or row["passing"] < 1 or row["passing"] != row["tests"]:
            reasons.append("case_not_fully_green:" + case)
    unknown = set(facts["case_coverage"]) - set(CASES)
    if unknown:
        reasons.append("tests_without_frozen_case:" + repr(sorted(map(str, unknown))))
    return reasons


# ---------------------------------------------------------------- checks
def check_focused(run_dir, record):
    record["sentinels"] = install_sentinels(run_dir)
    facts = run_tests(run_dir, "focused", load_focused())
    record["focused"] = facts
    record["loaded_modules"] = loaded_modules()
    reasons = focused_reasons(facts)
    if not record["loaded_modules"]["all_under_repo"]:
        reasons.append("module_loaded_outside_repo")
    return reasons


def parse_unittest_stream(text):
    ran = RAN.findall(text)
    summary = SUMMARY.findall(text)
    modules = {}
    for owner in VERBOSE.findall(text):
        for selector in COMPAT_SELECTORS:
            if owner.startswith(selector + "."):
                modules[selector] = modules.get(selector, 0) + 1
    details = summary[-1][1] if summary else ""
    skipped = re.search(r"skipped=(\d+)", details)
    return {"ran": int(ran[-1]) if ran else None, "status": summary[-1][0] if summary else None,
            "details": details, "skipped": int(skipped[1]) if skipped else 0, "per_selector": modules}


def check_compatibility(run_dir, record):
    temp = run_dir / "tmp"
    temp.mkdir(mode=0o700)
    env = dict(os.environ, TMPDIR=str(temp))
    child = run_child(run_dir, "compatibility", [sys.executable, "-m", "unittest", "-v", *COMPAT_SELECTORS],
                      REPO, env=env)
    record["children"] = [child]
    search_variable = "PYTHON" + "PATH"
    record["child_env_notes"] = {"TMPDIR": str(temp), "search_path_variable_present": search_variable in os.environ}
    parsed = parse_unittest_stream(Path(child["stderr_path"]).read_text(errors="replace")) \
        if "stderr_path" in child else {}
    record["compatibility"] = parsed
    reasons = []
    if child.get("exit") != 0:
        reasons.append("compatibility_exit_%s" % child.get("exit"))
    if not parsed.get("ran"):
        reasons.append("zero_or_unparsed_tests")
    if parsed.get("status") != "OK":
        reasons.append("compatibility_not_ok")
    if parsed.get("skipped"):
        reasons.append("unexpected_skips")
    for selector in COMPAT_SELECTORS:
        if not parsed.get("per_selector", {}).get(selector):
            reasons.append("selector_ran_no_tests:" + selector)
    return reasons


def check_negative(run_dir, record):
    record["sentinels"] = install_sentinels(run_dir)
    suite = load_focused()
    bundle = sys.modules[BUNDLE_MODULE]
    record["loaded_modules"] = loaded_modules()
    reasons = []
    original = getattr(bundle, HOOK, None)
    if not callable(original):
        record["negative"] = {"hook_present": False}
        return ["negative_check_unavailable:hook_missing"]
    source_before = sha256_file(bundle.__file__)
    neg = {"bypassed_branch": BUNDLE_MODULE + "." + HOOK
           + " (decode's parse_float finiteness hook) replaced in memory by builtins.float",
           "hook_present": True, "module_source_sha256_before": source_before}

    def probe(label):
        try:
            value = bundle.decode(b"[1e999]")
        except bundle.BundleError as error:
            return {"probe": label, "outcome": "BundleError", "reason": list(error.args)}
        except Exception as error:  # untyped escape is recorded, never hidden
            return {"probe": label, "outcome": "untyped:" + type(error).__name__}
        return {"probe": label, "outcome": "accepted", "value_repr": repr(value)}

    with mock.patch.object(bundle, HOOK, float):
        neg["mutant_hook_is_float"] = getattr(bundle, HOOK) is float
        neg["mutant_probe"] = probe("mutant")
        mutant = run_tests(run_dir, "mutant", suite)
    neg["restored_hook_identity"] = getattr(bundle, HOOK) is original
    neg["restored_probe"] = probe("restored")
    restored = run_tests(run_dir, "restored", load_focused())
    neg["module_source_sha256_after"] = sha256_file(bundle.__file__)
    neg["mutant"], neg["restored"] = mutant, restored
    failed = set(mutant["failing"])
    neg["mutant_failed_tests"] = sorted(failed)
    neg["mutant_control_tests_passing"] = sorted(set(mutant["test_ids"]) - failed)
    record["negative"] = neg
    if not neg["mutant_hook_is_float"] or not neg["restored_hook_identity"]:
        reasons.append("mutation_not_applied_or_not_restored")
    if neg["mutant_probe"]["outcome"] != "accepted":
        reasons.append("mutant_probe_not_bypassed")
    if neg["restored_probe"] != {"probe": "restored", "outcome": "BundleError", "reason": ["invalid_json"]}:
        reasons.append("restored_probe_not_typed_refusal")
    if mutant["tests_run"] <= 0 or failed != MUTANT_MUST_FAIL:
        reasons.append("mutant_failure_set_mismatch:missing=%s:extra=%s" % (
            sorted(MUTANT_MUST_FAIL - failed), sorted(failed - MUTANT_MUST_FAIL)))
    for test_id, rows in mutant["failing"].items():
        if not all(row["kind"] == "failure" and row["marked"] for row in rows):
            reasons.append("mutant_failure_not_typed_refusal_assertion:" + test_id)
    if mutant["skipped"] or len(mutant["test_ids"]) - len(failed) < 1:
        reasons.append("mutant_controls_missing_or_skipped")
    reasons.extend("restored:" + reason for reason in focused_reasons(restored))
    if neg["module_source_sha256_after"] != source_before:
        reasons.append("source_mutated_on_disk")
    if not record["loaded_modules"]["all_under_repo"]:
        reasons.append("module_loaded_outside_repo")
    return reasons


def static_portability_scan():
    findings = []
    for relative in CHANGED_FILES[1:]:
        text = (REPO / relative).read_text()
        for token in BOX_SPECIFIC:
            if token in text:
                findings.append({"file": relative, "box_specific": token})
        for pattern in INLINE_FORMS:
            if pattern.search(text):
                findings.append({"file": relative, "inline_or_shell_form": pattern.pattern})
    return findings


def check_portability(run_dir, record):
    record["static_scan"] = static_portability_scan()
    reasons = ["static:" + json.dumps(item) for item in record["static_scan"]]
    copy_root = run_dir / "second-prefix" / ("copy-" + secrets.token_hex(6)) / "repo"
    copy_root.mkdir(parents=True)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    for top in ("campaign_tool", "tests", "tools"):
        shutil.copytree(REPO / top, copy_root / top, symlinks=True, ignore=ignore)
    copied = {relative: sha256_file(copy_root / relative) for relative in CHANGED_FILES}
    original = {relative: sha256_file(REPO / relative) for relative in CHANGED_FILES}
    record["copy"] = {"root": str(copy_root), "hashes": copied, "match_original": copied == original}
    if copied != original:
        reasons.append("copy_hash_mismatch")
    copy_out = run_dir / "copy-focused-output"
    child = run_child(run_dir, "copy-focused",
                      [sys.executable, copy_root / "tools" / "accept_fc3_bundle_json.py",
                       "--check", "focused", "--output-root", copy_out], copy_root)
    record["children"] = [child]
    runs = sorted(copy_out.glob("focused-*/run.json")) if copy_out.is_dir() else []
    copy_record = json.loads(runs[0].read_text()) if len(runs) == 1 else None
    record["copy_run"] = {"run_json": str(runs[0]) if len(runs) == 1 else None,
                          "run_json_sha256": sha256_file(runs[0]) if len(runs) == 1 else None}
    if child.get("exit") != 0 or not copy_record or copy_record.get("result") != "pass":
        reasons.append("copy_focused_failed")
    else:
        modules = copy_record.get("loaded_modules", {})
        record["copy_run"]["loaded_modules"] = modules
        record["copy_run"]["tests_run"] = copy_record["focused"]["tests_run"]
        if not modules.get("all_under_repo") or any(
                not Path(path).is_relative_to(copy_root) for key, path in modules.items() if key != "all_under_repo"):
            reasons.append("copy_loaded_modules_outside_copy")
        if copy_record.get("repo") != str(copy_root.resolve()):
            reasons.append("copy_repo_root_not_derived_from_file")
    # Original-prefix focused run in this process, compared with the copy.
    record["sentinels"] = install_sentinels(run_dir)
    facts = run_tests(run_dir, "original-focused", load_focused())
    record["focused"] = facts
    record["loaded_modules"] = loaded_modules()
    reasons.extend("original:" + reason for reason in focused_reasons(facts))
    if not record["loaded_modules"]["all_under_repo"]:
        reasons.append("module_loaded_outside_repo")
    if copy_record and copy_record["focused"]["tests_run"] != facts["tests_run"]:
        reasons.append("copy_and_original_test_counts_differ")
    return reasons


def require_exact_head(record):
    identity = record["source_identity"]
    if identity.get("clean") is not True:
        return ["tree_not_clean_exact_head_required"]
    return []


def check_credentials(run_dir, record):
    reasons = require_exact_head(record)
    scanner = Path(sys.executable).parent / "detect-secrets"
    record["scanner"] = {"path": str(scanner), "present": scanner.is_file()}
    if not scanner.is_file():
        return reasons + ["detect_secrets_missing_beside_interpreter"]
    scan = run_child(run_dir, "secret-scan", [scanner, "scan", "--all-files", "--exclude-files", "^.git/",
                                              "--no-verify"], REPO)
    report = run_dir / "secret-scan.json"
    if "stdout_path" in scan:
        os.link(scan["stdout_path"], report)
    checker = run_child(run_dir, "check-secret-report",
                        [sys.executable, REPO / "tools" / "check_secret_report.py", report], REPO)
    record["children"] = [scan, checker]
    try:
        data = json.loads(report.read_text())
        results = data["results"]
        if type(results) is not dict:
            raise ValueError("results not an object")
        record["raw_scanner_hits"] = {path: len(hits) for path, hits in sorted(results.items())}
    except (OSError, ValueError, KeyError, TypeError) as error:
        reasons.append("scan_report_unreadable:" + type(error).__name__)
    if scan.get("exit") != 0:
        reasons.append("detect_secrets_exit_%s" % scan.get("exit"))
    if checker.get("exit") != 0:
        reasons.append("check_secret_report_exit_%s" % checker.get("exit"))
    if "stdout_path" in checker:
        record["checker_summary"] = Path(checker["stdout_path"]).read_text().strip().splitlines()[-1:]
    return reasons


def check_public_tree(run_dir, record):
    reasons = require_exact_head(record)
    child = run_child(run_dir, "public-tree", [sys.executable, REPO / "tools" / "check_public_tree.py"], REPO)
    record["children"] = [child]
    if child.get("exit") != 0:
        reasons.append("public_tree_exit_%s" % child.get("exit"))
    return reasons


HANDLERS = {"focused": check_focused, "compatibility": check_compatibility, "negative": check_negative,
            "portability": check_portability, "credentials": check_credentials,
            "public-tree": check_public_tree}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", required=True, choices=CHECKS)
    parser.add_argument("--output-root", required=True)
    args = parser.parse_args(argv)
    run_dir = new_run_dir(args.output_root, args.check)
    record = {"schema": "fc3-bundle-json-acceptance/v1", "brief": BRIEF, "check": args.check,
              "argv": [sys.executable, *sys.argv], "cwd": os.getcwd(), "repo": str(REPO),
              "interpreter": sys.executable, "python": sys.version.split()[0],
              "run_dir": str(run_dir), "started_at": stamp(), "review_required": True,
              "full_discovery": "operator-run (not part of this runner)"}
    try:
        record["source_identity"] = source_identity()
        reasons = scope_reasons(record["source_identity"])
        reasons += HANDLERS[args.check](run_dir, record)
    except Exception as error:  # an unavailable or incomplete check fails closed
        reasons = ["check_error:%s:%s" % (type(error).__name__, str(error)[:500])]
    record["sentinel_hits"] = list(SENTINEL_HITS)
    if SENTINEL_HITS:
        reasons.append("sentinel_hits_present")
    record["finished_at"] = stamp()
    record["reasons"] = reasons
    record["result"] = "pass" if not reasons else "fail"
    record["exit"] = 0 if not reasons else 1
    write_new(run_dir / "run.json", json.dumps(record, indent=2, sort_keys=True, default=str) + "\n")
    print(json.dumps({"check": args.check, "result": record["result"], "run_dir": str(run_dir),
                      "reasons": reasons}, sort_keys=True))
    return record["exit"]


if __name__ == "__main__":
    raise SystemExit(main())
