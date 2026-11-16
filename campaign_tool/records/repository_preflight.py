"""Read-only ancestor ``.git`` marker diagnosis for the records engine and wrapper.

``find_marker`` walks a canonical absolute path and every parent to ``/`` and reports
the nearest ``.git`` entry as a depth (0 = the supplied directory, 1 = its parent) and
the fixed basename only. Nothing is read, followed, removed or chmod-ed. Output never
carries a path, an ancestor name or any private content.

Two small CLI modes:
  probe --project PATH                 run in the execution namespace (container)
  check --target-config F --timeout S  host side: probe each distinct records project
"""
from __future__ import annotations

import argparse
import errno
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

MARKER = ".git"
SCHEMA = "records-repository-preflight-v1"
STAGE_MODULE = "campaign_tool.records.run_stages"
PROBE_MODULE = "campaign_tool.records.repository_preflight"
MAX_OUTPUT_BYTES = 512
EXIT_OK, EXIT_MARKER, EXIT_FAILED = 0, 1, 2
CODE_MARKER = "PROJECT_INSIDE_REPOSITORY"
CODE_FAILED = "PROJECT_REPOSITORY_CHECK_FAILED"


class PreflightError(Exception):
    """Inspection was uncertain; never read as 'no repository'."""


def find_marker(path):
    """Depth of the nearest ancestor ``.git`` marker, or None when the chain is clean."""
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise PreflightError("noncanonical_path")
    for depth, directory in enumerate((path, *path.parents)):
        try:
            os.lstat(directory / MARKER)
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError as exc:
            if exc.errno in (errno.ENOENT, errno.ENOTDIR):
                continue
            raise PreflightError("inspection_failed") from None
        return depth
    return None


def probe_result(project):
    try:
        depth = find_marker(project)
    except PreflightError:
        return None
    if depth is None:
        return {"schema": SCHEMA, "status": "clean", "depth": None, "marker": MARKER}
    return {"schema": SCHEMA, "status": "marker", "depth": depth, "marker": MARKER}


def _probe_main(args):
    result = probe_result(args.project)
    if result is None:
        print(CODE_FAILED, file=sys.stderr)
        return EXIT_FAILED
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return EXIT_OK


# --- host driver ---------------------------------------------------------------------

def probe_contexts(config):
    """Distinct (exec prefix, project) pairs from enabled records stages; [] = not applicable.

    Raises PreflightError for a records invocation that cannot be read unambiguously.
    """
    from .run_orchestrator import WORK_STAGES
    seen, contexts = set(), []
    for entry in config["stages"]:
        if not entry.get("enabled", True):
            continue
        argv = entry["argv"]
        if STAGE_MODULE not in argv:
            continue
        at = argv.index(STAGE_MODULE)
        if (argv.count(STAGE_MODULE) != 1 or at < 2 or argv[at - 1] != "-m"
                or at + 1 >= len(argv) or argv[at + 1] not in WORK_STAGES):
            raise PreflightError("malformed_records_stage")
        tail = argv[at + 2:]
        if tail.count("--project") != 1 or tail.index("--project") + 1 >= len(tail):
            raise PreflightError("ambiguous_project")
        project = tail[tail.index("--project") + 1]
        if not os.path.isabs(project) or project.startswith("--"):
            raise PreflightError("ambiguous_project")
        key = (tuple(argv[:at - 1]), project)
        if key not in seen:
            seen.add(key)
            contexts.append(key)
    return contexts


def _validated(stdout):
    if not stdout or len(stdout) > MAX_OUTPUT_BYTES:
        raise PreflightError("bad_output")
    try:
        text = stdout.decode("ascii")
        body = json.loads(text)
    except ValueError:
        raise PreflightError("bad_output") from None
    if text != json.dumps(body, sort_keys=True, separators=(",", ":")) + "\n":
        raise PreflightError("bad_output")
    if not isinstance(body, dict) or set(body) != {"schema", "status", "depth", "marker"}:
        raise PreflightError("bad_output")
    if body["schema"] != SCHEMA or body["marker"] != MARKER:
        raise PreflightError("bad_output")
    if body["status"] == "clean" and body["depth"] is None:
        return None
    depth = body["depth"]
    if body["status"] == "marker" and type(depth) is int and 0 <= depth <= 4096:
        return depth
    raise PreflightError("bad_output")


def run_probe(prefix, project, deadline):
    """One bounded probe in the SAME execution context. Returns depth or None (clean)."""
    left = deadline - time.monotonic()
    if left <= 0:
        raise PreflightError("timeout")
    argv = [*prefix, "-m", PROBE_MODULE, "probe", "--project", project]
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        raise PreflightError("probe_unavailable") from None
    try:
        stdout, _ = proc.communicate(timeout=left)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        proc.communicate()
        raise PreflightError("timeout") from None
    if proc.returncode != 0:
        raise PreflightError("probe_failed")
    return _validated(stdout)


def check_target(config, timeout):
    """Return ('not-applicable'|'clean', None) or ('marker', depth); raise PreflightError."""
    contexts = probe_contexts(config)
    if not contexts:
        return "not-applicable", None
    deadline = time.monotonic() + timeout
    nearest = None
    for prefix, project in contexts:
        depth = run_probe(list(prefix), project, deadline)
        if depth is not None and (nearest is None or depth < nearest):
            nearest = depth
    return ("marker", nearest) if nearest is not None else ("clean", None)


def _check_main(args):
    from .run_orchestrator import TargetConfigError, load_target_config
    try:
        if args.timeout < 1:
            raise PreflightError("no_budget")
        config = load_target_config(args.target_config)
        status, depth = check_target(config, args.timeout)
    except (PreflightError, TargetConfigError):
        print(CODE_FAILED)
        return EXIT_FAILED
    if status == "marker":
        print(f"{CODE_MARKER} depth={depth} basename={MARKER}")
        return EXIT_MARKER
    print(status)
    return EXIT_OK


def main(argv=None):
    parser = argparse.ArgumentParser(prog="repository_preflight")
    sub = parser.add_subparsers(dest="mode", required=True)
    probe = sub.add_parser("probe")
    probe.add_argument("--project", required=True)
    probe.set_defaults(func=_probe_main)
    check = sub.add_parser("check")
    check.add_argument("--target-config", required=True)
    check.add_argument("--timeout", type=int, required=True)
    check.set_defaults(func=_check_main)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
