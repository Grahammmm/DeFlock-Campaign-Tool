"""Check or copy an explicit allowlist of public resources into the wheel package."""
import argparse
import os
import stat
import sys
import tempfile
from pathlib import Path

RESOURCES = (
    "data/agencies/us-ca.json",
    "schemas/agency-seed.schema.json",
    "schemas/law-package.schema.json",
    "jurisdictions/us-ca/package.json",
    "templates/records-request.md",
)


def _read_source(source, name):
    """Read one canonical source; an OSError names only the resource path."""
    try:
        return source.read_bytes()
    except OSError as exc:
        raise type(exc)("unreadable source: %s (%s)" % (name, exc.strerror or type(exc).__name__)) from None


def _replace(source, target, original, name):
    """Write via a same-directory temp file, fsync, then os.replace; never leave a partial target."""
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=target.parent, prefix=".sync-")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(original)
            stream.flush()
            os.fchmod(stream.fileno(), stat.S_IMODE(source.stat().st_mode))
            os.fsync(stream.fileno())
        os.replace(temp, target)
    except OSError as exc:
        raise OSError("write failed: %s (%s)" % (name, exc.strerror or type(exc).__name__)) from None
    finally:
        if os.path.lexists(temp):
            os.unlink(temp)


def sync(root, write=False):
    root = Path(root).resolve()
    failures = []
    bundle = root / "campaign_tool" / "_resources"
    for candidate in (bundle, bundle.parent):
        if candidate.is_symlink():
            raise ValueError("symlink resource directory")
    allowed_files = set(RESOURCES)
    allowed_dirs = {
        parent.as_posix()
        for name in RESOURCES
        for parent in Path(name).parents
        if parent != Path(".")
    }
    # Inventory before copying anything, including in --write mode.
    # Unknown files are a review failure, never silently removed or packaged.
    if bundle.exists():
        for entry in sorted(bundle.rglob("*")):
            relative = entry.relative_to(bundle).as_posix()
            if entry.is_symlink():
                raise ValueError("symlink resource path: " + relative)
            allowed = (
                entry.is_file() and relative in allowed_files
            ) or (
                entry.is_dir() and relative in allowed_dirs
            )
            if not allowed:
                failures.append("unexpected resource: " + relative)
    if failures:
        return failures
    prepared = []
    for name in RESOURCES:
        source = root / name
        target = root / "campaign_tool" / "_resources" / name
        for candidate in (source, target):
            if candidate.is_symlink() or any(p.is_symlink() for p in candidate.parents if p != root and root in p.parents):
                raise ValueError("symlink resource path: " + name)
        if target.exists() and target.stat().st_nlink != 1:
            raise ValueError("multiply-linked resource target: " + name)
        original = _read_source(source, name)
        prepared.append((name, target, original))
    # All sources and destinations are checked before any destination is changed.
    for name, target, original in prepared:
        if write and not (target.is_file() and target.read_bytes() == original):
            _replace(root / name, target, original, name)
        if not target.is_file() or target.read_bytes() != original:
            failures.append(name)
    return failures


def states(root):
    """Read-only (name, state) per resource: ok, stale or missing-target."""
    root = Path(root).resolve()
    rows = []
    for name in RESOURCES:
        original, target = _read_source(root / name, name), root / "campaign_tool" / "_resources" / name
        rows.append((name, "missing-target" if not target.is_file() else "ok" if target.read_bytes() == original else "stale"))
    return rows


def main(argv=None, root=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="Copy the five public canonical resources; default is read-only")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1] if root is None else Path(root)
    try:
        unexpected = [item for item in sync(root, args.write) if item.startswith("unexpected resource: ")]
        rows = [] if unexpected else states(root)
    except (ValueError, OSError) as exc:
        print("Resource check stopped: %s" % exc, file=sys.stderr)
        return 2
    for item in unexpected:
        print("Resource check failed: " + item)
    for name, state in rows:
        print(state, name)
    if rows:
        count = [sum(state == key for _, state in rows) for key in ("ok", "stale", "missing-target")]
        print("resources=%d ok=%d stale=%d missing-target=%d" % (len(rows), *count))
    return int(bool(unexpected) or any(state != "ok" for _, state in rows))


if __name__ == "__main__":
    raise SystemExit(main())
