"""FA101 acceptance launcher: malformed OCR page selectors are refused before source bytes.

Exactly one --case per invocation. Each invocation creates a fresh, uniquely named child
directory beneath --output-root (outside this source tree; never reused or overwritten) and
writes a receipt with the actual interpreter, source manifest hash, full git HEAD, dirty diff
hash, authored/referenced file hashes, every child argv/cwd/exit, raw stdout/stderr and the
executed/skipped counts.

  behavior          tests.records.test_fa101.Behavior                              (A1)
  compatibility     the three pinned tests.records.test_ocr.OCRTests selectors      (A2)
  negative-control  tests.records.test_fa101.GateControl; passes only when the in-memory
                    copy with ONLY the preflight disabled lets malformed selectors reach the
                    source/PDF seams (or raise a raw TypeError) and valid selection passes in
                    both the control and the restored staged producer               (A3)
  credential-scan   detect-secrets from SOURCE-MANIFEST.json runtime, then the unchanged
                    tools/check_secret_report.py with the same interpreter          (A4)
  public-tree       the unchanged tools/check_public_tree.py with the repo .venv     (A5)

Test cases run in an ordinary file-launched child (this file, --child-spec). Before any
application import the child disables bytecode writes, points tempfile/TMPDIR/HOME/XDG below
its own run directory and installs an audit-hook guard refusing network access and child
processes; the OCR module's bound default runners are then replaced by refusing sentinels.
The child self-tests each boundary before loading tests and only the named selectors are
loaded (no discovery). An empty selection, a skip or a missing required test fails.

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

SOURCE = Path(__file__).resolve().parents[1]
WRITE_ROOT = SOURCE.parent
MANIFEST = WRITE_ROOT / "SOURCE-MANIFEST.json"
SELF = "tools/accept_fa101.py"
FA101 = "tests.records.test_fa101"
OCR_TESTS = "tests.records.test_ocr.OCRTests"
BEHAVIOR_TESTS = (
    "test_malformed_pages_are_refused_before_tools_source_and_pdf",
    "test_malformed_fidelity_holds_are_refused_before_tools_source_and_pdf",
    "test_list_and_tuple_pages_select_the_same_receipts",
    "test_none_pages_selects_every_page_in_order",
    "test_empty_selections_remain_valid_and_still_verify_the_source",
    "test_duplicate_holds_are_idempotent_membership",
    "test_page_count_bound_is_checked_after_source_verification",
    "test_selectors_are_snapshotted_before_the_source_read",
    "test_source_mismatch_stays_fail_closed",
    "test_existing_fidelity_hold_stays_sticky",
    "test_producer_and_cache_positive_with_fake_ocr",
    "test_in_tree_callers_use_the_documented_selector_shape",
)
COMPATIBILITY_TESTS = (
    OCR_TESTS + ".test_high_confidence_remains_unreviewed",
    OCR_TESTS + ".test_fidelity_hold_atomic_and_sticky",
    OCR_TESTS + ".test_low_confidence_receipt_index_and_idempotent_rerun",
)
SELECTORS = {
    "behavior": (FA101 + ".Behavior",),
    "compatibility": COMPATIBILITY_TESTS,
    "negative-control": (FA101 + ".GateControl",),
}
REQUIRED_TESTS = {
    "behavior": {FA101 + ".Behavior." + name for name in BEHAVIOR_TESTS},
    "compatibility": set(COMPATIBILITY_TESTS),
    "negative-control": {FA101 + ".GateControl.test_disabled_preflight_is_detected_and_restored_positive_passes"},
}
CASES = ("behavior", "compatibility", "negative-control", "credential-scan", "public-tree")
CONTROL_PROBES = ("nested_list_pages", "nested_dict_holds", "string_pages", "set_holds")
SELECTOR_ERROR = "invalid or duplicate page selector"
HASHED = ("campaign_tool/records/extract/ocr.py", "tests/records/test_fa101.py", SELF,
          "tests/records/test_ocr.py", "tools/check_secret_report.py", "tools/check_public_tree.py")
LABEL = "REVIEW REQUIRED - scoped offline acceptance, not full-repository CI or semantic acceptance"
DROPPED_ENV = ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONINSPECT", "PYTHONSAFEPATH")


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    return sha256_bytes(Path(path).read_bytes())


def repo_modules():
    return sorted(n for n in sys.modules if n.split(".")[0] in ("campaign_tool", "tests"))


# ----------------------------------------------------------------------------- child

class Guard:
    """Audit hook for trusted synthetic tests: no network, no child processes (not a sandbox)."""
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
            raise PermissionError("FA101 offline guard refused " + event)


class Sentinel:
    def __init__(self, name):
        self.name = name

    def __call__(self, *args, **kwargs):
        raise PermissionError("FA101 sentinel refused bound default " + self.name)


def self_test(guard, ocr):
    """Each boundary must refuse in this fresh file-launched process before tests load."""
    import socket
    guard.phase = "boundary-self-test"
    outcomes = {}

    def attempt(name, action):
        before = len(guard.refusals)
        try:
            action()
            outcomes[name] = {"outcome": "not_refused"}
        except PermissionError as error:
            outcomes[name] = {"outcome": "PermissionError", "hook_refusals": len(guard.refusals) - before,
                              "message": str(error)[:120]}
        except Exception as error:  # an effect was attempted: never acceptable
            outcomes[name] = {"outcome": type(error).__name__}

    def network():
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.connect(("127.0.0.1", 9))
    attempt("network", network)
    attempt("process", lambda: subprocess.Popen([sys.executable, "--version"]))
    attempt("ocr_runner_default", lambda: ocr.extract_image_only_pages.__kwdefaults__["runner"](["probe"]))
    attempt("dependency_which_default", lambda: ocr.dependency_status())
    guard.phase = "tests"
    ok = (outcomes["network"].get("hook_refusals") == 1 and outcomes["process"].get("hook_refusals") == 1
          and outcomes["ocr_runner_default"].get("hook_refusals") == 0
          and outcomes["dependency_which_default"].get("hook_refusals") == 0
          and all(o["outcome"] == "PermissionError" for o in outcomes.values()))
    return outcomes, ok


def host_path_globals(module):
    """Module globals that name an absolute host path (none are expected in the OCR producer)."""
    found = {}
    for name, value in vars(module).items():
        if isinstance(value, (str, Path)) and not name.startswith("__") and os.path.isabs(str(value)):
            found[name] = str(value)
    return found


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
    facts = {"case": spec["case"], "executable": sys.executable, "python": sys.version, "cwd": os.getcwd(),
             "source_root": str(SOURCE), "repo_modules_at_start": repo_modules()}
    guard = Guard()
    sys.addaudithook(guard)
    guard.active = True
    facts["guard_installed_before_app_import"] = repo_modules() == []
    while str(SOURCE) in sys.path:
        sys.path.remove(str(SOURCE))
    sys.path.insert(0, str(SOURCE))
    import unittest
    from campaign_tool.records.extract import ocr
    ocr.extract_image_only_pages.__kwdefaults__["runner"] = Sentinel("extract_image_only_pages.runner")
    defaults = list(ocr.dependency_status.__defaults__)
    defaults[0], defaults[1] = Sentinel("dependency_status.which"), Sentinel("dependency_status.probe")
    ocr.dependency_status.__defaults__ = tuple(defaults)
    facts["bound_defaults"] = {
        "extract_image_only_pages.runner": type(ocr.extract_image_only_pages.__kwdefaults__["runner"]).__name__,
        "dependency_status.which": type(ocr.dependency_status.__defaults__[0]).__name__,
        "dependency_status.probe": type(ocr.dependency_status.__defaults__[1]).__name__}
    facts["ocr_host_path_globals"] = host_path_globals(ocr)
    facts["boundary_self_test"], boundary_ok = self_test(guard, ocr)
    loader = unittest.TestLoader()
    suite = unittest.TestSuite(loader.loadTestsFromName(name) for name in spec["selectors"])
    selected = [test.id() for test in iter_tests(suite)]
    sys.stderr.write("selected %d tests from %s\n" % (len(selected), ", ".join(spec["selectors"])))
    result = unittest.TextTestRunner(stream=sys.stderr, verbosity=2).run(suite)
    guard.active = False
    counts = {"tests_run": result.testsRun, "selected": selected,
              "failures": [t.id() for t, _ in result.failures], "errors": [t.id() for t, _ in result.errors],
              "skipped": [[t.id(), reason] for t, reason in result.skipped],
              "expected_failures": [t.id() for t, _ in result.expectedFailures],
              "unexpected_successes": [t.id() for t in result.unexpectedSuccesses]}
    loaded = {n: os.path.realpath(getattr(sys.modules[n], "__file__", None) or "") or None for n in repo_modules()}
    outside = sorted(n for n, f in loaded.items() if f and not Path(f).is_relative_to(SOURCE))
    fa101 = sys.modules.get(FA101)
    test_refusals = [r for r in guard.refusals if r["phase"] != "boundary-self-test"]
    facts.update({
        "counts": counts, "modules_outside_source": outside, "ocr_module_file": os.path.realpath(ocr.__file__),
        "dont_write_bytecode": sys.dont_write_bytecode, "tempdir": tempfile.gettempdir(), "home": os.environ["HOME"],
        "guard_refusals": guard.refusals,
        "control_outcomes": list(getattr(fa101, "CONTROL_OUTCOMES", [])) if fa101 else []})
    required = set(spec["required"])
    facts["missing_required"] = sorted(required - set(selected))
    facts["child_ok"] = bool(
        result.testsRun > 0 and result.testsRun == len(selected) and not facts["missing_required"]
        and not counts["failures"] and not counts["errors"] and not counts["skipped"]
        and not counts["expected_failures"] and not counts["unexpected_successes"] and not outside
        and boundary_ok and facts["guard_installed_before_app_import"] and not facts["ocr_host_path_globals"]
        and not test_refusals and Path(facts["tempdir"]).is_relative_to(root)
        and Path(facts["ocr_module_file"]).is_relative_to(SOURCE))
    facts["status"] = "PASS" if facts["child_ok"] else "FAIL"
    Path(spec["result"]).write_text(json.dumps(facts, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    sys.stdout.write(json.dumps({"case": spec["case"], "status": facts["status"], "tests_run": result.testsRun},
                                sort_keys=True) + "\n")
    return 0 if facts["child_ok"] else 1


# ---------------------------------------------------------------------------- parent

def git(*args):
    done = subprocess.run(["git", *args], cwd=SOURCE, capture_output=True, check=False)
    if done.returncode != 0:
        raise RuntimeError("git " + args[0] + " failed")
    return done.stdout


def identity():
    status = git("status", "--porcelain", "--untracked-files=all").decode()
    diff = git("diff", "HEAD", "--binary")
    return {"head": git("rev-parse", "HEAD").decode().strip(), "dirty_files": status.splitlines(),
            "dirty_diff_sha256": sha256_bytes(diff), "dirty_diff_bytes": len(diff),
            "file_sha256": {p: sha256_file(SOURCE / p) for p in HASHED if (SOURCE / p).is_file()}}


def capture(run_dir, label, argv, cwd, timeout=1500, env=None):
    """Run one literal argv (no shell); keep raw stdout/stderr files and their hashes."""
    out_path, err_path = run_dir / (label + "-stdout.txt"), run_dir / (label + "-stderr.txt")
    with out_path.open("xb") as out, err_path.open("xb") as err:
        done = subprocess.run(argv, cwd=cwd, env=env, stdout=out, stderr=err, check=False, timeout=timeout)
    return {"label": label, "argv": [str(a) for a in argv], "cwd": str(cwd), "exit": done.returncode,
            "stdout": str(out_path), "stdout_sha256": sha256_file(out_path), "stdout_bytes": out_path.stat().st_size,
            "stderr": str(err_path), "stderr_sha256": sha256_file(err_path), "stderr_bytes": err_path.stat().st_size}


def run_tests(case, run_dir):
    child_root = run_dir / "child"
    child_root.mkdir(mode=0o700)
    spec = {"case": case, "selectors": list(SELECTORS[case]), "required": sorted(REQUIRED_TESTS[case]),
            "child_root": str(child_root), "result": str(child_root / "child-result.json")}
    spec_path = run_dir / "child-spec.json"
    spec_path.write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in DROPPED_ENV}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    record = capture(run_dir, "child", [sys.executable, "-B", str(SOURCE / SELF), "--child-spec", str(spec_path)],
                     SOURCE, env=env)
    record["spec_sha256"] = sha256_file(spec_path)
    try:
        result = json.loads(Path(spec["result"]).read_text(encoding="utf-8"))
        record["result_sha256"] = sha256_file(spec["result"])
        record["result"] = result
    except (OSError, ValueError) as error:
        record["result_error"] = type(error).__name__ + ": " + str(error)[:200]
        return record, False
    passed = record["exit"] == 0 and result.get("status") == "PASS" and result.get("child_ok") is True \
        and REQUIRED_TESTS[case] <= set(result["counts"]["selected"])
    if case == "compatibility":
        passed = passed and set(result["counts"]["selected"]) == set(COMPATIBILITY_TESTS)
    if case == "negative-control":
        record["control_verdict"] = control_verdict(result.get("control_outcomes", []))
        passed = passed and record["control_verdict"]["detected"] and record["control_verdict"]["restored_positive"]
    return record, passed


def control_verdict(outcomes):
    observed = {(o["module"], o["case"]): o for o in outcomes}
    expected_keys = {(m, c) for m in ("control", "staged") for c in CONTROL_PROBES + ("valid_selection",)}
    if len(outcomes) != len(expected_keys) or set(observed) != expected_keys:
        return {"detected": False, "restored_positive": False, "reason": "unexpected control outcome set"}
    detected = all(
        (observed[("control", c)]["outcome"] == "TypeError" or "_read" in observed[("control", c)]["seams"])
        and (observed[("staged", c)]["outcome"], observed[("staged", c)]["detail"], observed[("staged", c)]["seams"])
        == ("ValueError", SELECTOR_ERROR, []) for c in CONTROL_PROBES)
    restored = all((observed[(m, "valid_selection")]["outcome"], observed[(m, "valid_selection")]["detail"])
                   == ("returned", [1, 2]) for m in ("control", "staged"))
    return {"detected": detected, "restored_positive": restored,
            "control_failures": {c: [observed[("control", c)]["outcome"], observed[("control", c)]["seams"]]
                                 for c in CONTROL_PROBES}}


def run_credential_scan(run_dir, record):
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    runtime = manifest.get("runtime", {})
    python, scanner = runtime.get("credential_scan_python3"), runtime.get("detect_secrets")
    record["credential_runtime"] = {"credential_scan_python3": python, "detect_secrets": scanner}
    if not (isinstance(python, str) and os.path.isfile(python) and isinstance(scanner, str)
            and os.path.isfile(scanner)):
        record["stop_reason"] = "staging_failure: declared credential-scan runtime missing; nothing installed"
        return False
    scan = capture(run_dir, "detect-secrets", [python, scanner, "scan", "--all-files", "--exclude-files", "^.git/",
                                               "--no-verify"], SOURCE)
    report = Path(scan["stdout"])
    try:
        raw_hits = sum(len(v) for v in json.loads(report.read_bytes())["results"].values())
    except (ValueError, KeyError, TypeError, AttributeError):
        raw_hits = None
    scan["raw_hits"] = raw_hits
    check = capture(run_dir, "check-secret-report", [python, str(SOURCE / "tools/check_secret_report.py"), str(report)],
                    SOURCE, timeout=600)
    match = re.search(rb"potential secrets: (\d+);", Path(check["stdout"]).read_bytes())
    check["potential_secrets"] = int(match.group(1)) if match else None
    record["commands"] = [scan, check]
    return scan["exit"] == 0 and raw_hits is not None and check["exit"] == 0 and check["potential_secrets"] == 0


def run_public_tree(run_dir, record):
    check = capture(run_dir, "check-public-tree", [str(SOURCE / ".venv/bin/python3"),
                                                   str(SOURCE / "tools/check_public_tree.py")], SOURCE, timeout=600)
    record["commands"] = [check]
    return check["exit"] == 0


def parent_main(case, output_root):
    root = Path(os.path.abspath(output_root))
    if root == SOURCE or root.is_relative_to(SOURCE) or SOURCE.is_relative_to(root):
        raise SystemExit("output root must be outside (and not an ancestor of) the source tree")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    run_dir = root / "-".join((case, stamp, str(os.getpid()), secrets.token_hex(4)))
    run_dir.mkdir(mode=0o700)  # fails rather than reuse: prior evidence is never overwritten
    start = identity()
    record = {"label": LABEL, "case": case, "argv": [sys.executable, *sys.argv], "cwd": os.getcwd(),
              "interpreter": sys.executable, "python": sys.version, "run_dir": str(run_dir),
              "source_manifest": str(MANIFEST),
              "source_manifest_sha256": sha256_file(MANIFEST) if MANIFEST.is_file() else None,
              "identity_before": start, "selectors": list(SELECTORS.get(case, ()))}
    try:
        if case == "credential-scan":
            passed = run_credential_scan(run_dir, record)
        elif case == "public-tree":
            passed = run_public_tree(run_dir, record)
        else:
            child, passed = run_tests(case, run_dir)
            record["commands"] = [child]
    except Exception as error:  # recorded and failed closed
        record["runner_fault"] = type(error).__name__ + ": " + str(error)[:300]
        passed = False
    end = identity()
    record["identity_after"] = end
    record["source_unchanged"] = start == end
    record["exit"] = 0 if passed and start == end else 1
    summary = {"case": case, "exit": record["exit"], "run_dir": str(run_dir), "head": end["head"],
               "dirty_files": end["dirty_files"], "label": LABEL, "commands": []}
    for command in record.get("commands", []):
        counts = command.get("result", {}).get("counts", {})
        summary["commands"].append({
            "label": command["label"], "exit": command["exit"], "tests_run": counts.get("tests_run"),
            "failures": counts.get("failures"), "errors": counts.get("errors"),
            "skipped": len(counts.get("skipped", [])) if counts else None,
            "status": command.get("result", {}).get("status"), "potential_secrets": command.get("potential_secrets"),
            "raw_hits": command.get("raw_hits"), "control_verdict": command.get("control_verdict"),
            "stdout_bytes": command["stdout_bytes"], "stderr_bytes": command["stderr_bytes"]})
    if "stop_reason" in record or "runner_fault" in record:
        summary["stop"] = record.get("stop_reason") or record.get("runner_fault")
    out = (json.dumps(summary, indent=1, sort_keys=True) + "\n").encode()
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
