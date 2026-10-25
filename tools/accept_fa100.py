"""FA100 acceptance launcher: exact positive-int OCR receipt page identities.

Exactly one --case per invocation. Each invocation creates a fresh, uniquely named child of
--output-root (outside this source tree; never reused, so prior author/reviewer evidence is
never overwritten) and writes a receipt with the actual argv/cwd/stdout/stderr/exit of every
command it ran, the interpreter, the source-manifest hash, the full git HEAD, the dirty-diff
hash, authored/fixture/test hashes and executed/skipped counts.

  behavior          tests.records.test_fa100.Behavior                              (A1)
  compatibility     four pinned tests.records.test_ocr.OCRTests selectors, unchanged (A2)
  negative-control  tests.records.test_fa100.GateControl; passes only when the
                    equality-only control accepted every true/1.0 alias (the exact-type
                    regression failed) and the corrected module refused them, with the
                    valid producer receipt passing under both                       (A3)
  credential-scan   SOURCE-MANIFEST runtime.credential_scan_python3 runs
                    runtime.detect_secrets with the repository arguments, then the
                    unchanged tools/check_secret_report.py on that report           (A4)
  public-tree       the unchanged tools/check_public_tree.py with .venv/bin/python3 (A5)

Test cases run in an ordinary file-launched child (this file, --child-spec). Before any
application import the child points tempfile/TMPDIR/HOME below its own run directory and
installs an audit hook that refuses network and child processes; after importing the OCR
module it replaces the bound default runner/which/probe with refusing sentinels. Each
boundary is self-tested in that fresh process and recorded. Only the named selectors are
loaded (no discovery). An offline guard for trusted synthetic tests, not a sandbox.

A pass is REVIEW REQUIRED: scoped evidence, not full-repository CI, semantic, legal, human,
stage or release acceptance.
"""
import sys

sys.dont_write_bytecode = True

import argparse  # noqa: E402 - bytecode writes are disabled before any further import
import datetime  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
from pathlib import Path  # noqa: E402
import re  # noqa: E402
import secrets  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
WRITE_ROOT = REPO.parent
MANIFEST = WRITE_ROOT / "SOURCE-MANIFEST.json"
SELF = "tools/accept_fa100.py"
FA100 = "tests.records.test_fa100"
PINNED = "tests.records.test_ocr.OCRTests"
SELECTORS = {
    "behavior": (FA100 + ".Behavior",),
    "compatibility": (PINNED + ".test_high_confidence_remains_unreviewed",
                      PINNED + ".test_fidelity_hold_atomic_and_sticky",
                      PINNED + ".test_load_page_receipt_rejects_tamper_and_other_versions",
                      PINNED + ".test_low_confidence_receipt_index_and_idempotent_rerun"),
    "negative-control": (FA100 + ".GateControl",),
}
REQUIRED_TESTS = {
    "behavior": {FA100 + ".Behavior." + name for name in (
        "test_valid_producer_receipts_validate_and_load_byte_identical",
        "test_identity_page_malformed_is_refused_by_public_loader",
        "test_identity_page_malformed_is_refused_by_validator_and_internal_loader",
        "test_locator_page_malformed_is_refused_at_every_entry_point",
        "test_eligibility_page_malformed_is_refused_when_eligibility_exists",
        "test_later_positive_page_passes_and_its_float_alias_is_refused",
        "test_restart_and_cache_replay_refuse_persisted_aliases",
        "test_valid_receipts_replay_from_cache_unchanged",
        "test_page_count_exact_integer_bound_is_unchanged",
        "test_authored_sources_contain_no_machine_paths")},
    "compatibility": set(SELECTORS["compatibility"]),
    "negative-control": {FA100 + ".GateControl.test_equality_only_control_accepts_aliases_and_corrected_module_refuses"},
}
CONTROL_FIELDS = ("identity", "locator", "eligibility")
CONTROL_ALIASES = ("bool_true", "float_same")
EXPECTED_CONTROL = {**{("control", f + ":" + k): "regression_failed_alias_accepted"
                       for f in CONTROL_FIELDS for k in CONTROL_ALIASES},
                    **{("staged", f + ":" + k): "refused" for f in CONTROL_FIELDS for k in CONTROL_ALIASES},
                    ("control", "valid_producer"): "passed", ("staged", "valid_producer"): "passed"}
CASES = ("behavior", "compatibility", "negative-control", "credential-scan", "public-tree")
HASHED = ("campaign_tool/records/extract/ocr.py", "tests/records/test_fa100.py", SELF,
          "tests/records/ocr_fixtures.py", "tests/records/test_ocr.py",
          "tools/check_secret_report.py", "tools/check_public_tree.py")
DROPPED_ENV = ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONINSPECT", "PYTHONSAFEPATH")
LABEL = "REVIEW REQUIRED - scoped offline acceptance, not full-repository CI or semantic acceptance"


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    return sha256_bytes(Path(path).read_bytes())


# ----------------------------------------------------------------------------- child

class Guard:
    """Refuses network and child-process audit events while active."""
    NETWORK = ("socket.connect", "socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr",
               "socket.bind", "socket.sendto", "socket.sendmsg", "urllib.Request", "http.client.connect",
               "ftplib.connect", "smtplib.connect", "imaplib.open", "poplib.connect")
    PROCESS = ("subprocess.Popen", "os.system", "os.posix_spawn", "os.fork", "os.forkpty", "pty.spawn")

    def __init__(self):
        self.active = False
        self.phase = "setup"
        self.refusals = []

    def __call__(self, event, args):
        if not self.active:
            return
        if event in self.NETWORK or event in self.PROCESS or event.startswith(("os.exec", "os.spawn")):
            self.refusals.append({"phase": self.phase, "event": event})
            raise PermissionError("FA100 offline guard refused " + event)


class Sentinel:
    def __init__(self, name):
        self.name = name

    def __call__(self, *args, **kwargs):
        raise PermissionError("FA100 sentinel refused bound default " + self.name)


def boundary_self_test(guard, ocr, root):
    """Each boundary must refuse before any effect, in this fresh file-launched process."""
    import socket
    guard.phase = "boundary-self-test"
    outcomes = {}

    def attempt(name, action):
        before = len(guard.refusals)
        try:
            action()
            outcomes[name] = {"outcome": "not_refused"}
        except PermissionError as error:
            outcomes[name] = {"outcome": "PermissionError", "guard_refusals": len(guard.refusals) - before,
                              "message": str(error)[:120]}
        except Exception as error:  # an effect was attempted: never acceptable
            outcomes[name] = {"outcome": type(error).__name__}

    def network():
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.connect(("127.0.0.1", 9))
    attempt("network", network)
    attempt("process", lambda: subprocess.Popen(["true"]))
    attempt("bound_default_dependency_status", lambda: ocr.dependency_status())
    attempt("bound_default_runner", lambda: ocr.extract_image_only_pages.__kwdefaults__["runner"]([]))
    outcomes["tempdir_below_run"] = {"outcome": Path(tempfile.gettempdir()).resolve().is_relative_to(root.resolve()),
                                     "tempdir": tempfile.gettempdir()}
    guard.phase = "tests"
    ok = (all(outcomes[k]["outcome"] == "PermissionError" for k in
              ("network", "process", "bound_default_dependency_status", "bound_default_runner"))
          and outcomes["network"]["guard_refusals"] == 1 and outcomes["process"]["guard_refusals"] == 1
          and outcomes["tempdir_below_run"]["outcome"] is True)
    return ok, outcomes


def iter_tests(suite):
    import unittest
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from iter_tests(item)
        else:
            yield item


def child_main(spec_path):
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    root = Path(spec["child_root"])
    tmp, home = root / "tmp", root / "home"
    tmp.mkdir(mode=0o700)
    home.mkdir(mode=0o700)
    tempfile.tempdir = str(tmp)
    for key in ("TMPDIR", "TEMP", "TMP"):
        os.environ[key] = str(tmp)
    os.environ["HOME"] = str(home)
    for key in ("XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
        os.environ[key] = str(home / key.lower())
    repo_loaded_at_start = sorted(n for n in sys.modules if n.split(".")[0] in ("campaign_tool", "tests"))
    guard = Guard()
    sys.addaudithook(guard)
    guard.active = True
    while str(REPO) in sys.path:
        sys.path.remove(str(REPO))
    sys.path.insert(0, str(REPO))
    import unittest
    from campaign_tool.records.extract import ocr
    ocr.extract_image_only_pages.__kwdefaults__["runner"] = Sentinel("extract_image_only_pages.runner")
    defaults = list(ocr.dependency_status.__defaults__)
    defaults[0], defaults[1] = Sentinel("dependency_status.which"), Sentinel("dependency_status.probe")
    ocr.dependency_status.__defaults__ = tuple(defaults)
    absolute_globals = sorted(k for k, v in vars(ocr).items() if not k.startswith("__")
                              and isinstance(v, (str, Path)) and os.path.isabs(str(v)))
    boundary_ok, boundary = boundary_self_test(guard, ocr, root)
    loader = unittest.TestLoader()
    suite = unittest.TestSuite(loader.loadTestsFromName(name) for name in spec["selectors"])
    selected = [test.id() for test in iter_tests(suite)]
    sys.stderr.write("selected %d tests from %s\n" % (len(selected), ", ".join(spec["selectors"])))
    result = unittest.TextTestRunner(stream=sys.stderr, verbosity=2).run(suite)
    guard.active = False
    loaded = {n: os.path.realpath(getattr(sys.modules[n], "__file__", "") or "")
              for n in sys.modules if n.split(".")[0] in ("campaign_tool", "tests")}
    outside = sorted(n for n, f in loaded.items() if f and not Path(f).is_relative_to(REPO))
    fa100 = sys.modules.get(FA100)
    counts = {"tests_run": result.testsRun, "selected": selected,
              "failures": [t.id() for t, _ in result.failures], "errors": [t.id() for t, _ in result.errors],
              "skipped": [[t.id(), reason] for t, reason in result.skipped],
              "expected_failures": [t.id() for t, _ in result.expectedFailures],
              "unexpected_successes": [t.id() for t in result.unexpectedSuccesses]}
    test_refusals = [r for r in guard.refusals if r["phase"] != "boundary-self-test"]
    facts = {"case": spec["case"], "executable": sys.executable, "python": sys.version, "cwd": os.getcwd(),
             "repo": str(REPO), "repo_modules_at_start": repo_loaded_at_start,
             "ocr_module_file": os.path.realpath(ocr.__file__), "ocr_absolute_path_globals": absolute_globals,
             "bound_defaults": {"extract_image_only_pages.runner": type(ocr.extract_image_only_pages.__kwdefaults__["runner"]).__name__,
                                "dependency_status.which": type(ocr.dependency_status.__defaults__[0]).__name__,
                                "dependency_status.probe": type(ocr.dependency_status.__defaults__[1]).__name__},
             "boundary_ok": boundary_ok, "boundary": boundary, "guard_refusals_in_tests": test_refusals,
             "modules_outside_repo": outside, "counts": counts, "tempdir": tempfile.gettempdir(),
             "control_outcomes": list(getattr(fa100, "CONTROL_OUTCOMES", [])) if fa100 else []}
    facts["child_ok"] = bool(
        boundary_ok and not repo_loaded_at_start and not absolute_globals and not outside and not test_refusals
        and result.testsRun > 0 and result.testsRun == len(selected) and result.wasSuccessful()
        and not counts["skipped"] and not counts["expected_failures"] and not counts["unexpected_successes"])
    Path(spec["result"]).write_text(json.dumps(facts, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    sys.stdout.write(json.dumps({"case": spec["case"], "child_ok": facts["child_ok"],
                                 "tests_run": result.testsRun}, sort_keys=True) + "\n")
    return 0 if facts["child_ok"] else 1


# ---------------------------------------------------------------------------- parent

def git(*args):
    done = subprocess.run(["git", *args], cwd=REPO, capture_output=True, check=False)
    if done.returncode != 0:
        raise RuntimeError("git " + args[0] + " failed")
    return done.stdout


def identity():
    status = git("status", "--porcelain", "--untracked-files=all").decode()
    diff = git("diff", "HEAD", "--binary")
    return {"head": git("rev-parse", "HEAD").decode().strip(), "dirty_files": status.splitlines(),
            "clean_tree": status == "", "dirty_diff_sha256": sha256_bytes(diff),
            "file_sha256": {p: sha256_file(REPO / p) for p in HASHED if (REPO / p).is_file()}}


def run_command(run_dir, label, argv, cwd, env=None, timeout=1800):
    """Run a literal argv (never a shell body); keep raw stdout/stderr files and their hashes."""
    out_path, err_path = run_dir / (label + "-stdout.txt"), run_dir / (label + "-stderr.txt")
    with out_path.open("xb") as out, err_path.open("xb") as err:
        done = subprocess.run(argv, cwd=cwd, env=env, stdout=out, stderr=err, check=False, timeout=timeout)
    return {"label": label, "argv": [str(a) for a in argv], "cwd": str(cwd), "exit": done.returncode,
            "stdout": str(out_path), "stdout_sha256": sha256_file(out_path), "stdout_bytes": out_path.stat().st_size,
            "stderr": str(err_path), "stderr_sha256": sha256_file(err_path), "stderr_bytes": err_path.stat().st_size}


def run_tests(case, run_dir, record):
    child_root = run_dir / "child"
    child_root.mkdir(mode=0o700)
    spec = {"case": case, "selectors": list(SELECTORS[case]), "child_root": str(child_root),
            "result": str(child_root / "child-result.json")}
    spec_path = run_dir / "child-spec.json"
    spec_path.write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in DROPPED_ENV}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["TMPDIR"] = str(child_root)
    child = run_command(run_dir, "child", [sys.executable, "-B", str(REPO / SELF), "--child-spec", str(spec_path)],
                        REPO, env=env)
    child["spec_sha256"] = sha256_file(spec_path)
    record["commands"].append(child)
    try:
        result = json.loads(Path(spec["result"]).read_text(encoding="utf-8"))
        record["child_result_sha256"] = sha256_file(spec["result"])
    except (OSError, ValueError) as error:
        record["log"].append("child result unreadable: " + type(error).__name__)
        return False
    record["child_result"] = result
    counts = result["counts"]
    record["counts"] = {"executed": counts["tests_run"], "skipped": len(counts["skipped"]),
                        "failures": len(counts["failures"]), "errors": len(counts["errors"]),
                        "failed_tests": counts["failures"] + counts["errors"]}
    missing = sorted(REQUIRED_TESTS[case] - set(counts["selected"]))
    record["missing_required_tests"] = missing
    passed = child["exit"] == 0 and result.get("child_ok") is True and not missing \
        and Path(result["ocr_module_file"]).is_relative_to(REPO)
    if case == "negative-control":
        outcomes = result.get("control_outcomes", [])
        observed = {(o["module"], o["case"]): o["outcome"] for o in outcomes}
        record["control"] = {"observed": {"%s|%s" % k: v for k, v in sorted(observed.items())},
                             "failing_regression_outputs": [o for o in outcomes if o["module"] == "control"
                                                            and o["case"] != "valid_producer"],
                             "restored_positive": [o for o in outcomes if o["case"] == "valid_producer"],
                             "matches_expected": len(outcomes) == len(EXPECTED_CONTROL) and observed == EXPECTED_CONTROL}
        passed = passed and record["control"]["matches_expected"] and all(
            o["detail"] for o in record["control"]["failing_regression_outputs"])
    return passed


def load_manifest(record):
    raw = MANIFEST.read_bytes()
    record["source_manifest"] = {"path": str(MANIFEST), "sha256": sha256_bytes(raw)}
    return json.loads(raw)


def run_credential_scan(run_dir, record):
    runtime = load_manifest(record).get("runtime", {})
    python, scanner = runtime.get("credential_scan_python3"), runtime.get("detect_secrets")
    record["runtime"] = {"credential_scan_python3": python, "detect_secrets": scanner}
    if not all(isinstance(p, str) and os.path.isabs(p) and os.path.isfile(p) for p in (python, scanner)):
        record["log"].append("staging failure: declared credential-scan runtime missing; nothing installed or switched")
        return False
    scan = run_command(run_dir, "detect-secrets", [python, scanner, "scan", "--all-files", "--exclude-files",
                                                   "^.git/", "--no-verify"], REPO)
    record["commands"].append(scan)
    report = Path(scan["stdout"])
    check = run_command(run_dir, "check-secret-report",
                        [python, str(REPO / "tools/check_secret_report.py"), str(report)], REPO, timeout=600)
    record["commands"].append(check)
    try:
        raw_hits = sum(len(v) for v in json.loads(report.read_bytes())["results"].values())
    except (ValueError, KeyError, TypeError, AttributeError):
        raw_hits = None
    match = re.search(rb"potential secrets: (\d+);", Path(check["stdout"]).read_bytes())
    potential = int(match.group(1)) if match else None
    record["credential"] = {"raw_hits": raw_hits, "potential_secrets": potential}
    return scan["exit"] == 0 and check["exit"] == 0 and raw_hits is not None and potential == 0


def run_public_tree(run_dir, record):
    load_manifest(record)
    command = run_command(run_dir, "check-public-tree",
                          [str(REPO / ".venv/bin/python3"), str(REPO / "tools/check_public_tree.py")], REPO)
    record["commands"].append(command)
    return command["exit"] == 0


def safe_output_root(raw):
    root = Path(os.path.abspath(raw))
    if root == REPO or root.is_relative_to(REPO) or REPO.is_relative_to(root):
        raise ValueError("output root must be outside (and not an ancestor of) the source tree")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if Path(os.path.realpath(root)) != root:
        raise ValueError("output root must not resolve through a symlink")
    return root


def parent_main(case, output_root):
    root = safe_output_root(output_root)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    run_dir = root / "-".join((case, stamp, str(os.getpid()), secrets.token_hex(4)))
    run_dir.mkdir(mode=0o700)
    record = {"label": LABEL, "case": case, "argv": [sys.executable, *sys.argv], "cwd": os.getcwd(),
              "interpreter": sys.executable, "interpreter_realpath": os.path.realpath(sys.executable),
              "python": sys.version, "run_dir": str(run_dir), "selectors": list(SELECTORS.get(case, ())),
              "identity_before": identity(), "commands": [], "log": []}
    try:
        if case == "credential-scan":
            passed = run_credential_scan(run_dir, record)
        elif case == "public-tree":
            passed = run_public_tree(run_dir, record)
        else:
            if MANIFEST.is_file():
                load_manifest(record)
            passed = run_tests(case, run_dir, record)
    except Exception as error:  # recorded and failed closed
        record["log"].append("runner fault: " + type(error).__name__ + ": " + str(error)[:300])
        passed = False
    record["identity_after"] = identity()
    unchanged = record["identity_after"] == record["identity_before"]
    record["source_unchanged_during_run"] = unchanged
    record["exit"] = 0 if passed and unchanged else 1
    summary = {"case": case, "exit": record["exit"], "run_dir": str(run_dir),
               "head": record["identity_after"]["head"], "dirty_diff_sha256": record["identity_after"]["dirty_diff_sha256"],
               "counts": record.get("counts"), "credential": record.get("credential"),
               "control_matches_expected": record.get("control", {}).get("matches_expected"),
               "commands": [{"label": c["label"], "exit": c["exit"]} for c in record["commands"]], "label": LABEL}
    out = (json.dumps(summary, sort_keys=True) + "\n").encode()
    (run_dir / "summary.json").write_bytes(out)
    record["summary_sha256"] = sha256_bytes(out)
    (run_dir / "receipt.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    sys.stdout.write(out.decode())
    return record["exit"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--case", choices=CASES)
    parser.add_argument("--output-root")
    parser.add_argument("--child-spec", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.child_spec:
        if args.case or args.output_root:
            parser.error("--child-spec is used alone")
        return child_main(args.child_spec)
    if not args.case or not args.output_root:
        parser.error("--case and --output-root are required")
    return parent_main(args.case, args.output_root)


if __name__ == "__main__":
    raise SystemExit(main())
