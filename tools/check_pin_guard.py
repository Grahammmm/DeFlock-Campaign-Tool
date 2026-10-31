#!/usr/bin/env python3
"""PR pin guard: a PR that changes a hash-pinned file must update its pin.

Some repo tests freeze the exact sha256 of source files: the comprehension
contract import closure, and the T4B checker's historical pins (verified
against their frozen copies; the live files may move on).
The comprehension contract is the single current pin set. If a PR edits such a
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
T4B_FROZEN = "tests/fixtures/t4b_s5/pinned-source"
TOKEN = re.compile(rb"(?<![a-f0-9])(?:[a-f0-9]{64}|[a-f0-9]{40})(?![a-f0-9])")


def _restorer(anchors):
    """Same token restoration as restore_fixture_hashes() in the contract test."""
    return lambda raw: TOKEN.sub(lambda m: anchors.get(m[0].decode(), m[0].decode()).encode(), raw)


def _comprehension_pins(repo):
    """Mirror tests/test_comprehension_contract.py: raw and materialized pins."""
    fix = repo / COMPREHENSION
    restore = _restorer(ast.literal_eval((fix / "hash-anchors.py").read_text(encoding="utf-8")))
    pins = json.loads(restore((fix / "source-pins.json").read_bytes()))
    out = [(path, digest, None) for path, digest in pins["files"].items()]
    out += [(f"{COMPREHENSION}/{name}", digest, None) for name, digest in pins["fixture_files"].items()]
    out += [(f"{COMPREHENSION}/{name}", digest, restore)
            for name, digest in pins["materialized_fixture_files"].items()]
    return out


def _t4b_pins(repo):
    """Module-level PINS literal of tools/t4b_test_apply_check.py (not imported)."""
    tree = ast.parse((repo / "tools/t4b_test_apply_check.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "PINS" for t in node.targets):
            return [(f"{T4B_FROZEN}/{path}.txt", digest, None)
                    for path, digest in ast.literal_eval(node.value).items()]
    raise ValueError("PINS not found in tools/t4b_test_apply_check.py")


# name -> (files that hold the pins, loader returning [(repo path, sha256, transform or None)]).
# transform maps the file bytes before hashing (None = raw bytes).
REGISTRIES = {
    "comprehension contract (tests/fixtures/comprehension/source-pins.json + hash-anchors.py)": (
        (f"{COMPREHENSION}/source-pins.json", f"{COMPREHENSION}/hash-anchors.py"),
        _comprehension_pins,
    ),
    f"T4B checker historical pins (PINS in tools/t4b_test_apply_check.py vs {T4B_FROZEN}/)": (
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
        if changed is not None and not any(h in changed for h in holders):
            pins = [pin for pin in pins if pin[0] in changed]
        for path, digest, transform in sorted(pins, key=lambda pin: (pin[0], pin[2] is not None)):
            target = repo / path
            if not target.is_file():
                if (name, path, "pinned file missing") not in found:
                    found.append((name, path, "pinned file missing"))
                continue
            data = target.read_bytes()
            if hashlib.sha256(transform(data) if transform else data).hexdigest() != digest:
                found.append((name, path, "materialized content (hash anchors restored) differs from its pin"
                              if transform else "content differs from its pin"))
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
