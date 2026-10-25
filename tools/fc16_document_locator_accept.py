#!/usr/bin/env python3
"""FC16-DOCUMENT-LOCATOR acceptance recorder (machine checks only; REVIEW REQUIRED).

Exactly one --phase per invocation:

  suite          baseline red (the new regression in a local clone checked out at
                 the manifest base, with only the committed test module and fixture
                 overlaid), then the complete frozen matrix on the candidate under
                 scoped offline sentinels, then isolated in-memory mutants of the
                 unit membership and gap gates, then the restored module again
  compatibility  tests.records.test_fc_doc_coverage.FcDocCoverageTests on the base
                 clone (original) and on the candidate (final); deltas recorded
  portable       the new module as an absolute script from an unrelated cwd with a
                 temporary-root argument, plus single-module discovery in a
                 dedicated process between inert peer sentinels
  credentials    manifest-declared detect-secrets with the unchanged CI arguments,
                 then the unchanged tools/check_secret_report.py on that report
  public-tree    local exact committed-tree clone checked by the unchanged
                 tools/check_public_tree.py, plus one in-place informational run

Each invocation writes a fresh UUID directory below --out-base (outside the
repository) and never touches earlier outputs. Children are argument lists with
no shell. Repository paths are derived from this file; runtime paths come from
arguments and the source manifest. A pass is machine validity only: REVIEW
REQUIRED, never semantic, factual, legal or release acceptance. Full repository
discovery and required CI remain owed to the shipping operator.
"""
from __future__ import annotations

import argparse
import ast
import copy
import datetime
import hashlib
import importlib
import io
import json
import os
import platform
import re
import socket
import subprocess
import sys
import tempfile
import types
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True

REPO = Path(__file__).resolve().parents[1]
SCHEMA = "fc16-document-locator-acceptance/v1"
CARD = "FC16-DOCUMENT-LOCATOR"
PHASES = ("suite", "compatibility", "portable", "credentials", "public-tree")
MANIFEST_SCHEMA = "agentorange.source-manifest/v1"
DOCUMENT = "campaign_tool/records/understanding/document.py"
TEST_FILE = "tests/records/test_fc16_document_locator.py"
FIXTURE_FILE = "tests/fixtures/fc16_document_locator/cases.json"
RECORDER = "tools/fc16_document_locator_accept.py"
AUTHORED = (DOCUMENT, TEST_FILE, FIXTURE_FILE, RECORDER)
# Pinned read-only sources: their base blobs are recorded and must be unchanged at HEAD.
PINNED_READ_ONLY = ("campaign_tool/records/understanding/evidence.py", "tests/records/test_fc_doc_coverage.py")
CHECKERS = ("tools/check_public_tree.py", "campaign_tool/records/public_scan.py", "tools/check_secret_report.py")
MODULE = "tests.records.test_fc16_document_locator"
CLASS = "FC16DocumentLocatorTests"
FROZEN_CASES = ("valid_aliases", "invalid_types", "contradiction", "mixed_and_nonpage")
COMPAT_SELECTOR = "tests.records.test_fc_doc_coverage.FcDocCoverageTests"
COMPAT_FILE = "tests/records/test_fc_doc_coverage.py"
GAP = "unit_page_locator_invalid"
FIXED_MEMBERSHIP = "page_units = [u for u, unit_page, _ in located if unit_page == page]"
ORIGINAL_TUPLE = 'page in (u.get("locator", {}).get("page"), u.get("locator", {}).get("page_no"))'
MUTANT_MEMBERSHIP = "page_units = [u for u, unit_page, _ in located if " + ORIGINAL_TUPLE + "]"
FIXED_GAP = 'gaps.add("unit_page_locator_invalid")'
CHILD_TIMEOUT = 900
HEX40 = re.compile(r"[0-9a-f]{40}\Z")
# Box-specific or inline-interpreter markers, split so this file does not match itself.
BOX_MARKERS = ("/ho" + "me/", "/Us" + "ers/", "agent" + "-work", "/t" + "mp/", ".her" + "mes", "kan" + "ban",
               "venv-" + "fullsuite", "PYTHON" + "PATH", "shell" + "=True", "<<" + "EOF")


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    path = Path(path)
    return sha256_bytes(path.read_bytes()) if path.is_file() and not path.is_symlink() else None


def run_cmd(argv, cwd, env=None, timeout=CHILD_TIMEOUT):
    """Local argument-list command, no shell; returns (exit, stdout bytes, stderr bytes)."""
    proc = subprocess.run([str(a) for a in argv], cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False)
    return proc.returncode, proc.stdout, proc.stderr


def git(*args, cwd=REPO):
    code, out, err = run_cmd(["git", "-C", str(cwd), *args], cwd)
    return code, out.decode("utf-8", "replace").rstrip("\n"), err.decode("utf-8", "replace").strip()


def child(run, record, name, argv, cwd, env=None):
    """Run a child and retain raw stdout/stderr with hashes in the run directory."""
    code, out, err = run_cmd(argv, cwd, env=env)
    (run / (name + ".stdout")).write_bytes(out)
    (run / (name + ".stderr")).write_bytes(err)
    entry = {"argv": [str(a) for a in argv], "cwd": str(cwd), "exit": code,
             "stdout": name + ".stdout", "stdout_sha256": sha256_bytes(out),
             "stderr": name + ".stderr", "stderr_sha256": sha256_bytes(err)}
    record["children"][name] = entry
    return code, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def python_env(tmp):
    tmp.mkdir(parents=True, exist_ok=True)
    return dict(os.environ, TMPDIR=str(tmp), PYTHONDONTWRITEBYTECODE="1")


# identity -------------------------------------------------------------------
def load_manifest(path, problems):
    raw = Path(path).read_bytes()
    manifest = json.loads(raw)
    info = {"path": str(path), "sha256": sha256_bytes(raw), "schema": manifest.get("schema"),
            "head": manifest.get("head"), "tree": manifest.get("tree"), "repo": manifest.get("repo"),
            "runtime": manifest.get("runtime")}
    if manifest.get("schema") != MANIFEST_SCHEMA:
        problems.append("manifest_schema")
    if not (isinstance(manifest.get("head"), str) and HEX40.match(manifest["head"])):
        problems.append("manifest_head_malformed")
    if not (isinstance(manifest.get("tree"), str) and HEX40.match(manifest["tree"])):
        problems.append("manifest_tree_malformed")
    if not isinstance(manifest.get("repo"), str) or os.path.realpath(manifest["repo"]) != str(REPO):
        problems.append("manifest_repo_mismatch")
    runtime = manifest.get("runtime") if isinstance(manifest.get("runtime"), dict) else {}
    for key in ("repo_venv", "credential_scan_python3", "detect_secrets"):
        if not isinstance(runtime.get(key), str) or not os.path.exists(runtime[key]):
            problems.append("manifest_runtime_missing:" + key)
    return manifest, info


def preflight(manifest, args, problems):
    """Validate manifest identity, base ancestry, write scope and read-only pins before imports."""
    base = manifest.get("head") or ""
    out = {"base": base}
    if os.path.realpath(args.repo) != str(REPO):
        problems.append("repo_argument_not_this_repository")
    venv = REPO / ".venv"
    out["venv_link"] = os.readlink(venv) if venv.is_symlink() else None
    runtime = manifest.get("runtime") or {}
    if out["venv_link"] is None or os.path.realpath(venv) != os.path.realpath(runtime.get("repo_venv", "")):
        problems.append("venv_link_not_manifest_bound")
    base_tree = git("rev-parse", base + "^{tree}")
    out["base_tree"] = base_tree[1] if base_tree[0] == 0 else None
    if out["base_tree"] != manifest.get("tree"):
        problems.append("manifest_tree_mismatch")
    if git("merge-base", "--is-ancestor", base, "HEAD")[0] != 0:
        problems.append("base_not_ancestor_of_head")
    head = git("rev-parse", "HEAD")[1]
    out["candidate_is_descendant"] = head != base
    if head == base:
        problems.append("candidate_head_equals_base")
    diff = git("diff", "--name-only", base, "HEAD")
    changed = sorted(filter(None, diff[1].splitlines())) if diff[0] == 0 else None
    out["changed_files"] = changed
    if changed is None or sorted(set(changed) - set(AUTHORED)):
        problems.append("changes_outside_write_set")
    pins = {}
    for rel in (DOCUMENT,) + PINNED_READ_ONLY:
        code, blob, _ = run_cmd(["git", "-C", str(REPO), "show", base + ":" + rel], REPO)
        pins[rel] = sha256_bytes(blob) if code == 0 else None
    out["base_blob_sha256"] = pins
    for rel in PINNED_READ_ONLY + CHECKERS:
        if git("diff", "--quiet", base, "HEAD", "--", rel)[0] != 0:
            problems.append("read_only_changed:" + rel)
    log = git("log", "--format=%H%x00%B%x01", base + "..HEAD")
    commits = [c.strip() for c in log[1].split("\x01") if c.strip()]
    out["commits"] = [{"commit": c.split("\x00", 1)[0], "signed_off": "Signed-off-by:" in c} for c in commits]
    if not commits or not all(c["signed_off"] for c in out["commits"]):
        problems.append("commit_missing_signoff")
    return out


def identity():
    head = git("rev-parse", "HEAD")
    tree = git("rev-parse", "HEAD^{tree}")
    status = git("status", "--porcelain=v1", "--untracked-files=all", "--ignored")
    return {"head": head[1] if head[0] == 0 else None, "tree": tree[1] if tree[0] == 0 else None,
            "status_porcelain_ignored": status[1] if status[0] == 0 else None,
            "sha256": {rel: sha256_file(REPO / rel) for rel in AUTHORED + PINNED_READ_ONLY + CHECKERS}}


def clone_at(run, record, name, commit):
    dest = run / name / "repo"
    dest.parent.mkdir(parents=True)
    code, _, _ = child(run, record, name + "-clone", ["git", "clone", "--quiet", "--local", "--no-hardlinks",
                                                      "--no-checkout", "--no-recurse-submodules", str(REPO),
                                                      str(dest)], run)
    if code != 0:
        return None
    code, _, _ = child(run, record, name + "-checkout", ["git", "-C", str(dest), "checkout", "--quiet",
                                                         "--detach", commit], run)
    return dest if code == 0 else None


# unittest helpers -------------------------------------------------------------
class RecordingResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.events = []

    def _event(self, test, subtest, outcome, err=None):
        message = None
        if err is not None:
            message = "".join(str(err[1]))[:1200]
        self.events.append({"test": test.id(), "subtest": subtest.id() if subtest is not None else None,
                            "outcome": outcome, "message": message})

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is None:
            self._event(test, subtest, "ok")
        else:
            self._event(test, subtest, "fail" if issubclass(err[0], test.failureException) else "error", err)

    def addSuccess(self, test):
        super().addSuccess(test)
        self._event(test, None, "ok")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._event(test, None, "fail", err)

    def addError(self, test, err):
        super().addError(test, err)
        self._event(test, None, "error", err)

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.events.append({"test": test.id(), "subtest": None, "outcome": "skipped", "message": reason})


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


def run_suite(suite, label, run, record):
    ids = sorted(t.id() for t in flatten(suite))
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=2, resultclass=RecordingResult).run(suite)
    raw = stream.getvalue().encode("utf-8")
    (run / (label + ".unittest.txt")).write_bytes(raw)
    summary = {"ids": ids, "tests_run": result.testsRun, "successful": result.wasSuccessful(),
               "failures": sorted({t.id() for t, _ in result.failures}),
               "errors": sorted({t.id() for t, _ in result.errors}),
               "skipped": [t.id() for t, _ in result.skipped],
               "expected_failures": len(result.expectedFailures),
               "unexpected_successes": len(result.unexpectedSuccesses),
               "events": result.events, "raw_output": label + ".unittest.txt", "raw_output_sha256": sha256_bytes(raw)}
    record["phases"][label] = summary
    return summary


def clean(summary, expected_ids):
    return (summary["successful"] and summary["tests_run"] == len(expected_ids) and summary["ids"] == sorted(expected_ids)
            and not summary["skipped"] and not summary["expected_failures"] and not summary["unexpected_successes"]
            and all(e["outcome"] == "ok" for e in summary["events"]))


def defined_tests(path, class_name):
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    names = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            names += [f.name for f in node.body if isinstance(f, ast.FunctionDef) and f.name.startswith("test")]
    return names


def expected_ids():
    return [MODULE + "." + CLASS + "." + name for name in defined_tests(REPO / TEST_FILE, CLASS)]


def expected_subtests():
    cases = json.loads((REPO / FIXTURE_FILE).read_text(encoding="utf-8"))["cases"]
    invalid = cases["invalid_types"]
    return {"test_valid_aliases": len(cases["valid_aliases"]),
            "test_invalid_types": len(invalid["forms"]) * len(invalid["bad_values"]),
            "test_contradiction": len(cases["contradiction"]),
            "test_mixed_and_nonpage": len(cases["mixed_and_nonpage"])}


def subtest_counts(summary):
    counts = {}
    for event in summary["events"]:
        if event["subtest"] is not None:
            name = event["test"].rsplit(".", 1)[-1]
            counts[name] = counts.get(name, 0) + 1
    return counts


_VERBOSE = re.compile(r"(?m)^ *(test\w*) \(([\w.]+)\)(?: \((.*)\))? \.\.\. (ok|FAIL|ERROR|skipped.*)$")


def parse_verbose(text):
    ran = re.findall(r"(?m)^Ran (\d+) tests? in ", text)
    final = [line for line in text.splitlines() if re.match(r"^(OK|FAILED)\b", line)]
    return {"ran": int(ran[-1]) if ran else None, "final": final[-1] if final else None,
            "skipped_mentioned": "skipped" in (final[-1] if final else ""),
            "results": [{"test": m.group(2), "subtest": m.group(3), "outcome": m.group(4)} for m in _VERBOSE.finditer(text)]}


# scoped offline sentinels -------------------------------------------------------
HITS = []


def sentinel(label):
    def refuse(*args, **kwargs):
        HITS.append(label)
        raise PermissionError("fc16 acceptance sentinel refused " + label)
    return refuse


def scoped_sentinels(tmp):
    """Context managers that are all restored on exit; no audit hook is installed."""
    tmp.mkdir(parents=True, exist_ok=True)
    patches = [mock.patch.object(tempfile, "tempdir", str(tmp)),
               mock.patch.dict(os.environ, {"TMPDIR": str(tmp)}),
               mock.patch.object(socket.socket, "connect", sentinel("socket.connect")),
               mock.patch.object(socket.socket, "connect_ex", sentinel("socket.connect_ex")),
               mock.patch.object(socket.socket, "bind", sentinel("socket.bind")),
               mock.patch.object(socket, "create_connection", sentinel("socket.create_connection")),
               mock.patch.object(socket, "getaddrinfo", sentinel("socket.getaddrinfo")),
               mock.patch.object(subprocess.Popen, "__init__", sentinel("subprocess.Popen")),
               mock.patch.object(os, "system", sentinel("os.system"))]
    return patches


def static_closure():
    """Import closure of the product module and the new tests; network/model clients are refused."""
    roots = [REPO / DOCUMENT, REPO / TEST_FILE]
    seen, external = {}, set()
    pending = list(roots)
    while pending:
        path = pending.pop()
        rel = path.relative_to(REPO).as_posix()
        if rel in seen:
            continue
        seen[rel] = sha256_file(path)
        parts = path.relative_to(REPO).with_suffix("").parts
        for index in range(1, len(parts)):
            init = REPO.joinpath(*parts[:index], "__init__.py")
            if init.is_file():
                pending.append(init)
        for node in ast.walk(ast.parse(path.read_bytes())):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    anchor = list(parts[:-1]) if path.name != "__init__.py" else list(parts[:-1])
                    anchor = anchor[:len(anchor) - (node.level - 1)]
                    stem = ".".join(anchor + ([node.module] if node.module else []))
                else:
                    stem = node.module or ""
                names = [stem] + [stem + "." + a.name for a in node.names]
            for name in names:
                base = REPO.joinpath(*name.split("."))
                if (base / "__init__.py").is_file():
                    pending.append(base / "__init__.py")
                elif base.with_suffix(".py").is_file():
                    pending.append(base.with_suffix(".py"))
                elif name and not (REPO / name.split(".")[0]).exists():
                    external.add(name.split(".")[0])
    nonstdlib = sorted(external - set(sys.stdlib_module_names))
    network = sorted(external & {"socket", "ssl", "http", "urllib", "smtplib", "imaplib", "requests", "httpx",
                                 "openai", "anthropic", "ollama", "asyncio"})
    return {"files": seen, "external": sorted(external), "non_stdlib": nonstdlib, "network": network}


def build_mutant(source, old, new, label):
    """Compile an isolated in-memory copy of document.py with exactly one gate replaced."""
    if source.count(old) != 1:
        raise ValueError("mutant anchor not unique: " + label)
    mutated = source.replace(old, new)
    module = types.ModuleType("campaign_tool.records.understanding._fc16_mutant_" + label)
    module.__package__ = "campaign_tool.records.understanding"
    module.__file__ = "<fc16-mutant-" + label + ">"
    exec(compile(mutated, module.__file__, "exec"), module.__dict__)  # isolated copy; never written to disk
    return module, sha256_bytes(mutated.encode("utf-8"))


# phases ---------------------------------------------------------------------------
def phase_suite(run, record, manifest):
    base = manifest["head"]
    problems = record["problems"]
    # 1. baseline red: base product with only the committed new test module and fixture overlaid.
    clone = clone_at(run, record, "baseline", base)
    if clone is None:
        problems.append("baseline_clone_failed")
        return
    for rel in (TEST_FILE, FIXTURE_FILE):
        code, blob, _ = run_cmd(["git", "-C", str(REPO), "show", "HEAD:" + rel], REPO)
        if code != 0:
            problems.append("baseline_overlay_unavailable:" + rel)
            return
        target = clone / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blob)
    base_doc = sha256_file(clone / DOCUMENT)
    code, _, err = child(run, record, "baseline-red", [sys.executable, "-B", "-m", "unittest", "-v", MODULE], clone,
                         env=python_env(run / "baseline" / "tmp"))
    parsed = parse_verbose(err)
    deciding = re.search(r"FAIL: test_contradiction \([\w.]+\) \(subcase='page1_then_page_no2'\)\n-+\n"
                         r"(?:.*\n)*?AssertionError: Lists differ: \[1, 1\] != \[0, 0\]", err)
    by_name = {}
    for item in parsed["results"]:
        if item["subtest"] is None:
            by_name[item["test"].rsplit(".", 1)[-1]] = item["outcome"]
    record["phases"]["baseline_red"] = {
        "scope": "implementation baseline red; NOT final acceptance", "clone_head": git("rev-parse", "HEAD", cwd=clone)[1],
        "document_sha256": base_doc, "exit": code, "ran": parsed["ran"], "final": parsed["final"],
        "contradiction_counts_one_and_one": deciding is not None, "ordinary_control": by_name.get("test_valid_aliases"),
        "results": parsed["results"]}
    if code == 0 or not deciding:
        problems.append("baseline_red_not_reproduced")
    if by_name.get("test_valid_aliases") != "ok" or by_name.get("test_fixture_declares_the_frozen_matrix") != "ok":
        problems.append("baseline_ordinary_control_not_green")
    if "errors=" in (parsed["final"] or "") or re.search(r"(?m)^ERROR: ", err):
        problems.append("baseline_has_errors_not_failures")
    # 2. candidate matrix under scoped sentinels.
    closure = record["closure"] = static_closure()
    if closure["non_stdlib"] or closure["network"]:
        problems.append("import_closure_not_offline_stdlib")
    ids = expected_ids()
    code, base_blob, _ = run_cmd(["git", "-C", str(REPO), "show", base + ":" + DOCUMENT], REPO)
    if code != 0 or ORIGINAL_TUPLE not in base_blob.decode("utf-8"):
        problems.append("original_tuple_membership_not_in_base")
    source = (REPO / DOCUMENT).read_text(encoding="utf-8")
    patches = scoped_sentinels(run / "tmp")
    for patch in patches:
        patch.start()
    try:
        if str(REPO) not in sys.path:
            sys.path.insert(0, str(REPO))
        document = importlib.import_module("campaign_tool.records.understanding.document")
        tests = importlib.import_module(MODULE)
        loaded = {"document": os.path.realpath(document.__file__), "tests": os.path.realpath(tests.__file__)}
        record["loaded"] = loaded
        if any(not p.startswith(str(REPO) + os.sep) for p in loaded.values()):
            problems.append("module_loaded_outside_repo")
        record["case_map"] = getattr(tests, "CASE_MAP", None)
        if record["case_map"] != {case: "test_" + case for case in FROZEN_CASES}:
            problems.append("case_map_mismatch")
        loader = unittest.TestLoader()
        positive = run_suite(loader.loadTestsFromName(MODULE), "candidate", run, record)
        if not clean(positive, ids):
            problems.append("candidate_matrix_not_clean")
        record["subtests"] = {"expected": expected_subtests(), "observed": subtest_counts(positive)}
        if record["subtests"]["expected"] != record["subtests"]["observed"]:
            problems.append("subtest_matrix_incomplete")
        # 3. isolated mutants of the post-fix _coverage, each altering one named gate only.
        original = document._coverage
        mutants = record["mutants"] = {}
        specs = (("membership", FIXED_MEMBERSHIP, MUTANT_MEMBERSHIP, "test_contradiction", "Lists differ: [1, 1] != [0, 0]"),
                 ("gap", FIXED_GAP, "pass", "test_mixed_and_nonpage", "'" + GAP + "'"))
        for label, old, new, target, needle in specs:
            mutant, mutated_sha = build_mutant(source, old, new, label)
            names = [MODULE + "." + CLASS + "." + target, MODULE + "." + CLASS + ".test_valid_aliases"]
            with mock.patch.object(document, "_coverage", mutant._coverage):
                summary = run_suite(loader.loadTestsFromNames(names), "mutant-" + label, run, record)
            failing = [e for e in summary["events"] if e["test"].endswith("." + target) and e["outcome"] == "fail"]
            valid_ok = [e for e in summary["events"] if e["test"].endswith(".test_valid_aliases")]
            detected = bool(failing) and any(needle in (e["message"] or "") for e in failing)
            mutants[label] = {"replaced": old, "with": new, "mutated_source_sha256": mutated_sha,
                              "deciding_test": target, "needle": needle, "detected": detected,
                              "failing_subtests": [e["subtest"] for e in failing],
                              "valid_control_ok": bool(valid_ok) and all(e["outcome"] == "ok" for e in valid_ok),
                              "errors": summary["errors"]}
            if not detected or summary["errors"] or not mutants[label]["valid_control_ok"]:
                problems.append("mutant_not_detected_exactly:" + label)
            if label == "membership":
                decided = [e for e in failing if "page1_then_page_no2" in (e["subtest"] or "")]
                if not decided or needle not in (decided[0]["message"] or ""):
                    problems.append("membership_mutant_deciding_subcase_missing")
            if document._coverage is not original:
                problems.append("coverage_not_restored:" + label)
        restored = run_suite(loader.loadTestsFromName(MODULE), "restored", run, record)
        if not clean(restored, ids):
            problems.append("restored_matrix_not_clean")
    finally:
        for patch in reversed(patches):
            patch.stop()
    record["sentinel_hits"] = list(HITS)
    if HITS:
        problems.append("sentinel_hits")
    if sha256_file(REPO / DOCUMENT) != record["identity_initial"]["sha256"][DOCUMENT]:
        problems.append("document_bytes_changed_during_suite")


def phase_compatibility(run, record, manifest):
    problems = record["problems"]
    defined = sorted(COMPAT_SELECTOR + "." + n for n in defined_tests(REPO / COMPAT_FILE, "FcDocCoverageTests"))
    outcomes = {}
    clone = clone_at(run, record, "original", manifest["head"])
    if clone is None:
        problems.append("original_clone_failed")
        return
    for label, root in (("original", clone), ("final", REPO)):
        code, _, err = child(run, record, "compat-" + label, [sys.executable, "-B", "-m", "unittest", "-v", COMPAT_SELECTOR],
                             root, env=python_env(run / ("tmp-" + label)))
        parsed = parse_verbose(err)
        # unittest -v prints "name (module.Class.name)"; normalize to selector ids.
        normalized, sub_failures = {}, []
        for item in parsed["results"]:
            if item["subtest"] is not None:
                sub_failures.append(item)
                continue
            normalized[COMPAT_SELECTOR + "." + item["test"].rsplit(".", 1)[-1]] = item["outcome"]
        outcomes[label] = normalized
        record["phases"][label] = {"exit": code, "ran": parsed["ran"], "final": parsed["final"],
                                   "skipped_mentioned": parsed["skipped_mentioned"], "ids": sorted(normalized),
                                   "outcomes": normalized, "subtest_lines": sub_failures}
        if code != 0 or parsed["ran"] != len(defined) or not (parsed["final"] or "").startswith("OK") \
                or parsed["skipped_mentioned"] or sorted(normalized) != defined or sub_failures \
                or any(v != "ok" for v in normalized.values()):
            problems.append("compatibility_not_clean:" + label)
    deltas = sorted(k for k in set(outcomes["original"]) | set(outcomes["final"])
                    if outcomes["original"].get(k) != outcomes["final"].get(k))
    record["behavior_deltas"] = deltas
    if deltas:
        problems.append("compatibility_behavior_delta")
    record["limits_compat"] = "Local compatibility gate only; full discovery and required CI are operator-run."


def phase_portable(run, record, manifest, args):
    problems = record["problems"]
    markers = {}
    for rel in (TEST_FILE, FIXTURE_FILE, RECORDER):
        text = (REPO / rel).read_text(encoding="utf-8")
        markers[rel] = [m for m in BOX_MARKERS if m in text]
    record["box_markers"] = markers
    if any(markers.values()):
        problems.append("machine_specific_markers")
    ids = defined_tests(REPO / TEST_FILE, CLASS)
    cwd = run / "unrelated-cwd"
    cwd.mkdir()
    temp_root = run / "script-temp-root"
    temp_root.mkdir()
    code, _, err = child(run, record, "script", [sys.executable, "-B", str(REPO / TEST_FILE), "--temp-root", str(temp_root)],
                         cwd, env=python_env(run / "script-env-tmp"))
    parsed = parse_verbose(err)
    names = sorted({r["test"].rsplit(".", 1)[-1] for r in parsed["results"]})
    # Verbose unittest prints passing subtests silently, so every frozen subcase is
    # covered by its enclosing test reporting ok; per-subtest counts are recorded
    # in the suite phase from the result object.
    record["phases"]["script"] = {"exit": code, "cwd": str(cwd), "ran": parsed["ran"], "final": parsed["final"],
                                  "tests": names, "results": parsed["results"],
                                  "main_module_ids": bool(parsed["results"]) and all(
                                      r["test"].startswith("__main__.") for r in parsed["results"])}
    if code != 0 or parsed["ran"] != len(ids) or names != sorted(ids) or parsed["skipped_mentioned"] \
            or not (parsed["final"] or "").startswith("OK") or any(r["outcome"] != "ok" for r in parsed["results"]) \
            or any(r["subtest"] is not None for r in parsed["results"]) \
            or not record["phases"]["script"]["main_module_ids"]:
        problems.append("script_run_not_clean")
    report = run / "discovery-report.json"
    code, _, _ = child(run, record, "discovery", [sys.executable, "-B", str(REPO / RECORDER), "--phase", "portable",
                                                  "--repo", str(args.repo), "--manifest", str(args.manifest),
                                                  "--out-base", str(args.out_base), "--discovery-child", str(report)],
                       REPO, env=python_env(run / "discovery-tmp"))
    data = json.loads(report.read_text(encoding="utf-8")) if report.is_file() else None
    record["phases"]["discovery"] = data
    if code != 0 or not data or not data.get("passed"):
        problems.append("discovery_isolation_failed")


SNAPSHOT_KEYS = ("environ", "tempdir", "gettempdir", "sys_path", "cwd", "meta_path", "socket_connect", "popen_init")


def snapshot():
    return {"environ": dict(os.environ), "tempdir": tempfile.tempdir, "gettempdir": tempfile.gettempdir(),
            "sys_path": list(sys.path), "cwd": os.getcwd(), "meta_path": len(sys.meta_path),
            "socket_connect": id(socket.socket.connect), "popen_init": id(subprocess.Popen.__init__)}


class PeerSentinel(unittest.TestCase):
    """Inert peer: records process state before and checks it after the discovered module."""
    state = {}

    def test_peer_before(self):
        PeerSentinel.state["before"] = snapshot()
        self.assertEqual(json.loads(json.dumps({"peer": [1, 2]})), {"peer": [1, 2]})

    def test_peer_after(self):
        after = snapshot()
        PeerSentinel.state["after"] = after
        before = PeerSentinel.state.get("before")
        self.assertIsNotNone(before)
        for key in SNAPSHOT_KEYS:
            self.assertEqual(after[key], before[key], key)


def discovery_child(report_path):
    loader = unittest.TestLoader()
    discovered = loader.discover(start_dir=str(REPO / "tests" / "records"), pattern="test_fc16_document_locator.py",
                                 top_level_dir=str(REPO))
    ids = [t.id() for t in flatten(discovered)]  # before running: a run releases finished tests
    suite = unittest.TestSuite([PeerSentinel("test_peer_before"), discovered, PeerSentinel("test_peer_after")])
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=2, resultclass=RecordingResult).run(suite)
    module = sys.modules.get(MODULE)
    loaded = os.path.realpath(module.__file__) if module else None
    expected = sorted(expected_ids())
    passed = (result.wasSuccessful() and not result.skipped and sorted(ids) == expected
              and result.testsRun == len(expected) + 2 and loaded == str(REPO / TEST_FILE))
    report = {"passed": passed, "discovered_ids": sorted(ids), "expected_ids": expected, "tests_run": result.testsRun,
              "failures": [t.id() for t, _ in result.failures], "errors": [t.id() for t, _ in result.errors],
              "skipped": [t.id() for t, _ in result.skipped], "loaded_test_module": loaded,
              "peer_state_keys": list(SNAPSHOT_KEYS), "events": result.events,
              "unittest_output": stream.getvalue()}
    with open(report_path, "x", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
    return 0 if passed else 1


def phase_credentials(run, record, manifest):
    problems = record["problems"]
    runtime = manifest.get("runtime") or {}
    declared = runtime.get("credential_scan_python3")
    record["interpreter_check"] = {"sys_executable": sys.executable, "manifest": declared,
                                   "realpath_equal": os.path.realpath(sys.executable) == os.path.realpath(declared or "")}
    if sys.executable != declared:
        problems.append("interpreter_not_manifest_credential_scan_python3")
    scanner = runtime.get("detect_secrets")
    if not scanner or not os.access(scanner, os.X_OK):
        problems.append("detect_secrets_unavailable")
        return
    code, out, _ = child(run, record, "secret-scan", [scanner, "scan", "--all-files", "--exclude-files", "^.git/",
                                                      "--no-verify"], REPO)
    report = run / "secret-scan.json"
    report.write_bytes((run / "secret-scan.stdout").read_bytes())
    try:
        results = json.loads(out)["results"]
        if not isinstance(results, dict):
            raise ValueError("results not an object")
        record["scan_summary"] = {"files_with_hits": sorted(results), "raw_hits": sum(len(v) for v in results.values())}
    except (ValueError, KeyError, TypeError) as exc:
        problems.append("secret_report_malformed:" + type(exc).__name__)
        return
    if code != 0:
        problems.append("detect_secrets_exit_nonzero")
    code2, out2, _ = child(run, record, "check-secret-report", [sys.executable, str(REPO / "tools" / "check_secret_report.py"),
                                                                str(report)], REPO)
    record["checker_stdout"] = out2[-2000:]
    if code2 != 0:
        problems.append("check_secret_report_exit_nonzero")


def phase_public_tree(run, record, manifest):
    problems = record["problems"]
    status = record["identity_initial"]["status_porcelain_ignored"]
    lines = [line for line in (status or "").splitlines() if line]
    record["status_lines"] = lines
    if status is None or any(line != "!! .venv" for line in lines):
        problems.append("untracked_or_modified_entries_present")
    for rel in AUTHORED:
        if git("ls-files", "--error-unmatch", rel)[0] != 0:
            problems.append("authored_not_tracked:" + rel)
    if git("diff", "--quiet")[0] != 0 or git("diff", "--cached", "--quiet")[0] != 0:
        problems.append("index_or_worktree_differs_from_head")
    head, tree = record["identity_initial"]["head"], record["identity_initial"]["tree"]
    clone = clone_at(run, record, "committed-tree", head)
    if clone is None:
        problems.append("committed_tree_clone_failed")
        return
    clone_head, clone_tree = git("rev-parse", "HEAD", cwd=clone)[1], git("rev-parse", "HEAD^{tree}", cwd=clone)[1]
    links = [str(p.relative_to(clone)) for p in clone.rglob("*") if p.is_symlink() and ".git" not in p.relative_to(clone).parts]
    hashes = {rel: sha256_file(clone / rel) for rel in AUTHORED + CHECKERS}
    record["clone"] = {"path": str(clone), "head": clone_head, "tree": clone_tree, "symlinks": links, "sha256": hashes}
    if clone_head != head or clone_tree != tree:
        problems.append("clone_identity_mismatch")
    if links:
        problems.append("clone_contains_links")
    if any(hashes[rel] != record["identity_initial"]["sha256"][rel] for rel in hashes):
        problems.append("clone_file_hash_mismatch")
    code, out, _ = child(run, record, "public-tree-clone", [sys.executable, str(clone / "tools" / "check_public_tree.py")],
                         clone, env=python_env(run / "public-tree-tmp"))
    try:
        findings = json.loads(out)["findings"]
    except (ValueError, KeyError, TypeError):
        findings = None
    record["committed_tree_findings"] = findings
    if code != 0 or findings != []:
        problems.append("committed_tree_public_check_failed")
    code, out, _ = child(run, record, "public-tree-in-place", [sys.executable, str(REPO / "tools" / "check_public_tree.py")],
                         REPO, env=python_env(run / "public-tree-inplace-tmp"))
    try:
        in_place = json.loads(out)["findings"]
    except (ValueError, KeyError, TypeError):
        in_place = None
    venv_ok = record["preflight"]["venv_link"] is not None
    recognized = (code == 0 and in_place == []) or (code == 1 and venv_ok and in_place == [[".venv", 0, "symlink_not_scanned"]])
    record["in_place_informational"] = {"label": "in_place_informational (INC-174)", "exit": code, "findings": in_place,
                                        "recognized": recognized}
    if not recognized:
        problems.append("in_place_public_check_unrecognized")


def tree_digest(root):
    digest = hashlib.sha256()
    for directory, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not (Path(directory) == Path(root) and d == ".git"))
        for name in sorted(files) + sorted(d + "/" for d in dirs):
            path = Path(directory) / name.rstrip("/")
            rel = path.relative_to(root).as_posix()
            if path.is_symlink():
                digest.update(b"L" + rel.encode() + b"\0" + os.readlink(path).encode() + b"\0")
            elif name.endswith("/"):
                digest.update(b"D" + rel.encode() + b"\0")
            else:
                digest.update(b"F" + rel.encode() + b"\0" + hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description="FC16-DOCUMENT-LOCATOR acceptance recorder (offline)")
    parser.add_argument("--phase", required=True, choices=PHASES)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--out-base", required=True)
    parser.add_argument("--discovery-child", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.discovery_child:
        if args.phase != "portable":
            parser.error("--discovery-child is internal to the portable phase")
        return discovery_child(args.discovery_child)
    out_base = Path(args.out_base)
    if not out_base.is_absolute():
        parser.error("--out-base must be absolute")
    resolved = Path(os.path.realpath(out_base))
    if resolved == REPO or REPO in resolved.parents:
        parser.error("--out-base must be outside the repository")
    resolved.mkdir(parents=True, exist_ok=True)
    run = resolved / ("%s-%s" % (args.phase, uuid.uuid4()))
    run.mkdir(mode=0o700, exist_ok=False)
    record = {"schema": SCHEMA, "card": CARD, "phase": args.phase, "argv": [sys.executable, *sys.argv],
              "cwd": os.getcwd(), "interpreter": sys.executable, "python": platform.python_version(),
              "started": now(), "run_dir": str(run), "repo": str(REPO), "phases": {}, "children": {}, "problems": [],
              "limits": ["MACHINE CHECK ONLY - REVIEW REQUIRED; not semantic, factual, legal or release acceptance.",
                         "Full repository discovery and required CI on the exact head are operator-run.",
                         "Sentinels are scoped and restored; they are not a hostile-code sandbox.",
                         "Synthetic fixtures only; no records, network, model calls or live actions."]}
    problems = record["problems"]
    try:
        manifest, record["manifest"] = load_manifest(args.manifest, problems)
        record["preflight"] = preflight(manifest, args, problems)
        record["identity_initial"] = identity()
        record["repo_tree_before"] = tree_digest(REPO)
        handlers = {"suite": lambda: phase_suite(run, record, manifest),
                    "compatibility": lambda: phase_compatibility(run, record, manifest),
                    "portable": lambda: phase_portable(run, record, manifest, args),
                    "credentials": lambda: phase_credentials(run, record, manifest),
                    "public-tree": lambda: phase_public_tree(run, record, manifest)}
        if not problems:
            handlers[args.phase]()
        else:
            problems.append("preflight_failed_phase_not_run")
        record["identity_final"] = identity()
        record["repo_tree_after"] = tree_digest(REPO)
        if record["identity_final"] != record["identity_initial"]:
            problems.append("identity_changed_during_run")
        if record["repo_tree_after"] != record["repo_tree_before"]:
            problems.append("repository_tree_changed_during_run")
    except Exception as exc:  # an incomplete row is a failure, never a pass
        problems.append("incomplete:%s:%s" % (type(exc).__name__, str(exc)[:300]))
    record["finished"] = now()
    record["verdict"] = "REVIEW_REQUIRED" if not problems else "FAIL"
    record["exit"] = 0 if not problems else 1
    payload = (json.dumps(record, indent=2, sort_keys=True, default=str) + "\n").encode("utf-8")
    (run / "evidence.json").write_bytes(payload)
    final = record.get("identity_final") or {}
    print(json.dumps({"phase": args.phase, "verdict": record["verdict"], "problems": problems, "run_dir": str(run),
                      "head": final.get("head"), "tree": final.get("tree"),
                      "manifest_sha256": (record.get("manifest") or {}).get("sha256"),
                      "evidence_sha256": sha256_bytes(payload)}, indent=2, sort_keys=True))
    return record["exit"]


if __name__ == "__main__":
    raise SystemExit(main())
