"""Bounded FA1604 unittest/evidence launcher; never grants stage acceptance."""
import argparse
from contextlib import redirect_stdout, redirect_stderr
import hashlib
import importlib
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
from unittest.mock import patch
import uuid

sys.dont_write_bytecode = True
REPO = Path(__file__).resolve().parents[1]
TEST = "tests.records.test_fa1604_occurrence_alias_graph"
BEHAVIOR = [TEST + ".AliasGraphTests." + name for name in (
    "test_alias_graph_order_and_replay", "test_only_missing_parent_child_is_blocked")]
COMPATIBILITY = ["tests.records.test_ledger_occurrences.OccurrenceProjectionTests." + name for name in (
    "test_explicit_mail_parent_and_hierarchical_attachment",
    "test_replay_adds_no_occurrences_rows_or_gaps",
    "test_parent_hash_alone_does_not_select_a_message_occurrence")]
AUTHORED = ["tests/records/test_fa1604_occurrence_alias_graph.py", "tools/accept_fa1604.py"]
SOURCE = ["campaign_tool/__init__.py", "campaign_tool/records/__init__.py",
          "campaign_tool/records/ledger/__init__.py", "campaign_tool/records/ledger/store.py",
          "campaign_tool/records/ledger/occurrence_projection.py",
          "campaign_tool/records/ledger/migrations/__init__.py",
          "campaign_tool/records/ledger/migrations/v001.py",
          "campaign_tool/records/ledger/migrations/occurrence_projection_v001.py",
          "tests/__init__.py", "tests/records/__init__.py", "tests/records/test_ledger.py",
          "tests/records/test_ledger_occurrences.py"]


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def safe(path):
    path = Path(os.path.abspath(path))
    require(not any(p.is_symlink() for p in (path, *path.parents)), "symlink ancestry refused")
    return path


def dump(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def command(argv, cwd, run, label):
    proc = subprocess.run([str(a) for a in argv], cwd=cwd, capture_output=True, check=False)
    stdout, stderr = run / (label + ".stdout"), run / (label + ".stderr")
    stdout.write_bytes(proc.stdout)
    stderr.write_bytes(proc.stderr)
    row = {"argv": [str(a) for a in argv], "cwd": str(cwd), "exit": proc.returncode,
           "stdout": str(stdout), "stdout_sha256": digest(stdout),
           "stderr": str(stderr), "stderr_sha256": digest(stderr)}
    dump(run / (label + ".command.json"), row)
    return proc, row


def git(*args, cwd=REPO):
    return subprocess.check_output(["git", *args], cwd=cwd).decode().strip()


def identity():
    manifest_path = safe(REPO.parent / "SOURCE-MANIFEST.json")
    manifest = json.loads(manifest_path.read_text())
    base = manifest["head"]
    require(re.fullmatch(r"[a-f0-9]{40}", base) is not None, "invalid manifest base")
    head = git("rev-parse", "HEAD")
    require(git("merge-base", base, head) == base, "source not descended from admitted base")
    source = {p: {"sha256": digest(REPO / p), "base_blob": git("rev-parse", base + ":" + p),
                  "head_blob": git("rev-parse", head + ":" + p)} for p in SOURCE}
    require(all(r["base_blob"] == r["head_blob"] for r in source.values()), "read-only import closure changed")
    for p in SOURCE:
        require(git("hash-object", p) == source[p]["head_blob"], "uncommitted source drift")
    return {"manifest_path": str(manifest_path), "manifest_sha256": digest(manifest_path),
            "base": base, "head": head, "tree": git("rev-parse", "HEAD^{tree}"),
            "source": source, "authored_sha256": {p: digest(REPO / p) for p in AUTHORED},
            "status": git("status", "--porcelain=v1", "--untracked-files=all")}, manifest


def test_ids(suite):
    result = []
    for item in suite:
        result.extend(test_ids(item) if isinstance(item, unittest.TestSuite) else [item.id()])
    return result


def run_tests(module, names, run, label, expected_failure=False):
    stream, out, err = io.StringIO(), io.StringIO(), io.StringIO()
    fixtures = run / (label + "-fixtures")
    fixtures.mkdir(mode=0o700)
    tmp = fixtures / "tmp"
    tmp.mkdir(mode=0o700)
    with patch.object(module, "FIXTURE_ROOT", fixtures), patch.object(tempfile, "tempdir", str(tmp)), module.synthetic_boundary(fixtures):
        loader = unittest.TestLoader()
        suite = loader.loadTestsFromNames(names)
        selected = test_ids(suite)
        require(not loader.errors and selected == names, "missing or substituted test selector")
        with redirect_stdout(out), redirect_stderr(err):
            result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    (run / (label + ".stdout")).write_text(out.getvalue())
    (run / (label + ".stderr")).write_text(err.getvalue() + stream.getvalue())
    row = {"selected_test_ids": selected, "executed": result.testsRun, "skipped": len(result.skipped),
           "errors": len(result.errors), "failures": len(result.failures),
           "stdout_sha256": digest(run / (label + ".stdout")),
           "stderr_sha256": digest(run / (label + ".stderr")), "fixture_root": str(fixtures),
           "fixture_sha256": {str(p.relative_to(fixtures)): digest(p) for p in sorted(fixtures.rglob("*")) if p.is_file()},
           "expected_failure": expected_failure}
    dump(run / (label + ".tests.json"), row)
    require(result.testsRun == len(names) and result.testsRun > 0 and not result.skipped and not result.errors,
            "zero/missing/skipped/errored tests")
    if expected_failure:
        require(len(result.failures) == 1 and result.failures[0][0].id() == names[0], "wrong negative-control failure")
        require("alias-cardinality: legacy aliases must coalesce" in result.failures[0][1], "unrelated failure")
    else:
        require(result.wasSuccessful(), "required tests failed; inspect retained output")
    return row


def negative(module, run):
    from campaign_tool.records.ledger import occurrence_projection as projector
    original_plan, original_project = projector._base_plan, projector.project_occurrences
    observed = []

    def recording_project(database, import_id, **kwargs):
        result = original_project(database, import_id, **kwargs)
        with projector.ledger(database, readonly=True) as con:
            rows = [dict(r) for r in con.execute("SELECT * FROM occurrences")]
        observed.append({"result": result, "rows": rows})
        return result

    with patch.object(projector, "_base_plan", module.faulty_mail_identity(original_plan)), patch.object(projector, "project_occurrences", recording_project):
        failed = run_tests(module, BEHAVIOR[:1], run, "fault-legacy-oid", expected_failure=True)
    require(projector._base_plan is original_plan and projector.project_occurrences is original_project, "fault leaked")
    require(len(observed) == 1, "fault did not exercise real projector")
    result, rows = observed[0]["result"], observed[0]["rows"]
    require(result["canonical_occurrences_in_projection"] == 7 and result["canonical_kinds"] == {"mail": 3, "attachment": 4}, "unisolated identity fault")
    mails = {r["id"]: json.loads(r["source_ref"]) for r in rows if r["kind"] == "mail"}
    require(sorted(r["uid"] for r in mails.values()) == [7, 7, 8], "distinct native mail boundary lost")
    parts = [(mails[r["parent_occurrence_id"]]["uid"], json.loads(r["source_ref"])["mime_part_path"]) for r in rows if r["kind"] == "attachment"]
    require(sorted(parts) == [(7, "1.2"), (7, "1.2"), (7, "1.3"), (8, "1.2")], "distinct MIME/parent boundary lost")
    dump(run / "fault-observed.json", observed)
    return [failed, run_tests(module, BEHAVIOR, run, "restored-positive")]


def isolation(module, run):
    originals = (tempfile.tempdir, list(sys.path), socket_state(), os.umask(0o077))
    os.umask(originals[3])
    rows = [run_tests(module, BEHAVIOR, run, "ordinary-before"),
            run_tests(module, COMPATIBILITY, run, "peer-before"),
            run_tests(module, BEHAVIOR, run, "second-prefix"),
            run_tests(module, COMPATIBILITY, run, "peer-after"),
            run_tests(module, BEHAVIOR, run, "ordinary-after")]
    require((tempfile.tempdir, list(sys.path), socket_state()) == originals[:3], "ambient fixture/mock/path leak")
    mask = os.umask(originals[3])
    require(mask == originals[3], "ambient umask leak")
    for name in AUTHORED:
        text = (REPO / name).read_text()
        require(not re.search(r"/(?:home|Users|workspace)/", text), "box-only path in authored code")
        require(not re.search(r"(?<![a-fA-F0-9])[a-fA-F0-9]{40}(?![a-fA-F0-9])|(?<![a-fA-F0-9])[a-fA-F0-9]{64}(?![a-fA-F0-9])", text), "literal hex identity in authored code")
    return rows


def socket_state():
    import socket
    return (socket.socket.connect, socket.socket.connect_ex, socket.create_connection, subprocess.Popen)


def exact_checkout(run, initial):
    require(not initial["status"], "scan requires clean committed candidate")
    head = initial["head"]
    changed = git("diff", "--name-only", initial["base"], head).splitlines()
    require(set(changed) == set(AUTHORED), "authored path scope mismatch")
    target = safe(run / "exact-head")
    proc, clone = command(["git", "clone", "--local", "--no-hardlinks", "--no-checkout", REPO, target], REPO, run, "clone")
    require(proc.returncode == 0, "local clone failed")
    proc, checkout = command(["git", "checkout", "--detach", head], target, run, "checkout")
    require(proc.returncode == 0, "literal HEAD checkout failed")
    require(git("rev-parse", "HEAD", cwd=target) == head, "export head mismatch")
    require(git("rev-parse", "HEAD^{tree}", cwd=target) == initial["tree"], "export tree mismatch")
    names = git("ls-files", "-z").split("\0")
    exported = git("ls-files", "-z", cwd=target).split("\0")
    require(names == exported and set(AUTHORED).issubset(names), "incomplete tracked export")
    checks = {}
    for name in names:
        if not name:
            continue
        source, dest = safe(REPO / name), safe(target / name)
        require(source.is_file() and dest.is_file(), "nonregular tracked file")
        require(digest(source) == digest(dest), "export bytes mismatch")
        require((source.stat().st_dev, source.stat().st_ino) != (dest.stat().st_dev, dest.stat().st_ino), "hardlinked export file")
        require(git("hash-object", name, cwd=target) == git("rev-parse", head + ":" + name), "export differs from HEAD")
        checks[name] = digest(dest)
    require(not git("status", "--porcelain=v1", "--untracked-files=all", cwd=target), "dirty scan export")
    dump(run / "export.json", {"head": head, "tree": initial["tree"], "files": checks, "clone": clone, "checkout": checkout})
    return target


def scan(case, run, initial, manifest):
    target = exact_checkout(run, initial)
    if case == "credential-scan":
        runtime = manifest["runtime"]
        interpreter, entry = Path(runtime["credential_scan_python3"]), Path(runtime["detect_secrets"])
        require(interpreter.is_file() and entry.is_file(), "declared scanner runtime missing; operator staging required")
        proc, scanner = command([interpreter, "-B", entry, "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"], target, run, "detect-secrets")
        require(proc.returncode == 0, "detect-secrets failed")
        report = run / "detect-secrets.stdout"
        parsed = json.loads(proc.stdout)
        require(isinstance(parsed.get("results"), dict), "missing real scanner results")
        proc, checker = command([interpreter, "-B", target / "tools/check_secret_report.py", report], target, run, "secret-checker")
        require(proc.returncode == 0 and b"potential secrets: 0;" in proc.stdout, "unapproved potential secrets")
        return [scanner, checker]
    proc, row = command([sys.executable, "-B", target / "tools/check_public_tree.py"], target, run, "public-tree")
    require(proc.returncode == 0, "public-tree failed")
    result = json.loads(proc.stdout)
    require(result["scanned_files"] > 0 and result["findings"] == [], "incomplete/violating public tree")
    return [row]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", required=True, choices=["behavior", "compatibility", "negative-control", "isolation", "credential-scan", "public-tree"])
    parser.add_argument("--output-root", required=True, type=Path)
    args = parser.parse_args()
    output = safe(args.output_root)
    candidate = safe(REPO.parent)
    require(output.is_relative_to(candidate) and not output.is_relative_to(REPO) and output != candidate, "output must be outside repo in candidate root")
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    run = output / ("run-" + uuid.uuid4().hex)
    run.mkdir(mode=0o700)
    tmp = run / "tmp"
    tmp.mkdir(mode=0o700)
    old_tempdir = tempfile.tempdir
    tempfile.tempdir = str(tmp)
    sys.path.insert(0, str(REPO))
    receipt = {"argv": sys.argv, "cwd": str(Path.cwd()), "interpreter": sys.executable,
               "case": args.case, "run": str(run), "exit": 1, "status": "FAILED"}
    try:
        require(Path.cwd().resolve() == REPO, "acceptance cwd must equal candidate repo")
        initial, manifest = identity()
        receipt["identity"] = initial
        dump(run / "initial-identity.json", initial)
        if args.case in ("credential-scan", "public-tree"):
            receipt["checks"] = scan(args.case, run, initial, manifest)
        else:
            # This module imports stdlib only. Its scoped guard is active before
            # loading any application or compatibility module.
            module = importlib.import_module(TEST)
            with module.synthetic_boundary(run):
                importlib.import_module("campaign_tool.records.ledger.occurrence_projection")
                if args.case == "behavior":
                    receipt["checks"] = [run_tests(module, BEHAVIOR, run, "behavior")]
                elif args.case == "compatibility":
                    receipt["checks"] = [run_tests(module, COMPATIBILITY, run, "compatibility")]
                elif args.case == "negative-control":
                    receipt["checks"] = negative(module, run)
                else:
                    # identity uses git, so isolation's unchanged-source check
                    # happens after leaving the scoped process sentinel.
                    receipt["checks"] = isolation(module, run)
        require(identity()[0] == initial, "source identity changed during acceptance")
        receipt.update(exit=0, status="PASS_REVIEW_REQUIRED")
    except Exception:
        receipt["error"] = traceback.format_exc()
        (run / "failure.stderr").write_text(receipt["error"])
    finally:
        tempfile.tempdir = old_tempdir
    dump(run / "receipt.json", receipt)
    print(json.dumps({"exit": receipt["exit"], "status": receipt["status"], "receipt": str(run / "receipt.json")}))
    return receipt["exit"]


if __name__ == "__main__":
    raise SystemExit(main())
