#!/usr/bin/env python3
"""FC7-REPORT-CLOSE acceptance runner: six independent --check modes.

Each invocation creates a fresh exclusive run directory under --output-root,
records argv, cwd, source identity, child argv/exit/stdout/stderr, selected test
IDs and counts, and exits 0 only when every check of its mode passed. Output is
REVIEW REQUIRED evidence, never semantic, legal, stage, live or release
acceptance.

Every mode starts with a fail-closed preflight: the staged SOURCE-MANIFEST.json
beside the repository is verified against local git facts (base commit, tree,
origin remote) and the declared runtime, and any failed preflight check aborts
the mode before an application import, test, scanner or checker runs.

Application code only ever runs behind a guard installed before its first
import: network, model/mail module imports, database paths outside the run
directory and child processes are refused. The compatibility selector runs in a
guarded child interpreter of this script (internal entry --internal-guarded-run),
and the only children that guarded process may start are the four admitted
records CLI commands with paths inside its temporary root; each is re-launched
through this script's guarded CLI entry (--internal-guarded-cli), so it carries
the same guard. The internal entries are not acceptance modes. Guards are an
accident barrier, not a hostile-code sandbox. Child processes are literal argv
arrays; none uses a shell.
"""
import argparse
import ast
from datetime import datetime, timezone
import difflib
import hashlib
import importlib
import importlib.abc
import io
import json
import os
from pathlib import Path
import re
import runpy
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.dont_write_bytecode = True
RUNNER_FILE = Path(__file__).resolve()
REPO = RUNNER_FILE.parents[1]
MANIFEST_NAME = "SOURCE-MANIFEST.json"
MANIFEST = REPO.parent / MANIFEST_NAME
MANIFEST_SCHEMA = "agentorange.source-manifest/v1"
CHECKS = ("focused", "compatibility", "negative", "portability", "credentials", "public-tree")
SOURCE = "campaign_tool/records/gates/safe_output.py"
TEST = "tests/records/test_fc7_report_close.py"
RUNNER = "tools/accept_fc7_report_close.py"
FIXTURE = "tests/fixtures/review-cases.json"
ALLOWED_CHANGES = (SOURCE, TEST, RUNNER)
TEST_MODULE = "tests.records.test_fc7_report_close"
TEST_CLASS = "ReportCloseTest"
REQUIRED_CASES = ("successful_write_and_replace", "write_or_fsync_failure_with_successful_cleanup",
                  "unlink_failure_still_closes_directory", "close_failure_remains_failure",
                  "original_target_and_residue_evidence", "symlink_and_nonregular_target_refusal")
# Fully mocked cleanup-failure cases the base finalizer must fail.
MUTANT_CASES = ("unlink_failure_still_closes_directory", "original_target_and_residue_evidence")
COMPATIBILITY_FILE = "tests/records/test_review_regressions.py"
COMPATIBILITY_CLASS = "PrivateReportTests"
COMPATIBILITY_MODULE = "tests.records.test_review_regressions"
COMPATIBILITY = COMPATIBILITY_MODULE + "." + COMPATIBILITY_CLASS
# The records CLI commands the compatibility selector may launch, and the
# modules the unchanged campaign_tool/records/cli.py maps them to.
CLI_MODULE = "campaign_tool.records"
CLI_COMMANDS = {"validate-findings": "gates.validate_findings", "batch-report": "gates.batch_report",
                "reconcile-coverage": "gates.reconcile_coverage", "collect-reviews": "gates.collect_reviews"}
CLI_FLAGS = ("--output", "--check-files")
# Static import closure of the focused in-process run (module file -> allowed imports).
CLOSURE = {
    TEST: {"os", "pathlib", "stat", "sys", "tempfile", "unittest", "unittest.mock",
           "campaign_tool.records.gates"},
    SOURCE: {"os", "pathlib", "secrets", "stat"},
    "campaign_tool/__init__.py": set(), "campaign_tool/records/__init__.py": set(),
    "campaign_tool/records/gates/__init__.py": set(), "tests/__init__.py": set(),
    "tests/records/__init__.py": set(),
}
LOCAL_PACKAGES = ("campaign_tool", "tests", "tools")
# Non-stdlib top-level names the compatibility closure may reference: pypdf is
# the declared offline PDF parser; the others are the gates modules' script-mode
# fallback imports (taken only when __package__ is empty), which must never
# actually load (checked at run time).
FALLBACK_ALIASES = ("safe_output", "validate_findings")
ADMITTED_EXTERNAL = {"pypdf": "declared offline PDF parser"}
ADMITTED_EXTERNAL.update({name: "script-mode fallback alias; must not load" for name in FALLBACK_ALIASES})
# Process-spawning or dynamic-import references allowed in the compatibility
# closure: (module, attribute) -> count. Anything else fails before execution.
# The extract.ocr references are default-argument runners of OCR helpers the
# selector never calls; at run time the guard still refuses any such child.
ADMITTED_SPAWN_SITES = {(COMPATIBILITY_MODULE, "subprocess.run"): 2,
                        ("campaign_tool.records.extract.ocr", "subprocess.run"): 2,
                        ("campaign_tool.records.public_scan", "subprocess.check_output"): 1}
ADMITTED_DYNAMIC_IMPORTS = {("campaign_tool.records.cli", "importlib.import_module"): 1,
                            # literal importlib.import_module("pypdf") version probe
                            ("campaign_tool.records.extract.ocr", "importlib.import_module"): 1}
SPAWN_NAMES = {"subprocess": {"run", "Popen", "call", "check_call", "check_output", "getoutput",
                              "getstatusoutput"},
               "os": {"system", "popen", "fork", "forkpty", "posix_spawn", "posix_spawnp", "execv",
                      "execve", "execvp", "execvpe", "execl", "execle", "execlp", "execlpe", "spawnv",
                      "spawnve", "spawnvp", "spawnvpe", "spawnl", "spawnle", "spawnlp", "spawnlpe"}}
DYNAMIC_IMPORT_NAMES = {"importlib": {"import_module", "__import__"}, "runpy": {"run_module", "run_path"}}
# Alternate process APIs; the compatibility closure may not import any of them.
PROCESS_MODULES = ("pty", "multiprocessing", "concurrent", "asyncio", "_posixsubprocess", "pexpect")
NETWORK_MODULES = ("campaign_tool.records.portal.https_transport", "campaign_tool.digest.model",
                   "campaign_tool.outbox", "campaign_tool.meetings", "campaign_tool.discovery",
                   "campaign_tool.records.worker_lifecycle", "campaign_tool.records.ocr_stage",
                   "campaign_tool.records.intake.imap_intake")
BLOCKED_IMPORTS = NETWORK_MODULES + ("campaign_tool.digest", "smtplib", "imaplib", "poplib", "ftplib",
                                     "telnetlib", "requests", "httpx", "urllib3", "aiohttp", "openai",
                                     "anthropic", "ollama")
# Built from pieces so this file does not match its own portability scan.
HOST_TOKENS = ("/" + "home/", "/" + "Users/", "agent" + "-work", "." + "hermes", "kan" + "ban",
               "/" + "tmp/", "/" + "workspace/", "/" + "private/var/")
ARTIFACT_ENV = "FC7_REPORT_CLOSE_ARTIFACT_ROOT"
GUARD_MESSAGE = "FC7 guard refused "
COPY_IGNORE = shutil.ignore_patterns(".git", ".venv", "__pycache__", "node_modules")
CONTRACT = {"card": "FC7-REPORT-CLOSE", "task": "t_58006055", "checks": list(CHECKS),
            "write_set": list(ALLOWED_CHANGES), "frozen_cases": list(REQUIRED_CASES),
            "compatibility_selector": COMPATIBILITY, "compatibility_cli_commands": sorted(CLI_COMMANDS),
            "negative": "base sequential unlink-then-close finalizer restored in memory only; "
                        "fail-closed manifest preflight controls",
            "status": "REVIEW_REQUIRED_NOT_ACCEPTANCE", "revision": 2}
TIMEOUT = 1800


def sha_bytes(raw):
    return hashlib.sha256(raw).hexdigest()


def sha_file(path):
    return sha_bytes(Path(path).read_bytes())


def now():
    return datetime.now(timezone.utc).isoformat()


def canonical(data):
    return json.dumps(data, indent=2, sort_keys=True, default=str) + "\n"


class Preflight(Exception):
    """A failed preflight requirement: the mode stops before any work."""


class Run:
    def __init__(self, check, output_root, *, quiet=False):
        self.check, self.quiet = check, quiet
        output_root = Path(os.path.abspath(output_root))
        if output_root == REPO or REPO in output_root.parents:
            raise SystemExit("output root must be outside the repository")
        output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        self.directory = output_root / f"run-{stamp}-{os.getpid()}"
        self.directory.mkdir(mode=0o700)  # exclusive: fails if it already exists
        self.checks, self.files = [], {}
        self.receipt = {"schema": "fc7-report-close-acceptance-v2", "check": check,
                        "contract": CONTRACT, "contract_sha256": sha_bytes(canonical(CONTRACT).encode()),
                        "runner_sha256": sha_file(RUNNER_FILE), "argv": list(sys.argv), "cwd": os.getcwd(),
                        "executable": sys.executable, "python": sys.version, "repo": str(REPO),
                        "started_at": now(), "run_directory": str(self.directory),
                        "work_started": False, "status": "REVIEW_REQUIRED_NOT_ACCEPTANCE"}

    def require(self, name, ok, detail=None):
        self.checks.append({"name": name, "ok": bool(ok), "detail": detail})
        return bool(ok)

    def must(self, name, ok, detail=None):
        """A preflight requirement: record it and stop the mode when it fails."""
        if not self.require(name, ok, detail):
            raise Preflight(name)

    def begin_work(self, label):
        self.receipt["work_started"] = True
        self.receipt.setdefault("work", []).append(label)

    def save(self, name, data):
        raw = data.encode() if isinstance(data, str) else data
        path = self.directory / name
        with open(path, "xb") as stream:
            stream.write(raw)
        self.files[name] = {"path": str(path), "sha256": sha_bytes(raw), "bytes": len(raw)}
        return path

    def command(self, label, argv, *, cwd, env=None):
        """Run one literal argv without a shell; save raw stdout/stderr."""
        started = now()
        try:
            done = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, timeout=TIMEOUT,
                                  shell=False, check=False)
            code, stdout, stderr, error = done.returncode, done.stdout, done.stderr, None
        except (OSError, subprocess.TimeoutExpired) as caught:
            code, stdout, stderr, error = None, b"", b"", type(caught).__name__ + ": " + str(caught)
        record = {"label": label, "argv": [str(part) for part in argv], "cwd": str(cwd),
                  "started_at": started, "ended_at": now(), "exit": code, "error": error,
                  "stdout": self.save(label + ".stdout", stdout).name,
                  "stderr": self.save(label + ".stderr", stderr).name}
        self.receipt.setdefault("commands", []).append(record)
        return record, stdout, stderr

    def finish(self):
        passed = bool(self.checks) and all(row["ok"] for row in self.checks)
        self.receipt.update({"checks": self.checks, "files": self.files, "ended_at": now(),
                             "result": "PASS_REVIEW_REQUIRED" if passed else "FAIL"})
        raw = canonical(self.receipt)
        with open(self.directory / "receipt.json", "x", encoding="utf-8") as stream:
            stream.write(raw)
        if not self.quiet:
            print(json.dumps({"check": self.check, "result": self.receipt["result"],
                              "run_directory": str(self.directory), "receipt_sha256": sha_bytes(raw.encode()),
                              "checks_passed": sum(row["ok"] for row in self.checks),
                              "checks_total": len(self.checks),
                              "failed_checks": [row["name"] for row in self.checks if not row["ok"]]}, indent=2))
        return 0 if passed else 1


def git(run, label, *arguments, cwd=REPO):
    record, stdout, _ = run.command("git-" + label, ["git", *arguments], cwd=cwd)
    return record["exit"], stdout.decode(errors="replace")


# ---------------------------------------------------------------- preflight

def read_manifest(run, path, prefix):
    run.must(prefix + "present", Path(path).is_file(), str(path))
    raw = Path(path).read_bytes()
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    run.must(prefix + "json_object", isinstance(data, dict), str(path))
    return data, sha_bytes(raw)


def is_sha(value):
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{40}", value))


def executable_file(value):
    return isinstance(value, str) and os.path.isabs(value) and os.path.isfile(value) and os.access(value, os.X_OK)


def verify_git_facts(run, origin_repo, manifest):
    """The declared base commit, tree and remote, checked against local git only."""
    head, tree, remote = manifest["head"], manifest["tree"], manifest.get("remote")
    code, out = git(run, "verify-base-commit", "rev-parse", "--verify", "--quiet", head + "^{commit}",
                    cwd=origin_repo)
    run.must("manifest_base_commit_present", code == 0 and out.strip() == head, {"exit": code, "out": out.strip()})
    code, out = git(run, "verify-base-tree", "rev-parse", "--verify", "--quiet", head + "^{tree}",
                    cwd=origin_repo)
    run.must("manifest_tree_is_base_tree", code == 0 and out.strip() == tree,
             {"declared": tree, "actual": out.strip(), "exit": code})
    code, out = git(run, "origin-url", "remote", "get-url", "origin", cwd=origin_repo)
    run.must("manifest_remote_is_local_origin", code == 0 and isinstance(remote, str) and out.strip() == remote,
             {"declared": remote, "origin": out.strip(), "exit": code})
    run.must("manifest_remote_is_local_directory", os.path.isabs(remote) and os.path.isdir(remote), remote)
    # Read-only object lookup in the admitted local source clone; no fetch, no network.
    code, _ = git(run, "remote-has-base", "-C", remote, "cat-file", "-e", head + "^{commit}", cwd=origin_repo)
    run.must("manifest_remote_contains_base", code == 0, code)


def verify_runtime(run, repo, manifest, check, executable):
    runtime = manifest.get("runtime")
    run.must("manifest_runtime_declared", isinstance(runtime, dict), runtime)
    credential, scanner, venv = (runtime.get(key) for key in ("credential_scan_python3", "detect_secrets",
                                                              "repo_venv"))
    run.receipt["runtime_declared"] = {"credential_scan_python3": credential, "detect_secrets": scanner,
                                       "repo_venv": venv}
    run.must("runtime_credential_scan_python3_executable", executable_file(credential), credential)
    run.must("runtime_detect_secrets_executable", executable_file(scanner), scanner)
    run.must("runtime_repo_venv_python3_executable", isinstance(venv, str) and os.path.isabs(venv)
             and executable_file(os.path.join(venv, "bin", "python3")), venv)
    staged = repo / ".venv"
    if staged.exists() or staged.is_symlink():
        run.must("staged_venv_is_declared_repo_venv", os.path.realpath(staged) == os.path.realpath(venv),
                 {"staged": os.path.realpath(staged), "declared": venv})
    executable = os.path.abspath(executable)
    if check == "credentials":
        run.must("interpreter_is_declared_credential_scan_python3", executable == credential,
                 {"actual": executable, "declared": credential})
    else:
        # The ordinary rows use repo/.venv/bin/python3, the staged link to the declared venv.
        environment = os.path.dirname(os.path.dirname(executable))
        run.must("interpreter_is_declared_repo_venv", os.path.basename(executable).startswith("python")
                 and os.path.realpath(environment) == os.path.realpath(venv),
                 {"actual": executable, "environment": os.path.realpath(environment), "declared": venv})


def preflight(run, repo=REPO, manifest_path=None, check=None, executable=None):
    """Verify the staged manifest before any work; every failure raises Preflight."""
    manifest_path = Path(manifest_path or (MANIFEST if repo == REPO else repo.parent / MANIFEST_NAME))
    check, executable = check or run.check, executable or sys.executable
    manifest, digest = read_manifest(run, manifest_path, "source_manifest_")
    run.receipt["source_manifest"] = {"path": str(manifest_path), "sha256": digest,
                                      "head": manifest.get("head"), "tree": manifest.get("tree"),
                                      "remote": manifest.get("remote"), "repo": manifest.get("repo"),
                                      "derived_from": manifest.get("derived_from")}
    run.must("manifest_schema", manifest.get("schema") == MANIFEST_SCHEMA, manifest.get("schema"))
    run.must("manifest_card", manifest.get("card") == CONTRACT["card"], manifest.get("card"))
    run.must("manifest_repo_is_this_repo", isinstance(manifest.get("repo"), str)
             and os.path.abspath(manifest["repo"]) == str(repo), {"declared": manifest.get("repo"),
                                                                  "actual": str(repo)})
    run.must("manifest_base_is_full_sha", is_sha(manifest.get("head")), manifest.get("head"))
    run.must("manifest_tree_is_full_sha", is_sha(manifest.get("tree")), manifest.get("tree"))
    derived = manifest.get("derived_from")
    if (repo / ".git").exists():
        run.must("manifest_not_derived_in_git_checkout", derived is None, derived)
        origin_repo, origin_manifest = repo, manifest
    else:
        # A portability copy: its manifest must be the verified original plus a new repo path.
        run.must("derived_from_declared", isinstance(derived, dict) and isinstance(derived.get("path"), str)
                 and isinstance(derived.get("sha256"), str), derived)
        original, original_digest = read_manifest(run, derived["path"], "derived_from_manifest_")
        run.must("derived_from_sha256_matches", original_digest == derived["sha256"],
                 {"declared": derived["sha256"], "actual": original_digest})
        mine = {key: value for key, value in manifest.items() if key not in ("repo", "derived_from")}
        theirs = {key: value for key, value in original.items() if key != "repo"}
        run.must("derived_manifest_matches_original", mine == theirs,
                 sorted(key for key in set(mine) | set(theirs) if mine.get(key) != theirs.get(key)))
        origin_repo = Path(os.path.abspath(original.get("repo") or "/"))
        run.must("derived_original_is_git_checkout", (origin_repo / ".git").exists(), str(origin_repo))
        run.must("derived_original_manifest_beside_its_repo",
                 Path(derived["path"]).resolve() == (origin_repo.parent / MANIFEST_NAME).resolve(), derived["path"])
        copied = {name: [sha_file(root / name) if (root / name).is_file() else None for root in (repo, origin_repo)]
                  for name in ALLOWED_CHANGES + (FIXTURE,)}
        run.receipt["derived_copy_hashes"] = copied
        run.must("derived_copy_matches_original_files", all(a and a == b for a, b in copied.values()), copied)
        origin_manifest = original
    run.must("manifest_write_root_is_repo_parent", isinstance(origin_manifest.get("write_root"), str)
             and os.path.abspath(origin_manifest["write_root"]) == str(origin_repo.parent),
             origin_manifest.get("write_root"))
    verify_git_facts(run, origin_repo, origin_manifest)
    verify_runtime(run, repo, manifest, check, executable)
    return manifest


def identity(run, manifest, repo=REPO):
    hashes = {name: sha_file(repo / name) if (repo / name).is_file() else None for name in ALLOWED_CHANGES}
    record = {"repo": str(repo), "file_sha256": hashes, "base": manifest.get("head")}
    run.receipt["identity"] = record
    run.must("write_set_files_present", all(hashes.values()), hashes)
    if not (repo / ".git").exists():
        record["git_available"] = False  # verified derived copy (preflight)
        return record
    base = manifest["head"]
    code, head = git(run, "rev-parse-head", "rev-parse", "HEAD", cwd=repo)
    record["head"] = head.strip() if code == 0 else None
    run.must("candidate_head_known", is_sha(record["head"]), record["head"])
    code, tree = git(run, "rev-parse-head-tree", "rev-parse", "HEAD^{tree}", cwd=repo)
    record["tree"] = tree.strip() if code == 0 else None
    code, _ = git(run, "merge-base-is-ancestor", "merge-base", "--is-ancestor", base, "HEAD", cwd=repo)
    run.must("base_is_ancestor_of_head", code == 0, code)
    code, log = git(run, "log-base-head", "log", "--format=%H %s", base + "..HEAD", cwd=repo)
    record["candidate_commits"] = log.splitlines() if code == 0 else None
    code, names = git(run, "diff-names-base-head", "diff", "--name-only", base, "HEAD", cwd=repo)
    changed = set(names.split()) if code == 0 else None
    code, status = git(run, "status-porcelain", "status", "--porcelain", "--untracked-files=all", cwd=repo)
    lines = status.splitlines() if code == 0 else None
    dirty = {line[3:] for line in lines or () if line}
    record.update({"committed_changes_from_base": sorted(changed or ()), "status_porcelain": lines,
                   "dirty_paths": sorted(dirty)})
    run.must("diff_and_status_readable", changed is not None and lines is not None)
    outside = sorted(((changed or set()) | dirty) - set(ALLOWED_CHANGES))
    run.must("changes_only_in_write_set", not outside, outside)
    run.must("working_tree_clean", not dirty, sorted(dirty))
    return record


def start(run, repo=REPO):
    manifest = preflight(run, repo)
    return manifest, identity(run, manifest, repo)


# ---------------------------------------------------------------- static closure

def module_imports(path):
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add("." * node.level + (node.module or ""))
    return names


def inspect_closure(run, repo=REPO):
    """Static import inspection of every file the focused run executes, before execution."""
    found = {name: sorted(module_imports(repo / name)) for name in CLOSURE}
    extra = {name: sorted(set(found[name]) - CLOSURE[name]) for name in CLOSURE}
    run.receipt["static_closure"] = {"imports": found, "unexpected": extra}
    run.must("static_import_closure_admitted", not any(extra.values()), extra)


def resolve_module(repo, name):
    base = repo.joinpath(*name.split("."))
    if (base / "__init__.py").is_file():
        return base / "__init__.py"
    if base.with_suffix(".py").is_file():
        return base.with_suffix(".py")
    if base.is_dir():
        return base  # namespace package (no __init__.py)
    return None


def dotted(repo, path):
    parts = list(path.relative_to(repo).with_suffix("").parts)
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def absolute_imports(repo, path):
    """(module heads, candidate submodules) imported by one file, relative imports resolved."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    me = dotted(repo, path)
    package = me if path.name == "__init__.py" else me.rpartition(".")[0]
    heads, submodules = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            heads.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                anchor = package.split(".")[:len(package.split(".")) - node.level + 1]
                head = ".".join(anchor + ([node.module] if node.module else []))
            else:
                head = node.module
            heads.add(head)
            submodules.update(head + "." + alias.name for alias in node.names)
    return tree, heads, submodules


def references(tree, table):
    """Attribute references such as subprocess.run or importlib.import_module."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and \
                node.attr in table.get(node.value.id, ()):
            found.append((node.value.id + "." + node.attr, node.lineno))
    return found


def cli_command_map(repo):
    tree = ast.parse((repo / "campaign_tool/records/cli.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "COMMANDS"
                                                for t in node.targets):
            return ast.literal_eval(node.value)
    return None


def compatibility_closure(run, repo, label):
    """Complete static closure of the selector plus the admitted CLI children,
    validated before execution: local files only, admitted externals only, no
    blocked module, and only the admitted spawn and dynamic-import sites."""
    commands = cli_command_map(repo)
    mapped = {name: (commands or {}).get(name) for name in CLI_COMMANDS}
    run.must(label + "_cli_commands_map_to_admitted_modules", mapped == CLI_COMMANDS, mapped)
    roots = [COMPATIBILITY_MODULE, CLI_MODULE + ".__main__", CLI_MODULE + ".cli"] + \
            [CLI_MODULE + "." + module for module in CLI_COMMANDS.values()]
    modules, external, unresolved, spawns, dynamic = {}, set(), [], [], []
    queue = list(roots)
    while queue:
        name = queue.pop()
        top = name.split(".")[0]
        if name in modules:
            continue
        if top not in LOCAL_PACKAGES:
            external.add(top)
            continue
        path = resolve_module(repo, name)
        if path is None:
            unresolved.append(name)
            continue
        modules[name] = str(path.relative_to(repo))
        parts = name.split(".")
        queue.extend(".".join(parts[:i]) for i in range(1, len(parts)))
        if path.is_dir():
            continue
        tree, heads, submodules = absolute_imports(repo, path)
        queue.extend(heads)
        # "from package import name" may name a submodule; follow it only if it is a file.
        queue.extend(sub for sub in submodules if sub.split(".")[0] not in LOCAL_PACKAGES
                     or resolve_module(repo, sub) is not None)
        spawns.extend((name, ref, line) for ref, line in references(tree, SPAWN_NAMES))
        dynamic.extend((name, ref, line) for ref, line in references(tree, DYNAMIC_IMPORT_NAMES))
    # Unresolved local names must be attributes of a resolved module, never missing modules.
    missing = sorted(name for name in unresolved if name.rpartition(".")[0] not in modules)
    nonstdlib = sorted(name for name in external if name not in sys.stdlib_module_names)
    blocked = sorted(name for name in modules if any(name == b or name.startswith(b + ".") for b in BLOCKED_IMPORTS))
    blocked += sorted(name for name in external if name in BLOCKED_IMPORTS)

    def count(rows):
        tally = {}
        for module, ref, _ in rows:
            tally[(module, ref)] = tally.get((module, ref), 0) + 1
        return tally

    record = {"roots": roots, "modules": modules, "external": sorted(external), "nonstdlib": nonstdlib,
              "unresolved_attributes": sorted(unresolved), "missing": missing, "blocked": blocked,
              "spawn_sites": spawns, "dynamic_import_sites": dynamic}
    run.receipt.setdefault("compatibility_closure", {})[label] = record
    run.must(label + "_closure_has_no_missing_local_module", not missing, missing)
    run.must(label + "_closure_external_admitted", set(nonstdlib) <= set(ADMITTED_EXTERNAL), nonstdlib)
    run.must(label + "_closure_has_no_blocked_module", not blocked, blocked)
    process_apis = sorted(set(PROCESS_MODULES) & external)
    run.must(label + "_closure_has_no_alternate_process_api", not process_apis, process_apis)
    run.must(label + "_closure_spawn_sites_admitted", count(spawns) == ADMITTED_SPAWN_SITES,
             [list(row) for row in spawns])
    run.must(label + "_closure_dynamic_imports_admitted", count(dynamic) == ADMITTED_DYNAMIC_IMPORTS,
             [list(row) for row in dynamic])
    return set(modules)


# ---------------------------------------------------------------- guards

def guard_marked(function):
    function.fc7_guard = True
    return function


class ImportBlocker(importlib.abc.MetaPathFinder):
    def __init__(self, guard):
        self.guard = guard

    def find_spec(self, name, path=None, target=None):
        if any(name == blocked or name.startswith(blocked + ".") for blocked in BLOCKED_IMPORTS):
            self.guard.hits.append({"call": "import", "args": [name]})
            raise ImportError(GUARD_MESSAGE + "import of " + name)
        return None


class Guard:
    """Network, model/mail import, child-process and database-path refusal.

    install() patches the process permanently (guarded children); start()/stop()
    patch reversibly (in-process focused and negative runs). A child policy may
    admit specific child argv; without one every child process is refused."""

    def __init__(self, allowed_root, child_policy=None):
        self.allowed_root, self.child_policy = Path(allowed_root).resolve(), child_policy
        self.hits, self.patches, self.blocker = [], [], ImportBlocker(self)

    def refuse(self, name):
        @guard_marked
        def blocked(*args, **kwargs):
            self.hits.append({"call": name, "args": [repr(arg)[:160] for arg in args]})
            raise RuntimeError(GUARD_MESSAGE + name)
        return blocked

    def replacements(self):
        import http.client
        import socket
        import sqlite3
        import urllib.request
        real_connect = sqlite3.connect

        @guard_marked
        def database(target, *args, **kwargs):
            text = str(target)
            if text != ":memory:" and not Path(os.path.abspath(text)).is_relative_to(self.allowed_root):
                self.hits.append({"call": "sqlite3.connect", "args": [text]})
                raise RuntimeError(GUARD_MESSAGE + "database path")
            return real_connect(target, *args, **kwargs)

        targets = [(socket, "create_connection"), (socket, "getaddrinfo"), (socket.socket, "connect"),
                   (socket.socket, "connect_ex"), (http.client.HTTPConnection, "connect"),
                   (urllib.request, "urlopen")]
        targets += [(os, name) for name in sorted(SPAWN_NAMES["os"]) if hasattr(os, name)]
        rows = [(owner, attribute, self.refuse(getattr(owner, "__name__", str(owner)) + "." + attribute))
                for owner, attribute in targets]
        popen = self.child_policy.popen(self) if self.child_policy else self.refuse("subprocess.Popen")
        rows += [(subprocess, "Popen", popen), (sqlite3, "connect", database)]
        return rows

    def install(self):
        sys.meta_path.insert(0, self.blocker)
        for owner, attribute, value in self.replacements():
            setattr(owner, attribute, value)

    def start(self):
        sys.meta_path.insert(0, self.blocker)
        self.patches = [mock.patch.object(owner, attribute, value)
                        for owner, attribute, value in self.replacements()]
        for patch in self.patches:
            patch.start()

    def stop(self):
        for patch in reversed(self.patches):
            patch.stop()
        self.patches = []
        if self.blocker in sys.meta_path:
            sys.meta_path.remove(self.blocker)


def cli_refusal(argv, kwargs, scratch):
    """None when argv is an admitted records CLI child; otherwise the refusal reason."""
    if kwargs.get("shell") or kwargs.get("executable") or kwargs.get("cwd") is not None \
            or kwargs.get("env") is not None:
        return "shell_executable_cwd_or_env_override"
    if not isinstance(argv, (list, tuple)) or not all(isinstance(part, str) for part in argv):
        return "argv_not_a_literal_string_list"
    if list(argv[:3]) != [sys.executable, "-m", CLI_MODULE]:
        return "not_the_records_cli_under_this_interpreter"
    if len(argv) < 4 or argv[3] not in CLI_COMMANDS:
        return "command_not_admitted"
    for part in argv[4:]:
        if part.startswith("-"):
            if part not in CLI_FLAGS:
                return "flag_not_admitted"
        elif not (os.path.isabs(part) and ".." not in Path(part).parts
                  and Path(part).is_relative_to(scratch)):
            return "path_outside_temporary_root"
    return None


class CliPolicy:
    """Admits only the records CLI children and re-launches them guarded."""

    def __init__(self, repo, scratch, records):
        self.repo, self.scratch, self.records = Path(repo), Path(scratch).resolve(), Path(records)
        self.admitted, self.refused, self.probe_next = [], [], False
        self.real_popen = subprocess.Popen

    def popen(self, guard):
        @guard_marked
        def admitted_popen(args, *rest, **kwargs):
            reason = "positional_popen_arguments" if rest else cli_refusal(args, kwargs, self.scratch)
            if reason:
                self.refused.append({"argv": [str(part) for part in args] if isinstance(args, (list, tuple))
                                     else str(args), "reason": reason})
                guard.hits.append({"call": "subprocess.Popen", "args": [repr(args)[:160]], "reason": reason})
                raise RuntimeError(GUARD_MESSAGE + "child process: " + reason)
            index = len(self.admitted) + 1
            spec = {"repo": str(self.repo), "scratch": str(self.scratch),
                    "record": str(self.records / f"cli-{index}.json"), "probe": self.probe_next}
            self.probe_next = False
            spec_path = self.records / f"cli-{index}.spec.json"
            spec_path.write_text(canonical(spec))
            child = [sys.executable, "-B", str(RUNNER_FILE), "--internal-guarded-cli", str(spec_path),
                     "--", *args[3:]]
            environment = dict(os.environ, TMPDIR=str(self.scratch))
            self.admitted.append({"argv": list(args), "relaunched_as": child, "spec": spec})
            return self.real_popen(child, cwd=str(self.repo), env=environment, **kwargs)
        return admitted_popen


def application_modules():
    return sorted(name for name in sys.modules if name.split(".")[0] in LOCAL_PACKAGES or name in FALLBACK_ALIASES)


def loaded_modules():
    return {name: str(Path(module.__file__).resolve()) for name, module in sorted(sys.modules.items())
            if (name.split(".")[0] in LOCAL_PACKAGES or name in FALLBACK_ALIASES)
            and getattr(module, "__file__", None)}


def guarded_prologue(spec, child_policy=None):
    """Child-side: temp defaults, guard, then the repo on sys.path, all before application imports."""
    preloaded = application_modules()
    scratch = str(Path(spec["scratch"]).resolve())
    os.environ["TMPDIR"] = scratch
    os.environ[ARTIFACT_ENV] = scratch
    tempfile.tempdir = scratch
    guard = Guard(scratch, child_policy)
    guard.install()
    runner_dir = str(RUNNER_FILE.parent)
    sys.path[:] = [entry for entry in sys.path if entry not in ("", ".", runner_dir)]
    sys.path.insert(0, spec["repo"])
    return guard, {"pid": os.getpid(), "preloaded_application_modules": preloaded,
                   "guard_installed_before_application_import": not preloaded,
                   "sys_path_head": sys.path[0], "tempdir": scratch, "executable": sys.executable}


def run_probes(guard, policy=None):
    """Inert controls: every forbidden call is made only after confirming the guard
    replaced the target, so nothing real is ever attempted."""
    import http.client
    import socket
    import sqlite3
    import urllib.request
    probes = [
        ("network_create_connection", socket.create_connection, lambda: socket.create_connection(("192.0.2.1", 9))),
        ("network_getaddrinfo", socket.getaddrinfo, lambda: socket.getaddrinfo("fc7-synthetic.invalid", 80)),
        ("network_urlopen", urllib.request.urlopen, lambda: urllib.request.urlopen("http://192.0.2.1/")),
        ("network_http_connect", http.client.HTTPConnection.connect,
         lambda: http.client.HTTPConnection("192.0.2.1", 9).connect()),
        ("database_outside_root", sqlite3.connect, lambda: sqlite3.connect("/fc7-synthetic-forbidden/report.db")),
        ("os_system", os.system, lambda: os.system("true")),
        ("child_git", subprocess.Popen, lambda: subprocess.run(["git", "--version"])),
        ("child_shell", subprocess.Popen, lambda: subprocess.run("true", shell=True)),
        ("child_unadmitted_records_command", subprocess.Popen,
         lambda: subprocess.run([sys.executable, "-m", CLI_MODULE, "publish"])),
        ("child_path_outside_root", subprocess.Popen,
         lambda: subprocess.run([sys.executable, "-m", CLI_MODULE, "batch-report", "/fc7-synthetic-outside/root"])),
    ]
    results = []
    for name, target, attempt in probes:
        before = len(guard.hits)
        if not getattr(target, "fc7_guard", False):
            results.append({"probe": name, "outcome": "guard_absent_not_attempted"})
            continue
        try:
            attempt()
            outcome = "allowed"
        except RuntimeError as caught:
            outcome = "refused" if str(caught).startswith(GUARD_MESSAGE) else "other_error: " + str(caught)
        results.append({"probe": name, "outcome": outcome, "hits_added": len(guard.hits) - before})
    for name, module in (("model_import", "campaign_tool.digest.model"), ("mail_import", "smtplib")):
        if module in sys.modules or guard.blocker not in sys.meta_path:
            results.append({"probe": name, "outcome": "precondition_failed_not_attempted"})
            continue
        before = len(guard.hits)
        try:
            importlib.import_module(module)
            outcome = "allowed"
        except ImportError as caught:
            outcome = "refused" if GUARD_MESSAGE in str(caught) else "other_error: " + str(caught)
        results.append({"probe": name, "outcome": outcome, "hits_added": len(guard.hits) - before,
                        "loaded_after": module in sys.modules})
    memory = sqlite3.connect(":memory:")
    memory.close()
    results.append({"probe": "database_memory_positive", "outcome": "allowed"})
    return results


def guarded_cli_main(spec_path, arguments):
    """Internal: one admitted records CLI child, guard installed before its import."""
    spec = json.loads(Path(spec_path).read_text())
    guard, record = guarded_prologue(spec)
    record.update({"role": "records-cli", "arguments": arguments, "probe": spec.get("probe", False)})
    code = 97
    try:
        if not record["guard_installed_before_application_import"]:
            code = 98
        elif spec.get("probe"):
            record["probes"] = run_probes(guard)
            code = 0
        else:
            sys.argv = [str(Path(spec["repo"]) / CLI_MODULE.replace(".", "/") / "__main__.py"), *arguments]
            try:
                runpy.run_module(CLI_MODULE, run_name="__main__", alter_sys=True)
                code = 0
            except SystemExit as caught:
                code = caught.code if isinstance(caught.code, int) else (0 if caught.code is None else 1)
    finally:
        record.update({"exit": code, "guard_hits": guard.hits, "loaded_modules": loaded_modules()})
        with open(spec["record"], "x", encoding="utf-8") as stream:
            stream.write(canonical(record))
    return code


def guarded_run_main(spec_path):
    """Internal: the compatibility selector (or the guard controls) behind the guard."""
    spec = json.loads(Path(spec_path).read_text())
    records = Path(spec["records"])
    policy = CliPolicy(spec["repo"], spec["scratch"], records)
    guard, record = guarded_prologue(spec, policy)
    record.update({"role": "guarded-" + spec["mode"]})
    code = 97
    try:
        if not record["guard_installed_before_application_import"]:
            code = 98
        elif spec["mode"] == "controls":
            record["probes"] = run_probes(guard, policy)
            record["refused_before_child_probe"] = list(policy.refused)
            # Guard propagation: one admitted CLI argv, re-launched in probe mode.
            policy.probe_next = True
            probe_root = Path(spec["scratch"]) / "probe-root"
            probe_root.mkdir(mode=0o700)
            done = subprocess.run([sys.executable, "-m", CLI_MODULE, "collect-reviews", str(probe_root)],
                                  capture_output=True)
            record["child_probe_exit"] = done.returncode
            code = 0
        else:
            suite = unittest.TestLoader().loadTestsFromName(spec["selector"])
            result = unittest.TextTestRunner(stream=sys.stderr, verbosity=2).run(suite)
            code = 0 if result.wasSuccessful() else 1
    finally:
        record.update({"exit": code, "guard_hits": guard.hits, "loaded_modules": loaded_modules(),
                       "admitted_children": policy.admitted, "refused_children": policy.refused})
        with open(spec["result"], "x", encoding="utf-8") as stream:
            stream.write(canonical(record))
    return code


# ---------------------------------------------------------------- focused / negative

def open_descriptors():
    try:
        return len(os.listdir("/dev/fd"))
    except OSError:
        return None


def prepare_in_process(run):
    """Fresh temporary defaults and the guard, then import the test module."""
    preloaded = application_modules()
    run.must("no_application_import_before_guard", not preloaded, preloaded)
    scratch = run.directory / "tmp"
    scratch.mkdir(mode=0o700)
    os.environ["TMPDIR"] = str(scratch)
    os.environ[ARTIFACT_ENV] = str(scratch)
    tempfile.tempdir = str(scratch)
    inspect_closure(run)
    run.begin_work("in-process tests")
    guard = Guard(run.directory)
    guard.start()
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    module = importlib.import_module(TEST_MODULE)
    run.require("test_module_from_this_repo", Path(module.__file__).resolve() == REPO / TEST, module.__file__)
    run.require("guard_before_application_import", module.APPLICATION_PRELOADED == [],
                module.APPLICATION_PRELOADED)
    run.require("fixture_from_this_repo", module.FIXTURE == REPO / FIXTURE, str(module.FIXTURE))
    return module, guard, scratch


class RecordingResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.outcomes = {}

    def _mark(self, test, outcome):
        self.outcomes[test.id()] = outcome

    def addSuccess(self, test):
        super().addSuccess(test)
        self.outcomes.setdefault(test.id(), "pass")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._mark(test, "fail")

    def addError(self, test, err):
        super().addError(test, err)
        self._mark(getattr(test, "test_case", test), "error")

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
            self._mark(test, "fail" if issubclass(err[0], test.failureException) else "error")


def run_cases(run, label, module, names):
    loader = unittest.TestLoader()
    suite = unittest.TestSuite(loader.loadTestsFromName(TEST_CLASS + ".test_" + name, module) for name in names)
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=2, resultclass=RecordingResult).run(suite)
    run.save(label + ".unittest.txt", stream.getvalue())
    outcomes = {key.rsplit(".", 1)[-1][5:]: value for key, value in result.outcomes.items()}
    summary = {"tests_run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
               "skipped": len(result.skipped), "expected_failures": len(result.expectedFailures),
               "unexpected_successes": len(result.unexpectedSuccesses), "successful": result.wasSuccessful(),
               "test_ids": sorted(result.outcomes), "outcomes": outcomes}
    run.receipt.setdefault("unittest", {})[label] = summary
    return summary


def all_pass(summary, count):
    return (summary["successful"] and summary["tests_run"] == count and summary["failures"] == 0
            and summary["errors"] == 0 and summary["skipped"] == 0 and summary["expected_failures"] == 0
            and summary["unexpected_successes"] == 0)


def check_focused(run):
    start(run)
    descriptors = open_descriptors()
    module, guard, scratch = prepare_in_process(run)
    try:
        summary = run_cases(run, "focused", module, REQUIRED_CASES)
    finally:
        guard.stop()
    declared = sorted(name[5:] for name in dir(getattr(module, TEST_CLASS)) if name.startswith("test_"))
    run.require("frozen_case_set_exact", declared == sorted(REQUIRED_CASES), declared)
    run.require("all_named_cases_ran", summary["tests_run"] == len(REQUIRED_CASES), summary["tests_run"])
    run.require("all_named_cases_pass", summary["outcomes"] == {name: "pass" for name in REQUIRED_CASES},
                summary["outcomes"])
    run.require("no_failures_errors_or_skips", all_pass(summary, len(REQUIRED_CASES)), summary)
    observations = dict(module.OBSERVATIONS)
    run.receipt["descriptor_observations"] = observations
    run.require("descriptor_evidence_for_every_case", sorted(observations) == sorted(REQUIRED_CASES),
                sorted(observations))
    unclosed = {case: [row.get("leaked", row.get("unclosed_directories")) for row in rows]
                for case, rows in observations.items()}
    run.require("every_acquired_directory_closed",
                all(value == [] for rows in unclosed.values() for value in rows), unclosed)
    after = open_descriptors()
    run.receipt["process_descriptors"] = {"before": descriptors, "after": after}
    run.require("no_process_descriptor_growth", descriptors is None or after <= descriptors,
                {"before": descriptors, "after": after})
    run.require("offline_guard_untouched", guard.hits == [], guard.hits)
    modules = loaded_modules()
    run.receipt["loaded_modules"] = modules
    outside = sorted(name for name, path in modules.items() if not Path(path).is_relative_to(REPO))
    run.require("application_modules_from_this_repo", bool(modules) and not outside, outside)
    network = sorted(name for name in NETWORK_MODULES if name in sys.modules)
    run.require("network_capable_application_modules_not_loaded", not network, network)
    leftovers = sorted(str(path) for path in scratch.iterdir())
    run.require("temporary_report_roots_released", not leftovers, leftovers)


def base_write_private(run, safe_output, base_source):
    """The base finalizer, compiled in memory and bound to the live module globals."""
    current = (REPO / SOURCE).read_text(encoding="utf-8").splitlines()
    old = base_source.splitlines()
    finally_line = old.index("    finally:") if "    finally:" in old else None
    changed = [(tag, i1, i2, j1, j2) for tag, i1, i2, j1, j2 in
               difflib.SequenceMatcher(a=old, b=current, autojunk=False).get_opcodes() if tag != "equal"]
    run.receipt["base_to_candidate_opcodes"] = changed
    run.require("base_differs_only_in_finalizer", finally_line is not None and bool(changed)
                and all(i1 > finally_line for _, i1, _, _, _ in changed), changed)
    run.require("base_finalizer_is_sequential_unlink_then_close",
                finally_line is not None and [line.strip() for line in old[finally_line + 1:]] ==
                ["if temporary is not None:", "os.unlink(temporary, dir_fd=directory)", "os.close(directory)"],
                old[finally_line:] if finally_line is not None else None)
    namespace = {}
    exec(compile(base_source, "base:" + SOURCE, "exec"), namespace)
    old_function = namespace["write_private"]
    mutant = types.FunctionType(old_function.__code__, safe_output.__dict__, "write_private",
                                old_function.__defaults__, old_function.__closure__)
    mutant.__kwdefaults__ = old_function.__kwdefaults__
    return mutant


PREFLIGHT_CONTROLS = (
    # name, manifest edit, check, executable override, expected first failing preflight check
    ("valid_manifest", None, "focused", None, None),
    ("remote_mismatch", ("remote", "/fc7-synthetic/nonexistent-remote"), "focused", None,
     "manifest_remote_is_local_origin"),
    ("repo_mismatch", ("repo", "/fc7-synthetic/elsewhere/repo"), "focused", None, "manifest_repo_is_this_repo"),
    ("base_commit_absent", ("head", "derived"), "focused", None, "manifest_base_commit_present"),
    ("runtime_missing", ("runtime", None), "focused", None, "manifest_runtime_declared"),
    ("credential_python_missing", ("runtime.credential_scan_python3", "/fc7-synthetic/bin/python3"),
     "focused", None, "runtime_credential_scan_python3_executable"),
    ("scanner_missing", ("runtime.detect_secrets", "/fc7-synthetic/bin/detect-secrets"), "focused", None,
     "runtime_detect_secrets_executable"),
    ("wrong_interpreter_for_credentials", None, "credentials", "repo-venv",
     "interpreter_is_declared_credential_scan_python3"),
)


def preflight_controls(run):
    """In-process: synthetic manifests through the same preflight; work only after success."""
    original = json.loads(MANIFEST.read_text())
    results = {}
    for name, edit, check, executable, expected in PREFLIGHT_CONTROLS:
        manifest = json.loads(json.dumps(original))
        if edit:
            key, value = edit
            if value == "derived":  # a well-formed commit id that is not in this repository
                value = hashlib.sha1(b"fc7-synthetic-absent-base").hexdigest()
            owner = manifest
            if key.startswith("runtime."):
                owner, key = manifest["runtime"], key.split(".", 1)[1]
            if value is None:
                owner.pop(key, None)
            else:
                owner[key] = value
        root = run.directory / "preflight-controls" / name
        root.mkdir(parents=True, mode=0o700)
        path = root / MANIFEST_NAME
        path.write_text(canonical(manifest))
        sub = Run(check, root / "out", quiet=True)
        worked, failed = [], None
        chosen = str(REPO / ".venv" / "bin" / "python3") if executable == "repo-venv" else sys.executable
        try:
            preflight(sub, REPO, path, check, chosen)
            worked.append(name)  # stands in for the mode's work
            sub.begin_work("control-work")
        except Preflight as caught:
            failed = str(caught)
        sub.require("control_completed", True)
        sub.finish()
        results[name] = {"expected_failure": expected, "failed_check": failed, "work_ran": bool(worked),
                         "work_started": sub.receipt["work_started"], "receipt": str(sub.directory / "receipt.json"),
                         "commands": [row["label"] for row in sub.receipt.get("commands", [])]}
        if expected is None:
            run.require("preflight_control_" + name + "_passes_and_works", failed is None and worked, results[name])
        else:
            run.require("preflight_control_" + name + "_stops_before_work",
                        failed == expected and not worked and not sub.receipt["work_started"], results[name])
    run.receipt["preflight_controls"] = results


def no_work_child_control(run):
    """End to end: a derived copy with a tampered remote; the child runner must fail
    its preflight and start no test, scanner or checker work."""
    parent = run.directory / "no-work-control" / "fc7-tampered-root"
    copy = parent / "repo"
    shutil.copytree(REPO, copy, symlinks=True, ignore=COPY_IGNORE)
    manifest = json.loads(MANIFEST.read_text())
    manifest.update({"repo": str(copy), "remote": "/fc7-synthetic/nonexistent-remote",
                     "derived_from": {"path": str(MANIFEST), "sha256": sha_file(MANIFEST)}})
    (parent / MANIFEST_NAME).write_text(canonical(manifest))
    root = run.directory / "no-work-control" / "out"
    record, _, _ = run.command("no-work-child-focused", [sys.executable, "-B", str(copy / RUNNER), "--check",
                                                         "focused", "--output-root", str(root)], cwd=copy)
    found = sorted(root.glob("run-*/receipt.json"))
    child = json.loads(found[0].read_text()) if len(found) == 1 else {}
    failed = [row["name"] for row in child.get("checks", []) if not row["ok"]]
    labels = [row["label"] for row in child.get("commands", [])]
    summary = {"exit": record["exit"], "result": child.get("result"), "failed_checks": failed,
               "work_started": child.get("work_started"), "unittest": "unittest" in child,
               "commands": labels, "receipt": str(found[0]) if found else None}
    run.receipt["no_work_child_control"] = summary
    run.require("no_work_child_fails_preflight_before_work", record["exit"] == 1 and child.get("result") == "FAIL"
                and failed[:1] == ["derived_manifest_matches_original"] and "preflight_aborted" in failed
                and child.get("work_started") is False and "unittest" not in child
                and all(label.startswith("git-") for label in labels), summary)


def check_negative(run):
    manifest, record = start(run)
    on_disk = sha_file(REPO / SOURCE)
    code, base_source = git(run, "show-base-source-negative", "show", manifest["head"] + ":" + SOURCE)
    run.must("base_source_read", code == 0 and bool(base_source), code)
    preflight_controls(run)
    no_work_child_control(run)
    module, guard, _ = prepare_in_process(run)
    try:
        safe_output = importlib.import_module("campaign_tool.records.gates.safe_output")
        original = safe_output.write_private
        mutant = base_write_private(run, safe_output, base_source)
        module.OBSERVATIONS.clear()
        with mock.patch.object(safe_output, "write_private", mutant):
            broken = run_cases(run, "mutant", module, MUTANT_CASES)
        mutant_rows = {case: module.OBSERVATIONS.get(case, []) for case in MUTANT_CASES}
        run.receipt["mutant_observations"] = mutant_rows
        run.require("mutant_detected_as_assertion_failure",
                    broken["outcomes"] == {case: "fail" for case in MUTANT_CASES}
                    and broken["errors"] == 0 and broken["failures"] >= len(MUTANT_CASES), broken)
        rows = [row for case in MUTANT_CASES for row in mutant_rows[case]]
        run.require("mutant_still_raises_cleanup_failure", len(rows) == 3 and all(
            row["raised"] == repr(OSError("synthetic cleanup failure")) for row in rows), rows)
        run.require("mutant_leaves_owned_fake_directory_unclosed", len(rows) == 3 and all(
            row["unclosed_directories"] == [1002] for row in rows), rows)
        run.require("finalizer_restored", safe_output.write_private is original)
        module.OBSERVATIONS.clear()
        restored = run_cases(run, "restored", module, ("write_or_fsync_failure_with_successful_cleanup",)
                             + MUTANT_CASES)
        run.receipt["restored_observations"] = dict(module.OBSERVATIONS)
        run.require("restored_cleanup_control_and_cases_pass", all_pass(restored, 1 + len(MUTANT_CASES)), restored)
    finally:
        guard.stop()
    run.require("production_file_unchanged_on_disk", sha_file(REPO / SOURCE) == on_disk, on_disk)
    run.require("offline_guard_untouched", guard.hits == [], guard.hits)


# ---------------------------------------------------------------- compatibility

VERBOSE_LINE = re.compile(r"^(test_\w+) \(([\w.]+)\)(?: \(.*\))? \.\.\. (.*)$", re.M)


def parse_unittest(stderr):
    text = stderr.decode(errors="replace")
    ran = re.findall(r"^Ran (\d+) tests? in ", text, re.M)
    final = re.findall(r"^(OK|FAILED)(?: \(([^)]*)\))?\s*$", text, re.M)
    rows = {}
    for name, dotted_name, outcome in VERBOSE_LINE.findall(text):
        test_id = dotted_name if dotted_name.endswith("." + name) else dotted_name + "." + name
        rows[test_id] = outcome.strip()
    return {"tests_run": int(ran[-1]) if ran else 0, "final": final[-1][0] if final else None,
            "detail": final[-1][1] if final else None, "test_ids": sorted(rows), "outcomes": rows}


def declared_tests(path, class_name):
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return sorted(item.name for item in node.body
                          if isinstance(item, ast.FunctionDef) and item.name.startswith("test_"))
    return []


def guarded_child(run, label, repo, mode):
    """Launch this runner's guarded entry for one repository; return its record."""
    base = run.directory / label
    scratch, records = base / "tmp", base / "guard-records"
    for path in (base, scratch, records):
        path.mkdir(mode=0o700)
    spec = {"repo": str(repo), "scratch": str(scratch.resolve()), "records": str(records),
            "result": str(base / "guarded-result.json"), "selector": COMPATIBILITY, "mode": mode}
    spec_path = base / "guarded-spec.json"
    spec_path.write_text(canonical(spec))
    environment = dict(os.environ, TMPDIR=str(scratch))
    record, _, stderr = run.command(label, [sys.executable, "-B", str(RUNNER_FILE), "--internal-guarded-run",
                                            str(spec_path)], cwd=repo, env=environment)
    result_path = Path(spec["result"])
    result = json.loads(result_path.read_text()) if result_path.is_file() else {}
    children = []
    for index, row in enumerate(result.get("admitted_children", []), 1):
        path = Path(row["spec"]["record"])
        children.append(json.loads(path.read_text()) if path.is_file() else {"missing_record": str(path)})
    return {"exit": record["exit"], "stderr": stderr, "result": result, "children": children,
            "result_path": str(result_path)}


def modules_within(modules, repo, admitted):
    stray = sorted(name for name, path in modules.items()
                   if not Path(path).is_relative_to(repo.resolve()) or name not in admitted)
    return stray


def check_guarded(run, label, guarded, repo, closure):
    """Shared validation of a guarded parent and its CLI children."""
    result = guarded["result"]
    run.require(label + "_guard_before_application_import",
                result.get("guard_installed_before_application_import") is True
                and result.get("preloaded_application_modules") == [], result.get("preloaded_application_modules"))
    run.require(label + "_parent_modules_in_closure_and_repo",
                bool(result.get("loaded_modules")) and not modules_within(result.get("loaded_modules", {}),
                                                                          repo, closure),
                modules_within(result.get("loaded_modules", {}), repo, closure))
    run.require(label + "_fallback_aliases_not_loaded",
                not set(FALLBACK_ALIASES) & set(result.get("loaded_modules", {})))
    for index, child in enumerate(guarded["children"], 1):
        name = f"{label}_child_{index}"
        run.require(name + "_record_present", "missing_record" not in child, child.get("missing_record"))
        run.require(name + "_guard_before_application_import",
                    child.get("guard_installed_before_application_import") is True
                    and child.get("preloaded_application_modules") == [],
                    child.get("preloaded_application_modules"))
        run.require(name + "_modules_in_closure_and_repo",
                    not modules_within(child.get("loaded_modules", {}), repo, closure),
                    modules_within(child.get("loaded_modules", {}), repo, closure))
        run.require(name + "_fallback_aliases_not_loaded",
                    not set(FALLBACK_ALIASES) & set(child.get("loaded_modules", {})))


def compatibility_side(run, label, repo, expected, closure):
    guarded = guarded_child(run, label, repo, "unittest")
    parsed = parse_unittest(guarded["stderr"])
    result = guarded["result"]
    parsed.update({"exit": guarded["exit"], "repo": str(repo), "safe_output_sha256": sha_file(repo / SOURCE),
                   "guarded_result": guarded["result_path"],
                   "admitted_children": [row["argv"][3:4] + [len(row["argv"])] for row in
                                         result.get("admitted_children", [])],
                   "child_exits": [child.get("exit") for child in guarded["children"]]})
    run.require(label + "_exit_zero", guarded["exit"] == 0 and result.get("exit") == 0,
                {"process": guarded["exit"], "recorded": result.get("exit")})
    run.require(label + "_selected_exact_declared_tests", parsed["test_ids"] == expected
                and parsed["tests_run"] == len(expected), {"ids": parsed["test_ids"], "ran": parsed["tests_run"]})
    run.require(label + "_ok_without_skips", parsed["final"] == "OK" and not parsed["detail"]
                and all(value == "ok" for value in parsed["outcomes"].values()), parsed["outcomes"])
    run.require(label + "_no_guard_hits_or_refused_children", result.get("guard_hits") == []
                and result.get("refused_children") == [],
                {"hits": result.get("guard_hits"), "refused": result.get("refused_children")})
    commands = [row["argv"][3] for row in result.get("admitted_children", [])]
    run.require(label + "_exactly_the_four_admitted_cli_children", sorted(commands) == sorted(CLI_COMMANDS),
                commands)
    run.require(label + "_cli_children_pass_unguarded_nothing",
                len(guarded["children"]) == len(CLI_COMMANDS)
                and all(child.get("exit") == 0 and child.get("guard_hits") == [] and child.get("probe") is False
                        for child in guarded["children"]),
                [(child.get("arguments", [None])[:1], child.get("exit"), child.get("guard_hits"))
                 for child in guarded["children"]])
    check_guarded(run, label, guarded, repo, closure)
    parsed["children"] = guarded["children"]
    parsed["parent"] = {key: result.get(key) for key in ("pid", "executable", "sys_path_head", "tempdir",
                                                         "preloaded_application_modules", "loaded_modules")}
    return parsed


def compatibility_controls(run, closure):
    guarded = guarded_child(run, "guard-controls", REPO, "controls")
    result = guarded["result"]
    expected = {"network_create_connection", "network_getaddrinfo", "network_urlopen", "network_http_connect",
                "database_outside_root", "os_system", "child_git", "child_shell",
                "child_unadmitted_records_command", "child_path_outside_root", "model_import", "mail_import"}

    def verdict(probes):
        refused = {row["probe"] for row in probes if row.get("outcome") == "refused" and row.get("hits_added") == 1}
        positive = [row for row in probes if row["probe"] == "database_memory_positive"]
        return refused == expected and len(probes) == len(expected) + 1 and positive[0]["outcome"] == "allowed"

    probes = result.get("probes", [])
    run.require("guard_controls_parent_refuses_every_forbidden_probe", guarded["exit"] == 0 and verdict(probes),
                probes)
    run.require("guard_controls_model_and_mail_modules_not_loaded",
                all(row.get("loaded_after") is False for row in probes if row["probe"].endswith("_import")), probes)
    refusals = {row["reason"] for row in result.get("refused_children", [])}
    run.require("guard_controls_child_refusal_reasons",
                refusals == {"not_the_records_cli_under_this_interpreter", "shell_executable_cwd_or_env_override",
                             "command_not_admitted", "path_outside_temporary_root"}, sorted(refusals))
    children = guarded["children"]
    child_probes = children[0].get("probes", []) if len(children) == 1 else []
    run.require("guard_controls_propagated_to_cli_child",
                len(children) == 1 and children[0].get("probe") is True and children[0].get("exit") == 0
                and result.get("child_probe_exit") == 0 and verdict(child_probes), children)
    check_guarded(run, "guard_controls", guarded, REPO, closure)
    run.receipt["guard_controls"] = {"parent_probes": probes, "refused_children": result.get("refused_children"),
                                     "child": children, "result": guarded["result_path"]}


CLOSURE_CONTROLS = (
    # name, line appended to a synthetic copy of the selector file (never imported), expected failing check
    ("unadmitted_spawn_site", "subprocess.Popen([\"fc7-synthetic\"])", "_closure_spawn_sites_admitted"),
    ("blocked_mail_module", "import smtplib", "_closure_has_no_blocked_module"),
    ("alternate_process_api", "import pty", "_closure_has_no_alternate_process_api"),
    ("unadmitted_dynamic_import", "importlib.import_module(\"fc7_synthetic\")", "_closure_dynamic_imports_admitted"),
    ("unadmitted_external", "import fc7_synthetic_package", "_closure_external_admitted"),
)


def closure_controls(run):
    """Static closure validation must reject inert synthetic closures before any launch."""
    copy = run.directory / "closure-controls" / "repo"
    shutil.copytree(REPO, copy, symlinks=True, ignore=COPY_IGNORE)
    original = (REPO / COMPATIBILITY_FILE).read_text(encoding="utf-8")
    results = {}
    for name, line, expected in CLOSURE_CONTROLS:
        (copy / COMPATIBILITY_FILE).write_text(original + "\n\ndef fc7_synthetic_never_called():\n    "
                                               + line + "\n", encoding="utf-8")
        sub = Run("compatibility", run.directory / "closure-controls" / name, quiet=True)
        failed = None
        try:
            compatibility_closure(sub, copy, name)
        except Preflight as caught:
            failed = str(caught)
        sub.require("control_completed", True)
        sub.finish()
        results[name] = {"expected_failure": name + expected, "failed_check": failed,
                         "commands": [row["label"] for row in sub.receipt.get("commands", [])],
                         "receipt": str(sub.directory / "receipt.json")}
        run.require("closure_control_" + name + "_rejected_before_launch",
                    failed == name + expected and not results[name]["commands"], results[name])
    (copy / COMPATIBILITY_FILE).write_text(original, encoding="utf-8")
    sub = Run("compatibility", run.directory / "closure-controls" / "unchanged_positive", quiet=True)
    try:
        compatibility_closure(sub, copy, "unchanged_positive")
        failed = None
    except Preflight as caught:
        failed = str(caught)
    sub.require("control_completed", True)
    sub.finish()
    results["unchanged_positive"] = {"failed_check": failed}
    run.require("closure_control_unchanged_positive_admitted", failed is None, failed)
    run.receipt["closure_controls"] = results


def check_compatibility(run):
    manifest, record = start(run)
    run.must("compatibility_in_git_checkout", (REPO / ".git").exists())
    expected = sorted(COMPATIBILITY + "." + name for name in
                      declared_tests(REPO / COMPATIBILITY_FILE, COMPATIBILITY_CLASS))
    run.must("selector_declares_tests", bool(expected), expected)
    code, base_source = git(run, "show-base-source-compat", "show", manifest["head"] + ":" + SOURCE)
    run.must("base_source_read", code == 0 and bool(base_source), code)
    copy = run.directory / "base-copy" / "repo"
    shutil.copytree(REPO, copy, symlinks=True, ignore=COPY_IGNORE)
    (copy / SOURCE).write_bytes(base_source.encode())
    run.must("base_copy_selector_unchanged", sha_file(copy / COMPATIBILITY_FILE) == sha_file(REPO / COMPATIBILITY_FILE))
    closure_before = compatibility_closure(run, copy, "before")
    closure_after = compatibility_closure(run, REPO, "after")
    closure_controls(run)
    run.begin_work("guarded compatibility selector")
    compatibility_controls(run, closure_after)
    before = compatibility_side(run, "compatibility-before", copy, expected, closure_before)
    after = compatibility_side(run, "compatibility-after", REPO, expected, closure_after)
    run.receipt["compatibility"] = {"selector": COMPATIBILITY, "expected_ids": expected,
                                    "before": before, "after": after}
    run.require("before_ran_base_source", before["safe_output_sha256"] == sha_bytes(base_source.encode()),
                before["safe_output_sha256"])
    run.require("after_ran_candidate_source", after["safe_output_sha256"] == sha_file(REPO / SOURCE),
                after["safe_output_sha256"])
    run.receipt["full_discovery"] = "operator-run; scoped selector success is not full-suite success"


# ---------------------------------------------------------------- portability / credentials / public tree

def check_portability(run):
    start(run)
    scanned = {name: sorted(token for token in HOST_TOKENS if token in (REPO / name).read_text())
               for name in (TEST, RUNNER)}
    run.must("no_host_paths_in_new_test_or_runner", not any(scanned.values()), scanned)
    run.begin_work("two-prefix focused runs")
    parent = run.directory / "second-prefix" / "fc7-alternate-root"
    copy = parent / "repo"
    shutil.copytree(REPO, copy, symlinks=True, ignore=COPY_IGNORE)
    manifest = json.loads(MANIFEST.read_text())
    manifest.update({"repo": str(copy), "derived_from": {"path": str(MANIFEST), "sha256": sha_file(MANIFEST)}})
    (parent / MANIFEST_NAME).write_text(canonical(manifest))
    copied = {name: sha_file(copy / name) for name in ALLOWED_CHANGES}
    run.receipt["copy"] = {"path": str(copy), "file_sha256": copied}
    run.require("copy_bytes_match", copied == {name: sha_file(REPO / name) for name in ALLOWED_CHANGES}, copied)
    receipts = {}
    for label, repo in (("first-prefix-focused", REPO), ("second-prefix-focused", copy)):
        root = run.directory / label
        record, _, _ = run.command(label, [sys.executable, "-B", str(repo / RUNNER), "--check", "focused",
                                           "--output-root", str(root)], cwd=repo)
        run.require(label + "_exit_zero", record["exit"] == 0, record["exit"])
        found = sorted(root.glob("run-*/receipt.json"))
        if not run.require(label + "_receipt_present", len(found) == 1, [str(path) for path in found]):
            return
        child = json.loads(found[0].read_text())
        receipts[label] = child
        modules = child.get("loaded_modules") or {}
        stray = sorted(name for name, path in modules.items() if not Path(path).is_relative_to(repo.resolve()))
        run.receipt.setdefault("child_receipts", {})[label] = {
            "path": str(found[0]), "sha256": sha_file(found[0]), "result": child.get("result"),
            "repo": child.get("repo"), "loaded_modules": modules,
            "derived_copy_hashes": child.get("derived_copy_hashes")}
        run.require(label + "_passed", child.get("result") == "PASS_REVIEW_REQUIRED", child.get("result"))
        run.require(label + "_ran_from_its_prefix", child.get("repo") == str(repo.resolve()), child.get("repo"))
        run.require(label + "_loaded_only_its_modules", bool(modules) and not stray, stray)
    run.require("second_prefix_verified_as_derived_copy",
                bool(receipts["second-prefix-focused"].get("derived_copy_hashes")))
    first, second = (receipts[label]["unittest"]["focused"]["outcomes"] for label in receipts)
    run.require("same_case_outcomes_across_prefixes", first == second, {"first": first, "second": second})


def check_credentials(run):
    manifest, _ = start(run)
    scanner = manifest["runtime"]["detect_secrets"]
    run.begin_work("credential scan")
    record, stdout, _ = run.command("detect-secrets", [scanner, "scan", "--all-files", "--exclude-files",
                                                       "^.git/", "--no-verify"], cwd=REPO)
    report = run.save("secret-scan.json", stdout)
    run.require("scan_exit_zero", record["exit"] == 0, record["exit"])
    try:
        results = json.loads(stdout)["results"]
        readable = isinstance(results, dict)
    except (ValueError, KeyError, TypeError):
        results, readable = {}, False
    run.require("scan_report_readable", readable)
    hits = {name: len(rows) for name, rows in (results or {}).items()}
    run.receipt["raw_hits_by_file"] = hits
    run.require("no_raw_hits_in_fc7_files", not any(name in hits for name in ALLOWED_CHANGES),
                {name: hits.get(name) for name in ALLOWED_CHANGES})
    record, _, _ = run.command("check-secret-report", [sys.executable, "-B",
                                                       str(REPO / "tools" / "check_secret_report.py"),
                                                       str(report)], cwd=REPO)
    run.require("unchanged_checker_exit_zero", record["exit"] == 0, record["exit"])


def check_public_tree(run):
    start(run)
    run.begin_work("public tree check")
    record, _, _ = run.command("public-tree", [sys.executable, "-B", str(REPO / "tools" / "check_public_tree.py")],
                               cwd=REPO)
    run.require("public_tree_exit_zero", record["exit"] == 0, record["exit"])


MODES = {"focused": check_focused, "compatibility": check_compatibility, "negative": check_negative,
         "portability": check_portability, "credentials": check_credentials, "public-tree": check_public_tree}


def main():
    # Internal guarded entries, used only by this runner's own child processes.
    if len(sys.argv) >= 4 and sys.argv[1] == "--internal-guarded-cli" and sys.argv[3] == "--":
        return guarded_cli_main(sys.argv[2], sys.argv[4:])
    if len(sys.argv) == 3 and sys.argv[1] == "--internal-guarded-run":
        return guarded_run_main(sys.argv[2])
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", required=True, choices=CHECKS)
    parser.add_argument("--output-root", required=True)
    args = parser.parse_args()
    run = Run(args.check, args.output_root)
    try:
        MODES[args.check](run)
    except Preflight as caught:  # a failed preflight stops the mode before any work
        run.require("preflight_aborted", False, str(caught))
    except Exception as caught:  # recorded as a failed check, never as success
        run.require("mode_completed_without_exception", False, type(caught).__name__ + ": " + str(caught))
    return run.finish()


if __name__ == "__main__":
    sys.exit(main())
