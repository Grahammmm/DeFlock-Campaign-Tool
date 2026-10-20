#!/usr/bin/env python3
"""Conservative swap-run pruning; invoked by rootless_swap.py prune.

The host selects terminal, superseded runs under the swap lock. The namespace
helper sees ONLY one selected run writable, never the private parent or live
mounts. The helper entry point removes its contents; the host removes the empty
run directory. No image, container, timer or live mount is changed.
"""
import argparse
import fcntl
import json
import os
import re
import shutil
import stat
import sys
from pathlib import Path

RUN_RE = re.compile(r"run-[0-9]{8}T[0-9]{6}Z")
IMAGE_RE = re.compile(r"(?:sha256:[0-9a-f]{64}|[a-z0-9][a-z0-9._/:-]*@sha256:[0-9a-f]{64})")
TERMINAL = {"complete", "rolled_back", "refused"}


def real_directory(path):
    """Refuse aliases, including symlinks in ancestors, before bind mounting."""
    if not path.is_absolute() or path != path.resolve() or not stat.S_ISDIR(path.lstat().st_mode):
        raise ValueError(f"not a real absolute directory: {path}")


def inventory(private, explicit_reuse):
    """Fail closed on unknown state; protect references from ALL runs, not just survivors."""
    real_directory(private)
    info = private.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("--private must be owned by this user with mode 0700")
    if any((p / ".git").exists() for p in (private, *private.parents)):
        raise ValueError("--private is inside a Git checkout")
    if any(c in str(private) for c in ',"\n'):
        raise ValueError("--private contains a comma, quote or newline")
    states = {}
    for path in sorted(private.glob("run-*")):
        if not RUN_RE.fullmatch(path.name):
            raise ValueError(f"unrecognized run directory: {path.name}")
        real_directory(path)
        state_path = path / "state.json"
        if not stat.S_ISREG(state_path.lstat().st_mode):
            raise ValueError(f"not a regular state.json: {path.name}")
        state = json.loads(state_path.read_text())
        if not isinstance(state, dict) or state.get("run_dir") != str(path) or state.get("private") != str(private):
            raise ValueError(f"state.json identity mismatch: {path.name}")
        states[path] = state
    protected = {max(states): ["newest run"]} if states else {}
    references = list(explicit_reuse)
    for path, state in states.items():
        if state.get("phase") not in TERMINAL:
            protected.setdefault(path, []).append("unfinished or unknown phase")
        reuse = state.get("reuse")
        if reuse is not None:
            if not isinstance(reuse, dict) or not isinstance(reuse.get("run_dir"), str):
                raise ValueError(f"invalid reuse reference in {path.name}")
            references.append(reuse["run_dir"])
    for reference in references:
        path = Path(os.path.expanduser(reference))
        if path not in states or path != path.resolve():
            raise ValueError(f"reuse reference is not a known direct run: {reference}")
        protected.setdefault(path, []).append("referenced by --reuse-image")
    return states, protected


def clear_contents(root):
    """Namespace-only cleanup, fd-relative and symlink-safe; never follow links.

    Pre-scan for nested mount points/device boundaries before deleting anything.
    The writable bind is the selected run itself, so siblings are inaccessible.
    """
    real_directory(root)
    if not shutil.rmtree.avoids_symlink_attacks:
        raise ValueError("safe fd-based rmtree is unavailable")
    # ismount() alone misses same-filesystem bind mounts on Linux. Docker
    # recursively binds submounts by default, so inspect the helper's mount table.
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        mountpoint = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), line.split()[4])
        if root in Path(mountpoint).parents:
            raise ValueError("refusing nested mount in selected run")
    device = root.stat().st_dev
    for parent, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(parent) / name
            if path.lstat().st_dev != device or (not path.is_symlink() and os.path.ismount(path)):
                raise ValueError("refusing nested mount in selected run")
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        # Keep state until last, so a failed cleanup can usually be previewed
        # and retried with the original protection graph intact.
        for name in sorted(os.listdir(fd), key=lambda n: (n == "state.json", n)):
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                shutil.rmtree(name, dir_fd=fd)
            else:
                os.unlink(name, dir_fd=fd)
    finally:
        os.close(fd)


def main(argv, *, system, out, swap_class, lock):
    parser = argparse.ArgumentParser(description="Prune superseded swap runs (dry-run by default)")
    parser.add_argument("--private", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="delete listed eligible runs (default: dry-run)")
    mode.add_argument("--dry-run", action="store_true")
    parser.add_argument("--reuse-image", action="append", default=[], metavar="RUN_DIR",
                        help="also protect a run intended for a future --reuse-image (repeatable)")
    parser.add_argument("--helper-image", help="immutable helper image override; otherwise each run's helper image")
    parser.add_argument("--helper-timeout", type=int, default=7200)
    args = parser.parse_args(argv)
    args.mode = "apply" if args.apply else "dry-run"
    swap = swap_class(args, system=system, out=out)
    private = Path(os.path.expanduser(args.private))
    handle = None
    try:
        # Validate before opening anything writable. Dry-run never creates a
        # lock file; it takes the existing lock read-only if there is one.
        inventory(private, args.reuse_image)
        lock_path = private / ".flock-swap.lock"
        if lock_path.is_symlink():
            raise ValueError("swap lock must not be a symlink")
        if args.apply:
            try:
                handle = lock(private)
            except SystemExit as error:
                raise ValueError(str(error)) from error
        elif lock_path.exists():
            handle = lock_path.open("r")
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        states, protected = inventory(private, args.reuse_image)
        candidates = [p for p in states if p not in protected]
        for path in states:
            swap.log(f"{'KEEP' if path in protected else 'PRUNE'} {path}: " +
                     (", ".join(protected[path]) if path in protected else "superseded terminal run"))
        if not args.apply:
            swap.log(f"prune: dry-run; {len(candidates)} eligible run(s); nothing changed. Use --apply to delete.")
            return 0
        if not candidates:
            swap.log("prune: no eligible runs")
            return 0
        _, options, _ = swap.sh(["docker", "info", "--format", "{{json .SecurityOptions}}"])
        if "name=rootless" not in options:
            raise ValueError("the Docker daemon is not rootless")
        # Resolve and validate every helper before any deletion. Never pull.
        images = {}
        for path in candidates:
            image = args.helper_image or states[path].get("helper_image", "")
            if not isinstance(image, str) or not IMAGE_RE.fullmatch(image):
                raise ValueError(f"no immutable helper image for {path.name}; use --helper-image")
            images[path] = swap.image_inspect(image)["Id"]
            if not IMAGE_RE.fullmatch(images[path]):
                raise ValueError("invalid resolved helper image")
        _, ids, _ = swap.sh(["docker", "ps", "-aq", "--no-trunc"])
        containers = []
        if ids.split():
            _, raw, _ = swap.sh(["docker", "inspect", "--type", "container", *ids.split()])
            containers = json.loads(raw)
        for path in candidates:
            # A stopped container can still depend on the files. Refuse all
            # candidates before deleting any when a container or process uses one.
            for container in containers:
                for mount in container.get("Mounts") or []:
                    source = mount.get("Source")
                    if source and (Path(source) == path or path in Path(source).parents or Path(source) in path.parents):
                        raise ValueError(f"a container mounts {path.name} or an overlapping path")
            if system.open_files_under([str(path)]):
                raise ValueError(f"a process uses {path.name}")
        for path in candidates:
            # Recheck the graph under the shared swap lock before each helper.
            current, kept = inventory(private, args.reuse_image)
            if path not in current or path in kept or current[path] != states[path]:
                raise ValueError("prune selection changed; retry dry-run")
            swap.state = {"run_dir": str(path), "helper_image": images[path], "mounts": []}
            swap.helper(["python3", "-B", "-I", "/recipe/prune_runs.py", "clear-contents", "/private"], live=None)
            path.rmdir()  # parent owned by the host user; helper removed subuid-owned contents
            if path.exists():
                raise ValueError(f"run still exists after pruning: {path.name}")
            swap.log(f"pruned {path.name}")
        swap.log(f"prune: removed {len(candidates)} superseded run(s)")
        return 0
    except Exception as error:
        swap.log(f"prune: refused or stopped: {error}")
        return 2
    finally:
        if handle is not None:
            handle.close()


if __name__ == "__main__":
    # Only the helper entry point is available here. Host selection is always
    # through rootless_swap.py, which supplies the existing namespace helper.
    if len(sys.argv) != 3 or sys.argv[1] != "clear-contents":
        sys.exit("use rootless_swap.py prune")
    clear_contents(Path(sys.argv[2]))
