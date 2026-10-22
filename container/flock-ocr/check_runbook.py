#!/usr/bin/env python3
"""Check the ordering rules of the owner runbook (runbook.json).

Static only: it reads JSON and reports rule violations. It runs no Docker,
SQLite, tar or lifecycle command and touches no data.

Rules:
- Writers are paused and every mount user is stopped before the snapshot; the
  snapshot, its evidence and a restore test all precede the snapshot gate; no
  build, image check, rename, create or start precedes the gate.
- Every main step after the record step names an existing rollback path.
- Each rollback path begins with every mount user stopped (stopping them
  itself when it can be entered after the replacement started), validates data
  after the last restore and before the old container starts, starts the old
  container exactly once, never starts the replacement, renames the old
  container back when entered after the rename, and re-enables writers last.
"""
import argparse
import json
import sys
from pathlib import Path

PRE_GATE = ["pause_writers", "stop_all_mount_users", "snapshot", "snapshot_evidence",
            "restore_test", "snapshot_gate"]
POST_GATE = {"build", "verify_image", "rename_old", "create_new", "start_new"}
STOPPED = {"confirm_stopped", "stop_all_mount_users"}


def _kinds(steps):
    return [step["kind"] for step in steps]


def check_main(main, paths):
    errors = []
    kinds = _kinds(main)
    if not kinds or kinds[0] != "record":
        errors.append("main must begin with the read-only record step")
    positions = []
    for kind in PRE_GATE:
        if kinds.count(kind) != 1:
            errors.append(f"main needs exactly one {kind} step")
            return errors
        positions.append(kinds.index(kind))
    if positions != sorted(positions):
        errors.append("pause, stop, snapshot, evidence, restore test and gate are out of order")
    gate = kinds.index("snapshot_gate")
    for index, kind in enumerate(kinds):
        if kind in POST_GATE and index < gate:
            errors.append(f"{main[index]['id']} ({kind}) runs before the snapshot gate")
    for index, step in enumerate(main):
        target = step.get("on_fail")
        if index == 0 and target == "stop_unchanged":
            continue
        if target not in paths:
            errors.append(f"{step['id']} has no rollback path")
    order = ["rename_old", "create_new", "start_new", "stop_all_mount_users_again",
             "validate_data", "restart_new", "runtime_reverify", "release_writers"]
    if any(kinds.count(kind) != 1 for kind in order):
        errors.append("main needs exactly one recreate and ordered acceptance step")
    elif [kinds.index(kind) for kind in order] != sorted(kinds.index(kind) for kind in order):
        errors.append("recreate and acceptance steps are out of order")
    if kinds and kinds[-1] != "release_writers":
        errors.append("writers must be released only as the last main step")
    return errors


def check_path(name, steps, entries, main_kinds):
    errors = []
    kinds = _kinds(steps)
    entered_after = lambda kind: kind in main_kinds and any(i >= main_kinds.index(kind) for i in entries)
    if not kinds:
        return [f"{name} is empty"]
    if entered_after("start_new"):
        if kinds[0] != "stop_all_mount_users":
            errors.append(f"{name} can follow the replacement start, so it must stop every mount user first")
        if "set_aside_new" not in kinds:
            errors.append(f"{name} must set the replacement aside")
    elif kinds[0] not in STOPPED:
        errors.append(f"{name} must begin with every mount user stopped")
    if "start_new" in kinds:
        errors.append(f"{name} must never start the replacement")
    if kinds.count("start_old") != 1:
        errors.append(f"{name} must start the retained old container exactly once")
        return errors
    start = kinds.index("start_old")
    if "set_aside_new" in kinds and kinds.index("set_aside_new") > start:
        errors.append(f"{name} sets the replacement aside after starting the old container")
    restores = [i for i, kind in enumerate(kinds) if kind == "restore"]
    if any(i > start for i in restores):
        errors.append(f"{name} restores data after a container has started")
    last_restore = restores[-1] if restores else -1
    if not any(kind == "validate_data" and last_restore < i < start for i, kind in enumerate(kinds)):
        errors.append(f"{name} must validate data after any restore and before starting the old container")
    if entered_after("rename_old"):
        if "rename_old_back" not in kinds or kinds.index("rename_old_back") > start:
            errors.append(f"{name} must rename the old container back before starting it")
    if kinds.count("release_writers") != 1 or kinds[-1] != "release_writers":
        errors.append(f"{name} must re-enable writers once, as its last step")
    return errors


def check(plan):
    main = plan["main"]
    paths = plan["rollback"]
    errors = check_main(main, paths)
    main_kinds = _kinds(main)
    for name, steps in paths.items():
        entries = [i for i, step in enumerate(main) if step.get("on_fail") == name]
        if not entries:
            errors.append(f"{name} is never used")
        errors.extend(check_path(name, steps, entries, main_kinds))
    ids = [step["id"] for step in main] + [s["id"] for steps in paths.values() for s in steps]
    if len(ids) != len(set(ids)):
        errors.append("step IDs must be unique")
    return errors


def ordered_ids(plan):
    return [step["id"] for step in plan["main"]] + [
        step["id"] for steps in plan["rollback"].values() for step in steps]


def main(argv=None):
    parser = argparse.ArgumentParser(description="Check runbook ordering rules (static).")
    parser.add_argument("plan", nargs="?", default=str(Path(__file__).with_name("runbook.json")))
    args = parser.parse_args(argv)
    errors = check(json.loads(Path(args.plan).read_text()))
    for error in errors:
        print("FAIL", error)
    print("runbook: OK" if not errors else f"runbook: {len(errors)} violation(s)")
    return int(bool(errors))


if __name__ == "__main__":
    sys.exit(main())
