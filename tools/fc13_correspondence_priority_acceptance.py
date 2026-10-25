"""FC13-CORRESPONDENCE-PRIORITY acceptance runner (five separate phases).

Usage (one phase per invocation):
    python3 tools/fc13_correspondence_priority_acceptance.py --phase PHASE \
        --source-manifest SOURCE-MANIFEST.json --out-base OUT_BASE

PHASE is one of suite, compatibility, portable, credentials, public-tree.
Every invocation creates a fresh unique directory below OUT_BASE (which must be
outside the repository), never overwrites earlier evidence, and seals an
evidence manifest only after every child process has exited. The repository is
derived from this file's location; nothing here embeds host-specific paths.
Machine-valid output is REVIEW REQUIRED, not semantic acceptance.

Children run with shell disabled. Test children re-enter this file with the
internal ``--child`` mode, which redirects temporary storage and installs an
offline socket sentinel before any product import.
"""
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
import tarfile
import time
import unittest
import uuid

REPO = Path(__file__).resolve().parents[1]
RUNNER_REL = "tools/fc13_correspondence_priority_acceptance.py"
TEST_REL = "tests/records/test_fc13_correspondence_priority.py"
FIXTURE_REL = "tests/fixtures/fc13-correspondence-priority/cases.json"
AUTHORED = (FIXTURE_REL, TEST_REL, RUNNER_REL)
SOURCE_PINS = ("campaign_tool/records/understanding/correspondence.py",
               "tests/records/test_understanding.py",
               "tests/records/test_correspondence.py")
SCANNER_FILES = ("tools/check_public_tree.py", "campaign_tool/records/public_scan.py",
                 "tools/check_secret_report.py", ".gitignore")
TEST_MODULE = "tests.records.test_fc13_correspondence_priority"
COMPAT_SELECTOR = "tests.records.test_understanding.CorrespondenceTests"
COMPAT_TESTS = ("CorrespondenceTests.test_bundle_mode_requires_citations",
                "CorrespondenceTests.test_tracks_explicit_events_and_never_sends")
REQUIRED_TESTS = (
    "ClassifyPriorityTests.test_boundary_near_misses_are_other",
    "ClassifyPriorityTests.test_case_variants_keep_their_label",
    "ClassifyPriorityTests.test_event_type_versus_kind_precedence",
    "ClassifyPriorityTests.test_neutral_and_non_string_text_is_other",
    "ClassifyPriorityTests.test_pair_priority_in_both_text_orders",
    "ClassifyPriorityTests.test_recognized_explicit_type_beats_competing_text",
    "ClassifyPriorityTests.test_single_category_positives",
    "ClassifyPriorityTests.test_unknown_explicit_type_falls_back_to_text",
    "FrozenContractTests.test_each_phrase_hits_only_its_own_marker",
    "FrozenContractTests.test_fixture_matrix_is_complete_and_unique",
    "FrozenContractTests.test_marker_order_matches_frozen_brief",
    "NegativeControlTests.test_reversed_marker_sequence_fails_selected_pair_assertion",
    "TrackConsumerTests.test_track_timeline_event_type_matches_frozen_label",
)
PAIR_TEST = "ClassifyPriorityTests.test_pair_priority_in_both_text_orders"
TRACK_TEST = "TrackConsumerTests.test_track_timeline_event_type_matches_frozen_label"
MUTANT_KILLED = (PAIR_TEST, TRACK_TEST)
MUTANT_SURVIVORS = ("ClassifyPriorityTests.test_recognized_explicit_type_beats_competing_text",
                    "ClassifyPriorityTests.test_event_type_versus_kind_precedence",
                    "ClassifyPriorityTests.test_single_category_positives")
PHASES = ("suite", "compatibility", "portable", "credentials", "public-tree")
SENTINEL_BEFORE = "AmbientSentinelBefore.test_ambient_unchanged_after_discovery_import"
SENTINEL_AFTER = "AmbientSentinelAfter.test_ambient_and_markers_unchanged_after_suite"
# Assembled from pieces so this file never matches its own host-path scan.
HOST_PATH_TOKENS = ("/" + "home/", "/" + "Users/", "/" + "tmp/", "/" + "root/", "agent" + "-work",
                    "." + "hermes", "venv" + "-fullsuite", "candidates" + "/deflock", "C:" + "\\")
STEP_TIMEOUT = 1800


class AcceptanceFailure(Exception):
    pass


def require(condition, message):
    if not condition:
        raise AcceptanceFailure(message)


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    return sha256_bytes(Path(path).read_bytes())


def strict_json(text):
    def hook(pairs):
        seen = {}
        for key, value in pairs:
            if key in seen:
                raise ValueError("duplicate json key: " + key)
            seen[key] = value
        return seen
    return json.loads(text, object_pairs_hook=hook)


def utc_stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


# ---------------------------------------------------------------- child mode
class RecordingResult(unittest.TextTestResult):
    """Records every test outcome and every failing subtest with its message."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records = {}
        self.order = []
        self.subtest_failures = []

    def _set(self, test, outcome):
        key = test.id()
        if key not in self.records:
            self.order.append(key)
            self.records[key] = outcome
        elif self.records[key] in ("running", "ok"):
            self.records[key] = outcome

    def startTest(self, test):
        self._set(test, "running")
        super().startTest(test)

    def addSuccess(self, test):
        self._set(test, "ok")
        super().addSuccess(test)

    def addFailure(self, test, err):
        self._set(test, "failed")
        super().addFailure(test, err)

    def addError(self, test, err):
        self._set(test, "error")
        super().addError(test, err)

    def addSkip(self, test, reason):
        self._set(test, "skipped")
        super().addSkip(test, reason)

    def addExpectedFailure(self, test, err):
        self._set(test, "expected_failure")
        super().addExpectedFailure(test, err)

    def addUnexpectedSuccess(self, test):
        self._set(test, "unexpected_success")
        super().addUnexpectedSuccess(test)

    def addSubTest(self, test, subtest, err):
        if err is not None:
            kind = "failed" if issubclass(err[0], test.failureException) else "error"
            self._set(test, kind)
            self.subtest_failures.append({
                "test": test.id(), "subtest": subtest.id(), "kind": kind,
                "exception": err[0].__name__, "message": str(err[1])})
        super().addSubTest(test, subtest, err)


def _child_bootstrap(tmp_root, code_root):
    import socket
    import tempfile

    os.environ["TMPDIR"] = str(tmp_root)
    tempfile.tempdir = str(tmp_root)

    def offline(*_args, **_kwargs):
        raise OSError("fc13 offline sentinel: network access is not permitted")

    socket.socket.connect = offline
    socket.socket.connect_ex = offline
    socket.create_connection = offline
    socket.getaddrinfo = offline
    sys.path.insert(0, str(code_root))


def _ambient_snapshot():
    import socket
    import tempfile

    mask = os.umask(0o022)
    os.umask(mask)
    return {"environ": dict(os.environ), "tempdir": tempfile.tempdir, "cwd": os.getcwd(),
            "umask": mask, "recursion": sys.getrecursionlimit(),
            "socket": [id(socket.socket.connect), id(socket.socket.connect_ex),
                       id(socket.create_connection), id(socket.getaddrinfo)],
            "meta_path": [repr(item) for item in sys.meta_path],
            "path_hooks": [repr(item) for item in sys.path_hooks]}


def _diff_snapshot(before, after):
    changed = [key for key in before if before[key] != after[key]]
    env_delta = sorted(set(before["environ"].items()) ^ set(after["environ"].items()))
    return changed, [name for name, _ in env_delta]


def _sentinel_classes(before, markers_holder):
    class AmbientSentinelBefore(unittest.TestCase):
        def test_ambient_unchanged_after_discovery_import(self):
            changed, env_keys = _diff_snapshot(before, _ambient_snapshot())
            self.assertEqual(changed, [], f"discovery import changed {changed} env={env_keys}")

    class AmbientSentinelAfter(unittest.TestCase):
        def test_ambient_and_markers_unchanged_after_suite(self):
            changed, env_keys = _diff_snapshot(before, _ambient_snapshot())
            self.assertEqual(changed, [], f"suite changed {changed} env={env_keys}")
            module = sys.modules["campaign_tool.records.understanding.correspondence"]
            self.assertIs(module.MARKERS, markers_holder["markers"])

    return AmbientSentinelBefore, AmbientSentinelAfter


def child_main(argv):
    parser = argparse.ArgumentParser(prog="fc13-child")
    parser.add_argument("--child", choices=("unittest", "mutant", "discovery"), required=True)
    parser.add_argument("--code-root", type=Path, required=True)
    parser.add_argument("--tmp-root", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("selectors", nargs="*")
    args = parser.parse_args(argv)
    if not args.tmp_root.is_absolute() or not args.tmp_root.is_dir():
        raise SystemExit("child tmp root must be an existing absolute directory")
    _child_bootstrap(args.tmp_root, args.code_root)
    record = {"mode": args.child, "code_root": str(args.code_root), "selectors": args.selectors}
    loader = unittest.TestLoader()
    if args.child == "discovery":
        # The sentinel snapshot is retaken after this child's own bootstrap so
        # only discovery/import/run effects of the new module are compared.
        before = _ambient_snapshot()
        discovered = loader.discover(start_dir=str(args.code_root / "tests" / "records"),
                                     pattern=Path(TEST_REL).name,
                                     top_level_dir=str(args.code_root))
        module = sys.modules["campaign_tool.records.understanding.correspondence"]
        holder = {"markers": module.MARKERS}
        first, last = _sentinel_classes(before, holder)
        suite = unittest.TestSuite([loader.loadTestsFromTestCase(first), discovered,
                                    loader.loadTestsFromTestCase(last)])
    else:
        suite = loader.loadTestsFromNames(args.selectors)
    from campaign_tool.records.understanding import correspondence
    record["product_file"] = str(Path(correspondence.__file__).resolve())
    if args.child == "mutant":
        correspondence.MARKERS = tuple(reversed(correspondence.MARKERS))
    record["markers_in_effect"] = [label for label, _ in correspondence.MARKERS]
    runner = unittest.TextTestRunner(stream=sys.stderr, verbosity=2, resultclass=RecordingResult)
    result = runner.run(suite)
    record.update({
        "tests_run": result.testsRun,
        "tests": [{"id": key, "outcome": result.records[key]} for key in result.order],
        "subtest_failures": result.subtest_failures,
        "failures": len(result.failures), "errors": len(result.errors),
        "skipped": len(result.skipped), "expected_failures": len(result.expectedFailures),
        "unexpected_successes": len(result.unexpectedSuccesses),
        "was_successful": result.wasSuccessful()})
    args.result.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0 if result.wasSuccessful() else 1


# --------------------------------------------------------------- parent mode
class Run:
    def __init__(self, phase, manifest_path, out_base):
        self.phase = phase
        self.manifest_path = manifest_path
        self.out_base = out_base
        self.dir = out_base / f"{phase}-{utc_stamp()}-{uuid.uuid4().hex}"
        self.dir.mkdir(parents=False, exist_ok=False)
        self.evidence = self.dir / "evidence"
        self.work = self.dir / "work"
        self.tmp = self.work / "tmp"
        for path in (self.evidence, self.work, self.tmp):
            path.mkdir(exist_ok=False)
        import tempfile
        tempfile.tempdir = str(self.tmp)
        self.steps = []
        self.checks = []
        self.receipt = {"schema": "fc13-correspondence-priority-receipt/v1", "phase": phase,
                        "argv": list(sys.argv), "cwd": os.getcwd(),
                        "interpreter": sys.executable, "interpreter_prefix": sys.prefix,
                        "python_version": sys.version, "repo": str(REPO),
                        "run_dir": str(self.dir), "started_at": utc_stamp(),
                        "review_status": "REVIEW REQUIRED - no semantic acceptance"}

    def check(self, name, condition, detail=None):
        self.checks.append({"check": name, "ok": bool(condition), "detail": detail})
        require(condition, f"{name}: {detail}")

    def step(self, name, argv, cwd, stdout_name=None):
        env = dict(os.environ)
        for key in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONINSPECT"):
            env.pop(key, None)
        env["TMPDIR"] = str(self.tmp)
        started = time.monotonic()
        proc = subprocess.run([str(item) for item in argv], cwd=str(cwd), env=env, shell=False,
                              stdin=subprocess.DEVNULL, capture_output=True, timeout=STEP_TIMEOUT)
        out_path = self.evidence / (stdout_name or f"{name}.stdout")
        err_path = self.evidence / f"{name}.stderr"
        require(not out_path.exists() and not err_path.exists(), "evidence name reused: " + name)
        out_path.write_bytes(proc.stdout)
        err_path.write_bytes(proc.stderr)
        entry = {"name": name, "argv": [str(item) for item in argv], "cwd": str(cwd),
                 "shell": False, "exit": proc.returncode,
                 "seconds": round(time.monotonic() - started, 3),
                 "stdout": out_path.name, "stdout_sha256": sha256_bytes(proc.stdout),
                 "stdout_bytes": len(proc.stdout), "stderr": err_path.name,
                 "stderr_sha256": sha256_bytes(proc.stderr), "stderr_bytes": len(proc.stderr)}
        self.steps.append(entry)
        return proc, entry

    def git(self, name, *args, cwd=None, ok_codes=(0,)):
        proc, entry = self.step(name, ["git", *args], cwd or REPO)
        self.check(f"{name} exit", proc.returncode in ok_codes, proc.returncode)
        return proc

    def child(self, name, mode, selectors, code_root, cwd):
        result_path = self.evidence / f"{name}.result.json"
        argv = [sys.executable, REPO / RUNNER_REL, "--child", mode, "--code-root", code_root,
                "--tmp-root", self.tmp, "--result", result_path, *selectors]
        proc, entry = self.step(name, argv, cwd)
        self.check(f"{name} result written", result_path.is_file(), str(result_path))
        result = strict_json(result_path.read_text(encoding="utf-8"))
        entry["result"] = result_path.name
        entry["result_sha256"] = sha256_file(result_path)
        return proc, result

    def seal(self, verdict, error=None):
        self.receipt.update({"steps": self.steps, "checks": self.checks, "verdict": verdict,
                             "error": error, "finished_at": utc_stamp()})
        receipt_path = self.evidence / "receipt.json"
        receipt_path.write_text(json.dumps(self.receipt, indent=2, sort_keys=True) + "\n",
                                encoding="utf-8")
        entries = []
        for path in sorted(self.evidence.rglob("*")):
            if path.is_file() and not path.is_symlink():
                entries.append({"path": path.relative_to(self.dir).as_posix(),
                                "sha256": sha256_file(path), "bytes": path.stat().st_size})
        manifest = {"schema": "fc13-correspondence-priority-evidence/v1", "phase": self.phase,
                    "verdict": verdict, "sealed_at": utc_stamp(), "files": entries,
                    "identity": self.receipt.get("identity")}
        manifest_path = self.dir / "evidence-manifest.json"
        data = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
        manifest_path.write_bytes(data)
        digest = sha256_bytes(data)
        (self.dir / "evidence-manifest.sha256").write_text(
            f"{digest}  evidence-manifest.json\n", encoding="utf-8")
        for path in [*self.evidence.rglob("*"), manifest_path,
                     self.dir / "evidence-manifest.sha256"]:
            if path.is_file() and not path.is_symlink():
                path.chmod(0o444)
        return digest


def load_manifest(run):
    raw = run.manifest_path.read_text(encoding="utf-8")
    manifest = strict_json(raw)
    run.receipt["source_manifest"] = {"path": str(run.manifest_path), "sha256": sha256_bytes(
        raw.encode("utf-8"))}
    run.check("manifest schema", manifest.get("schema") == "agentorange.source-manifest/v1",
              manifest.get("schema"))
    run.check("manifest repo is this repo", Path(manifest.get("repo", "")).resolve() == REPO,
              manifest.get("repo"))
    run.check("manifest write_root contains repo",
              REPO.parent == Path(manifest.get("write_root", "")).resolve(),
              manifest.get("write_root"))
    head = manifest.get("head", "")
    run.check("manifest head shape", bool(re.fullmatch(r"[0-9a-f]{40}", head)), head)
    run.check("manifest tree shape", bool(re.fullmatch(r"[0-9a-f]{40}", manifest.get("tree", ""))),
              manifest.get("tree"))
    runtime = manifest.get("runtime", {})
    for key in ("repo_venv", "credential_scan_python3", "detect_secrets"):
        value = runtime.get(key)
        run.check(f"runtime {key} absolute and present",
                  isinstance(value, str) and Path(value).is_absolute() and Path(value).exists(),
                  value)
    return manifest


def check_interpreter(run, manifest):
    runtime = manifest["runtime"]
    if run.phase == "credentials":
        expected = Path(runtime["credential_scan_python3"])
        run.check("credentials interpreter matches manifest credential_scan_python3",
                  Path(sys.executable) == expected, sys.executable)
    else:
        venv = REPO / ".venv"
        run.check(".venv is a symlink", venv.is_symlink(), str(venv))
        run.check(".venv resolves to manifest repo_venv",
                  venv.resolve() == Path(runtime["repo_venv"]).resolve(), str(venv.resolve()))
        run.check("staged repo interpreter", Path(sys.executable) == venv / "bin" / "python3",
                  sys.executable)
    run.check("interpreter prefix is manifest repo_venv",
              Path(sys.prefix).resolve() == Path(runtime["repo_venv"]).resolve(), sys.prefix)


def candidate_identity(run, manifest):
    base = manifest["head"]
    head = run.git("git-rev-parse-head", "rev-parse", "HEAD").stdout.decode().strip()
    tree = run.git("git-rev-parse-tree", "rev-parse", "HEAD^{tree}").stdout.decode().strip()
    base_tree = run.git("git-rev-parse-base-tree", "rev-parse",
                        base + "^{tree}").stdout.decode().strip()
    run.check("manifest tree is base tree", base_tree == manifest["tree"], base_tree)
    run.check("candidate HEAD is a descendant commit", head != base, head)
    run.git("git-base-is-ancestor", "merge-base", "--is-ancestor", base, "HEAD")
    changed = run.git("git-diff-names", "diff", "--name-only", base, "HEAD").stdout.decode()
    run.check("diff is exactly the exclusive write set",
              sorted(changed.split()) == sorted(AUTHORED), changed.split())
    run.git("git-source-pins-unchanged", "diff", "--quiet", base, "HEAD", "--",
            *SOURCE_PINS, *SCANNER_FILES)
    status = run.git("git-status", "status", "--porcelain", "--untracked-files=all").stdout.decode()
    lines = [line for line in status.splitlines() if line]
    run.check("worktree clean apart from manifest .venv link",
              all(line == "?? .venv" for line in lines), lines)
    run.git("git-diff-worktree-quiet", "diff", "--quiet")
    run.git("git-diff-index-quiet", "diff", "--cached", "--quiet")
    log = run.git("git-log-bodies", "log", "--format=%H%x00%B%x01", base + "..HEAD").stdout.decode()
    commits = [chunk.strip() for chunk in log.split("\x01") if chunk.strip()]
    run.check("candidate commits exist", bool(commits), len(commits))
    for chunk in commits:
        sha, _, body = chunk.partition("\x00")
        run.check(f"commit {sha} signed off", "Signed-off-by:" in body, sha)
    identity = {"base": base, "base_tree": base_tree, "head": head, "tree": tree,
                "commits": [chunk.partition("\x00")[0] for chunk in commits],
                "authored_sha256": {rel: sha256_file(REPO / rel) for rel in AUTHORED},
                "source_pin_sha256": {rel: sha256_file(REPO / rel) for rel in SOURCE_PINS},
                "scanner_sha256": {rel: sha256_file(REPO / rel) for rel in SCANNER_FILES}}
    run.receipt["identity"] = identity
    return identity


def fixture_dimensions():
    data = strict_json((REPO / FIXTURE_REL).read_text(encoding="utf-8"))
    return {"schema": data["schema"], "expected_counts": data["expected_counts"],
            "marker_order": data["marker_order"], "explicit_types": data["explicit_types"],
            "negative_control": data["negative_control"],
            "pair_ids": [case["id"] for case in data["pairs"]],
            "case_ids": {group: [case["id"] for case in data[group]]
                         for group in data["expected_counts"] if group != "total"}}


def short_ids(result):
    return [{"id": item["id"].split(".", item["id"].count(".") - 1)[-1], "outcome": item["outcome"]}
            for item in result["tests"]]


def require_clean_result(run, name, result, expected_ids):
    ids = short_ids(result)
    run.check(f"{name} test ids", sorted(item["id"] for item in ids) == sorted(expected_ids),
              [item["id"] for item in ids])
    run.check(f"{name} all ok", all(item["outcome"] == "ok" for item in ids), ids)
    run.check(f"{name} tests_run", result["tests_run"] == len(expected_ids), result["tests_run"])
    for key in ("failures", "errors", "skipped", "expected_failures", "unexpected_successes"):
        run.check(f"{name} {key} zero", result[key] == 0, result[key])
    run.check(f"{name} no subtest failures", result["subtest_failures"] == [],
              len(result["subtest_failures"]))
    run.check(f"{name} successful", result["was_successful"] is True, result["was_successful"])


def phase_suite(run, identity):
    dims = fixture_dimensions()
    run.receipt["case_dimensions"] = dims
    proc, result = run.child("suite", "unittest", [TEST_MODULE], REPO, REPO)
    run.check("suite exit zero", proc.returncode == 0, proc.returncode)
    require_clean_result(run, "suite", result, REQUIRED_TESTS)
    run.check("suite product file is candidate", result["product_file"] == str(
        (REPO / SOURCE_PINS[0]).resolve()), result["product_file"])

    selectors = [TEST_MODULE + "." + name for name in (*MUTANT_KILLED, *MUTANT_SURVIVORS)]
    proc, mutant = run.child("negative-control-mutant", "mutant", selectors, REPO, REPO)
    control = dims["negative_control"]
    run.check("mutant markers reversed in memory",
              mutant["markers_in_effect"] == list(reversed(dims["marker_order"])),
              mutant["markers_in_effect"])
    run.check("mutant exit one", proc.returncode == 1, proc.returncode)
    outcomes = {item["id"]: item["outcome"] for item in short_ids(mutant)}
    run.check("mutant killed exactly the deciding tests",
              {key for key, value in outcomes.items() if value != "ok"} == set(MUTANT_KILLED),
              outcomes)
    run.check("mutant killed tests failed by assertion (not error)",
              all(outcomes[name] == "failed" for name in MUTANT_KILLED), outcomes)
    run.check("mutant explicit-type and single positives survive",
              all(outcomes.get(name) == "ok" for name in MUTANT_SURVIVORS), outcomes)
    run.check("mutant no errors", mutant["errors"] == 0, mutant["errors"])
    for killed in MUTANT_KILLED:
        rows = [row for row in mutant["subtest_failures"] if row["test"].endswith(killed)]
        found = [re.search(r"case='([^']+)'", row["subtest"]) for row in rows]
        run.check(f"mutant {killed} subtests carry case ids", all(found), len(found))
        failed_ids = sorted(match.group(1) for match in found if match)
        expected_ids = list(dims["pair_ids"])
        if killed == TRACK_TEST:
            expected_ids += control["track_flips_beyond_pairs"]
        run.check(f"mutant {killed} fails exactly the declared multi-marker cases",
                  failed_ids == sorted(expected_ids), len(failed_ids))
        run.check(f"mutant {killed} failures are AssertionError",
                  all(row["kind"] == "failed" and row["exception"] == "AssertionError"
                      for row in rows), len(rows))
        marker = (f"{control['selected_case']}: expected={control['expected']!r} "
                  f"actual={control['mutant_label']!r}")
        selected = [row for row in rows if f"case='{control['selected_case']}'" in row["subtest"]]
        run.check(f"mutant {killed} selected pair returns wrong label at exact assertion",
                  len(selected) == 1 and marker in selected[0]["message"], marker)

    proc, restored = run.child("negative-control-restored", "unittest", selectors, REPO, REPO)
    run.check("restored exit zero", proc.returncode == 0, proc.returncode)
    require_clean_result(run, "restored", restored, (*MUTANT_KILLED, *MUTANT_SURVIVORS))
    run.check("restored markers are frozen order",
              restored["markers_in_effect"] == dims["marker_order"], restored["markers_in_effect"])


def extract_base(run, base, target, paths):
    proc, entry = run.step("git-archive-base", ["git", "archive", "--format=tar", base, *paths],
                           REPO, stdout_name="git-archive-base.tar")
    run.check("git archive exit", proc.returncode == 0, proc.returncode)
    target.mkdir(exist_ok=False)
    with tarfile.open(fileobj=io.BytesIO(proc.stdout), mode="r:") as archive:
        archive.extractall(target, filter="data")
    for rel in SOURCE_PINS[:2]:
        run.check(f"baseline {rel} equals candidate bytes",
                  sha256_file(target / rel) == sha256_file(REPO / rel), rel)


def phase_compatibility(run, identity):
    baseline = run.work / "baseline-tree"
    extract_base(run, identity["base"], baseline,
                 ["campaign_tool", "tests/__init__.py", "tests/records/__init__.py",
                  "tests/records/test_understanding.py"])
    proc, base_result = run.child("compat-baseline", "unittest", [COMPAT_SELECTOR], baseline,
                                  baseline)
    run.check("baseline exit zero", proc.returncode == 0, proc.returncode)
    require_clean_result(run, "baseline", base_result, COMPAT_TESTS)
    run.check("baseline product file is baseline tree",
              Path(base_result["product_file"]).is_relative_to(baseline.resolve()),
              base_result["product_file"])
    proc, final = run.child("compat-final", "unittest", [COMPAT_SELECTOR], REPO, REPO)
    run.check("final exit zero", proc.returncode == 0, proc.returncode)
    require_clean_result(run, "final", final, COMPAT_TESTS)
    run.check("final product file is candidate", Path(final["product_file"]).is_relative_to(REPO),
              final["product_file"])
    run.check("baseline and final outcomes identical", short_ids(base_result) == short_ids(final),
              short_ids(final))
    run.receipt["compatibility"] = {
        "selectors": [COMPAT_SELECTOR], "intentional_fixture_or_behavior_deltas": [],
        "owed_to_shipping_operator": ["mandatory full discovery", "supported execution surface",
                                      "exact-head required CI"],
        "note": "Scoped local compatibility only; not full repository CI and no push authority."}


def phase_portable(run, identity):
    unrelated = run.work / "unrelated-cwd"
    unrelated.mkdir(exist_ok=False)
    proc, _ = run.step("portable-script", [sys.executable, REPO / TEST_REL, "--tmp-root", run.tmp],
                       unrelated)
    stderr = proc.stderr.decode("utf-8", "replace")
    run.check("portable script exit zero", proc.returncode == 0, proc.returncode)
    ran = re.findall(r"^test_\w+ \(__main__\.(\w+\.test_\w+)\) \.\.\. ok$", stderr, re.M)
    run.check("portable script ran full matrix", sorted(ran) == sorted(REQUIRED_TESTS), ran)
    run.check("portable script summary", f"Ran {len(REQUIRED_TESTS)} tests" in stderr
              and stderr.rstrip().endswith("OK"), stderr[-200:])
    run.check("portable script left unrelated cwd empty", list(unrelated.iterdir()) == [],
              [p.name for p in unrelated.iterdir()])

    proc, _ = run.step("portable-script-missing-tmp-root", [sys.executable, REPO / TEST_REL],
                       unrelated)
    run.check("script mode without --tmp-root refuses", proc.returncode != 0
              and b"requires --tmp-root" in proc.stderr, proc.returncode)

    discovery_cwd = run.work / "discovery-cwd"
    discovery_cwd.mkdir(exist_ok=False)
    proc, result = run.child("portable-discovery", "discovery", [], REPO, discovery_cwd)
    run.check("discovery exit zero", proc.returncode == 0, proc.returncode)
    ids = [item["id"] for item in short_ids(result)]
    require_clean_result(run, "discovery", result, (SENTINEL_BEFORE, *REQUIRED_TESTS,
                                                    SENTINEL_AFTER))
    run.check("sentinels run before and after the module",
              ids[0] == SENTINEL_BEFORE and ids[-1] == SENTINEL_AFTER, ids)

    text = (REPO / TEST_REL).read_text(encoding="utf-8")
    bootstrap_at = text.find("\n    _script_bootstrap()\n")
    import_at = text.find("\nfrom campaign_tool")
    run.check("script bootstrap precedes product import",
              0 <= bootstrap_at < import_at, [bootstrap_at, import_at])
    findings = {}
    for rel in AUTHORED:
        data = (REPO / rel).read_text(encoding="utf-8")
        hits = [token for token in HOST_PATH_TOKENS if token in data]
        if "addaudithook" in data and rel != RUNNER_REL:
            hits.append("addaudithook")
        findings[rel] = hits
    run.receipt["host_path_scan"] = {"tokens": list(HOST_PATH_TOKENS), "findings": findings}
    run.check("authored bytes contain no host-specific paths",
              all(not hits for hits in findings.values()), findings)


def phase_credentials(run, identity, manifest):
    detector = manifest["runtime"]["detect_secrets"]
    report_name = "detect-secrets-report.json"
    proc, _ = run.step("detect-secrets", [detector, "scan", "--all-files", "--exclude-files",
                                          "^.git/", "--no-verify"], REPO, stdout_name=report_name)
    run.check("detect-secrets exit zero", proc.returncode == 0, proc.returncode)
    report = strict_json(proc.stdout.decode("utf-8"))
    run.check("detect-secrets report has results", isinstance(report.get("results"), dict),
              type(report.get("results")).__name__)
    run.receipt["detect_secrets"] = {"version": report.get("version"),
                                     "result_files": len(report["results"]),
                                     "authored_hits": {rel: report["results"].get(rel, [])
                                                       for rel in AUTHORED}}
    proc, _ = run.step("check-secret-report", [sys.executable, REPO / "tools" /
                                               "check_secret_report.py",
                                               run.evidence / report_name], REPO)
    run.check("check_secret_report exit zero", proc.returncode == 0, proc.returncode)


def phase_public_tree(run, identity):
    visible = run.git("git-scan-visible-untracked", "ls-files", "-z", "--others",
                      "--exclude-standard").stdout.decode()
    names = [name for name in visible.split("\0") if name]
    run.check("no untracked scan-visible file except .venv link",
              all(name == ".venv" for name in names), names)
    tracked = run.git("git-ls-files-stage", "ls-files", "-s").stdout.decode()
    for rel in AUTHORED:
        run.check(f"{rel} tracked", f"\t{rel}\n" in tracked, rel)
    clone = run.work / "committed-tree-clone"
    run.git("git-clone-local", "clone", "--local", "--no-hardlinks", "--no-checkout",
            "--no-recurse-submodules", REPO, clone, cwd=run.work)
    run.git("git-clone-checkout", "-C", clone, "checkout", "--detach", identity["head"],
            cwd=run.work)
    clone_head = run.git("git-clone-head", "-C", clone, "rev-parse", "HEAD",
                         cwd=run.work).stdout.decode().strip()
    clone_tree = run.git("git-clone-tree", "-C", clone, "rev-parse", "HEAD^{tree}",
                         cwd=run.work).stdout.decode().strip()
    run.check("clone HEAD equals candidate", clone_head == identity["head"], clone_head)
    run.check("clone tree equals candidate", clone_tree == identity["tree"], clone_tree)
    clone_status = run.git("git-clone-status", "-C", clone, "status", "--porcelain",
                           "--untracked-files=all", cwd=run.work).stdout.decode()
    run.check("clone clean", clone_status.strip() == "", clone_status)
    for rel in AUTHORED + SCANNER_FILES:
        run.check(f"clone {rel} bytes equal candidate",
                  sha256_file(clone / rel) == sha256_file(REPO / rel), rel)
    proc, _ = run.step("public-tree-committed-clone",
                       [sys.executable, clone / "tools" / "check_public_tree.py"], clone)
    run.check("committed-tree checker exit zero", proc.returncode == 0, proc.returncode)
    scan = strict_json(proc.stdout.decode("utf-8"))
    run.check("committed-tree findings empty", scan.get("findings") == [], scan.get("findings"))
    run.check("committed-tree scanned files", isinstance(scan.get("scanned_files"), int)
              and scan["scanned_files"] > 0, scan.get("scanned_files"))
    proc, _ = run.step("public-tree-in-place-informational",
                       [sys.executable, REPO / "tools" / "check_public_tree.py"], REPO)
    try:
        in_place = strict_json(proc.stdout.decode("utf-8"))
    except ValueError:
        in_place = None
    venv_ok = (REPO / ".venv").is_symlink()
    if proc.returncode == 0 and in_place is not None and in_place.get("findings") == []:
        outcome = "exit0_empty_findings"
    elif (proc.returncode == 1 and in_place is not None and venv_ok
          and in_place.get("findings") == [[".venv", 0, "symlink_not_scanned"]]):
        outcome = "exit1_only_manifest_venv_symlink_not_scanned"
    else:
        outcome = "unrecognized"
    run.receipt["in_place_informational"] = {"label": "in_place_informational (INC-174)",
                                             "exit": proc.returncode, "outcome": outcome,
                                             "findings": in_place and in_place.get("findings")}
    run.check("in-place informational outcome recognized", outcome != "unrecognized", outcome)
    after_head = run.git("git-recheck-head", "rev-parse", "HEAD").stdout.decode().strip()
    after_status = run.git("git-recheck-status", "status", "--porcelain",
                           "--untracked-files=all").stdout.decode()
    run.check("candidate HEAD unchanged after scans", after_head == identity["head"], after_head)
    run.check("candidate status unchanged after scans",
              all(line == "?? .venv" for line in after_status.splitlines() if line),
              after_status)


def parent_main(argv):
    parser = argparse.ArgumentParser(description="FC13 acceptance runner")
    parser.add_argument("--phase", choices=PHASES, required=True)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--out-base", type=Path, required=True)
    args = parser.parse_args(argv)
    out_base = args.out_base
    if not out_base.is_absolute() or not args.source_manifest.is_absolute():
        print(json.dumps({"error": "absolute --out-base and --source-manifest required"}))
        return 2
    resolved = out_base.resolve()
    if resolved == REPO or REPO in resolved.parents:
        print(json.dumps({"error": "out-base must be outside the repository"}))
        return 2
    out_base.mkdir(parents=True, exist_ok=True)
    run = Run(args.phase, args.source_manifest, out_base)
    verdict, error = "FAIL", None
    try:
        manifest = load_manifest(run)
        check_interpreter(run, manifest)
        identity = candidate_identity(run, manifest)
        if args.phase == "suite":
            phase_suite(run, identity)
        elif args.phase == "compatibility":
            phase_compatibility(run, identity)
        elif args.phase == "portable":
            phase_portable(run, identity)
        elif args.phase == "credentials":
            phase_credentials(run, identity, manifest)
        else:
            phase_public_tree(run, identity)
        verdict = "PASS"
    except (AcceptanceFailure, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        error = f"{type(exc).__name__}: {exc}"
    digest = run.seal(verdict, error)
    print(json.dumps({"phase": args.phase, "verdict": verdict, "error": error,
                      "run_dir": str(run.dir), "head": (run.receipt.get("identity") or {}).get("head"),
                      "checks": len(run.checks), "steps": len(run.steps),
                      "evidence_manifest_sha256": digest,
                      "review_status": "REVIEW REQUIRED - no semantic acceptance"}, indent=2))
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--child":
        raise SystemExit(child_main(sys.argv[1:]))
    raise SystemExit(parent_main(sys.argv[1:]))
