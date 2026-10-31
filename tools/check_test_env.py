"""Report drift between this interpreter and the pinned records test packages.

For local parity checks before trusting a local ``unittest discover`` failure:
a failure under unpinned local packages may not be a repo defect. CI does not
run this tool. It is read-only: no pip, no subprocess, no network, no writes.

Usage: python3 tools/check_test_env.py [--requirements FILE]
Exit 0 every pin installed at exactly its version, 1 any differ, 2 usage error.
"""

import argparse
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import re
import sys

REPO = Path(__file__).resolve().parents[1]
DEFAULT = REPO / "requirements-records-test.txt"
PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;#]+)$")


def normalise(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def parse(text):
    pins, seen = [], set()
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = re.split(r"\s#", raw, maxsplit=1)[0].strip()
        if not line or line.startswith("#"):
            continue
        match = PIN.match(line)
        if not match:
            raise ValueError(f"line {lineno}: unsupported requirement")
        name = match.group(1)
        if normalise(name) in seen:
            raise ValueError(f"line {lineno}: duplicate {name}")
        seen.add(normalise(name))
        pins.append((name, match.group(2), lineno))
    return pins


def check(pins, version_of=version):
    results = []
    for name, pinned, _ in pins:
        try:
            installed = version_of(normalise(name))
        except PackageNotFoundError:
            results.append((name, "missing", None))
            continue
        results.append((name, "ok" if installed == pinned else "mismatch", installed))
    return results


def main(argv=None, version_of=version):
    parser = argparse.ArgumentParser(description="Report drift from pinned test packages.")
    parser.add_argument("--requirements", type=Path, default=DEFAULT)
    args = parser.parse_args(argv)
    try:
        pins = parse(args.requirements.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        print(f"check-test-env: {args.requirements}: {exc}", file=sys.stderr)
        return 2
    differ = 0
    for (name, pinned, _), (_, status, installed) in zip(pins, check(pins, version_of)):
        if status == "ok":
            print(f"{name} ok {pinned}")
            continue
        differ += 1
        if status == "missing":
            print(f"{name} missing (pinned {pinned})")
        else:
            print(f"{name} mismatch installed={installed} pinned={pinned}")
    py = ".".join(str(part) for part in sys.version_info[:3])
    print(f"check-test-env: {len(pins)} pins, {differ} differ (python {py})")
    return 1 if differ else 0


if __name__ == "__main__":
    sys.exit(main())
