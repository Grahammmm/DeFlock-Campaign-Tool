"""Conservative PR-only filtering; uncertain input always runs the full suite.

Only prose directly in docs/ (or docs/ci/) is exempt. Runbooks, provenance
JSON, fixtures, README/build metadata and every unknown path remain relevant.
No workflow-level path filter is used: required check contexts always report.
"""
from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess


def needs_tests(event_name: str, head_ref: str, paths: list[str] | None) -> bool:
    if event_name != "pull_request" or not head_ref or head_ref == "main" or head_ref.startswith("batch/"):
        return True
    if not paths:
        return True
    for path in paths:
        item = PurePosixPath(path)
        if (item.suffix != ".md" or str(item.parent) not in ("docs", "docs/ci")
                or item.is_absolute() or ".." in item.parts):
            return True
    return False


def changed_paths(event: dict) -> list[str] | None:
    try:
        pr = event["pull_request"]
        base, head = pr["base"]["sha"], pr["head"]["sha"]
        if not all(isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{40}", sha)
                   for sha in (base, head)):
            return None
        # No API pagination/300-file limit; include both sides of renames as
        # delete+add, so renaming code into a prose path cannot hide the code.
        result = subprocess.run(
            ["git", "diff", "--no-renames", "--name-only", "-z", f"{base}...{head}", "--"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
        )
        return [part.decode("utf-8", errors="strict") for part in result.stdout.split(b"\0") if part]
    except (KeyError, TypeError, UnicodeError, OSError, subprocess.SubprocessError):
        return None


def main() -> int:
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    head_ref = os.environ.get("GITHUB_HEAD_REF", "")
    paths = None
    if event_name == "pull_request":
        try:
            event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
            paths = changed_paths(event)
        except (KeyError, ValueError, OSError):
            pass
    required = needs_tests(event_name, head_ref, paths)
    print(json.dumps({"run_tests": required, "changed_paths_known": paths is not None,
                      "path_count": len(paths) if paths is not None else None}))
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write(f"run-tests={str(required).lower()}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
