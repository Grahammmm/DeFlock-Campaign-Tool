"""FC16-DETECTOR-MANIFEST-BALANCE five-row local acceptance recorder.

Records real unittest, scanner and checker executions for this card only; it is not a
substitute product implementation or a general framework. Machine-valid output means
REVIEW REQUIRED, never factual, legal or human acceptance.

Author: claude-opus-5-5 / anthropic (opus-coder-bopus). Offline; no network, no pushes.
Source pins are bound through the supplied source manifest head and git blobs; no hash
literals are embedded here.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import traceback
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
TEST = "tests/records/test_fc16_detector_manifest_balance.py"
TEST_MODULE = "tests.records.test_fc16_detector_manifest_balance"
FIXTURE = "tests/fixtures/fc16_detector_manifest_balance/cases.json"
RECORDER = "tools/fc16_detector_manifest_balance_accept.py"
AUTHORED = (TEST, FIXTURE, RECORDER)
PINNED = ("campaign_tool/records/detectors/core.py", "tests/records/test_detectors.py",
          "campaign_tool/records/detectors/__init__.py", "campaign_tool/records/__init__.py",
          "campaign_tool/__init__.py")
CHECKERS = ("tools/check_secret_report.py", "tools/check_public_tree.py", "campaign_tool/records/public_scan.py")
SELECTORS = ("tests.records.test_detectors.DetectorTests",)
CASE_TESTS = {"mixed_outcomes": "test_mixed_outcomes", "multiplicity": "test_multiplicity",
              "outside_interval": "test_outside_interval", "conflict": "test_conflict",
              "empty_and_order": "test_empty_and_order",
              "deciding_negative_control": "test_deciding_negative_control"}
# Frozen subcase matrix per detector (seven detectors):
# mixed 1, multiplicity 2 shapes x 3 extras, outside 1, conflict 1, empty/order 5, controls 3.
SUBCASES_PER_DETECTOR = {"mixed_outcomes": 1, "multiplicity": 6, "outside_interval": 1, "conflict": 1,
                         "empty_and_order": 5, "deciding_negative_control": 3}
DETECTOR_COUNT = 7
EXPECTED_SUBCASES = DETECTOR_COUNT * sum(SUBCASES_PER_DETECTOR.values())
CHILD_ENV_KEYS = ("TMPDIR", "TMP", "TEMP")


class RowFailure(Exception):
    pass


def need(condition, reason):
    if not condition:
        raise RowFailure(reason)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def git(args, cwd=ROOT):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, check=True, shell=False).stdout


def no_symlink_ancestry(path):
    need(not any(part.is_symlink() for part in (path, *path.parents)), "symlink in path ancestry: " + str(path))
    return path


def identity(base):
    return {"head": git(["rev-parse", "HEAD"]).decode().strip(),
            "tree": git(["rev-parse", "HEAD^{tree}"]).decode().strip(),
            "status": git(["status", "--porcelain=v1", "--untracked-files=all"]).decode(),
            "authored_sha256": {p: sha((ROOT / p).read_bytes()) for p in AUTHORED},
            "pinned_sha256": {p: sha((ROOT / p).read_bytes()) for p in PINNED},
            "checker_sha256": {p: sha((ROOT / p).read_bytes()) for p in CHECKERS},
            "diff_sha256": sha(git(["diff", "--binary", base, "HEAD"])),
            "changed_paths": git(["diff", "--name-only", base, "HEAD"]).decode().splitlines()}


# ---------------------------------------------------------------- child processes

def _ambient():
    return {"env": {k: os.environ.get(k) for k in sorted(os.environ)}, "tempdir": tempfile.tempdir,
            "socket": socket.socket, "create_connection": socket.create_connection,
            "popen": subprocess.Popen, "bytecode": sys.dont_write_bytecode}


def child(mode, out):
    """Run declared tests in-process after the temporary-root redirect, before product import."""
    temporary = out / ("tmp-" + mode)
    temporary.mkdir()
    for key in CHILD_ENV_KEYS:
        need(os.environ.get(key) == str(temporary), "child temporary redirect missing: " + key)
    tempfile.tempdir = str(temporary)
    sys.dont_write_bytecode = True
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    before = _ambient()
    loaded_before = set(sys.modules)
    records = {"mode": mode, "test_ids": [], "subcases": [], "statuses": {}}

    class Result(unittest.TextTestResult):
        def startTest(self, test):
            records["test_ids"].append(test.id())
            super().startTest(test)

        def addSuccess(self, test):
            records["statuses"][test.id()] = "ok"
            super().addSuccess(test)

        def addFailure(self, test, err):
            records["statuses"][test.id()] = "fail"
            super().addFailure(test, err)

        def addError(self, test, err):
            records["statuses"][test.id()] = "error"
            super().addError(test, err)

        def addSkip(self, test, reason):
            records["statuses"][test.id()] = "skip"
            super().addSkip(test, reason)

        def addSubTest(self, test, subtest, err):
            records["subcases"].append({"id": subtest.id(), "success": err is None})
            super().addSubTest(test, subtest, err)

    class InertPeer(unittest.TestCase):
        """Unrelated peer: ambient defaults are unchanged around the card module."""

        def runTest(self):
            now = _ambient()
            for key in before:
                self.assertEqual(now[key], before[key], "ambient default changed: " + key)

    loader = unittest.TestLoader()
    if mode == "compatibility":
        suite = loader.loadTestsFromNames(list(SELECTORS))
    elif mode == "suite":
        suite = loader.loadTestsFromName(TEST_MODULE)
    else:
        discovered = loader.discover(start_dir=str(ROOT / "tests" / "records"),
                                     pattern=Path(TEST).name, top_level_dir=str(ROOT))
        suite = unittest.TestSuite([InertPeer(), discovered, InertPeer()])
    result = unittest.TextTestRunner(stream=sys.stdout, verbosity=2, resultclass=Result).run(suite)
    after = _ambient()
    records.update(tests_run=result.testsRun, skipped=len(result.skipped), failures=len(result.failures),
                   errors=len(result.errors), successful=result.wasSuccessful(),
                   ambient_unchanged=all(after[k] == before[k] for k in before),
                   new_modules=sorted(set(sys.modules) - loaded_before))
    write_json(out / ("tests-" + mode + ".json"), records)
    return 0 if result.wasSuccessful() and not result.skipped and records["ambient_unchanged"] else 1


# ---------------------------------------------------------------- row helpers

def validate_card_matrix(records, prefix):
    expected_ids = sorted(prefix + "DetectorManifestBalanceTests." + name for name in CASE_TESTS.values())
    measured = sorted(i for i in records["test_ids"] if i.endswith(tuple(CASE_TESTS.values())))
    need(measured == expected_ids, "card test identity mismatch")
    need(records["skipped"] == 0 and records["failures"] == 0 and records["errors"] == 0, "card failures or skips")
    need(len(records["subcases"]) == EXPECTED_SUBCASES, "frozen subcase count mismatch")
    need(all(s["success"] for s in records["subcases"]), "failed frozen subcase")
    per_case = {case: 0 for case in CASE_TESTS}
    for subcase in records["subcases"]:
        match = re.search(r"case='([a-z_]+)'", subcase["id"])
        need(match is not None and match.group(1) in per_case, "unmapped subcase: " + subcase["id"])
        per_case[match.group(1)] += 1
    for case, count in SUBCASES_PER_DETECTOR.items():
        need(per_case[case] == count * DETECTOR_COUNT, "missing subcases for " + case)
    return per_case


def unittest_lines(raw):
    lines = {}
    for line in raw.decode(errors="replace").splitlines():
        match = re.match(r"^(test\S*) \((\S+)\)(?: \.\.\.)? (ok|FAIL|ERROR|skipped.*|expected failure)$", line)
        if match:
            lines[match.group(2)] = match.group(3)
    return lines


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=["suite", "compatibility", "portable", "credentials", "public-tree"])
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--out-base", type=Path)
    parser.add_argument("--child", choices=["suite", "compatibility", "peer"])
    parser.add_argument("--child-output", type=Path)
    options = parser.parse_args()
    if options.child:
        return child(options.child, no_symlink_ancestry(options.child_output.absolute()))
    need(options.phase and options.repo and options.manifest and options.out_base, "required arguments missing")
    need(options.repo.absolute() == ROOT, "--repo must be this recorder's repository")
    manifest_path = no_symlink_ancestry(options.manifest.absolute())
    manifest_raw = manifest_path.read_bytes()
    manifest = json.loads(manifest_raw)
    need(manifest.get("schema") == "agentorange.source-manifest/v1", "manifest schema")
    need(Path(manifest["repo"]) == ROOT and Path(manifest["write_root"]) == ROOT.parent, "manifest repository")
    need(manifest_path.parent == ROOT.parent, "manifest outside write root")
    base = manifest["head"]
    need(re.fullmatch(r"[0-9a-f]{40}", base) is not None, "manifest head must be a full commit")
    need(git(["rev-parse", base + "^{tree}"]).decode().strip() == manifest["tree"], "manifest tree mismatch")
    need(git(["merge-base", base, "HEAD"]).decode().strip() == base, "candidate is not a descendant of base")
    runtime = manifest["runtime"]
    venv = ROOT / ".venv"
    need(venv.is_symlink() and venv.resolve() == Path(runtime["repo_venv"]).resolve(), "manifest venv link")
    expected_python = runtime["credential_scan_python3"] if options.phase == "credentials" else str(venv / "bin" / "python3")
    need(Path(sys.executable) == Path(expected_python), "interpreter spelling differs from row declaration")
    need(Path(sys.prefix).resolve() == Path(runtime["repo_venv"]).resolve(), "interpreter environment")
    for path in AUTHORED + PINNED + CHECKERS:
        no_symlink_ancestry(ROOT / path)
    out_base = no_symlink_ancestry(options.out_base.absolute())
    need(out_base == ROOT.parent / "out", "output base must be the write root out directory")
    out_base.mkdir(exist_ok=True)
    row = out_base / (options.phase + "-" + uuid.uuid4().hex)
    row.mkdir()
    start = identity(base)
    receipt = {"card": "FC16-DETECTOR-MANIFEST-BALANCE", "phase": options.phase, "argv": list(sys.argv),
               "cwd": str(Path.cwd()), "interpreter": sys.executable, "prefix": sys.prefix,
               "source_manifest": str(manifest_path), "source_manifest_sha256": sha(manifest_raw),
               "base": base, "base_tree": manifest["tree"], "identity": start, "children": [],
               "author": "claude-opus-5-5 / anthropic", "review_required": True,
               "full_discovery": "operator-run (owed on the admitted execution surface)"}
    write_json(row / "started.json", receipt)
    (row / "candidate.diff").write_bytes(git(["diff", "--binary", base, "HEAD"]))
    (row / "source-manifest.json").write_bytes(manifest_raw)

    def run(label, argv, cwd=ROOT, allowed=(0,), env=None):
        argv = [str(item) for item in argv]
        done = subprocess.run(argv, cwd=str(cwd), capture_output=True, shell=False, timeout=900, env=env)
        (row / (label + ".stdout")).write_bytes(done.stdout)
        (row / (label + ".stderr")).write_bytes(done.stderr)
        receipt["children"].append({"label": label, "argv": argv, "cwd": str(cwd), "exit": done.returncode,
                                    "stdout_sha256": sha(done.stdout), "stderr_sha256": sha(done.stderr),
                                    "env_overrides": sorted(k for k in (env or {}) if os.environ.get(k) != env[k])})
        write_json(row / "progress.json", receipt)
        need(done.returncode in allowed, "child exit " + str(done.returncode) + ": " + label)
        return done

    def child_env(mode):
        env = dict(os.environ)
        for key in CHILD_ENV_KEYS:
            env[key] = str(row / ("tmp-" + mode))
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return env

    try:
        need(set(start["changed_paths"]) <= set(AUTHORED), "candidate diff exceeds exclusive write set")
        need(set(start["changed_paths"]) == set(AUTHORED), "authored files are not all committed")
        for path in PINNED + CHECKERS:
            need((ROOT / path).read_bytes() == git(["show", base + ":" + path]), "pinned source drift: " + path)
        need(start["head"] != base and start["status"] == "", "final rows require a committed clean candidate")
        need(git(["log", "-1", "--format=%B", "HEAD"]).decode().count("Signed-off-by:") >= 1, "HEAD not signed off")

        if options.phase == "suite":
            run("suite", [sys.executable, __file__, "--child", "suite", "--child-output", row], env=child_env("suite"))
            records = json.loads((row / "tests-suite.json").read_text())
            receipt["test_results"] = records
            receipt["case_to_test"] = CASE_TESTS
            receipt["subcases_per_case"] = validate_card_matrix(records, TEST_MODULE + ".")
            need(records["ambient_unchanged"], "card suite changed ambient defaults")
            # Test-only card: product bytes equal the base, so the run above is also the
            # green baseline. Isolated synthetic mutants live in test_deciding_negative_control.
            receipt["baseline"] = {"kind": "test-only green baseline", "product_equals_base": True,
                                   "mutants": "in-memory manifest copies in test_deciding_negative_control"}
        elif options.phase == "compatibility":
            clone = row / "base-clone"
            run("base-clone", ["git", "clone", "--local", "--no-hardlinks", "--no-checkout",
                               "--no-recurse-submodules", ROOT, clone])
            run("base-checkout", ["git", "checkout", "--detach", base], cwd=clone)
            need(git(["rev-parse", "HEAD"], cwd=clone).decode().strip() == base, "base clone head")
            selector_argv = [sys.executable, "-m", "unittest", "-v", *SELECTORS]
            (row / "tmp-original").mkdir()
            (row / "tmp-final").mkdir()
            original = run("original", selector_argv, cwd=clone, env=child_env("original"))
            final = run("final", selector_argv, cwd=ROOT, env=child_env("final"))
            original_lines, final_lines = unittest_lines(original.stderr), unittest_lines(final.stderr)
            deltas = {k: [original_lines.get(k), final_lines.get(k)]
                      for k in sorted(set(original_lines) | set(final_lines))
                      if original_lines.get(k) != final_lines.get(k)}
            receipt["original_vs_final"] = {"original_tests": len(original_lines), "final_tests": len(final_lines),
                                            "deltas": deltas}
            need(original_lines and not deltas, "selector behavior delta against base")
            need(all(v == "ok" for v in final_lines.values()), "selector not ok")
            run("compatibility", [sys.executable, __file__, "--child", "compatibility", "--child-output", row],
                env=child_env("compatibility"))
            records = json.loads((row / "tests-compatibility.json").read_text())
            receipt["test_results"] = records
            need(records["successful"] and records["skipped"] == 0, "selector failures or skips")
            need(records["tests_run"] == len(final_lines), "selector count differs between runners")
            need(records["ambient_unchanged"], "selectors changed ambient defaults")
        elif options.phase == "portable":
            unrelated = row / "unrelated-cwd"
            unrelated.mkdir()
            temporary = row / "script-temporary-root"
            temporary.mkdir()
            run("absolute-script", [sys.executable, ROOT / TEST, "--temporary-root", temporary], cwd=unrelated,
                env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
            summary = json.loads((temporary / "fc16-script-summary.json").read_text())
            receipt["script_results"] = summary
            receipt["script_subcases_per_case"] = validate_card_matrix(summary, "__main__.")
            need(summary["tests_run"] == len(CASE_TESTS) and summary["successful"], "script matrix incomplete")
            run("peer-discovery", [sys.executable, __file__, "--child", "peer", "--child-output", row],
                cwd=unrelated, env=child_env("peer"))
            records = json.loads((row / "tests-peer.json").read_text())
            receipt["test_results"] = records
            receipt["peer_subcases_per_case"] = validate_card_matrix(records, TEST_MODULE + ".")
            peers = [i for i in records["test_ids"] if i.endswith("InertPeer.runTest")]
            need(len(peers) == 2 and records["tests_run"] == len(CASE_TESTS) + 2, "peer sentinels missing")
            need(records["ambient_unchanged"], "discovery changed ambient defaults")
            box_path = re.compile(r"/(?:home|Users|root|mnt|srv)/[A-Za-z0-9._-]+/")
            findings = {p: bool(box_path.search((ROOT / p).read_text())) for p in AUTHORED}
            receipt["machine_path_scan"] = findings
            need(not any(findings.values()), "machine-specific path literal in authored file")
        elif options.phase == "credentials":
            need(Path(sys.executable) == Path(runtime["credential_scan_python3"]), "credential interpreter")
            scanner = Path(runtime["detect_secrets"])
            need(scanner.is_file() and scanner.parent == Path(sys.executable).parent, "scanner runtime")
            scan = run("detect-secrets", [sys.executable, scanner, "scan", "--all-files", "--exclude-files", "^.git/",
                                          "--no-verify"])
            report = row / "detect-secrets-report.json"
            report.write_bytes(scan.stdout)
            parsed = json.loads(scan.stdout)
            need(isinstance(parsed.get("results"), dict), "scanner JSON shape")
            receipt["scanner_result_paths"] = sorted(parsed["results"])
            run("check-secret-report", [sys.executable, ROOT / "tools/check_secret_report.py", report])
        else:
            untracked = git(["ls-files", "--others", "--exclude-standard"]).decode().splitlines()
            need(untracked == [], "scan-visible untracked entries: " + repr(untracked))
            ignored = git(["ls-files", "--others", "--ignored", "--exclude-standard", "--directory"]).decode().splitlines()
            receipt["ignored_entries"] = ignored
            write_json(row / "path-inventory.json", git(["ls-files", "-z"]).decode().split("\0"))
            for path in AUTHORED:
                raw = (ROOT / path).read_bytes()
                need(git(["show", "HEAD:" + path]) == raw, "HEAD bytes differ: " + path)
                need(git(["show", ":" + path]) == raw, "index bytes differ: " + path)
            clone = row / "committed-tree"
            run("local-clone", ["git", "clone", "--local", "--no-hardlinks", "--no-checkout",
                                "--no-recurse-submodules", ROOT, clone])
            run("clone-checkout", ["git", "checkout", "--detach", start["head"]], cwd=clone)
            need(git(["rev-parse", "HEAD"], cwd=clone).decode().strip() == start["head"], "clone head")
            need(git(["rev-parse", "HEAD^{tree}"], cwd=clone).decode().strip() == start["tree"], "clone tree")
            need(not (clone / ".venv").exists() and not (clone / ".venv").is_symlink(), "runtime link in clone")
            for path in AUTHORED:
                need(sha((clone / path).read_bytes()) == start["authored_sha256"][path], "clone authored hash")
            for path in CHECKERS:
                need(sha((clone / path).read_bytes()) == start["checker_sha256"][path], "clone checker hash")
            committed = run("committed-tree-check", [sys.executable, clone / "tools/check_public_tree.py"], cwd=clone)
            report = json.loads(committed.stdout)
            need(report.get("findings") == [], "committed-tree findings")
            receipt["committed_tree_report"] = report
            in_place = run("in_place_informational", [sys.executable, ROOT / "tools/check_public_tree.py"],
                           allowed=(0, 1))
            findings = json.loads(in_place.stdout).get("findings")
            recognized = ((in_place.returncode == 0 and findings == []) or
                          (in_place.returncode == 1 and findings == [[".venv", 0, "symlink_not_scanned"]]))
            receipt["in_place_informational"] = {"exit": in_place.returncode, "findings": findings,
                                                 "incident": "INC-174", "recognized": recognized,
                                                 "note": "informational only; not an in-place pass"}
            need(recognized, "unrecognized in-place public-tree outcome")
        need(identity(base) == start, "candidate identity changed during the row")
        need(manifest_path.read_bytes() == manifest_raw, "source manifest changed during the row")
        receipt["exit"] = 0
    except Exception:
        receipt["exit"] = 1
        receipt["failure"] = traceback.format_exc()
    receipt["evidence_sha256"] = {p.name: sha(p.read_bytes()) for p in sorted(row.iterdir())
                                  if p.is_file() and p.name != "receipt.json"}
    write_json(row / "receipt.json", receipt)
    print(json.dumps({"phase": options.phase, "exit": receipt["exit"], "head": start["head"],
                      "row": str(row), "receipt_sha256": sha((row / "receipt.json").read_bytes())}))
    if receipt["exit"]:
        print(receipt["failure"], file=sys.stderr)
    return receipt["exit"]


if __name__ == "__main__":
    raise SystemExit(main())
