#!/usr/bin/env python3
"""PR pin guard: a PR that changes a hash-pinned file must update its pin.

Some repo tests freeze the exact sha256 of source files (the comprehension
contract import closure, the T4B checker contract). If a PR edits such a
file without re-pinning it, the failure only shows up inside the long
offline suite, under the same check name as any other failure there. When
main is already red on that check, the new failure is masked, the PR
merges, and main breaks again (DeFlock #342 -> run_stages.py, 2026-10-28).

This guard is its own small PR check. It looks only at the files the PR
changes (first parent of the CI merge commit -> HEAD) and fails that PR,
naming the file and the pin registry to update. If a PR edits a pin
registry itself, every pin in that registry is verified. It reads files
and hashes them; it imports and executes nothing from the repo.

Usage:
  python3 tools/check_pin_guard.py              # changed files vs HEAD^1
  python3 tools/check_pin_guard.py --base REV   # changed files vs REV
  python3 tools/check_pin_guard.py --all        # verify every pin

To cover a new pin registry, add a loader to REGISTRIES.
"""
import argparse
import ast
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
COMPREHENSION = "tests/fixtures/comprehension"
TOKEN = re.compile(rb"(?<![a-f0-9])(?:[a-f0-9]{64}|[a-f0-9]{40})(?![a-f0-9])")


def _comprehension_pins(repo):
    """Mirror tests/test_comprehension_contract.py: tokens -> real hashes."""
    fix = repo / COMPREHENSION
    anchors = ast.literal_eval((fix / "hash-anchors.py").read_text(encoding="utf-8"))
    raw = TOKEN.sub(lambda m: anchors.get(m[0].decode(), m[0].decode()).encode(),
                    (fix / "source-pins.json").read_bytes())
    pins = json.loads(raw)
    out = dict(pins["files"])
    out.update({f"{COMPREHENSION}/{name}": digest for name, digest in pins["fixture_files"].items()})
    return out


def _t4b_pins(repo):
    """Module-level PINS literal of tools/t4b_test_apply_check.py (not imported)."""
    tree = ast.parse((repo / "tools/t4b_test_apply_check.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "PINS" for t in node.targets):
            return dict(ast.literal_eval(node.value))
    raise ValueError("PINS not found in tools/t4b_test_apply_check.py")


# name -> (files that hold the pins, loader returning {repo path: sha256})
REGISTRIES = {
    "comprehension contract (tests/fixtures/comprehension/source-pins.json + hash-anchors.py)": (
        (f"{COMPREHENSION}/source-pins.json", f"{COMPREHENSION}/hash-anchors.py"),
        _comprehension_pins,
    ),
    "T4B checker (PINS in tools/t4b_test_apply_check.py)": (
        ("tools/t4b_test_apply_check.py",),
        _t4b_pins,
    ),
}


def changed_files(repo, base):
    out = subprocess.run(
        ["git", "diff", "--name-only", "--no-renames", base, "HEAD"],
        cwd=repo, check=True, capture_output=True, text=True).stdout
    return {line.strip() for line in out.splitlines() if line.strip()}


def violations(repo, changed):
    """changed=None checks every pin. Returns [(registry, path, problem)]."""
    found = []
    for name, (holders, loader) in REGISTRIES.items():
        pins = loader(repo)
        if changed is None or any(h in changed for h in holders):
            targets = sorted(pins)
        else:
            targets = sorted(p for p in pins if p in changed)
        for path in targets:
            target = repo / path
            if not target.is_file():
                found.append((name, path, "pinned file missing"))
            elif hashlib.sha256(target.read_bytes()).hexdigest() != pins[path]:
                found.append((name, path, "content differs from its pin"))
    return found


def main(argv=None):
    ap = argparse.ArgumentParser(description="Fail a PR that changes a hash-pinned file without its pin.")
    ap.add_argument("--base", default="HEAD^1", help="base revision (default HEAD^1)")
    ap.add_argument("--all", action="store_true", help="verify every pin, ignore the diff")
    args = ap.parse_args(argv)
    changed = None if args.all else changed_files(REPO, args.base)
    scope = "all pins" if changed is None else f"{len(changed)} changed file(s) vs {args.base}"
    found = violations(REPO, changed)
    if not found:
        print(f"pin guard OK ({scope})")
        return 0
    print(f"pin guard FAILED ({scope}):")
    for name, path, problem in found:
        print(f"  {path}: {problem}; update its pin in {name} in this same PR")
    print("A pinned file changed without its pin. Either revert the change or re-pin it here;"
          " never leave the re-pin for a later PR (that breaks main).")
    return 1


if __name__ == "__main__":
    sys.exit(main())
