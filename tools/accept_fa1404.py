"""Scoped FA1404 acceptance launcher (private snapshot history no-replace guards).

One case per invocation; every invocation writes a new unique child of
--output-root and never touches earlier children:

  behavior          A1-A4 support cases in tests/records/test_fa1404.py
  compatibility     exactly four named SnapshotAcceptanceTests methods
  negative-control  two in-memory acceptance.SQL controls, each run against
                    the deciding test for its predicate: (1) without ONLY the
                    three added triggers, (2) without ONLY the reserved -1
                    trigger. Each deciding test must fail on its preservation
                    assertion, the unaffected positive must pass, then the
                    restored candidate must pass
  isolation         second fresh temp prefix, source/module identity, and an
                    unrelated peer suite before/after with no global poisoning
  credential-scan   SOURCE-MANIFEST runtime detect-secrets scan, then the
                    unchanged tools/check_secret_report.py on that exact JSON
  public-tree       unchanged tools/check_public_tree.py under repo .venv

Before any repository import, bytecode and temp roots are redirected into the
run directory and an audit hook refuses network, child processes and writes or
SQLite opens outside it. The hook is an offline guard for trusted synthetic
tests, not a sandbox. A pass is REVIEW REQUIRED: scoped evidence only, not full
repository CI, semantic acceptance or live permission.
"""
import argparse
import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile

REPO = Path(__file__).resolve().parents[1]
MANIFEST = REPO.parent / "SOURCE-MANIFEST.json"
PRODUCT = "campaign_tool/records/ledger_catalog_acceptance.py"
AUTHORED = (PRODUCT, "tests/records/test_fa1404.py", "tools/accept_fa1404.py")
REFERENCED = ("tests/records/test_ledger_catalog_acceptance.py", "tests/records/test_ledger_catalog.py",
              "campaign_tool/records/ledger_catalog.py", "tools/check_secret_report.py",
              "tools/check_public_tree.py")
NEW = "tests.records.test_fa1404.FA1404NoReplaceTests."
OLD = "tests.records.test_ledger_catalog_acceptance.SnapshotAcceptanceTests."
DECIDING = NEW + "test_a1_replace_refused_recursive_triggers_off"
RESERVED_DECIDING = NEW + "test_a2_reserved_row_auto_insert_continues_recursive_triggers_off"
UNAFFECTED = NEW + "test_a2_noncolliding_inserts_and_pointer_cas_continue"
PEER = "tests.records.test_ledger_catalog.OverlayTests"
SELECTORS = {
    "behavior": tuple(NEW + n for n in (
        "test_a1_replace_refused_recursive_triggers_off", "test_a1_replace_refused_recursive_triggers_on",
        "test_a1_legacy_database_gains_guards_on_next_acceptance",
        "test_a2_first_cas_and_exact_replay_one_event_each", "test_a2_response_loss_and_directory_sync_recovery",
        "test_a2_noncolliding_inserts_and_pointer_cas_continue",
        "test_a2_reserved_row_auto_insert_continues_recursive_triggers_off",
        "test_a2_reserved_row_auto_insert_continues_recursive_triggers_on",
        "test_a1_explicit_reserved_sequence_refused",
        "test_a3_no_new_publication_or_semantic_authority",
        "test_a4_control_variant_removes_only_added_guards",
        "test_a4_reserved_guard_is_load_bearing")),
    "compatibility": tuple(OLD + n for n in (
        "test_transactional_pointer_exact_bindings", "test_replay_same_receipt_one_event",
        "test_pointer_compare_and_swap_and_no_old_reactivation",
        "test_commit_then_response_failure_replay_recovers")),
}
CASES = ("behavior", "compatibility", "negative-control", "isolation", "credential-scan", "public-tree")
LABEL = "REVIEW REQUIRED - scoped offline FA1404 acceptance, not full-repository CI or semantic acceptance"
WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND


def sha256_bytes(raw):
    return hashlib.sha256(raw).hexdigest()


def sha256_file(path):
    return sha256_bytes(Path(path).read_bytes())


def git(*args):
    done = subprocess.run(["git", *args], cwd=REPO, capture_output=True, check=False)
    if done.returncode != 0:
        raise RuntimeError("git " + args[0] + " failed")
    return done.stdout


def identity():
    status = git("status", "--porcelain", "--untracked-files=all").decode()
    return {"head": git("rev-parse", "HEAD").decode().strip(), "status": status.splitlines(),
            "clean_tree": status == "",
            "authored_sha256": {p: sha256_file(REPO / p) for p in AUTHORED if (REPO / p).is_file()},
            "referenced_sha256": {p: sha256_file(REPO / p) for p in REFERENCED}}


def tree_hashes(parent):
    return {str(p.relative_to(parent)): sha256_file(p) for p in sorted(parent.rglob("*"))
            if p.is_file() and not p.is_symlink()}


class OfflineGuard:
    NETWORK = ("socket.connect", "socket.getaddrinfo", "socket.gethostbyname", "socket.bind",
               "socket.sendto", "urllib.Request")
    PROCESS = ("subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn", "os.fork",
               "os.forkpty", "pty.spawn")
    # event -> ((path argument index, dir_fd argument index or None), ...)
    PATHS = {"os.mkdir": ((0, 2),), "os.remove": ((0, 1),), "os.rmdir": ((0, 1),),
             "os.rename": ((0, 2), (1, 3)), "os.replace": ((0, 2), (1, 3)), "os.chmod": ((0, 2),),
             "os.chown": ((0, 3),), "os.symlink": ((1, 2),), "os.link": ((0, 2), (1, 3)),
             "os.truncate": ((0, None),), "shutil.rmtree": ((0, 1),)}

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.active = False
        self.refusals = []
        self.mkdirs = []

    def inside(self, value, dir_fd=None):
        if isinstance(value, int):
            return True
        try:
            text = os.fsdecode(value)
        except TypeError:
            return False
        if text.startswith("file:"):
            text = text[5:].split("?", 1)[0]
        path = Path(text)
        if not path.is_absolute():
            base = os.readlink("/proc/self/fd/" + str(dir_fd)) if isinstance(dir_fd, int) else os.getcwd()
            path = Path(base) / path
        resolved = Path(os.path.realpath(path))
        return resolved == self.root or resolved.is_relative_to(self.root)

    def refuse(self, event, detail):
        self.refusals.append({"event": event, "detail": str(detail)[:200]})
        raise PermissionError("FA1404 offline guard refused " + event)

    def __call__(self, event, args):
        if not self.active:
            return
        if event in self.NETWORK:
            self.refuse(event, "")
        elif event in self.PROCESS:
            self.refuse(event, args[:1])
        elif event == "open":
            path, mode, flags = args
            writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or \
                (isinstance(flags, int) and flags & WRITE_FLAGS)
            if writing and path is not None and not self.inside(path):
                self.refuse(event, path)
        elif event == "sqlite3.connect":
            if args[0] not in (":memory:", b":memory:") and not self.inside(args[0]):
                self.refuse(event, args[0])
        elif event in self.PATHS:
            for index, fd_index in self.PATHS[event]:
                if index >= len(args) or args[index] is None:
                    continue
                dir_fd = args[fd_index] if fd_index is not None and fd_index < len(args) else None
                if not self.inside(args[index], dir_fd):
                    self.refuse(event, args[index])
            if event == "os.mkdir":
                self.mkdirs.append(os.fsdecode(args[0]) if not isinstance(args[0], int) else "")


def tests_of(suite):
    import unittest
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from tests_of(item)
        else:
            yield item


def run_named(names, stream, title):
    """Run exact selectors; return a counts record (never decides acceptance alone)."""
    import unittest
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(n) for n in names)
    selected = [t.id() for t in tests_of(suite)]
    stream.write("== " + title + " ==\n")
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    return {"title": title, "selected": selected, "tests_run": result.testsRun,
            "failures": {t.id(): tb for t, tb in result.failures},
            "errors": {t.id(): tb for t, tb in result.errors},
            "skipped": [t.id() for t, _ in result.skipped],
            "expected_failures": [t.id() for t, _ in result.expectedFailures],
            "unexpected_successes": [t.id() for t in result.unexpectedSuccesses]}


def clean_pass(counts, names):
    return (counts["tests_run"] > 0 and counts["tests_run"] == len(counts["selected"])
            and counts["selected"] == list(names) and not counts["failures"] and not counts["errors"]
            and not counts["skipped"] and not counts["expected_failures"]
            and not counts["unexpected_successes"])


def case_selected(case, stream, record):
    counts = run_named(SELECTORS[case], stream, case)
    record["runs"] = [counts]
    return clean_pass(counts, SELECTORS[case])


def control_detected(run, deciding, marker_suffix):
    tb = run["failures"].get(deciding, "")
    return (run["tests_run"] == 1 and run["selected"] == [deciding] and not run["errors"]
            and list(run["failures"]) == [deciding] and marker_suffix in tb and not run["skipped"])


def case_negative_control(stream, record):
    from unittest import mock
    from campaign_tool.records import ledger_catalog_acceptance as acceptance
    from tests.records import test_fa1404
    original = acceptance.SQL
    marker = test_fa1404.PRESERVATION_MARKER
    controls = (
        ("all-added", test_fa1404.ADDED_TRIGGERS, DECIDING, marker + " recursive_triggers=False"),
        ("reserved-only", (test_fa1404.RESERVED_TRIGGER,), RESERVED_DECIDING,
         marker + " reserved sequence recursive_triggers=False"),
    )
    record["control"] = {"candidate_sql_sha256": sha256_bytes(original.encode()),
                         "source_on_disk_sha256_before": sha256_file(REPO / PRODUCT), "variants": []}
    runs, ok = [], True
    for label, names, deciding, expected in controls:
        variant = test_fa1404.without_added_guards(original, names)
        with mock.patch.object(acceptance, "SQL", variant):
            control = run_named((deciding,), stream, "control " + label + ": deciding test")
            positive = run_named((UNAFFECTED,), stream, "control " + label + ": unaffected positive")
        detected = control_detected(control, deciding, expected)
        record["control"]["variants"].append({
            "label": label, "removed_triggers": list(names), "deciding": deciding,
            "variant_sql_sha256": sha256_bytes(variant.encode()), "detected_expected_failure": detected,
            "restored_module_sql": acceptance.SQL is original})
        runs += [control, positive]
        ok = ok and detected and clean_pass(positive, (UNAFFECTED,)) and acceptance.SQL is original
    restored_names = (DECIDING, RESERVED_DECIDING, UNAFFECTED)
    restored = run_named(restored_names, stream, "restored candidate")
    record["runs"] = runs + [restored]
    record["control"]["source_on_disk_sha256_after"] = sha256_file(REPO / PRODUCT)
    return (ok and clean_pass(restored, restored_names)
            and record["control"]["source_on_disk_sha256_before"] == record["control"]["source_on_disk_sha256_after"])


def global_state():
    import sqlite3
    return {"environ": dict(os.environ), "tempdir": tempfile.tempdir, "sys_path": list(sys.path),
            "sqlite3_connect": id(sqlite3.connect), "cwd": os.getcwd()}


def case_isolation(run_root, guard, stream, record, blob):
    prefix = run_root / ("second-prefix-" + secrets.token_hex(4))
    prefix.mkdir(mode=0o700)
    tempfile.tempdir = str(prefix)
    os.environ["TMPDIR"] = str(prefix)
    before_peer = run_named((PEER,), stream, "peer before")
    state_before = global_state()
    from campaign_tool.records import ledger_catalog_acceptance as acceptance
    module_file = Path(acceptance.__file__).resolve()
    sql_before = sha256_bytes(acceptance.SQL.encode())
    fa_names = (DECIDING, NEW + "test_a1_replace_refused_recursive_triggers_on", RESERVED_DECIDING, UNAFFECTED)
    fa = run_named(fa_names, stream, "candidate from second prefix")
    state_after = global_state()
    after_peer = run_named((PEER,), stream, "peer after")
    used_prefix = [p for p in guard.mkdirs if Path(p).resolve().is_relative_to(prefix.resolve())]
    checks = {
        "module_file_is_candidate": module_file == (REPO / PRODUCT).resolve(),
        "module_bytes_equal_head_blob": sha256_file(module_file) == sha256_bytes(blob),
        "package_root_is_candidate": all(Path(m.__file__).resolve().is_relative_to(REPO)
                                         for n, m in sys.modules.items()
                                         if n.startswith("campaign_tool") and getattr(m, "__file__", None)),
        "module_sql_unchanged": sha256_bytes(acceptance.SQL.encode()) == sql_before,
        "global_state_unchanged": state_before == state_after,
        "second_prefix_used": len(used_prefix) > 0,
        "peer_same_selection": before_peer["selected"] == after_peer["selected"],
    }
    record["isolation"] = dict(checks, second_prefix=str(prefix), prefix_mkdirs=len(used_prefix))
    record["runs"] = [before_peer, fa, after_peer]
    return (all(checks.values()) and clean_pass(before_peer, before_peer["selected"])
            and clean_pass(after_peer, before_peer["selected"])
            and clean_pass(fa, fa_names))


def child(argv, run_root, name, record, stdout_path=None):
    """Run one literal child argv from repo cwd and retain its real streams."""
    if stdout_path is None:
        done = subprocess.run(argv, cwd=REPO, capture_output=True, check=False)
        out = done.stdout
    else:
        with open(stdout_path, "xb") as handle:
            done = subprocess.run(argv, cwd=REPO, stdout=handle, stderr=subprocess.PIPE, check=False)
        out = Path(stdout_path).read_bytes()
    (run_root / (name + ".stdout")).write_bytes(out)
    (run_root / (name + ".stderr")).write_bytes(done.stderr)
    record.setdefault("children", []).append(
        {"name": name, "argv": argv, "cwd": str(REPO), "exit": done.returncode,
         "stdout_sha256": sha256_bytes(out), "stderr_sha256": sha256_bytes(done.stderr)})
    return done.returncode, out


def case_credential_scan(run_root, stream, record):
    runtime = json.loads(MANIFEST.read_text())["runtime"]
    python, scanner = runtime.get("credential_scan_python3"), runtime.get("detect_secrets")
    record["runtime"] = {"manifest": str(MANIFEST), "credential_scan_python3": python, "detect_secrets": scanner}
    if not (python and scanner and Path(python).is_file() and Path(scanner).is_file()):
        stream.write("staging failure: manifest scanner runtime missing; nothing installed or substituted\n")
        record["stop_reason"] = "missing_runtime"
        return False
    report = run_root / "secret-scan.json"
    scan_exit, raw = child([python, scanner, "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"],
                           run_root, "detect-secrets", record, stdout_path=report)
    check_exit, check_out = child([python, str(REPO / "tools/check_secret_report.py"), str(report)],
                                  run_root, "check-secret-report", record)
    text = check_out.decode(errors="replace")
    stream.write(text)
    try:
        results = json.loads(raw)["results"]
        hits = sum(len(v) for v in results.values())
    except (ValueError, KeyError, TypeError, AttributeError):
        hits = None
    record["scan_report"] = {"path": str(report), "sha256": sha256_file(report), "raw_hits": hits}
    zero = "potential secrets: 0;" in text
    record["potential_secrets_zero"] = zero
    return scan_exit == 0 and check_exit == 0 and zero and hits is not None


def case_public_tree(run_root, stream, record):
    python = REPO / ".venv" / "bin" / "python3"
    code, out = child([str(python), str(REPO / "tools/check_public_tree.py")], run_root, "check-public-tree", record)
    text = out.decode(errors="replace")
    stream.write(text)
    stream.write((run_root / "check-public-tree.stderr").read_text(errors="replace"))
    return code == 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--case", required=True, choices=CASES)
    parser.add_argument("--output-root", required=True, type=Path)
    args = parser.parse_args(argv)

    parent = args.output_root.resolve()
    if parent == REPO or parent.is_relative_to(REPO):
        parser.error("--output-root must be outside the repository")
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    prior = tree_hashes(parent)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    run_root = parent / "-".join((args.case, stamp, str(os.getpid()), secrets.token_hex(4)))
    run_root.mkdir(mode=0o700)
    start = identity()
    blob = git("show", "HEAD:" + PRODUCT)
    record = {"label": LABEL, "case": args.case, "argv": [sys.executable, *sys.argv], "cwd": os.getcwd(),
              "python": sys.version, "executable": sys.executable, "run_root": str(run_root),
              "identity_before": start, "selectors": list(SELECTORS.get(args.case, ()))}
    stream = io.StringIO()
    guard = OfflineGuard(run_root)
    try:
        if args.case == "credential-scan":
            passed = case_credential_scan(run_root, stream, record)
        elif args.case == "public-tree":
            passed = case_public_tree(run_root, stream, record)
        else:
            tmp = run_root / "tmp"
            tmp.mkdir(mode=0o700)
            tempfile.tempdir = str(tmp)
            os.environ["TMPDIR"] = str(tmp)
            sys.dont_write_bytecode = True
            sys.pycache_prefix = str(run_root / "pycache")
            if str(REPO) not in sys.path:
                sys.path.insert(0, str(REPO))
            sys.addaudithook(guard)
            guard.active = True
            try:
                if args.case == "negative-control":
                    passed = case_negative_control(stream, record)
                elif args.case == "isolation":
                    passed = case_isolation(run_root, guard, stream, record, blob)
                else:
                    passed = case_selected(args.case, stream, record)
            finally:
                guard.active = False
    except Exception as error:  # recorded and failed closed, never an expected red
        stream.write("unexpected runner fault: " + type(error).__name__ + ": " + str(error)[:300] + "\n")
        record["runner_fault"] = type(error).__name__
        passed = False
    record["guard_refusals"] = guard.refusals
    end = identity()
    record["identity_after"] = end
    after = tree_hashes(parent)
    record["prior_evidence_files"] = len(prior)
    record["prior_evidence_unchanged"] = all(after.get(k) == v for k, v in prior.items())
    for run in record.get("runs", ()):
        run["failures"] = {k: v[-1500:] for k, v in run["failures"].items()}
        run["errors"] = {k: v[-1500:] for k, v in run["errors"].items()}
    passed = bool(passed and not guard.refusals and record["prior_evidence_unchanged"]
                  and start["authored_sha256"] == end["authored_sha256"]
                  and start["referenced_sha256"] == end["referenced_sha256"] and start["head"] == end["head"])
    raw = stream.getvalue().encode()
    (run_root / "output.txt").write_bytes(raw)
    record["raw_output"] = str(run_root / "output.txt")
    record["raw_output_sha256"] = sha256_bytes(raw)
    record["exit"] = 0 if passed else 1
    result_path = run_root / "result.json"
    result_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    if not (result_path.is_file() and (run_root / "output.txt").is_file()):
        record["exit"] = 1
    sys.stdout.write(stream.getvalue())
    sys.stdout.write(json.dumps({"case": args.case, "exit": record["exit"], "run_root": str(run_root),
                                 "tests_run": [r["tests_run"] for r in record.get("runs", ())],
                                 "head": end["head"], "clean_tree": end["clean_tree"],
                                 "result_sha256": sha256_file(result_path), "label": LABEL}) + "\n")
    return record["exit"]


if __name__ == "__main__":
    raise SystemExit(main())
