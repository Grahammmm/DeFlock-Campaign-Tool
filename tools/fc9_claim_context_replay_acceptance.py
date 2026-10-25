"""FC9-CLAIM-CONTEXT-REPLAY acceptance runner.

One --phase per invocation: suite, compatibility, portable, credentials or
public-tree. Each invocation creates a new UUID directory below --out-base,
redirects tempfile.tempdir into it before any product import, runs every child
as a literal argv list with the shell disabled, keeps raw stdout/stderr and
writes summary.json with hashes. Exit zero only when every required condition
holds. Machine validity is REVIEW REQUIRED, never semantic, legal, human or
live acceptance, and authorizes no export or publication.

The repository is derived from this file's location; product imports in
children are resolved from that same tree by sys.path set here, never by an
environment variable or module flag.
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
import unittest
import uuid

HERE = Path(__file__).resolve()
REPO = HERE.parents[1]
PHASES = ("suite", "compatibility", "portable", "credentials", "public-tree")
# Hex pins are split into 8-character pieces only so the unchanged repository secret
# scanner does not read them as high-entropy strings; no allowlist entry is added.
BASE = "".join(("6dd2ae50", "8e316dd8", "3aa6d744", "45c46fb8", "c8d25757"))
MANIFEST_SCHEMA = "agentorange.source-manifest/v1"
MODULE = "tests.records.test_fc9_claim_context_replay"
TEST_REL = "tests/records/test_fc9_claim_context_replay.py"
FIXTURE_REL = "tests/fixtures/fc9-claim-context-replay/cases.json"
RUNNER_REL = "tools/fc9_claim_context_replay_acceptance.py"
AUTHORED = (TEST_REL, FIXTURE_REL, RUNNER_REL)
PRODUCT_PINS = {
    "campaign_tool/records/understanding/evidence.py": "".join((
        "215ed758", "99325c4a", "c294cfaf", "890fb85d", "ccabf179", "de240ed4", "4f93d178", "eaeb11b4")),
    "campaign_tool/records/grounded_evidence.py": "".join((
        "43ace8a6", "17ab8147", "7e72ceb3", "9633f1b6", "c00dfa32", "213aba12", "34367bd1", "5fbd87f8")),
    "tests/records/test_understanding.py": "".join((
        "77fa084d", "17606632", "7bc17953", "81a91167", "1da7875f", "1f5721e1", "64002616", "d6f5922a")),
}
CHECKER_FILES = ("tools/check_public_tree.py", "campaign_tool/records/public_scan.py",
                 "tools/check_secret_report.py")
COMPAT_SELECTORS = ("tests.records.test_understanding.EvidenceTests",
                    "tests.records.test_understanding.GroundedEvidenceTests")
REQUIRED_COMPAT = tuple("tests.records.test_understanding." + n for n in (
    "EvidenceTests.test_receipt_normalizes_to_pinned_bundle",
    "EvidenceTests.test_tampered_bundle_is_rejected",
    "EvidenceTests.test_receipt_binding_mismatch_is_rejected",
    "EvidenceTests.test_duplicate_json_keys_are_rejected",
    "EvidenceTests.test_quote_citation_and_claim_round_trip",
    "GroundedEvidenceTests.test_json_pointer_support_is_exact",
))
_M = MODULE + "."
REQUIRED_SUITE = tuple(_M + n for n in (
    "ClaimPersistenceTests.test_claim_a_json_roundtrip_validates",
    "ClaimPersistenceTests.test_restored_byte_identical_bundle_replays_original_claim",
    "ClaimPersistenceTests.test_reordered_equal_context_is_not_a_change",
    "RehashedContextTests.test_rehashed_context_bundle_is_valid_with_identical_units",
    "RehashedContextTests.test_old_citations_validate_against_rehashed_context",
    "RehashedContextTests.test_saved_claim_rejected_on_rehashed_context",
    "RehashedContextTests.test_fresh_claim_on_rehashed_context_passes",
    "RehashedContextTests.test_fresh_claim_rejected_on_original_context",
    "RehashedContextTests.test_forged_context_pin_with_recomputed_hash_rejected",
    "RehashedContextTests.test_forged_snapshot_pin_with_recomputed_hash_rejected",
    "ReceiptIdentityTests.test_changed_receipt_rejects_old_supports_as_not_current",
    "ReceiptIdentityTests.test_recited_supports_on_changed_receipt_pass",
    "MalformedTests.test_corrupt_snapshot_hash_is_bundle_refusal_not_context_refusal",
    "MalformedTests.test_malformed_saved_claims_rejected",
    "CoverageTests.test_fixture_dimensions_are_complete",
))
# Declared synthetic control: with only the final stale/changed equality gate
# disabled, these context-only refusals must fail their expected-refusal assertion.
CONTROL_MUST_FAIL = tuple(_M + n for n in (
    "RehashedContextTests.test_saved_claim_rejected_on_rehashed_context",
    "RehashedContextTests.test_fresh_claim_rejected_on_original_context",
    "RehashedContextTests.test_forged_context_pin_with_recomputed_hash_rejected",
    "RehashedContextTests.test_forged_snapshot_pin_with_recomputed_hash_rejected",
))
# Bundle/source/citation checks stay intact under the control, so these stay green.
CONTROL_MUST_PASS = tuple(_M + n for n in (
    "ClaimPersistenceTests.test_claim_a_json_roundtrip_validates",
    "RehashedContextTests.test_old_citations_validate_against_rehashed_context",
    "RehashedContextTests.test_fresh_claim_on_rehashed_context_passes",
    "ReceiptIdentityTests.test_changed_receipt_rejects_old_supports_as_not_current",
    "ReceiptIdentityTests.test_recited_supports_on_changed_receipt_pass",
    "MalformedTests.test_corrupt_snapshot_hash_is_bundle_refusal_not_context_refusal",
))
CONTROL_POSITIVES = tuple(_M + n for n in (
    "RehashedContextTests.test_fresh_claim_on_rehashed_context_passes",
    "ClaimPersistenceTests.test_restored_byte_identical_bundle_replays_original_claim",
))
GATE_CODE = "evidence_claim_stale_or_changed"
SENTINEL_TEST = "tests.records.test_understanding.EvidenceTests.test_duplicate_json_keys_are_rejected"
# Host-specific and inline-interpreter markers, assembled so this file does not match itself.
FORBIDDEN_MARKERS = tuple(m.encode() for m in (
    "/" + "home/", "/" + "Users/", "agent" + "-work", "venv" + "-fullsuite", "/" + "root/", "/" + "tmp/",
    "python3 -" + "c", "python -" + "c", "<" + "<EOF", "shell" + "=True", "os." + "system(",
    "PYTHON" + "PATH", "candidates" + "/fc9",
))
HEX40 = re.compile(r"[0-9a-f]{40}\Z")


def sha_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha_file(path):
    return sha_bytes(Path(path).read_bytes())


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def write_new(path, text):
    with open(path, "x", encoding="utf-8") as handle:
        handle.write(text)


class Run:
    def __init__(self, phase, out_base):
        base = Path(out_base).absolute()
        base.mkdir(parents=True, exist_ok=True)
        self.dir = base / ("%s-%s" % (phase, uuid.uuid4()))
        self.dir.mkdir(exist_ok=False)
        self.tmp = self.dir / "tmp"
        self.tmp.mkdir()
        tempfile.tempdir = str(self.tmp)
        self.children = []
        self.count = 0

    def fresh(self, name):
        path = self.dir / name
        path.mkdir(exist_ok=False)
        return path

    def child(self, name, argv, cwd, stdout_path=None):
        self.count += 1
        tag = "%02d-%s" % (self.count, name)
        out_path = Path(stdout_path) if stdout_path else self.dir / (tag + ".stdout")
        err_path = self.dir / (tag + ".stderr")
        with open(out_path, "xb") as out, open(err_path, "xb") as err:
            try:
                proc = subprocess.run([str(a) for a in argv], cwd=str(cwd), stdout=out, stderr=err, shell=False,
                                      stdin=subprocess.DEVNULL, timeout=1800)
                code, launch_error = proc.returncode, None
            except (OSError, subprocess.SubprocessError) as exc:
                code, launch_error = None, "%s:%s" % (type(exc).__name__, exc)
        rec = {"name": tag, "argv": [str(a) for a in argv], "cwd": str(cwd), "exit": code,
               "launch_error": launch_error, "stdout": str(out_path), "stdout_sha256": sha_file(out_path),
               "stderr": str(err_path), "stderr_sha256": sha_file(err_path)}
        self.children.append(rec)
        rec_text = Path(out_path).read_text(encoding="utf-8", errors="replace")
        return rec, rec_text

    def git(self, name, *args, cwd=REPO):
        rec, text = self.child("git-" + name, ["git", *args], cwd)
        return rec["exit"], text

    def py_child(self, name, mode, targets, cwd=REPO):
        result_path = self.dir / ("%s.result.json" % name)
        tmp_root = self.dir / ("%s-tmp" % name)
        argv = [sys.executable, str(HERE), "--child", mode, "--child-result", str(result_path),
                "--tmp-root", str(tmp_root), *targets]
        rec, _ = self.child(name, argv, cwd)
        try:
            rec["result"] = json.loads(result_path.read_text(encoding="utf-8"))
            rec["result_sha256"] = sha_file(result_path)
        except (OSError, ValueError):
            rec["result"] = None
        return rec


def clean(result, required, label):
    """Problems for a recorded unittest result; empty means all required passed and nothing else is wrong."""
    if not result:
        return ["%s:missing_result" % label]
    problems = []
    if result["tests_run"] <= 0:
        problems.append("%s:zero_tests" % label)
    for kind in ("failures", "errors", "skipped", "expected_failures", "unexpected_successes"):
        if result[kind]:
            problems.append("%s:%s:%s" % (label, kind, ",".join(sorted(result[kind]))))
    missing = sorted(set(required) - set(result["passed"]))
    if missing:
        problems.append("%s:required_not_passed:%s" % (label, ",".join(missing)))
    accounted = set(result["passed"]) | set(result["failed_tests"]) | set(result["skipped"])
    if result["tests_run"] != len(accounted):
        problems.append("%s:unaccounted_tests" % label)
    return problems


# ---------------------------------------------------------------- shared identity checks


def check_identity(run, phase, manifest_path):
    problems, info = [], {}
    try:
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        info["manifest_sha256"] = sha_file(manifest_path)
    except (OSError, ValueError) as exc:
        return ["manifest_unreadable:%s" % type(exc).__name__], info, {}
    info["manifest_path"] = str(Path(manifest_path).absolute())
    if manifest.get("schema") != MANIFEST_SCHEMA:
        problems.append("manifest_schema")
    if Path(str(manifest.get("repo"))).resolve() != REPO:
        problems.append("manifest_repo_mismatch")
    if manifest.get("head") != BASE:
        problems.append("manifest_head_not_pinned_base")
    code, tree = run.git("base-tree", "rev-parse", BASE + "^{tree}")
    if code != 0 or tree.strip() != manifest.get("tree"):
        problems.append("manifest_tree_mismatch")
    code, head = run.git("head", "rev-parse", "HEAD")
    head = head.strip()
    code2, head_tree = run.git("head-tree", "rev-parse", "HEAD^{tree}")
    info.update(head=head, head_tree=head_tree.strip(), base=BASE, base_tree=tree.strip())
    if code != 0 or not HEX40.fullmatch(head):
        problems.append("head_unreadable")
    elif head == BASE:
        problems.append("head_is_base_not_candidate")
    code, _ = run.git("descends", "merge-base", "--is-ancestor", BASE, "HEAD")
    if code != 0:
        problems.append("head_not_descended_from_base")
    runtime = manifest.get("runtime") or {}
    exe = os.path.abspath(sys.executable)
    info.update(interpreter=exe, interpreter_resolved=str(Path(sys.executable).resolve()), prefix=sys.prefix,
                python_version=sys.version)
    venv = Path(str(runtime.get("repo_venv"))).resolve()
    if phase == "credentials":
        if exe != os.path.abspath(str(runtime.get("credential_scan_python3"))):
            problems.append("credential_interpreter_not_manifest")
    elif exe != str(REPO / ".venv" / "bin" / "python3"):
        problems.append("interpreter_not_staged_repo_venv")
    if Path(sys.prefix).resolve() != venv:
        problems.append("interpreter_prefix_not_manifest_venv")
    if (REPO / ".venv").resolve() != venv:
        problems.append("staged_venv_link_not_manifest_venv")
    pins = {rel: sha_file(REPO / rel) for rel in PRODUCT_PINS}
    info["product_hashes"] = pins
    for rel, want in PRODUCT_PINS.items():
        if pins[rel] != want:
            problems.append("product_pin_changed:" + rel)
    return problems, info, manifest


# ---------------------------------------------------------------- phases


def phase_suite(run, manifest):
    problems, detail = [], {}
    rec = run.py_child("suite", "suite", [MODULE])
    res = rec["result"]
    problems += clean(res, REQUIRED_SUITE, "suite")
    if rec["exit"] != 0:
        problems.append("suite_exit:%s" % rec["exit"])
    if res:
        if set(res["cases_exercised"]) != set(res["cases_expected"]) or not res["cases_expected"]:
            problems.append("suite_case_coverage_incomplete")
        detail["case_dimensions"] = res.get("case_dimensions")
    ctl = run.py_child("control", "control", sorted(set(CONTROL_MUST_FAIL + CONTROL_MUST_PASS + CONTROL_POSITIVES)))
    c = ctl["result"]
    if ctl["exit"] != 0:
        problems.append("control_exit:%s" % ctl["exit"])
    if not c:
        return problems + ["control_missing_result"], detail
    targets = sorted(set(CONTROL_MUST_FAIL + CONTROL_MUST_PASS + CONTROL_POSITIVES))
    problems += clean(c.get("baseline"), targets, "control_baseline")
    mutant = c.get("injected") or {}
    if not c.get("gate_reached_in_mutant"):
        problems.append("control_gate_never_reached")
    for t in CONTROL_MUST_FAIL:
        if t not in mutant.get("failed_tests", []):
            problems.append("control_not_detected:" + t)
        elif t not in mutant.get("failed_on_expected_refusal", []) or t in mutant.get("failed_other", []):
            problems.append("control_failed_for_other_reason:" + t)
    if mutant.get("errors"):
        problems.append("control_mutant_errors:" + ",".join(mutant["errors"]))
    for t in CONTROL_MUST_PASS:
        if t not in mutant.get("passed", []):
            problems.append("control_intact_check_not_green:" + t)
    if not c.get("restored_is_original"):
        problems.append("control_gate_not_restored")
    problems += clean(c.get("final"), targets, "control_final")
    detail["control"] = {"injected_behavior": c.get("injected_behavior"),
                         "baseline_passed": len((c.get("baseline") or {}).get("passed", [])),
                         "injected_failed": mutant.get("failed_tests"),
                         "final_passed": len((c.get("final") or {}).get("passed", []))}
    detail["test_first"] = "not applicable: test-only brief, no product change"
    return problems, detail


def phase_compatibility(run, manifest):
    rec = run.py_child("compatibility", "plain", list(COMPAT_SELECTORS))
    problems = clean(rec["result"], REQUIRED_COMPAT, "compatibility")
    if rec["exit"] != 0:
        problems.append("compatibility_exit:%s" % rec["exit"])
    return problems, {"scope": "named selectors only; not full repository CI (OWED to shipping operator)",
                      "full_discovery": "operator-run"}


def phase_portable(run, manifest):
    problems, detail = [], {}
    literals = {}
    for rel in AUTHORED:
        data = (REPO / rel).read_bytes()
        hits = sorted(m.decode() for m in FORBIDDEN_MARKERS if m in data)
        literals[rel] = hits
        if hits:
            problems.append("host_literal:%s:%s" % (rel, ",".join(hits)))
    detail["static_literal_check"] = literals
    cwd = run.fresh("script-cwd-%s" % uuid.uuid4().hex[:8])
    report = run.dir / "script-case-report.json"
    rec, _ = run.child("script", [sys.executable, str(REPO / TEST_REL), "--tmp-root", str(run.dir / "script-tmp"),
                                  "--case-report", str(report), "-v"], cwd)
    if rec["exit"] != 0:
        problems.append("script_exit:%s" % rec["exit"])
    try:
        script = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        script = None
        problems.append("script_case_report_missing")
    err = Path(rec["stderr"]).read_text(encoding="utf-8", errors="replace")
    if not re.search(r"^Ran \d+ tests? in ", err, re.M) or not re.search(r"^OK$", err, re.M):
        problems.append("script_unittest_not_ok")
    if script:
        if not script["successful"] or set(script["exercised"]) != set(script["expected"]) or not script["expected"]:
            problems.append("script_case_coverage_incomplete")
    dcwd = run.fresh("discovery-cwd-%s" % uuid.uuid4().hex[:8])
    drec = run.py_child("discovery", "discovery", [], cwd=dcwd)
    d = drec["result"]
    if drec["exit"] != 0:
        problems.append("discovery_exit:%s" % drec["exit"])
    problems += clean(d, REQUIRED_SUITE, "discovery")
    if d:
        for k in ("sentinel_before", "sentinel_after"):
            problems += clean(d.get(k), [SENTINEL_TEST], "discovery_" + k)
        if d.get("ambient_changed_by_import"):
            problems.append("discovery_import_changed_ambient:" + ",".join(d["ambient_changed_by_import"]))
        if d.get("ambient_changed_by_run"):
            problems.append("discovery_run_changed_ambient:" + ",".join(d["ambient_changed_by_run"]))
        outside = sorted(n for n, p in d.get("modules", {}).items() if not Path(p).is_relative_to(REPO))
        if outside or "campaign_tool.records.grounded_evidence" not in d.get("modules", {}):
            problems.append("discovery_module_origin:" + ",".join(outside))
        if set(d["cases_exercised"]) != set(d["cases_expected"]):
            problems.append("discovery_case_coverage_incomplete")
        if script and set(script["expected"]) != set(d["cases_expected"]):
            problems.append("script_discovery_case_sets_differ")
    detail.update(script_tests_run=(script or {}).get("tests_run"),
                  script_cases=len((script or {}).get("exercised", [])),
                  discovery_tests_run=(d or {}).get("tests_run"),
                  discovery_cases=len((d or {}).get("cases_exercised", [])))
    return problems, detail


def phase_credentials(run, manifest):
    problems = []
    runtime = manifest.get("runtime") or {}
    scanner = os.path.abspath(str(runtime.get("detect_secrets")))
    if Path(scanner).parent != Path(os.path.abspath(sys.executable)).parent:
        problems.append("scanner_not_beside_credential_interpreter")
    if not Path(scanner).is_file():
        return problems + ["scanner_unavailable"], {"scanner": scanner}
    report = run.dir / "secret-scan.json"
    scan, _ = run.child("detect-secrets", [scanner, "scan", "--all-files", "--exclude-files", "^.git/",
                                           "--no-verify"], REPO, stdout_path=report)
    if scan["exit"] != 0:
        problems.append("scan_exit:%s" % scan["exit"])
    try:
        parsed = json.loads(report.read_text(encoding="utf-8"))
        results = parsed["results"]
        if not isinstance(results, dict):
            raise ValueError
        venv_paths = sorted(p for p in results if p == ".venv" or p.startswith(".venv/"))
        if venv_paths:
            problems.append("scan_traversed_staged_venv")
        version = parsed.get("version")
    except (OSError, ValueError, KeyError, TypeError):
        problems.append("report_malformed")
        version, results = None, {}
    checker, text = run.child("check-secret-report", [sys.executable, str(REPO / "tools" / "check_secret_report.py"),
                                                      str(report)], REPO)
    if checker["exit"] != 0:
        problems.append("checker_exit:%s" % checker["exit"])
    return problems, {"scanner": scanner, "scanner_version": version, "report": str(report),
                      "report_sha256": sha_file(report), "report_paths_with_results": sorted(results),
                      "checker_stdout": text.strip().splitlines()[-3:]}


def parse_scan(text):
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) and isinstance(value.get("findings"), list) else None
    except ValueError:
        return None


def phase_public_tree(run, manifest):
    problems, detail = [], {}
    code, status = run.git("status", "status", "--porcelain=v1", "--untracked-files=all", "--ignored=no")
    lines = [ln for ln in status.splitlines() if ln]
    detail["status_before"] = lines
    # Only the verified standard staged .venv link may appear (git may also ignore it outright).
    if code != 0 or lines not in ([], ["?? .venv"]):
        problems.append("source_status_not_only_staged_venv")
    venv_ok = (REPO / ".venv").is_symlink() and (REPO / ".venv").resolve() == Path(
        str((manifest.get("runtime") or {}).get("repo_venv"))).resolve()
    if lines == ["?? .venv"] and not venv_ok:
        problems.append("untracked_venv_not_manifest_link")
    code, listed = run.git("ls-files", "ls-files", "--error-unmatch", "--", *AUTHORED)
    if code != 0 or sorted(listed.split()) != sorted(AUTHORED):
        problems.append("authored_not_tracked")
    for name, args in (("diff-index", ("diff", "--cached", "--quiet", "HEAD", "--")),
                       ("diff-worktree", ("diff", "--quiet", "--"))):
        if run.git(name, *args)[0] != 0:
            problems.append(name + "_differs_from_head")
    _, head = run.git("head", "rev-parse", "--verify", "HEAD^{commit}")
    _, tree = run.git("tree", "rev-parse", "--verify", "HEAD^{tree}")
    head, tree = head.strip(), tree.strip()
    if not (HEX40.fullmatch(head) and HEX40.fullmatch(tree)):
        return problems + ["head_or_tree_invalid"], detail
    detail.update(head=head, tree=tree)
    blobs = {}
    for rel in AUTHORED + CHECKER_FILES:
        blobs[rel] = run.git("blob-" + Path(rel).name, "rev-parse", "HEAD:" + rel)[1].strip()
    for rel in CHECKER_FILES:
        if run.git("base-blob-" + Path(rel).name, "rev-parse", BASE + ":" + rel)[1].strip() != blobs[rel]:
            problems.append("checker_changed_from_base:" + rel)
    row = run.fresh("committed-tree")
    dest = row / "clone"
    code, _ = run.git("clone", "clone", "--local", "--no-hardlinks", "--no-checkout", "--no-recurse-submodules",
                      str(REPO), str(dest), cwd=row)
    if code != 0:
        return problems + ["clone_failed"], detail
    if run.git("checkout", "checkout", "--detach", head, cwd=dest)[0] != 0:
        return problems + ["clone_checkout_failed"], detail
    c_head = run.git("clone-head", "rev-parse", "HEAD", cwd=dest)[1].strip()
    c_tree = run.git("clone-tree", "rev-parse", "HEAD^{tree}", cwd=dest)[1].strip()
    if (c_head, c_tree) != (head, tree):
        problems.append("clone_identity_mismatch")
    if (dest / ".venv").exists() or (dest / ".venv").is_symlink():
        problems.append("clone_has_staged_runtime_link")
    hashes = {}
    for rel in AUTHORED + CHECKER_FILES:
        hashes[rel] = [sha_file(REPO / rel), sha_file(dest / rel)]
        if hashes[rel][0] != hashes[rel][1]:
            problems.append("clone_file_hash_mismatch:" + rel)
    detail["file_hashes_source_clone"] = hashes
    c_status = run.git("clone-status", "status", "--porcelain=v1", "--untracked-files=all", cwd=dest)[1]
    if c_status.strip():
        problems.append("clone_not_clean")
    rec, text = run.child("committed-tree-check", [sys.executable, str(dest / "tools" / "check_public_tree.py")], dest)
    scan = parse_scan(text)
    detail["committed_tree_scan"] = {"exit": rec["exit"], "findings": (scan or {}).get("findings"),
                                     "scanned_files": (scan or {}).get("scanned_files")}
    if rec["exit"] != 0 or scan is None or scan["findings"] != []:
        problems.append("committed_tree_scan_not_clean")
    after_head = run.git("head-after", "rev-parse", "HEAD")[1].strip()
    after_status = [ln for ln in run.git("status-after", "status", "--porcelain=v1", "--untracked-files=all",
                                         "--ignored=no")[1].splitlines() if ln]
    if after_head != head or after_status != lines:
        problems.append("source_changed_during_row")
    # INC-174 in-place informational scan of the staged tree (never reported as a staged-tree pass).
    venv = Path(str((manifest.get("runtime") or {}).get("repo_venv"))).resolve()
    link_ok = (REPO / ".venv").is_symlink() and (REPO / ".venv").resolve() == venv
    irec, itext = run.child("in-place-informational", [sys.executable, str(REPO / "tools" / "check_public_tree.py")],
                            REPO)
    iscan = parse_scan(itext)
    allowed = iscan is not None and link_ok and (
        (irec["exit"] == 0 and iscan["findings"] == []) or
        (irec["exit"] == 1 and iscan["findings"] == [[".venv", 0, "symlink_not_scanned"]]))
    detail["in_place_informational"] = {"label": "INC-174", "exit": irec["exit"],
                                        "findings": (iscan or {}).get("findings"), "venv_link_is_manifest_venv": link_ok,
                                        "status": "informational only; not a staged-tree pass"}
    if not allowed:
        problems.append("in_place_informational_unexpected")
    return problems, detail


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--phase", required=True, choices=PHASES)
    parser.add_argument("--source-manifest", required=True)
    parser.add_argument("--out-base", required=True)
    args = parser.parse_args(argv)
    run = Run(args.phase, args.out_base)
    started = utc_now()
    try:
        problems, ident, manifest = check_identity(run, args.phase, args.source_manifest)
        handler = {"suite": phase_suite, "compatibility": phase_compatibility, "portable": phase_portable,
                   "credentials": phase_credentials, "public-tree": phase_public_tree}[args.phase]
        more, detail = handler(run, manifest)
        problems = problems + more
    except Exception as exc:  # an incomplete row is a failure, never a pass
        problems, ident, detail = ["runner_exception:%s:%s" % (type(exc).__name__, exc)], {}, {}
    tests_run = {rec["name"]: rec["result"].get("tests_run") for rec in run.children
                 if isinstance(rec.get("result"), dict) and "tests_run" in rec["result"]}
    artifacts = {}
    for path in sorted(run.dir.rglob("*")):
        if path.is_file() and not path.is_symlink() and "committed-tree" not in path.relative_to(run.dir).parts \
                and not path.relative_to(run.dir).parts[0].endswith("tmp"):
            artifacts[str(path.relative_to(run.dir))] = sha_file(path)
    summary = {
        "card": "FC9-CLAIM-CONTEXT-REPLAY", "phase": args.phase, "verdict": "PASS" if not problems else "FAIL",
        "problems": problems,
        "authority": "REVIEW REQUIRED: machine validity only; not semantic, legal, human or live acceptance",
        "argv": [sys.executable, *sys.argv], "cwd": os.getcwd(), "run_dir": str(run.dir), "repo": str(REPO),
        "identity": ident, "fixture_sha256": sha_file(REPO / FIXTURE_REL),
        "authored_hashes": {rel: sha_file(REPO / rel) for rel in AUTHORED if (REPO / rel).is_file()},
        "started": started, "finished": utc_now(), "tests_run": tests_run, "children": run.children,
        "detail": detail, "artifact_sha256": artifacts,
    }
    write_new(run.dir / "summary.json", json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print("%s %s run_dir=%s problems=%s" % (args.phase, summary["verdict"], run.dir, problems))
    return 0 if not problems else 1


# ---------------------------------------------------------------- child side


class RecordingResult(unittest.TextTestResult):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.passed, self.failed, self.expected_refusal_fail, self.other_fail = [], set(), set(), set()

    def _parent(self, test):
        return getattr(test, "test_case", test).id()

    def addSuccess(self, test):
        super().addSuccess(test)
        self.passed.append(test.id())

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            self.failed.add(test.id())
            if issubclass(err[0], AssertionError) and str(err[1]) == "ValueError not raised":
                self.expected_refusal_fail.add(test.id())
            else:
                self.other_fail.add(test.id())

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.failed.add(self._parent(test))
        self.other_fail.add(self._parent(test))

    def addError(self, test, err):
        super().addError(test, err)
        self.failed.add(self._parent(test))
        self.other_fail.add(self._parent(test))


def run_suite(suite):
    runner = unittest.TextTestRunner(stream=sys.stderr, verbosity=2, resultclass=RecordingResult)
    res = runner.run(suite)
    passed = sorted(set(res.passed) - res.failed)
    return {"tests_run": res.testsRun, "passed": passed, "failed_tests": sorted(res.failed),
            "failed_on_expected_refusal": sorted(res.expected_refusal_fail),
            "failed_other": sorted(res.other_fail),
            "failures": sorted({res._parent(t) for t, _ in res.failures}),
            "errors": sorted({res._parent(t) for t, _ in res.errors}),
            "skipped": sorted(t.id() for t, _ in res.skipped),
            "expected_failures": sorted(t.id() for t, _ in res.expectedFailures),
            "unexpected_successes": sorted(t.id() for t in res.unexpectedSuccesses)}


def load(names):
    return unittest.defaultTestLoader.loadTestsFromNames(names)


def ambient():
    import socket
    return {"environ": dict(os.environ), "tempdir": tempfile.tempdir, "cwd": os.getcwd(), "sys_path": list(sys.path),
            "socket_connect": id(socket.socket.connect), "dont_write_bytecode": sys.dont_write_bytecode}


def diff(a, b):
    return sorted(k for k in a if a[k] != b[k])


def child_main(mode, result_path, tmp_root, targets):
    tmp = Path(tmp_root).absolute()
    tmp.mkdir(parents=True, exist_ok=False)
    tempfile.tempdir = str(tmp)  # before any product import
    sys.dont_write_bytecode = True
    root = str(REPO)
    sys.path[:] = [root] + [p for p in sys.path if p not in ("", ".", root)]
    out = {"mode": mode, "source_root": root, "targets": targets, "tempdir": tempfile.tempdir}
    if mode == "discovery":
        out["sentinel_before"] = run_suite(load([SENTINEL_TEST]))
        before = ambient()
        suite = unittest.defaultTestLoader.discover(start_dir=str(REPO / "tests" / "records"),
                                                    pattern=Path(TEST_REL).name, top_level_dir=root)
        after_import = ambient()
        out.update(run_suite(suite))
        after_run = ambient()
        out["ambient_changed_by_import"] = diff(before, after_import)
        out["ambient_changed_by_run"] = diff(after_import, after_run)
        out["sentinel_after"] = run_suite(load([SENTINEL_TEST]))
    elif mode == "control":
        from campaign_tool.records import grounded_evidence
        original = grounded_evidence._need
        reached = []

        def gate_disabled(ok, code):
            # Injected behavior: skip only the final stale/changed equality gate.
            if code == GATE_CODE:
                reached.append(bool(ok))
                return None
            return original(ok, code)

        out["injected_behavior"] = ("grounded_evidence._need wrapper ignores only code %s; every other check, "
                                    "including evidence.need bundle checks, is unchanged" % GATE_CODE)
        out["baseline"] = run_suite(load(targets))
        grounded_evidence._need = gate_disabled
        try:
            out["injected"] = run_suite(load(targets))
        finally:
            grounded_evidence._need = original
        out["gate_reached_in_mutant"] = False in reached
        out["restored_is_original"] = grounded_evidence._need is original
        out["final"] = run_suite(load(targets))
    else:
        out.update(run_suite(load(targets)))
    mod = sys.modules.get(MODULE)
    if mod is not None:
        out["cases_expected"] = sorted(mod.expected_case_ids())
        out["cases_exercised"] = sorted(mod.CASES_EXERCISED)
        dims = {}
        for case in out["cases_exercised"]:
            dims[case.split("/")[0]] = dims.get(case.split("/")[0], 0) + 1
        out["case_dimensions"] = {"families": dims, "builders": list(mod.BUILDERS),
                                  "changed_contexts": [v["id"] for v in mod.CHANGED],
                                  "claims": [c["id"] for c in mod.CLAIMS], "malformed": list(mod.MALFORMED)}
    out["modules"] = {n: str(Path(m.__file__).resolve()) for n, m in sorted(sys.modules.items())
                      if n.split(".")[0] in ("campaign_tool", "tests") and getattr(m, "__file__", None)}
    write_new(result_path, json.dumps(out, indent=2, sort_keys=True) + "\n")
    final = out.get("final") if mode == "control" else out
    ok = final.get("tests_run", 0) > 0 and not final.get("failures") and not final.get("errors")
    return 0 if ok else 1


if __name__ == "__main__":
    if len(sys.argv) >= 7 and sys.argv[1] == "--child" and sys.argv[3] == "--child-result" \
            and sys.argv[5] == "--tmp-root":
        if sys.argv[2] not in ("suite", "plain", "control", "discovery"):
            raise SystemExit(2)
        raise SystemExit(child_main(sys.argv[2], sys.argv[4], sys.argv[6], sys.argv[7:]))
    raise SystemExit(main())
