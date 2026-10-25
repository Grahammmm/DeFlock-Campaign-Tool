#!/usr/bin/env python3
"""FC9-DOCUMENT-CANDIDATE-COUNTS acceptance runner (machine checks only; REVIEW REQUIRED).

Five separately invoked phases: suite, compatibility, portable, credentials,
public-tree. Every invocation creates a new UUID directory beneath --out-base
and records exact argv/cwd/interpreter, exit, raw stdout/stderr, candidate
HEAD/tree, fixture and source hashes, test/case counts and artifact hashes.
tempfile.tempdir is redirected into that directory before any product import.
Children are literal argument lists with the shell disabled. The repository
is derived from this file; manifest and output paths are literal CLI
arguments. A pass is machine validity only, never semantic, legal, human or
live acceptance.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import hashlib
import importlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SPEC = "FC9-DOCUMENT-CANDIDATE-COUNTS acceptance runner v1"
PHASES = ("suite", "compatibility", "portable", "credentials", "public-tree")
MANIFEST_SCHEMA = "agentorange.source-manifest/v1"
SUITE_MODULE = "tests.records.test_fc9_document_candidate_counts"
SUITE_FILE = "tests/records/test_fc9_document_candidate_counts.py"
FIXTURE_FILE = "tests/fixtures/fc9-document-candidate-counts/cases.json"
RUNNER_FILE = "tools/fc9_document_candidate_counts_acceptance.py"
AUTHORED = (SUITE_FILE, FIXTURE_FILE, RUNNER_FILE)
COMPAT_SELECTORS = ("tests.records.test_understanding.DocumentTests", "tests.records.test_fc_doc_coverage")
# Read-only subjects pinned by the card brief; any byte change fails every phase.
PINNED_SOURCES = {
    "campaign_tool/records/understanding/document.py": "f3a48098d27ad71061781d2eaacfde803f3f1be108f2a4fce3085f12dacd9a27",
    "tests/records/test_understanding.py": "77fa084d176066327bc1795381a911671da7875f1f5721e164002616d6f5922a",
    "tests/records/test_fc_doc_coverage.py": "ceadbfa869eca6264a04077cf098260f5c2a8fb7dde9142c668052e8fb21a257",
}
CHECKER_FILES = ("tools/check_public_tree.py", "campaign_tool/records/public_scan.py", "tools/check_secret_report.py")
SENTINEL_TEST = "tests.records.test_understanding.EvidenceTests.test_receipt_normalizes_to_pinned_bundle"
CONTRACT_TESTS = ("test_frozen_caps_match_product_constants", "test_fixture_expectations_are_frozen_count_arithmetic",
                  "test_fixture_covers_every_cap_boundary", "test_malformed_specs_are_rejected",
                  "test_malformed_fixture_documents_are_rejected", "test_each_generated_unit_feeds_exactly_one_family",
                  "test_negative_control_rejects_zeroed_omissions")
# Frozen experiment behaviour -> named tests (case tests are test_case_<fixture id>).
BEHAVIOUR_MAP = {
    "zero/below/at/above for classifications": ["test_case_classifications_" + b for b in ("zero", "below", "at", "above")],
    "zero/below/at/above for entity candidates": ["test_case_entity_candidates_" + b for b in ("zero", "below", "at", "above")],
    "zero/below/at/above for date mentions": ["test_case_date_mentions_" + b for b in ("zero", "below", "at", "above")],
    "zero/below/at/above for summary facts": ["test_case_summary_facts_" + b for b in ("zero", "below", "at", "above")],
    "zero/below/at/above for cross-record keys (two originals)":
        ["test_case_cross_record_key_candidates_" + b for b in ("zero", "below", "at", "above")],
    "MAX_KEY_GROUPS and one above; key_occurrences_not_tracked":
        ["test_case_key_groups_at", "test_case_key_groups_above", "test_case_key_groups_above_repeated_untracked"],
    "MAX_SUMMARY_FACTS own cap despite other candidates": ["test_case_summary_facts_cap_amid_other_candidates"],
    "no counter silently resets (all families over, interleaved, two originals)": ["test_case_all_families_above_interleaved"],
    "frozen cap values unchanged": ["test_frozen_caps_match_product_constants"],
    "expected counts independent of analyze": ["test_fixture_expectations_are_frozen_count_arithmetic",
                                               "test_fixture_covers_every_cap_boundary"],
    "unambiguous single-family units": ["test_each_generated_unit_feeds_exactly_one_family"],
    "malformed case specs and fixture documents": ["test_malformed_specs_are_rejected",
                                                   "test_malformed_fixture_documents_are_rejected"],
    "declared synthetic negative control": ["test_negative_control_rejects_zeroed_omissions"],
}
CHILD_TIMEOUT = 1200
# Fragments are joined at run time so this file does not match its own scan.
HOST_FRAGMENTS = (("/ho", "me/"), ("agent", "-work"), ("venv-", "fullsuite"), (".her", "mes"), ("/t", "mp/"),
                  ("/U", "sers/"), ("opus-", "coder"), ("PYTHON", "PATH"), ("shell", "=True"), ("os.sys", "tem("),
                  ("bash", " -c"), ("<<", "EOF"))
_HEX40 = re.compile(r"[0-9a-f]{40}\Z")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str | None:
    return sha256_bytes(path.read_bytes()) if path.is_file() else None


class Run:
    def __init__(self, run_dir: Path):
        self.run_dir = run_dir
        self.children: list[dict] = []
        self.problems: list[str] = []

    def child(self, name: str, argv: list, cwd: Path, stdout_path: Path | None = None) -> dict:
        """Run one literal argument-list child (shell disabled) and keep its raw output."""
        out_path = stdout_path or self.run_dir / f"{name}.stdout"
        err_path = self.run_dir / f"{name}.stderr"
        record = {"name": name, "argv": [str(a) for a in argv], "cwd": str(cwd), "shell": False}
        try:
            with open(out_path, "xb") as out, open(err_path, "xb") as err:
                proc = subprocess.run([str(a) for a in argv], cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=out,
                                      stderr=err, timeout=CHILD_TIMEOUT, check=False, shell=False)
            record["exit"] = proc.returncode
        except (OSError, subprocess.SubprocessError) as exc:
            record["exit"] = None
            record["launch_error"] = f"{type(exc).__name__}: {exc}"
        record.update(stdout_path=str(out_path), stderr_path=str(err_path),
                      stdout_sha256=sha256_file(out_path), stderr_sha256=sha256_file(err_path))
        self.children.append(record)
        return record

    def text(self, record: dict, stream: str = "stdout") -> str:
        return Path(record[f"{stream}_path"]).read_text(encoding="utf-8", errors="replace")

    def git(self, name: str, *args: str, cwd: Path = REPO) -> tuple[int | None, str]:
        record = self.child(name, ["git", *args], cwd)
        return record["exit"], self.text(record)

    def need(self, ok: bool, message: str) -> bool:
        if not ok:
            self.problems.append(message)
        return ok

    def write_json(self, name: str, value) -> Path:
        path = self.run_dir / name
        with open(path, "x", encoding="utf-8") as fh:
            json.dump(value, fh, indent=2, sort_keys=True, default=str)
        return path


# ---------------------------------------------------------------- identity

def identity(run: Run, label: str) -> dict:
    _, head = run.git(f"{label}-rev-parse-head", "rev-parse", "HEAD")
    _, tree = run.git(f"{label}-rev-parse-tree", "rev-parse", "HEAD^{tree}")
    code, status = run.git(f"{label}-status", "status", "--porcelain=v1", "--untracked-files=all")
    return {"head": head.strip(), "tree": tree.strip(), "status_exit": code, "status_porcelain": status,
            "authored_sha256": {rel: sha256_file(REPO / rel) for rel in AUTHORED},
            "pinned_sha256": {rel: sha256_file(REPO / rel) for rel in PINNED_SOURCES}}


def check_manifest(run: Run, manifest_path: Path, phase: str) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    runtime = manifest.get("runtime") if isinstance(manifest, dict) else None
    run.need(isinstance(manifest, dict) and manifest.get("schema") == MANIFEST_SCHEMA, "manifest schema mismatch")
    run.need(isinstance(runtime, dict), "manifest runtime missing")
    runtime = runtime or {}
    run.need(Path(str(manifest.get("repo"))).resolve() == REPO, "manifest repo is not this candidate")
    run.need(Path(str(manifest.get("write_root"))).resolve() == REPO.parent, "manifest write_root mismatch")
    base, base_tree = str(manifest.get("head")), str(manifest.get("tree"))
    run.need(bool(_HEX40.fullmatch(base)) and bool(_HEX40.fullmatch(base_tree)), "manifest head/tree not full SHAs")
    facts = {"manifest_sha256": sha256_file(manifest_path), "base": base, "base_tree": base_tree}
    _, measured_base_tree = run.git("base-tree", "rev-parse", f"{base}^{{tree}}")
    facts["measured_base_tree"] = measured_base_tree.strip()
    run.need(facts["measured_base_tree"] == base_tree, "manifest tree does not match the base commit tree")
    _, head = run.git("candidate-head", "rev-parse", "HEAD")
    head = head.strip()
    run.need(head != base, "HEAD is still the pinned base; no candidate commit")
    ancestor = run.child("base-is-ancestor", ["git", "merge-base", "--is-ancestor", base, head], REPO)
    run.need(ancestor["exit"] == 0, "candidate HEAD does not descend from the pinned base")
    _, revs = run.git("candidate-commits", "rev-list", "--reverse", f"{base}..{head}")
    commits = []
    for index, sha in enumerate(revs.split()):
        _, body = run.git(f"commit-body-{index}", "log", "-1", "--format=%B", sha)
        _, author = run.git(f"commit-author-{index}", "log", "-1", "--format=%an <%ae>", sha)
        signed = any(line.startswith("Signed-off-by: ") for line in body.splitlines())
        commits.append({"sha": sha, "signed_off": signed, "author": author.strip(),
                        "subject": body.splitlines()[0] if body else ""})
        run.need(signed, f"commit {sha} lacks Signed-off-by")
    run.need(bool(commits), "no candidate commits above base")
    facts["candidate_commits"] = commits
    venv = Path(str(runtime.get("repo_venv")))
    facts["repo_venv"] = str(venv)
    facts["dotvenv_link"] = os.readlink(REPO / ".venv") if (REPO / ".venv").is_symlink() else None
    run.need((REPO / ".venv").is_symlink() and (REPO / ".venv").resolve() == venv.resolve(),
             ".venv is not the manifest-bound shared repo venv link")
    facts["interpreter"] = sys.executable
    facts["sys_prefix"] = sys.prefix
    if phase == "credentials":
        expected = str(runtime.get("credential_scan_python3"))
        run.need(sys.executable == expected and Path(expected).is_absolute(),
                 f"credentials row must run under runtime.credential_scan_python3 ({expected})")
    else:
        run.need(Path(sys.prefix).resolve() == venv.resolve(), "interpreter is not the staged repo venv")
        run.need(Path(sys.executable).parent.parent == REPO / ".venv",
                 "interpreter was not invoked through the staged repo .venv")
    for rel, pinned in PINNED_SOURCES.items():
        run.need(sha256_file(REPO / rel) == pinned, f"read-only pinned source changed: {rel}")
    facts["runtime"] = runtime
    return facts


# ---------------------------------------------------------------- unittest helpers

class RecordingResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.outcomes: list[dict] = []

    def _note(self, test, outcome):
        self.outcomes.append({"id": test.id(), "outcome": outcome})

    def addSuccess(self, test):
        super().addSuccess(test)
        self._note(test, "ok")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._note(test, "FAIL")

    def addError(self, test, err):
        super().addError(test, err)
        self._note(test, "ERROR")

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self._note(test, "skipped")

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self._note(test, "expected failure")

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self._note(test, "unexpected success")


def run_suite_in_process(run: Run, label: str, suite: unittest.TestSuite) -> dict:
    """Run a loaded suite; keep the raw runner stream and any stdout/stderr writes."""
    stream, out, err = io.StringIO(), io.StringIO(), io.StringIO()
    expected = suite.countTestCases()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        result = unittest.TextTestRunner(stream=stream, verbosity=2, resultclass=RecordingResult).run(suite)
    for suffix, buffer in (("runner.txt", stream), ("stdout", out), ("stderr", err)):
        with open(run.run_dir / f"{label}.{suffix}", "x", encoding="utf-8") as fh:
            fh.write(buffer.getvalue())
    summary = {"loaded": expected, "ran": result.testsRun, "failures": len(result.failures),
               "errors": len(result.errors), "skipped": len(result.skipped),
               "expected_failures": len(result.expectedFailures),
               "unexpected_successes": len(result.unexpectedSuccesses),
               "outcomes": result.outcomes,
               "runner_sha256": sha256_bytes(stream.getvalue().encode("utf-8"))}
    run.need(result.testsRun == expected and expected > 0, f"{label}: ran {result.testsRun} of {expected}")
    run.need(result.wasSuccessful() and not result.skipped and not result.expectedFailures
             and not result.unexpectedSuccesses, f"{label}: not cleanly green {summary['failures']}F "
             f"{summary['errors']}E {summary['skipped']}S")
    run.need(all(o["outcome"] == "ok" for o in result.outcomes), f"{label}: non-ok outcome")
    return summary


def _import_repo():
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))


def _method_names(outcomes: list[dict]) -> dict[str, str]:
    return {o["id"].rsplit(".", 1)[-1]: o["outcome"] for o in outcomes}


def case_report(fixture: dict, outcomes: dict[str, str]) -> list[dict]:
    rows = []
    for case in fixture["cases"]:
        name = f"test_case_{case['id']}"
        rows.append({"case": case["id"], "dimension": case["dimension"], "boundary": case["boundary"],
                     "originals": case["originals"], "interleave": case["interleave"], "counts": case["counts"],
                     "expected_totals": case["expect"]["candidate_totals"],
                     "expected_omissions": case["expect"]["candidate_omissions"],
                     "expected_partial": case["expect"]["partial_understanding"],
                     "test": name, "outcome": outcomes.get(name)})
    return rows


# ---------------------------------------------------------------- phases

def phase_suite(run: Run) -> dict:
    _import_repo()
    module = importlib.import_module(SUITE_MODULE)
    run.need(Path(module.__file__).resolve() == REPO / SUITE_FILE, "suite module loaded outside candidate")
    suite = unittest.defaultTestLoader.loadTestsFromName(SUITE_MODULE)
    summary = run_suite_in_process(run, "suite-unittest", suite)
    outcomes = _method_names(summary["outcomes"])
    fixture = module.FIXTURE_DOC
    cases = case_report(fixture, outcomes)
    for row in cases:
        run.need(row["outcome"] == "ok", f"case {row['case']} not ok: {row['outcome']}")
    for name in CONTRACT_TESTS:
        run.need(outcomes.get(name) == "ok", f"contract test {name} not ok")
    behaviours = {b: {t: outcomes.get(t) for t in tests} for b, tests in BEHAVIOUR_MAP.items()}
    for behaviour, tests in behaviours.items():
        run.need(bool(tests) and all(v == "ok" for v in tests.values()), f"behaviour unmapped/not ok: {behaviour}")
    # Declared synthetic negative control, receipted with separate labels.
    control = module.run_negative_control()
    run.write_json("negative-control.json", control)
    run.need(all(v["rejected"] for v in control["mutant_verdicts"].values()),
             f"negative control mutant survived: {control['mutant_verdicts']}")
    run.need(control["mutant_top_level_differences"] == ["candidate_omissions"] and control["mutant_emitted_unchanged"]
             and control["mutant_total_unchanged"], "negative control did not inject exactly the declared mutant")
    run.need(control["final_candidate_output"]["unchanged_by_control"], "control altered the baseline result")
    rerun = run_suite_in_process(run, "negative-restored-positive", unittest.defaultTestLoader.loadTestsFromName(
        f"{SUITE_MODULE}.FC9CandidateCountCaseTests.test_case_{module.NEGATIVE_CASE}"))
    dims = sorted({c["dimension"] for c in cases})
    return {"unittest": summary, "cases": cases, "case_count": len(cases), "dimensions": dims,
            "malformed_probe_count": len(fixture["malformed_specs"]), "behaviour_map": behaviours,
            "negative_control": control, "restored_positive_rerun": rerun}


def phase_compatibility(run: Run) -> dict:
    _import_repo()
    suite = unittest.defaultTestLoader.loadTestsFromNames(list(COMPAT_SELECTORS))
    summary = run_suite_in_process(run, "compatibility-unittest", suite)
    return {"selectors": list(COMPAT_SELECTORS), "unittest": summary,
            "scope_note": "scoped compatibility only; full repository CI and full discovery are operator-run and OWED"}


_VERBOSE_LINE = re.compile(r"^(test\w*) \(([\w.]+)\)(?:\n.*?)? \.\.\. (ok|FAIL|ERROR|skipped.*|expected failure|unexpected success)$",
                           re.M)


def host_literal_scan() -> list[dict]:
    hits = []
    for rel in AUTHORED:
        text = (REPO / rel).read_text(encoding="utf-8")
        for a, b in HOST_FRAGMENTS:
            if (a + b) in text:
                hits.append({"file": rel, "pattern": a + b})
    return hits


def phase_portable(run: Run) -> dict:
    hits = host_literal_scan()
    run.need(not hits, f"host-specific or forbidden literals: {hits}")
    script_cwd = run.run_dir / "script-cwd"
    script_cwd.mkdir()
    script_tmp = run.run_dir / "script-temp-root"
    script = run.child("script-run", [sys.executable, REPO / SUITE_FILE, "--temp-root", script_tmp, "-v"], script_cwd)
    text = run.text(script, "stderr")
    script_outcomes = {m.group(1): m.group(3) for m in _VERBOSE_LINE.finditer(text)}
    ran = re.findall(r"^Ran (\d+) tests? in ", text, re.M)
    run.need(script["exit"] == 0, f"script run exit {script['exit']}")
    run.need(bool(re.search(r"^OK$", text, re.M)), "script run final line is not OK")
    run.need(bool(ran) and int(ran[-1]) == len(script_outcomes), "script run count mismatch")
    report_path = run.run_dir / "discovery-report.json"
    disc_cwd = run.run_dir / "discovery-cwd"
    disc_cwd.mkdir()
    disc = run.child("discovery-child", [sys.executable, REPO / RUNNER_FILE, "--discovery-child", report_path,
                                         run.run_dir / "discovery-temp-root"], disc_cwd)
    report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.is_file() else None
    run.need(disc["exit"] == 0 and report is not None, f"discovery child exit {disc['exit']}")
    report = report or {}
    disc_outcomes = _method_names(report.get("discovery", {}).get("outcomes", []))
    for label in ("sentinel_before", "sentinel_after"):
        run.need(report.get(label, {}).get("ok") is True, f"{label} not green")
    run.need(not report.get("ambient_changes"), f"discovery changed ambient state: {report.get('ambient_changes')}")
    run.need(not report.get("outside_repo_modules"), "discovery loaded product modules outside candidate")
    _import_repo()
    fixture = json.loads((REPO / FIXTURE_FILE).read_text(encoding="utf-8"))
    required = {f"test_case_{c['id']}" for c in fixture["cases"]} | set(CONTRACT_TESTS)
    script_ok = {k for k, v in script_outcomes.items() if v == "ok"}
    disc_ok = {k for k, v in disc_outcomes.items() if v == "ok"}
    run.need(required <= script_ok, f"script run missing required cases {sorted(required - script_ok)}")
    run.need(required <= disc_ok, f"discovery missing required cases {sorted(required - disc_ok)}")
    run.need(set(script_outcomes.values()) == {"ok"} and set(disc_outcomes.values()) == {"ok"},
             "non-ok outcome in portable runs")
    return {"host_literal_hits": hits, "script": {"cwd": str(script_cwd), "temp_root": str(script_tmp),
                                                    "ran": int(ran[-1]) if ran else None,
                                                    "outcomes": script_outcomes},
            "discovery_report": report, "required_case_coverage": sorted(required),
            "script_required_covered": sorted(required & script_ok),
            "discovery_required_covered": sorted(required & disc_ok),
            "equal_required_coverage": (required & script_ok) == (required & disc_ok) == required}


def _ambient() -> dict:
    umask = os.umask(0o077)
    os.umask(umask)
    return {"environ": dict(os.environ), "tempdir": tempfile.tempdir, "sys_path": list(sys.path),
            "cwd": os.getcwd(), "umask": umask, "argv": list(sys.argv)}


def discovery_child(report_path: Path, temp_root: Path) -> int:
    """Isolated child: tempdir first, then sentinel/discovery/sentinel with ambient snapshots."""
    temp_root.mkdir(parents=True, exist_ok=False)
    tempfile.tempdir = str(temp_root)
    _import_repo()
    run = Run(report_path.parent)
    before = _ambient()
    sentinel_before = run_suite_in_process(run, "discovery-sentinel-before",
                                           unittest.defaultTestLoader.loadTestsFromName(SENTINEL_TEST))
    mid = _ambient()
    discovered = unittest.defaultTestLoader.discover(start_dir=str(REPO / "tests" / "records"),
                                                     pattern=Path(SUITE_FILE).name, top_level_dir=str(REPO))
    discovery = run_suite_in_process(run, "discovery-suite", discovered)
    sentinel_after = run_suite_in_process(run, "discovery-sentinel-after",
                                          unittest.defaultTestLoader.loadTestsFromName(SENTINEL_TEST))
    after = _ambient()
    changes = sorted(k for k in before if before[k] != after[k]) + \
        [f"during-sentinel:{k}" for k in before if before[k] != mid[k]]
    modules = {n: getattr(m, "__file__", None) for n, m in sorted(sys.modules.items())
               if n.split(".")[0] in ("campaign_tool", "tests")}
    outside = {n: f for n, f in modules.items() if f and not Path(f).resolve().is_relative_to(REPO)}
    report = {"temp_root": str(temp_root), "sentinel": SENTINEL_TEST,
              "sentinel_before": {**sentinel_before, "ok": sentinel_before["failures"] == 0
                                  and sentinel_before["errors"] == 0 and sentinel_before["ran"] == 1},
              "discovery": discovery,
              "sentinel_after": {**sentinel_after, "ok": sentinel_after["failures"] == 0
                                 and sentinel_after["errors"] == 0 and sentinel_after["ran"] == 1},
              "ambient_changes": changes, "loaded_modules": modules, "outside_repo_modules": outside,
              "problems": run.problems}
    with open(report_path, "x", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, sort_keys=True, default=str)
    return 0 if not run.problems and not changes and not outside else 1


def phase_credentials(run: Run, manifest: dict) -> dict:
    scanner = Path(str(manifest["runtime"].get("detect_secrets")))
    if not run.need(scanner.is_absolute() and scanner.is_file(), f"manifest detect_secrets unavailable: {scanner}"):
        return {}
    report = run.run_dir / "secret-scan.json"
    scan = run.child("detect-secrets-scan", [scanner, "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"],
                     REPO, stdout_path=report)
    run.need(scan["exit"] == 0, f"scanner exit {scan['exit']}")
    data = json.loads(report.read_text(encoding="utf-8"))
    results = data.get("results") if isinstance(data, dict) else None
    run.need(isinstance(results, dict), "secret report malformed")
    traversed = sorted(p for p in (results or {}) if p == ".venv" or p.startswith(".venv/"))
    run.need(not traversed, f"scanner traversed the staged .venv link: {traversed[:5]}")
    check = run.child("check-secret-report", [sys.executable, REPO / "tools" / "check_secret_report.py", report], REPO)
    out = run.text(check)
    found = re.search(r"potential secrets: (\d+)", out)
    run.need(check["exit"] == 0, f"check_secret_report exit {check['exit']}")
    run.need(bool(found) and int(found.group(1)) == 0, "potential secrets not reported as zero")
    return {"scanner": str(scanner), "report": str(report), "report_sha256": sha256_file(report),
            "files_with_hits": sorted(results or {}), "potential_secrets": int(found.group(1)) if found else None,
            "plugins_used": len(data.get("plugins_used", [])) if isinstance(data, dict) else None}


def _scan_json(run: Run, record: dict) -> dict | None:
    try:
        return json.loads(run.text(record))
    except ValueError:
        return None


def phase_public_tree(run: Run, manifest: dict, before: dict) -> dict:
    head, tree = before["head"], before["tree"]
    run.need(bool(_HEX40.fullmatch(head)) and bool(_HEX40.fullmatch(tree)), "candidate HEAD/tree unavailable")
    run.need(before["status_porcelain"] == "", f"candidate has uncommitted or untracked files: {before['status_porcelain']!r}")
    for rel in AUTHORED:
        tracked = run.child("ls-files-" + Path(rel).name, ["git", "ls-files", "--error-unmatch", rel], REPO)
        run.need(tracked["exit"] == 0, f"authored file not tracked: {rel}")
    worktree = run.child("diff-worktree-head", ["git", "diff", "--quiet", "HEAD", "--"], REPO)
    index = run.child("diff-index-head", ["git", "diff", "--cached", "--quiet", "HEAD", "--"], REPO)
    run.need(worktree["exit"] == 0 and index["exit"] == 0, "tracked or index bytes differ from HEAD")
    _, ignored = run.git("ignored-inventory", "status", "--porcelain=v1", "--ignored", "--untracked-files=all")
    _, tracked_paths = run.git("tracked-inventory", "ls-files")
    clone = run.run_dir / "committed-tree"
    cl = run.child("clone", ["git", "clone", "--local", "--no-hardlinks", "--no-checkout", "--no-recurse-submodules",
                             REPO, clone], run.run_dir)
    run.need(cl["exit"] == 0, "local clone failed")
    co = run.child("clone-checkout", ["git", "checkout", "--detach", head], clone)
    run.need(co["exit"] == 0, "clone checkout failed")
    _, clone_head = run.git("clone-head", "rev-parse", "HEAD", cwd=clone)
    _, clone_tree = run.git("clone-tree", "rev-parse", "HEAD^{tree}", cwd=clone)
    run.need(clone_head.strip() == head and clone_tree.strip() == tree, "clone HEAD/tree differ from candidate")
    run.need(not os.path.lexists(clone / ".venv"), "clone contains a .venv entry")
    compared = {rel: {"candidate": sha256_file(REPO / rel), "clone": sha256_file(clone / rel)}
                for rel in AUTHORED + CHECKER_FILES + tuple(PINNED_SOURCES)}
    for rel, pair in compared.items():
        run.need(pair["candidate"] is not None and pair["candidate"] == pair["clone"], f"clone bytes differ: {rel}")
    scan = run.child("clone-check-public-tree", [sys.executable, clone / "tools" / "check_public_tree.py"], clone)
    parsed = _scan_json(run, scan)
    run.need(scan["exit"] == 0, f"committed-tree public scan exit {scan['exit']}")
    run.need(isinstance(parsed, dict) and parsed.get("findings") == [], "committed-tree scan findings not empty")
    # INC-174 informational: the unchanged checker in the staged repo itself.
    inplace = run.child("inplace-check-public-tree", [sys.executable, REPO / "tools" / "check_public_tree.py"], REPO)
    inplace_parsed = _scan_json(run, inplace)
    venv_ok = (REPO / ".venv").is_symlink() and (REPO / ".venv").resolve() == Path(manifest["runtime"]["repo_venv"]).resolve()
    findings = inplace_parsed.get("findings") if isinstance(inplace_parsed, dict) else None
    allowed = (inplace["exit"] == 0 and findings == []) or \
        (inplace["exit"] == 1 and findings == [[".venv", 0, "symlink_not_scanned"]] and venv_ok)
    run.need(allowed, f"in-place informational scan outside allowed outcomes: exit {inplace['exit']} {findings}")
    after_head = run.git("source-head-recheck", "rev-parse", "HEAD")[1].strip()
    after_status = run.git("source-status-recheck", "status", "--porcelain=v1", "--untracked-files=all")[1]
    run.need(after_head == head and after_status == before["status_porcelain"], "source HEAD/status changed")
    return {"candidate_head": head, "candidate_tree": tree, "status_porcelain": before["status_porcelain"],
            "ignored_inventory": ignored, "tracked_path_count": len(tracked_paths.splitlines()),
            "clone": str(clone), "clone_head": clone_head.strip(), "clone_tree": clone_tree.strip(),
            "hash_comparison": compared, "committed_tree_scan": parsed,
            "in_place_informational": {"label": "INC-174 in_place_informational (not a staged-tree pass)",
                                       "exit": inplace["exit"], "findings": findings,
                                       "venv_link_matches_manifest": venv_ok}}


# ---------------------------------------------------------------- main

def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[0] == "--discovery-child":
        return discovery_child(Path(argv[1]), Path(argv[2]))
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--phase", required=True, choices=PHASES)
    parser.add_argument("--source-manifest", required=True, type=Path)
    parser.add_argument("--out-base", required=True, type=Path)
    args = parser.parse_args(argv)
    out_base = args.out_base.resolve()
    if out_base == REPO or out_base.is_relative_to(REPO):
        print("refused: --out-base must be outside the candidate repository", file=sys.stderr)
        return 2
    out_base.mkdir(parents=True, exist_ok=True)
    run_dir = out_base / f"{args.phase}-{uuid.uuid4()}"
    run_dir.mkdir(exist_ok=False)
    temp_root = run_dir / "tmp"
    temp_root.mkdir()
    tempfile.tempdir = str(temp_root)  # before any product import
    run = Run(run_dir)
    started = datetime.datetime.now(datetime.timezone.utc).isoformat()
    record = {"spec": SPEC, "phase": args.phase, "argv": [sys.executable, str(Path(__file__).resolve()), *argv],
              "cwd": os.getcwd(), "interpreter": sys.executable, "python_version": sys.version,
              "repo": str(REPO), "run_dir": str(run_dir), "tempdir": tempfile.tempdir, "started_utc": started,
              "verdict_scope": "MACHINE CHECK ONLY - REVIEW REQUIRED; not semantic, legal, human or live acceptance",
              "full_discovery": "operator-run (not part of this packet)"}
    try:
        record["manifest"] = check_manifest(run, args.source_manifest.resolve(), args.phase)
        record["identity_before"] = before = identity(run, "before")
        record["fixture_sha256"] = sha256_file(REPO / FIXTURE_FILE)
        if args.phase == "suite":
            record["result"] = phase_suite(run)
        elif args.phase == "compatibility":
            record["result"] = phase_compatibility(run)
        elif args.phase == "portable":
            record["result"] = phase_portable(run)
        elif args.phase == "credentials":
            record["result"] = phase_credentials(run, json.loads(args.source_manifest.read_text(encoding="utf-8")))
        else:
            record["result"] = phase_public_tree(run, json.loads(args.source_manifest.read_text(encoding="utf-8")),
                                                 before)
        record["identity_after"] = after = identity(run, "after")
        for key in ("head", "tree", "status_porcelain", "authored_sha256", "pinned_sha256"):
            run.need(before[key] == after[key], f"candidate {key} changed during phase")
    except Exception as exc:  # an incomplete phase is a failure, never a pass
        run.problems.append(f"incomplete: {type(exc).__name__}: {exc}")
    record["children"] = run.children
    record["problems"] = run.problems
    record["passed"] = not run.problems
    record["artifacts"] = {str(p.relative_to(run_dir)): sha256_file(p) for p in sorted(run_dir.rglob("*"))
                           if p.is_file() and not p.is_relative_to(run_dir / "committed-tree")
                           and not p.is_relative_to(temp_root)}
    run.write_json("result.json", record)
    print(json.dumps({"phase": args.phase, "passed": record["passed"], "problems": run.problems,
                      "run_dir": str(run_dir), "head": record.get("identity_before", {}).get("head"),
                      "tree": record.get("identity_before", {}).get("tree"),
                      "result_sha256": sha256_file(run_dir / "result.json")}, sort_keys=True))
    return 0 if record["passed"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
