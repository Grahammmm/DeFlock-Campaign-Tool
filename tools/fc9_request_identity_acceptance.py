"""FC9 offline acceptance. Every invocation retains fresh, source-bound evidence."""
import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import traceback
import unittest
import uuid

REPO = Path(__file__).resolve().parents[1]
PRODUCT = "campaign_tool/records/understanding/correspondence.py"
TEST = "tests/records/test_fc9_request_identity.py"
FIXTURE = "tests/fixtures/fc9-request-identity/cases.json"
RUNNER = "tools/fc9_request_identity_acceptance.py"
AUTHORED = [PRODUCT, TEST, FIXTURE, RUNNER]
CHECKERS = ["tools/check_public_tree.py", "tools/check_secret_report.py", "campaign_tool/records/public_scan.py"]
BASE_PRODUCT_HASH = "".join(["78db1be2", "a0e927cb", "de586fde", "3c0f53f2", "550e4494", "b2de938a", "90fade33", "b38756ad"])
BASE_TEST_HASH = "".join(["e9b98af0", "6e3415f7", "a5e982fe", "3285dcb4", "d9b8f1cd", "3bfa2171", "705da992", "4afacf7b"])
MODULE = "tests.records.test_fc9_request_identity"
COMPATIBILITY = ["tests.records.test_correspondence", "tests.records.test_understanding.CorrespondenceTests"]


def need(condition, message):
    if not condition:
        raise RuntimeError(message)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save_json(path, data):
    Path(path).write_text(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def captured(argv, cwd):
    return subprocess.run([str(x) for x in argv], cwd=cwd, shell=False, capture_output=True, timeout=240)


def git(*arguments, cwd=REPO):
    result = captured(["git", *arguments], cwd)
    need(result.returncode == 0, "git failure: " + result.stderr.decode(errors="replace"))
    return result.stdout.decode().strip()


def identity():
    return {"head": git("rev-parse", "HEAD"), "tree": git("rev-parse", "HEAD^{tree}"),
            "status": git("status", "--short", "--untracked-files=all"),
            "source_hashes": {p: digest(REPO / p) for p in AUTHORED + CHECKERS}}


def metadata_cases():
    data = json.loads((REPO / FIXTURE).read_text(encoding="utf-8"))
    return {"fixture_cases": len(data["cases"]), "labels": [c["label"] for c in data["cases"]],
            "orderings": ["forward", "reverse"], "occurrences_per_case": 3,
            "dimensions": ["measured delimiter pair", "percent-encoded lookalikes", "isolated colon", "percent", "ordinary", "Unicode casefold", "pipe IDs and message IDs", "single-character boundaries", "case/whitespace normalization", "empty/null/malformed", "replay", "old-ID mutation count and ownership"]}


def run_tests(selectors, discover=False):
    loader = unittest.TestLoader()
    # Sentinel surrounds discovery and execution in the same isolated process.
    before = (dict(os.environ), tempfile.tempdir, list(sys.path))

    class Sentinel(unittest.TestCase):
        def runTest(self):
            self.assertEqual(dict(os.environ), before[0])
            self.assertEqual(tempfile.tempdir, before[1])
            self.assertEqual(sys.path, before[2])

    stream = io.StringIO()
    before_result = unittest.TextTestRunner(stream=stream, verbosity=2).run(unittest.TestSuite([Sentinel()])) if discover else None
    suite = loader.discover(str(REPO / "tests" / "records"), pattern="test_fc9_request_identity.py", top_level_dir=str(REPO)) if discover else loader.loadTestsFromNames(selectors)
    count = suite.countTestCases()
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    after_result = unittest.TextTestRunner(stream=stream, verbosity=2).run(unittest.TestSuite([Sentinel()])) if discover else None
    print(stream.getvalue(), end="")
    summary = {"tests_run": result.testsRun, "loaded_tests": count, "failures": len(result.failures), "errors": len(result.errors), "skipped": len(result.skipped), "successful": result.wasSuccessful() and not result.skipped}
    if before_result is not None and after_result is not None:
        summary["sentinel_tests"] = before_result.testsRun + after_result.testsRun
        summary["successful"] = summary["successful"] and before_result.wasSuccessful() and after_result.wasSuccessful()
    print("FC9_TEST_SUMMARY " + json.dumps(summary, sort_keys=True))
    return 0 if summary["successful"] else 1


def child_main(args):
    temporary = Path(args.temporary_root).resolve(strict=True)
    tempfile.tempdir = str(temporary)
    sys.path.insert(0, str(REPO))
    if args.child == "compatibility":
        return run_tests(COMPATIBILITY)
    if args.child == "baseline":
        return run_tests([MODULE + ".IdentityTests.test_measured_request_count", MODULE + ".IdentityTests.test_measured_item_ownership", MODULE + ".IdentityTests.test_ordinary_identifiers_unchanged"])
    return run_tests([MODULE], discover=args.child == "discovery")


def main(args):
    os.umask(0o077)
    manifest_path = Path(args.source_manifest).absolute()
    manifest = json.loads(manifest_path.read_text())
    need(manifest["schema"] == "agentorange.source-manifest/v1", "manifest schema")
    need(Path(manifest["repo"]).resolve() == REPO, "manifest repository mismatch")
    need(re.fullmatch(r"[0-9a-f]{40}", manifest["head"]) is not None, "manifest head format")
    out_base = Path(args.out_base).absolute()
    need(out_base.parent.resolve() == Path(manifest["write_root"]).resolve() and out_base.name == "out", "output must be admitted out-base")
    out_base.mkdir(mode=0o700, exist_ok=True)
    output = out_base / (args.phase + "-" + uuid.uuid4().hex)
    output.mkdir(mode=0o700)
    temporary = output / "temporary"
    temporary.mkdir(mode=0o700)
    tempfile.tempdir = str(temporary)
    receipt = {"phase": args.phase, "run_dir": str(output), "started_at": datetime.now(timezone.utc).isoformat(),
               "argv": sys.argv, "cwd": str(Path.cwd()), "interpreter": sys.executable,
               "interpreter_resolved": str(Path(sys.executable).resolve()), "manifest_file_sha256": digest(manifest_path),
               "commands": [], "coverage": metadata_cases(), "review_required": True, "remote_closure": "OWED"}

    def command(label, argv, cwd=REPO, allowed=(0,)):
        proc = captured(argv, cwd)
        (output / (label + ".stdout")).write_bytes(proc.stdout)
        (output / (label + ".stderr")).write_bytes(proc.stderr)
        record = {"label": label, "argv": [str(x) for x in argv], "cwd": str(cwd), "exit": proc.returncode,
                  "stdout": label + ".stdout", "stderr": label + ".stderr"}
        receipt["commands"].append(record)
        # Failures are never silently retried or converted to killed mutations.
        need(proc.returncode in allowed, label + " unexpected exit " + str(proc.returncode))
        return proc

    def child(label, kind, cwd=REPO, allowed=(0,)):
        child_tmp = temporary / label
        child_tmp.mkdir(mode=0o700)
        return command(label, [sys.executable, str(REPO / RUNNER), "--child", kind, "--temporary-root", str(child_tmp)], cwd, allowed)

    def summary(proc):
        rows = [line.removeprefix("FC9_TEST_SUMMARY ") for line in proc.stdout.decode().splitlines() if line.startswith("FC9_TEST_SUMMARY ")]
        need(len(rows) == 1, "missing/duplicate unittest summary")
        return json.loads(rows[0])

    exit_code = 1
    try:
        need((REPO / ".venv").is_symlink(), "standard staging venv link missing")
        need((REPO / ".venv").resolve() == Path(manifest["runtime"]["repo_venv"]).resolve(), "staging venv mismatch")
        need(Path(sys.prefix).resolve() == Path(manifest["runtime"]["repo_venv"]).resolve(), "runtime venv prefix mismatch")
        receipt["runtime_prefix"] = sys.prefix
        receipt["staged_venv_resolved"] = str((REPO / ".venv").resolve())
        need(Path(sys.executable).resolve() == (REPO / ".venv" / "bin" / "python3").resolve(), "interpreter mismatch")
        if args.phase == "credentials":
            need(os.path.abspath(sys.executable) == os.path.abspath(manifest["runtime"]["credential_scan_python3"]), "literal credential interpreter mismatch")
        receipt["initial_identity"] = identity()
        initial = receipt["initial_identity"]
        base_tree = git("rev-parse", manifest["head"] + "^{tree}")
        need(base_tree == manifest["tree"], "manifest baseline tree mismatch")
        need(git("merge-base", manifest["head"], initial["head"]) == manifest["head"], "candidate does not descend from admitted base")
        need(digest(REPO / "tests/records/test_correspondence.py") == BASE_TEST_HASH, "existing tests changed")
        need(hashlib.sha256(captured(["git", "show", manifest["head"] + ":" + PRODUCT], REPO).stdout).hexdigest() == BASE_PRODUCT_HASH, "base product pin mismatch")
        if args.phase == "baseline":
            need(initial["head"] == manifest["head"] and digest(REPO / PRODUCT) == BASE_PRODUCT_HASH, "test-first requires pinned original product")
            proc = child("test-first-red", "baseline", allowed=(1,))
            report = summary(proc)
            need(report == {"tests_run": 3, "loaded_tests": 3, "failures": 2, "errors": 0, "skipped": 0, "successful": False}, "red must be exact count and ownership failures, ordinary positive green")
            text = proc.stdout.decode()
            need("measured request count" in text and "measured item ownership" in text, "wrong red assertions")
            receipt["test_summary"] = report
            receipt["status"] = "EXPECTED_TEST_FIRST_RED_NOT_ACCEPTANCE"
        else:
            need(initial["head"] != manifest["head"], "final acceptance requires committed candidate")
            need(initial["status"] == "", "final acceptance requires clean tracked and untracked tree")
            inventory = git("ls-files", "--cached", "--others", "--exclude-standard").splitlines()
            receipt["scan_visible_inventory"] = inventory
            tracked = set(git("ls-files").splitlines())
            need(set(inventory) - tracked <= {".venv"}, "untracked scan-visible files")
            for path in AUTHORED:
                need(path in tracked, "authored file not committed")
                head_bytes = captured(["git", "show", initial["head"] + ":" + path], REPO)
                index_bytes = captured(["git", "show", ":" + path], REPO)
                need(head_bytes.returncode == index_bytes.returncode == 0 and head_bytes.stdout == index_bytes.stdout == (REPO / path).read_bytes(), "HEAD/index/working byte mismatch")
            changed = set(git("diff", "--name-only", manifest["head"], initial["head"]).splitlines())
            need(changed == set(AUTHORED), "exclusive write set mismatch")
            if args.phase in ("suite", "compatibility"):
                report = summary(child(args.phase, args.phase))
                need(report["successful"] and report["tests_run"] > 0 and report["skipped"] == 0, "incomplete suite")
                receipt["test_summary"] = report
            elif args.phase == "portable":
                for path in AUTHORED[1:]:
                    content = (REPO / path).read_text()
                    # Construct literals so the scanner does not flag itself.
                    need(not any(prefix in content for prefix in ("/" + "home/", "/" + "Users/", "/" + "workspace/")), "host-specific authored path")
                cwd = output / "fresh-cwd"
                cwd.mkdir(mode=0o700)
                script_tmp = temporary / "script"
                script_tmp.mkdir(mode=0o700)
                script = command("absolute-script", [sys.executable, str(REPO / TEST), "--temporary-root", str(script_tmp)], cwd)
                discovery = summary(child("ordinary-discovery", "discovery", cwd))
                script_text = script.stderr.decode()
                match = re.search(r"Ran (\d+) tests?", script_text)
                if match is None:
                    raise RuntimeError("absolute-script unittest count missing")
                need(int(match[1]) == discovery["tests_run"], "script/discovery coverage count mismatch")
                need(discovery["successful"] and discovery["sentinel_tests"] == 2, "discovery/sentinel failed")
                receipt["test_summary"] = discovery
                receipt["script_tests"] = int(match[1])
                receipt["equal_fixture_coverage"] = True
            elif args.phase == "credentials":
                scanner = Path(manifest["runtime"]["detect_secrets"])
                need(scanner.is_file() and scanner.parent == Path(manifest["runtime"]["credential_scan_python3"]).parent, "scanner runtime mismatch")
                # The unchanged tool uses git's scan-visible inventory. Reject
                # unexpected links before invoking it; never traverse a venv.
                need(not any((REPO / p).is_symlink() for p in inventory), "scanner inventory includes a staging/dependency link; operator disposition required")
                proc = command("detect-secrets", [str(scanner), "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"])
                data = json.loads(proc.stdout)
                need(isinstance(data.get("results"), dict), "invalid genuine scanner JSON")
                command("check-secret-report", [sys.executable, str(REPO / "tools/check_secret_report.py"), str(output / "detect-secrets.stdout")])
                receipt["scan_file_count"] = len(inventory)
            elif args.phase == "public-tree":
                clone = output / "committed-tree"
                command("clone", ["git", "clone", "--local", "--no-hardlinks", "--no-checkout", "--no-recurse-submodules", str(REPO), str(clone)], output)
                command("checkout", ["git", "checkout", "--detach", initial["head"]], clone)
                need(git("rev-parse", "HEAD", cwd=clone) == initial["head"], "clone HEAD mismatch")
                need(git("rev-parse", "HEAD^{tree}", cwd=clone) == initial["tree"], "clone tree mismatch")
                need(not (clone / ".venv").exists(), "clone contains runtime link")
                receipt["clone_hashes"] = {p: digest(clone / p) for p in AUTHORED + CHECKERS}
                need(receipt["clone_hashes"] == initial["source_hashes"], "clone/checker hash mismatch")
                for path in CHECKERS:
                    baseline = captured(["git", "show", manifest["head"] + ":" + path], REPO)
                    need(baseline.returncode == 0 and hashlib.sha256(baseline.stdout).hexdigest() == digest(clone / path), "checker/allowlist changed")
                proc = command("committed-public-tree", [sys.executable, str(clone / "tools/check_public_tree.py")], clone)
                result = json.loads(proc.stdout)
                need(result.get("findings") == [] and result.get("scanned_files") == len(tracked), "committed public scan incomplete/dirty")
                receipt["committed_public_result"] = result
                info = command("in_place_informational-INC-174", [sys.executable, str(REPO / "tools/check_public_tree.py")], allowed=(0, 1))
                info_result = json.loads(info.stdout)
                need((info.returncode == 0 and info_result.get("findings") == []) or (info.returncode == 1 and info_result.get("findings") == [[".venv", 0, "symlink_not_scanned"]]), "unexpected staged scan finding")
                need(info_result.get("scanned_files") == len(tracked), "staged scan incomplete")
                receipt["in_place_informational_INC_174"] = info_result
            receipt["status"] = "PASS_REVIEW_REQUIRED"
        receipt["final_identity"] = identity()
        need(receipt["initial_identity"] == receipt["final_identity"], "source identity changed during row")
        exit_code = 0
    except Exception as error:
        receipt["status"] = "FAIL_STOP"
        receipt["error"] = str(error)
        (output / "exception.stderr").write_text(traceback.format_exc())
    receipt["exit"] = exit_code
    receipt["artifact_hashes"] = {str(p.relative_to(output)): digest(p) for p in output.iterdir() if p.is_file()}
    save_json(output / "receipt.json", receipt)
    print(json.dumps({"phase": args.phase, "status": receipt["status"], "exit": exit_code, "run_dir": str(output), "receipt_sha256": digest(output / "receipt.json"), "test_summary": receipt.get("test_summary"), "error": receipt.get("error")}, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=["baseline", "suite", "compatibility", "portable", "credentials", "public-tree"])
    parser.add_argument("--source-manifest")
    parser.add_argument("--out-base")
    parser.add_argument("--child", choices=["baseline", "suite", "compatibility", "discovery"])
    parser.add_argument("--temporary-root")
    arguments = parser.parse_args()
    if arguments.child:
        need(arguments.temporary_root is not None, "child temporary root required")
        raise SystemExit(child_main(arguments))
    need(arguments.phase and arguments.source_manifest and arguments.out_base, "phase/manifest/out-base required")
    raise SystemExit(main(arguments))
