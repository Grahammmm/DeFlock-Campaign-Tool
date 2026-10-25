"""FC13-ATTEMPT-BINDING local acceptance runner; output is REVIEW REQUIRED only.

Five separate phases (suite, compatibility, portable, credentials, public-tree).
Every invocation writes a fresh UUID directory beneath --out-base, never inside
the repository, and seals an evidence manifest only after every child exited.
Children are shell-disabled argv lists. No network, OCR, receipt file or store.
"""
from __future__ import annotations

import argparse
import copy
import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

REPO = Path(__file__).resolve().parents[1]
TEST = "tests.records.test_fc13_attempt_binding"
TEST_FILE = "tests/records/test_fc13_attempt_binding.py"
FIXTURE = "tests/fixtures/fc13-attempt-binding/cases.json"
RUNNER = "tools/fc13_attempt_binding_acceptance.py"
PRODUCT = "campaign_tool/records/understanding/evidence.py"
AUTHORED = (PRODUCT, TEST_FILE, FIXTURE, RUNNER)
UNCHANGED = ("campaign_tool/records/understanding/document.py", "tests/records/test_understanding.py",
             "tools/check_public_tree.py", "tools/check_secret_report.py",
             "campaign_tool/records/public_scan.py")
SELECTORS = ("tests.records.test_understanding.EvidenceTests", "tests.records.test_understanding.DocumentTests")
SENTINEL = "tests.records.test_understanding.EvidenceTests"
STATIC_TESTS = ("test_fixture_matrix_coverage", "test_normalize_receipt_emits_native_on_both_sides",
                "test_accepted_hashes_are_deterministic", "test_disabled_equality_guard_is_killed_then_restored")
CONTROL_TEST = "test_disabled_equality_guard_is_killed_then_restored"
PHASES = ("suite", "compatibility", "portable", "credentials", "public-tree")
RESULT_LINE = re.compile(r"^(test_\w+) \(([\w.]+)\)(?:\n.*?)? \.\.\. (ok|FAIL|ERROR|skipped.*|expected failure|unexpected success)$",
                         re.MULTILINE)
# Box-specific path tokens, assembled so this file does not match itself.
BOX_TOKENS = tuple("".join(parts) for parts in (("/", "home", "/"), ("/", "root", "/"), ("agent", "-work"),
                                               (".", "hermes"), ("/", "tmp", "/"), ("Agent", "Orange")))


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def file_sha(path):
    p = Path(path)
    return sha(p.read_bytes()) if p.is_file() else None


def git(repo, *args):
    r = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, timeout=300)
    return r.returncode, r.stdout.decode("utf-8", "replace").strip()


def write_new(path, data):
    fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return sha(data)


class Run:
    def __init__(self, root, manifest_path):
        self.root = root
        self.manifest_path = manifest_path
        self.children = []

    def child(self, label, argv, cwd, env=None):
        argv = [str(x) for x in argv]
        r = subprocess.run(argv, cwd=str(cwd), capture_output=True, timeout=1800, env=env, shell=False)
        out = write_new(self.root / (label + ".stdout"), r.stdout)
        err = write_new(self.root / (label + ".stderr"), r.stderr)
        text = r.stderr.decode("utf-8", "replace")
        results = [{"method": m.group(1), "id": m.group(2), "status": m.group(3)} for m in RESULT_LINE.finditer(text)]
        ran = re.search(r"^Ran (\d+) tests? in", text, re.MULTILINE)
        row = {"label": label, "argv": argv, "cwd": str(cwd), "interpreter": argv[0], "exit": r.returncode,
               "stdout": label + ".stdout", "stdout_sha256": out, "stdout_bytes": len(r.stdout),
               "stderr": label + ".stderr", "stderr_sha256": err, "stderr_bytes": len(r.stderr),
               "tests_run": int(ran.group(1)) if ran else None, "results": results,
               "failing": [x["id"] for x in results if x["status"] in ("FAIL", "ERROR", "unexpected success")],
               "skipped": [x["id"] for x in results if x["status"].startswith("skipped") or x["status"] == "expected failure"],
               "summary_ok": bool(re.search(r"^OK$", text, re.MULTILINE))}
        self.children.append(row)
        return row


def unittest_clean(row, expected_methods=None):
    ok = (row["exit"] == 0 and row["summary_ok"] and isinstance(row["tests_run"], int) and row["tests_run"] > 0
          and len(row["results"]) == row["tests_run"] and not row["failing"] and not row["skipped"]
          and all(x["status"] == "ok" for x in row["results"]))
    if expected_methods is not None:
        ok = ok and sorted(x["method"] for x in row["results"]) == sorted(expected_methods)
    return ok


def expected_methods():
    cases = json.loads((REPO / FIXTURE).read_text(encoding="utf-8"))
    ids = [c["id"] for c in cases["cases"]]
    for kind in cases["required_invalid_kinds"]:
        ids += ["invalid_unit_" + kind, "invalid_receipt_" + kind]
    dims = {}
    for c in cases["cases"]:
        dims.setdefault(c["dimension"], []).append(c["id"])
    dims["invalid_unit"] = ["invalid_unit_" + k for k in cases["required_invalid_kinds"]]
    dims["invalid_receipt"] = ["invalid_receipt_" + k for k in cases["required_invalid_kinds"]]
    return ["test_case_" + i for i in ids] + list(STATIC_TESTS), dims


def identity(manifest):
    """Verify the operator manifest and bind the candidate HEAD, diff and pins."""
    problems = []
    base = manifest.get("head", "")
    if manifest.get("schema") != "agentorange.source-manifest/v1":
        problems.append("manifest_schema")
    if manifest.get("repo") != str(REPO):
        problems.append("manifest_repo")
    rc, base_tree = git(REPO, "rev-parse", base + "^{tree}")
    if rc != 0 or base_tree != manifest.get("tree"):
        problems.append("manifest_base_tree")
    _, head = git(REPO, "rev-parse", "HEAD")
    _, tree = git(REPO, "rev-parse", "HEAD^{tree}")
    rc, _ = git(REPO, "merge-base", "--is-ancestor", base, head)
    if rc != 0:
        problems.append("base_not_ancestor")
    for key in ("credential_scan_python3", "detect_secrets", "repo_venv"):
        if not Path(manifest.get("runtime", {}).get(key, "")).exists():
            problems.append("runtime_path_missing:" + key)
    _, status = git(REPO, "status", "--porcelain", "--untracked-files=all")
    _, names = git(REPO, "diff", "--name-only", base, head)
    changed = [n for n in names.splitlines() if n]
    if not set(changed) <= set(AUTHORED):
        problems.append("diff_outside_write_set")
    _, revs = git(REPO, "rev-list", "--reverse", base + ".." + head)
    commits = []
    for rev in [r for r in revs.splitlines() if r]:
        _, body = git(REPO, "show", "-s", "--format=%B", rev)
        signed = "Signed-off-by:" in body
        if not signed:
            problems.append("unsigned_commit")
        commits.append({"commit": rev, "signed_off": signed})
    diff = subprocess.run(["git", "diff", "--binary", base, head], cwd=str(REPO), capture_output=True, timeout=300).stdout
    base_blobs, head_blobs = {}, {}
    for path in AUTHORED + UNCHANGED:
        b = subprocess.run(["git", "show", base + ":" + path], cwd=str(REPO), capture_output=True, timeout=60)
        c = subprocess.run(["git", "show", head + ":" + path], cwd=str(REPO), capture_output=True, timeout=60)
        base_blobs[path] = sha(b.stdout) if b.returncode == 0 else None
        head_blobs[path] = sha(c.stdout) if c.returncode == 0 else None
    for path in UNCHANGED:
        if base_blobs[path] is None or base_blobs[path] != head_blobs[path] or file_sha(REPO / path) != base_blobs[path]:
            problems.append("unchanged_file_changed:" + path)
    for path in AUTHORED:
        if file_sha(REPO / path) != head_blobs[path]:
            problems.append("worktree_differs_from_head:" + path)
    return {"base": base, "base_tree": base_tree, "head": head, "tree": tree, "status_porcelain": status,
            "clean": status == "", "changed_files": changed, "commits": commits,
            "diff_sha256": sha(diff), "diff_bytes": len(diff), "product_changed": PRODUCT in changed,
            "base_blob_sha256": base_blobs, "head_blob_sha256": head_blobs, "problems": problems}


def test_env(run_root, name="tmp"):
    tmp = run_root / name
    tmp.mkdir(mode=0o700)
    env = dict(os.environ)
    env["TMPDIR"] = str(tmp)
    return env


def phase_suite(run, py, ident):
    want, dims = expected_methods()
    row = run.child("suite", [py, "-m", "unittest", "-v", TEST], REPO, test_env(run.root))
    control = [x for x in row["results"] if x["method"] == CONTROL_TEST]
    passed = unittest_clean(row, want) and len(control) == 1 and control[0]["status"] == "ok"
    return passed, {"expected_methods": want, "expected_count": len(want), "case_dimensions": dims,
                    "observed_count": row["tests_run"], "failing": row["failing"], "skipped": row["skipped"],
                    "negative_control": {"test": CONTROL_TEST, "status": control[0]["status"] if control else None,
                                         "mutation": "in-memory source-derived validate_bundle with only the receipt_attempt_kind_mismatch line replaced by pass"},
                    "pre_fix_red_expected_when_product_unchanged": not ident["product_changed"]}


def phase_compatibility(run, py, ident):
    final = run.child("compat-final", [py, "-m", "unittest", "-v", *SELECTORS], REPO, test_env(run.root))
    base_clone = run.root / "base-clone"
    clone = run.child("base-clone", ["git", "clone", "--local", "--no-hardlinks", "--no-checkout",
                                     "--no-recurse-submodules", REPO, base_clone], run.root)
    checkout = run.child("base-checkout", ["git", "checkout", "--detach", ident["base"]], base_clone)
    _, clone_head = git(base_clone, "rev-parse", "HEAD")
    baseline = run.child("compat-baseline", [py, "-m", "unittest", "-v", *SELECTORS], base_clone,
                         test_env(run.root, "baseline-tmp"))
    final_ids = sorted(x["id"] for x in final["results"])
    base_ids = sorted(x["id"] for x in baseline["results"])
    passed = (unittest_clean(final) and unittest_clean(baseline) and clone["exit"] == 0 and checkout["exit"] == 0
              and clone_head == ident["base"] and final_ids == base_ids)
    return passed, {"selectors": list(SELECTORS), "final_ids": final_ids, "baseline_ids": base_ids,
                    "baseline_head": clone_head, "same_ids": final_ids == base_ids,
                    "intentional_fixture_or_behavior_deltas": [],
                    "scope_note": "scoped compatibility only; mandatory full discovery and exact-head required CI remain OWED to the existing shipping operator"}


def snapshot():
    import socket
    import tempfile
    mask = os.umask(0o022)
    os.umask(mask)
    return {"environ": dict(os.environ), "tempdir": tempfile.tempdir, "sys_path": list(sys.path), "cwd": os.getcwd(),
            "umask": mask, "socket": [id(socket.socket.connect), id(socket.socket.connect_ex),
                                      id(socket.create_connection), id(socket.getaddrinfo)],
            "argv": list(sys.argv)}


def discovery_child():
    """In-process discovery isolation probe (run as a separate child by phase portable)."""
    import unittest
    sys.path.insert(0, str(REPO))
    stream = io.StringIO()
    runner = unittest.TextTestRunner(stream=stream, verbosity=2)
    loader = unittest.TestLoader()
    s0 = snapshot()
    before = runner.run(loader.loadTestsFromName(SENTINEL))
    s1 = snapshot()
    suite = loader.discover(start_dir=str(REPO / "tests" / "records"), pattern="test_fc13_attempt_binding.py",
                            top_level_dir=str(REPO))
    s2 = snapshot()
    from campaign_tool.records.understanding import evidence
    pinned_reader = id(evidence.read_pinned)
    mid = runner.run(suite)
    s3 = snapshot()
    after = runner.run(loader.loadTestsFromName(SENTINEL))
    s4 = snapshot()
    report = {"sentinel_before": [before.testsRun, len(before.failures), len(before.errors), len(before.skipped)],
              "discovered": [mid.testsRun, len(mid.failures), len(mid.errors), len(mid.skipped)],
              "sentinel_after": [after.testsRun, len(after.failures), len(after.errors), len(after.skipped)],
              "import_time_unchanged": s1 == s2, "run_unchanged": s2 == s3 and s3 == s4 and s0 == s1,
              "read_pinned_restored": id(evidence.read_pinned) == pinned_reader}
    sys.stderr.write(stream.getvalue())
    sys.stdout.write(json.dumps(report, sort_keys=True) + "\n")
    ok = (report["import_time_unchanged"] and report["run_unchanged"] and report["read_pinned_restored"]
          and before.wasSuccessful() and mid.wasSuccessful() and after.wasSuccessful() and mid.testsRun > 0
          and before.testsRun > 0 and not mid.skipped and not before.skipped and not after.skipped)
    return 0 if ok else 1


def phase_portable(run, py, ident):
    want, _ = expected_methods()
    cwd = run.root / "unrelated-cwd"
    cwd.mkdir()
    tmp_root = run.root / "script-temp-root"
    tmp_root.mkdir(mode=0o700)
    script = run.child("portable-script", [py, REPO / TEST_FILE, "--fc13-tmp-root", tmp_root, "-v"], cwd)
    disc = run.child("discovery-isolation", [py, REPO / RUNNER, "--discovery-child"], REPO, test_env(run.root))
    try:
        disc_report = json.loads((run.root / disc["stdout"]).read_text())
    except ValueError:
        disc_report = None
    leftovers = sorted(p.name for p in tmp_root.iterdir())
    box = {}
    for path in (TEST_FILE, FIXTURE, RUNNER):
        text = (REPO / path).read_text(encoding="utf-8")
        box[path] = [t for t in BOX_TOKENS if t in text]
    no_box = not any(box.values())
    disc_methods = [x["method"] for x in disc["results"] if x["id"].startswith(TEST)]
    passed = (unittest_clean(script, want) and disc["exit"] == 0 and isinstance(disc_report, dict)
              and sorted(disc_methods) == sorted(want) and no_box and not disc["failing"] and not disc["skipped"])
    return passed, {"unrelated_cwd": str(cwd), "temporary_root": str(tmp_root), "temporary_root_leftovers": leftovers,
                    "script_count": script["tests_run"], "discovery_report": disc_report,
                    "discovery_new_module_methods": len(disc_methods), "box_path_tokens_found": box, "no_box_paths": no_box}


def phase_credentials(run, py, manifest):
    runtime = manifest["runtime"]
    expected_py = runtime["credential_scan_python3"]
    interpreter_ok = os.path.abspath(sys.executable) == os.path.abspath(expected_py)
    scan = run.child("detect-secrets", [runtime["detect_secrets"], "scan", "--all-files", "--exclude-files", "^.git/",
                                        "--no-verify"], REPO)
    raw = (run.root / scan["stdout"]).read_bytes()
    report = run.root / "scanner-report.json"
    report_sha = write_new(report, raw)
    check = run.child("check-secret-report", [py, REPO / "tools/check_secret_report.py", report], REPO)
    try:
        parsed = json.loads(raw)
        entries = len(parsed.get("results", {}))
        parse_ok = isinstance(parsed.get("results"), dict)
    except ValueError:
        entries, parse_ok = None, False
    passed = interpreter_ok and scan["exit"] == 0 and check["exit"] == 0 and parse_ok
    return passed, {"interpreter": sys.executable, "manifest_credential_scan_python3": expected_py,
                    "interpreter_matches_manifest": interpreter_ok, "scanner": runtime["detect_secrets"],
                    "report": report.name, "report_sha256": report_sha, "report_result_files": entries,
                    "checker_sha256": file_sha(REPO / "tools/check_secret_report.py")}


def phase_public_tree(run, py, ident):
    _, inventory = git(REPO, "ls-files", "-s")
    _, untracked = git(REPO, "ls-files", "--others", "--exclude-standard")
    scan_visible_untracked = [l for l in untracked.splitlines() if l]
    clone = run.root / "committed-tree"
    c1 = run.child("clone", ["git", "clone", "--local", "--no-hardlinks", "--no-checkout", "--no-recurse-submodules",
                             REPO, clone], run.root)
    c2 = run.child("checkout", ["git", "checkout", "--detach", ident["head"]], clone)
    _, clone_head = git(clone, "rev-parse", "HEAD")
    _, clone_tree = git(clone, "rev-parse", "HEAD^{tree}")
    authored_match = all(file_sha(clone / p) == file_sha(REPO / p) == ident["head_blob_sha256"][p] for p in AUTHORED)
    scan = run.child("public-tree-committed", [py, clone / "tools/check_public_tree.py"], clone)
    try:
        committed = json.loads((run.root / scan["stdout"]).read_text())
        findings = committed.get("findings")
    except (ValueError, AttributeError):
        committed, findings = None, None
    inplace = run.child("public-tree-in-place-informational", [py, REPO / "tools/check_public_tree.py"], REPO)
    try:
        inplace_findings = json.loads((run.root / inplace["stdout"]).read_text()).get("findings")
    except (ValueError, AttributeError):
        inplace_findings = None
    venv_ok = (REPO / ".venv").is_symlink() and os.readlink(REPO / ".venv") == manifest_venv(run)
    inplace_ok = ((inplace["exit"] == 0 and inplace_findings == []) or
                  (inplace["exit"] == 1 and inplace_findings == [[".venv", 0, "symlink_not_scanned"]] and venv_ok))
    after = identity(load_manifest(run.manifest_path))
    passed = (ident["clean"] and scan_visible_untracked in ([], [".venv"]) and venv_ok and c1["exit"] == 0
              and c2["exit"] == 0 and clone_head == ident["head"] and clone_tree == ident["tree"] and authored_match
              and scan["exit"] == 0 and findings == [] and inplace_ok
              and after["head"] == ident["head"] and after["clean"] and not after["problems"])
    return passed, {"inventory_sha256": sha(inventory.encode()), "tracked_entries": len(inventory.splitlines()),
                    "scan_visible_untracked": scan_visible_untracked,
                    "clone": str(clone), "clone_head": clone_head, "clone_tree": clone_tree,
                    "authored_match": authored_match, "committed_findings": findings,
                    "in_place_informational": {"exit": inplace["exit"], "findings": inplace_findings,
                                               "recognized": inplace_ok, "venv_link_manifest_bound": venv_ok,
                                               "note": "INC-174 informational only; the pass is the committed-tree clone"},
                    "checker_sha256": file_sha(clone / "tools/check_public_tree.py"),
                    "allowlist_sha256": file_sha(clone / "campaign_tool/records/public_scan.py"),
                    "post_check_identity": {"head": after["head"], "clean": after["clean"], "problems": after["problems"]}}


def load_manifest(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def manifest_venv(run):
    return load_manifest(run.manifest_path)["runtime"]["repo_venv"]


def seal(run_root):
    files = []
    for p in sorted(run_root.rglob("*")):
        rel = p.relative_to(run_root).as_posix()
        if rel.split("/")[0] in ("committed-tree", "base-clone") or p.is_dir() or p.is_symlink():
            continue
        files.append({"path": rel, "sha256": file_sha(p), "bytes": p.stat().st_size})
    body = {"schema": "fc13-attempt-binding-evidence-manifest-v1", "files": files,
            "excluded_trees": ["committed-tree", "base-clone"],
            "note": "sealed after every child process exited; reruns use a new directory"}
    raw = json.dumps(body, sort_keys=True, indent=2).encode() + b"\n"
    return write_new(run_root / "evidence-manifest.json", raw)


def main():
    if sys.argv[1:] == ["--discovery-child"]:
        return discovery_child()
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=PHASES, required=True)
    ap.add_argument("--source-manifest", required=True)
    ap.add_argument("--out-base", required=True)
    args = ap.parse_args()
    manifest_path = Path(args.source_manifest).absolute()
    manifest = load_manifest(manifest_path)
    out_base = Path(args.out_base).absolute()
    if out_base == REPO or REPO in out_base.parents:
        raise SystemExit("out_base_inside_repo")
    ident = identity(manifest)
    out_base.mkdir(parents=True, exist_ok=True)
    root = out_base / (args.phase + "-" + str(uuid.uuid4()))
    root.mkdir(mode=0o700)
    run = Run(root, manifest_path)
    py = sys.executable
    started = datetime.datetime.now(datetime.timezone.utc).isoformat()
    if ident["problems"] or not ident["clean"]:
        passed, detail = False, {"identity_refused": True}
    elif args.phase == "suite":
        passed, detail = phase_suite(run, py, ident)
    elif args.phase == "compatibility":
        passed, detail = phase_compatibility(run, py, ident)
    elif args.phase == "portable":
        passed, detail = phase_portable(run, py, ident)
    elif args.phase == "credentials":
        passed, detail = phase_credentials(run, py, manifest)
    else:
        passed, detail = phase_public_tree(run, py, ident)
    receipt = {"schema": "fc13-attempt-binding-receipt-v1", "phase": args.phase,
               "argv": [py, str(Path(__file__).resolve()), *sys.argv[1:]], "cwd": os.getcwd(), "interpreter": py,
               "source_manifest": str(manifest_path), "source_manifest_sha256": file_sha(manifest_path),
               "identity": ident, "fixture_sha256": file_sha(REPO / FIXTURE),
               "authored_sha256": {p: file_sha(REPO / p) for p in AUTHORED},
               "children": copy.deepcopy(run.children), "detail": detail, "pass": bool(passed),
               "exit": 0 if passed else 1, "review_required": True, "semantic_acceptance": "NOT_ASSESSED",
               "full_discovery": "operator-run (OWED)", "started_utc": started,
               "completed_utc": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    receipt_sha = write_new(root / "receipt.json", json.dumps(receipt, sort_keys=True, indent=2).encode() + b"\n")
    manifest_sha = seal(root)
    print(json.dumps({"phase": args.phase, "pass": bool(passed), "run_dir": str(root), "head": ident["head"],
                      "receipt_sha256": receipt_sha, "evidence_manifest_sha256": manifest_sha,
                      "review_required": True}, sort_keys=True))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
