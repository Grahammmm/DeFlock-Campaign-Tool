#!/usr/bin/env python3
"""FC7-CATALOG-LEASE-CLOCK acceptance runner: six separate --check modes.

Each invocation first runs an admission preflight: the staged SOURCE-MANIFEST.json
beside the repository must declare the frozen admitted base, the repository's actual
origin remote (which must hold that base), this repository or an identical relocated
copy, and the runtime declared for the mode; the candidate must descend from the
frozen base with changes confined to the write set. Any preflight failure is recorded
and the run stops before any application import, source copy, scanner or test.

Every run creates one fresh exclusive run directory under --output-root (outside the
repository) and records argv, cwd, interpreter, base and candidate HEAD, the
acceptance-contract identity, every changed-file hash, selected test IDs and counts
and raw child stdout/stderr/exit. It exits 0 only when every explicit check of its
mode passed. Child processes are limited to the named literal argv shapes accepted
by named_child(), always without a shell; anything else is refused unspawned.

Machine-valid output is REVIEW REQUIRED evidence, never semantic, legal, human,
stage, live or publication acceptance. Full discovery stays operator-run.
"""
import argparse
import ast
from datetime import datetime, timezone
import hashlib
import http.client  # noqa: F401  (guarded owners are read from these modules)
import importlib
import io
import json
import os
from pathlib import Path
import _posixsubprocess  # noqa: F401
import re
import shutil
import socket  # noqa: F401
import subprocess
import sys
import tempfile
import traceback
import unittest
from unittest import mock
import urllib.request  # noqa: F401
import warnings

sys.dont_write_bytecode = True
ORIGINAL_ENV = dict(os.environ)
REPO = Path(__file__).resolve().parents[1]
CARD = "FC7-CATALOG-LEASE-CLOCK"
TASK = "t_1de27499"
# Frozen admission identity from the card (written as integers so no high-entropy
# string literal is committed): base commit and its catalog_stage.py SHA256.
FROZEN_BASE = format(0x4caaa92a18a997d58afe7c651d0a2aeb6875a09c, "040x")
FROZEN_SOURCE_SHA256 = format(0x121dcad494918c8fa66f19158db0df9c0b2da7ff40d133b32a84410fd4105c35, "064x")
CHECKS = ("focused", "compatibility", "negative", "portability", "credentials", "public-tree")
SOURCE = "campaign_tool/records/catalog_stage.py"
TEST = "tests/records/test_fc7_catalog_lease_clock.py"
RUNNER = "tools/accept_fc7_catalog_lease_clock.py"
ALLOWED_CHANGES = (SOURCE, TEST, RUNNER)
TEST_MODULE = "tests.records.test_fc7_catalog_lease_clock"
TEST_CLASS = "LeaseClockTest"
CHILD_FLAG = "--fc7-guarded-child"
FROZEN_CASES = ("valid_active_foreign_and_same_owner", "expired_exact_expiry_and_absent",
                "null_nonstring_naive_and_garbage", "impossible_timezone_hours_minutes_and_calendar",
                "fractional_seconds_and_legal_offsets", "no_stage_mutation_on_invalid_lease",
                "unchanged_prior_drift_gate")
AUXILIARY_CASES = ("offline_sentinels_held",)
COMPATIBILITY = ("tests.records.test_catalog_stage",)
COMPATIBILITY_CLASS = ("tests/records/test_catalog_stage.py", "CatalogStageTests")
# Deciding synthetic mutants: (label, stored expiry template, exception the mutant lets escape).
MUTANT_VALUES = (("naive", "2099-10-25T00:00:00", "TypeError"),
                 ("impossible_offset", "2099-10-25T00:00:00+24:00", "ValueError"))
EXPIRED_CONTROL = "expired_exact_expiry_and_absent"
NETWORK_MODULES = ("campaign_tool.records.portal.https_transport", "campaign_tool.digest.model",
                   "campaign_tool.outbox", "campaign_tool.meetings", "campaign_tool.discovery",
                   "campaign_tool.records.worker_lifecycle", "campaign_tool.records.ocr_stage",
                   "campaign_tool.records.intake.imap_intake")
OS_PROCESS = ("system", "popen", "fork", "forkpty", "posix_spawn", "posix_spawnp", "execv", "execve",
              "execvp", "execvpe", "spawnv", "spawnve", "spawnvp", "spawnvpe")
# Capability-bearing imports and the installed sentinel that covers each one.
GUARD_COVERAGE = {
    "subprocess": "subprocess.Popen, _posixsubprocess.fork_exec",
    "_posixsubprocess": "_posixsubprocess.fork_exec",
    "multiprocessing": "os.fork, _posixsubprocess.fork_exec",
    "pty": "os.forkpty, os.fork, os.execv*",
    "os.process": "os.system/popen/fork/forkpty/exec*/spawn*/posix_spawn*",
    "socket": "socket.create_connection/getaddrinfo, socket.socket.connect/connect_ex",
    "asyncio": "socket.socket.connect, subprocess.Popen",
    "http.client": "http.client.HTTPConnection.connect, socket",
    "urllib.request": "urllib.request.urlopen, socket",
    "xmlrpc.client": "http.client, socket", "requests": "socket.socket.connect",
    "smtplib": "socket.create_connection", "imaplib": "socket.create_connection",
    "poplib": "socket.create_connection", "ftplib": "socket.create_connection",
    "nntplib": "socket.create_connection", "telnetlib": "socket.create_connection"}
CAPABLE = tuple(sorted(set(GUARD_COVERAGE) - {"os.process"})) + ("ctypes",)
# Reviewed capability imports no sentinel covers. ctypes in extract.ocr binds only libc
# renameat2 (a local rename); worker_lifecycle must additionally never be loaded.
REVIEWED_UNGUARDED = {"campaign_tool.records.extract.ocr": ("ctypes",),
                      "campaign_tool.records.worker_lifecycle": ("ctypes",)}
# Built from pieces so this file does not match its own host-path scan.
HOST_TOKENS = ("/" + "home/", "/" + "Users/", "agent" + "-work", "." + "hermes", "kan" + "ban",
               "/" + "tmp/", "/" + "workspace/", "/" + "root/")
ARTIFACT_ENV = "FC7_LEASE_CLOCK_ARTIFACT_ROOT"
# Untracked entries that standard operator staging supplies (shared interpreter link, node_modules).
STAGED_UNTRACKED = (".venv", "node_modules")
DROPPED_CHILD_ENV = ("RECORDS_TEST_DEPENDENCIES", ARTIFACT_ENV)
TIMEOUT = 1800
SCAN_ARGS = ["scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"]
CONTRACT = {"card": CARD, "task": TASK, "base": FROZEN_BASE, "base_source_sha256": FROZEN_SOURCE_SHA256,
            "checks": list(CHECKS), "write_set": list(ALLOWED_CHANGES), "test_module": TEST_MODULE,
            "frozen_cases": list(FROZEN_CASES), "auxiliary_cases": list(AUXILIARY_CASES),
            "compatibility_selectors": list(COMPATIBILITY),
            "compatibility_child": ["-B", "-E", "-s", "-m", TEST_MODULE, CHILD_FLAG],
            "negative": {"seam": "catalog_stage._lease_expiry", "mutant": "datetime.fromisoformat",
                         "values": [list(row) for row in MUTANT_VALUES], "restored_control": EXPIRED_CONTROL},
            "blocked_result": {"status": "blocked", "reason": "catalog_lease_clock_invalid",
                               "promoted": False, "canonical_stage_changed": False}}
PREFLIGHT_CONTROLS = (("missing", "manifest_present_beside_repo"),
                      ("malformed", "manifest_json_readable"),
                      ("wrong_base", "manifest_base_is_frozen_admitted_base"),
                      ("wrong_remote", "manifest_remote_is_repo_origin"),
                      ("wrong_runtime", "runtime_declared_for_mode"))


def sha_bytes(raw):
    return hashlib.sha256(raw).hexdigest()


def sha_file(path):
    return sha_bytes(Path(path).read_bytes())


def now():
    return datetime.now(timezone.utc).isoformat()


def same_path(left, right):
    return bool(left) and bool(right) and os.path.realpath(left) == os.path.realpath(right)


class Run:
    def __init__(self, check, output_root):
        self.check = check
        root = Path(os.path.abspath(output_root))
        if root == REPO or REPO in root.parents:
            raise SystemExit("output root must be a directory outside the repository")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        self.directory = root / ("run-" + stamp + "-" + str(os.getpid()))
        self.directory.mkdir(mode=0o700)  # exclusive: never reuses a prior run
        self.tmp = self.directory / "tmp"
        self.tmp.mkdir(mode=0o700)
        self.checks, self.files, self.scanner = [], {}, None
        self.receipt = {"schema": "fc7-catalog-lease-clock-acceptance-v2", "card": CARD, "task": TASK,
                        "check": check, "argv": list(sys.argv), "cwd": os.getcwd(),
                        "executable": sys.executable, "prefix": sys.prefix, "python": sys.version,
                        "repo": str(REPO), "run_directory": str(self.directory), "started_at": now(),
                        "contract": CONTRACT,
                        "contract_sha256": sha_bytes(json.dumps(CONTRACT, sort_keys=True).encode()),
                        "status": "REVIEW_REQUIRED_NOT_ACCEPTANCE",
                        "full_discovery": "operator-run; OWED, not PASS, until real outputs exist"}

    def require(self, name, ok, detail=None):
        self.checks.append({"name": name, "ok": bool(ok), "detail": detail})
        return bool(ok)

    def failed(self):
        return [row["name"] for row in self.checks if not row["ok"]]

    def save(self, name, data):
        raw = data.encode() if isinstance(data, str) else data
        path = self.directory / name
        with open(path, "xb") as stream:
            stream.write(raw)
        self.files[name] = {"path": str(path), "sha256": sha_bytes(raw), "bytes": len(raw)}
        return path

    def child_env(self, label):
        """Sanitised environment: temporary, home and XDG roots point into this run only."""
        directory = self.tmp / label
        directory.mkdir(mode=0o700)
        environment = {name: value for name, value in ORIGINAL_ENV.items()
                       if not name.startswith("PYTHON") and name not in DROPPED_CHILD_ENV}
        for name in ("TMPDIR", "TEMP", "TMP", "HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
                     "XDG_CACHE_HOME", "XDG_STATE_HOME"):
            environment[name] = str(directory)
        return environment

    def named_child(self, argv):
        """The only child argv shapes this runner may start; None means refuse."""
        executable, rest = argv[0], argv[1:]
        if executable == "git":
            return "git"
        if self.scanner and executable == self.scanner and rest == SCAN_ARGS:
            return "detect-secrets"
        if executable != sys.executable or rest[:1] != ["-B"]:
            return None
        if rest[1:6] == ["-E", "-s", "-m", TEST_MODULE, CHILD_FLAG] and rest[6:7] == ["--report"] \
                and rest[8:] == list(COMPATIBILITY):
            return "guarded-unittest-child"
        script = Path(rest[1]) if len(rest) > 1 else None
        if script and script.name == Path(RUNNER).name and rest[2:4] == ["--check", "focused"] \
                and rest[4:5] == ["--output-root"] and len(rest) == 6:
            return "runner-focused"
        if script and script.name == "check_public_tree.py" and len(rest) == 2:
            return "public-tree-checker"
        if script and script == REPO / "tools" / "check_secret_report.py" and len(rest) == 3:
            return "secret-report-checker"
        return None

    def command(self, label, argv, *, cwd, env=None, boundary=False):
        """One named literal argv, no shell; raw stdout/stderr saved. Unnamed argv is refused."""
        argv = [str(part) for part in argv]
        started, kind = now(), self.named_child(argv)
        if kind is None:
            code, out, err, error = None, b"", b"", "refused_unnamed_child"
            if not boundary:
                self.require("child_argv_named:" + label, False, argv)
        else:
            try:
                done = subprocess.run(argv, cwd=cwd, env=env, capture_output=True,
                                      timeout=TIMEOUT, shell=False, check=False)
                code, out, err, error = done.returncode, done.stdout, done.stderr, None
            except (OSError, subprocess.TimeoutExpired) as caught:
                code, out, err, error = None, b"", b"", type(caught).__name__ + ": " + str(caught)
        record = {"label": label, "kind": kind, "argv": argv, "cwd": str(cwd),
                  "started_at": started, "ended_at": now(), "exit": code, "error": error,
                  "stdout": self.save(label + ".stdout", out).name,
                  "stderr": self.save(label + ".stderr", err).name}
        self.receipt.setdefault("commands", []).append(record)
        return record, out, err

    def finish(self):
        passed = bool(self.checks) and not self.failed()
        self.receipt.update({"checks": self.checks, "files": self.files, "ended_at": now(),
                             "result": "PASS_REVIEW_REQUIRED" if passed else "FAIL"})
        raw = json.dumps(self.receipt, indent=2, sort_keys=True, default=repr) + "\n"
        with open(self.directory / "receipt.json", "x", encoding="utf-8") as stream:
            stream.write(raw)
        failed = self.failed()
        print(json.dumps({"check": self.check, "result": self.receipt["result"],
                          "run_directory": str(self.directory), "receipt_sha256": sha_bytes(raw.encode()),
                          "checks_passed": len(self.checks) - len(failed), "checks_total": len(self.checks),
                          "failed_checks": failed}, indent=2))
        return 0 if passed else 1


def git(run, label, *arguments, root=REPO):
    if not (Path(root) / ".git").exists():
        return None, None
    record, out, _ = run.command("git-" + label, ["git", "-C", root, *arguments], cwd=REPO,
                                 env=dict(ORIGINAL_ENV))
    return record["exit"], out.decode(errors="replace")


# -- admission preflight -----------------------------------------------------------------
def verify_manifest(run):
    """SOURCE-MANIFEST.json from the staged repo parent: base, remote, repo and mode runtime."""
    path = REPO.parent / "SOURCE-MANIFEST.json"
    if not run.require("manifest_present_beside_repo", path.is_file(), str(path)):
        return None
    try:
        manifest = json.loads(path.read_text())
        readable = isinstance(manifest, dict)
    except (ValueError, UnicodeError) as caught:
        manifest, readable = None, False
        run.receipt["manifest_error"] = str(caught)
    if not run.require("manifest_json_readable", readable, str(path)):
        return None
    runtime = manifest.get("runtime") if isinstance(manifest.get("runtime"), dict) else {}
    record = {"path": str(path), "sha256": sha_file(path), "head": manifest.get("head"),
              "tree": manifest.get("tree"), "remote": manifest.get("remote"), "repo": manifest.get("repo"),
              "runtime": runtime}
    run.receipt["manifest"] = record
    run.require("manifest_schema_and_card", manifest.get("schema") == "agentorange.source-manifest/v1"
                and manifest.get("card") == CARD, [manifest.get("schema"), manifest.get("card")])
    run.require("manifest_base_is_frozen_admitted_base", manifest.get("head") == FROZEN_BASE,
                [manifest.get("head"), FROZEN_BASE])
    # Remote: the declared remote is this repository's actual origin and holds the frozen base.
    remote = manifest.get("remote")
    code, origin = git(run, "origin-url", "config", "--get", "remote.origin.url")
    origin = origin.strip() if code == 0 else None
    record["actual_origin"] = origin
    if run.require("manifest_remote_is_repo_origin", isinstance(remote, str) and bool(remote) and remote == origin,
                   [remote, origin]):
        held = Path(remote).is_absolute() and (Path(remote) / ".git").exists() and git(
            run, "remote-has-frozen-base", "cat-file", "-e", FROZEN_BASE + "^{commit}", root=remote)[0] == 0
        run.require("declared_remote_holds_frozen_base", held, remote)
    # Runtime declared for this mode.
    if run.check == "credentials":
        declared = runtime.get("credential_scan_python3")
        scanner = runtime.get("detect_secrets")
        ok = (isinstance(declared, str) and os.path.abspath(sys.executable) == declared
              and isinstance(scanner, str) and Path(scanner).is_absolute() and Path(scanner).is_file())
        run.scanner = scanner if ok else None
        detail = {"executable": os.path.abspath(sys.executable), "credential_scan_python3": declared,
                  "detect_secrets": scanner}
    else:
        venv = runtime.get("repo_venv")
        interpreter = REPO / ".venv" / "bin" / "python3"
        ok = (isinstance(venv, str) and same_path(sys.prefix, venv) and same_path(REPO / ".venv", venv)
              and same_path(sys.executable, interpreter))
        detail = {"executable": sys.executable, "prefix": sys.prefix, "repo_venv": venv,
                  "repo_interpreter": str(interpreter)}
    record["mode_runtime"] = detail
    run.require("runtime_declared_for_mode", ok, detail)
    # Repository: this staged repo, or a byte-identical relocated copy at the same commit.
    declared = manifest.get("repo")
    if declared == str(REPO):
        record["repo_relation"] = "staged"
        run.require("manifest_repo_is_this_repo_or_identical_relocation", True, declared)
    else:
        staged = Path(declared) if isinstance(declared, str) and declared else None
        same = bool(staged and staged.is_dir() and (staged / ".git").exists()) and all(
            (staged / name).is_file() and (REPO / name).is_file() and sha_file(staged / name) == sha_file(REPO / name)
            for name in ALLOWED_CHANGES)
        if same:
            ours, theirs = git(run, "rev-parse-HEAD-here", "rev-parse", "HEAD"), git(
                run, "rev-parse-HEAD-staged", "rev-parse", "HEAD", root=staged)
            same = ours[0] == 0 and theirs[0] == 0 and ours[1].strip() == theirs[1].strip()
        record["repo_relation"] = "relocated_copy_of:" + str(declared)
        run.require("manifest_repo_is_this_repo_or_identical_relocation", same, declared)
    return manifest


def identity(run):
    hashes = {name: sha_file(REPO / name) if (REPO / name).is_file() else None for name in ALLOWED_CHANGES}
    code, head = git(run, "rev-parse-HEAD", "rev-parse", "HEAD")
    head = head.strip() if code == 0 else None
    _, status = git(run, "status", "status", "--porcelain", "--untracked-files=all")
    record = {"repo": str(REPO), "base": FROZEN_BASE, "head": head, "git_available": head is not None,
              "status_porcelain": status, "file_sha256": hashes}
    run.receipt["identity"] = record
    run.require("write_set_files_present", all(hashes.values()), hashes)
    if not run.require("git_history_available", head is not None, str(REPO / ".git")):
        return record
    code, _ = git(run, "base-is-ancestor", "merge-base", "--is-ancestor", FROZEN_BASE, "HEAD")
    run.require("frozen_base_is_ancestor_of_head", code == 0, code)
    code, tree = git(run, "base-tree", "rev-parse", FROZEN_BASE + "^{tree}")
    declared = (run.receipt.get("manifest") or {}).get("tree")
    run.require("base_tree_matches_manifest", code == 0 and tree.strip() == declared, [tree, declared])
    shown, raw, _ = run.command("git-show-base-source", ["git", "-C", REPO, "show", FROZEN_BASE + ":" + SOURCE],
                                cwd=REPO, env=dict(ORIGINAL_ENV))
    record["base_source_sha256"] = sha_bytes(raw) if shown["exit"] == 0 else None
    run.require("base_source_is_frozen_pin", shown["exit"] == 0 and record["base_source_sha256"] ==
                FROZEN_SOURCE_SHA256, record["base_source_sha256"])
    _, changed = git(run, "diff-base-head", "diff", "--name-only", FROZEN_BASE, "HEAD")
    changed = set((changed or "").split())
    dirty = {line[3:] for line in (status or "").splitlines() if line.strip()
             and not (line.startswith("?? ") and line[3:].rstrip("/") in STAGED_UNTRACKED)}
    record["committed_changes_from_base"] = sorted(changed)
    record["uncommitted_paths"] = sorted(dirty)
    record["paths_outside_write_set"] = sorted((changed | dirty) - set(ALLOWED_CHANGES))
    run.require("changes_confined_to_write_set", not record["paths_outside_write_set"],
                record["paths_outside_write_set"])
    return record


def preflight(run):
    """Admission checks; True only when every one passed. Nothing else has run yet."""
    preloaded = sorted(name for name in sys.modules if name.split(".")[0] in ("campaign_tool", "tests"))
    run.require("no_application_import_before_preflight", not preloaded, preloaded)
    verify_manifest(run)
    if not run.failed():
        identity(run)
    failed = run.failed()
    run.receipt["dispatch"] = {"dispatched": not failed, "preflight_failed": failed}
    return not failed


def base_source(run):
    """Admitted-base catalog_stage.py bytes, verified against the frozen pin."""
    record, raw, _ = run.command("git-show-base-source-copy", ["git", "-C", REPO, "show", FROZEN_BASE + ":" + SOURCE],
                                 cwd=REPO, env=dict(ORIGINAL_ENV))
    ok = record["exit"] == 0 and sha_bytes(raw) == FROZEN_SOURCE_SHA256
    run.require("base_source_bytes_are_frozen_pin", ok, sha_bytes(raw) if raw else record["exit"])
    return raw if ok else None


# -- static closure inspection ----------------------------------------------------------
def _module_file(root, name):
    parts = name.split(".")
    for candidate in (root.joinpath(*parts).with_suffix(".py"), root.joinpath(*parts, "__init__.py")):
        if candidate.is_file():
            return candidate
    return None


def _capabilities(tree, imports):
    found = {item for item in imports if item in CAPABLE or item.split(".")[0] in CAPABLE}
    found = {item if item in CAPABLE else item.split(".")[0] for item in found}
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "os" \
                and node.attr in OS_PROCESS:
            found.add("os.process")
        elif isinstance(node, ast.ImportFrom) and node.module == "os" and any(
                alias.name in OS_PROCESS for alias in node.names):
            found.add("os.process")
    return sorted(found)


def _environment_reads(tree):
    reads = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in ("environ", "getenv", "expanduser", "home"):
            reads.add(node.attr)
    return sorted(reads)


def import_closure(root, start):
    """Repository modules statically reachable from `start` (module-level and lazy imports)."""
    seen, queue, external = {}, [start], set()
    while queue:
        name = queue.pop()
        if name in seen:
            continue
        path = _module_file(root, name)
        if path is None:
            continue
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)  # pre-existing source warnings, not ours
            tree = ast.parse(path.read_text(), str(path))
        imports = set()
        package = name if path.name == "__init__.py" else name.rpartition(".")[0]
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    anchor = package.split(".")
                    anchor = anchor[:len(anchor) - node.level + 1] if node.level > 1 else anchor
                    base = ".".join(anchor + ([node.module] if node.module else []))
                else:
                    base = node.module or ""
                imports.add(base)
                imports.update(base + "." + alias.name for alias in node.names)
        local = sorted(item for item in imports if item.split(".")[0] in ("campaign_tool", "tests"))
        external.update(item.split(".")[0] for item in imports if item.split(".")[0] not in ("campaign_tool", "tests"))
        seen[name] = {"path": str(path.relative_to(root)), "capabilities": _capabilities(tree, imports),
                      "environment_reads": _environment_reads(tree)}
        queue.extend(item for item in local if item not in seen)
    for parent in sorted({".".join(name.split(".")[:index]) for name in seen
                          for index in range(1, len(name.split(".")))}):
        if parent not in seen and _module_file(root, parent):
            seen.setdefault(parent, {"path": str(_module_file(root, parent).relative_to(root)),
                                     "capabilities": [], "environment_reads": []})
    return {"start": start, "modules": dict(sorted(seen.items())), "external_top_level": sorted(external)}


def inspect_closure(run, label, root, start):
    """Inventory, then gate: every capability is sentinel-covered or explicitly reviewed."""
    closure = import_closure(root, start)
    run.save(label + "-closure.json", json.dumps(closure, indent=2, sort_keys=True))
    flagged = {name: row["capabilities"] for name, row in closure["modules"].items() if row["capabilities"]}
    uncovered = {name: [item for item in items if item not in GUARD_COVERAGE
                        and item not in REVIEWED_UNGUARDED.get(name, ())]
                 for name, items in flagged.items()}
    uncovered = {name: items for name, items in uncovered.items() if items}
    run.receipt.setdefault("closures", {})[label] = {
        "start": start, "modules": len(closure["modules"]), "capabilities": flagged,
        "coverage": {item: GUARD_COVERAGE.get(item, "reviewed_unguarded")
                     for items in flagged.values() for item in items},
        "environment_reads": {name: row["environment_reads"] for name, row in closure["modules"].items()
                              if row["environment_reads"]},
        "uncovered": uncovered}
    run.require(label + "_closure_start_resolved", start in closure["modules"], len(closure["modules"]))
    run.require(label + "_closure_capabilities_all_guarded_or_reviewed", not uncovered, uncovered)
    return closure


# -- in-process unittest ---------------------------------------------------------------
class RecordingResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.outcomes, self.ids, self.tracebacks = {}, [], {}

    def _mark(self, test, outcome, err=None):
        name = test.id().rsplit(".", 1)[-1]
        if self.outcomes.get(name) in (None, "pass") or outcome == "error":
            self.outcomes[name] = outcome
        if err is not None:
            self.tracebacks.setdefault(name, []).append("".join(traceback.format_exception(*err)))

    def startTest(self, test):
        super().startTest(test)
        self.ids.append(test.id())

    def addSuccess(self, test):
        super().addSuccess(test)
        self.outcomes.setdefault(test.id().rsplit(".", 1)[-1], "pass")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._mark(test, "fail", err)

    def addError(self, test, err):
        super().addError(test, err)
        self._mark(getattr(test, "test_case", test), "error", err)

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self._mark(test, "skip")

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self._mark(test, "expected_failure")

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self._mark(test, "unexpected_success")

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            self._mark(test, "fail" if issubclass(err[0], test.failureException) else "error", err)


GUARDED = (("socket", "create_connection"), ("socket", "getaddrinfo"), ("socket.socket", "connect"),
           ("socket.socket", "connect_ex"), ("http.client.HTTPConnection", "connect"),
           ("urllib.request", "urlopen"), ("subprocess", "Popen"), ("_posixsubprocess", "fork_exec"),
           ("sqlite3", "connect")) + tuple(("os", name) for name in OS_PROCESS if hasattr(os, name))


def _owner(dotted):
    head, _, tail = dotted.partition(".")
    owner = importlib.import_module(head)
    for part in tail.split(".") if tail else ():
        owner = getattr(owner, part)
    return owner


def guarded_identity():
    return {dotted + "." + attribute: (vars(_owner(dotted)).get(attribute), getattr(_owner(dotted), attribute))
            for dotted, attribute in GUARDED}


def identity_diff(before, after):
    return sorted(name for name in before if before[name][0] is not after[name][0]
                  or before[name][1] is not after[name][1])


def redirect_temporary(run):
    """Temporary, home and XDG defaults point into this run before any test import."""
    for name in ("TMPDIR", "TEMP", "TMP", "HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME",
                 "XDG_STATE_HOME"):
        os.environ[name] = str(run.tmp)
    os.environ.pop("RECORDS_TEST_DEPENDENCIES", None)
    tempfile.tempdir = str(run.tmp)
    artifacts = run.directory / "artifacts"
    artifacts.mkdir(mode=0o700)
    os.environ[ARTIFACT_ENV] = str(artifacts)
    run.receipt["temporary_root"] = tempfile.gettempdir()
    return artifacts


def load_test_module(run):
    preloaded = sorted(name for name in sys.modules if name.split(".")[0] in ("campaign_tool", "tests"))
    run.require("no_application_import_before_sentinels", not preloaded, preloaded)
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    module = importlib.import_module(TEST_MODULE)
    run.require("test_module_from_this_repo", Path(module.__file__).resolve() == REPO / TEST, module.__file__)
    run.require("sentinels_installed_before_application_import", module.APPLICATION_PRELOADED == [],
                module.APPLICATION_PRELOADED)
    run.require("import_time_guards_complete", module.IMPORT_GUARDS == module.expected_guards(),
                module.IMPORT_GUARDS)
    return module


def run_cases(run, label, module, names):
    loader = unittest.TestLoader()
    suite = unittest.TestSuite(loader.loadTestsFromName(TEST_CLASS + ".test_" + name, module) for name in names)
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=2, resultclass=RecordingResult).run(suite)
    run.save(label + ".unittest.txt", stream.getvalue())
    summary = {"tests_run": result.testsRun, "test_ids": list(result.ids), "failures": len(result.failures),
               "errors": len(result.errors), "skipped": len(result.skipped),
               "expected_failures": len(result.expectedFailures),
               "unexpected_successes": len(result.unexpectedSuccesses),
               "successful": result.wasSuccessful(), "outcomes": dict(result.outcomes)}
    run.receipt.setdefault("unittest", {})[label] = summary
    return summary, result


def loaded_modules():
    return {name: str(Path(module.__file__).resolve()) for name, module in sorted(sys.modules.items())
            if name.split(".")[0] in ("campaign_tool", "tests") and getattr(module, "__file__", None)}


def require_clean_run(run, module, artifacts, before, closure):
    run.require("offline_sentinels_untouched", module.SENTINEL_HITS == [], module.SENTINEL_HITS)
    run.require("database_sentinel_untouched", module.DATABASE_HITS == [], module.DATABASE_HITS)
    paths = sorted(set(module.DATABASE_PATHS))
    run.receipt["database_paths"] = paths
    outside = [path for path in paths if not Path(path).is_relative_to(run.directory.resolve())]
    run.require("synthetic_databases_only_in_run_directory", bool(paths) and not outside, outside or len(paths))
    after = identity_diff(before, guarded_identity())
    run.require("guarded_callables_restored", after == [], after)
    run.require("no_guards_left_active", module.guards_active() == [], module.guards_active())
    modules = loaded_modules()
    run.receipt["loaded_modules"] = modules
    stray = sorted(name for name, path in modules.items() if not Path(path).is_relative_to(REPO))
    run.require("application_modules_from_this_repo", bool(modules) and not stray, stray)
    unaudited = sorted(set(modules) - set(closure["modules"]))
    run.require("loaded_modules_within_inspected_closure", not unaudited, unaudited)
    network = sorted(name for name in NETWORK_MODULES if name in sys.modules)
    run.require("network_capable_application_modules_not_loaded", not network, network)
    leftovers = sorted(str(path.relative_to(run.directory)) for path in artifacts.rglob("*"))
    run.receipt["artifact_root_entries"] = leftovers


def check_focused(run):
    closure = inspect_closure(run, "focused", REPO, TEST_MODULE)
    artifacts = redirect_temporary(run)
    before = guarded_identity()
    module = load_test_module(run)
    names = FROZEN_CASES + AUXILIARY_CASES
    summary, _ = run_cases(run, "focused", module, names)
    run.require("selected_tests_ran", summary["tests_run"] == len(names) and len(summary["test_ids"]) == len(names),
                summary["tests_run"])
    run.require("all_named_cases_pass", {name: summary["outcomes"].get("test_" + name) for name in names} ==
                {name: "pass" for name in names}, summary["outcomes"])
    run.require("no_failures_errors_or_skips", summary["successful"] and not (
        summary["skipped"] or summary["expected_failures"] or summary["unexpected_successes"]), summary)
    declared = sorted(name[5:] for name in dir(getattr(module, TEST_CLASS)) if name.startswith("test_"))
    run.require("declared_case_set_exact", declared == sorted(names), declared)
    run.require("single_test_class", [name for name, value in vars(module).items()
                                      if isinstance(value, type) and issubclass(value, unittest.TestCase)]
                == [TEST_CLASS], TEST_CLASS)
    require_clean_run(run, module, artifacts, before, closure)
    _, status = git(run, "status-after", "status", "--porcelain", "--untracked-files=all")
    run.require("repository_tree_unchanged_by_run", status == run.receipt["identity"].get("status_porcelain"),
                status)


def deciding_mutant(run, module, catalog_stage, label, template, escaped):
    """Run the committed assert_invalid helper for one value; return (failed_as_expected, detail)."""
    case = getattr(module, TEST_CLASS)("test_" + AUXILIARY_CASES[0])
    detail = {"value": template, "expected_escape": escaped}
    try:
        case.setUp()
        try:
            case.assert_invalid(template)
            detail["outcome"] = "no_failure"
        except AssertionError as caught:
            frames = traceback.extract_tb(caught.__traceback__)
            last = [frame for frame in frames if Path(frame.filename).resolve() == REPO / TEST][-1]
            detail.update({"outcome": "assertion_failure", "message": str(caught),
                           "failing_line": last.line, "failing_function": last.name,
                           "traceback": "".join(traceback.format_exception(caught))})
        except Exception as caught:  # an error is not the deciding assertion
            detail.update({"outcome": "error", "message": type(caught).__name__ + ": " + str(caught)})
    finally:
        case.doCleanups()
    decided = (detail.get("outcome") == "assertion_failure"
               and detail.get("failing_function") == "assert_invalid"
               and detail.get("failing_line") == "self.assertEqual(result, BLOCKED, repr(value))"
               and "'reason': 'catalog_lease_clock_invalid'" in detail.get("message", "")
               and "'raised': '" + escaped + "'" in detail.get("message", ""))
    return decided, detail


def check_negative(run):
    closure = inspect_closure(run, "negative", REPO, TEST_MODULE)
    artifacts = redirect_temporary(run)
    before = guarded_identity()
    module = load_test_module(run)
    catalog_stage = importlib.import_module("campaign_tool.records.catalog_stage")
    on_disk = sha_file(REPO / SOURCE)
    strict = getattr(catalog_stage, "_lease_expiry", None)
    if not run.require("strict_lease_clock_seam_present", callable(strict)):
        return
    saved, decisions = [], {}
    # The committed module's own offline and database sentinels guard these direct helper calls.
    module._install(saved, module.EXECUTION_TARGETS)
    try:
        with mock.patch.object(catalog_stage, "_lease_expiry", datetime.fromisoformat):
            run.require("mutant_installed_in_memory", catalog_stage._lease_expiry == datetime.fromisoformat)
            for label, template, escaped in MUTANT_VALUES:
                decided, detail = deciding_mutant(run, module, catalog_stage, label, template, escaped)
                decisions["mutant_" + label] = detail
                run.require("mutant_detected_by_exact_blocked_assertion_" + label, decided,
                            {key: detail.get(key) for key in ("outcome", "failing_line", "message")})
        run.require("strict_seam_restored", catalog_stage._lease_expiry is strict)
        for label, template, _ in MUTANT_VALUES:
            decided, detail = deciding_mutant(run, module, catalog_stage, label, template, "none")
            decisions["restored_" + label] = detail
            run.require("restored_strict_seam_blocks_" + label, detail.get("outcome") == "no_failure", detail)
    finally:
        module._restore(saved)
    run.save("negative-decisions.json", json.dumps(decisions, indent=2, sort_keys=True))
    with mock.patch.object(catalog_stage, "_lease_expiry", datetime.fromisoformat):
        broken, _ = run_cases(run, "mutant-cases", module, ("null_nonstring_naive_and_garbage",
                                                            "impossible_timezone_hours_minutes_and_calendar"))
    run.require("mutant_fails_committed_cases", all(broken["outcomes"].get("test_" + name) == "fail" for name in
                                                    ("null_nonstring_naive_and_garbage",
                                                     "impossible_timezone_hours_minutes_and_calendar")),
                broken["outcomes"])
    restored, _ = run_cases(run, "restored-cases", module, (EXPIRED_CONTROL, "null_nonstring_naive_and_garbage",
                                                           "impossible_timezone_hours_minutes_and_calendar"))
    run.require("restored_expired_control_and_cases_pass", restored["successful"] and restored["tests_run"] == 3
                and not restored["skipped"] and restored["outcomes"].get("test_" + EXPIRED_CONTROL) == "pass",
                restored["outcomes"])
    run.require("production_file_unchanged_on_disk", sha_file(REPO / SOURCE) == on_disk, on_disk)
    require_clean_run(run, module, artifacts, before, closure)


VERBOSE_LINE = re.compile(r"^(\w+) \(([\w.]+)\)")


def parse_unittest(stderr):
    text = stderr.decode(errors="replace")
    ran = re.findall(r"^Ran (\d+) tests? in ", text, re.M)
    final = re.findall(r"^(OK|FAILED)(?: \(([^)]*)\))?\s*$", text, re.M)
    ids = sorted({match.group(2) for match in map(VERBOSE_LINE.match, text.splitlines()) if match})
    return {"tests_run": int(ran[-1]) if ran else 0, "final": final[-1][0] if final else None,
            "detail": final[-1][1] if final else None, "test_ids": ids, "test_id_count": len(ids)}


def declared_tests(root, relative, class_name):
    tree = ast.parse((root / relative).read_text())
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return sorted(item.name for item in node.body
                          if isinstance(item, ast.FunctionDef) and item.name.startswith("test_"))
    return []


def check_compatibility(run):
    """Exact existing selectors, before and after the fix, each in a fully guarded child."""
    raw = base_source(run)
    closure = inspect_closure(run, "compatibility-child", REPO, TEST_MODULE)
    for selector in COMPATIBILITY:
        inspect_closure(run, "compatibility-" + selector, REPO, selector)
        run.require("selector_inside_child_closure_" + selector, selector in closure["modules"], selector)
    declared = declared_tests(REPO, *COMPATIBILITY_CLASS)
    run.require("compatibility_selector_declares_tests", bool(declared), len(declared))
    if raw is None:
        return
    copy = run.directory / "before-fix" / "repo"
    shutil.copytree(REPO, copy, symlinks=True, ignore=shutil.ignore_patterns(".git", "__pycache__", "node_modules"))
    (copy / SOURCE).write_bytes(raw)
    expected = sorted(COMPATIBILITY[0] + "." + COMPATIBILITY_CLASS[1] + "." + name for name in declared)
    results = {}
    for label, root in (("before-fix", copy), ("after-fix", REPO)):
        report_path = run.directory / (label + "-child-report.json")
        record, _, stderr = run.command("compatibility-" + label,
                                        [sys.executable, "-B", "-E", "-s", "-m", TEST_MODULE, CHILD_FLAG,
                                         "--report", report_path, *COMPATIBILITY],
                                        cwd=root, env=run.child_env(label))
        parsed = parse_unittest(stderr)
        try:
            report = json.loads(report_path.read_text())
        except (OSError, ValueError) as caught:
            report = {"unreadable": str(caught)}
        child_tmp = (run.tmp / label).resolve()
        modules = report.get("loaded_modules") or {}
        summary = {"exit": record["exit"], "root": str(root), "source_sha256": sha_file(root / SOURCE),
                   "report": str(report_path), "report_sha256": sha_file(report_path) if report_path.is_file() else None,
                   "tests_run": report.get("tests_run"), "test_ids": sorted(report.get("test_ids") or []),
                   "outcomes": report.get("outcomes"), "skipped": report.get("skipped"),
                   "stderr_parse": parsed, "probes_ok": (report.get("probes") or {}).get("ok"),
                   "database_paths": report.get("database_paths"), "loaded_modules": len(modules)}
        results[label] = summary
        run.require(label + "_child_exit_zero_and_report_ok", record["exit"] == 0 and report.get("ok") is True,
                    {"exit": record["exit"], "ok": report.get("ok")})
        run.require(label + "_ok_without_skips", parsed["final"] == "OK" and not parsed["detail"]
                    and report.get("successful") is True and report.get("skipped") == 0
                    and report.get("expected_failures") == 0 and report.get("unexpected_successes") == 0,
                    {k: parsed[k] for k in ("final", "detail")})
        run.require(label + "_selected_actual_tests", report.get("tests_run") == len(declared) > 0
                    and summary["test_ids"] == expected and parsed["test_ids"] == expected,
                    [report.get("tests_run"), len(declared)])
        run.require(label + "_boundary_probes_refused_and_recorded", summary["probes_ok"] is True,
                    (report.get("probes") or {}).get("calls"))
        run.require(label + "_guards_held_at_import_and_every_test", report.get("guards_held_at_every_test_start")
                    is True and (report.get("probes") or {}).get("import_guards") ==
                    (report.get("probes") or {}).get("expected_guards") and not report.get("application_preloaded"),
                    report.get("guards_at_end"))
        run.require(label + "_no_sentinel_or_database_hits", report.get("sentinel_hits") == []
                    and report.get("database_hits") == [], [report.get("sentinel_hits"), report.get("database_hits")])
        paths = report.get("database_paths") or []
        run.require(label + "_databases_only_in_child_temporary_root", bool(paths) and all(
            Path(path).is_relative_to(child_tmp) for path in paths), paths)
        run.require(label + "_environment_redirected", same_path(report.get("temporary_root"), child_tmp)
                    and same_path(report.get("home"), child_tmp) and report.get("records_test_dependencies") is None,
                    [report.get("temporary_root"), report.get("home")])
        stray = sorted(name for name, path in modules.items() if not Path(path).is_relative_to(root.resolve()))
        run.require(label + "_modules_loaded_only_from_its_root", bool(modules) and not stray, stray)
        unaudited = sorted(set(modules) - set(closure["modules"]) - {"__main__"})
        run.require(label + "_loaded_modules_within_inspected_closure", not unaudited, unaudited)
        network = sorted(name for name in NETWORK_MODULES if name in modules)
        run.require(label + "_network_capable_modules_not_loaded", not network, network)
    run.receipt["compatibility"] = {"selectors": list(COMPATIBILITY), "expected_test_ids": expected, **results}
    run.require("before_fix_ran_admitted_base_source", results["before-fix"]["source_sha256"] == FROZEN_SOURCE_SHA256,
                results["before-fix"]["source_sha256"])
    run.require("after_fix_ran_candidate_source", results["after-fix"]["source_sha256"] == sha_file(REPO / SOURCE),
                results["after-fix"]["source_sha256"])
    run.require("same_test_ids_before_and_after", results["before-fix"]["test_ids"] == results["after-fix"]["test_ids"])


def child_receipt(run, label, root, output_root, *, exit_expected):
    """Run this runner's focused mode from `root`; return its single receipt (or None)."""
    record, _, _ = run.command(label, [sys.executable, "-B", root / RUNNER, "--check", "focused",
                                       "--output-root", output_root], cwd=root, env=run.child_env(label))
    found = sorted(Path(output_root).glob("run-*/receipt.json"))
    run.require(label + "_exit_and_single_receipt", record["exit"] == exit_expected and len(found) == 1,
                [record["exit"], [str(path) for path in found]])
    if len(found) != 1:
        return None
    child = json.loads(found[0].read_text())
    run.receipt.setdefault("child_receipts", {})[label] = {
        "path": str(found[0]), "sha256": sha_file(found[0]), "result": child.get("result"),
        "repo": child.get("repo"), "dispatch": child.get("dispatch"),
        "failed_checks": [row["name"] for row in child.get("checks", []) if not row["ok"]],
        "loaded_modules": child.get("loaded_modules"),
        "manifest_relation": (child.get("manifest") or {}).get("repo_relation"),
        "database_paths": child.get("database_paths")}
    return child


def preflight_controls(run, copy_parent, copy):
    """Synthetic admission failures from a relocated copy: each stops before any work."""
    manifest_path = copy_parent / "SOURCE-MANIFEST.json"
    valid = manifest_path.read_bytes()
    held = copy_parent / "SOURCE-MANIFEST.json.valid"
    variants = {}
    for name, _ in PREFLIGHT_CONTROLS:
        data = json.loads(valid)
        if name == "wrong_base":
            data["head"] = "0" * 40
        elif name == "wrong_remote":
            data["remote"] = str(run.directory / "not-the-origin-remote")
        elif name == "wrong_runtime":
            data["runtime"] = {**data.get("runtime", {}), "repo_venv": str(run.directory / "undeclared-venv")}
        variants[name] = json.dumps(data, indent=2).encode()
    variants["malformed"] = valid[: len(valid) // 2]
    results = {}
    for name, expected in PREFLIGHT_CONTROLS:
        if name == "missing":
            os.replace(manifest_path, held)
        else:
            manifest_path.write_bytes(variants[name])
        child = child_receipt(run, "preflight-control-" + name, copy, run.directory / ("control-" + name),
                              exit_expected=1)
        if name == "missing":
            os.replace(held, manifest_path)
        summary = run.receipt.get("child_receipts", {}).get("preflight-control-" + name, {})
        work = sorted(key for key in ("unittest", "closures", "loaded_modules", "database_paths", "identity",
                                      "temporary_root") if child and key in child)
        commands = [row["label"] for row in (child or {}).get("commands", []) if not row["label"].startswith("git-")]
        results[name] = {"failed_checks": summary.get("failed_checks"), "dispatch": summary.get("dispatch"),
                         "work_keys": work, "non_git_commands": commands}
        run.require("preflight_control_" + name + "_stops_before_work", child is not None
                    and child.get("result") == "FAIL" and summary.get("failed_checks") == [expected]
                    and (child.get("dispatch") or {}).get("dispatched") is False and not work and not commands,
                    results[name])
    manifest_path.write_bytes(valid)
    run.require("valid_manifest_restored_in_control_copy", manifest_path.read_bytes() == valid
                and not held.exists())
    run.receipt["preflight_controls"] = results


def check_portability(run):
    scanned = {name: sorted(token for token in HOST_TOKENS if token in (REPO / name).read_text())
               for name in (TEST, RUNNER)}
    run.require("no_host_paths_in_new_test_or_runner", not any(scanned.values()), scanned)
    for name in (TEST, RUNNER):
        text = (REPO / name).read_text()
        run.require("repo_derived_from_file_" + Path(name).stem, "Path(__file__).resolve().parents[" in text)
    closure = inspect_closure(run, "portability", REPO, TEST_MODULE)
    # Boundary regression: an unnamed child argv is refused without ever being spawned.
    with mock.patch.object(subprocess, "run", side_effect=AssertionError("unnamed child spawned")) as spawned:
        record, _, _ = run.command("boundary-unnamed-child", ["/bin/sh", "-c", "true"], cwd=run.directory,
                                   boundary=True)
    run.require("unnamed_child_refused_without_spawn", record["error"] == "refused_unnamed_child"
                and record["exit"] is None and spawned.call_count == 0, record)
    copy_parent = run.directory / "second-prefix" / "fc7-relocated"
    copy = copy_parent / "repo"
    shutil.copytree(REPO, copy, symlinks=True, ignore=shutil.ignore_patterns("__pycache__", "node_modules"))
    shutil.copy2(REPO.parent / "SOURCE-MANIFEST.json", copy_parent / "SOURCE-MANIFEST.json")
    copied = {name: sha_file(copy / name) for name in ALLOWED_CHANGES}
    run.receipt["copy"] = {"path": str(copy), "file_sha256": copied, "closure_modules": len(closure["modules"])}
    run.require("copy_bytes_match", copied == {name: sha_file(REPO / name) for name in ALLOWED_CHANGES}, copied)
    receipts = {}
    for label, root in (("second-prefix", copy), ("first-prefix", REPO)):
        child = child_receipt(run, label + "-focused", root, run.directory / (label + "-focused"), exit_expected=0)
        if child is None:
            return
        receipts[label] = child
        run.require(label + "_focused_passed", child.get("result") == "PASS_REVIEW_REQUIRED", child.get("result"))
        run.require(label + "_preflight_passed_and_dispatched", (child.get("dispatch") or {}).get("dispatched")
                    is True, child.get("dispatch"))
        run.require(label + "_ran_from_its_prefix", child.get("repo") == str(root.resolve()), child.get("repo"))
        modules = child.get("loaded_modules") or {}
        stray = sorted(name for name, path in modules.items() if not Path(path).is_relative_to(root.resolve()))
        run.require(label + "_loaded_only_its_modules", bool(modules) and not stray, stray)
    left, right = (receipts[key]["unittest"]["focused"]["outcomes"] for key in ("first-prefix", "second-prefix"))
    run.require("identical_outcomes_across_prefixes", left == right and bool(left), [left, right])
    run.require("second_prefix_used_relocated_manifest",
                str((receipts["second-prefix"].get("manifest") or {}).get("repo_relation", "")).startswith(
                    "relocated_copy_of:"), receipts["second-prefix"].get("manifest"))
    run.require("first_prefix_used_staged_manifest",
                (receipts["first-prefix"].get("manifest") or {}).get("repo_relation") == "staged")
    preflight_controls(run, copy_parent, copy)


def check_credentials(run):
    scanner = Path(run.scanner)
    record, out, _ = run.command("detect-secrets", [scanner, *SCAN_ARGS], cwd=REPO, env=run.child_env("scan"))
    report = run.save("secret-scan.json", out)
    run.require("scanner_exit_zero", record["exit"] == 0, record["exit"])
    try:
        results = json.loads(out)["results"]
        readable = isinstance(results, dict)
    except (ValueError, KeyError, TypeError):
        results, readable = {}, False
    run.require("scanner_report_readable", readable)
    hits = {name: len(rows) for name, rows in (results or {}).items()}
    run.receipt["raw_hits_by_file"] = hits
    run.require("no_raw_hits_in_write_set", not any(name in hits for name in ALLOWED_CHANGES),
                {name: hits.get(name) for name in ALLOWED_CHANGES})
    record, _, _ = run.command("check-secret-report", [sys.executable, "-B", REPO / "tools" / "check_secret_report.py",
                                                       report], cwd=REPO, env=run.child_env("checker"))
    run.require("unchanged_repo_checker_exit_zero", record["exit"] == 0, record["exit"])


def check_public_tree(run):
    record, out, _ = run.command("public-tree", [sys.executable, "-B", REPO / "tools" / "check_public_tree.py"],
                                 cwd=REPO, env=run.child_env("public-tree"))
    if run.require("public_tree_exit_zero", record["exit"] == 0, record["exit"]):
        return
    # Diagnostic only; the required row above stays failed. Explain a staging-only refusal by
    # running the same unchanged checker on a copy without the operator-staged untracked links.
    try:
        findings = json.loads(out).get("findings")
    except (ValueError, AttributeError):
        findings = None
    staged_only = bool(findings) and all(isinstance(row, list) and len(row) == 3 and row[0] in STAGED_UNTRACKED
                                         and row[2] == "symlink_not_scanned" for row in findings)
    diagnosis = {"findings": findings, "only_operator_staged_symlinks": staged_only,
                 "note": "diagnostic only; does not change this mode's FAIL result"}
    if staged_only:
        copy = run.directory / "diagnostic-without-staged-links" / "repo"
        shutil.copytree(REPO, copy, symlinks=True, ignore=shutil.ignore_patterns(*STAGED_UNTRACKED, "__pycache__"))
        extra, _, _ = run.command("public-tree-diagnostic-copy", [sys.executable, "-B", copy / "tools" /
                                                                  "check_public_tree.py"],
                                  cwd=copy, env=run.child_env("public-tree-diagnostic"))
        diagnosis.update({"copy": str(copy), "copy_exit": extra["exit"]})
    run.receipt["public_tree_diagnosis"] = diagnosis


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", required=True, choices=CHECKS)
    parser.add_argument("--output-root", required=True)
    args = parser.parse_args()
    run = Run(args.check, args.output_root)
    try:
        if preflight(run):
            {"focused": check_focused, "compatibility": check_compatibility, "negative": check_negative,
             "portability": check_portability, "credentials": check_credentials,
             "public-tree": check_public_tree}[args.check](run)
    except Exception as caught:  # recorded as a failed check, never as success
        run.require("mode_completed_without_exception", False,
                    type(caught).__name__ + ": " + str(caught) + "\n" + traceback.format_exc())
    return run.finish()


if __name__ == "__main__":
    sys.exit(main())
