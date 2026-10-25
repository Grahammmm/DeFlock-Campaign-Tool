"""FC9-HEADER-CITATIONS acceptance runner (five separately invoked rows).

Usage: <staged python3> tools/fc9_header_citations_acceptance.py --phase PHASE
       --source-manifest MANIFEST.json --out-base OUT_DIR

Each invocation creates a fresh UUID row directory below --out-base and records
argv/cwd/interpreter, exit, raw stdout/stderr, candidate HEAD/tree, fixture and
source hashes, test/case counts and artifact hashes. Every child command is a
literal argument list with the shell disabled. The repository is derived from
this file. Machine-valid output is REVIEW REQUIRED only: never semantic, legal,
human or live acceptance, and never publication authority.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import types
import unittest
import uuid

HERE = Path(__file__).resolve()
REPO = HERE.parents[1]
PHASES = ("suite", "compatibility", "portable", "credentials", "public-tree")
MODULE = "tests.records.test_fc9_header_citations"
PRODUCT = "campaign_tool/records/understanding/correspondence.py"
TEST_FILE = "tests/records/test_fc9_header_citations.py"
FIXTURE = "tests/fixtures/fc9-header-citations/cases.json"
RUNNER = "tools/fc9_header_citations_acceptance.py"
AUTHORED = (PRODUCT, TEST_FILE, FIXTURE, RUNNER)
UNCHANGED_CHECKERS = ("tools/check_public_tree.py", "campaign_tool/records/public_scan.py",
                      "tools/check_secret_report.py", "campaign_tool/records/grounded_evidence.py",
                      "campaign_tool/records/understanding/evidence.py", ".gitignore")
COMPAT_SELECTORS = ("tests.records.test_correspondence", "tests.records.test_understanding.GroundedEvidenceTests")
SENTINEL_TEST = "tests.records.test_understanding.EvidenceTests.test_receipt_normalizes_to_pinned_bundle"
ALLOWED_INPLACE_FINDING = [".venv", 0, "symlink_not_scanned"]

REQUIRED_TESTS = tuple("%s.%s" % pair for pair in (
    ("AddressableHeaderTests", "test_every_addressable_representation_cites_exact_pointer"),
    ("AddressableHeaderTests", "test_dict_scalar"),
    ("AddressableHeaderTests", "test_dict_list"),
    ("AddressableHeaderTests", "test_dict_object"),
    ("AddressableHeaderTests", "test_dict_object_empty_value_uses_raw"),
    ("AddressableHeaderTests", "test_dict_list_object"),
    ("AddressableHeaderTests", "test_lower_case_dict_keys"),
    ("AddressableHeaderTests", "test_pair_array_compatibility_positive"),
    ("AddressableHeaderTests", "test_object_list"),
    ("AddressableHeaderTests", "test_body_only_text_has_no_header_values"),
    ("PointerTokenTests", "test_pointer_token_escapes_tilde_then_slash"),
    ("PointerTokenTests", "test_escaped_tokens_resolve_in_retained_data"),
    ("PointerTokenTests", "test_header_rows_carry_escaped_pointers"),
    ("PointerTokenTests", "test_raw_header_string_rows_have_no_pointer"),
    ("PointerTokenTests", "test_missing_pointer_path_is_unlocatable_not_fatal"),
    ("PointerTokenTests", "test_pointed_scalar_must_equal_value"),
    ("UnresolvedHeaderTests", "test_raw_header_strings_hold_and_stay_unknown"),
    ("UnresolvedHeaderTests", "test_stripped_dict_value_holds"),
    ("UnresolvedHeaderTests", "test_stripped_pair_value_holds"),
    ("UnresolvedHeaderTests", "test_unresolved_original_does_not_abort_valid_peer"),
    ("ConflictAndBoundaryTests", "test_contradictory_duplicate_dates_hold"),
    ("ConflictAndBoundaryTests", "test_contradictory_duplicate_ids_hold"),
    ("ConflictAndBoundaryTests", "test_same_instant_duplicates_are_one_date"),
    ("ConflictAndBoundaryTests", "test_missing_date_and_id_stay_unknown"),
    ("ConflictAndBoundaryTests", "test_timezone_unknown_clock_is_cited_but_not_ordered"),
    ("TextBackedCompatibilityTests", "test_text_backed_dict_headers_keep_text_span"),
    ("TextBackedCompatibilityTests", "test_text_backed_raw_strings_keep_text_span"),
    ("NegativeControlTests", "test_declared_negative_control_legacy_pointer_seam"),
))

CASE_MAP = {
    "dictionary scalar leaves": ["AddressableHeaderTests.test_dict_scalar",
                                 "AddressableHeaderTests.test_lower_case_dict_keys"],
    "dictionary list leaves": ["AddressableHeaderTests.test_dict_list"],
    "dictionary value/raw object leaves": ["AddressableHeaderTests.test_dict_object",
                                           "AddressableHeaderTests.test_dict_object_empty_value_uses_raw",
                                           "AddressableHeaderTests.test_dict_list_object"],
    "pair arrays (compatibility positive)": ["AddressableHeaderTests.test_pair_array_compatibility_positive"],
    "list objects": ["AddressableHeaderTests.test_object_list"],
    "exact json_pointer, quote, original/import/artifact binding, date/ID through events_from_bundle then track":
        ["AddressableHeaderTests.test_every_addressable_representation_cites_exact_pointer"],
    "body-only text, percent-free ordinary request ID": ["AddressableHeaderTests.test_body_only_text_has_no_header_values"],
    "escaped pointer-key helper": ["PointerTokenTests.test_pointer_token_escapes_tilde_then_slash",
                                   "PointerTokenTests.test_escaped_tokens_resolve_in_retained_data",
                                   "PointerTokenTests.test_header_rows_carry_escaped_pointers"],
    "verify pointed scalar matches value": ["PointerTokenTests.test_pointed_scalar_must_equal_value"],
    "missing paths": ["PointerTokenTests.test_missing_pointer_path_is_unlocatable_not_fatal",
                      "ConflictAndBoundaryTests.test_missing_date_and_id_stay_unknown"],
    "non-addressable raw header string -> hold, unknown": ["PointerTokenTests.test_raw_header_string_rows_have_no_pointer",
                                                           "UnresolvedHeaderTests.test_raw_header_strings_hold_and_stay_unknown"],
    "stripped/transformed value -> hold, unknown": ["UnresolvedHeaderTests.test_stripped_dict_value_holds",
                                                    "UnresolvedHeaderTests.test_stripped_pair_value_holds"],
    "valid peer source events survive": ["UnresolvedHeaderTests.test_unresolved_original_does_not_abort_valid_peer"],
    "duplicate contradictory headers": ["ConflictAndBoundaryTests.test_contradictory_duplicate_dates_hold",
                                        "ConflictAndBoundaryTests.test_contradictory_duplicate_ids_hold",
                                        "ConflictAndBoundaryTests.test_same_instant_duplicates_are_one_date"],
    "valid clocks / clock boundary": ["AddressableHeaderTests.test_every_addressable_representation_cites_exact_pointer",
                                      "ConflictAndBoundaryTests.test_timezone_unknown_clock_is_cited_but_not_ordered"],
    "ordinary text-backed headers (exact text-span fallback)": [
        "TextBackedCompatibilityTests.test_text_backed_dict_headers_keep_text_span",
        "TextBackedCompatibilityTests.test_text_backed_raw_strings_keep_text_span"],
    "declared synthetic negative control": ["NegativeControlTests.test_declared_negative_control_legacy_pointer_seam",
                                            "runner suite child: control + pinned-baseline observation"],
}

# Host-specific and inline-interpreter markers, assembled so this file does not match itself.
FORBIDDEN_MARKERS = tuple(m.encode() for m in (
    "/" + "home/", "/" + "Users/", "agent" + "-work", "venv" + "-fullsuite", "/" + "root/", "/" + "tmp/",
    "python3 -" + "c", "python -" + "c", "<" + "<EOF", "shell" + "=True", "os." + "system(", "PYTHON" + "PATH",
))


def sha_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha_file(path):
    return sha_bytes(Path(path).read_bytes())


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def write_new(path, data):
    with open(path, "xb") as handle:
        handle.write(data if isinstance(data, bytes) else data.encode("utf-8"))


def write_json(path, value):
    write_new(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def child_env(extra=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "LD_"))}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.update(extra or {})
    return env


class Row:
    def __init__(self, phase, out_base):
        base = Path(out_base)
        if not base.is_absolute():
            raise ValueError("out_base_must_be_absolute")
        base.mkdir(parents=True, exist_ok=True)
        self.dir = base / ("%s-%s" % (phase, uuid.uuid4().hex))
        self.dir.mkdir(exist_ok=False)
        self.tmp = self.dir / "tmp"
        self.tmp.mkdir()
        tempfile.tempdir = str(self.tmp)
        self.children = []
        self.count = 0

    def child(self, name, argv, cwd, env=None, stdout_path=None, timeout=1800):
        self.count += 1
        stem = "%02d-%s" % (self.count, name)
        out_path = Path(stdout_path) if stdout_path else self.dir / (stem + ".stdout")
        err_path = self.dir / (stem + ".stderr")
        with open(out_path, "xb") as out, open(err_path, "xb") as err:
            try:
                proc = subprocess.run([str(a) for a in argv], cwd=str(cwd), stdout=out, stderr=err,
                                      env=env if env is not None else child_env(), shell=False,
                                      stdin=subprocess.DEVNULL, timeout=timeout)
                code, launch_error = proc.returncode, None
            except (OSError, subprocess.SubprocessError) as exc:
                code, launch_error = None, "%s: %s" % (type(exc).__name__, exc)
        record = {"name": stem, "argv": [str(a) for a in argv], "cwd": str(cwd), "exit": code,
                  "launch_error": launch_error, "stdout": str(out_path), "stdout_sha256": sha_file(out_path),
                  "stderr": str(err_path), "stderr_sha256": sha_file(err_path)}
        self.children.append(record)
        return record


def git(*args, cwd=REPO):
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, shell=False,
                          stdin=subprocess.DEVNULL, timeout=120)
    return proc.returncode, proc.stdout.decode("utf-8", "replace"), proc.stderr.decode("utf-8", "replace")


def git_out(*args, cwd=REPO):
    code, out, _ = git(*args, cwd=cwd)
    return out.strip() if code == 0 else None


def load_manifest(path):
    manifest_path = Path(path)
    if not manifest_path.is_absolute():
        raise ValueError("manifest_path_must_be_absolute")
    raw = manifest_path.read_bytes()
    return json.loads(raw), sha_bytes(raw)


def identity(manifest):
    """Check manifest/base/runtime identity; returns (problems, detail)."""
    problems = []
    runtime = manifest.get("runtime") if isinstance(manifest.get("runtime"), dict) else {}
    if manifest.get("schema") != "agentorange.source-manifest/v1":
        problems.append("manifest_schema")
    if not isinstance(manifest.get("repo"), str) or Path(manifest["repo"]).resolve() != REPO:
        problems.append("manifest_repo_mismatch")
    base = manifest.get("head")
    if not isinstance(base, str) or not re.fullmatch(r"[0-9a-f]{40}", base):
        problems.append("manifest_head_invalid")
        base = None
    head = git_out("rev-parse", "HEAD")
    tree = git_out("rev-parse", "HEAD^{tree}")
    base_tree = git_out("rev-parse", base + "^{tree}") if base else None
    if base and base_tree != manifest.get("tree"):
        problems.append("manifest_tree_mismatch")
    if base and head == base:
        problems.append("head_is_base_not_candidate")
    if base and git("merge-base", "--is-ancestor", base, "HEAD")[0] != 0:
        problems.append("head_not_descended_from_base")
    commits = []
    if base:
        log = git_out("log", "--format=%H%x1f%an%x1f%(trailers:key=Signed-off-by,valueonly)%x1e", base + "..HEAD") or ""
        for entry in [e.strip() for e in log.split("\x1e") if e.strip()]:
            parts = entry.split("\x1f")
            signed = len(parts) > 2 and parts[2].strip() != ""
            commits.append({"commit": parts[0], "author": parts[1] if len(parts) > 1 else None, "signed_off": signed})
            if not signed:
                problems.append("commit_not_signed_off:" + parts[0])
    venv_link = REPO / ".venv"
    repo_venv = runtime.get("repo_venv")
    venv_ok = venv_link.is_symlink() and isinstance(repo_venv, str) and venv_link.resolve() == Path(repo_venv).resolve()
    if not venv_ok:
        problems.append("staged_venv_link_mismatch")
    detail = {"base": base, "base_tree": base_tree, "head": head, "tree": tree, "commits": commits,
              "venv_link": os.readlink(venv_link) if venv_link.is_symlink() else None,
              "venv_link_matches_manifest": venv_ok}
    return problems, detail


def interpreter_problems(phase, manifest):
    runtime = manifest.get("runtime") if isinstance(manifest.get("runtime"), dict) else {}
    if phase == "credentials":
        expected = runtime.get("credential_scan_python3")
        if not isinstance(expected, str) or sys.executable != expected:
            return ["interpreter_not_manifest_credential_scan_python3"]
        return []
    if sys.executable != str(REPO / ".venv" / "bin" / "python3"):
        return ["interpreter_not_staged_repo_venv"]
    return []


def unittest_child(row, name, mode, extra, cwd=REPO):
    result_path = row.dir / (name + ".result.json")
    argv = [sys.executable, str(HERE), "--child", mode, "--child-result", str(result_path),
            "--tmp-root", str(row.tmp / name), *extra]
    rec = row.child(name, argv, cwd)
    try:
        rec["result"] = json.loads(result_path.read_text())
        rec["result_sha256"] = sha_file(result_path)
    except (OSError, ValueError):
        rec["result"] = None
    return rec


def clean(result, required, prefix=""):
    problems = []
    if not result:
        return ["missing_child_result"]
    if result.get("tests_run", 0) <= 0:
        problems.append("zero_tests")
    for kind in ("failures", "errors", "skipped", "expected_failures", "unexpected_successes"):
        if result.get(kind):
            problems.append("%s:%s" % (kind, ",".join(result[kind])))
    passed = set(result.get("passed", []))
    missing = sorted(t for t in required if prefix + t not in passed)
    if missing:
        problems.append("required_not_passed:" + ",".join(missing))
    return problems


def fixture_dimensions():
    cases = json.loads((REPO / FIXTURE).read_text(encoding="utf-8"))
    groups = ("addressable_cases", "unresolved_cases", "conflict_cases", "boundary_cases",
              "text_backed_cases", "pointer_token_cases", "escaped_key_cases")
    dims = {group: len(cases[group]) for group in groups}
    dims["addressable_shapes"] = sorted({case["shape"] for case in cases["addressable_cases"]})
    dims["total_cases"] = sum(dims[group] for group in groups)
    return dims


def phase_suite(row, manifest):
    problems = []
    base = manifest.get("head")
    baseline_path = row.dir / "pinned-baseline-correspondence.py"
    code, out, err = git("show", "%s:%s" % (base, PRODUCT))
    if code != 0:
        problems.append("baseline_source_unavailable")
    else:
        write_new(baseline_path, out.encode("utf-8"))
    rec = unittest_child(row, "suite", "suite", ["--baseline-source", str(baseline_path)])
    res = rec["result"]
    if rec["exit"] != 0:
        problems.append("child_exit:%s" % rec["exit"])
    problems += clean(res, REQUIRED_TESTS, MODULE + ".")
    if res:
        control = res.get("control") or {}
        if control.get("detected") is not True:
            problems.append("control_not_detected")
        if control.get("pair_under_injection") != "passed" or control.get("restored_dict_and_pair") != "passed":
            problems.append("control_positive_not_green")
        if control.get("restored_is_original") is not True:
            problems.append("control_not_restored")
        baseline = res.get("baseline") or {}
        if baseline.get("dict_scalar") != "ValueError: evidence_pointer_missing":
            problems.append("baseline_defect_not_observed")
        if baseline.get("pair_array") != "passed":
            problems.append("baseline_pair_array_not_green")
        if baseline.get("source_sha256") != (sha_file(baseline_path) if baseline_path.exists() else None):
            problems.append("baseline_source_hash_mismatch")
        if res.get("candidate_after_control") != "passed":
            problems.append("candidate_after_control_not_green")
    return problems, {"suite": res, "fixture_dimensions": fixture_dimensions(), "case_map": CASE_MAP,
                      "required_tests": list(REQUIRED_TESTS),
                      "baseline_source": str(baseline_path),
                      "baseline_source_sha256": sha_file(baseline_path) if baseline_path.exists() else None}


def phase_compatibility(row, manifest):
    rec = unittest_child(row, "compatibility", "compat", list(COMPAT_SELECTORS))
    problems = [] if rec["exit"] == 0 else ["child_exit:%s" % rec["exit"]]
    problems += clean(rec["result"], ())
    return problems, {"compatibility": rec["result"], "selectors": list(COMPAT_SELECTORS),
                      "scope": "named selectors only; not full repository CI (exact-head CI OWED to shipping operator)"}


def parse_verbose(stderr_text):
    passed, other = [], []
    for line in stderr_text.splitlines():
        match = re.match(r"(test\w+) \(([\w.]+)\)\s*\.\.\.\s*(.*)$", line)
        if match:
            ident = match.group(2).split(".")
            ident = ".".join(ident[-2:]) if ident[-1] == match.group(1) else ident[-1] + "." + match.group(1)
            (passed if match.group(3).strip() == "ok" else other).append(ident)
    ran = re.search(r"^Ran (\d+) tests? in", stderr_text, re.M)
    return passed, other, int(ran.group(1)) if ran else None, bool(re.search(r"^OK\s*$", stderr_text, re.M))


def phase_portable(row, manifest):
    problems, static = [], {}
    for rel in (TEST_FILE, FIXTURE, RUNNER):
        data = (REPO / rel).read_bytes()
        hits = sorted(m.decode() for m in FORBIDDEN_MARKERS if m in data)
        static[rel] = hits
        if hits:
            problems.append("host_literal:%s:%s" % (rel, ",".join(hits)))
    cwd = row.dir / ("fresh-cwd-" + uuid.uuid4().hex)
    cwd.mkdir()
    script_tmp = row.dir / "script-tmp"
    rec = row.child("script", [sys.executable, str(REPO / TEST_FILE), "--tmp-root", str(script_tmp), "-v"], cwd)
    stderr_text = Path(rec["stderr"]).read_text(encoding="utf-8", errors="replace")
    passed, other, ran, ok = parse_verbose(stderr_text)
    if rec["exit"] != 0 or not ok:
        problems.append("script_not_ok:%s" % rec["exit"])
    missing = sorted(set(REQUIRED_TESTS) - set(passed))
    if missing:
        problems.append("script_required_missing:" + ",".join(missing))
    if other:
        problems.append("script_not_ok_tests:" + ",".join(other))
    if not script_tmp.is_dir():
        problems.append("script_tmp_root_not_created")
    if sorted(os.listdir(cwd)):
        problems.append("script_wrote_into_cwd")
    disc = unittest_child(row, "discovery", "discovery", [], cwd=cwd)
    res = disc["result"]
    if disc["exit"] != 0:
        problems.append("discovery_exit:%s" % disc["exit"])
    problems += ["discovery:" + p for p in clean(res, REQUIRED_TESTS, MODULE + ".")]
    if res:
        for key in ("environ_unchanged", "tempdir_unchanged", "sys_path_unchanged", "bytecode_flag_unchanged"):
            if res.get(key) is not True:
                problems.append("discovery_contamination:" + key)
        if res.get("audit_hook_installs"):
            problems.append("discovery_installed_audit_hook")
        if res.get("sentinel_before") != "passed" or res.get("sentinel_after") != "passed":
            problems.append("sentinel_not_green")
        if res.get("module_file") != str(REPO / TEST_FILE):
            problems.append("discovered_module_outside_repo")
    coverage = {"script_passed": sorted(passed), "script_tests_run": ran,
                "discovery_passed": sorted(t[len(MODULE) + 1:] for t in (res or {}).get("passed", [])
                                           if t.startswith(MODULE + ".")),
                "discovery_tests_run": (res or {}).get("tests_run")}
    if set(REQUIRED_TESTS) - set(coverage["discovery_passed"]) or set(REQUIRED_TESTS) - set(passed):
        problems.append("required_case_coverage_unequal")
    return problems, {"static_host_literal_hits": static, "fresh_cwd": str(cwd), "coverage": coverage,
                      "discovery": res}


def phase_credentials(row, manifest):
    runtime = manifest["runtime"]
    scanner = runtime.get("detect_secrets")
    problems = []
    if not isinstance(scanner, str) or not Path(scanner).is_file():
        return ["scanner_unavailable_stop_for_operator"], {"scanner": scanner}
    if Path(scanner).parent != Path(sys.executable).parent:
        problems.append("scanner_not_in_interpreter_env")
    report = row.dir / "secret-scan.json"
    scan = row.child("detect-secrets", [scanner, "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"],
                     REPO, stdout_path=report)
    if scan["exit"] != 0:
        problems.append("scan_exit:%s" % scan["exit"])
    parsed_version, result_files = None, None
    try:
        parsed = json.loads(report.read_text())
        parsed_version = parsed.get("version")
        if not isinstance(parsed.get("results"), dict):
            problems.append("report_malformed")
        else:
            result_files = sorted(parsed["results"])
    except (OSError, ValueError, AttributeError):
        problems.append("report_malformed")
    checker = row.child("check-secret-report", [sys.executable, str(REPO / "tools" / "check_secret_report.py"),
                                                str(report)], REPO)
    if checker["exit"] != 0:
        problems.append("checker_exit:%s" % checker["exit"])
    summary = Path(checker["stdout"]).read_text(errors="replace").strip().splitlines()[-1:]
    return problems, {"scanner": scanner, "scanner_report_version": parsed_version, "report": str(report),
                      "report_sha256": sha_file(report), "report_result_files": result_files,
                      "checker_summary": summary}


def phase_public_tree(row, manifest, ident):
    problems = []
    head, tree = ident["head"], ident["tree"]
    if not head or not re.fullmatch(r"[0-9a-f]{40}", head):
        return ["candidate_head_invalid"], {}
    status = git_out("status", "--porcelain=v1", "--untracked-files=all")
    inventory = git_out("status", "--porcelain=v1", "--untracked-files=all", "--ignored")
    others = (git_out("ls-files", "-z", "--others", "--exclude-standard") or "").split("\0")
    others = sorted(o for o in others if o)
    if status != "":
        problems.append("worktree_not_clean")
    if others not in ([], [".venv"]):
        problems.append("untracked_scan_visible:" + ",".join(others))
    if git("diff", "--quiet", "HEAD")[0] != 0 or git("diff", "--cached", "--quiet")[0] != 0:
        problems.append("index_or_worktree_differs_from_head")
    authored = {}
    for rel in AUTHORED:
        head_blob = git_out("rev-parse", "HEAD:" + rel)
        index_blob = git_out("rev-parse", ":" + rel)
        work_blob = git_out("hash-object", rel)
        authored[rel] = {"head_blob": head_blob, "index_blob": index_blob, "worktree_blob": work_blob,
                         "sha256": sha_file(REPO / rel)}
        if not head_blob or head_blob != index_blob or head_blob != work_blob:
            problems.append("authored_not_committed:" + rel)
    dest = row.dir / "committed-tree"
    clone = row.child("git-clone", ["git", "clone", "--local", "--no-hardlinks", "--no-checkout",
                                    "--no-recurse-submodules", str(REPO), str(dest)], row.dir)
    if clone["exit"] != 0:
        return problems + ["clone_failed"], {"inventory": inventory}
    checkout = row.child("git-checkout", ["git", "checkout", "--detach", head], dest)
    if checkout["exit"] != 0:
        return problems + ["checkout_failed"], {"inventory": inventory}
    clone_head = git_out("rev-parse", "HEAD", cwd=dest)
    clone_tree = git_out("rev-parse", "HEAD^{tree}", cwd=dest)
    if clone_head != head or clone_tree != tree:
        problems.append("clone_identity_mismatch")
    if (dest / ".venv").exists() or (dest / ".venv").is_symlink():
        problems.append("clone_contains_venv")
    compared = {}
    for rel in AUTHORED + UNCHANGED_CHECKERS:
        source_hash, clone_hash = sha_file(REPO / rel), sha_file(dest / rel)
        compared[rel] = {"source": source_hash, "clone": clone_hash}
        if source_hash != clone_hash:
            problems.append("clone_file_mismatch:" + rel)
    scan = row.child("committed-tree-check-public-tree",
                     [sys.executable, "-B", str(dest / "tools" / "check_public_tree.py")], dest)
    committed = None
    try:
        committed = json.loads(Path(scan["stdout"]).read_text())
    except (OSError, ValueError):
        problems.append("committed_scan_unparseable")
    if scan["exit"] != 0:
        problems.append("committed_scan_exit:%s" % scan["exit"])
    if not isinstance(committed, dict) or committed.get("findings") != []:
        problems.append("committed_scan_findings")
    inplace = row.child("in-place-informational-INC-174",
                        [sys.executable, "-B", str(REPO / "tools" / "check_public_tree.py")], REPO)
    inplace_result = None
    try:
        inplace_result = json.loads(Path(inplace["stdout"]).read_text())
    except (OSError, ValueError):
        problems.append("in_place_unparseable")
    inplace_findings = inplace_result.get("findings") if isinstance(inplace_result, dict) else None
    if inplace["exit"] == 0 and inplace_findings == []:
        inplace_label = "exit0_no_findings"
    elif (inplace["exit"] == 1 and inplace_findings == [ALLOWED_INPLACE_FINDING]
          and ident.get("venv_link_matches_manifest")):
        inplace_label = "exit1_only_verified_venv_symlink_not_scanned (informational, not a staged-tree pass)"
    else:
        inplace_label = "disallowed"
        problems.append("in_place_disallowed_outcome")
    if git_out("rev-parse", "HEAD") != head or git_out("status", "--porcelain=v1", "--untracked-files=all") != status:
        problems.append("source_changed_during_row")
    return problems, {"candidate_head": head, "candidate_tree": tree, "clone_head": clone_head,
                      "clone_tree": clone_tree, "clone_path": str(dest), "git_status": status,
                      "path_inventory_with_ignored": inventory, "untracked_scan_visible": others,
                      "authored": authored, "compared_hashes": compared, "committed_tree_scan": committed,
                      "in_place_informational": {"label": "INC-174", "exit": inplace["exit"],
                                                 "findings": inplace_findings, "outcome": inplace_label}}


def artifact_hashes(root):
    out = {}
    for path in sorted(root.rglob("*")):
        if "committed-tree" in path.relative_to(root).parts[:1]:
            continue
        if path.is_file() and not path.is_symlink():
            out[str(path.relative_to(root))] = sha_file(path)
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--phase", required=True, choices=PHASES)
    parser.add_argument("--source-manifest", required=True)
    parser.add_argument("--out-base", required=True)
    args = parser.parse_args(argv)
    row = Row(args.phase, args.out_base)
    started = utc_now()
    try:
        manifest, manifest_sha = load_manifest(args.source_manifest)
        problems, ident = identity(manifest)
        problems += interpreter_problems(args.phase, manifest)
        if args.phase == "public-tree":
            more, detail = phase_public_tree(row, manifest, ident)
        else:
            more, detail = {"suite": phase_suite, "compatibility": phase_compatibility,
                            "portable": phase_portable, "credentials": phase_credentials}[args.phase](row, manifest)
        problems += more
    except Exception as exc:  # an incomplete row is a failure, never a pass
        problems, detail, ident, manifest_sha = ["runner_exception:%s:%s" % (type(exc).__name__, exc)], {}, {}, None
    tests_run = {rec["name"]: rec["result"].get("tests_run") for rec in row.children
                 if isinstance(rec.get("result"), dict)}
    summary = {
        "card": "FC9-HEADER-CITATIONS", "phase": args.phase, "verdict": "PASS" if not problems else "FAIL",
        "problems": problems,
        "authority": "REVIEW REQUIRED: machine validity only; not semantic, legal, human or live acceptance",
        "argv": [sys.executable, *sys.argv], "cwd": os.getcwd(), "interpreter": sys.executable,
        "python_version": sys.version, "repo": str(REPO), "identity": ident,
        "source_manifest": args.source_manifest, "source_manifest_sha256": manifest_sha,
        "row_dir": str(row.dir), "tempdir": tempfile.tempdir, "started": started, "finished": utc_now(),
        "authored_hashes": {rel: sha_file(REPO / rel) for rel in AUTHORED if (REPO / rel).is_file()},
        "fixture_sha256": sha_file(REPO / FIXTURE) if (REPO / FIXTURE).is_file() else None,
        "tests_run": tests_run, "children": row.children, "detail": detail,
        "full_discovery": "operator-run (not part of this row)",
    }
    summary["artifact_sha256"] = artifact_hashes(row.dir)
    write_json(row.dir / "summary.json", summary)
    print("%s %s row_dir=%s problems=%s" % (args.phase, summary["verdict"], row.dir, problems))
    print("summary_sha256=%s" % sha_file(row.dir / "summary.json"))
    return 0 if not problems else 1


# ---------------------------------------------------------------- child side


class RecordingResult(unittest.TextTestResult):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.passed = []

    def addSuccess(self, test):
        super().addSuccess(test)
        self.passed.append(test.id())


def run_tests(suite):
    runner = unittest.TextTestRunner(stream=sys.stderr, verbosity=2, resultclass=RecordingResult)
    res = runner.run(suite)
    return {"tests_run": res.testsRun, "passed": sorted(res.passed),
            "failures": sorted(t.id() for t, _ in res.failures), "errors": sorted(t.id() for t, _ in res.errors),
            "skipped": sorted(t.id() for t, _ in res.skipped),
            "expected_failures": sorted(t.id() for t, _ in res.expectedFailures),
            "unexpected_successes": sorted(t.id() for t in res.unexpectedSuccesses)}


def load_baseline(path):
    source = Path(path).read_bytes()
    module = types.ModuleType("campaign_tool.records.understanding._fc9_pinned_baseline_correspondence")
    module.__package__ = "campaign_tool.records.understanding"
    module.__file__ = str(path)
    exec(compile(source, str(path), "exec"), module.__dict__)  # pinned base bytes from git show, in-memory only
    return module, sha_bytes(source)


def child_suite(out, baseline_source):
    from tests.records import test_fc9_header_citations as tm
    out.update(run_tests(unittest.defaultTestLoader.loadTestsFromName(MODULE)))
    out["control"] = tm.run_negative_control()
    baseline, digest = load_baseline(baseline_source)
    observed = {"source_sha256": digest, "label": "actual pinned-base behavior (no injection)"}
    tc = unittest.TestCase()
    dict_case = tm.case_named("addressable_cases", "dict_scalar")
    try:
        tm.events_for(baseline, tm.receipt_bundle(dict_case["name"], dict_case["headers"]))
        observed["dict_scalar"] = "no_error"
    except ValueError as exc:
        observed["dict_scalar"] = "ValueError: %s" % exc
    try:
        tm.check_addressable(tc, tm.case_named("addressable_cases", "pair_array"), baseline)
        observed["pair_array"] = "passed"
    except AssertionError as exc:
        observed["pair_array"] = "assertion_failed: " + str(exc).splitlines()[0][:240]
    out["baseline"] = observed
    try:
        tm.check_addressable(tc, dict_case)
        tm.check_addressable(tc, tm.case_named("addressable_cases", "pair_array"))
        out["candidate_after_control"] = "passed"
    except AssertionError as exc:
        out["candidate_after_control"] = "assertion_failed: " + str(exc).splitlines()[0][:240]
    out["labels"] = {"control": "injected behavior (legacy /headers/<index>/1 at the selection seam)",
                     "baseline": "actual baseline observation at the manifest base",
                     "candidate_after_control": "final candidate output after restore"}


def child_discovery(out):
    hooks = []

    def audit(event, args):
        if event == "sys.addaudithook":
            hooks.append(event)
    sys.addaudithook(audit)  # this child only; observes installs by discovered tests
    loader = unittest.TestLoader()
    out["sentinel_before"] = "passed" if run_tests(loader.loadTestsFromName(SENTINEL_TEST))["passed"] else "failed"
    env_before, tmp_before = dict(os.environ), tempfile.tempdir
    path_before, bytecode_before = list(sys.path), sys.dont_write_bytecode
    hooks.clear()
    suite = loader.discover(start_dir=str(REPO / "tests" / "records"), pattern="test_fc9_header_citations.py",
                            top_level_dir=str(REPO))
    out.update(run_tests(suite))
    out["audit_hook_installs"] = len(hooks)
    out["environ_unchanged"] = dict(os.environ) == env_before
    out["tempdir_unchanged"] = tempfile.tempdir == tmp_before
    out["sys_path_unchanged"] = list(sys.path) == path_before
    out["bytecode_flag_unchanged"] = sys.dont_write_bytecode == bytecode_before
    module = sys.modules.get(MODULE)
    out["module_file"] = str(Path(module.__file__).resolve()) if module else None
    out["sentinel_after"] = "passed" if run_tests(loader.loadTestsFromName(SENTINEL_TEST))["passed"] else "failed"


def child_main(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", required=True, choices=("suite", "compat", "discovery"))
    parser.add_argument("--child-result", required=True)
    parser.add_argument("--tmp-root", required=True)
    parser.add_argument("--baseline-source")
    parser.add_argument("targets", nargs="*")
    args = parser.parse_args(argv)
    tmp_root = Path(args.tmp_root)
    tmp_root.mkdir(parents=True, exist_ok=False)
    tempfile.tempdir = str(tmp_root)  # before any product import
    sys.dont_write_bytecode = True
    root = str(REPO)
    sys.path[:] = [root] + [p for p in sys.path if p not in ("", ".", root)]
    out = {"mode": args.child, "source_root": root, "tempdir": tempfile.tempdir}
    if args.child == "suite":
        child_suite(out, args.baseline_source)
    elif args.child == "compat":
        out.update(run_tests(unittest.defaultTestLoader.loadTestsFromNames(args.targets)))
    else:
        child_discovery(out)
    out["modules"] = {name: str(Path(mod.__file__).resolve()) for name, mod in sorted(sys.modules.items())
                      if name.split(".")[0] in ("campaign_tool", "tests") and getattr(mod, "__file__", None)}
    write_json(args.child_result, out)
    ok = out.get("tests_run", 0) > 0 and not out.get("failures") and not out.get("errors")
    return 0 if ok else 1


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--child":
        raise SystemExit(child_main(sys.argv[1:]))
    raise SystemExit(main())
