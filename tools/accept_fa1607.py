"""FA1607 scoped acceptance launcher (fake-Pillow image resource matrix).

    python3 tools/accept_fa1607.py --case CASE --output-root DIR

CASE is one of behavior, compatibility, negative-control, isolation, credential-scan
or public-tree. Every invocation creates a fresh exclusive ``<case>-<utc>-<pid>``
child of DIR (DIR must be absolute, outside this repository and free of symlinked
ancestors) holding output.txt and result.json: argv, cwd, interpreter, full HEAD,
tree, base, source/test hashes, selected test IDs, counts, skips, child-process argv
and exits, and the final exit.

Before any repository module is imported the launcher disables bytecode writes,
redirects tempfile/TMPDIR/HOME into the run directory and refuses IPv4/IPv6
connections and name lookups. Zero tests, unexpected failures, errors or skips, a
missing case, a missing declared runtime and child-process failures are fatal.
Base and runtime identity are read at run time from the operator SOURCE-MANIFEST.json
next to the repository and from git; nothing is installed or substituted.

A pass is REVIEW REQUIRED: scoped offline evidence, not full CI, real decoder
qualification or semantic acceptance.
"""
import argparse
import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile

REPO = Path(__file__).resolve().parents[1]
MANIFEST = REPO.parent / "SOURCE-MANIFEST.json"
TEST_MODULE = "tests.records.test_fa1607_image_resource_matrix"
NEW = TEST_MODULE + "."
MATRIX = NEW + "FA1607ImageResourceMatrixTests."
BEHAVIOR = [MATRIX + name for name in (
    "test_exactly_frame_limit_of_tiny_frames_is_accepted",
    "test_exactly_local_pixel_limit_in_one_frame_is_accepted",
    "test_exactly_total_pixel_limit_across_safe_frames_is_accepted",
    "test_mixed_frame_dimensions_follow_seek_and_load_after_check",
    "test_product_sets_max_image_pixels_inside_scoped_seam",
    "test_frame_count_over_limit_refused_before_any_seek_or_load",
    "test_local_pixels_over_limit_refused_before_offending_load",
    "test_cumulative_pixels_over_limit_refused_before_offending_load",
)] + [NEW + "FA1607SeamHygieneTests.test_setup_and_teardown_restore_real_pil_global_and_open"]
COMPATIBILITY = ["tests.records.test_extraction_routes.ReadOutputTests.test_each_image_frame_has_a_locator"]
CONTROL = [NEW + "FA1607FaultyReaderControlTests.test_faulty_reader_fails_only_cumulative_check"]
CASES = ("behavior", "compatibility", "negative-control", "isolation", "credential-scan", "public-tree")
NEW_FILES = ("tests/records/test_fa1607_image_resource_matrix.py", "tools/accept_fa1607.py")
ALLOWED_PREFIX = "tests/fixtures/fa1607/"
HASHED = ("campaign_tool/records/extraction_routes.py", *NEW_FILES, "tests/records/test_extraction_routes.py",
          "tests/records/ocr_fixtures.py", "tools/check_secret_report.py", "tools/check_public_tree.py",
          "campaign_tool/records/public_scan.py")
BOX_MARKERS = tuple(marker.encode() for marker in (
    "/" + "home" + "/", "/" + "Users" + "/", "agent" + "-work", ".hermes" + "/", "/" + "tmp" + "/"))


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def run_child(argv, cwd, record, env=None):
    done = subprocess.run(argv, cwd=cwd, capture_output=True, check=False, env=env)
    record.setdefault("subprocesses", []).append({
        "argv": [str(a) for a in argv], "cwd": str(cwd), "exit": done.returncode,
        "stdout_sha256": sha256(done.stdout), "stderr_sha256": sha256(done.stderr),
        "stdout_bytes": len(done.stdout), "stderr_bytes": len(done.stderr)})
    return done


def git(*args, cwd=REPO):
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=False)
    if done.returncode != 0:
        raise SystemExit("git_failed:" + " ".join(args) + ":" + done.stderr.decode("utf-8", "replace").strip())
    return done.stdout.decode("utf-8", "replace")


def hashes():
    return {name: sha256((REPO / name).read_bytes()) if (REPO / name).is_file() else None for name in HASHED}


def make_run_directory(output_root, case):
    root = Path(output_root)
    if not root.is_absolute() or ".." in root.parts:
        raise SystemExit("output_root_must_be_absolute_and_normalized")
    for ancestor in [root, *root.parents]:
        if ancestor.is_symlink():
            raise SystemExit("output_root_symlinked_ancestor:" + str(ancestor))
        if ancestor.exists() and not ancestor.is_dir():
            raise SystemExit("output_root_ancestor_not_directory:" + str(ancestor))
    if root == REPO or REPO in root.parents:
        raise SystemExit("output_root_inside_repository")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    run = root / "{}-{}-{}".format(case, stamp, os.getpid())
    run.mkdir(mode=0o700)
    return root, run


def load_manifest(record):
    manifest = json.loads(MANIFEST.read_text())
    runtime = manifest.get("runtime") or {}
    record["manifest"] = {"path": str(MANIFEST), "sha256": sha256(MANIFEST.read_bytes()),
                          "head": manifest.get("head"), "repo": manifest.get("repo")}
    if Path(manifest.get("repo", "")) != REPO:
        raise SystemExit("manifest_repo_mismatch")
    return manifest, runtime


def guard(run, record):
    """Pre-import boundary: bytecode off, temp/home redirected, network refused."""
    sys.dont_write_bytecode = True
    scratch = run / "tmp"
    home = run / "home"
    scratch.mkdir(mode=0o700)
    home.mkdir(mode=0o700)
    tempfile.tempdir = str(scratch)
    os.environ["TMPDIR"] = str(scratch)
    os.environ["HOME"] = str(home)
    attempts = []
    inet = (socket.AF_INET, socket.AF_INET6)
    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex

    def connect(sock, address):
        if sock.family in inet:
            attempts.append("connect:" + repr(address))
            raise OSError("fa1607_acceptance_offline")
        return real_connect(sock, address)

    def connect_ex(sock, address):
        if sock.family in inet:
            attempts.append("connect_ex:" + repr(address))
            raise OSError("fa1607_acceptance_offline")
        return real_connect_ex(sock, address)

    def lookup(*args, **kwargs):
        attempts.append("getaddrinfo:" + repr(args[:2]))
        raise OSError("fa1607_acceptance_offline")

    socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo = connect, connect_ex, lookup
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    record["guards"] = {"tmp": str(scratch), "home": str(home), "dont_write_bytecode": True}
    return scratch, attempts


def run_suite(ids, label, record, stream, suite=None):
    import unittest
    passed = []

    class Recording(unittest.TextTestResult):
        def addSuccess(self, test):
            super().addSuccess(test)
            passed.append(test.id())

    stream.write("== {} ==\n".format(label))
    if suite is None:
        suite = unittest.defaultTestLoader.loadTestsFromNames(ids)
    result = unittest.TextTestRunner(stream=stream, verbosity=2, resultclass=Recording).run(suite)
    entry = {"label": label, "selected": list(ids), "tests_run": result.testsRun, "passed": sorted(passed),
             "failures": [[t.id(), text] for t, text in result.failures],
             "errors": [[t.id(), text] for t, text in result.errors],
             "skipped": [[t.id(), why] for t, why in result.skipped],
             "expected_failures": len(result.expectedFailures),
             "unexpected_successes": len(result.unexpectedSuccesses)}
    record.setdefault("suites", []).append(entry)
    return entry


def suite_ok(entry, problems, expect=None):
    expect = sorted(entry["selected"]) if expect is None else sorted(expect)
    if entry["tests_run"] == 0:
        problems.append(entry["label"] + ":no_tests_ran")
    if entry["failures"] or entry["errors"] or entry["expected_failures"] or entry["unexpected_successes"]:
        problems.append(entry["label"] + ":test_failures")
    if entry["skipped"]:
        problems.append(entry["label"] + ":unexpected_skips")
    if entry["passed"] != expect:
        problems.append(entry["label"] + ":selected_tests_not_all_passed")


def global_state():
    from PIL import Image
    return {"pil_open": repr(Image.open), "pil_open_module": Image.open.__module__,
            "max_image_pixels": Image.MAX_IMAGE_PIXELS, "tempdir": tempfile.tempdir,
            "tmpdir_env": os.environ.get("TMPDIR"), "sys_path": list(sys.path),
            "connect": repr(socket.socket.connect), "getaddrinfo": repr(socket.getaddrinfo),
            "pil_module": repr(sys.modules.get("PIL"))}


def case_behavior(run, record, stream, problems):
    entry = run_suite(BEHAVIOR, "behavior", record, stream)
    suite_ok(entry, problems)


def case_compatibility(run, record, stream, problems):
    import importlib
    import inspect
    module = importlib.import_module("tests.records.test_extraction_routes")
    method = module.ReadOutputTests.test_each_image_frame_has_a_locator
    text = inspect.getsource(method)
    record["compatibility_inspection"] = {
        "method_source_sha256": sha256(text.encode()), "module_imports": sorted(
            name for name in ("tests.records.ocr_fixtures", "campaign_tool.records.extraction_routes")
            if name in sys.modules),
        "uses_tmp_fixture_dir": "self.root" in text, "subprocess_in_method": "subprocess" in text,
        "socket_in_method": "socket" in text, "fixture_input": "synthetic bytes b'fixture' via fake PIL module"}
    if "subprocess" in text or "socket" in text or "self.root" in text or "open(" in text.replace("open=lambda", ""):
        problems.append("compatibility_method_closure_not_inert")
    entry = run_suite(COMPATIBILITY, "compatibility", record, stream)
    suite_ok(entry, problems)


def case_negative_control(run, record, stream, problems):
    import unittest
    import importlib
    tests = importlib.import_module(TEST_MODULE)
    from PIL import Image
    real_open, real_max = Image.open, Image.MAX_IMAGE_PIXELS
    faulty = type("FaultyReaderMatrix", (tests.FA1607ImageResourceMatrixTests,),
                  {"READER": staticmethod(tests.image_without_cumulative_accounting)})
    decisive = "test_cumulative_pixels_over_limit_refused_before_offending_load"
    controls = ("test_frame_count_over_limit_refused_before_any_seek_or_load",
                "test_local_pixels_over_limit_refused_before_offending_load")
    fault = run_suite([decisive], "faulty-variant-cumulative", record, stream,
                      unittest.TestSuite([faulty(decisive)]))
    caught = (fault["tests_run"] == 1 and len(fault["failures"]) == 1 and not fault["errors"]
              and not fault["skipped"] and "ValueError not raised" in fault["failures"][0][1])
    record["negative_control"] = {"faulty_variant": "image_without_cumulative_accounting",
                                  "decisive_test": decisive, "intended_failure_caught": caught,
                                  "retained_failed_assertion": fault["failures"][0][1] if fault["failures"] else None}
    if not caught:
        problems.append("intended_failure_not_caught")
    held = run_suite(list(controls), "faulty-variant-orthogonal-controls", record, stream,
                     unittest.TestSuite([faulty(name) for name in controls]))
    suite_ok(held, problems, expect=[faulty(name).id() for name in controls])
    if Image.open is not real_open or Image.MAX_IMAGE_PIXELS != real_max:
        problems.append("seams_not_restored_after_faulty_variant")
    positives = run_suite(BEHAVIOR, "restored-positive-behavior", record, stream)
    suite_ok(positives, problems)
    portable = run_suite(CONTROL, "in-module-control", record, stream)
    suite_ok(portable, problems)


def case_isolation(run, record, stream, problems, runtime):
    before_hashes = hashes()
    for name in NEW_FILES:
        data = (REPO / name).read_bytes()
        hits = [marker.decode() for marker in BOX_MARKERS if marker in data]
        if hits:
            problems.append("box_only_path_in:" + name + ":" + ",".join(hits))
    state_before = global_state()
    first = run_suite(BEHAVIOR, "isolation-in-process-behavior", record, stream)
    suite_ok(first, problems)
    compat = run_suite(COMPATIBILITY, "isolation-peer-after-behavior", record, stream)
    suite_ok(compat, problems)
    state_after = global_state()
    record["global_state"] = {"before": state_before, "after": state_after}
    if state_before != state_after:
        problems.append("global_state_poisoned")
    second_root = run / "second-prefix"
    child = run_child([sys.executable, str(Path(__file__).resolve()), "--case", "behavior",
                       "--output-root", str(second_root)], REPO, record)
    stream.write("== child behavior in second prefix ==\n")
    stream.write(child.stdout.decode("utf-8", "replace"))
    children = sorted(p for p in second_root.iterdir() if p.is_dir()) if second_root.is_dir() else []
    child_result = json.loads((children[0] / "result.json").read_text()) if len(children) == 1 else {}
    record["second_prefix"] = {"root": str(second_root), "runs": [str(p) for p in children],
                               "exit": child_result.get("exit"), "tests_run": child_result.get("tests_run")}
    if child.returncode != 0 or child_result.get("exit") != 0 or child_result.get("tests_run") != len(BEHAVIOR):
        problems.append("second_prefix_behavior_failed")
    if child_result.get("candidate_head") != record["candidate_head"]:
        problems.append("second_prefix_head_mismatch")
    if hashes() != before_hashes:
        problems.append("source_identity_changed")


def scan_checkout(run, name, record, problems):
    """Fresh local no-hardlink clone at the literal HEAD, verified against the candidate."""
    dest = run / name
    head = record["candidate_head"]
    run_child(["git", "clone", "--quiet", "--no-hardlinks", "--no-checkout", str(REPO), str(dest)], run, record)
    run_child(["git", "checkout", "--quiet", "--detach", head], dest, record)
    checks = {
        "head": git("rev-parse", "HEAD", cwd=dest).strip() == head,
        "tree": git("rev-parse", "HEAD^{tree}", cwd=dest).strip() == record["candidate_tree"],
        "index": git("ls-files", "-s", cwd=dest) == git("ls-files", "-s"),
        "clean": git("status", "--porcelain", "--untracked-files=all", cwd=dest) == "",
        "new_files_tracked": all(name in git("ls-files", cwd=dest).splitlines() for name in NEW_FILES),
    }
    record.setdefault("scan_checkouts", {})[name] = {"path": str(dest), "checks": checks,
                                                     "tracked_files": len(git("ls-files", cwd=dest).splitlines())}
    if not all(checks.values()):
        problems.append("scan_checkout_mismatch:" + name)
    return dest


def case_credential_scan(run, record, stream, problems, runtime):
    python = runtime.get("credential_scan_python3")
    scanner = runtime.get("detect_secrets")
    if not python or not scanner or not os.access(python, os.X_OK) or not Path(scanner).is_file():
        problems.append("missing_declared_runtime:credential_scan_python3/detect_secrets")
        return
    dest = scan_checkout(run, "scan-checkout", record, problems)
    if problems:
        return
    report = run / "detect-secrets.json"
    done = run_child([python, scanner, "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"],
                     dest, record)
    report.write_bytes(done.stdout)
    if done.returncode != 0:
        problems.append("detect_secrets_failed")
        stream.write(done.stderr.decode("utf-8", "replace"))
        return
    checked = run_child([python, str(dest / "tools" / "check_secret_report.py"), str(report)], dest, record)
    text = checked.stdout.decode("utf-8", "replace")
    stream.write("== check_secret_report ==\n" + text + checked.stderr.decode("utf-8", "replace"))
    results = json.loads(done.stdout)["results"]
    record["credential_scan"] = {"report_path": str(report), "report_sha256": sha256(done.stdout),
                                 "files_with_hits": sorted(results), "checker_exit": checked.returncode,
                                 "scanner_exit": done.returncode, "checked_head": record["candidate_head"]}
    if checked.returncode != 0 or "potential secrets: 0;" not in text:
        problems.append("unapproved_potential_secrets_or_checker_failed")
    for name in NEW_FILES:
        if name in results:
            problems.append("new_file_has_scan_hits:" + name)


def case_public_tree(run, record, stream, problems, runtime):
    base = record["manifest"]["head"]
    dest = scan_checkout(run, "public-tree-checkout", record, problems)
    if problems:
        return
    python = str(REPO / ".venv" / "bin" / "python3")
    done = run_child([python, str(dest / "tools" / "check_public_tree.py")], dest, record)
    text = done.stdout.decode("utf-8", "replace")
    stream.write("== check_public_tree ==\n" + text + done.stderr.decode("utf-8", "replace"))
    try:
        result = json.loads(text)
    except ValueError:
        result = {}
    changed = git("diff", "--name-only", base, "HEAD").splitlines()
    untracked = git("status", "--porcelain", "--untracked-files=all")
    diff_check = run_child(["git", "diff", "--check", base, "HEAD"], REPO, record)
    outside = [p for p in changed if p not in NEW_FILES and not p.startswith(ALLOWED_PREFIX)]
    record["public_tree"] = {"exit": done.returncode, "scanned_files": result.get("scanned_files"),
                             "findings": result.get("findings"), "changed_paths": changed,
                             "outside_allowed_paths": outside, "candidate_status": untracked.splitlines(),
                             "diff_check_exit": diff_check.returncode,
                             "diff_check_stdout": diff_check.stdout.decode("utf-8", "replace")}
    if done.returncode != 0 or result.get("findings") != [] or not result.get("scanned_files"):
        problems.append("public_tree_violations_or_failure")
    if outside or not set(NEW_FILES) <= set(changed):
        problems.append("changed_paths_outside_scope_or_missing")
    if untracked:
        problems.append("uncommitted_or_untracked_changes")
    if diff_check.returncode != 0 or diff_check.stdout:
        problems.append("diff_check_failed")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--case", required=True, choices=CASES)
    parser.add_argument("--output-root", required=True)
    args = parser.parse_args(argv)
    _, run = make_run_directory(args.output_root, args.case)
    record = {"case": args.case, "argv": [sys.executable, *sys.argv], "cwd": os.getcwd(),
              "interpreter": sys.executable, "python": sys.version.split()[0],
              "candidate_head": git("rev-parse", "HEAD").strip(),
              "candidate_tree": git("rev-parse", "HEAD^{tree}").strip(),
              "status_before": git("status", "--porcelain", "--untracked-files=all").splitlines(),
              "source_sha256": hashes(), "run_directory": str(run),
              "full_discovery": "operator-owned (not part of this scoped acceptance)",
              "disposition": "REVIEW REQUIRED - scoped offline acceptance, not full CI or semantic acceptance"}
    problems = []
    stream = io.StringIO()
    manifest, runtime = load_manifest(record)
    base = manifest.get("head")
    if subprocess.run(["git", "merge-base", "--is-ancestor", str(base), "HEAD"], cwd=REPO).returncode != 0:
        problems.append("head_does_not_descend_from_manifest_base")
    scratch, attempts = guard(run, record)
    if not problems:
        if args.case == "behavior":
            case_behavior(run, record, stream, problems)
        elif args.case == "compatibility":
            case_compatibility(run, record, stream, problems)
        elif args.case == "negative-control":
            case_negative_control(run, record, stream, problems)
        elif args.case == "isolation":
            case_isolation(run, record, stream, problems, runtime)
        elif args.case == "credential-scan":
            case_credential_scan(run, record, stream, problems, runtime)
        else:
            case_public_tree(run, record, stream, problems, runtime)
    record["network_attempts"] = attempts
    if attempts:
        problems.append("network_attempted")
    if any(scratch.iterdir()):
        problems.append("temporary_residue")
    for child in record.get("subprocesses", []):
        if child["exit"] != 0:
            problems.append("child_process_failed:" + " ".join(child["argv"][:3]))
    suites = record.get("suites", [])
    record["tests_run"] = sum(s["tests_run"] for s in suites)
    record["skipped"] = sum(len(s["skipped"]) for s in suites)
    record["status_after"] = git("status", "--porcelain", "--untracked-files=all").splitlines()
    if record["status_after"] != record["status_before"]:
        problems.append("repository_changed_during_acceptance")
    if hashes() != record["source_sha256"]:
        problems.append("source_changed_during_acceptance")
    record["problems"] = problems
    code = 1 if problems else 0
    raw = stream.getvalue().encode("utf-8")
    (run / "output.txt").write_bytes(raw)
    record.update(exit=code, output_path=str(run / "output.txt"), output_sha256=sha256(raw))
    (run / "result.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    print(stream.getvalue()[-6000:])
    print(json.dumps({key: record.get(key) for key in (
        "case", "exit", "problems", "tests_run", "skipped", "interpreter", "candidate_head",
        "run_directory", "output_sha256")}, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
