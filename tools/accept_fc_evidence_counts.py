"""FC-EVIDENCE-COUNTS acceptance runner.

Runs one named check, captures every child result into a freshly created run
directory below --output-root, and exits nonzero unless the check's exact
required conditions hold. Machine validity is REVIEW REQUIRED, never semantic
or legal acceptance, activation or publication authority.

Children are argument arrays (no shell). Import search paths are set only in
this script, to the source tree that contains this file.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve()
REPO = HERE.parents[1]
CHECKS = ("focused", "compatibility", "negative", "portability", "credentials", "public-tree")
FOCUSED_MODULE = "tests.records.test_fc_evidence_counts"
COMPAT_TARGETS = ("tests.records.test_understanding.EvidenceTests",
                  "tests.records.test_understanding.GroundedEvidenceTests")
SOURCE_FILES = ("campaign_tool/records/understanding/evidence.py",
                "tests/records/test_fc_evidence_counts.py",
                "tools/accept_fc_evidence_counts.py")
_P = FOCUSED_MODULE + "."
REQUIRED_FOCUSED = tuple(_P + name for name in (
    "ReceiptCountPositiveTests.test_two_unit_positive",
    "ReceiptCountPositiveTests.test_zero_unit_receipt_is_valid",
    "ReceiptCountPositiveTests.test_native_zero_unit_receipt_is_valid",
    "ReceiptCountPositiveTests.test_honest_partial_receipt_is_valid",
    "ReceiptCountPositiveTests.test_hand_built_partial_receipt_is_valid",
    "ReceiptCountPositiveTests.test_multi_original_multi_receipt_bundle",
    "ReceiptCountPositiveTests.test_multi_receipt_counts_are_per_pair",
    "ReceiptCountPositiveTests.test_native_normalizer_and_citation_round_trip",
    "ReceiptCountPositiveTests.test_validation_does_not_rewrite_receipts_or_ids",
    "ReceiptCountRejectionTests.test_wrong_normalized_count_with_recomputed_hash",
    "ReceiptCountRejectionTests.test_inflated_normalized_count_only_with_recomputed_hash",
    "ReceiptCountRejectionTests.test_understated_normalized_count_with_recomputed_hash",
    "ReceiptCountRejectionTests.test_negative_count",
    "ReceiptCountRejectionTests.test_bool_count",
    "ReceiptCountRejectionTests.test_float_count",
    "ReceiptCountRejectionTests.test_string_count",
    "ReceiptCountRejectionTests.test_null_count",
    "ReceiptCountRejectionTests.test_missing_counts",
    "ReceiptCountRejectionTests.test_declared_smaller_than_normalized",
    "ReceiptCountRejectionTests.test_missing_unit",
    "ReceiptCountRejectionTests.test_zero_declared_receipt_with_units_present",
    "ReceiptCountRejectionTests.test_unbound_extra_receipt_claiming_units",
    "ReceiptCountRejectionTests.test_duplicate_unit_check_is_preserved",
    "ReceiptCountRejectionTests.test_unit_hash_check_is_preserved",
    "ReceiptCountRejectionTests.test_outer_hash_check_is_preserved",
    "ReceiptCountRejectionTests.test_unit_without_receipt_is_preserved",
    "ReceiptCountRejectionTests.test_count_codes_are_stable",
))
_C = "tests.records.test_understanding."
REQUIRED_COMPAT = tuple(_C + name for name in (
    "EvidenceTests.test_receipt_normalizes_to_pinned_bundle",
    "EvidenceTests.test_tampered_bundle_is_rejected",
    "EvidenceTests.test_receipt_binding_mismatch_is_rejected",
    "EvidenceTests.test_duplicate_json_keys_are_rejected",
    "EvidenceTests.test_quote_citation_and_claim_round_trip",
    "GroundedEvidenceTests.test_json_pointer_support_is_exact",
))
# Count-mismatch fixtures (rehashed outer snapshot) that a disabled predicate must let through.
MUTANT_MUST_FAIL = tuple(_P + "ReceiptCountRejectionTests." + name for name in (
    "test_wrong_normalized_count_with_recomputed_hash",
    "test_inflated_normalized_count_only_with_recomputed_hash",
    "test_understated_normalized_count_with_recomputed_hash",
    "test_missing_unit",
))
# Valid fixtures that must stay green with the predicate disabled and restored.
MUTANT_MUST_PASS = tuple(_P + "ReceiptCountPositiveTests." + name for name in (
    "test_honest_partial_receipt_is_valid",
    "test_hand_built_partial_receipt_is_valid",
    "test_two_unit_positive",
))
# Box-specific path and inline-interpreter markers, assembled so this file does not match itself.
FORBIDDEN_MARKERS = tuple(m.encode() for m in (
    "/" + "home/", "/" + "Users/", "agent" + "-work", "venv" + "-fullsuite", "/" + "root/",
    "python3 -" + "c", "python -" + "c", "\"-" + "c\"", "'-" + "c'", "<" + "<EOF", "shell" + "=True",
    "os." + "system(", "PYTHON" + "PATH",
))


def sha_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def write_new(path, data):
    with open(path, "xb") as handle:
        handle.write(data if isinstance(data, bytes) else data.encode("utf-8"))


def write_json(path, value):
    write_new(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


class Run:
    def __init__(self, check, output_root):
        root = Path(output_root).absolute()
        root.mkdir(parents=True, exist_ok=True)
        self.dir = root / ("%s-%s-%d-%s" % (check, utc_now(), os.getpid(), secrets.token_hex(4)))
        self.dir.mkdir(exist_ok=False)
        self.children = []

    def child(self, name, argv, cwd, env=None, stdout_path=None):
        out_path = Path(stdout_path) if stdout_path else self.dir / (name + ".stdout")
        err_path = self.dir / (name + ".stderr")
        with open(out_path, "xb") as out, open(err_path, "xb") as err:
            try:
                proc = subprocess.run(argv, cwd=str(cwd), stdout=out, stderr=err, env=env, shell=False,
                                      stdin=subprocess.DEVNULL, timeout=1800)
                code, launch_error = proc.returncode, None
            except (OSError, subprocess.SubprocessError) as exc:
                code, launch_error = None, type(exc).__name__
        record = {"name": name, "argv": [str(a) for a in argv], "cwd": str(cwd), "exit": code,
                  "launch_error": launch_error, "stdout": str(out_path), "stdout_sha256": sha_file(out_path),
                  "stderr": str(err_path), "stderr_sha256": sha_file(err_path)}
        self.children.append(record)
        return record


def child_env(extra=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "LD_"))}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.update(extra or {})
    return env


def unittest_child(run, name, mode, targets, cwd=REPO, script=HERE, env=None):
    result_path = run.dir / (name + ".result.json")
    argv = [sys.executable, str(script), "--child", mode, "--child-result", str(result_path), *targets]
    record = run.child(name, argv, cwd, env=env or child_env())
    try:
        record["result"] = json.loads(result_path.read_text())
        record["result_sha256"] = sha_file(result_path)
    except (OSError, ValueError):
        record["result"] = None
    return record


def clean_suite(result, required):
    """Return a list of problems; empty means every required test ran and passed with nothing else wrong."""
    problems = []
    if not result:
        return ["missing_child_result"]
    if result["tests_run"] <= 0:
        problems.append("zero_tests")
    for kind in ("failures", "errors", "skipped", "expected_failures", "unexpected_successes"):
        if result[kind]:
            problems.append("%s:%s" % (kind, ",".join(sorted(result[kind]))))
    missing = sorted(set(required) - set(result["passed"]))
    if missing:
        problems.append("required_not_passed:" + ",".join(missing))
    if result["tests_run"] != len(result["passed"]) + sum(len(result[k]) for k in (
            "failures", "errors", "skipped", "expected_failures", "unexpected_successes")):
        problems.append("unaccounted_tests")
    return problems


def check_focused(run):
    rec = unittest_child(run, "focused", "plain", [FOCUSED_MODULE])
    problems = clean_suite(rec["result"], REQUIRED_FOCUSED)
    if rec["exit"] != 0:
        problems.append("child_exit:%s" % rec["exit"])
    return problems, {"focused": rec["result"]}


def check_compatibility(run):
    rec = unittest_child(run, "compatibility", "plain", list(COMPAT_TARGETS))
    problems = clean_suite(rec["result"], REQUIRED_COMPAT)
    if rec["exit"] != 0:
        problems.append("child_exit:%s" % rec["exit"])
    return problems, {"compatibility": rec["result"]}


def check_negative(run):
    evidence_before = sha_file(REPO / SOURCE_FILES[0])
    targets = list(MUTANT_MUST_FAIL + MUTANT_MUST_PASS)
    rec = unittest_child(run, "negative", "negative", targets)
    problems = []
    res = rec["result"]
    if rec["exit"] != 0:
        problems.append("child_exit:%s" % rec["exit"])
    if not res:
        return problems + ["missing_child_result"], {}
    if not res.get("predicate_present"):
        problems.append("predicate_absent")
    mutant, restored = res.get("mutant"), res.get("restored")
    if not mutant or not restored:
        return problems + ["missing_phase"], {"negative": res}
    not_detected = sorted(t for t in MUTANT_MUST_FAIL if t not in mutant["failures"])
    if not_detected:
        problems.append("mutant_not_detected:" + ",".join(not_detected))
    if mutant["errors"] or mutant["skipped"]:
        problems.append("mutant_errors_or_skips")
    partial_broken = sorted(t for t in MUTANT_MUST_PASS if t not in mutant["passed"])
    if partial_broken:
        problems.append("mutant_valid_fixture_not_green:" + ",".join(partial_broken))
    problems += ["restored:" + p for p in clean_suite(restored, targets)]
    if not res.get("restored_is_original"):
        problems.append("predicate_not_restored")
    if sha_file(REPO / SOURCE_FILES[0]) != evidence_before:
        problems.append("production_source_changed")
    return problems, {"negative": res}


def copy_ignore(directory, names):
    return [n for n in names if n in (".git", "__pycache__") or n.endswith(".pyc")]


def check_portability(run):
    problems = []
    inspected = {}
    for rel in SOURCE_FILES[1:]:
        data = (REPO / rel).read_bytes()
        hits = sorted(m.decode() for m in FORBIDDEN_MARKERS if m in data)
        inspected[rel] = hits
        if hits:
            problems.append("forbidden_marker:%s:%s" % (rel, ",".join(hits)))
    with tempfile.TemporaryDirectory(prefix="portable-", dir=str(run.dir)) as tmp:
        copy_root = Path(tmp) / ("relocated-" + secrets.token_hex(4)) / "candidate"
        shutil.copytree(REPO, copy_root, ignore=copy_ignore, symlinks=True)
        home = Path(tmp) / "home"
        home.mkdir()
        copied = {rel: sha_file(copy_root / rel) for rel in SOURCE_FILES}
        if copied != {rel: sha_file(REPO / rel) for rel in SOURCE_FILES}:
            problems.append("copy_mismatch")
        env = child_env({"HOME": str(home), "TMPDIR": str(home), "XDG_DATA_HOME": str(home),
                         "XDG_CONFIG_HOME": str(home), "XDG_CACHE_HOME": str(home)})
        rec = unittest_child(run, "portable-focused", "isolated", [FOCUSED_MODULE], cwd=copy_root,
                             script=copy_root / "tools" / HERE.name, env=env)
        res = rec["result"]
        problems += clean_suite(res, REQUIRED_FOCUSED)
        if rec["exit"] != 0:
            problems.append("child_exit:%s" % rec["exit"])
        if res:
            if Path(res["source_root"]) != copy_root.resolve():
                problems.append("child_used_other_root")
            modules = res.get("modules", {})
            if not modules or "campaign_tool.records.understanding.evidence" not in modules:
                problems.append("module_record_missing")
            outside = sorted(n for n, p in modules.items() if not Path(p).is_relative_to(copy_root.resolve()))
            if outside:
                problems.append("module_outside_copy:" + ",".join(outside))
            if res.get("sentinel_hits"):
                problems.append("sentinel_hits:" + ",".join(res["sentinel_hits"]))
        detail = {"copy_root": str(copy_root), "copied_hashes": copied, "inspected": inspected,
                  "portable": res}
    return problems, detail


def check_credentials(run):
    scanner = Path(sys.executable).parent / "detect-secrets"
    report = run.dir / "secret-scan.json"
    problems = []
    if not scanner.is_file():
        return ["scanner_unavailable"], {"scanner": str(scanner)}
    scan = run.child("detect-secrets", [str(scanner), "scan", "--all-files", "--exclude-files", "^.git/",
                                        "--no-verify"], REPO, env=child_env(), stdout_path=report)
    if scan["exit"] != 0:
        problems.append("scan_exit:%s" % scan["exit"])
    try:
        parsed = json.loads(report.read_text())
        if not isinstance(parsed.get("results"), dict):
            problems.append("report_malformed")
        version = parsed.get("version")
    except (OSError, ValueError, AttributeError):
        problems.append("report_malformed")
        version = None
    checker = run.child("check-secret-report", [sys.executable, str(REPO / "tools" / "check_secret_report.py"),
                                                str(report)], REPO, env=child_env())
    if checker["exit"] != 0:
        problems.append("checker_exit:%s" % checker["exit"])
    text = Path(checker["stdout"]).read_text()
    if "potential secrets: 0;" not in text:
        problems.append("potential_secrets_nonzero_or_unreported")
    return problems, {"scanner": str(scanner), "scanner_report_version": version, "report": str(report),
                      "report_sha256": sha_file(report), "checker_summary": text.strip().splitlines()[-1:]}


def check_public_tree(run):
    rec = run.child("check-public-tree", [sys.executable, str(REPO / "tools" / "check_public_tree.py")], REPO,
                    env=child_env())
    problems = [] if rec["exit"] == 0 else ["public_tree_exit:%s" % rec["exit"]]
    return problems, {}


def git(*args):
    try:
        proc = subprocess.run(["git", *args], cwd=str(REPO), capture_output=True, text=True, timeout=60)
        return proc.stdout.strip() if proc.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", required=True, choices=CHECKS)
    parser.add_argument("--output-root", required=True)
    args = parser.parse_args(argv)
    run = Run(args.check, args.output_root)
    started = utc_now()
    handler = {"focused": check_focused, "compatibility": check_compatibility, "negative": check_negative,
               "portability": check_portability, "credentials": check_credentials,
               "public-tree": check_public_tree}[args.check]
    try:
        problems, detail = handler(run)
    except Exception as exc:  # incomplete check is a failure, never a pass
        problems, detail = ["runner_exception:%s" % type(exc).__name__], {}
    status = git("status", "--porcelain")
    summary = {
        "check": args.check, "verdict": "PASS" if not problems else "FAIL", "problems": problems,
        "authority": "REVIEW REQUIRED: machine validity only; not semantic/legal acceptance or publication",
        "argv": [sys.executable, *sys.argv], "cwd": os.getcwd(), "interpreter": sys.executable,
        "python_version": sys.version, "repo": str(REPO), "head": git("rev-parse", "HEAD"),
        "worktree_clean": status == "", "worktree_status": status, "started": started, "finished": utc_now(),
        "source_hashes": {rel: sha_file(REPO / rel) for rel in SOURCE_FILES if (REPO / rel).is_file()},
        "children": run.children, "detail": detail,
    }
    for rec in run.children:
        res = rec.get("result")
        if isinstance(res, dict) and "tests_run" in res:
            summary.setdefault("tests_run", {})[rec["name"]] = res["tests_run"]
    write_json(run.dir / "summary.json", summary)
    print("%s %s run_dir=%s problems=%s" % (args.check, summary["verdict"], run.dir, problems))
    return 0 if not problems else 1


# ---------------------------------------------------------------- child side


class RecordingResult(unittest.TextTestResult):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.passed = []

    def addSuccess(self, test):
        super().addSuccess(test)
        self.passed.append(test.id())


def run_suite(targets):
    suite = unittest.defaultTestLoader.loadTestsFromNames(targets)
    runner = unittest.TextTestRunner(stream=sys.stderr, verbosity=2, resultclass=RecordingResult)
    res = runner.run(suite)
    return {"tests_run": res.testsRun, "passed": sorted(res.passed),
            "failures": sorted(t.id() for t, _ in res.failures), "errors": sorted(t.id() for t, _ in res.errors),
            "skipped": sorted(t.id() for t, _ in res.skipped),
            "expected_failures": sorted(t.id() for t, _ in res.expectedFailures),
            "unexpected_successes": sorted(t.id() for t in res.unexpectedSuccesses)}


SENTINEL_HITS = []


def install_sentinels():
    import socket

    def blocked(label):
        def refuse(*a, **k):
            SENTINEL_HITS.append(label)
            raise PermissionError("sentinel:" + label)
        return refuse

    socket.socket.connect = blocked("socket.connect")
    socket.socket.connect_ex = blocked("socket.connect_ex")
    socket.create_connection = blocked("socket.create_connection")
    socket.getaddrinfo = blocked("socket.getaddrinfo")
    subprocess.Popen.__init__ = blocked("subprocess.Popen")
    os.system = blocked("os.system")
    for name in ("execv", "execve", "execvp", "execvpe", "fork", "forkpty", "posix_spawn", "posix_spawnp"):
        if hasattr(os, name):
            setattr(os, name, blocked("os." + name))


def child_main(mode, result_path, targets):
    sys.dont_write_bytecode = True
    root = str(REPO)
    sys.path[:] = [root] + [p for p in sys.path if p not in ("", ".", root) and Path(p).resolve() != REPO]
    if mode == "isolated":
        install_sentinels()
    out = {"mode": mode, "source_root": root, "targets": targets}
    if mode == "negative":
        from campaign_tool.records.understanding import evidence
        original = getattr(evidence, "_check_receipt_counts", None)
        out["predicate_present"] = callable(original)
        if callable(original):
            evidence._check_receipt_counts = lambda *a, **k: None  # in-memory mutant only
            try:
                out["mutant"] = run_suite(targets)
            finally:
                evidence._check_receipt_counts = original
            out["restored_is_original"] = evidence._check_receipt_counts is original
            out["restored"] = run_suite(targets)
    else:
        out.update(run_suite(targets))
    out["modules"] = {name: str(Path(mod.__file__).resolve()) for name, mod in sorted(sys.modules.items())
                      if name.split(".")[0] in ("campaign_tool", "tests") and getattr(mod, "__file__", None)}
    out["sentinel_hits"] = sorted(set(SENTINEL_HITS))
    write_json(result_path, out)
    phases = [out] if mode != "negative" else [out.get("restored") or {}]
    ok = all(p.get("tests_run", 0) > 0 and not p.get("failures") and not p.get("errors") for p in phases)
    return 0 if ok and (mode != "negative" or out.get("predicate_present")) else 1


if __name__ == "__main__":
    if len(sys.argv) >= 5 and sys.argv[1] == "--child" and sys.argv[3] == "--child-result":
        if sys.argv[2] not in ("plain", "negative", "isolated"):
            raise SystemExit(2)
        raise SystemExit(child_main(sys.argv[2], sys.argv[4], sys.argv[5:]))
    raise SystemExit(main())
