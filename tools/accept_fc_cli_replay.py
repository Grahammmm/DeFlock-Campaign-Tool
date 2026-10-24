#!/usr/bin/env python3
"""FC-CLI-REPLAY acceptance runner (offline, synthetic, REVIEW REQUIRED).

Usage: accept_fc_cli_replay.py --check CHECK --output-root DIR

Each invocation creates a fresh, uniquely named run directory under
--output-root and never overwrites earlier evidence. Children are argument
arrays (no shell). Import search paths are set only inside this script, to its
own repository or to an isolated copy of it. Machine validity is REVIEW
REQUIRED: it is not semantic, legal or release acceptance.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys

HERE = Path(__file__).resolve()
REPO = HERE.parents[1]
CHECKS = ("focused", "compatibility", "negative", "portability", "credentials", "public-tree")
FOCUSED = "tests.records.test_fc_cli_replay"
COMPAT = "tests.records.test_understanding.CliTests"
NEG_CONTRACT = FOCUSED + ".ReplayTests.test_leaf_symlink_byte_equal_result_refused"
NEG_POSITIVE = FOCUSED + ".ReplayTests.test_legitimate_private_replay_reused"
OWNED = (
    "campaign_tool/records/understanding/cli.py",
    "tests/records/test_fc_cli_replay.py",
    "tools/accept_fc_cli_replay.py",
)
INSPECTED = ("tests/records/test_fc_cli_replay.py", "tools/accept_fc_cli_replay.py")
# Box-specific or inline-interpreter forms, assembled so this file does not match itself.
FORBIDDEN_FORMS = (
    "/" + "home/", "/" + "Users/", "agent" + "-work", "venv" + "-fullsuite",
    '"-' + 'c"', "'-" + "c'", '"-' + 'e"', "'-" + "e'", "PYTHON" + "PATH",
    "shell" + "=True", "os." + "system(", "<" + "<",
)
CHILD_TIMEOUT = 900
REVIEW = "REVIEW REQUIRED (machine validity only; not semantic, legal or release acceptance)"


def sha_bytes(raw):
    return hashlib.sha256(raw).hexdigest()


def sha_file(path):
    return sha_bytes(Path(path).read_bytes())


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


# ---------------------------------------------------------------- child mode
def child_main(args):
    """Run unittest in-process against --source-root with redirected defaults."""
    sys.dont_write_bytecode = True
    source = Path(args.source_root).resolve()
    tmp_root = Path(args.tmp_root)
    tmp_root.mkdir(mode=0o700, parents=True, exist_ok=False)
    home = tmp_root / "home"
    home.mkdir(mode=0o700)
    os.environ["HOME"] = str(home)
    import tempfile
    tempfile.tempdir = str(tmp_root)
    sys.path[:] = [str(source)] + [p for p in sys.path if p not in ("", str(source), str(HERE.parent))]
    events = []
    if args.sentinels:
        install_sentinels(source, events)
    import unittest

    class Recording(unittest.TextTestResult):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.outcomes = []

        def addSuccess(self, test):
            super().addSuccess(test)
            self.outcomes.append([test.id(), "success"])

        def addFailure(self, test, err):
            super().addFailure(test, err)
            self.outcomes.append([test.id(), "failure"])

        def addError(self, test, err):
            super().addError(test, err)
            self.outcomes.append([test.id(), "error"])

        def addSkip(self, test, reason):
            super().addSkip(test, reason)
            self.outcomes.append([test.id(), "skip"])

        def addSubTest(self, test, subtest, err):
            super().addSubTest(test, subtest, err)
            if err is not None:
                kind = "subtest-failure" if issubclass(err[0], test.failureException) else "subtest-error"
                self.outcomes.append([subtest.id(), kind])

    def run(names):
        suite = unittest.TestLoader().loadTestsFromNames(names)
        runner = unittest.TextTestRunner(stream=sys.stderr, verbosity=2, resultclass=Recording)
        r = runner.run(suite)
        return {
            "names": names, "tests_run": r.testsRun, "was_successful": r.wasSuccessful(),
            "outcomes": r.outcomes,
            "failures": [[t.id(), tb] for t, tb in r.failures],
            "errors": [[t.id(), tb] for t, tb in r.errors],
            "skipped": [[t.id(), reason] for t, reason in r.skipped],
            "expected_failures": [t.id() for t, _ in r.expectedFailures],
            "unexpected_successes": [t.id() for t in r.unexpectedSuccesses],
        }

    def clean(res):
        return (res["tests_run"] > 0 and res["was_successful"] and not res["skipped"]
                and not res["unexpected_successes"] and not res["expected_failures"])

    out = {"mode": args.child, "source_root": str(source), "tmp_root": str(tmp_root),
           "interpreter": sys.executable, "sentinels": bool(args.sentinels), "sentinel_events": events}
    if args.child == "unittest":
        res = run(args.tests)
        out["result"] = res
        ok = clean(res)
    else:
        ok, detail = negative_control(run, clean)
        out.update(detail)
    out["loaded_modules"] = loaded_modules()
    out["child_verdict"] = "PASS" if ok and not blocked(events) else "FAIL"
    Path(args.result_json).write_text(json.dumps(out, indent=2, sort_keys=True) + "\n")
    return 0 if out["child_verdict"] == "PASS" else 1


def negative_control(run, clean):
    """Swap only the checked replay reader for the old raw read, then restore."""
    from unittest import mock
    from campaign_tool.records.understanding import cli
    original = getattr(cli, "_read_replay_artifact", None)
    detail = {"reader_present": original is not None}
    if original is None:
        return False, detail

    def raw_read(dest, name, data):
        return (Path(dest) / name).read_bytes()

    with mock.patch.object(cli, "_read_replay_artifact", raw_read):
        mutant = run([NEG_CONTRACT, NEG_POSITIVE])
    restored_identity = cli._read_replay_artifact is original
    restored = run([NEG_CONTRACT, NEG_POSITIVE])
    failed_ids = [i for i, _ in mutant["failures"]]
    mutant_detected = (mutant["tests_run"] == 2 and failed_ids == [NEG_CONTRACT]
                       and not mutant["errors"] and not mutant["skipped"]
                       and [NEG_POSITIVE, "success"] in mutant["outcomes"])
    detail.update({"mutant": mutant, "mutant_detected": mutant_detected,
                   "restored_identity": restored_identity, "restored": restored,
                   "restored_clean": clean(restored)})
    return mutant_detected and restored_identity and clean(restored), detail


def install_sentinels(source, events):
    import socket

    def refuse_network(*a, **k):
        events.append(["network", "blocked"])
        raise OSError("network_sentinel")
    socket.socket.connect = refuse_network
    socket.socket.connect_ex = refuse_network
    socket.create_connection = refuse_network
    socket.getaddrinfo = refuse_network
    real_init = subprocess.Popen.__init__

    def guarded(self, argv, *a, **k):
        cwd = Path(k.get("cwd") or os.getcwd()).resolve()
        allowed = (isinstance(argv, (list, tuple)) and len(argv) >= 4
                   and list(argv[:4]) == [sys.executable, "-B", "-m", "campaign_tool.records.understanding"]
                   and not k.get("shell") and (cwd == source or source in cwd.parents))
        events.append(["process", "allowed" if allowed else "blocked",
                       argv[3] if allowed else "refused", str(cwd)])
        if not allowed:
            raise OSError("process_sentinel")
        return real_init(self, argv, *a, **k)
    subprocess.Popen.__init__ = guarded

    def refuse_system(*a, **k):
        events.append(["process", "blocked", "system"])
        raise OSError("process_sentinel")
    os.system = refuse_system


def blocked(events):
    return any(e[1] == "blocked" for e in events)


def loaded_modules():
    out = {}
    for name, mod in sorted(sys.modules.items()):
        if name.split(".")[0] in ("campaign_tool", "tests"):
            out[name] = getattr(mod, "__file__", None)
    return out


# --------------------------------------------------------------- parent mode
class Recorder:
    def __init__(self, run_dir):
        self.run_dir = run_dir
        self.steps = []

    def step(self, name, argv, cwd, stdout_path=None):
        out_path = Path(stdout_path) if stdout_path else self.run_dir / (name + ".stdout")
        err_path = self.run_dir / (name + ".stderr")
        record = {"name": name, "argv": [str(a) for a in argv], "cwd": str(cwd)}
        with open(out_path, "xb") as out, open(err_path, "xb") as err:
            try:
                done = subprocess.run(record["argv"], cwd=cwd, stdout=out, stderr=err,
                                      stdin=subprocess.DEVNULL, timeout=CHILD_TIMEOUT)
                record["exit"] = done.returncode
            except subprocess.TimeoutExpired:
                record["exit"] = None
                record["timeout"] = True
            except OSError as exc:
                record["exit"] = None
                record["launch_error"] = type(exc).__name__
        record["stdout_file"] = out_path.name
        record["stderr_file"] = err_path.name
        record["stdout_sha256"] = sha_file(out_path)
        record["stderr_sha256"] = sha_file(err_path)
        self.steps.append(record)
        return record


def git(*args):
    done = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True, timeout=60)
    return done.stdout.strip() if done.returncode == 0 else None


def child_argv(script, source, tmp_root, result_json, mode, tests=(), sentinels=False):
    argv = [sys.executable, "-B", str(script), "--child", mode, "--source-root", str(source),
            "--tmp-root", str(tmp_root), "--result-json", str(result_json)]
    if sentinels:
        argv.append("--sentinels")
    if tests:
        argv += ["--tests", *tests]
    return argv


def read_child(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def summarize_unittest(res):
    if not res:
        return {"tests_run": 0}
    return {"tests_run": res["tests_run"], "failures": [i for i, _ in res["failures"]],
            "errors": [i for i, _ in res["errors"]], "skipped": [i for i, _ in res["skipped"]],
            "was_successful": res["was_successful"]}


def check_unittest(rec, run_dir, tests):
    result_json = run_dir / "child-result.json"
    step = rec.step("unittest", child_argv(HERE, REPO, run_dir / "tmp", result_json, "unittest", tests), REPO)
    child = read_child(result_json) or {}
    res = child.get("result") or {}
    ok = (step["exit"] == 0 and child.get("child_verdict") == "PASS" and res.get("tests_run", 0) > 0
          and res.get("skipped") == [] and res.get("failures") == [] and res.get("errors") == [])
    res = res or None
    return ok, {"unittest": summarize_unittest(res), "child_result_json": result_json.name}


def check_negative(rec, run_dir):
    result_json = run_dir / "child-result.json"
    step = rec.step("negative", child_argv(HERE, REPO, run_dir / "tmp", result_json, "negative"), REPO)
    child = read_child(result_json) or {}
    ok = (step["exit"] == 0 and child.get("child_verdict") == "PASS" and child.get("mutant_detected") is True
          and child.get("restored_clean") is True and child.get("restored_identity") is True)
    return ok, {"mutant": summarize_unittest(child.get("mutant")),
                "restored": summarize_unittest(child.get("restored")),
                "mutant_detected": child.get("mutant_detected"), "child_result_json": result_json.name}


def copy_tracked(dest):
    listing = subprocess.run(["git", "-C", str(REPO), "ls-files", "-z"], capture_output=True, timeout=60)
    if listing.returncode != 0:
        raise OSError("git_ls_files_failed")
    names = [n for n in listing.stdout.decode().split("\0") if n]
    for name in names:
        src, dst = REPO / name, dest / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_symlink():
            os.symlink(os.readlink(src), dst)
        elif src.is_file():
            shutil.copy2(src, dst)
    for name in OWNED:
        if not (dest / name).exists() and (REPO / name).is_file():
            (dest / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO / name, dest / name)
    return len(names)


def inspect_sources(root):
    hits = []
    for name in INSPECTED:
        text = (root / name).read_text()
        for n, line in enumerate(text.splitlines(), 1):
            for form in FORBIDDEN_FORMS:
                if form in line:
                    hits.append([name, n, form])
    return hits


def check_portability(rec, run_dir):
    copy = run_dir / ("portable-" + secrets.token_hex(4)) / "relocated" / "candidate"
    copy.mkdir(parents=True, mode=0o700)
    copied = copy_tracked(copy)
    hashes = {n: [sha_file(REPO / n), sha_file(copy / n)] for n in OWNED if (REPO / n).is_file()}
    same = all(a == b for a, b in hashes.values()) and len(hashes) == len(OWNED)
    hits = inspect_sources(copy)
    result_json = run_dir / "child-result.json"
    step = rec.step("portable-unittest",
                    child_argv(copy / "tools" / HERE.name, copy, run_dir / "tmp-portable", result_json,
                               "unittest", [FOCUSED], sentinels=True), copy)
    child = read_child(result_json) or {}
    res = child.get("result")
    mods = child.get("loaded_modules", {})
    files = [f for f in mods.values() if f]
    inside_copy = bool(files) and all(Path(f).resolve().is_relative_to(copy.resolve()) for f in files)
    touches_repo = any(Path(f).resolve().is_relative_to(REPO) for f in files)
    ok = (step["exit"] == 0 and child.get("child_verdict") == "PASS" and res and res["tests_run"] > 0
          and not res["skipped"] and same and not hits and inside_copy and not touches_repo
          and FOCUSED in mods and "campaign_tool.records.understanding.cli" in mods
          and not blocked(child.get("sentinel_events", [])))
    return ok, {"copy_root": str(copy), "tracked_files_copied": copied, "owned_hashes_match": same,
                "owned_hashes": hashes, "inspection_hits": hits, "modules_inside_copy": inside_copy,
                "modules_touch_staged_repo": touches_repo, "unittest": summarize_unittest(res),
                "sentinel_events": child.get("sentinel_events"), "child_result_json": result_json.name}


def check_credentials(rec, run_dir):
    scanner = Path(sys.executable).parent / "detect-secrets"
    if not scanner.is_file():
        return False, {"capability": "detect-secrets not installed adjacent to interpreter"}
    rec.step("detect-secrets-version", [scanner, "--version"], REPO)
    report = run_dir / "secret-scan.json"
    scan = rec.step("detect-secrets-scan",
                    [scanner, "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"],
                    REPO, stdout_path=report)
    detail = {"report": report.name, "report_sha256": sha_file(report)}
    try:
        data = json.loads(report.read_text())
        well_formed = isinstance(data.get("results"), dict)
        detail["raw_hit_files"] = sorted(data["results"]) if well_formed else None
    except (OSError, ValueError, AttributeError):
        well_formed = False
    detail["report_well_formed"] = well_formed
    checker = rec.step("check-secret-report", [sys.executable, REPO / "tools" / "check_secret_report.py", report], REPO)
    text = (run_dir / checker["stdout_file"]).read_text()
    m = re.search(r"potential secrets: (\d+)", text)
    detail["potential_secrets"] = int(m[1]) if m else None
    ok = scan["exit"] == 0 and checker["exit"] == 0 and well_formed and detail["potential_secrets"] == 0
    return ok, detail


def check_public_tree(rec, run_dir):
    step = rec.step("check-public-tree", [sys.executable, "-B", REPO / "tools" / "check_public_tree.py"], REPO)
    return step["exit"] == 0, {"exit": step["exit"]}


def parent_main(args):
    out_root = Path(os.path.abspath(args.output_root))
    if out_root == REPO or REPO in out_root.parents:
        print("output root must be outside the repository", file=sys.stderr)
        return 2
    out_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    run_dir = out_root / ("run-" + now() + "-" + str(os.getpid()) + "-" + secrets.token_hex(4))
    os.mkdir(run_dir, 0o700)
    rec = Recorder(run_dir)
    meta = {
        "schema": "fc-cli-replay-acceptance-v1", "check": args.check, "argv": sys.argv,
        "interpreter": sys.executable, "cwd": os.getcwd(), "repo": str(REPO),
        "head": git("rev-parse", "HEAD"), "status_porcelain": git("status", "--porcelain"),
        "owned_sha256": {n: sha_file(REPO / n) for n in OWNED if (REPO / n).is_file()},
        "started_at": now(), "review_status": REVIEW,
    }
    handlers = {
        "focused": lambda: check_unittest(rec, run_dir, [FOCUSED]),
        "compatibility": lambda: check_unittest(rec, run_dir, [COMPAT]),
        "negative": lambda: check_negative(rec, run_dir),
        "portability": lambda: check_portability(rec, run_dir),
        "credentials": lambda: check_credentials(rec, run_dir),
        "public-tree": lambda: check_public_tree(rec, run_dir),
    }
    try:
        ok, detail = handlers[args.check]()
    except Exception as exc:  # incomplete check is a failure, never a pass
        ok, detail = False, {"incomplete": type(exc).__name__}
    meta.update({"detail": detail, "steps": rec.steps, "finished_at": now(),
                 "verdict": "PASS" if ok and rec.steps else "FAIL"})
    (run_dir / "verdict.json").write_text(json.dumps(meta, indent=2, sort_keys=True, default=str) + "\n")
    hashes = {p.name: sha_file(p) for p in sorted(run_dir.iterdir()) if p.is_file()}
    (run_dir / "hashes.json").write_text(json.dumps(hashes, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"check": args.check, "verdict": meta["verdict"], "run_dir": str(run_dir),
                      "head": meta["head"], "detail": detail, "review_status": REVIEW},
                     indent=2, sort_keys=True, default=str))
    return 0 if meta["verdict"] == "PASS" else 1


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--check", choices=CHECKS)
    p.add_argument("--output-root")
    p.add_argument("--child", choices=("unittest", "negative"), help=argparse.SUPPRESS)
    p.add_argument("--source-root", help=argparse.SUPPRESS)
    p.add_argument("--tmp-root", help=argparse.SUPPRESS)
    p.add_argument("--result-json", help=argparse.SUPPRESS)
    p.add_argument("--sentinels", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--tests", nargs="+", default=[], help=argparse.SUPPRESS)
    args = p.parse_args(argv)
    if args.child:
        if not (args.source_root and args.tmp_root and args.result_json) or (args.child == "unittest" and not args.tests):
            p.error("child mode needs --source-root, --tmp-root, --result-json (and --tests)")
        return child_main(args)
    if not (args.check and args.output_root):
        p.error("--check and --output-root are required")
    return parent_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
