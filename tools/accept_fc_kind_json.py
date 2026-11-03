"""FC-KIND-JSON acceptance runner (offline, synthetic, REVIEW REQUIRED only).

Usage:
  python3 tools/accept_fc_kind_json.py --check CHECK --output-root DIR

CHECK is one of: focused, compatibility, negative, portability, credentials,
public-tree. Every invocation creates a NEW run directory under DIR (outside the
repository tree), records argv/cwd/exit/raw stdout+stderr/hashes for every child
it starts, the candidate HEAD, porcelain status and source hashes, and exits 0
only when every required assertion of that check holds. Children are argument
arrays (no shell). Import search paths are set only inside this script, to its
own repository or to an isolated copy of it. A passing result is machine
validity only: never semantic/legal acceptance, activation or publication.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import traceback
import uuid

REPO = Path(__file__).resolve().parents[1]
CHECKS = ("focused", "compatibility", "negative", "portability", "credentials", "public-tree")
FOCUSED = "tests.test_fc_kind_json"
COMPATIBILITY = "tests.test_understanding_kind"
KIND_SOURCE = "campaign_tool/records/understanding/kind.py"
NEW_SOURCES = (KIND_SOURCE, "tests/test_fc_kind_json.py", "tools/accept_fc_kind_json.py")
HASHED_SOURCES = NEW_SOURCES + ("tests/test_understanding_kind.py", "tools/check_public_tree.py",
                                "tools/check_secret_report.py")
TIMEOUT = 1200
# Box-specific or interpreter-inline forms forbidden in the new sources. Split
# literals keep this list from matching itself.
FORBIDDEN_SOURCE = ("/ho" "me/", "agent" "-work", "venv" "-fullsuite", "PYTHON" "PATH",
                    "shell" "=True", '"-' 'c"', "'-" "c'", "os." "system", "ba" "sh", "<" "<EOF",
                    "LD_" "PRELOAD", ".her" "mes")


def sha256_bytes(raw):
    return hashlib.sha256(raw).hexdigest()


def sha256_file(path):
    path = Path(path)
    return sha256_bytes(path.read_bytes()) if path.is_file() else None


# --------------------------------------------------------------------------- children
def child_summary(result):
    return {
        "tests_run": result.testsRun,
        "executed": list(result.started),
        "failures": [t.id() for t, _ in result.failures],
        "errors": [t.id() for t, _ in result.errors],
        "skipped": [[t.id(), reason] for t, reason in result.skipped],
        "expected_failures": [t.id() for t, _ in result.expectedFailures],
        "unexpected_successes": [t.id() for t in result.unexpectedSuccesses],
        "subtest_failures": list(result.subtest_failures),
        "was_successful": result.wasSuccessful(),
    }


def clean(summary):
    return (summary["was_successful"] and summary["tests_run"] > 0
            and summary["tests_run"] == len(summary["executed"])
            and not (summary["failures"] or summary["errors"] or summary["skipped"]
                     or summary["expected_failures"] or summary["unexpected_successes"]))


def run_tests(names):
    import unittest

    class Recorder(unittest.TextTestResult):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.started, self.subtest_failures = [], []

        def startTest(self, test):
            self.started.append(test.id())
            super().startTest(test)

        def addSubTest(self, test, subtest, err):
            if err is not None:
                self.subtest_failures.append(subtest.id())
            super().addSubTest(test, subtest, err)

    suite = unittest.defaultTestLoader.loadTestsFromNames(list(names))
    runner = unittest.TextTestRunner(stream=sys.stderr, verbosity=2, resultclass=Recorder)
    return child_summary(runner.run(suite))


def child_suite(module):
    summary = run_tests([module])
    return {"suite": summary, "pass": clean(summary)}


def child_negative():
    from unittest.mock import patch
    from campaign_tool.records.understanding import kind

    original = {"_parse_float": kind._parse_float,
                "_reject_lone_surrogates": kind._reject_lone_surrogates}
    positive = FOCUSED + ".LoneSurrogateTests.test_valid_paired_surrogate_and_astral_characters_accepted"
    finite = FOCUSED + ".NumericTests.test_ordinary_finite_exponents_accepted"
    overflow = FOCUSED + ".NumericTests.test_positive_and_negative_overflow_exponents_rejected"
    surrogate = FOCUSED + ".LoneSurrogateTests.test_lone_surrogate_in_locator_key_and_value"
    record = {"before": run_tests([FOCUSED])}

    def detected(summary, target):
        return (target in summary["failures"] + summary["errors"]
                or any(s.startswith(target) for s in summary["subtest_failures"]))

    def no_cue_failed(summary, target):
        return any(s.startswith(target) and "cue='no_cue'" in s for s in summary["subtest_failures"])

    # Mutant A: the overflow value gate is bypassed (plain float accepts 1e309).
    with patch.object(kind, "_parse_float", float):
        record["mutant_overflow_gate"] = {
            "gated": run_tests([overflow]), "positives": run_tests([positive, finite])}
    # Mutant B: the lone-surrogate gate is bypassed.
    with patch.object(kind, "_reject_lone_surrogates", lambda value: None):
        record["mutant_surrogate_gate"] = {
            "gated": run_tests([surrogate]), "positives": run_tests([positive])}
    restored = all(getattr(kind, name) is value for name, value in original.items())
    record["restored_identity"] = restored
    record["after"] = run_tests([FOCUSED])
    a, b = record["mutant_overflow_gate"], record["mutant_surrogate_gate"]
    record["decisions"] = {
        "before_clean": clean(record["before"]),
        "overflow_gate_detected": detected(a["gated"], overflow) and no_cue_failed(a["gated"], overflow),
        "overflow_positives_green": clean(a["positives"]),
        "surrogate_gate_detected": detected(b["gated"], surrogate) and no_cue_failed(b["gated"], surrogate),
        "surrogate_positive_green": clean(b["positives"]),
        "restored_identity": restored,
        "after_clean": clean(record["after"]),
    }
    record["pass"] = all(record["decisions"].values())
    return record


def child_portability(module, sandbox):
    """Run the focused suite from an isolated copy with sentinels installed first."""
    import socket
    import subprocess as subprocess_module
    import tempfile

    sandbox = Path(sandbox)
    for name in ("home", "tmp", "cache", "config", "data"):
        (sandbox / name).mkdir(parents=True, exist_ok=True)
    # Redirect any import-time data defaults BEFORE application code is imported.
    os.environ.update({"HOME": str(sandbox / "home"), "TMPDIR": str(sandbox / "tmp"),
                       "XDG_CACHE_HOME": str(sandbox / "cache"),
                       "XDG_CONFIG_HOME": str(sandbox / "config"),
                       "XDG_DATA_HOME": str(sandbox / "data")})
    tempfile.tempdir = str(sandbox / "tmp")
    attempts = []

    def sentinel(label):
        def refuse(*_args, **_kwargs):
            attempts.append(label)
            raise RuntimeError("portability sentinel refused " + label)
        return refuse

    socket.socket.connect = sentinel("socket.connect")
    socket.socket.connect_ex = sentinel("socket.connect_ex")
    socket.create_connection = sentinel("socket.create_connection")
    socket.getaddrinfo = sentinel("socket.getaddrinfo")
    subprocess_module.Popen = sentinel("subprocess.Popen")
    for name in ("system", "fork", "forkpty", "execv", "execve", "execvp", "execvpe",
                 "spawnv", "spawnve", "posix_spawn", "posix_spawnp", "popen"):
        if hasattr(os, name):
            setattr(os, name, sentinel("os." + name))
    summary = run_tests([module])
    loaded = {}
    for name, mod in sorted(sys.modules.items()):
        path = getattr(mod, "__file__", None)
        if path and (name == "campaign_tool" or name.startswith(("campaign_tool.", "tests"))):
            loaded[name] = str(Path(path).resolve())
    inside = all(Path(p).is_relative_to(REPO) for p in loaded.values())
    required = {"campaign_tool.records.understanding.kind", module}
    return {"suite": summary, "repo": str(REPO), "loaded_modules": loaded,
            "required_modules_loaded": required <= set(loaded),
            "all_loaded_inside_copy": inside, "sentinel_attempts": attempts,
            "pass": clean(summary) and inside and required <= set(loaded) and not attempts}


def child_main(args):
    sys.path.insert(0, str(REPO))
    try:
        if args.child == "suite":
            record = child_suite(args.module)
        elif args.child == "negative":
            record = child_negative()
        elif args.child == "portability":
            record = child_portability(args.module, args.sandbox)
        else:
            raise ValueError("unknown child")
    except Exception:  # Explicit verdict: an exception is a failed child.
        record = {"pass": False, "exception": traceback.format_exc()}
    fd = os.open(args.result_json, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(json.dumps(record, indent=2, sort_keys=True) + "\n")
    return 0 if record.get("pass") is True else 1


# --------------------------------------------------------------------------- parent
class Run:
    def __init__(self, check, output_root):
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.dir = output_root / f"{check}-{stamp}-{uuid.uuid4().hex[:12]}"
        self.dir.mkdir(mode=0o700)  # Fresh child; never reuse earlier evidence.
        self.children = []

    def write_new(self, name, raw):
        fd = os.open(self.dir / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
        return sha256_bytes(raw)

    def child(self, label, argv, cwd, stdout_name=None):
        index = len(self.children)
        entry = {"label": label, "argv": [str(a) for a in argv], "cwd": str(cwd)}
        try:
            result = subprocess.run(entry["argv"], cwd=cwd, capture_output=True, timeout=TIMEOUT)
            entry["exit"], out, err = result.returncode, result.stdout, result.stderr
        except subprocess.TimeoutExpired as exc:
            entry["exit"], entry["timeout"] = None, True
            out, err = exc.stdout or b"", exc.stderr or b""
        entry["stdout_file"] = stdout_name or f"{index:02d}-{label}.stdout"
        entry["stderr_file"] = f"{index:02d}-{label}.stderr"
        entry["stdout_sha256"] = self.write_new(entry["stdout_file"], out)
        entry["stderr_sha256"] = self.write_new(entry["stderr_file"], err)
        self.children.append(entry)
        return entry, out, err


def identity(repo):
    def git(*args):
        result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, timeout=60)
        return {"exit": result.returncode, "stdout": result.stdout}
    return {"head": git("rev-parse", "HEAD"), "porcelain": git("status", "--porcelain"),
            "sources_sha256": {rel: sha256_file(repo / rel) for rel in HASHED_SOURCES}}


def runner_child(run, label, child, result_name, cwd=REPO, script=None, extra=()):
    script = script or Path(__file__).resolve()
    result_path = run.dir / result_name
    argv = [sys.executable, script, "--child", child, "--result-json", result_path, *extra]
    entry, _, _ = run.child(label, argv, cwd)
    try:
        record = json.loads(result_path.read_text())
    except (OSError, ValueError):
        record = {"pass": False, "missing_child_result": True}
    entry["result_json"] = result_name
    entry["result_sha256"] = sha256_file(result_path)
    return entry, record


def check_suite(run, module):
    entry, record = runner_child(run, "unittest-" + module, "suite", "suite-result.json",
                                 extra=("--module", module))
    suite = record.get("suite", {})
    return {"tests_run": suite.get("tests_run", 0), "executed": suite.get("executed", []),
            "skipped": suite.get("skipped"), "failures": suite.get("failures"),
            "errors": suite.get("errors"),
            "pass": entry["exit"] == 0 and record.get("pass") is True and bool(suite) and clean(suite)}


def check_negative(run):
    entry, record = runner_child(run, "negative-control", "negative", "negative-result.json")
    decisions = record.get("decisions", {})
    expected = {"before_clean", "overflow_gate_detected", "overflow_positives_green",
                "surrogate_gate_detected", "surrogate_positive_green", "restored_identity",
                "after_clean"}
    ok = (entry["exit"] == 0 and record.get("pass") is True and set(decisions) == expected
          and all(v is True for v in decisions.values())
          and clean(record["before"]) and clean(record["after"]))
    return {"decisions": decisions,
            "tests_run": record.get("after", {}).get("tests_run", 0), "pass": ok}


def tracked_files(repo):
    raw = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                         cwd=repo, capture_output=True, check=True, timeout=60).stdout
    return sorted({name for name in raw.decode("utf-8", "surrogateescape").split("\0") if name})


def check_portability(run):
    copy_root = run.dir / ("copy-" + uuid.uuid4().hex) / "repo"
    copy_root.mkdir(parents=True)
    manifest = []
    for rel in tracked_files(REPO):
        source, target = REPO / rel, copy_root / rel
        if source.is_symlink():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(os.readlink(source))
            manifest.append([rel, "symlink", os.readlink(source)])
        elif source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            manifest.append([rel, "file", sha256_file(target)])
    manifest_raw = (json.dumps(manifest, sort_keys=True) + "\n").encode()
    manifest_hash = run.write_new("copy-manifest.json", manifest_raw)
    copied_same = all(sha256_file(copy_root / rel) == sha256_file(REPO / rel) for rel in NEW_SOURCES)
    static = {}
    for rel in NEW_SOURCES:
        text = (REPO / rel).read_text(encoding="utf-8")
        static[rel] = [pattern for pattern in FORBIDDEN_SOURCE if pattern in text]
    # Interpreter-inline subprocess forms: -c/-e flags passed as argv elements.
    inline = re.compile(r"""["']-[ce]["']""")
    for rel in NEW_SOURCES:
        if inline.search((REPO / rel).read_text(encoding="utf-8")):
            static[rel].append("inline-interpreter-flag")
    entry, record = runner_child(
        run, "portability-copy", "portability", "portability-result.json", cwd=copy_root,
        script=copy_root / "tools/accept_fc_kind_json.py",
        extra=("--module", FOCUSED, "--sandbox", run.dir / "sandbox"))
    loaded = record.get("loaded_modules", {})
    different_prefix = copy_root.resolve() != REPO and not copy_root.resolve().is_relative_to(REPO)
    used_copy = bool(loaded) and all(Path(p).is_relative_to(copy_root.resolve()) for p in loaded.values())
    suite = record.get("suite", {})
    ok = (entry["exit"] == 0 and record.get("pass") is True and bool(suite) and clean(suite)
          and used_copy and different_prefix and copied_same
          and record.get("sentinel_attempts") == [] and record.get("required_modules_loaded") is True
          and not any(static.values()))
    return {"copy_root": str(copy_root), "copy_manifest_sha256": manifest_hash,
            "copied_files": len(manifest), "new_sources_identical_in_copy": copied_same,
            "different_absolute_prefix": different_prefix, "loaded_from_copy": used_copy,
            "loaded_modules": loaded, "static_findings": static,
            "sentinel_attempts": record.get("sentinel_attempts"),
            "tests_run": suite.get("tests_run", 0), "pass": ok}


def check_credentials(run):
    scanner = Path(sys.executable).parent / "detect-secrets"
    argv = [scanner, "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"]
    scan, out, _ = run.child("detect-secrets", argv, REPO, stdout_name="secret-scan.json")
    report_path = run.dir / "secret-scan.json"
    try:
        report = json.loads(out)
        hits = sum(len(v) for v in report["results"].values())
        report_ok = isinstance(report["results"], dict)
    except (ValueError, KeyError, TypeError, AttributeError):
        hits, report_ok = None, False
    checker, cout, _ = run.child("check-secret-report",
                                 [sys.executable, REPO / "tools/check_secret_report.py", report_path], REPO)
    match = re.search(rb"potential secrets: (\d+);", cout)
    potential = int(match[1]) if match else None
    ok = (scan["exit"] == 0 and report_ok and checker["exit"] == 0 and potential == 0)
    return {"scanner": str(scanner), "raw_report_hits": hits, "potential_secrets": potential,
            "report_valid": report_ok, "pass": ok}


def check_public_tree(run):
    entry, out, _ = run.child("check-public-tree",
                              [sys.executable, REPO / "tools/check_public_tree.py"], REPO)
    return {"pass": entry["exit"] == 0}


def parent_main(args):
    output_root = Path(args.output_root).absolute()
    if output_root.is_symlink() or output_root.resolve().is_relative_to(REPO):
        print(json.dumps({"error": "output_root_must_be_outside_repo_and_not_symlink"}))
        return 2
    output_root.mkdir(parents=True, exist_ok=True)
    run = Run(args.check, output_root)
    started = datetime.datetime.now(datetime.timezone.utc).isoformat()
    before = identity(REPO)
    try:
        verdict = {"focused": lambda: check_suite(run, FOCUSED),
                   "compatibility": lambda: check_suite(run, COMPATIBILITY),
                   "negative": lambda: check_negative(run),
                   "portability": lambda: check_portability(run),
                   "credentials": lambda: check_credentials(run),
                   "public-tree": lambda: check_public_tree(run)}[args.check]()
    except Exception:  # Incomplete check is a failed check.
        verdict = {"pass": False, "exception": traceback.format_exc()}
    after = identity(REPO)
    stable = before == after
    passed = verdict.get("pass") is True and stable and all(
        c.get("exit") is not None for c in run.children) and bool(run.children)
    record = {"check": args.check, "argv": [sys.executable, *sys.argv], "cwd": os.getcwd(),
              "repo": str(REPO), "run_dir": str(run.dir), "started_utc": started,
              "identity_before": before, "identity_after": after, "identity_stable": stable,
              "children": run.children, "verdict": verdict, "pass": passed,
              "exit": 0 if passed else 1, "review_required": True,
              "semantic_assessment": "NOT_ASSESSED"}
    raw = (json.dumps(record, indent=2, sort_keys=True) + "\n").encode()
    digest = run.write_new("result.json", raw)
    print(json.dumps({"check": args.check, "pass": passed, "run_dir": str(run.dir),
                      "result_sha256": digest, "tests_run": verdict.get("tests_run"),
                      "head": before["head"]["stdout"].strip(), "review_required": True},
                     sort_keys=True))
    return record["exit"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", choices=CHECKS)
    parser.add_argument("--output-root")
    parser.add_argument("--child", choices=("suite", "negative", "portability"), help=argparse.SUPPRESS)
    parser.add_argument("--result-json", help=argparse.SUPPRESS)
    parser.add_argument("--module", help=argparse.SUPPRESS)
    parser.add_argument("--sandbox", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.child:
        if not args.result_json:
            parser.error("--result-json is required for a child")
        return child_main(args)
    if not args.check or not args.output_root:
        parser.error("--check and --output-root are required")
    return parent_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
